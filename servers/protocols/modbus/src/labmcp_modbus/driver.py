"""Modbus TCP / RTU driver: a thin pymodbus wrapper plus register-map logic.

References:

* "MODBUS Application Protocol Specification V1.1b3", Modbus Organization,
  April 2012: https://www.modbus.org/file/secure/modbusprotocolspecification.pdf
  - 6.1-6.4 / 6.12: read coils (FC01) and discrete inputs (FC02), 1-2000 bits;
    read holding (FC03) and input (FC04) registers, 1-125 registers.
  - 6.5 / 6.6 / 6.12: write single coil (FC05, 0xFF00 = ON, 0x0000 = OFF),
    single register (FC06), multiple registers (FC16, 1-123 registers).
  - 7: exception codes 01-0B.
* "MODBUS over Serial Line Specification and Implementation Guide V1.02", Dec 2006:
  https://www.modbus.org/file/secure/modbusoverserial.pdf - unit addresses 1-247,
  0 = broadcast (no reply); 19200 baud, even parity is the required default.
* "MODBUS Messaging on TCP/IP Implementation Guide V1.0b":
  https://www.modbus.org/file/secure/messagingimplementationguide.pdf - port 502;
  the unit identifier addresses devices behind gateways.
* pymodbus 3.x client API (https://pymodbus.readthedocs.io/): ``ModbusTcpClient``,
  ``ModbusSerialClient``, ``read_*/write_*(address, ..., count=, device_id=)``
  (the unit keyword was ``slave=`` before pymodbus 3.10; both are supported).

No MCP code here.
"""

from __future__ import annotations

import inspect
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from labmcp import (
    AuditLog,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentTimeout,
    SafetyLimitError,
    parse_address,
)

from labmcp_modbus.registers import Point, RegisterMap

MAX_READ_REGISTERS = 125  # FC03/FC04
MAX_READ_BITS = 2000  # FC01/FC02
MAX_WRITE_REGISTERS = 123  # FC16

EXCEPTION_CODES = {
    1: ("ILLEGAL FUNCTION", "the device does not support this function code"),
    2: ("ILLEGAL DATA ADDRESS", "the address (or address + count) is not implemented on this device; "
        "addresses are 0-based, check the register map and manual"),
    3: ("ILLEGAL DATA VALUE", "the device rejected the value (out of its own range, or wrong count)"),
    4: ("SERVER DEVICE FAILURE", "an unrecoverable error occurred in the device"),
    5: ("ACKNOWLEDGE", "the device accepted a long-running request and is still processing it"),
    6: ("SERVER DEVICE BUSY", "the device is busy; retry later"),
    8: ("MEMORY PARITY ERROR", "the device detected a memory parity error"),
    10: ("GATEWAY PATH UNAVAILABLE", "the gateway could not route the request (wrong unit id?)"),
    11: ("GATEWAY TARGET DEVICE FAILED TO RESPOND", "no answer from the device behind the gateway "
         "(powered off, wrong unit id or serial settings?)"),
}  # fmt: skip


class ModbusExceptionResponse(InstrumentProtocolError):
    """The device answered with a Modbus exception response."""

    def __init__(self, function: str, address: int, code: int | None) -> None:
        self.code = code
        name, meaning = EXCEPTION_CODES.get(code or -1, ("UNKNOWN", "unrecognised exception code"))
        super().__init__(
            f"Device replied with Modbus exception {code:02X} ({name}) to {function} at address {address}: "
            f"{meaning}."
            if code is not None
            else f"Device returned an error response to {function} at address {address}."
        )


class ModbusClient(Protocol):
    """What :class:`ModbusDevice` needs from a Modbus client (real or simulated)."""

    description: str

    def read_holding_registers(self, address: int, count: int, unit: int) -> list[int]: ...
    def read_input_registers(self, address: int, count: int, unit: int) -> list[int]: ...
    def read_coils(self, address: int, count: int, unit: int) -> list[bool]: ...
    def read_discrete_inputs(self, address: int, count: int, unit: int) -> list[bool]: ...
    def write_register(self, address: int, value: int, unit: int) -> None: ...
    def write_registers(self, address: int, values: list[int], unit: int) -> None: ...
    def write_coil(self, address: int, value: bool, unit: int) -> None: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------- pymodbus wrapper


def _with_default_port(address: str) -> str:
    """``tcp://host`` -> ``tcp://host:502`` (the registered Modbus/TCP port)."""
    return re.sub(r"^(tcp://[^/:?\[\]]+)(\?.*)?$", lambda m: m.group(1) + ":502" + (m.group(2) or ""), address)


class PymodbusClient:
    """Synchronous pymodbus client for ``tcp://host:502`` or ``serial:///dev/ttyUSB0?...``."""

    def __init__(self, address: str, *, timeout: float = 2.0) -> None:
        try:
            from pymodbus import FramerType
            from pymodbus.client import ModbusSerialClient, ModbusTcpClient
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise InstrumentConnectionError("pymodbus is not installed: `pip install pymodbus`.") from exc
        from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

        self._errors = (ModbusIOException, ConnectionException, ModbusException)
        addr = parse_address(_with_default_port(address))
        params = dict(addr.params)
        timeout = float(params.pop("timeout", timeout))
        retries = int(params.pop("retries", 1))
        framers = {"rtu": FramerType.RTU, "ascii": FramerType.ASCII, "socket": FramerType.SOCKET}
        if addr.kind == "tcp":
            framer = params.pop("framer", "socket").lower()
            self.serial = False
            self.description = f"modbus-tcp://{addr.target}:{addr.port}" + ("" if framer == "socket" else f" ({framer})")
            if framer not in framers:
                raise InstrumentConnectionError(f"Unknown framer {framer!r}: use socket, rtu or ascii")
            self._client = ModbusTcpClient(
                addr.target, port=int(addr.port or 502), framer=framers[framer], timeout=timeout, retries=retries
            )
        elif addr.kind == "serial":
            framer = params.pop("framer", "rtu").lower()
            if framer not in {"rtu", "ascii"}:
                raise InstrumentConnectionError(f"Unknown serial framer {framer!r}: use rtu or ascii")
            baud = int(params.pop("baudrate", 19200))
            parity = params.pop("parity", "E").upper()[:1]
            stopbits = int(params.pop("stopbits", 1))
            bytesize = int(params.pop("bytesize", 8))
            self.serial = True
            self.description = f"modbus-{framer}://{addr.target} @ {baud} {bytesize}{parity}{stopbits}"
            self._client = ModbusSerialClient(
                addr.target,
                framer=framers[framer],
                baudrate=baud,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
                timeout=timeout,
                retries=retries,
            )
        else:
            raise InstrumentConnectionError(
                f"Modbus needs a tcp:// or serial:// address, got {address!r} (e.g. tcp://192.168.1.20:502 "
                "or serial:///dev/ttyUSB0?baudrate=9600&parity=N)."
            )
        if params:
            raise InstrumentConnectionError(f"Unknown address parameter(s) for Modbus: {sorted(params)}")
        if not self._client.connect():
            raise InstrumentConnectionError(
                f"Could not open {self.description}. Check the IP address/port (TCP) or the serial port "
                "name and permissions, and that no other program holds the connection."
            )
        # pymodbus renamed the unit keyword from slave= to device_id= in 3.10.
        params_ = inspect.signature(self._client.read_holding_registers).parameters
        self._unit_kw = "device_id" if "device_id" in params_ else "slave"

    def _call(self, name: str, address: int, *args: Any, unit: int, **kwargs: Any) -> Any:
        io_error, conn_error, modbus_error = self._errors
        try:
            rr = getattr(self._client, name)(address, *args, **kwargs, **{self._unit_kw: unit})
        except io_error as exc:
            raise InstrumentTimeout(
                f"No valid reply from unit {unit} on {self.description} to {name} at address {address}: {exc}. "
                "Check the unit id, wiring (RS-485 A/B), baud rate/parity and termination."
            ) from exc
        except conn_error as exc:
            raise InstrumentConnectionError(
                f"Connection to {self.description} failed: {exc}. Call the `reconnect` tool."
            ) from exc
        except modbus_error as exc:
            raise InstrumentProtocolError(f"Modbus error during {name} at address {address}: {exc}") from exc
        except OSError as exc:  # pyserial SerialException (adapter unplugged), socket reset during send
            self._client.close()  # so the next request re-opens the port instead of reusing a dead handle
            raise InstrumentConnectionError(
                f"Connection to {self.description} failed during {name}: {exc}. Check the cable/adapter; "
                "the next request reconnects (or call the `reconnect` tool)."
            ) from exc
        if rr.isError():
            raise ModbusExceptionResponse(name, address, getattr(rr, "exception_code", None))
        return rr

    def read_holding_registers(self, address: int, count: int, unit: int) -> list[int]:
        return list(self._call("read_holding_registers", address, count=count, unit=unit).registers[:count])

    def read_input_registers(self, address: int, count: int, unit: int) -> list[int]:
        return list(self._call("read_input_registers", address, count=count, unit=unit).registers[:count])

    def read_coils(self, address: int, count: int, unit: int) -> list[bool]:
        return [bool(b) for b in self._call("read_coils", address, count=count, unit=unit).bits[:count]]

    def read_discrete_inputs(self, address: int, count: int, unit: int) -> list[bool]:
        return [bool(b) for b in self._call("read_discrete_inputs", address, count=count, unit=unit).bits[:count]]

    def write_register(self, address: int, value: int, unit: int) -> None:
        self._call("write_register", address, value, unit=unit)

    def write_registers(self, address: int, values: list[int], unit: int) -> None:
        self._call("write_registers", address, list(values), unit=unit)

    def write_coil(self, address: int, value: bool, unit: int) -> None:
        self._call("write_coil", address, bool(value), unit=unit)

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------- device


@dataclass
class PointReading:
    name: str
    value: float | bool | str | None
    raw: float | int | bool | None
    unit: str
    table: str
    address: int
    timestamp: str
    error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ModbusDevice:
    """One Modbus unit, optionally described by a :class:`RegisterMap`.

    Every request and reply is recorded in the audit log. Writes by name are
    checked against the map (writable flag, min/max, enum) before sending; raw
    writes are refused for addresses that belong to a mapped point.
    """

    def __init__(
        self,
        client: ModbusClient,
        *,
        unit_id: int = 1,
        register_map: RegisterMap | None = None,
        audit: AuditLog | None = None,
        allow_raw_writes: bool = True,
    ) -> None:
        self.client = client
        self.unit_id = unit_id
        self.map = register_map
        self.audit = audit
        self.allow_raw_writes = allow_raw_writes
        self.lock = threading.RLock()

    # ------------------------------------------------------------ low level

    def _log(self, direction: str, text: str) -> None:
        if self.audit:
            self.audit.record(direction, text, self.client.description)  # type: ignore[arg-type]

    def _request(self, fn: str, address: int, arg: Any) -> Any:
        label = f"unit {self.unit_id} {fn} address={address} " + (
            f"count={arg}" if fn.startswith("read") else f"value={arg}"
        )
        with self.lock:
            self._log("write", label)
            try:
                result = getattr(self.client, fn)(address, arg, unit=self.unit_id)
            except Exception as exc:
                self._log("read", f"error: {exc}")
                raise
            self._log("read", "ok" if result is None else str([int(v) for v in result]))
        return result

    def read_registers(self, table: str, address: int, count: int) -> list[int]:
        _check_span(address, count, MAX_READ_REGISTERS)
        fn = {"holding": "read_holding_registers", "input": "read_input_registers"}[table]
        return _complete(self._request(fn, address, count), count, fn, address)

    def read_bits(self, table: str, address: int, count: int) -> list[bool]:
        _check_span(address, count, MAX_READ_BITS)
        fn = {"coil": "read_coils", "discrete": "read_discrete_inputs"}[table]
        return _complete(self._request(fn, address, count), count, fn, address)

    def _protect_mapped(self, table: str, address: int, count: int) -> None:
        if not self.allow_raw_writes:
            raise SafetyLimitError(
                "Raw writes are disabled (--option raw_writes=false). Use write_point. Nothing was sent."
            )
        if self.map is None:
            return
        hits = self.map.overlapping(table, address, count)
        if hits:
            names = ", ".join(repr(p.name) for p in hits)
            raise SafetyLimitError(
                f"Refused: {table} address {address}" + (f"-{address + count - 1}" if count > 1 else "")
                + f" belongs to mapped point(s) {names}. Use `write_point` so the register map's limits "
                "are enforced. Nothing was sent to the device."
            )

    def write_register(self, address: int, value: int) -> None:
        if not -32768 <= value <= 65535:
            raise SafetyLimitError(f"Refused: {value} does not fit in a 16-bit register. Nothing was sent.")
        self._protect_mapped("holding", address, 1)
        self._request("write_register", address, value & 0xFFFF)

    def write_registers(self, address: int, values: list[int]) -> None:
        _check_span(address, len(values), MAX_WRITE_REGISTERS)
        if any(not -32768 <= v <= 65535 for v in values):
            raise SafetyLimitError("Refused: every value must fit in a 16-bit register. Nothing was sent.")
        self._protect_mapped("holding", address, len(values))
        self._request("write_registers", address, [v & 0xFFFF for v in values])

    def write_coil(self, address: int, value: bool) -> None:
        self._protect_mapped("coil", address, 1)
        self._request("write_coil", address, bool(value))

    # ------------------------------------------------------------ points

    def _require_map(self) -> RegisterMap:
        if self.map is None:
            raise InstrumentProtocolError(
                "No register map loaded. Start the server with --option register_map=/path/to/map.yaml "
                "(see the bundled example), or use the raw read/write tools."
            )
        return self.map

    def _read_raw(self, p: Point) -> list[int] | list[bool]:
        if p.is_bit:
            return self.read_bits(p.table, p.address, p.count)
        return self.read_registers(p.table, p.address, p.count)

    def _reading(self, p: Point) -> PointReading:
        value, raw = p.decode(self._read_raw(p))
        if isinstance(value, float) and not math.isfinite(value):
            # float registers often hold NaN as "no value"; NaN/inf are not valid JSON numbers either
            finite_raw = raw if not isinstance(raw, float) or math.isfinite(raw) else None
            return PointReading(
                p.name, None, finite_raw, p.unit, p.table, p.address, _now(),
                error=f"the device returned {value} (not a finite number): no valid value (e.g. a sensor "
                "fault), or the point's type/word_order/byte_order is wrong",
            )  # fmt: skip
        return PointReading(p.name, value, raw, p.unit, p.table, p.address, _now())

    def read_point(self, name: str) -> PointReading:
        return self._reading(self._point(name))

    def read_points(self, names: list[str] | None = None) -> list[PointReading]:
        m = self._require_map()
        out = []
        for name in names or list(m.points):
            p = self._point(name)
            try:
                out.append(self._reading(p))
            except (InstrumentProtocolError, InstrumentTimeout) as exc:
                out.append(PointReading(p.name, None, None, p.unit, p.table, p.address, _now(), error=str(exc)))
        return out

    def _point(self, name: str) -> Point:
        try:
            return self._require_map().point(name)
        except KeyError as exc:
            raise InstrumentProtocolError(str(exc.args[0])) from None

    def write_point(self, name: str, value: float | bool | str) -> tuple[PointReading, float | bool | str]:
        """Validate against the map, write, then read back. Returns (read-back, value written).

        If the write succeeds but the read-back fails (write-only register, link dropped), the
        reading carries the ``error`` rather than raising: the value WAS written.
        """
        p = self._point(name)
        try:
            encoded, stored = p.encode(value)
        except ValueError as exc:
            raise SafetyLimitError(f"Refused: {exc}. Nothing was sent to the device.") from None
        with self.lock:
            if p.table == "coil":
                self._request("write_coil", p.address, bool(encoded))
            elif isinstance(encoded, list) and len(encoded) == 1:
                self._request("write_register", p.address, encoded[0])
            else:
                self._request("write_registers", p.address, encoded)
            try:
                reading = self.read_point(name)
            except InstrumentError as exc:
                reading = PointReading(
                    p.name, None, None, p.unit, p.table, p.address, _now(),
                    error=f"the value was written, but reading it back failed: {exc}",
                )  # fmt: skip
            return reading, stored

    def apply_safe_state(self) -> list[dict[str, Any]]:
        """Write every ``safe_state`` step in order; keep going if one fails.

        A step is ``ok`` only if the write was accepted AND the value read back matches: many
        controllers acknowledge writes but ignore them (local/keypad mode), which must not be
        reported as safe.
        """
        m = self._require_map()
        if not m.safe_state:
            raise InstrumentProtocolError("The register map defines no safe_state.")
        results: list[dict[str, Any]] = []
        for step in m.safe_state:
            entry: dict[str, Any] = {"point": step.point, "requested": step.value}
            try:
                reading, stored = self.write_point(step.point, step.value)
            except Exception as exc:  # report every step, even if one fails
                results.append({**entry, "ok": False, "error": str(exc)})
                continue
            entry.update(written=stored, read_back=reading.value)
            if reading.error:
                entry.update(ok=False, error=f"not confirmed: {reading.error}")
            elif not values_match(reading.value, stored):
                entry.update(
                    ok=False,
                    error=f"the device accepted the write but reads back {reading.value!r} instead of {stored!r} "
                    "(it may be in local/keypad mode, or clamp the value)",
                )
            else:
                entry["ok"] = True
            results.append(entry)
        return results

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, Any]:
        info: dict[str, Any] = {"link": self.client.description, "unit_id": self.unit_id}
        if self.map is None:
            info["register_map"] = None
            return info
        info.update({k: v for k, v in self.map.device.items() if k in {"name", "manufacturer", "model"}})
        info["register_map"] = self.map.source
        info["points"] = len(self.map.points)
        probe = next(iter(self.map.points.values()), None)
        if probe is not None:  # prove the unit answers
            try:
                r = self.read_point(probe.name)
                info["probe"] = f"ok: {r.name} = {r.value} {r.unit}".strip()
            except (InstrumentProtocolError, InstrumentTimeout) as exc:
                info["probe"] = f"failed: {exc}"
        return info

    def close(self) -> None:
        self.client.close()


def values_match(read_back: Any, written: Any) -> bool:
    """True if a read-back equals the value written (floats compared to float32 precision)."""
    if isinstance(read_back, bool) or isinstance(written, bool):
        return read_back is written
    if isinstance(read_back, (int, float)) and isinstance(written, (int, float)):
        return math.isclose(read_back, written, rel_tol=1e-6, abs_tol=1e-9)
    return bool(read_back == written)


def _complete(values: list[Any], count: int, fn: str, address: int) -> list[Any]:
    """A reply with fewer registers/bits than requested would decode as garbage (or crash struct)."""
    if len(values) < count:
        raise InstrumentProtocolError(
            f"The device answered {fn} at address {address} with {len(values)} value(s) instead of {count}."
        )
    return list(values[:count])


def _check_span(address: int, count: int, maximum: int) -> None:
    if not 1 <= count <= maximum:
        raise SafetyLimitError(f"Refused: count must be 1-{maximum} for this function (got {count}).")
    if not address >= 0 or address + count > 65536:
        raise SafetyLimitError(f"Refused: addresses {address}..{address + count - 1} are outside 0-65535.")
