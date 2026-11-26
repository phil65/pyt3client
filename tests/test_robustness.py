"""Session setup failures, transport errors and concurrent credential writes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import sys
from typing import TYPE_CHECKING

import httpx2
import pytest

from pyt3client import T3Client, T3Error, fetch_descriptor
from pyt3client.errors import T3ConnectionError, T3HttpError
from pyt3client.models.tools import THREAD_LIST, ThreadListInput

from .conftest import BASE_URL, TOKEN, FakeMcpServer, tool_result


if TYPE_CHECKING:
    from pathlib import Path


THREAD_LIST_RESULT = {
    "projectId": None,
    "currentThreadId": None,
    "threads": [],
    "nextCursor": None,
    "total": 0,
}


async def test_failed_initialized_notification_reopens_session(
    fake_server: FakeMcpServer,
) -> None:
    """A session whose `notifications/initialized` failed is not reused."""
    fake_server.tools["t3_thread_list"] = lambda _args: tool_result(THREAD_LIST_RESULT)
    fail_next_notification = True

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal fail_next_notification
        body = json.loads(request.content) if request.content else {}
        if body.get("method") == "notifications/initialized" and fail_next_notification:
            fail_next_notification = False
            return httpx2.Response(503, text="busy")
        return fake_server.handle(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    async with T3Client(BASE_URL, TOKEN, http_client=http) as t3:
        with pytest.raises(T3HttpError):
            await t3.call(THREAD_LIST, ThreadListInput())
        result = await t3.call(THREAD_LIST, ThreadListInput())
    assert result.total == 0
    assert fake_server.sessions_opened == 2


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


def _time_out(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize("handler", [_refuse, _time_out])
async def test_mcp_transport_errors_are_wrapped(handler) -> None:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    async with T3Client(BASE_URL, TOKEN, http_client=http) as t3:
        with pytest.raises(T3ConnectionError) as caught:
            await t3.call(THREAD_LIST, ThreadListInput())
    assert isinstance(caught.value, T3Error)
    assert isinstance(caught.value.__cause__, httpx2.TransportError)
    assert BASE_URL in str(caught.value)


async def test_mcp_transport_error_after_session_is_wrapped(fake_server: FakeMcpServer) -> None:
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls > 2:  # initialize + notifications/initialized succeed
            raise httpx2.ReadTimeout("timed out", request=request)
        return fake_server.handle(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    async with T3Client(BASE_URL, TOKEN, http_client=http) as t3:
        with pytest.raises(T3ConnectionError):
            await t3.call(THREAD_LIST, ThreadListInput())


async def test_auth_transport_errors_are_wrapped() -> None:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse))
    with pytest.raises(T3ConnectionError):
        await fetch_descriptor(BASE_URL, http_client=http)


async def test_native_transport_errors_are_wrapped() -> None:
    from pyt3client.native.client import sign_in_native, websocket_url

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse))
    with pytest.raises(T3ConnectionError):
        await sign_in_native(BASE_URL, "pairing", http_client=http)
    with pytest.raises(T3ConnectionError):
        await websocket_url(BASE_URL, "token", http_client=http)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses concurrent renames onto one file"
)
def test_concurrent_credential_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writers don't share a temp file: no failed rename, no corrupt store."""
    from pyt3client import credentials
    from pyt3client.models.auth import T3Credentials

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def write(i: int) -> None:
        creds = T3Credentials(
            base_url=f"http://host{i}.test",
            environment_id=f"env-{i}",
            access_token="x" * (1 + i * 997 % 5000),
            expires_at=datetime.now(UTC),
            scopes=("orchestration:read",),
            access="read-only",
            client_id="c",
        )
        for _ in range(30):
            credentials.save({f"env{i}": creds})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(8)))
    saved = credentials.load()
    assert len(saved) == 1
    assert not list(credentials.credentials_path().parent.glob("*.tmp"))
