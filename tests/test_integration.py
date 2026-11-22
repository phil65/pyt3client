r"""End-to-end against a real T3 Code server.

`test_agent_turn_and_follow_up` runs real agent turns on the server's
`claudeAgent` provider (skipped when it is not set up). Start the server
without `ANTHROPIC_API_KEY` / `CLAUDECODE` / `CLAUDE_*` in its environment, or
Claude runs on that API key instead of the subscription login (see CLAUDE.md).

Never point this at your live install: it creates and deletes a project. Run an
isolated server and mint a code for it:

    env -u ANTHROPIC_API_KEY -u CLAUDECODE \
        t3 serve --base-dir /tmp/t3home --port 39731 --host 127.0.0.1 --no-browser
    t3 auth pairing create --base-dir /tmp/t3home --json   # use "credential"

    T3_URL=http://127.0.0.1:39731 T3_PAIRING_CODE=... uv run pytest -m integration

The pairing code is single use; the module signs in once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

import pytest

from pyt3client import T3Client, T3Credentials, T3ToolError, fetch_descriptor, pair
from pyt3client.models.tools import (
    ORCHESTRATOR_CAPABILITIES,
    PROJECT_CREATE,
    PROJECT_DELETE,
    THREAD_LAUNCH,
    THREAD_LIST,
    THREAD_READ,
    THREAD_SEND,
    THREAD_UPDATE,
    ModelSelection,
    ProjectCreateInput,
    ProjectDeleteInput,
    ThreadLaunchInput,
    ThreadListInput,
    ThreadReadInput,
    ThreadSendInput,
    ThreadUpdateInput,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pyt3client.models.tools import OrchestratorCapabilitiesOutput


URL = os.environ.get("T3_URL")
PAIRING_CODE = os.environ.get("T3_PAIRING_CODE")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and PAIRING_CODE), reason="set T3_URL and T3_PAIRING_CODE"),
]


@pytest.fixture(scope="module")
async def credentials() -> T3Credentials:
    assert URL
    assert PAIRING_CODE
    # Project tools need a full-access client (else `capability_denied`).
    return await pair(URL, PAIRING_CODE, access="full-access", client_name="pyt3client-tests")


@pytest.fixture
async def t3(credentials: T3Credentials) -> AsyncIterator[T3Client]:
    async with T3Client.from_credentials(credentials) as client:
        yield client


async def test_descriptor_matches_credentials(credentials: T3Credentials) -> None:
    descriptor = await fetch_descriptor(credentials.base_url)
    assert descriptor.environment_id == credentials.environment_id
    assert credentials.access == "full-access"
    assert "orchestration:operate" in credentials.scopes


async def test_server_offers_every_snapshot_tool(t3: T3Client) -> None:
    """Drift check: a tool missing on the server means the snapshot is newer than it."""
    snapshot = json.loads(
        (Path(__file__).parent.parent / "schemas" / "mcp_tools.json").read_text(encoding="utf-8")
    )
    live = {tool.name for tool in await t3.list_tools()}
    missing = {tool["name"] for tool in snapshot["tools"]} - live
    assert not missing, f"server lacks tools from the snapshot: {sorted(missing)}"


async def test_capabilities(t3: T3Client) -> None:
    capabilities = await t3.call(ORCHESTRATOR_CAPABILITIES)
    assert capabilities.runtime_mode == "full-access"
    assert all(provider.provider_instance_id for provider in capabilities.providers)


async def test_unknown_thread_is_a_typed_refusal(t3: T3Client) -> None:
    with pytest.raises(T3ToolError) as caught:
        await t3.call(THREAD_READ, ThreadReadInput(thread_id=str(uuid.uuid4())))
    assert caught.value.code == "thread_not_found"


async def test_idle_thread_lifecycle(t3: T3Client, tmp_path: Path) -> None:
    """Create a project, an idle thread (no prompt, so no agent turn), rename, clean up."""
    capabilities = await t3.call(ORCHESTRATOR_CAPABILITIES)
    # A new project has no default model, so a launch must name one.
    provider = next((p for p in capabilities.providers if p.can_run_child_task and p.models), None)
    if provider is None:
        pytest.skip("no provider with models is set up on this server")
    selection = ModelSelection(
        instance_id=provider.provider_instance_id, model=provider.models[0].id
    )
    project = await t3.call(
        PROJECT_CREATE,
        ProjectCreateInput(title="pyt3client integration", workspace_root=str(tmp_path)),
    )
    try:
        launched = await t3.call(
            THREAD_LAUNCH,
            ThreadLaunchInput(
                project_id=project.id, title="idle thread", model_selection=selection
            ),
        )
        assert launched.model_selection.instance_id == provider.provider_instance_id
        assert launched.project_id == project.id
        assert launched.run_id is None

        listed = await t3.call(THREAD_LIST, ThreadListInput(project_id=project.id))
        assert [thread.thread_id for thread in listed.threads] == [launched.thread_id]

        renamed = await t3.call(
            THREAD_UPDATE,
            ThreadUpdateInput(thread_id=launched.thread_id, action="rename", title="renamed"),
        )
        assert renamed.title == "renamed"

        read = await t3.call(THREAD_READ, ThreadReadInput(thread_id=launched.thread_id))
        assert read.thread.title == "renamed"
        assert read.thread.creation_source == "mcp"
        assert read.thread.created_by == "agent"

        waited = await t3.wait_for_thread(launched.thread_id, timeout=5)
        assert waited.status == "idle"
        assert not waited.timed_out
    finally:
        await t3.call(PROJECT_DELETE, ProjectDeleteInput(project_id=project.id, force=True))


def claude_selection(capabilities: OrchestratorCapabilitiesOutput) -> ModelSelection | None:
    """The `claudeAgent` provider's cheapest advertised model, preferring Haiku."""
    provider = next(
        (p for p in capabilities.providers if p.provider_instance_id == "claudeAgent"), None
    )
    if provider is None or not provider.models or not provider.can_run_child_task:
        return None
    model = next((m for m in provider.models if "haiku" in m.id), provider.models[-1])
    return ModelSelection(instance_id=provider.provider_instance_id, model=model.id)


async def assistant_texts(t3: T3Client, thread_id: str) -> list[str]:
    read = await t3.call(THREAD_READ, ThreadReadInput(thread_id=thread_id))
    return [
        item.text
        for item in read.items
        if item.type == "assistant_message" and item.text is not None
    ]


async def test_agent_turn_and_follow_up(t3: T3Client, tmp_path: Path) -> None:
    """Prompt a real agent, wait for its reply, then continue the conversation."""
    selection = claude_selection(await t3.call(ORCHESTRATOR_CAPABILITIES))
    if selection is None:
        pytest.skip("claudeAgent is not set up on this server")
    project = await t3.call(
        PROJECT_CREATE,
        ProjectCreateInput(title="pyt3client agent turn", workspace_root=str(tmp_path)),
    )
    try:
        launched = await t3.call(
            THREAD_LAUNCH,
            ThreadLaunchInput(
                project_id=project.id,
                title="ping",
                message="Reply with exactly the word PONG and nothing else. Do not use tools.",
                model_selection=selection,
                # Needs no tools; a tool request would wait for approval, not run.
                runtime_mode="approval-required",
            ),
        )
        assert launched.run_id is not None
        first = await t3.wait_for_thread(launched.thread_id, run_id=launched.run_id, timeout=180)
        assert first.status == "completed", first
        assert any("PONG" in text for text in await assistant_texts(t3, launched.thread_id))

        sent = await t3.call(
            THREAD_SEND,
            ThreadSendInput(
                thread_id=launched.thread_id,
                message="Now reply with exactly the word PING and nothing else.",
            ),
        )
        second = await t3.wait_for_thread(launched.thread_id, run_id=sent.run_id, timeout=180)
        assert second.status == "completed", second
        texts = await assistant_texts(t3, launched.thread_id)
        assert "PING" in texts[-1]
    finally:
        await t3.call(PROJECT_DELETE, ProjectDeleteInput(project_id=project.id, force=True))
