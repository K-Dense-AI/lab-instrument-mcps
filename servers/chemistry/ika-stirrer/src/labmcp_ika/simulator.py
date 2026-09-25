"""Wire-level simulator of an IKA hotplate stirrer or overhead stirrer (NAMUR commands).

Replies are ``"<value> <channel> "`` (the trailing blank is part of IKA's "blank CR LF"
terminator; the transport adds CR LF). Commands without a reply return nothing, the
``@`` commands echo their value, and unknown commands are ignored, as on real devices,
which never send error messages.

Physics: the plate heats towards the setpoint (rate limited), the medium probe follows the
plate, and with the external probe connected the plate regulates on the medium, as IKA
hotplates do. ``time_scale`` speeds the clock up for tests and demos.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from labmcp import LineSimulator

AMBIENT_C = 22.0


class IKASimulator(LineSimulator):
    def __init__(
        self,
        device: str = "hotplate",
        seed: int | None = 0,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.rng = random.Random(seed)
        self.time_scale = time_scale
        self.clock = clock
        self._last = clock()
        self.sim_time = 0.0
        # hotplate state
        self.plate_c = AMBIENT_C
        self.medium_c = AMBIENT_C
        self.temp_setpoint_c = 0.0
        self.safety_temp_c = 360.0  # safety-circuit dial (IN_SP_3)
        self.heating = False
        self.external_probe = True
        # motor state
        self.speed_setpoint = 0.0
        self.speed = 0.0
        self.motor = False
        # overhead stirrer settings (factory values from the EUROSTAR 60 control manual)
        self.speed_limit = 2000.0
        self.torque_limit = 60.0
        self.safe_speed = 100.0
        self.probe_c = AMBIENT_C
        # watchdog
        self.wd_mode: int | None = None
        self.wd_time_s = 0.0
        self.wd_last = 0.0
        self.wd_safety_temp_c = 0.0
        self.wd_safety_speed = 0.0
        self.wd_tripped = False

    # physics ---------------------------------------------------------------

    def _advance(self) -> None:
        now = self.clock()
        dt = max(0.0, now - self._last) * self.time_scale
        self._last = now
        self._check_watchdog(self.sim_time + dt)
        # Integrate in small steps so long idle periods (or a fast time_scale) stay stable.
        steps = max(1, min(20000, math.ceil(dt / 1.0)))
        for _ in range(steps):
            self._step(dt / steps)
        self.sim_time += dt

    def _step(self, dt: float) -> None:
        if self.device == "hotplate":
            if self.heating:
                target = self.temp_setpoint_c
                if self.external_probe:  # regulate on the medium temperature
                    target += 1.0 * (self.temp_setpoint_c - self.medium_c)
                target = min(max(target, AMBIENT_C), self.safety_temp_c - 5, 500.0)
                tau, max_rate = 60.0, 15.0 / 60.0  # s, K/s
            else:
                target, tau, max_rate = AMBIENT_C, 300.0, 5.0 / 60.0
            step = (target - self.plate_c) * (1 - math.exp(-dt / tau))
            self.plate_c += max(-max_rate * dt, min(max_rate * dt, step))
            self.medium_c += (self.plate_c - self.medium_c) * (1 - math.exp(-dt / 150.0))
        target_speed = min(self.speed_setpoint, self.speed_limit) if self.motor else 0.0
        self.speed += (target_speed - self.speed) * (1 - math.exp(-dt / 2.0))

    def _check_watchdog(self, now: float) -> None:
        if self.wd_mode is None or now - self.wd_last <= self.wd_time_s:
            return
        if self.wd_mode == 1:  # heating and stirring off, ER 2 on the display
            self.heating = False
            self.motor = False
        else:  # fall back to the WD safety limits, "WD" warning on the display
            self.temp_setpoint_c = self.wd_safety_temp_c
            self.speed_setpoint = self.wd_safety_speed
        self.wd_tripped = True
        self.wd_mode = None

    def _noisy(self, value: float, sigma: float) -> float:
        return value + self.rng.gauss(0, sigma)

    # protocol --------------------------------------------------------------

    def handle(self, command: str) -> str | list[str] | None:
        self._advance()
        cmd = command.strip()
        head, _, arg = cmd.partition(" ")
        hotplate = self.device == "hotplate"

        if head == "IN_NAME":
            return ("RCT digital" if hotplate else "EUROSTAR 60 control") + " "
        if head == "IN_PV_4":
            return self._reply(round(self._noisy(self.speed, 0.5) if self.speed > 1 else 0.0), 4)
        if head == "IN_SP_4":
            return self._reply(self.speed_setpoint, 4)
        if head == "OUT_SP_4" and arg:
            self.speed_setpoint = max(0.0, float(arg))
            return None
        if head == "START_4":
            self.motor = True
            return None
        if head == "STOP_4":
            self.motor = False
            return None
        if head == "RESET":
            self.heating = self.motor = False
            return None

        if hotplate:
            return self._hotplate(head, arg)
        return self._overhead(head, arg)

    def _hotplate(self, head: str, arg: str) -> str | None:
        match head:
            case "IN_PV_1":
                return self._reply(self._noisy(self.medium_c, 0.05), 1)
            case "IN_PV_2":
                return self._reply(self._noisy(self.plate_c, 0.1), 2)
            case "IN_PV_5":
                return self._reply(0.0, 5)
            case "IN_SP_1":
                return self._reply(self.temp_setpoint_c, 1)
            case "IN_SP_3":
                return self._reply(self.safety_temp_c, 3)
            case "OUT_SP_1" if arg:
                # The device will not accept a setpoint above its safety-circuit temperature.
                self.temp_setpoint_c = min(max(0.0, float(arg)), self.safety_temp_c)
                return None
            case "START_1":
                self.heating = True
                return None
            case "STOP_1":
                self.heating = False
                return None
        if head.startswith("OUT_SP_12@"):
            self.wd_safety_temp_c = float(head.partition("@")[2])
            return f"{self.wd_safety_temp_c:g} "
        if head.startswith("OUT_SP_42@"):
            self.wd_safety_speed = float(head.partition("@")[2])
            return f"{self.wd_safety_speed:g} "
        if head.startswith(("OUT_WD1@", "OUT_WD2@")):
            mode = int(head[6])
            value = int(head.partition("@")[2])
            if mode == 2 and value == 0:
                self.wd_mode = None
            elif 20 <= value <= 1500:
                self.wd_mode, self.wd_time_s, self.wd_last = mode, float(value), self.sim_time
                self.wd_tripped = False
            else:
                return None
            return f"{value} "
        return None  # unknown command: real devices stay silent

    def _overhead(self, head: str, arg: str) -> str | None:
        match head:
            case "IN_PV_3":
                return self._reply(self._noisy(self.probe_c, 0.05), 3)
            case "IN_PV_5":  # torque grows with speed in a viscous medium
                return self._reply(max(0.0, self._noisy(self.speed * 0.01, 0.2)), 5)
            case "IN_SP_5":
                return self._reply(self.torque_limit, 5)
            case "IN_SP_6":
                return self._reply(self.speed_limit, 6)
            case "IN_SP_8":
                return self._reply(self.safe_speed, 8)
            case "IN_MODE":
                return "1 "
        return None

    @staticmethod
    def _reply(value: float, channel: int) -> str:
        return f"{value:.1f} {channel} "
