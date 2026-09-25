"""SCPI driver for Rigol digital oscilloscopes.

Only commands present, with the same syntax, in the programming guide of each supported family
are used. Where the families differ (autoscale command, screenshot query, number of points on
screen, probe ratios) the difference is captured in a :class:`ScopeProfile`.

References (Rigol programming guides):

* MSO1000Z/DS1000Z Series Programming Guide, PGA19109-1110 (Jul. 2018),
  https://www.bitsavers.org/test_equipment/rigol/DS1000Z/PGA19109-1110_MSO1000Z_DS1000Z_Series_Digital_Oscilloscope_Programming_Guide_201807.pdf
* DS1000Z-E Series Programming Guide, PGA27100-1110,
  https://www.batronix.com/files/Rigol/Oszilloskope/DS1000Z-E/DS1000Z-E-ProgrammingGuide.pdf
* MSO5000 Series Programming Guide, PGA25104-1110 (May 2020),
  https://www.batronix.com/files/Rigol/Oszilloskope/MSO5000/MSO5000_ProgrammingGuide_EN-V2.0.pdf
* DHO800/DHO900 Programming Guide (2024),
  https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO900/DHO800900_ProgrammingGuide_EN.pdf
* DHO1000/DHO4000 Programming Guide, PGA34101-1110,
  https://www.batronix.com/files/Rigol/Oszilloskope/DHO1000/dho10004000_programmingguide_en.pdf

Waveform data (all guides): ``:WAVeform:DATA?`` in BYTE format returns an IEEE 488.2 block
``#9<9-digit length><bytes>`` followed by ``\\n``; volts = (byte - YORigin - YREFerence) x
YINCrement and time = (i - XREFerence) x XINCrement + XORigin, with the parameters from
``:WAVeform:PREamble?``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

from labmcp import InstrumentConnectionError, InstrumentProtocolError, Transport
from labmcp.scpi import SCPIDriver

_DS_PROBES = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
_HIGH_PROBES = (2000, 5000, 10000, 20000, 50000)


@dataclass(frozen=True)
class ScopeProfile:
    family: str
    guide: str
    autoscale_command: str
    screenshot_query: str
    screenshot_format: Literal["png", "bmp"]
    #: Points returned by :WAVeform:DATA? in NORMal (screen) mode; also 100 x horizontal divisions.
    screen_points: int
    probe_ratios: tuple[float, ...]
    #: Points per :WAVeform:DATA? when reading deep memory in BYTE format. 250 000 is the
    #: documented DS1000Z maximum; the other guides allow batched reads but give no maximum.
    memory_batch_points: int = 250_000


DS1000Z = ScopeProfile(
    "DS1000Z", "MSO1000Z/DS1000Z Programming Guide PGA19109-1110 (DS1000Z-E: PGA27100-1110)",
    ":AUToscale", ":DISPlay:DATA? ON,OFF,PNG", "png", 1200, _DS_PROBES,
)
MSO5000 = ScopeProfile(
    "MSO5000", "MSO5000 Programming Guide PGA25104-1110",
    ":AUToscale", ":DISPlay:DATA?", "bmp", 1000, (0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005) + _DS_PROBES + _HIGH_PROBES,
)
DHO = ScopeProfile(
    "DHO", "DHO800/DHO900 Programming Guide; DHO1000/DHO4000 Programming Guide PGA34101-1110",
    ":AUToset", ":DISPlay:DATA? PNG", "png", 1000, (0.001, 0.002, 0.005) + _DS_PROBES + _HIGH_PROBES,
)
PROFILES = {"DS1000Z": DS1000Z, "MSO5000": MSO5000, "DHO": DHO}
_MODEL_PATTERNS = (
    (re.compile(r"^(DS|MSO)1\d{3}Z", re.IGNORECASE), DS1000Z),
    (re.compile(r"^MSO5\d{3}", re.IGNORECASE), MSO5000),
    (re.compile(r"^DHO(8|9)\d{2}", re.IGNORECASE), DHO),
    (re.compile(r"^DHO(1|4)\d{3}", re.IGNORECASE), DHO),
)

#: Single-source measurement items that have the same name in all supported guides.
MEASUREMENTS: dict[str, tuple[str, str]] = {
    "vmax": ("VMAX", "V"),
    "vmin": ("VMIN", "V"),
    "vpp": ("VPP", "V"),
    "vtop": ("VTOP", "V"),
    "vbase": ("VBASe", "V"),
    "vamp": ("VAMP", "V"),
    "vavg": ("VAVG", "V"),
    "vrms": ("VRMS", "V"),
    "overshoot": ("OVERshoot", "ratio"),
    "preshoot": ("PREShoot", "ratio"),
    "period": ("PERiod", "s"),
    "frequency": ("FREQuency", "Hz"),
    "rise_time": ("RTIMe", "s"),
    "fall_time": ("FTIMe", "s"),
    "positive_width": ("PWIDth", "s"),
    "negative_width": ("NWIDth", "s"),
    "positive_duty": ("PDUTy", "ratio"),
    "negative_duty": ("NDUTy", "ratio"),
}
#: Rigol (and SCPI) return 9.9E37 when a parameter cannot be measured.
INVALID_THRESHOLD = 9.0e37

_COUPLINGS = {"AC", "DC", "GND"}
_SLOPES = {"rising": "POSitive", "falling": "NEGative", "either": "RFALl"}
_SWEEPS = {"auto": "AUTO", "normal": "NORMal", "single": "SINGle"}


def detect_profile(model: str, override: str | None = None) -> ScopeProfile:
    if override:
        key = override.strip().upper()
        if key not in PROFILES:
            raise InstrumentConnectionError(f"Unknown profile {override!r}; use one of {', '.join(PROFILES)}.")
        return PROFILES[key]
    for pattern, profile in _MODEL_PATTERNS:
        if pattern.match(model.strip()):
            return profile
    raise InstrumentConnectionError(
        f"Model {model!r} is not a supported Rigol oscilloscope family (DS1000Z/MSO1000Z, DS1000Z-E, MSO5000, "
        "DHO800/900/1000/4000). If its programming guide matches one of them, force it with "
        "--option profile=DS1000Z|MSO5000|DHO."
    )


def channel_count(model: str) -> int:
    """Analog channels from the model number: the last digit (DS1104Z -> 4, DHO802 -> 2)."""
    m = re.search(r"(\d+)", model)
    n = int(m[1][-1]) if m else 4
    return n if n in (2, 4) else 4


@dataclass
class Preamble:
    format: int  # 0 BYTE, 1 WORD, 2 ASCii
    type: int  # 0 NORMal, 1 MAXimum, 2 RAW
    points: int
    count: int
    xincrement: float
    xorigin: float
    xreference: float
    yincrement: float
    yorigin: float
    yreference: float

    @classmethod
    def parse(cls, reply: str) -> Preamble:
        parts = [p.strip() for p in reply.split(",")]
        if len(parts) != 10:
            raise InstrumentProtocolError(f"Expected 10 comma-separated values from :WAVeform:PREamble?, got {reply!r}")
        try:
            f = [float(p) for p in parts]
        except ValueError as exc:
            raise InstrumentProtocolError(f"Unparseable :WAVeform:PREamble? reply {reply!r}") from exc
        return cls(int(f[0]), int(f[1]), int(f[2]), int(f[3]), f[4], f[5], f[6], f[7], f[8], f[9])


@dataclass
class Waveform:
    channel: int
    mode: str
    time_s: list[float]
    volts: list[float]
    preamble: Preamble
    #: Fraction of screen points at the top/bottom code (0 or 255): the trace is off-screen/clipped.
    clipped_fraction: float


def _num(value: float) -> str:
    return format(value, ".10g")


class RigolScope(SCPIDriver):
    def __init__(self, transport: Transport, profile: str | None = None) -> None:
        super().__init__(transport)
        ident = super().identify()
        if not ident["manufacturer"].upper().startswith("RIGOL"):
            raise InstrumentConnectionError(
                f"The instrument identifies as {ident['manufacturer']!r} {ident['model']!r}, not a Rigol oscilloscope."
            )
        self.ident = ident
        self.model = ident["model"]
        self.profile = detect_profile(self.model, profile)
        self.channels = channel_count(self.model)
        # Clear old errors (e.g. from front-panel use) so they are not blamed on our first command.
        self.write("*CLS")

    def identify(self) -> dict[str, str]:
        """``*IDN?`` fields plus the detected command family and channel count (cached)."""
        return {**self.ident, "family": self.profile.family, "analog_channels": str(self.channels)}

    # ------------------------------------------------------------ helpers

    def _check_channel(self, channel: int) -> None:
        if not 1 <= channel <= self.channels:
            raise InstrumentProtocolError(f"The {self.model} has analog channels 1-{self.channels}; got {channel}.")

    def send(self, *commands: str) -> None:
        """Send setting commands, then read the error queue so a rejected value is reported."""
        with self.t.lock:
            for cmd in commands:
                self.write(cmd)
            self.check_errors(context="; ".join(commands))

    # ------------------------------------------------------------ settings

    def channel_settings(self, channel: int) -> dict[str, object]:
        self._check_channel(channel)
        c = f":CHANnel{channel}"
        with self.t.lock:
            return {
                "channel": channel,
                "enabled": self.query_bool(f"{c}:DISPlay?"),
                "scale_v_per_div": self.query_float(f"{c}:SCALe?"),
                "offset_v": self.query_float(f"{c}:OFFSet?"),
                "coupling": self.query(f"{c}:COUPling?"),
                "probe_ratio": self.query_float(f"{c}:PROBe?"),
                "bandwidth_limit": self.query(f"{c}:BWLimit?"),
            }

    def timebase(self) -> dict[str, float]:
        with self.t.lock:
            return {
                "scale_s_per_div": self.query_float(":TIMebase:MAIN:SCALe?"),
                "offset_s": self.query_float(":TIMebase:MAIN:OFFSet?"),
            }

    def trigger(self) -> dict[str, object]:
        with self.t.lock:
            info: dict[str, object] = {
                "type": self.query(":TRIGger:MODE?"),
                "sweep": self.query(":TRIGger:SWEep?"),
                "status": self.trigger_status(),
            }
            if str(info["type"]).upper().startswith("EDGE"):
                info.update(
                    source=self.query(":TRIGger:EDGE:SOURce?"),
                    level_v=self.query_float(":TRIGger:EDGE:LEVel?"),
                    slope=self.query(":TRIGger:EDGE:SLOPe?"),
                )
            return info

    def acquisition(self) -> dict[str, object]:
        with self.t.lock:
            depth: int | str = self.query(":ACQuire:MDEPth?")
            try:
                depth = int(float(depth))
            except ValueError:
                pass  # "AUTO"
            return {"sample_rate_sa_s": self.query_float(":ACQuire:SRATe?"), "memory_depth": depth}

    def trigger_status(self) -> str:
        """TD (triggered), WAIT, RUN, AUTO or STOP."""
        return self.query(":TRIGger:STATus?").upper()

    # ------------------------------------------------------------ control

    def autoscale(self) -> None:
        self.write(self.profile.autoscale_command)
        self.wait_complete(timeout=15.0)
        self.check_errors(context=self.profile.autoscale_command)

    def run(self) -> None:
        self.send(":RUN")

    def stop(self) -> None:
        self.send(":STOP")

    def force_trigger(self) -> None:
        self.send(":TFORce")

    def single(self, wait_s: float = 0.0) -> str:
        """Arm a single acquisition. With ``wait_s`` > 0, poll :TRIGger:STATus? until the scope
        has stopped (acquired) or the time runs out. Returns the last status."""
        self.send(":SINGle")
        status = self.trigger_status()
        if wait_s <= 0:
            return status
        deadline = time.monotonic() + wait_s
        armed_by = time.monotonic() + min(1.0, wait_s)
        # Wait for the scope to leave the previous STOP state (arming), then for it to stop again.
        while status == "STOP" and time.monotonic() < armed_by:
            time.sleep(0.02)
            status = self.trigger_status()
        while status != "STOP" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = self.trigger_status()
        return status

    def set_channel(
        self,
        channel: int,
        enabled: bool | None = None,
        scale_v_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        probe_ratio: float | None = None,
        bandwidth_limit_20mhz: bool | None = None,
    ) -> dict[str, object]:
        self._check_channel(channel)
        c = f":CHANnel{channel}"
        commands = []
        if probe_ratio is not None:  # first: the probe ratio changes the valid scale range
            if not any(abs(probe_ratio - p) <= 1e-9 * max(1.0, p) for p in self.profile.probe_ratios):
                valid = ", ".join(f"{p:g}" for p in self.profile.probe_ratios)
                raise InstrumentProtocolError(f"Probe ratio {probe_ratio:g} is not valid on the {self.profile.family}; use one of {valid}.")
            commands.append(f"{c}:PROBe {_num(probe_ratio)}")
        if scale_v_per_div is not None:
            commands.append(f"{c}:SCALe {_num(scale_v_per_div)}")
        if offset_v is not None:
            commands.append(f"{c}:OFFSet {_num(offset_v)}")
        if coupling is not None:
            if coupling.upper() not in _COUPLINGS:
                raise InstrumentProtocolError("Coupling must be AC, DC or GND.")
            commands.append(f"{c}:COUPling {coupling.upper()}")
        if bandwidth_limit_20mhz is not None:
            commands.append(f"{c}:BWLimit {'20M' if bandwidth_limit_20mhz else 'OFF'}")
        if enabled is not None:
            commands.append(f"{c}:DISPlay {'ON' if enabled else 'OFF'}")
        if commands:
            self.send(*commands)
        return self.channel_settings(channel)

    def set_timebase(self, scale_s_per_div: float | None = None, offset_s: float | None = None) -> dict[str, float]:
        commands = []
        if scale_s_per_div is not None:
            commands.append(f":TIMebase:MAIN:SCALe {_num(scale_s_per_div)}")
        if offset_s is not None:
            commands.append(f":TIMebase:MAIN:OFFSet {_num(offset_s)}")
        if commands:
            self.send(*commands)
        return self.timebase()

    def set_edge_trigger(
        self,
        source_channel: int | None = None,
        level_v: float | None = None,
        slope: Literal["rising", "falling", "either"] | None = None,
        sweep: Literal["auto", "normal", "single"] | None = None,
    ) -> dict[str, object]:
        commands = [":TRIGger:MODE EDGE"]
        if source_channel is not None:
            self._check_channel(source_channel)
            commands.append(f":TRIGger:EDGE:SOURce CHANnel{source_channel}")
        if slope is not None:
            commands.append(f":TRIGger:EDGE:SLOPe {_SLOPES[slope]}")
        if level_v is not None:
            commands.append(f":TRIGger:EDGE:LEVel {_num(level_v)}")
        if sweep is not None:
            commands.append(f":TRIGger:SWEep {_SWEEPS[sweep]}")
        self.send(*commands)
        return self.trigger()

    # ------------------------------------------------------------ measurements

    def measure(self, channel: int, items: list[str]) -> dict[str, float | None]:
        """``:MEASure:ITEM? <item>,CHANnel<n>`` for each item; None where the scope returns 9.9E37."""
        self._check_channel(channel)
        out: dict[str, float | None] = {}
        with self.t.lock:
            for item in items:
                key = item.lower()
                if key not in MEASUREMENTS:
                    raise InstrumentProtocolError(f"Unknown measurement {item!r}; use one of {', '.join(MEASUREMENTS)}.")
                value = self.query_float(f":MEASure:ITEM? {MEASUREMENTS[key][0]},CHANnel{channel}")
                out[key] = None if abs(value) >= INVALID_THRESHOLD else value
        return out

    # ------------------------------------------------------------ waveforms

    def prepare_capture(self, channel: int, mode: Literal["screen", "memory"] = "screen") -> Preamble:
        """Select the source, mode and BYTE format and return the preamble (no data read yet)."""
        self._check_channel(channel)
        with self.t.lock:
            if not self.query_bool(f":CHANnel{channel}:DISPlay?"):
                raise InstrumentProtocolError(f"CH{channel} is switched off; enable it with set_channel first.")
            if mode == "memory":
                status = self.trigger_status()
                if status != "STOP":
                    raise InstrumentProtocolError(
                        f"Deep-memory (RAW) data can only be read while the scope is stopped (trigger status is "
                        f"{status}). Call `stop` or `single` first, or use mode='screen'."
                    )
            self.send(
                f":WAVeform:SOURce CHANnel{channel}",
                f":WAVeform:MODE {'RAW' if mode == 'memory' else 'NORMal'}",
                ":WAVeform:FORMat BYTE",
            )
            return Preamble.parse(self.query(":WAVeform:PREamble?"))

    def read_capture(self, channel: int, preamble: Preamble, mode: Literal["screen", "memory"] = "screen") -> Waveform:
        """Read the data selected by :meth:`prepare_capture` and scale it to volts and seconds."""
        if preamble.format != 0:
            raise InstrumentProtocolError(f"Expected BYTE waveform format (0), the preamble reports {preamble.format}.")
        raw = bytearray()
        with self.t.lock:
            if mode == "screen":
                total = self.profile.screen_points
                self.send(":WAVeform:STARt 1", f":WAVeform:STOP {total}")
                raw += self.query_block(":WAVeform:DATA?", timeout=10.0)
            else:
                total = preamble.points
                batch = self.profile.memory_batch_points
                for start in range(1, total + 1, batch):
                    stop = min(start + batch - 1, total)
                    self.send(f":WAVeform:STARt {start}", f":WAVeform:STOP {stop}")
                    chunk = self.query_block(":WAVeform:DATA?", timeout=60.0)
                    if len(chunk) != stop - start + 1:
                        raise InstrumentProtocolError(
                            f"Asked for points {start}-{stop} ({stop - start + 1}) but the scope returned {len(chunk)} bytes."
                        )
                    raw += chunk
        if not raw:
            raise InstrumentProtocolError("The scope returned no waveform data. Is the channel enabled?")
        p = preamble
        volts = [(b - p.yorigin - p.yreference) * p.yincrement for b in raw]
        times = [(i - p.xreference) * p.xincrement + p.xorigin for i in range(len(raw))]
        clipped = sum(1 for b in raw if b in (0, 255)) / len(raw) if mode == "screen" else 0.0
        return Waveform(channel, mode, times, volts, p, clipped)

    # ------------------------------------------------------------ screenshot

    def screenshot(self) -> tuple[bytes, str]:
        """The current display as an image (PNG, or BMP on the MSO5000)."""
        data = self.query_block(self.profile.screenshot_query, timeout=30.0)
        fmt = self.profile.screenshot_format
        magic = {"png": b"\x89PNG", "bmp": b"BM"}[fmt]
        if not data.startswith(magic):
            raise InstrumentProtocolError(f"The screenshot data does not look like a {fmt.upper()} image ({data[:8]!r}...).")
        return data, fmt
