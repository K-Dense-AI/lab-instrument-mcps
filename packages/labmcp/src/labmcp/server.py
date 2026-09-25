"""``InstrumentServer``: the scaffolding every LabMCP server is built on.

It wraps a :class:`fastmcp.FastMCP` instance and adds what every instrument
server needs:

* a lazily-opened, thread-safe driver connection (the MCP client can start the
  server before the instrument is switched on),
* a ``--simulate`` mode backed by a wire-level simulator,
* ``--read-only`` mode that hides every state-changing tool,
* configurable safety limits and a command audit trail,
* three built-in tools (``get_connection_info``, ``get_command_log``, ``reconnect``),
* a consistent command line shared by all servers.

Instrument tools are ordinary FastMCP tools. Tag them with one of the kinds
below so annotations, read-only mode and documentation stay consistent::

    from labmcp import READ, HAZARD, InstrumentServer

    server = InstrumentServer("My Balance", connect=connect)
    mcp = server.mcp

    @mcp.tool(**READ)
    def read_weight() -> Weight:
        return server.driver.read_weight()
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Generic, TypeVar

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from labmcp.audit import AuditLog
from labmcp.errors import InstrumentConnectionError, InstrumentError
from labmcp.safety import Limit, SafetyLimits, parse_limit_args
from labmcp.transports import SimulatedTransport, Transport, open_transport
from labmcp.transports.sim import ByteSimulator, LineSimulator

log = logging.getLogger("labmcp")

D = TypeVar("D")

# --------------------------------------------------------------------------
# Tool kinds. Use as ``@mcp.tool(**READ)``.
# --------------------------------------------------------------------------

#: Reads state or data; never changes the instrument. Always available.
READ: dict[str, Any] = {
    "annotations": ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "tags": {"read"},
}

#: Changes a setting with no direct physical hazard (units, tare, display, zero).
#: Hidden in ``--read-only`` mode.
CONTROL: dict[str, Any] = {
    "annotations": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=False
    ),
    "tags": {"control"},
}

#: Physically acts on the world: heats, moves, dispenses, energises outputs,
#: consumes sample. MCP clients should ask the user before running these.
#: Hidden in ``--read-only`` mode.
HAZARD: dict[str, Any] = {
    "annotations": ToolAnnotations(
        read_only_hint=False, destructive_hint=True, open_world_hint=False
    ),
    "tags": {"control", "hazard"},
}

#: Makes the instrument safer (stop, abort, outputs off). Always available,
#: even in read-only mode, and never gated behind confirmation.
SAFETY: dict[str, Any] = {
    "annotations": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "tags": {"safety"},
}

_COMMON_INSTRUCTIONS = """\
This MCP server controls a PHYSICAL laboratory instrument through LabMCP.
- Call `get_connection_info` first: it tells you which instrument is connected, whether it is
  SIMULATED, whether the server is read-only, and which safety limits apply.
- Never present simulated data as a real measurement.
- Field names carry units (e.g. `temperature_c`, `mass_g`, `flow_ml_min`). Convert carefully.
- Tools annotated as destructive physically act on the instrument (heat, move, dispense,
  energise). Tell the user what you are about to do and why before calling them.
- If a safety limit refuses a request, do not work around it - report it to the user.
- If anything looks wrong, call the stop/abort/off tool immediately.
"""


@dataclass
class Settings:
    """Runtime configuration, populated from the CLI and ``LABMCP_*`` environment variables."""

    address: str | None = None
    simulate: bool = False
    read_only: bool = False
    timeout: float | None = None
    audit_log: str | None = None
    limits: dict[str, float] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class ConnectContext:
    """Passed to a server's ``connect`` function."""

    settings: Settings
    audit: AuditLog
    limits: SafetyLimits

    @property
    def simulate(self) -> bool:
        return self.settings.simulate

    @property
    def address(self) -> str | None:
        return self.settings.address

    def option(self, name: str, default: str | None = None) -> str | None:
        """A driver-specific ``--option name=value``."""
        return self.settings.options.get(name, default)

    def require_address(self) -> str:
        if not self.settings.address:
            raise InstrumentConnectionError(
                "No instrument address configured. Start the server with `--address <address>` "
                "(or set LABMCP_ADDRESS), or use `--simulate` to try it without hardware."
            )
        return self.settings.address

    def open_transport(
        self,
        *,
        simulator: Callable[[], LineSimulator | ByteSimulator] | None = None,
        **defaults: Any,
    ) -> Transport:
        """Open the configured address, or a simulated transport in ``--simulate`` mode.

        ``defaults`` are the instrument's factory communication settings (baudrate,
        read/write termination, ...). Query parameters in the address override them.
        """
        if self.settings.timeout is not None:
            defaults["timeout"] = self.settings.timeout
        if self.simulate:
            if simulator is None:
                raise InstrumentConnectionError("This server has no simulator.")
            common = {
                k: v
                for k, v in defaults.items()
                if k in {"read_termination", "write_termination", "encoding", "timeout"}
            }
            return SimulatedTransport(simulator(), audit=self.audit, **common)
        return open_transport(self.require_address(), audit=self.audit, **defaults)


class InstrumentServer(Generic[D]):
    """An MCP server for one instrument (or one family of compatible instruments).

    Args:
        name: Display name, e.g. ``"Mettler Toledo Balance (MT-SICS)"``.
        connect: ``connect(ctx) -> driver``. Must honour ``ctx.simulate``.
        instructions: Instrument-specific guidance for the model. Shared safety
            guidance is appended automatically.
        limits: Safety limits the scientist can override with ``--limit``.
        package: Distribution name, used for ``--version``.
        address_help: Example addresses shown in ``--help``.
        option_help: Driver-specific ``--option`` names and descriptions.
        connect_on_start: Connect when the server starts rather than on the first tool
            call (for instruments that push data, e.g. an analyzer sending results). A
            failure is logged and retried on the next tool call.
    """

    def __init__(
        self,
        name: str,
        *,
        connect: Callable[[ConnectContext], D],
        instructions: str = "",
        limits: list[Limit] | tuple[Limit, ...] = (),
        package: str | None = None,
        address_help: str = "",
        option_help: dict[str, str] | None = None,
        connect_on_start: bool = False,
    ) -> None:
        self.name = name
        self.package = package
        self.address_help = address_help
        self.option_help = option_help or {}
        self._connect = connect
        self.connect_on_start = connect_on_start
        self._limit_defs = tuple(limits)
        self._instructions = instructions.strip()
        self.settings = Settings()
        self.limits = SafetyLimits(self._limit_defs)
        self.audit = AuditLog()
        self._driver: D | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None

        self.mcp = FastMCP(name, instructions=self._build_instructions(), lifespan=self._lifespan)
        self._register_builtin_tools()

    # ---------------------------------------------------------------- driver

    @property
    def driver(self) -> D:
        """The connected driver. Connects on first use; raises a helpful error if it can't."""
        if self._driver is not None:
            return self._driver
        with self._lock:
            if self._driver is None:
                ctx = ConnectContext(self.settings, self.audit, self.limits)
                try:
                    self._driver = self._connect(ctx)
                    self._last_error = None
                    where = "simulator" if self.settings.simulate else self.settings.address
                    self.audit.event(f"connected to {where}", "labmcp")
                except InstrumentError as exc:
                    self._last_error = str(exc)
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    raise InstrumentConnectionError(
                        f"Could not connect to {self.name} at {self.settings.address!r}: "
                        f"{self._last_error}"
                    ) from exc
        return self._driver

    @property
    def connected(self) -> bool:
        return self._driver is not None

    def check(self, limit: str, value: float, what: str | None = None) -> float:
        """Shortcut for ``server.limits.check(...)``."""
        return self.limits.check(limit, value, what)

    def disconnect(self) -> None:
        with self._lock:
            driver, self._driver = self._driver, None
        if driver is not None:
            close = getattr(driver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # pragma: no cover - best effort
                    log.warning("Error while closing driver: %s", exc)
            self.audit.event("disconnected", "labmcp")

    # ------------------------------------------------------------ configure

    def configure(
        self,
        *,
        address: str | None = None,
        simulate: bool | None = None,
        read_only: bool | None = None,
        timeout: float | None = None,
        audit_log: str | None = None,
        limits: dict[str, float] | None = None,
        options: dict[str, str] | None = None,
    ) -> InstrumentServer[D]:
        """Apply settings programmatically (tests, notebooks, embedding)."""
        self.disconnect()
        s = self.settings
        if address is not None:
            s.address = address
        if simulate is not None:
            s.simulate = simulate
        if timeout is not None:
            s.timeout = timeout
        if options is not None:
            s.options = dict(options)
        if audit_log is not None:
            s.audit_log = audit_log
            self.audit = AuditLog(audit_log)
        s.limits = dict(limits or {})
        self.limits = SafetyLimits(self._limit_defs)
        self.limits.override(s.limits)
        if read_only is not None and read_only != s.read_only:
            s.read_only = read_only
            if read_only:
                self.mcp.disable(tags={"control"})
            else:
                self.mcp.enable(tags={"control"})
        self.mcp.instructions = self._build_instructions()
        return self

    def configure_from_env(self, environ: dict[str, str] | None = None) -> InstrumentServer[D]:
        env = os.environ if environ is None else environ
        truthy = {"1", "true", "yes", "on"}
        raw_opts = env.get("LABMCP_OPTIONS", "").strip()
        if raw_opts.startswith("{"):  # JSON, for values that contain commas (e.g. regexes)
            try:
                opts = {str(k): str(v) for k, v in json.loads(raw_opts).items()}
            except (json.JSONDecodeError, AttributeError) as exc:
                raise ValueError(f"LABMCP_OPTIONS is not a valid JSON object: {exc}") from exc
        else:
            opts = dict(item.split("=", 1) for item in raw_opts.split(",") if "=" in item)
        try:
            timeout = float(env["LABMCP_TIMEOUT"]) if env.get("LABMCP_TIMEOUT") else None
        except ValueError as exc:
            raise ValueError(f"LABMCP_TIMEOUT must be a number of seconds: {exc}") from exc
        return self.configure(
            address=env.get("LABMCP_ADDRESS") or None,
            simulate=env.get("LABMCP_SIMULATE", "").lower() in truthy,
            read_only=env.get("LABMCP_READ_ONLY", "").lower() in truthy,
            timeout=timeout,
            audit_log=env.get("LABMCP_AUDIT_LOG") or None,
            limits=parse_limit_args([env.get("LABMCP_LIMITS", "")]),
            options={k.strip(): v.strip() for k, v in opts.items()},
        )

    # -------------------------------------------------------------- running

    def run(self, argv: list[str] | None = None) -> None:
        """Parse the command line and serve over stdio (default) or HTTP."""
        parser = self._build_parser()
        args = parser.parse_args(argv)
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            self.configure_from_env()
            s = self.settings
            self.configure(
                address=args.address if args.address is not None else s.address,
                simulate=args.simulate or s.simulate,
                read_only=args.read_only or s.read_only,
                timeout=args.timeout if args.timeout is not None else s.timeout,
                audit_log=args.audit_log or s.audit_log,
                limits={**s.limits, **parse_limit_args(args.limit)},
                options={**s.options, **_parse_kv(args.option)},
            )
        except ValueError as exc:
            parser.error(str(exc))

        if args.check:
            sys.exit(self._run_check())

        if args.transport == "http":
            self.mcp.run(transport="http", host=args.host, port=args.port)
        else:
            self.mcp.run()

    main = run

    def _run_check(self) -> int:
        info = self._connection_info()
        print(json.dumps(info, indent=2, default=str))
        self.disconnect()
        return 0 if info.get("connected") and "error" not in info else 1

    # ------------------------------------------------------------- internal

    @asynccontextmanager
    async def _lifespan(self, _server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        if self.connect_on_start:
            try:
                await asyncio.to_thread(lambda: self.driver)
            except InstrumentError as exc:
                log.warning("Could not connect at startup (%s); will retry on the next tool call.", exc)
        try:
            yield {"instrument": self}
        finally:
            self.disconnect()

    def _build_instructions(self) -> str:
        parts = []
        if self.settings.simulate:
            parts.append(
                "SIMULATION MODE: no hardware is connected. All data is synthetic and must be "
                "labelled as simulated."
            )
        if self.settings.read_only:
            parts.append("READ-ONLY MODE: state-changing tools are disabled.")
        if self._instructions:
            parts.append(self._instructions)
        parts.append(_COMMON_INSTRUCTIONS)
        return "\n\n".join(parts)

    def _connection_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "server": self.name,
            "version": self._version(),
            "simulated": self.settings.simulate,
            "address": None if self.settings.simulate else self.settings.address,
            "read_only": self.settings.read_only,
            "safety_limits": self.limits.as_dict(),
        }
        try:
            driver = self.driver
            info["connected"] = True
            identify = getattr(driver, "identify", None)
            if callable(identify):
                info["instrument"] = identify()
        except Exception as exc:
            info["connected"] = self.connected
            info["error"] = str(exc) if isinstance(exc, InstrumentError) else f"{type(exc).__name__}: {exc}"
        return info

    def _register_builtin_tools(self) -> None:
        mcp = self.mcp

        @mcp.tool(**READ)
        def get_connection_info() -> dict[str, Any]:
            """Report which instrument is connected (identity, address, simulated or real),
            whether the server is read-only, and the active safety limits. Call this first."""
            return self._connection_info()

        @mcp.tool(**READ)
        def get_command_log(limit: int = 20) -> list[dict[str, Any]]:
            """Return the most recent raw commands sent to / replies received from the
            instrument (newest last). Useful for debugging and for recording what was done."""
            return self.audit.recent(max(1, min(limit, 500)))

        @mcp.tool(**SAFETY)
        def reconnect() -> dict[str, Any]:
            """Close and re-open the connection to the instrument (e.g. after it was power
            cycled or a cable was re-plugged)."""
            self.disconnect()
            return self._connection_info()

    def _version(self) -> str | None:
        if not self.package:
            return None
        try:
            return pkg_version(self.package)
        except PackageNotFoundError:
            return None

    def _build_parser(self) -> argparse.ArgumentParser:
        prog = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else self.package
        epilog = []
        if self.address_help:
            epilog.append("addresses:\n" + self.address_help.rstrip())
        if self._limit_defs:
            lines = [
                f"  {lim.name} (default {lim.default:g} {lim.unit}): {lim.description}"
                for lim in self._limit_defs
            ]
            epilog.append("safety limits (--limit name=value):\n" + "\n".join(lines))
        if self.option_help:
            lines = [f"  {k}: {v}" for k, v in self.option_help.items()]
            epilog.append("driver options (--option name=value):\n" + "\n".join(lines))
        epilog.append(
            "environment variables: LABMCP_ADDRESS, LABMCP_SIMULATE, LABMCP_READ_ONLY, "
            "LABMCP_TIMEOUT, LABMCP_AUDIT_LOG, LABMCP_LIMITS (a=1,b=2), LABMCP_OPTIONS (a=1,b=2 or JSON)"
        )
        p = argparse.ArgumentParser(
            prog=prog,
            description=f"LabMCP server: {self.name}",
            epilog="\n\n".join(epilog),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("-a", "--address", help="instrument address (see below)")
        p.add_argument("--simulate", action="store_true", help="use the built-in simulator")
        p.add_argument("--read-only", action="store_true", help="disable all state-changing tools")
        p.add_argument("--limit", action="append", metavar="NAME=VALUE", help="override a safety limit")
        p.add_argument("--option", action="append", metavar="NAME=VALUE", help="driver-specific option")
        p.add_argument("--timeout", type=float, help="reply timeout in seconds")
        p.add_argument("--audit-log", metavar="PATH", help="append all instrument traffic to a JSONL file")
        p.add_argument("--check", action="store_true", help="connect, print instrument info, and exit")
        p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
        p.add_argument("--host", default="127.0.0.1", help="HTTP host (with --transport http)")
        p.add_argument("--port", type=int, default=8000, help="HTTP port (with --transport http)")
        p.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
        p.add_argument("--version", action="version", version=f"%(prog)s {self._version() or 'dev'}")
        return p


def _parse_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--option must look like name=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out
