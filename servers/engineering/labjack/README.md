# LabJack T-series DAQ — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-labjack -->

Let an AI agent read analog inputs, stream waveforms, read thermocouples, check digital lines and (with your permission) set DACs and digital outputs on **LabJack T4, T7/T7-Pro and T8** DAQ devices through LabJack's official **LJM** library.

| | |
|---|---|
| **Package** | `labmcp-labjack` |
| **Instruments** | LabJack T4, T7, T7-Pro, T8 (and their OEM versions) |
| **Interfaces** | USB, Ethernet, WiFi (T7-Pro) |
| **Protocol** | LJM library + T-series Modbus register map ([T-Series Datasheet](https://support.labjack.com/docs/t-series-datasheet), [register map `ljm_constants.json`](https://github.com/labjack/ljm_constants), [`labjack-ljm` Python wrapper](https://github.com/labjack/labjack-ljm-python)) |
| **Status** | 🧪 **simulated**: tested against a simulated LJM backend, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-labjack --simulate --check
uvx labmcp-labjack --simulate --option sim_model=T8 --check     # T4, T7, T7-Pro (default) or T8
```

The simulator wires a 2 V / 10 Hz sine to AIN0, a thermistor divider (~2.5 V) to AIN1, DAC0 back into AIN2, an LM34 cold-junction sensor to AIN3, and a ~37 °C thermocouple to AIN0-AIN3 when the thermocouple feature is used. Other inputs are grounded.

## Connect your LabJack

1. **Install LJM** (the native library and USB driver) from [labjack.com/support/software/installers/ljm](https://labjack.com/support/software/installers/ljm). The `labjack-ljm` Python package is installed automatically with this server, but it needs that native library.
2. **Close other LabJack software** (Kipling, LJLogM, LJStreamM). A device can only be claimed by one process at a time over USB.
3. **Test the connection:**
   ```bash
   uvx labmcp-labjack --check                                  # first LabJack found
   uvx labmcp-labjack --address 470012345 --check              # by serial number
   uvx labmcp-labjack --address 192.168.1.207 --option connection_type=ETHERNET --check
   uvx labmcp-labjack --option device_type=T4 --check          # only open a T4
   ```
   `--address` is the LJM identifier: a serial number, an IP address, a device name, or `ANY` (the default).

## Add to your MCP client

**Claude Code**
```bash
claude mcp add labjack -- uvx labmcp-labjack --address 470012345
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "labjack": {
      "command": "uvx",
      "args": ["labmcp-labjack", "--address", "470012345"]
    }
  }
}
```

Add `--read-only` to allow measurements but block the DAC and digital-output tools (`set_outputs_safe` stays available). Add `--option safe_dio=FIO0,FIO1` to make `set_outputs_safe` always release those lines, even if another program drove them. For other clients, generate the snippet with `uvx labmcp config labjack --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Identify the connected LabJack (model, serial, firmware, connection) and list what it offers: analog inputs, valid input ranges, resolution indices, DAC range, digital lines and stream rate. Call this before configuring channels. |
| `read_analog_inputs` | 👁 read | Read one or more analog inputs once (command-response) and return volts. |
| `read_device_temperature` | 👁 read | Read the LabJack's internal temperature sensor (°C, about ±2 °C) and the estimated ambient air temperature. On the T8 also returns the sensor next to each AIN terminal. Cannot be read while a stream is running. |
| `read_digital_inputs` | 👁 read | Report the logic level and direction (input/output/analog) of digital I/O lines. |
| `read_thermocouple` | 👁 read | Read a thermocouple with the T7/T8 AIN thermocouple extended feature (types B, C, E, J, K, N, R, S, T) and return °C with the measured voltage and cold-junction temperature. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_digital_output` | ⚠️ hazard | Make a digital line an output and drive it high (3.3 V) or low (0 V). |
| `set_outputs_safe` | 🛑 safety | Put the outputs in a safe state: stop any stream, set DAC0 and DAC1 to 0 V, and release every digital line this server drove (plus the `safe_dio` option lines) to input - or drive them low with dio_mode='low'. Use when finished or if anything looks wrong. |
| `stream_analog` | 👁 read | Acquire a hardware-timed waveform on one or more analog inputs (LJM stream mode) and return per-channel statistics plus a downsampled waveform; optionally save everything to CSV. |
| `write_dac` | ⚠️ hazard | Set an analog output (DAC0/DAC1) to a DC voltage. The output stays at this value until changed or `set_outputs_safe` is called. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. The command log records every LJM register read and write.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_dac_voltage_v` | 5 V | Highest DAC voltage an agent may set (hardware: T4/T7 0-5 V, T8 0-10 V) |
| `max_stream_duration_s` | 60 s | Longest hardware-timed stream |
| `max_scan_rate_hz` | 10 000 Hz | Highest stream scan rate (the device maximum still applies) |

Override at launch, e.g. `--limit max_dac_voltage_v=2.5`. Limits are checked before anything is sent. Streams are also capped at 2 million samples in total.

## Example prompts

- "Read AIN0-AIN3 on the ±1 V range and tell me which channels look like they are floating."
- "Stream AIN0 and AIN1 at 5 kHz for 2 seconds, give me the RMS and peak-to-peak of each, and save the data to ~/data/run12.csv."
- "Read the type K thermocouple on AIN2 every 10 seconds for a minute and tell me whether it is still warming up."
- "Set DAC0 to 1.25 V, then check the loopback on AIN2 and report the error in millivolts."
- "Which digital lines are outputs right now, and what level are they at?"
- "We're done — put all the outputs back in a safe state."

## Notes

- **Model differences are enforced.** Valid input ranges (`AIN#_RANGE`) and resolution indices come from LabJack's register map: T7 ±10/±1/±0.1/±0.01 V and index 0-8 (T7-Pro 0-12, but 9-12 cannot stream); T8 ±11 … ±0.018 V and index 0-16 (one index shared by all inputs); T4 fixed ranges (AIN0-3 ±10 V, AIN4-11 0-2.5 V) and index 0-5. DAC range: T4/T7 0-5 V, T8 0-10 V.
- **Settings persist.** Range, resolution and differential settings made by `read_analog_inputs` stay on the device until changed or power-cycled. Omitted settings are left as they are; every reading reports what it was measured against (`GND`, the T7 negative input, or the T8's isolated terminal).
- **Reading digital lines never changes their direction.** `read_digital_inputs` uses the `DIO_STATE`/`DIO_DIRECTION` bitmasks. (Reading a single-line register such as `FIO3` would switch that line to input.)
- **T4 flexible I/O.** AIN4-AIN11 share terminals with FIO4-EIO3. The server refuses to read those AINs while the line is a digital output, because the read would switch it to analog and release whatever it drives.
- **`set_outputs_safe` default is `input`**, the T-series power-up state (input with pull-up). If an attached relay board treats a pulled-up input as "on", use `dio_mode="low"` instead.
- **Thermocouples** use the AIN extended feature (`AIN#_EF_INDEX` 20-30). With `cjc="internal"` the T7 uses `TEMPERATURE_DEVICE_K` and the T8 uses each channel's own terminal sensor; `cjc="lm34"` uses LabJack's LM34 conversion (55.56 K/V, 255.37 K). An open input returns `valid=false` (the device reports -9999).
- **Streams** follow LabJack's `stream_basic.py` example (internal clock, no trigger, auto settling). While a stream runs, command-response analog reads are blocked by the device. Skipped scans (buffer overflow) are reported and left empty.
- **Importing the LJM wrapper prints to stdout** if the native library is missing. The server redirects that import so the MCP stdio channel is never corrupted, and reports a clear install hint instead.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
