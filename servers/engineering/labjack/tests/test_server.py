import csv
import math
import threading
import time

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_labjack.driver import LabJackT, dio_index, dio_name, open_device
from labmcp_labjack.server import _downsample, server
from labmcp_labjack.simulator import SimulatedLJM


def make_driver(model: str = "T7-Pro") -> LabJackT:
    return open_device(SimulatedLJM(model=model), "ANY", "ANY", "ANY")


# ------------------------------------------------------------------ driver


def test_identify_and_model_detection():
    d = make_driver("T7-Pro")
    info = d.identify()
    assert info["manufacturer"] == "LabJack"
    assert info["model"] == "T7-Pro"
    assert info["serial"] == "470012345"
    assert d.spec.max_resolution_index == 12
    assert make_driver("T7").spec.max_resolution_index == 8
    assert make_driver("T4").spec.model == "T4"
    assert make_driver("T8").spec.dac_max_v == 10.0


def test_open_wrong_identifier_or_type_is_connection_error():
    with pytest.raises(InstrumentConnectionError, match="1227"):
        open_device(SimulatedLJM(), "ANY", "ANY", "999")
    with pytest.raises(InstrumentConnectionError, match="device_type=T4"):
        open_device(SimulatedLJM(model="T7"), "T4", "ANY", "ANY")


def test_dio_names_roundtrip():
    assert dio_index("FIO3") == 3
    assert dio_index("eio0") == 8
    assert dio_index("CIO1") == 17
    assert dio_index("MIO2") == 22
    assert dio_index("DIO5") == 5
    assert dio_name(19) == "CIO3"
    with pytest.raises(InstrumentProtocolError):
        dio_index("FIO9")


def test_read_ain_signals_and_dac_loopback():
    d = make_driver()
    ain0, ain1, ain3 = d.read_ain([0, 1, 3])
    assert -2.1 < ain0 < 2.1
    assert 2.3 < ain1 < 2.7  # thermistor divider near 24 °C
    assert 0.7 < ain3 < 0.85  # LM34 at the terminal temperature
    assert d.write_dac(0, 1.5) == pytest.approx(1.5)
    assert d.read_ain([2])[0] == pytest.approx(1.5, abs=0.005)


def test_range_and_resolution_validation():
    d = make_driver("T7")
    d.read_ain([0], range_v=1.0, resolution_index=8)
    assert d.ain_ranges([0]) == [1.0]
    with pytest.raises(InstrumentProtocolError, match="not a T7 input range"):
        d.read_ain([0], range_v=5.0)
    with pytest.raises(InstrumentProtocolError, match="0-8"):
        d.read_ain([0], resolution_index=12)
    with pytest.raises(InstrumentProtocolError, match="AIN14 does not exist"):
        d.read_ain([14])


def test_device_error_reply_is_reported_with_code():
    # Bypass the driver's pre-check to see the device's own error reply.
    d = make_driver("T7")
    with pytest.raises(InstrumentProtocolError, match=r"LJM error 2370 \(AIN_RANGE_INVALID\)"):
        d.write_names(["AIN0_RANGE"], [5.0])
    with pytest.raises(InstrumentProtocolError, match="1294"):
        d.read_names(["NOT_A_REGISTER"])


def test_t7_differential_rules():
    d = make_driver("T7")
    d.read_ain([0], differential=True)
    assert d.read_names(["AIN0_NEGATIVE_CH"]) == [1.0]
    assert d.negative_inputs([0]) == ["AIN1"]
    d.read_ain([0])  # None keeps the configuration
    assert d.read_names(["AIN0_NEGATIVE_CH"]) == [1.0]
    with pytest.raises(InstrumentProtocolError, match="even channel"):
        d.read_ain([1], differential=True)
    d.read_ain([0], differential=False)
    assert d.read_names(["AIN0_NEGATIVE_CH"]) == [199.0]
    assert d.negative_inputs([0]) == ["GND"]


def test_t4_fixed_ranges_and_flexible_io_guard():
    d = make_driver("T4")
    with pytest.raises(InstrumentProtocolError, match="fixed"):
        d.read_ain([0], range_v=10.0)
    with pytest.raises(InstrumentProtocolError, match="differential"):
        d.read_ain([0], differential=True)
    assert d.ain_ranges([0, 5]) == [10.0, 2.5]
    d.set_dio(4, True)  # FIO4 becomes a digital output
    with pytest.raises(InstrumentProtocolError, match="digital OUTPUT"):
        d.read_ain([4])
    with pytest.raises(InstrumentProtocolError, match="DIO0 does not exist on the T4"):
        d.set_dio(0, True)


def test_t8_simultaneous_reads_use_capture_registers():
    ljm = SimulatedLJM(model="T8")
    d = open_device(ljm, "ANY", "ANY", "ANY")
    calls = []
    original = ljm.eReadNames
    ljm.eReadNames = lambda h, n, names: calls.append(list(names)) or original(h, n, names)
    d.read_ain([0, 1, 2], range_v=4.8, resolution_index=3)
    assert calls[-1] == ["AIN0", "AIN1_CAPTURE", "AIN2_CAPTURE"]
    assert ljm.device.res == {c: 3 for c in range(8)}  # one shared resolution index
    assert d.ain_ranges([0, 1]) == [4.8, 4.8]


def test_read_dio_does_not_change_direction():
    d = make_driver()
    d.set_dio(0, True)
    d.set_dio(1, False)
    states = {s["name"]: s for s in d.read_dio([0, 1, 2])}
    assert states["FIO0"] == {"line": 0, "name": "FIO0", "direction": "output", "high": True}
    assert states["FIO1"]["direction"] == "output" and states["FIO1"]["high"] is False
    assert states["FIO2"]["direction"] == "input" and states["FIO2"]["high"] is True  # pull-up
    assert d.read_dio([0])[0]["direction"] == "output"  # still an output after reading


def test_dac_hardware_range_per_model():
    with pytest.raises(InstrumentProtocolError, match="0-5 V"):
        make_driver("T7").write_dac(0, 7.0)
    assert make_driver("T8").write_dac(1, 7.0) == pytest.approx(7.0)


def test_thermocouple():
    d = make_driver("T7")
    r = d.read_thermocouple(0, "K")
    assert r.temperature_c == pytest.approx(37.0, abs=0.5)
    assert 20 < r.cjc_temperature_c < 35
    open_tc = d.read_thermocouple(5, "J")
    assert open_tc.temperature_c is None
    lm34 = d.read_thermocouple(1, "T", cjc="lm34", cjc_channel=3)
    assert 20 < lm34.cjc_temperature_c < 30
    with pytest.raises(InstrumentProtocolError, match="T4"):
        make_driver("T4").read_thermocouple(0, "K")
    with pytest.raises(InstrumentProtocolError, match="cjc_channel"):
        d.read_thermocouple(0, "K", cjc="lm34")


def test_t8_thermocouple_uses_documented_default_cjc_register():
    # Datasheet 14.1.1: on the T8 the default CJC register (TEMPERATURE_DEVICE_K, 60052) is mapped
    # by the firmware to TEMPERATURE#_CAPTURE, so CONFIG_B must be 60052, not 700 + 2n.
    d = make_driver("T8")
    r = d.read_thermocouple(3, "K")
    assert d.ljm.device.ef_config[(3, "B")] == 60052
    assert r.temperature_c == pytest.approx(37.0, abs=0.5)
    assert 20 < r.cjc_temperature_c < 35


def test_stream_two_channels():
    d = make_driver("T7")
    s = d.stream_ain([0, 1], scan_rate_hz=1000, num_scans=200)
    assert len(s.data) == 2 and all(len(col) == 200 for col in s.data)
    assert s.scan_rate_hz == pytest.approx(1000)
    assert max(s.data[0]) - min(s.data[0]) == pytest.approx(4.0, abs=0.3)  # 2 V sine, 2 periods
    assert s.skipped_scans == 0
    # the stream was stopped: command-response reads work again
    d.read_ain([0])


def test_stream_validation():
    with pytest.raises(InstrumentProtocolError, match="exceeds"):
        make_driver("T4").stream_ain([0, 1], scan_rate_hz=40_000, num_scans=10)
    with pytest.raises(InstrumentProtocolError, match="adjacent"):
        make_driver("T8").stream_ain([0, 2], scan_rate_hz=100, num_scans=10)
    with pytest.raises(InstrumentProtocolError, match="24-bit"):
        make_driver("T7-Pro").stream_ain([0], scan_rate_hz=100, num_scans=10, resolution_index=10)


def test_safe_state_releases_outputs():
    d = make_driver()
    d.write_dac(0, 2.0)
    d.write_dac(1, 1.0)
    d.set_dio(3, True)
    actions = d.safe_state()
    assert "DAC0 and DAC1 set to 0 V" in actions
    assert "FIO3 set to input" in actions
    assert d.read_dacs() == [0.0, 0.0]
    assert d.read_dio([3])[0]["direction"] == "input"
    d.set_dio(3, True)
    assert "FIO3 driven low" in d.safe_state("low")
    assert d.read_dio([3])[0] == {"line": 3, "name": "FIO3", "direction": "output", "high": False}


# ------------------------------------------------------------------ MCP


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server, options={"safe_dio": "EIO0"}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["model"] == "T7-Pro"
        assert "AIN13" in dev["analog_inputs"] and dev["input_ranges_v"] == [10.0, 1.0, 0.1, 0.01]

        ain = (await client.call_tool("read_analog_inputs", {"channels": [0, 1], "range_v": 10})).structured_content
        assert [r["channel"] for r in ain["readings"]] == ["AIN0", "AIN1"]
        assert ain["readings"][0]["negative_input"] == "GND"

        dac = (await client.call_tool("write_dac", {"dac": 0, "voltage_v": 2.5})).structured_content
        assert dac["readback_v"] == pytest.approx(2.5)
        loop = (await client.call_tool("read_analog_inputs", {"channels": [2]})).structured_content
        assert loop["readings"][0]["voltage_v"] == pytest.approx(2.5, abs=0.01)

        path = tmp_path / "stream.csv"
        st = (
            await client.call_tool(
                "stream_analog",
                {"channels": [0, 1], "scan_rate_hz": 2000, "duration_s": 0.2, "max_points": 50, "save_path": str(path)},
            )
        ).structured_content
        assert st["scans"] == 400 and st["downsample_factor"] == 16 and st["aborted"] is False
        assert len(st["waveforms_v"]["AIN0"]) == 50 and len(st["time_s"]) == 50
        assert st["stats"][0]["peak_to_peak_v"] == pytest.approx(4.0, abs=0.4)
        rows = list(csv.reader(path.open()))
        assert rows[0] == ["time_s", "AIN0_v", "AIN1_v"] and len(rows) == 401
        assert math.isclose(float(rows[2][0]), 1 / st["scan_rate_hz"])

        tc = (await client.call_tool("read_thermocouple", {"channel": 0, "thermocouple_type": "K"})).structured_content
        assert tc["valid"] is True and tc["temperature_c"] == pytest.approx(37, abs=0.5)

        temp = (await client.call_tool("read_device_temperature", {})).structured_content
        assert 20 < temp["device_temperature_c"] < 35

        out = (await client.call_tool("set_digital_output", {"line": "FIO2", "level": "high"})).structured_content
        assert out == {"line": "FIO2", "level": "high", "timestamp": out["timestamp"]}
        dio = (await client.call_tool("read_digital_inputs", {"lines": ["FIO2", "FIO3"]})).structured_content
        assert dio["lines"][0]["direction"] == "output" and dio["lines"][0]["driven_by_server"] is True
        assert dio["lines"][1]["level"] == "high" and dio["lines"][1]["direction"] == "input"

        safe = (await client.call_tool("set_outputs_safe", {})).structured_content
        assert "FIO2 set to input" in safe["actions"] and "EIO0 set to input" in safe["actions"]

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any("DAC0=2.5" in entry["data"] for entry in log)


async def test_simulated_t8_via_option():
    async with simulated_client(server, options={"sim_model": "T8"}) as client:
        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["model"] == "T8" and dev["dac_range_v"] == [0.0, 10.0]
        ain = (await client.call_tool("read_analog_inputs", {"channels": [0, 1, 2]})).structured_content
        assert ain["simultaneous"] is True


async def test_read_only_hides_output_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"read_analog_inputs", "stream_analog", "read_digital_inputs", "read_thermocouple"} <= names
        assert "write_dac" not in names
        assert "set_digital_output" not in names
        assert "set_outputs_safe" in names  # safety tools stay available
        assert "reconnect" in names


async def test_dac_limit_refuses_before_sending():
    async with simulated_client(server, limits={"max_dac_voltage_v": 1.0}) as client:
        with pytest.raises(Exception, match="max_dac_voltage_v"):
            await client.call_tool("write_dac", {"dac": 0, "voltage_v": 2.0})
        assert server.driver.read_dacs() == [0.0, 0.0]  # nothing was sent


async def test_stream_limits():
    async with simulated_client(server, limits={"max_stream_duration_s": 0.5, "max_scan_rate_hz": 500}) as client:
        with pytest.raises(Exception, match="max_stream_duration_s"):
            await client.call_tool("stream_analog", {"channels": [0], "scan_rate_hz": 100, "duration_s": 2})
        with pytest.raises(Exception, match="max_scan_rate_hz"):
            await client.call_tool("stream_analog", {"channels": [0], "scan_rate_hz": 1000, "duration_s": 0.1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_dac_voltage_v": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_dac_voltage_v", 3)


def test_safe_state_interrupts_a_running_stream():
    d = make_driver("T7")
    d.write_dac(0, 3.0)
    result = {}
    worker = threading.Thread(target=lambda: result.update(s=d.stream_ain([0], 1000, 30_000)))
    worker.start()
    time.sleep(0.4)
    t0 = time.monotonic()
    actions = d.safe_state()  # must not wait ~30 s for the stream to finish
    assert time.monotonic() - t0 < 2.0
    worker.join(5)
    assert not worker.is_alive()
    s = result["s"]
    assert s.aborted and 0 < len(s.data[0]) < 30_000
    assert "DAC0 and DAC1 set to 0 V" in actions and d.read_dacs() == [0.0, 0.0]
    assert d.ljm.device.stream is None  # the stream was stopped
    # the next stream is not affected by the earlier abort
    assert not d.stream_ain([0], 1000, 100).aborted


def test_downsample_keeps_spikes_and_bounds_length():
    n = 10_000
    times = [i * 1e-3 for i in range(n)]
    col = [0.0] * n
    col[4321] = 5.0  # a one-sample spike that striding by 40 would miss
    col[777] = -3.0
    col[900] = math.nan
    t_out, (wave,), factor = _downsample(times, [col], 500)
    assert len(t_out) == len(wave) <= 500 and factor == 40
    assert max(wave) == 5.0 and min(wave) == -3.0
    assert t_out == sorted(t_out) and t_out[0] == 0.0 and t_out[-1] == times[-1]
    short_t, (short,), f1 = _downsample(times[:5], [[1.0, math.nan, 2.0, 3.0, 4.0]], 500)
    assert f1 == 1 and short == [1.0, None, 2.0, 3.0, 4.0]


async def test_low_scan_rate_cannot_exceed_the_duration_limit():
    # At least two scans are taken: 2 scans at 1 Hz last 2 s, although duration_s is 1.
    async with simulated_client(server, limits={"max_stream_duration_s": 1.5}) as client:
        with pytest.raises(Exception, match="max_stream_duration_s"):
            await client.call_tool("stream_analog", {"channels": [0], "scan_rate_hz": 1, "duration_s": 1})
        # Below 1 scan/s one eStreamRead would block set_outputs_safe for longer than a second.
        with pytest.raises(Exception, match="greater than or equal to 1"):
            await client.call_tool("stream_analog", {"channels": [0], "scan_rate_hz": 0.01, "duration_s": 1})


async def test_stream_save_path_refuses_existing_file_before_streaming(tmp_path):
    existing = tmp_path / "old.csv"
    existing.write_text("keep me", encoding="utf-8")
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("stream_analog", {"channels": [0], "duration_s": 0.1, "save_path": str(existing)})
        with pytest.raises(Exception, match=r"\.csv"):
            await client.call_tool("stream_analog", {"channels": [0], "duration_s": 0.1, "save_path": str(tmp_path / "a.txt")})
        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert not any("eStreamStart" in e["data"] for e in log)
    assert existing.read_text(encoding="utf-8") == "keep me"


async def test_invalid_safe_dio_is_refused_at_connect_and_releases_the_device(monkeypatch):
    created: list[SimulatedLJM] = []

    def factory(model: str) -> SimulatedLJM:
        created.append(SimulatedLJM(model=model))
        return created[-1]

    monkeypatch.setattr("labmcp_labjack.server.SimulatedLJM", factory)
    async with simulated_client(server, options={"sim_model": "T4", "safe_dio": "FIO0"}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is False and "safe_dio" in info["error"] and "DIO0" in info["error"]
    assert created and all(ljm._handles == {} for ljm in created)  # the handle was closed again


def test_failed_initialisation_releases_the_handle():
    ljm = SimulatedLJM(model="T7")

    def fail(*args):
        raise ljm.LJMError(1239, errorString="LJME_RECONNECT_FAILED")

    ljm.eReadNames = fail  # HARDWARE_INSTALLED cannot be read
    with pytest.raises(InstrumentConnectionError):
        open_device(ljm, "ANY", "ANY", "ANY")
    assert ljm._handles == {}
