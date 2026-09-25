# Examples

## Virtual lab (no hardware)

Run a simulated wet lab of six instruments (balance, hotplate stirrer, pH probe, syringe pump, potentiostat, spectrometer) and let your agent coordinate them.

- **Claude Desktop / Cursor / Windsurf:** merge [`virtual-lab.claude_desktop_config.json`](virtual-lab.claude_desktop_config.json) into your MCP config.
- **Claude Code:** run [`./virtual-lab-claude-code.sh`](virtual-lab-claude-code.sh).

Prompts to try:

- *"Which instruments are connected? Confirm they're all simulated."*
- *"Tare the balance, weigh out about 2 g, then heat the stirrer to 45 °C at 300 rpm and wait until it's at temperature."*
- *"Calibrate the pH probe at 7.00, then log pH every 5 s for a minute and report the drift."*
- *"Run cyclic voltammetry at 50, 100 and 200 mV/s and check whether the peak current scales with √(scan rate)."*
- *"Take a dark reference and a blank, then measure the absorbance spectrum and report the peak wavelength."*

Swap `--simulate` for `--address …` to point any entry at the real instrument.

## Register maps (Modbus)

[`servers/protocols/modbus/src/labmcp_modbus/examples/`](../servers/protocols/modbus/src/labmcp_modbus/examples/) has an example map for a generic PID temperature controller.
