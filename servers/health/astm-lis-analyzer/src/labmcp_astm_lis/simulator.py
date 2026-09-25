"""Wire-level simulator of an analyzer transmitting results over ASTM E1381 / E1394.

The simulated analyzer is the *sender*: it opens a session with ENQ, sends every record as
one or more frames (``<STX> FN text <ETB|ETX> C1 C2 <CR> <LF>``, frame numbers modulo 8,
records longer than 240 characters split with ETB), waits for ACK/NAK after each frame,
retransmits a NAKed frame with the same frame number (giving up after 6 attempts), and ends
with EOT. The receiver under test only ever answers ACK or NAK.

Two messages are sent after connection, 1.5 s apart:

1. a hematology CBC with 5-part differential for sample S26-000123 (flags H/L on WBC, PLT,
   NEUT%, LYMPH%, NEUT#; suspect-flag comments), whose long order record needs two frames.
   One frame is corrupted in transit on its first transmission (a flipped bit, so the
   checksum no longer matches) and must be NAKed and retransmitted;
2. a comprehensive metabolic panel for sample S26-000124 (high glucose and potassium with
   a hemolysis comment), whose patient record carries name, birth date, address and phone
   so that redaction can be seen working.

Values are fixed and internally consistent (MCV = HCT/RBC, differential sums to 100 %).
All patient data is fictitious.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from labmcp import ByteSimulator

ENQ, ACK, NAK, EOT, STX, ETX, ETB, CR, LF = b"\x05", b"\x06", b"\x15", b"\x04", b"\x02", b"\x03", b"\x17", b"\r", b"\n"

# (code, value, units, low, high) as the analyzer prints them; flags derive from the interval
CBC = [
    ("WBC", "11.8", "10*3/uL", "4.0", "10.0"),
    ("RBC", "4.52", "10*6/uL", "3.80", "5.20"),
    ("HGB", "13.1", "g/dL", "12.0", "16.0"),
    ("HCT", "39.8", "%", "36.0", "46.0"),
    ("MCV", "88.1", "fL", "80.0", "100.0"),
    ("MCH", "29.0", "pg", "27.0", "33.0"),
    ("MCHC", "32.9", "g/dL", "32.0", "36.0"),
    ("RDW-CV", "13.2", "%", "11.5", "14.5"),
    ("PLT", "142", "10*3/uL", "150", "400"),
    ("MPV", "10.1", "fL", "7.4", "10.4"),
    ("NEUT%", "78.2", "%", "40.0", "75.0"),
    ("LYMPH%", "14.1", "%", "20.0", "45.0"),
    ("MONO%", "6.0", "%", "2.0", "10.0"),
    ("EO%", "1.2", "%", "1.0", "6.0"),
    ("BASO%", "0.5", "%", "0.0", "1.0"),
    ("NEUT#", "9.23", "10*3/uL", "1.80", "7.70"),
    ("LYMPH#", "1.66", "10*3/uL", "1.00", "4.80"),
    ("MONO#", "0.71", "10*3/uL", "0.20", "1.00"),
    ("EO#", "0.14", "10*3/uL", "0.00", "0.50"),
    ("BASO#", "0.06", "10*3/uL", "0.00", "0.20"),
]
CBC_COMMENTS = {"WBC": "Left_Shift?", "PLT": "PLT_Clumps?"}

CMP = [
    ("GLU", "142", "mg/dL", "70", "99"),
    ("BUN", "18", "mg/dL", "7", "20"),
    ("CREA", "1.02", "mg/dL", "0.70", "1.30"),
    ("NA", "139", "mmol/L", "136", "145"),
    ("K", "5.8", "mmol/L", "3.5", "5.1"),
    ("CL", "103", "mmol/L", "98", "107"),
    ("CO2", "24", "mmol/L", "22", "29"),
    ("CA", "9.4", "mg/dL", "8.6", "10.2"),
    ("TP", "7.1", "g/dL", "6.0", "8.3"),
    ("ALB", "4.2", "g/dL", "3.5", "5.2"),
    ("TBIL", "0.6", "mg/dL", "0.1", "1.2"),
    ("ALP", "71", "U/L", "40", "129"),
    ("ALT", "34", "U/L", "7", "56"),
    ("AST", "28", "U/L", "10", "40"),
]
CMP_COMMENTS = {"K": "Hemolysis index 2+: potassium may be falsely elevated"}


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def _results(panel: list, comments: dict[str, str], started: datetime, done: datetime, operator: str, instrument: str) -> list[str]:
    records = []
    for i, (code, value, units, low, high) in enumerate(panel, start=1):
        v = float(value)
        flag = "L" if v < float(low) else "H" if v > float(high) else "N"
        records.append(
            f"R|{i}|^^^{code}^1|{value}|{units}|{low} to {high}|{flag}||F||{operator}|{_ts(started)}|{_ts(done)}|{instrument}"
        )
        if code in comments:
            records.append(f"C|1|I|{comments[code]}|I")
    return records


def default_messages(now: datetime | None = None) -> list[list[str]]:
    """The two demo messages (records without CR), time-stamped relative to ``now``."""
    now = now or datetime.now()
    cbc_tests = "\\".join(f"^^^{code}^1" for code, *_ in CBC)
    cbc = [
        f"H|\\^&|||SIM-HEM^1.0^HA000123|||||LIS||P|1|{_ts(now)}",
        "P|1|PAT-000417|||Doe^Jane^Q||19800101|F",
        f"O|1|S26-000123|^12^3|{cbc_tests}|R|{_ts(now - timedelta(minutes=47))}|||||N||||Whole blood||||||||||F",
        *_results(CBC, CBC_COMMENTS, now - timedelta(seconds=75), now - timedelta(seconds=5), "OP01", "SIM-HEM-01"),
        "L|1|N",
    ]
    cmp_tests = "\\".join(f"^^^{code}^1" for code, *_ in CMP)
    cmp_ = [
        f"H|\\^&|||SIM-CHEM^2.1^CA000456|||||LIS||P|1|{_ts(now)}",
        "P|1|PAT-000982|||Roe^Richard^A||19571230|M||1 Example Rd^Springfield||555-0100|Dr Lee",
        f"O|1|S26-000124|^4^1|{cmp_tests}|R|{_ts(now - timedelta(hours=2))}|||||N||||Serum||||||||||F",
        *_results(CMP, CMP_COMMENTS, now - timedelta(minutes=9), now - timedelta(seconds=20), "OP02", "SIM-CHEM-01"),
        "L|1|N",
    ]
    return [cbc, cmp_]


def encode_frames(records: list[str]) -> list[bytes]:
    """Frames for one message: frame numbers start at 1 and wrap modulo 8 (1..7, 0, 1, ...);
    records over 240 characters continue in an ETB intermediate frame."""
    frames: list[bytes] = []
    fn = 1
    for record in records:
        data = record.encode("latin-1") + CR
        chunks = [data[i : i + 240] for i in range(0, len(data), 240)]
        for i, chunk in enumerate(chunks):
            body = str(fn % 8).encode() + chunk + (ETX if i == len(chunks) - 1 else ETB)
            frames.append(STX + body + f"{sum(body) % 256:02X}".encode() + CR + LF)
            fn += 1
    return frames


class ASTMAnalyzerSimulator(ByteSimulator):
    """The analyzer side of an E1381 link. Call :meth:`attach` with a function that injects
    bytes into the receiver's input (``SimulatedTransport.push``) to start transmitting."""

    def __init__(
        self,
        messages: list[list[str]] | None = None,
        *,
        gap_s: float = 1.5,
        corrupt: tuple[tuple[int, int], ...] = ((0, 6),),
        retry_s: float = 10.0,
    ) -> None:
        self._messages = messages
        self.gap_s = gap_s
        self.retry_s = retry_s
        self.corrupt = set(corrupt)  # (message index, frame index) sent corrupted once
        self.log: list[str] = []
        self.naks_received = 0
        self.messages_sent = 0
        self._push: Callable[[bytes], None] | None = None
        self._queue: list[list[bytes]] = []
        self._state = "idle"
        self._msg = -1
        self._frame = 0
        self._retries = 0
        self._corrupted: set[tuple[int, int]] = set()
        self._timer: threading.Timer | None = None
        self._stopped = False
        self._lock = threading.Lock()

    # -- control ----------------------------------------------------------------

    def attach(self, push: Callable[[bytes], None]) -> None:
        self._push = push
        self._queue = [encode_frames(m) for m in (self._messages or default_messages())]
        self._schedule(0.05)

    def stop(self) -> None:
        self._stopped = True
        if self._timer:
            self._timer.cancel()

    @property
    def done(self) -> bool:
        return self._state == "idle" and self._msg >= len(self._queue) - 1

    def _schedule(self, delay: float) -> None:
        if self._stopped:
            return
        self._timer = threading.Timer(delay, self._begin)
        self._timer.daemon = True
        self._timer.start()

    def _begin(self) -> None:
        with self._lock:
            if self._stopped or self._state != "idle" or self._msg + 1 >= len(self._queue):
                return
            self._msg += 1
            self._frame = 0
            self._retries = 0
            self._state = "enq"
            self.log.append("ENQ")
        assert self._push is not None
        self._push(ENQ)

    # -- link protocol ----------------------------------------------------------

    def handle_bytes(self, data: bytes) -> bytes:
        out = b""
        with self._lock:
            for byte in data:
                out += self._on_reply(bytes([byte]))
        return out

    def _on_reply(self, reply: bytes) -> bytes:
        if self._state == "enq":
            if reply == ACK:
                self._state = "frame"
                return self._send()
            if reply == NAK:  # receiver not ready: wait 10 s and try again (E1381)
                self._state = "idle"
                self._msg -= 1
                self._schedule(self.retry_s)
            return b""
        if self._state != "frame":
            return b""
        frames = self._queue[self._msg]
        if reply in (ACK, EOT):
            self._frame += 1
            self._retries = 0
            if self._frame >= len(frames):
                return self._end()
            return self._send()
        if reply == NAK:
            self.naks_received += 1
            self._retries += 1
            self.log.append(f"NAK for frame index {self._frame}")
            if self._retries >= 6:  # E1381: give up after six transmissions of one frame
                self.log.append("six NAKs: message abandoned")
                return self._end(sent=False)
            return self._send(retransmission=True)
        return b""

    def _end(self, sent: bool = True) -> bytes:
        self._state = "idle"
        self.messages_sent += int(sent)
        self.log.append("EOT")
        self._schedule(self.gap_s)
        return EOT

    def _send(self, retransmission: bool = False) -> bytes:
        frame = self._queue[self._msg][self._frame]
        key = (self._msg, self._frame)
        if key in self.corrupt and key not in self._corrupted:
            self._corrupted.add(key)
            # Line noise: one text character arrives with a flipped bit; checksum unchanged.
            bad = bytearray(frame)
            pos = next(i for i in range(2, len(bad)) if chr(bad[i]).isdigit() and i > 3)
            bad[pos] ^= 0x01
            self.log.append(f"frame index {self._frame} sent with a transmission error")
            return bytes(bad)
        self.log.append(f"frame index {self._frame}" + (" (retransmission)" if retransmission else ""))
        return frame
