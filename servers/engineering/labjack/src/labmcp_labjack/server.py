"""MCP server for LabJack T-series DAQ devices (T4, T7/T7-Pro, T8) via the LJM library."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from labmcp import HAZARD, READ, SAFETY, ConnectContext, InstrumentConnectionError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_labjack.driver import LabJackT, dio_index, dio_name, load_ljm, open_device
from labmcp_labjack.simulator import SimulatedLJM

_DEVICE_TYPES = {"ANY", "T4", "T7", "T8"}
_CONNECTION_TYPES = {"ANY", "USB", "ETHERNET", "WIFI", "TCP"}


def connect(ctx: ConnectContext) -> LabJackT:
    device_type = (ctx.option("device_type", "ANY") or "ANY").upper()
    connection_type = (ctx.option("connection_type", "ANY") or "ANY").upper()
    if device_type not in _DEVICE_TYPES:
        raise InstrumentConnectionError(f"device_type must be one of {sorted(_DEVICE_TYPES)}, got {device_type!r}")
    if connection_type not in _CONNECTION_TYPES:
        raise InstrumentConnectionError(
            f"connection_type must be one of {sorted(_CONNECTION_TYPES)}, got {connection_type!r}"
        )
    identifier = ctx.address or "ANY"
    if ctx.simulate:
        model = ctx.option("sim_model") or ("T7-Pro" if device_type == "ANY" else device_type)
        ljm = SimulatedLJM(model=model)
    else:
        ljm = load_ljm()
    driver = open_device(ljm, device_type, connection_type, identifier, audit=ctx.audit)
    safe = ctx.option("safe_dio", "") or ""
    driver.safe_lines = {dio_index(item) for item in safe.replace(";", ",").split(",") if item.strip()}
    return driver


server = InstrumentServer(
    "LabJack T-series DAQ (LJM)",
    connect=connect,
    package="labmcp-labjack",
    instructions="""
Controls a LabJack T4, T7/T7-Pro or T8 USB/Ethernet DAQ device through LabJack's LJM library.
- Call `get_device_info` first: channel counts, input ranges, DAC range and resolution indices
  differ between the T4, T7 and T8.
- Analog inputs are in volts. T7 ranges ±10/±1/±0.1/±0.01 V; T8 ±11 V down to ±0.018 V; T4 fixed
  (AIN0-3 ±10 V, AIN4-11 0-2.5 V). Pick the smallest range that fits the signal.
- `stream_analog` is hardware-timed and bounded (duration/scan-rate limits); it returns stats and a
  downsampled waveform, with the full data in a CSV if you give `save_path`.
- `write_dac` and `set_digital_output` energise outputs that may drive heaters, valves, relays or
  other equipment. Confirm with the user what is wired to the output before using them.
- When finished (or if anything looks wrong) call `set_outputs_safe`: DACs to 0 V and every line
  this server drove released to input (or driven low).
- `read_digital_inputs` never changes line directions; reading a single line elsewhere would.
""",
    limits=[
        Limit("max_dac_voltage_v", 5.0, "V", "Highest DAC output voltage an agent may set"),
        Limit("max_stream_duration_s", 60.0, "s", "Longest hardware-timed stream"),
        Limit("max_scan_rate_hz", 10_000.0, "Hz", "Highest stream scan rate (scans/s, all channels per scan)"),
    ],
    address_help="""\
  ANY                      first LabJack found (default when --address is omitted)
  470012345                serial number
  192.168.1.207            IP address (Ethernet / T7-Pro WiFi)
  MyT7                     device name (DEVICE_NAME_DEFAULT)""",
    option_help={
        "device_type": "ANY (default), T4, T7 or T8 - only open this model",
        "connection_type": "ANY (default), USB, ETHERNET or WIFI",
        "safe_dio": "comma-separated lines set_outputs_safe always releases/drives low, e.g. FIO0,FIO1",
        "sim_model": "model for --simulate: T4, T7, T7-Pro (default) or T8",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ models


class DeviceInfo(BaseModel):
    model: str
    serial: str
    firmware: str
    hardware_version: str
    connection: str
    ip_address: str | None
    device_name: str | None
    analog_inputs: list[str]
    input_ranges_v: list[float] = Field(description="Valid ± input ranges in V (empty for the T4: fixed ranges)")
    max_resolution_index: int
    dac_range_v: list[float] = Field(description="[min, max] DAC output in V")
    digital_lines: list[str]
    max_stream_samples_per_s: float
    thermocouple_supported: bool
    dac_voltages_v: list[float] = Field(description="Current DAC0, DAC1 settings in V")
    timestamp: str


class ChannelVoltage(BaseModel):
    channel: str
    negative_input: str = Field(description="What the input is measured against: GND, AINn (T7 differential) or the T8's isolated AINn- terminal")
    voltage_v: float
    range_v: float = Field(description="± full-scale range the channel was read with, in V")
    near_full_scale: bool = Field(description="True if the reading is within 2 % of full scale (may be clipped)")


class AnalogReading(BaseModel):
    readings: list[ChannelVoltage]
    simultaneous: bool = Field(description="True if all channels were sampled at the same instant (T8)")
    timestamp: str


class DigitalLine(BaseModel):
    line: str = Field(description="Bank name, e.g. FIO0 / EIO3 / CIO1 / MIO2")
    dio: int = Field(description="DIO number")
    direction: Literal["input", "output", "analog"]
    level: Literal["high", "low"] | None = Field(description="Logic level on the terminal; None for analog lines")
    driven_by_server: bool


class DigitalReading(BaseModel):
    lines: list[DigitalLine]
    timestamp: str


class DeviceTemperature(BaseModel):
    device_temperature_c: float = Field(description="Internal sensor (TEMPERATURE_DEVICE_K), ±2 °C")
    ambient_estimate_c: float = Field(description="Estimated air temperature outside the enclosure")
    terminal_temperatures_c: list[float | None] | None = Field(
        description="T8 only: sensor next to each AIN0-AIN7 terminal"
    )
    timestamp: str


class ThermocoupleReading(BaseModel):
    channel: str
    thermocouple_type: str
    temperature_c: float | None = Field(description="Hot-junction temperature; None if the reading is invalid")
    thermocouple_voltage_v: float
    cjc_temperature_c: float
    cjc_source: str
    valid: bool
    message: str | None = None
    timestamp: str


class ChannelStats(BaseModel):
    channel: str
    mean_v: float | None
    std_v: float | None
    min_v: float | None
    max_v: float | None
    rms_v: float | None
    peak_to_peak_v: float | None


class StreamResult(BaseModel):
    channels: list[str]
    requested_scan_rate_hz: float
    scan_rate_hz: float = Field(description="Actual scan rate the device ran at")
    scans: int
    duration_s: float
    skipped_scans: int = Field(description="Scans lost to buffer overflow (null in the waveform, empty in the CSV)")
    stats: list[ChannelStats]
    time_s: list[float] = Field(description="Time of each returned (downsampled) point from the first scan")
    waveforms_v: dict[str, list[float | None]] = Field(description="Downsampled waveform per channel")
    downsample_factor: int
    saved_to: str | None = Field(description="Absolute path of the full-resolution CSV, if requested")
    timestamp: str


class DacSetting(BaseModel):
    dac: str
    requested_v: float
    readback_v: float
    timestamp: str


class DigitalOutput(BaseModel):
    line: str
    level: Literal["high", "low"]
    timestamp: str


class SafeStateReport(BaseModel):
    actions: list[str]
    timestamp: str


# ------------------------------------------------------------------ helpers


def _stats(name: str, values: list[float]) -> ChannelStats:
    good = [v for v in values if not math.isnan(v)]
    if not good:
        return ChannelStats(channel=name, mean_v=None, std_v=None, min_v=None, max_v=None, rms_v=None, peak_to_peak_v=None)
    n = len(good)
    mean = sum(good) / n
    var = sum((v - mean) ** 2 for v in good) / (n - 1) if n > 1 else 0.0
    lo, hi = min(good), max(good)
    return ChannelStats(
        channel=name,
        mean_v=mean,
        std_v=math.sqrt(var),
        min_v=lo,
        max_v=hi,
        rms_v=math.sqrt(sum(v * v for v in good) / n),
        peak_to_peak_v=hi - lo,
    )


def _write_csv(path: str, header: list[str], columns: list[list[float]]) -> str:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows([("" if math.isnan(v) else v) for v in row] for row in zip(*columns, strict=True))
    return str(target)


# ------------------------------------------------------------------ tools


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Identify the connected LabJack (model, serial, firmware, connection) and list what it
    offers: analog inputs, valid input ranges, resolution indices, DAC range, digital lines and
    stream rate. Call this before configuring channels."""
    d = server.driver
    ident = d.identify()
    spec = d.spec
    return DeviceInfo(
        model=spec.model,
        serial=ident["serial"],
        firmware=ident["firmware"],
        hardware_version=ident["hardware_version"],
        connection=ident["connection"],
        ip_address=ident.get("ip_address"),
        device_name=ident.get("device_name"),
        analog_inputs=[f"AIN{c}" for c in spec.ain_channels],
        input_ranges_v=list(spec.ain_ranges_v),
        max_resolution_index=spec.max_resolution_index,
        dac_range_v=[0.0, spec.dac_max_v],
        digital_lines=[dio_name(n) for n in spec.dio_lines],
        max_stream_samples_per_s=spec.max_stream_samples_per_s,
        thermocouple_supported=spec.thermocouple,
        dac_voltages_v=d.read_dacs(),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_analog_inputs(
    channels: Annotated[list[int], Field(min_length=1, max_length=14, description="AIN numbers, e.g. [0, 1, 2]")],
    range_v: Annotated[
        float | None,
        Field(gt=0, le=11, description="± input range in V for all listed channels (T7: 10/1/0.1/0.01; T8: 11 ... 0.018). Omit to keep the current range. Not settable on the T4."),
    ] = None,
    resolution_index: Annotated[
        int | None,
        Field(ge=0, le=16, description="Higher = less noise, slower (T4 0-5, T7 0-8, T7-Pro 0-12, T8 0-16; 0 = default). Omit to keep."),
    ] = None,
    differential: Annotated[
        bool | None,
        Field(description="T7 only: true = measure AINn - AIN(n+1) for even n (e.g. AIN0-AIN1), false = single-ended, omit = keep the current setting"),
    ] = None,
) -> AnalogReading:
    """Read one or more analog inputs once (command-response) and return volts.

    Range and resolution settings persist on the device until changed. On the T8, several
    channels are sampled simultaneously. Each reading reports what it was measured against
    (GND or the negative input). Fails while a stream is running."""
    d = server.driver
    volts = d.read_ain(channels, range_v, resolution_index, differential)
    ranges = d.ain_ranges(channels)
    negatives = d.negative_inputs(channels)
    readings = [
        ChannelVoltage(
            channel=f"AIN{c}",
            negative_input=neg,
            voltage_v=v,
            range_v=r,
            near_full_scale=abs(v) >= 0.98 * r,
        )
        for c, v, r, neg in zip(channels, volts, ranges, negatives, strict=True)
    ]
    return AnalogReading(
        readings=readings,
        simultaneous=d.spec.model == "T8" and len(channels) > 1,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_digital_inputs(
    lines: Annotated[
        list[str] | None,
        Field(description="Lines to report, e.g. ['FIO0', 'EIO3'] or ['DIO4']. Omit for all lines."),
    ] = None,
) -> DigitalReading:
    """Report the logic level and direction (input/output/analog) of digital I/O lines.

    Uses the DIO_STATE/DIO_DIRECTION bitmask registers, which never change a line's direction, so
    it is safe to call while lines are driving equipment. Outputs report the level actually on the
    terminal."""
    d = server.driver
    numbers = None if lines is None else [dio_index(x) for x in lines]
    states = d.read_dio(numbers)
    return DigitalReading(
        lines=[
            DigitalLine(
                line=s["name"],
                dio=s["line"],
                direction=s["direction"],
                level=None if s["high"] is None else ("high" if s["high"] else "low"),
                driven_by_server=s["line"] in d.driven_lines,
            )
            for s in states
        ],
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_device_temperature() -> DeviceTemperature:
    """Read the LabJack's internal temperature sensor (°C, about ±2 °C) and the estimated ambient
    air temperature. On the T8 also returns the sensor next to each AIN terminal. Cannot be read
    while a stream is running."""
    device_k, air_k, terminals = server.driver.device_temperature_k()
    return DeviceTemperature(
        device_temperature_c=device_k - 273.15,
        ambient_estimate_c=air_k - 273.15,
        terminal_temperatures_c=None
        if terminals is None
        else [None if k == -9999.0 else k - 273.15 for k in terminals],
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_thermocouple(
    channel: Annotated[int, Field(ge=0, le=13, description="AIN the thermocouple + lead is on")],
    thermocouple_type: Literal["B", "C", "E", "J", "K", "N", "R", "S", "T"] = "K",
    cjc: Annotated[
        Literal["internal", "lm34"],
        Field(description="Cold-junction sensor: 'internal' (device sensor; best for T7 screw terminals / T8) or 'lm34' (LM34 on cjc_channel, e.g. on a CB37)"),
    ] = "internal",
    cjc_channel: Annotated[int | None, Field(ge=0, le=13, description="AIN of the LM34 when cjc='lm34'")] = None,
    differential: Annotated[
        bool, Field(description="T7 only: thermocouple between AINn (+) and AIN(n+1) (-); recommended on the CB37")
    ] = False,
    resolution_index: Annotated[int | None, Field(ge=0, le=16, description="Omit for the device default")] = None,
) -> ThermocoupleReading:
    """Read a thermocouple with the T7/T8 AIN thermocouple extended feature (types B, C, E, J, K,
    N, R, S, T) and return °C with the measured voltage and cold-junction temperature.

    This configures the AIN extended feature on the channel (the T7 switches a ±10 V range to
    ±0.1 V). A floating/open thermocouple returns valid=false. Accuracy is dominated by the CJC:
    about ±2 °C with the internal sensor. Not available on the T4."""
    d = server.driver
    r = d.read_thermocouple(channel, thermocouple_type, cjc, cjc_channel, differential, resolution_index)
    valid = r.temperature_c is not None
    source = "internal sensor" if cjc == "internal" else f"LM34 on AIN{cjc_channel}"
    return ThermocoupleReading(
        channel=f"AIN{channel}" + (f"-AIN{channel + 1}" if differential else ""),
        thermocouple_type=thermocouple_type,
        temperature_c=r.temperature_c,
        thermocouple_voltage_v=r.thermocouple_voltage_v,
        cjc_temperature_c=r.cjc_temperature_c,
        cjc_source=source,
        valid=valid,
        message=None
        if valid
        else "The thermocouple voltage is outside the valid range for this type: the input is probably "
        "open/floating or the wrong type is selected.",
        timestamp=_now(),
    )


@mcp.tool(**READ, timeout=700)
def stream_analog(
    channels: Annotated[list[int], Field(min_length=1, max_length=14, description="AIN numbers, e.g. [0, 1]")],
    scan_rate_hz: Annotated[float, Field(gt=0, le=100_000, description="Scans per second (one sample of every channel per scan)")] = 1000.0,
    duration_s: Annotated[float, Field(gt=0, le=600, description="Acquisition time in seconds")] = 1.0,
    range_v: Annotated[float | None, Field(gt=0, le=11, description="± range in V for all channels (T7/T8); omit to keep")] = None,
    resolution_index: Annotated[int | None, Field(ge=0, le=16, description="Stream resolution index; omit for default")] = None,
    max_points: Annotated[int, Field(ge=10, le=20_000, description="Max points per channel in the returned waveform")] = 500,
    save_path: Annotated[str | None, Field(description="Write the full-resolution data to this CSV file")] = None,
) -> StreamResult:
    """Acquire a hardware-timed waveform on one or more analog inputs (LJM stream mode) and return
    per-channel statistics plus a downsampled waveform; optionally save everything to CSV.

    Bounded by the `max_stream_duration_s` and `max_scan_rate_hz` limits and the device maximum
    (T4 50 kS/s, T7 100 kS/s aggregate, T8 40 kscans/s). On the T8, list adjacent channels in
    order; on the T7 the streamed inputs are set to single-ended. Blocks for `duration_s`."""
    server.check("max_scan_rate_hz", scan_rate_hz, "stream scan rate")
    server.check("max_stream_duration_s", duration_s, "stream duration")
    num_scans = max(2, round(scan_rate_hz * duration_s))
    s = server.driver.stream_ain(channels, scan_rate_hz, num_scans, range_v, resolution_index)
    names = [f"AIN{c}" for c in s.channels]
    times = [i / s.scan_rate_hz for i in range(num_scans)]
    step = max(1, math.ceil(num_scans / max_points))
    saved = None
    if save_path:
        saved = _write_csv(save_path, ["time_s"] + [f"{n}_v" for n in names], [times, *s.data])
    return StreamResult(
        channels=names,
        requested_scan_rate_hz=scan_rate_hz,
        scan_rate_hz=s.scan_rate_hz,
        scans=num_scans,
        duration_s=num_scans / s.scan_rate_hz,
        skipped_scans=s.skipped_scans,
        stats=[_stats(n, col) for n, col in zip(names, s.data, strict=True)],
        time_s=times[::step],
        waveforms_v={n: [None if math.isnan(v) else v for v in col[::step]] for n, col in zip(names, s.data, strict=True)},
        downsample_factor=step,
        saved_to=saved,
        timestamp=_now(),
    )


@mcp.tool(**HAZARD)
def write_dac(
    dac: Annotated[int, Field(ge=0, le=1, description="0 for DAC0, 1 for DAC1")],
    voltage_v: Annotated[float, Field(ge=0, le=10, description="Output voltage (T4/T7: 0-5 V, T8: 0-10 V)")],
) -> DacSetting:
    """Set an analog output (DAC0/DAC1) to a DC voltage. The output stays at this value until
    changed or `set_outputs_safe` is called.

    This energises whatever is wired to the DAC (a heater driver, valve, laser or motor
    controller input). Checked against `max_dac_voltage_v` and the model's hardware range before
    anything is sent. The DAC can source about 20 mA through 50 Ohm; loads pull the voltage down."""
    server.check("max_dac_voltage_v", voltage_v, f"DAC{dac} voltage")
    readback = server.driver.write_dac(dac, voltage_v)
    return DacSetting(dac=f"DAC{dac}", requested_v=voltage_v, readback_v=readback, timestamp=_now())


@mcp.tool(**HAZARD)
def set_digital_output(
    line: Annotated[str, Field(description="Digital line, e.g. 'FIO0', 'EIO2', 'CIO1' or 'DIO5'")],
    level: Literal["high", "low"],
) -> DigitalOutput:
    """Make a digital line an output and drive it high (3.3 V) or low (0 V).

    Digital outputs often switch relays, solid-state relays, valves or instrument trigger inputs,
    so confirm with the user what the line controls. The line keeps driving until changed or
    released with `set_outputs_safe`."""
    d = server.driver
    number = dio_index(line)
    d.set_dio(number, level == "high")
    return DigitalOutput(line=dio_name(number), level=level, timestamp=_now())


@mcp.tool(**SAFETY)
def set_outputs_safe(
    dio_mode: Annotated[
        Literal["input", "low"],
        Field(description="'input' (default) returns lines to the power-up state (input with pull-up); 'low' drives them to 0 V instead"),
    ] = "input",
) -> SafeStateReport:
    """Put the outputs in a safe state: stop any stream, set DAC0 and DAC1 to 0 V, and release
    every digital line this server drove (plus the `safe_dio` option lines) to input - or drive
    them low with dio_mode='low'. Use when finished or if anything looks wrong."""
    actions = server.driver.safe_state(dio_mode)
    return SafeStateReport(actions=actions, timestamp=_now())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
