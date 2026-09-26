# Mettler Toledo Balance — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-mettler-toledo -->

Let an AI agent weigh samples, tare containers, log drift and evaporation, run internal adjustments and drive the draft shield on **Mettler Toledo balances** through the open **MT-SICS** interface command set.

| | |
|---|---|
| **Package** | `labmcp-mettler-toledo` |
| **Instruments** | Excellence XPR/XSR, XP/XS, XA/XE, MS/ML, NewClassic ME/MS, and other MT-SICS balances and weighing terminals |
| **Interfaces** | RS-232, USB (virtual COM port), Ethernet (XPR/XSR, or any serial-to-Ethernet adapter) |
| **Protocol** | MT-SICS levels 0–2 ([reference manual](https://www.mt.com/dam/product_organizations/laboratory_weighing/WEIGHING_SOLUTIONS/PRODUCTS/MT-SICS/MANUALS/en/Excellence-SICS-BA-en-11780711D.pdf)) |
| **Status** | 🧪 **simulated**: tested against a wire-level MT-SICS simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-mettler-toledo --simulate --check
```

## Connect your balance

1. **Balance setup:** in the balance menu, set the interface (RS232/USB/Ethernet) to **MT-SICS** (sometimes called "Host" or "Dialog"). Factory settings are 9600 baud, 8 data bits, no parity, 1 stop bit, Xon/Xoff.
2. **Find the port:** `uvx labmcp ports`
3. **Test the connection:**
   ```bash
   uvx labmcp-mettler-toledo --address /dev/ttyUSB0 --check          # Linux
   uvx labmcp-mettler-toledo --address /dev/tty.usbserial-XXXX --check  # macOS
   uvx labmcp-mettler-toledo --address COM4 --check                  # Windows
   uvx labmcp-mettler-toledo --address tcp://192.168.1.60:8001 --check  # Ethernet
   ```
   Non-default settings go in the address: `serial://COM4?baudrate=19200&xonxoff=false`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add balance -- uvx labmcp-mettler-toledo --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "balance": {
      "command": "uvx",
      "args": ["labmcp-mettler-toledo", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Add `--read-only` to allow weighing but block taring, zeroing and door movement. For other clients, generate the snippet with `uvx labmcp config mettler-toledo --address /dev/ttyUSB0 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `clear_tare` | 🎛 control | Clear the tare memory (tare = 0). |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_draft_shield` | 👁 read | Report the position of the motorised draft-shield doors (Excellence/XPR balances only). |
| `get_tare` | 👁 read | Return the weight currently stored in the tare memory. |
| `log_weight_series` | 👁 read | Record a series of immediate (unfiltered) readings to monitor drift, evaporation, moisture uptake or stabilisation. Returns every reading plus summary statistics. |
| `read_temperature` | 👁 read | Read the balance's internal temperature probe(s) in °C (MT-SICS M28, if supported). |
| `read_weight` | 👁 read | Read the current net weight from the balance. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `reset_balance` | 🛑 safety | Abort whatever the balance is doing (an adjustment, a repeating weight stream, a pending command) and reset it to its power-on state without zeroing (MT-SICS @). Clears the tare. Motorised draft-shield doors have their own obstruction detection and reverse if blocked. |
| `run_internal_adjustment` | 🎛 control | Adjust (calibrate) the balance with its built-in reference weight (MT-SICS C3). The pan must be empty and the balance undisturbed. Takes roughly 1-3 minutes. |
| `set_draft_shield` | ⚠️ hazard | Open or close the motorised draft-shield doors. Make sure nothing (and no one's fingers) is in the way of the doors. |
| `set_tare_preset` | 🎛 control | Preset a known tare weight (e.g. a container weighed earlier). |
| `show_message` | 🎛 control | Show a short message on the balance display (e.g. 'Add sample 3'). Use `show_weight_display` to return to the normal weight display. |
| `show_weight_display` | 🎛 control | Switch the balance display back to showing the weight. |
| `tare` | 🎛 control | Tare the balance: store the current load (e.g. an empty container) as the tare weight. Returns the stored tare weight. |
| `zero` | 🎛 control | Zero the balance with the current load. Also clears the tare memory. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_series_duration_s` | 600 s | Longest weight-logging series an agent may start |

Override at launch: `--limit max_series_duration_s=3600`. Whatever the limit says, a single series is capped at one hour (the tool's time limit).

## Example prompts

- "Tare the balance, then tell me when the reading is stable after I add the sample."
- "Log the weight every 10 s for 5 minutes and tell me the evaporation rate in mg/min."
- "Weigh out ~250 mg: tell me how much more to add, reading every few seconds, until I'm within ±2 mg."
- "Run an internal adjustment and confirm it succeeded before we start the calibration curve."

## Notes

- `read_weight(stable=True)` sends `S`, which waits for stability. If the balance cannot settle (draughts, vibration), it returns `S I`. The server reports this as an error, so retry or use `stable=false`.
- Weights are in the balance's **unit 1** (normally grams). Units shown on the display (unit 2) don't affect the interface.
- Draft-shield (`WS`) and temperature (`M28`) commands exist only on balances with that hardware; other balances answer `ES`/`L`, which the server reports clearly.
- `run_internal_adjustment` waits for the final `C3 A` without blocking the connection: `reset_balance` aborts a running adjustment, and other commands are refused until it ends (their replies would be mixed up with the result).
- Before each command the server discards anything the balance sent unasked (e.g. a print-key transmission) and checks that the reply belongs to the command, so one late reply cannot shift every later reply by one.
- `show_message` accepts printable ASCII only, and `set_tare_preset` only a unit symbol: a line break would otherwise reach the balance as a second MT-SICS command.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
