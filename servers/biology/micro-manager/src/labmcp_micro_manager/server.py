"""MCP server for microscopes controlled by Micro-Manager (pymmcore-plus)."""

from __future__ import annotations

import base64
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from fastmcp.tools.base import ToolResult
from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
    SafetyLimitError,
)
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from labmcp_micro_manager.driver import (
    OBJECTIVE_GROUP_RE,
    MicroManagerScope,
    image_stats,
    open_core,
    png_preview,
    resolve_save_path,
    save_tiff,
)
from labmcp_micro_manager.simulator import FakeMicroscopeCore

SOFT_LIMIT_OPTIONS = ("x_min_um", "x_max_um", "y_min_um", "y_max_um", "z_min_um", "z_max_um")

#: An acquisition must finish inside one tool call. Longer ones are refused up front (from the exposure
#: time alone) and stopped between slices/timepoints if overheads make them run long; the tool timeout
#: leaves room for the last images, returning Z and writing the TIFF.
ACQ_MAX_S = 3600.0
_ACQ_OVERRUN_S = 60.0
ACQ_TOOL_TIMEOUT_S = 4000.0


def connect(ctx: ConnectContext) -> MicroManagerScope:
    soft: dict[str, float] = {}
    for name in SOFT_LIMIT_OPTIONS:
        raw = ctx.option(name)
        if raw not in (None, ""):
            try:
                soft[name] = float(raw)
            except ValueError as exc:
                raise InstrumentConnectionError(f"--option {name}={raw!r} is not a number (micrometres).") from exc
    if ctx.simulate:
        core: Any = FakeMicroscopeCore()
        core.loadSystemConfiguration("sim://FakeMicroscope")
        scope = MicroManagerScope(core, ctx.audit, description="sim://FakeMicroscope")
    else:
        cfg = ctx.require_address()
        scope = MicroManagerScope(open_core(cfg, ctx.option("mm_path")), ctx.audit, description=cfg)
    scope.soft_limits = soft
    scope.data_dir = ctx.option("data_dir")
    return scope


server = InstrumentServer(
    "Micro-Manager Microscope (pymmcore-plus)",
    connect=connect,
    package="labmcp-micro-manager",
    instructions="""
Controls a microscope (camera, XY stage, focus drive, filter wheels, shutters, light sources,
autofocus) through Micro-Manager's device layer.
- Call `get_system_info` first: it lists the devices, presets (channels, objectives), pixel size,
  the current position and the soft limits.
- Light damages samples: use the shortest exposure that gives a usable image, keep auto-shutter
  on so the light is only on while the camera exposes, and check `saturated_fraction`
  (> 0.001 means reduce exposure or light power).
- Focus (Z) moves can drive the objective into the slide or dish. `focus_direction` says whether
  increasing Z moves the objective toward the sample. Move in small steps, and never try to get
  around a refused move - ask the user.
- Changing objectives (`set_objective`) rotates the turret: confirm with the user first
  (clearance, immersion oil/water).
- Positions are stage coordinates in micrometres; image values are raw camera counts.
- `acquire_z_stack` returns the focus to where it started. Long acquisitions can be aborted
  with `stop_stage`.
- If anything looks wrong, call `stop_stage` and `close_shutter`.
""",
    limits=[
        Limit("max_z_step_um", 50, "µm", "Largest focus (Z) move away from the current position"),
        Limit("max_xy_step_um", 20000, "µm", "Largest XY stage move (distance) in one step"),
        Limit("max_exposure_ms", 10000, "ms", "Longest camera exposure"),
        Limit("max_frames", 500, "frames", "Most images in one z-stack or time-lapse"),
        Limit("max_acquisition_duration_s", 3600, "s", "Longest time-lapse"),
    ],
    address_help="""\
  /path/to/MyScope.cfg             Micro-Manager hardware configuration file
  MMConfig_demo.cfg                Micro-Manager's demo devices (needs the adapters, see mm_path)""",
    option_help={
        "mm_path": "Micro-Manager folder with the device adapters (default: found by pymmcore-plus, "
        "e.g. after `mmcore install`)",
        "z_min_um / z_max_um": "absolute soft limits for the focus drive (µm)",
        "x_min_um / x_max_um / y_min_um / y_max_um": "absolute soft limits for the XY stage (µm)",
        "data_dir": "folder for images when no save_path is given (default: system temp folder)",
    },
)
mcp = server.mcp


# --------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Position(BaseModel):
    x_um: float | None
    y_um: float | None
    z_um: float | None
    xy_stage: str | None
    focus: str | None
    focus_direction: str = Field(description="What increasing Z does: 'toward_sample', 'away_from_sample' or 'unknown'")
    timestamp: str


class ConfigGroup(BaseModel):
    name: str
    presets: list[str]
    current: str | None
    role: str | None = Field(description="'channel', 'objective' or None")


class SystemInfo(BaseModel):
    mmcore_version: str
    configuration: str
    devices: list[dict[str, str]]
    camera: str | None
    xy_stage: str | None
    focus: str | None
    shutter: str | None
    autofocus: str | None
    image_width_px: int | None
    image_height_px: int | None
    bit_depth: int | None
    exposure_ms: float | None
    pixel_size_um: float | None = Field(description="Calibrated pixel size for the current objective, if configured")
    auto_shutter: bool
    shutter_open: bool | None
    config_groups: list[ConfigGroup]
    position: Position
    soft_limits_um: dict[str, float]
    timestamp: str


class ImageSummary(BaseModel):
    path: str = Field(description="TIFF file with the full image")
    width_px: int
    height_px: int
    dtype: str
    bit_depth: int
    min: float
    max: float
    mean: float
    std: float
    saturated_fraction: float = Field(description="Fraction of pixels at the camera's maximum value")
    focus_score: float = Field(description="Sharpness (normalised gradient energy); compare within one field")
    warning: str | None
    exposure_ms: float
    presets: dict[str, str | None] = Field(description="Current preset of every config group")
    x_um: float | None
    y_um: float | None
    z_um: float | None
    pixel_size_um: float | None
    timestamp: str


class FrameStats(BaseModel):
    index: int
    t_s: float | None = None
    z_um: float | None = None
    channel: str | None
    mean: float
    background: float = Field(description="1st-percentile intensity (camera offset + background)")
    max: float
    saturated_fraction: float
    focus_score: float


class StackSummary(BaseModel):
    path: str
    axes: str = Field(description="Axis order of the TIFF, e.g. 'ZCYX'")
    shape: list[int]
    channels: list[str | None]
    z_positions_um: list[float]
    frames: list[FrameStats]
    best_focus_z_um: float | None = Field(description="Z of the sharpest slice (first channel)")
    returned_to_z_um: float | None
    aborted: bool
    note: str | None = Field(None, description="Why the acquisition ended early, if it did")
    duration_s: float
    timestamp: str


class TimeLapseSummary(BaseModel):
    path: str
    axes: str
    shape: list[int]
    channels: list[str | None]
    interval_s: float
    frames: list[FrameStats] = Field(description="Per-frame statistics (at most 100 evenly spaced frames)")
    intensity_change_percent: dict[str, float] = Field(
        description="Mean intensity change from first to last timepoint per channel (bleaching/drift indicator)"
    )
    aborted: bool
    note: str | None = Field(None, description="Why the acquisition ended early, if it did")
    duration_s: float
    timestamp: str


class MoveResult(BaseModel):
    x_um: float | None
    y_um: float | None
    z_um: float | None
    moved_um: float = Field(description="Distance moved")
    toward_sample: bool | None = Field(None, description="For Z moves: True if the objective moved toward the sample")
    timestamp: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _scope() -> MicroManagerScope:
    return server.driver


def _soft() -> dict[str, float]:
    return _scope().soft_limits


def _direction_text(d: int) -> str:
    return {1: "toward_sample", -1: "away_from_sample"}.get(d, "unknown")


def _position() -> Position:
    scope = _scope()
    return Position(**scope.position(), focus_direction=_direction_text(scope.focus_direction()), timestamp=_now())


def _check_soft(axis: str, value: float) -> None:
    soft = _soft()
    lo, hi = soft.get(f"{axis}_min_um"), soft.get(f"{axis}_max_um")
    if lo is not None and value < lo:
        raise SafetyLimitError(
            f"Refused: target {axis.upper()} = {value:g} µm is below the soft limit {axis}_min_um = {lo:g} µm. "
            f"Nothing was sent to the microscope. Change it with `--option {axis}_min_um=<value>`."
        )
    if hi is not None and value > hi:
        raise SafetyLimitError(
            f"Refused: target {axis.upper()} = {value:g} µm is above the soft limit {axis}_max_um = {hi:g} µm. "
            f"Nothing was sent to the microscope. Change it with `--option {axis}_max_um=<value>`."
        )


def _check_z_target(z_now: float, z_target: float) -> None:
    server.check("max_z_step_um", abs(z_target - z_now), "focus move")
    _check_soft("z", z_target)


def _presets() -> dict[str, str | None]:
    return {name: g["current"] for name, g in _scope().config_groups().items()}


def _data_dir() -> str | None:
    return _scope().data_dir


def _saturation_warning(frac: float) -> str | None:
    if frac > 0.001:
        return f"{frac:.2%} of pixels are saturated: reduce exposure or light power for quantitative data."
    return None


def _with_preview(summary: BaseModel, image: np.ndarray | None, preview_px: int) -> ToolResult:
    data = summary.model_dump(mode="json")
    content: list[Any] = [TextContent(type="text", text=json.dumps(data, indent=1))]
    if image is not None:
        png = png_preview(image, preview_px)
        content.append(ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png"))
    return ToolResult(content=content, structured_content=data)


def _resolve_channels(channels: list[str] | None, channel_group: str | None) -> tuple[str | None, list[str | None]]:
    """Validate requested channel presets; returns (group, channels) with [None] = current settings."""
    if not channels:
        return None, [None]
    scope = _scope()
    group = channel_group or scope.channel_group() or ("Channel" if "Channel" in scope.config_groups() else None)
    if not group:
        raise InstrumentProtocolError("No channel group is set in this configuration: pass channel_group.")
    if OBJECTIVE_GROUP_RE.search(group):
        raise InstrumentProtocolError(
            f"{group!r} looks like an objective turret group: an acquisition must not rotate the turret between "
            "images. Use set_objective between acquisitions."
        )
    groups = scope.config_groups()
    if group not in groups:
        raise InstrumentProtocolError(f"Unknown config group {group!r}. Groups: {', '.join(groups)}.")
    unknown = [c for c in channels if c not in groups[group]["presets"]]
    if unknown:
        raise InstrumentProtocolError(
            f"Unknown {group} preset(s) {unknown}. Presets: {', '.join(groups[group]['presets'])}."
        )
    return group, list(channels)


def _frame_stats(index: int, img: np.ndarray, bit_depth: int, channel: str | None, **extra: float | None) -> FrameStats:
    s = image_stats(img, bit_depth)
    background = float(np.percentile(img, 1))
    return FrameStats(index=index, channel=channel, mean=round(s["mean"], 2), background=round(background, 2),
                      max=s["max"],
                      saturated_fraction=round(s["saturated_fraction"], 6),
                      focus_score=round(s["focus_score"], 6), **extra)


def _still_free(path: Path) -> Path:
    """The path chosen before an acquisition, or the next free name if a file appeared meanwhile."""
    return resolve_save_path(str(path), None, "", "") if path.exists() else path


def _restore_preset(group: str | None, preset: str | None) -> None:
    """Put the channel preset back after an acquisition (best effort)."""
    if group and preset:
        try:
            _scope().set_config(group, preset)
        except InstrumentError:
            pass


def _config_groups() -> list[ConfigGroup]:
    scope = _scope()
    channel = scope.channel_group()
    out = []
    for name, g in scope.config_groups().items():
        role = "channel" if name == channel else ("objective" if OBJECTIVE_GROUP_RE.search(name) else None)
        out.append(ConfigGroup(name=name, presets=g["presets"], current=g["current"], role=role))
    return out


def _check_acquisition_time(estimate_s: float, what: str) -> None:
    if estimate_s > ACQ_MAX_S:
        raise InstrumentProtocolError(
            f"The {what} would take at least {estimate_s:.0f} s, more than one tool call allows ({ACQ_MAX_S:.0f} s). "
            "Use fewer images or shorter exposures, or split it into several acquisitions. Nothing was moved."
        )


def _budget_note(what: str) -> str:
    return (f"Stopped early: the {what} would not have finished within one tool call ({ACQ_MAX_S:.0f} s). "
            "The images acquired so far were saved.")


def _thin(frames: list[FrameStats], limit: int = 100) -> list[FrameStats]:
    if len(frames) <= limit:
        return frames
    idx = np.linspace(0, len(frames) - 1, limit).round().astype(int)
    return [frames[i] for i in sorted(set(idx.tolist()))]


# --------------------------------------------------------------------------
# System and settings
# --------------------------------------------------------------------------


@mcp.tool(**READ)
def get_system_info() -> SystemInfo:
    """Describe the microscope: loaded devices (camera, stages, shutters, filter wheels...), which
    device has which role, camera size/bit depth/exposure, pixel size, config groups with their
    presets, the current position, and the soft limits. Call this first."""
    scope = _scope()
    roles = scope.roles()
    cam = scope.camera_info() if roles["camera"] else {}
    return SystemInfo(
        mmcore_version=str(scope._get("getVersionInfo")),
        configuration=scope.description,
        devices=scope.devices(),
        **roles,
        image_width_px=cam.get("width_px"),
        image_height_px=cam.get("height_px"),
        bit_depth=cam.get("bit_depth"),
        exposure_ms=cam.get("exposure_ms"),
        pixel_size_um=cam.get("pixel_size_um"),
        auto_shutter=scope.auto_shutter(),
        shutter_open=scope.shutter_open(),
        config_groups=_config_groups(),
        position=_position(),
        soft_limits_um=_soft(),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def list_config_groups() -> list[ConfigGroup]:
    """List Micro-Manager config groups (e.g. Channel, Objective) with their presets and the preset
    currently active."""
    return _config_groups()


@mcp.tool(**CONTROL)
def set_config(
    group: Annotated[str, Field(description="Config group, e.g. 'Channel'")],
    preset: Annotated[str, Field(description="Preset in that group, e.g. 'FITC'")],
) -> ConfigGroup:
    """Apply a config-group preset (e.g. switch the channel: filter cube, light source, emission
    filter). Objective/turret groups are refused here: use `set_objective`."""
    if OBJECTIVE_GROUP_RE.search(group):
        raise InstrumentProtocolError(
            f"{group!r} looks like an objective turret group: use set_objective (it moves hardware near the sample)."
        )
    _scope().set_config(group, preset)
    return next(g for g in _config_groups() if g.name == group)


@mcp.tool(**HAZARD)
def set_objective(
    preset: Annotated[str, Field(description="Objective preset, e.g. '20x'")],
    group: Annotated[str | None, Field(description="Objective config group; default: auto-detected")] = None,
) -> ConfigGroup:
    """Switch the objective (rotates the nosepiece/turret). A longer or immersion objective can hit the
    sample holder or need oil/water: confirm with the user first. Pixel size changes with the objective."""
    scope = _scope()
    grp = group or scope.objective_group()
    if not grp:
        raise InstrumentProtocolError("No objective config group found; pass group=... (see list_config_groups).")
    scope.set_config(grp, preset)
    return next(g for g in _config_groups() if g.name == grp)


@mcp.tool(**READ)
def get_exposure() -> float:
    """Return the camera exposure time in milliseconds."""
    return _scope().exposure_ms()


@mcp.tool(**CONTROL)
def set_exposure(
    exposure_ms: Annotated[float, Field(gt=0, le=60000, description="Camera exposure time in ms")],
) -> float:
    """Set the camera exposure time (ms). Longer exposures give brighter images but more
    photobleaching. Returns the exposure the camera accepted."""
    server.check("max_exposure_ms", exposure_ms, "exposure")
    return _scope().set_exposure_ms(exposure_ms)


@mcp.tool(**HAZARD)  # can leave a lamp or laser on the sample indefinitely
def set_shutter(
    open: Annotated[bool, Field(description="True opens the shutter (sample illuminated continuously)")],
    auto_shutter: Annotated[
        bool | None, Field(description="Also set auto-shutter (open only during exposures). Leave on normally.")
    ] = None,
) -> dict[str, bool | None]:
    """Open or close the current shutter, and optionally switch auto-shutter. An open shutter
    illuminates (and bleaches) the sample until it is closed again."""
    scope = _scope()
    if auto_shutter is not None:
        scope.set_auto_shutter(auto_shutter)
    state = scope.set_shutter(open)
    return {"shutter_open": state, "auto_shutter": scope.auto_shutter()}


@mcp.tool(**SAFETY)
def close_shutter() -> dict[str, bool | None]:
    """Close the shutter (stop illuminating the sample) and abort any running acquisition."""
    scope = _scope()
    scope.abort.set()
    try:
        state = scope.set_shutter(False) if scope._get("getShutterDevice") else None
    except InstrumentError as exc:
        raise InstrumentProtocolError(f"Could not close the shutter: {exc} (any acquisition was aborted).") from exc
    try:
        auto = scope.auto_shutter()
    except InstrumentError:
        auto = None
    return {"shutter_open": state, "auto_shutter": auto}


# --------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------


@mcp.tool(**READ)
def get_position() -> Position:
    """Read the XY stage and focus (Z) positions in micrometres, and which way increasing Z moves
    the objective."""
    return _position()


@mcp.tool(**HAZARD)
def move_stage_xy(
    x_um: Annotated[float, Field(description="Target X (µm), or the X offset if relative=True")],
    y_um: Annotated[float, Field(description="Target Y (µm), or the Y offset if relative=True")],
    relative: Annotated[bool, Field(description="Interpret x_um/y_um as offsets from the current position")] = False,
) -> MoveResult:
    """Move the XY stage. Moves longer than `max_xy_step_um` or outside the XY soft limits are refused
    before anything moves. Make sure the objective and condenser clear the sample holder."""
    scope = _scope()
    pos = scope.position()
    if pos["x_um"] is None:
        raise InstrumentProtocolError("No XY stage is configured.")
    tx, ty = (pos["x_um"] + x_um, pos["y_um"] + y_um) if relative else (x_um, y_um)
    dist = math.hypot(tx - pos["x_um"], ty - pos["y_um"])
    server.check("max_xy_step_um", dist, "XY move distance")
    _check_soft("x", tx)
    _check_soft("y", ty)
    x, y = scope.move_xy(tx, ty)
    return MoveResult(x_um=x, y_um=y, z_um=pos["z_um"], moved_um=round(dist, 3), timestamp=_now())


@mcp.tool(**HAZARD)
def move_z(
    z_um: Annotated[float, Field(description="Target focus position (µm), or the offset if relative=True")],
    relative: Annotated[bool, Field(description="Interpret z_um as an offset from the current position")] = False,
) -> MoveResult:
    """Move the focus drive (Z). Moving the objective toward the sample can crash it into the slide:
    moves larger than `max_z_step_um` or outside the Z soft limits are refused before anything moves."""
    scope = _scope()
    pos = scope.position()
    if pos["z_um"] is None:
        raise InstrumentProtocolError("No focus (Z) drive is configured.")
    target = pos["z_um"] + z_um if relative else z_um
    _check_z_target(pos["z_um"], target)
    z = scope.move_z(target)
    direction = scope.focus_direction()
    toward = None if direction == 0 or target == pos["z_um"] else (target - pos["z_um"]) * direction > 0
    return MoveResult(x_um=pos["x_um"], y_um=pos["y_um"], z_um=z, moved_um=round(abs(target - pos["z_um"]), 3),
                      toward_sample=toward, timestamp=_now())


@mcp.tool(**SAFETY)
def stop_stage() -> dict[str, Any]:
    """Immediately stop XY and Z stage motion and abort any z-stack or time-lapse in progress."""
    errors: list[str] = []
    stopped = _scope().stop_stages(errors)
    try:
        position = _position().model_dump()
    except InstrumentError as exc:
        position = None
        errors.append(f"reading the position failed: {exc}")
    return {"stopped": stopped, "acquisition_aborted": True, "errors": errors, "position": position}


@mcp.tool(**HAZARD, timeout=120)
def autofocus() -> MoveResult:
    """Run the configured autofocus device (e.g. a hardware focus lock) once; it moves the focus drive.
    If the result is outside the Z step or soft limits, the focus is moved back and an error is raised."""
    scope = _scope()
    z0 = scope.position()["z_um"]
    z = scope.autofocus()
    try:
        _check_z_target(z0, z)
    except SafetyLimitError as exc:
        scope.move_z(z0)
        raise SafetyLimitError(f"Autofocus moved Z to {z:g} µm, outside the limits; moved back to {z0:g} µm. {exc}") from exc
    pos = scope.position()
    return MoveResult(x_um=pos["x_um"], y_um=pos["y_um"], z_um=z, moved_um=round(abs(z - z0), 3), timestamp=_now())


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


@mcp.tool(**CONTROL, output_schema=ImageSummary.model_json_schema(), timeout=120)
def snap_image(
    save_path: Annotated[
        str | None, Field(description="TIFF path for the full image; default: a timestamped file in data_dir")
    ] = None,
    include_preview: Annotated[bool, Field(description="Return a small PNG preview image")] = True,
    preview_size_px: Annotated[int, Field(ge=64, le=1024, description="Longest side of the preview")] = 384,
) -> ToolResult:
    """Acquire one image with the current channel, exposure and position. The sample is illuminated
    during the exposure. Saves the full image as TIFF and returns summary statistics (min/max/mean,
    saturation, sharpness) plus an optional contrast-stretched preview."""
    scope = _scope()
    path = resolve_save_path(save_path, _data_dir(), "snap", _stamp())  # refuse a bad path before exposing
    cam = scope.camera_info()
    img = scope.snap()
    pos = scope.position()
    stats = image_stats(img, cam["bit_depth"])
    path = _still_free(path)
    presets = _presets()
    save_tiff(path, img, "YX", pixel_size_um=cam["pixel_size_um"],
              description={"exposure_ms": cam["exposure_ms"], **presets, "x_um": pos["x_um"], "y_um": pos["y_um"],
                           "z_um": pos["z_um"]})
    summary = ImageSummary(
        path=str(path), width_px=int(img.shape[1]), height_px=int(img.shape[0]), dtype=str(img.dtype),
        bit_depth=cam["bit_depth"], **{k: round(v, 6) if k != "max" else v for k, v in stats.items()},
        warning=_saturation_warning(stats["saturated_fraction"]), exposure_ms=cam["exposure_ms"], presets=presets,
        x_um=pos["x_um"], y_um=pos["y_um"], z_um=pos["z_um"], pixel_size_um=cam["pixel_size_um"], timestamp=_now(),
    )
    return _with_preview(summary, img if include_preview else None, preview_size_px)


@mcp.tool(**HAZARD, output_schema=StackSummary.model_json_schema(), timeout=ACQ_TOOL_TIMEOUT_S)
def acquire_z_stack(
    start_offset_um: Annotated[float, Field(description="First slice relative to the current Z (e.g. -10)")],
    end_offset_um: Annotated[float, Field(description="Last slice relative to the current Z (e.g. +10)")],
    step_um: Annotated[float, Field(gt=0, le=100, description="Spacing between slices (µm)")],
    channels: Annotated[
        list[str] | None, Field(description="Channel presets to image at every slice; omit for current settings")
    ] = None,
    channel_group: Annotated[str | None, Field(description="Config group of the channels; default: channel group")] = None,
    save_path: Annotated[str | None, Field(description="Multi-page TIFF path (ImageJ hyperstack, ZCYX)")] = None,
    include_preview: Annotated[bool, Field(description="Return a max-intensity projection preview")] = True,
) -> ToolResult:
    """Acquire a z-stack around the current focus: moves Z through the slices (optionally imaging
    several channels at each), saves a multi-page TIFF, reports per-slice statistics and the sharpest
    slice, and returns Z to where it started. Every slice must stay within `max_z_step_um` of the start
    and the Z soft limits."""
    if not (math.isfinite(start_offset_um) and math.isfinite(end_offset_um)):
        raise InstrumentProtocolError("start_offset_um and end_offset_um must be finite numbers. Nothing was moved.")
    scope = _scope()
    z0 = scope.position()["z_um"]
    if z0 is None:
        raise InstrumentProtocolError("No focus (Z) drive is configured.")
    span = end_offset_um - start_offset_um
    group, chans = _resolve_channels(channels, channel_group)
    # Count the slices (and check max_frames) before building the list: a tiny step over a large
    # span would otherwise allocate billions of positions.
    slices = abs(span) / step_um + 1e-9
    server.check("max_frames", (math.floor(slices) + 1 if math.isfinite(slices) else math.inf) * len(chans),
                 "number of images")
    n = int(math.floor(slices)) + 1
    sign = 1.0 if span >= 0 else -1.0
    zs = [round(z0 + start_offset_um + sign * i * step_um, 4) for i in range(n)]
    for z in zs:
        _check_z_target(z0, z)
    cam = scope.camera_info()
    slice_s = len(chans) * cam["exposure_ms"] / 1000.0
    _check_acquisition_time(n * slice_s, "z-stack")
    path = resolve_save_path(save_path, _data_dir(), "zstack", _stamp())  # refuse a bad path before moving
    original = scope.config_groups()[group]["current"] if group else None
    frames: list[FrameStats] = []
    images: list[list[np.ndarray]] = []
    aborted = False
    note: str | None = None
    t0 = time.monotonic()
    deadline = t0 + ACQ_MAX_S + _ACQ_OVERRUN_S
    back: float | None = None
    with scope.lock:
        scope.abort.clear()
        try:
            for z in zs:
                if scope.abort.is_set():
                    aborted = True
                    break
                if time.monotonic() + slice_s > deadline:
                    aborted, note = True, _budget_note("z-stack")
                    break
                scope.move_z(z)
                slice_imgs: list[np.ndarray] = []
                slice_frames: list[FrameStats] = []
                for ch in chans:
                    if scope.abort.is_set():
                        break
                    if group and ch:
                        scope.set_config(group, ch)
                    img = scope.snap()
                    slice_imgs.append(img)
                    slice_frames.append(_frame_stats(len(frames) + len(slice_frames), img, cam["bit_depth"], ch, z_um=z))
                if len(slice_imgs) < len(chans):  # stopped in the middle of a slice: drop it
                    aborted = True
                    break
                images.append(slice_imgs)
                frames.extend(slice_frames)
        finally:
            _restore_preset(group, original)
            if scope.abort.is_set():
                aborted = True  # stop_stage was called: do not move again
                try:
                    back = scope.position()["z_um"]
                except InstrumentError:
                    back = None
            else:
                try:
                    back = scope.move_z(z0)
                except InstrumentError:
                    back = None
    if not images:
        raise InstrumentProtocolError("The z-stack was aborted before the first slice; nothing was saved.")
    data = np.stack([np.stack(s) for s in images])  # Z, C, Y, X
    path = _still_free(path)
    save_tiff(path, data, "ZCYX", pixel_size_um=cam["pixel_size_um"], z_step_um=step_um,
              description={"exposure_ms": cam["exposure_ms"], "channels": chans, "z_start_um": zs[0]})
    first = [f for f in frames if f.channel == chans[0]]
    best = max(first, key=lambda f: f.focus_score).z_um if first else None
    summary = StackSummary(
        path=str(path), axes="ZCYX", shape=list(data.shape), channels=chans, z_positions_um=zs[: len(images)],
        frames=_thin(frames), best_focus_z_um=best, returned_to_z_um=back, aborted=aborted, note=note,
        duration_s=round(time.monotonic() - t0, 2), timestamp=_now(),
    )
    preview = data[:, 0].max(axis=0) if include_preview else None
    return _with_preview(summary, preview, 384)


@mcp.tool(**HAZARD, output_schema=TimeLapseSummary.model_json_schema(), timeout=ACQ_TOOL_TIMEOUT_S)
def acquire_time_lapse(
    timepoints: Annotated[int, Field(ge=1, le=100000, description="Number of timepoints")],
    interval_s: Annotated[float, Field(ge=0, le=86400, description="Time between timepoint starts (s)")],
    channels: Annotated[
        list[str] | None, Field(description="Channel presets to image at every timepoint; omit for current settings")
    ] = None,
    channel_group: Annotated[str | None, Field(description="Config group of the channels; default: channel group")] = None,
    save_path: Annotated[str | None, Field(description="Multi-page TIFF path (ImageJ hyperstack, TCYX)")] = None,
    include_preview: Annotated[bool, Field(description="Return a preview of the last frame")] = True,
) -> ToolResult:
    """Acquire a time-lapse at the current position: images every `interval_s` (optionally several
    channels per timepoint), saves a multi-page TIFF and reports per-frame statistics and the intensity
    change (bleaching). Bounded by `max_frames` and `max_acquisition_duration_s`; `stop_stage` aborts it."""
    scope = _scope()
    group, chans = _resolve_channels(channels, channel_group)
    server.check("max_frames", timepoints * len(chans), "number of images")
    server.check("max_acquisition_duration_s", (timepoints - 1) * interval_s, "time-lapse duration")
    cam = scope.camera_info()
    tp_s = len(chans) * cam["exposure_ms"] / 1000.0
    _check_acquisition_time(max((timepoints - 1) * interval_s + tp_s, timepoints * tp_s), "time-lapse")
    path = resolve_save_path(save_path, _data_dir(), "timelapse", _stamp())  # refuse a bad path before imaging
    original = scope.config_groups()[group]["current"] if group else None
    frames: list[FrameStats] = []
    images: list[list[np.ndarray]] = []
    aborted = False
    note: str | None = None
    t0 = time.monotonic()
    deadline = t0 + ACQ_MAX_S + _ACQ_OVERRUN_S
    with scope.lock:
        scope.abort.clear()
        try:
            for t in range(timepoints):
                start_at = t0 + t * interval_s
                if max(start_at, time.monotonic()) + tp_s > deadline:
                    aborted, note = True, _budget_note("time-lapse")
                    break
                wait = start_at - time.monotonic()
                if wait > 0 and scope.abort.wait(wait):
                    aborted = True
                    break
                if scope.abort.is_set():
                    aborted = True
                    break
                t_s = round(time.monotonic() - t0, 3)
                tp: list[np.ndarray] = []
                tp_frames: list[FrameStats] = []
                for ch in chans:
                    if scope.abort.is_set():
                        break
                    if group and ch:
                        scope.set_config(group, ch)
                    img = scope.snap()
                    tp.append(img)
                    tp_frames.append(_frame_stats(len(frames) + len(tp_frames), img, cam["bit_depth"], ch, t_s=t_s))
                if len(tp) < len(chans):  # stopped in the middle of a timepoint: drop it
                    aborted = True
                    break
                images.append(tp)
                frames.extend(tp_frames)
        finally:
            _restore_preset(group, original)
    if not images:
        raise InstrumentProtocolError("The time-lapse was aborted before the first timepoint; nothing was saved.")
    data = np.stack([np.stack(tp) for tp in images])  # T, C, Y, X
    path = _still_free(path)
    save_tiff(path, data, "TCYX", pixel_size_um=cam["pixel_size_um"], interval_s=interval_s or None,
              description={"exposure_ms": cam["exposure_ms"], "channels": chans})
    change: dict[str, float] = {}
    for ch in chans:
        series = [f.mean - f.background for f in frames if f.channel == ch]
        if len(series) >= 2 and series[0] > 0:
            change[str(ch or "current")] = round(100.0 * (series[-1] - series[0]) / series[0], 2)
    summary = TimeLapseSummary(
        path=str(path), axes="TCYX", shape=list(data.shape), channels=chans, interval_s=interval_s,
        frames=_thin(frames), intensity_change_percent=change, aborted=aborted, note=note,
        duration_s=round(time.monotonic() - t0, 2), timestamp=_now(),
    )
    return _with_preview(summary, data[-1, 0] if include_preview else None, 384)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
