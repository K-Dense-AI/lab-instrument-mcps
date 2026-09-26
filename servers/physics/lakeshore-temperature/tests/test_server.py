import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_lakeshore.driver import LakeShoreController, decode_reading_status
from labmcp_lakeshore.server import server
from labmcp_lakeshore.simulator import BATH_K, LakeShoreSimulator


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_driver(model: str = "336") -> tuple[LakeShoreController, LakeShoreSimulator, FakeClock]:
    clock = FakeClock()
    sim = LakeShoreSimulator(model=model, clock=clock)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination="\r\n")
    return LakeShoreController(t, min_interval_s=0), sim, clock


def sim_client(**settings):
    settings.setdefault("options", {})
    return simulated_client(server, **settings)


# ---------------------------------------------------------------- driver


def test_identify():
    ls, _, _ = make_driver()
    info = ls.identify()
    assert info["model"] == "Model 336"
    assert info["serial"] == "LSA3361"
    assert info["option_card_serial"] == "LSO3362"
    assert info["firmware"] == "2.9"


def test_read_inputs_all_types():
    ls, _, _ = make_driver()
    a = ls.read_input("A")
    assert a.sensor_type == "diode" and a.sensor_unit == "V"
    assert a.kelvin == pytest.approx(BATH_K, abs=0.01)
    assert 1.5 < a.sensor_value < 1.6
    b = ls.read_input("B")  # Pt-100 below 30 K -> temperature underrange
    assert b.kelvin is None
    assert "temperature underrange" in b.status
    d = ls.read_input("D")
    assert d.sensor_type == "disabled" and d.kelvin is None


def test_reading_status_decoding():
    assert decode_reading_status(0) == []
    assert decode_reading_status(1 | 128) == ["invalid reading", "sensor units overrange"]


def test_closed_loop_heats_to_setpoint():
    ls, sim, clock = make_driver()
    ls.set_setpoint(1, 50.0)
    ls.set_heater_range(1, 3)
    assert ls.heater_range(1) == 3
    clock.t += 3000.0
    assert ls.kelvin("A") == pytest.approx(50.0, abs=0.05)
    assert 0 < ls.output_percent(1) < 100


def test_ramp_moves_setpoint_gradually():
    ls, sim, clock = make_driver()
    ls.set_heater_range(1, 3)
    ls.set_ramp(1, True, 10.0)
    ls.set_setpoint(1, 104.2)
    assert ls.ramp(1) == (True, 10.0)
    clock.t += 60.0  # 10 K in the first minute
    assert ls.ramping(1)
    assert 8 < ls.kelvin("A") < 20
    clock.t += 1200.0
    assert not ls.ramping(1)


def test_pid_round_trip():
    ls, _, _ = make_driver()
    ls.set_pid(1, 30, 15, 0)
    assert ls.pid(1) == (30.0, 15.0, 0.0)


def test_out_of_range_value_raises_exe():
    ls, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="execution error"):
        ls.write("PID 1,5000,20,0")


def test_unknown_command_raises_cme():
    ls, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="command error"):
        ls.write("NOPE 1")


def test_model_specific_validation():
    ls, _, _ = make_driver("335")
    assert ls.spec.inputs == ("A", "B")
    with pytest.raises(InstrumentProtocolError, match="inputs A, B"):
        ls.read_input("C")
    with pytest.raises(InstrumentProtocolError, match="ranges 0-3"):
        ls.set_heater_range(1, 4)
    ls350, _, _ = make_driver("350")
    ls350.set_heater_range(1, 5)  # the 350 has five ranges
    assert ls350.range_names(1)[5] == "range 5"
    ls336, _, _ = make_driver("336")
    assert ls336.range_names(3) == ("off", "on")  # unpowered analog output
    with pytest.raises(InstrumentProtocolError, match="control loops"):
        ls336.pid(3)  # PID only on outputs 1 and 2 of the 336


def test_all_heaters_off():
    ls, sim, _ = make_driver()
    ls.set_heater_range(1, 3)
    ls.set_heater_range(3, 1)
    result = ls.all_heaters_off()
    assert result == {1: "off", 2: "off", 3: "off", 4: "off"}
    assert all(o.range == 0 for o in sim.outputs.values())
    assert ls.abort.is_set()


def test_non_lakeshore_idn_is_refused():
    class Other(LakeShoreSimulator):
        def _dispatch(self, head, args):
            return "ACME,THING,1,1" if head == "*IDN?" else super()._dispatch(head, args)

    t = SimulatedTransport(Other(), read_termination="\r\n", write_termination="\r\n")
    with pytest.raises(InstrumentProtocolError, match="not a Lake Shore"):
        LakeShoreController(t, min_interval_s=0)


# ---------------------------------------------------------------- MCP


async def test_tools_via_mcp():
    async with sim_client() as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True
        assert info["instrument"]["model"] == "Model 336"

        temps = (await client.call_tool("read_temperatures", {})).structured_content
        assert [r["input"] for r in temps["readings"]] == ["A", "B", "C", "D"]
        assert temps["readings"][0]["temperature_k"] == pytest.approx(BATH_K, abs=0.05)

        status = (await client.call_tool("get_heater_status", {})).structured_content["result"]
        assert len(status) == 4
        assert status[0]["mode"] == "closed_loop_pid" and status[0]["control_input"] == "A"
        assert status[2]["kind"] == "analog"

        s = (await client.call_tool("set_setpoint", {"output": 1, "setpoint_k": 20})).structured_content
        assert s["setpoint_k"] == pytest.approx(20)

        s = (
            await client.call_tool("set_ramp", {"output": 1, "enabled": True, "rate_k_per_min": 5})
        ).structured_content
        assert s["ramp_enabled"] is True and s["ramp_rate_k_per_min"] == 5

        s = (await client.call_tool("set_pid", {"output": 1, "p": 40, "i": 25, "d": 0})).structured_content
        assert (s["pid_p"], s["pid_i"], s["pid_d"]) == (40, 25, 0)

        s = (await client.call_tool("set_heater_range", {"output": 1, "heater_range": 2})).structured_content
        assert s["heater_range_name"] == "medium"

        off = (await client.call_tool("all_heaters_off", {})).structured_content
        assert set(off["outputs"].values()) == {"off"}


async def test_wait_for_stable_at_setpoint():
    async with sim_client() as client:
        result = (
            await client.call_tool(
                "wait_for_stable_temperature",
                {
                    "output": 1,
                    "tolerance_k": 0.05,
                    "stable_for_s": 0.3,
                    "timeout_s": 5,
                    "poll_interval_s": 0.1,
                },
            )
        ).structured_content
        assert result["stable"] is True
        assert result["control_input"] == "A"
        assert result["final_temperature_k"] == pytest.approx(BATH_K, abs=0.05)


async def test_wait_times_out_when_far_from_setpoint():
    async with sim_client() as client:
        await client.call_tool("set_setpoint", {"output": 1, "setpoint_k": 100})  # heater range is off
        result = (
            await client.call_tool(
                "wait_for_stable_temperature",
                {
                    "output": 1,
                    "tolerance_k": 0.1,
                    "stable_for_s": 1,
                    "timeout_s": 0.5,
                    "poll_interval_s": 0.1,
                },
            )
        ).structured_content
        assert result["stable"] is False
        assert "Not stable" in result["message"]


async def test_setpoint_in_celsius_is_converted():
    async with sim_client() as client:
        await client.call_tool("get_connection_info", {})
        server.driver.t.simulator.inputs["A"].units = 2  # preferred units Celsius
        s = (await client.call_tool("set_setpoint", {"output": 1, "setpoint_k": 300})).structured_content
        assert s["setpoint_units"] == "celsius"
        assert s["setpoint"] == pytest.approx(26.85)
        assert s["setpoint_k"] == pytest.approx(300)
        server.driver.t.simulator.inputs["A"].units = 3  # sensor units -> refuse
        with pytest.raises(Exception, match="sensor units"):
            await client.call_tool("set_setpoint", {"output": 1, "setpoint_k": 20})


async def test_sim_models_via_option():
    async with sim_client(options={"sim_model": "350"}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["instrument"]["model"] == "Model 350"
        s = (await client.call_tool("get_heater_status", {"output": 4})).structured_content["result"][0]
        assert s["pid_p"] is not None  # the 350 has PID on all four outputs


async def test_read_only_hides_control_tools():
    async with sim_client(read_only=True) as client:
        names = await tool_names(client)
        assert {"read_temperatures", "get_heater_status", "wait_for_stable_temperature"} <= names
        for hidden in ("set_setpoint", "set_heater_range", "set_ramp", "set_pid"):
            assert hidden not in names
        assert "all_heaters_off" in names


async def test_setpoint_limit():
    async with sim_client(limits={"max_setpoint_k": 100}) as client:
        with pytest.raises(Exception, match="max_setpoint_k"):
            await client.call_tool("set_setpoint", {"output": 1, "setpoint_k": 150})


async def test_heater_range_limit():
    async with sim_client(limits={"max_heater_range": 1}) as client:
        with pytest.raises(Exception, match="max_heater_range"):
            await client.call_tool("set_heater_range", {"output": 1, "heater_range": 2})
        s = (await client.call_tool("set_heater_range", {"output": 1, "heater_range": 1})).structured_content
        assert s["heater_range_name"] == "low"


async def test_wait_limit():
    async with sim_client(limits={"max_wait_s": 60}) as client:
        with pytest.raises(Exception, match="max_wait_s"):
            await client.call_tool("wait_for_stable_temperature", {"output": 1, "timeout_s": 600})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_setpoint_k": 10})
    with pytest.raises(SafetyLimitError):
        server.check("max_setpoint_k", 300)


def test_mirroring_output_has_no_control_input():
    # 336 OUTMODE mode 6 = Mirroring: the 2nd field is the mirrored OUTPUT, not an input letter.
    ls, sim, _ = make_driver("336")
    sim.outputs[3].mode = 6
    sim.outputs[3].control_input = 1  # mirrors output 1
    mode, inp, _ = ls.output_mode(3)
    assert mode == "mirroring"
    assert inp is None
