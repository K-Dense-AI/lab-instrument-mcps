"""Simulated Bluetooth LE backend: fake health sensors that emit byte-exact GATT packets.

The simulator implements the same interface as :class:`~labmcp_ble_health.driver.BleakBackend`
and encodes every characteristic value itself (independently of the driver's decoders), so
``--simulate`` and the tests run the real parsers on correctly encoded Bluetooth SIG packets:
little-endian fields, IEEE 11073-20601 SFLOAT/FLOAT values, flags bytes, Date Time structs.

Physiology is plausible but synthetic: a resting heart rate around 64 bpm with respiratory
sinus arrhythmia (RR intervals, SDNN ~40 ms), SpO2 around 97 %, a blood pressure of about
121/79 mmHg, a tympanic temperature of 36.8 C and a 72.35 kg weight. Measurement devices
(blood pressure, thermometer, scale, spot-check oximeter) complete in a few seconds rather
than the ~30-60 s a real measurement takes. The addresses use the IANA documentation MAC
range 00:00:5E:00:53:xx, so they can never collide with a real device.
"""

from __future__ import annotations

import math
import random
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from labmcp import InstrumentConnectionError, InstrumentProtocolError

from labmcp_ble_health.driver import ScanResult, uuid16

SIM_KIT = "00:00:5E:00:53:01"
SIM_HR_STRAP = "00:00:5E:00:53:02"
SIM_OXIMETER = "00:00:5E:00:53:03"
SIM_BP_MONITOR = "00:00:5E:00:53:04"
SIM_THERMOMETER = "00:00:5E:00:53:05"
SIM_SCALE = "00:00:5E:00:53:06"
SIM_DEFAULT_ADDRESS = SIM_KIT

_DIS = [0x2A29, 0x2A24, 0x2A25, 0x2A27, 0x2A26, 0x2A28]
_PROFILE_CHARS = {
    "heart_rate": (0x180D, [0x2A37, 0x2A38]),
    "pulse_oximeter": (0x1822, [0x2A5E, 0x2A5F, 0x2A60]),
    "blood_pressure": (0x1810, [0x2A35, 0x2A36, 0x2A49]),
    "health_thermometer": (0x1809, [0x2A1C, 0x2A1D, 0x2A1E]),
    "weight_scale": (0x181D, [0x2A9D, 0x2A9E]),
    "battery": (0x180F, [0x2A19]),
    "device_information": (0x180A, _DIS + [0x2A23]),
}


@dataclass
class SimDevice:
    name: str
    model: str
    profiles: list[str]
    rssi_dbm: int
    advertised: list[str] = field(default_factory=list)  # profiles listed in advertisements


SIM_DEVICES: dict[str, SimDevice] = {
    SIM_KIT: SimDevice(
        "SIM Health Kit",
        "LabMCP-SIM-KIT",
        ["heart_rate", "pulse_oximeter", "blood_pressure", "health_thermometer", "weight_scale", "battery", "device_information"],
        -48,
        ["heart_rate"],
    ),
    SIM_HR_STRAP: SimDevice("SIM HR Strap", "LabMCP-SIM-HR", ["heart_rate", "battery", "device_information"], -55, ["heart_rate"]),
    SIM_OXIMETER: SimDevice("SIM Oximeter", "LabMCP-SIM-PLX", ["pulse_oximeter", "battery", "device_information"], -61, ["pulse_oximeter"]),
    SIM_BP_MONITOR: SimDevice("SIM BP Monitor", "LabMCP-SIM-BPM", ["blood_pressure", "battery", "device_information"], -67, ["blood_pressure"]),
    SIM_THERMOMETER: SimDevice("SIM Thermometer", "LabMCP-SIM-HTM", ["health_thermometer", "battery", "device_information"], -70, ["health_thermometer"]),
    SIM_SCALE: SimDevice("SIM Scale", "LabMCP-SIM-WSC", ["weight_scale", "battery", "device_information"], -74, ["weight_scale"]),
}


# ----------------------------------------------------------------- encoders (independent of driver)


def enc_sfloat(value: float | None, exponent: int = 0, special: int | None = None) -> bytes:
    """IEEE 11073-20601 SFLOAT: 4-bit signed exponent (high nibble), 12-bit signed mantissa."""
    if special is not None or value is None:
        return struct.pack("<H", 0x07FF if special is None else special)  # default NaN
    mantissa = round(value / 10**exponent)
    if not -2046 <= mantissa <= 2045:
        raise ValueError(f"{value} does not fit an SFLOAT with exponent {exponent}")
    return struct.pack("<H", ((exponent & 0xF) << 12) | (mantissa & 0x0FFF))


def enc_float(value: float | None, exponent: int = -1) -> bytes:
    """IEEE 11073-20601 FLOAT: 8-bit signed exponent (high octet), 24-bit signed mantissa."""
    if value is None:
        return struct.pack("<I", 0x007FFFFF)  # NaN
    mantissa = round(value / 10**exponent)
    return struct.pack("<I", ((exponent & 0xFF) << 24) | (mantissa & 0xFFFFFF))


def enc_date_time(dt: datetime) -> bytes:
    """GSS 3.80 Date Time: uint16 year, then month, day, hours, minutes, seconds (uint8)."""
    return struct.pack("<HBBBBB", dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


# ----------------------------------------------------------------- backend

Emission = tuple[float, int, bytes]  # (delay before sending in s, characteristic, payload)


class SimulatedBLEBackend:
    """Drop-in replacement for BleakBackend with six simulated devices."""

    def __init__(self, address: str | None, seed: int | None = 0) -> None:
        self.address = address
        self.rng = random.Random(seed)
        self._connected = False
        self._callbacks: dict[int, Callable[[bytes], None]] = {}
        self._stops: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._energy_kj = 0.0
        self._t_hr = 0.0

    # -- discovery / connection ------------------------------------------------

    def scan(self, timeout_s: float) -> list[ScanResult]:
        time.sleep(min(timeout_s, 0.5))
        return [
            ScanResult(
                address=addr,
                name=dev.name,
                rssi_dbm=dev.rssi_dbm + self.rng.randint(-3, 3),
                service_uuids=[uuid16(_PROFILE_CHARS[p][0]) for p in dev.advertised],
                tx_power_dbm=None,
            )
            for addr, dev in SIM_DEVICES.items()
        ]

    @property
    def device(self) -> SimDevice:
        dev = SIM_DEVICES.get((self.address or "").upper())
        if dev is None:
            known = ", ".join(f"{a} ({d.name})" for a, d in SIM_DEVICES.items())
            raise InstrumentConnectionError(
                f"Simulated device {self.address} not found. Simulated devices: {known}."
            )
        return dev

    def connect(self, timeout_s: float) -> None:
        _ = self.device
        time.sleep(0.05)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        for stop in list(self._stops.values()):
            stop.set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def service_uuids(self) -> set[str]:
        return {uuid16(_PROFILE_CHARS[p][0]) for p in self.device.profiles}

    def characteristic_uuids(self) -> set[str]:
        return {uuid16(c) for p in self.device.profiles for c in _PROFILE_CHARS[p][1]}

    def _char(self, char_uuid: str) -> int:
        char = int(char_uuid[4:8], 16)
        if char_uuid.lower() not in self.characteristic_uuids():
            raise InstrumentProtocolError(f"Characteristic 0x{char:04X} not found on {self.address}")
        if not self._connected:
            raise InstrumentConnectionError(f"Simulated device {self.address} is not connected")
        return char

    # -- reads ------------------------------------------------------------------

    def read(self, char_uuid: str) -> bytes:
        char = self._char(char_uuid)
        dev = self.device
        strings = {
            0x2A29: "LabMCP Simulator",
            0x2A24: dev.model,
            0x2A25: f"SIM{int(self.address.replace(':', '')[-4:], 16):06d}",
            0x2A27: "rev A",
            0x2A26: "1.0.0-sim",
            0x2A28: "labmcp-sim",
        }
        if char in strings:
            return strings[char].encode()
        if char == 0x2A23:  # System ID: 40-bit manufacturer id + 24-bit OUI
            return bytes.fromhex("0102030405005e00")
        if char == 0x2A19:
            return bytes([86])
        if char == 0x2A38:
            return bytes([1])  # chest
        if char == 0x2A1D:
            return bytes([9])  # tympanum (ear drum)
        if char == 0x2A49:
            return struct.pack("<H", 0b0000_0111)  # body movement, cuff fit, irregular pulse
        if char == 0x2A9E:
            # time stamp, multiple users, BMI; weight res 0.05 kg (0b0100); height res 0.001 m (0b011)
            return struct.pack("<I", 0b111 | (0b0100 << 3) | (0b011 << 7))
        if char == 0x2A60:
            # measurement status + device/sensor status support, spot-check timestamp, PAI
            features = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 6)
            meas_support = (1 << 5) | (1 << 7) | (1 << 8) | (1 << 13)
            dev_support = (1 << 5) | (1 << 9) | (1 << 13) | (1 << 15)
            return struct.pack("<HH", features, meas_support) + dev_support.to_bytes(3, "little")
        raise InstrumentProtocolError(f"Characteristic 0x{char:04X} is not readable (notify/indicate only)")

    # -- notifications ----------------------------------------------------------

    def subscribe(self, char_uuid: str, callback: Callable[[bytes], None]) -> None:
        char = self._char(char_uuid)
        with self._lock:
            self._callbacks[char] = callback
        gens: dict[int, Callable[[], Iterator[Emission]]] = {
            0x2A37: self._heart_rate,
            0x2A5F: self._plx_continuous,
            0x2A5E: self._plx_spot_check,
            0x2A35: self._blood_pressure,
            0x2A1C: self._temperature,
            0x2A9D: self._weight,
        }
        if char in gens:
            stop = threading.Event()
            self._stops[char] = stop
            threading.Thread(target=self._emit, args=(gens[char](), stop), daemon=True, name=f"sim-{char:04X}").start()

    def unsubscribe(self, char_uuid: str) -> None:
        char = int(char_uuid[4:8], 16)
        with self._lock:
            self._callbacks.pop(char, None)
        stop = self._stops.pop(char, None)
        if stop:
            stop.set()

    def _emit(self, gen: Iterator[Emission], stop: threading.Event) -> None:
        for delay, char, payload in gen:
            if stop.wait(delay) or not self._connected:
                return
            with self._lock:
                cb = self._callbacks.get(char)
            if cb is not None:
                cb(payload)

    def close(self) -> None:
        self.disconnect()

    # -- device models ----------------------------------------------------------

    def _rr_generator(self) -> Iterator[float]:
        """Resting sinus rhythm: 64 bpm with respiratory (0.25 Hz) and baroreflex (0.1 Hz)
        modulation plus beat-to-beat noise."""
        base = 60.0 / 64.0
        while True:
            t = self._t_hr
            rr = base * (1 + 0.045 * math.sin(2 * math.pi * 0.25 * t) + 0.02 * math.sin(2 * math.pi * 0.1 * t))
            rr += self.rng.gauss(0, 0.012)
            self._t_hr += rr
            yield rr

    def _heart_rate(self) -> Iterator[Emission]:
        """Heart Rate Measurement (0x2A37) once per second: uint8 bpm, sensor contact
        supported and detected, RR intervals in 1/1024 s, energy expended every 10th packet."""
        beats = self._rr_generator()
        clock = 0.0
        next_beat = next(beats)
        count = 0
        last_bpm = 64
        while True:
            clock += 1.0
            rrs: list[float] = []
            while next_beat <= clock:
                rr = next(beats)
                rrs.append(rr)
                next_beat += rr
            count += 1
            self._energy_kj += 5.5 / 60.0  # ~5.5 kJ/min at rest
            flags = 0x02 | 0x04  # contact detected, contact supported (uint8 format)
            if rrs:
                last_bpm = round(60.0 / (sum(rrs) / len(rrs)))
                flags |= 0x10
            payload = bytes([flags, last_bpm])
            if count % 10 == 0:
                flags |= 0x08
                payload = bytes([flags, last_bpm]) + struct.pack("<H", int(self._energy_kj))
            payload += b"".join(struct.pack("<H", round(rr * 1024)) for rr in rrs)
            yield 1.0, 0x2A37, payload

    def _plx_continuous(self) -> Iterator[Emission]:
        """PLX Continuous Measurement (0x2A5F) once per second. The first packet reports
        SpO2/PR as NaN while the sensor is still acquiring (as real oximeters do)."""
        flags = 0x04 | 0x08 | 0x10  # measurement status, device & sensor status, PAI
        first = True
        while True:
            if first:
                body = enc_sfloat(None) + enc_sfloat(None)
                status = (1 << 5) | (1 << 13)  # measurement ongoing, measurement unavailable
                device = 1 << 9  # signal analysis ongoing
                pai = enc_sfloat(None)
                first = False
            else:
                body = enc_sfloat(round(97 + self.rng.gauss(0, 0.6))) + enc_sfloat(round(64 + self.rng.gauss(0, 2)))
                status = 1 << 7  # validated data
                device = 0
                pai = enc_sfloat(round(3.8 + self.rng.gauss(0, 0.3), 1), -1)
            yield 1.0, 0x2A5F, bytes([flags]) + body + struct.pack("<H", status) + device.to_bytes(3, "little") + pai

    def _plx_spot_check(self) -> Iterator[Emission]:
        """PLX Spot-check Measurement (0x2A5E): one indication once the reading is stable."""
        flags = 0x01 | 0x02  # timestamp, measurement status
        payload = (
            bytes([flags])
            + enc_sfloat(97)
            + enc_sfloat(63)
            + enc_date_time(datetime.now())
            + struct.pack("<H", 1 << 8)  # fully qualified data
        )
        yield 3.0, 0x2A5E, payload

    def _bp_packet(self, sys_: float, dia: float, pulse: float, when: datetime, user: int = 1) -> bytes:
        mean = round(dia + (sys_ - dia) / 3.0)
        flags = 0x02 | 0x04 | 0x08 | 0x10  # mmHg, time stamp, pulse rate, user id, status
        return (
            bytes([flags])
            + enc_sfloat(sys_)
            + enc_sfloat(dia)
            + enc_sfloat(mean)
            + enc_date_time(when)
            + enc_sfloat(pulse)
            + bytes([user])
            + struct.pack("<H", 0)
        )

    def _blood_pressure(self) -> Iterator[Emission]:
        """Cuff inflates to ~165 mmHg and deflates (Intermediate Cuff Pressure 0x2A36 every
        0.25 s), then the monitor indicates its unsent stored measurement from yesterday
        followed by the new one (Blood Pressure Measurement 0x2A35), oldest first."""
        nan = enc_sfloat(None)
        profile = [20 + 36 * i for i in range(5)] + [165 - 12 * i for i in range(8)]
        for p in profile:
            yield 0.25, 0x2A36, bytes([0x00]) + enc_sfloat(p) + nan + nan
        yield 0.3, 0x2A35, self._bp_packet(126, 82, 71, datetime.now() - timedelta(days=1, minutes=13))
        yield 0.1, 0x2A35, self._bp_packet(121, 79, 66, datetime.now())

    def _temperature(self) -> Iterator[Emission]:
        """Intermediate Temperature (0x2A1E) while the probe settles, then one Temperature
        Measurement (0x2A1C) indication: 36.8 C (FLOAT, exponent -1), time stamp, type."""
        for value in (36.2, 36.5, 36.7):
            yield 0.5, 0x2A1E, bytes([0x00]) + enc_float(value, -1)
        yield 0.5, 0x2A1C, bytes([0x02 | 0x04]) + enc_float(36.8, -1) + enc_date_time(datetime.now()) + bytes([9])

    def _weight(self) -> Iterator[Emission]:
        """Weight Measurement (0x2A9D): 72.35 kg (uint16 in 0.005 kg), time stamp, user 1,
        BMI 22.8 kg/m2 and height 1.782 m."""
        flags = 0x02 | 0x04 | 0x08
        payload = (
            bytes([flags])
            + struct.pack("<H", round(72.35 / 0.005))
            + enc_date_time(datetime.now())
            + bytes([1])
            + struct.pack("<HH", 228, 1782)
        )
        yield 2.0, 0x2A9D, payload
