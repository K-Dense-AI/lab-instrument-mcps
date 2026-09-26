import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_mettler_toledo.driver import MTSICSBalance
from labmcp_mettler_toledo.server import server
from labmcp_mettler_toledo.simulator import MTSICSSimulator


def make_driver(**kwargs) -> tuple[MTSICSBalance, MTSICSSimulator]:
    sim = MTSICSSimulator(**kwargs)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination="\r\n")
    return MTSICSBalance(t), sim


def test_parse_stable_weight():
    bal, _ = make_driver(load_g=100.0)
    w = bal.weight()
    assert w.value == pytest.approx(100.0)
    assert w.unit == "g"
    assert w.stable


def test_tare_then_net_is_zero():
    bal, sim = make_driver(load_g=25.0)
    tare = bal.tare()
    assert tare.value == pytest.approx(25.0)
    sim.load_g = 30.5
    assert bal.weight().value == pytest.approx(5.5)
    bal.clear_tare()
    assert bal.weight().value == pytest.approx(30.5)


def test_overload_raises_helpful_error():
    bal, _ = make_driver(load_g=500.0)
    with pytest.raises(InstrumentProtocolError, match="overload"):
        bal.weight()


def test_unknown_command_raises_syntax_error():
    bal, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="syntax error"):
        bal.command("NOPE")


def test_identify():
    bal, _ = make_driver()
    info = bal.identify()
    assert info["serial"] == "B123456789"
    assert info["model"] == "XS205DU"


def test_temperature_multiline():
    bal, _ = make_driver()
    assert bal.temperature_c() == [21.8, 22.1]


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True
        assert info["connected"] is True

        reading = (await client.call_tool("read_weight", {"stable": True})).structured_content
        assert reading["unit"] == "g"
        assert reading["value"] == pytest.approx(52.18734, abs=1e-4)

        await client.call_tool("tare", {})
        reading = (await client.call_tool("read_weight", {})).structured_content
        assert reading["value"] == pytest.approx(0.0, abs=1e-4)

        series = (await client.call_tool("log_weight_series", {"count": 3, "interval_s": 0.1})).structured_content
        assert series["count"] == 3

        log = (await client.call_tool("get_command_log", {"limit": 50})).data
        assert any(entry["data"] == "S" for entry in log)


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert "read_weight" in names
        assert "tare" not in names
        assert "set_draft_shield" not in names
        assert "reconnect" in names  # safety tools stay available
        assert "reset_balance" in names


async def test_series_duration_limit():
    async with simulated_client(server, limits={"max_series_duration_s": 1}) as client:
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_weight_series", {"count": 5, "interval_s": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_series_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_series_duration_s", 5)


def test_preset_tare_sends_plain_decimal_without_precision_loss():
    # Regression: the value was formatted with ":g", which sent "52.1873" for 52.18734 and
    # "1e-05" for 0.00001 (exponent notation is not a valid MT-SICS number).
    bal, sim = make_driver()
    sent: list[str] = []
    orig = sim.handle
    sim.handle = lambda cmd: (sent.append(cmd), orig(cmd))[1]
    assert bal.preset_tare(52.18734, "g").value == pytest.approx(52.1873, abs=1e-4)
    assert bal.preset_tare(0.00001, "g").value == pytest.approx(0.0, abs=1e-4)
    assert bal.preset_tare(100, "g").value == pytest.approx(100.0)
    assert sent == ["TA 52.18734 g", "TA 0.00001 g", "TA 100 g"]
