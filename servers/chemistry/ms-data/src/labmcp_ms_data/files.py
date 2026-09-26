"""Sandboxed file access and vendor/format detection for mass-spectrometry data.

Every path an agent passes in is resolved (``..`` and symlinks included) and must stay inside
the configured data root. Formats are recognised from the file or folder structure:

=====================  ===========================================  =====================
Format                 How it is recognised                         How it is read
=====================  ===========================================  =====================
mzML (+ .gz)           ``*.mzML`` / ``*.mzML.gz`` file              pyteomics
mzMLb                  ``*.mzMLb`` file (HDF5)                      pyteomics + h5py
Bruker timsTOF (TDF)   ``*.d`` folder containing ``analysis.tdf``   SQLite (+ timsrust)
Bruker BAF             ``*.d`` folder containing ``analysis.baf``   convert (msconvert)
Agilent MassHunter     ``*.d`` folder containing ``AcqData/``       convert (msconvert)
Thermo RAW             ``*.raw`` file                               convert (ThermoRawFileParser / msconvert)
Waters MassLynx        ``*.raw`` folder (``_FUNC*.DAT``)            convert (msconvert)
SCIEX                  ``*.wiff`` / ``*.wiff2`` file                convert (msconvert)
Shimadzu               ``*.lcd`` file                               convert (msconvert)
mzXML                  ``*.mzXML`` file                             convert (msconvert)
=====================  ===========================================  =====================
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from labmcp import InstrumentProtocolError

#: format id -> (vendor, human-readable format, readable directly by this server)
FORMATS: dict[str, tuple[str, str, bool]] = {
    "mzml": ("open standard (HUPO-PSI)", "mzML", True),
    "mzmlb": ("open standard (HUPO-PSI)", "mzMLb", True),
    "bruker_tdf": ("Bruker", "timsTOF .d (TDF)", True),
    "bruker_baf": ("Bruker", ".d (BAF)", False),
    "agilent_d": ("Agilent", "MassHunter .d", False),
    "thermo_raw": ("Thermo Fisher Scientific", ".raw", False),
    "waters_raw": ("Waters", "MassLynx .raw folder", False),
    "sciex_wiff": ("SCIEX", ".wiff/.wiff2", False),
    "shimadzu_lcd": ("Shimadzu", ".lcd", False),
    "mzxml": ("open standard (legacy)", "mzXML", False),
}

VENDOR_FORMATS = {
    "bruker_baf",
    "agilent_d",
    "thermo_raw",
    "waters_raw",
    "sciex_wiff",
    "shimadzu_lcd",
    "mzxml",
}


def detect_format(path: Path) -> str | None:
    """Return the format id of ``path`` (a file or a vendor folder), or None if it is not MS data."""
    name = path.name.lower()
    if path.is_dir():
        if name.endswith(".d"):
            if (path / "analysis.tdf").is_file():
                return "bruker_tdf"
            if (path / "analysis.baf").is_file():
                return "bruker_baf"
            if (path / "AcqData").is_dir():
                return "agilent_d"
            return None
        if name.endswith(".raw"):
            try:
                entries = [p.name.upper() for p in path.iterdir()]
            except OSError:
                return None
            if any(e.startswith("_FUNC") for e in entries) or "_EXTERN.INF" in entries:
                return "waters_raw"
        return None
    if not path.is_file():
        return None
    if name.endswith((".mzml", ".mzml.gz")):
        return "mzml"
    if name.endswith(".mzmlb"):
        return "mzmlb"
    if name.endswith(".raw"):
        return "thermo_raw"
    if name.endswith((".wiff", ".wiff2")):
        return "sciex_wiff"
    if name.endswith(".lcd"):
        return "shimadzu_lcd"
    if name.endswith((".mzxml", ".mzxml.gz")):
        return "mzxml"
    return None


def run_stem(path: Path) -> str:
    """Base name a converter gives the output (``sample.raw`` -> ``sample``)."""
    name = path.name
    for ext in (
        ".mzML.gz",
        ".mzXML.gz",
        ".mzML",
        ".mzMLb",
        ".mzXML",
        ".raw",
        ".d",
        ".wiff2",
        ".wiff",
        ".lcd",
    ):
        if name.lower().endswith(ext.lower()):
            return name[: -len(ext)]
    return path.stem


@dataclass
class FoundRun:
    path: Path
    format: str


class DataRoot:
    """A folder the agent may read from and write into, and nothing outside it."""

    def __init__(self, root: str | os.PathLike[str] | None) -> None:
        raw = Path(root).expanduser() if root else Path.cwd()
        if not raw.exists():
            raise InstrumentProtocolError(
                f"The data folder {str(raw)!r} does not exist. Start the server with "
                "`--address <folder with your MS files>`."
            )
        if not raw.is_dir():
            raise InstrumentProtocolError(f"--address must be a folder, but {str(raw)!r} is a file.")
        self.root = raw.resolve()

    def resolve(self, user_path: str, *, must_exist: bool = True) -> Path:
        """Resolve ``user_path`` (relative to the root, or absolute) and refuse anything outside it.

        Symlinks and ``..`` are resolved first, so a link pointing outside the root is refused too.
        """
        if not user_path or not str(user_path).strip():
            raise InstrumentProtocolError("Empty path.")
        if "\x00" in user_path:
            raise InstrumentProtocolError("Invalid path.")
        p = Path(user_path).expanduser()
        candidate = p if p.is_absolute() else self.root / p
        resolved = candidate.resolve()
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise InstrumentProtocolError(
                f"Refused: {user_path!r} is outside the data folder {str(self.root)!r} "
                "(paths are sandboxed; symlinks and '..' are resolved before checking)."
            )
        if must_exist and not resolved.exists():
            raise InstrumentProtocolError(
                f"{user_path!r} does not exist in the data folder. Call list_runs to see the available files."
            )
        return resolved

    def relative(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return str(path)
        return rel.as_posix() or "."

    def output_path(self, user_path: str, *, suffix: str = ".csv", overwrite: bool = False) -> Path:
        """A new file to write inside the root (its parent folder is created, inside the root).

        A name without an extension gets ``suffix``; any other extension is refused (so a typo can't
        clobber a data file with CSV), and an existing file is refused unless ``overwrite``.
        """
        ext = Path(user_path).suffix
        if not ext:
            user_path += suffix
        elif ext.lower() != suffix.lower():
            raise InstrumentProtocolError(
                f"save_path must end in {suffix} (got {Path(user_path).name!r}). Nothing was written."
            )
        p = self.resolve(user_path, must_exist=False)
        if p.is_dir():
            raise InstrumentProtocolError(f"{user_path!r} is a folder; give a file name. Nothing was written.")
        if p.exists() and not overwrite:
            raise InstrumentProtocolError(
                f"{self.relative(p)!r} already exists and was not overwritten. Choose another save_path or "
                "pass overwrite=true. Nothing was written."
            )
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InstrumentProtocolError(f"Cannot create the folder for {user_path!r}: {exc}") from exc
        # Re-check after mkdir (a parent could be a symlink created in the meantime).
        if not self._inside(p):
            raise InstrumentProtocolError(f"Refused: {user_path!r} is outside the data folder.")
        return p

    def contains(self, p: Path) -> bool:
        """True if ``p`` (symlinks resolved) is the root or inside it."""
        return self._inside(p)

    def find_runs(
        self, subfolder: str = ".", *, recursive: bool = True, max_depth: int = 6
    ) -> list[FoundRun]:
        start = self.resolve(subfolder)
        if not start.is_dir():
            fmt = detect_format(start)
            return [FoundRun(start, fmt)] if fmt else []
        found: list[FoundRun] = []
        base_depth = len(start.parts)
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            here = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                if d.startswith("."):
                    continue
                full = here / d
                if full.is_symlink():
                    continue
                fmt = detect_format(full)
                if fmt:
                    found.append(FoundRun(full, fmt))  # vendor folder: do not descend into it
                elif recursive and len(full.parts) - base_depth < max_depth:
                    keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                if f.startswith("."):
                    continue
                full = here / f
                if full.is_symlink() and not self._inside(full):
                    continue
                fmt = detect_format(full)
                if fmt:
                    found.append(FoundRun(full, fmt))
        return sorted(found, key=lambda r: str(r.path))

    def _inside(self, p: Path) -> bool:
        r = p.resolve()
        return r == self.root or r.is_relative_to(self.root)


def dir_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total
