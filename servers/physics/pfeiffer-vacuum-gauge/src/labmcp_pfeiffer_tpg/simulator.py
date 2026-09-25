"""Byte-level simulator of a Pfeiffer TPG gauge controller (mnemonics protocol).

Reproduces the framing from the communication manuals: ``<ACK><CR><LF>`` / ``<NAK><CR><LF>``
after each CR-terminated mnemonic, data only after ``<ENQ>``, the ERROR word after a NAK, ``<ETX>``
clearing the input buffer, and one stale line from the power-up measurement stream.

The vacuum system: a chamber pumping down from 8e-5 mbar towards 2e-7 mbar (time constant 10 min)
and a foreline at ~2.4e-2 mbar. Gauges report under/overrange outside their measurement ranges;
ionisation gauges that are switched off report status 4.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from labmcp import ByteSimulator

from labmcp_pfeiffer_tpg.driver import MBAR_PER_UNIT, MODELS, UNITS_26X, UNITS_36X

# Measurement ranges (mbar) used by the simulator; logarithmic gauges have 2 significant decimals.
_RANGES = {
    "PKR": (5e-9, 1000.0, True),
    "TPR": (5e-4, 1000.0, True),
    "IKR": (2e-9, 1e-2, True),
    "CMR": (0.1, 1100.0, False),
}
_ID_26X = {"PKR": "PKR", "TPR": "TPR", "IKR": "IKR9", "CMR": "CMR", None: "noSEn"}
_ID_36X = {"PKR": "PKR", "TPR": "TPR/PCR", "IKR": "IKR", "CMR": "CMR/APR", None: "noSENSOR"}
_LAYOUT = {
    "TPG361": [("PKR", "chamber", True)],
    "TPG362": [("PKR", "chamber", True), ("TPR", "foreline", True)],
    "TPG366": [
        ("PKR", "chamber", True),
        ("TPR", "foreline", True),
        ("IKR", "chamber", False),
        ("CMR", "chamber", True),
        (None, "", False),
        (None, "", False),
    ],
    "TPG261": [("PKR", "chamber", True), (None, "", False)],
    "TPG262": [("PKR", "chamber", True), ("TPR", "foreline", True)],
}
_SWITCHABLE = {"PKR", "IKR", "PBR", "IMR"}


@dataclass
class _Gauge:
    kind: str | None
    node: str
    on: bool


class TPGSimulator(ByteSimulator):
    def __init__(
        self,
        model: str = "TPG362",
        seed: int | None = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = MODELS[model]
        self.family = self.spec.family
        self.units = UNITS_36X if self.family == "36x" else UNITS_26X
        self.unit = 4 if self.family == "36x" else 0  # factory default hPa (36x) / mbar (26x)
        self.rng = random.Random(seed)
        self.clock = clock
        self.t0 = clock()
        self.gauges = [_Gauge(k, n, on) for k, n, on in _LAYOUT[model]]
        self.error = [0, 0, 0, 0]  # ERROR, NO HWR, PAR, SYN
        self.last: tuple[str, list[str]] | None = None
        self.buf = b""
        self.streaming = True
        self.log: list[str] = []

    # ------------------------------------------------------------ physics

    def node_mbar(self, node: str) -> float:
        t = self.clock() - self.t0
        if node == "foreline":
            return 2.4e-2 * (1 + self.rng.gauss(0, 0.01))
        return (2e-7 + 8e-5 * math.exp(-t / 600.0)) * (1 + self.rng.gauss(0, 0.02))

    def _fmt(self, value: float, log: bool) -> str:
        if log:
            mant, exp = f"{value:.2E}".split("E")
            return f"{mant}00E{exp}"
        return f"{value:.4E}"

    def _measure(self, g: _Gauge) -> str:
        if g.kind is None:
            return "5,2.0000E-2"  # documented "no sensor" output
        if not g.on:
            return "4,0.0000E+00"
        lo, hi, log = _RANGES[g.kind]
        p = self.node_mbar(g.node)
        status = 1 if p < lo else 2 if p > hi else 0
        p = min(max(p, lo), hi)
        unit = self.units[self.unit]
        value = 0.6 * math.log10(p) + 6.8 if unit == "V" else p / MBAR_PER_UNIT[unit]
        return f"{status},{self._fmt(value, log and unit != 'V')}"

    # ------------------------------------------------------------ framing

    def handle_bytes(self, data: bytes) -> bytes:
        out = b""
        if self.streaming:  # a measurement line that was already on its way at power-up
            self.streaming = False
            out += (",".join(self._measure(g) for g in self.gauges) + "\r\n").encode()
        for byte in data:
            if byte == 0x03:  # ETX: clear the input buffer
                self.buf = b""
            elif byte == 0x05:  # ENQ
                out += (self._enquiry() + "\r\n").encode()
            elif byte == 0x0D:
                line, self.buf = self.buf.decode("ascii", "replace"), b""
                out += b"\x06\r\n" if self._command(line) else b"\x15\r\n"
            elif byte != 0x0A:
                self.buf += bytes([byte])
        return out

    def _error_word(self) -> str:
        word, self.error = "".join(str(b) for b in self.error), [0, 0, 0, 0]
        return word

    def _command(self, line: str) -> bool:
        text = line.replace(" ", "").upper()
        self.log.append(text)
        mnem, rest = text[:3], text[3:]
        if rest and not rest.startswith(","):
            return self._fail(syntax=True)
        params = rest[1:].split(",") if rest else []
        n = self.spec.channels
        ok = True
        if mnem in {f"PR{i}" for i in range(1, n + 1)} or mnem in {"TID", "ERR", "PNR"}:
            ok = not params
        elif mnem == "PRX":
            ok = not params and n > 1
        elif mnem == "AYT":
            ok = not params and self.family == "36x"
        elif mnem == "UNI":
            if len(params) > 1 or (params and (not params[0].isdigit() or int(params[0]) not in self.units)):
                return self._fail(syntax=len(params) > 1)
            if params:
                self.unit = int(params[0])
        elif mnem == "SEN":
            if params and (len(params) != n or any(p not in {"0", "1", "2"} for p in params)):
                return self._fail(syntax=len(params) != n)
            for g, p in zip(self.gauges, params or ["0"] * n, strict=True):
                if p != "0" and g.kind in _SWITCHABLE:
                    g.on = p == "2"
        else:
            ok = False
        if not ok:
            return self._fail(syntax=True)
        self.last = (mnem, params)
        return True

    def _fail(self, syntax: bool) -> bool:
        self.error[3 if syntax else 2] = 1
        self.last = None
        return False

    def _enquiry(self) -> str:
        if self.last is None:
            return self._error_word()
        mnem, _ = self.last
        if mnem.startswith("PR") and mnem[2:].isdigit():
            return self._measure(self.gauges[int(mnem[2:]) - 1])
        if mnem == "PRX":
            return ",".join(self._measure(g) for g in self.gauges)
        if mnem == "TID":
            ids = _ID_36X if self.family == "36x" else _ID_26X
            return ",".join(ids[g.kind] for g in self.gauges)
        if mnem == "SEN":
            return ",".join("0" if g.kind not in _SWITCHABLE else ("2" if g.on else "1") for g in self.gauges)
        if mnem == "UNI":
            return str(self.unit)
        if mnem == "ERR":
            return self._error_word()
        if mnem == "PNR":
            return "010400" if self.family == "36x" else "302-510-D"
        if mnem == "AYT":
            part = {"TPG361": "PTG28040", "TPG362": "PTG28290", "TPG366": "PTG28770"}[self.spec.model]
            return f"{self.spec.model},{part},44990000,010400,010100"
        return self._error_word()
