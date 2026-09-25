"""MCP server for Ocean Insight / Ocean Optics spectrometers (python-seabreeze)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Annotated, Literal

import numpy as np
from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_ocean_spectrometer import analysis
from labmcp_ocean_spectrometer.driver import (
    SATURATION_FRACTION,
    OceanSpectrometer,
    Ratio,
    Spectrum,
    list_seabreeze_devices,
    open_seabreeze,
)
from labmcp_ocean_spectrometer.simulator import MODELS, FakeSpectrometer

BACKENDS = {"cseabreeze", "pyseabreeze"}


def connect(ctx: ConnectContext) -> OceanSpectrometer:
    backend = (ctx.option("backend", "cseabreeze") or "cseabreeze").lower()
    if backend not in BACKENDS:
        raise InstrumentProtocolError(
            f"--option backend must be cseabreeze or pyseabreeze (got {backend!r})."
        )
    initial_ms = float(ctx.option("integration_ms", "10") or 10)
    if ctx.simulate:
        model = (ctx.option("sim_model", "USB2000PLUS") or "USB2000PLUS").upper()
        if model not in MODELS:
            raise InstrumentProtocolError(f"--option sim_model must be one of {sorted(MODELS)}.")
        spec = FakeSpectrometer(
            model=model, source=(ctx.option("sim_source", "halogen") or "halogen").lower()
        )
        backend = "simulator"
    else:
        spec = open_seabreeze(ctx.address, backend)
    try:
        return OceanSpectrometer(spec, backend=backend, audit=ctx.audit, initial_integration_ms=initial_ms)
    except Exception:
        try:
            spec.close()
        except Exception:
            pass
        raise


server = InstrumentServer(
    "Ocean Insight Spectrometer (seabreeze)",
    connect=connect,
    package="labmcp-ocean-spectrometer",
    instructions="""
Controls an Ocean Insight / Ocean Optics spectrometer (USB2000+, USB4000, Flame, HR2000+/HR4000,
Maya2000 Pro, QE Pro, STS, NIRQuest, ...) through python-seabreeze. Intensities are raw detector
counts (not irradiance-calibrated).
- Start with `get_device_info`, then `auto_integration_time` (or `set_integration_time`) so the
  brightest peak sits at ~70-85 % of saturation. Saturated spectra are invalid.
- Absorbance / transmittance workflow: (1) ask the user to BLOCK the light path, then
  `store_dark_reference`; (2) ask them to put the BLANK (solvent / empty cuvette) in the beam,
  then `store_reference`; (3) ask them to insert the SAMPLE, then `measure_absorbance` or
  `measure_transmittance`. You cannot see the sample holder - wait for the user's confirmation
  before each step.
- Re-take dark and reference after changing integration time, boxcar or corrections, and
  periodically (lamps drift). Stored references are lost on reconnect.
- Absorbance above ~2 AU is unreliable (stray light); dilute the sample.
- Use averaging (`scans_to_average`) to reduce noise; boxcar smoothing broadens narrow peaks.
""",
    limits=[
        Limit("max_integration_time_ms", 10000, "ms", "Longest integration time an agent may set"),
        Limit(
            "max_acquisition_duration_s", 300, "s", "Longest single acquisition (scans x integration time)"
        ),
        Limit(
            "min_tec_setpoint_c", -20, "°C", "Coldest detector TEC setpoint (TE-cooled models)", kind="min"
        ),
    ],
    address_help="""\
  (omit)            use the only connected spectrometer
  USB2+H01234       the spectrometer with this serial number (see list_spectrometers)""",
    option_help={
        "backend": "cseabreeze (default, falls back to pyseabreeze) or pyseabreeze",
        "integration_ms": "integration time set on connect (default 10 ms, clamped to the device range)",
        "sim_model": "simulated model: USB2000PLUS (default) or QE-PRO (has a TEC)",
        "sim_source": "simulated light source: halogen (default), led or hg-ar",
    },
)
mcp = server.mcp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- models


class PeakModel(BaseModel):
    wavelength_nm: float
    height: float = Field(description="Value at the peak (counts, AU or %T)")
    prominence: float
    fwhm_nm: float | None = Field(
        description="Full width at half prominence; None if the peak runs off the edge"
    )


class DeviceInfo(BaseModel):
    model: str
    serial: str
    backend: str
    pixels: int
    wavelength_min_nm: float
    wavelength_max_nm: float
    integration_time_ms: float
    integration_time_min_ms: float
    integration_time_max_ms: float
    saturation_counts: float = Field(description="max_intensity: raw counts at full scale")
    electric_dark_correction: bool
    nonlinearity_correction: bool
    has_tec: bool
    dark_stored: str | None = Field(description="Timestamp of the stored dark, if any")
    reference_stored: str | None = Field(description="Timestamp of the stored reference, if any")


class SpectrumResult(BaseModel):
    model: str
    serial: str
    timestamp: str
    integration_time_ms: float
    scans_to_average: int
    boxcar_half_width: int
    correct_dark_counts: bool
    correct_nonlinearity: bool
    dark_subtracted: bool
    pixels: int
    points_returned: int
    wavelength_nm: list[float] = Field(description="Bin-averaged wavelengths (downsampled to max_points)")
    intensity_counts: list[float | None] = Field(description="Bin-averaged counts (downsampled)")
    max_counts: float
    max_at_nm: float
    min_counts: float
    mean_counts: float
    saturation_counts: float
    peak_raw_percent_of_saturation: float = Field(
        description="Highest raw count in any scan, % of saturation"
    )
    saturated: bool
    saturated_pixels: int
    peaks: list[PeakModel] = Field(description="Most prominent peaks, found on the full-resolution spectrum")
    warnings: list[str]
    saved_to: str | None


class ReferenceSummary(BaseModel):
    kind: Literal["dark", "reference"]
    timestamp: str
    integration_time_ms: float
    scans_to_average: int
    boxcar_half_width: int
    correct_dark_counts: bool
    correct_nonlinearity: bool
    mean_counts: float
    max_counts: float
    max_at_nm: float
    peak_raw_percent_of_saturation: float
    saturated_pixels: int
    warnings: list[str]


class WavelengthValue(BaseModel):
    wavelength_nm: float
    value: float | None = Field(description="Interpolated value; None if the pixels there are invalid")


class RatioResult(BaseModel):
    kind: Literal["absorbance", "transmittance"]
    units: str = Field(description="AU (absorbance, -log10 T) or % (transmittance)")
    timestamp: str
    integration_time_ms: float
    scans_to_average: int
    dark_timestamp: str
    reference_timestamp: str
    pixels: int
    valid_pixels: int = Field(description="Pixels where the reference had enough unsaturated signal")
    points_returned: int
    wavelength_nm: list[float]
    values: list[float | None] = Field(description="Downsampled values; None where invalid")
    at_wavelengths: list[WavelengthValue]
    max_value: float | None
    max_at_nm: float | None
    peaks: list[PeakModel] = Field(description="Absorbance maxima, or transmittance minima (dips)")
    warnings: list[str]
    saved_to: str | None


# ---------------------------------------------------------------- helpers

MaxPoints = Annotated[int, Field(ge=10, le=5000, description="Maximum points returned (bin-averaged)")]
NumPeaks = Annotated[int, Field(ge=0, le=50, description="Number of peaks to report")]
Scans = Annotated[int, Field(ge=1, le=1000, description="Spectra averaged together")]
Boxcar = Annotated[
    int, Field(ge=0, le=100, description="Boxcar smoothing: pixels averaged on EACH side (0 = off)")
]
WlMin = Annotated[float | None, Field(ge=0, description="Only report/search above this wavelength (nm)")]
WlMax = Annotated[float | None, Field(ge=0, description="Only report/search below this wavelength (nm)")]
SavePath = Annotated[str | None, Field(description="Optional CSV path for the full-resolution data")]


def _window(wl: np.ndarray, lo: float | None, hi: float | None) -> np.ndarray:
    mask = np.ones_like(wl, dtype=bool)
    if lo is not None:
        mask &= wl >= lo
    if hi is not None:
        mask &= wl <= hi
    if not mask.any():
        raise InstrumentProtocolError(
            f"No pixels between {lo} and {hi} nm (this spectrometer covers {wl[0]:.1f}-{wl[-1]:.1f} nm)."
        )
    return mask


def _peaks(
    wl: np.ndarray,
    y: np.ndarray,
    n: int,
    *,
    minima: bool = False,
    min_prominence_fraction: float = 0.05,
    min_separation_nm: float = 1.0,
) -> list[PeakModel]:
    if n <= 0:
        return []
    data = -y if minima else y
    finite = data[np.isfinite(data)]
    if finite.size < 3:
        return []
    found = analysis.find_peaks(
        wl,
        data,
        max_peaks=n,
        min_prominence=min_prominence_fraction * float(finite.max() - finite.min()),
        min_separation_nm=min_separation_nm,
    )
    return [
        PeakModel(
            wavelength_nm=round(p.wavelength_nm, 3),
            height=float(-p.height if minima else p.height),
            prominence=p.prominence,
            fwhm_nm=None if p.fwhm_nm is None else round(p.fwhm_nm, 3),
        )
        for p in found
    ]


def _intensity_warnings(drv: OceanSpectrometer, s: Spectrum) -> list[str]:
    warnings = []
    frac = s.peak_raw_counts / drv.max_intensity
    if s.saturated_pixels:
        warnings.append(
            f"{s.saturated_pixels} pixels are saturated: the spectrum is invalid there. Reduce the integration "
            "time (auto_integration_time) or attenuate the light."
        )
    elif frac > 0.9:
        warnings.append(
            f"Peak at {100 * frac:.0f}% of saturation: the detector is nonlinear near full scale."
        )
    elif frac < 0.1:
        warnings.append(f"Weak signal (peak {100 * frac:.1f}% of saturation): increase the integration time.")
    return warnings


def _check_acquisition(drv: OceanSpectrometer, scans: int) -> None:
    server.check(
        "max_acquisition_duration_s", scans * drv.integration_time_ms / 1000.0, "acquisition duration"
    )


# ---------------------------------------------------------------- tools


@mcp.tool(**READ)
def list_spectrometers() -> list[dict[str, str | bool]]:
    """List the Ocean spectrometers seabreeze can see (model, serial number, whether open) and
    which one this server is connected to. Use a serial number as `--address` to pick one."""
    connected = server.driver.serial if server.connected else None
    if server.settings.simulate:
        devices = [
            {"model": d.model, "serial_number": d.serial_number, "is_open": d.serial_number == connected}
            for d in FakeSpectrometer.list_devices()
        ]
    else:
        backend = (
            server.driver.backend
            if server.connected
            else (server.settings.options.get("backend") or "cseabreeze")
        )
        devices = list_seabreeze_devices(backend)
    for d in devices:
        d["connected_to_this_server"] = d["serial_number"] == connected
    return devices


@mcp.tool(**READ)
def get_device_info() -> DeviceInfo:
    """Model, serial, pixel count, wavelength range, integration-time limits, saturation level,
    supported corrections, TEC presence, and which dark/reference spectra are stored."""
    drv = server.driver
    lo, hi = drv.integration_limits_us
    return DeviceInfo(
        model=drv.model,
        serial=drv.serial,
        backend=drv.backend,
        pixels=len(drv.wavelengths_nm),
        wavelength_min_nm=round(float(drv.wavelengths_nm[0]), 3),
        wavelength_max_nm=round(float(drv.wavelengths_nm[-1]), 3),
        integration_time_ms=drv.integration_time_ms,
        integration_time_min_ms=lo / 1000.0,
        integration_time_max_ms=hi / 1000.0,
        saturation_counts=drv.max_intensity,
        electric_dark_correction=drv.supports_dark_correction,
        nonlinearity_correction=drv.supports_nonlinearity_correction,
        has_tec=drv.has_tec,
        dark_stored=drv.dark.timestamp if drv.dark else None,
        reference_stored=drv.reference.timestamp if drv.reference else None,
    )


@mcp.tool(**CONTROL)
def set_integration_time(
    integration_time_ms: Annotated[float, Field(gt=0, le=3_600_000, description="Integration time in ms")],
) -> dict[str, float | str]:
    """Set the detector integration time (ms), within the device limits from `get_device_info`.
    Stored dark/reference spectra become unusable until re-taken at the new time."""
    server.check("max_integration_time_ms", integration_time_ms, "integration time")
    drv = server.driver
    actual = drv.set_integration_time_ms(integration_time_ms)
    note = (
        "Re-take the dark and reference spectra before measuring absorbance."
        if drv.dark or drv.reference
        else ""
    )
    return {"integration_time_ms": actual, "note": note}


@mcp.tool(**READ, timeout=600)
def acquire_spectrum(
    scans_to_average: Scans = 1,
    boxcar_half_width: Boxcar = 0,
    correct_dark_counts: Annotated[
        bool, Field(description="Subtract the mean of the optically masked (electric dark) pixels")
    ] = False,
    correct_nonlinearity: Annotated[
        bool, Field(description="Apply the EEPROM nonlinearity correction")
    ] = False,
    subtract_stored_dark: Annotated[
        bool, Field(description="Subtract the stored dark spectrum (same settings required)")
    ] = False,
    wavelength_min_nm: WlMin = None,
    wavelength_max_nm: WlMax = None,
    max_points: MaxPoints = 500,
    num_peaks: NumPeaks = 5,
    save_path: SavePath = None,
) -> SpectrumResult:
    """Acquire an intensity spectrum (raw counts) with optional averaging, boxcar smoothing and
    corrections. Returns downsampled (wavelength, counts), summary statistics, the most
    prominent peaks with FWHM, and a saturation check; `save_path` writes the full spectrum."""
    drv = server.driver
    _check_acquisition(drv, scans_to_average)
    s = drv.acquire(scans_to_average, boxcar_half_width, correct_dark_counts, correct_nonlinearity)
    if subtract_stored_dark:
        if drv.dark is None:
            raise InstrumentProtocolError("No dark spectrum stored; run store_dark_reference first.")
        if drv.dark.settings() != s.settings():
            raise InstrumentProtocolError(
                "The stored dark was taken with different settings (integration time, boxcar or corrections)."
            )
        s = dataclasses.replace(s, counts=s.counts - drv.dark.counts)
    drv.last = s
    wl = s.wavelengths_nm
    mask = _window(wl, wavelength_min_nm, wavelength_max_nm)
    xw, yw = wl[mask], s.counts[mask]
    xs, ys = analysis.downsample(xw, yw, max_points)
    saved = None
    if save_path:
        saved = analysis.write_csv(save_path, ["wavelength_nm", "counts"], [wl, s.counts])
    imax = int(np.argmax(yw))
    return SpectrumResult(
        model=drv.model,
        serial=drv.serial,
        timestamp=s.timestamp,
        integration_time_ms=s.integration_time_ms,
        scans_to_average=s.scans_to_average,
        boxcar_half_width=s.boxcar_half_width,
        correct_dark_counts=s.correct_dark_counts,
        correct_nonlinearity=s.correct_nonlinearity,
        dark_subtracted=subtract_stored_dark,
        pixels=len(wl),
        points_returned=len(xs),
        wavelength_nm=[round(x, 3) for x in xs],
        intensity_counts=[None if y is None else round(y, 1) for y in ys],
        max_counts=float(yw[imax]),
        max_at_nm=round(float(xw[imax]), 3),
        min_counts=float(yw.min()),
        mean_counts=float(yw.mean()),
        saturation_counts=drv.max_intensity,
        peak_raw_percent_of_saturation=round(100.0 * s.peak_raw_counts / drv.max_intensity, 2),
        saturated=bool(s.saturated_pixels),
        saturated_pixels=s.saturated_pixels,
        peaks=_peaks(xw, yw, num_peaks),
        warnings=_intensity_warnings(drv, s),
        saved_to=saved,
    )


def _reference_summary(
    drv: OceanSpectrometer, kind: Literal["dark", "reference"], s: Spectrum
) -> ReferenceSummary:
    warnings = []
    frac = s.peak_raw_counts / drv.max_intensity
    if kind == "dark":
        edark = s.electric_dark_counts
        level = float(np.median(s.counts)) if edark is None or s.correct_dark_counts else edark
        if float(s.counts.max()) - level > 0.05 * drv.max_intensity:
            warnings.append(
                "The dark spectrum contains light (peak well above the dark level). Was the light path "
                "blocked? If not, block it and store the dark again."
            )
    else:
        warnings = _intensity_warnings(drv, s)
        if s.saturated_pixels:
            warnings.append("Saturated reference pixels are excluded from absorbance/transmittance.")
    imax = int(np.argmax(s.counts))
    return ReferenceSummary(
        kind=kind,
        timestamp=s.timestamp,
        integration_time_ms=s.integration_time_ms,
        scans_to_average=s.scans_to_average,
        boxcar_half_width=s.boxcar_half_width,
        correct_dark_counts=s.correct_dark_counts,
        correct_nonlinearity=s.correct_nonlinearity,
        mean_counts=float(s.counts.mean()),
        max_counts=float(s.counts[imax]),
        max_at_nm=round(float(s.wavelengths_nm[imax]), 3),
        peak_raw_percent_of_saturation=round(100.0 * frac, 2),
        saturated_pixels=s.saturated_pixels,
        warnings=warnings,
    )


@mcp.tool(**CONTROL, timeout=600)
def store_dark_reference(
    scans_to_average: Scans = 10,
    boxcar_half_width: Boxcar = 0,
    correct_dark_counts: Annotated[
        bool, Field(description="Electric-dark correction (use the same later)")
    ] = False,
    correct_nonlinearity: Annotated[
        bool, Field(description="Nonlinearity correction (use the same later)")
    ] = False,
) -> ReferenceSummary:
    """Record and store a DARK spectrum (kept in memory) for absorbance/transmittance.
    BEFORE calling, ask the user to BLOCK THE LIGHT PATH (close the shutter, switch the lamp off
    or cap the fiber) and wait for confirmation. Uses the present integration time."""
    drv = server.driver
    _check_acquisition(drv, scans_to_average)
    s = drv.store_dark(
        scans_to_average=scans_to_average,
        boxcar_half_width=boxcar_half_width,
        correct_dark_counts=correct_dark_counts,
        correct_nonlinearity=correct_nonlinearity,
    )
    return _reference_summary(drv, "dark", s)


@mcp.tool(**CONTROL, timeout=600)
def store_reference(
    scans_to_average: Scans = 10,
    boxcar_half_width: Boxcar = 0,
    correct_dark_counts: Annotated[bool, Field(description="Must match the stored dark")] = False,
    correct_nonlinearity: Annotated[bool, Field(description="Must match the stored dark")] = False,
) -> ReferenceSummary:
    """Record and store the REFERENCE (100 % transmission) spectrum in memory. BEFORE calling,
    ask the user to put the BLANK (solvent-filled or empty cuvette) in the beam with the light on,
    and wait for confirmation. Use the same settings as the dark."""
    drv = server.driver
    _check_acquisition(drv, scans_to_average)
    s = drv.store_reference(
        scans_to_average=scans_to_average,
        boxcar_half_width=boxcar_half_width,
        correct_dark_counts=correct_dark_counts,
        correct_nonlinearity=correct_nonlinearity,
    )
    return _reference_summary(drv, "reference", s)


def _ratio_result(
    drv: OceanSpectrometer,
    r: Ratio,
    wavelengths_nm: list[float] | None,
    wavelength_min_nm: float | None,
    wavelength_max_nm: float | None,
    max_points: int,
    num_peaks: int,
    save_path: str | None,
) -> RatioResult:
    assert drv.dark is not None and drv.reference is not None
    drv.last = r
    wl = r.sample.wavelengths_nm
    mask = _window(wl, wavelength_min_nm, wavelength_max_nm)
    xw, yw = wl[mask], r.values[mask]
    xs, ys = analysis.downsample(xw, yw, max_points)
    digits = 5 if r.kind == "absorbance" else 3
    at = []
    for w in wavelengths_nm or []:
        if not wl[0] <= w <= wl[-1]:
            at.append(WavelengthValue(wavelength_nm=w, value=None))
            continue
        j = int(np.searchsorted(wl, w))
        lo, hi = max(j - 1, 0), min(j, len(wl) - 1)
        ok = np.isfinite(r.values[lo]) and np.isfinite(r.values[hi])
        value = float(np.interp(w, wl[lo : hi + 1], r.values[lo : hi + 1])) if ok else None
        at.append(WavelengthValue(wavelength_nm=w, value=None if value is None else round(value, digits)))
    finite = np.isfinite(yw)
    warnings = _intensity_warnings(drv, r.sample) if r.sample.saturated_pixels else []
    if not finite.any():
        warnings.append("No valid pixels in the requested range (reference too dark or saturated there).")
    max_value = max_at = None
    if finite.any():
        k = int(np.nanargmax(yw))
        max_value, max_at = round(float(yw[k]), digits), round(float(xw[k]), 3)
        if r.kind == "absorbance" and max_value > 2.5:
            warnings.append(
                "Absorbance above ~2.5 AU is unreliable (stray light, too little light): dilute the sample."
            )
    saved = None
    if save_path:
        name = "absorbance_au" if r.kind == "absorbance" else "transmittance_percent"
        saved = analysis.write_csv(
            save_path,
            ["wavelength_nm", "dark_counts", "reference_counts", "sample_counts", name],
            [wl, drv.dark.counts, drv.reference.counts, r.sample.counts, r.values],
        )
    return RatioResult(
        kind=r.kind,
        units="AU" if r.kind == "absorbance" else "%",
        timestamp=r.sample.timestamp,
        integration_time_ms=r.sample.integration_time_ms,
        scans_to_average=r.sample.scans_to_average,
        dark_timestamp=drv.dark.timestamp,
        reference_timestamp=drv.reference.timestamp,
        pixels=len(wl),
        valid_pixels=int(r.valid.sum()),
        points_returned=len(xs),
        wavelength_nm=[round(x, 3) for x in xs],
        values=[None if y is None else round(y, digits) for y in ys],
        at_wavelengths=at,
        max_value=max_value,
        max_at_nm=max_at,
        peaks=_peaks(xw, yw, num_peaks, minima=r.kind == "transmittance", min_separation_nm=5.0),
        warnings=warnings,
        saved_to=saved,
    )


RatioWavelengths = Annotated[
    list[float] | None,
    Field(max_length=50, description="Wavelengths (nm) at which to report interpolated values"),
]


@mcp.tool(**READ, timeout=600)
def measure_absorbance(
    wavelengths_nm: RatioWavelengths = None,
    scans_to_average: Annotated[
        int | None, Field(ge=1, le=1000, description="Spectra averaged (default: as the reference)")
    ] = None,
    wavelength_min_nm: WlMin = None,
    wavelength_max_nm: WlMax = None,
    max_points: MaxPoints = 500,
    num_peaks: NumPeaks = 5,
    save_path: SavePath = None,
) -> RatioResult:
    """Measure the absorbance spectrum A = -log10((S - D) / (R - D)) of the sample now in the
    beam, using the stored dark D and reference R (same settings). Ask the user to insert the
    sample first. Also returns values at `wavelengths_nm` and the absorbance maxima."""
    drv = server.driver
    _check_acquisition(drv, scans_to_average or (drv.reference.scans_to_average if drv.reference else 1))
    r = drv.measure_ratio("absorbance", scans_to_average)
    return _ratio_result(
        drv, r, wavelengths_nm, wavelength_min_nm, wavelength_max_nm, max_points, num_peaks, save_path
    )


@mcp.tool(**READ, timeout=600)
def measure_transmittance(
    wavelengths_nm: RatioWavelengths = None,
    scans_to_average: Annotated[
        int | None, Field(ge=1, le=1000, description="Spectra averaged (default: as the reference)")
    ] = None,
    wavelength_min_nm: WlMin = None,
    wavelength_max_nm: WlMax = None,
    max_points: MaxPoints = 500,
    num_peaks: NumPeaks = 5,
    save_path: SavePath = None,
) -> RatioResult:
    """Measure the transmittance spectrum %T = 100 (S - D) / (R - D) of the sample now in the
    beam, using the stored dark and reference. Ask the user to insert the sample first. Also
    returns values at `wavelengths_nm` and the deepest transmission dips."""
    drv = server.driver
    _check_acquisition(drv, scans_to_average or (drv.reference.scans_to_average if drv.reference else 1))
    r = drv.measure_ratio("transmittance", scans_to_average)
    return _ratio_result(
        drv, r, wavelengths_nm, wavelength_min_nm, wavelength_max_nm, max_points, num_peaks, save_path
    )


@mcp.tool(**READ, timeout=120)
def find_peaks(
    max_peaks: Annotated[int, Field(ge=1, le=100, description="Maximum number of peaks")] = 10,
    min_prominence_fraction: Annotated[
        float, Field(ge=0, le=1, description="Minimum prominence as a fraction of the data range")
    ] = 0.05,
    min_separation_nm: Annotated[
        float, Field(ge=0, le=500, description="Minimum distance between peaks, nm")
    ] = 1.0,
    mode: Annotated[
        Literal["maxima", "minima"], Field(description="Peaks (maxima) or dips (minima)")
    ] = "maxima",
    wavelength_min_nm: WlMin = None,
    wavelength_max_nm: WlMax = None,
) -> dict[str, object]:
    """Find peaks (or dips) with position, height, prominence and FWHM in the most recent
    spectrum (intensity, absorbance or transmittance). Acquires a fresh intensity spectrum if
    none has been taken yet."""
    drv = server.driver
    last = drv.last
    if last is None:
        _check_acquisition(drv, 1)
        last = drv.last = drv.acquire()
    if isinstance(last, Ratio):
        kind, wl, y, ts = last.kind, last.sample.wavelengths_nm, last.values, last.sample.timestamp
    else:
        kind, wl, y, ts = "intensity", last.wavelengths_nm, last.counts, last.timestamp
    mask = _window(wl, wavelength_min_nm, wavelength_max_nm)
    peaks = _peaks(
        wl[mask],
        y[mask],
        max_peaks,
        minima=mode == "minima",
        min_prominence_fraction=min_prominence_fraction,
        min_separation_nm=min_separation_nm,
    )
    return {
        "spectrum": kind,
        "spectrum_timestamp": ts,
        "mode": mode,
        "peaks": [p.model_dump() for p in peaks],
    }


@mcp.tool(**CONTROL, timeout=600)
def auto_integration_time(
    target_min_percent: Annotated[
        float, Field(ge=10, le=95, description="Lower edge of the target band, % of saturation")
    ] = 70,
    target_max_percent: Annotated[
        float, Field(ge=15, le=97, description="Upper edge of the target band, % of saturation")
    ] = 85,
    max_iterations: Annotated[int, Field(ge=1, le=20, description="Maximum adjustment steps")] = 8,
    wavelength_min_nm: WlMin = None,
    wavelength_max_nm: WlMax = None,
) -> dict[str, object]:
    """Adjust the integration time until the brightest raw pixel (optionally within a wavelength
    window) is within the target band of saturation (default 70-85 %). Stays within the device
    limits and the `max_integration_time_ms` safety limit. Re-take dark/reference afterwards."""
    if target_min_percent >= target_max_percent:
        raise InstrumentProtocolError("target_min_percent must be below target_max_percent.")
    drv = server.driver
    window = None
    if wavelength_min_nm is not None or wavelength_max_nm is not None:
        window = (wavelength_min_nm or 0.0, wavelength_max_nm or 1e9)
    result = drv.auto_integration_time(
        target_min=target_min_percent / 100.0,
        target_max=target_max_percent / 100.0,
        max_iterations=max_iterations,
        max_ms=server.limits["max_integration_time_ms"],
        window=window,
    )
    last = result["history"][-1]["peak_fraction"] if result["history"] else None
    if not result["converged"]:
        if last is not None and last < target_min_percent / 100:
            result["message"] = (
                f"Signal too weak: at {result['integration_time_ms']:g} ms the peak is {100 * last:.1f}% of "
                "saturation. Increase the light level or the max_integration_time_ms limit."
            )
        else:
            result["message"] = "Did not converge within max_iterations; check the light source is stable."
    if drv.dark or drv.reference:
        result["note"] = "Integration time changed: re-take the dark and reference spectra."
    result["saturation_fraction_threshold"] = SATURATION_FRACTION
    return result


@mcp.tool(**READ)
def read_detector_temperature() -> dict[str, float | str]:
    """Read the detector temperature from the thermo-electric cooler (TE-cooled models such as
    the QE Pro, when seabreeze exposes the thermo_electric feature for them)."""
    return {"temperature_c": server.driver.tec_temperature_c(), "timestamp": _now()}


@mcp.tool(**HAZARD)
def set_detector_cooling(
    setpoint_c: Annotated[float, Field(ge=-40, le=30, description="Detector TEC setpoint, °C")],
) -> dict[str, float | str]:
    """Enable the detector thermo-electric cooler at `setpoint_c` (TE-cooled models only). The
    detector takes minutes to settle; dark current (and noise) drops as it cools. The TEC can only
    cool to roughly 15-40 °C below ambient (QE Pro manual). Checked against `min_tec_setpoint_c`."""
    server.check("min_tec_setpoint_c", setpoint_c, "TEC setpoint")
    drv = server.driver
    drv.set_tec(True, setpoint_c)
    return {"setpoint_c": setpoint_c, "temperature_c": drv.tec_temperature_c(), "timestamp": _now()}


@mcp.tool(**SAFETY)
def detector_cooling_off() -> dict[str, str | float]:
    """Switch the detector thermo-electric cooler off (the detector warms to ambient). Does
    nothing on spectrometers without a TEC."""
    drv = server.driver
    if not drv.has_tec:
        return {"status": f"The {drv.model} has no TEC; nothing to do."}
    drv.set_tec(False)
    return {"status": "TEC off", "temperature_c": drv.tec_temperature_c(), "timestamp": _now()}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
