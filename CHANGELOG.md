# Changelog

Notable changes to LabMCP. Each package is versioned on its own and released with a tag such as `labmcp-v0.1.0` or `labmcp-ika-v0.1.0`. Safety-relevant fixes are marked **[safety]**.

## Registry metadata releases - 2026-09-26

A patch release of every server (`labmcp` itself is unchanged at 0.1.2). The only change is metadata: each server's MCP Registry description is now a complete phrase of at most 100 characters, where before it was cut off mid-word. `build_catalog.py` accepts an optional `[tool.labmcp] registry_description` for descriptions that don't shorten cleanly. The README now lists mass spectrometers, and questions go through a new Question issue template.

## [0.1.2] - 2026-09-26

`labmcp` 0.1.2 and patch releases of 27 servers (every server except IKA; the four mass-spectrometry servers are still unreleased at 0.1.0). A software review of the whole codebase, looking for logic, concurrency, error-handling and safety bugs rather than protocol details. It adds about 280 regression tests. Servers that use the new `prepare_save_path` require `labmcp>=0.1.2`.

### Across all servers

- **[safety]** New `labmcp.prepare_save_path`. Every `save_path` now requires the expected extension, refuses existing files (unless a tool offers `overwrite`), folders, symlinks to other files and Windows device names, and is checked *before* a measurement starts. Before, most servers silently overwrote any file the agent named, and a bad path lost the data at the end of a long run.
- **[safety]** Stop tools no longer wait behind long operations in LabJack streams, NI-DAQmx acquisitions, RGA scans and leak checks, Ocean acquisitions and Mettler adjustments. Sweeps and series also stop within their time budget.
- A FastMCP tool timeout does not stop a synchronous tool. Long operations now have their own time budgets, below the tool timeouts, so the hardware is left in a known state.
- Downsampled waveforms keep each block's minimum and maximum, so spikes are no longer dropped. NaN/inf no longer break structured tool results.

### Core, scripts and CI

- A failed connect closes the transports it opened, so a retry no longer finds the port busy. The `driver` property is safe against a concurrent `reconnect`.
- Timeouts and limits that are NaN, infinite, zero or negative are refused, and so are invalid limit kinds. An audit-log write failure no longer blocks commands, including stop commands.
- TCP writes have a timeout, VISA timeouts apply per call rather than per byte, and closed or errored transports raise `InstrumentConnectionError`.
- `build_catalog.py` and `new_server.py` now produce UTF-8, LF and valid Python. `release.yml` validates tags, and a core release runs the full test suite.

### Servers (highlights)

- **keithley-smu:** **[safety]** an `output_off` sent while a sweep was being configured is no longer lost.
- **lakeshore:** **[safety]** `set_heater_range` checks the programmed setpoint against `max_setpoint_k` before enabling heat.
- **palmsens:** **[safety]** stopping a raw script, or a read failure mid-measurement, now switches the cell off. Raw scripts can't raise the current range past the limit.
- **srs-rga:** **[safety]** stops interrupt scans. Replies stay in step after a timeout. Stale or low-emission pressure readings can no longer approve switching the filament or CDEM on, and `all_off` attempts every step.
- **thermo-iapi:** **[safety]** `scan_count` and `until_stopped` acquisitions are cancelled after `max_acquisition_duration_s`. A successful start is no longer reported as a failure.
- **scpi-instrument, epics, modbus, sila2:** **[safety]** closed bypasses of the write denylist and allowlist (quotes inside block data), of EPICS limit fields, and of Modbus duplicate or overlapping map entries. Limits are checked on the value actually stored. SiLA FDL refuses XML entity declarations.
- **mettler-toledo:** **[safety]** line breaks can no longer inject commands. Replies are matched to their commands, and `reset_balance` aborts an internal adjustment.
- **new-era, tecan-cavro:** **[safety]** stop retries if its reply is lost. A timed-out Cavro move is terminated.
- **bench-psu:** **[safety]** a channel that reports an error after `output_on` is switched off again.
- **ms-data:** a unit name of "seconds" is no longer read as minutes (a 60× error). The server refuses to serve `/` when no `--address` is given. Conversions kill the whole process tree on timeout, and Docker mounts are read-only.
- **ms-worklist:** an extra column can no longer bypass `max_injection_volume_ul`, and the other validation holes are closed.
- **micro-manager:** `set_shutter` is now HAZARD. The z-stack no longer tries to allocate billions of positions, and colour-camera and objective-group mistakes are refused.
- **brainflow:** `configure_board` is now HAZARD (it can switch on impedance-test current), and `stop_streaming` is SAFETY.
- **astm-lis:** results survive `reconnect`, and malformed input no longer drops whole messages. **ble-health:** timed-out measurements are cancelled, and half-open connections are closed.

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
