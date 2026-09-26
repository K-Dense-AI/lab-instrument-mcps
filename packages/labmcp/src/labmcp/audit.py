"""Command audit trail.

Every byte exchanged with an instrument is recorded so a scientist can see
exactly what an AI agent asked the hardware to do. The most recent entries are
kept in memory (exposed through the ``get_command_log`` tool) and, when a path
is configured, appended to a JSON Lines file for long-term records.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Direction = Literal["write", "read", "event"]

log = logging.getLogger("labmcp")

#: Entries longer than this are truncated so bulk data doesn't bloat memory or the log file.
MAX_ENTRY_CHARS = 2048
TRUNCATED_PREFIX = 256


class AuditLog:
    def __init__(self, path: str | Path | None = None, maxlen: int = 500) -> None:
        self.path = Path(path).expanduser() if path else None
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        #: Last error writing the log file, or ``None``. Shown by ``get_connection_info``.
        self.write_error: str | None = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, direction: Direction, data: str | bytes, source: str = "") -> None:
        size = len(data)
        if size > MAX_ENTRY_CHARS:  # waveforms, screenshots, images: keep a readable prefix only
            data = data[:TRUNCATED_PREFIX]
        text = _to_text(data) if isinstance(data, bytes) else data
        if size > MAX_ENTRY_CHARS:
            unit = "bytes" if isinstance(data, bytes) else "chars"
            text = f"{text} ... [truncated, {size} {unit} total]"
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "direction": direction,
            "data": text.rstrip("\r\n"),
        }
        if source:
            entry["source"] = source
        with self._lock:
            self._entries.append(entry)
            if self.path:
                # A full disk or an unplugged network share must not make instrument I/O fail:
                # that would block stop commands, and lose replies already read from the wire.
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry) + "\n")
                except OSError as exc:
                    if self.write_error is None:
                        log.error("Cannot write the audit log %s: %s (keeping entries in memory)", self.path, exc)
                    self.write_error = f"{type(exc).__name__}: {exc}"
                else:
                    if self.write_error is not None:
                        log.warning("Audit log %s is writable again", self.path)
                    self.write_error = None

    def event(self, message: str, source: str = "") -> None:
        """Record a high-level action for drivers that don't speak a byte protocol."""
        self.record("event", message, source)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._entries)
        return entries[-limit:] if limit > 0 else entries


_CONTROL_NAMES = {
    0x00: "NUL", 0x01: "SOH", 0x02: "STX", 0x03: "ETX", 0x04: "EOT", 0x05: "ENQ", 0x06: "ACK",
    0x07: "BEL", 0x08: "BS", 0x0B: "VT", 0x0C: "FF", 0x0E: "SO", 0x0F: "SI", 0x10: "DLE",
    0x11: "XON", 0x13: "XOFF", 0x15: "NAK", 0x17: "ETB", 0x18: "CAN", 0x1B: "ESC", 0x1C: "FS",
    0x1D: "GS", 0x1E: "RS", 0x1F: "US", 0x7F: "DEL",
}


def _to_text(data: bytes) -> str:
    """Show ASCII protocols as text (control bytes as ``<STX>``, ``<ESC>`` ...) and binary as hex."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return data.hex(" ")
    controls = sum(1 for ch in text if not ch.isprintable() and ch not in "\r\n\t")
    if controls == 0:
        return text
    if controls > max(2, len(text) // 4):  # mostly binary
        return data.hex(" ")
    return "".join(
        f"<{_CONTROL_NAMES.get(ord(ch), f'x{ord(ch):02X}')}>" if not ch.isprintable() and ch not in "\r\n\t" else ch
        for ch in text
    )
