import json
import struct

import pytest
from labmcp_modbus.registers import (
    Point,
    RegisterMapError,
    bytes_to_registers,
    example_map_path,
    load_register_map,
    parse_register_map,
    registers_to_bytes,
)


def _map(points, safe_state=None, device=None):
    doc = {"device": device or {}, "points": points}
    if safe_state is not None:
        doc["safe_state"] = safe_state
    return parse_register_map(doc)


@pytest.mark.parametrize(
    ("word", "byte", "regs"),
    [
        ("big", "big", [0x3F80, 0x0000]),  # ABCD
        ("little", "big", [0x0000, 0x3F80]),  # CDAB
        ("big", "little", [0x803F, 0x0000]),  # BADC
        ("little", "little", [0x0000, 0x803F]),  # DCBA
    ],
)
def test_word_and_byte_order(word, byte, regs):
    data = struct.pack(">f", 1.0)
    assert bytes_to_registers(data, word, byte) == regs
    assert registers_to_bytes(regs, word, byte) == data
    p = Point("x", "holding", 0, "float32", word_order=word, byte_order=byte)
    assert p.decode(regs)[0] == 1.0


def test_decode_scaled_and_typed():
    assert Point("t", "input", 0, "int16", scale=0.1).decode([0xFF85])[0] == pytest.approx(-12.3)
    assert Point("n", "input", 0, "uint32").decode([0x0001, 0x0000])[0] == 65536
    assert Point("n", "input", 0, "int32").decode([0xFFFF, 0xFFFE])[0] == -2
    assert Point("f", "input", 0, "float64").decode(bytes_to_registers(struct.pack(">d", 2.5), "big", "big"))[0] == 2.5
    assert Point("k", "input", 0, "uint16", scale=2, offset=-273.15).decode([150])[0] == pytest.approx(26.85)
    assert Point("b", "discrete", 3, "bool").decode([True]) == (True, True)
    assert Point("e", "holding", 0, "uint16", enum={0: "off", 1: "on"}).decode([1]) == ("on", 1)


def test_encode_checks_limits_and_rounds():
    p = Point("sp", "holding", 0, "int16", scale=0.1, unit="°C", writable=True, min=0, max=150)
    assert p.encode(37.54) == ([375], 37.5)
    assert p.encode(-0.0) == ([0], 0.0)
    with pytest.raises(ValueError, match="above the register map maximum"):
        p.encode(150.04)
    with pytest.raises(ValueError, match="below the register map minimum"):
        p.encode(-1)
    with pytest.raises(ValueError, match="needs a number"):
        p.encode("hot")
    with pytest.raises(ValueError, match="not writable"):
        Point("pv", "holding", 1, "int16", writable=False).encode(1)


def test_encode_enum_bool_and_float():
    mode = Point("mode", "holding", 1, "uint16", writable=True, enum={0: "standby", 1: "auto"})
    assert mode.encode("AUTO") == ([1], "auto")
    assert mode.encode(0) == ([0], "standby")
    with pytest.raises(ValueError, match="only accepts"):
        mode.encode(7)
    coil = Point("en", "coil", 0, "bool", writable=True)
    assert coil.encode(True) == (True, True)
    assert coil.encode("off") == (False, False)
    with pytest.raises(ValueError, match="true/false"):
        coil.encode(2)
    pb = Point("pb", "holding", 4, "float32", writable=True, min=0.5, max=100, word_order="little")
    regs, stored = pb.encode(12.5)
    assert pb.decode(regs)[0] == 12.5 and stored == 12.5


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"table": "holding", "address": 0, "writable": True, "maximum": 5}, "unknown field"),
        ({"table": "holding", "address": 0, "type": "int16", "writable": True, "min": 0}, "must define both min and max"),
        ({"table": "input", "address": 0, "writable": True, "min": 0, "max": 1}, "read-only"),
        ({"table": "coil", "address": 0, "type": "int16"}, "must have type 'bool'"),
        ({"table": "holding", "address": 70000}, "address"),
        ({"table": "holding", "address": 65535, "type": "float32"}, "runs past"),
        ({"table": "holdings", "address": 0}, "table must be"),
        ({"table": "holding", "address": 0, "type": "float32", "enum": {0: "a"}}, "enum"),
        ({"table": "holding", "address": 0, "scale": 0}, "scale"),
        ({"table": "holding", "address": 0, "writable": True, "min": 5, "max": 1}, "greater than max"),
    ],
)
def test_invalid_points(spec, message):
    with pytest.raises(RegisterMapError, match=message):
        _map({"p": spec})


def test_invalid_documents_and_safe_state():
    sp = {"table": "holding", "address": 0, "type": "int16", "writable": True, "min": 0, "max": 100}
    with pytest.raises(RegisterMapError, match="unknown point"):
        _map({"sp": sp}, safe_state=[{"point": "nope", "value": 1}])
    with pytest.raises(RegisterMapError, match="above the register map maximum"):
        _map({"sp": sp}, safe_state=[{"point": "sp", "value": 500}])
    with pytest.raises(RegisterMapError, match="duplicate"):
        parse_register_map({"points": [{"name": "a", **sp}, {"name": "a", **sp}]})
    with pytest.raises(RegisterMapError, match="unknown top-level"):
        parse_register_map({"points": {}, "limits": {}})
    with pytest.raises(RegisterMapError, match="unit_id"):
        _map({}, device={"unit_id": 300})


def test_load_json_and_yaml(tmp_path):
    doc = {"device": {"unit_id": 7}, "points": [{"name": "pv", "table": "input", "address": 3, "type": "int16"}]}
    (tmp_path / "m.json").write_text(json.dumps(doc), encoding="utf-8")
    m = load_register_map(tmp_path / "m.json")
    assert m.unit_id == 7 and m.point("pv").address == 3
    (tmp_path / "m.yaml").write_text("points:\n  pv: {table: input, address: 3, type: int16, unit: degC}\n", encoding="utf-8")
    assert load_register_map(tmp_path / "m.yaml").point("pv").unit == "degC"
    (tmp_path / "bad.yaml").write_text("points: [\n", encoding="utf-8")
    with pytest.raises(RegisterMapError, match="invalid YAML"):
        load_register_map(tmp_path / "bad.yaml")
    with pytest.raises(RegisterMapError, match="Cannot read"):
        load_register_map(tmp_path / "missing.yaml")


def test_example_map():
    m = load_register_map(example_map_path())
    assert m.point("setpoint").max == 150
    assert m.point("control_mode").enum[0] == "standby"
    assert [s.point for s in m.safe_state] == ["output_enable", "control_mode"]
    assert [p.name for p in m.overlapping("holding", 5, 1)] == ["proportional_band"]
