"""MCP server for EPICS Channel Access process variables."""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Any

import numpy as np
from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentServer,
    InstrumentTimeout,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_epics.driver import EpicsClient, PVReading, ca_environment, parse_safe_state
from labmcp_epics.simulator import SimulatedIOC

_TRUTHY = {"1", "true", "yes", "on"}
SIM_SAFE_STATE = "{p}MTR:STOP=1;{p}HEATER=Off"
#: put_pv / put_pvs tool timeout. FastMCP can't stop the worker thread when it fires, so every
#: put-callback wait of one call must end within _PUT_BUDGET_S, leaving time for the read-backs:
#: otherwise a batch would keep writing PVs after the agent was told the call timed out.
_PUT_TOOL_TIMEOUT_S = 3700
_PUT_BUDGET_S = 3600.0
_MIN_PUT_WAIT_S = 0.5


def connect(ctx: ConnectContext) -> EpicsClient:
    timeout = ctx.settings.timeout or 2.0
    allow = ctx.option("put_allowlist")
    if allow:
        try:
            re.compile(allow)
        except re.error as exc:
            raise InstrumentConnectionError(f"--option put_allowlist is not a valid regex: {exc}") from exc
    require = (ctx.option("require_ctrl_limits", "false") or "").lower() in _TRUTHY
    limit_writes = (ctx.option("allow_limit_field_writes", "false") or "").lower() in _TRUTHY
    try:
        parse_safe_state(ctx.option("safe_state"))
    except ValueError as exc:
        raise InstrumentConnectionError(str(exc)) from exc
    if ctx.simulate:
        prefix = ctx.option("sim_prefix", "SIM:") or "SIM:"
        ioc = SimulatedIOC(prefix).start()
        return EpicsClient(
            env=ioc.client_env, timeout=timeout, put_allowlist=allow, require_ctrl_limits=require,
            allow_limit_field_writes=limit_writes, audit=ctx.audit, on_close=ioc.stop,
            check_pv=ctx.option("check_pv", f"{prefix}TEMP"),
            description=f"simulated IOC on 127.0.0.1:{ioc.port}, PV prefix {prefix!r} (localhost only)",
        )
    return EpicsClient(
        env=ca_environment(ctx.address, ctx.settings.options), timeout=timeout, put_allowlist=allow,
        require_ctrl_limits=require, allow_limit_field_writes=limit_writes, audit=ctx.audit,
        check_pv=ctx.option("check_pv"),
    )


server = InstrumentServer(
    "EPICS Channel Access",
    connect=connect,
    package="labmcp-epics",
    instructions="""
Reads and (optionally) writes EPICS process variables (PVs) over Channel Access, the control
system of most accelerators, synchrotron/neutron beamlines, telescopes and many physics labs.
- Facility control systems are SAFETY-CRITICAL. Only write PVs the user has explicitly asked you to
  change, state the PV, old value and new value before writing, and never loop writes.
- Always read a PV (`get_pv`) before writing it: check units, alarm severity, control limits and
  whether it is the setpoint or the readback (e.g. MTR vs MTR.RBV / :RBV).
- Alarm severity MAJOR or INVALID means something is wrong: report it instead of acting on it.
- `put_pv` waits for the IOC's put-completion (e.g. a motor finishing its move). If it times out,
  read the readback before doing anything else - never repeat the write blindly.
- Writes outside the PV's control limits (DRVL/DRVH) are refused by this server: the IOC would
  silently clip them. If a write is refused by the allow-list or access security, report it.
- In an emergency call `apply_safe_state` (if configured) and tell the user to use the facility's
  own stop/interlock procedures.
""",
    limits=[
        Limit("max_monitor_duration_s", 60, "s", "Longest monitor_pv collection"),
        Limit("max_put_batch", 10, "PVs", "Most PVs one put_pvs call may write"),
        Limit("max_put_wait_s", 60, "s", "Longest wait for a put-callback (completion) per write"),
    ],
    address_help="""\
  (none)                        use EPICS_CA_ADDR_LIST / EPICS_CA_AUTO_ADDR_LIST from the environment
  10.0.1.20                     shorthand for --option ca_addr_list=10.0.1.20 (auto address list off)
  "10.0.1.20 10.0.1.21:5064"    several IOCs or a CA gateway (space or comma separated)""",
    option_help={
        "ca_addr_list": "EPICS_CA_ADDR_LIST (space/comma separated host[:port]); disables the auto list",
        "auto_addr_list": "EPICS_CA_AUTO_ADDR_LIST yes/no",
        "server_port": "EPICS_CA_SERVER_PORT (default 5064)",
        "put_allowlist": "regex a PV name must fully match to be written, e.g. 'BL7:(SLIT|FILTER):[^.]*'",
        "require_ctrl_limits": "true: refuse numeric writes to PVs without control limits (DRVL/DRVH)",
        "allow_limit_field_writes": "true: allow writing limit fields (.DRVH/.DRVL/.HOPR/.LOPR/.HLM/.LLM/.DHLM/.DLLM)",
        "safe_state": "writes for apply_safe_state, e.g. 'BL7:MTR1.STOP=1;BL7:SHUTTER=Close'",
        "check_pv": "PV that --check / get_connection_info reads to prove connectivity",
        "sim_prefix": "PV prefix of the simulated IOC (default SIM:)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _finite(v: float) -> float | None:
    """JSON has no NaN/inf: pydantic writes them as null, which fails a `number` output schema."""
    return v if math.isfinite(v) else None


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float, np.ndarray)) and not isinstance(v, bool)


def _as_number(v: Any) -> float:
    """A monitor update as one number (arrays: their mean; an empty array: NaN)."""
    if isinstance(v, np.ndarray):
        return float(v.astype(float).mean()) if v.size else math.nan
    return float(v)


def _scalar(v: Any) -> Any:
    """A NaN/inf scalar reading as text ('nan', 'inf', '-inf') rather than an ambiguous null."""
    return str(v) if isinstance(v, float) and not math.isfinite(v) else v


# ------------------------------------------------------------------ models


class Range(BaseModel):
    low: float | None = Field(description="None: no limit on this side")
    high: float | None = Field(description="None: no limit on this side")


class ArraySummary(BaseModel):
    length: int
    non_finite: int = Field(0, description="Elements that are NaN or infinite (left out of the statistics)")
    minimum: float | None
    maximum: float | None
    mean: float | None
    std: float | None
    index_of_max: int | None


class PVValue(BaseModel):
    name: str
    value: Any = Field(description="Scalar, string, enum state name, or (possibly downsampled) array")
    native_type: str = Field(description="CA native type: DOUBLE, FLOAT, LONG, INT, CHAR, ENUM or STRING")
    element_count: int
    units: str | None = None
    precision: int | None = None
    severity: str = Field(description="Alarm severity: NO_ALARM, MINOR_ALARM, MAJOR_ALARM or INVALID_ALARM")
    status: str = Field(description="Alarm status, e.g. NO_ALARM, HIHI, HIGH, LOW, LOLO, UDF, COMM, TIMEOUT")
    timestamp: str | None = Field(description="IOC timestamp of the value (UTC)")
    age_s: float | None = Field(description="Seconds since the IOC timestamp (large = stale value)")
    enum_strings: list[str] | None = None
    display_limits: Range | None = None
    alarm_limits: Range | None = Field(default=None, description="LOLO/HIHI")
    warning_limits: Range | None = Field(default=None, description="LOW/HIGH")
    control_limits: Range | None = Field(default=None, description="DRVL/DRVH for output records; writes must lie inside")
    as_string: str | None = Field(default=None, description="CHAR waveform decoded as a (long) string")
    array_summary: ArraySummary | None = None
    downsampled: bool = False


class PVError(BaseModel):
    name: str
    error: str


class PVsResult(BaseModel):
    readings: list[PVValue]
    errors: list[PVError]
    timestamp: str


class PVInfo(BaseModel):
    name: str
    connected: bool
    server: str = Field(description="host:port of the IOC (or CA gateway) serving the PV")
    native_type: str
    element_count: int
    read_access: bool
    write_access: bool = Field(description="EPICS access security grants this client write access")
    put_allowed_by_allowlist: bool
    timestamp: str


class MonitorUpdate(BaseModel):
    timestamp: str
    value: Any
    severity: str


class MonitorStats(BaseModel):
    minimum: float
    maximum: float
    mean: float
    std: float
    first: float
    last: float
    rate_per_s: float = Field(description="Least-squares slope vs IOC timestamp, units/s")


class MonitorResult(BaseModel):
    name: str
    duration_s: float
    n_updates: int
    non_finite_updates: int = Field(0, description="Numeric updates that were NaN or infinite (not in stats)")
    updates: list[MonitorUpdate] = Field(description="Updates (arrays summarised by their mean), at most max_points")
    stats: MonitorStats | None = Field(description="For numeric PVs (arrays: per-update mean), finite values only")
    severities_seen: list[str]
    timestamp: str


class PutResult(BaseModel):
    name: str
    requested: Any
    previous_value: Any
    value: Any = Field(description="Value read back from the PV after the write")
    severity: str
    status: str
    completed: bool = Field(description="True if the IOC confirmed completion (put-callback)")
    elapsed_s: float
    warnings: list[str]
    timestamp: str


class PVWrite(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, description="PV name")]
    value: float | int | str | list[float | int | str] = Field(description="Value (number, enum state, string or array)")


class PutsResult(BaseModel):
    results: list[PutResult]
    not_written: list[str] = Field(description="PVs not written because an earlier write failed")
    error: str | None = None
    timestamp: str


class SafeStateResult(BaseModel):
    configured: bool
    results: list[PutResult]
    errors: list[PVError]
    message: str
    timestamp: str


# ------------------------------------------------------------------ helpers


def _client() -> EpicsClient:
    return server.driver


def _range(r: tuple[float, float] | None) -> Range | None:
    if r is None:
        return None
    low, high = _finite(r[0]), _finite(r[1])
    return None if low is None and high is None else Range(low=low, high=high)


def _summary(arr: np.ndarray) -> ArraySummary:
    ok = np.isfinite(arr)
    fin = arr[ok]
    if not fin.size:
        return ArraySummary(
            length=int(arr.size), non_finite=int(arr.size), minimum=None, maximum=None, mean=None, std=None,
            index_of_max=None,
        )
    return ArraySummary(
        length=int(arr.size), non_finite=int(arr.size - fin.size), minimum=float(fin.min()),
        maximum=float(fin.max()), mean=float(fin.mean()), std=float(fin.std()),
        index_of_max=int(np.argmax(np.where(ok, arr, -np.inf))),
    )


def _to_model(r: PVReading, max_elements: int) -> PVValue:
    value = _scalar(r.value)
    summary = None
    downsampled = False
    if r.array is not None and r.array.size > 1:
        arr = r.array.astype(float)
        summary = _summary(arr)
        if arr.size > max_elements:
            edges = np.linspace(0, arr.size, max_elements + 1).astype(int)
            value = [float(arr[a:b].mean()) for a, b in zip(edges[:-1], edges[1:], strict=True)]
            downsampled = True
    now = time.time()
    return PVValue(
        name=r.name, value=value, native_type=r.native_type, element_count=r.count, units=r.units,
        precision=r.precision, severity=r.severity, status=r.status, timestamp=_iso(r.timestamp),
        age_s=None if r.timestamp is None else round(now - r.timestamp, 3), enum_strings=r.enum_strings,
        display_limits=_range(r.display_limits), alarm_limits=_range(r.alarm_limits),
        warning_limits=_range(r.warning_limits), control_limits=_range(r.control_limits), as_string=r.as_string,
        array_summary=summary, downsampled=downsampled,
    )


def _put_result(prep: Any, outcome: dict[str, Any]) -> PutResult:
    after: PVReading = outcome["reading"]
    return PutResult(
        name=prep.name, requested=prep.requested, previous_value=_scalar(prep.before), value=_scalar(after.value),
        severity=after.severity, status=after.status, completed=outcome["completed"],
        elapsed_s=round(outcome["elapsed_s"], 4), warnings=prep.warnings, timestamp=_now(),
    )


def _safe_state_spec() -> list[tuple[str, Any]]:
    spec = server.settings.options.get("safe_state")
    if not spec and server.settings.simulate:
        spec = SIM_SAFE_STATE.format(p=server.settings.options.get("sim_prefix") or "SIM:")  # as connect()
    try:
        return parse_safe_state(spec)
    except ValueError as exc:
        raise InstrumentError(f"--option safe_state is malformed: {exc}. Nothing was written.") from exc


def _put_timeout(deadline: float, timeout_s: float) -> float:
    """The put-callback wait allowed now: ``timeout_s``, cut to what is left of the call's budget."""
    remaining = deadline - time.monotonic()
    if remaining < _MIN_PUT_WAIT_S:
        raise InstrumentTimeout(
            f"Stopped: this call used up its {_PUT_BUDGET_S:g} s time budget before the write was sent. "
            "Nothing was written."
        )
    return min(timeout_s, remaining)


PVName = Annotated[str, Field(min_length=1, max_length=255, description="PV name, e.g. 'BL7:MTR1.RBV'")]


# ------------------------------------------------------------------ tools


@mcp.tool(**READ)
def get_pv(
    name: PVName,
    max_elements: Annotated[int, Field(ge=1, le=10000, description="Arrays longer than this are downsampled")] = 100,
) -> PVValue:
    """Read one PV with its metadata: value, units, precision, alarm severity/status, IOC timestamp
    and age, display/alarm/warning/control limits and enum state names. Waveforms come back
    downsampled with min/max/mean statistics."""
    return _to_model(_client().read(name), max_elements)


@mcp.tool(**READ)
def get_pvs(
    names: Annotated[list[PVName], Field(min_length=1, max_length=200, description="PV names")],
    max_elements: Annotated[int, Field(ge=1, le=10000, description="Arrays longer than this are downsampled")] = 20,
) -> PVsResult:
    """Read many PVs at once (e.g. all motors or vacuum gauges of a beamline). PVs that cannot be
    reached are listed in `errors` instead of failing the whole call."""
    readings, errors = [], []
    for name, result in zip(names, _client().read_many(names), strict=True):
        if isinstance(result, Exception):
            errors.append(PVError(name=name, error=str(result)))
        else:
            readings.append(_to_model(result, max_elements))
    return PVsResult(readings=readings, errors=errors, timestamp=_now())


@mcp.tool(**READ)
def pv_info(name: PVName) -> PVInfo:
    """Connection details of a PV: serving IOC host:port, native type, element count, and whether this
    client has read/write access (EPICS access security) and passes the put allow-list."""
    info = _client().info(name)
    return PVInfo(**info, timestamp=_now())


@mcp.tool(**READ, timeout=3700)
def monitor_pv(
    name: PVName,
    duration_s: Annotated[float, Field(gt=0, le=3600, description="How long to collect updates")] = 5.0,
    max_updates: Annotated[int, Field(ge=1, le=100000, description="Stop after this many updates")] = 1000,
    max_points: Annotated[int, Field(ge=0, le=5000, description="Updates to include in the reply (evenly thinned)")] = 200,
) -> MonitorResult:
    """Subscribe to a PV and collect every value change for `duration_s` seconds (or until
    `max_updates`), then return statistics (min/max/mean/std, drift rate) and the updates."""
    server.check("max_monitor_duration_s", duration_s, "monitor duration")
    c = _client()
    first = c.read(name)
    updates = c.monitor(name, duration_s, max_updates)
    rows: list[tuple[float, Any, str]] = []
    for ts, resp in updates:
        value = c.update_value(first.native_type, resp, first.enum_strings)
        severity = c.AlarmSeverity(int(resp.metadata.severity)).name
        rows.append((ts, value, severity))
    numeric_all = [(ts, _as_number(v)) for ts, v, _ in rows if _is_numeric(v)]
    numeric = [(ts, y) for ts, y in numeric_all if math.isfinite(y)]
    stats = None
    if numeric:
        t = np.array([n[0] for n in numeric]) - numeric[0][0]
        y = np.array([n[1] for n in numeric])
        slope = float(np.polyfit(t, y, 1)[0]) if len(y) > 1 and np.ptp(t) > 0 else 0.0
        stats = MonitorStats(
            minimum=float(y.min()), maximum=float(y.max()), mean=float(y.mean()), std=float(y.std()),
            first=float(y[0]), last=float(y[-1]), rate_per_s=slope,
        )
    if max_points and len(rows) > max_points:
        keep = np.linspace(0, len(rows) - 1, max_points).astype(int)
        shown = [rows[i] for i in keep]
    else:
        shown = rows if max_points else []
    return MonitorResult(
        name=name, duration_s=duration_s, n_updates=len(rows), non_finite_updates=len(numeric_all) - len(numeric),
        updates=[
            MonitorUpdate(timestamp=_iso(ts) or "", severity=sev, value=_scalar(_as_number(v) if _is_numeric(v) else v))
            for ts, v, sev in shown
        ],
        stats=stats, severities_seen=sorted({sev for _, _, sev in rows}), timestamp=_now(),
    )


@mcp.tool(**HAZARD, timeout=_PUT_TOOL_TIMEOUT_S)
def put_pv(
    name: PVName,
    value: Annotated[
        float | int | str | list[float | int | str],
        Field(description="New value: number, enum state name or index, string, or array for waveforms"),
    ],
    wait: Annotated[bool, Field(description="Wait for the IOC's put-completion (put-callback)")] = True,
    timeout_s: Annotated[float, Field(gt=0, le=3600, description="Max seconds to wait for completion")] = 10.0,
) -> PutResult:
    """Write a PV. This can move motors, open shutters, change magnet or high-voltage setpoints and
    heat or cool samples: tell the user exactly what will change first. The write is refused unless
    the PV matches the put allow-list, the IOC grants write access, and the value has the right
    type and lies within the PV's control limits (DRVL/DRVH). Returns the read-back value."""
    if wait:
        server.check("max_put_wait_s", timeout_s, "put-completion wait")
    deadline = time.monotonic() + _PUT_BUDGET_S
    c = _client()
    prep = c.prepare_put(name, value)
    return _put_result(prep, c.put(prep, wait=wait, timeout=_put_timeout(deadline, timeout_s)))


@mcp.tool(**HAZARD, timeout=_PUT_TOOL_TIMEOUT_S)
def put_pvs(
    writes: Annotated[list[PVWrite], Field(min_length=1, max_length=100, description="PV writes, applied in order")],
    wait: Annotated[bool, Field(description="Wait for each put-completion before the next write")] = True,
    timeout_s: Annotated[float, Field(gt=0, le=3600, description="Max seconds to wait per write")] = 10.0,
) -> PutsResult:
    """Write several PVs in order (e.g. set both slit blades). Every write is validated first (allow-
    list, access rights, type, control limits); if any is invalid nothing is written. Writing stops
    at the first failure and the rest are reported as not written. The whole batch must finish within
    an hour: writes that would start later are not sent."""
    server.check("max_put_batch", len(writes), "number of PVs in one batch")
    if wait:
        server.check("max_put_wait_s", timeout_s, "put-completion wait")
    deadline = time.monotonic() + _PUT_BUDGET_S
    c = _client()
    prepared = [c.prepare_put(w.name, w.value) for w in writes]  # validate everything before writing
    results: list[PutResult] = []
    for i, prep in enumerate(prepared):
        remaining = deadline - time.monotonic()
        if remaining < _MIN_PUT_WAIT_S:
            return PutsResult(
                results=results, not_written=[p.name for p in prepared[i:]],
                error=f"Stopped: the batch used up its {_PUT_BUDGET_S:g} s time budget; the remaining PVs were "
                "not written.",
                timestamp=_now(),
            )
        try:
            results.append(_put_result(prep, c.put(prep, wait=wait, timeout=min(timeout_s, remaining))))
        except InstrumentError as exc:
            return PutsResult(
                results=results, not_written=[p.name for p in prepared[i + 1 :]],
                error=f"{prep.name}: {exc}", timestamp=_now(),
            )
    return PutsResult(results=results, not_written=[], timestamp=_now())


@mcp.tool(**SAFETY, timeout=120)
def apply_safe_state() -> SafeStateResult:
    """Emergency action: write the scientist-configured safe-state PVs (--option safe_state, e.g.
    motor STOP fields, shutter close, HV off), all of them even if one fails. Available in read-only
    mode. It does not replace the facility's own interlocks and stop buttons."""
    spec = _safe_state_spec()
    if not spec:
        return SafeStateResult(
            configured=False, results=[], errors=[],
            message="No safe state is configured (start the server with --option safe_state='PV=value;...'). "
            "Use the facility's own stop/interlock procedures.",
            timestamp=_now(),
        )
    c = _client()
    results, errors = [], []
    for name, value in spec:
        try:
            prep = c.prepare_put(name, value, check_allowlist=False)
            results.append(_put_result(prep, c.put(prep, wait=True, timeout=min(5.0, server.limits["max_put_wait_s"]))))
        except Exception as exc:
            errors.append(PVError(name=name, error=str(exc)))
    return SafeStateResult(
        configured=True, results=results, errors=errors,
        message="Safe state applied." if not errors else "Safe state applied with errors: check the listed PVs now.",
        timestamp=_now(),
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
