"""Wire-level simulator of a Tecan Cavro syringe pump speaking the DT protocol.

Reproduces the answer blocks of the XLP 6000 Operating Manual (734237-C) chapter 3:
``/0<status><data>`` + ETX CR LF, the ready/busy bit, error codes 2/3/7/9/11/15 and the
"immediate" versus "reported on Q" error behaviour of manual section 3.6.3. Plunger and valve
moves take realistic time (half-steps / top speed), so ``?`` reports intermediate positions
and ``Q`` reports busy while a move is in progress.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from labmcp import LineSimulator

_TOKEN_RE = re.compile(r"([A-Za-z?&#%*<>])(\d+(?:,\d*)*)?")
_REPORTS = {
    "?",
    "?1",
    "?2",
    "?3",
    "?4",
    "?6",
    "?10",
    "?12",
    "?15",
    "?16",
    "?17",
    "?23",
    "?24",
    "?25",
    "?28",
    "?29",
    "Q",
    "&",
    "F",
    "#",
}


class CavroSimulator(LineSimulator):
    def __init__(
        self,
        address: str = "1",
        standard_steps: int = 6000,
        valve_ports: int = 0,
        clock: Callable[[], float] | None = None,
        speed: float = 1.0,
    ) -> None:
        self.address = address
        self.standard_steps = standard_steps
        self.valve_ports = valve_ports  # 0 = 3-port non-distribution valve (i/o/b); N = N-port distribution
        self._t0 = time.monotonic()
        self.clock = clock or (lambda: (time.monotonic() - self._t0) * speed)
        self.firmware = "XLP6000 V1.2.3 sim"
        self.mode = 0
        self.initialized = False
        self.overloaded = False
        self.error = 0
        self.frac = 0.0  # plunger position as a fraction of the full stroke
        self.valve = "i" if valve_ports == 0 else "1"
        self.start_speed, self.top, self.cutoff = 900, 1400, 900
        self.slope = 14
        self.buffer = ""
        self.plan: list[dict[str, float | str]] = []  # scheduled actions with start/end times
        self.overload_next_move = False  # test hook: next plunger move stalls half-way (error 9)
        self.init_count = 0

    # ------------------------------------------------------------ helpers

    def _spp(self) -> int:
        return self.standard_steps * (8 if self.mode in (1, 2) else 1)

    def _pos(self) -> int:
        return round(self._frac_now() * self._spp())

    def _frac_now(self) -> float:
        now = self.clock()
        frac = self.frac
        for act in self.plan:
            if act["kind"] != "plunger" or now < float(act["t0"]):
                continue
            t0, t1 = float(act["t0"]), float(act["t1"])
            a, b = float(act["from"]), float(act["to"])
            frac = b if now >= t1 else a + (b - a) * (now - t0) / (t1 - t0)
        return frac

    def _busy(self) -> bool:
        self._settle()
        return bool(self.plan)

    def _settle(self) -> None:
        """Apply the effects of scheduled actions that have finished."""
        now = self.clock()
        while self.plan and now >= float(self.plan[0]["t1"]):
            act = self.plan.pop(0)
            if act["kind"] == "plunger":
                self.frac = float(act["to"])
                if act.get("error"):
                    self.error = int(act["error"])
                    self.overloaded = True
                    self.plan.clear()
            elif act["kind"] == "valve":
                self.valve = str(act["to"])
            elif act["kind"] == "init":
                self.frac, self.initialized, self.overloaded = 0.0, True, False
                self.valve = str(act["to"])
                self.init_count += 1
            elif act["kind"] == "error":
                self.error = int(act["code"])
                self.plan.clear()

    def _status(self, error: int | None = None) -> str:
        busy = self._busy()  # settles finished actions first, which may set an error
        code = self.error if error is None else error
        return chr(0x40 | (0 if busy else 0x20) | (code & 0x0F))

    def _answer(self, data: str = "", error: int | None = None) -> str:
        return f"/0{self._status(error)}{data}"

    # ------------------------------------------------------------ protocol

    def handle(self, command: str) -> str | list[str] | None:
        line = command.strip()
        if len(line) < 2 or line[0] != "/":
            return None
        if line[1] != self.address:
            return None  # addressed to another pump (or a multi-pump address, which never gets a reply)
        body = line[2:]
        self._settle()
        if body in _REPORTS:
            return self._report(body)
        if body == "T":
            self._terminate()
            return self._answer()
        return self._execute_string(body)

    def _report(self, body: str) -> str:
        if body in {"Q", "?29"}:
            reply = self._answer()
            if not self._busy() and self.error in {2, 3, 9, 10, 11, 15}:
                self.error = 0  # Q clears the error once it has been reported (manual 3.6.3)
            return reply
        values = {
            "?": str(self._pos()),
            "?1": str(self.start_speed),
            "?2": str(self.top),
            "?3": str(self.cutoff),
            "?4": str(self._pos()),
            "?6": self.valve,
            "?10": "1" if self.buffer else "0",
            "F": "1" if self.buffer else "0",
            "?12": "12",
            "?15": str(self.init_count),
            "?16": "0",
            "?17": "0",
            "?23": self.firmware,
            "&": self.firmware,
            "#": "1234",
            "?24": "122",
            "?25": str(self.slope),
            "?28": str(self.mode),
        }
        return self._answer(values.get(body, ""))

    def _terminate(self) -> None:
        now = self.clock()
        kept = []
        for act in self.plan:
            if act["kind"] == "plunger":
                if now >= float(act["t0"]):  # stop the move in progress where it is
                    self.frac = self._frac_now()
                continue
            if act["kind"] == "valve" and float(act["t0"]) <= now:
                kept.append(act)  # valve moves are not terminated (manual: T does not stop valves)
        self.plan = kept

    def _execute_string(self, body: str) -> str:
        if not body.endswith("R"):
            self.buffer = body  # stored until an R arrives
            return self._answer()
        body = (self.buffer + body[:-1]) if body == "R" else body[:-1]
        self.buffer = ""
        tokens = _TOKEN_RE.findall(body)
        if "".join(c + a for c, a in tokens) != body:
            return self._answer(error=2)  # unparsable -> invalid command, reported immediately
        if any(cmd not in "ZYWNVvcLAPDIOBEMk" for cmd, _ in tokens):
            return self._answer(error=2)
        if self._busy():
            if any(cmd != "V" for cmd, _ in tokens):
                self.error = 15
                return self._answer()
            return self._answer()
        return self._schedule(tokens)

    def _schedule(self, tokens: list[tuple[str, str]]) -> str:
        now = self.clock()
        t = now
        frac = self.frac
        valve = self.valve
        mode = self.mode
        for cmd, arg in tokens:
            args = [int(a) for a in arg.split(",") if a != ""] if arg else []
            n = args[0] if args else None
            if cmd in "ZYW":
                force = n or 0
                if force not in (0, 1, 2) and not 10 <= force <= 40:
                    return self._deferred_error(3)
                t1 = t + 2.0
                home = "i" if self.valve_ports == 0 else "1"
                self.plan.append({"kind": "init", "t0": t, "t1": t1, "to": home})
                t, frac, valve = t1, 0.0, home
                self.error = 0  # initialization clears initialization/overload errors
            elif cmd == "N":
                if n not in (0, 1, 2):
                    return self._deferred_error(3)
                self.mode = mode = n
            elif cmd == "V":
                if n is None or not 5 <= n <= 6000:
                    return self._deferred_error(3)
                self.top = n
            elif cmd == "v":
                if n is None or not 50 <= n <= 1000:
                    return self._deferred_error(3)
                self.start_speed = n
            elif cmd == "c":
                if n is None or not 50 <= n <= 2700:
                    return self._deferred_error(3)
                self.cutoff = n
            elif cmd == "L":
                if n is None or not 1 <= n <= 20:
                    return self._deferred_error(3)
                self.slope = n
            elif cmd == "k":
                if n is None or not 0 <= n <= 2040:
                    return self._deferred_error(3)
            elif cmd in "APD":
                if not (self.initialized or any(a["kind"] == "init" for a in self.plan)):
                    self.error = 7
                    return self._answer()
                if self.overloaded:
                    self.error = 9
                    return self._answer()
                if valve in {"b", "e"}:
                    return self._answer(error=11)
                spp = self.standard_steps * (8 if mode in (1, 2) else 1)
                cur = round(frac * spp)
                target = n if cmd == "A" else cur + (n or 0) if cmd == "P" else cur - (n or 0)
                if n is None or not 0 <= target <= spp:
                    return self._deferred_error(3)  # manual: A7000R answers OK, Q reports error 3
                pulses_per_step = 6000 / spp if mode in (0, 1) else 1.0
                duration = abs(target - cur) * pulses_per_step / max(self.top, 1)
                new_frac = target / spp
                act: dict[str, float | str] = {
                    "kind": "plunger",
                    "t0": t,
                    "t1": t + duration,
                    "from": frac,
                    "to": new_frac,
                }
                if self.overload_next_move and duration > 0:
                    self.overload_next_move = False
                    act["t1"] = t + duration / 2
                    act["to"] = frac + (new_frac - frac) / 2
                    act["error"] = 9
                self.plan.append(act)
                t, frac = float(act["t1"]), float(act["to"])
            elif cmd in "IOBE":
                if not (self.initialized or any(a["kind"] == "init" for a in self.plan)):
                    self.error = 7
                    return self._answer()
                target_valve = self._valve_target(cmd, n)
                if target_valve is None:
                    return self._deferred_error(3)
                self.plan.append({"kind": "valve", "t0": t, "t1": t + 0.25, "to": target_valve})
                t, valve = t + 0.25, target_valve
            elif cmd == "M":
                if n is None or not 0 <= n <= 30000:
                    return self._deferred_error(3)
                self.plan.append({"kind": "delay", "t0": t, "t1": t + n / 1000})
                t += n / 1000
        return self._answer()

    def _deferred_error(self, code: int) -> str:
        """Invalid operand: execute what came before, then report the error via Q (manual 3.6.3)."""
        last = float(self.plan[-1]["t1"]) if self.plan else self.clock()
        self.plan.append({"kind": "error", "t0": last, "t1": last, "code": code})
        self._settle()
        return self._answer(error=0)

    def _valve_target(self, cmd: str, n: int | None) -> str | None:
        if self.valve_ports == 0:  # 3-port non-distribution valve
            if n is not None:
                return None
            return {"I": "i", "O": "o", "B": "b", "E": None}[cmd]
        if cmd in "BE":
            return self.valve
        if n is None:
            return "1" if cmd == "I" else str(self.valve_ports)
        if not 1 <= n <= self.valve_ports:
            return None
        return str(n)
