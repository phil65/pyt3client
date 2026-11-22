"""Typed handles for T3 Code MCP tools.

Every tool in `pyt3client.models.tools` is exposed as a `ToolSpec` constant
(for example `THREAD_LAUNCH`) that ties the tool's wire name to its generated
input and output models. `T3Client.call` takes one, so calls are type-checked
end to end without hand-written signatures for each of the ~50 tools.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, JsonValue, RootModel


class JsonOutput(RootModel[JsonValue]):
    """Result of a tool whose server advertises no `outputSchema`.

    Those tools (for example `t3_pending_request` or `t3_queue`) still answer
    with a JSON text block; it is decoded but not validated against a schema.
    """


@dataclass(frozen=True, slots=True)
class ToolSpec[InputT: BaseModel, OutputT: BaseModel]:
    """A T3 Code MCP tool: its wire name plus the models for its arguments and result."""

    name: str
    """Tool name as advertised in `tools/list`, such as `t3_thread_launch`."""
    input_type: type[InputT]
    output_type: type[OutputT]
    read_only: bool
    """The tool's `readOnlyHint`. Read-only sign-ins can call only these tools."""
