# Safety

LabMCP servers let AI agents operate **physical equipment**. Hotplates reach 300+ °C, power supplies source lethal voltages, pumps pressurise tubing, and robots move. Please read this before connecting an agent to anything that can hurt people, samples, or equipment.

## You are responsible

LabMCP is provided **as is**, without warranty (see [LICENSE](LICENSE)). The servers are research tools. They are **not** certified safety systems or medical devices, and they are **not** validated for GxP, clinical, or diagnostic use. You remain responsible for:

- Following your institution's lab-safety rules, risk assessments, and instrument SOPs.
- **Supervising** any agent that controls hazardous equipment. Never leave an agent running hazardous equipment unattended.
- Keeping the instrument's **own** safety features (over-temperature cut-offs, current limits, interlocks, E-stops) enabled and correctly set. Software limits add to hardware protection. They never replace it.

## What LabMCP does to help

| Layer | What it does |
|---|---|
| **Tool kinds** | Every tool is tagged `READ`, `CONTROL`, `HAZARD`, or `SAFETY`. `HAZARD` tools carry the MCP `destructiveHint`, so clients such as Claude ask the user before running them. |
| **Read-only mode** | `--read-only` (or `LABMCP_READ_ONLY=1`) removes every `CONTROL` and `HAZARD` tool from the server. The agent can observe but not act. `SAFETY` tools (stop/off) stay available. |
| **Safety limits** | Servers define limits on dangerous setpoints (temperature, voltage, current, flow, speed, volume…). Requests beyond a limit are refused **before anything is sent to the instrument**. Tighten them at launch: `--limit max_temperature_c=80`. |
| **Stop tools** | Every server that can actuate has a `SAFETY` tool to stop, abort, or switch outputs off, and it is never hidden. |
| **Audit trail** | Every command sent and reply received is logged. Agents can inspect it with `get_command_log`, and `--audit-log run.jsonl` keeps a permanent record. |
| **Simulation** | `--simulate` runs the full server against a wire-level simulator, so you can rehearse a workflow with no hardware attached. |
| **Clear provenance** | `get_connection_info` reports whether data is real or simulated, and simulated servers tell the model to label data as simulated. |

## Recommended practice

1. **Rehearse in simulation** (`--simulate`) before running a new workflow on hardware.
2. **Start read-only.** Grant control only when you need it.
3. **Set limits for your experiment**, not just for the instrument: `--limit max_temperature_c=60` if your solvent boils at 65 °C.
4. **Keep a human in the loop** for hazard tools. Don't auto-approve destructive tools in your MCP client for hazardous instruments.
5. **Log everything** with `--audit-log` when results matter.
6. **Keep MCP servers local.** The default stdio transport is only reachable by the client that launched it. If you use `--transport http`, bind to `127.0.0.1` or put it behind authentication. Never expose instrument control to the internet.
7. **Treat instrument data as untrusted input** to the model. Don't let an agent act on free text read from instruments or files without review.

## Reporting a safety issue

If a server can bypass a limit, mislabel simulated data, send a command different from what a tool promises, or otherwise behave unsafely, report it **privately** (see [SECURITY.md](SECURITY.md)) rather than in a public issue.
