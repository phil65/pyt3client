"""Effect RPC over a WebSocket, the protocol of T3 Code's native clients.

Frames are JSON objects tagged by `_tag` (upstream, vendored Effect:
`.repos/effect/packages/effect/src/rpc/RpcMessage.ts`):

- client -> server: `Request {id, tag, payload, headers}`; `Ack {requestId}` after
  each chunk of a stream (the server sends the next chunk once acked);
  `Interrupt {requestId, interruptors}` to end a request early; `Ping`.
- server -> client: `Chunk {requestId, values}` (items of a stream); `Exit
  {requestId, exit}`, where `exit` is `{_tag: "Success", value}` or `{_tag:
  "Failure", cause: [...]}`; `Defect {defect}` and `ClientProtocolError`, which
  end every request; `Pong`.

Methods (`tag`) and their payloads are in upstream `packages/contracts/src/rpc.ts`
and `orchestrationV2.ts` (`ORCHESTRATION_V2_WS_METHODS`).
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from pyt3client.errors import T3RpcError
from pyt3client.log import get_logger


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping

    from websockets.asyncio.client import ClientConnection


logger = get_logger(__name__)

KEEPALIVE_SECONDS = 15.0
"""Interval of RPC `Ping`s, which keep idle subscriptions open through proxies."""


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _CauseFail(_Frame):
    tag: Literal["Fail"] = Field(alias="_tag")
    error: JsonValue = None


class _CauseDie(_Frame):
    tag: Literal["Die"] = Field(alias="_tag")
    defect: JsonValue = None


class _CauseInterrupt(_Frame):
    tag: Literal["Interrupt"] = Field(alias="_tag")


type _Cause = Annotated[_CauseFail | _CauseDie | _CauseInterrupt, Field(discriminator="tag")]


class _ExitSuccess(_Frame):
    tag: Literal["Success"] = Field(alias="_tag")
    value: JsonValue = None


class _ExitFailure(_Frame):
    tag: Literal["Failure"] = Field(alias="_tag")
    cause: list[_Cause] = Field(default_factory=list)


class _Chunk(_Frame):
    tag: Literal["Chunk"] = Field(alias="_tag")
    request_id: str | int = Field(alias="requestId")
    values: list[JsonValue]


class _Exit(_Frame):
    tag: Literal["Exit"] = Field(alias="_tag")
    request_id: str | int = Field(alias="requestId")
    exit: Annotated[_ExitSuccess | _ExitFailure, Field(discriminator="tag")]


class _Defect(_Frame):
    tag: Literal["Defect"] = Field(alias="_tag")
    defect: JsonValue = None


class _ProtocolError(_Frame):
    tag: Literal["ClientProtocolError"] = Field(alias="_tag")
    error: JsonValue = None


class _Ping(_Frame):
    tag: Literal["Ping"] = Field(alias="_tag")


class _Pong(_Frame):
    tag: Literal["Pong"] = Field(alias="_tag")


_SERVER_FRAME: TypeAdapter[_Chunk | _Exit | _Defect | _ProtocolError | _Ping | _Pong] = TypeAdapter(
    Annotated[_Chunk | _Exit | _Defect | _ProtocolError | _Ping | _Pong, Field(discriminator="tag")]
)


@dataclass(frozen=True, slots=True)
class _Closed:
    """The socket ended; every open request fails with `error`."""

    error: T3RpcError


type _Delivery = _Chunk | _Exit | _Closed


def _failure_message(exit_failure: _ExitFailure) -> str:
    """A readable message for a failed request: the typed error's message if it has one."""
    for cause in exit_failure.cause:
        match cause:
            case _CauseFail(error={"message": str() as message, **_rest}):
                tag = _rest.get("_tag")
                return f"{tag}: {message}" if isinstance(tag, str) else message
            case _CauseFail(error=error):
                return json.dumps(error)[:500]
            case _CauseDie(defect=defect):
                return f"defect: {json.dumps(defect)[:500]}"
            case _CauseInterrupt():
                return "interrupted"
    return "failed"


class RpcSocket:
    """Requests and streams over one Effect RPC WebSocket; many may run at once.

    Use `RpcSocket.connect(url)`. Each request gets its own id; a reader task routes
    the server's frames to the request they answer.
    """

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._next_id = 0
        self._pending: dict[str, asyncio.Queue[_Delivery]] = {}
        self._closed: T3RpcError | None = None
        self._tasks: list[asyncio.Task[None]] = []

    @classmethod
    @asynccontextmanager
    async def connect(cls, url: str) -> AsyncGenerator[RpcSocket]:
        """Open the socket at `url` (with its `wsTicket`) and close it on exit."""
        from websockets.asyncio.client import connect

        async with connect(url, max_size=None) as connection:
            socket = cls(connection)
            socket._tasks = [
                asyncio.create_task(socket._read(), name="t3-rpc-read"),
                asyncio.create_task(socket._keepalive(), name="t3-rpc-keepalive"),
            ]
            try:
                yield socket
            finally:
                for task in socket._tasks:
                    task.cancel()
                for task in socket._tasks:
                    with suppress(asyncio.CancelledError):
                        await task

    async def call(self, tag: str, payload: Mapping[str, JsonValue]) -> JsonValue:
        """Send a request and return its result.

        Raises:
            T3RpcError: The request failed, or the socket closed before it ended.
        """
        request_id, queue = self._open()
        try:
            await self._request(request_id, tag, payload)
            while True:
                match await queue.get():
                    case _Exit(exit=_ExitSuccess(value=value)):
                        return value
                    case _Exit(exit=_ExitFailure() as failure):
                        raise T3RpcError(tag, _failure_message(failure), cause=_dump(failure))
                    case _Closed(error=error):
                        raise error
                    case _Chunk():
                        continue  # Not expected for a plain request
        finally:
            self._pending.pop(request_id, None)

    async def stream(self, tag: str, payload: Mapping[str, JsonValue]) -> AsyncIterator[JsonValue]:
        """Send a streaming request and yield its items until the server ends it.

        Leaving the loop early interrupts the request on the server.

        Raises:
            T3RpcError: The request failed, or the socket closed before it ended.
        """
        request_id, queue = self._open()
        ended = False
        try:
            await self._request(request_id, tag, payload)
            while True:
                match await queue.get():
                    case _Chunk(values=values):
                        await self._send({"_tag": "Ack", "requestId": request_id})
                        for value in values:
                            yield value
                    case _Exit(exit=_ExitSuccess()):
                        ended = True
                        return
                    case _Exit(exit=_ExitFailure() as failure):
                        ended = True
                        raise T3RpcError(tag, _failure_message(failure), cause=_dump(failure))
                    case _Closed(error=error):
                        ended = True
                        raise error
        finally:
            self._pending.pop(request_id, None)
            if not ended and self._closed is None:
                interrupt = {"_tag": "Interrupt", "requestId": request_id, "interruptors": []}
                with suppress(Exception):
                    await self._send(interrupt)

    def _open(self) -> tuple[str, asyncio.Queue[_Delivery]]:
        if self._closed is not None:
            raise self._closed
        self._next_id += 1
        request_id = str(self._next_id)
        queue: asyncio.Queue[_Delivery] = asyncio.Queue()
        self._pending[request_id] = queue
        return request_id, queue

    async def _request(self, request_id: str, tag: str, payload: Mapping[str, JsonValue]) -> None:
        frame: dict[str, JsonValue] = {
            "_tag": "Request",
            "id": request_id,
            "tag": tag,
            "payload": dict(payload),
            "headers": [],
        }
        await self._send(frame)

    async def _send(self, frame: Mapping[str, JsonValue]) -> None:
        await self._connection.send(json.dumps(frame))

    async def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        error = T3RpcError("socket", "the connection closed")
        try:
            async for raw in self._connection:
                try:
                    frame = _SERVER_FRAME.validate_json(raw)
                except ValidationError:
                    logger.debug("Ignoring an unknown RPC frame: %s", str(raw)[:200])
                    continue
                match frame:
                    case _Chunk(request_id=request_id) | _Exit(request_id=request_id):
                        if queue := self._pending.get(str(request_id)):
                            queue.put_nowait(frame)
                    case _Ping():
                        await self._send({"_tag": "Pong"})
                    case _Pong():
                        pass
                    case _Defect(defect=defect):
                        error = T3RpcError("socket", f"server defect: {json.dumps(defect)[:500]}")
                        break
                    case _ProtocolError(error=protocol_error):
                        message = f"protocol error: {json.dumps(protocol_error)[:500]}"
                        error = T3RpcError("socket", message)
                        break
        except ConnectionClosed as closed:
            error = T3RpcError("socket", f"the connection closed: {closed}")
        finally:
            self._closed = error
            for queue in self._pending.values():
                queue.put_nowait(_Closed(error))

    async def _keepalive(self) -> None:
        while self._closed is None:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            with suppress(Exception):
                await self._send({"_tag": "Ping"})


def _dump(failure: _ExitFailure) -> JsonValue:
    return failure.model_dump(mode="json", by_alias=True)
