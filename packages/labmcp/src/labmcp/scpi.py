"""Helpers for SCPI / IEEE 488.2 instruments (oscilloscopes, DMMs, SMUs, PSUs, ...)."""

from __future__ import annotations

import re

from labmcp.errors import InstrumentProtocolError
from labmcp.transports.base import Transport
from labmcp.transports.sim import LineSimulator


class SCPIDriver:
    """Minimal base driver for IEEE 488.2 / SCPI instruments."""

    def __init__(self, transport: Transport) -> None:
        self.t = transport

    def write(self, command: str) -> None:
        self.t.write(command)

    def query(self, command: str, timeout: float | None = None) -> str:
        return self.t.query(command, timeout).strip()

    def query_float(self, command: str) -> float:
        reply = self.query(command)
        try:
            return float(reply.split(",")[0])
        except ValueError as exc:
            raise InstrumentProtocolError(f"Expected a number from {command!r}, got {reply!r}") from exc

    def query_int(self, command: str) -> int:
        return int(self.query_float(command))

    def query_bool(self, command: str) -> bool:
        reply = self.query(command).upper()
        if reply in {"1", "ON"}:
            return True
        if reply in {"0", "OFF"}:
            return False
        raise InstrumentProtocolError(f"Expected 0/1/ON/OFF from {command!r}, got {reply!r}")

    def identify(self) -> dict[str, str]:
        """Parse ``*IDN?`` into manufacturer/model/serial/firmware."""
        reply = self.query("*IDN?")
        parts = [p.strip() for p in reply.split(",")]
        parts += [""] * (4 - len(parts))
        return {
            "manufacturer": parts[0],
            "model": parts[1],
            "serial": parts[2],
            "firmware": ",".join(parts[3:]).strip(","),
        }

    def errors(self, max_errors: int = 20) -> list[str]:
        """Drain the SCPI error queue (``SYSTem:ERRor?``). Empty list means no errors."""
        out = []
        for _ in range(max_errors):
            reply = self.query("SYST:ERR?")
            code = reply.split(",", 1)[0].strip()
            if code.lstrip("+-").isdigit() and int(code) == 0:
                break
            out.append(reply)
        return out

    def check_errors(self, context: str = "") -> None:
        errs = self.errors()
        if errs:
            where = f" after {context!r}" if context else ""
            raise InstrumentProtocolError(f"Instrument reported error(s){where}: " + "; ".join(errs))

    def wait_complete(self, timeout: float = 10.0) -> None:
        """Block until pending operations complete (``*OPC?``)."""
        self.query("*OPC?", timeout=timeout)

    def query_block(self, command: str, timeout: float | None = None) -> bytes:
        """Query an IEEE 488.2 definite-length binary block (``#<n><len><data>``)."""
        with self.t.lock:
            self.t.write(command)
            head = self.t.read_bytes(2, timeout)
            if head[:1] != b"#":
                raise InstrumentProtocolError(f"Expected binary block from {command!r}, got {head!r}")
            ndigits = int(head[1:2])
            if ndigits == 0:  # indefinite-length block
                return self.t.read_until(self.t.read_termination.encode(), timeout)
            length = int(self.t.read_bytes(ndigits, timeout))
            data = self.t.read_bytes(length, timeout)
            try:  # consume the trailing terminator if present
                self.t.read_bytes(len(self.t.read_termination), 0.05)
            except Exception:
                pass
            return data

    def close(self) -> None:
        self.t.close()


class SCPISimulator(LineSimulator):
    """Base simulator implementing the IEEE 488.2 common commands.

    Subclasses set ``idn`` and implement :meth:`command` for everything else.
    Commands are normalised to upper case with the leading colon removed.
    Unknown commands push ``-113,"Undefined header"`` onto the error queue,
    mimicking real instruments.
    """

    idn = "LabMCP,Simulated SCPI Instrument,SIM0001,1.0"

    def __init__(self) -> None:
        self.error_queue: list[str] = []

    def handle(self, command: str) -> str | list[str] | None:
        replies: list[str] = []
        for part in command.split(";"):
            part = part.strip()
            if not part:
                continue
            reply = self._dispatch(part)
            if reply is not None:
                replies.append(reply)
        return ";".join(replies) if replies else None

    def _dispatch(self, cmd: str) -> str | None:
        head, _, arg = cmd.partition(" ")
        key = head.upper().lstrip(":")
        if key == "*IDN?":
            return self.idn
        if key in {"*RST", "*CLS"}:
            if key == "*CLS":
                self.error_queue.clear()
            else:
                self.reset()
            return None
        if key == "*OPC?":
            return "1"
        if key in {"SYST:ERR?", "SYSTEM:ERROR?", "SYST:ERR:NEXT?", "SYSTEM:ERROR:NEXT?"}:
            return self.error_queue.pop(0) if self.error_queue else '0,"No error"'
        try:
            reply = self.command(key, arg.strip())
        except _Undefined:
            self.error_queue.append('-113,"Undefined header"')
            return None
        except ValueError:
            self.error_queue.append('-224,"Illegal parameter value"')
            return None
        return reply

    def reset(self) -> None:
        """Called on ``*RST``."""

    def command(self, key: str, arg: str) -> str | None:
        """Handle an instrument-specific command. Raise :meth:`undefined` if unknown."""
        raise _Undefined()

    @staticmethod
    def undefined() -> Exception:
        return _Undefined()

    @staticmethod
    def matches(key: str, pattern: str) -> bool:
        """Match SCPI short/long forms: ``matches("SOUR:VOLT", "SOURce:VOLTage")``.

        In ``pattern`` the upper-case letters are the mandatory short form, the
        lower-case tail is optional; ``[...]`` marks optional nodes; ``<n>``
        matches a channel number.
        """
        return _compile(pattern).fullmatch(key) is not None


class _Undefined(Exception):
    pass


_CACHE: dict[str, re.Pattern[str]] = {}


def _compile(pattern: str) -> re.Pattern[str]:
    if pattern in _CACHE:
        return _CACHE[pattern]
    rx = re.compile(_compile_optional(pattern), re.IGNORECASE)
    _CACHE[pattern] = rx
    return rx


def _compile_optional(pattern: str) -> str:
    """Translate ``[...]`` optional groups, which may nest (``[SENSe[1]:]``)."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern[i] == "[":
            depth, j = 1, i + 1
            while j < len(pattern) and depth:
                depth += {"[": 1, "]": -1}.get(pattern[j], 0)
                j += 1
            if depth:
                raise ValueError(f"Unbalanced '[' in SCPI pattern {pattern!r}")
            out += "(?:" + _compile_optional(pattern[i + 1 : j - 1]) + ")?"
            i = j
            continue
        j = i
        while j < len(pattern) and pattern[j] != "[":
            j += 1
        out += _compile_nodes(pattern[i:j])
        i = j
    return out


def _compile_nodes(text: str) -> str:
    out = ""
    for token in re.split(r"(:|\?|<n>)", text):
        if token in {":", "?"}:
            out += re.escape(token)
        elif token == "<n>":
            out += r"\d*"
        elif token:
            short = "".join(c for c in token if c.isupper() or c.isdigit() or c == "*")
            tail = token[len(short) :]
            opt = "".join(f"(?:{re.escape(c)}" for c in tail) + ")?" * len(tail)
            out += re.escape(short) + opt
    return out

