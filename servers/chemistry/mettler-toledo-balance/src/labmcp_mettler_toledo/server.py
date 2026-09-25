"""MCP server for Mettler Toledo balances (MT-SICS)."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import CONTROL, HAZARD, READ, SAFETY, ConnectContext, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_mettler_toledo.driver import MTSICSBalance, Weight
from labmcp_mettler_toledo.simulator import MTSICSSimulator


def connect(ctx: ConnectContext) -> MTSICSBalance:
    # Factory defaults of Excellence / XPR / NewClassic balances: 9600 8N1, Xon/Xoff, CR LF.
    transport = ctx.open_transport(
        simulator=MTSICSSimulator,
        baudrate=9600,
        xonxoff=True,
        read_termination="\r\n",
        write_termination="\r\n",
        timeout=5.0,
    )
    return MTSICSBalance(transport)


server = InstrumentServer(
    "Mettler Toledo Balance (MT-SICS)",
    connect=connect,
    package="labmcp-mettler-toledo",
    instructions="""
Controls a Mettler Toledo laboratory balance over MT-SICS (Excellence, XPR/XSR, XP/XS, MS/ML,
NewClassic, ME/MA and other MT-SICS-compatible balances and weighing terminals).
- `read_weight` with stable=True waits for the balance to settle (preferred for measurements);
  stable=False returns the current, possibly drifting value immediately.
- Weights are *net* weights (gross minus tare) in the balance's configured unit.
- Tare before weighing into a container. Zeroing also clears the tare memory.
- Draft-shield and adjustment commands only exist on balances that have that hardware.
""",
    limits=[
        Limit("max_series_duration_s", 600, "s", "Longest allowed weight-logging series"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0            USB/RS-232 (defaults 9600 baud, 8N1, Xon/Xoff)
  serial://COM4?baudrate=19200     Windows, non-default baud rate
  tcp://192.168.1.60:8001          XPR/XSR or serial-to-Ethernet adapter (port as configured)""",
)
mcp = server.mcp


class WeightReading(BaseModel):
    value: float = Field(description="Net weight value")
    unit: str = Field(description="Unit reported by the balance, e.g. 'g' or 'mg'")
    stable: bool = Field(description="True if the balance reported a stable reading")
    timestamp: str = Field(description="UTC time the reading was taken (ISO 8601)")


class WeightSeries(BaseModel):
    readings: list[WeightReading]
    unit: str
    count: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    drift_per_min: float = Field(description="Least-squares slope of weight vs time, in unit/min")


def _reading(w: Weight) -> WeightReading:
    return WeightReading(
        value=w.value,
        unit=w.unit,
        stable=w.stable,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    )


@mcp.tool(**READ)
def read_weight(
    stable: Annotated[bool, Field(description="Wait for a stable reading (recommended)")] = True,
) -> WeightReading:
    """Read the current net weight from the balance."""
    return _reading(server.driver.weight(stable=stable))


@mcp.tool(**READ, timeout=900)
def log_weight_series(
    count: Annotated[int, Field(ge=2, le=1000, description="Number of readings")] = 10,
    interval_s: Annotated[float, Field(ge=0.1, le=600, description="Seconds between readings")] = 1.0,
) -> WeightSeries:
    """Record a series of immediate (unfiltered) readings to monitor drift, evaporation,
    moisture uptake or stabilisation. Returns every reading plus summary statistics."""
    server.check("max_series_duration_s", (count - 1) * interval_s, "series duration")
    readings: list[WeightReading] = []
    t0 = time.monotonic()
    times: list[float] = []
    for i in range(count):
        target = t0 + i * interval_s
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        times.append(time.monotonic() - t0)
        readings.append(_reading(server.driver.weight(stable=False)))
    values = [r.value for r in readings]
    tbar = statistics.fmean(times)
    vbar = statistics.fmean(values)
    denom = sum((t - tbar) ** 2 for t in times)
    slope = sum((t - tbar) * (v - vbar) for t, v in zip(times, values, strict=True)) / denom if denom else 0.0
    return WeightSeries(
        readings=readings,
        unit=readings[0].unit,
        count=count,
        mean=vbar,
        stdev=statistics.stdev(values),
        minimum=min(values),
        maximum=max(values),
        drift_per_min=slope * 60.0,
    )


@mcp.tool(**READ)
def get_tare() -> WeightReading:
    """Return the weight currently stored in the tare memory."""
    return _reading(server.driver.tare_value())


@mcp.tool(**CONTROL)
def tare(
    immediately: Annotated[
        bool, Field(description="Tare now even if unstable (less accurate). Default waits for stability.")
    ] = False,
) -> WeightReading:
    """Tare the balance: store the current load (e.g. an empty container) as the tare weight.
    Returns the stored tare weight."""
    return _reading(server.driver.tare(immediately=immediately))


@mcp.tool(**CONTROL)
def set_tare_preset(
    value: Annotated[float, Field(ge=0, description="Tare weight to preset")],
    unit: Annotated[str, Field(description="Must be the balance's unit 1, usually 'g'")] = "g",
) -> WeightReading:
    """Preset a known tare weight (e.g. a container weighed earlier)."""
    return _reading(server.driver.preset_tare(value, unit))


@mcp.tool(**CONTROL)
def clear_tare() -> str:
    """Clear the tare memory (tare = 0)."""
    server.driver.clear_tare()
    return "Tare cleared."


@mcp.tool(**CONTROL)
def zero(
    immediately: Annotated[bool, Field(description="Zero now even if the reading is unstable")] = False,
) -> str:
    """Zero the balance with the current load. Also clears the tare memory."""
    stable = server.driver.zero(immediately=immediately)
    return "Balance zeroed" + ("." if stable else " (reading was not stable).")


@mcp.tool(**CONTROL, timeout=330)
def run_internal_adjustment() -> str:
    """Adjust (calibrate) the balance with its built-in reference weight (MT-SICS C3).
    The pan must be empty and the balance undisturbed. Takes roughly 1-3 minutes."""
    server.driver.internal_adjustment()
    return "Internal adjustment completed successfully."


@mcp.tool(**CONTROL)
def show_message(
    text: Annotated[str, Field(max_length=40, description="Text for the balance display; '' clears it")],
) -> str:
    """Show a short message on the balance display (e.g. 'Add sample 3'). Use
    `show_weight_display` to return to the normal weight display."""
    server.driver.display_text(text)
    return "Message displayed."


@mcp.tool(**CONTROL)
def show_weight_display() -> str:
    """Switch the balance display back to showing the weight."""
    server.driver.display_weight()
    return "Weight display restored."


@mcp.tool(**READ)
def get_draft_shield() -> str:
    """Report the position of the motorised draft-shield doors (Excellence/XPR balances only)."""
    return server.driver.door_position()


@mcp.tool(**HAZARD)
def set_draft_shield(position: Literal["closed", "right_open", "left_open"]) -> str:
    """Open or close the motorised draft-shield doors. Make sure nothing (and no one's
    fingers) is in the way of the doors."""
    code = {"closed": 0, "right_open": 1, "left_open": 2}[position]
    server.driver.set_door(code)
    return f"Draft shield moving to {position}."


@mcp.tool(**READ)
def read_temperature() -> list[float]:
    """Read the balance's internal temperature probe(s) in °C (MT-SICS M28, if supported)."""
    return server.driver.temperature_c()


@mcp.tool(**SAFETY)
def reset_balance() -> str:
    """Abort whatever the balance is doing (an adjustment, a repeating weight stream, a pending
    command) and reset it to its power-on state without zeroing (MT-SICS @). Clears the tare.
    Motorised draft-shield doors have their own obstruction detection and reverse if blocked."""
    serial = server.driver.reset()
    return f"Balance reset (serial number {serial})."


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
