"""High-level async client for a T3 Code environment."""

import json
import time
from typing import TYPE_CHECKING, Self

import httpx2
from pydantic import JsonValue, SecretStr, ValidationError

from pyt3client.auth import fetch_descriptor
from pyt3client.errors import T3ProtocolError, T3ToolError
from pyt3client.mcp import McpHttpSession
from pyt3client.models.tools import THREAD_WAIT, ThreadWaitInput


if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from pydantic import BaseModel

    from pyt3client.mcp import CallToolResult, ToolInfo
    from pyt3client.models.auth import InteractionMode, RuntimeMode, T3Credentials
    from pyt3client.models.environment import EnvironmentDescriptor
    from pyt3client.models.tools import ThreadWaitOutput
    from pyt3client.tool import ToolSpec


MAX_WAIT_CHUNK_SECONDS = 50.0
"""Longest single `t3_thread_wait` call `wait_for_thread` makes.

`/mcp` sends nothing while a wait is open, and T3 Connect's edge drops idle
requests after about 100 s, so upstream splits forwarded waits into calls of
at most 50 s (see "Peer links" in docs/internals/remote.md upstream).
"""

_WAIT_SLACK_SECONDS = 30.0
"""Extra HTTP read time beyond a wait's own timeout, for server-side overhead."""


class T3Client:
    """Typed client for one T3 Code environment, over its `/mcp` endpoint.

    Every tool the environment offers is reachable through `call`, typed by the
    `ToolSpec` constants in `pyt3client.models.tools`:

    ```python
    from pyt3client import T3Client, pair
    from pyt3client.models.tools import THREAD_LAUNCH, ThreadLaunchInput

    credentials = await pair("http://127.0.0.1:3773", "PAIRING-CODE")
    async with T3Client.from_credentials(credentials) as t3:
        launched = await t3.call(
            THREAD_LAUNCH,
            ThreadLaunchInput(project_id="...", title="Fix tests", message="Fix the tests"),
        )
        done = await t3.wait_for_thread(launched.thread_id, run_id=launched.run_id)
    ```

    Get credentials with `pyt3client.pair()`; T3 Code's server reference for the
    tools is docs/orchestration-v2/orchestrator-mcp-server.md upstream.
    """

    def __init__(
        self,
        base_url: str,
        token: str | SecretStr,
        *,
        mode_limit: tuple[RuntimeMode, InteractionMode] | None = None,
        client_name: str = "pyt3client",
        timeout: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        """Create a client; the MCP session opens on first use.

        Args:
            base_url: Environment origin, such as `http://127.0.0.1:3773`.
            token: MCP bearer token, as returned by `pair()`.
            mode_limit: Narrow this client below its approved level for every call:
                threads it starts or steers run with at most this runtime and
                interaction mode. Sent as the `t3-mode-limit` header, which the
                server only ever uses to narrow (upstream `MODE_LIMIT_HEADER` in
                apps/server/src/mcp/McpInvocationContext.ts).
            client_name: Name reported in the MCP `initialize` handshake.
            timeout: Default HTTP timeout in seconds.
            http_client: Client to use; created and owned by this instance otherwise.
        """
        self.base_url = base_url.rstrip("/").removesuffix("/mcp")
        self._owns_http = http_client is None
        self._http = http_client or httpx2.AsyncClient(timeout=timeout)
        headers = (
            {} if mode_limit is None else {"t3-mode-limit": f"{mode_limit[0]}/{mode_limit[1]}"}
        )
        self._session = McpHttpSession(
            f"{self.base_url}/mcp",
            token if isinstance(token, SecretStr) else SecretStr(token),
            http_client=self._http,
            headers=headers,
            client_name=client_name,
        )

    @classmethod
    def from_credentials(
        cls,
        credentials: T3Credentials,
        *,
        mode_limit: tuple[RuntimeMode, InteractionMode] | None = None,
        client_name: str = "pyt3client",
        timeout: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Self:
        """Create a client from the result of `pair()`.

        Args:
            credentials: Credentials from `pair()`.
            mode_limit: See `T3Client`.
            client_name: See `T3Client`.
            timeout: See `T3Client`.
            http_client: See `T3Client`.
        """
        return cls(
            credentials.base_url,
            credentials.access_token,
            mode_limit=mode_limit,
            client_name=client_name,
            timeout=timeout,
            http_client=http_client,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """End the MCP session and close the HTTP client if this instance owns it."""
        await self._session.close()
        if self._owns_http:
            await self._http.aclose()

    @property
    def server_info(self) -> dict[str, JsonValue] | None:
        """`serverInfo` from the MCP handshake, once a request has been made."""
        return self._session.server_info

    async def descriptor(self) -> EnvironmentDescriptor:
        """The environment's public descriptor (id, version, capability flags)."""
        return await fetch_descriptor(self.base_url, http_client=self._http)

    async def list_tools(self) -> list[ToolInfo]:
        """Tools the server offers this credential, with their JSON Schemas.

        Useful to detect drift against the generated models: a newer server
        may offer tools `pyt3client.models.tools` does not know yet.
        """
        return await self._session.list_tools()

    async def call[InputT: BaseModel, OutputT: BaseModel](
        self,
        tool: ToolSpec[InputT, OutputT],
        params: InputT | None = None,
        *,
        timeout: float | None = None,
    ) -> OutputT:
        """Call a tool with typed arguments and return its typed result.

        Args:
            tool: The tool, such as `pyt3client.models.tools.THREAD_LIST`.
            params: Its arguments; `tool.input_type()` (all defaults) when omitted.
            timeout: HTTP read timeout in seconds for this call.

        Raises:
            T3ToolError: The tool refused, such as `thread_not_found` or
                `capability_denied` (a read-only sign-in calling a write tool).
            T3McpError: The request was rejected, such as invalid arguments.
            T3ProtocolError: The result does not match the generated model.
        """
        arguments = (params if params is not None else tool.input_type()).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        result = await self._session.call_tool(tool.name, arguments, timeout=timeout)
        payload = _payload(tool.name, result)
        try:
            return tool.output_type.model_validate(payload)
        except ValidationError as error:
            msg = (
                f"{tool.name}: result does not match the generated model; the server may be"
                f" newer than the snapshot (regenerate with scripts/generate_models.py): {error}"
            )
            raise T3ProtocolError(msg) from error

    async def call_raw(
        self,
        name: str,
        arguments: Mapping[str, JsonValue] | None = None,
        *,
        timeout: float | None = None,
    ) -> JsonValue:
        """Call any tool by name with untyped arguments; for tools newer than the models.

        Raises:
            T3ToolError: The tool refused.
            T3McpError: The request was rejected.
        """
        result = await self._session.call_tool(name, arguments or {}, timeout=timeout)
        return _payload(name, result)

    async def wait_for_thread(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
        until_delegated_work_settled: bool = False,
        environment_id: str | None = None,
    ) -> ThreadWaitOutput:
        """Wait until a thread's run ends, for longer than one HTTP request may stay idle.

        Repeats `t3_thread_wait` in chunks of at most `MAX_WAIT_CHUNK_SECONDS`.
        A run ends as `completed`, `failed`, `cancelled`, `interrupted` or
        `rolled_back`; an idle thread returns at once. Timing out does not stop
        the run: the result then has `timed_out=True` and the latest status.

        Args:
            thread_id: Thread to wait on.
            run_id: Run to wait for. Without it the server pins the thread's latest
                run on the first call, and later chunks keep waiting on that run.
            timeout: Overall limit in seconds; waits until the run ends otherwise.
            until_delegated_work_settled: Also wait for the run's subagents and
                background tasks; `result_run` then names the run with the answer.
            environment_id: Linked environment the thread lives in, if not this one.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            chunk = MAX_WAIT_CHUNK_SECONDS
            if deadline is not None:
                chunk = min(chunk, max(deadline - time.monotonic(), 0.0))
            params = ThreadWaitInput(thread_id=thread_id, timeout_ms=chunk * 1000)
            if run_id is not None:
                params.run_id = run_id
            if until_delegated_work_settled:
                params.until_delegated_work_settled = True
            if environment_id is not None:
                params.environment_id = environment_id
            result = await self.call(THREAD_WAIT, params, timeout=chunk + _WAIT_SLACK_SECONDS)
            if not result.timed_out:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                return result
            run_id = result.run_id or run_id


def _payload(tool: str, result: CallToolResult) -> JsonValue:
    """The JSON a tool returned, or its refusal raised as `T3ToolError`.

    T3 Code puts the result JSON in a single text block and, for tools with an
    `outputSchema`, also in `structuredContent`. Refusals are `isError: true`
    with `{"_tag": "OrchestratorMcpFailure", "code", "message"}` as text
    (upstream `OrchestratorMcpFailure`, packages/contracts/src/orchestratorMcp.ts).
    """
    text = result.text
    if result.is_error:
        try:
            failure = json.loads(text)
        except ValueError:
            failure = None
        if isinstance(failure, dict):
            code = failure.get("code")
            message = failure.get("message")
            if isinstance(code, str) and isinstance(message, str):
                raise T3ToolError(tool, code, message)
        raise T3ToolError(tool, "orchestration_error", text or "the tool refused the call")
    if result.structured_content is not None:
        return result.structured_content
    if not text:
        return None
    try:
        payload: JsonValue = json.loads(text)
    except ValueError:
        # A few tools answer plain prose; hand it back unchanged.
        return text
    return payload
