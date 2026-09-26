import asyncio
import re
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import tifffile
from labmcp import AuditLog, InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.testing import simulated_client, tool_names
from labmcp_micro_manager import driver as driver_module
from labmcp_micro_manager.driver import (
    MicroManagerScope,
    downsample,
    focus_score,
    image_stats,
    open_core,
    png_preview,
    resolve_save_path,
    save_tiff,
)
from labmcp_micro_manager.server import server
from labmcp_micro_manager.simulator import FakeMicroscopeCore


def make_scope(**kwargs) -> tuple[MicroManagerScope, FakeMicroscopeCore]:
    core = FakeMicroscopeCore(**kwargs)
    core.loadSystemConfiguration("sim")
    return MicroManagerScope(core, AuditLog(), description="sim"), core


def signal(img: np.ndarray) -> float:
    return float(img.mean() - np.percentile(img, 1))


# ------------------------------------------------------------------ driver + simulator


def test_identify_and_devices():
    scope, _ = make_scope()
    info = scope.identify()
    assert info["manufacturer"] == "Micro-Manager"
    assert info["camera"].startswith("Camera (")
    types = {d["label"]: d["type"] for d in scope.devices()}
    assert types["Camera"] == "Camera"
    assert types["XY"] == "XYStage"
    assert types["Z"] == "Stage"
    assert scope.objective_group() == "Objective"
    assert scope.channel_group() == "Channel"


def test_config_groups_and_errors():
    scope, core = make_scope()
    groups = scope.config_groups()
    assert groups["Channel"]["presets"] == ["DAPI", "FITC", "TRITC", "Brightfield"]
    scope.set_config("Channel", "FITC")
    assert core.config["Channel"] == "FITC"
    with pytest.raises(InstrumentProtocolError, match="Presets: DAPI, FITC"):
        scope.set_config("Channel", "Cy5")
    # The core's own error (ValueError in pymmcore) is translated
    with pytest.raises(InstrumentProtocolError, match='Preset "x" of configuration group "Nope" does not exist'):
        scope._get("setConfig", "Nope", "x")


def test_snap_is_plausible_and_scales_with_exposure():
    scope, _ = make_scope()
    scope.set_exposure_ms(10)
    dim = scope.snap()
    assert dim.shape == (512, 512) and dim.dtype == np.uint16
    assert dim.max() <= 4095 and dim.min() >= 0
    scope.set_exposure_ms(40)
    bright = scope.snap()
    assert 2.5 < signal(bright) / signal(dim) < 5.5
    scope.set_exposure_ms(2000)
    assert image_stats(scope.snap(), 12)["saturated_fraction"] > 0.001


def test_defocus_blurs_image():
    scope, core = make_scope()
    core.z = core.focus_z(0, 0)
    sharp = focus_score(scope.snap())
    core.z += 40
    blurred = focus_score(scope.snap())
    assert sharp > 3 * blurred


def test_dark_frame_when_shutter_closed_without_auto_shutter():
    scope, _ = make_scope()
    scope.set_auto_shutter(False)
    assert scope.set_shutter(False) is False
    dark = scope.snap()
    assert abs(dark.mean() - 100) < 1  # camera offset only
    assert dark.std() < 4  # read noise only
    scope.set_shutter(True)
    assert scope.snap().mean() > dark.mean() + 10


def test_photobleaching_reduces_signal():
    scope, _ = make_scope()
    first = signal(scope.snap())  # 20 ms
    scope.set_exposure_ms(10_000)
    for _ in range(10):  # 100 s of light
        scope.snap()
    scope.set_exposure_ms(20)
    assert signal(scope.snap()) < 0.6 * first


def test_stage_moves_and_range_error():
    scope, _ = make_scope()
    assert scope.move_xy(100.0, -50.0) == (100.0, -50.0)
    assert scope.move_z(1495.5) == 1495.5
    with pytest.raises(InstrumentProtocolError, match="outside the stage travel range"):
        scope.move_xy(90000, 0)
    assert scope.focus_direction() == 1
    log = [e["data"] for e in scope.audit.recent(20)]
    assert "setXYPosition(100.0, -50.0)" in log


def test_autofocus_finds_focus_or_fails():
    scope, core = make_scope()
    core.xy = [500.0, 200.0]
    z = scope.autofocus()
    assert abs(z - core.focus_z(500, 200)) < 1
    core.z = 5000
    with pytest.raises(InstrumentProtocolError, match="no focus found"):
        scope.autofocus()


def test_stop_stages_sets_abort():
    scope, core = make_scope()
    assert scope.stop_stages() == ["XY", "Z"]
    assert core.stops == ["XY", "Z"]
    assert scope.abort.is_set()


def test_no_camera_is_reported():
    core = FakeMicroscopeCore()  # configuration not loaded
    scope = MicroManagerScope(core)
    with pytest.raises(InstrumentProtocolError, match="No camera is configured"):
        scope.snap()


def test_png_preview_and_downsample():
    img = (np.arange(600 * 400, dtype=np.uint16).reshape(400, 600) % 4096).astype(np.uint16)
    assert downsample(img, 256).shape == (133, 200)
    png = png_preview(img, 256)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (200, 133)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert len(raw) == height * (width + 1)


def test_save_tiff_hyperstack_and_unique_paths(tmp_path):
    data = np.random.default_rng(1).integers(0, 4095, (3, 2, 16, 20), dtype=np.uint16)
    path = resolve_save_path(str(tmp_path / "stack.tif"), None, "zstack", "x")
    save_tiff(path, data, "ZCYX", pixel_size_um=0.65, z_step_um=2.0)
    with tifffile.TiffFile(path) as tif:
        assert tif.series[0].axes == "ZCYX"
        assert tif.imagej_metadata["spacing"] == 2.0
        np.testing.assert_array_equal(tif.asarray(), data)
    again = resolve_save_path(str(tmp_path / "stack.tif"), None, "zstack", "x")
    assert again.name == "stack-2.tif"
    with pytest.raises(InstrumentProtocolError, match=r"\.tif"):
        resolve_save_path(str(tmp_path / "x.png"), None, "snap", "x")
    auto = resolve_save_path(None, str(tmp_path / "data"), "snap", "20260101-120000")
    assert auto == tmp_path / "data" / "snap_20260101-120000.tif"


def test_save_tiff_handles_rgb_camera_images(tmp_path):
    # CMMCorePlus.getImage() returns (Y, X, 3) for RGB32 colour cameras; ImageJ needs axes 'YXS'.
    rgb = np.random.default_rng(2).integers(0, 255, (16, 20, 3), dtype=np.uint8)
    path = save_tiff(tmp_path / "rgb.tif", rgb, "YX")
    np.testing.assert_array_equal(tifffile.imread(path), rgb)
    stack = np.stack([np.stack([rgb]), np.stack([rgb])])  # Z, C, Y, X, S
    path = save_tiff(tmp_path / "rgb_stack.tif", stack, "ZCYX", z_step_um=1.0)
    assert tifffile.imread(path).shape == (2, 16, 20, 3)
    rgb16 = rgb.astype(np.uint16) * 256  # 64-bit RGB: no ImageJ RGB type, saved as 3 channels
    path = save_tiff(tmp_path / "rgb16.tif", rgb16, "YX")
    np.testing.assert_array_equal(tifffile.imread(path), np.moveaxis(rgb16, -1, 0))
    path = save_tiff(tmp_path / "rgb16_stack.tif", np.stack([np.stack([rgb16])] * 2), "ZCYX")
    assert tifffile.imread(path).shape == (2, 3, 16, 20)


def test_driver_only_calls_real_cmmcoreplus_methods():
    """Every core method the driver calls must exist on pymmcore-plus' CMMCorePlus."""
    pmp = pytest.importorskip("pymmcore_plus")
    source = Path(driver_module.__file__).read_text()
    used = set(re.findall(r'_(?:get|do|require)\("(\w+)"', source))
    assert {"snapImage", "setXYPosition", "setPosition", "fullFocus", "stop", "setConfig"} <= used
    missing = sorted(name for name in used if not hasattr(pmp.CMMCorePlus, name))
    assert missing == []
    fake_missing = sorted(name for name in used if not hasattr(FakeMicroscopeCore, name))
    assert fake_missing == []


def test_open_core_reports_missing_adapters(tmp_path):
    pytest.importorskip("pymmcore_plus")
    cfg = tmp_path / "scope.cfg"
    cfg.write_text("Device,Camera,DemoCamera,DCam\n")
    with pytest.raises(InstrumentConnectionError, match="mmcore install"):
        open_core(str(cfg), None)
    with pytest.raises(InstrumentConnectionError, match="not found"):
        open_core(str(tmp_path / "missing.cfg"), None)


# ------------------------------------------------------------------ MCP


def sim_core() -> FakeMicroscopeCore:
    return server.driver.core


async def test_system_info_and_presets_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        sysinfo = (await client.call_tool("get_system_info", {})).structured_content
        assert sysinfo["camera"] == "Camera"
        assert sysinfo["bit_depth"] == 12
        assert sysinfo["pixel_size_um"] == 0.65
        assert sysinfo["position"]["focus_direction"] == "toward_sample"
        roles = {g["name"]: g["role"] for g in sysinfo["config_groups"]}
        assert roles == {"Channel": "channel", "Objective": "objective", "System": None}

        grp = (await client.call_tool("set_config", {"group": "Channel", "preset": "FITC"})).structured_content
        assert grp["current"] == "FITC"
        with pytest.raises(Exception, match="use set_objective"):
            await client.call_tool("set_config", {"group": "Objective", "preset": "40x"})
        obj = (await client.call_tool("set_objective", {"preset": "40x"})).structured_content
        assert obj["current"] == "40x"
        assert (await client.call_tool("get_system_info", {})).structured_content["pixel_size_um"] == 0.1625

        assert (await client.call_tool("set_exposure", {"exposure_ms": 50})).data == 50.0
        assert (await client.call_tool("get_exposure", {})).data == 50.0
        state = (await client.call_tool("set_shutter", {"open": True})).structured_content
        assert state["shutter_open"] is True
        closed = (await client.call_tool("close_shutter", {})).structured_content
        assert closed["shutter_open"] is False


async def test_snap_image_returns_stats_preview_and_tiff(tmp_path):
    async with simulated_client(server) as client:
        result = await client.call_tool("snap_image", {"save_path": str(tmp_path / "cells.tif")})
        s = result.structured_content
        assert s["width_px"] == 512 and s["dtype"] == "uint16"
        assert 100 < s["mean"] < s["max"] <= 4095
        assert s["presets"]["Channel"] == "DAPI"
        assert s["warning"] is None
        kinds = [c.type for c in result.content]
        assert kinds == ["text", "image"]
        assert result.content[1].mimeType == "image/png"
        with tifffile.TiffFile(s["path"]) as tif:
            assert tif.asarray().shape == (512, 512)
        no_preview = await client.call_tool("snap_image", {"include_preview": False, "save_path": str(tmp_path / "b.tif")})
        assert [c.type for c in no_preview.content] == ["text"]


async def test_saturation_warning(tmp_path):
    async with simulated_client(server) as client:
        await client.call_tool("set_exposure", {"exposure_ms": 3000})
        s = (await client.call_tool("snap_image", {"save_path": str(tmp_path / "sat.tif")})).structured_content
        assert s["saturated_fraction"] > 0.001
        assert "saturated" in s["warning"]


async def test_stage_moves_and_limits_via_mcp():
    async with simulated_client(server) as client:
        z0 = (await client.call_tool("get_position", {})).structured_content["z_um"]
        moved = (await client.call_tool("move_z", {"z_um": 5, "relative": True})).structured_content
        assert moved["z_um"] == pytest.approx(z0 + 5)
        assert moved["toward_sample"] is True
        with pytest.raises(Exception, match="max_z_step_um"):
            await client.call_tool("move_z", {"z_um": z0 + 500})
        assert sim_core().z == pytest.approx(z0 + 5)  # nothing moved
        xy = (await client.call_tool("move_stage_xy", {"x_um": 100, "y_um": 50, "relative": True})).structured_content
        assert (xy["x_um"], xy["y_um"]) == (100.0, 50.0)
        with pytest.raises(Exception, match="max_xy_step_um"):
            await client.call_tool("move_stage_xy", {"x_um": 50000, "y_um": 0})
        sim_core().xy = [50000.0, 0.0]  # near the end of travel
        with pytest.raises(Exception, match="outside the stage travel range"):
            await client.call_tool("move_stage_xy", {"x_um": 10000, "y_um": 0, "relative": True})
        stop = (await client.call_tool("stop_stage", {})).structured_content
        assert stop["stopped"] == ["XY", "Z"]


async def test_soft_limits_from_options():
    async with simulated_client(server, options={"z_max_um": "1490", "x_min_um": "-100"}) as client:
        with pytest.raises(Exception, match="z_max_um"):
            await client.call_tool("move_z", {"z_um": 1495})
        with pytest.raises(Exception, match="x_min_um"):
            await client.call_tool("move_stage_xy", {"x_um": -200, "y_um": 0})
        info = (await client.call_tool("get_system_info", {})).structured_content
        assert info["soft_limits_um"] == {"x_min_um": -100.0, "z_max_um": 1490.0}
        with pytest.raises(Exception, match="z_max_um"):
            await client.call_tool("acquire_z_stack", {"start_offset_um": -2, "end_offset_um": 4, "step_um": 1})


async def test_autofocus_via_mcp():
    async with simulated_client(server) as client:
        result = (await client.call_tool("autofocus", {})).structured_content
        assert result["z_um"] == pytest.approx(sim_core().focus_z(0, 0), abs=1)
        assert result["moved_um"] == pytest.approx(12, abs=1)


async def test_autofocus_limit_moves_back():
    async with simulated_client(server, limits={"max_z_step_um": 5}) as client:
        z0 = sim_core().z
        with pytest.raises(Exception, match="moved back"):
            await client.call_tool("autofocus", {})
        assert sim_core().z == z0


async def test_z_stack_finds_focus_and_returns(tmp_path):
    async with simulated_client(server) as client:
        z0 = sim_core().z
        s = (await client.call_tool("acquire_z_stack", {
            "start_offset_um": -10, "end_offset_um": 30, "step_um": 4, "channels": ["DAPI", "FITC"],
            "save_path": str(tmp_path / "stack.tif"),
        })).structured_content
        assert s["shape"] == [11, 2, 512, 512]
        assert s["z_positions_um"][0] == pytest.approx(z0 - 10)
        assert s["returned_to_z_um"] == pytest.approx(z0)
        assert abs(s["best_focus_z_um"] - sim_core().focus_z(0, 0)) <= 4
        assert sim_core().config["Channel"] == "DAPI"  # preset restored
        assert s["aborted"] is False
        with tifffile.TiffFile(s["path"]) as tif:
            assert tif.series[0].axes == "ZCYX"


async def test_z_stack_limits(tmp_path):
    async with simulated_client(server, limits={"max_frames": 10}) as client:
        with pytest.raises(Exception, match="max_z_step_um"):
            await client.call_tool("acquire_z_stack", {"start_offset_um": -60, "end_offset_um": 0, "step_um": 30})
        with pytest.raises(Exception, match="max_frames"):
            await client.call_tool("acquire_z_stack", {"start_offset_um": -5, "end_offset_um": 5, "step_um": 1})
        with pytest.raises(Exception, match="Unknown Channel preset"):
            await client.call_tool("acquire_z_stack", {"start_offset_um": 0, "end_offset_um": 1, "step_um": 1,
                                                       "channels": ["GFP"]})
        assert sim_core().snaps == 0


async def test_time_lapse_reports_bleaching(tmp_path):
    async with simulated_client(server) as client:
        await client.call_tool("set_exposure", {"exposure_ms": 1000})
        s = (await client.call_tool("acquire_time_lapse", {
            "timepoints": 10, "interval_s": 0.02, "save_path": str(tmp_path / "tl.tif"),
        })).structured_content
        assert s["shape"] == [10, 1, 512, 512]
        assert len(s["frames"]) == 10
        assert s["intensity_change_percent"]["current"] < -2
        assert s["frames"][-1]["t_s"] >= 0.18


async def test_time_lapse_limits():
    async with simulated_client(server, limits={"max_acquisition_duration_s": 60, "max_frames": 20}) as client:
        with pytest.raises(Exception, match="max_acquisition_duration_s"):
            await client.call_tool("acquire_time_lapse", {"timepoints": 10, "interval_s": 10})
        with pytest.raises(Exception, match="max_frames"):
            await client.call_tool("acquire_time_lapse", {"timepoints": 11, "interval_s": 0,
                                                          "channels": ["DAPI", "FITC"]})


async def test_stop_stage_aborts_time_lapse(tmp_path):
    async with simulated_client(server) as client:
        async def stop_soon():
            await asyncio.sleep(0.4)
            return await client.call_tool("stop_stage", {})

        lapse, _ = await asyncio.gather(
            client.call_tool("acquire_time_lapse", {"timepoints": 50, "interval_s": 0.2,
                                                    "save_path": str(tmp_path / "abort.tif")}),
            stop_soon(),
        )
        s = lapse.structured_content
        assert s["aborted"] is True
        assert 1 <= s["shape"][0] < 50


async def test_read_only_hides_control_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"get_system_info", "get_position", "list_config_groups", "get_exposure"} <= names
        for hidden in ("snap_image", "move_z", "move_stage_xy", "set_config", "set_objective", "set_exposure",
                       "acquire_z_stack", "acquire_time_lapse", "autofocus", "set_shutter"):
            assert hidden not in names
        for kept in ("stop_stage", "close_shutter", "reconnect"):
            assert kept in names


async def test_exposure_limit():
    async with simulated_client(server, limits={"max_exposure_ms": 100}) as client:
        with pytest.raises(Exception, match="max_exposure_ms"):
            await client.call_tool("set_exposure", {"exposure_ms": 500})
        assert sim_core().exposure_ms == 20.0


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_z_step_um": 10})
    with pytest.raises(SafetyLimitError):
        server.check("max_z_step_um", 25)


# ------------------------------------------------------------------ real pymmcore-plus core


def _unicore_scope() -> tuple[MicroManagerScope, object]:
    """The driver on a real pymmcore-plus core (UniMMCore) with pure-Python devices: this
    exercises the actual CMMCorePlus method overloads and return types, without adapters."""
    unicore = pytest.importorskip("pymmcore_plus.experimental.unicore")

    class Cam(unicore.SimpleCameraDevice):
        _exp = 10.0

        def get_exposure(self):
            return self._exp

        def set_exposure(self, exposure):
            self._exp = exposure

        def sensor_shape(self):
            return (64, 80)

        def dtype(self):
            return np.uint16

        def snap(self, buffer):
            buffer[:] = np.random.default_rng(0).integers(100, 200, buffer.shape, dtype=np.uint16)
            return {}

    class Focus(unicore.StageDevice):
        _z = 100.0

        def home(self):
            pass

        def stop(self):
            self.stopped = True

        def set_origin(self):
            pass

        def set_position_um(self, val):
            self._z = val

        def get_position_um(self):
            return self._z

    class Stage(unicore.XYStageDevice):
        _p = (0.0, 0.0)

        def home(self):
            pass

        def stop(self):
            self.stopped = True

        def set_origin_x(self):
            pass

        def set_origin_y(self):
            pass

        def set_position_um(self, x, y):
            self._p = (x, y)

        def get_position_um(self):
            return self._p

    class Shutter(unicore.ShutterDevice):
        _open = False

        def get_open(self):
            return self._open

        def set_open(self, open):
            self._open = open

    class Wheel(unicore.StateDevice):
        _s = 0

        def get_state(self):
            return self._s

        def set_state(self, state):
            self._s = state

    try:
        core = unicore.UniMMCore()
        core.loadPyDevice("Camera", Cam())
        core.loadPyDevice("Z", Focus())
        core.loadPyDevice("XY", Stage())
        core.loadPyDevice("Shutter", Shutter())
        core.loadPyDevice("Wheel", Wheel({0: "DAPI", 1: "FITC"}))
        core.initializeAllDevices()
        core.setCameraDevice("Camera")
        core.setFocusDevice("Z")
        core.setXYStageDevice("XY")
        core.setShutterDevice("Shutter")
        core.defineConfig("Channel", "DAPI", "Wheel", "Label", "DAPI")
        core.defineConfig("Channel", "FITC", "Wheel", "Label", "FITC")
    except Exception as exc:  # experimental API changed: not a failure of this server
        pytest.skip(f"pymmcore-plus unicore Python devices unavailable: {exc}")
    return MicroManagerScope(core, AuditLog(), description="unicore"), core


def test_driver_on_real_pymmcore_plus_core():
    scope, core = _unicore_scope()
    types = {d["label"]: d["type"] for d in scope.devices()}
    assert types == {"Camera": "Camera", "Z": "Stage", "XY": "XYStage", "Shutter": "Shutter", "Wheel": "State"}
    assert scope.config_groups() == {"Channel": {"presets": ["DAPI", "FITC"], "current": "DAPI"}}
    scope.set_config("Channel", "FITC")
    assert core.getState("Wheel") == 1
    assert scope.set_exposure_ms(25) == 25.0
    img = scope.snap()
    assert img.shape == (64, 80) and img.dtype == np.uint16
    assert scope.camera_info()["width_px"] == 80
    assert scope.move_xy(10, 20) == (10.0, 20.0)
    assert scope.move_z(105.5) == 105.5
    assert scope.position()["z_um"] == 105.5
    assert scope.set_shutter(True) is True
    assert scope.stop_stages() == ["XY", "Z"]
    with pytest.raises(InstrumentProtocolError, match="No autofocus device"):
        scope.autofocus()
    with pytest.raises(InstrumentProtocolError, match="has no preset"):
        scope.set_config("Channel", "Cy5")
