"""T3 Code's native surface: the WebSocket RPC its web, desktop and mobile clients use.

Unlike `/mcp`, it streams a thread's events as they happen and can answer
approvals. It needs its own sign-in (see `sign_in_native`); install the `native`
extra (`pip install pyt3client[native]`) for the WebSocket dependency.

```python
from pyt3client.native import T3NativeClient, sign_in_native

credentials = await sign_in_native("http://127.0.0.1:3773", "PAIRING-TOKEN")
async with T3NativeClient.from_credentials(credentials) as t3:
    async for item in t3.subscribe_thread(thread_id):
        ...
```
"""

from __future__ import annotations

from pyt3client.native.client import T3NativeClient, sign_in_native, websocket_url
from pyt3client.native.models import (
    ApprovalDecision,
    ApprovalOption,
    DomainEvent,
    LatestVisibleMessageSummary,
    NativeItemRef,
    PendingRuntimeRequestSummary,
    ProjectShell,
    Run,
    RuntimeRequest,
    ShellProjectRemoved,
    ShellProjectUpdated,
    ShellSnapshot,
    ShellSnapshotData,
    ShellSnapshotRequired,
    ShellStreamItem,
    ShellSynchronized,
    ShellThreadRemoved,
    ShellThreadUpdated,
    ThreadEvent,
    ThreadShell,
    ThreadSnapshot,
    ThreadStreamItem,
    ThreadSynchronized,
    TurnItem,
    UnknownStreamItem,
    parse_shell_stream_item,
    parse_stream_item,
)
from pyt3client.native.rpc import RpcSocket

__all__ = [
    "ApprovalDecision",
    "ApprovalOption",
    "DomainEvent",
    "LatestVisibleMessageSummary",
    "NativeItemRef",
    "PendingRuntimeRequestSummary",
    "ProjectShell",
    "RpcSocket",
    "Run",
    "RuntimeRequest",
    "ShellProjectRemoved",
    "ShellProjectUpdated",
    "ShellSnapshot",
    "ShellSnapshotData",
    "ShellSnapshotRequired",
    "ShellStreamItem",
    "ShellSynchronized",
    "ShellThreadRemoved",
    "ShellThreadUpdated",
    "T3NativeClient",
    "ThreadEvent",
    "ThreadShell",
    "ThreadSnapshot",
    "ThreadStreamItem",
    "ThreadSynchronized",
    "TurnItem",
    "UnknownStreamItem",
    "parse_shell_stream_item",
    "parse_stream_item",
    "sign_in_native",
    "websocket_url",
]
