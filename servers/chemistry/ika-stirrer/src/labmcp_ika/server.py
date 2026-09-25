"""MCP server for IKA hotplate stirrers and overhead stirrers (NAMUR commands)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    SafetyLimitError,
)
from pydantic import BaseModel, Field

from labmcp_ika.driver import DEVICE_TYPES, HOTPLATE, OVERHEAD, WATCHDOG_MAX_S, WATCHDOG_MIN_S, IKAStirrer
from labmcp_ika.simulator import IKASimulator


def connect(ctx: ConnectContext) -> IKAStirrer:
    device = (ctx.option("device", HOTPLATE) or HOTPLATE).lower()
    if device not in DEVICE_TYPES:
        raise InstrumentProtocolError(f"--option device must be 'hotplate' or 'overhead', got {device!r}")
    # "Interfaces and outputs": 9600 baud, 7E1, no flow control, commands end with blank CR LF.
    transport = ctx.open_transport(
        simulator=lambda: IKASimulator(device=device),
        baudrate=9600,
        bytesize=7,
        parity="E",
        stopbits=1,
        read_termination="\r\n",
        write_termination=" \r\n",
        timeout=2.0,
    )
    return IKAStirrer(transport, device_type=device)


server = InstrumentServer(
    "IKA Hotplate / Overhead Stirrer (NAMUR)",
    connect=connect,
    package="labmcp-ika",
    instructions="""
Controls an IKA hotplate stirrer (C-MAG HS control, IKA Plate / RCT digital, ...) or, with
`--option device=overhead`, an IKA overhead stirrer (EUROSTAR control) over IKA's NAMUR commands.
- Call `get_status` first: it reports plate/probe temperature, speed and the setpoints.
- Setting a setpoint does not start anything; `start_heating` / `start_stirring` do. Tell the user
  before starting either, and check that a stir bar / impeller and vessel are in place.
- With an external PT1000/ETS-D probe connected, the hotplate regulates on the medium temperature;
  use `wait_for_temperature(sensor="external")` to wait for the reaction mixture itself.
- The hotplate refuses setpoints above its own safety-circuit temperature (the screwdriver dial).
- For unattended heating, `enable_watchdog` makes the hotplate switch itself off if this server
  stops talking to it (crash, cable pulled).
- Always call `stop_heating` / `stop_all` when finished or if anything looks wrong.
""",
    limits=[
        Limit("max_temperature_c", 150, "°C", "Highest hotplate temperature setpoint an agent may set"),
        Limit("max_speed_rpm", 1000, "rpm", "Highest stirring speed an agent may set"),
        Limit("max_wait_s", 3600, "s", "Longest wait_for_temperature an agent may start"),
    ],
    address_help="""\
  serial:///dev/ttyACM0              USB (virtual COM) or RS-232 (defaults 9600 baud, 7E1)
  serial://COM5                      Windows
  tcp://192.168.1.70:4001            serial-to-Ethernet adapter""",
    option_help={
        "device": "hotplate (default: C-MAG HS control, IKA Plate/RCT digital) or overhead (EUROSTAR control)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StirrerStatus(BaseModel):
    device_type: Literal["hotplate", "overhead"]
    speed_rpm: float = Field(description="Actual stirring speed")
    speed_setpoint_rpm: float
    stirring_commanded: bool | None = Field(
        description="Last start/stop sent by this server (the protocol cannot query it); None = unknown"
    )
    hotplate_temperature_c: float | None = Field(None, description="Hotplate sensor (IN_PV_2)")
    external_temperature_c: float | None = Field(
        None,
        description="External probe in the medium (IN_PV_1); None if unreadable, meaningless without a probe",
    )
    temperature_setpoint_c: float | None = None
    safety_temperature_c: float | None = Field(
        None, description="Safety-circuit temperature set on the device's dial (IN_SP_3)"
    )
    heating_commanded: bool | None = Field(
        None, description="Last heater start/stop sent by this server; None = unknown"
    )
    viscosity_trend: float | None = Field(None, description="Hotplate viscosity trend (IN_PV_5, relative)")
    probe_temperature_c: float | None = Field(None, description="Overhead stirrer PT1000 probe (IN_PV_3)")
    torque_ncm: float | None = Field(None, description="Overhead stirrer torque (IN_PV_5)")
    torque_limit_ncm: float | None = None
    speed_limit_rpm: float | None = Field(None, description="Overhead stirrer speed limit set on the device")
    safety_speed_rpm: float | None = None
    watchdog: dict[str, object] | None = None
    timestamp: str


class SetpointResult(BaseModel):
    requested: float
    reported_by_device: float = Field(description="Setpoint read back from the device after setting it")
    unit: str
    warning: str | None = None
    timestamp: str


class WaitResult(BaseModel):
    reached: bool
    sensor: str
    target_c: float
    tolerance_c: float
    final_temperature_c: float
    elapsed_s: float
    aborted: bool = Field(description="True if a stop command ended the wait early")
    trace: list[tuple[float, float]] = Field(description="(seconds since start, °C), at most ~30 points")
    message: str


def _optional(read: Callable[[], float]) -> float | None:
    """Readings that not every model/configuration provides (no probe plugged in, ...)."""
    try:
        return read()
    except InstrumentError:
        return None


def _drv() -> IKAStirrer:
    return server.driver


def _hotplate() -> IKAStirrer:
    drv = _drv()
    if drv.device_type != HOTPLATE:
        raise InstrumentProtocolError(
            "This is only available on hotplates; the server is configured for an overhead stirrer "
            "(--option device=overhead)."
        )
    return drv


@mcp.tool(**READ)
def get_status() -> StirrerStatus:
    """Read temperatures, stirring speed and setpoints. On hotplates: plate and external-probe
    temperature, temperature setpoint and the device's safety-circuit temperature. On overhead
    stirrers: PT1000 probe temperature, torque and the speed/torque limits set on the device."""
    drv = _drv()
    status = StirrerStatus(
        device_type=drv.device_type,  # type: ignore[arg-type]
        speed_rpm=drv.speed_rpm(),
        speed_setpoint_rpm=drv.speed_setpoint_rpm(),
        stirring_commanded=drv.stirring_commanded,
        timestamp=_now(),
    )
    if drv.device_type == HOTPLATE:
        status.hotplate_temperature_c = drv.hotplate_temperature_c()
        status.external_temperature_c = _optional(drv.external_temperature_c)
        status.temperature_setpoint_c = drv.temperature_setpoint_c()
        status.safety_temperature_c = drv.safety_temperature_c()
        status.viscosity_trend = _optional(drv.viscosity_trend)
        status.heating_commanded = drv.heating_commanded
        status.watchdog = drv.watchdog_state()
    else:
        status.probe_temperature_c = drv.probe_temperature_c()
        status.torque_ncm = drv.torque()
        status.torque_limit_ncm = drv.torque_limit()
        status.speed_limit_rpm = drv.speed_limit_rpm()
        status.safety_speed_rpm = drv.safety_speed_rpm()
    return status


@mcp.tool(**HAZARD)
def set_temperature(
    temperature_c: Annotated[float, Field(ge=0, le=500, description="Temperature setpoint in °C")],
) -> SetpointResult:
    """Set the hotplate temperature setpoint (OUT_SP_1). With an external probe connected this is
    the target temperature of the medium. Does not start heating; if heating is already on, the
    plate starts moving to the new setpoint immediately. Refused above `max_temperature_c` or
    above the device's own safety-circuit temperature."""
    drv = _hotplate()
    server.check("max_temperature_c", temperature_c, "temperature setpoint")
    safety = drv.safety_temperature_c()
    if temperature_c > safety:
        raise SafetyLimitError(
            f"Refused: {temperature_c:g} °C is above the hotplate's safety-circuit temperature of "
            f"{safety:g} °C (set with the dial on the device). Nothing was sent."
        )
    drv.set_temperature(temperature_c)
    reported = drv.temperature_setpoint_c()
    warning = None
    if abs(reported - temperature_c) > 1.0:
        warning = f"The hotplate reports a setpoint of {reported:g} °C, not {temperature_c:g} °C."
    return SetpointResult(
        requested=temperature_c, reported_by_device=reported, unit="°C", warning=warning, timestamp=_now()
    )


@mcp.tool(**HAZARD)
def start_heating() -> str:
    """Switch the hotplate heater on (START_1). It heats towards the current setpoint, which is
    checked against `max_temperature_c` first. Make sure the vessel and its contents can take
    the setpoint temperature and that nothing flammable is near the plate."""
    drv = _hotplate()
    setpoint = drv.temperature_setpoint_c()
    server.check("max_temperature_c", setpoint, "current temperature setpoint")
    drv.start_heater()
    return f"Heating started towards {setpoint:g} °C."


@mcp.tool(**SAFETY)
def stop_heating() -> str:
    """Switch the hotplate heater off (STOP_1). Stirring continues. The plate stays hot for a
    long time after switching off."""
    _hotplate().stop_heater()
    return "Heater switched off."


@mcp.tool(**HAZARD)
def set_speed(
    speed_rpm: Annotated[float, Field(ge=0, le=2000, description="Stirring speed setpoint in rpm")],
) -> SetpointResult:
    """Set the stirring speed setpoint (OUT_SP_4). Does not start the motor; if it is already
    running, the speed changes immediately. Refused above `max_speed_rpm` (and, on overhead
    stirrers, above the speed limit set on the device)."""
    drv = _drv()
    server.check("max_speed_rpm", speed_rpm, "stirring speed")
    if drv.device_type == OVERHEAD:
        limit = drv.speed_limit_rpm()
        if speed_rpm > limit:
            raise SafetyLimitError(
                f"Refused: {speed_rpm:g} rpm is above the speed limit of {limit:g} rpm set on the "
                "stirrer. Nothing was sent."
            )
    drv.set_speed(speed_rpm)
    reported = drv.speed_setpoint_rpm()
    warning = None
    if abs(reported - speed_rpm) > 10:
        warning = f"The stirrer reports a speed setpoint of {reported:g} rpm, not {speed_rpm:g} rpm."
    return SetpointResult(
        requested=speed_rpm, reported_by_device=reported, unit="rpm", warning=warning, timestamp=_now()
    )


@mcp.tool(**HAZARD)
def start_stirring() -> str:
    """Start the stirring motor (START_4) at the current speed setpoint, which is checked against
    `max_speed_rpm` first. Make sure the stir bar or impeller is in place and the vessel is
    secured (a decoupled stir bar or an unclamped vessel can splash)."""
    drv = _drv()
    setpoint = drv.speed_setpoint_rpm()
    server.check("max_speed_rpm", setpoint, "current speed setpoint")
    drv.start_motor()
    return f"Stirring started at {setpoint:g} rpm."


@mcp.tool(**SAFETY)
def stop_stirring() -> str:
    """Stop the stirring motor (STOP_4). Heating (if on) continues."""
    _drv().stop_motor()
    return "Motor stopped."


@mcp.tool(**SAFETY)
def stop_all() -> str:
    """Emergency stop: switch the heater off (hotplates) and stop the motor. Every stop command
    is sent even if one of them fails. Also ends a running `wait_for_temperature`."""
    errors = _drv().stop_all()
    if errors:
        raise InstrumentProtocolError("Some stop commands failed: " + "; ".join(errors))
    return "Heating and stirring stopped."


@mcp.tool(**READ, timeout=3660)
def wait_for_temperature(
    target_c: Annotated[
        float | None, Field(ge=-10, le=500, description="Temperature to wait for; default: the current setpoint")
    ] = None,
    tolerance_c: Annotated[float, Field(gt=0, le=50, description="Accepted deviation in °C")] = 1.0,
    sensor: Annotated[
        Literal["hotplate", "external"],
        Field(description="'external' = probe in the medium (IN_PV_1, or IN_PV_3 on overhead stirrers)"),
    ] = "hotplate",
    stable_for_s: Annotated[float, Field(ge=0, le=600, description="Must stay within tolerance this long")] = 30,
    timeout_s: Annotated[float, Field(ge=1, le=3600, description="Give up after this many seconds")] = 600,
    poll_interval_s: Annotated[float, Field(ge=0.5, le=60, description="Seconds between readings")] = 3.0,
) -> WaitResult:
    """Poll a temperature until it is within `tolerance_c` of the target for `stable_for_s`
    seconds, or until `timeout_s` passes. Does not change anything on the device (start heating
    first). Returns whether the target was reached plus a short temperature trace. A stop
    command ends the wait early."""
    server.check("max_wait_s", timeout_s, "wait timeout")
    drv = _drv()
    if drv.device_type == OVERHEAD:
        if sensor == "hotplate":
            raise InstrumentProtocolError("Overhead stirrers have no hotplate; use sensor='external'.")
        read = drv.probe_temperature_c
    else:
        read = drv.external_temperature_c if sensor == "external" else drv.hotplate_temperature_c
        if target_c is None:
            target_c = drv.temperature_setpoint_c()
    if target_c is None:
        raise InstrumentProtocolError("target_c is required for overhead stirrers (they have no setpoint).")

    drv.abort.clear()
    t0 = time.monotonic()
    trace: list[tuple[float, float]] = []
    inside_since: float | None = None
    reached = False
    while True:
        elapsed = time.monotonic() - t0
        temp = read()
        trace.append((round(elapsed, 1), temp))
        if abs(temp - target_c) <= tolerance_c:
            inside_since = elapsed if inside_since is None else inside_since
            if elapsed - inside_since >= stable_for_s:
                reached = True
                break
        else:
            inside_since = None
        if elapsed >= timeout_s or drv.abort.wait(min(poll_interval_s, timeout_s - elapsed)):
            break
    elapsed = time.monotonic() - t0
    aborted = drv.abort.is_set() and not reached
    step = max(1, len(trace) // 30)
    summary = trace[::step]
    if summary[-1] != trace[-1]:
        summary.append(trace[-1])
    if reached:
        message = f"Reached {target_c:g} ± {tolerance_c:g} °C ({sensor}) after {elapsed:.0f} s."
    elif aborted:
        message = "Wait ended early by a stop command."
    else:
        message = (
            f"Not reached within {timeout_s:g} s: {sensor} temperature is {trace[-1][1]:.1f} °C "
            f"(target {target_c:g} ± {tolerance_c:g} °C). Is heating on?"
        )
    return WaitResult(
        reached=reached,
        sensor=sensor,
        target_c=target_c,
        tolerance_c=tolerance_c,
        final_temperature_c=trace[-1][1],
        elapsed_s=round(elapsed, 1),
        aborted=aborted,
        trace=summary,
        message=message,
    )


@mcp.tool(**CONTROL)
def enable_watchdog(
    timeout_s: Annotated[
        int,
        Field(ge=WATCHDOG_MIN_S, le=WATCHDOG_MAX_S, description="Watchdog time (IKA allows 20-1500 s)"),
    ] = 60,
    mode: Annotated[
        Literal[1, 2],
        Field(description="1 = heater and motor off on timeout; 2 = fall back to the safety values below"),
    ] = 1,
    safety_temperature_c: Annotated[
        float, Field(ge=0, le=500, description="Mode 2 only: setpoint to fall back to (OUT_SP_12@)")
    ] = 50,
    safety_speed_rpm: Annotated[
        float, Field(ge=0, le=2000, description="Mode 2 only: speed to fall back to (OUT_SP_42@)")
    ] = 100,
) -> dict[str, object]:
    """Arm the hotplate's communication watchdog (OUT_WD1@m / OUT_WD2@m). The server then
    re-sends the watchdog command in the background; if the computer, this server or the cable
    fails, the hotplate switches heating and stirring off (mode 1) or falls back to the given
    safety values (mode 2) after `timeout_s`. Note: stopping or reconnecting the server also
    stops the refresh, so the watchdog trips unless you enable it again. Mode 1 cannot be
    cancelled over the interface."""
    drv = _hotplate()
    if mode == 2:
        server.check("max_temperature_c", safety_temperature_c, "watchdog safety temperature")
        server.check("max_speed_rpm", safety_speed_rpm, "watchdog safety speed")
        drv.set_watchdog_safety_values(safety_temperature_c, safety_speed_rpm)
    drv.enable_watchdog(mode, timeout_s)
    return drv.watchdog_state()


@mcp.tool(**CONTROL)
def disable_watchdog() -> dict[str, object]:
    """Cancel watchdog mode 2 (OUT_WD2@0) and stop the background refresh. Watchdog mode 1 has
    no cancel command; this tool reports an error for it."""
    drv = _hotplate()
    drv.disable_watchdog()
    return drv.watchdog_state()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
