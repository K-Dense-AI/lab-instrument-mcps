"""MCP server for JULABO heating and refrigerated circulators."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_julabo.driver import JulaboCirculator
from labmcp_julabo.simulator import JulaboSimulator


def connect(ctx: ConnectContext) -> JulaboCirculator:
    # RS-232 factory settings ("RS232 interface" in the manuals): 4800 baud, 7 data bits, even
    # parity, 1 stop bit, hardware handshake. Commands end with CR, replies with CR LF.
    # rtscts stays off by default: pyserial keeps RTS asserted, which is all the circulator
    # needs to transmit, and USB virtual COM ports often never raise CTS (writes would stall).
    transport = ctx.open_transport(
        simulator=JulaboSimulator,
        baudrate=4800,
        bytesize=7,
        parity="E",
        stopbits=1,
        rtscts=False,
        read_termination="\r\n",
        write_termination="\r",
        timeout=3.0,
    )
    try:
        delay = _seconds_option(ctx, "command_delay_s", "0.25", minimum=0.0)
        keepalive = _seconds_option(ctx, "keepalive_s", "0", minimum=0.0)
    except InstrumentConnectionError:
        transport.close()
        raise
    drv = JulaboCirculator(transport, command_delay_s=0.0 if ctx.simulate else delay)
    if keepalive > 0:
        drv.start_keepalive(keepalive)
    return drv


def _seconds_option(ctx: ConnectContext, name: str, default: str, *, minimum: float) -> float:
    """A ``--option name=<seconds>``; a negative or non-numeric value would otherwise only fail
    later, inside a setting or stop command (``time.sleep`` rejects negative delays)."""
    raw = ctx.option(name, default) or default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < minimum:
        raise InstrumentConnectionError(f"--option {name} must be a number of seconds >= {minimum:g} (got {raw!r}).")
    return value


server = InstrumentServer(
    "JULABO Circulator",
    connect=connect,
    package="labmcp-julabo",
    instructions="""
Controls a JULABO heating or refrigerated circulator (CORIO CD/CP, MAGIO, DYNEO) over its
interface commands.
- Remote control must be enabled on the circulator's own menu; `get_status` tells you
  (status 02/03 = remote, 00/01 = manual, negative = alarm or rejected command).
- `set_setpoint` changes the target temperature; `start_circulation` starts heating/cooling and
  the pump. Tell the user before starting and make sure the bath is filled and hoses are connected.
- Setpoints outside [min_temperature_c, max_temperature_c] are refused: they protect the bath
  fluid and the application. The circulator also refuses values outside its own warning limits.
- `read_temperatures` gives the bath temperature and heating power (negative = cooling);
  `wait_for_temperature` waits until the bath (or the external sensor) is stable at the target.
- If an alarm appears, or anything looks wrong, call `stop_circulation`.
""",
    limits=[
        Limit("max_temperature_c", 90, "°C", "Highest setpoint an agent may set"),
        Limit("min_temperature_c", 5, "°C", "Lowest setpoint an agent may set", kind="min"),
        Limit("max_wait_s", 7200, "s", "Longest wait_for_temperature an agent may start"),
    ],
    address_help="""\
  serial:///dev/ttyACM0             USB (virtual COM port; serial settings do not matter)
  serial://COM3                     RS-232, factory defaults 4800 baud, 7E1
  serial://COM3?rtscts=true         enforce the RTS/CTS hardware handshake
  serial://COM3?baudrate=9600       RS-232 with a non-default baud rate set on the device
  tcp://192.168.1.80:4001           serial-to-Ethernet adapter""",
    option_help={
        "command_delay_s": "pause after each OUT command before querying status (default 0.25)",
        "keepalive_s": "query status every N seconds to feed a watchdog set up on the circulator (default off)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Temperatures(BaseModel):
    bath_temperature_c: float = Field(description="Actual bath / internal temperature (in_pv_00)")
    heating_power_pct: float = Field(description="Actuating variable in %; negative = cooling (in_pv_01)")
    safety_sensor_temperature_c: float = Field(description="High-temperature safety sensor (in_pv_03)")
    external_temperature_c: float | None = Field(
        None, description="External Pt100 sensor (in_pv_02); None if not requested or not available (CORIO)"
    )
    timestamp: str


class CirculatorStatus(BaseModel):
    code: int = Field(description="Status code: 00-03 operating state, negative = alarm or rejected command")
    message: str = Field(description="Status text sent by the circulator")
    meaning: str
    kind: Literal["state", "command_error", "alarm"]
    remote_control: bool | None = Field(description="True if remote control is enabled (None during alarms)")
    running: bool = Field(description="Temperature control started (in_mode_05)")
    setpoint_c: float
    bath_temperature_c: float
    excess_temperature_protection_c: float = Field(
        description="Setting of the high-temperature safety function on the device (in_pv_04)"
    )
    high_warning_limit_c: float | None = Field(None, description="in_sp_03; not available on CORIO CD")
    low_warning_limit_c: float | None = Field(None, description="in_sp_04; not available on CORIO CD")
    firmware: str
    timestamp: str


class SetpointResult(BaseModel):
    setpoint_c: float = Field(description="Setpoint read back from the circulator")
    running: bool
    status: str
    timestamp: str


class WaitResult(BaseModel):
    reached: bool
    sensor: str
    target_c: float
    tolerance_c: float
    final_temperature_c: float
    elapsed_s: float
    aborted: bool = Field(description="True if a stop command or an alarm ended the wait early")
    trace: list[tuple[float, float]] = Field(description="(seconds since start, °C), at most ~30 points")
    message: str


def _drv() -> JulaboCirculator:
    return server.driver


@mcp.tool(**READ)
def read_temperatures(
    include_external: Annotated[
        bool, Field(description="Also read the external Pt100 sensor (MAGIO/DYNEO; not CORIO)")
    ] = False,
) -> Temperatures:
    """Read the bath temperature, the heating/cooling power in % and the safety-sensor
    temperature, and optionally the external Pt100 sensor."""
    drv = _drv()
    return Temperatures(
        bath_temperature_c=drv.bath_temperature_c(),
        heating_power_pct=drv.heating_power_pct(),
        safety_sensor_temperature_c=drv.safety_sensor_temperature_c(),
        external_temperature_c=drv.optional_number("in_pv_02") if include_external else None,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_status() -> CirculatorStatus:
    """Report the circulator's status message (decoded: operating state, rejected command or
    alarm), whether temperature control is running, the setpoint, the device's own
    excess-temperature protection setting and warning limits, and the firmware version."""
    drv = _drv()
    st = drv.status()
    high, low = drv.warning_limits_c()  # None on CORIO CD, which has no warning-limit commands
    return CirculatorStatus(
        code=st.code,
        message=st.label,
        meaning=st.meaning,
        kind=st.kind,  # type: ignore[arg-type]
        remote_control=st.remote,
        running=drv.is_running(),
        setpoint_c=drv.setpoint_c(),
        bath_temperature_c=drv.bath_temperature_c(),
        excess_temperature_protection_c=drv.excess_temperature_protection_c(),
        high_warning_limit_c=high,
        low_warning_limit_c=low,
        firmware=drv.version(),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_setpoint() -> SetpointResult:
    """Read the current temperature setpoint and whether temperature control is running."""
    drv = _drv()
    return SetpointResult(
        setpoint_c=drv.setpoint_c(), running=drv.is_running(), status=drv.status().label, timestamp=_now()
    )


@mcp.tool(**HAZARD)
def set_setpoint(
    temperature_c: Annotated[float, Field(ge=-95, le=400, description="New setpoint in °C")],
) -> SetpointResult:
    """Set the circulator's temperature setpoint (out_sp_00). If temperature control is running,
    the circulator starts heating or cooling to it immediately. Refused outside
    `min_temperature_c`..`max_temperature_c`; the circulator itself also rejects values outside
    its range or warning limits (reported as an error). Needs remote control enabled."""
    server.check("max_temperature_c", temperature_c, "setpoint")
    server.check("min_temperature_c", temperature_c, "setpoint")
    drv = _drv()
    st = drv.set_setpoint(temperature_c)
    return SetpointResult(
        setpoint_c=drv.setpoint_c(), running=drv.is_running(), status=st.label, timestamp=_now()
    )


@mcp.tool(**HAZARD)
def start_circulation() -> SetpointResult:
    """Start temperature control (out_mode_05 1): the pump runs and the bath heats or cools to
    the setpoint, which is checked against the safety limits first (it may have been changed on
    the front panel). Make sure the bath is filled and any external hoses are connected and
    secured. Needs remote control enabled."""
    drv = _drv()
    setpoint = drv.setpoint_c()
    server.check("max_temperature_c", setpoint, "current setpoint")
    server.check("min_temperature_c", setpoint, "current setpoint")
    st = drv.start()
    return SetpointResult(setpoint_c=setpoint, running=True, status=st.label, timestamp=_now())


@mcp.tool(**SAFETY)
def stop_circulation() -> str:
    """Stop temperature control and the pump (out_mode_05 0), then confirm with in_mode_05.
    Also ends a running `wait_for_temperature`. The bath stays hot/cold after stopping."""
    if not _drv().stop():
        raise InstrumentProtocolError(
            "Sent the stop command twice but the circulator still reports it is running. Stop it "
            "on the front panel (or switch it off at the mains switch)."
        )
    return "Temperature control stopped."


@mcp.tool(**READ, timeout=7260)
def wait_for_temperature(
    target_c: Annotated[
        float | None, Field(ge=-95, le=400, description="Temperature to wait for; default: the current setpoint")
    ] = None,
    tolerance_c: Annotated[float, Field(gt=0, le=20, description="Accepted deviation in °C")] = 0.5,
    sensor: Annotated[
        Literal["bath", "external"], Field(description="'external' = Pt100 sensor (MAGIO/DYNEO)")
    ] = "bath",
    stable_for_s: Annotated[
        float, Field(ge=0, le=1800, description="Must stay within tolerance this long")
    ] = 60,
    timeout_s: Annotated[float, Field(ge=1, le=7200, description="Give up after this many seconds")] = 1800,
    poll_interval_s: Annotated[float, Field(ge=0.5, le=120, description="Seconds between readings")] = 5.0,
) -> WaitResult:
    """Poll the bath (or external) temperature until it has been within `tolerance_c` of the
    target for `stable_for_s` seconds, or `timeout_s` passes. Does not change anything; start
    circulation first. Stops early (reached = false) if the circulator raises an alarm or a
    stop command is sent. Returns a short temperature trace."""
    server.check("max_wait_s", timeout_s, "wait timeout")
    drv = _drv()
    read = drv.external_temperature_c if sensor == "external" else drv.bath_temperature_c
    target = drv.setpoint_c() if target_c is None else target_c

    drv.abort.clear()
    t0 = time.monotonic()
    trace: list[tuple[float, float]] = []
    inside_since: float | None = None
    reached = False
    alarm: str | None = None
    while True:
        elapsed = time.monotonic() - t0
        temp = read()
        trace.append((round(elapsed, 1), temp))
        st = drv.status()
        if st.kind == "alarm":
            alarm = f"{st.label}: {st.meaning}"
            break
        if abs(temp - target) <= tolerance_c:
            inside_since = elapsed if inside_since is None else inside_since
            if elapsed - inside_since >= stable_for_s:
                reached = True
                break
        else:
            inside_since = None
        if elapsed >= timeout_s or drv.abort.wait(min(poll_interval_s, timeout_s - elapsed)):
            break
    elapsed = time.monotonic() - t0
    aborted = not reached and (alarm is not None or drv.abort.is_set())
    step = max(1, len(trace) // 30)
    summary = trace[::step]
    if summary[-1] != trace[-1]:
        summary.append(trace[-1])
    if reached:
        message = f"Reached {target:g} ± {tolerance_c:g} °C ({sensor}) after {elapsed:.0f} s."
    elif alarm:
        message = f"Wait ended by an alarm: {alarm}"
    elif aborted:
        message = "Wait ended early by a stop command."
    else:
        message = (
            f"Not reached within {timeout_s:g} s: {sensor} temperature is {trace[-1][1]:.2f} °C "
            f"(target {target:g} ± {tolerance_c:g} °C). Is temperature control running?"
        )
    return WaitResult(
        reached=reached,
        sensor=sensor,
        target_c=target,
        tolerance_c=tolerance_c,
        final_temperature_c=trace[-1][1],
        elapsed_s=round(elapsed, 1),
        aborted=aborted,
        trace=summary,
        message=message,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
