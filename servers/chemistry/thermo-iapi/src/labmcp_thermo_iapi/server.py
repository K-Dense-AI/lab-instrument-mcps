"""MCP server for Thermo Fisher Orbitrap mass spectrometers through the Instrument API (IAPI).

An adapter only: the IAPI assemblies are licensed by Thermo Fisher Scientific and must be
obtained by the user; they are never bundled or downloaded. See ``pythonnet_backend.py`` for the
IAPI members used and where each was verified.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from labmcp import (
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_thermo_iapi.backend import OrbitrapBackend, ScanRecord
from labmcp_thermo_iapi.driver import OrbitrapDriver, parse_selection, summarize_scan
from labmcp_thermo_iapi.pythonnet_backend import PythonNetBackend
from labmcp_thermo_iapi.simulator import MODELS, FakeOrbitrap

_TRUE = {"1", "true", "yes", "on"}


def _float_option(ctx: ConnectContext, name: str, default: float) -> float:
    raw = ctx.option(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise InstrumentProtocolError(f"--option {name} must be a number (got {raw!r}).") from exc


def connect(ctx: ConnectContext) -> OrbitrapDriver:
    buffer_size = int(_float_option(ctx, "buffer_size", 500))
    backend: OrbitrapBackend
    if ctx.simulate:
        model = (ctx.option("sim_model", "exploris480") or "exploris480").lower()
        if model not in MODELS:
            raise InstrumentProtocolError(
                f"--option sim_model must be one of {sorted(MODELS)} (got {model!r})."
            )
        backend = FakeOrbitrap(model, speed=_float_option(ctx, "sim_speed", 1.0))
    else:
        readbacks = [n.strip() for n in (ctx.option("readbacks", "") or "").split(";") if n.strip()]
        backend = PythonNetBackend(
            family=(ctx.option("instrument", "") or "").lower(),
            assembly_dir=ctx.option("assembly_dir"),
            runtime=ctx.option("runtime", "netfx") or "netfx",
            readback_names=readbacks,
            exclusive_scans=(ctx.option("exclusive_scans", "false") or "").lower() in _TRUE,
            connect_timeout_s=_float_option(ctx, "connect_timeout_s", 10.0),
        )
    try:
        return OrbitrapDriver(backend, audit=ctx.audit, buffer_size=buffer_size)
    except Exception:
        try:
            backend.close()
        except Exception:
            pass
        raise


server = InstrumentServer(
    "Thermo Orbitrap (Instrument API)",
    connect=connect,
    package="labmcp-thermo-iapi",
    instructions="""
Monitors and controls a Thermo Fisher Orbitrap mass spectrometer (Tribrid Fusion/Lumos/Eclipse/
Ascend, Exploris 240/480, Q Exactive family) through Thermo's licensed Instrument API (IAPI).
- Start with `get_instrument_status`: the system mode must be On for scans to arrive and for
  custom scans or acquisitions to run. The IAPI licence is required for any control tool.
- Scans stream in continuously in On mode. Read them with `get_recent_scans` (filter by MS order)
  or block for the next one with `wait_for_scan`; centroid lists are capped by `max_centroids`.
- Before `submit_custom_scan` or `set_repeating_scan`, call `get_possible_scan_parameters`: the
  instrument decides which parameter names and values are legal (they differ by model, licence
  and Tune version). Invalid values are refused before anything is sent.
- A custom scan's `running_number` comes back as the scan's `access_id`: use
  `wait_for_scan(access_id=...)` to pick up its result. System scans have access_id -1.
- `start_acquisition` consumes sample and writes a raw file on the instrument PC. Confirm the
  sample, the raw file path and the duration with the user first.
- `stop_acquisition` (also cancels custom and repeating scans), `cancel_custom_scans` and
  `cancel_repeating_scan` are always available; use them if anything looks wrong.
""",
    limits=[
        Limit("max_custom_scans_per_minute", 60, "scans/min", "Custom scans an agent may place per minute"),
        Limit("max_injection_time_ms", 1000, "ms", "Largest maximum injection time (MaxIT) in a scan"),
        Limit("max_acquisition_duration_s", 7200, "s", "Longest time-limited acquisition an agent may start"),
    ],
    address_help="""\
  (not used)   IAPI talks to the instrument software (Tune) on this Windows PC; select the
               instrument family with --option instrument=tribrid|exploris|exactive""",
    option_help={
        "instrument": "tribrid (Fusion/Lumos/Eclipse/Ascend, Factory<IFusionInstrumentAccessContainer>), "
        "exploris (Exploris 240/480, DataSystem.xml) or exactive (Q Exactive family, registry)",
        "assembly_dir": "folder with the IAPI assemblies you obtained from Thermo under your IAPI licence "
        "(e.g. API-2.0.dll, Spectrum-1.0.dll, Thermo.TNG.Factory.dll, Fusion.API-2.0.dll)",
        "runtime": "pythonnet runtime: netfx (default, .NET Framework 4.8) or coreclr",
        "readbacks": "instrument value names to read in get_instrument_status, separated by ';' "
        "(names differ by model; get_instrument_status lists the available ones)",
        "exclusive_scans": "true to request exclusive IScans access (default false, cooperative)",
        "connect_timeout_s": "seconds to wait for the IAPI service to connect (default 10)",
        "buffer_size": "number of recent scans kept in memory (default 500)",
        "sim_model": f"simulated instrument: {', '.join(sorted(MODELS))} (default exploris480)",
        "sim_speed": "simulator time factor (default 1 = real time)",
    },
)
mcp = server.mcp

_submit_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ----------------------------------------------------------------------- models


class CentroidModel(BaseModel):
    mz: float
    intensity: float
    charge: int | None = Field(None, description="Charge state assigned by the instrument, if any")


class ScanSummary(BaseModel):
    sequence: int = Field(description="Arrival order since the server connected")
    received_at: str = Field(description="UTC time MsScanArrived fired (ISO 8601)")
    scan_number: int | None
    ms_order: int | None = Field(description="1 = MS1 survey scan, 2 = MS2, ...")
    scan_mode: str | None
    precursor_mz: float | None
    master_scan_number: int | None = Field(description="Survey scan this MSn scan was triggered from")
    access_id: int | None = Field(description="RunningNumber of the custom/repeating scan; -1 = system scan")
    agc_target: float | None
    injection_time_ms: float | None
    centroid_count: int = Field(description="Number of centroids in the full scan")
    base_peak_mz: float | None
    base_peak_intensity: float | None
    summed_centroid_intensity: float = Field(description="Sum of all centroid intensities (a TIC proxy)")
    top_centroids: list[CentroidModel] = Field(description="Most intense centroids, capped by max_centroids")
    header: dict[str, str] | None = None
    trailer: dict[str, str] | None = None


class RecentScans(BaseModel):
    scans: list[ScanSummary]
    returned: int
    buffered_total: int
    saved_to: str | None = None
    timestamp: str


class WaitResult(BaseModel):
    found: bool
    waited_s: float
    scan: ScanSummary | None
    message: str


class InstrumentStatus(BaseModel):
    model: str
    family: str | None
    backend: str
    service_connected: bool
    instrument_connected: bool
    system_mode: str | None = Field(description="IState.SystemMode, e.g. On, Standby, Off, Disconnected")
    system_state: str | None = Field(description="IState.SystemState, e.g. Running, ReadyToDownload, Error")
    acquiring: bool
    can_pause: bool
    can_resume: bool
    api_license: bool | None = Field(description="IAPI licence found (Exploris only; None = not queryable)")
    readbacks: dict[str, Any] = Field(description="Requested instrument values (IInstrumentValues)")
    readback_names: list[str] = Field(description="Instrument value names the API exposes")
    last_status_log: dict[str, str] = Field(description="Latest scan status log (vacuum, source, ...)")
    scans_received: int
    last_scan_at: str | None
    custom_scans_last_minute: int
    acquisition: dict[str, Any] | None = None
    timestamp: str


class ParameterInfo(BaseModel):
    name: str
    selection: str = Field(description="Raw IParameterDescription.Selection")
    kind: str = Field(description="none | string | int | float | choice")
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None
    default_value: str
    help: str


class ScanSubmission(BaseModel):
    sent: bool = Field(description="True if IAPI reports the command was sent to the instrument")
    running_number: int
    values: dict[str, str] = Field(description="The exact IAPI scan values sent")
    custom_scans_last_minute: int | None = None
    message: str


# ---------------------------------------------------------------------- helpers


def _summary(rec: ScanRecord, max_centroids: int, include_header_trailer: bool) -> ScanSummary:
    s = summarize_scan(rec)
    base = rec.centroids[0] if rec.centroids else None
    return ScanSummary(
        sequence=rec.sequence,
        received_at=rec.received_at,
        centroid_count=rec.centroid_count,
        base_peak_mz=base.mz if base else None,
        base_peak_intensity=base.intensity if base else None,
        summed_centroid_intensity=round(sum(c.intensity for c in rec.centroids), 1),
        top_centroids=[
            CentroidModel(mz=c.mz, intensity=c.intensity, charge=c.charge)
            for c in rec.centroids[:max_centroids]
        ],
        header=dict(rec.header) if include_header_trailer else None,
        trailer=dict(rec.trailer) if include_header_trailer else None,
        **s,
    )


def _as_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _fmt(x: float) -> str:
    return f"{x:g}" if x != int(x) or abs(x) >= 1e15 else str(int(x))


MsOrder = Annotated[int | None, Field(ge=1, le=10, description="Only scans of this MS order (1 = MS1)")]
AccessId = Annotated[
    int | None,
    Field(ge=-1, le=2_147_483_647, description="Only scans with this access id (-1 = system scans)"),
]
MaxCentroids = Annotated[int, Field(ge=0, le=500, description="Most intense centroids to return per scan")]


def _build_values(
    *,
    scan_type: str | None,
    analyzer: str | None,
    first_mass_mz: float | None,
    last_mass_mz: float | None,
    orbitrap_resolution: int | None,
    agc_target: int | None,
    max_injection_time_ms: float | None,
    polarity: str | None,
    microscans: int | None,
    precursor_mz: float | None,
    isolation_width_mz: float | None,
    activation_type: str | None,
    collision_energy: float | None,
    scan_description: str | None,
    extra_parameters: dict[str, str] | None,
) -> dict[str, str]:
    driver = server.driver
    names = {p.name.lower(): p.name for p in driver.possible_parameters()}
    analyzer_key = names.get("analyzer") or names.get("massanalyzer") or "Analyzer"
    typed: dict[str, Any] = {
        "ScanType": scan_type,
        analyzer_key: analyzer,
        "FirstMass": first_mass_mz,
        "LastMass": last_mass_mz,
        "OrbitrapResolution": orbitrap_resolution,
        "AGCTarget": agc_target,
        "MaxIT": max_injection_time_ms,
        "Polarity": polarity,
        "Microscans": microscans,
        "PrecursorMass": precursor_mz,
        "IsolationWidth": isolation_width_mz,
        "ActivationType": activation_type,
        "CollisionEnergy": collision_energy,
        "ScanDescription": scan_description,
    }
    values = {
        k: (_fmt(float(v)) if isinstance(v, (int, float)) else str(v))
        for k, v in typed.items()
        if v is not None
    }
    for key, raw in (extra_parameters or {}).items():
        if any(key.lower() == k.lower() for k in values):
            raise InstrumentProtocolError(
                f"Refused: {key!r} is given both as a named argument and in extra_parameters. Nothing was sent."
            )
        values[key] = str(raw)
    if not values:
        raise InstrumentProtocolError(
            "Refused: no scan parameters given (at least one is required). Nothing was sent."
        )
    lower = {k.lower(): v for k, v in values.items()}
    if "maxit" in lower:
        server.check("max_injection_time_ms", _as_float(lower["maxit"]), "maximum injection time")
    validated = driver.validate(values)
    lo, hi = lower.get("firstmass"), lower.get("lastmass")
    if lo is not None and hi is not None and _as_float(lo) >= _as_float(hi):
        raise InstrumentProtocolError(
            f"Refused: first_mass_mz ({lo}) must be below last_mass_mz ({hi}). Nothing was sent."
        )
    return validated


# ------------------------------------------------------------------------ tools


@mcp.tool(**READ)
def get_instrument_status() -> InstrumentStatus:
    """Report the instrument model, IAPI service/instrument connection, system mode and state
    (On/Standby/Off; Running/ReadyToDownload/...), whether an acquisition can be paused or
    resumed, the IAPI licence where the API exposes it, requested readbacks, the latest scan
    status log (vacuum, source) and how many scans have arrived."""
    driver = server.driver
    st = driver.status()
    ident = driver.identify()
    return InstrumentStatus(
        model=ident.get("model", "unknown"),
        family=ident.get("family"),
        backend=ident.get("backend", driver.backend.kind),
        service_connected=bool(st.get("service_connected")),
        instrument_connected=bool(st.get("instrument_connected")),
        system_mode=st.get("system_mode"),
        system_state=st.get("system_state"),
        acquiring=st.get("system_state") == "Running",
        can_pause=bool(st.get("can_pause")),
        can_resume=bool(st.get("can_resume")),
        api_license=st.get("api_license"),
        readbacks=st.get("readbacks", {}),
        readback_names=st.get("readback_names", []),
        last_status_log=st.get("last_status_log", {}),
        scans_received=st.get("scans_received", 0),
        last_scan_at=st.get("last_scan_at"),
        custom_scans_last_minute=driver.custom_scans_in_window(),
        acquisition=st.get("acquisition"),
        timestamp=_now(),
    )


@mcp.tool(**READ)
def get_possible_scan_parameters(
    name_contains: Annotated[
        str | None, Field(max_length=50, description="Case-insensitive name filter")
    ] = None,
    refresh: Annotated[bool, Field(description="Re-read from the instrument instead of the cache")] = False,
) -> list[ParameterInfo]:
    """List the scan parameters this instrument accepts for custom and repeating scans
    (IScans.PossibleParameters): name, allowed range or choices, default and help. The set
    depends on the model, licence and Tune version."""
    out = []
    for p in server.driver.possible_parameters(refresh=refresh):
        if name_contains and name_contains.lower() not in p.name.lower():
            continue
        sel = parse_selection(p.selection)
        out.append(
            ParameterInfo(
                name=p.name,
                selection=p.selection,
                kind=sel.kind,
                min=sel.low,
                max=sel.high,
                choices=list(sel.choices) if sel.kind == "choice" else None,
                default_value=p.default_value,
                help=p.help,
            )
        )
    return out


@mcp.tool(**READ)
def get_recent_scans(
    count: Annotated[int, Field(ge=1, le=100, description="Number of most recent matching scans")] = 10,
    ms_order: MsOrder = None,
    access_id: AccessId = None,
    max_centroids: MaxCentroids = 20,
    include_header_trailer: Annotated[bool, Field(description="Include the raw header and trailer")] = False,
    save_path: Annotated[
        str | None, Field(description="Optional .csv path: write all kept centroids of the returned scans")
    ] = None,
) -> RecentScans:
    """Return the most recent scans received from the instrument (oldest first), optionally
    only one MS order or one custom scan's access id: scan number, MS order, precursor m/z,
    AGC target, injection time and the most intense centroids. Scans only arrive in On mode."""
    driver = server.driver
    recs = driver.recent_scans(count, ms_order=ms_order, access_id=access_id)
    saved = None
    if save_path:
        path = Path(save_path).expanduser()
        if path.suffix.lower() != ".csv":
            raise InstrumentProtocolError("save_path must end in .csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sequence", "scan_number", "ms_order", "precursor_mz", "mz", "intensity", "charge"])
            for r in recs:
                s = summarize_scan(r)
                for c in r.centroids:
                    w.writerow(
                        [
                            r.sequence,
                            s["scan_number"],
                            s["ms_order"],
                            s["precursor_mz"],
                            c.mz,
                            c.intensity,
                            c.charge,
                        ]
                    )
        saved = str(path)
    total = driver.buffered_count
    return RecentScans(
        scans=[_summary(r, max_centroids, include_header_trailer) for r in recs],
        returned=len(recs),
        buffered_total=total,
        saved_to=saved,
        timestamp=_now(),
    )


@mcp.tool(**READ, timeout=330)
def wait_for_scan(
    timeout_s: Annotated[float, Field(ge=0.1, le=300, description="Longest time to wait")] = 10.0,
    ms_order: MsOrder = None,
    access_id: AccessId = None,
    max_centroids: MaxCentroids = 20,
    include_header_trailer: Annotated[bool, Field(description="Include the raw header and trailer")] = False,
) -> WaitResult:
    """Wait for the next scan that arrives after this call (optionally of one MS order, or the
    result of a custom scan by its access id) and return it. Returns found=false after
    timeout_s if nothing matching arrived (e.g. the instrument is in Standby)."""
    t0 = time.monotonic()
    rec = server.driver.wait_for_scan(timeout_s, ms_order=ms_order, access_id=access_id)
    waited = round(time.monotonic() - t0, 3)
    if rec is None:
        return WaitResult(
            found=False,
            waited_s=waited,
            scan=None,
            message=f"No matching scan arrived within {timeout_s:g} s. Check get_instrument_status "
            "(scans only arrive in On mode).",
        )
    return WaitResult(
        found=True, waited_s=waited, scan=_summary(rec, max_centroids, include_header_trailer), message="ok"
    )


@mcp.tool(**HAZARD)
def start_acquisition(
    mode: Annotated[
        Literal["duration", "scan_count", "until_stopped"],
        Field(description="Stop after duration_s, after scan_count scans, or only when stopped"),
    ],
    raw_file_path: Annotated[
        str | None,
        Field(max_length=260, description="Raw file to write on the instrument PC, e.g. D:\\Data\\run1.raw"),
    ] = None,
    duration_s: Annotated[float | None, Field(ge=1, le=86400, description="For mode=duration")] = None,
    scan_count: Annotated[int | None, Field(ge=1, le=10_000_000, description="For mode=scan_count")] = None,
    sample_name: Annotated[str | None, Field(max_length=100)] = None,
    comment: Annotated[str | None, Field(max_length=200)] = None,
) -> str:
    """Start an acquisition with the instrument's current settings (IAPI StartAcquisition),
    recording to a raw file. This consumes sample. The instrument must be On; an acquisition
    must not already be running. Stop it with stop_acquisition."""
    if mode == "duration":
        if duration_s is None:
            raise InstrumentProtocolError("mode=duration needs duration_s.")
        server.check("max_acquisition_duration_s", duration_s, "acquisition duration")
    if mode == "scan_count" and scan_count is None:
        raise InstrumentProtocolError("mode=scan_count needs scan_count.")
    if raw_file_path and not raw_file_path.lower().endswith(".raw"):
        raise InstrumentProtocolError("raw_file_path must end in .raw")
    server.driver.start_acquisition(
        mode,
        duration_s=duration_s if mode == "duration" else None,
        scan_count=scan_count if mode == "scan_count" else None,
        raw_file_path=raw_file_path,
        sample_name=sample_name,
        comment=comment,
    )
    what = {
        "duration": f"for {duration_s:g} s",
        "scan_count": f"for {scan_count} scans",
        "until_stopped": "until stopped",
    }
    target = f" to {raw_file_path}" if raw_file_path else ""
    return f"Acquisition started {what[mode]}{target}."


@mcp.tool(**SAFETY)
def pause_acquisition() -> str:
    """Pause the running acquisition (IAPI Pause). Fails if the instrument reports it cannot pause."""
    server.driver.pause_acquisition()
    return "Acquisition paused."


@mcp.tool(**HAZARD)
def resume_acquisition() -> str:
    """Resume a paused acquisition (IAPI Resume). Sample consumption continues."""
    server.driver.resume_acquisition()
    return "Acquisition resumed."


@mcp.tool(**SAFETY)
def stop_acquisition(
    cancel_scans: Annotated[
        bool, Field(description="Also cancel pending custom scans and the repeating scan")
    ] = True,
    standby: Annotated[bool, Field(description="Then put the instrument in Standby")] = False,
) -> str:
    """Stop the running acquisition (IAPI CancelAcquisition), by default also cancelling custom
    and repeating scans, and optionally switch the instrument to Standby (switch back to On in
    Tune)."""
    driver = server.driver
    driver.cancel_acquisition()
    done = ["Acquisition cancelled"]
    if cancel_scans:
        driver.cancel_custom_scan()
        driver.cancel_repeating_scan()
        done.append("custom and repeating scans cancelled")
    if standby:
        driver.set_standby()
        done.append("instrument set to Standby (switch it back to On in Tune)")
    return "; ".join(done) + "."


@mcp.tool(**SAFETY)
def cancel_custom_scans() -> str:
    """Cancel any pending custom scan and its processing delay (IAPI CancelCustomScan)."""
    sent = server.driver.cancel_custom_scan()
    return "Custom scans cancelled." if sent else "Cancel request could not be sent to the instrument."


@mcp.tool(**SAFETY)
def cancel_repeating_scan() -> str:
    """Cancel the repeating scan set with set_repeating_scan (IAPI CancelRepetition)."""
    sent = server.driver.cancel_repeating_scan()
    return "Repeating scan cancelled." if sent else "Cancel request could not be sent to the instrument."


ScanType = Annotated[str | None, Field(max_length=20, description="ScanType, e.g. Full, SIM or MSn")]
Analyzer = Annotated[
    str | None, Field(max_length=20, description="Mass analyzer, e.g. Orbitrap or IonTrap (Tribrid)")
]
FirstMass = Annotated[float | None, Field(ge=1, le=20000, description="FirstMass: scan range start (m/z)")]
LastMass = Annotated[float | None, Field(ge=1, le=20000, description="LastMass: scan range end (m/z)")]
Resolution = Annotated[
    int | None,
    Field(ge=1000, le=1_000_000, description="OrbitrapResolution at m/z 200; must be an allowed value"),
]
AgcTarget = Annotated[int | None, Field(ge=1, le=100_000_000, description="AGCTarget (charges)")]
MaxIT = Annotated[float | None, Field(gt=0, le=10000, description="MaxIT: maximum injection time (ms)")]
Polarity = Annotated[str | None, Field(max_length=20, description="Polarity as the instrument lists it")]
Microscans = Annotated[int | None, Field(ge=1, le=1000)]
PrecursorMz = Annotated[float | None, Field(ge=1, le=20000, description="PrecursorMass (m/z) for MSn/SIM")]
IsolationWidth = Annotated[float | None, Field(gt=0, le=2000, description="IsolationWidth (m/z)")]
Activation = Annotated[str | None, Field(max_length=40, description="ActivationType, e.g. HCD or CID")]
CollisionEnergy = Annotated[float | None, Field(ge=0, le=500, description="CollisionEnergy (normalized, %)")]
Description = Annotated[str | None, Field(max_length=100, description="ScanDescription stored with the scan")]
Extra = Annotated[
    dict[str, str] | None,
    Field(
        max_length=40, description="Other IAPI scan values by exact name (see get_possible_scan_parameters)"
    ),
]
RunningNumber = Annotated[
    int | None, Field(ge=1, le=2_000_000_000, description="Reported back as access_id (default: auto)")
]


@mcp.tool(**HAZARD)
def submit_custom_scan(
    scan_type: ScanType = None,
    analyzer: Analyzer = None,
    first_mass_mz: FirstMass = None,
    last_mass_mz: LastMass = None,
    orbitrap_resolution: Resolution = None,
    agc_target: AgcTarget = None,
    max_injection_time_ms: MaxIT = None,
    polarity: Polarity = None,
    microscans: Microscans = None,
    precursor_mz: PrecursorMz = None,
    isolation_width_mz: IsolationWidth = None,
    activation_type: Activation = None,
    collision_energy: CollisionEnergy = None,
    scan_description: Description = None,
    extra_parameters: Extra = None,
    running_number: RunningNumber = None,
    single_processing_delay_s: Annotated[
        float,
        Field(
            ge=0,
            le=600,
            description="Hold further custom scans this long (ICustomScan.SingleProcessingDelay)",
        ),
    ] = 0.0,
) -> ScanSubmission:
    """Place one custom scan to run next (IAPI CreateCustomScan/SetCustomScan); unset values
    fall back to the instrument's defaults. Every value is checked against
    PossibleParameters and the safety limits, and calls are rate-limited, before anything is
    sent. Fetch the result with wait_for_scan(access_id=running_number)."""
    driver = server.driver
    with _submit_lock:
        values = _build_values(
            scan_type=scan_type,
            analyzer=analyzer,
            first_mass_mz=first_mass_mz,
            last_mass_mz=last_mass_mz,
            orbitrap_resolution=orbitrap_resolution,
            agc_target=agc_target,
            max_injection_time_ms=max_injection_time_ms,
            polarity=polarity,
            microscans=microscans,
            precursor_mz=precursor_mz,
            isolation_width_mz=isolation_width_mz,
            activation_type=activation_type,
            collision_energy=collision_energy,
            scan_description=scan_description,
            extra_parameters=extra_parameters,
        )
        server.check(
            "max_custom_scans_per_minute",
            driver.custom_scans_in_window() + 1,
            "custom scans in the last 60 s",
        )
        number = running_number or driver.next_running_number()
        sent = driver.set_custom_scan(values, number, single_processing_delay_s)
        recent = driver.custom_scans_in_window()
    return ScanSubmission(
        sent=sent,
        running_number=number,
        values=values,
        custom_scans_last_minute=recent,
        message=(
            f"Custom scan placed; wait_for_scan(access_id={number}) returns its result."
            if sent
            else "IAPI reports the custom scan could not be sent to the instrument."
        ),
    )


@mcp.tool(**HAZARD)
def set_repeating_scan(
    scan_type: ScanType = None,
    analyzer: Analyzer = None,
    first_mass_mz: FirstMass = None,
    last_mass_mz: LastMass = None,
    orbitrap_resolution: Resolution = None,
    agc_target: AgcTarget = None,
    max_injection_time_ms: MaxIT = None,
    polarity: Polarity = None,
    microscans: Microscans = None,
    precursor_mz: PrecursorMz = None,
    isolation_width_mz: IsolationWidth = None,
    activation_type: Activation = None,
    collision_energy: CollisionEnergy = None,
    scan_description: Description = None,
    extra_parameters: Extra = None,
    running_number: RunningNumber = None,
) -> ScanSubmission:
    """Define or replace the scan the instrument repeats when no method or custom scan is
    running (IAPI CreateRepeatingScan/SetRepetitionScan). Values are validated like
    submit_custom_scan. Cancel with cancel_repeating_scan."""
    driver = server.driver
    with _submit_lock:
        values = _build_values(
            scan_type=scan_type,
            analyzer=analyzer,
            first_mass_mz=first_mass_mz,
            last_mass_mz=last_mass_mz,
            orbitrap_resolution=orbitrap_resolution,
            agc_target=agc_target,
            max_injection_time_ms=max_injection_time_ms,
            polarity=polarity,
            microscans=microscans,
            precursor_mz=precursor_mz,
            isolation_width_mz=isolation_width_mz,
            activation_type=activation_type,
            collision_energy=collision_energy,
            scan_description=scan_description,
            extra_parameters=extra_parameters,
        )
        number = running_number or driver.next_running_number()
        sent = driver.set_repeating_scan(values, number)
    return ScanSubmission(
        sent=sent,
        running_number=number,
        values=values,
        message=(
            f"Repeating scan set; its scans report access_id={number}."
            if sent
            else "IAPI reports the repeating scan could not be sent to the instrument."
        ),
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
