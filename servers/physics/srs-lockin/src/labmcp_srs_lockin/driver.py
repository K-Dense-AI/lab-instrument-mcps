"""Driver for Stanford Research Systems DSP lock-in amplifiers.

Two command dialects are implemented, each verified against the vendor manual:

* **SR810 / SR830** (``sr8x0``): "SR830 DSP Lock-In Amplifier" user manual, revision 2.5 (10/2011),
  chapter 5 "Remote Programming", https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf, and the
  SR810 manual, revision 1.8 (01/2005), https://www.thinksrs.com/downloads/pdfs/manuals/SR810m.pdf.
* **SR860 / SR865A** (``sr86x``): "SR860 DSP Lock-in Amplifier" user manual, revision 2.11, chapter 4
  "Programming", https://www.thinksrs.com/downloads/pdfs/manuals/SR860m.pdf, and the SR865A manual,
  revision 2.11, https://www.thinksrs.com/downloads/pdfs/manuals/SR865Am.pdf.

The dialects share mnemonics but not indices. The differences this driver handles:

=====================  =================================  ====================================
                       SR810 / SR830                      SR860 / SR865A
=====================  =================================  ====================================
reference source       ``FMOD`` 1 = internal, 0 = ext     ``RSRC`` 0 = internal, 1 = ext
sensitivity            ``SENS`` 0 = 2 nV ... 26 = 1 V     ``SCAL`` 0 = 1 V ... 27 = 1 nV
time constant          ``OFLT`` 0 = 10 µs ... 19 = 30 ks  ``OFLT`` 0 = 1 µs ... 21 = 30 ks
``OUTP?`` / ``SNAP?``  1 = X, 2 = Y, 3 = R, 4 = θ         0 = X, 1 = Y, 2 = R, 3 = θ (max 3)
input                  ``ISRC`` 0..3 (A, A-B, I1M, I100M)  ``IVMD`` + ``ISRC`` + ``ICUR``
auto gain / range      ``AGAN`` / ``ARSV`` (reserve)       ``ASCL`` / ``ARNG`` (input range)
sine amplitude         4 mV ... 5 V (``SLVL``)            1 nV ... 2 V (``SLVL``), ``SOFF`` DC
reply interface        only the one chosen with ``OUTX``   the one that asked
=====================  =================================  ====================================

Set commands have no reply. After every set command the driver reads the Standard Event Status
byte (``*ESR?``) and raises if bit 4 (EXE: parameter out of range / not allowed now) or bit 5
(CMD: illegal command) is set, so a rejected command never goes unnoticed.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, Transport

SR8X0 = "sr8x0"
SR86X = "sr86x"

# SR830 manual p. 5-6 (SENS) - index 0 = 2 nV/fA ... 26 = 1 V/µA.
SR8X0_SENSITIVITIES_V: tuple[float, ...] = (
    2e-9, 5e-9, 10e-9, 20e-9, 50e-9, 100e-9, 200e-9, 500e-9,
    1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6, 200e-6, 500e-6,
    1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3, 1.0,
)  # fmt: skip
# SR860 manual p. 113 (SCAL) - index 0 = 1 V [µA] ... 27 = 1 nV [fA].
SR86X_SENSITIVITIES_V: tuple[float, ...] = (
    1.0, 500e-3, 200e-3, 100e-3, 50e-3, 20e-3, 10e-3, 5e-3, 2e-3, 1e-3,
    500e-6, 200e-6, 100e-6, 50e-6, 20e-6, 10e-6, 5e-6, 2e-6, 1e-6,
    500e-9, 200e-9, 100e-9, 50e-9, 20e-9, 10e-9, 5e-9, 2e-9, 1e-9,
)  # fmt: skip
# SR830 manual p. 5-6 (OFLT) - index 0 = 10 µs ... 19 = 30 ks.
SR8X0_TIME_CONSTANTS_S: tuple[float, ...] = (
    10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3,
    1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1e3, 3e3, 10e3, 30e3,
)  # fmt: skip
# SR860 manual p. 114 (OFLT) - index 0 = 1 µs ... 21 = 30 ks.
SR86X_TIME_CONSTANTS_S: tuple[float, ...] = (
    1e-6, 3e-6, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3,
    1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1e3, 3e3, 10e3, 30e3,
)  # fmt: skip

#: OFSL index -> dB/octave (identical on both families).
FILTER_SLOPES_DB = (6, 12, 18, 24)
#: Wait time to 99 % of the final value, in time constants (SR830 manual p. 3-25, "Wait Time").
SETTLE_TIME_CONSTANTS = {6: 5.0, 12: 7.0, 18: 9.0, 24: 10.0}
#: SR860 IRNG index -> voltage input range (V).
SR86X_INPUT_RANGES_V = (1.0, 0.3, 0.1, 0.03, 0.01)
RESERVE_MODES = ("high_reserve", "normal", "low_noise")  # SR830 RMOD 0/1/2
INPUT_CONFIGS = ("A", "A-B", "I_1MOhm", "I_100MOhm")  # SR830 ISRC 0..3

# LIAS? status bits (latched since the last read).
_LIAS_SR8X0 = {
    0: "input or amplifier overload (reduce the signal or increase the dynamic reserve)",
    1: "time-constant filter overload",
    2: "output overload (signal exceeds the sensitivity range)",
    3: "reference unlock",
}
_LIAS_SR86X = {
    0: "CH1 output scale overload (signal exceeds the sensitivity range)",
    1: "CH2 output scale overload (signal exceeds the sensitivity range)",
    3: "reference unlock (external or chop reference)",
    4: "input range overload (choose a larger input range)",
    5: "sync filter frequency out of range",
    6: "sync filter overload",
}


@dataclass(frozen=True)
class ModelSpec:
    model: str
    family: str
    max_frequency_hz: float
    min_amplitude_v: float
    max_amplitude_v: float
    max_harmonic: int
    sensitivities_v: tuple[float, ...]
    time_constants_s: tuple[float, ...]
    min_frequency_hz: float = 1e-3


MODELS: dict[str, ModelSpec] = {
    "SR830": ModelSpec(
        "SR830", SR8X0, 102e3, 0.004, 5.0, 19999, SR8X0_SENSITIVITIES_V, SR8X0_TIME_CONSTANTS_S
    ),
    "SR810": ModelSpec(
        "SR810", SR8X0, 102e3, 0.004, 5.0, 19999, SR8X0_SENSITIVITIES_V, SR8X0_TIME_CONSTANTS_S
    ),
    "SR860": ModelSpec("SR860", SR86X, 500e3, 1e-9, 2.0, 99, SR86X_SENSITIVITIES_V, SR86X_TIME_CONSTANTS_S),
    "SR865A": ModelSpec("SR865A", SR86X, 4e6, 1e-9, 2.0, 99, SR86X_SENSITIVITIES_V, SR86X_TIME_CONSTANTS_S),
}


@dataclass
class Snapshot:
    x: float
    y: float
    r: float
    theta_deg: float
    reference_frequency_hz: float


def sensitivity_index(spec: ModelSpec, full_scale_v: float) -> int:
    """Index of the smallest sensitivity range that is >= ``full_scale_v`` (never a smaller
    one, so a signal of that size does not overload)."""
    candidates = [(v, i) for i, v in enumerate(spec.sensitivities_v) if v >= full_scale_v * (1 - 1e-6)]
    if not candidates:
        raise ValueError(
            f"{full_scale_v:g} V is above the largest sensitivity ({max(spec.sensitivities_v):g} V) "
            f"of the {spec.model}."
        )
    return min(candidates)[1]


def time_constant_index(spec: ModelSpec, time_constant_s: float) -> int:
    """Index of the available time constant nearest to ``time_constant_s`` (log scale)."""
    target = math.log10(time_constant_s)
    return min(
        range(len(spec.time_constants_s)), key=lambda i: abs(math.log10(spec.time_constants_s[i]) - target)
    )


def _floats(cmd: str, reply: str, count: int) -> list[float]:
    try:
        values = [float(p) for p in reply.split(",") if p.strip()]
    except ValueError as exc:
        raise InstrumentProtocolError(f"Lock-in sent a non-numeric reply to {cmd!r}: {reply!r}") from exc
    if len(values) != count:
        raise InstrumentProtocolError(f"Expected {count} values from {cmd!r}, got {reply!r}")
    return values


class SRSLockIn:
    """Driver for SR810/SR830 and SR860/SR865A lock-in amplifiers.

    Args:
        transport: An open transport (VISA, serial, TCP or simulated).
        model: ``"auto"`` (from ``*IDN?``) or one of ``sr810``, ``sr830``, ``sr860``, ``sr865a``.
            An explicit model is also used for SRS models this driver does not know (e.g. an SR865),
            which is at your own risk.
        output_interface: For SR810/SR830 only: ``"rs232"`` or ``"gpib"`` sends ``OUTX 0/1`` before
            any query so the lock-in answers on the interface actually in use. ``None`` skips it.
    """

    def __init__(
        self, transport: Transport, *, model: str = "auto", output_interface: str | None = None
    ) -> None:
        self.t = transport
        #: Set by :meth:`set_amplitude_minimum`; long operations (sweeps) check it and stop.
        self.abort = threading.Event()
        self.warning: str | None = None
        wanted = model.strip().upper()
        if wanted != "AUTO" and wanted not in MODELS:
            raise InstrumentProtocolError(
                f"Unknown model option {model!r}. Use auto, sr810, sr830, sr860 or sr865a."
            )
        with self.t.lock:
            self.t.flush_input()
            if output_interface in {"rs232", "gpib"} and (wanted == "AUTO" or MODELS[wanted].family == SR8X0):
                # SR830 manual p. 5-1: responses go only to the interface selected with OUTX; send it
                # before any query. (On an SR86x this is an unknown command; *CLS below clears it.)
                self.t.write(f"OUTX {0 if output_interface == 'rs232' else 1}")
            self._idn = self.query("*IDN?", timeout=max(self.t.timeout, 3.0))
            parts = [p.strip() for p in self._idn.split(",")]
            if len(parts) < 2 or "stanford" not in parts[0].lower():
                raise InstrumentProtocolError(
                    f"*IDN? returned {self._idn!r}, which is not a Stanford Research Systems instrument."
                )
            reported = parts[1].upper()
            if wanted == "AUTO":
                if reported not in MODELS:
                    raise InstrumentProtocolError(
                        f"Connected to an SRS {reported}, which this server does not support (supported: "
                        f"{', '.join(MODELS)}). If it shares the SR830 or SR860 command set, restart with "
                        "`--option model=sr830` or `--option model=sr860` at your own risk."
                    )
                self.spec = MODELS[reported]
            else:
                if reported in MODELS and reported != wanted:
                    raise InstrumentProtocolError(
                        f"`--option model={model}` was given but the instrument reports {reported}."
                    )
                self.spec = MODELS[wanted]
                if reported != wanted:
                    self.warning = (
                        f"Instrument reports {reported}; using the {wanted} command set as requested."
                    )
            self.t.write("*CLS")

    # ------------------------------------------------------------ low level

    @property
    def family(self) -> str:
        return self.spec.family

    def query(self, cmd: str, timeout: float | None = None) -> str:
        return self.t.query(cmd, timeout).strip()

    def query_float(self, cmd: str) -> float:
        return _floats(cmd, self.query(cmd), 1)[0]

    def query_int(self, cmd: str) -> int:
        return int(round(self.query_float(cmd)))

    def write(self, cmd: str, timeout: float | None = None) -> None:
        """Send a set command and verify it was accepted (``*ESR?`` EXE/CMD bits)."""
        with self.t.lock:
            self.t.write(cmd)
            self._check_esr(cmd, timeout)

    def _check_esr(self, cmd: str, timeout: float | None = None) -> None:
        esr = int(round(_floats("*ESR?", self.query("*ESR?", timeout), 1)[0]))
        if esr & 32:
            raise InstrumentProtocolError(
                f"Lock-in rejected {cmd!r}: illegal command (CMD bit set, *ESR? = {esr}). "
                f"The command may not exist on the {self.spec.model}."
            )
        if esr & 16:
            raise InstrumentProtocolError(
                f"Lock-in could not execute {cmd!r}: parameter out of range or not allowed in the "
                f"current mode (EXE bit set, *ESR? = {esr})."
            )

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        parts = [p.strip() for p in self._idn.split(",")] + ["", "", ""]
        serial = re.sub(r"^s/n", "", parts[2], flags=re.IGNORECASE)
        firmware = re.sub(r"^(ver|v)", "", parts[3], flags=re.IGNORECASE)
        info = {
            "manufacturer": "Stanford Research Systems",
            "model": parts[1],
            "serial": serial,
            "firmware": firmware,
            "command_set": self.spec.model,
        }
        if self.warning:
            info["warning"] = self.warning
        return info

    # ------------------------------------------------------------ outputs

    def snapshot(self) -> Snapshot:
        """X, Y, R, θ and the reference frequency, recorded together with ``SNAP?``."""
        if self.family == SR8X0:
            cmd = "SNAP? 1,2,3,4,9"
            x, y, r, th, f = _floats(cmd, self.query(cmd), 5)
            return Snapshot(x, y, r, th, f)
        with self.t.lock:  # SR86x SNAP? takes at most 3 parameters
            x, y = _floats("SNAP? 0,1", self.query("SNAP? 0,1"), 2)
            r, th = _floats("SNAP? 2,3", self.query("SNAP? 2,3"), 2)
            f = self.query_float("FREQ?")
        return Snapshot(x, y, r, th, f)

    def aux_inputs_v(self) -> list[float]:
        channels = range(1, 5) if self.family == SR8X0 else range(4)
        return [self.query_float(f"OAUX? {j}") for j in channels]

    def overloads(self) -> list[str]:
        """Decode the LIA status byte/word (``LIAS?``). Bits are latched since the last read."""
        bits = _LIAS_SR8X0 if self.family == SR8X0 else _LIAS_SR86X
        status = self.query_int("LIAS?")
        return [text for bit, text in bits.items() if status & (1 << bit)]

    # ------------------------------------------------------------ reference

    def frequency_hz(self) -> float:
        return self.query_float("FREQ?")

    def set_frequency_hz(self, f: float) -> None:
        self.write(f"FREQ {f:.10g}")

    def reference_internal(self) -> bool:
        if self.family == SR8X0:
            return self.query_int("FMOD?") == 1
        return self.query_int("RSRC?") == 0

    def reference_source(self) -> str:
        if self.family == SR8X0:
            return "internal" if self.query_int("FMOD?") == 1 else "external"
        return {0: "internal", 1: "external", 2: "dual", 3: "chop"}.get(self.query_int("RSRC?"), "unknown")

    def set_reference_internal(self, internal: bool) -> None:
        if self.family == SR8X0:
            self.write(f"FMOD {1 if internal else 0}")
        else:
            self.write(f"RSRC {0 if internal else 1}")

    def harmonic(self) -> int:
        return self.query_int("HARM?")

    def set_harmonic(self, n: int) -> None:
        self.write(f"HARM {int(n)}")

    def phase_deg(self) -> float:
        return self.query_float("PHAS?")

    def set_phase_deg(self, p: float) -> None:
        self.write(f"PHAS {p:.4f}")

    # ------------------------------------------------------------ sine output

    def amplitude_v(self) -> float:
        return self.query_float("SLVL?")

    def set_amplitude_v(self, v: float) -> None:
        self.write(f"SLVL {v:.6g}")

    def dc_level_v(self) -> float | None:
        return self.query_float("SOFF?") if self.family == SR86X else None

    def set_amplitude_minimum(self) -> dict[str, float]:
        """Put the sine output in its safest state and stop any running sweep."""
        self.abort.set()
        self.set_amplitude_v(self.spec.min_amplitude_v)
        out = {"amplitude_v": self.amplitude_v()}
        if self.family == SR86X:
            self.write("SOFF 0")  # SR860 p. 109: sine out dc level
            out["dc_level_v"] = self.query_float("SOFF?")
        return out

    # ------------------------------------------------------------ gain / filter

    def sensitivity_index(self) -> int:
        return self.query_int("SENS?" if self.family == SR8X0 else "SCAL?")

    def sensitivity_v(self) -> float:
        return self.spec.sensitivities_v[self.sensitivity_index()]

    def set_sensitivity_index(self, i: int) -> None:
        self.write(f"{'SENS' if self.family == SR8X0 else 'SCAL'} {int(i)}")

    def time_constant_s(self) -> float:
        return self.spec.time_constants_s[self.query_int("OFLT?")]

    def set_time_constant_index(self, i: int) -> None:
        self.write(f"OFLT {int(i)}")

    def filter_slope_db(self) -> int:
        return FILTER_SLOPES_DB[self.query_int("OFSL?")]

    def set_filter_slope_db(self, slope_db: int) -> None:
        self.write(f"OFSL {FILTER_SLOPES_DB.index(slope_db)}")

    def sync_filter(self) -> bool:
        return self.query_int("SYNC?") == 1

    def set_sync_filter(self, on: bool) -> None:
        self.write(f"SYNC {1 if on else 0}")

    def reserve(self) -> str | None:
        return RESERVE_MODES[self.query_int("RMOD?")] if self.family == SR8X0 else None

    def set_reserve(self, mode: str) -> None:
        if self.family != SR8X0:
            raise InstrumentProtocolError(
                f"The {self.spec.model} has no dynamic-reserve setting; set the input range instead."
            )
        self.write(f"RMOD {RESERVE_MODES.index(mode)}")

    # ------------------------------------------------------------ input

    def input_configuration(self) -> str:
        if self.family == SR8X0:
            return INPUT_CONFIGS[self.query_int("ISRC?")]
        if self.query_int("IVMD?") == 1:
            return "I_100MOhm" if self.query_int("ICUR?") == 1 else "I_1MOhm"
        return "A-B" if self.query_int("ISRC?") == 1 else "A"

    def current_input(self) -> bool:
        return self.input_configuration().startswith("I_")

    def set_input_configuration(self, config: str) -> None:
        if self.family == SR8X0:
            self.write(f"ISRC {INPUT_CONFIGS.index(config)}")
            return
        if config.startswith("I_"):
            self.write("IVMD 1")
            self.write(f"ICUR {1 if config == 'I_100MOhm' else 0}")
        else:
            self.write("IVMD 0")
            self.write(f"ISRC {1 if config == 'A-B' else 0}")

    def coupling(self) -> str:
        return "DC" if self.query_int("ICPL?") == 1 else "AC"

    def set_coupling(self, coupling: str) -> None:
        self.write(f"ICPL {1 if coupling == 'DC' else 0}")

    def shield(self) -> str:
        return "ground" if self.query_int("IGND?") == 1 else "float"

    def set_shield(self, shield: str) -> None:
        self.write(f"IGND {1 if shield == 'ground' else 0}")

    def input_range_v(self) -> float | None:
        return SR86X_INPUT_RANGES_V[self.query_int("IRNG?")] if self.family == SR86X else None

    def set_input_range_v(self, v: float) -> None:
        if self.family != SR86X:
            raise InstrumentProtocolError(
                f"The {self.spec.model} has no input-range setting; use the dynamic reserve instead."
            )
        idx = min(range(len(SR86X_INPUT_RANGES_V)), key=lambda i: abs(SR86X_INPUT_RANGES_V[i] - v))
        self.write(f"IRNG {idx}")

    # ------------------------------------------------------------ auto functions

    def auto_phase(self) -> None:
        self.write("APHS")

    def auto_gain(self, timeout: float = 60.0) -> None:
        """SR8x0 ``AGAN`` / SR86x ``ASCL``: pick the sensitivity for the present signal."""
        self._auto("AGAN" if self.family == SR8X0 else "ASCL", timeout)

    def auto_range(self, timeout: float = 60.0) -> None:
        """SR8x0 ``ARSV`` (auto reserve) / SR86x ``ARNG`` (auto input range)."""
        self._auto("ARSV" if self.family == SR8X0 else "ARNG", timeout)

    def _auto(self, cmd: str, timeout: float) -> None:
        with self.t.lock:
            self.t.write(cmd)
            self._wait_idle(timeout)
            self._check_esr(cmd)

    def _wait_idle(self, timeout: float) -> None:
        if self.family == SR86X:
            self.query("*OPC?", timeout=timeout)  # SR860 p. 147: returns 1 when pending operations finish
            return
        # SR830 p. 5-2: a status query is only processed once the previous command has finished;
        # bit 1 (IFC) of the serial poll byte is set when no command is executing.
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(deadline - time.monotonic(), 0.5)
            if int(round(_floats("*STB?", self.query("*STB?", remaining), 1)[0])) & 2:
                return
            if time.monotonic() > deadline:
                raise InstrumentProtocolError(f"Lock-in auto function did not finish within {timeout:g} s.")
            time.sleep(0.1)

    def close(self) -> None:
        self.t.close()
