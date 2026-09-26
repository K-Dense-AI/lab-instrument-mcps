import csv
import threading
import time

import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_keithley_smu.driver import (
    Keithley2400,
    Keithley2450,
    Keithley2600,
    linear_levels,
    log_levels,
    open_smu,
    parse_model,
)
from labmcp_keithley_smu.server import server
from labmcp_keithley_smu.simulator import Diode, Keithley2450Simulator, make_simulator


def make_driver(dialect: str, dut: str = "resistor", **kwargs):
    sim = make_simulator(dialect, dut)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    return open_smu(t, kwargs.pop("requested", "auto"), **kwargs), sim


# ---------------------------------------------------------------- identification


def test_parse_model():
    assert parse_model("KEITHLEY INSTRUMENTS INC.,MODEL 2400,4105123,C32") == "2400"
    assert parse_model("KEITHLEY INSTRUMENTS,MODEL 2450,04096331,1.6.7c") == "2450"
    assert parse_model("Keithley Instruments, Model 2602B, 4388888, 3.0.4") == "2602B"
    with pytest.raises(InstrumentProtocolError, match="Keithley SourceMeter"):
        parse_model("RIGOL TECHNOLOGIES,DP832,DP8A1,00.01")


@pytest.mark.parametrize(
    ("dialect", "cls", "model"),
    [("2400", Keithley2400, "2400"), ("2450", Keithley2450, "2450"), ("2600", Keithley2600, "2602B")],
)
def test_auto_dialect_detection(dialect, cls, model):
    drv, _ = make_driver(dialect)
    assert isinstance(drv, cls)
    info = drv.identify()
    assert info["model"] == model and info["dialect"] == dialect
    assert float(info["max_voltage_v"]) > 0


def test_2450_in_tsp_mode_is_refused():
    sim = Keithley2450Simulator(make_simulator("2450").core)
    original = sim._dispatch
    sim._dispatch = lambda cmd: "TSP" if cmd.strip().upper() == "*LANG?" else original(cmd)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    with pytest.raises(InstrumentProtocolError, match="SCPI mode"):
        open_smu(t)


def test_2450_in_2400_emulation_uses_2400_dialect():
    sim = make_simulator("2450")
    original = sim._dispatch
    sim._dispatch = lambda cmd: "SCPI2400" if cmd.strip().upper() == "*LANG?" else original(cmd)
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    assert isinstance(open_smu(t), Keithley2400)


# ---------------------------------------------------------------- source / measure per dialect


@pytest.mark.parametrize("dialect", ["2400", "2450", "2600"])
def test_resistor_ohms_law_and_compliance(dialect):
    drv, _ = make_driver(dialect)
    drv.configure("voltage", 2.0, 10e-3)
    assert not drv.output_state()
    drv.set_output(True)
    r = drv.read("voltage")
    assert r.voltage_v == pytest.approx(2.0, abs=1e-3)
    assert r.current_a == pytest.approx(2e-3, rel=1e-3)
    assert not r.in_compliance
    # 5 V across 1 kOhm would need 5 mA: clamp at the 1 mA limit.
    drv.set_output(False)
    drv.configure("voltage", 5.0, 1e-3)
    drv.set_output(True)
    r = drv.read("voltage")
    assert r.in_compliance
    assert r.current_a == pytest.approx(1e-3, rel=1e-3)
    assert r.voltage_v == pytest.approx(1.0, rel=1e-2)
    st = drv.status()
    assert st.output_on and st.source == "voltage" and st.in_compliance
    assert st.level == pytest.approx(5.0) and st.compliance == pytest.approx(1e-3)
    drv.set_output(False)
    assert not drv.output_state()


@pytest.mark.parametrize("dialect", ["2400", "2450", "2600"])
def test_current_source_with_voltage_limit(dialect):
    drv, _ = make_driver(dialect)
    drv.configure("current", 1e-3, 5.0)
    drv.set_output(True)
    r = drv.read("current")
    assert r.voltage_v == pytest.approx(1.0, rel=1e-2)
    assert not r.in_compliance
    drv.set_level("current", 10e-3)  # needs 10 V > 5 V limit
    r = drv.read("current")
    assert r.in_compliance and r.voltage_v == pytest.approx(5.0, rel=1e-2)
    drv.set_output(False)


def test_diode_forward_and_reverse():
    drv, _ = make_driver("2600", dut="diode")
    drv.configure("voltage", 0.7, 0.1)
    drv.set_output(True)
    forward = drv.read("voltage").current_a
    drv.set_level("voltage", -1.0)
    reverse = drv.read("voltage").current_a
    drv.set_output(False)
    d = Diode()
    assert forward == pytest.approx(d.current(0.7), rel=1e-2)
    assert 1e-4 < forward < 0.1
    assert reverse == pytest.approx(-d.i_s, abs=1e-9)


def test_2400_read_with_output_off_is_error_802():
    drv, _ = make_driver("2400")
    with pytest.raises(InstrumentTimeout):
        drv.query(":READ?")
    assert any(e.startswith("+802") for e in drv.errors())
    with pytest.raises(InstrumentProtocolError, match="output is OFF"):
        drv.measure()


def test_2450_error_reply_is_reported():
    drv, _ = make_driver("2450")
    drv.write(":SOUR:VOLT:LEV 500")
    with pytest.raises(InstrumentProtocolError, match="-222"):
        drv.raise_errors("test")
    drv.write(":BOGUS 1")
    assert any("Undefined header" in e for e in drv.error_list())


def test_2600_parameter_too_big_error():
    drv, _ = make_driver("2600")
    with pytest.raises(InstrumentProtocolError, match="1101, Parameter too big"):
        drv.configure("voltage", 50.0, 1e-3)
    drv.tsp("smua.source.nonsense = 1")
    assert any(e.startswith("-286") for e in drv.error_list())


def test_2600_channel_b():
    sim = make_simulator("2600")
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n")
    drv = open_smu(t, "auto", channel="b")
    drv.configure("voltage", 1.0, 1e-2)
    drv.set_output(True)
    assert sim.cores["smub"].output and not sim.cores["smua"].output
    drv.set_output(False)


def test_remote_sense():
    for dialect in ("2400", "2450", "2600"):
        drv, _ = make_driver(dialect)
        drv.set_remote_sense(True)
        assert drv.status().remote_sense is True
        drv.set_remote_sense(False)
        assert drv.status().remote_sense is False


# ---------------------------------------------------------------- sweeps


def test_levels():
    assert linear_levels(0, 1, 5) == pytest.approx([0, 0.25, 0.5, 0.75, 1.0])
    assert log_levels(1e-3, 1, 4) == pytest.approx([1e-3, 1e-2, 1e-1, 1.0])
    with pytest.raises(InstrumentProtocolError, match="same sign"):
        log_levels(-1, 1, 3)


@pytest.mark.parametrize("dialect", ["2400", "2450", "2600"])
def test_sweep_turns_output_off_on_error(dialect):
    drv, _ = make_driver(dialect)
    drv.configure("voltage", 0.0, 10e-3)
    calls = {"n": 0}
    original = drv.read

    def flaky(source):
        calls["n"] += 1
        if calls["n"] == 3:
            raise InstrumentProtocolError("simulated failure")
        return original(source)

    drv.read = flaky
    with pytest.raises(InstrumentProtocolError, match="simulated failure"):
        drv.sweep("voltage", linear_levels(0, 1, 10))
    assert not drv.output_state()


def test_sweep_abort_from_other_thread():
    drv, _ = make_driver("2450")
    drv.configure("voltage", 0.0, 10e-3)
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("data", drv.sweep("voltage", linear_levels(0, 1, 50), delay_s=0.05))
    )
    worker.start()
    time.sleep(0.3)
    drv.request_abort()
    worker.join(timeout=10)
    data = result["data"]
    assert data.aborted and 0 < len(data.readings) < 50
    assert not drv.output_state()


def test_abort_before_output_on_keeps_output_off():
    # output_off can arrive while run_iv_sweep is still configuring; the sweep must not switch
    # the output on afterwards (the flag used to be cleared inside sweep()).
    drv, sim = make_driver("2450")
    drv.configure("voltage", 0.0, 10e-3)
    turned_on = []
    original = drv.set_output
    drv.set_output = lambda on: (turned_on.append(on), original(on))[1]
    drv.request_abort()
    data = drv.sweep("voltage", linear_levels(0, 1, 5), clear_abort=False)
    assert data.aborted and data.readings == []
    assert True not in turned_on
    assert not drv.output_state()


def test_abort_between_level_and_read_skips_the_read():
    # 2400: :READ? with the output off sends no reply (error +802). An output_off arriving after
    # a level change must end the sweep, not leave it waiting 30 s for a reply that never comes.
    drv, _ = make_driver("2400")
    drv.configure("voltage", 0.0, 10e-3)
    calls = {"n": 0}
    original = drv.set_level

    def level_then_abort(source, level):
        original(source, level)
        calls["n"] += 1
        if calls["n"] == 4:  # first-level set + 3 loop points
            drv.request_abort()

    drv.set_level = level_then_abort
    t0 = time.monotonic()
    data = drv.sweep("voltage", linear_levels(0, 1, 10), delay_s=0)
    assert time.monotonic() - t0 < 5
    assert data.aborted and len(data.readings) == 2
    assert not drv.output_state()


def test_sweep_deadline_stops_early_with_output_off():
    drv, _ = make_driver("2450")
    drv.configure("voltage", 0.0, 10e-3)
    data = drv.sweep("voltage", linear_levels(0, 1, 50), delay_s=0.05, deadline=time.monotonic() + 0.3)
    assert 0 < len(data.readings) < 50
    assert not data.aborted and "time budget" in data.stop_reason
    assert not drv.output_state()


# ---------------------------------------------------------------- MCP round trip


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        dev = (await client.call_tool("get_device_info", {})).data
        assert dev["model"] == "2450" and dev["dialect"] == "2450"

        st = (
            await client.call_tool(
                "configure_source", {"source": "voltage", "level": 1.0, "compliance": 0.01}
            )
        ).structured_content
        assert st["output_on"] is False and st["level"] == pytest.approx(1.0)
        assert st["compliance_unit"] == "A"

        with pytest.raises(Exception, match="output is OFF"):
            await client.call_tool("measure", {})

        st = (await client.call_tool("output_on", {})).structured_content
        assert st["output_on"] is True

        m = (await client.call_tool("measure", {})).structured_content
        assert m["current_a"] == pytest.approx(1e-3, rel=1e-2)
        assert m["resistance_ohm"] == pytest.approx(1000, rel=1e-2)

        st = (await client.call_tool("set_source_level", {"level": 2.0})).structured_content
        m = (await client.call_tool("measure", {})).structured_content
        assert m["voltage_v"] == pytest.approx(2.0, abs=1e-3)

        with pytest.raises(Exception, match="output is ON"):
            await client.call_tool(
                "configure_source", {"source": "voltage", "level": 0.5, "compliance": 0.01}
            )
        with pytest.raises(Exception, match="output is ON"):
            await client.call_tool("run_iv_sweep", {"start": 0, "stop": 1, "points": 5, "compliance": 0.01})

        st = (await client.call_tool("output_off", {})).structured_content
        assert st["output_on"] is False

        st = (await client.call_tool("set_4wire", {"enabled": True})).structured_content
        assert st["remote_sense"] is True

        out = tmp_path / "iv.csv"
        sweep = (
            await client.call_tool(
                "run_iv_sweep",
                {
                    "start": -1,
                    "stop": 1,
                    "points": 21,
                    "compliance": 0.01,
                    "delay_s": 0,
                    "dual": True,
                    "max_points": 10,
                    "save_path": str(out),
                },
            )
        ).structured_content
        assert sweep["points_measured"] == 41 and sweep["points_returned"] == 10
        assert sweep["output_off"] is True and sweep["compliance_points"] == 0
        assert sweep["ohmic_fit"]["resistance_ohm"] == pytest.approx(1000, rel=1e-3)
        assert sweep["ohmic_fit"]["r_squared"] > 0.9999
        with out.open() as fh:
            assert len(list(csv.reader(fh))) == 42

        status = (await client.call_tool("get_status", {})).structured_content
        assert status["output_on"] is False

        log = (await client.call_tool("get_command_log", {"limit": 500})).data
        assert any(entry["data"] == ":OUTP OFF" for entry in log)


async def test_diode_sweep_2600_with_compliance():
    async with simulated_client(server, options={"dialect": "2600", "sim_dut": "diode"}) as client:
        dev = (await client.call_tool("get_device_info", {})).data
        assert dev["dialect"] == "2600" and dev["channel"] == "smua"
        sweep = (
            await client.call_tool(
                "run_iv_sweep", {"start": 0, "stop": 1.0, "points": 11, "compliance": 0.01, "delay_s": 0}
            )
        ).structured_content
        assert sweep["output_off"] is True
        assert sweep["compliance_points"] >= 1
        assert sweep["first_compliance_level"] is not None and sweep["first_compliance_level"] > 0.5
        assert max(sweep["current_a"]) == pytest.approx(0.01, rel=1e-2)


async def test_2400_dialect_via_mcp_and_stop_on_compliance():
    async with simulated_client(server, options={"dialect": "2400"}) as client:
        sweep = (
            await client.call_tool(
                "run_iv_sweep",
                {
                    "start": 0,
                    "stop": 10,
                    "points": 11,
                    "compliance": 0.005,
                    "delay_s": 0,
                    "stop_on_compliance": True,
                },
            )
        ).structured_content
        assert sweep["points_measured"] == 7  # 6 V / 1 kOhm = 6 mA is the first point above 5 mA
        assert sweep["stop_reason"].startswith("compliance")
        assert sweep["output_off"] is True


async def test_read_only_hides_control_and_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_status", "get_device_info", "measure", "output_off", "reconnect"} <= names
        assert not names & {"configure_source", "set_source_level", "output_on", "run_iv_sweep", "set_4wire"}


@pytest.mark.parametrize(
    ("limits", "args", "match"),
    [
        ({"max_voltage_v": 5}, {"source": "voltage", "level": 6, "compliance": 1e-3}, "max_voltage_v"),
        ({"max_current_a": 0.01}, {"source": "voltage", "level": 1, "compliance": 0.02}, "max_current_a"),
        ({"max_current_a": 0.01}, {"source": "current", "level": 0.02, "compliance": 1}, "max_current_a"),
        ({"max_voltage_v": 5}, {"source": "current", "level": 1e-3, "compliance": 10}, "max_voltage_v"),
        ({"max_power_w": 0.01}, {"source": "voltage", "level": 10, "compliance": 0.01}, "max_power_w"),
    ],
)
async def test_source_limits(limits, args, match):
    async with simulated_client(server, limits=limits) as client:
        with pytest.raises(Exception, match=match):
            await client.call_tool("configure_source", args)
        status = (await client.call_tool("get_status", {})).structured_content
        assert status["level"] == 0 and status["output_on"] is False  # nothing was sent


async def test_sweep_limits():
    async with simulated_client(server, limits={"max_sweep_points": 10, "max_sweep_duration_s": 1}) as client:
        with pytest.raises(Exception, match="max_sweep_points"):
            await client.call_tool("run_iv_sweep", {"start": 0, "stop": 1, "points": 11, "compliance": 1e-3})
        with pytest.raises(Exception, match="max_sweep_points"):
            await client.call_tool(
                "run_iv_sweep", {"start": 0, "stop": 1, "points": 6, "compliance": 1e-3, "dual": True}
            )
        with pytest.raises(Exception, match="max_sweep_duration_s"):
            await client.call_tool(
                "run_iv_sweep", {"start": 0, "stop": 1, "points": 10, "compliance": 1e-3, "delay_s": 1}
            )
        with pytest.raises(Exception, match="max_voltage_v"):
            await client.call_tool("run_iv_sweep", {"start": 0, "stop": 50, "points": 5, "compliance": 1e-3})


async def test_model_maximum_and_output_on_limit_check():
    async with simulated_client(
        server, options={"dialect": "2600"}, limits={"max_voltage_v": 100, "max_power_w": 100}
    ) as client:
        with pytest.raises(Exception, match="at most 40 V"):
            await client.call_tool("configure_source", {"source": "voltage", "level": 60, "compliance": 1e-3})
    # A level set outside the server (e.g. front panel) is caught by output_on.
    async with simulated_client(server, options={"dialect": "2600"}, limits={"max_voltage_v": 5}) as client:
        server.driver.tsp("smua.source.levelv = 10")
        with pytest.raises(Exception, match="max_voltage_v"):
            await client.call_tool("output_on", {})
        assert (await client.call_tool("get_status", {})).structured_content["output_on"] is False


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_voltage_v": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_voltage_v", 5)


async def test_sweep_save_path_is_checked_before_the_output_goes_on(tmp_path):
    existing = tmp_path / "iv.csv"
    existing.write_text("keep me", encoding="utf-8")
    args = {"start": 0, "stop": 1, "points": 5, "compliance": 1e-3, "delay_s": 0}
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("run_iv_sweep", {**args, "save_path": str(existing)})
        with pytest.raises(Exception, match=r"must end in \.csv"):
            await client.call_tool("run_iv_sweep", {**args, "save_path": str(tmp_path / "iv.txt")})
        log = (await client.call_tool("get_command_log", {"limit": 500})).data
        assert not any(entry["data"] == ":OUTP ON" for entry in log)  # nothing was energised
    assert existing.read_text(encoding="utf-8") == "keep me"
    async with simulated_client(server) as client:
        nested = tmp_path / "new" / "dir" / "iv.csv"
        sweep = (
            await client.call_tool("run_iv_sweep", {**args, "save_path": str(nested)})
        ).structured_content
        assert sweep["saved_to"] == str(nested.resolve()) and nested.exists()


async def test_sweep_longer_than_the_tool_timeout_is_refused():
    async with simulated_client(server, limits={"max_sweep_duration_s": 1e6}) as client:
        with pytest.raises(Exception, match="one call can take"):
            await client.call_tool(
                "run_iv_sweep", {"start": 0, "stop": 1, "points": 100, "compliance": 1e-3, "delay_s": 30}
            )
        status = (await client.call_tool("get_status", {})).structured_content
        assert status["output_on"] is False


async def test_overflow_readings_are_flagged_not_used():
    async with simulated_client(server) as client:
        await client.call_tool("get_connection_info", {})
        drv = server.driver
        original = drv.read
        calls = {"n": 0}

        def sometimes_overflow(source):
            calls["n"] += 1
            r = original(source)
            if calls["n"] == 3:
                r.current_a = 9.9e37
            return r

        drv.read = sometimes_overflow
        sweep = (
            await client.call_tool(
                "run_iv_sweep", {"start": 0, "stop": 1, "points": 11, "compliance": 0.01, "delay_s": 0}
            )
        ).structured_content
        assert sweep["overflow_points"] == 1
        assert sweep["current_a"][2] is None
        assert sweep["current_max_a"] < 0.01 and sweep["max_abs_power_w"] < 0.01
        assert sweep["ohmic_fit"]["resistance_ohm"] == pytest.approx(1000, rel=1e-3)
        await client.call_tool("configure_source", {"source": "voltage", "level": 1.0, "compliance": 0.01})
        await client.call_tool("output_on", {})
        drv.read = lambda source: type(original(source))(voltage_v=1.0, current_a=9.9e37, in_compliance=False)
        with pytest.raises(Exception, match="overflow"):
            await client.call_tool("measure", {})
        await client.call_tool("output_off", {})
