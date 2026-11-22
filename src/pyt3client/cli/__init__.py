"""The `pyt3` command line tool (`pip install pyt3client[cli]`).

Kept out of `pyt3client`'s own imports so the library never needs typer.
"""

from __future__ import annotations

from pyt3client.cli.app import app

__all__ = ["app"]
