"""MCP server for Stanford Research Systems lock-in amplifiers (SR810/SR830, SR860/SR865A)."""

from __future__ import annotations

import csv
import math
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    parse_address,
    prepare_save_path,
)
from pydantic import BaseModel, Field

from labmcp_srs_lockin.driver import (
    MODELS,
    SETTLE_TIME_CONSTANTS,
    SRSLockIn,
    sensitivity_index,
    time_constant_index,
)
from labmcp_srs_lockin.simulator import SRSLockInSimulator


def _output_interface(ctx: ConnectContext) -> str | None:
    """Which interface the SR830's OUTX should point at (the SR86x answers whoever asked)."""
    choice = (ctx.option("interface", "auto") or "auto").lower()
    if choice in {"rs232", "gpib"}:
        return choice
    if choice == "none" or ctx.simulate or not ctx.address:
        return None
    addr = parse_address(ctx.address)
    if addr.kind == "visa":
        resource = addr.target.upper()
        if resource.startswith("GPIB"):
            return "gpib"
        if resource.startswith("ASRL"):
            return "rs232"
        return None  # USB / TCPIP resources exist only on the SR86x
    return "rs232"  # serial port or serial-to-Ethernet bridge


def connect(ctx: ConnectContext) -> SRSLockIn:
    model = (ctx.option("model", "auto") or "auto").lower()
    sim_model = model.upper() if model.upper() in MODELS else "SR830"
    interface = _output_interface(ctx)
    addr = parse_address(ctx.address) if ctx.address and not ctx.simulate else None
    serial_like = addr is not None and (addr.kind != "visa" or addr.target.upper().startswith("ASRL"))
    if serial_like and interface != "gpib":
        # RS-232: the SR830 terminates replies with CR (manual p. 5-2); commands accept LF.
        # The SR86x RS-232 reply terminator is user-selectable; LF with `model=sr860`.
        read_term = "\n" if model in {"sr860", "sr865a"} else "\r"
    else:
        read_term = "\n"  # GPIB / USBTMC / VXI-11 (EOI also ends the message)
    transport = ctx.open_transport(
        simulator=lambda: SRSLockInSimulator(model=sim_model),
        baudrate=9600,
        read_termination=read_term,
        write_termination="\n",
        timeout=3.0,
    )
    try:
        return SRSLockIn(transport, model=model, output_interface=interface)
    except Exception:
        transport.close()  # don't hold the port open (Windows would refuse the next attempt)
        raise


server = InstrumentServer(
    "SRS Lock-in Amplifier (SR830 / SR860)",
    connect=connect,
    package="labmcp-srs-lockin",
    instructions="""
Controls a Stanford Research Systems DSP lock-in amplifier: SR810/SR830 or SR860/SR865A.
- Call `get_settings` before measuring: it reports reference frequency/source, sine amplitude,
  sensitivity, time constant and input configuration.
- The SINE OUT drives the sample. `set_amplitude` and `frequency_sweep` are hazardous; the
  `set_amplitude_minimum` tool is the "output off" (SR830 minimum 4 mV rms, SR86x 1 nV rms and 0 V DC).
- After changing frequency, phase, sensitivity or time constant, wait at least 5 time constants
  (7-10 for 12-24 dB/oct slopes) before trusting a reading.
- Check `overloads` and `fraction_of_full_scale` in `read_outputs`; if overloaded, choose a larger
  sensitivity range (`set_sensitivity` or `auto_gain`).
- In external-reference mode the frequency is measured from REF IN and cannot be set.
- Choose a time constant much longer than one period of the reference (1/f) for clean outputs.
""",
    limits=[
        Limit("max_amplitude_v", 1.0, "V", "Largest sine-output amplitude (V rms) an agent may set"),
        Limit("max_sweep_duration_s", 600, "s", "Longest frequency sweep an agent may start"),
    ],
    address_help="""\
  GPIB0::8::INSTR                        GPIB via VISA (SR830 factory address 8)
  serial:///dev/ttyUSB0?baudrate=9600    SR830/SR86x RS-232 (set baud/parity on the front panel)
  USB0::<vid>::<pid>::<serial>::INSTR    SR86x USB (USBTMC; list resources with `pyvisa-shell`)
  TCPIP0::192.168.1.20::inst0::INSTR     SR86x Ethernet (VXI-11)""",
    option_help={
        "model": "auto (default, from *IDN?), sr810, sr830, sr860 or sr865a",
        "interface": "SR810/SR830 reply interface for OUTX: auto (default), rs232, gpib or none",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LockinReading(BaseModel):
    x: float = Field(description="In-phase output X (V rms, or A rms on a current input)")
    y: float = Field(description="Quadrature output Y (same unit as x)")
    r: float = Field(description="Magnitude R (same unit as x)")
    theta_deg: float = Field(description="Phase θ in degrees")
    unit: Literal["V", "A"] = Field(description="Unit of x, y and r")
    reference_frequency_hz: float
    harmonic: int
    sensitivity: float = Field(description="Full-scale sensitivity, in `unit`")
    fraction_of_full_scale: float = Field(description="r / sensitivity; above 1 means overload")
    overloads: list[str] = Field(description="Overload/unlock conditions latched since the last reading")
    aux_inputs_v: list[float] | None = Field(default=None, description="Aux In 1-4 (V), if requested")
    timestamp: str


class LockinSettings(BaseModel):
    model: str
    reference_source: str
    reference_frequency_hz: float
    harmonic: int
    phase_deg: float
    sine_amplitude_v: float = Field(description="Sine output amplitude, V rms")
    sine_dc_level_v: float | None = Field(default=None, description="Sine output DC level (SR86x only)")
    sensitivity: float = Field(description="Full-scale sensitivity (V, or A on a current input)")
    sensitivity_unit: Literal["V", "A"]
    time_constant_s: float
    filter_slope_db_oct: int
    settle_time_s: float = Field(description="Time to settle to 99 % after a change (manual table)")
    sync_filter: bool
    input_configuration: str = Field(description="A, A-B, I_1MOhm or I_100MOhm")
    coupling: str
    shield: str
    dynamic_reserve: str | None = Field(default=None, description="SR810/SR830 only")
    input_range_v: float | None = Field(default=None, description="SR86x voltage input range")
    timestamp: str


class SweepPoint(BaseModel):
    frequency_hz: float
    x: float
    y: float
    r: float
    theta_deg: float


class SweepResult(BaseModel):
    points: list[SweepPoint]
    unit: Literal["V", "A"]
    count: int
    completed: bool = Field(
        description="False if stopped early (by `set_amplitude_minimum` or the time budget)"
    )
    settle_time_per_point_s: float
    duration_s: float
    peak_frequency_hz: float | None = Field(
        description="Frequency of the largest R (null if no point was measured)"
    )
    peak_r: float | None
    amplitude_v: float = Field(description="Sine amplitude used for the whole sweep")
    time_constant_s: float
    warnings: list[str]
    saved_to: str | None = None
    started: str


def _unit(lockin: SRSLockIn) -> Literal["V", "A"]:
    return "A" if lockin.current_input() else "V"


def _settings(lockin: SRSLockIn) -> LockinSettings:
    with lockin.t.lock:
        current = lockin.current_input()
        sens = lockin.sensitivity_v() * (1e-6 if current else 1.0)
        tc = lockin.time_constant_s()
        slope = lockin.filter_slope_db()
        return LockinSettings(
            model=lockin.spec.model,
            reference_source=lockin.reference_source(),
            reference_frequency_hz=lockin.frequency_hz(),
            harmonic=lockin.harmonic(),
            phase_deg=lockin.phase_deg(),
            sine_amplitude_v=lockin.amplitude_v(),
            sine_dc_level_v=lockin.dc_level_v(),
            sensitivity=sens,
            sensitivity_unit="A" if current else "V",
            time_constant_s=tc,
            filter_slope_db_oct=slope,
            settle_time_s=SETTLE_TIME_CONSTANTS[slope] * tc,
            sync_filter=lockin.sync_filter(),
            input_configuration=lockin.input_configuration(),
            coupling=lockin.coupling(),
            shield=lockin.shield(),
            dynamic_reserve=lockin.reserve(),
            input_range_v=lockin.input_range_v(),
            timestamp=_now(),
        )


@mcp.tool(**READ)
def read_outputs(
    include_aux_inputs: Annotated[bool, Field(description="Also read Aux In 1-4 (V)")] = False,
) -> LockinReading:
    """Read X, Y, R and θ as one coherent snapshot (SNAP?), with the reference frequency,
    sensitivity, fraction of full scale and any overloads latched since the last reading.

    Values are only meaningful once the output filter has settled (~5-10 time constants after a
    change)."""
    lockin = server.driver
    with lockin.t.lock:
        snap = lockin.snapshot()
        current = lockin.current_input()
        sens = lockin.sensitivity_v() * (1e-6 if current else 1.0)
        return LockinReading(
            x=snap.x,
            y=snap.y,
            r=snap.r,
            theta_deg=snap.theta_deg,
            unit="A" if current else "V",
            reference_frequency_hz=snap.reference_frequency_hz,
            harmonic=lockin.harmonic(),
            sensitivity=sens,
            fraction_of_full_scale=abs(snap.r) / sens,
            overloads=lockin.overloads(),
            aux_inputs_v=lockin.aux_inputs_v() if include_aux_inputs else None,
            timestamp=_now(),
        )


@mcp.tool(**READ)
def get_settings() -> LockinSettings:
    """Report the reference (source, frequency, harmonic, phase), sine output, sensitivity,
    time constant/slope (with the 99 % settling time), and input configuration."""
    return _settings(server.driver)


@mcp.tool(**CONTROL)
def set_reference(
    frequency_hz: Annotated[
        float | None, Field(gt=0, le=4e6, description="Internal reference frequency (internal mode only)")
    ] = None,
    source: Annotated[
        Literal["internal", "external"] | None,
        Field(description="internal = sine output oscillator; external = lock to REF IN"),
    ] = None,
    harmonic: Annotated[int | None, Field(ge=1, le=19999, description="Detection harmonic n")] = None,
    phase_deg: Annotated[float | None, Field(ge=-360, le=360, description="Reference phase shift")] = None,
) -> LockinSettings:
    """Set the reference: source (internal/external), internal frequency, detection harmonic and
    phase shift. Omitted parameters are left unchanged. Changing the frequency also changes the
    frequency of the SINE OUT drive (at the current amplitude)."""
    lockin = server.driver
    spec = lockin.spec
    if harmonic is not None and harmonic > spec.max_harmonic:
        raise InstrumentProtocolError(f"The {spec.model} supports harmonics 1-{spec.max_harmonic}.")
    if frequency_hz is not None and not spec.min_frequency_hz <= frequency_hz <= spec.max_frequency_hz:
        raise InstrumentProtocolError(
            f"The {spec.model} internal frequency range is {spec.min_frequency_hz:g} Hz to "
            f"{spec.max_frequency_hz:g} Hz."
        )
    with lockin.t.lock:
        if source is not None:
            lockin.set_reference_internal(source == "internal")
        if frequency_hz is not None:
            if not lockin.reference_internal():
                raise InstrumentProtocolError(
                    "The frequency can only be set with the internal reference; the lock-in is "
                    "locked to an external reference (set source='internal' first)."
                )
            n = harmonic if harmonic is not None else lockin.harmonic()
            if frequency_hz * n > spec.max_frequency_hz:
                raise InstrumentProtocolError(
                    f"Detection frequency {frequency_hz * n:g} Hz (harmonic {n}) exceeds the "
                    f"{spec.model} limit of {spec.max_frequency_hz:g} Hz."
                )
            if harmonic is not None and harmonic < lockin.harmonic():
                lockin.set_harmonic(harmonic)  # lower the harmonic first so f x n stays in range
            lockin.set_frequency_hz(frequency_hz)
        if harmonic is not None:
            lockin.set_harmonic(harmonic)
        if phase_deg is not None:
            lockin.set_phase_deg(phase_deg)
        return _settings(lockin)


@mcp.tool(**HAZARD)
def set_amplitude(
    amplitude_v: Annotated[float, Field(gt=0, le=5.0, description="Sine output amplitude in V rms")],
) -> LockinSettings:
    """Set the SINE OUT amplitude (V rms), which drives the sample or excitation circuit.
    Refused above the `max_amplitude_v` safety limit. Tell the user the new drive level first."""
    server.check("max_amplitude_v", amplitude_v, "sine output amplitude")
    lockin = server.driver
    spec = lockin.spec
    if not spec.min_amplitude_v <= amplitude_v <= spec.max_amplitude_v:
        raise InstrumentProtocolError(
            f"The {spec.model} sine output range is {spec.min_amplitude_v:g} V to {spec.max_amplitude_v:g} V rms."
        )
    lockin.set_amplitude_v(amplitude_v)
    return _settings(lockin)


@mcp.tool(**SAFETY)
def set_amplitude_minimum() -> dict[str, float | str]:
    """Turn the sine output down to the instrument minimum (the lock-in's closest thing to
    'output off') and stop any running frequency sweep. SR810/SR830: 4 mV rms (it cannot go to
    zero - disconnect the cable if the sample must see no drive). SR86x: 1 nV rms and DC level 0 V."""
    out: dict[str, float | str] = dict(server.driver.set_amplitude_minimum())
    out["timestamp"] = _now()
    return out


@mcp.tool(**CONTROL)
def set_sensitivity(
    full_scale: Annotated[
        float, Field(gt=0, le=1.0, description="Largest signal to measure without overload")
    ],
    unit: Annotated[
        Literal["V", "A"], Field(description="V for voltage inputs; A for current inputs (1 V <-> 1 µA)")
    ] = "V",
    dynamic_reserve: Annotated[
        Literal["high_reserve", "normal", "low_noise"] | None,
        Field(description="SR810/SR830 only: dynamic reserve mode"),
    ] = None,
) -> LockinSettings:
    """Set the sensitivity (full-scale range). The smallest available range that is at least
    `full_scale` is chosen, so a signal of that size does not overload. On SR810/SR830 the dynamic
    reserve can be set at the same time."""
    lockin = server.driver
    volts = full_scale * 1e6 if unit == "A" else full_scale
    try:
        idx = sensitivity_index(lockin.spec, volts)
    except ValueError as exc:
        raise InstrumentProtocolError(str(exc)) from exc
    with lockin.t.lock:
        if dynamic_reserve is not None:
            lockin.set_reserve(dynamic_reserve)
        lockin.set_sensitivity_index(idx)
        return _settings(lockin)


@mcp.tool(**CONTROL)
def set_time_constant(
    time_constant_s: Annotated[float, Field(ge=1e-6, le=30e3, description="Output filter time constant")],
    filter_slope_db_oct: Annotated[
        Literal[6, 12, 18, 24] | None, Field(description="Low-pass filter slope in dB/octave")
    ] = None,
    sync_filter: Annotated[bool | None, Field(description="Synchronous filter on/off")] = None,
) -> LockinSettings:
    """Set the output filter time constant (nearest available value), and optionally the filter
    slope and synchronous filter. The SR830 may raise a too-short time constant to its minimum for
    the current slope/reserve; the returned settings show the value actually used."""
    lockin = server.driver
    idx = time_constant_index(lockin.spec, time_constant_s)
    with lockin.t.lock:
        if filter_slope_db_oct is not None:
            lockin.set_filter_slope_db(filter_slope_db_oct)
        lockin.set_time_constant_index(idx)
        if sync_filter is not None:
            lockin.set_sync_filter(sync_filter)
        return _settings(lockin)


@mcp.tool(**CONTROL)
def set_input(
    configuration: Annotated[
        Literal["A", "A-B", "I_1MOhm", "I_100MOhm"] | None,
        Field(description="Single-ended A, differential A-B, or current input with 1 MΩ / 100 MΩ gain"),
    ] = None,
    coupling: Annotated[Literal["AC", "DC"] | None, Field(description="Input coupling")] = None,
    shield: Annotated[Literal["float", "ground"] | None, Field(description="Input shield grounding")] = None,
    input_range_v: Annotated[
        Literal[1.0, 0.3, 0.1, 0.03, 0.01] | None, Field(description="SR86x voltage input range")
    ] = None,
) -> LockinSettings:
    """Configure the signal input: A / A-B / current, AC or DC coupling, shield float/ground and
    (SR86x) the voltage input range. Use DC coupling below ~160 mHz. Omitted parameters are left
    unchanged."""
    lockin = server.driver
    with lockin.t.lock:
        if configuration is not None:
            lockin.set_input_configuration(configuration)
        if coupling is not None:
            lockin.set_coupling(coupling)
        if shield is not None:
            lockin.set_shield(shield)
        if input_range_v is not None:
            lockin.set_input_range_v(input_range_v)
        return _settings(lockin)


@mcp.tool(**CONTROL, timeout=120)
def auto_phase() -> LockinSettings:
    """Run Auto Phase (APHS): shift the reference phase so that Y ≈ 0 and X ≈ R. The outputs then
    need several time constants to settle. Does nothing if the phase is unstable."""
    lockin = server.driver
    lockin.auto_phase()
    return _settings(lockin)


@mcp.tool(**CONTROL, timeout=120)
def auto_gain() -> LockinSettings:
    """Pick the sensitivity automatically for the present signal (SR830 AGAN / SR86x ASCL).
    On the SR830 AGAN does nothing when the time constant is longer than 1 s."""
    lockin = server.driver
    lockin.auto_gain()
    return _settings(lockin)


@mcp.tool(**CONTROL, timeout=120)
def auto_range() -> LockinSettings:
    """Optimise the input stage for the present signal: SR830 Auto Reserve (ARSV) or SR86x
    Auto Range of the voltage input range (ARNG)."""
    lockin = server.driver
    lockin.auto_range()
    return _settings(lockin)


#: ``frequency_sweep`` tool timeout. The sweep stops a minute before it, so it always returns its
#: data rather than being cut off, whatever ``max_sweep_duration_s`` is.
_SWEEP_TIMEOUT_S = 3700
_SWEEP_BUDGET_S = _SWEEP_TIMEOUT_S - 60


@mcp.tool(**HAZARD, timeout=_SWEEP_TIMEOUT_S)
def frequency_sweep(
    start_hz: Annotated[float, Field(gt=0, le=4e6, description="First frequency")],
    stop_hz: Annotated[float, Field(gt=0, le=4e6, description="Last frequency")],
    points: Annotated[int, Field(ge=2, le=1000, description="Number of frequencies")] = 51,
    log_spacing: Annotated[bool, Field(description="Logarithmic instead of linear spacing")] = False,
    settle_time_constants: Annotated[
        float | None,
        Field(
            ge=1, le=50, description="Wait per point in time constants (default: 99 % settling for the slope)"
        ),
    ] = None,
    restore_frequency: Annotated[
        bool, Field(description="Return to the starting frequency afterwards")
    ] = True,
    save_path: Annotated[str | None, Field(description="Optional CSV file for the full sweep")] = None,
) -> SweepResult:
    """Step the internal reference (and SINE OUT drive) from start_hz to stop_hz, waiting for the
    output filter to settle at each point, and record X/Y/R/θ vs frequency - e.g. a resonance or
    transfer-function measurement. The drive amplitude stays at its present value. Needs the
    internal reference. The estimated duration must be within `max_sweep_duration_s`;
    `set_amplitude_minimum` stops a running sweep. `save_path` must be a new .csv file."""
    lockin = server.driver
    spec = lockin.spec
    # Clear the stop flag before anything else: a set_amplitude_minimum from now on must stop
    # this sweep, even one that arrives while the settings below are being read.
    lockin.abort.clear()
    with lockin.t.lock:
        if not lockin.reference_internal():
            raise InstrumentProtocolError("A frequency sweep needs the internal reference (set_reference).")
        harmonic = lockin.harmonic()
        amplitude = lockin.amplitude_v()
        tc = lockin.time_constant_s()
        slope = lockin.filter_slope_db()
        f_initial = lockin.frequency_hz()
        unit = _unit(lockin)
    server.check("max_amplitude_v", amplitude, "present sine output amplitude used by the sweep")
    lo, hi = min(start_hz, stop_hz), max(start_hz, stop_hz)
    if lo < spec.min_frequency_hz or hi * harmonic > spec.max_frequency_hz:
        raise InstrumentProtocolError(
            f"Sweep {lo:g}-{hi:g} Hz at harmonic {harmonic} is outside the {spec.model} range "
            f"({spec.min_frequency_hz:g} Hz to {spec.max_frequency_hz:g} Hz detection frequency)."
        )
    n_tc = settle_time_constants if settle_time_constants is not None else SETTLE_TIME_CONSTANTS[slope]
    settle_s = n_tc * tc
    estimate = points * (settle_s + 0.05)
    server.check("max_sweep_duration_s", estimate, "estimated sweep duration")
    if estimate > _SWEEP_BUDGET_S:
        raise InstrumentError(
            f"Refused: the estimated sweep duration of {estimate:.0f} s exceeds the {_SWEEP_BUDGET_S} s one "
            "call can take. Use fewer points or a shorter time constant, or split the range. Nothing was sent."
        )
    # Validate the output file before the sweep, so a bad path cannot waste the measurement.
    path = prepare_save_path(save_path, suffixes=(".csv",)) if save_path else None
    if log_spacing:
        ratio = math.log(stop_hz / start_hz)
        freqs = [start_hz * math.exp(ratio * i / (points - 1)) for i in range(points)]
    else:
        freqs = [start_hz + (stop_hz - start_hz) * i / (points - 1) for i in range(points)]
    warnings: list[str] = []
    if tc < 5.0 / (lo * harmonic):
        warnings.append(
            f"Time constant {tc:g} s is short compared with the period at {lo:g} Hz; the outputs will "
            "carry 2f ripple. Consider a longer time constant or the sync filter."
        )

    started = _now()
    t0 = time.monotonic()
    deadline = t0 + _SWEEP_BUDGET_S
    data: list[SweepPoint] = []
    stop_reason: str | None = None
    try:
        for f in freqs:
            if time.monotonic() + settle_s > deadline:
                stop_reason = "Sweep stopped early: it would not finish within the tool's time budget."
                break
            with lockin.t.lock:  # set_amplitude_minimum sets the flag, then needs this lock
                if lockin.abort.is_set():
                    stop_reason = "Sweep stopped early by set_amplitude_minimum."
                    break
                lockin.set_frequency_hz(f)
            if lockin.abort.wait(settle_s):
                stop_reason = "Sweep stopped early by set_amplitude_minimum."
                break
            snap = lockin.snapshot()
            data.append(SweepPoint(frequency_hz=f, x=snap.x, y=snap.y, r=snap.r, theta_deg=snap.theta_deg))
    finally:
        if restore_frequency:
            lockin.set_frequency_hz(f_initial)
    completed = stop_reason is None
    if stop_reason:
        warnings.append(stop_reason)
    over = lockin.overloads()
    if over:
        warnings.append("Overload during sweep: " + "; ".join(over))
    saved = None
    if path is not None and data:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["frequency_hz", f"x_{unit}", f"y_{unit}", f"r_{unit}", "theta_deg"])
            for p in data:
                writer.writerow([p.frequency_hz, p.x, p.y, p.r, p.theta_deg])
        saved = str(path)
    peak = max(data, key=lambda p: abs(p.r)) if data else None
    return SweepResult(
        points=data,
        unit=unit,
        count=len(data),
        completed=completed,
        settle_time_per_point_s=settle_s,
        duration_s=time.monotonic() - t0,
        peak_frequency_hz=peak.frequency_hz if peak else None,
        peak_r=peak.r if peak else None,
        amplitude_v=amplitude,
        time_constant_s=tc,
        warnings=warnings,
        saved_to=saved,
        started=started,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
