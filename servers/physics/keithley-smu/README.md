# Keithley SourceMeter SMU — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-keithley-smu -->

Let an AI agent source voltage or current with a compliance limit, measure V and I, and run bounded IV sweeps (linear, log, dual) on **Keithley (Tektronix) SourceMeter SMUs**. Every sweep ends with the output switched off, even when something goes wrong. The server speaks all three Keithley remote dialects: 2400-series SCPI, 2450-family SCPI, and 2600B TSP.

| | |
|---|---|
| **Package** | `labmcp-keithley-smu` |
| **Instruments** | 2400, 2400-LV, 2401, 2410, 2420, 2425, 2430 (DC mode), 2440 · 2450, 2460, 2461, 2470 (SCPI command set) · 2601B, 2602B, 2604B, 2611B, 2612B, 2614B, 2634B, 2635B, 2636B |
| **Interfaces** | GPIB, USB (USBTMC), Ethernet (VXI-11 / raw socket 5025), RS-232 (2400 series), via VISA or `tcp://` |
| **Protocol** | SCPI: [2400 Series User's Manual 2400S-900-01 Rev. K §18](https://download.tek.com/manual/2400S-900-01_K-Sep2011_User.pdf), [2450 Reference Manual 2450-901-01 Rev. D §6](https://download.tek.com/manual/2450-901-01_D_May_2015_Ref.pdf). TSP: [2600B Reference Manual 2600BS-901-01 Rev. F §9](https://download.tek.com/manual/2600BS-901-01F_2600B_Reference_Aug2021.pdf) |
| **Status** | 🧪 **simulated**: tested against wire-level simulators of all three dialects, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-keithley-smu --simulate --check                                   # 2450 + 1 kΩ resistor
uvx labmcp-keithley-smu --simulate --option dialect=2600 --option sim_dut=diode --check
uvx labmcp-keithley-smu --simulate --option dialect=2400 --option sim_resistance_ohm=47 --check
```

The simulated DUT is a resistor (1 kΩ by default) or a 1N4148-like diode (Shockley equation, I_s = 2.52 nA, n = 1.752, R_s = 0.568 Ω). Compliance clamping behaves as on a real SMU.

## Connect your SMU

1. **Instrument side.**
   - **2400 series:** GPIB (default address 24) or RS-232. For RS-232 set *MENU > COMMUNICATION > RS-232*: 9600 baud, 8 data bits, no parity, **TERMINATOR LF**, no flow control.
   - **2450 / 2460 / 2461 / 2470:** use the **SCPI** command set (*MENU > System > Settings > Command Set > SCPI*, then reboot). If the instrument is in TSP mode the server stops with instructions. In `SCPI2400` emulation mode it is driven with the 2400 dialect. Above ±21 V the 2450 needs its safety **interlock** closed.
   - **2600B:** works as shipped (TSP). Leave `localnode.prompts` disabled (the default); prompts would be mixed into replies.
2. **Find the address:** `uvx labmcp ports` (or `python -m pyvisa info`). GPIB and USBTMC need a VISA library with GPIB support. NI-VISA works everywhere; pyvisa-py covers USBTMC/LAN (and GPIB with `gpib-ctypes`/linux-gpib).
3. **Test the connection:**
   ```bash
   uvx labmcp-keithley-smu --address GPIB0::24::INSTR --check
   uvx labmcp-keithley-smu --address USB0::0x05E6::0x2450::04096331::INSTR --check
   uvx labmcp-keithley-smu --address tcp://192.168.1.50:5025 --check             # 2450 / 2600B LAN
   uvx labmcp-keithley-smu --address TCPIP0::192.168.1.51::inst0::INSTR --option channel=b --check   # 2602B, SMU B
   ```
   The dialect is detected from `*IDN?` (and `*LANG?` on the 2450 family). Force it with `--option dialect=2400|2450|2600`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add smu -- uvx labmcp-keithley-smu --address GPIB0::24::INSTR --limit max_voltage_v=5 --limit max_current_a=0.02
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "smu": {
      "command": "uvx",
      "args": ["labmcp-keithley-smu", "--address", "USB0::0x05E6::0x2450::04096331::INSTR",
               "--limit", "max_voltage_v=5", "--limit", "max_current_a=0.02"]
    }
  }
}
```

Add `--read-only` to allow status and measurements but block configuration, output-on and sweeps (`output_off` stays available). For other clients, generate the snippet with `uvx labmcp config keithley-smu --address <resource> --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `configure_source` | 🎛 control | Set up the source (function, level, compliance, range, measurement speed) while the output is OFF. Measures V and I with auto-ranging. Refused if the output is on; turn it off first or use `set_source_level`. Limits are checked before anything is sent. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Identify the SMU: manufacturer, model, serial, firmware, command dialect (2400 / 2450 / 2600), channel (2600B) and the model's maximum source voltage and current. |
| `get_status` | 👁 read | Report whether the output is on, the source function (voltage/current), programmed level, compliance limit, whether the source is in compliance, terminals and 2/4-wire sense. |
| `measure` | 👁 read | Take one source-measure reading (voltage, current, V/I, power, compliance flag) of the energised DUT. Does not change any setting; refused if the output is off. |
| `output_off` | 🛑 safety | Switch the SMU output OFF immediately (and abort any IV sweep in progress). Always available, including in read-only mode. |
| `output_on` | ⚠️ hazard | Switch the SMU output ON: the configured voltage or current is applied to the DUT. The programmed level and compliance are read back from the instrument and checked against the safety limits first. Confirm with the user that the DUT is connected and safe to energise. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `run_iv_sweep` | ⚠️ hazard | Run a stepped IV sweep: configure the source, switch the output ON, step through the levels measuring V and I at each, then ALWAYS switch the output OFF (also on errors or when `output_off` is called). Returns the curve (downsampled), compliance flags, a linear fit and optional full CSV. The output must be off beforehand. Limits are checked first. |
| `set_4wire` | 🎛 control | Select 4-wire (remote sense, Kelvin) or 2-wire measurement. 4-wire removes lead resistance for low-resistance DUTs but needs the SENSE leads connected. Refused while the output is on. |
| `set_source_level` | ⚠️ hazard | Change the level of the configured source. If the output is ON the new voltage/current is applied to the DUT immediately. The level and the present compliance are checked against the safety limits first. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_voltage_v` | 20 V | Largest \|voltage\| an agent may source, or allow as voltage compliance |
| `max_current_a` | 0.1 A | Largest \|current\| an agent may source, or allow as current compliance |
| `max_power_w` | 2 W | Largest worst-case power \|source level × compliance\| |
| `max_sweep_points` | 501 | Most points in one IV sweep (the return leg of a dual sweep counts) |
| `max_sweep_duration_s` | 600 s | Longest estimated sweep duration (points × (delay + 2·NPLC/50 s + 20 ms)) |

The limits are checked **before** anything is sent: by `configure_source` and `set_source_level`, by `run_iv_sweep` (on the largest \|start\|, \|stop\|), and by `output_on`, which reads the programmed level and compliance back from the instrument. A level set on the front panel is therefore caught too. Requests beyond the model's own maximum (e.g. 40 V on a 2602B) are also refused. Override at launch: `--limit max_voltage_v=60 --limit max_power_w=10`. Tighten the defaults for fragile DUTs.

## Example prompts

- "Measure the IV curve of this LED from 0 to 3 V in 61 steps with a 20 mA compliance, then tell me the turn-on voltage at 1 mA."
- "Source 1 mA through the sample in 4-wire mode and give me its resistance; then turn the output off."
- "Run a dual sweep from −1 V to +1 V on the memristor with 1 mA compliance and 0.1 s per point, and save it to ~/data/memristor_run3.csv."
- "Do a log current sweep from 1 nA to 10 mA on the photodiode with a 2 V limit and fit the ideality factor."
- "What is the SMU doing right now? If the output is on, turn it off."

## Notes

- **Simple, verifiable commands.** Sweeps are stepped loops: set the level, wait `delay_s`, take one reading, check compliance. Built-in instrument sweeps and trigger models are not used, so every point shows up in `get_command_log`. The output is switched off in a `try/finally` at the end of every sweep. Calling `output_off` while a sweep runs sets an abort flag and switches the output off at once; the sweep then stops and reports `aborted: true`.
- **What is measured.**
  - 2400: concurrent V and I (`:SENS:FUNC:CONC ON`, `:SENS:FUNC "VOLT","CURR"`), read with `:READ?` (`:FORM:ELEM VOLT,CURR,STAT`). Compliance comes from status-word bit 3.
  - 2450: the measure function is the non-sourced quantity, source readback is on, and `:READ? "defbuffer1",SOUR,READ` returns both. Compliance comes from `:SOUR:VOLT:ILIM:TRIP?` / `:SOUR:CURR:VLIM:TRIP?`.
  - 2600B: `print(smuX.measure.iv())` and `print(smuX.source.compliance)`.

  In compliance, the reported V and I are the measured values, not the programmed level.
- **The 2400's `:MEAS?` turns the output on by itself** (manual p. 18-72), so it is never used. `measure` refuses to run when the output is off.
- **Reconfiguration happens with the output off.** `configure_source` and `set_4wire` refuse while the output is on. Use `set_source_level` (a hazard tool) to change a running source.
- **Output-off state** (normal / zero / high-Z / guard) is left as configured on the instrument. With the default *normal* state, a current source turned off becomes a 0 V source. For batteries, charged capacitors or solar cells, select **high-impedance** first (2450 manual p. 6-40).
- **The 2460/2461/2470 maxima are not built in** (their manuals weren't reviewed). The safety limits still apply, and the instrument rejects anything beyond its range. The 2430 pulse mode and the 2600B's `limitp` power compliance are not used.
- **Timing:** NPLC sets the integration time (1 PLC = 16.7 ms at 60 Hz, 20 ms at 50 Hz). Source auto-delay stays at the instrument default (on), and `delay_s` adds settling time per point.
- Simulator error replies follow the manuals' error lists (`+802 "Not permitted with OUTPUT off"`, `-222`, `-113`; 2600B `1101 "Parameter too big"`, `-285`/`-286`). The exact message wording on real firmware may differ.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
