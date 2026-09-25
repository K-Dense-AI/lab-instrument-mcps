# Thorlabs Optical Power Meter — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-thorlabs-pm -->

Let an AI agent read laser power, log stability and warm-up drift, set the wavelength correction, averaging and range, and dark-zero the sensor on **Thorlabs optical power meters** through their documented **SCPI** command set over USB (USBTMC / VISA).

| | |
|---|---|
| **Package** | `labmcp-thorlabs-pm` |
| **Instruments** | PM100D, PM100A, PM100USB, PM400 consoles with C-series photodiode and thermal sensors. PM101 and PM5020 use the same documented commands but are **unverified** (see Notes) |
| **Interfaces** | USB (USBTMC, via VISA) |
| **Protocol** | SCPI: [PM100D Operation Manual 17654-D02 §6.4](https://www.physics.utoronto.ca/~phy326/opt/PM100D-Manual.pdf), [PM100USB Operation Manual 19570-D02 §6.3](https://dcc.ligo.org/public/0126/T1600205/001/19570-D02%20%20PM100USB%20User%20Manual%20Rev%20C.pdf), PM400 Operating Manual v1.1 §5.5, and Thorlabs' [per-model SCPI command references](https://github.com/Thorlabs/Light_Analysis_Examples/tree/main/Python/Thorlabs%20PMxxx%20Power%20Meters/SCPI/commandDocu) |
| **Status** | 🧪 **simulated**: tested against a wire-level SCPI simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-thorlabs-pm --simulate --check
uvx labmcp-thorlabs-pm --simulate --option sim_sensor=S302C --check   # thermal sensor head
```

The simulator is a PM100D with an S120C photodiode (or S302C thermopile) looking at a 1.234 mW, 632.8 nm HeNe laser. Setting the wrong wavelength on the photodiode gives a proportionally wrong reading, as on the real bench.

## Connect your power meter

1. **Driver.** Thorlabs meters enumerate as USB Test & Measurement Class (USBTMC) devices, vendor ID `0x1313`.
   - **Windows:** the Thorlabs driver (TLPM / Thorlabs VISA) is *not* visible to PyVISA. Install the free **NI-VISA runtime**, then switch the meter to the NI-VISA driver with the **Thorlabs Driver Switcher** (installed with the Thorlabs Optical Power Monitor software), and start the server with `?backend=@ivi` (see below). This is the route Thorlabs documents for PyVISA.
   - **Linux:** pyvisa-py talks USBTMC directly (installed with this package). Give your user access to the device, e.g. `/etc/udev/rules.d/99-thorlabs-pm.rules`:
     ```
     SUBSYSTEM=="usb", ATTR{idVendor}=="1313", MODE="0666"
     ```
     then `sudo udevadm control --reload-rules` and re-plug the meter. NI-VISA also works (`?backend=@ivi`).
   - **macOS:** pyvisa-py + libusb (`brew install libusb`); NI-VISA is an alternative.
2. **Find the resource string:** `uvx labmcp ports` (or `python -m pyvisa info`). It looks like `USB0::0x1313::0x8078::P0012345::INSTR` (the product ID depends on the model: PM100D `0x8078`, PM100USB `0x8072`).
3. **Test the connection:**
   ```bash
   uvx labmcp-thorlabs-pm --address USB0::0x1313::0x8078::P0012345::INSTR --check
   uvx labmcp-thorlabs-pm --address "visa://USB0::0x1313::0x8078::P0012345::INSTR?backend=@ivi" --check   # NI-VISA
   ```
   For the second input of a PM5020 add `--option channel=2`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add power-meter -- uvx labmcp-thorlabs-pm --address USB0::0x1313::0x8078::P0012345::INSTR
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "power-meter": {
      "command": "uvx",
      "args": ["labmcp-thorlabs-pm", "--address", "USB0::0x1313::0x8078::P0012345::INSTR"]
    }
  }
}
```

Add `--read-only` to allow readings but block wavelength, averaging, range and zero changes. For other clients, generate the snippet with `uvx labmcp config thorlabs-pm --address <resource> --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Identify the meter console and the attached sensor head (model, serial, type, wavelength range, power ranges), and report the present wavelength, averaging, range and zero value. |
| `log_power_series` | 👁 read | Log a series of power readings to characterise laser stability, warm-up or drift. Returns mean, stdev, min/max, RMS and peak-to-peak stability (%) and drift (%/min), plus the readings (downsampled to `max_points`; use `save_path` to keep all of them). |
| `read_power` | 👁 read | Read the optical power once (in W, plus dBm and a formatted string). The reading uses the configured correction wavelength and averaging; set the wavelength first. |
| `read_sensor_temperature` | 👁 read | Read the sensor head temperature in °C (thermal sensors and other heads with a built-in temperature sensor only). |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_averaging` | 🎛 control | Set how many samples the meter averages for each reading. More averaging lowers noise but slows each reading (PM100 series: ~3 ms per sample; PM400/PM101/PM5020: 1 ms). |
| `set_range` | 🎛 control | Select auto-ranging, or a fixed power range that fits `range_w` (the meter picks the most sensitive range that can hold that power). A fixed range reports over-range if exceeded. |
| `set_wavelength` | 🎛 control | Set the correction wavelength (nm) the meter uses to convert sensor signal to power. Must be within the sensor's calibrated range (see `get_device_info`); sent as a whole number of nm. |
| `zero_sensor` | 🎛 control | Dark-zero the sensor (removes dark current / thermal offset). BEFORE calling, ask the user to block the beam and cover the sensor aperture completely, and wait for them to confirm: zeroing with light on the sensor corrupts all later readings. Takes a few seconds. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

A power meter only measures, so this server has no hazard tools. Your laser is the hazard: wear the right eyewear and never exceed the sensor's rated power or power density.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_series_duration_s` | 600 s | Longest power-logging series an agent may start |

Override at launch: `--limit max_series_duration_s=3600`.

## Example prompts

- "Set the meter to 1064 nm and tell me the power after the objective."
- "Log the laser power every 2 s for 10 minutes and report the RMS and peak-to-peak stability. Save the data to ~/data/laser_warmup.csv."
- "I've capped the sensor. Zero it, then I'll remove the cap and you read the power."
- "Measure the transmission of this ND filter: read the power without it, then again after I insert it, and give me the optical density."
- "Is the thermal sensor head still warming up? Read its temperature every minute until it's stable."

## Notes

- **Wavelength matters.** Photodiode responsivity varies by a factor of several across the sensor's range, so a wrong wavelength setting gives a wrong power. The server sends whole nanometres (the PM400/PM101/PM5020 references specify an integer), so 632.8 nm is set as 633 nm. That changes the responsivity by far less than the sensor's calibration uncertainty.
- **Units.** `MEAS:POW?` answers in the meter's configured unit (W or dBm). The driver reads `SENS:POW:UNIT?` first and always returns watts, so a front-panel unit change can't mislead the agent.
- **Out of range.** The meter answers INFINITY / 9.9E37 when the signal exceeds a manually selected range. The server reports this as an error that suggests auto-ranging.
- **Zeroing** (`SENS:CORR:COLL:ZERO`) runs in the background. The server polls `…:ZERO:STAT?`, checks the error queue, and returns the zero value (`…:ZERO:MAGN?`). Zero values are lost when the meter reboots.
- **Energy (pyroelectric) sensors** are recognised but not supported: single-pulse energy measurement needs a trigger/fetch workflow that this server doesn't implement yet.
- **Averaging** is `SENS:AVER:COUN`. One sample takes ~3 ms on the PM100 series and 1 ms on the PM400/PM101/PM5020. The reply timeout scales with the averaging count.
- **PM101 and PM5020:** Thorlabs' command references for these list exactly the commands used here. The PM5020 adds a channel suffix (`SENS2`, `MEAS2`, `SYST:SENS2:IDN?`), selected with `--option channel=2`. Neither has been tested yet, and PM5020 LAN/serial connections have not been tried. The PM100D's Windows TLPM DLL driver is not used; this server speaks SCPI only.
- The simulator's error replies for out-of-range values (`-222,"Data out of range"`) use the standard SCPI code. The Thorlabs documents only list `-113,"Undefined header"` explicitly.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
