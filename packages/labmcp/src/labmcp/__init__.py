"""LabMCP: shared foundations for Model Context Protocol servers that control lab instruments."""

from labmcp.audit import AuditLog
from labmcp.errors import (
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentTimeout,
    ReadOnlyModeError,
    SafetyLimitError,
)
from labmcp.safety import Limit, SafetyLimits
from labmcp.server import CONTROL, HAZARD, READ, SAFETY, ConnectContext, InstrumentServer, Settings
from labmcp.transports import (
    ByteSimulator,
    LineSimulator,
    SimulatedTransport,
    Transport,
    open_transport,
    parse_address,
)

__all__ = [
    "CONTROL",
    "HAZARD",
    "READ",
    "SAFETY",
    "AuditLog",
    "ByteSimulator",
    "ConnectContext",
    "InstrumentConnectionError",
    "InstrumentError",
    "InstrumentProtocolError",
    "InstrumentServer",
    "InstrumentTimeout",
    "Limit",
    "LineSimulator",
    "ReadOnlyModeError",
    "SafetyLimitError",
    "SafetyLimits",
    "Settings",
    "SimulatedTransport",
    "Transport",
    "open_transport",
    "parse_address",
]
