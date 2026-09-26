import csv

import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_thorlabs_pm.driver import ThorlabsPowerMeter, format_power, watts_to_dbm
from labmcp_thorlabs_pm.server import server
from labmcp_thorlabs_pm.simulator import LASER_W, ThorlabsPMSimulator, si_responsivity


def make_driver(**kwargs) -> tuple[ThorlabsPowerMeter, ThorlabsPMSimulator]:
    channel = kwargs.pop("channel", None)
    sim = ThorlabsPMSimulator(**kwargs)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    return ThorlabsPowerMeter(t, channel=channel), sim


# ---------------------------------------------------------------- driver


def test_identify_and_sensor():
    pm, _ = make_driver()
    info = pm.identify()
    assert info["model"] == "PM100D"
    assert info["sensor"] == "S120C"
    s = pm.sensor()
    assert s.type_name == "photodiode"
    assert s.measures_power and s.wavelength_settable
    assert not s.has_temperature_sensor
    assert pm.wavelength_range_nm() == (400.0, 1100.0)


def test_power_depends_on_wavelength_setting():
    pm, sim = make_driver()
    pm.set_wavelength_nm(633)
    p633 = pm.power_w()
    assert p633 == pytest.approx(LASER_W, rel=0.03)
    pm.set_wavelength_nm(532)
    p532 = pm.power_w()
    # A Si photodiode set to the wrong wavelength reads R(633)/R(532) too high.
    assert p532 / p633 == pytest.approx(si_responsivity(632.8) / si_responsivity(532), rel=0.02)


def test_dbm_unit_is_converted_to_watts():
    pm, sim = make_driver()
    pm.set_wavelength_nm(633)
    pm.write("SENS:POW:UNIT DBM")
    assert pm.power_unit() == "DBM"
    assert pm.power_w() == pytest.approx(LASER_W, rel=0.03)


def test_zero_removes_dark_offset():
    pm, sim = make_driver(laser_w=0.0)
    before = pm.power_w()
    assert before > 1e-8  # dark current shows up as ~tens of nW
    zero_a = pm.zero(poll_s=0.05)
    assert zero_a == pytest.approx(sim.sensor["offset"], rel=0.05)
    assert abs(pm.power_w()) < 2e-9


def test_manual_range_over_range_raises():
    pm, _ = make_driver()
    pm.set_range_w(5e-5)
    assert not pm.auto_range()
    with pytest.raises(InstrumentProtocolError, match="out of the measurement range"):
        pm.power_w()
    pm.set_auto_range(True)
    assert pm.power_w() > 0


def test_out_of_range_wavelength_reports_scpi_error():
    pm, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="-222"):
        pm.command("SENS:CORR:WAV 2000")


def test_undefined_header_error():
    pm, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="Undefined header"):
        pm.command("SENS:BOGUS 1")


def test_temperature_needs_thermal_sensor():
    pm, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="no temperature sensor"):
        pm.temperature_c()
    thermal, _ = make_driver(sensor="S302C")
    assert thermal.sensor().type_name == "thermopile"
    assert thermal.temperature_c() == pytest.approx(24.1, abs=0.5)


def test_channel_suffix_for_pm5020():
    pm, _ = make_driver(channel=2)
    pm.set_wavelength_nm(633)
    assert pm.power_w() > 0
    assert pm.wavelength_nm() == 633


def test_helpers():
    assert watts_to_dbm(1e-3) == pytest.approx(0.0)
    assert watts_to_dbm(-1e-9) is None
    assert format_power(1.234e-3) == "1.234 mW"
    assert format_power(5.6e-7) == "560 nW"


# ---------------------------------------------------------------- MCP


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["model"] == "PM100D"
        assert dev["sensor"]["name"] == "S120C"
        assert dev["wavelength_min_nm"] == 400

        wl = (await client.call_tool("set_wavelength", {"wavelength_nm": 632.8})).structured_content
        assert wl["wavelength_nm"] == 633

        reading = (await client.call_tool("read_power", {})).structured_content
        assert reading["power_w"] == pytest.approx(LASER_W, rel=0.03)
        assert reading["formatted"].endswith("mW")
        assert reading["power_dbm"] == pytest.approx(0.91, abs=0.2)

        avg = (await client.call_tool("set_averaging", {"count": 100})).structured_content
        assert avg["averaging_count"] == 100

        rng = (await client.call_tool("set_range", {"mode": "manual", "range_w": 4e-3})).structured_content
        assert rng["auto_range"] is False and rng["range_w"] == pytest.approx(5e-3)
        rng = (await client.call_tool("set_range", {"mode": "auto"})).structured_content
        assert rng["auto_range"] is True

        out = tmp_path / "series.csv"
        series = (
            await client.call_tool(
                "log_power_series", {"count": 20, "interval_s": 0.0, "max_points": 10, "save_path": str(out)}
            )
        ).structured_content
        assert series["count"] == 20 and series["points_returned"] == 10 and series["completed"] is True
        assert series["saved_to"] == str(out.resolve())
        assert series["rms_stability_percent"] < 1.0
        with out.open() as fh:
            assert len(list(csv.reader(fh))) == 21

        zero = (await client.call_tool("zero_sensor", {})).structured_content
        assert zero["zero_value"] > 0

        with pytest.raises(Exception, match="outside the calibrated range"):
            await client.call_tool("set_wavelength", {"wavelength_nm": 1550})

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(entry["data"] == "MEAS:POW?" for entry in log)


async def test_thermal_sensor_temperature_via_mcp():
    async with simulated_client(server, options={"sim_sensor": "S302C"}) as client:
        temp = (await client.call_tool("read_sensor_temperature", {})).structured_content
        assert 20 < temp["temperature_c"] < 30


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"read_power", "get_device_info", "log_power_series", "read_sensor_temperature"} <= names
        assert not names & {"set_wavelength", "set_averaging", "set_range", "zero_sensor"}
        assert "reconnect" in names


async def test_series_duration_limit():
    async with simulated_client(server, limits={"max_series_duration_s": 5}) as client:
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_power_series", {"count": 11, "interval_s": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_series_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_series_duration_s", 5)


async def test_series_duration_includes_the_measurement_time():
    # With interval_s=0 the old check saw a 0 s series whatever the count or averaging; at 10000
    # samples x 3 ms each reading takes ~30 s, so 30 readings take ~15 min.
    async with simulated_client(server) as client:
        await client.call_tool("set_averaging", {"count": 10000})
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_power_series", {"count": 30, "interval_s": 0})
    async with simulated_client(server, limits={"max_series_duration_s": 1e6}) as client:
        with pytest.raises(Exception, match="one call can take"):
            await client.call_tool("log_power_series", {"count": 5000, "interval_s": 1})


async def test_series_stops_early_within_the_time_budget(monkeypatch):
    from labmcp_thorlabs_pm import server as server_module

    monkeypatch.setattr(server_module, "_SERIES_BUDGET_S", 3.5)  # measure_timeout() is ~3 s
    async with simulated_client(server) as client:
        series = (
            await client.call_tool("log_power_series", {"count": 100, "interval_s": 0.01})
        ).structured_content
        assert series["completed"] is False
        assert 2 <= series["count"] < 100
        assert series["duration_s"] < 1.5


async def test_series_save_path_is_checked_before_logging(tmp_path):
    existing = tmp_path / "series.csv"
    existing.write_text("keep me", encoding="utf-8")
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool(
                "log_power_series", {"count": 5, "interval_s": 0, "save_path": str(existing)}
            )
        with pytest.raises(Exception, match=r"must end in \.csv"):
            await client.call_tool("log_power_series", {"count": 5, "save_path": str(tmp_path / "s.xlsx")})
        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert not any(entry["data"] == "MEAS:POW?" for entry in log)
    assert existing.read_text(encoding="utf-8") == "keep me"
