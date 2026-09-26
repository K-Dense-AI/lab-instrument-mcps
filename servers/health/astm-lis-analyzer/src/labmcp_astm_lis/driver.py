"""Receive-only LIS for clinical and laboratory analyzers: ASTM E1381 low-level protocol and
ASTM E1394 record parsing (CLSI LIS1-A / LIS2-A2). Research / lab-operations use only.

References (verified):

* Beckman Coulter, "UniCel DxI and Access 2 LIS Vendor Information", document C03112-AF
  (May 2026). Chapter 2 describes the ASTM E1381-95 low-level protocol: establishment
  (ENQ -> ACK/NAK), transfer (``<STX> FN text <ETB|ETX> C1 C2 <CR> <LF>``, max 240 text
  characters per frame, frame numbers 1..7,0,... modulo 8, a retransmitted frame keeps its
  number), checksum (sum of the bytes from the frame number up to and including ETB/ETX,
  modulo 256, two uppercase hex characters), acknowledgements (ACK, NAK, EOT), termination
  (EOT), time-outs (15 s sender, 30 s receiver) and restricted characters. Chapter 3 describes
  the ASTM E1394-97 records H, P, O, R, C, Q, L, the delimiters declared in the header
  (``H|\\^&``) and the field positions used below.
  https://www.beckmancoulter.com/download/file/wsr-228138/C03112AF?type=pdf
* ASTM E1381-02 / CLSI LIS1-A and ASTM E1394-97 / CLSI LIS2-A2 themselves are purchase-only;
  the record field order was cross-checked against the vendor document above and the open
  source python-astm record maps (https://github.com/kxepal/python-astm, astm/records.py).

This module never sends anything to the analyzer except the link-layer replies ACK and NAK:
there is no order download and no host-query response (receive-only by design).
"""

from __future__ import annotations

import json
import re
import select
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from labmcp import InstrumentConnectionError, InstrumentError, InstrumentTimeout, Transport

if TYPE_CHECKING:
    from labmcp import AuditLog

# --------------------------------------------------------------------------- E1381 constants

ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
EOT = b"\x04"
STX = b"\x02"
ETX = b"\x03"
ETB = b"\x17"
CR = b"\r"
LF = b"\n"
MAX_FRAME_TEXT = 240  # E1381: 247 characters per frame including 7 characters of overhead

_CONTROL_NAMES = {0x05: "ENQ", 0x06: "ACK", 0x15: "NAK", 0x04: "EOT", 0x02: "STX", 0x03: "ETX", 0x17: "ETB"}


def frame_checksum(body: bytes) -> str:
    """E1381 checksum of ``body`` = frame number + text + ETB/ETX: modulo-256 sum as 2 hex chars."""
    return f"{sum(body) & 0xFF:02X}"


class FrameError(ValueError):
    """A defective frame: the receiver must reply NAK."""


@dataclass
class Frame:
    number: int
    text: bytes
    final: bool  # True for an end frame (ETX), False for an intermediate frame (ETB)
    checksum: str


def parse_frame(raw: bytes) -> Frame:
    """Validate and split one frame ``<STX> FN text <ETB|ETX> C1 C2 <CR> <LF>``.

    Characters before the (last) STX are ignored, as E1381 requires. Raises FrameError with
    the reason if the frame is defective.
    """
    start = raw.rfind(STX)
    if start < 0:
        raise FrameError("frame does not contain STX")
    raw = raw[start:]
    end = max(raw.rfind(ETX), raw.rfind(ETB))
    if end < 2:
        raise FrameError("no ETX/ETB end-of-block character")
    fn = raw[1:2]
    if not fn.isdigit() or int(fn) > 7:
        raise FrameError(f"invalid frame number {fn!r} (must be 0-7)")
    trailer = raw[end + 1 :]
    received = trailer[:2].decode("ascii", "replace").upper()
    if len(received) < 2 or trailer[2:] not in (b"\r\n", b"\n", b""):
        raise FrameError(f"frame must end with two checksum characters and CR LF, got {trailer!r}")
    computed = frame_checksum(raw[1 : end + 1])
    if received != computed:
        raise FrameError(f"checksum mismatch: frame says {received}, computed {computed}")
    return Frame(int(fn), raw[2:end], raw[end : end + 1] == ETX, computed)


def build_frames(record: str, first_number: int, encoding: str = "latin-1") -> list[bytes]:
    """Encode one record (with its trailing CR) as E1381 frames, splitting at 240 characters.
    Used by the simulator and tests; the receiver never sends frames."""
    data = record.encode(encoding) + CR
    chunks = [data[i : i + MAX_FRAME_TEXT] for i in range(0, len(data), MAX_FRAME_TEXT)] or [CR]
    frames = []
    for i, chunk in enumerate(chunks):
        fn = str((first_number + i) % 8).encode()
        eob = ETX if i == len(chunks) - 1 else ETB
        body = fn + chunk + eob
        frames.append(STX + body + frame_checksum(body).encode() + CR + LF)
    return frames


# --------------------------------------------------------------------------- E1381 receiver


@dataclass
class LinkStats:
    sessions: int = 0
    frames_accepted: int = 0
    frames_rejected: int = 0
    checksum_errors: int = 0
    frame_number_errors: int = 0
    duplicate_frames: int = 0
    overlong_frames: int = 0
    sessions_aborted: int = 0
    bytes_ignored: int = 0
    last_rejection: str | None = None


class E1381Receiver:
    """Receiver side of the E1381 data link layer (pure state machine, no I/O).

    Feed it ENQ, frames and EOT; it returns the reply to send (ACK/NAK, or nothing) and
    delivers each complete record (text without the trailing CR) to ``on_record``.
    """

    def __init__(
        self,
        on_record: Callable[[str], None],
        on_session_end: Callable[[bool], None],
        log: Callable[[str], None] = lambda _msg: None,
        *,
        encoding: str = "latin-1",
        receive_timeout_s: float = 30.0,
    ) -> None:
        self.on_record = on_record
        self.on_session_end = on_session_end
        self.log = log
        self.encoding = encoding
        self.receive_timeout_s = receive_timeout_s
        self.state = "neutral"
        self.stats = LinkStats()
        self._last_fn: int | None = None
        self._buffer = bytearray()
        self._last_rx = time.monotonic()

    def on_enq(self) -> bytes:
        if self.state == "transfer":
            self.log("ENQ during a transfer: the analyzer restarted; discarding the incomplete message")
            self._abort()
        self.state = "transfer"
        self._last_fn = None
        self._buffer.clear()
        self._last_rx = time.monotonic()
        self.stats.sessions += 1
        self.log(f"ENQ -> ACK (session {self.stats.sessions})")
        return ACK

    def on_frame(self, raw: bytes) -> bytes:
        if self.state != "transfer":
            self.stats.bytes_ignored += len(raw)
            self.log("frame received without a preceding ENQ: ignored (is the analyzer set to ASTM E1381?)")
            return b""
        self._last_rx = time.monotonic()
        try:
            frame = parse_frame(raw)
        except FrameError as exc:
            return self._reject(str(exc), checksum="checksum" in str(exc))
        if self._last_fn is not None and frame.number == self._last_fn:
            # Our ACK was lost and the analyzer resent the frame we already accepted.
            self.stats.duplicate_frames += 1
            self.log(f"frame {frame.number} received twice (retransmission): ACK, duplicate ignored")
            return ACK
        expected = 1 if self._last_fn is None else (self._last_fn + 1) % 8
        if frame.number != expected:
            self.stats.frame_number_errors += 1
            return self._reject(f"frame number {frame.number}, expected {expected}")
        self._last_fn = frame.number
        self.stats.frames_accepted += 1
        if len(frame.text) > MAX_FRAME_TEXT:
            self.stats.overlong_frames += 1
        self._buffer += frame.text
        if frame.final:
            text = bytes(self._buffer).decode(self.encoding, "replace")
            self._buffer.clear()
            # One record per end frame per E1381; tolerate analyzers that pack several.
            for record in re.split(r"\r\n?|\n", text):
                if record:
                    self.on_record(record)
        return ACK

    def _reject(self, reason: str, checksum: bool = False) -> bytes:
        self.stats.frames_rejected += 1
        self.stats.checksum_errors += int(checksum)
        self.stats.last_rejection = reason
        self.log(f"defective frame -> NAK: {reason}")
        return NAK

    def on_eot(self) -> bytes:
        if self.state == "transfer":
            if self._buffer:
                self.log("EOT inside a multi-frame record: partial record discarded")
                self._buffer.clear()
            self.state = "neutral"
            self.log(f"EOT: session {self.stats.sessions} ended")
            self.on_session_end(True)
        return b""

    def on_incomplete_frame(self) -> bytes:
        """A frame started (STX) but its CR LF never arrived: defective, reply NAK."""
        if self.state != "transfer":
            return b""
        return self._reject("incomplete frame (no CR LF received)")

    def on_other(self, data: bytes) -> bytes:
        self.stats.bytes_ignored += len(data)
        return b""

    def check_timeout(self, now: float | None = None) -> None:
        """E1381: a receiver that gets no frame or EOT for 30 s discards the incomplete message."""
        now = time.monotonic() if now is None else now
        if self.state == "transfer" and now - self._last_rx > self.receive_timeout_s:
            self.log(f"no frame for {self.receive_timeout_s:g} s: incomplete message discarded")
            self._abort()

    def reset(self, reason: str) -> None:
        if self.state == "transfer":
            self.log(f"{reason}: incomplete message discarded")
            self._abort()

    def _abort(self) -> None:
        self.state = "neutral"
        self._buffer.clear()
        self.stats.sessions_aborted += 1
        self.on_session_end(False)


# --------------------------------------------------------------------------- E1394 records


@dataclass(frozen=True)
class Delimiters:
    field: str = "|"
    repeat: str = "\\"
    component: str = "^"
    escape: str = "&"

    @classmethod
    def from_header(cls, record: str) -> Delimiters:
        """The header declares the delimiters: ``H`` + field + repeat + component + escape."""
        if len(record) < 5 or record[0] != "H":
            raise ValueError(f"not a header record: {record[:10]!r}")
        chars = record[1:5]
        if len(set(chars)) != 4 or any(c.isalnum() or c in " \r\n" for c in chars):
            raise ValueError(f"invalid delimiter definition {chars!r}")
        return cls(*chars)


def _escape_len(text: str, i: int, esc: str) -> int:
    """Length of the escape sequence starting at ``text[i]`` (an escape character), or 0 if it
    is a literal escape character. Only three-character sequences (``&F&``, ``&|&``, ...) are
    recognised, so a stray ``&`` can never swallow the delimiters that follow it."""
    if i + 2 < len(text) and text[i + 2] == esc:
        return 3
    return 0


def split_escaped(text: str, sep: str, esc: str) -> list[str]:
    """Split ``text`` on ``sep`` without splitting inside escape sequences (kept verbatim)."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == esc:
            n = _escape_len(text, i, esc)
            if n:
                cur.append(text[i : i + n])
                i += n
                continue
        if ch == sep:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def unescape(text: str, d: Delimiters) -> str:
    """Decode escape sequences: ``&F& &S& &R& &E&`` (field, component, repeat, escape) and the
    vendor form ``&|&`` / ``&&&`` (delimiter character between escapes, e.g. Beckman).
    Anything else is kept verbatim."""
    if d.escape not in text:
        return text
    mnemonic = {"F": d.field, "S": d.component, "R": d.repeat, "E": d.escape}
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == d.escape and _escape_len(text, i, d.escape):
            body = text[i + 1]
            if body in mnemonic:
                out.append(mnemonic[body])
            elif body in (d.field, d.repeat, d.component, d.escape):
                out.append(body)
            else:
                out.append(text[i : i + 3])
            i += 3
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def split_fields(record: str, d: Delimiters) -> list[str]:
    """Raw (still escaped) fields. Index 0 is the record type (ASTM field 1)."""
    if record.startswith("H") and len(record) >= 5:
        # The delimiter definition (field 2, e.g. "\^&") must not be tokenised itself.
        rest = record[5:]
        fields = ["H", record[2:5]]
        if rest:
            fields += split_escaped(rest[1:] if rest[0] == d.field else rest, d.field, d.escape)
        return fields
    return split_escaped(record, d.field, d.escape)


def fget(fields: list[str], n: int) -> str:
    """ASTM field ``n`` (1-based, field 1 = record type); '' if absent."""
    return fields[n - 1] if 0 < n <= len(fields) else ""


def repeats(value: str, d: Delimiters) -> list[list[str]]:
    return [
        [unescape(c, d) for c in split_escaped(rep, d.component, d.escape)]
        for rep in split_escaped(value, d.repeat, d.escape)
    ]


def components(value: str, d: Delimiters) -> list[str]:
    """Decoded components of the first repeat of a field."""
    return repeats(value, d)[0]


def text_of(value: str, d: Delimiters) -> str:
    """Decoded field as display text (components joined with the component delimiter)."""
    return d.component.join(components(value, d)).strip()


def astm_datetime(value: str) -> str | None:
    """ASTM date/time ``YYYYMMDD[HHMM[SS]]`` (analyzer local time) -> ISO 8601, or None."""
    v = value.strip()
    fmt = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 8: "%Y%m%d"}.get(len(v))
    if not fmt:
        return None
    try:
        return datetime.strptime(v, fmt).isoformat()
    except ValueError:
        return None


# Patient record fields masked by redact_patient_info (E1394 P record field numbers).
PATIENT_REDACTED_FIELDS = {6: "patient name", 7: "mother's maiden name", 8: "birth date", 11: "address", 13: "telephone"}
REDACTED = "REDACTED"


def redact_patient_record(record: str, d: Delimiters) -> str:
    """Replace the identifying P-record fields (name, maiden name, DOB, address, phone)."""
    fields = split_escaped(record, d.field, d.escape)
    for n in PATIENT_REDACTED_FIELDS:
        if n <= len(fields) and fields[n - 1]:
            fields[n - 1] = REDACTED
    return d.field.join(fields)


# Result abnormal flags defined by CLSI LIS2-A2 (ASTM E1394) section 9.7: L H LL HH < > N A U D B W
# (the first seven are also documented in the Beckman C03112-AF result record description).
# Other codes are passed through undecoded: their meaning is analyzer-specific.
ABNORMAL_FLAG_MEANINGS = {
    "N": "normal",
    "L": "below the low normal (reference) limit",
    "H": "above the high normal (reference) limit",
    "LL": "below the low panic (critical) limit",
    "HH": "above the high panic (critical) limit",
    "<": "below the measuring range / low off-scale",
    ">": "above the measuring range / high off-scale",
    "A": "abnormal",
    "U": "significant change up",
    "D": "significant change down",
    "B": "better (direction not relevant or not defined)",
    "W": "worse (direction not relevant or not defined)",
}
RESULT_STATUS_MEANINGS = {"F": "final result", "X": "result cannot be generated (order not honoured or fatally flagged)"}

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


@dataclass
class ResultEntry:
    result_id: str
    message_id: int
    received_at: str
    analyzer: str
    sample_id: str
    specimen_id: str
    instrument_specimen_id: str
    patient_id: str
    laboratory_patient_id: str
    patient_name: str | None
    patient_birth_date: str | None
    patient_sex: str | None
    test_code: str
    test_name: str | None
    universal_test_id: list[str]
    value: str
    numeric_value: float | None
    interpretation: str | None
    units: str
    reference_range: str
    abnormal_flags: list[str]
    abnormal_flag_meanings: list[str]
    nature_of_abnormality: str
    result_status: str
    result_status_meaning: str | None
    operator: str
    started_at: str | None
    completed_at: str | None
    instrument: str
    priority: str
    specimen_type: str
    comments: list[str]
    order_comments: list[str]
    sequence: int
    record: str
    order_record: str
    patient_record: str


@dataclass
class ReceivedMessage:
    message_id: int
    received_at: str
    source: str
    complete: bool
    analyzer: str
    analyzer_components: list[str]
    receiver_id: str
    processing_id: str
    version: str
    header_timestamp: str | None
    termination_code: str
    kind: str  # "results", "query", "other"
    records: list[str]
    results: list[ResultEntry]
    queries: list[str]
    comments: list[str]
    warnings: list[str]
    redacted: bool


def _comment_text(fields: list[str], d: Delimiters) -> str:
    parts = [p for rep in repeats(fget(fields, 4), d) for p in rep if p.strip()]
    return " ".join(p.strip() for p in parts)


def parse_message(
    records: list[str],
    *,
    message_id: int,
    received_at: str,
    source: str,
    complete: bool,
    redacted: bool,
) -> ReceivedMessage:
    """Parse one E1394 message (H ... L) into structured results. Never raises: problems are
    reported in ``warnings``."""
    d = Delimiters()
    warnings: list[str] = []
    hf: list[str] = []
    patient: list[str] = []
    patient_raw = ""
    order: list[str] | None = None
    order_raw = ""
    order_comments: list[str] = []
    last: Any = None
    results: list[ResultEntry] = []
    queries: list[str] = []
    comments: list[str] = []
    termination = ""
    for rec in records:
        rtype = rec[:1]
        if rtype == "H":
            try:
                d = Delimiters.from_header(rec)
            except ValueError as exc:
                warnings.append(f"{exc}; using the default delimiters | \\ ^ &")
                d = Delimiters()
            hf = split_fields(rec, d)
            last = "header"
            continue
        fields = split_fields(rec, d)
        if rtype == "P":
            patient, patient_raw = fields, rec
            order, order_raw, order_comments = None, "", []
            last = comments  # patient-level comments are reported with the message
        elif rtype == "O":
            order, order_raw, order_comments = fields, rec, []
            last = order_comments
        elif rtype == "R":
            if order is None:
                warnings.append("result record without a preceding order record")
                order, order_raw, order_comments = [], "", []
            entry = _result_entry(fields, rec, order, order_raw, order_comments, patient, patient_raw,
                                  hf, d, message_id, received_at, len(results) + 1)
            results.append(entry)
            last = entry
        elif rtype == "C":
            text = _comment_text(fields, d)
            if isinstance(last, ResultEntry):
                last.comments.append(text)
            elif isinstance(last, list):
                last.append(text)
            else:
                comments.append(text)
        elif rtype == "Q":
            start = components(fget(fields, 3), d)
            ids = ", ".join(c for c in start if c) or "(all)"
            queries.append(f"host query for {ids}, tests {text_of(fget(fields, 5), d) or 'ALL'}")
        elif rtype == "L":
            termination = fget(fields, 3)
        elif rtype in ("M", "S"):
            pass  # manufacturer / scientific records: kept raw only
        else:
            warnings.append(f"unknown record type {rtype!r}")
    if not hf:
        warnings.append("message has no header record")
    if not complete:
        warnings.append("message ended without a terminator (L) record")
    if queries:
        warnings.append("the analyzer sent a host query; this server is receive-only and does not answer it")
    sender = components(fget(hf, 5), d) if hf else []
    kind = "results" if results else "query" if queries else "other"
    return ReceivedMessage(
        message_id=message_id,
        received_at=received_at,
        source=source,
        complete=complete,
        analyzer=" ".join(c for c in sender if c),
        analyzer_components=sender,
        receiver_id=text_of(fget(hf, 10), d),
        processing_id=text_of(fget(hf, 12), d),
        version=text_of(fget(hf, 13), d),
        header_timestamp=astm_datetime(fget(hf, 14)),
        termination_code=termination,
        kind=kind,
        records=list(records),
        results=results,
        queries=queries,
        comments=comments,
        warnings=warnings,
        redacted=redacted,
    )


def _first_nonempty(values: list[str]) -> str:
    return next((v.strip() for v in values if v.strip()), "")


def _result_entry(
    fields: list[str],
    raw: str,
    order: list[str],
    order_raw: str,
    order_comments: list[str],
    patient: list[str],
    patient_raw: str,
    hf: list[str],
    d: Delimiters,
    message_id: int,
    received_at: str,
    index: int,
) -> ResultEntry:
    test = components(fget(fields, 3), d)
    # E1394 Universal Test ID: ID ^ name ^ type ^ manufacturer's local code [^ ...]. Analyzers
    # put their test code in the 4th component (Beckman ^^^TSH), the 5th (Sysmex ^^^^WBC) or
    # only the 1st; take the first non-empty local component, else the universal ID.
    test_code = _first_nonempty(test[3:]) or _first_nonempty(test[:1])
    value_parts = components(fget(fields, 4), d)
    value = value_parts[0].strip() if value_parts else ""
    ref = components(fget(fields, 6), d)
    flags = [c for rep in repeats(fget(fields, 7), d) for c in rep if c.strip()]
    status = text_of(fget(fields, 9), d)
    specimen = text_of(fget(order, 3), d) if order else ""
    inst_specimen = components(fget(order, 4), d) if order else []
    name = text_of(fget(patient, 6), d) if patient else ""
    dob = fget(patient, 8).strip() if patient else ""
    sender = components(fget(hf, 5), d) if hf else []
    return ResultEntry(
        result_id=f"{message_id}-{index}",
        message_id=message_id,
        received_at=received_at,
        analyzer=" ".join(c for c in sender if c),
        sample_id=specimen.split(d.component)[0].strip() or _first_nonempty(inst_specimen),
        specimen_id=specimen,
        instrument_specimen_id=d.component.join(inst_specimen).strip(),
        patient_id=text_of(fget(patient, 3), d) if patient else "",
        laboratory_patient_id=text_of(fget(patient, 4), d) if patient else "",
        patient_name=name or None,
        patient_birth_date=(astm_datetime(dob) or dob) if dob and dob != REDACTED else (dob or None),
        patient_sex=text_of(fget(patient, 9), d) or None if patient else None,
        test_code=test_code,
        test_name=(test[1].strip() or None) if len(test) > 1 else None,
        universal_test_id=test,
        value=value,
        numeric_value=float(value) if _NUMBER_RE.match(value) else None,
        interpretation=d.component.join(value_parts[1:]).strip() or None,
        units=text_of(fget(fields, 5), d),
        reference_range=" ".join(p.strip() for p in ref if p.strip()),
        abnormal_flags=flags,
        abnormal_flag_meanings=[ABNORMAL_FLAG_MEANINGS.get(f, "analyzer-specific code") for f in flags],
        nature_of_abnormality=text_of(fget(fields, 8), d),
        result_status=status,
        result_status_meaning=RESULT_STATUS_MEANINGS.get(status),
        operator=text_of(fget(fields, 11), d),
        started_at=astm_datetime(fget(fields, 12)),
        completed_at=astm_datetime(fget(fields, 13)),
        instrument=text_of(fget(fields, 14), d),
        priority=text_of(fget(order, 6), d) if order else "",
        specimen_type=(components(fget(order, 16), d)[0].strip() if order else ""),
        comments=[],
        order_comments=order_comments,
        sequence=int(fget(fields, 2)) if fget(fields, 2).strip().isdigit() else index,
        record=raw,
        order_record=order_raw,
        patient_record=patient_raw,
    )


# --------------------------------------------------------------------------- store


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_since(value: str) -> datetime:
    """ISO 8601 date/time; naive values are taken as UTC."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"`since` must be an ISO 8601 date/time such as 2026-09-25T08:00:00Z, got {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ResultStore:
    """Thread-safe in-memory store of received messages, optionally appended to a JSONL file."""

    def __init__(self, *, max_messages: int = 5000, store_path: str | None = None, redacted: bool = True) -> None:
        self.max_messages = max_messages
        self.store_path = Path(store_path).expanduser() if store_path else None
        self.redacted = redacted
        self._messages: list[ReceivedMessage] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self.load_errors = 0
        if self.store_path:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            if self.store_path.exists():
                self._load()

    def _load(self) -> None:
        assert self.store_path is not None
        for line in self.store_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                msg = parse_message(
                    row["records"],
                    message_id=int(row["message_id"]),
                    received_at=row["received_at"],
                    source=row.get("source", ""),
                    complete=bool(row.get("complete", True)),
                    redacted=bool(row.get("redacted", False)),
                )
            except (ValueError, KeyError, TypeError):
                self.load_errors += 1
                continue
            self._append(msg)
            self._next_id = max(self._next_id, msg.message_id + 1)

    def _append(self, msg: ReceivedMessage) -> None:
        self._messages.append(msg)
        if len(self._messages) > self.max_messages:
            del self._messages[: len(self._messages) - self.max_messages]

    def add(self, records: list[str], *, source: str, complete: bool) -> ReceivedMessage:
        with self._lock:
            msg = parse_message(
                records,
                message_id=self._next_id,
                received_at=utc_now(),
                source=source,
                complete=complete,
                redacted=self.redacted,
            )
            self._next_id += 1
            self._append(msg)
            if self.store_path:
                row = {
                    "message_id": msg.message_id,
                    "received_at": msg.received_at,
                    "source": msg.source,
                    "complete": msg.complete,
                    "redacted": msg.redacted,
                    "records": msg.records,
                }
                with self.store_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            return msg

    def messages(self, limit: int = 50, since: datetime | None = None) -> tuple[list[ReceivedMessage], int]:
        with self._lock:
            msgs = [m for m in self._messages if since is None or parse_since(m.received_at) >= since]
        return msgs[-limit:], len(msgs)

    def results(
        self,
        *,
        sample_id: str | None = None,
        patient_id: str | None = None,
        test_code: str | None = None,
        since: datetime | None = None,
        abnormal_only: bool = False,
        limit: int = 100,
    ) -> tuple[list[ResultEntry], int]:
        def keep(r: ResultEntry) -> bool:
            if sample_id and r.sample_id.lower() != sample_id.strip().lower():
                return False
            if patient_id and patient_id.strip() not in (r.patient_id, r.laboratory_patient_id):
                return False
            if test_code and r.test_code.lower() != test_code.strip().lower():
                return False
            if since and parse_since(r.received_at) < since:
                return False
            return not (abnormal_only and not any(f != "N" for f in r.abnormal_flags))

        with self._lock:
            found = [r for m in self._messages for r in m.results if keep(r)]
        return found[-limit:], len(found)

    def result(self, result_id: str) -> tuple[ResultEntry, ReceivedMessage] | None:
        with self._lock:
            for m in self._messages:
                for r in m.results:
                    if r.result_id == result_id:
                        return r, m
        return None

    def clear(self) -> tuple[int, int]:
        with self._lock:
            n_msg = len(self._messages)
            n_res = sum(len(m.results) for m in self._messages)
            self._messages.clear()
        return n_msg, n_res

    def counts(self) -> dict[str, Any]:
        with self._lock:
            last = self._messages[-1] if self._messages else None
            return {
                "messages": len(self._messages),
                "results": sum(len(m.results) for m in self._messages),
                "last_message_at": last.received_at if last else None,
                "last_analyzer": last.analyzer if last else None,
            }


def to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)


# --------------------------------------------------------------------------- TCP listener


class ListenTransport(Transport):
    """TCP server transport: the analyzer connects to us (LIS as TCP server).

    One analyzer connection at a time; a new connection replaces the previous one (analyzers
    reconnect after a reboot without closing the old socket). ``allow_from`` restricts which
    IP addresses may connect.
    """

    def __init__(self, host: str, port: int, *, allow_from: set[str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        try:
            self._server = socket.create_server((host, port))
        except OSError as exc:
            raise InstrumentConnectionError(
                f"Could not listen on {host}:{port}: {exc}. Is another program (or another copy of "
                "this server) using the port? Ports below 1024 need administrator rights."
            ) from exc
        self._server.setblocking(False)
        self.host = host
        self.port = self._server.getsockname()[1]
        self.description = f"tcp-listen://{host}:{self.port}"
        self.allow_from = allow_from or set()
        self.peer: str | None = None
        self.generation = 0  # increments on every connect / disconnect
        self.rejected_connections = 0
        self._client: socket.socket | None = None

    def _accept(self) -> None:
        try:
            conn, addr = self._server.accept()
        except (BlockingIOError, InterruptedError):
            return
        if self.allow_from and addr[0] not in self.allow_from:
            self.rejected_connections += 1
            conn.close()
            return
        self._drop()
        conn.setblocking(False)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._client = conn
        self.peer = f"{addr[0]}:{addr[1]}"
        self.generation += 1

    def _drop(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self.peer = None
                self.generation += 1

    def _read(self, max_bytes: int, timeout: float) -> bytes:
        socks = [self._server] + ([self._client] if self._client is not None else [])
        ready, _, _ = select.select(socks, [], [], max(timeout, 0))
        if self._server in ready:
            self._accept()
            return b""
        if self._client is not None and self._client in ready:
            try:
                data = self._client.recv(max_bytes)
            except OSError:
                data = b""
            if not data:
                self._drop()
            return data
        return b""

    def _write(self, data: bytes) -> None:
        if self._client is None:
            return  # the analyzer went away; there is nobody to acknowledge
        try:
            self._client.setblocking(True)
            self._client.sendall(data)
            self._client.setblocking(False)
        except OSError:
            self._drop()

    def _flush_input(self) -> None:
        pass

    def _close(self) -> None:
        self._drop()
        self._server.close()


# --------------------------------------------------------------------------- receiver thread


class MessageAssembler:
    """Groups records into messages (H ... L) and hands them to the store."""

    def __init__(self, store: ResultStore, source: Callable[[], str], log: Callable[[str], None], redact: bool) -> None:
        self.store = store
        self.source = source
        self.log = log
        self.redact = redact
        self._records: list[str] = []
        self._delims = Delimiters()

    def add(self, record: str) -> None:
        rtype = record[:1]
        if rtype == "H":
            if self._records:
                self._finish(False)
            try:
                self._delims = Delimiters.from_header(record)
            except ValueError:
                self._delims = Delimiters()
        elif not self._records:
            self.log(f"{rtype!r} record outside a message (no header record): ignored")
            return
        if rtype == "P" and self.redact:
            record = redact_patient_record(record, self._delims)
        self._records.append(record)
        self.log(f"record {record[:200]}")
        if rtype == "L":
            self._finish(True)

    def session_end(self, normal: bool) -> None:
        if not self._records:
            return
        if normal:
            self._finish(False)  # EOT before the terminator record: keep, flagged incomplete
        else:
            self.log("incomplete message discarded (link aborted)")
            self._records = []

    def _finish(self, complete: bool) -> None:
        records, self._records = self._records, []
        try:
            msg = self.store.add(records, source=self.source(), complete=complete)
        except Exception as exc:  # a parser bug must never stop the link from ACKing
            self.log(f"could not store message ({type(exc).__name__}: {exc}); records: {records!r}"[:500])
            return
        self.log(
            f"message {msg.message_id} stored: {len(msg.results)} results"
            + (f", sample(s) {', '.join(sorted({r.sample_id for r in msg.results}))}" if msg.results else "")
            + ("" if complete else " (incomplete)")
        )


class ASTMReceiver:
    """Background receiver: reads the link, replies ACK/NAK, stores parsed messages."""

    def __init__(
        self,
        open_link: Callable[[], Transport],
        *,
        mode: str,
        where: str,
        store: ResultStore,
        audit: AuditLog | None = None,
        redact: bool = True,
        encoding: str = "latin-1",
        poll_s: float = 0.2,
        receive_timeout_s: float = 30.0,
        frame_timeout_s: float = 10.0,
        on_close: list[Callable[[], None]] | None = None,
    ) -> None:
        self._open_link = open_link
        self.mode = mode
        self.where = where
        self.store = store
        self.audit = audit
        self.redact = redact
        self.poll_s = poll_s
        self.frame_timeout_s = frame_timeout_s
        self._on_close = on_close or []
        self.last_error: str | None = None
        self.last_activity_at: str | None = None
        self.started_at = utc_now()
        # The first open is synchronous so configuration errors (bad port, port in use,
        # unreachable analyzer) are reported to the caller; later drops reconnect in background.
        self.link: Transport | None = open_link()
        self.link.audit = None  # traffic is logged at record level instead (with redaction)
        self._generation = getattr(self.link, "generation", 0)
        self._assembler = MessageAssembler(store, self._source, self._event, redact)
        self.protocol = E1381Receiver(
            self._assembler.add, self._assembler.session_end, self._event,
            encoding=encoding, receive_timeout_s=receive_timeout_s,
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="astm-receiver", daemon=True)
        self._thread.start()

    # -- helpers ----------------------------------------------------------------

    def _source(self) -> str:
        peer = getattr(self.link, "peer", None)
        return peer or self.where

    def _event(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, self.where)

    # -- loop -------------------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            link = self.link
            if link is None:
                try:
                    link = self.link = self._open_link()
                    link.audit = None
                    self._event(f"reconnected to {self.where}")
                    self.last_error = None
                    backoff = 1.0
                except InstrumentError as exc:
                    self.last_error = str(exc)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            try:
                self._step(link)
            except InstrumentConnectionError as exc:
                if self._stop.is_set():
                    return
                self.last_error = str(exc)
                self._event(f"link lost: {exc}")
                self.protocol.reset("link lost")
                link.close()
                self.link = None
            except Exception as exc:  # never let the receiver thread die silently
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._event(f"receiver error: {self.last_error}")
                self._stop.wait(0.5)

    def _sync_generation(self, link: Transport) -> None:
        """A new or lost analyzer connection (TCP listener) ends any session in progress."""
        generation = getattr(link, "generation", 0)
        if generation != self._generation:
            self._generation = generation
            self.protocol.reset("analyzer connection changed")
            peer = getattr(link, "peer", None)
            self._event(f"analyzer connected from {peer}" if peer else "analyzer disconnected")

    def _step(self, link: Transport) -> None:
        try:
            first = link.read_bytes(1, timeout=self.poll_s)
        except InstrumentTimeout:
            self._sync_generation(link)
            self.protocol.check_timeout()
            return
        # The byte just read belongs to the current connection: reset *before* handling it.
        self._sync_generation(link)
        self.last_activity_at = utc_now()
        if first == STX:
            try:
                rest = link.read_until(LF, timeout=self.frame_timeout_s)
            except InstrumentTimeout:
                link.flush_input()
                reply = self.protocol.on_incomplete_frame()
            else:
                reply = self.protocol.on_frame(STX + rest + LF)
        elif first == ENQ:
            reply = self.protocol.on_enq()
        elif first == EOT:
            reply = self.protocol.on_eot()
        else:
            reply = self.protocol.on_other(first)
        if reply:
            link.write_bytes(reply)

    # -- info -------------------------------------------------------------------

    def link_state(self) -> str:
        if self.link is None:
            return f"reconnecting ({self.last_error})"
        if isinstance(self.link, ListenTransport):
            return f"analyzer connected from {self.link.peer}" if self.link.peer else "listening, no analyzer connected"
        return "connected"

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": self.mode,
            "address": self.where,
            "link_state": self.link_state(),
            "session_state": "receiving" if self.protocol.state == "transfer" else "idle",
            "receiver_running": self._thread.is_alive(),
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "last_error": self.last_error,
            "redact_patient_info": self.redact,
            "store_path": str(self.store.store_path) if self.store.store_path else None,
            "link": asdict(self.protocol.stats),
            **self.store.counts(),
        }
        if isinstance(self.link, ListenTransport):
            info["rejected_connections"] = self.link.rejected_connections
            info["allow_from"] = sorted(self.link.allow_from)
        return info

    def identify(self) -> dict[str, Any]:
        counts = self.store.counts()
        return {
            "manufacturer": "any ASTM E1381/E1394 analyzer (receive-only LIS)",
            "mode": self.mode,
            "link": self.where,
            "link_state": self.link_state(),
            "last_analyzer": counts["last_analyzer"] or "no message received yet",
            "messages_received": counts["messages"],
        }

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        for fn in self._on_close:
            fn()
        if self.link is not None:
            self.link.close()
