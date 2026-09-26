"""MT-SICS driver for Mettler Toledo balances.

Implements the MT-SICS level 0-2 commands documented in the
"MT-SICS Reference Manual for Excellence Balances" (Mettler Toledo 11780711).
Commands and replies are ASCII lines terminated by CR LF. Replies start with
the command identifier and a status character, e.g. ``S S     100.00 g``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from labmcp import InstrumentProtocolError, Transport

_WEIGHT_RE = re.compile(r"^(?P<id>[A-Z0-9]+)\s+(?P<status>[SDA])\s+(?P<value>[-+]?\d+(?:\.\d*)?)\s*(?P<unit>\S+)\s*$")

_STATUS_MEANING = {
    "I": "command not executable right now (the balance is busy, or stability was not reached in time)",
    "+": "balance is in the overload range (too much weight on the pan)",
    "-": "balance is in the underload range (the pan may be missing)",
    "L": "command understood but parameter wrong, or not possible on this balance",
}

_ERRORS = {
    "ES": "syntax error: the balance did not recognise the command",
    "ET": "transmission error: check baud rate / parity settings",
    "EL": "logical error: the balance cannot execute this command",
}

DOOR_POSITIONS = {0: "closed", 1: "right_open", 2: "left_open", 8: "error", 9: "intermediate"}


def _decimal(value: float) -> str:
    """Shortest exact decimal representation of ``value`` without an exponent."""
    text = format(Decimal(repr(float(value))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass
class Weight:
    value: float
    unit: str
    stable: bool


class MTSICSBalance:
    def __init__(self, transport: Transport) -> None:
        self.t = transport

    # ------------------------------------------------------------ low level

    def command(self, cmd: str, timeout: float | None = None) -> str:
        """Send ``cmd`` and return the reply, raising on MT-SICS error replies."""
        reply = self.t.query(cmd, timeout).strip()
        return self._check(cmd, reply)

    def _check(self, cmd: str, reply: str) -> str:
        if reply in _ERRORS:
            raise InstrumentProtocolError(f"Balance replied {reply} to {cmd!r}: {_ERRORS[reply]}")
        parts = reply.split()
        if len(parts) == 2 and parts[1] in _STATUS_MEANING:
            raise InstrumentProtocolError(
                f"Balance replied {reply!r} to {cmd!r}: {_STATUS_MEANING[parts[1]]}"
            )
        return reply

    @staticmethod
    def _parse_weight(cmd: str, reply: str) -> Weight:
        m = _WEIGHT_RE.match(reply)
        if not m:
            raise InstrumentProtocolError(f"Unexpected reply to {cmd!r}: {reply!r}")
        return Weight(float(m["value"]), m["unit"], m["status"] in {"S", "A"})

    @staticmethod
    def _quoted(reply: str) -> list[str]:
        return re.findall(r'"([^"]*)"', reply)

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        info: dict[str, str] = {"manufacturer": "Mettler Toledo"}
        for key, cmd in (("serial", "I4"), ("balance_data", "I2"), ("software", "I3")):
            try:
                quoted = self._quoted(self.command(cmd))
                info[key] = quoted[0] if quoted else ""
            except InstrumentProtocolError:
                pass
        try:
            quoted = self._quoted(self.command("I11"))
            if quoted:
                info["model"] = quoted[0]
        except InstrumentProtocolError:
            pass
        try:
            quoted = self._quoted(self.command("I1"))
            if quoted:
                info["mt_sics_level"] = quoted[0]
        except InstrumentProtocolError:
            pass
        return info

    # ------------------------------------------------------------ weighing

    def weight(self, stable: bool = True, timeout: float = 15.0) -> Weight:
        cmd = "S" if stable else "SI"
        reply = self.command(cmd, timeout=timeout)
        return self._parse_weight(cmd, reply)

    def zero(self, immediately: bool = False, timeout: float = 15.0) -> bool:
        """Zero the balance. Returns whether the weight was stable when zeroed."""
        cmd = "ZI" if immediately else "Z"
        reply = self.command(cmd, timeout=timeout)
        status = reply.split()[1] if len(reply.split()) > 1 else ""
        if status not in {"A", "S", "D"}:
            raise InstrumentProtocolError(f"Unexpected reply to {cmd!r}: {reply!r}")
        return status in {"A", "S"}

    def tare(self, immediately: bool = False, timeout: float = 15.0) -> Weight:
        cmd = "TI" if immediately else "T"
        return self._parse_weight(cmd, self.command(cmd, timeout=timeout))

    def tare_value(self) -> Weight:
        return self._parse_weight("TA", self.command("TA"))

    def preset_tare(self, value: float, unit: str) -> Weight:
        # Plain decimal notation without precision loss: ``:g`` would send "52.1873" for
        # 52.18734 and "1e-05" for 0.00001, which MT-SICS does not accept as a number.
        cmd = f"TA {_decimal(value)} {unit}"
        return self._parse_weight(cmd, self.command(cmd))

    def clear_tare(self) -> None:
        self.command("TAC")

    def reset(self) -> str:
        """``@``: reset to power-on state (no zeroing). Returns the serial number."""
        reply = self.command("@")
        quoted = self._quoted(reply)
        return quoted[0] if quoted else reply

    # ------------------------------------------------------------ display

    def display_text(self, text: str) -> None:
        safe = text.replace('"', "'")
        self.command(f'D "{safe}"')

    def display_weight(self) -> None:
        self.command("DW")

    # ------------------------------------------------------------ level 2

    def internal_adjustment(self, timeout: float = 300.0) -> None:
        """``C3``: adjust with the internal reference weight. Blocks until done."""
        with self.t.lock:
            first = self.command("C3", timeout=10.0)
            if first != "C3 B":
                raise InstrumentProtocolError(f"Adjustment did not start: {first!r}")
            final = self.t.read(timeout=timeout).strip()
        if final != "C3 A":
            raise InstrumentProtocolError(
                f"Adjustment did not complete ({final!r}); stability may not have been reached."
            )

    def door_position(self) -> str:
        reply = self.command("WS")
        parts = reply.split()
        if len(parts) < 3 or not parts[2].isdigit():
            raise InstrumentProtocolError(f"Unexpected reply to 'WS': {reply!r}")
        return DOOR_POSITIONS.get(int(parts[2]), f"unknown ({parts[2]})")

    def set_door(self, position: int) -> None:
        self.command(f"WS {position}", timeout=10.0)

    def temperature_c(self) -> list[float]:
        """``M28``: read the built-in temperature probe(s) in °C."""
        with self.t.lock:
            self.t.write("M28")
            temps: list[float] = []
            while True:
                reply = self._check("M28", self.t.read().strip())
                parts = reply.split()
                if len(parts) < 4:
                    raise InstrumentProtocolError(f"Unexpected reply to 'M28': {reply!r}")
                temps.append(float(parts[3]))
                if parts[1] == "A":
                    return temps

    def close(self) -> None:
        self.t.close()
