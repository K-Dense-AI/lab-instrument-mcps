"""Driver for Ocean Insight (Ocean Optics) spectrometers through python-seabreeze.

python-seabreeze (https://github.com/ap--/python-seabreeze, docs:
https://python-seabreeze.readthedocs.io, API checked against seabreeze 2.11) is the open-source
Python wrapper of Ocean's SeaBreeze library. Only its documented public API is used:

* ``seabreeze.use("cseabreeze" | "pyseabreeze", force=...)``: backend selection (``force=False``
  lets seabreeze fall back to the other backend).
* ``seabreeze.spectrometers.list_devices()`` -> devices with ``.model``, ``.serial_number``,
  ``.is_open``; ``Spectrometer(device)`` / ``Spectrometer.from_serial_number(serial)``.
* ``Spectrometer.wavelengths()``, ``.intensities(correct_dark_counts=, correct_nonlinearity=)``,
  ``.integration_time_micros(us)``, ``.integration_time_micros_limits``, ``.max_intensity``,
  ``.pixels``, ``.model``, ``.serial_number``, ``.close()``.
* Feature API via ``Spectrometer.f``: ``f.spectrometer.get_electric_dark_pixel_indices()``,
  ``f.nonlinearity_coefficients.get_nonlinearity_coefficients()`` and, on TE-cooled models,
  ``f.thermo_electric.read_temperature_degrees_celsius()``,
  ``.set_temperature_setpoint_degrees_celsius(t)``, ``.enable_tec(state)``.

Raw spectra are read with ``intensities()`` (no corrections) so saturation can be detected on
the raw ADC counts. The electric-dark and nonlinearity corrections are then applied exactly as
``seabreeze.spectrometers.Spectrometer.intensities`` does it (subtract the mean of the
electric-dark pixels; divide by the EEPROM nonlinearity polynomial evaluated at the
dark-subtracted counts).

seabreeze cannot read the integration time back, so the driver sets it on connect and tracks
it. After every change the next spectrum is discarded because it may have been integrated
(partly) with the old setting; the QE Pro manual (MNL-0000) notes that it returns the most
recently *completed* spectrum.
"""

from __future__ import annotations

import threading
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
from labmcp import AuditLog, InstrumentConnectionError, InstrumentError, InstrumentProtocolError

from labmcp_ocean_spectrometer.analysis import boxcar

SATURATION_FRACTION = 0.98  # raw counts at or above this fraction of max_intensity are saturated


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SETUP_HELP = (
    "Is the spectrometer plugged in and not open in OceanView or another program? On Linux run "
    "`seabreeze_os_setup` once (installs the udev rules, needs sudo) and re-plug the device; on "
    "Windows `seabreeze_os_setup` installs the USB drivers."
)


def _load_seabreeze(backend: str) -> Any:
    try:
        import seabreeze
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise InstrumentConnectionError(
            "python-seabreeze is not installed. Install it with `pip install seabreeze[pyseabreeze]`."
        ) from exc
    import sys

    if "seabreeze.spectrometers" not in sys.modules:
        # cseabreeze is the default; without `force` seabreeze falls back to pyseabreeze.
        seabreeze.use(backend, force=backend == "pyseabreeze")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import seabreeze.spectrometers as sb
    return sb


def list_seabreeze_devices(backend: str = "cseabreeze") -> list[dict[str, Any]]:
    sb = _load_seabreeze(backend)
    try:
        devices = sb.list_devices()
    except Exception as exc:
        raise InstrumentConnectionError(f"seabreeze could not list devices: {exc}. {_SETUP_HELP}") from exc
    return [{"model": d.model, "serial_number": d.serial_number, "is_open": bool(d.is_open)} for d in devices]


def open_seabreeze(serial: str | None, backend: str = "cseabreeze") -> Any:
    """Open the spectrometer with ``serial`` (or the only one connected)."""
    sb = _load_seabreeze(backend)
    try:
        devices = sb.list_devices()
    except Exception as exc:
        raise InstrumentConnectionError(f"seabreeze could not list devices: {exc}. {_SETUP_HELP}") from exc
    if not devices:
        raise InstrumentConnectionError(f"No Ocean spectrometer found. {_SETUP_HELP}")
    try:
        if serial:
            return sb.Spectrometer.from_serial_number(serial)
        if len(devices) > 1:
            found = ", ".join(f"{d.model} {d.serial_number}" for d in devices)
            raise InstrumentConnectionError(
                f"Several spectrometers are connected ({found}). Start the server with "
                "`--address <serial number>` to choose one."
            )
        return sb.Spectrometer(devices[0])
    except InstrumentError:
        raise
    except Exception as exc:
        raise InstrumentConnectionError(f"Could not open the spectrometer: {exc}. {_SETUP_HELP}") from exc


@dataclass
class Spectrum:
    wavelengths_nm: np.ndarray
    counts: np.ndarray
    integration_time_ms: float
    scans_to_average: int
    boxcar_half_width: int
    correct_dark_counts: bool
    correct_nonlinearity: bool
    saturated_mask: np.ndarray
    peak_raw_counts: float
    electric_dark_counts: float | None
    timestamp: str = field(default_factory=_now)

    @property
    def saturated_pixels(self) -> int:
        return int(self.saturated_mask.sum())

    def settings(self) -> tuple[float, int, bool, bool]:
        return (
            self.integration_time_ms,
            self.boxcar_half_width,
            self.correct_dark_counts,
            self.correct_nonlinearity,
        )


@dataclass
class Ratio:
    kind: Literal["absorbance", "transmittance"]
    sample: Spectrum
    values: np.ndarray  # absorbance (AU) or transmittance (%), NaN where invalid
    valid: np.ndarray


class OceanSpectrometer:
    """A spectrometer opened through seabreeze (or the simulator's look-alike)."""

    def __init__(
        self,
        spec: Any,
        *,
        backend: str,
        audit: AuditLog | None = None,
        initial_integration_ms: float = 10.0,
    ) -> None:
        self.spec = spec
        self.backend = backend
        self.audit = audit
        self.lock = threading.RLock()
        self.wavelengths_nm = np.asarray(
            self._call(spec.wavelengths, what="reading wavelengths"), dtype=float
        )
        self.max_intensity = float(spec.max_intensity)
        lo, hi = spec.integration_time_micros_limits
        self.integration_limits_us = (int(lo), int(hi))
        try:
            self._dark_pixels = list(spec.f.spectrometer.get_electric_dark_pixel_indices())
        except Exception:
            self._dark_pixels = []
        self._nc: np.poly1d | None = None
        nc_feature = getattr(spec.f, "nonlinearity_coefficients", None)
        if nc_feature is not None:
            try:  # same construction as seabreeze.spectrometers.Spectrometer.__init__
                self._nc = np.poly1d(nc_feature.get_nonlinearity_coefficients()[::-1])
            except Exception:
                self._nc = None
        self.tec = getattr(spec.f, "thermo_electric", None)
        self.dark: Spectrum | None = None
        self.reference: Spectrum | None = None
        self.last: Spectrum | Ratio | None = None
        self._discard_next = True
        self.integration_time_ms = 0.0
        lo_ms, hi_ms = lo / 1000.0, hi / 1000.0
        self.set_integration_time_ms(min(max(initial_integration_ms, lo_ms), hi_ms))

    # ------------------------------------------------------------ helpers

    def _event(self, message: str) -> None:
        if self.audit:
            self.audit.event(message, "seabreeze")

    @staticmethod
    def _call(fn: Any, *args: Any, what: str) -> Any:
        try:
            return fn(*args)
        except InstrumentError:
            raise
        except Exception as exc:
            raise InstrumentProtocolError(
                f"seabreeze error while {what}: {type(exc).__name__}: {exc}"
            ) from exc

    def _user_action(self, step: str) -> None:
        """Only the simulator implements this hook (it emulates the scientist blocking the beam
        or inserting a blank/sample). With real hardware the scientist does it."""
        hook = getattr(self.spec, "simulate_user_action", None)
        if callable(hook):
            hook(step)

    # ------------------------------------------------------------ identity

    @property
    def model(self) -> str:
        return str(self.spec.model)

    @property
    def serial(self) -> str:
        return str(self.spec.serial_number)

    @property
    def supports_dark_correction(self) -> bool:
        return bool(self._dark_pixels)

    @property
    def supports_nonlinearity_correction(self) -> bool:
        return self._nc is not None

    def identify(self) -> dict[str, str]:
        return {
            "manufacturer": "Ocean Insight",
            "model": self.model,
            "serial": self.serial,
            "pixels": str(len(self.wavelengths_nm)),
            "wavelength_range_nm": f"{self.wavelengths_nm[0]:.1f}-{self.wavelengths_nm[-1]:.1f}",
            "backend": self.backend,
        }

    def close(self) -> None:
        try:
            self.spec.close()
        except Exception:
            pass

    # ------------------------------------------------------------ acquisition

    def set_integration_time_ms(self, ms: float) -> float:
        us = int(round(ms * 1000.0))
        lo, hi = self.integration_limits_us
        if not lo <= us <= hi:
            raise InstrumentProtocolError(
                f"Integration time {ms:g} ms is outside this spectrometer's range "
                f"({lo / 1000:g}-{hi / 1000:g} ms). Nothing was sent."
            )
        with self.lock:
            self._call(self.spec.integration_time_micros, us, what="setting the integration time")
            self.integration_time_ms = us / 1000.0
            self._discard_next = True
        self._event(f"integration_time_micros({us})")
        return self.integration_time_ms

    def _raw(self) -> np.ndarray:
        return np.asarray(self._call(self.spec.intensities, what="reading a spectrum"), dtype=float)

    def correct(self, raw: np.ndarray, dark_counts: bool, nonlinearity: bool) -> np.ndarray:
        """Electric-dark and nonlinearity corrections, as in seabreeze's Spectrometer.intensities."""
        out = np.array(raw, dtype=float)
        dark_offset = 0.0
        if dark_counts or nonlinearity:
            dark_offset = float(np.mean(out[self._dark_pixels])) if self._dark_pixels else 0.0
            out -= dark_offset
        if nonlinearity and self._nc is not None:
            out = out / np.polyval(self._nc, out)
        if nonlinearity and not dark_counts:
            out += dark_offset
        return out

    def acquire(
        self,
        scans_to_average: int = 1,
        boxcar_half_width: int = 0,
        correct_dark_counts: bool = False,
        correct_nonlinearity: bool = False,
    ) -> Spectrum:
        if correct_dark_counts and not self.supports_dark_correction:
            raise InstrumentProtocolError(
                f"The {self.model} has no electric dark pixels, so dark-count correction is not available. "
                "Use store_dark_reference / a stored dark instead."
            )
        if correct_nonlinearity and not self.supports_nonlinearity_correction:
            raise InstrumentProtocolError(f"The {self.model} has no nonlinearity coefficients stored.")
        with self.lock:
            if self._discard_next:
                self._raw()  # may contain light integrated with the previous integration time
                self._discard_next = False
            raws = [self._raw() for _ in range(scans_to_average)]
            it = self.integration_time_ms
        self._event(f"intensities() x{scans_to_average} at {it:g} ms")
        stack = np.vstack(raws)
        saturated = (stack >= SATURATION_FRACTION * self.max_intensity).any(axis=0)
        corrected = np.mean(
            [self.correct(r, correct_dark_counts, correct_nonlinearity) for r in raws], axis=0
        )
        return Spectrum(
            wavelengths_nm=self.wavelengths_nm,
            counts=boxcar(corrected, boxcar_half_width),
            integration_time_ms=it,
            scans_to_average=scans_to_average,
            boxcar_half_width=boxcar_half_width,
            correct_dark_counts=correct_dark_counts,
            correct_nonlinearity=correct_nonlinearity,
            saturated_mask=saturated,
            peak_raw_counts=float(stack.max()),
            electric_dark_counts=float(stack[:, self._dark_pixels].mean()) if self._dark_pixels else None,
        )

    # ------------------------------------------------------------ references

    def store_dark(self, **settings: Any) -> Spectrum:
        self._user_action("dark")
        self.dark = self.acquire(**settings)
        self._event("stored dark reference")
        return self.dark

    def store_reference(self, **settings: Any) -> Spectrum:
        if self.dark is not None and self.dark.integration_time_ms != self.integration_time_ms:
            raise InstrumentProtocolError(
                f"The stored dark was recorded at {self.dark.integration_time_ms:g} ms but the integration "
                f"time is now {self.integration_time_ms:g} ms. Store a new dark reference first."
            )
        self._user_action("reference")
        self.reference = self.acquire(**settings)
        self._event("stored reference (blank) spectrum")
        return self.reference

    def measure_ratio(
        self, kind: Literal["absorbance", "transmittance"], scans_to_average: int | None = None
    ) -> Ratio:
        dark, ref = self.dark, self.reference
        if dark is None or ref is None:
            missing = " and ".join(n for n, s in (("a dark", dark), ("a reference", ref)) if s is None)
            raise InstrumentProtocolError(
                f"No {missing} spectrum stored. Run store_dark_reference (light blocked) and store_reference "
                "(blank in the beam) first."
            )
        if dark.settings() != ref.settings():
            raise InstrumentProtocolError(
                "The dark and reference were recorded with different settings (integration time, boxcar or "
                "corrections). Store both again with the same settings."
            )
        if ref.integration_time_ms != self.integration_time_ms:
            raise InstrumentProtocolError(
                f"The integration time changed from {ref.integration_time_ms:g} ms (reference) to "
                f"{self.integration_time_ms:g} ms. Store a new dark and reference."
            )
        self._user_action("sample")
        sample = self.acquire(
            scans_to_average=scans_to_average or ref.scans_to_average,
            boxcar_half_width=ref.boxcar_half_width,
            correct_dark_counts=ref.correct_dark_counts,
            correct_nonlinearity=ref.correct_nonlinearity,
        )
        denom = ref.counts - dark.counts
        valid = denom > max(0.005 * float(np.max(denom)), 1e-12)
        valid &= ~ref.saturated_mask & ~sample.saturated_mask
        with np.errstate(divide="ignore", invalid="ignore"):
            transmission = (sample.counts - dark.counts) / denom
            if kind == "absorbance":
                values = np.where(valid & (transmission > 0), -np.log10(transmission), np.nan)
            else:
                values = np.where(valid, 100.0 * transmission, np.nan)
        return Ratio(kind=kind, sample=sample, values=values, valid=valid & np.isfinite(values))

    # ------------------------------------------------------------ auto integration

    def auto_integration_time(
        self,
        target_min: float = 0.70,
        target_max: float = 0.85,
        max_iterations: int = 8,
        max_ms: float | None = None,
        window: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        """Iteratively scale the integration time until the raw peak is within the target band."""
        lo_us, hi_us = self.integration_limits_us
        lo_ms, hi_ms = lo_us / 1000.0, hi_us / 1000.0
        if max_ms is not None:
            hi_ms = min(hi_ms, max_ms)
        mask = np.ones_like(self.wavelengths_nm, dtype=bool)
        if window is not None:
            mask = (self.wavelengths_nm >= window[0]) & (self.wavelengths_nm <= window[1])
            if not mask.any():
                raise InstrumentProtocolError(f"No pixels between {window[0]:g} and {window[1]:g} nm.")
        target = 0.5 * (target_min + target_max)
        history: list[dict[str, float]] = []
        converged = False
        for _ in range(max_iterations):
            spec = self.acquire()
            peak = float(spec.counts[mask].max())
            fraction = peak / self.max_intensity
            t = self.integration_time_ms
            history.append({"integration_time_ms": t, "peak_fraction": round(fraction, 4)})
            if target_min <= fraction <= target_max:
                converged = True
                break
            dark = spec.electric_dark_counts or 0.0
            if fraction >= SATURATION_FRACTION:
                new_t = t * 0.4
            else:
                signal = max(peak - dark, 1.0)
                new_t = t * (target * self.max_intensity - dark) / signal
                new_t = min(new_t, t * 10.0)
            new_t = min(max(new_t, lo_ms), hi_ms)
            if abs(new_t - t) < 1e-3:
                break  # pinned at a limit
            self.set_integration_time_ms(new_t)
        return {"converged": converged, "integration_time_ms": self.integration_time_ms, "history": history}

    # ------------------------------------------------------------ TEC

    @property
    def has_tec(self) -> bool:
        return self.tec is not None

    def _require_tec(self) -> Any:
        if self.tec is None:
            raise InstrumentProtocolError(
                f"The {self.model} (seabreeze backend {self.backend}) has no thermo-electric cooler feature."
            )
        return self.tec

    def tec_temperature_c(self) -> float:
        tec = self._require_tec()
        return float(self._call(tec.read_temperature_degrees_celsius, what="reading the TEC temperature"))

    def set_tec(self, enabled: bool, setpoint_c: float | None = None) -> None:
        tec = self._require_tec()
        with self.lock:
            if setpoint_c is not None:
                self._call(
                    tec.set_temperature_setpoint_degrees_celsius, setpoint_c, what="setting the TEC setpoint"
                )
                self._event(f"thermo_electric.set_temperature_setpoint_degrees_celsius({setpoint_c:g})")
            self._call(tec.enable_tec, bool(enabled), what="switching the TEC")
            self._event(f"thermo_electric.enable_tec({bool(enabled)})")
