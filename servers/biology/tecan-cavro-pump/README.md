# Tecan Cavro Syringe Pump — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-cavro -->

Let an AI agent initialize, aspirate and dispense microlitre volumes at set flow rates, switch valve ports and stop **Tecan Cavro OEM syringe pumps** through the documented **Cavro Data Terminal (DT) protocol**.

| | |
|---|---|
| **Package** | `labmcp-cavro` |
| **Instruments** | Cavro XLP 6000, Cavro XMP 6000, Cavro XCalibur (with 3-port, 4-port or distribution valves, or valveless) |
| **Interfaces** | RS-232, RS-485 (with an RS-485 adapter), serial-to-Ethernet adapters |
| **Protocol** | Cavro DT protocol ([XLP 6000 Operating Manual, 734237-C, chapter 3 and appendix G](https://www.manualslib.com/manual/1214060/Tecan-Cavro-Xlp-6000.html)); step resolution per [Tecan XCalibur](https://partnering.tecan.com/cavro-xcalibur-pump-for-oem-liquid-handling) / [XMP 6000](https://partnering.tecan.com/cavro-xmp-6000-pump-for-oem-liquid-handling) specifications |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-cavro --simulate --check
```

The simulator is an XLP 6000 with a 1 mL syringe and a 3-port valve. Moves take real time; add `--option sim_speed=10` to speed it up.

## Connect your pump

1. **Pump setup:** set the **address switch** (switch 0 is DT address `1`, switch 1 is `2`, … switch 8 is `9`, switches 9–E are `:` `;` `<` `=` `>` `?`). The pump detects DT versus OEM protocol from the first message after power-up. The default is **9600 baud, 8N1**; 38400 baud is selected with the `U47` configuration command.
2. **Tell the server what is installed.** The pump cannot report its syringe, so these options are **required** on real hardware:
   - `--option syringe_ul=1000`: syringe volume in µL.
   - `--option model=xlp6000` (or `xmp6000`, 6000 increments per stroke; `xcalibur`, 3000). For another DT-compatible pump, give `--option steps_per_stroke=<increments per full stroke in standard mode>` instead.
   - Optional: `--option resolution=fine` (N1, 8× finer volume steps; applied by `initialize`) and `--option pump_address=2`.
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-cavro --address /dev/ttyUSB0 --option syringe_ul=1000 --option model=xcalibur --check
   uvx labmcp-cavro --address COM3 --option syringe_ul=500 --option model=xlp6000 --check
   ```
   Non-default baud rates go in the address: `serial://COM3?baudrate=38400`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add cavro -- uvx labmcp-cavro --address /dev/ttyUSB0 --option syringe_ul=1000 --option model=xcalibur
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "cavro": {
      "command": "uvx",
      "args": ["labmcp-cavro", "--address", "/dev/ttyUSB0",
               "--option", "syringe_ul=1000", "--option", "model=xcalibur"]
    }
  }
}
```

Add `--read-only` to allow status reads but block every move.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `aspirate_ul` | ⚠️ hazard | Draw `volume_ul` into the syringe at `flow_ul_s` through the current (or given) valve port, and wait until the move has finished. The pump must be initialized and have room for the volume. |
| `dispense_ul` | ⚠️ hazard | Push `volume_ul` out of the syringe at `flow_ul_s` through the current (or given) valve port, and wait until the move has finished. The syringe must contain at least that volume. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_status` | 👁 read | Report whether the pump is ready or busy, any error (decoded), the plunger position as the volume in the syringe, the valve position, resolution mode and current top speed. |
| `initialize` | ⚠️ hazard | Initialize the pump: drive the plunger to the top of the syringe (expelling its contents through the valve), set that as position 0 and home the valve. Needed after power-up, a plunger overload or `terminate`. Route the valve output to waste first. Takes a few seconds. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_valve` | ⚠️ hazard | Turn the valve to a named position (3/4-port valves) or to a numbered port (distribution valves). In bypass the plunger cannot move. Returns the new status. |
| `terminate` | 🛑 safety | Stop any plunger move, loop or delay immediately (DT command T). A valve move in progress still completes. Re-initialize afterwards: the plunger may have lost steps. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_volume_ul` | 5000 µL | Largest volume one `aspirate_ul`/`dispense_ul` may move |
| `max_flow_ul_s` | 500 µL/s | Highest plunger flow rate an agent may use |

Override at launch: `--limit max_flow_ul_s=50`. Independently of the limits, the server refuses moves that would overfill or over-empty the syringe, and flows the pump cannot produce with the installed syringe (top speed 5–6000 pulses/s).

## Example prompts

- "Initialize the pump, then prime it: aspirate 1 mL from the input port and dispense it to the output port, three times."
- "Aspirate 150 µL of sample slowly (20 µL/s) from port 3 and dispense 50 µL into port 5."
- "How much liquid is in the syringe right now and which port is the valve on?"
- "The pump reported a plunger overload. What does that mean and what should I check?"
- "Stop the pump now."

## Notes

- **Conversion.** Steps = volume × steps-per-stroke ÷ syringe volume, where steps-per-stroke is 6000 (XLP/XMP) or 3000 (XCalibur) in standard mode and 8× that in fine-positioning mode (N1). The mode is read from the pump (`?28`) before every move. Top speed `V` = flow × 6000 ÷ syringe volume: on all three models a full stroke is 6000 speed pulses in N0/N1. For the XCalibur this is derived from Tecan's published 1.2 s–20 min per stroke, not from its operating manual; please confirm on hardware.
- **Busy/ready** is read only from `Q`, as the manual requires. Invalid commands are reported immediately; invalid operands (e.g. a move past the end of the syringe) are reported by the next `Q`, and the server turns both into clear errors.
- **Initialization force** is chosen from the syringe size (full ≥ 1 mL, half for 250/500 µL, third for 50/100 µL; manual table 3-6) unless you pick one.
- **`terminate`** sends `T` without `R`: in the DT command set `R` resumes a terminated string. `T` does not stop a valve move in progress. Re-initialize after terminating. If a move or initialization is still busy well past its expected duration (1.5× + 10 s), or the link fails while waiting, the server sends `T` itself instead of leaving the plunger moving.
- The server does not use microstep mode (N2), the OEM protocol (checksums, sequence numbers) or CAN.
- **Cavro Centris** has a different resolution (181,490 increments per stroke) and volume-based firmware commands that we could not verify, so it is not supported. XE 1000 and older XL/XP 3000 pumps may work with `--option steps_per_stroke=…`, but they are untested.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
