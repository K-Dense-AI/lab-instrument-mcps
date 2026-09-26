"""MCP server that builds, validates and exports LC-MS worklist / sequence / batch import files.

It never starts an acquisition: it writes files into one output folder, and a person imports them
into MassLynx, SCIEX OS, MassHunter or Xcalibur. Formats and sources: see ``formats.py``.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from labmcp import CONTROL, READ, ConnectContext, InstrumentServer, Limit
from pydantic import Field

from labmcp_ms_worklist.driver import ControlPlan, Defaults, ValidationReport, Worklist, WorklistStore
from labmcp_ms_worklist.formats import FORMATS, NOT_IMPLEMENTED, POSITION_PATTERNS, FormatName, Sample
from labmcp_ms_worklist.simulator import simulated_store

DEFAULT_DIR = "./worklists"

PositionPattern = Literal["any", "vial_number", "well", "agilent_plate_well", "waters_plate_well", "tray_well"]


def connect(ctx: ConnectContext) -> WorklistStore:
    if ctx.simulate:
        return simulated_store()
    address = ctx.address or DEFAULT_DIR
    if address.startswith("file://"):
        address = address[len("file://") :]
    return WorklistStore(os.path.expanduser(address))


server = InstrumentServer(
    "LC-MS Worklist Builder (MassLynx, SCIEX OS, MassHunter, Xcalibur)",
    connect=connect,
    package="labmcp-ms-worklist",
    instructions="""
Builds sample-queue import files for LC-MS acquisition software. It does NOT control an instrument
and never starts a run: it writes files into one output folder, and a person imports them.
- Workflow: create_worklist -> (add_samples) -> insert_qc_blanks -> validate_worklist -> export_worklist.
- Call list_formats to see what each vendor format needs and which parts are verified vs assumed;
  tell the user about the assumptions for the format they chose (especially SCIEX OS headers).
- For SCIEX OS (and to be certain for any vendor) ask the user for a blank batch/sequence exported
  from their own software, place it in the output folder and pass it as template_path.
- Method names must match methods that exist on the acquisition PC; the server cannot check that.
- Randomised run orders record their seed; report it to the user.
- import_worklist reads an existing file from the output folder so it can be edited or converted
  to another vendor's format (export with a different `format`).
- Files are only read from / written to the output folder; existing files are never overwritten
  unless overwrite=true.
""",
    limits=[
        Limit("max_injection_volume_ul", 100, "µL", "Largest injection volume allowed in a worklist"),
    ],
    address_help=f"""\
  --address <folder>         output folder for worklist files (default {DEFAULT_DIR}; created if missing)
  --simulate                 use a temporary folder that is deleted when the server stops""",
)
mcp = server.mcp


def _store() -> WorklistStore:
    return server.driver


def _max_vol() -> float:
    return server.limits["max_injection_volume_ul"]


def _check_volumes(samples: list[Sample]) -> None:
    for s in samples:
        if s.injection_volume_ul is not None:
            server.check("max_injection_volume_ul", s.injection_volume_ul, f"injection volume for {s.sample_name!r}")


def _to_samples(items: list[Sample | str]) -> list[Sample]:
    return [Sample(sample_name=i) if isinstance(i, str) else i for i in items]


def _view(wl: Worklist, max_rows: int = 200) -> dict[str, Any]:
    store = _store()
    samples = store.samples(wl)
    return {
        "name": wl.name,
        "format": wl.format,
        "sample_count": len(samples),
        "samples": [{"row": i, **s.model_dump()} for i, s in enumerate(samples[:max_rows], start=1)],
        "truncated": len(samples) > max_rows,
        "defaults": wl.defaults.model_dump(),
        "plan": wl.plan.model_dump() if wl.plan else None,
        "randomisation_seed": wl.plan.seed if wl.plan and wl.plan.randomize else None,
        "source_file": wl.source_file,
        "template_columns": wl.template_columns,
        "history": wl.history,
        "output_dir": str(store.root),
    }


SamplesArg = Annotated[
    list[Sample | str],
    Field(
        min_length=1,
        max_length=2000,
        description="Samples in run order: objects (sample_name required) or plain sample names",
    ),
]
VolumeArg = Annotated[
    float | None, Field(gt=0, le=1000, description="Injection volume in µL for samples that don't set one")
]


@mcp.tool(**READ)
def list_formats() -> dict[str, Any]:
    """List the supported import formats (Waters MassLynx, SCIEX OS, Agilent MassHunter, Thermo
    Xcalibur): columns, required fields, sample-type names, file extensions, how to import, what
    is verified against vendor documents and what is assumed, and the source URLs. Also lists the
    position patterns and the formats that were researched but not implemented."""
    formats = []
    for spec in FORMATS.values():
        formats.append(
            {
                "format": spec.name,
                "title": spec.title,
                "vendor": spec.vendor,
                "software": spec.software,
                "how_to_import": spec.how_to_import,
                "file_extensions": list(spec.extensions),
                "delimiters": list(spec.delimiters),
                "default_columns": [h for h, _ in spec.columns],
                "column_mapping": {h: f for h, f in spec.columns if f},
                "required_fields": list(spec.required),
                "fields_without_column": list(spec.unsupported),
                "sample_types": spec.type_out,
                "method_extensions": {k: list(v) for k, v in spec.method_extensions.items()},
                "encoding": spec.encoding,
                "line_ending": "CRLF",
                "verified": list(spec.verified),
                "assumed": list(spec.assumed),
                "sources": list(spec.sources),
                "notes": list(spec.notes),
            }
        )
    return {
        "formats": formats,
        "position_patterns": {k: v[1] for k, v in POSITION_PATTERNS.items()},
        "not_implemented": NOT_IMPLEMENTED,
        "data_file_pattern_placeholders": ["{index}", "{sample_name}", "{sample_id}", "{type}", "{worklist}",
                                           "{date}", "{position}"],
    }


@mcp.tool(**CONTROL)
def create_worklist(
    name: Annotated[str, Field(description="Draft name (letters, digits, _ - .); used in file names")],
    format: Annotated[FormatName, Field(description="Target vendor format (see list_formats)")],
    samples: SamplesArg,
    ms_method: Annotated[str, Field(description="Default MS/acquisition/instrument method")] = "",
    lc_method: Annotated[str, Field(description="Default LC/inlet method (Waters INLET_FILE, SCIEX LC Method)")] = "",
    tune_file: Annotated[str, Field(description="Default tune file (Waters MS_TUNE_FILE)")] = "",
    processing_method: Annotated[str, Field(description="Default processing method (SCIEX OS, Xcalibur)")] = "",
    data_path: Annotated[str, Field(description="Data folder (Xcalibur Path column)")] = "",
    injection_volume_ul: VolumeArg = None,
    data_file_pattern: Annotated[
        str, Field(description="Data file naming pattern; placeholders {index} {sample_name} {sample_id} {type} "
                   "{worklist} {date} {position}; illegal characters become '_'")
    ] = "{worklist}_{index:03d}_{sample_name}",
    position_pattern: Annotated[PositionPattern, Field(description="Autosampler position format to enforce")] = "any",
    plate_size: Annotated[Literal[24, 48, 54, 96, 384], Field(description="Positions per plate/tray")] = 96,
    max_vial: Annotated[int, Field(ge=1, le=10000, description="Highest vial number (vial_number pattern)")] = 120,
    first_position: Annotated[
        str, Field(description="If set, samples without a position get sequential positions from here, e.g. 'A1'")
    ] = "",
    vendor_columns: Annotated[
        dict[str, str] | None,
        Field(description="Constant vendor columns for every row, e.g. {'Rack Type': '...', 'Plate Type': '...'}"),
    ] = None,
    bracket_type: Annotated[
        Literal[1, 2, 3, 4], Field(description="Xcalibur only: 1 Overlapped, 2 None, 3 Non-Overlapped, 4 Open")
    ] = 4,
    replace: Annotated[bool, Field(description="Replace an existing draft with the same name")] = False,
) -> dict[str, Any]:
    """Create an in-memory draft worklist from a sample list plus defaults (methods, injection
    volume, tray positions, data-file naming pattern). Nothing is written to disk until
    export_worklist. Injection volumes above the max_injection_volume_ul limit are refused."""
    items = _to_samples(samples)
    _check_volumes(items)
    if injection_volume_ul is not None:
        server.check("max_injection_volume_ul", injection_volume_ul, "default injection volume")
    defaults = Defaults(
        ms_method=ms_method,
        lc_method=lc_method,
        tune_file=tune_file,
        processing_method=processing_method,
        data_path=data_path,
        injection_volume_ul=injection_volume_ul,
        data_file_pattern=data_file_pattern,
        position_pattern=position_pattern,
        plate_size=plate_size,
        max_vial=max_vial,
        first_position=first_position,
        vendor_columns=vendor_columns or {},
    )
    wl = _store().create(name, format, items, defaults, replace=replace)
    wl.xcalibur_bracket_type = bracket_type
    return _view(wl)


@mcp.tool(**CONTROL)
def add_samples(
    name: Annotated[str, Field(description="Draft worklist name")],
    samples: SamplesArg,
) -> dict[str, Any]:
    """Append samples to a draft worklist (the worklist's defaults and automatic positions apply;
    any blank/QC/randomisation plan is re-applied to the longer list with the same seed)."""
    items = _to_samples(samples)
    _check_volumes(items)
    return _view(_store().add(name, items))


@mcp.tool(**CONTROL)
def insert_qc_blanks(
    name: Annotated[str, Field(description="Draft worklist name")],
    blank_every_n: Annotated[int | None, Field(ge=1, le=1000, description="Insert a blank after every N samples")] = None,
    blank_at_start: bool = False,
    blank_at_end: bool = False,
    blank_position: Annotated[str, Field(description="Tray position of the blank vial")] = "",
    blank_name: str = "Blank",
    blank_ms_method: Annotated[str, Field(description="Method for blanks (default: the worklist's ms_method)")] = "",
    blank_injection_volume_ul: VolumeArg = None,
    qc_at_start: Annotated[int, Field(ge=0, le=50, description="Number of QC injections at the start")] = 0,
    qc_at_end: Annotated[int, Field(ge=0, le=50, description="Number of QC injections at the end")] = 0,
    qc_every_n: Annotated[int | None, Field(ge=1, le=1000, description="Insert a QC after every N samples")] = None,
    qc_position: Annotated[str, Field(description="Tray position of the pooled QC vial")] = "",
    qc_name: str = "QC",
    qc_injection_volume_ul: VolumeArg = None,
    randomize: Annotated[bool, Field(description="Randomise the run order of the samples first")] = False,
    seed: Annotated[
        int | None, Field(ge=0, le=2**31 - 1, description="Random seed (a new one is drawn and recorded if omitted)")
    ] = None,
    randomize_types: Annotated[
        list[Literal["sample", "blank", "qc", "standard", "solvent", "double_blank"]],
        Field(description="Which sample types are shuffled among their own slots (standards stay put by default)"),
    ] = ["sample"],  # noqa: B006 - pydantic copies defaults
) -> dict[str, Any]:
    """Insert blanks and QC injections and optionally randomise the run order (with a recorded seed).
    Replaces any previous plan, so calling it again does not duplicate blanks. The plan is
    re-applied whenever samples are added; data-file names follow the new run order."""
    for v, what in ((blank_injection_volume_ul, "blank injection volume"), (qc_injection_volume_ul, "QC injection volume")):
        if v is not None:
            server.check("max_injection_volume_ul", v, what)
    plan = ControlPlan(
        blank_every_n=blank_every_n,
        blank_at_start=blank_at_start,
        blank_at_end=blank_at_end,
        blank_name=blank_name,
        blank_position=blank_position,
        blank_ms_method=blank_ms_method,
        blank_injection_volume_ul=blank_injection_volume_ul,
        qc_every_n=qc_every_n,
        qc_at_start=qc_at_start,
        qc_at_end=qc_at_end,
        qc_name=qc_name,
        qc_position=qc_position,
        qc_injection_volume_ul=qc_injection_volume_ul,
        randomize=randomize,
        seed=seed,
        randomize_types=list(randomize_types),
    )
    return _view(_store().set_plan(name, plan))


@mcp.tool(**READ)
def get_worklist(
    name: Annotated[str, Field(description="Draft worklist name")],
    max_rows: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    """Show a draft worklist in run order, with its defaults, blank/QC plan, randomisation seed and
    the history of operations applied to it."""
    return _view(_store().get(name), max_rows)


@mcp.tool(**READ)
def list_worklists() -> dict[str, Any]:
    """List the draft worklists in memory and the files in the output folder."""
    store = _store()
    return {
        "output_dir": str(store.root),
        "drafts": [
            {"name": wl.name, "format": wl.format, "sample_count": len(store.samples(wl))}
            for wl in store.drafts.values()
        ],
        "files": store.list_files(),
    }


@mcp.tool(**READ)
def validate_worklist(
    name: Annotated[str, Field(description="Draft worklist name")],
    format: Annotated[FormatName | None, Field(description="Validate for another vendor format (default: the draft's)")] = None,
) -> ValidationReport:
    """Check a draft against the target format: required fields, duplicate data-file names,
    characters Windows does not allow in file names, tray/vial position format and plate bounds,
    injection volume (> 0 and <= the max_injection_volume_ul limit), and method file extensions.
    Errors block export; warnings don't."""
    return _store().validate(name, max_injection_volume_ul=_max_vol(), fmt=format)


@mcp.tool(**CONTROL)
def export_worklist(
    name: Annotated[str, Field(description="Draft worklist name")],
    format: Annotated[FormatName | None, Field(description="Vendor format (default: the draft's); use another to convert")] = None,
    filename: Annotated[
        str | None, Field(description="File name inside the output folder (default '<name>_<format>.csv')")
    ] = None,
    overwrite: Annotated[bool, Field(description="Replace an existing file of the same name")] = False,
    template_path: Annotated[
        str | None,
        Field(description="A blank batch/sequence/worklist exported from the vendor software, in the output folder; "
              "its header (and delimiter) is used"),
    ] = None,
    template_columns: Annotated[list[str] | None, Field(description="Header columns to use instead of a template file")] = None,
    delimiter: Annotated[
        Literal[",", ";", "\t"] | None, Field(description="Field separator (Xcalibur must match the Windows list separator)")
    ] = None,
    bracket_type: Annotated[Literal[1, 2, 3, 4] | None, Field(description="Xcalibur bracket type override")] = None,
    utf8_bom: Annotated[bool | None, Field(description="Force a UTF-8 BOM on/off (default per format)")] = None,
    write_provenance: Annotated[
        bool, Field(description="Also write <file>.provenance.json (history, seed, original sample order)")
    ] = True,
) -> dict[str, Any]:
    """Validate the draft and write the vendor import file into the output folder. Returns the path,
    a preview of the first lines, warnings and import instructions. Refuses if validation finds
    errors or the file exists (unless overwrite=true). This does not start an acquisition: a person
    imports the file into the acquisition software."""
    store = _store()
    _check_volumes(store.samples(store.get(name)))
    return store.export(
        name,
        max_injection_volume_ul=_max_vol(),
        fmt=format,
        filename=filename,
        overwrite=overwrite,
        template_path=template_path,
        template_columns=template_columns,
        delimiter=delimiter,
        bracket_type=bracket_type,
        utf8_bom=utf8_bom,
        write_provenance=write_provenance,
    )


@mcp.tool(**READ)
def import_worklist(
    path: Annotated[str, Field(description="File in the output folder (relative path)")],
    format: Annotated[FormatName | None, Field(description="Format; detected from the header if omitted")] = None,
    name: Annotated[str | None, Field(description="Draft name (default: the file name)")] = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Parse an existing MassLynx, SCIEX OS, MassHunter or Xcalibur import file from the output
    folder into a draft, so it can be validated, edited or exported in another vendor's format.
    Columns without a neutral equivalent are kept verbatim. Only reads; nothing is written."""
    wl, warnings = _store().import_file(path, fmt=format, name=name, replace=replace)
    view = _view(wl)
    view["import_warnings"] = warnings
    return view


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

