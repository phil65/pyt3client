"""Minimal MCP Streamable HTTP client, just enough for T3 Code's `/mcp`.

T3 Code needs only `initialize`, `tools/list` and `tools/call`, with no
sampling, elicitation or server-initiated requests, so this speaks that subset
directly over httpx rather than pulling in a full MCP SDK. It mirrors upstream's
own client for linked environments, apps/server/src/peer/PeerMcpClient.ts
(https://github.com/pingdotgg/t3code):

- POST JSON-RPC to `/mcp` with `accept: application/json, text/event-stream`.
- `initialize` answers with an `mcp-session-id` header; later requests send it
  plus `mcp-protocol-version`. The server returns 400 without that header.
- Sessions live in server memory, so after a server restart a request gets 404.
  The session is then opened again and the request retried once.
- Requests must not carry an `Origin` header: `/mcp` answers 403 to anything
  that looks like a browser (apps/server/src/mcp/McpHttpServer.ts). httpx sends
  none by default.
"""

from __future__ import annotations

import asyncio
from itertools import count
import json
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from pyt3client.errors import T3AuthError, T3HttpError, T3McpError, T3ProtocolError
from pyt3client.log import get_logger


if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import SecretStr


logger = get_logger(__name__)

PROTOCOL_VERSION = "2025-06-18"
"""MCP revision T3 Code speaks (`PROTOCOL_VERSION` in upstream PeerMcpClient.ts)."""


class _JsonRpcError(BaseModel):
    code: int
    message: str
    data: JsonValue = None


class _JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    result: dict[str, JsonValue] | None = None
    error: _JsonRpcError | None = None


class ContentBlock(BaseModel):
    """One block of a tool result. T3 Code answers with a single JSON `text` block."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class CallToolResult(BaseModel):
    """MCP `tools/call` result."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content: list[ContentBlock] = Field(default_factory=list)
    structured_content: dict[str, JsonValue] | None = Field(None, alias="structuredContent")
    """Present for tools with an `outputSchema`; the same JSON as the text block."""
    is_error: bool = Field(False, alias="isError")

    @property
    def text(self) -> str:
        """All text blocks joined, as upstream reads them."""
        return "".join(block.text or "" for block in self.content)


class ToolAnnotations(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    read_only_hint: bool | None = Field(None, alias="readOnlyHint")
    destructive_hint: bool | None = Field(None, alias="destructiveHint")
    idempotent_hint: bool | None = Field(None, alias="idempotentHint")
    open_world_hint: bool | None = Field(None, alias="openWorldHint")


class ToolInfo(BaseModel):
    """One entry of `tools/list`."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, JsonValue] = Field(alias="inputSchema")
    output_schema: dict[str, JsonValue] | None = Field(None, alias="outputSchema")
    annotations: ToolAnnotations | None = None


def _parse_messages(response: httpx.Response) -> list[_JsonRpcResponse]:
    """Decode a JSON body, or every `data:` event of an SSE body."""
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return [_JsonRpcResponse.model_validate_json(response.content)]
    messages: list[_JsonRpcResponse] = []
    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line.removeprefix("data:").lstrip()
            for line in event.split("\n")
            if line.startswith("data:")
        )
        if data:
            parsed = json.loads(data)
            # Notifications (progress, logging) have no `id`; skip them.
            if isinstance(parsed, dict) and "id" in parsed:
                messages.append(_JsonRpcResponse.model_validate(parsed))
    return messages


class McpHttpSession:
    """One MCP session against a T3 Code environment's `/mcp`.

    Safe for concurrent use: requests are independent HTTP POSTs, and the
    session is opened once, lazily, under a lock.
    """

    def __init__(
        self,
        url: str,
        token: SecretStr,
        *,
        http_client: httpx.AsyncClient,
        headers: Mapping[str, str] | None = None,
        client_name: str = "pyt3client",
        client_version: str = "0",
    ) -> None:
        """Create a session; nothing is sent until the first request.

        Args:
            url: Full `/mcp` endpoint URL.
            token: Bearer token for the endpoint.
            http_client: Client used for every request; owned by the caller.
            headers: Extra headers sent with every request (such as `t3-mode-limit`).
            client_name: `clientInfo.name` reported in `initialize`.
            client_version: `clientInfo.version` reported in `initialize`.
        """
        self._url = url
        self._token = token
        self._http = http_client
        self._headers = dict(headers or {})
        self._client_info = {"name": client_name, "version": client_version}
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        self._ids = count(1)
        self.server_info: dict[str, JsonValue] | None = None
        """`serverInfo` from `initialize`, such as `{"name": "T3 Code", "version": ...}`."""

    def _request_headers(self, *, session: bool) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self._token.get_secret_value()}",
            **self._headers,
        }
        if session and self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
            headers["mcp-protocol-version"] = PROTOCOL_VERSION
        return headers

    def _check_status(self, response: httpx.Response, method: str) -> None:
        match response.status_code:
            case httpx.codes.OK | httpx.codes.ACCEPTED:
                return
            case httpx.codes.UNAUTHORIZED:
                msg = (
                    f"{method}: the environment no longer accepts this credential. It may have"
                    " been revoked under Settings -> Sharing & access or expired; pair again."
                )
                raise T3AuthError(msg)
            case status:
                msg = f"{method} was refused"
                raise T3HttpError(msg, status=status, body=response.text)

    async def _open(self) -> str:
        response = await self._http.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": self._client_info,
                },
            },
            headers=self._request_headers(session=False),
        )
        self._check_status(response, "initialize")
        session_id = response.headers.get("mcp-session-id")
        if session_id is None:
            msg = "initialize: the server did not start an MCP session"
            raise T3ProtocolError(msg)
        result = self._result("initialize", response, 0)
        server_info = result.get("serverInfo")
        self.server_info = server_info if isinstance(server_info, dict) else None
        self._session_id = session_id
        notified = await self._http.post(
            self._url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._request_headers(session=True),
        )
        self._check_status(notified, "notifications/initialized")
        logger.debug("Opened MCP session %s on %s", session_id, self._url)
        return session_id

    async def _ensure_session(self, stale: str | None = None) -> None:
        async with self._lock:
            # Another task may already have replaced the stale session.
            if self._session_id is None or self._session_id == stale:
                self._session_id = None
                await self._open()

    def _result(
        self, method: str, response: httpx.Response, request_id: int
    ) -> dict[str, JsonValue]:
        try:
            messages = _parse_messages(response)
        except (ValidationError, ValueError) as error:
            msg = f"{method}: unreadable response: {error}"
            raise T3ProtocolError(msg) from error
        message = next((m for m in messages if m.id == request_id), None)
        if message is None:
            msg = f"{method}: the server sent no response for request {request_id}"
            raise T3ProtocolError(msg)
        if message.error is not None:
            raise T3McpError(method, message.error.code, message.error.message)
        if message.result is None:
            msg = f"{method}: the server returned neither a result nor an error"
            raise T3ProtocolError(msg)
        return message.result

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, JsonValue]:
        """Send one JSON-RPC request and return its `result`.

        Args:
            method: JSON-RPC method, such as `tools/call`.
            params: Request params.
            timeout: Read timeout in seconds for this request; the client's default otherwise.

        Raises:
            T3AuthError: The token was rejected (HTTP 401).
            T3McpError: The server answered with a JSON-RPC error.
            T3HttpError: Any other unexpected HTTP status.
            T3ProtocolError: The response could not be read.
        """
        await self._ensure_session()
        for attempt in range(2):
            session_id = self._session_id
            request_id = next(self._ids)
            response = await self._http.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                },
                headers=self._request_headers(session=True),
                timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
            )
            if response.status_code == httpx.codes.NOT_FOUND and attempt == 0:
                logger.debug("MCP session %s is gone (server restart?); reopening", session_id)
                await self._ensure_session(stale=session_id)
                continue
            self._check_status(response, method)
            return self._result(method, response, request_id)
        msg = f"{method}: the server dropped the MCP session twice"
        raise T3ProtocolError(msg)

    async def list_tools(self) -> list[ToolInfo]:
        """All tools the server offers this credential, following pagination."""
        tools: list[ToolInfo] = []
        cursor: str | None = None
        while True:
            result = await self.request("tools/list", {} if cursor is None else {"cursor": cursor})
            try:
                tools.extend(
                    ToolInfo.model_validate(tool) for tool in _as_list(result.get("tools"))
                )
            except ValidationError as error:
                msg = f"tools/list: unexpected tool shape: {error}"
                raise T3ProtocolError(msg) from error
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float | None = None,
    ) -> CallToolResult:
        """Call a tool and return the raw MCP result (refusals are not raised here)."""
        result = await self.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout=timeout
        )
        try:
            return CallToolResult.model_validate(result)
        except ValidationError as error:
            msg = f"tools/call {name}: unexpected result shape: {error}"
            raise T3ProtocolError(msg) from error

    async def close(self) -> None:
        """End the session on the server (best effort)."""
        if self._session_id is None:
            return
        try:
            await self._http.delete(self._url, headers=self._request_headers(session=True))
        except httpx.HTTPError as error:
            logger.debug("Closing MCP session failed: %s", error)
        finally:
            self._session_id = None


def _as_list(value: JsonValue) -> list[JsonValue]:
    if isinstance(value, list):
        return value
    msg = f"expected a list, got {type(value).__name__}"
    raise T3ProtocolError(msg)
