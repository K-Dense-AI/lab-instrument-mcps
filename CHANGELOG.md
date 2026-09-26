# Changelog

Notable changes to LabMCP. Each package is versioned on its own and released with a tag such as `labmcp-v0.1.0` or `labmcp-ika-v0.1.0`. Safety-relevant fixes are marked **[safety]**.

## [0.1.0] - 2026-09-26

First public release: the `labmcp` core library and 28 instrument servers, all published to PyPI, with each server also listed in the MCP Registry.

- **Core (`labmcp`):** serial, TCP and VISA transports; a simulator base; safety limits (`--limit`); read-only mode (`--read-only`); hazard annotations; an audit log (`get_command_log`, `--audit-log`); a connection check (`--check`); and the `labmcp` CLI (`list`, `info`, `ports`, `config`).
- **Servers:** 5 biology, 6 chemistry, 5 physics, 3 health, 5 engineering and 4 universal-protocol servers (SCPI, Modbus, EPICS, SiLA 2). See the [catalog](README.md#-supported-instruments).
- Every server has a wire-level simulator (`--simulate`). All are 🧪 simulated; none is hardware-verified yet.
