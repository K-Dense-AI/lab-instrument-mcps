import ast
import csv
import time
from pathlib import Path

import labmcp_thermo_iapi.pythonnet_backend as pnb
import pytest
from fastmcp.exceptions import ToolError
from labmcp import InstrumentConnectionError, InstrumentProtocolError
from labmcp.testing import simulated_client, tool_names
from labmcp_thermo_iapi.backend import ParameterDescription
from labmcp_thermo_iapi.driver import OrbitrapDriver, parse_selection, summarize_scan, validate_scan_values
from labmcp_thermo_iapi.iapi_members import BCL_MEMBERS, VERIFIED_IAPI_MEMBERS
from labmcp_thermo_iapi.server import server
from labmcp_thermo_iapi.simulator import FakeOrbitrap

FAST = {"sim_speed": "25"}


def make_driver(**kwargs) -> tuple[OrbitrapDriver, FakeOrbitrap]:
    fake = FakeOrbitrap(autostart=False, **kwargs)
    return OrbitrapDriver(fake, buffer_size=200), fake


def fake() -> FakeOrbitrap:
    backend = server.driver.backend
    assert isinstance(backend, FakeOrbitrap)
    return backend


# ------------------------------------------------------------------ selection grammar


def test_parse_selection_grammar():
    assert parse_selection("").kind == "none"
    assert parse_selection("string").kind == "string"
    s = parse_selection("1-100")
    assert (s.kind, s.low, s.high) == ("int", 1, 100)
    s = parse_selection("40.0-6000.0")
    assert (s.kind, s.low, s.high) == ("float", 40.0, 6000.0)
    s = parse_selection("15000,30000,60000")
    assert s.kind == "choice" and s.choices == ("15000", "30000", "60000")


def test_validate_scan_values():
    possible = [
        ParameterDescription("OrbitrapResolution", "15000,60000,120000"),
        ParameterDescription("FirstMass", "40.0-6000.0"),
        ParameterDescription("Microscans", "1-100"),
        ParameterDescription("ActivationType", "CID,HCD"),
        ParameterDescription("IsolationWidth", "0.4-1200.0"),
    ]
    ok = validate_scan_values(
        {
            "orbitrapresolution": "60000",
            "FirstMass": "350",
            "ActivationType": "CID;HCD",
            "IsolationWidth": "1.2;2.0",
        },
        possible,
    )
    assert ok["OrbitrapResolution"] == "60000"  # canonical IAPI name
    with pytest.raises(InstrumentProtocolError) as exc:
        validate_scan_values(
            {"OrbitrapResolution": "70000", "FirstMass": "10", "Microscans": "1.5", "Bogus": "1"}, possible
        )
    msg = str(exc.value)
    assert "70000" in msg and "40 to 6000" in msg and "integer" in msg and "'Bogus'" in msg
    with pytest.raises(InstrumentProtocolError, match="PossibleParameters is empty"):
        validate_scan_values({"FirstMass": "350"}, [])


# ------------------------------------------------------------------ driver + FakeOrbitrap


def test_scan_buffering_and_ms_order_filter():
    drv, sim = make_driver()
    sim.start_acquisition(
        "duration", duration_s=600, scan_count=None, raw_file_path=None, sample_name=None, comment=None
    )
    assert sim.pump(120) == 120
    ms1 = drv.recent_scans(100, ms_order=1)
    ms2 = drv.recent_scans(100, ms_order=2)
    assert ms1 and ms2
    assert all(summarize_scan(r)["ms_order"] == 1 for r in ms1)
    ms1_numbers = {summarize_scan(r)["scan_number"] for r in ms1}
    for r in ms2:
        s = summarize_scan(r)
        assert s["precursor_mz"] is not None and 350 <= s["precursor_mz"] <= 1500
        assert s["access_id"] == -1
        assert s["injection_time_ms"] <= 22.0 + 1e-9
        if ms1_numbers and s["master_scan_number"] >= min(ms1_numbers):
            assert s["master_scan_number"] in ms1_numbers
    # realistic MS1 content: many centroids sorted by intensity, sub-5 ppm-ish m/z values
    rec = ms1[-1]
    assert rec.centroid_count > 50
    ints = [c.intensity for c in rec.centroids]
    assert ints == sorted(ints, reverse=True)
    assert len(drv.recent_scans(5)) == 5
    # ring buffer is bounded
    sim.pump(300)
    assert drv.buffered_count == 200
    drv.close()


def test_idle_instrument_repeats_ms1_and_standby_stops_scans():
    drv, sim = make_driver()
    sim.pump(10)
    assert {summarize_scan(r)["ms_order"] for r in drv.recent_scans(10)} == {1}
    sim.set_standby()
    assert sim.pump(5) == 0
    assert drv.status()["system_mode"] == "Standby"


def test_wait_for_scan_driver_timeout_is_bounded():
    drv, _ = make_driver()
    t0 = time.monotonic()
    assert drv.wait_for_scan(0.3) is None
    assert 0.25 <= time.monotonic() - t0 < 2.0


# ------------------------------------------------------------------ MCP round trips


async def test_connection_info_and_status():
    async with simulated_client(server, options=FAST) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is True and info["simulated"] is True
        assert info["instrument"]["model"] == "Orbitrap Exploris 480"
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert st["system_mode"] == "On" and st["service_connected"] and st["instrument_connected"]
        assert st["acquiring"] is False and st["api_license"] is True
        assert "SourceSprayVoltage" in st["readbacks"]


async def test_status_log_reports_vacuum():
    async with simulated_client(server, options=FAST) as client:
        await client.call_tool("wait_for_scan", {"timeout_s": 5})
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert any("Vacuum" in k for k in st["last_status_log"])
        assert st["scans_received"] >= 1


async def test_possible_parameters_tool():
    async with simulated_client(server, options={**FAST, "sim_model": "eclipse"}) as client:
        params = (await client.call_tool("get_possible_scan_parameters", {})).structured_content["result"]
        by_name = {p["name"]: p for p in params}
        assert by_name["OrbitrapResolution"]["kind"] == "choice"
        assert "500000" not in by_name["OrbitrapResolution"]["choices"]  # not in the RESOLUTIONS table
        assert by_name["FirstMass"]["kind"] == "float" and by_name["FirstMass"]["min"] == 50.0
        assert "Analyzer" in by_name  # Tribrid only
        only = (
            await client.call_tool("get_possible_scan_parameters", {"name_contains": "mass"})
        ).structured_content
        assert {p["name"] for p in only["result"]} == {"FirstMass", "LastMass", "PrecursorMass"}


async def test_get_recent_scans_filters_and_caps(tmp_path: Path):
    async with simulated_client(server, options={**FAST, "sim_speed": "60"}) as client:
        await client.call_tool("start_acquisition", {"mode": "duration", "duration_s": 600})
        res = (await client.call_tool("wait_for_scan", {"timeout_s": 20, "ms_order": 2})).structured_content
        assert res["found"] and res["scan"]["ms_order"] == 2
        out = tmp_path / "scans.csv"
        data = (
            await client.call_tool(
                "get_recent_scans",
                {
                    "count": 5,
                    "ms_order": 2,
                    "max_centroids": 3,
                    "include_header_trailer": True,
                    "save_path": str(out),
                },
            )
        ).structured_content
        assert 1 <= data["returned"] <= 5
        for s in data["scans"]:
            assert s["ms_order"] == 2 and len(s["top_centroids"]) <= 3
            assert s["header"]["MSOrder"] == "2" and "Access Id:" in s["trailer"]
        # An MS2 scan can legitimately have no centroids above the detection floor.
        with out.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == sum(s["centroid_count"] for s in data["scans"])
        assert all(r["ms_order"] == "2" for r in rows)
        await client.call_tool("stop_acquisition", {})


async def test_wait_for_scan_timeout():
    async with simulated_client(server, options=FAST) as client:
        t0 = time.monotonic()
        res = (await client.call_tool("wait_for_scan", {"timeout_s": 0.5, "ms_order": 3})).structured_content
        assert res["found"] is False and res["scan"] is None
        assert 0.45 <= time.monotonic() - t0 < 5


async def test_custom_scan_roundtrip():
    async with simulated_client(server, options=FAST) as client:
        res = (
            await client.call_tool(
                "submit_custom_scan",
                {
                    "scan_type": "MSn",
                    "precursor_mz": 652.3,
                    "isolation_width_mz": 1.2,
                    "orbitrap_resolution": 30000,
                    "max_injection_time_ms": 54,
                    "collision_energy": 28,
                    "first_mass_mz": 120,
                    "last_mass_mz": 1400,
                },
            )
        ).structured_content
        assert res["sent"] is True
        n = res["running_number"]
        assert res["values"]["OrbitrapResolution"] == "30000" and res["values"]["MaxIT"] == "54"
        assert fake().sent_custom_scans[-1]["running_number"] == n
        got = (await client.call_tool("wait_for_scan", {"timeout_s": 10, "access_id": n})).structured_content
        assert got["found"] and got["scan"]["access_id"] == n
        assert got["scan"]["precursor_mz"] == pytest.approx(652.3)


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"orbitrap_resolution": 70000}, "not one of the allowed values"),
        ({"first_mass_mz": 10, "last_mass_mz": 500}, "outside the instrument's range"),
        ({"first_mass_mz": 900, "last_mass_mz": 500}, "must be below"),
        ({"activation_type": "ETD"}, "not one of the allowed values"),  # Exploris: HCD only
        ({"extra_parameters": {"NotAParameter": "1"}}, "not a parameter this instrument accepts"),
        ({"extra_parameters": {"Microscans": "abc"}}, "not a number"),
        ({"max_injection_time_ms": 2500}, "max_injection_time_ms"),
        ({"extra_parameters": {"MaxIT": "5000"}}, "max_injection_time_ms"),
        ({}, "no scan parameters"),
    ],
)
async def test_custom_scan_validation_refuses_before_sending(args, match):
    async with simulated_client(server, options=FAST) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool("submit_custom_scan", args)
        with pytest.raises(ToolError, match=match):
            await client.call_tool("set_repeating_scan", args)
        assert fake().sent_custom_scans == []
        assert fake().sent_repeating_scans == []
        assert "SetCustomScan" not in fake().calls


async def test_custom_scan_rate_limit():
    async with simulated_client(server, options=FAST, limits={"max_custom_scans_per_minute": 3}) as client:
        for _ in range(3):
            res = await client.call_tool(
                "submit_custom_scan", {"scan_type": "Full", "orbitrap_resolution": 60000}
            )
            assert res.structured_content["sent"]
        with pytest.raises(ToolError, match="max_custom_scans_per_minute"):
            await client.call_tool("submit_custom_scan", {"scan_type": "Full"})
        assert len(fake().sent_custom_scans) == 3
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert st["custom_scans_last_minute"] == 3


async def test_repeating_scan_and_cancel():
    async with simulated_client(server, options=FAST) as client:
        res = (
            await client.call_tool(
                "set_repeating_scan",
                {
                    "scan_type": "SIM",
                    "precursor_mz": 524.26,
                    "isolation_width_mz": 10,
                    "orbitrap_resolution": 120000,
                },
            )
        ).structured_content
        n = res["running_number"]
        got = (await client.call_tool("wait_for_scan", {"timeout_s": 10, "access_id": n})).structured_content
        assert got["found"] and got["scan"]["scan_mode"] == "SIM"
        await client.call_tool("cancel_repeating_scan", {})
        assert fake().repeating is None and "CancelRepetition" in fake().calls


async def test_acquisition_lifecycle_and_limits():
    async with simulated_client(server, options=FAST, limits={"max_acquisition_duration_s": 120}) as client:
        with pytest.raises(ToolError, match="max_acquisition_duration_s"):
            await client.call_tool("start_acquisition", {"mode": "duration", "duration_s": 600})
        with pytest.raises(ToolError, match="needs duration_s"):
            await client.call_tool("start_acquisition", {"mode": "duration"})
        with pytest.raises(ToolError, match=r"\.raw"):
            await client.call_tool(
                "start_acquisition", {"mode": "until_stopped", "raw_file_path": "C:/x.txt"}
            )
        assert "StartAcquisition(duration)" not in fake().calls
        msg = (
            await client.call_tool(
                "start_acquisition",
                {
                    "mode": "duration",
                    "duration_s": 60,
                    "raw_file_path": "D:/Data/run1.raw",
                    "sample_name": "HeLa",
                },
            )
        ).data
        assert "60 s" in msg
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert (
            st["acquiring"] and st["can_pause"] and st["acquisition"]["raw_file_path"] == "D:/Data/run1.raw"
        )
        with pytest.raises(ToolError, match="already running"):
            await client.call_tool("start_acquisition", {"mode": "until_stopped"})
        await client.call_tool("pause_acquisition", {})
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert st["can_resume"] and not st["can_pause"]
        await client.call_tool("resume_acquisition", {})
        await client.call_tool("submit_custom_scan", {"scan_type": "Full"})
        msg = (await client.call_tool("stop_acquisition", {"standby": True})).data
        assert "Standby" in msg
        calls = fake().calls
        assert calls[-3:] == ["CancelCustomScan", "CancelRepetition", "SetMode(Standby)"]
        assert "CancelAcquisition" in calls and not fake().custom_queue
        st = (await client.call_tool("get_instrument_status", {})).structured_content
        assert st["system_mode"] == "Standby" and not st["acquiring"]
        with pytest.raises(ToolError, match="must be On"):
            await client.call_tool("start_acquisition", {"mode": "until_stopped"})


async def test_scan_count_acquisition_ends_by_itself():
    drv, sim = make_driver()
    sim.start_acquisition(
        "scan_count", duration_s=None, scan_count=5, raw_file_path=None, sample_name=None, comment=None
    )
    sim.pump(5)
    assert sim.acquiring
    sim.pump(1)
    assert not sim.acquiring


async def test_read_only_hides_hazards_keeps_safety():
    async with simulated_client(server, options=FAST, read_only=True) as client:
        names = await tool_names(client)
        for hazard in ("start_acquisition", "resume_acquisition", "submit_custom_scan", "set_repeating_scan"):
            assert hazard not in names
        for safety in (
            "stop_acquisition",
            "pause_acquisition",
            "cancel_custom_scans",
            "cancel_repeating_scan",
            "reconnect",
        ):
            assert safety in names
        for read in (
            "get_instrument_status",
            "get_possible_scan_parameters",
            "get_recent_scans",
            "wait_for_scan",
        ):
            assert read in names
    async with simulated_client(server, options=FAST) as client:
        assert "submit_custom_scan" in await tool_names(client)


# ------------------------------------------------------------------ pythonnet backend


async def test_pythonnet_backend_on_non_windows_gives_clear_error(monkeypatch):
    monkeypatch.setattr(pnb, "_is_windows", lambda: False)
    async with simulated_client(
        server, simulate=False, options={"instrument": "tribrid", "assembly_dir": "C:/IAPI"}
    ) as client:
        with pytest.raises(ToolError, match="Windows .NET API") as exc:
            await client.call_tool("get_instrument_status", {})
        assert "--simulate" in str(exc.value)
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["connected"] is False and "Windows" in info["error"]


def test_pythonnet_preflight_errors(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(pnb, "_is_windows", lambda: True)
    with pytest.raises(InstrumentConnectionError, match="--option instrument"):
        pnb.PythonNetBackend(family="velos", assembly_dir=str(tmp_path)).preflight()
    with pytest.raises(InstrumentConnectionError, match="not bundled") as exc:
        pnb.PythonNetBackend(family="tribrid", assembly_dir=None).preflight()
    assert "licence" in str(exc.value)
    with pytest.raises(InstrumentConnectionError, match="does not exist"):
        pnb.PythonNetBackend(family="tribrid", assembly_dir=str(tmp_path / "nope")).preflight()
    (tmp_path / "API-2.0.dll").write_bytes(b"")
    with pytest.raises(InstrumentConnectionError, match="Thermo.TNG.Factory.dll") as exc:
        pnb.PythonNetBackend(family="tribrid", assembly_dir=str(tmp_path)).preflight()
    assert "Fusion.API-2.0.dll or Fusion.API-1.0.dll" in str(exc.value)
    for name in ("Spectrum-1.0.dll", "Thermo.TNG.Factory.dll", "Fusion.API-1.0.dll"):
        (tmp_path / name).write_bytes(b"")
    dlls = pnb.PythonNetBackend(family="tribrid", assembly_dir=str(tmp_path)).preflight()
    assert [d.name for d in dlls] == [
        "API-2.0.dll",
        "Spectrum-1.0.dll",
        "Thermo.TNG.Factory.dll",
        "Fusion.API-1.0.dll",
    ]


def test_iapi_exceptions_are_translated():
    class PrivilegeNotHeldException(Exception):
        pass

    class CommunicationException(Exception):
        pass

    class InvalidOperationException(Exception):
        pass

    assert "licence" in str(pnb._translate(PrivilegeNotHeldException("no"), "place a scan"))
    assert "Tune running" in str(pnb._translate(CommunicationException("down"), "pause"))
    assert "On" in str(pnb._translate(InvalidOperationException("bad state"), "start"))


def test_copy_scan_from_net_like_objects():
    """Exercise the IAPI scan-copy path with Python stand-ins that mimic pythonnet objects."""

    class Info:
        Available = True
        Valid = True

        def __init__(self, d):
            self._d = d
            self.ItemNames = list(d)

        def TryGetValue(self, name, _out):  # pythonnet returns (ok, out_value)
            return (name in self._d, self._d.get(name))

    class KV:
        def __init__(self, k, v):
            self.Key, self.Value = k, v

    class C:
        def __init__(self, mz, i, z):
            self.Mz, self.Intensity, self.Charge = mz, i, z

    class Scan:
        Header = [KV("Scan", "42"), KV("MSOrder", "2"), KV("PrecursorMass[0]", "500.25"), KV("Flag", None)]
        Trailer = Info({"Access Id:": "7", "Ion Injection Time (ms):": "12.5"})
        StatusLog = Info({"Vacuum": "1e-10"})
        CentroidCount = 3
        Centroids = [C(200.1, 10.0, None), C(300.2, 30.0, 1), C(400.3, 20.0, 2)]

    rec = pnb.PythonNetBackend(family="tribrid", assembly_dir=None)._copy_scan(Scan())
    assert rec.header == {"Scan": "42", "MSOrder": "2", "PrecursorMass[0]": "500.25", "Flag": ""}
    assert [c.mz for c in rec.centroids] == [300.2, 400.3, 200.1]
    s = summarize_scan(rec)
    assert (s["scan_number"], s["ms_order"], s["access_id"], s["injection_time_ms"]) == (42, 2, 7, 12.5)
    assert rec.status_log == {"Vacuum": "1e-10"}


_HELPERS = {"_net": 1, "_net_set": 1, "_net_subscribe": 1, "_net_type": 0}


def test_pythonnet_backend_uses_only_verified_iapi_names():
    tree = ast.parse(Path(pnb.__file__).read_text(encoding="utf-8"))
    used: set[str] = set()
    bcl: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        name = node.func.id
        if name in _HELPERS:
            arg = node.args[_HELPERS[name]]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"line {node.lineno}: {name}() must be called with a literal IAPI name"
            )
            used.add(arg.value)
        elif name == "_bcl":
            bcl.add(node.args[0].value)
    unknown = sorted(used - set(VERIFIED_IAPI_MEMBERS))
    assert not unknown, f"IAPI names not in iapi_members.VERIFIED_IAPI_MEMBERS: {unknown}"
    unused = sorted(set(VERIFIED_IAPI_MEMBERS) - used)
    assert not unused, f"verified list has entries the backend no longer uses: {unused}"
    assert len(used) > 50
    assert bcl <= BCL_MEMBERS | {"System.Reflection.Assembly", "System.TimeSpan"}
    for url in VERIFIED_IAPI_MEMBERS.values():
        assert url.startswith(
            "https://github.com/thermofisherlsms/iapi/blob/c246dcc8772d03c9c32e9b2fde486e97572c8fbf/"
        )
