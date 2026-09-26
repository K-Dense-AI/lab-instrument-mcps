# Atlas Scientific EZO Sensors — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-atlas-ezo -->

Let an AI agent read, calibrate and log **Atlas Scientific EZO™** sensor circuits (pH, ORP, dissolved oxygen, conductivity, RTD temperature, humidity, CO2 and pressure) over their documented **UART** command set. Useful for bioreactors, cell-culture media, fermentation, hydroponics, aquaculture and environmental monitoring.

| | |
|---|---|
| **Package** | `labmcp-atlas-ezo` |
| **Instruments** | EZO-pH, EZO-ORP, EZO-DO, EZO-EC, EZO-RTD, EZO-HUM, EZO-CO2, EZO-PRS (type detected automatically with `i`) |
| **Interfaces** | UART via a USB-serial adapter or Atlas isolated carrier board, or a serial-to-Ethernet bridge (one circuit per port) |
| **Protocol** | EZO UART command set ([pH datasheet V 6.1](https://files.atlas-scientific.com/pH_EZO_Datasheet.pdf), [ORP V 5.2](https://files.atlas-scientific.com/ORP_EZO_Datasheet.pdf), [DO V 5.8](https://files.atlas-scientific.com/DO_EZO_Datasheet.pdf), [EC V 6.7](https://files.atlas-scientific.com/EC_EZO_Datasheet.pdf), [RTD V 3.7](https://files.atlas-scientific.com/EZO_RTD_Datasheet.pdf), [HUM V 1.5](https://files.atlas-scientific.com/EZO-HUM-Datasheet.pdf), [CO2 V 2.3](https://files.atlas-scientific.com/EZO_CO2_Datasheet.pdf), [PRS V 2.3](https://files.atlas-scientific.com/EZO-PRS-Datasheet.pdf)) |
| **Status** | 🧪 **simulated**: tested against a wire-level EZO simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-atlas-ezo --simulate --check
uvx labmcp-atlas-ezo --simulate --option sim_sensor=DO --check     # also ORP, EC, RTD, HUM, CO2, PRS
```

## Connect your instrument

1. **Circuit setup:** the circuit must be in **UART mode** (LED green/cyan; blue means I2C mode, see "Change to UART mode" in the datasheet). Factory settings are **9600 baud**, 8 data bits, no parity, 1 stop bit, no flow control, CR line endings. Cross TX/RX between the circuit and the adapter, and use an electrically isolated carrier board when the probe sits in the same liquid as other probes or pumps.
2. **Find the port:** `uvx labmcp ports`
3. **Test the connection:**
   ```bash
   uvx labmcp-atlas-ezo --address /dev/ttyUSB0 --check                # Linux
   uvx labmcp-atlas-ezo --address /dev/tty.usbserial-XXXX --check     # macOS
   uvx labmcp-atlas-ezo --address COM5 --check                        # Windows
   uvx labmcp-atlas-ezo --address "serial://COM5?baudrate=38400" --check   # non-default baud rate
   ```
   On connect the server sends `C,0` (continuous readings off, so replies are not interleaved with readings) and `*OK,1` (response codes on, the factory default). The circuit keeps both settings across power cycles.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add ph-probe -- uvx labmcp-atlas-ezo --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "ph-probe": {
      "command": "uvx",
      "args": ["labmcp-atlas-ezo", "--address", "/dev/ttyUSB0"]
    },
    "do-probe": {
      "command": "uvx",
      "args": ["labmcp-atlas-ezo", "--address", "/dev/ttyUSB1"]
    }
  }
}
```

Run one server per circuit. Add `--read-only` to allow readings but block calibration and settings changes. For other clients, generate the snippet with `uvx labmcp config atlas-ezo --address /dev/ttyUSB0 --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `calibrate` | 🎛 control | Store one calibration point. The probe must already be in the named standard (buffer, air, dry, ...) with a stable reading. Point names are validated per circuit type. pH: a `mid` calibration erases existing low/high points, so always do mid first. EC: do `dry` first. |
| `clear_calibration` | 🎛 control | Delete all stored calibration data (Cal,clear). pH/ORP/DO/EC/RTD return to uncalibrated; CO2/PRS return to their factory calibration. The probe must be recalibrated afterwards. |
| `get_calibration_status` | 👁 read | Report how many calibration points are stored and what that means for this circuit type; for pH also the probe slope (acid/base % of ideal and zero offset in mV). |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_info` | 👁 read | Report the circuit type, firmware, device name, supply voltage and last restart reason, LED state, calibration state, temperature compensation and type-specific settings (enabled outputs, EC probe constant K, DO salinity/pressure compensation). |
| `log_series` | 👁 read | Record a series of readings (e.g. to watch a probe stabilise before calibrating, follow pH or DO in a bioreactor, or log temperature). Returns every point plus mean, stdev, min, max and drift per minute for each output. |
| `read_value` | 👁 read | Take one reading (about 1 s) and return every enabled output with its unit: pH; ORP in mV; DO in mg/L (and % saturation if enabled); EC in µS/cm plus TDS (ppm), salinity (PSU) and specific gravity if enabled; RTD temperature; humidity %RH (+ air temperature, dew point); CO2 ppm; or pressure. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_do_compensation` | 🎛 control | DO circuits only: set salinity compensation (irrelevant below ~2500 µS/cm) and/or atmospheric pressure compensation (default 101.3 kPa; lower at altitude). Neither is retained through a power cycle. |
| `set_led` | 🎛 control | Turn the circuit's status LED on or off. Returns the new LED state. |
| `set_probe_constant` | 🎛 control | EC circuits only: set the conductivity probe's cell constant K to match the probe (printed on it). Recalibrate after changing it. Returns the K now in use. |
| `set_temperature_compensation` | 🎛 control | Set the sample temperature used to compensate pH, EC or DO readings (T,n; always °C). Not retained through a power cycle. Returns the value the circuit now uses. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_series_duration_s` | 600 s | Longest logging series an agent may start |

Override at launch: `--limit max_series_duration_s=3600` (3600 s is the most one `log_series` call can run; split longer logs into several calls). `log_series` writes its optional `save_path` only to a new `.csv` file (existing files are never overwritten). These circuits only measure, so there are no hazardous tools; the risk is a *bad calibration*, which is why `calibrate` validates point names and standard values per circuit type and `clear_calibration` is hidden in `--read-only` mode.

## Example prompts

- "What sensor is connected, is it calibrated, and what's the pH slope?"
- "The pH probe is in pH 7.00 buffer. Log it every 2 s until it's stable to ±0.01, then calibrate the midpoint. Then walk me through pH 4 and pH 10."
- "The bioreactor is at 37 °C: set the temperature compensation and read the dissolved oxygen in mg/L and % saturation."
- "Log conductivity, TDS and salinity every 30 s for 10 minutes and tell me whether the nutrient solution is drifting."
- "Turn off the LED on the DO circuit, we're running a light-sensitive culture."

## Notes

- **Units per circuit:** pH; ORP in mV; DO in mg/L and (if enabled with `O,%,1`) % saturation; EC in µS/cm plus TDS (ppm), salinity (PSU) and specific gravity when enabled; RTD in its configured scale (°C/K/°F); HUM in %RH plus air temperature and dew point (°C) when enabled; CO2 in ppm (+ internal temperature); PRS in its configured unit (psi, atm, bar, kPa, inH2O, cmH2O). Which outputs are enabled is read from the circuit (`O,?`, `S,?`, `U,?`) rather than assumed.
- **Output order** of multi-value readings follows the datasheets (EC: EC,TDS,S,SG). The DO order (mg/L then % saturation) is not spelled out in the DO datasheet; it was cross-checked against the open-source EnviroDIY ModularSensors and feastorg `ezo-driver` libraries. Please report if your circuit differs.
- **Temperature compensation** (`T,n`, pH/EC/DO only) and DO salinity/pressure compensation are **not retained** when the circuit loses power; set them again after a power cycle. Readings report the compensation temperature in use.
- **Calibration rules enforced:** pH midpoint must be calibrated before low/high (a new `mid` erases low/high, as the datasheet warns), with mid 6-8, low 0-6.5 and high 7.5-14; ORP ±1020 mV; EC 0.07-500,000 µS/cm (do `dry` first); CO2 high point 3000-5000 ppm; RTD -126 to 1254 °C. The server cannot see which standard the probe is actually in: the agent must confirm that with you.
- An RTD reading of -1023 means no probe is connected; the reading includes a warning.
- DO queries such as `P,?` answer `?,P,90.25` (note the extra comma) per the datasheet; the driver accepts both forms. The DO salinity reply contains a micro sign, so the port is read as Latin-1.
- Not exposed as tools (verified but not useful or too risky for an agent): `Factory` (erases calibration), `Sleep`, `Find`, `Baud`, `I2C`, `Plock`, calibration `Export`/`Import`, the RTD data logger, and the HUM/CO2/PRS alarm pins. EZO pumps (EZO-PMP) and other actuator circuits are refused on connect.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
