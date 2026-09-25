"""Wire-level simulator of a small SCPI bench instrument: a DC power supply whose
output drives a 100 ohm load, plus a built-in DMM measuring across that load.

It is deliberately generic (not a clone of any vendor's instrument) and uses
only SCPI-99 standard command trees, so the model can practise the usual
patterns: ``*IDN?``, ``MEASure:VOLTage:DC?``, ``CONFigure`` + ``READ?``,
``[SOURce:]VOLTage``/``CURRent``, ``OUTPut[:STATe]``, ``FORMat[:DATA] REAL,64``
binary blocks, ``SYSTem:ERRor?`` and the ``*ESR?``/``*STB?`` status bytes.

Physics: the output voltage slews towards the setpoint with a 20 ms time
constant; if V/R exceeds the current limit the supply goes into constant-current
mode. DMM readings have ~20 uV of noise.
"""

from __future__ import annotations

import math
import random
import re
import struct
import time

from labmcp.scpi import SCPISimulator

LOAD_OHM = 100.0
MAX_VOLTAGE_V = 30.0
MAX_CURRENT_A = 3.0
SLEW_TAU_S = 0.02
ERROR_QUEUE_LENGTH = 10
MAX_TRIGGER_COUNT = 10000

# Standard Event Status Register bits (IEEE 488.2; error classes per SCPI-99 Vol. 2, 21.8)
ESR_OPC, ESR_QYE, ESR_DDE, ESR_EXE, ESR_CME = 0x01, 0x04, 0x08, 0x10, 0x20


class DMMPowerSupplySimulator(SCPISimulator):
    idn = "LabMCP,SIM-DMM-PSU,SIM000001,1.0.0"

    def __init__(self, seed: int | None = 0) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self.esr = 0
        self.ese = 0
        self.sre = 0
        self.reset()

    # ------------------------------------------------------------- state

    def reset(self) -> None:
        """``*RST`` state. SCPI-99 Vol. 2, 15.12: OUTPut:STATe is OFF at *RST."""
        self.voltage_set = 0.0
        self.current_limit = 0.1
        self.output_on = False
        self.function = "VOLT"
        self.trigger_count = 1
        self.data_format = "ASC"
        self.real_length = 64
        self.swapped = False
        self.readings: list[float] = []
        self._v_out = 0.0
        self._t_last = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        dt, self._t_last = now - self._t_last, now
        target = self.voltage_set if self.output_on else 0.0
        self._v_out = target + (self._v_out - target) * math.exp(-dt / SLEW_TAU_S)

    def _terminal_voltage(self) -> float:
        self._advance()
        v = self._v_out
        if v / LOAD_OHM > self.current_limit:  # constant-current mode
            v = self.current_limit * LOAD_OHM
        return v

    def _reading(self) -> float:
        v = self._terminal_voltage()
        if self.function == "CURR":
            return v / LOAD_OHM + self.rng.gauss(0, 2e-7)
        return v + self.rng.gauss(0, 2e-5)

    def _take(self) -> list[float]:
        return [self._reading() for _ in range(self.trigger_count)]

    # ------------------------------------------------------------- formatting

    @staticmethod
    def _nr3(value: float) -> str:
        return f"{value:+.9E}"

    def _format(self, values: list[float]) -> str:
        if self.data_format == "ASC":
            return ",".join(self._nr3(v) for v in values)
        code = ">" if not self.swapped else "<"
        code += ("d" if self.real_length == 64 else "f") * len(values)
        payload = struct.pack(code, *values)
        size = str(len(payload))
        # Definite-length arbitrary block: #<digits><length><data>. The simulated
        # transport carries text, so bytes travel as latin-1 characters.
        return f"#{len(size)}{size}" + payload.decode("latin-1")

    # ------------------------------------------------------------- errors

    def _error(self, code: int, text: str) -> None:
        self.error_queue.append(f'{code},"{text}"')

    def handle(self, command: str) -> str | list[str] | None:
        # Process message units one at a time so each error sets its ESR bit and
        # *CLS clears the ESR as well as the error queue.
        replies: list[str] = []
        for part in command.split(";"):
            if not part.strip():
                continue
            if part.strip().upper() == "*CLS":
                self.esr = 0
            before = len(self.error_queue)
            reply = super().handle(part)
            for entry in self.error_queue[before:]:
                code = int(entry.split(",", 1)[0])
                if -199 <= code <= -100:
                    self.esr |= ESR_CME
                elif -299 <= code <= -200:
                    self.esr |= ESR_EXE
                elif -399 <= code <= -300:
                    self.esr |= ESR_DDE
                elif -499 <= code <= -400:
                    self.esr |= ESR_QYE
            if len(self.error_queue) > ERROR_QUEUE_LENGTH:  # SCPI-99 Vol. 2, 21.8.1
                self.error_queue[ERROR_QUEUE_LENGTH - 1 :] = ['-350,"Queue overflow"']
            if isinstance(reply, str):
                replies.append(reply)
        return ";".join(replies) if replies else None

    # ------------------------------------------------------------- commands

    @staticmethod
    def _number(arg: str, minimum: float, maximum: float, default: float) -> float:
        word = arg.strip().upper()
        if word in {"MIN", "MINIMUM"}:
            return minimum
        if word in {"MAX", "MAXIMUM"}:
            return maximum
        if word in {"DEF", "DEFAULT"}:
            return default
        return float(re.sub(r"\s*[VA]$", "", word))  # optional unit suffix, e.g. "5 V"

    def _set_level(self, arg: str, maximum: float, default: float) -> float | None:
        if not arg.strip():
            self._error(-109, "Missing parameter")
            return None
        value = self._number(arg, 0.0, maximum, default)
        if not 0.0 <= value <= maximum:
            self._error(-222, "Data out of range")
            return None
        return value

    def command(self, key: str, arg: str) -> str | None:
        m = self.matches
        # --- IEEE 488.2 common commands not handled by the base class
        if key == "*OPC":
            self.esr |= ESR_OPC
            return None
        if key == "*WAI":
            return None
        if key == "*ESR?":
            value, self.esr = self.esr, 0
            return str(value)
        if key == "*ESE":
            self.ese = int(float(arg))
            return None
        if key == "*ESE?":
            return str(self.ese)
        if key == "*SRE":
            self.sre = int(float(arg))
            return None
        if key == "*SRE?":
            return str(self.sre)
        if key == "*STB?":
            stb = (0x04 if self.error_queue else 0) | (0x20 if self.esr & self.ese else 0)
            return str(stb)
        if key == "*TST?":
            return "0"
        # --- SYSTem
        if m(key, "SYSTem:VERSion?"):
            return "1999.0"
        if m(key, "SYSTem:ERRor:COUNt?"):
            return str(len(self.error_queue))
        # --- source (PSU)
        if m(key, "[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]"):
            value = self._set_level(arg, MAX_VOLTAGE_V, 0.0)
            if value is not None:
                self._advance()
                self.voltage_set = value
            return None
        if m(key, "[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]?"):
            if arg:
                return self._nr3(self._number(arg, 0.0, MAX_VOLTAGE_V, 0.0))
            return self._nr3(self.voltage_set)
        if m(key, "[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]"):
            value = self._set_level(arg, MAX_CURRENT_A, 0.1)
            if value is not None:
                self.current_limit = value
            return None
        if m(key, "[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]?"):
            if arg:
                return self._nr3(self._number(arg, 0.0, MAX_CURRENT_A, 0.1))
            return self._nr3(self.current_limit)
        if m(key, "OUTPut[:STATe]"):
            word = arg.strip().upper()
            if word not in {"ON", "OFF", "1", "0"}:
                raise ValueError(arg)
            self._advance()
            self.output_on = word in {"ON", "1"}
            return None
        if m(key, "OUTPut[:STATe]?"):
            return "1" if self.output_on else "0"
        # --- measurement (DMM)
        if m(key, "MEASure[:SCALar]:VOLTage[:DC]?"):
            self.function = "VOLT"
            return self._format([self._reading()])
        if m(key, "MEASure[:SCALar]:CURRent[:DC]?"):
            self.function = "CURR"
            return self._format([self._reading()])
        if m(key, "CONFigure[:SCALar]:VOLTage[:DC]"):
            self.function = "VOLT"
            return None
        if m(key, "CONFigure[:SCALar]:CURRent[:DC]"):
            self.function = "CURR"
            return None
        if m(key, "CONFigure?"):
            return '"VOLT:DC"' if self.function == "VOLT" else '"CURR:DC"'
        if m(key, "TRIGger[:SEQuence]:COUNt"):
            count = int(self._number(arg, 1, MAX_TRIGGER_COUNT, 1))
            if not 1 <= count <= MAX_TRIGGER_COUNT:
                self._error(-222, "Data out of range")
                return None
            self.trigger_count = count
            return None
        if m(key, "TRIGger[:SEQuence]:COUNt?"):
            return str(self.trigger_count)
        if m(key, "READ?"):
            self.readings = self._take()
            return self._format(self.readings)
        if m(key, "INITiate[:IMMediate]"):
            self.readings = self._take()
            return None
        if m(key, "FETCh?"):
            if not self.readings:
                self._error(-230, "Data corrupt or stale")
                return None
            return self._format(self.readings)
        if m(key, "ABORt"):
            return None
        # --- FORMat (SCPI-99 Vol. 2, ch. 9)
        if m(key, "FORMat[:DATA]"):
            parts = [p.strip().upper() for p in arg.split(",")]
            if parts[0] in {"ASC", "ASCII"}:
                self.data_format = "ASC"
            elif parts[0] == "REAL":
                length = int(parts[1]) if len(parts) > 1 else 64
                if length not in {32, 64}:
                    raise ValueError(arg)
                self.data_format, self.real_length = "REAL", length
            else:
                raise ValueError(arg)
            return None
        if m(key, "FORMat[:DATA]?"):
            return "ASC,0" if self.data_format == "ASC" else f"REAL,{self.real_length}"
        if m(key, "FORMat:BORDer"):
            word = arg.strip().upper()
            if word in {"NORM", "NORMAL"}:
                self.swapped = False
            elif word in {"SWAP", "SWAPPED"}:
                self.swapped = True
            else:
                raise ValueError(arg)
            return None
        if m(key, "FORMat:BORDer?"):
            return "SWAP" if self.swapped else "NORM"
        raise self.undefined()
