# pyt3client

[![PyPI License](https://img.shields.io/pypi/l/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Package status](https://img.shields.io/pypi/status/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Monthly downloads](https://img.shields.io/pypi/dm/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Distribution format](https://img.shields.io/pypi/format/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Wheel availability](https://img.shields.io/pypi/wheel/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Python version](https://img.shields.io/pypi/pyversions/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Implementation](https://img.shields.io/pypi/implementation/pyt3client.svg)](https://pypi.org/project/pyt3client/)
[![Releases](https://img.shields.io/github/downloads/phil65/pyt3client/total.svg)](https://github.com/phil65/pyt3client/releases)
[![Github Contributors](https://img.shields.io/github/contributors/phil65/pyt3client)](https://github.com/phil65/pyt3client/graphs/contributors)
[![Github Forks](https://img.shields.io/github/forks/phil65/pyt3client)](https://github.com/phil65/pyt3client/forks)
[![Github Issues](https://img.shields.io/github/issues/phil65/pyt3client)](https://github.com/phil65/pyt3client/issues)
[![Github Issues](https://img.shields.io/github/issues-pr/phil65/pyt3client)](https://github.com/phil65/pyt3client/pulls)
[![Github Watchers](https://img.shields.io/github/watchers/phil65/pyt3client)](https://github.com/phil65/pyt3client/watchers)
[![Github Stars](https://img.shields.io/github/stars/phil65/pyt3client)](https://github.com/phil65/pyt3client/stars)
[![Github Repository size](https://img.shields.io/github/repo-size/phil65/pyt3client)](https://github.com/phil65/pyt3client)
[![Github last commit](https://img.shields.io/github/last-commit/phil65/pyt3client)](https://github.com/phil65/pyt3client/commits)
[![Github release date](https://img.shields.io/github/release-date/phil65/pyt3client)](https://github.com/phil65/pyt3client/releases)
[![Github language count](https://img.shields.io/github/languages/count/phil65/pyt3client)](https://github.com/phil65/pyt3client)
[![Github commits this month](https://img.shields.io/github/commit-activity/m/phil65/pyt3client)](https://github.com/phil65/pyt3client)
[![Package status](https://codecov.io/gh/phil65/pyt3client/branch/main/graph/badge.svg)](https://codecov.io/gh/phil65/pyt3client/)
[![PyUp](https://pyup.io/repos/github/phil65/pyt3client/shield.svg)](https://pyup.io/repos/github/phil65/pyt3client/)

[Read the documentation!](https://phil65.github.io/pyt3client/)

Thin, typed Python client for [T3 Code](https://github.com/pingdotgg/t3code)
environments. It drives T3 Code's threads, projects and agents from Python through
the environment's MCP endpoint (`/mcp`), the interface T3 Code offers outside agents.

- **Headless sign-in.** `pair()` turns a one-time pairing code into a 30-day token,
  without a browser.
- **Fully typed.** Every one of the server's ~50 tools has generated Pydantic input
  and output models, taken from the JSON Schemas a real server advertises.
- **Small.** It depends only on `httpx2` and `pydantic`.

## Quickstart

On the machine running T3 Code, create a pairing code:

```bash
t3 auth pairing create      # or in the app: Settings -> Sharing & access
```

```python
import asyncio

from pyt3client import T3Client, pair
from pyt3client.models.tools import (
    ORCHESTRATOR_CAPABILITIES,
    THREAD_LAUNCH,
    THREAD_READ,
    ModelSelection,
    ThreadLaunchInput,
    ThreadReadInput,
)


async def main() -> None:
    # One time: spend the pairing code. Persist `credentials` (it is a pydantic model).
    credentials = await pair("http://127.0.0.1:3773", "PAIRING-CODE", access="auto")

    async with T3Client.from_credentials(credentials) as t3:
        caps = await t3.call(ORCHESTRATOR_CAPABILITIES)
        provider = caps.providers[0]
        launched = await t3.call(
            THREAD_LAUNCH,
            ThreadLaunchInput(
                project_id="<project id>",
                title="Find slow tests",
                message="Find why the test suite is slow.",
                model_selection=ModelSelection(
                    instance_id=provider.provider_instance_id, model=provider.models[0].id
                ),
            ),
        )
        done = await t3.wait_for_thread(launched.thread_id, run_id=launched.run_id)
        transcript = await t3.call(THREAD_READ, ThreadReadInput(thread_id=launched.thread_id))
        print(done.status, [item.text for item in transcript.items])


asyncio.run(main())
```

Refusals raise `T3ToolError` with the server's code (for example `thread_not_found`
or `capability_denied`). For a tool newer than the bundled models, use
`t3.call_raw(name, arguments)`.

## Command line: `pyt3`

T3 Code's own `t3` CLI only works on the machine running the server. `pyt3`
signs in once and then drives any environment it can reach, such as a homelab
box over Tailscale or a VPS over T3 Connect.

```bash
pip install "pyt3client[cli]"

pyt3 login https://box.tailnet.ts.net:3773 PAIRING-CODE --name box
pyt3 models                                    # providers, models, options
pyt3 start /srv/app "Find why the tests are slow" \
    --provider claudeAgent --model claude-haiku-5-5 --wait
pyt3 threads                                   # or --search, --status, --project
echo "Now fix it" | pyt3 send <thread-id> - --wait
pyt3 show <thread-id>                          # state + latest messages
pyt3 pending <thread-id>                       # questions the agent waits on
pyt3 answer <thread-id> <request-id> color=red
pyt3 interrupt <thread-id>
```

`start` takes a project id or any folder inside a project. Relative paths resolve
against your shell, which only makes sense when the server runs on this machine.
Every command takes `--json`. With several saved environments, pick one with
`--env <alias>` or `PYT3_ENV`.

`start`, `send --wait` and `wait` exit like upstream `t3 thread --wait`:
0 when the run completed, 1 when it failed (or on any error), 2 when the agent
waits for an answer or approval, and 3 when `--timeout` ran out.

Tokens are saved in `$XDG_CONFIG_HOME/pyt3client/credentials.json` (mode 0600).
`pyt3 envs` lists them without the tokens. `pyt3 logout` forgets one; revoke it in
T3 Code under Settings -> Sharing & access.

## Access levels

`pair(..., access=...)` picks what the client may do. A thread the client starts
or steers can never run above this level. To narrow it further for one client, pass
`T3Client(..., mode_limit=("approval-required", "plan"))`.

| Level | Can |
|---|---|
| `read-only` | read projects, threads, providers, models |
| `approval-required` … `auto` | also start, message, wait for and stop threads |
| `full-access` | also create, update and delete projects |

Use `https`, `localhost` or Tailscale HTTPS addresses. Over plain `http` on a LAN,
the pairing code and token travel unencrypted.

## Development

See [CLAUDE.md](CLAUDE.md) for the upstream map: where each protocol detail lives
in the t3code repository, how the models are generated, and how to test against an
isolated server.
