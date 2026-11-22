"""Saved sign-ins for the `pyt3` CLI, one per environment alias.

The file is `$XDG_CONFIG_HOME/pyt3client/credentials.json` (falling back to
`~/.config/pyt3client/credentials.json`), readable by the owner only. It holds
bearer tokens: anyone who can read it can act on those environments at the
approved access level until the token expires (30 days) or is revoked in
T3 Code under Settings -> Sharing & access.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, JsonValue

from pyt3client.models.auth import T3Credentials


if TYPE_CHECKING:
    from collections.abc import Mapping


STORE_VERSION = 1


class StoreError(Exception):
    """The credentials file cannot be read, or no environment matches."""


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/pyt3client`, or `~/.config/pyt3client`."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pyt3client"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def default_alias(label: str, base_url: str) -> str:
    """An alias from the environment's label, else its host: lower case, no spaces."""
    alias = re.sub(r"[^a-z0-9._-]+", "-", label.strip().lower()).strip("-")
    return alias or (urlsplit(base_url).hostname or "t3")


class _StoreFile(BaseModel):
    version: int
    environments: dict[str, dict[str, JsonValue]]


def load() -> dict[str, T3Credentials]:
    """All saved environments by alias; empty when nothing is saved yet."""
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        stored = _StoreFile.model_validate_json(path.read_bytes())
        return {
            alias: T3Credentials.model_validate(entry)
            for alias, entry in stored.environments.items()
        }
    except ValueError as error:
        msg = f"{path} is not a valid pyt3 credentials file: {error}"
        raise StoreError(msg) from error


def save(environments: Mapping[str, T3Credentials]) -> Path:
    """Replace the saved environments; the file is created with mode 0600."""
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries: dict[str, dict[str, JsonValue]] = {
        # SecretStr dumps as a mask; the file must hold the real token.
        alias: credentials.model_dump(mode="json")
        | {"access_token": credentials.access_token.get_secret_value()}
        for alias, credentials in sorted(environments.items())
    }
    payload = _StoreFile(version=STORE_VERSION, environments=entries).model_dump_json(indent=2)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(payload + "\n")
    temporary.replace(path)
    path.chmod(0o600)
    return path


def select(
    environments: Mapping[str, T3Credentials], alias: str | None
) -> tuple[str, T3Credentials]:
    """The environment named `alias`, or the only saved one when `alias` is None.

    Raises:
        StoreError: Nothing is saved, the alias is unknown, or several are saved
            and none was chosen.
    """
    if not environments:
        msg = "No environment saved. Run `pyt3 login <url> <pairing-code>` first."
        raise StoreError(msg)
    if alias is not None:
        if alias in environments:
            return alias, environments[alias]
        msg = f"No saved environment {alias!r}. Saved: {', '.join(sorted(environments))}"
        raise StoreError(msg)
    if len(environments) == 1:
        return next(iter(environments.items()))
    msg = (
        "Several environments are saved; choose one with --env or PYT3_ENV: "
        f"{', '.join(sorted(environments))}"
    )
    raise StoreError(msg)


def is_expired(credentials: T3Credentials, *, now: datetime | None = None) -> bool:
    return credentials.expires_at <= (now or datetime.now(UTC))
