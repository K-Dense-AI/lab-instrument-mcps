"""IKA NAMUR driver for hotplate stirrers and overhead stirrers.

Commands, serial settings and terminators were verified in the "Interfaces and outputs"
section of these IKA operating instructions:

* IKA Plate (RCT digital), 11/2018 (print code 20014388),
  https://shop.textalk.se/shop/ws72/72372/art12/156948912-a58526-20000015643_20014388a_A2_IKA_Plate_112018_web.pdf
* C-MAG HS 7 control, 11/2018 (print code 20014381a),
  https://shop.textalk.se/shop/ws72/72372/art16/156948916-cc53a2-20000015641_20014381a_A2_C-MAG_HS_7_control_112018_web.pdf
* EUROSTAR 60 control / EUROSTAR 100 control (20000003965c, 03/2018),
  https://www.wolflabs.co.uk/documents/IKA_overhead-stirrers_Eurostar-60-100-control_manual.pdf

Wire format (identical in all three manuals): 9600 baud, 7 data bits, even parity, 1 stop
bit, no flow control. Commands are upper case, command and parameter are separated by a
space, and every command and every response ends with "blank CR LF" (0x20 0x0D 0x0A).
The device only transmits when asked: ``IN_*`` commands return one line, ``START_*``,
``STOP_*`` and ``OUT_SP_n`` return nothing, and the ``@`` variants (``OUT_SP_12@n``,
``OUT_SP_42@n``, ``OUT_WDx@m``) echo the value. The manuals do not document the exact
layout of the reply line, so numeric replies are parsed as "first number on the line"
(real devices are reported to append the channel number, e.g. ``25.3 2``).

Channel numbers: 1 = external temperature sensor, 2 = hotplate, 3 = PT1000 probe
(overhead stirrers), 4 = stirring speed, 5 = viscosity trend (hotplate) / torque (overhead).
"""

from __future__ import annotations

import logging
import re
import threading

from labmcp import InstrumentError, InstrumentProtocolError, Transport

log = logging.getLogger("labmcp.ika")

HOTPLATE = "hotplate"
OVERHEAD = "overhead"
DEVICE_TYPES = (HOTPLATE, OVERHEAD)

#: Watchdog time range documented for OUT_WD1@m / OUT_WD2@m.
WATCHDOG_MIN_S = 20
WATCHDOG_MAX_S = 1500

_NUMBER_RE = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s+\S+)*\s*$")


class IKAStirrer:
    """An IKA hotplate stirrer (``device_type="hotplate"``) or overhead stirrer (``"overhead"``)."""

    def __init__(self, transport: Transport, device_type: str = HOTPLATE) -> None:
        if device_type not in DEVICE_TYPES:
            raise ValueError(f"device_type must be one of {DEVICE_TYPES}, got {device_type!r}")
        self.t = transport
        self.device_type = device_type
        # The NAMUR set has no "is the heater on?" query, so remember what we commanded.
        self.heating_commanded: bool | None = None
        self.stirring_commanded: bool | None = None
        #: Set by stop commands so a running wait_for_temperature loop ends early.
        self.abort = threading.Event()
        self._wd_mode: int | None = None
        self._wd_time_s: int | None = None
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        self.watchdog_last_error: str | None = None

    # ------------------------------------------------------------ low level

    def query(self, cmd: str, timeout: float | None = None) -> str:
        """Send an ``IN_*`` command and return the reply line (stripped)."""
        with self.t.lock:
            # The device never sends unsolicited data; anything waiting is stale.
            self.t.flush_input()
            reply = self.t.query(cmd, timeout).strip()
        if not reply:
            raise InstrumentProtocolError(f"IKA device sent an empty reply to {cmd!r}.")
        return reply

    def send(self, cmd: str) -> None:
        """Send a command that has no reply (``OUT_SP_n x``, ``START_n``, ``STOP_n``)."""
        self.t.write(cmd)

    def query_number(self, cmd: str) -> float:
        reply = self.query(cmd)
        m = _NUMBER_RE.match(reply)
        if not m:
            raise InstrumentProtocolError(
                f"IKA device replied {reply!r} to {cmd!r}; expected a number. Check that the "
                f"server's device type ({self.device_type}) matches the instrument."
            )
        return float(m.group(1))

    def _echo(self, cmd: str, expected: float) -> float:
        """Send an ``@`` command and check the echoed value."""
        value = self.query_number(cmd)
        if abs(value - expected) > 0.5:
            raise InstrumentProtocolError(
                f"IKA device echoed {value:g} to {cmd!r}; expected {expected:g}. The value may be "
                "outside the range the device accepts."
            )
        return value

    def _require(self, device_type: str, what: str) -> None:
        if self.device_type != device_type:
            raise InstrumentProtocolError(
                f"{what} is only available on IKA {device_type} devices, but this server is "
                f"configured for a {self.device_type} (start it with --option device={device_type} "
                "if that is wrong)."
            )

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{round(value, 1):g}"

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "IKA", "device_type": self.device_type}
        try:
            info["model"] = self.query("IN_NAME")
        except InstrumentError as exc:
            info["model"] = f"unknown ({exc})"
        return info

    # ------------------------------------------------------------ stirring (both types)

    def speed_rpm(self) -> float:
        return self.query_number("IN_PV_4")

    def speed_setpoint_rpm(self) -> float:
        return self.query_number("IN_SP_4")

    def set_speed(self, rpm: float) -> None:
        self.send(f"OUT_SP_4 {int(round(rpm))}")

    def start_motor(self) -> None:
        self.abort.clear()
        self.send("START_4")
        self.stirring_commanded = True

    def stop_motor(self) -> None:
        self.abort.set()
        self.send("STOP_4")
        self.stirring_commanded = False

    # ------------------------------------------------------------ hotplate

    def external_temperature_c(self) -> float:
        """``IN_PV_1``: external temperature sensor (e.g. PT 1000 / ETS-D in the medium)."""
        self._require(HOTPLATE, "The external sensor reading IN_PV_1")
        return self.query_number("IN_PV_1")

    def hotplate_temperature_c(self) -> float:
        """``IN_PV_2``: hotplate sensor."""
        self._require(HOTPLATE, "Hotplate temperature")
        return self.query_number("IN_PV_2")

    def viscosity_trend(self) -> float:
        """``IN_PV_5``: viscosity trend value (hotplates; relative, no unit)."""
        self._require(HOTPLATE, "The viscosity trend")
        return self.query_number("IN_PV_5")

    def temperature_setpoint_c(self) -> float:
        self._require(HOTPLATE, "The temperature setpoint")
        return self.query_number("IN_SP_1")

    def safety_temperature_c(self) -> float:
        """``IN_SP_3``: the safety-circuit temperature set on the device (screwdriver dial)."""
        self._require(HOTPLATE, "The safety temperature")
        return self.query_number("IN_SP_3")

    def set_temperature(self, celsius: float) -> None:
        self._require(HOTPLATE, "Setting a temperature")
        self.send(f"OUT_SP_1 {self._fmt(celsius)}")

    def start_heater(self) -> None:
        self._require(HOTPLATE, "Heating")
        self.abort.clear()
        self.send("START_1")
        self.heating_commanded = True

    def stop_heater(self) -> None:
        self._require(HOTPLATE, "Heating")
        self.abort.set()
        self.send("STOP_1")
        self.heating_commanded = False

    # ------------------------------------------------------------ overhead stirrer

    def probe_temperature_c(self) -> float:
        """``IN_PV_3``: PT1000 probe of an overhead stirrer."""
        self._require(OVERHEAD, "The PT1000 probe reading IN_PV_3")
        return self.query_number("IN_PV_3")

    def torque(self) -> float:
        """``IN_PV_5``: current torque value (overhead stirrers, Ncm as on the display)."""
        self._require(OVERHEAD, "Torque")
        return self.query_number("IN_PV_5")

    def torque_limit(self) -> float:
        self._require(OVERHEAD, "The torque limit")
        return self.query_number("IN_SP_5")

    def speed_limit_rpm(self) -> float:
        self._require(OVERHEAD, "The speed limit")
        return self.query_number("IN_SP_6")

    def safety_speed_rpm(self) -> float:
        self._require(OVERHEAD, "The safety speed")
        return self.query_number("IN_SP_8")

    # ------------------------------------------------------------ stop

    def stop_all(self) -> list[str]:
        """Stop heating (hotplates) and stirring. Sends every stop even if one fails."""
        self.abort.set()
        errors: list[str] = []
        stops = [self.stop_heater, self.stop_motor] if self.device_type == HOTPLATE else [self.stop_motor]
        for stop in stops:
            try:
                stop()
            except InstrumentError as exc:
                errors.append(str(exc))
        return errors

    # ------------------------------------------------------------ watchdog (hotplates)

    def set_watchdog_safety_values(self, temperature_c: float, speed_rpm: float) -> None:
        """``OUT_SP_12@n`` / ``OUT_SP_42@n``: values watchdog mode 2 falls back to."""
        self._require(HOTPLATE, "The watchdog")
        self._echo(f"OUT_SP_12@{self._fmt(temperature_c)}", temperature_c)
        self._echo(f"OUT_SP_42@{int(round(speed_rpm))}", round(speed_rpm))

    def enable_watchdog(self, mode: int, timeout_s: int, keepalive_interval_s: float | None = None) -> None:
        """Arm watchdog mode 1 (heater and motor off) or 2 (fall back to the WD safety values)
        and keep it alive from a background thread by re-sending ``OUT_WDx@m``.

        If this process dies, the cable is pulled or :meth:`close` is called, the device
        is no longer refreshed and trips after ``timeout_s`` seconds.
        """
        self._require(HOTPLATE, "The watchdog")
        if mode not in (1, 2):
            raise ValueError("watchdog mode must be 1 or 2")
        if not WATCHDOG_MIN_S <= timeout_s <= WATCHDOG_MAX_S:
            raise ValueError(f"watchdog time must be {WATCHDOG_MIN_S}-{WATCHDOG_MAX_S} s")
        if self._wd_mode is not None and self._wd_mode != mode:
            raise InstrumentProtocolError(
                f"Watchdog mode {self._wd_mode} is already active; the manual documents no way to "
                "switch modes without it tripping."
            )
        self._stop_keepalive()
        self._echo(f"OUT_WD{mode}@{timeout_s}", timeout_s)
        self._wd_mode, self._wd_time_s = mode, timeout_s
        self.watchdog_last_error = None
        interval = keepalive_interval_s if keepalive_interval_s is not None else max(1.0, timeout_s / 4)
        self._wd_stop.clear()
        self._wd_thread = threading.Thread(
            target=self._keepalive, args=(interval,), name="ika-watchdog", daemon=True
        )
        self._wd_thread.start()

    def _keepalive(self, interval: float) -> None:
        while not self._wd_stop.wait(interval):
            self.watchdog_tick()

    def watchdog_tick(self) -> None:
        """Re-send the watchdog command once (called by the keep-alive thread)."""
        mode, time_s = self._wd_mode, self._wd_time_s
        if mode is None or time_s is None:
            return
        try:
            self._echo(f"OUT_WD{mode}@{time_s}", time_s)
            self.watchdog_last_error = None
        except InstrumentError as exc:  # keep trying; the device trips if we never get through
            self.watchdog_last_error = str(exc)
            log.warning("IKA watchdog refresh failed: %s", exc)

    def disable_watchdog(self) -> None:
        """Cancel watchdog mode 2 with ``OUT_WD2@0``. Mode 1 has no documented cancel command."""
        self._require(HOTPLATE, "The watchdog")
        if self._wd_mode is None:
            return
        if self._wd_mode == 1:
            raise InstrumentProtocolError(
                "Watchdog mode 1 cannot be cancelled over the interface (the IKA manual documents "
                "no command for it). It stays armed while this server runs; stopping the server "
                "makes it trip, switching heating and stirring off."
            )
        self._stop_keepalive()
        self.query_number("OUT_WD2@0")
        self._wd_mode = self._wd_time_s = None

    def watchdog_state(self) -> dict[str, object]:
        return {
            "mode": self._wd_mode,
            "timeout_s": self._wd_time_s,
            "keepalive_running": bool(self._wd_thread and self._wd_thread.is_alive()),
            "last_error": self.watchdog_last_error,
        }

    def _stop_keepalive(self) -> None:
        self._wd_stop.set()
        thread, self._wd_thread = self._wd_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def close(self) -> None:
        # Deliberately does NOT cancel the watchdog: if it is armed, the device switches
        # itself off after the watchdog time, which is the fail-safe we want.
        self._stop_keepalive()
        self.t.close()
