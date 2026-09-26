"""Wire-level simulator of an SRS RGA200 with the electron multiplier (CDEM) option.

Reproduces the RGA command set byte for byte (see ``driver.py`` for the manual reference):
CR-terminated commands, ``value<LF><CR>`` ASCII replies, the STATUS byte echoed by hardware
commands, silence for parameter-setting commands and for rejected commands (which set RS232_ERR
bits instead), and binary 4-byte little-endian ion currents in units of 1e-16 A.

The vacuum is an unbaked stainless chamber at ~1e-7 Torr (H2O, H2, N2, CO, CO2, O2, Ar, CH4 with
70 eV fragment patterns). Scenarios (``scenario=``):

* ``"normal"``: steady background.
* ``"helium_leak"``: a helium leak that someone is spraying: He (m/z 4) pulses every 20 s.
* ``"overpressure"``: a growing leak; the pressure rises one decade every 10 s after 15 s, and the
  filament protection trips the emission (FIL_ERR FL6, CDEM off), as the real unit does.

Tests can also call :meth:`RGASimulator.set_pressure_scale` / set ``helium_torr`` directly.
"""

from __future__ import annotations

import math
import random
import struct
import time

from labmcp import ByteSimulator

# 70 eV fragment patterns (base peak = 1), typical literature values.
FRAGMENTS: dict[str, dict[int, float]] = {
    "H2": {2: 1.0, 1: 0.05},
    "He": {4: 1.0},
    "CH4": {16: 1.0, 15: 0.86, 14: 0.16, 13: 0.08, 12: 0.03},
    "H2O": {18: 1.0, 17: 0.23, 16: 0.011, 20: 0.002},
    "N2": {28: 1.0, 14: 0.07, 29: 0.007},
    "CO": {28: 1.0, 12: 0.047, 16: 0.017, 29: 0.012},
    "O2": {32: 1.0, 16: 0.11, 34: 0.004},
    "Ar": {40: 1.0, 20: 0.15, 36: 0.003},
    "CO2": {44: 1.0, 28: 0.11, 16: 0.09, 12: 0.087, 22: 0.019, 45: 0.012},
}
# Ionisation sensitivity relative to N2.
REL_SENS = {
    "H2": 0.44,
    "He": 0.14,
    "CH4": 1.6,
    "H2O": 1.0,
    "N2": 1.0,
    "CO": 1.05,
    "O2": 0.86,
    "Ar": 1.2,
    "CO2": 1.4,
}

BACKGROUND_TORR = {
    "H2O": 6.0e-8,
    "H2": 1.5e-8,
    "N2": 1.0e-8,
    "CO": 5.0e-9,
    "CO2": 3.0e-9,
    "O2": 1.0e-9,
    "CH4": 6.0e-10,
    "Ar": 1.2e-10,
}
AIR = {"N2": 0.78, "O2": 0.21, "Ar": 0.0093, "CO2": 0.0004, "H2O": 0.01}

NF_NOISE_A = (7e-15, 1e-14, 1.5e-14, 2e-14, 4e-14, 1.2e-13, 2.5e-13, 5e-13)
PEAK_SIGMA_AMU = 0.24  # ~1 amu wide at 10 % height
FILAMENT_TRIP_TORR = 2e-4  # emission can no longer be regulated above roughly this pressure


class RGASimulator(ByteSimulator):
    def __init__(
        self,
        model: int = 200,
        serial: str = "19045",
        firmware: str = "0.51",
        has_cdem: bool = True,
        scenario: str = "normal",
        seed: int | None = 0,
        time_scale: float = 1.0,
    ) -> None:
        self.rng = random.Random(seed)
        self.m_max = model
        self.id_string = f"SRSRGA{model}VER{firmware}SN{serial}"
        self.has_cdem = has_cdem
        self.scenario = scenario
        self.time_scale = time_scale  # simulated seconds per real second (tests speed up degas)
        self.t0 = time.monotonic()
        self._rx = b""
        self.log: list[str] = []
        # ionizer / detector state (power-on defaults, manual IN1 / FL / HV descriptions)
        self.emission_ma = 0.0
        self.ee, self.ie, self.vf = 70, 1, 90
        self.hv = 0
        self.nf, self.mi, self.mf, self.sa = 4, 1, model, 10
        self.tp_flag = True
        self.rf_mass: float = 0.0
        self.sp, self.st, self.mg, self.mv = 0.1033, 0.0197, 1.25, 1400.0
        # error bytes
        self.rs232_err = self.fil_err = self.cem_err = self.qmf_err = self.det_err = self.ps_err = 0
        # extra gas
        self.pressure_scale = 1.0
        self.helium_torr = 0.0
        self.air_leak_torr = 0.0
        self.degas_until: float | None = None
        self._pending: bytes = b""

    # ------------------------------------------------------------ physics

    def now(self) -> float:
        return (time.monotonic() - self.t0) * self.time_scale

    def set_pressure_scale(self, scale: float) -> None:
        """Multiply the whole background (e.g. 1e4 to simulate a vent)."""
        self.pressure_scale = scale

    def partial_pressures(self) -> dict[str, float]:
        t = self.now()
        gases = {g: p * self.pressure_scale for g, p in BACKGROUND_TORR.items()}
        air = self.air_leak_torr
        if self.scenario == "overpressure" and t > 15:
            air += 1e-7 * 10 ** ((t - 15) / 10.0)
        for g, frac in AIR.items():
            gases[g] = gases.get(g, 0.0) + air * frac
        he = self.helium_torr
        if self.scenario == "helium_leak":
            phase = t % 20.0
            if phase > 5:  # spray at 5 s into every 20 s cycle, fast rise, ~3 s pump-out
                he += 1e-7 * (1 - math.exp(-(phase - 5) / 0.8)) * math.exp(-max(0.0, phase - 9) / 3.0)
            he += 2e-12  # atmospheric-helium background
        gases["He"] = he
        if self.degas_until is not None:
            gases["H2O"] *= 3.0
            gases["H2"] *= 5.0
        return gases

    def total_pressure_torr(self) -> float:
        return sum(self.partial_pressures().values())

    def _check_filament(self) -> None:
        """Background filament protection: overpressure shuts down emission and the CDEM."""
        if self.emission_ma > 0 and self.total_pressure_torr() > FILAMENT_TRIP_TORR:
            self.emission_ma = 0.0
            self.fil_err |= (1 << 6) | (1 << 5)
            self.hv = 0
            if self.degas_until is not None:
                self.degas_until = None
                self._pending += self._status_bytes()

    def _gain(self) -> float:
        if self.hv <= 0:
            return 1.0
        return max(1.0, 1000.0 * 10 ** ((self.hv - 1400) / 350.0))

    def _ion_current_a(self, mass: float) -> float:
        """Ion current at a (possibly fractional) mass setting, with noise."""
        signal = 0.0
        if self.emission_ma > 0:
            s = self.sp * 1e-3 * self.emission_ma / 1.0 * (self.ee / 70.0) ** 0.5
            for gas, p in self.partial_pressures().items():
                for m, frac in FRAGMENTS[gas].items():
                    d = mass - m
                    if abs(d) < 1.5:
                        signal += p * s * REL_SENS[gas] * frac * math.exp(-0.5 * (d / PEAK_SIGMA_AMU) ** 2)
        noise = self.rng.gauss(0.0, NF_NOISE_A[self.nf])
        if self.hv > 0:
            return signal * self._gain() + noise * 3.0
        return signal + noise

    def _peak_locked(self, mass: int) -> float:
        return max(self._ion_current_a(mass + (k - 3) * 0.1) for k in range(7))

    def _total_current_a(self) -> float:
        if not self.tp_flag:
            return 0.0
        signal = 0.0
        if self.emission_ma > 0:
            s = self.st * 1e-3 * self.emission_ma
            signal = sum(p * s * REL_SENS[g] for g, p in self.partial_pressures().items())
        return signal + self.rng.gauss(0.0, NF_NOISE_A[self.nf])

    @staticmethod
    def _pack(current_a: float) -> bytes:
        raw = int(round(current_a / 1e-16))
        raw = max(-(2**31), min(2**31 - 1, raw))
        return struct.pack("<i", raw)

    # ------------------------------------------------------------ protocol helpers

    def status(self) -> int:
        s = 0
        if self.rs232_err:
            s |= 1 << 0
        if self.fil_err & 0xFE:
            s |= 1 << 1
        if self.cem_err:
            s |= 1 << 3
        if self.qmf_err:
            s |= 1 << 4
        if self.det_err:
            s |= 1 << 5
        if self.ps_err:
            s |= 1 << 6
        return s

    @staticmethod
    def _text(value: object) -> bytes:
        return f"{value}\n\r".encode("ascii")

    def _status_bytes(self) -> bytes:
        return self._text(self.status())

    def _bad_param(self) -> bytes:
        self.rs232_err |= 1 << 1
        return b""

    def _bad_command(self) -> bytes:
        self.rs232_err |= 1 << 0
        return b""

    @staticmethod
    def _number(param: str) -> float | None:
        try:
            return float(param)
        except ValueError:
            return None

    def _int_param(self, param: str, lo: int, hi: int) -> int | None:
        v = self._number(param)
        if v is None or v != int(v) or not lo <= v <= hi:
            return None
        return int(v)

    # ------------------------------------------------------------ transport hooks

    def poll(self) -> bytes | None:
        self._check_filament()
        if self.degas_until is not None and self.now() >= self.degas_until:
            self.degas_until = None
            self._pending += self._status_bytes()
        out, self._pending = self._pending, b""
        return out or None

    def handle_bytes(self, data: bytes) -> bytes:
        self._rx += data
        out = b""
        while b"\r" in self._rx:
            line, _, self._rx = self._rx.partition(b"\r")
            text = line.decode("ascii", "replace").replace("\n", "").strip()
            if not text:  # single CRs and LFs are ignored (manual p. 6-7)
                continue
            if len(text) > 13:
                self.rs232_err |= 1 << 2  # command too long
                continue
            self.log.append(text)
            if self.degas_until is not None:
                self.degas_until = None  # any command aborts the degas without a STATUS echo
            self._check_filament()
            out += self.execute(text[:2].upper(), text[2:].strip())
        return out

    # ------------------------------------------------------------ command set

    def execute(self, name: str, p: str) -> bytes:  # noqa: C901 - one branch per command
        mmax = self.m_max
        if name == "ID":
            return self._text(self.id_string) if p == "?" else self._bad_param()
        if name == "IN":
            level = self._int_param(p, 0, 2)
            if level is None:
                return self._bad_param()
            self.rs232_err = 0
            if level >= 1:
                self.tp_flag = True
                self.mi, self.mf, self.sa, self.nf = 1, mmax, 10, 4
                self.ie, self.ee, self.vf = 1, 70, 90
            if level == 2:
                self.emission_ma = 0.0
                self.hv = 0
            return self._status_bytes()
        if name == "DG":
            minutes = 3 if p == "*" else self._int_param(p, 0, 20)
            if minutes is None:
                return self._bad_param()
            if minutes == 0:
                return b""
            if self.fil_err & 0xFE:
                return self._status_bytes()
            self.hv = 0
            self.degas_until = self.now() + minutes * 60.0
            return b""
        if name in ("EE", "IE", "VF"):
            lo, hi, default = {"EE": (25, 105, 70), "IE": (0, 1, 1), "VF": (0, 150, 90)}[name]
            attr = name.lower()
            if p == "?":
                return self._text(getattr(self, attr))
            value = default if p == "*" else self._int_param(p, lo, hi)
            if value is None:
                return self._bad_param()
            setattr(self, attr, value)
            return self._status_bytes()
        if name == "FL":
            if p == "?":
                shown = self.emission_ma + (self.rng.uniform(-0.01, 0.01) if self.emission_ma else 0.0)
                return self._text(f"{shown:.2f}")
            value = 1.0 if p == "*" else self._number(p)
            if value is None or not (value == 0 or 0.02 <= value <= 3.5):
                return self._bad_param()
            if value == 0:
                self.emission_ma = 0.0
                return self._status_bytes()
            if self.total_pressure_torr() > FILAMENT_TRIP_TORR:
                self.emission_ma = 0.0
                self.hv = 0
                self.fil_err |= (1 << 6) | (1 << 5)
            else:
                self.emission_ma = round(value, 2)
                self.fil_err = 0  # cleared once emission is established
            return self._status_bytes()
        if name == "CA" or name == "CL":
            if p:
                return self._bad_param()
            if name == "CA":
                self.rf_mass = 0.0
            return self._status_bytes()
        if name == "HV":
            if not self.has_cdem:
                return self._bad_command()
            if p == "?":
                return self._text(f"{self.hv - 1 if self.hv else 0}")
            value = 1400 if p == "*" else self._int_param(p, 0, 2490)
            if value is None or 0 < value < 10:
                return self._bad_param()
            self.hv = value
            self.tp_flag = value == 0  # HV0 sets TP_Flag, CDEM on clears it
            return self._status_bytes()
        if name == "MO":
            return self._text(1 if self.has_cdem else 0) if p == "?" else self._bad_param()
        if name in ("NF", "SA", "MI", "MF"):
            attr = name.lower()
            if p == "?":
                return self._text(getattr(self, attr))
            lo, hi, default = {
                "NF": (0, 7, 4),
                "SA": (10, 25, 10),
                "MI": (1, mmax, 1),
                "MF": (1, mmax, mmax),
            }[name]
            value = default if p == "*" else self._int_param(p, lo, hi)
            if value is None:
                return self._bad_param()
            if (name == "MI" and value > self.mf) or (name == "MF" and value < self.mi):
                self.rs232_err |= 1 << 6  # parameter conflict
                return b""
            setattr(self, attr, value)
            return b""
        if name == "AP":
            return self._text((self.mf - self.mi) * self.sa + 1) if p == "?" else self._bad_param()
        if name == "HP":
            return self._text(self.mf - self.mi + 1) if p == "?" else self._bad_param()
        if name in ("SC", "HS"):
            count = 1 if p in ("", "*") else self._int_param(p, 0, 255)  # continuous: one scan here
            if count is None:
                return self._bad_param()
            out = b""
            for _ in range(count):
                if name == "SC":
                    n = (self.mf - self.mi) * self.sa + 1
                    out += b"".join(self._pack(self._ion_current_a(self.mi + i / self.sa)) for i in range(n))
                else:
                    out += b"".join(self._pack(self._peak_locked(m)) for m in range(self.mi, self.mf + 1))
                out += self._pack(self._total_current_a())
            return out
        if name == "MR":
            mass = self._int_param(p, 0, mmax)
            if mass is None:
                return self._bad_param()
            if mass == 0:
                self.rf_mass = 0.0
                return b""
            self.rf_mass = mass + 0.3
            return self._pack(self._peak_locked(mass))
        if name == "TP":
            if p == "?":
                return self._pack(self._total_current_a())
            flag = self._int_param(p, 0, 1)
            if flag is None:
                return self._bad_param()
            self.tp_flag = bool(flag)
            return b""
        if name in ("SP", "ST", "MG", "MV"):
            if name in ("MG", "MV") and not self.has_cdem:
                return self._bad_command()
            attr = name.lower()
            if p == "?":
                return self._text(f"{getattr(self, attr):.4f}")
            hi = {"SP": 10.0, "ST": 100.0, "MG": 2000.0, "MV": 2490.0}[name]
            value = self._number(p)
            if value is None or not 0 <= value <= hi:
                return self._bad_param()
            setattr(self, attr, round(value, 4))
            return b""
        if name == "ML":
            value = self._number(p)
            if value is None or not 0 <= value <= mmax:
                return self._bad_param()
            self.rf_mass = value
            return b""
        if name == "CE":
            return self._text(0) if p == "?" else self._bad_param()
        if name in ("ER", "EP", "ED", "EQ", "EM", "EF", "EC"):
            if p != "?":
                return self._bad_param()
            if name == "ER":
                return self._text(self.status())
            if name == "EC":
                value, self.rs232_err = self.rs232_err, 0
                return self._text(value)
            if name == "EM":
                value = self.cem_err | (0 if self.has_cdem else 1 << 7)
                self.cem_err = 0
                return self._text(value)
            attr = {"EP": "ps_err", "ED": "det_err", "EQ": "qmf_err", "EF": "fil_err"}[name]
            return self._text(getattr(self, attr))
        if name in ("DI", "DS", "RI", "RS"):
            return self._text({"DI": 128, "DS": 0.0, "RI": -9.5, "RS": 1071.2}[name]) if p == "?" else b""
        return self._bad_command()
