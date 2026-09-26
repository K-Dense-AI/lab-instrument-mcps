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


# ------------------------------------------------------------------ regression tests (code review)


def test_denylist_not_hidden_by_a_quote_inside_block_data():
    # '#11"' is a 1-byte definite-length block holding a '"'. The instrument's parser skips it,
    # so it runs OUTP ON; a quote-aware split alone saw a single DATA unit with a string in it.
    command = 'DATA #11";OUTP ON;DISP:TEXT "x'
    policy = CommandPolicy.from_options({"write_denylist": r"^OUTP\d*(:STAT)? (ON|1)$"})
    with pytest.raises(CommandRefused, match="denylist"):
        policy.check_command(command)
    policy = CommandPolicy.from_options({"write_allowlist": r"(DATA|DISP)(:\S+)*( .*)?"})
    with pytest.raises(CommandRefused, match="compound"):
        policy.check_command(command)
    with pytest.raises(CommandRefused, match="compound"):  # any ';' at all under an allowlist
        policy.check_command('DISP:TEXT "a;b"')
    assert policy.check_command('DISP:TEXT "ab"') == ['DISP:TEXT "ab"']


def test_denylist_alias_conflict_fails_closed():
    with pytest.raises(ValueError, match="both set"):
        CommandPolicy.from_options({"write_denylist": "^OUTP", "denylist": r"^\*RST"})
    same = CommandPolicy.from_options({"write_denylist": "^OUTP", "denylist": "^OUTP"})
    assert same.write_denylist is not None and same.write_denylist.pattern == "^OUTP"
    alias_only = CommandPolicy.from_options({"denylist": "^OUTP"})
    assert alias_only.write_denylist is not None


@pytest.mark.parametrize(
    "command",
    ["OUTP ON", "OUTP 2", "OUTP 1.0", "OUTP +1", "OUTP #H1", "outp:stat 0.7", "OUTPut2:STATe 5",
     "VOLT 5;:OUTP 1", "OUTP:POL NORM;STAT 3"],
)
def test_recommended_output_denylist_catches_numeric_booleans(command):
    # SCPI booleans take any number (non-zero = ON), so '(ON|1)$' is not enough.
    policy = CommandPolicy.from_options({"write_denylist": r"^OUTP\d*(:STAT)? (?!OFF\b)"})
    with pytest.raises(CommandRefused, match="denylist"):
        policy.check_command(command)


@pytest.mark.parametrize("command", ["OUTP OFF", "OUTPut:STATe OFF", "OUTP?", "OUTP:POL NORM", "OUTP:PROT:CLE"])
def test_recommended_output_denylist_allows_off(command):
    policy = CommandPolicy.from_options({"write_denylist": r"^OUTP\d*(:STAT)? (?!OFF\b)"})
    assert policy.check_command(command)


class _BadBlockSimulator(DMMPowerSupplySimulator):
    def command(self, key: str, arg: str) -> str | None:
        if key == "BAD:BLOCK?":
            return "#2xy0123"
        return super().command(key, arg)


def test_read_block_malformed_length_field_is_a_protocol_error():
    sim = _BadBlockSimulator()
    t = SimulatedTransport(sim, read_termination="\n", write_termination="\n", encoding="latin-1")
    d = SCPIInstrument(t, CommandPolicy())
    with pytest.raises(InstrumentProtocolError, match="length field"):
        d.read_block("BAD:BLOCK?", max_bytes=1000)
    assert d.checked_query("*IDN?").response.startswith("LabMCP")  # resynchronised


def test_read_block_timeout_bounds_the_whole_transfer():
    # Each piece of the block arrives 0.3 s after it is asked for. With a 0.5 s timeout for
    # the whole transfer, the second read must already fail (it used to get a fresh 0.5 s).
    d, _ = make_driver()
    d.checked_write("FORM REAL,64")
    real = d.t.read_bytes

    def slow(size, timeout=None):
        if timeout is not None and timeout < 0.3:
            time.sleep(timeout)
            raise InstrumentTimeout("slow instrument")
        time.sleep(0.3)
        return real(size, timeout)

    d.t.read_bytes = slow
    t0 = time.monotonic()
    with pytest.raises(InstrumentTimeout):
        d.read_block("READ?", max_bytes=1000, timeout=0.5)
    d.t.read_bytes = real
    assert time.monotonic() - t0 < 1.0


def test_decode_non_finite_values_become_null():
    from labmcp_scpi.server import _decode

    data = struct.pack(">4f", 1.0, float("nan"), float("inf"), 3.0)
    decoded, values = _decode(data, "float32", "big", 10)
    assert decoded.values == [1.0, None, None, 3.0]
    assert decoded.non_finite == 2 and decoded.mean == 2.0
    assert len(values) == 4


async def test_query_binary_block_with_nan_values_via_mcp():
    async with simulated_client(server) as client:
        await client.call_tool("scpi_write", {"command": "FORM REAL,32;TRIG:COUN 3"})
        server.driver.t.simulator._reading = lambda: float("nan")
        block = (
            await client.call_tool("query_binary_block", {"command": "READ?", "decode_as": "float32"})
        ).structured_content
        assert block["decoded"]["values"] == [None, None, None]
        assert block["decoded"]["non_finite"] == 3 and block["decoded"]["mean"] is None


async def test_query_binary_block_save_path_checks(tmp_path):
    async with simulated_client(server) as client:
        await client.call_tool("scpi_write", {"command": "FORM REAL,32;TRIG:COUN 2"})
        marker = _mark()
        with pytest.raises(Exception, match="must end in"):
            await client.call_tool("query_binary_block", {"command": "READ?", "save_path": str(tmp_path / "x.sh")})
        existing = tmp_path / "old.bin"
        existing.write_bytes(b"keep")
        with pytest.raises(Exception, match="already exists"):
            await client.call_tool("query_binary_block", {"command": "READ?", "save_path": str(existing)})
        assert _writes_since(marker) == []  # refused before the query was sent
        assert existing.read_bytes() == b"keep"
        r = (
            await client.call_tool(
                "query_binary_block", {"command": "READ?", "save_path": str(existing), "overwrite": True}
            )
        ).structured_content
        assert existing.read_bytes() != b"keep" and len(existing.read_bytes()) == 8
        nested = tmp_path / "new" / "dir" / "trace.csv"
        r = (
            await client.call_tool(
                "query_binary_block", {"command": "READ?", "decode_as": "float32", "save_path": str(nested)}
            )
        ).structured_content
        assert r["saved_to"] == str(nested.resolve())
        assert nested.read_text(encoding="utf-8").splitlines()[0] == "index,value"


async def test_scpi_batch_stops_sending_when_its_time_budget_is_used(monkeypatch):
    import labmcp_scpi.server as scpi_server

    # Pretend the 900 s tool timeout is 0.2 s away: FastMCP would report a timeout but could not
    # stop the thread, so the batch itself must not send anything after that point.
    monkeypatch.setattr(scpi_server, "BATCH_MARGIN_S", scpi_server.BATCH_TIMEOUT_S - 0.2)
    async with simulated_client(server) as client:
        await client.call_tool("identify", {})
        marker = _mark()
        batch = (
            await client.call_tool(
                "scpi_batch",
                {"steps": ["VOLT 1", "VOLT 2", "OUTP ON"], "delay_between_s": 0.3, "stop_on_error": False},
            )
        ).structured_content
        assert [s["status"] for s in batch["steps"]] == ["ok", "not_run", "not_run"]
        assert "time budget" in batch["steps"][1]["detail"]
        assert batch["stopped_early"] is True and batch["completed"] == 1
        sent = _writes_since(marker)
        assert not any("VOLT 2" in w or "OUTP ON" in w for w in sent)
        assert not server.driver.t.simulator.output_on


def test_oversized_block_still_arriving_is_drained_not_left_for_the_next_reply():
    # On a socket the payload is still arriving when the header has been read, so a flush
    # right after the header left most of it to be read as the reply to the next query.
    d, _ = make_driver()
    t = d.t
    d.checked_write("FORM REAL,64;TRIG:COUN 100")
    real_push = t._push

    def trickle(data: bytes) -> None:
        if data.startswith(b"#"):
            real_push(data[:6])  # '#3800' + first payload byte
            threading.Timer(0.2, real_push, args=(data[6:],)).start()
        else:
            real_push(data)

    t._push = trickle
    with pytest.raises(InstrumentProtocolError, match="max_block_bytes"):
        d.read_block("READ?", max_bytes=100, timeout=2.0)
    t._push = real_push
    time.sleep(0.3)
    assert d.checked_query("*IDN?").response.startswith("LabMCP")
