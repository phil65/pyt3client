"""Saved sign-ins, one per environment alias (written by `pyt3 login`).

The file is `$XDG_CONFIG_HOME/pyt3client/credentials.json` (falling back to
`~/.config/pyt3client/credentials.json`), readable by the owner only. Next to the
MCP sign-ins (`environments`) it keeps native sign-ins (`native`, written by
`pyt3 login-native`) under the same aliases. It holds bearer tokens: anyone who
can read it can act on those environments at the approved access level until the
token expires (30 days) or is revoked in T3 Code under Settings -> Sharing & access.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, JsonValue, SecretStr

from pyt3client.models.auth import T3Credentials, T3NativeCredentials


if TYPE_CHECKING:
    from collections.abc import Generator, Mapping


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
    native: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    """Native sign-ins by alias; absent from files written before they existed."""


def _read() -> _StoreFile:
    path = credentials_path()
    if not path.exists():
        return _StoreFile(version=STORE_VERSION, environments={})
    try:
        return _StoreFile.model_validate_json(path.read_bytes())
    except ValueError as error:
        msg = f"{path} is not a valid pyt3 credentials file: {error}"
        raise StoreError(msg) from error


def load() -> dict[str, T3Credentials]:
    """All saved MCP sign-ins by alias; empty when nothing is saved yet."""
    stored = _read()
    try:
        return {
            alias: T3Credentials.model_validate(entry)
            for alias, entry in stored.environments.items()
        }
    except ValueError as error:
        msg = f"{credentials_path()} holds an invalid sign-in: {error}"
        raise StoreError(msg) from error


def load_native() -> dict[str, T3NativeCredentials]:
    """All saved native sign-ins by alias."""
    stored = _read()
    try:
        return {
            alias: T3NativeCredentials.model_validate(entry)
            for alias, entry in stored.native.items()
        }
    except ValueError as error:
        msg = f"{credentials_path()} holds an invalid native sign-in: {error}"
        raise StoreError(msg) from error


def _entries(
    saved: Mapping[str, T3Credentials | T3NativeCredentials],
) -> dict[str, dict[str, JsonValue]]:
    entries: dict[str, dict[str, JsonValue]] = {}
    for alias, credentials in sorted(saved.items()):
        token: SecretStr = credentials.access_token
        # SecretStr dumps as a mask; the file must hold the real token.
        entries[alias] = credentials.model_dump(mode="json") | {
            "access_token": token.get_secret_value()
        }
    return entries


@contextmanager
def _locked() -> Generator[None]:
    """Hold an exclusive lock on `credentials.json.lock` (across processes).

    Every read-modify-write of the store holds it, so concurrent sign-ins don't
    overwrite each other. No-op where `flock` is missing (Windows).
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # releases the lock


def save(environments: Mapping[str, T3Credentials]) -> Path:
    """Replace the saved MCP sign-ins (native ones stay); the file has mode 0600.

    To add or remove one sign-in, use `update`: it doesn't lose concurrent ones.
    """
    with _locked():
        stored = _read()
        store = _StoreFile(
            version=STORE_VERSION, environments=_entries(environments), native=stored.native
        )
        return _write(store)


def save_native(native: Mapping[str, T3NativeCredentials]) -> Path:
    """Replace the saved native sign-ins (MCP ones stay); see `update_native`."""
    with _locked():
        stored = _read()
        store = _StoreFile(
            version=STORE_VERSION, environments=stored.environments, native=_entries(native)
        )
        return _write(store)


def update(alias: str, credentials: T3Credentials | None) -> Path:
    """Save (or with None, forget) the MCP sign-in of one alias; the others stay."""
    with _locked():
        stored = _read()
        environments = dict(stored.environments)
        if credentials is None:
            environments.pop(alias, None)
        else:
            environments |= _entries({alias: credentials})
        return _write(
            stored.model_copy(
                update={
                    "version": STORE_VERSION,
                    "environments": dict(sorted(environments.items())),
                }
            )
        )


def update_native(alias: str, credentials: T3NativeCredentials | None) -> Path:
    """Save (or with None, forget) the native sign-in of one alias; the others stay."""
    with _locked():
        stored = _read()
        native = dict(stored.native)
        if credentials is None:
            native.pop(alias, None)
        else:
            native |= _entries({alias: credentials})
        return _write(
            stored.model_copy(
                update={"version": STORE_VERSION, "native": dict(sorted(native.items()))}
            )
        )


def _write(store: _StoreFile) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = store.model_dump_json(indent=2)
    # A temp file per write (mkstemp: mode 0600), so concurrent writers never write into
    # or rename away each other's file; the rename replaces the store atomically.
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(payload + "\n")
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
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


def is_expired(
    credentials: T3Credentials | T3NativeCredentials, *, now: datetime | None = None
) -> bool:
    return credentials.expires_at <= (now or datetime.now(UTC))
