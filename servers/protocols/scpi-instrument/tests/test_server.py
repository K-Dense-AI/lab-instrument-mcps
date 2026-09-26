import struct
import threading
import time

import pytest
from labmcp import InstrumentProtocolError, InstrumentTimeout, SafetyLimitError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_scpi.driver import SCPIInstrument, parse_error
from labmcp_scpi.policy import CommandPolicy, CommandRefused, normalized_form, short_form, split_units
from labmcp_scpi.server import server
from labmcp_scpi.simulator import DMMPowerSupplySimulator


@pytest.fixture(autouse=True)
def _reset_server():
    # simulated_client() keeps options from earlier calls unless told otherwise
    server.configure(simulate=True, read_only=False, options={}, limits={})
    yield
    server.disconnect()


def make_driver(**options: str) -> tuple[SCPIInstrument, DMMPowerSupplySimulator]:
    sim = DMMPowerSupplySimulator()
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n", encoding="latin-1")
    return SCPIInstrument(t, CommandPolicy.from_options(options), safe_state="OUTP OFF"), sim


# ------------------------------------------------------------------ policy


@pytest.mark.parametrize(
    ("long", "short"),
    [("VOLTage", "VOLT"), ("LEVel", "LEV"), ("ERRor", "ERR"), ("OUTPut2", "OUTP2"), ("IMMediate", "IMM"),
     ("STATe", "STAT"), ("FREQuency", "FREQ"), ("SWEEP", "SWE"), ("FREE", "FREE"), ("CALibration", "CAL"),
     ("CALCulate", "CALC")],
)
def test_short_form_rule(long, short):
    assert short_form(long) == short


def test_normalized_form():
    assert normalized_form(":OUTPut1:STATe  on") == "OUTP1:STAT ON"
    assert normalized_form("SOURce:VOLTage:LEVel 5 , 1") == "SOUR:VOLT:LEV 5,1"
    assert normalized_form("*idn?") == "*IDN?"
    assert split_units('DISP:TEXT "a;b";VOLT 5') == ['DISP:TEXT "a;b"', "VOLT 5"]


@pytest.mark.parametrize("query", ["*IDN?", "MEAS:VOLT:DC? 10,0.001", "VOLT? MAX", ":SYSTem:ERRor?", "CALC:DATA?"])
def test_query_accepted(query):
    assert CommandPolicy().check_query(query) == query


@pytest.mark.parametrize(
    ("query", "why"),
    [
        ("OUTP ON", "not a query"),
        ("VOLT 5;VOLT?", "';'"),
        ("*IDN?\nOUTP ON", "newline"),
        ("*TST?", "self-test"),
        ("*CAL?", "self-calibration"),
        ("CAL:ALL?", "calibration"),
        ("CALibration:ALL?", "calibration"),
        ("DIAGnostic:TEST?", "DIAGnostic"),
        ("", "empty"),
    ],
)
def test_query_refused(query, why):
    with pytest.raises(CommandRefused, match=why):
        CommandPolicy().check_query(query)


def test_query_denylist_option():
    policy = CommandPolicy.from_options({"query_denylist": r"^(MEAS|READ)"})
    with pytest.raises(CommandRefused, match="query denylist"):
        policy.check_query("MEASure:VOLTage:DC?")
    policy.check_query("FETC?")


def test_write_denylist_catches_long_forms_and_compound_units():
    policy = CommandPolicy.from_options({"write_denylist": r"^OUTP\d*(:STAT)? (ON|1)$"})
    for cmd in ["OUTP ON", "outp:stat 1", "VOLT 5;:OUTPut:STATe ON", "OUTPut2 ON"]:
        with pytest.raises(CommandRefused, match="denylist"):
            policy.check_command(cmd)
    assert policy.check_command("OUTP OFF") == ["OUTP OFF"]
    assert policy.check_command("VOLT 5;CURR 0.1") == ["VOLT 5", "CURR 0.1"]


def test_write_denylist_also_applies_to_queries():
    policy = CommandPolicy.from_options({"denylist": r"^MEAS"})
    with pytest.raises(CommandRefused, match="command denylist"):
        policy.check_query("MEAS:VOLT?")


def test_write_allowlist():
    policy = CommandPolicy.from_options({"write_allowlist": r"(CONF|SENS|TRIG)(:\S+)*( .*)?"})
    assert policy.check_command("CONFigure:VOLTage:DC 10") == ["CONFigure:VOLTage:DC 10"]
    assert policy.check_command("*IDN?") == ["*IDN?"]  # safe queries are always fine
    with pytest.raises(CommandRefused, match="allowlist"):
        policy.check_command("OUTP ON")
    with pytest.raises(CommandRefused, match="compound"):
        policy.check_command("CONF:VOLT:DC;OUTP ON")
    with pytest.raises(CommandRefused, match="allowlist"):
        policy.check_command("*TST?")  # side-effect queries need to be allowlisted too


def test_invalid_regex_and_bad_text():
    with pytest.raises(ValueError, match="write_denylist"):
        CommandPolicy.from_options({"write_denylist": "OUTP("})
    with pytest.raises(CommandRefused, match="unterminated"):
        CommandPolicy().check_command('DISP:TEXT "hello')


def test_parse_error():
    e = parse_error('-113,"Undefined header"')
    assert (e.code, e.message) == (-113, "Undefined header")
    assert parse_error('+0,"No error"').code == 0
    assert parse_error("garbage").code is None


# ------------------------------------------------------------------ driver + simulator


def test_identify():
    d, _ = make_driver()
    info = d.identify()
    assert info["manufacturer"] == "LabMCP"
    assert info["model"] == "SIM-DMM-PSU"
    assert info["raw"].count(",") == 3


def test_psu_and_dmm_physics():
    d, sim = make_driver()
    assert abs(float(d.checked_query("MEAS:VOLT:DC?").response)) < 1e-3  # output off at power-on
    r = d.checked_write("VOLT 5;CURR 1;OUTP ON")
    assert r.error_check == "ok"
    time.sleep(0.3)  # 20 ms slew time constant
    assert float(d.checked_query("MEAS:VOLT:DC?").response) == pytest.approx(5.0, abs=0.01)
    assert float(d.checked_query("MEAS:CURR:DC?").response) == pytest.approx(0.05, abs=1e-3)
    d.checked_write("CURR 0.02;VOLT 20")  # 20 V into 100 ohm needs 0.2 A -> constant current
    time.sleep(0.3)
    assert float(d.checked_query("MEAS:VOLT?").response) == pytest.approx(2.0, abs=0.01)
    assert d.checked_query("OUTP?").response == "1"
    assert d.checked_write("VOLT?").response == "+2.000000000E+01"


def test_error_replies():
    d, _ = make_driver()
    r = d.checked_write("VOLT 50")
    assert r.error_check == "errors"
    assert r.errors[0].code == -222
    assert d.checked_write("BOGUS 1").errors[0].code == -113
    assert d.checked_write("OUTP MAYBE").errors[0].code == -224
    assert d.checked_write("VOLT").errors[0].code == -109
    assert d.checked_query("*ESR?").response == "48"  # command (-1xx) + execution (-2xx) error bits
    assert d.checked_query("*ESR?").response == "0"  # reading the ESR clears it
    d.checked_write("BOGUS 1", check_errors=False)
    assert d.checked_query("*STB?").response == "4"  # error queue not empty
    assert d.checked_query("*ESR?").response == "32"
    assert d.read_errors() != []
    assert d.read_errors() == []


def test_unknown_query_times_out_and_reports_queue():
    d, _ = make_driver()
    with pytest.raises(InstrumentTimeout, match="-113"):
        d.checked_query("BOGUS?")


def test_error_queue_overflow():
    d, _ = make_driver()
    for _ in range(12):
        d.t.write("BOGUS")
    errors = d.read_errors(max_errors=50)
    assert len(errors) == 10
    assert errors[-1].code == -350


def test_binary_block():
    d, _ = make_driver()
    d.checked_write("VOLT 3;OUTP ON;FORM REAL,64;TRIG:COUN 5")
    time.sleep(0.3)
    data = d.read_block("READ?", max_bytes=1000)
    values = struct.unpack(">5d", data)
    assert all(v == pytest.approx(3.0, abs=0.01) for v in values)
    d.checked_write("FORM:BORD SWAP;FORM REAL,32")
    values = struct.unpack("<5f", d.read_block("FETC?", max_bytes=1000))
    assert values[0] == pytest.approx(3.0, abs=0.01)
    with pytest.raises(InstrumentProtocolError, match="query_binary_block"):
        d.checked_query("FETC?")
    assert d.checked_query("*IDN?").response.startswith("LabMCP")  # resynchronised


def test_binary_block_size_limit_and_ascii_reply():
    d, _ = make_driver()
    d.checked_write("FORM REAL,64;TRIG:COUN 100")
    with pytest.raises(InstrumentProtocolError, match="max_block_bytes"):
        d.read_block("READ?", max_bytes=100)
    assert d.checked_query("*IDN?").response.startswith("LabMCP")
    d.checked_write("FORM ASC")
    with pytest.raises(InstrumentProtocolError, match="Expected an IEEE 488.2 binary block"):
        d.read_block("READ?", max_bytes=100)


def test_reset_clear_and_safe_state():
    d, sim = make_driver()
    d.checked_write("VOLT 5;OUTP ON")
    assert sim.output_on
    d.apply_safe_state()
    assert not sim.output_on
    d.checked_write("VOLT 5;OUTP ON;BOGUS", check_errors=False)
    result = d.device_clear()
    assert result["errors_before_clear"][0].code == -113
    assert d.read_errors() == []
    r = d.reset()
    assert r.error_check == "ok"
    assert not sim.output_on and sim.voltage_set == 0.0
    assert d.wait_operation_complete(1.0) >= 0


def test_reset_respects_denylist():
    d, _ = make_driver(write_denylist=r"^\*RST")
    with pytest.raises(CommandRefused):
        d.reset()


# ------------------------------------------------------------------ MCP


def _mark() -> str:
    marker = f"test-marker-{time.monotonic_ns()}"
    server.audit.event(marker, "test")
    return marker


def _writes_since(marker: str) -> list[str]:
    entries = server.audit.recent(500)
    start = max(i for i, e in enumerate(entries) if e["data"] == marker)
    return [e["data"] for e in entries[start + 1 :] if e["direction"] == "write"]


async def test_tools_via_mcp(tmp_path):
    async with simulated_client(server) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        ident = (await client.call_tool("identify", {})).structured_content
        assert ident["model"] == "SIM-DMM-PSU"

        primer = (await client.call_tool("scpi_primer", {})).data
        assert "short form" in primer["primer"] and primer["safe_state"] == "OUTP OFF"

        visa = (await client.call_tool("list_visa_resources", {})).structured_content
        assert visa["simulated"] is True

        r = (await client.call_tool("scpi_write", {"command": "VOLT 2.5;CURR 0.5;OUTP ON"})).structured_content
        assert r["ok"] is True and r["errors"] == []
        time.sleep(0.3)
        q = (await client.call_tool("scpi_query", {"command": "MEAS:VOLT:DC?"})).structured_content
        assert float(q["response"]) == pytest.approx(2.5, abs=0.01)

        r = (await client.call_tool("scpi_write", {"command": "VOLT 99"})).structured_content
        assert r["ok"] is False and r["errors"][0]["code"] == -222

        batch = (
            await client.call_tool(
                "scpi_batch", {"steps": ["FORM REAL,32", "TRIG:COUN 4", "BOGUS", "OUTP OFF"]}
            )
        ).structured_content
        assert [s["status"] for s in batch["steps"]] == ["ok", "ok", "errors", "not_run"]
        assert batch["stopped_early"] is True

        csv_path = tmp_path / "readings.csv"
        block = (
            await client.call_tool(
                "query_binary_block",
                {"command": "READ?", "decode_as": "float32", "save_path": str(csv_path)},
            )
        ).structured_content
        assert block["length_bytes"] == 16
        assert block["decoded"]["count"] == 4
        assert block["decoded"]["mean"] == pytest.approx(2.5, abs=0.01)
        assert block["data_base64"] is not None
        assert csv_path.read_text().startswith("index,value")

        server.driver.t.write("BOGUS")  # queue an error without reading it back
        errors = (await client.call_tool("get_errors", {})).structured_content["result"]
        assert errors[0]["code"] == -113
        assert (await client.call_tool("get_errors", {})).structured_content["result"] == []

        done = (await client.call_tool("wait_operation_complete", {"timeout_s": 5})).data
        assert done["complete"] is True

        safe = (await client.call_tool("apply_safe_state", {})).structured_content
        assert safe["command"] == "OUTP OFF" and safe["ok"] is True
        q = (await client.call_tool("scpi_query", {"command": "OUTP?"})).structured_content
        assert q["response"] == "0"

        cleared = (await client.call_tool("device_clear", {})).data
        assert cleared["error_check"] == "ok"
        reset = (await client.call_tool("reset_instrument", {})).structured_content
        assert reset["ok"] is True


async def test_scpi_query_refuses_commands_and_sends_nothing():
    async with simulated_client(server) as client:
        marker = _mark()
        with pytest.raises(Exception, match="scpi_write"):
            await client.call_tool("scpi_query", {"command": "OUTP ON"})
        with pytest.raises(Exception, match="self-test"):
            await client.call_tool("scpi_query", {"command": "*TST?"})
        assert _writes_since(marker) == []


async def test_policy_options_via_mcp():
    options = {"write_denylist": r"^OUTP\d*(:STAT)? (ON|1)$", "query_denylist": "^MEAS"}
    async with simulated_client(server, options=options) as client:
        await client.call_tool("identify", {})  # connect first
        marker = _mark()
        with pytest.raises(Exception, match="denylist"):
            await client.call_tool("scpi_write", {"command": "VOLT 1;:OUTPut:STATe ON"})
        with pytest.raises(Exception, match="denylist"):  # validated before the first step is sent
            await client.call_tool("scpi_batch", {"steps": ["VOLT 1", "OUTP 1"]})
        with pytest.raises(Exception, match="query denylist"):
            await client.call_tool("scpi_query", {"command": "MEAS:VOLT?"})
        assert _writes_since(marker) == []
        r = (await client.call_tool("scpi_write", {"command": "MEAS:VOLT?"})).structured_content
        assert r["response"] is not None  # side-effect queries still work through the HAZARD tool


async def test_invalid_policy_fails_closed():
    async with simulated_client(server, options={"write_allowlist": "("}) as client:
        with pytest.raises(Exception, match="Invalid server configuration"):
            await client.call_tool("scpi_query", {"command": "*IDN?"})


async def test_read_only_hides_hazard_tools():
    async with simulated_client(server, read_only=True) as client:
        names = await tool_names(client)
        assert {"scpi_query", "identify", "get_errors", "query_binary_block", "scpi_primer"} <= names
        assert {"scpi_write", "scpi_batch", "reset_instrument"}.isdisjoint(names)
        assert {"device_clear", "apply_safe_state", "reconnect"} <= names  # safety tools stay


async def test_safe_state_tool_only_listed_when_configured():
    try:
        server.configure(simulate=False, read_only=False, options={})
        names = {t.name for t in await server.mcp.list_tools()}
        assert "apply_safe_state" not in names and "device_clear" in names
        server.configure(simulate=False, options={"safe_state": "OUTP1 OFF;:OUTP2 OFF"})
        names = {t.name for t in await server.mcp.list_tools()}
        assert "apply_safe_state" in names
    finally:
        server.configure(simulate=True, options={})


async def test_wait_limit():
    async with simulated_client(server, limits={"max_operation_wait_s": 10}) as client:
        with pytest.raises(Exception, match="max_operation_wait_s"):
            await client.call_tool("wait_operation_complete", {"timeout_s": 60})


async def test_block_size_limit():
    async with simulated_client(server, limits={"max_block_bytes": 16}) as client:
        await client.call_tool("scpi_write", {"command": "FORM REAL,64;TRIG:COUN 10"})
        with pytest.raises(Exception, match="max_block_bytes"):
            await client.call_tool("query_binary_block", {"command": "READ?"})


def test_limit_error_type():
    server.configure(simulate=True, limits={"max_operation_wait_s": 1})
    with pytest.raises(SafetyLimitError):
        server.check("max_operation_wait_s", 5)


async def test_read_only_refuses_measure_queries_by_default():
    from labmcp.testing import simulated_client

    async with simulated_client(server, read_only=True) as client:
        with pytest.raises(Exception, match="read-only"):
            await client.call_tool("scpi_query", {"command": "MEAS:VOLT:DC?"})
    async with simulated_client(server, read_only=True, options={"allow_measure_in_read_only": "true"}) as client:
        result = await client.call_tool("scpi_query", {"command": "MEAS:VOLT:DC?"})
        assert result.structured_content


def test_write_denylist_resolves_relative_headers_in_compound_messages():
    # SCPI-99 Vol. 1, 6.2.4: after ';' a header without a leading colon continues the previous
    # path, so 'OUTP:POL NORM;STAT ON' switches the output on (OUTP:STAT ON).
    policy = CommandPolicy.from_options({"write_denylist": r"^OUTP\d*(:STAT)? (ON|1)$"})
    for cmd in ["OUTP:POL NORM;STAT ON", "OUTPut1:PROTection OFF;STATe 1", "*CLS;OUTP:POL NORM;*WAI;STAT ON"]:
        with pytest.raises(CommandRefused, match="denylist"):
            policy.check_command(cmd)
    # a leading colon returns to the root, so this STAT is not under OUTP
    assert policy.check_command("OUTP:POL NORM;:STAT ON") == ["OUTP:POL NORM", ":STAT ON"]
    # and a deeper path does not collapse: this is OUTP:PROT:STAT, not OUTP:STAT
    assert policy.check_command("OUTP:PROT:CLE;STAT ON") == ["OUTP:PROT:CLE", "STAT ON"]


def test_expand_headers():
    from labmcp_scpi.policy import expand_headers

    assert expand_headers(["SOUR:VOLT 1", "CURR 2", "*OPC", "LEV 3", ":OUTP ON", "PROT:CLE"]) == [
        "SOUR:VOLT 1", "SOUR:CURR 2", "*OPC", "SOUR:LEV 3", "OUTP ON", "PROT:CLE",
    ]


def test_binary_block_late_terminator_is_consumed():
    """IEEE 488.2 ends a block response with NL; if it arrives late it must still be consumed."""
    d, _ = make_driver()
    t = d.t
    d.checked_write("FORM REAL,64")
    real_push = t._push

    def delayed(data: bytes) -> None:  # hold back the final terminator of the block reply
        if data.startswith(b"#") and data.endswith(b"\n"):
            real_push(data[:-1])
            threading.Timer(0.2, real_push, args=(b"\n",)).start()
        else:
            real_push(data)

    t._push = delayed
    d.read_block("READ?", max_bytes=1000)
    t._push = real_push
    time.sleep(0.3)
    assert d.checked_query("*IDN?").response.startswith("LabMCP")
