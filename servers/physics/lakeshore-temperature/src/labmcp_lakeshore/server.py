"""MCP server for Lake Shore Cryotronics Model 335 / 336 / 350 temperature controllers."""

from __future__ import annotations

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
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    parse_address,
)
from pydantic import BaseModel, Field

from labmcp_lakeshore.driver import MODELS, LakeShoreController
from labmcp_lakeshore.simulator import LakeShoreSimulator


def connect(ctx: ConnectContext) -> LakeShoreController:
    addr = parse_address(ctx.address) if ctx.address and not ctx.simulate else None
    sim_model = ctx.option("sim_model", "336") or "336"
    if sim_model not in MODELS:
        raise InstrumentProtocolError(f"sim_model must be one of {', '.join(MODELS)}")
    speed = float(ctx.option("sim_speed", "1") or 1)
    # USB is a virtual COM port fixed at 57600 baud, 7 data bits, odd parity, 1 stop bit
    # (336 manual table 6-5). Replies end in CR LF; VISA/GPIB reads end on EOI/LF.
    transport = ctx.open_transport(
        simulator=lambda: LakeShoreSimulator(model=sim_model, speed=speed),
        baudrate=57600,
        bytesize=7,
        parity="O",
        stopbits=1,
        read_termination="\n" if addr is not None and addr.kind == "visa" else "\r\n",
        write_termination="\r\n",
        timeout=3.0,
    )
    interval = float(ctx.option("min_interval_s", "0" if ctx.simulate else "0.05") or 0)
    return LakeShoreController(transport, min_interval_s=interval)


server = InstrumentServer(
    "Lake Shore Temperature Controller (335 / 336 / 350)",
    connect=connect,
    package="labmcp-lakeshore",
    instructions="""
Controls a Lake Shore Model 335, 336 or 350 cryogenic temperature controller.
- Call `read_temperatures` and `get_heater_status` first to see which inputs are enabled, which
  input each output controls, and whether any heater is on.
- An output only heats when its heater range is not off: `set_setpoint` alone does not start
  heating, `set_heater_range` does. Output modes and control inputs are set on the front panel.
- Use the lowest heater range that can reach the setpoint; setpoints are refused above
  `max_setpoint_k`, heater ranges above `max_heater_range`.
- Setpoints are given in kelvin; the server converts if the control input's preferred units are
  Celsius and refuses if they are sensor units.
- Use `wait_for_stable_temperature` before measurements, and `all_heaters_off` when finished or if
  anything looks wrong (a temperature rising without control, a sensor error, an open heater).
""",
    limits=[
        Limit("max_setpoint_k", 325, "K", "Highest control setpoint an agent may set"),
        Limit("max_heater_range", 3, "", "Highest heater range index (335/336: 3 = high; 350: 1-5)"),
        Limit("max_wait_s", 3600, "s", "Longest wait_for_stable_temperature call"),
    ],
    address_help="""\
  serial:///dev/ttyACM0            USB (virtual COM port, 57600 baud 7O1 set automatically)
  serial://COM5                    Windows USB
  tcp://192.168.1.40:7777          Ethernet (336 / 350; TCP port 7777)
  GPIB0::12::INSTR                 IEEE-488 via VISA""",
    option_help={
        "min_interval_s": "minimum time between messages (default 0.05 s, the manual's 50 ms)",
        "sim_model": "model simulated with --simulate: 335, 336 (default) or 350",
        "sim_speed": "simulated time per real second with --simulate (default 1)",
    },
)
mcp = server.mcp

InputName = Literal["A", "B", "C", "D"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class InputTemperature(BaseModel):
    input: str
    name: str = Field(description="Sensor input name set on the controller")
    sensor_type: str
    temperature_k: float | None = Field(description="Kelvin reading; null if the reading is not valid")
    sensor_value: float | None = Field(description="Raw sensor reading in sensor_unit")
    sensor_unit: str | None = Field(description="V (diode), ohm (RTD), mV (thermocouple) or nF")
    status: list[str] = Field(description="Reading status flags from RDGST?; empty means OK")
    preferred_units: str = Field(description="Units the controller displays and uses for setpoints")


class TemperatureReadings(BaseModel):
    readings: list[InputTemperature]
    timestamp: str


class OutputStatus(BaseModel):
    output: int
    kind: Literal["heater", "analog"] = Field(description="heater = powered output; analog = 10 V output")
    mode: str = Field(description="off, closed_loop_pid, zone, open_loop, monitor_out, warmup_supply ...")
    control_input: str | None
    heater_range: int
    heater_range_name: str
    output_percent: float = Field(description="Heater or analog output in percent of full scale")
    setpoint: float
    setpoint_units: str
    setpoint_k: float | None = Field(description="Setpoint converted to kelvin (null for sensor units)")
    ramp_enabled: bool | None = None
    ramp_rate_k_per_min: float | None = None
    ramping: bool | None = None
    pid_p: float | None = None
    pid_i: float | None = None
    pid_d: float | None = None
    heater_error: str | None = Field(default=None, description="HTRST? error (cleared by reading)")
    power_up_enabled: bool


class StabilityResult(BaseModel):
    stable: bool
    output: int
    control_input: str
    setpoint_k: float
    final_temperature_k: float
    elapsed_s: float
    tolerance_k: float
    stable_for_s: float
    window_mean_k: float | None = Field(description="Mean of the readings in the final stable window")
    window_stdev_k: float | None
    aborted: bool = Field(description="True if all_heaters_off stopped the wait")
    trace: list[tuple[float, float]] = Field(description="(seconds since start, K), downsampled")
    message: str


def _to_kelvin(value: float, units: str) -> float | None:
    if units == "kelvin":
        return value
    if units == "celsius":
        return value + 273.15
    return None


def _output_status(ls: LakeShoreController, out: int) -> OutputStatus:
    with ls.t.lock:
        mode, inp, powerup = ls.output_mode(out)
        analog = ls.is_analog(out)
        rng = ls.heater_range(out)
        names = ls.range_names(out)
        units = ls.setpoint_units(out)
        sp = ls.setpoint(out)
        status = OutputStatus(
            output=out,
            kind="analog" if analog else "heater",
            mode=mode,
            control_input=inp,
            heater_range=rng,
            heater_range_name=names[rng] if rng < len(names) else f"range {rng}",
            output_percent=ls.output_percent(out),
            setpoint=sp,
            setpoint_units=units,
            setpoint_k=_to_kelvin(sp, units),
            heater_error=ls.heater_error(out),
            power_up_enabled=powerup,
        )
        if out in ls.spec.loop_outputs:
            status.ramp_enabled, status.ramp_rate_k_per_min = ls.ramp(out)
            status.ramping = ls.ramping(out)
            status.pid_p, status.pid_i, status.pid_d = ls.pid(out)
    return status


@mcp.tool(**READ)
def read_temperatures(
    inputs: Annotated[
        list[InputName] | None, Field(description="Inputs to read (default: all inputs of the model)")
    ] = None,
) -> TemperatureReadings:
    """Read every (or the selected) sensor input: kelvin, raw sensor units, sensor type, input
    name and decoded reading status (invalid, under/overrange). Disabled inputs are listed with
    null readings."""
    ls = server.driver
    names = inputs or list(ls.spec.inputs)
    readings = []
    for name in names:
        r = ls.read_input(name)
        readings.append(
            InputTemperature(
                input=r.input,
                name=r.name,
                sensor_type=r.sensor_type,
                temperature_k=r.kelvin,
                sensor_value=r.sensor_value,
                sensor_unit=r.sensor_unit,
                status=r.status,
                preferred_units=r.preferred_units,
            )
        )
    return TemperatureReadings(readings=readings, timestamp=_now())


@mcp.tool(**READ)
def get_heater_status(
    output: Annotated[int | None, Field(ge=1, le=4, description="One output (default: all)")] = None,
) -> list[OutputStatus]:
    """Report each output's control mode and input, heater range, output %, setpoint (and in
    kelvin), ramp state, PID values and heater errors (open/short). Reading a heater error clears
    it on the controller."""
    ls = server.driver
    outs = [output] if output is not None else list(ls.spec.outputs)
    return [_output_status(ls, o) for o in outs]


@mcp.tool(**HAZARD)
def set_setpoint(
    output: Annotated[int, Field(ge=1, le=4, description="Output / control loop number")],
    setpoint_k: Annotated[float, Field(gt=0, le=1500, description="Target temperature in kelvin")],
) -> OutputStatus:
    """Set the control setpoint of an output's loop in kelvin. If setpoint ramping is on, the
    setpoint moves toward the new value at the ramp rate. Heating only happens if the output is in
    closed-loop mode and its heater range is not off. Refused above `max_setpoint_k`."""
    server.check("max_setpoint_k", setpoint_k, f"setpoint for output {output}")
    ls = server.driver
    with ls.t.lock:
        mode, inp, _ = ls.output_mode(output)
        if inp is None:
            raise InstrumentProtocolError(
                f"Output {output} has no control input assigned (mode {mode}); configure it on the front panel."
            )
        units = ls.setpoint_units(output)
        if units == "sensor":
            raise InstrumentProtocolError(
                f"Input {inp} uses sensor units as preferred units, so the setpoint of output {output} is in "
                "sensor units, not kelvin. Change the input's preferred units to kelvin on the controller."
            )
        ls.set_setpoint(output, setpoint_k - 273.15 if units == "celsius" else setpoint_k)
        return _output_status(ls, output)


@mcp.tool(**CONTROL)
def set_ramp(
    output: Annotated[int, Field(ge=1, le=4, description="Output / control loop number")],
    enabled: Annotated[bool, Field(description="Ramp the setpoint instead of stepping it")],
    rate_k_per_min: Annotated[
        float, Field(ge=0, le=100, description="Ramp rate in K/min (0.1-100; 350: 0.001-100)")
    ] = 1.0,
) -> OutputStatus:
    """Turn setpoint ramping on or off for an output's control loop and set the rate. With ramping
    on, the next setpoint change moves the setpoint gradually - gentler on samples and wiring."""
    ls = server.driver
    if enabled and not ls.spec.min_ramp_k_min <= rate_k_per_min <= 100:
        raise InstrumentProtocolError(
            f"The Model {ls.spec.model} ramp rate range is {ls.spec.min_ramp_k_min:g}-100 K/min."
        )
    ls.set_ramp(output, enabled, rate_k_per_min)
    return _output_status(ls, output)


@mcp.tool(**HAZARD)
def set_heater_range(
    output: Annotated[int, Field(ge=1, le=4, description="Output number")],
    heater_range: Annotated[
        int,
        Field(
            ge=0, le=5, description="0 = off; 335/336: 1 low, 2 medium, 3 high; 350: 1-5; outputs 3/4: 1 = on"
        ),
    ],
) -> OutputStatus:
    """Set an output's heater range (each step is ~10x more power). Anything above 0 lets the
    output heat: in closed loop as the PID demands, in open loop at the front-panel manual output.
    Refused above `max_heater_range`. Start with the lowest range that can reach the setpoint."""
    server.check("max_heater_range", heater_range, f"heater range for output {output}")
    ls = server.driver
    with ls.t.lock:
        ls.set_heater_range(output, heater_range)
        return _output_status(ls, output)


@mcp.tool(**CONTROL)
def set_pid(
    output: Annotated[int, Field(ge=1, le=4, description="Output / control loop number")],
    p: Annotated[float, Field(ge=0.1, le=1000, description="Proportional gain")],
    i: Annotated[
        float, Field(ge=0.1, le=1000, description="Integral (reset) setting = 1000 / integral seconds")
    ],
    d: Annotated[float, Field(ge=0, le=200, description="Derivative (rate) setting, 0-200")],
) -> OutputStatus:
    """Set the P, I and D values of an output's control loop (Lake Shore conventions)."""
    ls = server.driver
    ls.set_pid(output, p, i, d)
    return _output_status(ls, output)


@mcp.tool(**READ, timeout=7300)
def wait_for_stable_temperature(
    output: Annotated[int, Field(ge=1, le=4, description="Control loop whose setpoint and input are used")],
    tolerance_k: Annotated[float, Field(gt=0, le=50, description="Allowed |T - setpoint|")] = 0.1,
    stable_for_s: Annotated[
        float, Field(ge=0, le=3600, description="How long T must stay within tolerance")
    ] = 60,
    timeout_s: Annotated[float, Field(gt=0, le=7200, description="Give up after this long")] = 1800,
    poll_interval_s: Annotated[float, Field(ge=0.1, le=60, description="Seconds between readings")] = 2.0,
) -> StabilityResult:
    """Wait until the control input of `output` has stayed within `tolerance_k` of the setpoint
    (and the setpoint is no longer ramping) for `stable_for_s`, or until `timeout_s`. Returns
    whether it stabilised, the final temperature and a downsampled trace. Changes nothing."""
    server.check("max_wait_s", timeout_s, "wait duration")
    ls = server.driver
    with ls.t.lock:
        _, inp, _ = ls.output_mode(output)
        if inp is None:
            raise InstrumentProtocolError(f"Output {output} has no control input assigned.")
        units = ls.setpoint_units(output)
    if units == "sensor":
        raise InstrumentProtocolError(
            f"The setpoint of output {output} is in sensor units; cannot compare in K."
        )
    loop = output in ls.spec.loop_outputs
    ls.abort.clear()
    t0 = time.monotonic()
    trace: list[tuple[float, float]] = []
    window: list[float] = []
    stable_since: float | None = None
    stable = aborted = False
    setpoint_k = temp = float("nan")
    while True:
        now = time.monotonic() - t0
        with ls.t.lock:
            converted = _to_kelvin(ls.setpoint(output), units)
            setpoint_k = float("nan") if converted is None else converted
            temp = ls.kelvin(inp)
            ramping = ls.ramping(output) if loop else False
        trace.append((round(now, 2), temp))
        if abs(temp - setpoint_k) <= tolerance_k and not ramping:
            if stable_since is None:
                stable_since, window = now, []
            window.append(temp)
            if now - stable_since >= stable_for_s:
                stable = True
                break
        else:
            stable_since, window = None, []
        if now >= timeout_s:
            break
        if ls.abort.wait(min(poll_interval_s, max(timeout_s - now, 0.0))):
            aborted = True
            break
    step = max(1, len(trace) // 100)
    elapsed = time.monotonic() - t0
    if stable:
        msg = f"Input {inp} stable at {temp:.4f} K (setpoint {setpoint_k:.4f} K) for {stable_for_s:g} s."
    elif aborted:
        msg = "Wait aborted by all_heaters_off."
    else:
        msg = f"Not stable after {elapsed:.0f} s: input {inp} at {temp:.4f} K, setpoint {setpoint_k:.4f} K."
    return StabilityResult(
        stable=stable,
        output=output,
        control_input=inp,
        setpoint_k=setpoint_k,
        final_temperature_k=temp,
        elapsed_s=elapsed,
        tolerance_k=tolerance_k,
        stable_for_s=stable_for_s,
        window_mean_k=statistics.fmean(window) if window else None,
        window_stdev_k=statistics.stdev(window) if len(window) > 1 else None,
        aborted=aborted,
        trace=trace[::step],
        message=msg,
    )


@mcp.tool(**SAFETY)
def all_heaters_off() -> dict[str, object]:
    """Turn every output off (heater range 0 on outputs 1-4), like the front-panel All Off key,
    and stop any running wait. Reports the read-back range of each output."""
    result = server.driver.all_heaters_off()
    return {"outputs": {str(k): v for k, v in result.items()}, "timestamp": _now()}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
