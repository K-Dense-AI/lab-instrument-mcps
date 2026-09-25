"""MCP server for Tecan Cavro syringe pumps (XLP 6000, XMP 6000, XCalibur) using the DT protocol."""

from __future__ import annotations

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

from labmcp_cavro.driver import (
    ADDRESSES,
    ERROR_CODES,
    FORCE_CODES,
    MODELS,
    SPEED_PULSES_PER_STROKE,
    TOP_SPEED_MAX,
    TOP_SPEED_MIN,
    CavroPump,
)
from labmcp_cavro.simulator import CavroSimulator


def connect(ctx: ConnectContext) -> CavroPump:
    syringe = ctx.option("syringe_ul") or ("1000" if ctx.simulate else None)
    model = ctx.option("model") or ("xlp6000" if ctx.simulate else None)
    steps = ctx.option("steps_per_stroke")
    if not syringe:
        raise InstrumentConnectionError(
            "Tell the server which syringe is installed, e.g. `--option syringe_ul=1000` for a 1 mL "
            "syringe. Volumes are converted to plunger steps with this value."
        )
    if not model and not steps:
        raise InstrumentConnectionError(
            "Tell the server which pump this is: `--option model=xlp6000`, `xmp6000` or `xcalibur` "
            "(or `--option steps_per_stroke=<increments per full stroke in standard mode>`)."
        )
    if model and model not in MODELS and not steps:
        raise InstrumentConnectionError(f"Unknown model {model!r}; choose one of {', '.join(MODELS)}.")
    syringe_ul = float(syringe)
    if not 10 <= syringe_ul <= 50_000:
        raise InstrumentConnectionError(f"syringe_ul={syringe_ul:g} looks wrong (expected 10-50000 µL).")
    resolution = ctx.option("resolution", "standard")
    if resolution not in {"standard", "fine"}:
        raise InstrumentConnectionError("--option resolution must be 'standard' (N0) or 'fine' (N1)")
    address = ctx.option("pump_address", "1") or "1"
    standard_steps = int(steps) if steps else MODELS[str(model)][1]
    speed = float(ctx.option("sim_speed", "1") or 1)
    # DT protocol defaults (XLP 6000 manual table 3-4): 9600 baud 8N1, CR-terminated commands,
    # answers end with ETX CR LF.
    transport = ctx.open_transport(
        simulator=lambda: CavroSimulator(address=address, standard_steps=standard_steps, speed=speed),
        baudrate=9600,
        read_termination="\x03\r\n",
        write_termination="\r",
        timeout=2.0,
    )
    return CavroPump(
        transport,
        address=address,
        syringe_ul=syringe_ul,
        model=model or "custom",
        standard_steps=standard_steps,
        resolution_mode=0 if resolution == "standard" else 1,
    )


server = InstrumentServer(
    "Tecan Cavro Syringe Pump (DT protocol)",
    connect=connect,
    package="labmcp-cavro",
    instructions="""
Controls a Tecan Cavro OEM syringe pump with a valve (XLP 6000, XMP 6000, XCalibur) over the
Data Terminal (DT) protocol.
- After power-up, a plunger overload or `terminate`, call `initialize` before any move. It drives
  the plunger to the top of the syringe and homes the valve, pushing out whatever is in the syringe.
- Volumes are converted with the configured syringe size and pump resolution shown by
  `get_connection_info`/`get_status`. If they do not match the installed syringe, stop and ask.
- `aspirate_ul` pulls liquid into the syringe through the current valve port, `dispense_ul` pushes
  it out. Choose the valve port first (`set_valve`, or the `valve`/`port` arguments).
- Never run the pump dry for more than a few cycles, and never move against a closed line: a
  plunger overload (error 9) means blocked tubing or too much back-pressure.
- `terminate` stops plunger moves immediately (a valve move in progress still completes).
""",
    limits=[
        Limit("max_volume_ul", 5000.0, "µL", "Largest volume a single aspirate/dispense may move"),
        Limit("max_flow_ul_s", 500.0, "µL/s", "Highest plunger flow rate an agent may use"),
    ],
    address_help="""\
  serial:///dev/ttyUSB0              RS-232 (or RS-485 adapter), DT protocol at 9600 baud 8N1
  serial://COM3?baudrate=38400       pump configured for 38400 baud (U47 command)
  tcp://192.168.1.80:4001            serial-to-Ethernet adapter (raw TCP)""",
    option_help={
        "syringe_ul": "installed syringe volume in µL, e.g. 1000 (required)",
        "model": "xlp6000 or xmp6000 (6000 steps/stroke) or xcalibur (3000); required unless steps_per_stroke",
        "steps_per_stroke": "override: plunger increments per full stroke in standard (N0) mode",
        "resolution": "standard (N0, default) or fine (N1, 8x finer positions); applied by `initialize`",
        "pump_address": f"DT address character, one of {ADDRESSES} (address switch 0 = '1', default)",
        "sim_speed": "simulator only: time acceleration factor (default 1)",
    },
)
mcp = server.mcp

ValvePosition = Literal["input", "output", "bypass", "extra"]
_VALVE_CODES = {"input": "I", "output": "O", "bypass": "B", "extra": "E"}
_VALVE_NAMES = {"i": "input", "o": "output", "b": "bypass", "e": "extra"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _valve_code(position: str | None, port: int | None, direction: str) -> str | None:
    if position and port:
        raise ValueError("Give either a named valve `position` or a distribution-valve `port`, not both.")
    if position:
        return _VALVE_CODES[position]
    if port:
        return f"{'I' if direction == 'cw' else 'O'}{port}"
    return None


def _valve_name(raw: str) -> str:
    return _VALVE_NAMES.get(raw, f"port {raw}" if raw.isdigit() else raw)


class PumpStatus(BaseModel):
    ready: bool = Field(description="True when the pump accepts new move commands (Q status bit 5)")
    error_code: int = Field(description="Error code from the status byte (0 = none)")
    error: str = Field(description="Meaning of the error code")
    plunger_position_ul: float = Field(description="Volume currently drawn into the syringe")
    plunger_position_steps: int
    steps_per_stroke: int = Field(description="Plunger increments for a full stroke in the current mode")
    syringe_ul: float
    resolution_mode: int = Field(
        description="0 = standard (N0), 1 = fine positioning (N1), 2 = microstep (N2)"
    )
    valve_position: str
    top_speed_pulses_s: int
    top_speed_flow_ul_s: float = Field(description="Flow rate the current top speed corresponds to")
    timestamp: str


class MoveResult(BaseModel):
    action: Literal["aspirate", "dispense"]
    requested_ul: float
    moved_ul: float = Field(description="Volume actually commanded after rounding to whole plunger steps")
    steps: int
    flow_ul_s: float = Field(
        description="Flow at the commanded top speed (ramps make the average slightly lower)"
    )
    top_speed_pulses_s: int
    duration_s: float
    terminated: bool = Field(description="True if `terminate` interrupted the move")
    plunger_position_ul: float
    valve_position: str
    timestamp: str


def _status(pump: CavroPump) -> PumpStatus:
    q = pump.query_status()
    mode = pump.mode()
    spp = pump.steps_per_stroke(mode)
    steps = pump.plunger_steps()
    top = pump.top_speed()
    pulses = SPEED_PULSES_PER_STROKE if mode in (0, 1) else spp
    return PumpStatus(
        ready=q.ready,
        error_code=q.error,
        error=ERROR_CODES.get(q.error, "unknown error"),
        plunger_position_ul=steps * pump.syringe_ul / spp,
        plunger_position_steps=steps,
        steps_per_stroke=spp,
        syringe_ul=pump.syringe_ul,
        resolution_mode=mode,
        valve_position=_valve_name(pump.valve_position()),
        top_speed_pulses_s=top,
        top_speed_flow_ul_s=top * pump.syringe_ul / pulses,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_status() -> PumpStatus:
    """Report whether the pump is ready or busy, any error (decoded), the plunger position as the
    volume in the syringe, the valve position, resolution mode and current top speed."""
    return _status(server.driver)


@mcp.tool(**HAZARD, timeout=90)
def initialize(
    valve_direction: Annotated[
        Literal["cw", "ccw", "no_valve"],
        Field(
            description="Valve homing/port numbering: cw (Z command), ccw (Y), or no_valve (W, plunger only)"
        ),
    ] = "cw",
    force: Annotated[
        Literal["auto", "full", "half", "third"],
        Field(description="Plunger force against the syringe top; auto picks it from the syringe size"),
    ] = "auto",
    input_port: Annotated[
        int | None, Field(ge=1, le=12, description="Distribution valves: input port")
    ] = None,
    output_port: Annotated[
        int | None, Field(ge=1, le=12, description="Distribution valves: output port")
    ] = None,
) -> PumpStatus:
    """Initialize the pump: drive the plunger to the top of the syringe (expelling its contents
    through the valve), set that as position 0 and home the valve. Needed after power-up, a plunger
    overload or `terminate`. Route the valve output to waste first. Takes a few seconds."""
    pump = server.driver
    kind = {"cw": "Z", "ccw": "Y", "no_valve": "W"}[valve_direction]
    code = None if force == "auto" else FORCE_CODES[force]
    pump.initialize(kind, code, input_port, output_port, timeout=60.0)
    return _status(pump)


@mcp.tool(**HAZARD, timeout=30)
def set_valve(
    position: Annotated[
        ValvePosition | None, Field(description="Non-distribution valves: input, output, bypass or extra")
    ] = None,
    port: Annotated[int | None, Field(ge=1, le=12, description="Distribution valves: port number")] = None,
    direction: Annotated[
        Literal["cw", "ccw"], Field(description="Distribution valves: rotation direction to the port")
    ] = "cw",
) -> PumpStatus:
    """Turn the valve to a named position (3/4-port valves) or to a numbered port (distribution
    valves). In bypass the plunger cannot move. Returns the new status."""
    code = _valve_code(position, port, direction)
    if code is None:
        raise ValueError("Give a valve `position` or a `port`.")
    server.driver.move_valve(code)
    return _status(server.driver)


def _move(action: str, volume_ul: float, flow_ul_s: float, valve: str | None, port: int | None) -> MoveResult:
    server.check("max_volume_ul", volume_ul, "volume")
    server.check("max_flow_ul_s", flow_ul_s, "flow rate")
    pump = server.driver
    syringe = pump.syringe_ul
    mode = pump.mode()
    if mode == 2:
        raise InstrumentProtocolError(
            "The pump is in microstep mode (N2), which this server does not use. Call `initialize` to "
            "restore the configured resolution."
        )
    spp = pump.steps_per_stroke(mode)
    steps = round(volume_ul * spp / syringe)
    if steps <= 0:
        raise ValueError(f"{volume_ul:g} µL is below the pump resolution ({syringe / spp:.3g} µL per step).")
    speed = round(flow_ul_s * SPEED_PULSES_PER_STROKE / syringe)
    lo, hi = (s * syringe / SPEED_PULSES_PER_STROKE for s in (TOP_SPEED_MIN, TOP_SPEED_MAX))
    if not TOP_SPEED_MIN <= speed <= TOP_SPEED_MAX:
        raise ValueError(
            f"A flow of {flow_ul_s:g} µL/s is outside what a {syringe:g} µL syringe allows "
            f"({lo:.3g}-{hi:.4g} µL/s)."
        )
    code = _valve_code(valve, port, "cw")
    if code is not None:
        pump.move_valve(code)
    position = pump.plunger_steps()
    if action == "aspirate" and position + steps > spp:
        raise ValueError(
            f"Aspirating {volume_ul:g} µL would overfill the syringe: it holds {position * syringe / spp:.4g} "
            f"of {syringe:g} µL."
        )
    if action == "dispense" and position - steps < 0:
        raise ValueError(
            f"Only {position * syringe / spp:.4g} µL is in the syringe; cannot dispense {volume_ul:g} µL."
        )
    expected = steps * (SPEED_PULSES_PER_STROKE / spp) / speed
    t0 = time.monotonic()
    result = pump.move_plunger(
        "P" if action == "aspirate" else "D", steps, speed, timeout=expected * 1.5 + 10
    )
    return MoveResult(
        action=action,  # type: ignore[arg-type]
        requested_ul=volume_ul,
        moved_ul=steps * syringe / spp,
        steps=steps,
        flow_ul_s=speed * syringe / SPEED_PULSES_PER_STROKE,
        top_speed_pulses_s=speed,
        duration_s=round(time.monotonic() - t0, 3),
        terminated=result.terminated,
        plunger_position_ul=result.position_steps * syringe / spp,
        valve_position=_valve_name(pump.valve_position()),
        timestamp=_now(),
    )


@mcp.tool(**HAZARD, timeout=1300)
def aspirate_ul(
    volume_ul: Annotated[float, Field(gt=0, le=50_000, description="Volume to draw into the syringe, µL")],
    flow_ul_s: Annotated[float, Field(gt=0, le=50_000, description="Plunger flow rate, µL/s")] = 100.0,
    valve: Annotated[
        ValvePosition | None, Field(description="Turn the valve here first (e.g. 'input')")
    ] = None,
    port: Annotated[
        int | None, Field(ge=1, le=12, description="Distribution valves: port to turn to first")
    ] = None,
) -> MoveResult:
    """Draw `volume_ul` into the syringe at `flow_ul_s` through the current (or given) valve port,
    and wait until the move has finished. The pump must be initialized and have room for the volume."""
    return _move("aspirate", volume_ul, flow_ul_s, valve, port)


@mcp.tool(**HAZARD, timeout=1300)
def dispense_ul(
    volume_ul: Annotated[float, Field(gt=0, le=50_000, description="Volume to push out of the syringe, µL")],
    flow_ul_s: Annotated[float, Field(gt=0, le=50_000, description="Plunger flow rate, µL/s")] = 100.0,
    valve: Annotated[
        ValvePosition | None, Field(description="Turn the valve here first (e.g. 'output')")
    ] = None,
    port: Annotated[
        int | None, Field(ge=1, le=12, description="Distribution valves: port to turn to first")
    ] = None,
) -> MoveResult:
    """Push `volume_ul` out of the syringe at `flow_ul_s` through the current (or given) valve port,
    and wait until the move has finished. The syringe must contain at least that volume."""
    return _move("dispense", volume_ul, flow_ul_s, valve, port)


@mcp.tool(**SAFETY)
def terminate() -> PumpStatus:
    """Stop any plunger move, loop or delay immediately (DT command T). A valve move in progress
    still completes. Re-initialize afterwards: the plunger may have lost steps."""
    pump = server.driver
    pump.terminate()
    deadline = time.monotonic() + 2.0
    status = _status(pump)
    while not status.ready and time.monotonic() < deadline:
        time.sleep(0.1)
        status = _status(pump)
    return status


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
