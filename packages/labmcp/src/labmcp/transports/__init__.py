"""Transports and the address (URI) parser.

Every LabMCP server accepts an ``--address`` in one of these forms:

==============================================  ===========================================
``serial:///dev/ttyUSB0?baudrate=9600``         RS-232 / USB-serial (Linux/macOS)
``serial://COM3?baudrate=19200&parity=E``       RS-232 / USB-serial (Windows)
``/dev/ttyUSB0`` or ``COM3``                    Shorthand for a serial port
``tcp://192.168.1.50:5025``                     Raw TCP socket (LAN / serial-to-Ethernet)
``visa://TCPIP0::192.168.1.50::INSTR``          Any VISA resource (needs ``labmcp[visa]``)
``GPIB0::22::INSTR`` / ``USB0::...::INSTR``     Shorthand for a VISA resource
==============================================  ===========================================

Query parameters override the driver's defaults, e.g. ``?baudrate=19200&timeout=5``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from labmcp.transports.base import Transport
from labmcp.transports.sim import ByteSimulator, LineSimulator, SimulatedTransport

__all__ = [
    "Address",
    "ByteSimulator",
    "LineSimulator",
    "SimulatedTransport",
    "Transport",
    "open_transport",
    "parse_address",
]

_VISA_RE = re.compile(r"^(GPIB|USB|TCPIP|ASRL|VXI|PXI)\d*::", re.IGNORECASE)
_SERIAL_RE = re.compile(r"^(COM\d+|/dev/.+)$", re.IGNORECASE)

# Parameters that belong to the transport base class rather than a specific kind.
_COMMON = {"read_termination", "write_termination", "encoding", "timeout"}
_TERMINATIONS = {"CR": "\r", "LF": "\n", "CRLF": "\r\n", "NONE": ""}


@dataclass
class Address:
    kind: str  # "serial" | "tcp" | "visa"
    target: str  # port name, host, or VISA resource string
    port: int | None = None
    params: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        query = "&".join(f"{k}={v}" for k, v in self.params.items())
        base = f"{self.kind}://{self.target}" + (f":{self.port}" if self.port else "")
        return base + (f"?{query}" if query else "")


def parse_address(address: str) -> Address:
    """Parse an instrument address string into its components."""
    address = address.strip()
    if not address:
        raise ValueError("Empty instrument address")

    if _VISA_RE.match(address):
        return Address("visa", address)
    if _SERIAL_RE.match(address):
        return Address("serial", address)

    scheme, _, rest = address.partition("://")
    scheme = scheme.lower()
    if not rest:
        raise ValueError(
            f"Unrecognised address {address!r}. Use serial:///dev/ttyUSB0, serial://COM3, "
            "tcp://host:port or visa://RESOURCE."
        )
    body, _, query = rest.partition("?")
    params = dict(parse_qsl(query, keep_blank_values=True))

    if scheme == "serial":
        return Address("serial", unquote(body), params=params)
    if scheme == "visa":
        return Address("visa", unquote(body), params=params)
    if scheme == "tcp":
        parts = urlsplit(f"//{body}")
        if not parts.hostname or not parts.port:
            raise ValueError(f"TCP address needs host and port, e.g. tcp://192.168.1.50:5025 (got {address!r})")
        return Address("tcp", parts.hostname, parts.port, params)
    raise ValueError(f"Unsupported address scheme {scheme!r} in {address!r}")


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def open_transport(address: str | Address, **defaults: Any) -> Transport:
    """Open a transport for ``address``.

    ``defaults`` are the driver's preferred settings (baud rate, terminations, ...);
    anything in the address query string overrides them.
    """
    addr = parse_address(address) if isinstance(address, str) else address
    options: dict[str, Any] = dict(defaults)
    for key, raw in addr.params.items():
        if key in {"read_termination", "write_termination"}:
            options[key] = _TERMINATIONS.get(raw.upper(), raw.encode().decode("unicode_escape"))
        else:
            options[key] = _coerce(raw)

    common = {k: options.pop(k) for k in list(options) if k in _COMMON or k == "audit"}
    if addr.kind == "serial":
        from labmcp.transports.serial import SerialTransport

        keep = {"baudrate", "bytesize", "parity", "stopbits", "rtscts", "xonxoff", "dsrdtr"}
        return SerialTransport(addr.target, **{k: v for k, v in options.items() if k in keep}, **common)
    if addr.kind == "tcp":
        from labmcp.transports.tcp import TCPTransport

        keep = {"connect_timeout"}
        return TCPTransport(addr.target, addr.port or 0, **{k: v for k, v in options.items() if k in keep}, **common)
    if addr.kind == "visa":
        from labmcp.transports.visa import VisaTransport

        backend = str(options.get("backend", "@py"))
        return VisaTransport(addr.target, backend=backend, **common)
    raise ValueError(f"Unsupported transport kind {addr.kind!r}")
