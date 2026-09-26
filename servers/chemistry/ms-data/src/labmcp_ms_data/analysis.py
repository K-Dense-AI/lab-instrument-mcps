"""Numerical helpers: chromatogram downsampling, XIC peak integration, spectrum peak lists (numpy only)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PROTON_MASS = 1.007276


def downsample_max_indices(y: np.ndarray, max_points: int) -> np.ndarray:
    """Indices kept by :func:`downsample_max` (the highest point of each of ``max_points`` bins)."""
    y = np.asarray(y, dtype=float)
    if y.size <= max_points:
        return np.arange(y.size)
    bins = np.array_split(np.arange(y.size), max_points)
    return np.array([b[int(np.argmax(y[b]))] for b in bins if b.size], dtype=np.int64)


def downsample_max(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[list[float], list[float]]:
    """Split into ``max_points`` contiguous bins and keep, per bin, the point with the highest ``y``.

    Keeping the maximum (not the mean) preserves chromatographic peak heights and apex positions.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    idx = downsample_max_indices(y, max_points)
    return x[idx].tolist(), y[idx].tolist()


def trapezoid(y: np.ndarray, x: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if y.size < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2.0)


@dataclass
class ChromPeak:
    apex_rt_min: float
    apex_intensity: float
    start_rt_min: float
    end_rt_min: float
    area: float  # intensity x minutes, baseline not subtracted
    fwhm_s: float | None
    points_across_peak: int


def _half_crossing(
    x: np.ndarray, y: np.ndarray, i: int, level: float, step: int, lo: int, hi: int
) -> float | None:
    """RT where the trace first falls below ``level`` walking from ``i``, within ``lo..hi`` only."""
    j = i
    while lo <= j + step <= hi:
        k = j + step
        if y[k] < level:
            frac = (y[j] - level) / (y[j] - y[k]) if y[j] != y[k] else 0.0
            return float(x[j] + frac * (x[k] - x[j]))
        j = k
    return None


def integrate_apex_peak(
    rt_min: np.ndarray, y: np.ndarray, *, stop_fraction: float = 0.01
) -> ChromPeak | None:
    """Find the most intense point and integrate the peak around it.

    The peak extends from the apex on each side until the trace falls below ``stop_fraction`` (1 %) of the
    apex or reaches a valley (a point lower than the next 2 points on that side). Area is the
    trapezoidal integral of intensity over retention time in minutes, without baseline subtraction.
    """
    rt = np.asarray(rt_min, dtype=float)
    y = np.asarray(y, dtype=float)
    if y.size == 0 or not np.any(y > 0):
        return None
    i = int(np.argmax(y))
    apex = float(y[i])
    stop = stop_fraction * apex

    def walk(step: int) -> int:
        j = i
        while 0 <= j + step < y.size:
            k = j + step
            if y[k] <= stop:
                return k
            k2 = k + step
            k3 = k2 + step
            if 0 <= k3 < y.size and y[k2] > y[k] and y[k3] > y[k]:
                return k  # valley
            j = k
        return j

    lo, hi = walk(-1), walk(+1)
    # FWHM of the apex peak only: a co-eluting neighbour beyond a valley must not widen it.
    left = _half_crossing(rt, y, i, apex / 2, -1, lo, hi)
    right = _half_crossing(rt, y, i, apex / 2, +1, lo, hi)
    fwhm = (right - left) * 60.0 if left is not None and right is not None else None
    return ChromPeak(
        apex_rt_min=float(rt[i]),
        apex_intensity=apex,
        start_rt_min=float(rt[lo]),
        end_rt_min=float(rt[hi]),
        area=trapezoid(y[lo : hi + 1], rt[lo : hi + 1]),
        fwhm_s=fwhm,
        points_across_peak=int(hi - lo + 1),
    )


def tolerance_da(mz: float, tol: float, unit: str) -> float:
    return mz * tol * 1e-6 if unit == "ppm" else tol


def local_maxima(mz: np.ndarray, intensity: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Crude centroiding of profile data: keep points higher than both neighbours."""
    if intensity.size < 3:
        return mz, intensity
    y = intensity
    keep = np.zeros(y.size, dtype=bool)
    keep[1:-1] = (y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:])
    keep[0] = y[0] > y[1]
    keep[-1] = y[-1] > y[-2]
    return mz[keep], y[keep]


def write_csv(path: Path, header: list[str], columns: list[np.ndarray | list], *, overwrite: bool = False) -> str:
    """Write equal-length columns to CSV and return the absolute path.

    Without ``overwrite`` the file is created exclusively (``FileExistsError`` if it appeared meanwhile).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if overwrite else "x", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in zip(*columns, strict=True):
            out = []
            for v in row:
                if v is None or (isinstance(v, float) and not np.isfinite(v)):
                    out.append("")
                elif isinstance(v, (float, np.floating)):
                    out.append(f"{float(v):.8g}")
                else:
                    out.append(str(v))
            writer.writerow(out)
    return str(path)
