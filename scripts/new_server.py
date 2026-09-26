#!/usr/bin/env python3
"""Scaffold a new LabMCP server.

    uv run python scripts/new_server.py --domain chemistry --slug ika-stirrer \\
        --package labmcp-ika --name "IKA Hotplate Stirrer" --vendor IKA

See docs/writing-a-server.md for what to do next.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAINS = ["biology", "chemistry", "physics", "health", "engineering", "protocols"]

PYPROJECT = '''\
[project]
name = "{package}"
version = "0.1.0"
description = "MCP server for {name}: TODO one-line description."
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.10"
authors = [{{ name = "K-Dense and LabMCP contributors" }}]
keywords = ["mcp", "lab-instrument", "TODO"]
dependencies = ["labmcp>=0.1,<0.2"]

[project.scripts]
{package} = "{module}.server:main"

[project.urls]
Homepage = "https://github.com/K-Dense-AI/lab-instrument-mcps/tree/main/servers/{domain}/{slug}"

[tool.labmcp]
name = "{name}"
domain = "{domain}"
category = "TODO"
vendor = "{vendor}"
models = ["TODO"]
interfaces = ["RS-232"]
protocol = "TODO"
summary = "TODO: what an agent can do with it, in one line."
status = "simulated"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{module}"]
'''

DRIVER = '''\
"""Driver for {name}.

Protocol reference: TODO (manual title, document number, URL).
"""

from __future__ import annotations

from labmcp import InstrumentProtocolError, Transport


class {cls}:
    def __init__(self, transport: Transport) -> None:
        self.t = transport

    def command(self, cmd: str) -> str:
        reply = self.t.query(cmd).strip()
        if reply.startswith("ERR"):  # TODO: real error format
            raise InstrumentProtocolError(f"Instrument rejected {{cmd!r}}: {{reply}}")
        return reply

    def identify(self) -> dict[str, str]:
        return {{"manufacturer": "{vendor}", "model": self.command("TODO_ID?")}}

    def close(self) -> None:
        self.t.close()
'''

SIMULATOR = '''\
"""Wire-level simulator for {name}."""

from __future__ import annotations

from labmcp import LineSimulator


class {cls}Simulator(LineSimulator):
    def __init__(self) -> None:
        self.state: dict[str, float] = {{}}

    def handle(self, command: str) -> str | list[str] | None:
        if command == "TODO_ID?":
            return "SIM-MODEL"
        return "ERR unknown command"
'''

SERVER = '''\
"""MCP server for {name}."""

from __future__ import annotations

from labmcp import READ, ConnectContext, InstrumentServer
from {module}.driver import {cls}
from {module}.simulator import {cls}Simulator


def connect(ctx: ConnectContext) -> {cls}:
    transport = ctx.open_transport(
        simulator={cls}Simulator,
        baudrate=9600,
        read_termination="\\r\\n",
        write_termination="\\r\\n",
    )
    return {cls}(transport)


server = InstrumentServer(
    "{name}",
    connect=connect,
    package="{package}",
    instructions="""
TODO: 3-8 bullet points of instrument-specific guidance for the model.
""",
    address_help="""\\
  serial:///dev/ttyUSB0      TODO default settings""",
)
mcp = server.mcp


@mcp.tool(**READ)
def get_identity() -> dict[str, str]:
    """TODO: replace with real tools. See docs/writing-a-server.md."""
    return server.driver.identify()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
'''

TEST = '''\
from labmcp.testing import simulated_client, tool_names
from {module}.server import server


async def test_connection_info():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {{}})).data
        assert info["connected"] is True


async def test_read_only():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert "get_connection_info" in names
'''

README = '''\
# {name}: MCP Server

<!-- mcp-name: io.github.K-Dense-AI/{package} -->

TODO: one paragraph on what an agent can do with this instrument.

| | |
|---|---|
| **Package** | `{package}` |
| **Instruments** | TODO |
| **Interfaces** | TODO |
| **Protocol** | TODO (link to manual) |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx {package} --simulate --check
```

## Connect your instrument

TODO: instrument-side setup, then:

```bash
uvx {package} --address /dev/ttyUSB0 --check
```

## Add to your MCP client

```bash
claude mcp add {slug} -- uvx {package} --address /dev/ttyUSB0
```

```json
{{
  "mcpServers": {{
    "{slug}": {{ "command": "uvx", "args": ["{package}", "--address", "/dev/ttyUSB0"] }}
  }}
}}
```

## Tools

<!-- TOOLS:START -->
<!-- TOOLS:END -->

## Safety limits

TODO

## Example prompts

- TODO

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet* | | | | |
'''


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, choices=DOMAINS)
    ap.add_argument("--slug", required=True, help="folder name, e.g. ika-stirrer")
    ap.add_argument("--package", required=True, help="PyPI name, must start with labmcp-")
    ap.add_argument("--name", required=True, help='display name, e.g. "IKA Hotplate Stirrer"')
    ap.add_argument("--vendor", required=True)
    a = ap.parse_args()

    if not re.fullmatch(r"labmcp(-[a-z0-9]+)+", a.package):
        sys.exit("--package must start with 'labmcp-' and be lower-case-with-dashes (it becomes a module name)")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", a.slug):
        sys.exit("--slug must be lower-case-with-dashes")
    for opt, value in (("--name", a.name), ("--vendor", a.vendor)):
        if any(ch in value for ch in '"\\{}') or not value.strip():  # pasted into TOML and Python strings
            sys.exit(f"{opt} must be non-empty and must not contain quotes, backslashes or braces")
    module = a.package.replace("-", "_")
    cls = "".join(p.capitalize() for p in a.package.removeprefix("labmcp-").split("-")) + "Driver"
    if not cls.isidentifier():  # e.g. labmcp-3d-printer -> "3dPrinterDriver"
        cls = "Instrument" + cls
    dest = ROOT / "servers" / a.domain / a.slug
    if dest.exists():
        sys.exit(f"{dest} already exists")

    ctx = {"package": a.package, "module": module, "cls": cls, "name": a.name, "vendor": a.vendor,
           "domain": a.domain, "slug": a.slug}
    files = {
        "pyproject.toml": PYPROJECT,
        "README.md": README,
        f"src/{module}/__init__.py": f'"""LabMCP server for {a.name}."""\n',
        f"src/{module}/driver.py": DRIVER,
        f"src/{module}/simulator.py": SIMULATOR,
        f"src/{module}/server.py": SERVER,
        "tests/test_server.py": TEST,
    }
    for rel, template in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 on every OS (the README template has emoji; Windows would default to cp1252).
        path.write_text(template.format(**ctx) if "{" in template else template, encoding="utf-8", newline="\n")
    print(f"Created {dest.relative_to(ROOT)}\nNext: uv sync --all-packages && uv run pytest {dest.relative_to(ROOT)}")
    print("Then follow docs/writing-a-server.md.")


if __name__ == "__main__":
    main()
