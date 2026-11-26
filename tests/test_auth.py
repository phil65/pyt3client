"""Headless MCP sign-in against a fake environment that checks the OAuth flow."""

import base64
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx2
import pytest

from pyt3client import T3AuthError, pair
from pyt3client.auth import REDIRECT_URI, parse_approval_code


BASE = "http://t3.test"
ENVIRONMENT_ID = "env-1"


@dataclass
class FakeOAuthServer:
    """Validates each step the way apps/server/src/auth/McpOAuth.ts does."""

    valid_codes: set[str] = field(default_factory=lambda: {"PLAINCODE", "AUTO-23456789ABCD"})
    decisions: list[dict[str, Any]] = field(default_factory=list)
    _challenge: str | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        match request.method, request.url.path:
            case "GET", "/.well-known/t3/environment":
                return httpx2.Response(
                    200,
                    json={
                        "environmentId": ENVIRONMENT_ID,
                        "label": "test",
                        "platform": {"os": "linux", "arch": "x64"},
                        "serverVersion": "test",
                        "capabilities": {"mcpApprovalCodes": True},
                    },
                )
            case "GET", "/.well-known/oauth-authorization-server":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": BASE,
                        "authorization_endpoint": f"{BASE}/oauth/mcp/authorize",
                        "token_endpoint": f"{BASE}/oauth/mcp/token",
                        "registration_endpoint": f"{BASE}/oauth/mcp/register",
                        "scopes_supported": ["orchestration:read", "orchestration:operate"],
                    },
                )
            case "POST", "/oauth/mcp/register":
                body = json.loads(request.content)
                assert body["redirect_uris"] == [REDIRECT_URI]
                return httpx2.Response(
                    201,
                    json={
                        "client_id": "client-1",
                        "client_name": body["client_name"],
                        "redirect_uris": body["redirect_uris"],
                    },
                )
            case "POST", "/oauth/mcp/decision":
                body = json.loads(request.content)
                self.decisions.append(body)
                authorization, decision = body["authorization"], body["decision"]
                assert authorization["code_challenge_method"] == "S256"
                assert authorization["resource"] == f"{BASE}/mcp"
                if decision["code"] not in self.valid_codes:
                    return httpx2.Response(
                        400,
                        json={"_tag": "AuthMcpApprovalError", "message": "That code is not valid."},
                    )
                self._challenge = authorization["code_challenge"]
                query = urlencode({"code": "auth-code", "state": authorization["state"]})
                return httpx2.Response(200, json={"redirectTo": f"{REDIRECT_URI}?{query}"})
            case "POST", "/oauth/mcp/token":
                form = {
                    key: values[0] for key, values in parse_qs(request.content.decode()).items()
                }
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if challenge != self._challenge or form["code"] != "auth-code":
                    return httpx2.Response(
                        400, json={"error": "invalid_grant", "error_description": "PKCE mismatch"}
                    )
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "mcp-token",
                        "token_type": "Bearer",
                        "expires_in": 2_592_000,
                        "scope": "orchestration:read orchestration:operate",
                    },
                )
            case method, path:
                return httpx2.Response(404, text=f"{method} {path}")


@pytest.fixture
def oauth() -> FakeOAuthServer:
    return FakeOAuthServer()


@pytest.fixture
def client(oauth: FakeOAuthServer) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(oauth.handle))


async def test_pair_with_plain_code(oauth: FakeOAuthServer, client: httpx2.AsyncClient) -> None:
    credentials = await pair(f"{BASE}/mcp", "PLAINCODE", access="read-only", http_client=client)
    assert credentials.base_url == BASE
    assert credentials.environment_id == ENVIRONMENT_ID
    assert credentials.access_token.get_secret_value() == "mcp-token"
    assert credentials.scopes == ("orchestration:read", "orchestration:operate")
    assert credentials.access == "read-only"
    assert oauth.decisions[0]["decision"] == {
        "_tag": "pairing-code",
        "access": "read-only",
        "code": "PLAINCODE",
    }


async def test_approval_code_sets_its_own_level(
    oauth: FakeOAuthServer, client: httpx2.AsyncClient
) -> None:
    credentials = await pair(BASE, " auto-23456789abcd ", access="read-only", http_client=client)
    assert credentials.access == "auto"
    # Sent in the canonical upper-case form, as upstream parseMcpApprovalCode returns it.
    assert oauth.decisions[0]["decision"] == {
        "_tag": "pairing-code",
        "access": "auto",
        "code": "AUTO-23456789ABCD",
    }


async def test_rejected_code_raises(client: httpx2.AsyncClient) -> None:
    with pytest.raises(T3AuthError, match="That code is not valid"):
        await pair(BASE, "WRONG", http_client=client)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AUTO-23456789ABCD", ("auto", "AUTO-23456789ABCD")),
        (" full-23456789abcd", ("full-access", "FULL-23456789ABCD")),
        ("SUPERVISED-23456789ABCD", ("approval-required", "SUPERVISED-23456789ABCD")),
        ("AUTO-1111", None),  # wrong length
        ("AUTO-0123456789AB", None),  # 0 and 1 are not in the pairing alphabet
        ("23456789ABCD", None),  # a plain pairing code
    ],
)
def test_parse_approval_code(code: str, expected: tuple[str, str] | None) -> None:
    assert parse_approval_code(code) == expected
