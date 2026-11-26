"""The generated models must match the wire names in the committed schema snapshot."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pyt3client.models import tools
from pyt3client.tool import JsonOutput


if TYPE_CHECKING:
    from pydantic import BaseModel


SNAPSHOT = json.loads(
    (Path(__file__).parent.parent / "schemas" / "mcp_tools.json").read_text(encoding="utf-8")
)
SNAPSHOT_TOOLS: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in SNAPSHOT["tools"]}
SPECS = {spec.name: spec for spec in tools.ALL_TOOLS}


def wire_names(model: type[BaseModel]) -> set[str]:
    return {info.alias or name for name, info in model.model_fields.items()}


def test_registry_covers_snapshot() -> None:
    assert set(SPECS) == set(SNAPSHOT_TOOLS)


@pytest.mark.parametrize("name", sorted(SNAPSHOT_TOOLS))
def test_input_wire_names_match_schema(name: str) -> None:
    schema = SNAPSHOT_TOOLS[name]["inputSchema"]
    assert wire_names(SPECS[name].input_type) == set(schema.get("properties", {}))


@pytest.mark.parametrize("name", sorted(SNAPSHOT_TOOLS))
def test_output_wire_names_match_schema(name: str) -> None:
    spec = SPECS[name]
    schema = SNAPSHOT_TOOLS[name].get("outputSchema")
    if schema is None:
        assert spec.output_type is JsonOutput
        return
    assert wire_names(spec.output_type) == set(schema.get("properties", {}))


@pytest.mark.parametrize("name", sorted(SNAPSHOT_TOOLS))
def test_read_only_flag_matches_annotations(name: str) -> None:
    annotations = SNAPSHOT_TOOLS[name].get("annotations") or {}
    assert SPECS[name].read_only is bool(annotations.get("readOnlyHint", False))


def test_snake_case_construction_dumps_camel_case() -> None:
    params = tools.ThreadWaitInput(thread_id="t1", timeout_ms=1000)
    dumped = params.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped == {"threadId": "t1", "timeoutMs": 1000}


def test_explicit_aliases_survive() -> None:
    """`_tag` and `async` cannot come from the camelCase generator."""
    names = {
        info.alias
        for model in (tools.Run, tools.Script)
        for info in model.model_fields.values()
        if info.alias
    }
    assert {"_tag", "async"} <= names


def test_model_selection_is_canonical() -> None:
    """The loosened advertised ModelSelection is replaced by the upstream wire form."""
    selection = tools.ModelSelection.model_validate({
        "instanceId": "codex",
        "model": "gpt-5",
        "options": [{"id": "reasoningEffort", "value": "high"}, {"id": "fast", "value": True}],
    })
    assert selection.instance_id == "codex"
    assert selection.options is not None
    assert [option.value for option in selection.options] == ["high", True]
