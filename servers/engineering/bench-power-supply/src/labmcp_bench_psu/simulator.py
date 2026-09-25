"""Per-dialect SCPI simulators of bench power supplies driving resistive loads.

Each output drives a resistor. With the output on, the supply regulates voltage (CV) while
``V_set / R <= I_set`` and crosses over to constant current (CC, ``V = I_set * R``) otherwise.
Over-voltage / over-current protection trips turn the output off, as on the real instruments.
Reply formats follow the programming guides cited in :mod:`labmcp_bench_psu.driver`.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from labmcp.scpi import SCPISimulator

from labmcp_bench_psu.driver import RIGOL_MODELS, SIGLENT_MODELS, TTI_MODELS, ChannelSpec

#: Default resistive loads (ohms) per channel: CH1 crosses to CC at modest settings.
DEFAULT_LOADS = (10.0, 100.0, 1e9)


@dataclass
class SimOutput:
    spec: ChannelSpec
    load_ohm: float
    set_v: float = 0.0
    set_i: float = 1.0
    on: bool = False
    ovp_v: float = 0.0
    ovp_on: bool = False
    ocp_a: float = 0.0
    ocp_on: bool = False
    ovp_trip: bool = False
    ocp_trip: bool = False
    events: int = 0  # Aim-TTi limit event status register

    def solve(self) -> tuple[float, float, str]:
        """(|V|, I, mode) at the terminals."""
        if not self.on:
            return 0.0, 0.0, "CV"
        vmag = abs(self.set_v)
        if vmag / self.load_ohm <= self.set_i:
            return vmag, vmag / self.load_ohm, "CV"
        return self.set_i * self.load_ohm, self.set_i, "CC"

    def update(self) -> None:
        v, i, mode = self.solve()
        if not self.on:
            return
        self.events |= 0x01 if mode == "CV" else 0x02
        if self.ovp_on and v > self.ovp_v:
            self.on, self.ovp_trip = False, True
            self.events |= 0x04
        elif self.ocp_on and i > self.ocp_a:
            self.on, self.ocp_trip = False, True
            self.events |= 0x08


class _PSUSim(SCPISimulator):
    models: dict[str, list[ChannelSpec]] = {}
    maker = ""

    def __init__(self, model: str, loads: tuple[float, ...] = DEFAULT_LOADS, seed: int | None = 0) -> None:
        super().__init__()
        self.model = model
        self.rng = random.Random(seed)
        self.idn = f"{self.maker},{model},SIM000123,00.01.17"
        self.loads = loads
        self.reset()

    def reset(self) -> None:
        specs = self.models[self.model]
        self.outputs = {
            s.number: SimOutput(s, self.loads[min(i, len(self.loads) - 1)], set_i=min(1.0, s.max_current_a),
                                set_v=0.0 if s.programmable else s.max_voltage_v,
                                ovp_v=s.max_voltage_v * 1.05, ocp_a=s.max_current_a * 1.05)
            for i, s in enumerate(specs)
        }
        self.selected = 1

    def out(self, ch: int) -> SimOutput:
        if ch not in self.outputs:
            raise ValueError(f"no channel {ch}")
        return self.outputs[ch]

    def measured(self, ch: int) -> tuple[float, float]:
        o = self.out(ch)
        o.update()
        v, i, _ = o.solve()
        if o.on:
            v += self.rng.gauss(0, 0.0005)
            i += self.rng.gauss(0, 0.0002)
        sign = -1.0 if o.spec.negative else 1.0
        return sign * max(v, 0.0), max(i, 0.0)

    def set_voltage(self, ch: int, value: float) -> None:
        o = self.out(ch)
        if (value < 0) != o.spec.negative and value != 0:
            raise ValueError("wrong polarity")
        if abs(value) > o.spec.max_voltage_v:
            raise ValueError("out of range")
        o.set_v = value
        o.update()

    def set_current(self, ch: int, value: float) -> None:
        o = self.out(ch)
        if not 0 <= value <= o.spec.max_current_a:
            raise ValueError("out of range")
        o.set_i = value
        o.update()

    def switch(self, ch: int, on: bool) -> None:
        o = self.out(ch)
        if on:
            o.ovp_trip = o.ocp_trip = False
        o.on = on
        o.update()


def _ch(arg: str, default: int) -> int:
    m = re.search(r"CH(\d)", arg.upper())
    return int(m.group(1)) if m else default


def _onoff(text: str) -> bool:
    t = text.strip().upper()
    if t in {"ON", "1"}:
        return True
    if t in {"OFF", "0"}:
        return False
    raise ValueError(text)


class RigolSimulator(_PSUSim):
    """Rigol DP800/DP700/DP900 dialect (default DP832)."""

    models = RIGOL_MODELS
    maker = "RIGOL TECHNOLOGIES"

    def reset(self) -> None:
        super().reset()
        for o in self.outputs.values():  # *RST: outputs off, OVP/OCP off (DP800 guide, Appendix B)
            o.ovp_on = o.ocp_on = False

    def command(self, key: str, arg: str) -> str | None:
        m = self.matches
        args = [a.strip() for a in arg.split(",")] if arg else []
        ch = _ch(arg, self.selected)
        src = re.match(r"^SOUR(?:CE)?(\d)", key)
        sch = int(src.group(1)) if src else self.selected
        if m(key, "[SOURce<n>:]VOLTage[:LEVel][:IMMediate][:AMPLitude]"):
            self.set_voltage(sch, float(args[0]))
            return None
        if m(key, "[SOURce<n>:]VOLTage[:LEVel][:IMMediate][:AMPLitude]?"):
            return f"{self.out(sch).set_v:.3f}"
        if m(key, "[SOURce<n>:]CURRent[:LEVel][:IMMediate][:AMPLitude]"):
            self.set_current(sch, float(args[0]))
            return None
        if m(key, "[SOURce<n>:]CURRent[:LEVel][:IMMediate][:AMPLitude]?"):
            return f"{self.out(sch).set_i:.4f}"
        if m(key, "APPLy?"):
            o = self.out(ch)
            rating = f"{'-' if o.spec.negative else ''}{round(o.spec.max_voltage_v / 1.06):g}V/{round(o.spec.max_current_a / 1.06):g}A"
            return f"CH{ch}:{rating},{o.set_v:.3f},{o.set_i:.4f}"
        if m(key, "APPLy"):
            self.selected = ch
            nums = [a for a in args if not a.upper().startswith("CH")]
            if nums:
                self.set_voltage(ch, float(nums[0]))
            if len(nums) > 1:
                self.set_current(ch, float(nums[1]))
            return None
        if m(key, "INSTrument:NSELect"):
            self.out(int(args[0]))
            self.selected = int(args[0])
            return None
        if m(key, "MEASure:ALL[:DC]?"):
            v, i = self.measured(ch)
            return f"{v:.4f},{i:.4f},{abs(v * i):.3f}"
        if m(key, "MEASure[:VOLTage][:DC]?"):
            return f"{self.measured(ch)[0]:.4f}"
        if m(key, "MEASure:CURRent[:DC]?"):
            return f"{self.measured(ch)[1]:.4f}"
        if m(key, "OUTPut[:STATe]"):
            self.switch(ch, _onoff(args[-1]))
            return None
        if m(key, "OUTPut[:STATe]?"):
            o = self.out(ch)
            o.update()
            return "ON" if o.on else "OFF"
        if m(key, "OUTPut:MODE?") or m(key, "OUTPut:CVCC?"):
            o = self.out(ch)
            o.update()
            return o.solve()[2] if o.on else "CV"
        for kind in ("OVP", "OCP"):
            if m(key, f"OUTPut:{kind}:VALue"):
                level = float(args[-1])
                o = self.out(ch)
                if kind == "OVP":
                    o.ovp_v = level
                else:
                    o.ocp_a = level
                o.update()
                return None
            if m(key, f"OUTPut:{kind}:VALue?"):
                o = self.out(ch)
                return f"{o.ovp_v if kind == 'OVP' else o.ocp_a:.3f}"
            if m(key, f"OUTPut:{kind}[:STATe]"):
                o = self.out(ch)
                if kind == "OVP":
                    o.ovp_on = _onoff(args[-1])
                else:
                    o.ocp_on = _onoff(args[-1])
                o.update()
                return None
            if m(key, f"OUTPut:{kind}[:STATe]?"):
                o = self.out(ch)
                return "ON" if (o.ovp_on if kind == "OVP" else o.ocp_on) else "OFF"
            if m(key, f"OUTPut:{kind}:QUES?") or m(key, f"OUTPut:{kind}:ALAR?"):
                o = self.out(ch)
                o.update()
                return "YES" if (o.ovp_trip if kind == "OVP" else o.ocp_trip) else "NO"
            if m(key, f"OUTPut:{kind}:CLEAR"):
                o = self.out(ch)
                if kind == "OVP":
                    o.ovp_trip = False
                else:
                    o.ocp_trip = False
                return None
        raise self.undefined()


class SiglentSimulator(_PSUSim):
    """Siglent SPD3303X(-E) / SPD1000X dialect (default SPD3303X)."""

    models = SIGLENT_MODELS
    maker = "Siglent Technologies"

    def _dispatch(self, cmd: str) -> str | None:
        head = cmd.partition(" ")[0].upper().lstrip(":")
        if head in {"SYST:ERR?", "SYSTEM:ERROR?"}:
            if self.error_queue:
                code, _, text = self.error_queue.pop(0).partition(",")
                return f"{code} {text.strip(chr(34))}"
            return "0 No Error"
        return super()._dispatch(cmd)

    def command(self, key: str, arg: str) -> str | None:
        m = self.matches
        args = [a.strip() for a in arg.split(",")] if arg else []
        prefix = re.match(r"^CH(\d):(.*)$", key)
        if prefix:
            ch, sub = int(prefix.group(1)), prefix.group(2)
        else:
            ch, sub = self.selected, key
        single = len(self.outputs) == 1
        if m(sub, "VOLTage") and self.out(ch).spec.programmable:
            self.set_voltage(ch, float(args[0]))
            return None
        if m(sub, "VOLTage?") and self.out(ch).spec.programmable:
            return f"{self.out(ch).set_v:.3f}"
        if m(sub, "CURRent") and self.out(ch).spec.programmable:
            self.set_current(ch, float(args[0]))
            return None
        if m(sub, "CURRent?") and self.out(ch).spec.programmable:
            return f"{self.out(ch).set_i:.3f}"
        if m(key, "INSTrument"):
            self.selected = _ch(arg, 1)
            self.out(self.selected)
            return None
        if m(key, "INSTrument?"):
            return f"CH{self.selected}"
        if m(key, "MEASure:VOLTage?"):
            return f"{self.measured(_ch(arg, self.selected))[0]:.3f}"
        if m(key, "MEASure:CURRent?"):
            return f"{self.measured(_ch(arg, self.selected))[1]:.3f}"
        if m(key, "MEASure:POWEr?"):
            v, i = self.measured(_ch(arg, self.selected))
            return f"{v * i:.3f}"
        if m(key, "OUTPut"):
            self.switch(_ch(args[0], 1), _onoff(args[1]))
            return None
        if m(key, "SYSTem:STATus?"):
            word = 0
            for n in (1, 2):
                if n in self.outputs:
                    o = self.outputs[n]
                    o.update()
                    word |= (o.solve()[2] == "CC") << (n - 1)
                    word |= o.on << (3 + n)
            word |= 0 if single else 0x04  # bits 2,3 = 01: independent mode
            return f"0x{word:04X}"
        if single and m(key, "OVP"):
            self.outputs[1].ovp_v, self.outputs[1].ovp_on = float(args[0]), True
            return None
        if single and m(key, "OVP?"):
            return f"{self.outputs[1].ovp_v:.3f}"
        if single and m(key, "OCP"):
            self.outputs[1].ocp_a, self.outputs[1].ocp_on = float(args[0]), True
            return None
        if single and m(key, "OCP?"):
            return f"{self.outputs[1].ocp_a:.3f}"
        if single and m(key, "OUTPut:RESEt:PROTect"):
            self.outputs[1].ovp_trip = self.outputs[1].ocp_trip = False
            return None
        raise self.undefined()

    def reset(self) -> None:
        super().reset()
        if len(self.outputs) == 1:  # SPD1000X protection is always active
            o = self.outputs[1]
            o.ovp_on = o.ocp_on = True


class TTiSimulator(_PSUSim):
    """Aim-TTi (Thurlby Thandar) CPX / MX / QL / PL-P remote command set (default CPX400DP)."""

    models = TTI_MODELS
    maker = "THURLBY THANDAR"

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.eer = 0
        self.qer = 0
        self.esr = 0
        super().__init__(*args, **kwargs)
        self.idn = f"{self.maker},{self.model},279730,1.00-1.00"

    def reset(self) -> None:
        super().reset()
        for o in self.outputs.values():  # protection is always armed on CPX/QL/PL-P
            o.ovp_on = o.ocp_on = True

    def _dispatch(self, cmd: str) -> str | None:
        head, _, arg = cmd.partition(" ")
        key = head.upper().lstrip(":")
        if key == "*IDN?":
            return self.idn
        if key == "*RST":
            self.reset()
            return None
        if key == "*OPC?":
            return "1"
        if key in {"EER?", "QER?", "*ESR?"}:
            attr = {"EER?": "eer", "QER?": "qer", "*ESR?": "esr"}[key]
            value, _ = getattr(self, attr), setattr(self, attr, 0)
            return str(value)
        try:
            return self.command(key, arg.strip())
        except ValueError:
            self.eer = 100
            self.esr |= 0x10
            return None
        except Exception:
            self.esr |= 0x20
            return None

    def command(self, key: str, arg: str) -> str | None:
        if key == "OPALL":
            for n in self.outputs:
                self.switch(n, _onoff(arg))
            return None
        if key == "TRIPRST":
            for o in self.outputs.values():
                o.ovp_trip = o.ocp_trip = False
            return None
        m = re.fullmatch(r"(V|I|OP|OVP|OCP|LSR)(\d)(O?)(\??)", key)
        if not m:
            raise self.undefined()
        what, ch, readback, query = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if ch not in self.outputs:
            self.eer = 103
            self.esr |= 0x10
            return None
        o = self.outputs[ch]
        if readback and query:
            v, i = self.measured(ch)
            return f"{v:.3f}V" if what == "V" else f"{i:.3f}A"
        if query:
            if what == "V":
                return f"V{ch} {o.set_v:.3f}"
            if what == "I":
                return f"I{ch} {o.set_i:.3f}"
            if what == "OP":
                o.update()
                return "1" if o.on else "0"
            if what == "OVP":
                return f"VP{ch} {o.ovp_v:.2f}"
            if what == "OCP":
                return f"CP{ch} {o.ocp_a:.3f}"
            o.update()
            events, o.events = o.events, 0
            return str(events)
        if what == "V":
            self.set_voltage(ch, float(arg))
        elif what == "I":
            self.set_current(ch, float(arg))
        elif what == "OP":
            self.switch(ch, _onoff(arg))
        elif what in {"OVP", "OCP"}:
            level = float(arg)
            if what == "OVP":
                o.ovp_v = level
            else:
                o.ocp_a = level
            o.update()
        else:
            raise self.undefined()
        return None


def make_simulator(dialect: str = "auto", model: str | None = None) -> _PSUSim:
    dialect = "rigol" if dialect in {"", "auto"} else dialect
    cls, default = {
        "rigol": (RigolSimulator, "DP832"),
        "siglent": (SiglentSimulator, "SPD3303X"),
        "tti": (TTiSimulator, "CPX400DP"),
    }[dialect]
    return cls(model or default)
