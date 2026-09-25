# Pfeiffer Vacuum Gauge Controller — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-pfeiffer-tpg -->

Let an AI agent read and log chamber and foreline pressures, identify the connected gauges, change the pressure unit and switch ionisation gauges on and off (with a pressure check) on **Pfeiffer Vacuum TPG total-pressure gauge controllers**.

| | |
|---|---|
| **Package** | `labmcp-pfeiffer-tpg` |
| **Instruments** | TPG 361 SingleGauge, TPG 362 DualGauge, TPG 366 MaxiGauge (ActiveLine gauges); TPG 261 / TPG 262 (compact gauges) |
| **Interfaces** | TPG 36x: USB type B (virtual COM port), Ethernet (TCP port 8000); TPG 26x: RS-232 |
| **Protocol** | Pfeiffer mnemonics protocol, ACK/NAK + ENQ ([TPG 361/362 communication protocol BG 5510 BEN](https://www.ajvs.com/library/Pfeiffer_Operating_instructions_TPG361_TPG362_SingleGauge_DualGauge_Controller_Communication_Protocol.pdf), [TPG 366 communication protocol BG 5511 BEN](https://www.ajvs.com/library/TPG_366_Communication_Protocol_BG5511BEN.pdf), [TPG 262 operating instructions BG 805 196 BE](https://www.idealvac.com/files/ManualsII/Pfeiffer_TPG262_Operating_Instructions.pdf) section 5; originals in the [Pfeiffer download center](https://www.pfeiffer-vacuum.com)) |
| **Status** | 🧪 **simulated**: tested against a byte-level simulator of the protocol, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-pfeiffer-tpg --simulate --check                      # TPG 362: FullRange gauge + Pirani
uvx labmcp-pfeiffer-tpg --simulate --option model=tpg366 --check # 6 channels incl. a cold cathode gauge
```

The simulated chamber pumps down from 8e-5 mbar towards 2e-7 mbar (10 min time constant) and the foreline sits at about 2.4e-2 mbar.

## Connect your controller

1. **Protocol (TPG 36x):** the *PROTOCOL* parameter must be *AUTO* (factory setting) or *MNE* (mnemonics), not *PV* (Pfeiffer Vacuum telegram protocol).
2. **USB (TPG 36x):** the USB type B port appears as a virtual COM port. Factory setting 9600 baud, 8N1 (parameter *BAUD USB*; 19200-115200 also possible).
3. **Ethernet (TPG 36x, TPG 366):** set DHCP or a static IP (front panel or Pfeiffer's Ethernet Configuration Tool); the command port is fixed at **8000**.
4. **RS-232 (TPG 261/262):** 9600 baud 8N1 by default, only TXD/RXD used.
5. **Test the connection:**
   ```bash
   uvx labmcp-pfeiffer-tpg --address /dev/ttyUSB0 --check
   uvx labmcp-pfeiffer-tpg --address COM3 --check
   uvx labmcp-pfeiffer-tpg --address "serial://COM3?baudrate=115200" --check
   uvx labmcp-pfeiffer-tpg --address tcp://192.168.1.70:8000 --check
   ```
   The model is detected with `AYT` (TPG 36x); a controller that does not know `AYT` is treated as a TPG 26x. Use `--option model=tpg261` (or `tpg262`, `tpg361`, `tpg362`, `tpg366`) to be explicit.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add vacuum -- uvx labmcp-pfeiffer-tpg --address tcp://192.168.1.70:8000
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "vacuum": {
      "command": "uvx",
      "args": ["labmcp-pfeiffer-tpg", "--address", "tcp://192.168.1.70:8000"]
    }
  }
}
```

Add `--read-only` to allow reading and logging but block unit changes and switching gauges on (`switch_gauge_off` stays available). For other clients, generate the snippet with `uvx labmcp config pfeiffer-tpg --address tcp://192.168.1.70:8000 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_errors` | 👁 read | Read (and clear) the controller's ERROR word: controller error, no hardware, inadmissible parameter or syntax error. An empty list means no error. |
| `get_gauge_types` | 👁 read | List the gauge connected to each channel (TID), what kind it is, and whether it is an ionisation gauge that is currently switched on or off (SEN). |
| `log_pressure_series` | 👁 read | Log pressures at a fixed interval (e.g. a pump-down curve, leak-up/rate-of-rise test or bake-out). Returns per-channel statistics in log-space plus a downsampled series; the full series can be written to CSV. Bounded by `max_log_duration_s`. |
| `read_all_pressures` | 👁 read | Read every channel of the controller at once (PRX), with status per channel. |
| `read_pressure` | 👁 read | Read one gauge channel: status, pressure in the display unit and in mbar. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_gauge_power` | ⚠️ hazard | Switch an ionisation gauge (IKR, PKR, PBR, IMR) on or off (SEN). Switching a hot or cold cathode gauge on at high pressure can burn out the filament or contaminate the gauge, so switching ON is refused unless a valid reading from this gauge or `reference_channel` is at or below `max_switch_on_pressure_mbar`. Switching off may trip setpoint relays used as interlocks. |
| `set_unit` | 🎛 control | Change the pressure unit used on the display and interface (UNI). Readings in mbar are always included regardless of this setting. |
| `switch_gauge_off` | 🛑 safety | Switch an ionisation gauge off (e.g. before venting, or if the pressure is rising). Always allowed. Note that setpoint relays assigned to this channel may change state. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_switch_on_pressure_mbar` | 1e-2 mbar | An ionisation gauge may only be switched on when a valid reading (its own, or `reference_channel`) is at or below this pressure |
| `max_log_duration_s` | 3600 s | Longest `log_pressure_series` an agent may start |

Override at launch: `--limit max_switch_on_pressure_mbar=1e-3 --limit max_log_duration_s=86400`.

## Example prompts

- "What's the chamber pressure right now, and is the foreline below 5e-2 mbar?"
- "Log all gauges every 10 s for 30 minutes and tell me how many decades per minute the chamber is pumping down."
- "Close the valve, then log channel 1 every 2 s for 5 minutes so I can estimate the leak-up rate."
- "Which gauges are connected, and which ones are ionisation gauges that are switched off?"
- "The chamber is below 1e-5 mbar on the FullRange gauge - switch on the cold cathode gauge on channel 3, using channel 1 as the reference."

## Notes

- **Switching ionisation gauges on is the hazard.** Hot-cathode (Bayard-Alpert) filaments can burn out and cold-cathode (Penning) gauges contaminate when operated at high pressure. `set_gauge_power(on=True)` requires a reading with status *ok* at or below `max_switch_on_pressure_mbar` from the gauge itself or from `reference_channel` on the same vacuum. An *underrange* reading from a Pirani is not accepted as proof (what the controller reports on underrange is not specified), so in a Pirani + Penning setup switch the Penning on at the controller, or use a FullRange gauge as the reference. The 1e-2 mbar default is the upper end of a typical cold cathode measuring range; check your gauge's manual and lower it for hot-filament gauges if recommended.
- Switching a gauge off (`switch_gauge_off`, or `set_gauge_power(on=False)`) can change setpoint (switching-function) relays assigned to that channel, which may be wired to valve or pump interlocks.
- Every pressure comes with the controller's status: *ok*, *underrange*, *overrange*, *sensor error*, *sensor off*, *no sensor* or *identification error*. `pressure` is only filled in when the status is *ok*; `raw_value` is what the controller sent. Values are converted to mbar (1 Torr = 1.333224 mbar); in *V* mode no conversion is made.
- Every command waits for ACK/NAK; after a NAK the ERROR word (controller error, no hardware, inadmissible parameter, syntax error) is read and reported. `get_errors` reads and clears it.
- Degas, filter, calibration factors, switching-function setpoints, gauge control (hot start/self control), the data logger and the Pfeiffer Vacuum (PV) telegram protocol are not exposed. RS-485 multi-drop operation has not been tested.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
