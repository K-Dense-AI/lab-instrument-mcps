# SRS Lock-in Amplifier — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-srs-lockin -->

Let an AI agent read X/Y/R/θ, set the reference frequency, harmonic and phase, drive level, sensitivity and time constant, run auto phase/gain, and record bounded frequency sweeps on **Stanford Research Systems DSP lock-in amplifiers**, both the classic **SR810/SR830** and the current **SR860/SR865A**.

| | |
|---|---|
| **Package** | `labmcp-srs-lockin` |
| **Instruments** | SR830, SR810 (SR8x0 command set); SR860, SR865A (SR86x command set) |
| **Interfaces** | GPIB (VISA), RS-232; SR86x also USB (USBTMC) and Ethernet (VXI-11) |
| **Protocol** | SRS ASCII remote commands, IEEE-488.2 style ([SR830 manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf) ch. 5, [SR810 manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR810m.pdf), [SR860 manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR860m.pdf) ch. 4, [SR865A manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR865Am.pdf)) |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator of both command sets, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-srs-lockin --simulate --check                       # SR830 simulator
uvx labmcp-srs-lockin --simulate --option model=sr860 --check  # SR860 simulator
```

The simulated sample is a resonator (10 kHz, Q = 50) driven by the sine output, so a `frequency_sweep` from 9 to 11 kHz shows a resonance peak.

## Connect your lock-in

The model is detected from `*IDN?` (override with `--option model=sr830|sr810|sr860|sr865a`).

1. **GPIB (all models):** set the GPIB address in the setup menu (SR830 factory default 8). Install `labmcp[visa]` (included) and a VISA backend (pyvisa-py works with most USB-GPIB adapters; NI-VISA with `?backend=@ivi`).
2. **RS-232:** set baud rate and parity on the instrument (SR830 default 9600 baud, no parity; the word length is always 8 bits). The SR830 only answers on the interface selected by `OUTX`; the server sends `OUTX 0` automatically on serial connections and `OUTX 1` on GPIB. The SR830 ends RS-232 replies with CR. For an **SR86x on RS-232**, pass `--option model=sr860` (replies end with LF) or set the SR86x transmit terminator to CR.
3. **SR86x USB / Ethernet:** use the USBTMC or VXI-11 VISA resource (`TCPIP0::<ip>::inst0::INSTR`; enable VXI-11 and add your PC to the trusted IP range in the system menu).
4. **Test the connection:**
   ```bash
   uvx labmcp-srs-lockin --address GPIB0::8::INSTR --check
   uvx labmcp-srs-lockin --address /dev/ttyUSB0 --check                      # SR830 RS-232
   uvx labmcp-srs-lockin --address "serial://COM3?baudrate=19200" --check    # non-default baud
   uvx labmcp-srs-lockin --address TCPIP0::192.168.1.20::inst0::INSTR --check  # SR86x LAN
   ```
   Use `--option interface=gpib` if you reach an SR830's GPIB port through a raw TCP adapter.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add lockin -- uvx labmcp-srs-lockin --address GPIB0::8::INSTR
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "lockin": {
      "command": "uvx",
      "args": ["labmcp-srs-lockin", "--address", "GPIB0::8::INSTR"]
    }
  }
}
```

Add `--read-only` to allow reading outputs and settings but block every setting change and the sine-output drive. For other clients, generate the snippet with `uvx labmcp config srs-lockin --address GPIB0::8::INSTR --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `auto_gain` | 🎛 control | Pick the sensitivity automatically for the present signal (SR830 AGAN / SR86x ASCL). On the SR830 AGAN does nothing when the time constant is longer than 1 s. |
| `auto_phase` | 🎛 control | Run Auto Phase (APHS): shift the reference phase so that Y ≈ 0 and X ≈ R. The outputs then need several time constants to settle. Does nothing if the phase is unstable. |
| `auto_range` | 🎛 control | Optimise the input stage for the present signal: SR830 Auto Reserve (ARSV) or SR86x Auto Range of the voltage input range (ARNG). |
| `frequency_sweep` | ⚠️ hazard | Step the internal reference (and SINE OUT drive) from start_hz to stop_hz, waiting for the output filter to settle at each point, and record X/Y/R/θ vs frequency - e.g. a resonance or transfer-function measurement. The drive amplitude stays at its present value. Needs the internal reference. The estimated duration must be within `max_sweep_duration_s`; `set_amplitude_minimum` stops a running sweep. `save_path` must be a new .csv file. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_settings` | 👁 read | Report the reference (source, frequency, harmonic, phase), sine output, sensitivity, time constant/slope (with the 99 % settling time), and input configuration. |
| `read_outputs` | 👁 read | Read X, Y, R and θ as one coherent snapshot (SNAP?), with the reference frequency, sensitivity, fraction of full scale and any overloads latched since the last reading. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_amplitude` | ⚠️ hazard | Set the SINE OUT amplitude (V rms), which drives the sample or excitation circuit. Refused above the `max_amplitude_v` safety limit. Tell the user the new drive level first. |
| `set_amplitude_minimum` | 🛑 safety | Turn the sine output down to the instrument minimum (the lock-in's closest thing to 'output off') and stop any running frequency sweep. SR810/SR830: 4 mV rms (it cannot go to zero - disconnect the cable if the sample must see no drive). SR86x: 1 nV rms and DC level 0 V. |
| `set_input` | 🎛 control | Configure the signal input: A / A-B / current, AC or DC coupling, shield float/ground and (SR86x) the voltage input range. Use DC coupling below ~160 mHz. Omitted parameters are left unchanged. |
| `set_reference` | 🎛 control | Set the reference: source (internal/external), internal frequency, detection harmonic and phase shift. Omitted parameters are left unchanged. Changing the frequency also changes the frequency of the SINE OUT drive (at the current amplitude). |
| `set_sensitivity` | 🎛 control | Set the sensitivity (full-scale range). The smallest available range that is at least `full_scale` is chosen, so a signal of that size does not overload. On SR810/SR830 the dynamic reserve can be set at the same time. |
| `set_time_constant` | 🎛 control | Set the output filter time constant (nearest available value), and optionally the filter slope and synchronous filter. The SR830 may raise a too-short time constant to its minimum for the current slope/reserve; the returned settings show the value actually used. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_amplitude_v` | 1.0 V | Largest sine-output amplitude (V rms) `set_amplitude` may set; a sweep also refuses to run if the present amplitude is above it |
| `max_sweep_duration_s` | 600 s | Longest estimated frequency sweep (points × settle time) an agent may start |

Override at launch: `--limit max_amplitude_v=0.1 --limit max_sweep_duration_s=1800`.

## Example prompts

- "Set the lock-in to 13.7 Hz internal reference, 100 mV drive, 1 s time constant at 24 dB/oct, and tell me X and Y once it has settled."
- "Auto-phase, then read R and θ ten times and give me the mean and standard deviation of R."
- "Sweep the drive from 30 to 34 kHz in 201 log-spaced steps at 10 mV and find the resonance frequency and Q of my tuning fork."
- "Switch to the current input with 100 MΩ gain, DC coupling, and pick the sensitivity automatically."
- "We're done - turn the sine output down to the minimum."

## Notes

- **The sine output is the hazard.** It drives your sample (heating, electromigration, piezo stress). `set_amplitude` checks `max_amplitude_v`; `set_amplitude_minimum` is the closest thing to "output off": the SR810/SR830 cannot go below 4 mV rms, so disconnect the cable if the sample must see no drive. On the SR86x it sets 1 nV rms and a 0 V DC level (`SOFF`). The SR830 powers up and resets (`*RST`) to **1.000 V rms**.
- Every set command is followed by `*ESR?`; if the lock-in flags an execution error (parameter out of range, e.g. `FREQ` in external-reference mode) or an illegal command, the tool reports it instead of silently continuing.
- `set_sensitivity` picks the smallest range that is **at least** the requested full scale (never a smaller one, which would overload). `set_time_constant` picks the nearest available value; the SR830 may raise it to the minimum allowed for the slope/reserve, and the returned settings show the value actually used.
- `read_outputs` returns `unit` = V for voltage inputs and A for the current input (1 V ↔ 1 µA sensitivity mapping from the manuals). The SR830 manual (SR530-compatibility `QX`/`QY` commands, ch. 5) states that outputs are returned in amps when the current input is selected; the SR86x manual does not say explicitly — please report what your instrument returns.
- `overloads` comes from `LIAS?`, whose bits are latched since the last read; the first reading after a transient may show an overload that has already cleared.
- `frequency_sweep` waits the manual's 99 % settling time at each point (5/7/9/10 time constants for 6/12/18/24 dB/oct) unless `settle_time_constants` is given, and restores the starting frequency afterwards. Calling `set_amplitude_minimum` stops a running sweep at the next point (also one that is still reading its settings). One sweep can take at most 3640 s (the tool's timeout minus a margin) whatever `max_sweep_duration_s` is: longer estimates are refused, and a sweep running slower than estimated stops early and returns the points measured so far (`completed: false`). `save_path` must end in `.csv` and must not exist yet (an existing file is never overwritten); it is checked before the sweep starts.
- `set_amplitude_minimum` tries both the amplitude and (SR86x) the DC level even if one of them fails, then reports every failure.
- `auto_gain` maps to `AGAN` (SR830) or `ASCL` (SR86x); `auto_range` maps to `ARSV` (SR830 auto reserve) or `ARNG` (SR86x auto input range). SR830 `AGAN` does nothing when the time constant is above 1 s.
- Data-buffer, streaming and FFT functions are not exposed. The SR850, SR844 (RF) and SR865 (non-A) are not included because they were not verified; an explicit `--option model=...` lets you try a compatible model at your own risk.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
