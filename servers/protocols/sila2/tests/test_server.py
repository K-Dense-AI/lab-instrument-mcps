import asyncio
import base64

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_sila2 import driver as drv
from labmcp_sila2.driver import SilaBridge, open_client, parse_address, parse_allowlist
from labmcp_sila2.fdl import (
    Converter,
    FDLValidationError,
    convert_parameters,
    parse_feature,
    to_json_schema,
    to_jsonable,
)
from labmcp_sila2.server import server
from labmcp_sila2.simulator import CANCEL_CONTROLLER_FDL, TEMPERATURE_CONTROLLER_FDL, SimulatedSilaServer

# A feature exercising every basic type and constraint (parsed locally, never served).
TYPES_FDL = """<?xml version="1.0" encoding="utf-8" ?>
<Feature SiLA2Version="1.0" FeatureVersion="2.1" Originator="org.example" Category="tests"
         xmlns="http://www.sila-standard.org">
  <Identifier>TypeZoo</Identifier><DisplayName>Type Zoo</DisplayName><Description>All types.</Description>
  <Command>
    <Identifier>Everything</Identifier><DisplayName>Everything</DisplayName><Description>d</Description>
    <Observable>No</Observable>
    <Parameter><Identifier>Name</Identifier><DisplayName>n</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>String</Basic></DataType>
        <Constraints><MinimalLength>2</MinimalLength><MaximalLength>8</MaximalLength><Pattern>[A-Z][a-z]+</Pattern></Constraints>
      </Constrained></DataType></Parameter>
    <Parameter><Identifier>Mode</Identifier><DisplayName>m</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>String</Basic></DataType>
        <Constraints><Set><Value>Fast</Value><Value>Slow</Value></Set></Constraints></Constrained></DataType></Parameter>
    <Parameter><Identifier>Count</Identifier><DisplayName>c</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>Integer</Basic></DataType>
        <Constraints><MinimalExclusive>0</MinimalExclusive><MaximalExclusive>100</MaximalExclusive></Constraints>
      </Constrained></DataType></Parameter>
    <Parameter><Identifier>Enabled</Identifier><DisplayName>e</DisplayName><Description>d</Description>
      <DataType><Basic>Boolean</Basic></DataType></Parameter>
    <Parameter><Identifier>Blob</Identifier><DisplayName>b</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>Binary</Basic></DataType>
        <Constraints><MaximalLength>4</MaximalLength></Constraints></Constrained></DataType></Parameter>
    <Parameter><Identifier>Day</Identifier><DisplayName>d</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>Date</Basic></DataType>
        <Constraints><MinimalInclusive>2020-01-01Z</MinimalInclusive></Constraints></Constrained></DataType></Parameter>
    <Parameter><Identifier>At</Identifier><DisplayName>a</DisplayName><Description>d</Description>
      <DataType><Basic>Timestamp</Basic></DataType></Parameter>
    <Parameter><Identifier>Clock</Identifier><DisplayName>c</DisplayName><Description>d</Description>
      <DataType><Basic>Time</Basic></DataType></Parameter>
    <Parameter><Identifier>Wells</Identifier><DisplayName>w</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><List><DataType><Basic>String</Basic></DataType></List></DataType>
        <Constraints><MinimalElementCount>1</MinimalElementCount><MaximalElementCount>3</MaximalElementCount></Constraints>
      </Constrained></DataType></Parameter>
    <Parameter><Identifier>Target</Identifier><DisplayName>t</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><Basic>String</Basic></DataType>
        <Constraints><FullyQualifiedIdentifier>FeatureIdentifier</FullyQualifiedIdentifier></Constraints>
      </Constrained></DataType></Parameter>
    <Parameter><Identifier>Payload</Identifier><DisplayName>p</DisplayName><Description>d</Description>
      <DataType><Basic>Any</Basic></DataType></Parameter>
  </Command>
</Feature>"""

GOOD = {
    "Name": "Plate", "Mode": "Fast", "Count": 5, "Enabled": True, "Blob": base64.b64encode(b"ab").decode(),
    "Day": "2026-09-25", "At": "2026-09-25T10:00:00Z", "Clock": "12:30:00+02:00", "Wells": ["A1", "B2"],
    "Target": "org.silastandard/core/SiLAService/v1", "Payload": {"type": "Real", "value": 1.5},
}


# ------------------------------------------------------------------ FDL parsing / validation


def test_parse_simulator_features():
    tc = parse_feature(TEMPERATURE_CONTROLLER_FDL)
    assert tc.fully_qualified_identifier == "org.labmcp/simulation/TemperatureController/v1"
    assert tc.commands["ControlTemperature"].observable
    assert not tc.commands["SetRampRate"].observable
    assert tc.properties["CurrentTemperature"].observable
    cancel = parse_feature(CANCEL_CONTROLLER_FDL)
    assert cancel.fully_qualified_identifier == "org.silastandard/core.commands/CancelController/v1"


def test_json_schema_rendering_has_units_and_limits():
    tc = parse_feature(TEMPERATURE_CONTROLLER_FDL)
    schema = to_json_schema(tc.commands["ControlTemperature"].parameters[0].type, tc)
    assert schema["type"] == "number" and schema["minimum"] == -20 and schema["maximum"] == 120
    assert schema["x-unit"] == "degC" and "273.15" in schema["x-unit-si"]
    state = to_json_schema(tc.properties["DeviceState"].type, tc)
    assert state["enum"] == ["Idle", "Controlling", "RunningProgram", "Off"]
    zoo = parse_feature(TYPES_FDL)
    wells = to_json_schema(zoo.commands["Everything"].parameters[8].type, zoo)
    assert wells == {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}


def test_all_types_convert():
    zoo = parse_feature(TYPES_FDL)
    native, warnings = convert_parameters(zoo, zoo.commands["Everything"], dict(GOOD))
    assert native["Blob"] == b"ab"
    assert native["Day"].date.isoformat() == "2026-09-25"
    assert native["At"].tzinfo is not None and native["Clock"].utcoffset().total_seconds() == 7200
    assert native["Payload"].value == 1.5
    assert warnings == []


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("Name", "p", "at least 2"),
        ("Name", "plate", "pattern"),
        ("Mode", "Medium", "allowed values"),
        ("Count", 0, "> 0"),
        ("Count", 100, "< 100"),
        ("Count", 2.5, "integer"),
        ("Count", True, "integer"),
        ("Enabled", "yes", "true or false"),
        ("Blob", "!!", "base64"),
        ("Blob", base64.b64encode(b"abcde").decode(), "at most 4 bytes"),
        ("Day", "2019-12-31", ">= 2020-01-01Z"),
        ("Day", "25/09/2026", "invalid date"),
        ("Wells", [], "at least 1"),
        ("Wells", ["A1", "A2", "A3", "A4"], "at most 3"),
        ("Wells", "A1", "expected a list"),
        ("Target", "not/a/fqi", "fully qualified"),
        ("Payload", {"type": "Real"}, "SiLA Any"),
    ],
)
def test_constraint_violations_are_refused(field, value, message):
    zoo = parse_feature(TYPES_FDL)
    params = dict(GOOD, **{field: value})
    with pytest.raises(FDLValidationError, match=message):
        convert_parameters(zoo, zoo.commands["Everything"], params)


def test_structures_need_exact_fields():
    tc = parse_feature(TEMPERATURE_CONTROLLER_FDL)
    steps = tc.commands["RunProgram"].parameters[0].type
    conv = Converter(tc)
    assert conv.convert([{"TargetTemperature": 30, "HoldTime": 5}], steps, "Steps") == [
        {"TargetTemperature": 30.0, "HoldTime": 5}
    ]
    with pytest.raises(FDLValidationError, match="missing"):
        conv.convert([{"TargetTemperature": 30}], steps, "Steps")
    with pytest.raises(FDLValidationError, match=r"violates constraint <= 120"):
        conv.convert([{"TargetTemperature": 300, "HoldTime": 5}], steps, "Steps")


def test_to_jsonable():
    from collections import namedtuple

    R = namedtuple("R", "A B")
    assert to_jsonable(R(b"\x01", [1.0, float("nan")])) == {"A": {"base64": "AQ==", "length": 1}, "B": [1.0, None]}


def test_address_and_allowlist_parsing():
    assert parse_address("sila://robot.lab:50052") == ("robot.lab", 50052)
    assert parse_address("10.0.0.5:50052") == ("10.0.0.5", 50052)
    assert parse_address("[fe80::1]:50052") == ("fe80::1", 50052)
    with pytest.raises(InstrumentConnectionError, match="host:port"):
        parse_address("robot.lab")
    assert parse_allowlist("Shaker.Shake, Incubator.*") == [("shaker", "shake"), ("incubator", "*")]
    assert parse_allowlist("") is None
    with pytest.raises(ValueError):
        parse_allowlist("JustAName")


def test_discovery_parses_mdns_records(monkeypatch):
    import zeroconf

    class Info:
        port = 50052
        properties = {b"server_name": b"Liquid Handler", b"version": b"2.0", b"description": b"LH", b"ca0": b"-----"}

        def parsed_addresses(self):
            return ["192.168.1.40"]

    class FakeZC:
        def get_service_info(self, type_, name, timeout=0):
            return Info()

        def close(self):
            pass

    class FakeBrowser:
        def __init__(self, zc, type_, listener):
            assert type_ == "_sila._tcp.local."
            listener.add_service(zc, type_, "6f1e0c3a-0000-4000-8000-000000000001._sila._tcp.local.")

        def cancel(self):
            pass

    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf, "ServiceBrowser", FakeBrowser)
    found = drv.discover(0.01)
    assert found == [
        {
            "server_uuid": "6f1e0c3a-0000-4000-8000-000000000001", "server_name": "Liquid Handler",
            "description": "LH", "version": "2.0", "addresses": ["192.168.1.40"], "port": 50052,
            "advertises_ca_certificate": True,
        }
    ]


# ------------------------------------------------------------------ bridge against the real in-process server


@pytest.fixture(scope="module")
def bridge():
    sim = SimulatedSilaServer().start()
    client = open_client(sim.host, sim.port, insecure=True, root_certs=None, private_key=None, cert_chain=None,
                         timeout=15)
    b = SilaBridge(client, address=f"{sim.host}:{sim.port}", on_close=sim.stop, call_timeout=10)
    yield b
    b.close()


def test_bridge_reads_features_and_properties(bridge):
    assert set(bridge.features) == {"SiLAService", "TemperatureController", "CancelController"}
    assert bridge.can_cancel
    _, state = bridge.get_property("temperaturecontroller", "DeviceState")
    assert state in {"Idle", "Off"}
    with pytest.raises(InstrumentProtocolError, match="no property"):
        bridge.get_property("TemperatureController", "Humidity")
    with pytest.raises(InstrumentProtocolError, match="does not implement"):
        bridge.feature("Shaker")


def test_defined_execution_error_is_explained(bridge):
    first = bridge.call("TemperatureController", "RunProgram", {"Steps": [{"TargetTemperature": 40, "HoldTime": 30}]})
    try:
        second = bridge.call("TemperatureController", "ControlTemperature", {"TargetTemperature": 30})
        res = bridge.result(bridge.wait(second["execution_id"], 5)["execution"].execution_id)
        assert res["success"] is False
        assert "DeviceBusy" in res["error"] and "Cancel it first" in res["error"]
    finally:
        bridge.cancel(first["execution_id"])
    assert bridge.wait(first["execution_id"], 5)["status"] == "finishedWithError"


def test_server_side_validation_error_passes_through(bridge):
    # bypass client-side validation to show the server's own ValidationError is reported
    handle = bridge.client.TemperatureController.SetRampRate
    with pytest.raises(InstrumentProtocolError, match="SiLA validation error"):
        bridge._call(lambda: handle(RampRate=99.0), "calling SetRampRate")


def test_cancel_unknown_execution(bridge):
    with pytest.raises(InstrumentProtocolError, match="InvalidCommandExecutionUUID"):
        bridge.cancel("00000000-0000-4000-8000-000000000000")
    with pytest.raises(InstrumentProtocolError, match="not a command execution UUID"):
        bridge.cancel("abc")


def test_cancel_without_cancel_controller(bridge):
    saved = dict(bridge.features)
    bridge.features.pop("CancelController")
    try:
        out = bridge.cancel(None)
        assert out["supported"] is False and "does not implement the CancelController" in out["message"]
    finally:
        bridge.features.update(saved)


# ------------------------------------------------------------------ MCP round trips


async def test_tools_via_mcp():
    async with simulated_client(server) as c:
        info = (await c.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        si = (await c.call_tool("get_server_info", {})).structured_content
        assert si["server_type"] == "TemperatureController" and si["cancellation_supported"] is True

        feats = (await c.call_tool("list_features", {})).structured_content["features"]
        assert {f["identifier"] for f in feats} == {"TemperatureController", "CancelController"}
        one = (await c.call_tool("list_features", {"feature": "TemperatureController", "include_fdl": True}))
        assert "<Identifier>TemperatureController</Identifier>" in one.structured_content["features"][0][
            "feature_definition_xml"
        ]

        temp = (await c.call_tool("get_property", {"feature": "TemperatureController", "property": "CurrentTemperature"}))
        assert temp.structured_content["type"]["x-unit"] == "degC"

        r = (
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "SetRampRate", "parameters": {"RampRate": 10}},
            )
        ).structured_content
        assert r["done"] and r["responses"] == {"PreviousRampRate": 2.0}

        started = (
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "ControlTemperature",
                 "parameters": {"TargetTemperature": 25}},
            )
        ).structured_content
        exec_id = started["execution_id"]
        assert started["observable"] and not started["done"]
        with pytest.raises(Exception, match="has not finished"):
            await c.call_tool("get_command_result", {"execution_id": exec_id})
        await asyncio.sleep(0.2)
        st = (await c.call_tool("get_command_status", {"execution_id": exec_id})).structured_content
        assert st["status"] in {"running", "finishedSuccessfully"}
        for _ in range(50):
            st = (await c.call_tool("get_command_status", {"execution_id": exec_id})).structured_content
            if st["done"]:
                break
            await asyncio.sleep(0.1)
        assert st["latest_intermediate_response"]["CurrentTemperature"] > 22
        res = (await c.call_tool("get_command_result", {"execution_id": exec_id})).structured_content
        assert res["success"] and abs(res["responses"]["FinalTemperature"] - 25) < 0.2

        waited = (
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "RunProgram", "wait_s": 10,
                 "parameters": {"Steps": [{"TargetTemperature": 24, "HoldTime": 0}, {"TargetTemperature": 23, "HoldTime": 0}]}},
            )
        ).structured_content
        assert waited["done"] and waited["responses"] == {"StepsCompleted": 2}

        series = (
            await c.call_tool(
                "subscribe_property",
                {"feature": "TemperatureController", "property": "CurrentTemperature", "duration_s": 1.0},
            )
        ).structured_content
        assert series["mode"] == "subscription" and series["n_updates"] >= 3
        polled = (
            await c.call_tool(
                "subscribe_property",
                {"feature": "TemperatureController", "property": "RampRate", "duration_s": 0.5, "poll_interval_s": 0.2},
            )
        ).structured_content
        assert polled["mode"] == "polling" and polled["stats"]["last"] == 10.0

        execs = (await c.call_tool("list_executions", {})).structured_content["result"]
        assert len(execs) == 2

        disc = (await c.call_tool("discover_servers", {"timeout_s": 1})).structured_content
        assert disc["simulated"] and disc["servers"][0]["server_name"] == "LabMCP Simulated Thermoblock"

        log = (await c.call_tool("get_command_log", {"limit": 50})).data
        assert any("SetRampRate" in e["data"] for e in log)


async def test_invalid_parameters_are_refused_before_sending():
    async with simulated_client(server) as c:
        before = len((await c.call_tool("get_command_log", {"limit": 500})).data)
        for params, message in (
            ({"RampRate": 50}, "<= 10"),
            ({"RampRate": 0}, "> 0"),
            ({"RampRate": "fast"}, "expected a number"),
            ({}, "missing"),
        ):
            with pytest.raises(Exception, match=message):
                await c.call_tool(
                    "call_command", {"feature": "TemperatureController", "command": "SetRampRate", "parameters": params}
                )
        log = (await c.call_tool("get_command_log", {"limit": 500})).data
        assert not any("SetRampRate" in e["data"] for e in log[before:])  # nothing was sent


async def test_cancel_running_command():
    async with simulated_client(server) as c:
        started = (
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "RunProgram",
                 "parameters": {"Steps": [{"TargetTemperature": 60, "HoldTime": 60}]}},
            )
        ).structured_content
        out = (await c.call_tool("cancel_command", {"execution_id": started["execution_id"]})).structured_content
        assert out["supported"] and out["status_after"] == "finishedWithError"
        res = (await c.call_tool("get_command_result", {"execution_id": started["execution_id"]})).structured_content
        assert not res["success"] and "cancelled" in res["error"]
        everything = (await c.call_tool("cancel_command", {"all_commands": True})).structured_content
        assert everything["supported"] and "CancelAll" in everything["message"]


async def test_command_allowlist():
    async with simulated_client(server, options={"command_allowlist": "TemperatureController.SetRampRate"}) as c:
        with pytest.raises(Exception, match="allow-list"):
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "ControlTemperature", "parameters": {"TargetTemperature": 30}},
            )
        ok = await c.call_tool(
            "call_command", {"feature": "TemperatureController", "command": "SetRampRate", "parameters": {"RampRate": 1}}
        )
        assert ok.structured_content["done"]
        feats = (await c.call_tool("list_features", {"feature": "TemperatureController"})).structured_content
        callable_ = {cmd["identifier"]: cmd["callable"] for cmd in feats["features"][0]["commands"]}
        assert callable_ == {"ControlTemperature": False, "RunProgram": False, "SetRampRate": True, "SwitchOff": False}


async def test_read_only_hides_commands_keeps_cancel():
    async with simulated_client(server, read_only=True) as c:
        names = await tool_names(c)
        assert {"list_features", "get_property", "subscribe_property", "get_command_status"} <= names
        assert "call_command" not in names
        assert "cancel_command" in names and "reconnect" in names


async def test_limits_refuse_long_waits():
    limits = {"max_command_wait_s": 1, "max_subscription_duration_s": 1, "max_discovery_s": 1}
    async with simulated_client(server, limits=limits) as c:
        with pytest.raises(Exception, match="max_command_wait_s"):
            await c.call_tool(
                "call_command",
                {"feature": "TemperatureController", "command": "ControlTemperature",
                 "parameters": {"TargetTemperature": 30}, "wait_s": 5},
            )
        with pytest.raises(Exception, match="max_subscription_duration_s"):
            await c.call_tool(
                "subscribe_property",
                {"feature": "TemperatureController", "property": "CurrentTemperature", "duration_s": 5},
            )
        with pytest.raises(Exception, match="max_discovery_s"):
            await c.call_tool("discover_servers", {"timeout_s": 5})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_command_wait_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_command_wait_s", 2)
    server.configure(simulate=True, limits={})


def test_unreachable_server_is_explained():
    server.configure(simulate=False, address="127.0.0.1:1", options={"insecure": "true", "connect_timeout_s": "3"})
    info = server._connection_info()
    assert info["connected"] is False and "SiLA server" in info["error"]
    server.configure(simulate=True, address="", options={})


LIST_PROBE_FDL = """<?xml version="1.0" encoding="utf-8" ?>
<Feature SiLA2Version="1.0" FeatureVersion="1.0" Originator="org.example" Category="tests"
         xmlns="http://www.sila-standard.org">
  <Identifier>ListProbe</Identifier><DisplayName>List Probe</DisplayName><Description>d</Description>
  <Command><Identifier>Collect</Identifier><DisplayName>Collect</DisplayName><Description>d</Description>
    <Observable>No</Observable>
    <Response><Identifier>Items</Identifier><DisplayName>i</DisplayName><Description>d</Description>
      <DataType><Constrained><DataType><List><DataType><Basic>String</Basic></DataType></List></DataType>
      <Constraints><MaximalElementCount>3</MaximalElementCount></Constraints></Constrained></DataType></Response>
  </Command>
</Feature>"""


def test_undecodable_response_warns_command_was_executed():
    """sila2 <= 0.14 cannot decode top-level Constrained List responses: make sure the user learns
    that the command ran anyway (so it is not blindly repeated)."""
    import socket

    from sila2.framework import Feature
    from sila2.server import FeatureImplementationBase, SilaServer

    calls = []

    class Impl(FeatureImplementationBase):
        def Collect(self, *, metadata):
            calls.append(1)
            return ["A1"]

    srv = SilaServer("probe", "Probe", "d", "1.0", "https://example.org")
    srv.set_feature_implementation(Feature(LIST_PROBE_FDL), Impl(srv))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv.start_insecure("127.0.0.1", port, enable_discovery=False)
    try:
        client = open_client("127.0.0.1", port, insecure=True, root_certs=None, private_key=None, cert_chain=None,
                             timeout=10)
        b = SilaBridge(client, address=f"127.0.0.1:{port}")
        try:
            with pytest.raises(InstrumentProtocolError, match="WAS EXECUTED"):
                b.call("ListProbe", "Collect", {})
            assert calls == [1]
        finally:
            b.close()
    finally:
        srv.stop(0.2)
