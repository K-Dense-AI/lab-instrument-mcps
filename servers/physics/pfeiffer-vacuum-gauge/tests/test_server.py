import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_pfeiffer_tpg.driver import TPGController, decode_error_word, describe_gauge
from labmcp_pfeiffer_tpg.server import server
from labmcp_pfeiffer_tpg.simulator import TPGSimulator


def make_driver(sim_model: str = "TPG362", **kwargs) -> tuple[TPGController, TPGSimulator]:
    sim = TPGSimulator(model=sim_model)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination="\r")
    return TPGController(t, settle_s=0, **kwargs), sim


def sim_client(**settings):
    settings.setdefault("options", {})
    return simulated_client(server, **settings)


# ---------------------------------------------------------------- driver


def test_identify_tpg362_and_stale_stream_discarded():
    tpg, sim = make_driver()
    info = tpg.identify()
    assert info["model"] == "TPG362"
    assert info["part_number"] == "PTG28290"
    assert info["serial"] == "44990000"
    assert tpg.channels == 2
    assert sim.log[0] == "AYT"  # ETX went first; the power-up line was flushed, not parsed


def test_wire_format_ack_then_enq():
    tpg, sim = make_driver()
    assert sim.handle_bytes(b"PR1\r") == b"\x06\r\n"
    reply = sim.handle_bytes(b"\x05")
    status, value = reply.decode().strip().split(",")
    assert status == "0" and "E-" in value and len(value) == len("1.2300E-05")


def test_pressures_and_units():
    tpg, _ = make_driver()
    p1, p2 = tpg.pressures()
    assert p1.status == "ok" and p1.unit == "hPa"  # TPG 36x factory unit
    assert 1e-7 < p1.mbar < 1e-4
    assert p2.mbar == pytest.approx(2.4e-2, rel=0.05)
    assert tpg.set_unit("Torr") == "Torr"
    p2t = tpg.pressure(2)
    assert p2t.unit == "Torr"
    assert p2t.value == pytest.approx(2.4e-2 / 1.333224, rel=0.05)
    assert p2t.mbar == pytest.approx(2.4e-2, rel=0.05)


def test_nak_reads_and_decodes_error_word():
    tpg, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="NAK.*SYN: syntax error"):
        tpg.query("FOO")
    with pytest.raises(InstrumentProtocolError, match="PAR: inadmissible parameter"):
        tpg.query("UNI,9")
    assert tpg.errors() == []  # the word was cleared when read


def test_error_word_decoding():
    assert decode_error_word("0000") == []
    assert decode_error_word("1001") == [
        "ERROR: controller error (see the front-panel display)",
        "SYN: syntax error",
    ]
    with pytest.raises(InstrumentProtocolError):
        decode_error_word("hello")


def test_gauge_identification():
    assert describe_gauge("TPR/PCR")[1] == "pirani"
    assert describe_gauge("IKR9")[2] is True
    assert describe_gauge("noSENSOR")[1] == "none"
    assert describe_gauge("noSEn")[1] == "none"
    tpg, _ = make_driver("TPG366")
    assert tpg.gauge_ids() == ["PKR", "TPR/PCR", "IKR", "CMR/APR", "noSENSOR", "noSENSOR"]
    assert tpg.sensor_states() == [2, 0, 1, 0, 0, 0]


def test_tpg366_all_channels_and_statuses():
    tpg, _ = make_driver("TPG366")
    ps = tpg.pressures()
    assert len(ps) == 6
    assert [p.status for p in ps] == ["ok", "ok", "sensor off", "underrange", "no sensor", "no sensor"]
    assert ps[2].value is None and ps[4].mbar is None


def test_sen_needs_one_value_per_channel():
    tpg, sim = make_driver("TPG366")
    assert tpg.set_sensor(3, True) == [2, 0, 2, 0, 0, 0]
    assert sim.log[-1] == "SEN,0,0,2,0,0,0"
    with pytest.raises(InstrumentProtocolError, match="syntax"):
        tpg.query("SEN,0,2")  # wrong number of values for a 6-channel unit


def test_tpg361_single_channel():
    tpg, _ = make_driver("TPG361")
    assert tpg.channels == 1
    assert len(tpg.pressures()) == 1
    with pytest.raises(InstrumentProtocolError, match="channels 1-1"):
        tpg.pressure(2)


def test_tpg26x_detected_without_ayt():
    tpg, _ = make_driver("TPG262")
    info = tpg.identify()
    assert info["model"] == "TPG 261/262"
    assert info["firmware"] == "302-510-D"
    assert tpg.unit() == "mbar"
    assert tpg.gauge_ids() == ["PKR", "TPR"]
    with pytest.raises(InstrumentProtocolError, match="supports the units"):
        tpg.set_unit("hPa")  # TPG 26x: mbar, Torr, Pa only


def test_explicit_model_option():
    tpg, _ = make_driver("TPG261", model="tpg261")
    assert tpg.identify()["model"] == "TPG261"


# ---------------------------------------------------------------- MCP


async def test_tools_via_mcp(tmp_path):
    async with sim_client() as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True
        assert info["instrument"]["model"] == "TPG362"

        r = (await client.call_tool("read_pressure", {"channel": 2})).structured_content
        assert r["status"] == "ok"
        assert r["pressure_mbar"] == pytest.approx(2.4e-2, rel=0.05)

        allp = (await client.call_tool("read_all_pressures", {})).structured_content
        assert len(allp["readings"]) == 2 and allp["unit"] == "hPa"

        gauges = (await client.call_tool("get_gauge_types", {})).structured_content["result"]
        assert gauges[0]["identifier"] == "PKR" and gauges[0]["ionisation_gauge"] is True
        assert gauges[1]["power"] == "cannot be switched"

        u = (await client.call_tool("set_unit", {"unit": "Pa"})).structured_content
        assert u["unit"] == "Pa"

        errs = (await client.call_tool("get_errors", {})).structured_content
        assert errs["errors"] == []

        out = tmp_path / "log.csv"
        series = (
            await client.call_tool(
                "log_pressure_series", {"duration_s": 0.6, "interval_s": 0.2, "save_path": str(out)}
            )
        ).structured_content
        assert series["count"] == 4
        assert series["stats"][1]["geometric_mean_mbar"] == pytest.approx(2.4e-2, rel=0.05)
        assert len(out.read_text().strip().splitlines()) == 5

        off = (await client.call_tool("switch_gauge_off", {"channel": 1})).structured_content["result"]
        assert off[0]["power"] == "off"


async def test_switch_on_requires_low_pressure_reference():
    async with sim_client(options={"model": "tpg366"}) as client:
        # IKR on channel 3 is off: no own reading and no reference -> refused
        with pytest.raises(Exception, match="reference_channel"):
            await client.call_tool("set_gauge_power", {"channel": 3, "on": True})
        # the Pirani (channel 2) reads the foreline at 2.4e-2 mbar, above the 1e-2 mbar limit
        with pytest.raises(Exception, match="max_switch_on_pressure_mbar"):
            await client.call_tool("set_gauge_power", {"channel": 3, "on": True, "reference_channel": 2})
        # the PKR (channel 1) reads the chamber in high vacuum -> allowed
        gauges = (
            await client.call_tool("set_gauge_power", {"channel": 3, "on": True, "reference_channel": 1})
        ).structured_content["result"]
        assert gauges[2]["power"] == "on"
        with pytest.raises(Exception, match="cannot be switched"):
            await client.call_tool("set_gauge_power", {"channel": 2, "on": True})


async def test_read_only_hides_control_tools():
    async with sim_client(read_only=True) as client:
        names = await tool_names(client)
        assert {"read_pressure", "read_all_pressures", "get_gauge_types", "log_pressure_series"} <= names
        assert "set_unit" not in names and "set_gauge_power" not in names
        assert "switch_gauge_off" in names


async def test_log_duration_limit():
    async with sim_client(limits={"max_log_duration_s": 10}) as client:
        with pytest.raises(Exception, match="max_log_duration_s"):
            await client.call_tool("log_pressure_series", {"duration_s": 60})


async def test_switch_on_pressure_limit_override():
    # With the limit raised to 0.1 mbar the foreline Pirani is accepted as a reference.
    async with sim_client(options={"model": "tpg366"}, limits={"max_switch_on_pressure_mbar": 0.1}) as client:
        gauges = (
            await client.call_tool("set_gauge_power", {"channel": 3, "on": True, "reference_channel": 2})
        ).structured_content["result"]
        assert gauges[2]["power"] == "on"


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_log_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_log_duration_s", 5)
