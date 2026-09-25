"""MethodSCRIPT driver for PalmSens potentiostats (EmStat Pico, EmStat4, Sensit Wearable, Nexus).

Sources:

* "MethodSCRIPT manual", version 1.8 (2025-10-15), PalmSens BV:
  https://www.palmsens.com/app/uploads/2025/10/MethodSCRIPT-v1_8.pdf (script syntax ch. 3, SI-prefixed
  literals ch. 4, data packages ch. 5, loop markers ch. 6 / 9.3, errors ch. 11 and appendix A,
  device ranges appendix B, variable types appendix C, measurement loops ch. 14.11).
* "Communication protocol for EmStat Pico", version 1.6 (2025-09-30):
  https://assets.palmsens.com/app/uploads/2025/10/Emstat-Pico-communication-protocol-V1.6.pdf and
  "Communication protocol for EmStat4", version 1.3 (2024-03-25):
  https://assets.palmsens.com/app/uploads/2024/03/EmStat4-communication-protocol-V1.3.pdf
  (commands ``t`` firmware version, ``i`` serial number, ``e`` execute script, ``Z`` abort; LF line
  endings; ``c!XXXX`` error replies).
* PalmSens' reference implementation: https://github.com/PalmSens/MethodSCRIPT_Examples
  (``palmsens/instrument.py``, ``palmsens/mscript.py``).

Protocol summary: every line ends with LF. ``e`` + script lines + an empty line executes a script;
the device answers ``e`` (or ``e!XXXX: Line L, Col C`` on a parse error), then the script output:
``M<technique>`` / ``*`` around measurement loops, ``C<nnnn>`` / ``-`` around CV scans, ``L`` / ``+``
around loops, ``T<text>`` from send_string, ``P<var>;<var>...`` data packages and finally an empty
line. Runtime errors are ``!XXXX: Line L``. Package values are 7 hex digits with an offset of 2^27
followed by an SI prefix character.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field

from labmcp import InstrumentError, InstrumentProtocolError, InstrumentTimeout, Transport

SI_PREFIXES: dict[str, float] = {
    "a": 1e-18,
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    " ": 1.0,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "i": 1.0,
}

DEVICE_TYPES = {
    "espico": "EmStat Pico",
    "senswb": "Sensit Wearable",
    "es4_hr": "EmStat4 HR",
    "es4_lr": "EmStat4 LR",
    "mes4hr": "MultiEmStat4 HR",
    "mes4lr": "MultiEmStat4 LR",
    "espbl": "EmStat Pico bootloader",
    "nexus1": "Nexus",
}

#: Measurement technique IDs of the ``M<id>`` loop marker (manual table 5).
TECHNIQUES = {
    "0000": "LSV",
    "0001": "DPV",
    "0002": "SWV",
    "0003": "NPV",
    "0004": "ACV",
    "0005": "CV",
    "0006": "CP stripping",
    "0007": "CA",
    "0008": "PAD",
    "0009": "FCA",
    "000A": "CP",
    "000B": "OCP",
    "000D": "EIS",
    "000E": "GEIS",
    "000F": "LSP",
    "0010": "FCV",
}

#: Most relevant error codes from MethodSCRIPT manual appendix A.
ERROR_CODES: dict[int, str] = {
    0x0001: "an unspecified error has occurred",
    0x0002: "an invalid VarType has been used",
    0x0003: "the command was not recognized",
    0x0006: "communication mode invalid (e.g. no script is running)",
    0x0007: "an argument has an unexpected value",
    0x0008: "command exceeds maximum length",
    0x0009: "the command has timed out",
    0x000C: "cannot run a script without loading one first",
    0x000E: "an overflow has occurred while averaging a measured value",
    0x000F: "the given potential is not valid",
    0x0010: "a variable has become NaN or inf",
    0x0014: "cannot perform OCP measurement when cell on",
    0x001B: "this command or part of it is not supported by the current device",
    0x001C: "step potential must be at least 1 DAC LSB for this technique",
    0x001D: "pulse potential must be at least 1 DAC LSB for this technique",
    0x001F: "product is not licensed for this technique",
    0x0020: "cannot have more than one high speed and/or max range mode enabled",
    0x0021: "the specified PGStat mode is not supported",
    0x0023: "command is invalid for the selected PGStat mode",
    0x0024: "the maximum number of vars to measure has been exceeded",
    0x0028: "variable divided by zero",
    0x0032: "critical cell overload, measurement aborted to prevent damage",
    0x0058: "timing error during fast measurement (possibly caused by communication)",
    0x005A: "the instrument cannot meet the requested measurement timing",
    0x0080: "the CE is oscillating",
    0x4001: "the script command is unknown",
    0x4004: "an unexpected character was encountered",
    0x4005: "the script is too large for the internal script memory",
    0x4008: "this optional argument is not valid for this command",
    0x400B: "measurement loops cannot be placed inside other measurement loops",
    0x400C: "command not supported in current situation",
    0x4018: "the script has ended unexpectedly",
    0x401B: "the pck sequence is called wrong",
    0x4026: "the variable already exists when declared",
    0x4027: "this command requires the cell to be enabled with cell_on",
    0x4028: "this command requires the cell to be disabled with cell_off",
    0x4029: "the technique requires that at least one step should be made",
    0x4032: "cannot set the potential (or potential range) within the active measurement loop",
    0x4039: "the literal argument was not correctly formed",
    0x4200: "argument value cannot be negative for this command",
    0x4202: "argument value cannot be zero for this command",
    0x4204: "argument value must be positive for this command",
    0x4205: "argument value is outside the allowed bounds for this command",
    0x4206: "argument value cannot be used for this specific instrument",
    0x420B: "argument variable is not declared",
    0x7FFF: "a fatal error has occurred, the device must be reset",
}

#: Potentiostat capabilities per device (MethodSCRIPT manual appendix B).
DEVICE_SPECS: dict[str, dict[str, object]] = {
    "EmStat Pico": {
        "potential_range_v": (-1.7, 2.0),
        "pgstat_modes": "low speed: -1.25..2.0 V, 2.2 V window, <=100 Hz; high speed: -1.7..2.0 V, "
        "1.214 V window, <=200 kHz; max range: -1.7..2.0 V, 2.6 V window, <=100 Hz",
        "current_ranges": "100 nA, 2, 4, 8, 16, 32, 63, 125, 250, 500 uA, 1 mA, 5 mA",
    },
    "EmStat4 LR": {"potential_range_v": (-3.0, 3.0), "current_ranges": "1 nA ... 10 mA (decades)"},
    "EmStat4 HR": {"potential_range_v": (-6.0, 6.0), "current_ranges": "100 nA ... 100 mA (decades)"},
    "Nexus": {"potential_range_v": (-10.0, 10.0), "current_ranges": "100 pA ... 1 A (decades)"},
}
DEVICE_SPECS["Sensit Wearable"] = DEVICE_SPECS["EmStat Pico"]

ERROR_RE = re.compile(r"!([0-9A-Fa-f]{4})(?::\s*Line\s+(\d+)(?:,\s*Col\s+(\d+))?)?")
_CONTROL_CHARS = dict.fromkeys(range(32), None)


# ---------------------------------------------------------------- value encoding


def decode_value(text: str) -> float:
    """Decode a package value ``HHHHHHHp`` (7 hex digits, offset 2^27, SI prefix) or ``     nan``."""
    if text.strip() == "nan":
        return math.nan
    if len(text) != 8 or text[7] not in SI_PREFIXES:
        raise InstrumentProtocolError(f"Malformed MethodSCRIPT value {text!r}")
    return (int(text[:7], 16) - (1 << 27)) * SI_PREFIXES[text[7]]


_LITERAL_PREFIXES = [
    ("G", 1e9),
    ("M", 1e6),
    ("k", 1e3),
    ("", 1.0),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
]


def encode_literal(value: float) -> str:
    """Format ``value`` as a MethodSCRIPT float literal: an integer with an SI prefix (``-500m``)."""
    if not math.isfinite(value):
        raise ValueError(f"{value!r} cannot be sent to the instrument")
    if value == 0:
        return "0"
    for prefix, factor in _LITERAL_PREFIXES:
        mantissa = value / factor
        rounded = round(mantissa)
        if rounded != 0 and abs(rounded) < 2**31 and abs(mantissa - rounded) <= 1e-6 * abs(mantissa):
            return f"{rounded}{prefix}"
    return f"{round(value / 1e-9)}n"


def error_text(code: int) -> str:
    return ERROR_CODES.get(code, "see appendix A of the MethodSCRIPT manual")


@dataclass
class Variable:
    type: str  # VarType id, e.g. 'da' (set potential), 'ba' (current), 'eb' (time)
    value: float
    status: int = 0  # metadata 1: 0 OK, 1 timing not met, 2 overload, 4 underload, 8 overload warning
    range: int | None = None  # metadata 2: current/potential range index


def parse_package(line: str) -> list[Variable]:
    """Parse a data package line ``P<var>;<var>...`` (manual chapter 5)."""
    if not line.startswith("P"):
        raise InstrumentProtocolError(f"Not a data package: {line!r}")
    out = []
    for part in line[1:].split(";"):
        fields = part.split(",")
        head = fields[0]
        if len(head) != 10:
            raise InstrumentProtocolError(f"Malformed variable {part!r} in package {line!r}")
        var = Variable(head[:2], decode_value(head[2:10]))
        for meta in fields[1:]:
            if meta[:1] == "1" and len(meta) == 2:
                var.status = int(meta[1], 16)
            elif meta[:1] == "2" and len(meta) == 3:
                var.range = int(meta[1:], 16)
        out.append(var)
    return out


@dataclass
class ScriptResult:
    packets: list[tuple[int, list[Variable]]] = field(default_factory=list)  # (scan index, variables)
    texts: list[str] = field(default_factory=list)  # send_string output
    lines: list[str] = field(default_factory=list)  # every output line, in order
    error: str | None = None  # runtime error, decoded
    aborted: bool = False
    timed_out: bool = False
    duration_s: float = 0.0


# ---------------------------------------------------------------- script generation


def select_pgstat_mode(device: str, e_min: float, e_max: float, bandwidth_hz: float) -> int:
    """Pick a PGStat mode that can apply ``e_min..e_max`` at ``bandwidth_hz`` (manual appendix B.1).

    The EmStat Pico / Sensit have a limited "dynamic potential window" that depends on the mode;
    EmStat4 and Nexus accept any mode with no functional difference, so low speed (2) is used.
    """
    if device not in {"EmStat Pico", "Sensit Wearable"}:
        return 2
    window = e_max - e_min
    #        mode, lowest V, highest V, window V, max bandwidth Hz
    modes = {2: (-1.25, 2.0, 2.2, 100.0), 4: (-1.7, 2.0, 2.6, 100.0), 3: (-1.7, 2.0, 1.214, 200e3)}
    order = [2, 4, 3] if bandwidth_hz <= 100 else [3]
    for mode in order:
        lo, hi, win, _ = modes[mode]
        if lo <= e_min and e_max <= hi and window <= win:
            return mode
    raise ValueError(
        f"The {device} cannot apply {e_min:g} V to {e_max:g} V at a bandwidth of {bandwidth_hz:.3g} Hz. "
        "Low speed mode: -1.25 to 2.0 V within a 2.2 V window (<=100 Hz); max range: -1.7 to 2.0 V within "
        "2.6 V (<=100 Hz); high speed: -1.7 to 2.0 V within 1.214 V. Narrow the potential range or scan "
        "more slowly."
    )


def bandwidth_for_rate(points_per_s: float) -> float:
    """Max bandwidth ~6x the data rate, as in PalmSens' example scripts (e.g. 10 pts/s -> 60 Hz)."""
    return min(max(6.0 * points_per_s, 1.0), 200e3)


def build_script(
    technique_line: str,
    *,
    begin_potential_v: float,
    e_min: float,
    e_max: float,
    pgstat_mode: int,
    bandwidth_hz: float,
    current_range_a: float,
    autorange: bool,
    equilibration_s: float,
    with_timer: bool = False,
) -> list[str]:
    """MethodSCRIPT for one measurement loop, following PalmSens' measurement-loop examples.

    The cell is always switched off by the ``on_finished:`` section, including after an abort.
    """
    lit = encode_literal
    lines = ["var p", "var c"]
    if with_timer:
        lines.append("var t")
    lines += [
        "set_pgstat_chan 0",
        f"set_pgstat_mode {pgstat_mode}",
        f"set_max_bandwidth {lit(bandwidth_hz)}",
        f"set_range_minmax da {lit(e_min)} {lit(e_max)}",
        f"set_range ba {lit(current_range_a)}",
    ]
    low = lit(min(1e-9, current_range_a)) if autorange else lit(current_range_a)
    lines.append(f"set_autoranging ba {low} {lit(current_range_a)}")
    lines += [f"set_e {lit(begin_potential_v)}", "cell_on"]
    if equilibration_s > 0:
        lines.append(f"wait {lit(equilibration_s)}")
    if with_timer:
        lines.append("timer_start")
    lines.append(technique_line)
    if with_timer:
        lines.append("  timer_get t")
    lines += ["  pck_start"]
    if with_timer:
        lines.append("  pck_add t")
    lines += ["  pck_add p", "  pck_add c", "  pck_end", "endloop", "on_finished:", "  cell_off"]
    return lines


_POTENTIAL_ARGS = {  # command -> indices of potential arguments (MethodSCRIPT manual 14.9 / 14.11)
    "set_e": (0,),
    "meas_loop_lsv": (2, 3),
    "meas_loop_cv": (2, 3, 4),
    "meas_loop_dpv": (2, 3),
    "meas_loop_ca": (2,),
}


def parse_literal(token: str) -> float | None:
    """Value of a MethodSCRIPT literal (``500m``, ``-2``, ``10i``, ``0x1F``), or None if not a literal."""
    m = re.fullmatch(r"([-+]?\d+)([afpnumkMGTPEi]?)", token)
    if m:
        return int(m[1]) * SI_PREFIXES.get(m[2] or " ", 1.0)
    m = re.fullmatch(r"0x([0-9A-Fa-f]+)i?|0b([01]+)i?", token)
    if m:
        return float(int(m[1], 16) if m[1] else int(m[2], 2))
    return None


def script_potentials(lines: list[str]) -> list[tuple[int, float]]:
    """Best-effort list of (line number, potential in V) a raw script would apply.

    Covers literal (and ``store_var``-assigned) potential arguments of set_e, set_range_minmax da and
    the CV/LSV/DPV/CA measurement loops. Potentials computed at run time are not seen.
    """
    known: dict[str, float] = {}
    found: list[tuple[int, float]] = []
    for number, raw in enumerate(lines, start=1):
        tokens = raw.split("#", 1)[0].split()
        if not tokens:
            continue
        cmd, args = tokens[0], [a.split("(")[0] for a in tokens[1:]]
        if cmd == "store_var" and len(args) >= 2:
            value = parse_literal(args[1])
            if value is not None:
                known[args[0]] = value
            continue
        indices: tuple[int, ...] = _POTENTIAL_ARGS.get(cmd, ())
        if cmd == "set_range_minmax" and args[:1] == ["da"]:
            indices = (1, 2)
        for i in indices:
            if i < len(args):
                value = parse_literal(args[i])
                value = known.get(args[i]) if value is None else value
                if value is not None:
                    found.append((number, value))
        if cmd == "meas_loop_dpv" and len(args) > 5:  # the pulse adds to the step potential
            pulse = parse_literal(args[5]) or known.get(args[5]) or 0.0
            ends = [v for n, v in found[-2:] if n == number]
            found += [(number, v + math.copysign(pulse, v)) for v in ends]
    return found


# ---------------------------------------------------------------- device


class MethodScriptDevice:
    """A MethodSCRIPT instrument on a (virtual) serial port."""

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self._run_lock = threading.Lock()
        self._running = threading.Event()
        self.info: dict[str, str] = {}

    # ------------------------------------------------------------ lines

    def _readline(self, timeout: float) -> str:
        return self.t.read(timeout).translate(_CONTROL_CHARS)  # drops CR and XON/XOFF characters

    @property
    def device_type(self) -> str:
        return self.info.get("model", "unknown")

    @property
    def running(self) -> bool:
        return self._running.is_set()

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        """``t`` (firmware version, multi-line, ends with ``*``) and ``i`` (serial number)."""
        if self._running.is_set():
            return dict(self.info)
        with self.t.lock:
            self.t.write("t")
            line = ""
            for _ in range(20):  # skip stray output (e.g. from a script started by someone else)
                line = self._readline(2.0)
                if line.startswith("t"):
                    break
            else:
                raise InstrumentProtocolError(
                    f"No reply to the firmware-version command 't' (last line {line!r})"
                )
            if "!" in line:
                raise InstrumentProtocolError(f"Instrument rejected 't': {line!r}")
            parts = [line[1:]]
            while not parts[-1].endswith("*"):
                parts.append(self._readline(2.0))
            self.t.write("i")
            serial = self._readline(2.0)
        head = parts[0]
        code = head[:6]
        version = head[6:].split("#", 1)[0]
        info = {
            "manufacturer": "PalmSens",
            "model": DEVICE_TYPES.get(code, f"unknown ({code})"),
            "device_code": code,
            "firmware": ".".join(version[:2])
            if len(version) == 2
            else f"{version[0]}.{version[1]}.{version[2:]}",
            "build": head.split("#", 1)[1].strip() if "#" in head else "",
            "release": parts[-1].rstrip("*").strip() or "R",
        }
        if serial.startswith("i") and "!" not in serial:
            info["serial"] = serial[1:]
        if code == "espbl":
            raise InstrumentProtocolError(
                "The EmStat Pico is in bootloader mode (firmware update). Power-cycle it to start the "
                "application firmware."
            )
        self.info = info
        return dict(info)

    # ------------------------------------------------------------ scripts

    def execute(self, script: list[str], timeout_s: float) -> ScriptResult:
        """Run a script (``e`` + lines + empty line) and collect its output until the final empty line.

        If the script is still running after ``timeout_s`` it is aborted with ``Z``.
        """
        lines = [ln.rstrip() for ln in script if ln.strip()]  # empty lines would end the script early
        if any(len(ln) >= 256 for ln in lines):
            raise ValueError("MethodSCRIPT lines are limited to 255 characters")
        if not self._run_lock.acquire(blocking=False):
            raise InstrumentError(
                "A measurement is already running. Wait for it to finish or call `abort_measurement`."
            )
        t0 = time.monotonic()
        try:
            self._running.set()
            with self.t.lock:
                self.t.write("e")
                for ln in lines:
                    self.t.write(ln)
                self.t.write("")
            result = self._collect(lines, timeout_s)
        finally:
            self._running.clear()
            self._run_lock.release()
        result.duration_s = time.monotonic() - t0
        return result

    def _collect(self, script: list[str], timeout_s: float) -> ScriptResult:
        result = ScriptResult()
        ack = ""
        ack_deadline = time.monotonic() + 10.0
        while not ack:  # the acknowledgement 'e' (or a parse error)
            if time.monotonic() > ack_deadline:
                raise InstrumentTimeout("The instrument did not acknowledge the script ('e').")
            try:
                ack = self._readline(1.0)
            except InstrumentTimeout:
                continue
        err = ERROR_RE.search(ack)
        if err:
            self._recover_after_error()
            code, line_no, col = int(err[1], 16), err[2], err[3]
            where = ""
            if line_no and line_no.isdigit() and 0 < int(line_no) <= len(script):
                where = f" at line {line_no}{f', column {col}' if col else ''} ({script[int(line_no) - 1].strip()!r})"
            raise InstrumentProtocolError(
                f"The instrument rejected the script{where}: error 0x{code:04X}, {error_text(code)}."
            )
        if ack != "e":
            raise InstrumentProtocolError(f"Unexpected reply to the script: {ack!r}")

        deadline = time.monotonic() + timeout_s
        abort_sent = False
        scan = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if abort_sent:
                    raise InstrumentTimeout("The script did not end within 15 s of being aborted.")
                self.t.write("Z")
                abort_sent = result.timed_out = True
                deadline = time.monotonic() + 15.0
                continue
            try:
                line = self._readline(min(0.5, remaining))
            except InstrumentTimeout:
                continue
            if line == "":
                break  # end of script
            result.lines.append(line)
            err = ERROR_RE.match(line)
            if err:
                code = int(err[1], 16)
                result.error = f"error 0x{code:04X} at script line {err[2] or '?'}: {error_text(code)}"
                self._recover_after_error()
                break
            head = line[0]
            if head == "P":
                result.packets.append((scan, parse_package(line)))
            elif head == "T":
                result.texts.append(line[1:])
            elif head == "C" and line[1:].isdigit():
                scan = int(line[1:])
            elif line in {"Z", "Y"}:
                result.aborted = True
        return result

    def _recover_after_error(self) -> None:
        # The instrument ignores input for ~50-100 ms after an error (protocol manual ch. 8).
        time.sleep(0.15)
        self.t.flush_input()

    # ------------------------------------------------------------ abort

    def abort(self) -> dict[str, object]:
        """Abort a running script (``Z``). Scripts built by this driver switch the cell off in
        ``on_finished:``; if nothing was running, a one-line ``cell_off`` script is run as well."""
        if self._running.is_set():
            self.t.write("Z")
            end = time.monotonic() + 20.0
            while self._running.is_set() and time.monotonic() < end:
                time.sleep(0.05)
            return {"was_running": True, "stopped": not self._running.is_set()}
        was_running = False
        with self.t.lock:
            self.t.write("")  # flush a partially received command, as PalmSens' abort_and_sync does
            self.t.write("Z")
            end = time.monotonic() + 3.0
            reply = ""
            while time.monotonic() < end:
                try:
                    reply = self._readline(0.5)
                except InstrumentTimeout:
                    continue
                if reply.startswith("Z"):
                    break
            if reply == "Z":  # a script (not started by this server) was running: drain its output
                was_running = True
                end = time.monotonic() + 30.0
                while time.monotonic() < end:
                    try:
                        if self._readline(0.5) == "":
                            break
                    except InstrumentTimeout:
                        continue
            else:  # 'Z!0006': nothing was running
                self._recover_after_error()
        note = "cell switched off"
        try:
            res = self.execute(["cell_off"], timeout_s=5.0)
            if res.error:
                note = f"cell_off script reported {res.error}"
        except InstrumentError as exc:
            note = f"could not run cell_off: {exc}"
        return {"was_running": was_running, "stopped": True, "note": note}

    def close(self) -> None:
        self.t.close()
