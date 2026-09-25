"""MCP server for National Instruments DAQ devices via NI-DAQmx."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from labmcp import HAZARD, READ, SAFETY, ConnectContext, InstrumentProtocolError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_ni_daqmx.driver import NIDAQ, THERMOCOUPLE_TYPES, Acquisition, load_nidaqmx
from labmcp_ni_daqmx.simulator import SimulatedNIDAQmx


def connect(ctx: ConnectContext) -> NIDAQ:
    backend = SimulatedNIDAQmx() if ctx.simulate else load_nidaqmx()
    return NIDAQ(backend, ctx.address, audit=ctx.audit, safe_do_lines=ctx.option("safe_do_lines", "") or "")


server = InstrumentServer(
    "NI-DAQmx DAQ Device",
    connect=connect,
    package="labmcp-ni-daqmx",
    instructions="""
Controls one National Instruments DAQ device (USB-600x, USB-621x, M/X Series, CompactDAQ modules...)
through the NI-DAQmx driver.
- Call `get_device_info` first: it lists the device's analog inputs/outputs, digital lines, voltage
  ranges and maximum sample rates. Channel names can be short ("ai0:3", "port0/line1") or full
  ("Dev1/ai0").
- `read_analog` takes one on-demand sample (samples=1) or a finite hardware-timed acquisition;
  large acquisitions are summarised and downsampled, with full data in a CSV via `save_path`.
- Choose the terminal configuration to match the wiring (RSE = referenced single-ended, DIFF =
  differential, NRSE). The wrong one gives offset or noisy readings, not an error.
- `write_analog` and `write_digital_lines` energise outputs that may drive valves, heaters, relays
  or other instruments; confirm what is connected before using them.
- When finished (or if anything looks wrong) call `set_outputs_safe` (all AO to 0 V, driven DO lines low).
""",
    limits=[
        Limit("max_ao_voltage_v", 5.0, "V", "Largest |voltage| an agent may put on an analog output"),
        Limit("max_samples", 100_000, "samples", "Most samples per channel in one acquisition"),
        Limit("max_rate_hz", 100_000, "Hz", "Highest sample clock rate per channel"),
        Limit("max_acquisition_s", 60.0, "s", "Longest finite acquisition (samples / rate)"),
    ],
    address_help="""\
  Dev1                     device name as shown in NI MAX / nilsdev (optional if exactly one device)
  cDAQ1Mod1                CompactDAQ module
  SimDev1                  an NI MAX simulated device (tests the real NI-DAQmx path without hardware)""",
    option_help={
        "safe_do_lines": "DO lines set_outputs_safe always drives low, e.g. port0/line0:3",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ models


class DeviceSummary(BaseModel):
    name: str
    product_type: str | None
    product_category: str | None
    serial_number: str | None = Field(description="Hexadecimal, as shown in NI MAX")
    is_simulated: bool = Field(description="True for an NI MAX simulated device")
    analog_inputs: list[str]
    analog_outputs: list[str]
    digital_lines: list[str]


class DeviceList(BaseModel):
    devices: list[DeviceSummary]
    connected_device: str
    nidaqmx_driver_version: str
    timestamp: str


class DeviceInfo(DeviceSummary):
    ai_voltage_ranges_v: list[list[float]] = Field(description="Supported [min, max] input ranges")
    ao_voltage_ranges_v: list[list[float]] = Field(description="Supported [min, max] output ranges")
    ai_max_single_channel_rate_hz: float | None
    ai_max_multi_channel_rate_hz: float | None = Field(description="Aggregate rate over all channels (multiplexed devices)")
    ai_simultaneous_sampling: bool | None
    di_lines: list[str]
    do_lines: list[str]
    ao_last_written_v: dict[str, float] = Field(description="Values this server wrote to AO channels")
    do_last_written: dict[str, bool] = Field(description="Levels this server wrote to DO lines (true = high)")
    timestamp: str


class ChannelSummary(BaseModel):
    channel: str
    unit: str
    mean: float
    std: float
    min: float
    max: float
    rms: float
    last: float


class Acquired(BaseModel):
    channels: list[str]
    unit: str
    samples_per_channel: int
    sample_rate_hz: float | None = Field(description="Actual sample clock rate (None for one on-demand sample)")
    stats: list[ChannelSummary]
    time_s: list[float] = Field(description="Time of each returned point from the first sample")
    waveforms: dict[str, list[float]] = Field(description="Downsampled data per channel (all data if small)")
    downsample_factor: int
    saved_to: str | None
    warnings: list[str] = Field(description="NI-DAQmx warnings raised during the acquisition")
    timestamp: str


class LineState(BaseModel):
    line: str
    level: Literal["high", "low"]
    source: str = Field(description="'measured', or the last value this server commanded for lines it drives")


class DigitalLines(BaseModel):
    lines: list[LineState]
    timestamp: str


class AnalogOutput(BaseModel):
    channel: str
    voltage_v: float
    timestamp: str


class DigitalOutput(BaseModel):
    lines: list[LineState]
    timestamp: str


class SafeStateReport(BaseModel):
    actions: list[str]
    timestamp: str


# ------------------------------------------------------------------ helpers


def _check_acquisition(samples: int, rate_hz: float | None) -> None:
    server.check("max_samples", samples, "samples per channel")
    if samples > 1:
        if rate_hz is None:
            raise InstrumentProtocolError("rate_hz is required when samples > 1.")
        server.check("max_rate_hz", rate_hz, "sample rate")
        server.check("max_acquisition_s", samples / rate_hz, "acquisition time")


def _summarise(acq: Acquisition, unit: str, max_points: int, save_path: str | None) -> Acquired:
    n = len(acq.data[0])
    rate = acq.rate_hz
    times = [i / rate for i in range(n)] if rate else [0.0]
    stats = []
    for name, col in zip(acq.channels, acq.data, strict=True):
        mean = sum(col) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in col) / (n - 1)) if n > 1 else 0.0
        stats.append(
            ChannelSummary(
                channel=name,
                unit=unit,
                mean=mean,
                std=std,
                min=min(col),
                max=max(col),
                rms=math.sqrt(sum(v * v for v in col) / n),
                last=col[-1],
            )
        )
    step = max(1, math.ceil(n / max_points))
    saved = None
    if save_path:
        target = Path(save_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s"] + [f"{c}_{unit}" for c in acq.channels])
            writer.writerows(zip(times, *acq.data, strict=True))
        saved = str(target)
    return Acquired(
        channels=acq.channels,
        unit=unit,
        samples_per_channel=n,
        sample_rate_hz=rate,
        stats=stats,
        time_s=times[::step],
        waveforms={c: col[::step] for c, col in zip(acq.channels, acq.data, strict=True)},
        downsample_factor=step,
        saved_to=saved,
        warnings=acq.warnings,
        timestamp=_now(),
    )


def _summary(d: dict[str, Any]) -> dict[str, Any]:
    keys = DeviceSummary.model_fields
    return {k: v for k, v in d.items() if k in keys}


# ------------------------------------------------------------------ tools


@mcp.tool(**READ)
def list_devices() -> DeviceList:
    """List every NI-DAQmx device the driver can see (product type, serial number, channels,
    whether it is an NI MAX simulated device) and which one this server is connected to."""
    d = server.driver
    return DeviceList(
        devices=[DeviceSummary(**_summary(x)) for x in d.list_devices()],
        connected_device=d.name,
        nidaqmx_driver_version=d.driver_version(),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Describe the connected device: analog input/output channels, digital lines, supported
    voltage ranges, maximum sample rates, and the outputs this server has set. Call this before
    choosing channels, ranges and rates."""
    d = server.driver
    info = d.describe()
    return DeviceInfo(**info, ao_last_written_v=dict(d.ao_last), do_last_written=dict(d.do_last), timestamp=_now())


@mcp.tool(**READ, timeout=700)
def read_analog(
    channels: Annotated[str, Field(description="Analog inputs, e.g. 'ai0', 'ai0:3' or 'Dev1/ai0, Dev1/ai4'")],
    terminal_config: Annotated[
        Literal["default", "rse", "nrse", "diff", "pseudo_diff"],
        Field(description="Input wiring: rse = single-ended to AI GND, diff = differential, nrse = to AI SENSE"),
    ] = "default",
    min_v: Annotated[float, Field(ge=-100, le=100, description="Lowest expected voltage (sets the input range)")] = -10.0,
    max_v: Annotated[float, Field(ge=-100, le=100, description="Highest expected voltage (sets the input range)")] = 10.0,
    samples: Annotated[int, Field(ge=1, le=10_000_000, description="Samples per channel; 1 = one on-demand reading")] = 1,
    rate_hz: Annotated[float | None, Field(gt=0, le=10_000_000, description="Sample clock rate per channel (required if samples > 1)")] = None,
    max_points: Annotated[int, Field(ge=10, le=20_000, description="Max points per channel in the returned waveform")] = 500,
    save_path: Annotated[str | None, Field(description="Write all samples to this CSV file")] = None,
) -> Acquired:
    """Measure voltage on one or more analog inputs: a single on-demand reading, or a finite
    hardware-timed acquisition of `samples` per channel at `rate_hz`. Returns per-channel
    statistics and a downsampled waveform; optionally saves all data to CSV.

    Bounded by the `max_samples`, `max_rate_hz` and `max_acquisition_s` limits; the device's own
    maximum rate (shared by all channels on multiplexed devices) also applies. Blocks for
    samples / rate_hz seconds."""
    _check_acquisition(samples, rate_hz)
    acq = server.driver.read_voltage(channels, terminal_config, min_v, max_v, samples, rate_hz)
    return _summarise(acq, "v", max_points, save_path)


@mcp.tool(**READ, timeout=700)
def read_thermocouple(
    channels: Annotated[str, Field(description="Thermocouple inputs, e.g. 'ai0' or 'ai0:3' (on a thermocouple-capable device/module)")],
    thermocouple_type: Literal["B", "E", "J", "K", "N", "R", "S", "T"] = "K",
    cjc_source: Annotated[
        Literal["built_in", "constant"],
        Field(description="'built_in' = the module's cold-junction sensor (e.g. NI 9211/9213, USB-TC01); 'constant' = cjc_value_c"),
    ] = "built_in",
    cjc_value_c: Annotated[float, Field(ge=-50, le=150, description="Cold-junction temperature in °C when cjc_source='constant'")] = 25.0,
    min_c: Annotated[float, Field(ge=-270, le=1820, description="Lowest expected temperature, °C")] = 0.0,
    max_c: Annotated[float, Field(ge=-270, le=1820, description="Highest expected temperature, °C")] = 100.0,
    samples: Annotated[int, Field(ge=1, le=1_000_000, description="Samples per channel; 1 = one reading")] = 1,
    rate_hz: Annotated[float | None, Field(gt=0, le=100_000, description="Sample rate if samples > 1")] = None,
    max_points: Annotated[int, Field(ge=10, le=20_000)] = 500,
    save_path: Annotated[str | None, Field(description="Write all samples to this CSV file")] = None,
) -> Acquired:
    """Measure temperature (°C) with thermocouples (types B, E, J, K, N, R, S, T) on a
    thermocouple-capable device, e.g. an NI 9211/9212/9213/9214 module or USB-TC01.

    Use cjc_source='built_in' on modules with a cold-junction sensor; 'constant' uses cjc_value_c
    (accuracy then depends on how well you know the terminal temperature). Devices without
    thermocouple support return an NI-DAQmx error."""
    if thermocouple_type not in THERMOCOUPLE_TYPES:
        raise InstrumentProtocolError(f"thermocouple_type must be one of {', '.join(THERMOCOUPLE_TYPES)}")
    _check_acquisition(samples, rate_hz)
    acq = server.driver.read_thermocouple(channels, thermocouple_type, cjc_source, cjc_value_c, min_c, max_c, samples, rate_hz)
    return _summarise(acq, "c", max_points, save_path)


@mcp.tool(**READ)
def read_digital_lines(
    lines: Annotated[str, Field(description="Digital lines, e.g. 'port0/line0:3' or 'Dev1/port1/line0'")],
) -> DigitalLines:
    """Read the logic level of digital lines.

    Lines that this server is driving as outputs are reported from their last commanded level and
    are NOT re-read: an NI-DAQmx digital-input task would switch them to inputs and release
    whatever they drive. Reading a line another program drives may likewise make it an input."""
    states = server.driver.read_lines(lines)
    return DigitalLines(
        lines=[LineState(line=s["line"], level="high" if s["high"] else "low", source=s["source"]) for s in states],
        timestamp=_now(),
    )


@mcp.tool(**HAZARD)
def write_analog(
    channel: Annotated[str, Field(description="One analog output, e.g. 'ao0' or 'Dev1/ao1'")],
    voltage_v: Annotated[float, Field(ge=-10.5, le=10.5, description="DC output voltage in V")],
) -> AnalogOutput:
    """Set an analog output to a DC voltage (static, software-timed). The output keeps this value
    after the call on most NI devices, until changed or `set_outputs_safe` is called.

    This energises whatever the output drives (valve or heater controllers, laser drivers,
    actuators). Checked against `max_ao_voltage_v` (absolute value) and the device's AO range
    (e.g. USB-6008/6009: 0-5 V) before anything is sent."""
    server.check("max_ao_voltage_v", abs(voltage_v), f"|{channel}| voltage")
    name = server.driver.write_voltage(channel, voltage_v)
    return AnalogOutput(channel=name, voltage_v=voltage_v, timestamp=_now())


@mcp.tool(**HAZARD)
def write_digital_lines(
    lines: Annotated[str, Field(description="Digital lines, e.g. 'port0/line0' or 'port0/line0:3'")],
    levels: Annotated[
        list[Literal["high", "low"]],
        Field(min_length=1, description="One level for all lines, or one per line in order"),
    ],
) -> DigitalOutput:
    """Drive digital output lines high or low. The lines keep their level after the call until
    changed or `set_outputs_safe` is called.

    Digital outputs often switch relays, valves, pumps or trigger other instruments; confirm with
    the user what each line controls. Voltage levels depend on the device (e.g. 5 V TTL or 3.3 V)."""
    names = server.driver.write_lines(lines, [lvl == "high" for lvl in levels])
    d = server.driver
    return DigitalOutput(
        lines=[LineState(line=n, level="high" if d.do_last[n] else "low", source="commanded") for n in names],
        timestamp=_now(),
    )


@mcp.tool(**SAFETY)
def set_outputs_safe(
    digital_low: Annotated[
        bool, Field(description="Also drive low every DO line this server drove (and the safe_do_lines option)")
    ] = True,
) -> SafeStateReport:
    """Put the device's outputs in a safe state: every analog output to 0 V (or the bottom of its
    range if 0 V is not in it) and, by default, every digital line this server drove - plus the
    `safe_do_lines` option lines - driven low. Use when finished or if anything looks wrong."""
    return SafeStateReport(actions=server.driver.safe_state(digital_low), timestamp=_now())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
