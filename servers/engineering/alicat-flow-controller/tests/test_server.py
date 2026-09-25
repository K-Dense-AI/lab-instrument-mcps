import time

import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_alicat.driver import AlicatDevice, parse_frame, parse_layout
from labmcp_alicat.server import server
from labmcp_alicat.simulator import AlicatSimulator


def make_device(unit_id: str = "A", **kwargs) -> tuple[AlicatDevice, AlicatSimulator]:
    sim = AlicatSimulator(unit_id=unit_id, **kwargs)
    t = SimulatedTransport(sim, read_termination="\r", write_termination="\r", timeout=0.3)
    dev = AlicatDevice(t, unit_id, timeout=0.3, idle_timeout=0.05)
    dev.initialise()
    return dev, sim


# Real replies captured on an MC-500SCCM-D (10v20.0-R24), published in alicatlib's test fixtures.
CAPTURED_TABLE = """\
A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________
A D01 700 Unit ID                    string          1
A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA
A D03 003 Flow Temp                  s decimal     7/2 002 02 `C
A D04 004 Volu Flow                  s decimal     7/2 012 02 CCM
A D05 005 Mass Flow                  s decimal     7/2 012 02 SCCM
A D06 037 Mass Flow Setpt            s decimal     7/2 012 02 SCCM
A D07 703 Gas                        string          6
A D08 701 *Error                     string          3 ADC
A D09 702 *Status                    string          3 OPL
A D10 702 *Status                    string          3 HLD""".splitlines()

LEGACY_TABLE = """\
A  D00 NAME_______ TYPE_____ MinVal_  MaxVal_  UNITS__
A  D01 Unit ID     char         A         Z         na
A  D02 Pressure    signed    +000.00  +160.00     PSIA
A  D03 Temperature signed    -010.00  +050.00        C
A  D04 Volumetric  signed    +0000.0  +0500.0      CCM
A  D05 Mass        signed    +0000.0  +0500.0     SCCM
A  D06 SetPoint    signed    +0000.0  +0500.0     SCCM
A  D07 Gas         string        Air       D2       na
A  D08 Error       string         na      ADC       na
A  D09 Status      string         na      LCK       na""".splitlines()


def test_parse_captured_layout_and_frame():
    layout = parse_layout(CAPTURED_TABLE)
    assert layout is not None and layout.source == "??D*"
    assert [f.key for f in layout.required] == [
        "unit_id", "abs_pressure", "temperature", "volumetric_flow", "mass_flow", "setpoint", "gas"
    ]
    assert layout.find("temperature").unit == "°C"
    assert layout.setpoint.unit == "SCCM" and layout.setpoint.decimals == 2
    frame = parse_frame("A +014.62 +021.89 +000.00 +000.00 +078.95     N2 HLD", layout, "A")
    assert frame.number("abs_pressure") == pytest.approx(14.62)
    assert frame.number("setpoint") == pytest.approx(78.95)
    assert frame.get("gas").value == "N2"
    assert frame.status_codes == ["HLD"]


def test_parse_legacy_layout():
    layout = parse_layout(LEGACY_TABLE)
    assert layout is not None and layout.source == "??D* (legacy)"
    assert [f.key for f in layout.required] == [
        "unit_id", "abs_pressure", "temperature", "volumetric_flow", "mass_flow", "setpoint", "gas"
    ]
    frame = parse_frame("A +014.52 +021.50 +0000.1 +0000.2 0116.3      N2", layout, "A")
    assert frame.number("setpoint") == pytest.approx(116.3)
    assert frame.get("mass_flow").unit == "SCCM"


def test_fallback_frame_without_layout():
    frame = parse_frame("A +087.59 +025.00 +164.7 +981.6 985.0 022741.4 Air HLD", None, "A", True)
    assert frame.layout_source == "fallback"
    assert frame.number("mass_flow") == pytest.approx(981.6)
    assert frame.number("setpoint") == pytest.approx(985.0)
    assert frame.number("total") == pytest.approx(22741.4)
    assert frame.status_codes == ["HLD"]


def test_identify_and_layout_from_simulator():
    dev, _ = make_device()
    info = dev.identify()
    assert info["model"] == "MC-500SCCM-D"
    assert info["serial"] == "521641"
    assert info["firmware"] == "10v20.0-R24"
    assert dev.identity.firmware == (10, 20)
    assert dev.is_controller and dev.has_gas_select
    assert dev.layout.source == "??D*"


def test_setpoint_and_first_order_response():
    dev, sim = make_device()
    reply = dev.set_setpoint(100)
    assert reply.requested == pytest.approx(100) and reply.unit == "SCCM"
    time.sleep(1.0)  # ~7 time constants
    frame = dev.poll()
    assert frame.number("mass_flow") == pytest.approx(100 + sim.zero_offset, abs=1.0)
    assert frame.number("setpoint") == pytest.approx(100)


def test_old_firmware_uses_s_command_and_legacy_layout():
    dev, sim = make_device(firmware="5v12.0-R22", layout_dialect="legacy")
    assert dev.layout.source == "??D* (legacy)"
    reply = dev.set_setpoint(50)
    assert reply.requested == pytest.approx(50)
    assert sim.setpoint == pytest.approx(50)
    got, name = dev.set_gas(1)
    assert (got, name) == (1, "Ar")


def test_device_without_d_table_falls_back():
    dev, _ = make_device(layout_dialect="none")
    assert dev.layout is None
    frame = dev.poll()
    assert frame.layout_source == "fallback"
    assert frame.get("gas").value == "N2"


def test_hold_closed_and_cancel():
    dev, sim = make_device()
    dev.set_setpoint(200)
    frame = dev.hold_closed()
    assert "HLD" in frame.status_codes
    time.sleep(0.5)
    assert abs(sim.true_flow) < 5
    frame = dev.cancel_hold()
    assert "HLD" not in frame.status_codes


def test_error_reply_raises():
    dev, _ = make_device()
    with pytest.raises(InstrumentProtocolError, match=r"replied '\?'"):
        dev.command("NOPE")


def test_meter_ignores_setpoint_commands():
    dev, _ = make_device(controller=False, model="M-500SCCM-D")
    assert not dev.is_controller
    with pytest.raises(InstrumentTimeout):
        dev.command("LS 10")


def test_gas_select_rejects_uninstalled_gas():
    dev, _ = make_device()
    assert dev.set_gas(7) == (7, "He")
    with pytest.raises(InstrumentProtocolError):
        dev.set_gas(33)  # Cl2 is not installed on a standard device


def test_integer_setpoint_shape_is_never_sent():
    dev, sim = make_device()
    with pytest.raises(ValueError):
        dev.command("49408")
    assert sim.setpoint == 0


def test_other_unit_id_on_bus():
    dev, _ = make_device(unit_id="C")
    assert dev.poll().unit_id == "C"


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "MC-500SCCM-D"

        dev_info = (await client.call_tool("get_device_info", {})).structured_content
        assert dev_info["is_controller"] is True
        assert dev_info["setpoint_full_scale"] == pytest.approx(500)
        assert dev_info["setpoint_source"].startswith("serial")

        reading = (await client.call_tool("read_flow", {})).structured_content
        assert reading["mass_flow_unit"] == "SCCM"
        assert reading["pressure_kind"] == "absolute"
        assert reading["gas"] == "N2"

        # Taring with the setpoint at 0 and no flow is allowed; it zeroes the sensor offset.
        tared = (await client.call_tool("tare_flow", {})).structured_content
        assert tared["mass_flow"] == pytest.approx(0, abs=0.5)

        result = (await client.call_tool("set_flow_setpoint", {"setpoint": 50})).structured_content
        assert result["accepted_setpoint"] == pytest.approx(50)

        # ...but taring while the setpoint is non-zero is refused.
        with pytest.raises(Exception, match="setpoint is 50"):
            await client.call_tool("tare_flow", {})

        series = (await client.call_tool("log_flow_series", {"count": 3, "interval_s": 0.05})).structured_content
        assert series["count"] == 3 and series["quantity"] == "mass_flow"

        gas = (await client.call_tool("set_gas", {"gas": "Ar"})).structured_content
        assert gas["gas_number"] == 1 and gas["gas"] == "Ar"
        gases = (await client.call_tool("list_gases", {})).structured_content["result"]
        assert {"number": 8, "name": "N2"} in gases

        held = (await client.call_tool("hold_valve", {})).structured_content
        assert held["valve_hold"] is True
        await client.call_tool("resume_control", {})

        closed = (await client.call_tool("close_valve", {})).structured_content
        assert closed["setpoint_zeroed"] is True
        assert closed["valves_held_closed"] is True
        reading = (await client.call_tool("read_flow", {})).structured_content
        assert reading["setpoint"] == 0 and reading["valve_hold"] is True

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(entry["data"] == "AHC" for entry in log)


async def test_tare_pressure_absolute_and_gauge():
    async with simulated_client(server) as client:
        frame = (await client.call_tool("tare_pressure", {"kind": "absolute"})).structured_content
        assert frame["pressure_kind"] == "absolute"
        with pytest.raises(Exception, match=r"'\?'"):
            await client.call_tool("tare_pressure", {"kind": "gauge"})  # no gauge sensor on this MFC


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"read_flow", "get_device_info", "log_flow_series", "list_gases"} <= names
        for hidden in ("set_flow_setpoint", "set_gas", "tare_flow", "tare_pressure", "hold_valve", "resume_control"):
            assert hidden not in names
        assert "close_valve" in names  # safety tools stay available
        assert "reconnect" in names


async def test_setpoint_limit():
    async with simulated_client(server, limits={"max_setpoint": 20}) as client:
        with pytest.raises(Exception, match="max_setpoint"):
            await client.call_tool("set_flow_setpoint", {"setpoint": 25})
        with pytest.raises(Exception, match="max_setpoint"):
            await client.call_tool("set_flow_setpoint", {"setpoint": -25})
        ok = (await client.call_tool("set_flow_setpoint", {"setpoint": 20})).structured_content
        assert ok["accepted_setpoint"] == pytest.approx(20)


async def test_setpoint_above_full_scale_refused():
    async with simulated_client(server, limits={"max_setpoint": 10000}) as client:
        with pytest.raises(Exception, match="full scale"):
            await client.call_tool("set_flow_setpoint", {"setpoint": 600})
        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert not any(entry["data"].startswith("ALS 600") for entry in log)


async def test_series_duration_limit():
    async with simulated_client(server, limits={"max_series_duration_s": 1}) as client:
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_flow_series", {"count": 5, "interval_s": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_setpoint": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_setpoint", 5)
    server.disconnect()


async def test_unit_id_option():
    async with simulated_client(server, options={"unit_id": "B"}) as client:
        reading = (await client.call_tool("read_flow", {})).structured_content
        assert reading["unit_id"] == "B"
    server.configure(options={})
