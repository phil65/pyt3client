# pyt3client

Pinned upstream versions (T3 Code commit, MCP revision): see the Specs table in `AGENTS.md`.

Thin, typed Python client for [T3 Code](https://github.com/pingdotgg/t3code)
environments. It talks to an environment's MCP endpoint (`/mcp`), the interface T3
Code offers outside agents and scripts.

This file maps the parts of upstream that matter here, so you don't have to
rediscover them. Paths below are relative to the t3code repository. To read
upstream: `git clone --depth 1 https://github.com/pingdotgg/t3code`. The upstream
code is TypeScript built on Effect.

## Layout

| Path | What |
|---|---|
| `src/pyt3client/auth.py` | `pair()`: headless OAuth sign-in with a pairing code. `fetch_descriptor()`. |
| `src/pyt3client/mcp.py` | Minimal MCP Streamable HTTP JSON-RPC client (`McpHttpSession`). |
| `src/pyt3client/client.py` | `T3Client`: typed `call(spec, params)`, `call_raw`, `wait_for_thread`, `list_tools`. |
| `src/pyt3client/tool.py` | `ToolSpec[In, Out]` and `JsonOutput`. |
| `src/pyt3client/cli/` | `pyt3` CLI (typer, `cli` extra; never imported by the library). Tests inject a transport via `Runtime` as `obj=`. |
| `src/pyt3client/credentials.py` | Saved sign-ins (`pyt3 login`): `load()`, `select()`, `save()`, `is_expired()`; native ones (`pyt3 login-native`) in `load_native()` / `save_native()` under the same aliases. No typer, so library users such as agentpool can read them. |
| `src/pyt3client/native/` | Native WebSocket RPC (`native` extra): `sign_in_native()`, `websocket_url()`, `RpcSocket` (Effect RPC framing), `T3NativeClient` (`subscribe_thread`, `dispatch`, `respond_to_request`), hand-written stream models. |
| `src/pyt3client/models/tools.py` | **Generated.** Input/output models + one `ToolSpec` constant per tool. Do not edit. |
| `src/pyt3client/models/{auth,environment}.py` | Hand-written mirrors of upstream contracts. |
| `schemas/mcp_tools.json` | Snapshot of a real server's `tools/list` (input + output JSON Schemas). |
| `scripts/generate_models.py` | Snapshot (`fetch`) and codegen (`generate`). Holds schema patches. |

## Commands

```bash
uv sync --all-groups
uv run pytest                                   # unit tests; integration tests skip
uv run ruff check . && uv run ruff format --check . && uv run ty check
uvx pyright src tests scripts                   # editors use pyright; keep it clean too
uv run --group codegen scripts/generate_models.py generate              # from snapshot
uv run --group codegen scripts/generate_models.py fetch --url URL --token TOKEN  # re-snapshot
```

## Testing against a real server

**Never touch the user's live install** (`~/.t3/userdata`): don't start a server
against it and don't write to it. Run an isolated server with its own data
directory and port:

```bash
# Strip inherited Claude env vars (see below), then serve on a scratch dir + port.
env $(env | grep -oE '^(ANTHROPIC_[A-Z_]*|CLAUDECODE|CLAUDE_[A-Z_]*)' | sed 's/^/-u /') \
  t3 serve --base-dir /tmp/t3home --port 39731 --host 127.0.0.1 --no-browser /tmp/t3proj
t3 auth pairing create --base-dir /tmp/t3home --ttl 10m --json   # "credential" is the code
T3_URL=http://127.0.0.1:39731 T3_PAIRING_CODE=<code> uv run pytest -m integration
```

The `serverInstallation.baseDir` in the descriptor names the launcher install, not
the data directory. The data lands under `--base-dir`/userdata. A first start runs
migrations there, which is how you can tell it's isolated.

**Start the server without `ANTHROPIC_API_KEY` and `CLAUDECODE` / `CLAUDE_*`.**
When you launch it from inside a Claude Code session, it inherits them, and the
`claudeAgent` provider then runs on that API key ("Credit balance is too low")
instead of the user's subscription login in `~/.claude/.credentials.json`. With
the subscription, agent turns cost nothing extra, so tests may run real turns.
`test_agent_turn_and_follow_up` does: Haiku, about 8 s.

## How the client fits T3 Code

### Two client surfaces upstream; this package uses the MCP one

1. **`/mcp` (used here).** MCP over Streamable HTTP plus OAuth 2.1. It's the
   supported route for outside agents. User docs: `docs/user/outside-agents.md`.
   Tool reference: `docs/orchestration-v2/orchestrator-mcp-server.md`.
2. **Native RPC (`pyt3client.native`).** This is what web, desktop and mobile use.
   REST lives under `/api` (`packages/contracts/src/environmentHttp.ts`). The
   WebSocket is Effect RPC at `/ws?orchestrationProtocol=2&wsTicket=…` with JSON
   frames:
   `{"_tag":"Request","id","tag","payload","headers":[]}` →
   `{"_tag":"Chunk","requestId","values"}` /
   `{"_tag":"Exit","requestId","exit":{"_tag":"Success"|"Failure",...}}`, plus
   `Ack`, `Interrupt`, `Ping`/`Pong` (vendored at
   `.repos/effect/packages/effect/src/rpc/RpcMessage.ts`). Methods are in
   `packages/contracts/src/rpc.ts` and `orchestrationV2.ts`
   (`ORCHESTRATION_V2_WS_METHODS`). Auth goes: pairing code →
   `POST /oauth/token` (token exchange with
   `subject_token_type=urn:t3:params:oauth:token-type:environment-bootstrap`) →
   bearer → `POST /api/auth/websocket-ticket` → socket. A working end-to-end
   example is `apps/server/scripts/verify-background-live.ts`. **An MCP token
   (subject `mcp-client`) is rejected on this surface, and a native bearer on
   `/mcp`**, so a client of both holds two sign-ins.

   Remote clients exchange a one-time pairing *token* (`t3 auth pairing create`;
   not the MCP pairing code) the same way (upstream
   `packages/client-runtime/src/authorization/remote.ts`,
   `bootstrapRemoteBearerSession`). Found live (2026-11-22):
   - The stream is `snapshot`, then `event`s; `turn-item.updated` carries whole
     items. Long assistant and reasoning texts grow in place (`streaming: true`,
     updates about 0.5 s apart). Over `/mcp` the same message only shows once done.
   - Approvals: an `approval_request` item (`status: waiting`, `requestId`,
     `requestKind`, `prompt`) plus `runtime-request.updated`. Answer with the
     `runtime-request.respond` command (`decision`) via `dispatchCommand`.
   - Read-only commands (`ls`) need no approval even in `approval-required`.
   - Ack every `Chunk`; the server waits for it before sending more.

### Sign-in (`auth.py`)

This ports upstream `PeerLinks.signIn` (`apps/server/src/peer/PeerLinks.ts`): register →
`POST /oauth/mcp/decision` with `{"_tag":"pairing-code", access, code}` → read
`code` from the returned `redirectTo` → `POST /oauth/mcp/token` with the PKCE verifier.
Nothing listens on the loopback redirect URI. Server side:
`apps/server/src/auth/mcpOAuthHttp.ts` and `apps/server/src/auth/McpOAuth.ts`.
Schemas: `packages/contracts/src/auth.ts`.

- Access levels (`AuthMcpClientAccess`) map to scopes through `mcpClientScopes`. A
  plain pairing code must carry those scopes. App approval codes (`AUTO-…`,
  `FULL-…`, …) fix their own level (`parseMcpApprovalCode`).
- Tokens last 30 days and don't renew. Revoke them in Settings → Sharing & access.
- The server derives the issuer and resource from Host and `X-Forwarded-Proto`, so a
  proxy that rewrites them breaks sign-in.
- Don't register with `software_id: t3code-peer-link`. That marks a *linked
  environment* and fences its work (`apps/server/src/mcp/linkOrigin.ts`).

### MCP transport (`mcp.py`)

This mirrors `apps/server/src/peer/PeerMcpClient.ts`. Protocol `2025-06-18`. The
session comes from the `mcp-session-id` header; after that, every request needs
`mcp-protocol-version` (or the server answers 400). Sessions live in server memory:
a 404 means re-initialize. A request with an `Origin` header gets 403
(`apps/server/src/mcp/McpHttpServer.ts`). The optional `t3-mode-limit:
<runtime>/<interaction>` header only ever narrows access
(`MODE_LIMIT_HEADER` in `apps/server/src/mcp/McpInvocationContext.ts`).

### Tool results and errors (`client.py`)

- Success: one JSON text block, plus `structuredContent` for tools with an
  `outputSchema`.
- Refusal: `isError: true` with the text
  `{"_tag":"OrchestratorMcpFailure","code","message"}` → `T3ToolError`. Codes come
  from `OrchestratorMcpFailure` in `packages/contracts/src/orchestratorMcp.ts`.
- A JSON-RPC `error` (unknown tool, bad arguments) → `T3McpError`.
- `t3_thread_wait` is split into ≤50 s calls. The server sends nothing during a
  wait, and T3 Connect drops idle requests after about 100 s (see "Peer links" in
  `docs/internals/remote.md`).

### Server policy we've observed (useful when a call is refused)

- Project tools (`t3_project_create/update/delete`) need a **full-access** client:
  `capability_denied` otherwise.
- `t3_thread_launch` in a project with no default model needs `model_selection`.
  Pick one from `orchestrator_capabilities`.
- Tools that act *as the calling thread* (`delegate_task`, `task_status`,
  `create_threads`, `request_secret`, …) aren't offered to outside clients. Calling
  them returns `thread_credential_required`. Use `t3_thread_launch` and
  `t3_thread_wait` instead.
- A thread a client starts or steers can never run above the client's approved
  level, or above its `t3-mode-limit`.

## Generated models: how and why

The tools are defined in `apps/server/src/mcp/toolkits/*/tools.ts` (Effect Schema)
and advertised as JSON Schema after `apps/server/src/mcp/advertisedJsonSchema.ts`
simplifies them. Rather than port TypeScript by hand, we snapshot the advertised
schemas and generate Pydantic models with datamodel-code-generator. Post-processing
in `scripts/generate_models.py`:

- **Schema patches.** Where the advertised schema is `{}` but the contract is
  precise, the canonical form is restored. The `ModelSelection` advertised is the
  lenient decode side (`ModelSelectionSource` in
  `packages/contracts/src/modelSelection.ts`). We use the wire form
  `{instanceId, model, options?: [{id, value}]}`. Re-check the patches whenever you
  re-snapshot.
- **Field normalization** (`_normalize_fields`). Models subclass
  `models/_base.T3Model` (`alias_generator=to_camel`). Per-field aliases are
  dropped when `to_camel(field_name)` already produces them, and positional
  defaults become `default=`. Without this, pyright flags every
  `Model(snake_name=...)` call. The aliases that remain (`_tag`, `async`) are
  intentional.
- `tests/test_models.py` checks every model's wire names, and the registry's
  `read_only` flags, against the snapshot. If it fails after regeneration, the
  post-processing is wrong.

To bump to a newer T3 Code: start an isolated server on the new version, run
`fetch`, review the diff of `schemas/mcp_tools.json` and `models/tools.py`, then run
the tests and the integration tests.
