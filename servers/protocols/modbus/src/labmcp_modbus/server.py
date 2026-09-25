"""MCP server for Modbus TCP / RTU devices (temperature controllers, chillers, PLCs, sensors, VFDs)."""

from __future__ import annotations

import math
import struct
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentServer,
    Settings,
)
from pydantic import BaseModel, Field

from labmcp_modbus.driver import (
    MAX_READ_BITS,
    MAX_READ_REGISTERS,
    MAX_WRITE_REGISTERS,
    ModbusDevice,
    PointReading,
    PymodbusClient,
)
from labmcp_modbus.registers import (
    DATA_TYPES,
    RegisterMap,
    RegisterMapError,
    example_map_path,
    load_register_map,
    registers_to_bytes,
)
from labmcp_modbus.simulator import SimulatedPIDController

RAW_WRITE_TOOLS = {"write_register", "write_registers", "write_coil"}
_FALSE = {"0", "false", "no", "off"}


def _load_map(settings: Settings) -> tuple[RegisterMap | None, str | None]:
    """The configured register map (the bundled example in --simulate mode), or an error message."""
    path = settings.options.get("register_map") or (str(example_map_path()) if settings.simulate else None)
    if not path:
        return None, None
    try:
        return load_register_map(path), None
    except RegisterMapError as exc:
        return None, str(exc)


def _raw_writes_allowed(settings: Settings) -> bool:
    return settings.options.get("raw_writes", "true").strip().lower() not in _FALSE


def connect(ctx: ConnectContext) -> ModbusDevice:
    register_map, error = _load_map(ctx.settings)
    if error:
        raise InstrumentConnectionError(f"Invalid register map: {error}")
    unit_opt = ctx.option("unit_id")
    map_unit = register_map.unit_id if register_map else None
    try:
        unit_id = int(unit_opt) if unit_opt else (map_unit if map_unit is not None else 1)
    except ValueError as exc:
        raise InstrumentConnectionError(f"--option unit_id must be an integer, got {unit_opt!r}") from exc
    if not 0 <= unit_id <= 255:
        raise InstrumentConnectionError(f"unit_id must be 0-255, got {unit_id}")
    if ctx.simulate:
        client: Any = SimulatedPIDController(unit_id=unit_id, extra_map=register_map)
    else:
        client = PymodbusClient(ctx.require_address(), timeout=ctx.settings.timeout or 2.0)
        if client.serial and not 1 <= unit_id <= 247:
            client.close()
            raise InstrumentConnectionError(
                f"unit_id {unit_id} is not valid on a serial line: use 1-247 (0 is broadcast, which "
                "writes to every device and gets no reply)."
            )
    return ModbusDevice(
        client,
        unit_id=unit_id,
        register_map=register_map,
        audit=ctx.audit,
        allow_raw_writes=_raw_writes_allowed(ctx.settings),
    )


class ModbusServer(InstrumentServer[ModbusDevice]):
    """Lists `apply_safe_state` only if the register map defines a safe state, and hides the raw
    write tools with --option raw_writes=false."""

    register_map: RegisterMap | None = None
    map_error: str | None = None

    def configure(self, **kwargs: Any) -> ModbusServer:
        super().configure(**kwargs)
        self.register_map, self.map_error = _load_map(self.settings)
        if self.register_map is not None and self.register_map.safe_state:
            self.mcp.enable(names={"apply_safe_state"})
        else:
            self.mcp.disable(names={"apply_safe_state"})
        if _raw_writes_allowed(self.settings) and not self.settings.read_only:
            self.mcp.enable(names=RAW_WRITE_TOOLS)
        else:
            self.mcp.disable(names=RAW_WRITE_TOOLS)
        return self


server = ModbusServer(
    "Modbus TCP/RTU Device",
    connect=connect,
    package="labmcp-modbus",
    instructions="""
Generic Modbus client for temperature controllers, chillers, PLCs, sensors, VFDs and loggers.
- Call `list_points` first. With a register map, use `read_points` and `write_point`: values are
  decoded, scaled and carry units, and writes are checked against the map's writable flag and
  min/max before anything is sent. The raw register tools return unscaled 16-bit values.
- Addresses are 0-based protocol addresses: a manual's "40001" is holding address 0, "30001" is
  input address 0.
- `write_point` reads the value back after writing: report the read-back value. Setpoint, mode
  and output changes physically heat, cool, pump or move things: say what will happen first.
- Register-map limits are the lab's safety settings. If a write is refused, tell the user; never
  try to reach the same register with the raw write tools (mapped addresses are refused there).
- Exception 02 = address not implemented, 03 = the device rejected the value, timeout = wrong
  unit id / wiring / baud rate / parity. An absurd 32-bit value usually means the wrong word_order.
- If anything looks wrong, call `apply_safe_state` (listed when the map defines one).
""",
    address_help="""\
  tcp://192.168.1.20:502                          Modbus TCP (port defaults to 502)
  tcp://192.168.1.30:4001?framer=rtu               RTU frames through a transparent serial-to-Ethernet gateway
  serial:///dev/ttyUSB0?baudrate=9600&parity=N&stopbits=2   Modbus RTU over RS-485/RS-232
  serial://COM3?baudrate=19200&parity=E            Windows (default 19200 8E1, the Modbus serial default)
  add &framer=ascii for Modbus ASCII; &timeout=1&retries=2 to tune retries""",
    option_help={
        "unit_id": "Modbus unit (slave) id, default from the map or 1 (serial: 1-247)",
        "register_map": "YAML or JSON register map with named, typed, scaled points (see examples/)",
        "raw_writes": "true (default) / false: hide write_register, write_registers and write_coil",
    },
)
mcp = server.mcp


# ---------------------------------------------------------------- models


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PointValue(BaseModel):
    name: str
    value: float | bool | str | None = Field(description="Scaled value in `unit`, enum label, or bool")
    raw: float | int | bool | None = Field(description="Decoded number before scaling")
    unit: str
    table: str
    address: int
    timestamp: str
    error: str | None = Field(default=None, description="Why this point could not be read")


def _pv(r: PointReading) -> PointValue:
    return PointValue(
        name=r.name, value=r.value, raw=r.raw, unit=r.unit, table=r.table, address=r.address,
        timestamp=r.timestamp, error=r.error,
    )  # fmt: skip


class RegisterMapInfo(BaseModel):
    loaded: bool
    source: str | None
    device: dict[str, Any]
    unit_id: int | None
    points: list[dict[str, Any]]
    safe_state: list[dict[str, Any]]
    raw_writes: bool
    error: str | None = None


class RegisterRead(BaseModel):
    table: str
    address: int
    count: int
    registers: list[int] = Field(description="Raw unsigned 16-bit values")
    hex: list[str]
    decoded: list[float] | None = Field(default=None, description="Values decoded with decode_as")
    timestamp: str


class BitRead(BaseModel):
    table: str
    address: int
    count: int
    values: list[bool]
    timestamp: str


class PointWrite(BaseModel):
    name: str
    requested: float | bool | str
    written: float | bool | str = Field(description="Value after rounding to the register's resolution")
    read_back: float | bool | str | None
    unit: str
    matches: bool = Field(description="True if the read-back equals the written value")
    timestamp: str


# ---------------------------------------------------------------- read tools


@mcp.tool(**READ)
def list_points() -> RegisterMapInfo:
    """Describe the loaded register map: device, every named point (table, 0-based address,
    type, scaling, unit, writable, min/max, enum) and the safe-state steps. Works without a
    connection."""
    m, error = server.register_map, server.map_error
    raw_ok = _raw_writes_allowed(server.settings)
    if m is None:
        return RegisterMapInfo(
            loaded=False, source=None, device={}, unit_id=None, points=[], safe_state=[], raw_writes=raw_ok,
            error=error or "No register map loaded (--option register_map=/path/map.yaml); raw tools only.",
        )  # fmt: skip
    opt = server.settings.options.get("unit_id")
    return RegisterMapInfo(
        loaded=True,
        source=m.source,
        device=m.device,
        unit_id=int(opt) if opt and opt.isdigit() else (m.unit_id if m.unit_id is not None else 1),
        points=[p.describe() for p in m.points.values()],
        safe_state=[{"point": s.point, "value": s.value} for s in m.safe_state],
        raw_writes=raw_ok,
    )


@mcp.tool(**READ)
def read_points(
    names: Annotated[
        list[str] | None, Field(max_length=200, description="Point names from list_points; omit for all points")
    ] = None,
) -> list[PointValue]:
    """Read named points from the register map, decoded and scaled into engineering units
    (e.g. process_temperature = 25.1 °C). A point that cannot be read gets an `error`
    instead of failing the whole call."""
    return [_pv(r) for r in server.driver.read_points(names)]


_DECODE = Literal["none", "int16", "uint32", "int32", "float32", "float64"]


@mcp.tool(**READ)
def read_registers(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based start address")],
    count: Annotated[int, Field(ge=1, le=MAX_READ_REGISTERS, description="Number of registers")] = 1,
    table: Annotated[Literal["holding", "input"], Field(description="holding (FC03) or input (FC04)")] = "holding",
    decode_as: Annotated[_DECODE, Field(description="Also decode the registers as this type")] = "none",
    word_order: Annotated[Literal["big", "little"], Field(description="32/64-bit: big = high word first")] = "big",
    byte_order: Annotated[Literal["big", "little"], Field(description="big = standard Modbus byte order")] = "big",
) -> RegisterRead:
    """Read raw holding or input registers (unsigned 16-bit), optionally decoded as int16,
    32-bit or 64-bit values. For exploring a device; prefer read_points when a register map
    describes it."""
    regs = server.driver.read_registers(table, address, count)
    decoded = None
    if decode_as != "none":
        code, size = DATA_TYPES[decode_as]
        if count % size:
            raise InstrumentError(f"{decode_as} needs a multiple of {size} registers; got count={count}.")
        decoded = [
            float(struct.unpack(">" + code, registers_to_bytes(regs[i : i + size], word_order, byte_order))[0])
            for i in range(0, count, size)
        ]
    return RegisterRead(
        table=table, address=address, count=count, registers=regs, hex=[f"0x{r:04X}" for r in regs],
        decoded=decoded, timestamp=_now(),
    )  # fmt: skip


@mcp.tool(**READ)
def read_coils(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based start address")],
    count: Annotated[int, Field(ge=1, le=MAX_READ_BITS, description="Number of coils")] = 1,
) -> BitRead:
    """Read coils (FC01): single-bit outputs such as run/stop or relay states."""
    bits = server.driver.read_bits("coil", address, count)
    return BitRead(table="coil", address=address, count=count, values=bits, timestamp=_now())


@mcp.tool(**READ)
def read_discrete_inputs(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based start address")],
    count: Annotated[int, Field(ge=1, le=MAX_READ_BITS, description="Number of inputs")] = 1,
) -> BitRead:
    """Read discrete inputs (FC02): single-bit, read-only status such as alarms or limit switches."""
    bits = server.driver.read_bits("discrete", address, count)
    return BitRead(table="discrete", address=address, count=count, values=bits, timestamp=_now())


# ---------------------------------------------------------------- hazard tools


@mcp.tool(**HAZARD)
def write_point(
    name: Annotated[str, Field(description="A writable point from list_points")],
    value: Annotated[
        float | bool | str, Field(description="Engineering value (e.g. 37.5 for °C), true/false, or an enum label")
    ],
) -> PointWrite:
    """Write a named point from the register map (setpoint, mode, output enable...). The value
    is checked against the point's writable flag, min/max or enum BEFORE sending, converted
    to raw registers, written, then read back. Changing setpoints and outputs acts on real
    equipment."""
    reading, written = server.driver.write_point(name, value)
    return PointWrite(
        name=name,
        requested=value,
        written=written,
        read_back=reading.value,
        unit=reading.unit,
        matches=_same(reading.value, written),
        timestamp=_now(),
    )


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    return bool(a == b)


def _read_back(address: int, count: int) -> list[int] | None:
    try:
        return server.driver.read_registers("holding", address, count)
    except InstrumentError:
        return None  # write-only / command registers often cannot be read


@mcp.tool(**HAZARD)
def write_register(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based holding-register address")],
    value: Annotated[int, Field(ge=-32768, le=65535, description="Raw value (negative = int16 two's complement)")],
) -> dict[str, Any]:
    """Write one raw holding register (FC06). No scaling or limits are applied, so only use it
    for addresses the register map does not describe (mapped addresses are refused: use
    write_point). Returns a read-back if the register is readable."""
    server.driver.write_register(address, value)
    return {"address": address, "written": value & 0xFFFF, "read_back": _read_back(address, 1), "timestamp": _now()}


@mcp.tool(**HAZARD)
def write_registers(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based start address")],
    values: Annotated[
        list[Annotated[int, Field(ge=-32768, le=65535)]],
        Field(min_length=1, max_length=MAX_WRITE_REGISTERS, description="Raw 16-bit values"),
    ],
) -> dict[str, Any]:
    """Write consecutive raw holding registers (FC16), e.g. both halves of a 32-bit value.
    No scaling or limits are applied; mapped addresses are refused (use write_point)."""
    server.driver.write_registers(address, values)
    return {
        "address": address,
        "written": [v & 0xFFFF for v in values],
        "read_back": _read_back(address, len(values)),
        "timestamp": _now(),
    }


@mcp.tool(**HAZARD)
def write_coil(
    address: Annotated[int, Field(ge=0, le=65535, description="0-based coil address")],
    value: Annotated[bool, Field(description="true = ON (0xFF00), false = OFF")],
) -> dict[str, Any]:
    """Switch one raw coil (FC05). Coils often start or stop equipment (heaters, pumps,
    motors); mapped coils are refused here (use write_point)."""
    server.driver.write_coil(address, value)
    try:
        back: bool | None = server.driver.read_bits("coil", address, 1)[0]
    except InstrumentError:
        back = None
    return {"address": address, "written": value, "read_back": back, "timestamp": _now()}


# ---------------------------------------------------------------- safety


@mcp.tool(**SAFETY)
def apply_safe_state() -> dict[str, Any]:
    """Put the device into the safe state defined in the register map (e.g. heater output
    off, controller to standby), writing each step in order and reading it back. Every
    step is attempted even if an earlier one fails. Call it immediately if anything looks
    wrong."""
    steps = server.driver.apply_safe_state()
    return {"steps": steps, "all_ok": all(s["ok"] for s in steps), "timestamp": _now()}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
