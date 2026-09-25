"""Wire-level simulator of a MethodSCRIPT potentiostat (an EmStat Pico) with a redox-active cell.

The simulator speaks the communication protocol (``t``, ``i``, ``e`` + script + empty line, ``Z``,
``Y``) and interprets the subset of MethodSCRIPT that measurement scripts use: variables,
``store_var``, PGStat/range/bandwidth set-up, ``set_e``, ``cell_on``/``cell_off``, ``wait``,
``timer_start``/``timer_get``, ``send_string``, the CV/LSV/DPV/CA measurement loops (with
``nscans``), ``pck_*`` packages and ``on_finished:``. Output lines use the exact package encoding
of MethodSCRIPT manual chapter 5 and are streamed in (accelerated) real time, so aborting works.

The cell is a 3 mm disk electrode in 1 mM of a reversible one-electron couple (E0 = +0.20 V,
D = 7.6e-6 cm^2/s, the ferro/ferricyanide couple vs Ag/AgCl), plus double-layer charging and
noise. Currents follow semi-infinite planar diffusion exactly for the staircase potential the
instrument applies (superposition of Cottrell responses), which gives the classic CV "duck".
Halt/resume (``h``/``H``) and other techniques are not simulated.
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field

from labmcp import LineSimulator

F = 96485.332  # C/mol
R_GAS = 8.314462  # J/(mol K)
SI = {
    "a": 1e-18,
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "": 1.0,
    " ": 1.0,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "i": 1.0,
}
_ENCODE_PREFIXES = ["a", "f", "p", "n", "u", "m", " ", "k", "M", "G"]
TECHNIQUE_IDS = {
    "meas_loop_lsv": "0000",
    "meas_loop_dpv": "0001",
    "meas_loop_cv": "0005",
    "meas_loop_ca": "0007",
}
LOOP_ARGS = {"meas_loop_lsv": 6, "meas_loop_dpv": 8, "meas_loop_cv": 7, "meas_loop_ca": 5}
KNOWN = {
    "var",
    "store_var",
    "set_pgstat_chan",
    "set_pgstat_mode",
    "set_max_bandwidth",
    "set_range",
    "set_range_minmax",
    "set_autoranging",
    "set_e",
    "cell_on",
    "cell_off",
    "wait",
    "timer_start",
    "timer_get",
    "send_string",
    "pck_start",
    "pck_add",
    "pck_end",
    "endloop",
    "on_finished:",
    *TECHNIQUE_IDS,
}
# EmStat Pico low-speed current ranges (manual table 38): (index, maximum current in A)
PICO_RANGES = [
    (0x0, 60e-9),
    (0x1, 1.17e-6),
    (0x2, 2.34e-6),
    (0x3, 4.68e-6),
    (0x4, 9.38e-6),
    (0x5, 18.7e-6),
    (0x6, 37.5e-6),
    (0x7, 75e-6),
    (0x8, 150e-6),
    (0x9, 300e-6),
    (0xA, 600e-6),
    (0xB, 3e-3),
]


def encode_value(value: float, integer: bool = False) -> str:
    """Encode ``value`` as a package value: 7 hex digits (offset 2^27) + SI prefix (manual 4.3)."""
    if integer:
        return f"{int(value) + (1 << 27):07X}i"
    if not math.isfinite(value):
        return "     nan"
    if value == 0:
        return f"{1 << 27:07X} "
    for prefix in _ENCODE_PREFIXES:
        raw = round(value / SI[prefix])
        if abs(raw) < (1 << 27) - 1:
            return f"{raw + (1 << 27):07X}{prefix}"
    return "     nan"


class ScriptError(Exception):
    def __init__(self, code: int, line: int, col: int | None = None) -> None:
        self.code, self.line, self.col = code, line, col

    def reply(self, prefix: str = "") -> str:
        where = f"Line {self.line}" + (f", Col {self.col}" if self.col is not None else "")
        return f"{prefix}!{self.code:04X}: {where}"


@dataclass
class Cell:
    """Reversible redox couple R -> O + e- at a planar disk electrode."""

    e0_v: float = 0.20
    n: int = 1
    conc_mol_cm3: float = 1e-6  # 1 mM
    diff_cm2_s: float = 7.6e-6
    area_cm2: float = math.pi * 0.15**2  # 3 mm disk
    cdl_f_cm2: float = 20e-6
    temperature_k: float = 298.15
    noise_a: float = 2e-9
    rng: random.Random = field(default_factory=lambda: random.Random(1))

    def fraction_oxidised(self, e_v: float) -> float:
        x = self.n * F / (R_GAS * self.temperature_k) * (e_v - self.e0_v)
        return 1.0 / (1.0 + math.exp(-max(min(x, 700.0), -700.0)))

    def _k(self) -> float:
        return self.n * F * self.area_cm2 * self.conc_mol_cm3 * math.sqrt(self.diff_cm2_s / math.pi)

    def noise(self, i: float) -> float:
        return i + self.rng.gauss(0.0, self.noise_a + 0.002 * abs(i))

    def staircase(self, potentials: list[float], step_s: float, lead_s: float) -> list[float]:
        """Current sampled at the end of each potential step of a staircase sweep.

        The solution initially contains only the reduced form; the cell was switched on at
        potentials[0] ``lead_s`` seconds before the first step (equilibration). The current is the
        exact superposition of Cottrell terms for each step change of the surface fraction;
        steps older than 256 are summed in blocks of 64 to keep this O(N).
        """
        k = self._k()
        jumps: list[tuple[float, float]] = []  # (time of change, change in oxidised fraction)
        g_prev = 0.0
        g0 = self.fraction_oxidised(potentials[0]) if potentials else g_prev
        jumps.append((0.0, g0 - g_prev))
        g_prev = g0
        blocks: list[tuple[float, float]] = []  # aggregated far-past jumps (mean time, sum)
        out = []
        cdl = self.cdl_f_cm2 * self.area_cm2
        for idx, e in enumerate(potentials):
            t_change = lead_s + idx * step_s
            if idx > 0:
                g = self.fraction_oxidised(e)
                jumps.append((t_change, g - g_prev))
                g_prev = g
            t = t_change + step_s  # sampled at the end of the step
            while len(jumps) > 256 + 64:
                chunk, jumps = jumps[:64], jumps[64:]
                total = sum(d for _, d in chunk)
                mean_t = sum(tt * abs(d) for tt, d in chunk) / (sum(abs(d) for _, d in chunk) or 1.0)
                blocks.append((mean_t if total else chunk[0][0], total))
            i = sum(d / math.sqrt(t - tt) for tt, d in jumps if d)
            i += sum(d / math.sqrt(t - tt) for tt, d in blocks if d)
            slope = (e - potentials[idx - 1]) / step_s if idx > 0 else 0.0
            out.append(self.noise(k * i + cdl * slope))
        return out

    def dpv(self, potentials: list[float], pulse_v: float, pulse_s: float) -> list[float]:
        k = self._k() / math.sqrt(pulse_s)
        return [
            self.noise(k * (self.fraction_oxidised(e + pulse_v) - self.fraction_oxidised(e)))
            for e in potentials
        ]

    def chrono(self, e_v: float, times_since_on: list[float]) -> list[float]:
        """Cottrell current after switching the cell on at ``e_v`` (plus a small background)."""
        delta = self.fraction_oxidised(e_v)
        return [self.noise(self._k() * delta / math.sqrt(t) + 5e-9) for t in times_since_on]


def _staircase(begin: float, vertices: list[float], step: float) -> list[float]:
    """Potentials of a staircase from ``begin`` through ``vertices`` in steps of ``step``."""
    out = [begin]
    current = begin
    for target in vertices:
        direction = 1.0 if target > current else -1.0
        n = int(math.floor(abs(target - current) / step + 1e-9))
        out += [current + direction * step * (j + 1) for j in range(n)]
        current = current + direction * step * n
    return out


class MethodScriptSimulator(LineSimulator):
    """EmStat Pico speaking the communication protocol + MethodSCRIPT subset."""

    def __init__(self, speed: float = 10.0, seed: int = 1) -> None:
        self.speed = speed
        self.cell = Cell(rng=random.Random(seed))
        self.receiving: list[str] | None = None
        self.queue: list[tuple[float, str, str]] = []  # (due time, line, section)
        self.running = False
        self._clock = time.monotonic

    # ------------------------------------------------------------ transport hooks

    def poll(self) -> list[str]:
        """Lines whose (simulated) time has come."""
        now = self._clock()
        due = [line for t, line, _ in self.queue if t <= now]
        self.queue = [item for item in self.queue if item[0] > now]
        if due and self.running and not self.queue:
            self.running = False
        return due

    def handle(self, command: str) -> str | list[str] | None:
        if self.receiving is not None:
            if command.strip() == "":
                script, self.receiving = self.receiving, None
                return self._start(script)
            self.receiving.append(command)
            return None
        cmd = command.strip()
        if self.running:
            if cmd == "Z":
                return self._abort("Z", loop_only=False)
            if cmd == "Y":
                return self._abort("Y", loop_only=True)
            if cmd == "t":
                return self._version()
            return f"{cmd[:1]}!0006" if cmd else None
        if cmd == "t":
            return self._version()
        if cmd == "i":
            return "iESPSIM01"
        if cmd == "e":
            self.receiving = []
            return None
        if cmd == "Z":
            return "Z!0006"  # no script running
        if cmd == "":
            return None
        return f"{cmd[0]}!0003"

    @staticmethod
    def _version() -> list[str]:
        return ["tespico1600#Sep 30 2025 12:00:00", "R*"]

    # ------------------------------------------------------------ abort

    def _abort(self, echo: str, loop_only: bool) -> list[str]:
        """``Z``: skip to ``on_finished:``; ``Y``: end the measurement loop, continue after it."""
        now = self._clock()
        pending_loop = [line for _, line, section in self.queue if section == "loop"]
        in_loop = "*" in pending_loop and not any(line.startswith("M") for line in pending_loop)
        keep = {"post", "finished", "end"} if loop_only else {"finished", "end"}
        rest = [line for _, line, section in self.queue if section in keep]
        if loop_only and pending_loop and not in_loop:  # Y before the loop started: nothing to end
            return [echo]
        self.queue = [
            (now + 1e-3 * (i + 1), line, "end") for i, line in enumerate((["*"] if in_loop else []) + rest)
        ]
        return [echo]

    # ------------------------------------------------------------ script interpreter

    def _start(self, script: list[str]) -> str | list[str]:
        try:
            program = self._parse(script)
        except ScriptError as err:
            return err.reply("e")
        now = self._clock()
        self.queue = []
        try:
            timeline = self._run(program)
        except ScriptError as err:  # runtime error: output up to the error, then the error line
            timeline = err.timeline + [(err.at, err.reply(), "end")]  # type: ignore[attr-defined]
        else:
            timeline.append((timeline[-1][0] if timeline else 0.0, "", "end"))
        self.queue = [(now + t / self.speed, line, section) for t, line, section in timeline]
        self.running = True
        return "e"

    def _parse(self, script: list[str]) -> list[tuple[int, str, list[str]]]:
        program = []
        declared: set[str] = set()
        for number, raw in enumerate(script, start=1):
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            if text.startswith("send_string"):
                program.append((number, "send_string", [text[len("send_string") :].strip().strip('"')]))
                continue
            tokens = text.split()
            cmd, args = tokens[0], tokens[1:]
            if cmd not in KNOWN:
                raise ScriptError(0x4001, number, len(raw.rstrip()) + 1)
            if cmd == "var":
                if not args or args[0] in declared:
                    raise ScriptError(0x4026 if args else 0x4007, number)
                declared.add(args[0])
            elif cmd in TECHNIQUE_IDS and len([a for a in args if "(" not in a]) != LOOP_ARGS[cmd]:
                raise ScriptError(0x4007, number)
            for a in args:
                if cmd == "var" or "(" in a or re.fullmatch(r"[a-j][a-z]", a):  # optional args, VarTypes
                    continue
                if re.fullmatch(r"[a-z][a-z0-9_]*", a) and a not in declared:
                    raise ScriptError(0x420B, number)
            program.append((number, cmd, args))
        return program

    def _run(self, program: list[tuple[int, str, list[str]]]) -> list[tuple[float, str, str]]:
        values: dict[str, tuple[float, str]] = {}
        out: list[tuple[float, str, str]] = []
        t = 0.0
        timer0 = 0.0
        cell_on = False
        cell_on_t = 0.0
        section = "pre"
        loop_vars: list[str] = []
        package: list[str] = []
        int_vars: set[str] = set()
        i = 0

        def val(token: str, line: int) -> float:
            if token in values:
                return values[token][0]
            m = re.fullmatch(r"([-+]?\d+)([afpnumkMGTPEi]?)", token)
            if m:
                return int(m[1]) * SI[m[2]]
            m = re.fullmatch(r"0x([0-9A-Fa-f]+)i?|0b([01]+)i?", token)
            if m:
                return float(int(m[1], 16) if m[1] else int(m[2], 2))
            raise ScriptError(0x4039, line)

        def fail(code: int, line: int) -> None:
            err = ScriptError(code, line)
            err.timeline, err.at = out, t  # type: ignore[attr-defined]
            raise err

        while i < len(program):
            number, cmd, args = program[i]
            i += 1
            if cmd == "on_finished:":
                section = "finished"
            elif cmd == "store_var":
                values[args[0]] = (val(args[1], number), args[2] if len(args) > 2 else "ja")
                if args[1].endswith("i") or args[1].startswith(("0x", "0b")):
                    int_vars.add(args[0])
            elif cmd == "set_e":
                if not -1.7 <= val(args[0], number) <= 2.0:
                    fail(0x000F, number)
            elif cmd == "cell_on":
                cell_on, cell_on_t = True, t
            elif cmd == "cell_off":
                cell_on = False
            elif cmd == "wait":
                t += max(0.0, val(args[0], number))
            elif cmd == "timer_start":
                timer0 = t
            elif cmd == "timer_get":
                values[args[0]] = (t - timer0, "eb")
            elif cmd == "pck_start":
                package = []
            elif cmd == "pck_add":
                if args[0] in values:
                    v, vt = values[args[0]]
                    package.append(vt + encode_value(v, integer=args[0] in int_vars))
                else:
                    package.append("ja" + encode_value(val(args[0], number)))
            elif cmd == "pck_end":
                out.append((t, "P" + ";".join(package), section))
            elif cmd == "send_string":
                out.append((t, "T" + args[0], section))
            elif cmd in TECHNIQUE_IDS:
                if not cell_on:
                    fail(0x4027, number)
                body = []
                while i < len(program) and program[i][1] != "endloop":
                    body.append(program[i])
                    i += 1
                i += 1  # skip endloop
                loop_vars = [a[0] for _, c, a in body if c == "pck_add"]
                with_timer = any(c == "timer_get" for _, c, _ in body)
                timer_var = next((a[0] for _, c, a in body if c == "timer_get"), "")
                nscans = 1
                for a in args:
                    m = re.fullmatch(r"nscans\((\d+)\)", a)
                    if m:
                        nscans = int(m[1])
                plain = [a for a in args if "(" not in a]
                points = self._technique(cmd, plain, nscans, number, val, fail, t - cell_on_t)
                out.append((t, f"M{TECHNIQUE_IDS[cmd]}", "loop"))
                last_scan = -1
                for scan, pt_t, e, cur in points:
                    if nscans > 1 and scan != last_scan:
                        if last_scan >= 0:
                            out.append((t + pt_t, "-", "loop"))
                        out.append((t + pt_t, f"C{scan:04d}", "loop"))
                        last_scan = scan
                    parts = []
                    for name in loop_vars:
                        if name == plain[0]:
                            parts.append("da" + encode_value(e))
                        elif name == plain[1]:
                            parts.append("ba" + encode_value(cur) + self._meta(cur))
                        elif with_timer and name == timer_var:
                            parts.append("eb" + encode_value(t + pt_t - timer0))
                        elif name in values:
                            v, vt = values[name]
                            parts.append(vt + encode_value(v, integer=name in int_vars))
                    out.append((t + pt_t, "P" + ";".join(parts), "loop"))
                if points:
                    t += points[-1][1]
                if nscans > 1 and points:
                    out.append((t, "-", "loop"))
                out.append((t, "*", "loop"))
                section = "post"
            # set-up commands (pgstat mode, ranges, bandwidth, pck outside loops) have no output here
        return out

    def _technique(self, cmd, args, nscans, number, val, fail, since_on):
        """[(scan, time since loop start, set potential, current)] for one measurement loop."""
        cell = self.cell
        e_vals = [val(a, number) for a in args[2:]]
        pts: list[tuple[int, float, float, float]] = []
        if cmd == "meas_loop_ca":
            e, interval, run = e_vals
            if interval <= 0 or run <= 0:
                fail(0x4204, number)
            n = max(1, int(round(run / interval)))
            times = [interval * (k + 1) for k in range(n)]
            currents = cell.chrono(e, [since_on + tt for tt in times])
            return [(0, tt, e, c) for tt, c in zip(times, currents, strict=True)]
        if cmd == "meas_loop_cv":
            begin, v1, v2, step, rate = e_vals
            vertices = [v1, v2, begin]
        elif cmd == "meas_loop_lsv":
            begin, end, step, rate = e_vals
            vertices = [end]
        else:  # meas_loop_dpv
            begin, end, step, pulse, pulse_t, rate = e_vals
            vertices = [end]
            if pulse_t <= 0 or rate >= step / pulse_t / 2:
                fail(0x4205, number)
        if step <= 0 or rate <= 0:
            fail(0x4204, number)
        if any(not -1.7 <= v <= 2.0 for v in [begin, *vertices]):
            fail(0x000F, number)
        dt = step / rate
        single = _staircase(begin, vertices, step)
        if cmd == "meas_loop_dpv":
            direction = 1.0 if vertices[0] >= begin else -1.0
            currents = cell.dpv(single, direction * abs(pulse), pulse_t)
            return [(0, dt * (k + 1), e, c) for k, (e, c) in enumerate(zip(single, currents, strict=True))]
        potentials = single * nscans if cmd == "meas_loop_cv" else single
        currents = cell.staircase(potentials, dt, lead_s=since_on)
        per_scan = len(single)
        for k, (e, c) in enumerate(zip(potentials, currents, strict=True)):
            pts.append((k // per_scan, dt * (k + 1), e, c))
        return pts

    @staticmethod
    def _meta(current: float) -> str:
        index, top = next(((i, m) for i, m in PICO_RANGES if abs(current) <= 0.8 * m), PICO_RANGES[-1])
        status = 2 if abs(current) > 0.95 * top else 0
        return f",1{status:X},2{index:02X},40"
