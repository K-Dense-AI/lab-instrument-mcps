"""MCP server bridging to any SiLA 2 server (lab-automation standard)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Annotated, Any

from labmcp import HAZARD, READ, SAFETY, ConnectContext, InstrumentConnectionError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_sila2.driver import (
    CANCEL_CONTROLLER,
    MAX_CALL_TIMEOUT_S,
    SilaBridge,
    discover,
    discover_client,
    open_client,
    parse_address,
    parse_allowlist,
    read_file,
    seconds_option,
)
from labmcp_sila2.fdl import to_json_schema
from labmcp_sila2.simulator import SIM_SERVER_UUID, SimulatedSilaServer

_TRUTHY = {"1", "true", "yes", "on"}
MAX_WAIT_S = 3600
# call_command / subscribe_property: the longest wait plus a server call on each side, with margin, so
# the MCP tool timeout never cuts a call short (which would lose the execution_id).
LONG_TOOL_TIMEOUT_S = MAX_WAIT_S + 2 * MAX_CALL_TIMEOUT_S + 60


def connect(ctx: ConnectContext) -> SilaBridge:
    try:
        allowlist = parse_allowlist(ctx.option("command_allowlist"))
    except ValueError as exc:
        raise InstrumentConnectionError(str(exc)) from exc
    call_timeout = seconds_option(ctx.option("call_timeout_s", "") or ctx.settings.timeout or 30.0, "call_timeout_s")
    if ctx.simulate:
        sim = SimulatedSilaServer().start()
        try:
            client = open_client(sim.host, sim.port, insecure=True, root_certs=None, private_key=None,
                                 cert_chain=None, timeout=15.0)
        except Exception:
            sim.stop()
            raise
        return SilaBridge(client, address=f"{sim.host}:{sim.port}", allowlist=allowlist, call_timeout=call_timeout,
                          audit=ctx.audit, on_close=sim.stop,
                          security="insecure (simulated server on 127.0.0.1, discovery off)")

    insecure = (ctx.option("insecure", "false") or "").lower() in _TRUTHY
    root = read_file(ctx.option("root_cert"), "root certificate")
    key = read_file(ctx.option("client_key"), "client private key")
    chain = read_file(ctx.option("client_cert"), "client certificate chain")
    if insecure and (root or key or chain):
        raise InstrumentConnectionError("insecure=true cannot be combined with root_cert/client_cert/client_key.")
    security = "insecure (no TLS)" if insecure else ("TLS, custom root certificate" if root else "TLS, system trust store")
    if ctx.address:
        host, port = parse_address(ctx.address)
        client = open_client(host, port, insecure=insecure, root_certs=root, private_key=key, cert_chain=chain,
                             timeout=seconds_option(ctx.option("connect_timeout_s", "15") or 15, "connect_timeout_s"))
        address = f"{host}:{port}"
    else:
        # No address: use SiLA Server Discovery (mDNS) to find the server by name or UUID.
        name, uuid = ctx.option("server_name"), ctx.option("server_uuid")
        if not (name or uuid):
            raise InstrumentConnectionError(
                "No SiLA server address. Pass --address host:port, or --option server_name=<name> / "
                "--option server_uuid=<uuid> to find it with SiLA Server Discovery (see the discover_servers tool)."
            )
        client = discover_client(
            server_name=name, server_uuid=uuid,
            discovery_timeout=seconds_option(ctx.option("discovery_timeout_s", "10") or 10, "discovery_timeout_s"),
            connect_timeout=seconds_option(ctx.option("connect_timeout_s", "15") or 15, "connect_timeout_s"),
            insecure=insecure, root_certs=root, private_key=key, cert_chain=chain,
        )
        address = f"{client.address}:{client.port} (discovered)"
    return SilaBridge(client, address=address, allowlist=allowlist, call_timeout=call_timeout, audit=ctx.audit,
                      security=security)


server = InstrumentServer(
    "SiLA 2 Bridge",
    connect=connect,
    package="labmcp-sila2",
    instructions="""
Bridges to a SiLA 2 server: a lab device or middleware (liquid handlers, incubators, plate readers,
robots, scheduling software) that describes itself in SiLA Feature Definitions.
- Start with `get_server_info` and `list_features`: commands, properties, parameter types, units and
  constraints all come from the server's own Feature Definitions (FDL). Never guess identifiers.
- SiLA commands can move robots, dispense liquid, heat, shake or open doors. `call_command` is a
  hazardous action: say which command you will run, with which parameters, and why.
- Parameters are validated against the FDL (types, ranges, sets, lengths, patterns) before sending.
- Observable (long-running) commands return an `execution_id` immediately: poll `get_command_status`,
  then read `get_command_result`. Don't start a second run while one is in progress.
- To stop a running command use `cancel_command` (works only if the server implements SiLA's
  CancelController feature; otherwise use the device's own stop command or physical stop button).
""",
    limits=[
        Limit("max_command_wait_s", 300, "s", "Longest call_command wait for an observable command"),
        Limit("max_subscription_duration_s", 120, "s", "Longest subscribe_property collection"),
        Limit("max_discovery_s", 30, "s", "Longest mDNS discovery scan"),
    ],
    address_help="""\
  192.168.1.40:50052              SiLA server (TLS; add --option root_cert=ca.pem for self-signed servers)
  sila://robot.lab.local:50052    same, URI form
  (none)                          --option server_name=<name> or server_uuid=<uuid>: find it via mDNS""",
    option_help={
        "insecure": "true: connect without TLS (only for servers started with --insecure, e.g. test servers)",
        "root_cert": "PEM file with the server's (self-signed) CA certificate",
        "client_cert / client_key": "PEM files for mutual TLS, if the server requires client certificates",
        "command_allowlist": "only these commands may be called, e.g. 'Shaker.Shake,Incubator.*'",
        "server_name / server_uuid": "without --address: connect to the discovered server with this name/UUID",
        "call_timeout_s": f"deadline for unobservable commands and property reads (default 30, max {MAX_CALL_TIMEOUT_S:g})",
        "connect_timeout_s / discovery_timeout_s": f"connection / discovery deadlines (default 15 / 10, max {MAX_CALL_TIMEOUT_S:g})",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _bridge() -> SilaBridge:
    return server.driver


def _address_for(record: dict[str, Any]) -> str:
    """A --address for a discovery record (IPv6 in brackets, as parse_address expects)."""
    if not record["addresses"] or record["port"] is None:
        return ""
    host = record["addresses"][0]
    return f"[{host}]:{record['port']}" if ":" in host else f"{host}:{record['port']}"


# ------------------------------------------------------------------ models


class DiscoveredServer(BaseModel):
    server_uuid: str
    server_name: str | None
    description: str | None
    version: str | None
    addresses: list[str]
    port: int | None
    advertises_ca_certificate: bool = Field(description="Server publishes its self-signed CA in the mDNS record")
    connect_with: str = Field(description="Suggested --address")


class DiscoveryResult(BaseModel):
    servers: list[DiscoveredServer]
    duration_s: float
    simulated: bool
    timestamp: str


class ServerInfo(BaseModel):
    server_name: str | None = None
    server_type: str | None = None
    server_uuid: str | None = None
    server_version: str | None = None
    server_description: str | None = None
    vendor_url: str | None = None
    address: str
    security: str
    features: list[str] = Field(description="Fully qualified identifiers of the implemented features")
    cancellation_supported: bool = Field(description=f"Server implements {CANCEL_CONTROLLER}")
    command_allowlist: list[str] | None = Field(description="Commands this bridge may call (None: all)")
    simulated: bool
    timestamp: str


class FeatureList(BaseModel):
    features: list[dict[str, Any]] = Field(
        description="Per feature: commands (parameters/responses as JSON-schema-like dicts), properties, errors"
    )
    timestamp: str


class PropertyValue(BaseModel):
    feature: str
    property: str
    observable: bool
    value: Any
    type: dict[str, Any] = Field(description="JSON-schema-like description incl. unit (x-unit) and constraints")
    timestamp: str


class PropertyUpdate(BaseModel):
    time: str
    value: Any


class PropertySeries(BaseModel):
    feature: str
    property: str
    mode: str = Field(description="subscription (observable property) or polling")
    n_updates: int
    updates: list[PropertyUpdate]
    stats: dict[str, float] | None = Field(description="min/max/mean/first/last for numeric values")
    timestamp: str


class CommandResult(BaseModel):
    feature: str
    command: str
    observable: bool
    execution_id: str | None = Field(default=None, description="For observable commands: use with get_command_status")
    status: str | None = None
    progress: float | None = None
    done: bool
    responses: Any = None
    error: str | None = None
    warnings: list[str]
    timestamp: str


class CommandStatus(BaseModel):
    execution_id: str
    feature: str
    command: str
    parameters: Any
    status: str = Field(description="waiting, running, finishedSuccessfully or finishedWithError")
    done: bool
    progress: float | None = Field(description="0-1, if the server reports it")
    estimated_remaining_s: float | None
    lifetime_of_execution_s: float | None = Field(description="How long the server keeps the result")
    latest_intermediate_response: Any = None
    started: str
    timestamp: str


class CommandOutcome(BaseModel):
    execution_id: str
    feature: str
    command: str
    status: str
    success: bool
    responses: Any
    error: str | None
    timestamp: str


class CancelResult(BaseModel):
    supported: bool
    execution_id: str | None
    message: str
    status_after: str | None = None
    timestamp: str


def _series_stats(values: list[Any]) -> dict[str, float] | None:
    nums = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    return {"min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums), "first": nums[0], "last": nums[-1]}


def _status_model(st: dict[str, Any]) -> CommandStatus:
    ex = st["execution"]
    return CommandStatus(
        execution_id=ex.execution_id, feature=ex.feature, command=ex.command, parameters=ex.parameters,
        status=st["status"], done=st["done"], progress=st["progress"], estimated_remaining_s=st["estimated_remaining_s"],
        lifetime_of_execution_s=st["lifetime_of_execution_s"], latest_intermediate_response=st["latest_intermediate"],
        started=_iso(ex.started), timestamp=_now(),
    )


FeatureName = Annotated[str, Field(min_length=1, max_length=255, description="Feature identifier, e.g. 'TemperatureController'")]
Metadata = Annotated[
    dict[str, Any] | None,
    Field(description="SiLA client metadata, e.g. {'LockController.LockIdentifier': '...'} (if the server requires it)"),
]


# ------------------------------------------------------------------ tools


@mcp.tool(**READ, timeout=180)  # timeout_s <= 120 plus the last record lookups and shutdown
def discover_servers(
    timeout_s: Annotated[float, Field(gt=0, le=120, description="How long to listen for mDNS announcements")] = 3.0,
) -> DiscoveryResult:
    """Find SiLA 2 servers on the local network with SiLA Server Discovery (mDNS `_sila._tcp`). Lists
    name, UUID, address and port of each without connecting. Does not need a configured server."""
    server.check("max_discovery_s", timeout_s, "discovery time")
    if server.settings.simulate:
        b = _bridge()
        host, _, port = b.address.partition(":")
        entry = DiscoveredServer(
            server_uuid=SIM_SERVER_UUID, server_name="LabMCP Simulated Thermoblock",
            description="Simulated SiLA 2 temperature controller (not on the network)", version="1.0.0",
            addresses=[host], port=int(port), advertises_ca_certificate=False, connect_with=b.address,
        )
        return DiscoveryResult(servers=[entry], duration_s=0.0, simulated=True, timestamp=_now())
    found = discover(timeout_s)
    servers = [DiscoveredServer(**d, connect_with=_address_for(d)) for d in found]
    return DiscoveryResult(servers=servers, duration_s=timeout_s, simulated=False, timestamp=_now())


@mcp.tool(**READ)
def get_server_info() -> ServerInfo:
    """Identify the connected SiLA server (name, type, UUID, version, vendor, description), list its
    features, and say whether commands can be cancelled and which commands this bridge may call."""
    info = _bridge().server_info()
    return ServerInfo(**info, simulated=server.settings.simulate, timestamp=_now())


@mcp.tool(**READ)
def list_features(
    feature: Annotated[str | None, Field(description="Only this feature (identifier); default all")] = None,
    include_fdl: Annotated[bool, Field(description="Also return the raw Feature Definition XML")] = False,
    include_core: Annotated[bool, Field(description="Include the SiLAService core feature")] = False,
) -> FeatureList:
    """Describe the server's features from their Feature Definitions: every command (observable or
    not, parameters, responses, intermediate responses, defined errors, and whether the allow-list
    permits calling it) and every property, with types rendered as JSON-schema-like dicts including
    units and constraints."""
    b = _bridge()
    if feature:
        irs = [b.feature(feature)]
    else:
        irs = [ir for ir in b.features.values() if include_core or ir.identifier != "SiLAService"]
    return FeatureList(features=[b.describe_feature(ir, include_fdl) for ir in irs], timestamp=_now())


@mcp.tool(**READ)
def get_property(feature: FeatureName, property: Annotated[str, Field(min_length=1, max_length=255)],
                 metadata: Metadata = None) -> PropertyValue:
    """Read the current value of a SiLA property (for observable properties: the first value of a
    subscription)."""
    b = _bridge()
    ir, value = b.get_property(feature, property, metadata)
    p = b._property(ir, property)
    return PropertyValue(feature=ir.identifier, property=p.identifier, observable=p.observable, value=value,
                         type=to_json_schema(p.type, ir), timestamp=_now())


@mcp.tool(**READ, timeout=LONG_TOOL_TIMEOUT_S)
def subscribe_property(
    feature: FeatureName,
    property: Annotated[str, Field(min_length=1, max_length=255)],
    duration_s: Annotated[float, Field(gt=0, le=MAX_WAIT_S, description="How long to collect updates")] = 10.0,
    max_updates: Annotated[int, Field(ge=1, le=10000, description="Stop after this many updates")] = 500,
    poll_interval_s: Annotated[float, Field(ge=0.1, le=60, description="Polling interval for unobservable properties")] = 1.0,
    metadata: Metadata = None,
) -> PropertySeries:
    """Collect a property's values for `duration_s` seconds (SiLA subscription for observable
    properties, polling otherwise) and summarise them (e.g. watch a temperature settle)."""
    server.check("max_subscription_duration_s", duration_s, "subscription duration")
    out = _bridge().subscribe_property(feature, property, duration_s, max_updates, poll_interval_s, metadata)
    values = [v for _, v in out["updates"]]
    return PropertySeries(
        feature=out["feature"].identifier, property=out["property"].identifier, mode=out["mode"],
        n_updates=len(values), updates=[PropertyUpdate(time=_iso(t), value=v) for t, v in out["updates"]],
        stats=_series_stats(values), timestamp=_now(),
    )


@mcp.tool(**HAZARD, timeout=LONG_TOOL_TIMEOUT_S)
def call_command(
    feature: FeatureName,
    command: Annotated[str, Field(min_length=1, max_length=255, description="Command identifier")],
    parameters: Annotated[
        dict[str, Any], Field(description="Parameter identifier -> value, as described by list_features")
    ] = {},  # noqa: B006 - pydantic copies defaults
    wait_s: Annotated[
        float, Field(ge=0, le=MAX_WAIT_S, description="Observable commands: wait up to this long for completion (0 = return at once)")
    ] = 0.0,
    metadata: Metadata = None,
) -> CommandResult:
    """Run a SiLA command. This may physically act on the device (move, dispense, heat, shake, open
    doors): tell the user what will happen first. Parameters are validated against the Feature
    Definition before anything is sent, and the command must be in the allow-list if one is set.
    Unobservable commands return their responses; observable ones return an `execution_id`."""
    if wait_s:
        server.check("max_command_wait_s", wait_s, "command wait")
    b = _bridge()
    out = b.call(feature, command, parameters, metadata)
    ir, cmd = out["feature"], out["command"]
    if not out["observable"]:
        return CommandResult(feature=ir.identifier, command=cmd.identifier, observable=False, done=True,
                             status="finishedSuccessfully", responses=out["responses"], warnings=out["warnings"],
                             timestamp=_now())
    exec_id = out["execution_id"]
    st = b.wait(exec_id, wait_s) if wait_s else b.status(exec_id)
    responses = error = None
    if st["done"]:
        res = b.result(exec_id)
        responses, error = res["responses"], res["error"]
    return CommandResult(
        feature=ir.identifier, command=cmd.identifier, observable=True, execution_id=exec_id, status=st["status"],
        progress=st["progress"], done=st["done"], responses=responses, error=error, warnings=out["warnings"],
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_command_status(execution_id: Annotated[str, Field(min_length=1, max_length=64)]) -> CommandStatus:
    """Status of an observable command started with `call_command`: waiting / running /
    finishedSuccessfully / finishedWithError, progress, estimated remaining time and the latest
    intermediate response."""
    return _status_model(_bridge().status(execution_id))


@mcp.tool(**READ)
def get_command_result(execution_id: Annotated[str, Field(min_length=1, max_length=64)]) -> CommandOutcome:
    """Responses of a finished observable command, or the SiLA execution error it finished with.
    Fails with a clear message if the command is still running."""
    res = _bridge().result(execution_id)
    ex = res["execution"]
    return CommandOutcome(execution_id=ex.execution_id, feature=ex.feature, command=ex.command, status=res["status"],
                          success=res["success"], responses=res["responses"], error=res["error"], timestamp=_now())


@mcp.tool(**READ)
def list_executions() -> list[CommandStatus]:
    """All observable command executions started in this session, newest last, with their status."""
    b = _bridge()
    with b.lock:
        ids = list(b.executions)
    return [_status_model(b.status(i)) for i in ids]


@mcp.tool(**SAFETY)
def cancel_command(
    execution_id: Annotated[
        str | None, Field(description="Execution to cancel; omit (or all_commands=true) to cancel everything")
    ] = None,
    all_commands: Annotated[bool, Field(description="Cancel every running command on the server")] = False,
) -> CancelResult:
    """Cancel a running observable command, or all commands, through SiLA's CancelController feature.
    Always available (even read-only). If the server has no CancelController, this explains that and
    points to the device's own stop command."""
    b = _bridge()
    target = None if all_commands or not execution_id else execution_id
    out = b.cancel(target)
    after = None
    if out["supported"] and target is not None:
        with contextlib.suppress(Exception):
            after = b.wait(target, 5.0)["status"]
    return CancelResult(supported=out["supported"], execution_id=target, message=out["message"], status_after=after,
                        timestamp=_now())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
