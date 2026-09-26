"""File-based backend for LC-MS sample-queue (worklist / sequence / batch) import files.

There is no instrument connection. The "driver" is a :class:`WorklistStore` bound to one output
folder: it keeps in-memory draft worklists, validates them, and writes vendor import files into
that folder only (no ``..``, no absolute paths elsewhere, no symlink escapes, no overwriting
unless asked). A person then imports the file into the acquisition software, or the software
picks it up from a watched folder where the vendor documents that. Nothing here starts an
acquisition.

The file formats and their sources (Waters MassLynx WKB63781 and Getting Started Guide
715009602; SCIEX OS KB "Importing a .txt or .csv file into a batch"; Agilent MassHunter worklist
import example and KPRs; Thermo Xcalibur 2.2 Acquisition and Processing User Guide XCALI-97209 D)
are documented in :mod:`labmcp_ms_worklist.formats`.
"""

from __future__ import annotations

import csv
import functools
import json
import math
import os
import random
import re
import secrets
import shutil
import string
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from labmcp import InstrumentError
from pydantic import BaseModel, Field

from labmcp_ms_worklist.formats import (
    AGILENT,
    FORMATS,
    POSITION_PATTERNS,
    TEXT_FIELDS,
    FormatSpec,
    Sample,
    check_position,
    control_char_problem,
    decode,
    encode,
    filename_problem,
    generate_positions,
    normalise_header,
    parse,
    parse_number,
    render,
    resolve_columns,
    sanitise_filename,
    volume_overrides,
)

MAX_IMPORT_BYTES = 5_000_000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PLACEHOLDERS = ("index", "sample_name", "sample_id", "type", "worklist", "date", "position")


class WorklistError(InstrumentError):
    """A worklist operation was refused (bad input, sandbox violation, validation failure)."""


def _locked(method: Any) -> Any:
    """Serialise store operations: FastMCP runs sync tools in a thread pool."""

    @functools.wraps(method)
    def wrapper(self: WorklistStore, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


class Defaults(BaseModel):
    """Values applied to samples that don't set them, plus naming and position rules."""

    ms_method: str = ""
    lc_method: str = ""
    tune_file: str = ""
    processing_method: str = ""
    data_path: str = ""
    injection_volume_ul: float | None = None
    data_file_pattern: str = "{worklist}_{index:03d}_{sample_name}"
    position_pattern: str = "any"
    plate_size: int = 96
    max_vial: int = 120
    first_position: str = ""
    vendor_columns: dict[str, str] = Field(default_factory=dict)


class ControlPlan(BaseModel):
    """How blanks/QCs are inserted and whether the run order is randomised (re-applied on every change)."""

    blank_every_n: int | None = None
    blank_at_start: bool = False
    blank_at_end: bool = False
    blank_name: str = "Blank"
    blank_position: str = ""
    blank_ms_method: str = ""
    blank_injection_volume_ul: float | None = None
    qc_every_n: int | None = None
    qc_at_start: int = 0
    qc_at_end: int = 0
    qc_name: str = "QC"
    qc_position: str = ""
    qc_injection_volume_ul: float | None = None
    randomize: bool = False
    seed: int | None = None
    randomize_types: list[str] = Field(default_factory=lambda: ["sample"])


class Entry(BaseModel):
    sample: Sample
    auto_data_file: bool = False
    inserted: bool = False


class Worklist(BaseModel):
    name: str
    format: str
    defaults: Defaults
    base: list[Entry] = Field(default_factory=list)
    plan: ControlPlan | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    created: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    source_file: str | None = None
    xcalibur_bracket_type: int = 4
    template_columns: list[str] | None = None
    delimiter: str | None = None

    def record(self, op: str, **params: Any) -> None:
        self.history.append(
            {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op, **params}
        )


class Issue(BaseModel):
    row: int | None = Field(None, description="1-based run-order row, or None for the whole worklist")
    field: str
    message: str


class ValidationReport(BaseModel):
    worklist: str
    format: str
    valid: bool = Field(description="True when there are no errors (warnings are allowed)")
    sample_count: int
    errors: list[Issue]
    warnings: list[Issue]


def _now_date() -> str:
    return datetime.now().strftime("%Y%m%d")


class WorklistStore:
    """Drafts in memory plus a sandboxed output folder."""

    def __init__(self, output_dir: str | os.PathLike[str], *, simulated: bool = False) -> None:
        root = Path(output_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise WorklistError(f"Output folder {str(root)!r} is not a directory.")
        self.root = root.resolve()
        self.simulated = simulated
        self.drafts: dict[str, Worklist] = {}
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ basics

    def identify(self) -> dict[str, Any]:
        return {
            "manufacturer": "LabMCP",
            "model": "LC-MS worklist file writer (no instrument connection; writes import files only)",
            "output_dir": str(self.root),
            "temporary_output_dir": self.simulated,
            "writable": os.access(self.root, os.W_OK),
            "formats": sorted(FORMATS),
            "drafts": sorted(self.drafts),
        }

    def close(self) -> None:
        if self.simulated:
            shutil.rmtree(self.root, ignore_errors=True)

    def get(self, name: str) -> Worklist:
        try:
            return self.drafts[name]
        except KeyError:
            known = ", ".join(sorted(self.drafts)) or "(none)"
            raise WorklistError(f"No worklist named {name!r}. Existing drafts: {known}.") from None

    # ----------------------------------------------------------------- sandbox

    def resolve(self, relpath: str) -> Path:
        """Resolve ``relpath`` inside the output folder or refuse."""
        if not relpath or not relpath.strip():
            raise WorklistError("A file name is required.")
        if "\x00" in relpath:
            raise WorklistError("File names cannot contain NUL characters.")
        parts = re.split(r"[\\/]+", relpath)
        if ".." in parts:
            raise WorklistError(
                f"Refused {relpath!r}: '..' is not allowed. Files can only be read from and written to the "
                f"worklist folder {str(self.root)!r}."
            )
        candidate = Path(relpath)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise WorklistError(
                f"Refused {relpath!r}: it resolves outside the worklist folder {str(self.root)!r} "
                "(absolute path elsewhere or a symlink escape)."
            )
        return resolved

    def write_file(self, relpath: str, data: bytes, *, overwrite: bool) -> Path:
        target = self.resolve(relpath)
        if target == self.root:
            raise WorklistError("Refused: the target is the worklist folder itself.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after creating parents, in case a parent is a symlink created meanwhile.
        if not target.parent.resolve().is_relative_to(self.root):
            raise WorklistError(f"Refused {relpath!r}: its folder resolves outside the worklist folder.")
        if target.is_dir():
            raise WorklistError(f"Refused {relpath!r}: a folder with that name exists.")
        try:
            if overwrite:
                tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                with open(target, "xb") as fh:
                    fh.write(data)
        except FileExistsError:
            raise WorklistError(
                f"{relpath!r} already exists in the worklist folder. Choose another file name or pass "
                "overwrite=true."
            ) from None
        except OSError as exc:
            raise WorklistError(f"Could not write {relpath!r}: {exc}") from exc
        return target

    def read_file(self, relpath: str) -> bytes:
        path = self.resolve(relpath)
        if not path.is_file():
            raise WorklistError(f"{relpath!r} does not exist in the worklist folder {str(self.root)!r}.")
        try:
            if path.stat().st_size > MAX_IMPORT_BYTES:
                raise WorklistError(f"{relpath!r} is larger than {MAX_IMPORT_BYTES // 1_000_000} MB; not a worklist?")
            return path.read_bytes()
        except OSError as exc:
            raise WorklistError(f"Could not read {relpath!r}: {exc}") from exc

    def _decode(self, relpath: str) -> str:
        try:
            return decode(self.read_file(relpath))
        except ValueError as exc:
            raise WorklistError(f"Could not read {relpath!r}: {exc}.") from exc

    def list_files(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                out.append({"path": p.relative_to(self.root).as_posix(), "bytes": p.stat().st_size})
        return out

    # ----------------------------------------------------------------- drafts

    @_locked
    def create(
        self,
        name: str,
        fmt: str,
        samples: list[Sample],
        defaults: Defaults,
        *,
        replace: bool = False,
    ) -> Worklist:
        if fmt not in FORMATS:
            raise WorklistError(f"Unknown format {fmt!r}. Use one of: {', '.join(sorted(FORMATS))}.")
        if not NAME_RE.fullmatch(name):
            raise WorklistError(
                "Worklist names must be 1-64 characters of letters, digits, '_', '-' or '.', starting with a "
                "letter or digit (they are used in file names)."
            )
        if name in self.drafts and not replace:
            raise WorklistError(f"A worklist named {name!r} already exists; pass replace=true to start over.")
        self._check_defaults(defaults)
        wl = Worklist(name=name, format=fmt, defaults=defaults)
        wl.record("create_worklist", format=fmt, defaults=defaults.model_dump(), sample_count=len(samples))
        self.drafts[name] = wl
        self._append(wl, samples)
        return wl

    @_locked
    def add(self, name: str, samples: list[Sample]) -> Worklist:
        wl = self.get(name)
        self._append(wl, samples)
        wl.record("add_samples", sample_count=len(samples))
        return wl

    @_locked
    def set_plan(self, name: str, plan: ControlPlan) -> Worklist:
        wl = self.get(name)
        if plan.randomize and plan.seed is None:
            plan.seed = secrets.randbelow(2**31)
        for t in plan.randomize_types:
            if t not in ("sample", "blank", "qc", "standard", "solvent", "double_blank"):
                raise WorklistError(f"Unknown sample type {t!r} in randomize_types.")
        wl.plan = plan
        wl.record("insert_qc_blanks", **plan.model_dump())
        return wl

    @staticmethod
    def _check_defaults(d: Defaults) -> None:
        if d.position_pattern not in POSITION_PATTERNS:
            raise WorklistError(
                f"Unknown position_pattern {d.position_pattern!r}. Use one of: {', '.join(POSITION_PATTERNS)}."
            )
        if d.plate_size not in (24, 48, 54, 96, 384):
            raise WorklistError("plate_size must be 24, 48, 54, 96 or 384.")
        hint = "Placeholders: {index}, {sample_name}, {sample_id}, {type}, {worklist}, {date}, {position}."
        try:
            fields = list(string.Formatter().parse(d.data_file_pattern))
            for _, fld, spec, conv in fields:
                if fld is None:
                    continue
                # Only plain placeholders: no attribute/index access ('{sample_name.__class__}'), no
                # conversions, and no huge widths ('{index:>999999999}' would allocate a gigabyte).
                if fld not in PLACEHOLDERS or conv is not None:
                    raise ValueError(f"unknown placeholder {{{fld}{'!' + conv if conv else ''}}}")
                if spec and ("{" in spec or re.search(r"\d{3,}", spec)):
                    raise ValueError(f"format spec {spec!r} of {{{fld}}} is not allowed (use e.g. {{index:03d}})")
            d.data_file_pattern.format_map(_PatternValues(1, "S", "", "sample", "WL", "A1"))
        except (KeyError, ValueError, IndexError, AttributeError) as exc:
            raise WorklistError(f"Invalid data_file_pattern {d.data_file_pattern!r}: {exc}. {hint}") from exc

    def _append(self, wl: Worklist, samples: list[Sample]) -> None:
        d = wl.defaults
        new: list[Entry] = []
        for s in samples:
            s = s.model_copy(deep=True)
            for f in ("ms_method", "lc_method", "tune_file", "processing_method", "data_path"):
                if not getattr(s, f) and getattr(d, f):
                    setattr(s, f, getattr(d, f))
            if s.injection_volume_ul is None and d.injection_volume_ul is not None:
                s.injection_volume_ul = d.injection_volume_ul
            for k, v in d.vendor_columns.items():
                s.extra.setdefault(k, v)
            new.append(Entry(sample=s, auto_data_file=not s.data_file))
        missing = [e for e in new if not e.sample.position]
        if missing and d.first_position:
            used = [e.sample.position for e in wl.base if e.sample.position]
            start = d.first_position
            try:
                if used:  # continue after the last position already in the worklist
                    start = generate_positions(d.position_pattern, d.plate_size, d.max_vial, used[-1], 2)[1]
                positions = generate_positions(d.position_pattern, d.plate_size, d.max_vial, start, len(missing))
            except ValueError as exc:
                raise WorklistError(str(exc)) from exc
            for e, p in zip(missing, positions, strict=True):
                e.sample.position = p
        wl.base.extend(new)

    @_locked
    def entries(self, wl: Worklist, spec: FormatSpec | None = None) -> list[Entry]:
        """The run order: base samples, randomised and with controls inserted per the plan.

        With ``spec`` of another vendor than the draft's, ``extra`` values that would override one
        of that vendor's mapped columns (e.g. Xcalibur's verbatim 'Std Clear' sample type) are left
        out: they belong to the draft's own format.
        """
        rows = [e.model_copy(deep=True) for e in wl.base]
        plan = wl.plan
        if plan is not None:
            if plan.randomize:
                slots = [i for i, e in enumerate(rows) if e.sample.sample_type in plan.randomize_types]
                picked = [rows[i] for i in slots]
                random.Random(plan.seed).shuffle(picked)
                for i, e in zip(slots, picked, strict=True):
                    rows[i] = e
            rows = self._insert_controls(wl, rows, plan)
        self._name_files(wl, rows)
        if spec is not None and spec.name != wl.format:
            for e in rows:
                e.sample.extra = {k: v for k, v in e.sample.extra.items() if not _maps(spec, k)}
        return rows

    def samples(self, wl: Worklist, spec: FormatSpec | None = None) -> list[Sample]:
        return [e.sample for e in self.entries(wl, spec)]

    def _control(self, wl: Worklist, kind: str, plan: ControlPlan) -> Entry:
        d = wl.defaults
        if kind == "blank":
            s = Sample(
                sample_name=plan.blank_name,
                sample_type="blank",
                position=plan.blank_position,
                injection_volume_ul=plan.blank_injection_volume_ul
                if plan.blank_injection_volume_ul is not None
                else d.injection_volume_ul,
                ms_method=plan.blank_ms_method or d.ms_method,
            )
        else:
            s = Sample(
                sample_name=plan.qc_name,
                sample_type="qc",
                position=plan.qc_position,
                injection_volume_ul=plan.qc_injection_volume_ul
                if plan.qc_injection_volume_ul is not None
                else d.injection_volume_ul,
                ms_method=d.ms_method,
            )
        s.lc_method, s.tune_file = d.lc_method, d.tune_file
        s.processing_method, s.data_path = d.processing_method, d.data_path
        s.extra = dict(d.vendor_columns)
        return Entry(sample=s, auto_data_file=True, inserted=True)

    def _insert_controls(self, wl: Worklist, rows: list[Entry], plan: ControlPlan) -> list[Entry]:
        out: list[Entry] = []
        if plan.blank_at_start:
            out.append(self._control(wl, "blank", plan))
        out.extend(self._control(wl, "qc", plan) for _ in range(plan.qc_at_start))
        for i, e in enumerate(rows, start=1):
            out.append(e)
            last = i == len(rows)
            # After the last sample the periodic control is only skipped when the matching at-end
            # control follows (6 samples, blank every 3 -> a blank after sample 3 AND after sample 6).
            if plan.qc_every_n and i % plan.qc_every_n == 0 and not (last and plan.qc_at_end):
                out.append(self._control(wl, "qc", plan))
            if plan.blank_every_n and i % plan.blank_every_n == 0 and not (last and plan.blank_at_end):
                out.append(self._control(wl, "blank", plan))
        out.extend(self._control(wl, "qc", plan) for _ in range(plan.qc_at_end))
        if plan.blank_at_end:
            out.append(self._control(wl, "blank", plan))
        return out

    def _name_files(self, wl: Worklist, rows: list[Entry]) -> None:
        date = wl.created[:10].replace("-", "")
        for i, e in enumerate(rows, start=1):
            if e.auto_data_file:
                s = e.sample
                values = _PatternValues(i, s.sample_name, s.sample_id, s.sample_type, wl.name, s.position, date)
                e.sample.data_file = sanitise_filename(wl.defaults.data_file_pattern.format_map(values))

    # --------------------------------------------------------------- validate

    @_locked
    def validate(
        self,
        name: str,
        *,
        max_injection_volume_ul: float,
        fmt: str | None = None,
        has_template: bool | None = None,
    ) -> ValidationReport:
        wl = self.get(name)
        spec = FORMATS[fmt or wl.format]
        if has_template is None:
            has_template = wl.template_columns is not None and spec.name == wl.format
        entries = self.entries(wl, spec)
        errors: list[Issue] = []
        warnings: list[Issue] = []
        d = wl.defaults

        if not entries:
            errors.append(Issue(field="samples", message="The worklist has no samples."))
        seen_files: dict[str, int] = {}
        seen_pairs: dict[tuple[str, str], int] = {}
        for row, e in enumerate(entries, start=1):
            s = e.sample
            for f in spec.required:
                if (s.injection_volume_ul is None) if f == "injection_volume_ul" else not getattr(s, f):
                    errors.append(Issue(row=row, field=f, message=f"{f} is required by {spec.title}."))
            for f in spec.recommended:
                if (s.injection_volume_ul is None) if f == "injection_volume_ul" else not getattr(s, f):
                    warnings.append(Issue(row=row, field=f, message=f"{f} is empty (usually needed)."))
            for f in spec.unsupported:
                if getattr(s, f):
                    warnings.append(Issue(row=row, field=f, message=f"{spec.title} has no column for {f}; it is not written."))
            texts = {f: getattr(s, f) for f in TEXT_FIELDS} | {f"extra[{k!r}]": f"{k}{v}" for k, v in s.extra.items()}
            for f, text in texts.items():
                problem = control_char_problem(text)
                if problem:
                    errors.append(Issue(row=row, field=f, message=f"{f} {problem}; the import file would break."))
            if s.data_file:
                problem = filename_problem(s.data_file)
                if problem:
                    errors.append(Issue(row=row, field="data_file", message=f"data file {s.data_file!r} {problem}."))
                key = s.data_file.lower()  # Windows file names are case-insensitive
                if spec.data_file_suffix and key.endswith(spec.data_file_suffix):
                    key = key[: -len(spec.data_file_suffix)]  # 'Run1' and 'Run1.d' are the same file
                if spec.unique_data_files:
                    if key in seen_files:
                        errors.append(
                            Issue(row=row, field="data_file",
                                  message=f"duplicate data file {s.data_file!r} (also row {seen_files[key]}); "
                                  "the second injection would overwrite or fail.")
                        )
                    else:
                        seen_files[key] = row
                else:
                    pair = (key, s.sample_name.lower())
                    if pair in seen_pairs:
                        errors.append(
                            Issue(row=row, field="sample_name",
                                  message=f"sample name {s.sample_name!r} appears twice in data file "
                                  f"{s.data_file!r} (also row {seen_pairs[pair]}).")
                        )
                    else:
                        seen_pairs[pair] = row
                if s.data_path and len(s.data_path.rstrip("\\/")) + 1 + len(s.data_file) + 4 > 260:
                    warnings.append(Issue(row=row, field="data_file", message="full data path exceeds 260 characters."))
            if s.position:
                problem = check_position(s.position, d.position_pattern, d.plate_size, d.max_vial)
                if problem:
                    errors.append(Issue(row=row, field="position", message=problem + "."))
            volumes: list[tuple[str, float | None]] = [("injection_volume_ul", s.injection_volume_ul)]
            for header, raw in volume_overrides(s):  # written verbatim, so checked the same way
                try:
                    ov = parse_number(raw)
                except ValueError:
                    errors.append(
                        Issue(row=row, field=f"extra[{header!r}]",
                              message=f"injection volume {raw!r} is not a number, so the max_injection_volume_ul "
                              "limit cannot be checked.")
                    )
                    continue
                if not (spec is AGILENT and ov == -1):  # MassHunter '-1' = as method
                    volumes.append((f"extra[{header!r}]", ov))
            for f, v in volumes:
                if v is None:
                    continue
                if not math.isfinite(v) or v <= 0:
                    errors.append(Issue(row=row, field=f, message=f"injection volume {v:g} µL must be a number > 0."))
                elif v > max_injection_volume_ul:
                    errors.append(
                        Issue(row=row, field=f,
                              message=f"injection volume {v:g} µL exceeds the limit max_injection_volume_ul="
                              f"{max_injection_volume_ul:g} µL.")
                    )
            for f, exts in spec.method_extensions.items():
                value = getattr(s, f)
                ext = _extension(value)
                if ext and ext.lower() not in exts:
                    level = warnings if spec.name == "sciex_os" else errors  # SCIEX extensions are assumed
                    hint = " ('.dam' is an Analyst method; SCIEX OS uses .msm)" if ext.lower() == ".dam" else ""
                    level.append(
                        Issue(row=row, field=f, message=f"{f} {value!r} has extension {ext!r}; {spec.title} expects "
                              f"{' or '.join(exts)}{hint}.")
                    )
            if spec is AGILENT and s.ms_method and not _extension(s.ms_method):
                warnings.append(Issue(row=row, field="ms_method", message="MassHunter methods normally end in '.m'."))
            if spec.max_sample_name and len(s.sample_name) > spec.max_sample_name:
                errors.append(
                    Issue(row=row, field="sample_name", message=f"sample name is longer than {spec.max_sample_name} characters.")
                )
            if spec.encoding != "utf-8-sig":
                texts = [getattr(s, f) for f in TEXT_FIELDS] + list(s.extra.values())
                if any(any(ord(ch) > 127 for ch in t) for t in texts):
                    warnings.append(
                        Issue(row=row, field="text", message="non-ASCII characters may be garbled by the vendor "
                              "software; prefer plain ASCII.")
                    )
            if s.sample_type in ("solvent", "double_blank") and spec.type_out[s.sample_type] == "Blank":
                warnings.append(
                    Issue(row=row, field="sample_type",
                          message=f"{spec.title} has no '{s.sample_type}' type; written as 'Blank'.")
                )
        if not has_template and spec.name == "sciex_os":
            warnings.append(
                Issue(field="template", message="No SCIEX OS template header given: the default header is a best "
                      "guess. Export a blank batch from SCIEX OS and pass it as template_path to be sure.")
            )
        return ValidationReport(
            worklist=wl.name,
            format=spec.name,
            valid=not errors,
            sample_count=len(entries),
            errors=errors,
            warnings=warnings,
        )

    # ----------------------------------------------------------------- export

    @_locked
    def export(
        self,
        name: str,
        *,
        max_injection_volume_ul: float,
        fmt: str | None = None,
        filename: str | None = None,
        overwrite: bool = False,
        template_path: str | None = None,
        template_columns: list[str] | None = None,
        delimiter: str | None = None,
        bracket_type: int | None = None,
        utf8_bom: bool | None = None,
        write_provenance: bool = True,
        preview_lines: int = 12,
    ) -> dict[str, Any]:
        wl = self.get(name)
        spec = FORMATS[fmt or wl.format]
        notes: list[str] = []
        if template_path:
            template_columns, tdelim = self._template_header(spec, template_path)
            delimiter = delimiter or tdelim
        # A template stored on the draft (e.g. from import_worklist) is reused for the same format.
        if template_columns is None and spec.name == wl.format:
            template_columns = wl.template_columns
            delimiter = delimiter or wl.delimiter
        report = self.validate(
            name, max_injection_volume_ul=max_injection_volume_ul, fmt=spec.name,
            has_template=template_columns is not None,
        )  # fmt: skip
        if not report.valid:
            listed = "; ".join(
                f"row {i.row}: {i.message}" if i.row else i.message for i in report.errors[:10]
            )
            more = f" (+{len(report.errors) - 10} more)" if len(report.errors) > 10 else ""
            raise WorklistError(
                f"Not exported: {len(report.errors)} validation error(s): {listed}{more}. Run validate_worklist for details."
            )
        samples = self.samples(wl, spec)
        if spec.name != wl.format:
            overridden = sorted({k for s in self.samples(wl) for k in s.extra if _maps(spec, k)})
            if overridden:
                notes.append(
                    f"Values kept verbatim from the {wl.format} draft for {', '.join(overridden)} were not written: "
                    f"{spec.title} uses its own values for those columns."
                )
        cols, dropped = resolve_columns(spec, template_columns)
        for f in dropped:
            if any(getattr(s, f) not in ("", None) for s in samples):
                notes.append(f"The template has no column for {f}; those values were not written.")
        if spec.name == "sciex_os" and template_columns is None:
            notes.append("SCIEX OS default header used (best guess); pass template_path to use your system's header.")
        bt = bracket_type if bracket_type is not None else wl.xcalibur_bracket_type
        if bt not in (1, 2, 3, 4):
            raise WorklistError("bracket_type must be 1 (Overlapped), 2 (None), 3 (Non-Overlapped) or 4 (Open).")
        try:
            text = render(spec, samples, template_columns=template_columns, delimiter=delimiter, bracket_type=bt)
        except ValueError as exc:
            raise WorklistError(str(exc)) from exc
        data = encode(spec, text, utf8_bom)
        fname = filename or f"{wl.name}_{spec.name}{spec.extensions[0]}"
        ext = Path(fname).suffix.lower()
        if not ext:
            fname += spec.extensions[0]
        elif ext not in spec.extensions:
            raise WorklistError(f"{spec.title} files must end in {' or '.join(spec.extensions)} (got {ext!r}).")
        # '<file name>.provenance.json': unique per export file (T.csv and T.txt don't share one), and
        # checked before anything is written so neither file is replaced without overwrite=true.
        target = self.resolve(fname)
        ppath = target.with_name(target.name + ".provenance.json")
        if write_provenance and not overwrite and ppath.exists():
            raise WorklistError(
                f"{ppath.relative_to(self.root).as_posix()!r} already exists. Choose another file name, pass "
                "overwrite=true, or write_provenance=false. Nothing was written."
            )
        path = self.write_file(fname, data, overwrite=overwrite)
        rel = path.relative_to(self.root).as_posix()
        wl.record("export_worklist", format=spec.name, file=rel, bytes=len(data))
        provenance_rel = None
        if write_provenance:
            prov = {
                "generator": "labmcp-ms-worklist",
                "worklist": wl.name,
                "format": spec.name,
                "file": rel,
                "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "randomisation": (
                    {"seed": wl.plan.seed, "algorithm": "Python random.Random(seed).shuffle over the rows whose "
                     "type is in randomize_types, applied to the samples in the order they were added",
                     "randomize_types": wl.plan.randomize_types}
                    if wl.plan and wl.plan.randomize else None
                ),
                "history": wl.history,
                "base_samples": [e.sample.model_dump() for e in wl.base],
            }
            self.write_file(
                ppath.relative_to(self.root).as_posix(),
                json.dumps(prov, indent=2, default=str).encode("utf-8"),
                overwrite=overwrite,
            )
            provenance_rel = ppath.relative_to(self.root).as_posix()
        lines = text.splitlines()
        return {
            "path": str(path),
            "relative_path": rel,
            "format": spec.name,
            "bytes": len(data),
            "encoding": "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8",
            "line_ending": "CRLF",
            "sample_count": len(samples),
            "preview": lines[:preview_lines],
            "how_to_import": spec.how_to_import,
            "warnings": [f"row {i.row}: {i.message}" if i.row else i.message for i in report.warnings],
            "notes": notes,
            "provenance_file": provenance_rel,
            "acquisition_started": False,
        }

    def _template_header(self, spec: FormatSpec, relpath: str) -> tuple[list[str], str]:
        text = self._decode(relpath).lstrip("﻿")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines and lines[0].lower().startswith("bracket type="):
            lines = lines[1:]
        if not lines:
            raise WorklistError(f"Template {relpath!r} has no header row.")
        header = lines[0]
        delim = max(spec.delimiters, key=header.count)
        if not header.count(delim):
            delim = spec.delimiters[0]
        cols = next(csv.reader([header], delimiter=delim))
        return [c.strip() for c in cols], delim

    # ----------------------------------------------------------------- import

    @_locked
    def import_file(self, relpath: str, *, fmt: str | None = None, name: str | None = None,
                    replace: bool = False) -> tuple[Worklist, list[str]]:
        text = self._decode(relpath)
        try:
            parsed = parse(text, fmt)
        except (ValueError, KeyError) as exc:
            raise WorklistError(f"Could not parse {relpath!r}: {exc}") from exc
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(relpath).stem)[:64] or "imported"
        wl_name = name or stem
        if not NAME_RE.fullmatch(wl_name):
            raise WorklistError(
                "Worklist names must be 1-64 characters of letters, digits, '_', '-' or '.', starting with a "
                "letter or digit (they are used in file names)."
            )
        if wl_name in self.drafts and not replace:
            raise WorklistError(f"A worklist named {wl_name!r} already exists; pass another name or replace=true.")
        spec = FORMATS[parsed.format]
        default_headers = [h for h, _ in spec.columns]
        wl = Worklist(
            name=wl_name,
            format=parsed.format,
            defaults=Defaults(data_file_pattern="{worklist}_{index:03d}"),
            base=[Entry(sample=s) for s in parsed.samples],
            source_file=relpath,
            xcalibur_bracket_type=parsed.bracket_type or 4,
            template_columns=None if parsed.columns == default_headers else parsed.columns,
            delimiter=parsed.delimiter,
        )
        wl.record("import_worklist", file=relpath, format=parsed.format, sample_count=len(parsed.samples))
        self.drafts[wl_name] = wl
        return wl, parsed.warnings


def _maps(spec: FormatSpec, header: str) -> bool:
    """True if ``spec`` reads ``header`` as one of its mapped (canonical) columns."""
    return spec.aliases.get(normalise_header(header)) is not None


def _extension(value: str) -> str:
    base = re.split(r"[\\/]", value.rstrip("\\/"))[-1]
    return os.path.splitext(base)[1]


class _PatternValues(dict):
    """Values for data_file_pattern placeholders."""

    def __init__(self, index: int, sample_name: str, sample_id: str, sample_type: str, worklist: str,
                 position: str, date: str | None = None) -> None:
        super().__init__(
            index=index,
            sample_name=sample_name,
            sample_id=sample_id,
            type=sample_type,
            worklist=worklist,
            position=position,
            date=date or _now_date(),
        )

    def __missing__(self, key: str) -> str:
        raise KeyError(f"unknown placeholder {{{key}}}")
