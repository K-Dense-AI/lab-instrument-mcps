"""Backend that drives a real Orbitrap through the scientist's own Thermo IAPI assemblies.

The Thermo Fisher Instrument API (IAPI) is a Windows .NET API that runs on the instrument PC
next to Tune. Its assemblies are **not** bundled with, or downloaded by, this package: using
them requires a signed IAPI software licence agreement with Thermo Fisher Scientific (see
https://github.com/thermofisherlsms/iapi/blob/master/GettingStarted.md#legal-requirements and
the licensing guidance PDF in that repository's ``docs`` folder). The scientist points the
server at a folder containing the assemblies they obtained with ``--option assembly_dir=...``.

Everything is taken from the official repository https://github.com/thermofisherlsms/iapi
(commit c246dcc8772d03c9c32e9b2fde486e97572c8fbf):

* Interface definitions: ``lib/API-2.0.xml`` (``Thermo.Interfaces.InstrumentAccess_V1``:
  ``IInstrumentAccessContainer``, ``IInstrumentAccess``, ``IMsScanContainer``, ``IMsScan``,
  ``MsScanEventArgs``, ``IControl``, ``IAcquisition``, ``IState``, ``IAcquisitionWorkflow``,
  ``IScans``, ``IScanDefinition``, ``ICustomScan``, ``IParameterDescription``,
  ``IInstrumentValues``, ``IReadback``, ``IContent``), ``lib/Spectrum-1.0.xml``
  (``ISpectrum``, ``IMassIntensity``, ``ICentroid``, ``IInformationSourceAccess``),
  ``lib/tribrid/Thermo.TNG.Factory.XML`` (``Factory<T>.Create``) and
  ``lib/Exploris4.3-and-higher/Thermo.API.Exploris.NetStd-1.0.xml``
  (``IExplorisInstrumentAccessContainer``, ``IExplorisInstrumentAccess.Licenses``).
* Connection recipes: Tribrid ``Factory<IFusionInstrumentAccessContainer>.Create()`` then
  ``StartOnlineAccess()`` and ``Get(1)`` (``examples/tribrid/MinifiedExample/Program.cs``);
  Exploris: registry ``HKLM\\SOFTWARE\\Thermo Exploris`` value ``data`` (64-bit view) or
  ``%ProgramData%\\Thermo\\Exploris``, then ``DataSystem.xml`` elements ``ApiFileName`` and
  ``ApiClassName`` (``examples/Exploris/3 DataListening/Connection.cs``); Exactive / Q Exactive:
  registry ``HKLM\\SOFTWARE\\Finnigan\\Xcalibur\\Devices\\Thermo Exactive`` values
  ``ApiFileName_Clr2_32_V1`` / ``ApiClassName_Clr2_32_V1``
  (``docs/exactive/2 KeepInstrumentConnection/KeepInstrumentConnection/Connection.cs``).
* Scan handling (``e.GetScan()`` must be disposed quickly, or shared memory stays blocked):
  ``examples/Exploris/3 DataListening/DataReceiver.cs``.
* Acquisition workflow (``CreateAcquisitionLimitedByDuration`` + ``RawFileName`` +
  ``StartAcquisition``, ``CancelAcquisition``) and custom / repeating scans
  (``CreateCustomScan``/``SetCustomScan``/``CancelCustomScan``,
  ``CreateRepeatingScan``/``SetRepetitionScan``/``CancelRepetition``):
  ``examples/tribrid/FusionExampleClient2pt0/Form1.cs``.
* Exception types that IAPI documents for control calls (``CommunicationException``,
  ``AccessViolationException``, ``InvalidOperationException``) and the licence exception
  (``PrivilegeNotHeldException``) from ``lib/API-2.0.xml`` and the Exploris ``Connection.cs``.

Every .NET member is reached through :func:`_net` / :func:`_net_set` / :func:`_net_subscribe`
/ :func:`_net_type` with its ``"Interface.Member"`` name, and a test checks each of those names
against :data:`labmcp_thermo_iapi.iapi_members.VERIFIED_IAPI_MEMBERS`.

This backend has **never been run against a real instrument**. Instrument readback names
(vacuum gauges, spray voltage, ...) are not listed in the IAPI repository; they differ by model
and are reported as whatever ``IInstrumentValues.ValueNames`` and the scan ``StatusLog`` contain.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from labmcp import InstrumentConnectionError, InstrumentError, InstrumentProtocolError

from labmcp_thermo_iapi.backend import (
    AcquisitionMode,
    Centroid,
    OrbitrapBackend,
    ParameterDescription,
    ScanRecord,
)

log = logging.getLogger("labmcp.thermo_iapi")

FAMILIES = ("tribrid", "exploris", "exactive")

#: For each family, groups of assembly file names; at least one file of every group must exist.
REQUIRED_ASSEMBLIES: dict[str, list[tuple[str, ...]]] = {
    # GettingStarted.md, "Add references to the following assemblies"
    "tribrid": [
        ("API-2.0.dll",),
        ("Spectrum-1.0.dll",),
        ("Thermo.TNG.Factory.dll",),
        ("Fusion.API-2.0.dll", "Fusion.API-1.0.dll"),
    ],
    # examples/Exploris/*/Dependencies and lib/Exploris4.3-and-higher
    "exploris": [
        ("Thermo.API.NetStd-1.0.dll", "Thermo.API-2.0.dll"),
        ("Thermo.API.Exploris.NetStd-1.0.dll", "Thermo.API.Exploris-1.0.dll"),
        ("Thermo.API.Spectrum.NetStd-1.0.dll", "Thermo.API.Spectrum-1.1.dll", "Thermo.API.Spectrum-1.0.dll"),
    ],
    # lib/ and lib/exactive
    "exactive": [
        ("API-2.0.dll", "API-1.1.dll", "API-1.0.dll"),
        ("Spectrum-1.0.dll",),
        ("ESAPI-1.1.dll", "ESAPI-1.0.dll"),
    ],
}

LICENCE_HELP = (
    "Controlling the instrument through IAPI requires a signed IAPI software licence agreement "
    "with Thermo Fisher Scientific and the licence applied to the instrument (see "
    "https://github.com/thermofisherlsms/iapi/blob/master/GettingStarted.md#legal-requirements). "
    "Reading scans and status may work without it; custom scans and acquisitions do not."
)

MAX_CENTROIDS_KEPT = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_windows() -> bool:
    return sys.platform == "win32"


# ------------------------------------------------------------------ .NET access helpers
# Every IAPI member goes through one of these with its "Interface.Member" name, so the test
# suite can check each name against the verified list in iapi_members.py.


def _net(obj: Any, member: str) -> Any:
    """Read ``obj.<Member>`` for the IAPI member ``"Interface.Member"``.

    pythonnet wraps objects as the *declared* return type; when a member lives on a derived
    or sibling interface, fall back to the concrete object (``__implementation__``).
    """
    name = member.rsplit(".", 1)[-1]
    try:
        return getattr(obj, name)
    except AttributeError:
        impl = getattr(obj, "__implementation__", None)
        if impl is None or impl is obj:
            raise
        return getattr(impl, name)


def _net_set(obj: Any, member: str, value: Any) -> None:
    name = member.rsplit(".", 1)[-1]
    target = obj
    if not hasattr(obj, name):
        target = getattr(obj, "__implementation__", obj)
    setattr(target, name, value)


def _net_subscribe(obj: Any, member: str, handler: Callable[..., None], *, add: bool = True) -> None:
    """``obj.Event += handler`` (or ``-=``) for the IAPI event ``"Interface.Event"``."""
    name = member.rsplit(".", 1)[-1]
    binding = getattr(obj, name)
    if add:
        binding += handler
    else:
        binding -= handler
    setattr(obj, name, binding)


def _net_type(qualified: str) -> Any:
    """Import a .NET type by its fully qualified IAPI name (after the assemblies are loaded)."""
    namespace, _, type_name = qualified.rpartition(".")
    return getattr(importlib.import_module(namespace), type_name)


def _bcl(qualified: str) -> Any:
    """Import a .NET base-class-library type or member (``System....``)."""
    namespace, _, name = qualified.rpartition(".")
    return getattr(importlib.import_module(namespace), name)


def _dispose(obj: Any) -> None:
    """``IDisposable.Dispose()``: releases the shared memory behind a scan / the IScans lock."""
    try:
        obj.Dispose()
    except Exception:  # pragma: no cover - best effort
        pass


def _try_get(src: Any, name: str) -> str | None:
    """``IInformationSourceAccess.TryGetValue(name, out value)``; pythonnet returns (ok, value)."""
    result = _net(src, "IInformationSourceAccess.TryGetValue")(name, None)
    if isinstance(result, tuple):
        ok, value = result[0], result[1]
        return None if not ok or value is None else str(value)
    return None


def _info_dict(src: Any) -> dict[str, str]:
    """Copy an ``IInformationSourceAccess`` (trailer, status log) or a name/value header."""
    if src is None:
        return {}
    try:
        names = list(_net(src, "IInformationSourceAccess.ItemNames"))
    except AttributeError:
        names = None
    out: dict[str, str] = {}
    if names is not None:
        for n in names:
            v = _try_get(src, str(n))
            out[str(n)] = "" if v is None else v
        return out
    # IMsScan.Header: "a set of name/value pairs. A pure name has a value of null."
    for kv in src:
        key = getattr(kv, "Key", None)
        if key is None:
            continue
        value = getattr(kv, "Value", None)
        out[str(key)] = "" if value is None else str(value)
    return out


def _translate(exc: BaseException, action: str) -> InstrumentError:
    """Turn an IAPI exception into an error that tells the scientist what to do."""
    kind = type(exc).__name__
    text = str(exc) or kind
    if kind in {"PrivilegeNotHeldException", "UnauthorizedAccessException"} or "licen" in text.lower():
        return InstrumentConnectionError(f"IAPI refused to {action}: {text}. {LICENCE_HELP}")
    if kind == "CommunicationException":
        return InstrumentConnectionError(
            f"Could not {action}: the connection to the instrument is not established ({text}). "
            "Is Tune running and the instrument connected? Try `reconnect`."
        )
    if kind == "AccessViolationException":
        return InstrumentProtocolError(
            f"Could not {action}: the instrument is under exclusive use of another application "
            f"({text}). Close the other IAPI client or the exclusive Tune session."
        )
    if kind == "InvalidOperationException":
        return InstrumentProtocolError(
            f"Could not {action}: the instrument is not in the proper condition ({text}). "
            "Check the system mode with get_instrument_status (it usually must be On)."
        )
    return InstrumentProtocolError(f"Could not {action}: {kind}: {text}")


# ------------------------------------------------------------------------------ backend


class PythonNetBackend(OrbitrapBackend):
    kind = "pythonnet"

    def __init__(
        self,
        *,
        family: str,
        assembly_dir: str | None,
        runtime: str = "netfx",
        readback_names: list[str] | None = None,
        exclusive_scans: bool = False,
        connect_timeout_s: float = 10.0,
        instrument_index: int = 1,
    ) -> None:
        self.family = family.lower()
        self.assembly_dir = assembly_dir
        self.runtime = runtime
        self.readback_names = list(readback_names or [])
        self.exclusive_scans = exclusive_scans
        self.connect_timeout_s = connect_timeout_s
        self.instrument_index = instrument_index
        self._container: Any = None
        self._instrument: Any = None
        self._scan_container: Any = None
        self._control: Any = None
        self._acquisition: Any = None
        self._scans: Any = None
        self._handler: Callable[..., None] | None = None
        self._on_scan: Callable[[ScanRecord], None] | None = None
        self._readbacks: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- preflight checks

    def preflight(self) -> list[Path]:
        """Check the platform, the family and the assemblies; return the DLLs to load.

        Raises a clear :class:`InstrumentConnectionError` before touching pythonnet.
        """
        if not _is_windows():
            raise InstrumentConnectionError(
                f"The Thermo Instrument API (IAPI) is a Windows .NET API: it only runs on the Windows "
                f"PC that controls the Orbitrap (this is {sys.platform!r}). Run this server on the "
                "instrument PC with `pip install labmcp-thermo-iapi[windows]`, or use `--simulate` "
                "to try it without hardware."
            )
        if self.family not in FAMILIES:
            raise InstrumentConnectionError(
                f"--option instrument must be one of {', '.join(FAMILIES)} (got {self.family!r}): "
                "tribrid = Orbitrap Fusion/Lumos/Eclipse/Ascend (and Stellar); exploris = Orbitrap "
                "Exploris 240/480; exactive = Q Exactive family."
            )
        if not self.assembly_dir:
            raise InstrumentConnectionError(
                "No IAPI assemblies configured. IAPI is licensed by Thermo Fisher and is not bundled "
                "with this server: put the assemblies you obtained under your IAPI licence in a "
                "folder and start the server with `--option assembly_dir=C:\\path\\to\\iapi`. " + LICENCE_HELP
            )
        folder = Path(self.assembly_dir).expanduser()
        if not folder.is_dir():
            raise InstrumentConnectionError(
                f"--option assembly_dir={self.assembly_dir!r} does not exist or is not a folder."
            )
        dlls: list[Path] = []
        missing: list[str] = []
        for group in REQUIRED_ASSEMBLIES[self.family]:
            found = [folder / name for name in group if (folder / name).is_file()]
            if found:
                dlls.append(found[0])
            else:
                missing.append(" or ".join(group))
        if missing:
            raise InstrumentConnectionError(
                f"IAPI assemblies for a {self.family} instrument are missing from {str(folder)!r}: "
                f"{'; '.join(missing)}. These come with your IAPI licence from Thermo Fisher "
                "(they are not redistributed by LabMCP). " + LICENCE_HELP
            )
        return dlls

    def _load_clr(self, dlls: list[Path]) -> Any:
        try:
            import pythonnet
        except ImportError as exc:
            raise InstrumentConnectionError(
                "pythonnet is not installed. On the Windows instrument PC install the server with "
                "`pip install labmcp-thermo-iapi[windows]` (or `pip install pythonnet`)."
            ) from exc
        try:
            if pythonnet.get_runtime_info() is None:
                pythonnet.load(self.runtime)
        except Exception as exc:
            raise InstrumentConnectionError(
                f"pythonnet could not start the .NET runtime {self.runtime!r}: {exc}. IAPI targets "
                ".NET Framework 4.8 (`--option runtime=netfx`, the default)."
            ) from exc
        import clr

        folder = str(dlls[0].parent)
        if folder not in sys.path:
            sys.path.append(folder)
        for dll in dlls:
            try:
                clr.AddReference(str(dll))
            except Exception as exc:
                raise InstrumentConnectionError(f"Could not load {dll.name}: {exc}") from exc
        return clr

    # ------------------------------------------------------------------ connection

    def _create_container(self) -> Any:
        if self.family == "tribrid":
            factory = _net_type("Thermo.TNG.Factory.Factory")
            iface = _net_type("Thermo.Interfaces.FusionAccess_V1.IFusionInstrumentAccessContainer")
            return _net(factory[iface], "Factory.Create")()
        if self.family == "exploris":
            filename, classname = self._exploris_api_location()
            iface = _net_type("Thermo.Interfaces.ExplorisAccess_V1.IExplorisInstrumentAccessContainer")
        else:
            filename, classname = self._exactive_api_location()
            iface = _net_type("Thermo.Interfaces.InstrumentAccess_V1.IInstrumentAccessContainer")
        assembly = _bcl("System.Reflection.Assembly")
        asm = assembly.LoadFrom(filename) if os.path.isfile(filename) else assembly.Load(filename)
        instance = asm.CreateInstance(classname)
        if instance is None:
            raise InstrumentConnectionError(f"{filename} does not contain the IAPI class {classname!r}.")
        return iface(instance)

    def _exploris_api_location(self) -> tuple[str, str]:
        import winreg  # Windows only; preflight() has checked the platform

        base: str | None = None
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                "SOFTWARE\\Thermo Exploris",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                base = str(winreg.QueryValueEx(key, "data")[0])
        except OSError:
            base = None
        if not base or not os.path.isfile(os.path.join(base, "DataSystem.xml")):
            base = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "Thermo", "Exploris")
        config = os.path.join(base, "DataSystem.xml")
        if not os.path.isfile(config):
            raise InstrumentConnectionError(
                f"Exploris instrument software not found ({config} is missing). Run the server on "
                "the Exploris instrument PC with Tune installed."
            )
        root = ET.parse(config).getroot()
        filename = (root.findtext("ApiFileName") or "").strip()
        classname = (root.findtext("ApiClassName") or "").strip()
        if not filename or not classname:
            raise InstrumentConnectionError(f"{config} has no ApiFileName/ApiClassName entries.")
        return filename, classname

    def _exactive_api_location(self) -> tuple[str, str]:
        import winreg

        path = "SOFTWARE\\Finnigan\\Xcalibur\\Devices\\Thermo Exactive"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                filename = str(winreg.QueryValueEx(key, "ApiFileName_Clr2_32_V1")[0])
                classname = str(winreg.QueryValueEx(key, "ApiClassName_Clr2_32_V1")[0])
        except OSError as exc:
            raise InstrumentConnectionError(
                f"Cannot find the Exactive API in the registry (HKLM\\{path}): {exc}. Run the server "
                "on the instrument PC with Tune installed."
            ) from exc
        return filename, classname

    def open(self, on_scan: Callable[[ScanRecord], None]) -> None:
        dlls = self.preflight()
        self._load_clr(dlls)
        self._on_scan = on_scan
        try:
            container = self._create_container()
        except InstrumentError:
            raise
        except Exception as exc:
            raise _translate(exc, "create the IAPI instrument container") from exc
        self._container = container
        try:
            _net(container, "IInstrumentAccessContainer.StartOnlineAccess")()
        except AttributeError:
            pass  # older Exactive API: online by default
        deadline = time.monotonic() + self.connect_timeout_s
        while not self._service_connected():
            if time.monotonic() > deadline:
                raise InstrumentConnectionError(
                    f"The IAPI service did not connect within {self.connect_timeout_s:g} s. Is the "
                    "instrument software (Tune) running and the instrument switched on?"
                )
            time.sleep(0.1)
        try:
            self._instrument = _net(container, "IInstrumentAccessContainer.Get")(self.instrument_index)
            self._scan_container = _net(self._instrument, "IInstrumentAccess.GetMsScanContainer")(0)
            self._control = _net(self._instrument, "IInstrumentAccess.Control")
            self._acquisition = _net(self._control, "IControl.Acquisition")
        except Exception as exc:
            raise _translate(exc, "access the instrument") from exc
        self._handler = self._handle_scan
        _net_subscribe(self._scan_container, "IMsScanContainer.MsScanArrived", self._handler)

    def _service_connected(self) -> bool:
        try:
            return bool(_net(self._container, "IInstrumentAccessContainer.ServiceConnected"))
        except AttributeError:
            return True

    def _scans_iface(self) -> Any:
        with self._lock:
            if self._scans is None:
                try:
                    self._scans = _net(self._control, "IControl.GetScans")(self.exclusive_scans)
                except Exception as exc:
                    raise _translate(exc, "get access to the IScans interface") from exc
                if self._scans is None:
                    raise InstrumentProtocolError(
                        "IAPI did not grant access to IScans: another application holds exclusive "
                        "access to custom scans."
                    )
            return self._scans

    # --------------------------------------------------------------------- scans

    def _handle_scan(self, sender: Any, e: Any) -> None:
        try:
            scan = _net(e, "MsScanEventArgs.GetScan")()
        except Exception as exc:  # pragma: no cover - hardware only
            log.warning("GetScan failed: %s", exc)
            return
        try:
            record = self._copy_scan(scan)
        except Exception as exc:  # pragma: no cover - hardware only
            log.warning("Could not read an arriving scan: %s", exc)
            return
        finally:
            _dispose(scan)  # "caution! You must dispose this, or you block shared memory!"
        if self._on_scan is not None:
            self._on_scan(record)

    def _copy_scan(self, scan: Any) -> ScanRecord:
        received = _now()
        header = _info_dict(_net(scan, "IMsScan.Header"))
        trailer = _info_dict(_net(scan, "IMsScan.Trailer"))
        status_log: dict[str, str] = {}
        try:
            src = _net(scan, "IMsScan.StatusLog")
            if (
                src is not None
                and _net(src, "IInformationSourceAccess.Available")
                and _net(src, "IInformationSourceAccess.Valid")
            ):
                status_log = _info_dict(src)
        except AttributeError:
            pass
        count = int(_net(scan, "ISpectrum.CentroidCount") or 0)
        centroids: list[Centroid] = []
        for c in _net(scan, "ISpectrum.Centroids") or []:
            charge = _net(c, "ICentroid.Charge")
            centroids.append(
                Centroid(
                    mz=float(_net(c, "IMassIntensity.Mz")),
                    intensity=float(_net(c, "IMassIntensity.Intensity")),
                    charge=None if charge is None else int(charge),
                )
            )
        centroids.sort(key=lambda c: c.intensity, reverse=True)
        return ScanRecord(
            received_at=received,
            header=header,
            trailer=trailer,
            centroid_count=count or len(centroids),
            centroids=centroids[:MAX_CENTROIDS_KEPT],
            status_log=status_log,
        )

    # -------------------------------------------------------------------- status

    def identify(self) -> dict[str, str]:
        info = {"manufacturer": "Thermo Fisher Scientific", "backend": "pythonnet (IAPI)"}
        info["family"] = self.family
        try:
            info["model"] = str(_net(self._instrument, "IInstrumentAccess.InstrumentName"))
            info["instrument_id"] = str(_net(self._instrument, "IInstrumentAccess.InstrumentId"))
            info["detector_class"] = str(_net(self._scan_container, "IMsScanContainer.DetectorClass"))
        except Exception as exc:  # pragma: no cover - hardware only
            info["error"] = str(exc)
        return info

    def _api_license(self) -> bool | None:
        if self.family != "exploris":
            return None  # Tribrid/Exactive enforce the licence in the service; no query in the API
        try:
            licences = _net(self._instrument, "IExplorisInstrumentAccess.Licenses")
            for lic in licences:
                features = [str(f).lower() for f in _net(lic, "IExplorisLicenseInfo.Features")]
                if "api" in features:
                    return True
            return False
        except Exception:  # pragma: no cover - hardware only
            return None

    def _readback(self, name: str) -> dict[str, Any]:
        values = _net(self._control, "IControl.InstrumentValues")
        node = self._readbacks.get(name)
        if node is None:
            node = _net(values, "IInstrumentValues.Get")(name)
            self._readbacks[name] = node  # Get() registers the node; content follows shortly
        content = _net(node, "IReadback.Content") if node is not None else None
        if content is None:
            return {"value": None, "unit": None, "status": "no content yet"}
        return {
            "value": _net(content, "IContent.Content"),
            "unit": _net(content, "IContent.Unit"),
            "status": str(_net(content, "IContent.Status")),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = _net(self._acquisition, "IAcquisition.State")
            out: dict[str, Any] = {
                "service_connected": self._service_connected(),
                "instrument_connected": bool(_net(self._instrument, "IInstrumentAccess.Connected")),
                "instrument_name": str(_net(self._instrument, "IInstrumentAccess.InstrumentName")),
                "instrument_id": int(_net(self._instrument, "IInstrumentAccess.InstrumentId")),
                "system_mode": str(_net(state, "IState.SystemMode")) if state is not None else None,
                "system_state": str(_net(state, "IState.SystemState")) if state is not None else None,
                "can_pause": bool(_net(self._acquisition, "IAcquisition.CanPause")),
                "can_resume": bool(_net(self._acquisition, "IAcquisition.CanResume")),
                "api_license": self._api_license(),
            }
            try:
                values = _net(self._control, "IControl.InstrumentValues")
                out["readback_names"] = sorted(str(n) for n in _net(values, "IInstrumentValues.ValueNames"))
            except Exception:
                out["readback_names"] = []
            readbacks: dict[str, Any] = {}
            for name in self.readback_names:
                try:
                    readbacks[name] = self._readback(name)
                except Exception as exc:
                    readbacks[name] = {"value": None, "unit": None, "status": f"error: {exc}"}
            out["readbacks"] = readbacks
            return out

    def possible_parameters(self) -> list[ParameterDescription]:
        scans = self._scans_iface()
        out = []
        for p in _net(scans, "IScans.PossibleParameters") or []:
            out.append(
                ParameterDescription(
                    name=str(_net(p, "IParameterDescription.Name")),
                    selection=str(_net(p, "IParameterDescription.Selection") or ""),
                    default_value=str(_net(p, "IParameterDescription.DefaultValue") or ""),
                    help=str(_net(p, "IParameterDescription.Help") or ""),
                )
            )
        return out

    # --------------------------------------------------------------- acquisition

    def start_acquisition(
        self,
        mode: AcquisitionMode,
        *,
        duration_s: float | None,
        scan_count: int | None,
        raw_file_path: str | None,
        sample_name: str | None,
        comment: str | None,
    ) -> None:
        acq = self._acquisition
        try:
            if mode == "duration":
                timespan = _bcl("System.TimeSpan")
                workflow = _net(acq, "IAcquisition.CreateAcquisitionLimitedByDuration")(
                    timespan.FromSeconds(float(duration_s or 0))
                )
            elif mode == "scan_count":
                workflow = _net(acq, "IAcquisition.CreateAcquisitionLimitedByCount")(int(scan_count or 0))
            else:
                workflow = _net(acq, "IAcquisition.CreatePermanentAcquisition")()
            if raw_file_path:
                _net_set(workflow, "IAcquisitionWorkflow.RawFileName", raw_file_path)
            if sample_name:
                _net_set(workflow, "IAcquisitionWorkflow.SampleName", sample_name)
            if comment:
                _net_set(workflow, "IAcquisitionWorkflow.Comment", comment)
            _net(acq, "IAcquisition.StartAcquisition")(workflow)
        except Exception as exc:
            raise _translate(exc, "start the acquisition") from exc

    def pause_acquisition(self) -> None:
        if not _net(self._acquisition, "IAcquisition.CanPause"):
            raise InstrumentProtocolError(
                "The instrument reports that the current operation cannot be paused."
            )
        try:
            _net(self._acquisition, "IAcquisition.Pause")()
        except Exception as exc:
            raise _translate(exc, "pause the acquisition") from exc

    def resume_acquisition(self) -> None:
        if not _net(self._acquisition, "IAcquisition.CanResume"):
            raise InstrumentProtocolError("The instrument reports that there is nothing to resume.")
        try:
            _net(self._acquisition, "IAcquisition.Resume")()
        except Exception as exc:
            raise _translate(exc, "resume the acquisition") from exc

    def cancel_acquisition(self) -> None:
        try:
            _net(self._acquisition, "IAcquisition.CancelAcquisition")()
        except Exception as exc:
            raise _translate(exc, "cancel the acquisition") from exc

    def set_standby(self) -> None:
        acq = self._acquisition
        try:
            _net(acq, "IAcquisition.SetMode")(_net(acq, "IAcquisition.CreateStandbyMode")())
        except Exception as exc:
            raise _translate(exc, "switch the instrument to Standby") from exc

    # ------------------------------------------------------------------- scans

    @staticmethod
    def _fill(definition: Any, values: dict[str, str], running_number: int) -> None:
        table = _net(definition, "IScanDefinition.Values")
        for key, value in values.items():
            table[key] = value
        _net_set(definition, "IScanDefinition.RunningNumber", running_number)

    def set_custom_scan(
        self, values: dict[str, str], *, running_number: int, single_processing_delay_s: float
    ) -> bool:
        scans = self._scans_iface()
        try:
            scan = _net(scans, "IScans.CreateCustomScan")()
            self._fill(scan, values, running_number)
            _net_set(scan, "ICustomScan.SingleProcessingDelay", float(single_processing_delay_s))
            return bool(_net(scans, "IScans.SetCustomScan")(scan))
        except Exception as exc:
            raise _translate(exc, "place the custom scan") from exc

    def cancel_custom_scan(self) -> bool:
        try:
            return bool(_net(self._scans_iface(), "IScans.CancelCustomScan")())
        except InstrumentError:
            raise
        except Exception as exc:
            raise _translate(exc, "cancel custom scans") from exc

    def set_repeating_scan(self, values: dict[str, str], *, running_number: int) -> bool:
        scans = self._scans_iface()
        try:
            scan = _net(scans, "IScans.CreateRepeatingScan")()
            self._fill(scan, values, running_number)
            return bool(_net(scans, "IScans.SetRepetitionScan")(scan))
        except Exception as exc:
            raise _translate(exc, "set the repeating scan") from exc

    def cancel_repeating_scan(self) -> bool:
        try:
            return bool(_net(self._scans_iface(), "IScans.CancelRepetition")())
        except InstrumentError:
            raise
        except Exception as exc:
            raise _translate(exc, "cancel the repeating scan") from exc

    def close(self) -> None:
        if self._scan_container is not None and self._handler is not None:
            try:
                _net_subscribe(
                    self._scan_container, "IMsScanContainer.MsScanArrived", self._handler, add=False
                )
            except Exception:  # pragma: no cover - best effort
                pass
        if self._scans is not None:
            _dispose(self._scans)  # releases the IScans lock (IControl.GetScans docs)
        if self._container is not None:
            _dispose(self._container)
        self._scans = self._container = self._instrument = None
