# Opentrons OT-2 / Flex — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-opentrons -->

Let an AI agent upload and check protocols, review the deck layout with you, start, pause, stop and resume runs, follow their progress, and read pipettes and modules on **Opentrons Flex and OT-2** liquid-handling robots through the robot's documented **HTTP API**.

| | |
|---|---|
| **Package** | `labmcp-opentrons` |
| **Instruments** | Opentrons Flex, Opentrons OT-2 (robot software 5.1 or newer) |
| **Interfaces** | Ethernet, Wi-Fi, USB (network over USB) |
| **Protocol** | Opentrons robot-server HTTP API on port 31950, `Opentrons-Version: 3` ([HTTP API guide](https://github.com/Opentrons/opentrons-integration-tools/tree/main/http-api), [robot-server source](https://github.com/Opentrons/opentrons/tree/edge/robot-server); every robot also serves its spec at `http://<robot-ip>:31950/openapi.json` and `/redoc`) |
| **Status** | 🧪 **simulated**: tested against an in-memory robot-server simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

Why this server rather than a generic HTTP wrapper: it refuses to start a run unless the robot's own protocol analysis passed, the user has confirmed the deck, the door is closed, the E-stop is released and module setpoints are within your limits. It exposes no single-move pipetting commands, keeps pause and stop available even in read-only mode, and records every HTTP request in the command log.

## Try it without hardware

```bash
uvx labmcp-opentrons --simulate --check
uvx labmcp-opentrons --simulate --option sim_model=ot2 --check   # simulate an OT-2
```

The simulator runs protocols in memory: Python protocols are compiled and scanned for labware, pipettes, modules and steps, runs go through the real status lifecycle, and module temperatures respond to the protocol.

## Connect your robot

1. **Find the IP address:** Opentrons App > Devices > your robot > Robot settings > Networking (or on the Flex touchscreen: Settings > Network). The robot must be on the same network as the computer running the MCP server. USB-connected robots also get an IP address (OT-2: `169.254.x.x`).
2. **Test the connection:**
   ```bash
   uvx labmcp-opentrons --address 192.168.1.20 --check
   uvx labmcp-opentrons --address http://ot2-lab.local:31950 --check
   ```
   `--check` prints the robot's name, model, software and firmware versions.
3. **Access control (Flex only, rarely needed):** if your Flex has access control enabled and requests fail with HTTP 401, pass a token with `--option access_token=<token>`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add opentrons -- uvx labmcp-opentrons --address 192.168.1.20
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "opentrons": {
      "command": "uvx",
      "args": ["labmcp-opentrons", "--address", "192.168.1.20"]
    }
  }
}
```

Add `--read-only` to let the agent monitor the robot (status, runs, protocols, modules) while blocking uploads, runs, homing and lights. `pause_run`, `stop_run` and `deactivate_modules` stay available. For other clients, generate the snippet with `uvx labmcp config opentrons --address 192.168.1.20 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `deactivate_modules` | 🛑 safety | Switch attached modules off: stop heating/cooling (Temperature Module, Thermocycler block and lid, Heater-Shaker heater), stop shaking, and lower Magnetic Module magnets. Only possible when no run is in progress: stop the run first. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_protocol` | 👁 read | Show a stored protocol's analysis: whether it is ready to run, analysis errors, the deck layout it expects (labware and modules per slot, pipettes per mount, liquids), the number of steps and the highest module temperature / shake speed it requests. Review this with the user before start_run. |
| `get_robot_status` | 👁 read | Report the robot's name, model (Flex/OT-2), software and firmware versions, lights, door and E-stop state, and the current run. Call this first to check the robot is ready. |
| `get_run_status` | 👁 read | Report a run's status, the step it is on, progress, recent steps and any errors, plus advice on what to do next (e.g. when the run is waiting for error recovery). |
| `home_robot` | ⚠️ hazard | Home all axes of the robot (the gantry and pipettes move to their home positions). Refused while a run is active. Make sure nothing is in the robot's path and the door is closed. |
| `list_instruments` | 👁 read | List attached pipettes (and the Flex gripper): mount, name, channels, volume range, and whether a tip is detected and calibration data exists. |
| `list_modules` | 👁 read | List attached modules (Temperature Module, Heater-Shaker, Thermocycler, Magnetic Module, Absorbance Plate Reader, ...) with live temperatures, targets, shake speed and status. |
| `list_protocols` | 👁 read | List protocols stored on the robot (newest last) with their analysis status and result. |
| `list_runs` | 👁 read | List recent protocol runs, newest first, with their status. |
| `pause_run` | 🛑 safety | Pause a running protocol. The robot finishes its current step and then holds; resume_run continues it. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `resume_run` | ⚠️ hazard | Resume a paused run (the robot starts moving again), or leave error recovery. Refused while the door is open. For a run awaiting recovery, `error_recovery` must be given. |
| `set_lights` | 🎛 control | Turn the robot's deck (rail) lights on or off. |
| `start_run` | ⚠️ hazard | Create a run of an analyzed protocol and start it: the robot begins moving and pipetting. Refused unless the protocol's analysis completed without errors, its module setpoints are within the safety limits, no other run is active, the door is closed and the E-stop is released. |
| `stop_run` | 🛑 safety | Stop (cancel) a run immediately. A stopped run cannot be resumed; the robot homes and drops any attached tips into the trash. Use this whenever something looks wrong. |
| `upload_protocol` | 🎛 control | Upload a protocol file (plus optional custom labware) to the robot. The robot analyzes it (simulates it) and this tool returns the analysis: errors, deck layout and whether it is ready to run. Uploading does not move the robot. Uploading identical files returns the existing protocol. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. `get_command_log` shows every HTTP request sent to the robot and its reply.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_module_temperature_c` | 110 °C | Highest temperature (Temperature Module, Thermocycler block or lid, Heater-Shaker) a protocol may request; `start_run` refuses protocols above it |
| `max_shake_speed_rpm` | 3000 rpm | Fastest Heater-Shaker speed a protocol may request |

The defaults are the hardware maxima. Tighten them for heat-sensitive plastics or samples: `--limit max_module_temperature_c=70 --limit max_shake_speed_rpm=1500`. The check reads the setpoints from the robot's analysis of the protocol (every `celsius` and `rpm` value in module commands, including thermocycler profiles).

Other safety interlocks built into `start_run`:

- the robot's analysis must have completed with result `ok` (pending, failed or parameter-required analyses are refused, and the errors are shown);
- `deck_confirmed=true` is required, which the agent should only pass after you confirmed the deck matches the layout from `get_protocol`;
- no other run may be active, the Flex door must be closed and the E-stop released.

## Example prompts

- "Upload `~/protocols/serial_dilution.py` to the Flex and tell me whether the analysis passed. What labware do I need where?"
- "The deck is set up as you listed. Start the serial dilution run and tell me when it's halfway."
- "What is the robot doing right now? How many steps are left?"
- "Pause the run — I need to top up the reservoir." … "OK, resume."
- "The run says a tip is missing. I've refilled the tip rack in slot C2; continue the run."
- "The PCR finished. Turn off the thermocycler and the heater-shaker, then switch the lights off."

## Notes

- **Protocols, not single moves.** This server runs complete protocols that the robot has analyzed. It deliberately has no tools for individual pipetting or gantry moves (`POST /runs/{id}/commands`), because without full deck state these can crash pipettes into labware. That may come later behind stricter checks.
- **Labware Position Check offsets are not applied.** Runs created through the HTTP API do not include the offsets the Opentrons App applies from Labware Position Check. If your setup depends on them, start the run from the App, or set offsets in the protocol (`labware.set_offset(...)`).
- **Run-time parameters** use their default values. Protocols that need a value this server cannot give (e.g. a CSV file parameter) are reported as not ready.
- **Stopping** a run is final: the robot homes and drops tips in the trash. Pausing waits for the current step to finish.
- **Error recovery** (recent robot software, mainly Flex): when a step fails (e.g. no tip detected), the run waits in `awaiting-recovery`. `resume_run(error_recovery="continue")` continues from the robot's actual state and skips the failed step; `"assume_false_positive"` treats the failure as a false alarm. Only use either after someone has checked the robot.
- **Modules stay on** after a protocol ends unless the protocol switches them off. `deactivate_modules` sends the robot's stateless deactivate commands (`POST /commands`), which the robot only accepts when no run is in progress.
- `list_instruments` uses `GET /instruments`, which the robot fully supports on Flex; on an OT-2 it returns pipettes with less detail (no calibration data).
- The HTTP API version is pinned with the `Opentrons-Version: 3` header, as in Opentrons' integration examples. Override with `--option api_version=4` if needed.

## Hardware verification

| Model | Robot software | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
