"""A fake ``labjack.ljm`` module: a simulated T4 / T7 / T7-Pro / T8 behind the same LJM API.

It implements the subset of LJM functions the driver uses (``openS``, ``getHandleInfo``,
``eReadNames``, ``eWriteNames``, ``namesToAddresses``, ``eStreamStart/Read/Stop`` ...) and
raises ``LJMError`` with the real LJM/device error codes (e.g. 2370 AIN_RANGE_INVALID,
2605 STREAM_IS_ACTIVE, 1294 LJME_INVALID_NAME) so ``--simulate`` exercises the driver's
validation and error handling.

Simulated wiring (all voltages in V):

* AIN0 - 2 V amplitude, 10 Hz sine (a function generator)
* AIN1 - NTC thermistor divider (10 kOhm NTC, B = 3950, 10 kOhm to 5 V) at ~24 °C, drifting slowly
* AIN2 - loopback from DAC0
* AIN3 - LM34 cold-junction sensor (10 mV/°F) at the screw-terminal temperature
* other AINs - grounded (noise only); thermocouple feature: TC at ~37 °C on AIN0-AIN3, open elsewhere
* digital inputs read high (internal pull-ups)
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from types import SimpleNamespace

_ERROR_NAMES = {
    1225: "LJME_STREAM_NOT_INITIALIZED",
    1227: "LJME_DEVICE_NOT_FOUND",
    1294: "LJME_INVALID_NAME",
    2370: "AIN_RANGE_INVALID",
    2373: "AIN_NEGATIVE_CHANNEL_INVALID",
    2375: "AIN_RESOLUTION_INVALID",
    2580: "AIN_EF_INVALID_TYPE",
    2583: "AIN_EF_CHANNEL_INACTIVE",
    2605: "STREAM_IS_ACTIVE",
    2607: "STREAM_CHN_LIST_INVALID",
    2608: "STREAM_SCAN_RATE_INVALID",
}


class SimLJMError(Exception):
    """Same attributes as ``labjack.ljm.LJMError``."""

    def __init__(self, errorCode: int | None = None, errorAddress: int | None = None, errorString: str | None = None) -> None:  # noqa: N803
        self._errorCode = errorCode
        self._errorAddress = errorAddress
        self._errorString = errorString if errorString is not None else _ERROR_NAMES.get(errorCode or 0, "")
        super().__init__(str(self))

    @property
    def errorCode(self) -> int | None:  # noqa: N802 - LJM's naming
        return self._errorCode

    @property
    def errorAddress(self) -> int | None:  # noqa: N802
        return self._errorAddress

    @property
    def errorString(self) -> str:  # noqa: N802
        return self._errorString

    def __str__(self) -> str:
        return f"LJM library error code {self._errorCode} {self._errorString}"


_MODELS = {
    # model: (device type, number of AINs, valid ranges (0 = default), max CR res, max stream res,
    #         DAC min, DAC max, DIO lines, max samples/s, firmware, hardware version)
    "T4": (4, 12, (0.0,), 5, 5, 0.0, 5.0, range(4, 20), 50_000, 1.0029, 1.20),
    "T7": (7, 14, (0.0, 10.0, 1.0, 0.1, 0.01), 8, 8, 0.0, 5.0, range(23), 100_000, 1.0299, 1.35),
    "T7-PRO": (7, 14, (0.0, 10.0, 1.0, 0.1, 0.01), 12, 8, 0.0, 5.0, range(23), 100_000, 1.0299, 1.35),
    "T8": (8, 8, (0.0, 11.0, 9.6, 4.8, 2.4, 1.2, 0.6, 0.3, 0.15, 0.075, 0.036, 0.018), 16, 16, -0.1, 10.3, range(20), 40_000, 1.0025, 1.30),
}
_SEEBECK_V_PER_C = {20: 61e-6, 21: 51.7e-6, 22: 40.7e-6, 23: 5.9e-6, 24: 40.6e-6, 25: 5.9e-6, 27: 26.1e-6, 28: 0.9e-6, 30: 13.4e-6}
_BANKS = {"FIO": (0, 8), "EIO": (8, 8), "CIO": (16, 4), "MIO": (20, 3), "DIO": (0, 23)}


class _SimDevice:
    def __init__(self, model: str, serial: int, seed: int | None) -> None:
        key = model.upper()
        if key not in _MODELS:
            raise ValueError(f"Unknown simulated model {model!r}; use T4, T7, T7-Pro or T8")
        (self.device_type, self.n_ain, self.valid_ranges, self.max_res, self.max_stream_res,
         self.dac_min, self.dac_max, lines, self.max_rate, self.firmware, self.hardware) = _MODELS[key]
        self.model = key
        self.lines = set(lines)
        self.serial = serial
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.range = {c: 0.0 for c in range(self.n_ain)}
        self.res = {c: 0 for c in range(self.n_ain)}
        self.neg = {c: 199 for c in range(self.n_ain)}
        self.settling = {c: 0.0 for c in range(self.n_ain)}
        self.ef_index: dict[int, int] = {}
        self.ef_config: dict[tuple[int, str], float] = {}
        self.ef_result: dict[tuple[int, str], float] = {}
        self.capture: dict[int, float] = {}
        self.temp_capture: dict[int, float] = {}
        self.dac = [0.0, 0.0]
        self.direction = 0
        self.output = 0
        self.analog_enable = sum(1 << b for b in range(4, 12)) if key == "T4" else 0
        self.misc: dict[str, float] = {"STREAM_SETTLING_US": 0, "STREAM_RESOLUTION_INDEX": 0,
                                       "STREAM_TRIGGER_INDEX": 0, "STREAM_CLOCK_SOURCE": 0,
                                       "AIN_SAMPLING_RATE_HZ": 100, "DIO_INHIBIT": 0}
        self.stream: dict[str, object] | None = None

    # -- physics ------------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self.t0

    def device_temp_k(self, t: float) -> float:
        return 301.2 + 0.3 * math.sin(2 * math.pi * t / 900.0)

    def full_scale(self, c: int) -> tuple[float, float]:
        if self.model == "T4":
            return (-10.3, 10.1) if c < 4 else (0.0, 2.5)
        r = self.range[c] or self.valid_ranges[1]
        if self.model == "T8":
            return -1.02 * r, 1.02 * r
        return -1.06 * r, 1.01 * r  # T7: about -10.6 .. +10.1 V on the ±10 V range

    def noise_v(self, c: int, res: int) -> float:
        if self.model == "T4":
            return 1.0e-3
        default = {"T7": 8, "T7-PRO": 9, "T8": 9}[self.model]
        eff = res or default
        r = self.range[c] or self.valid_ranges[1]
        return max(0.2e-6, (r / 10.0) * 316e-6 / (2 ** ((eff - 1) / 2.4)))

    def signal_v(self, c: int, t: float, res: int | None = None) -> float:
        if c == 0:
            v = 2.0 * math.sin(2 * math.pi * 10.0 * t)
        elif c == 1:
            temp_c = 24.0 + 0.4 * math.sin(2 * math.pi * t / 900.0)
            r_ntc = 10_000 * math.exp(3950 * (1 / (temp_c + 273.15) - 1 / 298.15))
            v = 5.0 * r_ntc / (r_ntc + 10_000)
        elif c == 2:
            v = self.dac[0] * 0.9995  # DAC0 -> AIN2 jumper
        elif c == 3:
            terminal_f = (self.device_temp_k(t) - 273.15 - 3.0) * 9 / 5 + 32
            v = terminal_f / 100.0  # LM34: 10 mV/°F
        else:
            v = 0.0
        v += self.rng.gauss(0.0, self.noise_v(c, self.res[c] if res is None else res))
        lo, hi = self.full_scale(c)
        return min(max(v, lo), hi)

    # -- registers ----------------------------------------------------------

    def _error(self, code: int) -> SimLJMError:
        return SimLJMError(code)

    def _line(self, bank: str, num: str) -> int:
        start, count = _BANKS[bank]
        n = int(num)
        if n >= count or start + n not in self.lines:
            raise self._error(1294)
        return start + n

    def _check_ain(self, c: int) -> None:
        if c >= self.n_ain:
            raise self._error(1294)

    def read(self, name: str) -> float:
        name = name.upper()
        t = self.now()
        if m := re.fullmatch(r"AIN(\d+)", name):
            c = int(m[1])
            self._check_ain(c)
            if self.stream is not None:
                raise self._error(2605)
            if self.model == "T8":  # any AIN read captures all channels simultaneously
                self.capture = {ch: self.signal_v(ch, t) for ch in range(self.n_ain)}
                self.temp_capture = {ch: self.device_temp_k(t) + 0.05 * ch for ch in range(8)}
                return self.capture[c]
            return self.signal_v(c, t)
        if (m := re.fullmatch(r"AIN(\d+)_CAPTURE", name)) and self.model == "T8":
            self._check_ain(int(m[1]))
            return self.capture.get(int(m[1]), 0.0)
        if m := re.fullmatch(r"AIN(\d+)_(RANGE|RESOLUTION_INDEX|NEGATIVE_CH|SETTLING_US)", name):
            c = int(m[1])
            self._check_ain(c)
            table = {"RANGE": self.range, "RESOLUTION_INDEX": self.res, "NEGATIVE_CH": self.neg, "SETTLING_US": self.settling}[m[2]]
            return float(table[c])
        if m := re.fullmatch(r"AIN(\d+)_EF_READ_([A-D])", name):
            return self._ef_read(int(m[1]), m[2], t)
        if m := re.fullmatch(r"AIN(\d+)_EF_(INDEX|CONFIG_[A-J])", name):
            c = int(m[1])
            if m[2] == "INDEX":
                return float(self.ef_index.get(c, 0))
            return self.ef_config.get((c, m[2][-1]), 0.0)
        if m := re.fullmatch(r"DAC([01])", name):
            return self.dac[int(m[1])]
        if m := re.fullmatch(r"(FIO|EIO|CIO|MIO|DIO)(\d+)", name):
            line = self._line(m[1], m[2])
            bit = 1 << line
            self.direction &= ~bit  # reading a single line makes it an input
            self.analog_enable &= ~bit
            return 1.0  # internal pull-up
        if name == "DIO_STATE":
            inputs = ~self.direction & ~self.analog_enable
            mask = sum(1 << b for b in self.lines)
            return float(((self.output & self.direction) | inputs) & mask)
        if name == "DIO_DIRECTION":
            return float(self.direction)
        if name == "DIO_ANALOG_ENABLE" and self.model == "T4":
            return float(self.analog_enable)
        if name == "TEMPERATURE_DEVICE_K":
            if self.stream is not None:
                raise self._error(2605)
            return self.device_temp_k(t) + self.rng.gauss(0, 0.02)
        if name == "TEMPERATURE_AIR_K":
            return self.device_temp_k(t) - 4.3 + self.rng.gauss(0, 0.02)
        if self.model == "T8" and (m := re.fullmatch(r"TEMPERATURE([0-7])(_CAPTURE)?", name)):
            if not m[2]:
                self.read("AIN0")
            return self.temp_capture.get(int(m[1]), self.device_temp_k(t))
        fixed = {
            "SERIAL_NUMBER": float(self.serial),
            "PRODUCT_ID": float(self.device_type),
            "FIRMWARE_VERSION": self.firmware,
            "HARDWARE_VERSION": self.hardware,
            "BOOTLOADER_VERSION": 0.9400,
            "HARDWARE_INSTALLED": 15.0 if self.model == "T7-PRO" else 0.0,
        }
        if name in fixed:
            return fixed[name]
        if name in self.misc and not (self.model == "T4" and name in {"STREAM_TRIGGER_INDEX", "STREAM_CLOCK_SOURCE", "AIN_SAMPLING_RATE_HZ"}):
            return float(self.misc[name])
        raise self._error(1294)

    def write(self, name: str, value: float) -> None:
        name = name.upper()
        if m := re.fullmatch(r"AIN(\d+)_RANGE", name):
            c = int(m[1])
            self._check_ain(c)
            if not any(math.isclose(value, r, rel_tol=1e-6, abs_tol=1e-12) for r in self.valid_ranges):
                raise self._error(2370)
            self.range[c] = float(value)
            return
        if m := re.fullmatch(r"AIN(\d+)_RESOLUTION_INDEX|AIN_ALL_RESOLUTION_INDEX", name):
            if not 0 <= value <= self.max_res or value != int(value):
                raise self._error(2375)
            targets = range(self.n_ain) if (m[1] is None or self.model == "T8") else [int(m[1])]
            for c in targets:
                self._check_ain(c)
                self.res[c] = int(value)
            return
        if m := re.fullmatch(r"AIN(\d+)_NEGATIVE_CH", name):
            c = int(m[1])
            self._check_ain(c)
            if self.model.startswith("T7") and int(value) != 199 and (c % 2 or c > 12 or int(value) != c + 1):
                raise self._error(2373)
            self.neg[c] = int(value)
            return
        if m := re.fullmatch(r"AIN(\d+)_SETTLING_US", name):
            self.settling[int(m[1])] = float(value)
            return
        if m := re.fullmatch(r"AIN(\d+)_EF_INDEX", name):
            c = int(m[1])
            self._check_ain(c)
            if int(value) in _SEEBECK_V_PER_C and self.model == "T4":
                raise self._error(2580)
            self.ef_index[c] = int(value)
            for letter in "ABCDEFGHIJ":  # a new index restores the feature's defaults
                self.ef_config.pop((c, letter), None)
            return
        if m := re.fullmatch(r"AIN(\d+)_EF_CONFIG_([A-J])", name):
            self.ef_config[(int(m[1]), m[2])] = float(value)
            return
        if m := re.fullmatch(r"DAC([01])", name):
            self.dac[int(m[1])] = min(max(float(value), self.dac_min), self.dac_max)
            return
        if m := re.fullmatch(r"(FIO|EIO|CIO|MIO|DIO)(\d+)", name):
            line = self._line(m[1], m[2])
            bit = 1 << line
            self.direction |= bit  # writing a single line makes it an output
            self.analog_enable &= ~bit
            self.output = (self.output | bit) if value else (self.output & ~bit)
            return
        if name == "STREAM_RESOLUTION_INDEX" and not 0 <= value <= self.max_stream_res:
            raise SimLJMError(2606, errorString="STREAM_CONFIG_INVALID")
        if name in self.misc and not (self.model == "T4" and name in {"STREAM_TRIGGER_INDEX", "STREAM_CLOCK_SOURCE", "AIN_SAMPLING_RATE_HZ"}):
            self.misc[name] = float(value)
            return
        raise self._error(1294)

    def _ef_read(self, c: int, letter: str, t: float) -> float:
        self._check_ain(c)
        index = self.ef_index.get(c, 0)
        if index not in _SEEBECK_V_PER_C:
            raise self._error(2583)
        if letter != "A":
            return self.ef_result.get((c, letter), 0.0)
        address = int(self.ef_config.get((c, "B"), 60052))
        slope = self.ef_config.get((c, "D"), 1.0)
        offset = self.ef_config.get((c, "E"), 0.0)
        if address == 60052 and self.model == "T8":
            # The T8 firmware maps the default TEMPERATURE_DEVICE_K to TEMPERATURE#_CAPTURE.
            raw = self.device_temp_k(t) + 0.05 * c
        elif address == 60052:
            raw = self.device_temp_k(t)
        elif 700 <= address < 716:
            raw = self.device_temp_k(t) + 0.05 * ((address - 700) // 2)
        elif address % 2 == 0 and address // 2 < self.n_ain:
            raw = self.signal_v(address // 2, t)
        else:
            raise SimLJMError(2586, errorString="AIN_EF_INVALID_CJC_REGISTER")
        cjc_k = raw * slope + offset
        units = int(self.ef_config.get((c, "A"), 0))
        convert = (lambda k: k) if units == 0 else (lambda k: k - 273.15) if units == 1 else (lambda k: (k - 273.15) * 9 / 5 + 32)
        seebeck = _SEEBECK_V_PER_C[index]
        if c <= 3:
            hot_c = 37.0 + 0.2 * math.sin(2 * math.pi * t / 120.0) + self.rng.gauss(0, 0.02)
            tc_v = seebeck * (hot_c - (cjc_k - 273.15))
            result = convert(hot_c + 273.15)
        else:  # nothing connected: the floating input is outside the thermocouple range
            tc_v = 0.08 + self.rng.gauss(0, 1e-4)
            result = -9999.0
        self.ef_result[(c, "B")] = tc_v
        self.ef_result[(c, "C")] = convert(cjc_k)
        self.ef_result[(c, "D")] = seebeck * (cjc_k - 273.15)
        return result


class SimulatedLJM:
    """Stand-in for the ``labjack.ljm`` module with one simulated device attached."""

    LJMError = SimLJMError
    constants = SimpleNamespace(
        dtANY=0, dtT4=4, dtT7=7, dtT8=8, ctANY=0, ctUSB=1, ctTCP=2, ctETHERNET=3, ctWIFI=4,
        UINT16=0, UINT32=1, INT32=2, FLOAT32=3,
    )

    def __init__(self, model: str = "T7-Pro", serial: int = 470012345, seed: int | None = 0) -> None:
        self.device = _SimDevice(model, serial, seed)
        self._handles: dict[int, _SimDevice] = {}
        self._next = 1

    def _dev(self, handle: int) -> _SimDevice:
        if handle not in self._handles:
            raise SimLJMError(1224, errorString="LJME_DEVICE_NOT_OPEN")
        return self._handles[handle]

    # -- open/close ---------------------------------------------------------

    def openS(self, deviceType: str = "ANY", connectionType: str = "ANY", identifier: str = "ANY") -> int:  # noqa: N802,N803
        dt = str(deviceType).upper().replace("LJM_DT", "") or "ANY"
        ct = str(connectionType).upper().replace("LJM_CT", "") or "ANY"
        ident = str(identifier).strip() or "ANY"
        model = "T7" if self.device.model == "T7-PRO" else self.device.model
        if dt not in {"ANY", model} or ct not in {"ANY", "USB"} or ident.upper() not in {"ANY", "LJM_IDANY", str(self.device.serial)}:
            raise SimLJMError(1227)
        handle = self._next
        self._next += 1
        self._handles[handle] = self.device
        return handle

    def getHandleInfo(self, handle: int) -> tuple[int, int, int, int, int, int]:  # noqa: N802
        dev = self._dev(handle)
        return dev.device_type, 1, dev.serial, 0, 0, 64

    @staticmethod
    def numberToIP(number: int) -> str:  # noqa: N802
        return ".".join(str((number >> s) & 0xFF) for s in (24, 16, 8, 0))

    def close(self, handle: int) -> None:
        dev = self._handles.pop(handle, None)
        if dev is not None:
            dev.stream = None

    # -- single values ------------------------------------------------------

    def eReadName(self, handle: int, name: str) -> float:  # noqa: N802
        dev = self._dev(handle)
        with dev.lock:
            return dev.read(name)

    def eReadNames(self, handle: int, numFrames: int, aNames: list[str]) -> list[float]:  # noqa: N802,N803
        dev = self._dev(handle)
        with dev.lock:
            return [dev.read(n) for n in aNames[:numFrames]]

    def eWriteName(self, handle: int, name: str, value: float) -> None:  # noqa: N802
        dev = self._dev(handle)
        with dev.lock:
            dev.write(name, value)

    def eWriteNames(self, handle: int, numFrames: int, aNames: list[str], aValues: list[float]) -> None:  # noqa: N802,N803
        dev = self._dev(handle)
        with dev.lock:
            for n, v in zip(aNames[:numFrames], aValues[:numFrames], strict=False):
                dev.write(n, v)

    def eReadNameString(self, handle: int, name: str) -> str:  # noqa: N802
        self._dev(handle)
        if name.upper() != "DEVICE_NAME_DEFAULT":
            raise SimLJMError(1294)
        return f"LabMCP-Sim-{self.device.model}"

    # -- stream -------------------------------------------------------------

    def namesToAddresses(self, numFrames: int, aNames: list[str]) -> tuple[list[int], list[int]]:  # noqa: N802,N803
        addresses, types = [], []
        for name in aNames[:numFrames]:
            m = re.fullmatch(r"AIN(\d+)", name.upper())
            if not m:
                raise SimLJMError(1294)
            addresses.append(2 * int(m[1]))
            types.append(3)
        return addresses, types

    def eStreamStart(self, handle: int, scansPerRead: int, numAddresses: int, aScanList: list[int], scanRate: float) -> float:  # noqa: N802,N803
        dev = self._dev(handle)
        with dev.lock:
            if dev.stream is not None:
                raise SimLJMError(2605)
            if numAddresses < 1 or any(a % 2 or a // 2 >= dev.n_ain for a in aScanList[:numAddresses]):
                raise SimLJMError(2607)
            per_scan = 1 if dev.model == "T8" else numAddresses
            if scanRate <= 0 or scanRate * per_scan > dev.max_rate:
                raise SimLJMError(2608)
            actual = float(scanRate)
            if dev.model != "T8" and scanRate > 152.588:  # scan interval in multiples of 100 ns
                actual = 10e6 / round(10e6 / scanRate)
            res = int(dev.misc["STREAM_RESOLUTION_INDEX"])
            dev.stream = {"channels": [a // 2 for a in aScanList[:numAddresses]], "spr": int(scansPerRead),
                          "rate": actual, "t_start": dev.now(), "wall_start": time.monotonic(), "emitted": 0, "res": res}
            return actual

    def eStreamRead(self, handle: int) -> tuple[list[float], int, int]:  # noqa: N802
        dev = self._dev(handle)
        st = dev.stream
        if st is None:
            raise SimLJMError(1303, errorString="LJME_STREAM_NOT_RUNNING")
        rate, spr, emitted = st["rate"], st["spr"], st["emitted"]
        ready_at = st["wall_start"] + (emitted + spr) / rate  # type: ignore[operator]
        delay = ready_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)  # data arrives in real time, like the hardware
        data: list[float] = []
        with dev.lock:
            for k in range(emitted, emitted + spr):  # type: ignore[arg-type]
                t = st["t_start"] + k / rate  # type: ignore[operator]
                data.extend(dev.signal_v(c, t, max(1, st["res"])) for c in st["channels"])  # type: ignore[union-attr]
            st["emitted"] = emitted + spr  # type: ignore[operator]
        return data, 0, 0

    def eStreamStop(self, handle: int) -> None:  # noqa: N802
        dev = self._dev(handle)
        if dev.stream is None:
            raise SimLJMError(1225)
        dev.stream = None
