"""Regenerate `pyt3client.models.tools` from a T3 Code server's advertised MCP tools.

T3 Code's MCP tools are defined with Effect Schema in TypeScript and advertised
over `tools/list` as JSON Schema (`inputSchema` and, for most tools,
`outputSchema`). Instead of hand-porting the TypeScript we snapshot what a real
server advertises and generate Pydantic models from it, so the models match
the wire exactly.

Upstream (https://github.com/pingdotgg/t3code):

- Tool definitions: `apps/server/src/mcp/toolkits/*/tools.ts`
- Source schemas: `packages/contracts/src/orchestratorMcp.ts` and friends
- Schema simplification for advertising: `apps/server/src/mcp/advertisedJsonSchema.ts`
  (this is why some fields are `{}` / Any: branded or recursive schemas are
  loosened before they are advertised)

Usage:

    # 1. Snapshot from a running server (token from `pyt3client.pair(...)`):
    uv run --group codegen scripts/generate_models.py fetch --url http://127.0.0.1:3773 --token ...

    # 2. Regenerate models from the committed snapshot:
    uv run --group codegen scripts/generate_models.py generate

Run against an isolated server, never your live install, for example:
`t3 serve --base-dir /tmp/t3home --port 39731 --no-browser`, then mint a code
with `t3 auth pairing create --base-dir /tmp/t3home --json`.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "schemas" / "mcp_tools.json"
OUTPUT = ROOT / "src" / "pyt3client" / "models" / "tools.py"
# The MCP revision T3's server speaks; see `PROTOCOL_VERSION` in
# apps/server/src/peer/PeerMcpClient.ts upstream.
PROTOCOL_VERSION = "2025-06-18"


def _response_json(response: Any) -> Any:
    """Decode an MCP response that is either JSON or a single SSE event."""
    text: str = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        text = "".join(
            line.removeprefix("data:").strip()
            for line in text.splitlines()
            if line.startswith("data:")
        )
    return json.loads(text)


def fetch(url: str, token: str) -> None:
    """Snapshot `initialize` + `tools/list` from a live server into SNAPSHOT."""
    import httpx

    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
    }
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30) as client:
        init = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pyt3client-codegen", "version": "1"},
                },
            },
        )
        init.raise_for_status()
        server_info = _response_json(init)["result"]["serverInfo"]
        session = {
            "mcp-session-id": init.headers["mcp-session-id"],
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
        )
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {} if cursor is None else {"cursor": cursor}
            listed = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
                headers=session,
            )
            listed.raise_for_status()
            result = _response_json(listed)["result"]
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        client.delete("/mcp", headers=session)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"serverInfo": server_info, "tools": sorted(tools, key=lambda t: t["name"])}
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(tools)} tools from {server_info} to {SNAPSHOT.relative_to(ROOT)}")


def class_base(tool_name: str) -> str:
    """`t3_thread_launch` -> `ThreadLaunch`, `schedule_task` -> `ScheduleTask`."""
    return "".join(part.capitalize() for part in tool_name.removeprefix("t3_").split("_"))


def constant_name(tool_name: str) -> str:
    """`t3_thread_launch` -> `THREAD_LAUNCH`."""
    return tool_name.removeprefix("t3_").upper()


# --- Schema patches ------------------------------------------------------------
# The advertised schemas are loosened in places (`{}` = anything). Where the
# upstream contract is precise, restore it so the models stay typed. Each patch
# names the upstream schema it mirrors; re-check them when bumping the snapshot.

# packages/contracts/src/modelSelection.ts `ModelSelectionWire` (+ model.ts
# `ProviderOptionSelection`). The advertised schema is the lenient decode side
# (`ModelSelectionSource`, which also accepts a legacy `provider`); the server
# always encodes this canonical form, and accepts it on input.
MODEL_SELECTION: dict[str, Any] = {
    "title": "ModelSelection",
    "type": "object",
    "properties": {
        "instanceId": {
            "type": "string",
            "description": "Provider instance id, such as `codex` or `claudeAgent`.",
        },
        "model": {"type": "string", "description": "Model slug of that provider."},
        "options": {
            "type": "array",
            "description": "Model options, as listed by `orchestrator_capabilities`.",
            "items": {
                "title": "ProviderOptionSelection",
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "value": {"anyOf": [{"type": "string"}, {"type": "boolean"}]},
                },
                "required": ["id", "value"],
            },
        },
    },
    "required": ["instanceId", "model"],
}
_MODEL_SELECTION_KEYS = {"provider", "instanceId", "model", "options"}
# Forward-compatible literals the server advertises as `{}`. Typed as `str` so a
# newer server's values still decode:
FORWARD_COMPATIBLE_STRINGS: dict[str, str] = {
    # packages/contracts/src/environment.ts `ThreadEnvMode`
    "defaultThreadEnvMode": "`local` or `worktree` (upstream `ThreadEnvMode`).",
    # packages/contracts/src/environment.ts `EnvironmentMachineKind`
    "machine": "Machine kind such as `laptop` (upstream `EnvironmentMachineKind`).",
}


def patch_schema(node: Any) -> Any:
    """Restore precise upstream types where the advertised schema is `{}`."""
    if isinstance(node, list):
        return [patch_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    properties = node.get("properties")
    if isinstance(properties, dict) and set(properties) == _MODEL_SELECTION_KEYS:
        return {"$ref": "#/$defs/ModelSelection"}
    patched = {key: patch_schema(value) for key, value in node.items()}
    if isinstance(properties, dict):
        patched["properties"] = {
            key: (
                {"type": "string", "description": FORWARD_COMPATIBLE_STRINGS[key]}
                if key in FORWARD_COMPATIBLE_STRINGS and value == {}
                else patch_schema(value)
            )
            for key, value in properties.items()
        }
    return patched


def generate() -> None:
    """Generate OUTPUT from SNAPSHOT with datamodel-code-generator."""
    snapshot = json.loads(SNAPSHOT.read_text())
    tools: list[dict[str, Any]] = snapshot["tools"]
    defs: dict[str, Any] = {"ModelSelection": MODEL_SELECTION}
    for tool in tools:
        base = class_base(tool["name"])
        defs[f"{base}Input"] = {**patch_schema(tool["inputSchema"]), "title": f"{base}Input"}
        if output := tool.get("outputSchema"):
            defs[f"{base}Output"] = {**patch_schema(output), "title": f"{base}Output"}
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "T3McpTools",
        "type": "object",
        # Referencing every definition makes the generator emit all of them.
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in defs},
        "$defs": defs,
    }
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = Path(tmp) / "tools.schema.json"
        models_path = Path(tmp) / "tools.py"
        schema_path.write_text(json.dumps(document))
        subprocess.run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(schema_path),
                "--input-file-type",
                "jsonschema",
                "--output",
                str(models_path),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.13",
                "--enum-field-as-literal",
                "all",
                "--use-union-operator",
                "--use-standard-collections",
                "--use-schema-description",
                "--use-field-description",
                "--use-double-quotes",
                "--snake-case-field",
                # Wire names come from the base class's camelCase alias generator;
                # see _normalize_fields below.
                "--base-class",
                "pyt3client.models._base.T3Model",
                "--use-title-as-name",
                "--collapse-root-models",
                # Identical nested shapes (project icons, schedules, ...) become one class.
                "--reuse-model",
                # Plain `str` / `int` with `Field(max_length=...)` instead of `constr(...)`,
                # which type checkers cannot see through.
                "--field-constraints",
                "--disable-timestamp",
                "--formatters",
                "ruff-format",
            ],
            check=True,
        )
        models = models_path.read_text()
    # The wrapper model only exists to make the generator emit every definition.
    models = _normalize_fields(_drop_class(models, "T3McpTools"))
    OUTPUT.write_text(_header(snapshot) + models + _registry(tools))
    subprocess.run(["ruff", "format", "--quiet", str(OUTPUT)], check=True)
    # Formatting collapses `= (Field(...))` wrappers left by the first pass; simplify again.
    OUTPUT.write_text(_normalize_fields(OUTPUT.read_text()))
    subprocess.run(["ruff", "check", "--fix", "--quiet", str(OUTPUT)], check=False)
    subprocess.run(["ruff", "format", "--quiet", str(OUTPUT)], check=True)
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(tools)} tools)")


def _normalize_fields(source: str) -> str:
    """Rewrite `Field(...)` calls so type checkers read the models like pydantic does.

    Pyright and other `dataclass_transform` readers take a per-field `alias` as
    the constructor keyword, and recognize a default only when passed as
    `default=`. So for every field:

    - `alias="x"` is dropped when `T3Model`'s camelCase generator yields the same
      name from the field name; the rest (`_tag` on `field_tag`, `async` on
      `async_`) stay. tests/test_models.py checks every wire name against the
      snapshot.
    - A positional default becomes `default=...`; a positional `...` (required)
      is dropped.
    """
    from pydantic.alias_generators import to_camel

    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def position(lineno: int, col: int) -> int:
        # ast columns are UTF-8 byte offsets; the generated code is ASCII.
        return offsets[lineno - 1] + col

    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "Field"
        ):
            continue
        call = node.value
        keywords = [
            keyword
            for keyword in call.keywords
            if not (
                keyword.arg == "alias"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == to_camel(node.target.id)
            )
        ]
        parts = [
            f"default={ast.get_source_segment(source, arg)}"
            for arg in call.args
            if not (isinstance(arg, ast.Constant) and arg.value is Ellipsis)
        ]
        parts += [ast.get_source_segment(source, keyword) or "" for keyword in keywords]
        start = position(call.lineno, call.col_offset)
        end = position(call.end_lineno or call.lineno, call.end_col_offset or 0)
        edits.append((start, end, f"Field({', '.join(parts)})"))
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    # A Field left with nothing but its default is just the default.
    source = re.sub(r" = Field\(\)", "", source)
    return re.sub(r" = Field\(default=None\)", " = None", source)


def _drop_class(source: str, name: str) -> str:
    """Remove one top-level class definition from generated source."""
    lines = source.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"class {name}("))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and not lines[i].startswith((" ", "\t"))
        ),
        len(lines),
    )
    return "".join(lines[:start] + lines[end:])


def _header(snapshot: dict[str, Any]) -> str:
    info = snapshot["serverInfo"]
    return (
        '"""Pydantic models and a typed registry for T3 Code\'s MCP tools.\n\n'
        "GENERATED by scripts/generate_models.py from schemas/mcp_tools.json - do not edit.\n"
        f"Snapshot taken from {info['name']} {info['version']}.\n\n"
        "Upstream sources: apps/server/src/mcp/toolkits/*/tools.ts and\n"
        "packages/contracts/src/orchestratorMcp.ts in https://github.com/pingdotgg/t3code.\n"
        '"""\n\n'
    )


def _registry(tools: list[dict[str, Any]]) -> str:
    out = [
        "\n\n# --- Tool registry -----------------------------------------------------------\n",
        "from pyt3client.tool import JsonOutput, ToolSpec  # noqa: E402\n\n",
    ]
    for tool in tools:
        name = tool["name"]
        base = class_base(name)
        output = f"{base}Output" if tool.get("outputSchema") else "JsonOutput"
        read_only = bool((tool.get("annotations") or {}).get("readOnlyHint", False))
        out.append(
            f"{constant_name(name)}: ToolSpec[{base}Input, {output}] = ToolSpec(\n"
            f'    name="{name}",\n'
            f"    input_type={base}Input,\n"
            f"    output_type={output},\n"
            f"    read_only={read_only},\n"
            ")\n"
        )
    names = ", ".join(constant_name(tool["name"]) for tool in tools)
    out.append(
        f"\nALL_TOOLS: tuple[ToolSpec[Any, Any], ...] = ({names},)\n"
        '"""Every tool in the snapshot, in name order."""\n'
    )
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="Snapshot tools/list from a running server")
    fetch_parser.add_argument("--url", required=True, help="Environment origin, without /mcp")
    fetch_parser.add_argument("--token", required=True, help="MCP bearer token")
    fetch_parser.add_argument("--no-generate", action="store_true", help="Only write the snapshot")
    sub.add_parser("generate", help="Regenerate models from the committed snapshot")
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.url, args.token)
        if args.no_generate:
            return
    generate()


if __name__ == "__main__":
    main()
