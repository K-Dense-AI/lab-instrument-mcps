"""MCP server for Alicat mass flow / pressure meters and controllers (Alicat ASCII serial)."""

from __future__ import annotations

import csv
import statistics
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

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
    SafetyLimitError,
    prepare_save_path,
)
from pydantic import BaseModel, Field

from labmcp_alicat.driver import GASES, STATUS_CODES, AlicatDevice, DataFrame
from labmcp_alicat.simulator import AlicatSimulator

#: A logging series must finish inside one tool call (see ``timeout=`` on `log_flow_series`), even if
#: `max_series_duration_s` is raised: otherwise the call is cut off and the readings are lost.
HARD_MAX_SERIES_S = 850.0
SERIES_TOOL_TIMEOUT_S = 900.0


def connect(ctx: ConnectContext) -> AlicatDevice:
    unit_id = (ctx.option("unit_id", "A") or "A").strip().upper()
    if len(unit_id) != 1 or not "A" <= unit_id <= "Z":
        raise InstrumentConnectionError(
            f"--option unit_id must be a single letter A-Z (got {unit_id!r}). The factory default is A."
        )
    # Factory defaults (MPL manual, "Digital Control"): 19200 baud, 8N1, no flow control, CR.
    transport = ctx.open_transport(
        simulator=lambda: AlicatSimulator(unit_id=unit_id),
        baudrate=19200,
        read_termination="\r",
        write_termination="\r",
        timeout=1.0,
    )
    device = AlicatDevice(transport, unit_id)
    try:
        device.initialise()
    except Exception:
        transport.close()
        raise
    return device


server = InstrumentServer(
    "Alicat Flow / Pressure Controller (Alicat ASCII)",
    connect=connect,
    package="labmcp-alicat",
    instructions="""
Controls one Alicat Scientific mass flow meter/controller or pressure controller (MC, MCR, MCS,
MCE, MCV, M, PC, ...) over Alicat's ASCII serial protocol.
- Call `get_device_info` first: it tells you the model, whether it is a controller, the
  engineering units of every field and the setpoint full scale. Units are whatever the device
  is configured for (e.g. SCCM, SLPM, PSIA); never assume.
- `set_flow_setpoint` starts or changes gas flow (or pressure on a pressure controller). State
  the gas, value and units to the user first. A setpoint of 0 on a flow controller closes the valve.
- `close_valve` is the emergency stop: it zeroes the flow setpoint and holds all valves closed.
  Use it whenever something looks wrong. `resume_control` releases the hold.
- Only tare when NOTHING is flowing (upstream shut off or setpoint 0), and at operating pressure
  for flow tares / open to atmosphere for gauge-pressure tares. A tare with flow present stores a
  wrong zero that makes every later reading wrong.
- Changing the gas (`set_gas`) changes how the controller converts its sensor signal; do it with
  the setpoint at 0.
- Status codes such as MOV/VOV/POV (over range) or HLD (valve hold) are reported with every reading.
""",
    limits=[
        Limit("max_setpoint", 100, "device units", "Largest setpoint magnitude, in the controller's own setpoint units"),
        Limit("max_series_duration_s", 600, "s", "Longest allowed logging series"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0             USB/RS-232 (defaults 19200 baud, 8N1, CR)
  serial://COM4?baudrate=115200     Windows, non-default baud rate
  tcp://192.168.1.60:4001           serial-to-Ethernet adapter (raw TCP)""",
    option_help={"unit_id": "Alicat unit ID letter A-Z (default A); several devices can share an RS-485 bus"},
)
mcp = server.mcp


# ---------------------------------------------------------------- models


class FieldValue(BaseModel):
    name: str = Field(description="Field name as reported by the device, e.g. 'Mass Flow'")
    key: str = Field(description="Normalised key, e.g. 'mass_flow', 'abs_pressure', 'setpoint', 'gas'")
    value: float | str | None
    unit: str = Field(description="Engineering unit label from the device ('' if unknown or text)")


class StatusFlag(BaseModel):
    code: str
    meaning: str


class FlowReading(BaseModel):
    timestamp: str = Field(description="UTC time the frame was read (ISO 8601)")
    unit_id: str
    mass_flow: float | None = None
    mass_flow_unit: str = ""
    volumetric_flow: float | None = None
    volumetric_flow_unit: str = ""
    pressure: float | None = None
    pressure_unit: str = ""
    pressure_kind: str = Field("", description="'absolute', 'gauge' or 'differential'")
    temperature: float | None = None
    temperature_unit: str = ""
    setpoint: float | None = Field(None, description="Controller setpoint (None on meters)")
    setpoint_unit: str = ""
    gas: str | None = None
    valve_hold: bool = Field(description="True if HLD is active (closed-loop control bypassed)")
    status: list[StatusFlag] = Field(description="Status / error codes reported with this frame")
    fields: list[FieldValue] = Field(description="Every field of the data frame, in device order")
    layout_source: str = Field(description="How fields were identified: '??D*' (device table) or 'fallback'")
    raw: str = Field(description="The raw data frame")


def _reading(frame: DataFrame) -> FlowReading:
    def pick(*keys: str) -> tuple[float | None, str, str]:
        for key in keys:
            v = frame.get(key)
            if v is not None and isinstance(v.value, float):
                return v.value, v.unit, key
        return None, "", ""

    mass, mass_u, _ = pick("mass_flow")
    vol, vol_u, _ = pick("volumetric_flow")
    pres, pres_u, pres_key = pick("abs_pressure", "gauge_pressure", "diff_pressure")
    temp, temp_u, _ = pick("temperature")
    sp, sp_u, _ = pick("setpoint")
    gas = frame.get("gas")
    kinds = {"abs_pressure": "absolute", "gauge_pressure": "gauge", "diff_pressure": "differential"}
    return FlowReading(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        unit_id=frame.unit_id,
        mass_flow=mass,
        mass_flow_unit=mass_u,
        volumetric_flow=vol,
        volumetric_flow_unit=vol_u,
        pressure=pres,
        pressure_unit=pres_u,
        pressure_kind=kinds.get(pres_key, ""),
        temperature=temp,
        temperature_unit=temp_u,
        setpoint=sp,
        setpoint_unit=sp_u,
        gas=str(gas.value) if gas is not None and gas.value is not None else None,
        valve_hold="HLD" in frame.status_codes,
        status=[StatusFlag(code=c, meaning=STATUS_CODES.get(c, "unknown")) for c in frame.status_codes],
        fields=[FieldValue(name=v.name, key=v.key, value=v.value, unit=v.unit) for v in frame.values],
        layout_source=frame.layout_source,
        raw=frame.raw,
    )


class DeviceField(BaseModel):
    name: str
    key: str
    unit: str
    statistic: int | None = Field(description="Alicat statistic number (Serial Primer Appendix A)")


class DeviceInfo(BaseModel):
    model: str | None
    serial: str | None
    firmware: str | None
    manufactured: str | None
    calibrated: str | None
    unit_id: str
    is_controller: bool
    has_gas_select: bool
    setpoint: float | None = Field(None, description="Current setpoint (controllers, 9v00+ firmware)")
    setpoint_full_scale: float | None = Field(None, description="Largest setpoint the device accepts")
    setpoint_unit: str = ""
    setpoint_source: str | None = Field(
        None, description="'serial/front panel' or 'analog' (10v05+). With 'analog', serial setpoints are ignored."
    )
    data_frame_layout: str
    fields: list[DeviceField]
    notes: list[str]


class SetpointResult(BaseModel):
    requested: float
    accepted_setpoint: float = Field(description="Setpoint the controller reports it is now using")
    unit: str
    message: str


class GasResult(BaseModel):
    gas_number: int
    gas: str
    saved_as_power_up: bool


class GasEntry(BaseModel):
    number: int
    name: str


class SeriesPoint(BaseModel):
    t_s: float = Field(description="Seconds since the start of the series")
    mass_flow: float | None
    volumetric_flow: float | None
    pressure: float | None
    temperature: float | None
    setpoint: float | None
    status: list[str]


class FlowSeries(BaseModel):
    quantity: str = Field(description="Which field the statistics describe (mass_flow if present)")
    unit: str
    count: int
    mean: float | None
    stdev: float | None
    minimum: float | None
    maximum: float | None
    points: list[SeriesPoint]
    saved_to: str | None = None


class CloseValveResult(BaseModel):
    setpoint_zeroed: bool | None = Field(description="Flow setpoint set to 0 (None if not applicable)")
    valves_held_closed: bool
    reading: FlowReading | None
    notes: list[str]


# ---------------------------------------------------------------- helpers


def _controller() -> AlicatDevice:
    dev = server.driver
    if not dev.is_controller:
        model = dev.identity.info.get("model", "this device")
        raise InstrumentProtocolError(
            f"{model} is a meter/gauge, not a controller: it has no valve or setpoint. "
            "(Alicat meters silently ignore setpoint and valve commands.)"
        )
    return dev


def _setpoint_full_scale(dev: AlicatDevice) -> tuple[float, str] | None:
    sp = dev.layout.setpoint if dev.layout else None
    return dev.full_scale(sp.statistic if sp and sp.statistic else 37)


def _is_flow_controller(dev: AlicatDevice) -> bool:
    """True if the setpoint is a *flow* setpoint (0 closes the valve); False for pressure control."""
    if dev.layout is not None:
        sp = dev.layout.setpoint
        if sp is None:
            return False
        if dev.layout.source == "??D*":
            return sp.statistic in {36, 37, 49}
        # Legacy table (no statistic column): a controller that measures flow controls flow.
        return dev.layout.find("mass_flow") is not None or dev.layout.find("volumetric_flow") is not None
    base = dev.identity.info.get("model", "").split("-")[0].upper()
    return base.startswith(("M", "L"))


def _require_no_flow(dev: AlicatDevice, what: str) -> DataFrame:
    frame = dev.poll()
    if dev.is_controller and _is_flow_controller(dev):
        sp = frame.number("setpoint")
        if sp is not None and sp != 0:
            raise InstrumentProtocolError(
                f"Refused to {what}: the flow setpoint is {sp:g}, so gas may be flowing. Set the "
                "setpoint to 0 (or call `close_valve`), make sure no flow passes the device, then retry. "
                "Nothing was sent."
            )
    flow, stat = frame.number("mass_flow"), 5
    if flow is None:
        flow, stat = frame.number("volumetric_flow"), 4
    fs = dev.full_scale(stat) if flow is not None else None
    if fs and fs[0] > 0 and flow is not None and abs(flow) > 0.02 * fs[0]:
        raise InstrumentProtocolError(
            f"Refused to {what}: the device still reads a flow of {flow:g} ({abs(flow) / fs[0]:.1%} of "
            "full scale). Taring now would store this as zero. Stop the flow (close the upstream "
            "valve) and wait for the reading to settle, then retry. Nothing was sent."
        )
    return frame


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def read_flow() -> FlowReading:
    """Read the live data frame: mass flow, volumetric flow, pressure, temperature, setpoint
    (controllers), active gas and any status codes, each with the device's engineering units."""
    return _reading(server.driver.poll())


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Report model, serial number, firmware, calibration date, whether the device is a
    controller, the units of every data-frame field, the setpoint full scale and setpoint source."""
    dev = server.driver
    info = dev.identity.info
    notes: list[str] = []
    if "warning" in info:
        notes.append(info["warning"])
    setpoint = None
    fs = unit = None
    source = None
    if dev.is_controller:
        try:
            ls = dev.get_setpoint()
            setpoint = ls.current if ls else None
        except InstrumentError:
            pass
        fs_unit = _setpoint_full_scale(dev)
        if fs_unit:
            fs, unit = fs_unit
        code = dev.setpoint_source()
        source = {"S": "serial/front panel (saved)", "U": "serial/front panel", "A": "analog"}.get(code or "", code)
        if code == "A":
            notes.append("Setpoint source is ANALOG: serial setpoints will be ignored until it is changed "
                         "on the front panel (MENU > CONTROL > Setpoint Setup > Setpoint Source).")
    if dev.layout is None:
        notes.append("The device did not answer ??D*; fields were identified by position (fallback).")
    fields = [DeviceField(name=f.name, key=f.key, unit=f.unit, statistic=f.statistic)
              for f in (dev.layout.required if dev.layout else [])]
    sp_field = dev.layout.setpoint if dev.layout else None
    return DeviceInfo(
        model=info.get("model"),
        serial=info.get("serial"),
        firmware=info.get("firmware"),
        manufactured=info.get("manufactured"),
        calibrated=info.get("calibrated"),
        unit_id=dev.unit_id,
        is_controller=dev.is_controller,
        has_gas_select=dev.has_gas_select,
        setpoint=setpoint,
        setpoint_full_scale=fs,
        setpoint_unit=unit or (sp_field.unit if sp_field else ""),
        setpoint_source=source,
        data_frame_layout=dev.layout.source if dev.layout else "fallback",
        fields=fields,
        notes=notes,
    )


@mcp.tool(**READ)
def list_gases() -> list[GasEntry]:
    """List the gases installed on this mass flow device (number and short name), as used by
    `set_gas`. Liquid and pressure-only devices have no gas list."""
    return [GasEntry(number=n, name=name) for n, name in server.driver.gases()]


@mcp.tool(**READ, timeout=SERIES_TOOL_TIMEOUT_S)
def log_flow_series(
    count: Annotated[int, Field(ge=2, le=1000, description="Number of readings")] = 20,
    interval_s: Annotated[float, Field(ge=0.05, le=600, description="Seconds between readings")] = 1.0,
    save_path: Annotated[
        str | None, Field(description="Optional new .csv file for the full series (an existing file is not overwritten)")
    ] = None,
) -> FlowSeries:
    """Record a time series of data frames (e.g. to check flow stability, settling after a
    setpoint change, or pressure drift). Returns every point plus mean/stdev/min/max of the
    mass flow (or the main measured quantity for non-flow devices)."""
    duration = (count - 1) * interval_s
    server.check("max_series_duration_s", duration, "series duration")
    if duration > HARD_MAX_SERIES_S:
        raise SafetyLimitError(
            f"Refused: a {duration:g} s series cannot run in a single tool call (maximum {HARD_MAX_SERIES_S:g} s). "
            "Split it into several shorter series. Nothing was sent to the instrument."
        )
    path = prepare_save_path(save_path, suffixes=(".csv",)) if save_path else None
    dev = server.driver
    frames: list[tuple[float, FlowReading]] = []
    t0 = time.monotonic()
    for i in range(count):
        delay = t0 + i * interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        frames.append((time.monotonic() - t0, _reading(dev.poll())))
    first = frames[0][1]
    if first.mass_flow is not None:
        quantity, unit = "mass_flow", first.mass_flow_unit
    elif first.volumetric_flow is not None:
        quantity, unit = "volumetric_flow", first.volumetric_flow_unit
    else:
        quantity, unit = "pressure", first.pressure_unit
    values = [getattr(r, quantity) for _, r in frames if getattr(r, quantity) is not None]
    points = [
        SeriesPoint(t_s=round(t, 3), mass_flow=r.mass_flow, volumetric_flow=r.volumetric_flow, pressure=r.pressure,
                    temperature=r.temperature, setpoint=r.setpoint, status=[s.code for s in r.status])
        for t, r in frames
    ]
    saved = None
    if path is not None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "t_s"] + [f"{f.key} ({f.unit})" if f.unit else f.key for f in first.fields]
                            + ["status"])
            for t, r in frames:
                writer.writerow([r.timestamp, f"{t:.3f}"] + [f.value for f in r.fields]
                                + [" ".join(s.code for s in r.status)])
        saved = str(path)
    return FlowSeries(
        quantity=quantity,
        unit=unit,
        count=count,
        mean=statistics.fmean(values) if values else None,
        stdev=statistics.stdev(values) if len(values) > 1 else None,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        points=points,
        saved_to=saved,
    )


@mcp.tool(**HAZARD)
def set_flow_setpoint(
    setpoint: Annotated[
        float,
        Field(ge=-1e6, le=1e6, description="New setpoint in the controller's setpoint units (see get_device_info)"),
    ],
) -> SetpointResult:
    """Change the controller setpoint: starts, changes or stops gas flow (or sets the target
    pressure on a pressure controller). 0 stops flow and closes the valve on a flow controller.
    Negative values only work on bidirectional controllers. Refused on meters, above the
    `max_setpoint` limit, and above the device's full scale."""
    server.check("max_setpoint", abs(setpoint), "setpoint")
    dev = _controller()
    fs = _setpoint_full_scale(dev)
    if fs and abs(setpoint) > fs[0] * 1.0001:
        raise InstrumentProtocolError(
            f"Refused: setpoint {setpoint:g} exceeds this controller's full scale of {fs[0]:g} {fs[1]}. "
            "Nothing was sent."
        )
    reply = dev.set_setpoint(setpoint)
    sp_field = dev.layout.setpoint if dev.layout else None
    decimals = sp_field.decimals if sp_field and sp_field.decimals is not None else None
    tol = 0.51 * 10 ** -decimals if decimals is not None else max(0.01, 0.001 * abs(setpoint))
    unit = reply.unit or (sp_field.unit if sp_field else "")
    if abs(reply.requested - setpoint) > tol + 1e-9:
        hint = ""
        if dev.setpoint_source() == "A":
            hint = " The setpoint source is ANALOG, so serial setpoints are ignored."
        raise InstrumentProtocolError(
            f"The controller did not accept setpoint {setpoint:g}: it now reports {reply.requested:g} {unit}. "
            "It may be outside the controller's range (negative on a unidirectional device) or the "
            f"setpoint source may not be serial.{hint}"
        )
    return SetpointResult(
        requested=setpoint,
        accepted_setpoint=reply.requested,
        unit=unit,
        message=f"Setpoint set to {reply.requested:g} {unit}." + (" Flow stopped." if setpoint == 0 else ""),
    )


def _resolve_gas(gas: str) -> int:
    text = gas.strip()
    if text.isdigit():
        return int(text)
    for number, name in GASES.items():
        if name.lower() == text.lower():
            return number
    raise InstrumentProtocolError(
        f"Unknown gas {gas!r}. Give a gas number (Alicat Serial Primer Appendix C, or `list_gases`) or "
        f"one of: {', '.join(f'{n}={name}' for n, name in list(GASES.items())[:20])} ..."
    )


@mcp.tool(**CONTROL)
def set_gas(
    gas: Annotated[
        str,
        Field(description="Gas short name or number, e.g. 'N2' (8), 'Air' (0), 'O2' (11), 'CO2' (4), "
                          "'Ar' (1), 'He' (7), 'H2' (6), 'CH4' (2); COMPOSER mixes are 236-255"),
    ],
    save_as_power_up: Annotated[bool, Field(description="Also make it the power-up gas (10v05+)")] = False,
) -> GasResult:
    """Select the gas calibration the mass flow device uses (Gas Select). This changes how flow is
    computed, so do it with the setpoint at 0. Only gases installed on the device work (see
    `list_gases`); corrosive gases need anti-corrosive (S-series) hardware."""
    number = _resolve_gas(gas)
    if not 0 <= number <= 255:
        raise InstrumentProtocolError(f"Gas number {number} is out of range (0-255).")
    dev = server.driver
    if not dev.has_gas_select:
        raise InstrumentProtocolError("This device has no gas field in its data frame: it is not a gas mass flow device.")
    got, name = dev.set_gas(number, save=save_as_power_up)
    return GasResult(gas_number=got, gas=name or GASES.get(got, str(got)),
                     saved_as_power_up=save_as_power_up and dev.firmware_at_least(10, 5))


@mcp.tool(**CONTROL)
def tare_flow() -> FlowReading:
    """Tare (zero) the flow reading. ONLY run with no flow through the device: upstream shut off
    or controller setpoint 0, with the line at its normal operating pressure. Refused if the
    setpoint is not 0 or a significant flow is still being measured."""
    dev = server.driver
    _require_no_flow(dev, "tare the flow")
    return _reading(dev.tare_flow())


@mcp.tool(**CONTROL)
def tare_pressure(
    kind: Annotated[
        Literal["gauge", "absolute"],
        Field(description="'gauge' zeroes a gauge/differential sensor; 'absolute' aligns the absolute "
                          "sensor to the built-in barometer (barometer option required)"),
    ] = "gauge",
) -> FlowReading:
    """Tare a pressure reading. Requires no flow and the device OPEN TO ATMOSPHERE (gauge), or no
    flow and an unpressurised process line (absolute, barometer-equipped devices only)."""
    dev = server.driver
    _require_no_flow(dev, f"tare {kind} pressure")
    frame = dev.tare_gauge_pressure() if kind == "gauge" else dev.tare_absolute_pressure()
    return _reading(frame)


@mcp.tool(**CONTROL)
def hold_valve() -> FlowReading:
    """Freeze the controller's valve(s) at their current position (HLD): closed-loop control stops,
    so flow will drift if upstream pressure changes. Use `resume_control` to release, or
    `close_valve` to shut off completely."""
    frame = _controller().hold_position()
    if "HLD" not in frame.status_codes:
        raise InstrumentProtocolError(f"Valve hold not confirmed (no HLD in reply {frame.raw!r}).")
    return _reading(frame)


@mcp.tool(**HAZARD)
def resume_control() -> FlowReading:
    """Cancel any valve hold and resume closed-loop control to the current setpoint. Flow restarts
    immediately if the setpoint is not 0 (check it with `read_flow` first)."""
    frame = _controller().cancel_hold()
    if "HLD" in frame.status_codes:
        raise InstrumentProtocolError(f"The valve hold is still active after 'C': {frame.raw!r}")
    return _reading(frame)


@mcp.tool(**SAFETY)
def close_valve() -> CloseValveResult:
    """Stop the flow: set the flow setpoint to 0 and hold all valves closed (Alicat HC). Safe to
    call at any time. On pressure controllers the pressure setpoint is left unchanged and the
    valves are held closed, trapping the current pressure."""
    dev = server.driver
    notes: list[str] = []
    if not dev.is_controller:
        return CloseValveResult(setpoint_zeroed=None, valves_held_closed=False, reading=_reading(dev.poll()),
                                notes=["This device is a meter/gauge: it has no valve to close."])
    zeroed: bool | None = None
    if _is_flow_controller(dev):
        try:
            dev.set_setpoint(0)
            zeroed = True
        except InstrumentError as exc:
            zeroed = False
            notes.append(f"Setting the setpoint to 0 failed: {exc}")
    held = False
    frame = None
    try:
        frame = dev.hold_closed()
        held = "HLD" in frame.status_codes
        if not held:
            notes.append(f"The reply did not show HLD: {frame.raw!r}")
    except InstrumentError as exc:
        notes.append(f"Hold-valves-closed (HC, firmware 5v07+) failed: {exc}")
    if not held and not zeroed:
        raise InstrumentProtocolError(
            "Could not confirm that the valve is closed: " + " ".join(notes)
            + " Close the upstream gas supply manually."
        )
    try:
        reading = _reading(frame if frame is not None else dev.poll())
    except InstrumentError:
        reading = None
    if held:
        notes.append("Valves held closed (HLD). Use `resume_control` to return to closed-loop control.")
    return CloseValveResult(setpoint_zeroed=zeroed, valves_held_closed=held, reading=reading, notes=notes)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
