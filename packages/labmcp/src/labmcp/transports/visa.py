"""VISA transport (GPIB, USBTMC, LXI/VXI-11, HiSLIP) via PyVISA.

Install with ``pip install "labmcp[visa]"``. By default the pure-Python
``pyvisa-py`` backend is used; pass ``?backend=@ivi`` (or a path to a vendor
VISA library) to use NI-VISA / Keysight IO Libraries instead.
"""

from __future__ import annotations

import time
from typing import Any

from labmcp.errors import InstrumentConnectionError, InstrumentTimeout
from labmcp.transports.base import Transport


class VisaTransport(Transport):
    def __init__(self, resource: str, *, backend: str = "@py", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        try:
            import pyvisa
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise InstrumentConnectionError(
                "VISA support is not installed. Install it with `pip install \"labmcp[visa]\"`."
            ) from exc
        self._pyvisa = pyvisa
        self.description = f"visa://{resource}"
        self._res: Any = None
        try:
            self._rm = pyvisa.ResourceManager(backend)
            self._res = self._rm.open_resource(resource)
            self._res.read_termination = self.read_termination
            self._res.write_termination = self.write_termination
            self._res.encoding = self.encoding
            self._res.timeout = int(self.timeout * 1000)
        except Exception as exc:  # pyvisa raises a variety of backend-specific errors
            if self._res is not None:  # don't leak the session (it would block the next attempt)
                try:
                    self._res.close()
                except Exception:
                    pass
            raise InstrumentConnectionError(
                f"Could not open VISA resource {resource!r} (backend {backend}): {exc}. "
                "List visible resources with `python -m pyvisa info` / "
                "`pyvisa-shell` and check the address."
            ) from exc

    # Message-based VISA sessions handle termination/EOI themselves, so the
    # line API delegates to PyVISA rather than scanning for terminators.

    def write(self, command: str) -> None:
        self._ensure_open()
        with self.lock:
            if self.audit:
                self.audit.record("write", command, self.description)
            self._set_timeout(None)  # not whatever short timeout the previous read used
            self._call(self._res.write, command)

    def read(self, timeout: float | None = None) -> str:
        self._ensure_open()
        with self.lock:
            self._set_timeout(timeout)
            reply: str = self._call(self._res.read)
        if self.audit:
            self.audit.record("read", reply, self.description)
        return reply

    def read_bytes(self, size: int, timeout: float | None = None) -> bytes:
        self._ensure_open()
        with self.lock:
            self._set_timeout(timeout)
            data: bytes = self._call(self._res.read_bytes, size)
        if self.audit:
            self.audit.record("read", data, self.description)
        return data

    def read_until(self, terminator: bytes, timeout: float | None = None) -> bytes:
        self._ensure_open()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        out = bytearray()
        with self.lock:
            # One overall deadline (not per byte) and one audit entry for the whole reply.
            while not out.endswith(terminator):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InstrumentTimeout(
                        f"Timed out waiting for a reply from {self.description}"
                        + (f" (partial: {bytes(out)!r})" if out else "")
                        + ". Is the instrument on and is the command valid for this model?"
                    )
                self._set_timeout(remaining)
                out += self._call(self._res.read_bytes, 1)
        data = bytes(out[: len(out) - len(terminator)])
        if self.audit:
            self.audit.record("read", data, self.description)
        return data

    def _write(self, data: bytes) -> None:
        self._set_timeout(None)
        self._call(self._res.write_raw, data)

    def _read(self, max_bytes: int, timeout: float) -> bytes:  # pragma: no cover - unused
        self._set_timeout(timeout)
        return self._call(self._res.read_bytes, 1)

    def _flush_input(self) -> None:
        try:
            self._res.clear()
        except Exception:
            pass

    def _close(self) -> None:
        self._res.close()

    def _set_timeout(self, timeout: float | None) -> None:
        ms = max(1, int((self.timeout if timeout is None else timeout) * 1000))
        self._call(setattr, self._res, "timeout", ms)

    def _call(self, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except self._pyvisa.errors.Error as exc:
            if self._closed:  # e.g. InvalidSession: `reconnect` closed the resource mid-read
                raise self._closed_error() from exc
            timeout_code = self._pyvisa.constants.StatusCode.error_timeout
            if getattr(exc, "error_code", None) == timeout_code:
                raise InstrumentTimeout(
                    f"Timed out waiting for {self.description}. Is the instrument on and is "
                    "the command valid for this model?"
                ) from exc
            raise InstrumentConnectionError(f"VISA error on {self.description}: {exc}") from exc
        except (OSError, ValueError, TypeError) as exc:  # e.g. a socket error from pyvisa-py
            self._raise_io_error("VISA I/O on", exc)
