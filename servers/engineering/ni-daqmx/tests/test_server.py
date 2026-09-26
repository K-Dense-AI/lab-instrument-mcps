import csv
import threading
import time

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_ni_daqmx.driver import NIDAQ, expand_channels
from labmcp_ni_daqmx.server import _downsample, server
from labmcp_ni_daqmx.simulator import SimulatedNIDAQmx


def make_driver(**kwargs) -> tuple[NIDAQ, SimulatedNIDAQmx]:
    sim = SimulatedNIDAQmx()
    return NIDAQ(sim, **kwargs), sim


# ------------------------------------------------------------------ driver


def test_expand_channels():
    assert expand_channels("ai0:2", "Dev1") == ["Dev1/ai0", "Dev1/ai1", "Dev1/ai2"]
    assert expand_channels("Dev1/ai3, ai5", "Dev1") == ["Dev1/ai3", "Dev1/ai5"]
    assert expand_channels(["port0/line0:1"], "Dev1") == ["Dev1/port0/line0", "Dev1/port0/line1"]
    assert expand_channels("ai2:0", "Dev1") == ["Dev1/ai2", "Dev1/ai1", "Dev1/ai0"]
    with pytest.raises(InstrumentProtocolError, match="Dev2"):
        expand_channels("Dev2/ai0", "Dev1")
    with pytest.raises(InstrumentProtocolError, match="duplicates"):
        expand_channels("ai0, ai0", "Dev1")


def test_device_selection():
    d, _ = make_driver()
    assert d.name == "Dev1"
    assert d.identify()["model"] == "USB-6341"
    two = SimulatedNIDAQmx(devices=("Dev1", "Dev2"))
    with pytest.raises(InstrumentConnectionError, match="choose one with --address"):
        NIDAQ(two)
    assert NIDAQ(two, "dev2").name == "Dev2"
    with pytest.raises(InstrumentConnectionError, match="not found"):
        NIDAQ(two, "Dev9")


def test_describe_lists_channels_and_ranges():
    d, _ = make_driver()
    info = d.describe()
    assert info["analog_inputs"][0] == "Dev1/ai0" and len(info["analog_inputs"]) == 16
    assert info["analog_outputs"] == ["Dev1/ao0", "Dev1/ao1"]
    assert [-10.0, 10.0] in info["ai_voltage_ranges_v"]
    assert info["ao_voltage_ranges_v"] == [[-10.0, 10.0]]
    assert info["serial_number"] == "1F2E3D4C"


def test_single_point_and_finite_acquisition():
    d, _ = make_driver()
    single = d.read_voltage("ai1")
    assert single.rate_hz is None and single.data[0][0] == pytest.approx(2.5, abs=0.01)
    acq = d.read_voltage("ai0:1", "rse", -5, 5, samples=200, rate_hz=10_000)
    assert acq.channels == ["Dev1/ai0", "Dev1/ai1"]
    assert len(acq.data) == 2 and len(acq.data[0]) == 200
    assert acq.rate_hz == pytest.approx(10_000)
    assert max(acq.data[0]) - min(acq.data[0]) > 0.5  # part of a 50 Hz, 1 V sine


def test_ao_loopback_and_hardware_range():
    d, _ = make_driver()
    d.write_voltage("ao0", 1.25)
    assert d.read_voltage("ai2").data[0][0] == pytest.approx(1.25, abs=0.005)
    with pytest.raises(InstrumentProtocolError, match="outside the analog output range"):
        d.write_voltage("ao1", 12.0)
    with pytest.raises(InstrumentProtocolError, match="not one of the analog outputs"):
        d.write_voltage("ao7", 1.0)


def test_daqmx_error_replies_are_reported():
    d, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match=r"-200077"):
        d.read_voltage("ai0", "pseudo_diff")
    with pytest.raises(InstrumentProtocolError, match=r"-200081.*too high"):
        d.read_voltage("ai0:3", samples=10, rate_hz=400_000)
    with pytest.raises(InstrumentProtocolError, match=r"-200576.*cjc_source='constant'"):
        d.read_thermocouple("ai4", "K", "built_in")
    # every failed task was closed: the device is not left reserved
    assert d.read_voltage("ai1").data[0][0] == pytest.approx(2.5, abs=0.01)


def test_thermocouple_with_constant_cjc():
    d, _ = make_driver()
    acq = d.read_thermocouple("ai4:5", "K", "constant", 24.0, samples=5, rate_hz=10)
    assert acq.data[0][-1] == pytest.approx(37.0, abs=0.5)
    assert acq.data[1][-1] == pytest.approx(24.0, abs=0.5)  # shorted input reads the CJC temperature


def test_digital_lines_and_driven_line_protection():
    d, sim = make_driver()
    states = d.read_lines("port1/line0:1")
    assert [s["high"] for s in states] == [True, False]
    d.write_lines("port0/line0:1", [True, False])
    assert sim.hardware["Dev1"].line_output == {"dev1/port0/line0": True, "dev1/port0/line1": False}
    states = {s["line"]: s for s in d.read_lines("port0/line0:2")}
    assert states["Dev1/port0/line0"]["source"].startswith("last commanded")
    assert states["Dev1/port0/line0"]["high"] is True
    assert states["Dev1/port0/line2"]["source"] == "measured"
    # the driven lines were not turned into inputs by the read
    assert sim.hardware["Dev1"].line_output["dev1/port0/line0"] is True


def test_safe_state():
    d, sim = make_driver(safe_do_lines="port2/line7")
    d.write_voltage("ao0", 3.0)
    d.write_voltage("ao1", -2.0)
    d.write_lines("port0/line3", [True])
    actions = d.safe_state()
    hw = sim.hardware["Dev1"]
    assert hw.ao_value == {"dev1/ao0": 0.0, "dev1/ao1": 0.0}
    assert hw.line_output["dev1/port0/line3"] is False and hw.line_output["dev1/port2/line7"] is False
    assert any(a.startswith("driven low") for a in actions)


# ------------------------------------------------------------------ MCP


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        devices = (await client.call_tool("list_devices", {})).structured_content
        assert devices["connected_device"] == "Dev1" and devices["devices"][0]["product_type"] == "USB-6341"

        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["ai_max_multi_channel_rate_hz"] == 500_000

        one = (await client.call_tool("read_analog", {"channels": "ai1"})).structured_content
        assert one["samples_per_channel"] == 1 and one["stats"][0]["last"] == pytest.approx(2.5, abs=0.01)

        path = tmp_path / "acq.csv"
        acq = (
            await client.call_tool(
                "read_analog",
                {"channels": "ai0:1", "terminal_config": "diff", "min_v": -2, "max_v": 2, "samples": 1000,
                 "rate_hz": 20_000, "max_points": 100, "save_path": str(path)},
            )
        ).structured_content
        assert acq["downsample_factor"] == 20 and len(acq["waveforms"]["Dev1/ai0"]) == 100
        assert len(acq["time_s"]) == 100
        assert acq["stats"][0]["rms"] == pytest.approx(0.707, abs=0.1)
        rows = list(csv.reader(path.open()))
        assert rows[0] == ["time_s", "Dev1/ai0_v", "Dev1/ai1_v"] and len(rows) == 1001

        tc = (
            await client.call_tool("read_thermocouple", {"channels": "ai4", "cjc_source": "constant", "cjc_value_c": 23})
        ).structured_content
        assert tc["unit"] == "c" and tc["stats"][0]["last"] == pytest.approx(37, abs=0.5)

        ao = (await client.call_tool("write_analog", {"channel": "ao0", "voltage_v": 1.5})).structured_content
        assert ao["channel"] == "Dev1/ao0"
        loop = (await client.call_tool("read_analog", {"channels": "ai2"})).structured_content
        assert loop["stats"][0]["last"] == pytest.approx(1.5, abs=0.01)

        do = (await client.call_tool("write_digital_lines", {"lines": "port0/line0:1", "levels": ["high", "low"]})).structured_content
        assert [x["level"] for x in do["lines"]] == ["high", "low"]
        di = (await client.call_tool("read_digital_lines", {"lines": "port1/line0"})).structured_content
        assert di["lines"][0]["level"] == "high" and di["lines"][0]["source"] == "measured"

        safe = (await client.call_tool("set_outputs_safe", {})).structured_content
        assert "Dev1/ao0 set to 0 V" in safe["actions"]

        log = (await client.call_tool("get_command_log", {"limit": 100})).data
        assert any("AO Dev1/ao0 = 1.5 V" in e["data"] for e in log)


async def test_read_only_hides_output_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"list_devices", "read_analog", "read_thermocouple", "read_digital_lines"} <= names
        assert "write_analog" not in names
        assert "write_digital_lines" not in names
        assert "set_outputs_safe" in names
        assert "reconnect" in names


async def test_ao_voltage_limit():
    async with simulated_client(server, limits={"max_ao_voltage_v": 2.0}) as client:
        with pytest.raises(Exception, match="max_ao_voltage_v"):
            await client.call_tool("write_analog", {"channel": "ao0", "voltage_v": -3.0})
        assert server.driver.ao_last == {}  # nothing was sent


async def test_acquisition_limits():
    async with simulated_client(server, limits={"max_samples": 1000, "max_rate_hz": 5000, "max_acquisition_s": 0.1}) as client:
        with pytest.raises(Exception, match="max_samples"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 2000, "rate_hz": 1000})
        with pytest.raises(Exception, match="max_rate_hz"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 100, "rate_hz": 10_000})
        with pytest.raises(Exception, match="max_acquisition_s"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 1000, "rate_hz": 1000})
        with pytest.raises(Exception, match="rate_hz is required"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 10})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_ao_voltage_v": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_ao_voltage_v", 2)


def test_safe_state_does_not_wait_for_a_running_acquisition():
    d, sim = make_driver()
    d.write_voltage("ao0", 3.0)
    d.write_lines("port0/line2", [True])
    result = {}
    worker = threading.Thread(target=lambda: result.update(a=d.read_voltage("ai0", samples=30_000, rate_hz=10_000)))
    worker.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    d.safe_state()  # the 3 s acquisition holds the analog-input lock, not the output lock
    assert time.monotonic() - t0 < 1.0
    hw = sim.hardware["Dev1"]
    assert hw.ao_value["dev1/ao0"] == 0.0 and hw.line_output["dev1/port0/line2"] is False
    worker.join(10)
    assert len(result["a"].data[0]) == 30_000  # the acquisition itself was not disturbed


def test_safe_state_drives_other_lines_low_when_one_fails(monkeypatch):
    d, sim = make_driver()
    d.write_lines("port0/line0:2", [True])
    original = d.write_lines

    def flaky(lines, levels):
        if "Dev1/port0/line1" in lines:
            raise InstrumentProtocolError("line1 is broken")
        return original(lines, levels)

    monkeypatch.setattr(d, "write_lines", flaky)
    with pytest.raises(InstrumentProtocolError, match="line1 is broken"):
        d.safe_state()
    hw = sim.hardware["Dev1"]
    assert hw.line_output["dev1/port0/line0"] is False and hw.line_output["dev1/port0/line2"] is False


def test_line_names_are_canonical_so_safe_state_has_no_case_duplicates():
    d, sim = make_driver(safe_do_lines="port0/line1")
    names = d.write_lines("PORT0/LINE1", [True])
    assert names == ["Dev1/port0/line1"] and list(d.do_last) == ["Dev1/port0/line1"]
    d.safe_state()
    assert sim.hardware["Dev1"].line_output["dev1/port0/line1"] is False


def test_invalid_safe_do_lines_and_huge_ranges_are_refused():
    with pytest.raises(InstrumentConnectionError, match="safe_do_lines"):
        make_driver(safe_do_lines="port7/line0")
    with pytest.raises(InstrumentProtocolError, match="at most 1024"):
        expand_channels("ai0:100000000", "Dev1")


def test_downsample_keeps_spikes():
    n = 10_000
    times = [i * 1e-4 for i in range(n)]
    col = [0.0] * n
    col[5003] = 9.0
    col[17] = -4.0
    t_out, (wave,), factor = _downsample(times, [col], 500)
    assert len(wave) == len(t_out) <= 500 and factor == 40
    assert max(wave) == 9.0 and min(wave) == -4.0 and t_out == sorted(t_out)


async def test_acquisition_longer_than_the_tool_timeout_is_refused():
    async with simulated_client(server, limits={"max_acquisition_s": 1e6, "max_samples": 1e7}) as client:
        with pytest.raises(Exception, match="single tool call"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 1_000_000, "rate_hz": 1000})


async def test_save_path_refuses_existing_file_before_acquiring(tmp_path):
    existing = tmp_path / "old.csv"
    existing.write_text("keep me", encoding="utf-8")
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("read_analog", {"channels": "ai0", "samples": 10, "rate_hz": 100, "save_path": str(existing)})
        log = (await client.call_tool("get_command_log", {"limit": 100})).data
        assert not any(e["data"].startswith("AI voltage") for e in log)
    assert existing.read_text(encoding="utf-8") == "keep me"
