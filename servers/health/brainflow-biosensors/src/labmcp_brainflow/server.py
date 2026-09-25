"""MCP server for EEG/EMG/ECG/PPG biosensing boards supported by BrainFlow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from labmcp import CONTROL, READ, ConnectContext, InstrumentConnectionError, InstrumentServer, Limit
from pydantic import BaseModel, Field

from labmcp_brainflow.driver import (
    ADS1299_BOARDS,
    BANDS,
    BOARDS,
    PRESETS,
    BiosensorBoard,
    BrainFlowLibrary,
    ChannelInfo,
    Recording,
    band_power,
    downsample,
    input_params,
    railed_percent,
    resolve_board,
    rms,
    welch_psd,
)
from labmcp_brainflow.simulator import FAKE_BOARD_ID, FakeBoardShim, FakeLibrary

#: Signal-quality thresholds (EEG-oriented; documented in the README).
FLAT_STD_UV = 0.5
RAILED_PERCENT = 90.0
LINE_NOISE_RATIO = 1.0
HIGH_RMS_UV = 100.0


def connect(ctx: ConnectContext) -> BiosensorBoard:
    options = ctx.settings.options
    if ctx.simulate:
        reason = "forced with --option simulator=fake"
        if ctx.option("simulator", "brainflow") != "fake":
            try:
                lib = BrainFlowLibrary()
                synthetic = int(lib.BoardIds.SYNTHETIC_BOARD.value)
                return BiosensorBoard(
                    lib.open_board(synthetic, {}), lib, synthetic, "SYNTHETIC_BOARD", audit=ctx.audit
                )
            except Exception as exc:  # native library missing/unloadable on this platform
                reason = f"{type(exc).__name__}: {exc}"
        return BiosensorBoard(FakeBoardShim(), FakeLibrary(reason), FAKE_BOARD_ID, "FAKE_EEG", audit=ctx.audit)

    try:
        lib = BrainFlowLibrary()
    except Exception as exc:
        raise InstrumentConnectionError(
            f"Could not load BrainFlow ({type(exc).__name__}: {exc}). Install it with `pip install brainflow` "
            "on a supported platform (Windows, macOS, Linux x86_64/arm64), or use --simulate."
        ) from exc
    board = resolve_board(ctx.option("board"), lib.BoardIds)
    params = input_params(board, ctx.address, options)
    descr_id = board.board_id
    if board.enum_name in {"PLAYBACK_FILE_BOARD", "STREAMING_BOARD"}:
        master = resolve_board(options["master_board"], lib.BoardIds)
        params["master_board"] = master.board_id
        descr_id = master.board_id
    return BiosensorBoard(
        lib.open_board(board.board_id, params),
        lib,
        board.board_id,
        board.enum_name,
        descr_board_id=descr_id,
        params=params,
        audit=ctx.audit,
        exg_gain=int(ctx.option("exg_gain", "24") or 24),
    )


server = InstrumentServer(
    "BrainFlow Biosensing Boards (EEG/EMG/ECG/PPG)",
    connect=connect,
    package="labmcp-brainflow",
    instructions="""
Acquires biosignals (EEG, EMG, ECG, EOG, PPG, EDA, IMU) from boards supported by BrainFlow:
OpenBCI Cyton/Daisy/Ganglion/Galea, Muse 2/S/Athena, Neurosity Crown, g.tec Unicorn, BrainBit, ...
- RESEARCH USE ONLY. This is not a medical device: never use its output to diagnose, monitor or
  treat anyone, and never present band powers or signal quality as clinical findings.
- Call `get_board_info` first: channel names, sampling rate and presets depend on the board.
- EXG values are in microvolts (uV); timestamps are Unix seconds (UTC).
- Check `get_signal_quality` before an experiment. 'flat', 'railed' or 'line_noise' channels usually
  mean a poorly attached electrode; ask the participant/experimenter to fix it and re-check.
- `record` and `get_band_powers` start a temporary stream if none is running. For event-related
  experiments call `start_streaming`, then `insert_marker` at each event (it works while `record`
  is running), and `stop_streaming` at the end to save battery.
- Band powers need >= 2 s of data (4 s recommended); `average_relative` values sum to 1.
""",
    limits=[
        Limit("max_record_duration_s", 60, "s", "Longest recording/analysis window an agent may request"),
    ],
    address_help="""\
  (none)                        synthetic / Muse / Ganglion / Crown / Unicorn: auto-discovery
  /dev/cu.usbserial-XXXX, COM3   Cyton, Cyton+Daisy, FreeEEG, Knight, BLED112 dongle boards (serial_port)
  00:55:DA:B0:12:34              Ganglion (native BLE), Muse, Enophone, Explore (mac_address)
  192.168.4.1                    OpenBCI WiFi Shield, EmotiBit (ip_address)
  UN-2019.05.08                  Unicorn, BrainBit, Crown, ANT Neuro (serial_number)""",
    option_help={
        "board": "board alias (cyton, cyton_daisy, ganglion, muse_2, muse_s, crown, unicorn, ...), a BrainFlow "
        "BoardIds name (e.g. CYTON_DAISY_BOARD) or numeric id; default synthetic",
        "serial_number / mac_address / ip_address / serial_port": "set a BrainFlowInputParams field directly",
        "ip_port": "local port for WiFi Shield boards (default 6789) or the streaming board",
        "other_info": "board-specific string, e.g. Muse preset p50, Ganglion fw:2",
        "timeout": "device discovery timeout in seconds (BLE/WiFi boards)",
        "master_board": "board the data came from (playback/streaming boards)",
        "exg_gain": "ADS1299 PGA gain for railed-% on OpenBCI Cyton boards (default 24)",
        "simulator": "with --simulate: brainflow (synthetic board, default) or fake (pure numpy)",
    },
)
mcp = server.mcp

PresetName = Literal["default", "auxiliary", "ancillary"]
ChannelKind = Literal[
    "exg", "eeg", "emg", "ecg", "eog", "ppg", "eda", "accel", "gyro", "magnetometer", "temperature",
    "resistance", "optical", "rotation", "analog", "other", "all",
]
ExgKind = Literal["exg", "eeg", "emg", "ecg", "eog"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ models


class BoardEntry(BaseModel):
    alias: str = Field(description="Value for --option board=")
    board_id: int
    brainflow_name: str
    description: str
    address: str | None = Field(description="BrainFlowInputParams field that --address fills (None: no address)")
    address_required: bool
    address_hint: str
    notes: str


class BoardInfo(BaseModel):
    board: str
    board_id: int
    device_name: str | None
    simulated: bool
    backend: str = Field(description="BrainFlow version, or the built-in fake board")
    preset: str
    presets: list[str] = Field(description="Data buffers this board provides")
    sampling_rate_hz: int
    channels: dict[str, list[str]] = Field(description="Channel names per type (EXG rows are shared by eeg/emg/ecg/eog)")
    exg_unit: str | None
    streaming: bool
    buffered_samples: int
    timestamp: str


class StreamState(BaseModel):
    streaming: bool
    sampling_rate_hz: int
    buffer_duration_s: float | None = None
    buffered_samples: int
    message: str
    timestamp: str


class ChannelStats(BaseModel):
    name: str
    kind: str
    row: int = Field(description="Row index in BrainFlow's data array")
    unit: str | None
    mean: float
    std: float
    minimum: float
    maximum: float
    peak_to_peak: float


class MarkerEvent(BaseModel):
    time_s: float = Field(description="Seconds from the start of the recording")
    value: float
    timestamp: str


class RecordResult(BaseModel):
    board: str
    simulated: bool
    preset: str
    sampling_rate_hz: int
    n_samples: int
    duration_s: float
    start_time: str | None
    end_time: str | None
    channels: list[ChannelStats]
    markers: list[MarkerEvent]
    trace_times_s: list[float] | None = Field(default=None, description="Time axis of the downsampled traces")
    traces: dict[str, list[float]] | None = Field(default=None, description="Downsampled traces per channel")
    traces_dc_removed: bool
    saved_to: str | None = None
    saved_format: str | None = None
    timestamp: str


class ChannelBandPower(BaseModel):
    name: str
    absolute_uv2: dict[str, float] = Field(description="Band power, uV^2 (integrated Welch PSD)")
    relative: dict[str, float] = Field(description="Band power / sum of the five bands")
    peak_frequency_hz: float | None = Field(description="Frequency of the largest PSD peak in 1-45 Hz")


class BandPowerResult(BaseModel):
    board: str
    simulated: bool
    window_s: float
    sampling_rate_hz: int
    bands_hz: dict[str, tuple[float, float]]
    average_relative: dict[str, float] = Field(
        description="BrainFlow DataFilter.get_avg_band_powers: channel-averaged, relative (sums to 1)"
    )
    average_relative_stddev: dict[str, float] = Field(description="Across-channel stddev / mean per band")
    channels: list[ChannelBandPower]
    processing: str
    timestamp: str


class ChannelQuality(BaseModel):
    name: str
    verdict: Literal["ok", "flat", "railed", "line_noise", "high_amplitude"]
    rms_uv: float = Field(description="RMS after detrend, 50/60 Hz notch and 1-45 Hz band-pass")
    peak_to_peak_uv: float = Field(description="Peak-to-peak of the raw (DC-removed) signal")
    raw_std_uv: float
    railed_percent: float | None = Field(description="% of ADS1299 input range used (OpenBCI Cyton boards only)")
    line_noise_50hz_uv2: float | None
    line_noise_60hz_uv2: float | None
    line_noise_ratio: float | None = Field(description="Mains power (50 or 60 Hz, whichever is larger) / 1-45 Hz power")


class SignalQuality(BaseModel):
    board: str
    simulated: bool
    window_s: float
    sampling_rate_hz: int
    channels: list[ChannelQuality]
    summary: dict[str, int]
    dominant_mains_hz: int | None
    advice: str
    timestamp: str


class MarkerResult(BaseModel):
    value: float
    timestamp: str
    message: str


class ConfigResult(BaseModel):
    command: str
    reply: str
    timestamp: str


# ------------------------------------------------------------------ helpers


def _board() -> BiosensorBoard:
    return server.driver


def _preset(name: str) -> int:
    return PRESETS[name]


def _channel_stats(rec: Recording, channels: list[ChannelInfo]) -> list[ChannelStats]:
    out = []
    for ch in channels:
        x = rec.data[ch.row]
        out.append(
            ChannelStats(
                name=ch.name, kind=ch.kind, row=ch.row, unit=ch.unit,
                mean=float(np.mean(x)), std=float(np.std(x)), minimum=float(np.min(x)),
                maximum=float(np.max(x)), peak_to_peak=float(np.ptp(x)),
            )
        )
    return out


def _clean(board: BiosensorBoard, x: np.ndarray, fs: int) -> np.ndarray:
    """Detrend, notch 50 and 60 Hz, band-pass 1-45 Hz (BrainFlow DataFilter when available)."""
    lib = board.lib
    y = lib.detrend(x)
    if fs > 104:
        y = lib.bandstop(y, fs, 48.0, 52.0)
    if fs > 124:
        y = lib.bandstop(y, fs, 58.0, 62.0)
    return lib.bandpass(y, fs, 1.0, min(45.0, fs / 2.0 - 1.0))


# ------------------------------------------------------------------ tools


@mcp.tool(**READ)
def list_supported_boards() -> list[BoardEntry]:
    """List common BrainFlow boards: the `--option board=` alias, BrainFlow board id, and which
    connection detail `--address` must hold (serial port, Bluetooth MAC, IP address or serial number).
    Any other BrainFlow BoardIds name or numeric id is accepted too. Does not need a board."""
    return [
        BoardEntry(
            alias=b.alias, board_id=b.board_id, brainflow_name=b.enum_name, description=b.description,
            address=b.address_field, address_required=b.address_required,
            address_hint=b.address_hint or "no address needed", notes=b.notes,
        )
        for b in BOARDS
    ]


@mcp.tool(**READ)
def get_board_info(preset: PresetName = "default") -> BoardInfo:
    """Describe the connected board: channel names by type (EEG/EMG/ECG/EOG share the EXG rows on
    most boards), sampling rate, available presets (data buffers) and streaming state."""
    b = _board()
    p = _preset(preset)
    d = b.descr(p)
    groups: dict[str, list[str]] = {}
    for kind in b.available_kinds(p):
        groups[kind] = [c.name for c in b.channels(kind, p)]
    exg_unit = "uV" if "exg" in groups and "GFORCE" not in b.board_name else ("ADC counts" if "exg" in groups else None)
    return BoardInfo(
        board=b.board_name, board_id=b.board_id, device_name=d.get("name"), simulated=server.settings.simulate,
        backend=b.lib.name, preset=preset, presets=b.presets(), sampling_rate_hz=int(d["sampling_rate"]),
        channels=groups, exg_unit=exg_unit, streaming=b.streaming, buffered_samples=b.buffered_samples(p),
        timestamp=_now(),
    )


@mcp.tool(**CONTROL)
def start_streaming(
    buffer_duration_s: Annotated[
        float, Field(ge=1, le=3600, description="Size of BrainFlow's ring buffer, in seconds of data")
    ] = 300,
) -> StreamState:
    """Start continuous acquisition into BrainFlow's ring buffer (the board's radio/LEDs switch on;
    nothing is applied to the participant). Needed for `insert_marker`; `record` then reads from
    the live stream. Call `stop_streaming` when finished."""
    b = _board()
    fs = b.sampling_rate(0)
    already = b.streaming
    b.start_stream(int(buffer_duration_s * fs))
    return StreamState(
        streaming=True, sampling_rate_hz=fs, buffer_duration_s=b.buffer_samples / fs,
        buffered_samples=b.buffered_samples(0),
        message="Stream was already running." if already else "Streaming started.", timestamp=_now(),
    )


@mcp.tool(**CONTROL)
def stop_streaming() -> StreamState:
    """Stop acquisition (saves battery). Data already in the buffer is kept until the session is
    released (`reconnect`) or read."""
    b = _board()
    was = b.streaming
    b.stop_stream()
    return StreamState(
        streaming=False, sampling_rate_hz=b.sampling_rate(0), buffered_samples=b.buffered_samples(0),
        message="Streaming stopped." if was else "The stream was not running.", timestamp=_now(),
    )


@mcp.tool(**READ, timeout=3700)
def record(
    duration_s: Annotated[float, Field(gt=0, le=3600, description="Seconds of data to collect")] = 5.0,
    channel_type: Annotated[ChannelKind, Field(description="Which channels to summarise")] = "exg",
    preset: PresetName = "default",
    max_points: Annotated[int, Field(ge=10, le=5000, description="Max points per downsampled trace")] = 250,
    include_traces: Annotated[bool, Field(description="Return downsampled traces")] = True,
    remove_dc: Annotated[bool, Field(description="Subtract each channel's mean from the traces")] = True,
    save_path: Annotated[
        str | None, Field(description="Write the full-resolution data (all rows) to this file")
    ] = None,
    save_format: Annotated[
        Literal["csv", "brainflow"],
        Field(description="csv: labelled columns; brainflow: DataFilter.write_file format (replayable)"),
    ] = "csv",
) -> RecordResult:
    """Record `duration_s` seconds and return per-channel statistics, event markers and downsampled
    traces. Uses the live stream if one is running, otherwise starts a temporary one. The full data
    (every row, full sampling rate) can be written to `save_path`."""
    server.check("max_record_duration_s", duration_s, "recording duration")
    b = _board()
    p = _preset(preset)
    channels = b.channels(channel_type, p)
    rec = b.acquire(duration_s, p)
    d = b.descr(p)
    n = rec.data.shape[1]
    ts_row, marker_row = d.get("timestamp_channel"), d.get("marker_channel")
    ts = rec.data[ts_row] if ts_row is not None else None
    t0 = float(ts[0]) if ts is not None else rec.started
    times = (ts - t0) if ts is not None else np.arange(n) / rec.sampling_rate
    markers: list[MarkerEvent] = []
    if marker_row is not None:
        for i in np.nonzero(rec.data[marker_row])[0]:
            markers.append(
                MarkerEvent(time_s=float(times[i]), value=float(rec.data[marker_row][i]),
                            timestamp=_iso(float(ts[i])) if ts is not None else _now())
            )
    traces = trace_times = None
    if include_traces:
        trace_times = downsample(times, max_points)
        traces = {}
        for ch in channels:
            x = rec.data[ch.row]
            traces[ch.name] = downsample(x - x.mean() if remove_dc else x, max_points)
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        b.save(rec, str(path), save_format)
        saved = str(path)
    return RecordResult(
        board=b.board_name, simulated=server.settings.simulate, preset=preset, sampling_rate_hz=int(rec.sampling_rate),
        n_samples=n, duration_s=n / rec.sampling_rate,
        start_time=_iso(float(ts[0])) if ts is not None else None,
        end_time=_iso(float(ts[-1])) if ts is not None else None,
        channels=_channel_stats(rec, channels), markers=markers, trace_times_s=trace_times, traces=traces,
        traces_dc_removed=remove_dc, saved_to=saved, saved_format=save_format if saved else None, timestamp=_now(),
    )


@mcp.tool(**READ, timeout=3700)
def get_band_powers(
    window_s: Annotated[float, Field(ge=1, le=3600, description="Seconds of data to analyse (4 s recommended)")] = 4.0,
    channel_type: Annotated[ExgKind, Field(description="EXG channel group to analyse")] = "exg",
) -> BandPowerResult:
    """EEG band powers (delta 1-4, theta 4-8, alpha 8-13, beta 13-30, gamma 30-50 Hz) over the most
    recent `window_s` seconds: BrainFlow's channel-averaged relative powers plus per-channel absolute
    (uV^2) and relative powers and the peak frequency. Records a fresh window if not streaming."""
    server.check("max_record_duration_s", window_s, "analysis window")
    b = _board()
    channels = b.channels(channel_type, 0)
    rec = b.latest(window_s, 0)
    fs = int(rec.sampling_rate)
    rows = [c.row for c in channels]
    avg, std = b.lib.avg_band_powers(rec.data, rows, fs)
    per_channel = []
    for ch in channels:
        freqs, psd = welch_psd(_clean(b, rec.data[ch.row], fs), fs)
        absolute = {name: band_power(freqs, psd, lo, hi) for name, (lo, hi) in BANDS.items()}
        total = sum(absolute.values()) or 1.0
        band = (freqs >= 1.0) & (freqs <= 45.0)
        peak = float(freqs[band][np.argmax(psd[band])]) if band.any() and psd[band].max() > 0 else None
        per_channel.append(
            ChannelBandPower(name=ch.name, absolute_uv2=absolute,
                             relative={k: v / total for k, v in absolute.items()}, peak_frequency_hz=peak)
        )
    names = list(BANDS)
    return BandPowerResult(
        board=b.board_name, simulated=server.settings.simulate, window_s=rec.data.shape[1] / fs, sampling_rate_hz=fs,
        bands_hz=dict(BANDS), average_relative=dict(zip(names, avg, strict=True)),
        average_relative_stddev=dict(zip(names, std, strict=True)),
        channels=per_channel,
        processing=(
            "average_relative: BrainFlow get_avg_band_powers (detrend, 48-52 & 58-62 Hz band-stop, 2-45 Hz "
            "band-pass, Welch PSD). Per-channel: detrend, 50/60 Hz notch, 1-45 Hz band-pass, Welch PSD "
            "(Hann, 50% overlap); gamma is effectively 30-45 Hz."
            + ("" if b.lib.native else " Fallback numpy filters (FFT masks) in use.")
        ),
        timestamp=_now(),
    )


@mcp.tool(**READ, timeout=3700)
def get_signal_quality(
    window_s: Annotated[float, Field(ge=1, le=3600, description="Seconds of data to assess")] = 4.0,
) -> SignalQuality:
    """Check every EXG channel for common electrode problems: flat line (disconnected), railed
    (amplifier saturated, OpenBCI Cyton boards), strong 50/60 Hz mains noise (poor contact or
    missing reference), and implausibly high amplitude (movement, muscle, loose electrode)."""
    server.check("max_record_duration_s", window_s, "analysis window")
    b = _board()
    channels = b.channels("exg", 0)
    rec = b.latest(window_s, 0)
    fs = int(rec.sampling_rate)
    results: list[ChannelQuality] = []
    mains_votes = {50: 0.0, 60: 0.0}
    for ch in channels:
        raw = rec.data[ch.row]
        dc_removed = raw - raw.mean()
        std = float(np.std(raw))
        filtered = _clean(b, raw, fs)
        freqs, psd = welch_psd(b.lib.detrend(raw), fs)
        broadband = band_power(freqs, psd, 1.0, 45.0)
        p50 = band_power(freqs, psd, 49.0, 51.0) if fs >= 104 else None
        p60 = band_power(freqs, psd, 59.0, 61.0) if fs >= 124 else None
        mains = max(v for v in (p50, p60, 0.0) if v is not None)
        ratio = mains / broadband if broadband > 0 else (float("inf") if mains > 0 else 0.0)
        if p50 is not None:
            mains_votes[50] += p50
        if p60 is not None:
            mains_votes[60] += p60
        railed = railed_percent(raw, b.exg_gain) if b.board_name in ADS1299_BOARDS else None
        level = rms(filtered)
        if std < FLAT_STD_UV:
            verdict = "flat"
        elif railed is not None and railed >= RAILED_PERCENT:
            verdict = "railed"
        elif ratio > LINE_NOISE_RATIO:
            verdict = "line_noise"
        elif level > HIGH_RMS_UV:
            verdict = "high_amplitude"
        else:
            verdict = "ok"
        results.append(
            ChannelQuality(
                name=ch.name, verdict=verdict, rms_uv=level, peak_to_peak_uv=float(np.ptp(dc_removed)),
                raw_std_uv=std, railed_percent=railed, line_noise_50hz_uv2=p50, line_noise_60hz_uv2=p60,
                line_noise_ratio=None if np.isinf(ratio) else float(ratio),
            )
        )
    summary: dict[str, int] = {}
    for r in results:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
    dominant = max(mains_votes, key=lambda k: mains_votes[k]) if any(mains_votes.values()) else None
    bad = [r.name for r in results if r.verdict != "ok"]
    advice = (
        "All channels look usable."
        if not bad
        else f"Check electrodes {', '.join(bad)}: re-seat, add gel/saline, check the reference/bias (SRB/BIAS) "
        "leads, and move away from mains-powered equipment. Re-run this check afterwards."
    )
    return SignalQuality(
        board=b.board_name, simulated=server.settings.simulate, window_s=rec.data.shape[1] / fs, sampling_rate_hz=fs,
        channels=results, summary=summary, dominant_mains_hz=dominant, advice=advice, timestamp=_now(),
    )


@mcp.tool(**CONTROL)
def insert_marker(
    value: Annotated[
        float, Field(ge=-1e6, le=1e6, description="Event code written to the marker channel; must not be 0")
    ],
    preset: PresetName = "default",
) -> MarkerResult:
    """Write an event marker into the data stream at the current sample (for event-related
    experiments: stimulus onsets, condition changes). Requires `start_streaming`; markers appear in
    `record` results and saved files."""
    _board().insert_marker(value, _preset(preset))
    return MarkerResult(value=value, timestamp=_now(), message=f"Marker {value:g} inserted.")


@mcp.tool(**CONTROL)
def configure_board(
    command: Annotated[
        str, Field(min_length=1, max_length=200, description="Board-specific configuration string")
    ],
) -> ConfigResult:
    """Send a board-specific configuration command through BrainFlow's config_board (e.g. OpenBCI
    channel settings 'x1060110X', test signals, or Muse presets 'p50'/'p61' to enable PPG). Only
    acquisition settings of the amplifier change. Consult the board's SDK documentation first."""
    reply = _board().config_board(command)
    return ConfigResult(command=command, reply=reply, timestamp=_now())


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

