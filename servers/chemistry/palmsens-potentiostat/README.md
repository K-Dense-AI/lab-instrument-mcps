# PalmSens Potentiostat — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-palmsens -->

Let an AI agent run cyclic voltammetry, linear sweep voltammetry, differential pulse voltammetry and chronoamperometry on **PalmSens potentiostats that speak MethodSCRIPT**, and get back downsampled data, peak summaries and optional full-resolution CSV files.

| | |
|---|---|
| **Package** | `labmcp-palmsens` |
| **Instruments** | EmStat Pico (module and development board), EmStat4 LR/HR (EmStat4S, EmStat4M, EmStat4X), Sensit Wearable / Sensit BT, Nexus |
| **Interfaces** | USB (virtual COM port), UART |
| **Protocol** | MethodSCRIPT v1.x ([MethodSCRIPT manual v1.8](https://www.palmsens.com/app/uploads/2025/10/MethodSCRIPT-v1_8.pdf)) over the PalmSens online communication protocol ([EmStat Pico v1.6](https://assets.palmsens.com/app/uploads/2025/10/Emstat-Pico-communication-protocol-V1.6.pdf), [EmStat4 v1.3](https://assets.palmsens.com/app/uploads/2024/03/EmStat4-communication-protocol-V1.3.pdf)); cross-checked against [PalmSens/MethodSCRIPT_Examples](https://github.com/PalmSens/MethodSCRIPT_Examples) |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-palmsens --simulate --check
```

The simulator is an EmStat Pico with a 3 mm disk electrode in 1 mM of a reversible one-electron redox couple (E° = +0.20 V, like ferro/ferricyanide vs Ag/AgCl). It interprets the scripts the server sends and streams correctly encoded data packages, so you get a proper CV "duck", DPV peaks and Cottrell decays. Simulated time runs 10× faster than real time; change that with `--option sim_speed=1` (real time) or `=100`.

## Connect your instrument

1. **Close PSTrace / MultiTrace.** Only one program can hold the serial port.
2. **Find the port:** `uvx labmcp ports`. An EmStat Pico development board or Sensit shows up as a USB serial port (FTDI). An EmStat4 or Nexus shows up as a virtual COM port named after the device.
3. **Test the connection:**
   ```bash
   uvx labmcp-palmsens --address /dev/ttyUSB0 --check            # EmStat Pico / Sensit (Linux)
   uvx labmcp-palmsens --address /dev/tty.usbmodemXXXX --check   # EmStat4 (macOS)
   uvx labmcp-palmsens --address COM7 --check                    # Windows
   ```
   Defaults are the EmStat Pico's: **230400 baud, 8N1, XON/XOFF**, LF line endings. Over USB the EmStat4 and Nexus ignore the baud rate. For an EmStat4 **UART** connection use `serial://COM7?baudrate=921600&rtscts=true&xonxoff=false`.
4. `--check` sends the firmware-version (`t`) and serial-number (`i`) commands and prints the model, firmware and serial.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add potentiostat -- uvx labmcp-palmsens --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "potentiostat": {
      "command": "uvx",
      "args": ["labmcp-palmsens", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Add `--read-only` to allow device information only: every measurement tool applies potential and is hidden.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `abort_measurement` | 🛑 safety | Abort the running measurement or script immediately (communication command Z) and make sure the cell is switched off. Safe to call when nothing is running. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Identify the potentiostat (model, firmware, serial number) and list its potential window, PGStat modes and current ranges, so measurement parameters can be chosen within them. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `run_chronoamperometry` | ⚠️ hazard | Hold the cell at `potential_v` for `run_time_s` and record the current every `interval_s`. Returns the data (time from the instrument timer) plus first/last/mean current and the charge. Applies potential to the cell. |
| `run_cyclic_voltammetry` | ⚠️ hazard | Run a cyclic voltammogram: begin -> vertex 1 -> vertex 2 -> begin, `n_scans` times, and return potential/current/time data with the anodic and cathodic peaks of every scan. Applies potential to the cell for the whole scan; the cell is switched off at the end. |
| `run_differential_pulse_voltammetry` | ⚠️ hazard | Run differential pulse voltammetry (current = forward - reverse, per step) from begin to end potential and return the data with the peak current and peak potential. The scan rate must be below step / pulse_time / 2. Applies potential to the cell. |
| `run_linear_sweep_voltammetry` | ⚠️ hazard | Run a linear sweep voltammogram from `begin_potential_v` to `end_potential_v` and return the data with the largest/smallest current and where they occur. Applies potential to the cell. |
| `run_methodscript` | ⚠️ hazard | Advanced: run a raw MethodSCRIPT and return its raw output and decoded data packages. Literal potentials of set_e / set_range_minmax da and the CV, LSV, DPV, SWV, NPV, ACV, CA, PAD, EIS and fast CV/CA techniques are checked against `max_potential_v`, and literal `set_range ba` / `set_autoranging ba` currents against `max_current_range_a`; values computed at run time are not. Aborted after `timeout_s`; add `on_finished:` + `cell_off` to your script so the cell is switched off after an abort. After a runtime error, a timeout or `abort_measurement` the server switches the cell off itself. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_potential_v` | 2.0 V | Largest absolute potential vs RE any technique (or literal in a raw script) may apply, including DPV pulses |
| `max_current_range_a` | 0.01 A | Highest current range an agent may select (with autorange, the instrument never goes above `current_range_a`); in raw scripts, literal `set_range ba` / `set_autoranging ba` values |
| `max_duration_s` | 600 s | Longest measurement (including equilibration) or raw-script timeout |

Override at launch, e.g. `--limit max_potential_v=1.0 --limit max_duration_s=1800`. Whatever the limits say, a single tool call is capped at one hour.

## Example prompts

- "Run a CV of my ferrocyanide sample from −0.2 V to +0.6 V and back at 50 mV/s, three cycles, and tell me E½ and the peak separation."
- "Do a scan-rate study: CVs at 25, 50, 100 and 200 mV/s between −0.1 and 0.5 V, save each as CSV, and check whether the peak current scales with √v."
- "Run DPV from 0 to 0.6 V with 50 mV pulses and report the peak potential and height."
- "Hold 0.5 V for 60 s, record the current every 0.2 s, and give me the total charge passed."
- "Which potential window and current ranges does this potentiostat support?"
- "Abort the measurement."

## Notes

- **What the server sends.** Each technique is one MethodSCRIPT modelled on PalmSens' measurement-loop examples: `set_pgstat_chan 0`, `set_pgstat_mode`, `set_max_bandwidth`, `set_range_minmax da`, `set_range ba`, `set_autoranging ba`, `set_e`, `cell_on`, optional `wait`, then `meas_loop_cv` / `lsv` / `dpv` / `ca` with `pck_add` of the set potential and current, and `on_finished: cell_off`. So the cell is switched off after completion and after an abort. A runtime error skips `on_finished:` (MethodSCRIPT manual §10.1), so after one the server sends a separate `cell_off` script. The exact script is in `get_command_log`.
- **PGStat mode and bandwidth.** Max bandwidth is 6× the data rate (the ratio PalmSens' examples use). On the EmStat Pico and Sensit, the server picks low speed (−1.25 to 2.0 V, 2.2 V window, ≤ 100 Hz), then max range (−1.7 to 2.0 V, 2.6 V window), then high speed (1.214 V window) to fit your potentials. If none fits, it says why before anything is sent. EmStat4 and Nexus use low speed mode; per the manual, the modes do not differ on those instruments.
- **Current sign and units.** Anodic (oxidation) current is positive; potentials are the *set* potentials vs RE; `time_s` comes from the instrument timer for chronoamperometry and from step/scan rate for sweeps.
- **Summaries.** CV peaks are the raw maximum and minimum current of each scan (no baseline correction). ΔEp and the midpoint are estimates. Staircase sampling makes ΔEp somewhat larger than the ideal 59 mV at large steps.
- **Raw scripts.** `run_methodscript` checks literal potentials (and `store_var` literals) of `set_e`, `set_range_minmax da` and the CV/LSV/DPV/SWV/NPV/ACV/CA/PAD/EIS loops and `meas_fast_cv`/`meas_fast_ca` against `max_potential_v` (DPV pulse and 2x SWV amplitude included). Literal current ranges of `set_range ba` / `set_autoranging ba` are checked against `max_current_range_a`. Values computed inside the script are not checked. The script is aborted at `timeout_s` and a `cell_off` script is run.
- **Abort.** `abort_measurement` sends the communication-protocol `Z` command. A running technique returns the data measured so far with `aborted: true`. Afterwards (and when nothing was running) the server runs a one-line `cell_off` script, so the cell is also switched off after a raw script without `on_finished:`. If the output of a running script cannot be read (e.g. a USB glitch), the server aborts the script and switches the cell off before reporting the error; a single corrupted data line is skipped with a warning instead.
- **Saving data.** `save_path` must end in `.csv`; missing folders are created and an existing file is never overwritten (choose a new name). The path is checked before the measurement starts. Points with a `nan` value are left out (with a warning).
- **Not covered:** galvanostatic techniques, EIS, SWV/NPV/PAD, multiplexers, bipotentiostat mode, multi-channel MultiEmStat4 systems and the CRC16 protocol extension. Use `run_methodscript` for single-channel scripts beyond the built-in techniques.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
