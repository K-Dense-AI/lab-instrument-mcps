"""Driver for JULABO heating and refrigerated circulators (JULABO interface commands).

Commands, status messages, alarm codes and interface settings were verified in the
"Interface commands" appendix (chapter 11) of these JULABO original operating manuals:

* CORIO CD, 1.950.0800.us.V10 (03/2022),
  https://pim-resources.coleparmer.com/instruction-manual/corio-cd-circulator-manual.pdf
* CORIO CP, 1.950.0900.us.V04 (04/2022),
  https://pim-resources.coleparmer.com/instruction-manual/corio-cp-operating-manual-1-950-0900-us.pdf
* MAGIO MS, 1.950.1700.us.V05 (05/2022),
  https://pim-resources.coleparmer.com/instruction-manual/magio-ms-operating-manual-1-950-1700-us-v05.pdf
* DYNEO DD, 1.950.1300.us.V03 (05/2022),
  https://pim-resources.coleparmer.com/instruction-manual/dyneo-dd-operating-manual-1-950-1300-us-v03.pdf

Wire format: a command is sent as ``command CR`` or ``command SPACE parameter CR``
(e.g. ``out_sp_00 55.5``); IN commands answer with one line ending in CR LF (``55.5``).
OUT commands send no reply and are only accepted in remote-control mode; whether one was
rejected is reported by the next ``status`` query (``-08 INVALID COMMAND``, ``-10 VALUE TOO
SMALL``...), which also reports pending alarms. RS-232 factory settings: 4800 baud, 7 data
bits, even ("straight") parity, 1 stop bit, hardware (RTS/CTS) handshake. On USB the
circulator is a virtual COM port and these settings do not matter.

Command availability differs by family: CORIO CD only has in_pv_00/01/03/04, in_sp_00,
in_mode_05, out_sp_00, out_mode_05, version and status. CORIO CP adds the warning limits
(in_sp_03/04); MAGIO and DYNEO add the external Pt100 sensor (in_pv_02) and more.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass

from labmcp import InstrumentError, InstrumentProtocolError, InstrumentTimeout, Transport

log = logging.getLogger("labmcp.julabo")

_STATUS_RE = re.compile(r"^\s*(-?\d+)\s*(.*?)\s*$")
_NUMBER_RE = re.compile(r"^\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*$")

#: Status messages (manual 11.1.4). Codes >= 0 are operating states.
STATES = {
    0: ("MANUAL STOP", "standby, manual operation (remote control is not enabled)"),
    1: ("MANUAL START", "running, manual operation (remote control is not enabled)"),
    2: ("REMOTE STOP", "standby, remote control operation"),
    3: ("REMOTE START", "running, remote control operation"),
}

#: Replies to a rejected command (manual 11.1.4).
COMMAND_ERRORS = {
    -8: "the circulator did not recognise the last command",
    -9: "the last command is not permitted in the current operating mode (is remote control enabled?)",
    -10: "the last value sent was too small",
    -11: "the last value sent was too large",
    -13: "the value is not within the temperature limits set on the circulator",
}

#: Alarms and warnings (manual 11.2; the list depends on the model).
ALARMS = {
    -1: "bath fluid level too low; top up the bath fluid and check the hoses",
    -3: "measured temperature is above the high-temperature warning limit",
    -4: "measured temperature is below the low-temperature warning limit",
    -5: "working temperature sensor cable broken or short-circuited",
    -6: "temperature difference between working sensor and high-temperature protection sensor too large "
    "(increase pump capacity)",
    -14: "the set excess-temperature protection value has been exceeded",
    -15: "external temperature sensor line short-circuited or interrupted",
    -33: "high-temperature protection sensor line short-circuited or interrupted",
    -38: "setpoint is set to the external sensor but no signal is available",
    -40: "low-level early warning: bath fluid level is critical",
    -41: "high-level early warning: bath fluid level is critical",
    -60: "internal read/write error; switch off at the mains, wait 4 s, switch on",
    -61: "communication (CAN bus) error between circulator and refrigeration unit",
    -62: "CAN bus error; switch off at the mains, wait 4 s, switch on",
    -63: "the watchdog function has responded",
    -70: "units with incompatible voltage/frequency variants connected, or incorrectly configured",
    -72: "configuration between circulator and refrigeration unit failed",
    -83: "excessive power consumption via the USB-A port",
    -108: "alarm latch of the protective equipment is still active; power-cycle the unit",
    -116: "alarm latch of the protective equipment is still active; power-cycle the unit",
    -143: "over-temperature alarm limit exceeded",
    -144: "under-temperature alarm limit exceeded",
    -421: "ambient temperature outside the specification",
    -427: "pressure sensor detects excessive condensation pressure",
    -431: "maximum permissible compressor current exceeded",
    -503: "external setpoint via EPROG selected but no analog module connected",
    -504: "external actuating variable via EPROG selected but no analog module connected",
    -505: "the analog module transmits an invalid setpoint",
    -1109: "bath fluid viscosity too high or circulation rate too low",
    -1305: "pump speed limit of the heating block not reached (motor defective or fluid too viscous)",
    -1427: "pressure sensor detects excessive condensation pressure",
    -1431: "compressor current below the permissible minimum",
    -1501: "timeout on the serial interface (watchdog: send the setpoint at least every 30 s)",
    -2426: "evaporation temperature below the warning threshold",
}


@dataclass
class Status:
    code: int
    text: str
    kind: str  # "state" | "command_error" | "alarm"
    meaning: str

    @property
    def label(self) -> str:
        """The code and text as the circulator shows them, e.g. ``03 REMOTE START`` / ``-08 ...``."""
        return f"{self.code:03d} {self.text}" if self.code < 0 else f"{self.code:02d} {self.text}"

    @property
    def remote(self) -> bool | None:
        return self.code in (2, 3) if self.kind == "state" else None

    @property
    def running(self) -> bool | None:
        return self.code in (1, 3) if self.kind == "state" else None


def parse_status(reply: str) -> Status:
    """Parse a ``status`` reply such as ``03 REMOTE START`` or ``-01 ...``."""
    m = _STATUS_RE.match(reply)
    if not m:
        raise InstrumentProtocolError(f"Unexpected reply to 'status': {reply!r}")
    code, text = int(m.group(1)), m.group(2)
    if code in STATES:
        return Status(code, text or STATES[code][0], "state", STATES[code][1])
    if code in COMMAND_ERRORS:
        return Status(code, text, "command_error", COMMAND_ERRORS[code])
    if code < 0:
        return Status(code, text, "alarm", ALARMS.get(code, "alarm/warning not listed in the manual"))
    return Status(code, text, "state", "unknown operating state")


class JulaboCirculator:
    def __init__(self, transport: Transport, command_delay_s: float = 0.25) -> None:
        self.t = transport
        #: Pause after each OUT command before the next command (see README "Notes").
        self.command_delay_s = command_delay_s
        #: Set by :meth:`stop` so a running wait loop ends early.
        self.abort = threading.Event()
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        #: Optional commands this model did not answer (e.g. in_sp_03 on a CORIO CD).
        self.unsupported: set[str] = set()

    # ------------------------------------------------------------ low level

    def query(self, cmd: str, timeout: float | None = None) -> str:
        with self.t.lock:
            self.t.flush_input()  # the circulator never sends unsolicited data
            reply = self.t.query(cmd, timeout).strip()
        if not reply:
            raise InstrumentProtocolError(f"Circulator sent an empty reply to {cmd!r}.")
        return reply

    def query_number(self, cmd: str) -> float:
        reply = self.query(cmd)
        if not _NUMBER_RE.match(reply):
            if _STATUS_RE.match(reply) and any(c.isalpha() for c in reply):
                st = parse_status(reply)
                raise InstrumentProtocolError(f"Circulator replied {reply!r} to {cmd!r}: {st.meaning}.")
            raise InstrumentProtocolError(f"Circulator replied {reply!r} to {cmd!r}; expected a number.")
        return float(reply)

    def optional_number(self, cmd: str) -> float | None:
        """Query a command that not every model has; ``None`` if this circulator lacks it."""
        if cmd in self.unsupported:
            return None
        try:
            return self.query_number(cmd)
        except (InstrumentTimeout, InstrumentProtocolError):
            self.unsupported.add(cmd)
        # Read (and so clear) the "-08 INVALID COMMAND" the device may now report.
        with contextlib.suppress(InstrumentError):
            self.status()
        return None

    def send(self, cmd: str) -> Status:
        """Send an OUT command, then query ``status`` and raise if the command was rejected."""
        with self.t.lock:
            self.t.write(cmd)
            if self.command_delay_s:
                time.sleep(self.command_delay_s)
            st = self.status()
        if st.kind == "command_error":
            raise InstrumentProtocolError(
                f"Circulator rejected {cmd!r}: '{st.label}' ({st.meaning}). Nothing was changed."
            )
        return st

    # ------------------------------------------------------------ identity / status

    def version(self) -> str:
        return self.query("version")

    def status(self) -> Status:
        return parse_status(self.query("status"))

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "JULABO"}
        try:
            info["version"] = self.version()
        except InstrumentError as exc:
            info["version"] = f"unknown ({exc})"
        try:
            st = self.status()
            info["status"] = st.label
        except InstrumentError:
            pass
        return info

    def require_remote(self) -> Status:
        st = self.status()
        if st.kind == "state" and st.remote is False:
            raise InstrumentProtocolError(
                f"The circulator is in manual mode ('{st.label}'), so it ignores remote "
                "commands. Enable remote control on the circulator first (CORIO: MENU > IntE > rEM > "
                "USb or 232; MAGIO/DYNEO: Main menu > Connect unit > Remote control)."
            )
        return st

    # ------------------------------------------------------------ readings

    def bath_temperature_c(self) -> float:
        """``in_pv_00``: actual (bath/internal) temperature."""
        return self.query_number("in_pv_00")

    def heating_power_pct(self) -> float:
        """``in_pv_01``: current actuating variable in % (negative = cooling)."""
        return self.query_number("in_pv_01")

    def external_temperature_c(self) -> float:
        """``in_pv_02``: external Pt100 sensor (MAGIO, DYNEO; not CORIO)."""
        return self.query_number("in_pv_02")

    def safety_sensor_temperature_c(self) -> float:
        """``in_pv_03``: temperature of the high-temperature safety sensor."""
        return self.query_number("in_pv_03")

    def excess_temperature_protection_c(self) -> float:
        """``in_pv_04``: current setting of the high-temperature safety function."""
        return self.query_number("in_pv_04")

    def setpoint_c(self) -> float:
        return self.query_number("in_sp_00")

    def warning_limits_c(self) -> tuple[float | None, float | None]:
        """``in_sp_03`` / ``in_sp_04``: (high, low) temperature warning limits (not on CORIO CD)."""
        return self.optional_number("in_sp_03"), self.optional_number("in_sp_04")

    def is_running(self) -> bool:
        """``in_mode_05``: 1 = temperature control started, 0 = stopped."""
        value = self.query_number("in_mode_05")
        if value not in (0, 1):
            raise InstrumentProtocolError(f"Unexpected in_mode_05 value {value:g}; expected 0 or 1.")
        return value == 1

    # ------------------------------------------------------------ control

    def set_setpoint(self, celsius: float) -> Status:
        self.require_remote()
        st = self.send(f"out_sp_00 {celsius:.2f}")
        reported = self.setpoint_c()
        if abs(reported - celsius) > 0.051:
            raise InstrumentProtocolError(
                f"Setpoint not accepted: circulator reports {reported:g} °C after 'out_sp_00 {celsius:.2f}'."
            )
        return st

    def start(self) -> Status:
        self.require_remote()
        self.abort.clear()
        st = self.send("out_mode_05 1")
        if not self.is_running():
            raise InstrumentProtocolError(
                f"The circulator did not start (status '{st.label}': {st.meaning})."
            )
        return st

    def stop(self) -> bool:
        """Stop temperature control. Retries once; returns True if in_mode_05 confirms it."""
        self.abort.set()
        for _ in range(2):
            try:
                with self.t.lock:
                    self.t.write("out_mode_05 0")
                    if self.command_delay_s:
                        time.sleep(self.command_delay_s)
                    if not self.is_running():
                        return True
            except InstrumentError as exc:
                log.warning("Stop attempt failed: %s", exc)
        return False

    # ------------------------------------------------------------ keep-alive (device watchdog)

    def start_keepalive(self, interval_s: float) -> None:
        """Query ``status`` every ``interval_s`` so a watchdog configured on the circulator
        (MAGIO/DYNEO, restart mode "all valid commands") sees continuous traffic."""
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive, args=(interval_s,), name="julabo-keepalive", daemon=True
        )
        self._keepalive_thread.start()

    def _keepalive(self, interval_s: float) -> None:
        while not self._keepalive_stop.wait(interval_s):
            try:
                self.status()
            except InstrumentError as exc:
                log.warning("JULABO keep-alive query failed: %s", exc)

    def close(self) -> None:
        self._keepalive_stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=5)
        self.t.close()
