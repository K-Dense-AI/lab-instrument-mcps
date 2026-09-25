"""Driver for Opentrons OT-2 and Flex robots over the robot-server HTTP API.

The robot runs an HTTP server on port 31950. Every request carries an
``Opentrons-Version`` header selecting the HTTP API version; we send ``3`` like
the official integration examples (the server serves ``min(requested, latest)``;
version 3 is the first with the current ``GET /modules`` format).

Endpoints used (all verified against the robot-server source and the official guide):

* ``GET /health``                          robot name, model, software/firmware versions
* ``GET|POST /robot/lights``               rail lights ``{"on": bool}``
* ``GET /robot/door/status``               front door (Flex; ``doorRequiredClosedForProtocol``)
* ``GET /robot/control/estopStatus``       E-stop state (Flex; HTTP 403 on OT-2)
* ``GET /instruments``                     pipettes and gripper
* ``GET /modules``                         attached modules with live data (API version >= 3)
* ``GET|POST /protocols``, ``GET /protocols/{id}``, ``GET /protocols/{id}/analyses/{id}``
* ``GET|POST /runs``, ``GET /runs/{id}``, ``GET /runs/{id}/commands``
* ``POST /runs/{id}/actions``              ``play`` | ``pause`` | ``stop`` |
  ``resume-from-recovery`` | ``resume-from-recovery-assuming-false-positive``
* ``POST /commands?waitUntilComplete=true`` stateless ``home`` and module-deactivate commands

References:

* Opentrons, "Opentrons HTTP API" guide and examples,
  https://github.com/Opentrons/opentrons-integration-tools/tree/main/http-api
* Opentrons robot-server source (the OpenAPI spec is generated from it),
  https://github.com/Opentrons/opentrons/tree/edge/robot-server (checked at commit 5d98948,
  2026-09-24): ``versioning.py``, ``health/``, ``robot/control/``, ``service/legacy/routers/control.py``,
  ``protocols/``, ``runs/``, ``commands/``, ``instruments/``, ``modules/``.
* The robot's own spec: ``http://<robot-ip>:31950/openapi.json`` (rendered at ``/redoc``).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from labmcp import (
    AuditLog,
    InstrumentConnectionError,
    InstrumentProtocolError,
    InstrumentTimeout,
)

DEFAULT_PORT = 31950
DEFAULT_API_VERSION = "3"

ROBOT_MODELS = {"OT-3 Standard": "Flex", "OT-2 Standard": "OT-2"}

#: Run statuses (``EngineStatus`` in the protocol engine).
TERMINAL_STATUSES = {"stopped", "failed", "succeeded"}
ACTIVE_STATUSES = {
    "running",
    "paused",
    "blocked-by-open-door",
    "stop-requested",
    "finishing",
    "awaiting-recovery",
    "awaiting-recovery-paused",
    "awaiting-recovery-blocked-by-open-door",
}

#: Stateless commands (``POST /commands``) that switch a module off, per ``moduleType``.
DEACTIVATE_COMMANDS: dict[str, list[str]] = {
    "temperatureModuleType": ["temperatureModule/deactivate"],
    "heaterShakerModuleType": ["heaterShaker/deactivateShaker", "heaterShaker/deactivateHeater"],
    "thermocyclerModuleType": ["thermocycler/deactivateBlock", "thermocycler/deactivateLid"],
    "magneticModuleType": ["magneticModule/disengage"],
}

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_STATUS_HINTS = {
    401: "The robot has access control enabled and needs an access token: restart the server with "
    "`--option access_token=<token>`.",
    403: "The robot refused the request (insufficient permission, the E-stop is engaged, or the "
    "feature is not supported on this robot model).",
    404: "Not found: check the ID (runs and protocols are listed by `list_runs` / `list_protocols`).",
    409: "Conflict: the robot is busy with another run, or the action is not allowed in the run's "
    "current state. Check `get_run_status`.",
    422: "The robot rejected the request as invalid.",
    503: "The robot server is not ready yet (still booting, or the motor controller is not ready). "
    "Wait a minute and retry.",
}


class RobotHTTPError(InstrumentProtocolError):
    """The robot answered with an HTTP error status."""

    def __init__(self, message: str, status: int, error_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_id = error_id


@dataclass
class HTTPResponse:
    status: int
    body: Any  # parsed JSON, or the raw text if the body was not JSON


class HTTPBackend(Protocol):
    """What the driver needs from an HTTP client. Implemented by :class:`HttpxBackend`
    (real robot) and :class:`labmcp_opentrons.simulator.FakeOpentronsRobot`."""

    description: str
    api_version: str

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        timeout: float | None = None,
    ) -> HTTPResponse: ...

    def close(self) -> None: ...


def normalize_base_url(address: str) -> str:
    """Turn ``192.168.1.20``, ``ot2.local:31950`` or ``http://10.0.0.5:31950`` into a base URL."""
    raw = address.strip().rstrip("/")
    if not raw:
        raise ValueError("Empty robot address")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(
            f"Unsupported address {address!r}: use the robot's IP or hostname, e.g. "
            "192.168.1.20 or http://192.168.1.20:31950"
        )
    if not parts.hostname:
        raise ValueError(f"No hostname in robot address {address!r}")
    if parts.path not in {"", "/"} or parts.query:
        raise ValueError(f"Robot address {address!r} must not contain a path or query")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{parts.port or DEFAULT_PORT}"


class HttpxBackend:
    """Real HTTP client for a robot on the network."""

    def __init__(
        self,
        base_url: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        access_token: str | None = None,
        timeout: float = 10.0,
        transport: Any = None,
    ) -> None:
        import httpx

        self._httpx = httpx
        self.base_url = base_url
        self.api_version = api_version
        self.description = base_url
        headers = {"Opentrons-Version": api_version, "Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)

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
        httpx = self._httpx
        kwargs: dict[str, Any] = {"params": params}
        if json is not None:
            kwargs["json"] = json
        if files is not None:
            kwargs["files"] = files
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise InstrumentTimeout(
                f"The robot at {self.base_url} did not answer {method} {path} in time ({exc}). "
                "Is it powered on and reachable? Try `get_robot_status` again."
            ) from exc
        except httpx.TransportError as exc:
            raise InstrumentConnectionError(
                f"Could not reach the robot at {self.base_url} ({type(exc).__name__}: {exc}). "
                "Check that it is powered on and on the same network, and that the IP address "
                "matches the Opentrons App (Robot settings > Networking)."
            ) from exc
        try:
            body: Any = r.json() if r.content else None
        except ValueError:
            body = r.text
        return HTTPResponse(r.status_code, body)

    def close(self) -> None:
        self._client.close()


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def _check_id(value: str, what: str) -> str:
    if not _ID_RE.match(value or ""):
        raise InstrumentProtocolError(f"Invalid {what} {value!r}: expected an ID like the ones listed by the robot.")
    return value


class OpentronsRobot:
    """Typed wrapper around the robot-server HTTP API.

    Every request and reply is recorded in ``audit`` (shown by ``get_command_log``).
    """

    def __init__(self, backend: HTTPBackend, audit: AuditLog | None = None, poll_interval_s: float = 1.0) -> None:
        self.backend = backend
        self.audit = audit or AuditLog()
        self.poll_interval_s = poll_interval_s
        self._analysis_cache: dict[tuple[str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------ low level

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        timeout: float | None = None,
        ok: Iterable[int] = (200, 201),
    ) -> Any:
        """Send one request and return the parsed JSON body, raising on HTTP errors."""
        target = path + (f"?{urlencode(params)}" if params else "")
        sent = f"{method} {target}"
        if json_body is not None:
            sent += " " + _short(json_body)
        if files:
            sent += " files=" + ",".join(name for _, (name, _data, _ct) in files)
        source = self.backend.description
        self.audit.record("write", sent, source)
        resp = self.backend.request(method, path, params=params, json=json_body, files=files, timeout=timeout)
        self.audit.record("read", f"HTTP {resp.status} {_short(resp.body) if resp.body is not None else ''}", source)
        if resp.status not in set(ok):
            raise self._error(method, path, resp)
        return resp.body

    @staticmethod
    def _error(method: str, path: str, resp: HTTPResponse) -> RobotHTTPError:
        body = resp.body
        ident = detail = None
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                ident = errors[0].get("id")
                detail = errors[0].get("detail") or errors[0].get("title")
            elif "message" in body:  # legacy endpoints (/robot/*)
                detail = body["message"]
            elif "debugMessage" in body:  # access-control failures
                detail = body["debugMessage"]
            elif "detail" in body:  # request validation errors
                detail = _short(body["detail"], 400)
        elif isinstance(body, str) and body:
            detail = _short(body, 400)
        what = f" {ident}" if ident else ""
        msg = f"Robot refused {method} {path} (HTTP {resp.status}{what})"
        if detail:
            msg += f": {detail}"
        hint = _STATUS_HINTS.get(resp.status)
        if hint and not (resp.status == 403 and ident == "NotSupportedOnOT2"):
            msg += f". {hint}"
        return RobotHTTPError(msg, resp.status, ident)

    # ------------------------------------------------------------ robot

    def health(self) -> dict[str, Any]:
        return self.call("GET", "/health")

    def identify(self) -> dict[str, Any]:
        h = self.health()
        return {
            "manufacturer": "Opentrons",
            "model": ROBOT_MODELS.get(h.get("robot_model", ""), h.get("robot_model")),
            "name": h.get("name"),
            "serial": h.get("robot_serial"),
            "software_version": h.get("api_version"),
            "firmware_version": h.get("fw_version"),
            "system_version": h.get("system_version"),
            "http_api_version": self.backend.api_version,
        }

    def lights_on(self) -> bool:
        return bool(self.call("GET", "/robot/lights")["on"])

    def set_lights(self, on: bool) -> bool:
        return bool(self.call("POST", "/robot/lights", json_body={"on": on})["on"])

    def door_status(self) -> dict[str, Any] | None:
        """Door state, or ``None`` if this software version has no door endpoint."""
        try:
            return self.call("GET", "/robot/door/status")["data"]
        except RobotHTTPError as exc:
            if exc.status == 404:
                return None
            raise

    def estop_status(self) -> dict[str, Any] | None:
        """E-stop state (Flex), or ``None`` on robots without an E-stop (OT-2 answers 403)."""
        try:
            return self.call("GET", "/robot/control/estopStatus")["data"]
        except RobotHTTPError as exc:
            if exc.status in {403, 404}:
                return None
            raise

    def instruments(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "/instruments")["data"])

    def modules(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "/modules")["data"])

    # ------------------------------------------------------------ protocols

    def protocols(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "/protocols")["data"])

    def protocol(self, protocol_id: str) -> dict[str, Any]:
        return self.call("GET", f"/protocols/{_check_id(protocol_id, 'protocol ID')}")["data"]

    def analysis(self, protocol_id: str, analysis_id: str) -> dict[str, Any]:
        key = (protocol_id, analysis_id)
        if key in self._analysis_cache:
            return self._analysis_cache[key]
        path = f"/protocols/{_check_id(protocol_id, 'protocol ID')}/analyses/{_check_id(analysis_id, 'analysis ID')}"
        data = self.call("GET", path)["data"]
        if data.get("status") == "completed":  # completed analyses never change
            self._analysis_cache[key] = data
        return data

    def latest_analysis(self, protocol: dict[str, Any]) -> dict[str, Any] | None:
        summaries = protocol.get("analysisSummaries") or []
        if not summaries:
            return None
        return self.analysis(protocol["id"], summaries[-1]["id"])

    def upload_protocol(self, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        """``POST /protocols`` (multipart, one ``files`` part per file). Returns the protocol."""
        parts = [
            ("files", (name, data, "application/json" if name.lower().endswith(".json") else "text/x-python"))
            for name, data in files
        ]
        return self.call("POST", "/protocols", files=parts, timeout=120.0)["data"]

    def wait_for_analysis(self, protocol_id: str, timeout_s: float) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Poll until the newest analysis completes. Returns (protocol, analysis or None if pending)."""
        deadline = time.monotonic() + timeout_s
        while True:
            proto = self.protocol(protocol_id)
            summaries = proto.get("analysisSummaries") or []
            if summaries and summaries[-1].get("status") == "completed":
                return proto, self.latest_analysis(proto)
            if time.monotonic() >= deadline:
                return proto, None
            time.sleep(self.poll_interval_s)

    # ------------------------------------------------------------ runs

    def runs(self, page_length: int = 20) -> tuple[list[dict[str, Any]], str | None]:
        """Most recent runs (oldest first) and the ID of the current run, if any."""
        body = self.call("GET", "/runs", params={"pageLength": page_length})
        current = ((body.get("links") or {}).get("current") or {}).get("href")
        current_id = current.rsplit("/", 1)[-1] if current else None
        return list(body["data"]), current_id

    def run(self, run_id: str) -> dict[str, Any]:
        return self.call("GET", f"/runs/{_check_id(run_id, 'run ID')}")["data"]

    def create_run(self, protocol_id: str) -> dict[str, Any]:
        body = {"data": {"protocolId": _check_id(protocol_id, "protocol ID")}}
        return self.call("POST", "/runs", json_body=body, timeout=120.0)["data"]

    def run_action(self, run_id: str, action_type: str) -> dict[str, Any]:
        body = {"data": {"actionType": action_type}}
        return self.call("POST", f"/runs/{_check_id(run_id, 'run ID')}/actions", json_body=body)["data"]

    def run_commands(self, run_id: str, page_length: int = 20, cursor: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"pageLength": page_length}
        if cursor is not None:
            params["cursor"] = cursor
        return self.call("GET", f"/runs/{_check_id(run_id, 'run ID')}/commands", params=params)

    # ------------------------------------------------------------ stateless commands

    def stateless_command(self, command_type: str, params: dict[str, Any], timeout_s: float = 60.0) -> dict[str, Any]:
        """Run one command through ``POST /commands`` and wait for it. Raises if it failed."""
        body = {"data": {"commandType": command_type, "params": params}}
        query = {"waitUntilComplete": "true", "timeout": int(timeout_s * 1000)}
        data = self.call("POST", "/commands", params=query, json_body=body, timeout=timeout_s + 15)["data"]
        status = data.get("status")
        if status == "failed":
            err = data.get("error") or {}
            raise InstrumentProtocolError(
                f"Robot command {command_type} failed: {err.get('detail') or err.get('errorType') or 'unknown error'}"
            )
        if status != "succeeded":
            raise InstrumentTimeout(
                f"Robot command {command_type} did not finish within {timeout_s:g} s (status {status!r})."
            )
        return data

    def home(self, timeout_s: float = 120.0) -> dict[str, Any]:
        return self.stateless_command("home", {}, timeout_s=timeout_s)

    def close(self) -> None:
        self.backend.close()


# --------------------------------------------------------------------------
# Analysis helpers (pure functions, used by the server and tests)
# --------------------------------------------------------------------------


def location_text(location: Any, modules_by_id: dict[str, dict[str, Any]], labware_by_id: dict[str, dict[str, Any]]) -> str:
    """Human-readable deck location of a labware/module location object."""
    if isinstance(location, str):
        return location  # "offDeck", "systemLocation", ...
    if not isinstance(location, dict):
        return str(location)
    if "slotName" in location:
        return f"slot {location['slotName']}"
    if "addressableAreaName" in location:
        return f"area {location['addressableAreaName']}"
    if "moduleId" in location:
        mod = modules_by_id.get(location["moduleId"], {})
        where = location_text(mod.get("location"), {}, {}) if mod else "?"
        return f"on {mod.get('model', 'module')} ({where})"
    if "labwareId" in location:
        lw = labware_by_id.get(location["labwareId"], {})
        return f"on {lw.get('loadName', 'labware')}"
    return json.dumps(location)


def deck_layout(analysis: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """What must be on the deck for this protocol: labware, modules and pipettes."""
    modules = {m["id"]: m for m in analysis.get("modules") or [] if "id" in m}
    labware = {lw["id"]: lw for lw in analysis.get("labware") or [] if "id" in lw}
    return {
        "labware": [
            {
                "load_name": lw.get("loadName"),
                "display_name": lw.get("displayName"),
                "location": location_text(lw.get("location"), modules, labware),
            }
            for lw in labware.values()
            if lw.get("loadName") not in {None, "opentrons_1_trash_1100ml_fixed", "opentrons_1_trash_3200ml_fixed"}
        ],
        "modules": [
            {"model": m.get("model"), "location": location_text(m.get("location"), {}, {})} for m in modules.values()
        ],
        "pipettes": [
            {"name": p.get("pipetteName"), "mount": p.get("mount")} for p in analysis.get("pipettes") or []
        ],
        "liquids": [
            {"name": liq.get("displayName"), "description": liq.get("description")}
            for liq in analysis.get("liquids") or []
        ],
    }


def module_setpoints(commands: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Highest module temperature (°C) and shake speed (rpm) requested by analyzed commands.

    Looks at every ``celsius`` value inside the params of temperature-module, heater-shaker and
    thermocycler commands (including thermocycler profiles), and every heater-shaker ``rpm``.
    """
    max_c: float | None = None
    max_rpm: float | None = None

    def walk(obj: Any, key: str) -> Iterable[float]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key and isinstance(v, (int, float)):
                    yield float(v)
                else:
                    yield from walk(v, key)
        elif isinstance(obj, list):
            for item in obj:
                yield from walk(item, key)

    for cmd in commands:
        ctype = str(cmd.get("commandType", ""))
        if not ctype.startswith(("temperatureModule/", "heaterShaker/", "thermocycler/")):
            continue
        params = cmd.get("params") or {}
        for c in walk(params, "celsius"):
            max_c = c if max_c is None else max(max_c, c)
        if ctype.startswith("heaterShaker/"):
            for rpm in walk(params, "rpm"):
                max_rpm = rpm if max_rpm is None else max(max_rpm, rpm)
    return max_c, max_rpm


def error_text(errors: list[dict[str, Any]] | None) -> list[str]:
    """Flatten protocol-engine ``ErrorOccurrence`` objects (incl. wrapped errors) to messages."""
    out: list[str] = []
    for err in errors or []:
        detail = err.get("detail") or err.get("errorType") or "unknown error"
        code = err.get("errorCode")
        out.append(f"{err.get('errorType', 'Error')}{f' [{code}]' if code else ''}: {detail}")
        wrapped = err.get("wrappedErrors") or []
        out.extend("  caused by " + line for line in error_text(wrapped))
    return out
