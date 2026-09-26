import csv
import time

import numpy as np
import pytest
from labmcp import InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_ocean_spectrometer import analysis
from labmcp_ocean_spectrometer.driver import OceanSpectrometer
from labmcp_ocean_spectrometer.server import server
from labmcp_ocean_spectrometer.simulator import NONLINEARITY, FakeSpectrometer


def make_driver(
    model: str = "USB2000PLUS", source: str = "halogen", **spec_kwargs
) -> tuple[OceanSpectrometer, FakeSpectrometer]:
    spec = FakeSpectrometer(model=model, source=source, **spec_kwargs)
    return OceanSpectrometer(spec, backend="simulator"), spec


# ---------------------------------------------------------------- analysis


def test_find_peaks_gaussian_fwhm():
    x = np.linspace(400, 700, 3001)
    y = 1000 * np.exp(-0.5 * ((x - 532.1) / 4.0) ** 2) + 3000 * np.exp(-0.5 * ((x - 610.0) / 2.0) ** 2) + 50
    peaks = analysis.find_peaks(x, y, max_peaks=5)
    assert [round(p.wavelength_nm, 1) for p in peaks] == [610.0, 532.1]
    assert peaks[0].fwhm_nm == pytest.approx(2.3548 * 2.0, rel=0.01)
    assert peaks[1].fwhm_nm == pytest.approx(2.3548 * 4.0, rel=0.01)
    assert peaks[0].prominence == pytest.approx(3000, rel=0.01)


def test_boxcar_and_downsample():
    y = np.array([0.0, 0, 3, 0, 0])
    assert analysis.boxcar(y, 1) == pytest.approx([0, 1, 1, 1, 0])
    assert analysis.boxcar(y, 0) == pytest.approx(y)
    xs, ys = analysis.downsample(np.arange(10.0), np.array([1.0, np.nan] * 5), 5)
    assert xs == pytest.approx([0.5, 2.5, 4.5, 6.5, 8.5]) and ys == [1.0] * 5
    _, ys = analysis.downsample(np.arange(4.0), np.array([np.nan, np.nan, 1.0, 2.0]), 2)
    assert ys == [None, 1.5]


# ---------------------------------------------------------------- driver


def test_device_properties():
    drv, _ = make_driver()
    info = drv.identify()
    assert info["model"] == "USB2000PLUS" and info["pixels"] == "2048"
    assert drv.integration_limits_us == (1000, 655350000)
    assert drv.integration_time_ms == 10.0
    assert drv.supports_dark_correction and drv.supports_nonlinearity_correction
    assert not drv.has_tec
    assert 339 < drv.wavelengths_nm[0] < 341 and 1020 < drv.wavelengths_nm[-1] < 1030


def test_integration_time_bounds():
    drv, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="outside this spectrometer's range"):
        drv.set_integration_time_ms(0.5)
    assert drv.set_integration_time_ms(20) == 20.0


def test_first_spectrum_after_change_is_discarded():
    drv, _ = make_driver()
    s10 = drv.acquire(correct_dark_counts=True)
    drv.set_integration_time_ms(20)
    s20 = drv.acquire(correct_dark_counts=True)  # would be a 10 ms spectrum without the discard
    assert s20.counts.max() / s10.counts.max() == pytest.approx(2.0, rel=0.03)


def test_saturation_detected():
    drv, _ = make_driver()
    drv.set_integration_time_ms(100)
    s = drv.acquire()
    assert s.saturated_pixels > 100
    assert s.peak_raw_counts == drv.max_intensity


def test_corrections_match_seabreeze_formula():
    drv, _ = make_driver()
    raw = np.full(2048, 1100.0)
    raw[500] = 50000.0
    out = drv.correct(raw, dark_counts=True, nonlinearity=True)
    assert out[drv._dark_pixels].mean() == pytest.approx(0.0)
    expected = 48900.0 / np.polyval(np.poly1d(NONLINEARITY[::-1]), 48900.0)
    assert out[500] == pytest.approx(expected)
    only_nl = drv.correct(raw, dark_counts=False, nonlinearity=True)
    assert only_nl[500] == pytest.approx(expected + 1100.0)


def test_dark_correction_unavailable_without_dark_pixels():
    spec = FakeSpectrometer()
    spec.f.spectrometer._dp = []
    drv = OceanSpectrometer(spec, backend="simulator")
    with pytest.raises(InstrumentProtocolError, match="no electric dark pixels"):
        drv.acquire(correct_dark_counts=True)


def test_absorbance_and_transmittance_workflow():
    drv, spec = make_driver()
    with pytest.raises(InstrumentProtocolError, match="No a dark and a reference"):
        drv.measure_ratio("absorbance")
    drv.store_dark(scans_to_average=5)
    assert spec.light_blocked
    drv.store_reference(scans_to_average=5)
    a = drv.measure_ratio("absorbance")
    wl = drv.wavelengths_nm
    expected = spec.sample_absorbance(wl)
    band = (wl > 450) & (wl < 650)
    assert np.nanmax(np.abs(a.values[band] - expected[band])) < 0.03
    t = drv.measure_ratio("transmittance")
    k = int(np.argmin(np.abs(wl - 520)))
    assert t.values[k] == pytest.approx(100 * 10 ** -expected[k], rel=0.05)
    drv.set_integration_time_ms(12)
    with pytest.raises(InstrumentProtocolError, match="integration time changed"):
        drv.measure_ratio("absorbance")
    with pytest.raises(InstrumentProtocolError, match="Store a new dark"):
        drv.store_reference()


def test_auto_integration_converges():
    drv, _ = make_driver()
    result = drv.auto_integration_time()
    assert result["converged"]
    assert 0.70 <= result["history"][-1]["peak_fraction"] <= 0.85
    assert drv.integration_time_ms > 10


def test_auto_integration_respects_max():
    drv, spec = make_driver()
    spec.set_scene(light_blocked=True)
    result = drv.auto_integration_time(max_ms=50)
    assert not result["converged"]
    assert drv.integration_time_ms <= 50


def test_hg_ar_lines_found():
    drv, _ = make_driver(source="hg-ar")
    s = drv.acquire(scans_to_average=3, correct_dark_counts=True)
    peaks = analysis.find_peaks(s.wavelengths_nm, s.counts, max_peaks=6)
    found = [p.wavelength_nm for p in peaks]
    for line in (435.83, 546.07):
        assert min(abs(f - line) for f in found) < 0.3
    fwhm = next(p.fwhm_nm for p in peaks if abs(p.wavelength_nm - 546.07) < 0.3)
    assert fwhm == pytest.approx(1.3, abs=0.3)


def test_tec_on_qe_pro():
    drv, spec = make_driver(model="QE-PRO")
    assert drv.has_tec and len(drv.wavelengths_nm) == 1044
    spec.tec.tau = 0.05
    drv.set_tec(True, -10.0)
    time.sleep(0.5)
    assert drv.tec_temperature_c() == pytest.approx(-10.0, abs=0.5)
    drv.set_tec(False)
    no_tec, _ = make_driver()
    with pytest.raises(InstrumentProtocolError, match="no thermo-electric cooler"):
        no_tec.tec_temperature_c()


# ---------------------------------------------------------------- MCP


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["model"] == "USB2000PLUS"

        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["pixels"] == 2048 and dev["saturation_counts"] == 65535
        assert dev["integration_time_ms"] == 10

        devices = (await client.call_tool("list_spectrometers", {})).structured_content["result"]
        assert any(d["connected_to_this_server"] for d in devices)

        out = tmp_path / "spectrum.csv"
        spec = (
            await client.call_tool(
                "acquire_spectrum",
                {"scans_to_average": 3, "boxcar_half_width": 2, "max_points": 100, "save_path": str(out)},
            )
        ).structured_content
        assert spec["points_returned"] == 100 and spec["pixels"] == 2048
        assert spec["saturated"] is False and spec["peaks"]
        with out.open() as fh:
            assert len(list(csv.reader(fh))) == 2049

        auto = (await client.call_tool("auto_integration_time", {})).structured_content
        assert auto["converged"] is True

        dark = (await client.call_tool("store_dark_reference", {"scans_to_average": 5})).structured_content
        assert dark["kind"] == "dark" and dark["warnings"] == []
        ref = (await client.call_tool("store_reference", {"scans_to_average": 5})).structured_content
        assert ref["saturated_pixels"] == 0

        out = tmp_path / "absorbance.csv"
        absb = (
            await client.call_tool(
                "measure_absorbance", {"wavelengths_nm": [520, 700], "max_points": 200, "save_path": str(out)}
            )
        ).structured_content
        assert absb["units"] == "AU"
        assert absb["at_wavelengths"][0]["value"] == pytest.approx(0.82, abs=0.03)
        assert absb["at_wavelengths"][1]["value"] == pytest.approx(0.02, abs=0.02)
        assert absb["peaks"][0]["wavelength_nm"] == pytest.approx(520, abs=3)
        assert absb["peaks"][0]["fwhm_nm"] == pytest.approx(40, rel=0.15)
        with out.open() as fh:
            rows = list(csv.reader(fh))
        assert rows[0][-1] == "absorbance_au" and len(rows) == 2049

        trans = (
            await client.call_tool("measure_transmittance", {"wavelengths_nm": [520]})
        ).structured_content
        assert trans["at_wavelengths"][0]["value"] == pytest.approx(100 * 10**-0.82, rel=0.08)
        assert trans["peaks"][0]["wavelength_nm"] == pytest.approx(520, abs=3)

        peaks = (await client.call_tool("find_peaks", {"mode": "minima", "max_peaks": 3})).structured_content
        assert peaks["spectrum"] == "transmittance"
        assert peaks["peaks"][0]["wavelength_nm"] == pytest.approx(520, abs=3)

        it = (await client.call_tool("set_integration_time", {"integration_time_ms": 15})).structured_content
        assert it["integration_time_ms"] == 15 and "Re-take" in it["note"]
        with pytest.raises(Exception, match="integration time changed"):
            await client.call_tool("measure_absorbance", {})

        off = (await client.call_tool("detector_cooling_off", {})).structured_content
        assert "no TEC" in off["status"]

        log = (await client.call_tool("get_command_log", {"limit": 200})).data
        assert any("intensities()" in entry["data"] for entry in log)


async def test_dark_with_light_on_warns(monkeypatch):
    async with simulated_client(server) as client:
        await client.call_tool("get_device_info", {})
        monkeypatch.setattr(server.driver.spec, "simulate_user_action", lambda step: None)
        dark = (await client.call_tool("store_dark_reference", {})).structured_content
        assert any("contains light" in w for w in dark["warnings"])


async def test_saturation_warning_via_mcp():
    async with simulated_client(server) as client:
        await client.call_tool("set_integration_time", {"integration_time_ms": 100})
        spec = (await client.call_tool("acquire_spectrum", {})).structured_content
        assert spec["saturated"] is True
        assert any("saturated" in w for w in spec["warnings"])


async def test_hg_ar_find_peaks_via_mcp():
    async with simulated_client(server, options={"sim_source": "hg-ar"}) as client:
        result = (
            await client.call_tool(
                "find_peaks", {"max_peaks": 5, "wavelength_min_nm": 500, "wavelength_max_nm": 600}
            )
        ).structured_content
        assert result["spectrum"] == "intensity"
        assert min(abs(p["wavelength_nm"] - 546.07) for p in result["peaks"]) < 0.3


async def test_qe_pro_tec_via_mcp():
    async with simulated_client(server, options={"sim_model": "QE-PRO"}) as client:
        dev = (await client.call_tool("get_device_info", {})).structured_content
        assert dev["has_tec"] is True and dev["integration_time_min_ms"] == 8
        t = (await client.call_tool("read_detector_temperature", {})).structured_content
        assert t["temperature_c"] == pytest.approx(22, abs=1)
        on = (await client.call_tool("set_detector_cooling", {"setpoint_c": -10})).structured_content
        assert on["setpoint_c"] == -10
        off = (await client.call_tool("detector_cooling_off", {})).structured_content
        assert off["status"] == "TEC off"
        with pytest.raises(Exception, match="min_tec_setpoint_c"):
            await client.call_tool("set_detector_cooling", {"setpoint_c": -30})


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"acquire_spectrum", "measure_absorbance", "measure_transmittance", "find_peaks"} <= names
        assert {"list_spectrometers", "get_device_info", "read_detector_temperature"} <= names
        assert {"detector_cooling_off", "reconnect"} <= names
        assert not names & {
            "set_integration_time",
            "store_dark_reference",
            "store_reference",
            "auto_integration_time",
            "set_detector_cooling",
        }


async def test_integration_and_duration_limits():
    async with simulated_client(
        server, limits={"max_integration_time_ms": 50, "max_acquisition_duration_s": 0.1}
    ) as client:
        with pytest.raises(Exception, match="max_integration_time_ms"):
            await client.call_tool("set_integration_time", {"integration_time_ms": 100})
        with pytest.raises(Exception, match="max_acquisition_duration_s"):
            await client.call_tool("acquire_spectrum", {"scans_to_average": 20})
        auto = (
            await client.call_tool(
                "auto_integration_time", {"target_min_percent": 90, "target_max_percent": 95}
            )
        ).structured_content
        assert auto["integration_time_ms"] <= 50


def test_limit_error_type():
    server.configure(simulate=True, limits={"min_tec_setpoint_c": -5})
    with pytest.raises(SafetyLimitError):
        server.check("min_tec_setpoint_c", -10)


def test_open_seabreeze_device_selection(monkeypatch):
    from types import SimpleNamespace

    from labmcp import InstrumentConnectionError
    from labmcp_ocean_spectrometer import driver as drv_mod
    from labmcp_ocean_spectrometer.simulator import FakeDevice

    devices: list = []
    opened = []
    fake_sb = SimpleNamespace(
        list_devices=lambda: devices,
        Spectrometer=SimpleNamespace(from_serial_number=lambda s: opened.append(s) or s),
    )
    monkeypatch.setattr(drv_mod, "_load_seabreeze", lambda backend: fake_sb)
    with pytest.raises(InstrumentConnectionError, match="No Ocean spectrometer found"):
        drv_mod.open_seabreeze(None)
    devices[:] = [FakeDevice("USB2000PLUS", "A1"), FakeDevice("QE-PRO", "B2")]
    with pytest.raises(InstrumentConnectionError, match="Several spectrometers.*A1.*B2"):
        drv_mod.open_seabreeze(None)
    assert drv_mod.open_seabreeze("B2") == "B2" and opened == ["B2"]
    assert drv_mod.list_seabreeze_devices() == [
        {"model": "USB2000PLUS", "serial_number": "A1", "is_open": False},
        {"model": "QE-PRO", "serial_number": "B2", "is_open": False},
    ]


# ---------------------------------------------------------------- regressions (software review)


def test_find_peaks_does_not_report_the_edges_of_an_invalid_gap():
    # Regression: NaN pixels (e.g. the centre of a band too strong to measure) were filled with the
    # minimum, so the pixels at both edges of the gap came back as two sharp, prominent "peaks".
    x = np.linspace(400, 700, 601)
    a = 3.0 * np.exp(-0.5 * ((x - 520) / 20) ** 2) + 0.05 + 0.3 * np.exp(-0.5 * ((x - 620) / 8) ** 2)
    y = np.where(a > 2.2, np.nan, a)
    peaks = analysis.find_peaks(x, y, max_peaks=5)
    assert [round(p.wavelength_nm) for p in peaks] == [620]
    assert peaks[0].fwhm_nm == pytest.approx(2.3548 * 8, rel=0.02)


def test_boxcar_spreads_saturation_to_neighbours_in_ratio_validity():
    # Regression: with boxcar smoothing, pixels next to a saturated one include its clipped value
    # but were still reported as valid absorbance.
    drv, spec = make_driver()
    drv.store_dark(scans_to_average=2, boxcar_half_width=2)
    drv.store_reference(scans_to_average=2, boxcar_half_width=2)
    real = spec.intensities

    def clipped(*args, **kwargs):
        out = real(*args, **kwargs)
        out[1000] = drv.max_intensity
        return out

    spec.intensities = clipped
    r = drv.measure_ratio("absorbance")
    assert r.sample.saturated_pixels == 1
    assert not r.valid[998:1003].any() and r.valid[995] and r.valid[1005]


def test_acquisition_does_not_hold_the_lock_for_the_whole_series():
    # Regression: acquire() held the driver lock for every scan of the series (up to minutes), so
    # detector_cooling_off (SAFETY) and reconnect waited for the whole acquisition.
    import threading

    drv, _ = make_driver(model="QE-PRO")
    drv.set_tec(True, -10.0)
    worker = threading.Thread(target=drv.acquire, kwargs={"scans_to_average": 100})  # ~2 s simulated
    worker.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    drv.set_tec(False)
    assert time.monotonic() - t0 < 0.3 and worker.is_alive()
    worker.join(10)


def test_integration_time_change_during_acquisition_is_refused():
    import threading

    drv, _ = make_driver()
    box = {}

    def run():
        try:
            drv.acquire(scans_to_average=100)
        except InstrumentProtocolError as exc:
            box["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.2)
    drv.set_integration_time_ms(20)
    worker.join(10)
    assert "changed during the acquisition" in str(box["error"])


def test_auto_integration_respects_time_budget():
    drv, _ = make_driver()
    result = drv.auto_integration_time(time_budget_s=0.0)
    assert result["time_limited"] is True and result["history"] == [] and not result["converged"]


async def test_acquisition_longer_than_the_tool_timeout_is_refused():
    # Regression: with the limits raised (the README suggests max_integration_time_ms=60000), an
    # acquisition (or find_peaks with its 120 s timeout) could outlast its tool timeout.
    limits = {"max_integration_time_ms": 600_000, "max_acquisition_duration_s": 100_000}
    async with simulated_client(server, limits=limits) as client:
        await client.call_tool("set_integration_time", {"integration_time_ms": 60_000})
        with pytest.raises(Exception, match="more than one tool call allows"):
            await client.call_tool("acquire_spectrum", {"scans_to_average": 10})
    for name in ("acquire_spectrum", "measure_absorbance", "find_peaks", "auto_integration_time"):
        tool = await server.mcp.get_tool(name)
        assert tool.timeout >= 540


async def test_save_path_is_checked_before_acquiring(tmp_path):
    existing = tmp_path / "spectrum.csv"
    existing.write_text("keep me", encoding="utf-8")
    async with simulated_client(server) as client:
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("acquire_spectrum", {"save_path": str(existing)})
        with pytest.raises(Exception, match=r"\.csv"):
            await client.call_tool("acquire_spectrum", {"save_path": str(tmp_path / "spectrum.txt")})
        assert server.driver.last is None  # nothing was acquired
        nested = tmp_path / "new" / "dir" / "s.csv"
        result = (await client.call_tool("acquire_spectrum", {"save_path": str(nested)})).structured_content
        assert result["saved_to"] == str(nested.resolve()) and nested.exists()
    assert existing.read_text(encoding="utf-8") == "keep me"


async def test_strong_absorbance_warns_about_invalid_pixels():
    async with simulated_client(server) as client:
        await client.call_tool("store_dark_reference", {"scans_to_average": 3})
        await client.call_tool("store_reference", {"scans_to_average": 3})
        server.driver.spec.sample["peak_absorbance"] = 6.0  # ~1e-6 transmission at the band centre
        result = (await client.call_tool("measure_absorbance", {})).structured_content
        assert any("inside the range are invalid" in w for w in result["warnings"])


async def test_detector_cooling_off_survives_a_failed_temperature_read(monkeypatch):
    async with simulated_client(server, options={"sim_model": "QE-PRO"}) as client:
        await client.call_tool("set_detector_cooling", {"setpoint_c": -10})
        tec = server.driver.tec

        def broken():
            raise RuntimeError("USB read failed")

        monkeypatch.setattr(tec, "read_temperature_degrees_celsius", broken)
        off = (await client.call_tool("detector_cooling_off", {})).structured_content
        assert off["status"] == "TEC off" and "USB read failed" in off["temperature_error"]
        assert tec.enabled is False
