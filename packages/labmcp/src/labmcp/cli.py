"""The ``labmcp`` command: discover servers, find ports, and generate client configs.

    labmcp list [--domain chemistry]
    labmcp info mettler-toledo
    labmcp ports
    labmcp config mettler-toledo --address /dev/ttyUSB0 --client claude-desktop
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from importlib import resources
from typing import Any

CLIENTS = ["claude-desktop", "claude-code", "cursor", "vscode", "codex", "json"]


def load_catalog() -> dict[str, Any]:
    try:
        text = resources.files("labmcp").joinpath("catalog.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"servers": []}
    return json.loads(text)


def find_server(name: str) -> dict[str, Any]:
    servers = load_catalog()["servers"]
    key = name.lower().removeprefix("labmcp-")
    for s in servers:
        if key in {s["package"].removeprefix("labmcp-"), s["path"].rsplit("/", 1)[-1]}:
            return s
    matches = [s for s in servers if key in s["package"] or key in s["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    hint = ", ".join(s["package"] for s in matches) if matches else "run `labmcp list`"
    sys.exit(f"No unique server matches {name!r} ({hint}).")


def cmd_list(args: argparse.Namespace) -> None:
    servers = load_catalog()["servers"]
    if args.domain:
        servers = [s for s in servers if s["domain"] == args.domain]
    if not servers:
        print("No servers found.")
        return
    width = max(len(s["package"]) for s in servers)
    current = None
    for s in sorted(servers, key=lambda s: (s["domain"], s["package"])):
        if s["domain"] != current:
            current = s["domain"]
            print(f"\n{current.upper()}")
        print(f"  {s['package']:<{width}}  {s['name']} - {s['summary']}")
    print("\nDetails: labmcp info <name>   Client config: labmcp config <name> --address <addr>")


def cmd_info(args: argparse.Namespace) -> None:
    s = find_server(args.name)
    print(f"{s['name']}  ({s['package']} {s['version']})")
    print(f"  {s['description']}")
    print(f"  Vendor:     {s['vendor']}")
    print(f"  Models:     {', '.join(s['models'])}")
    print(f"  Interfaces: {', '.join(s['interfaces'])}")
    print(f"  Protocol:   {s['protocol']}")
    print(f"  Status:     {s['status']}")
    print(f"  Docs:       https://github.com/K-Dense-AI/lab-instrument-mcps/tree/main/{s['path']}")
    print(f"  Tools ({len(s['tools'])}):")
    for t in s["tools"]:
        print(f"    [{t['kind']:<7}] {t['name']}: {t['description']}")


def cmd_ports(_args: argparse.Namespace) -> None:
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    print("Serial ports:")
    for p in ports:
        extra = " ".join(x for x in (p.manufacturer, p.product, p.serial_number) if x)
        print(f"  {p.device:<28} {p.description}" + (f"  [{extra}]" if extra else ""))
    if not ports:
        print("  (none found)")
    try:
        import pyvisa
    except ImportError:
        print("\nVISA: not installed (pip install \"labmcp[visa]\" to list GPIB/USBTMC/LAN instruments)")
        return
    print("\nVISA resources (pyvisa-py):")
    try:
        found = pyvisa.ResourceManager("@py").list_resources()
    except Exception as exc:  # pragma: no cover - depends on system
        print(f"  error: {exc}")
        return
    for r in found:
        print(f"  {r}")
    if not found:
        print("  (none found)")


def cmd_config(args: argparse.Namespace) -> None:
    s = find_server(args.name)
    key = args.key or s["package"].removeprefix("labmcp-")
    server_args = [s["package"]]
    if args.simulate:
        server_args.append("--simulate")
    if args.address:
        server_args += ["--address", args.address]
    if args.read_only:
        server_args.append("--read-only")
    for lim in args.limit or []:
        server_args += ["--limit", lim]
    entry = {"command": "uvx", "args": server_args}

    if args.client == "claude-code":
        print(f"claude mcp add {shlex.quote(key)} -- uvx {shlex.join(server_args)}")
    elif args.client == "codex":
        table = key.replace("-", "_")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", table):  # not a TOML bare key: quote it
            table = json.dumps(table)
        print(f"[mcp_servers.{table}]")
        print('command = "uvx"')
        print("args = " + json.dumps(server_args))
    elif args.client == "vscode":
        print(json.dumps({"servers": {key: {"type": "stdio", **entry}}}, indent=2))
    else:
        print(json.dumps({"mcpServers": {key: entry}}, indent=2))
    if not args.address and not args.simulate:
        print("\n# Tip: add --address <port/IP> (see `labmcp ports`) or --simulate to try it without hardware.", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="labmcp", description="LabMCP: MCP servers for lab instruments")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list available instrument servers")
    sp.add_argument("--domain", choices=["biology", "chemistry", "physics", "health", "engineering", "protocols"])
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("info", help="show details and tools for one server")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("ports", help="list serial ports and VISA instruments on this computer")
    sp.set_defaults(fn=cmd_ports)

    sp = sub.add_parser("config", help="print an MCP client configuration snippet")
    sp.add_argument("name")
    sp.add_argument("--client", choices=CLIENTS, default="claude-desktop")
    sp.add_argument("--address", help="instrument address")
    sp.add_argument("--simulate", action="store_true")
    sp.add_argument("--read-only", action="store_true")
    sp.add_argument("--limit", action="append", metavar="NAME=VALUE")
    sp.add_argument("--key", help="name for the server entry in the client config")
    sp.set_defaults(fn=cmd_config)

    args = p.parse_args(argv)
    # Catalog text has characters such as θ and ≈ that a legacy code page (Windows, when output
    # is piped or redirected) can't encode; print a replacement instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")
    args.fn(args)


if __name__ == "__main__":
    main()
