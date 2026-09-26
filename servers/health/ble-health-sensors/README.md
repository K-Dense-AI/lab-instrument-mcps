# Bluetooth LE Health Sensors — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-ble-health -->

Let an AI agent read **any Bluetooth Low Energy sensor that implements the standard Bluetooth SIG health profiles**: record heart rate with RR intervals and HRV from a chest strap, read SpO2 and pulse rate from an oximeter, and capture blood pressure, body temperature and weight readings from monitors, thermometers and scales, in physiology labs, sports-science studies, sleep/HRV research and lab-operations settings.

> [!WARNING]
> **Research and lab-operations use only. This is not a medical device.** It is not intended for diagnosis, monitoring of patients, triage, or any clinical decision-making, and has not been validated or cleared by any regulator. Consumer sensors can be inaccurate; values pass through an AI agent that can misread them. Never use this server's output to make health decisions. Readings of people are personal data: follow your ethics approval and data-protection rules.

| | |
|---|---|
| **Package** | `labmcp-ble-health` |
| **Instruments** | Any device implementing the Bluetooth SIG profiles: heart-rate straps/armbands (Heart Rate Service 0x180D), pulse oximeters (Pulse Oximeter Service 0x1822), blood pressure monitors (0x1810), thermometers (Health Thermometer 0x1809), weight scales (Weight Scale 0x181D), plus Battery (0x180F) and Device Information (0x180A) |
| **Interfaces** | Bluetooth Low Energy, via [`bleak`](https://github.com/hbldh/bleak) (Linux/BlueZ, macOS/CoreBluetooth, Windows/WinRT) |
| **Protocol** | Bluetooth SIG GATT: [GATT Specification Supplement](https://btprodspecificationrefs.blob.core.windows.net/gatt-specification-supplement/GATT_Specification_Supplement.pdf), [HRS 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html), [PLXS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXS_v1.0.1/out/en/index-en.html), [BLS 1.1.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BLS_v1.1.1/out/en/index-en.html), [HTS 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HTS_v1.0/out/en/index-en.html), [WSS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/WSS_v1.0.1/out/en/index-en.html); IEEE 11073-20601 SFLOAT/FLOAT per the [PHD Transcoding White Paper v16](https://www.bluetooth.com/wp-content/uploads/2019/03/PHD_Transcoding_WP_v16.pdf) |
| **Status** | 🧪 **simulated**: tested against a simulated BLE backend that emits byte-exact GATT packets, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-ble-health --simulate --check
```

The simulator provides six devices (addresses `00:00:5E:00:53:01`–`06`, from the IANA documentation range): a combined "health kit" (default) exposing every service, and a separate HR strap, oximeter, BP monitor, thermometer and scale. Pick one with e.g. `--simulate --address 00:00:5E:00:53:04`.

## Connect your device

1. **Check the device uses the standard profiles.** Only devices that implement the Bluetooth SIG services above work. Many consumer wearables (most smartwatches, many fitness bands, several BP-monitor and scale brands) use proprietary protocols instead, and some only speak the standard profile in a "pairing"/"data transfer" mode. Chest straps that advertise the Heart Rate Service are the most reliable choice for HRV work.
2. **Free the device.** A BLE sensor usually accepts one connection: close the vendor's phone app (or switch phone Bluetooth off) before connecting.
3. **Find the address:** start the server without `--address` and ask the agent to run `scan_devices`, or run it with `--check` after putting the device in advertising mode (wear the strap; press the monitor's Bluetooth/start button). On Linux/Windows the address is the MAC (`AA:BB:CC:DD:EE:FF`); on macOS it is a CoreBluetooth UUID that is specific to your computer.
4. **Test the connection:**
   ```bash
   uvx labmcp-ble-health --address AA:BB:CC:DD:EE:FF --check                       # Linux / Windows
   uvx labmcp-ble-health --address 1A2B3C4D-1111-2222-3333-444455556666 --check    # macOS
   ```
   Devices that require bonding (common for BP monitors and scales): add `--option pair=true` on Linux/Windows; macOS shows a system pairing prompt automatically. On Linux choose a non-default adapter with `--option adapter=hci1`; lengthen device discovery with `--option connect_timeout_s=40` (at most 60 s).

macOS: the terminal (or MCP client app) that launches the server needs Bluetooth permission (System Settings › Privacy & Security › Bluetooth). Without it macOS terminates the process as soon as Bluetooth is touched (exit code 134), before any error message can be printed.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add hr-strap -- uvx labmcp-ble-health --address AA:BB:CC:DD:EE:FF
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "hr-strap": {
      "command": "uvx",
      "args": ["labmcp-ble-health", "--address", "AA:BB:CC:DD:EE:FF"]
    }
  }
}
```

Every tool is read-only (nothing on the device is changed, no control points are written), so `--read-only` makes no difference. Run one server entry per device if you use several sensors at once. For other clients, generate the snippet with `uvx labmcp config ble-health --address AA:BB:CC:DD:EE:FF --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_device_info` | 👁 read | Read the device's identity (manufacturer, model, serial, firmware), the standard health services it exposes and its features. |
| `read_battery` | 👁 read | Read the device's battery level (Battery Service, 0-100 %). |
| `read_pulse_oximetry` | 👁 read | Read SpO2 (%) and pulse rate from a pulse oximeter, averaged over a few seconds or as a single spot-check reading. |
| `read_temperature` | 👁 read | Wait for a thermometer to send a temperature measurement and return it in °C. |
| `read_weight` | 👁 read | Wait for a scale to send a weight measurement (kg), with BMI and height if the scale sends them. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `record_heart_rate` | 👁 read | Record heart rate for `duration_s`: bpm series, RR intervals and time-domain HRV (mean HR, SDNN, RMSSD, pNN50). |
| `scan_devices` | 👁 read | Scan for nearby Bluetooth LE devices: address, name, signal strength and advertised health services. Works without a configured --address. |
| `wait_for_blood_pressure` | 👁 read | Wait for a blood pressure monitor to send a measurement: systolic, diastolic and mean arterial pressure (mmHg) and pulse rate. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_record_duration_s` | 600 s | Longest heart-rate recording or continuous-oximetry averaging window an agent may start |
| `max_wait_s` | 300 s | Longest time a tool may wait for a blood pressure, temperature, weight or spot-check reading |

Override at launch: `--limit max_record_duration_s=1800`. Note that many MCP clients also enforce their own per-tool-call timeout; long recordings may need that raised too (e.g. `MCP_TOOL_TIMEOUT` in Claude Code).

## Example prompts

- "Record 5 minutes of heart rate from the chest strap and give me mean HR, SDNN and RMSSD, and save the raw RR intervals to ~/hrv/p07_baseline.csv."
- "Scan for heart-rate straps nearby and tell me which one has the strongest signal."
- "Wait for the blood pressure monitor: I'll start the cuff now. Report systolic/diastolic and whether the device flagged movement or an irregular pulse."
- "Average SpO2 and pulse rate over 30 seconds from the oximeter and tell me how many samples were unusable."
- "Read the scale when the participant steps on it and log weight and BMI with the device timestamp."
- "What's the battery level of the strap before we start the session?"

## Notes

- **Measurement devices push, they are not polled.** Blood pressure monitors, thermometers, scales and spot-check oximeters only *indicate* a value when a measurement completes (these characteristics cannot be read). Call the tool first, then take the measurement. The tool keeps scanning and reconnecting until its timeout because many monitors only advertise right after measuring (for oximeters this needs `mode="spot_check"`; `auto` must connect first to find out which mode the device supports).
- **One measurement at a time.** Starting a measurement cancels one that is still running on the same server (for example a recording whose tool call already timed out in the MCP client), and `reconnect` cancels it too; the cancelled call returns an error.
- **Malformed packets** are skipped and logged (`get_command_log`) rather than discarding the whole recording; `record_heart_rate` and `read_pulse_oximetry` report how many in `malformed_packets`.
- **`save_path`** (`record_heart_rate`) must be a new `.csv` file: `~` is expanded, missing folders are created, and an existing file is never overwritten. It is checked before recording starts.
- **Stored measurements.** Monitors send unsent stored readings back-to-back, oldest first, when they connect. Tools keep listening 1.5 s after the first one and return the newest as the result, listing older ones in `other_measurements`; the device clock time is in `device_timestamp` (device local time, often wrong if the clock was never set).
- **Special values** (IEEE 11073 NaN, NRes, ±INF) are never turned into numbers: they are reported as `null` with an explanation (`special_values`, `unavailable_samples`). Oximeters typically send NaN for the first seconds while acquiring.
- **Units**: blood pressure in kPa is converted to mmHg (1 kPa = 7.500617 mmHg), °F to °C, lb to kg; `unit_reported` says what the device sent. Weight resolution is 0.005 kg / 0.01 lb per the Weight Scale spec; `0xFFFF` means "measurement unsuccessful" and is returned as `weight_kg: null`. Energy expended is in kJ (HRS 1.0).
- **HRV** is computed from the RR intervals the sensor sends (unit 1/1024 s): SDNN (sample SD), RMSSD, pNN50. RR intervals outside 0.3–2.0 s are excluded as artefacts; no further artefact correction or ectopic-beat editing is done. If a notification is lost over the air, the successive difference across that gap is not a true beat-to-beat difference. Optical wrist sensors often send no RR intervals at all. Use a validated ECG system for anything beyond exploratory research.
- The **Enhanced Blood Pressure Measurement**, **Record Access Control Point** (downloading a monitor's whole memory), Current Time and User Data services are not implemented; nothing is ever written to the device.
- Bluetooth pairing, bonding keys and OS permissions are handled by the operating system; the server never sees PINs.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
