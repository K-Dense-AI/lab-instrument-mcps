"""Wire-level SCPI simulator of a Rigol oscilloscope (DS1000Z, MSO5000 or DHO family).

It is a :class:`~labmcp.ByteSimulator` so it can answer ``:WAVeform:DATA?`` and ``:DISPlay:DATA?``
with real IEEE 488.2 binary blocks (``#9<length><bytes>\\n``) - the driver's block parser, BYTE
decoding with the ``:WAVeform:PREamble?`` scaling and PNG/BMP checks run exactly as on hardware.
Text commands go through :class:`~labmcp.scpi.SCPISimulator` (error queue, ``*IDN?``, ``*OPC?``).

Signals at the probe tips: CH1 1 kHz square 0-3 V (1 µs edges, like a probe-compensation
output); CH2 2 kHz sine, 1 V amplitude; CH3/CH4 grounded (noise only). The trigger locks to the
source channel when the level is inside its signal range, otherwise NORMal/SINGle sweeps wait.
"""

from __future__ import annotations

import math
import random
import re
import struct
import time
import zlib

from labmcp import ByteSimulator
from labmcp.scpi import SCPISimulator

_SQUARE_TAU_S = 1e-6
_DS_PROBES = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
_COLORS = {1: (255, 230, 0), 2: (0, 220, 255), 3: (255, 0, 200), 4: (60, 120, 255)}
_INVALID = 9.9e37


def _family(model: str) -> str:
    if re.match(r"^(DS|MSO)1\d{3}Z", model, re.IGNORECASE):
        return "DS1000Z"
    if re.match(r"^MSO5\d{3}", model, re.IGNORECASE):
        return "MSO5000"
    if re.match(r"^DHO\d{3,4}", model, re.IGNORECASE):
        return "DHO"
    raise ValueError(f"The simulator does not know model {model!r}")


def _snap125(x: float) -> float:
    """Nearest value of the 1-2-5 sequence (in log space)."""
    exp = math.floor(math.log10(x))
    candidates = [m * 10.0**e for e in (exp - 1, exp, exp + 1) for m in (1, 2, 5)]
    return min(candidates, key=lambda c: abs(math.log10(c) - math.log10(x)))


class RigolScopeSimulator(SCPISimulator, ByteSimulator):
    def __init__(self, model: str = "DS1104Z", seed: int | None = 0) -> None:
        SCPISimulator.__init__(self)
        self.model = model
        self.family = _family(model)
        serial = {"DS1000Z": "DS1ZA203500001", "MSO5000": "MSO5A211000001", "DHO": "DHO8A240100001"}[self.family]
        firmware = {"DS1000Z": "00.04.05.SP2", "MSO5000": "00.01.03.00.03", "DHO": "00.01.02.00.08"}[self.family]
        self.idn = f"RIGOL TECHNOLOGIES,{model},{serial},{firmware}"
        digits = re.search(r"(\d+)", model)
        self.n_ch = int(digits[1][-1]) if digits and digits[1][-1] in "24" else 4
        self.screen_points = 1200 if self.family == "DS1000Z" else 1000
        self.hdiv = self.screen_points / 100
        self.yref = 127 if self.family == "DS1000Z" else 128
        if self.family == "DS1000Z":
            self.probes, self.physical_probe = _DS_PROBES, 10.0
        else:
            extra = (0.0001, 0.0002, 0.0005) if self.family == "MSO5000" else ()
            self.probes = extra + (0.001, 0.002, 0.005) + _DS_PROBES + (2000, 5000, 10000, 20000, 50000)
            self.physical_probe = 1.0
        self.min_scale = 1e-3 if self.family == "DS1000Z" else 5e-4
        self.rng = random.Random(seed)
        self._buf = b""
        self.reset()

    # ------------------------------------------------------------ state

    def reset(self) -> None:
        self.ch = {
            n: {"disp": n <= 2, "scale": 1.0 if n != 2 else 0.5, "offset": 0.0, "coup": "DC",
                "probe": self.physical_probe, "bw": "OFF"}
            for n in range(1, self.n_ch + 1)
        }
        self.tscale = 200e-6
        self.toffset = 0.0
        self.trig = {"mode": "EDGE", "source": 1, "level": 1.5, "slope": "POS", "sweep": "AUTO"}
        self.running = True
        self.single_armed_at: float | None = None
        self.frozen_seed: int | None = None
        self.wav = {"source": 1, "mode": "NORM", "format": "BYTE", "start": 1, "stop": self.screen_points}

    def _fmt(self, value: float) -> str:
        return f"{value:.6e}" if self.family == "DS1000Z" else f"{value:.6E}"

    # ------------------------------------------------------------ physics

    def _tip(self, n: int, t: float) -> float:
        if n == 1:
            period, half = 1e-3, 0.5e-3
            phase = t % period
            if phase < half:
                return 3.0 * (1 - math.exp(-phase / _SQUARE_TAU_S))
            return 3.0 * math.exp(-(phase - half) / _SQUARE_TAU_S)
        if n == 2:
            return math.sin(2 * math.pi * 2000.0 * t + 0.3)
        return 0.0

    def _dc(self, n: int) -> float:
        return 1.5 if n == 1 else 0.0

    def _gain(self, n: int) -> float:
        return self.ch[n]["probe"] / self.physical_probe  # displayed / actual, with the physical probe fixed

    def displayed(self, n: int, t: float, rng: random.Random) -> float:
        coupling = self.ch[n]["coup"]
        if coupling == "GND":
            return 0.0
        v = self._tip(n, t) + rng.gauss(0.0, 3e-3)
        if coupling == "AC":
            v -= self._dc(n)
        return v * self._gain(n)

    def _range(self, n: int) -> tuple[float, float]:
        """Displayed min/max of the (noise-free) signal."""
        lo, hi = {1: (0.0, 3.0), 2: (-1.0, 1.0)}.get(n, (0.0, 0.0))
        if self.ch[n]["coup"] == "GND":
            return 0.0, 0.0
        if self.ch[n]["coup"] == "AC":
            lo, hi = lo - self._dc(n), hi - self._dc(n)
        g = self._gain(n)
        return lo * g, hi * g

    def triggerable(self) -> bool:
        lo, hi = self._range(self.trig["source"])
        return lo < self.trig["level"] < hi

    def n_enabled(self) -> int:
        return max(1, sum(1 for c in self.ch.values() if c["disp"]))

    def memory_depth(self) -> int:
        if self.family == "DS1000Z":
            return {1: 12000, 2: 6000}.get(self.n_enabled(), 3000)
        return 10000

    def sample_rate(self) -> float:
        n = self.n_enabled()
        if self.family == "DS1000Z":
            max_rate = {1: 1e9, 2: 5e8}.get(n, 2.5e8)
        elif self.family == "MSO5000":
            max_rate = 8e9 if n == 1 else 4e9 if n == 2 else 2e9
        else:
            max_rate = 1.25e9 if n == 1 else 6.25e8
        return min(max_rate, self.memory_depth() / (self.hdiv * self.tscale))

    def status(self) -> str:
        if not self.running:
            return "STOP"
        if self.single_armed_at is not None:
            if self.triggerable() and time.monotonic() - self.single_armed_at > 0.05:
                self._freeze()
                return "STOP"
            return "WAIT"
        if self.trig["sweep"] == "AUTO":
            return "TD" if self.triggerable() else "AUTO"
        return "TD" if self.triggerable() else "WAIT"

    def _freeze(self) -> None:
        self.running = False
        self.single_armed_at = None
        self.frozen_seed = self.rng.randrange(1 << 30)

    def _data_rng(self) -> random.Random:
        if not self.running and self.frozen_seed is not None:
            return random.Random(self.frozen_seed)  # a stopped scope keeps showing the same acquisition
        return random.Random(self.rng.randrange(1 << 30))

    # ------------------------------------------------------------ waveform

    def _preamble(self) -> list[float]:
        n = self.wav["source"]
        c = self.ch[n]
        yinc = c["scale"] / 25.0
        yorig = round(c["offset"] / yinc)
        fmt = {"BYTE": 0, "WORD": 1, "ASC": 2}[self.wav["format"]]
        if self.wav["mode"] == "RAW":
            srate = self.sample_rate()
            depth = self.memory_depth()
            return [fmt, 2, depth, 1, 1.0 / srate, self.toffset - depth / (2 * srate), 0, yinc, yorig, self.yref]
        return [fmt, 0, self.screen_points, 1, self.tscale / 100.0, self.toffset - self.hdiv / 2 * self.tscale, 0, yinc, yorig, self.yref]

    def _wave_codes(self, n: int, first: int, count: int, raw: bool) -> bytes:
        pre = self._preamble() if self.wav["source"] == n else None
        if pre is None:
            saved = self.wav["source"]
            self.wav["source"] = n
            pre = self._preamble()
            self.wav["source"] = saved
        xinc, xorig, yinc, yorig, yref = pre[4], pre[5], pre[7], pre[8], pre[9]
        rng = self._data_rng()
        if not self.ch[n]["disp"]:
            return b""
        out = bytearray()
        for i in range(first - 1, first - 1 + count):
            v = self.displayed(n, xorig + i * xinc, rng)
            out.append(min(255, max(0, round(v / yinc + yorig + yref))))
        return bytes(out)

    def _waveform_block(self) -> bytes:
        n = self.wav["source"]
        raw = self.wav["mode"] == "RAW"
        if raw and self.running:
            self.error_queue.append('-221,"Settings conflict"')
            return self._block(b"")
        limit = self.memory_depth() if raw else self.screen_points
        start = max(1, int(self.wav["start"]))
        stop = min(limit, int(self.wav["stop"]))
        count = max(0, stop - start + 1)
        if raw:
            count = min(count, 250_000)
        if self.wav["format"] == "ASC":
            pre = self._preamble()
            codes = self._wave_codes(n, start, count, raw)
            text = ",".join(self._fmt((b - pre[8] - pre[9]) * pre[7]) for b in codes)
            return self._block(text.encode())
        return self._block(self._wave_codes(n, start, count, raw))

    @staticmethod
    def _block(payload: bytes) -> bytes:
        return f"#9{len(payload):09d}".encode() + payload + b"\n"

    # ------------------------------------------------------------ measurements

    def _measure(self, item: str, n: int) -> float:
        c = self.ch[n]
        if not c["disp"]:
            return _INVALID
        lo, hi = self._range(n)
        g = self._gain(n) if c["coup"] != "GND" else 0.0
        window = self.hdiv * self.tscale
        periodic = n in (1, 2) and c["coup"] != "GND"
        period = {1: 1e-3, 2: 5e-4}.get(n, 0.0)
        noise = 3e-3 * abs(self._gain(n))
        if not periodic:
            lo, hi = -3 * noise, 3 * noise
        values = {
            "VMAX": hi, "VMIN": lo, "VPP": hi - lo, "VTOP": hi, "VBASE": lo, "VAMP": hi - lo,
            "VAVG": (lo + hi) / 2 if periodic else 0.0,
            "VRMS": (math.sqrt((hi * hi + lo * lo) / 2) if n == 1 else (hi - lo) / (2 * math.sqrt(2))) if periodic else noise,
            "OVERSHOOT": 0.0, "PRESHOOT": 0.0,
        }
        if periodic and window >= period:
            rise = 2.197 * _SQUARE_TAU_S if n == 1 else (2 * math.asin(0.8)) / (2 * math.pi / period)
            values.update(PERIOD=period, FREQUENCY=1 / period, RTIME=rise, FTIME=rise, PWIDTH=period / 2,
                          NWIDTH=period / 2, PDUTY=0.5, NDUTY=0.5)
        key = item.upper()
        full = {"VBAS": "VBASE", "OVER": "OVERSHOOT", "PRES": "PRESHOOT", "PER": "PERIOD", "FREQ": "FREQUENCY",
                "RTIM": "RTIME", "FTIM": "FTIME", "PWID": "PWIDTH", "NWID": "NWIDTH", "PDUT": "PDUTY", "NDUT": "NDUTY"}
        key = full.get(key, key)
        if key not in {"VMAX", "VMIN", "VPP", "VTOP", "VBASE", "VAMP", "VAVG", "VRMS", "OVERSHOOT", "PRESHOOT",
                       "PERIOD", "FREQUENCY", "RTIME", "FTIME", "PWIDTH", "NWIDTH", "PDUTY", "NDUTY"}:
            raise ValueError(item)
        if key not in values:
            return _INVALID
        value = values[key]
        return value * (1 + self.rng.gauss(0, 2e-4)) + (self.rng.gauss(0, 2e-4 * g) if key.startswith("V") else 0.0)

    # ------------------------------------------------------------ screenshot

    def _screenshot(self, arg: str) -> bytes:
        width, height = 800, 480
        px = bytearray(width * height * 3)

        def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
            if 0 <= x < width and 0 <= y < height:
                i = (y * width + x) * 3
                px[i : i + 3] = bytes(rgb)

        grid = (70, 70, 70)
        for k in range(int(self.hdiv) + 1):
            x = min(width - 1, round(k * (width - 1) / self.hdiv))
            for y in range(height):
                put(x, y, grid)
        for k in range(9):
            y = min(height - 1, round(k * (height - 1) / 8))
            for x in range(width):
                put(x, y, grid)
        for n, c in self.ch.items():
            if not c["disp"]:
                continue
            codes = self._wave_codes(n, 1, self.screen_points, raw=False)
            prev = None
            for x in range(width):
                code = codes[x * self.screen_points // width]
                y = round((255 - code) * (height - 1) / 255)
                lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
                for yy in range(lo, hi + 1):
                    put(x, yy, _COLORS[n])
                prev = y
        wants_png = "PNG" in arg.upper() and self.family != "MSO5000"
        return _png(width, height, px) if wants_png else _bmp(width, height, px)

    # ------------------------------------------------------------ transport

    def handle_bytes(self, data: bytes) -> bytes:
        self._buf += data
        out = b""
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            text = line.decode("ascii", "replace").strip()
            if not text:
                continue
            head, _, arg = text.partition(" ")
            key = head.upper().lstrip(":")
            if self.matches(key, "WAVeform:DATA?"):
                out += self._waveform_block()
            elif self.matches(key, "DISPlay:DATA?"):
                out += self._block(self._screenshot(arg))
            else:
                reply = self.handle(text)
                if reply is not None:
                    out += (reply + "\n").encode("ascii")
        return out

    # ------------------------------------------------------------ SCPI commands

    def _channel_command(self, n: int, sub: str, arg: str) -> str | None:
        if n > self.n_ch:
            raise self.undefined()
        c = self.ch[n]
        query = sub.endswith("?")
        node = sub.rstrip("?")
        m = self.matches
        if m(node, "DISPlay"):
            if query:
                return "1" if c["disp"] else "0"
            if arg.upper() not in {"1", "ON", "0", "OFF"}:
                raise ValueError(arg)
            c["disp"] = arg.upper() in {"1", "ON"}
            return None
        if m(node, "SCALe"):
            if query:
                return self._fmt(c["scale"])
            value = float(arg)
            if not self.min_scale * c["probe"] <= value <= 10.0 * c["probe"] * 1.0001:
                raise ValueError(arg)
            c["scale"] = _snap125(value)  # coarse (1-2-5) steps with fine adjustment off
            return None
        if m(node, "OFFSet"):
            if query:
                return self._fmt(c["offset"])
            value = float(arg)
            limit = (100.0 if c["scale"] >= 0.5 * c["probe"] else 2.0) * c["probe"]
            if abs(value) > limit:
                raise ValueError(arg)
            c["offset"] = value
            return None
        if m(node, "COUPling"):
            if query:
                return c["coup"]
            if arg.upper() not in {"AC", "DC", "GND"}:
                raise ValueError(arg)
            c["coup"] = arg.upper()
            return None
        if m(node, "PROBe"):
            if query:
                return self._fmt(c["probe"]) if self.family == "DS1000Z" else f"{c['probe']:g}"
            value = float(arg)
            if not any(abs(value - p) <= 1e-9 * max(1.0, p) for p in self.probes):
                raise ValueError(arg)
            ratio = value / c["probe"]
            c["probe"] = value
            c["scale"] *= ratio  # the displayed scale follows the probe ratio
            c["offset"] *= ratio
            return None
        if m(node, "BWLimit"):
            if query:
                return c["bw"]
            if arg.upper() not in {"20M", "OFF"}:
                raise ValueError(arg)
            c["bw"] = arg.upper()
            return None
        raise self.undefined()

    def command(self, key: str, arg: str) -> str | None:
        m = self.matches
        if cm := re.fullmatch(r"CHAN(?:NEL)?(\d):(.+)", key):
            return self._channel_command(int(cm[1]), cm[2], arg)
        if (m(key, "AUToscale") and self.family != "DHO") or (m(key, "AUToset") and self.family == "DHO"):
            self._autoscale()
            return None
        if m(key, "RUN"):
            self.running, self.single_armed_at = True, None
            if self.trig["sweep"] == "SING":
                self.trig["sweep"] = "AUTO"
            return None
        if m(key, "STOP"):
            self._freeze()
            return None
        if m(key, "SINGle"):
            self.trig["sweep"] = "SING"
            self.running, self.single_armed_at = True, time.monotonic()
            return None
        if m(key, "TFORce"):
            if self.running and self.trig["sweep"] in {"NORM", "SING"} and self.single_armed_at is not None:
                self._freeze()
            return None
        if m(key, "TIMebase[:MAIN]:SCALe?"):
            return self._fmt(self.tscale)
        if m(key, "TIMebase[:MAIN]:SCALe"):
            value = float(arg)
            if not 5e-9 <= value <= 50.0:
                raise ValueError(arg)
            self.tscale = _snap125(value)
            return None
        if m(key, "TIMebase[:MAIN]:OFFSet?"):
            return self._fmt(self.toffset)
        if m(key, "TIMebase[:MAIN]:OFFSet"):
            value = float(arg)
            if not -self.hdiv * self.tscale * 10 <= value <= max(1.0, 10 * self.tscale):
                raise ValueError(arg)
            self.toffset = value
            return None
        if m(key, "TRIGger:MODE?"):
            return self.trig["mode"]
        if m(key, "TRIGger:MODE"):
            if arg.upper() not in {"EDGE", "PULS", "PULSE", "SLOP", "SLOPE", "VID", "VIDEO"}:
                raise ValueError(arg)
            self.trig["mode"] = arg.upper()[:4] if arg.upper() != "EDGE" else "EDGE"
            return None
        if m(key, "TRIGger:STATus?"):
            return self.status()
        if m(key, "TRIGger:SWEep?"):
            return self.trig["sweep"]
        if m(key, "TRIGger:SWEep"):
            value = {"AUTO": "AUTO", "NORM": "NORM", "NORMAL": "NORM", "SING": "SING", "SINGLE": "SING"}.get(arg.upper())
            if value is None:
                raise ValueError(arg)
            self.trig["sweep"] = value
            if value == "SING":
                self.running, self.single_armed_at = True, time.monotonic()
            return None
        if m(key, "TRIGger:EDGE:SOURce?"):
            return f"CHAN{self.trig['source']}"
        if m(key, "TRIGger:EDGE:SOURce"):
            sm = re.fullmatch(r"CHAN(?:NEL)?(\d)", arg.upper())
            if not sm or not 1 <= int(sm[1]) <= self.n_ch:
                raise ValueError(arg)
            self.trig["source"] = int(sm[1])
            return None
        if m(key, "TRIGger:EDGE:SLOPe?"):
            return self.trig["slope"]
        if m(key, "TRIGger:EDGE:SLOPe"):
            value = {"POS": "POS", "POSITIVE": "POS", "NEG": "NEG", "NEGATIVE": "NEG", "RFAL": "RFAL", "RFALL": "RFAL"}.get(arg.upper())
            if value is None:
                raise ValueError(arg)
            self.trig["slope"] = value
            return None
        if m(key, "TRIGger:EDGE:LEVel?"):
            return self._fmt(self.trig["level"])
        if m(key, "TRIGger:EDGE:LEVel"):
            value = float(arg)
            c = self.ch[self.trig["source"]]
            if not -5 * c["scale"] - c["offset"] <= value <= 5 * c["scale"] - c["offset"]:
                raise ValueError(arg)
            self.trig["level"] = value
            return None
        if m(key, "ACQuire:SRATe?"):
            return self._fmt(self.sample_rate())
        if m(key, "ACQuire:MDEPth?"):
            return str(self.memory_depth())
        if m(key, "MEASure:ITEM?"):
            item, _, src = arg.partition(",")
            sm = re.fullmatch(r"CHAN(?:NEL)?(\d)", src.strip().upper())
            if not sm or not 1 <= int(sm[1]) <= self.n_ch:
                raise ValueError(arg)
            return self._fmt(self._measure(item.strip(), int(sm[1])))
        if m(key, "WAVeform:SOURce?"):
            return f"CHAN{self.wav['source']}"
        if m(key, "WAVeform:SOURce"):
            sm = re.fullmatch(r"CHAN(?:NEL)?(\d)", arg.upper())
            if not sm or not 1 <= int(sm[1]) <= self.n_ch:
                raise ValueError(arg)
            self.wav["source"] = int(sm[1])
            return None
        if m(key, "WAVeform:MODE"):
            value = {"NORM": "NORM", "NORMAL": "NORM", "RAW": "RAW", "MAX": "MAX", "MAXIMUM": "MAX"}.get(arg.upper())
            if value is None:
                raise ValueError(arg)
            self.wav["mode"] = value
            return None
        if m(key, "WAVeform:FORMat"):
            value = {"BYTE": "BYTE", "WORD": "WORD", "ASC": "ASC", "ASCII": "ASC"}.get(arg.upper())
            if value is None:
                raise ValueError(arg)
            self.wav["format"] = value
            return None
        if m(key, "WAVeform:STARt") or m(key, "WAVeform:STOP"):
            value = int(float(arg))
            limit = self.memory_depth() if self.wav["mode"] == "RAW" else self.screen_points
            if not 1 <= value <= limit:
                raise ValueError(arg)
            self.wav["start" if m(key, "WAVeform:STARt") else "stop"] = value
            return None
        if m(key, "WAVeform:PREamble?"):
            p = self._preamble()
            return ",".join([str(int(p[0])), str(int(p[1])), str(int(p[2])), str(int(p[3])), self._fmt(p[4]),
                             self._fmt(p[5]), str(int(p[6])), self._fmt(p[7]), str(int(p[8])), str(int(p[9]))])
        raise self.undefined()

    def _autoscale(self) -> None:
        for n, c in self.ch.items():
            lo, hi = self._range(n)
            if hi - lo > 1e-6:
                c["disp"] = True
                c["scale"] = _snap125((hi - lo) / 6.0)
                c["offset"] = -(hi + lo) / 2
            else:
                c["disp"] = False
        self.tscale = 200e-6 if self.hdiv == 12 else 500e-6
        self.toffset = 0.0
        lo, hi = self._range(1)
        self.trig.update(mode="EDGE", source=1, level=(lo + hi) / 2, slope="POS", sweep="AUTO")
        self.running, self.single_armed_at = True, None


def _png(width: int, height: int, px: bytearray) -> bytes:
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(px[y * stride : (y + 1) * stride]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _bmp(width: int, height: int, px: bytearray) -> bytes:
    stride = width * 3
    pad = (4 - stride % 4) % 4
    rows = []
    for y in range(height - 1, -1, -1):  # bottom-up, BGR
        row = bytearray(px[y * stride : (y + 1) * stride])
        row[0::3], row[2::3] = row[2::3], row[0::3]
        rows.append(bytes(row) + b"\x00" * pad)
    image = b"".join(rows)
    header = b"BM" + struct.pack("<IHHI", 54 + len(image), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(image), 2835, 2835, 0, 0)
    return header + info + image
