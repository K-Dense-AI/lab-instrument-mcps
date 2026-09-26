# Thermo Orbitrap (Instrument API): MCP Server

<!-- mcp-name: io.github.K-Dense-AI/labmcp-thermo-iapi -->

Let an AI agent watch and steer a **Thermo Fisher Orbitrap mass spectrometer** in real time: check the instrument mode and vacuum, read the scans streaming off the instrument (filtered by MS order, with the most intense centroids), place validated custom scans and pick up their results, set a repeating scan, and start, pause or stop acquisitions to a raw file. It does this through Thermo's own **Instrument API (IAPI)**.

> [!IMPORTANT]
> **This is an adapter. It contains no Thermo software.** The IAPI is a Windows .NET API. To use it you need a **signed IAPI software licence agreement with Thermo Fisher Scientific**, with the licence applied to your instrument ([legal requirements](https://github.com/thermofisherlsms/iapi/blob/master/GettingStarted.md#legal-requirements), [licence FAQ](https://github.com/thermofisherlsms/iapi/blob/master/docs/API%20license%20agreement%20FAQs%20r1.3.pdf), [how to obtain a licence](https://github.com/thermofisherlsms/iapi/blob/master/docs/Orbitrap_IAPI_Online_licensing_guidance_material.pdf)). This package **never bundles or downloads** the IAPI assemblies: you point it at the ones you obtained. **It has never been tested on real hardware.** Everything below was developed against the official IAPI interface definitions and examples and a simulator.

| | |
|---|---|
| **Package** | `labmcp-thermo-iapi` (install the `[windows]` extra on the instrument PC for pythonnet) |
| **Instruments** | Orbitrap Tribrid (Fusion, Fusion Lumos, Eclipse, Ascend), Orbitrap Exploris 240/480, Q Exactive family. Not the Orbitrap Astral: Thermo states the Astral line does not support IAPI (as of fall 2025). Not the Exploris 120, which Thermo's examples say doesn't support the API. |
| **Interfaces** | .NET API on the Windows instrument PC (through [pythonnet](https://pythonnet.github.io/)) |
| **Protocol** | [Thermo Fisher Instrument API (IAPI)](https://github.com/thermofisherlsms/iapi): interface definitions in `lib/*.xml`, examples in `examples/` |
| **Status** | 🧪 **simulated**: tested against a simulated Orbitrap (FakeOrbitrap), not yet verified on hardware. [Report a hardware test](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml) |

## Try it without hardware

```bash
uvx labmcp-thermo-iapi --simulate --check
```

The simulator is an Orbitrap Exploris 480 by default (`--option sim_model=eclipse` or `qexactive-hf` for the others). It runs a synthetic tryptic digest: MS1 survey scans in On mode, a data-dependent top-10 method during an acquisition, custom and repeating scans that come back with their access id, realistic transient and injection times, and a status log with vacuum readings every ~5 s.

## Connect your instrument

On the **Windows instrument PC**, with Tune installed and your IAPI licence applied:

1. **Get the IAPI assemblies** that come with your licence (for a Tribrid: `API-2.0.dll`, `Spectrum-1.0.dll`, `Thermo.TNG.Factory.dll` and `Fusion.API-2.0.dll` for Tune 4.3 or `Fusion.API-1.0.dll` for Tune 4.2 and earlier). Put them in a folder, e.g. `C:\IAPI`.
2. **Install** Python 3.10+ and the server with pythonnet:
   ```powershell
   pip install "labmcp-thermo-iapi[windows]"
   ```
3. **Test the connection** (the instrument family picks the connection recipe from the IAPI examples):
   ```powershell
   labmcp-thermo-iapi --option instrument=tribrid  --option assembly_dir=C:\IAPI --check   # Fusion/Lumos/Eclipse/Ascend
   labmcp-thermo-iapi --option instrument=exploris --option assembly_dir=C:\IAPI --check   # Exploris 240/480
   labmcp-thermo-iapi --option instrument=exactive --option assembly_dir=C:\IAPI --check   # Q Exactive family
   ```

| Family | How the server connects (from the IAPI repository) | Assemblies expected in `assembly_dir` |
|---|---|---|
| `tribrid` | `Factory<IFusionInstrumentAccessContainer>.Create()`, `StartOnlineAccess()`, `Get(1)` | `API-2.0.dll`, `Spectrum-1.0.dll`, `Thermo.TNG.Factory.dll`, `Fusion.API-2.0.dll` or `Fusion.API-1.0.dll` |
| `exploris` | `HKLM\SOFTWARE\Thermo Exploris` → `DataSystem.xml` (`ApiFileName`, `ApiClassName`) | `Thermo.API.NetStd-1.0.dll`, `Thermo.API.Exploris.NetStd-1.0.dll`, `Thermo.API.Spectrum.NetStd-1.0.dll` (4.3+) or the `Thermo.API-2.0` / `Thermo.API.Exploris-1.0` / `Thermo.API.Spectrum-1.x` set |
| `exactive` | `HKLM\SOFTWARE\Finnigan\Xcalibur\Devices\Thermo Exactive` (`ApiFileName_Clr2_32_V1`, `ApiClassName_Clr2_32_V1`) | `API-2.0.dll` (or 1.x), `Spectrum-1.0.dll`, `ESAPI-1.1.dll` (or 1.0) |

The server gives a specific message if it is not running on Windows, pythonnet is missing, the folder or an assembly is missing, the IAPI service does not connect (Tune not running), or IAPI refuses a command for lack of a licence.

Other options: `readbacks=Name1;Name2` (instrument values to show in `get_instrument_status`; the tool lists the names your instrument exposes), `exclusive_scans=true` (exclusive rather than cooperative `IScans` access), `runtime=coreclr`, `connect_timeout_s`, `buffer_size` (scans kept in memory, default 500).

## Add to your MCP client

```bash
claude mcp add orbitrap -- labmcp-thermo-iapi --option instrument=tribrid --option assembly_dir=C:\IAPI
```

```json
{
  "mcpServers": {
    "orbitrap": {
      "command": "labmcp-thermo-iapi",
      "args": ["--option", "instrument=exploris", "--option", "assembly_dir=C:\\IAPI"]
    }
  }
}
```

Add `--read-only` to let the agent watch scans and status but not place scans or start acquisitions. The stop and cancel tools stay available. To reach the server from another computer, run it with `--transport http --host 0.0.0.0 --port 8000` on the instrument PC and protect that port.

## Tools

<!-- TOOLS:START -->
| Tool | Kind | Description |
|---|---|---|
| `cancel_custom_scans` | 🛑 safety | Cancel any pending custom scan and its processing delay (IAPI CancelCustomScan). |
| `cancel_repeating_scan` | 🛑 safety | Cancel the repeating scan set with set_repeating_scan (IAPI CancelRepetition). |
| `get_command_log` | 👁 read | Return the most recent raw commands sent to / replies received from the instrument (newest last). Useful for debugging and for recording what was done. |
| `get_connection_info` | 👁 read | Report which instrument is connected (identity, address, simulated or real), whether the server is read-only, and the active safety limits. Call this first. |
| `get_instrument_status` | 👁 read | Report the instrument model, IAPI service/instrument connection, system mode and state (On/Standby/Off; Running/ReadyToDownload/...), whether an acquisition can be paused or resumed, the IAPI licence where the API exposes it, requested readbacks, the latest scan status log (vacuum, source) and how many scans have arrived. |
| `get_possible_scan_parameters` | 👁 read | List the scan parameters this instrument accepts for custom and repeating scans (IScans.PossibleParameters): name, allowed range or choices, default and help. The set depends on the model, licence and Tune version. |
| `get_recent_scans` | 👁 read | Return the most recent scans received from the instrument (oldest first), optionally only one MS order or one custom scan's access id: scan number, MS order, precursor m/z, AGC target, injection time and the most intense centroids. Scans only arrive in On mode. |
| `pause_acquisition` | 🛑 safety | Pause the running acquisition (IAPI Pause). Fails if the instrument reports it cannot pause. |
| `reconnect` | 🛑 safety | Close and re-open the connection to the instrument (e.g. after it was power cycled or a cable was re-plugged). |
| `resume_acquisition` | ⚠️ hazard | Resume a paused acquisition (IAPI Resume). Sample consumption continues. |
| `set_repeating_scan` | ⚠️ hazard | Define or replace the scan the instrument repeats when no method or custom scan is running (IAPI CreateRepeatingScan/SetRepetitionScan). Values are validated like submit_custom_scan. Cancel with cancel_repeating_scan. |
| `start_acquisition` | ⚠️ hazard | Start an acquisition with the instrument's current settings (IAPI StartAcquisition), recording to a raw file. This consumes sample. The instrument must be On; an acquisition must not already be running. Stop it with stop_acquisition. scan_count and until_stopped acquisitions are cancelled automatically after max_acquisition_duration_s. |
| `stop_acquisition` | 🛑 safety | Stop the running acquisition (IAPI CancelAcquisition), by default also cancelling custom and repeating scans, and optionally switch the instrument to Standby (switch back to On in Tune). Every step is attempted even if an earlier one fails; failures are reported. |
| `submit_custom_scan` | ⚠️ hazard | Place one custom scan to run next (IAPI CreateCustomScan/SetCustomScan); unset values fall back to the instrument's defaults. Every value is checked against PossibleParameters and the safety limits, and calls are rate-limited, before anything is sent. Fetch the result with wait_for_scan(access_id=running_number). |
| `wait_for_scan` | 👁 read | Wait for the next scan that arrives after this call (optionally of one MS order, or the result of a custom scan by its access id) and return it. For a custom scan placed with submit_custom_scan, a result that arrived since it was placed is returned at once. Returns found=false after timeout_s if nothing matching arrived (e.g. the instrument is in Standby). |
<!-- TOOLS:END -->

`get_connection_info`, `get_command_log` and `reconnect` are built into every LabMCP server. Every IAPI control call (with the exact scan values sent) is recorded in the command log.

## Safety limits

| Limit | Default | Meaning |
|---|---|---|
| `max_custom_scans_per_minute` | 60 scans/min | Custom scans an agent may place in any 60 s window (server-side rate limit; `reconnect` does not reset it) |
| `max_injection_time_ms` | 1000 ms | Largest maximum injection time (`MaxIT`) in a custom or repeating scan (every element of a multi-valued `MaxIT`) |
| `max_acquisition_duration_s` | 7200 s | Longest acquisition an agent may start: a `duration` above it is refused, and `scan_count` / `until_stopped` acquisitions are cancelled automatically when it runs out (wall-clock, pauses included) |

Override at launch: `--limit max_custom_scans_per_minute=600`.

On top of these limits, every custom and repeating scan value is checked against the instrument's own `IScans.PossibleParameters` before anything is sent: the parameter name must exist, numbers must lie inside the instrument's range (for example the m/z range for `FirstMass`/`LastMass`), and choice parameters such as `OrbitrapResolution` must be one of the allowed values. IAPI itself silently ignores illegal values, which is why the server checks first, and why accepted values are sent in the instrument's own spelling (`hcd` goes out as `HCD`, `2.0` for an integer parameter as `2`).

`stop_acquisition` attempts every step (cancel the acquisition, the custom scans, the repeating scan, Standby) even if one fails, and reports what failed.

## Example prompts

- "Is the Orbitrap in On mode and what are the vacuum readings? How many scans came in during the last minute?"
- "Show me the last 5 MS2 scans with their precursor m/z, injection times and top 10 fragments."
- "Place a 120k SIM scan on m/z 524.26 with a 10 m/z window and tell me the most intense peaks you see."
- "Start a 30-minute acquisition to D:\Data\HeLa_test.raw with sample name HeLa 200 ng, then check every few minutes that MS2 scans are still arriving."

## Notes

- **Instrument mode:** scans only arrive in **On** mode, and custom scans and acquisitions need it too. `stop_acquisition(standby=true)` switches to Standby. This server has no tool to switch back to On (do that in Tune).
- **Custom scans:** values you leave out come from the instrument's defaults, as Thermo documents for `IScanDefinition.Values`. `running_number` comes back as the scan's `access_id` (trailer item `Access Id:`). The server numbers scans automatically if you don't. A custom scan usually finishes before the agent's next tool call, so `wait_for_scan(access_id=...)` also returns a result that arrived after the scan was placed but before the wait started. IAPI leaves the result undefined when several custom scans are placed before the instrument has processed the earlier ones; the rate limit keeps this under control.
- **Parameter names differ by model and Tune version** (e.g. `Analyzer` on Tribrids). Use `get_possible_scan_parameters` and pass anything that has no named argument through `extra_parameters`.
- **Scan headers:** `scan_number`, `ms_order`, `scan_mode` and `precursor_mz` come from header keys used in Thermo's examples (`Scan`, `MSOrder`, `ScanMode`, `PrecursorMass[0]`). `master_scan_number`, `agc_target` and `injection_time_ms` are read from the trailer names Thermo raw files use (`Master Scan Number:`, `AGC Target:`, `Ion Injection Time (ms):`). The IAPI repository doesn't document these, so they may be `null` on your instrument. Use `include_header_trailer=true` to see exactly what it sends.
- **Readbacks:** IAPI has no fixed list of readback names (vacuum gauges, voltages). `get_instrument_status` shows the names from `IInstrumentValues.ValueNames` and the latest scan `StatusLog`, where the vacuum readings usually appear.
- **Licence check:** on Exploris instruments the server reports whether an API licence is present (`IExplorisInstrumentAccess.Licenses`). Tribrid and Exactive instruments enforce the licence in the instrument service, and a refused command reports it.
- **Verified API surface:** every IAPI member the pythonnet backend touches is listed with its source file in [`iapi_members.py`](src/labmcp_thermo_iapi/iapi_members.py) (checked against [thermofisherlsms/iapi@c246dcc](https://github.com/thermofisherlsms/iapi/tree/c246dcc8772d03c9c32e9b2fde486e97572c8fbf)). A test fails if the backend uses a name that isn't in that list.
- **Scan data:** each scan's .NET object is copied and disposed at once, as Thermo's examples require (otherwise the instrument's shared memory stays blocked). Up to 5 000 centroids per scan are kept. `max_centroids` limits what a tool returns, and `get_recent_scans(save_path=...)` writes all kept centroids to a new CSV file (the path must end in `.csv` and must not exist yet: existing files are never overwritten).

## Hardware verification

| Model | Firmware | Interface | Verified by | Date |
|---|---|---|---|---|
| *none yet: [be the first](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml)* | | | | |
