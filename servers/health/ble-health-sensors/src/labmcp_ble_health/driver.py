"""Driver for Bluetooth Low Energy health sensors that implement the standard Bluetooth SIG
GATT health profiles. Research / lab-operations use only; not a medical device.

Byte layouts are implemented from the Bluetooth SIG documents (all fields little-endian):

* GATT Specification Supplement (GSS), version 2026-09-09, sections 3.30 Battery Level,
  3.34 Blood Pressure Measurement, 3.38 Body Sensor Location, 3.80 Date Time,
  3.126 Heart Rate Measurement, 3.239 Temperature Measurement, 3.242 Temperature Type,
  3.276 Weight Measurement, 3.277 Weight Scale Feature, and 2.1.1 (special medfloat values).
  https://btprodspecificationrefs.blob.core.windows.net/gatt-specification-supplement/GATT_Specification_Supplement.pdf
* Heart Rate Service 1.0 (HRS, V10r00): energy expended in kJ, RR-interval in 1/1024 s.
  https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html
* Pulse Oximeter Service 1.0.1 (PLXS): PLX Spot-check / Continuous Measurement and PLX
  Features layouts (these characteristics are defined in PLXS itself, not in the GSS).
  https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXS_v1.0.1/out/en/index-en.html
* Blood Pressure Service 1.1.1 (BLS), Health Thermometer Service 1.0 (HTS), Weight Scale
  Service 1.0.1 (WSS: weight 0xFFFF = "measurement unsuccessful"), Battery Service 1.1,
  Device Information Service 1.2 - same URL pattern (BLS_v1.1.1, HTS_v1.0, WSS_v1.0.1, ...).
* Personal Health Devices Transcoding White Paper v16 (Bluetooth SIG), section 2.2:
  IEEE 11073-20601 FLOAT (8-bit exponent, 24-bit mantissa) and SFLOAT (4-bit exponent,
  12-bit mantissa), both two's complement, value = mantissa * 10**exponent.
  https://www.bluetooth.com/wp-content/uploads/2019/03/PHD_Transcoding_WP_v16.pdf
* 16-bit UUIDs: Bluetooth SIG Assigned Numbers (2026-08-05).

The GATT link itself is provided by a *backend*: :class:`BleakBackend` (the ``bleak``
library, imported lazily) for real hardware, or the simulator in ``simulator.py``. Both
expose the same small interface (scan / connect / read / subscribe), so ``--simulate``
exercises exactly the parsers below.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import math
import queue
import re
import statistics
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from labmcp import InstrumentConnectionError, InstrumentError, InstrumentProtocolError, InstrumentTimeout

if TYPE_CHECKING:
    from labmcp import AuditLog

# --------------------------------------------------------------------------- UUIDs


def uuid16(value: int) -> str:
    """Full 128-bit UUID string for a 16-bit Bluetooth SIG UUID (as bleak reports them)."""
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


# Services (Assigned Numbers, section 3.4)
SVC_HEALTH_THERMOMETER = 0x1809
SVC_DEVICE_INFORMATION = 0x180A
SVC_HEART_RATE = 0x180D
SVC_BATTERY = 0x180F
SVC_BLOOD_PRESSURE = 0x1810
SVC_WEIGHT_SCALE = 0x181D
SVC_PULSE_OXIMETER = 0x1822

SERVICE_NAMES = {
    SVC_HEALTH_THERMOMETER: "health_thermometer",
    SVC_DEVICE_INFORMATION: "device_information",
    SVC_HEART_RATE: "heart_rate",
    SVC_BATTERY: "battery",
    SVC_BLOOD_PRESSURE: "blood_pressure",
    SVC_WEIGHT_SCALE: "weight_scale",
    SVC_PULSE_OXIMETER: "pulse_oximeter",
}
SERVICE_BY_NAME = {name: code for code, name in SERVICE_NAMES.items()}

# Characteristics (Assigned Numbers, section 3.8)
CHR_BATTERY_LEVEL = 0x2A19
CHR_TEMPERATURE_MEASUREMENT = 0x2A1C
CHR_TEMPERATURE_TYPE = 0x2A1D
CHR_INTERMEDIATE_TEMPERATURE = 0x2A1E
CHR_SYSTEM_ID = 0x2A23
CHR_MODEL_NUMBER = 0x2A24
CHR_SERIAL_NUMBER = 0x2A25
CHR_FIRMWARE_REVISION = 0x2A26
CHR_HARDWARE_REVISION = 0x2A27
CHR_SOFTWARE_REVISION = 0x2A28
CHR_MANUFACTURER_NAME = 0x2A29
CHR_BLOOD_PRESSURE_MEASUREMENT = 0x2A35
CHR_INTERMEDIATE_CUFF_PRESSURE = 0x2A36
CHR_HEART_RATE_MEASUREMENT = 0x2A37
CHR_BODY_SENSOR_LOCATION = 0x2A38
CHR_BLOOD_PRESSURE_FEATURE = 0x2A49
CHR_PLX_SPOT_CHECK = 0x2A5E
CHR_PLX_CONTINUOUS = 0x2A5F
CHR_PLX_FEATURES = 0x2A60
CHR_WEIGHT_MEASUREMENT = 0x2A9D
CHR_WEIGHT_SCALE_FEATURE = 0x2A9E

DIS_STRINGS = {
    "manufacturer": CHR_MANUFACTURER_NAME,
    "model": CHR_MODEL_NUMBER,
    "serial": CHR_SERIAL_NUMBER,
    "hardware_revision": CHR_HARDWARE_REVISION,
    "firmware_revision": CHR_FIRMWARE_REVISION,
    "software_revision": CHR_SOFTWARE_REVISION,
}

# BLE MAC address (Linux/Windows) or CoreBluetooth peripheral UUID (macOS).
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def normalize_address(address: str) -> str:
    """Validate a BLE address (``AA:BB:CC:DD:EE:FF`` or a macOS UUID; ``ble://`` prefix allowed)."""
    addr = address.strip()
    if addr.lower().startswith("ble://"):
        addr = addr[6:]
    if _MAC_RE.match(addr):
        return addr.upper()
    if _UUID_RE.match(addr):
        return addr.upper()
    raise InstrumentConnectionError(
        f"{address!r} is not a Bluetooth LE address. Use the device's MAC address "
        "(e.g. AA:BB:CC:DD:EE:FF on Linux/Windows) or its CoreBluetooth UUID on macOS "
        "(e.g. 1A2B3C4D-...). The `scan_devices` tool lists nearby devices and their addresses."
    )


# --------------------------------------------------------------------------- IEEE 11073 floats

_SFLOAT_SPECIAL = {0x07FF: "NaN", 0x0800: "NRes", 0x07FE: "+INF", 0x0802: "-INF", 0x0801: "reserved"}
_FLOAT_SPECIAL = {
    0x007FFFFF: "NaN",
    0x00800000: "NRes",
    0x007FFFFE: "+INF",
    0x00800002: "-INF",
    0x00800001: "reserved",
}
SPECIAL_MEANING = {
    "NaN": "not a number: no valid value (e.g. sensor could not measure)",
    "NRes": "not at this resolution: value out of the representable range",
    "+INF": "positive infinity",
    "-INF": "negative infinity",
    "reserved": "reserved special value",
}


@dataclass
class MedFloat:
    """A decoded IEEE 11073-20601 FLOAT/SFLOAT. ``value`` is ``None`` for special values."""

    value: float | None
    special: str | None = None


def _scale(mantissa: int, exponent: int) -> float:
    # Decimal keeps e.g. 364 * 10**-1 exactly 36.4 instead of 36.400000000000006.
    return float(Decimal(mantissa).scaleb(exponent))


def decode_sfloat(raw: int) -> MedFloat:
    """Decode a 16-bit SFLOAT (medfloat16): 4-bit exponent, 12-bit mantissa, both signed."""
    raw &= 0xFFFF
    if raw in _SFLOAT_SPECIAL:
        return MedFloat(None, _SFLOAT_SPECIAL[raw])
    mantissa = raw & 0x0FFF
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    exponent = raw >> 12
    if exponent >= 0x8:
        exponent -= 0x10
    return MedFloat(_scale(mantissa, exponent))


def decode_float32(raw: int) -> MedFloat:
    """Decode a 32-bit FLOAT (medfloat32): 8-bit exponent, 24-bit mantissa, both signed."""
    raw &= 0xFFFFFFFF
    if raw in _FLOAT_SPECIAL:
        return MedFloat(None, _FLOAT_SPECIAL[raw])
    mantissa = raw & 0x00FFFFFF
    if mantissa >= 0x00800000:
        mantissa -= 0x01000000
    exponent = raw >> 24
    if exponent >= 0x80:
        exponent -= 0x100
    return MedFloat(_scale(mantissa, exponent))


# --------------------------------------------------------------------------- byte reader


class _Reader:
    """Little-endian field reader that raises a helpful error on short packets."""

    def __init__(self, data: bytes, what: str) -> None:
        self.data = bytes(data)
        self.what = what
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def _take(self, n: int, field_name: str) -> bytes:
        if self.remaining < n:
            raise InstrumentProtocolError(
                f"Malformed {self.what}: packet {self.data.hex(' ')} ({len(self.data)} bytes) ends "
                f"before the {field_name} field at offset {self.pos} (needs {n} more bytes)."
            )
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self, name: str) -> int:
        return self._take(1, name)[0]

    def u16(self, name: str) -> int:
        return struct.unpack("<H", self._take(2, name))[0]

    def u24(self, name: str) -> int:
        return int.from_bytes(self._take(3, name), "little")

    def u32(self, name: str) -> int:
        return struct.unpack("<I", self._take(4, name))[0]

    def sfloat(self, name: str) -> MedFloat:
        return decode_sfloat(self.u16(name))

    def float32(self, name: str) -> MedFloat:
        return decode_float32(self.u32(name))

    def date_time(self, name: str) -> str | None:
        return parse_date_time(self._take(7, name))


def parse_date_time(data: bytes) -> str | None:
    """GSS 3.80 Date Time (7 octets) -> ISO 8601 device-local time, or None if unknown/invalid."""
    if len(data) != 7:
        raise InstrumentProtocolError(f"Date Time must be 7 bytes, got {data.hex(' ')}")
    year, month, day, hours, minutes, seconds = struct.unpack("<HBBBBB", data)
    if not year or not month or not day:
        return None  # 0 means "not known" (GSS 3.80)
    try:
        return datetime(year, month, day, hours, minutes, seconds).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- measurements


@dataclass
class HeartRateMeasurement:
    bpm: int
    sensor_contact: bool | None  # None when the sensor does not support contact detection
    energy_expended_kj: int | None
    rr_intervals_s: list[float]


BODY_SENSOR_LOCATIONS = {0: "other", 1: "chest", 2: "wrist", 3: "finger", 4: "hand", 5: "ear_lobe", 6: "foot"}


def parse_heart_rate_measurement(data: bytes) -> HeartRateMeasurement:
    """GSS 3.126 / HRS 3.1 Heart Rate Measurement (0x2A37)."""
    r = _Reader(data, "Heart Rate Measurement (0x2A37)")
    flags = r.u8("Flags")
    bpm = r.u16("Heart Rate (uint16)") if flags & 0x01 else r.u8("Heart Rate (uint8)")
    contact_supported = bool(flags & 0x04)
    contact = bool(flags & 0x02) if contact_supported else None
    energy = r.u16("Energy Expended") if flags & 0x08 else None
    rr: list[float] = []
    if flags & 0x10:
        while r.remaining >= 2:
            rr.append(r.u16("RR-Interval") / 1024.0)
    return HeartRateMeasurement(bpm=bpm, sensor_contact=contact, energy_expended_kj=energy, rr_intervals_s=rr)


def parse_body_sensor_location(data: bytes) -> str:
    code = _Reader(data, "Body Sensor Location (0x2A38)").u8("Body Sensor Location")
    return BODY_SENSOR_LOCATIONS.get(code, f"reserved ({code})")


def parse_battery_level(data: bytes) -> int:
    level = _Reader(data, "Battery Level (0x2A19)").u8("Battery Level")
    if level > 100:
        raise InstrumentProtocolError(f"Battery Level {level} is outside 0-100 % (reserved value).")
    return level


# PLXS table 3.4 (bits 0-4 RFU) and table 3.5.
PLX_MEASUREMENT_STATUS = {
    5: "measurement_ongoing",
    6: "early_estimated_data",
    7: "validated_data",
    8: "fully_qualified_data",
    9: "data_from_measurement_storage",
    10: "data_for_demonstration",
    11: "data_for_testing",
    12: "calibration_ongoing",
    13: "measurement_unavailable",
    14: "questionable_measurement_detected",
    15: "invalid_measurement_detected",
}
PLX_DEVICE_SENSOR_STATUS = {
    0: "extended_display_update_ongoing",
    1: "equipment_malfunction_detected",
    2: "signal_processing_irregularity_detected",
    3: "inadequate_signal_detected",
    4: "poor_signal_detected",
    5: "low_perfusion_detected",
    6: "erratic_signal_detected",
    7: "non_pulsatile_signal_detected",
    8: "questionable_pulse_detected",
    9: "signal_analysis_ongoing",
    10: "sensor_interference_detected",
    11: "sensor_unconnected_to_user",
    12: "unknown_sensor_connected",
    13: "sensor_displaced",
    14: "sensor_malfunctioning",
    15: "sensor_disconnected",
}
PLX_FEATURES = {
    0: "measurement_status_support",
    1: "device_and_sensor_status_support",
    2: "spot_check_measurement_storage",
    3: "spot_check_timestamp",
    4: "spo2pr_fast",
    5: "spo2pr_slow",
    6: "pulse_amplitude_index",
    7: "multiple_bonds",
}


def _bits(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value & (1 << bit)]


@dataclass
class PulseOximetryMeasurement:
    kind: str  # "spot_check" or "continuous"
    spo2_pct: MedFloat
    pulse_rate_bpm: MedFloat
    timestamp: str | None = None
    device_clock_not_set: bool = False
    spo2_fast_pct: MedFloat | None = None
    pulse_rate_fast_bpm: MedFloat | None = None
    spo2_slow_pct: MedFloat | None = None
    pulse_rate_slow_bpm: MedFloat | None = None
    measurement_status: list[str] = field(default_factory=list)
    device_sensor_status: list[str] = field(default_factory=list)
    pulse_amplitude_index_pct: MedFloat | None = None


def parse_plx_spot_check(data: bytes) -> PulseOximetryMeasurement:
    """PLXS 3.1 PLX Spot-check Measurement (0x2A5E)."""
    r = _Reader(data, "PLX Spot-check Measurement (0x2A5E)")
    flags = r.u8("Flags")
    m = PulseOximetryMeasurement("spot_check", r.sfloat("SpO2"), r.sfloat("PR"))
    if flags & 0x01:
        m.timestamp = r.date_time("Timestamp")
    if flags & 0x02:
        m.measurement_status = _bits(r.u16("Measurement Status"), PLX_MEASUREMENT_STATUS)
    if flags & 0x04:
        m.device_sensor_status = _bits(r.u24("Device and Sensor Status"), PLX_DEVICE_SENSOR_STATUS)
    if flags & 0x08:
        m.pulse_amplitude_index_pct = r.sfloat("Pulse Amplitude Index")
    m.device_clock_not_set = bool(flags & 0x10)
    return m


def parse_plx_continuous(data: bytes) -> PulseOximetryMeasurement:
    """PLXS 3.2 PLX Continuous Measurement (0x2A5F)."""
    r = _Reader(data, "PLX Continuous Measurement (0x2A5F)")
    flags = r.u8("Flags")
    m = PulseOximetryMeasurement("continuous", r.sfloat("SpO2PR-Normal SpO2"), r.sfloat("SpO2PR-Normal PR"))
    if flags & 0x01:
        m.spo2_fast_pct, m.pulse_rate_fast_bpm = r.sfloat("SpO2PR-Fast SpO2"), r.sfloat("SpO2PR-Fast PR")
    if flags & 0x02:
        m.spo2_slow_pct, m.pulse_rate_slow_bpm = r.sfloat("SpO2PR-Slow SpO2"), r.sfloat("SpO2PR-Slow PR")
    if flags & 0x04:
        m.measurement_status = _bits(r.u16("Measurement Status"), PLX_MEASUREMENT_STATUS)
    if flags & 0x08:
        m.device_sensor_status = _bits(r.u24("Device and Sensor Status"), PLX_DEVICE_SENSOR_STATUS)
    if flags & 0x10:
        m.pulse_amplitude_index_pct = r.sfloat("Pulse Amplitude Index")
    return m


def parse_plx_features(data: bytes) -> dict[str, Any]:
    """PLXS 3.3 PLX Features (0x2A60)."""
    r = _Reader(data, "PLX Features (0x2A60)")
    features = r.u16("Supported Features")
    out: dict[str, Any] = {"supported_features": _bits(features, PLX_FEATURES)}
    if features & 0x01:
        out["measurement_status_supported"] = _bits(r.u16("Measurement Status Support"), PLX_MEASUREMENT_STATUS)
    if features & 0x02:
        out["device_sensor_status_supported"] = _bits(
            r.u24("Device and Sensor Status Support"), PLX_DEVICE_SENSOR_STATUS
        )
    return out


MMHG_PER_KPA = 7.500617  # 1 kPa = 7.500617 mmHg (1 mmHg = 133.322 Pa)


@dataclass
class BloodPressureMeasurement:
    unit: str  # "mmHg" or "kPa", as reported by the device
    systolic: MedFloat
    diastolic: MedFloat
    mean_arterial: MedFloat
    timestamp: str | None
    pulse_rate_bpm: MedFloat | None
    user_id: int | None
    status: list[str]


def _bp_status(value: int) -> list[str]:
    out = _bits(value, {0: "body_movement_detected", 1: "cuff_too_loose", 2: "irregular_pulse_detected"})
    rng = (value >> 3) & 0b11
    if rng == 0b01:
        out.append("pulse_rate_exceeds_upper_limit")
    elif rng == 0b10:
        out.append("pulse_rate_below_lower_limit")
    elif rng == 0b11:
        out.append("pulse_rate_range_reserved_value")
    if value & (1 << 5):
        out.append("improper_measurement_position")
    return out


def parse_blood_pressure_measurement(data: bytes, what: str = "Blood Pressure Measurement (0x2A35)") -> BloodPressureMeasurement:
    """GSS 3.34 Blood Pressure Measurement (0x2A35). Intermediate Cuff Pressure (0x2A36) shares
    the layout; there the first value is the current cuff pressure and the others are NaN."""
    r = _Reader(data, what)
    flags = r.u8("Flags")
    unit = "kPa" if flags & 0x01 else "mmHg"
    sys_, dia, mean = r.sfloat("Systolic"), r.sfloat("Diastolic"), r.sfloat("Mean Arterial Pressure")
    ts = r.date_time("Time Stamp") if flags & 0x02 else None
    pulse = r.sfloat("Pulse Rate") if flags & 0x04 else None
    user = r.u8("User ID") if flags & 0x08 else None
    status = _bp_status(r.u16("Measurement Status")) if flags & 0x10 else []
    return BloodPressureMeasurement(unit, sys_, dia, mean, ts, pulse, user, status)


def to_mmhg(value: MedFloat, unit: str) -> float | None:
    if value.value is None:
        return None
    return round(value.value * MMHG_PER_KPA, 1) if unit == "kPa" else value.value


TEMPERATURE_TYPES = {
    1: "armpit",
    2: "body_general",
    3: "ear_earlobe",
    4: "finger",
    5: "gastrointestinal_tract",
    6: "mouth",
    7: "rectum",
    8: "toe",
    9: "tympanum_ear_drum",
}


@dataclass
class TemperatureMeasurement:
    value: MedFloat
    unit: str  # "C" or "F", as reported by the device
    timestamp: str | None
    temperature_type: str | None


def parse_temperature_type(code: int) -> str:
    return TEMPERATURE_TYPES.get(code, f"reserved ({code})")


def parse_temperature_measurement(data: bytes, what: str = "Temperature Measurement (0x2A1C)") -> TemperatureMeasurement:
    """GSS 3.239 Temperature Measurement (0x2A1C); Intermediate Temperature (0x2A1E) is identical."""
    r = _Reader(data, what)
    flags = r.u8("Flags")
    value = r.float32("Temperature Measurement Value")
    unit = "F" if flags & 0x01 else "C"
    ts = r.date_time("Time Stamp") if flags & 0x02 else None
    ttype = parse_temperature_type(r.u8("Temperature Type")) if flags & 0x04 else None
    return TemperatureMeasurement(value, unit, ts, ttype)


def to_celsius(value: MedFloat, unit: str) -> float | None:
    if value.value is None:
        return None
    return round((value.value - 32.0) * 5.0 / 9.0, 3) if unit == "F" else value.value


KG_PER_LB = 0.45359237
M_PER_INCH = 0.0254


@dataclass
class WeightMeasurement:
    unit: str  # "kg" or "lb", as reported by the device
    weight: float | None  # None = measurement unsuccessful (raw 0xFFFF)
    timestamp: str | None
    user_id: int | None
    bmi_kg_m2: float | None
    height: float | None  # metres (SI) or inches (imperial), as reported
    height_unit: str


def parse_weight_measurement(data: bytes) -> WeightMeasurement:
    """GSS 3.276 Weight Measurement (0x2A9D). Resolution 0.005 kg or 0.01 lb; 0xFFFF means
    'measurement unsuccessful' (WSS 1.0.1 section 3.2.1.2)."""
    r = _Reader(data, "Weight Measurement (0x2A9D)")
    flags = r.u8("Flags")
    imperial = bool(flags & 0x01)
    raw = r.u16("Weight")
    weight = None if raw == 0xFFFF else round(raw * (0.01 if imperial else 0.005), 3)
    ts = r.date_time("Time Stamp") if flags & 0x02 else None
    user = r.u8("User ID") if flags & 0x04 else None
    bmi = height = None
    if flags & 0x08:
        bmi = round(r.u16("BMI") * 0.1, 1)
        h = r.u16("Height")
        height = round(h * (0.1 if imperial else 0.001), 3)
    return WeightMeasurement(
        "lb" if imperial else "kg", weight, ts, user, bmi, height, "in" if imperial else "m"
    )


_WEIGHT_RES = {1: (0.5, 1.0), 2: (0.2, 0.5), 3: (0.1, 0.2), 4: (0.05, 0.1), 5: (0.02, 0.05), 6: (0.01, 0.02), 7: (0.005, 0.01)}
_HEIGHT_RES = {1: (0.01, 1.0), 2: (0.005, 0.5), 3: (0.001, 0.1)}


def parse_weight_scale_feature(data: bytes) -> dict[str, Any]:
    """GSS 3.277 Weight Scale Feature (0x2A9E)."""
    value = _Reader(data, "Weight Scale Feature (0x2A9E)").u32("Weight Scale Feature")
    wres = _WEIGHT_RES.get((value >> 3) & 0xF)
    hres = _HEIGHT_RES.get((value >> 7) & 0x7)
    return {
        "timestamp_supported": bool(value & 0x1),
        "multiple_users_supported": bool(value & 0x2),
        "bmi_supported": bool(value & 0x4),
        "weight_resolution_kg": wres[0] if wres else None,
        "weight_resolution_lb": wres[1] if wres else None,
        "height_resolution_m": hres[0] if hres else None,
        "height_resolution_in": hres[1] if hres else None,
    }


def parse_blood_pressure_feature(data: bytes) -> list[str]:
    """GSS 3.33 Blood Pressure Feature (0x2A49)."""
    value = _Reader(data, "Blood Pressure Feature (0x2A49)").u16("Blood Pressure Feature")
    return _bits(
        value,
        {
            0: "body_movement_detection",
            1: "cuff_fit_detection",
            2: "irregular_pulse_detection",
            3: "pulse_rate_range_detection",
            4: "measurement_position_detection",
            5: "multiple_bonds",
            6: "e2e_crc",
            7: "user_data_service",
            8: "user_facing_time",
        },
    )


def decode_string(data: bytes) -> str:
    """Device Information strings are UTF-8 (utf8s); some devices pad with NULs."""
    return bytes(data).split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------- HRV


def hrv_statistics(rr_s: list[float], min_rr_s: float = 0.3, max_rr_s: float = 2.0) -> dict[str, Any]:
    """Time-domain HRV from RR intervals (seconds).

    Intervals outside ``[min_rr_s, max_rr_s]`` (200-30 bpm) are treated as artefacts and
    excluded. SDNN is the sample standard deviation of the RR intervals; RMSSD the root mean
    square of successive differences; pNN50 the percentage of successive differences > 50 ms.
    """
    valid = [r for r in rr_s if min_rr_s <= r <= max_rr_s]
    out: dict[str, Any] = {
        "rr_count": len(rr_s),
        "rr_excluded": len(rr_s) - len(valid),
        "mean_rr_ms": None,
        "mean_hr_bpm": None,
        "sdnn_ms": None,
        "rmssd_ms": None,
        "pnn50_pct": None,
    }
    if not valid:
        return out
    rr_ms = [r * 1000.0 for r in valid]
    mean_rr = statistics.fmean(rr_ms)
    out["mean_rr_ms"] = round(mean_rr, 1)
    out["mean_hr_bpm"] = round(60000.0 / mean_rr, 1)
    if len(rr_ms) >= 2:
        out["sdnn_ms"] = round(statistics.stdev(rr_ms), 1)
        diffs = [b - a for a, b in zip(rr_ms, rr_ms[1:], strict=False)]
        out["rmssd_ms"] = round(math.sqrt(statistics.fmean(d * d for d in diffs)), 1)
        out["pnn50_pct"] = round(100.0 * sum(abs(d) > 50.0 for d in diffs) / len(diffs), 1)
    return out


# --------------------------------------------------------------------------- backends


@dataclass
class ScanResult:
    address: str
    name: str | None
    rssi_dbm: int | None
    service_uuids: list[str]
    tx_power_dbm: int | None = None


class BLEBackend(Protocol):
    """What the driver needs from a GATT client (real: bleak; simulated: simulator.py)."""

    def scan(self, timeout_s: float) -> list[ScanResult]: ...
    def connect(self, timeout_s: float) -> None: ...
    def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    def service_uuids(self) -> set[str]: ...
    def characteristic_uuids(self) -> set[str]: ...
    def read(self, char_uuid: str) -> bytes: ...
    def subscribe(self, char_uuid: str, callback: Callable[[bytes], None]) -> None: ...
    def unsubscribe(self, char_uuid: str) -> None: ...
    def close(self) -> None: ...


class BleakBackend:
    """Real GATT client on top of ``bleak``. Bleak is asyncio-based, so it runs on its own event
    loop in a background thread; the synchronous methods below submit coroutines to it."""

    def __init__(self, address: str | None, *, adapter: str | None = None, pair: bool = False) -> None:
        try:
            import bleak  # lazy: only needed for real hardware
        except ImportError as exc:  # pragma: no cover - bleak is a declared dependency
            raise InstrumentConnectionError(
                "The `bleak` package is required for Bluetooth LE: pip install bleak"
            ) from exc
        self._bleak = bleak
        self.address = address
        self._adapter = adapter
        self._pair = pair
        self._client: Any = None
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="labmcp-bleak", daemon=True)
        self._thread.start()

    # -- plumbing ---------------------------------------------------------------

    def _call(self, coro: Any, timeout: float, what: str) -> Any:
        if self._closed:
            coro.close()
            raise InstrumentConnectionError(
                "The Bluetooth connection was closed (reconnect or server shutdown). Call the tool again."
            )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as exc:  # the event loop was closed meanwhile
            coro.close()
            raise InstrumentConnectionError(f"The Bluetooth event loop is closed ({exc}). Call the tool again.") from exc
        try:
            try:
                return fut.result(timeout)
            except concurrent.futures.TimeoutError:
                if fut.cancel():  # still running: the coroutine is cancelled (and cleans up after itself)
                    raise
                # It finished right at the deadline: use its outcome instead of dropping it (a
                # dropped connected client would stay connected, and the device stops advertising).
                return fut.result(0)
        except (concurrent.futures.TimeoutError, asyncio.TimeoutError, TimeoutError) as exc:
            raise InstrumentTimeout(
                f"Bluetooth operation '{what}' timed out after {timeout:g} s. Is the device on, "
                "in range and not connected to another app (phone, watch)?"
            ) from exc
        except InstrumentError:
            raise
        except Exception as exc:
            raise _translate_bleak_error(exc, what, self.address) from exc

    def _adapter_kwargs(self, args_class: str) -> dict[str, Any]:
        """Select the Linux adapter. Newer bleak takes it in the ``bluez`` args dict
        (``BlueZScannerArgs`` / ``BlueZClientArgs`` gained ``adapter``, which deprecated the
        ``adapter`` keyword); bleak 1.0.x only understands ``adapter=`` and silently ignores an
        unknown key in the ``bluez`` dict, so check which form this bleak version supports."""
        if not self._adapter:
            return {}
        try:
            import bleak.args.bluez as bluez_args

            supported = "adapter" in getattr(bluez_args, args_class).__annotations__
        except (ImportError, AttributeError):
            supported = False
        return {"bluez": {"adapter": self._adapter}} if supported else {"adapter": self._adapter}

    def _scanner_kwargs(self) -> dict[str, Any]:
        return self._adapter_kwargs("BlueZScannerArgs")

    def _client_kwargs(self) -> dict[str, Any]:
        return {"pair": self._pair, **self._adapter_kwargs("BlueZClientArgs")}

    # -- interface --------------------------------------------------------------

    def scan(self, timeout_s: float) -> list[ScanResult]:
        async def run() -> list[ScanResult]:
            found = await self._bleak.BleakScanner.discover(
                timeout=timeout_s, return_adv=True, **self._scanner_kwargs()
            )
            return [
                ScanResult(
                    address=dev.address,
                    name=adv.local_name or dev.name,
                    rssi_dbm=adv.rssi,
                    service_uuids=[u.lower() for u in adv.service_uuids],
                    tx_power_dbm=adv.tx_power,
                )
                for dev, adv in found.values()
            ]

        return self._call(run(), timeout_s + 10.0, "scan")

    async def _connect_coro(self, timeout_s: float) -> Any:
        device = await self._bleak.BleakScanner.find_device_by_address(
            self.address, timeout=timeout_s, **self._scanner_kwargs()
        )
        if device is None:
            raise InstrumentConnectionError(
                f"Bluetooth device {self.address} was not found within {timeout_s:g} s. Make sure it "
                "is switched on and advertising (many monitors only advertise right after a "
                "measurement or when their Bluetooth button is pressed), is in range, and is not "
                "connected to a phone app. Use `scan_devices` to see what is nearby."
            )
        client = self._bleak.BleakClient(device, timeout=timeout_s, **self._client_kwargs())
        try:
            await client.connect()
        except BaseException:  # failed, timed out or cancelled: never leave a half-open link behind
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return client

    def connect(self, timeout_s: float) -> None:
        if self.address is None:
            raise InstrumentConnectionError("No device address configured (start the server with --address).")
        self.disconnect()  # release a stale client (e.g. after the device dropped the link)
        self._client = self._call(self._connect_coro(timeout_s), timeout_s * 2 + 10.0, "connect")

    def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(InstrumentError):
                self._call(client.disconnect(), 10.0, "disconnect")

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    def service_uuids(self) -> set[str]:
        return {s.uuid.lower() for s in self._client.services}

    def characteristic_uuids(self) -> set[str]:
        return {c.uuid.lower() for s in self._client.services for c in s.characteristics}

    def read(self, char_uuid: str) -> bytes:
        return bytes(self._call(self._client.read_gatt_char(char_uuid), 15.0, f"read {char_uuid[4:8]}"))

    def subscribe(self, char_uuid: str, callback: Callable[[bytes], None]) -> None:
        def handler(_sender: Any, data: bytearray) -> None:
            callback(bytes(data))

        self._call(self._client.start_notify(char_uuid, handler), 15.0, f"subscribe {char_uuid[4:8]}")

    def unsubscribe(self, char_uuid: str) -> None:
        if self.is_connected:
            self._call(self._client.stop_notify(char_uuid), 10.0, f"unsubscribe {char_uuid[4:8]}")

    def close(self) -> None:
        if self._closed:
            return
        self.disconnect()
        self._closed = True  # later calls fail fast instead of waiting on a stopped loop
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive():
            self._loop.close()  # releases the selector and self-pipe file descriptors


def _translate_bleak_error(exc: Exception, what: str, address: str | None) -> InstrumentError:
    name = type(exc).__name__
    text = str(exc) or name
    low = text.lower()
    if "notavailable" in name.lower() or "bluetooth is turned off" in low or "powered off" in low:
        return InstrumentConnectionError(
            f"Bluetooth is not available on this computer ({text}). Turn Bluetooth on and make sure "
            "this program has permission to use it (macOS: System Settings > Privacy & Security > "
            "Bluetooth)."
        )
    if "notfound" in name.lower() and "characteristic" in name.lower():
        return InstrumentProtocolError(f"{what}: characteristic not found on {address} ({text}).")
    if "notfound" in name.lower():
        return InstrumentConnectionError(
            f"Bluetooth device {address} was not found ({text}). Is it on, advertising and in range? "
            "Use `scan_devices` to list nearby devices."
        )
    if "authentication" in low or "encryption" in low or "insufficient" in low:
        return InstrumentConnectionError(
            f"{what} was refused by {address}: {text}. The device requires pairing/bonding. On "
            "Linux/Windows restart the server with `--option pair=true` (and accept the pairing on "
            "the device); on macOS accept the system pairing prompt."
        )
    return InstrumentConnectionError(f"Bluetooth {what} failed for {address}: {name}: {text}")


# --------------------------------------------------------------------------- driver


@dataclass
class Packet:
    char: int  # 16-bit characteristic UUID
    t_s: float  # seconds since the listen started
    data: bytes


class _Cancel(threading.Event):
    """Set to stop a running listen; ``reason`` says why."""

    reason = "cancelled"


#: How long a new measurement waits for the one it replaces to unsubscribe and stop. Bounded by
#: the Bluetooth call timeouts (connect <= 2 x 60 + 10 s, subscribe 15 s, unsubscribe 10 s).
PREEMPT_WAIT_S = 180.0


class BLEHealthSensor:
    """One Bluetooth LE health sensor, identified by its address."""

    def __init__(
        self,
        backend: BLEBackend,
        address: str,
        *,
        audit: AuditLog | None = None,
        connect_timeout_s: float = 20.0,
        settle_s: float = 1.5,
    ) -> None:
        self.backend = backend
        self.address = address
        self.audit = audit
        self.connect_timeout_s = connect_timeout_s
        #: After the first measurement arrives, keep listening this long for more (monitors
        #: send stored measurements back-to-back, oldest first).
        self.settle_s = settle_s
        self._chars: set[str] = set()
        self._services: set[str] = set()
        self._lock = threading.RLock()
        # One listen at a time: a second subscription to the same characteristic would steal the
        # first one's notifications, and the first one's unsubscribe would then silence the second.
        self._listen_lock = threading.Lock()
        self._guard = threading.Lock()
        self._active: _Cancel | None = None
        self._closed = False

    # -- helpers ----------------------------------------------------------------

    def _event(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, f"ble://{self.address}")

    def ensure_connected(self, timeout_s: float | None = None) -> None:
        with self._lock:
            if self.backend.is_connected:
                return
            self._event("connect")
            self.backend.connect(timeout_s or self.connect_timeout_s)
            self._services = self.backend.service_uuids()
            self._chars = self.backend.characteristic_uuids()
            self._event("connected; services: " + ", ".join(self.standard_services()))

    def standard_services(self) -> list[str]:
        return [name for code, name in SERVICE_NAMES.items() if uuid16(code) in self._services]

    def has(self, char: int) -> bool:
        return uuid16(char) in self._chars

    def _require(self, char: int, what: str, service: int) -> None:
        if not self.has(char):
            found = ", ".join(self.standard_services()) or "none of the standard health services"
            raise InstrumentProtocolError(
                f"Device {self.address} does not expose {what} (0x{char:04X}, part of the "
                f"{SERVICE_NAMES[service]} service 0x{service:04X}). Standard services found: {found}. "
                "Many consumer devices use proprietary protocols instead of the Bluetooth SIG "
                "profiles; only standard-profile devices are supported."
            )

    def read_char(self, char: int) -> bytes:
        with self._lock:
            self.ensure_connected()
            data = self.backend.read(uuid16(char))
            self._event(f"read 0x{char:04X}: {data.hex(' ')}")
            return data

    # -- identity / device info -------------------------------------------------

    def identify(self) -> dict[str, Any]:
        info = self.device_info()
        return {k: v for k, v in info.items() if k not in {"features"}}

    def device_info(self) -> dict[str, Any]:
        self.ensure_connected()
        info: dict[str, Any] = {"address": self.address}
        for key, char in DIS_STRINGS.items():
            if self.has(char):
                with contextlib.suppress(InstrumentError):
                    info[key] = decode_string(self.read_char(char))
        if self.has(CHR_SYSTEM_ID):
            with contextlib.suppress(InstrumentError):
                info["system_id"] = self.read_char(CHR_SYSTEM_ID).hex()
        info["services"] = self.standard_services()
        features: dict[str, Any] = {}
        readers: list[tuple[str, int, Callable[[bytes], Any]]] = [
            ("body_sensor_location", CHR_BODY_SENSOR_LOCATION, parse_body_sensor_location),
            ("pulse_oximeter", CHR_PLX_FEATURES, parse_plx_features),
            ("blood_pressure", CHR_BLOOD_PRESSURE_FEATURE, parse_blood_pressure_feature),
            ("weight_scale", CHR_WEIGHT_SCALE_FEATURE, parse_weight_scale_feature),
            ("temperature_type", CHR_TEMPERATURE_TYPE, lambda d: parse_temperature_type(d[0]) if d else None),
        ]
        for key, char, parse in readers:
            if self.has(char):
                try:
                    features[key] = parse(self.read_char(char))
                except InstrumentError as exc:
                    features[key] = f"unreadable: {exc}"
        info["features"] = features
        return info

    def battery_level(self) -> int:
        self.ensure_connected()
        self._require(CHR_BATTERY_LEVEL, "Battery Level", SVC_BATTERY)
        return parse_battery_level(self.read_char(CHR_BATTERY_LEVEL))

    def scan(self, timeout_s: float) -> list[ScanResult]:
        self._event(f"scan {timeout_s:g} s")
        return self.backend.scan(timeout_s)

    # -- notifications / indications -------------------------------------------

    def _cancel_active(self, reason: str) -> None:
        with self._guard:
            if self._active is not None:
                self._active.reason = reason
                self._active.set()

    def _begin_listen(self) -> _Cancel:
        """Become the only running listen. A listen still running (e.g. one whose MCP call already
        timed out on the client side, so nobody will read its result) is cancelled first."""
        deadline = time.monotonic() + PREEMPT_WAIT_S
        while True:
            with self._guard:
                if self._closed:
                    raise InstrumentConnectionError(
                        "This connection was closed (reconnect or shutdown). Call the tool again."
                    )
            self._cancel_active("a new measurement was started")
            if self._listen_lock.acquire(timeout=0.25):
                break
            if time.monotonic() > deadline:
                raise InstrumentError(
                    f"The previous measurement did not stop within {PREEMPT_WAIT_S:g} s. Try again, or "
                    "call `reconnect`."
                )
        cancel = _Cancel()
        with self._guard:
            self._active = cancel
        return cancel

    def _end_listen(self, cancel: _Cancel) -> None:
        with self._guard:
            if self._active is cancel:
                self._active = None
        self._listen_lock.release()

    def _unsubscribe(self, subscribed: list[int]) -> None:
        for char in subscribed:
            with contextlib.suppress(InstrumentError):
                self.backend.unsubscribe(uuid16(char))
        subscribed.clear()

    def listen(
        self,
        chars: list[int],
        *,
        seconds: float,
        done: Callable[[list[Packet]], bool] | None = None,
        reconnect: bool = True,
        required: tuple[int, str, int] | None = None,
        partial_ok: bool = False,
    ) -> tuple[list[Packet], bool]:
        """Subscribe to ``chars`` (those the device has) and collect packets.

        Without ``done`` it records for ``seconds``. With ``done`` it stops ``settle_s`` after
        ``done(packets)`` first returns true, or raises ``InstrumentTimeout`` after ``seconds``
        (with ``partial_ok``, packets that did not satisfy ``done`` are returned instead).
        With ``reconnect`` it keeps (re)connecting until the deadline, which is how monitors
        that only advertise after a measurement are caught. Returns (packets, disconnected).

        Only one listen runs at a time: starting another one (or closing the connection) cancels
        this one, which then raises ``InstrumentError``.
        """
        cancel = self._begin_listen()
        q: queue.Queue[tuple[int, float, bytes]] = queue.Queue()
        packets: list[Packet] = []
        t0 = time.monotonic()
        end = t0 + seconds
        subscribed: list[int] = []
        disconnected = False
        last_error: InstrumentError | None = None
        satisfied = False
        try:
            while time.monotonic() < end:
                if cancel.is_set():
                    raise InstrumentError(f"The measurement was cancelled: {cancel.reason}.")
                if not subscribed:
                    try:
                        self.ensure_connected(min(max(end - time.monotonic(), 1.0), self.connect_timeout_s))
                        if required:
                            self._require(*required)
                        for char in chars:
                            if self.has(char) and not cancel.is_set():
                                self.backend.subscribe(
                                    uuid16(char), lambda data, c=char: q.put((c, time.monotonic(), data))
                                )
                                subscribed.append(char)
                        self._event("subscribed " + ", ".join(f"0x{c:04X}" for c in subscribed))
                    except InstrumentProtocolError:
                        raise
                    except InstrumentError as exc:
                        if not reconnect:
                            raise
                        last_error = exc
                        # Undo a partial subscription so the retry does not subscribe twice.
                        self._unsubscribe(subscribed)
                        cancel.wait(min(1.0, max(end - time.monotonic(), 0)))
                        continue
                try:
                    char, t, data = q.get(timeout=max(min(0.25, end - time.monotonic()), 0.001))
                except queue.Empty:
                    if not self.backend.is_connected:
                        disconnected = True
                        subscribed.clear()
                        self._event("device disconnected")
                        if not reconnect:
                            break
                    continue
                packets.append(Packet(char, t - t0, data))
                if len(packets) == 1:
                    self._event(f"first packet 0x{char:04X}: {data.hex(' ')}")
                if done is not None and not satisfied and done(packets):
                    satisfied = True
                    end = min(end, time.monotonic() + self.settle_s)
        finally:
            try:
                if subscribed:
                    self._unsubscribe(subscribed)
                    self._event(f"unsubscribed; {len(packets)} packets received")
            finally:
                self._end_listen(cancel)
        if done is not None and not satisfied:
            if partial_ok and packets:
                return packets, disconnected
            if last_error is not None and not packets:
                raise InstrumentConnectionError(
                    f"No measurement received within {seconds:g} s; last connection error: {last_error}"
                )
            raise InstrumentTimeout(
                f"No measurement received from {self.address} within {seconds:g} s. Start a "
                "measurement on the device (or check that it is not sending to a paired phone app) "
                "and call the tool again, possibly with a longer timeout."
            )
        return packets, disconnected

    # -- measurements -----------------------------------------------------------

    def record_heart_rate(self, duration_s: float) -> tuple[list[tuple[float, HeartRateMeasurement]], int, bool]:
        """Collect Heart Rate Measurement notifications for ``duration_s``.
        Returns (samples, malformed_packet_count, disconnected_early)."""
        self.ensure_connected()
        self._require(CHR_HEART_RATE_MEASUREMENT, "Heart Rate Measurement", SVC_HEART_RATE)
        packets, disconnected = self.listen([CHR_HEART_RATE_MEASUREMENT], seconds=duration_s, reconnect=False)
        samples: list[tuple[float, HeartRateMeasurement]] = []
        malformed = 0
        for p in packets:
            try:
                samples.append((p.t_s, parse_heart_rate_measurement(p.data)))
            except InstrumentProtocolError:
                malformed += 1
        return samples, malformed, disconnected

    def _parse(self, packets: list[Packet], parse: Callable[[Packet], Any]) -> list[Any]:
        """Parse pushed packets, skipping (and logging) malformed ones so one corrupted packet does
        not discard a whole recording or the valid measurements around it. Raises the parse error
        only if no packet at all could be decoded."""
        out: list[Any] = []
        error: InstrumentProtocolError | None = None
        for p in packets:
            try:
                out.append(parse(p))
            except InstrumentProtocolError as exc:
                error = exc
                self._event(f"malformed packet skipped: {exc}")
        if error is not None and not out:
            raise error
        return out

    def pulse_oximetry(
        self, mode: str, duration_s: float, timeout_s: float
    ) -> tuple[list[PulseOximetryMeasurement], int]:
        """``continuous``: collect 0x2A5F notifications for ``duration_s``.
        ``spot_check``: wait up to ``timeout_s`` for 0x2A5E indications (connecting whenever the
        oximeter becomes available). ``auto`` connects and picks continuous when the device
        supports it. Returns (measurements, malformed_packet_count)."""
        if mode == "auto":
            self.ensure_connected()
            mode = "continuous" if self.has(CHR_PLX_CONTINUOUS) else "spot_check"
        if mode == "continuous":
            self.ensure_connected()
            self._require(CHR_PLX_CONTINUOUS, "PLX Continuous Measurement", SVC_PULSE_OXIMETER)
            packets, _ = self.listen([CHR_PLX_CONTINUOUS], seconds=duration_s, reconnect=False)
            if not packets:
                raise InstrumentTimeout(
                    f"No PLX Continuous Measurement notifications within {duration_s:g} s. Is the "
                    "oximeter on a finger and measuring?"
                )
            parsed = self._parse(packets, lambda p: parse_plx_continuous(p.data))
            return parsed, len(packets) - len(parsed)
        # Spot-check oximeters often only advertise once a reading is ready, so do not insist on a
        # connection up front: listen() keeps (re)connecting until the timeout.
        packets, _ = self.listen(
            [CHR_PLX_SPOT_CHECK],
            seconds=timeout_s,
            done=lambda ps: any(p.char == CHR_PLX_SPOT_CHECK for p in ps),
            required=(CHR_PLX_SPOT_CHECK, "PLX Spot-check Measurement", SVC_PULSE_OXIMETER),
        )
        parsed = self._parse(packets, lambda p: parse_plx_spot_check(p.data))
        return parsed, len(packets) - len(parsed)

    def blood_pressure(self, timeout_s: float) -> tuple[list[BloodPressureMeasurement], list[BloodPressureMeasurement]]:
        """Wait for Blood Pressure Measurement indications (0x2A35). Also listens to Intermediate
        Cuff Pressure (0x2A36) when present. Returns (measurements, cuff_pressure_updates)."""
        req = (CHR_BLOOD_PRESSURE_MEASUREMENT, "Blood Pressure Measurement", SVC_BLOOD_PRESSURE)
        packets, _ = self.listen(
            [CHR_BLOOD_PRESSURE_MEASUREMENT, CHR_INTERMEDIATE_CUFF_PRESSURE],
            seconds=timeout_s,
            done=lambda ps: any(p.char == CHR_BLOOD_PRESSURE_MEASUREMENT for p in ps),
            required=req,
        )
        final = self._parse(
            [p for p in packets if p.char == CHR_BLOOD_PRESSURE_MEASUREMENT],
            lambda p: parse_blood_pressure_measurement(p.data),
        )
        cuff: list[BloodPressureMeasurement] = []
        for p in packets:
            if p.char == CHR_INTERMEDIATE_CUFF_PRESSURE:
                with contextlib.suppress(InstrumentProtocolError):
                    cuff.append(parse_blood_pressure_measurement(p.data, "Intermediate Cuff Pressure (0x2A36)"))
        return final, cuff

    def temperature(
        self, timeout_s: float, accept_intermediate: bool = False
    ) -> tuple[list[tuple[bool, TemperatureMeasurement]], str | None]:
        """Wait for Temperature Measurement indications (0x2A1C), optionally also accepting
        Intermediate Temperature notifications (0x2A1E). Returns ([(is_final, measurement)],
        sensor_type).

        The wait always ends on a final Temperature Measurement (HTS: indicated once the
        measurement is complete); Intermediate Temperature values, which a thermometer notifies
        repeatedly while the probe settles, are only returned if no final value arrives before
        the timeout."""
        chars = [CHR_TEMPERATURE_MEASUREMENT] + ([CHR_INTERMEDIATE_TEMPERATURE] if accept_intermediate else [])
        packets, _ = self.listen(
            chars,
            seconds=timeout_s,
            done=lambda ps: any(p.char == CHR_TEMPERATURE_MEASUREMENT for p in ps),
            required=(CHR_TEMPERATURE_MEASUREMENT, "Temperature Measurement", SVC_HEALTH_THERMOMETER),
            partial_ok=accept_intermediate,
        )
        out = self._parse(
            packets,
            lambda p: (
                p.char == CHR_TEMPERATURE_MEASUREMENT,
                parse_temperature_measurement(
                    p.data,
                    "Temperature Measurement (0x2A1C)"
                    if p.char == CHR_TEMPERATURE_MEASUREMENT
                    else "Intermediate Temperature (0x2A1E)",
                ),
            ),
        )
        sensor_type = None
        if self.has(CHR_TEMPERATURE_TYPE):
            try:
                data = self.read_char(CHR_TEMPERATURE_TYPE)
                sensor_type = parse_temperature_type(data[0]) if data else None
            except InstrumentError:
                pass
        return out, sensor_type

    def weight(self, timeout_s: float) -> tuple[list[WeightMeasurement], dict[str, Any] | None]:
        """Wait for Weight Measurement indications (0x2A9D). Returns (measurements, features)."""
        packets, _ = self.listen(
            [CHR_WEIGHT_MEASUREMENT],
            seconds=timeout_s,
            done=lambda ps: bool(ps),
            required=(CHR_WEIGHT_MEASUREMENT, "Weight Measurement", SVC_WEIGHT_SCALE),
        )
        features = None
        if self.has(CHR_WEIGHT_SCALE_FEATURE):
            with contextlib.suppress(InstrumentError):
                features = parse_weight_scale_feature(self.read_char(CHR_WEIGHT_SCALE_FEATURE))
        return self._parse(packets, lambda p: parse_weight_measurement(p.data)), features

    def close(self) -> None:
        with self._guard:
            self._closed = True
        self._cancel_active("the connection was closed (reconnect or server shutdown)")
        # Let a cancelled listen unsubscribe while the link is still up (bounded wait).
        if self._listen_lock.acquire(timeout=2.0):
            self._listen_lock.release()
        self.backend.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
