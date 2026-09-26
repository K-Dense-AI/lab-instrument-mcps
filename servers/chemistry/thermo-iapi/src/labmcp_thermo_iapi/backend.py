"""The backend interface the driver talks to, and the plain-Python records it exchanges.

Two implementations exist:

* :class:`labmcp_thermo_iapi.pythonnet_backend.PythonNetBackend` loads the scientist's own,
  licensed Thermo Fisher Instrument API (IAPI) assemblies through pythonnet on the Windows
  instrument PC.
* :class:`labmcp_thermo_iapi.simulator.FakeOrbitrap` emits synthetic scans for ``--simulate``
  and CI.

Nothing in here imports .NET. Everything that crosses the backend boundary is a plain Python
value, so the driver, the tools and the tests are identical for both backends.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

AcquisitionMode = Literal["duration", "scan_count", "until_stopped"]


@dataclass(frozen=True)
class ParameterDescription:
    """One entry of ``IScans.PossibleParameters`` (``IParameterDescription``).

    ``selection`` follows the grammar documented on ``IParameterDescription.Selection`` in
    lib/API-2.0.xml: ``""`` (no argument), ``"string"`` (free text), ``"num1-num2"`` (integer
    range, inclusive), ``"num1.frac-num2.frac"`` (float range, inclusive) or
    ``"sel1,sel2,..."`` (one of the listed values).
    """

    name: str
    selection: str
    default_value: str = ""
    help: str = ""


@dataclass
class Centroid:
    mz: float
    intensity: float
    charge: int | None = None


@dataclass
class ScanRecord:
    """A scan copied out of ``MsScanEventArgs.GetScan()`` before the .NET object is disposed."""

    received_at: str  # UTC ISO 8601, stamped by the backend when MsScanArrived fired
    header: dict[str, str]
    trailer: dict[str, str]
    centroid_count: int
    centroids: list[Centroid]  # sorted by decreasing intensity, capped by the backend
    status_log: dict[str, str] = field(default_factory=dict)
    sequence: int = 0  # assigned by the driver: arrival order since connect


class OrbitrapBackend(abc.ABC):
    """What the driver needs from an Orbitrap, expressed in IAPI terms.

    Every method maps onto IAPI members listed in :mod:`labmcp_thermo_iapi.iapi_members`
    (see :class:`~labmcp_thermo_iapi.pythonnet_backend.PythonNetBackend` for the mapping).
    Control methods return what the IAPI returns: ``True`` when the command was sent to the
    instrument, ``False`` otherwise.
    """

    #: "simulator" or "pythonnet"
    kind: str = "abstract"

    @abc.abstractmethod
    def open(self, on_scan: Callable[[ScanRecord], None]) -> None:
        """Connect, and call ``on_scan`` for every scan (from a background thread)."""

    @abc.abstractmethod
    def identify(self) -> dict[str, str]: ...

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """Keys: service_connected, instrument_connected, instrument_name, instrument_id,
        system_mode, system_state, can_pause, can_resume, api_license (True/False/None),
        readbacks {name: {value, unit, status}}, readback_names [str]."""

    @abc.abstractmethod
    def possible_parameters(self) -> list[ParameterDescription]: ...

    @abc.abstractmethod
    def start_acquisition(
        self,
        mode: AcquisitionMode,
        *,
        duration_s: float | None,
        scan_count: int | None,
        raw_file_path: str | None,
        sample_name: str | None,
        comment: str | None,
    ) -> None: ...

    @abc.abstractmethod
    def pause_acquisition(self) -> None: ...

    @abc.abstractmethod
    def resume_acquisition(self) -> None: ...

    @abc.abstractmethod
    def cancel_acquisition(self) -> None: ...

    @abc.abstractmethod
    def set_standby(self) -> None: ...

    @abc.abstractmethod
    def set_custom_scan(
        self, values: dict[str, str], *, running_number: int, single_processing_delay_s: float
    ) -> bool: ...

    @abc.abstractmethod
    def cancel_custom_scan(self) -> bool: ...

    @abc.abstractmethod
    def set_repeating_scan(self, values: dict[str, str], *, running_number: int) -> bool: ...

    @abc.abstractmethod
    def cancel_repeating_scan(self) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...
