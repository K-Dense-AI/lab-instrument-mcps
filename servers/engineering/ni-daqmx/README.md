# NI-DAQmx DAQ Devices — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ni-daqmx -->

Let an AI agent acquire voltages and thermocouple temperatures, read digital lines and (with your permission) set analog and digital outputs on **National Instruments DAQ hardware** through the official **NI-DAQmx** driver and its Python API.

| | |
|---|---|
| **Package** | `labmcp-ni-daqmx` |
| **Instruments** | NI-DAQmx devices: USB-6001/6002/6003, USB-6008/6009, USB-6211/6212/6218, USB/PCIe M and X Series (62xx/63xx), CompactDAQ C Series modules (e.g. NI 9201, 9211, 9263, 9401), NI MAX simulated devices |
| **Interfaces** | USB, PCI/PCIe, PXI, Ethernet (cDAQ chassis) |
| **Protocol** | NI-DAQmx via the `nidaqmx` Python API ([API docs](https://nidaqmx-python.readthedocs.io/), [source](https://github.com/ni/nidaqmx-python), [NI-DAQmx driver](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html)) |
| **Status** | 🧪 **simulated**: tested against a simulated `nidaqmx` backend, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-ni-daqmx --simulate --check
```

The built-in simulator is a USB-6341-like X Series device `Dev1`: a 1 V / 50 Hz sine on ai0, a 2.5 V sensor on ai1, ao0/ao1 looped back to ai2/ai3, a ~37 °C thermocouple on ai4, and an external "interlock" holding port1/line0 high. It reproduces NI-DAQmx behaviour such as error codes (e.g. -200170 channel does not exist, -200576 no built-in CJC) and the shapes `task.read()` returns.

**Testing the real NI-DAQmx path without hardware:** in NI MAX (Windows) choose *Devices and Interfaces → Create New… → Simulated NI-DAQmx Device*, then run the server *without* `--simulate` against that device, e.g. `--address SimDev1`. NI MAX simulated devices return synthetic data, and `get_connection_info` reports `ni_max_simulated_device: True`. On Linux, simulated-device support is limited (NI's configuration tools can import a configuration file that defines one, with mixed results); use `--simulate` there.

## Connect your instrument

1. **Install NI-DAQmx** (Windows or Linux; macOS is not supported by NI-DAQmx) from [ni.com](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html). The `nidaqmx` Python package is installed with this server; `python -m nidaqmx installdriver` can also install the driver.
2. **Find the device name** in NI MAX (*Devices and Interfaces*) or with `nilsdev` on Linux, e.g. `Dev1` or `cDAQ1Mod1`. Close NI MAX test panels before connecting.
3. **Test the connection:**
   ```bash
   uvx labmcp-ni-daqmx --check                    # works if exactly one device is present
   uvx labmcp-ni-daqmx --address Dev1 --check
   ```
   `--address` is the NI-DAQmx device name. It is optional when exactly one device exists.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add ni-daq -- uvx labmcp-ni-daqmx --address Dev1
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "ni-daq": {
      "command": "uvx",
      "args": ["labmcp-ni-daqmx", "--address", "Dev1"]
    }
  }
}
```

Add `--read-only` to allow acquisitions but block the output tools (`set_outputs_safe` stays available). Add `--option safe_do_lines=port0/line0:3` to make `set_outputs_safe` always drive those lines low. For other clients, generate the snippet with `uvx labmcp config ni-daqmx --address Dev1 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Describe the connected device: analog input/output channels, digital lines, supported voltage ranges, maximum sample rates, and the outputs this server has set. Call this before choosing channels, ranges and rates. |
| `list_devices` | 👁 read | List every NI-DAQmx device the driver can see (product type, serial number, channels, whether it is an NI MAX simulated device) and which one this server is connected to. |
| `read_analog` | 👁 read | Measure voltage on one or more analog inputs: a single on-demand reading, or a finite hardware-timed acquisition of `samples` per channel at `rate_hz`. Returns per-channel statistics and a downsampled waveform; optionally saves all data to CSV. |
| `read_digital_lines` | 👁 read | Read the logic level of digital lines. |
| `read_thermocouple` | 👁 read | Measure temperature (°C) with thermocouples (types B, E, J, K, N, R, S, T) on a thermocouple-capable device, e.g. an NI 9211/9212/9213/9214 module or USB-TC01. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_outputs_safe` | 🛑 safety | Put the device's outputs in a safe state: every analog output to 0 V (or the bottom of its range if 0 V is not in it) and, by default, every digital line this server drove - plus the `safe_do_lines` option lines - driven low. Use when finished or if anything looks wrong. |
| `write_analog` | ⚠️ hazard | Set an analog output to a DC voltage (static, software-timed). The output keeps this value after the call on most NI devices, until changed or `set_outputs_safe` is called. |
| `write_digital_lines` | ⚠️ hazard | Drive digital output lines high or low. The lines keep their level after the call until changed or `set_outputs_safe` is called. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. The command log records every task the server ran (channels, ranges, rates, values written).

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_ao_voltage_v` | 5 V | Largest \|voltage\| an agent may put on an analog output |
| `max_samples` | 100 000 | Most samples per channel in one acquisition |
| `max_rate_hz` | 100 000 Hz | Highest sample clock rate per channel |
| `max_acquisition_s` | 60 s | Longest finite acquisition (samples / rate) |

Override at launch, e.g. `--limit max_ao_voltage_v=10 --limit max_acquisition_s=300`. Limits are checked before anything is sent. The device's own AO range is also enforced (e.g. USB-6008/6009 outputs 0-5 V only), as are its maximum sample rates.

## Example prompts

- "List the NI devices and tell me which inputs Dev1 has and their voltage ranges."
- "Acquire ai0 and ai1 differentially at 10 kHz for 0.5 s on the ±1 V range and give me the RMS noise of each; save the data to ~/daq/noise.csv."
- "Read the type K thermocouples on cDAQ1Mod1/ai0:3 once a second for 30 seconds using the built-in CJC and report the average of each."
- "Set ao0 to 2.0 V, confirm it on the ai2 loopback, then set it back to 0."
- "Is the interlock on port1/line0 closed? Then switch port0/line2 high to open the valve."
- "We're done: put every output in a safe state."

## Notes

- **Every tool call is one short DAQmx task** (create channels, configure timing, read or write, close), as in NI's examples, so the server never holds the device reserved between calls. Another program using the device at the same time causes a clear "resource reserved" error (-50103). Output and digital-line tasks are serialised separately from acquisitions, so `set_outputs_safe` (and `write_analog`) never wait for a running acquisition to finish: NI-DAQmx runs an analog-input task and AO/DIO tasks on the same device side by side.
- **Acquisitions** are capped at 600 s per tool call even if `max_acquisition_s` is raised. The returned waveform is downsampled by keeping each block's minimum and maximum, so short spikes are not lost. `save_path` must be a new `.csv` file: it is checked before acquiring, and an existing file is never overwritten.
- **Outputs hold their value after the task closes** on most NI devices, which is what the output tools rely on. Some devices have configurable idle/power-up output behaviour; check your device if it resets outputs.
- **Reading a digital line can change it.** A DAQmx digital-input task configures the line as an input. The server therefore reports lines it is driving from their last commanded level instead of reading them, and documents the risk for lines driven by other software.
- **Terminal configuration matters.** `default` lets NI-DAQmx choose (often differential where supported). A wrong choice gives offsets or noise rather than an error.
- **Thermocouples** use `add_ai_thrmcpl_chan` with built-in or constant cold-junction compensation. A CJC *channel* (`SCANNABLE_CHANNEL`) is not offered in this version because its correct use depends on the device.
- **Rates:** multiplexed devices share `ai_max_multi_channel_rate_hz` across channels, and NI-DAQmx coerces the sample clock to a rate the timebase can produce. The actual rate is returned in `sample_rate_hz`.
- NI-DAQmx warnings (e.g. about coerced values) are returned in each acquisition's `warnings` list.

## Hardware verification

| Model | NI-DAQmx version | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
