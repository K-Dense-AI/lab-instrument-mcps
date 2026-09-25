# ASTM LIS Analyzer Receiver — MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-astm-lis -->

Let an AI agent collect and query results from **clinical and laboratory analyzers** (hematology, clinical chemistry, immunoassay, urinalysis, coagulation) that upload results over the standard **ASTM E1381 / E1394** host interface (CLSI LIS1-A / LIS2-A2). The server acts as a small, receive-only LIS endpoint: it acknowledges the analyzer's frames, validates checksums, reassembles messages and lets the agent look results up by sample, patient, test or time.

> [!WARNING]
> **Research and lab-operations use only. This is not a medical device and not a validated laboratory information system.** It must not be used to report, release or interpret patient results, for diagnosis, or for any clinical decision-making. Results pass through an AI agent that can misread them; always rely on your laboratory's validated LIS/middleware and release process. Patient data is personal health information: patient name, birth date, address and phone are redacted by default; follow your institution's privacy rules before changing that.

| | |
|---|---|
| **Package** | `labmcp-astm-lis` |
| **Instruments** | Analyzers whose host-interface manual specifies ASTM E1381/E1394 (CLSI LIS1-A/LIS2-A2) result upload, e.g. models from Sysmex, Roche (cobas), Abbott, Beckman Coulter, Mindray and others. Check your model's host-interface manual. |
| **Interfaces** | RS-232 (`serial://`), TCP/IP with the analyzer as server (`tcp://analyzer:port`), TCP/IP with the analyzer as client (`--option listen_port=5000`) |
| **Protocol** | ASTM E1381 low-level protocol (ENQ/ACK/NAK/EOT, framed text with modulo-8 frame numbers and checksums) + ASTM E1394 records H, P, O, R, C, Q, L. Implemented from the publicly available [Beckman Coulter UniCel DxI / Access 2 LIS Vendor Information, C03112-AF](https://www.beckmancoulter.com/download/file/wsr-228138/C03112AF?type=pdf) (chapters 2–4); the ASTM/CLSI standards themselves are purchase-only. **Receive-only.** |
| **Status** | 🧪 **simulated**: tested against a wire-level analyzer simulator and loopback TCP, not yet verified on an analyzer. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-astm-lis --simulate --check
```

The simulated analyzer uploads two messages right after start: a CBC with 5-part differential (with H/L flags and suspect-flag comments, one record long enough to need two frames, and one frame corrupted on the line that is NAKed and retransmitted) and a chemistry panel (high glucose and potassium with a hemolysis comment). All patient data in it is fictitious.

## Connect your analyzer

On the analyzer, open the host / LIS / "online" communication settings:

1. **Protocol:** ASTM (may be called ASTM E1381/E1394, LIS1-A/LIS2-A2, CLSI or "ASTM host"). Keep the ASTM low-level protocol (ENQ/STX framing) enabled, also over TCP/IP.
2. **Results:** enable automatic result upload.
3. **Queries and orders:** this server does not answer host queries or download orders. Turn off "host query" / "order request" / bidirectional mode, otherwise the analyzer waits for (and times out on) an answer before running samples.
4. **Link:** pick one of
   - **RS-232**: note the baud rate (commonly 9600), 8 data bits, no parity, 1 stop bit (fixed by E1381), and use a null-modem cable.
     ```bash
     uvx labmcp-astm-lis --address /dev/ttyUSB0 --check                        # Linux
     uvx labmcp-astm-lis --address serial://COM3?baudrate=19200 --check        # Windows, non-default baud
     ```
   - **TCP, analyzer is the server** (it listens on a port): the LIS connects to it.
     ```bash
     uvx labmcp-astm-lis --address tcp://192.168.1.80:5000 --check
     ```
   - **TCP, analyzer is the client** (you enter the host IP/port on the analyzer): this server listens. Allow the port through the computer's firewall and restrict senders with `allow_from`.
     ```bash
     uvx labmcp-astm-lis --option listen_port=5000 --option allow_from=192.168.1.80 --check
     ```
5. Optional: keep a local record of every message with `--option store_path=~/lis/results.jsonl` (reloaded when the server restarts).

The receiver starts as soon as the MCP server starts, so uploads are acknowledged even before the agent calls a tool.

## Add to your MCP client

**Claude Code**
```bash
claude mcp add analyzer -- uvx labmcp-astm-lis --option listen_port=5000 --option store_path=~/lis/results.jsonl
```

**Claude Desktop / Cursor / Windsurf** (`claude_desktop_config.json`, `.cursor/mcp.json`, …)
```json
{
  "mcpServers": {
    "analyzer": {
      "command": "uvx",
      "args": ["labmcp-astm-lis", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

Add `--read-only` to hide `clear_results`. For other clients, generate the snippet with `uvx labmcp config astm-lis --address /dev/ttyUSB0 --client vscode` (also `cursor`, `codex`, `claude-code`).

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `clear_results` | 🎛 control | Delete all received messages and results from this server's memory (e.g. between runs). |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_connection_status` | 👁 read | Report the analyzer link state, the messages and results received, and link-layer error counters. |
| `get_result_detail` | 👁 read | Show every decoded field of one result, with its comments, order and patient context, raw records and message header. |
| `get_results` | 👁 read | Return received results, filtered by sample ID, patient ID, test code, time received or abnormal flag (newest last). |
| `list_received_messages` | 👁 read | List the most recent ASTM messages received (newest last). |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server.

## Safety limits

This server is receive-only and never commands the analyzer (it only answers ACK/NAK), so it has no physical safety limits. Privacy settings:

| Option | Default | Meaning |
|---|---|---|
| `redact_patient_info` | `true` | Masks P-record fields 6 (patient name), 7 (mother's maiden name), 8 (birth date), 11 (address) and 13 (telephone) as `REDACTED` before anything is stored, logged, written to `store_path` or returned by a tool |
| `allow_from` | (any) | In listen mode, only accept connections from these IP addresses (`;`- or space-separated) |
| `max_messages` | 5000 | Messages kept in memory; oldest are dropped first |

Patient IDs, sex, attending physician and free-text comment records are **not** redacted (IDs are needed to filter results). If your analyzers put identifying text in comments, treat the output accordingly.

## Example prompts

- "Have any results come in from the hematology analyzer in the last hour? List the samples."
- "Show me all flagged results for sample S26-000123, with units, reference ranges and the analyzer's comments."
- "Export today's potassium results as a table of sample ID, value and flag."
- "Is the analyzer link healthy? How many frames were NAKed today and why?"
- "Compare the glucose results of samples S26-000124 and S26-000131."
- "Clear the stored results before we start the next validation run."

## Notes

- **Receive-only.** No order download, no host-query responses and no result acknowledgements beyond the link-level ACK. Query (Q) messages from the analyzer are stored and reported (`kind: "query"`) but not answered. Order download is planned future work.
- **Link layer (E1381).** Every frame is checked: checksum (modulo-256 sum from the frame number to ETX/ETB, as two hex digits), frame number (first frame 1, then modulo 8; a repeat of the last accepted number is treated as a retransmission and ACKed without duplicating data) and framing. Defective frames get NAK and the analyzer retransmits them. Frames outside an ENQ … EOT session are ignored and counted in `bytes_ignored`, which usually means the analyzer is not using the ASTM low-level protocol (raw E1394 without framing is not supported). A session that goes silent for 30 s, or restarts with ENQ, has its incomplete message discarded, as E1381 requires; a message that ends with EOT but no terminator (L) record is kept and flagged `complete: false`.
- **Tolerances.** Frames with more than 240 text characters and end frames that pack several records are accepted (and counted); a frame whose CR LF never arrives is NAKed after 10 s. Text is decoded as Latin-1 by default (`--option encoding=...`).
- **Records (E1394).** Delimiters are taken from each header record (`H|\^&` by default). Escape sequences `&F& &S& &R& &E&` and the vendor form `&|&` are decoded; other escapes are kept verbatim. The test code is the first non-empty "local code" component of the Universal Test ID (`^^^TSH`, `^^^^WBC`), falling back to the first component. The sample ID is the order's Specimen ID, or the first non-empty component of the Instrument Specimen ID. Values are returned exactly as sent (`"<0.1"`, `">500"`, `"Cancelled"`); `numeric_value` is only set for plain numbers.
- **Flags and statuses** are passed through as sent. Meanings are only attached for the codes documented in the vendor specification used (`N L H LL HH < >`, status `F X`); anything else is analyzer-specific, so check your analyzer's host-interface manual.
- Timestamps inside records (test started/completed, header time) are the analyzer's local time; `received_at` is when this server stored the message (UTC).
- **Listen mode** serves one analyzer per port: a new connection replaces the previous one (analyzers often reconnect after a reboot without closing the old socket). Run one server entry (or port) per analyzer. There is no TLS; keep the port on the lab network and use `allow_from`.
- Only tested against the simulator and loopback TCP. Differences between vendors' ASTM dialects are common; please report what works (or doesn't) with your analyzer.

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
