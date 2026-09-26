import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_new_era.driver import NewEraPump, choose_rate_units, find_syringe_preset, format_float
from labmcp_new_era.server import server
from labmcp_new_era.simulator import NewEraSimulator


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_pump(
    address: int | None = None, sim_address: int = 0
) -> tuple[NewEraPump, NewEraSimulator, FakeClock]:
    clock = FakeClock()
    sim = NewEraSimulator(address=sim_address, clock=clock)
    t = SimulatedTransport(sim, read_termination="\x03", write_termination="\r")
    return NewEraPump(t, address=address), sim, clock


# ------------------------------------------------------------------ number formatting


def test_format_float_follows_4_digit_rule():
    assert format_float(26.59) == "26.59"
    assert format_float(0.5) == "0.500"
    assert format_float(1699) == "1699"
    assert format_float(14.43) == "14.43"
    with pytest.raises(ValueError):
        format_float(12345)
    with pytest.raises(ValueError):
        format_float(0.0001)


def test_choose_rate_units_picks_most_precise():
    assert choose_rate_units(0.005)[:2] == ("5.000", "UM")
    assert choose_rate_units(1.5)[:2] == ("1.500", "MM")
    text, units, actual = choose_rate_units(0.0123456)  # 12.35 µL/min vs 740.7 µL/h
    assert (text, units) == ("740.7", "UH") and actual == pytest.approx(0.0123456, rel=1e-4)


def test_syringe_preset_lookup():
    assert find_syringe_preset("bd 60 ml") == ("BD 60 mL", 26.59)
    with pytest.raises(ValueError, match="list_syringe_presets"):
        find_syringe_preset("Acme 3 mL")


# ------------------------------------------------------------------ driver vs simulator


def test_identify_acknowledges_power_on_reset_alarm():
    pump, _, _ = make_pump()
    info = pump.identify()
    assert info["model"] == "NE-1000"
    assert info["firmware"] == "3.923"
    assert pump.reset_alarm_seen


def test_unknown_command_raises_clear_error():
    pump, _, _ = make_pump()
    pump.status()  # acknowledge the reset alarm
    with pytest.raises(InstrumentProtocolError, match="not recognised"):
        pump.command("XYZ")


def test_infuse_progresses_over_time_and_stops_at_volume():
    pump, sim, clock = make_pump()
    pump.set_diameter(14.43)  # BD 10 mL -> pump works in mL
    plan = pump.start_pumping("INF", 0.5, 1.0)
    assert plan.volume_text == "0.500ML"
    assert plan.rate_text == "1.000MM"
    assert sim.phases[2]["fun"] == "STP"
    clock.now = 15.0
    assert pump.status().prompt == "I"
    inf, wdr, units = pump.dispensed()
    assert units == "ML" and inf == pytest.approx(0.25, abs=0.002) and wdr == 0
    clock.now = 120.0
    assert pump.status().prompt == "S"
    assert pump.dispensed()[0] == pytest.approx(0.5, abs=0.001)


def test_withdraw_small_syringe_uses_microlitres():
    pump, _, clock = make_pump()
    pump.set_diameter(4.699)  # BD 1 mL -> pump works in µL
    plan = pump.start_pumping("WDR", 0.2, 0.1)
    assert plan.volume_text == "200.0UL"
    assert plan.rate_text == "100.0UM"
    clock.now = 60.0
    assert pump.status().prompt == "W"
    clock.now = 200.0
    inf, wdr, units = pump.dispensed()
    assert units == "UL" and wdr == pytest.approx(200.0, abs=0.5)


def test_rate_out_of_range_is_explained():
    pump, _, _ = make_pump()
    pump.set_diameter(4.699)
    with pytest.raises(InstrumentProtocolError, match="out of range for a 4.699 mm syringe"):
        pump.start_pumping("INF", 0.5, 50.0)


def test_refuses_to_start_while_running_and_stop_cancels_pause():
    pump, _, clock = make_pump()
    pump.set_diameter(14.43)
    pump.start_pumping("INF", 1.0, 1.0)
    clock.now = 5.0
    with pytest.raises(InstrumentProtocolError, match="stop_pump"):
        pump.start_pumping("INF", 1.0, 1.0)
    st = pump.stop()
    assert st.prompt == "S"  # not 'P': the pause was cancelled
    clock.now = 60.0
    assert pump.dispensed()[0] == pytest.approx(5 / 60, abs=0.002)


def test_stall_alarm_is_reported():
    pump, sim, clock = make_pump()
    pump.set_diameter(14.43)
    sim.stall_after_ml = 0.1
    pump.start_pumping("INF", 1.0, 1.0)
    clock.now = 30.0
    with pytest.raises(InstrumentProtocolError, match="stalled"):
        pump.command("DIS")
    assert pump.status().prompt == "S"


def test_network_address_prefix():
    pump, _, _ = make_pump(address=7, sim_address=7)
    assert pump.identify()["network_address"] == "7"
    wrong, _, _ = make_pump(address=3, sim_address=7)
    with pytest.raises(InstrumentTimeout):
        wrong.status()


# ------------------------------------------------------------------ MCP round trip


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True and info["simulated"] is True
        assert info["instrument"]["model"] == "NE-1000"

        presets = (await client.call_tool("list_syringe_presets", {"contains": "BD"})).structured_content
        assert any(p["name"] == "BD 10 mL" for p in presets["result"])

        st = (await client.call_tool("set_syringe", {"preset": "BD 10 mL"})).structured_content
        assert st["diameter_mm"] == pytest.approx(14.43)

        started = (
            await client.call_tool("infuse", {"volume_ml": 0.2, "rate_ml_min": 2.0})
        ).structured_content
        assert started["state"] == "infusing"
        assert started["estimated_duration_s"] == pytest.approx(6.0)

        status = (await client.call_tool("get_status", {})).structured_content
        assert status["running"] is True and status["direction"] == "INF"

        stopped = (await client.call_tool("stop_pump", {})).structured_content
        assert stopped["state"] == "stopped"

        vol = (await client.call_tool("get_dispensed_volume", {})).structured_content
        assert 0 <= vol["infused_ml"] < 0.2

        cleared = (await client.call_tool("clear_dispensed_volume", {})).structured_content
        assert cleared["infused_ml"] == 0

        log = (await client.call_tool("get_command_log", {"limit": 100})).data
        assert any(entry["data"] == "RUN" for entry in log)


async def test_pump_address_option_via_mcp():
    async with simulated_client(server, options={"pump_address": "12"}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["instrument"]["network_address"] == "12"


async def test_read_only_hides_control_and_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_status", "get_dispensed_volume", "list_syringe_presets"} <= names
        assert not {"infuse", "withdraw", "set_syringe", "clear_dispensed_volume"} & names
        assert "stop_pump" in names and "reconnect" in names


async def test_rate_limit_refuses():
    async with simulated_client(server, limits={"max_rate_ml_min": 1.0}) as client:
        with pytest.raises(Exception, match="max_rate_ml_min"):
            await client.call_tool("infuse", {"volume_ml": 0.1, "rate_ml_min": 2.0})


async def test_volume_limit_refuses():
    async with simulated_client(server, limits={"max_volume_ml": 0.5}) as client:
        with pytest.raises(Exception, match="max_volume_ml"):
            await client.call_tool("withdraw", {"volume_ml": 1.0, "rate_ml_min": 0.5})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_rate_ml_min": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_rate_ml_min", 5)


# ------------------------------------------------------------------ regressions (bug review)


def test_stop_retries_when_a_stop_packet_is_lost():
    # Regression: a lost reply to the first STP raised out of stop(), leaving the pump running.
    clock = FakeClock()

    class LossySim(NewEraSimulator):
        lost = 0

        def handle(self, command):
            if command.strip() == "STP" and self.lost == 0:
                self.lost += 1
                return None  # packet lost on the wire: not executed, no reply
            return super().handle(command)

    sim = LossySim(clock=clock)
    pump = NewEraPump(SimulatedTransport(sim, read_termination="\x03", write_termination="\r", timeout=0.2))
    pump.set_diameter(14.43)
    pump.start_pumping("INF", 1.0, 1.0)
    clock.now = 5.0
    st = pump.stop()
    assert sim.lost == 1 and st.prompt == "S" and not sim.running


def test_stop_raises_when_the_pump_never_answers():
    sim = NewEraSimulator(address=42)  # the pump ignores every packet sent to address 0
    pump = NewEraPump(SimulatedTransport(sim, read_termination="\x03", write_termination="\r", timeout=0.05))
    with pytest.raises(InstrumentProtocolError, match="Could not confirm that the pump stopped"):
        pump.stop()


def test_unrepresentable_volume_is_refused_before_the_rate_is_sent():
    pump, sim, _ = make_pump()
    pump.set_diameter(4.699)  # BD 1 mL: the pump works in µL, so 12 mL = 12000 µL (> 4 digits)
    before = dict(sim.phases[1])
    with pytest.raises(InstrumentProtocolError, match="works in µL"):
        pump.start_pumping("INF", 12.0, 0.5)
    assert sim.phases[1]["rate"] == before["rate"] and sim.phases[1]["vol"] == before["vol"]
    assert not sim.running
