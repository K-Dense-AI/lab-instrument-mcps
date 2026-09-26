"""MCP server for Atlas Scientific EZO sensor circuits (UART)."""

from __future__ import annotations

import csv
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Annotated

from labmcp import (
    CONTROL,
    READ,
    ConnectContext,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    prepare_save_path,
)
from pydantic import BaseModel, Field

from labmcp_atlas_ezo.driver import (
    CAL_MEANING,
    CAL_POINTS,
    RESTART_CODES,
    SENSOR_TYPES,
    TEMPERATURE_COMPENSATED,
    EZOCircuit,
    Reading,
)
from labmcp_atlas_ezo.simulator import EZOSimulator

#: One `log_series` call must finish inside its tool timeout, whatever `max_series_duration_s` says.
HARD_MAX_SERIES_S = 3600.0
SERIES_TOOL_TIMEOUT_S = 3900.0  # series + up to 1000 readings of ~1-3 s that may lag the schedule


def connect(ctx: ConnectContext) -> EZOCircuit:
    sim_sensor = ctx.option("sim_sensor", "pH") or "pH"
    # Factory UART settings (all EZO datasheets): 9600 baud, 8N1, no flow control, CR.
    transport = ctx.open_transport(
        simulator=lambda: EZOSimulator(sim_sensor),
        baudrate=9600,
        read_termination="\r",
        write_termination="\r",
        encoding="latin-1",  # DO's salinity query contains a micro sign; never fail to decode it
        timeout=1.5,
    )
    circuit = EZOCircuit(transport)
    try:
        circuit.initialise()
    except Exception:
        transport.close()
        raise
    return circuit


server = InstrumentServer(
    "Atlas Scientific EZO Sensor (UART)",
    connect=connect,
    package="labmcp-atlas-ezo",
    instructions="""
Reads one Atlas Scientific EZO sensor circuit over UART: pH, ORP, dissolved oxygen (DO),
conductivity (EC), RTD temperature, humidity (HUM), CO2 or pressure (PRS). The circuit type is
detected automatically; `get_info` reports it with its firmware, calibration and settings.
- `read_value` returns each output with its unit (pH, mV, mg/L and % saturation, µS/cm / ppm /
  PSU, °C, %RH, ppm CO2, psi ...). Values come from the probe as calibrated; check
  `get_calibration_status` before trusting them for a report.
- pH, EC and DO readings are temperature compensated to the value set with
  `set_temperature_compensation` (default 25 °C, 20 °C for DO). It is NOT retained when the
  circuit loses power, so set it again after a power cycle and whenever the sample temperature
  changes (e.g. from an RTD probe in the same vessel).
- Calibration changes how every later reading is computed. Only calibrate when the user says the
  probe is in the named standard (e.g. pH 7.00 buffer), after the reading has stabilised.
  pH: always `mid` first (it erases low/high), then `low`, then `high`. EC: `dry` first.
- `clear_calibration` erases the stored calibration: confirm with the user first.
""",
    limits=[Limit("max_series_duration_s", 600, "s", "Longest allowed logging series")],
    address_help="""\
  serial:///dev/ttyUSB0            USB-UART adapter / isolated carrier board (9600 8N1, CR)
  serial://COM5?baudrate=38400     Windows, circuit set to a non-default baud rate
  tcp://192.168.1.70:4001          serial-to-Ethernet adapter (raw TCP)""",
    option_help={"sim_sensor": "circuit type for --simulate: pH (default), ORP, DO, EC, RTD, HUM, CO2 or PRS"},
)
mcp = server.mcp


# ---------------------------------------------------------------- models


class MeasuredValue(BaseModel):
    name: str = Field(description="Output name, e.g. 'ph', 'do_mg_l', 'conductivity_us_cm'")
    value: float
    unit: str


class SensorReading(BaseModel):
    timestamp: str = Field(description="UTC time of the reading (ISO 8601)")
    sensor: str = Field(description="Circuit type, e.g. 'pH', 'Dissolved oxygen'")
    value: float = Field(description="The primary output (first value)")
    unit: str = Field(description="Unit of the primary output")
    values: list[MeasuredValue] = Field(description="Every enabled output of the circuit")
    temperature_compensation_c: float | None = Field(
        None, description="Temperature the reading is compensated to (pH, EC, DO only)"
    )
    raw: str
    warnings: list[str] = Field(default_factory=list)


class SensorInfo(BaseModel):
    sensor: str
    model: str
    firmware: str
    device_name: str | None
    restart_reason: str | None
    supply_voltage_v: float | None
    led_on: bool | None
    calibration_points: int | None
    calibration: str | None
    calibration_point_names: list[str]
    temperature_compensation_c: float | None
    enabled_outputs: list[str]
    probe_constant_k: float | None = Field(None, description="EC probe cell constant K")
    do_salinity: str | None = Field(None, description="DO salinity compensation")
    do_pressure_kpa: float | None = Field(None, description="DO atmospheric pressure compensation")
    notes: list[str]


class CalibrationStatus(BaseModel):
    sensor: str
    points: int
    meaning: str
    ph_acid_slope_percent: float | None = Field(None, description="pH only: acid slope vs ideal probe (%)")
    ph_base_slope_percent: float | None = Field(None, description="pH only: base slope vs ideal probe (%)")
    ph_zero_offset_mv: float | None = Field(None, description="pH only: zero-point offset (mV)")
    valid_points: list[str]
    advice: str


class CalibrationResult(BaseModel):
    point: str
    reference_value: float | None
    calibration_points: int
    meaning: str
    message: str


class SeriesStats(BaseModel):
    name: str
    unit: str
    mean: float
    stdev: float | None
    minimum: float
    maximum: float
    drift_per_min: float = Field(description="Least-squares slope vs time, in unit/min")


class SeriesPoint(BaseModel):
    t_s: float
    values: dict[str, float]


class SensorSeries(BaseModel):
    sensor: str
    count: int
    stats: list[SeriesStats]
    points: list[SeriesPoint]
    saved_to: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _to_model(r: Reading, temp: float | None) -> SensorReading:
    values = [MeasuredValue(name=k, value=v, unit=r.units.get(k, "")) for k, v in r.values.items()]
    return SensorReading(
        timestamp=_now(),
        sensor=r.sensor,
        value=values[0].value,
        unit=values[0].unit,
        values=values,
        temperature_compensation_c=temp,
        raw=r.raw,
        warnings=r.warnings,
    )


def _safe(fn, default=None):  # type: ignore[no-untyped-def]
    try:
        return fn()
    except InstrumentError:
        return default


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def read_value() -> SensorReading:
    """Take one reading (about 1 s) and return every enabled output with its unit: pH; ORP in mV;
    DO in mg/L (and % saturation if enabled); EC in µS/cm plus TDS (ppm), salinity (PSU) and
    specific gravity if enabled; RTD temperature; humidity %RH (+ air temperature, dew point);
    CO2 ppm; or pressure."""
    circuit = server.driver
    reading = circuit.read()
    return _to_model(reading, _safe(circuit.temperature_compensation))


@mcp.tool(**READ)
def get_info() -> SensorInfo:
    """Report the circuit type, firmware, device name, supply voltage and last restart reason,
    LED state, calibration state, temperature compensation and type-specific settings (enabled
    outputs, EC probe constant K, DO salinity/pressure compensation)."""
    c = server.driver
    notes: list[str] = []
    restart, vcc = _safe(c.status, (None, None))
    points = _safe(c.calibration_points)
    meaning = CAL_MEANING.get(c.sensor, {}).get(points, str(points)) if points is not None else None
    salinity = _safe(c.salinity) if c.sensor == "DO" else None
    if vcc is not None and not 3.2 <= vcc <= 5.4:
        notes.append(f"Supply voltage {vcc:g} V is outside the 3.3-5 V operating range.")
    if c.sensor in TEMPERATURE_COMPENSATED:
        notes.append("Temperature compensation is lost when the circuit is power cycled.")
    return SensorInfo(
        sensor=SENSOR_TYPES.get(c.sensor, c.sensor),
        model=f"EZO-{c.device_label}",
        firmware=c.firmware,
        device_name=_safe(c.name) or None,
        restart_reason=RESTART_CODES.get(restart, restart) if restart else None,
        supply_voltage_v=vcc,
        led_on=_safe(c.led),
        calibration_points=points,
        calibration=meaning,
        calibration_point_names=list(CAL_POINTS.get(c.sensor, {})),
        temperature_compensation_c=_safe(c.temperature_compensation),
        enabled_outputs=[f"{n} ({u})" if u else n for n, u in c.outputs()],
        probe_constant_k=_safe(c.probe_constant),
        do_salinity=f"{salinity[0]:g} {salinity[1]}" if salinity else None,
        do_pressure_kpa=_safe(c.pressure_kpa) if c.sensor == "DO" else None,
        notes=notes,
    )


_ADVICE = {
    "PH": "Calibrate mid (pH 7) first, then low (pH 4), then high (pH 10). Slopes within 95-105% "
          "indicate a healthy probe.",
    "ORP": "Single-point calibration in an ORP standard (e.g. 225 mV).",
    "DO": "Calibrate 'atmospheric' with the dry probe in air, optionally 'zero' in zero-oxygen solution.",
    "EC": "Calibrate 'dry' first, then 'single', or 'low' + 'high' bracketing the samples.",
    "RTD": "Single-point calibration against a reference thermometer or ice/boiling water.",
    "CO2": "Factory calibrated. Custom calibration only with certified gases (high point 3000-5000 ppm).",
    "PRS": "Factory calibrated. Optionally set zero at 0 gauge pressure, in the current units.",
    "HUM": "Factory calibrated; no user calibration.",
}


@mcp.tool(**READ)
def get_calibration_status() -> CalibrationStatus:
    """Report how many calibration points are stored and what that means for this circuit type;
    for pH also the probe slope (acid/base % of ideal and zero offset in mV)."""
    c = server.driver
    if c.sensor == "HUM":
        return CalibrationStatus(sensor=SENSOR_TYPES[c.sensor], points=0, meaning="factory calibrated",
                                 valid_points=[], advice=_ADVICE["HUM"])
    points = c.calibration_points()
    slope = c.slope() if points else None
    return CalibrationStatus(
        sensor=SENSOR_TYPES[c.sensor],
        points=points,
        meaning=CAL_MEANING.get(c.sensor, {}).get(points, str(points)),
        ph_acid_slope_percent=slope[0] if slope else None,
        ph_base_slope_percent=slope[1] if slope else None,
        ph_zero_offset_mv=slope[2] if slope else None,
        valid_points=list(CAL_POINTS.get(c.sensor, {})),
        advice=_ADVICE.get(c.sensor, ""),
    )


def _validate_calibration(c: EZOCircuit, point: str, value: float | None) -> None:
    s = c.sensor
    points = CAL_POINTS.get(s)
    if not points:
        raise InstrumentProtocolError(f"EZO-{c.device_label} has no user calibration.")
    if point not in points:
        raise InstrumentProtocolError(
            f"Invalid calibration point {point!r} for EZO-{c.device_label}. Valid points: {', '.join(points)}."
        )
    needs_value = points[point][1]
    if needs_value and value is None:
        raise InstrumentProtocolError(f"Calibration point {point!r} needs `value` (the standard's value).")
    if not needs_value and value is not None:
        raise InstrumentProtocolError(f"Calibration point {point!r} takes no value; omit `value`.")
    if value is None:
        return
    if not math.isfinite(value):
        raise InstrumentProtocolError(f"Refused: calibration value {value!r} is not a finite number.")
    ranges: dict[tuple[str, str], tuple[float, float, str]] = {
        ("PH", "mid"): (6.0, 8.0, "pH (use a pH 7 buffer)"),
        ("PH", "low"): (0.0, 6.5, "pH (use an acidic buffer, e.g. pH 4)"),
        ("PH", "high"): (7.5, 14.0, "pH (use a basic buffer, e.g. pH 10)"),
        ("ORP", "single"): (-1020.0, 1020.0, "mV"),
        ("EC", "single"): (0.07, 500000.0, "µS/cm"),
        ("EC", "low"): (0.07, 500000.0, "µS/cm"),
        ("EC", "high"): (0.07, 500000.0, "µS/cm"),
        ("CO2", "high"): (3000.0, 5000.0, "ppm (datasheet: high point 3000-5000 ppm)"),
    }
    if s == "RTD":
        to_c = {"c": value, "k": value - 273.15, "f": (value - 32) * 5 / 9}[c.rtd_scale]
        if not -126.0 <= to_c <= 1254.0:
            raise InstrumentProtocolError(f"RTD calibration temperature {value:g} is outside -126 to 1254 °C.")
        return
    lo, hi, what = ranges.get((s, point), (float("-inf"), float("inf"), ""))
    if not lo <= value <= hi:
        raise InstrumentProtocolError(f"Refused: {point} calibration value {value:g} must be {lo:g}-{hi:g} {what}.")


@mcp.tool(**CONTROL, timeout=30)
def calibrate(
    point: Annotated[
        str,
        Field(description="pH: mid|low|high; ORP: single; DO: atmospheric|zero; EC: dry|single|low|high; "
                          "RTD: single; CO2: zero|high; PRS: zero|high"),
    ],
    value: Annotated[
        float | None,
        Field(description="Value of the standard the probe is in (pH units, mV, µS/cm, temperature in the "
                          "RTD's current scale, ppm CO2, pressure in current units). Omit for DO "
                          "atmospheric/zero, EC dry and CO2/PRS zero."),
    ] = None,
) -> CalibrationResult:
    """Store one calibration point. The probe must already be in the named standard (buffer,
    air, dry, ...) with a stable reading. Point names are validated per circuit type. pH: a `mid`
    calibration erases existing low/high points, so always do mid first. EC: do `dry` first."""
    c = server.driver
    _validate_calibration(c, point, value)
    before = c.calibration_points()
    if c.sensor == "PH" and point in {"low", "high"} and before == 0:
        raise InstrumentProtocolError(
            "Refused: calibrate the pH midpoint first (`point='mid'`, pH 7 buffer). A later mid "
            "calibration would erase this point anyway."
        )
    c.calibrate(point, value)
    after = c.calibration_points()
    msg = f"Calibration point '{point}' stored."
    if c.sensor == "PH" and point == "mid" and before >= 2:
        msg += " Note: the mid calibration cleared the previous low/high points; redo them."
    return CalibrationResult(
        point=point,
        reference_value=value,
        calibration_points=after,
        meaning=CAL_MEANING.get(c.sensor, {}).get(after, str(after)),
        message=msg,
    )


@mcp.tool(**CONTROL)
def clear_calibration() -> CalibrationStatus:
    """Delete all stored calibration data (Cal,clear). pH/ORP/DO/EC/RTD return to uncalibrated;
    CO2/PRS return to their factory calibration. The probe must be recalibrated afterwards."""
    c = server.driver
    if c.sensor == "HUM":
        raise InstrumentProtocolError("EZO-HUM has no user calibration to clear.")
    c.clear_calibration()
    return get_calibration_status()


@mcp.tool(**CONTROL)
def set_temperature_compensation(
    temperature_c: Annotated[float, Field(ge=-10, le=130, description="Sample temperature in °C")],
) -> float:
    """Set the sample temperature used to compensate pH, EC or DO readings (T,n; always °C).
    Not retained through a power cycle. Returns the value the circuit now uses."""
    c = server.driver
    if c.sensor not in TEMPERATURE_COMPENSATED:
        raise InstrumentProtocolError(
            f"EZO-{c.device_label} has no temperature compensation (only pH, EC and DO circuits do)."
        )
    result = c.set_temperature_compensation(temperature_c)
    if result is None:
        raise InstrumentProtocolError("The circuit did not report its compensation temperature.")
    return result


@mcp.tool(**CONTROL)
def set_probe_constant(
    k: Annotated[float, Field(ge=0.01, le=100, description="Cell constant K of the EC probe (e.g. 0.1, 1.0, 10)")],
) -> float:
    """EC circuits only: set the conductivity probe's cell constant K to match the probe
    (printed on it). Recalibrate after changing it. Returns the K now in use."""
    c = server.driver
    if c.sensor != "EC":
        raise InstrumentProtocolError("The probe constant only applies to EZO-EC circuits.")
    result = c.set_probe_constant(k)
    if result is None:
        raise InstrumentProtocolError("The circuit did not report its probe constant.")
    return result


@mcp.tool(**CONTROL)
def set_do_compensation(
    salinity: Annotated[float | None, Field(ge=0, le=100000, description="Sample salinity (see salinity_unit)")] = None,
    salinity_unit: Annotated[str, Field(pattern="^(us_cm|ppt)$", description="'us_cm' (µS/cm) or 'ppt'")] = "us_cm",
    pressure_kpa: Annotated[float | None, Field(ge=50, le=200, description="Atmospheric pressure in kPa")] = None,
) -> SensorInfo:
    """DO circuits only: set salinity compensation (irrelevant below ~2500 µS/cm) and/or
    atmospheric pressure compensation (default 101.3 kPa; lower at altitude). Neither is
    retained through a power cycle."""
    c = server.driver
    if c.sensor != "DO":
        raise InstrumentProtocolError("Salinity/pressure compensation only applies to EZO-DO circuits.")
    if salinity is None and pressure_kpa is None:
        raise InstrumentProtocolError("Give `salinity` and/or `pressure_kpa`.")
    if salinity is not None:
        if salinity_unit == "ppt" and salinity > 42:
            raise InstrumentProtocolError("Salinity in ppt must be 0-42.")
        c.set_salinity(salinity, ppt=salinity_unit == "ppt")
    if pressure_kpa is not None:
        c.set_pressure_kpa(pressure_kpa)
    return get_info()


@mcp.tool(**CONTROL)
def set_led(on: Annotated[bool, Field(description="True = LED on (default), False = off (e.g. light-sensitive cultures)")]) -> bool:
    """Turn the circuit's status LED on or off. Returns the new LED state."""
    c = server.driver
    c.set_led(on)
    return c.led()


@mcp.tool(**READ, timeout=SERIES_TOOL_TIMEOUT_S)
def log_series(
    count: Annotated[int, Field(ge=2, le=1000, description="Number of readings")] = 10,
    interval_s: Annotated[float, Field(ge=1.0, le=600, description="Seconds between readings (>= 1)")] = 2.0,
    save_path: Annotated[
        str | None, Field(description="Optional new .csv file for the full series (never overwritten)")
    ] = None,
) -> SensorSeries:
    """Record a series of readings (e.g. to watch a probe stabilise before calibrating, follow pH
    or DO in a bioreactor, or log temperature). Returns every point plus mean, stdev, min, max
    and drift per minute for each output."""
    duration = (count - 1) * interval_s
    server.check("max_series_duration_s", duration, "series duration")
    if duration > HARD_MAX_SERIES_S:
        raise InstrumentProtocolError(
            f"A {duration:g} s series cannot run in a single tool call (maximum {HARD_MAX_SERIES_S:g} s). "
            "Split it into several log_series calls."
        )
    path = prepare_save_path(save_path, suffixes=(".csv",)) if save_path else None  # before any reading
    c = server.driver
    t0 = time.monotonic()
    points: list[SeriesPoint] = []
    units: dict[str, str] = {}
    stamps: list[str] = []
    for i in range(count):
        delay = t0 + i * interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        r = c.read()
        units.update(r.units)
        stamps.append(_now())
        points.append(SeriesPoint(t_s=round(time.monotonic() - t0, 3), values=r.values))
    stats = []
    for name, unit in units.items():
        pairs = [(p.t_s, p.values[name]) for p in points if name in p.values]
        if not pairs:
            continue
        ts, vs = zip(*pairs, strict=True)
        tbar, vbar = statistics.fmean(ts), statistics.fmean(vs)
        denom = sum((t - tbar) ** 2 for t in ts)
        slope = sum((t - tbar) * (v - vbar) for t, v in pairs) / denom if denom else 0.0
        stats.append(SeriesStats(name=name, unit=unit, mean=vbar, stdev=statistics.stdev(vs) if len(vs) > 1 else None,
                                 minimum=min(vs), maximum=max(vs), drift_per_min=slope * 60.0))
    saved = None
    if path is not None:
        names = list(units)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "t_s"] + [f"{n} ({units[n]})" if units[n] else n for n in names])
            for stamp, p in zip(stamps, points, strict=True):
                writer.writerow([stamp, p.t_s] + [p.values.get(n) for n in names])
        saved = str(path)
    return SensorSeries(sensor=SENSOR_TYPES[c.sensor], count=count, stats=stats, points=points, saved_to=saved)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
