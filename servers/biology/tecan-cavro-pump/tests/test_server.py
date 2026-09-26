import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_cavro.driver import CavroError, CavroPump, force_for_syringe
from labmcp_cavro.server import server
from labmcp_cavro.simulator import CavroSimulator


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_pump(clock=None, speed: float = 1000.0, **pump_kwargs) -> tuple[CavroPump, CavroSimulator]:
    std = pump_kwargs.pop("standard_steps", 6000)
    sim = CavroSimulator(standard_steps=std, clock=clock, speed=speed)
    t = SimulatedTransport(sim, read_termination="\x03\r\n", write_termination="\r")
    return CavroPump(t, standard_steps=std, **pump_kwargs), sim


# ------------------------------------------------------------------ driver vs simulator


def test_answer_block_and_identity():
    pump, _ = make_pump()
    reply = pump.query_status()
    assert reply.ready and reply.error == 0 and reply.status == 0x60  # '`' = ready, no error
    info = pump.identify()
    assert info["firmware"].startswith("XLP6000")
    assert info["model"] == "Cavro XLP 6000"


def test_move_before_initialization_is_error_7():
    pump, _ = make_pump()
    with pytest.raises(CavroError, match="not initialized") as exc:
        pump.move_plunger("P", 100, 1000, timeout=5)
    assert exc.value.code == 7


def test_invalid_command_is_reported_immediately():
    pump, _ = make_pump()
    with pytest.raises(CavroError, match="invalid command"):
        pump.command("t2000R")  # manual 3.6.3 example


def test_invalid_operand_is_reported_by_q():
    pump, _ = make_pump()
    pump.initialize()
    assert pump.command("A7000R").error == 0  # manual: answer says 'no error' ...
    with pytest.raises(CavroError, match="invalid operand") as exc:  # ... and Q reports error 3
        pump.wait_ready(5)
    assert exc.value.code == 3


def test_xcalibur_error_4_is_decoded():
    # XCalibur manual 733085-B, 3.6.3: error 4 = "Invalid Command Sequence" (answered immediately).
    err = CavroError(4, "in reply to 'X'")
    assert err.code == 4 and "invalid command sequence" in str(err)
    assert "unknown error" not in str(err)


def test_initialize_aspirate_dispense_positions():
    pump, sim = make_pump()
    pump.initialize()
    assert sim.initialized and pump.plunger_steps() == 0
    pump.move_plunger("P", 3000, 6000, timeout=5)
    assert pump.plunger_steps() == 3000
    pump.move_plunger("D", 1000, 6000, timeout=5)
    assert pump.plunger_steps() == 2000


def test_busy_progress_and_terminate():
    clock = FakeClock()
    pump, sim = make_pump(clock=clock)
    pump.command("ZR")
    clock.now = 3.0
    pump.wait_ready(1)
    pump.command("V600P3000R")  # 3000 half-steps at 600 Hz = 5 s
    clock.now = 5.5
    assert not pump.query_status().ready
    assert pump.plunger_steps() == pytest.approx(1500, abs=2)
    with pytest.raises(CavroError, match="command overflow"):
        pump.command("A0R")  # moves are refused while busy
    pump.terminate()
    assert pump.query_status().ready
    clock.now = 20.0
    assert pump.plunger_steps() == pytest.approx(1500, abs=2)  # stayed where it was stopped


def test_plunger_overload_requires_reinitialization():
    pump, sim = make_pump()
    pump.initialize()
    sim.overload_next_move = True
    with pytest.raises(CavroError, match="plunger overload") as exc:
        pump.move_plunger("P", 4000, 6000, timeout=5)
    assert exc.value.code == 9
    with pytest.raises(CavroError, match="plunger overload"):
        pump.move_plunger("D", 100, 6000, timeout=5)
    pump.initialize()
    pump.move_plunger("P", 100, 6000, timeout=5)
    assert pump.plunger_steps() == 100


def test_bypass_blocks_plunger():
    pump, _ = make_pump()
    pump.initialize()
    assert pump.move_valve("B") == "b"
    with pytest.raises(CavroError, match="plunger move not allowed"):
        pump.move_plunger("P", 100, 1000, timeout=5)


def test_fine_positioning_mode_and_xcalibur_resolution():
    pump, _ = make_pump(resolution_mode=1)
    pump.initialize()
    assert pump.mode() == 1 and pump.steps_per_stroke() == 48000
    xc, _ = make_pump(model="xcalibur", standard_steps=3000)
    xc.initialize()
    assert xc.steps_per_stroke() == 3000


def test_initialization_force_follows_syringe_size():
    assert force_for_syringe(1000) == 0
    assert force_for_syringe(500) == 1
    assert force_for_syringe(100) == 2


# ------------------------------------------------------------------ MCP round trip

FAST = {"sim_speed": "500"}


async def test_tools_via_mcp():
    async with simulated_client(server, options=FAST) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True and info["instrument"]["syringe_ul"] == "1000"

        st = (await client.call_tool("initialize", {})).structured_content
        assert st["ready"] and st["plunger_position_ul"] == 0

        moved = (
            await client.call_tool("aspirate_ul", {"volume_ul": 250, "flow_ul_s": 250, "valve": "input"})
        ).structured_content
        assert moved["moved_ul"] == pytest.approx(250)
        assert moved["top_speed_pulses_s"] == 1500
        assert moved["plunger_position_ul"] == pytest.approx(250)
        assert moved["valve_position"] == "input"

        out = (
            await client.call_tool("dispense_ul", {"volume_ul": 100, "flow_ul_s": 50, "valve": "output"})
        ).structured_content
        assert out["plunger_position_ul"] == pytest.approx(150)

        st = (await client.call_tool("set_valve", {"position": "bypass"})).structured_content
        assert st["valve_position"] == "bypass"
        with pytest.raises(Exception, match="plunger move not allowed"):
            await client.call_tool("dispense_ul", {"volume_ul": 10})

        st = (await client.call_tool("terminate", {})).structured_content
        assert st["ready"] is True

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any(entry["data"] == "/1V1500P1500R" for entry in log)


async def test_overfill_and_flow_range_are_refused():
    async with simulated_client(server, options=FAST) as client:
        await client.call_tool("initialize", {})
        with pytest.raises(Exception, match="overfill"):
            await client.call_tool("aspirate_ul", {"volume_ul": 1200, "flow_ul_s": 100})
        with pytest.raises(Exception, match="outside what a 1000"):
            await client.call_tool("aspirate_ul", {"volume_ul": 100, "flow_ul_s": 0.5})


async def test_read_only_hides_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert "get_status" in names
        assert not {"initialize", "aspirate_ul", "dispense_ul", "set_valve"} & names
        assert "terminate" in names


async def test_volume_limit_refuses():
    async with simulated_client(server, limits={"max_volume_ul": 100}) as client:
        with pytest.raises(Exception, match="max_volume_ul"):
            await client.call_tool("aspirate_ul", {"volume_ul": 200})


async def test_flow_limit_refuses():
    async with simulated_client(server, limits={"max_flow_ul_s": 50}) as client:
        with pytest.raises(Exception, match="max_flow_ul_s"):
            await client.call_tool("dispense_ul", {"volume_ul": 10, "flow_ul_s": 100})


def test_hardware_requires_syringe_and_model():
    server.configure(simulate=False, address="serial:///dev/does-not-exist", options={})
    info = server._connection_info()
    assert info["connected"] is False and "syringe_ul" in info["error"]
    server.configure(simulate=False, options={"syringe_ul": "1000"})
    assert "model=xlp6000" in server._connection_info()["error"]
    server.configure(simulate=True, options={})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_flow_ul_s": 10})
    with pytest.raises(SafetyLimitError):
        server.check("max_flow_ul_s", 20)
    with pytest.raises(InstrumentProtocolError):
        raise CavroError(11, "test")
