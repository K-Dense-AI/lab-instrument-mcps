# Lake Shore Temperature Controller — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-lakeshore -->

Let an AI agent read every sensor on your cryostat, check heater status, set setpoints, ramps, heater ranges and PID values, wait for the temperature to settle, and switch all heaters off on **Lake Shore Cryotronics Model 335, 336 and 350** temperature controllers.

| | |
|---|---|
| **Package** | `labmcp-lakeshore` |
| **Instruments** | Model 335 (2 inputs, 2 outputs), Model 336 (4 inputs, 4 outputs), Model 350 (4 inputs, 4 outputs) |
| **Interfaces** | USB (virtual COM port), Ethernet (336 and 350, TCP port 7777), IEEE-488 (GPIB via VISA) |
| **Protocol** | Lake Shore ASCII commands, chapter 6 of the [Model 336](https://www.lakeshore.com/docs/default-source/product-downloads/336manual.pdf) (rev. 2.3), [Model 335](https://www.lakeshore.com/docs/default-source/product-downloads/335_manual.pdf) (rev. 1.6) and [Model 350](https://www.lakeshore.com/docs/default-source/software/manuals/350_manual.pdf) (rev. 1.7) user's manuals |
| **Status** | 🧪 **simulated**: tested against a wire-level simulator with a thermal model, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-lakeshore --simulate --check
uvx labmcp-lakeshore --simulate --option sim_model=350 --option sim_speed=20 --check
```

The simulator models a small cryostat at 4.2 K: a stage (inputs A diode and C Cernox-like, heated by output 1) and a sample (input B Pt-100, heated by output 2), with Lake Shore's PID law and setpoint ramping. `sim_speed` makes simulated time run faster than real time.

## Connect your controller

The model is detected from `*IDN?`; other Lake Shore models (325, 331, 340, 372, ...) are refused.

1. **Pick the interface** in the controller's Interface menu (only one remote interface is active at a time).
2. **USB:** install the Lake Shore USB driver (Windows Update, or lakeshore.com). The port runs at a fixed 57600 baud, 7 data bits, odd parity, 1 stop bit; the server sets this automatically.
3. **Ethernet (336, 350):** configure the IP address on the front panel; the command port is TCP 7777 (two connections at most).
4. **Test the connection:**
   ```bash
   uvx labmcp-lakeshore --address /dev/ttyACM0 --check          # Linux
   uvx labmcp-lakeshore --address /dev/tty.usbmodemXXXX --check # macOS
   uvx labmcp-lakeshore --address COM5 --check                  # Windows
   uvx labmcp-lakeshore --address tcp://192.168.1.40:7777 --check
   uvx labmcp-lakeshore --address GPIB0::12::INSTR --check
   ```

## Add to your MCP client

**Claude Code**
```bash
claude mcp add cryostat -- uvx labmcp-lakeshore --address tcp://192.168.1.40:7777
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "cryostat": {
      "command": "uvx",
      "args": ["labmcp-lakeshore", "--address", "tcp://192.168.1.40:7777"]
    }
  }
}
```

Add `--read-only` to allow reading temperatures and heater status but block setpoint, ramp, range and PID changes (`all_heaters_off` stays available). For other clients, generate the snippet with `uvx labmcp config lakeshore --address tcp://192.168.1.40:7777 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `all_heaters_off` | 🛑 safety | Turn every output off (heater range 0 on outputs 1-4), like the front-panel All Off key, and stop any running wait. Reports the read-back range of each output. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_heater_status` | 👁 read | Report each output's control mode and input, heater range, output %, setpoint (and in kelvin), ramp state, PID values and heater errors (open/short). Reading a heater error clears it on the controller. |
| `read_temperatures` | 👁 read | Read every (or the selected) sensor input: kelvin, raw sensor units, sensor type, input name and decoded reading status (invalid, under/overrange). Disabled inputs are listed with null readings. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_heater_range` | ⚠️ hazard | Set an output's heater range (each step is ~10x more power). Anything above 0 lets the output heat: in closed loop as the PID demands, in open loop at the front-panel manual output. Refused above `max_heater_range`. Start with the lowest range that can reach the setpoint. |
| `set_pid` | 🎛 control | Set the P, I and D values of an output's control loop (Lake Shore conventions). |
| `set_ramp` | 🎛 control | Turn setpoint ramping on or off for an output's control loop and set the rate. With ramping on, the next setpoint change moves the setpoint gradually - gentler on samples and wiring. |
| `set_setpoint` | ⚠️ hazard | Set the control setpoint of an output's loop in kelvin. If setpoint ramping is on, the setpoint moves toward the new value at the ramp rate. Heating only happens if the output is in closed-loop mode and its heater range is not off. Refused above `max_setpoint_k`. |
| `wait_for_stable_temperature` | 👁 read | Wait until the control input of `output` has stayed within `tolerance_k` of the setpoint (and the setpoint is no longer ramping) for `stable_for_s`, or until `timeout_s`. Returns whether it stabilised, the final temperature and a downsampled trace. Changes nothing. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_setpoint_k` | 325 K | Highest setpoint `set_setpoint` accepts (room temperature plus margin). Raise it only if your stage, wiring and sensors are rated higher |
| `max_heater_range` | 3 | Highest heater range index. On the 335/336 3 = High (full power); on the 350 ranges go 1-5 in decade steps, so 3 is 1/100 of full power |
| `max_wait_s` | 3600 s | Longest `wait_for_stable_temperature` call |

Override at launch: `--limit max_setpoint_k=420 --limit max_heater_range=2`.

## Example prompts

- "Read all the temperatures and tell me if any sensor has an error."
- "Ramp output 1 to 77 K at 2 K/min on the medium range, then wait until input A has been within 50 mK for 5 minutes."
- "Step the sample from 10 K to 300 K in 10 K steps and tell me when each step is stable." (with `max_setpoint_k` high enough)
- "What are the PID settings on loop 1? Reduce P to 30 and keep I and D."
- "Something's wrong - turn all heaters off."

## Notes

- **Setting a setpoint does not start heating; the heater range does.** With a range above 0 an output heats as its PID loop demands (closed loop) or at the manual output (open loop). Output modes and control inputs are configured on the front panel; the server does not change them, since pointing a loop at the wrong sensor can cause a thermal runaway.
- Setpoints are always given in kelvin. `SETP` uses the preferred units of the loop's control input: the server converts for Celsius and refuses when the input is set to sensor units.
- Every command is followed by `*ESR?`; command (CME) and execution (EXE) errors are reported instead of being ignored. The server leaves at least 50 ms between messages, as the manuals require (`--option min_interval_s=`).
- `get_heater_status` reads `HTRST?`, which clears a latched heater open/short error on the controller.
- `wait_for_stable_temperature` needs the setpoint to have stopped ramping (`RAMPST?`) as well as the temperature to be in tolerance. `all_heaters_off` stops a running wait.
- `all_heaters_off` sends `RANGE n,0` to every output and reads it back, like the front-panel **All Off** key. It does not change setpoints or output modes. Consider also setting a hardware temperature limit (`TLIMIT`, front panel) as a last line of defence.
- The 3062 scanner option (inputs D1-D5), zone tables, autotune, curves, alarms and relays are not exposed. The Model 372 AC resistance bridge was not verified and is refused at connect time.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
