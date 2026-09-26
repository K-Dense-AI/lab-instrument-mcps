import json
import time

import httpx
import pytest
from labmcp import AuditLog, InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_opentrons.driver import (
    HttpxBackend,
    OpentronsRobot,
    RobotHTTPError,
    deck_layout,
    module_setpoints,
    normalize_base_url,
)
from labmcp_opentrons.server import server
from labmcp_opentrons.simulator import FakeOpentronsRobot

GOOD_PROTOCOL = """
from opentrons import protocol_api

metadata = {"protocolName": "Serial dilution"}
requirements = {"robotType": "Flex", "apiLevel": "2.19"}

def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_flex_96_tiprack_200ul", "C2")
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "D2")
    reservoir = protocol.load_labware("nest_12_reservoir_15ml", "D1")
    temp = protocol.load_module("temperature module gen2", "B1")
    cold = temp.load_labware("opentrons_96_aluminumblock_nest_wellplate_100ul")
    pipette = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    temp.set_temperature(celsius=4)
    pipette.transfer(100, reservoir["A1"], plate["A1"])
    pipette.transfer(100, plate["A1"], plate["A2"], mix_after=(3, 50))
    pipette.transfer(100, plate["A2"], cold["A3"])
"""

HOT_PROTOCOL = GOOD_PROTOCOL.replace("temp.set_temperature(celsius=4)", "temp.set_temperature(celsius=95)")

SHAKE_PROTOCOL = """
requirements = {"robotType": "Flex", "apiLevel": "2.19"}

def run(protocol):
    hs = protocol.load_module("heaterShakerModuleV1", "D1")
    plate = hs.load_labware("nest_96_wellplate_200ul_flat")
    hs.set_and_wait_for_temperature(37)
    hs.set_and_wait_for_shake_speed(rpm=2500)
    protocol.delay(seconds=10)
    hs.deactivate_shaker()
"""

BROKEN_PROTOCOL = """
requirements = {"robotType": "Flex", "apiLevel": "2.19"}

def run(protocol)
    protocol.home()
"""

OT2_PROTOCOL = """
metadata = {"apiLevel": "2.15", "protocolName": "OT-2 test"}

def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    p300 = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    p300.transfer(50, plate["A1"], plate["B1"])
"""


class ManualClock:
    def __init__(self) -> None:
        self.t = time.monotonic()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_robot(model: str = "flex", **kwargs) -> tuple[OpentronsRobot, FakeOpentronsRobot, ManualClock]:
    clock = ManualClock()
    sim = FakeOpentronsRobot(model, clock=clock, command_duration_s=1.0, analysis_duration_s=2.0, **kwargs)
    return OpentronsRobot(sim, AuditLog(), poll_interval_s=0.0), sim, clock


def upload(robot: OpentronsRobot, clock: ManualClock, text: str, name: str = "protocol.py") -> dict:
    proto = robot.upload_protocol([(name, text.encode())])
    clock.advance(5)
    return robot.protocol(proto["id"])


# ------------------------------------------------------------------ driver


def test_normalize_base_url():
    assert normalize_base_url("192.168.1.20") == "http://192.168.1.20:31950"
    assert normalize_base_url("ot2.local:8080") == "http://ot2.local:8080"
    assert normalize_base_url("http://10.0.0.5:31950/") == "http://10.0.0.5:31950"
    assert normalize_base_url("[fe80::1]") == "http://[fe80::1]:31950"
    for bad in ("", "ftp://x", "http://10.0.0.5/runs"):
        with pytest.raises(ValueError):
            normalize_base_url(bad)


def test_identify_flex_and_ot2():
    robot, _, _ = make_robot("flex")
    info = robot.identify()
    assert info["model"] == "Flex"
    assert info["manufacturer"] == "Opentrons"
    robot2, _, _ = make_robot("ot2")
    assert robot2.identify()["model"] == "OT-2"
    assert robot2.estop_status() is None  # OT-2 answers 403 NotSupportedOnOT2
    assert robot.estop_status()["status"] == "disengaged"


def test_lights_roundtrip_and_audit():
    robot, sim, _ = make_robot()
    assert robot.set_lights(True) is True
    assert sim.lights is True
    assert robot.lights_on() is True
    log = [e["data"] for e in robot.audit.recent(10)]
    assert any(line.startswith('POST /robot/lights {"on":true}') for line in log)
    assert any(line.startswith("HTTP 200") for line in log)


def test_upload_and_analysis_ok_with_deck_layout():
    robot, _, clock = make_robot()
    proto = robot.upload_protocol([("dilution.py", GOOD_PROTOCOL.encode())])
    assert proto["analysisSummaries"][-1]["status"] == "pending"
    clock.advance(5)
    proto, analysis = robot.wait_for_analysis(proto["id"], timeout_s=1)
    assert analysis["result"] == "ok"
    layout = deck_layout(analysis)
    slots = {lw["load_name"]: lw["location"] for lw in layout["labware"]}
    assert slots["opentrons_flex_96_tiprack_200ul"] == "slot C2"
    assert slots["opentrons_96_aluminumblock_nest_wellplate_100ul"].startswith("on temperatureModuleV2 (slot B1)")
    assert layout["pipettes"] == [{"name": "flex_1channel_1000", "mount": "left"}]
    assert module_setpoints(analysis["commands"]) == (4.0, None)


def test_same_files_return_existing_protocol():
    robot, _, clock = make_robot()
    first = upload(robot, clock, GOOD_PROTOCOL)
    again = robot.upload_protocol([("protocol.py", GOOD_PROTOCOL.encode())])
    assert again["id"] == first["id"]


def test_syntax_error_fails_analysis():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, BROKEN_PROTOCOL)
    analysis = robot.latest_analysis(proto)
    assert analysis["result"] == "not-ok"
    assert "SyntaxError" in analysis["errors"][0]["detail"]


def test_wrong_robot_type_is_rejected():
    robot, _, _ = make_robot("ot2")
    with pytest.raises(RobotHTTPError, match="ProtocolRobotTypeMismatch.*Flex") as info:
        robot.upload_protocol([("dilution.py", GOOD_PROTOCOL.encode())])
    assert info.value.status == 422


def test_missing_api_level_is_rejected():
    robot, _, _ = make_robot()
    with pytest.raises(RobotHTTPError, match="apiLevel"):
        robot.upload_protocol([("p.py", b"def run(protocol):\n    pass\n")])


def test_run_lifecycle_play_pause_resume_succeed():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL)
    total = len(robot.latest_analysis(proto)["commands"])
    run = robot.create_run(proto["id"])
    assert run["status"] == "idle" and run["current"] is True
    robot.run_action(run["id"], "play")
    clock.advance(3.5)
    assert robot.run(run["id"])["status"] == "running"
    page = robot.run_commands(run["id"], page_length=5)
    assert page["meta"]["totalLength"] == 4
    assert page["links"]["current"]["meta"]["index"] == 3
    robot.run_action(run["id"], "pause")
    clock.advance(100)
    assert robot.run(run["id"])["status"] == "paused"
    assert robot.run_commands(run["id"])["meta"]["totalLength"] == 4  # frozen while paused
    with pytest.raises(RobotHTTPError, match="Cannot pause a run that is not running") as info:
        robot.run_action(run["id"], "pause")
    assert info.value.status == 409
    robot.run_action(run["id"], "play")
    clock.advance(total + 1)
    done = robot.run(run["id"])
    assert done["status"] == "succeeded"
    assert done["completedAt"]
    with pytest.raises(RobotHTTPError, match="already stopped"):
        robot.run_action(run["id"], "play")


def test_stop_request_then_stopped():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL)
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    clock.advance(2)
    robot.run_action(run["id"], "stop")
    assert robot.run(run["id"])["status"] == "stopped"


def test_second_run_refused_while_active():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL)
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    with pytest.raises(RobotHTTPError, match="RunAlreadyActive"):
        robot.create_run(proto["id"])


def test_open_door_blocks_run():
    robot, sim, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL)
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    sim.open_door()
    assert robot.run(run["id"])["status"] == "blocked-by-open-door"
    assert robot.door_status()["status"] == "open"
    with pytest.raises(RobotHTTPError, match="door"):
        robot.run_action(run["id"], "play")
    sim.close_door()
    assert robot.run(run["id"])["status"] == "paused"


def test_error_recovery_then_resume():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL + "# SIMULATE_RECOVERABLE_ERROR\n")
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    clock.advance(100)
    assert robot.run(run["id"])["status"] == "awaiting-recovery"
    page = robot.run_commands(run["id"])
    assert page["links"]["currentlyRecoveringFrom"]
    with pytest.raises(RobotHTTPError, match="recovery mode"):
        robot.run_action(run["id"], "pause")
    robot.run_action(run["id"], "resume-from-recovery")
    clock.advance(100)
    assert robot.run(run["id"])["status"] == "succeeded"


def test_fatal_error_fails_run():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL + "# SIMULATE_FATAL_ERROR\n")
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    clock.advance(100)
    failed = robot.run(run["id"])
    assert failed["status"] == "failed"
    assert "Overpressure" in failed["errors"][0]["detail"]


def test_stateless_commands_blocked_during_run():
    robot, sim, clock = make_robot()
    assert robot.home()["status"] == "succeeded"
    proto = upload(robot, clock, GOOD_PROTOCOL)
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    with pytest.raises(RobotHTTPError, match="RunActive"):
        robot.home()
    robot.run_action(run["id"], "stop")
    temp = next(m for m in robot.modules() if m["moduleType"] == "temperatureModuleType")
    robot.stateless_command("temperatureModule/deactivate", {"moduleId": temp["id"]})
    with pytest.raises(InstrumentProtocolError, match="no module loaded"):
        robot.stateless_command("temperatureModule/deactivate", {"moduleId": "nope"})


def test_module_temperature_follows_protocol():
    robot, _, clock = make_robot()
    proto = upload(robot, clock, GOOD_PROTOCOL)
    run = robot.create_run(proto["id"])
    robot.run_action(run["id"], "play")
    clock.advance(100)
    assert robot.run(run["id"])["status"] == "succeeded"
    clock.advance(60)  # the module keeps cooling after the run
    temp = next(m for m in robot.modules() if m["moduleType"] == "temperatureModuleType")
    assert temp["data"]["targetTemperature"] == 4.0
    assert temp["data"]["status"] == "cooling"
    assert 4.0 < temp["data"]["currentTemperature"] < 6.0


def test_invalid_ids_are_rejected_before_sending():
    robot, _, _ = make_robot()
    with pytest.raises(InstrumentProtocolError, match="Invalid run ID"):
        robot.run("../health")
    assert robot.audit.recent(5) == []


def test_httpx_backend_headers_and_multipart():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/protocols":
            return httpx.Response(201, json={"data": {"id": "p1", "analysisSummaries": []}})
        if request.url.path == "/runs/r1/actions":
            return httpx.Response(409, json={"errors": [{"id": "RunActionNotAllowed", "title": "Run Action Not Allowed",
                                                         "detail": "Cannot pause a run that is not running."}]})
        return httpx.Response(404, json={"message": "nope", "errorCode": "4000"})

    backend = HttpxBackend("http://robot.test:31950", access_token="tok", transport=httpx.MockTransport(handler))
    robot = OpentronsRobot(backend, AuditLog())
    assert robot.upload_protocol([("a.py", b"print(1)")])["id"] == "p1"
    req = seen[0]
    assert req.headers["Opentrons-Version"] == "3"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="files"; filename="a.py"' in req.content
    with pytest.raises(RobotHTTPError, match=r"HTTP 409 RunActionNotAllowed\): Cannot pause"):
        robot.run_action("r1", "pause")
    with pytest.raises(RobotHTTPError, match="HTTP 404.*nope"):
        robot.lights_on()


def test_httpx_backend_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = HttpxBackend("http://robot.test:31950", transport=httpx.MockTransport(handler))
    with pytest.raises(InstrumentConnectionError, match="Could not reach the robot"):
        OpentronsRobot(backend).health()


# ------------------------------------------------------------------ MCP


def fast_sim() -> FakeOpentronsRobot:
    sim = server.driver.backend
    sim.command_duration_s = 0.01
    sim.analysis_duration_s = 0.05
    return sim


async def test_status_instruments_modules_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        status = (await client.call_tool("get_robot_status", {})).structured_content
        assert status["model"] == "Flex"
        assert status["ready_to_start_run"] is True
        inst = (await client.call_tool("list_instruments", {})).structured_content["result"]
        assert {i["instrument_type"] for i in inst} == {"pipette", "gripper"}
        assert inst[0]["max_volume_ul"] == 1000.0
        mods = (await client.call_tool("list_modules", {})).structured_content["result"]
        assert "Heater-Shaker" in {m["module_type"] for m in mods}
        assert all("temperature_c" in m for m in mods)
        await client.call_tool("set_lights", {"on": True})
        assert (await client.call_tool("get_robot_status", {})).structured_content["lights_on"] is True


async def test_protocol_run_workflow_via_mcp(tmp_path):
    path = tmp_path / "dilution.py"
    path.write_text(GOOD_PROTOCOL)
    async with simulated_client(server) as client:
        fast_sim()
        detail = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content
        assert detail["ready_to_run"] is True, detail
        assert detail["analysis_result"] == "ok"
        assert detail["max_module_temperature_c"] == 4.0
        assert any(lw["location"] == "slot D2" for lw in detail["deck"]["labware"])
        pid = detail["id"]

        listed = (await client.call_tool("list_protocols", {})).structured_content["result"]
        assert listed[0]["name"] == "Serial dilution"

        with pytest.raises(Exception, match="deck not confirmed"):
            await client.call_tool("start_run", {"protocol_id": pid, "deck_confirmed": False})

        server.driver.backend.command_duration_s = 5.0  # keep it running while we pause
        run = (await client.call_tool("start_run", {"protocol_id": pid, "deck_confirmed": True})).structured_content
        assert run["status"] == "running"
        assert run["commands_expected"] > 5

        paused = (await client.call_tool("pause_run", {})).structured_content
        assert paused["accepted"] is True and paused["status"] == "paused"
        with pytest.raises(Exception, match="another run|Run .* is paused"):
            await client.call_tool("home_robot", {})
        resumed = (await client.call_tool("resume_run", {})).structured_content
        assert resumed["status"] == "running"

        stopped = (await client.call_tool("stop_run", {})).structured_content
        assert stopped["accepted"] is True
        final = (await client.call_tool("get_run_status", {"run_id": run["id"]})).structured_content
        assert final["status"] == "stopped"
        again = (await client.call_tool("stop_run", {"run_id": run["id"]})).structured_content
        assert again["accepted"] is False

        runs = (await client.call_tool("list_runs", {})).structured_content["result"]
        assert runs[0]["id"] == run["id"]

        off = (await client.call_tool("deactivate_modules", {})).structured_content["result"]
        assert off and all(m["ok"] for m in off)
        assert (await client.call_tool("home_robot", {})).data == "Robot homed."

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(e["data"].startswith("POST /runs ") for e in log)
        assert any('"actionType":"play"' in e["data"] for e in log)


async def test_run_completes_with_progress(tmp_path):
    path = tmp_path / "dilution.py"
    path.write_text(GOOD_PROTOCOL)
    async with simulated_client(server) as client:
        fast_sim()
        pid = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content["id"]
        await client.call_tool("start_run", {"protocol_id": pid, "deck_confirmed": True})
        time.sleep(0.5)
        status = (await client.call_tool("get_run_status", {})).structured_content
        assert status["status"] == "succeeded"
        assert status["progress_percent"] == 100.0
        assert status["commands_executed"] == status["commands_expected"]


async def test_failed_analysis_is_reported_and_blocks_start(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text(BROKEN_PROTOCOL)
    async with simulated_client(server) as client:
        fast_sim()
        detail = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content
        assert detail["ready_to_run"] is False
        assert "SyntaxError" in detail["message"]
        assert detail["analysis_errors"]
        with pytest.raises(Exception, match="Refused to start.*FAILED"):
            await client.call_tool("start_run", {"protocol_id": detail["id"], "deck_confirmed": True})


async def test_awaiting_recovery_requires_explicit_choice(tmp_path):
    path = tmp_path / "flaky.py"
    path.write_text(GOOD_PROTOCOL + "# SIMULATE_RECOVERABLE_ERROR\n")
    async with simulated_client(server) as client:
        fast_sim()
        pid = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content["id"]
        await client.call_tool("start_run", {"protocol_id": pid, "deck_confirmed": True})
        time.sleep(0.3)
        status = (await client.call_tool("get_run_status", {})).structured_content
        assert status["status"] == "awaiting-recovery"
        assert "tipPhysicallyMissing" in status["recovering_from"]["error"]
        with pytest.raises(Exception, match="awaiting error recovery"):
            await client.call_tool("resume_run", {})
        await client.call_tool("resume_run", {"error_recovery": "assume_false_positive"})
        time.sleep(0.3)
        assert (await client.call_tool("get_run_status", {})).structured_content["status"] == "succeeded"


async def test_upload_rejects_bad_paths(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="File not found"):
            await client.call_tool("upload_protocol", {"path": str(tmp_path / "missing.py")})
        with pytest.raises(Exception, match="expected a .json or .py file"):
            await client.call_tool("upload_protocol", {"path": str(txt)})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_robot_status", "list_protocols", "get_run_status", "list_modules"} <= names
        for hidden in ("upload_protocol", "start_run", "resume_run", "home_robot", "set_lights"):
            assert hidden not in names
        for kept in ("pause_run", "stop_run", "deactivate_modules", "reconnect"):
            assert kept in names


async def test_temperature_limit_refuses_hot_protocol(tmp_path):
    path = tmp_path / "hot.py"
    path.write_text(HOT_PROTOCOL)
    async with simulated_client(server, limits={"max_module_temperature_c": 50}) as client:
        fast_sim()
        detail = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content
        assert detail["ready_to_run"] is False
        assert "max_module_temperature_c" in detail["message"]
        with pytest.raises(Exception, match="max_module_temperature_c"):
            await client.call_tool("start_run", {"protocol_id": detail["id"], "deck_confirmed": True})
        assert server.driver.backend.runs == {}  # nothing was created


async def test_shake_limit_refuses_fast_protocol(tmp_path):
    path = tmp_path / "shake.py"
    path.write_text(SHAKE_PROTOCOL)
    async with simulated_client(server, limits={"max_shake_speed_rpm": 1000}) as client:
        fast_sim()
        detail = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content
        assert detail["max_shake_speed_rpm"] == 2500
        with pytest.raises(Exception, match="max_shake_speed_rpm"):
            await client.call_tool("start_run", {"protocol_id": detail["id"], "deck_confirmed": True})


async def test_ot2_simulation_option(tmp_path):
    path = tmp_path / "ot2.py"
    path.write_text(OT2_PROTOCOL)
    async with simulated_client(server, options={"sim_model": "ot2"}) as client:
        fast_sim()
        status = (await client.call_tool("get_robot_status", {})).structured_content
        assert status["model"] == "OT-2" and status["estop"] is None
        detail = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content
        assert detail["ready_to_run"] is True
        assert {"load_name": "opentrons_96_tiprack_300ul", "display_name": None, "location": "slot 1"} in detail["deck"]["labware"]


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_module_temperature_c": 60})
    with pytest.raises(SafetyLimitError):
        server.check("max_module_temperature_c", 95)


def test_error_body_formats():
    robot, _, _ = make_robot()
    body = robot.call("GET", "/health")
    assert json.dumps(body)  # plain JSON
    with pytest.raises(RobotHTTPError, match="RouteNotFound"):
        robot.call("GET", "/nope")


async def test_deactivate_modules_sends_every_command_even_after_a_timeout():
    # Regression: a timeout (InstrumentTimeout) on one deactivate command used to escape the
    # loop, so the Heater-Shaker heater and all later modules were left on.
    from labmcp import InstrumentTimeout

    async with simulated_client(server) as client:
        sim = fast_sim()
        for m in sim.attached_modules:
            if "target" in m:
                m["target"] = 60.0
        drv = server.driver
        real = drv.stateless_command

        def flaky(ctype, params, timeout_s=60.0):
            if ctype == "heaterShaker/deactivateShaker":
                raise InstrumentTimeout("Robot command heaterShaker/deactivateShaker did not finish")
            return real(ctype, params, timeout_s=timeout_s)

        drv.stateless_command = flaky
        try:
            off = (await client.call_tool("deactivate_modules", {})).structured_content["result"]
        finally:
            del drv.stateless_command
        by_type = {m["module_type"]: m for m in off}
        assert by_type["Heater-Shaker"]["ok"] is False
        assert "deactivateShaker" in by_type["Heater-Shaker"]["error"]
        assert by_type["Temperature Module"]["ok"] is True
        assert by_type["Thermocycler"]["ok"] is True
        sent = [c["commandType"] for c in sim.stateless]
        assert "heaterShaker/deactivateHeater" in sent
        assert all(m.get("target") is None for m in sim.attached_modules if "target" in m)


# ------------------------------------------------------------------ regressions (bug review)


async def _running_run(client, tmp_path) -> str:
    path = tmp_path / "dilution.py"
    path.write_text(GOOD_PROTOCOL, encoding="utf-8")
    fast_sim()
    pid = (await client.call_tool("upload_protocol", {"path": str(path)})).structured_content["id"]
    server.driver.backend.command_duration_s = 5.0
    run = (await client.call_tool("start_run", {"protocol_id": pid, "deck_confirmed": True})).structured_content
    assert run["status"] == "running"
    return run["id"]


async def test_stop_run_is_sent_even_if_the_status_read_fails(tmp_path):
    from labmcp import InstrumentTimeout

    async with simulated_client(server) as client:
        rid = await _running_run(client, tmp_path)
        drv = server.driver
        real_run = drv.run
        calls = {"n": 0}

        def flaky_run(run_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise InstrumentTimeout("GET /runs/... timed out")
            return real_run(run_id)

        drv.run = flaky_run
        try:
            stopped = (await client.call_tool("stop_run", {"run_id": rid})).structured_content
        finally:
            del drv.run
        assert stopped["accepted"] is True
        assert drv.backend.runs[rid].status in {"stop-requested", "stopped"}


async def test_stop_run_race_with_a_run_that_just_ended_is_not_an_error(tmp_path):
    async with simulated_client(server) as client:
        rid = await _running_run(client, tmp_path)
        drv = server.driver
        real_action = drv.run_action

        def ended_meanwhile(run_id, action):
            drv.backend.runs[run_id].status = "succeeded"  # finished between the status read and the stop
            return real_action(run_id, action)  # the robot answers 409 RunActionNotAllowed

        drv.run_action = ended_meanwhile
        try:
            result = (await client.call_tool("stop_run", {"run_id": rid})).structured_content
        finally:
            del drv.run_action
        assert result["accepted"] is False and result["status"] == "succeeded"
        assert "already succeeded" in result["message"]


async def test_deactivate_modules_reports_unknown_module_ids():
    async with simulated_client(server) as client:
        off = (await client.call_tool("deactivate_modules", {"module_ids": ["no-such-module"]})).structured_content
        assert off["result"] == [{"module_id": "no-such-module", "module_type": "unknown", "commands": [],
                                  "ok": False, "error": "No attached module has this ID; see list_modules."}]
        assert server.driver.backend.stateless == []
