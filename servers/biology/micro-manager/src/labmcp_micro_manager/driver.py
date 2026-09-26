"""Driver for microscopes controlled by Micro-Manager, through pymmcore-plus.

Micro-Manager's device layer (MMCore) talks to cameras, XY and focus stages,
filter wheels, shutters, light sources and autofocus devices through device
adapters, configured in a hardware configuration file (``.cfg``). pymmcore-plus
(``CMMCorePlus``) is the pure-Python wrapper of MMCore. This driver only uses the
public MMCore methods listed below, with the current-device overloads:

* ``loadSystemConfiguration(path)``, ``unloadAllDevices()``, ``getVersionInfo()``, ``getAPIVersionInfo()``
* ``getLoadedDevices()``, ``getDeviceType/Library/Name/Description(label)``
* ``getCameraDevice()``, ``getXYStageDevice()``, ``getFocusDevice()``, ``getShutterDevice()``,
  ``getAutoFocusDevice()``, ``getChannelGroup()``
* ``getAvailableConfigGroups()``, ``getAvailableConfigs(group)``, ``getCurrentConfig(group)``,
  ``setConfig(group, preset)``, ``waitForConfig(group, preset)``
* ``getExposure()``, ``setExposure(ms)``, ``snapImage()``, ``getImage()``, ``getImageWidth/Height()``,
  ``getImageBitDepth()``, ``getBytesPerPixel()``, ``getPixelSizeUm()``
* ``getXYPosition()``, ``setXYPosition(x, y)``, ``getPosition()``, ``setPosition(z)``,
  ``waitForDevice(label)``, ``stop(label)``, ``getFocusDirection(label)``
* ``getShutterOpen()``, ``setShutterOpen(bool)``, ``getAutoShutter()``, ``setAutoShutter(bool)``, ``fullFocus()``

References:

* MMCore API reference, https://valelab4.ucsf.edu/~MM/doc/MMCore/html/class_c_m_m_core.html
* pymmcore-plus ``CMMCorePlus`` API (checked against pymmcore-plus 0.18.1 / pymmcore 12.5.0.75),
  https://pymmcore-plus.github.io/pymmcore-plus/api/cmmcoreplus/
* Micro-Manager configuration guide, https://micro-manager.org/Micro-Manager_Configuration_Guide

pymmcore raises ``RuntimeError`` (device errors), ``ValueError`` (unknown presets) and
``OSError`` (configuration loading) - all are turned into ``InstrumentProtocolError``.
"""

from __future__ import annotations

import re
import struct
import tempfile
import threading
import zlib
from pathlib import Path
from typing import Any

import numpy as np
from labmcp import (
    AuditLog,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    prepare_save_path,
)

#: Config groups whose presets rotate an objective turret / nosepiece.
OBJECTIVE_GROUP_RE = re.compile(r"objective|nosepiece|turret|magnification|\blens", re.IGNORECASE)

_CORE_ERRORS = (RuntimeError, ValueError, OSError)


def open_core(config_path: str, mm_path: str | None = None) -> Any:
    """Create a ``CMMCorePlus`` and load a hardware configuration. Lazily imports pymmcore-plus."""
    try:
        from pymmcore_plus import CMMCorePlus
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise InstrumentConnectionError(
            f"pymmcore-plus could not be imported ({exc}). Install it with `pip install pymmcore-plus`."
        ) from exc
    path = Path(config_path).expanduser()
    if mm_path and not Path(mm_path).expanduser().is_dir():
        raise InstrumentConnectionError(f"--option mm_path={mm_path!r} is not a directory.")
    core = CMMCorePlus(mm_path=str(Path(mm_path).expanduser()) if mm_path else None)
    try:
        core.loadSystemConfiguration(str(path))
    except FileNotFoundError as exc:
        raise InstrumentConnectionError(
            f"Micro-Manager configuration file not found: {path}. Pass the path of your .cfg file with "
            "--address (create one with Micro-Manager's Hardware Configuration Wizard), or use "
            "--address MMConfig_demo.cfg for Micro-Manager's demo devices."
        ) from exc
    except _CORE_ERRORS as exc:
        hint = ""
        if "Failed to load device adapter" in str(exc) or "Failed to load module" in str(exc):
            hint = (
                " The Micro-Manager device adapters were not found. Install them with `mmcore install` "
                "(pymmcore-plus CLI) or point to an existing Micro-Manager folder with "
                "--option mm_path=/path/to/Micro-Manager. Adapter versions must match pymmcore's "
                "device interface version."
            )
        try:
            core.unloadAllDevices()
        except _CORE_ERRORS:
            pass
        raise InstrumentConnectionError(
            f"Micro-Manager could not load {path}: {str(exc).strip()}.{hint} Also check that every device in "
            "the configuration is switched on and connected."
        ) from exc
    return core


class MicroManagerScope:
    """Microscope operations on top of a ``CMMCorePlus``-compatible core object."""

    def __init__(self, core: Any, audit: AuditLog | None = None, *, description: str = "mmcore") -> None:
        self.core = core
        self.audit = audit or AuditLog()
        self.description = description
        #: Serialises multi-step operations (acquisitions, moves) between tool calls.
        self.lock = threading.RLock()
        #: Set by `stop_stages`/`close_shutter` to abort a running acquisition.
        self.abort = threading.Event()
        #: Absolute stage bounds (µm) from ``--option x_min_um=...`` etc., checked by the server.
        self.soft_limits: dict[str, float] = {}
        #: Folder for images saved without an explicit path.
        self.data_dir: str | None = None

    # ------------------------------------------------------------ low level

    def _get(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self.core, name)(*args)
        except _CORE_ERRORS as exc:
            raise InstrumentProtocolError(f"Micro-Manager {name}{args!r} failed: {str(exc).strip()}") from exc

    def _do(self, name: str, *args: Any) -> Any:
        """A state-changing core call: recorded in the audit log."""
        self.audit.record("write", f"{name}({', '.join(repr(a) for a in args)})", self.description)
        result = self._get(name, *args)
        if result is not None and not isinstance(result, np.ndarray):
            self.audit.record("read", repr(result), self.description)
        return result

    def _require(self, getter: str, what: str) -> str:
        label = self._get(getter)
        if not label:
            raise InstrumentProtocolError(f"No {what} is configured in this Micro-Manager hardware configuration.")
        return str(label)

    # ------------------------------------------------------------ identity

    def roles(self) -> dict[str, str | None]:
        return {
            "camera": self._get("getCameraDevice") or None,
            "xy_stage": self._get("getXYStageDevice") or None,
            "focus": self._get("getFocusDevice") or None,
            "shutter": self._get("getShutterDevice") or None,
            "autofocus": self._get("getAutoFocusDevice") or None,
        }

    def identify(self) -> dict[str, Any]:
        roles = self.roles()
        info: dict[str, Any] = {
            "manufacturer": "Micro-Manager",
            "mmcore": self._get("getVersionInfo"),
            "device_api": self._get("getAPIVersionInfo"),
            "configuration": self.description,
        }
        if roles["camera"]:
            cam = roles["camera"]
            info["camera"] = f"{cam} ({self._get('getDeviceLibrary', cam)}/{self._get('getDeviceName', cam)})"
        info.update({k: v for k, v in roles.items() if k != "camera" and v})
        return info

    def devices(self) -> list[dict[str, str]]:
        out = []
        for label in self._get("getLoadedDevices"):
            if label == "Core":
                continue
            dtype = self._get("getDeviceType", label)
            name = getattr(dtype, "name", str(dtype))
            out.append(
                {
                    "label": str(label),
                    "type": name[:-6] if name.endswith("Device") else name,
                    "library": str(self._get("getDeviceLibrary", label)),
                    "adapter": str(self._get("getDeviceName", label)),
                    "description": str(self._get("getDeviceDescription", label)),
                }
            )
        return out

    # ------------------------------------------------------------ presets

    def config_groups(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for group in self._get("getAvailableConfigGroups"):
            presets = [str(p) for p in self._get("getAvailableConfigs", group)]
            try:
                current = self._get("getCurrentConfig", group) or None
            except InstrumentProtocolError:
                current = None
            out[str(group)] = {"presets": presets, "current": current}
        return out

    def channel_group(self) -> str | None:
        return self._get("getChannelGroup") or None

    def objective_group(self) -> str | None:
        return next((g for g in self._get("getAvailableConfigGroups") if OBJECTIVE_GROUP_RE.search(str(g))), None)

    def set_config(self, group: str, preset: str) -> str:
        groups = self.config_groups()
        if group not in groups:
            raise InstrumentProtocolError(
                f"Unknown config group {group!r}. Available groups: {', '.join(groups) or '(none)'}."
            )
        if preset not in groups[group]["presets"]:
            raise InstrumentProtocolError(
                f"Group {group!r} has no preset {preset!r}. Presets: {', '.join(groups[group]['presets'])}."
            )
        with self.lock:
            self._do("setConfig", group, preset)
            self._get("waitForConfig", group, preset)
        return preset

    # ------------------------------------------------------------ camera

    def exposure_ms(self) -> float:
        return float(self._get("getExposure"))

    def set_exposure_ms(self, ms: float) -> float:
        self._require("getCameraDevice", "camera")
        self._do("setExposure", float(ms))
        return self.exposure_ms()

    def camera_info(self) -> dict[str, Any]:
        cam = self._require("getCameraDevice", "camera")
        return {
            "label": cam,
            "width_px": int(self._get("getImageWidth")),
            "height_px": int(self._get("getImageHeight")),
            "bit_depth": int(self._get("getImageBitDepth")),
            "bytes_per_pixel": int(self._get("getBytesPerPixel")),
            "exposure_ms": self.exposure_ms(),
            "pixel_size_um": float(self._get("getPixelSizeUm")) or None,
        }

    def snap(self) -> np.ndarray:
        """Acquire one image with the current settings (auto-shutter opens the light path)."""
        self._require("getCameraDevice", "camera")
        with self.lock:
            self._do("snapImage")
            img = self._get("getImage")
        return np.asarray(img)

    # ------------------------------------------------------------ stages

    def position(self) -> dict[str, Any]:
        xy = self._get("getXYStageDevice") or None
        focus = self._get("getFocusDevice") or None
        x = y = z = None
        if xy:
            x, y = (float(v) for v in self._get("getXYPosition"))
        if focus:
            z = float(self._get("getPosition"))
        return {"x_um": x, "y_um": y, "z_um": z, "xy_stage": xy, "focus": focus}

    def focus_direction(self) -> int:
        """+1: increasing Z moves the objective toward the sample, -1: away, 0: unknown."""
        focus = self._get("getFocusDevice")
        if not focus:
            return 0
        try:
            return int(self._get("getFocusDirection", focus))
        except (InstrumentProtocolError, TypeError, ValueError):
            return 0

    def move_xy(self, x_um: float, y_um: float) -> tuple[float, float]:
        xy = self._require("getXYStageDevice", "XY stage")
        with self.lock:
            self._do("setXYPosition", float(x_um), float(y_um))
            self._get("waitForDevice", xy)
        x, y = self._get("getXYPosition")
        return float(x), float(y)

    def move_z(self, z_um: float) -> float:
        focus = self._require("getFocusDevice", "focus (Z) stage")
        with self.lock:
            self._do("setPosition", float(z_um))
            self._get("waitForDevice", focus)
        return float(self._get("getPosition"))

    def stop_stages(self, errors: list[str] | None = None) -> list[str]:
        """Abort any acquisition and halt XY and Z motion. Deliberately does not wait for `lock`.

        Best effort: every stage is tried even if an earlier one fails; failures are appended to
        ``errors`` (some stage adapters do not implement stop).
        """
        self.abort.set()
        stopped = []
        for getter in ("getXYStageDevice", "getFocusDevice"):
            try:
                label = self._get(getter)
                if label:
                    self._do("stop", label)
                    stopped.append(str(label))
            except InstrumentError as exc:
                if errors is not None:
                    errors.append(str(exc))
        return stopped

    def autofocus(self) -> float:
        self._require("getAutoFocusDevice", "autofocus device")
        focus = self._require("getFocusDevice", "focus (Z) stage")
        with self.lock:
            self._do("fullFocus")
            self._get("waitForDevice", focus)
        return float(self._get("getPosition"))

    # ------------------------------------------------------------ shutter

    def shutter_open(self) -> bool | None:
        if not self._get("getShutterDevice"):
            return None
        return bool(self._get("getShutterOpen"))

    def auto_shutter(self) -> bool:
        return bool(self._get("getAutoShutter"))

    def set_shutter(self, open_: bool) -> bool:
        self._require("getShutterDevice", "shutter")
        self._do("setShutterOpen", bool(open_))
        return bool(self._get("getShutterOpen"))

    def set_auto_shutter(self, on: bool) -> bool:
        self._do("setAutoShutter", bool(on))
        return self.auto_shutter()

    def close(self) -> None:
        try:
            self._do("unloadAllDevices")
        except InstrumentProtocolError:
            pass


# --------------------------------------------------------------------------
# Image helpers (pure functions)
# --------------------------------------------------------------------------


def image_stats(img: np.ndarray, bit_depth: int) -> dict[str, float]:
    """Summary statistics in camera counts, plus the fraction of saturated pixels."""
    a = np.asarray(img)
    full = (1 << int(bit_depth)) - 1 if bit_depth and np.issubdtype(a.dtype, np.integer) else float(a.max())
    return {
        "min": float(a.min()),
        "max": float(a.max()),
        "mean": float(a.mean()),
        "std": float(a.std()),
        "saturated_fraction": float(np.count_nonzero(a >= full) / a.size) if full else 0.0,
        "focus_score": focus_score(a),
    }


def focus_score(img: np.ndarray) -> float:
    """Normalised gradient energy (Brenner-like): higher = sharper. Comparable between images of
    the same field and settings (e.g. slices of a z-stack), not across samples."""
    a = np.asarray(img, dtype=np.float64)
    if a.ndim > 2:
        a = a.mean(axis=-1)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    gx = a[:, 2:] - a[:, :-2]
    gy = a[2:, :] - a[:-2, :]
    mean = a.mean()
    return float((np.mean(gx**2) + np.mean(gy**2)) / (mean * mean)) if mean > 0 else 0.0


def downsample(img: np.ndarray, max_px: int) -> np.ndarray:
    """Block-average so the longest side is at most ``max_px``."""
    a = np.asarray(img, dtype=np.float64)
    if a.ndim > 2:
        a = a.mean(axis=-1)
    factor = max(1, int(np.ceil(max(a.shape) / max_px)))
    if factor == 1:
        return a
    h, w = (a.shape[0] // factor) * factor, (a.shape[1] // factor) * factor
    return a[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def png_preview(img: np.ndarray, max_px: int = 384) -> bytes:
    """8-bit grayscale PNG, contrast-stretched to the 0.5-99.5 percentiles (display only)."""
    a = downsample(img, max_px)
    lo, hi = np.percentile(a, [0.5, 99.5])
    scaled = np.zeros_like(a) if hi <= lo else np.clip((a - lo) / (hi - lo), 0, 1)
    pix = (scaled * 255 + 0.5).astype(np.uint8)
    raw = b"".join(b"\x00" + row.tobytes() for row in pix)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", pix.shape[1], pix.shape[0], 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def default_data_dir() -> Path:
    return Path(tempfile.gettempdir()) / "labmcp-micro-manager"


TIFF_SUFFIXES = (".tif", ".tiff")


def resolve_save_path(save_path: str | None, data_dir: str | None, stem: str, stamp: str) -> Path:
    """Where to write a TIFF (absolute path; parent folders created).

    Never overwrites: an existing name gets a numeric suffix. Refuses folders and other extensions.
    """
    if save_path:
        path = Path(save_path).expanduser()
        if path.suffix.lower() not in TIFF_SUFFIXES:
            raise InstrumentProtocolError(f"save_path must end in .tif or .tiff (got {path.name!r}). Nothing was written.")
        if path.is_dir():
            raise InstrumentProtocolError(f"save_path {path} is a folder; give a .tif file name. Nothing was written.")
    else:
        base = Path(data_dir).expanduser() if data_dir else default_data_dir()
        path = base / f"{stem}_{stamp}.tif"
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return prepare_save_path(candidate, suffixes=TIFF_SUFFIXES)


def save_tiff(
    path: Path,
    data: np.ndarray,
    axes: str,
    *,
    pixel_size_um: float | None = None,
    z_step_um: float | None = None,
    interval_s: float | None = None,
    description: dict[str, Any] | None = None,
) -> Path:
    """Write an ImageJ-compatible (multi-page) TIFF. ``axes`` like 'YX', 'ZCYX', 'TCYX'."""
    import tifffile

    arr = np.asarray(data)
    if arr.ndim == len(axes) + 1 and arr.shape[-1] in (3, 4):
        # Colour cameras: CMMCorePlus.getImage() returns (Y, X, 3) RGB for RGB32 pixel types.
        arr = arr[..., :3]
        if arr.dtype == np.uint8:
            axes += "S"  # ImageJ RGB (8-bit samples only)
        else:
            # ImageJ has no >8-bit RGB type: keep the colour planes as channels instead.
            arr = np.moveaxis(arr, -1, -3)  # (..., 3, Y, X)
            if "C" in axes:
                c = axes.index("C")
                arr = np.moveaxis(arr, -3, c + 1)
                arr = arr.reshape(arr.shape[:c] + (arr.shape[c] * 3,) + arr.shape[c + 2 :])
            else:
                axes = axes[:-2] + "C" + axes[-2:]
    metadata: dict[str, Any] = {"axes": axes, "unit": "um"}
    if z_step_um:
        metadata["spacing"] = float(z_step_um)
    if interval_s:
        metadata["finterval"] = float(interval_s)
    if description:
        metadata["Info"] = "\n".join(f"{k} = {v}" for k, v in description.items())
    if arr.dtype not in (np.uint8, np.uint16, np.float32):
        arr = arr.astype(np.float32)
    kwargs: dict[str, Any] = {}
    if pixel_size_um:
        kwargs["resolution"] = (1.0 / pixel_size_um, 1.0 / pixel_size_um)
    tifffile.imwrite(path, arr, imagej=True, metadata=metadata, **kwargs)
    return path
