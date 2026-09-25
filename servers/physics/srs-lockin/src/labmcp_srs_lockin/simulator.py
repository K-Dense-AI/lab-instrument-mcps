"""Wire-level simulator of an SRS lock-in amplifier (SR830 or SR860 dialect).

The simulated sample is a mechanical/electrical resonator (f0 = 10 kHz, Q = 50) driven by the
sine output and read back on input A, plus a small amount of white noise. The output filter is a
single-pole low-pass with the programmed time constant, so readings taken right after a change
have not settled yet - exactly as on the real instrument.

Error behaviour follows the manuals: set commands have no reply; an unknown command sets bit 5
(CMD) and an out-of-range parameter sets bit 4 (EXE) of the Standard Event Status byte, which the
driver reads with ``*ESR?``.
"""

from __future__ import annotations

import cmath
import math
import random
import re
import time
from collections.abc import Callable

from labmcp import LineSimulator

from labmcp_srs_lockin.driver import MODELS, SR8X0, SR86X, SR86X_INPUT_RANGES_V

_CMD_RE = re.compile(r"^(\*?[A-Z]+)(\?)?\s*(.*)$")

F0_HZ = 10_000.0
Q = 50.0
GAIN = 2e-4  # |H| at DC; peak |H| = GAIN * Q = 0.01
EXT_FREQUENCY_HZ = 1234.5
EXT_SIGNAL_V = 5e-3
NOISE_V_PER_RTHZ = 20e-9


class _Bad(Exception):
    """Parameter out of range -> EXE bit."""


class _Unknown(Exception):
    """Unknown command -> CMD bit."""


class SRSLockInSimulator(LineSimulator):
    def __init__(
        self,
        model: str = "SR830",
        seed: int | None = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = MODELS[model.upper()]
        self.family = self.spec.family
        self.rng = random.Random(seed)
        self.clock = clock
        self.esr = 128  # PON: set by power-on
        self.lias = 0
        self.reset()

    def reset(self) -> None:
        """Standard settings (SR830 manual p. 4-3, SR860 manual p. 64)."""
        self.internal = True
        self.harmonic = 1
        self.phase = 0.0
        if self.family == SR8X0:
            self.freq = 1000.0
            self.amplitude = 1.0
            self.sens = 26  # 1 V
            self.oflt = 8  # 100 ms
            self.ofsl = 1  # 12 dB/oct
        else:
            self.freq = 100_000.0
            self.amplitude = 1e-9
            self.sens = 0  # 1 V
            self.oflt = 10  # 100 ms
            self.ofsl = 0  # 6 dB/oct
        self.dc_level = 0.0
        self.reserve = 2  # SR830: low noise
        self.isrc = 0
        self.ivmd = 0
        self.icur = 0
        self.irng = 0
        self.icpl = 0
        self.ignd = 0
        self.sync = 0
        self.outx = 1
        self._out = complex(0.0, 0.0)
        self._t_last = self.clock()

    # ------------------------------------------------------------ physics

    @property
    def tc(self) -> float:
        return self.spec.time_constants_s[self.oflt]

    @property
    def sensitivity(self) -> float:
        return self.spec.sensitivities_v[self.sens]

    def _current_mode(self) -> bool:
        return self.isrc >= 2 if self.family == SR8X0 else self.ivmd == 1

    def ref_frequency(self) -> float:
        return self.freq if self.internal else EXT_FREQUENCY_HZ

    def _input_phasor(self) -> complex:
        """Steady-state signal at the detection frequency, in V rms (before the reference phase)."""
        f = self.ref_frequency()
        if not self.internal:
            sig = cmath.rect(EXT_SIGNAL_V, math.radians(30.0)) if self.harmonic == 1 else 0j
        elif self.harmonic == 1:
            x = f / F0_HZ
            sig = self.amplitude * GAIN / complex(1 - x * x, x / Q)
        else:
            sig = 0j
        if self.icpl == 0:  # ac coupling: ~160 mHz high-pass
            fd = f * self.harmonic
            sig *= complex(0, fd) / complex(0.16, fd)
        if self._current_mode():
            sig /= 10e3  # resonator read through a 10 kΩ transimpedance -> amperes
        return sig

    def _output(self) -> complex:
        """Filtered X + iY (single-pole low-pass with the programmed time constant)."""
        now = self.clock()
        dt = max(now - self._t_last, 0.0)
        self._t_last = now
        target = self._input_phasor() * cmath.exp(-1j * math.radians(self.phase))
        self._out += (target - self._out) * (1 - math.exp(-dt / self.tc))
        slope = (6, 12, 18, 24)[self.ofsl]
        enbw = {6: 1 / 4, 12: 1 / 8, 18: 3 / 32, 24: 5 / 64}[slope] / self.tc
        sigma = NOISE_V_PER_RTHZ * math.sqrt(enbw) * (1e-6 if self._current_mode() else 1.0)
        out = self._out + complex(self.rng.gauss(0, sigma), self.rng.gauss(0, sigma))
        self._update_overloads(out)
        return out

    def _update_overloads(self, out: complex) -> None:
        full_scale = self.sensitivity * (1e-6 if self._current_mode() else 1.0)
        over = abs(out) > full_scale
        raw = abs(self._input_phasor())
        if self.family == SR8X0:
            if over:
                self.lias |= 1 << 2
            if not self._current_mode() and raw > 1.0:
                self.lias |= 1 << 0
        else:
            if over:
                self.lias |= 0b11
            if not self._current_mode() and raw > SR86X_INPUT_RANGES_V[self.irng]:
                self.lias |= 1 << 4

    # ------------------------------------------------------------ protocol

    def handle(self, command: str) -> str | list[str] | None:
        replies = [r for r in (self._one(part) for part in command.split(";")) if r is not None]
        if not replies:
            return None
        return replies if self.family == SR8X0 else ";".join(replies)

    def _one(self, cmd: str) -> str | None:
        text = cmd.strip().upper()
        if not text:
            return None
        if self.family == SR8X0:
            text = text.replace(" ", "")  # SR830: any number of embedded spaces is allowed
        m = _CMD_RE.match(text)
        if not m:
            self.esr |= 32
            return None
        head, query, arg = m[1], bool(m[2]), m[3].strip()
        args = [a.strip() for a in arg.split(",")] if arg else []
        try:
            return self._dispatch(head, query, args)
        except _Unknown:
            self.esr |= 32
        except (_Bad, ValueError, IndexError):
            self.esr |= 16
        return None

    def _fmt(self, v: float) -> str:
        return f"{v:.6g}" if self.family == SR8X0 else f"{v:.10e}"

    def _dispatch(self, head: str, query: bool, args: list[str]) -> str | None:
        sr830 = self.family == SR8X0
        # common commands ------------------------------------------------
        if head == "*IDN" and query:
            if sr830:
                return f"Stanford_Research_Systems,{self.spec.model},s/n86025,ver1.07"
            return f"Stanford_Research_Systems,{self.spec.model},003456,v1.55"
        if head == "*CLS":
            self.esr = 0
            self.lias = 0
            return None
        if head == "*RST":
            self.reset()
            return None
        if head == "*ESR" and query:
            value, self.esr = self.esr, 0
            return str(value)
        if head == "*STB" and query:
            return "3" if sr830 else "0"  # SR830: bit 0 no scan, bit 1 no command in progress
        if head == "*OPC" and query and not sr830:
            return "1"
        if head == "LIAS" and query:
            value, self.lias = self.lias, 0
            return str(value)
        if head == "OUTX" and sr830:
            return self._set_int(args, "outx", 0, 1, query)

        # reference ------------------------------------------------------
        if head == "FREQ":
            if query:
                return self._fmt(self.ref_frequency())
            if not self.internal:
                raise _Bad()  # only allowed with the internal reference
            f = float(args[0])
            if not (self.spec.min_frequency_hz <= f <= self.spec.max_frequency_hz):
                raise _Bad()
            if f * self.harmonic > self.spec.max_frequency_hz:
                raise _Bad()
            self.freq = f
            return None
        if head == ("FMOD" if sr830 else "RSRC"):
            if query:
                return str((1 if self.internal else 0) if sr830 else (0 if self.internal else 1))
            i = int(args[0])
            if sr830:
                if i not in (0, 1):
                    raise _Bad()
                self.internal = i == 1
            else:
                if i not in (0, 1, 2, 3):
                    raise _Bad()
                self.internal = i != 1
            return None
        if head == "HARM":
            if query:
                return str(self.harmonic)
            n = int(args[0])
            if (
                not (1 <= n <= self.spec.max_harmonic)
                or n * self.ref_frequency() > self.spec.max_frequency_hz
            ):
                raise _Bad()
            self.harmonic = n
            return None
        if head == "PHAS":
            if query:
                return self._fmt(self.phase)
            p = float(args[0])
            lo, hi = (-360.0, 729.99) if sr830 else (-360000.0, 360000.0)
            if not (lo <= p <= hi):
                raise _Bad()
            self.phase = (p + 180.0) % 360.0 - 180.0
            return None
        if head == "SLVL":
            if query:
                return self._fmt(self.amplitude)
            v = float(args[0])
            if not (self.spec.min_amplitude_v <= v <= self.spec.max_amplitude_v):
                raise _Bad()
            self.amplitude = round(v / 0.002) * 0.002 if sr830 else float(f"{v:.3g}")
            return None
        if head == "SOFF" and not sr830:
            if query:
                return self._fmt(self.dc_level)
            v = float(args[0])
            if not (-5.0 <= v <= 5.0):
                raise _Bad()
            self.dc_level = v
            return None

        # gain / filter --------------------------------------------------
        if head == ("SENS" if sr830 else "SCAL"):
            return self._set_int(args, "sens", 0, len(self.spec.sensitivities_v) - 1, query)
        if head == "OFLT":
            return self._set_int(args, "oflt", 0, len(self.spec.time_constants_s) - 1, query)
        if head == "OFSL":
            return self._set_int(args, "ofsl", 0, 3, query)
        if head == "SYNC":
            return self._set_int(args, "sync", 0, 1, query)
        if head == "RMOD" and sr830:
            return self._set_int(args, "reserve", 0, 2, query)

        # input ----------------------------------------------------------
        if head == "ISRC":
            return self._set_int(args, "isrc", 0, 3 if sr830 else 1, query)
        if head == "ICPL":
            return self._set_int(args, "icpl", 0, 1, query)
        if head == "IGND":
            return self._set_int(args, "ignd", 0, 1, query)
        if head == "IVMD" and not sr830:
            return self._set_int(args, "ivmd", 0, 1, query)
        if head == "ICUR" and not sr830:
            return self._set_int(args, "icur", 0, 1, query)
        if head == "IRNG" and not sr830:
            return self._set_int(args, "irng", 0, 4, query)

        # data -----------------------------------------------------------
        if head == "OUTP" and query:
            return self._fmt(self._param(int(args[0])))
        if head == "SNAP" and query:
            idx = [int(a) for a in args]
            if not (2 <= len(idx) <= (6 if sr830 else 3)):
                raise _Bad()
            out = self._output()
            return ",".join(self._fmt(self._param(i, out)) for i in idx)
        if head == "OAUX" and query:
            j = int(args[0])
            if j not in (range(1, 5) if sr830 else range(4)):
                raise _Bad()
            return self._fmt(0.0012 * j + self.rng.gauss(0, 3e-4))

        # auto functions -------------------------------------------------
        if head == "APHS":
            sig = self._input_phasor()
            if abs(sig) > 0:
                self.phase = math.degrees(cmath.phase(sig))
            return None
        if head == ("AGAN" if sr830 else "ASCL"):
            r = abs(self._input_phasor()) * (1e6 if self._current_mode() else 1.0)
            fits = [(v, i) for i, v in enumerate(self.spec.sensitivities_v) if v >= 1.25 * r]
            self.sens = min(fits)[1] if fits else self.sens
            return None
        if head == "ARSV" and sr830:
            return None
        if head == "ARNG" and not sr830:
            r = abs(self._input_phasor())
            fits = [(v, i) for i, v in enumerate(SR86X_INPUT_RANGES_V) if v >= 1.2 * r]
            self.irng = min(fits)[1] if fits else 0
            return None
        raise _Unknown()

    def _param(self, i: int, out: complex | None = None) -> float:
        """Value of an OUTP?/SNAP? parameter index for this family."""
        if self.family == SR86X:
            if i == 15:
                return self.freq
            if i == 16:
                return EXT_FREQUENCY_HZ
            if i == 13:
                return self.amplitude
            table = {0: "x", 1: "y", 2: "r", 3: "t", 4: "a1", 5: "a2", 6: "a3", 7: "a4"}
        else:
            if i == 9:
                return self.ref_frequency()
            table = {1: "x", 2: "y", 3: "r", 4: "t", 5: "a1", 6: "a2", 7: "a3", 8: "a4"}
        name = table.get(i)
        if name is None:
            raise _Bad()
        if name.startswith("a"):
            return 0.0012 * int(name[1]) + self.rng.gauss(0, 3e-4)
        z = self._output() if out is None else out
        return {"x": z.real, "y": z.imag, "r": abs(z), "t": math.degrees(cmath.phase(z))}[name]

    def _set_int(self, args: list[str], attr: str, lo: int, hi: int, query: bool) -> str | None:
        if query:
            return str(getattr(self, attr))
        i = int(args[0])
        if not (lo <= i <= hi):
            raise _Bad()
        setattr(self, attr, i)
        return None
