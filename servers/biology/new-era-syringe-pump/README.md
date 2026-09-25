# New Era Syringe Pump — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-new-era -->

Let an AI agent set up the syringe, infuse or withdraw an exact volume at a set rate, follow the dispensed volume and stop **New Era Pump Systems syringe pumps** through their documented **RS-232 Basic-mode** command set.

| | |
|---|---|
| **Package** | `labmcp-new-era` |
| **Instruments** | NE-1000 Multi-Phaser, NE-1002X, NE-1010, NE-4000 two-channel, NE-500 OEM, NE-8000 high-pressure, and other NE-1000-series pumps with an RS-232 port |
| **Interfaces** | RS-232 (RJ-11 "To Computer" jack), USB via New Era's CBL-USB232 or any USB-RS-232 adapter, serial-to-Ethernet adapters |
| **Protocol** | New Era RS-232 Basic mode ([NE-1000 User Manual, Publication #1200-01, section 10](https://www.syringepump.com/download/NE-1000%20Syringe%20Pump%20User%20Manual.pdf); same command set in the [NE-4000 manual](https://www.syringepump.com/download/NE-4000%20Syringe%20Pump%20User%20Manual.pdf)) |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-new-era --simulate --check
```

The simulator pumps in real time. Add `--option sim_speed=20` to make simulated time run 20× faster for demos.

## Connect your pump

1. **Pump setup:** the pump must be in **Address mode** (the default, `Ad:nn` in the Setup menu) and in **Basic** communications mode (the default; if someone enabled Safe mode, send `SAF0` from a terminal or run a master reset). Factory settings are **19200 baud, 8N1, network address 0**.
2. **Cable:** New Era CBL-PC-PUMP-7 (plus CBL-USB232 for USB) into the pump's **To Computer** jack.
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-new-era --address /dev/ttyUSB0 --check            # Linux
   uvx labmcp-new-era --address /dev/tty.usbserial-XXXX --check  # macOS
   uvx labmcp-new-era --address COM5 --check                    # Windows
   ```
   Non-default baud rates go in the address: `serial://COM5?baudrate=9600`.
5. **Pump networks:** up to 100 pumps can be daisy-chained (**To Network** → next pump's **To Computer**), each with its own address 0–99. Run one server per pump and select it with `--option pump_address=3`. Without the option, commands carry no address prefix, which the pumps treat as address 0.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add syringe-pump -- uvx labmcp-new-era --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "syringe-pump": {
      "command": "uvx",
      "args": ["labmcp-new-era", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Each server controls one pump (`--option pump_address=N` on a daisy-chained network). A serial port can only be opened by one server at a time, so to drive two pumps at once give each its own cable/port. Add `--read-only` to allow monitoring but block pumping and settings changes.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `clear_dispensed_volume` | 🎛 control | Reset the infused and withdrawn volume accumulators to zero. Only possible while stopped. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_dispensed_volume` | 👁 read | Return the accumulated infused and withdrawn volumes (pump command DIS). |
| `get_status` | 👁 read | Report what the pump is doing (infusing/withdrawing/stopped/paused, alarms), the syringe diameter, rate, target volume, direction and the accumulated dispensed volumes. |
| `infuse` | ⚠️ hazard | Push `volume_ml` out of the syringe at `rate_ml_min`, then stop. Returns as soon as pumping has started, with the estimated duration. Requires the correct syringe diameter (`set_syringe`) and a stopped pump. Overwrites phases 1-2 of the pump's stored Pumping Program. |
| `list_syringe_presets` | 👁 read | List the syringe inside diameters from the New Era manual's reference table (BD, Monoject, Terumo, HSW Norm-Ject, Poulten & Graf glass, stainless steel, SGE, Hamilton), with the NE-1000 rate range each allows. Use a name with `set_syringe`. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_syringe` | 🎛 control | Set the syringe inside diameter, either from a preset or a measured value. The pump must be stopped. Changing the diameter also resets the dispensed-volume accumulators and may switch the pump's volume units (µL below 14.0 mm, mL above). |
| `stop_pump` | 🛑 safety | Stop the pump immediately (STP). A paused program is also cancelled so it cannot resume. |
| `withdraw` | ⚠️ hazard | Pull `volume_ml` into the syringe at `rate_ml_min`, then stop. Returns as soon as pumping has started. Make sure the syringe has room for the volume. Overwrites phases 1-2 of the pump's stored Pumping Program. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_rate_ml_min` | 10 mL/min | Highest pumping rate `infuse`/`withdraw` may set |
| `max_volume_ml` | 20 mL | Largest volume a single `infuse`/`withdraw` may move |

Override at launch: `--limit max_rate_ml_min=0.5 --limit max_volume_ml=2` (e.g. for a microfluidic chip). The pump itself also refuses rates outside what the syringe diameter allows (`?OOR`), and the server explains that range.

## Example prompts

- "Set the pump for a BD 10 mL syringe and infuse 2 mL at 0.5 mL/min into the reactor. Tell me when it's done."
- "Withdraw 250 µL at 100 µL/min, then report how much was actually withdrawn."
- "What's the pump doing right now? Is there an alarm?"
- "Our syringe is a 5 mL glass Hamilton with 10.30 mm inside diameter — set that, then prime by infusing 0.1 mL at 1 mL/min."
- "Stop the pump now."

## Notes

- **Diameter is everything.** The pump converts volume to plunger travel from the inside diameter. Presets come from the reference table in the NE-1000 manual (section 12.7); always confirm against your syringe manufacturer's data. Changing the diameter resets the dispensed-volume counters, and the pump works in µL for diameters ≤ 14.0 mm and in mL above.
- **Stored program is overwritten.** `infuse`/`withdraw` program phase 1 as a RATE phase (rate, volume, direction) and phase 2 as STOP, so the pump stops after the volume. Any multi-phase Pumping Program you stored on the pump is overwritten in phases 1–2.
- **Rounding.** The pump accepts at most 4 significant digits (and 3 decimals). The server picks the rate units (µL/min, mL/min, µL/h, mL/h) that represent your rate most precisely and reports the exact values it sent.
- `infuse`/`withdraw` return as soon as pumping starts; long infusions keep running after the tool call. `stop_pump` sends `STP` and, if the program is then paused, a second `STP` so the program cannot be resumed by accident.
- The first reply after the pump powers up is a reset alarm (`A?R`). The server acknowledges it and repeats the command; stall alarms (`A?S`) are reported, never retried.
- **Safe mode is not supported.** Safe mode (CRC-16 framing, communication watchdog) is recommended by New Era for production. This server uses Basic mode only.
- The **NE-300 "Just Infusion"** manual documents no RS-232 interface, so it is not supported. Syringe rate ranges quoted by `list_syringe_presets` are for the NE-1000; other models (e.g. NE-8000, NE-1010) have different speed ranges.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
