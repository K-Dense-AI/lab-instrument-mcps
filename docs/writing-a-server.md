# Writing a LabMCP server

This guide covers adding a new instrument server. The reference implementation is
[`servers/chemistry/mettler-toledo-balance`](../servers/chemistry/mettler-toledo-balance). Read it first: it's short.

## 0. Check it belongs here

- **No official MCP server exists** for the instrument. If the vendor ships one, link to it in [docs/official-servers.md](official-servers.md) instead of building a competing server.
- **Build on a documented or open interface**: a published command set (MT-SICS, SCPI, NAMUR, MethodSCRIPT…), an open-source library (python-seabreeze, BrainFlow, pycromanager…), or a documented HTTP API. Don't use reverse-engineered protocols without saying so clearly in the README.
- **One server per instrument family**, meaning a set of instruments that share a protocol (e.g. *all* MT-SICS balances), not one server per model.

## 1. Scaffold

```bash
uv run python scripts/new_server.py --domain chemistry --slug ika-stirrer \
    --package labmcp-ika --name "IKA Hotplate Stirrer" --vendor IKA
uv sync --all-packages
```

Domains: `biology`, `chemistry`, `physics`, `health`, `engineering`, `protocols` (generic protocols that cover many instruments, e.g. SCPI, Modbus, SiLA 2).

The scaffold produces:

```
servers/<domain>/<slug>/
├── pyproject.toml          # [project] + [tool.labmcp] catalog metadata
├── README.md               # user docs (tools table is generated)
├── src/<module>/
│   ├── driver.py           # protocol implementation, no MCP code in here
│   ├── simulator.py        # wire-level simulator
│   └── server.py           # InstrumentServer + tools
└── tests/test_server.py
```

## 2. Driver (`driver.py`)

- A plain Python class that takes a `labmcp.Transport` (or, for SDK-based instruments, whatever handle the SDK gives you). No FastMCP imports.
- Parse every reply strictly. Raise `InstrumentProtocolError` with a message that says **what the instrument said and what it means** ("Balance replied `S +`: overload, too much weight on the pan").
- Hold `self.t.lock` for multi-step exchanges that must not interleave with other tool calls, but **only for one exchange at a time**. Tool calls run concurrently, and a SAFETY tool (stop, output off) has to take the same lock to send its command, so it waits for as long as another thread holds it. In long operations (sweeps, ramps, waits, acquisitions), take the lock per exchange and check a `threading.Event` that your stop tool sets between steps (see the Julabo and PalmSens drivers).
- Implement `identify() -> dict` (manufacturer, model, serial, firmware…). `get_connection_info` shows it.
- Implement `close()`.
- Only implement commands you have **verified in the vendor's manual**. Cite the manual (title + document number/URL) in the module docstring.

## 3. Simulator (`simulator.py`)

Every server ships a simulator so it can be tested in CI and tried without hardware.

- **Byte/line protocols:** subclass `labmcp.LineSimulator` (implement `handle(command) -> reply | [replies] | None`) or `labmcp.ByteSimulator` (`handle_bytes(data) -> bytes`). The simulator must reproduce the **exact wire format** from the manual, including error replies, so that `--simulate` exercises the real parsing code.
- **Instruments that stream on their own** (a running measurement, a sensor in continuous mode): add a `poll()` method to the simulator that returns whatever is due now. The simulated transport calls it whenever the driver waits for input.
- **SCPI instruments:** subclass `labmcp.scpi.SCPISimulator`. It already handles `*IDN?`, `*RST`, `*CLS`, `*OPC?`, `SYST:ERR?`, and use `self.matches(key, "SOURce:VOLTage[:LEVel]?")` for short/long forms.
- **SDK-based instruments** (seabreeze, BrainFlow, nidaqmx, …): write a small fake class with the same interface as your driver's backend and select it in `connect()` when `ctx.simulate` is true. Prefer an SDK's own simulator when there is one (e.g. BrainFlow's synthetic board, NI-DAQmx simulated devices).
- Make simulated data physically plausible (noise, time constants, ramp rates), because people will demo with it.

## 4. Server (`server.py`)

```python
from labmcp import CONTROL, HAZARD, READ, SAFETY, ConnectContext, InstrumentServer, Limit

def connect(ctx: ConnectContext) -> MyDriver:
    t = ctx.open_transport(simulator=MySimulator, baudrate=9600,
                           read_termination="\r\n", write_termination="\r\n")
    return MyDriver(t)

server = InstrumentServer("Vendor Thing (Protocol)", connect=connect, package="labmcp-thing",
                          instructions="...", limits=[Limit("max_temperature_c", 150, "°C", "Hotplate setpoint")])
mcp = server.mcp

@mcp.tool(**READ)
def read_temperature() -> TemperatureReading: ...

def main() -> None:
    server.run()
```

### Tool kinds (required on every tool)

| Kind | Use for | Effect |
|---|---|---|
| `READ` | Measurements, status, settings queries | `readOnlyHint`; always available |
| `CONTROL` | Settings with no direct physical hazard (units, tare, display, zero, acquisition parameters) | Hidden in `--read-only` |
| `HAZARD` | Anything that **heats, cools, moves, dispenses, pressurises, energises an output, or consumes sample** | `destructiveHint`, so clients ask the user first. Hidden in `--read-only` |
| `SAFETY` | Stop, abort, outputs off, heater off | Always available, even in read-only mode |

If a HAZARD tool exists, a matching SAFETY tool (stop/off) must exist too.

### Tool design rules

1. **Scientist-level verbs**, not register pokes: `run_iv_sweep`, `acquire_spectrum`, `dispense_volume`. Keep a raw passthrough (`send_command`) only for SCPI-style generic servers, and mark it `HAZARD`.
2. **Units in names**: `temperature_c`, `flow_ml_min`, `voltage_v`, `wavelength_nm`, `duration_s`. Use SI-ish units the field already uses.
3. **Typed inputs with bounds**: `Annotated[float, Field(ge=0, le=2000, description="...")]`. Bounds that are hardware maxima go in `Field`; bounds a lab might want to tighten go in `Limit`s checked with `server.check(...)` **before** anything is sent.
4. **Structured outputs**: return Pydantic models (or dicts) so clients get `structuredContent`. Include timestamps on measurements.
5. **Long operations**: keep total runtime bounded by a `Limit` and enforce that deadline in the driver, and return summary statistics along with raw data. Don't rely on the tool's `timeout=`: FastMCP can't interrupt a sync tool running in a worker thread, so the call still runs to completion (and keeps holding the transport lock).
6. **Big data** (spectra, waveforms, images): return downsampled data plus summary stats by default, with a `max_points` parameter. Offer `save_path` to write full data to CSV/NPY/TIFF on disk. Check it with `labmcp.prepare_save_path(save_path, suffixes=(".csv",), overwrite=overwrite)` **before** acquiring, then open the returned path with mode `"x"` (or `"w"` if `overwrite`).
7. **No NaN or infinity in results.** JSON has neither: FastMCP sends `null`, and clients that validate structured output against the schema reject a `null` in a `float` field, so the whole result is lost. Use `float | None` for values that can be missing or over range.
8. **Docstrings are prompts.** The first paragraph goes in the README tools table. Say what the tool does, what the prerequisites are, and what can go wrong.
9. `instructions=`: 3–8 bullet points of instrument-specific operating guidance (e.g. "Always turn the heater off when finished").
10. Instruments that **push** data (an analyzer sending results) should pass `connect_on_start=True` so the server connects at launch rather than on the first tool call.

## 5. Tests (`tests/test_server.py`)

Minimum:

- Driver-level tests against the simulator for each command family, including at least one **error reply**.
- An MCP round-trip through `labmcp.testing.simulated_client(server)` that calls the main tools.
- `read_only=True` hides every CONTROL/HAZARD tool and keeps SAFETY tools.
- Each safety limit refuses an out-of-range request.

```bash
uv run pytest servers/chemistry/ika-stirrer
```

## 6. Metadata and docs

- Fill `[tool.labmcp]` in `pyproject.toml`: `name, domain, category, vendor, models, interfaces, protocol, summary, status`. New servers start as `status = "simulated"`; change it to `hardware-verified` only after someone has tested it on real hardware and reported the model and firmware.
- Write the README following the reference server: setup on the instrument side, the `--check` command, client config, safety limits, example prompts, notes, hardware verification table.
- Regenerate the catalog and the README tables:

```bash
uv run python scripts/build_catalog.py
```

## 7. Checklist

- [ ] No official MCP server exists (checked vendor site, GitHub, MCP Registry)
- [ ] Commands verified against a cited manual/SDK
- [ ] Simulator reproduces the wire format, including errors
- [ ] Every tool has a kind; hazards have a stop
- [ ] Limits on dangerous setpoints
- [ ] Tests pass; `--simulate --check` works
- [ ] README complete; catalog regenerated
