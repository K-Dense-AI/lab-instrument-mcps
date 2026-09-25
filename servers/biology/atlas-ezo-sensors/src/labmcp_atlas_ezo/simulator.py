"""Wire-level simulator of an Atlas Scientific EZO circuit in UART mode.

Reproduces the reply formats from the EZO datasheets: data line(s) followed by ``*OK``,
``*ER`` for unknown commands, ``?Cmd,value`` queries (including the DO quirk ``?,P,90.25``),
and continuous mode ON at power-up, which interleaves a reading with command replies until
``C,0`` is sent. The simulated "sample" is a pH 7.2 culture medium at 25 °C, a 1413 µS/cm
conductivity standard, air-saturated water, etc., with small noise and drift, and each
calibration step reduces the reading error the way a real probe calibration would.
"""

from __future__ import annotations

import math
import random
import time

from labmcp import LineSimulator

_FIRMWARE = {"PH": "2.16", "ORP": "1.97", "DO": "1.98", "EC": "2.16", "RTD": "2.01", "HUM": "1.0",
             "CO2": "1.0", "PRS": "1.0"}
_LABEL = {"PH": "pH", "ORP": "ORP", "DO": "D.O.", "EC": "EC", "RTD": "RTD", "HUM": "HUM", "CO2": "CO2",
          "PRS": "PRS"}


def _do_saturation_mg_l(t_c: float) -> float:
    """Oxygen solubility in fresh water at 1 atm (polynomial fit to Benson & Krause)."""
    return 14.652 - 0.41022 * t_c + 0.007991 * t_c**2 - 0.000077774 * t_c**3


class EZOSimulator(LineSimulator):
    def __init__(self, sensor: str = "pH", *, seed: int | None = 0) -> None:
        key = sensor.replace(".", "").upper()
        if key not in _FIRMWARE:
            raise ValueError(f"Unknown EZO simulator type {sensor!r}; use one of {', '.join(_LABEL.values())}")
        self.sensor = key
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        self.continuous = 1  # factory default: one reading per second
        self.response_codes = True
        self.led = True
        self.name = ""
        self.cal_points: set[str] = set()
        self.temp_comp_c = 20.0 if key == "DO" else 25.0
        self.k = 1.0
        self.tds_factor = 0.54
        self.salinity: tuple[float, str] = (0.0, "\u00b5S")
        self.pressure_kpa = 101.3
        self.enabled = {"DO": ["mg"], "EC": ["EC", "TDS", "S", "SG"], "HUM": ["HUM"], "CO2": ["ppm"]}.get(key, [])
        self.rtd_scale = "c"
        self.prs_unit = "psi"
        self.prs_unit_suffix = False
        self.asleep = False
        # The "sample" the probe is sitting in.
        self.sample = {"PH": 7.20, "ORP": 225.0, "DO": 8.30, "EC": 1413.0, "RTD": 25.104, "HUM": 45.20,
                       "CO2": 452.0, "PRS": 0.120}[key]

    # ------------------------------------------------------------- values

    def _drift(self) -> float:
        return math.sin((time.monotonic() - self.t0) / 60.0)

    def _error(self) -> float:
        """Reading error of an uncalibrated / partly calibrated probe."""
        if self.sensor == "PH":
            return 0.0 if {"low", "high"} & self.cal_points else (0.03 if "mid" in self.cal_points else 0.12)
        if self.sensor == "EC":
            return 0.0 if len(self.cal_points) >= 2 else 0.06 * self.sample
        if self.sensor in {"ORP", "RTD"}:
            return 0.0 if self.cal_points else (4.0 if self.sensor == "ORP" else 0.35)
        if self.sensor == "DO":
            return 0.0 if "atmospheric" in self.cal_points else 0.4
        return 0.0

    def _reading(self) -> str:
        n = self.rng.gauss
        s = self.sensor
        if s == "PH":
            return f"{self.sample + self._error() + 0.004 * self._drift() + n(0, 0.002):.3f}"
        if s == "ORP":
            return f"{self.sample + self._error() + n(0, 0.3):.1f}"
        if s == "RTD":
            c = self.sample + self._error() + 0.02 * self._drift() + n(0, 0.003)
            value = {"c": c, "k": c + 273.15, "f": c * 9 / 5 + 32}[self.rtd_scale]
            return f"{value:.3f}"
        if s == "DO":
            mg = max(0.0, self.sample + self._error() + n(0, 0.01))
            pct = 100.0 * mg / _do_saturation_mg_l(self.temp_comp_c) * 101.325 / self.pressure_kpa
            parts = {"mg": f"{mg:.2f}", "%": f"{pct:.1f}"}
            return ",".join(parts[p] for p in ("mg", "%") if p in self.enabled) or "no output"
        if s == "EC":
            ec = self.sample + self._error() + n(0, 1.0)
            ms = ec / 1000.0
            sal = 0.4665 * ms**1.0878
            parts = {"EC": f"{ec:.0f}" if ec >= 100 else f"{ec:.2f}", "TDS": f"{ec * self.tds_factor:.0f}",
                     "S": f"{sal:.2f}", "SG": f"{1.0 + 0.00075 * sal:.3f}"}
            return ",".join(parts[p] for p in ("EC", "TDS", "S", "SG") if p in self.enabled) or "no output"
        if s == "HUM":
            rh = self.sample + n(0, 0.05)
            t = 22.40 + n(0, 0.02)
            g = math.log(rh / 100) + 17.62 * t / (243.12 + t)
            dew = 243.12 * g / (17.62 - g)
            out = []
            if "HUM" in self.enabled:
                out.append(f"{rh:.2f}")
            if "T" in self.enabled:
                out.append(f"{t:.2f}")
            if "Dew" in self.enabled:
                out += ["Dew", f"{dew:.2f}"]
            return ",".join(out) or "no output"
        if s == "CO2":
            out = [f"{self.sample + n(0, 3):.0f}"] if "ppm" in self.enabled else []
            if "t" in self.enabled:
                out.append(f"{31.5 + n(0, 0.05):.2f}")
            return ",".join(out) or "no output"
        # PRS: compound sensor referenced to sea level
        psi = self.sample + n(0, 0.002)
        factor = {"psi": 1, "atm": 0.068046, "bar": 0.0689476, "kpa": 6.89476, "inh2o": 27.6799, "cmh2o": 70.307}
        value = f"{psi * factor[self.prs_unit]:.3f}"
        return value + (f",{self.prs_unit}" if self.prs_unit_suffix else "")

    # ------------------------------------------------------------- protocol

    def _ok(self, *lines: str) -> list[str]:
        out = list(lines)
        if self.response_codes:
            out.append("*OK")
        return out

    def handle(self, command: str) -> str | list[str] | None:
        cmd = command.strip()
        if not cmd:
            return None
        if self.asleep:
            self.asleep = False
            return "*WA"  # the first character only wakes the circuit
        prefix: list[str] = []
        if self.continuous:
            prefix = [self._reading()]  # a continuous reading lands in the middle of the reply
        try:
            reply = self._dispatch(cmd)
        except (ValueError, IndexError):
            reply = None
        if reply is None:
            reply = ["*ER"]
        return prefix + reply

    def _dispatch(self, cmd: str) -> list[str] | None:
        parts = cmd.split(",")
        head = parts[0].strip().lower()
        args = [p.strip() for p in parts[1:]]
        s = self.sensor
        q = args[:1] == ["?"]
        if head == "i" and not args:
            return self._ok(f"?i,{_LABEL[s]},{_FIRMWARE[s]}")
        if head == "r" and not args:
            return self._ok(self._reading())
        if head == "c":
            if q:
                return self._ok(f"?C,{self.continuous}")
            n = int(args[0])
            if not 0 <= n <= 99:
                return None
            self.continuous = n
            return self._ok()
        if head == "*ok":
            if q:
                return [f"?*OK,{int(self.response_codes)}"] + (["*OK"] if self.response_codes else [])
            self.response_codes = args[0] == "1"
            return self._ok()
        if head == "l":
            if q:
                return self._ok(f"?L,{int(self.led)}")
            self.led = args[0] == "1"
            return self._ok()
        if head == "status":
            return self._ok("?Status,P,5.038")
        if head == "name":
            if q:
                return self._ok(f"?Name,{self.name}")
            self.name = args[0] if args else ""
            return self._ok()
        if head == "find":
            self.continuous = 0
            return self._ok()
        if head == "sleep":
            self.asleep = True
            return self._ok("*SL")
        if head == "factory":
            self.__init__(_LABEL[s])  # type: ignore[misc]
            return ["*OK", "*RS", "*RE"]
        if head == "cal":
            return self._calibrate(args)
        if head == "t" and s in {"PH", "EC", "DO"}:
            if q:
                return self._ok(f"?T,{self.temp_comp_c:g}")
            self.temp_comp_c = float(args[0])
            return self._ok()
        if head == "slope" and s == "PH" and q:
            if "mid" not in self.cal_points:
                return self._ok("?Slope,100.0,100.0,0.00")
            return self._ok("?Slope,99.7,100.3,-0.89")
        if head == "k" and s == "EC":
            if q:
                return self._ok(f"?K,{self.k:g}")
            k = float(args[0])
            if not 0.01 <= k <= 1000:
                return None
            self.k = k
            return self._ok()
        if head == "o" and s in {"DO", "EC", "HUM", "CO2"}:
            return self._output(args)
        if head == "s" and s == "RTD":
            if q:
                return self._ok(f"?S,{self.rtd_scale}")
            if args[0].lower() not in {"c", "k", "f"}:
                return None
            self.rtd_scale = args[0].lower()
            return self._ok()
        if head == "s" and s == "DO":
            if q:
                return self._ok(f"?S,{self.salinity[0]:g},{self.salinity[1]}")
            self.salinity = (float(args[0]), "ppt" if args[1:2] == ["ppt"] else "\u00b5S")
            return self._ok()
        if head == "p" and s == "DO":
            if q:
                return self._ok(f"?,P,{self.pressure_kpa:g}")  # sic: the UART datasheet shows "?,P,"
            self.pressure_kpa = float(args[0])
            return self._ok()
        if head == "u" and s == "PRS":
            if q:
                return self._ok(f"?U,{self.prs_unit}")
            arg = args[0].lower()
            if arg in {"0", "1"}:
                self.prs_unit_suffix = arg == "1"
            elif arg in {"psi", "atm", "bar", "kpa", "inh2o", "cmh2o"}:
                self.prs_unit = arg
            else:
                return None
            return self._ok()
        return None

    def _output(self, args: list[str]) -> list[str] | None:
        order = {"DO": ["mg", "%"], "EC": ["EC", "TDS", "S", "SG"], "HUM": ["HUM", "T", "Dew"], "CO2": ["ppm", "t"]}[
            self.sensor
        ]
        if args[:1] == ["?"]:
            if self.sensor == "CO2":
                return self._ok("?O," + ",".join(self.enabled))
            # EC/DO/HUM list enabled parameters as "?,O,..." (DO shows "%,mg" order in its example)
            listed = ["%", "mg"] if self.sensor == "DO" else order
            return self._ok("?,O," + ",".join(p for p in listed if p in self.enabled))
        if len(args) != 2 or args[1] not in {"0", "1"}:
            return None
        match = next((p for p in order if p.lower() == args[0].lower()), None)
        if match is None:
            return None
        if args[1] == "1" and match not in self.enabled:
            self.enabled.append(match)
        if args[1] == "0" and match in self.enabled:
            self.enabled.remove(match)
        self.enabled.sort(key=order.index)
        return self._ok()

    def _calibrate(self, args: list[str]) -> list[str] | None:
        s = self.sensor
        if args[:1] == ["?"]:
            n = len(self.cal_points)
            if s in {"CO2", "PRS"}:
                n = int("zero" in self.cal_points) + 2 * int("high" in self.cal_points)
            label = "CAL" if s == "EC" else "Cal"
            return self._ok(f"?{label},{n}")
        if args[:1] == ["clear"]:
            self.cal_points.clear()
            return self._ok()
        if s == "HUM":
            return None
        if s == "PH":
            if len(args) != 2 or args[0] not in {"mid", "low", "high"}:
                return None
            float(args[1])
            if args[0] == "mid":
                self.cal_points = {"mid"}  # "Cal,mid ... will clear the other calibration points"
            else:
                self.cal_points.add(args[0])
            return self._ok()
        if s == "DO":
            if not args:
                self.cal_points.add("atmospheric")
            elif args == ["0"]:
                self.cal_points.add("zero")
            else:
                return None
            return self._ok()
        if s == "EC":
            if args == ["dry"]:
                self.cal_points = {"dry"}
            elif len(args) == 1:
                self.cal_points.add("single")
            elif len(args) == 2 and args[0] in {"low", "high"}:
                self.cal_points.add(args[0])
            else:
                return None
            if args != ["dry"]:
                float(args[-1])  # a non-numeric value is rejected with *ER
            return self._ok()
        if s in {"CO2", "PRS"}:
            if len(args) != 1:
                return None
            self.cal_points.add("zero" if float(args[0]) == 0 else "high")
            return self._ok()
        if len(args) != 1:  # ORP, RTD
            return None
        float(args[0])
        self.cal_points.add("single")
        return self._ok()
