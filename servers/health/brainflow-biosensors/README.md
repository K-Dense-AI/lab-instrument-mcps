# BrainFlow Biosensing Boards — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-brainflow -->

Let an AI agent stream, record and analyse **EEG, EMG, ECG, EOG, PPG and EDA** from the biosensing boards supported by the open-source **[BrainFlow](https://brainflow.org)** SDK: OpenBCI, Muse, Neurosity, g.tec Unicorn, BrainBit and many more. It covers recording with event markers, EEG band powers and per-electrode signal-quality checks.

> **Research use only. This is not a medical device.** Do not use it to diagnose, monitor or treat anyone. Follow the board manufacturer's safety instructions (battery operation, electrical isolation, skin preparation). This server does not change any of them.

| | |
|---|---|
| **Package** | `labmcp-brainflow` |
| **Instruments** | OpenBCI Cyton, Cyton+Daisy, Ganglion, Galea (USB dongle, BLE, WiFi Shield) · Muse 2 / S / S Athena / 2016 · Neurosity Crown / Notion · g.tec Unicorn · BrainBit · Callibri · Enophone · Mentalab Explore · EmotiBit · FreeEEG32/128 · NeuroPawn Knight · Shimmer3 · any other board in BrainFlow's `BoardIds` |
| **Interfaces** | USB serial dongle, Bluetooth LE (native or BLED112 dongle), WiFi, vendor SDKs (all through BrainFlow) |
| **Protocol** | BrainFlow SDK ≥ 5.21: `BoardShim`, `BrainFlowInputParams`, `DataFilter` ([User API](https://brainflow.readthedocs.io/en/stable/UserAPI.html), [Supported Boards](https://brainflow.readthedocs.io/en/stable/SupportedBoards.html), [Data Format](https://brainflow.readthedocs.io/en/stable/DataFormatDesc.html)) |
| **Status** | 🧪 **simulated**: tested against BrainFlow's own synthetic board and a built-in fake, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-brainflow --simulate --check
```

`--simulate` runs **BrainFlow's own `SYNTHETIC_BOARD`**, so the real BrainFlow code path runs: 16 EXG channels at 250 Hz with sine waves at 5, 10, 15 … 80 Hz plus accelerometer, gyro, PPG, EDA and temperature rows. On platforms where BrainFlow's native library can't load, or with `--option simulator=fake`, a pure-numpy stand-in is used instead. It simulates an 8-channel, 250 Hz EEG headset with 1/f background, posterior alpha at 10 Hz and one badly attached electrode (Fp2, heavy 50 Hz pickup), so the signal-quality check has something to find.

## Connect your board

1. **Pick the board** with `--option board=<alias>` (or any BrainFlow `BoardIds` name such as `ANT_NEURO_EE_411_BOARD`, or a numeric id). The `list_supported_boards` tool prints this table too.
2. **Give the connection detail** with `--address`. Depending on the board, BrainFlow expects a different `BrainFlowInputParams` field, and the server fills in the right one:

| Board (`--option board=`) | `--address` is | Required? | Notes |
|---|---|---|---|
| `cyton`, `cyton_daisy` | serial port of the USB dongle | yes | macOS: use `/dev/cu.usbserial-*`, not `/dev/tty.*` |
| `ganglion` (native BLE) | Bluetooth MAC | no (auto-discovery) | FW 2 boards: `--option other_info=fw:2` |
| `ganglion_dongle` | serial port of the BLED112 dongle | yes | optional `--option mac_address=` |
| `cyton_wifi`, `cyton_daisy_wifi`, `ganglion_wifi` | WiFi Shield IP (192.168.4.1 in direct mode) | no (SSDP) | `--option ip_port=` (default 6789) |
| `muse_2`, `muse_s`, `muse_s_athena`, `muse_2016` | Bluetooth MAC | no | `--option serial_number=Muse-XXXX`; PPG: `--option other_info=p50` (Muse 2) / `p61` (Muse S) |
| `muse_2_bled`, `muse_s_bled` | serial port of the BLED112 dongle | yes | |
| `crown`, `notion_1`, `notion_2` | device serial number | no | same network, broadcast must be allowed |
| `unicorn`, `brainbit` | device serial number | no | Unicorn: pair with the supplied dongle |
| `enophone`, `explore_4`, `explore_8` | Bluetooth MAC | Linux only | |
| `emotibit` | network broadcast address | no | |
| `freeeeg32`, `freeeeg128`, `neuropawn_knight`, `shimmer3`, `ironbci_32` | serial port | yes | |
| `playback` | path of a file saved in BrainFlow format | yes | `--option master_board=<board it was recorded with>` |
| `synthetic` | nothing | | BrainFlow's synthetic board |

For a board that isn't listed, the server infers the field from the address format: `COM3` or `/dev/…` becomes a serial port, `AA:BB:…` a MAC address, `a.b.c.d` an IP address, and anything else a serial number. You can also set any field directly with `--option serial_port=… / mac_address=… / ip_address=… / serial_number=… / other_info=… / timeout=…`.

3. **Test the connection:**
   ```bash
   uvx labmcp-brainflow --option board=cyton --address /dev/cu.usbserial-DM00Q0QN --check   # macOS
   uvx labmcp-brainflow --option board=cyton_daisy --address COM3 --check                  # Windows
   uvx labmcp-brainflow --option board=muse_2 --option serial_number=Muse-1A2B --check
   uvx labmcp-brainflow --option board=unicorn --check
   ```
   Close other programs that hold the device (OpenBCI GUI, Muse app) first. On Linux, add yourself to the `dialout` group for serial ports.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add eeg -- uvx labmcp-brainflow --option board=cyton --address /dev/ttyUSB0
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "eeg": {
      "command": "uvx",
      "args": ["labmcp-brainflow", "--option", "board=cyton", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

With `--read-only`, the agent can still record and analyse data (a temporary stream is started for each recording) but cannot leave the stream running, insert markers or send `configure_board` commands.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `configure_board` | 🎛 control | Send a board-specific configuration command through BrainFlow's config_board (e.g. OpenBCI channel settings 'x1060110X', test signals, or Muse presets 'p50'/'p61' to enable PPG). Only acquisition settings of the amplifier change. Consult the board's SDK documentation first. |
| `get_band_powers` | 👁 read | EEG band powers (delta 1-4, theta 4-8, alpha 8-13, beta 13-30, gamma 30-50 Hz) over the most recent `window_s` seconds: BrainFlow's channel-averaged relative powers plus per-channel absolute (uV^2) and relative powers and the peak frequency. Records a fresh window if not streaming. |
| `get_board_info` | 👁 read | Describe the connected board: channel names by type (EEG/EMG/ECG/EOG share the EXG rows on most boards), sampling rate, available presets (data buffers) and streaming state. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_signal_quality` | 👁 read | Check every EXG channel for common electrode problems: flat line (disconnected), railed (amplifier saturated, OpenBCI Cyton boards), strong 50/60 Hz mains noise (poor contact or missing reference), and implausibly high amplitude (movement, muscle, loose electrode). |
| `insert_marker` | 🎛 control | Write an event marker into the data stream at the current sample (for event-related experiments: stimulus onsets, condition changes). Requires `start_streaming`; markers appear in `record` results and saved files. |
| `list_supported_boards` | 👁 read | List common BrainFlow boards: the `--option board=` alias, BrainFlow board id, and which connection detail `--address` must hold (serial port, Bluetooth MAC, IP address or serial number). Any other BrainFlow BoardIds name or numeric id is accepted too. Does not need a board. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `record` | 👁 read | Record `duration_s` seconds and return per-channel statistics, event markers and downsampled traces. Uses the live stream if one is running, otherwise starts a temporary one. The full data (every row, full sampling rate) can be written to `save_path`. |
| `start_streaming` | 🎛 control | Start continuous acquisition into BrainFlow's ring buffer (the board's radio/LEDs switch on; nothing is applied to the participant). Needed for `insert_marker`; `record` then reads from the live stream. Call `stop_streaming` when finished. |
| `stop_streaming` | 🎛 control | Stop acquisition (saves battery). Data already in the buffer is kept until the session is released (`reconnect`) or read. |
<!-- TOOLS:END -->

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_record_duration_s` | 60 s | Longest recording or analysis window (`record`, `get_band_powers`, `get_signal_quality`) an agent may request |

Override at launch: `--limit max_record_duration_s=600`. No tool here applies anything to the participant. BrainFlow boards only measure.

## Example prompts

- "Check the signal quality on all 8 Cyton channels and tell me which electrodes need more gel."
- "Record 30 s eyes-closed and 30 s eyes-open, save both as CSV in ~/data/sub-01/, and compare occipital alpha power."
- "Start streaming. I'll run an oddball task: insert marker 1 for standards and 2 for targets when I tell you. Then save the session in BrainFlow format."
- "What's the peak alpha frequency on O1 and O2 over the last 10 seconds?"
- "Is there 50 or 60 Hz mains noise in this setup, and on which channels?"
- "List the boards you support and tell me how to connect my Muse S."

## Notes

- **How the band powers are computed.** `average_relative` comes from BrainFlow's `DataFilter.get_avg_band_powers(apply_filter=True)`: detrend, 48–52 and 58–62 Hz band-stop, 2–45 Hz band-pass, Welch PSD, then bands of 1–4, 4–8, 8–13, 13–30 and 30–50 Hz averaged over channels and normalised to sum to 1. Per-channel values use detrend, a 50/60 Hz notch and a 1–45 Hz 4th-order zero-phase Butterworth (BrainFlow `DataFilter`), followed by a Welch PSD (Hann window, 50 % overlap, ≈0.5 Hz bins). Absolute band power is the integrated PSD in µV², and gamma is effectively 30–45 Hz.
- **Signal-quality verdicts** (EEG-oriented thresholds): `flat` means the raw SD is below 0.5 µV (disconnected); `railed` means at least 90 % of the ADS1299 input range is used (OpenBCI Cyton family only, using BrainFlow's formula with gain 24, set by `--option exg_gain=`); `line_noise` means 50 or 60 Hz power exceeds the whole 1–45 Hz power; `high_amplitude` means the band-passed RMS is above 100 µV. EMG/ECG recordings will naturally be "high amplitude".
- **Units.** BrainFlow returns EXG in µV "wherever possible". The OYMotion gForce armbands return ADC counts. Accelerometer, PPG and similar channels use board-specific units. Timestamps are Unix seconds.
- **Presets.** Some boards stream several buffers with different sampling rates, for example Muse EEG (default), IMU (auxiliary) and PPG (ancillary). Pass `preset=` to `get_board_info` and `record`.
- **Markers** go into the default preset's marker row and need an active stream (`start_streaming`). You can insert them while `record` is running. Value 0 is reserved.
- **Saved files.** `save_format="csv"` writes labelled columns for every row. `save_format="brainflow"` writes `DataFilter.write_file` output, which you can replay with `--option board=playback --address file.csv --option master_board=<board>`.
- `configure_board` passes a string straight to the board firmware through BrainFlow's `config_board`, for example OpenBCI channel settings or Muse presets. It only changes acquisition settings, but check the board's SDK documentation for valid commands.
- `reconnect` (and stopping the server) stops the stream and calls `release_session`, which frees the serial port or BLE connection.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
