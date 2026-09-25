"""Raw TCP socket transport (LAN instruments, serial-to-Ethernet bridges, SCPI port 5025)."""

from __future__ import annotations

import select
import socket
from typing import Any

from labmcp.errors import InstrumentConnectionError
from labmcp.transports.base import Transport


class TCPTransport(Transport):
    def __init__(self, host: str, port: int, *, connect_timeout: float = 5.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.description = f"tcp://{host}:{port}"
        try:
            self._sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
        except OSError as exc:
            raise InstrumentConnectionError(
                f"Could not connect to {host}:{port}: {exc}. Check the IP address, that the "
                "instrument's LAN interface is enabled, and that no firewall blocks the port."
            ) from exc
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.setblocking(False)

    def _write(self, data: bytes) -> None:
        self._sock.setblocking(True)
        try:
            self._sock.sendall(data)
        finally:
            self._sock.setblocking(False)

    def _read(self, max_bytes: int, timeout: float) -> bytes:
        ready, _, _ = select.select([self._sock], [], [], max(timeout, 0))
        if not ready:
            return b""
        data = self._sock.recv(max_bytes)
        if not data:
            raise ConnectionResetError("instrument closed the connection")
        return data

    def _flush_input(self) -> None:
        while True:
            ready, _, _ = select.select([self._sock], [], [], 0)
            if not ready or not self._sock.recv(65536):
                return

    def _close(self) -> None:
        self._sock.close()
