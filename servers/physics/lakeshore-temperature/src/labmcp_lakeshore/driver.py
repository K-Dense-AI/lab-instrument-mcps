"""Driver for Lake Shore Cryotronics Model 335, 336 and 350 cryogenic temperature controllers.

Commands verified against chapter 6 "Computer Interface Operation" of:

* Model 336 User's Manual, rev. 2.3 (27 Oct 2022), P/N 119-048,
  https://www.lakeshore.com/docs/default-source/product-downloads/336manual.pdf
* Model 335 User's Manual, rev. 1.6 (10 Jul 2019), P/N 119-055,
  https://www.lakeshore.com/docs/default-source/product-downloads/335_manual.pdf
* Model 350 User's Manual, rev. 1.7 (13 Nov 2019), P/N 119-057,
  https://www.lakeshore.com/docs/default-source/software/manuals/350_manual.pdf

Messages are ASCII lines; queries answer with one line ending in CR LF. Set commands have no
reply, so after each one the driver reads ``*ESR?`` and raises if bit 5 (CME, command error) or
bit 4 (EXE, execution error) is set. The manuals ask the host to leave 50 ms after each message
and send at most 20 messages per second (336 manual section 6.3.5); the driver enforces a minimum
interval between transactions.

Model differences handled here:

=========  ======  =======  =====================================  =============  ============
Model      Inputs  Outputs  RANGE (heater outputs 1/2)             PID/RAMP       Ramp K/min
=========  ======  =======  =====================================  =============  ============
335        A, B    1, 2     0 off, 1 low, 2 medium, 3 high         outputs 1, 2   0.1 - 100
336        A - D   1 - 4    0 off, 1 low, 2 medium, 3 high         outputs 1, 2   0.1 - 100
350        A - D   1 - 4    0 off, 1 - 5 (decade steps in power)   outputs 1 - 4  0.001 - 100
=========  ======  =======  =====================================  =============  ============

Outputs 3 and 4 (336/350) and output 2 of the 335 in voltage mode are unpowered analog outputs:
``RANGE`` 0 = off, 1 = on. Outputs 3/4 are read with ``AOUT?``; the 335's output 2 is read with
``HTR?`` in both modes (percent of full-scale voltage in voltage mode, 335 manual ``HTR?``).
The 3062 scanner option (inputs D1-D5) is not supported.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from labmcp import InstrumentProtocolError, Transport

SENSOR_TYPES = {
    0: "disabled",
    1: "diode",
    2: "platinum RTD",
    3: "NTC RTD",
    4: "thermocouple",
    5: "capacitance",
}
SENSOR_UNITS = {1: "V", 2: "ohm", 3: "ohm", 4: "mV", 5: "nF"}
PREFERRED_UNITS = {1: "kelvin", 2: "celsius", 3: "sensor"}
OUTPUT_MODES = {
    0: "off",
    1: "closed_loop_pid",
    2: "zone",
    3: "open_loop",
    4: "monitor_out",
    5: "warmup_supply",
    6: "mirroring",  # 336 only
}
# RDGST? bit weighting -> meaning (identical on 335/336/350).
READING_STATUS = {
    1: "invalid reading",
    16: "temperature underrange",
    32: "temperature overrange",
    64: "sensor units zero",
    128: "sensor units overrange",
}
HEATER_ERRORS = {0: None, 1: "heater open load", 2: "heater short (or compliance on 350 output 2)"}


@dataclass(frozen=True)
class ModelSpec:
    model: str
    inputs: tuple[str, ...]
    outputs: tuple[int, ...]
    heater_outputs: tuple[int, ...]  # read with HTR?; the others with AOUT?
    heater_range_names: tuple[str, ...]
    loop_outputs: tuple[int, ...]  # PID / RAMP / RAMPST? are valid for these
    min_ramp_k_min: float


MODELS = {
    "335": ModelSpec("335", ("A", "B"), (1, 2), (1, 2), ("off", "low", "medium", "high"), (1, 2), 0.1),
    "336": ModelSpec(
        "336", ("A", "B", "C", "D"), (1, 2, 3, 4), (1, 2), ("off", "low", "medium", "high"), (1, 2), 0.1
    ),
    "350": ModelSpec(
        "350",
        ("A", "B", "C", "D"),
        (1, 2, 3, 4),
        (1, 2),
        ("off", "range 1", "range 2", "range 3", "range 4", "range 5"),
        (1, 2, 3, 4),
        0.001,
    ),
}
ANALOG_RANGE_NAMES = ("off", "on")


@dataclass
class InputReading:
    input: str
    name: str
    sensor_type: str
    kelvin: float | None
    sensor_value: float | None
    sensor_unit: str | None
    status: list[str] = field(default_factory=list)
    preferred_units: str = "kelvin"


def decode_reading_status(code: int) -> list[str]:
    return [text for bit, text in READING_STATUS.items() if code & bit]


class LakeShoreController:
    """Driver for Lake Shore 335 / 336 / 350 controllers.

    Args:
        transport: An open transport (USB virtual COM port, TCP port 7777, GPIB or simulated).
        min_interval_s: Minimum time between messages (manual: 50 ms).
    """

    def __init__(self, transport: Transport, *, min_interval_s: float = 0.05) -> None:
        self.t = transport
        self.min_interval_s = min_interval_s
        self._last = 0.0
        #: Set by :meth:`all_heaters_off`; waiting loops check it.
        self.abort = threading.Event()
        with self.t.lock:
            self.t.flush_input()
            self._idn = self.query("*IDN?")
            parts = [p.strip() for p in self._idn.split(",")]
            if len(parts) < 2 or parts[0] != "LSCI":
                raise InstrumentProtocolError(
                    f"*IDN? returned {self._idn!r}, which is not a Lake Shore instrument."
                )
            model = parts[1].upper().removeprefix("MODEL")
            if model not in MODELS:
                raise InstrumentProtocolError(
                    f"Connected to a Lake Shore Model {model}, which this server does not support "
                    f"(supported: {', '.join('Model ' + m for m in MODELS)})."
                )
            self.spec = MODELS[model]
            self.write("*CLS", check=False)

    # ------------------------------------------------------------ low level

    def _pace(self) -> None:
        wait = self._last + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def query(self, cmd: str, timeout: float | None = None) -> str:
        with self.t.lock:
            self._pace()
            try:
                return self.t.query(cmd, timeout).strip()
            finally:
                self._last = time.monotonic()

    def query_float(self, cmd: str) -> float:
        reply = self.query(cmd)
        try:
            return float(reply)
        except ValueError as exc:
            raise InstrumentProtocolError(
                f"Controller sent a non-numeric reply to {cmd!r}: {reply!r}"
            ) from exc

    def query_ints(self, cmd: str) -> list[int]:
        reply = self.query(cmd)
        try:
            return [int(float(p)) for p in reply.split(",")]
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unexpected reply to {cmd!r}: {reply!r}") from exc

    def write(self, cmd: str, check: bool = True) -> None:
        """Send a command (no reply) and verify it with ``*ESR?``."""
        with self.t.lock:
            self._pace()
            try:
                self.t.write(cmd)
            finally:
                self._last = time.monotonic()
            if check:
                esr = self.query_ints("*ESR?")[0]
                if esr & 32:
                    raise InstrumentProtocolError(
                        f"Controller rejected {cmd!r}: command error (CME bit, *ESR? = {esr}) - unrecognised "
                        f"command or parameter for the Model {self.spec.model}."
                    )
                if esr & 16:
                    raise InstrumentProtocolError(
                        f"Controller could not execute {cmd!r}: execution error (EXE bit, *ESR? = {esr}) - "
                        "the value is outside what the instrument can do in its present configuration."
                    )

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        parts = [p.strip() for p in self._idn.split(",")] + ["", ""]
        serial, _, option_serial = parts[2].partition("/")
        return {
            "manufacturer": "Lake Shore Cryotronics",
            "model": f"Model {self.spec.model}",
            "serial": serial,
            "option_card_serial": option_serial,
            "firmware": parts[3],
        }

    # ------------------------------------------------------------ validation

    def _check_input(self, inp: str) -> str:
        inp = inp.upper()
        if inp not in self.spec.inputs:
            raise InstrumentProtocolError(
                f"The Model {self.spec.model} has inputs {', '.join(self.spec.inputs)}; got {inp!r}."
            )
        return inp

    def _check_output(self, out: int, loop: bool = False) -> int:
        valid = self.spec.loop_outputs if loop else self.spec.outputs
        if out not in valid:
            what = "control loops (PID/ramp) on outputs" if loop else "outputs"
            raise InstrumentProtocolError(
                f"The Model {self.spec.model} has {what} {', '.join(map(str, valid))}; got {out}."
            )
        return out

    # ------------------------------------------------------------ inputs

    def input_type(self, inp: str) -> tuple[int, int]:
        """(sensor type, preferred units) from ``INTYPE?``."""
        fields = self.query_ints(f"INTYPE? {self._check_input(inp)}")
        if len(fields) < 5:
            raise InstrumentProtocolError(f"Unexpected reply to INTYPE? {inp}: {fields}")
        return fields[0], fields[4]

    def read_input(self, inp: str) -> InputReading:
        inp = self._check_input(inp)
        with self.t.lock:
            sensor_type, units = self.input_type(inp)
            name = self.query(f"INNAME? {inp}").strip('"').strip()
            reading = InputReading(
                input=inp,
                name=name,
                sensor_type=SENSOR_TYPES.get(sensor_type, f"type {sensor_type}"),
                kelvin=None,
                sensor_value=None,
                sensor_unit=SENSOR_UNITS.get(sensor_type),
                preferred_units=PREFERRED_UNITS.get(units, "kelvin"),
            )
            if sensor_type == 0:
                return reading
            status = self.query_ints(f"RDGST? {inp}")[0]
            reading.status = decode_reading_status(status)
            kelvin = self.query_float(f"KRDG? {inp}")
            sensor = self.query_float(f"SRDG? {inp}")
        reading.sensor_value = None if status & (1 | 128) else sensor
        reading.kelvin = None if status & (1 | 16 | 32) else kelvin
        return reading

    def kelvin(self, inp: str) -> float:
        """Temperature of one input in kelvin; raises if the reading is not valid."""
        inp = self._check_input(inp)
        with self.t.lock:
            status = self.query_ints(f"RDGST? {inp}")[0]
            value = self.query_float(f"KRDG? {inp}")
        if status & (1 | 16 | 32):
            raise InstrumentProtocolError(
                f"Input {inp} has no valid temperature reading: {', '.join(decode_reading_status(status))}."
            )
        return value

    # ------------------------------------------------------------ outputs

    def output_mode(self, out: int) -> tuple[str, str | None, bool]:
        """(mode, control input letter or None, power-up enable) from ``OUTMODE?``."""
        fields = self.query_ints(f"OUTMODE? {self._check_output(out)}")
        if len(fields) < 3:
            raise InstrumentProtocolError(f"Unexpected reply to OUTMODE? {out}: {fields}")
        mode, inp, powerup = fields[:3]
        letters = {1: "A", 2: "B", 3: "C", 4: "D"}
        if mode == 6:
            # 336 Mirroring: the second field is the output being mirrored (1-4), not an input
            # (336 manual, OUTMODE command), so this output has no control input of its own.
            return OUTPUT_MODES[6], None, bool(powerup)
        return OUTPUT_MODES.get(mode, f"mode {mode}"), letters.get(inp), bool(powerup)

    def is_analog(self, out: int) -> bool:
        """True for unpowered analog outputs (RANGE is only off/on)."""
        if out not in self.spec.heater_outputs:
            return True
        if self.spec.model == "335" and out == 2:
            return self.query_ints("HTRSET? 2")[0] == 1  # 335: <type> 0 = current, 1 = voltage
        return False

    def range_names(self, out: int) -> tuple[str, ...]:
        return ANALOG_RANGE_NAMES if self.is_analog(out) else self.spec.heater_range_names

    def heater_range(self, out: int) -> int:
        return self.query_ints(f"RANGE? {self._check_output(out)}")[0]

    def set_heater_range(self, out: int, rng: int) -> None:
        names = self.range_names(self._check_output(out))
        if not 0 <= rng < len(names):
            raise InstrumentProtocolError(
                f"Output {out} of the Model {self.spec.model} accepts ranges 0-{len(names) - 1} "
                f"({', '.join(names)}); got {rng}."
            )
        self.write(f"RANGE {out},{rng}")

    def output_percent(self, out: int) -> float:
        """Heater output (``HTR?``, outputs 1/2) or analog output (``AOUT?``, outputs 3/4) in %."""
        self._check_output(out)
        return self.query_float(f"HTR? {out}" if out in self.spec.heater_outputs else f"AOUT? {out}")

    def heater_error(self, out: int) -> str | None:
        """``HTRST?`` for heater outputs; reading it clears the latched error."""
        if out not in self.spec.heater_outputs:
            return None
        code = self.query_ints(f"HTRST? {out}")[0]
        return HEATER_ERRORS.get(code, f"error code {code}")

    def setpoint(self, out: int) -> float:
        return self.query_float(f"SETP? {self._check_output(out)}")

    def set_setpoint(self, out: int, value: float) -> None:
        self.write(f"SETP {self._check_output(out)},{value:.4f}")

    def ramp(self, out: int) -> tuple[bool, float]:
        reply = self.query(f"RAMP? {self._check_output(out, loop=True)}")
        try:
            on, rate = reply.split(",")[:2]
            return int(float(on)) == 1, float(rate)
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unexpected reply to RAMP? {out}: {reply!r}") from exc

    def set_ramp(self, out: int, on: bool, rate_k_min: float) -> None:
        self.write(f"RAMP {self._check_output(out, loop=True)},{1 if on else 0},{rate_k_min:g}")

    def ramping(self, out: int) -> bool:
        return self.query_ints(f"RAMPST? {self._check_output(out, loop=True)}")[0] == 1

    def pid(self, out: int) -> tuple[float, float, float]:
        reply = self.query(f"PID? {self._check_output(out, loop=True)}")
        try:
            p, i, d = (float(v) for v in reply.split(",")[:3])
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unexpected reply to PID? {out}: {reply!r}") from exc
        return p, i, d

    def set_pid(self, out: int, p: float, i: float, d: float) -> None:
        self.write(f"PID {self._check_output(out, loop=True)},{p:g},{i:g},{d:g}")

    def setpoint_units(self, out: int) -> str:
        """Units the setpoint of ``out`` is expressed in: kelvin, celsius or sensor (``SETP`` uses
        the preferred units of the loop's control input)."""
        _, inp, _ = self.output_mode(out)
        if inp is None:
            return "kelvin"
        return PREFERRED_UNITS.get(self.input_type(inp)[1], "kelvin")

    # ------------------------------------------------------------ safety

    def all_heaters_off(self) -> dict[int, str]:
        """``RANGE n,0`` on every output; returns the read-back state (or error) per output.

        Best effort: every output is tried, and the range is read back even when the command's
        ``*ESR?`` check failed (the ``RANGE`` may still have been applied)."""
        self.abort.set()
        result: dict[int, str] = {}
        for out in self.spec.outputs:
            error: str | None = None
            try:
                self.write(f"RANGE {out},0")
            except Exception as exc:  # keep going: every output must be tried
                error = str(exc)
            try:
                rng = self.heater_range(out)
            except Exception as exc:
                result[out] = f"error: {error or exc}"
                continue
            result[out] = (
                "off"
                if rng == 0
                else f"STILL ON (range {rng}) - check the instrument" + (f": {error}" if error else "")
            )
        return result

    def close(self) -> None:
        self.t.close()
