"""A synthetic LC-MS/MS run behind the same backend interface as the real file readers.

The run imitates a 12-minute reversed-phase LC gradient on a high-resolution Orbitrap-type
instrument in data-dependent acquisition (DDA) mode, positive electrospray:

* eight well-known analytes (small molecules and peptides, singly and doubly charged) elute as
  slightly tailing Gaussian peaks (FWHM ~6-9 s) with isotope envelopes from a Poisson (carbon
  count) approximation, spaced 1.00336/z,
* a constant polysiloxane background ion (m/z 445.1200), 40 persistent chemical-background ions
  (1e5-6e5 counts, which DDA keeps picking after exclusion), ~150 random noise peaks per MS1 scan, 5 % multiplicative intensity noise and a slow baseline drift,
* a one-scan electrospray dropout at ~10.6 min (for the QC tool to find),
* DDA: after each MS1 survey scan, up to five MS2 scans of the most intense precursors above
  1e5 counts (monoisotopic precursor selection, 1.6 m/z isolation, HCD 30 eV, 12 s dynamic
  exclusion at 10 ppm), with fragment ions for each analyte plus noise,
* AGC-like injection times (MS1 up to 50 ms, MS2 up to 100 ms), centroided data.

:func:`write_mzml` writes any run (synthetic or not) as a standard mzML 1.1 file with zlib-
compressed 64-bit arrays, so tests can exercise the real pyteomics reader on the same data.
"""

from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from labmcp_ms_data.driver import RunBackend, RunMetadata, ScanTable

C13_SPACING = 1.003355


@dataclass(frozen=True)
class Compound:
    name: str
    mz: float  # monoisotopic m/z of the observed ion
    charge: int
    carbons: int
    rt_min: float
    fwhm_s: float
    height: float  # apex intensity of the monoisotopic peak
    fragments: tuple[tuple[float, float], ...]  # (m/z, relative intensity)


COMPOUNDS: tuple[Compound, ...] = (
    Compound("caffeine [M+H]+", 195.08765, 1, 8, 2.10, 6.0, 3.0e7,
             ((138.06619, 1.0), (110.07127, 0.35), (83.06037, 0.12), (69.04472, 0.08))),
    Compound("sulfamethoxazole [M+H]+", 254.05939, 1, 10, 4.35, 6.5, 1.2e7,
             ((156.01138, 1.0), (108.04439, 0.55), (92.04948, 0.45), (99.05529, 0.20))),
    Compound("leucine enkephalin [M+H]+", 556.27657, 1, 28, 5.40, 7.0, 8.0e6,
             ((397.20268, 0.65), (278.11356, 0.40), (120.08078, 1.0), (136.07569, 0.50))),
    Compound("angiotensin II [M+2H]2+", 523.77443, 2, 50, 6.20, 7.5, 1.5e7,
             ((784.41037, 0.35), (263.13902, 0.60), (400.19793, 0.45), (513.28200, 0.30), (110.07127, 1.0))),
    Compound("verapamil [M+H]+", 455.29043, 1, 27, 7.80, 7.0, 2.2e7,
             ((165.09100, 1.0), (303.20670, 0.40), (150.06753, 0.20), (260.16451, 0.10))),
    Compound("glu-fibrinopeptide B [M+2H]2+", 785.84265, 2, 66, 8.50, 8.5, 6.0e6,
             ((1056.47494, 0.50), (684.34687, 0.45), (813.38946, 0.30), (480.25650, 0.25), (175.11895, 1.0))),
    Compound("reserpine [M+H]+", 609.28066, 1, 33, 9.60, 8.0, 1.0e7,
             ((195.06519, 1.0), (397.21218, 0.70), (448.19659, 0.20), (236.07061, 0.15))),
    Compound("terfenadine [M+H]+", 472.32100, 1, 32, 10.30, 9.0, 1.8e7,
             ((436.29987, 1.0), (454.31044, 0.45), (262.19581, 0.12))),
)  # fmt: skip

SILOXANE_MZ = 445.12003  # [(C2H6SiO)6 + H]+, ubiquitous background ion
RUN_LENGTH_MIN = 12.0
DROPOUT_RT_MIN = 10.6
MS1_DURATION_S = 0.30
MS2_DURATION_S = 0.09
TOP_N = 5
DDA_THRESHOLD = 1.0e5
EXCLUSION_S = 12.0
EXCLUSION_PPM = 10.0
ISOLATION_WIDTH = 1.6
COLLISION_ENERGY = 30.0


def isotope_pattern(carbons: int, n: int = 4) -> np.ndarray:
    """Relative abundances M, M+1, ... (M = 1) from a Poisson approximation of 13C incorporation."""
    lam = 0.0107 * carbons + 0.004  # small extra for 15N/17O/18O/34S contributions
    pmf = np.array([math.exp(-lam) * lam**k / math.factorial(k) for k in range(n)])
    return pmf / pmf[0]


def elution_profile(rt_min: float, c: Compound) -> float:
    """Slightly tailing Gaussian (trailing side 1.4x wider), normalised to 1 at the apex."""
    sigma = c.fwhm_s / 2.3548 / 60.0
    if rt_min > c.rt_min:
        sigma *= 1.4
    return math.exp(-0.5 * ((rt_min - c.rt_min) / sigma) ** 2)


class SyntheticRun(RunBackend):
    """An in-memory DDA LC-MS/MS run. Deterministic for a given seed."""

    format = "mzml"

    def __init__(self, seed: int = 7, name: str = "simulated_lcms_run.mzML") -> None:
        super().__init__(Path(name))
        self.path = Path(name)
        self.rng = np.random.default_rng(seed)
        self.background = sorted(
            zip(self.rng.uniform(150.0, 1100.0, 40), self.rng.lognormal(np.log(2.0e5), 0.5, 40), strict=True)
        )
        self._peaks: list[tuple[np.ndarray, np.ndarray]] = []
        rows: list[dict] = []
        self._generate(rows)
        self._table = ScanTable.from_rows(rows)

    # ------------------------------------------------------------ generation

    def _ms1(self, rt: float) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float, int, str]]]:
        rng = self.rng
        mzs: list[float] = []
        ints: list[float] = []
        candidates: list[tuple[float, float, int, str]] = []  # (mono m/z, intensity, charge, name)
        spray = 0.08 if abs(rt - DROPOUT_RT_MIN) < 0.004 else 1.0
        drift = 1.0 + 0.15 * math.sin(rt / RUN_LENGTH_MIN * math.pi)  # slow baseline change
        for c in COMPOUNDS:
            prof = elution_profile(rt, c)
            if prof < 1e-4:
                continue
            base = c.height * prof * spray
            for k, rel in enumerate(isotope_pattern(c.carbons)):
                inten = base * rel * rng.lognormal(0.0, 0.05)
                if inten < 800:
                    continue
                mz = c.mz + k * C13_SPACING / c.charge
                mzs.append(mz + rng.normal(0.0, mz * 1.0e-6))  # ~1 ppm mass error
                ints.append(inten)
            candidates.append((c.mz, base, c.charge, c.name))
        sil = 1.5e6 * drift * spray * rng.lognormal(0.0, 0.05)
        for k, rel in enumerate(isotope_pattern(12, 3)):
            mzs.append(SILOXANE_MZ + k * C13_SPACING)
            ints.append(sil * rel * (1.0 + 2.6 * (k == 2)))  # Si isotopes make M+2 larger
        candidates.append((SILOXANE_MZ, sil, 1, "polysiloxane background"))
        for bmz, bint in self.background:
            inten = bint * drift * spray * rng.lognormal(0.0, 0.1)
            mzs.append(bmz)
            ints.append(inten)
            candidates.append((float(bmz), inten, 1, "chemical background"))
        n_noise = int(rng.integers(120, 180))
        mzs.extend(rng.uniform(100.0, 1200.0, n_noise).tolist())
        ints.extend((rng.lognormal(math.log(6e3), 0.8, n_noise) * drift * spray).tolist())
        order = np.argsort(mzs)
        return np.asarray(mzs)[order], np.asarray(ints)[order], candidates

    def _ms2(self, prec_mz: float, prec_int: float, name: str) -> tuple[np.ndarray, np.ndarray]:
        rng = self.rng
        comp = next((c for c in COMPOUNDS if c.name == name), None)
        mzs: list[float] = []
        ints: list[float] = []
        scale = 0.25 * prec_int
        if comp is not None:
            for mz, rel in comp.fragments:
                mzs.append(mz + rng.normal(0.0, mz * 1.5e-6))
                ints.append(scale * rel * rng.lognormal(0.0, 0.1))
        elif name.startswith("polysiloxane"):
            for mz, rel in ((429.08873, 1.0), (355.07000, 0.5), (281.05120, 0.3)):
                mzs.append(mz)
                ints.append(scale * rel * rng.lognormal(0.0, 0.1))
        else:  # chemical background: a few fragments at fixed fractions of the precursor m/z
            frag_rng = np.random.default_rng(int(prec_mz * 1000))
            for mz in frag_rng.uniform(60.0, prec_mz - 17.0, 5):
                mzs.append(float(mz))
                ints.append(scale * frag_rng.uniform(0.1, 1.0) * rng.lognormal(0.0, 0.1))
        mzs.append(prec_mz)
        ints.append(0.1 * scale)  # surviving precursor
        n_noise = int(rng.integers(15, 40))
        mzs.extend(rng.uniform(50.0, prec_mz * 1.1, n_noise).tolist())
        ints.extend(rng.lognormal(math.log(max(scale, 1.0) * 0.004), 0.7, n_noise).tolist())
        order = np.argsort(mzs)
        return np.asarray(mzs)[order], np.asarray(ints)[order]

    def _add(self, rows: list[dict], mz: np.ndarray, inten: np.ndarray, **meta) -> int:
        idx = len(rows)
        imax = int(np.argmax(inten))
        rows.append(
            {
                "native_id": f"controllerType=0 controllerNumber=1 scan={idx + 1}",
                "scan": idx + 1,
                "tic": float(inten.sum()),
                "base_peak_mz": float(mz[imax]),
                "base_peak_intensity": float(inten[imax]),
                "low_mz": float(mz[0]),
                "high_mz": float(mz[-1]),
                "centroided": 1,
                "polarity": 1,
                **meta,
            }
        )
        self._peaks.append((mz, inten))
        return idx

    def _generate(self, rows: list[dict]) -> None:
        t_s = 0.0
        exclusion: list[tuple[float, float]] = []  # (m/z, excluded until t_s)
        while t_s / 60.0 < RUN_LENGTH_MIN:
            rt = t_s / 60.0
            mz, inten, candidates = self._ms1(rt)
            tic = float(inten.sum())
            ms1_it = float(np.clip(3.0e8 / max(tic, 1.0), 1.0, 50.0))
            ms1_index = self._add(
                rows, mz, inten, ms_level=1, rt_min=rt, injection_time_ms=ms1_it,
                filter_string="FTMS + c ESI Full ms [100.0000-1200.0000]",
            )  # fmt: skip
            t_s += MS1_DURATION_S
            exclusion = [(m, until) for m, until in exclusion if until > t_s]
            picks = sorted(candidates, key=lambda c: c[1], reverse=True)
            n = 0
            for prec_mz, prec_int, charge, name in picks:
                if n >= TOP_N or prec_int < DDA_THRESHOLD:
                    break
                if any(abs(prec_mz - m) / m * 1e6 < EXCLUSION_PPM for m, _ in exclusion):
                    continue
                frag_mz, frag_int = self._ms2(prec_mz, prec_int, name)
                it = float(np.clip(2.0e9 / prec_int, 5.0, 100.0))
                self._add(
                    rows, frag_mz, frag_int, ms_level=2, rt_min=t_s / 60.0, injection_time_ms=it,
                    precursor_mz=prec_mz, precursor_charge=charge, precursor_intensity=prec_int,
                    precursor_scan=ms1_index + 1,
                    filter_string=f"FTMS + c ESI d Full ms2 {prec_mz:.4f}@hcd{COLLISION_ENERGY:.2f} "
                    f"[{50.0:.4f}-{prec_mz * 1.1:.4f}]",
                )  # fmt: skip
                exclusion.append((prec_mz, t_s + EXCLUSION_S))
                t_s += MS2_DURATION_S
                n += 1
            t_s += 0.02

    # ------------------------------------------------------------ backend interface

    def metadata(self) -> RunMetadata:
        return RunMetadata(
            instrument_vendor="Thermo Fisher Scientific (simulated)",
            instrument_model="Q Exactive (simulated)",
            instrument_serial="SIM-0001",
            acquisition_date="2026-01-15T09:30:00Z",
            software="labmcp-ms-data simulator",
            sample_name="Synthetic 8-analyte standard mix",
            ion_mobility=False,
            notes=["SIMULATED data: a synthetic DDA LC-MS/MS run, not a real measurement."],
        )

    @property
    def table(self) -> ScanTable:
        return self._table

    def read_peaks(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        self._check_index(index)
        return self._peaks[index]

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- mzML writer


def _cv(acc: str, name: str, value: object = "", unit: tuple[str, str] | None = None) -> str:
    unit_attrs = ""
    if unit:
        ucv = unit[0].split(":")[0]
        unit_attrs = f' unitCvRef="{ucv}" unitAccession="{unit[0]}" unitName="{unit[1]}"'
    v = "" if value is None else str(value)
    return f'<cvParam cvRef="MS" accession="{acc}" name="{name}" value={quoteattr(v)}{unit_attrs}/>'


def _array(values: np.ndarray, acc: str, name: str, unit: tuple[str, str]) -> str:
    data = base64.b64encode(zlib.compress(np.asarray(values, dtype="<f8").tobytes())).decode()
    return (
        f'<binaryDataArray encodedLength="{len(data)}">'
        + _cv("MS:1000523", "64-bit float")
        + _cv("MS:1000574", "zlib compression")
        + _cv(acc, name, "", unit)
        + f"<binary>{data}</binary></binaryDataArray>"
    )


MINUTE = ("UO:0000031", "minute")
MS_UNIT = ("UO:0000028", "millisecond")
MZ_UNIT = ("MS:1000040", "m/z")
COUNTS = ("MS:1000131", "number of detector counts")
EV = ("UO:0000266", "electronvolt")


def write_mzml(run: RunBackend, path: str | Path, *, run_id: str = "run1") -> Path:
    """Write ``run`` to an (unindexed) mzML 1.1.0 file and return its path."""
    path = Path(path)
    t = run.table
    md = run.metadata()
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">',
        '<cvList count="2"><cv id="MS" fullName="Proteomics Standards Initiative Mass Spectrometry Ontology" '
        'URI="https://raw.githubusercontent.com/HUPO-PSI/psi-ms-CV/master/psi-ms.obo"/>'
        '<cv id="UO" fullName="Unit Ontology" '
        'URI="https://raw.githubusercontent.com/bio-ontology-research-group/unit-ontology/master/unit.obo"/></cvList>',
        "<fileDescription><fileContent>"
        + _cv("MS:1000579", "MS1 spectrum")
        + _cv("MS:1000580", "MSn spectrum")
        + "</fileContent></fileDescription>",
        '<softwareList count="1"><software id="labmcp" version="0.1.0">'
        + _cv("MS:1000799", "custom unreleased software tool", "labmcp-ms-data")
        + "</software></softwareList>",
        '<instrumentConfigurationList count="1"><instrumentConfiguration id="IC1">'
        + _cv("MS:1001911", "Q Exactive")
        + _cv("MS:1000529", "instrument serial number", md.instrument_serial or "")
        + '<componentList count="3"><source order="1">'
        + _cv("MS:1000073", "electrospray ionization")
        + '</source><analyzer order="2">'
        + _cv("MS:1000484", "orbitrap")
        + '</analyzer><detector order="3">'
        + _cv("MS:1000624", "inductive detector")
        + "</detector></componentList></instrumentConfiguration></instrumentConfigurationList>",
        '<dataProcessingList count="1"><dataProcessing id="dp1"><processingMethod order="1" softwareRef="labmcp">'
        + _cv("MS:1000544", "Conversion to mzML")
        + "</processingMethod></dataProcessing></dataProcessingList>",
        f'<run id="{run_id}" defaultInstrumentConfigurationRef="IC1"'
        + (f' startTimeStamp="{escape(md.acquisition_date)}"' if md.acquisition_date else "")
        + ">",
        f'<spectrumList count="{len(t)}" defaultDataProcessingRef="dp1">',
    ]
    for i in range(len(t)):
        mz, inten = run.read_peaks(i)
        level = int(t.ms_level[i])
        parts = [
            f'<spectrum index="{i}" id={quoteattr(t.native_id[i])} defaultArrayLength="{len(mz)}">',
            _cv("MS:1000511", "ms level", level),
            _cv("MS:1000579", "MS1 spectrum") if level == 1 else _cv("MS:1000580", "MSn spectrum"),
            _cv("MS:1000127", "centroid spectrum")
            if t.centroided[i] != 0
            else _cv("MS:1000128", "profile spectrum"),
            _cv("MS:1000129", "negative scan") if t.polarity[i] < 0 else _cv("MS:1000130", "positive scan"),
            _cv("MS:1000285", "total ion current", f"{t.tic[i]:.6g}"),
            _cv("MS:1000504", "base peak m/z", f"{t.base_peak_mz[i]:.6f}", MZ_UNIT),
            _cv("MS:1000505", "base peak intensity", f"{t.base_peak_intensity[i]:.6g}", COUNTS),
            _cv("MS:1000528", "lowest observed m/z", f"{t.low_mz[i]:.6f}", MZ_UNIT),
            _cv("MS:1000527", "highest observed m/z", f"{t.high_mz[i]:.6f}", MZ_UNIT),
            '<scanList count="1">' + _cv("MS:1000795", "no combination") + "<scan>",
            _cv("MS:1000016", "scan start time", f"{t.rt_min[i]:.6f}", MINUTE),
        ]
        if t.filter_string[i]:
            parts.append(_cv("MS:1000512", "filter string", t.filter_string[i]))
        if np.isfinite(t.injection_time_ms[i]):
            parts.append(_cv("MS:1000927", "ion injection time", f"{t.injection_time_ms[i]:.4g}", MS_UNIT))
        parts.append("</scan></scanList>")
        if level > 1 and np.isfinite(t.precursor_mz[i]):
            ref = ""
            if t.precursor_scan[i] > 0:
                ref = f" spectrumRef={quoteattr(t.native_id[int(t.precursor_scan[i]) - 1])}"
            half = ISOLATION_WIDTH / 2
            parts.append(
                f'<precursorList count="1"><precursor{ref}><isolationWindow>'
                + _cv("MS:1000827", "isolation window target m/z", f"{t.precursor_mz[i]:.6f}", MZ_UNIT)
                + _cv("MS:1000828", "isolation window lower offset", f"{half}", MZ_UNIT)
                + _cv("MS:1000829", "isolation window upper offset", f"{half}", MZ_UNIT)
                + '</isolationWindow><selectedIonList count="1"><selectedIon>'
                + _cv("MS:1000744", "selected ion m/z", f"{t.precursor_mz[i]:.6f}", MZ_UNIT)
                + (
                    _cv("MS:1000041", "charge state", int(t.precursor_charge[i]))
                    if t.precursor_charge[i]
                    else ""
                )
                + (
                    _cv("MS:1000042", "peak intensity", f"{t.precursor_intensity[i]:.6g}", COUNTS)
                    if np.isfinite(t.precursor_intensity[i])
                    else ""
                )
                + "</selectedIon></selectedIonList><activation>"
                + _cv("MS:1000422", "beam-type collision-induced dissociation")
                + _cv("MS:1000045", "collision energy", f"{COLLISION_ENERGY}", EV)
                + "</activation></precursor></precursorList>"
            )
        parts.append('<binaryDataArrayList count="2">')
        parts.append(_array(mz, "MS:1000514", "m/z array", MZ_UNIT))
        parts.append(_array(inten, "MS:1000515", "intensity array", COUNTS))
        parts.append("</binaryDataArrayList></spectrum>")
        lines.append("".join(parts))
    lines += ["</spectrumList>", "</run>", "</mzML>"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
