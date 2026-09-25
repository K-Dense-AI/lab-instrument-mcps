"""Wire-level simulators of Keithley SourceMeters driving a device under test (DUT).

Three simulators share one source-measure model (:class:`SMUCore`):

* :class:`Keithley2400Simulator`: 2400 SCPI (``:READ?`` returns ``VOLT,CURR,STAT`` with the
  compliance bit 3 set in the status word; ``:READ?`` with the output off queues error
  ``+802,"Not permitted with OUTPUT off"`` and sends nothing, like the real instrument).
* :class:`Keithley2450Simulator`: 2450 SCPI (``:READ? "defbuffer1",SOUR,READ``, ``*LANG?``,
  ``...:ILIM:TRIP?``, ``SYST:ERR?`` replies of the form ``0,"No error;0,0,0"``).
* :class:`Keithley2600Simulator`: 2600B TSP (attribute assignments, ``print(...)`` queries,
  ``errorqueue`` with codes from the reference manual's error summary, numbers printed with the
  default ``format.asciiprecision`` of 6 digits).

The DUT is a resistor or a diode (Shockley equation with series resistance, 1N4148-like
parameters). Compliance is honoured the way an SMU does it: when the programmed source would
push the DUT past the limit, the SMU clamps at the limit and the other quantity follows the DUT.
"""

from __future__ import annotations

import math
import random
import re

from labmcp import LineSimulator
from labmcp.scpi import SCPISimulator

THERMAL_VOLTAGE_V = 0.025852  # kT/q at 300 K


class Resistor:
    def __init__(self, resistance_ohm: float = 1000.0) -> None:
        self.r = resistance_ohm

    def current(self, voltage_v: float) -> float:
        return voltage_v / self.r

    def voltage(self, current_a: float) -> float:
        return current_a * self.r


class Diode:
    """Shockley diode with series resistance (defaults: 1N4148 SPICE model IS=2.52n N=1.752 RS=0.568)."""

    def __init__(
        self, i_sat_a: float = 2.52e-9, ideality: float = 1.752, r_series_ohm: float = 0.568
    ) -> None:
        self.i_s = i_sat_a
        self.n_vt = ideality * THERMAL_VOLTAGE_V
        self.rs = r_series_ohm

    def voltage(self, current_a: float) -> float:
        if current_a <= -self.i_s:
            return -math.inf  # the ideal diode cannot conduct more reverse current than I_s
        return self.n_vt * math.log1p(current_a / self.i_s) + current_a * self.rs

    def current(self, voltage_v: float) -> float:
        lo, hi = -self.i_s * (1 - 1e-12), max(abs(voltage_v) / self.rs, 1e-3) + 1.0
        for _ in range(200):  # bisection on the monotonic V(I)
            mid = 0.5 * (lo + hi)
            if self.voltage(mid) < voltage_v:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)


def make_dut(kind: str = "resistor", resistance_ohm: float = 1000.0) -> Resistor | Diode:
    if kind == "resistor":
        return Resistor(resistance_ohm)
    if kind == "diode":
        return Diode()
    raise ValueError(f"Unknown simulated DUT {kind!r}; use 'resistor' or 'diode'.")


class SMUCore:
    """State and physics of one SMU channel."""

    def __init__(self, dut: Resistor | Diode, max_v: float, max_i: float, seed: int | None = 0) -> None:
        self.dut = dut
        self.max_v = max_v
        self.max_i = max_i
        self.rng = random.Random(seed)
        self.reset()

    def reset(self) -> None:
        self.func = "VOLT"
        self.level = {"VOLT": 0.0, "CURR": 0.0}
        self.limit = {"CURR": 105e-6, "VOLT": 21.0}  # limit on the *measured* quantity
        self.source_range: dict[str, float | None] = {"VOLT": None, "CURR": None}  # None = auto
        self.output = False
        self.remote_sense = False
        self.nplc = 1.0

    def operating_point(self) -> tuple[float, float, bool]:
        """(voltage, current, in_compliance) at the terminals, with measurement noise."""
        if not self.output:
            v, i, comp = 0.0, 0.0, False
        elif self.func == "VOLT":
            v = self.level["VOLT"]
            i = self.dut.current(v)
            comp = abs(i) > self.limit["CURR"]
            if comp:
                i = math.copysign(self.limit["CURR"], i)
                v = self.dut.voltage(i)
        else:
            i = self.level["CURR"]
            v = self.dut.voltage(i)
            comp = not math.isfinite(v) or abs(v) > self.limit["VOLT"]
            if comp:
                v = math.copysign(self.limit["VOLT"], v)
                i = self.dut.current(v)
        k = 1.0 / math.sqrt(max(self.nplc, 0.01))
        v += self.rng.gauss(0.0, (2e-6 + 1e-5 * abs(v)) * k)
        i += self.rng.gauss(0.0, (20e-12 + 5e-5 * abs(i)) * k)
        return v, i, comp


# ====================================================================== 2400 SCPI


class _KeithleySCPISimulator(SCPISimulator):
    def __init__(self, core: SMUCore) -> None:
        super().__init__()
        self.core = core
        self.terminals = "FRON"

    def reset(self) -> None:
        self.core.reset()

    def _max(self, func: str) -> float:
        return self.core.max_v if func == "VOLT" else self.core.max_i

    @staticmethod
    def _func(arg: str) -> str:
        a = arg.strip().strip("\"'").upper()
        if a.startswith("VOLT"):
            return "VOLT"
        if a.startswith("CURR"):
            return "CURR"
        raise ValueError(arg)

    @staticmethod
    def _bool(arg: str) -> bool:
        a = arg.strip().upper()
        if a in {"1", "ON"}:
            return True
        if a in {"0", "OFF"}:
            return False
        raise ValueError(arg)

    def _set_level(self, func: str, arg: str) -> None:
        value = float(arg)
        rng = self.core.source_range[func]
        if abs(value) > self._max(func):
            self.error_queue.append('-222,"Data out of range"')
        elif rng is not None and abs(value) > rng * 1.05:
            self.error_queue.append('-221,"Settings conflict"')
        else:
            self.core.level[func] = value

    def _source_commands(self, key: str, arg: str) -> tuple[bool, str | None]:
        """Commands shared by the 2400 and the 2450. Returns (handled, reply)."""
        m, c = self.matches, self.core
        if m(key, "SOURce[1]:FUNCtion[:MODE]"):
            c.func = self._func(arg)
            return True, None
        if m(key, "SOURce[1]:FUNCtion[:MODE]?"):
            return True, c.func
        for func in ("VOLT", "CURR"):
            name = "VOLTage" if func == "VOLT" else "CURRent"
            if m(key, f"SOURce[1]:{name}:RANGe:AUTO"):
                if self._bool(arg):
                    c.source_range[func] = None
                return True, None
            if m(key, f"SOURce[1]:{name}:RANGe:AUTO?"):
                return True, "1" if c.source_range[func] is None else "0"
            if m(key, f"SOURce[1]:{name}:RANGe"):
                value = abs(float(arg))
                if value > self._max(func):
                    self.error_queue.append('-222,"Data out of range"')
                else:
                    c.source_range[func] = value
                return True, None
            if m(key, f"SOURce[1]:{name}[:LEVel][:IMMediate][:AMPLitude]"):
                self._set_level(func, arg)
                return True, None
            if m(key, f"SOURce[1]:{name}[:LEVel][:IMMediate][:AMPLitude]?"):
                return True, f"{c.level[func]:+.6E}"
        if m(key, "OUTPut[1][:STATe]"):
            c.output = self._bool(arg)
            return True, None
        if m(key, "OUTPut[1][:STATe]?"):
            return True, "1" if c.output else "0"
        if m(key, "ROUTe:TERMinals"):
            a = arg.strip().upper()
            if not (a.startswith("FRON") or a.startswith("REAR")):
                raise ValueError(arg)
            c.output = False  # switching terminals turns the output off
            self.terminals = a[:4]
            return True, None
        if m(key, "ROUTe:TERMinals?"):
            return True, self.terminals
        return False, None

    def _set_limit(self, measured: str, arg: str, lo: float) -> None:
        value = abs(float(arg))
        if value < lo or value > self._max(measured) * 1.0001:
            self.error_queue.append('-222,"Data out of range"')
        else:
            self.core.limit[measured] = value

    def _set_nplc(self, arg: str, lo: float, hi: float) -> None:
        value = float(arg)
        if not lo <= value <= hi:
            self.error_queue.append('-222,"Data out of range"')
        else:
            self.core.nplc = value


class Keithley2400Simulator(_KeithleySCPISimulator):
    idn = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,4105123,C32   Oct  4 2010 14:20:11/A02  /S/K"

    def command(self, key: str, arg: str) -> str | None:
        handled, reply = self._source_commands(key, arg)
        if handled:
            return reply
        m, c = self.matches, self.core
        if m(key, "SOURce[1]:VOLTage:MODE") or m(key, "SOURce[1]:CURRent:MODE"):
            if not arg.strip().upper().startswith("FIX"):
                raise ValueError(arg)
            return None
        for func in ("VOLT", "CURR"):
            name = "VOLTage" if func == "VOLT" else "CURRent"
            if m(key, f"[SENSe:]{name}[:DC]:PROTection[:LEVel]"):
                self._set_limit(func, arg, 0.0)
                return None
            if m(key, f"[SENSe:]{name}[:DC]:PROTection[:LEVel]?"):
                return f"{c.limit[func]:+.6E}"
            if m(key, f"[SENSe:]{name}[:DC]:PROTection:TRIPped?"):
                comp = c.output and c.func != func and c.operating_point()[2]
                return "1" if comp else "0"
            if m(key, f"[SENSe:]{name}[:DC]:RANGe:AUTO"):
                self._bool(arg)
                return None
            if m(key, f"[SENSe:]{name}[:DC]:NPLCycles"):
                self._set_nplc(arg, 0.01, 10.0)
                return None
        if m(key, "[SENSe:]FUNCtion:CONCurrent"):
            self._bool(arg)
            return None
        if m(key, "[SENSe:]FUNCtion[:ON]"):
            for item in arg.split(","):
                self._func(item)
            return None
        if m(key, "FORMat:ELEMents[:SENSe]"):
            return None
        if m(key, "ARM[:SEQuence][:LAYer]:COUNt") or m(key, "TRIGger[:SEQuence]:COUNt"):
            if int(float(arg)) < 1:
                raise ValueError(arg)
            return None
        if m(key, "SYSTem:RSENse"):
            c.remote_sense = self._bool(arg)
            return None
        if m(key, "SYSTem:RSENse?"):
            return "1" if c.remote_sense else "0"
        if m(key, "READ?"):
            if not c.output:
                self.error_queue.append('+802,"Not permitted with OUTPUT off"')
                return None
            v, i, comp = c.operating_point()
            stat = (1 << 11) | (1 << 12) | (1 << 14 if c.func == "VOLT" else 1 << 15)
            stat |= 1 << 2 if self.terminals == "FRON" else 0
            stat |= 1 << 3 if comp else 0
            stat |= 1 << 22 if c.remote_sense else 0
            return f"{v:+.6E},{i:+.6E},{float(stat):+.6E}"
        raise self.undefined()


# ====================================================================== 2450 SCPI


class Keithley2450Simulator(_KeithleySCPISimulator):
    idn = "KEITHLEY INSTRUMENTS,MODEL 2450,04096331,1.6.7c"

    def __init__(self, core: SMUCore) -> None:
        super().__init__(core)
        self.measure_func = "CURR"
        self.rsense = {"VOLT": False, "CURR": False}

    def _dispatch(self, cmd: str) -> str | None:
        key = cmd.partition(" ")[0].upper().lstrip(":")
        if key in {"SYST:ERR?", "SYSTEM:ERROR?", "SYST:ERR:NEXT?", "SYSTEM:ERROR:NEXT?"}:
            if not self.error_queue:
                return '0,"No error;0,0,0"'
            code, _, msg = self.error_queue.pop(0).partition(",")
            return f'{code},"{msg.strip(chr(34))};1;2015/05/06 12:57:04.484"'
        if key == "*LANG?":
            return "SCPI"
        return super()._dispatch(cmd)

    def command(self, key: str, arg: str) -> str | None:
        handled, reply = self._source_commands(key, arg)
        if handled:
            return reply
        m, c = self.matches, self.core
        for src, measured, lim, lo in (
            ("VOLTage", "CURR", "ILIMit", 1e-9),
            ("CURRent", "VOLT", "VLIMit", 0.02),
        ):
            if m(key, f"SOURce[1]:{src}:{lim}[:LEVel]"):
                self._set_limit(measured, arg, lo)
                return None
            if m(key, f"SOURce[1]:{src}:{lim}[:LEVel]?"):
                return f"{c.limit[measured]:+.6E}"
            if m(key, f"SOURce[1]:{src}:{lim}[:LEVel]:TRIPped?"):
                comp = c.output and c.func != measured and c.operating_point()[2]
                return "1" if comp else "0"
            if m(key, f"SOURce[1]:{src}:READ:BACK"):
                self._bool(arg)
                return None
        if m(key, "[SENSe:]FUNCtion[:ON]"):
            self.measure_func = self._func(arg)
            return None
        if m(key, "[SENSe:]FUNCtion[:ON]?"):
            return f'"{self.measure_func}:DC"'
        for func in ("VOLT", "CURR"):
            name = "VOLTage" if func == "VOLT" else "CURRent"
            if m(key, f"[SENSe:]{name}[:DC]:RANGe:AUTO"):
                self._bool(arg)
                return None
            if m(key, f"[SENSe:]{name}[:DC]:NPLCycles"):
                self._set_nplc(arg, 0.01, 10.0)
                return None
            if m(key, f"[SENSe:]{name}[:DC]:RSENse"):
                self.rsense[func] = self._bool(arg)
                c.remote_sense = self.rsense["VOLT"] or self.rsense["CURR"]
                return None
            if m(key, f"[SENSe:]{name}[:DC]:RSENse?"):
                return "1" if self.rsense[func] else "0"
        if m(key, "[SENSe:]COUNt"):
            if int(float(arg)) < 1:
                raise ValueError(arg)
            return None
        if m(key, "READ?"):
            parts = [p.strip().upper() for p in arg.split(",")] if arg.strip() else []
            if parts and parts[0].startswith('"'):
                if parts[0].strip('"') != "DEFBUFFER1":
                    self.error_queue.append('-222,"Data out of range"')
                    return None
                parts = parts[1:]
            v, i, _ = c.operating_point()
            src_value = v if c.func == "VOLT" else i
            reading = v if self.measure_func == "VOLT" else i
            values = []
            for p in parts or ["READ"]:
                if p.startswith("SOUR"):
                    values.append(src_value)
                elif p.startswith("READ"):
                    values.append(reading)
                else:
                    raise ValueError(p)
            return ",".join(f"{x:+.6E}" for x in values)
        raise self.undefined()


# ====================================================================== 2600B TSP

_TSP_CONST = {
    "OUTPUT_DCAMPS": 0,
    "OUTPUT_DCVOLTS": 1,
    "OUTPUT_OFF": 0,
    "OUTPUT_ON": 1,
    "OUTPUT_HIGH_Z": 2,
    "AUTORANGE_OFF": 0,
    "AUTORANGE_ON": 1,
    "AUTORANGE_FOLLOW_LIMIT": 2,
    "SENSE_LOCAL": 0,
    "SENSE_REMOTE": 1,
}
_ASSIGN = re.compile(r"^(smu[ab])\.((?:source|measure)\.\w+|sense)\s*=\s*(\S+)$")
_PRINT = re.compile(r"^print\((.*)\)$")


def _tsp_num(value: float) -> str:
    return f"{value:.5e}"  # format.asciiprecision = 6 (default)


class Keithley2600Simulator(LineSimulator):
    """A 2602B (two channels) speaking TSP."""

    idn = "Keithley Instruments, Model 2602B, 4388888, 3.0.4"
    model = "2602B"

    def __init__(self, cores: dict[str, SMUCore]) -> None:
        self.cores = cores
        self.errors: list[tuple[int, str]] = []
        self.nplc = {name: 1.0 for name in cores}

    def _error(self, code: int, message: str) -> None:
        self.errors.append((code, message))

    def handle(self, command: str) -> str | None:
        cmd = command.strip()
        if cmd == "*IDN?":
            return self.idn
        m = _ASSIGN.match(cmd)
        if m:
            self._assign(m.group(1), m.group(2), m.group(3))
            return None
        m = _PRINT.match(cmd)
        if m:
            return self._print(m.group(1).strip())
        self._error(-285, f"TSP Syntax error at line 1: unexpected symbol near `{cmd[:20]}'")
        return None

    def _value(self, text: str, smu: str) -> float:
        if text.startswith(f"{smu}."):
            name = text.split(".", 1)[1]
            if name in _TSP_CONST:
                return float(_TSP_CONST[name])
            raise KeyError(text)
        return float(text)

    def _assign(self, smu: str, attr: str, text: str) -> None:
        core = self.cores.get(smu)
        if core is None:
            self._error(-286, f"TSP Runtime error at line 1: attempt to index global `{smu}' (a nil value)")
            return
        try:
            value = self._value(text, smu)
        except (KeyError, ValueError):
            self._error(-286, f"TSP Runtime error at line 1: invalid value {text}")
            return
        limits = {"v": core.max_v, "i": core.max_i}
        if attr == "source.func":
            core.func = "VOLT" if value == 1 else "CURR"
        elif attr in {"source.levelv", "source.leveli"}:
            y = attr[-1]
            if abs(value) > limits[y]:
                self._error(1101, "Parameter too big")
            else:
                core.level["VOLT" if y == "v" else "CURR"] = value
        elif attr in {"source.limitv", "source.limiti"}:
            y = attr[-1]
            if value <= 0:
                self._error(1102, "Parameter too small")
            elif value > limits[y]:
                self._error(1101, "Parameter too big")
            else:
                core.limit["VOLT" if y == "v" else "CURR"] = value
        elif attr in {"source.rangev", "source.rangei"}:
            y = attr[-1]
            if abs(value) > limits[y]:
                self._error(1101, "Parameter too big")
            else:
                core.source_range["VOLT" if y == "v" else "CURR"] = abs(value)
        elif attr in {"source.autorangev", "source.autorangei"}:
            if value == 1:
                core.source_range["VOLT" if attr[-1] == "v" else "CURR"] = None
        elif attr == "source.output":
            core.output = value == 1
        elif attr in {"measure.autorangev", "measure.autorangei"}:
            pass
        elif attr == "measure.nplc":
            if not 0.001 <= value <= 25:
                self._error(
                    1101 if value > 25 else 1102, "Parameter too big" if value > 25 else "Parameter too small"
                )
            else:
                core.nplc = value
        elif attr == "sense":
            core.remote_sense = value == 1
        else:
            self._error(-286, f"TSP Runtime error at line 1: unknown attribute {smu}.{attr}")

    def _print(self, expr: str) -> str | None:
        if expr == "errorqueue.count":
            return _tsp_num(len(self.errors))
        if expr == "errorqueue.next()":
            if not self.errors:
                return f"{_tsp_num(0)}\tQueue Is Empty\t{_tsp_num(0)}\t{_tsp_num(1)}"
            code, msg = self.errors.pop(0)
            return f"{_tsp_num(code)}\t{msg}\t{_tsp_num(20)}\t{_tsp_num(1)}"
        if expr == "localnode.model":
            return self.model
        m = re.match(r"^(smu[ab])\.(.+)$", expr)
        core = self.cores.get(m.group(1)) if m else None
        if m is None or core is None:
            self._error(-286, f"TSP Runtime error at line 1: attempt to index a nil value ({expr})")
            return None
        attr = m.group(2)
        if attr in {"measure.iv()", "measure.i()", "measure.v()"}:
            v, i, _ = core.operating_point()
            if attr == "measure.iv()":
                return f"{_tsp_num(i)}\t{_tsp_num(v)}"
            return _tsp_num(i if attr == "measure.i()" else v)
        if attr == "source.compliance":
            return "true" if core.output and core.operating_point()[2] else "false"
        simple = {
            "source.output": 1.0 if core.output else 0.0,
            "source.func": 1.0 if core.func == "VOLT" else 0.0,
            "source.levelv": core.level["VOLT"],
            "source.leveli": core.level["CURR"],
            "source.limitv": core.limit["VOLT"],
            "source.limiti": core.limit["CURR"],
            "sense": 1.0 if core.remote_sense else 0.0,
            "measure.nplc": core.nplc,
        }
        if attr in simple:
            return _tsp_num(simple[attr])
        self._error(-286, f"TSP Runtime error at line 1: unknown attribute {expr}")
        return None


# ====================================================================== factory


def make_simulator(
    dialect: str = "2450", dut: str = "resistor", resistance_ohm: float = 1000.0, seed: int | None = 0
) -> LineSimulator:
    """Build the simulator for ``dialect`` with the chosen DUT on every channel."""
    if dialect == "2400":
        return Keithley2400Simulator(SMUCore(make_dut(dut, resistance_ohm), 210.0, 1.05, seed))
    if dialect == "2450":
        return Keithley2450Simulator(SMUCore(make_dut(dut, resistance_ohm), 210.0, 1.05, seed))
    if dialect == "2600":
        cores = {
            "smua": SMUCore(make_dut(dut, resistance_ohm), 40.0, 3.0, seed),
            "smub": SMUCore(make_dut(dut, resistance_ohm), 40.0, 3.0, None if seed is None else seed + 1),
        }
        for core in cores.values():  # 2602B *RST defaults: 40 V / 1 A limits (reference manual 9-244)
            core.limit = {"VOLT": 40.0, "CURR": 1.0}
        return Keithley2600Simulator(cores)
    raise ValueError(f"Unknown dialect {dialect!r}; use 2400, 2450 or 2600.")
