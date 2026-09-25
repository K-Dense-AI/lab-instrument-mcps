"""Wire-level SCPI simulator of a Thorlabs PM100D with a C-series sensor and a HeNe laser.

Physics that is modelled (so demos behave like the real bench):

* A photodiode meter measures photocurrent and divides by the responsivity R(λ) at the
  *configured* wavelength. The simulated laser is a 632.8 nm HeNe, so setting the wrong
  wavelength on the S120C (Si photodiode) gives a proportionally wrong reading, exactly as on
  hardware. The thermopile (S302C) response is spectrally flat.
* Laser intensity noise (0.05 % per 1 ms sample, averaged down by ``SENS:AVER``), a slow
  ±0.2 % drift and a dark/background offset that ``SENS:CORR:COLL:ZERO`` removes. The
  simulated zero assumes the scientist covered the sensor.
* Power ranges, auto-ranging and over-range (+9.9E37) replies.

Replies follow the manuals cited in :mod:`labmcp_thorlabs_pm.driver`. Errors for out-of-range
parameters use the standard SCPI code -222 "Data out of range" and missing sensor hardware uses
-241 "Hardware missing" (the Thorlabs documentation only lists -113 "Undefined header"
explicitly).
"""

from __future__ import annotations

import math
import random
import time

from labmcp.scpi import SCPISimulator

SENSORS = {
    # name: type, subtype, flags, wavelength range (nm), power ranges (W, most sensitive first)
    "S120C": {
        "serial": "16060232",
        "cal": "2-JUN-2021",
        "type": 1,
        "subtype": 2,
        "flags": 1 | 32,
        "wl": (400, 1100),
        "ranges": [5e-8, 5e-7, 5e-6, 5e-5, 5e-4, 5e-3, 5e-2],
        "offset": 1.2e-8,  # dark + ambient photocurrent, A
    },
    "S302C": {
        "serial": "19051104",
        "cal": "14-MAR-2022",
        "type": 2,
        "subtype": 18,
        "flags": 1 | 32 | 64 | 256,
        "wl": (190, 25000),
        "ranges": [2e-3, 2e-2, 2e-1, 2.0],
        "offset": 4e-6,  # thermopile offset voltage, V
    },
}

LASER_NM = 632.8
LASER_W = 1.234e-3


def si_responsivity(wavelength_nm: float) -> float:
    """Approximate responsivity (A/W) of a Si photodiode."""
    qe = (
        0.85
        / (1.0 + math.exp(-(wavelength_nm - 420.0) / 35.0))
        / (1.0 + math.exp((wavelength_nm - 1040.0) / 25.0))
    )
    return wavelength_nm / 1239.84 * qe


class ThorlabsPMSimulator(SCPISimulator):
    def __init__(self, sensor: str = "S120C", seed: int | None = 0, laser_w: float = LASER_W) -> None:
        super().__init__()
        if sensor not in SENSORS:
            raise ValueError(f"Unknown simulated sensor {sensor!r}; choose from {sorted(SENSORS)}")
        self.idn = "THORLABS,PM100D,P0012345,2.8.0"
        self.rng = random.Random(seed)
        self.sensor_name = sensor
        self.sensor = SENSORS[sensor]
        self.laser_w = laser_w  # what actually reaches the sensor
        self.t0 = time.monotonic()
        self.reset()

    def reset(self) -> None:
        self.wavelength = 633.0
        self.averaging = 1
        self.auto_range = True
        self.range_index = len(self.sensor["ranges"]) - 1
        self.unit = "W"
        self.zero = 0.0
        self._zero_until = 0.0

    # physics ---------------------------------------------------------------

    def _gain(self, wavelength_nm: float) -> float:
        """Signal per watt (A/W for photodiodes, V/W for thermopiles)."""
        return si_responsivity(wavelength_nm) if self.sensor["type"] == 1 else 0.25

    def _true_power(self) -> float:
        t = time.monotonic() - self.t0
        drift = 1.0 + 0.002 * math.sin(2 * math.pi * t / 120.0)
        noise = self.rng.gauss(0.0, 5e-4 / math.sqrt(self.averaging))
        return self.laser_w * drift * (1.0 + noise)

    def _signal(self) -> float:
        """Raw detector signal (A or V) including the dark offset."""
        return self._true_power() * self._gain(LASER_NM) + self.sensor["offset"]

    def _reading_w(self) -> float:
        return (self._signal() - self.zero) / self._gain(self.wavelength)

    def _range_for(self, power_w: float) -> int:
        ranges = self.sensor["ranges"]
        for i, top in enumerate(ranges):
            if abs(power_w) <= top:
                return i
        return len(ranges) - 1

    # protocol --------------------------------------------------------------

    def command(self, key: str, arg: str) -> str | None:
        m = self.matches
        s = self.sensor
        arg_u = arg.strip().upper()

        if m(key, "SYSTem:SENSor<n>:IDN?"):
            return (
                f'"{self.sensor_name}","{s["serial"]}","{s["cal"]}",{s["type"]},{s["subtype"]},{s["flags"]}'
            )

        if m(key, "MEASure<n>[:SCALar][:POWer]?") or m(key, "READ?"):
            power = self._reading_w()
            if self.auto_range:
                self.range_index = self._range_for(power)
            if power > s["ranges"][self.range_index] * 1.1:
                return "9.9E+37"
            if self.unit == "DBM":
                return f"{10 * math.log10(max(power, 1e-15) / 1e-3):.6E}"
            return f"{power:.6E}"
        if m(key, "MEASure<n>[:SCALar]:TEMPerature?"):
            if not s["flags"] & 256:
                self.error_queue.append('-241,"Hardware missing"')
                return None
            return f"{24.1 + self.rng.gauss(0, 0.02):.2f}"

        if m(key, "[SENSe<n>:]POWer[:DC]:UNIT"):
            if arg_u not in {"W", "DBM"}:
                raise ValueError(arg)
            self.unit = arg_u
            return None
        if m(key, "[SENSe<n>:]POWer[:DC]:UNIT?"):
            return self.unit

        if m(key, "[SENSe<n>:]CORRection:WAVelength"):
            lo, hi = s["wl"]
            value = float(arg)
            if not lo <= value <= hi:
                self.error_queue.append('-222,"Data out of range"')
                return None
            self.wavelength = round(value)
            return None
        if m(key, "[SENSe<n>:]CORRection:WAVelength?"):
            lo, hi = s["wl"]
            value = {"MIN": lo, "MAX": hi}.get(arg_u, self.wavelength)
            return f"{float(value):.6E}"

        if m(key, "[SENSe<n>:]AVERage[:COUNt]"):
            count = int(float(arg))
            if count < 1:
                raise ValueError(arg)
            self.averaging = count
            return None
        if m(key, "[SENSe<n>:]AVERage[:COUNt]?"):
            return str(self.averaging)

        if m(key, "[SENSe<n>:]POWer[:DC]:RANGe:AUTO"):
            self.auto_range = arg_u in {"1", "ON"}
            return None
        if m(key, "[SENSe<n>:]POWer[:DC]:RANGe:AUTO?"):
            return "1" if self.auto_range else "0"
        if m(key, "[SENSe<n>:]POWer[:DC]:RANGe[:UPPer]"):
            ranges = s["ranges"]
            if arg_u.startswith("MIN"):
                self.range_index = 0
            elif arg_u.startswith("MAX"):
                self.range_index = len(ranges) - 1
            else:
                value = float(arg)
                if value <= 0 or value > ranges[-1] * 1.1:
                    self.error_queue.append('-222,"Data out of range"')
                    return None
                self.range_index = self._range_for(value)
            self.auto_range = False
            return None
        if m(key, "[SENSe<n>:]POWer[:DC]:RANGe[:UPPer]?"):
            ranges = s["ranges"]
            value = {"MIN": ranges[0], "MAX": ranges[-1]}.get(arg_u[:3], ranges[self.range_index])
            return f"{value:.6E}"

        if m(key, "[SENSe<n>:]CORRection:COLLect:ZERO[:INITiate]"):
            # Assumes the sensor aperture was covered: only the dark offset is measured.
            self.zero = s["offset"] + self.rng.gauss(0.0, s["offset"] * 0.01)
            self._zero_until = time.monotonic() + 0.6
            return None
        if m(key, "[SENSe<n>:]CORRection:COLLect:ZERO:ABORt"):
            self._zero_until = 0.0
            return None
        if m(key, "[SENSe<n>:]CORRection:COLLect:ZERO:STATe?"):
            return "1" if time.monotonic() < self._zero_until else "0"
        if m(key, "[SENSe<n>:]CORRection:COLLect:ZERO:MAGNitude?"):
            return f"{self.zero:.6E}"

        raise self.undefined()
