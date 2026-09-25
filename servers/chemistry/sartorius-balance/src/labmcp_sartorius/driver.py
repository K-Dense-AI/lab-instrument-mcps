"""SBI (Sartorius Balance Interface) driver for Sartorius laboratory balances.

Commands, data formats and interface settings were verified in:

* "Entris II - Description of the Interface", Sartorius Technical Note (10/2020),
  https://www.sartorius.hr/media/dypfvdsn/entris-ii-technical-note-en-sartorius.pdf
* "User Manual Secura, Quintix, Practum" (WSE6004-e181008), chapter 10.3 "Interface Specification",
  https://api.sartorius.com/document-hub/dam/download/21625/Manual_Secura_Quintix_Practum_WSE6004-e181008.pdf
* "Operating Instructions Cubis Series MSE" (WMS6004-e190715), "Data Interfaces",
  https://api.sartorius.com/document-hub/dam/download/20494/Manual_Cubis_MSE_WMS6004-e.pdf
* "Operating Instructions Cubis MCA" (WMC6028-e211007), "Connections / SBI Protocol" menu,
  https://www.sartorius.com/download/920578/manual-cubis-mca-micro-balances-wmc6028-e-pdf-data.pdf
* "Sartorius Comparator Interface Description for the CC Model Series" (98647-000-53),
  https://api.sartorius.com/document-hub/dam/download/22650/MAN-CC_Interface-e.pdf
  (source of the rule "If the weighing system has not stabilized, no unit symbol is output")

Commands are ``ESC <char> [CR LF]`` (format 1, e.g. ``ESC P``) or ``ESC <char><digit>_ [CR LF]``
(format 2, e.g. ``ESC x1_``). Only ``ESC P`` (print) and the ``ESC x#_`` info commands produce
output; the others (tare, zero, adjustment, filter, key lock) are silent.

A weight line is 16 characters (14 + CR LF)::

    pos 1      sign (+, - or space)
    pos 2-10   value, right-aligned, leading zeros as spaces
    pos 11     space
    pos 12-14  unit symbol, or spaces while the reading is not stable
    pos 15-16  CR LF

or 22 characters, where a 6-character ID code (``N``, ``G#``, ``T``, ``Stat``...) precedes
the same 16 characters. Special lines report ``High`` (overload), ``Low`` (underload),
``Cal.Ext.``/``Cal.Int.`` (adjustment) and errors ``Err ###``, ``APP.ERR``, ``DIS.ERR``,
``PRT.ERR``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport

ESC = "\x1b"

AMBIENT_FILTERS = {"very_stable": "K", "stable": "L", "unstable": "M", "very_unstable": "N"}

_VALUE_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$")
# Position-independent fallback, e.g. for "output in one line with full length" (Cubis II).
_LOOSE_RE = re.compile(
    r"^(?P<id>.*?)\s*(?P<sign>[+-])?\s*(?P<value>\d+(?:[.,]\d*)?|[.,]\d+)\s*(?P<unit>[^\s\d.,+\-]\S{0,3})?\s*$"
)
_ERR_RE = re.compile(r"\bErr\s*(\d+)", re.IGNORECASE)
_APP_ERR_RE = re.compile(r"\b(APP|DIS|PRT)\.ERR", re.IGNORECASE)

_SPECIAL = {
    "High": "the balance is overloaded (High): remove weight from the pan",
    "Low": "the balance is in underload (Low): is the weighing pan in place and free?",
}
_APP_ERR = {"APP": "application error", "DIS": "display error", "PRT": "printer/output error"}


@dataclass
class SBIReading:
    value: float
    unit: str | None  # None when the balance sent no unit (it omits it while unstable)
    stable: bool
    ident: str  # 6-character ID code of the 22-character format ("" for 16 characters)
    decimals: int
    raw: str


class BalanceBusy(InstrumentProtocolError):
    """The balance sent an adjustment status line (``Cal.Int.`` / ``Cal.Ext.``) instead of a weight."""


def parse_sbi_line(line: str) -> SBIReading:
    """Parse one SBI data line (without CR LF) into a reading, or raise a helpful error."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        raise InstrumentProtocolError("The balance sent an empty line.")
    for word, meaning in _SPECIAL.items():
        if re.search(rf"\b{word}\b", raw):
            raise InstrumentProtocolError(f"Balance sent {raw.strip()!r}: {meaning}.")
    if m := _ERR_RE.search(raw):
        raise InstrumentProtocolError(
            f"Balance sent {raw.strip()!r}: error {m.group(1)}; see the troubleshooting table in the "
            "balance's operating instructions."
        )
    if m := _APP_ERR_RE.search(raw):
        raise InstrumentProtocolError(f"Balance sent {raw.strip()!r}: {_APP_ERR[m.group(1).upper()]}.")
    if "Cal." in raw:
        raise BalanceBusy(f"Balance sent {raw.strip()!r}: an adjustment/calibration is in progress.")

    if len(raw) in (14, 20):  # the documented fixed-width 16- and 22-character formats
        ident, body = (raw[:6].strip(), raw[6:]) if len(raw) == 20 else ("", raw)
        sign, number, gap, unit = body[0], body[1:10].replace(" ", ""), body[10], body[11:14].strip()
        if sign in "+- " and gap == " " and _VALUE_RE.match(number):
            return _reading(sign, number, unit, ident, raw)
    m = _LOOSE_RE.match(raw)
    if not m or m["id"].strip().startswith("Stat"):
        raise InstrumentProtocolError(f"Unexpected SBI data from the balance: {raw!r}")
    return _reading(m["sign"] or "+", m["value"], (m["unit"] or "").strip(), m["id"].strip(), raw)


def _reading(sign: str, number: str, unit: str, ident: str, raw: str) -> SBIReading:
    number = number.replace(",", ".")
    value = float(number)
    if sign == "-" and not number.startswith("-"):
        value = -value
    decimals = len(number.split(".")[1]) if "." in number else 0
    return SBIReading(value, unit or None, bool(unit), ident, decimals, raw)


class SBIBalance:
    def __init__(self, transport: Transport, legacy: bool = False) -> None:
        self.t = transport
        #: Older balances (CP/CPA, LE...) only know ESC T for taring and have no ESC U / ESC V.
        self.legacy = legacy
        #: Last unit symbol seen (the balance omits it in unstable readings).
        self.last_unit: str | None = None

    # ------------------------------------------------------------ low level

    def send(self, command: str) -> None:
        """Send ``ESC <command> CR LF``. SBI control commands have no reply."""
        self.t.write(ESC + command)

    def _lines(self, command: str, timeout: float | None = None) -> list[str]:
        """Send a command and collect its output: one line, plus up to 5 more that follow within
        0.15 s of each other (e.g. the date/time line or a G#/T/N weighing block)."""
        with self.t.lock:
            self.t.flush_input()
            self.send(command)
            try:
                lines = [self.t.read(timeout)]
            except InstrumentTimeout as exc:
                raise InstrumentTimeout(
                    f"No reply to ESC {command}. Check that the balance's interface is set to SBI with "
                    "matching baud rate/parity, and that data output is 'manual without stability' "
                    f"(with 'after stability' the balance waits until the reading settles). ({exc})"
                ) from exc
            for _ in range(5):  # bounded, in case the balance is streaming (auto print on)
                try:
                    lines.append(self.t.read(0.15))
                except InstrumentTimeout:
                    break
            return lines

    # ------------------------------------------------------------ identity

    def info(self, number: int) -> str | None:
        """``ESC x#_``: 1 = model, 2 = serial number, 3 = balance software version."""
        try:
            lines = self._lines(f"x{number}_", timeout=1.5)
        except InstrumentTimeout:
            return None
        return " ".join(line.strip() for line in lines if line.strip()) or None

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "Sartorius", "protocol": "SBI"}
        for key, number in (("model", 1), ("serial", 2), ("software", 3)):
            value = self.info(number)
            if value:
                info[key] = value
        return info

    # ------------------------------------------------------------ weighing

    def print_reading(self, timeout: float | None = None) -> SBIReading:
        """``ESC P``: one reading, stable or not (as configured, 'without stability')."""
        lines = [line for line in self._lines("P", timeout) if line.strip()]
        if not lines:
            raise InstrumentProtocolError("The balance answered ESC P with an empty line.")
        readings = [parse_sbi_line(line) for line in lines if not _is_info_line(line)]
        if not readings:
            raise InstrumentProtocolError(f"No weight value in the balance's output: {lines!r}")
        # A G#/T/N weighing block: report the net value.
        reading = next((r for r in readings if r.ident == "N"), readings[-1])
        if reading.unit:
            self.last_unit = reading.unit
        return reading

    def weight(self, stable: bool = True, timeout: float = 20.0) -> SBIReading:
        """Read the weight. With ``stable=True``, poll ``ESC P`` until the balance reports a
        stable value (unit symbol present) or ``timeout`` expires."""
        if not stable:
            return self.print_reading()
        deadline = time.monotonic() + timeout
        last: SBIReading | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                last = self.print_reading(timeout=max(0.5, remaining))
            except BalanceBusy:
                last = None
            else:
                if last.stable:
                    return last
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        where = f" (last reading {last.value:g}, unstable)" if last else ""
        raise InstrumentProtocolError(
            f"The balance did not report a stable reading within {timeout:g} s{where}. Check for "
            "draughts, vibration or a sample that is evaporating, or read with stable=false."
        )

    def tare(self) -> None:
        """``ESC U`` (tare key); ``ESC T`` on legacy balances."""
        self.send("T" if self.legacy else "U")

    def zero(self) -> None:
        """``ESC V`` (zero key). Not available on legacy balances."""
        if self.legacy:
            raise InstrumentProtocolError(
                "Legacy SBI balances have no separate zero command; use tare (ESC T), which zeroes "
                "when the pan is empty."
            )
        self.send("V")

    def start_internal_adjustment(self) -> None:
        """``ESC Z``: internal calibration/adjustment (balances with a built-in weight only)."""
        self.send("Z")

    def set_ambient_filter(self, level: str) -> None:
        """``ESC K/L/M/N``: adapt the filter to very stable ... very unstable conditions."""
        self.send(AMBIENT_FILTERS[level])

    def lock_keys(self, locked: bool) -> None:
        """``ESC O`` blocks the keypad, ``ESC R`` unblocks it."""
        self.send("O" if locked else "R")

    def close(self) -> None:
        self.t.close()


def _is_info_line(line: str) -> bool:
    """Date/time lines of the 'date & time, value' output format."""
    return bool(re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}:\d{2}", line)) and not re.search(
        r"\s(?:g|mg|kg|ct|lb|oz|ozt)\s*$", line
    )
