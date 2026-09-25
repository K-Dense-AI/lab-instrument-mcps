import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_srs_lockin.driver import (
    MODELS,
    SR8X0_SENSITIVITIES_V,
    SR86X_SENSITIVITIES_V,
    SRSLockIn,
    sensitivity_index,
    time_constant_index,
)
from labmcp_srs_lockin.server import server
from labmcp_srs_lockin.simulator import SRSLockInSimulator


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_driver(sim_model: str = "SR830", **kwargs) -> tuple[SRSLockIn, SRSLockInSimulator, FakeClock]:
    clock = FakeClock()
    sim = SRSLockInSimulator(model=sim_model, clock=clock)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    return SRSLockIn(t, **kwargs), sim, clock


# ---------------------------------------------------------------- tables


def test_tables_match_manuals():
    assert len(SR8X0_SENSITIVITIES_V) == 27  # SR830 SENS 0..26
    assert len(SR86X_SENSITIVITIES_V) == 28  # SR860 SCAL 0..27
    assert len(MODELS["SR830"].time_constants_s) == 20  # OFLT 0..19
    assert len(MODELS["SR860"].time_constants_s) == 22  # OFLT 0..21
    assert SR8X0_SENSITIVITIES_V[0] == 2e-9 and SR8X0_SENSITIVITIES_V[26] == 1.0
    assert SR86X_SENSITIVITIES_V[0] == 1.0 and SR86X_SENSITIVITIES_V[27] == 1e-9


def test_sensitivity_rounds_up_never_down():
    sr830, sr860 = MODELS["SR830"], MODELS["SR860"]
    assert SR8X0_SENSITIVITIES_V[sensitivity_index(sr830, 3e-3)] == 5e-3
    assert SR8X0_SENSITIVITIES_V[sensitivity_index(sr830, 1e-3)] == 1e-3
    assert sensitivity_index(sr860, 3e-3) == 7  # 5 mV on the SR860 table
    with pytest.raises(ValueError):
        sensitivity_index(sr830, 2.0)


def test_time_constant_nearest():
    assert MODELS["SR830"].time_constants_s[time_constant_index(MODELS["SR830"], 0.12)] == 0.1
    assert time_constant_index(MODELS["SR830"], 1e-6) == 0  # 10 µs is the SR830 minimum
    assert time_constant_index(MODELS["SR860"], 1e-6) == 0  # 1 µs on the SR860


# ---------------------------------------------------------------- driver


def test_identify_sr830():
    lockin, _, _ = make_driver()
    info = lockin.identify()
    assert info["model"] == "SR830"
    assert info["serial"] == "86025"
    assert info["firmware"] == "1.07"
    assert lockin.family == "sr8x0"


def test_outx_sent_first_for_rs232():
    lockin, sim, _ = make_driver(output_interface="rs232")
    assert sim.outx == 0
    assert lockin.spec.model == "SR830"


def test_snapshot_on_resonance():
    lockin, sim, clock = make_driver()
    lockin.set_frequency_hz(10_000.0)
    lockin.set_sensitivity_index(22)  # 50 mV
    clock.t += 10.0  # many time constants
    snap = lockin.snapshot()
    assert snap.r == pytest.approx(0.01, rel=0.02)  # |H(f0)| = GAIN * Q at 1 V drive
    assert snap.reference_frequency_hz == pytest.approx(10_000.0)
    assert snap.theta_deg == pytest.approx(-90.0, abs=2.0)


def test_output_filter_needs_time_to_settle():
    lockin, _, clock = make_driver()
    lockin.set_frequency_hz(10_000.0)
    clock.t += 0.01  # 0.1 time constants after the change
    early = lockin.snapshot().r
    clock.t += 10.0
    late = lockin.snapshot().r
    assert early < 0.5 * late


def test_auto_phase_zeroes_y():
    lockin, _, clock = make_driver()
    lockin.set_frequency_hz(9_000.0)
    lockin.auto_phase()
    clock.t += 10.0
    snap = lockin.snapshot()
    assert abs(snap.y) < 0.01 * abs(snap.x)
    assert snap.x > 0


def test_auto_gain_picks_range():
    lockin, _, clock = make_driver()
    lockin.set_frequency_hz(10_000.0)
    lockin.auto_gain()
    assert lockin.sensitivity_v() == pytest.approx(20e-3)  # smallest range >= 1.25 x 10 mV


def test_out_of_range_parameter_sets_exe_and_raises():
    lockin, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="EXE"):
        lockin.set_frequency_hz(200e3)  # SR830 maximum is 102 kHz


def test_unknown_command_sets_cmd_and_raises():
    lockin, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="illegal command"):
        lockin.write("NOPE 1")


def test_frequency_not_settable_in_external_mode():
    lockin, _, _ = make_driver()
    lockin.set_reference_internal(False)
    assert lockin.reference_source() == "external"
    assert lockin.frequency_hz() == pytest.approx(1234.5)
    with pytest.raises(InstrumentProtocolError, match="EXE"):
        lockin.set_frequency_hz(1000.0)


def test_overload_is_reported():
    lockin, _, clock = make_driver()
    lockin.set_frequency_hz(10_000.0)
    lockin.set_sensitivity_index(17)  # 1 mV, signal is 10 mV
    clock.t += 10.0
    lockin.snapshot()
    assert any("output overload" in o for o in lockin.overloads())
    assert lockin.overloads() == []  # latched bits clear on read


def test_amplitude_minimum_sr830():
    lockin, sim, _ = make_driver()
    out = lockin.set_amplitude_minimum()
    assert out["amplitude_v"] == pytest.approx(0.004)
    assert lockin.abort.is_set()


def test_sr860_dialect():
    lockin, sim, clock = make_driver("SR860")
    assert lockin.family == "sr86x"
    assert lockin.identify()["serial"] == "003456"
    assert lockin.reference_source() == "internal"  # RSRC 0
    lockin.set_amplitude_v(0.5)
    lockin.set_frequency_hz(10_000.0)
    lockin.set_sensitivity_index(sensitivity_index(lockin.spec, 0.01))
    clock.t += 10.0
    snap = lockin.snapshot()
    assert snap.r == pytest.approx(0.005, rel=0.02)
    lockin.set_input_configuration("I_100MOhm")
    assert (sim.ivmd, sim.icur) == (1, 1)
    assert lockin.input_configuration() == "I_100MOhm"
    lockin.set_input_range_v(0.1)
    assert lockin.input_range_v() == 0.1
    out = lockin.set_amplitude_minimum()
    assert out == {"amplitude_v": pytest.approx(1e-9), "dc_level_v": 0.0}


def test_sr860_rejects_sr830_only_commands():
    lockin, _, _ = make_driver("SR860")
    with pytest.raises(InstrumentProtocolError, match="no dynamic-reserve"):
        lockin.set_reserve("normal")
    with pytest.raises(InstrumentProtocolError, match="illegal command"):
        lockin.write("RMOD 1")


def test_model_mismatch_is_refused():
    with pytest.raises(InstrumentProtocolError, match="reports SR830"):
        make_driver("SR830", model="sr860")


# ---------------------------------------------------------------- MCP


def sim_client(**settings):
    """simulated_client with the default (SR830) simulator unless options are given."""
    settings.setdefault("options", {})
    return simulated_client(server, **settings)


async def test_tools_via_mcp():
    async with sim_client() as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True
        assert info["instrument"]["model"] == "SR830"

        settings = (await client.call_tool("get_settings", {})).structured_content
        assert settings["reference_source"] == "internal"
        assert settings["sensitivity_unit"] == "V"

        s = (
            await client.call_tool("set_reference", {"frequency_hz": 9500, "harmonic": 1, "phase_deg": 10})
        ).structured_content
        assert s["reference_frequency_hz"] == pytest.approx(9500)
        assert s["phase_deg"] == pytest.approx(10)

        s = (await client.call_tool("set_amplitude", {"amplitude_v": 0.5})).structured_content
        assert s["sine_amplitude_v"] == pytest.approx(0.5)

        s = (await client.call_tool("set_sensitivity", {"full_scale": 0.03})).structured_content
        assert s["sensitivity"] == pytest.approx(0.05)

        s = (
            await client.call_tool("set_time_constant", {"time_constant_s": 0.001, "filter_slope_db_oct": 6})
        ).structured_content
        assert s["time_constant_s"] == pytest.approx(0.001)
        assert s["settle_time_s"] == pytest.approx(0.005)

        reading = (await client.call_tool("read_outputs", {"include_aux_inputs": True})).structured_content
        assert reading["unit"] == "V"
        assert len(reading["aux_inputs_v"]) == 4

        sweep = (
            await client.call_tool("frequency_sweep", {"start_hz": 9000, "stop_hz": 11000, "points": 5})
        ).structured_content
        assert sweep["count"] == 5
        assert sweep["completed"] is True
        assert sweep["peak_frequency_hz"] == pytest.approx(10_000)

        s = (await client.call_tool("auto_phase", {})).structured_content
        assert "phase_deg" in s

        s = (
            await client.call_tool("set_input", {"configuration": "A-B", "coupling": "DC"})
        ).structured_content
        assert s["input_configuration"] == "A-B" and s["coupling"] == "DC"

        off = (await client.call_tool("set_amplitude_minimum", {})).structured_content
        assert off["amplitude_v"] == pytest.approx(0.004)


async def test_sweep_saves_csv(tmp_path):
    async with sim_client() as client:
        await client.call_tool("set_time_constant", {"time_constant_s": 0.0001, "filter_slope_db_oct": 6})
        out = tmp_path / "sweep.csv"
        sweep = (
            await client.call_tool(
                "frequency_sweep",
                {"start_hz": 100, "stop_hz": 100000, "points": 4, "log_spacing": True, "save_path": str(out)},
            )
        ).structured_content
        assert sweep["saved_to"] == str(out)
        assert len(out.read_text().strip().splitlines()) == 5
        freqs = [p["frequency_hz"] for p in sweep["points"]]
        assert freqs[0] == pytest.approx(100) and freqs[-1] == pytest.approx(100000)


async def test_sr860_via_mcp():
    async with sim_client(options={"model": "sr860"}, limits={"max_amplitude_v": 5}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["instrument"]["model"] == "SR860"
        s = (await client.call_tool("set_input", {"input_range_v": 0.1})).structured_content
        assert s["input_range_v"] == 0.1
        assert s["dynamic_reserve"] is None
        with pytest.raises(Exception, match="sine output range"):
            await client.call_tool("set_amplitude", {"amplitude_v": 3.0})  # SR86x maximum is 2 V


async def test_read_only_hides_control_tools():
    async with sim_client(read_only=True) as client:
        names = await tool_names(client)
        assert {"read_outputs", "get_settings"} <= names
        for hidden in ("set_amplitude", "frequency_sweep", "set_reference", "set_sensitivity", "auto_gain"):
            assert hidden not in names
        assert "set_amplitude_minimum" in names  # safety tools stay available


async def test_amplitude_limit():
    async with sim_client(limits={"max_amplitude_v": 0.2}) as client:
        with pytest.raises(Exception, match="max_amplitude_v"):
            await client.call_tool("set_amplitude", {"amplitude_v": 0.5})


async def test_sweep_refuses_if_present_amplitude_exceeds_limit():
    # The SR830 powers up at 1.000 V rms; with a 0.5 V limit the sweep must refuse.
    async with sim_client(limits={"max_amplitude_v": 0.5}) as client:
        with pytest.raises(Exception, match="max_amplitude_v"):
            await client.call_tool("frequency_sweep", {"start_hz": 900, "stop_hz": 1100, "points": 3})


async def test_sweep_duration_limit():
    async with sim_client(limits={"max_sweep_duration_s": 5}) as client:
        # 100 ms time constant, 12 dB/oct -> 0.7 s per point; 51 points ~ 38 s
        with pytest.raises(Exception, match="max_sweep_duration_s"):
            await client.call_tool("frequency_sweep", {"start_hz": 900, "stop_hz": 1100, "points": 51})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_amplitude_v": 0.1})
    with pytest.raises(SafetyLimitError):
        server.check("max_amplitude_v", 0.5)
