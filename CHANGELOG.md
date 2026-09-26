# Changelog

Notable changes to LabMCP. Each package is versioned on its own and released with a tag such as `labmcp-v0.1.0` or `labmcp-ika-v0.1.0`. Safety-relevant fixes are marked **[safety]**.

## [0.1.1] - 2026-09-26

`labmcp` 0.1.1 and 13 server updates (0.1.1), four new mass-spectrometry servers (0.1.0), and a protocol review of every existing server against vendor manuals and specs.

### New servers (mass spectrometry)

- **`labmcp-ms-data`:** reads LC-MS data from mzML/mzMLb and Bruker timsTOF `.d` (TDF). Tools cover run info, TIC/BPC, XICs with apex and area, spectra, MS2 search and quick QC. Vendor files (Thermo, Waters, Agilent, SCIEX, Shimadzu) are converted with the user's own msconvert or ThermoRawFileParser; no vendor DLLs are bundled.
- **`labmcp-ms-worklist`:** builds, randomises, validates and exports sample-queue import files for Waters MassLynx, SCIEX OS, Agilent MassHunter and Thermo Xcalibur. It writes files only and never starts an acquisition.
- **`labmcp-srs-rga`:** controls SRS RGA100/200/300 residual gas analysers: spectra, partial and total pressure, and helium leak checks. **[safety]** The filament, CDEM and degas are interlocked on pressure.
- **`labmcp-thermo-iapi`:** a thin adapter for the licensed Thermo Instrument API (Orbitrap Tribrid, Exploris and Exactive). Users supply their own IAPI assemblies. Custom scans are validated and rate-limited.

### Fixes

- **Core:** **[safety]** NaN inputs and `--limit x=nan` no longer bypass safety limits. SCPI simulators now reject truncated keywords (such as `VOLTA`), as real instruments do. Binary block reads wait up to 1 s for the closing newline instead of 50 ms, so a late newline can no longer shift every later reply. Shorthand addresses with a `?query` are now parsed correctly.
- **scpi-instrument:** **[safety]** the write denylist can no longer be bypassed with compound SCPI headers (for example `OUTP:POL NORM;STAT ON`).
- **palmsens:** **[safety]** the cell is switched off after a MethodSCRIPT runtime error (`on_finished:` is skipped on errors). The potential limit now also covers SWV, NPV, ACV, PAD, EIS and fast CV/CA in raw scripts.
- **opentrons:** **[safety]** `deactivate_modules` sends every command to every module, even after a timeout or error.
- **labjack:** T8 thermocouples use the documented cold-junction register (60052).
- **lakeshore:** a 336 output in Mirroring mode no longer reports a control input.
- **srs-lockin:** SR86x snapshots use a single coherent `SNAP?`.
- **ble-health:** the thermometer waits for the final measurement instead of an intermediate one. Adapter selection works on bleak 1.0.x.
- **micro-manager:** colour-camera images save correctly (8-bit as ImageJ RGB, deeper as 3 channels).
- **mettler-toledo:** preset tare is sent as a plain decimal, with no precision loss or exponent notation.
- **modbus:** float32 values too large to fit are refused cleanly.
- **cavro:** error code 4 is decoded. **astm-lis:** the LIS2-A2 abnormal flags A/U/D/B/W are decoded. **bench-psu:** the simulator accepts Aim-TTi MX `OVP<N> ON|OFF`.

## [0.1.0] - 2026-09-26

First public release: the `labmcp` core library and 28 instrument servers, all published to PyPI, with each server also listed in the MCP Registry.

- **Core (`labmcp`):** serial, TCP and VISA transports; a simulator base; safety limits (`--limit`); read-only mode (`--read-only`); hazard annotations; an audit log (`get_command_log`, `--audit-log`); a connection check (`--check`); and the `labmcp` CLI (`list`, `info`, `ports`, `config`).
- **Servers:** 5 biology, 6 chemistry, 5 physics, 3 health, 5 engineering and 4 universal-protocol servers (SCPI, Modbus, EPICS, SiLA 2). See the [catalog](README.md#-supported-instruments).
- Every server has a wire-level simulator (`--simulate`). All are 🧪 simulated; none is hardware-verified yet.
