"""Wire-level simulator of an Alicat mass flow controller (default: MC-500SCCM-D, 10v20).

Reply formats follow replies captured on real hardware (see the driver docstring): the
``??M*`` table, the column-aligned ``??D*`` table (and the older pre-6v dialect), the
``LS`` reply ``A +078.94 +078.94 12 SCCM`` and data frames such as
``A +014.62 +021.89 +000.00 +000.00 +078.95     N2``.

The physics is deliberately simple but plausible: mass flow follows the setpoint with a
first-order response (tau ~0.15 s), the sensor has a small zero offset until it is tared,
pressure sags slightly with flow and readings carry a little noise.
"""

from __future__ import annotations

import math
import random
import time

from labmcp import LineSimulator

_GAS_LONG = {0: "Air", 1: "Argon", 2: "Methane", 4: "Carbon Dioxide", 6: "Hydrogen", 7: "Helium",
             8: "Nitrogen", 11: "Oxygen"}
_GAS_SHORT = {0: "Air", 1: "Ar", 2: "CH4", 4: "CO2", 6: "H2", 7: "He", 8: "N2", 11: "O2"}

_STATUS_ROWS = ["OPL", "POV", "P2O", "TOV", "VOV", "MOV", "TMF", "OVR", "COM", "HLD", "EXH", "GTA", "LCK"]


class AlicatSimulator(LineSimulator):
    def __init__(
        self,
        *,
        unit_id: str = "A",
        model: str = "MC-500SCCM-D",
        firmware: str = "10v20.0-R24",
        full_scale: float = 500.0,
        controller: bool = True,
        layout_dialect: str = "canonical",  # "canonical" | "legacy" | "none"
        barometer: bool = True,
        gas: int = 8,
        zero_offset: float = 0.9,
        tau_s: float = 0.15,
        seed: int | None = 0,
    ) -> None:
        self.unit_id = unit_id
        self.model = model
        self.firmware = firmware
        self.full_scale = full_scale
        self.controller = controller
        self.layout_dialect = layout_dialect
        self.barometer = barometer
        self.gas = gas
        self.zero_offset = zero_offset  # sensor offset (SCCM) until tared
        self.tau_s = tau_s
        self.rng = random.Random(seed)
        self.setpoint = 0.0
        self.hold: str | None = None  # None | "position" | "closed"
        self.true_flow = 0.0
        self.upstream_psia = 14.62
        self.temperature_c = 21.9
        self.tare_offset = 0.0
        self._t = time.monotonic()

    # ------------------------------------------------------------- physics

    @property
    def major(self) -> tuple[int, int]:
        major, _, minor = self.firmware.partition("v")
        return int(major), int(minor[:2])

    def _advance(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        if self.hold == "closed":
            target = 0.0
        elif self.hold == "position":
            target = self.true_flow
        else:
            target = self.setpoint if self.controller else self.true_flow
        target = max(-self.full_scale, min(self.full_scale, target))
        self.true_flow = target + (self.true_flow - target) * math.exp(-dt / self.tau_s)

    def _mass_flow_reading(self) -> float:
        noise = self.rng.gauss(0, self.full_scale * 0.0002)
        return self.true_flow + self.zero_offset - self.tare_offset + noise

    def _pressure(self) -> float:
        return self.upstream_psia - 0.002 * abs(self.true_flow) / self.full_scale * 100 + self.rng.gauss(0, 0.003)

    # ------------------------------------------------------------- formatting

    def _frame(self) -> str:
        self._advance()
        mass = self._mass_flow_reading()
        pressure = self._pressure()
        temp = self.temperature_c + self.rng.gauss(0, 0.01)
        vol = mass * (temp + 273.15) / 298.15 * 14.696 / pressure
        parts = [self.unit_id, f"{pressure:+07.2f}", f"{temp:+07.2f}", f"{vol:+07.2f}", f"{mass:+07.2f}"]
        if self.controller:
            parts.append(f"{self.setpoint:+07.2f}")
        parts.append(f"{_GAS_SHORT.get(self.gas, 'Mix'):>6}")
        line = " ".join(parts)
        if self.hold:
            line += " HLD"
        if abs(mass) > self.full_scale * 1.28:
            line += " MOV"
        return line

    def _layout(self) -> list[str]:
        uid = self.unit_id
        if self.layout_dialect == "legacy":
            rows = [
                f"{uid}  D00 NAME_______ TYPE_____ MinVal_  MaxVal_  UNITS__",
                f"{uid}  D01 Unit ID     char         A         Z         na",
                f"{uid}  D02 Pressure    signed    +000.00  +160.00     PSIA",
                f"{uid}  D03 Temperature signed    -010.00  +050.00        C",
                f"{uid}  D04 Volumetric  signed    +0000.0  +{self.full_scale:06.1f}      CCM",
                f"{uid}  D05 Mass        signed    +0000.0  +{self.full_scale:06.1f}     SCCM",
            ]
            if self.controller:
                rows.append(f"{uid}  D06 SetPoint    signed    +0000.0  +{self.full_scale:06.1f}     SCCM")
            n = len(rows)
            rows.append(f"{uid}  D{n:02d} Gas         string        Air       D2       na")
            for i, code in enumerate(["ADC", "LCK", "OVR", "POV", "TOV", "VOV", "MOV", "HLD"]):
                label = "Error" if i == 0 else "Status"
                rows.append(f"{uid}  D{n + 1 + i:02d} {label:<11} string         na      {code}       na")
            return rows
        header = f"{uid} D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________"
        fields = [
            ("700", "Unit ID", "string", "1", ""),
            ("002", "Abs Press", "s decimal", "7/2", "010 02 PSIA"),
            ("003", "Flow Temp", "s decimal", "7/2", "002 02 `C"),
            ("004", "Volu Flow", "s decimal", "7/2", "012 02 CCM"),
            ("005", "Mass Flow", "s decimal", "7/2", "012 02 SCCM"),
        ]
        if self.controller:
            fields.append(("037", "Mass Flow Setpt", "s decimal", "7/2", "012 02 SCCM"))
        fields.append(("703", "Gas", "string", "6", ""))
        fields.append(("701", "*Error", "string", "3", "ADC"))
        fields += [("702", "*Status", "string", "3", code) for code in _STATUS_ROWS]
        rows = [header]
        for i, (code, name, typ, width, notes) in enumerate(fields, start=1):
            rows.append(f"{uid} D{i:02d} {code} {name:<26} {typ:<11} {width:>5} {notes}".rstrip())
        rows.append("")  # real devices end the table with an empty line
        return rows

    def _manufacturing(self) -> list[str]:
        uid = self.unit_id
        return [
            f"{uid}  M00 ALICAT SCIENTIFIC",
            f"{uid}  M01 www.alicat.com",
            f"{uid}  M02 Ph   520-290-6060",
            f"{uid}  M03 info@alicat.com",
            f"{uid}  M04 Model Number {self.model}",
            f"{uid}  M05 Serial Number 521641",
            f"{uid}  M06 Date Manufactured 03/02/2025",
            f"{uid}  M07 Date Calibrated   03/02/2025",
            f"{uid}  M08 Calibrated By     BL",
            f"{uid}  M09 Software Revision {self.firmware}",
        ]

    # ------------------------------------------------------------- protocol

    def handle(self, command: str) -> str | list[str] | None:
        cmd = command.strip()
        if not cmd or cmd[0].upper() != self.unit_id:
            return None  # addressed to another device on the bus
        body = cmd[1:].strip()
        head, _, arg = body.partition(" ")
        head = head.upper()
        arg = arg.strip()
        uid = self.unit_id
        controller_only = {"S", "LS", "HP", "HC", "C", "LSS"}
        if head in controller_only and not self.controller:
            return None  # meters silently ignore controller commands
        match head:
            case "":
                return self._frame()
            case "??M*":
                return self._manufacturing()
            case "??D*":
                return "?" if self.layout_dialect == "none" else self._layout()
            case "??G*":
                return [f"{uid} G{n:02d} {name:>8}" for n, name in sorted(_GAS_SHORT.items())]
            case "VE":
                return f"{uid}   {self.firmware} Jan  9 2025,15:04:07"
            case "LS":
                if self.major < (9, 0):
                    return "?"
                if arg:
                    self._set(float(arg))
                return f"{uid} {self.setpoint:+07.2f} {self.setpoint:+07.2f} 12 SCCM"
            case "S":
                self._set(float(arg))
                return self._frame()
            case "LSS":
                return f"{uid} S" if self.major >= (10, 5) else "?"
            case "GS":
                if self.major < (10, 5):
                    return "?"
                if arg:
                    number = int(arg.split()[0])
                    if number not in _GAS_SHORT:
                        return "?"
                    self.gas = number
                return f"{uid} {self.gas} {_GAS_SHORT[self.gas]} {_GAS_LONG[self.gas]}"
            case "G":
                number = int(arg)
                if number not in _GAS_SHORT:
                    return "?"
                self.gas = number
                return self._frame()
            case "V":
                self._advance()
                self.tare_offset = self.true_flow + self.zero_offset  # a tare with flow is wrong!
                return self._frame()
            case "P":
                return "?"  # this simulated MFC has no gauge-pressure sensor
            case "PC":
                return self._frame() if self.barometer else "?"
            case "HP":
                self._advance()
                self.hold = "position"
                return self._frame()
            case "HC":
                self._advance()
                self.hold = "closed"
                return self._frame()
            case "C":
                self._advance()
                self.hold = None
                return self._frame()
            case "FPF":
                return f"{uid} +{self.full_scale:06.2f} 12 SCCM"
        return "?"

    def _set(self, value: float) -> None:
        self._advance()
        # "The setpoint is limited to the controller range limits" (Serial Primer p. 12).
        self.setpoint = max(0.0, min(self.full_scale, value))
