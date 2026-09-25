"""MCP server for New Era Pump Systems syringe pumps (NE-1000 family, RS-232 Basic mode)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Annotated, Literal

from labmcp import (
    CONTROL,
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

from labmcp_new_era.driver import (
    ALARMS,
    PROMPTS,
    RATE_UNIT_NAMES,
    RATE_UNITS,
    SYRINGE_PRESETS_MM,
    VOLUME_UNITS,
    NewEraPump,
    find_syringe_preset,
    ne1000_rate_range_ml_min,
)
from labmcp_new_era.simulator import NewEraSimulator


def _address_option(ctx: ConnectContext) -> int | None:
    raw = ctx.option("pump_address")
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or not 0 <= int(raw) <= 99:
        raise InstrumentConnectionError(f"--option pump_address must be 0-99, got {raw!r}")
    return int(raw)


def connect(ctx: ConnectContext) -> NewEraPump:
    address = _address_option(ctx)
    speed = float(ctx.option("sim_speed", "1") or 1)
    # Factory defaults (NE-1000 manual 10.1/10.2): 19200 baud, 8N1, no flow control, address 0.
    # Commands end with CR; replies are STX ... ETX with no line terminator.
    transport = ctx.open_transport(
        simulator=lambda: NewEraSimulator(address=address or 0, speed=speed),
        baudrate=19200,
        read_termination="\x03",
        write_termination="\r",
        timeout=2.0,
    )
    return NewEraPump(transport, address=address)


server = InstrumentServer(
    "New Era Syringe Pump (RS-232)",
    connect=connect,
    package="labmcp-new-era",
    instructions="""
Controls a New Era Pump Systems syringe pump (NE-1000, NE-1002X, NE-4000, NE-500, NE-8000 and other
NE-1000-series pumps) over its RS-232 Basic-mode command set.
- Volumes and rates are only correct if the syringe inside diameter is right: check it with
  `get_status` and set it with `set_syringe` (preset name or measured diameter) before pumping.
- `infuse` / `withdraw` program the pump to move an exact volume at a fixed rate and then stop.
  They return immediately; poll `get_status` or `get_dispensed_volume` to follow progress.
- The pump only knows plunger travel: it cannot detect an empty syringe, a closed valve or a
  disconnected line. Ask the user to confirm the syringe, tubing and destination first.
- A stall alarm means the plunger is blocked (end of travel, closed valve, clogged line). Never
  simply restart after a stall: have the user check the fluid path.
- Call `stop_pump` immediately if anything looks wrong.
""",
    limits=[
        Limit("max_rate_ml_min", 10.0, "mL/min", "Highest pumping rate an agent may set"),
        Limit("max_volume_ml", 20.0, "mL", "Largest volume a single infuse/withdraw may move"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0            USB-RS-232 cable to the pump's 'To Computer' jack (19200 baud, 8N1)
  serial://COM5?baudrate=9600      Windows, pump set to a non-default baud rate
  tcp://192.168.1.70:4001          serial-to-Ethernet adapter (raw TCP)""",
    option_help={
        "pump_address": "network address 0-99 of the pump to control on a daisy-chained network "
        "(default: no address prefix, i.e. address 0)",
        "sim_speed": "simulator only: time acceleration factor (default 1 = real time)",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _vol_ml(value: float, units: str) -> float:
    return value * VOLUME_UNITS[units]


class DispensedVolume(BaseModel):
    infused_ml: float = Field(description="Volume infused since the accumulator was last cleared")
    withdrawn_ml: float = Field(description="Volume withdrawn since the accumulator was last cleared")
    pump_units: str = Field(description="Units the pump itself uses for volumes: 'UL' or 'ML'")
    timestamp: str


class PumpStatus(BaseModel):
    state: str = Field(description="infusing, withdrawing, stopped, paused, purging, ... or alarm")
    running: bool
    alarm: str | None = Field(None, description="Alarm the pump reported (now acknowledged), if any")
    diameter_mm: float = Field(description="Syringe inside diameter setting")
    rate_ml_min: float | None = Field(None, description="Rate setting (or actual rate while pumping)")
    rate_setting: str | None = Field(None, description="Rate as stored on the pump, e.g. '250.0 µL/min'")
    target_volume_ml: float | None = Field(
        None, description="'Volume to be dispensed' of phase 1 (0 = continuous)"
    )
    direction: str | None = Field(None, description="INF (infuse), WDR (withdraw) or STK (sticky)")
    dispensed: DispensedVolume
    timestamp: str


class DispenseStarted(BaseModel):
    direction: Literal["infuse", "withdraw"]
    volume_ml: float = Field(
        description="Volume programmed on the pump (after rounding to its 4-digit format)"
    )
    rate_ml_min: float = Field(description="Rate programmed on the pump (after rounding)")
    pump_commands: str = Field(description="Rate and volume exactly as sent, e.g. 'RAT 1.500MM, VOL 0.500ML'")
    diameter_mm: float
    estimated_duration_s: float
    state: str
    note: str
    timestamp: str


class SyringePreset(BaseModel):
    name: str
    diameter_mm: float
    ne1000_min_rate_ul_h: float = Field(description="Lowest rate on an NE-1000 with this syringe")
    ne1000_max_rate_ml_min: float = Field(description="Highest rate on an NE-1000 with this syringe")


def _dispensed(pump: NewEraPump) -> DispensedVolume:
    inf, wdr, units = pump.dispensed()
    return DispensedVolume(
        infused_ml=_vol_ml(inf, units), withdrawn_ml=_vol_ml(wdr, units), pump_units=units, timestamp=_now()
    )


@mcp.tool(**READ)
def get_status() -> PumpStatus:
    """Report what the pump is doing (infusing/withdrawing/stopped/paused, alarms), the syringe
    diameter, rate, target volume, direction and the accumulated dispensed volumes."""
    pump = server.driver
    st = pump.status()
    alarm = None
    if st.alarm:
        alarm = f"A?{st.alarm}: {ALARMS.get(st.alarm, 'unknown alarm')}"
        st = pump.status()  # the first reply acknowledged the alarm
    rate_ml_min = rate_setting = target = direction = None
    # Each query may answer '?NA' (e.g. the selected phase is not a RATE phase): report None then.
    with contextlib.suppress(InstrumentProtocolError):
        value, units = pump.rate()
        rate_ml_min = value * RATE_UNITS[units]
        rate_setting = f"{value:g} {RATE_UNIT_NAMES[units]}"
    with contextlib.suppress(InstrumentProtocolError):
        vol, vunits = pump.volume()
        target = _vol_ml(vol, vunits)
    with contextlib.suppress(InstrumentProtocolError):
        direction = pump.direction()
    return PumpStatus(
        state=PROMPTS.get(st.prompt or "", "alarm"),
        running=st.prompt in {"I", "W", "X"},
        alarm=alarm,
        diameter_mm=pump.diameter_mm(),
        rate_ml_min=rate_ml_min,
        rate_setting=rate_setting,
        target_volume_ml=target,
        direction=direction,
        dispensed=_dispensed(pump),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def list_syringe_presets(
    contains: Annotated[
        str, Field(description="Only list presets whose name contains this text, e.g. 'BD'")
    ] = "",
) -> list[SyringePreset]:
    """List the syringe inside diameters from the New Era manual's reference table (BD, Monoject,
    Terumo, HSW Norm-Ject, Poulten & Graf glass, stainless steel, SGE, Hamilton), with the NE-1000
    rate range each allows. Use a name with `set_syringe`."""
    out = []
    for name, dia in SYRINGE_PRESETS_MM.items():
        if contains.lower() not in name.lower():
            continue
        lo, hi = ne1000_rate_range_ml_min(dia)
        out.append(
            SyringePreset(
                name=name, diameter_mm=dia, ne1000_min_rate_ul_h=lo * 60_000, ne1000_max_rate_ml_min=hi
            )
        )
    return out


@mcp.tool(**CONTROL)
def set_syringe(
    preset: Annotated[
        str | None, Field(description="Syringe preset name from `list_syringe_presets`, e.g. 'BD 10 mL'")
    ] = None,
    diameter_mm: Annotated[
        float | None, Field(ge=0.1, le=50.0, description="Syringe inside diameter in mm (overrides preset)")
    ] = None,
) -> PumpStatus:
    """Set the syringe inside diameter, either from a preset or a measured value. The pump must be
    stopped. Changing the diameter also resets the dispensed-volume accumulators and may switch the
    pump's volume units (µL below 14.0 mm, mL above)."""
    if diameter_mm is None:
        if not preset:
            raise ValueError("Give either `preset` or `diameter_mm`.")
        _, diameter_mm = find_syringe_preset(preset)
    server.driver.set_diameter(diameter_mm)
    return get_status()


def _start(direction: str, volume_ml: float, rate_ml_min: float) -> DispenseStarted:
    server.check("max_volume_ml", volume_ml, "volume")
    server.check("max_rate_ml_min", rate_ml_min, "pumping rate")
    pump = server.driver
    plan = pump.start_pumping(direction, volume_ml, rate_ml_min)
    st = pump.status()
    note = "Pumping started; the pump stops by itself after the programmed volume."
    if plan.reset_alarm_cleared:
        note += " (A power-on reset alarm was acknowledged first.)"
    return DispenseStarted(
        direction="infuse" if direction == "INF" else "withdraw",
        volume_ml=plan.volume_ml,
        rate_ml_min=plan.rate_ml_min,
        pump_commands=f"RAT {plan.rate_text}, VOL {plan.volume_text}, DIR {direction}",
        diameter_mm=plan.diameter_mm,
        estimated_duration_s=plan.volume_ml / plan.rate_ml_min * 60.0,
        state=PROMPTS.get(st.prompt or "", "alarm"),
        note=note,
        timestamp=_now(),
    )


@mcp.tool(**HAZARD)
def infuse(
    volume_ml: Annotated[float, Field(gt=0, le=1000, description="Volume to infuse, in mL")],
    rate_ml_min: Annotated[float, Field(gt=0, le=1000, description="Infusion rate, in mL/min")],
) -> DispenseStarted:
    """Push `volume_ml` out of the syringe at `rate_ml_min`, then stop. Returns as soon as pumping
    has started, with the estimated duration. Requires the correct syringe diameter (`set_syringe`)
    and a stopped pump. Overwrites phases 1-2 of the pump's stored Pumping Program."""
    return _start("INF", volume_ml, rate_ml_min)


@mcp.tool(**HAZARD)
def withdraw(
    volume_ml: Annotated[float, Field(gt=0, le=1000, description="Volume to withdraw, in mL")],
    rate_ml_min: Annotated[float, Field(gt=0, le=1000, description="Withdrawal rate, in mL/min")],
) -> DispenseStarted:
    """Pull `volume_ml` into the syringe at `rate_ml_min`, then stop. Returns as soon as pumping has
    started. Make sure the syringe has room for the volume. Overwrites phases 1-2 of the pump's
    stored Pumping Program."""
    return _start("WDR", volume_ml, rate_ml_min)


@mcp.tool(**READ)
def get_dispensed_volume() -> DispensedVolume:
    """Return the accumulated infused and withdrawn volumes (pump command DIS)."""
    return _dispensed(server.driver)


@mcp.tool(**CONTROL)
def clear_dispensed_volume() -> DispensedVolume:
    """Reset the infused and withdrawn volume accumulators to zero. Only possible while stopped."""
    server.driver.clear_dispensed()
    return _dispensed(server.driver)


@mcp.tool(**SAFETY)
def stop_pump() -> PumpStatus:
    """Stop the pump immediately (STP). A paused program is also cancelled so it cannot resume."""
    server.driver.stop()
    return get_status()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
