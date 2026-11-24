"""What `orchestration.subscribeThread` streams, as far as clients of pyt3client need it.

Hand-written, unlike the MCP models: the native contracts are Effect Schemas with
no JSON Schema endpoint. Fields not listed here are kept (`extra="allow"`), so
`model_extra` holds what newer servers add.

Upstream (`packages/contracts/src/orchestrationV2.ts`):

- `OrchestrationV2ThreadStreamItem`: `snapshot` (the thread's projection), then
  `event`s (`OrchestrationV2DomainEvent`, numbered by `sequence`), `synchronized`.
- `OrchestrationV2TurnItem` (the payload of `turn-item.updated`): one row of the
  timeline. Text items grow in place while `streaming` is true.
- `OrchestrationV2RuntimeRequest` (`runtime-request.updated`): an approval or a
  question the provider waits on; answer it with `runtime-request.respond`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from pyt3client.models._base import T3Model


type ApprovalDecision = Literal["accept", "acceptForSession", "acceptAlways", "decline", "cancel"]
"""`ProviderApprovalDecision` (upstream `packages/contracts/src/providerPolicy.ts`)."""


class _Open(T3Model):
    model_config = ConfigDict(extra="allow")


class ApprovalOption(_Open):
    """A choice an approval request offers (`ProviderApprovalOption`)."""

    decision: ApprovalDecision
    label: str
    warning: str | None = None


class NativeItemRef(_Open):
    """The provider's own id of what an item stands for (`OrchestrationV2ProviderRef`).

    An approval request shares it with the tool call it is about.
    """

    driver: str
    native_id: str


class TurnItem(_Open):
    """A timeline row (`OrchestrationV2TurnItem`): a message, reasoning, tool work, a request.

    `type` is e.g. `user_message`, `assistant_message`, `reasoning`, `command_execution`,
    `file_change`, `dynamic_tool`, `approval_request`, `user_input_request`,
    `checkpoint`. `status` is one of `idle`, `pending`, `running`, `waiting`,
    `completed`, `interrupted`, `failed`, `cancelled`, `rolled_back`.
    """

    id: str
    thread_id: str
    run_id: str | None = None
    type: str
    status: str
    title: str | None = None
    text: str | None = None
    """Text of message and reasoning items; complete once `streaming` is false."""
    streaming: bool | None = None
    native_item_ref: NativeItemRef | None = None
    request_id: str | None = None
    """For `approval_request` and `user_input_request`: the runtime request to answer."""
    request_kind: str | None = None
    """For `approval_request`: `command`, `file-read`, `file-change`, `mcp-elicitation`
    or `permission` (`ProviderRequestKind`)."""
    prompt: str | None = None
    """For `approval_request`: what is to be approved, such as the command."""
    reason: str | None = None
    options: list[ApprovalOption] | None = None
    """For `approval_request`: the decisions on offer, when the provider names them."""

    @property
    def item_id(self) -> str:
        """The item's id, as `/mcp`'s thread reads name it (`itemId`)."""
        return self.id


class Run(_Open):
    """A run of a thread (`run.created` / `run.updated`)."""

    id: str
    thread_id: str
    status: str
    """`preparing`, `queued`, `starting`, `running`, `waiting`, `completed`,
    `interrupted`, `failed`, `cancelled` or `rolled_back`."""


class RuntimeRequest(_Open):
    """An approval or question a provider waits on (`OrchestrationV2RuntimeRequest`)."""

    id: str
    kind: str
    """A `ProviderRequestKind`, or `dynamic_tool_call`, `user_input`, `auth_refresh`."""
    status: Literal["pending", "resolved", "expired", "cancelled"]
    decision: ApprovalDecision | None = None


class DomainEvent(_Open):
    """A thread event (`OrchestrationV2DomainEvent`); `payload` depends on `type`."""

    type: str
    thread_id: str | None = None
    run_id: str | None = None
    payload: dict[str, JsonValue]

    def turn_item(self) -> TurnItem | None:
        """The item of a `turn-item.updated` event."""
        if self.type != "turn-item.updated":
            return None
        return TurnItem.model_validate(self.payload)

    def run(self) -> Run | None:
        """The run of a `run.created` or `run.updated` event."""
        if self.type not in ("run.created", "run.updated"):
            return None
        return Run.model_validate(self.payload)

    def runtime_request(self) -> RuntimeRequest | None:
        """The request of a `runtime-request.updated` event."""
        if self.type != "runtime-request.updated":
            return None
        return RuntimeRequest.model_validate(self.payload)


class ThreadSnapshot(_Open):
    """The thread as it is when the subscription starts; events continue after it."""

    kind: Literal["snapshot"]
    snapshot_sequence: int
    projection: dict[str, JsonValue]
    """`OrchestrationV2ThreadProjection`: thread, runs, turn items, requests, ..."""


class ThreadEvent(_Open):
    """One event of the thread, in `sequence` order."""

    kind: Literal["event"]
    sequence: int
    event: DomainEvent


class ThreadSynchronized(_Open):
    """The subscription caught up with the thread."""

    kind: Literal["synchronized"]


class UnknownStreamItem(_Open):
    """A stream item of a kind this client does not know (newer servers)."""

    kind: str


class ProjectShell(_Open):
    """A project row in the workspace shell (`OrchestrationProjectShell`)."""

    id: str
    title: str
    workspace_root: str
    default_model_selection: dict[str, JsonValue] | None = None
    pinned_at: str | None = None
    archived_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PendingRuntimeRequestSummary(_Open):
    """Summary of a pending approval or input request on a thread shell."""

    id: str | None = None
    request_id: str | None = None
    kind: str | None = None
    prompt: str | None = None


class LatestVisibleMessageSummary(_Open):
    """Preview of the latest visible message on a thread shell."""

    text: str | None = None
    role: str | None = None
    type: str | None = None
    created_at: str | None = None


class ThreadShell(_Open):
    """A thread row in the workspace shell (`OrchestrationV2ThreadShell`)."""

    id: str
    project_id: str
    title: str
    status: str = "idle"
    provider_instance_id: str | None = None
    model_selection: dict[str, JsonValue] | None = None
    runtime_mode: str | None = None
    interaction_mode: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    active_run_id: str | None = None
    latest_run_id: str | None = None
    activity_run_status: str | None = None
    last_error: str | None = None
    pending_runtime_request: PendingRuntimeRequestSummary | None = None
    latest_visible_message: LatestVisibleMessageSummary | None = None
    pinned_at: str | None = None
    archived_at: str | None = None
    settled_at: str | None = None
    last_visited_at: str | None = None
    updated_at: str | None = None
    created_at: str | None = None
    deleted_at: str | None = None


class ShellSnapshotData(_Open):
    """Initial projects and threads payload (`OrchestrationV2ShellSnapshot`)."""

    schema_version: int = 1
    snapshot_sequence: int = 0
    projects: list[ProjectShell] = Field(default_factory=list)
    threads: list[ThreadShell] = Field(default_factory=list)
    archived_threads: list[ThreadShell] = Field(default_factory=list)


class ShellSnapshot(_Open):
    """Initial workspace shell snapshot."""

    kind: Literal["snapshot"]
    snapshot: ShellSnapshotData


class ShellProjectUpdated(_Open):
    """A project was created or updated."""

    kind: Literal["project.updated"]
    sequence: int
    project: ProjectShell


class ShellProjectRemoved(_Open):
    """A project was removed."""

    kind: Literal["project.removed"]
    sequence: int
    project_id: str


class ShellThreadUpdated(_Open):
    """A thread row was created or updated."""

    kind: Literal["thread.updated"]
    sequence: int
    location: Literal["active", "archive"]
    thread: ThreadShell


class ShellThreadRemoved(_Open):
    """A thread row was removed from `location`."""

    kind: Literal["thread.removed"]
    sequence: int
    location: Literal["active", "archive"]
    thread_id: str


class ShellSynchronized(_Open):
    """The shell subscription caught up."""

    kind: Literal["synchronized"]


class ShellSnapshotRequired(_Open):
    """Resume gap too large; re-subscribe without `after_sequence`."""

    kind: Literal["snapshot-required"]


type ThreadStreamItem = ThreadSnapshot | ThreadEvent | ThreadSynchronized | UnknownStreamItem
type ShellStreamItem = (
    ShellSnapshot
    | ShellProjectUpdated
    | ShellProjectRemoved
    | ShellThreadUpdated
    | ShellThreadRemoved
    | ShellSynchronized
    | ShellSnapshotRequired
    | UnknownStreamItem
)


def parse_stream_item(value: JsonValue) -> ThreadStreamItem:
    """A `subscribeThread` stream value as a model.

    Raises:
        pydantic.ValidationError: A known kind with an unexpected shape.
    """
    kind = value.get("kind") if isinstance(value, dict) else None
    match kind:
        case "snapshot":
            return ThreadSnapshot.model_validate(value)
        case "event":
            return ThreadEvent.model_validate(value)
        case "synchronized":
            return ThreadSynchronized.model_validate(value)
        case _:
            return UnknownStreamItem.model_validate(value)


_SHELL_ITEM_MODELS: dict[str, type[ShellStreamItem]] = {
    "snapshot": ShellSnapshot,
    "project.updated": ShellProjectUpdated,
    "project.removed": ShellProjectRemoved,
    "thread.updated": ShellThreadUpdated,
    "thread.removed": ShellThreadRemoved,
    "synchronized": ShellSynchronized,
    "snapshot-required": ShellSnapshotRequired,
}


def parse_shell_stream_item(value: JsonValue) -> ShellStreamItem:
    """A `subscribeShell` stream value as a model.

    Raises:
        pydantic.ValidationError: A known kind with an unexpected shape.
    """
    kind = value.get("kind") if isinstance(value, dict) else None
    model = _SHELL_ITEM_MODELS.get(kind) if isinstance(kind, str) else None
    if model is not None:
        return model.model_validate(value)
    return UnknownStreamItem.model_validate(value)
