"""The native surface: sign-in over HTTP, Effect RPC over a (fake) WebSocket server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx2
import pytest
from websockets.asyncio.server import serve

from pyt3client import T3AuthError, T3RpcError
from pyt3client.native import (
    RpcSocket,
    T3NativeClient,
    ThreadEvent,
    ThreadSnapshot,
    UnknownStreamItem,
    sign_in_native,
    websocket_url,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from websockets.asyncio.server import ServerConnection


type Script = Callable[[ServerConnection, Callable[[], Awaitable[dict[str, Any]]]], Awaitable[None]]


@dataclass
class FakeRpcServer:
    """A WebSocket server that runs `script` per connection and records what it got."""

    received: list[dict[str, Any]] = field(default_factory=list)
    path: str = ""


@asynccontextmanager
async def rpc_server(script: Script) -> AsyncGenerator[tuple[str, FakeRpcServer]]:
    state = FakeRpcServer()

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        state.path = connection.request.path

        async def receive() -> dict[str, Any]:
            frame = json.loads(await connection.recv())
            state.received.append(frame)
            return frame

        await script(connection, receive)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/ws", state


def frame(**fields: Any) -> str:
    return json.dumps(fields)


SNAPSHOT = {"kind": "snapshot", "snapshotSequence": 7, "projection": {"thread": {"id": "t1"}}}
ITEM_EVENT = {
    "kind": "event",
    "sequence": 8,
    "event": {
        "type": "turn-item.updated",
        "threadId": "t1",
        "payload": {
            "id": "turn-item:a",
            "threadId": "t1",
            "runId": "r1",
            "type": "assistant_message",
            "status": "running",
            "text": "Hel",
            "streaming": True,
            "ordinal": 3,
        },
    },
}


async def test_stream_acks_chunks_and_parses_items() -> None:
    async def script(connection: ServerConnection, receive: Any) -> None:
        request = await receive()
        rid = request["id"]
        await connection.send(frame(_tag="Chunk", requestId=rid, values=[SNAPSHOT]))
        assert (await receive()) == {"_tag": "Ack", "requestId": rid}
        await connection.send(
            frame(_tag="Chunk", requestId=rid, values=[ITEM_EVENT, {"kind": "x"}])
        )
        await receive()
        await connection.send(frame(_tag="Exit", requestId=rid, exit={"_tag": "Success"}))
        await connection.wait_closed()

    async with rpc_server(script) as (url, state), RpcSocket.connect(url) as socket:
        values = [
            v async for v in socket.stream("orchestration.subscribeThread", {"threadId": "t1"})
        ]
    request = state.received[0]
    assert (request["_tag"], request["tag"], request["payload"]) == (
        "Request",
        "orchestration.subscribeThread",
        {"threadId": "t1"},
    )
    assert values == [SNAPSHOT, ITEM_EVENT, {"kind": "x"}]


async def test_call_returns_value_or_raises_typed_failure() -> None:
    async def script(connection: ServerConnection, receive: Any) -> None:
        ok = await receive()
        await connection.send(
            frame(
                _tag="Exit", requestId=ok["id"], exit={"_tag": "Success", "value": {"sequence": 5}}
            )
        )
        bad = await receive()
        error = {"_tag": "OrchestrationV2DispatchCommandError", "message": "thread is busy"}
        exit_ = {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": error}]}
        await connection.send(frame(_tag="Exit", requestId=bad["id"], exit=exit_))
        await connection.wait_closed()

    async with rpc_server(script) as (url, _), RpcSocket.connect(url) as socket:
        assert await socket.call("orchestration.dispatchCommand", {"type": "x"}) == {"sequence": 5}
        with pytest.raises(T3RpcError, match="OrchestrationV2DispatchCommandError: thread is busy"):
            await socket.call("orchestration.dispatchCommand", {"type": "y"})


async def test_leaving_a_stream_early_interrupts_it() -> None:
    interrupted = asyncio.Event()

    async def script(connection: ServerConnection, receive: Any) -> None:
        request = await receive()
        await connection.send(frame(_tag="Chunk", requestId=request["id"], values=[SNAPSHOT]))
        while (got := await receive())["_tag"] != "Interrupt":
            pass
        assert got["requestId"] == request["id"]
        interrupted.set()
        await connection.wait_closed()

    async with rpc_server(script) as (url, _), RpcSocket.connect(url) as socket:
        stream = socket.stream("orchestration.subscribeThread", {"threadId": "t1"})
        async for _value in stream:
            break
        await stream.aclose()
        await asyncio.wait_for(interrupted.wait(), 5)


async def test_server_ping_is_answered_and_closing_fails_open_requests() -> None:
    async def script(connection: ServerConnection, receive: Any) -> None:
        await receive()  # The request, left unanswered
        await connection.send(frame(_tag="Ping"))
        assert (await receive()) == {"_tag": "Pong"}
        await connection.close()

    async with rpc_server(script) as (url, _), RpcSocket.connect(url) as socket:
        with pytest.raises(T3RpcError, match="closed"):
            await socket.call("orchestration.getThreadProjection", {"threadId": "t1"})
        with pytest.raises(T3RpcError):  # Later requests fail at once
            await socket.call("orchestration.getThreadProjection", {"threadId": "t1"})


def _auth_transport(
    seen: list[httpx2.Request], *, ticket_status: int = 200
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        match request.url.path:
            case "/oauth/token":
                form = parse_qs(request.content.decode())
                if form["subject_token"] != ["GOOD"]:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                token = {"access_token": "bearer-1", "expires_in": 3600, "scope": "a b"}
                return httpx2.Response(200, json=token)
            case "/api/auth/websocket-ticket":
                if ticket_status != httpx2.codes.OK:
                    return httpx2.Response(ticket_status, json={"error": "unauthorized"})
                return httpx2.Response(200, json={"ticket": "t/1+", "expiresAt": "x"})
            case _:
                return httpx2.Response(404)

    return httpx2.MockTransport(handle)


async def test_sign_in_exchanges_the_pairing_token() -> None:
    seen: list[httpx2.Request] = []
    async with httpx2.AsyncClient(transport=_auth_transport(seen)) as http:
        credentials = await sign_in_native(
            "http://t3.test/", " GOOD ", label="me", http_client=http
        )
        with pytest.raises(T3AuthError, match="refused"):
            await sign_in_native("http://t3.test", "USED", http_client=http)
    form = parse_qs(seen[0].content.decode())
    assert form["subject_token_type"] == ["urn:t3:params:oauth:token-type:environment-bootstrap"]
    assert form["client_label"] == ["me"]
    assert credentials.base_url == "http://t3.test"
    assert credentials.access_token.get_secret_value() == "bearer-1"
    assert credentials.scopes == ("a", "b")


async def test_websocket_url_carries_a_fresh_ticket() -> None:
    seen: list[httpx2.Request] = []
    async with httpx2.AsyncClient(transport=_auth_transport(seen)) as http:
        url = await websocket_url("https://t3.test", "bearer-1", http_client=http)
    assert url == "wss://t3.test/ws?orchestrationProtocol=2&wsTicket=t%2F1%2B"
    assert seen[0].headers["authorization"] == "Bearer bearer-1"
    async with httpx2.AsyncClient(transport=_auth_transport([], ticket_status=401)) as http:
        with pytest.raises(T3AuthError, match="native credential"):
            await websocket_url("http://t3.test", "mcp-token", http_client=http)


async def test_client_subscribes_and_answers_approvals() -> None:
    async def script(connection: ServerConnection, receive: Any) -> None:
        subscribe = await receive()
        sid = subscribe["id"]
        await connection.send(frame(_tag="Chunk", requestId=sid, values=[SNAPSHOT, ITEM_EVENT]))
        respond = await receive()
        while respond["_tag"] != "Request":
            respond = await receive()
        value = {"_tag": "Success", "value": {"sequence": 9}}
        await connection.send(frame(_tag="Exit", requestId=respond["id"], exit=value))
        await connection.wait_closed()

    async with rpc_server(script) as (url, state):
        host = url.removeprefix("ws://").removesuffix("/ws")
        async with (
            httpx2.AsyncClient(transport=_auth_transport([])) as http,
            T3NativeClient(f"http://{host}", "bearer-1", http_client=http) as t3,
        ):
            items = []
            async for item in t3.subscribe_thread("t1"):
                items.append(item)
                if len(items) == 2:
                    break
            sequence = await t3.respond_to_request("t1", "req-1", decision="accept")
    assert state.path == "/ws?orchestrationProtocol=2&wsTicket=t%2F1%2B"
    snapshot, event = items
    assert isinstance(snapshot, ThreadSnapshot)
    assert snapshot.snapshot_sequence == 7
    assert isinstance(event, ThreadEvent)
    item = event.event.turn_item()
    assert item is not None
    assert (item.item_id, item.text, item.streaming, item.model_extra) == (
        "turn-item:a",
        "Hel",
        True,
        {"ordinal": 3},
    )
    assert sequence == 9
    command = next(f for f in state.received if f.get("tag") == "orchestration.dispatchCommand")
    payload = command["payload"]
    assert payload.pop("commandId")
    assert payload == {
        "type": "runtime-request.respond",
        "threadId": "t1",
        "requestId": "req-1",
        "decision": "accept",
    }


def test_unknown_stream_kinds_are_kept() -> None:
    from pyt3client.native import parse_shell_stream_item, parse_stream_item

    item = parse_stream_item({"kind": "bounded-snapshot", "x": 1})
    assert isinstance(item, UnknownStreamItem)
    assert item.model_extra == {"x": 1}

    shell_item = parse_shell_stream_item({"kind": "future-shell-event", "y": 2})
    assert isinstance(shell_item, UnknownStreamItem)
    assert shell_item.model_extra == {"y": 2}


async def test_client_subscribes_shell_and_dispatches_thread_commands() -> None:
    from pyt3client.native import (
        ShellSnapshot,
        ShellSynchronized,
        ShellThreadUpdated,
    )

    shell_snapshot = {
        "kind": "snapshot",
        "snapshot": {
            "schemaVersion": 2,
            "snapshotSequence": 10,
            "projects": [{"id": "p1", "title": "acptui", "workspaceRoot": "/srv/acptui"}],
            "threads": [
                {
                    "id": "t1",
                    "projectId": "p1",
                    "title": "Fix bug",
                    "status": "running",
                    "latestVisibleMessage": {"text": "Working on it", "role": "assistant"},
                }
            ],
            "archivedThreads": [],
        },
    }
    shell_update = {
        "kind": "thread.updated",
        "sequence": 11,
        "location": "active",
        "thread": {
            "id": "t1",
            "projectId": "p1",
            "title": "Fix bug",
            "status": "waiting",
            "pendingRuntimeRequest": {"requestId": "req-9", "kind": "command", "prompt": "rm -rf"},
        },
    }

    async def script(connection: ServerConnection, receive: Any) -> None:
        sub = await receive()
        await connection.send(
            frame(
                _tag="Chunk",
                requestId=sub["id"],
                values=[shell_snapshot, shell_update, {"kind": "synchronized"}],
            )
        )
        for seq in (12, 13, 14):
            req = await receive()
            while req["_tag"] != "Request":
                req = await receive()
            await connection.send(
                frame(
                    _tag="Exit",
                    requestId=req["id"],
                    exit={"_tag": "Success", "value": {"sequence": seq}},
                )
            )
        await connection.wait_closed()

    async with rpc_server(script) as (url, state):
        host = url.removeprefix("ws://").removesuffix("/ws")
        async with (
            httpx2.AsyncClient(transport=_auth_transport([])) as http,
            T3NativeClient(f"http://{host}", "bearer-1", http_client=http) as t3,
        ):
            items = []
            async for item in t3.subscribe_shell():
                items.append(item)
                if len(items) == 3:
                    break
            seq_msg = await t3.send_message("t1", "Hello!")
            seq_stop = await t3.stop_thread("t1", reason="user")
            seq_visit = await t3.visit_thread("t1")

    snap, upd, synced = items
    assert isinstance(snap, ShellSnapshot)
    assert snap.snapshot.projects[0].title == "acptui"
    assert snap.snapshot.threads[0].status == "running"
    assert isinstance(upd, ShellThreadUpdated)
    assert upd.thread.status == "waiting"
    assert upd.thread.pending_runtime_request is not None
    assert upd.thread.pending_runtime_request.request_id == "req-9"
    assert isinstance(synced, ShellSynchronized)
    assert (seq_msg, seq_stop, seq_visit) == (12, 13, 14)

    dispatches = [
        f["payload"] for f in state.received if f.get("tag") == "orchestration.dispatchCommand"
    ]
    assert [d["type"] for d in dispatches] == ["message.dispatch", "thread.stop", "thread.visit"]
    assert dispatches[0]["text"] == "Hello!"
    assert dispatches[0]["dispatchMode"] == {"type": "start_immediately"}
