"""T3Client against the in-memory fake `/mcp` server from conftest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pyt3client import T3AuthError, T3Client, T3McpError, T3ProtocolError, T3ToolError
from pyt3client.models.tools import (
    PENDING_REQUEST,
    THREAD_LIST,
    THREAD_READ,
    PendingRequestInput,
    ThreadListInput,
    ThreadReadInput,
)

from .conftest import BASE_URL, TOKEN, FakeMcpServer, tool_failure, tool_result


if TYPE_CHECKING:
    import httpx


THREAD_LIST_RESULT = {
    "projectId": None,
    "currentThreadId": None,
    "threads": [],
    "nextCursor": None,
    "total": 0,
}


def make_client(http_client: httpx.AsyncClient, **kwargs: Any) -> T3Client:
    return T3Client(BASE_URL, TOKEN, http_client=http_client, **kwargs)


async def test_typed_call(fake_server: FakeMcpServer, http_client: httpx.AsyncClient) -> None:
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    async with make_client(http_client) as t3:
        result = await t3.call(THREAD_LIST, ThreadListInput(all_projects=True, limit=5))
        assert t3.server_info == {"name": "T3 Code", "version": "test"}
    assert result.total == 0
    assert result.threads == []
    # snake_case in Python, camelCase on the wire; unset fields are not sent
    assert fake_server.calls == [("t3_thread_list", {"allProjects": True, "limit": 5})]
    assert fake_server.sessions_opened == 1


async def test_sse_responses(fake_server: FakeMcpServer, http_client: httpx.AsyncClient) -> None:
    fake_server.sse = True
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    async with make_client(http_client) as t3:
        result = await t3.call(THREAD_LIST, ThreadListInput())
    assert result.total == 0


async def test_text_only_result(fake_server: FakeMcpServer, http_client: httpx.AsyncClient) -> None:
    """Tools without an outputSchema answer with a JSON text block only."""
    fake_server.tools["t3_pending_request"] = lambda _args: tool_result(
        {"requests": []}, structured=False
    )
    async with make_client(http_client) as t3:
        result = await t3.call(PENDING_REQUEST, PendingRequestInput(action="list"))
    assert result.root == {"requests": []}


async def test_tool_refusal_raises_with_code(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    fake_server.tools["t3_thread_read"] = lambda _args: tool_failure(
        "thread_not_found", "Thread nope is no longer available."
    )
    async with make_client(http_client) as t3:
        with pytest.raises(T3ToolError) as caught:
            await t3.call(THREAD_READ, ThreadReadInput(thread_id="nope"))
    assert caught.value.code == "thread_not_found"
    assert caught.value.tool == "t3_thread_read"


async def test_unknown_tool_is_a_jsonrpc_error(http_client: httpx.AsyncClient) -> None:
    async with make_client(http_client) as t3:
        with pytest.raises(T3McpError) as caught:
            await t3.call_raw("t3_does_not_exist")
    assert caught.value.code == -32602


async def test_result_not_matching_model(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result({"unexpected": True})
    async with make_client(http_client) as t3:
        with pytest.raises(T3ProtocolError, match="regenerate"):
            await t3.call(THREAD_LIST, ThreadListInput())


async def test_reopens_session_after_server_restart(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    async with make_client(http_client) as t3:
        await t3.call(THREAD_LIST, ThreadListInput())
        fake_server.drop_session_once = True
        await t3.call(THREAD_LIST, ThreadListInput())
    assert fake_server.sessions_opened == 2
    assert len(fake_server.calls) == 2


async def test_rejected_token(http_client: httpx.AsyncClient) -> None:
    async with T3Client(BASE_URL, "revoked", http_client=http_client) as t3:
        with pytest.raises(T3AuthError, match="no longer accepts"):
            await t3.call(THREAD_LIST, ThreadListInput())


async def test_mode_limit_header(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    async with make_client(http_client, mode_limit=("approval-required", "plan")) as t3:
        await t3.call(THREAD_LIST, ThreadListInput())
    assert all(
        request.headers["t3-mode-limit"] == "approval-required/plan"
        for request in fake_server.requests
    )


async def test_no_origin_header(fake_server: FakeMcpServer, http_client: httpx.AsyncClient) -> None:
    """`/mcp` refuses requests that look like they come from a browser."""
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    async with make_client(http_client) as t3:
        await t3.call(THREAD_LIST, ThreadListInput())
    assert all("origin" not in request.headers for request in fake_server.requests)


async def test_wait_for_thread_chunks_and_pins_run(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    answers = iter([
        {"threadId": "t1", "runId": "r1", "status": "running", "timedOut": True},
        {"threadId": "t1", "runId": "r1", "status": "running", "timedOut": True},
        {"threadId": "t1", "runId": "r1", "status": "completed", "timedOut": False},
    ])
    fake_server.tools["t3_thread_wait"] = lambda _args: tool_result(next(answers))
    async with make_client(http_client) as t3:
        result = await t3.wait_for_thread("t1")
    assert result.status == "completed"
    sent = [arguments for name, arguments in fake_server.calls if name == "t3_thread_wait"]
    assert len(sent) == 3
    # The first chunk lets the server pin the latest run; later chunks keep that run.
    assert "runId" not in sent[0]
    assert [arguments.get("runId") for arguments in sent[1:]] == ["r1", "r1"]
    assert all(arguments["timeoutMs"] <= 50_000 for arguments in sent)


async def test_wait_for_thread_overall_timeout(
    fake_server: FakeMcpServer, http_client: httpx.AsyncClient
) -> None:
    fake_server.tools["t3_thread_wait"] = lambda _args: tool_result({
        "threadId": "t1",
        "runId": "r1",
        "status": "running",
        "timedOut": True,
    })
    async with make_client(http_client) as t3:
        result = await t3.wait_for_thread("t1", run_id="r1", timeout=0)
    assert result.timed_out
    assert len(fake_server.calls) == 1
