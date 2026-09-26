"""Driver for Thermo Fisher Orbitrap mass spectrometers through the Instrument API (IAPI).

Protocol reference: the official IAPI repository https://github.com/thermofisherlsms/iapi
(README, GettingStarted.md, interface XML documentation in ``lib/`` and the example programs in
``examples/``; checked at commit c246dcc8772d03c9c32e9b2fde486e97572c8fbf). The .NET calls
themselves live in :mod:`labmcp_thermo_iapi.pythonnet_backend`; this module holds the logic
that is identical for the real backend and the simulator:

* a ring buffer filled from ``IMsScanContainer.MsScanArrived`` so tools can read recent scans
  and wait for new ones without blocking the instrument's event thread;
* validation of custom / repeating scan values against ``IScans.PossibleParameters``, using the
  ``IParameterDescription.Selection`` grammar documented in ``lib/API-2.0.xml``
  (``""``, ``"string"``, ``"num1-num2"``, ``"num1.frac-num2.frac"``, ``"sel1,sel2,..."``).
  IAPI itself silently ignores illegal values ("Illegal values will be ignored, values out of
  range will not be accepted", ``IScanDefinition.Values``), so the driver refuses them up front;
* a sliding-window counter for the custom-scan rate limit.

No MCP code lives here.

Header keys used for convenience fields (``Scan``, ``MSOrder``, ``ScanMode``,
``PrecursorMass[0]``) and the trailer key ``Access Id:`` come from the repository's examples
(``examples/tribrid/*``, ``examples/Exploris/5 PlacingScans``). Other trailer names
(``Master Scan Number:``, ``AGC Target:``, ``Ion Injection Time (ms):``) are the names Thermo
raw files use but are **not** documented in the IAPI repository; they are read if present and
the full header/trailer dictionaries are always returned as sent by the instrument.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from labmcp import AuditLog, InstrumentProtocolError

from labmcp_thermo_iapi.backend import OrbitrapBackend, ParameterDescription, ScanRecord

_INT_RANGE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$")
_FLOAT_RANGE = re.compile(r"^\s*(-?\d+\.\d+)\s*-\s*(-?\d+\.\d+)\s*$")


@dataclass(frozen=True)
class Selection:
    kind: str  # "none" | "string" | "int" | "float" | "choice"
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()


def parse_selection(selection: str) -> Selection:
    """Parse an ``IParameterDescription.Selection`` string (grammar in lib/API-2.0.xml)."""
    s = (selection or "").strip()
    if s == "":
        return Selection("none")
    if s == "string":
        return Selection("string")
    m = _FLOAT_RANGE.match(s)
    if m:
        return Selection("float", float(m.group(1)), float(m.group(2)))
    m = _INT_RANGE.match(s)
    if m:
        return Selection("int", float(m.group(1)), float(m.group(2)))
    return Selection("choice", choices=tuple(c.strip() for c in s.split(",") if c.strip()))


def _finite(text: str) -> float | None:
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _check_one(name: str, value: str, sel: Selection) -> tuple[str | None, str]:
    """Return ``(error or None, value in the instrument's own spelling)``.

    IAPI silently ignores values it doesn't recognise, so a value accepted here is sent exactly
    as the instrument lists it: ``hcd`` -> ``HCD``, ``60000.0`` -> ``60000`` for a choice list,
    ``2.0`` -> ``2`` for an integer range.
    """
    if sel.kind == "string":
        return None, value
    if sel.kind == "none":
        return (None, value) if value == "" else (f"{name} takes no value (got {value!r})", value)
    if sel.kind == "choice":
        if value in sel.choices:
            return None, value
        lowered = {c.lower(): c for c in sel.choices}
        if value.lower() in lowered:
            return None, lowered[value.lower()]
        number = _finite(value)
        if number is not None:
            for choice in sel.choices:
                if _finite(choice) == number:
                    return None, choice
        return f"{name}={value!r} is not one of the allowed values: {', '.join(sel.choices)}", value
    assert sel.low is not None and sel.high is not None
    number = _finite(value)
    if number is None:
        return f"{name}={value!r} is not a number (allowed {sel.low:g} to {sel.high:g})", value
    if sel.kind == "int":
        if number != int(number):
            return f"{name}={value!r} must be an integer (allowed {sel.low:g} to {sel.high:g})", value
        value = str(int(number))
    if not sel.low <= number <= sel.high:
        return f"{name}={value!r} is outside the instrument's range {sel.low:g} to {sel.high:g}", value
    return None, value


def validate_scan_values(values: dict[str, str], possible: list[ParameterDescription]) -> dict[str, str]:
    """Check every value against ``PossibleParameters``; raise listing every problem.

    Returns the values keyed by the instrument's parameter names and spelled as the instrument
    lists them. Multi-valued parameters (e.g. ``ActivationType = "CID;HCD"`` or
    ``IsolationWidth = "1.2;2.0"``, as used in the repository's scan-handler example) are
    checked element by element, splitting on ``;`` (and on ``,`` unless the selection is a
    choice list).
    """
    if not possible:
        raise InstrumentProtocolError(
            "Refused: the instrument has not reported its possible scan parameters "
            "(IScans.PossibleParameters is empty), so the scan cannot be validated. Nothing was "
            "sent. Check that the instrument is connected and in On mode, then try again."
        )
    by_name = {p.name: p for p in possible}
    by_lower = {p.name.lower(): p for p in possible}
    problems: list[str] = []
    out: dict[str, str] = {}
    for key, raw in values.items():
        p = by_name.get(key) or by_lower.get(key.lower())
        if p is None:
            problems.append(f"{key!r} is not a parameter this instrument accepts")
            continue
        sel = parse_selection(p.selection)
        value = str(raw).strip()
        err: str | None
        if sel.kind in ("string", "none"):
            err, value = _check_one(p.name, value, sel)
        elif not value:
            err = f"{p.name} is empty"
        else:
            # "-1" marks "use the default" in multi-valued lists (e.g. IsolationWidth "3;-1,-1,-1"
            # in the FusionExampleClient2pt0 example).
            tokens = re.split(r"(;)" if sel.kind == "choice" else r"([;,])", value)
            errs: list[str] = []
            for i in range(0, len(tokens), 2):
                part = tokens[i].strip()
                if part == "-1":
                    tokens[i] = part
                    continue
                e, tokens[i] = _check_one(p.name, part, sel)
                if e:
                    errs.append(e)
            err = errs[0] if errs else None
            value = "".join(tokens)
        if err:
            problems.append(err)
        out[p.name] = value
    if problems:
        raise InstrumentProtocolError(
            "Refused: invalid scan parameters, nothing was sent to the instrument: "
            + "; ".join(problems)
            + ". Call get_possible_scan_parameters for the names and ranges this instrument accepts."
        )
    return out


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _lookup(d: dict[str, str], *names: str) -> str | None:
    lowered = {k.lower().rstrip(": ").strip(): v for k, v in d.items()}
    for n in names:
        v = lowered.get(n.lower().rstrip(": ").strip())
        if v is not None and v != "":
            return v
    return None


def scan_ms_order(rec: ScanRecord) -> int | None:
    v = _lookup(rec.header, "MSOrder")
    if v is None:
        return None
    m = re.search(r"\d+", v)
    return int(m.group()) if m else None


def scan_access_id(rec: ScanRecord) -> int | None:
    v = _number(_lookup(rec.trailer, "Access Id:", "Access ID"))
    return None if v is None else int(v)


def summarize_scan(rec: ScanRecord) -> dict[str, Any]:
    """Convenience fields extracted from the header/trailer (None when not reported)."""
    scan_no = _number(_lookup(rec.header, "Scan", "ScanNumber"))
    master = _number(_lookup(rec.trailer, "Master Scan Number:", "Master Scan Number"))
    return {
        "scan_number": None if scan_no is None else int(scan_no),
        "ms_order": scan_ms_order(rec),
        "scan_mode": _lookup(rec.header, "ScanMode"),
        "precursor_mz": _number(_lookup(rec.header, "PrecursorMass[0]")),
        "master_scan_number": None if master is None or master <= 0 else int(master),
        "access_id": scan_access_id(rec),
        "agc_target": _number(_lookup(rec.trailer, "AGC Target:")),
        "injection_time_ms": _number(_lookup(rec.trailer, "Ion Injection Time (ms):")),
    }


class SlidingWindow:
    """Thread-safe count of events in the last ``window_s`` seconds (monotonic clock).

    The server keeps one for the whole process and hands it to every driver it creates, so
    `reconnect` does not reset the custom-scan rate limit.
    """

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = window_s
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def count(self) -> int:
        now = time.monotonic()
        with self._lock:
            while self._times and now - self._times[0] > self.window_s:
                self._times.popleft()
            return len(self._times)

    def add(self) -> None:
        with self._lock:
            self._times.append(time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._times.clear()


class OrbitrapDriver:
    """Owns a backend, buffers its scans and enforces pre-send validation."""

    def __init__(
        self,
        backend: OrbitrapBackend,
        *,
        audit: AuditLog | None = None,
        buffer_size: int = 500,
        custom_scan_window: SlidingWindow | None = None,
    ) -> None:
        self.backend = backend
        self.audit = audit
        self._buffer: deque[ScanRecord] = deque(maxlen=max(10, buffer_size))
        self._cond = threading.Condition()
        self._sequence = 0
        self._custom_scans = custom_scan_window if custom_scan_window is not None else SlidingWindow(60.0)
        # running number -> last scan sequence when that custom scan was placed, so its result can
        # be picked up even if it arrived before wait_for_scan was called.
        self._custom_placed: OrderedDict[int, int] = OrderedDict()
        self._running_number = 0
        self.lock = threading.RLock()
        self._params_cache: list[ParameterDescription] | None = None
        backend.open(self._on_scan)
        self._event(f"connected ({backend.kind})")

    # ------------------------------------------------------------------ plumbing

    def _event(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, "thermo-iapi")

    def _on_scan(self, rec: ScanRecord) -> None:
        with self._cond:
            self._sequence += 1
            rec.sequence = self._sequence
            self._buffer.append(rec)
            self._cond.notify_all()

    @property
    def buffered_count(self) -> int:
        with self._cond:
            return len(self._buffer)

    @property
    def last_sequence(self) -> int:
        with self._cond:
            return self._sequence

    def identify(self) -> dict[str, str]:
        return self.backend.identify()

    def status(self) -> dict[str, Any]:
        st = self.backend.status()
        with self._cond:
            st["scans_received"] = self._sequence
            st["scans_buffered"] = len(self._buffer)
            st["last_scan_at"] = self._buffer[-1].received_at if self._buffer else None
            st["last_status_log"] = next((r.status_log for r in reversed(self._buffer) if r.status_log), {})
        return st

    # --------------------------------------------------------------------- scans

    def recent_scans(
        self, count: int, ms_order: int | None = None, access_id: int | None = None
    ) -> list[ScanRecord]:
        with self._cond:
            items = list(self._buffer)
        out = [
            r
            for r in reversed(items)
            if (ms_order is None or scan_ms_order(r) == ms_order)
            and (access_id is None or scan_access_id(r) == access_id)
        ]
        return list(reversed(out[:count]))

    def wait_for_scan(
        self,
        timeout_s: float,
        *,
        ms_order: int | None = None,
        access_id: int | None = None,
        after_sequence: int | None = None,
    ) -> ScanRecord | None:
        """Wait for a scan newer than ``after_sequence`` matching the filters.

        ``after_sequence`` defaults to now, except for the access id of a custom scan placed
        through this driver: its result may already have arrived (a scan takes milliseconds, the
        next tool call seconds), so the default is the moment the scan was placed.
        """
        if after_sequence is None and access_id is not None:
            with self._cond:
                after_sequence = self._custom_placed.get(access_id)
        start = self.last_sequence if after_sequence is None else after_sequence
        deadline = time.monotonic() + timeout_s
        seen = start
        with self._cond:
            while True:
                for r in self._buffer:
                    if r.sequence <= seen:
                        continue
                    if (ms_order is None or scan_ms_order(r) == ms_order) and (
                        access_id is None or scan_access_id(r) == access_id
                    ):
                        return r
                seen = self._sequence
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ------------------------------------------------------------ scan parameters

    def possible_parameters(self, refresh: bool = False) -> list[ParameterDescription]:
        with self.lock:
            if refresh or not self._params_cache:
                self._params_cache = self.backend.possible_parameters()
            return list(self._params_cache)

    def validate(self, values: dict[str, str]) -> dict[str, str]:
        return validate_scan_values(values, self.possible_parameters())

    def selection_of(self, name: str) -> Selection | None:
        for p in self.possible_parameters():
            if p.name.lower() == name.lower():
                return parse_selection(p.selection)
        return None

    def custom_scans_in_window(self) -> int:
        """Custom scans placed in the rate-limit window (60 s), including before a reconnect."""
        return self._custom_scans.count()

    def next_running_number(self) -> int:
        with self.lock:
            self._running_number = self._running_number % 2_000_000_000 + 1
            return self._running_number

    def set_custom_scan(
        self, values: dict[str, str], running_number: int, single_processing_delay_s: float = 0.0
    ) -> bool:
        """Send an already-validated custom scan and count it for the rate limit."""
        with self.lock:
            self._custom_scans.add()  # counted even if the call fails: it may have reached Tune
            with self._cond:
                self._custom_placed[running_number] = self._sequence
                self._custom_placed.move_to_end(running_number)
                while len(self._custom_placed) > 1000:
                    self._custom_placed.popitem(last=False)
            return self._do(
                f"SetCustomScan(RunningNumber={running_number}, {values})",
                lambda: self.backend.set_custom_scan(
                    values, running_number=running_number, single_processing_delay_s=single_processing_delay_s
                ),
            )

    def set_repeating_scan(self, values: dict[str, str], running_number: int) -> bool:
        with self._cond:  # a repeating scan's access id means "the next one", not "since placed"
            self._custom_placed.pop(running_number, None)
        return self._do(
            f"SetRepetitionScan(RunningNumber={running_number}, {values})",
            lambda: self.backend.set_repeating_scan(values, running_number=running_number),
        )

    # Cancels don't wait for the driver lock: a stop must not queue behind another control call.
    def cancel_custom_scan(self) -> bool:
        return self._do("CancelCustomScan()", self.backend.cancel_custom_scan, locked=False)

    def cancel_repeating_scan(self) -> bool:
        return self._do("CancelRepetition()", self.backend.cancel_repeating_scan, locked=False)

    # --------------------------------------------------------------- acquisition

    def _do(self, what: str, fn: Callable[[], Any], *, locked: bool = True) -> Any:
        """Run one IAPI control call and record it (and its result or error) in the command log."""
        try:
            if locked:
                with self.lock:
                    result = fn()
            else:
                result = fn()
        except Exception as exc:
            self._event(f"{what} -> error: {exc}")
            raise
        self._event(what if result is None else f"{what} -> {result}")
        return result

    def start_acquisition(self, mode: str, **kwargs: Any) -> None:
        self._do(
            f"StartAcquisition({mode}, {kwargs})", lambda: self.backend.start_acquisition(mode, **kwargs)
        )

    def pause_acquisition(self) -> None:
        self._do("Pause()", self.backend.pause_acquisition)

    def resume_acquisition(self) -> None:
        self._do("Resume()", self.backend.resume_acquisition)

    def cancel_acquisition(self) -> None:
        self._do("CancelAcquisition()", self.backend.cancel_acquisition)

    def set_standby(self) -> None:
        self._do("SetMode(Standby)", self.backend.set_standby)

    def close(self) -> None:
        self.backend.close()
