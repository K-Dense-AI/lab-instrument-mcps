"""SCPI driver for Thorlabs optical power and energy meters.

The PM100 family (PM100D, PM100A, PM100USB) and the PM400 share one SCPI command set; the newer
PM101 and PM5020 documentation lists the same commands (the PM5020 adds a channel suffix,
``SENS2``/``MEAS2``/``SYST:SENS2:IDN?``, for its second sensor input). Every command used here was
checked against:

* "Optical Power and Energy Meter PM100D Operation Manual", Thorlabs 17654-D02 Rev H (2017),
  section 6.4 "PM100D SCPI Commands" (the same table appears in the PM100USB manual 19570-D02
  Rev C, section 6.3, and states that all commands also work on the PM100A and PM100USB).
* "Optical Power and Energy Meter PM400 Operating Manual", Thorlabs, version 1.1 (2019),
  section 5.5 "SCPI Commands" ("all described commands work also with the PM100D, PM100A,
  PM100USB and PM160").
* Thorlabs' per-model SCPI command documentation (pm400.html, pm101.html, pm5020.html,
  pm100d3.html), published by Thorlabs at
  https://github.com/Thorlabs/Light_Analysis_Examples/tree/main/Python/Thorlabs%20PMxxx%20Power%20Meters/SCPI/commandDocu

Commands (long forms as documented):

* ``*IDN?`` -> ``THORLABS,<model>,<serial>,<firmware>``
* ``SYSTem:SENSor:IDN?`` -> ``<name>,<serial>,<cal date>,<type>,<subtype>,<flags>``
* ``MEASure[:SCALar][:POWer]?`` -> power in the unit set by ``SENSe:POWer[:DC]:UNIT {W|DBM}``;
  INFINITY/-INFINITY (or the SCPI overflow value 9.9E37) when out of range
* ``MEASure[:SCALar]:TEMPerature?`` -> sensor head temperature in °C (sensor flag 256 only)
* ``SENSe:CORRection:WAVelength <nm>`` / ``...:WAVelength? [MIN|MAX]``
* ``SENSe:AVERage[:COUNt] <n>`` / ``?`` (PM100: one sample ~3 ms; PM400/PM101/PM5020: 1 kHz)
* ``SENSe:POWer[:DC]:RANGe:AUTO {0|1}`` / ``?``; ``SENSe:POWer[:DC]:RANGe[:UPPer] <W>`` / ``? [MIN|MAX]``
* ``SENSe:CORRection:COLLect:ZERO[:INITiate]``, ``...:ZERO:STATe?``, ``...:ZERO:MAGNitude?``
* ``SYSTem:ERRor[:NEXT]?``
"""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport
from labmcp.scpi import SCPIDriver

#: SYST:SENS:IDN? sensor type codes (PM400 / PM5020 command documentation).
SENSOR_TYPES = {
    0: "none",
    1: "photodiode",
    2: "thermopile",
    3: "pyroelectric",
    5: "four-quadrant thermopile",
}

#: SYST:SENS:IDN? capability flags.
FLAG_POWER = 1
FLAG_ENERGY = 2
FLAG_RESPONSE_SETTABLE = 16
FLAG_WAVELENGTH_SETTABLE = 32
FLAG_TAU_SETTABLE = 64
FLAG_TEMPERATURE = 256

#: SCPI returns +9.9E37 for "infinity" (overflow).
_OVERFLOW = 9.0e37


@dataclass
class SensorInfo:
    name: str
    serial: str
    calibration: str
    type_code: int
    type_name: str
    subtype: int
    flags: int

    @property
    def connected(self) -> bool:
        return self.type_code != 0 and self.name.lower() not in {"", "no sensor"}

    @property
    def measures_power(self) -> bool:
        return bool(self.flags & FLAG_POWER)

    @property
    def measures_energy(self) -> bool:
        return bool(self.flags & FLAG_ENERGY)

    @property
    def wavelength_settable(self) -> bool:
        return bool(self.flags & FLAG_WAVELENGTH_SETTABLE)

    @property
    def has_temperature_sensor(self) -> bool:
        return bool(self.flags & FLAG_TEMPERATURE)


def watts_to_dbm(power_w: float) -> float | None:
    """Convert W to dBm (re 1 mW). ``None`` for zero or negative power (after zeroing, noise can
    make a dark reading slightly negative)."""
    if power_w <= 0 or not math.isfinite(power_w):
        return None
    return 10.0 * math.log10(power_w / 1e-3)


def format_power(power_w: float) -> str:
    """Human-readable power with an SI prefix, e.g. ``1.234 mW``."""
    if not math.isfinite(power_w):
        return "over range"
    mag = abs(power_w)
    for factor, unit in ((1.0, "W"), (1e-3, "mW"), (1e-6, "µW"), (1e-9, "nW"), (1e-12, "pW")):
        if mag >= factor:
            return f"{power_w / factor:.4g} {unit}"
    return f"{power_w / 1e-15:.4g} fW"


class ThorlabsPowerMeter(SCPIDriver):
    """A Thorlabs PM100-family / PM400-family power meter.

    Args:
        transport: An open transport (normally VISA USBTMC).
        channel: Sensor channel suffix for multi-channel consoles (PM5020: 1 or 2). ``None`` sends
            the plain commands (``SENS:...``, ``MEAS:...``), which address the only (or first)
            channel on every model.
    """

    def __init__(self, transport: Transport, channel: int | None = None) -> None:
        super().__init__(transport)
        suffix = "" if channel is None else str(channel)
        self.channel = channel
        self._sens = f"SENS{suffix}"
        self._meas = f"MEAS{suffix}"
        self._sensor_idn = f"SYST:SENS{suffix}:IDN?"
        self._averaging: int | None = None
        self._sensor: SensorInfo | None = None

    # ------------------------------------------------------------ helpers

    def command(self, cmd: str) -> None:
        """Send a setting command and raise if the meter queued an error for it."""
        with self.t.lock:
            self.write(cmd)
            self.check_errors(cmd)

    def _query_number(self, cmd: str, timeout: float | None = None) -> float:
        reply = self.query(cmd, timeout)
        try:
            return float(reply.split(",")[0])
        except ValueError as exc:
            raise InstrumentProtocolError(f"Expected a number from {cmd!r}, got {reply!r}") from exc

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        info = super().identify()
        try:
            sensor = self.sensor(refresh=True)
        except InstrumentProtocolError:
            return info
        info["sensor"] = sensor.name if sensor.connected else "none"
        if sensor.connected:
            info["sensor_serial"] = sensor.serial
            info["sensor_type"] = sensor.type_name
        return info

    def sensor(self, refresh: bool = False) -> SensorInfo:
        """``SYST:SENS:IDN?``: name, serial, calibration date, type, subtype and capability flags."""
        if self._sensor is not None and not refresh:
            return self._sensor
        reply = self.query(self._sensor_idn)
        fields = [f.strip() for f in next(csv.reader([reply], skipinitialspace=True))]
        if len(fields) < 6:
            raise InstrumentProtocolError(
                f"Unexpected reply to {self._sensor_idn!r}: {reply!r} (expected 6 fields: "
                "name, serial, calibration, type, subtype, flags)"
            )
        try:
            type_code, subtype, flags = int(float(fields[3])), int(float(fields[4])), int(float(fields[5]))
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unexpected sensor type/flags in {reply!r}") from exc
        self._sensor = SensorInfo(
            name=fields[0],
            serial=fields[1],
            calibration=fields[2],
            type_code=type_code,
            type_name=SENSOR_TYPES.get(type_code, f"unknown ({type_code})"),
            subtype=subtype,
            flags=flags,
        )
        return self._sensor

    # ------------------------------------------------------------ measurement

    def power_unit(self) -> str:
        unit = self.query(f"{self._sens}:POW:UNIT?").strip().strip('"').upper()
        if unit not in {"W", "DBM"}:
            raise InstrumentProtocolError(f"Unexpected power unit {unit!r} (expected W or DBM)")
        return unit

    def averaging(self) -> int:
        """``SENS:AVER:COUN?``: number of samples averaged per reading."""
        self._averaging = int(self._query_number(f"{self._sens}:AVER:COUN?"))
        return self._averaging

    def set_averaging(self, count: int) -> int:
        self.command(f"{self._sens}:AVER:COUN {int(count)}")
        return self.averaging()

    def _measure_timeout(self) -> float:
        # PM100 series: one sample takes ~3 ms (PM100D manual 6.4.2.3.5). Newer consoles are faster.
        count = self._averaging if self._averaging is not None else self.averaging()
        return 3.0 + count * 0.003 * 1.5

    def power_w(self) -> float:
        """``MEAS:POW?`` converted to watts (the reply is in the unit set by ``SENS:POW:UNIT``)."""
        sensor = self.sensor()
        if not sensor.connected:
            raise InstrumentProtocolError("No sensor is connected to the power meter.")
        if not sensor.measures_power:
            raise InstrumentProtocolError(
                f"Sensor {sensor.name} ({sensor.type_name}) is an energy sensor; it cannot measure "
                "CW power. Energy (pulse) measurement is not supported by this server."
            )
        with self.t.lock:
            unit = self.power_unit()
            value = self._query_number(f"{self._meas}:POW?", timeout=self._measure_timeout())
        if not math.isfinite(value) or abs(value) >= _OVERFLOW:
            raise InstrumentProtocolError(
                f"Power meter reports the signal is out of the measurement range ({value!r}). "
                "Enable auto-ranging (`set_range` mode=auto) or select a higher range."
            )
        if unit == "DBM":
            value = 1e-3 * 10.0 ** (value / 10.0)
        return value

    def temperature_c(self) -> float:
        """``MEAS:TEMP?``: sensor head temperature (only sensors with flag 256)."""
        sensor = self.sensor()
        if not sensor.has_temperature_sensor:
            raise InstrumentProtocolError(
                f"Sensor {sensor.name or '(none)'} has no temperature sensor (SYST:SENS:IDN? flags "
                f"{sensor.flags} lack bit 256)."
            )
        return self._query_number(f"{self._meas}:TEMP?")

    # ------------------------------------------------------------ wavelength

    def wavelength_nm(self) -> float:
        return self._query_number(f"{self._sens}:CORR:WAV?")

    def wavelength_range_nm(self) -> tuple[float, float]:
        """Sensor calibration range from ``SENS:CORR:WAV? MIN`` / ``MAX``."""
        with self.t.lock:
            lo = self._query_number(f"{self._sens}:CORR:WAV? MIN")
            hi = self._query_number(f"{self._sens}:CORR:WAV? MAX")
        return lo, hi

    def set_wavelength_nm(self, wavelength_nm: float) -> float:
        """Set the correction wavelength (whole nm, as the PM400/PM101/PM5020 references specify)."""
        self.command(f"{self._sens}:CORR:WAV {int(round(wavelength_nm))}")
        return self.wavelength_nm()

    # ------------------------------------------------------------ range

    def auto_range(self) -> bool:
        return self.query_bool(f"{self._sens}:POW:RANG:AUTO?")

    def set_auto_range(self, enabled: bool) -> None:
        self.command(f"{self._sens}:POW:RANG:AUTO {1 if enabled else 0}")

    def range_w(self) -> float:
        """Upper limit (W) of the power range presently in use."""
        return self._query_number(f"{self._sens}:POW:RANG?")

    def range_limits_w(self) -> tuple[float, float]:
        """Upper limits of the most (MIN) and least (MAX) sensitive power ranges."""
        with self.t.lock:
            lo = self._query_number(f"{self._sens}:POW:RANG? MIN")
            hi = self._query_number(f"{self._sens}:POW:RANG? MAX")
        return lo, hi

    def set_range_w(self, power_w: float) -> float:
        """Select the range that best fits ``power_w`` (this disables auto-ranging)."""
        self.command(f"{self._sens}:POW:RANG {power_w:.6g}")
        return self.range_w()

    # ------------------------------------------------------------ zero

    def zero_value(self) -> float:
        """``SENS:CORR:COLL:ZERO:MAGN?``: present zero correction (A for photodiodes, V for thermal)."""
        return self._query_number(f"{self._sens}:CORR:COLL:ZERO:MAGN?")

    def zero(self, timeout: float = 60.0, poll_s: float = 0.2) -> float:
        """Run the dark-zero routine and wait for it. Returns the zero value (A or V)."""
        with self.t.lock:
            self.write(f"{self._sens}:CORR:COLL:ZERO:INIT")
            deadline = time.monotonic() + timeout
            while True:
                if not self.query_bool(f"{self._sens}:CORR:COLL:ZERO:STAT?"):
                    break
                if time.monotonic() > deadline:
                    self.write(f"{self._sens}:CORR:COLL:ZERO:ABOR")
                    raise InstrumentTimeout(
                        f"Zeroing did not finish within {timeout:g} s and was aborted. Make sure the "
                        "sensor aperture is completely covered."
                    )
                time.sleep(poll_s)
            self.check_errors("zero adjustment")
            return self.zero_value()
