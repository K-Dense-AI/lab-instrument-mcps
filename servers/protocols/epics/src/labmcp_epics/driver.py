"""EPICS Channel Access client driver (caproto threading client).

References:

* "Channel Access Protocol Specification", EPICS documentation
  (https://docs.epics-controls.org/en/latest/specs/ca_protocol.html): DBR_TIME / DBR_CTRL payloads
  (value, alarm status/severity, timestamp, units, precision, display/alarm/control limits, enum
  strings), access rights, WRITE_NOTIFY (put-callback) completion.
* EPICS Base record reference, aoRecord (https://docs.epics-controls.org/projects/base/en/latest/aoRecord.html)
  and ``aoRecord.c``/``longoutRecord.c`` ``get_control_double``: for output records the CA control
  limits are DRVH/DRVL; the IOC silently *clips* VAL to them ("only enforced as long as DRVH > DRVL").
  That is why this driver refuses out-of-range writes itself instead of letting the IOC clip.
* caproto threading client (https://caproto.github.io/caproto/master/threading-client.html):
  ``Context.get_pvs``, ``PV.read(data_type='time'|'control')``, ``PV.write(wait=True)``,
  ``PV.subscribe().add_callback``; configuration via EPICS_CA_ADDR_LIST / EPICS_CA_AUTO_ADDR_LIST /
  EPICS_CA_SERVER_PORT environment variables (caproto 1.3).

No MCP code in here. caproto is imported lazily inside :class:`EpicsClient`.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from labmcp import InstrumentConnectionError, InstrumentProtocolError, InstrumentTimeout

#: CA string values are at most 40 bytes including the terminating NUL (MAX_STRING_SIZE).
MAX_STRING_CHARS = 39
_INT_RANGES = {"CHAR": (0, 255), "INT": (-(2**15), 2**15 - 1), "LONG": (-(2**31), 2**31 - 1)}
_PV_NAME_RE = re.compile(r"^[^\s\"']{1,255}$")


class EnvOverride:
    """Set EPICS_* environment variables for this process and restore them on close.

    Channel Access (EPICS Base and caproto alike) is configured through the environment."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self._saved: dict[str, str | None] = {}

    def apply(self) -> None:
        for key, value in self.values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def restore(self) -> None:
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved.clear()


def ca_environment(address: str | None, options: dict[str, str]) -> dict[str, str]:
    """EPICS_CA_* variables from ``--address`` (shorthand for the address list) and ``--option``s."""
    env: dict[str, str] = {}
    addr_list = options.get("ca_addr_list") or address or ""
    addr_list = addr_list.replace("ca://", "").replace(",", " ").strip()
    if addr_list:
        env["EPICS_CA_ADDR_LIST"] = " ".join(addr_list.split())
        env["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    if options.get("auto_addr_list"):
        env["EPICS_CA_AUTO_ADDR_LIST"] = "YES" if options["auto_addr_list"].lower() in {"1", "yes", "true"} else "NO"
    if options.get("server_port"):
        env["EPICS_CA_SERVER_PORT"] = str(int(options["server_port"]))
    return env


@dataclass
class PVReading:
    name: str
    value: Any
    native_type: str
    count: int
    units: str | None = None
    precision: int | None = None
    severity: str = "NO_ALARM"
    status: str = "NO_ALARM"
    timestamp: float | None = None
    enum_strings: list[str] | None = None
    display_limits: tuple[float, float] | None = None
    alarm_limits: tuple[float, float] | None = None
    warning_limits: tuple[float, float] | None = None
    control_limits: tuple[float, float] | None = None
    as_string: str | None = None
    array: np.ndarray | None = field(default=None, repr=False)


@dataclass
class PreparedPut:
    name: str
    pv: Any
    data: list[Any]
    requested: Any
    native_type: str
    control_limits: tuple[float, float] | None
    warnings: list[str]
    before: Any


def _text(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.split(b"\x00", 1)[0].decode("latin-1")
    return str(raw)


def _limits(lo: Any, hi: Any) -> tuple[float, float] | None:
    """EPICS convention: equal low/high limits (usually 0, 0) mean 'not configured'."""
    lo, hi = float(lo), float(hi)
    return None if lo == hi else (lo, hi)


class EpicsClient:
    """Thread-safe wrapper around one caproto threading ``Context``."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        timeout: float = 2.0,
        put_allowlist: str | None = None,
        require_ctrl_limits: bool = False,
        audit: Any | None = None,
        on_close: Any | None = None,
        description: str = "",
        check_pv: str | None = None,
    ) -> None:
        self.env = EnvOverride(env or {})
        self.env.apply()
        try:
            from caproto import AccessRights, AlarmSeverity, AlarmStatus, ChannelType
            from caproto.threading.client import Context
        except Exception:
            self.env.restore()
            raise
        self.AccessRights = AccessRights
        self.AlarmSeverity = AlarmSeverity
        self.AlarmStatus = AlarmStatus
        self.ChannelType = ChannelType
        self.timeout = timeout
        self.allow = re.compile(put_allowlist) if put_allowlist else None
        self.require_ctrl_limits = require_ctrl_limits
        self.audit = audit
        self._on_close = on_close
        self.description = description
        self.check_pv = check_pv
        self.ctx = Context(timeout=timeout)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ connection

    def identify(self) -> dict[str, Any]:
        env = {k: os.environ[k] for k in ("EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST", "EPICS_CA_SERVER_PORT")
               if k in os.environ}
        info: dict[str, Any] = {
            "protocol": "EPICS Channel Access (caproto threading client)",
            "caproto_version": _caproto_version(),
            "ca_environment": env or "defaults (auto address list: broadcast on all interfaces)",
            "put_allowlist": self.allow.pattern if self.allow else None,
            "require_ctrl_limits": self.require_ctrl_limits,
        }
        if self.description:
            info["note"] = self.description
        if self.check_pv:  # --option check_pv=NAME makes --check prove a PV is reachable
            try:
                r = self.read(self.check_pv)
                info["check_pv"] = {"name": r.name, "value": r.value, "units": r.units, "severity": r.severity}
            except Exception as exc:
                info["check_pv"] = {"name": self.check_pv, "error": str(exc)}
        return info

    def connect(self, names: list[str], timeout: float | None = None) -> list[Any]:
        """Search for and connect to ``names``; raise listing any that could not be found."""
        for n in names:
            if not _PV_NAME_RE.match(n):
                raise InstrumentProtocolError(f"Invalid PV name {n!r} (no whitespace or quotes, max 255 chars).")
        tmo = self.timeout if timeout is None else timeout
        pvs = self.ctx.get_pvs(*names, timeout=tmo)
        deadline = time.monotonic() + tmo
        missing = []
        for pv in pvs:
            try:
                pv.wait_for_connection(timeout=max(0.05, deadline - time.monotonic()))
            except Exception:
                missing.append(pv.name)
        if missing:
            raise InstrumentConnectionError(
                f"Could not connect to PV(s) {', '.join(missing)} within {tmo:g} s. Check the spelling, that the "
                "IOC is running, and that EPICS_CA_ADDR_LIST (--option ca_addr_list=...) reaches it (or its "
                "CA gateway) through any firewall (UDP 5064/5065, TCP 5064)."
            )
        return pvs

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ctx.disconnect()
        self.env.restore()
        if self._on_close is not None:
            with contextlib.suppress(Exception):
                self._on_close()

    # ------------------------------------------------------------ reading

    def _native(self, pv: Any) -> tuple[str, int]:
        return self.ChannelType(pv.channel.native_data_type).name, int(pv.channel.native_data_count)

    def read(self, name: str, timeout: float | None = None) -> PVReading:
        (pv,) = self.connect([name], timeout)
        return self._read_pv(pv, timeout)

    def read_many(self, names: list[str], timeout: float | None = None) -> list[PVReading | Exception]:
        tmo = self.timeout if timeout is None else timeout
        pvs = self.ctx.get_pvs(*names, timeout=tmo)
        deadline = time.monotonic() + tmo
        out: list[PVReading | Exception] = []
        for pv in pvs:
            try:
                pv.wait_for_connection(timeout=max(0.05, deadline - time.monotonic()))
            except Exception:
                out.append(InstrumentConnectionError(f"Could not connect to {pv.name} within {tmo:g} s."))
                continue
            try:
                out.append(self._read_pv(pv, timeout))
            except Exception as exc:
                out.append(exc)
        return out

    def _read_pv(self, pv: Any, timeout: float | None) -> PVReading:
        tmo = self.timeout if timeout is None else timeout
        native, count = self._native(pv)
        try:
            ctrl = pv.read(data_type="control", timeout=tmo)
            tim = pv.read(data_type="time", timeout=tmo)
        except Exception as exc:
            raise _ca_error(exc, f"reading {pv.name}") from exc
        md, tm = ctrl.metadata, tim.metadata
        r = PVReading(name=pv.name, value=None, native_type=native, count=count)
        r.severity = self.AlarmSeverity(int(tm.severity)).name
        r.status = self.AlarmStatus(int(tm.status)).name
        r.timestamp = float(tm.timestamp)
        data = tim.data
        if native == "STRING":
            values = [_text(v) for v in data]
            r.value = values[0] if count == 1 else values
        elif native == "ENUM":
            r.enum_strings = [_text(s) for s in md.enum_strings]
            idx = int(data[0])
            r.value = r.enum_strings[idx] if idx < len(r.enum_strings) else idx
        else:
            arr = np.asarray(data)
            r.array = arr
            r.value = arr.item() if count == 1 and arr.size == 1 else arr.tolist()
            r.units = _text(md.units) or None
            if hasattr(md, "precision"):
                r.precision = int(md.precision)
            r.display_limits = _limits(md.lower_disp_limit, md.upper_disp_limit)
            r.alarm_limits = _limits(md.lower_alarm_limit, md.upper_alarm_limit)
            r.warning_limits = _limits(md.lower_warning_limit, md.upper_warning_limit)
            r.control_limits = _limits(md.lower_ctrl_limit, md.upper_ctrl_limit)
            if native == "CHAR" and count > 1:
                r.as_string = bytes(int(v) & 0xFF for v in arr).split(b"\x00", 1)[0].decode("latin-1")
        return r

    def info(self, name: str, timeout: float | None = None) -> dict[str, Any]:
        (pv,) = self.connect([name], timeout)
        native, count = self._native(pv)
        rights = pv.access_rights
        host, port = pv.circuit_manager.circuit.address
        return {
            "name": pv.name,
            "connected": True,
            "server": f"{host}:{port}",
            "native_type": native,
            "element_count": count,
            "read_access": bool(rights & self.AccessRights.READ),
            "write_access": bool(rights & self.AccessRights.WRITE),
            "put_allowed_by_allowlist": self.allowed(pv.name),
        }

    def monitor(self, name: str, duration_s: float, max_updates: int) -> list[tuple[float, Any]]:
        """Collect (EPICS timestamp, response) for every update during ``duration_s``."""
        (pv,) = self.connect([name])
        updates: list[tuple[float, Any]] = []
        done = threading.Event()

        def on_update(_sub: Any, response: Any) -> None:
            if len(updates) < max_updates:
                updates.append((float(response.metadata.timestamp), response))
            if len(updates) >= max_updates:
                done.set()

        sub = pv.subscribe(data_type="time")
        token = sub.add_callback(on_update)
        try:
            done.wait(duration_s)
        finally:
            with contextlib.suppress(Exception):
                sub.remove_callback(token)
        return updates

    def update_value(self, pv_native: str, response: Any, enum_strings: list[str] | None) -> Any:
        data = response.data
        if pv_native == "STRING":
            return _text(data[0]) if len(data) == 1 else [_text(v) for v in data]
        if pv_native == "ENUM":
            idx = int(data[0])
            return enum_strings[idx] if enum_strings and idx < len(enum_strings) else idx
        arr = np.asarray(data)
        return arr.item() if arr.size == 1 else arr

    # ------------------------------------------------------------ writing

    def allowed(self, name: str) -> bool:
        return self.allow is None or bool(self.allow.fullmatch(name))

    def prepare_put(self, name: str, value: Any, *, check_allowlist: bool = True) -> PreparedPut:
        """Validate a write against the allow-list, access rights, native type, element count and
        control (DRVH/DRVL) limits. Nothing is written."""
        if check_allowlist and not self.allowed(name):
            raise InstrumentProtocolError(
                f"Refused: {name} does not match the put allow-list /{self.allow.pattern}/ "  # type: ignore[union-attr]
                "(--option put_allowlist). Nothing was written."
            )
        (pv,) = self.connect([name])
        if not pv.access_rights & self.AccessRights.WRITE:
            raise InstrumentProtocolError(
                f"Refused: the IOC grants this client read-only access to {name} (EPICS access security). "
                "Nothing was written."
            )
        current = self._read_pv(pv, None)
        native, count = current.native_type, current.count
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        warnings: list[str] = []
        if native == "CHAR" and count > 1 and isinstance(value, str):
            raw = value.encode("latin-1", "replace")
            if len(raw) >= count:
                raise InstrumentProtocolError(f"Refused: {len(raw)} characters do not fit in {name} ({count - 1} max).")
            values = list(raw) + [0]
        if not values:
            raise InstrumentProtocolError("Refused: empty value.")
        if len(values) > count:
            raise InstrumentProtocolError(f"Refused: {len(values)} elements given but {name} holds {count}.")

        if native == "ENUM":
            choices = current.enum_strings or []
            data = []
            for v in values:
                if isinstance(v, str) and v in choices:
                    data.append(choices.index(v))
                elif isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() and 0 <= int(v) < len(choices):
                    data.append(int(v))
                else:
                    raise InstrumentProtocolError(
                        f"Refused: {v!r} is not a valid state of {name}. Choices: {choices} (or their index)."
                    )
        elif native == "STRING":
            data = []
            for v in values:
                if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                    raise InstrumentProtocolError(f"Refused: {name} is a string PV; got {v!r}.")
                s = str(v)
                if len(s.encode("latin-1", "replace")) > MAX_STRING_CHARS:
                    raise InstrumentProtocolError(f"Refused: CA strings hold at most {MAX_STRING_CHARS} characters.")
                data.append(s)
        else:
            data = []
            for v in values:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise InstrumentProtocolError(f"Refused: {name} is numeric ({native}); got {v!r}.")
                if not math.isfinite(float(v)):
                    raise InstrumentProtocolError(f"Refused: {v!r} is not a finite number.")
                if native in _INT_RANGES:
                    if not float(v).is_integer():
                        raise InstrumentProtocolError(f"Refused: {name} is an integer PV ({native}); got {v!r}.")
                    lo, hi = _INT_RANGES[native]
                    if not lo <= int(v) <= hi:
                        raise InstrumentProtocolError(f"Refused: {v!r} is outside the {native} range {lo}..{hi}.")
                    data.append(int(v))
                else:
                    data.append(float(v))
            limits = current.control_limits
            if limits is not None and not (native == "CHAR" and isinstance(value, str)):
                lo, hi = limits
                bad = [v for v in data if not lo <= v <= hi]
                if bad:
                    raise InstrumentProtocolError(
                        f"Refused: {bad[0]:g} is outside the control limits of {name} "
                        f"({lo:g} .. {hi:g}{' ' + current.units if current.units else ''}; DRVL/DRVH for output "
                        "records - the IOC would silently clip it). Nothing was written."
                    )
            elif limits is None:
                if self.require_ctrl_limits:
                    raise InstrumentProtocolError(
                        f"Refused: {name} has no control limits configured and --option require_ctrl_limits=true. "
                        "Nothing was written."
                    )
                warnings.append(f"{name} reports no control limits (DRVL/DRVH); the value was only type-checked.")
        return PreparedPut(name, pv, data, value, native, current.control_limits, warnings, current.value)

    def put(self, prep: PreparedPut, *, wait: bool, timeout: float) -> dict[str, Any]:
        """Write a validated value. With ``wait`` the IOC's put-callback (WRITE_NOTIFY) is awaited:
        it completes when record processing finishes (e.g. a motor reaching its target)."""
        if self.audit is not None:
            self.audit.record("write", f"caput{' -c' if wait else ''} {prep.name} {prep.data}", "epics")
        t0 = time.monotonic()
        try:
            response = prep.pv.write(prep.data, wait=wait, timeout=timeout)
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower() or "did not respond" in str(exc):
                raise InstrumentTimeout(
                    f"No put-completion from {prep.name} within {timeout:g} s. The IOC may still be processing "
                    "(e.g. a motor still moving) - read the PV/readback before retrying. Do not repeat the write "
                    "blindly."
                ) from exc
            raise _ca_error(exc, f"writing {prep.name}") from exc
        elapsed = time.monotonic() - t0
        if wait and response is not None:
            status = getattr(response, "status", None)
            ok = getattr(status, "success", 1)
            if not ok:
                raise InstrumentProtocolError(
                    f"IOC reported failure writing {prep.name}: {getattr(status, 'name', status)} "
                    f"({getattr(status, 'description', '')})."
                )
        after = self._read_pv(prep.pv, None)
        if self.audit is not None:
            self.audit.record("read", f"{prep.name} = {after.value!r} ({after.severity})", "epics")
        return {"completed": wait, "elapsed_s": elapsed, "reading": after}


def _ca_error(exc: Exception, doing: str) -> InstrumentProtocolError:
    return InstrumentProtocolError(f"Channel Access error while {doing}: {type(exc).__name__}: {exc}")


def _caproto_version() -> str:
    try:
        import caproto

        return str(caproto.__version__)
    except Exception:  # pragma: no cover
        return "unknown"


def parse_safe_state(spec: str | None) -> list[tuple[str, Any]]:
    """``"MTR:STOP=1;HV:ENABLE=Off"`` -> [("MTR:STOP", 1), ("HV:ENABLE", "Off")]."""
    out: list[tuple[str, Any]] = []
    for item in (spec or "").split(";"):
        item = item.strip()
        if not item:
            continue
        name, sep, raw = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"safe_state entries must look like PV=value, got {item!r}")
        raw = raw.strip()
        value: Any = raw
        for cast in (int, float):
            try:
                value = cast(raw)
                break
            except ValueError:
                pass
        out.append((name.strip(), value))
    return out
