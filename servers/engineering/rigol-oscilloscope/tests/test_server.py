import asyncio
import csv
import dataclasses
import time

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_rigol_scope.driver import (
    DHO,
    DS1000Z,
    MSO5000,
    Preamble,
    RigolScope,
    channel_count,
    detect_profile,
)
from labmcp_rigol_scope.server import _downsample, server
from labmcp_rigol_scope.simulator import RigolScopeSimulator


def make_scope(model: str = "DS1104Z", **kwargs) -> tuple[RigolScope, RigolScopeSimulator]:
    sim = RigolScopeSimulator(model=model)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    return RigolScope(t, **kwargs), sim


# ------------------------------------------------------------------ identity / profiles


def test_profile_detection_and_channels():
    assert detect_profile("DS1054Z") is DS1000Z
    assert detect_profile("MSO1104Z") is DS1000Z
    assert detect_profile("DS1202Z-E") is DS1000Z
    assert detect_profile("MSO5354") is MSO5000
    assert detect_profile("DHO804") is DHO
    assert detect_profile("DHO4804") is DHO
    assert detect_profile("XYZ123", "dho") is DHO
    with pytest.raises(InstrumentConnectionError, match="profile="):
        detect_profile("DS2202A")
    assert channel_count("DS1202Z-E") == 2
    assert channel_count("DHO1204") == 4
    assert channel_count("MSO5072") == 2


def test_identify():
    scope, _ = make_scope("DS1202Z-E")
    info = scope.identify()
    assert info["manufacturer"] == "RIGOL TECHNOLOGIES"
    assert info["model"] == "DS1202Z-E"
    assert info["family"] == "DS1000Z" and info["analog_channels"] == "2"
    with pytest.raises(InstrumentProtocolError, match="channels 1-2"):
        scope.channel_settings(3)


def test_non_rigol_instrument_is_refused():
    sim = RigolScopeSimulator()
    sim.idn = "KEYSIGHT TECHNOLOGIES,DSOX1204G,CN000,1.0"
    with pytest.raises(InstrumentConnectionError, match="not a Rigol"):
        RigolScope(SimulatedTransport(sim, read_termination="\n", write_termination="\n"))


# ------------------------------------------------------------------ settings


def test_read_settings():
    scope, _ = make_scope()
    ch1 = scope.channel_settings(1)
    assert ch1 == {"channel": 1, "enabled": True, "scale_v_per_div": 1.0, "offset_v": 0.0, "coupling": "DC",
                   "probe_ratio": 10.0, "bandwidth_limit": "OFF"}
    assert scope.timebase() == {"scale_s_per_div": 200e-6, "offset_s": 0.0}
    trig = scope.trigger()
    assert trig["type"] == "EDGE" and trig["source"] == "CHAN1" and trig["level_v"] == 1.5
    acq = scope.acquisition()
    assert acq["memory_depth"] == 6000  # two channels on
    assert acq["sample_rate_sa_s"] == pytest.approx(6000 / (12 * 200e-6))


def test_set_channel_applies_and_snaps():
    scope, sim = make_scope()
    ch = scope.set_channel(2, scale_v_per_div=0.3, offset_v=0.1, coupling="AC", bandwidth_limit_20mhz=True)
    assert ch["scale_v_per_div"] == 0.2  # 1-2-5 steps
    assert ch["coupling"] == "AC" and ch["bandwidth_limit"] == "20M" and ch["offset_v"] == pytest.approx(0.1)
    ch = scope.set_channel(1, probe_ratio=1)
    assert ch["probe_ratio"] == 1.0 and ch["scale_v_per_div"] == pytest.approx(0.1)
    with pytest.raises(InstrumentProtocolError, match="Probe ratio 3"):
        scope.set_channel(1, probe_ratio=3)


def test_error_queue_is_reported():
    scope, sim = make_scope()
    with pytest.raises(InstrumentProtocolError, match='-224,"Illegal parameter value"'):
        scope.set_channel(1, scale_v_per_div=5000)
    with pytest.raises(InstrumentProtocolError, match='-113,"Undefined header"'):
        scope.send(":NOT:A:COMMand 1")
    assert scope.errors() == []  # the queue was drained


def test_autoscale_uses_family_command():
    scope, sim = make_scope("DS1104Z")
    scope.set_channel(1, scale_v_per_div=5)
    scope.autoscale()
    assert sim.ch[1]["scale"] == 0.5 and sim.ch[1]["offset"] == pytest.approx(-1.5)
    dho, dsim = make_scope("DHO804")
    dho.autoscale()  # :AUToset on the DHO
    assert dsim.ch[1]["scale"] == 0.5
    # the DS1000Z does not know :AUToset
    with pytest.raises(InstrumentProtocolError, match="-113"):
        scope.send(":AUToset")


def test_timebase_and_trigger():
    scope, _ = make_scope()
    tb = scope.set_timebase(scale_s_per_div=1e-4, offset_s=2e-4)
    assert tb == {"scale_s_per_div": 1e-4, "offset_s": 2e-4}
    trig = scope.set_edge_trigger(source_channel=2, level_v=0.2, slope="falling", sweep="normal")
    assert trig["source"] == "CHAN2" and trig["slope"] == "NEG" and trig["sweep"] == "NORM"
    assert trig["status"] == "TD"
    with pytest.raises(InstrumentProtocolError, match="-224"):
        scope.set_edge_trigger(level_v=50)  # outside ±5 div of CH2


def test_single_waits_for_trigger_and_force():
    scope, _ = make_scope()
    assert scope.single(wait_s=2) == "STOP"
    scope.set_edge_trigger(level_v=4.0)  # above the 0-3 V square: never triggers
    assert scope.single(wait_s=0.3) == "WAIT"
    scope.force_trigger()
    assert scope.trigger_status() == "STOP"
    scope.run()
    assert scope.trigger_status() in {"TD", "AUTO", "WAIT"}


# ------------------------------------------------------------------ measurements


def test_measurements():
    scope, _ = make_scope()
    m = scope.measure(1, ["vpp", "vmax", "vmin", "vavg", "frequency", "period", "rise_time", "positive_duty"])
    assert m["vpp"] == pytest.approx(3.0, rel=0.01)
    assert m["vavg"] == pytest.approx(1.5, rel=0.01)
    assert m["frequency"] == pytest.approx(1000, rel=0.01)
    assert m["period"] == pytest.approx(1e-3, rel=0.01)
    assert m["rise_time"] == pytest.approx(2.2e-6, rel=0.05)
    assert m["positive_duty"] == pytest.approx(0.5, rel=0.01)
    sine = scope.measure(2, ["vrms", "frequency"])
    assert sine["vrms"] == pytest.approx(0.707, rel=0.01) and sine["frequency"] == pytest.approx(2000, rel=0.01)
    assert scope.measure(3, ["vpp"]) == {"vpp": None}  # channel off -> 9.9E37
    scope.set_timebase(scale_s_per_div=5e-6)  # 60 µs on screen: less than one period
    assert scope.measure(1, ["frequency"])["frequency"] is None
    with pytest.raises(InstrumentProtocolError, match="Unknown measurement"):
        scope.measure(1, ["bogus"])


# ------------------------------------------------------------------ waveforms


def test_screen_capture_uses_preamble_scaling():
    scope, _ = make_scope()
    pre = scope.prepare_capture(1)
    assert (pre.format, pre.type, pre.points) == (0, 0, 1200)
    assert pre.xincrement == pytest.approx(2e-6) and pre.yreference == 127
    w = scope.read_capture(1, pre)
    assert len(w.volts) == 1200
    assert min(w.volts) == pytest.approx(0.0, abs=0.05) and max(w.volts) == pytest.approx(3.0, abs=0.05)
    assert w.time_s[0] == pytest.approx(-1.2e-3) and w.time_s[-1] == pytest.approx(1.2e-3 - 2e-6)
    assert w.clipped_fraction == 0.0
    scope.set_channel(1, offset_v=0.5)  # offset moves the trace, not the decoded volts
    w2 = scope.read_capture(1, scope.prepare_capture(1))
    assert max(w2.volts) == pytest.approx(3.0, abs=0.05)


def test_clipping_is_detected():
    scope, _ = make_scope()
    scope.set_channel(1, scale_v_per_div=0.2)
    w = scope.read_capture(1, scope.prepare_capture(1))
    assert w.clipped_fraction > 0.3


def test_memory_capture_requires_stop_and_reads_in_batches():
    scope, _ = make_scope()
    with pytest.raises(InstrumentProtocolError, match="stopped"):
        scope.prepare_capture(1, "memory")
    scope.stop()
    scope.profile = dataclasses.replace(scope.profile, memory_batch_points=2500)
    pre = scope.prepare_capture(1, "memory")
    assert pre.type == 2 and pre.points == 6000
    w = scope.read_capture(1, pre, "memory")
    assert len(w.volts) == 6000
    assert w.time_s[1] - w.time_s[0] == pytest.approx(pre.xincrement)
    assert max(w.volts) == pytest.approx(3.0, abs=0.05)


def test_disabled_channel_capture_is_refused():
    scope, _ = make_scope()
    with pytest.raises(InstrumentProtocolError, match="switched off"):
        scope.prepare_capture(3)


def test_preamble_parse_errors():
    with pytest.raises(InstrumentProtocolError, match="10 comma-separated"):
        Preamble.parse("0,0,1200")


@pytest.mark.parametrize(("model", "magic", "fmt"), [("DS1104Z", b"\x89PNG", "png"), ("MSO5074", b"BM", "bmp"), ("DHO804", b"\x89PNG", "png")])
def test_screenshot_formats(model, magic, fmt):
    scope, _ = make_scope(model)
    data, got = scope.screenshot()
    assert got == fmt and data.startswith(magic) and len(data) > 1000


# ------------------------------------------------------------------ MCP


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["family"] == "DS1000Z"

        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["model"] == "DS1104Z" and dev["screen_points"] == 1200 and dev["horizontal_divisions"] == 12

        settings = (await client.call_tool("get_settings", {})).structured_content
        assert len(settings["channels"]) == 4 and settings["timebase"]["window_s"] == pytest.approx(2.4e-3)

        meas = (await client.call_tool("measure", {"channel": 1, "items": ["vpp", "frequency"]})).structured_content
        values = {m["name"]: m for m in meas["measurements"]}
        assert values["vpp"]["value"] == pytest.approx(3.0, rel=0.01) and values["frequency"]["unit"] == "Hz"

        path = tmp_path / "ch1.csv"
        wave = (
            await client.call_tool("capture_waveform", {"channel": 1, "max_points": 100, "save_path": str(path)})
        ).structured_content
        assert wave["points"] == 1200 and wave["downsample_factor"] == 24 and len(wave["volts"]) == 100
        assert wave["stats"]["peak_to_peak_v"] == pytest.approx(3.0, abs=0.1)
        rows = list(csv.reader(path.open()))
        assert rows[0] == ["time_s", "ch1_v"] and len(rows) == 1201

        ch = (await client.call_tool("set_channel", {"channel": 2, "scale_v_per_div": 0.2})).structured_content
        assert ch["scale_v_per_div"] == 0.2
        tb = (await client.call_tool("set_timebase", {"scale_s_per_div": 0.0005})).structured_content
        assert tb["window_s"] == pytest.approx(6e-3)
        trig = (await client.call_tool("set_trigger", {"source_channel": 1, "level_v": 1.0, "slope": "rising"})).structured_content
        assert trig["level_v"] == 1.0

        shot = (await client.call_tool("single", {"wait_s": 2})).structured_content
        assert shot["trigger_status"] == "STOP"
        mem = (await client.call_tool("capture_waveform", {"channel": 1, "mode": "memory"})).structured_content
        assert mem["points"] == 6000 and mem["mode"] == "memory"

        img_path = tmp_path / "screen.png"
        result = await client.call_tool("screenshot", {"save_path": str(img_path)})
        assert result.structured_content["format"] == "png"
        assert img_path.read_bytes().startswith(b"\x89PNG")
        assert any(getattr(c, "type", "") == "image" for c in result.content)

        state = (await client.call_tool("run", {})).structured_content
        assert state["trigger_status"] != "STOP"
        await client.call_tool("autoscale", {})

        log = (await client.call_tool("get_command_log", {"limit": 300})).data
        assert any(e["data"].startswith(":MEASure:ITEM? VPP,CHANnel1") for e in log)


async def test_dho_family_via_option():
    async with simulated_client(server, options={"sim_model": "DHO804"}) as client:
        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["family"] == "DHO" and dev["screen_points"] == 1000
        wave = (await client.call_tool("capture_waveform", {"channel": 2})).structured_content
        assert wave["points"] == 1000
        await client.call_tool("autoscale", {})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_settings", "measure", "capture_waveform", "screenshot", "get_device_info"} <= names
        for control in ("autoscale", "run", "stop", "single", "force_trigger", "set_channel", "set_timebase", "set_trigger"):
            assert control not in names
        assert "reconnect" in names


async def test_memory_points_limit():
    async with simulated_client(server, limits={"max_memory_points": 1000}) as client:
        await client.call_tool("stop", {})
        with pytest.raises(Exception, match="max_memory_points"):
            await client.call_tool("capture_waveform", {"channel": 1, "mode": "memory"})
        # screen captures are not affected
        await client.call_tool("capture_waveform", {"channel": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_memory_points": 10})
    with pytest.raises(SafetyLimitError):
        server.check("max_memory_points", 100)


def test_downsample_keeps_glitches_at_their_times():
    n = 12_000
    times = [i * 1e-6 for i in range(n)]
    volts = [0.0] * n
    volts[6789] = 2.5  # a one-sample glitch
    t_out, v_out, factor = _downsample(times, volts, 500)
    assert len(v_out) == len(t_out) <= 500 and factor == 48
    assert max(v_out) == 2.5 and t_out[v_out.index(2.5)] == times[6789]
    assert t_out == sorted(t_out) and t_out[0] == times[0] and t_out[-1] == times[-1]


def test_memory_capture_reports_clipping():
    scope, _ = make_scope()
    scope.set_channel(1, scale_v_per_div=0.2)
    scope.stop()
    w = scope.read_capture(1, scope.prepare_capture(1, "memory"), "memory")
    assert w.clipped_fraction > 0.3  # was always 0.0 in memory mode


def test_settings_change_during_read_is_refused():
    scope, sim = make_scope()
    pre = scope.prepare_capture(1)
    original = scope.query_block

    def turn_the_knob(*args, **kwargs):
        data = original(*args, **kwargs)
        sim.ch[1]["scale"] = 2.0  # V/div changed on the front panel while the data was in flight
        return data

    scope.query_block = turn_the_knob
    with pytest.raises(InstrumentProtocolError, match="changed while the waveform was being read"):
        scope.read_capture(1, pre)


def test_preamble_with_non_finite_values_is_rejected():
    with pytest.raises(InstrumentProtocolError, match="Non-finite"):
        Preamble.parse("0,0,1200,1,nan,-0.0012,0,0.04,0,127")


async def test_concurrent_captures_do_not_mix_channels():
    async with simulated_client(server) as client:
        await client.call_tool("get_connection_info", {})
        scope = server.driver
        original = scope.prepare_capture

        def slow_prepare(*args, **kwargs):
            pre = original(*args, **kwargs)
            time.sleep(0.1)  # widen the window between selecting the source and reading the data
            return pre

        scope.prepare_capture = slow_prepare
        ch1, ch2 = await asyncio.gather(
            client.call_tool("capture_waveform", {"channel": 1}),
            client.call_tool("capture_waveform", {"channel": 2}),
        )
        assert ch1.structured_content["stats"]["peak_to_peak_v"] == pytest.approx(3.0, abs=0.1)  # CH1 square
        assert ch2.structured_content["stats"]["rms_v"] == pytest.approx(0.707, abs=0.05)  # CH2 sine


async def test_save_paths_are_validated_before_reading(tmp_path):
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match=r"\.png"):
            await client.call_tool("screenshot", {"save_path": str(tmp_path / "screen.jpg")})
        existing = tmp_path / "screen.png"
        existing.write_bytes(b"keep")
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("screenshot", {"save_path": str(existing)})
        existing_csv = tmp_path / "wave.csv"
        existing_csv.write_text("keep", encoding="utf-8")
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("capture_waveform", {"channel": 1, "save_path": str(existing_csv)})
        log = (await client.call_tool("get_command_log", {"limit": 300})).data
        assert not any("DISPlay:DATA?" in e["data"] or "WAVeform" in e["data"] for e in log)
    assert existing.read_bytes() == b"keep" and existing_csv.read_text(encoding="utf-8") == "keep"
