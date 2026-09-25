# labmcp

Shared foundations for **[LabMCP](https://github.com/K-Dense-AI/lab-instrument-mcps)**, an open-source collection of Model Context Protocol servers that let AI agents operate laboratory instruments. It is built on [FastMCP](https://gofastmcp.com).

Install `labmcp` directly if you want to **build** an instrument server or use the `labmcp` helper CLI. To **use** an instrument, install its server instead, e.g. `uvx labmcp-mettler-toledo`.

## The `labmcp` CLI

```bash
uvx labmcp list                          # all instrument servers, by domain
uvx labmcp info mettler-toledo           # models, interfaces, tools
uvx labmcp ports                         # serial ports (and VISA resources with labmcp[visa])
uvx labmcp config ika --address COM3     # MCP client config snippet
```

## Building a server

```python
from labmcp import HAZARD, READ, SAFETY, ConnectContext, InstrumentServer, Limit

def connect(ctx: ConnectContext) -> MyDriver:
    return MyDriver(ctx.open_transport(simulator=MySimulator, baudrate=9600,
                                       read_termination="\r\n", write_termination="\r\n"))

server = InstrumentServer("My Hotplate", connect=connect, package="labmcp-my-hotplate",
                          limits=[Limit("max_temperature_c", 150, "°C", "Hotplate setpoint")])
mcp = server.mcp

@mcp.tool(**READ)
def read_temperature() -> float:
    return server.driver.temperature_c()

@mcp.tool(**HAZARD)
def set_temperature(temperature_c: float) -> str:
    server.check("max_temperature_c", temperature_c, "setpoint")
    server.driver.set_temperature(temperature_c)
    return "ok"

@mcp.tool(**SAFETY)
def heater_off() -> str:
    server.driver.heater_off()
    return "heater off"

def main() -> None:
    server.run()   # --address, --simulate, --read-only, --limit, --check, --audit-log, ...
```

What you get:

- **Transports:** serial (pyserial), raw TCP, VISA (`labmcp[visa]`: GPIB/USBTMC/LXI/HiSLIP), and a wire-level `SimulatedTransport`. All are thread-safe and share the same line handling and timeouts.
- **Addresses as URIs:** `serial:///dev/ttyUSB0?baudrate=9600`, `tcp://10.0.0.5:5025`, `visa://GPIB0::22::INSTR`.
- **Safety:** tool kinds (`READ`/`CONTROL`/`HAZARD`/`SAFETY`) with MCP annotations, `--read-only` mode, safety `Limit`s checked before sending, and a command audit log.
- **Built-in tools:** `get_connection_info`, `get_command_log`, `reconnect`.
- **SCPI helpers:** `labmcp.scpi.SCPIDriver` and `SCPISimulator`, with short/long-form matching and error-queue handling.
- **Testing:** `labmcp.testing.simulated_client(server)` gives you an in-memory FastMCP client.

See the [contributor guide](https://github.com/K-Dense-AI/lab-instrument-mcps/blob/main/docs/writing-a-server.md).

License: Apache-2.0.
