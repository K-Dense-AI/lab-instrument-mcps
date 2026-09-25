import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_atlas_ezo.driver import EZOCircuit, _parse_query
from labmcp_atlas_ezo.server import server
from labmcp_atlas_ezo.simulator import EZOSimulator


def make_circuit(sensor: str = "pH", **kwargs) -> tuple[EZOCircuit, EZOSimulator]:
    sim = EZOSimulator(sensor, **kwargs)
    t = SimulatedTransport(sim, read_termination="\r", write_termination="\r", timeout=0.5)
    c = EZOCircuit(t, timeout=0.5)
    c.initialise()
    return c, sim


def test_query_parsing_variants():
    assert _parse_query("?Cal,2", "Cal") == ["2"]
    assert _parse_query("?CAL,3", "Cal") == ["3"]
    assert _parse_query("?,P,90.25", "P") == ["90.25"]
    assert _parse_query("?,O,EC,TDS,S,SG", "O") == ["EC", "TDS", "S", "SG"]
    assert _parse_query("?Slope,99.7,100.3,-0.89", "Slope") == ["99.7", "100.3", "-0.89"]
    assert _parse_query("7.012", "Cal") is None


def test_initialise_turns_continuous_off_and_identifies():
    c, sim = make_circuit("pH")
    assert sim.continuous == 0  # C,0 was sent
    assert sim.response_codes is True
    assert c.sensor == "PH" and c.firmware == "2.16"
    assert c.identify()["model"] == "EZO-pH"


def test_initialise_enables_response_codes_when_off():
    sim = EZOSimulator("pH")
    sim.response_codes = False
    t = SimulatedTransport(sim, read_termination="\r", write_termination="\r", timeout=0.5)
    c = EZOCircuit(t, timeout=0.5)
    c.initialise()
    assert sim.response_codes is True and sim.continuous == 0


def test_ph_reading_and_calibration_sequence():
    c, sim = make_circuit("pH")
    r = c.read()
    assert r.units == {"ph": "pH"}
    assert r.values["ph"] == pytest.approx(7.2 + 0.12, abs=0.02)  # uncalibrated error
    c.calibrate("mid", 7.0)
    c.calibrate("low", 4.0)
    c.calibrate("high", 10.0)
    assert c.calibration_points() == 3
    assert c.slope() == (99.7, 100.3, -0.89)
    assert c.read().values["ph"] == pytest.approx(7.2, abs=0.02)
    c.calibrate("mid", 7.0)  # a mid calibration clears low/high
    assert c.calibration_points() == 1


def test_error_reply_raises():
    c, _ = make_circuit("pH")
    with pytest.raises(InstrumentProtocolError, match=r"\*ER"):
        c.command("K,1.0")  # K only exists on EC circuits


def test_temperature_compensation():
    c, _ = make_circuit("EC")
    assert c.temperature_compensation() == 25.0
    assert c.set_temperature_compensation(19.5) == 19.5


def test_ec_multi_output_and_parameters():
    c, sim = make_circuit("EC")
    r = c.read()
    assert list(r.values) == ["conductivity_us_cm", "tds_ppm", "salinity_psu", "specific_gravity"]
    assert r.units["conductivity_us_cm"] == "µS/cm"
    assert r.values["tds_ppm"] == pytest.approx(r.values["conductivity_us_cm"] * 0.54, rel=0.02)
    c.command("O,TDS,0")
    c.command("O,SG,0")
    r = c.read()  # the driver notices the output change and re-reads O,?
    assert list(r.values) == ["conductivity_us_cm", "salinity_psu"]
    assert c.set_probe_constant(10) == 10


def test_do_outputs_and_pressure_quirk():
    c, sim = make_circuit("DO")
    assert list(c.read().values) == ["do_mg_l"]
    c.command("O,%,1")
    c.refresh_config()
    r = c.read()
    assert list(r.values) == ["do_mg_l", "do_percent_saturation"]
    assert r.units["do_percent_saturation"] == "% sat"
    c.set_pressure_kpa(90.25)
    assert c.pressure_kpa() == 90.25  # reply is "?,P,90.25"
    c.set_salinity(37.5, ppt=True)
    assert c.salinity() == (37.5, "ppt")


def test_rtd_scale_and_hum_dew_label():
    c, sim = make_circuit("RTD")
    assert c.read().units == {"temperature": "°C"}
    c.command("S,f")
    c.refresh_config()
    r = c.read()
    assert r.units == {"temperature": "°F"} and r.values["temperature"] == pytest.approx(77.2, abs=1)
    h, _ = make_circuit("HUM")
    h.command("O,T,1")
    h.command("O,Dew,1")
    h.refresh_config()
    r = h.read()  # raw looks like "45.20,22.40,Dew,9.94"
    assert list(r.values) == ["relative_humidity_percent", "air_temperature_c", "dew_point_c"]
    assert r.values["dew_point_c"] < r.values["air_temperature_c"]


def test_unsupported_device_type_is_refused():
    class PumpSim(EZOSimulator):
        def _dispatch(self, cmd):
            if cmd.lower() == "i":
                return self._ok("?i,PMP,1.03")
            return super()._dispatch(cmd)

    t = SimulatedTransport(PumpSim("pH"), read_termination="\r", write_termination="\r", timeout=0.5)
    with pytest.raises(InstrumentProtocolError, match="PMP"):
        EZOCircuit(t, timeout=0.5).initialise()


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "EZO-pH"

        reading = (await client.call_tool("read_value", {})).structured_content
        assert reading["unit"] == "pH" and reading["temperature_compensation_c"] == 25.0

        details = (await client.call_tool("get_info", {})).structured_content
        assert details["supply_voltage_v"] == pytest.approx(5.038)
        assert details["restart_reason"] == "powered off"
        assert details["calibration_point_names"] == ["mid", "low", "high"]

        status = (await client.call_tool("get_calibration_status", {})).structured_content
        assert status["points"] == 0

        with pytest.raises(Exception, match="midpoint first"):
            await client.call_tool("calibrate", {"point": "low", "value": 4.0})
        with pytest.raises(Exception, match="Invalid calibration point"):
            await client.call_tool("calibrate", {"point": "dry"})
        with pytest.raises(Exception, match="must be 6-8"):
            await client.call_tool("calibrate", {"point": "mid", "value": 4.0})
        result = (await client.call_tool("calibrate", {"point": "mid", "value": 7.0})).structured_content
        assert result["calibration_points"] == 1

        temp = (await client.call_tool("set_temperature_compensation", {"temperature_c": 37.0})).data
        assert temp == 37.0

        led = (await client.call_tool("set_led", {"on": False})).data
        assert led is False

        series = (await client.call_tool("log_series", {"count": 2, "interval_s": 1.0})).structured_content
        assert series["count"] == 2 and series["stats"][0]["unit"] == "pH"

        cleared = (await client.call_tool("clear_calibration", {})).structured_content
        assert cleared["points"] == 0

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(entry["data"] == "C,0" for entry in log)


async def test_other_sensor_types_via_option():
    async with simulated_client(server, options={"sim_sensor": "DO"}) as client:
        reading = (await client.call_tool("read_value", {})).structured_content
        assert reading["unit"] == "mg/L"
        info = (await client.call_tool("set_do_compensation", {"pressure_kpa": 85.0})).structured_content
        assert info["do_pressure_kpa"] == 85.0
        await client.call_tool("calibrate", {"point": "atmospheric"})
        with pytest.raises(Exception, match="takes no value"):
            await client.call_tool("calibrate", {"point": "zero", "value": 0})
    async with simulated_client(server, options={"sim_sensor": "RTD"}) as client:
        with pytest.raises(Exception, match="no temperature compensation"):
            await client.call_tool("set_temperature_compensation", {"temperature_c": 20})
    async with simulated_client(server, options={"sim_sensor": "CO2"}) as client:
        reading = (await client.call_tool("read_value", {})).structured_content
        assert reading["unit"] == "ppm"
        with pytest.raises(Exception, match="3000-5000"):
            await client.call_tool("calibrate", {"point": "high", "value": 1000})
    server.configure(options={})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"read_value", "get_info", "get_calibration_status", "log_series"} <= names
        for hidden in ("calibrate", "clear_calibration", "set_temperature_compensation", "set_led",
                       "set_probe_constant", "set_do_compensation"):
            assert hidden not in names
        assert "reconnect" in names


async def test_series_duration_limit():
    async with simulated_client(server, limits={"max_series_duration_s": 5}) as client:
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_series", {"count": 10, "interval_s": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_series_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_series_duration_s", 5)
    server.disconnect()
