"""VISA transport (GPIB, USBTMC, LXI/VXI-11, HiSLIP) via PyVISA.

Install with ``pip install "labmcp[visa]"``. By default the pure-Python
``pyvisa-py`` backend is used; pass ``?backend=@ivi`` (or a path to a vendor
VISA library) to use NI-VISA / Keysight IO Libraries instead.
"""

from __future__ import annotations

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
        try:
            self._rm = pyvisa.ResourceManager(backend)
            self._res = self._rm.open_resource(resource)
        except Exception as exc:  # pyvisa raises a variety of backend-specific errors
            raise InstrumentConnectionError(
                f"Could not open VISA resource {resource!r} (backend {backend}): {exc}. "
                "List visible resources with `python -m pyvisa info` / "
                "`pyvisa-shell` and check the address."
            ) from exc
        self._res.read_termination = self.read_termination
        self._res.write_termination = self.write_termination
        self._res.encoding = self.encoding
        self._res.timeout = int(self.timeout * 1000)

    # Message-based VISA sessions handle termination/EOI themselves, so the
    # line API delegates to PyVISA rather than scanning for terminators.

    def write(self, command: str) -> None:
        self._ensure_open()
        with self.lock:
            if self.audit:
                self.audit.record("write", command, self.description)
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
        out = b""
        with self.lock:
            while not out.endswith(terminator):
                out += self.read_bytes(1, timeout)
        return out[: -len(terminator)]

    def _write(self, data: bytes) -> None:
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
        self._res.timeout = int((self.timeout if timeout is None else timeout) * 1000)

    def _call(self, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except self._pyvisa.errors.VisaIOError as exc:
            if exc.error_code == self._pyvisa.constants.StatusCode.error_timeout:
                raise InstrumentTimeout(
                    f"Timed out waiting for {self.description}. Is the instrument on and is "
                    "the command valid for this model?"
                ) from exc
            raise InstrumentConnectionError(f"VISA error on {self.description}: {exc}") from exc
