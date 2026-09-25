"""MCP server for Pfeiffer Vacuum TPG 361/362/366 and TPG 261/262 gauge controllers."""

from __future__ import annotations

import csv
import math
import statistics
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

from labmcp_pfeiffer_tpg.driver import SEN_STATUS, Pressure, TPGController, describe_gauge
from labmcp_pfeiffer_tpg.simulator import TPGSimulator


def connect(ctx: ConnectContext) -> TPGController:
    model = (ctx.option("model", "auto") or "auto").lower()
    sim_model = model.upper() if model != "auto" else "TPG362"
    # Factory settings: 9600 baud, 8 data bits, no parity, 1 stop bit, no handshake. Send CR only
    # (LF is allowed on USB/Ethernet but "should be avoided"); replies end with CR LF.
    transport = ctx.open_transport(
        simulator=lambda: TPGSimulator(model=sim_model),
        baudrate=9600,
        read_termination="\r\n",
        write_termination="\r",
        timeout=2.0,
    )
    return TPGController(transport, model=model, settle_s=0.0 if ctx.simulate else 0.2)


server = InstrumentServer(
    "Pfeiffer Vacuum TPG Gauge Controller",
    connect=connect,
    package="labmcp-pfeiffer-tpg",
    instructions="""
Reads a Pfeiffer Vacuum total-pressure gauge controller: TPG 361/362/366 (ActiveLine gauges) or
TPG 261/262 (compact gauges), over the ACK/ENQ mnemonics protocol.
- Call `get_gauge_types` first: it lists which gauge is on each channel and whether it is an
  ionisation gauge that can be switched on/off.
- Pressures are returned in the controller's display unit and also converted to mbar. Only trust a
  value whose status is "ok"; under/overrange means the pressure is outside the gauge's range.
- Switching an ionisation (hot or cold cathode) gauge ON at too high a pressure can damage or
  contaminate it: `set_gauge_power` needs a trusted pressure reading below
  `max_switch_on_pressure_mbar` from the gauge itself or a `reference_channel` (e.g. a Pirani).
- Switching a gauge off can change setpoint relays that interlock valves or pumps.
""",
    limits=[
        Limit("max_log_duration_s", 3600, "s", "Longest pressure-logging series"),
        Limit(
            "max_switch_on_pressure_mbar",
            1e-2,
            "mbar",
            "Highest measured pressure at which an ionisation gauge may be switched on",
        ),
    ],
    address_help="""\
  serial:///dev/ttyUSB0            TPG 36x USB (virtual COM) or TPG 26x RS-232, 9600 8N1
  serial://COM3?baudrate=115200    non-default USB baud rate (TPG 36x parameter BAUD USB)
  tcp://192.168.1.70:8000          TPG 36x / TPG 366 Ethernet (fixed port 8000)""",
    option_help={"model": "auto (default; AYT, else TPG 26x), tpg361, tpg362, tpg366, tpg261 or tpg262"},
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PressureReading(BaseModel):
    channel: int
    status: str = Field(description="ok, underrange, overrange, sensor error, sensor off, no sensor, ...")
    pressure: float | None = Field(description="Pressure in `unit`; null unless status is ok")
    unit: str = Field(description="Controller display unit: mbar, hPa, Pa, Torr, micron or V")
    pressure_mbar: float | None = Field(description="Pressure converted to mbar (null for V or invalid)")
    raw_value: float = Field(description="Value sent by the controller (range limit when under/overrange)")
    timestamp: str


class AllPressures(BaseModel):
    readings: list[PressureReading]
    unit: str
    timestamp: str


class GaugeInfo(BaseModel):
    channel: int
    identifier: str = Field(description="Gauge identifier from TID, e.g. PKR, TPR/PCR, IKR, CMR/APR")
    description: str
    kind: str = Field(
        description="pirani, cold_cathode, pirani_cold_cathode, pirani_hot_cathode, linear, none"
    )
    ionisation_gauge: bool = Field(description="Hot or cold cathode gauge: switch on only in high vacuum")
    power: str = Field(description="on, off, or cannot be switched (SEN)")


class ChannelStats(BaseModel):
    channel: int
    valid_points: int
    first_mbar: float | None
    last_mbar: float | None
    min_mbar: float | None
    max_mbar: float | None
    geometric_mean_mbar: float | None = Field(
        description="Mean of log10(p), the natural average for pressures"
    )
    change_per_min_decades: float | None = Field(
        description="Slope of log10(p) per minute (negative = pumping down)"
    )


class PressureSeries(BaseModel):
    channels: list[int]
    count: int
    interval_s: float
    duration_s: float
    stats: list[ChannelStats]
    times_s: list[float] = Field(description="Seconds since start (downsampled to max_points)")
    pressures_mbar: dict[str, list[float | None]] = Field(description="Channel -> mbar values (downsampled)")
    saved_to: str | None = None
    started: str


def _reading(p: Pressure) -> PressureReading:
    return PressureReading(
        channel=p.channel,
        status=p.status,
        pressure=p.value,
        unit=p.unit,
        pressure_mbar=p.mbar,
        raw_value=p.raw_value,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def read_pressure(
    channel: Annotated[int, Field(ge=1, le=6, description="Measurement channel (gauge) number")] = 1,
) -> PressureReading:
    """Read one gauge channel: status, pressure in the display unit and in mbar."""
    return _reading(server.driver.pressure(channel))


@mcp.tool(**READ)
def read_all_pressures() -> AllPressures:
    """Read every channel of the controller at once (PRX), with status per channel."""
    readings = [_reading(p) for p in server.driver.pressures()]
    return AllPressures(readings=readings, unit=readings[0].unit, timestamp=_now())


def _gauge_info(tpg: TPGController) -> list[GaugeInfo]:
    with tpg.t.lock:
        ids = tpg.gauge_ids()
        states = tpg.sensor_states()
    out = []
    for ch, (ident, state) in enumerate(zip(ids, states, strict=True), start=1):
        desc, kind, ion = describe_gauge(ident)
        out.append(
            GaugeInfo(
                channel=ch,
                identifier=ident,
                description=desc,
                kind=kind,
                ionisation_gauge=ion,
                power=SEN_STATUS.get(state, f"state {state}"),
            )
        )
    return out


@mcp.tool(**READ)
def get_gauge_types() -> list[GaugeInfo]:
    """List the gauge connected to each channel (TID), what kind it is, and whether it is an
    ionisation gauge that is currently switched on or off (SEN)."""
    return _gauge_info(server.driver)


@mcp.tool(**READ)
def get_errors() -> dict[str, object]:
    """Read (and clear) the controller's ERROR word: controller error, no hardware, inadmissible
    parameter or syntax error. An empty list means no error."""
    return {"errors": server.driver.errors(), "timestamp": _now()}


@mcp.tool(**READ, timeout=7300)
def log_pressure_series(
    duration_s: Annotated[float, Field(gt=0, le=86400, description="How long to log")] = 60,
    interval_s: Annotated[float, Field(ge=0.2, le=3600, description="Seconds between readings")] = 1.0,
    channels: Annotated[list[int] | None, Field(description="Channels to include (default: all)")] = None,
    max_points: Annotated[int, Field(ge=2, le=2000, description="Points returned per channel")] = 200,
    save_path: Annotated[str | None, Field(description="Optional CSV file for every reading")] = None,
) -> PressureSeries:
    """Log pressures at a fixed interval (e.g. a pump-down curve, leak-up/rate-of-rise test or
    bake-out). Returns per-channel statistics in log-space plus a downsampled series; the full
    series can be written to CSV. Bounded by `max_log_duration_s`."""
    server.check("max_log_duration_s", duration_s, "logging duration")
    tpg = server.driver
    wanted = channels or list(range(1, tpg.channels + 1))
    for ch in wanted:
        if not 1 <= ch <= tpg.channels:
            raise InstrumentProtocolError(f"The {tpg.spec.model} has channels 1-{tpg.channels}; got {ch}.")
    started = _now()
    t0 = time.monotonic()
    times: list[float] = []
    rows: list[dict[int, Pressure]] = []
    n = int(math.floor(duration_s / interval_s + 1e-9)) + 1
    for i in range(n):
        delay = t0 + i * interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        times.append(time.monotonic() - t0)
        all_p = {p.channel: p for p in tpg.pressures()}
        rows.append({ch: all_p[ch] for ch in wanted})

    stats = []
    for ch in wanted:
        pts = [
            (t, r[ch].mbar)
            for t, r in zip(times, rows, strict=True)
            if r[ch].mbar is not None and r[ch].mbar > 0
        ]
        logs = [math.log10(p) for _, p in pts]
        slope = None
        if len(pts) > 1:
            tbar, lbar = statistics.fmean(t for t, _ in pts), statistics.fmean(logs)
            den = sum((t - tbar) ** 2 for t, _ in pts)
            if den:
                slope = (
                    sum((t - tbar) * (lg - lbar) for (t, _), lg in zip(pts, logs, strict=True)) / den * 60.0
                )
        stats.append(
            ChannelStats(
                channel=ch,
                valid_points=len(pts),
                first_mbar=pts[0][1] if pts else None,
                last_mbar=pts[-1][1] if pts else None,
                min_mbar=min(p for _, p in pts) if pts else None,
                max_mbar=max(p for _, p in pts) if pts else None,
                geometric_mean_mbar=10 ** statistics.fmean(logs) if logs else None,
                change_per_min_decades=slope,
            )
        )
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s"] + [f"ch{ch}_{x}" for ch in wanted for x in ("status", "mbar")])
            for t, r in zip(times, rows, strict=True):
                writer.writerow([f"{t:.3f}"] + [v for ch in wanted for v in (r[ch].status, r[ch].mbar)])
        saved = str(path)
    step = max(1, math.ceil(len(times) / max_points))
    return PressureSeries(
        channels=wanted,
        count=len(times),
        interval_s=interval_s,
        duration_s=times[-1] if times else 0.0,
        stats=stats,
        times_s=[round(t, 3) for t in times[::step]],
        pressures_mbar={str(ch): [r[ch].mbar for r in rows[::step]] for ch in wanted},
        saved_to=saved,
        started=started,
    )


@mcp.tool(**CONTROL)
def set_unit(
    unit: Annotated[
        Literal["mbar", "hPa", "Pa", "Torr", "micron", "V"],
        Field(description="Display/interface unit (TPG 26x: mbar, Pa or Torr only)"),
    ],
) -> dict[str, str]:
    """Change the pressure unit used on the display and interface (UNI). Readings in mbar are
    always included regardless of this setting."""
    return {"unit": server.driver.set_unit(unit), "timestamp": _now()}


@mcp.tool(**HAZARD)
def set_gauge_power(
    channel: Annotated[int, Field(ge=1, le=6, description="Channel of the ionisation gauge")],
    on: Annotated[bool, Field(description="True = switch the gauge on, False = off")],
    reference_channel: Annotated[
        int | None,
        Field(
            ge=1,
            le=6,
            description="Another gauge on the same vacuum (e.g. a Pirani) used to check the pressure",
        ),
    ] = None,
) -> list[GaugeInfo]:
    """Switch an ionisation gauge (IKR, PKR, PBR, IMR) on or off (SEN). Switching a hot or cold
    cathode gauge on at high pressure can burn out the filament or contaminate the gauge, so
    switching ON is refused unless a valid reading from this gauge or `reference_channel` is at or
    below `max_switch_on_pressure_mbar`. Switching off may trip setpoint relays used as interlocks."""
    tpg = server.driver
    with tpg.t.lock:
        ids = tpg.gauge_ids()
        if not 1 <= channel <= len(ids):
            raise InstrumentProtocolError(f"The {tpg.spec.model} has channels 1-{len(ids)}; got {channel}.")
        if tpg.sensor_states()[channel - 1] == 0:
            raise InstrumentProtocolError(
                f"Channel {channel} ({ids[channel - 1]}) cannot be switched on/off - only ionisation gauges can."
            )
        if on:
            evidence = tpg.pressure(channel)
            if not evidence.valid and reference_channel is not None:
                if reference_channel == channel:
                    raise InstrumentProtocolError("reference_channel must be a different gauge.")
                evidence = tpg.pressure(reference_channel)
            if evidence.mbar is None:
                raise InstrumentProtocolError(
                    f"No valid pressure reading to check before switching on channel {channel} "
                    f"(channel {evidence.channel}: {evidence.status}). Pass `reference_channel` of a gauge on the "
                    "same vacuum whose reading is in range. An 'underrange' reading is not accepted as proof; "
                    "if you are sure the vacuum is good, switch the gauge on at the controller."
                )
            server.check(
                "max_switch_on_pressure_mbar",
                evidence.mbar,
                f"pressure on channel {evidence.channel} before switching on the ionisation gauge",
            )
        tpg.set_sensor(channel, on)
    return _gauge_info(tpg)


@mcp.tool(**SAFETY)
def switch_gauge_off(
    channel: Annotated[int, Field(ge=1, le=6, description="Channel of the ionisation gauge to switch off")],
) -> list[GaugeInfo]:
    """Switch an ionisation gauge off (e.g. before venting, or if the pressure is rising). Always
    allowed. Note that setpoint relays assigned to this channel may change state."""
    tpg = server.driver
    with tpg.t.lock:
        if channel > tpg.channels or tpg.sensor_states()[channel - 1] == 0:
            raise InstrumentProtocolError(f"Channel {channel} has no gauge that can be switched off.")
        tpg.set_sensor(channel, False)
    return _gauge_info(tpg)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
