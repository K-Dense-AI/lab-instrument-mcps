"""MCP server for Thorlabs optical power meters (PM100 / PM400 SCPI command set)."""

from __future__ import annotations

import csv
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from labmcp import CONTROL, READ, ConnectContext, InstrumentProtocolError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_thorlabs_pm.driver import ThorlabsPowerMeter, format_power, watts_to_dbm
from labmcp_thorlabs_pm.simulator import SENSORS, ThorlabsPMSimulator


def connect(ctx: ConnectContext) -> ThorlabsPowerMeter:
    channel_opt = ctx.option("channel")
    channel = int(channel_opt) if channel_opt else None
    if channel is not None and channel not in (1, 2):
        raise InstrumentProtocolError(f"--option channel must be 1 or 2 (got {channel_opt!r}).")
    sensor = ctx.option("sim_sensor", "S120C") or "S120C"
    if sensor not in SENSORS:
        raise InstrumentProtocolError(f"--option sim_sensor must be one of {sorted(SENSORS)}.")
    transport = ctx.open_transport(
        simulator=lambda: ThorlabsPMSimulator(sensor=sensor),
        read_termination="\n",
        write_termination="\n",
        timeout=5.0,
    )
    return ThorlabsPowerMeter(transport, channel=channel)


server = InstrumentServer(
    "Thorlabs Optical Power Meter (SCPI)",
    connect=connect,
    package="labmcp-thorlabs-pm",
    instructions="""
Reads a Thorlabs optical power meter console (PM100D, PM100A, PM100USB, PM400; PM101/PM5020
use the same commands) with a C-series photodiode or thermal sensor. The meter only measures;
it never emits light.
- ALWAYS set the wavelength (`set_wavelength`) to the source wavelength before trusting a
  reading: photodiode responsivity varies strongly with wavelength (thermal sensors much less).
- Zero the sensor (`zero_sensor`) only after the user confirms the beam is blocked / the sensor
  is capped; zeroing with light on the sensor makes every later reading wrong.
- Keep auto-ranging on unless the user needs a fixed range; an out-of-range reading is reported
  as an error, not a number.
- Use `log_power_series` for laser stability / warm-up / drift studies; it reports RMS and
  peak-to-peak stability in percent.
- Readings are in watts (dBm also given). Do not exceed the sensor's rated power or power
  density - check the sensor spec; the meter's max range is not a damage threshold.
""",
    limits=[
        Limit("max_series_duration_s", 600, "s", "Longest allowed power-logging series"),
    ],
    address_help="""\
  USB0::0x1313::0x8078::P0012345::INSTR   PM100D over USBTMC (VISA resource string)
  USB0::0x1313::0x8072::P2001234::INSTR   PM100USB
  visa://USB0::0x1313::0x8075::P5000123::INSTR?backend=@ivi   use NI-VISA instead of pyvisa-py""",
    option_help={
        "channel": "sensor channel suffix for multi-channel consoles (PM5020: 1 or 2); omit for single-channel meters",
        "sim_sensor": "simulated sensor head: S120C (Si photodiode, default) or S302C (thermopile)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SensorModel(BaseModel):
    name: str
    serial: str
    calibration: str = Field(description="Calibration date string reported by the sensor")
    type: str = Field(description="photodiode, thermopile, pyroelectric, four-quadrant thermopile")
    measures_power: bool
    measures_energy: bool
    wavelength_settable: bool
    has_temperature_sensor: bool
    flags: int = Field(description="Raw SYST:SENS:IDN? capability bitmap")


class DeviceInfo(BaseModel):
    manufacturer: str
    model: str
    serial: str
    firmware: str
    sensor: SensorModel | None = Field(description="None if no sensor head is plugged in")
    wavelength_nm: float | None
    wavelength_min_nm: float | None
    wavelength_max_nm: float | None
    averaging_count: int
    auto_range: bool | None
    range_w: float | None = Field(description="Upper limit of the power range in use, W")
    max_range_w: float | None = Field(
        description="Least sensitive range (W). Not the sensor damage threshold - check the sensor spec."
    )
    zero_value: float | None = Field(
        description="Present dark-zero correction (A for photodiodes, V for thermal)"
    )
    timestamp: str


class PowerReading(BaseModel):
    power_w: float
    power_dbm: float | None = Field(description="Power in dBm re 1 mW; None if the reading is <= 0")
    formatted: str = Field(description="Power with an SI prefix, e.g. '1.234 mW'")
    wavelength_nm: float | None = Field(description="Correction wavelength used for this reading")
    sensor: str
    timestamp: str


class PowerSeries(BaseModel):
    count: int
    interval_s: float
    duration_s: float = Field(description="Actual elapsed time from first to last reading")
    wavelength_nm: float | None
    mean_w: float
    stdev_w: float
    min_w: float
    max_w: float
    mean_formatted: str
    rms_stability_percent: float | None = Field(description="stdev / mean x 100 (None if mean <= 0)")
    peak_to_peak_stability_percent: float | None = Field(description="(max - min) / mean x 100")
    drift_w_per_min: float = Field(description="Least-squares slope of power vs time, W per minute")
    drift_percent_per_min: float | None = Field(description="Drift relative to the mean, % per minute")
    time_s: list[float] = Field(
        description="Reading times relative to the first reading (downsampled to max_points)"
    )
    power_w: list[float] = Field(description="Readings in W (downsampled to max_points)")
    points_returned: int
    saved_to: str | None = None
    started: str


def _sensor_model(drv: ThorlabsPowerMeter) -> SensorModel | None:
    s = drv.sensor(refresh=True)
    if not s.connected:
        return None
    return SensorModel(
        name=s.name,
        serial=s.serial,
        calibration=s.calibration,
        type=s.type_name,
        measures_power=s.measures_power,
        measures_energy=s.measures_energy,
        wavelength_settable=s.wavelength_settable,
        has_temperature_sensor=s.has_temperature_sensor,
        flags=s.flags,
    )


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Identify the meter console and the attached sensor head (model, serial, type, wavelength
    range, power ranges), and report the present wavelength, averaging, range and zero value."""
    drv = server.driver
    with drv.t.lock:
        idn = drv.identify()
        sensor = _sensor_model(drv)
        wl = wl_lo = wl_hi = None
        auto = rng = rng_max = zero = None
        if sensor is not None:
            if sensor.wavelength_settable:
                wl = drv.wavelength_nm()
                wl_lo, wl_hi = drv.wavelength_range_nm()
            if sensor.measures_power:
                auto = drv.auto_range()
                rng = drv.range_w()
                rng_max = drv.range_limits_w()[1]
                zero = drv.zero_value()
        averaging = drv.averaging()
    return DeviceInfo(
        manufacturer=idn.get("manufacturer", ""),
        model=idn.get("model", ""),
        serial=idn.get("serial", ""),
        firmware=idn.get("firmware", ""),
        sensor=sensor,
        wavelength_nm=wl,
        wavelength_min_nm=wl_lo,
        wavelength_max_nm=wl_hi,
        averaging_count=averaging,
        auto_range=auto,
        range_w=rng,
        max_range_w=rng_max,
        zero_value=zero,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_power() -> PowerReading:
    """Read the optical power once (in W, plus dBm and a formatted string). The reading uses the
    configured correction wavelength and averaging; set the wavelength first."""
    drv = server.driver
    with drv.t.lock:
        power = drv.power_w()
        wl = drv.wavelength_nm() if drv.sensor().wavelength_settable else None
        name = drv.sensor().name
    return PowerReading(
        power_w=power,
        power_dbm=watts_to_dbm(power),
        formatted=format_power(power),
        wavelength_nm=wl,
        sensor=name,
        timestamp=_now(),
    )


def _downsample_indices(n: int, max_points: int) -> list[int]:
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / (max_points - 1)
    return sorted({round(i * step) for i in range(max_points)})


@mcp.tool(**READ, timeout=900)
def log_power_series(
    count: Annotated[int, Field(ge=2, le=100000, description="Number of readings")] = 60,
    interval_s: Annotated[
        float, Field(ge=0, le=3600, description="Seconds between readings (0 = as fast as the meter allows)")
    ] = 1.0,
    max_points: Annotated[
        int, Field(ge=10, le=5000, description="Maximum readings returned in the reply")
    ] = 200,
    save_path: Annotated[str | None, Field(description="Optional CSV path for every reading")] = None,
) -> PowerSeries:
    """Log a series of power readings to characterise laser stability, warm-up or drift. Returns
    mean, stdev, min/max, RMS and peak-to-peak stability (%) and drift (%/min), plus the readings
    (downsampled to `max_points`; use `save_path` to keep all of them)."""
    server.check("max_series_duration_s", (count - 1) * interval_s, "series duration")
    drv = server.driver
    started = _now()
    wl = drv.wavelength_nm() if drv.sensor(refresh=True).wavelength_settable else None
    times: list[float] = []
    values: list[float] = []
    t0 = time.monotonic()
    for i in range(count):
        delay = t0 + i * interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        times.append(time.monotonic() - t0)
        values.append(drv.power_w())
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    tbar = statistics.fmean(times)
    denom = sum((t - tbar) ** 2 for t in times)
    slope = sum((t - tbar) * (v - mean) for t, v in zip(times, values, strict=True)) / denom if denom else 0.0
    rel = 100.0 / mean if mean > 0 else None
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s", "power_w"])
            writer.writerows((f"{t:.4f}", f"{v:.6e}") for t, v in zip(times, values, strict=True))
        saved = str(path.resolve())
    idx = _downsample_indices(count, max_points)
    return PowerSeries(
        count=count,
        interval_s=interval_s,
        duration_s=times[-1],
        wavelength_nm=wl,
        mean_w=mean,
        stdev_w=stdev,
        min_w=min(values),
        max_w=max(values),
        mean_formatted=format_power(mean),
        rms_stability_percent=None if rel is None else stdev * rel,
        peak_to_peak_stability_percent=None if rel is None else (max(values) - min(values)) * rel,
        drift_w_per_min=slope * 60.0,
        drift_percent_per_min=None if rel is None else slope * 60.0 * rel,
        time_s=[round(times[i], 4) for i in idx],
        power_w=[values[i] for i in idx],
        points_returned=len(idx),
        saved_to=saved,
        started=started,
    )


@mcp.tool(**CONTROL)
def set_wavelength(
    wavelength_nm: Annotated[float, Field(gt=0, le=100000, description="Source wavelength in nm")],
) -> dict[str, float]:
    """Set the correction wavelength (nm) the meter uses to convert sensor signal to power. Must be
    within the sensor's calibrated range (see `get_device_info`); sent as a whole number of nm."""
    drv = server.driver
    with drv.t.lock:
        sensor = drv.sensor(refresh=True)
        if not sensor.connected:
            raise InstrumentProtocolError("No sensor is connected to the power meter.")
        if not sensor.wavelength_settable:
            raise InstrumentProtocolError(
                f"Sensor {sensor.name} does not support a wavelength setting (adapter sensors need a "
                "responsivity instead)."
            )
        lo, hi = drv.wavelength_range_nm()
        if not lo <= round(wavelength_nm) <= hi:
            raise InstrumentProtocolError(
                f"{wavelength_nm:g} nm is outside the calibrated range of {sensor.name} "
                f"({lo:g}-{hi:g} nm). Nothing was sent."
            )
        actual = drv.set_wavelength_nm(wavelength_nm)
    return {"wavelength_nm": actual}


@mcp.tool(**CONTROL)
def set_averaging(
    count: Annotated[
        int,
        Field(ge=1, le=10000, description="Samples averaged per reading (PM100: ~3 ms per sample)"),
    ],
) -> dict[str, int]:
    """Set how many samples the meter averages for each reading. More averaging lowers noise but
    slows each reading (PM100 series: ~3 ms per sample; PM400/PM101/PM5020: 1 ms)."""
    return {"averaging_count": server.driver.set_averaging(count)}


@mcp.tool(**CONTROL)
def set_range(
    mode: Annotated[Literal["auto", "manual"], Field(description="auto-ranging or a fixed range")] = "auto",
    range_w: Annotated[
        float | None, Field(gt=0, description="For manual mode: the highest power you expect, in W")
    ] = None,
) -> dict[str, float | bool]:
    """Select auto-ranging, or a fixed power range that fits `range_w` (the meter picks the most
    sensitive range that can hold that power). A fixed range reports over-range if exceeded."""
    drv = server.driver
    with drv.t.lock:
        if mode == "auto":
            drv.set_auto_range(True)
        else:
            if range_w is None:
                raise InstrumentProtocolError(
                    "Manual range needs `range_w` (the highest power you expect, in W)."
                )
            lo, hi = drv.range_limits_w()
            if range_w > hi:
                raise InstrumentProtocolError(
                    f"{range_w:g} W exceeds the largest range of this sensor ({hi:g} W). Nothing was sent."
                )
            drv.set_range_w(range_w)
        return {"auto_range": drv.auto_range(), "range_w": drv.range_w()}


@mcp.tool(**CONTROL, timeout=90)
def zero_sensor() -> dict[str, float | str]:
    """Dark-zero the sensor (removes dark current / thermal offset). BEFORE calling, ask the user
    to block the beam and cover the sensor aperture completely, and wait for them to confirm:
    zeroing with light on the sensor corrupts all later readings. Takes a few seconds."""
    zero = server.driver.zero()
    return {"zero_value": zero, "status": "Zero adjustment complete.", "timestamp": _now()}


@mcp.tool(**READ)
def read_sensor_temperature() -> dict[str, float | str]:
    """Read the sensor head temperature in °C (thermal sensors and other heads with a built-in
    temperature sensor only)."""
    return {"temperature_c": server.driver.temperature_c(), "timestamp": _now()}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
