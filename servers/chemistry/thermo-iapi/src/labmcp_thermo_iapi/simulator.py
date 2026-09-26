"""FakeOrbitrap: a simulated Orbitrap behind the same backend interface as the IAPI backend.

Used by ``--simulate`` and the tests. It behaves the way the IAPI documentation and examples
describe the real instruments:

* In **On** mode the instrument scans continuously and ``MsScanArrived`` fires for every scan
  ("Bring the system to mode 'On' to see scans coming in", Exploris DataReceiver example). When
  idle it repeats a full MS1 scan (or the repeating scan set with ``SetRepetitionScan``); during
  an acquisition it runs a data-dependent top-10 method (one MS1 full scan followed by up to ten
  HCD MS2 scans on the most intense multiply-charged precursors, 20 s dynamic exclusion).
* Custom scans placed with ``SetCustomScan`` run next, and their ``RunningNumber`` comes back in
  the trailer item ``Access Id:`` (system scans report -1), as in the PlacingScans example.
* Scan timing follows Orbitrap physics: the transient length doubles with resolution
  (7 500 at m/z 200 -> 16 ms ... 480 000 -> 1 024 ms) and injection runs in parallel with the
  previous transient, so a scan takes max(transient, injection time) plus ~8 ms overhead.
* The sample is a synthetic tryptic digest: a few hundred peptides with Gaussian elution
  profiles, charge states 2-4 and averagine-like isotope envelopes, on top of chemical noise.
  MS2 spectra are b/y-like fragment ladders of the selected precursor. Injection time adapts
  to reach the AGC target and is capped by the maximum injection time.
* A status log (vacuum, spray voltage, capillary temperature) arrives every ~5 s.

``PossibleParameters`` values and the status-log / readback names are plausible stand-ins,
**not** copied from a real instrument: the real names and ranges come from the instrument at
run time.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from labmcp import InstrumentProtocolError

from labmcp_thermo_iapi.backend import (
    AcquisitionMode,
    Centroid,
    OrbitrapBackend,
    ParameterDescription,
    ScanRecord,
)

log = logging.getLogger("labmcp.thermo_iapi.simulator")

PROTON = 1.007276
ISOTOPE_SPACING = 1.003355
RESOLUTIONS = (7500, 15000, 30000, 45000, 60000, 90000, 120000, 180000, 240000, 480000)


def _res_list(max_res: int) -> str:
    return ",".join(str(r) for r in RESOLUTIONS if r <= max_res)


MODELS: dict[str, dict[str, Any]] = {
    "exploris480": {
        "name": "Orbitrap Exploris 480",
        "family": "exploris",
        "detector_class": "Orbitrap",
        "max_res": 480000,
        "mz_range": (40.0, 6000.0),
        "activation": "HCD",
        "analyzer": None,
    },
    "eclipse": {
        "name": "Orbitrap Eclipse",
        "family": "tribrid",
        "detector_class": "Tribrid Orbitrap",
        "max_res": 500000,
        "mz_range": (50.0, 6000.0),
        "activation": "CID,HCD,ETD,EThcD,ETciD,UVPD",
        "analyzer": "Orbitrap,IonTrap",
    },
    "qexactive-hf": {
        "name": "Q Exactive HF",
        "family": "exactive",
        "detector_class": "Orbitrap",
        "max_res": 240000,
        "mz_range": (50.0, 6000.0),
        "activation": "HCD",
        "analyzer": None,
    },
}


def _first_number(value: str | None, default: float | None) -> float | None:
    """The first usable number of an IAPI value, which may be multi-valued ("1.2;2.0",
    "350;-1"). ``-1`` means "the instrument default", as in the IAPI examples."""
    for part in re.split(r"[;,]", str(value or "")):
        try:
            number = float(part)
        except ValueError:
            continue
        if number != -1 and math.isfinite(number):
            return number
    return default


def _transient_ms(resolution: float) -> float:
    return 16.0 * max(resolution, 7500.0) / 7500.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class _Peptide:
    mass: float  # neutral monoisotopic mass (Da)
    charge_weights: tuple[float, float, float]  # z = 2, 3, 4
    apex_s: float  # retention time of the apex (s after acquisition start, cycling)
    width_s: float
    abundance: float  # ions/s at apex
    fragments: tuple[float, ...]  # singly charged fragment m/z values


def _averagine(mass: float, n: int = 5) -> list[float]:
    """Relative isotope abundances (Poisson with lambda ~ mass/1800 for peptides)."""
    lam = mass / 1800.0
    p = [math.exp(-lam) * lam**k / math.factorial(k) for k in range(n)]
    top = max(p)
    return [x / top for x in p]


class FakeOrbitrap(OrbitrapBackend):
    kind = "simulator"

    def __init__(
        self,
        model: str = "exploris480",
        *,
        speed: float = 1.0,
        seed: int | None = 0,
        gradient_s: float = 1800.0,
        autostart: bool = True,
    ) -> None:
        if model not in MODELS:
            raise InstrumentProtocolError(f"Unknown simulated model {model!r}; choose from {sorted(MODELS)}.")
        self.model_key = model
        self.model = MODELS[model]
        self.speed = max(0.01, float(speed))
        self.rng = random.Random(seed)
        self.gradient_s = gradient_s
        self.autostart = autostart
        self.system_mode = "On"
        self.acquiring = False
        self.paused = False
        self.acquisition: dict[str, Any] | None = None
        self.custom_queue: deque[tuple[dict[str, str], int]] = deque()
        self.repeating: tuple[dict[str, str], int] | None = None
        # Test hooks: everything that "reached the instrument".
        self.sent_custom_scans: list[dict[str, Any]] = []
        self.sent_repeating_scans: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._scan_number = 0
        self._last_ms1: tuple[int, list[Centroid]] | None = None
        self._dda_queue: deque[tuple[float, int]] = deque()
        self._exclusion: dict[int, float] = {}
        self._last_status_log = -1e9
        self._sim_t = 0.0  # simulated chromatographic time (s)
        self._acq_scans = 0
        self._acq_started = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_scan: Callable[[ScanRecord], None] | None = None
        self.peptides = self._make_sample(400)

    # ------------------------------------------------------------------ sample

    def _make_sample(self, n: int) -> list[_Peptide]:
        peptides = []
        for _ in range(n):
            mass = self.rng.uniform(800.0, 3500.0)
            weights = (self.rng.random() * 3 + 1, self.rng.random() * 2, self.rng.random() * 0.6)
            s = sum(weights)
            n_frag = self.rng.randint(8, 22)
            frags = tuple(sorted(self.rng.uniform(150.0, mass - 50.0) for _ in range(n_frag)))
            peptides.append(
                _Peptide(
                    mass=mass,
                    charge_weights=(weights[0] / s, weights[1] / s, weights[2] / s),
                    apex_s=self.rng.uniform(0.0, self.gradient_s),
                    width_s=self.rng.uniform(6.0, 20.0),
                    abundance=10 ** self.rng.uniform(4.0, 7.0),
                    fragments=frags,
                )
            )
        return peptides

    def _elution(self, p: _Peptide) -> float:
        t = self._sim_t % self.gradient_s
        return math.exp(-0.5 * ((t - p.apex_s) / p.width_s) ** 2)

    # --------------------------------------------------------------- lifecycle

    def open(self, on_scan: Callable[[ScanRecord], None]) -> None:
        self._on_scan = on_scan
        self.calls.append("StartOnlineAccess")
        if self.autostart:
            self._thread = threading.Thread(target=self._run, name="FakeOrbitrap", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def identify(self) -> dict[str, str]:
        return {
            "manufacturer": "Thermo Fisher Scientific",
            "model": self.model["name"],
            "family": self.model["family"],
            "instrument_id": "1",
            "detector_class": self.model["detector_class"],
            "backend": "simulator (FakeOrbitrap)",
        }

    def _system_state(self) -> str:
        if self.system_mode != "On":
            return "StandBy" if self.system_mode == "Standby" else "Off"
        if self.acquiring:
            return "Running"
        return "ReadyToDownload"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_connected": True,
                "instrument_connected": True,
                "instrument_name": self.model["name"],
                "instrument_id": 1,
                "system_mode": self.system_mode,
                "system_state": self._system_state(),
                "can_pause": self.acquiring and not self.paused,
                "can_resume": self.acquiring and self.paused,
                "api_license": True,
                "readback_names": [
                    "InstrumentAcquisition",
                    "Procedures",
                    "Root",
                    "SourceSprayVoltage",
                    "VirtualInstrument",
                ],
                "readbacks": {
                    "SourceSprayVoltage": {
                        "value": f"{2000 + self.rng.gauss(0, 2):.0f}" if self.system_mode == "On" else "0",
                        "unit": "V",
                        "status": "Ok",
                    }
                },
                "acquisition": None if self.acquisition is None else dict(self.acquisition),
            }

    # ------------------------------------------------------------- parameters

    def possible_parameters(self) -> list[ParameterDescription]:
        lo, hi = self.model["mz_range"]
        rng = f"{lo:.1f}-{hi:.1f}"
        params = [
            ParameterDescription("ScanType", "Full,SIM,MSn", "Full", "Type of scan"),
            ParameterDescription("FirstMass", rng, "350.0", "First m/z of the scan range"),
            ParameterDescription("LastMass", rng, "1500.0", "Last m/z of the scan range"),
            ParameterDescription(
                "OrbitrapResolution", _res_list(self.model["max_res"]), "60000", "Resolution at m/z 200"
            ),
            ParameterDescription("AGCTarget", "1000-5000000", "300000", "Target number of charges"),
            ParameterDescription("MaxIT", "0.001-5000.0", "50.0", "Maximum injection time (ms)"),
            ParameterDescription("Polarity", "Positive,Negative", "Positive", "Ion polarity"),
            ParameterDescription("Microscans", "1-100", "1", "Microscans averaged per scan"),
            ParameterDescription("PrecursorMass", rng, "", "Precursor m/z for MSn/SIM"),
            ParameterDescription("IsolationWidth", "0.4-1200.0", "1.6", "Isolation window width (m/z)"),
            ParameterDescription("ActivationType", self.model["activation"], "HCD", "Activation type"),
            ParameterDescription("CollisionEnergy", "0.0-200.0", "30.0", "Normalized collision energy (%)"),
            ParameterDescription("SrcRFLens", "0.0-150.0", "50.0", "RF lens (%)"),
            ParameterDescription("ScanDescription", "string", "", "Free text stored with the scan"),
        ]
        if self.model["analyzer"]:
            params.insert(
                1, ParameterDescription("Analyzer", self.model["analyzer"], "Orbitrap", "Mass analyzer")
            )
        return params

    # ------------------------------------------------------------ acquisition

    def _require_on(self, what: str) -> None:
        if self.system_mode != "On":
            raise InstrumentProtocolError(
                f"Could not {what}: the instrument is not in the proper condition (system mode is "
                f"{self.system_mode}; it must be On)."
            )

    def start_acquisition(
        self,
        mode: AcquisitionMode,
        *,
        duration_s: float | None,
        scan_count: int | None,
        raw_file_path: str | None,
        sample_name: str | None,
        comment: str | None,
    ) -> None:
        with self._lock:
            self._require_on("start the acquisition")
            if self.acquiring:
                raise InstrumentProtocolError(
                    "Could not start the acquisition: an acquisition is already running. Stop it first."
                )
            self.calls.append(f"StartAcquisition({mode})")
            self.acquiring, self.paused = True, False
            self._acq_scans = 0
            self._acq_started = self._sim_t
            self._dda_queue.clear()
            self.acquisition = {
                "mode": mode,
                "duration_s": duration_s,
                "scan_count": scan_count,
                "raw_file_path": raw_file_path,
                "sample_name": sample_name,
                "comment": comment,
                "started_at": _now(),
            }

    def _end_acquisition(self) -> None:
        self.acquiring = self.paused = False
        self.acquisition = None
        self._dda_queue.clear()

    def pause_acquisition(self) -> None:
        with self._lock:
            if not (self.acquiring and not self.paused):
                raise InstrumentProtocolError(
                    "The instrument reports that the current operation cannot be paused."
                )
            self.calls.append("Pause")
            self.paused = True

    def resume_acquisition(self) -> None:
        with self._lock:
            if not (self.acquiring and self.paused):
                raise InstrumentProtocolError("The instrument reports that there is nothing to resume.")
            self.calls.append("Resume")
            self.paused = False

    def cancel_acquisition(self) -> None:
        with self._lock:
            self.calls.append("CancelAcquisition")
            self._end_acquisition()

    def set_standby(self) -> None:
        with self._lock:
            self.calls.append("SetMode(Standby)")
            self._end_acquisition()
            self.system_mode = "Standby"

    # ------------------------------------------------------------------ scans

    def set_custom_scan(
        self, values: dict[str, str], *, running_number: int, single_processing_delay_s: float
    ) -> bool:
        with self._lock:
            self.calls.append("SetCustomScan")
            self.sent_custom_scans.append(
                {
                    "values": dict(values),
                    "running_number": running_number,
                    "delay_s": single_processing_delay_s,
                }
            )
            if len(self.custom_queue) < 50:
                self.custom_queue.append((dict(values), running_number))
            return True

    def cancel_custom_scan(self) -> bool:
        with self._lock:
            self.calls.append("CancelCustomScan")
            self.custom_queue.clear()
            return True

    def set_repeating_scan(self, values: dict[str, str], *, running_number: int) -> bool:
        with self._lock:
            self.calls.append("SetRepetitionScan")
            self.sent_repeating_scans.append({"values": dict(values), "running_number": running_number})
            self.repeating = (dict(values), running_number)
            return True

    def cancel_repeating_scan(self) -> bool:
        with self._lock:
            self.calls.append("CancelRepetition")
            self.repeating = None
            return True

    # ------------------------------------------------------------ scan engine

    def _defaults(self) -> dict[str, str]:
        return {p.name: p.default_value for p in self.possible_parameters() if p.default_value != ""}

    def _next_definition(self) -> tuple[dict[str, str], int] | None:
        """What the instrument scans next: custom > DDA MS2 > repeating/MS1."""
        if self.system_mode != "On" or (self.acquiring and self.paused):
            return None
        if self.custom_queue:
            return self.custom_queue.popleft()
        if self.acquiring and self._dda_queue:
            mz, z = self._dda_queue.popleft()
            return (
                {
                    "ScanType": "MSn",
                    "PrecursorMass": f"{mz:.4f}",
                    "OrbitrapResolution": "15000",
                    "AGCTarget": "100000",
                    "MaxIT": "22",
                    "IsolationWidth": "1.6",
                    "ActivationType": "HCD",
                    "CollisionEnergy": "30",
                    "FirstMass": "120",
                    "LastMass": f"{min(2000.0, mz * z):.0f}",
                    "_charge": str(z),
                },
                -1,
            )
        if self.repeating is not None:
            return self.repeating
        return (
            {
                "ScanType": "Full",
                "FirstMass": "350",
                "LastMass": "1500",
                "OrbitrapResolution": "120000" if self.acquiring else "60000",
                "AGCTarget": "300000",
                "MaxIT": "50",
            },
            -1,
        )

    def _ms1_peaks(self, lo: float, hi: float, resolution: float) -> list[tuple[float, float, int | None]]:
        peaks: list[tuple[float, float, int | None]] = []
        for p in self.peptides:
            e = self._elution(p)
            if e < 1e-3:
                continue
            for z, w in zip((2, 3, 4), p.charge_weights, strict=True):
                if w < 0.02:
                    continue
                mono = (p.mass + z * PROTON) / z
                for k, rel in enumerate(_averagine(p.mass)):
                    mz = mono + k * ISOTOPE_SPACING / z
                    if lo <= mz <= hi and rel > 0.05:
                        peaks.append((mz, p.abundance * e * w * rel, z))
        # chemical noise
        for _ in range(int(40 + (hi - lo) / 10)):
            peaks.append((self.rng.uniform(lo, hi), 10 ** self.rng.uniform(2.5, 4.3), None))
        return peaks

    def _ms2_peaks(
        self, precursor: float, charge: int, lo: float, hi: float
    ) -> list[tuple[float, float, int | None]]:
        mass = precursor * charge - charge * PROTON
        best = min(self.peptides, key=lambda p: abs(p.mass - mass))
        intensity = best.abundance * self._elution(best) * 0.3
        peaks: list[tuple[float, float, int | None]] = []
        if abs(best.mass - mass) < 0.05:
            for f in best.fragments:
                if lo <= f <= hi:
                    peaks.append((f, max(50.0, intensity * self.rng.uniform(0.05, 1.0)), 1))
        for _ in range(25):
            peaks.append((self.rng.uniform(lo, hi), 10 ** self.rng.uniform(1.5, 3.3), None))
        return peaks

    def _acquire(self, values: dict[str, str], access_id: int) -> tuple[ScanRecord, float]:
        v = {**self._defaults(), **values}
        scan_type = v.get("ScanType", "Full").split(";")[0]
        # Validated values may be multi-valued or "-1" (default): never let one crash the scan thread.
        lo = _first_number(v.get("FirstMass"), 350.0) or 350.0
        hi = _first_number(v.get("LastMass"), 1500.0) or 1500.0
        if hi <= lo:
            lo, hi = hi, lo
        resolution = _first_number(v.get("OrbitrapResolution"), 60000.0) or 60000.0
        agc = _first_number(v.get("AGCTarget"), 300000.0) or 300000.0
        max_it = _first_number(v.get("MaxIT"), 50.0) or 50.0
        precursor = _first_number(v.get("PrecursorMass"), None)
        is_ms2 = scan_type == "MSn" and precursor is not None
        if is_ms2:
            charge = int(v.get("_charge", 2))
            peaks = self._ms2_peaks(precursor or 0.0, charge, lo, hi)
        elif scan_type == "SIM" and precursor is not None:
            width = _first_number(v.get("IsolationWidth"), 10.0) or 10.0
            peaks = self._ms1_peaks(precursor - width / 2, precursor + width / 2, resolution)
        else:
            peaks = self._ms1_peaks(lo, hi, resolution)
        flux = sum(i for _, i, _ in peaks) or 1.0  # charges per second reaching the trap
        it_ms = min(max_it, max(0.1, 1000.0 * agc / flux))
        fill = flux * it_ms / 1000.0 / agc  # fraction of AGC target collected
        scale = 1.0 if fill >= 1 else fill
        detect_floor = 200.0 * math.sqrt(resolution / 60000.0)
        centroids = []
        for mz, inten, z in peaks:
            observed = inten * scale * self.rng.lognormvariate(0.0, 0.08)
            if observed >= detect_floor:
                ppm = self.rng.gauss(0.0, 1.5)
                centroids.append(
                    Centroid(mz=round(mz * (1 + ppm * 1e-6), 5), intensity=round(observed, 1), charge=z)
                )
        centroids.sort(key=lambda c: c.intensity, reverse=True)
        self._scan_number += 1
        ms_order = 2 if is_ms2 else 1
        header = {
            "Scan": str(self._scan_number),
            "MSOrder": str(ms_order),
            "ScanMode": "MSn" if is_ms2 else scan_type,
            "FirstMass": f"{lo:g}",
            "LastMass": f"{hi:g}",
            "Polarity": v.get("Polarity", "Positive"),
            "MassAnalyzer": "Orbitrap",
            "Resolution": f"{resolution:g}",
        }
        master = 0
        if is_ms2:
            header["PrecursorMass[0]"] = f"{precursor:.4f}"
            master = self._last_ms1[0] if self._last_ms1 else 0
        trailer = {
            "Access Id:": str(access_id),
            "Master Scan Number:": str(master),
            "AGC Target:": f"{agc:g}",
            "Ion Injection Time (ms):": f"{it_ms:.3f}",
            "Orbitrap Resolution:": f"{resolution:g}",
            "Scan Description:": v.get("ScanDescription", ""),
        }
        if is_ms2:
            trailer["HCD Energy:"] = v.get("CollisionEnergy", "30")
        status_log: dict[str, str] = {}
        if self._sim_t - self._last_status_log >= 5.0:
            self._last_status_log = self._sim_t
            status_log = {
                "Vacuum: Fore Vacuum (mbar)": f"{1.7 + self.rng.gauss(0, 0.02):.2f}",
                "Vacuum: High Vacuum (mbar)": f"{2.3e-5 * (1 + self.rng.gauss(0, 0.02)):.2e}",
                "Vacuum: Ultra High Vacuum (mbar)": f"{1.9e-10 * (1 + self.rng.gauss(0, 0.03)):.2e}",
                "Ion Source: Spray Voltage (V)": f"{2000 + self.rng.gauss(0, 2):.0f}",
                "Ion Source: Ion Transfer Tube Temp (C)": f"{275 + self.rng.gauss(0, 0.2):.1f}",
            }
        record = ScanRecord(
            received_at=_now(),
            header=header,
            trailer=trailer,
            centroid_count=len(centroids),
            centroids=centroids,
            status_log=status_log,
        )
        duration_s = (max(_transient_ms(resolution), it_ms) + 8.0) / 1000.0
        if not is_ms2 and access_id == -1:
            self._last_ms1 = (self._scan_number, centroids)
            if self.acquiring:
                self._schedule_dda(centroids)
        return record, duration_s

    def _schedule_dda(self, centroids: list[Centroid]) -> None:
        now = self._sim_t
        self._exclusion = {k: t for k, t in self._exclusion.items() if now - t < 20.0}
        picked = 0
        for c in centroids:
            if picked >= 10:
                break
            if c.charge is None or c.charge < 2:
                continue
            key = int(round(c.mz * 100))
            if any(abs(key - k) <= 1 for k in self._exclusion):  # +/- 10 mDa exclusion
                continue
            self._exclusion[key] = now
            self._dda_queue.append((c.mz, c.charge))
            picked += 1

    def step(self) -> tuple[ScanRecord | None, float]:
        """Produce the next scan (or None when idle) and how long it took, in simulated seconds."""
        with self._lock:
            if self.acquiring and self.acquisition is not None:
                a = self.acquisition
                time_up = a["mode"] == "duration" and self._sim_t - self._acq_started >= float(
                    a["duration_s"] or 0
                )
                count_done = a["mode"] == "scan_count" and self._acq_scans >= int(a["scan_count"] or 0)
                if time_up or count_done:
                    self._end_acquisition()
            definition = self._next_definition()
            if definition is None:
                return None, 0.05
            record, duration = self._acquire(*definition)
            if self.acquiring:
                self._acq_scans += 1
            self._sim_t += duration
            return record, duration

    def pump(self, n: int) -> int:
        """Deterministically produce up to ``n`` scans and deliver them (for tests with
        ``autostart=False``). Returns how many scans were delivered."""
        delivered = 0
        for _ in range(n):
            record, _ = self.step()
            if record is not None and self._on_scan is not None:
                self._on_scan(record)
                delivered += 1
        return delivered

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                record, duration = self.step()
            except Exception:  # pragma: no cover - a bug here must not silently stop all scans
                log.exception("FakeOrbitrap could not produce a scan; skipping it")
                record, duration = None, 0.1
            if self._stop.wait(duration / self.speed):
                break
            if record is not None and self._on_scan is not None:
                self._on_scan(record)
