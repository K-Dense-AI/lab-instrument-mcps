# SiLA 2 Bridge — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-sila2 -->

Let an AI agent work with any **SiLA 2** server: liquid handlers, incubators, plate readers, robotic arms, balances or scheduling middleware that implement the open [SiLA 2](https://sila-standard.com) lab-automation standard. The bridge reads the server's own Feature Definitions (FDL), so it knows every command, property, parameter type, unit and constraint. It **validates parameters against the FDL before sending**, runs and tracks long-running (observable) commands, and cancels them through SiLA's CancelController.

| | |
|---|---|
| **Package** | `labmcp-sila2` |
| **Instruments** | Any SiLA 2 server: devices and middleware with SiLA 2 interfaces, and servers built with sila_python, sila_java, sila_csharp or sila_cpp |
| **Interfaces** | Ethernet: gRPC over HTTP/2 with TLS (or insecure test servers), mDNS server discovery |
| **Protocol** | SiLA 2 v1.x ([Part A & Part B specifications](https://sila-standard.com/standards/), [FDL schemas & core features](https://gitlab.com/SiLA2/sila_base), client library [`sila2`](https://gitlab.com/SiLA2/sila_python) ([docs](https://sila2.gitlab.io/sila_python/))) |
| **Status** | 🧪 **simulated**: tested against a real in-process SiLA 2 server (sila2 library, gRPC on localhost), not yet verified against vendor servers. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-sila2 --simulate --check
```

`--simulate` starts a **real SiLA 2 server** in-process, built with the `sila2` library. It runs on `127.0.0.1` on a free port, unencrypted, with discovery off. It implements a simulated thermoblock feature (`org.labmcp/simulation/TemperatureController/v1`) and the official `CancelController` feature:

- the observable `CurrentTemperature` property, plus `TargetTemperature`, `RampRate` and `DeviceState`
- `ControlTemperature`, an observable command with progress, intermediate responses and a `DeviceBusy` defined error
- `RunProgram`, taking a list of `{TargetTemperature, HoldTime}` structures
- `SetRampRate`, constrained to 0 < rate ≤ 10 K/s
- `SwitchOff`

The whole bridge runs through the real gRPC and FDL path.

## Connect your SiLA server

1. **Find it.** Use the `discover_servers` tool (mDNS `_sila._tcp`), or run the server with its address and port shown. SiLA has no default port, but 50052 is common.
2. **TLS.** SiLA 2 servers use TLS by default, and most generate a **self-signed certificate** at startup. Pass that certificate's CA as `--option root_cert=ca.pem`: many servers write it to a file or publish it in their discovery record. Servers started in insecure test mode need `--option insecure=true`. For mutual TLS, add `--option client_cert=… --option client_key=…`.
3. **Test the connection:**
   ```bash
   uvx labmcp-sila2 --address 192.168.1.40:50052 --option root_cert=~/sila/ca.pem --check
   uvx labmcp-sila2 --address sila://localhost:50052 --option insecure=true --check      # test server
   uvx labmcp-sila2 --option server_name="Liquid Handler 1" --option root_cert=ca.pem --check  # via discovery
   ```

## Add to your MCP client

**Claude Code**
```bash
claude mcp add sila -- uvx labmcp-sila2 --address 192.168.1.40:50052 --option root_cert=/path/ca.pem
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "sila": {
      "command": "uvx",
      "args": ["labmcp-sila2", "--address", "192.168.1.40:50052", "--option", "root_cert=/path/ca.pem",
               "--option", "command_allowlist=Incubator.*,PlateReader.ReadPlate"]
    }
  }
}
```

`--read-only` lets the agent browse features, read and subscribe to properties, and check command status, but not call commands. `cancel_command` stays available. To restrict which commands may run, use `--option command_allowlist=Feature.Command,Feature.*`. Entries can also be separated by `;` or spaces, which is handy inside `LABMCP_OPTIONS=command_allowlist=A.B;C.*`.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `call_command` | ⚠️ hazard | Run a SiLA command. This may physically act on the device (move, dispense, heat, shake, open doors): tell the user what will happen first. Parameters are validated against the Feature Definition before anything is sent, and the command must be in the allow-list if one is set. Unobservable commands return their responses; observable ones return an `execution_id`. |
| `cancel_command` | 🛑 safety | Cancel a running observable command, or all commands, through SiLA's CancelController feature. Always available (even read-only). If the server has no CancelController, this explains that and points to the device's own stop command. |
| `discover_servers` | 👁 read | Find SiLA 2 servers on the local network with SiLA Server Discovery (mDNS `_sila._tcp`). Lists name, UUID, address and port of each without connecting. Does not need a configured server. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_command_result` | 👁 read | Responses of a finished observable command, or the SiLA execution error it finished with. Fails with a clear message if the command is still running. |
| `get_command_status` | 👁 read | Status of an observable command started with `call_command`: waiting / running / finishedSuccessfully / finishedWithError, progress, estimated remaining time and the latest intermediate response. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_property` | 👁 read | Read the current value of a SiLA property (for observable properties: the first value of a subscription). |
| `get_server_info` | 👁 read | Identify the connected SiLA server (name, type, UUID, version, vendor, description), list its features, and say whether commands can be cancelled and which commands this bridge may call. |
| `list_executions` | 👁 read | All observable command executions started in this session, newest last, with their status. |
| `list_features` | 👁 read | Describe the server's features from their Feature Definitions: every command (observable or not, parameters, responses, intermediate responses, defined errors, and whether the allow-list permits calling it) and every property, with types rendered as JSON-schema-like dicts including units and constraints. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `subscribe_property` | 👁 read | Collect a property's values for `duration_s` seconds (SiLA subscription for observable properties, polling otherwise) and summarise them (e.g. watch a temperature settle). |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_command_wait_s` | 300 s | Longest time `call_command` may block waiting for an observable command |
| `max_subscription_duration_s` | 120 s | Longest `subscribe_property` collection |
| `max_discovery_s` | 30 s | Longest mDNS discovery scan |

Override at launch, e.g. `--limit max_command_wait_s=60`. The main guards for commands:

| Guard | How |
|---|---|
| Hazard annotation | `call_command` is marked destructive, so MCP clients ask before running it |
| Allow-list | `--option command_allowlist=Feature.Command,...`: other commands are refused before anything is sent, and `list_features` shows `callable: false` for them |
| FDL validation | parameter names must match exactly. Types, ranges (min/max, inclusive or exclusive), sets, lengths, patterns, element counts, fully qualified identifiers and structure fields are checked against the server's FDL before sending |
| Read-only mode | `--read-only` hides `call_command` |
| Cancellation | `cancel_command` (SAFETY) uses `CancelController.CancelCommand` / `CancelAll` when the server implements that feature, and otherwise says so and points to the device's own stop command |
| Deadlines | unobservable commands and property reads time out after `call_timeout_s` (default 30 s, at most 300 s) instead of hanging; a subscription opened for a timed-out read is cancelled, and an observable command the server accepts after the deadline still appears in `list_executions` so it can be cancelled |

The server always re-validates too, so parameters rejected there come back as SiLA validation errors.

## Example prompts

- "What SiLA servers are on the network, and what can the incubator do?"
- "Set the incubator to 37 °C and tell me when it has reached the temperature."
- "Watch the plate reader's chamber temperature for a minute and tell me if it's stable."
- "Run the 'PCR-short' program on the thermocycler, poll its progress every 30 seconds, and report the result."
- "Something's wrong, cancel everything that's running on the liquid handler."
- "Show me the parameters (types, units and allowed ranges) of the robot's MovePlate command."

## Notes

- **Types as JSON.** `list_features` renders each SiLA type as a JSON-schema-like dict: `Real` becomes `number`, `Integer` becomes `integer`, `List` becomes `array` and `Structure` becomes `object`. SiLA units appear as `x-unit` (the label) and `x-unit-si` (the conversion to SI base units, *SI = value × factor + offset*). Other SiLA details use `x-sila-*` keys. Values are passed as JSON:
  - `Binary`: base64 strings
  - `Date`: `YYYY-MM-DD[±HH:MM]`
  - `Time`: `HH:MM:SS[±HH:MM]`
  - `Timestamp`: ISO 8601, UTC if no offset is given
  - `Any`: `{"type": "Real", "value": 1.5}`, basic types only
- **Observable commands** return an `execution_id` right away. `get_command_status` reports the SiLA execution status (`waiting`, `running`, `finishedSuccessfully`, `finishedWithError`), progress, estimated remaining time and the latest intermediate response. `get_command_result` returns the responses, or the defined/undefined execution error together with the error's description from the FDL. Pass `wait_s` to `call_command` for short runs. Executions are tracked only for this session and are forgotten on `reconnect`.
- **Cancellation isn't part of core SiLA.** It needs the server to implement `org.silastandard/core.commands/CancelController/v1`, and `get_server_info` reports whether it does. Some vendors provide their own `Stop`/`Abort` commands instead, which `list_features` shows.
- **SiLA client metadata** (e.g. a `LockController` lock identifier or an `AuthorizationService` access token) can be passed with `metadata={"LockController.LockIdentifier": "…"}` on `call_command`, `get_property` and `subscribe_property`. Values are validated against the metadata's FDL type.
- **Feature Definitions** are parsed only after refusing any `DOCTYPE`/entity declaration (SiLA FDL never uses one), so a hostile server can't trigger XML entity expansion. Features that the `sila2` library rejects as invalid are left out of `list_features` (the command log records why).
- **Not checked locally:** `Schema` and `ContentType` constraints, and the `Unit` constraint, which is informational. The server enforces these. Large binaries (over 2 MB) use SiLA binary transfer, which the `sila2` library handles.
- **Library limitation (sila2 ≤ 0.14):** a *Constrained List* used directly as a command parameter or response can't be unpacked. Parameters are affected on servers built with `sila2`, which is why the simulator's `RunProgram` uses a plain list and checks the 1–10 steps itself. Responses of that shape are affected in this bridge: the server executes the command, but its response can't be decoded. The bridge reports this as **"WAS EXECUTED … could not be decoded"** so the command is not repeated blindly. Properties of that shape decode fine. Plain (unconstrained) lists are not affected.
- **Discovery** browses mDNS for the requested time and returns what servers advertise (UUID, name, version, address, whether a CA certificate is published) without connecting to them. In `--simulate` mode it returns only the simulated server and sends nothing on the network.

## Hardware verification

| SiLA server (device / software) | Server version | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
