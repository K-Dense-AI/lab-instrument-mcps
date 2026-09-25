# Alicat Mass Flow & Pressure Controller — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-alicat -->

Let an AI agent read gas flow, pressure and temperature, set flow or pressure setpoints, switch the gas calibration, tare, and hold or close valves on **Alicat Scientific** mass flow meters and controllers and pressure controllers through Alicat's documented **ASCII serial** protocol.

| | |
|---|---|
| **Package** | `labmcp-alicat` |
| **Instruments** | Mass flow controllers (MC, MCR, MCS, MCE, MCV, MCW, ...), mass flow meters (M, MS, MW, ...), pressure controllers and gauges (PC, PCD, P), liquid flow devices (L, LC): any Alicat with RS-232/RS-485 ASCII serial (not Modbus/Profibus/EtherNet/IP builds) |
| **Interfaces** | RS-232, RS-485 (up to 26 devices per port, unit IDs A-Z), USB (virtual COM port), Ethernet via a serial-to-Ethernet bridge |
| **Protocol** | Alicat ASCII serial ([Serial Communications Primer, Rev. 2, Feb 2023](https://documents.alicat.com/Alicat-Serial-Primer.pdf); [Operating Manual DOC-MANUAL-MPL, Rev. 2](https://documents.alicat.com/manuals/DOC-MANUAL-MPL.pdf), "Digital Control") |
| **Status** | 🧪 **simulated**: tested against a wire-level Alicat simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-alicat --simulate --check
```

The simulator is an MC-500SCCM-D on firmware 10v20 flowing N2, with a first-order flow response, a small sensor zero offset (remove it with `tare_flow`) and realistic data frames.

## Connect your instrument

1. **Instrument setup:** the factory settings are unit ID **A**, **19200 baud**, 8 data bits, no parity, 1 stop bit, no flow control (MENU → SETUP → RS-232 Serial / RS-485 Serial). If several devices share one RS-485 port, give each a different unit ID. The device must be in **polling** mode (a letter ID, not `@` streaming).
2. **Controllers:** set MENU → CONTROL → Setpoint Setup → **Setpoint Source** to *Serial/Front Panel*. With an analog setpoint source the controller silently ignores serial setpoints (`get_device_info` warns about this).
3. **Find the port:** `uvx labmcp ports`
4. **Test the connection:**
   ```bash
   uvx labmcp-alicat --address /dev/ttyUSB0 --check                   # Linux
   uvx labmcp-alicat --address /dev/tty.usbserial-XXXX --check        # macOS
   uvx labmcp-alicat --address COM4 --check                           # Windows
   uvx labmcp-alicat --address COM4 --option unit_id=B --check        # device B on a shared bus
   uvx labmcp-alicat --address tcp://192.168.1.60:4001 --check        # serial-to-Ethernet bridge
   ```
   Non-default baud rates go in the address: `serial://COM4?baudrate=115200`.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add alicat -- uvx labmcp-alicat --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "alicat": {
      "command": "uvx",
      "args": ["labmcp-alicat", "--address", "/dev/ttyUSB0", "--limit", "max_setpoint=200"]
    }
  }
}
```

Add `--read-only` to allow reading flow but block setpoints, gas changes, tares and valve holds (`close_valve` stays available). For several devices on one bus, add one server entry per unit ID (`--option unit_id=A`, `--option unit_id=B`, ...). For other clients, generate the snippet with `uvx labmcp config alicat --address /dev/ttyUSB0 --client vscode`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `close_valve` | 🛑 safety | Stop the flow: set the flow setpoint to 0 and hold all valves closed (Alicat HC). Safe to call at any time. On pressure controllers the pressure setpoint is left unchanged and the valves are held closed, trapping the current pressure. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Report model, serial number, firmware, calibration date, whether the device is a controller, the units of every data-frame field, the setpoint full scale and setpoint source. |
| `hold_valve` | 🎛 control | Freeze the controller's valve(s) at their current position (HLD): closed-loop control stops, so flow will drift if upstream pressure changes. Use `resume_control` to release, or `close_valve` to shut off completely. |
| `list_gases` | 👁 read | List the gases installed on this mass flow device (number and short name), as used by `set_gas`. Liquid and pressure-only devices have no gas list. |
| `log_flow_series` | 👁 read | Record a time series of data frames (e.g. to check flow stability, settling after a setpoint change, or pressure drift). Returns every point plus mean/stdev/min/max of the mass flow (or the main measured quantity for non-flow devices). |
| `read_flow` | 👁 read | Read the live data frame: mass flow, volumetric flow, pressure, temperature, setpoint (controllers), active gas and any status codes, each with the device's engineering units. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `resume_control` | ⚠️ hazard | Cancel any valve hold and resume closed-loop control to the current setpoint. Flow restarts immediately if the setpoint is not 0 (check it with `read_flow` first). |
| `set_flow_setpoint` | ⚠️ hazard | Change the controller setpoint: starts, changes or stops gas flow (or sets the target pressure on a pressure controller). 0 stops flow and closes the valve on a flow controller. Negative values only work on bidirectional controllers. Refused on meters, above the `max_setpoint` limit, and above the device's full scale. |
| `set_gas` | 🎛 control | Select the gas calibration the mass flow device uses (Gas Select). This changes how flow is computed, so do it with the setpoint at 0. Only gases installed on the device work (see `list_gases`); corrosive gases need anti-corrosive (S-series) hardware. |
| `tare_flow` | 🎛 control | Tare (zero) the flow reading. ONLY run with no flow through the device: upstream shut off or controller setpoint 0, with the line at its normal operating pressure. Refused if the setpoint is not 0 or a significant flow is still being measured. |
| `tare_pressure` | 🎛 control | Tare a pressure reading. Requires no flow and the device OPEN TO ATMOSPHERE (gauge), or no flow and an unpressurised process line (absolute, barometer-equipped devices only). |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_setpoint` | 100 (device units) | Largest setpoint magnitude an agent may send, **in the controller's own setpoint units** (SCCM, SLPM, PSIA, ... as shown by `get_device_info`). Setpoints above the device's full scale are refused as well. |
| `max_series_duration_s` | 600 s | Longest logging series an agent may start |

Override at launch, e.g. `--limit max_setpoint=500`. The default of 100 is deliberately conservative: on a 500 SCCM controller it caps the agent at 100 SCCM until you raise it.

## Example prompts

- "What is this controller, what gas is it set to, and what are its units and full scale?"
- "Set the N2 flow to 50 SCCM, log it every 0.5 s for 30 s and tell me how long it took to settle within 1%."
- "Switch the controller to CO2, then ramp the setpoint 0 → 20 → 40 → 60 SCCM, holding each step for 2 minutes, and summarise the mean flow and pressure at each step."
- "The flow reads 0.8 SCCM with the gas off. Close the valve, then tare the flow sensor."
- "Something is wrong with the bioreactor gassing: stop the flow now."

## Notes

- **Data frame parsing.** On connect the server asks the device for its data-frame definition (`??D*`) and parses every poll against it, so field names, order and engineering units come from the device itself (both the 6v+ column table and the older pre-6v table are understood). If a device does not answer `??D*`, frames are parsed by position using the default layouts in the Serial Primer (p. 8) and the manual; `layout_source` in every reading says which method was used.
- **Setpoints** use `LS` on firmware 9v00+ and `S` on older firmware. After every change the reply is checked: if the controller reports a different requested setpoint (out of range, negative on a unidirectional device, or an analog setpoint source), the tool reports an error with what the device is now using.
- **`close_valve`** sets the flow setpoint to 0 (on a flow controller this closes the valve) and then sends `HC` (hold valves closed, firmware 5v07+), and confirms `HLD` in the reply. On a **pressure controller** it only sends `HC`, because a 0 pressure setpoint can *open* valves depending on the zero-pressure-control setting (Serial Primer p. 17). `resume_control` (`C`) releases the hold; it is marked as a hazard because flow restarts at the current setpoint.
- **Tares** must be done with no flow. `tare_flow` and `tare_pressure` refuse if a flow controller's setpoint is not 0, or if the device still reads more than 2% of full scale. They cannot detect every unsafe situation (e.g. a meter with a small real flow), so still shut the gas off first. Gauge-pressure tares need the port open to atmosphere; absolute tares need the barometer option and an unpressurised line.
- **Gas selection** uses `GS` (10v05+, not saved as the power-up gas unless `save_as_power_up`) or the older `G`. Only gases installed on the device are accepted; `list_gases` shows them.
- **Safety guard:** a unit ID followed by digits (e.g. `A49408`) is Alicat's legacy *integer setpoint* command. The driver refuses to send anything of that shape.
- Meters and gauges silently ignore controller commands; the server refuses controller tools on devices whose data frame has no setpoint.
- Not supported: GP-firmware devices (they need `$$` after the unit ID), streaming mode (`@`), Modbus/Profibus/EtherNet/IP variants, totalizer, batching, PID tuning and COMPOSER mix creation.
- Frame layouts and reply formats were cross-checked against replies captured on real 5v12 to 10v20 devices published by the open-source [alicatlib](https://github.com/GraysonBellamy/alicatlib) project and the [numat/alicat](https://github.com/numat/alicat) driver.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
