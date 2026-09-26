import asyncio
import csv
import math
import threading
import time

import pytest
from labmcp import InstrumentError, InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_palmsens.driver import (
    MethodScriptDevice,
    build_script,
    decode_value,
    encode_literal,
    parse_package,
    script_potentials,
    select_pgstat_mode,
)
from labmcp_palmsens.server import server
from labmcp_palmsens.simulator import MethodScriptSimulator

FAST = {"sim_speed": "1000"}


def make_device(speed: float = 1000.0) -> MethodScriptDevice:
    t = SimulatedTransport(
        MethodScriptSimulator(speed=speed), read_termination="\n", write_termination="\n"
    )
    dev = MethodScriptDevice(t)
    dev.identify()
    return dev


def cv_script(n_scans: int = 1, rate: str = "100m") -> list[str]:
    loop = f"meas_loop_cv p c -200m 600m -200m 5m {rate}" + (f" nscans({n_scans})" if n_scans > 1 else "")
    return build_script(
        loop,
        begin_potential_v=-0.2,
        e_min=-0.2,
        e_max=0.6,
        pgstat_mode=2,
        bandwidth_hz=60,
        current_range_a=1e-4,
        autorange=True,
        equilibration_s=0,
    )


# ------------------------------------------------------------------ encoding (manual examples)


def test_decode_values_from_the_manual():
    assert decode_value("8000800u") == pytest.approx(2048e-6)
    assert decode_value("800000Am") == pytest.approx(0.01)
    assert decode_value("7FFFFF6m") == pytest.approx(-0.01)
    assert decode_value("DF5CB18n") == pytest.approx(0.099994392)
    assert math.isnan(decode_value("     nan"))


def test_parse_package_example_from_the_manual():
    da, ba = parse_package("Pda8000800u;ba8000800u,10,20B")
    assert (da.type, da.value) == ("da", pytest.approx(2.048e-3))
    assert (ba.type, ba.value, ba.status, ba.range) == ("ba", pytest.approx(2.048e-3), 0, 0x0B)


def test_encode_literals():
    assert encode_literal(0.5) == "500m"
    assert encode_literal(-0.5) == "-500m"
    assert encode_literal(1e-4) == "100u"
    assert encode_literal(2) == "2"
    assert encode_literal(1200) == "1200"
    assert encode_literal(0) == "0"
    assert encode_literal(0.0123456) == "12345600n"


def test_pgstat_mode_selection():
    assert select_pgstat_mode("EmStat Pico", -0.5, 0.5, 60) == 2  # low speed
    assert select_pgstat_mode("EmStat Pico", -0.5, 1.9, 60) == 4  # 2.4 V window: max range
    assert select_pgstat_mode("EmStat Pico", -0.5, 0.5, 1200) == 3  # fast: high speed
    with pytest.raises(ValueError, match="cannot apply"):
        select_pgstat_mode("EmStat Pico", -0.8, 0.8, 1200)
    assert select_pgstat_mode("EmStat4 HR", -4, 4, 1e4) == 2


def test_script_potentials_follow_store_var():
    script = [
        "var v1",
        "store_var v1 1500m ja",
        "set_e -200m",
        "meas_loop_cv p c -200m v1 -1 10m 100m",
        "meas_loop_dpv p c 0 1 5m 50m 10m 10m",
    ]
    values = sorted(v for _, v in script_potentials(script))
    assert values == pytest.approx([-1.0, -0.2, -0.2, 0.0, 0.05, 1.0, 1.05, 1.5])


# ------------------------------------------------------------------ driver vs simulator


def test_identify():
    dev = make_device()
    assert dev.info["model"] == "EmStat Pico"
    assert dev.info["firmware"] == "1.6.00"
    assert dev.info["serial"] == "ESPSIM01"


def test_parse_error_names_the_line():
    dev = make_device()
    with pytest.raises(InstrumentProtocolError, match=r"line 2.*'wrong_cmd 1'.*0x4001"):
        dev.execute(["var i", "wrong_cmd 1"], timeout_s=5)


def test_runtime_error_is_reported():
    dev = make_device()
    script = [
        "var p",
        "var c",
        "meas_loop_ca p c 100m 100m 1",
        "pck_start",
        "pck_add p",
        "pck_add c",
        "pck_end",
        "endloop",
    ]
    result = dev.execute(script, timeout_s=5)
    assert result.error and "0x4027" in result.error  # cell_on missing


def test_cv_duck_curve_has_reversible_peaks():
    dev = make_device()
    result = dev.execute(cv_script(n_scans=2), timeout_s=10)
    assert result.error is None and not result.aborted
    assert {scan for scan, _ in result.packets} == {0, 1}
    assert result.lines[0] == "M0005" and result.lines[1] == "C0000" and result.lines[-1] == "*"
    scan0 = [{v.type: v.value for v in pk} for scan, pk in result.packets if scan == 0]
    hi = max(scan0, key=lambda d: d["ba"])
    lo = min(scan0, key=lambda d: d["ba"])
    assert 0.2 < hi["da"] < 0.28 and 0.12 < lo["da"] < 0.2  # around E0 = 0.20 V
    assert 1e-5 < hi["ba"] < 2e-5  # Randles-Sevcik: ~16 µA for 1 mM, 3 mm disk, 100 mV/s
    assert lo["ba"] < 0


def test_abort_stops_a_running_script_and_cell_off_runs():
    dev = make_device(speed=10.0)
    script = build_script(
        "meas_loop_ca p c 300m 100m 30",
        begin_potential_v=0.3,
        e_min=0.3,
        e_max=0.3,
        pgstat_mode=2,
        bandwidth_hz=60,
        current_range_a=1e-4,
        autorange=True,
        equilibration_s=0,
        with_timer=True,
    )
    box = {}
    worker = threading.Thread(target=lambda: box.update(result=dev.execute(script, timeout_s=60)))
    worker.start()
    time.sleep(0.3)
    with pytest.raises(InstrumentError, match="already running"):
        dev.execute(["var x"], timeout_s=5)
    info = dev.abort()
    worker.join(10)
    result = box["result"]
    assert info["was_running"] is True and result.aborted
    assert 0 < len(result.packets) < 300
    assert result.lines[-1] == "*"


def test_abort_when_idle_switches_cell_off():
    dev = make_device()
    info = dev.abort()
    assert info == {"was_running": False, "stopped": True, "note": "cell switched off"}


# ------------------------------------------------------------------ MCP round trip


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server, options=FAST) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True and info["instrument"]["model"] == "EmStat Pico"

        dev = (await client.call_tool("get_device_info", {})).data
        assert dev["potential_range_v"] == [-1.7, 2.0]

        out = tmp_path / "cv.csv"
        cv = (
            await client.call_tool(
                "run_cyclic_voltammetry",
                {
                    "begin_potential_v": -0.2,
                    "vertex1_potential_v": 0.6,
                    "vertex2_potential_v": -0.2,
                    "step_potential_v": 0.005,
                    "scan_rate_v_s": 0.1,
                    "max_points": 50,
                    "save_path": str(out),
                },
            )
        ).structured_content
        assert cv["simulated"] is True and cv["n_points"] == 321 and cv["returned_points"] <= 51
        peaks = cv["cv_peaks"][0]
        assert 0.04 < peaks["peak_separation_v"] < 0.1
        assert peaks["midpoint_potential_v"] == pytest.approx(0.2, abs=0.02)
        with out.open() as fh:
            rows = list(csv.reader(fh))
        assert (
            rows[0] == ["index", "scan", "time_s", "potential_v", "current_a", "status"] and len(rows) == 322
        )

        lsv = (
            await client.call_tool(
                "run_linear_sweep_voltammetry", {"begin_potential_v": -0.1, "end_potential_v": 0.5}
            )
        ).structured_content
        assert lsv["summary"]["max_current_potential_v"] > 0.2

        dpv = (
            await client.call_tool(
                "run_differential_pulse_voltammetry", {"begin_potential_v": -0.1, "end_potential_v": 0.5}
            )
        ).structured_content
        assert dpv["summary"]["peak_potential_v"] == pytest.approx(0.1875, abs=0.01)

        ca = (
            await client.call_tool(
                "run_chronoamperometry", {"potential_v": 0.5, "run_time_s": 2, "interval_s": 0.1}
            )
        ).structured_content
        assert ca["n_points"] == 20 and ca["data"][0]["time_s"] == pytest.approx(0.1)
        assert ca["summary"]["first_current_a"] > ca["summary"]["last_current_a"] > 0  # Cottrell decay
        assert ca["summary"]["charge_c"] > 0

        raw = (
            await client.call_tool(
                "run_methodscript",
                {
                    "script": 'e\nvar i\nstore_var i 5i ja\nsend_string "hello"\npck_start\npck_add i\npck_end\n'
                },
            )
        ).structured_content
        assert raw["texts"] == ["hello"] and raw["packages"] == [{"ja": 5.0}]

        aborted = (await client.call_tool("abort_measurement", {})).data
        assert aborted["was_running"] is False


async def test_abort_during_measurement_via_mcp():
    async with simulated_client(server, options={"sim_speed": "20"}) as client:

        async def later_abort():
            await asyncio.sleep(0.5)
            return (await client.call_tool("abort_measurement", {})).data

        cv_task = client.call_tool(
            "run_cyclic_voltammetry",
            {
                "begin_potential_v": 0,
                "vertex1_potential_v": 0.5,
                "vertex2_potential_v": -0.5,
                "scan_rate_v_s": 0.05,
                "n_scans": 3,
            },
        )
        cv, abort = await asyncio.gather(cv_task, later_abort())
        assert abort["was_running"] is True
        assert cv.structured_content["aborted"] is True
        assert "aborted" in " ".join(cv.structured_content["warnings"])


async def test_read_only_hides_measurements():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert "get_device_info" in names and "abort_measurement" in names
        assert (
            not {
                "run_cyclic_voltammetry",
                "run_linear_sweep_voltammetry",
                "run_chronoamperometry",
                "run_differential_pulse_voltammetry",
                "run_methodscript",
            }
            & names
        )


async def test_potential_limit_refuses():
    async with simulated_client(server, options=FAST, limits={"max_potential_v": 0.5}) as client:
        with pytest.raises(Exception, match="max_potential_v"):
            await client.call_tool(
                "run_cyclic_voltammetry",
                {"begin_potential_v": 0, "vertex1_potential_v": 0.6, "vertex2_potential_v": -0.2},
            )
        with pytest.raises(Exception, match="max_potential_v"):
            await client.call_tool(
                "run_methodscript", {"script": "var e\nstore_var e 800m ja\nset_e e\ncell_on"}
            )


async def test_current_range_limit_refuses():
    async with simulated_client(server, options=FAST, limits={"max_current_range_a": 1e-5}) as client:
        with pytest.raises(Exception, match="max_current_range_a"):
            await client.call_tool(
                "run_linear_sweep_voltammetry",
                {"begin_potential_v": 0, "end_potential_v": 0.5, "current_range_a": 1e-4},
            )


async def test_duration_limit_refuses():
    async with simulated_client(server, options=FAST, limits={"max_duration_s": 5}) as client:
        with pytest.raises(Exception, match="max_duration_s"):
            await client.call_tool("run_chronoamperometry", {"potential_v": 0.1, "run_time_s": 10})


async def test_dpv_scan_rate_constraint():
    async with simulated_client(server, options=FAST) as client:
        with pytest.raises(Exception, match="step / pulse time / 2"):
            await client.call_tool(
                "run_differential_pulse_voltammetry",
                {"begin_potential_v": 0, "end_potential_v": 0.5, "scan_rate_v_s": 0.2},
            )


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_potential_v": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_potential_v", 1.5)


# ------------------------------------------------------------------ regressions (vendor-doc review)


def test_runtime_error_switches_cell_off():
    # MethodSCRIPT manual 10.1: on_finished: is NOT executed after a script error, so the driver
    # must switch the cell off itself.
    sim = MethodScriptSimulator(speed=1000.0)
    dev = MethodScriptDevice(SimulatedTransport(sim, read_termination="\n", write_termination="\n"))
    dev.identify()
    result = dev.execute(["set_e 0", "cell_on", "set_e 3", "on_finished:", "cell_off"], timeout_s=5)
    assert result.error and "0x000F" in result.error
    assert result.cell_off_sent is True
    assert sim.cell_powered is False


def test_script_potentials_cover_more_techniques():
    script = [
        "meas_loop_swv p c f r -500m 500m 10m 100m 10",
        "meas_loop_npv p c -300m 700m 10m 5m 100m",
        "meas_loop_eis f zr zi 10m 100k 100 11i 1500m",
        "meas_fast_cv p c n 0 800m -900m 10m 1",
        "meas_loop_pad p c 500m 1200m 10m 50m 2",
    ]
    values = {round(v, 6) for _, v in script_potentials(script)}
    assert {-0.5, 0.5, -0.7, 0.7, -0.3, 1.5, 0.0, 0.8, -0.9, 1.2} <= values  # SWV ends +- 2 x amplitude


async def test_raw_swv_script_potential_limit_refuses():
    async with simulated_client(server, options=FAST, limits={"max_potential_v": 1.0}) as client:
        with pytest.raises(Exception, match="max_potential_v"):
            await client.call_tool(
                "run_methodscript",
                {"script": "var p\nvar c\nvar f\nvar r\ncell_on\nmeas_loop_swv p c f r 0 5 10m 20m 10\nendloop"},
            )
