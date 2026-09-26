# SRS Residual Gas Analyzer: MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-srs-rga -->

Let an AI agent read the residual gas composition of a vacuum system with a **Stanford Research Systems RGA100, RGA200 or RGA300** quadrupole mass spectrometer: partial pressures at chosen m/z, analog and histogram spectra, total pressure, helium leak checks and a first-pass identification of the gases (water, hydrogen, nitrogen/CO, oxygen, argon, CO2, hydrocarbons). The filament, the electron multiplier (CDEM) and the ionizer degas are protected by server-side pressure interlocks.

| | |
|---|---|
| **Package** | `labmcp-srs-rga` |
| **Instruments** | SRS RGA100 (1-100 amu), RGA200 (1-200 amu), RGA300 (1-300 amu), with or without the CDEM option (01) |
| **Interfaces** | RS-232 (fixed 28,800 baud, 8N1, RTS/CTS), TCP via a serial-to-Ethernet adapter or the SRS RGA Ethernet adapter |
| **Protocol** | SRS RGA command set: [Operating Manual and Programming Reference, Models RGA100/200/300](https://www.thinksrs.com/downloads/pdfs/manuals/RGAm.pdf), Rev. 1.9, chapter 6 "Programming the RGA Head" |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-srs-rga --simulate --check
uvx labmcp-srs-rga --simulate --option scenario=helium_leak    # He pulses every 20 s for leak_check
uvx labmcp-srs-rga --simulate --option scenario=overpressure   # a growing leak trips the filament
```

The simulator is an RGA200 with a CDEM on an unbaked chamber at ~1e-7 Torr (mostly water, then H2, N2/CO, CO2).

## Connect your RGA

1. **Cable:** the RGA's electronics box (ECU) has a DB-9 RS-232 port wired as DCE with **RTS/CTS hardware handshaking**. Use a straight-through cable (or a USB-serial adapter) that carries RTS and CTS. The settings are fixed: 28,800 baud, 8 data bits, no parity, 1 stop bit. The server sets these for you.
2. **Close RGA Windows / RGA Com** (only one program can use the port).
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-srs-rga --address /dev/ttyUSB0 --check          # Linux
   uvx labmcp-srs-rga --address /dev/tty.usbserial-XXXX --check  # macOS
   uvx labmcp-srs-rga --address COM3 --check                  # Windows
   uvx labmcp-srs-rga --address tcp://192.168.1.50:4001 --check   # raw-TCP serial-to-Ethernet adapter
   uvx labmcp-srs-rga --address tcp://192.168.1.50:818 --option user=admin --option password=admin --check  # SRS RGA Ethernet adapter
   ```
   On connection the server sends `ID?` and `IN0` (clear the RGA's buffers and run its hardware self-check). It does not switch anything on.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add rga -- uvx labmcp-srs-rga --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, ...)
```json
{
  "mcpServers": {
    "rga": {
      "command": "uvx",
      "args": ["labmcp-srs-rga", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Add `--read-only` to allow measurements but block switching the filament/CDEM, degassing, calibration and settings changes (the off switches stay available). For other clients, generate the snippet with `uvx labmcp config srs-rga --address /dev/ttyUSB0 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `all_off` | 🛑 safety | Put the RGA in a safe state: abort any degas, CDEM off (HV0), filament off (FL0) and quadrupole RF/DC off (MR0). Use when finished, before venting, or if anything looks wrong. It interrupts a running scan or leak check, and every step is attempted even if an earlier one fails. |
| `analog_scan` | 👁 read | Record an analog mass spectrum (SC1): the quadrupole steps through the mass range and the full peak shapes are returned (downsampled) with a peak list, the total pressure measured at the end of the scan, and optionally the full data as CSV. Use it to check peak positions and to survey unknown gases. Duration depends on the noise floor (e.g. 126 ms/amu at NF4). An existing save_path file is never overwritten. |
| `calibrate` | ⚠️ hazard | Calibrate the detector: `zero` (CA) re-zeroes the ion detector at the present noise floor and detector and corrects the RF scan table for temperature drift (seconds); `electrometer` (CL) recalibrates the electrometer's full I-V response (longer, clears all zero offsets). The quadrupole RF is switched off at the end. |
| `cdem_off` | 🛑 safety | Switch the electron multiplier off (HV0) and return to Faraday-cup detection. Always allowed, and it interrupts a running scan, mass measurement or leak check. |
| `degas` | ⚠️ hazard | Start an ionizer degas (DG): 20 mA of 400 eV electrons clean the ion source by electron stimulated desorption. The CDEM is switched off and left off. Degassing shortens filament life; prefer a bakeout. Refused above `max_filament_pressure_torr` and beyond `max_degas_minutes`. Returns immediately; the RGA is busy until it finishes (any command would abort it), and `filament_off` / `all_off` stop it. |
| `filament_off` | 🛑 safety | Switch the filament off (FL0), stopping a degas first if one is running. Always allowed, and it interrupts a running scan, mass measurement or leak check. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_status` | 👁 read | Report the RGA state: filament emission, CDEM on/off and voltage, ionizer settings, scan settings, stored sensitivity factors, and the decoded error bytes (e.g. FL6 when the overpressure protection shut the filament down). Reading the error bytes clears the communication (EC?) and CDEM (EM?) error bytes, as on the instrument. |
| `histogram_scan` | 👁 read | Record a bar-graph spectrum (HS1): one peak-locked value per integer m/z plus the total pressure. Faster than an analog scan and the usual way to follow a residual gas composition. |
| `identify_residual_gases` | 👁 read | Run a histogram scan and assign the major peaks to likely gases with a simple lookup of standard 70 eV fragment patterns: H2 (2), He (4), CH4 (16/15), H2O (18/17), N2 or CO (28, split with 14 and 12), O2 (32), Ar (40), CO2 (44), plus diagnoses such as an air leak or hydrocarbon contamination. A screening aid, not a quantitative analysis. |
| `leak_check` | 👁 read | Helium leak check: monitor m/z 4 while someone sprays helium on suspect joints, and report the baseline, every response above `threshold_factor` x baseline (start/end time, peak) and the time series. Keep the first few seconds free of helium so the baseline is clean. The response time of a real leak is a few seconds. Bounded by `max_leak_check_duration_s`. |
| `measure_masses` | 👁 read | Measure partial pressures at a list of m/z values (peak-locked single-mass measurements, MR). Fast and precise for monitoring known gases; the precision and time per mass are set by the noise floor (`set_scan_parameters`). Pressures are N2-equivalent. |
| `read_total_pressure` | 👁 read | Measure the total pressure with the RGA acting as an ionisation gauge (TP?, Faraday cup). Needs the filament on and the CDEM off. The value is N2-equivalent and can differ from a Bayard-Alpert gauge by a factor of a few, depending on the gas composition. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_cdem` | ⚠️ hazard | Switch the electron multiplier (CDEM) on at a given high voltage for ~100-10000x more signal. High pressure shortens CDEM life and can destroy it, so this is refused unless the pressure is below `max_cdem_pressure_torr` (RGA total pressure measured now with the Faraday cup, a recent one, or `external_pressure_torr`), and the voltage is capped by `max_cdem_voltage_v`. Total-pressure readings are disabled while the CDEM is on. |
| `set_filament` | ⚠️ hazard | Switch the filament on (or change its emission current). The filament burns out or is damaged above 1e-4 Torr, so this is refused unless the pressure is below `max_filament_pressure_torr`: pass `external_pressure_torr` from a separate gauge, or, if the filament is already on, the RGA's own total pressure is measured. The RGA's protection will also shut the filament down on overpressure (reported as FL6). |
| `set_scan_parameters` | 🎛 control | Change measurement settings: noise floor (speed vs detection limit), electron energy, ion energy, focus plate voltage and analog-scan resolution. Sensitivity factors stored in the head are only valid at the default ionizer settings (70 eV, 12 eV, 90 V). After changing the noise floor the detector zero is re-adjusted automatically at the start of the next scan. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

The RGA filament must only run below 1e-4 Torr, and a CDEM should never see voltage above 1e-4 Torr, with 5e-6 Torr or less recommended (manual: FL command warning p. 6-35, "CDEM Handling and Care" in the Maintenance chapter). The RGA only measures pressure while its filament emits, so switching on needs evidence:

- `set_filament` and `degas`: the pressure must be at or below `max_filament_pressure_torr`. `set_cdem`: at or below `max_cdem_pressure_torr`, and the voltage at or below `max_cdem_voltage_v` (the hardware range of 10-2490 V is enforced as well).
- Evidence for all three: `external_pressure_torr` (a reading from another gauge that the user confirmed) and, when the filament is on and the CDEM off, a total-pressure measurement taken right then. If both are available, the **higher** one is checked, so a mistyped gauge value can't override what the RGA measures. With the CDEM on (the RGA can't measure), a reading from the last `max_pressure_reading_age_s` is accepted, but only while the filament is still on: once it is off (switched off or tripped) an older reading says nothing about the present pressure.
- RGA readings used as evidence are scaled to 1 mA emission: the ion current is proportional to the emission current, so a reading at 0.1 mA would otherwise understate the pressure 10x.
- The instrument's own filament protection still applies: an overpressure shuts the emission and the CDEM down (error FL6), and `get_status` reports it.
- `all_off`, `filament_off` and `cdem_off` interrupt a running scan, mass measurement or leak check (which then returns an error and no data) instead of waiting for it to finish. `all_off` attempts every step even if one fails, and reports what failed.

| Limit | Default | Meaning |
|---|---|---|
| `max_filament_pressure_torr` | 1e-4 Torr | Highest pressure at which the filament may be switched on or the ionizer degassed |
| `max_cdem_pressure_torr` | 5e-6 Torr | Highest pressure at which the CDEM may be switched on |
| `max_cdem_voltage_v` | 2000 V | Highest CDEM high voltage (hardware maximum 2490 V) |
| `max_pressure_reading_age_s` | 60 s | Oldest RGA total-pressure reading accepted as evidence (only while the filament stays on) |
| `max_degas_minutes` | 3 min | Longest ionizer degas (hardware maximum 20 min) |
| `max_scan_duration_s` | 900 s | Longest estimated analog/histogram scan |
| `max_leak_check_duration_s` | 1800 s | Longest helium leak-check run |

Override at launch, e.g. `--limit max_cdem_voltage_v=2400 --limit max_filament_pressure_torr=1e-5`.

## Example prompts

- "The ion gauge reads 2e-7 Torr. Switch the RGA filament on and tell me what's in the chamber."
- "Take a 1-100 amu analog scan, save it to ~/data/rga/after_bake.csv, and compare water and hydrogen with this morning."
- "Is this an air leak? Check 28, 32 and 40 and the ratios."
- "Start a 3-minute helium leak check on m/z 4; I'll spray the flanges one by one after 20 seconds."
- "Switch the CDEM on at 1400 V, measure 2, 4, 18, 28, 44 every minute for 10 minutes, then switch everything off."

## Notes

- **Wire format.** Commands are two letters plus a parameter, ending with CR; ASCII replies end with LF CR. Ion currents from `MR`, `TP?` and every scan point are binary 4-byte little-endian two's complement integers in units of 1e-16 A. Hardware commands (`IN`, `FL`, `HV`, `EE`, `IE`, `VF`, `CA`, `CL`, `DG`) answer with the STATUS byte; `NF`, `MI`, `MF`, `SA`, `TP0/1`, `MR0` answer nothing. The server reads back every silent setting.
- **Rejected commands get no reply.** The RGA only flashes its error LED and sets RS232_ERR; the server then reads `EC?` and reports the reason (bad command, bad parameter, parameter conflict).
- **Partial pressures are N2-equivalent.** The server divides the ion current by the partial-pressure sensitivity `SP` stored in the head (factory value: N2 at default ionizer settings), and by the stored CDEM gain `MG` when the CDEM is on. Other gases have different sensitivities (H2 ~0.44, He ~0.14, Ar ~1.2 relative to N2); `identify_residual_gases` corrects with typical values, the other tools do not. The stored factors are only valid at 70 eV / 12 eV / 90 V and 1 mA emission.
- **Total pressure** (`TP?`) is disabled by the RGA while the CDEM is on (it returns 0), and needs the filament on.
- **Degas** runs inside the RGA and any command aborts it, so `degas` returns immediately and the server refuses other commands (except `filament_off`, `cdem_off` and `all_off`, which abort it) until it ends.
- **Noise floor** (`NF0`-`NF7`) sets the speed and detection limit: from 2000 ms/amu and ~7e-15 A noise (NF0) to 15 ms/amu and ~5e-13 A (NF7). The factory default is NF4 (126 ms/amu).
- **SRS RGA Ethernet adapter** login (`--option user=... --option password=...`, port 818) follows SRS's `srsinst.rga` driver and has not been tested on hardware. Generic raw-TCP serial servers need no login but must be set to 28,800 baud 8N1 with RTS/CTS.
- After a scan or `measure_masses` the server switches the quadrupole RF off (`MR0`), as the manual recommends.
- **`save_path`** (scans and `leak_check`) must end in `.csv` and must not exist yet: existing files are never overwritten. Missing folders are created, `~` is expanded, and the path is checked before the measurement starts.
- **Link recovery.** If a scan's binary data doesn't arrive in time (or a safety tool interrupts it), the server discards the partial data, halts the scan with `IN0` and waits for its STATUS reply before the next command, so later replies stay in step.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
