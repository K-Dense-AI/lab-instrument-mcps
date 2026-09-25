# JULABO Circulator — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-julabo -->

Let an AI agent run **JULABO heating and refrigerated circulators** through JULABO's documented interface commands: read the bath and external temperatures and the heating/cooling power, set the setpoint, start and stop temperature control, decode status and alarm messages, and wait until the bath is stable at temperature.

| | |
|---|---|
| **Package** | `labmcp-julabo` |
| **Instruments** | CORIO CD, CORIO CP, MAGIO MS (and MX), DYNEO DD circulators. PRESTO and older JULABO models use a similar command set but are unverified |
| **Interfaces** | USB (virtual COM port), RS-232 (CORIO CP, MAGIO, DYNEO) |
| **Protocol** | JULABO interface commands, appendix "Interface commands" of the original operating manuals: [CORIO CD](https://pim-resources.coleparmer.com/instruction-manual/corio-cd-circulator-manual.pdf) (1.950.0800.us.V10), [CORIO CP](https://pim-resources.coleparmer.com/instruction-manual/corio-cp-operating-manual-1-950-0900-us.pdf) (1.950.0900.us.V04), [MAGIO MS](https://pim-resources.coleparmer.com/instruction-manual/magio-ms-operating-manual-1-950-1700-us-v05.pdf) (1.950.1700.us.V05), [DYNEO DD](https://pim-resources.coleparmer.com/instruction-manual/dyneo-dd-operating-manual-1-950-1300-us-v03.pdf) (1.950.1300.us.V03). Official downloads: [julabo.com](https://www.julabo.com/en/service/downloads/operating-manuals) |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-julabo --simulate --check
```

## Connect your circulator

1. **Enable remote control on the circulator.** Without it the circulator answers queries but ignores every setting command.
   - CORIO CD/CP: `MENU` → `IntE` → `rEM` → `USb` (or `232` for RS-232). `rOFF` appears in standby.
   - MAGIO / DYNEO: Main menu → Connect unit → Remote control → USB or RS232. An "R" appears in the status bar.
2. **Cable:** USB-B (the circulator appears as a virtual COM port; on Windows install JULABO's USB driver) or a **null-modem** RS-232 cable. RS-232 factory settings: **4800 baud, 7 data bits, even parity, 1 stop bit, hardware handshake (RTS/CTS)**. Interface parameters can only be changed while remote control is off. The server uses these settings but does not enforce the handshake by default (it keeps RTS asserted, which lets the circulator transmit, and USB virtual COM ports often never assert CTS); add `?rtscts=true` to the address to enforce it.
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-julabo --address /dev/ttyACM0 --check                  # Linux (USB)
   uvx labmcp-julabo --address /dev/tty.usbmodem1101 --check         # macOS
   uvx labmcp-julabo --address COM3 --check                          # Windows
   uvx labmcp-julabo --address "serial://COM3?baudrate=9600" --check # RS-232, non-default baud rate
   ```
   `--check` prints the firmware string (`version`) and the current status, e.g. `02 REMOTE STOP`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add julabo -- uvx labmcp-julabo --address /dev/ttyACM0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "julabo": {
      "command": "uvx",
      "args": ["labmcp-julabo", "--address", "/dev/ttyACM0", "--limit", "max_temperature_c=120"]
    }
  }
}
```

Add `--read-only` to allow monitoring only (`stop_circulation` stays available). For other clients: `uvx labmcp config julabo-circulator --address /dev/ttyACM0 --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_setpoint` | 👁 read | Read the current temperature setpoint and whether temperature control is running. |
| `get_status` | 👁 read | Report the circulator's status message (decoded: operating state, rejected command or alarm), whether temperature control is running, the setpoint, the device's own excess-temperature protection setting and warning limits, and the firmware version. |
| `read_temperatures` | 👁 read | Read the bath temperature, the heating/cooling power in % and the safety-sensor temperature, and optionally the external Pt100 sensor. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_setpoint` | ⚠️ hazard | Set the circulator's temperature setpoint (out_sp_00). If temperature control is running, the circulator starts heating or cooling to it immediately. Refused outside `min_temperature_c`..`max_temperature_c`; the circulator itself also rejects values outside its range or warning limits (reported as an error). Needs remote control enabled. |
| `start_circulation` | ⚠️ hazard | Start temperature control (out_mode_05 1): the pump runs and the bath heats or cools to the setpoint, which is checked against the safety limits first (it may have been changed on the front panel). Make sure the bath is filled and any external hoses are connected and secured. Needs remote control enabled. |
| `stop_circulation` | 🛑 safety | Stop temperature control and the pump (out_mode_05 0), then confirm with in_mode_05. Also ends a running `wait_for_temperature`. The bath stays hot/cold after stopping. |
| `wait_for_temperature` | 👁 read | Poll the bath (or external) temperature until it has been within `tolerance_c` of the target for `stable_for_s` seconds, or `timeout_s` passes. Does not change anything; start circulation first. Stops early (reached = false) if the circulator raises an alarm or a stop command is sent. Returns a short temperature trace. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_temperature_c` | 90 °C | Highest setpoint an agent may set (also checked before `start_circulation`) |
| `min_temperature_c` | 5 °C | Lowest setpoint an agent may set (also checked before `start_circulation`) |
| `max_wait_s` | 7200 s | Longest `wait_for_temperature` |

The defaults suit a **water** bath. Set them to your bath fluid's working range, e.g. `--limit min_temperature_c=-30 --limit max_temperature_c=150` for a silicone oil / glycol-rated setup. The circulator additionally enforces its own working range, warning limits and excess-temperature protection; rejections come back as errors such as `-11 VALUE TOO LARGE`.

## Example prompts

- "Set the circulator to 37 °C, start it and tell me when the bath has been stable within ±0.1 °C for five minutes."
- "What's the bath temperature and how hard is the circulator working right now?"
- "Cool the jacketed reactor to 10 °C using the external Pt100 as the reference and let me know when it gets there."
- "Is there any alarm on the circulator? Explain what it means."
- "Stop the circulator."

## Notes

- **Remote control is a front-panel setting.** In manual mode (`00 MANUAL STOP` / `01 MANUAL START`) the server refuses to send settings and tells you how to enable remote control.
- **Every setting is verified.** OUT commands have no reply, so after each one the server queries `status` (to catch `-08 INVALID COMMAND`, `-09 COMMAND NOT ALLOWED IN CURRENT OPERATING MODE`, `-10 VALUE TOO SMALL`, `-11 VALUE TOO LARGE`, `-13 VALUE EXCEEDS TEMPERATURE LIMITS`) and reads the setpoint or `in_mode_05` back. `stop_circulation` retries once and reports an error if the circulator still says it is running.
- **Model differences.** CORIO CD has only the basic set (actual value, power, safety sensor, setpoint, start/stop). Warning limits (`in_sp_03/04`) need CORIO CP, MAGIO or DYNEO; the external Pt100 (`in_pv_02`) needs MAGIO or DYNEO. The server detects missing commands (no reply) and reports those values as `null`.
- **Command pacing (unverified).** The manuals give no minimum time between commands. The server waits `command_delay_s` (default 0.25 s) after each setting command before checking `status`; change it with `--option command_delay_s=0.5` if your circulator misses commands.
- **Watchdog.** MAGIO/DYNEO circulators have a watchdog configured in their own menu (mode, timeout, fallback setpoint, reset on "setpoint only" or "all valid commands"). The server does not configure it. With restart mode "all valid commands" you can keep it fed while the server runs with `--option keepalive_s=10` (queries `status` every 10 s). JULABO's alarm list notes that it expects the setpoint at least every 30 s in "setpoint" restart mode, which this keep-alive does not do.
- Status and alarm texts are decoded from the code (`-01`, `-14`, `-63`, ...) using the alarm tables in the manuals; the exact wording the circulator sends is passed through unchanged.
- Not implemented: pump stage/capacity (`out_sp_07/27`), warning-limit writes (`out_sp_03/04`), controller parameters (`out_par_*`), external/internal control switch (`out_mode_04`), EPROG and ATC calibration commands.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
