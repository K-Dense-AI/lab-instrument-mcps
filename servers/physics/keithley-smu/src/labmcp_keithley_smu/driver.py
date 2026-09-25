"""Drivers for Keithley (Tektronix) SourceMeter SMUs in their three remote command dialects.

* ``2400``: Model 2400/2401/2410/2420/2425/2430/2440 SCPI, verified against the
  "Series 2400 SourceMeter User's Manual", Keithley 2400S-900-01 Rev. K (September 2011),
  section 18 "SCPI Command Reference"
  (https://download.tek.com/manual/2400S-900-01_K-Sep2011_User.pdf). A 2450-family
  instrument set to the ``SCPI2400`` command set (``*LANG SCPI2400``) is also driven this way.
* ``2450``: Model 2450 (and 2460/2461/2470, same command set) in SCPI mode, verified against
  the "Model 2450 Interactive SourceMeter Instrument Reference Manual", 2450-901-01 Rev. D
  (May 2015), section 6 "SCPI command reference"
  (https://download.tek.com/manual/2450-901-01_D_May_2015_Ref.pdf).
* ``2600``: Series 2600B (2601B ... 2636B) TSP, verified against the "Series 2600B System
  SourceMeter Instrument Reference Manual", 2600BS-901-01 Rev. F (August 2021), section 9
  "TSP command reference" (https://download.tek.com/manual/2600BS-901-01F_2600B_Reference_Aug2021.pdf).

Only simple, individually documented commands are used (set source function, range, level and
limit; output on/off; one measurement; query state). IV sweeps are stepped loops of those
commands, so every point is verifiable in the audit log, and the output is always switched off
at the end (``try/finally``) and whenever ``output_off`` is called from another thread.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from labmcp import InstrumentConnectionError, InstrumentProtocolError, Transport
from labmcp.scpi import SCPIDriver

SourceKind = Literal["voltage", "current"]
Dialect = Literal["2400", "2450", "2600"]

#: Hardware maxima (|V|, |A|) of each model's DC source, from the manuals cited above.
#: 2460/2461/2470 are absent on purpose: their limits were not verified; the instrument itself
#: rejects out-of-range values and the server's safety limits still apply.
MODEL_MAXIMA: dict[str, tuple[float, float]] = {
    "2400": (210.0, 1.05),
    "2400-LV": (21.0, 1.05),
    "2401": (21.0, 1.05),
    "2410": (1100.0, 1.05),
    "2420": (63.0, 3.15),
    "2425": (105.0, 3.15),
    "2430": (105.0, 3.15),
    "2440": (42.0, 5.25),
    "2450": (210.0, 1.05),
    "2601B": (40.0, 3.0),
    "2602B": (40.0, 3.0),
    "2604B": (40.0, 3.0),
    "2611B": (200.0, 1.5),
    "2612B": (200.0, 1.5),
    "2614B": (200.0, 1.5),
    "2634B": (200.0, 1.5),
    "2635B": (200.0, 1.5),
    "2636B": (200.0, 1.5),
}

_2450_FAMILY = {"2450", "2460", "2461", "2470"}
_OVERFLOW = 9.9e37  # SCPI / Keithley overflow & NAN markers (+9.9E37, +9.91E37)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _num(value: float) -> str:
    """Format a number for a command without losing precision."""
    return f"{value:.9g}"


@dataclass
class Reading:
    voltage_v: float
    current_a: float
    in_compliance: bool
    timestamp: str = field(default_factory=_now)


@dataclass
class Status:
    output_on: bool
    source: SourceKind
    level: float
    compliance: float
    in_compliance: bool
    terminals: str | None
    remote_sense: bool | None

    @property
    def level_unit(self) -> str:
        return "V" if self.source == "voltage" else "A"

    @property
    def compliance_unit(self) -> str:
        return "A" if self.source == "voltage" else "V"


@dataclass
class SweepData:
    levels: list[float]
    readings: list[Reading]
    aborted: bool = False
    stop_reason: str | None = None


def parse_model(idn: str) -> str:
    """``KEITHLEY INSTRUMENTS INC.,MODEL 2400,...`` or ``Keithley Instruments, Model 2602B, ...``."""
    m = re.search(r"MODEL\s+([0-9]{4}[A-Z]?(?:-LV)?)", idn, re.IGNORECASE)
    if not m:
        raise InstrumentProtocolError(
            f"This does not look like a Keithley SourceMeter: *IDN? returned {idn!r}."
        )
    return m.group(1).upper()


class KeithleySMU:
    """Dialect-independent SMU operations. Subclasses implement the command set."""

    dialect: Dialect

    def __init__(self, transport: Transport, idn: str) -> None:
        self.t = transport
        self.idn_string = idn
        self.model = parse_model(idn)
        #: Set by :meth:`request_abort` (the SAFETY tool) to stop a running sweep.
        self.abort = threading.Event()

    # ------------------------------------------------------------ identity

    @property
    def max_voltage_v(self) -> float | None:
        return MODEL_MAXIMA.get(self.model, (None, None))[0]

    @property
    def max_current_a(self) -> float | None:
        return MODEL_MAXIMA.get(self.model, (None, None))[1]

    def identify(self) -> dict[str, str]:
        parts = [p.strip() for p in self.idn_string.split(",")]
        parts += [""] * (4 - len(parts))
        info = {
            "manufacturer": parts[0],
            "model": self.model,
            "serial": parts[2],
            "firmware": ",".join(parts[3:]).strip(", "),
            "dialect": self.dialect,
        }
        if self.max_voltage_v is not None:
            info["max_voltage_v"] = f"{self.max_voltage_v:g}"
            info["max_current_a"] = f"{self.max_current_a:g}"
        return info

    # ------------------------------------------------------------ interface

    def output_state(self) -> bool:
        raise NotImplementedError

    def set_output(self, on: bool) -> None:
        raise NotImplementedError

    def status(self) -> Status:
        raise NotImplementedError

    def configure(
        self,
        source: SourceKind,
        level: float,
        compliance: float,
        source_range: float | None = None,
        nplc: float = 1.0,
    ) -> None:
        raise NotImplementedError

    def set_level(self, source: SourceKind, level: float) -> None:
        raise NotImplementedError

    def read(self, source: SourceKind) -> Reading:
        """One source-measure reading. The output must already be on."""
        raise NotImplementedError

    def set_remote_sense(self, enabled: bool) -> None:
        raise NotImplementedError

    def error_list(self) -> list[str]:
        raise NotImplementedError

    def raise_errors(self, context: str) -> None:
        errs = self.error_list()
        if errs:
            raise InstrumentProtocolError(f"SMU reported error(s) after {context}: " + "; ".join(errs))

    def close(self) -> None:
        self.t.close()

    # ------------------------------------------------------------ shared logic

    def request_abort(self) -> None:
        """Stop any running sweep and switch the output off."""
        self.abort.set()
        self.set_output(False)

    def measure(self) -> tuple[Status, Reading]:
        with self.t.lock:
            status = self.status()
            if not status.output_on:
                raise InstrumentProtocolError(
                    "The SMU output is OFF, so there is nothing to measure. Configure the source and "
                    "call `output_on` first (after confirming the DUT is connected)."
                )
            return status, self.read(status.source)

    def sweep(
        self,
        source: SourceKind,
        levels: list[float],
        delay_s: float = 0.0,
        stop_on_compliance: bool = False,
    ) -> SweepData:
        """Step through ``levels`` with the output on, measuring at each point.

        The source must already be configured (function, limit, range, NPLC). The output is
        switched on at the first level and ALWAYS switched off afterwards, including on errors
        and when :meth:`request_abort` is called from another thread.
        """
        self.abort.clear()
        data = SweepData(levels=[], readings=[])
        try:
            self.set_level(source, levels[0])
            self.raise_errors("setting the first sweep level")
            self.set_output(True)
            self.raise_errors("turning the output on")
            for level in levels:
                if self.abort.is_set():
                    data.aborted, data.stop_reason = True, "aborted by output_off"
                    break
                self.set_level(source, level)
                self.raise_errors(f"setting the source level to {level:g}")
                if delay_s > 0 and self.abort.wait(delay_s):
                    data.aborted, data.stop_reason = True, "aborted by output_off"
                    break
                reading = self.read(source)
                data.levels.append(level)
                data.readings.append(reading)
                if stop_on_compliance and reading.in_compliance:
                    data.stop_reason = f"compliance reached at {level:g}"
                    break
        finally:
            try:
                self.set_output(False)
            except Exception as exc:
                raise InstrumentConnectionError(
                    f"Could not switch the SMU output OFF after the sweep ({exc}). THE OUTPUT MAY STILL "
                    "BE ON: press the OUTPUT ON/OFF key on the instrument now."
                ) from exc
        return data


# ======================================================================== SCPI


class _SCPISMU(KeithleySMU, SCPIDriver):
    def __init__(self, transport: Transport, idn: str) -> None:
        SCPIDriver.__init__(self, transport)
        KeithleySMU.__init__(self, transport, idn)

    def identify(self) -> dict[str, str]:
        return KeithleySMU.identify(self)

    def error_list(self) -> list[str]:
        return self.errors()

    def _send_all(self, commands: list[str], context: str) -> None:
        with self.t.lock:
            for cmd in commands:
                self.write(cmd)
            self.raise_errors(context)

    @staticmethod
    def _floats(reply: str, expected: int, cmd: str) -> list[float]:
        try:
            values = [float(x) for x in reply.split(",")]
        except ValueError as exc:
            raise InstrumentProtocolError(f"Could not parse the reply to {cmd!r}: {reply!r}") from exc
        if len(values) < expected:
            raise InstrumentProtocolError(f"Expected {expected} values from {cmd!r}, got {reply!r}")
        return values

    @staticmethod
    def _source_kind(reply: str) -> SourceKind:
        r = reply.strip().upper()
        if r.startswith("VOLT"):
            return "voltage"
        if r.startswith("CURR"):
            return "current"
        raise InstrumentProtocolError(f"Unexpected source function {reply!r} (expected VOLT or CURR)")

    def output_state(self) -> bool:
        return self.query_bool(":OUTP?")

    def set_output(self, on: bool) -> None:
        self.write(f":OUTP {'ON' if on else 'OFF'}")

    def _terminals(self) -> str | None:
        reply = self.query(":ROUT:TERM?").upper()
        return "front" if reply.startswith("FRON") else "rear" if reply.startswith("REAR") else reply


class Keithley2400(_SCPISMU):
    """2400-series SCPI (2400S-900-01 Rev. K, section 18)."""

    dialect: Dialect = "2400"

    #: Status word bits returned by the STATus element of :READ? (manual p. 18-50).
    STAT_COMPLIANCE = 1 << 3
    STAT_RANGE_COMPLIANCE = 1 << 16

    def configure(self, source, level, compliance, source_range=None, nplc=1.0) -> None:
        src = "VOLT" if source == "voltage" else "CURR"
        meas = "CURR" if source == "voltage" else "VOLT"
        rng = (
            f":SOUR:{src}:RANG:AUTO ON" if source_range is None else f":SOUR:{src}:RANG {_num(source_range)}"
        )
        self._send_all(
            [
                f":SOUR:FUNC {src}",
                f":SOUR:{src}:MODE FIX",
                rng,
                f":SOUR:{src}:LEV {_num(level)}",
                ":SENS:FUNC:CONC ON",
                ':SENS:FUNC "VOLT","CURR"',
                f":SENS:{meas}:RANG:AUTO ON",
                f":SENS:{meas}:PROT {_num(abs(compliance))}",
                f":SENS:{meas}:NPLC {_num(nplc)}",
                ":FORM:ELEM VOLT,CURR,STAT",
                ":ARM:COUN 1",
                ":TRIG:COUN 1",
            ],
            "configuring the source",
        )

    def set_level(self, source, level) -> None:
        src = "VOLT" if source == "voltage" else "CURR"
        self.write(f":SOUR:{src}:LEV {_num(level)}")

    def read(self, source) -> Reading:
        # :READ? needs the output on (otherwise error +802); it returns VOLT,CURR,STAT as
        # selected by :FORM:ELEM. Never use :MEAS? here: it turns the output on by itself.
        reply = self.query(":READ?", timeout=30.0)
        v, i, stat = self._floats(reply, 3, ":READ?")[:3]
        return Reading(voltage_v=v, current_a=i, in_compliance=bool(int(stat) & self.STAT_COMPLIANCE))

    def status(self) -> Status:
        with self.t.lock:
            on = self.output_state()
            source = self._source_kind(self.query(":SOUR:FUNC?"))
            src, meas = ("VOLT", "CURR") if source == "voltage" else ("CURR", "VOLT")
            level = self.query_float(f":SOUR:{src}:LEV?")
            compliance = self.query_float(f":SENS:{meas}:PROT?")
            tripped = self.query_bool(f":SENS:{meas}:PROT:TRIP?")
            terminals = self._terminals()
            rsense = self.query_bool(":SYST:RSEN?")
        return Status(on, source, level, compliance, tripped, terminals, rsense)

    def set_remote_sense(self, enabled: bool) -> None:
        self._send_all([f":SYST:RSEN {'ON' if enabled else 'OFF'}"], "setting remote sense")


class Keithley2450(_SCPISMU):
    """2450-family SCPI (2450-901-01 Rev. D, section 6)."""

    dialect: Dialect = "2450"

    def configure(self, source, level, compliance, source_range=None, nplc=1.0) -> None:
        src = "VOLT" if source == "voltage" else "CURR"
        meas = "CURR" if source == "voltage" else "VOLT"
        limit = "ILIM" if source == "voltage" else "VLIM"
        rng = (
            f":SOUR:{src}:RANG:AUTO ON" if source_range is None else f":SOUR:{src}:RANG {_num(source_range)}"
        )
        self._send_all(
            [
                f":SOUR:FUNC {src}",
                rng,
                f":SOUR:{src}:LEV {_num(level)}",
                f":SOUR:{src}:READ:BACK ON",
                f':SENS:FUNC "{meas}"',
                f":SENS:{meas}:RANG:AUTO ON",
                f":SENS:{meas}:NPLC {_num(nplc)}",
                f":SOUR:{src}:{limit} {_num(abs(compliance))}",
                ":SENS:COUN 1",
            ],
            "configuring the source",
        )

    def set_level(self, source, level) -> None:
        src = "VOLT" if source == "voltage" else "CURR"
        self.write(f":SOUR:{src}:LEV {_num(level)}")

    def read(self, source) -> Reading:
        # SOURce element = measured source value (readback is ON); READing = measure function.
        cmd = ':READ? "defbuffer1",SOUR,READ'
        with self.t.lock:
            src_value, reading = self._floats(self.query(cmd, timeout=30.0), 2, cmd)[:2]
            limit = ":SOUR:VOLT:ILIM:TRIP?" if source == "voltage" else ":SOUR:CURR:VLIM:TRIP?"
            tripped = self.query_bool(limit)
        if source == "voltage":
            return Reading(voltage_v=src_value, current_a=reading, in_compliance=tripped)
        return Reading(voltage_v=reading, current_a=src_value, in_compliance=tripped)

    def status(self) -> Status:
        with self.t.lock:
            on = self.output_state()
            source = self._source_kind(self.query(":SOUR:FUNC?"))
            src, meas, limit = ("VOLT", "CURR", "ILIM") if source == "voltage" else ("CURR", "VOLT", "VLIM")
            level = self.query_float(f":SOUR:{src}:LEV?")
            compliance = self.query_float(f":SOUR:{src}:{limit}?")
            tripped = self.query_bool(f":SOUR:{src}:{limit}:TRIP?")
            terminals = self._terminals()
            rsense = self.query_bool(f":SENS:{meas}:RSEN?")
        return Status(on, source, level, compliance, tripped, terminals, rsense)

    def set_remote_sense(self, enabled: bool) -> None:
        state = "ON" if enabled else "OFF"
        self._send_all([f":SENS:VOLT:RSEN {state}", f":SENS:CURR:RSEN {state}"], "setting remote sense")


# ======================================================================== TSP


class Keithley2600(KeithleySMU):
    """Series 2600B TSP (2600BS-901-01 Rev. F, section 9). Queries are ``print(...)``."""

    dialect: Dialect = "2600"

    def __init__(self, transport: Transport, idn: str, channel: str = "a") -> None:
        super().__init__(transport, idn)
        if channel not in {"a", "b"}:
            raise InstrumentProtocolError(f"2600B channel must be 'a' or 'b', got {channel!r}")
        self.channel = channel
        self.smu = f"smu{channel}"

    def identify(self) -> dict[str, str]:
        info = super().identify()
        info["channel"] = self.smu
        return info

    # low level ---------------------------------------------------------------

    def tsp(self, statement: str) -> None:
        self.t.write(statement)

    def print_value(self, expression: str, timeout: float | None = None) -> str:
        return self.t.query(f"print({expression})", timeout).strip()

    def print_number(self, expression: str, timeout: float | None = None) -> float:
        reply = self.print_value(expression, timeout)
        try:
            return float(reply.split("\t")[0])
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"Expected a number from print({expression}), got {reply!r}"
            ) from exc

    def print_bool(self, expression: str) -> bool:
        reply = self.print_value(expression).lower()
        if reply in {"true", "false"}:
            return reply == "true"
        try:
            return float(reply) != 0.0
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"Expected true/false from print({expression}), got {reply!r}"
            ) from exc

    def error_list(self, max_errors: int = 20) -> list[str]:
        out: list[str] = []
        with self.t.lock:
            count = int(self.print_number("errorqueue.count"))
            for _ in range(min(count, max_errors)):
                fields = self.print_value("errorqueue.next()").split("\t")
                code = fields[0].strip()
                try:
                    code = str(int(float(code)))
                except ValueError:
                    pass
                message = fields[1].strip() if len(fields) > 1 else ""
                out.append(f"{code}, {message}")
        return out

    def _send_all(self, statements: list[str], context: str) -> None:
        with self.t.lock:
            for s in statements:
                self.tsp(s)
            self.raise_errors(context)

    # operations --------------------------------------------------------------

    def output_state(self) -> bool:
        return self.print_bool(f"{self.smu}.source.output")

    def set_output(self, on: bool) -> None:
        self.tsp(f"{self.smu}.source.output = {self.smu}.{'OUTPUT_ON' if on else 'OUTPUT_OFF'}")

    def configure(self, source, level, compliance, source_range=None, nplc=1.0) -> None:
        s = self.smu
        y, other = ("v", "i") if source == "voltage" else ("i", "v")
        func = "OUTPUT_DCVOLTS" if source == "voltage" else "OUTPUT_DCAMPS"
        rng = (
            f"{s}.source.autorange{y} = {s}.AUTORANGE_ON"
            if source_range is None
            else f"{s}.source.range{y} = {_num(source_range)}"
        )
        self._send_all(
            [
                f"{s}.source.func = {s}.{func}",
                rng,
                f"{s}.source.level{y} = {_num(level)}",
                f"{s}.source.limit{other} = {_num(abs(compliance))}",
                f"{s}.measure.autorange{other} = {s}.AUTORANGE_ON",
                f"{s}.measure.nplc = {_num(nplc)}",
            ],
            "configuring the source",
        )

    def set_level(self, source, level) -> None:
        self.tsp(f"{self.smu}.source.level{'v' if source == 'voltage' else 'i'} = {_num(level)}")

    def read(self, source) -> Reading:
        with self.t.lock:
            reply = self.print_value(f"{self.smu}.measure.iv()", timeout=30.0)
            fields = reply.split("\t")
            try:
                i, v = float(fields[0]), float(fields[1])
            except (ValueError, IndexError) as exc:
                raise InstrumentProtocolError(
                    f"Could not parse print({self.smu}.measure.iv()): {reply!r}"
                ) from exc
            compliance = self.print_bool(f"{self.smu}.source.compliance")
        return Reading(voltage_v=v, current_a=i, in_compliance=compliance)

    def status(self) -> Status:
        s = self.smu
        with self.t.lock:
            on = self.output_state()
            func = int(self.print_number(f"{s}.source.func"))
            source: SourceKind = "voltage" if func == 1 else "current"
            y, other = ("v", "i") if source == "voltage" else ("i", "v")
            level = self.print_number(f"{s}.source.level{y}")
            compliance = self.print_number(f"{s}.source.limit{other}")
            tripped = self.print_bool(f"{s}.source.compliance")
            sense = int(self.print_number(f"{s}.sense"))
        return Status(on, source, level, compliance, tripped, None, sense == 1)

    def set_remote_sense(self, enabled: bool) -> None:
        mode = "SENSE_REMOTE" if enabled else "SENSE_LOCAL"
        self._send_all([f"{self.smu}.sense = {self.smu}.{mode}"], "setting remote sense")


# ======================================================================== factory


def open_smu(transport: Transport, dialect: str = "auto", channel: str = "a") -> KeithleySMU:
    """Identify the instrument and return the driver for its command dialect."""
    idn = transport.query("*IDN?").strip()
    model = parse_model(idn)
    dialect = (dialect or "auto").lower()
    if dialect == "auto":
        if model.startswith("26"):
            dialect = "2600"
        elif model in _2450_FAMILY:
            lang = transport.query("*LANG?").strip().strip('"').upper()
            if lang == "SCPI":
                dialect = "2450"
            elif lang == "SCPI2400":
                dialect = "2400"
            else:
                raise InstrumentProtocolError(
                    f"The {model} is using the {lang!r} command set. This server drives the 2450 family "
                    "in SCPI mode: select MENU > System > Settings > Command Set > SCPI (or send "
                    "`*LANG SCPI`) and reboot the instrument."
                )
        elif model.startswith("24"):
            dialect = "2400"
        else:
            raise InstrumentProtocolError(
                f"Unrecognised SourceMeter model {model!r}; start the server with "
                "`--option dialect=2400|2450|2600`."
            )
    if dialect == "2400":
        return Keithley2400(transport, idn)
    if dialect == "2450":
        return Keithley2450(transport, idn)
    if dialect == "2600":
        return Keithley2600(transport, idn, channel=channel)
    raise InstrumentProtocolError(f"Unknown dialect {dialect!r}; use auto, 2400, 2450 or 2600.")


def log_levels(start: float, stop: float, points: int) -> list[float]:
    if start == 0 or stop == 0 or (start > 0) != (stop > 0):
        raise InstrumentProtocolError("A log sweep needs non-zero start and stop values of the same sign.")
    ratio = stop / start
    return [start * ratio ** (k / (points - 1)) for k in range(points)]


def linear_levels(start: float, stop: float, points: int) -> list[float]:
    step = (stop - start) / (points - 1)
    return [start + k * step for k in range(points)]


def is_overflow(value: float) -> bool:
    return not math.isfinite(value) or abs(value) >= _OVERFLOW
