"""Sign in to a T3 Code environment's `/mcp` endpoint without a browser.

T3 Code lets outside agents use its MCP server through OAuth 2.1
(authorization code + PKCE, dynamic client registration). Interactive clients
send the user to an approval page; this module does what the page does, with a
pairing code instead of a click. It is the same flow T3 Code itself uses to
link one environment to another: upstream `PeerLinks.signIn` in
apps/server/src/peer/PeerLinks.ts.

The flow:

1. `GET  /.well-known/t3/environment`  public descriptor (environment id).
2. `POST /oauth/mcp/register`          register a public client (stateless).
3. `POST /oauth/mcp/decision`          approve with the pairing code; the answer
                                       is the redirect URL carrying `code`.
4. `POST /oauth/mcp/token`             exchange code + PKCE verifier for a token.

Server side: apps/server/src/auth/mcpOAuthHttp.ts (routes) and
apps/server/src/auth/McpOAuth.ts (validation, `approve`, `exchangeCode`).
Get a pairing code on the host with `t3 auth pairing create`, or in the app via
Settings -> Sharing & access.
"""

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
from typing import TYPE_CHECKING, get_args
from urllib.parse import parse_qs, urlsplit

import httpx2
from pydantic import ValidationError

from pyt3client.errors import T3AuthError, T3HttpError, T3ProtocolError, transport_errors
from pyt3client.models.auth import (
    ApprovalRedirect,
    McpAccess,
    McpTokenResult,
    OAuthAuthorizationServerMetadata,
    RegisteredClient,
    T3Credentials,
)
from pyt3client.models.environment import EnvironmentDescriptor


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel


DEFAULT_CLIENT_NAME = "pyt3client"
# Loopback redirects are accepted for any port (RFC 8252), and nothing has to
# listen here: `/oauth/mcp/decision` returns the redirect URL in its response
# body instead of redirecting a browser. Upstream uses the same trick with
# `http://127.0.0.1/t3code-peer-link`.
REDIRECT_URI = "http://127.0.0.1/pyt3client"

# Approval codes such as `AUTO-Q8RZ3NWD5KTC` carry their own level; the server
# applies that level whatever the request asks for. Upstream: `mcpApprovalCode`
# and `parseMcpApprovalCode` in packages/contracts/src/auth.ts.
_APPROVAL_CODE_PREFIXES: dict[str, McpAccess] = {
    "READ": "read-only",
    "SUPERVISED": "approval-required",
    "EDITS": "auto-accept-edits",
    "AUTO": "auto",
    "FULL": "full-access",
}
_PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_APPROVAL_CODE_PATTERN = re.compile(
    rf"^({'|'.join(_APPROVAL_CODE_PREFIXES)})-([{_PAIRING_CODE_ALPHABET}]{{12}})$", re.IGNORECASE
)


def parse_approval_code(code: str) -> tuple[McpAccess, str] | None:
    """Level and canonical form of an app approval code (`AUTO-…`), or None for a plain code.

    Mirrors upstream `parseMcpApprovalCode`: matching ignores case and
    surrounding space, and the canonical code is upper case.

    Args:
        code: A pairing code as shown by T3 Code.
    """
    if match := _APPROVAL_CODE_PATTERN.match(code.strip()):
        prefix = match.group(1).upper()
        return _APPROVAL_CODE_PREFIXES[prefix], f"{prefix}-{match.group(2).upper()}"
    return None


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode[ModelT: BaseModel](model: type[ModelT], response: httpx2.Response, what: str) -> ModelT:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as error:
        msg = f"Unexpected {what} response from {response.url}: {error}"
        raise T3ProtocolError(msg) from error


def _oauth_error(response: httpx2.Response, what: str) -> T3AuthError:
    """Turn an OAuth or approval error body into a readable exception.

    `/oauth/mcp/register` and `/oauth/mcp/token` answer RFC 6749 style
    `{"error", "error_description"}`; `/oauth/mcp/decision` answers
    `{"_tag": "AuthMcpApprovalError", "message"}`.
    """
    detail = response.text
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        description = body.get("error_description") or body.get("message") or body.get("error")
        if isinstance(description, str):
            detail = description
    return T3AuthError(f"{what} failed (HTTP {response.status_code}): {detail}")


def _normalize_base_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url.removesuffix("/mcp")


async def fetch_descriptor(
    base_url: str, *, http_client: httpx2.AsyncClient | None = None
) -> EnvironmentDescriptor:
    """Read an environment's public descriptor; needs no credential.

    Args:
        base_url: Environment origin, such as `http://127.0.0.1:3773`.
        http_client: Client to reuse; a short-lived one is created otherwise.
    """
    url = f"{_normalize_base_url(base_url)}/.well-known/t3/environment"
    async with _client_scope(http_client) as client:
        response = await client.get(url)
    if response.status_code != httpx2.codes.OK:
        msg = f"{url} is not a T3 Code environment"
        raise T3HttpError(msg, status=response.status_code, body=response.text)
    return _decode(EnvironmentDescriptor, response, "environment descriptor")


async def pair(
    base_url: str,
    pairing_code: str,
    *,
    access: McpAccess = "auto",
    client_name: str = DEFAULT_CLIENT_NAME,
    http_client: httpx2.AsyncClient | None = None,
) -> T3Credentials:
    """Sign in to an environment's `/mcp` with a one-time pairing code.

    The code is spent by this call. The resulting session shows up in T3 Code
    under Settings -> Sharing & access with `client_name` as its label, lasts
    30 days, and does not renew.

    Agents only send credentials over `https` or to `localhost`, so use a
    loopback, Tailscale HTTPS or T3 Connect address. A plain-http LAN address
    works technically but sends the code unencrypted.

    Args:
        base_url: Environment origin, such as `http://127.0.0.1:3773`. A trailing
            `/mcp` (as shown by "Copy MCP URL") is accepted.
        pairing_code: Code from `t3 auth pairing create` or the app. An app
            approval code (`AUTO-…`) fixes the level itself and overrides `access`;
            it is matched case-insensitively and sent in canonical upper case.
        access: Level to approve the client at. Must not exceed what the code grants.
        client_name: Label the environment shows for this client.
        http_client: Client to reuse; a short-lived one is created otherwise.

    Raises:
        T3AuthError: The environment rejected the code or the sign-in.
        T3HttpError: The address does not answer like a T3 Code environment.
        T3ConnectionError: The address could not be reached.
    """
    origin = _normalize_base_url(base_url)
    code = pairing_code.strip()
    if approval := parse_approval_code(code):
        access, code = approval
    if access not in get_args(McpAccess.__value__):
        msg = f"Unknown access level {access!r}"
        raise ValueError(msg)
    async with _client_scope(http_client) as client:
        descriptor = await fetch_descriptor(origin, http_client=client)
        metadata_response = await client.get(f"{origin}/.well-known/oauth-authorization-server")
        if metadata_response.status_code != httpx2.codes.OK:
            msg = "Environment does not offer MCP sign-in"
            raise T3HttpError(
                msg, status=metadata_response.status_code, body=metadata_response.text
            )
        metadata = _decode(
            OAuthAuthorizationServerMetadata, metadata_response, "authorization server metadata"
        )

        registered_response = await client.post(
            metadata.registration_endpoint,
            json={"client_name": client_name, "redirect_uris": [REDIRECT_URI]},
        )
        if registered_response.status_code not in {httpx2.codes.OK, httpx2.codes.CREATED}:
            raise _oauth_error(registered_response, "Client registration")
        registered = _decode(RegisteredClient, registered_response, "client registration")

        verifier = _base64url(secrets.token_bytes(32))
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _base64url(secrets.token_bytes(16))
        # `resource` must name this server's `/mcp` (RFC 8707); the server derives
        # its own from Host and X-Forwarded-Proto, so a rewriting proxy breaks this.
        resource = f"{origin}/mcp"
        authorization = {
            "response_type": "code",
            "client_id": registered.client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
        }
        decision_response = await client.post(
            f"{origin}/oauth/mcp/decision",
            json={
                "authorization": authorization,
                "decision": {"_tag": "pairing-code", "access": access, "code": code},
            },
        )
        if decision_response.status_code != httpx2.codes.OK:
            raise _oauth_error(decision_response, "Approval")
        redirect = _decode(ApprovalRedirect, decision_response, "approval")
        params = parse_qs(urlsplit(redirect.redirect_to).query)
        if params.get("state") != [state] or "code" not in params:
            description = params.get("error_description") or params.get("error") or ["no code"]
            msg = f"Approval was refused: {description[0]}"
            raise T3AuthError(msg)

        token_response = await client.post(
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": params["code"][0],
                "redirect_uri": REDIRECT_URI,
                "client_id": registered.client_id,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        if token_response.status_code != httpx2.codes.OK:
            raise _oauth_error(token_response, "Token exchange")
        token = _decode(McpTokenResult, token_response, "token")
    return T3Credentials(
        base_url=origin,
        environment_id=descriptor.environment_id,
        access_token=token.access_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in),
        scopes=tuple(token.scope.split()),
        access=access,
        client_id=registered.client_id,
    )


@asynccontextmanager
async def _client_scope(client: httpx2.AsyncClient | None) -> AsyncGenerator[httpx2.AsyncClient]:
    """Use a caller's httpx2 client, or own a short-lived one for the duration."""
    with transport_errors("T3 Code request"):
        if client is not None:
            yield client
            return
        async with httpx2.AsyncClient(timeout=30) as owned:
            yield owned
