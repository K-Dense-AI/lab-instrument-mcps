import importlib.util
import io
import json
import math
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    AuditLog,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    InstrumentTimeout,
    Limit,
    LineSimulator,
    SafetyLimitError,
    SafetyLimits,
    SimulatedTransport,
    Transport,
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
    # null = unset (it used to become the string "None", e.g. a store_path named "None")
    s.configure_from_env({"LABMCP_OPTIONS": '{"store_path": null, "legacy": true, "gain": 2.5}'})
    assert s.settings.options == {"legacy": "true", "gain": "2.5"}


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


def test_prepare_save_path_refuses_wrong_suffix_existing_file_and_folder(tmp_path):
    from labmcp import InstrumentError, prepare_save_path

    target = prepare_save_path(tmp_path / "sub" / "data.CSV")
    assert target.parent.is_dir() and target.is_absolute()
    with pytest.raises(InstrumentError, match="must end in"):
        prepare_save_path(tmp_path / "notes.txt")
    with pytest.raises(InstrumentError, match="must end in"):
        prepare_save_path(tmp_path / ".bashrc")
    target.write_text("x")
    with pytest.raises(InstrumentError, match="already exists"):
        prepare_save_path(target)
    assert prepare_save_path(target, overwrite=True) == target
    (tmp_path / "dir.csv").mkdir()
    with pytest.raises(InstrumentError, match="folder"):
        prepare_save_path(tmp_path / "dir.csv", overwrite=True)
    assert prepare_save_path(tmp_path / "img.tiff", suffixes=(".tif", ".tiff")).suffix == ".tiff"


# --------------------------------------------------------------- regressions (software review)

ROOT = Path(__file__).resolve().parents[3]


def test_driver_property_never_returns_none_when_reconnect_races():
    """A `reconnect` landing between the lock release and the return used to hand out None."""
    s = make_server()
    s.configure(simulate=True)

    class RacingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()

        def __exit__(self, *exc):
            self._lock.release()
            s._driver = None  # another thread's disconnect() wins the race right here

    s._lock = RacingLock()
    assert isinstance(s.driver, Thing)


def test_failed_connect_closes_the_transports_it_opened():
    opened = []

    def connect(ctx):
        t = ctx.open_transport(simulator=Echo)
        opened.append(t)
        t.query("SILENT", timeout=0.01)  # the "handshake" times out

    s = InstrumentServer("Leaky", connect=connect)
    s.configure(simulate=True)
    with pytest.raises(InstrumentTimeout):
        _ = s.driver
    assert opened[0].closed and not s.connected
    with pytest.raises(InstrumentConnectionError, match="Could not connect"):
        s._connect = lambda ctx: (opened.append(ctx.open_transport(simulator=Echo)), 1 / 0)
        _ = s.driver
    assert opened[1].closed


def test_close_interrupts_a_blocked_read_with_a_connection_error():
    t = SimulatedTransport(Echo(), timeout=10)
    errors = []

    def reader():
        try:
            t.read()
        except Exception as exc:
            errors.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    t.close()
    th.join(5)
    assert time.monotonic() - t0 < 2
    assert isinstance(errors[0], InstrumentConnectionError) and "closed while in use" in str(errors[0])


def test_tcp_close_under_a_blocked_read_maps_to_connection_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    t = open_transport(f"tcp://127.0.0.1:{srv.getsockname()[1]}", timeout=10)
    conn, _ = srv.accept()
    errors = []

    def reader():
        try:
            t.read()
        except Exception as exc:
            errors.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    t.close()  # used to leak ValueError (select on a closed socket) or wait the full timeout
    th.join(5)
    conn.close()
    srv.close()
    assert time.monotonic() - t0 < 2
    assert isinstance(errors[0], InstrumentConnectionError)


class FlakyTransport(Transport):
    description = "flaky://"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.flush_error = None

    def _write(self, data):
        pass

    def _read(self, max_bytes, timeout):
        return b""

    def _flush_input(self):
        if self.flush_error:
            raise self.flush_error

    def _close(self):
        raise RuntimeError("backend exploded while closing")


def test_flush_input_maps_os_errors_and_close_never_raises():
    t = FlakyTransport()
    t.flush_error = ConnectionResetError("peer reset")
    with pytest.raises(InstrumentConnectionError, match="peer reset"):
        t.flush_input()
    t.flush_error = ValueError("a genuine bug")
    with pytest.raises(ValueError):  # non-I/O errors on an open transport still propagate
        t.flush_input()
    t.close()  # _close raising a non-OSError used to escape
    t.close()
    t.flush_input()  # a no-op once closed
    with pytest.raises(InstrumentConnectionError, match="closed"):
        t.write("x")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_non_positive_or_non_finite_timeouts_are_refused(bad):
    with pytest.raises(ValueError, match="positive"):
        SimulatedTransport(Echo(), timeout=bad)
    with pytest.raises(ValueError, match="positive"):
        make_server().configure(timeout=bad)


def test_cli_errors_become_usage_errors(tmp_path, capsys):
    s = make_server()
    for argv in (
        ["--simulate", "--timeout", "nan", "--check"],
        ["--simulate", "--option", "novalue", "--check"],
        ["--simulate", "--limit", "max_x=80V", "--check"],
        ["--simulate", "--limit", "max_x=inf", "--check"],
        ["--simulate", "--audit-log", str(tmp_path / "file.txt" / "audit.jsonl"), "--check"],
    ):
        if "file.txt" in argv[-2]:
            (tmp_path / "file.txt").write_text("not a folder")
        with pytest.raises(SystemExit) as exc:
            s.run(argv)
        assert exc.value.code == 2, argv
    assert "plain number" in capsys.readouterr().err


def test_tcp_write_is_bounded_and_mapped(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    t = open_transport(f"tcp://127.0.0.1:{srv.getsockname()[1]}", timeout=1)
    timeouts = []

    class StuckSocket:
        def settimeout(self, value):
            timeouts.append(value)

        def sendall(self, data):
            raise TimeoutError("timed out")

        def setblocking(self, flag):
            pass

    real, t._sock = t._sock, StuckSocket()
    with pytest.raises(InstrumentConnectionError, match="timed out"):
        t.write("*RST")
    assert timeouts and all(v is not None and math.isfinite(v) for v in timeouts)
    t._sock = real
    t.close()
    srv.close()


def test_serial_transport_does_not_reconfigure_the_port_on_every_read(monkeypatch):
    import serial

    class FakeSerial:
        def __init__(self, **kw):
            self._timeout = kw["timeout"]
            self.sets = 0
            self.rx = bytearray(b"hello\r\nworld\r\n")

        @property
        def timeout(self):
            return self._timeout

        @timeout.setter
        def timeout(self, value):  # pyserial reconfigures the port here
            self.sets += 1
            self._timeout = value

        def read(self, n):
            out = bytes(self.rx[:1 if n == 1 else n])
            del self.rx[: len(out)]
            return out

        @property
        def in_waiting(self):
            return min(len(self.rx), 2)  # data trickles in

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    t = open_transport("serial:///dev/ttyFAKE?baudrate=9600&parity=even", timeout=2, read_termination="\r\n")
    assert t.read() == "hello" and t.read() == "world"
    assert t._port.sets == 1
    with pytest.raises(InstrumentConnectionError, match="parity"):
        open_transport("serial:///dev/ttyFAKE?parity=X")


class _LineServer:
    """A tiny TCP instrument for VISA SOCKET tests: replies ``reply`` to every line."""

    def __init__(self, reply=b"ID\n", delay=0.0):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.reply, self.delay = reply, delay
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        with conn:
            buf = b""
            while True:
                data = conn.recv(64)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    _, _, buf = buf.partition(b"\n")
                    time.sleep(self.delay)
                    conn.sendall(self.reply)

    def close(self):
        self.srv.close()


def test_visa_read_until_one_deadline_one_audit_entry_and_write_timeout():
    pytest.importorskip("pyvisa_py")
    lab = _LineServer(b"ABCDEFGHIJ;\n")
    audit = AuditLog()
    t = open_transport(f"visa://TCPIP0::127.0.0.1::{lab.port}::SOCKET", timeout=2, audit=audit)
    try:
        t.write("Q?")
        assert t.read_until(b";") == b"ABCDEFGHIJ"
        assert [e["direction"] for e in audit.recent()] == ["write", "read"]  # not one entry per byte
        assert t.read(timeout=0.05) == ""  # the trailing newline; leaves a 50 ms VISA timeout behind
        t.write("Q?")
        assert t._res.timeout == 2000  # writes use the configured timeout, not the last read's
        with pytest.raises(InstrumentTimeout):
            t.read_until(b"#", timeout=0.3)
        t._res.close()  # the session disappears under the transport (e.g. a concurrent reconnect)
        with pytest.raises(InstrumentConnectionError):
            t.read()
    finally:
        t.close()
        lab.close()


def test_audit_log_file_errors_do_not_break_instrument_io(tmp_path):
    path = tmp_path / "audit.jsonl"
    s = make_server()
    s.configure(simulate=True, audit_log=str(path))
    path.mkdir()  # the log file can no longer be opened for appending
    assert s.driver.identify() == {"model": "ID"}  # used to raise IsADirectoryError mid-exchange
    assert s.audit.recent()[-1]["data"] == "ID"
    assert "audit_log_error" in s._connection_info()
    path.rmdir()
    s.driver.t.query("again")
    assert s.audit.write_error is None and "again" in path.read_text(encoding="utf-8")


def test_limit_definitions_and_values_must_be_sane():
    with pytest.raises(ValueError, match="kind"):
        Limit("max_t", 100, kind="Max")  # used to silently act as a *minimum*
    with pytest.raises(ValueError, match="finite"):
        Limit("max_t", float("nan"))
    limits = SafetyLimits([Limit("max_v", 10, "V"), Limit("min_t", -20, "°C", kind="min")])
    with pytest.raises(ValueError, match="finite"):
        limits.override({"max_v": float("inf")})
    with pytest.raises(SafetyLimitError, match="not a finite"):
        limits.check("max_v", float("-inf"))  # -inf < 10 used to pass
    with pytest.raises(SafetyLimitError, match="not a finite"):
        limits.check("min_t", float("inf"))
    with pytest.raises(ValueError, match="plain number"):
        parse_limit_args(["max_v=5V"])


def test_query_block_rejects_non_numeric_length():
    class BadLen(SCPISimulator):
        def command(self, key, arg):
            return "#2X1ab"

    with pytest.raises(InstrumentProtocolError, match="Malformed"):
        SCPIDriver(SimulatedTransport(BadLen())).query_block("CURV?")


def test_prepare_save_path_edge_cases(tmp_path, monkeypatch):
    from labmcp import prepare_save_path
    from labmcp.files import _check_windows_name

    assert prepare_save_path(tmp_path / "a.csv", suffixes=".csv").name == "a.csv"  # a bare string
    assert prepare_save_path(tmp_path / "b.csv", suffixes=("CSV",)).name == "b.csv"  # no dot
    assert prepare_save_path(tmp_path / "c.csv.gz", suffixes=(".csv.gz",)).name == "c.csv.gz"
    with pytest.raises(InstrumentError, match="must end in"):
        prepare_save_path(tmp_path / ".csv")
    with pytest.raises(InstrumentError, match="empty"):
        prepare_save_path("")
    for name in ("COM3.csv", "nul.csv", "con.data.csv", "lpt1 .csv", "x.csv:stream"):
        with pytest.raises(InstrumentError, match="reserved"):
            _check_windows_name(name)
    _check_windows_name("computer.csv")
    monkeypatch.chdir(tmp_path)
    assert prepare_save_path("rel.csv") == tmp_path.resolve() / "rel.csv"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra privileges on Windows")
def test_prepare_save_path_checks_the_symlink_target(tmp_path):
    from labmcp import prepare_save_path

    rc = tmp_path / ".bashrc"
    rc.write_text("export PATH=...")
    (tmp_path / "data.csv").symlink_to(rc)
    with pytest.raises(InstrumentError, match="must end in"):
        prepare_save_path(tmp_path / "data.csv", overwrite=True)  # used to return ~/.bashrc
    (tmp_path / "dangling.csv").symlink_to(tmp_path / "new_profile")
    with pytest.raises(InstrumentError, match="must end in"):
        prepare_save_path(tmp_path / "dangling.csv")
    (tmp_path / "ok.csv").symlink_to(tmp_path / "real.csv")
    assert prepare_save_path(tmp_path / "ok.csv") == (tmp_path / "real.csv").resolve()


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="posix, non-root")
def test_prepare_save_path_refuses_unwritable_folder_with_a_hint(tmp_path, monkeypatch):
    from labmcp import prepare_save_path

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.chdir(ro)
    try:
        with pytest.raises(InstrumentError, match="working directory"):
            prepare_save_path("spectrum.csv")
    finally:
        ro.chmod(0o700)


def test_cli_survives_a_legacy_code_page_and_quotes_codex_keys(monkeypatch):
    from labmcp import cli

    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli.main(["list"])  # catalog text contains θ and ≈, not in cp1252
    cli.main(["config", "srs-lockin", "--client", "codex", "--key", "lock in.1"])
    out.flush()
    text = raw.getvalue().decode("cp1252")
    assert "labmcp-srs-lockin" in text and '[mcp_servers."lock in.1"]' in text


def _load_script(name):
    spec = importlib.util.spec_from_file_location(f"_labmcp_script_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_catalog_escapes_table_cells_and_checks_markers():
    bc = _load_script("build_catalog")
    table = bc.tools_table({"tools": [{"name": "t", "kind": "read", "description": "a | b"}]})
    assert "a \\| b" in table
    with pytest.raises(SystemExit, match="TOOLS:END"):
        bc.replace_block("<!-- TOOLS:START -->\n", "<!-- TOOLS:START -->", "<!-- TOOLS:END -->", "x")
    with pytest.raises(SystemExit):
        bc.replace_block("<!-- B -->\n<!-- A -->", "<!-- A -->", "<!-- B -->", "x")


def test_new_server_scaffold_passes_the_basic_checklist(tmp_path, monkeypatch):
    import asyncio

    ns = _load_script("new_server")
    bc = _load_script("build_catalog")
    monkeypatch.setattr(ns, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["new_server.py", "--domain", "physics", "--slug", "printer", "--package", "labmcp-3d-printer",
         "--name", "3D Printer µ", "--vendor", "ACME"],
    )
    ns.main()
    dest = tmp_path / "servers" / "physics" / "printer"
    assert "🧪" in (dest / "README.md").read_text(encoding="utf-8")
    monkeypatch.syspath_prepend(str(dest / "src"))
    server_mod = importlib.import_module("labmcp_3d_printer.server")  # "3dPrinterDriver" was a SyntaxError
    try:
        tools = bc.list_tools("labmcp_3d_printer.server", "printer")  # every tool has a kind
        assert {t["name"] for t in tools} >= {"get_identity", "get_connection_info"}

        async def connection_info():
            async with simulated_client(server_mod.server) as client:
                return (await client.call_tool("get_connection_info", {})).data

        assert asyncio.run(connection_info())["connected"] is True
    finally:
        for mod in [m for m in sys.modules if m.startswith("labmcp_3d_printer")]:
            del sys.modules[mod]
    monkeypatch.setattr(sys, "argv", [*sys.argv[:-4], "--name", 'Bad "Name"', "--vendor", "ACME"])
    with pytest.raises(SystemExit, match="quotes"):
        ns.main()


def test_registry_description_fits_the_registry_limit_without_cutting_words(capsys):
    bc = _load_script("build_catalog")
    long_desc = (
        "MCP server for JULABO heating and refrigerated circulators (CORIO, MAGIO, DYNEO): read "
        "temperatures, set setpoints, start and stop."
    )
    assert bc.registry_description(long_desc, {}, "x") == (
        "MCP server for JULABO heating and refrigerated circulators (CORIO, MAGIO, DYNEO)."
    )
    short = "MCP server for balances (MT-SICS): weigh, tare."
    assert bc.registry_description(short, {}, "x") == short
    no_colon = "MCP server for things (" + "a, " * 40 + "b) with more words after it"
    out = bc.registry_description(no_colon, {}, "x")
    assert len(out) <= 100 and out.endswith("…") and out.count("(") == out.count(")")
    assert "registry_description" in capsys.readouterr().err
    assert bc.registry_description(long_desc, {"registry_description": "Short."}, "x") == "Short."
    with pytest.raises(SystemExit, match="1-100 characters"):
        bc.registry_description(long_desc, {"registry_description": "x" * 101}, "x")
