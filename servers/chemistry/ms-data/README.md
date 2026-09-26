# Mass Spectrometry Data (mzML, Bruker TDF, vendor conversion) — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ms-data -->

Let an AI agent find, read and analyse **LC-MS data files from any vendor** through open interfaces: list the runs in a folder, report the instrument and acquisition details, plot TIC/BPC chromatograms, extract ion chromatograms with apex and area, read spectra, find MS2 scans by precursor m/z, and run a quick QC. Vendor files are converted to mzML with a converter **you** have installed. This is a data server: it controls no hardware.

| | |
|---|---|
| **Package** | `labmcp-ms-data` |
| **Instruments** | Any LC-MS run saved as mzML / mzML.gz / mzMLb; Bruker timsTOF (`.d`, TDF) read directly; Thermo `.raw`, Waters `.raw` folders, Agilent `.d`, SCIEX `.wiff`/`.wiff2`, Shimadzu `.lcd`, Bruker BAF `.d` after conversion |
| **Interfaces** | Files (a local or network data folder) |
| **Protocol** | [mzML 1.1](https://www.psidev.info/mzML) via [pyteomics](https://pyteomics.readthedocs.io/en/latest/api/mzml.html); mzMLb via pyteomics + h5py; Bruker TDF via its SQLite schema + [timsrust](https://github.com/MannLabs/timsrust) ([timsrust-pyo3](https://github.com/jspaezp/timsrust_pyo3)); conversion with [msconvert](https://proteowizard.sourceforge.io/tools/msconvert.html) or [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) |
| **Status** | 🧪 **simulated**: tested against a synthetic LC-MS run, hand-written and generated mzML/mzMLb files and a small Bruker TDF test set; not yet verified on a wide range of real instrument files. [Report a test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without data

```bash
uvx labmcp-ms-data --simulate --check
```

`--simulate` serves one synthetic 12-minute DDA LC-MS/MS run (`simulated_lcms_run.mzML`): eight known analytes (caffeine, sulfamethoxazole, leucine enkephalin, angiotensin II 2+, verapamil, glu-fibrinopeptide B 2+, reserpine, terfenadine) eluting as Gaussian LC peaks with isotope envelopes, a polysiloxane background ion, chemical noise, a top-5 DDA method with dynamic exclusion and fragment spectra, AGC-like injection times, and one electrospray dropout at ~10.6 min for the QC tool to find.

## Point it at your data

1. **Install** (pick the extras you need):
   ```bash
   uvx labmcp-ms-data --help                        # mzML / mzML.gz
   uvx --from 'labmcp-ms-data[bruker]' labmcp-ms-data --help   # + Bruker timsTOF spectra/XICs
   uvx --from 'labmcp-ms-data[mzmlb]' labmcp-ms-data --help    # + mzMLb
   ```
2. **Choose the data folder** with `--address` (default: the current directory; the server refuses to start in the filesystem root, where many MCP clients launch servers, without an `--address`). Subfolders are searched. The agent can only read and write inside this folder: paths with `..`, absolute paths elsewhere and symlinks pointing outside are refused.
   ```bash
   uvx labmcp-ms-data --address ~/data/lcms --check
   ```
3. **Vendor files (optional):** install a converter yourself; this package never bundles or downloads vendor libraries.
   - **Thermo .raw, any OS:** [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) (`--option converter=thermorawfileparser`, plus `--option converter_path=/opt/trfp/ThermoRawFileParser.dll` if it isn't on PATH; `.dll` is started with `dotnet`, `.exe` with `mono` on Linux/macOS). With `converter=auto`, a `converter_path` is recognised from its file name.
   - **All vendors, Windows:** [ProteoWizard msconvert](https://proteowizard.sourceforge.io/download.html) (`--option converter=msconvert`).
   - **All vendors, Linux/macOS:** Docker and `docker pull proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses` (`--option converter=docker`). Pulling the image means you accept the vendor licences. Only the input's folder (read-only) and the output folder are mounted into the container. On Apple Silicon it runs under x86-64 emulation (slow). On Linux the output files are owned by root.
   - `converter=auto` (default) tries ThermoRawFileParser for Thermo files, then msconvert, then Docker.

   The commands used are: `ThermoRawFileParser -i=<file> -o=<dir> -f=2 [-p] [-g]` and `msconvert <file> -o <dir> --mzML --zlib [--gzip] [--filter "peakPicking vendor msLevel=1-"]` (inside Docker via `wine msconvert`). They are run without a shell, with a timeout; on timeout the converter's whole process tree (and the Docker container) is killed and the incomplete output is deleted.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add ms-data -- uvx --from 'labmcp-ms-data[bruker]' labmcp-ms-data --address /path/to/data
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "ms-data": {
      "command": "uvx",
      "args": ["--from", "labmcp-ms-data[bruker]", "labmcp-ms-data", "--address", "/path/to/data",
               "--option", "converter=docker"]
    }
  }
}
```

Add `--read-only` to hide `convert_to_mzml` (the only tool that writes a data file; `save_path` CSV exports inside the data folder remain available). A `save_path` must end in `.csv` (a name without an extension gets one), and an existing file is never replaced unless the call passes `overwrite=true`. For other clients, generate the snippet with `uvx labmcp config ms-data --address /path/to/data --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `convert_to_mzml` | 🎛 control | Convert a vendor file (Thermo .raw, Waters .raw, Agilent .d, SCIEX .wiff, Shimadzu .lcd, Bruker .d) to mzML with the converter the user installed (ThermoRawFileParser, ProteoWizard msconvert, or msconvert in Docker; chosen with --option converter=...). Writes a new file in the data folder. Can take minutes; fails with install instructions if no converter is set up. |
| `extract_ion_chromatogram` | 👁 read | Extracted ion chromatogram (XIC/EIC) for one or more m/z values: the summed intensity within ± tolerance (ppm or Da) in every MS1 spectrum (or another `ms_level`). For each target returns the apex RT and intensity, the apex peak's boundaries, area (intensity x min, no baseline subtraction) and FWHM, plus a downsampled trace. Reads every spectrum in the RT window, so restrict `rt_start_min`/`rt_end_min` on long runs. |
| `find_ms2_scans` | 👁 read | Find the MS2 (MSn) spectra whose precursor m/z is within ± tolerance of `precursor_mz`, optionally within an RT window and for one charge state. Returns index, scan number, RT, precursor m/z, error in ppm, charge and intensity; open any hit with get_spectrum. |
| `get_bpc` | 👁 read | Base peak chromatogram (intensity of the most intense peak per spectrum, with its m/z) vs retention time, downsampled to `max_points`. Cleaner than the TIC for spotting eluting compounds; the base-peak m/z tells you which ion dominates each part of the run. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_run_info` | 👁 read | Describe one run: instrument vendor/model/serial (when the file records them), acquisition date, software, number of spectra per MS level, retention-time and m/z ranges, polarity, centroid/profile, and whether ion-mobility data is present. The first call on a large file indexes it (can take a while); later calls are instant. |
| `get_spectrum` | 👁 read | Read one spectrum, chosen by `index`, `scan_number`, `native_id` or nearest `rt_min` (give exactly one). Returns MS level, RT, polarity, centroid/profile, precursor m/z and charge for MS2, a summary (peak count, TIC, base peak, m/z range) and the `top_n` most intense peaks; `save_path` writes the full peak list to CSV. For profile spectra the top peaks are local maxima of the profile. |
| `get_tic` | 👁 read | Total ion chromatogram (sum of all intensities per spectrum vs retention time) for one MS level, downsampled to `max_points` (keeping the maximum in each bin so peaks survive). Returns the apex, median and area; `save_path` writes every point to CSV. |
| `list_runs` | 👁 read | Find mass-spectrometry runs in the data folder and detect each one's vendor and format (mzML/mzML.gz/mzMLb, Bruker .d TDF or BAF, Agilent .d, Thermo .raw, Waters .raw folder, SCIEX .wiff/.wiff2, Shimadzu .lcd, mzXML). `readable_directly=false` means the run must be converted with convert_to_mzml before it can be analysed. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `summarise_run` | 👁 read | Quick QC of a run: MS1/MS2 counts, TIC stability (CV, spray dropouts), where the signal elutes, cycle time, MS2 scans per cycle, median injection times and how often MS2 hit the maximum injection time, and precursor charge states. Returns plain-language warnings. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server (`reconnect` here closes the cached runs). There are no HAZARD tools, because nothing physical is controlled, so no SAFETY stop tool is needed.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_conversion_time_s` | 3600 s | Longest time a `convert_to_mzml` call may run before the converter is stopped |
| `max_xic_targets` | 50 | Most m/z values in one `extract_ion_chromatogram` call |

Override at launch: `--limit max_conversion_time_s=7200`.

## Example prompts

- "Which runs are in this folder, and which ones need converting?"
- "Give me the instrument, acquisition date and scan counts for QC_2024_03_01.mzML."
- "Extract XICs for caffeine (195.0877) and verapamil (455.2904) at 5 ppm and report apex RT and area."
- "Find every MS2 scan of m/z 523.7745 between 5 and 7 min and show me the fragments of the most intense one."
- "Run a QC on the last three runs: any spray dropouts, and how many MS2 per cycle?"
- "Convert sample_07.raw to mzML, then plot its base peak chromatogram."

## Notes

- **Format support:** mzML (indexed or not, optionally `.gz`) through pyteomics (Apache-2.0), using the PSI-MS vocabulary bundled with psims (no download). mzMLb needs the `[mzmlb]` extra (h5py, hdf5plugin). Gzipped mzML is decompressed to a temporary file for random access.
- **Bruker timsTOF (TDF):** run metadata (instrument, serial, date, m/z and 1/K0 ranges), the TIC (`Frames.SummedIntensities`), BPC intensity (`Frames.MaxIntensity`, the most intense single peak in any mobility scan; its m/z is not stored) and the DDA precursor list are read straight from the `analysis.tdf` SQLite tables (`GlobalMetadata`, `Frames`, `Precursors`, `PasefFrameMsMsInfo`), so they work without extras. Spectra and XICs need the `[bruker]` extra (`timsrust-pyo3`, Apache-2.0, wheels for Linux/macOS/Windows). MS1 and diaPASEF frames are summed over ion mobility (diaPASEF MS2 frames mix all isolation windows); each ddaPASEF precursor is one MS2 spectrum. Ion mobility is not resolved in the outputs. Bruker BAF `.d` folders need conversion.
- **Vendor formats:** Thermo, Waters, Agilent, SCIEX, Shimadzu and Bruker BAF files can only be decoded with the vendors' own libraries, whose licences don't allow redistribution. The server detects them and converts them with a tool you installed (see above). It never downloads vendor code.
- **Numbers:** retention times are in minutes (mzML scan start times in seconds are converted, going by the unit accession); XIC intensity is the sum of all peaks inside ± tolerance; areas are trapezoidal integrals over RT in minutes, with no baseline subtraction, between the points where the trace falls to 1 % of the apex or reaches a valley. The FWHM is measured within those bounds (it is `null` when a co-eluting peak keeps the trace above half height). Use them for relative comparisons.
- **Big files:** the first access to a run makes one pass over all spectra to index it (TIC, precursors, RTs). Up to 4 runs stay cached (`--option cache_size=N`). A run is reopened automatically when the file changes (modification time or size). XICs read every spectrum in the RT window, so narrow the window on long runs.
- **Profile data:** chromatograms and XICs work as-is. `get_spectrum` reports local maxima as "peaks". Convert with peak picking for proper centroids.

## Hardware verification

| Instrument / file type | Software version | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
