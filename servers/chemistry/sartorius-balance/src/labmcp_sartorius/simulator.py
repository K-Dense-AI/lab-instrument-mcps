"""Wire-level SBI simulator of a Sartorius analytical balance (220 g x 0.1 mg, internal weight).

``ESC P`` returns a 22-character line (``fmt=22``: 6-character ID code + 16 characters) or a
16-character line (``fmt=16``), laid out exactly as in the interface descriptions. While
the reading is settling (after a load change, tare or zero) the unit symbol is replaced by
spaces, as real balances do. Control commands are silent; unknown commands are ignored.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from labmcp import LineSimulator

CAPACITY_G = 220.0
SETTLE_S = 1.5
ADJUST_S = 8.0


class SBISimulator(LineSimulator):
    def __init__(
        self,
        load_g: float = 52.18734,
        fmt: int = 22,
        seed: int | None = 0,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        internal_weight: bool = True,
        adjust_s: float = ADJUST_S,
    ) -> None:
        self.rng = random.Random(seed)
        self.fmt = fmt
        self.time_scale = time_scale
        self.clock = clock
        self._t0 = clock()
        self.load_g = load_g  # what is physically on the pan
        self.zero_g = 0.0
        self.tare_g = 0.0
        self.internal_weight = internal_weight
        self.adjust_s = adjust_s
        self.keys_locked = False
        self.ambient = "L"
        self._settled_at = 0.0
        self._adjusting_until = -1.0
        self.adjustments = 0

    # helpers ---------------------------------------------------------------

    def now(self) -> float:
        return (self.clock() - self._t0) * self.time_scale

    def place(self, load_g: float) -> None:
        """Put a load on the pan (test/demo helper): the reading settles over ~1.5 s."""
        self.load_g = load_g
        self._settled_at = self.now() + SETTLE_S

    def _net(self) -> float:
        return self.load_g - self.zero_g - self.tare_g

    def _line(self, body: str, ident: str = "N") -> str:
        return f"{ident:<6}{body}" if self.fmt == 22 else body

    def _weight_line(self) -> str:
        stable = self.now() >= self._settled_at
        value = self._net()
        if stable:
            value += self.rng.gauss(0, 0.00003)
        else:  # settling: noisy, drifting towards the final value
            value += (self._settled_at - self.now()) * 0.002 + self.rng.gauss(0, 0.0003)
        value = round(value, 4)
        sign = "-" if value < 0 else "+"
        unit = "g" if stable else ""
        return self._line(f"{sign} {abs(value):>8.4f} {unit:<3}")

    def _special(self, text: str, position: int) -> str:
        # Special codes start at a fixed position of the 16-character block; the
        # 22-character format prefixes the ID code "Stat".
        return self._line((" " * (position - 1) + text).ljust(14), ident="Stat")

    # protocol --------------------------------------------------------------

    def handle(self, command: str) -> str | list[str] | None:
        if not command.startswith("\x1b"):
            return None
        cmd = command[1:]
        adjusting = self.now() < self._adjusting_until
        match cmd:
            case "P" | "kP_":
                if adjusting:
                    return self._special("Cal.Int.", 4)
                if self.load_g - self.zero_g > CAPACITY_G * 1.02:
                    return self._special("High", 7)
                if self.load_g - self.zero_g < -CAPACITY_G * 0.02:
                    return self._special("Low", 7)
                return self._weight_line()
            case "T":  # tare or zero: zeroes within the zero range, tares otherwise
                if abs(self.load_g - self.zero_g) <= CAPACITY_G * 0.02:
                    self.zero_g, self.tare_g = self.load_g, 0.0
                else:
                    self.tare_g = self.load_g - self.zero_g
                self._settled_at = self.now() + 0.5
            case "U":
                self.tare_g = self.load_g - self.zero_g
                self._settled_at = self.now() + 0.5
            case "V":  # zero key: only within the zero-setting range (here +-2 % of capacity)
                if abs(self.load_g - self.zero_g) <= CAPACITY_G * 0.02:
                    self.zero_g, self.tare_g = self.load_g, 0.0
                    self._settled_at = self.now() + 0.5
            case "Z":
                if self.internal_weight:
                    self._adjusting_until = self.now() + self.adjust_s
                    self.adjustments += 1
            case "K" | "L" | "M" | "N":
                self.ambient = cmd
            case "O":
                self.keys_locked = True
            case "R":
                self.keys_locked = False
            case "x1_":
                return "QUINTIX224-1S"
            case "x2_":
                return "0037402012"
            case "x3_":
                return "00-20-12.01"
        return None
