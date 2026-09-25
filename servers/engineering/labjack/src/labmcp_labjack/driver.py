"""Driver for LabJack T-series DAQ devices (T4, T7, T7-Pro, T8) through LabJack's LJM library.

The driver only uses the documented LJM "e" functions of the official ``labjack-ljm`` Python
wrapper (``openS``, ``getHandleInfo``, ``eReadNames``, ``eWriteNames``, ``namesToAddresses``,
``eStreamStart/Read/Stop``) and register names from the T-series Modbus map.

References:

* LabJack T-Series Datasheet, https://support.labjack.com/docs/t-series-datasheet
  (3.2 Stream Mode, 13.0 Digital I/O, 14.0 Analog Inputs and the per-device pages 14.3.0-14.3.2,
  14.1.1 Thermocouple, 15.0 DAC, 18.0 Internal Temp Sensor, Appendix A-3 noise/resolution).
* T-series Modbus map ``ljm_constants.json``, https://github.com/labjack/ljm_constants
  (register names and types, and the per-device valid values of ``AIN#_RANGE`` and
  ``AIN#_RESOLUTION_INDEX``).
* ``labjack-ljm`` 1.23 Python wrapper and LabJack's official examples,
  https://github.com/labjack/labjack-ljm-python (``Examples/More/Stream/stream_basic.py``,
  ``Examples/More/AIN/single_ain_with_config.py``).
"""

from __future__ import annotations

import contextlib
import math
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Literal

from labmcp import InstrumentConnectionError, InstrumentError, InstrumentProtocolError
from labmcp.audit import AuditLog

DT_T4, DT_T7, DT_T8 = 4, 7, 8
CONNECTION_NAMES = {1: "USB", 2: "TCP", 3: "Ethernet", 4: "WiFi"}
#: LJM/firmware sentinel meaning "no valid value" (skipped stream scans, open thermocouples).
DUMMY_VALUE = -9999.0
#: Largest stream the driver will hold in memory (samples summed over all channels).
MAX_STREAM_SAMPLES = 2_000_000

#: AIN#_EF_INDEX values of the thermocouple extended feature (datasheet 14.1.1, T7/T8 only).
THERMOCOUPLE_EF_INDEX = {"E": 20, "J": 21, "K": 22, "R": 23, "T": 24, "S": 25, "N": 27, "B": 28, "C": 30}
#: Modbus address of TEMPERATURE_DEVICE_K, the T7 default CJC source (datasheet 14.1.1).
TEMPERATURE_DEVICE_K_ADDRESS = 60052
#: TEMPERATURE#(0:7)_CAPTURE base address; the T8 default CJC for AIN n is 700 + 2n.
T8_TEMPERATURE_CAPTURE_ADDRESS = 700
#: LM34 CJC sensor conversion from the LabJack thermocouple app note (K/V and K).
LM34_SLOPE_K_PER_V, LM34_OFFSET_K = 55.56, 255.37

_CONNECTION_ERRORS = {1224, 1227, 1230, 1236, 1239, 1240, 1314}
_HINTS = {
    1227: "no matching LabJack was found - check the USB/Ethernet cable, the --address identifier "
    "and the device_type/connection_type options",
    1230: "the device is open in another program (Kipling, LJLogM, another script) - close it first",
    1294: "LJM does not know this register name",
    1314: "no LabJack devices were found on any connection",
    2370: "that input range is not available on this device",
    2373: "differential pairs must be an even AIN with the next odd AIN as negative channel",
    2375: "resolution index out of range for this device",
    2501: "the line is configured as an analog input (T4 flexible I/O)",
    2580: "this extended feature is not supported on this device",
    2583: "the AIN extended feature is not configured on this channel",
    2605: "a stream is running; command-response analog reads are blocked while streaming",
    2607: "a channel in the scan list cannot be streamed",
    2608: "scan rate x number of channels is too high for this device",
}


@dataclass(frozen=True)
class DeviceSpec:
    """Capabilities of one T-series model, from the datasheet and the Modbus map."""

    model: str
    ain_channels: tuple[int, ...]
    #: Valid AIN#_RANGE values in volts (±range). Empty for the T4, whose ranges are fixed.
    ain_ranges_v: tuple[float, ...]
    max_resolution_index: int
    max_stream_resolution_index: int
    dac_max_v: float
    dio_lines: tuple[int, ...]
    #: Aggregate stream sample rate (all channels), samples/s.
    max_stream_samples_per_s: float
    #: Highest scan rate regardless of channel count (T8: 40 kHz per channel, sampled simultaneously).
    max_stream_scan_rate_hz: float
    thermocouple: bool
    #: Single-ended/differential selectable via AIN#_NEGATIVE_CH (T7 only).
    selectable_differential: bool


T4 = DeviceSpec("T4", tuple(range(12)), (), 5, 5, 5.0, tuple(range(4, 20)), 50_000, 50_000, False, False)
T7 = DeviceSpec("T7", tuple(range(14)), (10.0, 1.0, 0.1, 0.01), 8, 8, 5.0, tuple(range(23)), 100_000, 100_000, True, True)
T7_PRO = replace(T7, model="T7-Pro", max_resolution_index=12)
T8 = DeviceSpec(
    "T8",
    tuple(range(8)),
    (11.0, 9.6, 4.8, 2.4, 1.2, 0.6, 0.3, 0.15, 0.075, 0.036, 0.018),
    16,
    16,
    10.0,
    tuple(range(20)),
    320_000,
    40_000,
    True,
    False,
)

_BANKS = (("FIO", 0, 8), ("EIO", 8, 8), ("CIO", 16, 4), ("MIO", 20, 3))


def dio_name(line: int) -> str:
    """Bank name of a DIO number: 0 -> FIO0, 8 -> EIO0, 16 -> CIO0, 20 -> MIO0."""
    for bank, start, count in _BANKS:
        if start <= line < start + count:
            return f"{bank}{line - start}"
    raise ValueError(f"DIO{line} does not exist on T-series devices")


def dio_index(name: str | int) -> int:
    """Parse ``"FIO3"``, ``"EIO0"``, ``"CIO1"``, ``"MIO2"``, ``"DIO5"`` or ``5`` into a DIO number."""
    if isinstance(name, int):
        return name
    text = name.strip().upper()
    if text.isdigit():
        return int(text)
    if text.startswith("DIO") and text[3:].isdigit():
        return int(text[3:])
    for bank, start, count in _BANKS:
        if text.startswith(bank) and text[3:].isdigit() and int(text[3:]) < count:
            return start + int(text[3:])
    raise InstrumentProtocolError(
        f"{name!r} is not a digital I/O name. Use FIO0-7, EIO0-7, CIO0-3, MIO0-2 or DIO0-22."
    )


def load_ljm() -> Any:
    """Import the official LJM wrapper (``from labjack import ljm``).

    Importing it loads the native LabJackM library immediately and, if that fails, the wrapper
    *prints* the error to stdout and continues with no library. stdout is the MCP stdio channel,
    so it is redirected to stderr here and the missing library is turned into a clear error.
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from labjack import ljm
    except ImportError as exc:
        raise InstrumentConnectionError(
            "The `labjack-ljm` Python package is not installed. Install it with "
            "`pip install labjack-ljm`, or use --simulate to try the server without hardware."
        ) from exc
    if getattr(getattr(ljm, "ljm", None), "_staticLib", None) is None:
        raise InstrumentConnectionError(
            "The LabJack LJM library (LabJackM) is not installed or could not be loaded. Install "
            "LJM from https://labjack.com/support/software/installers/ljm (it also installs the "
            "USB driver), or use --simulate to try the server without hardware."
        )
    return ljm


@dataclass
class StreamData:
    channels: list[int]
    scan_rate_hz: float
    #: One list per channel, in volts. Skipped scans (-9999) are replaced by NaN.
    data: list[list[float]]
    skipped_scans: int
    max_device_backlog: int
    max_ljm_backlog: int


@dataclass
class ThermocoupleResult:
    temperature_c: float | None
    thermocouple_voltage_v: float
    cjc_temperature_c: float


class LabJackT:
    """One open T-series device. ``ljm`` is the ``labjack.ljm`` module (or the simulator)."""

    def __init__(self, ljm: Any, handle: int, audit: AuditLog | None = None) -> None:
        self.ljm = ljm
        self.handle = handle
        self.audit = audit
        self.lock = threading.RLock()
        #: DIO lines this server has driven as outputs (released/driven low by `safe_state`).
        self.driven_lines: set[int] = set()
        #: Extra lines to include in `safe_state` (the ``safe_dio`` option).
        self.safe_lines: set[int] = set()
        info = self._call("getHandleInfo", ljm.getHandleInfo, handle)
        self.device_type, self.connection_type, self.serial_number, ip, _port, _ = info
        self.ip_address = ljm.numberToIP(ip) if ip else None
        spec = {DT_T4: T4, DT_T7: T7, DT_T8: T8}.get(self.device_type)
        if spec is None:
            self.close()
            raise InstrumentConnectionError(
                f"LJM opened a device of type {self.device_type}, which this server does not support "
                "(T4, T7/T7-Pro and T8 only)."
            )
        if spec is T7 and int(self.read_names(["HARDWARE_INSTALLED"])[0]) & 1:
            spec = T7_PRO  # bit 0 = high-resolution 24-bit ADC installed
        self.spec = spec

    # ------------------------------------------------------------ low level

    def _log(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, "ljm")

    def _call(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except self.ljm.LJMError as exc:
            code = getattr(exc, "errorCode", None)
            text = getattr(exc, "errorString", "") or str(exc)
            hint = _HINTS.get(code or 0)
            message = f"LJM error {code} ({text}) during {what}" + (f": {hint}." if hint else ".")
            self._log(f"error: {message}")
            if code in _CONNECTION_ERRORS:
                raise InstrumentConnectionError(message) from exc
            raise InstrumentProtocolError(message) from exc

    def read_names(self, names: list[str]) -> list[float]:
        with self.lock:
            values = self._call(f"read of {', '.join(names)}", self.ljm.eReadNames, self.handle, len(names), names)
        self._log("read " + ", ".join(f"{n}={v:.9g}" for n, v in zip(names, values, strict=True)))
        return [float(v) for v in values]

    def write_names(self, names: list[str], values: list[float]) -> None:
        self._log("write " + ", ".join(f"{n}={v:.9g}" for n, v in zip(names, values, strict=True)))
        with self.lock:
            self._call(f"write of {', '.join(names)}", self.ljm.eWriteNames, self.handle, len(names), names, values)

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        serial, firmware, hardware = self.read_names(["SERIAL_NUMBER", "FIRMWARE_VERSION", "HARDWARE_VERSION"])
        info = {
            "manufacturer": "LabJack",
            "model": self.spec.model,
            "serial": str(int(serial)),
            "firmware": f"{firmware:.4f}",
            "hardware_version": f"{hardware:.2f}",
            "connection": CONNECTION_NAMES.get(self.connection_type, str(self.connection_type)),
        }
        if self.ip_address:
            info["ip_address"] = self.ip_address
        with contextlib.suppress(InstrumentError, AttributeError):
            info["device_name"] = self._call(
                "read of DEVICE_NAME_DEFAULT", self.ljm.eReadNameString, self.handle, "DEVICE_NAME_DEFAULT"
            )
        return info

    # ------------------------------------------------------------ validation helpers

    def _check_ain_channels(self, channels: list[int]) -> None:
        if not channels:
            raise InstrumentProtocolError("Give at least one analog input channel.")
        if len(set(channels)) != len(channels):
            raise InstrumentProtocolError(f"Channel list {channels} contains duplicates.")
        valid = self.spec.ain_channels
        bad = [c for c in channels if c not in valid]
        if bad:
            raise InstrumentProtocolError(
                f"AIN{bad[0]} does not exist on the {self.spec.model}; valid analog inputs are "
                f"AIN{valid[0]}-AIN{valid[-1]}."
            )

    def _check_line(self, line: int) -> None:
        if line not in self.spec.dio_lines:
            lo, hi = self.spec.dio_lines[0], self.spec.dio_lines[-1]
            raise InstrumentProtocolError(
                f"DIO{line} does not exist on the {self.spec.model}; valid lines are "
                f"{dio_name(lo)} (DIO{lo}) to {dio_name(hi)} (DIO{hi})."
            )

    def _check_t4_flexible(self, channels: list[int]) -> None:
        """On the T4, AIN4-AIN11 share terminals with FIO4-EIO3. Reading the AIN switches the
        line to analog, which would release a digital output that is driving something."""
        flexible = [c for c in channels if 4 <= c <= 11]
        if self.spec is not T4 or not flexible:
            return
        direction, analog = (int(v) for v in self.read_names(["DIO_DIRECTION", "DIO_ANALOG_ENABLE"]))
        for c in flexible:
            bit = 1 << c
            if not analog & bit and direction & bit:
                raise InstrumentProtocolError(
                    f"AIN{c} shares its terminal with {dio_name(c)}, which is currently a digital "
                    f"OUTPUT. Reading AIN{c} would switch the line to analog input and release "
                    "whatever it drives. Release the line first (set_outputs_safe) or use another input."
                )

    def _match_range(self, range_v: float) -> float:
        for r in self.spec.ain_ranges_v:
            if math.isclose(r, range_v, rel_tol=1e-6):
                return r
        if not self.spec.ain_ranges_v:
            raise InstrumentProtocolError(
                "The T4's input ranges are fixed (AIN0-AIN3: ±10 V, AIN4-AIN11: 0-2.5 V); leave range_v unset."
            )
        valid = ", ".join(f"±{r:g}" for r in self.spec.ain_ranges_v)
        raise InstrumentProtocolError(f"±{range_v:g} V is not a {self.spec.model} input range. Valid: {valid} V.")

    def _check_differential(self, channels: list[int], differential: bool) -> None:
        if not differential:
            return
        if not self.spec.selectable_differential:
            why = "T4 inputs are single-ended only" if self.spec is T4 else "T8 inputs are always isolated differential inputs"
            raise InstrumentProtocolError(f"differential=true is only for the T7 ({why}).")
        for c in channels:
            if c % 2 or c > 12:
                raise InstrumentProtocolError(
                    f"AIN{c} cannot be a differential positive input: use an even channel 0-12; the "
                    "negative input is the next odd channel (AIN0-AIN1, AIN2-AIN3, ...)."
                )
            if c + 1 in channels:
                raise InstrumentProtocolError(f"AIN{c + 1} is the negative input of AIN{c}; don't list both.")

    def _t8_settle(self) -> None:
        # LabJack's example waits 50 ms after changing T8 analog settings (the AIN system restarts).
        if self.spec is T8:
            time.sleep(0.05)

    # ------------------------------------------------------------ analog inputs

    def ain_ranges(self, channels: list[int]) -> list[float]:
        """The ± full-scale range of each channel in volts (T4: fixed 10 V HV / 2.5 V LV)."""
        if self.spec is T4:
            return [10.0 if c < 4 else 2.5 for c in channels]
        values = self.read_names([f"AIN{c}_RANGE" for c in channels])
        default = self.spec.ain_ranges_v[0]
        return [v if v > 0 else default for v in values]

    def negative_inputs(self, channels: list[int]) -> list[str]:
        """What each input is measured against: "GND", "AINn" (T7 differential) or, on the T8,
        the channel's own isolated negative terminal."""
        if self.spec is T8:
            return [f"AIN{c}-" for c in channels]
        if not self.spec.selectable_differential:
            return ["GND"] * len(channels)
        negs = self.read_names([f"AIN{c}_NEGATIVE_CH" for c in channels])
        return ["GND" if int(n) == 199 else f"AIN{int(n)}" for n in negs]

    def read_ain(
        self,
        channels: list[int],
        range_v: float | None = None,
        resolution_index: int | None = None,
        differential: bool | None = None,
    ) -> list[float]:
        """Configure (optionally) and read analog inputs in volts via command-response.

        ``differential`` (T7): True = AINn - AIN(n+1), False = single-ended, None = keep.

        On the T8, several channels are read *simultaneously*: the first as ``AIN#`` (which
        captures all inputs) and the rest from ``AIN#_CAPTURE`` (datasheet 14.3.2).
        """
        spec = self.spec
        with self.lock:
            self._check_ain_channels(channels)
            self._check_differential(channels, bool(differential))
            self._check_t4_flexible(channels)
            names: list[str] = []
            values: list[float] = []
            if range_v is not None:
                r = self._match_range(range_v)
                for c in channels:
                    names.append(f"AIN{c}_RANGE")
                    values.append(r)
            if resolution_index is not None:
                if not 0 <= resolution_index <= spec.max_resolution_index:
                    raise InstrumentProtocolError(
                        f"Resolution index {resolution_index} is out of range for the {spec.model} "
                        f"(0-{spec.max_resolution_index}; 0 = device default)."
                    )
                if spec is T8:  # one shared resolution index for all T8 inputs
                    names.append("AIN_ALL_RESOLUTION_INDEX")
                    values.append(resolution_index)
                else:
                    for c in channels:
                        names.append(f"AIN{c}_RESOLUTION_INDEX")
                        values.append(resolution_index)
            if spec.selectable_differential and differential is not None:
                for c in channels:
                    names.append(f"AIN{c}_NEGATIVE_CH")
                    values.append(c + 1 if differential else 199)  # 199 = single-ended
            if names:
                self.write_names(names, values)
                if range_v is not None or resolution_index is not None:
                    self._t8_settle()
            if spec is T8 and len(channels) > 1:
                read = [f"AIN{channels[0]}"] + [f"AIN{c}_CAPTURE" for c in channels[1:]]
            else:
                read = [f"AIN{c}" for c in channels]
            return self.read_names(read)

    # ------------------------------------------------------------ digital I/O

    def read_dio(self, lines: list[int] | None = None) -> list[dict[str, Any]]:
        """State and direction of digital lines from the DIO_STATE / DIO_DIRECTION bitmasks.

        Unlike reading a single-line register (FIO3, ...), which switches that line to input,
        these bitmask reads do not change any line's direction (Modbus map, DIO_STATE).
        """
        lines = list(self.spec.dio_lines) if lines is None else lines
        for line in lines:
            self._check_line(line)
        names = ["DIO_STATE", "DIO_DIRECTION"] + (["DIO_ANALOG_ENABLE"] if self.spec is T4 else [])
        raw = [int(v) for v in self.read_names(names)]
        state, direction = raw[0], raw[1]
        analog = raw[2] if self.spec is T4 else 0
        out = []
        for line in lines:
            bit = 1 << line
            if analog & bit:
                mode, level = "analog", None
            else:
                mode = "output" if direction & bit else "input"
                level = bool(state & bit)
            out.append({"line": line, "name": dio_name(line), "direction": mode, "high": level})
        return out

    def set_dio(self, line: int, high: bool) -> None:
        """Drive one line high (3.3 V) or low. Writing a single-line register makes it an output."""
        self._check_line(line)
        with self.lock:
            self.write_names([dio_name(line)], [1 if high else 0])
            self.driven_lines.add(line)

    # ------------------------------------------------------------ DAC

    def write_dac(self, dac: int, voltage_v: float) -> float:
        """Set DAC0/DAC1 and return the read-back value (the value last written to the DAC chip)."""
        if dac not in (0, 1):
            raise InstrumentProtocolError("T-series devices have DAC0 and DAC1 only.")
        if not 0.0 <= voltage_v <= self.spec.dac_max_v:
            raise InstrumentProtocolError(
                f"{voltage_v:g} V is outside the {self.spec.model} DAC range of 0-{self.spec.dac_max_v:g} V."
            )
        with self.lock:
            self.write_names([f"DAC{dac}"], [voltage_v])
            return self.read_names([f"DAC{dac}"])[0]

    def read_dacs(self) -> list[float]:
        return self.read_names(["DAC0", "DAC1"])

    # ------------------------------------------------------------ temperatures

    def device_temperature_k(self) -> tuple[float, float, list[float] | None]:
        """(TEMPERATURE_DEVICE_K, TEMPERATURE_AIR_K, T8 per-terminal sensors or None)."""
        device_k, air_k = self.read_names(["TEMPERATURE_DEVICE_K", "TEMPERATURE_AIR_K"])
        terminals = None
        if self.spec is T8:
            names = ["TEMPERATURE0"] + [f"TEMPERATURE{n}_CAPTURE" for n in range(1, 8)]
            terminals = self.read_names(names)
        return device_k, air_k, terminals

    def read_thermocouple(
        self,
        channel: int,
        tc_type: str,
        cjc: Literal["internal", "lm34"] = "internal",
        cjc_channel: int | None = None,
        differential: bool = False,
        resolution_index: int | None = None,
    ) -> ThermocoupleResult:
        """Configure the AIN thermocouple extended feature on ``channel`` and read it (°C)."""
        spec = self.spec
        if not spec.thermocouple:
            raise InstrumentProtocolError(
                "The T4 has no thermocouple extended feature (T7/T8 only). Use an amplifier such as "
                "the LJTick-InAmp with read_analog_inputs instead."
            )
        tc = tc_type.upper()
        if tc not in THERMOCOUPLE_EF_INDEX:
            raise InstrumentProtocolError(f"Unknown thermocouple type {tc_type!r}; use one of {', '.join(THERMOCOUPLE_EF_INDEX)}.")
        with self.lock:
            self._check_ain_channels([channel])
            self._check_differential([channel], differential)
            if cjc == "internal":
                if spec is T8:
                    cjc_address = T8_TEMPERATURE_CAPTURE_ADDRESS + 2 * channel
                else:
                    cjc_address = TEMPERATURE_DEVICE_K_ADDRESS
                slope, offset = 1.0, 0.0
            else:
                if cjc_channel is None:
                    raise InstrumentProtocolError("cjc='lm34' needs cjc_channel (the AIN the LM34 is wired to).")
                self._check_ain_channels([cjc_channel])
                if cjc_channel in (channel, channel + 1 if differential else channel):
                    raise InstrumentProtocolError("The LM34 must be on a different AIN than the thermocouple.")
                cjc_address = 2 * cjc_channel  # AIN n lives at Modbus address 2n
                slope, offset = LM34_SLOPE_K_PER_V, LM34_OFFSET_K
            p = f"AIN{channel}_EF_"
            names = [p + "INDEX", p + "CONFIG_A", p + "CONFIG_B", p + "CONFIG_D", p + "CONFIG_E"]
            values: list[float] = [THERMOCOUPLE_EF_INDEX[tc], 1, cjc_address, slope, offset]  # CONFIG_A 1 = °C
            if spec.selectable_differential:
                names.append(f"AIN{channel}_NEGATIVE_CH")
                values.append(channel + 1 if differential else 199)
            if resolution_index is not None:
                if not 0 <= resolution_index <= spec.max_resolution_index:
                    raise InstrumentProtocolError(
                        f"Resolution index {resolution_index} is out of range for the {spec.model} "
                        f"(0-{spec.max_resolution_index})."
                    )
                names.append("AIN_ALL_RESOLUTION_INDEX" if spec is T8 else f"AIN{channel}_RESOLUTION_INDEX")
                values.append(resolution_index)
            self.write_names(names, values)
            self._t8_settle()
            # Only READ_A triggers a new measurement; B and C return values from that measurement.
            temp, volts, cjc_c = self.read_names([p + "READ_A", p + "READ_B", p + "READ_C"])
        return ThermocoupleResult(None if temp == DUMMY_VALUE else temp, volts, cjc_c)

    # ------------------------------------------------------------ stream

    def stream_ain(
        self,
        channels: list[int],
        scan_rate_hz: float,
        num_scans: int,
        range_v: float | None = None,
        resolution_index: int | None = None,
    ) -> StreamData:
        """Hardware-timed acquisition of ``num_scans`` scans with the LJM stream functions.

        Configuration follows LabJack's ``stream_basic.py`` example for each device type.
        The stream is always stopped, even if reading fails.
        """
        spec = self.spec
        n = len(channels)
        self._check_ain_channels(channels)
        if spec is T8:
            if channels != list(range(channels[0], channels[0] + n)):
                raise InstrumentProtocolError(
                    "On the T8 all streamed AINs must be adjacent and in ascending order (e.g. [0, 1, 2])."
                )
            if scan_rate_hz > spec.max_stream_scan_rate_hz:
                raise InstrumentProtocolError(f"The T8 streams at most {spec.max_stream_scan_rate_hz:g} scans/s.")
        elif scan_rate_hz * n > spec.max_stream_samples_per_s:
            raise InstrumentProtocolError(
                f"{n} channels x {scan_rate_hz:g} Hz = {scan_rate_hz * n:g} samples/s exceeds the "
                f"{spec.model} stream maximum of {spec.max_stream_samples_per_s:g} samples/s."
            )
        if resolution_index is not None and not 0 <= resolution_index <= spec.max_stream_resolution_index:
            raise InstrumentProtocolError(
                f"Stream resolution index must be 0-{spec.max_stream_resolution_index} on the {spec.model}"
                + (" (the T7-Pro 24-bit ADC, indices 9-12, cannot stream)." if spec is T7_PRO else ".")
            )
        if num_scans * n > MAX_STREAM_SAMPLES:
            raise InstrumentProtocolError(
                f"{num_scans} scans x {n} channels is more than the {MAX_STREAM_SAMPLES} samples this "
                "server buffers. Shorten the duration or lower the scan rate."
            )
        res = 0 if resolution_index is None else resolution_index
        r = None if range_v is None else self._match_range(range_v)  # T4: explains fixed ranges
        with self.lock:
            self._check_t4_flexible(channels)
            if spec is T4:
                names, values = ["STREAM_SETTLING_US", "STREAM_RESOLUTION_INDEX"], [0.0, float(res)]
            else:
                # Ensure triggered stream is off and the internal clock is used.
                self.write_names(["STREAM_TRIGGER_INDEX", "STREAM_CLOCK_SOURCE"], [0, 0])
                names, values = ["STREAM_RESOLUTION_INDEX"], [float(res)]
                if r is not None:
                    names += [f"AIN{c}_RANGE" for c in channels]
                    values += [r] * n
                if spec.selectable_differential:  # T7: single-ended, auto settling
                    names += [f"AIN{c}_NEGATIVE_CH" for c in channels] + ["STREAM_SETTLING_US"]
                    values += [199.0] * n + [0.0]
            self.write_names(names, values)
            self._t8_settle()
            scan_names = [f"AIN{c}" for c in channels]
            addresses = self._call("namesToAddresses", self.ljm.namesToAddresses, n, scan_names)[0]
            scans_per_read = max(1, min(num_scans, int(scan_rate_hz / 4)))
            self._log(f"eStreamStart {scan_names} at {scan_rate_hz:g} Hz, {num_scans} scans")
            actual = self._call(
                "eStreamStart", self.ljm.eStreamStart, self.handle, scans_per_read, n, addresses, scan_rate_hz
            )
            raw: list[float] = []
            max_dev = max_ljm = 0
            try:
                while len(raw) < num_scans * n:
                    data, dev_backlog, ljm_backlog = self._call("eStreamRead", self.ljm.eStreamRead, self.handle)
                    raw.extend(data)
                    max_dev, max_ljm = max(max_dev, dev_backlog), max(max_ljm, ljm_backlog)
            finally:
                self._stop_stream()
        raw = raw[: num_scans * n]
        skipped = sum(1 for v in raw[::n] if v == DUMMY_VALUE)
        per_channel = [[math.nan if v == DUMMY_VALUE else v for v in raw[i::n]] for i in range(n)]
        self._log(f"stream done: {num_scans} scans at {actual:g} Hz, {skipped} skipped")
        return StreamData(channels, float(actual), per_channel, skipped, max_dev, max_ljm)

    def _stop_stream(self) -> None:
        try:
            self.ljm.eStreamStop(self.handle)
            self._log("eStreamStop")
        except self.ljm.LJMError as exc:  # not running / already stopped
            self._log(f"eStreamStop: {exc}")

    # ------------------------------------------------------------ safety

    def safe_state(self, dio_mode: Literal["input", "low"] = "input") -> list[str]:
        """Stop any stream, set both DACs to 0 V and release (input) or drive low every DIO line
        this server drove plus the configured ``safe_dio`` lines. Tries every step."""
        done: list[str] = []
        errors: list[str] = []
        with self.lock:
            self._stop_stream()
            try:
                self.write_names(["DAC0", "DAC1"], [0.0, 0.0])
                done.append("DAC0 and DAC1 set to 0 V")
            except InstrumentError as exc:
                errors.append(str(exc))
            for line in sorted(self.driven_lines | self.safe_lines):
                name = dio_name(line)
                try:
                    if dio_mode == "input":
                        self.read_names([name])  # a single-line read switches the line to input
                        self.driven_lines.discard(line)
                        done.append(f"{name} set to input")
                    else:
                        self.write_names([name], [0])
                        done.append(f"{name} driven low")
                except InstrumentError as exc:
                    errors.append(str(exc))
        if errors:
            raise InstrumentProtocolError(
                "Safe state only partly applied. Done: " + ("; ".join(done) or "nothing") + ". Failed: " + "; ".join(errors)
            )
        return done

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ljm.close(self.handle)


def open_device(
    ljm: Any,
    device_type: str = "ANY",
    connection_type: str = "ANY",
    identifier: str = "ANY",
    audit: AuditLog | None = None,
) -> LabJackT:
    """``ljm.openS(device_type, connection_type, identifier)`` and wrap the handle."""
    try:
        handle = ljm.openS(device_type, connection_type, identifier)
    except ljm.LJMError as exc:
        code = getattr(exc, "errorCode", None)
        hint = _HINTS.get(code or 0, "")
        raise InstrumentConnectionError(
            f"Could not open a LabJack (device_type={device_type}, connection_type={connection_type}, "
            f"identifier={identifier}): LJM error {code} {getattr(exc, 'errorString', exc)}"
            + (f" - {hint}." if hint else ".")
        ) from exc
    if audit is not None:
        audit.event(f"openS({device_type}, {connection_type}, {identifier}) -> handle {handle}", "ljm")
    return LabJackT(ljm, handle, audit)
