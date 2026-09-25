"""RS-232 "Basic mode" driver for New Era Pump Systems syringe pumps (NE-1000 family).

Implements the RS-232 protocol and command set documented in:

* "NE-1000 Series of Programmable Syringe Pumps - Model NE-1000 Multi-Phaser - User Manual",
  New Era Pump Systems Inc., Publication #1200-01, firmware V3.923 (04/06/16), section 10
  "RS-232 Communications" and appendix 12.1/12.7:
  https://www.syringepump.com/download/NE-1000%20Syringe%20Pump%20User%20Manual.pdf
* The NE-4000 (Publication #1240-01, V3.919) and NE-500 OEM (Publication #1200-02, V3.919)
  manuals, which document the identical command set.

Wire format (Basic mode, factory default 19200 baud 8N1, address 0)::

    to pump:    [<address 0-99>] <command data> CR
    from pump:  STX <address> <status> [<data>] ETX

``<status>`` is a prompt (``I`` infusing, ``W`` withdrawing, ``S`` stopped, ``P`` paused,
``T`` timed pause, ``U`` waiting for trigger, ``X`` purging) or an alarm ``A?<type>``
(``R`` reset/power interrupted, ``S`` motor stalled, ``T`` safe-mode timeout, ``E`` program
error, ``O`` phase out of range). Command errors are returned in the data field as ``?``
(not recognised), ``?NA`` (not applicable now), ``?OOR`` (out of range), ``?COM`` (bad
packet) or ``?IGN`` (ignored).

Numbers are sent as "<float>": at most 4 digits plus a decimal point, at most 3 digits after
the point (section 10.2.1).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, Transport

STX = "\x02"

PROMPTS = {
    "I": "infusing",
    "W": "withdrawing",
    "S": "stopped",
    "P": "paused",
    "T": "timed pause phase",
    "U": "waiting for operational trigger",
    "X": "purging",
}

ALARMS = {
    "R": "pump was reset (power was interrupted)",
    "S": "pump motor stalled (the syringe is blocked, at the end of travel, or the force is too high)",
    "T": "Safe-mode communications time-out",
    "E": "Pumping Program error",
    "O": "Pumping Program phase is out of range",
}

ERRORS = {
    "": "command not recognised",
    "NA": "command not applicable right now (e.g. the pump is running, or the phase is not a RATE phase)",
    "OOR": "command data out of range",
    "COM": "invalid communications packet received",
    "IGN": "command ignored because a new program phase started at the same time",
}

#: Rate units of the ``RAT`` command and their factor to mL/min.
RATE_UNITS = {"MM": 1.0, "UM": 1e-3, "MH": 1.0 / 60.0, "UH": 1e-3 / 60.0}
RATE_UNIT_NAMES = {"MM": "mL/min", "UM": "µL/min", "MH": "mL/h", "UH": "µL/h"}
VOLUME_UNITS = {"ML": 1.0, "UL": 1e-3}  # factor to mL

#: NE-1000 plunger speed limits (User Manual section 12.5): used only to explain ?OOR replies.
NE1000_MAX_SPEED_CM_MIN = 5.1005
NE1000_MIN_SPEED_CM_H = 0.004205

_REPLY_RE = re.compile(r"^(?P<addr>\d{1,2})(?:A\?(?P<alarm>[A-Z])|(?P<prompt>[IWSPTUX]))(?P<data>.*)$", re.S)
_RATE_RE = re.compile(r"^\s*(?P<value>\d*\.?\d+)\s*(?P<units>UM|MM|UH|MH)?\s*$")
_VOLUME_RE = re.compile(r"^\s*(?P<value>\d*\.?\d+)\s*(?P<units>UL|ML)\s*$")
_DISPENSED_RE = re.compile(r"^\s*I\s*(?P<inf>\d*\.?\d+)\s*W\s*(?P<wdr>\d*\.?\d+)\s*(?P<units>UL|ML)\s*$")
_VERSION_RE = re.compile(r"^NE(?P<model>[0-9A-Z]+?)V\s*(?P<fw>\d+\.\d+)")


# Representative inside diameters from the NE-1000 User Manual, section 12.7 "Syringe Diameters
# and Rate Limits". Always confirm against your syringe manufacturer's specification.
SYRINGE_PRESETS_MM: dict[str, float] = {
    "BD 1 mL": 4.699,
    "BD 3 mL": 8.585,
    "BD 5 mL": 11.99,
    "BD 10 mL": 14.43,
    "BD 20 mL": 19.05,
    "BD 30 mL": 21.59,
    "BD 60 mL": 26.59,
    "HSW Norm-Ject 1 mL": 4.69,
    "HSW Norm-Ject 3 mL": 9.65,
    "HSW Norm-Ject 5 mL": 12.45,
    "HSW Norm-Ject 10 mL": 15.9,
    "HSW Norm-Ject 20 mL": 20.05,
    "HSW Norm-Ject 30 mL": 22.9,
    "HSW Norm-Ject 50 mL": 29.2,
    "Monoject 1 mL": 5.74,
    "Monoject 3 mL": 8.941,
    "Monoject 6 mL": 12.7,
    "Monoject 12 mL": 15.72,
    "Monoject 20 mL": 20.12,
    "Monoject 35 mL": 23.52,
    "Monoject 60 mL": 26.64,
    "Monoject 140 mL": 38.0,
    "Terumo 1 mL": 4.7,
    "Terumo 3 mL": 8.95,
    "Terumo 5 mL": 13.0,
    "Terumo 10 mL": 15.8,
    "Terumo 20 mL": 20.15,
    "Terumo 30 mL": 23.1,
    "Terumo 60 mL": 29.7,
    "Poulten & Graf glass 1 mL": 6.7,
    "Poulten & Graf glass 2 mL": 8.91,
    "Poulten & Graf glass 3 mL": 9.06,
    "Poulten & Graf glass 5 mL": 11.75,
    "Poulten & Graf glass 10 mL": 14.67,
    "Poulten & Graf glass 20 mL": 19.62,
    "Poulten & Graf glass 30 mL": 22.69,
    "Poulten & Graf glass 50 mL": 26.96,
    "Stainless steel 1 mL": 9.538,
    "Stainless steel 3 mL": 9.538,
    "Stainless steel 5 mL": 12.7,
    "Stainless steel 8 mL": 9.538,
    "Stainless steel 20 mL": 19.13,
    "Stainless steel 50 mL": 28.6,
    "SGE gas-tight 5 uL": 0.343,
    "SGE gas-tight 10 uL": 0.485,
    "SGE gas-tight 25 uL": 0.728,
    "SGE gas-tight 50 uL": 1.03,
    "SGE gas-tight 100 uL": 1.457,
    "Hamilton Microliter 0.5 uL": 0.103,
    "Hamilton Microliter 1 uL": 0.146,
    "Hamilton Microliter 2 uL": 0.206,
    "Hamilton Microliter 5 uL": 0.326,
}


def _norm(name: str) -> str:
    return re.sub(r"[\s\-_]+", "", name.lower().replace("µ", "u"))


def find_syringe_preset(name: str) -> tuple[str, float]:
    """Look up a preset by name (case, spaces and dashes are ignored)."""
    key = _norm(name)
    for preset, diameter in SYRINGE_PRESETS_MM.items():
        if _norm(preset) == key:
            return preset, diameter
    raise ValueError(
        f"Unknown syringe preset {name!r}. Call `list_syringe_presets` for the available names, "
        "or pass the inside diameter with `diameter_mm`."
    )


def format_float(value: float) -> str:
    """Format ``value`` as a New Era <float>: <= 4 digits, <= 3 decimals (manual 10.2.1)."""
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{value!r} cannot be sent to the pump (must be a positive number)")
    for decimals in (3, 2, 1, 0):
        text = f"{value:.{decimals}f}"
        if sum(ch.isdigit() for ch in text) <= 4:
            if float(text) <= 0:
                break
            return text
    raise ValueError(
        f"{value:g} cannot be represented with the pump's 4-digit number format "
        "(valid: 0.001 to 9999). Use different units."
    )


def choose_rate_units(rate_ml_min: float) -> tuple[str, str, float]:
    """Pick the RAT units that represent ``rate_ml_min`` most precisely.

    Returns ``(number_text, units, actual_rate_ml_min)``.
    """
    best: tuple[float, int, int, str, str, float] | None = None
    for rank, (units, factor) in enumerate(RATE_UNITS.items()):
        try:
            text = format_float(rate_ml_min / factor)
        except ValueError:
            continue
        actual = float(text) * factor
        err = round(abs(actual - rate_ml_min) / rate_ml_min, 12)
        significant = len(text.replace(".", "").lstrip("0"))  # precision headroom, e.g. 5.000 > 0.005
        cand = (err, -significant, rank, text, units, actual)
        if best is None or cand[:3] < best[:3]:
            best = cand
    if best is None:
        raise ValueError(f"A rate of {rate_ml_min:g} mL/min cannot be expressed in any pump rate unit.")
    return best[3], best[4], best[5]


def ne1000_rate_range_ml_min(diameter_mm: float) -> tuple[float, float]:
    """(min, max) pumping rate in mL/min for a syringe diameter, per the NE-1000 speed specification."""
    area_cm2 = math.pi * (diameter_mm / 20.0) ** 2
    return area_cm2 * NE1000_MIN_SPEED_CM_H / 60.0, area_cm2 * NE1000_MAX_SPEED_CM_MIN


@dataclass
class Reply:
    address: int
    prompt: str | None  # one of PROMPTS, or None when an alarm was reported
    alarm: str | None  # one of ALARMS, or None
    data: str

    @property
    def state(self) -> str:
        if self.alarm:
            return "alarm"
        return PROMPTS.get(self.prompt or "", "unknown")


@dataclass
class DispensePlan:
    direction: str  # "INF" | "WDR"
    volume_ml: float
    rate_ml_min: float
    rate_text: str  # what was sent, e.g. "1.500MM"
    volume_text: str  # e.g. "0.500ML"
    diameter_mm: float
    reset_alarm_cleared: bool


class NewEraPump:
    """One pump on a New Era RS-232 pump network (Basic mode)."""

    def __init__(self, transport: Transport, address: int | None = None) -> None:
        self.t = transport
        self.address = address  # None: send commands without an address prefix (= address 0)
        self.reset_alarm_seen = False

    # ------------------------------------------------------------ low level

    def _expected_address(self) -> int:
        return 0 if self.address is None else self.address

    def send(self, cmd: str, timeout: float | None = None) -> Reply:
        """Send one command and parse the reply packet. Alarms and ``?`` errors are returned, not raised."""
        prefix = "" if self.address is None else str(self.address)
        raw = self.t.query(prefix + cmd, timeout)
        start = raw.rfind(STX)
        if start < 0:
            raise InstrumentProtocolError(
                f"Pump reply to {cmd!r} has no STX start byte: {raw!r}. Is the pump in Safe mode? "
                "(Basic mode is required; send `SAF0` from a terminal or reset the pump.)"
            )
        body = raw[start + 1 :]
        m = _REPLY_RE.match(body)
        if not m:
            raise InstrumentProtocolError(f"Unexpected reply to {cmd!r}: {body!r}")
        addr = int(m["addr"])
        if addr != self._expected_address():
            raise InstrumentProtocolError(
                f"Reply to {cmd!r} came from pump address {addr}, expected {self._expected_address()}. "
                "Check the `pump_address` option."
            )
        return Reply(addr, m["prompt"], m["alarm"], m["data"].strip())

    def command(self, cmd: str, timeout: float | None = None) -> Reply:
        """Send ``cmd``; raise on alarms and command errors.

        A power-on reset alarm (``A?R``) is acknowledged by any reply, so the command is sent a
        second time in that case (the pump does not execute commands while an alarm is pending).
        """
        with self.t.lock:
            reply = self.send(cmd, timeout)
            if reply.alarm == "R":
                self.reset_alarm_seen = True
                reply = self.send(cmd, timeout)
        if reply.alarm:
            raise InstrumentProtocolError(
                f"Pump replied with alarm A?{reply.alarm} to {cmd!r}: "
                f"{ALARMS.get(reply.alarm, 'unknown alarm')}. The alarm is now acknowledged; "
                "fix the cause before starting the pump again."
            )
        if reply.data.startswith("?"):
            code = reply.data[1:].strip()
            raise InstrumentProtocolError(
                f"Pump rejected {cmd!r} (reply '?{code}'): {ERRORS.get(code, 'unknown error')}."
            )
        return reply

    def status(self) -> Reply:
        """An empty packet is a status query (manual 10.4). Alarms are returned, not raised."""
        return self.send("")

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "New Era Pump Systems"}
        reply = self.command("VER")
        m = _VERSION_RE.match(reply.data)
        if m:
            info["model"] = f"NE-{m['model']}"
            info["firmware"] = m["fw"]
        else:
            info["version"] = reply.data
        info["network_address"] = str(self._expected_address())
        return info

    # ------------------------------------------------------------ settings

    def diameter_mm(self) -> float:
        return self._float("DIA", self.command("DIA").data)

    def set_diameter(self, diameter_mm: float) -> str:
        text = format_float(diameter_mm)
        self.command(f"DIA{text}")
        return text

    def rate(self) -> tuple[float, str]:
        """Current rate setting (or actual rate while pumping) as (value, units)."""
        data = self.command("RAT").data
        m = _RATE_RE.match(data)
        if not m or not m["units"]:
            raise InstrumentProtocolError(f"Unexpected reply to 'RAT': {data!r}")
        return float(m["value"]), m["units"]

    def volume(self) -> tuple[float, str]:
        """'Volume to be dispensed' of the selected phase as (value, units 'UL'|'ML')."""
        data = self.command("VOL").data
        m = _VOLUME_RE.match(data)
        if not m:
            raise InstrumentProtocolError(f"Unexpected reply to 'VOL': {data!r}")
        return float(m["value"]), m["units"]

    def direction(self) -> str:
        data = self.command("DIR").data.strip()
        if data not in {"INF", "WDR", "STK"}:
            raise InstrumentProtocolError(f"Unexpected reply to 'DIR': {data!r}")
        return data

    def dispensed(self) -> tuple[float, float, str]:
        """Accumulated (infused, withdrawn, units) volumes (``DIS``)."""
        data = self.command("DIS").data
        m = _DISPENSED_RE.match(data)
        if not m:
            raise InstrumentProtocolError(f"Unexpected reply to 'DIS': {data!r}")
        return float(m["inf"]), float(m["wdr"]), m["units"]

    def clear_dispensed(self) -> None:
        with self.t.lock:
            self.command("CLDINF")
            self.command("CLDWDR")

    @staticmethod
    def _float(cmd: str, data: str) -> float:
        try:
            return float(data)
        except ValueError as exc:
            raise InstrumentProtocolError(f"Expected a number in reply to {cmd!r}, got {data!r}") from exc

    # ------------------------------------------------------------ pumping

    def start_pumping(self, direction: str, volume_ml: float, rate_ml_min: float) -> DispensePlan:
        """Program a single-phase dispense (phase 1 = RATE, phase 2 = STOP) and start it.

        The pump must be stopped. Phases 1 and 2 of the pump's stored Pumping Program are
        overwritten so that the pump stops after ``volume_ml``.
        """
        if direction not in {"INF", "WDR"}:
            raise ValueError("direction must be 'INF' or 'WDR'")
        with self.t.lock:
            reset_cleared = False
            st = self.status()
            if st.alarm == "R":
                reset_cleared = True
                self.reset_alarm_seen = True
                st = self.status()
            if st.alarm:
                raise InstrumentProtocolError(
                    f"Pump reported alarm A?{st.alarm}: {ALARMS.get(st.alarm, 'unknown alarm')}. "
                    "The alarm is now acknowledged; check the syringe and tubing, then try again."
                )
            if st.prompt in {"I", "W", "X", "T", "U"}:
                raise InstrumentProtocolError(
                    f"The pump is {st.state}. Stop it with `stop_pump` before starting a new dispense."
                )
            if st.prompt == "P":
                self.command("STP")  # cancel the pause so RUN starts a fresh program at phase 1
            diameter = self.diameter_mm()
            if diameter <= 0:
                raise InstrumentProtocolError("No syringe diameter is set. Call `set_syringe` first.")

            has_program = self._select_phase(1)
            if has_program:
                self.command("FUNRAT")
            rate_text, units, actual = choose_rate_units(rate_ml_min)
            try:
                self.command(f"RAT{rate_text}{units}")
            except InstrumentProtocolError as exc:
                if "OOR" not in str(exc):
                    raise
                lo, hi = ne1000_rate_range_ml_min(diameter)
                raise InstrumentProtocolError(
                    f"The pump refused a rate of {rate_ml_min:g} mL/min as out of range for a "
                    f"{diameter:g} mm syringe. (On an NE-1000 this syringe allows about "
                    f"{lo * 1000:.3g} µL/min to {hi:.3g} mL/min; other models differ.)"
                ) from exc
            _, vol_units = self.volume()
            vol_text = format_float(volume_ml / VOLUME_UNITS[vol_units])
            self.command(f"VOL{vol_text}")
            self.command(f"DIR{direction}")
            if has_program:
                self._select_phase(2)
                self.command("FUNSTP")
                self._select_phase(1)
            self.command("RUN")
        return DispensePlan(
            direction=direction,
            volume_ml=float(vol_text) * VOLUME_UNITS[vol_units],
            rate_ml_min=actual,
            rate_text=f"{rate_text}{units}",
            volume_text=f"{vol_text}{vol_units}",
            diameter_mm=diameter,
            reset_alarm_cleared=reset_cleared,
        )

    def _select_phase(self, phase: int) -> bool:
        """Select a program phase. Returns False if the pump has no Pumping Program (``PHN`` unknown)."""
        try:
            self.command(f"PHN{phase}")
        except InstrumentProtocolError as exc:
            if "not recognised" in str(exc):
                return False
            raise
        return True

    def stop(self) -> Reply:
        """Stop pumping. A second STP cancels the resulting pause so nothing can resume it."""
        with self.t.lock:
            for _ in range(3):
                self.send("STP")
                st = self.status()
                if st.alarm:  # that reply acknowledged the alarm; read the real state
                    st = self.status()
                if st.prompt not in {"I", "W", "X", "T", "U", "P"}:
                    break
        return st

    def close(self) -> None:
        self.t.close()
