"""MCP server for programmable DC bench power supplies (Rigol, Siglent, Aim-TTi)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_bench_psu.driver import PowerSupply, open_power_supply
from labmcp_bench_psu.simulator import make_simulator


def _int_option(ctx: ConnectContext, name: str) -> int | None:
    value = ctx.option(name)
    try:
        return int(value) if value else None
    except ValueError as exc:
        raise InstrumentConnectionError(f"--option {name} must be an integer, got {value!r}") from exc


def connect(ctx: ConnectContext) -> PowerSupply:
    dialect = (ctx.option("dialect", "auto") or "auto").strip().lower()
    if dialect in {"keysight", "agilent", "rs", "rohde"}:
        raise InstrumentConnectionError(
            "Keysight and Rohde & Schwarz supplies are not covered by this server: both vendors ship "
            "official MCP servers (see the README)."
        )
    delay = ctx.option("write_delay_s")
    transport = ctx.open_transport(
        simulator=lambda: make_simulator(dialect, ctx.option("sim_model")),
        # VISA/LAN sessions terminate with LF; Aim-TTi replies end in CR LF (stripped).
        read_termination="\n",
        write_termination="\n",
        baudrate=9600,
        timeout=3.0,
    )
    try:
        psu = open_power_supply(
            transport,
            dialect=dialect,
            channels=_int_option(ctx, "channels"),
            write_delay_s=0.0 if ctx.simulate else (float(delay) if delay else None),
        )
        psu.clear_error_state()
    except Exception:
        transport.close()
        raise
    return psu


server = InstrumentServer(
    "Bench DC Power Supply (Rigol / Siglent / Aim-TTi)",
    connect=connect,
    package="labmcp-bench-psu",
    instructions="""
Controls a programmable DC bench power supply: Rigol DP700/DP800/DP900, Siglent SPD3303X/SPD1000X
or Aim-TTi CPX/MX/QL/PL-P. Channels are numbered from 1.
- Call `get_outputs` first: it shows each channel's setpoints, measured voltage/current, whether
  the output is on and whether it is in constant-voltage (CV) or constant-current (CC) mode.
- Set the voltage AND the current limit before `output_on`. The current limit is what protects
  the load: choose the lowest value that works. `output_on` energises the load: tell the user
  which channel, voltage and current limit first.
- CC mode means the load draws the full current limit (the voltage is lower than set): check for
  a short circuit or a wrong load before raising the limit.
- Use `output_off` / `all_outputs_off` at once if anything looks wrong; they are always available.
- OVP/OCP (`set_protection`) turn the output off if exceeded; after a trip, find the cause, then
  `clear_protection_trip` (the output stays off until `output_on`).
""",
    limits=[
        Limit("max_voltage_v", 30.0, "V", "Largest voltage setpoint (magnitude) on any channel"),
        Limit("max_current_a", 3.0, "A", "Largest current-limit setpoint on any channel"),
    ],
    address_help="""\
  USB0::0x1AB1::0x0E11::DP8C000000::INSTR   USB-TMC via VISA (Rigol, Siglent)
  TCPIP0::192.168.1.50::INSTR               LAN via VISA/VXI-11
  tcp://192.168.1.50:5555                   Rigol raw socket (port as configured)
  tcp://192.168.1.51:5025                   Siglent raw socket
  tcp://192.168.1.52:9221                   Aim-TTi raw socket
  serial:///dev/ttyACM0                     Aim-TTi USB virtual COM / RS-232 (9600 baud)
  serial://COM3?write_termination=CRLF      Rigol DP800 RS-232 (needs CR LF)""",
    option_help={
        "dialect": "auto (default, from *IDN?), rigol, siglent or tti",
        "channels": "channel count for models missing from the model table",
        "write_delay_s": "pause after each write (default 0.1 s for Siglent, 0 otherwise)",
        "sim_model": "model for --simulate, e.g. DP832, DP711, SPD3303X, SPD1305X, CPX400DP, MX100TP",
    },
)
mcp = server.mcp

Channel = Annotated[int, Field(ge=1, le=4, description="Output channel number (1 = CH1 / output 1)")]


# ---------------------------------------------------------------- models


class OutputStatus(BaseModel):
    channel: int
    timestamp: str = Field(description="UTC time of the readings (ISO 8601)")
    output_on: bool | None = Field(description="Output state (None if the instrument cannot report it)")
    set_voltage_v: float | None
    set_current_a: float | None = Field(description="Current limit setpoint")
    measured_voltage_v: float | None
    measured_current_a: float | None
    measured_power_w: float | None
    mode: str | None = Field(description="'CV', 'CC' or 'UR' (unregulated); None when off or unknown")
    mode_source: str | None = Field(description="'instrument' or 'inferred' (from readback vs setpoints)")
    max_voltage_v: float | None = Field(description="Highest settable voltage on this channel (model table)")
    max_current_a: float | None
    ovp_v: float | None = None
    ovp_enabled: bool | None = None
    ovp_tripped: bool | None = None
    ocp_a: float | None = None
    ocp_enabled: bool | None = None
    ocp_tripped: bool | None = None
    notes: list[str] = Field(default_factory=list)


class AllOffResult(BaseModel):
    all_off_confirmed: bool
    outputs: list[OutputStatus]
    problems: list[str]


class ProtectionStatus(BaseModel):
    channel: int
    ovp_v: float | None
    ovp_enabled: bool | None
    ovp_tripped: bool | None
    ocp_a: float | None
    ocp_enabled: bool | None
    ocp_tripped: bool | None
    notes: list[str]


def _finite(x: float) -> float | None:
    return x if x != float("inf") else None


def _status(psu: PowerSupply, ch: int, with_protection: bool = True) -> OutputStatus:
    spec = psu.channel(ch)
    vmax = _finite(spec.max_voltage_v)
    notes = [spec.note] if spec.note else []
    set_v, set_i = psu.setpoints(ch)
    meas_v, meas_i = psu.measure(ch)
    on = psu.output_state(ch)
    mode, source = None, None
    if on is not False:
        mode = psu.mode(ch)
        source = "instrument" if mode else None
        if mode is None and on and None not in (set_v, set_i, meas_v, meas_i):
            assert set_v is not None and set_i is not None and meas_v is not None and meas_i is not None
            cc = meas_i >= 0.97 * set_i and abs(meas_v) < 0.97 * abs(set_v)
            mode, source = ("CC" if cc else "CV"), "inferred"
    status = OutputStatus(
        channel=ch,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        output_on=on,
        set_voltage_v=set_v,
        set_current_a=set_i,
        measured_voltage_v=meas_v,
        measured_current_a=meas_i,
        measured_power_w=abs(meas_v * meas_i) if meas_v is not None and meas_i is not None else None,
        mode=mode,
        mode_source=source,
        max_voltage_v=None if vmax is None else (-vmax if spec.negative else vmax),
        max_current_a=_finite(spec.max_current_a),
        notes=notes,
    )
    if with_protection and (psu.has_ovp or psu.has_ocp):
        p = psu.protection(ch)
        status.ovp_v, status.ovp_enabled, status.ovp_tripped = p.ovp_v, p.ovp_enabled, p.ovp_tripped
        status.ocp_a, status.ocp_enabled, status.ocp_tripped = p.ocp_a, p.ocp_enabled, p.ocp_tripped
        status.notes += p.notes
    return status


def _check_setpoints_against_limits(ch: int, volts: float | None, amps: float | None) -> None:
    if volts is not None:
        server.check("max_voltage_v", abs(volts), f"CH{ch} voltage setpoint")
    if amps is not None:
        server.check("max_current_a", amps, f"CH{ch} current-limit setpoint")


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def get_outputs(
    channel: Annotated[int | None, Field(ge=1, le=4, description="One channel, or omit for all")] = None,
) -> list[OutputStatus]:
    """Report each channel: voltage and current-limit setpoints, measured voltage/current/power,
    output on/off, CV/CC mode, and OVP/OCP levels and trip state where the model supports them."""
    psu = server.driver
    channels = [channel] if channel else [c.number for c in psu.channels]
    return [_status(psu, ch) for ch in channels]


@mcp.tool(**CONTROL)
def set_voltage(
    channel: Channel,
    voltage_v: Annotated[float, Field(ge=-150, le=150, description="Voltage setpoint in V (negative only on negative channels)")],
) -> OutputStatus:
    """Set a channel's voltage setpoint. Refused above `max_voltage_v` (checked even with the output
    off) or above the channel's range. If the output is on, the load sees the new voltage at once."""
    _check_setpoints_against_limits(channel, voltage_v, None)
    psu = server.driver
    spec = psu.channel(channel)
    if not spec.programmable:
        raise InstrumentProtocolError(f"CH{channel} is a fixed output ({spec.note}).")
    if spec.negative and voltage_v > 0:
        raise InstrumentProtocolError(f"CH{channel} is a negative output: give a voltage <= 0 V.")
    if not spec.negative and voltage_v < 0:
        raise InstrumentProtocolError(f"CH{channel} is a positive output: give a voltage >= 0 V.")
    if abs(voltage_v) > spec.max_voltage_v:
        raise InstrumentProtocolError(f"Refused: CH{channel} can be set to at most {spec.max_voltage_v:g} V.")
    psu.set_voltage(channel, voltage_v)
    status = _status(psu, channel, with_protection=False)
    if status.set_voltage_v is not None and abs(status.set_voltage_v - voltage_v) > 0.005 + 0.002 * abs(voltage_v):
        raise InstrumentProtocolError(
            f"CH{channel} reports a voltage setpoint of {status.set_voltage_v:g} V after setting {voltage_v:g} V."
        )
    return status


@mcp.tool(**CONTROL)
def set_current_limit(
    channel: Channel,
    current_a: Annotated[float, Field(ge=0, le=50, description="Current-limit setpoint in A")],
) -> OutputStatus:
    """Set a channel's current limit (the constant-current setpoint). Refused above `max_current_a`
    (checked even with the output off) or above the channel's range."""
    _check_setpoints_against_limits(channel, None, current_a)
    psu = server.driver
    spec = psu.channel(channel)
    if not spec.programmable:
        raise InstrumentProtocolError(f"CH{channel} is a fixed output ({spec.note}).")
    if current_a > spec.max_current_a:
        raise InstrumentProtocolError(f"Refused: CH{channel} can be set to at most {spec.max_current_a:g} A.")
    psu.set_current(channel, current_a)
    status = _status(psu, channel, with_protection=False)
    if status.set_current_a is not None and abs(status.set_current_a - current_a) > 0.002 + 0.002 * current_a:
        raise InstrumentProtocolError(
            f"CH{channel} reports a current limit of {status.set_current_a:g} A after setting {current_a:g} A."
        )
    return status


@mcp.tool(**HAZARD)
def output_on(channel: Channel) -> OutputStatus:
    """Switch a channel's output ON, energising the connected load at the present setpoints.
    The setpoints are read back first and the output is refused if they exceed the safety limits
    (e.g. after someone changed them on the front panel)."""
    psu = server.driver
    spec = psu.channel(channel)
    set_v, set_i = psu.setpoints(channel)
    if spec.programmable:
        _check_setpoints_against_limits(channel, set_v, set_i)
    else:
        _check_setpoints_against_limits(channel, spec.max_voltage_v, None)
    psu.set_output(channel, True)
    status = _status(psu, channel)
    if status.output_on is False:
        raise InstrumentProtocolError(
            f"CH{channel} did not turn on (a protection trip or front-panel lock may prevent it): {status.notes}"
        )
    if status.mode == "CC":
        status.notes.append("The output is in constant-current mode: the load draws the full current limit.")
    return status


@mcp.tool(**SAFETY)
def output_off(channel: Channel) -> OutputStatus:
    """Switch a channel's output OFF. Safe to call at any time."""
    psu = server.driver
    psu.set_output(channel, False)
    status = _status(psu, channel, with_protection=False)
    if status.output_on is True:
        raise InstrumentProtocolError(f"CH{channel} still reports ON. Switch it off at the front panel.")
    return status


@mcp.tool(**SAFETY)
def all_outputs_off() -> AllOffResult:
    """Switch every output OFF (emergency stop for the whole supply). Each channel is switched
    individually and read back where the instrument supports it."""
    psu = server.driver
    problems = psu.all_off()
    outputs = []
    for spec in psu.channels:
        try:
            outputs.append(_status(psu, spec.number, with_protection=False))
        except InstrumentError as exc:
            problems.append(f"CH{spec.number}: could not read status ({exc})")
    confirmed = not problems and all(o.output_on is not True for o in outputs)
    if problems:
        raise InstrumentProtocolError(
            "Could not confirm that all outputs are off: " + "; ".join(problems)
            + ". Switch them off at the front panel or disconnect the load."
        )
    return AllOffResult(all_off_confirmed=confirmed, outputs=outputs, problems=problems)


@mcp.tool(**CONTROL)
def set_protection(
    channel: Channel,
    ovp_v: Annotated[float | None, Field(ge=0, le=150, description="Over-voltage trip level in V")] = None,
    ocp_a: Annotated[float | None, Field(ge=0, le=50, description="Over-current trip level in A")] = None,
    enabled: Annotated[bool | None, Field(description="Switch OVP/OCP on or off (models that allow it)")] = True,
) -> ProtectionStatus:
    """Set over-voltage (OVP) and/or over-current (OCP) protection for a channel. When exceeded,
    the supply switches the output off. Rigol: levels + on/off; Aim-TTi CPX/QL/PL-P: levels (always
    armed), MX: levels + on/off; Siglent SPD1000X: levels only; SPD3303X: not available."""
    if ovp_v is None and ocp_a is None and enabled is None:
        raise InstrumentProtocolError("Give ovp_v, ocp_a and/or enabled.")
    psu = server.driver
    psu.channel(channel)
    if not (psu.has_ovp or psu.has_ocp):
        raise InstrumentProtocolError(f"{psu.idn.get('model')} has no remotely programmable OVP/OCP.")
    if ovp_v is not None or (enabled is not None and ocp_a is None):
        psu.set_ovp(channel, ovp_v, enabled)
    if ocp_a is not None or (enabled is not None and ovp_v is None):
        psu.set_ocp(channel, ocp_a, enabled)
    p = psu.protection(channel)
    return ProtectionStatus(channel=channel, ovp_v=p.ovp_v, ovp_enabled=p.ovp_enabled, ovp_tripped=p.ovp_tripped,
                            ocp_a=p.ocp_a, ocp_enabled=p.ocp_enabled, ocp_tripped=p.ocp_tripped, notes=p.notes)


@mcp.tool(**CONTROL)
def clear_protection_trip(channel: Channel) -> str:
    """Clear an OVP/OCP trip after fixing its cause. The output stays OFF; use `output_on` to
    re-energise. On Aim-TTi supplies this clears trips on all outputs."""
    psu = server.driver
    psu.channel(channel)
    return psu.clear_trips(channel)


@mcp.tool(**READ)
def get_errors() -> list[str]:
    """Read (and clear) the instrument's error queue / error registers. Empty list = no errors."""
    return server.driver.errors()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
