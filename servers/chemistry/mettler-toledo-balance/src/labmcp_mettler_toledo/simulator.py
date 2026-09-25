"""Wire-level MT-SICS simulator of an analytical balance (220 g x 0.1 mg)."""

from __future__ import annotations

import random

from labmcp import LineSimulator

CAPACITY_G = 220.0


class MTSICSSimulator(LineSimulator):
    def __init__(self, load_g: float = 52.18734, seed: int | None = 0) -> None:
        self.rng = random.Random(seed)
        self.load_g = load_g  # what is physically on the pan
        self.zero_g = 0.0
        self.tare_g = 0.0
        self.door = 0
        self.id = "Sim Balance"

    # helpers ---------------------------------------------------------------

    def _gross(self) -> float:
        return self.load_g - self.zero_g

    def _net(self) -> float:
        return self._gross() - self.tare_g

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{value:.4f}".rjust(10) + " g"

    def _noisy(self, value: float) -> float:
        return value + self.rng.gauss(0, 0.00004)

    # protocol --------------------------------------------------------------

    def handle(self, command: str) -> str | list[str] | None:
        cmd = command.strip()
        head, _, arg = cmd.partition(" ")
        if self.load_g > CAPACITY_G * 1.05 and head in {"S", "SI", "T", "TI", "Z", "ZI"}:
            return f"{'S' if head in {'S', 'SI'} else head} +"
        match head:
            case "@":
                self.tare_g = 0.0
                return 'I4 A "B123456789"'
            case "I1":
                return 'I1 A "0123" "2.30" "2.20" "1.00" ""'
            case "I2":
                return f'I2 A "XS205DU Excellence {CAPACITY_G:.4f} g"'
            case "I3":
                return 'I3 A "3.10 10.28.0.493.142"'
            case "I4":
                return 'I4 A "B123456789"'
            case "I11":
                return 'I11 A "XS205DU"'
            case "I10":
                if arg:
                    self.id = arg.strip('"')
                    return "I10 A"
                return f'I10 A "{self.id}"'
            case "S":
                return "S S " + self._fmt(self._net())
            case "SI":
                return "S D " + self._fmt(self._noisy(self._net()))
            case "Z":
                self.zero_g = self.load_g
                self.tare_g = 0.0
                return "Z A"
            case "ZI":
                self.zero_g = self.load_g
                self.tare_g = 0.0
                return "ZI S"
            case "T":
                self.tare_g = self._gross()
                return "T S " + self._fmt(self.tare_g)
            case "TI":
                self.tare_g = self._gross()
                return "TI S " + self._fmt(self.tare_g)
            case "TA":
                if arg:
                    value, _, unit = arg.partition(" ")
                    if unit.strip() != "g":
                        return "TA L"
                    self.tare_g = round(float(value), 4)
                return "TA A " + self._fmt(self.tare_g)
            case "TAC":
                self.tare_g = 0.0
                return "TAC A"
            case "D":
                return "D A"
            case "DW":
                return "DW A"
            case "C3":
                return ["C3 B", "C3 A"]
            case "WS":
                if arg:
                    if arg not in {"0", "1", "2"}:
                        return "WS L"
                    self.door = int(arg)
                    return "WS A"
                return f"WS A {self.door}"
            case "M28":
                return ["M28 B 1 21.8", "M28 A 2 22.1"]
        return "ES"
