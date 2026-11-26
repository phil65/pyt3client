"""Exceptions raised by pyt3client."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

import httpx2


if TYPE_CHECKING:
    from collections.abc import Generator


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


class T3ConnectionError(T3Error):
    """The server could not be reached, or the connection failed or timed out.

    Wraps httpx2's transport errors (connect errors, timeouts, dropped connections),
    which stay available as `__cause__`.
    """


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


class T3RpcError(T3Error):
    """A native RPC failed: the server ended the request with a failure, or the socket closed.

    Native RPC is Effect RPC over T3 Code's WebSocket (see `pyt3client.native`). A
    request that fails ends with an `Exit` whose cause is a typed error (`Fail`, such
    as `OrchestrationV2DispatchCommandError`), a defect (`Die`) or an interruption.
    """

    def __init__(self, tag: str, message: str, *, cause: object = None) -> None:
        super().__init__(f"{tag}: {message}")
        self.tag = tag
        self.message = message
        self.cause = cause
        """The failure as the server encoded it (JSON), if any."""


@contextmanager
def transport_errors(what: str, url: object = None) -> Generator[None]:
    """Raise httpx2 transport errors in the block as `T3ConnectionError`.

    Args:
        what: The operation, for the message.
        url: The address, for the message; the failed request's URL by default.
    """
    try:
        yield
    except httpx2.TransportError as error:
        if url is None:
            try:
                url = error.request.url
            except RuntimeError:  # raised without a request
                url = "?"
        msg = f"{what}: {url}: {type(error).__name__}: {error}"
        raise T3ConnectionError(msg) from error
