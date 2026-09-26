"""Tecan Cavro syringe-pump driver (Data Terminal "DT" protocol over RS-232/RS-485).

Command set, framing, status byte and error codes as documented in the
"Cavro XLP 6000 Modular Syringe Pump Operating Manual", Tecan Systems, document 734237-C
(October 2005), chapter 3 "Software Communication" and appendix G "Command Quick Reference":
https://www.manualslib.com/manual/1214060/Tecan-Cavro-Xlp-6000.html

Wire format (DT protocol, 9600 baud 8N1 by default)::

    to pump:    "/" <address> <commands> CR          e.g. "/1ZR\\r"  (switch 0 = address "1")
    from pump:  "/" "0" <status> [<data>] ETX CR LF   e.g. "/0`3000\\x03\\r\\n"

Status byte: ``0 1 X 0 e3 e2 e1 e0`` - bit 5 (X) is 1 when the pump is ready and 0 when busy;
bits 0-3 hold the error code. Only the ``Q`` reply's busy bit is authoritative (manual 3.6.1);
the error bits are valid in every reply.

Resolution (manual 3.3.2 "N" command, appendix E): N0 = standard mode, positions in
half-steps; N1 = fine positioning, positions in microsteps (x8) with speeds still in half-steps/s;
N2 = microstep mode (x8 positions and speeds). Full stroke is 6000 / 48000 increments on the
XLP 6000 and XMP 6000, 3000 / 24000 on the XCalibur (Tecan product specifications). Speeds
``V`` (5-6000 pulses/s) span 6000 pulses per full stroke in N0/N1 on all three models, which
gives the documented 1.2 s - 20 min per stroke.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport

#: DT address characters for address-switch positions 0..E (manual table 3-1/3-2).
ADDRESSES = "123456789:;<=>?"

#: Pump families with a verified number of plunger increments per full stroke in N0.
MODELS: dict[str, tuple[str, int]] = {
    "xlp6000": ("Cavro XLP 6000", 6000),
    "xmp6000": ("Cavro XMP 6000", 6000),
    "xcalibur": ("Cavro XCalibur", 3000),
}

#: Speed pulses per full stroke in N0/N1 (V is in half-steps/s): V5 = 20 min/stroke, V6000 ~ 1 s.
SPEED_PULSES_PER_STROKE = 6000
TOP_SPEED_MIN, TOP_SPEED_MAX = 5, 6000

ERROR_CODES: dict[int, str] = {
    0: "no error",
    1: "initialization error: the pump failed to initialize (check for blockages and loose "
    "connections, then initialize again)",
    2: "invalid command",
    3: "invalid operand (parameter out of range, e.g. a move beyond the end of the syringe)",
    # Not listed for the XLP 6000, but documented for the XCalibur (manual 733085-B, 3.6.3) and XL 3000.
    4: "invalid command sequence (command structure or communication protocol is incorrect)",
    6: "EEPROM failure (contact Tecan service)",
    7: "device not initialized: run `initialize` first",
    8: "internal failure (contact Tecan service)",
    9: "plunger overload: plunger movement blocked by excessive back-pressure; the pump must be "
    "re-initialized",
    10: "valve overload: the valve drive is blocked; re-initialize (or send a valve command)",
    11: "plunger move not allowed: the valve is in the bypass/throughput position",
    12: "internal failure (contact Tecan service)",
    14: "A/D converter failure (contact Tecan service)",
    15: "command overflow: a command was sent while the pump was still busy",
}

#: Initialization force codes (manual table 3-6): 0 full (>= 1 mL), 1 half (250/500 µL), 2 third (50/100 µL).
FORCE_CODES = {"full": 0, "half": 1, "third": 2}


def force_for_syringe(syringe_ul: float) -> int:
    """Recommended initialization force code for a syringe size (manual table 3-6)."""
    if syringe_ul >= 1000:
        return 0
    if syringe_ul >= 250:
        return 1
    return 2


class CavroError(InstrumentProtocolError):
    """The pump reported a non-zero error code in its status byte."""

    def __init__(self, code: int, context: str) -> None:
        self.code = code
        meaning = ERROR_CODES.get(code, "unknown error (see your pump's operating manual)")
        super().__init__(f"Pump reported error {code} {context}: {meaning}.")


@dataclass
class Reply:
    status: int
    data: str

    @property
    def ready(self) -> bool:
        return bool(self.status & 0x20)

    @property
    def error(self) -> int:
        return self.status & 0x0F


@dataclass
class MoveResult:
    steps: int
    top_speed: int
    position_steps: int
    terminated: bool


class CavroPump:
    """One Cavro pump addressed with the DT protocol."""

    def __init__(
        self,
        transport: Transport,
        *,
        address: str = "1",
        syringe_ul: float = 1000.0,
        model: str = "xlp6000",
        standard_steps: int | None = None,
        resolution_mode: int = 0,
    ) -> None:
        if address not in ADDRESSES or len(address) != 1:
            raise ValueError(f"Pump address must be one of {ADDRESSES!r} (switch 0 = '1'), got {address!r}")
        if resolution_mode not in (0, 1):
            raise ValueError("resolution_mode must be 0 (standard) or 1 (fine positioning)")
        self.t = transport
        self.address = address
        self.syringe_ul = float(syringe_ul)
        self.model_name, default_steps = MODELS.get(model, (model, 0))
        self.standard_steps = int(standard_steps or default_steps)
        if self.standard_steps <= 0:
            raise ValueError(f"Unknown pump model {model!r}; give the steps per stroke explicitly")
        self.resolution_mode = resolution_mode
        self._terminate = threading.Event()

    # ------------------------------------------------------------ low level

    def send(self, cmd: str, timeout: float | None = None) -> Reply:
        """Send one DT command string and parse the answer block (errors are returned, not raised)."""
        raw = self.t.query(f"/{self.address}{cmd}", timeout)
        start = raw.rfind("/0")
        if start < 0 or len(raw) < start + 3:
            raise InstrumentProtocolError(
                f"Unexpected answer to {cmd!r}: {raw!r} (expected '/0<status>...'). Check the pump "
                "address switch and that the pump uses the DT protocol at this baud rate."
            )
        status = ord(raw[start + 2])
        if status & 0xD0 != 0x40:  # bits 7..6 must be 01 and bit 4 must be 0
            raise InstrumentProtocolError(f"Invalid status byte {status:#04x} in answer to {cmd!r}: {raw!r}")
        return Reply(status, raw[start + 3 :])

    def command(self, cmd: str, timeout: float | None = None) -> Reply:
        """Send ``cmd`` and raise :class:`CavroError` if the answer carries an error code."""
        reply = self.send(cmd, timeout)
        if reply.error:
            raise CavroError(reply.error, f"in reply to {cmd!r}")
        return reply

    def query_status(self) -> Reply:
        """``Q``: the only authoritative ready/busy report (manual 3.6.1). Errors are returned."""
        return self.send("Q")

    def report(self, cmd: str) -> str:
        return self.command(cmd).data.strip()

    def _report_int(self, cmd: str) -> int:
        data = self.report(cmd)
        try:
            return int(data)
        except ValueError as exc:
            raise InstrumentProtocolError(f"Expected an integer in answer to {cmd!r}, got {data!r}") from exc

    def wait_ready(self, timeout: float, poll: float = 0.1, context: str = "") -> None:
        """Poll ``Q`` until the pump is ready; raise on any error code."""
        deadline = time.monotonic() + timeout
        while True:
            reply = self.query_status()
            if reply.error:
                raise CavroError(reply.error, context or "while waiting for the pump")
            if reply.ready:
                return
            if time.monotonic() > deadline:
                raise InstrumentTimeout(
                    f"Pump still busy after {timeout:.0f} s {context}. Call `terminate` if it should stop."
                )
            time.sleep(poll)

    # ------------------------------------------------------------ reports

    def plunger_steps(self) -> int:
        return self._report_int("?")

    def valve_position(self) -> str:
        return self.report("?6")

    def mode(self) -> int:
        return self._report_int("?28")

    def top_speed(self) -> int:
        return self._report_int("?2")

    def firmware(self) -> str:
        return self.report("&")

    def steps_per_stroke(self, mode: int | None = None) -> int:
        mode = self.mode() if mode is None else mode
        return self.standard_steps * (8 if mode in (1, 2) else 1)

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "Tecan (Cavro)", "model": self.model_name, "pump_address": self.address}
        with contextlib.suppress(InstrumentProtocolError):
            info["firmware"] = self.firmware()
        info["syringe_ul"] = f"{self.syringe_ul:g}"
        info["steps_per_stroke_standard"] = str(self.standard_steps)
        return info

    # ------------------------------------------------------------ actions

    def initialize(
        self,
        kind: str = "Z",
        force: int | None = None,
        input_port: int | None = None,
        output_port: int | None = None,
        timeout: float = 60.0,
    ) -> None:
        """``Z`` (valve homes CW), ``Y`` (CCW) or ``W`` (plunger only), then set N mode."""
        if kind not in {"Z", "Y", "W"}:
            raise ValueError("kind must be 'Z', 'Y' or 'W'")
        force = force_for_syringe(self.syringe_ul) if force is None else force
        if kind == "W" or (input_port is None and output_port is None):
            cmd = f"{kind}{force}R"
        else:
            cmd = f"{kind}{force},{input_port or 0},{output_port or 0}R"
        self._require_ready("initialize")
        reply = self.send(cmd)
        # Errors 1/7/9/10 describe the state initialization is meant to clear; the outcome is read
        # from Q afterwards (manual 3.6.3 "Initialization Errors").
        if reply.error and reply.error not in {1, 7, 9, 10}:
            raise CavroError(reply.error, f"in reply to {cmd!r}")
        self.wait_ready(timeout, context="during initialization")
        self.command(f"N{self.resolution_mode}R")
        self.wait_ready(5.0, context="after setting the resolution mode")

    def _require_ready(self, action: str) -> None:
        reply = self.query_status()
        if not reply.ready:
            raise InstrumentProtocolError(f"Cannot {action}: the pump is busy. Wait, or call `terminate`.")

    def move_plunger(self, kind: str, steps: int, top_speed: int, timeout: float) -> MoveResult:
        """``A`` absolute, ``P`` pick-up (down) or ``D`` dispense (up) at top speed ``V``."""
        if kind not in {"A", "P", "D"}:
            raise ValueError("kind must be 'A', 'P' or 'D'")
        if not TOP_SPEED_MIN <= top_speed <= TOP_SPEED_MAX:
            raise ValueError(f"top speed must be {TOP_SPEED_MIN}-{TOP_SPEED_MAX} pulses/s")
        self._require_ready("move the plunger")
        self._terminate.clear()
        self.command(f"V{top_speed}{kind}{steps}R")
        self.wait_ready(timeout, context=f"during the plunger move {kind}{steps}")
        return MoveResult(steps, top_speed, self.plunger_steps(), self._terminate.is_set())

    def move_valve(self, code: str, timeout: float = 10.0) -> str:
        """Valve command: ``I``/``O``/``B``/``E`` (non-distribution) or ``I<n>``/``O<n>`` (distribution)."""
        self._require_ready("move the valve")
        self.command(f"{code}R")
        self.wait_ready(timeout, context=f"during the valve move {code}")
        return self.valve_position()

    def terminate(self) -> Reply:
        """``T``: stop plunger moves, loops and delays immediately (valve moves finish)."""
        self._terminate.set()
        return self.send("T")

    def close(self) -> None:
        self.t.close()
