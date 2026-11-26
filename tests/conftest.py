"""Test helpers: an in-memory stand-in for a T3 Code environment's HTTP surface."""

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest


if TYPE_CHECKING:
    from collections.abc import Callable


BASE_URL = "http://t3.test"
TOKEN = "test-token"

type ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class FakeMcpServer:
    """Answers `/mcp` like T3 Code's server: sessions, tools/list, tools/call.

    Tool handlers return the raw MCP `tools/call` result
    (`{"content": [...], "structuredContent": ..., "isError": ...}`).
    """

    tools: dict[str, ToolHandler] = field(default_factory=dict)
    sse: bool = False
    """Answer with `text/event-stream` instead of JSON, as some MCP servers do."""
    sessions_opened: int = 0
    requests: list[httpx2.Request] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    drop_session_once: bool = False
    """Answer the next session request with 404, as after a server restart."""
    _live_session: str | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx2.Response(401, json={"error": "invalid_token"})
        if request.method == "DELETE":
            self._live_session = None
            return httpx2.Response(200)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            self.sessions_opened += 1
            self._live_session = f"session-{self.sessions_opened}"
            return self._respond(
                body["id"],
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "T3 Code", "version": "test"},
                },
                headers={"mcp-session-id": self._live_session},
            )
        session = request.headers.get("mcp-session-id")
        if self.drop_session_once:
            self.drop_session_once = False
            self._live_session = None
        if session is None or session != self._live_session:
            return httpx2.Response(404)
        if request.headers.get("mcp-protocol-version") != "2025-06-18":
            return httpx2.Response(400)
        match body["method"]:
            case "notifications/initialized":
                return httpx2.Response(202)
            case "tools/list":
                tools = [{"name": name, "inputSchema": {"type": "object"}} for name in self.tools]
                return self._respond(body["id"], {"tools": tools})
            case "tools/call":
                name = body["params"]["name"]
                arguments = body["params"]["arguments"]
                self.calls.append((name, arguments))
                if name not in self.tools:
                    return self._error(body["id"], -32602, f"Tool {name} not found")
                return self._respond(body["id"], self.tools[name](arguments))
            case method:
                return self._error(body["id"], -32601, f"Unknown method {method}")

    def _respond(
        self, request_id: int, result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if self.sse:
            progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            text = f"event: message\ndata: {json.dumps(progress)}\n\n"
            text += f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx2.Response(
                200,
                text=text,
                headers={"content-type": "text/event-stream", **(headers or {})},
            )
        return httpx2.Response(200, json=message, headers=headers)

    def _error(self, request_id: int, code: int, message: str) -> httpx2.Response:
        error = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        return httpx2.Response(200, json=error)


def tool_result(payload: dict[str, Any], *, structured: bool = True) -> dict[str, Any]:
    """A successful T3 tool result: JSON text block plus structuredContent."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if structured:
        result["structuredContent"] = payload
    return result


def tool_failure(code: str, message: str) -> dict[str, Any]:
    """A T3 tool refusal, as `OrchestratorMcpFailure` text with isError."""
    failure = {"_tag": "OrchestratorMcpFailure", "code": code, "message": message}
    return {"content": [{"type": "text", "text": json.dumps(failure)}], "isError": True}


@pytest.fixture
def fake_server() -> FakeMcpServer:
    return FakeMcpServer()


@pytest.fixture
def http_client(fake_server: FakeMcpServer) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(fake_server.handle))
