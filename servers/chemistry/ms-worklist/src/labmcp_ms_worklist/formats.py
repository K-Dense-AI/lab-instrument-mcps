"""Documented sample-queue import formats for LC-MS acquisition software.

Every exporter here writes a file that a person imports into the vendor software (or that
the software picks up itself where that is documented). Nothing in this module talks to an
instrument. What each format is based on, and what is assumed, is recorded in
:data:`FORMATS` and returned by the ``list_formats`` tool.

Sources
-------
Waters MassLynx (comma-delimited worksheet import):
  * Waters KB WKB63781, "How do you create a comma-delimited text file that can be imported
    into MassLynx as a sample list?" (row 1 = FIELD IDs, case sensitive, any column order,
    plus an ``Index`` column; File > Import Worksheet, "Comma Delimited (*.CSV, *.TXT)"), and the
    two example files attached to it (ANALYSIS_qmeth1.csv, ANALYSIS_targetlynx.csv: CRLF line
    endings, unquoted, TYPE values Blank/Standard/QC/Analyte, method names without extension).
    https://support.waters.com/KB_Inf/MassLynx/WKB63781_How_do_you_create_a_comma_delimited_text_file_that_can_be_imported_into_MassLynx_as_a_sample_list
  * MassLynx 4.2 Getting Started Guide, 715009602 Ver. 00, Table 5-1 "Required sample list
    columns": FILE_NAME, INLET_FILE, MS_FILE, SAMPLE_LOCATION (the "Bottle" column), INJ_VOL.
    https://help.waters.com/content/dam/waters/en/support/usermanuals/2025/715009602/715009602v00.pdf

SCIEX OS (batch import from .csv/.txt):
  * SCIEX KB "Importing a .txt or .csv file into a batch in SCIEX OS software": export a blank
    batch as the template, fill it in, then Open > Import from file.
    https://sciex.com/support/knowledge-base-articles/how-to-import-txt-file-to-make-sciex-os-batch_en_us
  * SCIEX OS Feature Guide for the Echo MS+ system, RUO-IDV-05-15796-C, Table 4-1 (column names
    Sample Name (< 252 characters), MS Method, Sample Type (Blank, Standard, Double blank,
    QualityControl, Solvent, Unknown), Data File, Processing Method).
    https://sciex.com/content/dam/SCIEX/pdf/customer-docs/user-guide/sciex-os-echo-msplus-7600-feature-guide-en.pdf
  * SCIEX KB "Unable to add plates or samples to a batch" (Rack Type, Plate Type, Rack Position,
    Plate Position, Vial Position columns).
  The exact header strings of an exported batch are not published, so the default header here is
  an assumption; pass the header of a blank batch exported from your own SCIEX OS as a template.

Agilent MassHunter Acquisition (worklist import from CSV):
  * Agilent Community answer quoting the example file shipped in D:\\MassHunter\\Worklist_Import:
    ``Sample Name,Barcode,Rack Code,Sample Position,Method,Data File,Sample Type,Level Name,
    Inj Vol (µL),Comment`` with positions such as ``P1-A1`` and sample types Calibration/Sample;
    headers that match the worklist column names import without a map file.
    https://community.agilent.com/technical/mass-spectrometry-software/f/mass-spectrometry-software-user-forum/12581/how-to-format-a-csv-file-for-import-as-a-new-worklist-for-masshunter
  * Agilent Community (Map File Generator, MassHunter 12.1): save as UTF-8 when the file contains µ.
  * Agilent Known Problem Report (LC/MS Acquisition): use ``-1`` in Inj Vol for "As method".
    https://www.agilent.com/cs/library/support/Patches/SSBs/MHAcqLCTQ_Classic.html
  * MassHunter Study Manager Quick Start G3335-90140: Sample Position, Method and Data File must
    be filled in for each sample.
  The Walkup Custom Sample Import Programmer Guide (G2725-90026) could not be retrieved (HTTP 403),
  so the Walkup import format is NOT implemented.

Thermo Xcalibur (sequence import from CSV):
  * Xcalibur 2.2 Data Acquisition and Processing User Guide, XCALI-97209 Rev. D, Appendix A: only
    .csv files; the first cell of the first row must be ``Bracket Type=n`` (1 Overlapped, 2 None,
    3 Non-Overlapped, 4 Open); the separator must equal the Windows list separator; sample types
    include Unknown, Blank, QC, Std Bracket, Std Clear, Std Update.
    https://tools.thermofisher.com/content/sfs/manuals/Man-XCALI-97209-Xcalibur-22-Acquisition-ManXCALI97209-D-EN.pdf
  * The exact header row (Sample Type, File Name, Sample ID, Path, Instrument Method, Process
    Method, Calibration File, Position, Inj Vol, Level, Sample Wt, Sample Vol, ISTD Amt,
    Dil Factor, L1 Study ... L5 Phone, Comment, Sample Name) is not printed in the manual; it is
    corroborated by independent open-source generators that target Xcalibur import (protti
    ``create_queue``, fgcz/qg, mapp-metabolomics-unit/lcms-sequencer) and by Rapid-QC-MS issue #83
    (native exports start with a bare ``Bracket Type=4`` line).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

SampleType = Literal["sample", "blank", "qc", "standard", "solvent", "double_blank"]
FormatName = Literal["waters_masslynx", "sciex_os", "agilent_masshunter", "thermo_xcalibur"]
SAMPLE_TYPES: tuple[str, ...] = ("sample", "blank", "qc", "standard", "solvent", "double_blank")

#: Canonical fields a column can map to (besides ``index`` and ``sample_type``).
TEXT_FIELDS = (
    "sample_name",
    "data_file",
    "sample_id",
    "position",
    "ms_method",
    "lc_method",
    "tune_file",
    "processing_method",
    "level",
    "comment",
    "data_path",
)


class Sample(BaseModel):
    """One injection in a worklist, in vendor-neutral terms."""

    sample_name: str = Field(description="Sample name shown in the queue")
    data_file: str = Field("", description="Data file name, without the vendor's extension (.raw/.d/.wiff)")
    sample_id: str = Field("", description="Sample ID / LIMS ID")
    sample_type: SampleType = Field("sample", description="sample, blank, qc, standard, solvent, double_blank")
    position: str = Field("", description="Autosampler tray/vial/well position, e.g. '12', 'A1', 'P1-A1', '1:A,1'")
    injection_volume_ul: float | None = Field(None, description="Injection volume in µL; None = as in the method")
    ms_method: str = Field("", description="MS / acquisition / instrument method")
    lc_method: str = Field("", description="LC / inlet method (Waters INLET_FILE, SCIEX LC Method)")
    tune_file: str = Field("", description="Tune file (Waters MS_TUNE_FILE)")
    processing_method: str = Field("", description="Processing method (SCIEX OS, Xcalibur)")
    level: str = Field("", description="Calibration level name")
    comment: str = Field("", description="Free-text comment / sample description")
    data_path: str = Field("", description="Data folder (Xcalibur Path column)")
    extra: dict[str, str] = Field(
        default_factory=dict,
        description="Vendor columns written verbatim, keyed by the exact column header (overrides mapped values)",
    )

    def content(self) -> dict[str, object]:
        """The fields that end up in a file (used for round-trip comparisons)."""
        return self.model_dump()


@dataclass(frozen=True)
class FormatSpec:
    name: str
    title: str
    vendor: str
    software: str
    how_to_import: str
    extensions: tuple[str, ...]
    delimiters: tuple[str, ...]
    #: Default header, in order. Each entry is (header, canonical field or None for a constant).
    columns: tuple[tuple[str, str | None], ...]
    #: Constant values written for columns with no canonical field.
    constants: dict[str, str]
    #: Normalised header -> canonical field (for reading files and templates).
    aliases: dict[str, str]
    #: Canonical sample type -> vendor text.
    type_out: dict[str, str]
    #: Fields that must be non-empty for every row (validation error otherwise).
    required: tuple[str, ...]
    #: Fields that should normally be filled in (validation warning otherwise).
    recommended: tuple[str, ...]
    #: Field -> allowed method-file extensions (only checked when the value has an extension).
    method_extensions: dict[str, tuple[str, ...]]
    #: Fields this vendor has no column for (warn if set).
    unsupported: tuple[str, ...]
    data_file_suffix: str
    encoding: str
    newline: str = "\r\n"
    #: False when several samples may share one data file (SCIEX .wiff/.wiff2).
    unique_data_files: bool = True
    max_sample_name: int | None = None
    verified: tuple[str, ...] = ()
    assumed: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    #: Extra vendor type texts accepted on import (lower case) -> canonical type.
    type_aliases: dict[str, str] = field(default_factory=dict)

    @property
    def type_in(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in self.type_out.items():
            out.setdefault(v.lower(), k)  # first canonical type wins (e.g. 'Blank' -> blank, not solvent)
        return out


def normalise_header(header: str) -> str:
    """'Inj Vol (µL)' -> 'injvol'; 'FILE_NAME' -> 'filename'."""
    h = re.sub(r"\(.*?\)", "", header.replace("\ufeff", ""))
    return re.sub(r"[^0-9a-z]", "", h.lower())


def _aliases(mapping: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {normalise_header(h): f for f, headers in mapping.items() for h in headers}


WATERS = FormatSpec(
    name="waters_masslynx",
    title="Waters MassLynx sample list (comma-delimited worksheet)",
    vendor="Waters",
    software="MassLynx 4.x",
    how_to_import="MassLynx sample list: File > Import Worksheet, File of Type = 'Comma Delimited (*.CSV, *.TXT)'.",
    extensions=(".csv", ".txt"),
    delimiters=(",",),
    columns=(
        ("FILE_NAME", "data_file"),
        ("FILE_TEXT", "sample_name"),
        ("MS_FILE", "ms_method"),
        ("MS_TUNE_FILE", "tune_file"),
        ("INLET_FILE", "lc_method"),
        ("SAMPLE_LOCATION", "position"),
        ("TYPE", "sample_type"),
        ("ID", "sample_id"),
        ("INJ_VOL", "injection_volume_ul"),
        ("Index", "index"),
    ),
    constants={},
    aliases=_aliases(
        {
            "data_file": ("FILE_NAME",),
            "sample_name": ("FILE_TEXT",),
            "ms_method": ("MS_FILE",),
            "tune_file": ("MS_TUNE_FILE",),
            "lc_method": ("INLET_FILE",),
            "position": ("SAMPLE_LOCATION",),
            "sample_type": ("TYPE",),
            "sample_id": ("ID",),
            "injection_volume_ul": ("INJ_VOL",),
            "index": ("Index",),
        }
    ),
    type_out={
        "sample": "Analyte",
        "blank": "Blank",
        "qc": "QC",
        "standard": "Standard",
        "solvent": "Blank",
        "double_blank": "Blank",
    },
    required=("data_file", "ms_method", "lc_method", "position", "injection_volume_ul"),
    recommended=(),
    method_extensions={"ms_method": (".exp",), "tune_file": (".ipr",)},
    unsupported=("processing_method", "data_path", "level", "comment"),
    data_file_suffix="",
    encoding="utf-8",
    verified=(
        "Row 1 holds MassLynx FIELD IDs (case and syntax sensitive); columns may be in any order; an extra "
        "'Index' column is added (WKB63781).",
        "Required columns FILE_NAME, INLET_FILE, MS_FILE, SAMPLE_LOCATION (the 'Bottle' column), INJ_VOL "
        "(MassLynx 4.2 Getting Started Guide 715009602, Table 5-1).",
        "Import with File > Import Worksheet, type 'Comma Delimited (*.CSV, *.TXT)' (WKB63781).",
        "CRLF line endings, unquoted fields, TYPE values Blank/Standard/QC/Analyte, MS_FILE/INLET_FILE given "
        "as method names without extension (example files attached to WKB63781).",
        "FILE_TEXT carries the sample text (used here for the sample name) and ID the sample ID (WKB18596).",
    ),
    assumed=(
        "Sample types 'solvent' and 'double_blank' are written as TYPE 'Blank' (MassLynx has no such type).",
        "Method extension checks: MS_FILE '.exp' (MassLynx 4.0 User's Guide), MS_TUNE_FILE '.ipr'; INLET_FILE "
        "extensions depend on the LC and are not checked.",
        "Files are written as UTF-8 without BOM; keep text ASCII (non-ASCII is flagged).",
        "The 'waters_plate_well' position pattern ('1:A,1') is a common MassLynx convention; positions are "
        "written exactly as given.",
    ),
    sources=(
        "https://support.waters.com/KB_Inf/MassLynx/WKB63781_How_do_you_create_a_comma_delimited_text_file_that_can_be_imported_into_MassLynx_as_a_sample_list",
        "https://help.waters.com/content/dam/waters/en/support/usermanuals/2025/715009602/715009602v00.pdf",
        "https://support.waters.com/KB_Inf/MassLynx/WKB18596_Text_fields_in_the_MassLynx_sample_list_that_can_be_used_to_carry_text_information_into_TargetLynx",
    ),
    notes=("Any other MassLynx FIELD ID (e.g. CONC_A, SAMPLE_GROUP, QUAN_REF) can be added per sample via 'extra'.",),
)

SCIEX = FormatSpec(
    name="sciex_os",
    title="SCIEX OS batch (import from .csv/.txt)",
    vendor="SCIEX",
    software="SCIEX OS",
    how_to_import="Batch workspace: Open > Import from file, select the file, choose append or replace, then Save As.",
    extensions=(".csv", ".txt"),
    delimiters=(",", "\t"),
    columns=(
        ("Sample Name", "sample_name"),
        ("Sample ID", "sample_id"),
        ("Sample Type", "sample_type"),
        ("MS Method", "ms_method"),
        ("LC Method", "lc_method"),
        ("Rack Type", None),
        ("Rack Position", None),
        ("Plate Type", None),
        ("Plate Position", None),
        ("Vial Position", "position"),
        ("Injection Volume", "injection_volume_ul"),
        ("Data File", "data_file"),
        ("Processing Method", "processing_method"),
        ("Comment", "comment"),
    ),
    constants={},
    aliases=_aliases(
        {
            "sample_name": ("Sample Name",),
            "sample_id": ("Sample ID",),
            "sample_type": ("Sample Type",),
            "ms_method": ("MS Method", "Acquisition Method"),
            "lc_method": ("LC Method",),
            "position": ("Vial Position", "Well Position"),
            "injection_volume_ul": ("Injection Volume", "Injection Volume (µL)", "Inj. Volume (µL)"),
            "data_file": ("Data File",),
            "processing_method": ("Processing Method",),
            "comment": ("Comment",),
        }
    ),
    type_out={
        "sample": "Unknown",
        "blank": "Blank",
        "qc": "QualityControl",
        "standard": "Standard",
        "solvent": "Solvent",
        "double_blank": "Double blank",
    },
    required=("sample_name", "data_file", "ms_method"),
    recommended=("position",),
    method_extensions={"ms_method": (".msm",), "lc_method": (".lcm",), "processing_method": (".qmethod",)},
    unsupported=("tune_file", "data_path", "level"),
    data_file_suffix="",
    encoding="utf-8",
    unique_data_files=False,
    max_sample_name=251,
    verified=(
        "Batches import from .txt or .csv via Open > Import from file; the column layout must come from a batch "
        "exported from SCIEX OS (Save > Export) (SCIEX KB).",
        "Column names Sample Name (< 252 characters), MS Method, Sample Type, Data File, Processing Method "
        "(SCIEX OS Feature Guide RUO-IDV-05-15796-C, Table 4-1); Rack Type, Plate Type, Rack Position, Plate "
        "Position, Vial Position (SCIEX KB); Injection Volume column (SCIEX OS 3.0 release notes).",
        "Sample Type values Blank, Standard, Double blank, QualityControl, Solvent, Unknown (RUO-IDV-05-15796-C).",
        "Several samples may share one data file (Analyst/SCIEX OS reference: data file names are not unique).",
    ),
    assumed=(
        "The exact header text of an exported batch (e.g. 'Injection Volume' vs 'Injection Volume (µL)', the "
        "'LC Method' column) is not published: the default header is a best guess. Pass template_path or "
        "template_columns from a blank batch exported on your own system; export reports when no template was used.",
        "Comma delimiter and CRLF line endings for the default header; a template's delimiter (comma or tab) is kept.",
        "Method extensions .msm (MS), .lcm (LC), .qmethod (processing) are flagged only as warnings.",
        "Rack Type / Rack Position / Plate Type / Plate Position are set with 'vendor_columns' (constant per "
        "worklist) or per sample via 'extra'; values depend on the configured LC.",
    ),
    sources=(
        "https://sciex.com/support/knowledge-base-articles/how-to-import-txt-file-to-make-sciex-os-batch_en_us",
        "https://sciex.com/support/knowledge-base-articles/how-can-i-get-a-batch-import-template-for-sciex-os-to-set-up-my-manual-lims-connection_en_us",
        "https://sciex.com/content/dam/SCIEX/pdf/customer-docs/user-guide/sciex-os-echo-msplus-7600-feature-guide-en.pdf",
        "https://sciex.com/resource-hub/knowledge-base-articles/lcms/troubleshooting/unable-to-add-plates-or-samples-to-a-batch-en-us",
    ),
)

AGILENT = FormatSpec(
    name="agilent_masshunter",
    title="Agilent MassHunter Acquisition worklist (CSV import)",
    vendor="Agilent",
    software="MassHunter Acquisition (LC/TQ, LC/Q-TOF) 10.x-12.x",
    how_to_import="Worklist: right-click a row > 'Add/Append Samples from... (csv, xlsx)' (older: 'Import Worklist...'); "
    "no map file is needed because the headers match the worklist column names.",
    extensions=(".csv",),
    delimiters=(",",),
    columns=(
        ("Sample Name", "sample_name"),
        ("Barcode", None),
        ("Rack Code", None),
        ("Sample Position", "position"),
        ("Method", "ms_method"),
        ("Data File", "data_file"),
        ("Sample Type", "sample_type"),
        ("Level Name", "level"),
        ("Inj Vol (µL)", "injection_volume_ul"),
        ("Comment", "comment"),
    ),
    constants={},
    aliases=_aliases(
        {
            "sample_name": ("Sample Name",),
            "position": ("Sample Position",),
            "ms_method": ("Method",),
            "data_file": ("Data File",),
            "sample_type": ("Sample Type",),
            "level": ("Level Name",),
            "injection_volume_ul": ("Inj Vol (µL)", "Inj Vol"),
            "comment": ("Comment",),
        }
    ),
    type_out={
        "sample": "Sample",
        "blank": "Blank",
        "qc": "QC",
        "standard": "Calibration",
        "solvent": "Blank",
        "double_blank": "DoubleBlank",
    },
    required=("sample_name", "position", "ms_method", "data_file"),
    recommended=(),
    method_extensions={"ms_method": (".m",)},
    unsupported=("sample_id", "lc_method", "tune_file", "processing_method", "data_path"),
    data_file_suffix=".d",
    encoding="utf-8-sig",
    verified=(
        "Header 'Sample Name,Barcode,Rack Code,Sample Position,Method,Data File,Sample Type,Level Name,Inj Vol (µL),"
        "Comment', positions like 'P1-A1', sample types 'Calibration' and 'Sample' (example file shipped in "
        "D:\\MassHunter\\Worklist_Import, quoted on the Agilent Community forum).",
        "Headers that match the worklist column names import without a map file (Agilent Community).",
        "Save as UTF-8 when the file contains µ (Agilent Community, MassHunter 12.1).",
        "Inj Vol '-1' means 'As method' (Agilent Known Problem Report, LC/MS Acquisition).",
        "Sample Position, Method and Data File must be filled in (Study Manager Quick Start G3335-90140).",
    ),
    assumed=(
        "Written with a UTF-8 byte-order mark (as Excel's 'CSV UTF-8') and CRLF line endings.",
        "Data File gets a '.d' suffix (MassHunter data files are .d folders); Method should end in '.m'.",
        "Sample types Blank, QC and DoubleBlank (only Sample and Calibration appear in the example); "
        "'solvent' is written as Blank.",
        "The MassHunter Walkup 'Custom Sample Import' format (G2725-90026) is not implemented: the guide could "
        "not be retrieved.",
    ),
    sources=(
        "https://community.agilent.com/technical/mass-spectrometry-software/f/mass-spectrometry-software-user-forum/12581/how-to-format-a-csv-file-for-import-as-a-new-worklist-for-masshunter",
        "https://community.agilent.com/technical/mass-spectrometry-software/f/mass-spectrometry-software-user-forum/9000/map-file-generator-in-masshunter-workstation-version-12-1",
        "https://www.agilent.com/cs/library/support/Patches/SSBs/MHAcqLCTQ_Classic.html",
        "https://labrulez.com/pdf/usermanual_study_manager_quick_start_Mass_Hunter_G3335_90140_EN_B_agilent_cbe834b0c3/usermanual-study-manager-quick-start-MassHunter-G3335-90140EN_B-agilent.pdf",
    ),
)

XCALIBUR = FormatSpec(
    name="thermo_xcalibur",
    title="Thermo Xcalibur sequence (CSV import)",
    vendor="Thermo Fisher Scientific",
    software="Xcalibur 2.2 and later (Sequence Setup)",
    how_to_import="Sequence Setup: File > Import Sequence, select the .csv file and the columns to import.",
    extensions=(".csv",),
    delimiters=(",", ";"),
    columns=(
        ("Sample Type", "sample_type"),
        ("File Name", "data_file"),
        ("Sample ID", "sample_id"),
        ("Path", "data_path"),
        ("Instrument Method", "ms_method"),
        ("Process Method", "processing_method"),
        ("Calibration File", None),
        ("Position", "position"),
        ("Inj Vol", "injection_volume_ul"),
        ("Level", "level"),
        ("Sample Wt", None),
        ("Sample Vol", None),
        ("ISTD Amt", None),
        ("Dil Factor", None),
        ("L1 Study", None),
        ("L2 Client", None),
        ("L3 Laboratory", None),
        ("L4 Company", None),
        ("L5 Phone", None),
        ("Comment", "comment"),
        ("Sample Name", "sample_name"),
    ),
    constants={"Sample Wt": "0", "Sample Vol": "0", "ISTD Amt": "0", "Dil Factor": "1"},
    aliases=_aliases(
        {
            "sample_type": ("Sample Type",),
            "data_file": ("File Name",),
            "sample_id": ("Sample ID",),
            "data_path": ("Path",),
            "ms_method": ("Instrument Method", "Inst Meth"),
            "processing_method": ("Process Method", "Processing Method", "Proc Meth"),
            "position": ("Position",),
            "injection_volume_ul": ("Inj Vol", "Injection Volume"),
            "level": ("Level",),
            "comment": ("Comment",),
            "sample_name": ("Sample Name", "SampleName"),
        }
    ),
    type_out={
        "sample": "Unknown",
        "blank": "Blank",
        "qc": "QC",
        "standard": "Std Bracket",
        "solvent": "Blank",
        "double_blank": "Blank",
    },
    required=("data_file", "ms_method", "position"),
    recommended=("data_path", "injection_volume_ul"),
    method_extensions={"ms_method": (".meth",), "processing_method": (".pmd",)},
    unsupported=("lc_method", "tune_file"),
    data_file_suffix="",
    encoding="utf-8",
    type_aliases={"std clear": "standard", "std update": "standard", "standard": "standard"},
    verified=(
        "Only .csv files import; the first cell of the first row must be 'Bracket Type=n' with n = 1 Overlapped, "
        "2 None, 3 Non-Overlapped, 4 Open (Xcalibur 2.2 Acquisition and Processing User Guide XCALI-97209 D, "
        "Appendix A).",
        "The separator must equal the Windows list separator (comma in the US; often ';' elsewhere) (same guide).",
        "Sample types Unknown, Blank, QC, Std Bracket, Std Clear, Std Update exist (same guide).",
        "Sequence columns Sample Type, File Name, Sample ID, Path, Instrument Method, Processing Method, "
        "Calibration File, Position, Inj Vol, Level, Sample Wt, Sample Vol, ISTD Corr Amt, Dil Factor, Comment, "
        "Sample Name, User Labels 1-5 (same guide, dialog references).",
    ),
    assumed=(
        "The exact header strings of row 2 ('Process Method', 'ISTD Amt', 'L1 Study'... 'L5 Phone') are not "
        "printed by Thermo; they are corroborated by open-source generators that target Xcalibur import "
        "(protti, fgcz/qg, lcms-sequencer). Export one sequence from your own Xcalibur and pass it as a template "
        "to be certain.",
        "Line 1 is a bare 'Bracket Type=n' (as Xcalibur's own export, Rapid-QC-MS issue #83); CRLF line endings.",
        "Sample Wt 0, Sample Vol 0, ISTD Amt 0, Dil Factor 1 are written as defaults.",
        "'standard' is written as 'Std Bracket' (suits bracket type 4, Open); 'solvent'/'double_blank' as 'Blank'.",
        "Method extensions .meth (instrument) and .pmd (processing) are checked only when an extension is given.",
    ),
    sources=(
        "https://tools.thermofisher.com/content/sfs/manuals/Man-XCALI-97209-Xcalibur-22-Acquisition-ManXCALI97209-D-EN.pdf",
        "https://github.com/jpquast/protti/blob/master/R/create_queue.R",
        "https://github.com/czbiohub-sf/Rapid-QC-MS/issues/83",
    ),
)

FORMATS: dict[str, FormatSpec] = {f.name: f for f in (WATERS, SCIEX, AGILENT, XCALIBUR)}

#: Formats that were researched but are deliberately not implemented.
NOT_IMPLEMENTED = {
    "agilent_walkup_custom_import": (
        "MassHunter Walkup Custom Sample Import (G2725-90026): the programmer guide could not be retrieved "
        "(HTTP 403 from agilent.com), so its format could not be verified."
    ),
}


# --------------------------------------------------------------------------- helpers


def fmt_number(value: float) -> str:
    """10.0 -> '10', 2.5 -> '2.5'."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_number(text: str) -> float | None:
    t = text.strip()
    if "," in t and "." not in t:  # decimal comma (e.g. a ';'-separated European Xcalibur export)
        t = t.replace(",", ".")
    if not t:
        return None
    return float(t)


def resolve_columns(
    spec: FormatSpec, template_columns: list[str] | None
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Header list for writing. With a template, map each template header to a canonical field.

    Returns ``(columns, unmapped_fields)`` where ``unmapped_fields`` are canonical fields the
    template has no column for.
    """
    if not template_columns:
        return list(spec.columns), []
    cols: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for header in template_columns:
        f = spec.aliases.get(normalise_header(header))
        if f in seen:
            f = None
        if f:
            seen.add(f)
        cols.append((header, f))
    default_fields = {f for _, f in spec.columns if f}
    return cols, sorted(default_fields - seen - {"index"})


def _cell(spec: FormatSpec, s: Sample, header: str, fld: str | None, index: int) -> str:
    if header in s.extra:
        return s.extra[header]
    if fld is None:
        return spec.constants.get(header, "")
    if fld == "index":
        return str(index)
    if fld == "sample_type":
        return spec.type_out[s.sample_type]
    if fld == "injection_volume_ul":
        if s.injection_volume_ul is None:
            return "-1" if spec is AGILENT else ""
        return fmt_number(s.injection_volume_ul)
    if fld == "data_file":
        name = s.data_file
        if spec.data_file_suffix and name and not name.lower().endswith(spec.data_file_suffix):
            name += spec.data_file_suffix
        return name
    return str(getattr(s, fld))


def render(
    spec: FormatSpec,
    samples: list[Sample],
    *,
    template_columns: list[str] | None = None,
    delimiter: str | None = None,
    bracket_type: int = 4,
) -> str:
    """Render samples to the text of an import file (newlines already in the vendor's style)."""
    delim = delimiter or spec.delimiters[0]
    if delim not in spec.delimiters:
        raise ValueError(f"{spec.title} does not accept the delimiter {delim!r}; use one of {list(spec.delimiters)}.")
    columns, _ = resolve_columns(spec, template_columns)
    # Extra vendor columns that are neither in the default header nor the template.
    # (inserted before a trailing Index column, which MassLynx examples keep last).
    headers = [h for h, _ in columns]
    tail = 1 if columns and columns[-1][1] == "index" else 0
    for s in samples:
        for key in s.extra:
            if key not in headers:
                pos = len(columns) - tail
                headers.insert(pos, key)
                columns.insert(pos, (key, None))
    buf = io.StringIO()
    if spec is XCALIBUR:
        buf.write(f"Bracket Type={bracket_type}{spec.newline}")
    writer = csv.writer(buf, delimiter=delim, lineterminator=spec.newline, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for i, s in enumerate(samples, start=1):
        writer.writerow([_cell(spec, s, h, f, i) for h, f in columns])
    return buf.getvalue()


def encode(spec: FormatSpec, text: str, bom: bool | None = None) -> bytes:
    encoding = spec.encoding
    if bom is not None:
        encoding = "utf-8-sig" if bom else "utf-8"
    return text.encode(encoding)


# --------------------------------------------------------------------------- reading


@dataclass
class Parsed:
    format: str
    samples: list[Sample]
    columns: list[str]
    delimiter: str
    bracket_type: int | None = None
    warnings: list[str] = field(default_factory=list)


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _split_first_line(text: str) -> tuple[str, str]:
    first, _, rest = text.partition("\n")
    return first.rstrip("\r"), rest


def _sniff_delimiter(header_line: str, allowed: tuple[str, ...]) -> str:
    counts = {d: header_line.count(d) for d in allowed}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else allowed[0]


def detect_format(text: str) -> str:
    text = text.lstrip("\ufeff")
    first, rest = _split_first_line(text)
    if first.strip().lower().startswith("bracket type="):
        return XCALIBUR.name
    headers = {normalise_header(h) for h in re.split(r"[,\t;]", first)}
    if {"filename", "msfile"} & headers and ("inletfile" in headers or "samplelocation" in headers or "index" in headers):
        return WATERS.name
    if "sampleposition" in headers and "method" in headers:
        return AGILENT.name
    if "samplename" in headers and ({"msmethod", "vialposition", "datafile", "racktype"} & headers):
        return SCIEX.name
    raise ValueError(
        "Could not recognise the file format from its header. Supported: Waters MassLynx (FIELD IDs such as "
        "FILE_NAME, MS_FILE), SCIEX OS batch (Sample Name, MS Method, Data File), Agilent MassHunter "
        "(Sample Name, Sample Position, Method), Thermo Xcalibur ('Bracket Type=' first line)."
    )


def parse(text: str, fmt: str | None = None) -> Parsed:
    """Parse the text of an import file into vendor-neutral samples."""
    text = text.lstrip("\ufeff")
    name = fmt or detect_format(text)
    spec = FORMATS[name]
    bracket: int | None = None
    body = text
    if spec is XCALIBUR:
        first, body = _split_first_line(text)
        m = re.match(r"\s*Bracket Type\s*=\s*(\d)", first, re.IGNORECASE)
        if not m:
            raise ValueError("Not an Xcalibur sequence: the first cell must be 'Bracket Type=n'.")
        bracket = int(m.group(1))
    header_line, _ = _split_first_line(body)
    delim = _sniff_delimiter(header_line, spec.delimiters)
    rows = list(csv.reader(io.StringIO(body, newline=""), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("The file has no header row.")
    header = [h.strip() for h in rows[0]]
    fields = [spec.aliases.get(normalise_header(h)) for h in header]
    type_in = spec.type_in
    samples: list[Sample] = []
    warnings: list[str] = []
    for rownum, row in enumerate(rows[1:], start=1):
        row = row + [""] * (len(header) - len(row))
        values: dict[str, object] = {}
        extra: dict[str, str] = {}
        for h, f, v in zip(header, fields, row, strict=False):
            if f is None:
                if v != "" and v != spec.constants.get(h, ""):
                    extra[h] = v
                continue
            if f == "index":
                continue
            if f == "sample_type":
                raw = v.strip()
                t = type_in.get(raw.lower())
                if t is None:
                    t = spec.type_aliases.get(raw.lower(), "sample")
                    if raw:
                        extra[h] = v  # keep the vendor's exact type text
                        if raw.lower() not in spec.type_aliases:
                            warnings.append(f"Row {rownum}: sample type {v!r} kept verbatim (no neutral equivalent).")
                values["sample_type"] = t
                continue
            if f == "injection_volume_ul":
                try:
                    vol = parse_number(v)
                except ValueError:
                    extra[h] = v
                    warnings.append(f"Row {rownum}: injection volume {v!r} is not a number; kept verbatim.")
                    continue
                if spec is AGILENT and vol is not None and vol < 0:
                    vol = None
                values[f] = vol
                continue
            if f == "data_file" and spec.data_file_suffix and v.lower().endswith(spec.data_file_suffix):
                v = v[: -len(spec.data_file_suffix)]
            values[f] = v
        if not values.get("sample_name"):
            values["sample_name"] = str(values.get("data_file") or values.get("sample_id") or f"Row{rownum}")
        samples.append(Sample(**values, extra=extra))  # type: ignore[arg-type]
    return Parsed(format=name, samples=samples, columns=header, delimiter=delim, bracket_type=bracket, warnings=warnings)


# --------------------------------------------------------------------------- positions

PositionPattern = Literal["any", "vial_number", "well", "agilent_plate_well", "waters_plate_well", "tray_well"]

POSITION_PATTERNS: dict[str, tuple[str | None, str]] = {
    "any": (None, "No format check."),
    "vial_number": (r"^(\d{1,4})$", "Vial number, e.g. '12'."),
    "well": (r"^([A-Pa-p])(\d{1,2})$", "Well on one plate/tray, e.g. 'A1'."),
    "agilent_plate_well": (r"^P(\d{1,2})-([A-Pa-p])(\d{1,2})$", "MassHunter plate-well, e.g. 'P1-A1'."),
    "waters_plate_well": (r"^(\d{1,2}):([A-Pa-p]),(\d{1,2})$", "MassLynx plate:row,column, e.g. '1:A,1'."),
    "tray_well": (r"^([A-Za-z0-9]{1,3}):([A-Pa-p])(\d{1,2})$", "Tray:well, e.g. 'R:A1' or 'B:C3' (Thermo Vanquish style)."),
}

PLATE_LAYOUTS: dict[int, tuple[int, int]] = {24: (4, 6), 48: (6, 8), 54: (6, 9), 96: (8, 12), 384: (16, 24)}


def check_position(position: str, pattern: str, plate_size: int, max_vial: int) -> str | None:
    """Return an error message, or None if the position fits the pattern and plate."""
    regex, desc = POSITION_PATTERNS[pattern]
    if regex is None:
        return None
    m = re.match(regex, position)
    if not m:
        return f"position {position!r} does not match the {pattern} pattern ({desc})"
    if pattern == "vial_number":
        n = int(m.group(1))
        if not 1 <= n <= max_vial:
            return f"vial {n} is outside 1-{max_vial}"
        return None
    row_letter, col = (m.group(1), m.group(2)) if pattern == "well" else (m.group(2), m.group(3))
    rows, cols = PLATE_LAYOUTS[plate_size]
    r = ord(row_letter.upper()) - ord("A") + 1
    c = int(col)
    if r > rows or not 1 <= c <= cols:
        return f"well {row_letter.upper()}{c} is outside a {plate_size}-position plate ({rows} rows x {cols} columns)"
    return None


def generate_positions(pattern: str, plate_size: int, max_vial: int, start: str, count: int) -> list[str]:
    """Sequential positions from ``start`` (row-major across a plate, then the next plate)."""
    if count <= 0:
        return []
    regex, desc = POSITION_PATTERNS[pattern]
    if regex is None:
        raise ValueError("Automatic positions need a position_pattern other than 'any'.")
    m = re.match(regex, start)
    if not m:
        raise ValueError(f"first_position {start!r} does not match the {pattern} pattern ({desc}).")
    out: list[str] = []
    if pattern == "vial_number":
        n0 = int(m.group(1))
        if n0 + count - 1 > max_vial:
            raise ValueError(f"{count} vials starting at {n0} would exceed max_vial={max_vial}.")
        return [str(n0 + i) for i in range(count)]
    rows, cols = PLATE_LAYOUTS[plate_size]
    if pattern == "well":
        plate, row, col = "", m.group(1).upper(), int(m.group(2))
    else:
        plate, row, col = m.group(1), m.group(2).upper(), int(m.group(3))
    idx = (ord(row) - ord("A")) * cols + (col - 1)
    per_plate = rows * cols
    for _ in range(count):
        if idx >= per_plate:
            if pattern in ("agilent_plate_well", "waters_plate_well"):
                plate, idx = str(int(plate) + 1), 0
            else:
                raise ValueError(f"Ran out of positions on the {plate_size}-position plate.")
        r, c = divmod(idx, cols)
        letter = chr(ord("A") + r)
        out.append(
            {
                "well": f"{letter}{c + 1}",
                "agilent_plate_well": f"P{plate}-{letter}{c + 1}",
                "waters_plate_well": f"{plate}:{letter},{c + 1}",
                "tray_well": f"{plate}:{letter}{c + 1}",
            }[pattern]
        )
        idx += 1
    return out


# --------------------------------------------------------------------------- file names

WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def filename_problem(name: str) -> str | None:
    """Why ``name`` is not a legal Windows file name, or None."""
    if not name:
        return "is empty"
    bad = sorted(set(WINDOWS_ILLEGAL.findall(name)))
    if bad:
        return "contains characters Windows does not allow in file names: " + " ".join(repr(c) for c in bad)
    if name != name.rstrip(" ."):
        return "ends with a space or a dot (Windows strips these)"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return f"uses the reserved Windows device name {name.split('.')[0].upper()!r}"
    if len(name) > 200:
        return f"is {len(name)} characters long (keep data file names well under 255)"
    return None


def sanitise_filename(name: str) -> str:
    """Replace characters that are illegal (or awkward) in Windows file names with '_'."""
    s = WINDOWS_ILLEGAL.sub("_", name)
    s = re.sub(r"\s+", "_", s).rstrip(" .")
    if s.split(".")[0].upper() in WINDOWS_RESERVED:
        s = "_" + s
    return s or "_"
