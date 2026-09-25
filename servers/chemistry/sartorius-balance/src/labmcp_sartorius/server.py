"""MCP server for Sartorius laboratory balances (SBI protocol)."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import (
    CONTROL,
    READ,
    ConnectContext,
    InstrumentProtocolError,
    InstrumentServer,
    InstrumentTimeout,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_sartorius.driver import BalanceBusy, SBIBalance, SBIReading
from labmcp_sartorius.simulator import SBISimulator

#: Seconds between status polls while an internal adjustment runs.
ADJUST_POLL_S = 1.0
#: If no adjustment status is seen within this time, assume the balance did not start one.
ADJUST_NO_SIGN_S = 15.0


def connect(ctx: ConnectContext) -> SBIBalance:
    # Factory settings of Cubis II/MSE, Secura/Quintix/Practum and Entris II: 9600 baud,
    # 8 data bits, odd parity, 1 stop bit; commands and replies end with CR LF.
    # The balances default to hardware handshake on RS-232; rtscts stays off by default
    # because pyserial keeps RTS asserted (all the balance needs to transmit) and USB
    # virtual COM ports may never raise CTS.
    transport = ctx.open_transport(
        simulator=SBISimulator,
        baudrate=9600,
        bytesize=8,
        parity="O",
        stopbits=1,
        rtscts=False,
        read_termination="\r\n",
        write_termination="\r\n",
        timeout=3.0,
    )
    legacy = (ctx.option("legacy", "false") or "false").lower() in {"1", "true", "yes", "on"}
    return SBIBalance(transport, legacy=legacy)


server = InstrumentServer(
    "Sartorius Balance (SBI)",
    connect=connect,
    package="labmcp-sartorius",
    instructions="""
Controls a Sartorius laboratory balance over SBI (Cubis MSE / Cubis II MCA, Secura, Quintix,
Practum, Entris II; older CP/CPA with --option legacy=true).
- `read_weight` with stable=True polls until the balance reports a stable value (SBI marks a
  stable reading by sending its unit symbol); stable=False returns the current value at once.
- Weights are net weights (gross minus tare) in the unit shown on the balance.
- Tare before weighing into a container; `zero` only works when the load is within the balance's
  zero-setting range (near empty pan).
- SBI commands are not acknowledged: `tare` and `zero` confirm by reading the balance afterwards.
- Internal adjustment only exists on balances with a built-in weight, and SBI does not report
  success; check the returned message.
""",
    limits=[
        Limit("max_series_duration_s", 600, "s", "Longest allowed weight-logging series"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0            RS-232 or USB virtual COM (defaults 9600 baud, 8 data bits, odd parity)
  serial://COM4?parity=N&bytesize=8  Windows, balance set to no parity
  tcp://192.168.1.61:49155         Cubis II "Serial transmission via Ethernet" (port as configured)""",
    option_help={
        "legacy": "true for older balances (CP/CPA, LE...) that only have ESC T for tare/zero",
    },
)
mcp = server.mcp


class WeightReading(BaseModel):
    value: float = Field(description="Net weight value")
    unit: str | None = Field(
        description="Unit sent by the balance (e.g. 'g'). SBI omits it for unstable readings; then this "
        "is the last unit seen, or None"
    )
    stable: bool = Field(description="True if the balance marked the reading as stable")
    id_code: str = Field(description="SBI 22-character-format ID code (e.g. 'N' = net); '' in 16-character format")
    timestamp: str = Field(description="UTC time the reading was taken (ISO 8601)")


class WeightSeries(BaseModel):
    readings: list[WeightReading]
    unit: str | None
    count: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    drift_per_min: float = Field(description="Least-squares slope of weight vs time, in unit/min")


class AdjustmentResult(BaseModel):
    observed_adjustment: bool = Field(
        description="True if the balance reported an adjustment in progress (Cal.* status or busy)"
    )
    duration_s: float
    final_reading: WeightReading | None
    message: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _reading(r: SBIReading, last_unit: str | None) -> WeightReading:
    return WeightReading(
        value=r.value, unit=r.unit or last_unit, stable=r.stable, id_code=r.ident, timestamp=_now()
    )


def _drv() -> SBIBalance:
    return server.driver


def _settled_near_zero(what: str, timeout_s: float) -> WeightReading:
    """After an (unacknowledged) tare/zero command, wait for a stable reading of ~0."""
    drv = _drv()
    deadline = time.monotonic() + timeout_s
    while True:
        # The first stable reading may still be the one from before the command was processed.
        r = drv.weight(stable=True, timeout=max(1.0, deadline - time.monotonic()))
        if abs(r.value) <= 5 * 10 ** (-r.decimals):  # within 5 digits of zero
            return _reading(r, drv.last_unit)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.3)
    hint = (
        "Older balances (CP/CPA, LE, ...) only understand ESC T: start the server with --option legacy=true."
        if not drv.legacy
        else "The balance may not have accepted the command."
    )
    zero_hint = (
        "For zero, the load must be within the zero-setting range (near an empty pan); use tare instead. "
        if what == "zero"
        else ""
    )
    raise InstrumentProtocolError(
        f"Sent the {what} command but the balance still reads {r.value:g} {r.unit or ''} (stable). "
        + zero_hint
        + hint
    )


@mcp.tool(**READ, timeout=90)
def read_weight(
    stable: Annotated[bool, Field(description="Wait for a stable reading (recommended)")] = True,
    timeout_s: Annotated[float, Field(ge=1, le=60, description="Max. seconds to wait for stability")] = 20,
) -> WeightReading:
    """Read the current net weight from the balance (ESC P). With stable=True the server polls
    until the balance reports a stable value; if it cannot settle (draughts, vibration,
    evaporation) you get an error; retry, or use stable=false for the current value."""
    drv = _drv()
    r = drv.weight(stable=stable, timeout=timeout_s)
    return _reading(r, drv.last_unit)


@mcp.tool(**READ, timeout=900)
def log_weight_series(
    count: Annotated[int, Field(ge=2, le=1000, description="Number of readings")] = 10,
    interval_s: Annotated[float, Field(ge=0.5, le=600, description="Seconds between readings")] = 1.0,
) -> WeightSeries:
    """Record a series of immediate (unfiltered) readings to monitor drift, evaporation,
    moisture uptake or stabilisation. Returns every reading plus summary statistics."""
    server.check("max_series_duration_s", (count - 1) * interval_s, "series duration")
    drv = _drv()
    readings: list[WeightReading] = []
    times: list[float] = []
    t0 = time.monotonic()
    for i in range(count):
        delay = t0 + i * interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        times.append(time.monotonic() - t0)
        readings.append(_reading(drv.weight(stable=False), drv.last_unit))
    values = [r.value for r in readings]
    tbar, vbar = statistics.fmean(times), statistics.fmean(values)
    denom = sum((t - tbar) ** 2 for t in times)
    slope = sum((t - tbar) * (v - vbar) for t, v in zip(times, values, strict=True)) / denom if denom else 0.0
    return WeightSeries(
        readings=readings,
        unit=drv.last_unit,
        count=count,
        mean=vbar,
        stdev=statistics.stdev(values),
        minimum=min(values),
        maximum=max(values),
        drift_per_min=slope * 60.0,
    )


@mcp.tool(**CONTROL, timeout=90)
def tare(
    timeout_s: Annotated[float, Field(ge=1, le=60, description="Max. seconds to wait for the tared reading")] = 10,
) -> WeightReading:
    """Tare the balance (ESC U, or ESC T on legacy balances): the current load (e.g. an empty
    container) becomes the tare. Then waits for a stable reading and checks that it is ~0;
    returns that reading."""
    _drv().tare()
    return _settled_near_zero("tare", timeout_s)


@mcp.tool(**CONTROL, timeout=90)
def zero(
    timeout_s: Annotated[float, Field(ge=1, le=60, description="Max. seconds to wait for the zeroed reading")] = 10,
) -> WeightReading:
    """Zero the balance (ESC V). Only works with the pan (nearly) empty, within the balance's
    zero-setting range; clears the tare. Confirms by reading ~0 afterwards."""
    _drv().zero()
    return _settled_near_zero("zero", timeout_s)


@mcp.tool(**CONTROL, timeout=330)
def run_internal_adjustment(
    timeout_s: Annotated[float, Field(ge=30, le=300, description="Max. seconds for the adjustment")] = 240,
) -> AdjustmentResult:
    """Adjust (calibrate) the balance with its built-in weight (ESC Z; isoCAL models only).
    The pan must be empty and the balance undisturbed. SBI sends no completion message, so the
    server watches for the adjustment status and waits until the balance weighs again; check
    `observed_adjustment` and the balance display or GLP printout for the result."""
    drv = _drv()
    drv.start_internal_adjustment()
    t0 = time.monotonic()
    busy_seen = False
    final: SBIReading | None = None
    while time.monotonic() - t0 < timeout_s:
        time.sleep(ADJUST_POLL_S)
        try:
            r = drv.print_reading(timeout=2.0)
        except (BalanceBusy, InstrumentTimeout):  # "Cal.Int." status, or too busy to answer
            busy_seen = True
            continue
        if busy_seen and r.stable:
            final = r
            break
        if not busy_seen and time.monotonic() - t0 > ADJUST_NO_SIGN_S:
            final = r
            break
    elapsed = time.monotonic() - t0
    if busy_seen and final is not None:
        message = f"The balance reported an adjustment and returned to weighing after {elapsed:.0f} s."
    elif busy_seen:
        message = f"The balance was still adjusting after {timeout_s:g} s; check its display."
    else:
        message = (
            f"No sign of an adjustment was seen within {ADJUST_NO_SIGN_S:g} s: the balance may have no "
            "internal weight, "
            "adjustment may be disabled or locked in its menu (verified models), or it adjusts without "
            "reporting a status over SBI. Check the display."
        )
    return AdjustmentResult(
        observed_adjustment=busy_seen,
        duration_s=round(elapsed, 1),
        final_reading=_reading(final, drv.last_unit) if final else None,
        message=message,
    )


@mcp.tool(**CONTROL)
def set_ambient_conditions(
    conditions: Literal["very_stable", "stable", "unstable", "very_unstable"],
) -> str:
    """Adapt the balance's filter to the ambient conditions (ESC K/L/M/N). Use 'unstable' or
    'very_unstable' for draughty or vibrating benches (slower but steadier readings)."""
    _drv().set_ambient_filter(conditions)
    return f"Ambient filter set to '{conditions}'."


@mcp.tool(**CONTROL)
def lock_keypad(
    locked: Annotated[bool, Field(description="True blocks the balance keys (ESC O), False unblocks (ESC R)")],
) -> str:
    """Block or unblock the balance keys, e.g. so nobody tares by accident during a long logging run."""
    _drv().lock_keys(locked)
    return "Keypad locked." if locked else "Keypad unlocked."


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
