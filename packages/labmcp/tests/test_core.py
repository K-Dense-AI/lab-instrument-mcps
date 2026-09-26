import json
import socket
import threading
import time

import pytest
from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    AuditLog,
    InstrumentServer,
    InstrumentTimeout,
    Limit,
    LineSimulator,
    SafetyLimitError,
    SafetyLimits,
    SimulatedTransport,
    open_transport,
    parse_address,
)
from labmcp.safety import parse_limit_args
from labmcp.scpi import SCPIDriver, SCPISimulator
from labmcp.testing import simulated_client, tool_names

# --------------------------------------------------------------- addresses


@pytest.mark.parametrize(
    "address, kind, target, port",
    [
        ("serial:///dev/ttyUSB0?baudrate=9600", "serial", "/dev/ttyUSB0", None),
        ("serial://COM3", "serial", "COM3", None),
        ("/dev/tty.usbserial-A1", "serial", "/dev/tty.usbserial-A1", None),
        ("COM12", "serial", "COM12", None),
        ("tcp://192.168.1.50:5025", "tcp", "192.168.1.50", 5025),
        ("visa://TCPIP0::10.0.0.2::inst0::INSTR", "visa", "TCPIP0::10.0.0.2::inst0::INSTR", None),
        ("GPIB0::22::INSTR", "visa", "GPIB0::22::INSTR", None),
        ("USB0::0x1AB1::0x04CE::DS1ZA1::INSTR", "visa", "USB0::0x1AB1::0x04CE::DS1ZA1::INSTR", None),
    ],
)
def test_parse_address(address, kind, target, port):
    addr = parse_address(address)
    assert (addr.kind, addr.target, addr.port) == (kind, target, port)


def test_parse_address_params():
    addr = parse_address("serial:///dev/ttyUSB0?baudrate=19200&parity=E")
    assert addr.params == {"baudrate": "19200", "parity": "E"}


@pytest.mark.parametrize("bad", ["", "http://x", "tcp://nohost", "gibberish"])
def test_parse_address_rejects(bad):
    with pytest.raises(ValueError):
        parse_address(bad)


# --------------------------------------------------------------- transports


class Echo(LineSimulator):
    def handle(self, command):
        if command == "SILENT":
            return None
        if command == "TWO":
            return ["one", "two"]
        return command.upper()


def test_simulated_transport_query_and_multiline():
    t = SimulatedTransport(Echo(), read_termination="\r\n", write_termination="\r\n")
    assert t.query("hello") == "HELLO"
    t.write("TWO")
    assert t.read() == "one"
    assert t.read() == "two"


def test_simulated_transport_timeout():
    t = SimulatedTransport(Echo(), timeout=0.05)
    with pytest.raises(InstrumentTimeout):
        t.query("SILENT")


def test_audit_log_records_traffic(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    t = SimulatedTransport(Echo(), audit=audit)
    t.query("ping")
    entries = audit.recent()
    assert [e["direction"] for e in entries] == ["write", "read"]
    assert entries[1]["data"] == "PING"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["data"] == "ping"


def test_audit_log_binary_as_hex():
    audit = AuditLog()
    audit.record("write", b"\x01\x02\xff")
    assert audit.recent()[0]["data"] == "01 02 ff"


def test_tcp_transport_roundtrip():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                buf += conn.recv(64)
            conn.sendall(b"ID,TCP,1,2\n")

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    t = open_transport(f"tcp://127.0.0.1:{port}", timeout=2)
    try:
        assert t.query("*IDN?") == "ID,TCP,1,2"
    finally:
        t.close()
        srv.close()


def test_open_transport_tcp_ignores_serial_defaults():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    t = open_transport(f"tcp://127.0.0.1:{port}", baudrate=9600, xonxoff=True, timeout=1)
    t.close()
    srv.close()


# --------------------------------------------------------------- safety


def test_limits_check_and_override():
    limits = SafetyLimits([Limit("max_temperature_c", 100, "°C", "hotplate setpoint")])
    assert limits.check("max_temperature_c", 50) == 50
    with pytest.raises(SafetyLimitError, match="--limit max_temperature_c"):
        limits.check("max_temperature_c", 150)
    limits.override({"max_temperature_c": 200})
    assert limits.check("max_temperature_c", 150) == 150
    with pytest.raises(ValueError, match="Unknown safety limit"):
        limits.override({"nope": 1})


def test_min_limit():
    limits = SafetyLimits([Limit("min_temperature_c", -20, "°C", kind="min")])
    with pytest.raises(SafetyLimitError, match="below the minimum"):
        limits.check("min_temperature_c", -40)


def test_parse_limit_args():
    assert parse_limit_args(["a=1,b=2.5", "c=3"]) == {"a": 1.0, "b": 2.5, "c": 3.0}


# --------------------------------------------------------------- scpi


class FakeDMM(SCPISimulator):
    idn = "ACME,DMM1000,SN42,1.2.3"

    def command(self, key, arg):
        if self.matches(key, "MEASure:VOLTage[:DC]?"):
            return "+1.234500E+00"
        raise self.undefined()


def test_scpi_driver():
    d = SCPIDriver(SimulatedTransport(FakeDMM()))
    assert d.identify() == {"manufacturer": "ACME", "model": "DMM1000", "serial": "SN42", "firmware": "1.2.3"}
    assert d.query_float("MEAS:VOLT:DC?") == pytest.approx(1.2345)
    assert d.query_float("measure:voltage?") == pytest.approx(1.2345)
    assert d.errors() == []
    d.write("BOGUS 1")
    assert d.errors() == ['-113,"Undefined header"']


# --------------------------------------------------------------- server


class Thing:
    def __init__(self, transport):
        self.t = transport
        self.closed = False

    def identify(self):
        return {"model": self.t.query("id")}

    def close(self):
        self.closed = True


def make_server():
    def connect(ctx):
        return Thing(ctx.open_transport(simulator=Echo))

    s = InstrumentServer("Test Thing", connect=connect, limits=[Limit("max_x", 10, "V")])
    mcp = s.mcp

    @mcp.tool(**READ)
    def look() -> str:
        return "looked"

    @mcp.tool(**CONTROL)
    def adjust() -> str:
        return "adjusted"

    @mcp.tool(**HAZARD)
    def energise(volts: float) -> str:
        s.check("max_x", volts, "voltage")
        return "zap"

    @mcp.tool(**SAFETY)
    def stop() -> str:
        return "stopped"

    return s


async def test_builtin_tools_and_kinds():
    s = make_server()
    async with simulated_client(s) as client:
        names = await tool_names(client)
        assert {"get_connection_info", "get_command_log", "reconnect", "look", "adjust", "energise", "stop"} <= names
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["look"].annotations.read_only_hint is True
        assert tools["energise"].annotations.destructive_hint is True
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["instrument"] == {"model": "ID"}
        assert info["safety_limits"]["max_x"]["value"] == 10


async def test_read_only_mode_toggles():
    s = make_server()
    async with simulated_client(s, read_only=True) as client:
        names = await tool_names(client)
        assert "look" in names and "stop" in names and "reconnect" in names
        assert "adjust" not in names and "energise" not in names
    async with simulated_client(s, read_only=False) as client:
        assert "energise" in await tool_names(client)


async def test_limit_enforced_through_tool():
    s = make_server()
    async with simulated_client(s, limits={"max_x": 5}) as client:
        assert (await client.call_tool("energise", {"volts": 4})).data == "zap"
        with pytest.raises(Exception, match="max_x"):
            await client.call_tool("energise", {"volts": 6})


async def test_missing_address_gives_helpful_error():
    s = make_server()
    s.configure(simulate=False)
    s.settings.address = None
    info = s._connection_info()
    assert info["connected"] is False
    assert "--simulate" in info["error"]


def test_instructions_mention_simulation():
    s = make_server()
    s.configure(simulate=True)
    assert "SIMULATION MODE" in s.mcp.instructions
    s.configure(simulate=False)
    assert "SIMULATION MODE" not in s.mcp.instructions


def test_env_configuration():
    s = make_server()
    s.configure_from_env(
        {
            "LABMCP_SIMULATE": "1",
            "LABMCP_LIMITS": "max_x=3",
            "LABMCP_OPTIONS": "channel=2",
        }
    )
    assert s.settings.simulate
    assert s.limits["max_x"] == 3
    assert s.settings.options == {"channel": "2"}


def test_disconnect_closes_driver():
    s = make_server()
    s.configure(simulate=True)
    driver = s.driver
    s.disconnect()
    assert driver.closed
    assert not s.connected


def test_env_options_json_allows_commas():
    s = make_server()
    s.configure_from_env({"LABMCP_OPTIONS": '{"denylist": "^(OUTP|SOUR),x", "n": 2}'})
    assert s.settings.options == {"denylist": "^(OUTP|SOUR),x", "n": "2"}
    with pytest.raises(ValueError, match="JSON"):
        s.configure_from_env({"LABMCP_OPTIONS": "{not json"})


class Streamer(LineSimulator):
    def __init__(self):
        self.pending = []

    def handle(self, command):
        if command == "START":
            self.pending = ["d1", "d2", "END"]
            return "OK"
        return None

    def poll(self):
        due, self.pending = self.pending[:1], self.pending[1:]
        return due


def test_simulator_poll_hook_streams_lines():
    t = SimulatedTransport(Streamer(), timeout=0.5)
    assert t.query("START") == "OK"
    assert [t.read(), t.read(), t.read()] == ["d1", "d2", "END"]


async def test_connect_on_start_connects_in_lifespan():
    def connect(ctx):
        return Thing(ctx.open_transport(simulator=Echo))

    s = InstrumentServer("Pushy", connect=connect, connect_on_start=True)
    s.configure(simulate=True)
    from fastmcp import Client

    async with Client(s.mcp):
        assert s.connected
    assert not s.connected


def test_check_exit_code_reflects_identify_errors(capsys):
    class Broken:
        def identify(self):
            raise RuntimeError("no reply")

    s = InstrumentServer("Broken", connect=lambda ctx: Broken())
    s.configure(simulate=True)
    assert s._run_check() == 1
    assert "no reply" in capsys.readouterr().out


def test_audit_log_truncates_bulk_data():
    audit = AuditLog()
    audit.record("read", b"#9000100000" + bytes(100_000))
    entry = audit.recent()[0]["data"]
    assert entry.endswith("[truncated, 100011 bytes total]")
    assert len(entry) < 1000


@pytest.mark.parametrize(
    "key, pattern, expected",
    [
        ("SENS:CURR:NPLC", "[SENSe[1]:]CURRent:NPLCycles", True),
        ("SENS1:CURR:NPLC", "[SENSe[1]:]CURRent:NPLCycles", True),
        ("CURR:NPLC", "[SENSe[1]:]CURRent:NPLCycles", True),
        ("ARM:SEQ1:COUN", "ARM[:SEQuence[1]]:COUNt", True),
        ("ARM:COUN", "ARM[:SEQuence[1]]:COUNt", True),
        ("ARM:SEQ:LAY", "ARM[:SEQuence[1]]:COUNt", False),
        ("SOUR:VOLT:LEV?", "[SOURce:]VOLTage[:LEVel]?", True),
    ],
)
def test_scpi_nested_optional_nodes(key, pattern, expected):
    assert SCPISimulator.matches(key, pattern) is expected


# --------------------------------------------------------------- regressions (protocol review)


@pytest.mark.parametrize(
    "key, expected",
    [("VOLT", True), ("VOLTAGE", True), ("voltage", True), ("VOLTA", False), ("VOLTAG", False), ("VOL", False)],
)
def test_scpi_matches_only_short_or_long_form(key, expected):
    # SCPI-99 Vol. 1, 6.2.1: a truncated long form (VOLTA) is an undefined header on real instruments.
    assert SCPISimulator.matches(key, "VOLTage") is expected


def test_limit_check_refuses_nan():
    limits = SafetyLimits([Limit("max_temperature_c", 100, "°C"), Limit("min_t", -20, "°C", kind="min")])
    with pytest.raises(SafetyLimitError, match="NaN"):
        limits.check("max_temperature_c", float("nan"))
    with pytest.raises(SafetyLimitError, match="NaN"):
        limits.check("min_t", float("nan"))
    with pytest.raises(ValueError, match="NaN"):
        limits.override({"max_temperature_c": float("nan")})
    with pytest.raises(ValueError, match="NaN"):
        limits.override(parse_limit_args(["max_temperature_c=nan"]))
    assert limits["max_temperature_c"] == 100


@pytest.mark.parametrize(
    "address, kind, target, params",
    [
        ("/dev/ttyUSB0?baudrate=19200&parity=E", "serial", "/dev/ttyUSB0", {"baudrate": "19200", "parity": "E"}),
        ("COM3?baudrate=9600", "serial", "COM3", {"baudrate": "9600"}),
        ("GPIB0::22::INSTR?backend=@ivi", "visa", "GPIB0::22::INSTR", {"backend": "@ivi"}),
    ],
)
def test_parse_address_shorthand_with_query(address, kind, target, params):
    addr = parse_address(address)
    assert (addr.kind, addr.target, addr.params) == (kind, target, params)


class BlockSim(SCPISimulator):
    def command(self, key, arg):
        if key == "CURV?":
            return "#15ABCDE"
        raise self.undefined()


def test_query_block_consumes_late_terminator():
    """The NL after a block may arrive later than 50 ms; it must not be left for the next query."""
    t = SimulatedTransport(BlockSim(), timeout=2.0)
    d = SCPIDriver(t)
    with t.lock:
        t.write_bytes(b"CURV?\n")
        t.flush_input()  # drop the simulator's immediate reply; replay it with a delayed terminator
        t.push(b"#15ABCDE")
        threading.Timer(0.2, t.push, args=(b"\n",)).start()
        t.write = lambda command: None  # the request was already "sent"
        assert d.query_block("CURV?") == b"ABCDE"
        del t.write
    time.sleep(0.3)  # the late terminator has arrived by now
    assert d.query("*IDN?") == SCPISimulator.idn


def test_query_block_rejects_malformed_header():
    class BadSim(SCPISimulator):
        def command(self, key, arg):
            return "#XYZ"

    with pytest.raises(Exception, match="Malformed binary block header"):
        SCPIDriver(SimulatedTransport(BadSim())).query_block("CURV?")
