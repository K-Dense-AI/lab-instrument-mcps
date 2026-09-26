"""MCP server for Stanford Research Systems RGA100 / RGA200 / RGA300 residual gas analyzers."""

from __future__ import annotations

import csv
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
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

from labmcp_srs_rga.analysis import find_peaks, identify_gases
from labmcp_srs_rga.driver import (
    NF_BASELINE_NOISE_A,
    NF_SCAN_MS_PER_AMU,
    NF_SINGLE_MASS_MS,
    PressureEvidence,
    ScanResult,
    SrsRga,
)
from labmcp_srs_rga.simulator import RGASimulator


def connect(ctx: ConnectContext) -> SrsRga:
    scenario = ctx.option("scenario", "normal") or "normal"
    model = int(ctx.option("sim_model", "200") or 200)
    # Fixed RS-232 settings (manual p. 6-6): 28,800 baud, 8 data bits, no parity, 1 stop bit, RTS/CTS.
    # Commands end with CR; ASCII replies end with LF CR (the LF is stripped by the driver).
    transport = ctx.open_transport(
        simulator=lambda: RGASimulator(model=model, scenario=scenario, seed=None),
        baudrate=28800,
        bytesize=8,
        parity="N",
        stopbits=1,
        rtscts=True,
        read_termination="\r",
        write_termination="\r",
        timeout=5.0,
    )
    rga = SrsRga(transport)
    user = ctx.option("user")
    if user and not ctx.simulate:
        rga.login(user, ctx.option("password", "") or "")
    # Clear the RGA's buffers (a scan may still be streaming) and run its hardware self-check.
    transport.write_bytes(b"\r\r")
    transport.flush_input()
    rga.identify()
    rga.initialize(0)
    return rga


server = InstrumentServer(
    "SRS Residual Gas Analyzer (RGA100/200/300)",
    connect=connect,
    package="labmcp-srs-rga",
    instructions="""
Controls a Stanford Research Systems RGA100/200/300 quadrupole residual gas analyzer (m/z up to
100, 200 or 300) over its RS-232 command set.
- Call `get_status` first: it reports the filament emission, the CDEM (electron multiplier) state,
  scan settings and any error bytes (e.g. a filament shut down by the overpressure protection).
- Measurements need the filament ON (typically 1 mA). The filament must only be switched on below
  1e-4 Torr: `set_filament` needs either a reading from a separate vacuum gauge passed as
  `external_pressure_torr`, or (if the filament is already on) the RGA's own total pressure.
- The CDEM gives ~1000x more signal but is damaged by high pressure: `set_cdem` requires a
  pressure below `max_cdem_pressure_torr` (default 5e-6 Torr). Total pressure reads 0 while the
  CDEM is on, so check the pressure with the Faraday cup first.
- Partial pressures are N2-equivalent (the head stores one sensitivity factor, SP, for N2). Other
  gases have different sensitivities (H2 ~0.4x, He ~0.14x, Ar ~1.2x): treat values as estimates.
- Typical residual gases: H2 (2), He (4), CH4 (16), H2O (18/17), N2/CO (28), O2 (32), Ar (40),
  CO2 (44). N2 28 + O2 32 in a ~4:1 ratio with Ar 40 means an air leak.
- When finished, call `all_off` (filament, CDEM and RF off). Degassing shortens filament life;
  use it only when needed.
""",
    limits=[
        Limit(
            "max_filament_pressure_torr",
            1e-4,
            "Torr",
            "Highest pressure at which the filament may be switched on or the ionizer degassed",
        ),
        Limit(
            "max_cdem_pressure_torr", 5e-6, "Torr", "Highest pressure at which the CDEM may be switched on"
        ),
        Limit("max_cdem_voltage_v", 2000, "V", "Highest CDEM high voltage an agent may set"),
        Limit(
            "max_pressure_reading_age_s", 60, "s", "Oldest RGA total-pressure reading accepted as evidence"
        ),
        Limit("max_degas_minutes", 3, "min", "Longest ionizer degas an agent may start"),
        Limit("max_scan_duration_s", 900, "s", "Longest estimated scan duration"),
        Limit("max_leak_check_duration_s", 1800, "s", "Longest helium leak-check monitoring run"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0            RS-232 (fixed 28800 baud, 8N1, RTS/CTS hardware handshake)
  serial://COM3                    Windows
  tcp://192.168.1.50:4001          raw-TCP serial-to-Ethernet adapter (port as configured)
  tcp://192.168.1.50:818           SRS RGA Ethernet adapter: add --option user=admin --option password=...""",
    option_help={
        "user": "login name for the SRS RGA Ethernet adapter (REA); omit for raw-TCP adapters",
        "password": "password for the SRS RGA Ethernet adapter",
        "scenario": "simulator only: normal (default), helium_leak or overpressure",
        "sim_model": "simulator only: 100, 200 (default) or 300",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- models


class InstrumentStatus(BaseModel):
    model: str
    max_mass_amu: int
    serial: str
    firmware: str
    filament_on: bool
    emission_current_ma: float
    cdem_installed: bool
    cdem_on: bool
    cdem_voltage_v: float
    electron_energy_ev: int
    ion_energy_ev: int = Field(description="8 (low) or 12 (high)")
    focus_voltage_v: int = Field(description="Magnitude of the (negative) focus plate voltage")
    noise_floor: int = Field(description="0 (slowest, lowest noise) to 7 (fastest)")
    scan_ms_per_amu: float
    single_mass_ms: float
    scan_start_mass: int
    scan_stop_mass: int
    points_per_amu: int
    partial_sensitivity_ma_per_torr: float = Field(description="SP, N2 sensitivity stored in the head")
    total_sensitivity_ma_per_torr: float = Field(description="ST, stored in the head")
    cdem_gain: float | None = Field(description="Stored CDEM gain (MG), used to scale CDEM currents")
    status_byte: int
    errors: dict[str, list[str]] = Field(description="Decoded error bytes; empty when everything is OK")
    degas: str | None = Field(description="Degas cycle progress or last result, if any")
    timestamp: str


class TotalPressure(BaseModel):
    pressure_torr: float = Field(description="N2-equivalent total pressure (ion current / ST)")
    ion_current_a: float
    filament_on: bool
    timestamp: str
    notes: list[str] = []


class MassReading(BaseModel):
    mz: int
    ion_current_a: float
    partial_pressure_torr: float = Field(description="N2-equivalent partial pressure")


class MassMeasurement(BaseModel):
    readings: list[MassReading]
    detector: Literal["faraday_cup", "cdem"]
    noise_floor: int
    timestamp: str
    warnings: list[str] = []


class Peak(BaseModel):
    mz: float
    partial_pressure_torr: float
    ion_current_a: float
    likely_species: str | None = None


class Spectrum(BaseModel):
    kind: Literal["analog", "histogram"]
    start_mass: int
    stop_mass: int
    points_per_amu: int
    total_points: int
    noise_floor: int
    detector: Literal["faraday_cup", "cdem"]
    mz: list[float] = Field(description="Mass axis (downsampled to max_points)")
    partial_pressure_torr: list[float] = Field(description="N2-equivalent partial pressure (downsampled)")
    peaks: list[Peak] = Field(description="Peaks above the noise, strongest first")
    total_pressure_torr: float | None = Field(description="Measured at the end of the scan (null with CDEM)")
    sum_partial_pressures_torr: float
    duration_s: float
    saved_to: str | None = None
    timestamp: str
    warnings: list[str] = []


class LeakEvent(BaseModel):
    start_s: float
    end_s: float
    peak_torr: float
    rise_factor: float = Field(description="Peak / baseline")


class LeakCheck(BaseModel):
    mz: int
    count: int
    interval_s: float
    duration_s: float
    baseline_torr: float = Field(description="Median of the first readings (before spraying)")
    baseline_noise_torr: float
    max_torr: float
    threshold_torr: float
    leak_detected: bool
    events: list[LeakEvent]
    times_s: list[float] = Field(description="Seconds since start (downsampled)")
    partial_pressure_torr: list[float] = Field(
        description="He-uncorrected (N2-equivalent) values, downsampled"
    )
    helium_note: str
    saved_to: str | None = None
    started: str


class GasAssignment(BaseModel):
    species: str
    formula: str
    main_mz: int
    partial_pressure_torr: float = Field(description="Approximate, corrected for relative sensitivity")
    fraction: float = Field(description="Fraction of the summed identified pressure")
    evidence: str
    confidence: Literal["high", "medium", "low"]


class GasIdentification(BaseModel):
    species: list[GasAssignment]
    diagnosis: list[str]
    unassigned_peaks: list[int]
    caveats: list[str]
    spectrum_torr: dict[str, float] = Field(description="m/z -> N2-equivalent partial pressure used")
    timestamp: str


class ActionResult(BaseModel):
    action: str
    status_byte: int
    filament_on: bool
    emission_current_ma: float
    cdem_on: bool
    cdem_voltage_v: float
    pressure_evidence: str | None = None
    message: str
    timestamp: str


# ---------------------------------------------------------------- helpers


def _cdem_gain(rga: SrsRga) -> float | None:
    if not rga.has_cdem() or rga.cdem_voltage_v() <= 10:
        return None
    gain = rga.cdem_stored_gain()
    return gain if gain >= 1 else None


def _sensitivity(rga: SrsRga) -> tuple[float, float | None]:
    return rga.partial_sensitivity_ma_per_torr(), _cdem_gain(rga)


def _measure_total(rga: SrsRga) -> TotalPressure:
    with rga.t.lock:
        emission = rga.emission_ma()
        if rga.has_cdem() and rga.cdem_voltage_v() > 10:
            raise InstrumentProtocolError(
                "The CDEM is on: the RGA disables total-pressure measurement to protect it (TP? returns "
                "0). Call `set_cdem` with voltage 0 / `cdem_off` first, or use `measure_masses`."
            )
        rga.enable_total_pressure(True)
        raw = rga.total_pressure_raw()
        st = rga.total_sensitivity_ma_per_torr()
    torr = SrsRga.to_torr(raw, st)
    notes = []
    if emission <= 0:
        notes.append("Filament is OFF: there is no ionisation, so this value is only electrometer noise.")
    elif raw > 0:
        rga.last_total_pressure = PressureEvidence(torr, f"RGA total pressure {torr:.2e} Torr")
    notes.append("N2-equivalent: the RGA total-pressure sensitivity is strongly gas dependent.")
    return TotalPressure(
        pressure_torr=torr,
        ion_current_a=SrsRga.current_a(raw),
        filament_on=emission > 0,
        timestamp=_now(),
        notes=notes,
    )


def _pressure_evidence(
    rga: SrsRga, external_pressure_torr: float | None, allow_fresh_tp: bool
) -> PressureEvidence:
    """Pick the pressure used for the switch-on check: a user-confirmed gauge value, a fresh RGA
    total pressure (filament on, CDEM off), or a recent one."""
    if external_pressure_torr is not None:
        return PressureEvidence(
            external_pressure_torr, f"external gauge reading {external_pressure_torr:.2e} Torr"
        )
    if allow_fresh_tp:
        tp = _measure_total(rga)
        if tp.filament_on and tp.ion_current_a > 0:
            return PressureEvidence(
                tp.pressure_torr, f"RGA total pressure {tp.pressure_torr:.2e} Torr (just measured)"
            )
    cached = rga.last_total_pressure
    if cached is not None:
        age = time.monotonic() - cached.monotonic
        if age <= server.limits["max_pressure_reading_age_s"]:
            return PressureEvidence(
                cached.torr, f"{cached.source}, measured {age:.0f} s ago", cached.monotonic
            )
    raise InstrumentProtocolError(
        "Refused: no trustworthy pressure reading. The RGA can only measure pressure while its filament "
        "is emitting (and with the CDEM off). Read a separate vacuum gauge (ion/Pirani/cold cathode) and "
        "pass the value as `external_pressure_torr` after confirming it with the user. Nothing was sent."
    )


def _action_result(
    rga: SrsRga, action: str, status: int, message: str, evidence: str | None = None
) -> ActionResult:
    emission = rga.emission_ma()
    hv = rga.cdem_voltage_v() if rga.has_cdem() else 0.0
    return ActionResult(
        action=action,
        status_byte=status,
        filament_on=emission > 0,
        emission_current_ma=emission,
        cdem_on=hv > 10,
        cdem_voltage_v=hv,
        pressure_evidence=evidence,
        message=message,
        timestamp=_now(),
    )


def _health_warnings(rga: SrsRga) -> list[str]:
    warnings = []
    status = rga.status_byte()
    if status:
        for name, msgs in rga.error_details(status).items():
            warnings.append(f"{name}: {', '.join(msgs)}")
    if rga.emission_ma() <= 0:
        warnings.append(
            "Filament is OFF: the data is electrometer noise only. Switch it on with `set_filament`."
        )
    return warnings


def _downsample(xs: list[float], ys: list[float], max_points: int) -> tuple[list[float], list[float]]:
    """Min/max-preserving downsampling so narrow peaks survive."""
    if len(xs) <= max_points:
        return xs, ys
    buckets = max(1, max_points // 2)
    size = math.ceil(len(xs) / buckets)
    ox: list[float] = []
    oy: list[float] = []
    for i in range(0, len(xs), size):
        seg = list(range(i, min(i + size, len(xs))))
        lo = min(seg, key=lambda k: ys[k])
        hi = max(seg, key=lambda k: ys[k])
        for k in sorted({lo, hi}):
            ox.append(xs[k])
            oy.append(ys[k])
    return ox, oy


def _spectrum(
    rga: SrsRga,
    result: ScanResult,
    kind: Literal["analog", "histogram"],
    duration: float,
    max_points: int,
    save_path: str | None,
    warnings: list[str],
) -> Spectrum:
    sp, gain = _sensitivity(rga)
    masses = result.masses
    torr = [SrsRga.to_torr(r, sp, gain) for r in result.currents_raw]
    total = None
    if gain is None and result.total_raw:
        total = SrsRga.to_torr(result.total_raw, rga.total_sensitivity_ma_per_torr())
    noise = _noise_torr(result.noise_floor, sp, gain)
    peaks = [
        Peak(mz=m, partial_pressure_torr=p, ion_current_a=p / torr_per_a(sp, gain), likely_species=s)
        for m, p, s in find_peaks(masses, torr, noise_torr=noise, histogram=kind == "histogram")
    ]
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["mz", "ion_current_a", "partial_pressure_torr_n2_equivalent"])
            for m, r, p in zip(masses, result.currents_raw, torr, strict=True):
                w.writerow([m, f"{SrsRga.current_a(r):.4e}", f"{p:.4e}"])
            w.writerow(
                [
                    "total",
                    f"{SrsRga.current_a(result.total_raw):.4e}",
                    "" if total is None else f"{total:.4e}",
                ]
            )
        saved = str(path)
    dx, dy = _downsample(masses, torr, max_points)
    step_sum = sum(p for p in torr if p > 0) / (result.points_per_amu if kind == "analog" else 1)
    return Spectrum(
        kind=kind,
        start_mass=result.start_mass,
        stop_mass=result.stop_mass,
        points_per_amu=result.points_per_amu,
        total_points=len(masses),
        noise_floor=result.noise_floor,
        detector="cdem" if gain else "faraday_cup",
        mz=dx,
        partial_pressure_torr=dy,
        peaks=peaks,
        total_pressure_torr=total,
        sum_partial_pressures_torr=step_sum
        if kind == "histogram"
        else sum(p.partial_pressure_torr for p in peaks),
        duration_s=round(duration, 2),
        saved_to=saved,
        timestamp=_now(),
        warnings=warnings,
    )


def torr_per_a(sp: float, gain: float | None) -> float:
    return SrsRga.to_torr(10**16, sp, gain)


def _noise_torr(nf: int, sp: float, gain: float | None) -> float:
    """Typical baseline noise (manual electrometer table) expressed as a partial pressure."""
    return NF_BASELINE_NOISE_A[nf] * (3.0 if gain else 1.0) * torr_per_a(sp, gain)


def _check_mass(rga: SrsRga, *masses: int) -> None:
    for m in masses:
        if not 1 <= m <= rga.m_max:
            raise InstrumentProtocolError(f"This RGA{rga.m_max} measures m/z 1-{rga.m_max}; got {m}.")


# ---------------------------------------------------------------- READ tools


@mcp.tool(**READ)
def get_status() -> InstrumentStatus:
    """Report the RGA state: filament emission, CDEM on/off and voltage, ionizer settings, scan
    settings, stored sensitivity factors, and the decoded error bytes (e.g. FL6 when the
    overpressure protection shut the filament down). Reading the error bytes clears the
    communication (EC?) and CDEM (EM?) error bytes, as on the instrument."""
    rga = server.driver
    with rga.t.lock:
        info = rga.identify()
        emission = rga.emission_ma()
        cdem = rga.has_cdem()
        hv = rga.cdem_voltage_v() if cdem else 0.0
        nf = rga.noise_floor()
        mi, mf = rga.scan_range()
        status = rga.status_byte()
        errors = rga.error_details(status) if status else {}
        gain = rga.cdem_stored_gain() if cdem else None
        status_model = InstrumentStatus(
            model=str(info["model"]),
            max_mass_amu=rga.m_max,
            serial=str(info["serial"]),
            firmware=str(info["firmware"]),
            filament_on=emission > 0,
            emission_current_ma=emission,
            cdem_installed=cdem,
            cdem_on=hv > 10,
            cdem_voltage_v=hv,
            electron_energy_ev=rga.electron_energy_ev(),
            ion_energy_ev=12 if rga.ion_energy_high() else 8,
            focus_voltage_v=rga.focus_voltage_v(),
            noise_floor=nf,
            scan_ms_per_amu=NF_SCAN_MS_PER_AMU[nf],
            single_mass_ms=NF_SINGLE_MASS_MS[nf],
            scan_start_mass=mi,
            scan_stop_mass=mf,
            points_per_amu=rga.steps_per_amu(),
            partial_sensitivity_ma_per_torr=rga.partial_sensitivity_ma_per_torr(),
            total_sensitivity_ma_per_torr=rga.total_sensitivity_ma_per_torr(),
            cdem_gain=gain,
            status_byte=status,
            errors=errors,
            degas=None,
            timestamp=_now(),
        )
    if rga.finished_degas is not None:
        d = rga.finished_degas
        status_model.degas = (
            f"last degas ({d.minutes} min) finished with STATUS={d.last_status}"
            if d.last_status is not None
            else f"last degas ({d.minutes} min) finished; no STATUS byte was received"
        )
    return status_model


@mcp.tool(**READ)
def read_total_pressure() -> TotalPressure:
    """Measure the total pressure with the RGA acting as an ionisation gauge (TP?, Faraday cup).
    Needs the filament on and the CDEM off. The value is N2-equivalent and can differ from a
    Bayard-Alpert gauge by a factor of a few, depending on the gas composition."""
    return _measure_total(server.driver)


@mcp.tool(**READ, timeout=600)
def measure_masses(
    masses: Annotated[
        list[int],
        Field(min_length=1, max_length=50, description="m/z values to measure, e.g. [2, 18, 28, 32, 40, 44]"),
    ],
) -> MassMeasurement:
    """Measure partial pressures at a list of m/z values (peak-locked single-mass measurements,
    MR). Fast and precise for monitoring known gases; the precision and time per mass are set by
    the noise floor (`set_scan_parameters`). Pressures are N2-equivalent."""
    rga = server.driver
    with rga.t.lock:
        _check_mass(rga, *masses)
        sp, gain = _sensitivity(rga)
        nf = rga.noise_floor()
        raws = [rga.single_mass(m) for m in masses]
        rga.rf_off()
        warnings = _health_warnings(rga)
    readings = [
        MassReading(
            mz=m, ion_current_a=SrsRga.current_a(r), partial_pressure_torr=SrsRga.to_torr(r, sp, gain)
        )
        for m, r in zip(masses, raws, strict=True)
    ]
    return MassMeasurement(
        readings=readings,
        detector="cdem" if gain else "faraday_cup",
        noise_floor=nf,
        timestamp=_now(),
        warnings=warnings,
    )


def _check_scan_time(rga: SrsRga, start: int, stop: int, histogram: bool) -> None:
    nf = rga.noise_floor()
    est = rga.estimate_scan_s(start, stop, nf, histogram)
    server.check("max_scan_duration_s", est, f"estimated scan duration at noise floor {nf}")


@mcp.tool(**READ, timeout=1900)
def analog_scan(
    start_mass: Annotated[int, Field(ge=1, le=300, description="First m/z")] = 1,
    stop_mass: Annotated[int, Field(ge=1, le=300, description="Last m/z")] = 50,
    points_per_amu: Annotated[int, Field(ge=10, le=25, description="Steps per amu (SA)")] = 10,
    max_points: Annotated[int, Field(ge=20, le=5000, description="Spectrum points returned")] = 500,
    save_path: Annotated[str | None, Field(description="Optional CSV path for the full spectrum")] = None,
) -> Spectrum:
    """Record an analog mass spectrum (SC1): the quadrupole steps through the mass range and the
    full peak shapes are returned (downsampled) with a peak list, the total pressure measured at
    the end of the scan, and optionally the full data as CSV. Use it to check peak positions and
    to survey unknown gases. Duration depends on the noise floor (e.g. 126 ms/amu at NF4)."""
    rga = server.driver
    with rga.t.lock:
        _check_mass(rga, start_mass, stop_mass)
        if stop_mass <= start_mass:
            raise InstrumentProtocolError("stop_mass must be greater than start_mass for an analog scan.")
        _check_scan_time(rga, start_mass, stop_mass, histogram=False)
        t0 = time.monotonic()
        result = rga.analog_scan(start_mass, stop_mass, points_per_amu)
        duration = time.monotonic() - t0
        warnings = _health_warnings(rga)
        return _spectrum(rga, result, "analog", duration, max_points, save_path, warnings)


@mcp.tool(**READ, timeout=1900)
def histogram_scan(
    start_mass: Annotated[int, Field(ge=1, le=300, description="First m/z")] = 1,
    stop_mass: Annotated[int, Field(ge=1, le=300, description="Last m/z")] = 50,
    save_path: Annotated[str | None, Field(description="Optional CSV path")] = None,
) -> Spectrum:
    """Record a bar-graph spectrum (HS1): one peak-locked value per integer m/z plus the total
    pressure. Faster than an analog scan and the usual way to follow a residual gas composition."""
    rga = server.driver
    with rga.t.lock:
        _check_mass(rga, start_mass, stop_mass)
        if stop_mass < start_mass:
            raise InstrumentProtocolError("stop_mass must be >= start_mass.")
        _check_scan_time(rga, start_mass, stop_mass, histogram=True)
        t0 = time.monotonic()
        result = rga.histogram_scan(start_mass, stop_mass)
        duration = time.monotonic() - t0
        warnings = _health_warnings(rga)
        return _spectrum(rga, result, "histogram", duration, 1000, save_path, warnings)


@mcp.tool(**READ, timeout=3700)
def leak_check(
    duration_s: Annotated[float, Field(gt=0, le=7200, description="How long to monitor")] = 120,
    interval_s: Annotated[float, Field(ge=0.1, le=60, description="Seconds between readings")] = 1.0,
    mz: Annotated[int, Field(ge=1, le=300, description="Tracer gas m/z (4 = helium)")] = 4,
    threshold_factor: Annotated[
        float, Field(ge=1.5, le=1000, description="Signal/baseline ratio that counts as a leak response")
    ] = 3.0,
    baseline_points: Annotated[int, Field(ge=3, le=100, description="Readings used for the baseline")] = 5,
    max_points: Annotated[int, Field(ge=10, le=5000, description="Points returned")] = 300,
    save_path: Annotated[str | None, Field(description="Optional CSV file for every reading")] = None,
) -> LeakCheck:
    """Helium leak check: monitor m/z 4 while someone sprays helium on suspect joints, and report
    the baseline, every response above `threshold_factor` x baseline (start/end time, peak) and
    the time series. Keep the first few seconds free of helium so the baseline is clean. The
    response time of a real leak is a few seconds. Bounded by `max_leak_check_duration_s`."""
    server.check("max_leak_check_duration_s", duration_s, "leak-check duration")
    rga = server.driver
    started = _now()
    with rga.t.lock:
        _check_mass(rga, mz)
        sp, gain = _sensitivity(rga)
        nf = rga.noise_floor()
        per_reading = NF_SINGLE_MASS_MS[nf] / 1000.0
        if per_reading > interval_s:
            raise InstrumentProtocolError(
                f"interval_s={interval_s} is shorter than one measurement at noise floor {nf} "
                f"({per_reading:.2f} s). Use a longer interval or a higher noise floor."
            )
        n = int(math.floor(duration_s / interval_s + 1e-9)) + 1
        t0 = time.monotonic()
        times: list[float] = []
        values: list[float] = []
        for i in range(n):
            delay = t0 + i * interval_s - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            raw = rga.single_mass(mz)
            times.append(time.monotonic() - t0)
            values.append(SrsRga.to_torr(raw, sp, gain))
        rga.rf_off()
        warnings = _health_warnings(rga)
    nb = min(baseline_points, len(values))
    base_vals = values[:nb]
    baseline = statistics.median(base_vals)
    noise = statistics.pstdev(base_vals) if nb > 1 else 0.0
    floor = _noise_torr(nf, sp, gain)
    threshold = max(baseline * threshold_factor, baseline + 5 * max(noise, floor))
    events: list[LeakEvent] = []
    current: list[int] = []
    for k, v in enumerate(values):
        if v > threshold:
            current.append(k)
        elif current:
            events.append(_event(times, values, current, baseline))
            current = []
    if current:
        events.append(_event(times, values, current, baseline))
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s", f"mz{mz}_partial_pressure_torr_n2_equivalent"])
            for t, v in zip(times, values, strict=True):
                w.writerow([f"{t:.3f}", f"{v:.4e}"])
        saved = str(path)
    step = max(1, math.ceil(len(times) / max_points))
    note = (
        "Helium values are N2-equivalent: the RGA is ~7x less sensitive to He than to N2, so the true He "
        "partial pressure is roughly 7x higher. Use the relative rise, not the absolute value."
        if mz == 4
        else "Values are N2-equivalent partial pressures."
    )
    if warnings:
        note += " Warnings: " + " | ".join(warnings)
    return LeakCheck(
        mz=mz,
        count=len(values),
        interval_s=interval_s,
        duration_s=round(times[-1], 3),
        baseline_torr=baseline,
        baseline_noise_torr=noise,
        max_torr=max(values),
        threshold_torr=threshold,
        leak_detected=bool(events),
        events=events,
        times_s=[round(t, 3) for t in times[::step]],
        partial_pressure_torr=values[::step],
        helium_note=note,
        saved_to=saved,
        started=started,
    )


def _event(times: list[float], values: list[float], idx: list[int], baseline: float) -> LeakEvent:
    peak = max(values[k] for k in idx)
    return LeakEvent(
        start_s=round(times[idx[0]], 3),
        end_s=round(times[idx[-1]], 3),
        peak_torr=peak,
        rise_factor=peak / baseline if baseline > 0 else float("inf"),
    )


@mcp.tool(**READ, timeout=1900)
def identify_residual_gases(
    stop_mass: Annotated[int, Field(ge=20, le=300, description="Scan m/z 1 up to this mass")] = 50,
) -> GasIdentification:
    """Run a histogram scan and assign the major peaks to likely gases with a simple lookup of
    standard 70 eV fragment patterns: H2 (2), He (4), CH4 (16/15), H2O (18/17), N2 or CO (28,
    split with 14 and 12), O2 (32), Ar (40), CO2 (44), plus diagnoses such as an air leak or
    hydrocarbon contamination. A screening aid, not a quantitative analysis."""
    rga = server.driver
    with rga.t.lock:
        stop = min(stop_mass, rga.m_max)
        _check_scan_time(rga, 1, stop, histogram=True)
        result = rga.histogram_scan(1, stop)
        sp, gain = _sensitivity(rga)
        warnings = _health_warnings(rga)
    spectrum = {
        m: SrsRga.to_torr(r, sp, gain) for m, r in zip(range(1, stop + 1), result.currents_raw, strict=True)
    }
    noise = _noise_torr(result.noise_floor, sp, gain)
    assignments, diagnosis, unassigned, caveats = identify_gases(spectrum, noise)
    return GasIdentification(
        species=[GasAssignment(**a) for a in assignments],
        diagnosis=diagnosis + warnings,
        unassigned_peaks=unassigned,
        caveats=caveats,
        spectrum_torr={str(m): p for m, p in spectrum.items()},
        timestamp=_now(),
    )


# ---------------------------------------------------------------- CONTROL


@mcp.tool(**CONTROL)
def set_scan_parameters(
    noise_floor: Annotated[
        int | None, Field(ge=0, le=7, description="0 = slowest/lowest noise ... 7 = fastest (default 4)")
    ] = None,
    electron_energy_ev: Annotated[int | None, Field(ge=25, le=105, description="Default 70 eV")] = None,
    ion_energy: Annotated[
        Literal["low", "high"] | None, Field(description="low = 8 eV, high = 12 eV")
    ] = None,
    focus_voltage_v: Annotated[
        int | None, Field(ge=0, le=150, description="Focus plate, default 90 V")
    ] = None,
    points_per_amu: Annotated[
        int | None, Field(ge=10, le=25, description="Analog scan steps per amu")
    ] = None,
) -> InstrumentStatus:
    """Change measurement settings: noise floor (speed vs detection limit), electron energy, ion
    energy, focus plate voltage and analog-scan resolution. Sensitivity factors stored in the head
    are only valid at the default ionizer settings (70 eV, 12 eV, 90 V). After changing the noise
    floor the detector zero is re-adjusted automatically at the start of the next scan."""
    rga = server.driver
    with rga.t.lock:
        if noise_floor is not None:
            rga.set_noise_floor(noise_floor)
        if electron_energy_ev is not None:
            rga.set_electron_energy(electron_energy_ev)
        if ion_energy is not None:
            rga.set_ion_energy(ion_energy == "high")
        if focus_voltage_v is not None:
            rga.set_focus_voltage(focus_voltage_v)
        if points_per_amu is not None:
            rga.set_steps_per_amu(points_per_amu)
    return get_status()


# ---------------------------------------------------------------- HAZARD


@mcp.tool(**HAZARD, timeout=120)
def set_filament(
    emission_current_ma: Annotated[
        float, Field(ge=0.02, le=3.5, description="Electron emission current; 1.0 mA is the standard setting")
    ] = 1.0,
    external_pressure_torr: Annotated[
        float | None,
        Field(gt=0, description="Pressure just read on a separate vacuum gauge, confirmed by the user"),
    ] = None,
) -> ActionResult:
    """Switch the filament on (or change its emission current). The filament burns out or is
    damaged above 1e-4 Torr, so this is refused unless the pressure is below
    `max_filament_pressure_torr`: pass `external_pressure_torr` from a separate gauge, or, if the
    filament is already on, the RGA's own total pressure is measured. The RGA's protection will
    also shut the filament down on overpressure (reported as FL6)."""
    rga = server.driver
    with rga.t.lock:
        filament_on = rga.emission_ma() > 0
        cdem_on = rga.has_cdem() and rga.cdem_voltage_v() > 10
        evidence = _pressure_evidence(rga, external_pressure_torr, allow_fresh_tp=filament_on and not cdem_on)
        server.check(
            "max_filament_pressure_torr", evidence.torr, f"pressure ({evidence.source}) for filament on"
        )
        status = rga.set_emission(emission_current_ma)
        return _action_result(
            rga,
            "filament_on",
            status,
            f"Filament emission set to {emission_current_ma:.2f} mA.",
            evidence.source,
        )


@mcp.tool(**HAZARD, timeout=120)
def set_cdem(
    voltage_v: Annotated[
        int,
        Field(ge=10, le=2490, description="CDEM high voltage (magnitude); typical 1000-1600 V, default 1400"),
    ] = 1400,
    external_pressure_torr: Annotated[
        float | None,
        Field(gt=0, description="Pressure just read on a separate vacuum gauge, confirmed by the user"),
    ] = None,
) -> ActionResult:
    """Switch the electron multiplier (CDEM) on at a given high voltage for ~100-10000x more
    signal. High pressure shortens CDEM life and can destroy it, so this is refused unless the
    pressure is below `max_cdem_pressure_torr` (RGA total pressure measured now with the Faraday
    cup, a recent one, or `external_pressure_torr`), and the voltage is capped by
    `max_cdem_voltage_v`. Total-pressure readings are disabled while the CDEM is on."""
    server.check("max_cdem_voltage_v", voltage_v, "CDEM high voltage")
    rga = server.driver
    with rga.t.lock:
        if not rga.has_cdem():
            raise InstrumentProtocolError(
                "This RGA has no electron multiplier (CDEM option 01 not installed)."
            )
        filament_on = rga.emission_ma() > 0
        cdem_on = rga.cdem_voltage_v() > 10
        evidence = _pressure_evidence(rga, external_pressure_torr, allow_fresh_tp=filament_on and not cdem_on)
        server.check("max_cdem_pressure_torr", evidence.torr, f"pressure ({evidence.source}) for CDEM on")
        status = rga.set_cdem_voltage(voltage_v)
        return _action_result(
            rga,
            "cdem_on",
            status,
            f"CDEM on at {voltage_v} V. Consider `calibrate` (zero) since the detector changed.",
            evidence.source,
        )


@mcp.tool(**HAZARD)
def degas(
    minutes: Annotated[int, Field(ge=1, le=20, description="Degas time incl. the 1-minute ramp")] = 3,
    external_pressure_torr: Annotated[
        float | None,
        Field(gt=0, description="Pressure just read on a separate vacuum gauge, confirmed by the user"),
    ] = None,
) -> ActionResult:
    """Start an ionizer degas (DG): 20 mA of 400 eV electrons clean the ion source by electron
    stimulated desorption. The CDEM is switched off and left off. Degassing shortens filament
    life; prefer a bakeout. Refused above `max_filament_pressure_torr` and beyond
    `max_degas_minutes`. Returns immediately; the RGA is busy until it finishes (any command would
    abort it), and `filament_off` / `all_off` stop it."""
    server.check("max_degas_minutes", minutes, "degas time")
    rga = server.driver
    with rga.t.lock:
        filament_on = rga.emission_ma() > 0
        cdem_on = rga.has_cdem() and rga.cdem_voltage_v() > 10
        evidence = _pressure_evidence(rga, external_pressure_torr, allow_fresh_tp=filament_on and not cdem_on)
        server.check("max_filament_pressure_torr", evidence.torr, f"pressure ({evidence.source}) for degas")
        fil_err = rga.filament_error_byte()
        if fil_err & 0xFE:
            raise InstrumentProtocolError(
                f"Refused: the filament has an error (FIL_ERR={fil_err}); the RGA would not degas. "
                "Switch the filament on successfully first."
            )
        result = _action_result(
            rga,
            "degas_started",
            0,
            f"Degas started for {minutes} min (1 min ramp to 20 mA). Do not use the RGA until it finishes "
            f"at about {(datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec='seconds')}; "
            "call `get_status` afterwards to see the result.",
            evidence.source,
        )
        rga.start_degas(minutes)
    return result


@mcp.tool(**HAZARD, timeout=300)
def calibrate(
    kind: Annotated[
        Literal["zero", "electrometer"],
        Field(
            description="zero = CA (re-zero detector + mass axis correction); electrometer = CL (full I-V)"
        ),
    ] = "zero",
) -> ActionResult:
    """Calibrate the detector: `zero` (CA) re-zeroes the ion detector at the present noise floor
    and detector and corrects the RF scan table for temperature drift (seconds); `electrometer`
    (CL) recalibrates the electrometer's full I-V response (longer, clears all zero offsets). The
    quadrupole RF is switched off at the end."""
    rga = server.driver
    with rga.t.lock:
        status = rga.calibrate_all() if kind == "zero" else rga.calibrate_electrometer()
        return _action_result(rga, f"calibrate_{kind}", status, "Calibration completed.")


# ---------------------------------------------------------------- SAFETY


@mcp.tool(**SAFETY, timeout=90)
def filament_off() -> ActionResult:
    """Switch the filament off (FL0), stopping a degas first if one is running. Always allowed."""
    rga = server.driver
    with rga.t.lock:
        stopped = rga.stop_degas()
        status = rga.status_command("FL0", timeout=60.0, check=False)
        msg = "Filament off." + (" A running degas was aborted." if stopped else "")
        return _action_result(rga, "filament_off", status, msg)


@mcp.tool(**SAFETY, timeout=90)
def cdem_off() -> ActionResult:
    """Switch the electron multiplier off (HV0) and return to Faraday-cup detection. Always allowed."""
    rga = server.driver
    with rga.t.lock:
        rga.stop_degas()
        if not rga.has_cdem():
            return _action_result(rga, "cdem_off", 0, "No CDEM installed; nothing to do.")
        status = rga.status_command("HV0", timeout=60.0, check=False)
        return _action_result(rga, "cdem_off", status, "CDEM off, Faraday cup active.")


@mcp.tool(**SAFETY, timeout=120)
def all_off() -> ActionResult:
    """Put the RGA in a safe state: abort any degas, CDEM off (HV0), filament off (FL0)
    and quadrupole RF/DC off (MR0). Use when finished, before venting, or if anything looks wrong."""
    rga = server.driver
    with rga.t.lock:
        stopped = rga.stop_degas()
        rga.t.flush_input()
        statuses = []
        if rga.has_cdem():
            statuses.append(rga.status_command("HV0", timeout=60.0, check=False))
        statuses.append(rga.status_command("FL0", timeout=60.0, check=False))
        rga.rf_off()
        status = 0
        for s in statuses:
            status |= s
        msg = "CDEM, filament and RF are off." + (" A running degas was aborted." if stopped else "")
        if status:
            msg += f" STATUS={status}: call `get_status` for the error details."
        return _action_result(rga, "all_off", status, msg)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
