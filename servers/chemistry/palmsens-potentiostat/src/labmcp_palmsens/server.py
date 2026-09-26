"""MCP server for PalmSens potentiostats that run MethodSCRIPT (EmStat Pico, EmStat4, Sensit, Nexus)."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    prepare_save_path,
)
from pydantic import BaseModel, Field

from labmcp_palmsens.driver import (
    DEVICE_SPECS,
    MethodScriptDevice,
    ScriptResult,
    bandwidth_for_rate,
    build_script,
    encode_literal,
    script_current_ranges,
    script_potentials,
    select_pgstat_mode,
)
from labmcp_palmsens.simulator import MethodScriptSimulator

HARD_MAX_DURATION_S = 3600.0  # one tool call must finish within the tool timeout below
#: Measurements run with a script timeout of 1.2 x duration + 30 s (up to 4350 s), then up to 15 s
#: to end after an abort and ~30 s for a cell_off script; the tool must not be cut off before that.
TOOL_TIMEOUT_S = 4500.0


def connect(ctx: ConnectContext) -> MethodScriptDevice:
    speed = float(ctx.option("sim_speed", "10") or 10)
    # EmStat Pico UART: 230400 baud 8N1 with XON/XOFF (Pico protocol manual table 1). EmStat4 /
    # Nexus over USB are virtual COM ports where the baud rate has no effect; their UART runs at
    # 921600 with RTS/CTS (EmStat4 protocol manual table 1): serial://...?baudrate=921600&rtscts=true
    # The simulator streams measurement packages through its poll() hook.
    transport = ctx.open_transport(
        simulator=lambda: MethodScriptSimulator(speed=speed),
        baudrate=230400,
        xonxoff=True,
        read_termination="\n",
        write_termination="\n",
        timeout=2.0,
    )
    device = MethodScriptDevice(transport)
    try:
        device.identify()  # learn the device type: potential windows and PGStat modes depend on it
    except Exception:
        transport.close()  # otherwise the port stays open and every reconnect finds it busy
        raise
    return device


server = InstrumentServer(
    "PalmSens Potentiostat (MethodSCRIPT)",
    connect=connect,
    package="labmcp-palmsens",
    instructions="""
Controls a PalmSens potentiostat that runs MethodSCRIPT (EmStat Pico, EmStat4 LR/HR, Sensit,
Nexus): cyclic voltammetry, linear sweep, differential pulse voltammetry and chronoamperometry.
- Every measurement switches the cell ON and applies potential to the electrodes. Confirm the
  electrodes are in solution and connected (WE, RE, CE) before starting.
- Potentials are versus the reference electrode, in volts. Anodic (oxidation) current is positive.
- Pick `current_range_a` just above the largest current you expect; with autorange the instrument
  may switch to lower ranges but never above it. Overload warnings mean the range is too low.
- Results are downsampled to `max_points`; peaks and other summaries use every point. Pass
  `save_path` to keep the full data as CSV.
- Call `abort_measurement` to stop immediately; the cell is switched off afterwards.
- `get_device_info` lists the potential window and current ranges of the connected model.
""",
    limits=[
        Limit("max_potential_v", 2.0, "V", "Largest absolute potential (vs RE) an agent may apply"),
        Limit("max_current_range_a", 0.01, "A", "Highest current range an agent may select"),
        Limit("max_duration_s", 600.0, "s", "Longest measurement or raw script an agent may start"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0                          EmStat Pico / Sensit (230400 baud, XON/XOFF)
  serial:///dev/ttyACM0                          EmStat4 / Nexus over USB (virtual COM port)
  serial://COM7?baudrate=921600&rtscts=true&xonxoff=false   EmStat4 UART""",
    option_help={"sim_speed": "simulator only: time acceleration factor (default 10)"},
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DataPoint(BaseModel):
    time_s: float = Field(description="Time since the start of the technique")
    potential_v: float = Field(description="Applied (set) potential vs RE")
    current_a: float = Field(description="Measured WE current (anodic positive)")
    scan: int = Field(0, description="CV scan index (0-based)")


class CVScanPeaks(BaseModel):
    scan: int
    anodic_peak_current_a: float
    anodic_peak_potential_v: float
    cathodic_peak_current_a: float
    cathodic_peak_potential_v: float
    peak_separation_v: float = Field(
        description="Epa - Epc (about 0.059 V for a reversible 1-electron couple)"
    )
    midpoint_potential_v: float = Field(description="(Epa + Epc) / 2, an estimate of E1/2")


class MeasurementResult(BaseModel):
    technique: str
    simulated: bool = Field(description="True if this data came from the built-in simulator")
    device: str
    started_at: str
    duration_s: float
    parameters: dict[str, Any] = Field(description="Technique parameters as sent to the instrument")
    n_points: int = Field(description="Number of points measured")
    returned_points: int
    data: list[DataPoint] = Field(description="Downsampled data (every n-th point, including the last)")
    summary: dict[str, float | None] = Field(description="Summary statistics computed from all points")
    cv_peaks: list[CVScanPeaks] | None = Field(
        None, description="Per-scan peaks (CV only; raw extrema, no baseline)"
    )
    aborted: bool = Field(description="True if the measurement was aborted before completion")
    warnings: list[str]
    saved_path: str | None = None
    timestamp: str


class ScriptOutput(BaseModel):
    simulated: bool
    duration_s: float
    output_lines: list[str] = Field(description="Raw output lines (truncated to max_lines)")
    total_lines: int
    packages: list[dict[str, float | None]] = Field(
        description="Data packages as {VarType: value} (None for 'nan'), truncated to max_lines"
    )
    total_packages: int
    texts: list[str] = Field(description="send_string output")
    error: str | None
    cell_off_after_error: bool | None = Field(
        None,
        description="After a runtime error (on_finished: is skipped) or a timeout: True if the server's "
        "cell_off script succeeded, False if it failed (the cell may still be on)",
    )
    aborted: bool
    timed_out: bool
    timestamp: str


# ---------------------------------------------------------------- helpers

CurrentRange = Annotated[
    float,
    Field(gt=1e-10, le=1.0, description="Highest expected |current| in A; sets the (maximum) current range"),
]
Autorange = Annotated[
    bool, Field(description="Let the instrument switch to lower current ranges automatically")
]
Equilibration = Annotated[
    float, Field(ge=0, le=600, description="Seconds to hold the begin potential before the technique starts")
]
MaxPoints = Annotated[int, Field(ge=10, le=100_000, description="Maximum number of points to return")]
SavePath = Annotated[
    str | None, Field(description="Write every point to this new .csv file (optional; never overwrites a file)")
]


def _check_common(e_min: float, e_max: float, current_range_a: float, duration_s: float) -> None:
    server.check("max_potential_v", max(abs(e_min), abs(e_max)), "potential magnitude")
    server.check("max_current_range_a", current_range_a, "current range")
    server.check("max_duration_s", duration_s, "measurement duration")
    if duration_s > HARD_MAX_DURATION_S:
        raise ValueError(
            f"A {duration_s:.0f} s measurement cannot run in a single tool call (maximum {HARD_MAX_DURATION_S:.0f} s)."
        )


def _points(result: ScriptResult, dt: float | None) -> tuple[list[DataPoint], list[int], int]:
    """Data points, their current status flags, and how many packages had a 'nan' value (dropped:
    a NaN would make every summary meaningless and is not valid JSON for the output schema)."""
    points, status, invalid = [], [], 0
    for k, (scan, variables) in enumerate(result.packets):
        by_type = {v.type: v for v in variables}
        if "ba" not in by_type or "da" not in by_type:
            continue
        cur = by_type["ba"]
        t = by_type["eb"].value if "eb" in by_type else (k + 1) * (dt or 0.0)
        if not all(math.isfinite(v) for v in (t, by_type["da"].value, cur.value)):
            invalid += 1
            continue
        points.append(DataPoint(time_s=t, potential_v=by_type["da"].value, current_a=cur.value, scan=scan))
        status.append(cur.status)
    return points, status, invalid


def _downsample(points: list[DataPoint], max_points: int) -> list[DataPoint]:
    if len(points) <= max_points:
        return points
    stride = math.ceil(len(points) / max_points)
    out = points[::stride]
    if out[-1] is not points[-1]:
        out = out[:-1] + [points[-1]] if len(out) >= max_points else out + [points[-1]]
    return out


def _save_csv(target: Path, points: list[DataPoint], status: list[int]) -> str:
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["index", "scan", "time_s", "potential_v", "current_a", "status"])
        for i, (p, s) in enumerate(zip(points, status, strict=True)):
            writer.writerow([i, p.scan, f"{p.time_s:.6g}", f"{p.potential_v:.6g}", f"{p.current_a:.6g}", s])
    return str(target)


def _extrema(points: list[DataPoint]) -> dict[str, float | None]:
    if not points:
        return {
            "max_current_a": None,
            "max_current_potential_v": None,
            "min_current_a": None,
            "min_current_potential_v": None,
        }
    hi = max(points, key=lambda p: p.current_a)
    lo = min(points, key=lambda p: p.current_a)
    return {
        "max_current_a": hi.current_a,
        "max_current_potential_v": hi.potential_v,
        "min_current_a": lo.current_a,
        "min_current_potential_v": lo.potential_v,
    }


def _cv_peaks(points: list[DataPoint]) -> list[CVScanPeaks]:
    out = []
    for scan in sorted({p.scan for p in points}):
        pts = [p for p in points if p.scan == scan]
        hi = max(pts, key=lambda p: p.current_a)
        lo = min(pts, key=lambda p: p.current_a)
        out.append(
            CVScanPeaks(
                scan=scan,
                anodic_peak_current_a=hi.current_a,
                anodic_peak_potential_v=hi.potential_v,
                cathodic_peak_current_a=lo.current_a,
                cathodic_peak_potential_v=lo.potential_v,
                peak_separation_v=hi.potential_v - lo.potential_v,
                midpoint_potential_v=(hi.potential_v + lo.potential_v) / 2,
            )
        )
    return out


def _run(
    technique: str,
    loop_line: str,
    parameters: dict[str, Any],
    *,
    begin_v: float,
    e_min: float,
    e_max: float,
    points_per_s: float,
    duration_s: float,
    dt: float | None,
    current_range_a: float,
    autorange: bool,
    equilibration_s: float,
    max_points: int,
    save_path: str | None,
    with_timer: bool = False,
) -> tuple[MeasurementResult, list[DataPoint]]:
    """Check limits, build and run the script; return the result (without summary) and all points."""
    _check_common(e_min, e_max, current_range_a, duration_s)
    # Checked before the measurement, so a bad path cannot waste it (or lose its data afterwards).
    target = prepare_save_path(save_path, suffixes=(".csv",)) if save_path else None
    device = server.driver
    bandwidth = bandwidth_for_rate(points_per_s)
    mode = select_pgstat_mode(device.device_type, e_min, e_max, bandwidth)
    script = build_script(
        loop_line,
        begin_potential_v=begin_v,
        e_min=e_min,
        e_max=e_max,
        pgstat_mode=mode,
        bandwidth_hz=bandwidth,
        current_range_a=current_range_a,
        autorange=autorange,
        equilibration_s=equilibration_s,
        with_timer=with_timer,
    )
    started = _now()
    result = device.execute(script, timeout_s=duration_s * 1.2 + 30.0)
    points, status, invalid = _points(result, dt)
    warnings = []
    if invalid:
        warnings.append(f"{invalid} point(s) had an invalid ('nan') value and were left out.")
    if result.malformed:
        warnings.append(f"{len(result.malformed)} data line(s) could not be decoded and were skipped.")
    if result.error:
        cell = (
            " The cell was switched off afterwards."
            if result.cell_off_sent
            else " WARNING: switching the cell off afterwards failed; call `abort_measurement`."
        )
        if not points:
            raise InstrumentProtocolError(f"The measurement failed: {result.error}.{cell}")
        warnings.append(f"The measurement stopped with {result.error}.{cell}")
    if result.timed_out:
        warnings.append("The measurement took too long and was aborted.")
    elif result.aborted:
        warnings.append("The measurement was aborted before completion.")
    overloads = sum(1 for s in status if s & 0x2)
    if overloads:
        warnings.append(f"{overloads} point(s) overloaded the current range: increase current_range_a.")
    timing = sum(1 for s in status if s & 0x1)
    if timing:
        warnings.append(f"{timing} point(s) did not meet the requested timing (instrument too busy).")
    saved = _save_csv(target, points, status) if target is not None and points else None
    data = _downsample(points, max_points)
    result_model = MeasurementResult(
        technique=technique,
        simulated=server.settings.simulate,
        device=device.device_type,
        started_at=started,
        duration_s=round(result.duration_s, 3),
        parameters={**parameters, "pgstat_mode": mode, "max_bandwidth_hz": round(bandwidth, 3)},
        n_points=len(points),
        returned_points=len(data),
        data=data,
        summary={},
        aborted=result.aborted or result.timed_out,
        warnings=warnings,
        saved_path=saved,
        timestamp=_now(),
    )
    return result_model, points


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def get_device_info() -> dict[str, Any]:
    """Identify the potentiostat (model, firmware, serial number) and list its potential window,
    PGStat modes and current ranges, so measurement parameters can be chosen within them."""
    device = server.driver
    info: dict[str, Any] = dict(device.identify())
    info["measurement_running"] = device.running
    spec = DEVICE_SPECS.get(device.device_type)
    if spec:
        info.update({k: (list(v) if isinstance(v, tuple) else v) for k, v in spec.items()})
    return info


@mcp.tool(**HAZARD, timeout=TOOL_TIMEOUT_S)
def run_cyclic_voltammetry(
    begin_potential_v: Annotated[
        float, Field(ge=-10, le=10, description="Start (and end) potential, V vs RE")
    ],
    vertex1_potential_v: Annotated[float, Field(ge=-10, le=10, description="First vertex, V vs RE")],
    vertex2_potential_v: Annotated[float, Field(ge=-10, le=10, description="Second vertex, V vs RE")],
    step_potential_v: Annotated[float, Field(ge=1e-4, le=0.25, description="Potential step, V")] = 0.01,
    scan_rate_v_s: Annotated[float, Field(gt=0, le=5, description="Scan rate, V/s")] = 0.1,
    n_scans: Annotated[int, Field(ge=1, le=100, description="Number of cycles")] = 1,
    current_range_a: CurrentRange = 1e-4,
    autorange: Autorange = True,
    equilibration_s: Equilibration = 0.0,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> MeasurementResult:
    """Run a cyclic voltammogram: begin -> vertex 1 -> vertex 2 -> begin, `n_scans` times, and return
    potential/current/time data with the anodic and cathodic peaks of every scan. Applies potential
    to the cell for the whole scan; the cell is switched off at the end."""
    path = (
        abs(vertex1_potential_v - begin_potential_v)
        + abs(vertex2_potential_v - vertex1_potential_v)
        + abs(begin_potential_v - vertex2_potential_v)
    )
    if path == 0:
        raise ValueError("The vertices must differ from the begin potential.")
    lit = encode_literal
    loop = (
        f"meas_loop_cv p c {lit(begin_potential_v)} {lit(vertex1_potential_v)} {lit(vertex2_potential_v)} "
        f"{lit(step_potential_v)} {lit(scan_rate_v_s)}" + (f" nscans({n_scans})" if n_scans > 1 else "")
    )
    e = [begin_potential_v, vertex1_potential_v, vertex2_potential_v]
    params = {
        "begin_potential_v": begin_potential_v,
        "vertex1_potential_v": vertex1_potential_v,
        "vertex2_potential_v": vertex2_potential_v,
        "step_potential_v": step_potential_v,
        "scan_rate_v_s": scan_rate_v_s,
        "n_scans": n_scans,
        "current_range_a": current_range_a,
        "autorange": autorange,
        "equilibration_s": equilibration_s,
    }
    res, full = _run(
        "cyclic voltammetry",
        loop,
        params,
        begin_v=begin_potential_v,
        e_min=min(e),
        e_max=max(e),
        points_per_s=scan_rate_v_s / step_potential_v,
        duration_s=equilibration_s + n_scans * path / scan_rate_v_s,
        dt=step_potential_v / scan_rate_v_s,
        current_range_a=current_range_a,
        autorange=autorange,
        equilibration_s=equilibration_s,
        max_points=max_points,
        save_path=save_path,
    )
    res.cv_peaks = _cv_peaks(full)
    res.summary = _extrema(full)
    return res


@mcp.tool(**HAZARD, timeout=TOOL_TIMEOUT_S)
def run_linear_sweep_voltammetry(
    begin_potential_v: Annotated[float, Field(ge=-10, le=10, description="Start potential, V vs RE")],
    end_potential_v: Annotated[float, Field(ge=-10, le=10, description="End potential, V vs RE")],
    step_potential_v: Annotated[float, Field(ge=1e-4, le=0.25, description="Potential step, V")] = 0.01,
    scan_rate_v_s: Annotated[float, Field(gt=0, le=5, description="Scan rate, V/s")] = 0.1,
    current_range_a: CurrentRange = 1e-4,
    autorange: Autorange = True,
    equilibration_s: Equilibration = 0.0,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> MeasurementResult:
    """Run a linear sweep voltammogram from `begin_potential_v` to `end_potential_v` and return the
    data with the largest/smallest current and where they occur. Applies potential to the cell."""
    if begin_potential_v == end_potential_v:
        raise ValueError("Begin and end potential must differ.")
    lit = encode_literal
    loop = (
        f"meas_loop_lsv p c {lit(begin_potential_v)} {lit(end_potential_v)} {lit(step_potential_v)} "
        f"{lit(scan_rate_v_s)}"
    )
    params = {
        "begin_potential_v": begin_potential_v,
        "end_potential_v": end_potential_v,
        "step_potential_v": step_potential_v,
        "scan_rate_v_s": scan_rate_v_s,
        "current_range_a": current_range_a,
        "autorange": autorange,
        "equilibration_s": equilibration_s,
    }
    res, full = _run(
        "linear sweep voltammetry",
        loop,
        params,
        begin_v=begin_potential_v,
        e_min=min(begin_potential_v, end_potential_v),
        e_max=max(begin_potential_v, end_potential_v),
        points_per_s=scan_rate_v_s / step_potential_v,
        duration_s=equilibration_s + abs(end_potential_v - begin_potential_v) / scan_rate_v_s,
        dt=step_potential_v / scan_rate_v_s,
        current_range_a=current_range_a,
        autorange=autorange,
        equilibration_s=equilibration_s,
        max_points=max_points,
        save_path=save_path,
    )
    res.summary = _extrema(full)
    return res


@mcp.tool(**HAZARD, timeout=TOOL_TIMEOUT_S)
def run_differential_pulse_voltammetry(
    begin_potential_v: Annotated[float, Field(ge=-10, le=10, description="Start potential, V vs RE")],
    end_potential_v: Annotated[float, Field(ge=-10, le=10, description="End potential, V vs RE")],
    step_potential_v: Annotated[float, Field(ge=1e-4, le=0.25, description="Potential step, V")] = 0.005,
    pulse_potential_v: Annotated[
        float, Field(gt=0, le=0.25, description="Pulse amplitude, V (absolute)")
    ] = 0.025,
    pulse_time_s: Annotated[float, Field(ge=0.001, le=1, description="Pulse duration, s")] = 0.05,
    scan_rate_v_s: Annotated[float, Field(gt=0, le=1, description="Scan rate, V/s")] = 0.025,
    current_range_a: CurrentRange = 1e-4,
    autorange: Autorange = True,
    equilibration_s: Equilibration = 0.0,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> MeasurementResult:
    """Run differential pulse voltammetry (current = forward - reverse, per step) from begin to end
    potential and return the data with the peak current and peak potential. The scan rate must be
    below step / pulse_time / 2. Applies potential to the cell."""
    if begin_potential_v == end_potential_v:
        raise ValueError("Begin and end potential must differ.")
    limit = step_potential_v / pulse_time_s / 2
    if scan_rate_v_s >= limit:
        raise ValueError(
            f"For DPV the scan rate must be below step / pulse time / 2 = {limit:.4g} V/s (MethodSCRIPT "
            "meas_loop_dpv); lower the scan rate or pulse time, or increase the step."
        )
    lit = encode_literal
    loop = (
        f"meas_loop_dpv p c {lit(begin_potential_v)} {lit(end_potential_v)} {lit(step_potential_v)} "
        f"{lit(pulse_potential_v)} {lit(pulse_time_s)} {lit(scan_rate_v_s)}"
    )
    sign = 1.0 if end_potential_v > begin_potential_v else -1.0
    e = [begin_potential_v, end_potential_v, end_potential_v + sign * pulse_potential_v]
    params = {
        "begin_potential_v": begin_potential_v,
        "end_potential_v": end_potential_v,
        "step_potential_v": step_potential_v,
        "pulse_potential_v": pulse_potential_v,
        "pulse_time_s": pulse_time_s,
        "scan_rate_v_s": scan_rate_v_s,
        "current_range_a": current_range_a,
        "autorange": autorange,
        "equilibration_s": equilibration_s,
    }
    res, full = _run(
        "differential pulse voltammetry",
        loop,
        params,
        begin_v=begin_potential_v,
        e_min=min(e),
        e_max=max(e),
        points_per_s=1.0 / pulse_time_s,
        duration_s=equilibration_s + abs(end_potential_v - begin_potential_v) / scan_rate_v_s,
        dt=step_potential_v / scan_rate_v_s,
        current_range_a=current_range_a,
        autorange=autorange,
        equilibration_s=equilibration_s,
        max_points=max_points,
        save_path=save_path,
    )
    ext = _extrema(full)
    if sign > 0:
        res.summary = {
            "peak_current_a": ext["max_current_a"],
            "peak_potential_v": ext["max_current_potential_v"],
        }
    else:
        res.summary = {
            "peak_current_a": ext["min_current_a"],
            "peak_potential_v": ext["min_current_potential_v"],
        }
    return res


@mcp.tool(**HAZARD, timeout=TOOL_TIMEOUT_S)
def run_chronoamperometry(
    potential_v: Annotated[float, Field(ge=-10, le=10, description="Applied DC potential, V vs RE")],
    run_time_s: Annotated[
        float, Field(gt=0, le=HARD_MAX_DURATION_S, description="Total measurement time, s")
    ],
    interval_s: Annotated[float, Field(ge=0.001, le=60, description="Time between points, s")] = 0.1,
    current_range_a: CurrentRange = 1e-4,
    autorange: Autorange = True,
    equilibration_s: Equilibration = 0.0,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> MeasurementResult:
    """Hold the cell at `potential_v` for `run_time_s` and record the current every `interval_s`.
    Returns the data (time from the instrument timer) plus first/last/mean current and the charge.
    Applies potential to the cell."""
    if interval_s > run_time_s:
        raise ValueError("interval_s must not exceed run_time_s.")
    lit = encode_literal
    loop = f"meas_loop_ca p c {lit(potential_v)} {lit(interval_s)} {lit(run_time_s)}"
    params = {
        "potential_v": potential_v,
        "run_time_s": run_time_s,
        "interval_s": interval_s,
        "current_range_a": current_range_a,
        "autorange": autorange,
        "equilibration_s": equilibration_s,
    }
    res, full = _run(
        "chronoamperometry",
        loop,
        params,
        begin_v=potential_v,
        e_min=potential_v,
        e_max=potential_v,
        points_per_s=1.0 / interval_s,
        duration_s=equilibration_s + run_time_s,
        dt=interval_s,
        current_range_a=current_range_a,
        autorange=autorange,
        equilibration_s=equilibration_s,
        max_points=max_points,
        save_path=save_path,
        with_timer=True,
    )
    summary: dict[str, float | None] = {
        "first_current_a": None,
        "last_current_a": None,
        "mean_last_10pct_current_a": None,
        "charge_c": None,
    }
    if full:
        tail = full[-max(1, len(full) // 10) :]
        charge = sum(
            (b.time_s - a.time_s) * (a.current_a + b.current_a) / 2
            for a, b in zip(full, full[1:], strict=False)
        )
        summary = {
            "first_current_a": full[0].current_a,
            "last_current_a": full[-1].current_a,
            "mean_last_10pct_current_a": sum(p.current_a for p in tail) / len(tail),
            "charge_c": charge,
        }
    res.summary = summary
    return res


@mcp.tool(**HAZARD, timeout=TOOL_TIMEOUT_S)
def run_methodscript(
    script: Annotated[
        str, Field(description="MethodSCRIPT, one command per line (a leading 'e' line is ignored)")
    ],
    timeout_s: Annotated[
        float, Field(gt=0, le=HARD_MAX_DURATION_S, description="Abort the script after this")
    ] = 60,
    max_lines: Annotated[
        int, Field(ge=1, le=10_000, description="Maximum output lines/packages to return")
    ] = 200,
) -> ScriptOutput:
    """Advanced: run a raw MethodSCRIPT and return its raw output and decoded data packages. Literal
    potentials of set_e / set_range_minmax da and the CV, LSV, DPV, SWV, NPV, ACV, CA, PAD, EIS and
    fast CV/CA techniques are checked against `max_potential_v`, and literal `set_range ba` /
    `set_autoranging ba` currents against `max_current_range_a`; values computed at run time are
    not. Aborted after `timeout_s`; add `on_finished:` + `cell_off` to your script so the cell is
    switched off after an abort. After a runtime error, a timeout or `abort_measurement` the server
    switches the cell off itself."""
    lines = [ln for ln in script.splitlines() if ln.strip()]
    if lines and lines[0].strip() == "e":
        lines = lines[1:]
    if not lines:
        raise ValueError("The script is empty.")
    for number, value in script_potentials(lines):
        server.check("max_potential_v", abs(value), f"potential on script line {number}")
    for number, value in script_current_ranges(lines):
        server.check("max_current_range_a", value, f"current range on script line {number}")
    server.check("max_duration_s", timeout_s, "script timeout")
    device = server.driver
    result = device.execute(lines, timeout_s=timeout_s)
    cell_off = result.cell_off_sent
    if result.timed_out:
        cell_off = device.cell_off()
    packages = [
        {v.type: v.value if math.isfinite(v.value) else None for v in variables} for _, variables in result.packets
    ]
    return ScriptOutput(
        simulated=server.settings.simulate,
        duration_s=round(result.duration_s, 3),
        output_lines=result.lines[:max_lines],
        total_lines=len(result.lines),
        packages=packages[:max_lines],
        total_packages=len(packages),
        texts=result.texts,
        error=result.error,
        cell_off_after_error=cell_off,
        aborted=result.aborted,
        timed_out=result.timed_out,
        timestamp=_now(),
    )


@mcp.tool(**SAFETY)
def abort_measurement() -> dict[str, Any]:
    """Abort the running measurement or script immediately (communication command Z) and make sure
    the cell is switched off. Safe to call when nothing is running."""
    return server.driver.abort()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
