# Generic SCPI Instrument — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-scpi -->

Let an AI agent talk to **any IEEE 488.2 / SCPI instrument**: multimeters, oscilloscopes, source-measure units, power supplies, electronic loads, function generators, spectrum analyzers and counters. It works over GPIB, USBTMC, LAN (VXI-11, HiSLIP, raw socket) and RS-232. Because the agent writes raw SCPI, the server puts the safety in the plumbing. The read path accepts only single queries. Anything that changes the instrument goes through tools the MCP client asks you to confirm. You can give the server command allow and deny lists and a safe-state command of your own.

> **Keysight or Rohde & Schwarz instrument?** Use the vendor's official MCP server instead: [Keysight MCP Server for Instrument Control](https://www.keysight.com/us/en/lib/resources/software-releases/keysight-mcp-server-for-instrument-control.html) or the MCP server that ships with [RsInstrument](https://github.com/Rohde-Schwarz/RsInstrument) (`python -m RsInstrument.mcp`). `labmcp-scpi` covers everything else: Tektronix/Keithley, Rigol, Siglent, SRS, Aim-TTi, B&K Precision, GW Instek, older HP/Agilent GPIB gear, and more.

| | |
|---|---|
| **Package** | `labmcp-scpi` |
| **Instruments** | Any instrument that speaks SCPI or IEEE 488.2 common commands |
| **Interfaces** | GPIB, USBTMC, LAN VXI-11 / HiSLIP (all via VISA), LAN raw socket (e.g. port 5025), RS-232 / USB-serial |
| **Protocol** | SCPI-99 / IEEE 488.2 ([SCPI-99 standard, IVI Foundation](https://www.ivifoundation.org/downloads/SCPI/scpi-99.pdf)); VISA via [PyVISA](https://pyvisa.readthedocs.io/) |
| **Status** | 🧪 **simulated**: tested against a wire-level SCPI simulator, not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-scpi --simulate --check
```

The simulator is a small, generic bench instrument (`*IDN?` → `LabMCP,SIM-DMM-PSU,SIM000001,1.0.0`). It is a 0–30 V / 3 A supply driving a 100 Ω load, with a built-in DMM across the load. It supports `MEASure:VOLTage:DC?`, `CONFigure` + `READ?`/`FETCh?`, `[SOURce:]VOLTage`/`CURRent`, `OUTPut[:STATe]`, `TRIGger:COUNt`, `FORMat[:DATA] REAL,32|64` binary blocks, `FORMat:BORDer`, `*ESR?`/`*STB?`, and a 10-entry `SYSTem:ERRor?` queue (overflow gives `-350`). In `--simulate` mode, the safe state is `OUTP OFF`.

## Connect your instrument

1. **Instrument setup:** enable the remote interface you want to use.
   - **LAN:** set a fixed IP address. Enable VXI-11/HiSLIP (for VISA) or the SCPI raw-socket server. Many instruments use port 5025 for the socket; check the LAN settings page or the manual.
   - **USB:** select USBTMC / "USB device (TMC)", not "USB mass storage" or a virtual COM port.
   - **GPIB:** note the instrument's GPIB address.
   - **RS-232:** set the baud rate, parity and handshake, and select SCPI as the language if the instrument asks.
2. **Find it:** `uvx labmcp ports`, or ask the agent to run `list_visa_resources`. That tool works without `--address` and lists VISA resources through pyvisa-py; pass `backend=@ivi` to use NI-VISA or another vendor VISA library.
3. **Test the connection:**
   ```bash
   uvx labmcp-scpi --address visa://TCPIP0::192.168.1.50::inst0::INSTR --check      # LAN, VXI-11
   uvx labmcp-scpi --address visa://TCPIP0::192.168.1.50::hislip0::INSTR --check    # LAN, HiSLIP
   uvx labmcp-scpi --address tcp://192.168.1.50:5025 --check                        # raw socket
   uvx labmcp-scpi --address "visa://USB0::0x1AB1::0x0588::DS1ZA000000::INSTR" --check
   uvx labmcp-scpi --address "visa://GPIB0::22::INSTR?backend=@ivi" --check         # NI-VISA
   uvx labmcp-scpi --address "serial:///dev/ttyUSB0?baudrate=9600&read_termination=CRLF" --check
   ```
   `--check` prints the parsed `*IDN?` reply. If it times out, check the terminator: SCPI uses LF by default, and some serial instruments need `read_termination=CRLF`. Also check the baud rate, and that the instrument is set to SCPI rather than an emulation language.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add scope -- uvx labmcp-scpi --address visa://TCPIP0::192.168.1.50::inst0::INSTR
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "psu": {
      "command": "uvx",
      "args": [
        "labmcp-scpi", "--address", "tcp://192.168.1.50:5025",
        "--option", "safe_state=OUTP OFF",
        "--option", "write_denylist=^OUTP\\d*(:STAT)? (ON|1)$"
      ]
    }
  }
}
```

Add `--read-only` to allow only queries. In read-only mode `scpi_write`, `scpi_batch` and `reset_instrument` disappear. `device_clear` and `apply_safe_state` stay available. Run one server per instrument, each with its own key (`scope`, `psu`, `dmm`, …).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `apply_safe_state` | 🛑 safety | Put the instrument into the lab's configured safe state (the `--option safe_state` program message, e.g. 'OUTP OFF'), then read the error queue. Only listed when a safe state is configured. Call it immediately if anything looks wrong. |
| `device_clear` | 🛑 safety | Recover a stuck or confused instrument: VISA device clear (or discard unread input on socket/serial links), report the error queue, optionally ABORt, then *CLS. This does NOT make an arbitrary instrument safe (outputs stay as they are): use `apply_safe_state` if the lab configured one, or the instrument's own controls. |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_errors` | 👁 read | Read and clear the instrument's error/event queue (SYSTem:ERRor? until 0,"No error"). Entries are returned oldest first; each can only be read once. An empty list means no errors. |
| `identify` | 👁 read | Ask the instrument who it is (*IDN?): manufacturer, model, serial number, firmware. |
| `list_visa_resources` | 👁 read | List GPIB / USBTMC / LAN (VXI-11, HiSLIP) instruments visible to VISA on this computer. Works without --address. Raw-socket instruments (tcp://host:5025) are not discoverable. |
| `query_binary_block` | 👁 read | Read an IEEE 488.2 definite-length binary block (waveforms, trace data, screenshots, FORMat REAL/INTeger readings). The query must pass the same read-only checks as `scpi_query`. Returns length, SHA-256 and (optionally) decoded values with summary statistics, downsampled to max_points; use save_path for the full data. Blocks larger than the `max_block_bytes` limit are discarded. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `reset_instrument` | ⚠️ hazard | Reset the instrument to its default settings (*RST, then *CLS), wait for completion and check errors. SCPI requires outputs OFF after *RST, but every other setting (levels, ranges, triggers, limits) returns to its default and some instruments differ: check the manual before resetting anything connected to a device under test. |
| `scpi_batch` | ⚠️ hazard | Run a short sequence of SCPI commands and queries in order, checking the error queue after every step. Every step is checked against the command policy BEFORE the first one is sent, so a refused step means nothing was sent. Returns per-step replies and errors. |
| `scpi_primer` | 👁 read | Concise SCPI syntax guide (long/short forms, queries, compound commands, common commands, error queue, binary blocks) plus this server's active command policy. Works without a connection. |
| `scpi_query` | 👁 read | Send ONE read-only SCPI query and return the instrument's text reply. |
| `scpi_write` | ⚠️ hazard | Send any SCPI program message (settings, compound 'A;B' messages, queries with side effects) and then read the error queue. If the message contains a query, its reply is returned. This can change the instrument's state, including switching outputs on; say what the command does before calling it. Commands in the lab's denylist, or outside its allowlist, are refused before anything is sent. |
| `wait_operation_complete` | 👁 read | Wait until the instrument has finished all pending operations (*OPC? returns 1), e.g. after starting a sweep, an acquisition or a settling source. |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. `apply_safe_state` is listed only when a safe state is configured.

## Command policy

A generic server cannot know which SCPI commands are dangerous on *your* instrument. It enforces what it can and lets you add the rest:

| Rule | Where | What happens |
|---|---|---|
| Single query only | `scpi_query`, `query_binary_block` (READ, allowed in read-only mode) | The header must end in `?` (parameters such as `VOLT? MAX` are fine). Messages with `;`, line breaks or control characters are refused. |
| Built-in side-effect queries | `scpi_query`, `query_binary_block` | `*TST?` (self-test), `*CAL?`, `CALibration…?` and `DIAGnostic…?` are refused here. They must go through `scpi_write`. |
| `--option query_denylist=REGEX` | `scpi_query`, `query_binary_block` | Extra queries that your lab treats as having side effects, e.g. `^(MEAS\|READ)` on an SMU. They then need `scpi_write`. |
| `--option write_denylist=REGEX` (alias `denylist`) | every raw tool | No tool ever sends a matching command. |
| `--option write_allowlist=REGEX` | `scpi_write`, `scpi_batch`, `reset_instrument` | If set, these tools may only send commands that match (queries that `scpi_query` accepts are always allowed). Compound `;` messages are refused. |
| `--option safe_state=MESSAGE` | `apply_safe_state` (SAFETY) | The program message that makes *your* instrument safe, e.g. `OUTP OFF`. It is sent as-is and never blocked by the lists. |
| `--option error_query=QUERY` | all writes | Error-queue query, default `SYST:ERR?`. |

Matching rules:
- Patterns are Python regular expressions, matched case-insensitively.
- Each `;`-separated message unit is checked in two forms. The first is the unit as sent, upper-cased, with collapsed whitespace and no leading colon. The second reduces every mnemonic to its SCPI short form (SCPI-99 Vol. 1 §6.2.1): `:OUTPut1:STATe ON` becomes `OUTP1:STAT ON`. So `^OUTP\d*(:STAT)? (ON|1)$` catches `OUTP ON`, `outp:stat 1`, `VOLT 5;:OUTPut:STATe ON` and `OUTPut2 ON`.
- The denylist uses `re.search`. The allowlist must match a whole unit.
- Every step of `scpi_batch` is checked **before the first step is sent**.

Examples:

```bash
# Power supply: the agent may set levels, but only a human switches the output on
--option 'write_denylist=^OUTP\d*(:STAT)? (ON|1)$' --option 'safe_state=OUTP OFF'
# DMM: allow measurement configuration only
--option 'write_allowlist=(CONF|SENS|TRIG|INIT|FORM|ABOR)(:\S+)*( .*)?'
# SMU: measuring can switch the output on, so require confirmation for it
--option 'query_denylist=^(MEAS|READ)' --option 'safe_state=OUTP OFF'
```

The environment variable `LABMCP_OPTIONS` splits on commas. Pass regular expressions that contain commas on the command line instead.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_operation_wait_s` | 300 s | Longest `*OPC?` wait an agent may request (`wait_operation_complete`) |
| `max_block_bytes` | 16777216 bytes | Largest binary block the server reads. Larger blocks are discarded before their payload is read. |

Override at launch: `--limit max_block_bytes=67108864`. This server has no voltage or current limits because it cannot interpret arbitrary commands. Set limits on the instrument itself (OVP/OCP, compliance, output protection), use a denylist, or use an instrument-specific server.

## Example prompts

- "What instrument is connected? Read its identity and check the error queue."
- "Configure the DMM for DC volts on the 10 V range, take 20 readings, and give me the mean and standard deviation."
- "Set the supply to 3.3 V with a 200 mA current limit, read both back, but don't enable the output yet."
- "Grab channel 1's waveform from the scope as a binary block, save it to ~/data/ch1.csv, and summarise min, max and mean."
- "We got a -222 error. What does it mean, and which of the last commands caused it?"
- "Something's wrong: apply the safe state, then clear the interface."

## Notes

- **A generic server cannot make an arbitrary instrument safe.** `device_clear` does four things: a VISA device clear (on raw socket and serial links it only discards unread input), reads the error queue, sends `ABORt`, and sends `*CLS`. It recovers communication and stops sweeps. It does **not** switch outputs off. Configure `safe_state` for anything that sources energy. For hazardous gear, use an instrument-specific LabMCP server where one exists: it can enforce real setpoint limits.
- **Queries are not always read-only.** On source-measure units such as the Keithley 2400, `MEASure?`, `READ?`, `INITiate` and `CONFigure` can switch the source output on (Keithley *2400 Series SourceMeter User's Manual*, 2400S-900-01 Rev. G, §11 and §17). In `--read-only` mode, `scpi_query` therefore refuses `MEASure?` and `READ?` by default, while `FETCh?` still returns the last reading. Set `--option allow_measure_in_read_only=true` if the instrument is a meter that cannot source. When not read-only, add `--option query_denylist=^(MEAS|READ)` for SMUs so these commands need confirmation through `scpi_write`.
- **`*RST`**: SCPI-99 Vol. 2 §15.12 requires `OUTPut:STATe OFF` after `*RST`. But `*RST` also resets levels, ranges, limits and trigger settings, and not every instrument complies.
- The error queue is read (and so cleared) after every `scpi_write`/`scpi_batch` step. The errors appear in the result. A query the instrument doesn't recognise produces **no reply**: the server times out (5 s by default, `--timeout` to change), resynchronises, and reports the `-113` from the queue.
- Instruments that don't implement `SYSTem:ERRor?` (some older IEEE 488.2-only gear) report `error_check: "unavailable"`. Set `--option error_query=…` if your instrument uses a different error query.
- `query_binary_block` reads definite-length blocks (`#<n><len><data>`) only. Indefinite `#0` blocks are refused. Byte order follows `FORMat:BORDer` (NORMal = big-endian).
- Text is sent as ASCII. Commands with control or non-ASCII characters are refused, and the server adds the LF terminator.
- A denylist is a guard rail, not a guarantee. It matches text, so vendor-specific aliases or macros (`*DMC`, user-defined sequences) can evade it. Keep the instrument's own protection limits set.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
