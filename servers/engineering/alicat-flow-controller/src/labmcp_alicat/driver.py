"""Alicat ASCII serial driver for mass flow / pressure meters and controllers.

Protocol references:

* Alicat Scientific, "Serial Communications Primer", Rev. 2, February 2023
  (https://documents.alicat.com/Alicat-Serial-Primer.pdf): command set, data frame,
  status codes, statistic numbers (Appendix A) and gas numbers (Appendix C).
* Alicat Scientific, "Operating Manual for Standard Flow and Pressure Devices, Models M, MC,
  P, PC, L, LC", DOC-MANUAL-MPL Rev. 2, July 2026
  (https://documents.alicat.com/manuals/DOC-MANUAL-MPL.pdf), "Digital Control" chapter:
  19200 8N1 defaults, polling, ``AS 15.44`` / ``AG 8`` examples, status messages.
* Alicat Scientific, "Operating Manual, Gas Flow Controllers" (2020.02.05 Rev. 0,
  https://documents.alicat.com/manuals/Gas_Flow_Controller_Manual.pdf): quick command guide
  (``ahp`` / ``ahc`` / ``ac`` / ``a??m*`` / ``a??d*``) and the integer-setpoint form.

Cross-checked (not copied) against the open-source ``alicat`` (numat) and ``alicatlib``
(GraysonBellamy, MIT) packages; the latter publishes ``??D*`` / ``??M*`` / ``LS`` replies
captured on real 5v12 ... 10v20 hardware, which is where the exact column layout parsed
below comes from.

Wire format: ASCII, commands and replies terminated by CR. Every command starts with the
unit ID letter (A-Z); devices with other IDs on the same RS-485 bus stay silent. A failed
command is answered with ``?``. Commands sent to hardware that lacks the feature (e.g. a
setpoint to a meter) are silently ignored, which shows up here as a timeout.

Safety-relevant quirk: a unit ID followed directly by digits (``A49408``) is the legacy
*integer setpoint* command (64000 = full scale). :meth:`AlicatDevice.command` refuses to
send anything of that shape, so a formatting bug can never become a setpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from labmcp import InstrumentProtocolError, InstrumentTimeout, Transport

#: Status / error codes that can trail a data frame (Serial Primer p. 8; MPL manual p. 15).
STATUS_CODES: dict[str, str] = {
    "ADC": "internal analog-digital converter / communication error (contact Alicat)",
    "COM": "digital connection has been idle too long",
    "EXH": "exhaust mode active: downstream valve fully open (multi-valve controllers)",
    "GTA": "control optimisation (autotune) in progress",
    "HLD": "valve hold active: closed-loop control is bypassed",
    "LCK": "front-panel buttons are locked",
    "MOV": "mass flow over range (outside the measurable range)",
    "OPL": "overpressure limit is enabled / tripped",
    "OVR": "totalizer rolled over (or frozen at its maximum)",
    "P2O": "second pressure sensor over range",
    "POV": "pressure over range",
    "TMF": "totalizer missed flow data (during an MOV or VOV)",
    "TOV": "temperature over range",
    "VOV": "volumetric flow over range",
}

#: Statistic numbers (Serial Primer Appendix A) that are *setpoints*.
SETPOINT_STATISTICS = {34, 36, 37, 38, 39, 49, 345, 353, 361}

#: Canonical keys for the statistics that commonly appear in data frames.
STATISTIC_KEYS: dict[int, str] = {
    2: "abs_pressure",
    3: "temperature",
    4: "volumetric_flow",
    5: "mass_flow",
    6: "gauge_pressure",
    7: "diff_pressure",
    8: "total_volume",
    9: "total_mass",
    15: "barometric_pressure",
    17: "volumetric_flow_external",
    344: "abs_pressure_2",
    352: "gauge_pressure_2",
    360: "diff_pressure_2",
    700: "unit_id",
    701: "error",
    702: "status",
    703: "gas",
}

#: Field names used by the pre-6v ``??D*`` dialect (no statistic column).
_LEGACY_NAMES: dict[str, tuple[str, int | None]] = {
    "unit id": ("unit_id", 700),
    "pressure": ("abs_pressure", 2),
    "temperature": ("temperature", 3),
    "volumetric": ("volumetric_flow", 4),
    "mass": ("mass_flow", 5),
    "setpoint": ("setpoint", 37),
    "gas": ("gas", 703),
}

#: Gas numbers from Serial Primer Appendix C (the standard, non-corrosive list and a few
#: common mixes). Devices only accept gases that are installed: see ``list_gases``.
GASES: dict[int, str] = {
    0: "Air", 1: "Ar", 2: "CH4", 3: "CO", 4: "CO2", 5: "C2H6", 6: "H2", 7: "He", 8: "N2",
    9: "N2O", 10: "Ne", 11: "O2", 12: "C3H8", 13: "nC4H10", 14: "C2H2", 15: "C2H4",
    16: "iC4H10", 17: "Kr", 18: "Xe", 19: "SF6", 20: "C-25", 21: "C-10", 22: "C-8", 23: "C-2",
    24: "C-75", 25: "He-25", 26: "He-75", 27: "A1025", 28: "Star29", 29: "P-5",
    140: "C-15", 141: "C-20", 142: "C-50", 143: "He-50", 144: "He-90", 164: "EAN-32",
    165: "EAN-36", 166: "EAN-40", 206: "P-10", 210: "D-2",
}

_FIRMWARE_RE = re.compile(r"(\d+)v(\d+)")
_UNIT_ID_RE = re.compile(r"^[A-Z]$")


def parse_firmware(text: str) -> tuple[int, int] | None:
    """``"10v20.0-R24"`` -> ``(10, 20)``; ``None`` for GP or unparseable versions."""
    m = _FIRMWARE_RE.search(text or "")
    return (int(m[1]), int(m[2])) if m else None


def _num(value: float) -> str:
    """Format a number the way Alicat's examples do (``15.44``, ``-15.44``, ``0``)."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _unit_label(raw: str) -> str:
    # Alicat sends a backtick for the degree sign (e.g. "`C").
    return raw.replace("`", "°")


@dataclass
class FrameField:
    """One column of the data frame, as advertised by ``??D*``."""

    key: str
    name: str
    statistic: int | None
    kind: str  # "decimal" | "string"
    unit: str = ""
    decimals: int | None = None
    conditional: bool = False
    note: str = ""


@dataclass
class FrameLayout:
    fields: list[FrameField]
    source: str  # "??D*" | "??D* (legacy)" | "fallback"

    @property
    def required(self) -> list[FrameField]:
        return [f for f in self.fields if not f.conditional]

    def find(self, key: str) -> FrameField | None:
        return next((f for f in self.required if f.key == key), None)

    @property
    def setpoint(self) -> FrameField | None:
        return next((f for f in self.required if f.key == "setpoint"), None)

    @property
    def has_gas(self) -> bool:
        return self.find("gas") is not None


@dataclass
class FrameValue:
    key: str
    name: str
    value: float | str | None
    unit: str
    statistic: int | None


@dataclass
class DataFrame:
    unit_id: str
    values: list[FrameValue]
    status_codes: list[str]
    extra_tokens: list[str]
    raw: str
    layout_source: str

    def get(self, key: str) -> FrameValue | None:
        return next((v for v in self.values if v.key == key), None)

    def number(self, key: str) -> float | None:
        v = self.get(key)
        return v.value if v is not None and isinstance(v.value, float) else None


@dataclass
class SetpointReply:
    current: float
    requested: float
    unit: str


@dataclass
class DeviceIdentity:
    info: dict[str, str] = field(default_factory=dict)
    firmware: tuple[int, int] | None = None


class AlicatDevice:
    """One Alicat device (unit ID ``A``-``Z``) on a serial port or serial-to-Ethernet bridge."""

    def __init__(
        self,
        transport: Transport,
        unit_id: str = "A",
        *,
        timeout: float = 1.0,
        idle_timeout: float = 0.25,
    ) -> None:
        unit_id = unit_id.strip().upper()
        if not _UNIT_ID_RE.match(unit_id):
            raise ValueError(
                f"Alicat unit ID must be a single letter A-Z, got {unit_id!r}. (A device in "
                "streaming mode has ID '@'; stop streaming with '@@ A' in a terminal first.)"
            )
        self.t = transport
        self.unit_id = unit_id
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.identity = DeviceIdentity()
        self.layout: FrameLayout | None = None
        self._full_scale: dict[int, tuple[float, str] | None] = {}

    # ------------------------------------------------------------ low level

    def _encode(self, cmd: str) -> str:
        # ``A`` + digits/sign is the legacy integer-setpoint command: never send that shape.
        if cmd and (cmd[0].isdigit() or cmd[0] in "+-."):
            raise ValueError(f"Refusing to send {self.unit_id + cmd!r}: it would be read as a setpoint")
        return self.unit_id + cmd

    def _readline(self, timeout: float) -> str:
        # Replies end in CR; drop any stray LF and skip blank lines (``??D*`` ends with one).
        while True:
            line = self.t.read(timeout).strip("\r\n")
            if line.strip():
                return line

    def command(self, cmd: str, timeout: float | None = None) -> str:
        """Send ``<unit_id><cmd>`` and return the one-line reply (raises on ``?``)."""
        full = self._encode(cmd)
        with self.t.lock:
            self.t.flush_input()
            self.t.write(full)
            try:
                reply = self._readline(self.timeout if timeout is None else timeout)
            except InstrumentTimeout as exc:
                raise InstrumentTimeout(
                    f"No reply from Alicat unit {self.unit_id} to {full!r}. Check the unit ID "
                    "(`--option unit_id=B`), baud rate (factory default 19200) and cable. "
                    "Controller-only commands are silently ignored by meters/gauges, and "
                    "commands a firmware version does not know may also go unanswered."
                ) from exc
        return self._check(full, reply)

    def lines(self, cmd: str, *, first_timeout: float | None = None, max_lines: int = 300,
              stop: re.Pattern[str] | None = None) -> list[str]:
        """Send a command with a multi-line reply and collect lines until the device goes quiet."""
        full = self._encode(cmd)
        out: list[str] = []
        with self.t.lock:
            self.t.flush_input()
            self.t.write(full)
            timeout = self.timeout if first_timeout is None else first_timeout
            while len(out) < max_lines:
                try:
                    line = self._readline(timeout)
                except InstrumentTimeout:
                    break
                out.append(line)
                if stop is not None and stop.search(line):
                    break
                timeout = self.idle_timeout
        if not out:
            raise InstrumentTimeout(f"No reply from Alicat unit {self.unit_id} to {full!r}.")
        if len(out) == 1:
            self._check(full, out[0])
        return out

    def _check(self, full: str, reply: str) -> str:
        text = reply.strip()
        if text == "?" or text == f"{self.unit_id} ?":
            raise InstrumentProtocolError(
                f"Alicat replied '?' to {full!r}: the command was not accepted. The device may "
                "not support it (check the firmware version / hardware option), or a value is "
                "out of range."
            )
        return reply

    # ------------------------------------------------------------ identity & layout

    def initialise(self) -> None:
        """Check that the device answers, then read its identity and data-frame layout."""
        frame = self.command("")
        if not frame.strip().startswith(self.unit_id):
            raise InstrumentProtocolError(
                f"Unexpected reply to poll {self.unit_id!r}: {frame!r}. If the device is "
                "streaming (unit ID '@'), stop it first by sending '@@ A'."
            )
        self.identity = self._read_identity()
        self.layout = self._read_layout()

    def _read_identity(self) -> DeviceIdentity:
        info: dict[str, str] = {"manufacturer": "Alicat Scientific", "unit_id": self.unit_id}
        labels = {
            "Model Number": "model",
            "Serial Number": "serial",
            "Date Manufactured": "manufactured",
            "Date Calibrated": "calibrated",
            "Calibrated By": "calibrated_by",
            "Software Revision": "firmware",
        }
        try:
            for line in self.lines("??M*", stop=re.compile(r"\bM09\b")):
                m = re.search(r"\bM(\d\d)\s+(.*)$", line)
                if not m:
                    continue
                text = m[2].strip()
                for label, key in labels.items():
                    if text.startswith(label):
                        info[key] = text[len(label):].strip()
        except (InstrumentProtocolError, InstrumentTimeout):
            pass
        if "firmware" not in info:
            try:  # VE: "A   10v20.0-R24 Jan  9 2025,15:04:07"
                parts = self.command("VE").split()
                if len(parts) >= 2:
                    info["firmware"] = parts[1]
            except (InstrumentProtocolError, InstrumentTimeout):
                pass
        firmware = parse_firmware(info.get("firmware", ""))
        if info.get("firmware", "").upper().startswith("GP"):
            info["warning"] = (
                "GP firmware needs '$$' after the unit ID for most commands; this server "
                "does not support GP devices beyond polling."
            )
        return DeviceIdentity(info, firmware)

    def _read_layout(self) -> FrameLayout | None:
        try:
            lines = self.lines("??D*")
        except (InstrumentProtocolError, InstrumentTimeout):
            return None
        return parse_layout(lines)

    def identify(self) -> dict[str, str]:
        out = dict(self.identity.info)
        out["data_frame_layout"] = self.layout.source if self.layout else "fallback (no ??D*)"
        return out

    def firmware_at_least(self, major: int, minor: int = 0) -> bool:
        fw = self.identity.firmware
        return fw is not None and fw >= (major, minor)

    # ------------------------------------------------------------ capabilities

    @property
    def is_controller(self) -> bool:
        if self.layout is not None:
            return self.layout.setpoint is not None
        model = self.identity.info.get("model", "")
        base = model.split("-")[0].upper()
        return "C" in base[1:] if base else False

    @property
    def has_gas_select(self) -> bool:
        if self.layout is not None:
            return self.layout.has_gas
        return True  # unknown: let the device decide

    # ------------------------------------------------------------ data

    def poll(self) -> DataFrame:
        reply = self.command("")
        frame = parse_frame(reply, self.layout, self.unit_id, self.is_controller)
        if frame is None and self.layout is not None:
            # The layout changed under us (e.g. FDF / DCU from the front panel): re-read it.
            self.layout = self._read_layout()
            frame = parse_frame(reply, self.layout, self.unit_id, self.is_controller)
        if frame is None:
            frame = parse_frame(reply, None, self.unit_id, self.is_controller)
        assert frame is not None
        return frame

    def _frame_reply(self, cmd: str, timeout: float | None = None) -> DataFrame:
        reply = self.command(cmd, timeout)
        frame = parse_frame(reply, self.layout, self.unit_id, self.is_controller)
        return frame or parse_frame(reply, None, self.unit_id, self.is_controller)  # type: ignore[return-value]

    def full_scale(self, statistic: int) -> tuple[float, str] | None:
        """``FPF <stat>`` (6v00+): ``A +500.00 12 SCCM`` -> ``(500.0, "SCCM")``. Cached."""
        if statistic not in self._full_scale:
            result = None
            try:
                parts = self.command(f"FPF {statistic}").split()
                result = (abs(float(parts[1])), _unit_label(parts[-1]) if len(parts) >= 4 else "")
            except (InstrumentProtocolError, InstrumentTimeout, IndexError, ValueError):
                result = None
            self._full_scale[statistic] = result
        return self._full_scale[statistic]

    def setpoint_source(self) -> str | None:
        """``LSS`` (10v05+): ``A S`` / ``A U`` (serial/front panel) or ``A A`` (analog)."""
        if not self.firmware_at_least(10, 5):
            return None
        try:
            parts = self.command("LSS").split()
        except (InstrumentProtocolError, InstrumentTimeout):
            return None
        return parts[1] if len(parts) >= 2 else None

    def gases(self) -> list[tuple[int, str]]:
        """``??G*``: gases installed on this device, e.g. ``A G08      N2``."""
        out = []
        for line in self.lines("??G*"):
            m = re.search(r"\bG(\d+)\s+(\S.*?)\s*$", line)
            if m:
                out.append((int(m[1]), m[2]))
        if not out:
            raise InstrumentProtocolError(f"Unexpected reply to '??G*': {out!r}")
        return out

    # ------------------------------------------------------------ control

    def set_setpoint(self, value: float) -> SetpointReply:
        """Query/change setpoint: ``LS`` on 9v00+, else the older ``S`` (4v33+)."""
        if self.identity.firmware is None or self.firmware_at_least(9, 0):
            try:
                return self._ls(f"LS {_num(value)}")
            except InstrumentProtocolError:
                if self.identity.firmware is not None:
                    raise
        frame = self._frame_reply(f"S {_num(value)}")
        sp = frame.get("setpoint")
        if sp is None or not isinstance(sp.value, float):
            raise InstrumentProtocolError(f"Setpoint reply has no setpoint field: {frame.raw!r}")
        return SetpointReply(sp.value, sp.value, sp.unit)

    def get_setpoint(self) -> SetpointReply | None:
        if not self.firmware_at_least(9, 0):
            return None
        return self._ls("LS")

    def _ls(self, cmd: str) -> SetpointReply:
        # "A +078.94 +078.94 12 SCCM": unit ID, current, requested, unit code, unit label.
        reply = self.command(cmd)
        parts = reply.split()
        try:
            return SetpointReply(float(parts[1]), float(parts[2]), _unit_label(parts[4]) if len(parts) > 4 else "")
        except (IndexError, ValueError) as exc:
            raise InstrumentProtocolError(f"Unexpected reply to {self.unit_id + cmd!r}: {reply!r}") from exc

    def set_gas(self, number: int, save: bool = False) -> tuple[int, str]:
        """Select a gas: ``GS n save`` on 10v05+ (reply ``A 8 N2 Nitrogen``), else ``G n``."""
        if self.firmware_at_least(10, 5):
            reply = self.command(f"GS {number} {1 if save else 0}")
            parts = reply.split()
            try:
                got = int(parts[1])
            except (IndexError, ValueError) as exc:
                raise InstrumentProtocolError(f"Unexpected reply to 'GS': {reply!r}") from exc
            name = parts[2] if len(parts) > 2 else ""
        else:
            frame = self._frame_reply(f"G {number}")
            gas = frame.get("gas")
            name = str(gas.value) if gas is not None else ""
            expected = GASES.get(number)
            got = number if expected is None or name == expected else -1
        if got != number:
            raise InstrumentProtocolError(
                f"The device did not switch to gas {number} (reports {name!r}); is that gas installed? "
                "Use `list_gases` to see what this device supports."
            )
        return got, name

    def tare_flow(self) -> DataFrame:
        return self._frame_reply("V", timeout=3.0)  # a tare takes a moment

    def tare_gauge_pressure(self) -> DataFrame:
        return self._frame_reply("P", timeout=3.0)  # a tare takes a moment

    def tare_absolute_pressure(self) -> DataFrame:
        return self._frame_reply("PC", timeout=3.0)  # a tare takes a moment

    def hold_position(self) -> DataFrame:
        return self._frame_reply("HP")

    def hold_closed(self) -> DataFrame:
        return self._frame_reply("HC")

    def cancel_hold(self) -> DataFrame:
        return self._frame_reply("C")

    def close(self) -> None:
        self.t.close()


# ---------------------------------------------------------------- parsing


def parse_layout(lines: list[str]) -> FrameLayout | None:
    """Parse a ``??D*`` table (6v+ canonical dialect or the pre-6v legacy dialect)."""
    header_i = next((i for i, ln in enumerate(lines) if "D00" in ln and "NAME" in ln), None)
    if header_i is None:
        return None
    header = lines[header_i]
    rows = [ln for ln in lines[header_i + 1 :] if re.search(r"\bD\d\d\b", ln)]
    if "ID_" in header:
        fields = [_canonical_row(header, row) for row in rows]
        source = "??D*"
    else:
        fields = [_legacy_row(row) for row in rows]
        source = "??D* (legacy)"
    parsed = [f for f in fields if f is not None]
    if not parsed or parsed[0].key != "unit_id":
        return None
    return FrameLayout(parsed, source)


def _columns(header: str) -> dict[str, int]:
    return {name: header.index(name) for name in ("ID_", "NAME", "TYPE", "WIDTH", "NOTES") if name in header}


def _canonical_row(header: str, row: str) -> FrameField | None:
    # "A D02 002 Abs Press                  s decimal     7/2 010 02 PSIA"
    cols = _columns(header)
    if not {"ID_", "NAME", "TYPE", "WIDTH"} <= cols.keys():
        return None
    code_txt = row[cols["ID_"] : cols["NAME"]].strip()
    name = row[cols["NAME"] : cols["TYPE"]].strip()
    type_txt = row[cols["TYPE"] : cols["WIDTH"]].strip().lower()
    rest = row[cols["WIDTH"] :].split()
    if not code_txt.isdigit() or not name:
        return None
    code = int(code_txt)
    conditional = name.startswith("*")
    name = name.lstrip("*").strip()
    kind = "decimal" if "decimal" in type_txt else "string"
    decimals = None
    if rest and "/" in rest[0]:
        try:
            decimals = int(rest[0].split("/")[1])
        except ValueError:
            decimals = None
    notes = rest[1:]
    unit = _unit_label(notes[-1]) if kind == "decimal" and notes else ""
    key = "setpoint" if code in SETPOINT_STATISTICS else STATISTIC_KEYS.get(code, _slug(name))
    return FrameField(key, name, code, kind, unit, decimals if kind == "decimal" else None,
                      conditional, " ".join(notes) if conditional else "")


def _legacy_row(row: str) -> FrameField | None:
    # "A  D02 Pressure    signed    +000.00  +160.00     PSIA"
    m = re.search(r"\bD\d\d\s+(.*)$", row)
    if not m:
        return None
    tokens = m[1].split()
    type_i = next((i for i, t in enumerate(tokens) if t.lower() in {"signed", "unsigned", "char", "string"}), None)
    if type_i is None or type_i == 0:
        return None
    name = " ".join(tokens[:type_i])
    kind = "decimal" if tokens[type_i].lower() in {"signed", "unsigned"} else "string"
    unit = tokens[-1] if kind == "decimal" and tokens[-1].lower() != "na" else ""
    lname = name.lower()
    conditional = lname in {"error", "status"}
    key, code = _LEGACY_NAMES.get(lname, (_slug(name), None))
    note = tokens[-2] if conditional and len(tokens) >= 2 else ""
    return FrameField(key, name, code, kind, _unit_label(unit), None, conditional, note)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "field"


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_frame(
    reply: str, layout: FrameLayout | None, unit_id: str, is_controller: bool = False
) -> DataFrame | None:
    """Parse a polled data frame. Returns ``None`` if it does not match ``layout``."""
    tokens = reply.split()
    if not tokens or tokens[0] != unit_id:
        raise InstrumentProtocolError(
            f"Unexpected data frame from unit {unit_id}: {reply!r} (expected it to start with "
            f"'{unit_id}'). Another device may be streaming on this port."
        )
    if layout is not None:
        required = layout.required
        if len(tokens) < len(required):
            return None
        values = []
        for fld, tok in zip(required[1:], tokens[1 : len(required)], strict=True):
            if fld.kind == "decimal":
                num = _to_float(tok)
                if num is None and not set(tok) <= set("-+."):
                    return None
                values.append(FrameValue(fld.key, fld.name, num, fld.unit, fld.statistic))
            else:
                values.append(FrameValue(fld.key, fld.name, tok, "", fld.statistic))
        trailing = tokens[len(required) :]
        source = layout.source
    else:
        values, trailing = _fallback_values(tokens[1:], is_controller)
        source = "fallback"
    status = [t for t in trailing if t.upper() in STATUS_CODES]
    extra = [t for t in trailing if t.upper() not in STATUS_CODES]
    return DataFrame(unit_id, values, [s.upper() for s in status], extra, reply.strip(), source)


def _fallback_values(tokens: list[str], is_controller: bool) -> tuple[list[FrameValue], list[str]]:
    """Positional parse for devices that do not answer ``??D*``.

    Default gas-flow frames are ``P T VolFlow MassFlow [Setpoint] [Total] Gas [status...]``
    (Serial Primer p. 8; MPL manual p. 49). Other layouts are returned unlabelled.
    """
    trailing: list[str] = []
    while tokens and tokens[-1].upper() in STATUS_CODES:
        trailing.insert(0, tokens.pop())
    gas = None
    if tokens and _to_float(tokens[-1]) is None:
        gas = tokens.pop()
    nums = [_to_float(t) for t in tokens]
    if gas is not None and 4 <= len(nums) <= 6:
        keys = ["abs_pressure", "temperature", "volumetric_flow", "mass_flow"]
        extra = {5: ["setpoint"] if is_controller else ["total"], 6: ["setpoint", "total"]}
        keys += extra.get(len(nums), [])
        values = [FrameValue(k, k.replace("_", " "), n, "", None) for k, n in zip(keys, nums, strict=True)]
    else:
        values = [FrameValue(f"value_{i + 1}", f"value {i + 1}", n, "", None) for i, n in enumerate(nums)]
    if gas is not None:
        values.append(FrameValue("gas", "gas", gas, "", 703))
    return values, trailing
