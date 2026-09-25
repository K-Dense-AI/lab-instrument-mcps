# Rigol Oscilloscope — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-rigol-scope -->

Let an AI agent read the setup, adjust channels, timebase and trigger, run single acquisitions, take the scope's own measurements, capture calibrated waveforms and grab screenshots on **Rigol digital oscilloscopes** over SCPI.

| | |
|---|---|
| **Package** | `labmcp-rigol-scope` |
| **Instruments** | DS1000Z / MSO1000Z (DS1054Z, DS1074Z, DS1104Z, MSO1104Z, "Plus" models), DS1000Z-E (DS1102Z-E, DS1202Z-E), MSO5000 (MSO5072 … MSO5354), DHO800 / DHO900 (DHO802, DHO804, DHO914, DHO924), DHO1000 / DHO4000 (DHO1072 … DHO4804) |
| **Interfaces** | USB (USB-TMC), LAN (VXI-11/LXI, raw socket) |
| **Protocol** | SCPI per Rigol's programming guides: [MSO1000Z/DS1000Z PGA19109-1110](https://www.bitsavers.org/test_equipment/rigol/DS1000Z/PGA19109-1110_MSO1000Z_DS1000Z_Series_Digital_Oscilloscope_Programming_Guide_201807.pdf), [DS1000Z-E PGA27100-1110](https://www.batronix.com/files/Rigol/Oszilloskope/DS1000Z-E/DS1000Z-E-ProgrammingGuide.pdf), [MSO5000 PGA25104-1110](https://www.batronix.com/files/Rigol/Oszilloskope/MSO5000/MSO5000_ProgrammingGuide_EN-V2.0.pdf), [DHO800/DHO900](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO900/DHO800900_ProgrammingGuide_EN.pdf), [DHO1000/DHO4000 PGA34101-1110](https://www.batronix.com/files/Rigol/Oszilloskope/DHO1000/dho10004000_programmingguide_en.pdf) |
| **Status** | 🧪 **simulated**: tested against a wire-level SCPI simulator (including binary waveform and screenshot blocks), not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-rigol-scope --simulate --check
uvx labmcp-rigol-scope --simulate --option sim_model=DHO804 --check    # also DS1202Z-E, MSO5074, DHO1204 ...
```

The simulated scope has a 1 kHz, 0-3 V square wave on CH1 and a 2 kHz, 1 V sine on CH2 (CH3/CH4 grounded). It answers `:WAVeform:DATA?` and `:DISPlay:DATA?` with real IEEE 488.2 binary blocks, keeps an SCPI error queue, and its trigger only fires when the level is inside the signal, so you can try `single` with a bad trigger level.

## Connect your oscilloscope

1. **USB:** plug in the USB device port. The server uses PyVISA with the pure-Python `pyvisa-py` backend, which needs `libusb` on the system (`brew install libusb`, `apt install libusb-1.0-0`; on Windows use NI-VISA with `?backend=@ivi`). Find the resource with `python -m pyvisa info` or `uvx labmcp ports`.
2. **LAN:** in the scope's *Utility* I/O / LAN settings enable DHCP or set a static IP, and note the address.
3. **Test the connection:**
   ```bash
   uvx labmcp-rigol-scope --address USB0::0x1AB1::0x04CE::DS1ZA123456789::INSTR --check   # USB-TMC
   uvx labmcp-rigol-scope --address TCPIP0::192.168.1.50::INSTR --check                   # LAN (VXI-11)
   uvx labmcp-rigol-scope --address tcp://192.168.1.50:5555 --check                        # LAN raw socket
   ```
   `--check` prints the `*IDN?` identity and the detected command family.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add scope -- uvx labmcp-rigol-scope --address TCPIP0::192.168.1.50::INSTR
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "scope": {
      "command": "uvx",
      "args": ["labmcp-rigol-scope", "--address", "TCPIP0::192.168.1.50::INSTR"]
    }
  }
}
```

Add `--read-only` to allow reading settings, measurements, waveforms and screenshots while blocking every tool that changes the scope setup. For other clients, generate the snippet with `uvx labmcp config rigol-oscilloscope --address TCPIP0::192.168.1.50::INSTR --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `autoscale` | 🎛 control | Run the scope's automatic setup (AUTO key): it picks vertical scales, timebase and trigger for the connected signals. This overwrites the user's current setup - only use it when asked. Needs signals of roughly >20 mVpp and >40 Hz. |
| `capture_waveform` | 👁 read | Capture a channel's waveform, scaled to volts and seconds with the scope's waveform preamble, and return statistics plus a downsampled trace; optionally save all points to CSV. |
| `force_trigger` | 🎛 control | Force one trigger (FORCE key). Only has an effect in NORMal or SINGle sweep while waiting. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Identify the oscilloscope (model, serial, firmware), the command family used for it, the number of analog channels, and the current sample rate and memory depth. |
| `get_settings` | 👁 read | Read the current vertical settings of every channel (on/off, V/div, offset, coupling, probe ratio, bandwidth limit), the timebase, the trigger (type, sweep, status, edge source/level/slope) and the acquisition sample rate / memory depth. |
| `measure` | 👁 read | Read the scope's automatic measurements for one channel: voltages (Vpp, Vmax, Vmin, Vtop, Vbase, Vamp, Vavg, Vrms, overshoot, preshoot) and timing (period, frequency, rise/fall time, +/- width, +/- duty). Values the scope cannot determine are null. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `run` | 🎛 control | Start continuous acquisition (RUN). |
| `screenshot` | 👁 read | Save a screenshot of the oscilloscope display (PNG; BMP on the MSO5000) and return its path. For PNG screenshots the image is also returned so the model can look at the screen. |
| `set_channel` | 🎛 control | Change a channel's vertical settings; unspecified settings are left alone. The probe ratio is applied first because it changes the valid scale range. Returns the settings the scope actually applied (the scale snaps to 1-2-5 steps). Rejected values are reported from the scope's error queue. |
| `set_timebase` | 🎛 control | Set the main timebase scale and/or offset. Returns the applied values. |
| `set_trigger` | 🎛 control | Configure an edge trigger (source, level, slope) and the sweep mode. Sets the trigger type to EDGE. The level must lie within the source channel's screen range. |
| `single` | 🎛 control | Arm a single acquisition (SINGLE key): the scope triggers once, then stops. With wait_s > 0 it waits until the acquisition is complete (status STOP). If the trigger condition is never met the status stays WAIT - check the trigger level/source or use `force_trigger`. |
| `stop` | 🎛 control | Stop acquisition (STOP) and freeze the current waveforms, e.g. before reading deep memory. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. The command log shows every SCPI command sent.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_memory_points` | 1 200 000 points | Most points read from acquisition memory in one capture (bounds transfer time) |

Override at launch, e.g. `--limit max_memory_points=24000000`. An oscilloscope does not source energy, so there are no hazard tools; this server does not control the built-in waveform generator some models have.

## Example prompts

- "What is on the scope right now? Give me the channel settings, timebase and trigger."
- "Measure the frequency, Vpp and rise time on CH1."
- "Set CH2 to 200 mV/div, AC coupling, trigger on CH2 falling edge at 0 V, then take a single shot and save the full memory record to ~/data/burst.csv."
- "Capture the CH1 waveform from the screen and tell me the duty cycle and whether the trace is clipped."
- "Take a screenshot of the scope so I can put it in my lab notebook."

## Notes

- **One driver, three command families.** Commands common to all guides are used as written (long forms, e.g. `:TRIGger:EDGE:LEVel`, `:MEASure:ITEM? VPP,CHANnel1`, `:WAVeform:PREamble?`). The differences are taken from each guide: autoscale is `:AUToscale` (DS1000Z, MSO5000) or `:AUToset` (DHO); screenshots are `:DISPlay:DATA? ON,OFF,PNG` (DS1000Z), `:DISPlay:DATA? PNG` (DHO) or `:DISPlay:DATA?` → BMP (MSO5000); screen captures return 1200 points (DS1000Z, 12 divisions) or 1000 (MSO5000/DHO, 10 divisions). The family is detected from `*IDN?`; unknown Rigol models are refused unless you force `--option profile=DS1000Z|MSO5000|DHO`.
- **Waveform scaling** always uses `:WAVeform:PREamble?` and BYTE format: volts = (byte − YORigin − YREFerence) × YINCrement. WORD format is not used because the DHO guides do not document its byte order. For the 12-bit DHO scopes this means 8-bit screen/memory data.
- **Deep memory** (`mode="memory"`) follows the documented RAW procedure: stop the scope, then read in batches of 250 000 points with `:WAVeform:STARt/STOP` (the DS1000Z maximum for BYTE). The MSO5000 and DHO guides document batched reads but not a maximum; the server checks every batch length and fails clearly if a batch comes back short. *Not yet verified on MSO5000/DHO hardware.*
- **Measurements** return `null` when the scope answers 9.9E37 (cannot measure). Duty cycle and overshoot are reported exactly as the scope returns them (the DS1000Z guide defines duty as a ratio).
- **Setting commands are checked** by reading `:SYSTem:ERRor?` afterwards, so an out-of-range scale, offset or trigger level is reported instead of silently ignored. The scale snaps to 1-2-5 steps unless fine adjustment (VERNier) is on.
- **LAN:** the programming guides describe LAN access through VISA (VXI-11/LXI, `TCPIP0::<ip>::INSTR`). Rigol scopes also accept raw SCPI on TCP port 5555 (`tcp://<ip>:5555`); that port is widely used but not stated in these guides.
- **USB** additionally needs `libusb` for `pyvisa-py`; `pyusb` is installed with this package.
- Screenshots are returned as MCP image content only for PNG; large BMP screenshots (MSO5000) are saved to disk and the path is returned.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
