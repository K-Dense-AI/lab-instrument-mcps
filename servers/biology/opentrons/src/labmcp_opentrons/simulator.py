"""In-memory simulator of an Opentrons robot-server (Flex or OT-2).

:class:`FakeOpentronsRobot` implements the same ``request()`` interface as the real
HTTP backend, so ``--simulate`` exercises the driver's request building, JSON
parsing and error handling. It reproduces the response shapes of the endpoints the
driver uses (``{"data": ...}`` bodies, ``meta``/``links`` on collections, and
``{"errors": [{"id", "title", "detail"}]}`` error bodies), the run lifecycle
(``idle -> running -> succeeded``, ``paused``, ``stop-requested -> stopped``,
``blocked-by-open-door``, ``awaiting-recovery``, ``failed``) and the action rules
of the protocol engine (e.g. you cannot pause a run that is not running).

Protocol "analysis" is a light-weight imitation: Python files are compiled (syntax
errors fail the analysis like on a robot) and scanned for ``load_labware``,
``load_instrument``, ``load_module`` and common pipetting / module calls to build a
plausible command list. Markers in a protocol trigger simulated failures:

* ``SIMULATE_ANALYSIS_ERROR``     analysis completes with result ``not-ok``
* ``SIMULATE_RECOVERABLE_ERROR``  the run enters ``awaiting-recovery`` half way through
* ``SIMULATE_FATAL_ERROR``        the run ``failed`` half way through
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from labmcp_opentrons.driver import TERMINAL_STATUSES, HTTPResponse

AMBIENT_C = 23.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _err(status: int, ident: str, title: str, detail: str, code: str = "4000") -> HTTPResponse:
    return HTTPResponse(status, {"errors": [{"id": ident, "title": title, "detail": detail, "errorCode": code}]})


_MODULE_MODELS = {
    "temperature module gen2": "temperatureModuleV2",
    "temperaturemodulev2": "temperatureModuleV2",
    "temperature module": "temperatureModuleV1",
    "heatershakermodulev1": "heaterShakerModuleV1",
    "heater-shaker module": "heaterShakerModuleV1",
    "thermocycler module gen2": "thermocyclerModuleV2",
    "thermocyclermodulev2": "thermocyclerModuleV2",
    "thermocycler": "thermocyclerModuleV1",
    "magnetic block": "magneticBlockV1",
    "magneticblockv1": "magneticBlockV1",
    "magnetic module gen2": "magneticModuleV2",
    "absorbancereaderv1": "absorbanceReaderV1",
}

_LW_RE = re.compile(
    r"load_labware\(\s*['\"]([\w.-]+)['\"](?:\s*,\s*(?:location\s*=\s*)?['\"]?([A-D][1-4]|1[0-2]|[1-9])['\"]?)?"
)
_PIP_RE = re.compile(r"load_instrument\(\s*['\"]([\w.-]+)['\"]\s*,\s*(?:mount\s*=\s*)?['\"](left|right)['\"]")
_MOD_RE = re.compile(
    r"load_module\(\s*['\"]([\w\s.-]+)['\"](?:\s*,\s*(?:location\s*=\s*)?['\"]?([A-D][1-4]|1[0-2]|[1-9])['\"]?)?"
)
_NUM = r"(?:celsius\s*=\s*|temperature\s*=\s*)?(-?\d+(?:\.\d+)?)"
_SETPOINTS = [
    (re.compile(r"\.set_temperature\(\s*" + _NUM), "temperatureModule/setTargetTemperature"),
    (re.compile(r"\.set_block_temperature\(\s*" + _NUM), "thermocycler/setTargetBlockTemperature"),
    (re.compile(r"\.set_lid_temperature\(\s*" + _NUM), "thermocycler/setTargetLidTemperature"),
    (re.compile(r"\.set_and_wait_for_temperature\(\s*" + _NUM), "heaterShaker/setTargetTemperature"),
    (re.compile(r"\.set_target_temperature\(\s*" + _NUM), "heaterShaker/setTargetTemperature"),
]
_SHAKE_RE = re.compile(r"\.set_and_wait_for_shake_speed\(\s*(?:rpm\s*=\s*)?(\d+(?:\.\d+)?)")
_STEPS = {
    ".transfer(": ["pickUpTip", "aspirate", "dispense", "blowout", "dropTip"],
    ".distribute(": ["pickUpTip", "aspirate", "dispense", "dispense", "dropTip"],
    ".consolidate(": ["pickUpTip", "aspirate", "aspirate", "dispense", "dropTip"],
    ".pick_up_tip(": ["pickUpTip"],
    ".aspirate(": ["aspirate"],
    ".dispense(": ["dispense"],
    ".mix(": ["aspirate", "dispense", "aspirate", "dispense"],
    ".blow_out(": ["blowout"],
    ".drop_tip(": ["dropTip"],
    ".move_labware(": ["moveLabware"],
    ".comment(": ["comment"],
    ".delay(": ["waitForDuration"],
    ".deactivate(": ["deactivate"],
}


@dataclass
class _Protocol:
    id: str
    created_at: str
    files: list[dict[str, str]]
    protocol_type: str
    robot_type: str
    metadata: dict[str, Any]
    digest: str
    analysis_id: str
    analysis: dict[str, Any]
    ready_at: float
    markers: set[str] = field(default_factory=set)

    def summary(self, now: float) -> dict[str, Any]:
        done = now >= self.ready_at
        return {
            "id": self.analysis_id,
            "status": "completed" if done else "pending",
            **({"result": self.analysis["result"]} if done else {}),
            "runTimeParameters": [],
        }

    def resource(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "createdAt": self.created_at,
            "files": self.files,
            "protocolType": self.protocol_type,
            "robotType": self.robot_type,
            "metadata": self.metadata,
            "analyses": [],
            "analysisSummaries": [self.summary(now)],
            "key": None,
            "protocolKind": "standard",
        }


@dataclass
class _Run:
    id: str
    protocol_id: str | None
    created_at: str
    plan: list[dict[str, Any]]
    markers: set[str]
    status: str = "idle"
    current: bool = True
    actions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None
    recovered: bool = False
    budget_s: float = 0.0
    last_tick: float = 0.0
    fail_index: int | None = None

    def resource(self, analysis: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "ok": True,
            "id": self.id,
            "createdAt": self.created_at,
            "status": self.status,
            "current": self.current,
            "actions": self.actions,
            "errors": self.errors,
            "hasEverEnteredErrorRecovery": self.recovered,
            "pipettes": (analysis or {}).get("pipettes", []),
            "modules": (analysis or {}).get("modules", []),
            "labware": (analysis or {}).get("labware", []),
            "liquids": [],
            "liquidClasses": [],
            "labwareOffsets": [],
            "runTimeParameters": [],
            "outputFileIds": [],
            "protocolId": self.protocol_id,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "logPeriodId": None,
        }


class FakeOpentronsRobot:
    """A simulated robot-server. ``model`` is ``"flex"`` or ``"ot2"``.

    ``clock`` returns seconds (monotonic); tests can pass a controllable clock.
    Each protocol command takes ``command_duration_s`` of simulated run time.
    """

    def __init__(
        self,
        model: str = "flex",
        *,
        command_duration_s: float = 0.4,
        analysis_duration_s: float = 0.3,
        clock: Callable[[], float] | None = None,
        api_version: str = "3",
    ) -> None:
        self.model = "ot2" if model.lower().replace("-", "") in {"ot2", "ot2standard"} else "flex"
        self.robot_type = "OT-2 Standard" if self.model == "ot2" else "OT-3 Standard"
        self.api_version = api_version
        self.description = f"sim://FakeOpentronsRobot ({'OT-2' if self.model == 'ot2' else 'Flex'})"
        self.command_duration_s = command_duration_s
        self.analysis_duration_s = analysis_duration_s
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.lights = False
        self.door_open = False
        self.estop = "disengaged" if self.model == "flex" else "notPresent"
        self.protocols: dict[str, _Protocol] = {}
        self.runs: dict[str, _Run] = {}
        self.stateless: list[dict[str, Any]] = []
        self.homed = False
        self._last_module_tick = self.clock()
        self.attached_modules = self._default_modules()

    # ------------------------------------------------------------ plumbing

    def close(self) -> None:
        pass

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        timeout: float | None = None,
    ) -> HTTPResponse:
        if not self.api_version:
            return HTTPResponse(422, {"errors": [{"id": "InvalidRequest", "title": "Invalid Request",
                                                  "detail": "Opentrons-Version header required"}]})
        params = params or {}
        parts = [p for p in path.split("/") if p]
        with self._lock:
            self._advance()
            route = (method.upper(), *parts)
            match route:
                case ("GET", "health"):
                    return HTTPResponse(200, self._health())
                case ("GET", "robot", "lights"):
                    return HTTPResponse(200, {"on": self.lights})
                case ("POST", "robot", "lights"):
                    if not isinstance(json, dict) or not isinstance(json.get("on"), bool):
                        return HTTPResponse(422, {"message": "on: field required", "errorCode": "4000"})
                    self.lights = json["on"]
                    return HTTPResponse(200, {"on": self.lights})
                case ("GET", "robot", "door", "status"):
                    return HTTPResponse(200, {"data": {
                        "status": "open" if self.door_open else "closed",
                        "doorRequiredClosedForProtocol": self.model == "flex",
                    }})
                case ("GET", "robot", "control", "estopStatus"):
                    if self.model == "ot2":
                        return _err(403, "NotSupportedOnOT2", "Not Supported On OT-2",
                                    "This endpoint is only available on Flex robots.")
                    return HTTPResponse(200, {"data": {"status": self.estop, "leftEstopPhysicalStatus": self.estop,
                                                       "rightEstopPhysicalStatus": "notPresent"}})
                case ("GET", "instruments"):
                    data = self._instruments()
                    return HTTPResponse(200, {"data": data, "meta": {"cursor": 0, "totalLength": len(data)}})
                case ("GET", "modules"):
                    data = [self._module_resource(m) for m in self.attached_modules]
                    return HTTPResponse(200, {"data": data, "meta": {"cursor": 0, "totalLength": len(data)}})
                case ("GET", "protocols"):
                    data = [p.resource(self.clock()) for p in self.protocols.values()]
                    return HTTPResponse(200, {"data": data, "meta": {"cursor": 0, "totalLength": len(data)}})
                case ("POST", "protocols"):
                    return self._upload(files or [])
                case ("GET", "protocols", pid):
                    proto = self.protocols.get(pid)
                    if proto is None:
                        return _err(404, "ProtocolNotFound", "Protocol Not Found", f"Protocol {pid} not found.")
                    return HTTPResponse(200, {"data": proto.resource(self.clock())})
                case ("GET", "protocols", pid, "analyses", aid):
                    return self._get_analysis(pid, aid)
                case ("GET", "runs"):
                    return self._list_runs(params)
                case ("POST", "runs"):
                    return self._create_run(json)
                case ("GET", "runs", rid):
                    run = self.runs.get(rid)
                    if run is None:
                        return _err(404, "RunNotFound", "Run Not Found", f"Run {rid} not found.")
                    return HTTPResponse(200, {"data": run.resource(self._run_analysis(run))})
                case ("POST", "runs", rid, "actions"):
                    return self._action(rid, json)
                case ("GET", "runs", rid, "commands"):
                    return self._run_commands(rid, params)
                case ("POST", "commands"):
                    return self._stateless(json)
            return _err(404, "RouteNotFound", "Route Not Found", f"{method} {path} is not a valid route.")

    # ------------------------------------------------------------ test hooks

    def open_door(self) -> None:
        with self._lock:
            self._advance()
            self.door_open = True
            for run in self.runs.values():
                if run.status == "running" and self.model == "flex":
                    run.status = "blocked-by-open-door"
                elif run.status == "awaiting-recovery" and self.model == "flex":
                    run.status = "awaiting-recovery-blocked-by-open-door"

    def close_door(self) -> None:
        with self._lock:
            self.door_open = False
            for run in self.runs.values():
                if run.status == "blocked-by-open-door":
                    run.status = "paused"
                elif run.status == "awaiting-recovery-blocked-by-open-door":
                    run.status = "awaiting-recovery-paused"

    # ------------------------------------------------------------ robot

    def _health(self) -> dict[str, Any]:
        flex = self.model == "flex"
        return {
            "name": "SimFlex" if flex else "SimOT2",
            "robot_model": self.robot_type,
            "api_version": "8.6.0",
            "fw_version": "Flex-sim v1" if flex else "v1.1.0-sim",
            "board_revision": "sim" if flex else "2.1",
            "logs": ["/logs/serial.log", "/logs/api.log", "/logs/server.log"],
            "system_version": "v1.19.0" if flex else "1.17.0",
            "maximum_protocol_api_version": [2, 26],
            "minimum_protocol_api_version": [2, 15] if flex else [2, 0],
            "robot_serial": "FLXA1020240101001" if flex else "OT2CEP20240101A01",
            "links": {"apiLog": "/logs/api.log", "serialLog": "/logs/serial.log", "serverLog": "/logs/server.log",
                      "apiSpec": "/openapi.json", "systemTime": "/system/time"},
        }

    def _instruments(self) -> list[dict[str, Any]]:
        cal = {"offset": {"x": 0.12, "y": -0.31, "z": 0.05}, "source": "user", "last_modified": "2026-01-15T10:00:00Z",
               "reasonability_check_failures": []}
        if self.model == "ot2":
            return [
                {"mount": "left", "instrumentType": "pipette", "instrumentModel": "p300_single_v2.1",
                 "instrumentName": "p300_single_gen2", "serialNumber": "P3HSV2120230101A01", "ok": True,
                 "data": {"channels": 1, "min_volume": 20.0, "max_volume": 300.0}},
                {"mount": "right", "instrumentType": "pipette", "instrumentModel": "p20_multi_v2.1",
                 "instrumentName": "p20_multi_gen2", "serialNumber": "P20MV2120230101A02", "ok": True,
                 "data": {"channels": 8, "min_volume": 1.0, "max_volume": 20.0}},
            ]
        return [
            {"mount": "left", "instrumentType": "pipette", "instrumentModel": "p1000_single_v3.6",
             "instrumentName": "p1000_single_flex", "serialNumber": "P1KSV3620240101A01", "subsystem": "pipette_left",
             "ok": True, "firmwareVersion": "38",
             "data": {"channels": 1, "min_volume": 5.0, "max_volume": 1000.0, "calibratedOffset": cal},
             "state": {"tipDetected": False}},
            {"mount": "right", "instrumentType": "pipette", "instrumentModel": "p50_multi_v3.5",
             "instrumentName": "p50_multi_flex", "serialNumber": "P50MV3520240101A02", "subsystem": "pipette_right",
             "ok": True, "firmwareVersion": "38",
             "data": {"channels": 8, "min_volume": 1.0, "max_volume": 50.0, "calibratedOffset": cal},
             "state": {"tipDetected": False}},
            {"mount": "extension", "instrumentType": "gripper", "instrumentModel": "gripperV1.3",
             "serialNumber": "GRPV1320240101A01", "subsystem": "gripper", "ok": True, "firmwareVersion": "12",
             "data": {"jawState": "homed", "calibratedOffset": cal}},
        ]

    def _default_modules(self) -> list[dict[str, Any]]:
        def ident(serial: str, rev: str) -> str:
            return hashlib.blake2s((serial + rev).encode(), digest_size=20).hexdigest()

        mods = [
            {"serial": "TDV21P20240101A01", "type": "temperatureModuleType", "model": "temperatureModuleV2",
             "rev": "temp_deck_v21", "fw": "v2.1.0", "temp": 22.4, "target": None},
            {"serial": "HSM1S20240101A02", "type": "heaterShakerModuleType", "model": "heaterShakerModuleV1",
             "rev": "hs_v1", "fw": "v1.0.5", "temp": 23.1, "target": None, "rpm": 0, "target_rpm": None},
        ]
        if self.model == "flex":
            mods += [
                {"serial": "TC2PVS20240101A03", "type": "thermocyclerModuleType", "model": "thermocyclerModuleV2",
                 "rev": "thermocycler_v2", "fw": "v1.2.0", "temp": 23.5, "target": None, "lid_temp": 23.9,
                 "lid_target": None},
                {"serial": "OPTMAA00034", "type": "absorbanceReaderType", "model": "absorbanceReaderV1",
                 "rev": "abs_v1", "fw": "v1.0.2"},
            ]
        for i, m in enumerate(mods):
            m["id"] = ident(m["serial"], m["rev"])
            m["port"] = i + 1
        return mods

    def _module_resource(self, m: dict[str, Any]) -> dict[str, Any]:
        def tstatus(cur: float, target: float | None) -> str:
            if target is None:
                return "idle"
            if abs(cur - target) < 0.5:
                return "holding at target"
            return "heating" if target > cur else "cooling"

        t = m["type"]
        if t == "temperatureModuleType":
            data: dict[str, Any] = {"status": tstatus(m["temp"], m["target"]), "currentTemperature": round(m["temp"], 1),
                                    "targetTemperature": m["target"]}
        elif t == "heaterShakerModuleType":
            data = {"status": "running" if (m["target"] is not None or m["rpm"]) else "idle",
                    "labwareLatchStatus": "idle_closed",
                    "speedStatus": "holding at target" if m["rpm"] else "idle", "currentSpeed": int(m["rpm"]),
                    "targetSpeed": m["target_rpm"], "temperatureStatus": tstatus(m["temp"], m["target"]),
                    "currentTemperature": round(m["temp"], 1), "targetTemperature": m["target"], "errorDetails": None}
        elif t == "thermocyclerModuleType":
            data = {"status": tstatus(m["temp"], m["target"]), "currentTemperature": round(m["temp"], 1),
                    "targetTemperature": m["target"], "lidStatus": "closed",
                    "lidTemperatureStatus": tstatus(m["lid_temp"], m["lid_target"]),
                    "lidTemperature": round(m["lid_temp"], 1), "lidTargetTemperature": m["lid_target"],
                    "holdTime": None, "rampRate": None, "currentCycleIndex": None, "totalCycleCount": None,
                    "currentStepIndex": None, "totalStepCount": None}
        else:
            data = {"status": "idle", "lidStatus": "on", "platePresence": "absent", "measureMode": "",
                    "sampleWavelengths": [], "referenceWavelength": None, "errorDetails": None}
        return {
            "id": m["id"], "serialNumber": m["serial"], "firmwareVersion": m["fw"], "hardwareRevision": m["rev"],
            "hasAvailableUpdate": False, "moduleType": t, "moduleModel": m["model"], "compatibleWithRobot": True,
            "moduleOffset": None, "data": data,
            "usbPort": {"port": m["port"], "portGroup": "main", "hub": False, "path": f"/dev/ot_module_{m['port']}"},
        }

    def _tick_modules(self, now: float) -> None:
        dt = max(0.0, now - self._last_module_tick)
        self._last_module_tick = now
        for m in self.attached_modules:
            for cur, tgt, tau in (("temp", "target", 20.0), ("lid_temp", "lid_target", 30.0)):
                if cur not in m:
                    continue
                goal = m[tgt] if m[tgt] is not None else AMBIENT_C
                m[cur] = goal + (m[cur] - goal) * math.exp(-dt / (tau if m[tgt] is not None else 300.0))

    # ------------------------------------------------------------ protocols

    def _upload(self, files: list[tuple[str, tuple[str, bytes, str]]]) -> HTTPResponse:
        mains = [(n, d) for _, (n, d, _c) in files if n.lower().endswith((".py", ".json"))]
        py = [(n, d) for n, d in mains if n.lower().endswith(".py")]
        if not files:
            return _err(422, "ProtocolFilesInvalid", "Protocol File(s) Invalid", "No files were uploaded.")
        if len(py) > 1:
            return _err(422, "ProtocolFilesInvalid", "Protocol File(s) Invalid",
                        "Upload a single Python protocol file and 0 or more custom labware JSON files.")
        name, data = py[0] if py else mains[0]
        text = data.decode("utf-8", "replace")
        digest = hashlib.sha256(b"".join(d for _, (_n, d, _c) in files)).hexdigest()
        for proto in self.protocols.values():
            if proto.digest == digest:
                return HTTPResponse(200, {"data": proto.resource(self.clock())})
        if name.lower().endswith(".py"):
            parsed = self._parse_python(name, text)
        else:
            parsed = self._parse_json(name, text)
        if isinstance(parsed, HTTPResponse):
            return parsed
        robot_type, metadata, analysis, markers = parsed
        if robot_type != self.robot_type:
            wanted = "Flex" if robot_type == "OT-3 Standard" else "OT-2"
            return _err(422, "ProtocolRobotTypeMismatch", "Protocol For Different Robot Type",
                        f"This protocol is for {wanted} robots. It can't be analyzed or run on this robot.")
        pid, aid = _uid(), _uid()
        files_meta = [{"name": n, "role": "main" if n == name else "labware"} for _, (n, _d, _c) in files]
        proto = _Protocol(pid, _now_iso(), files_meta, "python" if name.endswith(".py") else "json", robot_type,
                          metadata, digest, aid, {"id": aid, "status": "completed", **analysis},
                          self.clock() + self.analysis_duration_s, markers)
        self.protocols[pid] = proto
        return HTTPResponse(201, {"data": proto.resource(self.clock())})

    def _parse_python(self, name: str, text: str) -> tuple[str, dict[str, Any], dict[str, Any], set[str]] | HTTPResponse:
        if not re.search(r"['\"]apiLevel['\"]\s*:\s*['\"]2\.\d+['\"]", text):
            return _err(422, "ProtocolFilesInvalid", "Protocol File(s) Invalid",
                        f"{name}: apiLevel not declared. Declare it in `metadata` or `requirements`.")
        robot_type = "OT-3 Standard" if re.search(r"['\"]robotType['\"]\s*:\s*['\"](Flex|OT-3)", text) else "OT-2 Standard"
        meta_name = re.search(r"['\"]protocolName['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        api_level = re.search(r"['\"]apiLevel['\"]\s*:\s*['\"](2\.\d+)['\"]", text)
        metadata: dict[str, Any] = {"apiLevel": api_level.group(1) if api_level else None}
        if meta_name:
            metadata["protocolName"] = meta_name.group(1)
        markers = {m for m in ("SIMULATE_ANALYSIS_ERROR", "SIMULATE_RECOVERABLE_ERROR", "SIMULATE_FATAL_ERROR") if m in text}
        errors: list[dict[str, Any]] = []
        try:
            compile(text, name, "exec")
        except SyntaxError as exc:
            errors.append(self._error_occurrence("PythonException", f"SyntaxError: {exc.msg} ({name}, line {exc.lineno})"))
        if not errors and not re.search(r"^def run\(", text, re.MULTILINE):
            errors.append(self._error_occurrence(
                "MalformedPythonProtocolError", "Protocol file must define a function `run(protocol)`."))
        if not errors and "SIMULATE_ANALYSIS_ERROR" in markers:
            errors.append(self._error_occurrence(
                "LabwareDefinitionDoesNotExistError",
                "Unable to find a labware definition for 'corning_96_wellplate_999ul_flat' (simulated error)."))
        commands, labware, pipettes, modules = self._scan_python(text)
        analysis = {
            "result": "not-ok" if errors else "ok",
            "robotType": robot_type,
            "runTimeParameters": [],
            "commands": [] if errors else commands,
            "labware": labware,
            "pipettes": pipettes,
            "modules": modules,
            "liquids": [],
            "liquidClasses": [],
            "errors": errors,
        }
        return robot_type, metadata, analysis, markers

    def _parse_json(self, name: str, text: str) -> tuple[str, dict[str, Any], dict[str, Any], set[str]] | HTTPResponse:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            return _err(422, "ProtocolFilesInvalid", "Protocol File(s) Invalid", f"{name} is not valid JSON: {exc}")
        if not isinstance(doc, dict) or "commands" not in doc:
            return _err(422, "ProtocolFilesInvalid", "Protocol File(s) Invalid",
                        f"{name} is not a JSON protocol (no 'commands'). Labware files must be uploaded "
                        "together with a Python protocol.")
        robot_type = (doc.get("robot") or {}).get("model", "OT-2 Standard")
        commands = [self._command(c.get("commandType", "custom"), c.get("params") or {}) for c in doc["commands"]]
        labware = [{"id": c["params"].get("labwareId", _uid()), "loadName": c["params"].get("loadName"),
                    "location": c["params"].get("location", "offDeck"), "definitionUri": ""}
                   for c in commands if c["commandType"] == "loadLabware"]
        pipettes = [{"id": c["params"].get("pipetteId", _uid()), "pipetteName": c["params"].get("pipetteName"),
                     "mount": c["params"].get("mount")} for c in commands if c["commandType"] == "loadPipette"]
        analysis = {"result": "ok", "robotType": robot_type, "runTimeParameters": [], "commands": commands,
                    "labware": labware, "pipettes": pipettes, "modules": [], "liquids": [], "liquidClasses": [],
                    "errors": []}
        return robot_type, dict(doc.get("metadata") or {}), analysis, set()

    def _scan_python(self, text: str) -> tuple[list[dict[str, Any]], ...]:
        commands: list[dict[str, Any]] = []
        labware: list[dict[str, Any]] = []
        pipettes: list[dict[str, Any]] = []
        modules: list[dict[str, Any]] = []
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            if m := _MOD_RE.search(code):
                model = _MODULE_MODELS.get(m.group(1).strip().lower(), m.group(1).strip())
                mod = {"id": _uid(), "model": model, "location": {"slotName": m.group(2) or "D1"}}
                modules.append(mod)
                commands.append(self._command("loadModule", {"model": model, "location": mod["location"]}))
                if lw := _LW_RE.search(code[m.end():]):
                    labware.append({"id": _uid(), "loadName": lw.group(1), "location": {"moduleId": mod["id"]},
                                    "definitionUri": f"opentrons/{lw.group(1)}/1"})
                continue
            if m := _LW_RE.search(code):
                if m.group(2):
                    loc: Any = {"slotName": m.group(2)}
                elif modules:
                    loc = {"moduleId": modules[-1]["id"]}
                else:
                    loc = "offDeck"
                labware.append({"id": _uid(), "loadName": m.group(1), "location": loc,
                                "definitionUri": f"opentrons/{m.group(1)}/1"})
                commands.append(self._command("loadLabware", {"loadName": m.group(1), "location": loc}))
                continue
            if m := _PIP_RE.search(code):
                pipettes.append({"id": _uid(), "pipetteName": m.group(1), "mount": m.group(2)})
                commands.append(self._command("loadPipette", {"pipetteName": m.group(1), "mount": m.group(2)}))
                continue
            for regex, ctype in _SETPOINTS:
                if m := regex.search(code):
                    commands.append(self._command(ctype, {"moduleId": "", "celsius": float(m.group(1))}))
            if m := _SHAKE_RE.search(code):
                commands.append(self._command("heaterShaker/setAndWaitForShakeSpeed", {"moduleId": "", "rpm": float(m.group(1))}))
            for needle, steps in _STEPS.items():
                if needle in code:
                    commands.extend(self._command(s, {}) for s in steps)
        commands.append(self._command("home", {}))
        return commands, labware, pipettes, modules

    @staticmethod
    def _command(ctype: str, params: dict[str, Any]) -> dict[str, Any]:
        cid = _uid()
        return {"id": cid, "key": cid, "commandType": ctype, "params": params, "status": "succeeded",
                "intent": "protocol", "createdAt": _now_iso(), "startedAt": _now_iso(), "completedAt": _now_iso()}

    @staticmethod
    def _error_occurrence(error_type: str, detail: str, defined: bool = False) -> dict[str, Any]:
        return {"id": _uid(), "createdAt": _now_iso(), "isDefined": defined, "errorType": error_type,
                "errorCode": "4000", "detail": detail, "errorInfo": {}, "wrappedErrors": []}

    def _get_analysis(self, pid: str, aid: str) -> HTTPResponse:
        proto = self.protocols.get(pid)
        if proto is None:
            return _err(404, "ProtocolNotFound", "Protocol Not Found", f"Protocol {pid} not found.")
        if aid != proto.analysis_id:
            return _err(404, "AnalysisNotFound", "Protocol Analysis Not Found", f"Analysis {aid} not found.")
        if self.clock() < proto.ready_at:
            return HTTPResponse(200, {"data": {"id": aid, "status": "pending", "runTimeParameters": []}})
        return HTTPResponse(200, {"data": proto.analysis})

    # ------------------------------------------------------------ runs

    def _current_run(self) -> _Run | None:
        return next((r for r in self.runs.values() if r.current), None)

    def _run_analysis(self, run: _Run) -> dict[str, Any] | None:
        proto = self.protocols.get(run.protocol_id or "")
        return proto.analysis if proto else None

    def _list_runs(self, params: dict[str, Any]) -> HTTPResponse:
        runs = list(self.runs.values())
        if params.get("pageLength") is not None:
            runs = runs[-int(params["pageLength"]):]
        current = self._current_run()
        links = {"current": {"href": f"/runs/{current.id}"}} if current else {}
        data = [r.resource(self._run_analysis(r)) for r in runs]
        return HTTPResponse(200, {"data": data, "links": links, "meta": {"cursor": 0, "totalLength": len(data)}})

    def _create_run(self, body: Any) -> HTTPResponse:
        pid = ((body or {}).get("data") or {}).get("protocolId")
        proto = self.protocols.get(pid) if pid else None
        if pid and proto is None:
            return _err(404, "ProtocolNotFound", "Protocol Not Found", f"Protocol {pid} not found.")
        prev = self._current_run()
        if prev is not None:
            if prev.status not in TERMINAL_STATUSES | {"idle"}:
                return _err(409, "RunAlreadyActive", "Run Already Active", "Another run is currently active.")
            prev.current = False
        plan = [dict(c) for c in (proto.analysis["commands"] if proto else [])]
        run = _Run(_uid(), pid, _now_iso(), plan, proto.markers if proto else set(), last_tick=self.clock())
        if run.markers & {"SIMULATE_RECOVERABLE_ERROR", "SIMULATE_FATAL_ERROR"} and plan:
            half = len(plan) // 2
            kind = "pickUpTip" if "SIMULATE_RECOVERABLE_ERROR" in run.markers else "aspirate"
            run.fail_index = next((i for i in range(half, len(plan)) if plan[i]["commandType"] == kind), half)
        self.runs[run.id] = run
        return HTTPResponse(201, {"data": run.resource(self._run_analysis(run))})

    def _action(self, rid: str, body: Any) -> HTTPResponse:
        run = self.runs.get(rid)
        if run is None:
            return _err(404, "RunNotFound", "Run Not Found", f"Run {rid} not found.")
        if not run.current:
            return _err(409, "RunStopped", "Run Stopped", f"Run {rid} is not the current run")
        action = ((body or {}).get("data") or {}).get("actionType")
        not_allowed = "RunActionNotAllowed", "Run Action Not Allowed"
        if run.status in TERMINAL_STATUSES or run.status == "stop-requested":
            return _err(409, *not_allowed, "The run has already stopped.")
        now = self.clock()
        if action == "play":
            if run.status in {"blocked-by-open-door", "awaiting-recovery-blocked-by-open-door"}:
                return _err(409, *not_allowed, "Front door or top window is currently open.")
            if run.status == "awaiting-recovery-paused":
                run.status = "awaiting-recovery"
            elif run.status != "awaiting-recovery":
                if run.started_at is None:
                    run.started_at = _now_iso()
                run.status = "blocked-by-open-door" if (self.door_open and self.model == "flex") else "running"
                run.last_tick = now
        elif action == "pause":
            if run.status == "awaiting-recovery":
                return _err(409, *not_allowed, "Cannot pause a run in recovery mode.")
            if run.status != "running":
                return _err(409, *not_allowed, "Cannot pause a run that is not running.")
            run.status = "paused"
        elif action == "stop":
            run.status = "stop-requested" if run.started_at else "stopped"
            if run.status == "stopped":
                run.completed_at = _now_iso()
        elif action in {"resume-from-recovery", "resume-from-recovery-assuming-false-positive"}:
            if run.status != "awaiting-recovery":
                return _err(409, *not_allowed, "Cannot resume from recovery: the run is not awaiting recovery.")
            run.status = "running"
            run.last_tick = now
        else:
            return HTTPResponse(422, {"errors": [{"id": "InvalidRequest", "title": "Invalid Request",
                                                  "detail": f"Invalid actionType {action!r}"}]})
        record = {"id": _uid(), "createdAt": _now_iso(), "actionType": action}
        run.actions.append(record)
        return HTTPResponse(201, {"data": record})

    def _run_commands(self, rid: str, params: dict[str, Any]) -> HTTPResponse:
        run = self.runs.get(rid)
        if run is None:
            return _err(404, "RunNotFound", "Run Not Found", f"Run {rid} not found.")
        length = int(params.get("pageLength", 20))
        total = len(run.commands)
        cursor = max(0, int(params["cursor"])) if params.get("cursor") is not None else max(0, total - length)
        data = [
            {k: c.get(k) for k in ("id", "key", "commandType", "createdAt", "startedAt", "completedAt", "status",
                                   "error", "params", "intent")}
            for c in run.commands[cursor:cursor + length]
        ]
        links: dict[str, Any] = {}
        if run.commands:
            idx = total - 1
            cur = run.commands[idx]
            links["current"] = {"href": f"/runs/{rid}/commands/{cur['id']}",
                                "meta": {"runId": rid, "commandId": cur["id"], "index": idx, "key": cur["key"],
                                         "createdAt": cur["createdAt"]}}
            if run.status.startswith("awaiting-recovery"):
                links["currentlyRecoveringFrom"] = links["current"]
        return HTTPResponse(200, {"data": data, "meta": {"cursor": cursor, "totalLength": total}, "links": links})

    def _advance(self) -> None:
        now = self.clock()
        self._tick_modules(now)
        for run in self.runs.values():
            if run.status == "stop-requested":
                run.status = "stopped"
                run.completed_at = _now_iso()
                for c in run.commands:
                    if c["status"] == "running":
                        c["status"] = "failed"
                        c["error"] = self._error_occurrence("RunStoppedError", "Run was cancelled")
                continue
            if run.status != "running":
                run.last_tick = now
                continue
            run.budget_s += now - run.last_tick
            run.last_tick = now
            while run.status == "running":
                if run.commands and run.commands[-1]["status"] == "running":
                    if run.budget_s < self.command_duration_s:
                        break
                    run.budget_s -= self.command_duration_s
                    self._finish_command(run, run.commands[-1])
                    continue
                idx = len(run.commands)
                if idx >= len(run.plan):
                    run.status = "succeeded"
                    run.completed_at = _now_iso()
                    break
                template = run.plan[idx]
                cmd = {**template, "id": _uid(), "status": "running", "createdAt": _now_iso(),
                       "startedAt": _now_iso(), "completedAt": None, "error": None}
                cmd["key"] = cmd["id"]
                run.commands.append(cmd)

    def _finish_command(self, run: _Run, cmd: dict[str, Any]) -> None:
        idx = len(run.commands) - 1
        if run.fail_index is not None and idx == run.fail_index:
            run.fail_index = None
            cmd["status"] = "failed"
            cmd["completedAt"] = _now_iso()
            if "SIMULATE_RECOVERABLE_ERROR" in run.markers:
                cmd["error"] = self._error_occurrence(
                    "tipPhysicallyMissing", "No tip detected after pick up (simulated).", defined=True)
                run.status = "awaiting-recovery"
                run.recovered = True
            else:
                err = self._error_occurrence("PipetteOverpressureError", "Overpressure detected (simulated).")
                cmd["error"] = err
                run.errors = [err]
                run.status = "failed"
                run.completed_at = _now_iso()
            return
        cmd["status"] = "succeeded"
        cmd["completedAt"] = _now_iso()
        self._apply_to_modules(cmd)

    def _apply_to_modules(self, cmd: dict[str, Any]) -> None:
        ctype, params = cmd["commandType"], cmd.get("params") or {}
        family = ctype.split("/", 1)[0]
        mtype = {"temperatureModule": "temperatureModuleType", "heaterShaker": "heaterShakerModuleType",
                 "thermocycler": "thermocyclerModuleType"}.get(family)
        mod = next((m for m in self.attached_modules if m["type"] == mtype), None)
        if mod is None:
            return
        if ctype.endswith(("setTargetTemperature", "setTargetBlockTemperature")):
            mod["target"] = params.get("celsius")
        elif ctype.endswith("setTargetLidTemperature"):
            mod["lid_target"] = params.get("celsius")
        elif ctype.endswith("setAndWaitForShakeSpeed"):
            mod["rpm"] = mod["target_rpm"] = params.get("rpm")

    # ------------------------------------------------------------ stateless commands

    def _stateless(self, body: Any) -> HTTPResponse:
        current = self._current_run()
        if current is not None and current.started_at and current.status not in TERMINAL_STATUSES:
            return _err(409, "RunActive", "Run Active",
                        "There is an active run. Close the current run to issue commands via POST /commands.")
        data = (body or {}).get("data") or {}
        ctype = data.get("commandType")
        params = data.get("params") or {}
        allowed = {"home", "setRailLights", "temperatureModule/deactivate", "heaterShaker/deactivateShaker",
                   "heaterShaker/deactivateHeater", "thermocycler/deactivateBlock", "thermocycler/deactivateLid",
                   "magneticModule/disengage"}
        if ctype not in allowed:
            return HTTPResponse(422, {"errors": [{"id": "InvalidRequest", "title": "Invalid Request",
                                                  "detail": f"commandType {ctype!r} is not a valid stateless command"}]})
        cmd = {"id": _uid(), "key": _uid(), "commandType": ctype, "params": params, "createdAt": _now_iso(),
               "startedAt": _now_iso(), "completedAt": _now_iso(), "status": "succeeded", "result": {},
               "intent": "setup", "error": None}
        if ctype == "home":
            self.homed = True
        elif ctype == "setRailLights":
            self.lights = bool(params.get("on"))
        else:
            mod = next((m for m in self.attached_modules if m["id"] == params.get("moduleId")), None)
            family = ctype.split("/", 1)[0]
            expected = {"temperatureModule": "temperatureModuleType", "heaterShaker": "heaterShakerModuleType",
                        "thermocycler": "thermocyclerModuleType", "magneticModule": "magneticModuleType"}[family]
            if mod is None or mod["type"] != expected:
                cmd["status"] = "failed"
                cmd["error"] = self._error_occurrence(
                    "ModuleNotLoadedError", f"There is no module loaded with the ID {params.get('moduleId')!r}.")
            elif ctype.endswith("deactivateShaker"):
                mod["rpm"], mod["target_rpm"] = 0, None
            elif ctype.endswith("deactivateLid"):
                mod["lid_target"] = None
            else:
                mod["target"] = None
        self.stateless.append(cmd)
        return HTTPResponse(201, {"data": cmd})
