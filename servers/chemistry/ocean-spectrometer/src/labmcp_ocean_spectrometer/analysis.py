"""Spectrum processing: boxcar smoothing, downsampling, peak finding with FWHM (numpy only)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def boxcar(y: np.ndarray, half_width: int) -> np.ndarray:
    """Moving average over ``2 * half_width + 1`` pixels (OceanView's "boxcar width" is the
    number of pixels on each side). Edges average over the pixels that exist."""
    if half_width <= 0:
        return np.asarray(y, dtype=float)
    y = np.asarray(y, dtype=float)
    n = y.size
    c = np.concatenate(([0.0], np.cumsum(y)))
    idx = np.arange(n)
    lo = np.clip(idx - half_width, 0, n)
    hi = np.clip(idx + half_width + 1, 0, n)
    return (c[hi] - c[lo]) / (hi - lo)


def downsample(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[list[float], list[float | None]]:
    """Average contiguous pixel bins so at most ``max_points`` remain. NaN (masked) pixels are
    ignored; a bin with no valid pixel becomes ``None``."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.size <= max_points:
        xs, ys = x, y
    else:
        bins = np.array_split(np.arange(x.size), max_points)
        xs = np.array([x[b].mean() for b in bins])
        ys = np.array([np.nanmean(y[b]) if np.isfinite(y[b]).any() else np.nan for b in bins])
    return [float(v) for v in xs], [float(v) if np.isfinite(v) else None for v in ys]


@dataclass
class Peak:
    wavelength_nm: float
    height: float
    prominence: float
    fwhm_nm: float | None
    pixel: int


def _crossing(y: np.ndarray, start: int, level: float, step: int) -> float | None:
    """Fractional index where ``y`` first drops below ``level`` walking from ``start``."""
    n = y.size
    i = start
    while 0 <= i + step < n:
        j = i + step
        if y[j] < level:
            # linear interpolation between i (>= level) and j (< level)
            return i + step * (y[i] - level) / (y[i] - y[j])
        i = j
    return None


def find_peaks(
    x_nm: np.ndarray,
    y: np.ndarray,
    *,
    max_peaks: int = 10,
    min_prominence: float | None = None,
    min_separation_nm: float = 0.0,
) -> list[Peak]:
    """Local maxima ranked by topographic prominence, with FWHM measured at half prominence.

    ``min_prominence`` defaults to 5 % of the data range. NaN values are ignored.
    """
    x = np.asarray(x_nm, dtype=float)
    y = np.asarray(y, dtype=float)
    finite = np.isfinite(y)
    if finite.sum() < 3:
        return []
    y = np.where(finite, y, np.nanmin(y))
    n = y.size
    if min_prominence is None:
        min_prominence = 0.05 * float(y.max() - y.min())
    pitch = float(np.median(np.abs(np.diff(x)))) or 1.0
    sep_px = max(1, int(round(min_separation_nm / pitch)))
    # candidates: pixels that are the maximum of their +-sep_px window (first pixel of a plateau)
    pad = np.pad(y, sep_px, mode="constant", constant_values=-np.inf)
    window = np.lib.stride_tricks.sliding_window_view(pad, 2 * sep_px + 1)
    is_max = (y >= window.max(axis=1)) & (y > np.concatenate(([-np.inf], y[:-1])))
    candidates = np.flatnonzero(is_max)
    peaks: list[Peak] = []
    for i in candidates:
        if i == 0 or i == n - 1:
            continue
        left_higher = np.flatnonzero(y[:i] > y[i])
        right_higher = np.flatnonzero(y[i + 1 :] > y[i])
        lo = left_higher[-1] + 1 if left_higher.size else 0
        hi = i + 1 + right_higher[0] if right_higher.size else n
        base = max(y[lo : i + 1].min(), y[i:hi].min())
        prominence = float(y[i] - base)
        if prominence < min_prominence or prominence <= 0:
            continue
        level = y[i] - prominence / 2.0
        left = _crossing(y, i, level, -1)
        right = _crossing(y, i, level, +1)
        pixels = np.arange(n)
        fwhm = None
        if left is not None and right is not None:
            fwhm = abs(float(np.interp(right, pixels, x) - np.interp(left, pixels, x)))
        # parabolic interpolation of the vertex
        denom = y[i - 1] - 2 * y[i] + y[i + 1]
        offset = 0.5 * (y[i - 1] - y[i + 1]) / denom if denom != 0 else 0.0
        offset = float(np.clip(offset, -0.5, 0.5))
        peaks.append(
            Peak(
                wavelength_nm=float(np.interp(i + offset, pixels, x)),
                height=float(y[i]),
                prominence=prominence,
                fwhm_nm=fwhm,
                pixel=int(i),
            )
        )
    peaks.sort(key=lambda p: p.prominence, reverse=True)
    return peaks[:max_peaks]


def write_csv(path: str, header: list[str], columns: list[np.ndarray]) -> str:
    """Write equal-length columns to CSV and return the absolute path."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in zip(*columns, strict=True):
            writer.writerow(["" if not np.isfinite(v) else f"{v:.6g}" for v in row])
    return str(p.resolve())
