# IKA Hotplate & Overhead Stirrer — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ika -->

Let an AI agent heat and stir with **IKA hotplate stirrers** (C-MAG HS control, IKA Plate / RCT digital) and drive **IKA overhead stirrers** (EUROSTAR control) through IKA's documented **NAMUR** interface commands: read plate, probe and medium temperatures, speed and torque; set and start heating and stirring; wait for a temperature; and arm the hotplate's hardware watchdog so it switches itself off if the computer stops talking to it.

| | |
|---|---|
| **Package** | `labmcp-ika` |
| **Instruments** | Hotplate stirrers: C-MAG HS 7 control, IKA Plate (RCT digital). Overhead stirrers (`--option device=overhead`): EUROSTAR 60 control, EUROSTAR 100 control. Other IKA devices with the same NAMUR set are likely to work but are unverified |
| **Interfaces** | RS-232 (9-pin, PC 1.1 cable), USB (virtual COM port) |
| **Protocol** | IKA NAMUR commands, "Interfaces and outputs" section of the operating instructions: [IKA Plate (RCT digital)](https://shop.textalk.se/shop/ws72/72372/art12/156948912-a58526-20000015643_20014388a_A2_IKA_Plate_112018_web.pdf) (11/2018), [C-MAG HS 7 control](https://shop.textalk.se/shop/ws72/72372/art16/156948916-cc53a2-20000015641_20014381a_A2_C-MAG_HS_7_control_112018_web.pdf) (11/2018), [EUROSTAR 60/100 control](https://www.wolflabs.co.uk/documents/IKA_overhead-stirrers_Eurostar-60-100-control_manual.pdf) (20000003965c). Official downloads: [ika.com](https://www.ika.com/en/Products-LabEq/Magnetic-Stirrers-pg188/C-MAG-HS-7-control-20002694/Downloads-cpdl.html) |
| **Status** | 🧪 **simulated**: tested against a wire-level NAMUR simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-ika --simulate --check
uvx labmcp-ika --simulate --option device=overhead --check   # EUROSTAR simulator
```

## Connect your stirrer

1. **Cable:** USB A–B (the stirrer appears as a virtual COM port; on Windows install IKA's USB driver, linked in the manual) or the 9-pin RS-232 PC 1.1 cable. No setting on the device is needed. The interface is fixed at **9600 baud, 7 data bits, even parity, 1 stop bit, no flow control**, and every command ends with a blank + CR LF.
2. **Find the port:** `uvx labmcp ports`
3. **Test the connection:**
   ```bash
   uvx labmcp-ika --address /dev/ttyACM0 --check                          # Linux, hotplate
   uvx labmcp-ika --address /dev/tty.usbmodem14101 --check                # macOS
   uvx labmcp-ika --address COM5 --check                                  # Windows
   uvx labmcp-ika --address COM6 --option device=overhead --check         # EUROSTAR
   ```
   `--check` prints the name the device reports (`IN_NAME`).

## Add to your MCP client

**Claude Code**
```bash
claude mcp add ika -- uvx labmcp-ika --address /dev/ttyACM0 --limit max_temperature_c=120
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "ika": {
      "command": "uvx",
      "args": ["labmcp-ika", "--address", "/dev/ttyACM0", "--limit", "max_temperature_c=120"]
    }
  }
}
```

Add `--read-only` to allow monitoring only (the stop tools stay available). For other clients: `uvx labmcp config ika-stirrer --address /dev/ttyACM0 --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `disable_watchdog` | 🎛 control | Cancel watchdog mode 2 (OUT_WD2@0) and stop the background refresh. Watchdog mode 1 has no cancel command; this tool reports an error for it. |
| `enable_watchdog` | 🎛 control | Arm the hotplate's communication watchdog (OUT_WD1@m / OUT_WD2@m). The server then re-sends the watchdog command in the background; if the computer, this server or the cable fails, the hotplate switches heating and stirring off (mode 1) or falls back to the given safety values (mode 2) after `timeout_s`. Note: stopping or reconnecting the server also stops the refresh, so the watchdog trips unless you enable it again. Mode 1 cannot be cancelled over the interface. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_status` | 👁 read | Read temperatures, stirring speed and setpoints. On hotplates: plate and external-probe temperature, temperature setpoint and the device's safety-circuit temperature. On overhead stirrers: PT1000 probe temperature, torque and the speed/torque limits set on the device. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_speed` | ⚠️ hazard | Set the stirring speed setpoint (OUT_SP_4). Does not start the motor; if it is already running, the speed changes immediately. Refused above `max_speed_rpm` (and, on overhead stirrers, above the speed limit set on the device). |
| `set_temperature` | ⚠️ hazard | Set the hotplate temperature setpoint (OUT_SP_1). With an external probe connected this is the target temperature of the medium. Does not start heating; if heating is already on, the plate starts moving to the new setpoint immediately. Refused above `max_temperature_c` or above the device's own safety-circuit temperature. |
| `start_heating` | ⚠️ hazard | Switch the hotplate heater on (START_1). It heats towards the current setpoint, which is checked against `max_temperature_c` first. Make sure the vessel and its contents can take the setpoint temperature and that nothing flammable is near the plate. |
| `start_stirring` | ⚠️ hazard | Start the stirring motor (START_4) at the current speed setpoint, which is checked against `max_speed_rpm` first. Make sure the stir bar or impeller is in place and the vessel is secured (a decoupled stir bar or an unclamped vessel can splash). |
| `stop_all` | 🛑 safety | Emergency stop: switch the heater off (hotplates) and stop the motor. Every stop command is sent even if one of them fails. Also ends a running `wait_for_temperature`. |
| `stop_heating` | 🛑 safety | Switch the hotplate heater off (STOP_1). Stirring continues. The plate stays hot for a long time after switching off. |
| `stop_stirring` | 🛑 safety | Stop the stirring motor (STOP_4). Heating (if on) continues. |
| `wait_for_temperature` | 👁 read | Poll a temperature until it is within `tolerance_c` of the target for `stable_for_s` seconds, or until `timeout_s` passes. Does not change anything on the device (start heating first). Returns whether the target was reached plus a short temperature trace. A stop command ends the wait early. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_temperature_c` | 150 °C | Highest hotplate setpoint an agent may set (also checked before `start_heating`, and for the watchdog fallback temperature) |
| `max_speed_rpm` | 1000 rpm | Highest stirring speed an agent may set (also checked before `start_stirring`) |
| `max_wait_s` | 3600 s | Longest `wait_for_temperature` |

Override at launch, e.g. `--limit max_temperature_c=250 --limit max_speed_rpm=1500`. On top of these, the hotplate refuses setpoints above its own safety-circuit temperature (set with the screwdriver dial on the device, read with `IN_SP_3`), and overhead stirrers refuse speeds above the speed limit set in the device menu (`IN_SP_6`); the server checks both before sending.

## Example prompts

- "Heat the round-bottom flask to 65 °C using the external probe, stir at 400 rpm, and tell me when it has been stable for a minute."
- "Arm the watchdog with 60 s, then start heating to 80 °C. I'm leaving it running overnight."
- "Log the medium temperature every 30 s while it cools down and tell me when it is below 30 °C."
- "Ramp the EUROSTAR up to 300 rpm and report the torque; the mixture is getting viscous."
- "Something smells wrong, stop everything now."

## Notes

- **Two device types.** The default is `--option device=hotplate`. Overhead stirrers need `--option device=overhead`; they have no heater, report their PT1000 probe on `IN_PV_3` and torque on `IN_PV_5`. Tools that don't apply to the configured type return a clear error and send nothing.
- **No state query for the heater.** The NAMUR set has no "is the heater on?" command, so `get_status` reports the last start/stop *this server* sent (`heating_commanded`, `stirring_commanded`) and `None` when unknown. The front-panel buttons can change it without the server knowing.
- **Setpoints are read back** after `set_temperature` / `set_speed` (`IN_SP_1` / `IN_SP_4`), and a `warning` is returned if the device reports a different value.
- **Watchdog** (hotplates, `OUT_WD1@m` / `OUT_WD2@m`, 20–1500 s): `enable_watchdog` arms it and the server re-sends the command in the background every quarter of the watchdog time. If the server or the computer dies or the cable is pulled, the hotplate switches heating and stirring off (mode 1) or falls back to the safety temperature and speed (mode 2). **Stopping or reconnecting the server also stops the refresh**, so the watchdog trips. Mode 1 has no documented cancel command; mode 2 is cancelled with `OUT_WD2@0`. The manual's watchdog error codes differ between sections (ER 2 vs Err. 1/Err. 2); a trip is visible on the display, not over the interface.
- **Unverified reply layout.** The IKA manuals document the commands but not the exact reply line. The driver takes the first number on the line (devices are reported to append the channel, e.g. `25.3 2`), so both forms work. The value reported by `IN_PV_1` when no external probe is plugged in is not documented; ignore `external_temperature_c` if you have no probe.
- The IKA manuals print the terminator as "Blank CR LF (hex 0x20 0x0d 0x20 0x0A)"; the extra 0x20 contradicts the text ("Blank CR LF") and is treated as a typo, so the server sends `0x20 0x0D 0x0A`.
- Not implemented: `SET_MODE_n` (changes the power-on operating mode A/B/D, a persistent setting) and `RESET`; overhead-stirrer torque/speed-limit writes (`OUT_SP_5/6/8`) and direction change (`OUT_MODE_n`).

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
