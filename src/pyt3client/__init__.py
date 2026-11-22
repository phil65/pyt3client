"""pyt3client: thin, typed Python client for T3 Code environments.

T3 Code (https://github.com/pingdotgg/t3code) runs coding agents (Claude Code,
Codex, Cursor, OpenCode, ACP agents, ...) on a machine and exposes them to its
clients. This package talks to an environment through its MCP endpoint
(`/mcp`), the interface T3 Code offers to outside agents and scripts.

```python
from pyt3client import T3Client, pair
from pyt3client.models.tools import THREAD_LIST, ThreadListInput

credentials = await pair("http://127.0.0.1:3773", "PAIRING-CODE")
async with T3Client.from_credentials(credentials) as t3:
    threads = await t3.call(THREAD_LIST, ThreadListInput(all_projects=True))
```
"""

from __future__ import annotations

from importlib.metadata import version

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
from pyt3client.models.auth import T3Credentials
from pyt3client.models.environment import EnvironmentDescriptor
from pyt3client.tool import JsonOutput, ToolSpec

__version__ = version("pyt3client")
__title__ = "pyt3client"

__author__ = "Philipp Temminghoff"
__author_email__ = "philipptemminghoff@googlemail.com"
__copyright__ = "Copyright (c) 2026 Philipp Temminghoff"
__license__ = "MIT"
__url__ = "https://github.com/phil65/pyt3client"

__all__ = [
    "EnvironmentDescriptor",
    "JsonOutput",
    "T3AuthError",
    "T3Client",
    "T3Credentials",
    "T3Error",
    "T3HttpError",
    "T3McpError",
    "T3ProtocolError",
    "T3ToolError",
    "ToolSpec",
    "__version__",
    "fetch_descriptor",
    "pair",
]
