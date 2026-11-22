"""Exceptions raised by pyt3client."""

from __future__ import annotations

from typing import Literal


type OrchestratorFailureCode = Literal[
    "capability_denied",
    "parent_not_active",
    "provider_unavailable",
    "model_unavailable",
    "runtime_mode_escalation_denied",
    "interaction_mode_escalation_denied",
    "task_not_found",
    "task_not_cancellable",
    "thread_not_found",
    "run_not_found",
    "thread_not_sendable",
    "thread_not_interruptible",
    "invalid_request",
    "orchestration_error",
    "thread_credential_required",
    "target_required",
]
"""Codes of a typed tool refusal.

Mirrors `OrchestratorMcpFailure.code` in upstream
`packages/contracts/src/orchestratorMcp.ts`. Newer servers may send codes not
listed here, so `T3ToolError.code` is typed `OrchestratorFailureCode | str`.
"""


class T3Error(Exception):
    """Base class for every error pyt3client raises."""


class T3HttpError(T3Error):
    """The server answered with an unexpected HTTP status."""

    def __init__(self, message: str, *, status: int, body: str) -> None:
        super().__init__(f"{message} (HTTP {status}): {body[:500]}")
        self.status = status
        self.body = body


class T3AuthError(T3Error):
    """Signing in failed, or the server no longer accepts the credential.

    Raised for a rejected pairing code, a revoked or expired token (HTTP 401
    on `/mcp`), and OAuth errors from `/oauth/mcp/*`.
    """


class T3ProtocolError(T3Error):
    """The server answered in a shape this client does not understand.

    Usually a version mismatch: regenerate the models against the server
    (see `scripts/generate_models.py`) or update pyt3client.
    """


class T3McpError(T3Error):
    """The server answered a request with a JSON-RPC error.

    Unlike `T3ToolError` (a tool that ran and refused), this means the request
    itself was rejected: an unknown tool, arguments that fail the tool's input
    schema, or a malformed request.
    """

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method}: JSON-RPC error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message


class T3ToolError(T3Error):
    """A tool call was refused.

    T3 Code reports expected refusals as an MCP result with `isError: true`
    whose text is `{"_tag": "OrchestratorMcpFailure", "code": ..., "message": ...}`.
    """

    def __init__(self, tool: str, code: OrchestratorFailureCode | str, message: str) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message
