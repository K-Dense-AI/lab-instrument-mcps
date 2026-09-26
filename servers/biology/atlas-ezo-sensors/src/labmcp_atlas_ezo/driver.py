"""UART driver for Atlas Scientific EZO(TM) sensor circuits.

Protocol references (Atlas Scientific datasheets, "UART mode" chapters; commands, replies and
response codes are identical across the family unless noted):

* EZO-pH  datasheet V 6.1 (rev. 2/24)  https://files.atlas-scientific.com/pH_EZO_Datasheet.pdf
* EZO-ORP datasheet V 5.2 (rev. 11/24) https://files.atlas-scientific.com/ORP_EZO_Datasheet.pdf
* EZO-DO  datasheet V 5.8 (rev. 3/25)  https://files.atlas-scientific.com/DO_EZO_Datasheet.pdf
* EZO-EC  datasheet V 6.7 (rev. 7/26)  https://files.atlas-scientific.com/EC_EZO_Datasheet.pdf
* EZO-RTD datasheet V 3.7 (rev. 10/24) https://files.atlas-scientific.com/EZO_RTD_Datasheet.pdf
* EZO-HUM datasheet V 1.5              https://files.atlas-scientific.com/EZO-HUM-Datasheet.pdf
* EZO-CO2 datasheet V 2.3 (rev. 10/25) https://files.atlas-scientific.com/EZO_CO2_Datasheet.pdf
* EZO-PRS datasheet V 2.3 (rev. 8/25)  https://files.atlas-scientific.com/EZO-PRS-Datasheet.pdf

Wire format: ASCII, 9600 8N1 by default, commands and replies terminated by CR. Commands
are not case sensitive. With response codes enabled (factory default, ``*OK,1``) every
command is answered by its data line(s) (if any) followed by ``*OK``, or by ``*ER`` for an
unknown command / bad parameter. Queries answer ``?<Cmd>,<values>`` (e.g. ``?Cal,2``,
``?T,19.5``); a few DO queries answer ``?,P,90.25`` / ``?,O,%,mg``, which is also handled.

Continuous reading mode is ON by default (one reading per second), which would interleave
unsolicited readings with command replies. :meth:`EZOCircuit.initialise` therefore sends
``C,0`` (continuous off) and ``*OK,1`` (response codes on); both settings are retained by
the circuit across power cycles and are restored to their defaults by ``Factory``.

Multi-value outputs (EC, DO, HUM, CO2) are comma-separated in a fixed order filtered by
the parameters enabled with ``O,...`` (queried with ``O,?``): EC -> EC,TDS,S,SG (datasheet
p. 20 shows ``EC,TDS,SAL,SG``); DO -> mg/L,% (order cross-checked with the EnviroDIY
ModularSensors and feastorg ezo-driver libraries); HUM -> HUM,T,Dew; CO2 -> ppm,t.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport

#: Normalised device type (from the ``i`` reply, dots removed, upper case) -> display name.
SENSOR_TYPES: dict[str, str] = {
    "PH": "pH",
    "ORP": "ORP",
    "DO": "Dissolved oxygen",
    "EC": "Conductivity",
    "RTD": "Temperature (RTD)",
    "HUM": "Humidity",
    "CO2": "Carbon dioxide (gas)",
    "PRS": "Pressure",
}

#: Output parameters in wire order, with (parameter key used by ``O,...``, output name, unit).
OUTPUTS: dict[str, list[tuple[str, str, str]]] = {
    "PH": [("", "ph", "pH")],
    "ORP": [("", "orp_mv", "mV")],
    "DO": [("mg", "do_mg_l", "mg/L"), ("%", "do_percent_saturation", "% sat")],
    "EC": [("EC", "conductivity_us_cm", "µS/cm"), ("TDS", "tds_ppm", "ppm"),
           ("S", "salinity_psu", "PSU"), ("SG", "specific_gravity", "")],
    "RTD": [("", "temperature", "°C")],
    "HUM": [("HUM", "relative_humidity_percent", "%RH"), ("T", "air_temperature_c", "°C"),
            ("Dew", "dew_point_c", "°C")],
    "CO2": [("ppm", "co2_ppm", "ppm"), ("t", "internal_temperature_c", "°C")],
    "PRS": [("", "pressure", "psi")],
}

#: Calibration points per type: point name -> (command template, needs a value).
CAL_POINTS: dict[str, dict[str, tuple[str, bool]]] = {
    "PH": {"mid": ("Cal,mid,{v}", True), "low": ("Cal,low,{v}", True), "high": ("Cal,high,{v}", True)},
    "ORP": {"single": ("Cal,{v}", True)},
    "DO": {"atmospheric": ("Cal", False), "zero": ("Cal,0", False)},
    "EC": {"dry": ("Cal,dry", False), "single": ("Cal,{v}", True), "low": ("Cal,low,{v}", True),
           "high": ("Cal,high,{v}", True)},
    "RTD": {"single": ("Cal,{v}", True)},
    "CO2": {"zero": ("Cal,0", False), "high": ("Cal,{v}", True)},
    "PRS": {"zero": ("Cal,0", False), "high": ("Cal,{v}", True)},
}

#: What the number in ``?Cal,n`` means, per type.
CAL_MEANING: dict[str, dict[int, str]] = {
    "PH": {0: "not calibrated", 1: "one-point (mid)", 2: "two-point (mid + low or high)", 3: "three-point"},
    "ORP": {0: "not calibrated", 1: "calibrated"},
    "DO": {0: "not calibrated", 1: "one-point (atmospheric)", 2: "two-point (atmospheric + zero)"},
    "EC": {0: "not calibrated", 1: "one-point", 2: "two-point", 3: "three-point"},
    "RTD": {0: "not calibrated (factory)", 1: "calibrated"},
    "CO2": {0: "factory calibration", 1: "zero point", 2: "high point", 3: "zero and high point"},
    "PRS": {0: "factory calibration", 1: "zero point", 2: "high point", 3: "zero and high point"},
}

TEMPERATURE_COMPENSATED = {"PH", "EC", "DO"}

RESTART_CODES = {"P": "powered off", "S": "software reset", "B": "brown out", "W": "watchdog", "U": "unknown"}

_RTD_SCALE = {"c": "°C", "k": "K", "f": "°F"}
_PRS_UNITS = {"psi": "psi", "atm": "atm", "bar": "bar", "kpa": "kPa", "inh2o": "inH2O", "cmh2o": "cmH2O"}
_NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?$")

#: Seconds a reading takes (pH 800 ms; DO/EC/RTD 600 ms; HUM/CO2/PRS 1 s) plus margin.
_READ_TIMEOUT = 3.0


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


@dataclass
class Reading:
    sensor: str
    values: dict[str, float]
    units: dict[str, str]
    raw: str
    warnings: list[str] = field(default_factory=list)


class EZOCircuit:
    """One EZO circuit on a UART (USB-serial adapter, isolator carrier board, ...)."""

    def __init__(self, transport: Transport, *, timeout: float = 1.5) -> None:
        self.t = transport
        self.timeout = timeout
        self.sensor = ""  # normalised type, e.g. "PH"
        self.device_label = ""  # as reported, e.g. "pH" or "D.O."
        self.firmware = ""
        self.enabled: list[str] = []  # enabled O,... parameters (multi-output types)
        self.rtd_scale = "c"
        self.prs_unit = "psi"
        self.warnings: list[str] = []

    # ------------------------------------------------------------ low level

    def command(self, cmd: str, timeout: float | None = None) -> list[str]:
        """Send ``cmd`` and return its data lines, once ``*OK`` arrives. Raises on ``*ER``."""
        with self.t.lock:
            self.t.flush_input()
            self.t.write(cmd)
            return self._collect(cmd, self.timeout if timeout is None else timeout)

    def _collect(self, cmd: str, timeout: float) -> list[str]:
        # One overall deadline, not a per-line timeout: a circuit that keeps streaming readings
        # (continuous mode still on, e.g. after waking from sleep) must not keep us here forever.
        data: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InstrumentTimeout("overall reply deadline passed")
                line = self.t.read(remaining).strip()
            except InstrumentTimeout as exc:
                raise InstrumentTimeout(
                    f"No '*OK' from the EZO circuit after {cmd!r}"
                    + (f" (got {data!r})" if data else "")
                    + ". Check that it is in UART mode (LED green, not blue), the baud rate "
                    "(default 9600) and that TX/RX are crossed."
                ) from exc
            if not line:
                continue
            if line == "*OK":
                return data
            if line == "*ER":
                raise InstrumentProtocolError(
                    f"EZO circuit replied *ER to {cmd!r}: unknown command or invalid parameter "
                    f"for this {self.device_label or 'circuit'} (firmware {self.firmware or '?'})."
                )
            if line in {"*OV", "*UV"}:
                msg = "supply over-voltage (Vcc >= 5.5 V)" if line == "*OV" else "supply under-voltage (Vcc <= 3.1 V)"
                self.warnings.append(f"Circuit reported {line}: {msg}")
                continue
            if line in {"*RS", "*RE", "*WA", "*SL", "*DONE"}:
                continue
            data.append(line)

    def query(self, cmd: str, name: str, timeout: float | None = None) -> list[str]:
        """Send a ``?`` query and return the values after ``?<name>,``."""
        for line in self.command(cmd, timeout):
            values = _parse_query(line, name)
            if values is not None:
                return values
        raise InstrumentProtocolError(f"Unexpected reply to {cmd!r}: no '?{name},...' line.")

    # ------------------------------------------------------------ connect

    def initialise(self) -> None:
        """Stop continuous mode, enable response codes, identify the circuit."""
        with self.t.lock:
            # 1) C,0 - may be ignored if the circuit was asleep (the first byte wakes it) or if
            #    response codes are off, so don't insist on *OK yet; drain what arrives.
            self.t.flush_input()
            self.t.write("C,0")
            with contextlib.suppress(InstrumentTimeout, InstrumentProtocolError):
                self._collect("C,0", 1.2)
            # 2) response codes on (factory default), 3) C,0 again, now confirmed by *OK.
            self.command("*OK,1")
            self.command("C,0")
        info = self.query("i", "i")
        if not info:
            raise InstrumentProtocolError("Empty reply to 'i' (device information).")
        self.device_label = info[0]
        self.firmware = info[1] if len(info) > 1 else ""
        self.sensor = info[0].replace(".", "").upper()
        if self.sensor not in SENSOR_TYPES:
            raise InstrumentProtocolError(
                f"Connected EZO device reports type {info[0]!r}, which this server does not support "
                f"(supported: {', '.join(SENSOR_TYPES)}). Pumps and other actuators are deliberately "
                "not handled here."
            )
        self.refresh_config()

    def refresh_config(self) -> None:
        if self.sensor in {"EC", "DO", "HUM", "CO2"}:
            self.enabled = self.output_parameters()
        if self.sensor == "RTD":
            scale = self.query("S,?", "S")
            self.rtd_scale = scale[0].lower() if scale else "c"
        if self.sensor == "PRS":
            unit = self.query("U,?", "U")
            self.prs_unit = unit[0].lower() if unit else "psi"

    def identify(self) -> dict[str, str]:
        return {
            "manufacturer": "Atlas Scientific",
            "model": f"EZO-{self.device_label}",
            "sensor": SENSOR_TYPES.get(self.sensor, self.sensor),
            "firmware": self.firmware,
        }

    # ------------------------------------------------------------ readings

    def output_parameters(self) -> list[str]:
        values = self.query("O,?", "O")
        known = {k.lower(): k for k, _, _ in OUTPUTS[self.sensor]}
        return [known[v.lower()] for v in values if v.lower() in known]

    def outputs(self) -> list[tuple[str, str]]:
        """``(name, unit)`` of each value in a reading, in wire order."""
        spec = OUTPUTS[self.sensor]
        if self.sensor in {"EC", "DO", "HUM", "CO2"}:
            spec = [s for s in spec if s[0] in self.enabled]
        out = [(name, unit) for _, name, unit in spec]
        if self.sensor == "RTD":
            out = [("temperature", _RTD_SCALE.get(self.rtd_scale, self.rtd_scale))]
        if self.sensor == "PRS":
            out = [("pressure", _PRS_UNITS.get(self.prs_unit, self.prs_unit))]
        return out

    def read(self) -> Reading:
        self.warnings = []
        lines = self.command("R", timeout=_READ_TIMEOUT)
        if not lines:
            raise InstrumentProtocolError("The EZO circuit returned no reading for 'R'.")
        raw = lines[-1]
        if raw.lower() == "no output":
            raise InstrumentProtocolError(
                "The circuit reports 'no output': every output parameter is disabled (O,...)."
            )
        # Skip label tokens (e.g. PRS with U,1 appends ",psi"); keep numbers in order.
        tokens = [tok.strip() for tok in raw.split(",") if tok.strip()]
        numbers = [float(tok) for tok in tokens if _NUMBER_RE.match(tok)]
        if not numbers:
            raise InstrumentProtocolError(f"The EZO circuit's reply to 'R' contains no number: {raw!r}.")
        expected = self.outputs()
        if len(numbers) != len(expected):
            # The O,... settings may have been changed by someone else: re-read and retry once.
            self.refresh_config()
            expected = self.outputs()
            if len(numbers) != len(expected):
                raise InstrumentProtocolError(
                    f"Reading {raw!r} has {len(numbers)} value(s) but {len(expected)} expected "
                    f"({', '.join(n for n, _ in expected)})."
                )
        values = {name: num for (name, _), num in zip(expected, numbers, strict=True)}
        units = dict(expected)
        warnings = list(self.warnings)
        if self.sensor == "RTD" and numbers and numbers[0] <= -1023:
            warnings.append("RTD reads -1023: no temperature probe is connected.")
        return Reading(SENSOR_TYPES[self.sensor], values, units, raw, warnings)

    # ------------------------------------------------------------ status

    def status(self) -> tuple[str, float | None]:
        values = self.query("Status", "Status")
        code = values[0] if values else "U"
        try:
            vcc = float(values[1]) if len(values) > 1 else None
        except ValueError:
            vcc = None
        return code, vcc

    def calibration_points(self) -> int:
        values = self.query("Cal,?", "Cal")
        try:
            return int(values[0])
        except (IndexError, ValueError) as exc:
            raise InstrumentProtocolError(f"Unexpected calibration status {values!r}") from exc

    def slope(self) -> tuple[float, float, float] | None:
        """pH only: ``?Slope,99.7,100.3,-0.89`` -> acid %, base %, zero offset mV."""
        if self.sensor != "PH":
            return None
        values = self.query("Slope,?", "Slope")
        try:
            return float(values[0]), float(values[1]), float(values[2])
        except (IndexError, ValueError):
            return None

    def led(self) -> bool:
        return self.query("L,?", "L")[:1] == ["1"]

    def set_led(self, on: bool) -> None:
        self.command(f"L,{1 if on else 0}")

    def name(self) -> str:
        values = self.query("Name,?", "Name")
        return values[0] if values else ""

    # ------------------------------------------------------------ calibration & compensation

    def calibrate(self, point: str, value: float | None) -> None:
        points = CAL_POINTS.get(self.sensor)
        if not points or point not in points:
            valid = ", ".join(points) if points else "none (this circuit has no user calibration)"
            raise InstrumentProtocolError(f"Invalid calibration point {point!r} for EZO-{self.device_label}; valid: {valid}.")
        template, needs_value = points[point]
        if needs_value and value is None:
            raise InstrumentProtocolError(f"Calibration point {point!r} needs a reference value.")
        cmd = template.format(v=_fmt(value)) if needs_value and value is not None else template
        self.command(cmd, timeout=_READ_TIMEOUT)

    def clear_calibration(self) -> None:
        self.command("Cal,clear", timeout=_READ_TIMEOUT)

    def temperature_compensation(self) -> float | None:
        if self.sensor not in TEMPERATURE_COMPENSATED:
            return None
        values = self.query("T,?", "T")
        try:
            return float(values[0])
        except (IndexError, ValueError):
            return None

    def set_temperature_compensation(self, temperature_c: float) -> float | None:
        self.command(f"T,{_fmt(temperature_c)}")
        return self.temperature_compensation()

    def probe_constant(self) -> float | None:
        if self.sensor != "EC":
            return None
        values = self.query("K,?", "K")
        try:
            return float(values[0])
        except (IndexError, ValueError):
            return None

    def set_probe_constant(self, k: float) -> float | None:
        self.command(f"K,{_fmt(k)}")
        return self.probe_constant()

    def salinity(self) -> tuple[float, str] | None:
        """DO only: ``?S,50000,μS`` or ``?S,37.5,ppt``."""
        values = self.query("S,?", "S")
        try:
            return float(values[0]), ("ppt" if len(values) > 1 and "ppt" in values[1].lower() else "µS/cm")
        except (IndexError, ValueError):
            return None

    def set_salinity(self, value: float, ppt: bool) -> None:
        self.command(f"S,{_fmt(value)}" + (",ppt" if ppt else ""))

    def pressure_kpa(self) -> float | None:
        """DO only: ``?,P,90.25`` (UART datasheet) or ``?P,90.25``."""
        values = self.query("P,?", "P")
        try:
            return float(values[0])
        except (IndexError, ValueError):
            return None

    def set_pressure_kpa(self, kpa: float) -> None:
        self.command(f"P,{_fmt(kpa)}")

    def close(self) -> None:
        self.t.close()


def _parse_query(line: str, name: str) -> list[str] | None:
    """``?Cal,2`` / ``?CAL,2`` / ``?,O,%,mg`` / ``?*OK,1`` -> values after the name."""
    if not line.startswith("?"):
        return None
    parts = [p.strip() for p in line[1:].split(",")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if not parts or parts[0].lower() != name.lower():
        return None
    return parts[1:]
