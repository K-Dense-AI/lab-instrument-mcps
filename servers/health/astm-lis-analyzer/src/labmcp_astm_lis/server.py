"""MCP server that receives results from clinical / lab analyzers over ASTM E1381 / E1394.

Research / lab-operations use only. Not a medical device and not a validated LIS; not for
diagnosis or clinical decision-making. Receive-only: nothing is ever sent to the analyzer
except link-level ACK / NAK.
"""

from __future__ import annotations

import codecs
import logging
from dataclasses import asdict
from typing import Annotated, Any

from labmcp import (
    CONTROL,
    READ,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentServer,
    parse_address,
)
from pydantic import BaseModel, Field

from labmcp_astm_lis.driver import (
    PATIENT_REDACTED_FIELDS,
    ASTMReceiver,
    ListenTransport,
    ResultEntry,
    ResultStore,
    parse_since,
)
from labmcp_astm_lis.simulator import ASTMAnalyzerSimulator

log = logging.getLogger("labmcp.astm")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    low = value.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise InstrumentConnectionError(f"Option value {value!r} must be true or false.")


#: Received results, per configuration. The analyzer never re-sends a result it got an ACK for,
#: so the in-memory store must survive `reconnect` (and a failed reconnect) rather than be
#: replaced by an empty one.
_STORES: dict[tuple[Any, ...], ResultStore] = {}


def _store(ctx: ConnectContext, redact: bool) -> ResultStore:
    try:
        max_messages = int(ctx.option("max_messages", "5000") or 5000)
    except ValueError as exc:
        raise InstrumentConnectionError(f"max_messages must be a whole number: {exc}") from exc
    if max_messages < 1:
        raise InstrumentConnectionError(f"max_messages must be at least 1, got {max_messages}.")

    def make() -> ResultStore:
        return ResultStore(max_messages=max_messages, store_path=ctx.option("store_path") or None, redacted=redact)

    if ctx.simulate:  # the simulator re-sends its demo messages on every connect
        return make()
    key = (ctx.address, tuple(sorted(ctx.settings.options.items())))
    if key not in _STORES:
        _STORES[key] = make()
    return _STORES[key]


def connect(ctx: ConnectContext) -> ASTMReceiver:
    redact = _flag(ctx.option("redact_patient_info"), True)
    encoding = ctx.option("encoding", "latin-1") or "latin-1"
    try:
        codecs.lookup(encoding)  # an unknown codec would otherwise fail on every received message
    except LookupError as exc:
        raise InstrumentConnectionError(
            f"Unknown encoding {encoding!r} (--option encoding=...). Use e.g. latin-1, cp1252 or utf-8."
        ) from exc
    store = _store(ctx, redact)
    common: dict[str, Any] = {"store": store, "audit": ctx.audit, "redact": redact, "encoding": encoding}

    if ctx.simulate:
        link = ctx.open_transport(simulator=ASTMAnalyzerSimulator)
        sim = link.simulator  # type: ignore[attr-defined]
        receiver = ASTMReceiver(lambda: link, mode="simulated", where="simulated analyzer", on_close=[sim.stop], **common)
        sim.attach(link.push)  # type: ignore[attr-defined]
        return receiver

    port = ctx.option("listen_port")
    if port:
        if ctx.address:
            raise InstrumentConnectionError(
                "Use either --address (serial port or tcp://analyzer:port, the analyzer is the server) "
                "or --option listen_port=N (the analyzer connects to this computer), not both."
            )
        host = ctx.option("listen_host", "0.0.0.0") or "0.0.0.0"
        allow = {a for a in (ctx.option("allow_from") or "").replace(";", " ").split() if a}
        first = ListenTransport(host, int(port), allow_from=allow)
        links = iter([first])

        def open_listener() -> ListenTransport:
            # The first socket is bound up-front (so a busy port is reported immediately);
            # after a failure the listener is simply re-created.
            return next(links, None) or ListenTransport(host, int(port), allow_from=allow)

        return ASTMReceiver(open_listener, mode="tcp-listen", where=first.description, **common)

    address = ctx.require_address()
    kind = parse_address(address).kind
    if kind not in ("serial", "tcp"):
        raise InstrumentConnectionError(
            f"ASTM analyzers are connected over a serial port or TCP; {address!r} is a {kind} address."
        )

    def open_link() -> Any:
        # E1381 serial framing is fixed at 8 data bits, no parity, 1 stop bit; 9600 baud is the
        # usual default (override in the address, e.g. serial:///dev/ttyUSB0?baudrate=19200).
        return ctx.open_transport(baudrate=9600, bytesize=8, parity="N", stopbits=1, timeout=5.0)

    mode = "serial" if kind == "serial" else "tcp-client"
    return ASTMReceiver(open_link, mode=mode, where=address, **common)


server = InstrumentServer(
    "ASTM LIS Analyzer Receiver (E1381/E1394)",
    connect=connect,
    package="labmcp-astm-lis",
    # Start receiving at launch so the analyzer is ACKed (and the listen port is open) immediately.
    connect_on_start=True,
    instructions="""
Receive-only laboratory information system (LIS) endpoint for clinical and lab analyzers
(hematology, chemistry, immunoassay, urinalysis, coagulation) that speak ASTM E1381/E1394
(CLSI LIS1-A/LIS2-A2). RESEARCH / LAB-OPERATIONS USE ONLY: not a validated LIS or medical device.
Never interpret results as a diagnosis or give treatment advice; results must be released through
the laboratory's validated process.
- Analyzers push results; this server acknowledges frames and stores what arrives. It never
  sends orders and does not answer host queries.
- Start with `get_connection_status`: link state, messages received and frame NAK counts. If
  nothing arrives, the analyzer's host/LIS settings (protocol, baud rate, IP/port) need checking.
- Use `get_results` filtered by sample_id, patient_id, test_code or since; `get_result_detail`
  shows the full record, comments and order context.
- Values are passed through as sent (e.g. "<0.1", ">500"); always quote units, reference range and
  flags with a value. Flag and status meanings other than the standard codes are analyzer-specific.
- Patient name, birth date, address and phone are redacted by default. Refer to samples and
  patients by their IDs only; do not try to re-identify anyone.
""",
    address_help="""\
  serial:///dev/ttyUSB0               analyzer on RS-232 (default 9600 baud, 8N1)
  serial://COM3?baudrate=19200        Windows, non-default baud rate
  tcp://192.168.1.80:5000             analyzer is the TCP server (LIS connects to it)
  --option listen_port=5000           analyzer is the TCP client (it connects to this computer)""",
    option_help={
        "listen_port": "TCP port to listen on when the analyzer connects to this computer",
        "listen_host": "interface to listen on (default 0.0.0.0 = all)",
        "allow_from": "only accept analyzer connections from these IPs (separate with ';' or spaces)",
        "store_path": "append every received message to this JSONL file (reloaded at start)",
        "redact_patient_info": "true (default) masks patient name, maiden name, DOB, address, phone",
        "encoding": "character encoding of the analyzer's text (default latin-1)",
        "max_messages": "messages kept in memory (default 5000; oldest dropped first)",
    },
)
mcp = server.mcp

# ----------------------------------------------------------------------------- models


class LinkCounters(BaseModel):
    sessions: int
    frames_accepted: int
    frames_rejected: int = Field(description="Frames answered with NAK")
    checksum_errors: int
    frame_number_errors: int
    duplicate_frames: int
    overlong_frames: int = Field(description="Frames with more than 240 text characters (accepted)")
    sessions_aborted: int
    bytes_ignored: int = Field(description="Bytes received outside a session (not preceded by ENQ)")
    last_rejection: str | None


class ConnectionStatus(BaseModel):
    mode: str = Field(description="serial, tcp-client, tcp-listen or simulated")
    address: str
    link_state: str
    session_state: str = Field(description="receiving (inside ENQ ... EOT) or idle")
    receiver_running: bool
    started_at: str
    last_activity_at: str | None
    last_error: str | None
    redact_patient_info: bool
    store_path: str | None
    messages: int
    results: int
    last_message_at: str | None
    last_analyzer: str | None
    store_write_errors: int = Field(0, description="Messages that could not be appended to store_path (kept in memory)")
    last_store_error: str | None = None
    link: LinkCounters
    rejected_connections: int | None = None
    allow_from: list[str] | None = None


class MessageSummary(BaseModel):
    message_id: int
    received_at: str
    source: str
    analyzer: str
    kind: str = Field(description="results, query (host query, not answered) or other")
    complete: bool = Field(description="False if the message had no terminator (L) record")
    records: int
    results: int
    sample_ids: list[str]
    warnings: list[str]


class MessageList(BaseModel):
    messages: list[MessageSummary]
    total: int
    returned: int


class ResultSummary(BaseModel):
    result_id: str = Field(description="Pass to get_result_detail")
    received_at: str
    analyzer: str
    sample_id: str
    patient_id: str
    test_code: str
    value: str = Field(description="Value exactly as sent by the analyzer")
    numeric_value: float | None
    units: str
    reference_range: str
    abnormal_flags: list[str]
    result_status: str
    completed_at: str | None = Field(description="Analyzer local time")
    comments: list[str]


class ResultList(BaseModel):
    results: list[ResultSummary]
    total_matches: int
    returned: int
    truncated: bool
    redacted: bool


class ResultDetail(BaseModel):
    result: dict[str, Any]
    message: dict[str, Any] = Field(description="Header information of the message the result came in")
    redacted_fields: list[str]


class ClearReport(BaseModel):
    cleared_messages: int
    cleared_results: int
    note: str


def _summary(r: ResultEntry) -> ResultSummary:
    return ResultSummary(
        result_id=r.result_id,
        received_at=r.received_at,
        analyzer=r.analyzer,
        sample_id=r.sample_id,
        patient_id=r.patient_id,
        test_code=r.test_code,
        value=r.value,
        numeric_value=r.numeric_value,
        units=r.units,
        reference_range=r.reference_range,
        abnormal_flags=r.abnormal_flags,
        result_status=r.result_status,
        completed_at=r.completed_at,
        comments=r.comments,
    )


def _since(value: str | None) -> Any:
    if not value:
        return None
    try:
        return parse_since(value)
    except ValueError as exc:
        raise InstrumentError(str(exc)) from exc


# ----------------------------------------------------------------------------- tools


@mcp.tool(**READ)
def get_connection_status() -> ConnectionStatus:
    """Report the analyzer link state, the messages and results received, and link-layer error
    counters.

    Shows the link (serial / TCP client / TCP listener), whether an analyzer is connected, whether
    a transmission is in progress, and NAKed frames, checksum errors and retransmissions."""
    return ConnectionStatus(**server.driver.status())


@mcp.tool(**READ)
def list_received_messages(
    limit: Annotated[int, Field(ge=1, le=500, description="Most recent messages to return")] = 20,
    since: Annotated[str | None, Field(description="Only messages received at/after this ISO 8601 time (UTC if no zone)")] = None,
) -> MessageList:
    """List the most recent ASTM messages received (newest last).

    Each entry shows the analyzer, number of records and results, sample IDs, whether the
    message was complete, and any parser warnings."""
    msgs, total = server.driver.store.messages(limit, _since(since))
    return MessageList(
        messages=[
            MessageSummary(
                message_id=m.message_id,
                received_at=m.received_at,
                source=m.source,
                analyzer=m.analyzer,
                kind=m.kind,
                complete=m.complete,
                records=len(m.records),
                results=len(m.results),
                sample_ids=sorted({r.sample_id for r in m.results if r.sample_id}),
                warnings=m.warnings,
            )
            for m in msgs
        ],
        total=total,
        returned=len(msgs),
    )


@mcp.tool(**READ)
def get_results(
    sample_id: Annotated[str | None, Field(max_length=64, description="Sample / specimen ID (case-insensitive exact match)")] = None,
    patient_id: Annotated[str | None, Field(max_length=64, description="Patient ID as sent by the analyzer (exact match)")] = None,
    test_code: Annotated[str | None, Field(max_length=32, description="Analyzer test code, e.g. WBC, GLU, TSH (case-insensitive)")] = None,
    since: Annotated[str | None, Field(description="Only results received at/after this ISO 8601 time (UTC if no zone)")] = None,
    abnormal_only: Annotated[bool, Field(description="Only results with an abnormal flag other than N")] = False,
    limit: Annotated[int, Field(ge=1, le=2000, description="Most recent matching results to return")] = 200,
) -> ResultList:
    """Return received results, filtered by sample ID, patient ID, test code, time received or
    abnormal flag (newest last).

    Each result has its value exactly as transmitted, units, reference range, flags, status and
    any comment records that followed it."""
    found, total = server.driver.store.results(
        sample_id=sample_id,
        patient_id=patient_id,
        test_code=test_code,
        since=_since(since),
        abnormal_only=abnormal_only,
        limit=limit,
    )
    return ResultList(
        results=[_summary(r) for r in found],
        total_matches=total,
        returned=len(found),
        truncated=total > len(found),
        redacted=server.driver.redact,
    )


@mcp.tool(**READ)
def get_result_detail(
    result_id: Annotated[str, Field(max_length=32, description="result_id from get_results, e.g. '3-12'")],
) -> ResultDetail:
    """Show every decoded field of one result, with its comments, order and patient context, raw
    records and message header.

    Includes universal test ID components, operator, start/completion times, instrument,
    priority and specimen type. Patient identifiers stay redacted unless disabled at launch."""
    found = server.driver.store.result(result_id.strip())
    if found is None:
        raise InstrumentError(f"No result with id {result_id!r}. Use get_results to list result ids.")
    r, m = found
    message = {
        "message_id": m.message_id,
        "received_at": m.received_at,
        "source": m.source,
        "analyzer": m.analyzer,
        "analyzer_components": m.analyzer_components,
        "receiver_id": m.receiver_id,
        "processing_id": m.processing_id,
        "version": m.version,
        "header_timestamp": m.header_timestamp,
        "termination_code": m.termination_code,
        "complete": m.complete,
        "warnings": m.warnings,
    }
    return ResultDetail(
        result=asdict(r),
        message=message,
        redacted_fields=list(PATIENT_REDACTED_FIELDS.values()) if m.redacted else [],
    )


@mcp.tool(**CONTROL)
def clear_results() -> ClearReport:
    """Delete all received messages and results from this server's memory (e.g. between runs).

    The optional JSONL store file is not modified. Nothing is sent to the analyzer."""
    n_msg, n_res = server.driver.store.clear()
    return ClearReport(
        cleared_messages=n_msg,
        cleared_results=n_res,
        note="In-memory results cleared; the store_path file (if any) is unchanged.",
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
