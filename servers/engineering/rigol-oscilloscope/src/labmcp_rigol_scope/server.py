"""MCP server for Rigol digital oscilloscopes (SCPI)."""

from __future__ import annotations

import csv
import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from labmcp import CONTROL, READ, ConnectContext, InstrumentServer, Limit
from mcp.types import TextContent
from pydantic import BaseModel, Field

from labmcp_rigol_scope.driver import MEASUREMENTS, RigolScope
from labmcp_rigol_scope.simulator import RigolScopeSimulator

MeasurementName = Literal[
    "vmax", "vmin", "vpp", "vtop", "vbase", "vamp", "vavg", "vrms", "overshoot", "preshoot", "period",
    "frequency", "rise_time", "fall_time", "positive_width", "negative_width", "positive_duty", "negative_duty",
]


def connect(ctx: ConnectContext) -> RigolScope:
    model = ctx.option("sim_model", "DS1104Z") or "DS1104Z"
    transport = ctx.open_transport(
        simulator=lambda: RigolScopeSimulator(model=model),
        read_termination="\n",
        write_termination="\n",
        timeout=5.0,
    )
    try:
        return RigolScope(transport, profile=ctx.option("profile"))
    except Exception:
        transport.close()
        raise


server = InstrumentServer(
    "Rigol Oscilloscope (SCPI)",
    connect=connect,
    package="labmcp-rigol-scope",
    instructions="""
Controls a Rigol digital oscilloscope (DS1000Z/MSO1000Z, DS1000Z-E, MSO5000, DHO800/900/1000/4000) over SCPI.
- Call `get_device_info`, then `get_settings`, before changing anything: the scope's current setup
  is usually the user's own front-panel work. Only `autoscale` when asked - it changes every setting.
- Displayed values include the probe ratio: a 10x probe with the channel set to 1x reads 10x low.
  Check `probe_ratio` when amplitudes look wrong.
- `measure` uses the scope's own measurements; a value of null means the scope could not measure it
  (e.g. less than one period on screen, or the trace is clipped).
- `capture_waveform` mode='screen' reads the displayed points (fast); mode='memory' reads the full
  acquisition memory and needs the scope stopped (`stop` or `single` first).
- Screen data that sits at the top/bottom of the display is clipped (`clipped_fraction` > 0): adjust
  the vertical scale/offset before trusting amplitudes.
- This server never drives the scope's built-in signal generator (if any).
""",
    limits=[
        Limit("max_memory_points", 1_200_000, "points", "Most points read from acquisition memory in one capture"),
    ],
    address_help="""\
  USB0::0x1AB1::0x04CE::DS1ZA123456789::INSTR   USB-TMC (DS1000Z; find it with `python -m pyvisa info`)
  TCPIP0::192.168.1.50::INSTR                    LAN, VXI-11 (LXI)
  tcp://192.168.1.50:5555                        LAN, raw SCPI socket
  visa://USB0::...::INSTR?backend=@ivi           use NI-VISA / Keysight VISA instead of pyvisa-py""",
    option_help={
        "profile": "force the command profile: DS1000Z, MSO5000 or DHO (normally detected from *IDN?)",
        "sim_model": "model for --simulate, e.g. DS1104Z (default), DS1202Z-E, MSO5074, DHO804, DHO1204",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ models


class DeviceInfo(BaseModel):
    manufacturer: str
    model: str
    serial: str
    firmware: str
    family: str = Field(description="Command profile: DS1000Z, MSO5000 or DHO")
    programming_guide: str
    analog_channels: int
    screen_points: int = Field(description="Points per channel in a screen capture")
    horizontal_divisions: int
    sample_rate_sa_s: float
    memory_depth: int | str
    timestamp: str


class ChannelSettings(BaseModel):
    channel: int
    enabled: bool
    scale_v_per_div: float
    offset_v: float
    coupling: str
    probe_ratio: float
    bandwidth_limit: str


class TimebaseSettings(BaseModel):
    scale_s_per_div: float
    offset_s: float
    window_s: float = Field(description="Time shown across the screen (scale x divisions)")


class TriggerSettings(BaseModel):
    type: str
    sweep: str = Field(description="AUTO, NORM or SING")
    status: str = Field(description="TD (triggered), WAIT, RUN, AUTO or STOP")
    source: str | None = None
    level_v: float | None = None
    slope: str | None = None


class ScopeSettings(BaseModel):
    channels: list[ChannelSettings]
    timebase: TimebaseSettings
    trigger: TriggerSettings
    sample_rate_sa_s: float
    memory_depth: int | str
    timestamp: str


class RunState(BaseModel):
    trigger_status: str
    message: str
    timestamp: str


class Measurement(BaseModel):
    name: str
    scpi_item: str
    value: float | None = Field(description="null if the scope could not measure it (it returned 9.9E37)")
    unit: str = Field(description="V, s, Hz, or 'ratio' (as returned by the scope, e.g. 0.5 = 50 %)")


class MeasurementResult(BaseModel):
    channel: int
    measurements: list[Measurement]
    timestamp: str


class WaveformStats(BaseModel):
    min_v: float
    max_v: float
    peak_to_peak_v: float
    mean_v: float
    rms_v: float
    std_v: float


class WaveformResult(BaseModel):
    channel: int
    mode: str
    points: int
    sample_interval_s: float
    start_time_s: float = Field(description="Time of the first point relative to the trigger")
    stats: WaveformStats
    clipped_fraction: float = Field(description="Fraction of screen points pinned at the top/bottom of the display")
    time_s: list[float]
    volts: list[float]
    downsample_factor: int
    saved_to: str | None
    timestamp: str


# ------------------------------------------------------------------ tools


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Identify the oscilloscope (model, serial, firmware), the command family used for it, the
    number of analog channels, and the current sample rate and memory depth."""
    d = server.driver
    acq = d.acquisition()
    return DeviceInfo(
        manufacturer=d.ident["manufacturer"],
        model=d.model,
        serial=d.ident["serial"],
        firmware=d.ident["firmware"],
        family=d.profile.family,
        programming_guide=d.profile.guide,
        analog_channels=d.channels,
        screen_points=d.profile.screen_points,
        horizontal_divisions=d.profile.screen_points // 100,
        sample_rate_sa_s=acq["sample_rate_sa_s"],
        memory_depth=acq["memory_depth"],
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_settings() -> ScopeSettings:
    """Read the current vertical settings of every channel (on/off, V/div, offset, coupling, probe
    ratio, bandwidth limit), the timebase, the trigger (type, sweep, status, edge source/level/slope)
    and the acquisition sample rate / memory depth."""
    d = server.driver
    tb = d.timebase()
    acq = d.acquisition()
    return ScopeSettings(
        channels=[ChannelSettings(**d.channel_settings(n)) for n in range(1, d.channels + 1)],
        timebase=TimebaseSettings(**tb, window_s=tb["scale_s_per_div"] * d.profile.screen_points / 100),
        trigger=TriggerSettings(**d.trigger()),
        sample_rate_sa_s=acq["sample_rate_sa_s"],
        memory_depth=acq["memory_depth"],
        timestamp=_now(),
    )


def _state(message: str) -> RunState:
    return RunState(trigger_status=server.driver.trigger_status(), message=message, timestamp=_now())


@mcp.tool(**CONTROL, timeout=30)
def autoscale() -> RunState:
    """Run the scope's automatic setup (AUTO key): it picks vertical scales, timebase and trigger
    for the connected signals. This overwrites the user's current setup - only use it when asked.
    Needs signals of roughly >20 mVpp and >40 Hz."""
    server.driver.autoscale()
    return _state("Autoscale done. Call get_settings to see the new setup.")


@mcp.tool(**CONTROL)
def run() -> RunState:
    """Start continuous acquisition (RUN)."""
    server.driver.run()
    return _state("Acquisition running.")


@mcp.tool(**CONTROL)
def stop() -> RunState:
    """Stop acquisition (STOP) and freeze the current waveforms, e.g. before reading deep memory."""
    server.driver.stop()
    return _state("Acquisition stopped.")


@mcp.tool(**CONTROL, timeout=90)
def single(
    wait_s: Annotated[float, Field(ge=0, le=60, description="Seconds to wait for the trigger; 0 = just arm")] = 5.0,
) -> RunState:
    """Arm a single acquisition (SINGLE key): the scope triggers once, then stops. With wait_s > 0
    it waits until the acquisition is complete (status STOP). If the trigger condition is never met
    the status stays WAIT - check the trigger level/source or use `force_trigger`."""
    status = server.driver.single(wait_s)
    done = status == "STOP"
    msg = "Single acquisition complete." if done else f"Armed; not triggered yet (status {status})."
    return RunState(trigger_status=status, message=msg, timestamp=_now())


@mcp.tool(**CONTROL)
def force_trigger() -> RunState:
    """Force one trigger (FORCE key). Only has an effect in NORMal or SINGle sweep while waiting."""
    server.driver.force_trigger()
    return _state("Trigger forced.")


@mcp.tool(**CONTROL)
def set_channel(
    channel: Annotated[int, Field(ge=1, le=4, description="Analog channel number")],
    enabled: Annotated[bool | None, Field(description="Show (acquire) the channel")] = None,
    scale_v_per_div: Annotated[float | None, Field(gt=0, le=10_000, description="Vertical scale in V/div (1-2-5 steps)")] = None,
    offset_v: Annotated[float | None, Field(ge=-10_000, le=10_000, description="Vertical offset in V")] = None,
    coupling: Literal["AC", "DC", "GND"] | None = None,
    probe_ratio: Annotated[float | None, Field(gt=0, le=50_000, description="Probe attenuation, e.g. 1 or 10 - must match the probe")] = None,
    bandwidth_limit_20mhz: Annotated[bool | None, Field(description="20 MHz bandwidth limit on/off")] = None,
) -> ChannelSettings:
    """Change a channel's vertical settings; unspecified settings are left alone. The probe ratio
    is applied first because it changes the valid scale range. Returns the settings the scope
    actually applied (the scale snaps to 1-2-5 steps). Rejected values are reported from the
    scope's error queue."""
    return ChannelSettings(
        **server.driver.set_channel(channel, enabled, scale_v_per_div, offset_v, coupling, probe_ratio, bandwidth_limit_20mhz)
    )


@mcp.tool(**CONTROL)
def set_timebase(
    scale_s_per_div: Annotated[float | None, Field(gt=0, le=1000, description="Horizontal scale in s/div (1-2-5 steps)")] = None,
    offset_s: Annotated[float | None, Field(ge=-1000, le=1000, description="Horizontal (trigger) offset in s")] = None,
) -> TimebaseSettings:
    """Set the main timebase scale and/or offset. Returns the applied values."""
    d = server.driver
    tb = d.set_timebase(scale_s_per_div, offset_s)
    return TimebaseSettings(**tb, window_s=tb["scale_s_per_div"] * d.profile.screen_points / 100)


@mcp.tool(**CONTROL)
def set_trigger(
    source_channel: Annotated[int | None, Field(ge=1, le=4, description="Analog channel to trigger on")] = None,
    level_v: Annotated[float | None, Field(ge=-10_000, le=10_000, description="Trigger level in V (displayed units)")] = None,
    slope: Literal["rising", "falling", "either"] | None = None,
    sweep: Annotated[
        Literal["auto", "normal", "single"] | None,
        Field(description="auto = free-run when not triggered; normal = only on trigger; single = once"),
    ] = None,
) -> TriggerSettings:
    """Configure an edge trigger (source, level, slope) and the sweep mode. Sets the trigger type to
    EDGE. The level must lie within the source channel's screen range."""
    return TriggerSettings(**server.driver.set_edge_trigger(source_channel, level_v, slope, sweep))


@mcp.tool(**READ)
def measure(
    channel: Annotated[int, Field(ge=1, le=4)],
    items: Annotated[
        list[MeasurementName],
        Field(min_length=1, max_length=18, description="Parameters to measure"),
    ] = ["vpp", "vmax", "vmin", "vavg", "vrms", "frequency", "period"],  # noqa: B006 - copied by pydantic
) -> MeasurementResult:
    """Read the scope's automatic measurements for one channel: voltages (Vpp, Vmax, Vmin, Vtop,
    Vbase, Vamp, Vavg, Vrms, overshoot, preshoot) and timing (period, frequency, rise/fall time,
    +/- width, +/- duty). Values the scope cannot determine are null."""
    values = server.driver.measure(channel, list(items))
    return MeasurementResult(
        channel=channel,
        measurements=[
            Measurement(name=k, scpi_item=MEASUREMENTS[k][0], value=v, unit=MEASUREMENTS[k][1]) for k, v in values.items()
        ],
        timestamp=_now(),
    )


@mcp.tool(**READ, timeout=300)
def capture_waveform(
    channel: Annotated[int, Field(ge=1, le=4)],
    mode: Annotated[
        Literal["screen", "memory"],
        Field(description="'screen' = the displayed points (1000-1200); 'memory' = full acquisition memory (scope must be stopped)"),
    ] = "screen",
    max_points: Annotated[int, Field(ge=10, le=20_000, description="Max points returned (the data is downsampled)")] = 500,
    save_path: Annotated[str | None, Field(description="Write every point (time_s, volts) to this CSV file")] = None,
) -> WaveformResult:
    """Capture a channel's waveform, scaled to volts and seconds with the scope's waveform preamble,
    and return statistics plus a downsampled trace; optionally save all points to CSV.

    'memory' mode reads the whole record (bounded by the `max_memory_points` limit) in batches and
    requires the scope to be stopped; 'screen' works while running."""
    d = server.driver
    pre = d.prepare_capture(channel, mode)
    if mode == "memory":
        server.check("max_memory_points", pre.points, "memory capture")
    w = d.read_capture(channel, pre, mode)
    v = w.volts
    n = len(v)
    mean = sum(v) / n
    stats = WaveformStats(
        min_v=min(v),
        max_v=max(v),
        peak_to_peak_v=max(v) - min(v),
        mean_v=mean,
        rms_v=math.sqrt(sum(x * x for x in v) / n),
        std_v=math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0,
    )
    saved = None
    if save_path:
        target = Path(save_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s", f"ch{channel}_v"])
            writer.writerows(zip(w.time_s, v, strict=True))
        saved = str(target)
    step = max(1, math.ceil(n / max_points))
    return WaveformResult(
        channel=channel,
        mode=mode,
        points=n,
        sample_interval_s=pre.xincrement,
        start_time_s=w.time_s[0],
        stats=stats,
        clipped_fraction=w.clipped_fraction,
        time_s=w.time_s[::step],
        volts=v[::step],
        downsample_factor=step,
        saved_to=saved,
        timestamp=_now(),
    )


@mcp.tool(**READ, timeout=60)
def screenshot(
    save_path: Annotated[
        str | None, Field(description="Image file to write; default: a timestamped file in the system temp folder")
    ] = None,
    include_image: Annotated[bool, Field(description="Also return the image to the client (PNG only)")] = True,
) -> ToolResult:
    """Save a screenshot of the oscilloscope display (PNG; BMP on the MSO5000) and return its path.
    For PNG screenshots the image is also returned so the model can look at the screen."""
    data, fmt = server.driver.screenshot()
    if save_path:
        target = Path(save_path).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = Path(tempfile.gettempdir()) / "labmcp-rigol" / f"screenshot-{stamp}.{fmt}"
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    info = {"path": str(target), "format": fmt, "size_bytes": len(data), "timestamp": _now()}
    content: list = [TextContent(type="text", text=json.dumps(info))]
    if include_image and fmt == "png":
        content.append(Image(data=data, format="png").to_image_content())
    return ToolResult(content=content, structured_content=info)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
