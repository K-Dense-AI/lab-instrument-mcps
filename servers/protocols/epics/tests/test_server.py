import asyncio
import os

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, InstrumentTimeout, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_epics.driver import EnvOverride, EpicsClient, ca_environment, parse_safe_state
from labmcp_epics.server import server
from labmcp_epics.simulator import SimulatedIOC

# ------------------------------------------------------------------ pure helpers


def test_ca_environment_from_address_and_options():
    env = ca_environment("10.0.1.20, 10.0.1.21:5064", {})
    assert env == {"EPICS_CA_ADDR_LIST": "10.0.1.20 10.0.1.21:5064", "EPICS_CA_AUTO_ADDR_LIST": "NO"}
    env = ca_environment(None, {"ca_addr_list": "gw.facility.org", "auto_addr_list": "yes", "server_port": "5070"})
    assert env["EPICS_CA_AUTO_ADDR_LIST"] == "YES"
    assert env["EPICS_CA_SERVER_PORT"] == "5070"
    assert ca_environment(None, {}) == {}


def test_parse_safe_state():
    assert parse_safe_state("BL7:MTR1.STOP=1; BL7:SHUTTER=Close;BL7:HV=0.5") == [
        ("BL7:MTR1.STOP", 1),
        ("BL7:SHUTTER", "Close"),
        ("BL7:HV", 0.5),
    ]
    assert parse_safe_state(None) == []
    with pytest.raises(ValueError):
        parse_safe_state("NOVALUE")


def test_env_override_restores():
    os.environ.pop("EPICS_TEST_VAR", None)
    env = EnvOverride({"EPICS_TEST_VAR": "x"})
    env.apply()
    assert os.environ["EPICS_TEST_VAR"] == "x"
    env.restore()
    assert "EPICS_TEST_VAR" not in os.environ


# ------------------------------------------------------------------ driver against the simulated IOC


@pytest.fixture(scope="module")
def client():
    ioc = SimulatedIOC("TST:").start()
    c = EpicsClient(env=ioc.client_env, timeout=2.0, on_close=ioc.stop)
    yield c
    c.close()
    assert os.environ.get("EPICS_CA_ADDR_LIST") != ioc.client_env["EPICS_CA_ADDR_LIST"]


def test_read_metadata(client):
    r = client.read("TST:TEMP")
    assert r.native_type == "DOUBLE" and r.units == "degC" and r.precision == 2
    assert r.alarm_limits == (0.0, 80.0) and r.warning_limits == (5.0, 60.0)
    assert r.severity == "NO_ALARM" and r.timestamp is not None
    e = client.read("TST:HEATER")
    assert e.native_type == "ENUM" and e.value == "Off" and e.enum_strings == ["Off", "On"]
    assert client.read("TST:SAMPLE").value == "empty"
    assert client.read("TST:SPECTRUM").count == 512


def test_unknown_pv_is_a_clear_error(client):
    with pytest.raises(InstrumentConnectionError, match="EPICS_CA_ADDR_LIST"):
        client.read("TST:DOES:NOT:EXIST", timeout=0.5)


def test_access_rights_and_limits_refuse_before_writing(client):
    with pytest.raises(InstrumentProtocolError, match="read-only access"):
        client.prepare_put("TST:TEMP", 30)
    with pytest.raises(InstrumentProtocolError, match="control limits"):
        client.prepare_put("TST:TEMP:SP", 150)
    with pytest.raises(InstrumentProtocolError, match="integer PV"):
        client.prepare_put("TST:DET:FRAMES", 2.5)
    with pytest.raises(InstrumentProtocolError, match="not a valid state"):
        client.prepare_put("TST:HEATER", "Maybe")
    with pytest.raises(InstrumentProtocolError, match="numeric"):
        client.prepare_put("TST:TEMP:SP", "hot")
    assert client.read("TST:TEMP:SP").value == 22.0  # nothing was written


def test_put_callback_waits_for_motion(client):
    prep = client.prepare_put("TST:MTR", 0.4)
    out = client.put(prep, wait=True, timeout=10)
    assert out["completed"] and out["elapsed_s"] > 0.15  # 0.4 mm at 2 mm/s
    assert client.read("TST:MTR:RBV").value == pytest.approx(0.4)
    assert client.read("TST:MTR:DMOV").value == 1


def test_put_timeout_is_reported_not_retried(client):
    prep = client.prepare_put("TST:MTR", 9.0)
    with pytest.raises(InstrumentTimeout, match="Do not repeat"):
        client.put(prep, wait=True, timeout=0.2)
    assert client.read("TST:MTR:DMOV").value == 0  # still moving
    client.put(client.prepare_put("TST:MTR:STOP", 1), wait=True, timeout=2)


# ------------------------------------------------------------------ MCP round trips


async def test_tools_via_mcp():
    async with simulated_client(server) as c:
        info = (await c.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        temp = (await c.call_tool("get_pv", {"name": "SIM:TEMP"})).structured_content
        assert temp["units"] == "degC" and temp["age_s"] < 5

        spec = (await c.call_tool("get_pv", {"name": "SIM:SPECTRUM", "max_elements": 16})).structured_content
        assert spec["downsampled"] and len(spec["value"]) == 16
        assert 200 < spec["array_summary"]["index_of_max"] < 300

        many = (await c.call_tool("get_pvs", {"names": ["SIM:TEMP", "SIM:HEATER", "SIM:NOPE"]})).structured_content
        assert [r["name"] for r in many["readings"]] == ["SIM:TEMP", "SIM:HEATER"]
        assert many["errors"][0]["name"] == "SIM:NOPE"

        pv = (await c.call_tool("pv_info", {"name": "SIM:TEMP"})).structured_content
        assert pv["read_access"] and not pv["write_access"] and pv["server"].startswith("127.0.0.1:")

        put = (await c.call_tool("put_pv", {"name": "SIM:MTR", "value": 0.5})).structured_content
        assert put["completed"] and put["value"] == pytest.approx(0.5) and put["previous_value"] == 0.0

        heater = (await c.call_tool("put_pv", {"name": "SIM:HEATER", "value": "On"})).structured_content
        assert heater["value"] == "On"

        batch = (
            await c.call_tool(
                "put_pvs",
                {"writes": [{"name": "SIM:TEMP:SP", "value": 45}, {"name": "SIM:SAMPLE", "value": "LaB6 NIST 660c"}]},
            )
        ).structured_content
        assert [r["value"] for r in batch["results"]] == [45.0, "LaB6 NIST 660c"]

        mon = (await c.call_tool("monitor_pv", {"name": "SIM:TEMP", "duration_s": 1.5})).structured_content
        assert mon["n_updates"] >= 5
        assert mon["stats"]["rate_per_s"] > 0  # heating towards 45 degC

        log = (await c.call_tool("get_command_log", {"limit": 50})).data
        assert any("caput -c SIM:MTR" in e["data"] for e in log)


async def test_batch_is_validated_before_anything_is_written():
    async with simulated_client(server) as c:
        with pytest.raises(Exception, match="control limits"):
            await c.call_tool(
                "put_pvs",
                {"writes": [{"name": "SIM:TEMP:SP", "value": 30}, {"name": "SIM:MTR", "value": 50}]},
            )
        sp = (await c.call_tool("get_pv", {"name": "SIM:TEMP:SP"})).structured_content
        assert sp["value"] == 22.0


async def test_safe_state_stops_a_running_move():
    async with simulated_client(server) as c:
        move = asyncio.create_task(c.call_tool("put_pv", {"name": "SIM:MTR", "value": -9, "timeout_s": 30}))
        await asyncio.sleep(0.4)
        safe = (await c.call_tool("apply_safe_state", {})).structured_content
        assert safe["configured"] and not safe["errors"]
        result = (await move).structured_content
        assert -9 < result["value"] < 0  # stopped part-way
        heater = (await c.call_tool("get_pv", {"name": "SIM:HEATER"})).structured_content
        assert heater["value"] == "Off"


async def test_put_allowlist_and_required_limits():
    options = {"put_allowlist": r"SIM:(TEMP:SP|MTR:STOP)", "require_ctrl_limits": "true"}
    async with simulated_client(server, options=options) as c:
        with pytest.raises(Exception, match="allow-list"):
            await c.call_tool("put_pv", {"name": "SIM:MTR", "value": 1})
        with pytest.raises(Exception, match="no control limits"):
            await c.call_tool("put_pv", {"name": "SIM:MTR:STOP", "value": 1})
        ok = (await c.call_tool("put_pv", {"name": "SIM:TEMP:SP", "value": 30})).structured_content
        assert ok["value"] == 30.0
        # the safe state bypasses the allow-list (it was configured by the scientist)
        safe = (await c.call_tool("apply_safe_state", {})).structured_content
        assert [r["name"] for r in safe["results"]] == ["SIM:HEATER"]  # STOP refused: no limits + required
        assert safe["errors"][0]["name"] == "SIM:MTR:STOP"


async def test_read_only_hides_writes_keeps_safety():
    async with simulated_client(server, read_only=True) as c:
        names = await tool_names(c)
        assert {"get_pv", "get_pvs", "monitor_pv", "pv_info"} <= names
        assert "put_pv" not in names and "put_pvs" not in names
        assert "apply_safe_state" in names and "reconnect" in names


async def test_limits_refuse_out_of_range_requests():
    limits = {"max_monitor_duration_s": 1, "max_put_batch": 1, "max_put_wait_s": 2}
    async with simulated_client(server, limits=limits) as c:
        with pytest.raises(Exception, match="max_monitor_duration_s"):
            await c.call_tool("monitor_pv", {"name": "SIM:TEMP", "duration_s": 5})
        with pytest.raises(Exception, match="max_put_batch"):
            await c.call_tool(
                "put_pvs", {"writes": [{"name": "SIM:TEMP:SP", "value": 30}, {"name": "SIM:MTR", "value": 1}]}
            )
        with pytest.raises(Exception, match="max_put_wait_s"):
            await c.call_tool("put_pv", {"name": "SIM:MTR", "value": 1, "timeout_s": 10})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_put_batch": 2})
    with pytest.raises(SafetyLimitError):
        server.check("max_put_batch", 3)
    server.configure(simulate=True, limits={})


def test_disconnect_restores_environment():
    before = os.environ.get("EPICS_CA_ADDR_LIST")
    server.configure(simulate=True)
    assert server._connection_info()["connected"]
    assert os.environ.get("EPICS_CA_ADDR_LIST", "").startswith("127.0.0.1:")
    server.disconnect()
    assert os.environ.get("EPICS_CA_ADDR_LIST") == before
