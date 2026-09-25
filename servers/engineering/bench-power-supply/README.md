# Bench DC Power Supply — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-bench-psu -->

Let an AI agent set voltages and current limits, switch outputs on and off, watch constant-voltage / constant-current behaviour and configure over-voltage / over-current protection on **programmable DC bench power supplies** from **Rigol**, **Siglent** and **Aim-TTi**, using each vendor's documented remote command set. The dialect is detected from `*IDN?`.

| | |
|---|---|
| **Package** | `labmcp-bench-psu` |
| **Instruments** | Rigol DP832/DP832A, DP831, DP822, DP821, DP811, DP813, DP711, DP712, DP932A/U/E; Siglent SPD3303X/X-E, SPD1168X, SPD1305X; Aim-TTi CPX400D/DP, MX100T/TP, MX180T/TP, QL355/QL564 (P, T/TP), PL-P (PL068/155/303/601, PL303QMD/QMT) |
| **Interfaces** | USB (USB-TMC via VISA, or virtual COM on Aim-TTi), LAN (VISA/VXI-11 or raw socket), RS-232, GPIB via VISA |
| **Protocol** | SCPI ([Rigol DP800 Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/DP800_ProgrammingGuide_EN.pdf), [DP700](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/DP700_ProgrammingGuide_EN.pdf), [DP900](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/DP900_ProgrammingGuide_en.pdf); [Siglent SPD3303X Quick Start](https://siglentna.com/wp-content/uploads/dlm_uploads/2022/11/SPD3303X_QuickStart_E02A.pdf), [SPD1000X User Manual](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/06/SPD1000X_UserManual_E03B_0613.pdf)) and Aim-TTi remote commands ([CPX400D/DP manual](https://resources.aimtti.com/manuals/CPX400D+DP_Instruction_Manual_EN_48511-1480_14.pdf), [MX100T/TP manual](https://resources.aimtti.com/manuals/MX100T+MX100TP_Instruction_Manual-Iss6.pdf), [manuals index](https://resources.aimtti.com/manuals/)) |
| **Status** | 🧪 **simulated**: tested against wire-level simulators of each dialect driving a resistive load, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

> **Keysight / Agilent and Rohde & Schwarz supplies** are intentionally not covered: both vendors publish official MCP servers. Use the [Keysight MCP Server for Instrument Control](https://www.keysight.com/us/en/lib/resources/software-releases/keysight-mcp-server-for-instrument-control.html) or the MCP server included in [Rohde & Schwarz RsInstrument](https://github.com/Rohde-Schwarz/RsInstrument). This server refuses to connect to them.

## Try it without hardware

```bash
uvx labmcp-bench-psu --simulate --check                                        # Rigol DP832
uvx labmcp-bench-psu --simulate --option dialect=siglent --check               # Siglent SPD3303X
uvx labmcp-bench-psu --simulate --option dialect=tti --option sim_model=MX100TP --check
```

The simulated outputs drive resistive loads (CH1 10 Ω, CH2 100 Ω, CH3 open), so you can watch the CV → CC crossover and trip OVP/OCP.

## Connect your instrument

1. **Instrument setup:** enable the interface you use (Rigol: Utility → I/O Config; Siglent: System → Interface; Aim-TTi: LAN works out of the box, USB appears as a virtual COM port). Note the IP address or VISA resource string.
2. **Find the address:** `uvx labmcp ports` (serial) or `python -m pyvisa info` / your VISA vendor's tool (USB/LAN).
3. **Test the connection:**
   ```bash
   uvx labmcp-bench-psu --address "USB0::0x1AB1::0x0E11::DP8C123456789::INSTR" --check   # Rigol over USB-TMC
   uvx labmcp-bench-psu --address "TCPIP0::192.168.1.50::INSTR" --check                 # any LXI/VXI-11 supply
   uvx labmcp-bench-psu --address tcp://192.168.1.51:5025 --check                       # Siglent raw socket
   uvx labmcp-bench-psu --address tcp://192.168.1.52:9221 --check                       # Aim-TTi raw socket
   uvx labmcp-bench-psu --address /dev/ttyACM0 --check                                  # Aim-TTi USB virtual COM
   uvx labmcp-bench-psu --address "serial://COM3?write_termination=CRLF" --check        # Rigol DP800 RS-232
   ```
   USB-TMC and VXI-11 addresses use PyVISA (installed with this package; add `?backend=@ivi` to use NI-VISA / Keysight IO Libraries instead of pyvisa-py).

## Add to your MCP client

**Claude Code**
```bash
claude mcp add psu -- uvx labmcp-bench-psu --address "TCPIP0::192.168.1.50::INSTR" --limit max_voltage_v=12 --limit max_current_a=1
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "psu": {
      "command": "uvx",
      "args": ["labmcp-bench-psu", "--address", "TCPIP0::192.168.1.50::INSTR",
               "--limit", "max_voltage_v=12", "--limit", "max_current_a=1"]
    }
  }
}
```

Add `--read-only` to allow monitoring only: setpoint changes and `output_on` are hidden, while `output_off` and `all_outputs_off` stay available. For other clients, generate the snippet with `uvx labmcp config bench-psu --address TCPIP0::192.168.1.50::INSTR --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `all_outputs_off` | 🛑 safety | Switch every output OFF (emergency stop for the whole supply). Each channel is switched individually and read back where the instrument supports it. |
| `clear_protection_trip` | 🎛 control | Clear an OVP/OCP trip after fixing its cause. The output stays OFF; use `output_on` to re-energise. On Aim-TTi supplies this clears trips on all outputs. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_errors` | 👁 read | Read (and clear) the instrument's error queue / error registers. Empty list = no errors. |
| `get_outputs` | 👁 read | Report each channel: voltage and current-limit setpoints, measured voltage/current/power, output on/off, CV/CC mode, and OVP/OCP levels and trip state where the model supports them. |
| `output_off` | 🛑 safety | Switch a channel's output OFF. Safe to call at any time. |
| `output_on` | ⚠️ hazard | Switch a channel's output ON, energising the connected load at the present setpoints. The setpoints are read back first and the output is refused if they exceed the safety limits (e.g. after someone changed them on the front panel). |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `set_current_limit` | 🎛 control | Set a channel's current limit (the constant-current setpoint). Refused above `max_current_a` (checked even with the output off) or above the channel's range. |
| `set_protection` | 🎛 control | Set over-voltage (OVP) and/or over-current (OCP) protection for a channel. When exceeded, the supply switches the output off. Rigol: levels + on/off; Aim-TTi CPX/QL/PL-P: levels (always armed), MX: levels + on/off; Siglent SPD1000X: levels only; SPD3303X: not available. |
| `set_voltage` | 🎛 control | Set a channel's voltage setpoint. Refused above `max_voltage_v` (checked even with the output off) or above the channel's range. If the output is on, the load sees the new voltage at once. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_voltage_v` | 30 V | Largest voltage setpoint (magnitude) on any channel. Checked when setting the voltage, **even with the output off**, and again before `output_on` |
| `max_current_a` | 3 A | Largest current-limit setpoint on any channel, checked the same way |

Override at launch, e.g. `--limit max_voltage_v=12 --limit max_current_a=0.5`. Setpoints are also checked against each model's settable range. `output_on` reads the setpoints back from the instrument first, so a value changed on the front panel cannot slip past the limits.

## Example prompts

- "Show me all channels of the power supply: setpoints, measured current and whether they're in CV or CC."
- "Set CH1 to 12 V with a 200 mA current limit, set OVP at 13 V, then turn it on and tell me how much current the LED array draws."
- "Ramp CH2 from 0 to 5 V in 0.5 V steps, reading the current at each step, and plot the I-V curve of the load."
- "The heater is drawing more than expected: turn every output off now."
- "Channel 1 tripped. What happened, and is it safe to clear the trip?"

## Notes

- **Dialect detection:** `*IDN?` manufacturer "RIGOL" → Rigol, "Siglent" → Siglent, "THURLBY THANDAR"/"TTi"/"Aim" → Aim-TTi. Force one with `--option dialect=rigol|siglent|tti` for rebadged units. Models not in the built-in table are treated as `--option channels=N` channels with no model maxima (the safety limits still apply).
- **Rigol:** setpoints via `:SOUR<n>:VOLT/CURR`, readback via `:APPL? CHn` and `:MEAS:ALL? CHn`, mode via `:OUTP:MODE? CHn` (CV/CC/UR), OVP/OCP via `:OUTP:OVP|OCP[:STAT|:VAL|:QUES?]`. Trips are cleared with `:OUTP:OVP|OCP:CLEAR`, which only clears the flag. `[:SOUR]:VOLT:PROT:CLE` is never sent because the programming guide says it also turns the output on. DP831 CH3 is a negative output: give negative voltages (not yet verified on hardware). Over RS-232 the DP800 needs CR LF line endings (`?write_termination=CRLF`).
- **Siglent:** SPD3303X/X-E have **no** programmable OVP/OCP and report output state and CV/CC only through the `SYST:STAT?` status word (CH1/CH2). CH3 is fixed (2.5/3.3/5 V by front-panel switch) and can only be switched on/off; its state is not readable. SPD1000X has OVP/OCP levels only (always armed, trip state not readable). The error reply format (`0 No Error`) is not SCPI-standard, so settings are verified by reading them back. The server pauses 0.1 s after each command to Siglent supplies (`--option write_delay_s=` to change); Siglent's own examples wait between commands, but the required delay is not documented.
- **Aim-TTi:** there is no live CV/CC query, so `mode` is inferred from readback vs setpoints (`mode_source: "inferred"`). Trip flags come from the `LSR<n>?` limit event register, which latches events and is cleared by reading it. CPX/QL/PL-P protection is always armed (levels only); MX models can also switch OVP/OCP on/off. `TRIPRST` clears trips on all outputs (not documented for MX100T/TP). Original QL P/T models have no output-state query, so their `output_on` is reported as unknown. `OPALL 0` is sent first in `all_outputs_off`, then every output individually.
- Tracking / series / parallel modes, timers, sequences, remote sense and saved set-ups are not exposed. Put the supply in independent mode before handing it to an agent.
- GW Instek GPP and B&K Precision 9130/9140 supplies are not supported yet: too many reply formats are undocumented in their manuals to implement them safely.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
