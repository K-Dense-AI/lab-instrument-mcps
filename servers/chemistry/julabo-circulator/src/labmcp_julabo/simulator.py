"""Wire-level simulator of a JULABO refrigerated circulator.

Speaks the JULABO interface commands: IN commands answer one line, OUT commands answer
nothing and report rejections through the next ``status`` query, exactly as described in
the operating manuals. Unknown commands give no reply and set ``-08 INVALID COMMAND``.
The bath heats/cools towards the setpoint with a realistic rate and time constant;
``time_scale`` speeds the clock up for tests and demos.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from labmcp import LineSimulator

AMBIENT_C = 22.0

_STATE_TEXT = {0: "MANUAL STOP", 1: "MANUAL START", 2: "REMOTE STOP", 3: "REMOTE START"}
_ERROR_TEXT = {
    -8: "INVALID COMMAND",
    -9: "COMMAND NOT ALLOWED IN CURRENT OPERATING MODE",
    -10: "VALUE TOO SMALL",
    -11: "VALUE TOO LARGE",
    -13: "VALUE EXCEEDS TEMPERATURE LIMITS",
}


_CORIO_CD_COMMANDS = {
    "version", "status", "in_pv_00", "in_pv_01", "in_pv_03", "in_pv_04",
    "in_sp_00", "in_mode_05", "out_sp_00", "out_mode_05",
}  # fmt: skip


class JulaboSimulator(LineSimulator):
    def __init__(
        self,
        seed: int | None = 0,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        remote: bool = True,
        corio_cd: bool = False,
    ) -> None:
        self.rng = random.Random(seed)
        self.time_scale = time_scale
        self.clock = clock
        self._last = clock()
        # corio_cd=True restricts the command set to the one documented for CORIO CD.
        self.corio_cd = corio_cd
        self.version = "JULABO CORIO CD-200F VERSION 1.0" if corio_cd else "JULABO MAGIO MS-1000F VERSION 1.1.2"
        self.remote = remote
        self.running = False
        self.bath_c = AMBIENT_C
        self.external_c = AMBIENT_C
        self.setpoint_c = 20.0
        self.power_pct = 0.0
        self.high_warning_c = 150.0  # in_sp_03
        self.low_warning_c = -30.0  # in_sp_04
        self.excess_protection_c = 120.0  # in_pv_04 (set on the device)
        self.range_c = (-30.0, 200.0)  # working temperature range of this model
        self.pump_stage = 3
        self.last_error: int | None = None
        self.alarm: str | None = None  # e.g. "-01 LOW LEVEL ALARM"

    # physics ---------------------------------------------------------------

    def _advance(self) -> None:
        now = self.clock()
        dt = max(0.0, now - self._last) * self.time_scale
        self._last = now
        steps = max(1, min(20000, math.ceil(dt)))
        for _ in range(steps):
            self._step(dt / steps)

    def _step(self, dt: float) -> None:
        heat_rate, cool_rate = 2.0 / 60, 0.8 / 60  # K/s at full power
        loss = (AMBIENT_C - self.bath_c) / 3600.0  # K/s towards ambient
        if self.running:
            error = self.setpoint_c - self.bath_c
            # Controller: enough power to track the setpoint and cancel the losses.
            demand = error / 60.0 - loss  # K/s wanted
            rate = heat_rate if demand > 0 else cool_rate
            self.power_pct = max(-100.0, min(100.0, 100.0 * demand / rate))
            self.bath_c += (self.power_pct / 100.0) * rate * dt + loss * dt
        else:
            self.power_pct = 0.0
            self.bath_c += loss * dt
        self.external_c += (self.bath_c - 0.2 - self.external_c) * (1 - math.exp(-dt / 120.0))

    # protocol --------------------------------------------------------------

    def handle(self, command: str) -> str | list[str] | None:
        self._advance()
        cmd = command.strip()
        head, _, arg = cmd.partition(" ")
        head = head.lower()
        noise = self.rng.gauss(0, 0.01)
        if self.corio_cd and head not in _CORIO_CD_COMMANDS:
            self.last_error = -8
            return None
        match head:
            case "version":
                return self.version
            case "status":
                return self._status()
            case "in_pv_00":
                return f"{self.bath_c + noise:.2f}"
            case "in_pv_01":
                return f"{self.power_pct:.0f}"
            case "in_pv_02":
                return f"{self.external_c + noise:.2f}"
            case "in_pv_03":
                return f"{self.bath_c + 0.3 + noise:.2f}"
            case "in_pv_04":
                return f"{self.excess_protection_c:.0f}"
            case "in_sp_00":
                return f"{self.setpoint_c:.2f}"
            case "in_sp_03":
                return f"{self.high_warning_c:.2f}"
            case "in_sp_04":
                return f"{self.low_warning_c:.2f}"
            case "in_sp_07":
                return str(self.pump_stage)
            case "in_mode_05":
                return "1" if self.running else "0"
            case "out_sp_00" if arg:
                return self._set_setpoint(arg)
            case "out_mode_05" if arg in {"0", "1"}:
                if not self.remote or arg == "1" and self.alarm:
                    self.last_error = -9
                else:
                    self.running = arg == "1"
                return None
        self.last_error = -8
        return None

    def _set_setpoint(self, arg: str) -> None:
        try:
            value = float(arg)
        except ValueError:
            self.last_error = -8
            return None
        if not self.remote:
            self.last_error = -9
        elif value > self.range_c[1]:
            self.last_error = -11
        elif value < self.range_c[0]:
            self.last_error = -10
        elif not self.low_warning_c <= value <= self.high_warning_c:
            self.last_error = -13
        else:
            self.setpoint_c = round(value, 2)
        return None

    def _status(self) -> str:
        if self.last_error is not None:
            code, self.last_error = self.last_error, None
            return f"{code:03d} {_ERROR_TEXT[code]}"
        if self.alarm:
            return self.alarm
        code = (2 if self.remote else 0) + (1 if self.running else 0)
        return f"{code:02d} {_STATE_TEXT[code]}"

    def raise_alarm(self, text: str = "-01 LOW LEVEL ALARM") -> None:
        """Test helper: trigger an alarm (the circulator stops, as it would)."""
        self.alarm = text
        self.running = False
