import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.scpi import SCPISimulator
from labmcp.testing import simulated_client, tool_names
from labmcp_bench_psu.driver import AimTTiPSU, RigolPSU, SiglentPSU, open_power_supply, parse_idn
from labmcp_bench_psu.server import server
from labmcp_bench_psu.simulator import make_simulator


def make_psu(dialect: str = "rigol", model: str | None = None):
    sim = make_simulator(dialect, model)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n", timeout=0.5)
    psu = open_power_supply(t, write_delay_s=0)
    psu.clear_error_state()
    return psu, sim


# ---------------------------------------------------------------- Rigol


def test_rigol_identify_and_channels():
    psu, _ = make_psu("rigol", "DP832")
    assert isinstance(psu, RigolPSU)
    info = psu.identify()
    assert info["manufacturer"] == "RIGOL TECHNOLOGIES" and info["model"] == "DP832"
    assert [c.number for c in psu.channels] == [1, 2, 3]
    assert psu.channel(3).max_voltage_v == 5.3


def test_rigol_cv_cc_crossover_on_resistive_load():
    psu, _ = make_psu("rigol")
    psu.set_voltage(1, 5.0)  # 10 ohm load on CH1
    psu.set_current(1, 1.0)
    psu.set_output(1, True)
    v, i = psu.measure(1)
    assert psu.mode(1) == "CV"
    assert v == pytest.approx(5.0, abs=0.01) and i == pytest.approx(0.5, abs=0.01)
    psu.set_current(1, 0.1)
    v, i = psu.measure(1)
    assert psu.mode(1) == "CC"
    assert i == pytest.approx(0.1, abs=0.005) and v == pytest.approx(1.0, abs=0.05)


def test_rigol_ovp_trip_and_clear():
    psu, _ = make_psu("rigol")
    psu.set_ovp(1, 4.0, True)
    psu.set_voltage(1, 5.0)
    psu.set_output(1, True)
    p = psu.protection(1)
    assert p.ovp_enabled and p.ovp_tripped and p.ovp_v == pytest.approx(4.0)
    assert psu.output_state(1) is False
    psu.clear_trips(1)
    assert psu.protection(1).ovp_tripped is False
    assert psu.output_state(1) is False  # clearing does not re-enable the output


def test_rigol_error_reply_raises():
    psu, _ = make_psu("rigol")
    with pytest.raises(InstrumentProtocolError, match="Illegal parameter"):
        psu.set_voltage(1, 40)  # above the 32 V settable range
    with pytest.raises(InstrumentProtocolError, match="Undefined header"):
        psu._set(":BOGUS 1", "bogus")


def test_rigol_single_channel_dp711():
    psu, _ = make_psu("rigol", "DP711")
    assert [c.number for c in psu.channels] == [1]
    psu.set_voltage(1, 12)
    assert psu.setpoints(1)[0] == pytest.approx(12)


# ---------------------------------------------------------------- Siglent


def test_siglent_status_word_and_fixed_ch3():
    psu, _ = make_psu("siglent", "SPD3303X")
    assert isinstance(psu, SiglentPSU)
    psu.set_voltage(2, 12)
    psu.set_current(2, 0.05)  # 100 ohm load wants 0.12 A -> CC
    psu.set_output(2, True)
    assert psu.output_state(2) is True and psu.output_state(1) is False
    assert psu.mode(2) == "CC"
    assert psu.errors() == []
    with pytest.raises(InstrumentProtocolError, match="fixed output"):
        psu.set_voltage(3, 3.3)
    assert psu.output_state(3) is None
    assert not psu.has_ovp


def test_siglent_spd1000x_protection_levels():
    psu, _ = make_psu("siglent", "SPD1305X")
    assert psu.has_ovp and psu.has_ocp
    psu.set_ovp(1, 12.0, True)
    psu.set_ocp(1, 2.0, None)
    p = psu.protection(1)
    assert p.ovp_v == pytest.approx(12.0) and p.ocp_a == pytest.approx(2.0)
    with pytest.raises(InstrumentProtocolError, match="cannot be switched off"):
        psu.set_ovp(1, None, False)


# ---------------------------------------------------------------- Aim-TTi


def test_tti_replies_with_headers_and_units():
    psu, _ = make_psu("tti", "CPX400DP")
    assert isinstance(psu, AimTTiPSU)
    assert psu.idn["manufacturer"] == "THURLBY THANDAR"
    psu.set_voltage(2, 24.0)
    psu.set_current(2, 0.5)
    assert psu.setpoints(2) == (pytest.approx(24.0), pytest.approx(0.5))  # "V2 24.000"
    psu.set_output(2, True)
    v, i = psu.measure(2)  # "24.000V" / "0.240A"
    assert v == pytest.approx(24.0, abs=0.01) and i == pytest.approx(0.24, abs=0.01)


def test_tti_range_error_and_ocp_trip():
    psu, sim = make_psu("tti", "CPX400DP")
    with pytest.raises(InstrumentProtocolError, match="EER 100"):
        psu.set_voltage(1, 80)
    psu.set_ocp(1, 0.2, True)
    psu.set_voltage(1, 5)
    psu.set_current(1, 1)
    psu.set_output(1, True)  # 10 ohm -> 0.5 A > OCP 0.2 A
    p = psu.protection(1)
    assert p.ocp_tripped is True and p.ocp_a == pytest.approx(0.2)
    assert psu.output_state(1) is False
    assert "TRIPRST" in psu.clear_trips(1)


def test_tti_all_off_uses_opall():
    psu, sim = make_psu("tti", "MX100TP")
    for ch in (1, 2, 3):
        psu.set_output(ch, True)
    assert psu.all_off() == []
    assert all(not o.on for o in sim.outputs.values())


# ---------------------------------------------------------------- detection


class _IDN(SCPISimulator):
    def __init__(self, idn: str) -> None:
        super().__init__()
        self.idn = idn


@pytest.mark.parametrize(
    ("idn", "match"),
    [
        ("Keysight Technologies,E36312A,MY1234,2.1.0", "official Keysight MCP"),
        ("Rohde&Schwarz,NGE103B,5601.3800k03/100123,02.203", "RsInstrument"),
        ("ACME,PSU-9000,1,1.0", "Unsupported power supply"),
    ],
)
def test_unsupported_vendors_are_refused(idn, match):
    t = SimulatedTransport(_IDN(idn), read_termination="\n", write_termination="\n", timeout=0.5)
    with pytest.raises(InstrumentProtocolError, match=match):
        open_power_supply(t)


def test_parse_idn_with_spaces():
    idn = parse_idn("Siglent Technologies, SPD3303X, SPD00001130025, 1.01.01.01.02,V3.0")
    assert idn["model"] == "SPD3303X" and idn["firmware"] == "1.01.01.01.02,V3.0"


# ---------------------------------------------------------------- MCP


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "DP832"

        outputs = (await client.call_tool("get_outputs", {})).structured_content["result"]
        assert [o["channel"] for o in outputs] == [1, 2, 3]
        assert all(o["output_on"] is False for o in outputs)

        await client.call_tool("set_voltage", {"channel": 1, "voltage_v": 5.0})
        status = (await client.call_tool("set_current_limit", {"channel": 1, "current_a": 0.2})).structured_content
        assert status["set_voltage_v"] == pytest.approx(5.0) and status["set_current_a"] == pytest.approx(0.2)

        on = (await client.call_tool("output_on", {"channel": 1})).structured_content
        assert on["output_on"] is True and on["mode"] == "CC"  # 10 ohm load needs 0.5 A
        assert on["measured_current_a"] == pytest.approx(0.2, abs=0.01)

        prot = (await client.call_tool("set_protection", {"channel": 2, "ovp_v": 13.0, "ocp_a": 1.0})).structured_content
        assert prot["ovp_v"] == pytest.approx(13.0) and prot["ovp_enabled"] is True

        off = (await client.call_tool("output_off", {"channel": 1})).structured_content
        assert off["output_on"] is False

        await client.call_tool("output_on", {"channel": 2})
        result = (await client.call_tool("all_outputs_off", {})).structured_content
        assert result["all_off_confirmed"] is True

        assert (await client.call_tool("get_errors", {})).structured_content["result"] == []

        log = (await client.call_tool("get_command_log", {"limit": 500})).data
        assert any(entry["data"] == ":OUTP CH1,ON" for entry in log)


async def test_mcp_other_dialects():
    async with simulated_client(server, options={"dialect": "tti", "sim_model": "CPX400DP"}) as client:
        await client.call_tool("set_voltage", {"channel": 2, "voltage_v": 12})
        await client.call_tool("set_current_limit", {"channel": 2, "current_a": 1})
        on = (await client.call_tool("output_on", {"channel": 2})).structured_content
        assert on["mode"] == "CV" and on["mode_source"] == "inferred"
        await client.call_tool("all_outputs_off", {})
    async with simulated_client(server, options={"dialect": "siglent"}) as client:
        with pytest.raises(Exception, match="no remotely programmable OVP"):
            await client.call_tool("set_protection", {"channel": 1, "ovp_v": 10})
    server.configure(options={})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_outputs", "get_errors"} <= names
        for hidden in ("set_voltage", "set_current_limit", "output_on", "set_protection", "clear_protection_trip"):
            assert hidden not in names
        assert {"output_off", "all_outputs_off", "reconnect"} <= names


async def test_voltage_limit_checked_even_with_output_off():
    async with simulated_client(server, limits={"max_voltage_v": 12}) as client:
        with pytest.raises(Exception, match="max_voltage_v"):
            await client.call_tool("set_voltage", {"channel": 1, "voltage_v": 15})
        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert not any("VOLT 15" in entry["data"] for entry in log)


async def test_current_limit():
    async with simulated_client(server, limits={"max_current_a": 0.5}) as client:
        with pytest.raises(Exception, match="max_current_a"):
            await client.call_tool("set_current_limit", {"channel": 1, "current_a": 1.0})


async def test_output_on_refused_if_front_panel_setpoint_exceeds_limit():
    async with simulated_client(server, limits={"max_voltage_v": 10}) as client:
        await client.call_tool("get_connection_info", {})
        server.driver.set_voltage(1, 24.0)  # as if someone turned the knob on the front panel
        with pytest.raises(Exception, match="max_voltage_v"):
            await client.call_tool("output_on", {"channel": 1})
        outputs = (await client.call_tool("get_outputs", {"channel": 1})).structured_content["result"]
        assert outputs[0]["output_on"] is False


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_voltage_v": 5})
    with pytest.raises(SafetyLimitError):
        server.check("max_voltage_v", 6)
    server.disconnect()
