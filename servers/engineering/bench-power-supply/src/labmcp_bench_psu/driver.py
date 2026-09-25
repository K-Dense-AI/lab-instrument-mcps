"""Drivers for programmable DC bench power supplies, one class per command dialect.

Every command below was checked in the vendor's own programming documentation:

* Rigol DP800 Programming Guide PGH03109-1110 (Jun 2021), DP700 Programming Guide
  PGH05101-1110 (Jun 2016), DP900 Programming Guide PGH08101-1110 (2024), all from
  https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/ :
  ``:SOUR<n>:VOLT``/``:CURR`` (p2-83/2-90), ``:APPL? CHn`` (p2-9), ``:OUTP CHn,ON`` (p2-63),
  ``:MEAS:ALL? CHn`` (p2-34), ``:OUTP:MODE? CHn`` (p2-53), ``:OUTP:OVP[:STAT|:VAL|:QUES?|:CLEAR]``
  and the OCP equivalents (p2-54..61), ``:SYST:ERR?`` (p2-118).
* Siglent SPD3303X/X-E Quick Start EN_02A (2022), "Remote control" chapter
  (https://siglentna.com/wp-content/uploads/dlm_uploads/2022/11/SPD3303X_QuickStart_E02A.pdf) and
  SPD1000X User Manual EN_03B (2025) (https://siglentna.com/wp-content/uploads/dlm_uploads/2025/06/SPD1000X_UserManual_E03B_0613.pdf):
  ``CHn:VOLT``/``CHn:CURR``, ``OUTP CHn,ON``, ``MEAS:VOLT? CHn``, ``SYST:STAT?`` (hex status
  word), ``SYST:ERR?``, SPD1000X ``OVP``/``OCP`` levels and ``OUTP:RESE:PROT``.
* Aim-TTi (Thurlby Thandar) instruction manuals from https://resources.aimtti.com/manuals/ :
  CPX400D/DP Iss. 14 (48511-1480), MX100T/TP Iss. 6 (48511-1610), MX180T/TP (48511-1780),
  QL Series II Iss. 8 (48511-1560), PL-P Iss. 18 (48511-1140): ``V<n>``, ``I<n>``, ``V<n>?``
  (``V1 5.000``), ``V<n>O?`` (``5.001V``), ``OP<n>``, ``OPALL``, ``OVP<n>``/``OCP<n>``
  (``VP1 30.00``/``CP1 2.000``, ``IP1`` on QL), ``LSR<n>?``, ``TRIPRST``, ``EER?``, ``QER?``.

Keysight/Agilent and Rohde & Schwarz supplies are deliberately not handled: both vendors ship
official MCP servers.

Safety-relevant choices:

* Rigol trips are cleared with ``:OUTP:OVP:CLEAR`` (flag only). ``:SOUR:VOLT:PROT:CLE`` is never
  sent because the guide says it also turns the output back on.
* "All outputs off" switches each channel off individually (plus Aim-TTi ``OPALL 0``) and then
  reads the states back where the instrument can report them.
* After every setting the instrument's error queue is read, so a rejected value is reported
  instead of silently ignored.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport
from labmcp.scpi import SCPIDriver

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _float(reply: str, what: str) -> float:
    m = _NUM_RE.search(reply)
    if not m:
        raise InstrumentProtocolError(f"Expected a number for {what}, got {reply!r}")
    return float(m.group())


def _last_float(reply: str, what: str) -> float:
    found = _NUM_RE.findall(reply)
    if not found:
        raise InstrumentProtocolError(f"Expected a number for {what}, got {reply!r}")
    return float(found[-1])


def _bool(reply: str, what: str) -> bool:
    text = reply.strip().upper().lstrip("+")
    if text in {"1", "ON", "YES"}:
        return True
    if text in {"0", "OFF", "NO"}:
        return False
    raise InstrumentProtocolError(f"Expected ON/OFF for {what}, got {reply!r}")


@dataclass(frozen=True)
class ChannelSpec:
    """A programmable output. Maxima are the highest *settable* values from the vendor tables."""

    number: int
    max_voltage_v: float
    max_current_a: float
    negative: bool = False  # e.g. Rigol DP831 CH3 (-30 V)
    programmable: bool = True  # False for fixed outputs (Siglent SPD3303X CH3)
    note: str = ""


@dataclass
class Protection:
    ovp_v: float | None = None
    ovp_enabled: bool | None = None
    ovp_tripped: bool | None = None
    ocp_a: float | None = None
    ocp_enabled: bool | None = None
    ocp_tripped: bool | None = None
    notes: list[str] = field(default_factory=list)


def _specs(*rows: tuple) -> list[ChannelSpec]:
    return [ChannelSpec(*row) for row in rows]


RIGOL_MODELS: dict[str, list[ChannelSpec]] = {
    # DP800 Programming Guide Table 2-1 (settable ranges).
    "DP832": _specs((1, 32, 3.2), (2, 32, 3.2), (3, 5.3, 3.2)),
    "DP831": _specs((1, 8.4, 5.3), (2, 32, 2.1), (3, 32, 2.1, True)),
    "DP822": _specs((1, 21, 5.3), (2, 5.3, 16.4)),
    "DP821": _specs((1, 63, 1.05), (2, 8.4, 10.5)),
    "DP811": _specs((1, 42, 10.5, False, True, "two ranges: 20 V/10 A or 40 V/5 A (:OUTP:RANG)")),
    "DP813": _specs((1, 21, 21, False, True, "two ranges: 8 V/20 A or 20 V/10 A (:OUTP:RANG)")),
    # DP700 Programming Guide (:APPLy parameter table).
    "DP711": _specs((1, 32, 5.3)),
    "DP712": _specs((1, 53, 3.2)),
    # DP900 Programming Guide Table 4.9.
    "DP932": _specs((1, 33.6, 3.15), (2, 33.6, 3.15), (3, 6.3, 3.15)),
    "DP932E": _specs((1, 31.5, 3.15), (2, 31.5, 3.15), (3, 6.3, 3.15)),
}

SIGLENT_MODELS: dict[str, list[ChannelSpec]] = {
    "SPD3303X": _specs((1, 32, 3.2), (2, 32, 3.2),
                       (3, 5, 3.2, False, False, "fixed 2.5/3.3/5 V set by the front-panel switch")),
    "SPD1168X": _specs((1, 16, 8)),
    "SPD1305X": _specs((1, 30, 5)),
}

TTI_MODELS: dict[str, list[ChannelSpec]] = {
    "CPX400DP": _specs((1, 60, 20, False, True, "420 W power envelope"), (2, 60, 20, False, True, "420 W power envelope")),
    "CPX400D": _specs((1, 60, 20, False, True, "420 W power envelope"), (2, 60, 20, False, True, "420 W power envelope")),
    "MX100TP": _specs((1, 35, 6), (2, 35, 6), (3, 70, 3)),
    "MX180TP": _specs((1, 120, 20), (2, 60, 10), (3, 12, 3)),
    "QL355P": _specs((1, 35, 5)),
    "QL564P": _specs((1, 56, 4)),
    "QL355TP": _specs((1, 35, 5), (2, 35, 5)),
    "QL564TP": _specs((1, 56, 4), (2, 56, 4)),
    "PL068-P": _specs((1, 6, 8)),
    "PL155-P": _specs((1, 15, 5)),
    "PL303-P": _specs((1, 30, 3)),
    "PL601-P": _specs((1, 60, 1.5)),
    "PL303QMD-P": _specs((1, 30, 3), (2, 30, 3)),
    "PL303QMT-P": _specs((1, 30, 3), (2, 30, 3), (3, 6, 8)),
}
TTI_MODELS["MX100T"] = TTI_MODELS["MX100TP"]
TTI_MODELS["MX180T"] = TTI_MODELS["MX180TP"]
TTI_MODELS["QL355T"] = TTI_MODELS["QL355TP"]
TTI_MODELS["QL564T"] = TTI_MODELS["QL564TP"]


class PowerSupply(SCPIDriver):
    """Common interface. Channel numbers are 1-based everywhere."""

    dialect = ""
    vendor = ""
    has_ovp = False
    has_ocp = False
    #: Whether OVP/OCP can be switched on/off remotely (otherwise only levels can be set).
    protection_switchable = False
    can_clear_trips = False
    #: Seconds to wait after each write (slow command parsers).
    default_write_delay_s = 0.0

    def __init__(self, transport: Transport, idn: dict[str, str], channels: list[ChannelSpec],
                 *, write_delay_s: float | None = None, notes: list[str] | None = None) -> None:
        super().__init__(transport)
        self.idn = idn
        self.channels = channels
        self.write_delay_s = self.default_write_delay_s if write_delay_s is None else write_delay_s
        self.notes = list(notes or [])

    # ------------------------------------------------------------ basics

    def write(self, command: str) -> None:
        self.t.write(command)
        if self.write_delay_s:
            time.sleep(self.write_delay_s)

    def query(self, command: str, timeout: float | None = None) -> str:
        with self.t.lock:
            reply = self.t.query(command, timeout).strip()
            if self.write_delay_s:
                time.sleep(self.write_delay_s)
        return reply

    def channel(self, number: int) -> ChannelSpec:
        for spec in self.channels:
            if spec.number == number:
                return spec
        valid = ", ".join(str(c.number) for c in self.channels)
        raise InstrumentProtocolError(f"{self.idn.get('model', 'This supply')} has no channel {number} (valid: {valid}).")

    def identify(self) -> dict[str, str]:
        out = dict(self.idn)
        out["dialect"] = self.dialect
        out["channels"] = ", ".join(
            f"CH{c.number} {'-' if c.negative else ''}{c.max_voltage_v:g} V/{c.max_current_a:g} A"
            + ("" if c.programmable else " (fixed)")
            for c in self.channels
        )
        return out

    def _set(self, command: str, context: str) -> None:
        with self.t.lock:
            self.write(command)
            self.check_errors(context)

    # ------------------------------------------------------------ per dialect

    def set_voltage(self, ch: int, volts: float) -> None:
        raise NotImplementedError

    def set_current(self, ch: int, amps: float) -> None:
        raise NotImplementedError

    def setpoints(self, ch: int) -> tuple[float | None, float | None]:
        raise NotImplementedError

    def measure(self, ch: int) -> tuple[float | None, float | None]:
        raise NotImplementedError

    def output_state(self, ch: int) -> bool | None:
        raise NotImplementedError

    def mode(self, ch: int) -> str | None:
        """'CV', 'CC' or 'UR' when the instrument reports it, else ``None``."""
        return None

    def set_output(self, ch: int, on: bool) -> None:
        raise NotImplementedError

    def all_off(self) -> list[str]:
        """Switch every output off. Returns problems (empty list = all confirmed off)."""
        problems = []
        for spec in self.channels:
            try:
                self.write(self._off_command(spec.number))
            except Exception as exc:  # keep going: this is the emergency path
                problems.append(f"CH{spec.number}: {exc}")
        for spec in self.channels:
            try:
                if self.output_state(spec.number) is True:
                    problems.append(f"CH{spec.number} still reports ON")
            except Exception as exc:
                problems.append(f"CH{spec.number}: could not read back state ({exc})")
        return problems

    def _off_command(self, ch: int) -> str:
        raise NotImplementedError

    def protection(self, ch: int) -> Protection:
        return Protection(notes=["This supply has no remotely programmable OVP/OCP."])

    def set_ovp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        raise InstrumentProtocolError(f"{self.idn.get('model')} has no remotely programmable over-voltage protection.")

    def set_ocp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        raise InstrumentProtocolError(f"{self.idn.get('model')} has no remotely programmable over-current protection.")

    def clear_trips(self, ch: int) -> str:
        raise InstrumentProtocolError(f"{self.idn.get('model')}: clearing protection trips remotely is not supported.")

    def clear_error_state(self) -> None:
        """Drain stale errors at connect so later settings are not blamed for them."""
        try:
            self.errors()
        except (InstrumentTimeout, InstrumentProtocolError):
            pass


# ---------------------------------------------------------------- Rigol


class RigolPSU(PowerSupply):
    dialect = "rigol"
    vendor = "Rigol"
    has_ovp = has_ocp = True
    protection_switchable = True
    can_clear_trips = True

    def set_voltage(self, ch: int, volts: float) -> None:
        self._set(f":SOUR{ch}:VOLT {_num(volts)}", f"set CH{ch} voltage")

    def set_current(self, ch: int, amps: float) -> None:
        self._set(f":SOUR{ch}:CURR {_num(amps)}", f"set CH{ch} current")

    def setpoints(self, ch: int) -> tuple[float | None, float | None]:
        # ":APPL? CH1" -> "CH1:8V/5A,5.000,1.0000" (DP800/DP900) or "5.00,1.00" (DP700).
        parts = self.query(f":APPL? CH{ch}").split(",")
        if len(parts) < 2:
            raise InstrumentProtocolError(f"Unexpected reply to ':APPL? CH{ch}': {','.join(parts)!r}")
        return _float(parts[-2], "voltage setpoint"), _float(parts[-1], "current setpoint")

    def measure(self, ch: int) -> tuple[float | None, float | None]:
        parts = self.query(f":MEAS:ALL? CH{ch}").split(",")  # "2.0000,0.0500,0.100"
        if len(parts) < 2:
            raise InstrumentProtocolError(f"Unexpected reply to ':MEAS:ALL? CH{ch}': {parts!r}")
        return _float(parts[0], "voltage"), _float(parts[1], "current")

    def output_state(self, ch: int) -> bool | None:
        return _bool(self.query(f":OUTP? CH{ch}"), f"CH{ch} output state")

    def mode(self, ch: int) -> str | None:
        reply = self.query(f":OUTP:MODE? CH{ch}").upper()
        return reply if reply in {"CV", "CC", "UR"} else None

    def set_output(self, ch: int, on: bool) -> None:
        self._set(f":OUTP CH{ch},{'ON' if on else 'OFF'}", f"switch CH{ch} {'on' if on else 'off'}")

    def _off_command(self, ch: int) -> str:
        return f":OUTP CH{ch},OFF"

    def protection(self, ch: int) -> Protection:
        c = f"CH{ch}"
        return Protection(
            ovp_v=_float(self.query(f":OUTP:OVP:VAL? {c}"), "OVP level"),
            ovp_enabled=_bool(self.query(f":OUTP:OVP? {c}"), "OVP state"),
            ovp_tripped=_bool(self.query(f":OUTP:OVP:QUES? {c}"), "OVP tripped"),
            ocp_a=_float(self.query(f":OUTP:OCP:VAL? {c}"), "OCP level"),
            ocp_enabled=_bool(self.query(f":OUTP:OCP? {c}"), "OCP state"),
            ocp_tripped=_bool(self.query(f":OUTP:OCP:QUES? {c}"), "OCP tripped"),
        )

    def set_ovp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        if level is not None:
            self._set(f":OUTP:OVP:VAL CH{ch},{_num(level)}", f"set CH{ch} OVP level")
        if enabled is not None:
            self._set(f":OUTP:OVP CH{ch},{'ON' if enabled else 'OFF'}", f"switch CH{ch} OVP")

    def set_ocp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        if level is not None:
            self._set(f":OUTP:OCP:VAL CH{ch},{_num(level)}", f"set CH{ch} OCP level")
        if enabled is not None:
            self._set(f":OUTP:OCP CH{ch},{'ON' if enabled else 'OFF'}", f"switch CH{ch} OCP")

    def clear_trips(self, ch: int) -> str:
        # Flag-only clear; the output stays off (unlike :SOUR:VOLT:PROT:CLE).
        self._set(f":OUTP:OVP:CLEAR CH{ch}", f"clear CH{ch} OVP")
        self._set(f":OUTP:OCP:CLEAR CH{ch}", f"clear CH{ch} OCP")
        return f"OVP/OCP trip flags cleared on CH{ch}; the output remains off until switched on."


# ---------------------------------------------------------------- Siglent


class SiglentPSU(PowerSupply):
    dialect = "siglent"
    vendor = "Siglent"
    # SPD3303X parsers are known to drop commands sent back-to-back; Siglent's own example waits.
    default_write_delay_s = 0.1

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.single = len(self.channels) == 1
        self.has_ovp = self.has_ocp = self.single  # SPD1000X only
        self.can_clear_trips = self.single

    def errors(self, max_errors: int = 5) -> list[str]:
        # "0 No Error" (SPD3303X quick start) - not the SCPI "0,..." form. Whether the reply is a
        # queue is not documented, so stop at the first repeat.
        out: list[str] = []
        for _ in range(max_errors):
            reply = self.query("SYST:ERR?")
            m = re.match(r"\s*([-+]?\d+)", reply)
            if not m or int(m.group(1)) == 0 or reply in out:
                break
            out.append(reply)
        return out

    def _set(self, command: str, context: str) -> None:
        # Settings are verified by reading them back (see the server), not via SYST:ERR?,
        # whose queue semantics are undocumented on these models.
        self.write(command)

    def _status(self) -> int:
        reply = self.query("SYST:STAT?")
        try:
            return int(reply.strip(), 16)
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unexpected reply to 'SYST:STAT?': {reply!r}") from exc

    def _programmable(self, ch: int) -> ChannelSpec:
        spec = self.channel(ch)
        if not spec.programmable:
            raise InstrumentProtocolError(f"CH{ch} is a fixed output ({spec.note}); it cannot be programmed.")
        return spec

    def set_voltage(self, ch: int, volts: float) -> None:
        self._programmable(ch)
        self._set(f"CH{ch}:VOLT {_num(volts)}", f"set CH{ch} voltage")

    def set_current(self, ch: int, amps: float) -> None:
        self._programmable(ch)
        self._set(f"CH{ch}:CURR {_num(amps)}", f"set CH{ch} current")

    def setpoints(self, ch: int) -> tuple[float | None, float | None]:
        if not self.channel(ch).programmable:
            return None, None
        return _float(self.query(f"CH{ch}:VOLT?"), "voltage setpoint"), _float(self.query(f"CH{ch}:CURR?"), "current setpoint")

    def measure(self, ch: int) -> tuple[float | None, float | None]:
        if not self.channel(ch).programmable:
            return None, None
        return (_float(self.query(f"MEAS:VOLT? CH{ch}"), "voltage"),
                _float(self.query(f"MEAS:CURR? CH{ch}"), "current"))

    def output_state(self, ch: int) -> bool | None:
        if ch > 2:
            return None  # SYST:STAT? does not report CH3
        return bool(self._status() >> (4 + ch - 1) & 1)

    def mode(self, ch: int) -> str | None:
        if ch > 2:
            return None
        return "CC" if self._status() >> (ch - 1) & 1 else "CV"

    def set_output(self, ch: int, on: bool) -> None:
        self.channel(ch)
        self._set(f"OUTP CH{ch},{'ON' if on else 'OFF'}", f"switch CH{ch} {'on' if on else 'off'}")

    def _off_command(self, ch: int) -> str:
        return f"OUTP CH{ch},OFF"

    def protection(self, ch: int) -> Protection:
        if not self.single:
            return Protection(notes=["SPD3303X/X-E have no programmable OVP/OCP (internal protection only)."])
        return Protection(
            ovp_v=_float(self.query("OVP?"), "OVP level"),
            ocp_a=_float(self.query("OCP?"), "OCP level"),
            notes=["SPD1000X protection is always active; enable state and trip status are not readable remotely."],
        )

    def set_ovp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        self._level_only("OVP", ch, level, enabled)

    def set_ocp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        self._level_only("OCP", ch, level, enabled)

    def _level_only(self, what: str, ch: int, level: float | None, enabled: bool | None) -> None:
        self.channel(ch)
        if not self.single:
            raise InstrumentProtocolError("SPD3303X/X-E have no programmable OVP/OCP.")
        if enabled is False:
            raise InstrumentProtocolError(f"The SPD1000X {what} cannot be switched off remotely; only its level can be set.")
        if level is not None:
            self._set(f"{what} {_num(level)}", f"set {what} level")

    def clear_trips(self, ch: int) -> str:
        if not self.single:
            return super().clear_trips(ch)
        self._set("OUTP:RESE:PROT", "clear protection")
        return "Protection message cleared; the output remains off until switched on."


# ---------------------------------------------------------------- Aim-TTi


_TTI_EER = {
    100: "range error: the value is not allowed",
    101: "corrupted set-up store",
    102: "empty set-up store",
    103: "output not available (e.g. tracking or parallel mode)",
    104: "command not valid with the output on",
    200: "read-only: another interface holds the lock",
}


class AimTTiPSU(PowerSupply):
    dialect = "tti"
    vendor = "Aim-TTi"
    has_ovp = has_ocp = True
    can_clear_trips = True

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        model = self.idn.get("model", "").upper()
        # MX series: OVP<n>/OCP<n> ON|OFF (MX100TP Iss. 6 p35); TRIPRST is not listed for MX100T/TP.
        self.protection_switchable = model.startswith("MX")
        self.can_clear_trips = not model.startswith("MX100")

    def errors(self, max_errors: int = 5) -> list[str]:
        out = []
        eer = int(_float(self.query("EER?"), "EER"))
        if eer:
            out.append(f"EER {eer}: {_TTI_EER.get(eer, 'execution error')}")
        qer = int(_float(self.query("QER?"), "QER"))
        if qer:
            out.append(f"QER {qer}: query error")
        esr = int(_float(self.query("*ESR?"), "*ESR"))
        if esr & 0x20:
            out.append("*ESR bit 5: command error (syntax not recognised)")
        return out

    def set_voltage(self, ch: int, volts: float) -> None:
        self._set(f"V{ch} {_num(volts)}", f"set output {ch} voltage")

    def set_current(self, ch: int, amps: float) -> None:
        self._set(f"I{ch} {_num(amps)}", f"set output {ch} current limit")

    def setpoints(self, ch: int) -> tuple[float | None, float | None]:
        # "V1 5.000" / "I1 1.000" (the header echo is followed by the value).
        return (_last_float(self.query(f"V{ch}?"), "voltage setpoint"),
                _last_float(self.query(f"I{ch}?"), "current setpoint"))

    def measure(self, ch: int) -> tuple[float | None, float | None]:
        return _float(self.query(f"V{ch}O?"), "voltage"), _float(self.query(f"I{ch}O?"), "current")

    def output_state(self, ch: int) -> bool | None:
        try:
            return _bool(self.query(f"OP{ch}?", timeout=1.0), f"output {ch} state")
        except (InstrumentTimeout, InstrumentProtocolError):
            # Original QL P/T models have no OP<n>? query: clear the command-error bit it set.
            try:
                self.query("*ESR?", timeout=1.0)
            except InstrumentTimeout:
                pass
            return None

    def set_output(self, ch: int, on: bool) -> None:
        self._set(f"OP{ch} {1 if on else 0}", f"switch output {ch} {'on' if on else 'off'}")

    def _off_command(self, ch: int) -> str:
        return f"OP{ch} 0"

    def all_off(self) -> list[str]:
        problems = []
        try:
            self.write("OPALL 0")
        except Exception as exc:
            problems.append(f"OPALL 0 failed: {exc}")
        return problems + super().all_off()

    def limit_events(self, ch: int) -> int:
        """``LSR<n>?``: latched limit/trip events since the last read (reading clears them)."""
        return int(_float(self.query(f"LSR{ch}?"), "LSR"))

    def protection(self, ch: int) -> Protection:
        def level(reply: str) -> tuple[float | None, bool | None]:
            if "OFF" in reply.upper():
                return None, False
            return _last_float(reply, "protection level"), (True if self.protection_switchable else None)

        ovp, ovp_on = level(self.query(f"OVP{ch}?"))
        ocp, ocp_on = level(self.query(f"OCP{ch}?"))
        lsr = self.limit_events(ch)
        return Protection(
            ovp_v=ovp, ovp_enabled=ovp_on, ovp_tripped=bool(lsr & 0x04),
            ocp_a=ocp, ocp_enabled=ocp_on, ocp_tripped=bool(lsr & 0x08),
            notes=["Trip flags come from LSR<n>?, which latches events since the last read and clears on reading."]
            + (["A trip that needs the front panel or an AC power cycle to reset has occurred (LSR bit 6)."]
               if lsr & 0x40 else []),
        )

    def set_ovp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        self._prot("OVP", ch, level, enabled)

    def set_ocp(self, ch: int, level: float | None, enabled: bool | None) -> None:
        self._prot("OCP", ch, level, enabled)

    def _prot(self, what: str, ch: int, level: float | None, enabled: bool | None) -> None:
        if enabled is not None and not self.protection_switchable:
            if enabled is False:
                raise InstrumentProtocolError(f"{what} on this model cannot be switched off remotely; set its level instead.")
            enabled = None  # always on
        if enabled is not None:
            self._set(f"{what}{ch} {'ON' if enabled else 'OFF'}", f"switch output {ch} {what}")
        if level is not None:
            self._set(f"{what}{ch} {_num(level)}", f"set output {ch} {what} level")

    def clear_trips(self, ch: int) -> str:
        if not self.can_clear_trips:
            return super().clear_trips(ch)
        self._set("TRIPRST", "clear trips")
        return "TRIPRST sent: trips cleared on ALL outputs (they remain off until switched on)."


# ---------------------------------------------------------------- factory

DIALECTS = {"rigol": RigolPSU, "siglent": SiglentPSU, "tti": AimTTiPSU}

_OFFICIAL = {
    "KEYSIGHT": "Keysight: use the official Keysight MCP Server for Instrument Control "
                "(https://www.keysight.com/us/en/lib/resources/software-releases/keysight-mcp-server-for-instrument-control.html)",
    "AGILENT": "Agilent/Keysight: use the official Keysight MCP Server for Instrument Control "
               "(https://www.keysight.com/us/en/lib/resources/software-releases/keysight-mcp-server-for-instrument-control.html)",
    "ROHDE": "Rohde & Schwarz: use the official MCP server in RsInstrument (https://github.com/Rohde-Schwarz/RsInstrument)",
    "HAMEG": "Rohde & Schwarz: use the official MCP server in RsInstrument (https://github.com/Rohde-Schwarz/RsInstrument)",
}


def parse_idn(reply: str) -> dict[str, str]:
    parts = [p.strip() for p in reply.split(",")]
    parts += [""] * (4 - len(parts))
    return {"manufacturer": parts[0], "model": parts[1], "serial": parts[2],
            "firmware": ",".join(p for p in parts[3:] if p)}


def detect_dialect(idn: dict[str, str]) -> str | None:
    maker = idn["manufacturer"].upper()
    if "RIGOL" in maker:
        return "rigol"
    if "SIGLENT" in maker:
        return "siglent"
    if "THURLBY" in maker or "TTI" in maker.replace("-", "") or "AIM" in maker:
        return "tti"
    return None


def _model_channels(dialect: str, model: str, channels_option: int | None) -> tuple[list[ChannelSpec], list[str]]:
    table = {"rigol": RIGOL_MODELS, "siglent": SIGLENT_MODELS, "tti": TTI_MODELS}[dialect]
    key = model.upper().replace(" ", "")
    candidates = [key, key.rstrip("AUE"), key.removesuffix("-E")]
    for cand in candidates:
        if cand in table:
            return table[cand], []
    n = channels_option or 1
    specs = [ChannelSpec(i, float("inf"), float("inf")) for i in range(1, n + 1)]
    return specs, [
        f"Model {model!r} is not in this server's model table: assuming {n} channel(s) with no model "
        "maxima (only the safety limits apply). Set --option channels=N if that is wrong."
    ]


def open_power_supply(transport: Transport, *, dialect: str = "auto", channels: int | None = None,
                      write_delay_s: float | None = None) -> PowerSupply:
    """Identify the instrument (``*IDN?``) and return the matching dialect driver."""
    idn = parse_idn(transport.query("*IDN?").strip())
    maker = idn["manufacturer"].upper()
    for key, msg in _OFFICIAL.items():
        if key in maker:
            raise InstrumentProtocolError(f"{idn['manufacturer']} {idn['model']} is not handled here. {msg}.")
    detected = detect_dialect(idn)
    notes: list[str] = []
    if dialect == "auto":
        if detected is None:
            raise InstrumentProtocolError(
                f"Unsupported power supply {idn['manufacturer']!r} {idn['model']!r} (*IDN? = {idn}). Supported: "
                "Rigol DP700/DP800/DP900, Siglent SPD3303X/SPD1000X, Aim-TTi CPX/MX/QL/PL-P. If this is a "
                "rebadged model, force a dialect with --option dialect=rigol|siglent|tti."
            )
        dialect = detected
    elif dialect not in DIALECTS:
        raise InstrumentProtocolError(f"Unknown dialect {dialect!r}; use auto, rigol, siglent or tti.")
    elif detected != dialect:
        notes.append(f"Dialect forced to {dialect!r} although *IDN? reports {idn['manufacturer']!r}.")
    specs, more = _model_channels(dialect, idn["model"], channels)
    return DIALECTS[dialect](transport, idn, specs, write_delay_s=write_delay_s, notes=notes + more)
