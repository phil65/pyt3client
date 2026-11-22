"""Models for signing in to a T3 Code environment's `/mcp` endpoint.

Upstream schemas live in packages/contracts/src/auth.ts
(https://github.com/pingdotgg/t3code); names are noted per model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


type McpAccess = Literal[
    "read-only",
    "approval-required",
    "auto-accept-edits",
    "auto",
    "full-access",
]
"""Access level an outside MCP client is approved at (upstream `AuthMcpClientAccess`).

- `read-only`: read projects, threads, providers and models.
- `approval-required` ("Supervised") through `full-access`: also start, message
  and stop threads. A thread the client starts or steers can never run with a
  broader runtime mode than this level.

A plain pairing code must carry the scopes the level needs (upstream
`mcpClientScopes`): `orchestration:read` for `read-only`, plus
`orchestration:operate` for the others, plus `settings:write` and
`source-control:write` for `full-access`. The default code from
`t3 auth pairing create` carries all of them.
"""

type RuntimeMode = Literal["approval-required", "auto-accept-edits", "auto", "full-access"]
"""Upstream `RuntimeMode` (packages/contracts/src/providerPolicy.ts)."""

type InteractionMode = Literal["default", "plan"]
"""Upstream `ProviderInteractionMode`."""


class OAuthAuthorizationServerMetadata(BaseModel):
    """`GET /.well-known/oauth-authorization-server`.

    Upstream `AuthMcpAuthorizationServerMetadata`.
    """

    model_config = ConfigDict(extra="allow")

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes_supported: list[str]


class RegisteredClient(BaseModel):
    """`POST /oauth/mcp/register` result (upstream `AuthMcpRegisteredClient`).

    Registration is stateless: `client_id` is a signed blob of the metadata, so
    registering costs the server nothing and needs no cleanup.
    """

    model_config = ConfigDict(extra="allow")

    client_id: str
    client_name: str
    redirect_uris: list[str]


class ApprovalRedirect(BaseModel):
    """`POST /oauth/mcp/decision` result (upstream `AuthMcpApprovalRedirect`).

    `redirect_to` is the registered redirect URI carrying `code` and `state`,
    or `error` and `error_description` when the approval failed.
    """

    redirect_to: str = Field(alias="redirectTo")


class McpTokenResult(BaseModel):
    """`POST /oauth/mcp/token` result (upstream `AuthMcpTokenResult`)."""

    model_config = ConfigDict(extra="allow")

    access_token: SecretStr
    token_type: Literal["Bearer"]
    expires_in: float
    """Seconds. Outside MCP clients get a fixed 30 days; their sessions do not renew."""
    scope: str
    """Space-separated scopes, such as `orchestration:read orchestration:operate`."""


class T3Credentials(BaseModel):
    """What a successful sign-in yields; persist it to reconnect without pairing again.

    The token is an ordinary environment session with subject `mcp-client`.
    `/mcp` is the only endpoint that accepts it: the native WebSocket RPC and
    REST API reject this subject, because their surface would let the client
    act above its approved level. Revoke it in T3 Code under
    Settings -> Sharing & access.
    """

    base_url: str
    """Environment origin without a trailing slash, such as `http://127.0.0.1:3773`."""
    environment_id: str
    """The environment the token belongs to; check it against the descriptor before reuse."""
    access_token: SecretStr
    expires_at: datetime
    scopes: tuple[str, ...]
    access: McpAccess
    client_id: str


class T3NativeCredentials(BaseModel):
    """A sign-in for T3 Code's native surface: its WebSocket RPC and REST API.

    That surface is what the web, desktop and mobile clients use. It streams a
    thread's events as they happen and can answer approvals, which `/mcp` cannot.
    It rejects MCP tokens, and `/mcp` rejects this one, so a client that uses
    both holds two sign-ins. Obtained by exchanging a one-time pairing token
    (`t3 auth pairing create`, or Settings -> Sharing & access) at `/oauth/token`.
    """

    base_url: str
    """Environment origin without a trailing slash."""
    access_token: SecretStr
    expires_at: datetime
    scopes: tuple[str, ...]
