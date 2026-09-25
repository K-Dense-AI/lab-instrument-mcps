# Connecting LabMCP servers to your AI client

Every LabMCP server is a normal stdio MCP server launched with [`uvx`](https://docs.astral.sh/uv/). Install `uv` once:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

The `labmcp` helper prints the right snippet for your client:

```bash
uvx labmcp config mettler-toledo --address /dev/ttyUSB0 --client claude-desktop
uvx labmcp config mettler-toledo --address COM4 --client vscode
uvx labmcp config mettler-toledo --simulate --client claude-code
```

Use one entry per instrument. You can run as many servers side by side as you have instruments.

## Claude Code

```bash
claude mcp add balance -- uvx labmcp-mettler-toledo --address /dev/ttyUSB0
claude mcp add stirrer -- uvx labmcp-ika --address /dev/ttyACM0 --limit max_temperature_c=80
```

Add `--scope project` to share the configuration with your lab through a checked-in `.mcp.json`.

## Claude Desktop

Settings → Developer → Edit Config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "balance": {
      "command": "uvx",
      "args": ["labmcp-mettler-toledo", "--address", "/dev/ttyUSB0"]
    },
    "stirrer": {
      "command": "uvx",
      "args": ["labmcp-ika", "--address", "/dev/ttyACM0", "--limit", "max_temperature_c=80"]
    }
  }
}
```

Restart Claude Desktop after editing. On macOS, if `uvx` isn't found, use its full path (`which uvx`, usually `~/.local/bin/uvx`).

## Cursor / Windsurf

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global). Windsurf uses `~/.codeium/windsurf/mcp_config.json`. Same format as Claude Desktop (`mcpServers`).

## VS Code (GitHub Copilot agent mode)

`.vscode/mcp.json`:

```json
{
  "servers": {
    "balance": {
      "type": "stdio",
      "command": "uvx",
      "args": ["labmcp-mettler-toledo", "--address", "/dev/ttyUSB0"]
    }
  }
}
```

## OpenAI Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.balance]
command = "uvx"
args = ["labmcp-mettler-toledo", "--address", "/dev/ttyUSB0"]
```

## Environment variables instead of flags

Every flag has an environment-variable equivalent, which some clients find easier to template:

```json
{
  "mcpServers": {
    "balance": {
      "command": "uvx",
      "args": ["labmcp-mettler-toledo"],
      "env": { "LABMCP_ADDRESS": "/dev/ttyUSB0", "LABMCP_READ_ONLY": "1" }
    }
  }
}
```

## Remote / shared instruments (HTTP)

Run the server on the computer that is physically connected to the instrument:

```bash
uvx labmcp-mettler-toledo --address /dev/ttyUSB0 --transport http --host 127.0.0.1 --port 8765
```

Then point an HTTP-capable MCP client at `http://127.0.0.1:8765/mcp`. **Security:** the HTTP transport has no authentication built in. Bind it to `127.0.0.1` and reach it through an SSH tunnel or VPN (`ssh -L 8765:127.0.0.1:8765 lab-pc`). Never expose instrument control to the open internet.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools return "No instrument address configured" | Add `--address …`, or `--simulate` to try it without hardware. |
| "Could not open serial port" | Run `uvx labmcp ports` to find the port, and close other software (vendor apps, serial monitors) that holds it. On Linux, add yourself to the `dialout` group. |
| "Timed out waiting for a reply" | Check baud rate / parity / line endings on the instrument match. Override them in the address: `serial:///dev/ttyUSB0?baudrate=19200&parity=E`. |
| Server doesn't appear in the client | Run the exact command from your config in a terminal with `--check` to see errors. Check that `uvx` is on the client's PATH. |
| Need to see what was sent | Ask the agent to call `get_command_log`, or start the server with `--audit-log traffic.jsonl`. |
