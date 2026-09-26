"""MCP server for mass-spectrometry data files (mzML, mzMLb, Bruker TDF; vendor files via conversion)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import numpy as np
from labmcp import CONTROL, READ, ConnectContext, InstrumentProtocolError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_ms_data import analysis
from labmcp_ms_data.converter import CONVERTERS, plan_conversion, run_conversion
from labmcp_ms_data.driver import MsDataDriver, RunBackend, ScanTable
from labmcp_ms_data.files import FORMATS, DataRoot, detect_format, dir_size_bytes
from labmcp_ms_data.simulator import SyntheticRun


def connect(ctx: ConnectContext) -> MsDataDriver:
    address = ctx.address
    if address and address.startswith("file://"):
        address = address[len("file://") :]
    root = DataRoot(address)
    converter = {k: v for k in ("converter", "converter_path", "docker_image") if (v := ctx.option(k))}
    if converter.get("converter", "auto").lower() not in CONVERTERS:
        raise InstrumentProtocolError(f"--option converter must be one of {', '.join(CONVERTERS)}.")
    try:
        cache_size = int(ctx.option("cache_size", "4") or 4)
        seed = int(ctx.option("sim_seed", "7") or 7)
    except ValueError as exc:
        raise InstrumentProtocolError(f"--option cache_size / sim_seed must be integers: {exc}") from exc
    sim = SyntheticRun(seed=seed) if ctx.simulate else None
    return MsDataDriver(root, simulated_run=sim, cache_size=cache_size, converter=converter)


server = InstrumentServer(
    "Mass Spectrometry Data (mzML, Bruker TDF, vendor conversion)",
    connect=connect,
    package="labmcp-ms-data",
    instructions="""
This server reads mass-spectrometry DATA FILES; no instrument is controlled. File access is
sandboxed to one data folder (`--address`, default: the current directory).
- Start with `list_runs` to find files, then `get_run_info` for the run's instrument, date, scan
  counts and ranges. Paths are relative to the data folder.
- mzML, mzML.gz, mzMLb and Bruker timsTOF .d (TDF) are read directly. Thermo/Waters .raw,
  Agilent .d, SCIEX .wiff, Shimadzu .lcd and Bruker BAF must first be converted with
  `convert_to_mzml` (uses the converter the user installed; it writes a file).
- Retention times are in minutes (`rt_min`), m/z in Th, intensities in the file's arbitrary units.
  Tolerances are in ppm (default) or Da; use ~5-10 ppm for Orbitrap/TOF data, 0.3-0.5 Da for
  ion traps / low-resolution data.
- Chromatograms and spectra are summarised/downsampled (`max_points`, `top_n`); pass `save_path`
  (a CSV inside the data folder) when the user needs the full data.
- XIC areas are trapezoidal integrals over RT in minutes without baseline subtraction; treat them
  as relative quantities, not absolute amounts.
- The first call on a large file indexes every spectrum and can take tens of seconds.
""",
    limits=[
        Limit("max_conversion_time_s", 3600, "s", "Longest time a vendor-file conversion may run"),
        Limit("max_xic_targets", 50, "", "Most m/z values in one extract_ion_chromatogram call"),
    ],
    address_help="""\
  (omit)                    the current directory is the data folder
  /data/lcms                a folder with mzML / .d / .raw files (subfolders are searched)""",
    option_help={
        "converter": "auto (default), msconvert, docker or thermorawfileparser (for convert_to_mzml)",
        "converter_path": "path to msconvert / ThermoRawFileParser (.sh, .exe or .dll) / docker if not on PATH",
        "docker_image": "Docker image for converter=docker (default proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses)",
        "cache_size": "number of runs kept open in memory (default 4)",
        "sim_seed": "random seed of the simulated run (default 7)",
    },
)
mcp = server.mcp


# ---------------------------------------------------------------- models


class RunEntry(BaseModel):
    path: str = Field(description="Path relative to the data folder; pass it to the other tools")
    format: str
    vendor: str
    format_label: str
    readable_directly: bool = Field(description="False: convert with convert_to_mzml first")
    size_mb: float | None
    modified: str | None


class RunList(BaseModel):
    data_folder: str
    simulated: bool
    runs: list[RunEntry]
    total_found: int
    truncated: bool


class RunInfo(BaseModel):
    path: str
    format: str
    simulated: bool
    instrument_vendor: str | None
    instrument_model: str | None
    instrument_serial: str | None
    acquisition_date: str | None
    software: str | None
    sample_name: str | None
    spectra_total: int
    spectra_by_ms_level: dict[str, int]
    rt_start_min: float | None
    rt_end_min: float | None
    mz_min: float | None = Field(description="Lowest observed (or acquisition-range) m/z")
    mz_max: float | None
    polarity: Literal["positive", "negative", "mixed", "unknown"]
    spectrum_type: Literal["centroid", "profile", "mixed", "unknown"]
    ion_mobility: bool
    notes: list[str]


class Chromatogram(BaseModel):
    path: str
    kind: Literal["TIC", "BPC"]
    ms_level: int
    simulated: bool
    spectra_used: int
    points_returned: int
    rt_min: list[float]
    intensity: list[float]
    base_peak_mz: list[float | None] | None = Field(
        None, description="BPC only: m/z of the base peak per point"
    )
    max_intensity: float | None
    max_at_rt_min: float | None
    median_intensity: float | None
    area: float | None = Field(description="Trapezoidal integral over RT in minutes (intensity x min)")
    saved_to: str | None
    notes: list[str]


class XICTrace(BaseModel):
    target_mz: float
    tolerance_da: float
    mz_window: tuple[float, float]
    apex_rt_min: float | None
    apex_intensity: float | None
    peak_start_rt_min: float | None
    peak_end_rt_min: float | None
    area: float | None = Field(description="Apex peak area (intensity x min, no baseline subtraction)")
    fwhm_s: float | None
    points_across_peak: int | None
    total_area: float = Field(description="Integral of the whole trace in the RT window")
    rt_min: list[float] = Field(description="Downsampled trace (max per bin)")
    intensity: list[float]


class XICResult(BaseModel):
    path: str
    simulated: bool
    ms_level: int
    spectra_used: int
    rt_window_min: tuple[float, float] | None
    traces: list[XICTrace]
    saved_to: str | None
    warnings: list[str]


class Peak(BaseModel):
    mz: float
    intensity: float
    relative_intensity_percent: float


class PrecursorInfo(BaseModel):
    mz: float | None
    charge: int | None
    intensity: float | None
    precursor_scan: int | None = Field(description="Scan number (or frame id) of the survey scan, if known")


class SpectrumResult(BaseModel):
    path: str
    simulated: bool
    index: int = Field(description="0-based position in the file")
    native_id: str
    scan_number: int | None
    ms_level: int
    rt_min: float | None
    polarity: str
    spectrum_type: Literal["centroid", "profile", "unknown"]
    filter_string: str | None
    injection_time_ms: float | None
    precursor: PrecursorInfo | None
    peak_count: int
    tic: float = Field(description="Sum of intensities (inside the m/z window, if given)")
    base_peak_mz: float | None
    base_peak_intensity: float | None
    mz_range: tuple[float, float] | None
    top_peaks: list[Peak] = Field(description="Most intense peaks, highest first")
    saved_to: str | None
    notes: list[str]


class MS2Match(BaseModel):
    index: int
    native_id: str
    scan_number: int | None
    rt_min: float | None
    precursor_mz: float
    delta_ppm: float
    charge: int | None
    precursor_intensity: float | None
    base_peak_mz: float | None


class MS2Search(BaseModel):
    path: str
    simulated: bool
    precursor_mz: float
    tolerance_da: float
    rt_window_min: tuple[float, float] | None
    total_matches: int
    matches: list[MS2Match]
    truncated: bool


class RunQC(BaseModel):
    path: str
    simulated: bool
    rt_start_min: float | None
    rt_end_min: float | None
    ms1_spectra: int
    ms2_spectra: int
    msn_higher_spectra: int
    tic_median: float | None
    tic_cv_percent: float | None = Field(description="Coefficient of variation of the MS1 TIC (whole run)")
    tic_dropouts: int = Field(description="MS1 scans with TIC < 20 % of the local median (spray instability)")
    tic_dropout_rts_min: list[float]
    tic_elution_quartiles_min: list[float] | None = Field(
        description="RT at which 25 / 50 / 75 % of the summed MS1 TIC has eluted"
    )
    median_cycle_time_s: float | None = Field(description="Median time between consecutive MS1 scans")
    ms2_per_cycle_median: float | None
    ms2_per_cycle_mean: float | None
    ms2_per_cycle_max: int | None
    median_ms1_injection_time_ms: float | None
    median_ms2_injection_time_ms: float | None
    ms2_at_max_injection_time_percent: float | None
    ms2_precursor_charges: dict[str, int]
    warnings: list[str]


class ConversionReport(BaseModel):
    input: str
    detected_format: str
    converter: str
    command: list[str]
    output: str
    dry_run: bool
    success: bool
    duration_s: float | None
    output_size_mb: float | None
    return_code: int | None
    log_tail: str
    notes: list[str]


# ---------------------------------------------------------------- helpers

PathArg = Annotated[
    str | None,
    Field(
        description="Run path relative to the data folder (from list_runs); may be omitted if there is one run"
    ),
]
MaxPoints = Annotated[
    int, Field(ge=10, le=10000, description="Maximum points returned (downsampled, max per bin)")
]
SavePath = Annotated[
    str | None, Field(description="Optional CSV path inside the data folder for the full-resolution data")
]
RtStart = Annotated[float | None, Field(ge=0, description="Only use spectra at or after this RT (min)")]
RtEnd = Annotated[float | None, Field(ge=0, description="Only use spectra at or before this RT (min)")]
Tolerance = Annotated[float, Field(gt=0, le=1000, description="m/z tolerance (± this value)")]
TolUnit = Annotated[Literal["ppm", "da"], Field(description="Tolerance unit: ppm or da")]


def _drv() -> MsDataDriver:
    return server.driver


def _open(path: str | None) -> tuple[MsDataDriver, RunBackend]:
    drv = _drv()
    return drv, drv.open_run(path)


def _opt(v: float) -> float | None:
    return float(v) if np.isfinite(v) else None


def _rt_mask(t: ScanTable, start: float | None, end: float | None) -> np.ndarray:
    mask = np.ones(len(t), dtype=bool)
    if start is not None:
        mask &= t.rt_min >= start
    if end is not None:
        mask &= t.rt_min <= end
    if start is not None and end is not None and start > end:
        raise InstrumentProtocolError(f"rt_start_min ({start}) is after rt_end_min ({end}).")
    return mask


def _save(save_path: str | None, header: list[str], cols: list[Any]) -> str | None:
    if not save_path:
        return None
    p = _drv().root.output_path(save_path, suffix=".csv")
    return analysis.write_csv(p, header, cols)


def _window(start: float | None, end: float | None) -> tuple[float, float] | None:
    if start is None and end is None:
        return None
    return (start if start is not None else 0.0, end if end is not None else float("inf"))


def _levels(t: ScanTable) -> dict[str, int]:
    vals, counts = np.unique(t.ms_level, return_counts=True)
    return {str(int(v)): int(c) for v, c in zip(vals, counts, strict=True)}


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def list_runs(
    subfolder: Annotated[str, Field(description="Folder to search, relative to the data folder")] = ".",
    recursive: Annotated[bool, Field(description="Also search subfolders (up to 6 levels)")] = True,
    max_results: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> RunList:
    """Find mass-spectrometry runs in the data folder and detect each one's vendor and format
    (mzML/mzML.gz/mzMLb, Bruker .d TDF or BAF, Agilent .d, Thermo .raw, Waters .raw folder,
    SCIEX .wiff/.wiff2, Shimadzu .lcd, mzXML). `readable_directly=false` means the run must be
    converted with convert_to_mzml before it can be analysed."""
    drv = _drv()
    if drv.simulated_run is not None:
        run = drv.simulated_run
        return RunList(
            data_folder="(simulated)",
            simulated=True,
            runs=[
                RunEntry(
                    path=str(run.path),
                    format="mzml",
                    vendor="simulated",
                    format_label="mzML (in memory)",
                    readable_directly=True,
                    size_mb=None,
                    modified=None,
                )  # fmt: skip
            ],
            total_found=1,
            truncated=False,
        )
    found = drv.root.find_runs(subfolder, recursive=recursive)
    entries = []
    for r in found[:max_results]:
        vendor, label, readable = FORMATS[r.format]
        try:
            st = r.path.stat()
            modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            size = round(dir_size_bytes(r.path) / 1e6, 2)
        except OSError:
            modified, size = None, None
        entries.append(
            RunEntry(
                path=drv.root.relative(r.path),
                format=r.format,
                vendor=vendor,
                format_label=label,
                readable_directly=readable,
                size_mb=size,
                modified=modified,
            )  # fmt: skip
        )
    return RunList(
        data_folder=str(drv.root.root),
        simulated=False,
        runs=entries,
        total_found=len(found),
        truncated=len(found) > max_results,
    )


@mcp.tool(**READ, timeout=900)
def get_run_info(path: PathArg = None) -> RunInfo:
    """Describe one run: instrument vendor/model/serial (when the file records them), acquisition
    date, software, number of spectra per MS level, retention-time and m/z ranges, polarity,
    centroid/profile, and whether ion-mobility data is present. The first call on a large file
    indexes it (can take a while); later calls are instant."""
    drv, run = _open(path)
    t = run.table
    md = run.metadata()
    pol = set(np.unique(t.polarity).tolist()) - {0}
    polarity = (
        "unknown" if not pol else ("mixed" if len(pol) > 1 else ("positive" if 1 in pol else "negative"))
    )
    cen = set(np.unique(t.centroided).tolist()) - {-1}
    stype = "unknown" if not cen else ("mixed" if len(cen) > 1 else ("centroid" if 1 in cen else "profile"))
    lo = np.nanmin(t.low_mz) if np.isfinite(t.low_mz).any() else np.nan
    hi = np.nanmax(t.high_mz) if np.isfinite(t.high_mz).any() else np.nan
    if md.mz_acquisition_range:
        lo, hi = md.mz_acquisition_range
    rt = t.rt_min[np.isfinite(t.rt_min)]
    return RunInfo(
        path=drv.display_path(run),
        format=run.format,
        simulated=drv.simulated,
        instrument_vendor=md.instrument_vendor,
        instrument_model=md.instrument_model,
        instrument_serial=md.instrument_serial,
        acquisition_date=md.acquisition_date,
        software=md.software,
        sample_name=md.sample_name,
        spectra_total=len(t),
        spectra_by_ms_level=_levels(t),
        rt_start_min=float(rt.min()) if rt.size else None,
        rt_end_min=float(rt.max()) if rt.size else None,
        mz_min=_opt(lo),
        mz_max=_opt(hi),
        polarity=polarity,
        spectrum_type=stype,
        ion_mobility=md.ion_mobility,
        notes=list(md.notes),
    )


def _chromatogram(
    kind: Literal["TIC", "BPC"],
    path: str | None,
    ms_level: int,
    rt_start_min: float | None,
    rt_end_min: float | None,
    max_points: int,
    save_path: str | None,
) -> Chromatogram:
    drv, run = _open(path)
    t = run.table
    mask = _rt_mask(t, rt_start_min, rt_end_min) & (t.ms_level == ms_level)
    idx = np.flatnonzero(mask)
    notes: list[str] = []
    if idx.size == 0:
        raise InstrumentProtocolError(
            f"No MS{ms_level} spectra in that RT window (the run has {_levels(t)} spectra by MS level)."
        )
    rt = t.rt_min[idx]
    y = (t.tic if kind == "TIC" else t.base_peak_intensity)[idx]
    bpmz = t.base_peak_mz[idx]
    if not np.isfinite(y).any():
        raise InstrumentProtocolError(f"This file does not record the {kind} values for its spectra.")
    y = np.nan_to_num(y, nan=0.0)
    xs, ys = analysis.downsample_max(rt, y, max_points)
    bp: list[float | None] | None = None
    if kind == "BPC":
        lookup = dict(zip(rt.tolist(), bpmz.tolist(), strict=True))
        bp = [(_opt(lookup[x]) if x in lookup else None) for x in xs]
        if run.format == "bruker_tdf":
            notes.append("TDF stores the base-peak intensity per frame but not its m/z.")
    i = int(np.argmax(y))
    header = ["rt_min", "tic" if kind == "TIC" else "base_peak_intensity"]
    cols: list[Any] = [rt, y]
    if kind == "BPC":
        header.append("base_peak_mz")
        cols.append(bpmz)
    header.insert(0, "native_id")
    cols.insert(0, [t.native_id[k] for k in idx])
    return Chromatogram(
        path=drv.display_path(run),
        kind=kind,
        ms_level=ms_level,
        simulated=drv.simulated,
        spectra_used=int(idx.size),
        points_returned=len(xs),
        rt_min=[round(x, 4) for x in xs],
        intensity=[float(f"{v:.5g}") for v in ys],
        base_peak_mz=[None if v is None else round(v, 5) for v in bp] if bp is not None else None,
        max_intensity=float(y[i]),
        max_at_rt_min=round(float(rt[i]), 4),
        median_intensity=float(np.median(y)),
        area=analysis.trapezoid(y, rt),
        saved_to=_save(save_path, header, cols),
        notes=notes,
    )


MsLevel = Annotated[int, Field(ge=1, le=10, description="MS level to use (1 = survey scans)")]


@mcp.tool(**READ, timeout=900)
def get_tic(
    path: PathArg = None,
    ms_level: MsLevel = 1,
    rt_start_min: RtStart = None,
    rt_end_min: RtEnd = None,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> Chromatogram:
    """Total ion chromatogram (sum of all intensities per spectrum vs retention time) for one MS
    level, downsampled to `max_points` (keeping the maximum in each bin so peaks survive).
    Returns the apex, median and area; `save_path` writes every point to CSV."""
    return _chromatogram("TIC", path, ms_level, rt_start_min, rt_end_min, max_points, save_path)


@mcp.tool(**READ, timeout=900)
def get_bpc(
    path: PathArg = None,
    ms_level: MsLevel = 1,
    rt_start_min: RtStart = None,
    rt_end_min: RtEnd = None,
    max_points: MaxPoints = 500,
    save_path: SavePath = None,
) -> Chromatogram:
    """Base peak chromatogram (intensity of the most intense peak per spectrum, with its m/z) vs
    retention time, downsampled to `max_points`. Cleaner than the TIC for spotting eluting
    compounds; the base-peak m/z tells you which ion dominates each part of the run."""
    return _chromatogram("BPC", path, ms_level, rt_start_min, rt_end_min, max_points, save_path)


@mcp.tool(**READ, timeout=1800)
def extract_ion_chromatogram(
    mz: Annotated[list[float], Field(min_length=1, description="Target m/z value(s)")],
    path: PathArg = None,
    tolerance: Tolerance = 10.0,
    tolerance_unit: TolUnit = "ppm",
    ms_level: MsLevel = 1,
    rt_start_min: RtStart = None,
    rt_end_min: RtEnd = None,
    max_points: MaxPoints = 300,
    save_path: SavePath = None,
) -> XICResult:
    """Extracted ion chromatogram (XIC/EIC) for one or more m/z values: the summed intensity
    within ± tolerance (ppm or Da) in every MS1 spectrum (or another `ms_level`). For each target
    returns the apex RT and intensity, the apex peak's boundaries, area (intensity x min, no
    baseline subtraction) and FWHM, plus a downsampled trace. Reads every spectrum in the RT
    window, so restrict `rt_start_min`/`rt_end_min` on long runs."""
    server.check("max_xic_targets", len(mz), "number of XIC targets")
    if any(m <= 0 for m in mz):
        raise InstrumentProtocolError("m/z values must be positive.")
    drv, run = _open(path)
    t = run.table
    idx = np.flatnonzero(_rt_mask(t, rt_start_min, rt_end_min) & (t.ms_level == ms_level))
    if idx.size == 0:
        raise InstrumentProtocolError(f"No MS{ms_level} spectra in that RT window.")
    targets = np.asarray(mz, dtype=float)
    tols = np.array([analysis.tolerance_da(m, tolerance, tolerance_unit) for m in targets])
    lows, highs = targets - tols, targets + tols
    traces = np.zeros((targets.size, idx.size))
    pos = {int(k): j for j, k in enumerate(idx)}
    for i, mzs, inten in run.iter_peaks(idx.tolist()):
        if mzs.size == 0:
            continue
        cs = np.concatenate(([0.0], np.cumsum(inten)))
        lo = np.searchsorted(mzs, lows, side="left")
        hi = np.searchsorted(mzs, highs, side="right")
        traces[:, pos[i]] = cs[hi] - cs[lo]
    rt = t.rt_min[idx]
    warnings: list[str] = []
    if ms_level == 1 and tolerance_unit == "ppm" and tolerance < 2:
        warnings.append("Tolerances below 2 ppm can miss the peak if the mass calibration drifted.")
    out = []
    for k, target in enumerate(targets):
        y = traces[k]
        pk = analysis.integrate_apex_peak(rt, y)
        xs, ys = analysis.downsample_max(rt, y, max_points)
        out.append(
            XICTrace(
                target_mz=float(target),
                tolerance_da=float(tols[k]),
                mz_window=(float(lows[k]), float(highs[k])),
                apex_rt_min=round(pk.apex_rt_min, 4) if pk else None,
                apex_intensity=pk.apex_intensity if pk else None,
                peak_start_rt_min=round(pk.start_rt_min, 4) if pk else None,
                peak_end_rt_min=round(pk.end_rt_min, 4) if pk else None,
                area=pk.area if pk else None,
                fwhm_s=round(pk.fwhm_s, 2) if pk and pk.fwhm_s is not None else None,
                points_across_peak=pk.points_across_peak if pk else None,
                total_area=analysis.trapezoid(y, rt),
                rt_min=[round(x, 4) for x in xs],
                intensity=[float(f"{v:.5g}") for v in ys],
            )
        )
        if pk is None:
            warnings.append(f"No signal at m/z {target:.4f} ± {tols[k]:.4f} in the selected spectra.")
        elif pk.points_across_peak < 5:
            warnings.append(
                f"m/z {target:.4f}: only {pk.points_across_peak} points across the peak; the area is imprecise."
            )
    header = ["rt_min"] + [f"xic_{m:.4f}" for m in targets]
    saved = _save(save_path, header, [rt, *traces])
    return XICResult(
        path=drv.display_path(run),
        simulated=drv.simulated,
        ms_level=ms_level,
        spectra_used=int(idx.size),
        rt_window_min=_window(rt_start_min, rt_end_min),
        traces=out,
        saved_to=saved,
        warnings=warnings,
    )


@mcp.tool(**READ, timeout=600)
def get_spectrum(
    path: PathArg = None,
    index: Annotated[int | None, Field(ge=0, description="0-based spectrum index in the file")] = None,
    scan_number: Annotated[
        int | None, Field(ge=0, description="Native scan number (e.g. Thermo scan=N)")
    ] = None,
    native_id: Annotated[str | None, Field(description="Exact native spectrum id")] = None,
    rt_min: Annotated[
        float | None, Field(ge=0, description="Pick the spectrum nearest this RT (min)")
    ] = None,
    ms_level: Annotated[
        int | None, Field(ge=1, le=10, description="With rt_min: MS level to pick (default 1)")
    ] = None,
    top_n: Annotated[int, Field(ge=0, le=2000, description="Number of most intense peaks to return")] = 50,
    mz_min: Annotated[float | None, Field(ge=0, description="Only consider peaks above this m/z")] = None,
    mz_max: Annotated[float | None, Field(ge=0, description="Only consider peaks below this m/z")] = None,
    save_path: SavePath = None,
) -> SpectrumResult:
    """Read one spectrum, chosen by `index`, `scan_number`, `native_id` or nearest `rt_min`
    (give exactly one). Returns MS level, RT, polarity, centroid/profile, precursor m/z and
    charge for MS2, a summary (peak count, TIC, base peak, m/z range) and the `top_n` most
    intense peaks; `save_path` writes the full peak list to CSV. For profile spectra the top
    peaks are local maxima of the profile."""
    drv, run = _open(path)
    t = run.table
    given = [x is not None for x in (index, scan_number, native_id, rt_min)]
    if sum(given) != 1:
        raise InstrumentProtocolError("Give exactly one of index, scan_number, native_id or rt_min.")
    if index is not None:
        if index >= len(t):
            raise InstrumentProtocolError(f"index {index} is out of range (0-{len(t) - 1}).")
        i = index
    elif scan_number is not None:
        hits = np.flatnonzero(t.scan == scan_number)
        if hits.size == 0:
            raise InstrumentProtocolError(
                f"No spectrum with scan number {scan_number}"
                + (
                    ""
                    if (t.scan >= 0).any()
                    else " (this file's native ids carry no scan numbers; use index)"
                )
                + "."
            )
        i = int(hits[0])
    elif native_id is not None:
        try:
            i = t.native_id.index(native_id)
        except ValueError as exc:
            raise InstrumentProtocolError(f"No spectrum with native id {native_id!r}.") from exc
    else:
        level = ms_level or 1
        cand = np.flatnonzero((t.ms_level == level) & np.isfinite(t.rt_min))
        if cand.size == 0:
            raise InstrumentProtocolError(f"The run has no MS{level} spectra.")
        i = int(cand[np.argmin(np.abs(t.rt_min[cand] - rt_min))])
    mzs, inten = run.read_peaks(i)
    notes: list[str] = []
    window = np.ones(mzs.size, dtype=bool)
    if mz_min is not None:
        window &= mzs >= mz_min
    if mz_max is not None:
        window &= mzs <= mz_max
    wm, wi = mzs[window], inten[window]
    centroided = int(t.centroided[i])
    pm, pi = analysis.local_maxima(wm, wi) if centroided == 0 else (wm, wi)
    if centroided == 0:
        notes.append("Profile spectrum: top peaks are local maxima of the profile, not centroids.")
    order = np.argsort(pi)[::-1][:top_n]
    base = float(pi[order[0]]) if order.size else 0.0
    peaks = [
        Peak(
            mz=round(float(pm[k]), 5),
            intensity=float(f"{pi[k]:.6g}"),
            relative_intensity_percent=round(100.0 * float(pi[k]) / base, 2) if base > 0 else 0.0,
        )
        for k in order
    ]
    level = int(t.ms_level[i])
    precursor = None
    if level > 1:
        precursor = PrecursorInfo(
            mz=_opt(t.precursor_mz[i]),
            charge=int(t.precursor_charge[i]) or None,
            intensity=_opt(t.precursor_intensity[i]),
            precursor_scan=int(t.precursor_scan[i]) if t.precursor_scan[i] >= 0 else None,
        )
    if rt_min is not None and abs(float(t.rt_min[i]) - rt_min) > 0.5:
        notes.append(f"The nearest MS{level} spectrum is {abs(float(t.rt_min[i]) - rt_min):.2f} min away.")
    saved = _save(save_path, ["mz", "intensity"], [mzs, inten])
    k = int(np.argmax(wi)) if wi.size else None
    return SpectrumResult(
        path=drv.display_path(run),
        simulated=drv.simulated,
        index=i,
        native_id=t.native_id[i],
        scan_number=int(t.scan[i]) if t.scan[i] >= 0 else None,
        ms_level=level,
        rt_min=round(float(t.rt_min[i]), 5) if np.isfinite(t.rt_min[i]) else None,
        polarity={1: "positive", -1: "negative"}.get(int(t.polarity[i]), "unknown"),
        spectrum_type={1: "centroid", 0: "profile"}.get(centroided, "unknown"),
        filter_string=t.filter_string[i] or None,
        injection_time_ms=_opt(t.injection_time_ms[i]),
        precursor=precursor,
        peak_count=int(wm.size),
        tic=float(wi.sum()),
        base_peak_mz=round(float(wm[k]), 5) if k is not None else None,
        base_peak_intensity=float(wi[k]) if k is not None else None,
        mz_range=(float(wm[0]), float(wm[-1])) if wm.size else None,
        top_peaks=peaks,
        saved_to=saved,
        notes=notes,
    )


@mcp.tool(**READ, timeout=900)
def find_ms2_scans(
    precursor_mz: Annotated[float, Field(gt=0, description="Precursor m/z to look for")],
    path: PathArg = None,
    tolerance: Tolerance = 10.0,
    tolerance_unit: TolUnit = "ppm",
    rt_start_min: RtStart = None,
    rt_end_min: RtEnd = None,
    charge: Annotated[int | None, Field(ge=1, le=100, description="Only this precursor charge")] = None,
    max_results: Annotated[int, Field(ge=1, le=5000)] = 100,
) -> MS2Search:
    """Find the MS2 (MSn) spectra whose precursor m/z is within ± tolerance of `precursor_mz`,
    optionally within an RT window and for one charge state. Returns index, scan number, RT,
    precursor m/z, error in ppm, charge and intensity; open any hit with get_spectrum."""
    drv, run = _open(path)
    t = run.table
    tol = analysis.tolerance_da(precursor_mz, tolerance, tolerance_unit)
    mask = _rt_mask(t, rt_start_min, rt_end_min) & (t.ms_level >= 2) & np.isfinite(t.precursor_mz)
    mask &= np.abs(t.precursor_mz - precursor_mz) <= tol
    if charge is not None:
        mask &= t.precursor_charge == charge
    idx = np.flatnonzero(mask)
    matches = [
        MS2Match(
            index=int(i),
            native_id=t.native_id[i],
            scan_number=int(t.scan[i]) if t.scan[i] >= 0 else None,
            rt_min=round(float(t.rt_min[i]), 4) if np.isfinite(t.rt_min[i]) else None,
            precursor_mz=float(t.precursor_mz[i]),
            delta_ppm=round((float(t.precursor_mz[i]) - precursor_mz) / precursor_mz * 1e6, 2),
            charge=int(t.precursor_charge[i]) or None,
            precursor_intensity=_opt(t.precursor_intensity[i]),
            base_peak_mz=_opt(t.base_peak_mz[i]),
        )
        for i in idx[:max_results]
    ]
    return MS2Search(
        path=drv.display_path(run),
        simulated=drv.simulated,
        precursor_mz=precursor_mz,
        tolerance_da=tol,
        rt_window_min=_window(rt_start_min, rt_end_min),
        total_matches=int(idx.size),
        matches=matches,
        truncated=idx.size > max_results,
    )


def _rolling_median(y: np.ndarray, half: int = 10) -> np.ndarray:
    out = np.empty_like(y)
    for i in range(y.size):
        out[i] = np.median(y[max(0, i - half) : i + half + 1])
    return out


@mcp.tool(**READ, timeout=900)
def summarise_run(path: PathArg = None) -> RunQC:
    """Quick QC of a run: MS1/MS2 counts, TIC stability (CV, spray dropouts), where the signal
    elutes, cycle time, MS2 scans per cycle, median injection times and how often MS2 hit the
    maximum injection time, and precursor charge states. Returns plain-language warnings."""
    drv, run = _open(path)
    t = run.table
    ms1 = np.flatnonzero(t.ms_level == 1)
    ms2 = np.flatnonzero(t.ms_level == 2)
    warnings: list[str] = []
    rt = t.rt_min[np.isfinite(t.rt_min)]
    tic_median = tic_cv = None
    drop_rts: list[float] = []
    quartiles = None
    cycle = None
    per_cycle_med = per_cycle_mean = None
    per_cycle_max = None
    if ms1.size:
        tic = np.nan_to_num(t.tic[ms1], nan=0.0)
        rt1 = t.rt_min[ms1]
        tic_median = float(np.median(tic))
        if tic.mean() > 0:
            tic_cv = round(float(np.std(tic) / np.mean(tic) * 100.0), 1)
        if tic.size >= 5:
            local = _rolling_median(tic)
            drops = np.flatnonzero(tic < 0.2 * local)
            drop_rts = [round(float(rt1[k]), 3) for k in drops]
            if drops.size:
                warnings.append(
                    f"{drops.size} MS1 scan(s) with the TIC below 20 % of the local median (first at "
                    f"{drop_rts[0]:.2f} min): possible electrospray instability or an air bubble."
                )
        total = tic.sum()
        if total > 0:
            cum = np.cumsum(tic) / total
            quartiles = [
                round(float(rt1[min(np.searchsorted(cum, q), rt1.size - 1)]), 3) for q in (0.25, 0.5, 0.75)
            ]
        if ms1.size >= 2:
            cycle = round(float(np.median(np.diff(rt1))) * 60.0, 3)
            counts = np.diff(np.append(ms1, len(t))) - 1  # spectra between consecutive MS1 scans
            per_cycle_med = float(np.median(counts))
            per_cycle_mean = round(float(np.mean(counts)), 2)
            per_cycle_max = int(counts.max())
    else:
        warnings.append("The run has no MS1 spectra.")
    it1 = t.injection_time_ms[ms1] if ms1.size else np.array([])
    it2 = t.injection_time_ms[ms2] if ms2.size else np.array([])
    med_it1 = float(np.nanmedian(it1)) if np.isfinite(it1).any() else None
    med_it2 = float(np.nanmedian(it2)) if np.isfinite(it2).any() else None
    at_max = None
    if med_it2 is not None:
        finite = it2[np.isfinite(it2)]
        if finite.max() > 1.05 * finite.min():  # fixed fill / TIMS accumulation times are not AGC-limited
            at_max = round(float(np.mean(finite >= 0.99 * finite.max()) * 100.0), 1)
        if at_max is not None and at_max > 50:
            warnings.append(
                f"{at_max:.0f} % of MS2 scans reached the maximum injection time: MS2 is ion-limited "
                "(low sample amount or poor spray)."
            )
    charges: dict[str, int] = {}
    if ms2.size:
        vals, counts = np.unique(t.precursor_charge[ms2], return_counts=True)
        charges = {("unknown" if v == 0 else str(int(v))): int(c) for v, c in zip(vals, counts, strict=True)}
    if ms1.size and not ms2.size:
        warnings.append("No MS2 spectra: an MS1-only (full-scan) acquisition.")
    if tic_cv is not None and tic_cv > 150:
        warnings.append(f"The MS1 TIC varies strongly (CV {tic_cv:.0f} %).")
    return RunQC(
        path=drv.display_path(run),
        simulated=drv.simulated,
        rt_start_min=float(rt.min()) if rt.size else None,
        rt_end_min=float(rt.max()) if rt.size else None,
        ms1_spectra=int(ms1.size),
        ms2_spectra=int(ms2.size),
        msn_higher_spectra=int(np.sum(t.ms_level > 2)),
        tic_median=tic_median,
        tic_cv_percent=tic_cv,
        tic_dropouts=len(drop_rts),
        tic_dropout_rts_min=drop_rts[:20],
        tic_elution_quartiles_min=quartiles,
        median_cycle_time_s=cycle,
        ms2_per_cycle_median=per_cycle_med,
        ms2_per_cycle_mean=per_cycle_mean,
        ms2_per_cycle_max=per_cycle_max,
        median_ms1_injection_time_ms=med_it1,
        median_ms2_injection_time_ms=med_it2,
        ms2_at_max_injection_time_percent=at_max,
        ms2_precursor_charges=charges,
        warnings=warnings,
    )


@mcp.tool(**CONTROL, timeout=7200)
def convert_to_mzml(
    path: Annotated[
        str, Field(description="Vendor file or folder (from list_runs), relative to the data folder")
    ],
    output_folder: Annotated[
        str | None,
        Field(description="Folder for the .mzML, inside the data folder (default: next to the input)"),
    ] = None,
    peak_picking: Annotated[
        bool, Field(description="Centroid with the vendor algorithm during conversion")
    ] = True,
    gzip: Annotated[bool, Field(description="Write .mzML.gz")] = False,
    overwrite: Annotated[bool, Field(description="Replace an existing output file")] = False,
    dry_run: Annotated[bool, Field(description="Only show the command that would be run")] = False,
    timeout_s: Annotated[float, Field(gt=0, le=86400, description="Give up after this many seconds")] = 1800,
) -> ConversionReport:
    """Convert a vendor file (Thermo .raw, Waters .raw, Agilent .d, SCIEX .wiff, Shimadzu .lcd,
    Bruker .d) to mzML with the converter the user installed (ThermoRawFileParser, ProteoWizard
    msconvert, or msconvert in Docker; chosen with --option converter=...). Writes a new file in
    the data folder. Can take minutes; fails with install instructions if no converter is set up."""
    server.check("max_conversion_time_s", timeout_s, "conversion timeout")
    drv = _drv()
    if drv.simulated_run is not None:
        raise InstrumentProtocolError(
            "Simulation mode has no vendor files to convert. Start the server with --address <data folder>."
        )
    src = drv.root.resolve(path)
    fmt = detect_format(src)
    if fmt is None:
        raise InstrumentProtocolError(f"{path!r} is not a recognised MS data file or vendor folder.")
    out_dir = drv.root.resolve(output_folder, must_exist=False) if output_folder else src.parent
    if out_dir.exists() and not out_dir.is_dir():
        raise InstrumentProtocolError(f"output_folder {output_folder!r} is a file.")
    plan = plan_conversion(
        src,
        fmt,
        out_dir,
        converter=drv.converter.get("converter", "auto"),
        converter_path=drv.converter.get("converter_path"),
        docker_image=drv.converter.get("docker_image"),
        peak_picking=peak_picking,
        gzip=gzip,
    )
    drv.root.resolve(drv.root.relative(plan.output), must_exist=False)  # sandbox check on the output
    report = ConversionReport(
        input=drv.root.relative(src),
        detected_format=fmt,
        converter=plan.converter,
        command=plan.argv,
        output=drv.root.relative(plan.output),
        dry_run=dry_run,
        success=False,
        duration_s=None,
        output_size_mb=None,
        return_code=None,
        log_tail="",
        notes=list(plan.notes),
    )
    if plan.output.exists() and not overwrite:
        raise InstrumentProtocolError(
            f"{report.output} already exists. Read it directly, or pass overwrite=true to convert again."
        )
    if dry_run:
        report.notes.append("Dry run: nothing was executed.")
        return report
    out_dir.mkdir(parents=True, exist_ok=True)
    if plan.output.exists():
        plan.output.unlink()
    server.audit.event(f"convert: {' '.join(plan.argv)}", "converter")
    res = run_conversion(plan, timeout_s)
    report.duration_s = round(res.duration_s, 1)
    report.return_code = res.returncode
    report.log_tail = (res.stderr_tail or res.stdout_tail).strip()
    if res.timed_out:
        report.notes.append(f"Timed out after {timeout_s:g} s; the converter was stopped.")
        return report
    if plan.output.exists():
        report.success = res.returncode == 0
        report.output_size_mb = round(plan.output.stat().st_size / 1e6, 2)
    else:
        report.notes.append(f"The expected output {report.output} was not created; see log_tail.")
    return report


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
