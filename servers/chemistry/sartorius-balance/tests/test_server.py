import labmcp_sartorius.server as server_module
import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_sartorius.driver import BalanceBusy, SBIBalance, parse_sbi_line
from labmcp_sartorius.server import server
from labmcp_sartorius.simulator import SBISimulator


@pytest.fixture(autouse=True)
def _default_options():
    server.configure(options={})  # --option values persist between configure() calls
    yield
    server.configure(options={})


class RecordingSimulator(SBISimulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.received: list[str] = []

    def handle(self, command: str):
        self.received.append(command)
        return super().handle(command)


def make_driver(legacy: bool = False, **kwargs) -> tuple[SBIBalance, RecordingSimulator]:
    kwargs.setdefault("time_scale", 10.0)  # settle in 0.15 s instead of 1.5 s
    sim = RecordingSimulator(**kwargs)
    t = SimulatedTransport(sim, read_termination="\r\n", write_termination="\r\n")
    return SBIBalance(t, legacy=legacy), sim


# ---------------------------------------------------------------- SBI line parsing


@pytest.mark.parametrize(
    "line, value, unit, stable, ident",
    [
        ("+   123.56 g  ", 123.56, "g", True, ""),  # 16 characters, stable
        ("+   123.56    ", 123.56, None, False, ""),  # no unit symbol = not stable
        ("-   0.0012 g  ", -0.0012, "g", True, ""),
        ("   111.255 g  ", 111.255, "g", True, ""),  # position 1 blank = positive
        ("N     +   123.56 g  ", 123.56, "g", True, "N"),  # 22 characters with ID code
        ("G#    +  52.1873 mg ", 52.1873, "mg", True, "G#"),
        ("Qnt   +      253 pcs", 253, "pcs", True, "Qnt"),
        ("N + 123.56 g", 123.56, "g", True, "N"),  # Cubis II "one line with full length"
        ("+   123,56 g  ", 123.56, "g", True, ""),  # decimal comma
    ],
)
def test_parse_weight_lines(line, value, unit, stable, ident):
    r = parse_sbi_line(line)
    assert (r.value, r.unit, r.stable, r.ident) == (pytest.approx(value), unit, stable, ident)


@pytest.mark.parametrize(
    "line, match",
    [
        ("      High    ", "overloaded"),
        ("Stat        High    ", "overloaded"),
        ("      Low     ", "underload"),
        ("   Err 101    ", "error 101"),
        ("Stat     ERR 320    ", "error 320"),
        ("   APP.ERR    ", "application error"),
        ("garbage", "Unexpected SBI data"),
    ],
)
def test_parse_special_and_error_lines(line, match):
    with pytest.raises(InstrumentProtocolError, match=match):
        parse_sbi_line(line)


def test_calibration_status_is_busy():
    with pytest.raises(BalanceBusy):
        parse_sbi_line("Stat     Cal.Int.   ")


# ---------------------------------------------------------------- driver against the simulator


def test_commands_are_esc_sequences():
    bal, sim = make_driver()
    bal.print_reading()
    bal.tare()
    bal.zero()
    bal.set_ambient_filter("unstable")
    bal.lock_keys(True)
    assert sim.received == ["\x1bP", "\x1bU", "\x1bV", "\x1bM", "\x1bO"]
    assert bal.t.write_termination == "\r\n"


def test_stable_weight_and_unit_memory():
    bal, sim = make_driver()
    w = bal.weight(stable=True)
    assert w.value == pytest.approx(52.1873, abs=2e-4) and w.unit == "g" and w.stable
    assert w.ident == "N"
    sim.place(80.0)
    w = bal.weight(stable=False)
    assert not w.stable and w.unit is None and bal.last_unit == "g"
    w = bal.weight(stable=True, timeout=5)
    assert w.value == pytest.approx(80.0, abs=2e-4) and w.stable


def test_16_character_format():
    bal, _ = make_driver(fmt=16)
    w = bal.weight()
    assert w.ident == "" and w.unit == "g"
    assert len(bal.t.simulator._weight_line()) == 14  # 16 characters incl. CR LF


def test_tare_and_zero():
    bal, sim = make_driver(load_g=25.0)
    bal.tare()
    assert bal.weight().value == pytest.approx(0, abs=2e-4)
    sim.place(30.5)
    assert bal.weight(timeout=5).value == pytest.approx(5.5, abs=2e-4)
    sim.place(0.001)  # container removed, pan nearly empty
    bal.zero()
    assert bal.weight(timeout=5).value == pytest.approx(0, abs=2e-4)


def test_overload_raises():
    bal, sim = make_driver()
    sim.place(500)
    with pytest.raises(InstrumentProtocolError, match="overloaded"):
        bal.weight(stable=False)


def test_no_reply_gives_configuration_hint():
    bal, _ = make_driver()
    with pytest.raises(InstrumentTimeout, match="set to SBI"):
        bal._lines("Y", timeout=0.3)


def test_identify():
    bal, _ = make_driver()
    assert bal.identify() == {
        "manufacturer": "Sartorius",
        "protocol": "SBI",
        "model": "QUINTIX224-1S",
        "serial": "0037402012",
        "software": "00-20-12.01",
    }


def test_legacy_mode_uses_esc_t():
    bal, sim = make_driver(legacy=True, load_g=12.0)
    bal.tare()
    assert sim.received[-1] == "\x1bT"
    with pytest.raises(InstrumentProtocolError, match="no separate zero"):
        bal.zero()


def test_unstable_timeout():
    bal, sim = make_driver(time_scale=1.0)
    sim.place(10.0)
    with pytest.raises(InstrumentProtocolError, match="did not report a stable reading"):
        bal.weight(stable=True, timeout=0.5)


# ---------------------------------------------------------------- MCP level


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "QUINTIX224-1S"

        reading = (await client.call_tool("read_weight", {"stable": True})).structured_content
        assert reading["unit"] == "g" and reading["stable"] is True
        assert reading["value"] == pytest.approx(52.18734, abs=2e-4)

        tared = (await client.call_tool("tare", {})).structured_content
        assert tared["value"] == pytest.approx(0, abs=2e-4)

        series = (
            await client.call_tool("log_weight_series", {"count": 3, "interval_s": 0.5})
        ).structured_content
        assert series["count"] == 3 and series["unit"] == "g"

        await client.call_tool("set_ambient_conditions", {"conditions": "very_unstable"})
        await client.call_tool("lock_keypad", {"locked": True})
        sim = server.driver.t.simulator
        assert sim.ambient == "N" and sim.keys_locked

        log = (await client.call_tool("get_command_log", {"limit": 100})).data
        assert any(entry["data"].startswith("<ESC>P") for entry in log)  # control bytes are named


async def test_zero_outside_range_is_reported():
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="zero-setting range"):
            await client.call_tool("zero", {"timeout_s": 1})


async def test_internal_adjustment(monkeypatch):
    monkeypatch.setattr(server_module, "ADJUST_POLL_S", 0.05)
    async with simulated_client(server) as client:
        server.driver.t.simulator.adjust_s = 0.5
        result = (await client.call_tool("run_internal_adjustment", {"timeout_s": 30})).structured_content
        assert result["observed_adjustment"] is True
        assert result["final_reading"]["stable"] is True
        assert server.driver.t.simulator.adjustments == 1


async def test_internal_adjustment_not_supported(monkeypatch):
    monkeypatch.setattr(server_module, "ADJUST_POLL_S", 0.05)
    monkeypatch.setattr(server_module, "ADJUST_NO_SIGN_S", 0.5)
    async with simulated_client(server) as client:
        server.driver.t.simulator.internal_weight = False
        result = (await client.call_tool("run_internal_adjustment", {"timeout_s": 30})).structured_content
        assert result["observed_adjustment"] is False
        assert "No sign of an adjustment" in result["message"]


async def test_legacy_option_via_mcp():
    async with simulated_client(server, options={"legacy": "true"}) as client:
        tared = (await client.call_tool("tare", {})).structured_content  # ESC T tares a loaded pan
        assert tared["value"] == pytest.approx(0, abs=2e-4)
        with pytest.raises(Exception, match="no separate zero"):
            await client.call_tool("zero", {})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"read_weight", "log_weight_series", "reconnect"} <= names
        for hidden in ("tare", "zero", "run_internal_adjustment", "set_ambient_conditions", "lock_keypad"):
            assert hidden not in names


async def test_series_duration_limit():
    async with simulated_client(server, limits={"max_series_duration_s": 1}) as client:
        with pytest.raises(Exception, match="max_series_duration_s"):
            await client.call_tool("log_weight_series", {"count": 5, "interval_s": 1})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_series_duration_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_series_duration_s", 5)


async def test_series_longer_than_the_tool_timeout_is_refused():
    # Regression: with max_series_duration_s raised (the README suggests 3600), the 900 s tool
    # timeout cut the series off; now one call is capped below the tool timeout.
    async with simulated_client(server, limits={"max_series_duration_s": 100000}) as client:
        with pytest.raises(Exception, match="single tool call"):
            await client.call_tool("log_weight_series", {"count": 1000, "interval_s": 10})
