import csv
import struct
import time

import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_srs_rga.analysis import identify_gases
from labmcp_srs_rga.driver import RGAError, SrsRga, decode_bits, decode_current
from labmcp_srs_rga.server import server
from labmcp_srs_rga.simulator import RGASimulator


def make_driver(timeout: float = 0.3, **kwargs) -> tuple[SrsRga, RGASimulator]:
    sim = RGASimulator(**kwargs)
    t = SimulatedTransport(sim, read_termination="\r", write_termination="\r", timeout=timeout)
    rga = SrsRga(t)
    rga.identify()
    return rga, sim


def sim_of() -> RGASimulator:
    return server.driver.t.simulator


# ---------------------------------------------------------------- wire format


def test_wire_format_ascii_status_binary_and_silence():
    sim = RGASimulator()
    assert sim.handle_bytes(b"ID?\r") == b"SRSRGA200VER0.51SN19045\n\r"
    assert sim.handle_bytes(b"IN0\r") == b"0\n\r"  # STATUS byte, ASCII, LF CR
    assert sim.handle_bytes(b"NF3\r") == b""  # parameter setting: no reply
    assert sim.handle_bytes(b"nf?\r") == b"3\n\r"  # case-insensitive
    assert len(sim.handle_bytes(b"MR28\r")) == 4  # binary ion current, no terminator
    assert len(sim.handle_bytes(b"TP?\r")) == 4
    assert sim.handle_bytes(b"MR0\r") == b""
    assert sim.handle_bytes(b"\r\n\r") == b""  # lone CR/LF ignored


def test_decode_current_including_negative():
    assert decode_current(struct.pack("<i", 123456)) == 123456
    assert decode_current(b"\xff\xff\xff\xff") == -1
    assert decode_current(struct.pack("<i", -2_000_000)) == -2_000_000
    with pytest.raises(InstrumentProtocolError):
        decode_current(b"\x00\x01")


def test_single_mass_negative_current_through_driver():
    rga, sim = make_driver()
    sim._peak_locked = lambda mass: -5.0e-14  # baseline noise below zero
    raw = rga.single_mass(5)
    assert raw == -500
    assert SrsRga.current_a(raw) == pytest.approx(-5e-14)
    assert SrsRga.to_torr(raw, 0.1) == pytest.approx(-5e-10)


def test_identify_parses_id_string():
    rga, _ = make_driver(model=300, serial="12345", firmware="0.24")
    info = rga.identify()
    assert info["model"] == "RGA300" and info["serial"] == "12345" and info["firmware"] == "0.24"
    assert rga.m_max == 300


# ---------------------------------------------------------------- command families


def test_ionizer_commands_return_status_and_query_values():
    rga, sim = make_driver()
    assert rga.set_electron_energy(40) == 0 and rga.electron_energy_ev() == 40
    assert rga.set_ion_energy(False) == 0 and not rga.ion_energy_high()
    assert rga.set_focus_voltage(120) == 0 and rga.focus_voltage_v() == 120
    assert rga.set_emission(1.0) == 0
    assert rga.emission_ma() == pytest.approx(1.0, abs=0.02)
    assert rga.set_emission(0) == 0 and rga.emission_ma() == 0
    assert sim.log[-2:] == ["FL0", "FL?"]
    assert rga.initialize(1) == 0 and rga.electron_energy_ev() == 70


def test_detector_cdem_disables_total_pressure():
    rga, sim = make_driver()
    rga.set_emission(1.0)
    assert rga.total_pressure_raw() > 0
    assert rga.has_cdem()
    assert rga.set_cdem_voltage(1400) == 0
    assert rga.cdem_voltage_v() == pytest.approx(1400, abs=5)
    assert rga.total_pressure_raw() == 0  # TP_Flag cleared by the CDEM
    assert rga.set_cdem_voltage(0) == 0
    assert rga.total_pressure_raw() > 0  # HV0 sets TP_Flag again
    rga.set_noise_floor(2)
    assert rga.noise_floor() == 2
    assert rga.calibrate_all() == 0


def test_scans_point_counts_and_total_pressure():
    rga, sim = make_driver()
    rga.set_emission(1.0)
    res = rga.analog_scan(10, 20, 10)
    assert len(res.currents_raw) == (20 - 10) * 10 + 1
    assert res.masses[0] == 10 and res.masses[-1] == 20
    assert res.total_raw > 0
    i18 = res.masses.index(18.0)
    assert res.currents_raw[i18] == max(res.currents_raw)  # water dominates an unbaked system
    hist = rga.histogram_scan(1, 50)
    assert len(hist.currents_raw) == 50
    assert sim.mi == 1 and sim.mf == 50


def test_mass_range_order_avoids_parameter_conflict():
    rga, sim = make_driver()
    rga.set_scan_range(1, 50)
    rga.set_scan_range(60, 80)  # MF must be raised before MI
    assert rga.scan_range() == (60, 80)
    rga.set_scan_range(2, 5)  # MI must be lowered before MF
    assert rga.scan_range() == (2, 5)
    assert sim.rs232_err == 0


def test_sensitivity_storage_queries():
    rga, _ = make_driver()
    assert rga.partial_sensitivity_ma_per_torr() == pytest.approx(0.1033)
    assert rga.total_sensitivity_ma_per_torr() == pytest.approx(0.0197)
    assert rga.cdem_stored_gain() == pytest.approx(1250)


# ---------------------------------------------------------------- error replies


def test_bad_command_is_silent_and_diagnosed_with_ec():
    rga, sim = make_driver()
    with pytest.raises(InstrumentProtocolError, match="bad command"):
        rga.query("XX?")
    assert sim.rs232_err == 0  # EC? read and cleared it


def test_bad_parameter_and_conflict_bits():
    sim = RGASimulator()
    assert sim.handle_bytes(b"EE200\r") == b""
    assert sim.handle_bytes(b"ER?\r") == b"1\n\r"
    assert sim.handle_bytes(b"EC?\r") == b"2\n\r"
    sim.handle_bytes(b"MF50\r")
    sim.handle_bytes(b"MI60\r")
    assert sim.handle_bytes(b"EC?\r") == b"64\n\r"
    assert decode_bits("RS232_ERR", 64) == ["CM6: parameter conflict"]


def test_no_cdem_option_rejects_hv():
    rga, _ = make_driver(has_cdem=False)
    assert not rga.has_cdem()
    with pytest.raises(InstrumentProtocolError, match="no electron multiplier"):
        rga.set_cdem_voltage(1400)
    assert RGASimulator(has_cdem=False).handle_bytes(b"HV1400\r") == b""  # silently rejected on the wire


def test_overpressure_trips_filament_and_cdem():
    rga, sim = make_driver()
    rga.set_emission(1.0)
    rga.set_cdem_voltage(1400)
    sim.set_pressure_scale(1e5)  # vent: ~3e-3 Torr
    status = rga.status_byte()
    assert status & 0b10
    details = rga.error_details(status)
    assert any("FL6" in m for m in details["FIL_ERR"])
    assert rga.emission_ma() == 0 and rga.cdem_voltage_v() == 0
    with pytest.raises(RGAError, match="FL6"):
        rga.set_emission(1.0)  # the real unit cannot establish emission either
    sim.set_pressure_scale(1.0)
    assert rga.set_emission(1.0) == 0  # FIL_ERR clears after a successful switch-on
    assert rga.status_byte() == 0


def test_overpressure_scenario_rises_over_time():
    sim = RGASimulator(scenario="overpressure", time_scale=1.0)
    sim.t0 -= 60  # 60 s into the scenario
    assert sim.total_pressure_torr() > 1e-4


def test_degas_blocks_commands_then_reports_status():
    rga, sim = make_driver()
    rga.set_emission(1.0)
    rga.start_degas(1)
    with pytest.raises(InstrumentProtocolError, match="degas in progress"):
        rga.status_byte()
    assert sim.hv == 0
    # jump past the end of the degas
    rga.degas.until = time.monotonic() - 1
    sim.degas_until = sim.now() - 1
    assert rga.status_byte() == 0
    assert rga.finished_degas.last_status == 0


def test_stop_degas_aborts_without_status():
    rga, sim = make_driver()
    rga.start_degas(2)
    assert sim.degas_until is not None
    assert rga.stop_degas() is True
    assert sim.degas_until is None
    assert rga.status_byte() == 0


# ---------------------------------------------------------------- analysis


def test_identify_gases_air_leak():
    base = {m: 0.0 for m in range(1, 51)}
    base.update(
        {28: 7.8e-7, 14: 5.5e-8, 32: 1.8e-7, 16: 2.0e-8, 40: 1.1e-8, 20: 1.6e-9, 18: 5e-8, 17: 1.1e-8}
    )
    assignments, diagnosis, _, caveats = identify_gases(base, 1e-11)
    formulas = [a["formula"] for a in assignments]
    assert formulas[0] == "N2" and "O2" in formulas and "Ar" in formulas
    assert any("Air leak" in d for d in diagnosis)
    assert caveats


# ---------------------------------------------------------------- MCP round trip


async def test_mcp_round_trip(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] and info["instrument"]["model"] == "RGA200"

        status = (await client.call_tool("get_status", {})).structured_content
        assert status["filament_on"] is False and status["status_byte"] == 0

        on = (
            await client.call_tool(
                "set_filament", {"emission_current_ma": 1.0, "external_pressure_torr": 3e-8}
            )
        ).structured_content
        assert on["filament_on"] and "external gauge" in on["pressure_evidence"]

        tp = (await client.call_tool("read_total_pressure", {})).structured_content
        assert 3e-8 < tp["pressure_torr"] < 3e-7

        mm = (
            await client.call_tool("measure_masses", {"masses": [2, 18, 28, 32, 40, 44]})
        ).structured_content
        p = {r["mz"]: r["partial_pressure_torr"] for r in mm["readings"]}
        assert p[18] > p[28] > p[44] > p[40]
        assert mm["warnings"] == []

        csv_path = tmp_path / "scan.csv"
        scan = (
            await client.call_tool(
                "analog_scan",
                {"start_mass": 1, "stop_mass": 50, "max_points": 100, "save_path": str(csv_path)},
            )
        ).structured_content
        assert scan["total_points"] == 491 and len(scan["mz"]) <= 100
        assert round(scan["peaks"][0]["mz"]) == 18 and scan["peaks"][0]["likely_species"] == "H2O"
        assert scan["total_pressure_torr"] > 0
        rows = list(csv.reader(csv_path.open()))
        assert len(rows) == 1 + 491 + 1

        hist = (
            await client.call_tool("histogram_scan", {"start_mass": 1, "stop_mass": 50})
        ).structured_content
        assert {round(pk["mz"]) for pk in hist["peaks"]} >= {2, 18, 28, 44}

        gases = (await client.call_tool("identify_residual_gases", {"stop_mass": 50})).structured_content
        assert gases["species"][0]["formula"] == "H2O"
        assert {"H2", "CO2"} <= {s["formula"] for s in gases["species"]}
        assert any("Water vapour dominates" in d for d in gases["diagnosis"])

        params = (
            await client.call_tool("set_scan_parameters", {"noise_floor": 6, "electron_energy_ev": 70})
        ).structured_content
        assert params["noise_floor"] == 6

        cal = (await client.call_tool("calibrate", {"kind": "zero"})).structured_content
        assert cal["status_byte"] == 0

        off = (await client.call_tool("all_off", {})).structured_content
        assert not off["filament_on"] and not off["cdem_on"]


async def test_leak_check_detects_helium_pulse():
    async with simulated_client(server, options={"scenario": "helium_leak"}) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        sim = sim_of()
        sim.time_scale = 8.0  # 3 s real = 24 s simulated: one spray at 5-9 s
        sim.t0 = time.monotonic()
        res = (
            await client.call_tool(
                "leak_check", {"duration_s": 3.0, "interval_s": 0.15, "baseline_points": 3}
            )
        ).structured_content
        assert res["leak_detected"] is True
        assert res["events"][0]["rise_factor"] > 3
        assert res["events"][0]["start_s"] > 0.4


async def test_cdem_on_with_rga_pressure_and_off():
    async with simulated_client(server) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        # No external value: the RGA measures total pressure with the Faraday cup first.
        res = (await client.call_tool("set_cdem", {"voltage_v": 1400})).structured_content
        assert res["cdem_on"] and "RGA total pressure" in res["pressure_evidence"]
        mm = (await client.call_tool("measure_masses", {"masses": [18]})).structured_content
        assert mm["detector"] == "cdem"
        with pytest.raises(Exception, match="CDEM is on"):
            await client.call_tool("read_total_pressure", {})
        off = (await client.call_tool("cdem_off", {})).structured_content
        assert not off["cdem_on"]


async def test_overpressure_reported_in_status():
    async with simulated_client(server) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        sim_of().set_pressure_scale(1e5)
        status = (await client.call_tool("get_status", {})).structured_content
        assert status["filament_on"] is False
        assert any("FL6" in m for m in status["errors"]["FIL_ERR"])


async def test_degas_via_mcp_and_filament_off_aborts_it():
    async with simulated_client(server) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        res = (await client.call_tool("degas", {"minutes": 2})).structured_content
        assert res["action"] == "degas_started"
        with pytest.raises(Exception, match="degas in progress"):
            await client.call_tool("get_status", {})
        off = (await client.call_tool("filament_off", {})).structured_content
        assert not off["filament_on"] and "aborted" in off["message"]


async def test_read_only_hides_control_and_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        for name in (
            "get_status",
            "read_total_pressure",
            "measure_masses",
            "analog_scan",
            "histogram_scan",
            "leak_check",
            "identify_residual_gases",
        ):
            assert name in names
        for name in ("set_filament", "set_cdem", "degas", "calibrate", "set_scan_parameters"):
            assert name not in names
        for name in ("filament_off", "cdem_off", "all_off", "reconnect"):
            assert name in names


# ---------------------------------------------------------------- safety limits


async def test_filament_refused_without_pressure_evidence():
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="no trustworthy pressure reading"):
            await client.call_tool("set_filament", {"emission_current_ma": 1.0})
        assert sim_of().emission_ma == 0


async def test_filament_refused_at_high_external_pressure():
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="max_filament_pressure_torr"):
            await client.call_tool("set_filament", {"external_pressure_torr": 5e-4})
        assert sim_of().emission_ma == 0
        assert "FL1.00" not in sim_of().log


async def test_filament_change_refused_when_rga_reads_high_pressure():
    async with simulated_client(server) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        sim_of().set_pressure_scale(1300)  # ~1.3e-4 Torr: above the limit, below the trip point
        with pytest.raises(Exception, match="max_filament_pressure_torr"):
            await client.call_tool("set_filament", {"emission_current_ma": 2.0})
        assert sim_of().emission_ma == 1.0


async def test_filament_limit_can_be_tightened():
    async with simulated_client(server, limits={"max_filament_pressure_torr": 1e-6}) as client:
        with pytest.raises(Exception, match="max_filament_pressure_torr"):
            await client.call_tool("set_filament", {"external_pressure_torr": 5e-6})


async def test_cdem_pressure_and_voltage_limits():
    async with simulated_client(server) as client:
        await client.call_tool("set_filament", {"external_pressure_torr": 3e-8})
        with pytest.raises(Exception, match="max_cdem_pressure_torr"):
            await client.call_tool("set_cdem", {"voltage_v": 1400, "external_pressure_torr": 2e-5})
        with pytest.raises(Exception, match="max_cdem_voltage_v"):
            await client.call_tool("set_cdem", {"voltage_v": 2200, "external_pressure_torr": 1e-8})
        with pytest.raises(Exception, match="less than or equal to 2490"):  # hardware maximum (Field)
            await client.call_tool("set_cdem", {"voltage_v": 3000, "external_pressure_torr": 1e-8})
        assert sim_of().hv == 0


async def test_degas_limits():
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="max_degas_minutes"):
            await client.call_tool("degas", {"minutes": 10, "external_pressure_torr": 1e-8})
        with pytest.raises(Exception, match="max_filament_pressure_torr"):
            await client.call_tool("degas", {"minutes": 2, "external_pressure_torr": 1e-3})
        assert sim_of().degas_until is None


async def test_scan_and_leak_check_duration_limits():
    async with simulated_client(server, limits={"max_scan_duration_s": 100}) as client:
        await client.call_tool("set_scan_parameters", {"noise_floor": 0})
        with pytest.raises(Exception, match="max_scan_duration_s"):
            await client.call_tool("analog_scan", {"start_mass": 1, "stop_mass": 200})
        with pytest.raises(Exception, match="max_leak_check_duration_s"):
            await client.call_tool("leak_check", {"duration_s": 3600})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_cdem_voltage_v": 1500})
    with pytest.raises(SafetyLimitError):
        server.check("max_cdem_voltage_v", 1600)
    server.configure(simulate=True, limits={})
