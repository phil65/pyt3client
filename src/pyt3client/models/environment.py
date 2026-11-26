"""The public descriptor every T3 Code environment serves without authentication."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EnvironmentPlatform(BaseModel):
    """Upstream `ExecutionEnvironmentPlatform` (packages/contracts/src/environment.ts)."""

    model_config = ConfigDict(extra="allow")

    os: str
    arch: str
    machine: str | None = None
    """Forward compatible upstream (`EnvironmentMachineKind`), so typed as `str`."""


class EnvironmentDescriptor(BaseModel):
    """`GET /.well-known/t3/environment`.

    Upstream `ExecutionEnvironmentDescriptor` in packages/contracts/src/environment.ts.
    Clients read it before sending any credential: an address only gets a token
    when it reports the expected `environment_id`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    environment_id: str = Field(alias="environmentId")
    """Stable across restarts and address changes; identifies the environment."""
    label: str
    platform: EnvironmentPlatform
    server_version: str = Field(alias="serverVersion")
    orchestration_protocol_version: int | None = Field(None, alias="orchestrationProtocolVersion")
    """Version of the native WebSocket RPC protocol (`ORCHESTRATION_PROTOCOL_VERSION`)."""
    database_migration: int | None = Field(None, alias="databaseMigration")
    capabilities: dict[str, JsonValue]
    """Feature flags (upstream `ExecutionEnvironmentCapabilities`).

    Dozens of mostly-boolean flags that grow with every release, so kept as a
    mapping; use `supports()` to check one.
    """

    def supports(self, capability: str) -> bool:
        """Whether the server advertises `capability` as enabled.

        Args:
            capability: Flag name as sent by the server, such as `mcpApprovalCodes`.
        """
        return self.capabilities.get(capability) is True
