"""Transport base class.

A transport moves bytes between a driver and an instrument. Concrete
transports only implement the four raw primitives (``_write``, ``_read``,
``_flush_input``, ``_close``); line handling, encoding, locking and auditing
live here so every transport behaves identically - including the simulated one
used in tests.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from labmcp.errors import InstrumentConnectionError, InstrumentTimeout

if TYPE_CHECKING:
    from labmcp.audit import AuditLog


class Transport(ABC):
    """Byte/line oriented, thread-safe link to one instrument.

    Args:
        read_termination: Line terminator the instrument sends (e.g. ``"\\r\\n"``).
        write_termination: Terminator appended to every command we send.
        encoding: Text encoding for line-based protocols.
        timeout: Seconds to wait for a reply before raising ``InstrumentTimeout``.
        audit: Optional audit log that records all traffic.
    """

    #: Human-readable description, e.g. ``serial:///dev/ttyUSB0 @ 9600``.
    description: str = "transport"

    def __init__(
        self,
        *,
        read_termination: str = "\n",
        write_termination: str = "\n",
        encoding: str = "ascii",
        timeout: float = 2.0,
        audit: AuditLog | None = None,
    ) -> None:
        self.read_termination = read_termination
        self.write_termination = write_termination
        self.encoding = encoding
        self.timeout = timeout
        self.audit = audit
        #: Hold this lock for multi-step exchanges that must not interleave.
        self.lock = threading.RLock()
        self._buffer = b""
        self._closed = False

    # -- primitives implemented by subclasses ---------------------------------

    @abstractmethod
    def _write(self, data: bytes) -> None: ...

    @abstractmethod
    def _read(self, max_bytes: int, timeout: float) -> bytes:
        """Return up to ``max_bytes`` bytes; ``b""`` if nothing arrived before ``timeout``."""

    @abstractmethod
    def _flush_input(self) -> None: ...

    @abstractmethod
    def _close(self) -> None: ...

    # -- raw byte API ---------------------------------------------------------

    def write_bytes(self, data: bytes) -> None:
        self._ensure_open()
        with self.lock:
            if self.audit:
                self.audit.record("write", data, self.description)
            try:
                self._write(data)
            except OSError as exc:
                raise InstrumentConnectionError(
                    f"Write to {self.description} failed: {exc}. Check the cable/network and "
                    "call the `reconnect` tool."
                ) from exc

    def read_bytes(self, size: int, timeout: float | None = None) -> bytes:
        """Read exactly ``size`` bytes or raise ``InstrumentTimeout``."""
        self._ensure_open()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self.lock:
            while len(self._buffer) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    got = len(self._buffer)
                    raise InstrumentTimeout(
                        f"Timed out waiting for {size} bytes from {self.description} "
                        f"(received {got}). Is the instrument powered on and configured "
                        "with the same communication settings?"
                    )
                self._buffer += self._safe_read(size - len(self._buffer), remaining)
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        if self.audit:
            self.audit.record("read", data, self.description)
        return data

    def read_until(self, terminator: bytes, timeout: float | None = None) -> bytes:
        """Read up to and including ``terminator``; returns data *without* it."""
        self._ensure_open()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self.lock:
            while terminator not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    partial = self._buffer.decode(self.encoding, "replace")
                    raise InstrumentTimeout(
                        f"Timed out waiting for a reply from {self.description}"
                        + (f" (partial: {partial!r})" if partial else "")
                        + ". Is the instrument powered on, and do baud rate / line endings "
                        "match its configuration?"
                    )
                self._buffer += self._safe_read(4096, remaining)
            data, _, self._buffer = self._buffer.partition(terminator)
        if self.audit:
            self.audit.record("read", data, self.description)
        return data

    def flush_input(self) -> None:
        """Discard anything the instrument sent that we have not read yet."""
        with self.lock:
            self._buffer = b""
            self._flush_input()

    # -- line API -------------------------------------------------------------

    def write(self, command: str) -> None:
        self.write_bytes((command + self.write_termination).encode(self.encoding))

    def read(self, timeout: float | None = None) -> str:
        raw = self.read_until(self.read_termination.encode(self.encoding), timeout)
        return raw.decode(self.encoding, "replace")

    def query(self, command: str, timeout: float | None = None) -> str:
        """Send ``command`` and return the next line (stripped of the terminator)."""
        with self.lock:
            self.write(command)
            return self.read(timeout)

    # -- lifecycle ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._close()
            except OSError:
                pass

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InstrumentConnectionError(
                f"Connection to {self.description} is closed. Call the `reconnect` tool."
            )

    def _safe_read(self, max_bytes: int, timeout: float) -> bytes:
        try:
            return self._read(max_bytes, timeout)
        except OSError as exc:
            raise InstrumentConnectionError(
                f"Read from {self.description} failed: {exc}. Check the cable/network and "
                "call the `reconnect` tool."
            ) from exc

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.description}>"
