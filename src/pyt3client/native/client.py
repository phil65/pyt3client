"""Sign-in and client for T3 Code's native surface (WebSocket RPC).

Sign-in (upstream `packages/client-runtime/src/authorization/remote.ts`,
`bootstrapRemoteBearerSession`): a one-time pairing token is exchanged at
`/oauth/token` (`grant_type` token exchange, `subject_token_type`
`urn:t3:params:oauth:token-type:environment-bootstrap`) for a bearer. Each socket
then needs a short-lived ticket from `POST /api/auth/websocket-ticket` and opens
`/ws?orchestrationProtocol=2&wsTicket=...`. Pairing tokens come from
`t3 auth pairing create` or Settings -> Sharing & access in T3 Code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self
from urllib.parse import quote
from uuid import uuid4

import httpx2
from pydantic import BaseModel, JsonValue, SecretStr

from pyt3client.errors import T3AuthError, T3HttpError, T3ProtocolError, transport_errors
from pyt3client.models.auth import T3NativeCredentials
from pyt3client.native.models import parse_shell_stream_item, parse_stream_item
from pyt3client.native.rpc import RpcSocket


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType

    from pyt3client.native.models import ApprovalDecision, ShellStreamItem, ThreadStreamItem


ORCHESTRATION_PROTOCOL = 2
"""`ORCHESTRATION_PROTOCOL_VERSION` (upstream `packages/contracts/src/environment.ts`)."""

_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_BOOTSTRAP_TOKEN = "urn:t3:params:oauth:token-type:environment-bootstrap"
_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"


class _TokenResponse(BaseModel):
    access_token: str
    expires_in: int
    scope: str = ""


class _Ticket(BaseModel):
    ticket: str


class _Dispatched(BaseModel):
    sequence: int


def _origin(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/mcp")


async def sign_in_native(
    base_url: str,
    pairing_token: str,
    *,
    label: str = "pyt3client",
    scopes: Sequence[str] | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> T3NativeCredentials:
    """Exchange a one-time pairing token for a native bearer.

    Args:
        base_url: Environment origin, such as `http://127.0.0.1:3773`.
        pairing_token: The pairing token's credential (it can be used once).
        label: Client name shown in T3 Code under Settings -> Sharing & access.
        scopes: Scopes to ask for, at most those of the pairing token; all of them
            when omitted.
        http_client: Client to reuse; a short-lived one is created otherwise.

    Raises:
        T3AuthError: The pairing token was refused (used, expired or revoked).
        T3ConnectionError: The environment could not be reached.
    """
    origin = _origin(base_url)
    form = {
        "grant_type": _TOKEN_EXCHANGE,
        "subject_token": pairing_token.strip(),
        "subject_token_type": _BOOTSTRAP_TOKEN,
        "requested_token_type": _ACCESS_TOKEN,
        "client_label": label,
        "client_device_type": "bot",
    }
    if scopes:
        form["scope"] = " ".join(scopes)
    with transport_errors("native sign-in"):
        async with _client(http_client) as client:
            response = await client.post(f"{origin}/oauth/token", data=form)
    if response.status_code in (httpx2.codes.BAD_REQUEST, httpx2.codes.UNAUTHORIZED):
        msg = f"the pairing token was refused: {response.text[:300]}"
        raise T3AuthError(msg)
    if response.status_code != httpx2.codes.OK:
        msg = "native sign-in failed"
        raise T3HttpError(msg, status=response.status_code, body=response.text)
    token = _TokenResponse.model_validate_json(response.content)
    return T3NativeCredentials(
        base_url=origin,
        access_token=SecretStr(token.access_token),
        expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in),
        scopes=tuple(token.scope.split()),
    )


async def websocket_url(
    base_url: str, access_token: str, *, http_client: httpx2.AsyncClient | None = None
) -> str:
    """The URL of a new socket, with a fresh one-time ticket.

    Raises:
        T3AuthError: The bearer was refused (revoked, expired, or an MCP token).
        T3ConnectionError: The environment could not be reached.
    """
    origin = _origin(base_url)
    headers = {
        "authorization": f"Bearer {access_token}",
        "x-t3-orchestration-protocol": str(ORCHESTRATION_PROTOCOL),
    }
    with transport_errors("websocket ticket"):
        async with _client(http_client) as client:
            response = await client.post(f"{origin}/api/auth/websocket-ticket", headers=headers)
    if response.status_code == httpx2.codes.UNAUTHORIZED:
        msg = (
            "the environment refused the native credential: it may be revoked or expired,"
            " or be an MCP token (the native surface needs its own sign-in)"
        )
        raise T3AuthError(msg)
    if response.status_code != httpx2.codes.OK:
        msg = "websocket ticket"
        raise T3HttpError(msg, status=response.status_code, body=response.text)
    ticket = _Ticket.model_validate_json(response.content).ticket
    scheme = "wss" if origin.startswith("https:") else "ws"
    host = origin.split("://", 1)[1]
    return (
        f"{scheme}://{host}/ws?orchestrationProtocol={ORCHESTRATION_PROTOCOL}"
        f"&wsTicket={quote(ticket, safe='')}"
    )


class T3NativeClient:
    """One socket to an environment's native RPC; subscriptions and commands share it.

    ```python
    async with T3NativeClient.from_credentials(credentials) as t3:
        async for item in t3.subscribe_thread(thread_id):
            ...
    ```
    """

    def __init__(
        self, base_url: str, access_token: str, *, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        self.base_url = _origin(base_url)
        self._access_token = access_token
        self._http_client = http_client
        self._socket: RpcSocket | None = None
        self._connection: AbstractAsyncContextManager[RpcSocket] | None = None

    @classmethod
    def from_credentials(
        cls, credentials: T3NativeCredentials, *, http_client: httpx2.AsyncClient | None = None
    ) -> Self:
        return cls(
            credentials.base_url,
            credentials.access_token.get_secret_value(),
            http_client=http_client,
        )

    async def __aenter__(self) -> Self:
        url = await websocket_url(self.base_url, self._access_token, http_client=self._http_client)
        connection = RpcSocket.connect(url)
        self._socket = await connection.__aenter__()
        self._connection = connection
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            await self._connection.__aexit__(exc_type, exc_val, exc_tb)
        self._connection = self._socket = None

    @property
    def socket(self) -> RpcSocket:
        """The open socket, for methods this client has no wrapper for."""
        if self._socket is None:
            msg = "T3NativeClient is not connected; use `async with`"
            raise RuntimeError(msg)
        return self._socket

    async def subscribe_shell(
        self, *, after_sequence: int | None = None
    ) -> AsyncIterator[ShellStreamItem]:
        """Workspace shell snapshot (projects and threads), then live updates (`subscribeShell`)."""
        payload: dict[str, JsonValue] = {}
        if after_sequence is not None:
            payload["afterSequence"] = after_sequence
        async for value in self.socket.stream("orchestration.subscribeShell", payload):
            yield parse_shell_stream_item(value)

    async def subscribe_thread(
        self, thread_id: str, *, after_sequence: int | None = None
    ) -> AsyncIterator[ThreadStreamItem]:
        """The thread's snapshot, then its events as they happen (`subscribeThread`).

        Args:
            thread_id: The thread to follow.
            after_sequence: Resume after this event (a snapshot's `snapshot_sequence`
                or an event's `sequence`) instead of starting with a snapshot.
        """
        payload: dict[str, JsonValue] = {"threadId": thread_id}
        if after_sequence is not None:
            payload["afterSequence"] = after_sequence
        async for value in self.socket.stream("orchestration.subscribeThread", payload):
            yield parse_stream_item(value)

    async def get_server_config(self) -> JsonValue:
        """Fetch server configuration (providers, models, capabilities) via `server.getConfig`."""
        return await self.socket.call("server.getConfig", {})

    async def dispatch(self, command: Mapping[str, JsonValue]) -> int:
        """Run an orchestration command (`dispatchCommand`); returns its event sequence.

        `command` is an `OrchestrationV2Command` (upstream `orchestrationV2.ts`), such
        as `{"type": "thread.stop", "threadId": ...}`; a `commandId` is added
        when missing.

        Raises:
            T3RpcError: The command was refused.
        """
        payload = {"commandId": str(uuid4()), **command}
        result = await self.socket.call("orchestration.dispatchCommand", payload)
        try:
            return _Dispatched.model_validate(result).sequence
        except ValueError as error:
            msg = f"dispatchCommand: unexpected result {result!r}"
            raise T3ProtocolError(msg) from error

    async def send_message(
        self,
        thread_id: str,
        text: str,
        *,
        dispatch_mode: Mapping[str, JsonValue] | None = None,
        model_selection: Mapping[str, JsonValue] | None = None,
    ) -> int:
        """Dispatch a user message to a thread (`message.dispatch`)."""
        mode: dict[str, JsonValue] = (
            dict(dispatch_mode) if dispatch_mode is not None else {"type": "start_immediately"}
        )
        command: dict[str, JsonValue] = {
            "type": "message.dispatch",
            "threadId": thread_id,
            "messageId": str(uuid4()),
            "text": text,
            "attachments": [],
            "dispatchMode": mode,
        }
        if model_selection is not None:
            command["modelSelection"] = dict(model_selection)
        return await self.dispatch(command)

    async def stop_thread(self, thread_id: str, *, reason: str | None = None) -> int:
        """Stop the active run on a thread (`thread.stop`)."""
        command: dict[str, JsonValue] = {"type": "thread.stop", "threadId": thread_id}
        if reason is not None:
            command["reason"] = reason
        return await self.dispatch(command)

    async def visit_thread(self, thread_id: str, *, opened: bool = True) -> int:
        """Record that the user viewed a thread (`thread.visit`)."""
        return await self.dispatch({
            "type": "thread.visit",
            "threadId": thread_id,
            "visitedAt": datetime.now(UTC).isoformat(),
            "opened": opened,
        })

    async def create_thread(
        self,
        project_id: str,
        title: str,
        *,
        model_selection: Mapping[str, JsonValue],
        runtime_mode: str = "approval-required",
        interaction_mode: str = "default",
        thread_id: str | None = None,
        branch: str | None = None,
        worktree_path: str | None = None,
    ) -> tuple[str, int]:
        """Create a new thread in `project_id` (`thread.create`).

        Returns `(thread_id, sequence)`.
        """
        tid = thread_id or str(uuid4())
        seq = await self.dispatch({
            "type": "thread.create",
            "threadId": tid,
            "projectId": project_id,
            "title": title,
            "modelSelection": dict(model_selection),
            "runtimeMode": runtime_mode,
            "interactionMode": interaction_mode,
            "branch": branch,
            "worktreePath": worktree_path,
        })
        return tid, seq

    async def respond_to_request(
        self,
        thread_id: str,
        request_id: str,
        *,
        decision: ApprovalDecision | None = None,
        answers: Mapping[str, JsonValue] | None = None,
        feedback: str | None = None,
    ) -> int:
        """Answer an approval (`decision`) or a question (`answers`) a thread waits on.

        Args:
            thread_id: The thread.
            request_id: The runtime request (`TurnItem.request_id`).
            decision: For approvals.
            answers: For questions: question id -> answer.
            feedback: Text sent along with the decision, where the provider takes it.
        """
        command: dict[str, JsonValue] = {
            "type": "runtime-request.respond",
            "threadId": thread_id,
            "requestId": request_id,
        }
        if decision is not None:
            command["decision"] = decision
        if answers is not None:
            command["answers"] = dict(answers)
        if feedback is not None:
            command["feedback"] = feedback
        return await self.dispatch(command)


def _client(client: httpx2.AsyncClient | None) -> AbstractAsyncContextManager[httpx2.AsyncClient]:
    from contextlib import nullcontext

    return nullcontext(client) if client is not None else httpx2.AsyncClient(timeout=30)
