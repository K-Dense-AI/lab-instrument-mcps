"""Register maps: named, typed, scaled Modbus points loaded from YAML or JSON.

A register map turns raw 16-bit registers into quantities a scientist can use
("process_temperature = 25.1 °C") and carries the lab's write rules: only points
marked ``writable`` can be written by name, and numeric writable points must
declare ``min``/``max`` (or an ``enum``), which are checked before anything is
sent.

Addresses are **0-based protocol (PDU) addresses**, as sent on the wire
(MODBUS Application Protocol Specification V1.1b3, 4.4: "In a MODBUS PDU each
data is addressed from 0 to 65535"). A manual that lists "register 40001" means
holding-register address 0.

Multi-register values: Modbus defines big-endian byte order within a register
(spec 4.2) but not the order of registers within a 32/64-bit value, so
``word_order`` (big = most significant register first) and ``byte_order``
(big = standard Modbus byte order inside each register) are configurable.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Table = Literal["holding", "input", "coil", "discrete"]
TABLES: tuple[str, ...] = ("holding", "input", "coil", "discrete")
REGISTER_TABLES = {"holding", "input"}
WRITABLE_TABLES = {"holding", "coil"}

# type -> (struct code, number of 16-bit registers)
DATA_TYPES: dict[str, tuple[str, int]] = {
    "uint16": ("H", 1),
    "int16": ("h", 1),
    "uint32": ("I", 2),
    "int32": ("i", 2),
    "float32": ("f", 2),
    "float64": ("d", 4),
    "bool": ("", 1),
}
INTEGER_TYPES = {"uint16", "int16", "uint32", "int32"}
FLOAT_TYPES = {"float32", "float64"}

_POINT_KEYS = {
    "table", "address", "type", "word_order", "byte_order", "scale", "offset", "unit",
    "description", "writable", "min", "max", "enum", "name",
}  # fmt: skip
_DEVICE_KEYS = {"name", "manufacturer", "model", "description", "unit_id", "word_order", "byte_order", "manual"}
_TOP_KEYS = {"device", "points", "safe_state"}


class RegisterMapError(ValueError):
    """The register map file is invalid."""


@dataclass
class Point:
    name: str
    table: Table
    address: int
    type: str = "uint16"
    word_order: Literal["big", "little"] = "big"
    byte_order: Literal["big", "little"] = "big"
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    description: str = ""
    writable: bool = False
    min: float | None = None
    max: float | None = None
    enum: dict[int, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        """Number of registers (or bits) the point occupies."""
        return DATA_TYPES[self.type][1]

    @property
    def is_bit(self) -> bool:
        return self.table in {"coil", "discrete"}

    def overlaps(self, table: str, address: int, count: int) -> bool:
        return self.table == table and address < self.address + self.count and self.address < address + count

    # ------------------------------------------------------------ decoding

    def decode(self, raw: list[int] | list[bool]) -> tuple[float | bool | str, float | int | bool]:
        """Return ``(value, raw_number)`` for the registers/bits read from the device."""
        if self.is_bit:
            bit = bool(raw[0])
            return bit, bit
        regs = [int(r) for r in raw]
        if self.type == "bool":
            return regs[0] != 0, regs[0]
        code, _ = DATA_TYPES[self.type]
        number = struct.unpack(">" + code, registers_to_bytes(regs, self.word_order, self.byte_order))[0]
        if self.enum and number in self.enum:
            return self.enum[number], number
        value = number * self.scale + self.offset
        if math.isfinite(value):  # hide binary noise such as 25.100000000000001 (float32: ~7 digits)
            value = float(f"{value:.7g}" if self.type == "float32" else f"{value:.12g}")
        return value, number

    # ------------------------------------------------------------ encoding

    def encode(self, value: float | bool | str) -> tuple[list[int] | bool, float | bool | str]:
        """Validate ``value`` against the map and return ``(registers_or_bit, value_as_stored)``.

        Raises :class:`ValueError` with a scientist-readable reason; nothing is sent.
        """
        if not self.writable:
            raise ValueError(f"point {self.name!r} is not writable in the register map")
        if self.is_bit or self.type == "bool":
            bit = _as_bool(value, self.name)
            return (bit if self.is_bit else [int(bit)]), bit
        if self.enum:
            number = self._enum_number(value)
            return self._pack(number), self.enum[number]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"point {self.name!r} needs a number, got {value!r}")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"point {self.name!r} needs a finite number, got {value!r}")
        self._check_range(value, value)
        raw = (value - self.offset) / self.scale
        if self.type in INTEGER_TYPES:
            number: float | int = round(raw)
            stored = float(f"{number * self.scale + self.offset:.12g}")
            self._check_range(stored, value)
        else:
            number, stored = raw, value
        return self._pack(number), stored

    def _enum_number(self, value: float | bool | str) -> int:
        if isinstance(value, str):
            for number, label in self.enum.items():
                if label.lower() == value.strip().lower():
                    return number
            if value.strip().lstrip("-").isdigit():
                value = int(value.strip())
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if is_number and int(value) == value and int(value) in self.enum:
            return int(value)
        allowed = ", ".join(f"{label} ({n})" for n, label in self.enum.items())
        raise ValueError(f"point {self.name!r} only accepts: {allowed}; got {value!r}")

    def _check_range(self, value: float, requested: float) -> None:
        if self.min is not None and value < self.min:
            raise ValueError(
                f"{requested:g} {self.unit} is below the register map minimum of {self.min:g} {self.unit} "
                f"for {self.name!r}"
            )
        if self.max is not None and value > self.max:
            raise ValueError(
                f"{requested:g} {self.unit} is above the register map maximum of {self.max:g} {self.unit} "
                f"for {self.name!r}"
            )

    def _pack(self, number: float | int) -> list[int]:
        code, _ = DATA_TYPES[self.type]
        try:
            data = struct.pack(">" + code, number)
        except (struct.error, OverflowError) as exc:  # float32 overflow raises OverflowError
            raise ValueError(f"{number!r} does not fit in a {self.type} register for {self.name!r}") from exc
        return bytes_to_registers(data, self.word_order, self.byte_order)

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "table": self.table,
            "address": self.address,
            "type": self.type,
            "unit": self.unit,
            "description": self.description,
            "writable": self.writable,
        }
        if self.count > 1:
            out["registers"] = self.count
            out["word_order"], out["byte_order"] = self.word_order, self.byte_order
        if self.scale != 1 or self.offset != 0:
            out["scale"], out["offset"] = self.scale, self.offset
        if self.min is not None or self.max is not None:
            out["min"], out["max"] = self.min, self.max
        if self.enum:
            out["enum"] = {str(k): v for k, v in self.enum.items()}
        return out


@dataclass
class SafeStep:
    point: str
    value: float | bool | str


@dataclass
class RegisterMap:
    points: dict[str, Point]
    device: dict[str, Any] = field(default_factory=dict)
    safe_state: list[SafeStep] = field(default_factory=list)
    source: str = ""

    @property
    def unit_id(self) -> int | None:
        uid = self.device.get("unit_id")
        return int(uid) if uid is not None else None

    def point(self, name: str) -> Point:
        try:
            return self.points[name]
        except KeyError:
            known = ", ".join(self.points) or "(none)"
            raise KeyError(f"No point named {name!r} in the register map. Known points: {known}") from None

    def overlapping(self, table: str, address: int, count: int) -> list[Point]:
        return [p for p in self.points.values() if p.overlaps(table, address, count)]


# ---------------------------------------------------------------- byte/word order


def registers_to_bytes(regs: list[int], word_order: str, byte_order: str) -> bytes:
    words = regs if word_order == "big" else list(reversed(regs))
    out = b""
    for w in words:
        b = int(w).to_bytes(2, "big")
        out += b if byte_order == "big" else b[::-1]
    return out


def bytes_to_registers(data: bytes, word_order: str, byte_order: str) -> list[int]:
    words = []
    for i in range(0, len(data), 2):
        b = data[i : i + 2]
        words.append(int.from_bytes(b if byte_order == "big" else b[::-1], "big"))
    return words if word_order == "big" else list(reversed(words))


# ---------------------------------------------------------------- loading


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "on", "1", "false", "off", "0"}:
        return value.strip().lower() in {"true", "on", "1"}
    raise ValueError(f"point {name!r} needs true/false, got {value!r}")


def _order(value: Any, where: str, key: str) -> Literal["big", "little"]:
    if value not in {"big", "little"}:
        raise RegisterMapError(f"{where}: {key} must be 'big' or 'little', got {value!r}")
    return value  # type: ignore[return-value]


def _number(value: Any, where: str, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RegisterMapError(f"{where}: {key} must be a finite number, got {value!r}")
    return float(value)


def _parse_point(name: str, spec: Any, device: dict[str, Any]) -> Point:
    where = f"point {name!r}"
    if not isinstance(spec, dict):
        raise RegisterMapError(f"{where}: expected a mapping of fields, got {type(spec).__name__}")
    unknown = set(spec) - _POINT_KEYS
    if unknown:
        raise RegisterMapError(
            f"{where}: unknown field(s) {sorted(unknown)}. Allowed: {sorted(_POINT_KEYS - {'name'})}"
        )
    table = spec.get("table")
    if table not in TABLES:
        raise RegisterMapError(f"{where}: table must be one of {list(TABLES)}, got {table!r}")
    address = spec.get("address")
    if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address <= 65535:
        raise RegisterMapError(f"{where}: address must be an integer 0-65535 (0-based PDU address)")
    default_type = "bool" if table in {"coil", "discrete"} else "uint16"
    dtype = spec.get("type", default_type)
    if dtype not in DATA_TYPES:
        raise RegisterMapError(f"{where}: type must be one of {list(DATA_TYPES)}, got {dtype!r}")
    if table in {"coil", "discrete"} and dtype != "bool":
        raise RegisterMapError(f"{where}: {table} points are single bits and must have type 'bool'")
    point = Point(
        name=name,
        table=table,
        address=address,
        type=dtype,
        word_order=_order(spec.get("word_order", device.get("word_order", "big")), where, "word_order"),
        byte_order=_order(spec.get("byte_order", device.get("byte_order", "big")), where, "byte_order"),
        scale=_number(spec.get("scale", 1.0), where, "scale"),
        offset=_number(spec.get("offset", 0.0), where, "offset"),
        unit=str(spec.get("unit", "")),
        description=str(spec.get("description", "")),
        writable=spec.get("writable", False),
        min=_number(spec["min"], where, "min") if spec.get("min") is not None else None,
        max=_number(spec["max"], where, "max") if spec.get("max") is not None else None,
    )
    if not isinstance(point.writable, bool):
        raise RegisterMapError(f"{where}: writable must be true or false")
    if point.scale == 0:
        raise RegisterMapError(f"{where}: scale must not be 0")
    if point.address + point.count > 65536:
        raise RegisterMapError(f"{where}: a {dtype} at address {address} runs past address 65535")
    if point.writable and table not in WRITABLE_TABLES:
        raise RegisterMapError(f"{where}: {table} points are read-only in Modbus and cannot be writable")
    if "enum" in spec:
        enum = spec["enum"]
        if dtype not in INTEGER_TYPES or not isinstance(enum, dict) or not enum:
            raise RegisterMapError(f"{where}: enum must be a non-empty mapping of integers to labels on an integer type")
        try:
            point.enum = {int(k): str(v) for k, v in enum.items()}
        except (TypeError, ValueError) as exc:
            raise RegisterMapError(f"{where}: enum keys must be integers") from exc
    if point.min is not None and point.max is not None and point.min > point.max:
        raise RegisterMapError(f"{where}: min ({point.min:g}) is greater than max ({point.max:g})")
    numeric = dtype not in {"bool"} and not point.enum
    if point.writable and numeric and (point.min is None or point.max is None):
        raise RegisterMapError(
            f"{where}: writable numeric points must define both min and max (in {point.unit or 'engineering units'}) "
            "or an enum, so the server can refuse unsafe values before sending them"
        )
    return point


def parse_register_map(data: Any, source: str = "") -> RegisterMap:
    """Validate a decoded YAML/JSON document and build a :class:`RegisterMap`."""
    if not isinstance(data, dict):
        raise RegisterMapError(f"{source or 'register map'}: top level must be a mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise RegisterMapError(f"unknown top-level key(s) {sorted(unknown)}. Allowed: {sorted(_TOP_KEYS)}")
    device = data.get("device") or {}
    if not isinstance(device, dict):
        raise RegisterMapError("device must be a mapping")
    unknown = set(device) - _DEVICE_KEYS
    if unknown:
        raise RegisterMapError(f"device: unknown key(s) {sorted(unknown)}. Allowed: {sorted(_DEVICE_KEYS)}")
    uid = device.get("unit_id")
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= 255):
        raise RegisterMapError("device.unit_id must be an integer 0-255")
    raw_points = data.get("points")
    if isinstance(raw_points, list):
        items = []
        for entry in raw_points:
            if not isinstance(entry, dict) or "name" not in entry:
                raise RegisterMapError("each entry of a points list needs a 'name'")
            items.append((str(entry["name"]), entry))
    elif isinstance(raw_points, dict):
        items = [(str(k), v) for k, v in raw_points.items()]
    else:
        raise RegisterMapError("points must be a mapping (name: {...}) or a list of {name: ..., ...}")
    points: dict[str, Point] = {}
    for name, spec in items:
        if name in points:
            raise RegisterMapError(f"duplicate point name {name!r}")
        points[name] = _parse_point(name, spec, device)
    safe_state: list[SafeStep] = []
    for i, step in enumerate(data.get("safe_state") or []):
        where = f"safe_state[{i}]"
        if not isinstance(step, dict) or set(step) != {"point", "value"}:
            raise RegisterMapError(f"{where}: each step must be {{point: <name>, value: <value>}}")
        if step["point"] not in points:
            raise RegisterMapError(f"{where}: unknown point {step['point']!r}")
        try:
            points[step["point"]].encode(step["value"])
        except ValueError as exc:
            raise RegisterMapError(f"{where}: {exc}") from exc
        safe_state.append(SafeStep(step["point"], step["value"]))
    return RegisterMap(points=points, device=dict(device), safe_state=safe_state, source=source)


def load_register_map(path: str | Path) -> RegisterMap:
    """Load a register map from a ``.yaml``/``.yml`` or ``.json`` file."""
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegisterMapError(f"Cannot read register map {p}: {exc}") from exc
    if p.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RegisterMapError(f"{p}: invalid YAML: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegisterMapError(f"{p}: invalid JSON: {exc}") from exc
    try:
        return parse_register_map(data, str(p))
    except RegisterMapError as exc:
        raise RegisterMapError(f"{p}: {exc}") from exc


def example_map_path() -> Path:
    """The bundled example map for a generic PID temperature controller."""
    return Path(__file__).parent / "examples" / "generic_pid_controller.yaml"
