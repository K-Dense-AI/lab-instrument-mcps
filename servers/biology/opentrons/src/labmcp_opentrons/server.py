"""MCP server for Opentrons OT-2 and Flex liquid-handling robots (robot-server HTTP API)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from labmcp import (
    CONTROL,
    HAZARD,
    READ,
    SAFETY,
    ConnectContext,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentServer,
    Limit,
)
from pydantic import BaseModel, Field

from labmcp_opentrons.driver import (
    ACTIVE_STATUSES,
    DEACTIVATE_COMMANDS,
    DEFAULT_API_VERSION,
    ROBOT_MODELS,
    TERMINAL_STATUSES,
    HttpxBackend,
    OpentronsRobot,
    RobotHTTPError,
    deck_layout,
    error_text,
    module_setpoints,
    normalize_base_url,
)
from labmcp_opentrons.simulator import FakeOpentronsRobot

MAX_PROTOCOL_BYTES = 10_000_000


def connect(ctx: ConnectContext) -> OpentronsRobot:
    if ctx.simulate:
        backend: Any = FakeOpentronsRobot(model=ctx.option("sim_model", "flex") or "flex")
        robot = OpentronsRobot(backend, ctx.audit, poll_interval_s=0.1)
    else:
        try:
            base_url = normalize_base_url(ctx.require_address())
        except ValueError as exc:
            raise InstrumentConnectionError(str(exc)) from exc
        backend = HttpxBackend(
            base_url,
            api_version=ctx.option("api_version", DEFAULT_API_VERSION) or DEFAULT_API_VERSION,
            access_token=ctx.option("access_token"),
            timeout=ctx.settings.timeout or 10.0,
        )
        robot = OpentronsRobot(backend, ctx.audit, poll_interval_s=1.0)
    try:
        robot.health()  # fail now (clearly) if the robot is unreachable
    except Exception:
        robot.close()
        raise
    return robot


server = InstrumentServer(
    "Opentrons OT-2 / Flex (HTTP API)",
    connect=connect,
    package="labmcp-opentrons",
    instructions="""
Controls an Opentrons Flex or OT-2 liquid-handling robot through its HTTP API. The robot only
runs complete, analyzed protocols; there is no tool for single pipetting moves.
- Workflow: `get_robot_status` -> `upload_protocol` (or `list_protocols`) -> `get_protocol` ->
  show the user the required deck layout -> `start_run` -> poll `get_run_status`.
- NEVER start a run whose analysis is pending or failed. Report analysis errors to the user; they
  must fix the protocol file and upload it again.
- Before `start_run` or `home_robot`, ask the user to confirm the deck matches the layout from
  `get_protocol` (labware, tips, liquids, modules in the right slots) and that nothing else is on
  the deck or in the robot's path. Only then pass `deck_confirmed=true`.
- Runs started here do NOT apply Labware Position Check offsets from the Opentrons App.
- Flex: the front door must be closed and the E-stop released before a run can start.
- `pause_run` pauses after the current step; `stop_run` cancels the run for good (the robot
  then homes and drops tips). If anything looks wrong, call `stop_run` immediately.
- If a run is `awaiting-recovery`, a step failed (e.g. missing tip). Explain it to the user and
  only `resume_run` after they have physically checked the robot.
- Protocols may leave heaters or shakers on: offer `deactivate_modules` when a run ends.
""",
    limits=[
        Limit("max_module_temperature_c", 110, "°C", "Highest module temperature (block, lid or heater) a protocol may request"),
        Limit("max_shake_speed_rpm", 3000, "rpm", "Fastest Heater-Shaker speed a protocol may request"),
    ],
    address_help="""\
  192.168.1.20                     robot IP (port 31950 is added)
  ot2-lab.local                    mDNS hostname
  http://192.168.1.20:31950        full URL""",
    option_help={
        "api_version": f"Opentrons-Version header to send (default {DEFAULT_API_VERSION})",
        "access_token": "bearer token, only for Flex robots with access control enabled",
        "sim_model": "simulated robot model with --simulate: flex (default) or ot2",
    },
)
mcp = server.mcp


# --------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RobotStatus(BaseModel):
    name: str | None
    model: str | None = Field(description="'Flex' or 'OT-2'")
    serial: str | None
    software_version: str | None = Field(description="Robot software (opentrons) version")
    firmware_version: str | None
    system_version: str | None
    protocol_api_versions: str = Field(description="Supported Python Protocol API versions, e.g. '2.15-2.26'")
    lights_on: bool
    door_open: bool | None = Field(description="Front door open (None if not reported)")
    door_must_be_closed_to_run: bool | None
    estop: str | None = Field(description="E-stop state on Flex ('disengaged' is OK); None on OT-2")
    current_run_id: str | None
    current_run_status: str | None
    ready_to_start_run: bool = Field(description="No active run, door OK, E-stop OK")
    problems: list[str]
    timestamp: str


class InstrumentInfo(BaseModel):
    mount: str | None = Field(description="'left', 'right' or 'extension' (gripper)")
    instrument_type: str = Field(description="'pipette' or 'gripper'")
    name: str | None = Field(description="Pipette name used in protocols, e.g. 'p1000_single_flex'")
    model: str | None
    serial: str | None
    ok: bool = Field(description="False if the robot reports a problem (e.g. firmware update required)")
    channels: int | None = None
    min_volume_ul: float | None = None
    max_volume_ul: float | None = None
    tip_detected: bool | None = None
    calibrated: bool | None = Field(None, description="Whether calibration data is present")
    firmware_version: str | None = None
    problem: str | None = None


class ModuleInfo(BaseModel):
    id: str = Field(description="Module ID on the robot")
    module_type: str = Field(description="e.g. 'Heater-Shaker'")
    model: str
    serial: str | None
    firmware_version: str | None
    update_available: bool
    status: str | None = None
    temperature_c: float | None = None
    target_temperature_c: float | None = None
    lid_temperature_c: float | None = None
    lid_target_temperature_c: float | None = None
    shake_speed_rpm: float | None = None
    target_shake_speed_rpm: float | None = None
    live_data: dict[str, Any] = Field(description="All live data reported by the robot for this module")
    timestamp: str


class ProtocolSummary(BaseModel):
    id: str
    name: str
    protocol_type: str | None = Field(description="'python' or 'json'")
    robot_type: str | None
    api_level: str | None
    created_at: str | None
    files: list[str]
    analysis_status: str | None = Field(description="'pending' or 'completed'")
    analysis_result: str | None = Field(description="'ok', 'not-ok' or 'parameter-value-required'")


class DeckLabware(BaseModel):
    load_name: str | None
    display_name: str | None = None
    location: str


class DeckModule(BaseModel):
    model: str | None
    location: str


class DeckPipette(BaseModel):
    name: str | None
    mount: str | None


class DeckLayout(BaseModel):
    labware: list[DeckLabware]
    modules: list[DeckModule]
    pipettes: list[DeckPipette]
    liquids: list[dict[str, Any]]


class ProtocolDetail(ProtocolSummary):
    analysis_id: str | None
    analysis_errors: list[str] = Field(description="Errors from the robot's protocol analysis")
    ready_to_run: bool = Field(description="True only if analysis completed with result 'ok' and limits pass")
    problems: list[str]
    deck: DeckLayout | None = Field(description="Labware, modules and pipettes the protocol expects")
    command_count: int | None = Field(description="Number of steps in the analyzed protocol")
    max_module_temperature_c: float | None = Field(description="Highest module temperature the protocol requests")
    max_shake_speed_rpm: float | None = Field(description="Fastest Heater-Shaker speed the protocol requests")
    run_time_parameters: list[dict[str, Any]]
    message: str


class RunSummary(BaseModel):
    id: str
    protocol_id: str | None
    status: str
    current: bool
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    error_count: int


class CommandSummary(BaseModel):
    index: int
    command_type: str
    status: str
    error: str | None = None


class RunStatus(BaseModel):
    id: str
    protocol_id: str | None
    status: str = Field(description="idle, running, paused, blocked-by-open-door, stop-requested, finishing, "
                        "awaiting-recovery(-paused/-blocked-by-open-door), stopped, failed, succeeded")
    current: bool
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    current_command: CommandSummary | None
    commands_executed: int = Field(description="Commands queued or run so far")
    commands_expected: int | None = Field(description="Commands in the protocol analysis")
    progress_percent: float | None
    errors: list[str]
    recovering_from: CommandSummary | None
    recent_commands: list[CommandSummary]
    advice: str
    timestamp: str


class RunActionResult(BaseModel):
    run_id: str | None
    action: str
    accepted: bool
    message: str
    status: str | None
    timestamp: str


class ModuleDeactivation(BaseModel):
    module_id: str
    module_type: str
    commands: list[str]
    ok: bool
    error: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_MODULE_NAMES = {
    "temperatureModuleType": "Temperature Module",
    "magneticModuleType": "Magnetic Module",
    "thermocyclerModuleType": "Thermocycler",
    "heaterShakerModuleType": "Heater-Shaker",
    "magneticBlockType": "Magnetic Block",
    "absorbanceReaderType": "Absorbance Plate Reader",
    "flexStackerModuleType": "Flex Stacker",
    "vacuumModuleType": "Vacuum Module",
}

_ADVICE = {
    "idle": "The run has not started. Start it with start_run (a new run) or resume_run.",
    "running": "The protocol is running. Poll get_run_status; pause_run or stop_run if needed.",
    "paused": "The run is paused. resume_run continues it; stop_run cancels it.",
    "blocked-by-open-door": "The front door is open, so the run is paused. Close the door, then resume_run.",
    "stop-requested": "Stopping: the robot will home and drop tips.",
    "finishing": "The run is finishing (homing, dropping tips).",
    "awaiting-recovery": "A step failed and the robot is waiting for error recovery. Tell the user what failed; "
    "after they physically check the robot, resume_run(error_recovery=...) or stop_run.",
    "awaiting-recovery-paused": "Error recovery is paused. resume_run returns to recovery; stop_run cancels.",
    "awaiting-recovery-blocked-by-open-door": "Error recovery with the door open. Close the door first.",
    "stopped": "The run was stopped (cancelled). It cannot be resumed; start a new run if needed.",
    "failed": "The run failed. See `errors`; consider deactivate_modules and check the deck.",
    "succeeded": "The run completed successfully. Consider deactivate_modules if heaters or shakers are on.",
}


def _protocol_name(proto: dict[str, Any]) -> str:
    meta = proto.get("metadata") or {}
    if meta.get("protocolName"):
        return str(meta["protocolName"])
    files = proto.get("files") or []
    main = next((f["name"] for f in files if f.get("role") == "main"), None)
    return main or (files[0]["name"] if files else proto.get("id", "?"))


def _protocol_summary(proto: dict[str, Any]) -> dict[str, Any]:
    summaries = proto.get("analysisSummaries") or []
    last = summaries[-1] if summaries else {}
    return {
        "id": proto["id"],
        "name": _protocol_name(proto),
        "protocol_type": proto.get("protocolType"),
        "robot_type": ROBOT_MODELS.get(proto.get("robotType", ""), proto.get("robotType")),
        "api_level": (proto.get("metadata") or {}).get("apiLevel"),
        "created_at": proto.get("createdAt"),
        "files": [f.get("name", "") for f in proto.get("files") or []],
        "analysis_status": last.get("status"),
        "analysis_result": last.get("result"),
    }


def _protocol_detail(proto: dict[str, Any], analysis: dict[str, Any] | None) -> ProtocolDetail:
    summary = _protocol_summary(proto)
    problems: list[str] = []
    errors: list[str] = []
    deck = None
    count = max_c = max_rpm = None
    rtp: list[dict[str, Any]] = []
    if analysis is None or analysis.get("status") != "completed":
        problems.append("The robot's analysis of this protocol has not finished yet; check again with get_protocol.")
    else:
        errors = error_text(analysis.get("errors"))
        result = analysis.get("result")
        if result == "not-ok":
            problems.append("Protocol analysis FAILED on the robot: fix the protocol and upload it again.")
        elif result == "parameter-value-required":
            problems.append("The protocol needs run-time parameter values (e.g. a CSV file) that this server "
                            "cannot provide; start it from the Opentrons App.")
        elif result != "ok":
            problems.append(f"Unexpected analysis result {result!r}.")
        layout = deck_layout(analysis)
        deck = DeckLayout(**layout)
        commands = analysis.get("commands") or []
        count = len(commands)
        max_c, max_rpm = module_setpoints(commands)
        if max_c is not None and max_c > server.limits["max_module_temperature_c"]:
            problems.append(f"The protocol sets a module to {max_c:g} °C, above the safety limit "
                            f"max_module_temperature_c={server.limits['max_module_temperature_c']:g} °C.")
        if max_rpm is not None and max_rpm > server.limits["max_shake_speed_rpm"]:
            problems.append(f"The protocol shakes at {max_rpm:g} rpm, above the safety limit "
                            f"max_shake_speed_rpm={server.limits['max_shake_speed_rpm']:g} rpm.")
        rtp = [
            {k: p.get(k) for k in ("displayName", "variableName", "type", "value", "default") if k in p}
            for p in analysis.get("runTimeParameters") or []
        ]
    ready = not problems
    if ready:
        message = "Analysis OK. Show the user the deck layout and get confirmation before start_run."
    elif errors:
        message = "NOT READY: " + " ".join(problems) + " Analysis errors: " + " | ".join(errors)
    else:
        message = "NOT READY: " + " ".join(problems)
    return ProtocolDetail(
        **summary,
        analysis_id=(analysis or {}).get("id"),
        analysis_errors=errors,
        ready_to_run=ready,
        problems=problems,
        deck=deck,
        command_count=count,
        max_module_temperature_c=max_c,
        max_shake_speed_rpm=max_rpm,
        run_time_parameters=rtp,
        message=message,
    )


def _current_run_id() -> str | None:
    _runs, current = server.driver.runs(page_length=1)
    return current


def _resolve_run(run_id: str | None) -> str:
    rid = run_id or _current_run_id()
    if not rid:
        raise InstrumentProtocolError("There is no current run on the robot. Use list_runs to find a run ID.")
    return rid


def _cmd_summary(cmd: dict[str, Any], index: int) -> CommandSummary:
    err = cmd.get("error")
    return CommandSummary(
        index=index,
        command_type=str(cmd.get("commandType")),
        status=str(cmd.get("status")),
        error=(error_text([err])[0] if err else None),
    )


def _run_status(run_id: str, recent: int = 5) -> RunStatus:
    drv = server.driver
    run = drv.run(run_id)
    body = drv.run_commands(run_id, page_length=recent)
    page = body.get("data") or []
    meta = body.get("meta") or {}
    links = body.get("links") or {}
    cursor = int(meta.get("cursor") or 0)
    recent_cmds = [_cmd_summary(c, cursor + i) for i, c in enumerate(page)]
    by_id = {c.get("id"): s for c, s in zip(page, recent_cmds, strict=True)}

    def linked(name: str) -> CommandSummary | None:
        link = links.get(name) or {}
        lmeta = link.get("meta") or {}
        if not lmeta:
            return None
        return by_id.get(lmeta.get("commandId")) or CommandSummary(
            index=int(lmeta.get("index", 0)), command_type="?", status="?")

    current = linked("current")
    expected = None
    if run.get("protocolId"):
        try:
            analysis = drv.latest_analysis(drv.protocol(run["protocolId"]))
            if analysis and analysis.get("status") == "completed":
                expected = len(analysis.get("commands") or [])
        except InstrumentProtocolError:
            expected = None
    executed = int(meta.get("totalLength") or 0)
    status = str(run.get("status"))
    progress = None
    if status == "succeeded":
        progress = 100.0
    elif expected:
        done = current.index + (1 if current.status == "succeeded" else 0) if current else 0
        progress = round(min(100.0, 100.0 * done / expected), 1)
    return RunStatus(
        id=run["id"],
        protocol_id=run.get("protocolId"),
        status=status,
        current=bool(run.get("current")),
        created_at=run.get("createdAt"),
        started_at=run.get("startedAt"),
        completed_at=run.get("completedAt"),
        current_command=current,
        commands_executed=executed,
        commands_expected=expected,
        progress_percent=progress,
        errors=error_text(run.get("errors")),
        recovering_from=linked("currentlyRecoveringFrom"),
        recent_commands=recent_cmds,
        advice=_ADVICE.get(status, ""),
        timestamp=_now(),
    )


def _robot_blockers(check_runs: bool = True) -> list[str]:
    """Reasons the robot cannot start moving right now."""
    drv = server.driver
    problems: list[str] = []
    door = drv.door_status()
    if door and door.get("status") == "open" and door.get("doorRequiredClosedForProtocol"):
        problems.append("The front door is open: close it first.")
    estop = drv.estop_status()
    if estop and estop.get("status") not in {"disengaged", "notPresent"}:
        problems.append(f"E-stop is {estop.get('status')}: release it and acknowledge on the robot first.")
    rid = _current_run_id() if check_runs else None
    if rid:
        status = drv.run(rid).get("status")
        if status in ACTIVE_STATUSES:
            problems.append(f"Run {rid} is {status}: stop it (stop_run) or let it finish first.")
    return problems


# --------------------------------------------------------------------------
# Robot
# --------------------------------------------------------------------------


@mcp.tool(**READ)
def get_robot_status() -> RobotStatus:
    """Report the robot's name, model (Flex/OT-2), software and firmware versions, lights, door and
    E-stop state, and the current run. Call this first to check the robot is ready."""
    drv = server.driver
    h = drv.health()
    door = drv.door_status()
    estop = drv.estop_status()
    rid = _current_run_id()
    run_status = drv.run(rid).get("status") if rid else None
    problems = _robot_blockers()
    lo, hi = h.get("minimum_protocol_api_version") or [], h.get("maximum_protocol_api_version") or []
    return RobotStatus(
        name=h.get("name"),
        model=ROBOT_MODELS.get(h.get("robot_model", ""), h.get("robot_model")),
        serial=h.get("robot_serial"),
        software_version=h.get("api_version"),
        firmware_version=h.get("fw_version"),
        system_version=h.get("system_version"),
        protocol_api_versions="-".join(".".join(str(x) for x in v) for v in (lo, hi) if v),
        lights_on=drv.lights_on(),
        door_open=(door.get("status") == "open") if door else None,
        door_must_be_closed_to_run=door.get("doorRequiredClosedForProtocol") if door else None,
        estop=(estop or {}).get("status"),
        current_run_id=rid,
        current_run_status=run_status,
        ready_to_start_run=not problems,
        problems=problems,
        timestamp=_now(),
    )


@mcp.tool(**READ)
def list_instruments() -> list[InstrumentInfo]:
    """List attached pipettes (and the Flex gripper): mount, name, channels, volume range, and whether
    a tip is detected and calibration data exists."""
    out: list[InstrumentInfo] = []
    for inst in server.driver.instruments():
        data = inst.get("data") or {}
        ok = bool(inst.get("ok", True))
        out.append(
            InstrumentInfo(
                mount=inst.get("mount"),
                instrument_type=str(inst.get("instrumentType", "?")),
                name=inst.get("instrumentName"),
                model=inst.get("instrumentModel"),
                serial=inst.get("serialNumber"),
                ok=ok,
                channels=data.get("channels"),
                min_volume_ul=data.get("min_volume"),
                max_volume_ul=data.get("max_volume"),
                tip_detected=(inst.get("state") or {}).get("tipDetected"),
                calibrated=("calibratedOffset" in data and data["calibratedOffset"] is not None) if ok else None,
                firmware_version=inst.get("firmwareVersion"),
                problem=None if ok else f"{inst.get('status', 'not ok')} (update: {inst.get('update', '?')})",
            )
        )
    return out


@mcp.tool(**READ)
def list_modules() -> list[ModuleInfo]:
    """List attached modules (Temperature Module, Heater-Shaker, Thermocycler, Magnetic Module,
    Absorbance Plate Reader, ...) with live temperatures, targets, shake speed and status."""
    out: list[ModuleInfo] = []
    for mod in server.driver.modules():
        d = mod.get("data") or {}
        out.append(
            ModuleInfo(
                id=mod["id"],
                module_type=_MODULE_NAMES.get(mod.get("moduleType", ""), str(mod.get("moduleType"))),
                model=str(mod.get("moduleModel")),
                serial=mod.get("serialNumber"),
                firmware_version=mod.get("firmwareVersion"),
                update_available=bool(mod.get("hasAvailableUpdate")),
                status=d.get("status"),
                temperature_c=d.get("currentTemperature"),
                target_temperature_c=d.get("targetTemperature"),
                lid_temperature_c=d.get("lidTemperature"),
                lid_target_temperature_c=d.get("lidTargetTemperature"),
                shake_speed_rpm=d.get("currentSpeed"),
                target_shake_speed_rpm=d.get("targetSpeed"),
                live_data=d,
                timestamp=_now(),
            )
        )
    return out


@mcp.tool(**CONTROL)
def set_lights(on: Annotated[bool, Field(description="True to turn the deck lights on")]) -> str:
    """Turn the robot's deck (rail) lights on or off."""
    state = server.driver.set_lights(on)
    return f"Deck lights {'on' if state else 'off'}."


@mcp.tool(**HAZARD, timeout=200)
def home_robot() -> str:
    """Home all axes of the robot (the gantry and pipettes move to their home positions). Refused
    while a run is active. Make sure nothing is in the robot's path and the door is closed."""
    blockers = _robot_blockers()
    if blockers:
        raise InstrumentProtocolError("Refused to home: " + " ".join(blockers))
    server.driver.home(timeout_s=150)
    return "Robot homed."


@mcp.tool(**SAFETY, timeout=120)
def deactivate_modules(
    module_ids: Annotated[
        list[str] | None, Field(description="Module IDs from list_modules; omit to switch off all modules")
    ] = None,
) -> list[ModuleDeactivation]:
    """Switch attached modules off: stop heating/cooling (Temperature Module, Thermocycler block and
    lid, Heater-Shaker heater), stop shaking, and lower Magnetic Module magnets. Only possible when
    no run is in progress: stop the run first."""
    drv = server.driver
    results: list[ModuleDeactivation] = []
    for mod in drv.modules():
        if module_ids and mod["id"] not in module_ids:
            continue
        mtype = str(mod.get("moduleType"))
        commands = DEACTIVATE_COMMANDS.get(mtype, [])
        if not commands:
            continue
        # Send every deactivate command even if an earlier one fails (e.g. a Heater-Shaker whose
        # deactivateShaker times out must still get deactivateHeater), and keep going with the
        # remaining modules on any instrument error, including timeouts.
        errors: list[str] = []
        for ctype in commands:
            try:
                drv.stateless_command(ctype, {"moduleId": mod["id"]}, timeout_s=30)
            except RobotHTTPError as exc:
                msg = f"{ctype}: {exc}"
                if exc.status == 409:
                    msg += " Stop the current run first (stop_run), then try again."
                errors.append(msg)
            except InstrumentError as exc:
                errors.append(f"{ctype}: {exc}")
        error = " | ".join(errors) if errors else None
        results.append(
            ModuleDeactivation(module_id=mod["id"], module_type=_MODULE_NAMES.get(mtype, mtype),
                               commands=commands, ok=error is None, error=error)
        )
    return results


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------


@mcp.tool(**READ)
def list_protocols() -> list[ProtocolSummary]:
    """List protocols stored on the robot (newest last) with their analysis status and result."""
    return [ProtocolSummary(**_protocol_summary(p)) for p in server.driver.protocols()]


@mcp.tool(**READ)
def get_protocol(protocol_id: Annotated[str, Field(description="Protocol ID from list_protocols")]) -> ProtocolDetail:
    """Show a stored protocol's analysis: whether it is ready to run, analysis errors, the deck layout it
    expects (labware and modules per slot, pipettes per mount, liquids), the number of steps and the
    highest module temperature / shake speed it requests. Review this with the user before start_run."""
    drv = server.driver
    proto = drv.protocol(protocol_id)
    return _protocol_detail(proto, drv.latest_analysis(proto))


@mcp.tool(**CONTROL, timeout=420)
def upload_protocol(
    path: Annotated[str, Field(description="Local path of the protocol file (.py Python API or .json)")],
    labware_paths: Annotated[
        list[str] | None, Field(description="Custom labware definition .json files used by a Python protocol")
    ] = None,
    wait_for_analysis_s: Annotated[
        float, Field(ge=0, le=300, description="How long to wait for the robot's analysis to finish")
    ] = 90,
) -> ProtocolDetail:
    """Upload a protocol file (plus optional custom labware) to the robot. The robot analyzes it
    (simulates it) and this tool returns the analysis: errors, deck layout and whether it is ready
    to run. Uploading does not move the robot. Uploading identical files returns the existing protocol."""
    main = Path(path).expanduser()
    files: list[tuple[str, bytes]] = []
    for p, kinds in [(main, {".py", ".json"})] + [(Path(lp).expanduser(), {".json"}) for lp in labware_paths or []]:
        if not p.is_file():
            raise InstrumentProtocolError(f"File not found: {p}")
        if p.suffix.lower() not in kinds:
            raise InstrumentProtocolError(f"{p.name}: expected a {' or '.join(sorted(kinds))} file")
        size = p.stat().st_size
        if size > MAX_PROTOCOL_BYTES:
            raise InstrumentProtocolError(f"{p.name} is {size / 1e6:.1f} MB; protocol files are limited to 10 MB")
        files.append((p.name, p.read_bytes()))
    drv = server.driver
    proto = drv.upload_protocol(files)
    proto, analysis = drv.wait_for_analysis(proto["id"], wait_for_analysis_s)
    return _protocol_detail(proto, analysis)


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@mcp.tool(**READ)
def list_runs(limit: Annotated[int, Field(ge=1, le=50, description="Maximum runs to list")] = 10) -> list[RunSummary]:
    """List recent protocol runs, newest first, with their status."""
    runs, _current = server.driver.runs(page_length=limit)
    return [
        RunSummary(
            id=r["id"],
            protocol_id=r.get("protocolId"),
            status=str(r.get("status")),
            current=bool(r.get("current")),
            created_at=r.get("createdAt"),
            started_at=r.get("startedAt"),
            completed_at=r.get("completedAt"),
            error_count=len(r.get("errors") or []),
        )
        for r in reversed(runs)
    ]


@mcp.tool(**READ)
def get_run_status(
    run_id: Annotated[str | None, Field(description="Run ID; omit for the current run")] = None,
    recent_commands: Annotated[int, Field(ge=1, le=50, description="How many recent steps to include")] = 5,
) -> RunStatus:
    """Report a run's status, the step it is on, progress, recent steps and any errors, plus advice on
    what to do next (e.g. when the run is waiting for error recovery)."""
    return _run_status(_resolve_run(run_id), recent_commands)


@mcp.tool(**HAZARD, timeout=180)
def start_run(
    protocol_id: Annotated[str, Field(description="Protocol ID from upload_protocol or list_protocols")],
    deck_confirmed: Annotated[
        bool,
        Field(description="True only after the user confirmed the deck matches get_protocol's layout "
              "(labware, tips, liquids, modules) and nothing else is in the robot's path"),
    ],
) -> RunStatus:
    """Create a run of an analyzed protocol and start it: the robot begins moving and pipetting.
    Refused unless the protocol's analysis completed without errors, its module setpoints are within
    the safety limits, no other run is active, the door is closed and the E-stop is released."""
    if not deck_confirmed:
        raise InstrumentProtocolError(
            "Refused: deck not confirmed. Call get_protocol, show the user the required deck layout, and "
            "call start_run again with deck_confirmed=true once they confirm the deck is set up and clear."
        )
    drv = server.driver
    proto = drv.protocol(protocol_id)
    detail = _protocol_detail(proto, drv.latest_analysis(proto))
    if detail.max_module_temperature_c is not None:
        server.check("max_module_temperature_c", detail.max_module_temperature_c, "module temperature in protocol")
    if detail.max_shake_speed_rpm is not None:
        server.check("max_shake_speed_rpm", detail.max_shake_speed_rpm, "Heater-Shaker speed in protocol")
    if not detail.ready_to_run:
        raise InstrumentProtocolError("Refused to start: " + detail.message)
    blockers = _robot_blockers()
    if blockers:
        raise InstrumentProtocolError("Refused to start: " + " ".join(blockers))
    run = drv.create_run(protocol_id)
    if run.get("errors"):
        raise InstrumentProtocolError(
            f"Run {run['id']} was created but has errors, so it was not started: " + " | ".join(error_text(run["errors"]))
        )
    drv.run_action(run["id"], "play")
    return _run_status(run["id"])


@mcp.tool(**SAFETY)
def pause_run(run_id: Annotated[str | None, Field(description="Run ID; omit for the current run")] = None) -> RunActionResult:
    """Pause a running protocol. The robot finishes its current step and then holds; resume_run
    continues it."""
    drv = server.driver
    rid = _resolve_run(run_id)
    status = drv.run(rid).get("status")
    if status != "running":
        return RunActionResult(run_id=rid, action="pause", accepted=False, status=status, timestamp=_now(),
                               message=f"Not paused: the run is {status}, not running.")
    drv.run_action(rid, "pause")
    return RunActionResult(run_id=rid, action="pause", accepted=True, status=drv.run(rid).get("status"),
                           timestamp=_now(), message="Pause requested; the robot stops after the current step.")


@mcp.tool(**SAFETY)
def stop_run(run_id: Annotated[str | None, Field(description="Run ID; omit for the current run")] = None) -> RunActionResult:
    """Stop (cancel) a run immediately. A stopped run cannot be resumed; the robot homes and drops
    any attached tips into the trash. Use this whenever something looks wrong."""
    drv = server.driver
    rid = run_id or _current_run_id()
    if not rid:
        return RunActionResult(run_id=None, action="stop", accepted=False, status=None, timestamp=_now(),
                               message="There is no current run to stop.")
    status = drv.run(rid).get("status")
    if status in TERMINAL_STATUSES:
        return RunActionResult(run_id=rid, action="stop", accepted=False, status=status, timestamp=_now(),
                               message=f"The run already ended ({status}).")
    drv.run_action(rid, "stop")
    return RunActionResult(run_id=rid, action="stop", accepted=True, status=drv.run(rid).get("status"),
                           timestamp=_now(), message="Stop requested. The robot will home and drop tips.")


@mcp.tool(**HAZARD)
def resume_run(
    run_id: Annotated[str | None, Field(description="Run ID; omit for the current run")] = None,
    error_recovery: Annotated[
        Literal["continue", "assume_false_positive"] | None,
        Field(description="Only for runs awaiting error recovery, after the user physically checked the robot: "
              "'continue' resumes from the robot's current state (the failed step is skipped); "
              "'assume_false_positive' treats the failure as a false alarm (e.g. the tip really was picked up)"),
    ] = None,
) -> RunStatus:
    """Resume a paused run (the robot starts moving again), or leave error recovery. Refused while the
    door is open. For a run awaiting recovery, `error_recovery` must be given."""
    drv = server.driver
    rid = _resolve_run(run_id)
    status = str(drv.run(rid).get("status"))
    if status in {"blocked-by-open-door", "awaiting-recovery-blocked-by-open-door"}:
        raise InstrumentProtocolError("Refused: the robot door is open. Close it, then resume.")
    if status == "awaiting-recovery":
        if error_recovery is None:
            raise InstrumentProtocolError(
                "The run is awaiting error recovery. Tell the user which step failed (get_run_status). Only after "
                "they checked the robot, call resume_run with error_recovery='continue' or 'assume_false_positive', "
                "or stop_run."
            )
        action = "resume-from-recovery" if error_recovery == "continue" else "resume-from-recovery-assuming-false-positive"
    elif status in {"paused", "awaiting-recovery-paused"}:
        action = "play"
    else:
        raise InstrumentProtocolError(f"Nothing to resume: the run is {status}.")
    blockers = _robot_blockers(check_runs=False)
    if blockers:
        raise InstrumentProtocolError("Refused to resume: " + " ".join(blockers))
    drv.run_action(rid, action)
    return _run_status(rid)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
