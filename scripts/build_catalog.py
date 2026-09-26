#!/usr/bin/env python3
"""Build the server catalog from each server's ``[tool.labmcp]`` metadata.

Outputs:
  * catalog.json                                  - machine-readable index (repo root)
  * packages/labmcp/src/labmcp/catalog.json       - bundled copy for the `labmcp` CLI
  * servers/<domain>/<server>/server.json         - MCP Registry manifest per server
  * README.md                                     - tables between CATALOG markers

Run from the repo root inside the workspace environment so servers can be imported
(their tool lists are read from the live FastMCP objects):

    uv run python scripts/build_catalog.py          # write
    uv run python scripts/build_catalog.py --check  # CI: fail if anything is stale
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
REPO = "K-Dense-AI/lab-instrument-mcps"
REGISTRY_NAMESPACE = "io.github.K-Dense-AI"
SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"

DOMAINS = {
    "biology": ("🧬", "Biology & Life Sciences"),
    "chemistry": ("⚗️", "Chemistry"),
    "physics": ("🔭", "Physics & Optics"),
    "health": ("🩺", "Health & Biosignals"),
    "engineering": ("⚙️", "Engineering & Data Acquisition"),
    "protocols": ("🔌", "Universal Protocols (many instruments)"),
}
STATUS = {
    "simulated": "🧪 simulated",
    "hardware-verified": "✅ hardware-verified",
    "stable": "🟢 stable",
}
REQUIRED = ["name", "domain", "category", "vendor", "models", "interfaces", "protocol", "summary", "status"]


def load_servers() -> list[dict]:
    servers = []
    for pyproject in sorted(ROOT.glob("servers/*/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        meta = data.get("tool", {}).get("labmcp")
        project = data["project"]
        where = pyproject.parent.relative_to(ROOT).as_posix()
        if not meta:
            sys.exit(f"{where}: missing [tool.labmcp] table")
        missing = [k for k in REQUIRED if k not in meta]
        if missing:
            sys.exit(f"{where}: [tool.labmcp] missing {missing}")
        if meta["domain"] not in DOMAINS:
            sys.exit(f"{where}: unknown domain {meta['domain']!r} (use one of {list(DOMAINS)})")
        if meta["domain"] != pyproject.parent.parent.name:
            sys.exit(f"{where}: domain {meta['domain']!r} does not match its folder")
        if meta["status"] not in STATUS:
            sys.exit(f"{where}: unknown status {meta['status']!r}")
        scripts = project.get("scripts", {})
        if len(scripts) != 1:
            sys.exit(f"{where}: expected exactly one [project.scripts] entry")
        (command, target), = scripts.items()
        module = target.split(":")[0]
        servers.append(
            {
                **{k: meta[k] for k in REQUIRED},
                "package": project["name"],
                "version": project["version"],
                "description": project["description"],
                "command": command,
                "module": module,
                "path": where,
                "extras": meta.get("extras", {}),
                "tools": list_tools(module, where),
            }
        )
    return servers


def list_tools(module: str, where: str) -> list[dict]:
    try:
        mod = importlib.import_module(module)
    except Exception as exc:
        sys.exit(f"{where}: cannot import {module}: {exc} (run inside `uv run` after `uv sync --all-packages`)")
    server = getattr(mod, "server", None)
    if server is None or not hasattr(server, "mcp"):
        sys.exit(f"{where}: {module} must define `server = InstrumentServer(...)`")
    tools = asyncio.run(server.mcp.list_tools())
    out = []
    for tool in sorted(tools, key=lambda t: t.name):
        tags = set(tool.tags or ())
        if not tags & {"read", "control", "safety"}:
            sys.exit(f"{where}: tool {tool.name!r} has no kind; decorate it with **READ, **CONTROL, **HAZARD or **SAFETY")
        kind = "hazard" if "hazard" in tags else "control" if "control" in tags else "safety" if "safety" in tags else "read"
        out.append({"name": tool.name, "kind": kind, "description": " ".join((tool.description or "").strip().split("\n\n")[0].split())})
    kinds = {t["kind"] for t in out}
    stops = [t for t in out if t["kind"] == "safety" and t["name"] != "reconnect"]
    if "hazard" in kinds and not stops:
        sys.exit(f"{where}: has HAZARD tools but no SAFETY stop/off tool (see docs/writing-a-server.md)")
    return out


def server_json(s: dict) -> dict:
    return {
        "$schema": SCHEMA,
        "name": f"{REGISTRY_NAMESPACE}/{s['package']}",
        "title": s["name"],
        "description": s["description"][:100],
        "version": s["version"],
        "repository": {"url": f"https://github.com/{REPO}", "source": "github", "subfolder": s["path"]},
        "websiteUrl": f"https://github.com/{REPO}/tree/main/{s['path']}",
        "packages": [
            {
                "registryType": "pypi",
                "registryBaseUrl": "https://pypi.org",
                "identifier": s["package"],
                "version": s["version"],
                "transport": {"type": "stdio"},
                "environmentVariables": [
                    {"name": "LABMCP_ADDRESS", "description": "Instrument address, e.g. serial:///dev/ttyUSB0 or tcp://192.168.1.50:5025", "isRequired": False},
                    {"name": "LABMCP_SIMULATE", "description": "Set to 1 to use the built-in simulator (no hardware)", "isRequired": False},
                    {"name": "LABMCP_READ_ONLY", "description": "Set to 1 to disable all state-changing tools", "isRequired": False},
                    {"name": "LABMCP_LIMITS", "description": "Safety limit overrides, e.g. max_temperature_c=80", "isRequired": False},
                ],
            }
        ],
    }


def readme_tables(servers: list[dict]) -> str:
    lines: list[str] = []
    for domain, (emoji, title) in DOMAINS.items():
        group = [s for s in servers if s["domain"] == domain]
        if not group:
            continue
        lines += [f"### {emoji} {title}", "", "| Server | Instruments | Interface | Tools | Status | Install |", "|---|---|---|---|---|---|"]
        for s in sorted(group, key=lambda s: s["name"].lower()):
            models = ", ".join(s["models"][:4]) + (" …" if len(s["models"]) > 4 else "")
            link = f"[**{cell(s['name'])}**]({s['path']})"
            lines.append(
                f"| {link}<br><sub>{cell(s['summary'])}</sub> | {cell(s['vendor'])}: {cell(models)} "
                f"| {cell(', '.join(s['interfaces']))} "
                f"| {len(s['tools'])} | {STATUS[s['status']]} | `uvx {s['package']}` |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


KIND_LABEL = {
    "read": "👁 read",
    "control": "🎛 control",
    "hazard": "⚠️ hazard",
    "safety": "🛑 safety",
}


def cell(text: str) -> str:
    """Escape text for a Markdown table cell (a bare ``|`` would start a new column)."""
    return text.replace("|", "\\|")


def tools_table(s: dict) -> str:
    lines = ["| Tool | Kind | Description |", "|---|---|---|"]
    for t in s["tools"]:
        lines.append(f"| `{t['name']}` | {KIND_LABEL[t['kind']]} | {cell(t['description'])} |")
    return "\n".join(lines) + "\n"


def replace_block(text: str, start: str, end: str, body: str, where: str = "README.md") -> str:
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        sys.exit(f"{where}: needs exactly one {start} followed by one {end}")
    a, b = text.index(start) + len(start), text.index(end)
    return text[:a] + "\n" + body + text[b:]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:  # LF on every OS, like the repo
        fh.write(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if generated files are out of date")
    args = ap.parse_args()

    servers = load_servers()
    catalog = {"schema": 1, "repository": f"https://github.com/{REPO}", "servers": servers}
    outputs: dict[Path, str] = {
        ROOT / "catalog.json": json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        ROOT / "packages/labmcp/src/labmcp/catalog.json": json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
    }
    for s in servers:
        outputs[ROOT / s["path"] / "server.json"] = json.dumps(server_json(s), indent=2, ensure_ascii=False) + "\n"
        server_readme = ROOT / s["path"] / "README.md"
        body = read(server_readme)
        if "<!-- TOOLS:START -->" not in body:
            sys.exit(f"{s['path']}/README.md: missing <!-- TOOLS:START --> / <!-- TOOLS:END --> markers")
        if f"mcp-name: {REGISTRY_NAMESPACE}/{s['package']}" not in body:
            sys.exit(f"{s['path']}/README.md: missing '<!-- mcp-name: {REGISTRY_NAMESPACE}/{s['package']} -->'")
        outputs[server_readme] = replace_block(
            body, "<!-- TOOLS:START -->", "<!-- TOOLS:END -->", tools_table(s), f"{s['path']}/README.md"
        )

    readme = ROOT / "README.md"
    text = read(readme)
    text = replace_block(text, "<!-- CATALOG:START -->", "<!-- CATALOG:END -->", readme_tables(servers))
    n_tools = sum(len(s["tools"]) for s in servers)
    text = replace_block(
        text,
        "<!-- COUNTS:START -->",
        "<!-- COUNTS:END -->",
        f"[![Servers](https://img.shields.io/badge/Servers-{len(servers)}-brightgreen.svg)](#-supported-instruments) "
        f"[![Tools](https://img.shields.io/badge/Tools-{n_tools}-blue.svg)](#-supported-instruments)\n",
    )
    outputs[readme] = text

    stale = [p for p, content in outputs.items() if not p.exists() or read(p) != content]
    if args.check:
        if stale:
            sys.exit("Out of date (run `uv run python scripts/build_catalog.py`):\n  " + "\n  ".join(str(p.relative_to(ROOT)) for p in stale))
        print(f"Catalog up to date: {len(servers)} servers, {n_tools} tools.")
        return
    for path in stale:
        write(path, outputs[path])
    print(f"Wrote catalog: {len(servers)} servers, {n_tools} tools ({len(stale)} files updated).")


if __name__ == "__main__":
    main()
