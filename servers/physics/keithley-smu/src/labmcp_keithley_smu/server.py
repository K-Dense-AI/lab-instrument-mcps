"""MCP server for Keithley SourceMeter SMUs (2400 series SCPI, 2450 family SCPI, 2600B TSP)."""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_keithley_smu.driver import (
    KeithleySMU,
    SourceKind,
    Status,
    linear_levels,
    log_levels,
    open_smu,
)
from labmcp_keithley_smu.simulator import make_simulator


def connect(ctx: ConnectContext) -> KeithleySMU:
    dialect = (ctx.option("dialect", "auto") or "auto").lower()
    if dialect not in {"auto", "2400", "2450", "2600"}:
        raise InstrumentProtocolError(f"--option dialect must be auto, 2400, 2450 or 2600 (got {dialect!r}).")
    channel = (ctx.option("channel", "a") or "a").lower().removeprefix("smu")
    dut = (ctx.option("sim_dut", "resistor") or "resistor").lower()
    if dut not in {"resistor", "diode"}:
        raise InstrumentProtocolError(f"--option sim_dut must be resistor or diode (got {dut!r}).")
    resistance = float(ctx.option("sim_resistance_ohm", "1000") or 1000)
    sim_dialect = "2450" if dialect == "auto" else dialect
    transport = ctx.open_transport(
        simulator=lambda: make_simulator(sim_dialect, dut, resistance),
        read_termination="\n",
        write_termination="\n",
        timeout=10.0,
    )
    try:
        return open_smu(transport, dialect, channel)
    except Exception:
        transport.close()
        raise


server = InstrumentServer(
    "Keithley SourceMeter SMU (SCPI / TSP)",
    connect=connect,
    package="labmcp-keithley-smu",
    instructions="""
Controls a Keithley SourceMeter source-measure unit (2400 series, 2450/2460/2461/2470 in SCPI
mode, or 2600B series via TSP). The SMU applies voltage or current to a device under test (DUT).
- Workflow: `get_status` -> `configure_source` (output must be off) -> confirm with the user that
  the DUT is connected and that it is safe -> `output_on` -> `measure` -> `output_off`.
  `run_iv_sweep` switches the output on and off by itself.
- Always choose a compliance (the current limit when sourcing voltage, the voltage limit when
  sourcing current) that protects the DUT. LEDs, diodes, transistors and thin films are easily
  destroyed by too much current.
- Call `output_off` when you are done, before the user touches or changes the DUT, and
  immediately if anything looks wrong. It also aborts a running sweep.
- `in_compliance: true` means the source is clamped at the limit; the reading is the limit, not
  what the programmed level would have produced.
- Voltages above 30 V are hazardous. Never tell the user to touch the DUT while the output is on.
- For batteries, capacitors or solar cells, set the instrument's output-off state to
  high-impedance on the front panel first (this server does not change it).
""",
    limits=[
        Limit("max_voltage_v", 20, "V", "Largest |voltage| an agent may source or allow as compliance"),
        Limit("max_current_a", 0.1, "A", "Largest |current| an agent may source or allow as compliance"),
        Limit("max_power_w", 2, "W", "Largest worst-case power |source level x compliance|"),
        Limit("max_sweep_points", 501, "points", "Most points (including the return leg) in one IV sweep"),
        Limit("max_sweep_duration_s", 600, "s", "Longest estimated IV sweep duration"),
    ],
    address_help="""\
  GPIB0::24::INSTR                 2400 series (default GPIB address 24)
  USB0::0x05E6::0x2450::04096331::INSTR   2450 over USBTMC
  TCPIP0::192.168.1.50::inst0::INSTR      2450 / 2600B over LAN (VXI-11)
  tcp://192.168.1.50:5025          2450 / 2600B raw socket
  serial:///dev/ttyUSB0?baudrate=9600     2400 RS-232 (set TERMINATOR to LF on the front panel)""",
    option_help={
        "dialect": "auto (default, from *IDN?/*LANG?), 2400 (2400-series SCPI), 2450 (2450-family SCPI), 2600 (2600B TSP)",
        "channel": "2600B SMU channel: a (default) or b",
        "sim_dut": "simulated device under test: resistor (default) or diode (1N4148-like)",
        "sim_resistance_ohm": "resistance of the simulated resistor (default 1000)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StatusModel(BaseModel):
    output_on: bool
    source: Literal["voltage", "current"]
    level: float = Field(description="Programmed source level, in level_unit")
    level_unit: str
    compliance: float = Field(description="Compliance limit, in compliance_unit")
    compliance_unit: str
    in_compliance: bool = Field(description="True if the source is presently clamped at the compliance limit")
    terminals: str | None = Field(
        description="front/rear (None on the 2600B, which has one set of terminals)"
    )
    remote_sense: bool | None = Field(description="True = 4-wire (remote) sense")
    dialect: str
    timestamp: str


class MeasurementModel(BaseModel):
    voltage_v: float
    current_a: float
    resistance_ohm: float | None = Field(description="V / I (None if I = 0)")
    power_w: float = Field(description="V x I (negative = the DUT delivers power)")
    in_compliance: bool
    source: Literal["voltage", "current"]
    level: float
    compliance: float
    timestamp: str


class LinearFit(BaseModel):
    slope: float = Field(description="dI/dV in S for voltage sweeps, dV/dI in ohm for current sweeps")
    intercept: float
    resistance_ohm: float | None
    r_squared: float
    points_used: int


class SweepResult(BaseModel):
    source: Literal["voltage", "current"]
    spacing: Literal["linear", "log"]
    dual: bool
    points_requested: int
    points_measured: int
    points_returned: int
    compliance: float
    compliance_unit: str
    level: list[float] = Field(description="Programmed source levels (downsampled to max_points)")
    voltage_v: list[float] = Field(description="Measured voltage (downsampled)")
    current_a: list[float] = Field(description="Measured current (downsampled)")
    in_compliance: list[bool] = Field(description="Compliance flag per returned point")
    compliance_points: int = Field(description="Number of measured points that were in compliance")
    first_compliance_level: float | None
    voltage_min_v: float | None
    voltage_max_v: float | None
    current_min_a: float | None
    current_max_a: float | None
    max_abs_power_w: float | None
    ohmic_fit: LinearFit | None = Field(
        description="Least-squares line through the non-compliance points of a linear sweep (meaningful for resistive DUTs)"
    )
    aborted: bool
    stop_reason: str | None
    output_off: bool = Field(description="Output state confirmed OFF after the sweep")
    duration_s: float
    saved_to: str | None
    started: str


def _status_model(drv: KeithleySMU, st: Status) -> StatusModel:
    return StatusModel(
        output_on=st.output_on,
        source=st.source,
        level=st.level,
        level_unit=st.level_unit,
        compliance=st.compliance,
        compliance_unit=st.compliance_unit,
        in_compliance=st.in_compliance,
        terminals=st.terminals,
        remote_sense=st.remote_sense,
        dialect=drv.dialect,
        timestamp=_now(),
    )


def _check_request(drv: KeithleySMU, source: SourceKind, level: float, compliance: float) -> None:
    """Check a source level + compliance against the safety limits and the model maxima."""
    if source == "voltage":
        server.check("max_voltage_v", abs(level), "source voltage")
        server.check("max_current_a", abs(compliance), "current compliance")
        volts, amps = abs(level), abs(compliance)
    else:
        server.check("max_current_a", abs(level), "source current")
        server.check("max_voltage_v", abs(compliance), "voltage compliance")
        volts, amps = abs(compliance), abs(level)
    server.check("max_power_w", abs(level * compliance), "worst-case power |level x compliance|")
    vmax, imax = drv.max_voltage_v, drv.max_current_a
    if vmax is not None and imax is not None and (volts > vmax or amps > imax):
        raise InstrumentProtocolError(
            f"The Model {drv.model} can source at most {vmax:g} V / {imax:g} A; the request needs "
            f"{volts:g} V / {amps:g} A. Nothing was sent."
        )


@mcp.tool(**READ)
def get_device_info() -> dict[str, str]:
    """Identify the SMU: manufacturer, model, serial, firmware, command dialect (2400 / 2450 /
    2600), channel (2600B) and the model's maximum source voltage and current."""
    return server.driver.identify()


@mcp.tool(**READ)
def get_status() -> StatusModel:
    """Report whether the output is on, the source function (voltage/current), programmed level,
    compliance limit, whether the source is in compliance, terminals and 2/4-wire sense."""
    drv = server.driver
    return _status_model(drv, drv.status())


@mcp.tool(**CONTROL)
def configure_source(
    source: Annotated[Literal["voltage", "current"], Field(description="Source voltage or current")],
    level: Annotated[
        float,
        Field(
            ge=-1100,
            le=1100,
            description="Source level: volts for a voltage source, amps for a current source",
        ),
    ],
    compliance: Annotated[
        float,
        Field(
            gt=0,
            le=1100,
            description="Limit: current limit in A (voltage source) or voltage limit in V (current source)",
        ),
    ],
    source_range: Annotated[
        float | None, Field(gt=0, description="Fixed source range (V or A); omit for auto-range")
    ] = None,
    nplc: Annotated[
        float,
        Field(
            ge=0.01, le=10, description="Measurement integration time in power-line cycles (1 = 16.7/20 ms)"
        ),
    ] = 1.0,
) -> StatusModel:
    """Set up the source (function, level, compliance, range, measurement speed) while the output
    is OFF. Measures V and I with auto-ranging. Refused if the output is on; turn it off first or
    use `set_source_level`. Limits are checked before anything is sent."""
    drv = server.driver
    _check_request(drv, source, level, compliance)
    if source_range is not None and source_range < abs(level):
        raise InstrumentProtocolError(
            f"source_range {source_range:g} is smaller than |level| {abs(level):g}; omit it for auto-range."
        )
    with drv.t.lock:
        if drv.output_state():
            raise InstrumentProtocolError(
                "The output is ON. Call `output_off` before reconfiguring, or use `set_source_level` "
                "to change the level of the running source."
            )
        drv.configure(source, level, compliance, source_range, nplc)
        return _status_model(drv, drv.status())


@mcp.tool(**HAZARD)
def set_source_level(
    level: Annotated[
        float, Field(ge=-1100, le=1100, description="New level for the configured source: V or A")
    ],
) -> StatusModel:
    """Change the level of the configured source. If the output is ON the new voltage/current is
    applied to the DUT immediately. The level and the present compliance are checked against the
    safety limits first."""
    drv = server.driver
    with drv.t.lock:
        st = drv.status()
        _check_request(drv, st.source, level, st.compliance)
        drv.set_level(st.source, level)
        drv.raise_errors(f"setting the source level to {level:g}")
        return _status_model(drv, drv.status())


@mcp.tool(**HAZARD)
def output_on() -> StatusModel:
    """Switch the SMU output ON: the configured voltage or current is applied to the DUT. The
    programmed level and compliance are read back from the instrument and checked against the
    safety limits first. Confirm with the user that the DUT is connected and safe to energise."""
    drv = server.driver
    with drv.t.lock:
        st = drv.status()
        _check_request(drv, st.source, st.level, st.compliance)
        drv.set_output(True)
        drv.raise_errors("turning the output on")
        return _status_model(drv, drv.status())


@mcp.tool(**SAFETY)
def output_off() -> StatusModel:
    """Switch the SMU output OFF immediately (and abort any IV sweep in progress). Always
    available, including in read-only mode."""
    drv = server.driver
    drv.request_abort()
    return _status_model(drv, drv.status())


@mcp.tool(**READ)
def measure() -> MeasurementModel:
    """Take one source-measure reading (voltage, current, V/I, power, compliance flag) of the
    energised DUT. Does not change any setting; refused if the output is off."""
    drv = server.driver
    st, r = drv.measure()
    return MeasurementModel(
        voltage_v=r.voltage_v,
        current_a=r.current_a,
        resistance_ohm=r.voltage_v / r.current_a if r.current_a else None,
        power_w=r.voltage_v * r.current_a,
        in_compliance=r.in_compliance,
        source=st.source,
        level=st.level,
        compliance=st.compliance,
        timestamp=r.timestamp,
    )


@mcp.tool(**CONTROL)
def set_4wire(
    enabled: Annotated[bool, Field(description="True = 4-wire remote sense, False = 2-wire local sense")],
) -> StatusModel:
    """Select 4-wire (remote sense, Kelvin) or 2-wire measurement. 4-wire removes lead
    resistance for low-resistance DUTs but needs the SENSE leads connected. Refused while the
    output is on."""
    drv = server.driver
    with drv.t.lock:
        if drv.output_state():
            raise InstrumentProtocolError(
                "Turn the output off (`output_off`) before changing the sense mode."
            )
        drv.set_remote_sense(enabled)
        return _status_model(drv, drv.status())


def _fit(x: list[float], y: list[float]) -> tuple[float, float, float] | None:
    n = len(x)
    if n < 3:
        return None
    xm, ym = sum(x) / n, sum(y) / n
    sxx = sum((a - xm) ** 2 for a in x)
    if sxx == 0:
        return None
    slope = sum((a - xm) * (b - ym) for a, b in zip(x, y, strict=True)) / sxx
    intercept = ym - slope * xm
    ss_tot = sum((b - ym) ** 2 for b in y)
    ss_res = sum((b - (slope * a + intercept)) ** 2 for a, b in zip(x, y, strict=True))
    return slope, intercept, 1.0 - ss_res / ss_tot if ss_tot else 1.0


def _indices(n: int, max_points: int) -> list[int]:
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / (max_points - 1)
    return sorted({round(i * step) for i in range(max_points)})


@mcp.tool(**HAZARD, timeout=1800)
def run_iv_sweep(
    start: Annotated[
        float, Field(ge=-1100, le=1100, description="First level: V (voltage sweep) or A (current sweep)")
    ],
    stop: Annotated[float, Field(ge=-1100, le=1100, description="Last level: V or A")],
    points: Annotated[int, Field(ge=2, le=100000, description="Levels from start to stop (inclusive)")],
    compliance: Annotated[
        float,
        Field(
            gt=0,
            le=1100,
            description="Current limit in A (voltage sweep) or voltage limit in V (current sweep)",
        ),
    ],
    source: Annotated[
        Literal["voltage", "current"], Field(description="Sweep voltage or current")
    ] = "voltage",
    spacing: Annotated[Literal["linear", "log"], Field(description="Level spacing")] = "linear",
    dual: Annotated[bool, Field(description="Sweep back from stop to start afterwards (hysteresis)")] = False,
    delay_s: Annotated[float, Field(ge=0, le=60, description="Settling delay at each point, s")] = 0.05,
    nplc: Annotated[float, Field(ge=0.01, le=10, description="Integration time per measurement, PLC")] = 1.0,
    stop_on_compliance: Annotated[
        bool, Field(description="End the sweep at the first compliance point")
    ] = False,
    max_points: Annotated[
        int, Field(ge=10, le=5000, description="Maximum points returned in the reply")
    ] = 200,
    save_path: Annotated[str | None, Field(description="Optional CSV path for every point")] = None,
) -> SweepResult:
    """Run a stepped IV sweep: configure the source, switch the output ON, step through the
    levels measuring V and I at each, then ALWAYS switch the output OFF (also on errors or when
    `output_off` is called). Returns the curve (downsampled), compliance flags, a linear fit and
    optional full CSV. The output must be off beforehand. Limits are checked first."""
    drv = server.driver
    levels = log_levels(start, stop, points) if spacing == "log" else linear_levels(start, stop, points)
    if dual:
        levels = levels + levels[-2::-1]
    server.check("max_sweep_points", len(levels), "number of sweep points")
    peak = max(abs(start), abs(stop))
    _check_request(drv, source, peak, compliance)
    estimate = len(levels) * (delay_s + 2 * nplc / 50.0 + 0.02)
    server.check("max_sweep_duration_s", estimate, "estimated sweep duration")
    if drv.output_state():
        raise InstrumentProtocolError(
            "The output is ON. Call `output_off` first; the sweep switches the output on and off itself."
        )
    started = _now()
    t0 = time.monotonic()
    drv.configure(source, levels[0], compliance, None, nplc)
    data = drv.sweep(source, levels, delay_s=delay_s, stop_on_compliance=stop_on_compliance)
    output_off = not drv.output_state()
    duration = time.monotonic() - t0

    volts = [r.voltage_v for r in data.readings]
    amps = [r.current_a for r in data.readings]
    comp = [r.in_compliance for r in data.readings]
    fit = None
    if spacing == "linear":
        ok = [k for k, c in enumerate(comp) if not c]
        x, y = (
            ([volts[k] for k in ok], [amps[k] for k in ok])
            if source == "voltage"
            else (
                [amps[k] for k in ok],
                [volts[k] for k in ok],
            )
        )
        result = _fit(x, y)
        if result is not None:
            slope, intercept, r2 = result
            # I = V/R for a voltage sweep (slope = 1/R); V = I*R for a current sweep (slope = R).
            resistance = (1.0 / slope if slope else None) if source == "voltage" else slope
            fit = LinearFit(
                slope=slope, intercept=intercept, resistance_ohm=resistance, r_squared=r2, points_used=len(ok)
            )
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["index", "level", "voltage_v", "current_a", "in_compliance", "timestamp"])
            for k, r in enumerate(data.readings):
                writer.writerow(
                    [
                        k,
                        f"{data.levels[k]:.9g}",
                        f"{r.voltage_v:.9g}",
                        f"{r.current_a:.9g}",
                        int(r.in_compliance),
                        r.timestamp,
                    ]
                )
        saved = str(path.resolve())
    idx = _indices(len(data.readings), max_points)
    n = len(data.readings)
    first_comp = next((data.levels[k] for k, c in enumerate(comp) if c), None)
    return SweepResult(
        source=source,
        spacing=spacing,
        dual=dual,
        points_requested=len(levels),
        points_measured=n,
        points_returned=len(idx),
        compliance=compliance,
        compliance_unit="A" if source == "voltage" else "V",
        level=[data.levels[k] for k in idx],
        voltage_v=[volts[k] for k in idx],
        current_a=[amps[k] for k in idx],
        in_compliance=[comp[k] for k in idx],
        compliance_points=sum(comp),
        first_compliance_level=first_comp,
        voltage_min_v=min(volts) if n else None,
        voltage_max_v=max(volts) if n else None,
        current_min_a=min(amps) if n else None,
        current_max_a=max(amps) if n else None,
        max_abs_power_w=max(abs(v * i) for v, i in zip(volts, amps, strict=True)) if n else None,
        ohmic_fit=fit,
        aborted=data.aborted,
        stop_reason=data.stop_reason,
        output_off=output_off,
        duration_s=duration,
        saved_to=saved,
        started=started,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
