# AGENTS.md

Upstream map, layout, commands and pitfalls: `CLAUDE.md`.

## Specs

| Spec | URL | Targets | Code | Update check |
|---|---|---|---|---|
| T3 Code MCP tools (orchestrator MCP) | https://github.com/pingdotgg/t3code/blob/b387e3a179d7495321019363c5ba11e847834cd0/packages/contracts/src/orchestratorMcp.ts | T3 Code `0.1.11-nightly.20261117.4851` (tag `v0.1.11-nightly.20261117.4851` = commit `b387e3a179d7495321019363c5ba11e847834cd0`; `serverInfo` in `schemas/mcp_tools.json`) | `src/pyt3client/models/tools.py` (generated), `schemas/mcp_tools.json`, `scripts/generate_models.py` | https://github.com/pingdotgg/t3code/releases; re-snapshot with `scripts/generate_models.py fetch` |
| T3 Code native WebSocket RPC | https://github.com/pingdotgg/t3code/blob/b387e3a179d7495321019363c5ba11e847834cd0/packages/contracts/src/environment.ts | `ORCHESTRATION_PROTOCOL_VERSION` 2 (`ORCHESTRATION_PROTOCOL` in `src/pyt3client/native/client.py`) | `src/pyt3client/native/` | upstream `packages/contracts/src/environment.ts` |
| Model Context Protocol (Streamable HTTP) | https://modelcontextprotocol.io/specification/2025-06-18 | 2025-06-18 (`PROTOCOL_VERSION` in `src/pyt3client/mcp.py`, same as upstream `apps/server/src/peer/PeerMcpClient.ts`) | `src/pyt3client/mcp.py` | upstream `PeerMcpClient.ts` |
