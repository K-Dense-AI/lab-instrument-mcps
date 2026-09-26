import time

import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_julabo.driver import JulaboCirculator, parse_status
from labmcp_julabo.server import server
from labmcp_julabo.simulator import JulaboSimulator


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingSimulator(JulaboSimulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.received: list[str] = []

    def handle(self, command: str):
        self.received.append(command)
        return super().handle(command)


def make_driver(**kwargs) -> tuple[JulaboCirculator, RecordingSimulator, FakeClock]:
    clock = FakeClock()
    sim = RecordingSimulator(clock=clock, **kwargs)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination="\r")
    return JulaboCirculator(t, command_delay_s=0), sim, clock


# ---------------------------------------------------------------- driver level


def test_identify_and_status():
    drv, _, _ = make_driver()
    info = drv.identify()
    assert info["manufacturer"] == "JULABO"
    assert info["version"].startswith("JULABO MAGIO")
    st = drv.status()
    assert (st.code, st.kind, st.remote, st.running, st.label) == (2, "state", True, False, "02 REMOTE STOP")


def test_readings():
    drv, _, _ = make_driver()
    assert drv.bath_temperature_c() == pytest.approx(22, abs=0.1)
    assert drv.heating_power_pct() == 0
    assert drv.external_temperature_c() == pytest.approx(22, abs=0.1)
    assert drv.excess_temperature_protection_c() == 120
    assert drv.warning_limits_c() == (150, -30)


def test_set_setpoint_wire_format_and_readback():
    drv, sim, _ = make_driver()
    drv.set_setpoint(37.0)
    assert "out_sp_00 37.00" in sim.received
    assert drv.setpoint_c() == 37.0
    assert drv.t.write_termination == "\r"


def test_rejected_values_raise_device_error():
    drv, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="-11 VALUE TOO LARGE"):
        drv.set_setpoint(250)
    with pytest.raises(InstrumentProtocolError, match="-13 VALUE EXCEEDS TEMPERATURE LIMITS"):
        drv.set_setpoint(160)  # inside the device range but above its warning limit
    with pytest.raises(InstrumentProtocolError, match="-08 INVALID COMMAND"):
        drv.send("out_sp_99 1")
    assert drv.setpoint_c() == 20.0  # unchanged


def test_manual_mode_is_reported():
    drv, _, _ = make_driver(remote=False)
    with pytest.raises(InstrumentProtocolError, match="manual mode"):
        drv.set_setpoint(30)
    with pytest.raises(InstrumentProtocolError, match="manual mode"):
        drv.start()


def test_start_heats_and_stop():
    drv, sim, clock = make_driver()
    drv.set_setpoint(40)
    drv.start()
    assert drv.is_running() and drv.status().label == "03 REMOTE START"
    clock.now += 60
    assert drv.heating_power_pct() == 100  # full power while far from the setpoint
    clock.now += 3600
    assert drv.bath_temperature_c() == pytest.approx(40, abs=0.1)
    assert drv.stop() is True
    assert sim.running is False


def test_stop_reports_failure_if_circulator_keeps_running():
    class Stubborn(RecordingSimulator):
        def handle(self, command):
            if command == "out_mode_05 0":
                self.received.append(command)
                return None
            return super().handle(command)

    clock = FakeClock()
    sim = Stubborn(clock=clock)
    drv = JulaboCirculator(SimulatedTransport(sim, read_termination="\r\n", write_termination="\r"), 0)
    drv.start()
    assert drv.stop() is False
    assert sim.received.count("out_mode_05 0") == 2  # retried once


def test_parse_status_messages():
    alarm = parse_status("-01 LOW LEVEL ALARM")
    assert alarm.kind == "alarm" and "bath fluid level too low" in alarm.meaning
    assert parse_status("-09 COMMAND NOT ALLOWED IN CURRENT OPERATING MODE").kind == "command_error"
    assert parse_status("-9999 SOMETHING").meaning.startswith("alarm/warning not listed")
    with pytest.raises(InstrumentProtocolError):
        parse_status("garbage")


def test_query_number_rejects_status_reply():
    drv, sim, _ = make_driver()
    sim.raise_alarm()
    # An alarm/status line where a number was expected is decoded, not parsed as a value.
    with pytest.raises(InstrumentProtocolError, match="bath fluid level too low"):
        drv.query_number("status")
    with pytest.raises(InstrumentProtocolError, match="expected a number"):
        drv.query_number("version")


def test_corio_cd_optional_commands_do_not_poison_status():
    drv, _, _ = make_driver(corio_cd=True)
    assert drv.warning_limits_c() == (None, None)
    assert drv.optional_number("in_pv_02") is None
    drv.set_setpoint(30)  # the -08 left by the unsupported queries must not be blamed on this
    assert drv.setpoint_c() == 30
    assert "in_sp_03" in drv.unsupported


def test_keepalive_queries_status():
    drv, sim, _ = make_driver()
    drv.start_keepalive(0.05)
    deadline = time.monotonic() + 5  # poll rather than sleep a fixed time; CI runners can be slow
    while sim.received.count("status") < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    drv.close()
    assert sim.received.count("status") >= 3


# ---------------------------------------------------------------- MCP level


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["version"].startswith("JULABO")

        status = (await client.call_tool("get_status", {})).structured_content
        assert status["message"] == "02 REMOTE STOP" and status["remote_control"] is True
        assert status["high_warning_limit_c"] == 150

        temps = (await client.call_tool("read_temperatures", {"include_external": True})).structured_content
        assert temps["bath_temperature_c"] == pytest.approx(22, abs=0.5)
        assert temps["external_temperature_c"] is not None

        result = (await client.call_tool("set_setpoint", {"temperature_c": 37})).structured_content
        assert result["setpoint_c"] == 37
        started = (await client.call_tool("start_circulation", {})).structured_content
        assert started["running"] is True

        server.driver.t.simulator.time_scale = 5000
        wait = (
            await client.call_tool(
                "wait_for_temperature",
                {"tolerance_c": 0.2, "stable_for_s": 0, "timeout_s": 30, "poll_interval_s": 0.5},
            )
        ).structured_content
        assert wait["reached"] is True and wait["target_c"] == 37

        stopped = (await client.call_tool("stop_circulation", {})).data
        assert "stopped" in stopped
        assert server.driver.t.simulator.running is False

        log = (await client.call_tool("get_command_log", {"limit": 300})).data
        assert any(entry["data"] == "out_sp_00 37.00" for entry in log)


async def test_alarm_ends_wait():
    async with simulated_client(server) as client:
        await client.call_tool("set_setpoint", {"temperature_c": 60})
        await client.call_tool("start_circulation", {})
        server.driver.t.simulator.raise_alarm("-01 LOW LEVEL ALARM")
        wait = (await client.call_tool("wait_for_temperature", {"timeout_s": 30})).structured_content
        assert wait["reached"] is False and wait["aborted"] is True
        assert "-01 LOW LEVEL ALARM" in wait["message"]
        status = (await client.call_tool("get_status", {})).structured_content
        assert status["kind"] == "alarm" and status["running"] is False


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert "set_setpoint" not in names
        assert "start_circulation" not in names
        for kept in ("read_temperatures", "get_status", "get_setpoint", "wait_for_temperature"):
            assert kept in names
        assert "stop_circulation" in names  # safety tools stay available


async def test_max_temperature_limit():
    async with simulated_client(server, limits={"max_temperature_c": 50}) as client:
        with pytest.raises(Exception, match="max_temperature_c"):
            await client.call_tool("set_setpoint", {"temperature_c": 60})
        assert server.driver.t.simulator.setpoint_c == 20  # nothing was sent


async def test_min_temperature_limit():
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="min_temperature_c"):
            await client.call_tool("set_setpoint", {"temperature_c": -10})


async def test_start_checks_setpoint_changed_on_the_front_panel():
    async with simulated_client(server, limits={"max_temperature_c": 50}) as client:
        server.driver.t.simulator.setpoint_c = 80
        with pytest.raises(Exception, match="max_temperature_c"):
            await client.call_tool("start_circulation", {})
        assert server.driver.t.simulator.running is False


async def test_wait_limit():
    async with simulated_client(server, limits={"max_wait_s": 60}) as client:
        with pytest.raises(Exception, match="max_wait_s"):
            await client.call_tool("wait_for_temperature", {"timeout_s": 600})


def test_limit_error_type():
    server.configure(simulate=True, limits={"min_temperature_c": 10})
    with pytest.raises(SafetyLimitError):
        server.check("min_temperature_c", 4)


@pytest.mark.parametrize("option", [{"command_delay_s": "-1"}, {"command_delay_s": "abc"}, {"keepalive_s": "nan"}])
async def test_invalid_timing_options_are_refused_at_connect(option):
    # Regression: a negative command_delay_s made every OUT command, including the stop command,
    # fail later with a raw "sleep length must be non-negative" ValueError.
    async with simulated_client(server, options=option) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is False and "--option" in info["error"]
    server.configure(options={})
