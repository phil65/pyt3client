"""The `pyt3` CLI against the in-memory fake `/mcp` server from conftest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr
import pytest
from typer.testing import CliRunner

from pyt3client import credentials as store
from pyt3client.cli import app
from pyt3client.cli.app import Runtime
from pyt3client.models.auth import T3Credentials

from .conftest import BASE_URL, TOKEN, FakeMcpServer, tool_failure, tool_result
from .test_auth import FakeOAuthServer


if TYPE_CHECKING:
    from pathlib import Path


NOW = "2026-01-01T00:00:00Z"


def credentials(*, expires_in: timedelta = timedelta(days=30)) -> T3Credentials:
    return T3Credentials(
        base_url=BASE_URL,
        environment_id="env-1",
        access_token=SecretStr(TOKEN),
        expires_at=datetime.now(UTC) + expires_in,
        scopes=("orchestration:read", "orchestration:operate"),
        access="auto",
        client_id="client-1",
    )


def project(project_id: str, root: str) -> dict[str, Any]:
    return {
        "id": project_id,
        "title": project_id,
        "workspaceRoot": root,
        "defaultModelSelection": None,
        "scripts": [],
        "createdAt": NOW,
        "updatedAt": NOW,
        "deletedAt": None,
    }


def thread_state(status: str = "completed") -> dict[str, Any]:
    return {
        "threadId": "t1",
        "projectId": "p1",
        "title": "ping",
        "createdBy": "agent",
        "creationSource": "mcp",
        "status": status,
        "latestRunId": "r1",
        "activeRunId": None,
        "providerInstanceId": "claudeAgent",
        "model": "claude-haiku-5-5",
        "runtimeMode": "auto",
        "interactionMode": "default",
        "linkedPullRequest": None,
        "titleRegeneration": None,
        "branch": None,
        "worktreePath": None,
        "parentThreadId": None,
        "relationshipToParent": None,
        "runCount": 1,
        "itemCount": 2,
        "pendingRequestCount": 0,
        "archived": False,
        "settled": False,
        "settledAt": None,
        "snoozed": False,
        "snoozedUntil": None,
        "createdAt": NOW,
        "updatedAt": NOW,
    }


def item(position: int, kind: str, text: str, run_id: str = "r1") -> dict[str, Any]:
    return {
        "position": position,
        "visibility": "local",
        "sourceThreadId": "t1",
        "itemId": f"i{position}",
        "runId": run_id,
        "messageId": None,
        "createdBy": "agent",
        "creationSource": "provider",
        "type": kind,
        "status": "completed",
        "title": None,
        "text": text,
        "textTruncated": False,
        "updatedAt": NOW,
    }


def thread_read(_arguments: dict[str, Any]) -> dict[str, Any]:
    return tool_result({
        "thread": thread_state(),
        "recentRuns": [],
        "items": [item(0, "user_message", "Say PONG"), item(1, "assistant_message", "PONG")],
        "nextPosition": None,
        "hasMore": False,
    })


def launched(_arguments: dict[str, Any]) -> dict[str, Any]:
    return tool_result({
        "threadId": "t1",
        "projectId": "p1",
        "modelSelection": {"instanceId": "claudeAgent", "model": "claude-haiku-5-5"},
        "runId": "r1",
        "status": "starting",
    })


def waited(status: str, *, timed_out: bool = False) -> Any:
    return lambda _arguments: tool_result({
        "threadId": "t1",
        "runId": "r1",
        "status": status,
        "timedOut": timed_out,
    })


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PYT3_ENV", raising=False)
    return tmp_path


@pytest.fixture
def logged_in(config_home: Path) -> Path:
    store.save({"home": credentials()})
    return config_home


@pytest.fixture
def server(fake_server: FakeMcpServer) -> FakeMcpServer:
    fake_server.tools["t3_project_list"] = lambda _args: tool_result({
        "projects": [project("p1", "/srv/app"), project("p2", "/srv/app/packages/lib")],
        "nextCursor": None,
    })
    fake_server.tools["t3_thread_launch"] = launched
    fake_server.tools["t3_thread_read"] = thread_read
    fake_server.tools["t3_thread_wait"] = waited("completed")
    return fake_server


def invoke(server: FakeMcpServer | FakeOAuthServer, *args: str, stdin: str | None = None) -> Any:
    runtime = Runtime(transport=httpx.MockTransport(server.handle))
    return CliRunner().invoke(app, list(args), obj=runtime, input=stdin)


def test_login_saves_owner_only_credentials(config_home: Path) -> None:
    result = invoke(FakeOAuthServer(), "login", "http://t3.test", "PLAINCODE", "--access", "auto")
    assert result.exit_code == 0, result.output
    assert "Signed in to test" in result.output  # alias from the descriptor label
    path = config_home / "pyt3client" / "credentials.json"
    if os.name == "posix":  # Windows has no owner-only mode bits; ACLs apply there
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = store.load()["test"]
    assert saved.access_token.get_secret_value() == "mcp-token"
    assert saved.environment_id == "env-1"


def test_envs_never_prints_tokens(logged_in: Path, server: FakeMcpServer) -> None:
    result = invoke(server, "envs", "--json")
    assert result.exit_code == 0
    listed = json.loads(result.output)
    assert listed["home"]["url"] == BASE_URL
    assert TOKEN not in result.output


def test_env_selection(
    config_home: Path, server: FakeMcpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save({"home": credentials(), "work": credentials()})
    ambiguous = invoke(server, "projects")
    assert ambiguous.exit_code == 1
    assert "home, work" in ambiguous.stderr
    assert invoke(server, "--env", "work", "projects").exit_code == 0
    monkeypatch.setenv("PYT3_ENV", "home")
    assert invoke(server, "projects").exit_code == 0
    assert invoke(server, "--env", "nope", "projects").exit_code == 1


def test_expired_credentials_refused_offline(config_home: Path, server: FakeMcpServer) -> None:
    store.save({"home": credentials(expires_in=timedelta(days=-1))})
    result = invoke(server, "projects")
    assert result.exit_code == 1
    assert "expired" in result.stderr
    assert "pyt3 login" in result.stderr
    assert server.requests == []


def test_json_output(logged_in: Path, server: FakeMcpServer) -> None:
    result = invoke(server, "projects", "--json")
    assert result.exit_code == 0
    assert [entry["workspaceRoot"] for entry in json.loads(result.output)] == [
        "/srv/app",
        "/srv/app/packages/lib",
    ]


def test_start_from_stdin_waits_and_prints_reply(logged_in: Path, server: FakeMcpServer) -> None:
    result = invoke(
        server,
        "start",
        "/srv/app/packages/lib/src",
        "-",
        "--provider",
        "claudeAgent",
        "--model",
        "claude-haiku-5-5",
        "--option",
        "effort=high",
        "--option",
        "fast=true",
        "--wait",
        stdin="Say PONG\nplease",
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "PONG"
    name, arguments = next(call for call in server.calls if call[0] == "t3_thread_launch")
    assert name == "t3_thread_launch"
    # The deepest project containing the path wins; title is the first line.
    assert arguments["projectId"] == "p2"
    assert arguments["title"] == "Say PONG"
    assert arguments["message"] == "Say PONG\nplease"
    assert arguments["modelSelection"] == {
        "instanceId": "claudeAgent",
        "model": "claude-haiku-5-5",
        "options": [{"id": "effort", "value": "high"}, {"id": "fast", "value": True}],
    }


@pytest.mark.parametrize(
    ("status", "timed_out", "exit_code"),
    [
        ("completed", False, 0),
        ("failed", False, 1),
        ("cancelled", False, 1),
        ("interrupted", False, 1),
        ("waiting", False, 2),
        ("running", True, 3),
    ],
)
def test_wait_exit_codes(
    logged_in: Path, server: FakeMcpServer, status: str, timed_out: bool, exit_code: int
) -> None:
    server.tools["t3_thread_wait"] = waited(status, timed_out=timed_out)
    result = invoke(server, "wait", "t1", "--run", "r1", "--timeout", "0")
    assert result.exit_code == exit_code, result.output
    started = invoke(server, "start", "p1", "hi", "--wait", "--timeout", "0", "--json")
    assert started.exit_code == exit_code
    assert json.loads(started.stdout)["wait"]["status"] == status


def test_tool_refusal(logged_in: Path, server: FakeMcpServer) -> None:
    server.tools["t3_thread_read"] = lambda _args: tool_failure(
        "thread_not_found", "Thread nope is no longer available."
    )
    result = invoke(server, "show", "nope")
    assert result.exit_code == 1
    assert result.stderr.strip() == "error: thread_not_found: Thread nope is no longer available."


def test_revoked_token_hints_login(config_home: Path, server: FakeMcpServer) -> None:
    revoked = credentials().model_copy(update={"access_token": SecretStr("revoked")})
    store.save({"home": revoked})
    result = invoke(server, "projects")
    assert result.exit_code == 1
    assert "pyt3 login" in result.stderr


def test_unknown_project(logged_in: Path, server: FakeMcpServer) -> None:
    result = invoke(server, "start", "/elsewhere", "hi")
    assert result.exit_code == 1
    assert "no project" in result.stderr


def test_show_reads_newest_items(logged_in: Path, server: FakeMcpServer) -> None:
    result = invoke(server, "show", "t1", "--limit", "5")
    assert result.exit_code == 0
    assert "[assistant] PONG" in result.stdout
    _, arguments = next(call for call in server.calls if call[0] == "t3_thread_read")
    assert arguments["limit"] == 5
    assert arguments["beforePosition"] > 0


def test_answer_collects_repeated_ids(logged_in: Path, server: FakeMcpServer) -> None:
    server.tools["t3_pending_request"] = lambda _args: tool_result(
        {"sequence": 7}, structured=False
    )
    result = invoke(server, "answer", "t1", "req-1", "color=red", "toppings=a", "toppings=b")
    assert result.exit_code == 0, result.output
    _, arguments = server.calls[-1]
    assert arguments == {
        "action": "respond",
        "threadId": "t1",
        "requestId": "req-1",
        "answers": {"color": "red", "toppings": ["a", "b"]},
    }


def test_pending_lists_questions(logged_in: Path, server: FakeMcpServer) -> None:
    def pending(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments["action"] == "list":
            return tool_result({"requestIds": ["req-1"]}, structured=False)
        question = {
            "id": "color",
            "header": "Color",
            "question": "Which color?",
            "options": [{"label": "Red", "description": "warm", "value": "red"}],
        }
        return tool_result({"requestId": "req-1", "questions": [question]}, structured=False)

    server.tools["t3_pending_request"] = pending
    result = invoke(server, "pending", "t1")
    assert result.exit_code == 0, result.output
    assert "request req-1" in result.stdout
    assert "color: Which color?" in result.stdout
    assert "- Red = red: warm" in result.stdout


def test_logout(logged_in: Path, server: FakeMcpServer) -> None:
    assert invoke(server, "logout").exit_code == 0
    assert store.load() == {}


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("c:\\src\\app\\lib", "win"),  # Windows paths compare case-insensitively
        ("C:\\src\\app", "win"),
        ("/srv/app/lib", "posix"),
        ("/src/app", None),  # A POSIX path is never inside a Windows folder
    ],
)
def test_project_paths_are_server_paths(
    logged_in: Path, server: FakeMcpServer, reference: str, expected: str | None
) -> None:
    server.tools["t3_project_list"] = lambda _args: tool_result({
        "projects": [project("win", "C:\\src\\app"), project("posix", "/srv/app")],
        "nextCursor": None,
    })
    result = invoke(server, "start", reference, "hi")
    launches = [arguments for name, arguments in server.calls if name == "t3_thread_launch"]
    if expected is None:
        assert result.exit_code == 1
        assert "no project" in result.stderr
        assert launches == []
    else:
        assert result.exit_code == 0, result.output
        assert launches[0]["projectId"] == expected
