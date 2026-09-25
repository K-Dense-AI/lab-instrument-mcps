import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_ika.driver import IKAStirrer
from labmcp_ika.server import server
from labmcp_ika.simulator import IKASimulator


@pytest.fixture(autouse=True)
def _default_device():
    # InstrumentServer.configure() keeps --option values between calls; reset them so the
    # overhead-stirrer test does not leak into the others.
    server.configure(options={})
    yield
    server.configure(options={})


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingSimulator(IKASimulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.received: list[str] = []

    def handle(self, command: str):
        self.received.append(command)
        return super().handle(command)


def make_driver(device: str = "hotplate", **kwargs) -> tuple[IKAStirrer, RecordingSimulator, FakeClock]:
    clock = FakeClock()
    sim = RecordingSimulator(device=device, clock=clock, **kwargs)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination=" \r\n")
    return IKAStirrer(t, device_type=device), sim, clock


# ---------------------------------------------------------------- driver level


def test_identify_and_read_temperatures():
    drv, _, _ = make_driver()
    assert drv.identify() == {"manufacturer": "IKA", "device_type": "hotplate", "model": "RCT digital"}
    assert drv.hotplate_temperature_c() == pytest.approx(22.0, abs=1)
    assert drv.external_temperature_c() == pytest.approx(22.0, abs=1)
    assert drv.safety_temperature_c() == 360


def test_commands_use_namur_wire_format():
    drv, sim, _ = make_driver()
    drv.set_temperature(80.0)
    drv.set_speed(400.4)
    drv.start_heater()
    drv.start_motor()
    assert sim.received == ["OUT_SP_1 80", "OUT_SP_4 400", "START_1", "START_4"]
    # The transport terminates every command with "blank CR LF" (0x20 0x0D 0x0A).
    assert drv.t.write_termination == " \r\n"


def test_heating_follows_setpoint():
    drv, sim, clock = make_driver()
    drv.set_temperature(80)
    assert drv.temperature_setpoint_c() == 80
    drv.start_heater()
    clock.now += 1800
    assert drv.hotplate_temperature_c() == pytest.approx(80, abs=2)
    assert drv.external_temperature_c() == pytest.approx(80, abs=2)
    drv.stop_heater()
    assert sim.heating is False
    assert drv.heating_commanded is False


def test_stirring():
    drv, sim, clock = make_driver()
    drv.set_speed(500)
    drv.start_motor()
    clock.now += 30
    assert drv.speed_rpm() == pytest.approx(500, abs=3)
    drv.stop_all()
    assert sim.motor is False and sim.heating is False


def test_non_numeric_reply_raises():
    drv, _, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="expected a number"):
        drv.query_number("IN_NAME")


def test_unknown_command_times_out():
    drv, _, _ = make_driver()
    with pytest.raises(InstrumentTimeout):
        drv.query("IN_PV_9")


def test_overhead_rejects_hotplate_commands():
    drv, sim, _ = make_driver("overhead")
    with pytest.raises(InstrumentProtocolError, match="only available on IKA hotplate"):
        drv.set_temperature(50)
    assert sim.received == []  # nothing was sent
    assert drv.speed_limit_rpm() == 2000
    assert drv.torque_limit() == 60


def test_watchdog_mode1_switches_off_when_not_refreshed():
    drv, sim, clock = make_driver()
    drv.set_temperature(60)
    drv.start_heater()
    drv.start_motor()
    drv.enable_watchdog(1, 20, keepalive_interval_s=3600)  # no automatic refresh in this test
    assert "OUT_WD1@20" in sim.received
    clock.now += 15
    drv.watchdog_tick()  # refresh in time
    clock.now += 15
    drv.speed_rpm()
    assert sim.heating is True
    clock.now += 25  # no refresh for > 20 s
    drv.speed_rpm()
    assert sim.heating is False and sim.motor is False and sim.wd_tripped
    drv.close()


def test_watchdog_mode2_falls_back_to_safety_values():
    drv, sim, clock = make_driver()
    drv.set_temperature(120)
    drv.set_watchdog_safety_values(40, 100)
    assert "OUT_SP_12@40" in sim.received and "OUT_SP_42@100" in sim.received
    drv.enable_watchdog(2, 30, keepalive_interval_s=3600)
    clock.now += 40
    assert drv.temperature_setpoint_c() == 40
    assert drv.speed_setpoint_rpm() == 100
    drv.close()


def test_watchdog_mode1_cannot_be_cancelled_mode2_can():
    drv, sim, _ = make_driver()
    drv.enable_watchdog(1, 60, keepalive_interval_s=3600)
    with pytest.raises(InstrumentProtocolError, match="cannot be cancelled"):
        drv.disable_watchdog()
    drv.close()
    drv2, sim2, _ = make_driver()
    drv2.enable_watchdog(2, 60, keepalive_interval_s=3600)
    drv2.disable_watchdog()
    assert "OUT_WD2@0" in sim2.received and sim2.wd_mode is None
    with pytest.raises(ValueError):
        drv2.enable_watchdog(1, 5)


def test_watchdog_keepalive_thread_refreshes():
    drv, sim, _ = make_driver()
    drv.enable_watchdog(1, 20, keepalive_interval_s=0.05)
    import time

    time.sleep(0.3)
    assert sim.received.count("OUT_WD1@20") >= 3
    assert drv.watchdog_state()["keepalive_running"] is True
    drv.close()
    assert drv.watchdog_state()["keepalive_running"] is False


# ---------------------------------------------------------------- MCP level


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "RCT digital"

        status = (await client.call_tool("get_status", {})).structured_content
        assert status["device_type"] == "hotplate"
        assert status["hotplate_temperature_c"] == pytest.approx(22, abs=1)

        result = (await client.call_tool("set_temperature", {"temperature_c": 60})).structured_content
        assert result["reported_by_device"] == 60 and result["warning"] is None
        await client.call_tool("start_heating", {})
        await client.call_tool("set_speed", {"speed_rpm": 300})
        await client.call_tool("start_stirring", {})

        server.driver.t.simulator.time_scale = 2000  # 0.5 s real = ~17 min simulated
        wait = (
            await client.call_tool(
                "wait_for_temperature",
                {"sensor": "external", "tolerance_c": 1.5, "stable_for_s": 0, "timeout_s": 30, "poll_interval_s": 0.5},
            )
        ).structured_content
        assert wait["reached"] is True
        assert wait["target_c"] == 60

        stopped = (await client.call_tool("stop_all", {})).data
        assert "stopped" in stopped
        assert server.driver.t.simulator.heating is False

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(entry["data"] == "OUT_SP_1 60 " for entry in log)


async def test_watchdog_via_mcp():
    async with simulated_client(server) as client:
        state = (await client.call_tool("enable_watchdog", {"timeout_s": 60, "mode": 1})).structured_content
        assert state["mode"] == 1 and state["keepalive_running"] is True
        with pytest.raises(Exception, match="cannot be cancelled"):
            await client.call_tool("disable_watchdog", {})


async def test_overhead_stirrer_via_mcp():
    async with simulated_client(server, options={"device": "overhead"}) as client:
        status = (await client.call_tool("get_status", {})).structured_content
        assert status["device_type"] == "overhead"
        assert status["speed_limit_rpm"] == 2000
        assert status["hotplate_temperature_c"] is None
        await client.call_tool("set_speed", {"speed_rpm": 200})
        await client.call_tool("start_stirring", {})
        with pytest.raises(Exception, match="only available on hotplates"):
            await client.call_tool("set_temperature", {"temperature_c": 50})
        await client.call_tool("stop_all", {})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        for hidden in ("set_temperature", "start_heating", "set_speed", "start_stirring", "enable_watchdog"):
            assert hidden not in names
        for kept in ("get_status", "wait_for_temperature", "stop_heating", "stop_stirring", "stop_all", "reconnect"):
            assert kept in names


async def test_temperature_limit():
    async with simulated_client(server, limits={"max_temperature_c": 100}) as client:
        with pytest.raises(Exception, match="max_temperature_c"):
            await client.call_tool("set_temperature", {"temperature_c": 120})
        assert server.driver.t.simulator.temp_setpoint_c == 0  # nothing was sent


async def test_start_heating_checks_setpoint_set_on_the_device():
    async with simulated_client(server, limits={"max_temperature_c": 100}) as client:
        server.driver.t.simulator.temp_setpoint_c = 250  # someone turned the knob
        with pytest.raises(Exception, match="max_temperature_c"):
            await client.call_tool("start_heating", {})
        assert server.driver.t.simulator.heating is False


async def test_device_safety_circuit_refuses_setpoint():
    async with simulated_client(server, limits={"max_temperature_c": 400}) as client:
        server.driver.t.simulator.safety_temp_c = 100
        with pytest.raises(Exception, match="safety-circuit"):
            await client.call_tool("set_temperature", {"temperature_c": 120})


async def test_speed_limit():
    async with simulated_client(server, limits={"max_speed_rpm": 500}) as client:
        with pytest.raises(Exception, match="max_speed_rpm"):
            await client.call_tool("set_speed", {"speed_rpm": 800})


async def test_wait_limit():
    async with simulated_client(server, limits={"max_wait_s": 10}) as client:
        with pytest.raises(Exception, match="max_wait_s"):
            await client.call_tool("wait_for_temperature", {"target_c": 50, "timeout_s": 60})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_speed_rpm": 100})
    with pytest.raises(SafetyLimitError):
        server.check("max_speed_rpm", 200)
