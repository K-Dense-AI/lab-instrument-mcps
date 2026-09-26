import csv
import struct

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, InstrumentTimeout, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_ble_health.driver import (
    BLEHealthSensor,
    decode_float32,
    decode_sfloat,
    hrv_statistics,
    normalize_address,
    parse_blood_pressure_measurement,
    parse_date_time,
    parse_heart_rate_measurement,
    parse_plx_continuous,
    parse_plx_spot_check,
    parse_temperature_measurement,
    parse_weight_measurement,
    parse_weight_scale_feature,
    to_celsius,
    to_mmhg,
)
from labmcp_ble_health.server import server
from labmcp_ble_health.simulator import (
    SIM_BP_MONITOR,
    SIM_HR_STRAP,
    SIM_KIT,
    SimulatedBLEBackend,
    enc_float,
    enc_sfloat,
)

# ------------------------------------------------------------------ IEEE 11073 floats


def test_sfloat_spec_examples():
    # PHD Transcoding WP v16 2.2.2: 114 mmHg with exponent 0 is 0x0072.
    assert decode_sfloat(0x0072).value == 114.0
    assert decode_sfloat(0xF16C).value == 36.4  # exponent -1, mantissa 364
    assert decode_sfloat(0x0FFF).value == -1.0  # negative mantissa
    assert decode_sfloat(0x2005).value == 500.0  # positive exponent


@pytest.mark.parametrize(
    "raw, special",
    [(0x07FF, "NaN"), (0x0800, "NRes"), (0x07FE, "+INF"), (0x0802, "-INF"), (0x0801, "reserved")],
)
def test_sfloat_special_values(raw, special):
    decoded = decode_sfloat(raw)
    assert decoded.value is None
    assert decoded.special == special


def test_float32_spec_examples():
    # PHD Transcoding WP v16 2.2.1: 36.4 degC = exponent -1 (0xFF), mantissa 364 -> 0xFF00016C.
    assert decode_float32(0xFF00016C).value == 36.4
    assert decode_float32(0x00FFFFFF).value == -1.0
    for raw, special in [(0x007FFFFF, "NaN"), (0x00800000, "NRes"), (0x007FFFFE, "+INF"), (0x00800002, "-INF")]:
        assert decode_float32(raw).special == special


def test_simulator_encoders_match_spec_bytes():
    assert enc_sfloat(114) == bytes.fromhex("7200")
    assert enc_float(36.4, -1) == bytes.fromhex("6c0100ff")  # little-endian 0xFF00016C


# ------------------------------------------------------------------ characteristic parsers


def test_heart_rate_uint8_with_rr():
    # flags 0x16: uint8 format, contact supported + detected, RR present; 60 bpm; RR 1024/1024 s, 512/1024 s
    m = parse_heart_rate_measurement(bytes.fromhex("163c" "0004" "0002"))
    assert m.bpm == 60
    assert m.sensor_contact is True
    assert m.energy_expended_kj is None
    assert m.rr_intervals_s == [1.0, 0.5]


def test_heart_rate_uint16_energy_no_contact_support():
    # flags 0x19: uint16 value, energy expended, RR; 300 bpm (animal), 10 kJ, one RR of 0.25 s
    m = parse_heart_rate_measurement(bytes.fromhex("19" "2c01" "0a00" "0001"))
    assert m.bpm == 300
    assert m.sensor_contact is None  # bit 2 clear: contact detection not supported
    assert m.energy_expended_kj == 10
    assert m.rr_intervals_s == [0.25]


def test_heart_rate_contact_lost():
    m = parse_heart_rate_measurement(bytes([0x04, 72]))  # supported, not detected
    assert m.sensor_contact is False


def test_heart_rate_malformed_packet():
    with pytest.raises(InstrumentProtocolError, match="Heart Rate"):
        parse_heart_rate_measurement(bytes([0x01, 0x3C]))  # says uint16 but only one byte


def test_plx_spot_check_all_fields():
    data = (
        bytes([0x1F])  # timestamp, status, device status, PAI, device clock not set
        + enc_sfloat(98)
        + enc_sfloat(72)
        + struct.pack("<HBBBBB", 2026, 3, 14, 9, 26, 53)
        + struct.pack("<H", (1 << 8) | (1 << 14))
        + (1 << 5).to_bytes(3, "little")
        + enc_sfloat(4.2, -1)
    )
    m = parse_plx_spot_check(data)
    assert (m.spo2_pct.value, m.pulse_rate_bpm.value) == (98.0, 72.0)
    assert m.timestamp == "2026-03-14T09:26:53"
    assert m.measurement_status == ["fully_qualified_data", "questionable_measurement_detected"]
    assert m.device_sensor_status == ["low_perfusion_detected"]
    assert m.pulse_amplitude_index_pct.value == 4.2
    assert m.device_clock_not_set


def test_plx_continuous_fast_slow_and_nan():
    data = bytes([0x03]) + enc_sfloat(None) + enc_sfloat(None) + enc_sfloat(96) + enc_sfloat(70) + enc_sfloat(97) + enc_sfloat(68)
    m = parse_plx_continuous(data)
    assert m.spo2_pct.special == "NaN" and m.pulse_rate_bpm.special == "NaN"
    assert m.spo2_fast_pct.value == 96 and m.pulse_rate_slow_bpm.value == 68
    assert m.measurement_status == []


def test_blood_pressure_kpa_and_status():
    # flags: kPa, pulse rate, status; 16.0 / 10.7 / 12.4 kPa (exponent -1)
    data = (
        bytes([0x01 | 0x04 | 0x10])
        + enc_sfloat(16.0, -1)
        + enc_sfloat(10.7, -1)
        + enc_sfloat(12.4, -1)
        + enc_sfloat(64)
        + struct.pack("<H", (1 << 2) | (0b01 << 3))
    )
    m = parse_blood_pressure_measurement(data)
    assert m.unit == "kPa"
    assert to_mmhg(m.systolic, m.unit) == pytest.approx(120.0, abs=0.1)
    assert m.pulse_rate_bpm.value == 64
    assert m.timestamp is None and m.user_id is None
    assert m.status == ["irregular_pulse_detected", "pulse_rate_exceeds_upper_limit"]


def test_blood_pressure_mmhg_nan_map():
    data = bytes([0x00]) + enc_sfloat(118) + enc_sfloat(76) + enc_sfloat(None)
    m = parse_blood_pressure_measurement(data)
    assert to_mmhg(m.systolic, m.unit) == 118
    assert m.mean_arterial.special == "NaN"
    assert to_mmhg(m.mean_arterial, m.unit) is None


def test_temperature_fahrenheit_with_type():
    data = bytes([0x05]) + enc_float(98.6, -1) + bytes([6])  # F, type present (mouth)
    m = parse_temperature_measurement(data)
    assert m.unit == "F" and m.value.value == 98.6
    assert to_celsius(m.value, m.unit) == pytest.approx(37.0, abs=0.001)
    assert m.temperature_type == "mouth"


def test_weight_si_and_imperial():
    si = parse_weight_measurement(bytes([0x00]) + struct.pack("<H", 16000))
    assert si.unit == "kg" and si.weight == 80.0  # 16000 x 0.005 kg (PHD WP 3.5.4.1 example)
    imp = parse_weight_measurement(bytes([0x0D]) + struct.pack("<H", 16000) + bytes([3]) + struct.pack("<HH", 245, 701))
    assert imp.unit == "lb" and imp.weight == 160.0
    assert imp.user_id == 3 and imp.bmi_kg_m2 == 24.5
    assert imp.height == 70.1 and imp.height_unit == "in"


def test_weight_unsuccessful():
    m = parse_weight_measurement(bytes([0x00, 0xFF, 0xFF]))
    assert m.weight is None


def test_weight_scale_feature_resolution():
    f = parse_weight_scale_feature(struct.pack("<I", 0b111 | (0b0111 << 3) | (0b011 << 7)))
    assert f["weight_resolution_kg"] == 0.005 and f["height_resolution_m"] == 0.001
    assert f["bmi_supported"]


def test_date_time_unknown_year():
    assert parse_date_time(struct.pack("<HBBBBB", 0, 5, 1, 12, 0, 0)) is None


def test_hrv_statistics():
    stats = hrv_statistics([0.8, 0.85, 0.8, 0.9, 3.0])
    assert stats["rr_excluded"] == 1  # 3.0 s is outside 0.3-2.0 s
    assert stats["mean_rr_ms"] == 837.5
    assert stats["sdnn_ms"] == pytest.approx(47.9, abs=0.05)
    assert stats["rmssd_ms"] == pytest.approx(70.7, abs=0.05)
    assert stats["pnn50_pct"] == pytest.approx(33.3, abs=0.05)


def test_normalize_address():
    assert normalize_address("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    assert normalize_address("ble://1a2b3c4d-1111-2222-3333-444455556666").startswith("1A2B3C4D")
    with pytest.raises(InstrumentConnectionError, match="not a Bluetooth LE address"):
        normalize_address("/dev/ttyUSB0")


# ------------------------------------------------------------------ driver against the simulator


def make_driver(address: str = SIM_KIT) -> BLEHealthSensor:
    return BLEHealthSensor(SimulatedBLEBackend(address), address, settle_s=0.3)


def test_driver_device_info_and_battery():
    d = make_driver()
    info = d.device_info()
    assert info["manufacturer"] == "LabMCP Simulator"
    assert "heart_rate" in info["services"]
    assert info["features"]["body_sensor_location"] == "chest"
    assert d.battery_level() == 86


def test_driver_records_heart_rate_with_rr():
    d = make_driver(SIM_HR_STRAP)
    samples, malformed, disconnected = d.record_heart_rate(2.6)
    assert len(samples) >= 2 and malformed == 0 and not disconnected
    rr = [r for _, m in samples for r in m.rr_intervals_s]
    assert rr and all(0.7 < r < 1.2 for r in rr)
    assert all(50 <= m.bpm <= 80 for _, m in samples)


def test_driver_blood_pressure_returns_stored_then_new():
    d = make_driver(SIM_BP_MONITOR)
    final, cuff = d.blood_pressure(timeout_s=10)
    assert len(final) == 2  # stored measurement from yesterday, then the new one
    assert final[-1].systolic.value == 121 and final[-1].diastolic.value == 79
    assert final[0].timestamp < final[-1].timestamp
    assert max(c.systolic.value for c in cuff) == 165


def test_driver_missing_service_is_reported():
    d = make_driver(SIM_HR_STRAP)
    with pytest.raises(InstrumentProtocolError, match="does not expose Blood Pressure Measurement"):
        d.blood_pressure(timeout_s=5)


def test_driver_unknown_simulated_address():
    d = make_driver("AA:BB:CC:DD:EE:FF")
    with pytest.raises(InstrumentConnectionError, match="not found"):
        d.battery_level()


def test_driver_wait_times_out_without_measurement():
    d = make_driver(SIM_BP_MONITOR)
    d.ensure_connected()
    with pytest.raises(InstrumentTimeout, match="No measurement"):
        d.listen([0x2A35], seconds=0.5, done=lambda ps: False)


# ------------------------------------------------------------------ MCP round trip


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server, address="") as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "LabMCP-SIM-KIT"
        server.driver.settle_s = 0.3

        scan = (await client.call_tool("scan_devices", {"timeout_s": 1, "service": "heart_rate"})).structured_content
        assert {d["address"] for d in scan["devices"]} == {SIM_KIT, SIM_HR_STRAP}

        battery = (await client.call_tool("read_battery", {})).structured_content
        assert battery["battery_pct"] == 86

        csv_path = tmp_path / "hr.csv"
        hr = (
            await client.call_tool("record_heart_rate", {"duration_s": 5, "max_points": 10, "save_path": str(csv_path)})
        ).structured_content
        assert hr["notifications"] >= 4
        assert 50 < hr["bpm_mean"] < 80
        assert hr["hrv"]["rr_count"] >= 4 and hr["hrv"]["rmssd_ms"] > 0
        assert hr["sensor_contact_pct"] == 100.0
        rows = list(csv.reader(csv_path.open()))
        assert rows[0][0] == "t_s" and len(rows) == hr["notifications"] + 1

        ox = (await client.call_tool("read_pulse_oximetry", {"mode": "continuous", "duration_s": 2.5})).structured_content
        assert ox["unavailable_samples"] == 1  # the first packet is NaN while acquiring
        assert 94 <= ox["spo2_pct"] <= 100

        bp = (await client.call_tool("wait_for_blood_pressure", {"timeout_s": 15})).structured_content
        assert (bp["systolic_mmhg"], bp["diastolic_mmhg"], bp["mean_arterial_mmhg"]) == (121, 79, 93)
        assert len(bp["other_measurements"]) == 1

        temp = (await client.call_tool("read_temperature", {"timeout_s": 10})).structured_content
        assert temp["temperature_c"] == 36.8 and temp["final"] is True
        assert temp["measurement_site"] == "tympanum_ear_drum"

        weight = (await client.call_tool("read_weight", {"timeout_s": 10})).structured_content
        assert weight["weight_kg"] == 72.35 and weight["bmi_kg_m2"] == 22.8 and weight["height_m"] == 1.782


async def test_single_profile_device_reports_missing_service():
    try:
        async with simulated_client(server, address=SIM_HR_STRAP) as client:
            with pytest.raises(Exception, match="does not expose Weight Measurement"):
                await client.call_tool("read_weight", {"timeout_s": 5})
    finally:
        server.configure(address="")  # back to the default simulated kit


async def test_read_only_keeps_all_read_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        for name in ("scan_devices", "record_heart_rate", "wait_for_blood_pressure", "read_weight"):
            assert name in names  # every instrument tool is read-only
        assert "reconnect" in names


async def test_record_duration_limit():
    async with simulated_client(server, limits={"max_record_duration_s": 30}) as client:
        with pytest.raises(Exception, match="max_record_duration_s"):
            await client.call_tool("record_heart_rate", {"duration_s": 60})


async def test_wait_limit():
    async with simulated_client(server, limits={"max_wait_s": 20}) as client:
        with pytest.raises(Exception, match="max_wait_s"):
            await client.call_tool("wait_for_blood_pressure", {"timeout_s": 60})
        with pytest.raises(Exception, match="max_wait_s"):
            await client.call_tool("read_pulse_oximetry", {"mode": "spot_check", "timeout_s": 60})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_wait_s": 10})
    with pytest.raises(SafetyLimitError):
        server.check("max_wait_s", 11)


# ------------------------------------------------------------------ regression tests (spec review)


def test_enc_sfloat_rejects_special_value_mantissas():
    # SFLOAT mantissa 0x802 (-2046) is the -INF special value, not a number.
    with pytest.raises(ValueError):
        enc_sfloat(-2046)
    assert decode_sfloat(struct.unpack("<H", enc_sfloat(-2045))[0]).value == -2045.0


def test_temperature_waits_for_final_even_when_accepting_intermediate():
    # Intermediate values arrive 1 s apart, longer than settle_s: the first intermediate must not
    # end the wait before the final Temperature Measurement arrives.
    backend = SimulatedBLEBackend(SIM_KIT)
    backend.temperature_step_s = 0.6
    sensor = BLEHealthSensor(backend, SIM_KIT, settle_s=0.3)
    readings, site = sensor.temperature(10.0, accept_intermediate=True)
    finals = [m for is_final, m in readings if is_final]
    assert len(finals) == 1
    assert finals[0].value.value == 36.8
    assert site == "tympanum_ear_drum"


def test_temperature_falls_back_to_intermediate_on_timeout():
    backend = SimulatedBLEBackend(SIM_KIT)
    backend.temperature_step_s = 0.2
    backend.temperature_final = False
    sensor = BLEHealthSensor(backend, SIM_KIT, settle_s=0.3)
    readings, _ = sensor.temperature(1.5, accept_intermediate=True)
    assert readings and not any(is_final for is_final, _ in readings)
    assert readings[-1][1].value.value == 36.7
    # Without accept_intermediate the same situation is a timeout.
    backend2 = SimulatedBLEBackend(SIM_KIT)
    backend2.temperature_final = False
    with pytest.raises(InstrumentTimeout):
        BLEHealthSensor(backend2, SIM_KIT, settle_s=0.3).temperature(1.5)


def test_bleak_adapter_kwargs_match_installed_bleak(monkeypatch):
    import bleak.args.bluez as bluez_args
    from labmcp_ble_health.driver import BleakBackend

    backend = BleakBackend.__new__(BleakBackend)
    backend._adapter, backend._pair = "hci1", False
    expected = (
        {"bluez": {"adapter": "hci1"}}
        if "adapter" in bluez_args.BlueZScannerArgs.__annotations__
        else {"adapter": "hci1"}
    )
    assert backend._scanner_kwargs() == expected
    # bleak 1.0.x: BlueZScannerArgs has no 'adapter' key -> use the adapter keyword.
    monkeypatch.setattr(bluez_args.BlueZScannerArgs, "__annotations__", {"filters": object})
    assert backend._scanner_kwargs() == {"adapter": "hci1"}
    backend._adapter = None
    assert backend._client_kwargs() == {"pair": False}
