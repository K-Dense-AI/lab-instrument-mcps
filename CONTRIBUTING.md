# Contributing to LabMCP

Thanks for helping connect lab instruments to AI agents! There are three ways to help, and all of them matter.

## 1. Test a server on real hardware ⭐ most needed

New servers start out **🧪 simulated**: they are built from the vendor's programming manual and tested against a wire-level simulator. What moves a server to **✅ hardware-verified** is someone with the instrument running it:

```bash
uvx labmcp-<server> --address <port-or-ip> --check
```

Then try a few tools from your MCP client and file a [hardware verification report](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=hardware-verification.yml). Include failures too: a report of "`set_draft_shield` returns `WS L` on an XS205" is exactly what we need.

## 2. Request an instrument

Open an [instrument request](https://github.com/K-Dense-AI/lab-instrument-mcps/issues/new?template=instrument-request.yml). A link to the communication manual or SDK makes a request buildable.

We only build servers for instruments that **don't already have an official MCP server** (see [docs/official-servers.md](docs/official-servers.md)).

## 3. Write or improve a server

```bash
git clone https://github.com/K-Dense-AI/lab-instrument-mcps && cd lab-instrument-mcps
uv sync --all-packages          # installs every server in editable mode
uv run pytest                   # full test suite (all simulated, no hardware needed)
```

Then follow **[docs/writing-a-server.md](docs/writing-a-server.md)**. In short:

1. `uv run python scripts/new_server.py --domain <domain> --slug <slug> --package labmcp-<name> --name "<Name>" --vendor "<Vendor>"`
2. Implement `driver.py` (protocol), `simulator.py` (wire-level simulator), and `server.py` (tools).
3. Tag every tool `READ` / `CONTROL` / `HAZARD` / `SAFETY`, and add safety limits for dangerous setpoints.
4. Write tests, then run `uv run python scripts/build_catalog.py` to regenerate the catalog and README tables.
5. Open a PR using the checklist.

### Ground rules

- **Accuracy over breadth.** Implement only commands you verified in a manual or SDK, and cite it. An instrument that receives a wrong command can be damaged or can damage a sample.
- **Safety is not optional.** Read [SAFETY.md](SAFETY.md). PRs that weaken limits, read-only mode, or tool kinds need a maintainer's safety review.
- **No vendor lock-in.** Prefer open protocols and permissively licensed libraries. If an SDK is proprietary but free (e.g. a vendor runtime), import it lazily and document the install step.
- **Style:** `uv run ruff check .` must pass. Match the surrounding code.

### Releasing (maintainers)

Bump the version in the package's `pyproject.toml`, then tag it `<package>-v<version>` (e.g. `labmcp-mettler-toledo-v0.2.0`). The release workflow tests, builds, and publishes the package to PyPI and the MCP Registry.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind. We're all here to do better science.
