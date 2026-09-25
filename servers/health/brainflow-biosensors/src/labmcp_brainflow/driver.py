"""BrainFlow driver for EEG/EMG/ECG/PPG biosensing boards.

Built on the open-source BrainFlow SDK (https://brainflow.org, MIT licence), which exposes one API
for many boards. Everything used here was checked against brainflow 5.23 and its documentation:

* BrainFlow User API: ``BoardShim``, ``BrainFlowInputParams``, ``BoardIds``, ``BrainFlowPresets``,
  ``DataFilter`` (https://brainflow.readthedocs.io/en/stable/UserAPI.html).
* "Supported Boards" (https://brainflow.readthedocs.io/en/stable/SupportedBoards.html, source
  ``docs/SupportedBoards.rst``): which ``BrainFlowInputParams`` field each board needs
  (``serial_port``, ``mac_address``, ``ip_address``/``ip_port``, ``serial_number``, ``file``).
* "Data Format Description" (https://brainflow.readthedocs.io/en/stable/DataFormatDesc.html):
  data is a 2-D array ``[rows x samples]``; row meaning comes from ``get_board_descr``; EXG rows are
  in µV "wherever possible"; timestamps are Unix seconds; each board has 1-3 presets
  (default / auxiliary / ancillary buffers).
* ``src/data_handler/data_handler.cpp`` (brainflow 5.23): ``get_avg_band_powers`` (detrend, 50/60 Hz
  band-stop, 2-45 Hz band-pass, Welch PSD, bands 1-4/4-8/8-13/13-30/30-50 Hz, relative powers) and
  ``get_railed_percentage`` (ADS1299 full scale, 4.5 V reference, 24-bit).

No MCP code in here. ``brainflow`` is imported lazily so the package imports (and the fake
simulator works) even where BrainFlow's native libraries cannot be loaded.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from labmcp import InstrumentConnectionError, InstrumentProtocolError

# --------------------------------------------------------------------------- board table

ADDRESS_FIELDS = ("serial_port", "mac_address", "ip_address", "serial_number", "file")


@dataclass(frozen=True)
class BoardSpec:
    """A board we document explicitly (any other ``BoardIds`` member is accepted too)."""

    alias: str
    enum_name: str  # BoardIds member name
    board_id: int  # BoardIds value in brainflow 5.23 (used when brainflow is unavailable)
    description: str
    address_field: str | None  # BrainFlowInputParams field that --address fills
    address_required: bool = False
    address_hint: str = ""
    notes: str = ""


_SERIAL = "serial port of the USB dongle, e.g. /dev/cu.usbserial-XXXX (macOS: use /dev/cu.*), /dev/ttyUSB0, COM3"
_BLED = "serial port of the BLED112 Bluetooth dongle"
_WIFI = "IP address of the OpenBCI WiFi Shield (192.168.4.1 in direct mode); omit to discover it via SSDP"
_BLE_MAC = "Bluetooth MAC address (optional; BrainFlow auto-discovers the device if omitted)"
_SN = "device serial number (optional; needed when several devices are nearby)"

BOARDS: tuple[BoardSpec, ...] = (
    BoardSpec("synthetic", "SYNTHETIC_BOARD", -1, "BrainFlow synthetic board (no hardware, 16 ch @ 250 Hz)", None),
    BoardSpec("playback", "PLAYBACK_FILE_BOARD", -3, "Replay a file recorded with BrainFlow", "file", True,
              "path to a BrainFlow file (e.g. saved by `record` with save_format='brainflow')",
              "Needs --option master_board=<board the file was recorded with>."),
    BoardSpec("streaming", "STREAMING_BOARD", -2, "Receive data streamed by another BrainFlow process",
              "ip_address", True, "multicast address the master streams to, e.g. 225.1.1.1",
              "Needs --option ip_port=<port> and --option master_board=<board of the master process>."),
    BoardSpec("cyton", "CYTON_BOARD", 0, "OpenBCI Cyton (8 ch @ 250 Hz, USB dongle)", "serial_port", True, _SERIAL),
    BoardSpec("cyton_daisy", "CYTON_DAISY_BOARD", 2, "OpenBCI Cyton + Daisy (16 ch @ 125 Hz, USB dongle)",
              "serial_port", True, _SERIAL),
    BoardSpec("cyton_wifi", "CYTON_WIFI_BOARD", 5, "OpenBCI Cyton with WiFi Shield", "ip_address", False, _WIFI,
              "Uses --option ip_port=<free local port> (default 6789)."),
    BoardSpec("cyton_daisy_wifi", "CYTON_DAISY_WIFI_BOARD", 6, "OpenBCI Cyton + Daisy with WiFi Shield",
              "ip_address", False, _WIFI, "Uses --option ip_port=<free local port> (default 6789)."),
    BoardSpec("ganglion", "GANGLION_NATIVE_BOARD", 46, "OpenBCI Ganglion (4 ch @ 200 Hz, native Bluetooth LE)",
              "mac_address", False, _BLE_MAC,
              "Optional --option serial_number=<device name>. Firmware 2 boards need --option other_info=fw:2."),
    BoardSpec("ganglion_dongle", "GANGLION_BOARD", 1, "OpenBCI Ganglion via the BLED112 dongle", "serial_port",
              True, _BLED, "Optional --option mac_address=<Ganglion MAC>."),
    BoardSpec("ganglion_wifi", "GANGLION_WIFI_BOARD", 4, "OpenBCI Ganglion with WiFi Shield", "ip_address", False,
              _WIFI, "Uses --option ip_port=<free local port> (default 6789)."),
    BoardSpec("galea", "GALEA_BOARD", 3, "OpenBCI Galea (EEG/EMG/EOG + aux sensors)", None),
    BoardSpec("muse_2", "MUSE_2_BOARD", 38, "Interaxon Muse 2 (4 EEG ch @ 256 Hz, native BLE)", "mac_address",
              False, _BLE_MAC,
              "Optional --option serial_number=<device name, e.g. Muse-1234>; --option other_info=p50 enables PPG."),
    BoardSpec("muse_s", "MUSE_S_BOARD", 39, "Interaxon Muse S (native BLE)", "mac_address", False, _BLE_MAC,
              "Optional --option serial_number=<device name>; --option other_info=p61 enables PPG."),
    BoardSpec("muse_s_athena", "MUSE_S_ATHENA_BOARD", 67, "Interaxon Muse S Athena (native BLE)", "mac_address",
              False, _BLE_MAC, "Optional --option serial_number=<device name>."),
    BoardSpec("muse_2016", "MUSE_2016_BOARD", 41, "Interaxon Muse 2016 (native BLE)", "mac_address", False, _BLE_MAC),
    BoardSpec("muse_2_bled", "MUSE_2_BLED_BOARD", 22, "Interaxon Muse 2 via the BLED112 dongle", "serial_port",
              True, _BLED, "Optional --option serial_number=<device name>."),
    BoardSpec("muse_s_bled", "MUSE_S_BLED_BOARD", 21, "Interaxon Muse S via the BLED112 dongle", "serial_port",
              True, _BLED, "Optional --option serial_number=<device name>."),
    BoardSpec("crown", "CROWN_BOARD", 23, "Neurosity Crown (8 ch @ 256 Hz, WiFi/OSC)", "serial_number", False, _SN,
              "Device must be on the same network (uses broadcast; may not work on university networks)."),
    BoardSpec("notion_1", "NOTION_1_BOARD", 13, "Neurosity Notion 1", "serial_number", False, _SN),
    BoardSpec("notion_2", "NOTION_2_BOARD", 14, "Neurosity Notion 2", "serial_number", False, _SN),
    BoardSpec("unicorn", "UNICORN_BOARD", 8, "g.tec Unicorn Hybrid Black (8 ch @ 250 Hz)", "serial_number", False,
              _SN, "Pair the headset with the supplied dongle, not the built-in Bluetooth."),
    BoardSpec("brainbit", "BRAINBIT_BOARD", 7, "BrainBit headband (4 ch @ 250 Hz)", "serial_number", False, _SN),
    BoardSpec("callibri_eeg", "CALLIBRI_EEG_BOARD", 9, "Callibri configured for EEG", None),
    BoardSpec("callibri_emg", "CALLIBRI_EMG_BOARD", 10, "Callibri configured for EMG", None),
    BoardSpec("callibri_ecg", "CALLIBRI_ECG_BOARD", 11, "Callibri configured for ECG", None),
    BoardSpec("enophone", "ENOPHONE_BOARD", 37, "Enophone headphones (4 ch)", "mac_address", False,
              "Bluetooth MAC address (required on Linux; Windows/macOS find paired devices)"),
    BoardSpec("explore_4", "EXPLORE_4_CHAN_BOARD", 44, "Mentalab Explore, 4 channels", "mac_address", False,
              "Bluetooth MAC address (required on Linux)"),
    BoardSpec("explore_8", "EXPLORE_8_CHAN_BOARD", 45, "Mentalab Explore, 8 channels", "mac_address", False,
              "Bluetooth MAC address (required on Linux)"),
    BoardSpec("emotibit", "EMOTIBIT_BOARD", 47, "EmotiBit (PPG, EDA, IMU, temperature)", "ip_address", False,
              "broadcast address of the EmotiBit's network, e.g. 192.168.1.255 (optional)",
              "Optional --option serial_number=<EmotiBit id> when several are on the network."),
    BoardSpec("freeeeg32", "FREEEEG32_BOARD", 17, "FreeEEG32 (32 ch @ 512 Hz)", "serial_port", True, _SERIAL),
    BoardSpec("freeeeg128", "FREEEEG128_BOARD", 52, "FreeEEG128 (128 ch)", "serial_port", True, _SERIAL),
    BoardSpec("neuropawn_knight", "NEUROPAWN_KNIGHT_BOARD", 57, "NeuroPawn Knight (8 ch)", "serial_port", True,
              _SERIAL, 'Optional gain: --option other_info={"gain": 6}.'),
    BoardSpec("shimmer3", "SHIMMER3_BOARD", 68, "Shimmer3 (Bluetooth SPP serial port)", "serial_port", True,
              "Bluetooth serial port, e.g. COM3, /dev/rfcomm0, /dev/tty.*"),
    BoardSpec("pieeg", "PIEEG_BOARD", 56, "PiEEG shield for Raspberry Pi", "serial_port", False,
              "SPI device (default /dev/spidev0.0)"),
    BoardSpec("ironbci_32", "IRONBCI_32_BOARD", 65, "IronBCI32 (32 ch)", "serial_port", True, _SERIAL),
    BoardSpec("biolistener", "BIOLISTENER_BOARD", 64, "BioListener (8 ch)", "ip_address", False,
              "IP of this computer's interface to listen on (optional; default all interfaces)"),
    BoardSpec("gforce_pro", "GFORCE_PRO_BOARD", 16, "OYMotion gForcePro EMG armband (returns ADC units, not µV)",
              None),
)
_BY_ALIAS = {b.alias: b for b in BOARDS}
_BY_ENUM = {b.enum_name: b for b in BOARDS}

#: OpenBCI boards built on the TI ADS1299 (railed-percentage formula applies).
ADS1299_BOARDS = {"CYTON_BOARD", "CYTON_DAISY_BOARD", "CYTON_WIFI_BOARD", "CYTON_DAISY_WIFI_BOARD"}

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_SERIAL_RE = re.compile(r"^(COM\d+|/dev/.+)$", re.IGNORECASE)

PRESETS = {"default": 0, "auxiliary": 1, "ancillary": 2}


def guess_address_field(address: str) -> str:
    """For boards without an entry in BOARDS: pick the BrainFlowInputParams field from the format."""
    if _SERIAL_RE.match(address):
        return "serial_port"
    if _MAC_RE.match(address):
        return "mac_address"
    if _IPV4_RE.match(address):
        return "ip_address"
    return "serial_number"


@dataclass
class ResolvedBoard:
    board_id: int
    enum_name: str
    spec: BoardSpec | None


def resolve_board(value: str | None, board_ids: Any | None = None) -> ResolvedBoard:
    """Resolve ``--option board=...``: an alias (``cyton``), a BoardIds name (``CYTON_BOARD``,
    ``cyton_board``, ``MUSE_S_ATHENA``) or a numeric board id (``0``)."""
    raw = (value or "synthetic").strip()
    key = raw.lower().replace("-", "_")
    if key in _BY_ALIAS:
        spec = _BY_ALIAS[key]
        return ResolvedBoard(_enum_value(board_ids, spec.enum_name, spec.board_id), spec.enum_name, spec)
    upper = key.upper()
    for name in (upper, upper + "_BOARD"):
        if name in _BY_ENUM:
            spec = _BY_ENUM[name]
            return ResolvedBoard(_enum_value(board_ids, name, spec.board_id), name, spec)
        if board_ids is not None and name in board_ids.__members__:
            return ResolvedBoard(int(board_ids[name].value), name, None)
    if re.fullmatch(r"-?\d+", raw):
        board_id = int(raw)
        for spec in BOARDS:
            if _enum_value(board_ids, spec.enum_name, spec.board_id) == board_id:
                return ResolvedBoard(board_id, spec.enum_name, spec)
        if board_ids is not None:
            for member in board_ids:
                if int(member.value) == board_id:
                    return ResolvedBoard(board_id, member.name, None)
            raise InstrumentConnectionError(f"BrainFlow has no board with id {board_id}.")
        return ResolvedBoard(board_id, f"BOARD_{board_id}", None)
    known = ", ".join(b.alias for b in BOARDS)
    raise InstrumentConnectionError(
        f"Unknown board {raw!r}. Use one of: {known}; any BrainFlow BoardIds name "
        "(e.g. ANT_NEURO_EE_411_BOARD); or a numeric board id."
    )


def _enum_value(board_ids: Any | None, name: str, fallback: int) -> int:
    if board_ids is not None and name in board_ids.__members__:
        return int(board_ids[name].value)
    return fallback


def input_params(board: ResolvedBoard, address: str | None, options: dict[str, str]) -> dict[str, Any]:
    """Map ``--address`` and ``--option``s to BrainFlowInputParams fields for this board."""
    params: dict[str, Any] = {}
    spec = board.spec
    if address:
        target = spec.address_field if spec and spec.address_field else guess_address_field(address)
        if spec is not None and spec.address_field is None:
            raise InstrumentConnectionError(
                f"{spec.description} does not take an --address; remove it (BrainFlow finds the device itself)."
            )
        params[target] = address
    elif spec is not None and spec.address_required:
        raise InstrumentConnectionError(
            f"{spec.description} needs --address: {spec.address_hint}."
        )
    for key in ("serial_number", "mac_address", "ip_address", "other_info", "file", "serial_port"):
        if options.get(key):
            params[key] = options[key]
    if options.get("ip_port"):
        params["ip_port"] = int(options["ip_port"])
    elif board.enum_name in {"CYTON_WIFI_BOARD", "CYTON_DAISY_WIFI_BOARD", "GANGLION_WIFI_BOARD"}:
        params["ip_port"] = 6789  # "any local port which is currently free" (SupportedBoards.rst)
    if options.get("timeout"):
        params["timeout"] = int(float(options["timeout"]))
    if board.enum_name in {"PLAYBACK_FILE_BOARD", "STREAMING_BOARD"}:
        if not options.get("master_board"):
            raise InstrumentConnectionError(
                f"{board.enum_name} needs --option master_board=<board the data came from, e.g. cyton>."
            )
        if board.enum_name == "STREAMING_BOARD" and "ip_port" not in params:
            raise InstrumentConnectionError("The streaming board needs --option ip_port=<port>.")
    return params


# --------------------------------------------------------------------------- backend protocol


class BoardHandle(Protocol):
    """The subset of ``brainflow.BoardShim`` (instance methods) the driver uses."""

    def prepare_session(self) -> None: ...
    def release_session(self) -> None: ...
    def start_stream(self, num_samples: int = ...) -> None: ...
    def stop_stream(self) -> None: ...
    def get_board_data(self, num_samples: int | None = None, preset: int = 0) -> np.ndarray: ...
    def get_current_board_data(self, num_samples: int, preset: int = 0) -> np.ndarray: ...
    def get_board_data_count(self, preset: int = 0) -> int: ...
    def insert_marker(self, value: float, preset: int = 0) -> None: ...
    def config_board(self, config: str) -> str: ...


class BoardLibrary(Protocol):
    """Static board information (``BoardShim.get_board_descr`` etc.) and signal processing."""

    name: str
    native: bool

    def board_descr(self, board_id: int, preset: int = 0) -> dict[str, Any]: ...
    def board_presets(self, board_id: int) -> list[int]: ...
    def version(self) -> str: ...
    def detrend(self, x: np.ndarray, linear: bool = False) -> np.ndarray: ...
    def bandpass(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray: ...
    def bandstop(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray: ...
    def avg_band_powers(self, data: np.ndarray, rows: list[int], fs: int) -> tuple[list[float], list[float]]: ...


# --------------------------------------------------------------------------- DSP helpers (numpy)

#: Bands used by BrainFlow's get_avg_band_powers (data_handler.cpp).
BANDS: dict[str, tuple[float, float]] = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 50.0),
}


def welch_nfft(fs: float, n: int) -> int:
    """nfft rule from BrainFlow: 2 x next power of two of fs (~0.5 Hz bins), halved to fit the data."""
    nfft = 1
    while nfft < fs:
        nfft *= 2
    nfft *= 2
    while nfft > n:
        nfft //= 2
    return nfft


def welch_psd(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """One-sided Welch PSD (Hann window, 50 % overlap). Units: input unit² / Hz."""
    x = np.asarray(x, dtype=float)
    nfft = welch_nfft(fs, len(x))
    if nfft < 8:
        raise InstrumentProtocolError(
            f"Not enough data for a spectrum ({len(x)} samples); record at least 1-2 seconds."
        )
    window = np.hanning(nfft)
    step = nfft // 2
    scale = 1.0 / (fs * np.sum(window**2))
    acc = np.zeros(nfft // 2 + 1)
    count = 0
    for start in range(0, len(x) - nfft + 1, step):
        seg = x[start : start + nfft]
        seg = (seg - seg.mean()) * window
        acc += np.abs(np.fft.rfft(seg)) ** 2
        count += 1
    psd = acc / count * scale
    psd[1:-1] *= 2.0
    return np.fft.rfftfreq(nfft, 1.0 / fs), psd


def band_power(freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float) -> float:
    """Integrate the PSD over [lo, hi] Hz (trapezoid rule)."""
    mask = (freqs >= lo) & (freqs <= hi)
    if mask.sum() < 2:
        return 0.0
    f, p = freqs[mask], psd[mask]
    return float(np.sum((f[1:] - f[:-1]) * (p[1:] + p[:-1]) / 2.0))


def railed_percent(x: np.ndarray, gain: int = 24) -> float:
    """Percentage of the ADS1299 input range used (100 % = railed or a perfectly flat line).

    Same formula as BrainFlow's ``DataFilter.get_railed_percentage`` (4.5 V reference, 24-bit, µV).
    """
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    diffs = np.abs(np.diff(x))
    if not np.any((diffs > 1e-5) & (np.abs(x[1:]) > 1e-5)):
        return 100.0
    scaler = 4.5 / (2**23 - 1) / gain * 1e6
    return float(np.max(np.abs(x)) / (scaler * 2**23) * 100.0)


def downsample(values: np.ndarray, max_points: int) -> list[float]:
    """Block-average to at most ``max_points`` values (crude anti-aliasing)."""
    n = len(values)
    if n <= max_points:
        return [float(v) for v in values]
    edges = np.linspace(0, n, max_points + 1).astype(int)
    return [float(values[a:b].mean()) for a, b in zip(edges[:-1], edges[1:], strict=True) if b > a]


class NumpyDsp:
    """Pure-numpy fallback used when BrainFlow's native DataFilter library cannot be loaded.
    Filters are zero-phase FFT masks (brick-wall), not Butterworth like BrainFlow's."""

    @staticmethod
    def detrend(x: np.ndarray, linear: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if linear and len(x) > 1:
            t = np.arange(len(x))
            return x - np.polyval(np.polyfit(t, x, 1), t)
        return x - x.mean()

    @staticmethod
    def _mask(x: np.ndarray, fs: float, keep: Any) -> np.ndarray:
        spectrum = np.fft.rfft(np.asarray(x, dtype=float))
        freqs = np.fft.rfftfreq(len(x), 1.0 / fs)
        spectrum[~keep(freqs)] = 0
        return np.fft.irfft(spectrum, len(x))

    def bandpass(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
        return self._mask(x, fs, lambda f: (f >= lo) & (f <= hi))

    def bandstop(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
        return self._mask(x, fs, lambda f: (f < lo) | (f > hi))

    def avg_band_powers(self, data: np.ndarray, rows: list[int], fs: int) -> tuple[list[float], list[float]]:
        """Replicates BrainFlow's get_avg_band_powers(apply_filter=True) with numpy."""
        per_band: list[list[float]] = [[] for _ in BANDS]
        for row in rows:
            x = self.detrend(data[row])
            if fs > 104:
                x = self.bandstop(x, fs, 48.0, 52.0)
            if fs > 124:
                x = self.bandstop(x, fs, 58.0, 62.0)
            x = self.bandpass(x, fs, 2.0, min(45.0, fs / 2.0 - 1.0))
            freqs, psd = welch_psd(x, fs)
            for i, (lo, hi) in enumerate(BANDS.values()):
                per_band[i].append(band_power(freqs, psd, lo, hi))
        avg = [float(np.mean(b)) for b in per_band]
        std = [float(np.std(b)) for b in per_band]
        total = sum(avg) or 1.0
        return [a / total for a in avg], [s / a if a else 0.0 for s, a in zip(std, avg, strict=True)]


# --------------------------------------------------------------------------- real BrainFlow library


class BrainFlowLibrary:
    """Static BoardShim calls and DataFilter processing from the real ``brainflow`` package."""

    native = True

    def __init__(self) -> None:
        # Lazy import: brainflow bundles native libraries that may be missing on some platforms.
        from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams, LogLevels
        from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes

        # BrainFlow logs every session's input parameters at INFO to stderr; keep warnings and
        # errors unless the server runs with --verbose.
        verbose = logging.getLogger("labmcp").isEnabledFor(logging.DEBUG)
        BoardShim.set_log_level((LogLevels.LEVEL_INFO if verbose else LogLevels.LEVEL_WARN).value)
        self.BoardShim = BoardShim
        self.BoardIds = BoardIds
        self.BrainFlowInputParams = BrainFlowInputParams
        self.DataFilter = DataFilter
        self.DetrendOperations = DetrendOperations
        self.FilterTypes = FilterTypes
        self.name = f"brainflow {BoardShim.get_version()}"

    def board_descr(self, board_id: int, preset: int = 0) -> dict[str, Any]:
        return dict(self.BoardShim.get_board_descr(board_id, preset))

    def board_presets(self, board_id: int) -> list[int]:
        return [int(p) for p in self.BoardShim.get_board_presets(board_id)]

    def version(self) -> str:
        return str(self.BoardShim.get_version())

    def open_board(self, board_id: int, params: dict[str, Any]) -> BoardHandle:
        p = self.BrainFlowInputParams()
        for key, value in params.items():
            if key == "master_board":
                value = int(value)
            setattr(p, key, value)
        return self.BoardShim(board_id, p)

    # DataFilter works in place on contiguous float64 1-D arrays.
    def detrend(self, x: np.ndarray, linear: bool = False) -> np.ndarray:
        y = np.ascontiguousarray(x, dtype=np.float64).copy()
        op = self.DetrendOperations.LINEAR if linear else self.DetrendOperations.CONSTANT
        self.DataFilter.detrend(y, op.value)
        return y

    def bandpass(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
        y = np.ascontiguousarray(x, dtype=np.float64).copy()
        self.DataFilter.perform_bandpass(
            y, int(fs), float(lo), float(hi), 4, self.FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0.0
        )
        return y

    def bandstop(self, x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
        y = np.ascontiguousarray(x, dtype=np.float64).copy()
        self.DataFilter.perform_bandstop(
            y, int(fs), float(lo), float(hi), 4, self.FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0.0
        )
        return y

    def avg_band_powers(self, data: np.ndarray, rows: list[int], fs: int) -> tuple[list[float], list[float]]:
        avg, std = self.DataFilter.get_avg_band_powers(np.ascontiguousarray(data), rows, int(fs), True)
        return [float(v) for v in avg], [float(v) for v in std]

    def write_file(self, data: np.ndarray, path: str) -> None:
        self.DataFilter.write_file(np.ascontiguousarray(data), path, "w")


def brainflow_error(exc: Exception, doing: str) -> InstrumentProtocolError:
    """Turn a ``BrainFlowError`` (``'UNABLE_TO_OPEN_PORT_ERROR:2 ...'``) into an actionable message."""
    text = str(exc)
    hints = {
        "UNABLE_TO_OPEN_PORT_ERROR": "the serial port could not be opened: check the port name, that no other "
        "program (e.g. the OpenBCI GUI) has it open, and permissions (Linux: dialout group).",
        "PORT_ALREADY_OPEN_ERROR": "the port is already open, possibly by another program.",
        "BOARD_NOT_READY_ERROR": "the board did not respond: is it switched on, in range, and is the dongle "
        "switch on 'GPIO 6'/PC?",
        "SYNC_TIMEOUT_ERROR": "the board did not answer in time: check power, pairing and distance.",
        "STREAM_ALREADY_RUN_ERROR": "the stream is already running.",
        "STREAM_THREAD_IS_NOT_RUNNING": "the stream is not running; call start_streaming first.",
        "BOARD_NOT_CREATED_ERROR": "the session is not prepared; call the `reconnect` tool.",
        "INVALID_ARGUMENTS_ERROR": "BrainFlow rejected the arguments (check address/options for this board).",
        "UNSUPPORTED_BOARD_ERROR": "this board does not provide that data or command.",
        "GENERAL_ERROR": "the board reported a general error (see BrainFlow's log on stderr).",
    }
    for code, hint in hints.items():
        if code in text:
            return InstrumentProtocolError(f"BrainFlow error while {doing}: {text} - {hint}")
    return InstrumentProtocolError(f"BrainFlow error while {doing}: {text}")


# --------------------------------------------------------------------------- driver


@dataclass
class ChannelInfo:
    row: int
    name: str
    kind: str
    unit: str | None


@dataclass
class Recording:
    data: np.ndarray  # rows x samples
    sampling_rate: float
    preset: int
    started: float = field(default_factory=time.time)


EXG_KINDS = ("eeg", "emg", "ecg", "eog", "exg")
CHANNEL_KINDS = (
    "exg", "eeg", "emg", "ecg", "eog", "ppg", "eda", "accel", "gyro", "magnetometer",
    "temperature", "resistance", "optical", "rotation", "analog", "other", "all",
)


class BiosensorBoard:
    """One prepared BrainFlow session (a real board, BrainFlow's synthetic board or the fake)."""

    def __init__(
        self,
        handle: BoardHandle,
        library: BoardLibrary,
        board_id: int,
        board_name: str,
        *,
        descr_board_id: int | None = None,
        params: dict[str, Any] | None = None,
        audit: Any | None = None,
        exg_gain: int = 24,
    ) -> None:
        self.h = handle
        self.lib = library
        self.board_id = board_id
        self.board_name = board_name
        self.descr_board_id = board_id if descr_board_id is None else descr_board_id
        self.params = dict(params or {})
        self.audit = audit
        self.exg_gain = exg_gain
        self._presets: list[int] | None = None
        self.lock = threading.RLock()
        self.streaming = False
        self.stream_started: float | None = None
        self.buffer_samples = 0
        self._ever_streamed = False  # BrainFlow logs an error if the buffer is queried before
        self._prepared = False
        self._event(f"prepare_session board_id={board_id}")
        try:
            self.h.prepare_session()
        except Exception as exc:
            raise brainflow_error(exc, "preparing the session (connecting to the board)") from exc
        self._prepared = True

    # ------------------------------------------------------------ helpers

    def _event(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, "brainflow")

    def descr(self, preset: int = 0) -> dict[str, Any]:
        # Check first: asking BrainFlow 5.23 for a missing preset makes later get_board_presets()
        # calls report it as present.
        if preset not in self._preset_ids():
            raise InstrumentProtocolError(
                f"This board has no '{_preset_name(preset)}' preset (data buffer). "
                f"Available presets: {', '.join(self.presets())}."
            )
        return self.lib.board_descr(self.descr_board_id, preset)

    def _preset_ids(self) -> list[int]:
        if self._presets is None:
            try:
                self._presets = sorted(self.lib.board_presets(self.descr_board_id))
            except Exception:
                self._presets = [0]
        return self._presets

    def presets(self) -> list[str]:
        return [_preset_name(p) for p in self._preset_ids()]

    def sampling_rate(self, preset: int = 0) -> int:
        return int(self.descr(preset)["sampling_rate"])

    def row_names(self, preset: int = 0) -> dict[int, str]:
        """A readable name for every row of the data array."""
        d = self.descr(preset)
        names: dict[int, str] = {}
        eeg_names = [n for n in str(d.get("eeg_names", "")).split(",") if n]
        eeg_rows = list(d.get("eeg_channels", []))
        if len(eeg_names) == len(eeg_rows):
            names.update(dict(zip(eeg_rows, eeg_names, strict=True)))
        for kind in ("eeg", "emg", "ecg", "eog", "exg"):
            for i, row in enumerate(d.get(f"{kind}_channels", [])):
                names.setdefault(row, f"{kind}_{i + 1}")
        for key, value in d.items():
            if key.endswith("_channels") and isinstance(value, list):
                kind = key[: -len("_channels")]
                for i, row in enumerate(value):
                    names.setdefault(row, f"{kind}_{i + 1}")
            elif key.endswith("_channel") and isinstance(value, int):
                names.setdefault(value, key[: -len("_channel")])
        for row in range(int(d.get("num_rows", 0))):
            names.setdefault(row, f"row_{row}")
        return names

    def channels(self, kind: str = "exg", preset: int = 0) -> list[ChannelInfo]:
        d = self.descr(preset)
        names = self.row_names(preset)
        if kind == "all":
            skip = {d.get("timestamp_channel"), d.get("marker_channel"), d.get("package_num_channel")}
            rows = [r for r in range(int(d["num_rows"])) if r not in skip]
        elif kind == "exg":
            rows = sorted({r for k in EXG_KINDS for r in d.get(f"{k}_channels", [])})
        else:
            rows = list(d.get(f"{kind}_channels", []))
        if not rows:
            available = self.available_kinds(preset)
            raise InstrumentProtocolError(
                f"This board has no {kind} channels in the '{_preset_name(preset)}' preset. "
                f"Available channel types: {', '.join(available) or 'none'}."
            )
        exg_rows = {r for k in EXG_KINDS for r in d.get(f"{k}_channels", [])}
        out = []
        for row in rows:
            row_kind = kind if kind not in {"all", "exg"} else ("exg" if row in exg_rows else names[row].rsplit("_", 1)[0])
            unit = "uV" if row in exg_rows and "GFORCE" not in self.board_name.upper() else None
            out.append(ChannelInfo(row, names[row], row_kind, unit))
        return out

    def available_kinds(self, preset: int = 0) -> list[str]:
        d = self.descr(preset)
        kinds = [k[: -len("_channels")] for k, v in d.items() if k.endswith("_channels") and v]
        if any(d.get(f"{k}_channels") for k in EXG_KINDS):
            kinds.insert(0, "exg")
        return kinds

    # ------------------------------------------------------------ identity

    def identify(self) -> dict[str, Any]:
        d = self.descr(0)
        return {
            "board": self.board_name,
            "board_id": self.board_id,
            "device_name": d.get("name"),
            "backend": self.lib.name,
            "sampling_rate_hz": d.get("sampling_rate"),
            "presets": self.presets(),
            "connection_params": {k: v for k, v in self.params.items() if k != "other_info"},
            "streaming": self.streaming,
        }

    # ------------------------------------------------------------ streaming

    def start_stream(self, buffer_samples: int) -> None:
        with self.lock:
            if self.streaming:
                return
            self._event(f"start_stream buffer={buffer_samples}")
            try:
                self.h.start_stream(int(buffer_samples))
            except Exception as exc:
                raise brainflow_error(exc, "starting the stream") from exc
            self.streaming = True
            self._ever_streamed = True
            self.stream_started = time.time()
            self.buffer_samples = int(buffer_samples)

    def stop_stream(self) -> None:
        with self.lock:
            if not self.streaming:
                return
            self._event("stop_stream")
            try:
                self.h.stop_stream()
            except Exception as exc:
                raise brainflow_error(exc, "stopping the stream") from exc
            finally:
                self.streaming = False
                self.stream_started = None

    def buffered_samples(self, preset: int = 0) -> int:
        if not self._ever_streamed:
            return 0
        try:
            return int(self.h.get_board_data_count(preset))
        except Exception:
            return 0

    def acquire(self, duration_s: float, preset: int = 0) -> Recording:
        """Collect ``duration_s`` of new data. Uses the running stream (non-destructively), or starts
        and stops a temporary stream if none is running."""
        fs = self.sampling_rate(preset)
        n = max(2, int(round(duration_s * fs)))
        started = time.time()
        if self.streaming:
            time.sleep(duration_s)
            data = self._current(n, preset)
        else:
            self.start_stream(max(n * 2, 45000))
            try:
                time.sleep(duration_s)
                try:
                    data = self.h.get_board_data(None, preset)
                except Exception as exc:
                    raise brainflow_error(exc, "reading data") from exc
            finally:
                self.stop_stream()
            data = data[:, -n:] if data.shape[1] > n else data
        if data.shape[1] < 2:
            raise InstrumentProtocolError(
                "No data arrived from the board. Check that it is powered on, in range, and streaming "
                "(electrodes do not need to be attached to get data)."
            )
        return Recording(np.asarray(data, dtype=float), fs, preset, started)

    def latest(self, window_s: float, preset: int = 0) -> Recording:
        """The most recent ``window_s`` of data: from the running stream if it already holds enough,
        otherwise by acquiring a fresh window."""
        fs = self.sampling_rate(preset)
        n = max(2, int(round(window_s * fs)))
        if self.streaming and self.buffered_samples(preset) >= n:
            return Recording(self._current(n, preset), fs, preset, time.time() - window_s)
        return self.acquire(window_s, preset)

    def _current(self, n: int, preset: int) -> np.ndarray:
        try:
            return np.asarray(self.h.get_current_board_data(n, preset), dtype=float)
        except Exception as exc:
            raise brainflow_error(exc, "reading data") from exc

    def insert_marker(self, value: float, preset: int = 0) -> None:
        if not self.streaming:
            raise InstrumentProtocolError("Markers can only be inserted while streaming; call start_streaming first.")
        if value == 0:
            raise InstrumentProtocolError("Marker value 0 is reserved by BrainFlow (it means 'no marker').")
        self._event(f"insert_marker {value:g}")
        try:
            self.h.insert_marker(float(value), preset)
        except Exception as exc:
            raise brainflow_error(exc, "inserting a marker") from exc

    def config_board(self, command: str) -> str:
        with self.lock:
            if self.audit is not None:
                self.audit.record("write", f"config_board {command}", "brainflow")
            try:
                reply = self.h.config_board(command)
            except Exception as exc:
                raise brainflow_error(exc, f"sending config {command!r}") from exc
            reply = "" if reply is None else str(reply)
            if self.audit is not None:
                self.audit.record("read", reply, "brainflow")
            return reply

    def save(self, recording: Recording, path: str, fmt: str) -> None:
        if fmt == "brainflow":
            if self.lib.native:
                self.lib.write_file(recording.data, path)  # type: ignore[attr-defined]
            else:
                np.savetxt(path, recording.data.T, delimiter="\t", fmt="%.6f")
            return
        names = self.row_names(recording.preset)
        header = ",".join(names.get(r, f"row_{r}") for r in range(recording.data.shape[0]))
        np.savetxt(path, recording.data.T, delimiter=",", header=header, comments="", fmt="%.6f")

    def close(self) -> None:
        with self.lock:
            if self.streaming:
                with contextlib.suppress(Exception):
                    self.stop_stream()
            if self._prepared:
                self._event("release_session")
                with contextlib.suppress(Exception):
                    self.h.release_session()
                self._prepared = False


def _preset_name(preset: int) -> str:
    return {v: k for k, v in PRESETS.items()}.get(preset, str(preset))


def rms(x: np.ndarray) -> float:
    return float(math.sqrt(float(np.mean(np.square(x))))) if len(x) else 0.0
