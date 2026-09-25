# Sartorius Balance — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-sartorius -->

Let an AI agent weigh samples, tare containers, log drift and evaporation, and run internal adjustments on **Sartorius laboratory balances** through the documented **SBI** (Sartorius Balance Interface) ASCII protocol.

| | |
|---|---|
| **Package** | `labmcp-sartorius` |
| **Instruments** | Cubis MSE, Cubis II MCA, Secura, Quintix, Practum, Entris II; older CP/CPA-series balances with `--option legacy=true`. Other SBI balances are likely to work |
| **Interfaces** | RS-232, USB (virtual COM port, "PC-SBI"), Ethernet (Cubis II "serial transmission via Ethernet", or any serial-to-Ethernet adapter) |
| **Protocol** | SBI: [Entris II interface description](https://www.sartorius.hr/media/dypfvdsn/entris-ii-technical-note-en-sartorius.pdf) (technical note 10/2020), [Secura/Quintix/Practum user manual](https://api.sartorius.com/document-hub/dam/download/21625/Manual_Secura_Quintix_Practum_WSE6004-e181008.pdf) §10.3 (WSE6004), [Cubis MSE operating instructions](https://api.sartorius.com/document-hub/dam/download/20494/Manual_Cubis_MSE_WMS6004-e.pdf) (WMS6004), [Cubis MCA operating instructions](https://www.sartorius.com/download/920578/manual-cubis-mca-micro-balances-wmc6028-e-pdf-data.pdf) (WMC6028), [SBI interface description](https://api.sartorius.com/document-hub/dam/download/22650/MAN-CC_Interface-e.pdf) (98647-000-53) |
| **Status** | 🧪 **simulated**: tested against a wire-level SBI simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-sartorius --simulate --check
```

## Connect your balance

1. **Balance setup:** set the interface you use (RS232, USB-B / "PC-SBI", or Ethernet) to the **SBI** protocol, and set SBI data output to **manual, without stability** with **automatic output off** (factory settings on Cubis II and Entris II). With auto print on, `ESC P` toggles the automatic output instead of sending one reading. Either line format works (16 characters, or 22 with ID code).
2. **Serial settings:** factory settings are **9600 baud, 8 data bits, odd parity, 1 stop bit**, hardware handshake (Cubis II/MSE, Secura/Quintix/Practum, Entris II). The server uses these but does not enforce RTS/CTS (it keeps RTS asserted, which lets the balance transmit; USB virtual COM ports often never assert CTS). Add `?rtscts=true` to enforce it, or e.g. `?bytesize=7&parity=E` if your balance is set differently.
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-sartorius --address /dev/ttyUSB0 --check                 # Linux
   uvx labmcp-sartorius --address /dev/tty.usbmodem14201 --check       # macOS
   uvx labmcp-sartorius --address COM4 --check                         # Windows
   uvx labmcp-sartorius --address tcp://192.168.1.61:49155 --check     # Cubis II via Ethernet (port as configured)
   ```
   `--check` prints the model, serial number and software version (`ESC x1_`, `x2_`, `x3_`) when the balance supports them.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add balance -- uvx labmcp-sartorius --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "balance": {
      "command": "uvx",
      "args": ["labmcp-sartorius", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Add `--read-only` to allow weighing but block taring, zeroing and adjustment. For other clients: `uvx labmcp config sartorius-balance --address /dev/ttyUSB0 --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `lock_keypad` | 🎛 control | Block or unblock the balance keys, e.g. so nobody tares by accident during a long logging run. |
| `log_weight_series` | 👁 read | Record a series of immediate (unfiltered) readings to monitor drift, evaporation, moisture uptake or stabilisation. Returns every reading plus summary statistics. |
| `read_weight` | 👁 read | Read the current net weight from the balance (ESC P). With stable=True the server polls until the balance reports a stable value; if it cannot settle (draughts, vibration, evaporation) you get an error; retry, or use stable=false for the current value. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `run_internal_adjustment` | 🎛 control | Adjust (calibrate) the balance with its built-in weight (ESC Z; isoCAL models only). The pan must be empty and the balance undisturbed. SBI sends no completion message, so the server watches for the adjustment status and waits until the balance weighs again; check `observed_adjustment` and the balance display or GLP printout for the result. |
| `set_ambient_conditions` | 🎛 control | Adapt the balance's filter to the ambient conditions (ESC K/L/M/N). Use 'unstable' or 'very_unstable' for draughty or vibrating benches (slower but steadier readings). |
| `tare` | 🎛 control | Tare the balance (ESC U, or ESC T on legacy balances): the current load (e.g. an empty container) becomes the tare. Then waits for a stable reading and checks that it is ~0; returns that reading. |
| `zero` | 🎛 control | Zero the balance (ESC V). Only works with the pan (nearly) empty, within the balance's zero-setting range; clears the tare. Confirms by reading ~0 afterwards. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_series_duration_s` | 600 s | Longest weight-logging series an agent may start |

Override at launch: `--limit max_series_duration_s=3600`. No tool of this server moves anything or heats, so there are no hazard tools.

## Example prompts

- "Tare the balance, then tell me the weight once it's stable after I add the sample."
- "Log the weight every 10 s for 5 minutes and tell me the evaporation rate in mg/min."
- "Weigh out ~250 mg: tell me how much more to add, reading every few seconds, until I'm within ±2 mg."
- "The bench is vibrating today; switch the balance filter to unstable conditions and lock the keypad."
- "Run an internal adjustment before we start the calibration curve and tell me whether it ran."

## Notes

- **Stability comes from the unit symbol.** SBI has no explicit stability flag: the balance only sends the unit symbol (positions 12–14) when the reading is stable ("If the weighing system has not stabilized, no unit symbol is output", interface description 98647-000-53; the newer manuals list those positions as "unit symbol or space"). `read_weight(stable=true)` therefore polls `ESC P` until a reading with a unit arrives. Unstable readings report the last unit seen.
- **Line formats.** The parser follows the documented fixed-width layouts: 16 characters (sign, value in positions 2–10, unit in 12–14, CR LF) and 22 characters (6-character ID code such as `N`, `G#`, `T`, `Stat`, then the same 16). It also accepts the Cubis II "one line with full length" format, a decimal comma, and a G#/T/N weighing block (the `N` line is reported). `High`, `Low`, `Err ###`, `APP.ERR`, `DIS.ERR` and `PRT.ERR` are reported as clear errors.
- **No acknowledgements.** SBI control commands (`ESC U` tare, `ESC V` zero, `ESC Z` adjust, `ESC K`–`N` filter, `ESC O`/`R` keys) produce no reply, so a balance that does not support one silently ignores it. `tare` and `zero` therefore wait for a stable reading and check it is ~0, and report an error otherwise.
- **Zero vs tare.** `ESC V` only zeroes within the balance's zero-setting range (nearly empty pan); `ESC U` tares any load. Older balances (CP/CPA, LE, ...) only have `ESC T` (the tare key): start the server with `--option legacy=true`.
- **Internal adjustment (unverified).** `ESC Z` starts isoCAL on balances with a built-in weight. SBI sends no completion or result message; the server watches for the `Cal.` status (or a balance too busy to answer) and waits for a stable weight again, and reports `observed_adjustment=false` if it never sees one. Confirm the result on the display or GLP printout. The exact status output during adjustment could not be verified in the manuals.
- **Identity replies (unverified format).** `ESC x1_`/`x2_`/`x3_` print the model, serial number and software version; their exact layout is not documented, so the text is passed through as sent.
- The command log shows SBI traffic as hex (`1b 50 0d 0a` = `ESC P CR LF`) because of the ESC character.
- Not implemented: draft-shield and ionizer commands (Cubis MSE only), external adjustment (`ESC W`, needs a reference weight), `ESC S` restart and the function-key commands.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
