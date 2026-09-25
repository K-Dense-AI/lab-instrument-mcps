"""Wire-level simulator of a New Era NE-1000 syringe pump (RS-232 Basic mode).

Reproduces the reply packets of NE-1000 User Manual section 10 (``STX addr status data ETX``,
``?``/``?NA``/``?OOR`` errors, ``A?R`` power-on alarm, ``A?S`` stall alarm) and models pumping
progress over time: the plunger advances at the programmed rate, phase 1 ends once its
"Volume to be Dispensed" is reached and the program moves on to phase 2.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable

from labmcp import LineSimulator

STX = "\x02"
RATE_FACTORS = {"MM": 1.0, "UM": 1e-3, "MH": 1.0 / 60.0, "UH": 1e-3 / 60.0}  # to mL/min
MAX_SPEED_CM_MIN = 5.1005  # NE-1000 spec (manual 12.5)
MIN_SPEED_CM_H = 0.004205
_NUM_RE = re.compile(r"^\d*\.?\d+$")


def _fmt(value: float) -> str:
    for decimals in (3, 2, 1, 0):
        text = f"{value:.{decimals}f}"
        if sum(ch.isdigit() for ch in text) <= 4:
            return text
    return "9999"


def _valid_float(text: str) -> bool:
    if not _NUM_RE.match(text):
        return False
    digits = sum(ch.isdigit() for ch in text)
    decimals = len(text.split(".")[1]) if "." in text else 0
    return digits <= 4 and decimals <= 3


class NewEraSimulator(LineSimulator):
    def __init__(
        self,
        address: int = 0,
        model: str = "1000",
        firmware: str = "3.923",
        clock: Callable[[], float] | None = None,
        speed: float = 1.0,
    ) -> None:
        self.address = address
        self.model = model
        self.firmware = firmware
        self._t0 = time.monotonic()
        self.clock = clock or (lambda: (time.monotonic() - self._t0) * speed)
        self.diameter = 14.43  # BD 10 mL
        self.volume_units_override: str | None = None
        # Factory-ish program: phase 1 RATE; phase 2 also RATE with no volume target (continuous),
        # so a driver that forgets to program phase 2 = STOP never stops.
        self.phases: dict[int, dict[str, object]] = {
            1: {"fun": "RAT", "rate": 1.0, "units": "MM", "vol": 0.0, "dir": "INF"},
            2: {"fun": "RAT", "rate": 1.0, "units": "MM", "vol": 0.0, "dir": "INF"},
        }
        self.selected = 1
        self.running = False
        self.paused = False
        self.run_phase = 1
        self.phase_done_ml = 0.0
        self.dispensed = {"INF": 0.0, "WDR": 0.0}  # mL
        self.alarm: str | None = "R"  # power-on reset alarm, as after switching the pump on
        self.stall_after_ml: float | None = None  # test hook: stall once this much has been pumped
        self._last = self.clock()

    # ------------------------------------------------------------ helpers

    def _vol_units(self) -> str:
        if self.volume_units_override:
            return self.volume_units_override
        return "UL" if self.diameter <= 14.0 else "ML"

    def _vol_factor(self) -> float:
        return 1e-3 if self._vol_units() == "UL" else 1.0

    def _phase(self, n: int) -> dict[str, object]:
        return self.phases.setdefault(n, {"fun": "RAT", "rate": 1.0, "units": "MM", "vol": 0.0, "dir": "INF"})

    def _rate_ml_min(self, ph: dict[str, object]) -> float:
        return float(ph["rate"]) * RATE_FACTORS[str(ph["units"])]  # type: ignore[arg-type]

    def _rate_limits(self) -> tuple[float, float]:
        area = math.pi * (self.diameter / 20.0) ** 2
        return area * MIN_SPEED_CM_H / 60.0, area * MAX_SPEED_CM_MIN

    def _operating(self) -> bool:
        return self.running

    def _advance(self) -> None:
        now = self.clock()
        dt_min = max(0.0, now - self._last) / 60.0
        self._last = now
        while self.running and dt_min > 0:
            ph = self._phase(self.run_phase)
            if ph["fun"] != "RAT":
                self._finish_phase()
                continue
            rate = self._rate_ml_min(ph)
            target = float(ph["vol"]) * self._vol_factor()  # type: ignore[arg-type]
            remaining = target - self.phase_done_ml if target > 0 else math.inf
            step_ml = min(rate * dt_min, remaining)
            direction = str(ph["dir"]) if ph["dir"] in {"INF", "WDR"} else "INF"
            total = self.dispensed["INF"] + self.dispensed["WDR"]
            if self.stall_after_ml is not None and total + step_ml >= self.stall_after_ml:
                self.dispensed[direction] += max(0.0, self.stall_after_ml - total)
                self.running = False
                self.alarm = "S"
                return
            self.dispensed[direction] += step_ml
            self.phase_done_ml += step_ml
            dt_min -= step_ml / rate if rate > 0 else dt_min
            if self.phase_done_ml >= target > 0:
                self._finish_phase()

    def _finish_phase(self) -> None:
        self.run_phase += 1
        self.phase_done_ml = 0.0
        if self._phase(self.run_phase)["fun"] == "STP" or self.run_phase > 41:
            self.running = False
            self.paused = False
            self.run_phase = 1

    def _reply(self, data: str = "") -> str:
        if self.running:
            prompt = "I" if self._phase(self.run_phase)["dir"] == "INF" else "W"
        elif self.paused:
            prompt = "P"
        else:
            prompt = "S"
        return f"{STX}{self.address:02d}{prompt}{data}"

    # ------------------------------------------------------------ protocol

    def handle(self, command: str) -> str | list[str] | None:
        cmd = re.sub(r"\s+", "", command).upper()  # the pump strips spaces and upper-cases
        m = re.match(r"^(\d{1,2})(.*)$", cmd)
        addr, body = (int(m[1]), m[2]) if m else (0, cmd)
        if addr != self.address:
            return None  # other pumps on the network ignore the packet
        self._advance()
        if self.alarm:  # any valid packet acknowledges a pending alarm
            alarm, self.alarm = self.alarm, None
            return f"{STX}{self.address:02d}A?{alarm}"
        return self._reply(self._dispatch(body))

    def _dispatch(self, body: str) -> str:
        if body == "":
            return ""
        if body == "VER":
            return f"NE{self.model}V{self.firmware}"
        for key in ("DIA", "PHN", "FUN", "RAT", "VOL", "DIR", "RUN", "STP", "DIS", "CLD"):
            if body.startswith(key):
                return getattr(self, f"_cmd_{key.lower()}")(body[len(key) :])
        return "?"

    def _cmd_dia(self, arg: str) -> str:
        if not arg:
            return _fmt(self.diameter)
        if self._operating():
            return "?NA"
        if not _valid_float(arg) or not 0.1 <= float(arg) <= 50.0:
            return "?OOR"
        self.diameter = float(arg)
        self.dispensed = {"INF": 0.0, "WDR": 0.0}  # changing the diameter clears the accumulators
        return ""

    def _cmd_phn(self, arg: str) -> str:
        if not arg:
            return f"{self.selected:02d}"
        if self._operating():
            return "?NA"
        if not arg.isdigit() or not 1 <= int(arg) <= 41:
            return "?OOR"
        self.selected = int(arg)
        return ""

    def _cmd_fun(self, arg: str) -> str:
        ph = self._phase(self.selected)
        if not arg:
            return str(ph["fun"])
        if self._operating():
            return "?NA"
        if arg not in {"RAT", "STP", "FIL", "INC", "DEC", "CLD", "BEP"}:
            return "?"
        ph["fun"] = arg
        return ""

    def _cmd_rat(self, arg: str) -> str:
        ph = self._phase(self.run_phase if self.running else self.selected)
        if ph["fun"] != "RAT":
            return "?NA"
        if not arg:
            return f"{_fmt(float(ph['rate']))}{ph['units']}"  # type: ignore[arg-type]
        m = re.match(r"^(\d*\.?\d+)(UM|MM|UH|MH)?$", arg)
        if not m or not _valid_float(m[1]):
            return "?OOR"
        units = m[2] or str(ph["units"])
        if self.running and m[2] and m[2] != ph["units"]:
            return "?NA"  # rate units cannot be changed while pumping
        rate = float(m[1]) * RATE_FACTORS[units]
        lo, hi = self._rate_limits()
        if not lo <= rate <= hi:
            return "?OOR"
        ph["rate"], ph["units"] = float(m[1]), units
        return ""

    def _cmd_vol(self, arg: str) -> str:
        ph = self._phase(self.selected)
        if not arg:
            return f"{_fmt(float(ph['vol']))}{self._vol_units()}"  # type: ignore[arg-type]
        if self._operating():
            return "?NA"
        if arg in {"UL", "ML"}:
            self.volume_units_override = arg
            return ""
        if not _valid_float(arg):
            return "?OOR"
        ph["vol"] = float(arg)
        return ""

    def _cmd_dir(self, arg: str) -> str:
        ph = self._phase(self.run_phase if self.running else self.selected)
        if not arg:
            return str(ph["dir"])
        if arg not in {"INF", "WDR", "REV", "STK"}:
            return "?"
        if self.running and float(ph["vol"]) > 0:  # type: ignore[arg-type]
            return "?NA"
        if arg == "REV":
            ph["dir"] = "WDR" if ph["dir"] == "INF" else "INF"
        else:
            ph["dir"] = arg
        return ""

    def _cmd_run(self, arg: str) -> str:
        if self.running:
            return "?NA"
        if self.paused:
            self.paused = False
        else:
            self.run_phase = int(arg) if arg.isdigit() else 1
            self.phase_done_ml = 0.0
        self.running = True
        self._last = self.clock()
        return ""

    def _cmd_stp(self, arg: str) -> str:
        if self.running:
            self.running, self.paused = False, True
        elif self.paused:
            self.paused, self.run_phase, self.phase_done_ml = False, 1, 0.0
        return ""

    def _cmd_dis(self, arg: str) -> str:
        f = self._vol_factor()
        return f"I{_fmt(self.dispensed['INF'] / f)}W{_fmt(self.dispensed['WDR'] / f)}{self._vol_units()}"

    def _cmd_cld(self, arg: str) -> str:
        if self._operating():
            return "?NA"
        if arg not in {"INF", "WDR"}:
            return "?"
        self.dispensed[arg] = 0.0
        return ""
