"""In-process simulated transport.

Simulators emulate an instrument at the *wire protocol* level: the driver sends
exactly the bytes it would send to real hardware and parses exactly the replies
a real instrument would produce. That means ``--simulate`` exercises the same
code path as a physical connection - it is what makes the test-suite
meaningful and lets people try a server before plugging anything in.

Write a simulator for a line protocol by subclassing :class:`LineSimulator`
and implementing :meth:`LineSimulator.handle`; for binary protocols subclass
:class:`ByteSimulator`.

Instruments that emit data on their own (a running measurement, a streaming
sensor) can implement an optional ``poll()`` method returning whatever is due
now (reply line(s) for line simulators, bytes for byte simulators, or
``None``). The transport calls it whenever the driver waits for input.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from labmcp.transports.base import Transport


class LineSimulator:
    """Base class for simulators of text/line protocols."""

    def handle(self, command: str) -> str | list[str] | None:
        """Return the reply line(s) for ``command`` (without terminators), or ``None``."""
        raise NotImplementedError


class ByteSimulator:
    """Base class for simulators of binary protocols."""

    def handle_bytes(self, data: bytes) -> bytes:
        """Consume ``data`` written by the driver and return any reply bytes."""
        raise NotImplementedError


class SimulatedTransport(Transport):
    def __init__(self, simulator: LineSimulator | ByteSimulator, **kwargs: Any) -> None:
        kwargs.setdefault("timeout", 0.5)
        super().__init__(**kwargs)
        self.simulator = simulator
        self.description = f"sim://{type(simulator).__name__}"
        self._pending_in = b""
        self._out = b""
        self._cv = threading.Condition()

    def _write(self, data: bytes) -> None:
        if isinstance(self.simulator, ByteSimulator):
            reply = self.simulator.handle_bytes(data)
            self._push(reply)
            return
        self._pending_in += data
        term = self.write_termination.encode(self.encoding)
        while term and term in self._pending_in:
            line, _, self._pending_in = self._pending_in.partition(term)
            reply = self.simulator.handle(line.decode(self.encoding))
            if reply is None:
                continue
            lines = [reply] if isinstance(reply, str) else reply
            rt = self.read_termination
            self._push("".join(item + rt for item in lines).encode(self.encoding))

    def _deliver(self, due: str | bytes | list[str] | None) -> None:
        if not due:
            return
        if isinstance(due, bytes):
            self._push(due)
        else:
            lines = [due] if isinstance(due, str) else due
            self._push("".join(line + self.read_termination for line in lines).encode(self.encoding))

    def push(self, data: str | bytes) -> None:
        """Inject unsolicited data (e.g. a streaming instrument) into the read buffer."""
        self._push(data.encode(self.encoding) if isinstance(data, str) else data)

    def _push(self, data: bytes) -> None:
        if data:
            with self._cv:
                self._out += data
                self._cv.notify_all()

    def _read(self, max_bytes: int, timeout: float) -> bytes:
        poll = getattr(self.simulator, "poll", None)
        if callable(poll):
            self._deliver(poll())
        with self._cv:
            if not self._out:
                self._cv.wait(timeout=min(timeout, 0.02 if callable(poll) else 0.05))
            data, self._out = self._out[:max_bytes], self._out[max_bytes:]
        if not data:
            time.sleep(0)
        return data

    def _flush_input(self) -> None:
        with self._cv:
            self._out = b""

    def _close(self) -> None:
        pass
