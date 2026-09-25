import asyncio
import json
import socket
import threading
import time

import pytest
from labmcp import InstrumentConnectionError, SimulatedTransport
from labmcp.testing import simulated_client, tool_names
from labmcp_astm_lis.driver import (
    ACK,
    ENQ,
    EOT,
    NAK,
    ASTMReceiver,
    Delimiters,
    E1381Receiver,
    FrameError,
    ListenTransport,
    ResultStore,
    build_frames,
    components,
    frame_checksum,
    parse_frame,
    parse_message,
    parse_since,
    redact_patient_record,
    split_fields,
    unescape,
)
from labmcp_astm_lis.server import server
from labmcp_astm_lis.simulator import ASTMAnalyzerSimulator, default_messages, encode_frames

# Records from the "Typical Upload" examples in Beckman Coulter C03112-AF, chapter 3.
BECKMAN_UPLOAD = [
    "H|\\^&|||ACCESS^500001|||||LIS||P|1|20111231235959",
    "P|1|098765678",
    "O|1|SPEC1234|^1^4|^^^Ferritin^2|||||||||||Serum||||||||||F",
    "R|1|^^^Ferritin^1|105.6|ng/ml||N||F||||20111231235959",
    "C|1|I|CEX;PEX|I",
    "L|1|F",
]


def wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ E1381 frames


@pytest.mark.parametrize(
    "text, checksum",
    [  # frame number + record + CR, followed by ETX; checksums printed in C03112-AF
        ("1H|\\^&|||ACCESS^500001|||||LIS||P|1|20111231235959", "20"),
        ("1H|\\^&|", "61"),
        ("2P|1|098765678", "A3"),
        ("3O|1|SPEC1234||^^^Ferritin|R||||||A||||Serum", "F8"),
        ("4C|1|I|CEX;PEX|I", "42"),
        ("4L|1|F", "FF"),
    ],
)
def test_checksum_matches_vendor_examples(text, checksum):
    assert frame_checksum(text.encode() + b"\r\x03") == checksum


def test_parse_frame_and_errors():
    good = b"\x021H|\\^&|\r\x0361\r\n"
    frame = parse_frame(b"noise" + good)  # characters before STX are ignored
    assert frame.number == 1 and frame.final and frame.text == b"H|\\^&|\r"
    with pytest.raises(FrameError, match="checksum mismatch"):
        parse_frame(b"\x021H|\\^&|\r\x0362\r\n")
    with pytest.raises(FrameError, match="frame number"):
        parse_frame(b"\x029H|\r\x0300\r\n")
    with pytest.raises(FrameError, match="ETX/ETB"):
        parse_frame(b"\x021H|\\^&|\r61\r\n")


def test_build_frames_splits_long_records_and_wraps_numbers():
    record = "R|1|" + "X" * 500
    frames = build_frames(record, 7)
    assert len(frames) == 3
    assert [f[1:2] for f in frames] == [b"7", b"0", b"1"]  # modulo 8
    assert frames[0][-5:-4] == b"\x17" and frames[-1][-5:-4] == b"\x03"  # ETB, ETB, ETX
    assert all(len(parse_frame(f).text) <= 240 for f in frames)


def make_link():
    records, sessions, log = [], [], []
    link = E1381Receiver(records.append, sessions.append, log.append)
    return link, records, sessions, log


def test_receiver_session_ack_nak_and_frame_numbers():
    link, records, sessions, _ = make_link()
    assert link.on_frame(build_frames("H|\\^&", 1)[0]) == b""  # no ENQ yet: ignored
    assert link.on_enq() == ACK
    msg = [f"R|{i}|^^^T{i}|{i}" for i in range(1, 11)]
    frames = encode_frames(msg)  # 10 frames: numbers 1..7, 0, 1, 2
    assert [f[1:2] for f in frames][6:9] == [b"7", b"0", b"1"]
    for f in frames[:3]:
        assert link.on_frame(f) == ACK
    assert link.on_frame(frames[2]) == ACK  # retransmission after a lost ACK
    assert link.stats.duplicate_frames == 1
    assert link.on_frame(frames[4]) == NAK  # frame 4 skipped
    bad = bytearray(frames[3])
    bad[6] ^= 0x01
    assert link.on_frame(bytes(bad)) == NAK
    assert link.stats.checksum_errors == 1
    for f in frames[3:]:
        assert link.on_frame(f) == ACK
    link.on_eot()
    assert records == msg
    assert sessions == [True]


def test_receiver_timeout_discards_incomplete_message():
    link, _, sessions, log = make_link()
    link.on_enq()
    link.on_frame(build_frames("H|\\^&", 1)[0])
    link.check_timeout(time.monotonic() + 31)
    assert link.state == "neutral" and sessions == [False]
    assert any("discarded" in line for line in log)


# ------------------------------------------------------------------ E1394 records


def test_parse_beckman_upload_example():
    m = parse_message(BECKMAN_UPLOAD, message_id=1, received_at="2026-01-01T00:00:00+00:00", source="t", complete=True, redacted=False)
    assert m.analyzer == "ACCESS 500001" and m.receiver_id == "LIS" and m.termination_code == "F"
    (r,) = m.results
    assert r.sample_id == "SPEC1234" and r.instrument_specimen_id == "^1^4"
    assert r.patient_id == "098765678"
    assert r.test_code == "Ferritin" and r.value == "105.6" and r.numeric_value == 105.6
    assert r.units == "ng/ml" and r.abnormal_flags == ["N"] and r.result_status == "F"
    assert r.completed_at == "2011-12-31T23:59:59"
    assert r.comments == ["CEX;PEX"]
    assert r.specimen_type == "Serum"


def test_interpretation_component_and_cancelled_value():
    rec = BECKMAN_UPLOAD[:3] + ["R|1|^^^Rub-IgG^1|0.24^Non-React.|S/CO||N||F||||20111231235959", "R|2|^^^TU^1|Cancelled|%Uptake||N||X", "L|1|F"]
    m = parse_message(rec, message_id=1, received_at="x", source="t", complete=True, redacted=False)
    assert (m.results[0].value, m.results[0].interpretation) == ("0.24", "Non-React.")
    assert m.results[1].numeric_value is None and m.results[1].result_status_meaning.startswith("result cannot")


def test_sysmex_style_test_and_sample_ids():
    rec = [
        "H|\\^&|||XN-350^00-18^11001^^^^12345678||||||||E1394-97",
        "P|1",
        "O|1||^^                00000001^B|^^^^WBC\\^^^^RBC|||||||N||||||||||||||F",
        "R|1|^^^^WBC^1|7.80|10*3/uL||N||||||20011116101000",
        "L|1|N",
    ]
    (r,) = parse_message(rec, message_id=1, received_at="x", source="t", complete=True, redacted=False).results
    assert r.test_code == "WBC" and r.sample_id == "00000001" and r.value == "7.80"


def test_escapes_and_custom_delimiters():
    d = Delimiters()
    assert unescape("1&S&40", d) == "1^40"
    assert components("1&S&40^x", d) == ["1^40", "x"]  # escaped delimiter does not split
    assert unescape("a&|&b &&& c&F&d", d) == "a|b & c|d"  # vendor style and mnemonics
    assert unescape("A&B", d) == "A&B"  # a lone escape character is literal
    custom = "H!@#$!!!ANALYZER#7"
    dd = Delimiters.from_header(custom)
    assert (dd.field, dd.repeat, dd.component, dd.escape) == ("!", "@", "#", "$")
    assert split_fields(custom, dd)[4] == "ANALYZER#7"
    m = parse_message([custom, "O!1!S1!!###GLU", "R!1!###GLU!5.5!mmol/L", "L!1!N"], message_id=1, received_at="x", source="t", complete=True, redacted=False)
    assert m.results[0].test_code == "GLU" and m.results[0].sample_id == "S1"


def test_redaction_masks_identifiers_only():
    rec = "P|1|PAT-9|LAB-9||Roe^Richard^A|Smith|19571230|M||1 Example Rd||555-0100|Dr Lee"
    red = redact_patient_record(rec, Delimiters())
    assert red == "P|1|PAT-9|LAB-9||REDACTED|REDACTED|REDACTED|M||REDACTED||REDACTED|Dr Lee"


def test_query_message_is_flagged_not_answered():
    m = parse_message(["H|\\^&", "Q|1|^Samp45||ALL||||||||O", "L|1|N"], message_id=1, received_at="x", source="t", complete=True, redacted=True)
    assert m.kind == "query" and "Samp45" in m.queries[0]
    assert any("receive-only" in w for w in m.warnings)


def test_store_filters_and_jsonl_reload(tmp_path):
    path = tmp_path / "results.jsonl"
    store = ResultStore(store_path=str(path))
    for msg in default_messages():
        store.add(msg, source="t", complete=True)
    assert store.results(sample_id="s26-000123")[1] == 20
    assert store.results(patient_id="PAT-000982")[1] == 14
    assert [r.value for r in store.results(test_code="k")[0]] == ["5.8"]
    abnormal, n = store.results(abnormal_only=True)
    assert n == 7 and {r.test_code for r in abnormal} >= {"WBC", "PLT", "GLU", "K"}
    assert store.results(since=parse_since("2999-01-01"))[1] == 0
    reloaded = ResultStore(store_path=str(path))
    assert reloaded.counts()["results"] == 34
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2 and rows[1]["records"][0].startswith("H|")


# ------------------------------------------------------------------ receiver thread + simulator


def make_sim_receiver(**kwargs):
    sim = ASTMAnalyzerSimulator(gap_s=0.2)
    link = SimulatedTransport(sim)
    store = ResultStore(redacted=kwargs.get("redact", True))
    rx = ASTMReceiver(lambda: link, mode="simulated", where="sim", store=store, on_close=[sim.stop], **kwargs)
    sim.attach(link.push)
    return rx, sim, store


def test_simulated_analyzer_bad_frame_is_naked_and_retransmitted():
    rx, sim, store = make_sim_receiver()
    try:
        assert wait_until(lambda: store.counts()["messages"] == 2)
        assert sim.naks_received == 1
        assert "frame index 6 (retransmission)" in sim.log
        assert rx.protocol.stats.checksum_errors == 1
        assert store.counts()["results"] == 34  # nothing lost, nothing duplicated
        msgs, _ = store.messages()
        assert all(m.complete for m in msgs)
        patient = msgs[1].records[1]
        assert "Roe" not in patient and "19571230" not in patient and "PAT-000982" in patient
    finally:
        rx.close()


def test_redaction_can_be_disabled():
    rx, _, store = make_sim_receiver(redact=False)
    try:
        assert wait_until(lambda: store.counts()["messages"] == 2)
        r = store.results(sample_id="S26-000124")[0][0]
        assert r.patient_name == "Roe^Richard^A" and r.patient_birth_date == "1957-12-30T00:00:00"
    finally:
        rx.close()


def _send_message_as_analyzer(sock, records, corrupt_first=False):
    replies = []
    sock.sendall(ENQ)
    replies.append(sock.recv(1))
    for i, frame in enumerate(encode_frames(records)):
        if corrupt_first and i == 0:
            bad = bytearray(frame)
            bad[3] ^= 0x01
            sock.sendall(bytes(bad))
            replies.append(sock.recv(1))
        sock.sendall(frame)
        replies.append(sock.recv(1))
    sock.sendall(EOT)
    return replies


def test_tcp_listener_receives_from_analyzer():
    store = ResultStore()
    rx = ASTMReceiver(lambda: ListenTransport("127.0.0.1", 0), mode="tcp-listen", where="test", store=store)
    port = rx.link.port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as analyzer:
            replies = _send_message_as_analyzer(analyzer, BECKMAN_UPLOAD, corrupt_first=True)
        assert replies[0] == ACK and replies[1] == NAK and set(replies[2:]) == {ACK}
        assert wait_until(lambda: store.counts()["results"] == 1)
        assert store.messages()[0][0].source.startswith("127.0.0.1:")
    finally:
        rx.close()


def test_tcp_listener_allow_from_rejects_other_hosts():
    link = ListenTransport("127.0.0.1", 0, allow_from={"10.9.9.9"})
    rx = ASTMReceiver(lambda: link, mode="tcp-listen", where="test", store=ResultStore())
    try:
        with socket.create_connection(("127.0.0.1", link.port), timeout=5) as s:
            s.settimeout(3)
            assert s.recv(1) == b""  # closed by the server
        assert link.rejected_connections == 1
    finally:
        rx.close()


def test_listen_port_in_use_is_reported():
    busy = socket.create_server(("127.0.0.1", 0))
    try:
        with pytest.raises(InstrumentConnectionError, match="Could not listen"):
            ListenTransport("127.0.0.1", busy.getsockname()[1])
    finally:
        busy.close()


def test_tcp_client_mode_via_server():
    analyzer_srv = socket.create_server(("127.0.0.1", 0))
    port = analyzer_srv.getsockname()[1]
    replies: list[bytes] = []

    def analyzer():
        conn, _ = analyzer_srv.accept()
        with conn:
            conn.settimeout(5)
            replies.extend(_send_message_as_analyzer(conn, BECKMAN_UPLOAD))
            time.sleep(0.5)

    t = threading.Thread(target=analyzer, daemon=True)
    t.start()
    server.configure(address=f"tcp://127.0.0.1:{port}", simulate=False, read_only=False, options={})
    try:
        drv = server.driver
        assert wait_until(lambda: drv.store.counts()["results"] == 1)
        assert drv.status()["mode"] == "tcp-client" and set(replies) == {ACK}
    finally:
        server.disconnect()
        server.configure(address="", simulate=True)
        analyzer_srv.close()


def test_option_validation():
    server.configure(address="tcp://127.0.0.1:1", simulate=False, options={"listen_port": "5000"})
    try:
        with pytest.raises(InstrumentConnectionError, match="not both"):
            _ = server.driver
        server.configure(address="", simulate=True, options={"redact_patient_info": "maybe"})
        with pytest.raises(InstrumentConnectionError, match="true or false"):
            _ = server.driver
    finally:
        server.configure(address="", simulate=True, options={})


# ------------------------------------------------------------------ MCP round trip


async def test_tools_via_mcp():
    async with simulated_client(server, address="", options={}) as client:
        info = (await client.call_tool("get_connection_info", {})).data
        assert info["simulated"] is True and info["connected"] is True

        async def status():
            return (await client.call_tool("get_connection_status", {})).structured_content

        end = time.monotonic() + 10
        while (await status())["messages"] < 2 and time.monotonic() < end:
            await asyncio.sleep(0.1)
        st = await status()
        assert st["messages"] == 2 and st["results"] == 34
        assert st["link"]["checksum_errors"] == 1 and st["redact_patient_info"] is True

        msgs = (await client.call_tool("list_received_messages", {})).structured_content
        assert [m["sample_ids"] for m in msgs["messages"]] == [["S26-000123"], ["S26-000124"]]

        cbc = (await client.call_tool("get_results", {"sample_id": "S26-000123"})).structured_content
        assert cbc["total_matches"] == 20 and cbc["redacted"] is True
        wbc = next(r for r in cbc["results"] if r["test_code"] == "WBC")
        assert wbc["value"] == "11.8" and wbc["abnormal_flags"] == ["H"] and wbc["comments"] == ["Left_Shift?"]

        k = (await client.call_tool("get_results", {"test_code": "K", "patient_id": "PAT-000982"})).structured_content
        assert k["returned"] == 1

        detail = (await client.call_tool("get_result_detail", {"result_id": k["results"][0]["result_id"]})).structured_content
        assert detail["result"]["patient_name"] == "REDACTED"
        assert "Roe" not in json.dumps(detail) and "555-0100" not in json.dumps(detail)
        assert detail["result"]["comments"][0].startswith("Hemolysis")
        assert detail["message"]["analyzer_components"][0] == "SIM-CHEM"

        with pytest.raises(Exception, match="ISO 8601"):
            await client.call_tool("get_results", {"since": "yesterday"})

        log = (await client.call_tool("get_command_log", {"limit": 500})).data
        assert any("NAK" in e["data"] for e in log)
        assert not any("Roe" in e["data"] for e in log)

        cleared = (await client.call_tool("clear_results", {})).structured_content
        assert cleared["cleared_results"] == 34
        assert (await client.call_tool("get_results", {})).structured_content["total_matches"] == 0


async def test_read_only_hides_clear_results():
    async with simulated_client(server, read_only=True, address="", options={}) as client:
        names = await tool_names(client)
        assert "get_results" in names and "get_connection_status" in names
        assert "clear_results" not in names
        assert "reconnect" in names  # safety tools stay available
