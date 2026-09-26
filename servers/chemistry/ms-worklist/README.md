# LC-MS Worklist Builder (MassLynx, SCIEX OS, MassHunter, Xcalibur): MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ms-worklist -->

Let an AI agent build LC-MS sample queues (worklists, sequences, batches) and export them as **import files** for Waters MassLynx, SCIEX OS, Agilent MassHunter Acquisition and Thermo Xcalibur. The agent can lay out samples on a tray, insert blanks and QCs, randomise the run order with a recorded seed, check the list for mistakes, convert an existing file from one vendor to another, and write the file for a person to import. It uses **only documented import formats**. It **never starts an acquisition** and doesn't talk to the mass spectrometer.

| | |
|---|---|
| **Package** | `labmcp-ms-worklist` |
| **Software** | Waters MassLynx 4.x, SCIEX OS, Agilent MassHunter Acquisition (LC/TQ, LC/Q-TOF), Thermo Xcalibur 2.2+ |
| **Interfaces** | File (writes CSV/TXT import files into one output folder) |
| **Protocol** | Vendor sample-list import formats (see [Formats](#formats) for sources) |
| **Status** | 🧪 **simulated**: tested with golden files and round-trip tests, not yet imported into the vendor software by us. [Report a test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without an instrument

```bash
uvx labmcp-ms-worklist --simulate --check
```

`--simulate` uses a temporary output folder that is deleted when the server stops. Everything else is the same code.

## Set up

The "instrument address" is the **output folder** where worklist files are written (default `./worklists`, created if missing):

```bash
uvx labmcp-ms-worklist --address "D:\Worklists" --check        # Windows acquisition PC or a shared drive
uvx labmcp-ms-worklist --address ~/lcms/worklists --check        # macOS / Linux, then copy the files over
```

- The server only reads and writes inside this folder. It refuses `..`, absolute paths elsewhere and symlinks that point outside it. It never overwrites an existing file unless the agent passes `overwrite=true`.
- To edit or convert an existing file, or to use a vendor template, **copy it into the folder first**.
- Point `--address` at a folder the acquisition software watches only if the vendor documents that behaviour for your setup. Otherwise import the file by hand (instructions below).

## Add to your MCP client

**Claude Code**
```bash
claude mcp add ms-worklist -- uvx labmcp-ms-worklist --address ~/lcms/worklists
```

**Claude Desktop / Cursor / Windsurf**
```json
{
  "mcpServers": {
    "ms-worklist": { "command": "uvx", "args": ["labmcp-ms-worklist", "--address", "/path/to/worklists"] }
  }
}
```

`--read-only` keeps only the tools that inspect things (`list_formats`, `validate_worklist`, `import_worklist`, `get_worklist`, `list_worklists`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `add_samples` | 🎛 control | Append samples to a draft worklist (the worklist's defaults and automatic positions apply; any blank/QC/randomisation plan is re-applied to the longer list with the same seed). |
| `create_worklist` | 🎛 control | Create an in-memory draft worklist from a sample list plus defaults (methods, injection volume, tray positions, data-file naming pattern). Nothing is written to disk until export_worklist. Injection volumes above the max_injection_volume_ul limit are refused. |
| `export_worklist` | 🎛 control | Validate the draft and write the vendor import file into the output folder. Returns the path, a preview of the first lines, warnings and import instructions. Refuses if validation finds errors or the file exists (unless overwrite=true). This does not start an acquisition: a person imports the file into the acquisition software. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_worklist` | 👁 read | Show a draft worklist in run order, with its defaults, blank/QC plan, randomisation seed and the history of operations applied to it. |
| `import_worklist` | 👁 read | Parse an existing MassLynx, SCIEX OS, MassHunter or Xcalibur import file from the output folder into a draft, so it can be validated, edited or exported in another vendor's format. Columns without a neutral equivalent are kept verbatim. Only reads; nothing is written. |
| `insert_qc_blanks` | 🎛 control | Insert blanks and QC injections and optionally randomise the run order (with a recorded seed). Replaces any previous plan, so calling it again does not duplicate blanks. The plan is re-applied whenever samples are added; data-file names follow the new run order. |
| `list_formats` | 👁 read | List the supported import formats (Waters MassLynx, SCIEX OS, Agilent MassHunter, Thermo Xcalibur): columns, required fields, sample-type names, file extensions, how to import, what is verified against vendor documents and what is assumed, and the source URLs. Also lists the position patterns and the formats that were researched but not implemented. |
| `list_worklists` | 👁 read | List the draft worklists in memory and the files in the output folder. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `validate_worklist` | 👁 read | Check a draft against the target format: required fields, duplicate data-file names, characters Windows does not allow in file names, tray/vial position format and plate bounds, injection volume (> 0 and <= the max_injection_volume_ul limit), and method file extensions. Errors block export; warnings don't. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. There are **no HAZARD tools**: nothing here moves, heats, injects or acquires. The server only writes files. The CONTROL tools change in-memory drafts or write a file, and `--read-only` hides them.

Typical flow: `create_worklist` → `add_samples` (optional) → `insert_qc_blanks` → `validate_worklist` → `export_worklist`, then import the file in the vendor software.

## Formats

The table below separates what the vendor documents say (**verified**) from what the server assumes. `list_formats` returns the same information to the agent. To be sure of the header for your software version, export an empty batch, sequence or worklist from your own system, put it in the output folder, and pass it to `export_worklist` as `template_path`. The server then writes that header, and that delimiter, exactly.

### Waters MassLynx: sample list worksheet (`waters_masslynx`)

- **Import:** Sample list > File > Import Worksheet, File of Type "Comma Delimited (*.CSV, *.TXT)".
- **Verified:**
  - Row 1 holds the MassLynx FIELD IDs, which are case sensitive. Columns can be in any order, and an `Index` column is added. Source: [WKB63781](https://support.waters.com/KB_Inf/MassLynx/WKB63781_How_do_you_create_a_comma_delimited_text_file_that_can_be_imported_into_MassLynx_as_a_sample_list).
  - The required columns are `FILE_NAME`, `INLET_FILE`, `MS_FILE`, `SAMPLE_LOCATION` and `INJ_VOL`. Source: [MassLynx 4.2 Getting Started Guide 715009602, Table 5-1](https://help.waters.com/content/dam/waters/en/support/usermanuals/2025/715009602/715009602v00.pdf). The "Bottle" column's FIELD ID is `SAMPLE_LOCATION`, not `BOTTLE`.
  - The example files attached to WKB63781 use CRLF line endings and unquoted fields, `TYPE` values `Blank`/`Standard`/`QC`/`Analyte`, and method names without an extension.
- **Mapping:** `FILE_NAME` = data file, `FILE_TEXT` = sample name, `MS_FILE`, `MS_TUNE_FILE`, `INLET_FILE`, `SAMPLE_LOCATION`, `TYPE`, `ID`, `INJ_VOL`, `Index`. You can add any other FIELD ID (for example `CONC_A` or `QUAN_REF`) per sample through `extra`.
- **Assumed:**
  - `solvent` and `double_blank` are written as `Blank`.
  - Extension checks: `.exp` for MS_FILE and `.ipr` for the tune file, applied only when an extension is given.
  - Files are UTF-8 without a BOM. Non-ASCII text is flagged.
  - The `waters_plate_well` position pattern (`1:A,1`) is a common convention. Its comma is CSV-quoted.

### SCIEX OS: batch import (`sciex_os`)

- **Import:** Batch workspace > Open > Import from file, then Save As.
- **Verified:**
  - Batches import from `.txt` or `.csv`, and the column layout must come from a batch exported from SCIEX OS. Source: [SCIEX KB](https://sciex.com/support/knowledge-base-articles/how-to-import-txt-file-to-make-sciex-os-batch_en_us).
  - Column names Sample Name (< 252 characters), MS Method, Sample Type, Data File and Processing Method. Source: [SCIEX OS Feature Guide RUO-IDV-05-15796-C, Table 4-1](https://sciex.com/content/dam/SCIEX/pdf/customer-docs/user-guide/sciex-os-echo-msplus-7600-feature-guide-en.pdf).
  - Rack Type, Rack Position, Plate Type, Plate Position and Vial Position columns. Source: [SCIEX KB](https://sciex.com/resource-hub/knowledge-base-articles/lcms/troubleshooting/unable-to-add-plates-or-samples-to-a-batch-en-us).
  - Sample types Unknown, Blank, Standard, QualityControl, Solvent and Double blank.
  - Several samples can share one data file.
- **Assumed:**
  - **SCIEX doesn't publish the exact header text of an exported batch.** The default header (`Sample Name,Sample ID,Sample Type,MS Method,LC Method,Rack Type,Rack Position,Plate Type,Plate Position,Vial Position,Injection Volume,Data File,Processing Method,Comment`) is a best guess. Use `template_path` with your own export. `validate_worklist` and `export_worklist` warn when no template was used.
  - Extensions `.msm`, `.lcm` and `.qmethod` produce warnings only.
  - Rack and plate columns are set with `vendor_columns` or per sample with `extra`.

### Agilent MassHunter Acquisition: worklist CSV import (`agilent_masshunter`)

- **Import:** Worklist, right-click > "Add/Append Samples from... (csv, xlsx)". Older versions call it "Import Worklist...". You don't need a map file because the headers match the worklist column names.
- **Verified:**
  - The header `Sample Name,Barcode,Rack Code,Sample Position,Method,Data File,Sample Type,Level Name,Inj Vol (µL),Comment`, positions like `P1-A1`, and the types `Calibration` and `Sample` come from the example file shipped in `D:\MassHunter\Worklist_Import`, as quoted on the [Agilent Community](https://community.agilent.com/technical/mass-spectrometry-software/f/mass-spectrometry-software-user-forum/12581/how-to-format-a-csv-file-for-import-as-a-new-worklist-for-masshunter).
  - Save as UTF-8 when the file contains µ. Source: [Agilent Community](https://community.agilent.com/technical/mass-spectrometry-software/f/mass-spectrometry-software-user-forum/9000/map-file-generator-in-masshunter-workstation-version-12-1).
  - `Inj Vol` = `-1` means "As method". Source: [Agilent Known Problem Report](https://www.agilent.com/cs/library/support/Patches/SSBs/MHAcqLCTQ_Classic.html).
  - Sample Position, Method and Data File must be filled in. Source: Study Manager Quick Start G3335-90140.
- **Assumed:**
  - The file has a UTF-8 BOM and CRLF line endings.
  - Data files get a `.d` suffix.
  - Methods should end in `.m`.
  - The sample types Blank, QC and DoubleBlank don't appear in the example; `solvent` is written as Blank.

### Thermo Xcalibur: sequence CSV import (`thermo_xcalibur`)

- **Import:** Sequence Setup > File > Import Sequence.
- **Verified** in the [Xcalibur 2.2 Acquisition and Processing User Guide XCALI-97209 Rev. D](https://tools.thermofisher.com/content/sfs/manuals/Man-XCALI-97209-Xcalibur-22-Acquisition-ManXCALI97209-D-EN.pdf), Appendix A:
  - Only `.csv` files are accepted.
  - The first cell must be `Bracket Type=n` (1 Overlapped, 2 None, 3 Non-Overlapped, 4 Open).
  - The separator must match the Windows list separator. Use `delimiter=";"` where that is `;`.
  - The sequence column names and the sample types Unknown, Blank, QC, Std Bracket, Std Clear and Std Update.
- **Corroborated, not published by Thermo:** the exact row-2 header strings (`Sample Type,File Name,Sample ID,Path,Instrument Method,Process Method,Calibration File,Position,Inj Vol,Level,Sample Wt,Sample Vol,ISTD Amt,Dil Factor,L1 Study,L2 Client,L3 Laboratory,L4 Company,L5 Phone,Comment,Sample Name`). Open-source generators that target Xcalibur import use them: [protti](https://github.com/jpquast/protti/blob/master/R/create_queue.R), fgcz/qg and lcms-sequencer. Native exports start with a bare `Bracket Type=4` line ([Rapid-QC-MS #83](https://github.com/czbiohub-sf/Rapid-QC-MS/issues/83)). Export one sequence from your Xcalibur as a template if you want certainty.
- **Assumed:**
  - Sample Wt 0, Sample Vol 0, ISTD Amt 0 and Dil Factor 1 are written as defaults.
  - `standard` is written as `Std Bracket`, which suits bracket type 4.
  - Extension checks: `.meth` for the instrument method and `.pmd` for the processing method.

### Not implemented

- **MassHunter Walkup Custom Sample Import** ([G2725-90026](https://www.agilent.com/cs/library/usermanuals/public/G2725-90026_Walkup_Programmer.pdf)): the guide couldn't be retrieved (HTTP 403), so its format couldn't be verified.
- **Native worklist and sequence files** (MassHunter `.wkl` XML, Xcalibur `.sld`, MassLynx `.spl`): not implemented. We found no public specification for them, so the server uses the CSV import paths above.

## Validation

`validate_worklist`, which `export_worklist` also runs, reports **errors**, which block export, and **warnings**:

- Required fields for the format are missing.
- Duplicate data-file names, compared case-insensitively. For SCIEX OS a shared data file is allowed, but a sample name repeated within one data file is an error.
- Characters Windows doesn't allow in file names (`< > : " / \ | ? *`, control characters), a trailing dot or space, and reserved names (CON, NUL, COM1…).
- The position doesn't match the chosen `position_pattern` (`vial_number`, `well`, `agilent_plate_well`, `waters_plate_well`, `tray_well`) or falls outside the `plate_size` (24/48/54/96/384) or `max_vial`.
- The injection volume is ≤ 0 or above the `max_injection_volume_ul` limit.
- The method file has the wrong extension, for example an Analyst `.dam` in a SCIEX OS batch, or a MassHunter `.m` in an Xcalibur sequence.
- Fields the target format has no column for, and non-ASCII text in formats without a BOM.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_injection_volume_ul` | 100 µL | Largest injection volume allowed in a worklist. Enforced when samples are added and again at export. |

Override at launch, for example `--limit max_injection_volume_ul=20` for a 20 µL loop.

## Example prompts

- "Make an Xcalibur sequence for the 40 plasma samples in this list: wells A1 onwards on the R tray, 2 µL, method C:\Xcalibur\methods\DIA_60min. Add a blank after every 10 samples from R:H12 and a pooled QC at the start and end from R:H11. Randomise the samples and tell me the seed."
- "Import `old_batch.csv` from the worklist folder and convert it to a MassHunter worklist with method D:\MassHunter\methods\Panel.m."
- "I put `blank_batch.txt` (exported from our SCIEX OS) in the folder. Use it as the template for this batch."
- "Check this MassLynx sample list for duplicate file names and bad vial positions before I import it."

## Notes

- Drafts live in memory. `reconnect` (or restarting the server) discards them. In `--simulate` it also deletes the temporary folder. Export files you want to keep.
- By default, `export_worklist` also writes `<file>.provenance.json` next to the worklist. It contains the operation history, the randomisation seed and the sample order before randomisation, so the run order can be reproduced. Pass `write_provenance=false` to skip it.
- Randomisation shuffles only the types in `randomize_types` (by default unknown samples) among their own slots, using Python's `random.Random(seed)`. Standards, blanks and QCs stay where they are.
- Method names must match methods that exist on the acquisition PC. The server can't check that.

## Verification

| Software | Version | Format | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
