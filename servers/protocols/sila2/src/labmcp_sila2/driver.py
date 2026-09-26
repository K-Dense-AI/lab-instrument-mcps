"""Generic SiLA 2 client bridge.

SiLA 2 (https://sila-standard.com) is the open lab-automation standard: every SiLA Server describes
its capabilities in Feature Definitions (FDL XML) and exposes them over gRPC (HTTP/2, TLS by
default). References:

* SiLA 2 Specification Part A (core concepts: features, commands, properties, data types,
  constraints, errors, observable commands, discovery) and Part B (gRPC mapping, execution info,
  mDNS discovery ``_sila._tcp``): https://sila-standard.com/standards/
* Official schemas and core features (``FeatureDefinition.xsd``, ``SiLAService``, ``CancelController``):
  https://gitlab.com/SiLA2/sila_base
* ``sila2`` Python library 0.14 (https://gitlab.com/SiLA2/sila_python, docs
  https://sila2.gitlab.io/sila_python/): ``SilaClient(address, port, insecure=..., root_certs=...)``,
  dynamic attributes ``client.<Feature>.<Property>.get()/.subscribe()``,
  ``client.<Feature>.<Command>(**params)`` (observable commands return a
  ``ClientObservableCommandInstance`` with ``.status``, ``.progress``, ``.get_responses()``),
  ``SilaDiscoveryBrowser``/zeroconf for discovery.

Cancellation is not part of the core protocol: it is offered by servers that implement the
``org.silastandard/core.commands/CancelController/v1`` feature. This bridge uses it when present.

No MCP code in here. ``sila2`` and ``zeroconf`` are imported lazily.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from labmcp import InstrumentConnectionError, InstrumentProtocolError, InstrumentTimeout

from labmcp_sila2.fdl import (
    Converter,
    FDLValidationError,
    FeatureIR,
    convert_parameters,
    element_schema,
    parse_feature,
    to_json_schema,
    to_jsonable,
    type_label,
)

CANCEL_CONTROLLER = "org.silastandard/core.commands/CancelController/v1"
SILA_SERVICE = "org.silastandard/core/SiLAService/v1"
MAX_TRACKED = 200
MAX_CALL_TIMEOUT_S = 300.0
_END = object()  # sentinel: a subscription ended without a value


def _run_in_thread(fn: Any, name: str = "labmcp-sila2-call") -> concurrent.futures.Future[Any]:
    """Run ``fn`` in a fresh daemon thread and return a future for its result.

    sila2's gRPC calls have no deadline. A shared pool would let calls that outlive their deadline
    pile up and starve later calls (including cancel_command), and non-daemon pool threads are joined
    at interpreter exit, so a hung call would stop the server (or ``--check``) from exiting.
    """
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = fn()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    threading.Thread(target=runner, name=name, daemon=True).start()
    return future


def _deadline_passed(exc: BaseException, future: concurrent.futures.Future[Any]) -> bool:
    """True if ``future.result(timeout=...)`` gave up waiting, rather than the call raising a TimeoutError
    of its own (the two are the same class since Python 3.11)."""
    if not isinstance(exc, concurrent.futures.TimeoutError):
        return False
    return not (future.done() and not future.cancelled() and future.exception() is exc)


def _when_late(future: concurrent.futures.Future[Any], cleanup: Any) -> None:
    """After a deadline: pass the result to ``cleanup`` if the call still succeeds later."""

    def done(f: concurrent.futures.Future[Any]) -> None:
        if f.cancelled() or f.exception() is not None:
            return
        with contextlib.suppress(Exception):
            cleanup(f.result())

    future.add_done_callback(done)


def _next_or_end(subscription: Any) -> Any:
    """The next item of a sila2 subscription, or ``_END`` once it has ended or been cancelled by us.

    Cancelling a subscription can queue the stream's own CANCELLED error ahead of the end marker; that
    is not a server error."""
    try:
        return next(subscription, _END)
    except Exception as exc:
        inner = getattr(exc, "exception", exc)  # sila2 wraps non-SiLA gRPC errors in SilaConnectionError
        code = getattr(inner, "code", None)
        if callable(code) and getattr(code(), "name", "") == "CANCELLED":
            return _END
        raise


def _cancel_quietly(subscription: Any) -> None:
    with contextlib.suppress(Exception):
        subscription.cancel()


def _close_quietly(client: Any) -> None:
    with contextlib.suppress(Exception):
        client.close()


def seconds_option(value: Any, name: str, maximum: float = MAX_CALL_TIMEOUT_S) -> float:
    """Validate a timeout option (seconds): a finite number in (0, maximum]."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not (math.isfinite(seconds) and 0 < seconds <= maximum):
        raise InstrumentConnectionError(f"{name} must be a number of seconds between 0 and {maximum:g}, got {value!r}.")
    return seconds


def parse_address(address: str) -> tuple[str, int]:
    """``host:port``, ``sila://host:port`` or ``[ipv6]:port`` -> (host, port)."""
    text = address.strip()
    text = re.sub(r"^(sila|grpc|tcp)://", "", text, flags=re.IGNORECASE).rstrip("/")
    m = re.fullmatch(r"\[(?P<h6>[0-9a-fA-F:]+)\]:(?P<p6>\d+)|(?P<h>[^:\s/]+):(?P<p>\d+)", text)
    if not m:
        raise InstrumentConnectionError(
            f"SiLA address must look like host:port (e.g. 192.168.1.40:50052 or sila://robot.lab:50052), got {address!r}."
        )
    host = m.group("h6") or m.group("h")
    port = int(m.group("p6") or m.group("p"))
    if not 0 < port < 65536:
        raise InstrumentConnectionError(f"Invalid port {port}")
    return host, port


def parse_allowlist(spec: str | None) -> list[tuple[str, str]] | None:
    """``"TemperatureController.SetRampRate, Shaker.*"`` -> [(feature, command), ...] (lower-case)."""
    if spec is None or not spec.strip():
        return None
    out = []
    for item in re.split(r"[,;\s]+", spec.strip()):
        if not item:
            continue
        feat, sep, cmd = item.rpartition(".")
        if not sep or not feat or not cmd:
            raise ValueError(f"command_allowlist entries must look like Feature.Command or Feature.*, got {item!r}")
        out.append((feat.lower(), cmd.lower()))
    return out


def sila_error_message(exc: BaseException, feature: FeatureIR | None = None) -> str:
    """Readable text for sila2 errors (defined/undefined execution errors, validation, framework)."""
    name = type(exc).__name__
    ident = getattr(exc, "identifier", None)
    message = getattr(exc, "message", None) or str(exc)
    if ident and feature is not None and ident in feature.errors:
        display, description = feature.errors[ident]
        return f"SiLA defined execution error {ident} ({display}): {message}. {description}"
    if name == "DefinedExecutionError":
        return f"SiLA defined execution error {getattr(exc, 'fully_qualified_identifier', ident)}: {message}"
    if name == "ValidationError":
        param = getattr(exc, "parameter_fully_qualified_identifier", None)
        return f"SiLA validation error{f' for {param}' if param else ''}: {message}"
    if name == "UndefinedExecutionError":
        return f"SiLA undefined execution error: {message}"
    if name in {"CommandExecutionNotFinished", "InvalidCommandExecutionUUID", "CommandExecutionNotAccepted",
                "InvalidMetadata", "NoMetadataAllowed", "FrameworkError"}:
        return f"SiLA framework error ({name}): {message}"
    if name == "SilaConnectionError" or "RpcError" in name:
        return f"Connection to the SiLA server failed: {message}"
    return f"{name}: {message}"


def _undecodable_response(exc: BaseException) -> bool:
    """sila2 <= 0.14 fails to decode top-level Constrained List responses ('... of message X_Responses')."""
    text = str(exc)
    return "Failed to parse field" in text and ("_Responses" in text or "_IntermediateResponses" in text)


@dataclass
class Execution:
    """A tracked observable command execution."""

    execution_id: str
    feature: str
    command: str
    instance: Any
    started: float
    parameters: dict[str, Any]
    intermediate: list[Any] = field(default_factory=list)
    subscription: Any = None


class SilaBridge:
    """One connection to a SiLA 2 server plus parsed Feature Definitions."""

    def __init__(
        self,
        client: Any,
        *,
        address: str,
        allowlist: list[tuple[str, str]] | None = None,
        call_timeout: float = 30.0,
        audit: Any | None = None,
        on_close: Any | None = None,
        security: str = "",
    ) -> None:
        self.client = client
        self.address = address
        self.allowlist = allowlist
        self.call_timeout = call_timeout
        self.audit = audit
        self._on_close = on_close
        self.security = security
        self.lock = threading.RLock()
        self.executions: OrderedDict[str, Execution] = OrderedDict()
        self._closed = False
        self.features: dict[str, FeatureIR] = {}
        try:
            self._load_features()
        except Exception:
            self.close()
            raise

    def _load_features(self) -> None:
        client = self.client
        for fqi in self._call(lambda: client.SiLAService.ImplementedFeatures.get(), "listing features"):
            xml = self._call(lambda f=fqi: client.SiLAService.GetFeatureDefinition(f).FeatureDefinition,
                             f"reading the definition of {fqi}")
            try:
                ir = parse_feature(xml)
            except Exception as exc:  # malformed (or hostile) vendor FDL
                self._event(f"could not parse feature {fqi}: {exc}")
                continue
            if not self._client_has(ir.identifier):
                # sila2 validates FDL against FeatureDefinition.xsd and skips invalid features; listing
                # them here would only lead to confusing errors when they are used.
                self._event(f"sila2 could not load feature {fqi} (invalid Feature Definition); it is not offered")
                continue
            self.features[ir.identifier] = ir

    def _client_has(self, identifier: str) -> bool:
        loaded = getattr(self.client, "_features", None)
        if isinstance(loaded, dict):
            return identifier in loaded
        return hasattr(self.client, identifier)

    # ------------------------------------------------------------ helpers

    def _event(self, message: str) -> None:
        if self.audit is not None:
            self.audit.event(message, "sila2")

    def _call(self, fn: Any, doing: str, timeout: float | None = None, feature: FeatureIR | None = None,
              on_late: Any | None = None) -> Any:
        """Run a blocking sila2 call with a deadline (sila2's client calls have none).

        ``on_late(result)`` is called if the call succeeds after the deadline (to cancel a
        subscription or track an execution that would otherwise leak)."""
        limit = self.call_timeout if timeout is None else timeout
        future = _run_in_thread(fn)
        try:
            return future.result(timeout=limit)
        except (InstrumentProtocolError, InstrumentConnectionError, InstrumentTimeout):
            raise
        except Exception as exc:
            if _deadline_passed(exc, future):
                if on_late is not None:
                    _when_late(future, on_late)
                raise InstrumentTimeout(
                    f"The SiLA server did not answer within {limit:g} s "
                    f"while {doing}. It may still be executing the request - check its state before retrying."
                ) from exc
            msg = sila_error_message(exc, feature)
            if "Connection to the SiLA server failed" in msg or "StatusCode.UNAVAILABLE" in str(exc):
                raise InstrumentConnectionError(f"{msg} (while {doing}). Check the address and TLS options.") from exc
            raise InstrumentProtocolError(f"{msg} (while {doing})") from exc

    def feature(self, name: str) -> FeatureIR:
        """Find a feature by identifier (case-insensitive) or fully qualified identifier."""
        key = name.strip()
        for ir in self.features.values():
            if key.lower() in {ir.identifier.lower(), ir.fully_qualified_identifier.lower()}:
                return ir
        raise InstrumentProtocolError(
            f"The server does not implement a feature {name!r}. Available: {', '.join(sorted(self.features))}."
        )

    def _client_feature(self, ir: FeatureIR) -> Any:
        try:
            return getattr(self.client, ir.identifier)
        except AttributeError as exc:
            raise InstrumentProtocolError(
                f"The sila2 client did not load feature {ir.identifier} (invalid Feature Definition?)."
            ) from exc

    def allowed(self, feature: str, command: str) -> bool:
        if self.allowlist is None:
            return True
        f, c = feature.lower(), command.lower()
        return any(af == f and ac in {c, "*"} for af, ac in self.allowlist)

    @property
    def can_cancel(self) -> bool:
        return any(ir.fully_qualified_identifier == CANCEL_CONTROLLER for ir in self.features.values())

    # ------------------------------------------------------------ server / features

    def identify(self) -> dict[str, Any]:
        s = self.client.SiLAService
        info = {"address": self.address, "security": self.security}
        for key, prop in (("server_name", "ServerName"), ("server_type", "ServerType"), ("server_uuid", "ServerUUID"),
                          ("server_version", "ServerVersion"), ("vendor_url", "ServerVendorURL")):
            with contextlib.suppress(Exception):
                info[key] = self._call(lambda p=prop: getattr(s, p).get(), f"reading {prop}", timeout=10)
        info["features"] = sorted(ir.fully_qualified_identifier for ir in self.features.values())
        info["cancellation_supported"] = self.can_cancel
        info["command_allowlist"] = [f"{f}.{c}" for f, c in self.allowlist] if self.allowlist is not None else None
        return info

    def server_info(self) -> dict[str, Any]:
        info = self.identify()
        with contextlib.suppress(Exception):
            info["server_description"] = self._call(
                lambda: self.client.SiLAService.ServerDescription.get(), "reading ServerDescription", timeout=10
            )
        return info

    def describe_feature(self, ir: FeatureIR, include_fdl: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "identifier": ir.identifier,
            "fully_qualified_identifier": ir.fully_qualified_identifier,
            "display_name": ir.display_name,
            "description": ir.description,
            "feature_version": ir.feature_version,
            "maturity_level": ir.maturity_level,
            "commands": [],
            "properties": [],
        }
        for cmd in ir.commands.values():
            out["commands"].append({
                "identifier": cmd.identifier,
                "display_name": cmd.display_name,
                "description": cmd.description,
                "observable": cmd.observable,
                "callable": self.allowed(ir.identifier, cmd.identifier),
                "parameters": element_schema(cmd.parameters, ir),
                "responses": element_schema(cmd.responses, ir),
                "intermediate_responses": element_schema(cmd.intermediate_responses, ir),
                "defined_execution_errors": {e: ir.errors.get(e, ("", ""))[1] for e in cmd.errors},
            })
        for prop in ir.properties.values():
            out["properties"].append({
                "identifier": prop.identifier,
                "display_name": prop.display_name,
                "description": prop.description,
                "observable": prop.observable,
                "type": {**to_json_schema(prop.type, ir), "x-sila-type-label": type_label(prop.type, ir)},
            })
        if ir.metadata:
            out["metadata"] = element_schema(list(ir.metadata.values()), ir)
        if include_fdl:
            out["feature_definition_xml"] = ir.xml
        return out

    # ------------------------------------------------------------ metadata

    def _metadata(self, metadata: dict[str, Any] | None) -> list[Any] | None:
        """``{"LockController.LockIdentifier": "abc"}`` -> sila2 ClientMetadataInstances."""
        if not metadata:
            return None
        out = []
        for key, value in metadata.items():
            feat, sep, meta_id = key.rpartition(".")
            if not sep:
                raise InstrumentProtocolError(f"Metadata keys must look like Feature.Metadata, got {key!r}")
            ir = self.feature(feat)
            if meta_id not in ir.metadata:
                raise InstrumentProtocolError(f"Feature {ir.identifier} defines no metadata {meta_id!r}.")
            try:
                native = Converter(ir).convert(value, ir.metadata[meta_id].type, key)
            except FDLValidationError as exc:
                raise InstrumentProtocolError(f"Refused: {exc}. Nothing was sent.") from exc
            out.append(getattr(self._client_feature(ir), meta_id)(native))
        return out

    # ------------------------------------------------------------ properties

    def get_property(self, feature: str, prop: str, metadata: dict[str, Any] | None = None,
                     timeout: float | None = None) -> tuple[FeatureIR, Any]:
        ir = self.feature(feature)
        p = self._property(ir, prop)
        handle = getattr(self._client_feature(ir), p.identifier)
        meta = self._metadata(metadata)
        doing = f"reading {ir.identifier}.{p.identifier}"
        if not p.observable:
            value = self._call(lambda: handle.get(metadata=meta), doing, timeout=timeout, feature=ir)
            return ir, to_jsonable(value)
        # sila2's ObservableProperty.get() blocks until the first value with no way to give up, leaving
        # the stream (and a thread) behind on a timeout. Subscribe, wait for one value, always cancel.
        sub = self._call(lambda: handle.subscribe(metadata=meta), doing, timeout=timeout, feature=ir,
                         on_late=_cancel_quietly)
        try:
            value = self._call(lambda: _next_or_end(sub), doing, timeout=timeout, feature=ir)
        finally:
            _cancel_quietly(sub)  # also unblocks the waiting thread after a timeout
        if value is _END:
            raise InstrumentProtocolError(f"The SiLA server ended the subscription to {ir.identifier}.{p.identifier} "
                                          "without sending a value.")
        return ir, to_jsonable(value)

    def _property(self, ir: FeatureIR, prop: str) -> Any:
        for p in ir.properties.values():
            if p.identifier.lower() == prop.strip().lower():
                return p
        raise InstrumentProtocolError(
            f"Feature {ir.identifier} has no property {prop!r}. Properties: {', '.join(ir.properties) or 'none'}."
        )

    def subscribe_property(self, feature: str, prop: str, duration_s: float, max_updates: int,
                           poll_interval_s: float, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Collect updates of a property for ``duration_s``: a SiLA subscription for observable
        properties, polling ``get()`` for unobservable ones."""
        ir = self.feature(feature)
        p = self._property(ir, prop)
        handle = getattr(self._client_feature(ir), p.identifier)
        meta = self._metadata(metadata)
        updates: list[tuple[float, Any]] = []
        updates_lock = threading.Lock()
        done = threading.Event()
        t0 = time.time()
        deadline = time.monotonic() + duration_s  # immune to wall-clock steps
        if p.observable:
            def on_value(value: Any) -> None:  # sila2 may run callbacks concurrently
                with updates_lock:
                    if len(updates) < max_updates:
                        updates.append((time.time(), to_jsonable(value)))
                    if len(updates) >= max_updates:
                        done.set()

            doing = f"subscribing to {ir.identifier}.{p.identifier}"
            sub = self._call(lambda: handle.subscribe(metadata=meta, callbacks=[on_value]), doing, feature=ir,
                             on_late=_cancel_quietly)
            try:
                done.wait(max(0.0, deadline - time.monotonic()))
            finally:
                _cancel_quietly(sub)
            if not updates and getattr(sub, "is_cancelled", False):
                # No value at all: the server may have rejected or broken the stream (e.g. missing
                # metadata). The first queued item tells; after cancel() the queue always ends.
                item = self._call(lambda: _next_or_end(sub), doing, timeout=2.0, feature=ir)
                if item is not _END:
                    updates.append((time.time(), to_jsonable(item)))
            mode = "subscription"
        else:
            while time.monotonic() < deadline and len(updates) < max_updates:
                value = self._call(lambda: handle.get(metadata=meta), f"reading {ir.identifier}.{p.identifier}",
                                   feature=ir)
                updates.append((time.time(), to_jsonable(value)))
                time.sleep(max(0.0, min(poll_interval_s, deadline - time.monotonic())))
            mode = "polling"
        with updates_lock:
            collected = list(updates)
        return {"feature": ir, "property": p, "mode": mode, "updates": collected, "started": t0}

    # ------------------------------------------------------------ commands

    def command_ir(self, ir: FeatureIR, command: str) -> Any:
        for c in ir.commands.values():
            if c.identifier.lower() == command.strip().lower():
                return c
        raise InstrumentProtocolError(
            f"Feature {ir.identifier} has no command {command!r}. Commands: {', '.join(ir.commands) or 'none'}."
        )

    def prepare(self, feature: str, command: str, parameters: dict[str, Any]) -> tuple[FeatureIR, Any, dict[str, Any], list[str]]:
        """Check the allow-list and validate parameters against the FDL. Nothing is sent."""
        ir = self.feature(feature)
        cmd = self.command_ir(ir, command)
        if not self.allowed(ir.identifier, cmd.identifier):
            allowed = ", ".join(f"{f}.{c}" for f, c in self.allowlist or [])
            raise InstrumentProtocolError(
                f"Refused: {ir.identifier}.{cmd.identifier} is not in the command allow-list ({allowed}). "
                "Nothing was sent. Restart the server with a different --option command_allowlist if intended."
            )
        try:
            native, warnings = convert_parameters(ir, cmd, parameters or {})
        except FDLValidationError as exc:
            raise InstrumentProtocolError(f"Refused: {exc}. Nothing was sent.") from exc
        return ir, cmd, native, warnings

    def call(self, feature: str, command: str, parameters: dict[str, Any], metadata: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict[str, Any]:
        ir, cmd, native, warnings = self.prepare(feature, command, parameters)
        meta = self._metadata(metadata)
        handle = getattr(self._client_feature(ir), cmd.identifier)
        if self.audit is not None:
            self.audit.record("write", f"{ir.identifier}.{cmd.identifier}({to_jsonable(native)})", "sila2")
        kwargs = dict(native)
        if meta:
            kwargs["metadata"] = meta

        def late(result: Any) -> None:
            # The server accepted/answered after the deadline: keep the execution visible (and
            # cancellable) instead of leaking it, and record late unobservable responses.
            if cmd.observable:
                exec_id = self._track(ir, cmd, result, parameters)
                self._event(f"{ir.identifier}.{cmd.identifier} was accepted after the deadline: execution {exec_id}")
            elif self.audit is not None:
                self.audit.record("read", f"{ir.identifier}.{cmd.identifier} -> {to_jsonable(result)} (late)", "sila2")

        try:
            result = self._call(lambda: handle(**kwargs), f"calling {ir.identifier}.{cmd.identifier}",
                                timeout=timeout, feature=ir, on_late=late)
        except InstrumentProtocolError as exc:
            if _undecodable_response(exc):
                raise InstrumentProtocolError(
                    f"{ir.identifier}.{cmd.identifier} WAS EXECUTED by the server, but its response could not be "
                    f"decoded ({exc}). This is a known sila2 <= 0.14 limitation with Constrained List responses. "
                    "Do not repeat the command; check the device state with get_property instead."
                ) from exc
            raise
        if not cmd.observable:
            responses = to_jsonable(result)
            if self.audit is not None:
                self.audit.record("read", f"{ir.identifier}.{cmd.identifier} -> {responses}", "sila2")
            return {"feature": ir, "command": cmd, "observable": False, "responses": responses, "warnings": warnings}
        exec_id = self._track(ir, cmd, result, parameters)
        self._event(f"started {ir.identifier}.{cmd.identifier} execution {exec_id}")
        deadline = time.monotonic() + 1.0  # give the execution-info stream a moment to report a status
        while result.status is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return {"feature": ir, "command": cmd, "observable": True, "execution_id": exec_id, "warnings": warnings}

    def _track(self, ir: FeatureIR, cmd: Any, result: Any, parameters: dict[str, Any]) -> str:
        """Remember an observable command execution (and follow its intermediate responses)."""
        exec_id = str(result.execution_uuid)
        execution = Execution(exec_id, ir.identifier, cmd.identifier, result, time.time(), to_jsonable(parameters))
        if cmd.intermediate_responses and hasattr(result, "subscribe_to_intermediate_responses"):
            def on_intermediate(value: Any, ex: Execution = execution) -> None:
                ex.intermediate.append(to_jsonable(value))
                del ex.intermediate[:-20]  # keep the most recent 20

            with contextlib.suppress(Exception):
                sub = result.subscribe_to_intermediate_responses()
                sub.add_callback(on_intermediate)
                execution.subscription = sub
        with self.lock:
            if self._closed:  # a late result after reconnect: nothing to track it for
                self._release(execution)
                return exec_id
            self.executions[exec_id] = execution
            while len(self.executions) > MAX_TRACKED:
                _, old = self.executions.popitem(last=False)
                self._release(old)
        return exec_id

    def execution(self, execution_id: str) -> Execution:
        key = execution_id.strip().lower()
        with self.lock:
            ex = self.executions.get(key)
        if ex is None:
            raise InstrumentProtocolError(
                f"Unknown execution id {execution_id!r}: only observable commands started by this server session "
                "are tracked (they are forgotten on reconnect)."
            )
        return ex

    def status(self, execution_id: str) -> dict[str, Any]:
        ex = self.execution(execution_id)
        inst = ex.instance
        status = inst.status.name if inst.status is not None else "unknown (no execution info received yet)"
        remaining = inst.estimated_remaining_time
        lifetime = inst.lifetime_of_execution
        progress = None if inst.progress is None else float(inst.progress)
        return {
            "execution": ex,
            "status": status,
            "done": bool(inst.done),
            "progress": progress if progress is not None and math.isfinite(progress) else None,
            "estimated_remaining_s": None if remaining is None else remaining.total_seconds(),
            "lifetime_of_execution_s": None if lifetime is None else lifetime.total_seconds(),
            "latest_intermediate": ex.intermediate[-1] if ex.intermediate else None,
        }

    def wait(self, execution_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        ex = self.execution(execution_id)
        while not ex.instance.done and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.status(execution_id)

    def result(self, execution_id: str) -> dict[str, Any]:
        ex = self.execution(execution_id)
        ir = self.features.get(ex.feature)
        st = self.status(execution_id)
        if not st["done"]:
            raise InstrumentProtocolError(
                f"Execution {execution_id} of {ex.feature}.{ex.command} has not finished (status {st['status']}, "
                f"progress {st['progress']}). Check again later with get_command_status."
            )
        try:
            responses = self._call(lambda: ex.instance.get_responses(), f"reading the result of {execution_id}",
                                   feature=ir)
        except InstrumentProtocolError as exc:
            error = str(exc)
            if _undecodable_response(exc):
                error = (f"The command finished ({st['status']}) but its responses could not be decoded: {error}. "
                         "This is a known sila2 <= 0.14 limitation with Constrained List responses.")
            return {"execution": ex, "status": st["status"], "success": False, "responses": None, "error": error}
        return {"execution": ex, "status": st["status"], "success": True, "responses": to_jsonable(responses),
                "error": None}

    def cancel(self, execution_id: str | None) -> dict[str, Any]:
        """Cancel one execution (or all) through the server's CancelController feature."""
        if not self.can_cancel:
            return {
                "supported": False,
                "message": "This SiLA server does not implement the CancelController feature "
                f"({CANCEL_CONTROLLER}), so running commands cannot be cancelled through SiLA. Use the "
                "instrument's own stop/abort command if it has one (see list_features), or its physical stop button.",
            }
        cancel = self.feature(CANCEL_CONTROLLER)
        handle = self._client_feature(cancel)
        if execution_id is None:
            self._event("CancelController.CancelAll")
            self._call(lambda: handle.CancelAll(), "cancelling all commands", feature=cancel)
            return {"supported": True, "message": "CancelAll sent: the server cancels every running command."}
        try:
            ex_id = str(UUID(execution_id.strip()))  # canonical form, as the CancelController's UUID type requires
        except ValueError as exc:
            raise InstrumentProtocolError(f"{execution_id!r} is not a command execution UUID.") from exc
        self._event(f"CancelController.CancelCommand {ex_id}")
        self._call(lambda: handle.CancelCommand(CommandExecutionUUID=ex_id), f"cancelling {ex_id}", feature=cancel)
        return {"supported": True, "message": f"Cancellation of {ex_id} requested."}

    # ------------------------------------------------------------ lifecycle

    def _release(self, ex: Execution) -> None:
        with contextlib.suppress(Exception):
            if ex.subscription is not None:
                ex.subscription.cancel()
        with contextlib.suppress(Exception):
            ex.instance.cancel_execution_info_subscription()

    def close(self) -> None:
        with self.lock:
            self._closed = True
            for ex in self.executions.values():
                self._release(ex)
            self.executions.clear()
        _close_quietly(self.client)  # closing the channel also ends calls still waiting past their deadline
        if self._on_close is not None:
            with contextlib.suppress(Exception):
                self._on_close()


# --------------------------------------------------------------------------- connecting / discovery


def read_file(path: str | None, what: str) -> bytes | None:
    if not path:
        return None
    p = Path(path).expanduser()
    try:
        return p.read_bytes()
    except OSError as exc:
        raise InstrumentConnectionError(f"Cannot read {what} {p}: {exc}") from exc


def open_client(host: str, port: int, *, insecure: bool, root_certs: bytes | None, private_key: bytes | None,
                cert_chain: bytes | None, timeout: float) -> Any:
    """Create a ``sila2`` SilaClient with a connection deadline."""
    from sila2.client import SilaClient

    if not logging.getLogger("labmcp").isEnabledFor(logging.DEBUG):
        logging.getLogger("sila2").setLevel(logging.WARNING)  # sila2 logs every call at INFO

    def make() -> Any:
        if insecure:
            return SilaClient(host, port, insecure=True)
        return SilaClient(host, port, root_certs=root_certs, private_key=private_key, cert_chain=cert_chain)

    future = _run_in_thread(make, "labmcp-sila2-connect")
    try:
        return future.result(timeout=timeout)
    except Exception as exc:
        if _deadline_passed(exc, future):
            _when_late(future, _close_quietly)  # don't leak a channel that connects after we gave up
            raise InstrumentConnectionError(
                f"No SiLA server answered at {host}:{port} within {timeout:g} s. Check the address, that the server "
                "is running, and TLS: most servers use TLS with a self-signed certificate (pass --option "
                "root_cert=<CA PEM>), test servers often run with --insecure (then pass --option insecure=true)."
            ) from exc
        raise InstrumentConnectionError(
            f"Could not connect to the SiLA server at {host}:{port}: {sila_error_message(exc)}. If the server uses "
            "TLS with a self-signed certificate pass --option root_cert=<CA PEM>; for unencrypted test servers pass "
            "--option insecure=true."
        ) from exc


def discover_client(*, server_name: str | None, server_uuid: str | None, discovery_timeout: float,
                    connect_timeout: float, insecure: bool, root_certs: bytes | None, private_key: bytes | None,
                    cert_chain: bytes | None) -> Any:
    """Find a server with SiLA Server Discovery and connect to it, within an overall deadline."""
    from sila2.client import SilaClient

    def find() -> Any:
        return SilaClient.discover(server_name=server_name, server_uuid=server_uuid, timeout=discovery_timeout,
                                   insecure=insecure, root_certs=root_certs, private_key=private_key,
                                   cert_chain=cert_chain)

    future = _run_in_thread(find, "labmcp-sila2-discover")
    try:
        return future.result(timeout=discovery_timeout + connect_timeout)
    except Exception as exc:
        if _deadline_passed(exc, future):
            _when_late(future, _close_quietly)
            raise InstrumentConnectionError(
                f"A SiLA server matching name={server_name!r} uuid={server_uuid!r} was not reached within "
                f"{discovery_timeout + connect_timeout:g} s (discovery + connection)."
            ) from exc
        if isinstance(exc, TimeoutError):  # sila2: nothing matching was discovered
            raise InstrumentConnectionError(
                f"No SiLA server matching name={server_name!r} uuid={server_uuid!r} was discovered."
            ) from exc
        raise InstrumentConnectionError(f"Could not connect to the discovered SiLA server: {sila_error_message(exc)}") from exc


def discover(timeout_s: float) -> list[dict[str, Any]]:
    """Browse mDNS for ``_sila._tcp.local.`` services (SiLA Server Discovery) for ``timeout_s``.

    Returns what each server advertises (UUID = instance name, TXT records ``server_name``,
    ``description``, ``version``, ``ca0..`` = CA certificate for self-signed TLS) without connecting."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    # Listener callbacks run on the browser thread, which browser.cancel() joins: keep each lookup short
    # so the scan ends close to timeout_s.
    info_timeout_ms = int(min(3000, max(200, timeout_s * 500)))

    class Listener(ServiceListener):
        def _add(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=info_timeout_ms)
            if info is None:
                return
            props = {
                (k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)):
                (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                for k, v in (info.properties or {}).items()
            }
            with lock:
                found[name] = {
                    "server_uuid": name.split(".")[0],
                    "server_name": props.get("server_name"),
                    "description": props.get("description"),
                    "version": props.get("version"),
                    "addresses": info.parsed_addresses(),
                    "port": info.port,
                    "advertises_ca_certificate": any(k.startswith("ca") and k[2:].isdigit() for k in props),
                }

        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self._add(zc, type_, name)

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self._add(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            with lock:
                found.pop(name, None)

    try:
        zc = Zeroconf()
    except OSError as exc:
        raise InstrumentConnectionError(f"mDNS discovery could not start on this computer: {exc}") from exc
    try:
        browser = ServiceBrowser(zc, "_sila._tcp.local.", Listener())
        try:
            time.sleep(timeout_s)
        finally:
            with contextlib.suppress(Exception):
                browser.cancel()
    finally:
        with contextlib.suppress(Exception):
            zc.close()
    with lock:
        return sorted(found.values(), key=lambda d: (d.get("server_name") or "", d["server_uuid"]))
