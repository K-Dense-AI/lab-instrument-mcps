"""RS-232 / USB-serial transport (pyserial)."""

from __future__ import annotations

from typing import Any

from labmcp.errors import InstrumentConnectionError
from labmcp.transports.base import Transport

_PARITY = {"N": "N", "E": "E", "O": "O", "M": "M", "S": "S"}
_READ_SLICE_S = 0.1


class SerialTransport(Transport):
    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        rtscts: bool = False,
        xonxoff: bool = False,
        dsrdtr: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        import serial  # pyserial

        parity_code = _PARITY.get(str(parity).strip().upper()[:1])
        if parity_code is None:
            raise InstrumentConnectionError(f"Unknown serial parity {parity!r}: use N, E, O, M or S.")
        self.description = f"serial://{port} @ {baudrate} {bytesize}{parity_code}{stopbits:g}"
        try:
            self._port = serial.Serial(
                port=port,
                baudrate=int(baudrate),
                bytesize=int(bytesize),
                parity=parity_code,
                stopbits=float(stopbits) if float(stopbits) == 1.5 else int(stopbits),
                rtscts=bool(rtscts),
                xonxoff=bool(xonxoff),
                dsrdtr=bool(dsrdtr),
                timeout=0,
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            raise InstrumentConnectionError(
                f"Could not open serial port {port!r}: {exc}. Check the port name "
                "(`python -m serial.tools.list_ports` lists them), that no other program "
                "has it open, and that you have permission to use it."
            ) from exc

    def _write(self, data: bytes) -> None:
        self._port.write(data)
        self._port.flush()

    def _read(self, max_bytes: int, timeout: float) -> bytes:
        # Setting pyserial's timeout reconfigures the port (tcsetattr / SetCommState), so wait
        # in fixed slices instead of re-setting it on every call; the base class loops until
        # its own deadline, and a read returns as soon as a byte arrives.
        slice_s = min(max(timeout, 0.001), _READ_SLICE_S)
        if self._port.timeout != slice_s:
            self._port.timeout = slice_s
        first = self._port.read(1)
        if not first:
            return b""
        waiting = self._port.in_waiting
        rest = self._port.read(min(waiting, max_bytes - 1)) if waiting and max_bytes > 1 else b""
        return first + rest

    def _flush_input(self) -> None:
        self._port.reset_input_buffer()

    def _close(self) -> None:
        self._port.close()
