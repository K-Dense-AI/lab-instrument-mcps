"""Wire-level simulator of a Lake Shore 335 / 336 / 350 on a small cryostat.

Thermal model (two nodes, explicit time stepping):

* **stage** (inputs A and C): heat capacity 5 J/K, linked to a 4.2 K cold head by 0.05 W/K
  (time constant 100 s), heated by output 1;
* **sample** (input B): 1 J/K, linked to the stage by 0.05 W/K (20 s), heated by output 2.

Heater power is the output percentage times the range's full power (High = 25 W, decade steps
below). Closed-loop outputs run the Lake Shore PID law, Output = P [e + (I/1000) ∫e dt + D de/dt]
(336 manual section 2.7), with setpoint ramping. Sensor units follow rough DT-670 diode,
Pt-100 and Cernox-like curves. Simulated time can run faster than real time (``speed``).

Unknown commands set the CME bit (32) and out-of-range values the EXE bit (16) in ``*ESR?``.
"""

from __future__ import annotations

import bisect
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from labmcp import LineSimulator

from labmcp_lakeshore.driver import MODELS

BATH_K = 4.2
C_STAGE, G_STAGE = 5.0, 0.05
C_SAMPLE, G_SAMPLE = 1.0, 0.05
FULL_POWER_W = 25.0
DT_S = 0.05

# Rough DT-670 silicon diode curve (K, V) - for simulation only.
_DIODE = [
    (1.4, 1.664), (4.2, 1.578), (10, 1.420), (20, 1.214), (30, 1.108), (50, 1.071),
    (77.4, 1.024), (100, 0.975), (150, 0.872), (200, 0.762), (250, 0.650), (300, 0.560), (400, 0.340),
    (500, 0.090),
]  # fmt: skip


def diode_volts(t: float) -> float:
    ts = [p[0] for p in _DIODE]
    i = min(max(bisect.bisect_left(ts, t), 1), len(_DIODE) - 1)
    (t0, v0), (t1, v1) = _DIODE[i - 1], _DIODE[i]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


@dataclass
class _Input:
    sensor_type: int  # 0 disabled, 1 diode, 2 PTC RTD, 3 NTC RTD
    units: int  # 1 K, 2 °C, 3 sensor
    name: str
    node: str  # "stage" | "sample"


@dataclass
class _Output:
    mode: int
    control_input: int  # 0 none, 1..4 = A..D
    powerup: int = 0
    range: int = 0
    setpoint_k: float = BATH_K
    ramp_on: int = 0
    ramp_rate: float = 10.0
    ramp_sp_k: float = BATH_K  # present (ramping) setpoint
    p: float = 50.0
    i: float = 20.0
    d: float = 0.0
    integral: float = 0.0
    last_error: float | None = None
    percent: float = 0.0
    htr_type: int = 0  # 335 output 2: 0 current, 1 voltage


class LakeShoreSimulator(LineSimulator):
    def __init__(
        self,
        model: str = "336",
        seed: int | None = 0,
        clock: Callable[[], float] = time.monotonic,
        speed: float = 1.0,
    ) -> None:
        self.spec = MODELS[model]
        self.rng = random.Random(seed)
        self.clock = clock
        self.speed = speed
        self._t = clock()
        self.esr = 0
        self.temps = {"stage": BATH_K, "sample": BATH_K}  # cold, at base temperature
        self.inputs = {
            "A": _Input(1, 1, "Stage", "stage"),
            "B": _Input(2, 1, "Sample", "sample"),
            "C": _Input(3, 1, "Cold head", "stage"),
            "D": _Input(0, 1, "", "stage"),
        }
        self.inputs = {k: v for k, v in self.inputs.items() if k in self.spec.inputs}
        self.outputs = {out: _Output(mode=0, control_input=0) for out in self.spec.outputs}
        self.outputs[1] = _Output(mode=1, control_input=1)  # closed loop on input A, heater off
        self.outputs[2] = _Output(mode=1, control_input=2)  # closed loop on input B, heater off

    # ------------------------------------------------------------ physics

    def _range_power(self, out: int, rng: int) -> float:
        if rng == 0:
            return 0.0
        names = self.spec.heater_range_names
        return FULL_POWER_W * 10.0 ** (rng - (len(names) - 1))

    def _control_temp(self, o: _Output) -> float | None:
        letter = {1: "A", 2: "B", 3: "C", 4: "D"}.get(o.control_input)
        inp = self.inputs.get(letter) if letter else None
        if inp is None or inp.sensor_type == 0:
            return None
        return self.temps[inp.node]

    def advance(self) -> None:
        now = self.clock()
        total = max(now - self._t, 0.0) * self.speed
        self._t = now
        steps = min(int(math.ceil(total / DT_S)), 400_000)
        dt = total / steps if steps else 0.0
        for _ in range(steps):
            self._step(dt)

    def _step(self, dt: float) -> None:
        power = {"stage": 0.0, "sample": 0.0}
        for out, o in self.outputs.items():
            if o.ramp_on and o.ramp_rate > 0:
                step = o.ramp_rate / 60.0 * dt
                delta = o.setpoint_k - o.ramp_sp_k
                o.ramp_sp_k += max(-step, min(step, delta))
            else:
                o.ramp_sp_k = o.setpoint_k
            t = self._control_temp(o)
            if o.mode != 1 or o.range == 0 or t is None or out not in self.spec.heater_outputs:
                o.percent, o.integral, o.last_error = 0.0, 0.0, None
                continue
            e = o.ramp_sp_k - t
            deriv = 0.0 if o.last_error is None else (e - o.last_error) / dt
            o.last_error = e
            o.integral += o.p * o.i / 1000.0 * e * dt
            o.integral = max(0.0, min(100.0, o.integral))  # anti-windup
            o.percent = max(0.0, min(100.0, o.p * e + o.integral + o.p * o.d * deriv))
            power["stage" if out == 1 else "sample"] += o.percent / 100.0 * self._range_power(out, o.range)
        ts, tm = self.temps["stage"], self.temps["sample"]
        q_link = G_SAMPLE * (tm - ts)
        self.temps["stage"] = ts + (power["stage"] - G_STAGE * (ts - BATH_K) + q_link) / C_STAGE * dt
        self.temps["sample"] = tm + (power["sample"] - q_link) / C_SAMPLE * dt

    # ------------------------------------------------------------ sensors

    def _kelvin(self, inp: _Input) -> float:
        return self.temps[inp.node] + self.rng.gauss(0, 0.001)

    def _status(self, inp: _Input, t: float) -> int:
        if inp.sensor_type == 1:
            return 16 if t < 1.4 else 32 if t > 500 else 0
        if inp.sensor_type == 2:
            return 16 if t < 30 else 32 if t > 800 else 0
        if inp.sensor_type == 3:
            return 32 if t > 420 else 0
        return 0

    def _sensor_units(self, inp: _Input, t: float) -> float:
        if inp.sensor_type == 1:
            return diode_volts(t)
        if inp.sensor_type == 2:
            return max(100.0 * (1 + 3.85e-3 * (t - 273.15)), 3.0)
        if inp.sensor_type == 3:
            return 60.0 * (300.0 / t) ** 0.85
        return 0.0

    # ------------------------------------------------------------ protocol

    def handle(self, command: str) -> str | list[str] | None:
        self.advance()
        replies = [r for r in (self._one(p.strip()) for p in command.split(";")) if r is not None]
        return ";".join(replies) if replies else None

    def _one(self, cmd: str) -> str | None:
        if not cmd:
            return None
        head, _, arg = cmd.partition(" ")
        head = head.upper()
        args = [a.strip() for a in arg.split(",")] if arg.strip() else []
        try:
            return self._dispatch(head, args)
        except _OutOfRange:
            self.esr |= 16
        except (KeyError, IndexError, ValueError):
            self.esr |= 32
        return None

    def _input(self, args: list[str]) -> _Input:
        return self.inputs[args[0].upper()]

    def _output(self, args: list[str], loop: bool = False) -> tuple[int, _Output]:
        out = int(args[0])
        if out not in (self.spec.loop_outputs if loop else self.spec.outputs):
            raise KeyError(out)
        return out, self.outputs[out]

    def _units_of(self, o: _Output) -> int:
        letter = {1: "A", 2: "B", 3: "C", 4: "D"}.get(o.control_input)
        return self.inputs[letter].units if letter in self.inputs else 1

    def _dispatch(self, head: str, args: list[str]) -> str | None:
        model = self.spec.model
        if head == "*IDN?":
            return f"LSCI,MODEL{model},LSA{model}1/LSO{model}2,2.9"
        if head == "*CLS":
            self.esr = 0
            return None
        if head == "*ESR?":
            value, self.esr = self.esr, 0
            return f"{value:03d}"
        if head == "*OPC?":
            return "1"
        if head in {"KRDG?", "CRDG?", "SRDG?", "RDGST?"}:
            inp = self._input(args)
            if inp.sensor_type == 0:
                return "000" if head == "RDGST?" else "+0.0000"
            t = self._kelvin(inp)
            status = self._status(inp, t)
            if head == "RDGST?":
                return f"{status:03d}"
            if head == "SRDG?":
                return f"{self._sensor_units(inp, t):+.5f}"
            if status:
                return "+0.0000"
            return f"{t:+.4f}" if head == "KRDG?" else f"{t - 273.15:+.4f}"
        if head == "INTYPE?":
            inp = self._input(args)
            fields = [inp.sensor_type, 1 if inp.sensor_type in (2, 3) else 0, 0, 0, inp.units]
            if model == "350":
                fields.append(1)
            return ",".join(str(f) for f in fields)
        if head == "INNAME?":
            return self._input(args).name.ljust(15)
        if head == "OUTMODE?":
            _, o = self._output(args)
            return f"{o.mode},{o.control_input},{o.powerup}"
        if head == "RANGE?":
            _, o = self._output(args)
            return str(o.range)
        if head == "RANGE":
            out, o = self._output(args)
            rng = int(args[1])
            analog = out not in self.spec.heater_outputs or (model == "335" and out == 2 and o.htr_type == 1)
            if not 0 <= rng <= (1 if analog else len(self.spec.heater_range_names) - 1):
                raise _OutOfRange()
            o.range = rng
            return None
        if head == "HTR?":
            out = int(args[0])
            if out not in self.spec.heater_outputs:
                raise KeyError(out)
            return f"{self.outputs[out].percent:+.1f}"
        if head == "AOUT?":
            out = int(args[0])
            if out in self.spec.heater_outputs:
                raise KeyError(out)
            return f"{self.outputs[out].percent:+.1f}"
        if head == "HTRST?":
            out = int(args[0])
            if out not in self.spec.heater_outputs:
                raise KeyError(out)
            return "0"
        if head == "HTRSET?" and model == "335":
            out, o = self._output(args)
            return f"{o.htr_type if out == 2 else 0},1,2,+0.000,1"
        if head == "SETP?":
            _, o = self._output(args)
            units = self._units_of(o)
            return f"{o.setpoint_k - 273.15 if units == 2 else o.setpoint_k:+.4f}"
        if head == "SETP":
            _, o = self._output(args)
            value = float(args[1])
            units = self._units_of(o)
            k = value + 273.15 if units == 2 else value
            if not 0 <= k <= 2000:
                raise _OutOfRange()
            if not o.ramp_on:
                o.ramp_sp_k = k
            o.setpoint_k = k
            return None
        if head == "RAMP?":
            _, o = self._output(args, loop=True)
            return f"{o.ramp_on},{o.ramp_rate:g}"
        if head == "RAMP":
            _, o = self._output(args, loop=True)
            on, rate = int(args[1]), float(args[2])
            if on not in (0, 1) or not (rate == 0 or self.spec.min_ramp_k_min <= rate <= 100):
                raise _OutOfRange()
            # The ramp starts from the present setpoint when the setpoint is next changed.
            o.ramp_on, o.ramp_rate = on, rate
            return None
        if head == "RAMPST?":
            _, o = self._output(args, loop=True)
            return "1" if o.ramp_on and abs(o.ramp_sp_k - o.setpoint_k) > 1e-6 else "0"
        if head == "PID?":
            _, o = self._output(args, loop=True)
            return f"{o.p:+.1f},{o.i:+.1f},{o.d:+.0f}"
        if head == "PID":
            _, o = self._output(args, loop=True)
            p, i, d = (float(a) for a in args[1:4])
            if not (0.1 <= p <= 1000 and 0.1 <= i <= 1000 and 0 <= d <= 200):
                raise _OutOfRange()
            o.p, o.i, o.d = p, i, d
            return None
        raise KeyError(head)


class _OutOfRange(Exception):
    pass
