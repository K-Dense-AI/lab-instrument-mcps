import asyncio
import socket
import threading
import time

import pytest
from labmcp import InstrumentConnectionError, InstrumentProtocolError, SafetyLimitError
from labmcp.audit import AuditLog
from labmcp.testing import simulated_client, tool_names
from labmcp_modbus.driver import ModbusDevice, ModbusExceptionResponse, PymodbusClient, _with_default_port
from labmcp_modbus.registers import example_map_path, load_register_map
from labmcp_modbus.server import server
from labmcp_modbus.simulator import SimulatedPIDController


@pytest.fixture(autouse=True)
def _reset_server():
    # simulated_client() keeps options from earlier calls unless told otherwise
    server.configure(simulate=True, read_only=False, options={}, limits={})
    yield
    server.disconnect()


def make_device(register_map="example", **kwargs) -> tuple[ModbusDevice, SimulatedPIDController, AuditLog]:
    m = load_register_map(example_map_path()) if register_map == "example" else register_map
    sim = SimulatedPIDController(extra_map=m)
    audit = AuditLog()
    return ModbusDevice(sim, register_map=m, audit=audit, **kwargs), sim, audit


def _sent(audit: AuditLog) -> list[str]:
    return [e["data"] for e in audit.recent(500) if e["direction"] == "write"]


# ------------------------------------------------------------------ driver + simulator


def test_read_points_decoded_with_units():
    dev, _, _ = make_device()
    values = {r.name: r for r in dev.read_points()}
    assert values["process_temperature"].value == pytest.approx(22.0, abs=0.3)
    assert values["process_temperature"].unit == "°C"
    assert values["process_temperature_precise"].value == pytest.approx(22.0, abs=0.3)
    assert values["setpoint"].value == 25.0
    assert values["control_mode"].value == "standby"
    assert values["proportional_band"].value == 10.0
    assert values["output_enable"].value is False
    assert all(r.error is None for r in values.values())


def test_write_point_reads_back_and_heats():
    dev, sim, _ = make_device()
    reading, written = dev.write_point("setpoint", 60.04)
    assert written == 60.0 and reading.value == 60.0
    dev.write_point("control_mode", "auto")
    dev.write_point("output_enable", True)
    reading, _ = dev.write_point("proportional_band", 12.5)  # float32 over two registers
    assert reading.value == 12.5
    sim.advance(600)
    assert dev.read_point("process_temperature").value == pytest.approx(60.0, abs=0.5)
    assert dev.read_point("at_setpoint").value is True
    assert dev.read_point("output_power").value > 5


def test_write_point_limits_refuse_before_sending():
    dev, _, audit = make_device()
    before = len(_sent(audit))
    with pytest.raises(SafetyLimitError, match="maximum of 150"):
        dev.write_point("setpoint", 200)
    with pytest.raises(SafetyLimitError, match="not writable"):
        dev.write_point("process_temperature", 30)
    with pytest.raises(SafetyLimitError, match="only accepts"):
        dev.write_point("control_mode", "turbo")
    with pytest.raises(SafetyLimitError, match="maximum of 50"):
        dev.write_point("manual_output", 80)
    with pytest.raises(InstrumentProtocolError, match="No point named"):
        dev.write_point("nope", 1)
    assert len(_sent(audit)) == before


def test_modbus_exception_replies():
    dev, _, _ = make_device(register_map=None)  # raw mode: no map protection
    with pytest.raises(ModbusExceptionResponse, match="02 \\(ILLEGAL DATA ADDRESS\\)"):
        dev.read_registers("holding", 50, 1)
    with pytest.raises(ModbusExceptionResponse, match="03 \\(ILLEGAL DATA VALUE\\)"):
        dev.write_register(0, 5000)  # 500.0 °C: beyond the device's own -50..400 °C range
    with pytest.raises(ModbusExceptionResponse, match="0B"):
        ModbusDevice(SimulatedPIDController(unit_id=1), unit_id=9).read_registers("input", 0, 1)


def test_raw_writes_protect_mapped_addresses():
    dev, _, audit = make_device()
    with pytest.raises(SafetyLimitError, match="'setpoint'"):
        dev.write_register(0, 3000)
    with pytest.raises(SafetyLimitError, match="'proportional_band'"):
        dev.write_registers(3, [0, 0])  # overlaps ramp_rate and proportional_band
    with pytest.raises(SafetyLimitError, match="'output_enable'"):
        dev.write_coil(0, True)
    assert _sent(audit) == []
    raw_off, _, _ = make_device(register_map=None, allow_raw_writes=False)
    with pytest.raises(SafetyLimitError, match="raw_writes=false"):
        raw_off.write_register(100, 1)


def test_bounded_counts():
    dev, _, _ = make_device()
    with pytest.raises(SafetyLimitError, match="1-125"):
        dev.read_registers("holding", 0, 126)
    with pytest.raises(SafetyLimitError, match="1-2000"):
        dev.read_bits("coil", 0, 2001)
    with pytest.raises(SafetyLimitError, match="outside"):
        dev.read_registers("holding", 65535, 2)


def test_safe_state():
    dev, sim, _ = make_device()
    dev.write_point("control_mode", "auto")
    dev.write_point("output_enable", True)
    steps = dev.apply_safe_state()
    assert all(s["ok"] for s in steps)
    assert sim.coils[0] is False and sim.holding[1] == 0


def test_identify_and_extra_points(tmp_path):
    dev, _, _ = make_device()
    info = dev.identify()
    assert info["model"] == "PID-100" and info["probe"].startswith("ok: process_temperature")
    path = tmp_path / "custom.yaml"
    path.write_text(
        "points:\n"
        "  flow: {table: holding, address: 200, type: float32, unit: L/min, writable: true, min: 0, max: 10}\n"
        "  pump: {table: coil, address: 7, writable: true}\n",
        encoding="utf-8",
    )
    dev, _, _ = make_device(register_map=load_register_map(path))
    assert dev.write_point("flow", 2.5)[0].value == 2.5  # the simulator stores unknown mapped points
    assert dev.write_point("pump", True)[0].value is True


def test_audit_log_records_traffic():
    dev, _, audit = make_device()
    dev.read_point("setpoint")
    entries = audit.recent(10)
    assert entries[-2]["data"] == "unit 1 read_holding_registers address=0 count=1"
    assert entries[-1]["data"] == "[250]"


# ------------------------------------------------------------------ real pymodbus client


def test_default_port():
    assert _with_default_port("tcp://10.0.0.5") == "tcp://10.0.0.5:502"
    assert _with_default_port("tcp://10.0.0.5?timeout=1") == "tcp://10.0.0.5:502?timeout=1"
    assert _with_default_port("tcp://10.0.0.5:1502") == "tcp://10.0.0.5:1502"


@pytest.fixture
def pymodbus_server():
    """A real pymodbus TCP server on localhost (checks our wrapper against the installed pymodbus)."""
    datastore = pytest.importorskip("pymodbus.datastore")
    server_mod = pytest.importorskip("pymodbus.server")
    if not hasattr(datastore, "ModbusDeviceContext"):
        pytest.skip("pymodbus < 3.10 datastore API")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    block = datastore.ModbusSparseDataBlock
    device = datastore.ModbusDeviceContext(
        hr=block({0: [250, 1, 0]}), ir=block({0: [221]}), co=block({0: [False]}), di=block({0: [True]})
    )
    context = datastore.ModbusServerContext(devices={1: device}, single=False)
    loop = asyncio.new_event_loop()
    holder = {}

    async def serve():
        holder["server"] = server_mod.ModbusTcpServer(context, address=("127.0.0.1", port))
        await holder["server"].serve_forever()

    thread = threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True)
    thread.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port
    asyncio.run_coroutine_threadsafe(holder["server"].shutdown(), loop).result(5)
    thread.join(5)


def test_pymodbus_client_round_trip(pymodbus_server):
    client = PymodbusClient(f"tcp://127.0.0.1:{pymodbus_server}?retries=0", timeout=1.0)
    try:
        assert client.read_holding_registers(0, 3, unit=1) == [250, 1, 0]
        assert client.read_input_registers(0, 1, unit=1) == [221]
        assert client.read_discrete_inputs(0, 1, unit=1) == [True]
        client.write_register(0, 300, unit=1)
        client.write_registers(1, [2, 3], unit=1)
        assert client.read_holding_registers(0, 3, unit=1) == [300, 2, 3]
        client.write_coil(0, True, unit=1)
        assert client.read_coils(0, 1, unit=1) == [True]
        with pytest.raises(ModbusExceptionResponse, match="ILLEGAL DATA ADDRESS"):
            client.read_holding_registers(40, 1, unit=1)
        dev = ModbusDevice(client, unit_id=1, register_map=None)
        assert dev.read_registers("holding", 0, 1) == [300]
    finally:
        client.close()


def test_pymodbus_client_errors():
    with pytest.raises(InstrumentConnectionError, match="tcp:// or serial://"):
        PymodbusClient("visa://GPIB0::1::INSTR")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here
    with pytest.raises(InstrumentConnectionError, match="Could not open"):
        PymodbusClient(f"tcp://127.0.0.1:{port}?retries=0", timeout=0.3)
    with pytest.raises(InstrumentConnectionError, match="Unknown address parameter"):
        PymodbusClient(f"tcp://127.0.0.1:{port}?baud=9600", timeout=0.3)


# ------------------------------------------------------------------ MCP


async def test_tools_via_mcp():
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True
        assert info["instrument"]["probe"].startswith("ok")

        points = (await client.call_tool("list_points", {})).structured_content
        assert points["loaded"] is True
        assert {p["name"] for p in points["points"]} >= {"setpoint", "output_enable", "process_temperature"}

        values = (await client.call_tool("read_points", {"names": ["setpoint", "control_mode"]})).structured_content
        assert [v["value"] for v in values["result"]] == [25.0, "standby"]

        w = (await client.call_tool("write_point", {"name": "setpoint", "value": 45.5})).structured_content
        assert w["read_back"] == 45.5 and w["matches"] is True and w["unit"] == "°C"
        w = (await client.call_tool("write_point", {"name": "control_mode", "value": "auto"})).structured_content
        assert w["read_back"] == "auto"
        w = (await client.call_tool("write_point", {"name": "output_enable", "value": True})).structured_content
        assert w["read_back"] is True
        w = (await client.call_tool("write_point", {"name": "proportional_band", "value": 12.3})).structured_content
        assert w["matches"] is True

        regs = (
            await client.call_tool("read_registers", {"address": 4, "count": 2, "decode_as": "float32"})
        ).structured_content
        assert regs["decoded"][0] == pytest.approx(12.3, rel=1e-6)
        assert regs["hex"][0].startswith("0x")
        coils = (await client.call_tool("read_coils", {"address": 0})).structured_content
        assert coils["values"] == [True]
        di = (await client.call_tool("read_discrete_inputs", {"address": 0, "count": 3})).structured_content
        assert len(di["values"]) == 3

        with pytest.raises(Exception, match="maximum of 150"):
            await client.call_tool("write_point", {"name": "setpoint", "value": 180})
        with pytest.raises(Exception, match="write_point"):
            await client.call_tool("write_register", {"address": 0, "value": 1800})
        with pytest.raises(Exception, match="ILLEGAL DATA ADDRESS"):
            await client.call_tool("read_registers", {"address": 300})

        safe = (await client.call_tool("apply_safe_state", {})).data
        assert safe["all_ok"] is True
        values = (await client.call_tool("read_points", {"names": ["output_enable", "control_mode"]})).structured_content
        assert [v["value"] for v in values["result"]] == [False, "standby"]


async def test_raw_writes_to_unmapped_addresses(tmp_path):
    path = tmp_path / "partial.yaml"
    path.write_text("points:\n  pv: {table: input, address: 0, type: int16, scale: 0.1, unit: °C}\n", encoding="utf-8")
    async with simulated_client(server, options={"register_map": str(path)}) as client:
        r = (await client.call_tool("write_register", {"address": 10, "value": 1500})).structured_content
        assert r["read_back"] == [1500]
        r = (await client.call_tool("write_registers", {"address": 0, "values": [300, 0]})).structured_content
        assert r["read_back"] == [300, 0]
        r = (await client.call_tool("write_coil", {"address": 0, "value": True})).structured_content
        assert r["read_back"] is True
        names = await tool_names(client)
        assert "apply_safe_state" not in names  # this map has no safe_state


async def test_read_only_hides_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"list_points", "read_points", "read_registers", "read_coils", "read_discrete_inputs"} <= names
        assert {"write_point", "write_register", "write_registers", "write_coil"}.isdisjoint(names)
        assert {"apply_safe_state", "reconnect"} <= names


async def test_raw_writes_option_hides_raw_tools():
    async with simulated_client(server, options={"raw_writes": "false"}) as client:
        names = await tool_names(client)
        assert "write_point" in names
        assert {"write_register", "write_registers", "write_coil"}.isdisjoint(names)
    async with simulated_client(server, options={}) as client:
        assert "write_register" in await tool_names(client)


async def test_invalid_map_reports_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("points:\n  sp: {table: holding, address: 0, type: int16, writable: true}\n", encoding="utf-8")
    async with simulated_client(server, options={"register_map": str(path)}) as client:
        info = (await client.call_tool("list_points", {})).structured_content
        assert info["loaded"] is False and "min and max" in info["error"]
        with pytest.raises(Exception, match="Invalid register map"):
            await client.call_tool("read_points", {})
        assert "apply_safe_state" not in await tool_names(client)


async def test_unit_id_option():
    async with simulated_client(server, options={"unit_id": "5"}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["instrument"]["unit_id"] == 5
    async with simulated_client(server, options={"unit_id": "abc"}) as client:
        with pytest.raises(Exception, match="unit_id"):
            await client.call_tool("read_points", {})


# ------------------------------------------------------------------ regressions (review 2026-09)


def _stub_pymodbus(inner) -> PymodbusClient:
    from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

    client = PymodbusClient.__new__(PymodbusClient)
    client._errors = (ModbusIOException, ConnectionException, ModbusException)
    client._client, client._unit_kw, client.description = inner, "device_id", "modbus-rtu:///dev/ttyUSB0"
    return client


def test_os_error_from_pymodbus_becomes_connection_error_and_resets_the_link():
    # pyserial raises SerialException (an OSError) when the USB adapter is unplugged, and a reset
    # socket raises from send(); pymodbus lets both through and keeps the dead handle.
    class Unplugged:
        closed = False

        def read_holding_registers(self, address, count, device_id):
            raise OSError(5, "Input/output error")

        def close(self):
            self.closed = True

    inner = Unplugged()
    with pytest.raises(InstrumentConnectionError, match="failed during read_holding_registers"):
        _stub_pymodbus(inner).read_holding_registers(0, 2, unit=1)
    assert inner.closed  # the next request re-opens the port


def test_short_reply_is_a_protocol_error_not_a_struct_crash():
    dev, sim, _ = make_device()
    real = sim.read_input_registers
    sim.read_input_registers = lambda address, count, unit: real(address, count, unit)[:1]
    with pytest.raises(InstrumentProtocolError, match="1 value\\(s\\) instead of 2"):
        dev.read_point("process_temperature_precise")  # float32: struct.unpack needed 4 bytes
    by_name = {r.name: r for r in dev.read_points(["process_temperature_precise", "process_temperature"])}
    assert "instead of 2" in by_name["process_temperature_precise"].error
    assert by_name["process_temperature"].error is None


def test_nan_float_is_reported_not_silently_null():
    dev, sim, _ = make_device()
    sim._refresh_inputs = lambda: None  # freeze the input registers
    sim.input[2], sim.input[3] = 0x7FC0, 0x0000  # float32 NaN, a common "no value" marker
    r = dev.read_point("process_temperature_precise")
    assert r.value is None and "not a finite number" in r.error


async def test_nan_decode_as_float32_does_not_break_structured_output():
    async with simulated_client(server) as client:
        await client.call_tool("read_points", {"names": ["setpoint"]})
        sim = server.driver.client
        sim._refresh_inputs = lambda: None
        sim.input[2], sim.input[3] = 0xFFFF, 0xFFFF  # NaN
        r = (await client.call_tool(
            "read_registers", {"address": 2, "count": 2, "table": "input", "decode_as": "float32"}
        )).structured_content
        assert r["decoded"] == ["nan"]
        v = (await client.call_tool("read_points", {"names": ["process_temperature_precise"]})).structured_content
        assert v["result"][0]["value"] is None and "not a finite number" in v["result"][0]["error"]


def test_write_point_read_back_failure_does_not_hide_the_write():
    dev, sim, _ = make_device()

    def write_only(address, count, unit):
        raise ModbusExceptionResponse("read_holding_registers", address, 0x02)

    sim.read_holding_registers = write_only
    reading, written = dev.write_point("setpoint", 40)  # used to raise although 40 °C WAS written
    assert written == 40.0 and sim.holding[0] == 400
    assert reading.value is None and "was written, but reading it back failed" in reading.error


async def test_write_point_tool_reports_read_back_error():
    async with simulated_client(server) as client:
        await client.call_tool("read_points", {"names": ["setpoint"]})

        def write_only(address, count, unit):
            raise ModbusExceptionResponse("read_holding_registers", address, 0x02)

        server.driver.client.read_holding_registers = write_only
        w = (await client.call_tool("write_point", {"name": "setpoint", "value": 41})).structured_content
        assert w["written"] == 41.0 and w["matches"] is False and "ILLEGAL DATA ADDRESS" in w["read_back_error"]


def test_safe_state_is_not_ok_when_the_device_ignores_the_write():
    # Controllers in local/keypad mode often ACK a write and ignore it: that is not a safe state.
    dev, sim, _ = make_device()
    dev.write_point("output_enable", True)
    sim.write_coil = lambda address, value, unit: None  # acknowledged, ignored
    steps = {s["point"]: s for s in dev.apply_safe_state()}
    assert steps["output_enable"]["ok"] is False and steps["output_enable"]["read_back"] is True
    assert "reads back True instead of False" in steps["output_enable"]["error"]
    assert steps["control_mode"]["ok"] is True

    def unreadable(address, count, unit):
        raise ModbusExceptionResponse("read_holding_registers", address, 0x02)

    sim.read_holding_registers = unreadable
    steps = {s["point"]: s for s in dev.apply_safe_state()}
    assert steps["control_mode"]["ok"] is False and "not confirmed" in steps["control_mode"]["error"]
