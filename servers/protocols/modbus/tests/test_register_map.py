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


def test_encode_float32_overflow_is_a_refusal_not_a_crash():
    # struct.pack(">f", 1e39) raises OverflowError (not struct.error); it must become a ValueError
    # so the driver reports "Refused ... Nothing was sent" instead of an internal error.
    p = Point("big", "holding", 0, "float32", scale=1e-3, writable=True, min=0, max=1e37)
    with pytest.raises(ValueError, match="does not fit in a float32"):
        p.encode(1e37)


# ------------------------------------------------------------------ regressions (review 2026-09)

_SP = {"table": "holding", "address": 0, "type": "int16", "scale": 0.1, "writable": True, "min": 0, "max": 100}


def test_duplicate_keys_are_errors_not_last_one_wins(tmp_path):
    # PyYAML and json keep the LAST duplicate silently: a second `max` (or a second definition of a
    # point) would quietly replace the lab's limit.
    (tmp_path / "dup_max.yaml").write_text(
        "points:\n  sp: {table: holding, address: 0, type: int16, writable: true, min: 0, max: 50, max: 400}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegisterMapError, match="duplicate key 'max'"):
        load_register_map(tmp_path / "dup_max.yaml")
    (tmp_path / "dup_point.yaml").write_text(
        "points:\n"
        "  sp: {table: holding, address: 0, type: int16, writable: true, min: 0, max: 50}\n"
        "  sp: {table: holding, address: 0, type: int16, writable: true, min: 0, max: 400}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegisterMapError, match="duplicate key 'sp'"):
        load_register_map(tmp_path / "dup_point.yaml")
    (tmp_path / "dup.json").write_text(
        '{"points": {"sp": {"table": "holding", "address": 0, "type": "int16", "writable": true,'
        ' "min": 0, "max": 50, "max": 400}}}',
        encoding="utf-8",
    )
    with pytest.raises(RegisterMapError, match="duplicate key 'max'"):
        load_register_map(tmp_path / "dup.json")
    # YAML merge keys may still be overridden, as YAML intends
    (tmp_path / "merge.yaml").write_text(
        "base: &sp {table: holding, type: int16, writable: true, min: 0, max: 50}\n"
        "points:\n  sp: {<<: *sp, address: 0, max: 40}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegisterMapError, match="unknown top-level"):  # `base` is not allowed, but it parsed
        load_register_map(tmp_path / "merge.yaml")


def test_json_with_utf8_bom_loads(tmp_path):
    doc = {"points": {"pv": {"table": "input", "address": 0}}}
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode())  # Windows Notepad
    assert load_register_map(tmp_path / "bom.json").point("pv").address == 0


def test_overlapping_writable_points_are_refused():
    # A second writable view of the setpoint register with looser limits would bypass `max: 100`.
    loose = {**_SP, "type": "uint16", "scale": 1, "max": 4000}
    with pytest.raises(RegisterMapError, match="share holding address"):
        _map({"sp": _SP, "sp_raw": loose})
    wide = {"table": "holding", "address": 1, "type": "float32", "writable": True, "min": 0, "max": 1}
    with pytest.raises(RegisterMapError, match="'sp_hi'"):
        _map({"wide": wide, "sp_hi": {**_SP, "address": 2}})
    # read-only views of a writable register, and adjacent writable points, are fine
    m = _map({"sp": _SP, "sp_raw": {"table": "holding", "address": 0}, "next": {**_SP, "address": 1}})
    assert set(m.points) == {"sp", "sp_raw", "next"}
    assert _map({"a": {"table": "coil", "address": 0, "writable": True}, "b": {**_SP}}).points  # other table


@pytest.mark.parametrize("safe_state", [{"point": "sp", "value": 0}, 5, [{"point": ["sp"], "value": 0}]])
def test_malformed_safe_state_is_a_map_error(safe_state):
    with pytest.raises(RegisterMapError, match="safe_state"):
        parse_register_map({"points": {"sp": _SP}, "safe_state": safe_state})


def test_encode_never_leaks_overflow_error():
    tiny = Point("t", "holding", 0, "int32", scale=1e-310, writable=True, min=0, max=1e300)
    with pytest.raises(ValueError, match="does not fit"):
        tiny.encode(1e10)  # (value - offset) / scale == inf; round(inf) raised OverflowError
    sp = Point("sp", "holding", 0, "int16", writable=True, min=0, max=100)
    with pytest.raises(ValueError, match="finite number"):
        sp.encode(10**400)  # float(10**400) raised OverflowError


def test_stored_value_is_rechecked_for_float_types():
    # float32 underflows to 0 with a huge scale: 0 is below min and must be refused, not sent.
    p = Point("f", "holding", 0, "float32", scale=1e50, writable=True, min=1, max=1e60)
    with pytest.raises(ValueError, match=r"stored as 0 .*below the register map minimum"):
        p.encode(5)
    q = Point("q", "holding", 0, "int16", scale=0.1, unit="%", writable=True, min=0.05, max=1)
    with pytest.raises(ValueError, match="stored as 0 %"):
        q.encode(0.05)
    f = Point("pb", "holding", 0, "float32", writable=True, min=0, max=100)
    assert f.encode(12.3) == (bytes_to_registers(struct.pack(">f", 12.3), "big", "big"), 12.3)


@pytest.mark.parametrize(
    ("enum", "message"),
    [
        ({1.5: "a"}, "must be integers"),  # int(1.5) == 1 silently
        ({True: "a"}, "must be integers"),
        ({"x": "a"}, "must be integers"),
        ({70000: "a"}, "does not fit in a uint16"),
        ({-1: "a"}, "does not fit in a uint16"),
        ({0: "Auto", 1: "auto"}, "more than one value"),  # a write by label would be ambiguous
        ({1: "a", "1": "b"}, "listed twice"),
    ],
)
def test_invalid_enums(enum, message):
    with pytest.raises(RegisterMapError, match=message):
        _map({"mode": {"table": "holding", "address": 0, "writable": True, "enum": enum}})


def test_json_enum_keys_are_strings():
    m = parse_register_map(json.loads('{"points": {"m": {"table": "holding", "address": 0, "type": "int16",'
                                      ' "writable": true, "enum": {"-1": "off", "1": "on"}}}}'))
    assert m.point("m").encode("off") == ([0xFFFF], "off")
