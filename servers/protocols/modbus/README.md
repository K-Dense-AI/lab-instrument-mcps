# Modbus TCP/RTU Device — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-modbus -->

Let an AI agent read and control **Modbus devices** over Ethernet (Modbus TCP) or RS-485/RS-232 (Modbus RTU/ASCII): PID temperature controllers, recirculating chillers, PLCs, process sensors, VFDs and data loggers. You describe the device once in a **register map** (YAML or JSON). The agent then works with named points in real units, such as `process_temperature = 37.2 °C` or `setpoint`, instead of raw registers. The server enforces the map's rules: only `writable` points can be written by name, every writable number must have a `min` and `max`, and out-of-range values are refused before anything is sent. The map can also define a **safe state**, a list of point writes (heater off, controller to standby) that a SAFETY tool runs on request.

| | |
|---|---|
| **Package** | `labmcp-modbus` |
| **Instruments** | Any Modbus device: temperature controllers (Watlow, Eurotherm, Omega, Autonics, …), chillers and circulators, PLCs and remote I/O, sensors/transmitters, VFDs, power meters, data loggers |
| **Interfaces** | Ethernet (Modbus TCP, port 502), RS-485 / RS-232 (Modbus RTU or ASCII), serial-to-Ethernet gateways (RTU over TCP) |
| **Protocol** | [MODBUS Application Protocol Specification V1.1b3](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf), [MODBUS over Serial Line V1.02](https://www.modbus.org/file/secure/modbusoverserial.pdf), [MODBUS Messaging on TCP/IP Implementation Guide V1.0b](https://www.modbus.org/file/secure/messagingimplementationguide.pdf); client via [pymodbus](https://pymodbus.readthedocs.io/) 3.8+ |
| **Status** | 🧪 **simulated**: tested against an in-memory simulated controller and a local pymodbus server, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

What this server adds over a plain register-poking Modbus MCP:

- **Typed register maps:** int16/uint16/int32/uint32/float32/float64/bool, word and byte order, `scale` and `offset`, units, enums (`0 = standby, 1 = auto`) and descriptions.
- **Per-point limits:** writable points need `min`/`max` or an enum, and values are checked before sending. Raw writes to addresses that belong to a mapped point are refused, so the limits can't be bypassed.
- **Read-back:** every `write_point` reads the value back and reports whether it matches.
- **Safe state:** an `apply_safe_state` SAFETY tool, defined in the map, that works even in `--read-only` mode.
- **Read-only mode** (`--read-only`), an **audit log** of every request and reply, and **bounded** request sizes.

## Try it without hardware

```bash
uvx labmcp-modbus --simulate --check
```

The simulator is a generic single-loop PID temperature controller. Its layout is the bundled [example register map](src/labmcp_modbus/examples/generic_pid_controller.yaml). It is illustrative and not a copy of any vendor's map. A heater (3 °C/s at full power) heats a thermal mass that cools towards a 22 °C room (τ = 90 s). The PID loop has a ramping working setpoint and alarm bits. The simulator answers exception 02 for unimplemented addresses, 03 for values outside its own range (e.g. a 500 °C setpoint), and 0B for a wrong unit id. With `--simulate --option register_map=my_map.yaml`, points that are not in the example layout get zero-initialised storage, so you can test your own map.

## Connect your device

1. **Device setup:** in the device's communications menu, enable Modbus and note its settings.
   - **RTU (RS-485):** set the unit (slave) address (1–247), baud rate, parity and stop bits. The server defaults to the Modbus serial default of 19200 baud, 8 data bits, even parity, 1 stop bit. Many devices ship with other settings (e.g. 9600 8N1); put your device's settings in the address. Wire the USB-RS485 adapter's A/B (D−/D+) lines to the device; vendors disagree on the A/B labels, so swap them if nothing answers. Connect the signal ground, and put 120 Ω termination at both ends of longer buses.
   - **TCP:** set the IP address. The port is 502 unless changed. Many TCP devices ignore the unit id. If yours does not, it is usually 1; the TCP implementation guide recommends 255 for directly addressed devices.
2. **Write a register map** for your device from its Modbus manual. Copy the [example](src/labmcp_modbus/examples/generic_pid_controller.yaml) and see [Register map](#register-map) below.
3. **Test the connection:**
   ```bash
   uvx labmcp-modbus --address tcp://192.168.1.20:502 --option register_map=~/maps/oven.yaml --check
   uvx labmcp-modbus --address "serial:///dev/ttyUSB0?baudrate=9600&parity=N" --option unit_id=3 \
       --option register_map=~/maps/controller.yaml --check
   uvx labmcp-modbus --address "serial://COM4?baudrate=19200&parity=E" --option register_map=C:/maps/chiller.json --check
   uvx labmcp-modbus --address "tcp://192.168.1.30:4001?framer=rtu" --check      # RTU through a transparent gateway
   ```
   `--check` connects, reads the map's first point as a probe and prints `probe: ok: …` or the error. More address parameters: `stopbits=2`, `bytesize=7`, `framer=ascii` (Modbus ASCII), `timeout=1`, `retries=2`.

## Register map

```yaml
device:                 # optional
  name: Incubator oven
  manufacturer: Example
  model: X-100
  unit_id: 1            # --option unit_id overrides it
  word_order: big       # default for 32/64-bit points
  byte_order: big

points:
  chamber_temperature:
    table: input        # holding | input | coil | discrete
    address: 0          # 0-based protocol address ("30001" in the manual -> input 0)
    type: int16         # uint16 | int16 | uint32 | int32 | float32 | float64 | bool
    scale: 0.1          # value = raw * scale + offset
    unit: °C
    description: Chamber temperature
  setpoint:
    table: holding
    address: 0          # "40001" -> holding 0
    type: int16
    scale: 0.1
    unit: °C
    writable: true
    min: 20             # required for writable numbers: checked before sending
    max: 60
  mode:
    table: holding
    address: 1
    writable: true
    enum: {0: standby, 1: run}
  heater_enable:
    table: coil
    address: 0
    writable: true

safe_state:             # optional: run in order by apply_safe_state
  - {point: heater_enable, value: false}
  - {point: mode, value: standby}
```

| Field | Default | Meaning |
|---|---|---|
| `table` | (required) | `holding` (FC03/06/16), `input` (FC04, read-only), `coil` (FC01/05), `discrete` (FC02, read-only) |
| `address` | (required) | **0-based** protocol (PDU) address. Manual numbering: 4xxxx → holding `xxxx − 1`, 3xxxx → input, 1xxxx → discrete, 0xxxx → coil |
| `type` | `uint16` (`bool` for coils/discretes) | 32-bit types use 2 registers, `float64` uses 4. `bool` in a register means 0 = false, anything else = true |
| `word_order` / `byte_order` | `big` / `big` | `big`/`big` = ABCD (most significant register first). `little`/`big` = CDAB (common on PLCs). `big`/`little` = BADC. `little`/`little` = DCBA |
| `scale`, `offset` | 1, 0 | Engineering value = raw × scale + offset. Writes are rounded to the register's resolution and re-checked |
| `unit`, `description` | "" | Shown to the agent |
| `writable` | `false` | Only holding registers and coils can be writable |
| `min`, `max` | none | Engineering-unit limits. **Required** for writable numeric points (unless `enum` is used) |
| `enum` | none | `{number: label}` for integer points. Writes accept the label or the number |

The server validates the map when it starts. Unknown fields (e.g. a misspelt `maximum`) are errors rather than being silently ignored, so a typo can't remove a limit. If the map is invalid, `list_points` shows the error and the device tools refuse to run. The server fails closed.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add oven -- uvx labmcp-modbus --address tcp://192.168.1.20:502 --option register_map=/home/me/maps/oven.yaml
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "controller": {
      "command": "uvx",
      "args": [
        "labmcp-modbus", "--address", "serial:///dev/ttyUSB0?baudrate=9600&parity=N",
        "--option", "unit_id=1",
        "--option", "register_map=/home/me/maps/controller.yaml",
        "--option", "raw_writes=false"
      ]
    }
  }
}
```

Useful flags:
- `--read-only` allows reads only. `apply_safe_state` stays available.
- `--option raw_writes=false` removes `write_register`, `write_registers` and `write_coil`, so the agent can only write what the map allows. This is recommended once your map is complete.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `apply_safe_state` | 🛑 safety | Put the device into the safe state defined in the register map (e.g. heater output off, controller to standby), writing each step in order and reading it back. Every step is attempted even if an earlier one fails. Call it immediately if anything looks wrong. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `list_points` | 👁 read | Describe the loaded register map: device, every named point (table, 0-based address, type, scaling, unit, writable, min/max, enum) and the safe-state steps. Works without a connection. |
| `read_coils` | 👁 read | Read coils (FC01): single-bit outputs such as run/stop or relay states. |
| `read_discrete_inputs` | 👁 read | Read discrete inputs (FC02): single-bit, read-only status such as alarms or limit switches. |
| `read_points` | 👁 read | Read named points from the register map, decoded and scaled into engineering units (e.g. process_temperature = 25.1 °C). A point that cannot be read gets an `error` instead of failing the whole call. |
| `read_registers` | 👁 read | Read raw holding or input registers (unsigned 16-bit), optionally decoded as int16, 32-bit or 64-bit values. For exploring a device; prefer read_points when a register map describes it. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `write_coil` | ⚠️ hazard | Switch one raw coil (FC05). Coils often start or stop equipment (heaters, pumps, motors); mapped coils are refused here (use write_point). |
| `write_point` | ⚠️ hazard | Write a named point from the register map (setpoint, mode, output enable...). The value is checked against the point's writable flag, min/max or enum BEFORE sending, converted to raw registers, written, then read back. Changing setpoints and outputs acts on real equipment. |
| `write_register` | ⚠️ hazard | Write one raw holding register (FC06). No scaling or limits are applied, so only use it for addresses the register map does not describe (mapped addresses are refused: use write_point). Returns a read-back if the register is readable. |
| `write_registers` | ⚠️ hazard | Write consecutive raw holding registers (FC16), e.g. both halves of a 32-bit value. No scaling or limits are applied; mapped addresses are refused (use write_point). |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. `apply_safe_state` is listed only when the register map defines a `safe_state`.

## Safety limits

This server has no global `--limit` values: a generic Modbus client can't know what a register means. The protections come from the register map and the server:

| Protection | Where |
|---|---|
| `writable: false` (default) | `write_point` refuses the point |
| `min` / `max` / `enum` per point | `write_point` refuses out-of-range values **before sending**, and again after rounding to the register resolution |
| Mapped-address protection | `write_register(s)` / `write_coil` refuse any address that overlaps a mapped point |
| `--option raw_writes=false` | Removes the raw write tools entirely |
| `--read-only` | Removes every write tool. `apply_safe_state` remains |
| `safe_state` in the map | `apply_safe_state` writes each step in order and reads it back. Every step is attempted even if one fails |
| Request bounds | 1–125 registers (FC03/04), 1–2000 bits (FC01/02), 1–123 registers (FC16) per request, as in the spec |
| No broadcast | Unit id 0 is refused on serial lines: it would write to every device, and devices do not reply to broadcasts |

## Example prompts

- "What's the chamber temperature and output power right now? Show every point with its unit."
- "Ramp the controller to 55 °C at 2 °C/min, switch it to auto, enable the heater, and tell me when it's within 0.5 °C of setpoint."
- "Set the high alarm to 70 °C, then check whether any alarm or sensor-fault bits are active."
- "Read holding registers 100–109 and decode them as float32 (try both word orders). I'm mapping a new chiller."
- "Something's wrong: put the controller in its safe state and confirm the heater is off."

## Notes

- **Addresses are 0-based on the wire.** Manuals often use 1-based "Modicon" numbers (40001 = holding 0). If every read is off by one register, the map uses the wrong convention.
- **Word order is not standardised** for 32-bit values: the Modbus spec only defines byte order within a register. If a float or 32-bit value looks absurd, try `word_order: little`. `read_registers` with `decode_as` helps you find the right order.
- `write_point` uses FC06 for single-register points, FC16 for multi-register points and FC05 for coils. A few devices accept only FC16 even for single registers. For those, map the value as part of a multi-register point or report it so we can add an option.
- Some controllers save written parameters to non-volatile memory, which survives only a limited number of write cycles. Don't have the agent rewrite setpoints in a tight loop. Check whether the manual offers a RAM-only or remote setpoint register.
- Reading is normally side-effect free, but a few devices clear counters or alarms when they are read. Leave such registers out of the map or mark them in `description`.
- Modbus TCP devices often accept only a few simultaneous connections. Close other SCADA or logging clients if the connection is refused.
- Timeouts nearly always mean a wrong unit id, baud rate, parity or A/B wiring. Exception 02 means an address the device does not implement. Exception 03 means the device rejected the value by its own internal limits.
- Works with pymodbus 3.8 or newer (the unit keyword changed from `slave=` to `device_id=` in 3.10; both are handled).
- The bundled map is an illustrative example. Contributions of real device maps are welcome: include the manual title and revision the addresses came from.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
