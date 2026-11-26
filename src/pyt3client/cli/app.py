"""The `pyt3` command: drive a (possibly remote) T3 Code environment from a shell.

T3 Code's own `t3` CLI only works on the machine that runs the server (it
reads the local data directory). `pyt3` signs in to any environment's `/mcp`
once (`pyt3 login`) and then works from anywhere that can reach it.

Exit codes follow upstream `t3 thread ... --wait`: 0 when the run completed,
1 when it failed, was cancelled or interrupted (or on any error), 2 when the
agent waits for an answer or approval, and 3 when `--timeout` ran out first.
"""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import PurePath, PurePosixPath, PureWindowsPath
import sys
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn

import httpx2
from pydantic import BaseModel, JsonValue
import typer

from pyt3client import credentials as store
from pyt3client.auth import fetch_descriptor, pair
from pyt3client.client import T3Client
from pyt3client.errors import (
    T3AuthError,
    T3Error,
    T3HttpError,
    T3McpError,
    T3ProtocolError,
    T3ToolError,
)
from pyt3client.models.tools import (
    ORCHESTRATOR_CAPABILITIES,
    PENDING_REQUEST,
    PROJECT_LIST,
    THREAD_INTERRUPT,
    THREAD_LAUNCH,
    THREAD_LIST,
    THREAD_READ,
    THREAD_SEARCH,
    THREAD_SEND,
    ModelSelection,
    PendingRequestInput,
    ProjectListInput,
    ProviderOptionSelection,
    ThreadInterruptInput,
    ThreadLaunchInput,
    ThreadListInput,
    ThreadReadInput,
    ThreadSearchInput,
    ThreadSendInput,
    WorkspaceStrategy,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pyt3client.models.auth import (
        InteractionMode,
        McpAccess,
        RuntimeMode,
        T3Credentials,
        T3NativeCredentials,
    )
    from pyt3client.models.tools import Item, Project, ThreadReadOutput, ThreadWaitOutput


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WAITING = 2
EXIT_TIMEOUT = 3

LOGIN_HINT = "Run `pyt3 login <url> <pairing-code>` again."
# `t3_thread_read` pages from the oldest item for outside clients; reading
# before the largest position T3 accepts (a JS safe integer) gives the newest page.
_NEWEST = 9_007_199_254_740_991

type ThreadStatus = Literal[
    "idle",
    "preparing",
    "queued",
    "starting",
    "running",
    "waiting",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
    "rolled_back",
]


@dataclass
class Runtime:
    """Per-invocation state; tests pass one as `obj=` to inject an httpx2 transport."""

    transport: httpx2.AsyncBaseTransport | None = None
    env: str | None = None

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self.transport, timeout=60)


app = typer.Typer(
    name="pyt3",
    help="Drive T3 Code environments (local or remote) over their MCP endpoint.",
    no_args_is_help=True,
    add_completion=False,
)

JsonFlag = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
TimeoutOption = Annotated[
    float | None, typer.Option("--timeout", help="Stop waiting after this many seconds (exit 3).")
]


@app.callback()
def main(
    ctx: typer.Context,
    env: Annotated[
        str | None,
        typer.Option("--env", envvar="PYT3_ENV", help="Saved environment alias (see `pyt3 envs`)."),
    ] = None,
) -> None:
    """Drive T3 Code environments (local or remote) over their MCP endpoint."""
    runtime = ctx.obj if isinstance(ctx.obj, Runtime) else Runtime()
    runtime.env = env
    ctx.obj = runtime


def _runtime(ctx: typer.Context) -> Runtime:
    root = ctx.find_root()
    if isinstance(root.obj, Runtime):
        return root.obj
    runtime = Runtime()
    root.obj = runtime
    return runtime


# --- output helpers --------------------------------------------------------------


def _echo_json(value: BaseModel | JsonValue) -> None:
    data = value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _dump(model: BaseModel) -> JsonValue:
    return model.model_dump(mode="json", by_alias=True)


def _fail(message: str, *, hint: str | None = None, code: int = EXIT_FAILED) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(code)


def _read_message(message: str) -> str:
    """The message argument, or stdin when it is `-`."""
    text = sys.stdin.read() if message == "-" else message
    if not text.strip():
        _fail("the message is empty")
    return text


def _wait_exit_code(result: ThreadWaitOutput) -> int:
    if result.timed_out:
        return EXIT_TIMEOUT
    return _status_exit_code(result.status)


def _status_exit_code(status: ThreadStatus) -> int:
    match status:
        case "completed" | "idle":
            return EXIT_OK
        case "waiting":
            return EXIT_WAITING
        case "failed" | "cancelled" | "interrupted" | "rolled_back":
            return EXIT_FAILED
        case "preparing" | "queued" | "starting" | "running":
            # A wait only returns these when it timed out, which is handled above.
            return EXIT_TIMEOUT


def _item_line(item: Item) -> str:
    role = item.type.removesuffix("_message")
    text = item.text if item.text is not None else (item.title or "")
    suffix = " …" if item.text_truncated else ""
    return f"[{role}] {text.strip()}{suffix}"


def _option_value(value: str | bool) -> str:
    """An option value as `--option` takes it (`true` / `false` for switches)."""
    match value:
        case bool():
            return "true" if value else "false"
        case str():
            return value


def _parse_option(raw: str) -> ProviderOptionSelection:
    key, sep, value = raw.partition("=")
    if not sep or not key.strip():
        msg = f"--option takes id=value, got {raw!r}"
        raise typer.BadParameter(msg)
    match value.strip().lower():
        case "true":
            return ProviderOptionSelection(id=key.strip(), value=True)
        case "false":
            return ProviderOptionSelection(id=key.strip(), value=False)
        case _:
            return ProviderOptionSelection(id=key.strip(), value=value.strip())


# --- running against an environment ---------------------------------------------


def _credentials(runtime: Runtime) -> tuple[str, T3Credentials]:
    try:
        alias, credentials = store.select(store.load(), runtime.env)
    except store.StoreError as error:
        _fail(str(error))
    if store.is_expired(credentials):
        _fail(
            f"the sign-in for {alias!r} expired on {credentials.expires_at:%Y-%m-%d}",
            hint=LOGIN_HINT,
        )
    return alias, credentials


def _run(ctx: typer.Context, operation: Callable[[T3Client], Awaitable[int]]) -> NoReturn:
    """Run `operation` against the selected environment and exit with its code."""
    runtime = _runtime(ctx)
    alias, credentials = _credentials(runtime)

    async def go() -> int:
        async with (
            runtime.http_client() as http,
            T3Client.from_credentials(credentials, http_client=http) as t3,
        ):
            return await operation(t3)

    try:
        code = asyncio.run(go())
    except T3ToolError as error:
        hint = LOGIN_HINT if error.code == "capability_denied" else None
        _fail(f"{error.code}: {error.message}", hint=hint)
    except T3AuthError as error:
        _fail(f"auth: {error}", hint=LOGIN_HINT)
    except T3McpError as error:
        _fail(f"request: {error.message}")
    except T3HttpError as error:
        _fail(f"http {error.status}: {error}")
    except T3ProtocolError as error:
        _fail(f"protocol: {error}")
    except T3Error as error:
        _fail(str(error))
    except httpx2.HTTPError as error:
        _fail(f"cannot reach {alias!r} at {credentials.base_url}: {error}")
    raise typer.Exit(code)


async def _all_projects(t3: T3Client) -> list[Project]:
    projects: list[Project] = []
    cursor: int | None = None
    while True:
        params = ProjectListInput(limit=100) if cursor is None else ProjectListInput(cursor=cursor)
        page = await t3.call(PROJECT_LIST, params)
        projects.extend(page.projects)
        if page.next_cursor is None:
            return projects
        cursor = page.next_cursor.root


def _server_path(text: str) -> PurePath | None:
    r"""`text` as an absolute path of the flavor it is written in, or None if relative.

    `/srv/app` is POSIX; `C:\src\app` and `\\host\share` are Windows paths.
    """
    if text.startswith("/"):
        return PurePosixPath(text)
    windows = PureWindowsPath(text)
    return windows if windows.is_absolute() else None


async def _resolve_project(t3: T3Client, reference: str) -> str:
    """A project id, or the project whose folder contains the path `reference`.

    Absolute paths are paths on the server's filesystem and are compared as
    given, POSIX or Windows by their form, whatever this machine runs.
    Relative paths resolve against this shell's directory, which only makes
    sense when the environment runs on this machine.
    """
    projects = await _all_projects(t3)
    if any(project.id == reference for project in projects):
        return reference
    path = _server_path(reference)
    if path is None:
        local = os.path.abspath(reference)  # noqa: PTH100 - must not follow symlinks
        path = _server_path(local) or PurePosixPath(local)
    containing = [
        project
        for project in projects
        if (root := _server_path(project.workspace_root)) is not None
        and type(root) is type(path)
        and (path == root or root in path.parents)
    ]
    if not containing:
        msg = f"no project with id {reference!r} or containing {path}"
        raise T3ToolError(THREAD_LAUNCH.name, "invalid_request", msg)
    return max(containing, key=lambda project: len(project.workspace_root)).id


async def _read_newest(
    t3: T3Client,
    thread_id: str,
    *,
    limit: int,
    activity: bool = False,
) -> ThreadReadOutput:
    view: Literal["messages", "activity"] = "activity" if activity else "messages"
    return await t3.call(
        THREAD_READ,
        ThreadReadInput(thread_id=thread_id, view=view, before_position=_NEWEST, limit=limit),
    )


async def _reply(t3: T3Client, thread_id: str, run_id: str | None) -> str | None:
    """The run's last assistant message, falling back to the thread's last one."""
    read = await _read_newest(t3, thread_id, limit=20)
    replies = [item for item in read.items if item.type == "assistant_message" and item.text]
    own = [item for item in replies if run_id is not None and item.run_id == run_id]
    chosen = (own or replies)[-1:] or None
    return chosen[0].text if chosen else None


async def _wait_and_report(
    t3: T3Client,
    thread_id: str,
    run_id: str | None,
    *,
    timeout: float | None,
    as_json: bool,
    launched: BaseModel,
) -> int:
    result = await t3.wait_for_thread(thread_id, run_id=run_id, timeout=timeout)
    reply = await _reply(
        t3, thread_id, (result.result_run.run_id if result.result_run else None) or result.run_id
    )
    code = _wait_exit_code(result)
    if as_json:
        _echo_json({"thread": _dump(launched), "wait": _dump(result), "reply": reply})
        return code
    if reply:
        typer.echo(reply.strip())
    if code != EXIT_OK:
        state = "timed out" if result.timed_out else result.status
        typer.echo(f"{thread_id}: {state}", err=True)
    return code


# --- environments -----------------------------------------------------------------


@app.command()
def login(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Environment address, e.g. https://host:3773.")],
    pairing_code: Annotated[str, typer.Argument(help="Code from `t3 auth pairing create`.")],
    access: Annotated[
        Literal["read-only", "approval-required", "auto-accept-edits", "auto", "full-access"],
        typer.Option(help="What this CLI may do. App approval codes (AUTO-…) set their own."),
    ] = "auto",
    name: Annotated[
        str | None, typer.Option("--name", help="Alias to save it as (default: its label).")
    ] = None,
    client_name: Annotated[
        str, typer.Option(help="Label the environment shows for this sign-in.")
    ] = "pyt3",
    as_json: JsonFlag = False,
) -> None:
    """Sign in with a one-time pairing code and save the token."""
    runtime = _runtime(ctx)
    level: McpAccess = access

    async def go() -> tuple[str, T3Credentials]:
        async with runtime.http_client() as http:
            credentials = await pair(
                url, pairing_code, access=level, client_name=client_name, http_client=http
            )
            descriptor = await fetch_descriptor(credentials.base_url, http_client=http)
        return name or store.default_alias(descriptor.label, credentials.base_url), credentials

    try:
        alias, credentials = asyncio.run(go())
        path = store.update(alias, credentials)
    except T3AuthError as error:
        _fail(f"auth: {error}")
    except (T3Error, httpx2.HTTPError) as error:
        _fail(f"cannot sign in at {url}: {error}")
    except store.StoreError as error:
        _fail(str(error))
    if as_json:
        _echo_json({"alias": alias, "path": str(path)} | _env_summary(credentials))
        return
    typer.echo(
        f"Signed in to {alias} ({credentials.base_url}) as {credentials.access},"
        f" valid until {credentials.expires_at:%Y-%m-%d}."
    )


@app.command("login-native")
def login_native(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Environment address, e.g. https://host:3773.")],
    pairing_token: Annotated[
        str, typer.Argument(help="Token from `t3 auth pairing create` (not an MCP code).")
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Alias to save it as (default: the MCP sign-in of this URL, or its label).",
        ),
    ] = None,
    client_name: Annotated[
        str, typer.Option(help="Label the environment shows for this sign-in.")
    ] = "pyt3",
    as_json: JsonFlag = False,
) -> None:
    """Sign in to the native WebSocket RPC (live streaming, approvals) and save the token.

    The native surface rejects MCP tokens, so this is a second sign-in, kept under the
    same alias as the environment's MCP one.
    """
    from pyt3client.native import sign_in_native

    runtime = _runtime(ctx)

    async def go() -> tuple[str, T3NativeCredentials]:
        async with runtime.http_client() as http:
            credentials = await sign_in_native(
                url, pairing_token, label=client_name, http_client=http
            )
            if name is not None:
                return name, credentials
            for alias, saved in store.load().items():
                if saved.base_url == credentials.base_url:
                    return alias, credentials
            descriptor = await fetch_descriptor(credentials.base_url, http_client=http)
        return store.default_alias(descriptor.label, credentials.base_url), credentials

    try:
        alias, credentials = asyncio.run(go())
        path = store.update_native(alias, credentials)
    except T3AuthError as error:
        _fail(f"auth: {error}")
    except (T3Error, httpx2.HTTPError) as error:
        _fail(f"cannot sign in at {url}: {error}")
    except store.StoreError as error:
        _fail(str(error))
    if as_json:
        summary: dict[str, JsonValue] = {
            "alias": alias,
            "path": str(path),
            "url": credentials.base_url,
            "scopes": list(credentials.scopes),
            "expiresAt": credentials.expires_at.isoformat(),
        }
        _echo_json(summary)
        return
    typer.echo(
        f"Signed in natively to {alias} ({credentials.base_url}),"
        f" valid until {credentials.expires_at:%Y-%m-%d}."
    )


@app.command()
def logout(
    ctx: typer.Context,
    alias: Annotated[
        str | None, typer.Argument(help="Environment to forget (default: --env or the only one).")
    ] = None,
) -> None:
    """Forget a saved sign-in. Revoke it in T3 Code under Settings -> Sharing & access."""
    runtime = _runtime(ctx)
    try:
        chosen, _ = store.select(store.load(), alias or runtime.env)
        store.update(chosen, None)
        store.update_native(chosen, None)
    except store.StoreError as error:
        _fail(str(error))
    typer.echo(f"Forgot {chosen}. The token stays valid until revoked in T3 Code.")


def _env_summary(credentials: T3Credentials) -> dict[str, JsonValue]:
    return {
        "url": credentials.base_url,
        "environmentId": credentials.environment_id,
        "access": credentials.access,
        "expiresAt": credentials.expires_at.isoformat(),
        "expired": store.is_expired(credentials),
    }


@app.command()
def envs(as_json: JsonFlag = False) -> None:
    """List saved environments (never prints tokens)."""
    try:
        environments = store.load()
        native = store.load_native()
    except store.StoreError as error:
        _fail(str(error))
    if as_json:
        listed: dict[str, JsonValue] = {}
        for alias, creds in environments.items():
            summary = _env_summary(creds)
            if (signed_in := native.get(alias)) is not None:
                summary["native"] = {
                    "expiresAt": signed_in.expires_at.isoformat(),
                    "expired": store.is_expired(signed_in),
                }
            listed[alias] = summary
        _echo_json(listed)
        return
    if not environments:
        typer.echo("No environment saved. Run `pyt3 login <url> <pairing-code>`.")
        return
    for alias, credentials in sorted(environments.items()):
        state = (
            "EXPIRED"
            if store.is_expired(credentials)
            else f"until {credentials.expires_at:%Y-%m-%d}"
        )
        native_state = "native" if alias in native else "-"
        typer.echo(
            f"{alias}\t{credentials.base_url}\t{credentials.access}\t{state}"
            f"\t{credentials.environment_id}\t{native_state}"
        )


# --- reading ----------------------------------------------------------------------------


@app.command()
def models(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """List providers and their models."""

    async def go(t3: T3Client) -> int:
        capabilities = await t3.call(ORCHESTRATOR_CAPABILITIES)
        if as_json:
            _echo_json(capabilities)
            return EXIT_OK
        for provider in capabilities.providers:
            state = "can run" if provider.can_run_child_task else "unavailable"
            label = f" ({provider.display_name})" if provider.display_name else ""
            typer.echo(f"{provider.provider_instance_id}{label}  [{state}]")
            for constraint in provider.constraints:
                typer.echo(f"    ! {constraint}")
            for model in provider.models:
                options = ", ".join(
                    f"{option.id}={'|'.join(_option_value(value) for value in option.values)}"
                    for option in model.options or []
                )
                typer.echo(f"  {model.id}" + (f"  ({options})" if options else ""))
            if provider.omitted_models:
                typer.echo(f"  … {provider.omitted_models.root} more")
        return EXIT_OK

    _run(ctx, go)


@app.command()
def projects(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """List projects."""

    async def go(t3: T3Client) -> int:
        found = await _all_projects(t3)
        if as_json:
            _echo_json([_dump(project) for project in found])
            return EXIT_OK
        for project in found:
            typer.echo(f"{project.id}\t{project.title}\t{project.workspace_root}")
        return EXIT_OK

    _run(ctx, go)


@app.command()
def threads(
    ctx: typer.Context,
    project: Annotated[
        str | None, typer.Option(help="Project id or folder (default: all projects).")
    ] = None,
    all_threads: Annotated[bool, typer.Option("--all", help="Include subagent threads.")] = False,
    status: Annotated[
        list[ThreadStatus] | None, typer.Option(help="Only threads in this status (repeatable).")
    ] = None,
    search: Annotated[
        str | None, typer.Option(help="Search titles, branches, PRs and messages instead.")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 50,
    as_json: JsonFlag = False,
) -> None:
    """List threads, newest first, or search them."""

    async def go(t3: T3Client) -> int:
        project_id = await _resolve_project(t3, project) if project else None
        if search is not None:
            found = await t3.call(
                THREAD_SEARCH,
                ThreadSearchInput(query=search, limit=min(limit, 50))
                if project_id is None
                else ThreadSearchInput(query=search, limit=min(limit, 50), project_id=project_id),
            )
            if as_json:
                _echo_json(found)
                return EXIT_OK
            for match in found.matches:
                snippet = f"  {match.snippet.root}" if match.snippet else ""
                typer.echo(f"{match.thread_id}\t{match.title}\t[{match.source}]{snippet}")
            return EXIT_OK
        params = ThreadListInput(limit=limit)
        if project_id is None:
            params.all_projects = True
        else:
            params.project_id = project_id
        if all_threads:
            params.include_subagents = True
        if status:
            params.statuses = list(status)
        listed = await t3.call(THREAD_LIST, params)
        if as_json:
            _echo_json(listed)
            return EXIT_OK
        for thread in listed.threads:
            typer.echo(
                f"{thread.thread_id}\t{thread.status}\t{thread.title}"
                f"\t{thread.provider_instance_id}/{thread.model}"
            )
        if listed.total > len(listed.threads):
            typer.echo(f"… {listed.total - len(listed.threads)} more (raise --limit)", err=True)
        return EXIT_OK

    _run(ctx, go)


@app.command()
def show(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    activity: Annotated[
        bool, typer.Option(help="Include tool calls, reasoning and runtime requests.")
    ] = False,
    limit: Annotated[int, typer.Option(min=1, max=100, help="Latest items to show.")] = 10,
    as_json: JsonFlag = False,
) -> None:
    """Show a thread's state and its latest messages."""

    async def go(t3: T3Client) -> int:
        read = await _read_newest(t3, thread, limit=limit, activity=activity)
        if as_json:
            _echo_json(read)
            return EXIT_OK
        state = read.thread
        typer.echo(f"{state.title}  [{state.status}]  {state.provider_instance_id}/{state.model}")
        typer.echo(f"{state.thread_id}  {state.runtime_mode}/{state.interaction_mode}")
        if state.link:
            typer.echo(state.link)
        typer.echo("")
        for item in read.items:
            typer.echo(_item_line(item))
        return EXIT_OK

    _run(ctx, go)


# --- acting -------------------------------------------------------------------------


@app.command()
def start(
    ctx: typer.Context,
    project: Annotated[str, typer.Argument(help="Project id, or a folder inside a project.")],
    message: Annotated[str, typer.Argument(help="First prompt; `-` reads stdin.")],
    title: Annotated[
        str | None, typer.Option(help="Thread title (default: the message's first line).")
    ] = None,
    provider: Annotated[
        str | None, typer.Option(help="Provider instance id (see `pyt3 models`).")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Model slug of that provider.")] = None,
    option: Annotated[
        list[str] | None, typer.Option(help="Model option id=value (repeatable).")
    ] = None,
    mode: Annotated[
        Literal["approval-required", "auto-accept-edits", "auto", "full-access"] | None,
        typer.Option(help="Runtime mode (default: the project's)."),
    ] = None,
    plan: Annotated[bool, typer.Option(help="Start in plan mode.")] = False,
    worktree: Annotated[
        str | None, typer.Option(help="Run in a new worktree from this branch, tag or commit.")
    ] = None,
    branch: Annotated[str | None, typer.Option(help="Branch name for the new worktree.")] = None,
    wait: Annotated[bool, typer.Option(help="Wait for the run and print the reply.")] = False,
    timeout: TimeoutOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Start a thread and print its id (or, with --wait, the agent's reply)."""
    if (provider is None) != (model is None):
        _fail("pass --provider and --model together")
    if option and provider is None:
        _fail("--option needs --provider and --model")
    if branch is not None and worktree is None:
        _fail("--branch needs --worktree")
    text = _read_message(message)
    runtime_mode: RuntimeMode | None = mode
    interaction: InteractionMode | None = "plan" if plan else None

    async def go(t3: T3Client) -> int:
        params = ThreadLaunchInput(
            project_id=await _resolve_project(t3, project),
            title=title or text.strip().splitlines()[0][:120],
            message=text,
        )
        if provider is not None and model is not None:
            selection = ModelSelection(instance_id=provider, model=model)
            if option:
                selection.options = [_parse_option(raw) for raw in option]
            params.model_selection = selection
        if runtime_mode is not None:
            params.runtime_mode = runtime_mode
        if interaction is not None:
            params.interaction_mode = interaction
        if worktree is not None:
            strategy = WorkspaceStrategy(type="worktree", base_ref=worktree)
            if branch is not None:
                strategy.branch = branch
            params.workspace_strategy = strategy
        launched = await t3.call(THREAD_LAUNCH, params)
        if wait:
            return await _wait_and_report(
                t3,
                launched.thread_id,
                launched.run_id,
                timeout=timeout,
                as_json=as_json,
                launched=launched,
            )
        if as_json:
            _echo_json(launched)
        else:
            typer.echo(launched.thread_id)
        return EXIT_OK

    _run(ctx, go)


@app.command()
def send(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message; `-` reads stdin.")],
    mode: Annotated[
        Literal["auto", "queue", "steer", "restart"] | None,
        typer.Option(
            help="auto (default): start an idle thread, steer or queue a busy one;"
            " queue: run after active work; steer: into the running turn;"
            " restart: interrupt and restart with this message."
        ),
    ] = None,
    wait: Annotated[bool, typer.Option(help="Wait for the run and print the reply.")] = False,
    timeout: TimeoutOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Send a message to a thread."""
    text = _read_message(message)

    async def go(t3: T3Client) -> int:
        params = ThreadSendInput(thread_id=thread, message=text)
        if mode is not None:
            params.mode = mode
        sent = await t3.call(THREAD_SEND, params)
        if wait:
            return await _wait_and_report(
                t3, thread, sent.run_id, timeout=timeout, as_json=as_json, launched=sent
            )
        if as_json:
            _echo_json(sent)
        else:
            typer.echo(f"{sent.run_id}\t{sent.status}")
        return EXIT_OK

    _run(ctx, go)


@app.command(name="wait")
def wait_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    run: Annotated[str | None, typer.Option(help="Run to wait for (default: the latest).")] = None,
    timeout: TimeoutOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Wait until a thread's run ends; the exit code tells how it ended."""

    async def go(t3: T3Client) -> int:
        result = await t3.wait_for_thread(thread, run_id=run, timeout=timeout)
        if as_json:
            _echo_json(result)
        else:
            state = "timed out" if result.timed_out else result.status
            typer.echo(f"{result.run_id or '-'}\t{state}")
        return _wait_exit_code(result)

    _run(ctx, go)


@app.command()
def interrupt(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    run: Annotated[
        str | None, typer.Option(help="Run to interrupt (default: the newest running).")
    ] = None,
    reason: Annotated[str | None, typer.Option(help="Why; shown in the thread.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Interrupt a thread's running turn."""

    async def go(t3: T3Client) -> int:
        params = ThreadInterruptInput(thread_id=thread)
        if run is not None:
            params.run_id = run
        if reason is not None:
            params.reason = reason
        result = await t3.call(THREAD_INTERRUPT, params)
        if as_json:
            _echo_json(result)
        else:
            typer.echo(f"{result.run_id or '-'}\t{result.status}")
        return EXIT_OK

    _run(ctx, go)


@app.command()
def pending(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    as_json: JsonFlag = False,
) -> None:
    """List the questions a thread's agent waits on (approvals are not listed)."""

    async def go(t3: T3Client) -> int:
        listed = await t3.call(
            PENDING_REQUEST, PendingRequestInput(action="list", thread_id=thread)
        )
        request_ids = _string_list(listed.root, "requestIds")
        # The session is safe for concurrent calls; gather keeps the listed order.
        reads = await asyncio.gather(
            *(
                t3.call(
                    PENDING_REQUEST,
                    PendingRequestInput(action="read", thread_id=thread, request_id=request_id),
                )
                for request_id in request_ids
            )
        )
        requests: list[JsonValue] = [read.root for read in reads]
        if as_json:
            _echo_json(requests)
            return EXIT_OK
        if not requests:
            typer.echo("No pending questions.")
        for request in requests:
            _print_request(request)
        return EXIT_OK

    _run(ctx, go)


@app.command()
def answer(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    request_id: Annotated[str, typer.Argument(help="Request id from `pyt3 pending`.")],
    answers: Annotated[
        list[str],
        typer.Argument(
            help="question-id=answer, where the answer is an option value or label, or free"
            " text. Repeat a question id to select several options."
        ),
    ],
    as_json: JsonFlag = False,
) -> None:
    """Answer an agent's pending question. Cannot approve permission requests."""
    parsed: dict[str, JsonValue] = {}
    for raw in answers:
        question_id, sep, value = raw.partition("=")
        if not sep or not question_id.strip():
            _fail(f"answers take question-id=answer, got {raw!r}")
        key = question_id.strip()
        match parsed.get(key):
            case None:
                parsed[key] = value
            case list() as values:
                values.append(value)
            case previous:
                parsed[key] = [previous, value]

    async def go(t3: T3Client) -> int:
        result = await t3.call(
            PENDING_REQUEST,
            PendingRequestInput(
                action="respond", thread_id=thread, request_id=request_id, answers=parsed
            ),
        )
        if as_json:
            _echo_json(result)
        else:
            typer.echo("Answered.")
        return EXIT_OK

    _run(ctx, go)


def _string_list(value: JsonValue, key: str) -> list[str]:
    """`value[key]` as a list of strings; pending-request results have no schema."""
    if isinstance(value, dict):
        items = value.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, str)]
    return []


def _print_request(request: JsonValue) -> None:
    """Print a `t3_pending_request` read result: request id plus its questions.

    Questions follow upstream `UserInputQuestion`
    (packages/contracts/src/providerRuntime.ts): id, header, question, options
    of {label, description, value?}, multiSelect, allowCustomAnswer.
    """
    if not isinstance(request, dict):
        typer.echo(json.dumps(request))
        return
    typer.echo(f"request {request.get('requestId')}")
    questions = request.get("questions")
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        multi = " (several allowed)" if question.get("multiSelect") is True else ""
        typer.echo(f"  {question.get('id')}: {question.get('question')}{multi}")
        options = question.get("options")
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict):
                value = option.get("value")
                shown = f"{option.get('label')}" + (f" = {value}" if isinstance(value, str) else "")
                description = option.get("description")
                typer.echo(f"    - {shown}" + (f": {description}" if description else ""))
