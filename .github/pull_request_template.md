## What does this PR do?

<!-- New server? Fix? Which instrument(s)? -->

## New server checklist

- [ ] No official MCP server exists for this instrument (checked vendor site, GitHub, MCP Registry)
- [ ] Commands verified against a cited manual / SDK (link: )
- [ ] Wire-level simulator reproduces replies **and error replies**
- [ ] Every tool uses a kind (`READ` / `CONTROL` / `HAZARD` / `SAFETY`); every hazard has a stop tool
- [ ] Safety limits on dangerous setpoints
- [ ] `uv run pytest servers/<domain>/<slug>` passes and `--simulate --check` works
- [ ] README complete and `uv run python scripts/build_catalog.py` run
- [ ] Tested on real hardware? If yes: model + firmware:
