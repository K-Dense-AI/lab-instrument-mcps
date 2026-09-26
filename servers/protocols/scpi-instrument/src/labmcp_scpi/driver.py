"""Generic IEEE 488.2 / SCPI driver with a command policy.

References:

* "Standard Commands for Programmable Instruments (SCPI), Version 1999.0",
  SCPI Consortium / IVI Foundation, May 1999 (Vol. 1 Syntax & Style, Vol. 2
  Command Reference): https://www.ivifoundation.org/downloads/SCPI/scpi-99.pdf
  - Vol. 1, 4.1.1: mandatory IEEE 488.2 common commands (*CLS, *ESE, *ESR?,
    *IDN?, *OPC, *OPC?, *RST, *SRE, *STB?, *TST?, *WAI).
  - Vol. 1, 6.2: long/short form mnemonics and compound headers.
  - Vol. 2, 21.8: SYSTem:ERRor[:NEXT]? returns ``<code>,"<description>"``, FIFO,
    ``0,"No error"`` when empty, -350 on overflow.
  - Vol. 2, 24.5: ABORt returns the trigger system to IDLE and aborts sweeps
    and measurements in progress.
  - Vol. 2, 9: FORMat[:DATA] REAL/INTeger data is sent as an IEEE 488.2
    definite-length block ``#<n><length><bytes>``.
* PyVISA documentation (https://pyvisa.readthedocs.io/): ``Resource.clear()``
  sends the interface's device-clear (GPIB SDC, USBTMC INITIATE_CLEAR, VXI-11
  device_clear, HiSLIP device clear). labmcp's VISA transport calls it from
  ``flush_input()``.

No MCP code here: the class can be used from scripts and notebooks.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from labmcp import InstrumentError, InstrumentProtocolError, InstrumentTimeout, Transport
from labmcp.scpi import SCPIDriver, consume_block_terminator

from labmcp_scpi.policy import CommandPolicy, CommandRefused

_ERROR_RE = re.compile(r'^\s*([+-]?\d+)\s*(?:,\s*"?(.*?)"?)?\s*$', re.DOTALL)


@dataclass
class SCPIError:
    code: int | None
    message: str
    raw: str


@dataclass
class ExchangeResult:
    command: str
    response: str | None
    errors: list[SCPIError]
    error_check: str  # "ok", "errors", "unavailable: ..." or "skipped"
    elapsed_s: float


def parse_error(reply: str) -> SCPIError:
    m = _ERROR_RE.match(reply)
    if not m:
        return SCPIError(None, reply.strip(), reply)
    return SCPIError(int(m.group(1)), (m.group(2) or "").strip(), reply.strip())


class SCPIInstrument(SCPIDriver):
    """Any SCPI instrument, with every raw command checked by a :class:`CommandPolicy`."""

    def __init__(
        self,
        transport: Transport,
        policy: CommandPolicy | None = None,
        *,
        error_query: str = "SYST:ERR?",
        safe_state: str | None = None,
    ) -> None:
        super().__init__(transport)
        self.policy = policy or CommandPolicy()
        self.error_query = error_query
        self.safe_state = safe_state

    def identify(self) -> dict[str, str]:
        """``*IDN?`` split into manufacturer, model, serial and firmware (IEEE 488.2), plus the raw reply."""
        raw = self.query("*IDN?")
        parts = [p.strip() for p in raw.split(",")]
        parts += [""] * (4 - len(parts))
        return {
            "manufacturer": parts[0],
            "model": parts[1],
            "serial": parts[2],
            "firmware": ",".join(parts[3:]).strip(","),
            "raw": raw,
        }

    # ------------------------------------------------------------ errors

    def read_errors(self, max_errors: int = 20, timeout: float | None = None) -> list[SCPIError]:
        """Drain the error/event queue. Reading is destructive: each entry is returned once."""
        out: list[SCPIError] = []
        with self.t.lock:
            for _ in range(max_errors):
                err = parse_error(self.query(self.error_query, timeout))
                if err.code == 0:
                    break
                out.append(err)
                if err.code is None:  # not an error-queue reply; stop rather than loop
                    break
        return out

    def _error_check(self) -> tuple[list[SCPIError], str]:
        try:
            errors = self.read_errors()
        except InstrumentTimeout as exc:
            self._resync()
            return [], f"unavailable: no reply to {self.error_query!r} ({exc})"
        return errors, ("errors" if errors else "ok")

    def _resync(self) -> None:
        """Discard stale input after a timeout (on VISA this is a device clear)."""
        with contextlib.suppress(Exception):  # best effort
            self.t.flush_input()

    def _timeout_error(self, command: str, exc: InstrumentTimeout) -> InstrumentTimeout:
        self._resync()
        try:
            errors = self.read_errors(timeout=1.0)
        except InstrumentTimeout:
            errors = []
        detail = "; ".join(e.raw for e in errors) if errors else "none reported"
        return InstrumentTimeout(
            f"No reply to {command!r}: {exc} The instrument's error queue: {detail}. "
            "A query the instrument does not recognise (-113 Undefined header) produces no reply."
        )

    # ------------------------------------------------------------ policy-checked I/O

    def checked_query(self, command: str, timeout: float | None = None) -> ExchangeResult:
        """Send one read-only query (``scpi_query``) and return its reply line."""
        command = self.policy.check_query(command)
        t0 = time.monotonic()
        with self.t.lock:
            try:
                reply = self.t.query(command, timeout)
            except InstrumentTimeout as exc:
                raise self._timeout_error(command, exc) from exc
            if re.match(r"^#[1-9]", reply):
                self._resync()
                raise InstrumentProtocolError(
                    f"{command!r} returned an IEEE 488.2 binary block, which cannot be read as text. "
                    "Use `query_binary_block` for this query."
                )
        return ExchangeResult(command, reply.strip(), [], "skipped", time.monotonic() - t0)

    def checked_write(
        self, command: str, timeout: float | None = None, check_errors: bool = True
    ) -> ExchangeResult:
        """Send any program message (``scpi_write``). If it contains a query, read the reply."""
        units = self.policy.check_command(command)
        command = command.strip()
        expects_reply = any(u.split(None, 1)[0].endswith("?") for u in units)
        t0 = time.monotonic()
        response: str | None = None
        with self.t.lock:
            if expects_reply:
                try:
                    response = self.t.query(command, timeout).strip()
                except InstrumentTimeout as exc:
                    raise self._timeout_error(command, exc) from exc
                if re.match(r"^#[1-9]", response):  # a binary block cannot be read as a text line
                    self._resync()
                    response = "<binary block discarded: read binary replies with query_binary_block>"
            else:
                self.t.write(command)
            errors, status = self._error_check() if check_errors else ([], "skipped")
        return ExchangeResult(command, response, errors, status, time.monotonic() - t0)

    def read_block(self, command: str, max_bytes: int, timeout: float | None = None) -> bytes:
        """Send a query that answers with a definite-length block and return its payload.

        The block's declared length is checked against ``max_bytes`` before the
        payload is read; an oversized payload is thrown away unkept. ``timeout``
        bounds the whole transfer (header and payload), not each read.
        """
        command = self.policy.check_query(command)
        deadline = time.monotonic() + (self.t.timeout if timeout is None else timeout)

        def left() -> float:
            return max(0.001, deadline - time.monotonic())

        with self.t.lock:
            self.t.write(command)
            try:
                first = self.t.read_bytes(1, left())
                if first != b"#":
                    rest = self.t.read_until(self.t.read_termination.encode(self.t.encoding), 1.0)
                    text = (first + rest).decode(self.t.encoding, "replace")
                    raise InstrumentProtocolError(
                        f"Expected an IEEE 488.2 binary block from {command!r} but the instrument sent "
                        f"{text[:80]!r}. Use `scpi_query` for text replies, or select a binary data "
                        "format first (e.g. FORMat:DATA REAL,32 with scpi_write)."
                    )
                digits = self.t.read_bytes(1, left())
                if not digits.isdigit():
                    self._resync()
                    raise InstrumentProtocolError(f"Malformed block header from {command!r}: #{digits!r}")
                if digits == b"0":
                    self._resync()
                    raise InstrumentProtocolError(
                        f"{command!r} returned an indefinite-length block (#0). Only definite-length "
                        "blocks are supported; configure the instrument for definite-length output."
                    )
                size_field = self.t.read_bytes(int(digits), left())
                if not size_field.isdigit():
                    self._resync()
                    raise InstrumentProtocolError(
                        f"Malformed block header from {command!r}: #{digits.decode()}{size_field!r} "
                        "(the length field must be decimal digits)."
                    )
                length = int(size_field)
                if length > max_bytes:
                    self._discard_block(length, left)
                    raise InstrumentProtocolError(
                        f"{command!r} announced a {length}-byte block, above the {max_bytes}-byte limit "
                        "(safety limit `max_block_bytes`). The data was discarded. Request less data "
                        "(fewer points) or restart with a higher --limit max_block_bytes."
                    )
                data = self.t.read_bytes(length, left())
            except InstrumentTimeout as exc:
                raise self._timeout_error(command, exc) from exc
            # The block is followed by the response terminator (IEEE 488.2 NL^END);
            # leaving it unread would shift every later reply by one.
            consume_block_terminator(self.t, timeout)
        return data

    def _discard_block(self, length: int, left: Callable[[], float]) -> None:
        """Throw away a block payload we will not keep, so it cannot be read as the next reply.

        Flushing alone is not enough on a raw socket or serial link: most of the payload
        is still on its way when the header has been read. VISA's device clear does
        discard it, so there the flush suffices.
        """
        from labmcp.transports.visa import VisaTransport

        if not isinstance(self.t, VisaTransport):
            with contextlib.suppress(InstrumentError):  # out of time: fall back to a flush
                remaining = length
                while remaining > 0:
                    chunk = min(remaining, 65536)
                    self.t.read_bytes(chunk, left())
                    remaining -= chunk
                consume_block_terminator(self.t, 1.0)
        self._resync()

    # ------------------------------------------------------------ common commands

    def wait_operation_complete(self, timeout: float) -> float:
        """``*OPC?``: block until all pending operations finish. Returns the wait in seconds."""
        t0 = time.monotonic()
        try:
            reply = self.query("*OPC?", timeout=timeout)
        except InstrumentTimeout as exc:
            raise self._timeout_error("*OPC?", exc) from exc
        if reply.strip().lstrip("+") != "1":
            raise InstrumentProtocolError(f"Unexpected reply to '*OPC?': {reply!r} (expected 1)")
        return time.monotonic() - t0

    def reset(self, clear_status: bool = True, timeout: float = 30.0) -> ExchangeResult:
        """``*RST`` (and ``*CLS``), then wait with ``*OPC?`` and read the error queue."""
        command = "*RST;*CLS" if clear_status else "*RST"
        self.policy.check_command("*RST")
        t0 = time.monotonic()
        with self.t.lock:
            self.t.write("*RST")
            if clear_status:
                self.t.write("*CLS")
            self.wait_operation_complete(timeout)
            errors, status = self._error_check()
        return ExchangeResult(command, None, errors, status, time.monotonic() - t0)

    def device_clear(self, abort: bool = True) -> dict[str, object]:
        """Recover a confused or busy instrument. Never blocked by the command policy.

        1. Interface device clear (VISA) or discard of unread input (socket/serial).
        2. Read (and report) the error queue.
        3. Optionally ``ABORt`` (trigger system to IDLE, sweeps/measurements aborted).
        4. ``*CLS``: clear status registers and the error queue (including a -113 from
           ABORt on instruments that do not implement it).
        """
        from labmcp.transports.visa import VisaTransport

        steps: list[str] = []
        with self.t.lock:
            self.t.flush_input()
            if isinstance(self.t, VisaTransport):
                steps.append("VISA device clear sent (clears input/output buffers, resets the parser)")
            else:
                steps.append("unread input discarded (raw socket/serial links have no device-clear message)")
            errors, status = self._error_check()
            if abort:
                self.t.write("ABORt")
                steps.append("ABORt sent")
            self.t.write("*CLS")
            steps.append("*CLS sent (status registers and error queue cleared)")
        return {"steps": steps, "errors_before_clear": errors, "error_check": status}

    def apply_safe_state(self, timeout: float | None = None) -> ExchangeResult:
        """Send the user-configured ``safe_state`` program message. Never blocked by the policy."""
        if not self.safe_state:
            raise CommandRefused(
                "No safe state is configured. Start the server with e.g. "
                "`--option safe_state=\"OUTP OFF\"` (the commands that make YOUR instrument safe)."
            )
        t0 = time.monotonic()
        with self.t.lock:
            self.t.write(self.safe_state)
            errors, status = self._error_check()
        return ExchangeResult(self.safe_state, None, errors, status, time.monotonic() - t0)
