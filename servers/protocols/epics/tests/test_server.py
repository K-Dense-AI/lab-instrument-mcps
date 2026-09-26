import asyncio
import dataclasses
import math
import os
import time

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, InstrumentTimeout, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_epics import server as server_module
from labmcp_epics.driver import EnvOverride, EpicsClient, PVReading, _limits, ca_environment, parse_safe_state
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


def test_nan_limits_mean_no_limit_on_that_side():
    # EPICS 3.16+ reports unset limits as NaN (e.g. ai alarm limits with no alarm severity)
    assert _limits(math.nan, math.nan) is None
    assert _limits(0, 0) is None
    lo, hi = _limits(math.nan, 80.0)
    assert math.isnan(lo) and hi == 80.0


def test_failed_start_restores_environment_and_stops_the_ioc(monkeypatch):
    import caproto.threading.client as ca_client

    def broken_context(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(ca_client, "Context", broken_context)
    os.environ.pop("EPICS_TEST_VAR2", None)
    closed = []
    with pytest.raises(OSError):
        EpicsClient(env={"EPICS_TEST_VAR2": "x"}, on_close=lambda: closed.append(True))
    assert closed == [True]  # e.g. SimulatedIOC.stop: no leaked IOC thread
    assert "EPICS_TEST_VAR2" not in os.environ


# ------------------------------------------------------------------ write validation without an IOC


class _FakePV:
    def __init__(self, name):
        from caproto import AccessRights

        self.name = name
        self.access_rights = AccessRights.READ | AccessRights.WRITE


def _offline(reading, **attrs):
    """An EpicsClient whose PVs all look like ``reading`` (no network)."""
    from caproto import AccessRights

    c = EpicsClient.__new__(EpicsClient)
    c.AccessRights = AccessRights
    c.allow, c.require_ctrl_limits, c.allow_limit_field_writes = None, False, False
    for k, v in attrs.items():
        setattr(c, k, v)
    c.connect = lambda names, timeout=None: [_FakePV(n) for n in names]
    c._read_pv = lambda pv, timeout: dataclasses.replace(reading, name=pv.name)
    return c


def test_float32_overflow_is_refused():
    c = _offline(PVReading(name="X", value=1.0, native_type="FLOAT", count=1))
    with pytest.raises(InstrumentProtocolError, match="32-bit FLOAT"):
        c.prepare_put("X:GAIN", 1e39)  # would reach the IOC as +inf
    assert c.prepare_put("X:GAIN", 1e38).data == [1e38]


def test_nan_control_limit_side_is_unbounded_but_other_side_holds():
    c = _offline(PVReading(name="X", value=1.0, native_type="DOUBLE", count=1, control_limits=(math.nan, 10.0)))
    with pytest.raises(InstrumentProtocolError, match="control limits"):
        c.prepare_put("X:SP", 11)
    assert c.prepare_put("X:SP", -1e6).data == [-1e6]


def test_limit_fields_cannot_be_written_to_escape_the_limits():
    c = _offline(PVReading(name="X", value=1.0, native_type="DOUBLE", count=1))
    for name in ("BL7:MTR1.DRVH", "BL7:MTR1.hlm", "BL7:MTR1.DLLM", "BL7:AO.LOPR$"):
        with pytest.raises(InstrumentProtocolError, match="sets a limit"):
            c.prepare_put(name, 1000)
    assert c.prepare_put("BL7:MTR1.VAL", 1).data == [1.0]
    assert c.prepare_put("BL7:MTR1.DRVH", 5, check_allowlist=False).data == [5.0]  # scientist's safe state
    c.allow_limit_field_writes = True
    assert c.prepare_put("BL7:MTR1.DRVH", 5).data == [5.0]


def test_non_latin1_text_is_refused_not_mangled():
    wave = _offline(PVReading(name="X", value=[0] * 40, native_type="CHAR", count=40))
    with pytest.raises(InstrumentProtocolError, match="latin-1"):
        wave.prepare_put("X:FILE", "sample_\u20ac.h5")  # used to be written as 'sample_?.h5'
    assert wave.prepare_put("X:FILE", "caf\u00e9").data[:4] == list("caf\u00e9".encode("latin-1"))
    string = _offline(PVReading(name="X", value="", native_type="STRING", count=1))
    with pytest.raises(InstrumentProtocolError, match="latin-1"):
        string.prepare_put("X:NAME", "\u20ac")


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


def test_invalid_names_are_refused_before_caproto(client):
    # caproto raises for record names > 59 chars inside its search thread, which kills the thread and
    # silently breaks every later PV search on the context (including the safe-state PVs).
    too_long = "TST:" + "X" * 60
    with pytest.raises(InstrumentProtocolError, match="at most 59"):
        client.read(too_long)
    with pytest.raises(InstrumentProtocolError, match="Invalid PV name"):
        client.read("TST:TEMP\n")
    results = client.read_many([too_long, "", "TST:TEMP"], timeout=1.0)
    assert isinstance(results[0], InstrumentProtocolError) and isinstance(results[1], InstrumentProtocolError)
    assert results[2].name == "TST:TEMP"
    assert client.read("TST:BEAM:CURRENT", timeout=2.0).units == "mA"  # a fresh search still works


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


async def test_long_name_in_get_pvs_does_not_break_later_searches():
    async with simulated_client(server) as c:
        many = (await c.call_tool("get_pvs", {"names": ["SIM:" + "Y" * 60, "SIM:TEMP"]})).structured_content
        assert [r["name"] for r in many["readings"]] == ["SIM:TEMP"] and "at most 59" in many["errors"][0]["error"]
        heater = (await c.call_tool("get_pv", {"name": "SIM:HEATER"})).structured_content
        assert heater["value"] == "Off"


async def test_nan_readings_and_nan_limits_pass_output_validation(monkeypatch):
    original = EpicsClient._read_pv

    def with_nans(self, pv, timeout):
        r = original(self, pv, timeout)
        r.alarm_limits = (math.nan, 80.0)  # EPICS 7 ai record: only HIHI has a severity
        r.warning_limits = (math.nan, math.nan)
        if r.array is not None and r.array.size > 1:
            r.array = r.array.astype(float)
            r.array[:3] = [math.nan, math.inf, -math.inf]
        elif r.native_type == "DOUBLE":
            r.value = math.nan
        return r

    monkeypatch.setattr(EpicsClient, "_read_pv", with_nans)
    async with simulated_client(server) as c:
        temp = (await c.call_tool("get_pv", {"name": "SIM:TEMP"})).structured_content
        assert temp["value"] == "nan"
        assert temp["alarm_limits"] == {"low": None, "high": 80.0} and temp["warning_limits"] is None
        spec = (await c.call_tool("get_pv", {"name": "SIM:SPECTRUM", "max_elements": 8})).structured_content
        assert spec["array_summary"]["non_finite"] == 3 and spec["array_summary"]["maximum"] > 0


async def test_monitor_with_nan_updates(monkeypatch):
    original = EpicsClient.update_value
    calls = []

    def sometimes_nan(self, native, response, enum_strings):
        calls.append(1)
        return math.nan if len(calls) % 2 else original(self, native, response, enum_strings)

    monkeypatch.setattr(EpicsClient, "update_value", sometimes_nan)
    async with simulated_client(server) as c:
        mon = (await c.call_tool("monitor_pv", {"name": "SIM:TEMP", "duration_s": 1.0})).structured_content
        assert mon["n_updates"] >= 3 and mon["non_finite_updates"] >= 1
        assert math.isfinite(mon["stats"]["mean"]) and math.isfinite(mon["stats"]["rate_per_s"])
        assert "nan" in [u["value"] for u in mon["updates"]]


async def test_put_batch_stays_inside_the_tool_timeout(monkeypatch):
    # FastMCP can't stop the worker thread when the tool timeout fires, so the batch itself must stop
    # waiting and writing before then (the budget stands in for the 3600 s of the real tool).
    batch = {"writes": [{"name": "SIM:MTR", "value": 2.0}, {"name": "SIM:TEMP:SP", "value": 30}]}  # 1 s move
    monkeypatch.setattr(server_module, "_PUT_BUDGET_S", 0.8)
    async with simulated_client(server) as c:
        t0 = time.monotonic()
        out = (await c.call_tool("put_pvs", batch)).structured_content
        assert time.monotonic() - t0 < 3
        assert out["results"] == [] and out["not_written"] == ["SIM:TEMP:SP"] and "No put-completion" in out["error"]
        await c.call_tool("apply_safe_state", {})
        assert (await c.call_tool("get_pv", {"name": "SIM:TEMP:SP"})).structured_content["value"] == 22.0

    # the first write completes, then too little budget is left to start the second
    monkeypatch.setattr(server_module, "_PUT_BUDGET_S", 3.0)
    monkeypatch.setattr(server_module, "_MIN_PUT_WAIT_S", 2.5)
    async with simulated_client(server) as c:
        out = (await c.call_tool("put_pvs", batch)).structured_content
        assert [r["name"] for r in out["results"]] == ["SIM:MTR"]
        assert out["not_written"] == ["SIM:TEMP:SP"] and out["error"].startswith("Stopped")
        assert (await c.call_tool("get_pv", {"name": "SIM:TEMP:SP"})).structured_content["value"] == 22.0


async def test_safe_state_errors_and_empty_sim_prefix():
    async with simulated_client(server, options={"sim_prefix": ""}) as c:
        safe = (await c.call_tool("apply_safe_state", {})).structured_content
        assert [r["name"] for r in safe["results"]] == ["SIM:MTR:STOP", "SIM:HEATER"] and not safe["errors"]
    async with simulated_client(server, options={"safe_state": "NOVALUE"}) as c:
        with pytest.raises(Exception, match="safe_state is malformed"):
            await c.call_tool("apply_safe_state", {})


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
