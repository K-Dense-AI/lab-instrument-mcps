"""A fake seabreeze ``Spectrometer`` with the same attributes and methods as the real one.

It produces physically plausible raw spectra:

* light sources: a tungsten-halogen lamp (Planck curve at 2900 K), a white LED (blue pump +
  phosphor) or a Hg-Ar calibration lamp (NIST Hg I / Ar I lines at the instrument resolution),
* a silicon detector response and grating efficiency,
* dark offset, dark current growing with integration time (and, for the TE-cooled QE Pro,
  doubling every ~6 °C of detector temperature), read noise and shot noise (sigma ~ sqrt(counts)),
  lamp flicker, a mild detector nonlinearity (corrected by the stored coefficients) and ADC
  saturation at ``max_intensity``,
* optically masked electric-dark pixels,
* a stale first spectrum after an integration-time change (as on real hardware).

The "scene" (beam blocked / blank / sample in the beam) is part of the simulator: the driver's
``simulate_user_action`` hook plays the scientist's role (block the light before a dark, insert
the blank before a reference, insert the sample before a measurement). Tests can also call
:meth:`FakeSpectrometer.set_scene` directly.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import Any

import numpy as np

# NIST atomic lines (nm, air) with rough relative intensities for a Hg-Ar pen lamp.
HG_AR_LINES = [
    (253.65, 0.3), (296.73, 0.1), (302.15, 0.08), (313.16, 0.15), (334.15, 0.05), (365.02, 0.35),
    (404.66, 0.5), (407.78, 0.08), (435.83, 1.0), (546.07, 1.0), (576.96, 0.25), (579.07, 0.25),
    (696.54, 0.15), (706.72, 0.12), (738.40, 0.15), (750.39, 0.35), (763.51, 0.6), (772.38, 0.2),
    (794.82, 0.2), (800.62, 0.18), (811.53, 0.5), (826.45, 0.25), (842.46, 0.3), (852.14, 0.15),
    (866.79, 0.06), (912.30, 0.3), (922.45, 0.1),
]  # fmt: skip

MODELS: dict[str, dict[str, Any]] = {
    "USB2000PLUS": {
        "serial": "USB2+SIM001",
        "pixels": 2048,
        "max_intensity": 65535.0,
        "it_limits_us": (1000, 655350000),
        "dark_pixels": list(range(6, 21)),
        "wl_coeffs": (340.3, 0.380, -2.2e-5),
        "dark_offset": 1100.0,
        "read_noise": 6.0,
        "dark_rate": 60.0,  # counts / s
        "fwhm_nm": 1.3,
        "tec": False,
    },
    "QE-PRO": {
        "serial": "QEP-SIM001",
        "pixels": 1044,
        "max_intensity": 262143.0,
        "it_limits_us": (8000, 1600000000),
        "dark_pixels": list(range(0, 4)) + list(range(1040, 1044)),
        "wl_coeffs": (200.0, 0.85, -1.52e-5),
        "dark_offset": 2500.0,
        "read_noise": 14.0,
        "dark_rate": 150.0,  # counts / s at -10 °C
        "fwhm_nm": 2.0,
        "tec": True,
    },
}

NONLINEARITY = [1.0, -1.5e-7]  # EEPROM-style coefficients c0 + c1*x: response drops ~1 % at 65k counts


class FakeTEC:
    """seabreeze ``thermo_electric`` feature look-alike with a first-order thermal response."""

    def __init__(self, ambient_c: float = 22.0, tau_s: float = 20.0) -> None:
        self.ambient = ambient_c
        self.tau = tau_s
        self.setpoint = -10.0
        self.enabled = False
        self._temp = ambient_c
        self._t = time.monotonic()

    def _update(self) -> float:
        now = time.monotonic()
        target = max(self.setpoint, self.ambient - 40.0) if self.enabled else self.ambient
        self._temp = target + (self._temp - target) * math.exp(-(now - self._t) / self.tau)
        self._t = now
        return self._temp

    def read_temperature_degrees_celsius(self) -> float:
        return round(self._update(), 2)

    def set_temperature_setpoint_degrees_celsius(self, temperature: float) -> None:
        self._update()
        self.setpoint = float(temperature)

    def enable_tec(self, state: bool) -> None:
        self._update()
        self.enabled = bool(state)


class _SpectrometerFeature:
    def __init__(self, dark_pixels: list[int]) -> None:
        self._dp = dark_pixels

    def get_electric_dark_pixel_indices(self) -> list[int]:
        return list(self._dp)


class _NonlinearityFeature:
    def get_nonlinearity_coefficients(self) -> list[float]:
        return list(NONLINEARITY)


class FakeDevice:
    """What ``seabreeze.spectrometers.list_devices()`` returns."""

    def __init__(self, model: str, serial_number: str) -> None:
        self.model = model
        self.serial_number = serial_number
        self.is_open = False


class FakeSpectrometer:
    def __init__(self, model: str = "USB2000PLUS", source: str = "halogen", seed: int | None = 0) -> None:
        if model not in MODELS:
            raise ValueError(f"Unknown simulated model {model!r}; choose from {sorted(MODELS)}")
        if source not in {"halogen", "led", "hg-ar"}:
            raise ValueError(f"Unknown simulated light source {source!r}; use halogen, led or hg-ar")
        self.cfg = MODELS[model]
        self._model = model
        self.rng = np.random.default_rng(seed)
        n = self.cfg["pixels"]
        c0, c1, c2 = self.cfg["wl_coeffs"]
        px = np.arange(n, dtype=float)
        self._wl = c0 + c1 * px + c2 * px**2
        self._dp = self.cfg["dark_pixels"]
        self._it_us = 100000
        self._prev_it_us = self._it_us
        self._stale = False
        self.tec = FakeTEC() if self.cfg["tec"] else None
        self.f = SimpleNamespace(
            spectrometer=_SpectrometerFeature(self._dp),
            nonlinearity_coefficients=_NonlinearityFeature(),
            thermo_electric=self.tec,
        )
        self.features = {
            "spectrometer": [self.f.spectrometer],
            "nonlinearity_coefficients": [self.f.nonlinearity_coefficients],
            "thermo_electric": [self.tec] if self.tec else [],
        }
        self.source = source
        self.light_blocked = False
        self.sample_in = False
        self.sample = {"peak_nm": 520.0, "peak_absorbance": 0.8, "width_nm": 40.0, "baseline": 0.02}
        self._flux = self._source_flux(source)
        self.is_open = True

    # ------------------------------------------------------------ seabreeze API

    @property
    def model(self) -> str:
        return self._model

    @property
    def serial_number(self) -> str:
        return self.cfg["serial"]

    @property
    def pixels(self) -> int:
        return self.cfg["pixels"]

    @property
    def max_intensity(self) -> float:
        return self.cfg["max_intensity"]

    @property
    def integration_time_micros_limits(self) -> tuple[int, int]:
        return self.cfg["it_limits_us"]

    def wavelengths(self) -> np.ndarray:
        return self._wl.copy()

    def integration_time_micros(self, integration_time_micros: int) -> None:
        lo, hi = self.cfg["it_limits_us"]
        if not lo <= integration_time_micros <= hi:
            raise ValueError("[Fix] Specified integration time is out of range.")
        self._prev_it_us, self._it_us = self._it_us, int(integration_time_micros)
        self._stale = True

    def intensities(
        self, correct_dark_counts: bool = False, correct_nonlinearity: bool = False
    ) -> np.ndarray:
        it_us = self._prev_it_us if self._stale else self._it_us
        self._stale = False
        time.sleep(min(it_us / 1e6, 0.02))
        out = self._raw(it_us)
        # Same correction path as seabreeze.spectrometers.Spectrometer.intensities
        if correct_nonlinearity or correct_dark_counts:
            dark_offset = np.mean(out[self._dp]) if self._dp else 0.0
            out -= dark_offset
        if correct_nonlinearity:
            out = out / np.polyval(np.poly1d(NONLINEARITY[::-1]), out)
        if correct_nonlinearity and not correct_dark_counts:
            out += dark_offset
        return out

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    @staticmethod
    def list_devices() -> list[FakeDevice]:
        return [FakeDevice(m, cfg["serial"]) for m, cfg in MODELS.items()]

    # ------------------------------------------------------------ simulation hooks

    def set_scene(self, *, light_blocked: bool | None = None, sample_in: bool | None = None) -> None:
        if light_blocked is not None:
            self.light_blocked = light_blocked
        if sample_in is not None:
            self.sample_in = sample_in

    def simulate_user_action(self, step: str) -> None:
        """Play the scientist's role before a dark / reference / sample measurement."""
        if step == "dark":
            self.set_scene(light_blocked=True)
        elif step == "reference":
            self.set_scene(light_blocked=False, sample_in=False)
        elif step == "sample":
            self.set_scene(light_blocked=False, sample_in=True)

    def sample_absorbance(self, wl: np.ndarray | None = None) -> np.ndarray:
        wl = self._wl if wl is None else wl
        s = self.sample
        sigma = s["width_nm"] / 2.3548
        return s["baseline"] + s["peak_absorbance"] * np.exp(-0.5 * ((wl - s["peak_nm"]) / sigma) ** 2)

    # ------------------------------------------------------------ physics

    def _source_flux(self, source: str) -> np.ndarray:
        """Signal counts per ms at the detector (unblocked, no sample)."""
        wl = self._wl
        silicon = 1.0 / (1 + np.exp(-(wl - 380.0) / 30.0)) / (1 + np.exp((wl - 1030.0) / 40.0))
        grating = np.exp(-0.5 * ((wl - 550.0) / 260.0) ** 2)
        if source == "halogen":
            lam = wl * 1e-9
            planck = 1.0 / (lam**5 * (np.exp(1.4388e-2 / (lam * 2900.0)) - 1.0))
            shape = planck * silicon * grating
        elif source == "led":
            shape = (
                np.exp(-0.5 * ((wl - 450.0) / 9.0) ** 2) + 0.55 * np.exp(-0.5 * ((wl - 565.0) / 50.0) ** 2)
            ) * silicon
        else:
            sigma = self.cfg["fwhm_nm"] / 2.3548
            shape = sum(a * np.exp(-0.5 * ((wl - c) / sigma) ** 2) for c, a in HG_AR_LINES) * silicon
        # At 10 ms the brightest pixel sits at ~45 % of full scale (USB2000+).
        return shape / shape.max() * 0.45 * self.cfg["max_intensity"] / 10.0

    def _raw(self, it_us: int) -> np.ndarray:
        cfg = self.cfg
        it_ms = it_us / 1000.0
        signal = np.zeros_like(self._wl)
        if not self.light_blocked:
            flicker = 1.0 + self.rng.normal(0.0, 0.001)
            signal = self._flux * it_ms * flicker
            if self.sample_in:
                signal = signal * 10.0 ** (-self.sample_absorbance())
        signal[self._dp] = 0.0  # optically masked pixels
        dark_rate = cfg["dark_rate"]
        if self.tec is not None:
            dark_rate *= 2.0 ** ((self.tec.read_temperature_degrees_celsius() + 10.0) / 6.0)
        dark = cfg["dark_offset"] + dark_rate * it_ms / 1000.0
        # shot noise ~ sqrt(counts) plus read noise
        noisy = signal + self.rng.normal(0.0, 1.0, signal.shape) * np.sqrt(signal + cfg["read_noise"] ** 2)
        measured = noisy / (1.0 - NONLINEARITY[1] * np.clip(noisy, 0, None))  # raw = true * p(raw)
        raw = dark + measured
        return np.clip(np.round(raw), 0.0, cfg["max_intensity"])
