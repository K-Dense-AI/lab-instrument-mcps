#!/usr/bin/env bash
# Add a fully simulated wet lab to Claude Code (no hardware needed).
# Remove them again with: claude mcp remove <name>
set -euo pipefail
claude mcp add balance      -- uvx labmcp-mettler-toledo --simulate
claude mcp add stirrer      -- uvx labmcp-ika --simulate --limit max_temperature_c=80
claude mcp add ph-probe     -- uvx labmcp-atlas-ezo --simulate
claude mcp add syringe-pump -- uvx labmcp-new-era --simulate
claude mcp add potentiostat -- uvx labmcp-palmsens --simulate
claude mcp add spectrometer -- uvx labmcp-ocean-spectrometer --simulate
echo "Virtual lab added. Try: 'Which instruments are connected? Are any of them simulated?'"
