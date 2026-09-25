# Instruments with official MCP servers

LabMCP only builds servers for instruments that **don't** have an official MCP server from their vendor (or from the project or standards body that owns the interface). If your instrument is listed here, use the official server.

*Last reviewed: 2026-09-25. Vendors ship new servers often. If you know of one we've missed, [open an issue](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new) or a PR.*

## Instrument control

| Vendor | Coverage | Official server |
|---|---|---|
| **Keysight** | Oscilloscopes, power supplies, SMUs (B2900/B1500), VNAs, signal generators/analyzers | [Keysight MCP Server for Instrument Control](https://www.keysight.com/us/en/lib/resources/software-releases/keysight-mcp-server-for-instrument-control.html) ([docs](https://helpfiles.keysight.com/kmsic/English/keysight_mcp_for_instrument_control/Content/overview.html)) |
| **Rohde & Schwarz** | R&S instruments through `RsInstrument` | Built into [RsInstrument](https://github.com/Rohde-Schwarz/RsInstrument) (`python -m RsInstrument.mcp`) |
| **Saleae** | Logic 2 logic analyzers | [Logic 2 MCP server](https://docs.saleae.com/mcp/) (experimental) |
| **DAQiFi** | Nyquist DAQ devices | `io.github.daqifi/daqifi-mcp` in the MCP Registry ([daqifi-core](https://github.com/daqifi/daqifi-core)) |

## Related official servers (software, facilities)

These aren't instrument drivers, but they pair well with LabMCP servers:

| Owner | What | Link |
|---|---|---|
| Benchling | ELN / LIMS | [Benchling MCP](https://help.benchling.com/hc/en-us/articles/40342713479437-Benchling-MCP-Server) |
| 10x Genomics | Cloud analysis | [txg-mcp](https://github.com/10XGenomics/txg-mcp) |
| Argonne APS (BCDA) | Bluesky queueserver at APS | [bait_mcp](https://github.com/BCDA-APS/bait_mcp) |
| LBNL ALS | Control-system agent framework | [osprey](https://github.com/als-apg/osprey) |
| SimpleBLE | Generic Bluetooth LE GATT access (library-official) | [simpleaible](https://github.com/simpleble/simpleble/tree/main/simpleaible) |

## Watch list

- **Anthropic Model Hardware Standard** (research preview, Aug 2026). Tecan, QIAGEN, MBF Bioscience and others are listed as partners. If they ship official agent interfaces for their instruments, we'll list them here and won't duplicate them.
- **NI LabVIEW 2026 Q3** adds an assistant that *consumes* MCP servers. It doesn't provide one for NI-DAQmx hardware, so `labmcp-ni-daqmx` fills that gap.
