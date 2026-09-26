"""Readers ("backends") for mass-spectrometry data files, and the data-folder driver.

This is a *data* server: there is no instrument. ``connect`` opens a :class:`MsDataDriver` on a
data folder (the sandbox root); each run (file or vendor folder) is opened on demand by a
:class:`RunBackend` and cached.

Open interfaces used (APIs checked against the documentation and source of the versions noted):

* **mzML / indexed mzML / mzML.gz** (HUPO-PSI mzML 1.1, https://www.psidev.info/mzML) through
  pyteomics 5.0 (Apache-2.0; https://pyteomics.readthedocs.io/en/latest/api/mzml.html):
  ``pyteomics.mzml.MzML(source, use_index=..., decode_binary=...)``, iteration, ``len()`` and
  ``get_by_index(i)``. Spectrum dictionaries use the PSI-MS controlled-vocabulary names as keys
  ("ms level", "scan start time", "total ion current", "selected ion m/z", ...). The run header
  (instrument model / serial, ``startTimeStamp``) is read with lxml's ``iterparse`` so the
  spectra do not have to be parsed for it. Gzipped files are decompressed to a temporary file
  for random access.
* **mzMLb** (HDF5-wrapped mzML) through ``pyteomics.mzmlb.MzMLb`` (needs h5py; hdf5plugin for
  Blosc-compressed files).
* **Bruker timsTOF .d (TDF)**: run metadata, the TIC/BPC and DDA precursors are read from the
  ``analysis.tdf`` SQLite database (tables ``GlobalMetadata``, ``Frames``, ``Precursors``,
  ``PasefFrameMsMsInfo``; schema as documented with Bruker's TDF SDK and used by open readers
  such as timsrust and alphatims). ``Frames.Time`` is in seconds; ``MsMsType`` 0 = MS1,
  8 = ddaPASEF, 9 = diaPASEF, 2 = MRM. Peak data (spectra, XICs) is decoded with the optional
  ``timsrust-pyo3`` 0.4 bindings (Apache-2.0; https://github.com/jspaezp/timsrust_pyo3, built on
  https://github.com/MannLabs/timsrust): ``FrameReader(path).read_frame(i)`` (0-based;
  ``Frame.tof_indices`` / ``.intensities`` for all mobility scans), ``Metadata(analysis.tdf)
  .resolve_mzs(tof_indices)``, and ``SpectrumReader(path).get(i)`` for DDA precursor spectra
  (one per ``Precursors`` row, in Id order).
"""

from __future__ import annotations

import gzip
import io
import re
import shutil
import sqlite3
import tempfile
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
from labmcp import InstrumentProtocolError

from labmcp_ms_data.files import FORMATS, VENDOR_FORMATS, DataRoot, detect_format

# ---------------------------------------------------------------- common data structures


@dataclass
class RunMetadata:
    instrument_vendor: str | None = None
    instrument_model: str | None = None
    instrument_serial: str | None = None
    acquisition_date: str | None = None
    software: str | None = None
    sample_name: str | None = None
    ion_mobility: bool = False
    mz_acquisition_range: tuple[float, float] | None = None
    notes: list[str] = field(default_factory=list)


_FLOAT_COLS = (
    "rt_min", "tic", "base_peak_mz", "base_peak_intensity", "precursor_mz", "precursor_intensity",
    "injection_time_ms", "low_mz", "high_mz",
)  # fmt: skip
_INT_COLS = ("scan", "ms_level", "precursor_charge", "centroided", "polarity", "precursor_scan")
_INT_DEFAULTS = {"scan": -1, "ms_level": 0, "precursor_charge": 0, "centroided": -1, "polarity": 0,
                 "precursor_scan": -1}  # fmt: skip


@dataclass
class ScanTable:
    """One row per spectrum, in file order. Unknown floats are NaN; unknown ints use the defaults
    above (charge 0, scan -1, centroided -1, polarity 0)."""

    native_id: list[str]
    filter_string: list[str]
    scan: np.ndarray
    ms_level: np.ndarray
    rt_min: np.ndarray
    tic: np.ndarray
    base_peak_mz: np.ndarray
    base_peak_intensity: np.ndarray
    precursor_mz: np.ndarray
    precursor_charge: np.ndarray
    precursor_intensity: np.ndarray
    precursor_scan: np.ndarray
    centroided: np.ndarray  # 1 centroid, 0 profile, -1 unknown
    polarity: np.ndarray  # +1 positive, -1 negative, 0 unknown
    injection_time_ms: np.ndarray
    low_mz: np.ndarray
    high_mz: np.ndarray

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> ScanTable:
        kw: dict[str, Any] = {
            "native_id": [str(r.get("native_id", f"index={i}")) for i, r in enumerate(rows)],
            "filter_string": [str(r.get("filter_string") or "") for r in rows],
        }
        for c in _FLOAT_COLS:
            kw[c] = np.array([_f(r.get(c)) for r in rows], dtype=float)
        for c in _INT_COLS:
            d = _INT_DEFAULTS[c]
            kw[c] = np.array([d if r.get(c) is None else int(r[c]) for r in rows], dtype=np.int64)
        return cls(**kw)

    def __len__(self) -> int:
        return len(self.native_id)


def _f(v: Any) -> float:
    if v is None or v == "":
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


class RunBackend(ABC):
    """Uniform read access to one LC-MS run."""

    format: str = ""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    @abstractmethod
    def metadata(self) -> RunMetadata: ...

    @property
    @abstractmethod
    def table(self) -> ScanTable: ...

    @abstractmethod
    def read_peaks(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        """(m/z, intensity) arrays of spectrum ``index`` (0-based, file order), sorted by m/z."""

    def iter_peaks(self, indices: Iterable[int]) -> Iterator[tuple[int, np.ndarray, np.ndarray]]:
        for i in indices:
            mz, inten = self.read_peaks(int(i))
            yield int(i), mz, inten

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def _check_index(self, index: int) -> None:
        n = len(self.table)
        if not 0 <= index < n:
            raise InstrumentProtocolError(
                f"Spectrum index {index} is out of range (this run has {n} spectra)."
            )


def _sorted_arrays(mz: Any, inten: Any) -> tuple[np.ndarray, np.ndarray]:
    m = np.asarray(mz if mz is not None else [], dtype=float)
    y = np.asarray(inten if inten is not None else [], dtype=float)
    if m.size != y.size:
        raise InstrumentProtocolError(f"Corrupt spectrum: {m.size} m/z values but {y.size} intensities.")
    if m.size > 1 and np.any(np.diff(m) < 0):
        o = np.argsort(m, kind="stable")
        m, y = m[o], y[o]
    return m, y


# ---------------------------------------------------------------- mzML (pyteomics)

_SCAN_RE = re.compile(r"\bscan=(\d+)")
_INDEX_RE = re.compile(r"\b(?:index|spectrum|scanId|cycle)=(\d+)")


def _scan_number(native_id: str) -> int | None:
    m = _SCAN_RE.search(native_id)
    return int(m.group(1)) if m else None


def _minutes(value: Any) -> float:
    """Convert a pyteomics unitfloat retention time to minutes (mzML allows seconds or minutes)."""
    if value is None:
        return float("nan")
    unit = str(getattr(value, "unit_info", "") or "").lower()
    v = float(value)
    if unit in ("second", "s", "uo:0000010"):
        return v / 60.0
    if unit in ("hour", "uo:0000032"):
        return v * 60.0
    return v  # minute (the usual unit) or unspecified


def _array_values(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    if hasattr(value, "decode") and not isinstance(value, (bytes, str)):
        value = value.decode()
    return np.asarray(value, dtype=float)


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def parse_mzml_header(source: Path | BinaryIO) -> RunMetadata:
    """Read instrument configuration and run start time without parsing the spectra."""
    from lxml import etree

    md = RunMetadata()
    groups: dict[str, list[tuple[str, str, str]]] = {}
    configs: list[list[tuple[str, str, str]]] = []
    software: list[str] = []
    current_group: str | None = None
    in_config = 0
    config_params: list[tuple[str, str, str]] = []
    component_depth = 0
    fh = open(source, "rb") if isinstance(source, Path) else nullcontext(source)  # noqa: SIM115
    with fh as stream:
        for event, el in etree.iterparse(stream, events=("start", "end"), huge_tree=True):
            tag = _local(el.tag)
            if event == "start":
                if tag in ("spectrumList", "chromatogramList"):
                    break
                if tag == "referenceableParamGroup":
                    current_group = el.get("id")
                    groups[current_group or ""] = []
                elif tag == "instrumentConfiguration":
                    in_config += 1
                    config_params = []
                elif tag == "componentList":
                    component_depth += 1
                elif tag == "run":
                    md.acquisition_date = el.get("startTimeStamp")
                elif tag == "software":
                    sid = el.get("id") or ""
                    ver = el.get("version") or ""
                    software.append(f"{sid} {ver}".strip())
                continue
            # end events
            if tag == "cvParam":
                item = (el.get("accession") or "", el.get("name") or "", el.get("value") or "")
                if current_group is not None:
                    groups[current_group].append(item)
                elif in_config and not component_depth:
                    config_params.append(item)
            elif tag == "referenceableParamGroupRef" and in_config and not component_depth:
                config_params.extend(groups.get(el.get("ref") or "", []))
            elif tag == "referenceableParamGroup":
                current_group = None
            elif tag == "componentList":
                component_depth -= 1
            elif tag == "instrumentConfiguration":
                in_config -= 1
                configs.append(config_params)
            elif tag == "sourceFile":
                pass
            if tag not in ("run", "mzML"):
                el.clear()
    for params in configs:
        for acc, name, value in params:
            if acc == "MS:1000529":  # instrument serial number
                md.instrument_serial = md.instrument_serial or value or None
            elif acc.startswith("MS:") and name and md.instrument_model is None and not value:
                md.instrument_model = name
    if md.instrument_model and md.instrument_model.lower().endswith("instrument model"):
        md.instrument_model = None  # generic term: model not specified by the converter
    md.software = "; ".join(software) or None
    return md


def _row_from_pyteomics(i: int, s: dict[str, Any]) -> dict[str, Any]:
    native_id = str(s.get("id", f"index={i}"))
    scans = (s.get("scanList") or {}).get("scan") or [{}]
    scan0 = scans[0] if scans else {}
    row: dict[str, Any] = {
        "native_id": native_id,
        "scan": _scan_number(native_id),
        "ms_level": s.get("ms level"),
        "rt_min": _minutes(scan0.get("scan start time")),
        "tic": s.get("total ion current"),
        "base_peak_mz": s.get("base peak m/z"),
        "base_peak_intensity": s.get("base peak intensity"),
        "low_mz": s.get("lowest observed m/z"),
        "high_mz": s.get("highest observed m/z"),
        "injection_time_ms": scan0.get("ion injection time"),
        "filter_string": scan0.get("filter string"),
    }
    if row["ms_level"] is None:
        row["ms_level"] = 1 if "MS1 spectrum" in s else (2 if "MSn spectrum" in s else 0)
    if "centroid spectrum" in s:
        row["centroided"] = 1
    elif "profile spectrum" in s:
        row["centroided"] = 0
    if "positive scan" in s:
        row["polarity"] = 1
    elif "negative scan" in s:
        row["polarity"] = -1
    precs = (s.get("precursorList") or {}).get("precursor") or []
    if precs:
        p = precs[0]
        ions = (p.get("selectedIonList") or {}).get("selectedIon") or [{}]
        ion = ions[0] if ions else {}
        mz = ion.get("selected ion m/z")
        if mz is None:
            mz = (p.get("isolationWindow") or {}).get("isolation window target m/z")
        row["precursor_mz"] = mz
        if ion.get("charge state") is not None:
            row["precursor_charge"] = int(float(ion["charge state"]))
        row["precursor_intensity"] = ion.get("peak intensity")
        ref = p.get("spectrumRef")
        if ref:
            row["precursor_ref"] = str(ref)
    # Summary values missing from the file: compute them from the arrays.
    need = any(row.get(k) is None for k in ("tic", "base_peak_mz", "low_mz"))
    if need:
        mz_arr = _array_values(s.get("m/z array"))
        int_arr = _array_values(s.get("intensity array"))
        if mz_arr is not None and int_arr is not None and int_arr.size:
            if row["tic"] is None:
                row["tic"] = float(int_arr.sum())
            if row["base_peak_mz"] is None:
                k = int(np.argmax(int_arr))
                row["base_peak_mz"] = float(mz_arr[k])
                row["base_peak_intensity"] = float(int_arr[k])
            if row["low_mz"] is None:
                row["low_mz"] = float(mz_arr.min())
                row["high_mz"] = float(mz_arr.max())
        elif int_arr is not None and row["tic"] is None:
            row["tic"] = 0.0
    row["_im"] = _has_ion_mobility(s, scan0)
    return row


def _has_ion_mobility(s: dict[str, Any], scan0: dict[str, Any]) -> bool:
    for key in scan0:
        if "ion mobility" in key or "drift time" in key or "compensation voltage" in key:
            return True
    return any(isinstance(k, str) and ("ion mobility array" in k or "drift time array" in k) for k in s)


_CV_LOCK = threading.Lock()
_CV: Any = None


def psi_ms_cv() -> Any:
    """The PSI-MS controlled vocabulary for pyteomics, loaded once per process.

    pyteomics >= 5 needs psims to parse PSI formats and, by default, downloads the current
    psi-ms.obo for every reader. The copy bundled with psims is used instead (offline, fast);
    if that is unavailable, psims' normal loader (download, bundled fallback) is used.
    """
    global _CV
    with _CV_LOCK:
        if _CV is None:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # psims warns about hdf5plugin on import
                try:
                    from psims.controlled_vocabulary.controlled_vocabulary import ControlledVocabulary
                    from psims.controlled_vocabulary.vendor import _use_vendored_psims_obo

                    with _use_vendored_psims_obo() as fh:
                        _CV = ControlledVocabulary.from_obo(fh)
                except Exception:
                    from psims.controlled_vocabulary.controlled_vocabulary import load_psims

                    _CV = load_psims()
        return _CV


class MzMLBackend(RunBackend):
    """mzML (indexed or not, optionally gzipped) through pyteomics."""

    format = "mzml"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # psims (imported by pyteomics) warns about hdf5plugin
                from pyteomics import mzml  # noqa: F401
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise InstrumentProtocolError(
                "pyteomics is not installed: `pip install pyteomics lxml`."
            ) from exc
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.source = self._prepare_source(path)
        self._md = self._read_header()
        self._reader = self._open_indexed()
        self._table, im = self._build_table()
        self._md.ion_mobility = im

    def _prepare_source(self, path: Path) -> Path:
        if path.name.lower().endswith(".gz"):
            self._tmp = tempfile.TemporaryDirectory(prefix="labmcp-ms-")
            out = Path(self._tmp.name) / path.name[:-3]
            try:
                with gzip.open(path, "rb") as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            except (OSError, EOFError) as exc:
                raise InstrumentProtocolError(f"{path.name} is not a valid gzip file: {exc}") from exc
            return out
        return path

    def _read_header(self) -> RunMetadata:
        try:
            return parse_mzml_header(self.source)
        except Exception as exc:
            raise InstrumentProtocolError(
                f"{self.path.name} is not a readable mzML file (XML error: {exc}). Is the file complete?"
            ) from exc

    def _open_indexed(self) -> Any:
        from pyteomics import mzml

        return mzml.MzML(str(self.source), use_index=True, decode_binary=True, cv=psi_ms_cv())

    def _iter_light(self) -> Iterator[dict[str, Any]]:
        from pyteomics import mzml

        with mzml.MzML(str(self.source), use_index=False, decode_binary=False, cv=psi_ms_cv()) as reader:
            yield from reader

    def _build_table(self) -> tuple[ScanTable, bool]:
        rows: list[dict[str, Any]] = []
        im = False
        try:
            for i, s in enumerate(self._iter_light()):
                row = _row_from_pyteomics(i, s)
                im = im or row.pop("_im")
                rows.append(row)
        except InstrumentProtocolError:
            raise
        except Exception as exc:
            raise InstrumentProtocolError(
                f"Error while reading spectra from {self.path.name} after {len(rows)} spectra: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        by_id = {r["native_id"]: k for k, r in enumerate(rows)}
        for r in rows:
            ref = r.pop("precursor_ref", None)
            if ref is not None and ref in by_id:
                r["precursor_scan"] = rows[by_id[ref]].get("scan") or by_id[ref] + 1
        return ScanTable.from_rows(rows), im

    def metadata(self) -> RunMetadata:
        return self._md

    @property
    def table(self) -> ScanTable:
        return self._table

    def read_peaks(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        self._check_index(index)
        with self.lock:
            try:
                s = self._reader.get_by_index(index)
            except Exception as exc:
                raise InstrumentProtocolError(
                    f"Could not read spectrum {index} from {self.path.name}: {type(exc).__name__}: {exc}"
                ) from exc
        return _sorted_arrays(s.get("m/z array"), s.get("intensity array"))

    def iter_peaks(self, indices: Iterable[int]) -> Iterator[tuple[int, np.ndarray, np.ndarray]]:
        wanted = sorted({int(i) for i in indices})
        if not wanted:
            return
        if len(wanted) < 0.3 * len(self.table):
            yield from super().iter_peaks(wanted)
            return
        from pyteomics import mzml

        want = set(wanted)
        with mzml.MzML(str(self.source), use_index=False, decode_binary=True, cv=psi_ms_cv()) as reader:
            for i, s in enumerate(reader):
                if i in want:
                    mz, inten = _sorted_arrays(s.get("m/z array"), s.get("intensity array"))
                    yield i, mz, inten
                if i >= wanted[-1]:
                    break

    def close(self) -> None:
        try:
            self._reader.close()
        except Exception:
            pass
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


class MzMLbBackend(MzMLBackend):
    """mzMLb (HDF5) through ``pyteomics.mzmlb`` (optional: needs h5py)."""

    format = "mzmlb"

    def __init__(self, path: Path) -> None:
        try:
            import h5py  # noqa: F401
            from pyteomics import mzmlb  # noqa: F401
        except ImportError as exc:
            raise InstrumentProtocolError(
                "Reading mzMLb needs h5py: install `labmcp-ms-data[mzmlb]` (or `pip install h5py hdf5plugin`)."
            ) from exc
        super().__init__(path)

    def _read_header(self) -> RunMetadata:
        # The mzML header lives in the HDF5 dataset "mzML"; pyteomics exposes it as a buffer.
        from pyteomics import mzmlb

        md = RunMetadata()
        try:
            with mzmlb.MzMLb(str(self.source), cv=psi_ms_cv()) as reader:
                buf = reader.handle["mzML"][()]
            try:
                md = parse_mzml_header(io.BytesIO(bytes(buf)))
            except Exception:
                pass
        except Exception as exc:
            md.notes.append(f"Could not read the mzMLb header: {exc}")
        return md

    def _open_indexed(self) -> Any:
        from pyteomics import mzmlb

        return mzmlb.MzMLb(str(self.source), cv=psi_ms_cv())

    def _iter_light(self) -> Iterator[dict[str, Any]]:
        from pyteomics import mzmlb

        with mzmlb.MzMLb(str(self.source), cv=psi_ms_cv()) as reader:
            yield from reader

    def iter_peaks(self, indices: Iterable[int]) -> Iterator[tuple[int, np.ndarray, np.ndarray]]:
        yield from RunBackend.iter_peaks(self, sorted({int(i) for i in indices}))


# ---------------------------------------------------------------- Bruker timsTOF (TDF)

MSMS_TYPES = {0: "MS1", 2: "MRM", 8: "ddaPASEF", 9: "diaPASEF", 10: "prmPASEF"}


def _load_timsrust() -> Any | None:
    try:
        import timsrust_pyo3
    except ImportError:
        return None
    return timsrust_pyo3


class BrukerTdfBackend(RunBackend):
    """Bruker timsTOF ``.d`` folders (TDF). Metadata/TIC via SQLite; peaks via timsrust (optional).

    Spectrum list: one entry per MS1 frame (summed over ion mobility), one per non-DDA MS/MS
    frame (diaPASEF / PRM / MRM, summed over all isolation windows and mobility), and one per DDA
    precursor (from the ``Precursors`` table; ``SpectrumReader`` gives its merged PASEF MS2
    spectrum). Entries are sorted by retention time.
    """

    format = "bruker_tdf"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.tdf = path / "analysis.tdf"
        self.tr = _load_timsrust()
        self._frame_reader: Any = None
        self._spectrum_reader: Any = None
        self._tr_meta: Any = None
        try:
            con = sqlite3.connect(f"{self.tdf.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error as exc:
            raise InstrumentProtocolError(f"Cannot open {self.tdf}: {exc}") from exc
        try:
            self._md, self._table, self._kinds = self._read(con)
        except sqlite3.Error as exc:
            raise InstrumentProtocolError(
                f"{path.name}/analysis.tdf is not a readable TDF database ({exc})."
            ) from exc
        finally:
            con.close()

    @staticmethod
    def _tables(con: sqlite3.Connection) -> set[str]:
        return {r[0].lower() for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _read(self, con: sqlite3.Connection) -> tuple[RunMetadata, ScanTable, list[tuple[str, int]]]:
        tables = self._tables(con)
        if "frames" not in tables:
            raise InstrumentProtocolError(f"{self.path.name}/analysis.tdf has no Frames table.")
        gm: dict[str, str] = {}
        if "globalmetadata" in tables:
            gm = {str(k): str(v) for k, v in con.execute("SELECT Key, Value FROM GlobalMetadata")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(Frames)")}
        acc = "AccumulationTime" if "AccumulationTime" in cols else "NULL"
        frames = con.execute(
            f"SELECT Id, Time, Polarity, MsMsType, SummedIntensities, MaxIntensity, {acc} FROM Frames ORDER BY Id"
        ).fetchall()
        precursors: list[tuple] = []
        prec_rt: dict[int, float] = {}
        if "precursors" in tables:
            precursors = con.execute(
                "SELECT Id, MonoisotopicMz, LargestPeakMz, Charge, Intensity, Parent FROM Precursors ORDER BY Id"
            ).fetchall()
            if "pasefframemsmsinfo" in tables:
                prec_rt = {
                    int(p): float(t)
                    for p, t in con.execute(
                        "SELECT p.Precursor, MIN(f.Time) FROM PasefFrameMsMsInfo p JOIN Frames f "
                        "ON f.Id = p.Frame GROUP BY p.Precursor"
                    )
                }
        frame_time = {int(f[0]): float(f[1]) for f in frames}
        lo = _f(gm.get("MzAcqRangeLower"))
        hi = _f(gm.get("MzAcqRangeUpper"))
        rows: list[dict[str, Any]] = []
        kinds: list[tuple[str, int]] = []
        for fid, t, pol, msms, summed, maxi, acc_t in frames:
            msms = int(msms)
            if msms == 8:
                continue  # ddaPASEF frames are represented by their precursors
            level = 1 if msms == 0 else 2
            rows.append(
                {
                    "native_id": f"frame={fid}",
                    "scan": fid,
                    "ms_level": level,
                    "rt_min": float(t) / 60.0,
                    "tic": summed,
                    "base_peak_intensity": maxi,
                    "polarity": 1 if pol == "+" else (-1 if pol == "-" else 0),
                    "centroided": 1,
                    "injection_time_ms": acc_t,
                    "low_mz": lo,
                    "high_mz": hi,
                    "filter_string": MSMS_TYPES.get(msms, f"MsMsType {msms}"),
                }
            )
            kinds.append(("frame", int(fid)))
        pol_default = rows[0]["polarity"] if rows else 0
        for pid, mono, largest, charge, inten, parent in precursors:
            mz = mono if mono is not None else largest
            t = prec_rt.get(int(pid), frame_time.get(int(parent or 0), float("nan")))
            rows.append(
                {
                    "native_id": f"precursor={pid}",
                    "scan": -1,
                    "ms_level": 2,
                    "rt_min": t / 60.0,
                    "precursor_mz": mz,
                    "precursor_charge": int(charge) if charge else None,
                    "precursor_intensity": inten,
                    "precursor_scan": int(parent) if parent else None,
                    "polarity": pol_default,
                    "centroided": 1,
                    "filter_string": "ddaPASEF precursor",
                }
            )
            kinds.append(("precursor", int(pid)))
        order = sorted(range(len(rows)), key=lambda k: (rows[k]["rt_min"], k))
        rows = [rows[k] for k in order]
        kinds = [kinds[k] for k in order]
        md = RunMetadata(
            instrument_vendor=gm.get("InstrumentVendor") or "Bruker",
            instrument_model=gm.get("InstrumentName") or None,
            instrument_serial=gm.get("InstrumentSerialNumber") or None,
            acquisition_date=gm.get("AcquisitionDateTime") or None,
            software=" ".join(
                x for x in (gm.get("AcquisitionSoftware"), gm.get("AcquisitionSoftwareVersion")) if x
            )
            or None,
            sample_name=gm.get("SampleName") or None,
            ion_mobility=True,
            mz_acquisition_range=(lo, hi) if np.isfinite(lo) and np.isfinite(hi) else None,
        )
        k0lo, k0hi = gm.get("OneOverK0AcqRangeLower"), gm.get("OneOverK0AcqRangeUpper")
        if k0lo and k0hi:
            md.notes.append(f"TIMS 1/K0 range {float(k0lo):.3f}-{float(k0hi):.3f} V·s/cm².")
        if gm.get("MethodName"):
            md.notes.append(f"Method: {gm['MethodName']}")
        md.notes.append(
            "TDF: the TIC is Frames.SummedIntensities (sum over all mobility scans); the base peak "
            "intensity is Frames.MaxIntensity (the most intense single peak in any mobility scan) and "
            "its m/z is not stored."
        )
        if self.tr is None:
            md.notes.append(
                "Spectra and XICs need the optional timsrust reader: `pip install labmcp-ms-data[bruker]`. "
                "Metadata, TIC, BPC and precursor lists work without it."
            )
        return md, ScanTable.from_rows(rows), kinds

    def metadata(self) -> RunMetadata:
        return self._md

    @property
    def table(self) -> ScanTable:
        return self._table

    def _need_timsrust(self) -> Any:
        if self.tr is None:
            raise InstrumentProtocolError(
                "Reading Bruker TDF peak data needs the optional timsrust reader. Install it with "
                "`pip install labmcp-ms-data[bruker]` (or `uv tool install 'labmcp-ms-data[bruker]'`) and restart. "
                "Alternatively convert the .d folder to mzML (convert_to_mzml with msconvert)."
            )
        return self.tr

    def _frame_peaks(self, frame_id: int) -> tuple[np.ndarray, np.ndarray]:
        tr = self._need_timsrust()
        if self._frame_reader is None:
            self._frame_reader = tr.FrameReader(str(self.path))
            self._tr_meta = tr.Metadata(str(self.tdf))
        frame = self._frame_reader.read_frame(frame_id - 1)
        if int(frame.index) != frame_id:
            raise InstrumentProtocolError(f"timsrust returned frame {frame.index} instead of {frame_id}.")
        tof = np.asarray(frame.tof_indices, dtype=np.int64)
        inten = np.asarray(frame.intensities, dtype=float)
        if tof.size == 0:
            return np.zeros(0), np.zeros(0)
        uniq, inv = np.unique(tof, return_inverse=True)
        summed = np.bincount(inv, weights=inten)
        mz = np.asarray(self._tr_meta.resolve_mzs(uniq.tolist()), dtype=float)
        return _sorted_arrays(mz, summed)

    def _precursor_peaks(self, precursor_id: int) -> tuple[np.ndarray, np.ndarray]:
        tr = self._need_timsrust()
        if self._spectrum_reader is None:
            self._spectrum_reader = tr.SpectrumReader(str(self.path))
        spec = self._spectrum_reader.get(precursor_id - 1)
        return _sorted_arrays(spec.mz_values, spec.intensities)

    def read_peaks(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        self._check_index(index)
        kind, ident = self._kinds[index]
        with self.lock:
            try:
                if kind == "frame":
                    return self._frame_peaks(ident)
                return self._precursor_peaks(ident)
            except InstrumentProtocolError:
                raise
            except Exception as exc:
                raise InstrumentProtocolError(
                    f"timsrust could not read {kind} {ident} of {self.path.name}: {type(exc).__name__}: {exc}"
                ) from exc


# ---------------------------------------------------------------- driver


def open_backend(path: Path, fmt: str) -> RunBackend:
    if fmt == "mzml":
        return MzMLBackend(path)
    if fmt == "mzmlb":
        return MzMLbBackend(path)
    if fmt == "bruker_tdf":
        return BrukerTdfBackend(path)
    vendor, label, _ = FORMATS[fmt]
    if fmt in VENDOR_FORMATS:
        raise InstrumentProtocolError(
            f"{path.name} is a {vendor} {label} file, which this server cannot read directly (vendor "
            "formats need the vendor's own libraries). Convert it with convert_to_mzml first, then read "
            "the .mzML file."
        )
    raise InstrumentProtocolError(f"Unsupported format {fmt!r} for {path.name}.")


def _version(dist: str) -> str | None:
    try:
        return pkg_version(dist)
    except PackageNotFoundError:
        return None


class MsDataDriver:
    """The connected 'instrument': a sandboxed data folder plus a small cache of open runs."""

    def __init__(
        self,
        root: DataRoot,
        *,
        simulated_run: RunBackend | None = None,
        cache_size: int = 4,
        converter: dict[str, str] | None = None,
    ) -> None:
        self.root = root
        self.simulated_run = simulated_run
        self.converter = converter or {}
        self.cache_size = max(1, cache_size)
        self._cache: OrderedDict[Path, tuple[float, RunBackend]] = OrderedDict()
        self.lock = threading.RLock()

    @property
    def simulated(self) -> bool:
        return self.simulated_run is not None

    def identify(self) -> dict[str, str]:
        tr = _version("timsrust-pyo3") or _version("timsrust_pyo3")
        info = {
            "manufacturer": "Vendor-neutral (open formats)",
            "model": "Mass-spectrometry data reader",
            "data_folder": str(self.root.root),
            "mzml_reader": f"pyteomics {_version('pyteomics') or '?'}",
            "mzmlb_reader": "available" if _module_available("h5py") else "not installed ([mzmlb] extra)",
            "bruker_tdf_reader": f"SQLite metadata + timsrust-pyo3 {tr}"
            if tr
            else "SQLite metadata only (install the [bruker] extra for spectra)",
            "converter": self.converter.get("converter", "auto"),
        }
        if self.simulated_run is not None:
            info["model"] = "Mass-spectrometry data reader (SIMULATED run)"
            info["simulated_run"] = str(self.simulated_run.path)
        return info

    def open_run(self, user_path: str | None) -> RunBackend:
        """Open (or reuse) the run at ``user_path``; with a single run in the folder the path may be omitted."""
        if self.simulated_run is not None:
            if user_path and Path(user_path).name != self.simulated_run.path.name:
                raise InstrumentProtocolError(
                    f"Simulation mode has one synthetic run, {self.simulated_run.path.name!r}. "
                    "Omit `path` or use that name."
                )
            return self.simulated_run
        if not user_path:
            runs = [r for r in self.root.find_runs(".") if FORMATS[r.format][2]]
            if len(runs) == 1:
                path, fmt = runs[0].path, runs[0].format
            else:
                names = ", ".join(self.root.relative(r.path) for r in runs[:10]) or "none"
                raise InstrumentProtocolError(
                    f"Give the `path` of the run to read ({len(runs)} readable runs found: {names})."
                )
        else:
            path = self.root.resolve(user_path)
            fmt = detect_format(path)
            if fmt is None:
                raise InstrumentProtocolError(
                    f"{user_path!r} is not a recognised MS data file or folder (mzML, mzML.gz, mzMLb, "
                    "Bruker .d, or a vendor format to convert). Call list_runs."
                )
        with self.lock:
            mtime = path.stat().st_mtime
            if fmt == "bruker_tdf":
                mtime = (path / "analysis.tdf").stat().st_mtime
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                self._cache.move_to_end(path)
                return cached[1]
            if cached:
                cached[1].close()
                del self._cache[path]
            backend = open_backend(path, fmt)
            self._cache[path] = (mtime, backend)
            while len(self._cache) > self.cache_size:
                _, (_, old) = self._cache.popitem(last=False)
                old.close()
            return backend

    def display_path(self, backend: RunBackend) -> str:
        if backend is self.simulated_run:
            return str(backend.path)
        return self.root.relative(backend.path)

    def close(self) -> None:
        with self.lock:
            for _, backend in self._cache.values():
                backend.close()
            self._cache.clear()


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None
