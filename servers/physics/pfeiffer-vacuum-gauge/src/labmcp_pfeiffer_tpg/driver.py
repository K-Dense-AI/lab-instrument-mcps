"""Driver for Pfeiffer Vacuum total-pressure gauge controllers (mnemonics protocol).

Supported controllers and the documents the commands were verified against:

* **TPG 361 / TPG 362** (SingleGauge / DualGauge, ActiveLine): "Communication Protocol TPG 361,
  TPG 362", BG 5510 BEN / D (2018-08), and operating instructions BG 5500 BEN (interfaces, Ethernet
  port 8000). Copies: https://www.ajvs.com/library/Pfeiffer_Operating_instructions_TPG361_TPG362_SingleGauge_DualGauge_Controller_Communication_Protocol.pdf
  and https://www.ajvs.com/library/Manual_Instructions_TPG361_TPG362_SingleGauge_DualGauge_Controller.pdf
* **TPG 366** (MaxiGauge, 6 channels): "Communication Protocol TPG 366", BG 5511 BEN (2018-08), and
  operating instructions BG 5501 BEN. Copies: https://www.ajvs.com/library/TPG_366_Communication_Protocol_BG5511BEN.pdf
  and https://www.ajvs.com/library/TPG_366_Gauge_Controller_BG5501BEN_A.pdf
* **TPG 261 / TPG 262** (SingleGauge / DualGauge, compact gauges): operating instructions
  BG 805 195 BE / B (TPG 261) and BG 805 196 BE / B (2004-08, TPG 262), section 5 "RS232C Interface".
  Copies: https://www.idealvac.com/files/ManualsII/Pfeiffer_Single_Gauge_TPG261.pdf and
  https://www.idealvac.com/files/ManualsII/Pfeiffer_TPG262_Operating_Instructions.pdf

The originals are in the Pfeiffer Vacuum download center (https://www.pfeiffer-vacuum.com); the
copies above are distributor mirrors of the same document numbers.

All share the same framing. The host sends a three-letter mnemonic (plus ``,``-separated
parameters) ending in CR; the controller answers ``<ACK><CR><LF>`` or, on a transmission or
programming error, ``<NAK><CR><LF>``. The host then sends ``<ENQ>`` (0x05) to fetch the data line.
After a NAK, ``<ENQ>`` returns the ERROR word, which this driver decodes. ``<ETX>`` (0x03) clears the
controller's input buffer. On power-up the controllers stream measurements every second until the
first character arrives, so :meth:`TPGController.__init__` sends ETX and discards anything pending.

Channel-specific commands (e.g. ``SEN``) must carry exactly one value per channel of the device:
1 (TPG 361), 2 (TPG 362 and TPG 261/262) or 6 (TPG 366).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from labmcp import InstrumentProtocolError, Transport

ETX = b"\x03"
ENQ = b"\x05"
ACK = "\x06"
NAK = "\x15"

#: PRn / PRX status digit.
MEASUREMENT_STATUS = {
    0: "ok",
    1: "underrange",
    2: "overrange",
    3: "sensor error",
    4: "sensor off",
    5: "no sensor",
    6: "identification error",
}
#: ERROR word ``aaaa``: one digit per condition, left to right.
ERROR_WORD = (
    "ERROR: controller error (see the front-panel display)",
    "NO HWR: no hardware",
    "PAR: inadmissible parameter",
    "SYN: syntax error",
)
UNITS_36X = {0: "mbar", 1: "Torr", 2: "Pa", 3: "micron", 4: "hPa", 5: "V"}
UNITS_26X = {0: "mbar", 1: "Torr", 2: "Pa"}
MBAR_PER_UNIT = {"mbar": 1.0, "hPa": 1.0, "Pa": 0.01, "Torr": 1.333224, "micron": 1.333224e-3}
SEN_STATUS = {0: "cannot be switched", 1: "off", 2: "on"}

# Gauge identifiers (TID) -> (description, kind, ionisation gauge switched by SEN?)
_GAUGES = {
    "TPR": ("Pirani gauge", "pirani", False),
    "PCR": ("Pirani/capacitance gauge", "pirani", False),
    "IKR": ("cold cathode (Penning) gauge", "cold_cathode", True),
    "PKR": ("FullRange Pirani/cold cathode gauge", "pirani_cold_cathode", True),
    "PBR": ("FullRange Pirani/Bayard-Alpert hot cathode gauge", "pirani_hot_cathode", True),
    "IMR": ("Pirani/high-pressure hot cathode gauge", "pirani_hot_cathode", True),
    "CMR": ("capacitance diaphragm gauge (linear)", "linear", False),
    "APR": ("piezo gauge (linear)", "linear", False),
}


@dataclass(frozen=True)
class ModelSpec:
    model: str
    family: str  # "36x" | "26x"
    channels: int  # values in channel-specific commands


MODELS = {
    "TPG361": ModelSpec("TPG361", "36x", 1),
    "TPG362": ModelSpec("TPG362", "36x", 2),
    "TPG366": ModelSpec("TPG366", "36x", 6),
    "TPG261": ModelSpec("TPG261", "26x", 2),
    "TPG262": ModelSpec("TPG262", "26x", 2),
}


@dataclass
class Pressure:
    channel: int
    status_code: int
    status: str
    raw_value: float
    unit: str

    @property
    def valid(self) -> bool:
        return self.status_code == 0

    @property
    def value(self) -> float | None:
        return self.raw_value if self.valid else None

    @property
    def mbar(self) -> float | None:
        factor = MBAR_PER_UNIT.get(self.unit)
        return None if self.value is None or factor is None else self.value * factor


def describe_gauge(ident: str) -> tuple[str, str, bool]:
    """(description, kind, is_ionisation_gauge) for a TID identifier."""
    key = ident.strip().upper()
    if key.startswith("NOSEN"):
        return "no gauge connected", "none", False
    if key.startswith("NOID"):
        return "gauge not identified", "unknown", False
    for prefix, info in _GAUGES.items():
        if key.startswith(prefix):
            return info
    return f"unknown gauge {ident!r}", "unknown", False


def decode_error_word(word: str) -> list[str]:
    word = word.strip()
    if len(word) != 4 or not set(word) <= {"0", "1"}:
        raise InstrumentProtocolError(f"Unexpected ERROR word from the gauge controller: {word!r}")
    return [text for digit, text in zip(word, ERROR_WORD, strict=True) if digit == "1"]


def _parse_pressure(channel: int, status: str, value: str, unit: str) -> Pressure:
    try:
        code, raw = int(status), float(value)
    except ValueError as exc:
        raise InstrumentProtocolError(
            f"Unexpected measurement {status},{value} for channel {channel}"
        ) from exc
    return Pressure(channel, code, MEASUREMENT_STATUS.get(code, f"status {code}"), raw, unit)


class TPGController:
    """Driver for TPG 361/362/366 and TPG 261/262 controllers.

    Args:
        transport: An open transport (USB/RS-232 serial, TCP port 8000, or simulated).
        model: ``"auto"`` or one of ``tpg361``, ``tpg362``, ``tpg366``, ``tpg261``, ``tpg262``.
            Auto-detection uses ``AYT`` (TPG 36x); controllers that reject it are treated as TPG 26x.
        settle_s: Time to wait after ETX before discarding stale data.
    """

    def __init__(self, transport: Transport, *, model: str = "auto", settle_s: float = 0.2) -> None:
        self.t = transport
        wanted = model.strip().upper().replace(" ", "")
        if wanted != "AUTO" and wanted not in MODELS:
            raise InstrumentProtocolError(
                f"Unknown model option {model!r}; use auto, tpg361, tpg362, tpg366, tpg261 or tpg262."
            )
        self._ayt: list[str] = []
        self._family_guess = False
        with self.t.lock:
            self.t.write_bytes(ETX)  # stops the power-up measurement stream and clears the input buffer
            time.sleep(settle_s)
            self.t.flush_input()
            try:
                self._ayt = [p.strip() for p in self.query("AYT").split(",")]
            except InstrumentProtocolError:
                self._ayt = []  # TPG 26x: AYT is not a TPG 26x mnemonic
            if wanted != "AUTO":
                self.spec = MODELS[wanted]
            elif self._ayt and self._ayt[0].upper().replace(" ", "") in MODELS:
                self.spec = MODELS[self._ayt[0].upper().replace(" ", "")]
            elif self._ayt:
                raise InstrumentProtocolError(
                    f"Connected to {self._ayt[0]!r}, which this server does not know. Restart with "
                    "`--option model=tpg361|tpg362|tpg366` if it uses the same protocol."
                )
            else:
                self.spec = MODELS["TPG262"]
                self._family_guess = True
            self._firmware = self._ayt[3] if len(self._ayt) > 3 else self.query("PNR")

    # ------------------------------------------------------------ low level

    def _read_report(self, cmd: str) -> str:
        for _ in range(8):  # skip stale continuous-mode lines, if any
            line = self.t.read().strip()
            if line in (ACK, NAK):
                return line
        raise InstrumentProtocolError(f"No ACK/NAK from the gauge controller after {cmd!r}.")

    def enquire(self) -> str:
        """Send ``<ENQ>`` and return the data line."""
        with self.t.lock:
            self.t.write_bytes(ENQ)
            return self.t.read().strip()

    def send(self, cmd: str) -> None:
        """Send a mnemonic and require ``<ACK>``; on ``<NAK>`` read and decode the ERROR word."""
        with self.t.lock:
            self.t.write(cmd)
            if self._read_report(cmd) == NAK:
                problems = decode_error_word(self.enquire())
                raise InstrumentProtocolError(
                    f"Gauge controller rejected {cmd!r} (NAK): {'; '.join(problems) or 'no error flag set'}."
                )

    def query(self, cmd: str) -> str:
        with self.t.lock:
            self.send(cmd)
            return self.enquire()

    # ------------------------------------------------------------ identity

    @property
    def channels(self) -> int:
        return self.spec.channels

    @property
    def units_table(self) -> dict[int, str]:
        return UNITS_36X if self.spec.family == "36x" else UNITS_26X

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "Pfeiffer Vacuum", "model": self.spec.model, "firmware": self._firmware}
        if len(self._ayt) >= 5:
            info.update(part_number=self._ayt[1], serial=self._ayt[2], hardware=self._ayt[4])
        elif self._family_guess:
            info["model"] = "TPG 261/262"
            info["note"] = (
                "TPG 26x family detected (no AYT); use --option model=tpg261 or tpg262 to be explicit."
            )
        return info

    def _check_channel(self, channel: int) -> int:
        if not 1 <= channel <= self.channels:
            raise InstrumentProtocolError(
                f"The {self.spec.model} has channels 1-{self.channels}; got {channel}."
            )
        return channel

    # ------------------------------------------------------------ measurement

    def unit(self) -> str:
        code = int(self.query("UNI"))
        return self.units_table.get(code, f"unit {code}")

    def set_unit(self, unit: str) -> str:
        codes = {v: k for k, v in self.units_table.items()}
        if unit not in codes:
            raise InstrumentProtocolError(
                f"The {self.spec.model} supports the units {', '.join(codes)}; got {unit!r}."
            )
        reply = self.query(f"UNI,{codes[unit]}")
        return self.units_table.get(int(reply), reply)

    def pressure(self, channel: int) -> Pressure:
        self._check_channel(channel)
        with self.t.lock:
            unit = self.unit()
            reply = self.query(f"PR{channel}")
        parts = reply.split(",")
        if len(parts) != 2:
            raise InstrumentProtocolError(f"Unexpected reply to PR{channel}: {reply!r}")
        return _parse_pressure(channel, parts[0], parts[1], unit)

    def pressures(self) -> list[Pressure]:
        if self.channels == 1:
            return [self.pressure(1)]
        with self.t.lock:
            unit = self.unit()
            reply = self.query("PRX")
        parts = [p.strip() for p in reply.split(",")]
        if len(parts) != 2 * self.channels:
            raise InstrumentProtocolError(f"Expected {self.channels} channels from PRX, got {reply!r}")
        return [_parse_pressure(i + 1, parts[2 * i], parts[2 * i + 1], unit) for i in range(self.channels)]

    # ------------------------------------------------------------ gauges

    def gauge_ids(self) -> list[str]:
        ids = [p.strip() for p in self.query("TID").split(",")]
        if len(ids) != self.channels:
            raise InstrumentProtocolError(f"Expected {self.channels} gauge identifiers from TID, got {ids}")
        return ids

    def sensor_states(self) -> list[int]:
        return [int(v) for v in self.query("SEN").split(",")]

    def set_sensor(self, channel: int, on: bool) -> list[int]:
        """``SEN`` with 2 (on) or 1 (off) for ``channel`` and 0 (no change) for the others."""
        self._check_channel(channel)
        values = ["0"] * self.channels
        values[channel - 1] = "2" if on else "1"
        return [int(v) for v in self.query("SEN," + ",".join(values)).split(",")]

    def errors(self) -> list[str]:
        return decode_error_word(self.query("ERR"))

    def close(self) -> None:
        self.t.close()
