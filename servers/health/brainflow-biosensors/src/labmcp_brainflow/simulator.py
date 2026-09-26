"""Simulated biosensing boards.

``--simulate`` normally uses BrainFlow's own ``SYNTHETIC_BOARD`` (real BrainFlow code: 16 EXG channels
at 250 Hz, sine waves at 5, 10, 15 ... 80 Hz plus noise, accelerometer, gyro, PPG, EDA, temperature).

If BrainFlow's native library cannot be loaded on this platform (or ``--option simulator=fake`` is
given), :class:`FakeBoardShim` stands in: an 8-channel, 250 Hz "EEG headset" with the same method
names as ``brainflow.BoardShim``. Its data is physiologically flavoured: ~10 µV 1/f background,
posterior alpha (10 Hz, stronger on O1/O2), small 50 Hz mains pickup, a DC offset per electrode, and
one poorly attached electrode (Fp2) with heavy 50 Hz noise so signal-quality checks have something
to find. Samples accrue in real time while streaming, and markers land in the marker row.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
from labmcp import InstrumentProtocolError

from labmcp_brainflow.driver import NumpyDsp

FAKE_BOARD_ID = -1  # reported like the synthetic board
FAKE_FS = 250
FAKE_NAMES = ["Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2"]
FAKE_DESCR: dict[str, Any] = {
    "name": "LabMCP fake EEG (BrainFlow native library unavailable)",
    "sampling_rate": FAKE_FS,
    "num_rows": 14,
    "package_num_channel": 0,
    "eeg_channels": list(range(1, 9)),
    "emg_channels": list(range(1, 9)),
    "ecg_channels": list(range(1, 9)),
    "eog_channels": list(range(1, 9)),
    "eeg_names": ",".join(FAKE_NAMES),
    "accel_channels": [9, 10, 11],
    "timestamp_channel": 12,
    "marker_channel": 13,
}


class FakeBoardShim:
    """Pure-numpy stand-in for ``brainflow.board_shim.BoardShim`` (default preset only)."""

    def __init__(self, seed: int | None = 0) -> None:
        self.rng = np.random.default_rng(seed)
        self.prepared = False
        self.streaming = False
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._count = 0
        self._capacity = 450000
        self._t0 = 0.0
        self._produced = 0
        self._pink = np.zeros(8)
        self._pending_markers: list[tuple[float, float]] = []  # (time inserted, value)
        self._phase = self.rng.uniform(0, 2 * np.pi, 8)
        self._offsets = self.rng.uniform(-300.0, 300.0, 8)  # electrode DC offsets, µV

    # -- session -------------------------------------------------------------

    def prepare_session(self) -> None:
        self.prepared = True

    def release_session(self) -> None:
        self.streaming = False
        self.prepared = False
        self._chunks, self._count = [], 0

    def is_prepared(self) -> bool:
        return self.prepared

    def start_stream(self, num_samples: int = 450000, streamer_params: str | None = None) -> None:
        self._need_prepared()
        if self.streaming:
            raise InstrumentProtocolError("STREAM_ALREADY_RUN_ERROR:8 stream is already running")
        with self._lock:  # like BrainFlow, a new stream starts with a new (empty) ring buffer
            self._chunks, self._count = [], 0
            self._pending_markers = []
        self.streaming = True
        self._capacity = int(num_samples)
        self._t0 = time.time()
        self._produced = 0

    def stop_stream(self) -> None:
        self._need_prepared()
        if not self.streaming:
            raise InstrumentProtocolError("STREAM_THREAD_IS_NOT_RUNNING:11 stream is not running")
        self._generate()
        self.streaming = False

    # -- data ----------------------------------------------------------------

    def get_board_data_count(self, preset: int = 0) -> int:
        self._generate()
        return self._count

    def get_current_board_data(self, num_samples: int, preset: int = 0) -> np.ndarray:
        self._generate()
        with self._lock:
            data = self._all()
        return data[:, -num_samples:] if num_samples < data.shape[1] else data

    def get_board_data(self, num_samples: int | None = None, preset: int = 0) -> np.ndarray:
        self._generate()
        with self._lock:
            data = self._all()
            if num_samples is None or num_samples >= data.shape[1]:
                self._chunks, self._count = [], 0
                return data
            out, rest = data[:, :num_samples], data[:, num_samples:]
            self._chunks, self._count = [rest], rest.shape[1]
            return out

    def insert_marker(self, value: float, preset: int = 0) -> None:
        if not self.streaming:
            raise InstrumentProtocolError("STREAM_THREAD_IS_NOT_RUNNING:11 stream is not running")
        with self._lock:
            self._pending_markers.append((time.time(), float(value)))

    def config_board(self, config: str) -> str:
        self._need_prepared()
        return f"Config:{config}"

    # -- internals -----------------------------------------------------------

    def _need_prepared(self) -> None:
        if not self.prepared:
            raise InstrumentProtocolError("BOARD_NOT_CREATED_ERROR:15 session is not prepared")

    def _all(self) -> np.ndarray:
        if not self._chunks:
            return np.zeros((FAKE_DESCR["num_rows"], 0))
        if len(self._chunks) > 1:
            self._chunks = [np.concatenate(self._chunks, axis=1)]
        return self._chunks[0].copy()

    def _generate(self) -> None:
        if not self.streaming:
            return
        with self._lock:
            due = int((time.time() - self._t0) * FAKE_FS)
            n = due - self._produced
            if n <= 0:
                return
            idx = np.arange(self._produced, due)
            t = idx / FAKE_FS
            block = np.zeros((FAKE_DESCR["num_rows"], n))
            block[0] = idx % 256
            white = self.rng.normal(0.0, 1.0, (8, n))
            pink = np.empty((8, n))
            state = self._pink
            for i in range(n):  # AR(1) "1/f-like" background
                state = 0.97 * state + white[:, i]
                pink[:, i] = state
            self._pink = state
            alpha_amp = np.array([4, 4, 8, 8, 12, 12, 20, 20], dtype=float)
            envelope = 1.0 + 0.3 * np.sin(2 * np.pi * 0.1 * t)
            for ch in range(8):
                eeg = 2.5 * pink[ch] + alpha_amp[ch] * envelope * np.sin(2 * np.pi * 10.0 * t + self._phase[ch])
                mains = (40.0 if ch == 1 else 1.5) * np.sin(2 * np.pi * 50.0 * t)
                block[1 + ch] = self._offsets[ch] + eeg + mains + self.rng.normal(0, 0.5, n)
            block[9] = self.rng.normal(0.0, 0.01, n)
            block[10] = self.rng.normal(0.0, 0.01, n)
            block[11] = 1.0 + self.rng.normal(0.0, 0.01, n)
            block[12] = self._t0 + t
            for inserted, value in self._pending_markers:  # marker lands on the sample at insertion time
                i = int((inserted - self._t0) * FAKE_FS) - (due - n)
                block[13, min(max(i, 0), n - 1)] = value
            self._pending_markers = []
            self._produced = due
            self._chunks.append(block)
            self._count += n
            if self._count > self._capacity:  # ring buffer: drop the oldest samples
                data = np.concatenate(self._chunks, axis=1)[:, -self._capacity :]
                self._chunks, self._count = [data], data.shape[1]


class FakeLibrary(NumpyDsp):
    """Board description + numpy DSP for the fake board."""

    native = False

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        self.name = "built-in fake board" + (f" (BrainFlow unavailable: {reason})" if reason else "")

    def board_descr(self, board_id: int, preset: int = 0) -> dict[str, Any]:
        if preset != 0:
            raise InstrumentProtocolError("UNSUPPORTED_BOARD_ERROR:14 no such preset")
        return dict(FAKE_DESCR)

    def board_presets(self, board_id: int) -> list[int]:
        return [0]

    def version(self) -> str:
        return "n/a"
