# EPICS Channel Access — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-epics -->

Let an AI agent read, monitor and, when you allow it, carefully write **EPICS process variables (PVs)** over **Channel Access**. EPICS is the control system behind most particle accelerators, synchrotron and neutron beamlines, large telescopes, fusion experiments and many physics labs. The server uses [caproto](https://github.com/caproto/caproto), a pure-Python CA implementation, so there is nothing to compile and no `libca` to install.

> ⚠️ **Facility control systems are safety-critical.** Writing a PV can move motors and sample stages, open shutters, ramp magnets or high voltage, and change cryogenic or vacuum systems. **Follow your facility's rules. Get authorisation from the responsible scientist or control-room staff before enabling writes, and run with `--read-only` unless writes are explicitly authorised.** This server is not an interlock or a personnel-protection system and never replaces them. Where possible, connect through the facility's CA gateway with its access-security rules, and limit writes with `--option put_allowlist=`.

| | |
|---|---|
| **Package** | `labmcp-epics` |
| **Instruments** | Any EPICS IOC serving Channel Access: EPICS Base 3.14–7.x soft/hard IOCs, areaDetector, motor, asyn, StreamDevice, caproto/pythonSoftIOC IOCs, CA gateways |
| **Interfaces** | Ethernet: CA search on UDP 5064, circuits on TCP 5064, repeater on UDP 5065 |
| **Protocol** | EPICS Channel Access v4.13 ([protocol specification](https://docs.epics-controls.org/en/latest/specs/ca_protocol.html), [ao record: DRVH/DRVL](https://docs.epics-controls.org/projects/base/en/latest/aoRecord.html), [caproto threading client](https://caproto.github.io/caproto/master/threading-client.html)) |
| **Status** | 🧪 **simulated**: tested against an in-process caproto IOC (real CA over localhost), not yet verified at a facility. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-epics --simulate --check
```

The simulator is a real Channel Access server running in-process. It is bound to `127.0.0.1` on random free ports, and the client is pointed only at that address (auto address list off), so it neither broadcasts on nor answers your network. It serves `SIM:TEMP` (°C, with alarm limits, following `SIM:TEMP:SP` while `SIM:HEATER` is On), `SIM:MTR` (a motor whose put-callback completes only when the move is done, with `SIM:MTR:RBV`, `:DMOV`, `:STOP` and `:VELO`), `SIM:DET:FRAMES`, `SIM:SAMPLE` (string), `SIM:SPECTRUM` (a 512-point waveform), `SIM:BEAM:CURRENT` and `SIM:STATUS`.

## Connect to your control system

1. **Network:** your computer must reach the IOCs, or the facility's CA gateway: UDP 5064/5065 and TCP 5064 through any firewall. Most facilities put control networks behind a gateway, so ask your controls group for its address.
2. **Address list:** Channel Access finds PVs by UDP search. Either use your usual EPICS environment (`EPICS_CA_ADDR_LIST`, `EPICS_CA_AUTO_ADDR_LIST`), or pass it explicitly:
   ```bash
   uvx labmcp-epics --read-only --address 10.0.1.20 --option check_pv=SR:CURRENT --check   # one IOC / gateway
   uvx labmcp-epics --read-only --option ca_addr_list="gw1.facility.org 10.0.1.21:5064" --option check_pv=BL7:RING:I --check
   uvx labmcp-epics --read-only --option check_pv=BL7:RING:I --check                        # use the environment as-is
   ```
   Passing an address list switches the automatic (broadcast) list off unless you add `--option auto_addr_list=yes`. With `--option check_pv=<a PV you know exists>`, `--check` reads that PV and prints its value, or the reason it can't be reached. Without it, `--check` only confirms that the CA client starts.

## Add to your MCP client

**Claude Code** (read-only, recommended)
```bash
claude mcp add epics -- uvx labmcp-epics --read-only --address gw1.facility.org
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "epics": {
      "command": "uvx",
      "args": ["labmcp-epics", "--read-only", "--address", "gw1.facility.org"]
    }
  }
}
```

**Authorised writes** (example: a beamline scientist lets the agent move only the sample stage and the filter wheel, and configures an emergency safe state):
```bash
uvx labmcp-epics --address 10.0.7.10 \
  --option put_allowlist='BL7:(SAMPLE:(X|Y)|FILTER:POS)' \
  --option require_ctrl_limits=true \
  --option safe_state='BL7:SAMPLE:X.STOP=1;BL7:SAMPLE:Y.STOP=1;BL7:SHUTTER:CMD=Close'
```

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `apply_safe_state` | 🛑 safety | Emergency action: write the scientist-configured safe-state PVs (--option safe_state, e.g. motor STOP fields, shutter close, HV off), all of them even if one fails. Available in read-only mode. It does not replace the facility's own interlocks and stop buttons. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_pv` | 👁 read | Read one PV with its metadata: value, units, precision, alarm severity/status, IOC timestamp and age, display/alarm/warning/control limits and enum state names. Waveforms come back downsampled with min/max/mean statistics. |
| `get_pvs` | 👁 read | Read many PVs at once (e.g. all motors or vacuum gauges of a beamline). PVs that cannot be reached are listed in `errors` instead of failing the whole call. |
| `monitor_pv` | 👁 read | Subscribe to a PV and collect every value change for `duration_s` seconds (or until `max_updates`), then return statistics (min/max/mean/std, drift rate) and the updates. |
| `put_pv` | ⚠️ hazard | Write a PV. This can move motors, open shutters, change magnet or high-voltage setpoints and heat or cool samples: tell the user exactly what will change first. The write is refused unless the PV matches the put allow-list, the IOC grants write access, and the value has the right type and lies within the PV's control limits (DRVL/DRVH). Returns the read-back value. |
| `put_pvs` | ⚠️ hazard | Write several PVs in order (e.g. set both slit blades). Every write is validated first (allow- list, access rights, type, control limits); if any is invalid nothing is written. Writing stops at the first failure and the rest are reported as not written. The whole batch must finish within an hour: writes that would start later are not sent. |
| `pv_info` | 👁 read | Connection details of a PV: serving IOC host:port, native type, element count, and whether this client has read/write access (EPICS access security) and passes the put allow-list. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_monitor_duration_s` | 60 s | Longest `monitor_pv` collection |
| `max_put_batch` | 10 PVs | Most PVs one `put_pvs` call may write |
| `max_put_wait_s` | 60 s | Longest wait for one put-callback (completion) |

Override at launch, e.g. `--limit max_put_batch=4`. The main write guards are options, not numeric limits:

| Guard | How |
|---|---|
| Read-only mode | `--read-only` hides `put_pv` and `put_pvs` entirely |
| Allow-list | `--option put_allowlist=<regex>`: a PV name must **fully** match it to be written. Prefer patterns that exclude record fields (`BL7:SAMPLE:[^.]*` rather than `BL7:SAMPLE:.*`) |
| Limit fields | writes to the fields that set the limits (`.DRVH`, `.DRVL`, `.HOPR`, `.LOPR`, and the motor record's `.HLM`, `.LLM`, `.DHLM`, `.DLLM`) are refused, so a write can't raise a limit and then pass it. `--option allow_limit_field_writes=true` permits them |
| EPICS access security | writes are refused up front if the IOC or gateway grants this client read-only access |
| Control limits | numeric writes outside the PV's control limits are refused. For output records these are DRVL/DRVH, which the IOC would otherwise **silently clip** to. A NaN limit means no limit on that side |
| No limits configured | allowed with a warning, or refused with `--option require_ctrl_limits=true` |
| Type checks | enum states must be valid names or indices, integers must be integral and in range, 32-bit FLOAT values must fit, strings are at most 39 latin-1 characters, arrays must fit the element count |
| PV names | no whitespace or quotes, and record names of at most 59 characters (EPICS 3.14+); anything else is refused before a search is sent |
| Batches | `put_pvs` validates every write before sending any, stops at the first failure, and sends no write once its one-hour budget (inside the tool timeout) is used up |
| Put-completion | `put_pv` waits for the IOC's put-callback. On timeout it says the IOC may still be processing and not to repeat blindly |
| Safe state | `apply_safe_state` (SAFETY, available even in read-only mode) writes only the PVs the scientist configured with `--option safe_state` |

## Example prompts

- "What is the storage-ring current, and are any of the BL7 vacuum gauges in alarm?"
- "Read BL7:MONO:ENERGY and its readback, and tell me the units and control limits."
- "Monitor the sample temperature for 30 s and tell me whether it is still drifting."
- "Move the sample stage X to 12.5 mm and confirm the readback when the move is done."
- "Record the detector spectrum waveform and tell me where the peak is."
- "Stop everything: apply the safe state."

## Notes

- **Setpoint versus readback:** for motor records, `NAME` (VAL) is the target and `NAME.RBV` is the actual position. `put_pv` returns the value read back from the PV you wrote, so read the readback PV separately to confirm physical motion.
- **Control limits** come from the CA `DBR_CTRL` response. EPICS convention treats equal low and high limits (normally 0/0) as "not configured", and those PVs are only type-checked.
- **NaN and infinity:** JSON has neither. A NaN or infinite scalar value comes back as the text `"nan"`, `"inf"` or `"-inf"`, array elements as `null`, a limit that is NaN (EPICS 3.16+ reports unset alarm limits that way) as `null`, and waveform and monitor statistics leave such values out and count them (`non_finite`, `non_finite_updates`).
- **Alarms:** every reading includes severity (`NO_ALARM`, `MINOR_ALARM`, `MAJOR_ALARM`, `INVALID_ALARM`), status (`HIHI`, `LOLO`, `UDF`, `COMM`, …) and `age_s`, the time since the IOC timestamp, so stale values stand out.
- **Waveforms** are downsampled by block averaging to `max_elements`, with min/max/mean/argmax statistics. CHAR waveforms are also decoded to text (`as_string`), the usual EPICS long-string convention. When writing, a text value is accepted for CHAR waveforms.
- **pvAccess:** only Channel Access is supported. caproto 1.x implements CA only, and pvAccess would need [p4p](https://pypi.org/project/p4p/), a compiled EPICS Base binding that is deliberately not a dependency. EPICS 7 IOCs serve their records over both CA and PVA, so this covers normal records. PVA-only data (for example NTNDArray images from areaDetector's PVA plugin, or QSRV group PVs) can't be read.
- The CA client is configured through `EPICS_CA_*` environment variables, as in EPICS Base. The server sets them for its own process from `--address`/`--option` and restores them on disconnect. If `put_allowlist` holds a comma, pass it on the command line or give `LABMCP_OPTIONS` in its JSON form (`{"put_allowlist": "BL7:(A|B){1,2}"}`), because the `a=1,b=2` form splits on commas.
- `get_command_log` records every write (`caput -c NAME value`) and its read-back.

## Hardware verification

| Facility / IOC | EPICS version | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
