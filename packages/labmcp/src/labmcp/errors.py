"""Exception hierarchy shared by every LabMCP server.

All errors subclass FastMCP's ``ToolError`` so their messages always reach the
MCP client (and therefore the model), even when a server masks internal error
details. Messages should tell the scientist what went wrong *and* what to do
about it.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class InstrumentError(ToolError):
    """Base class for all instrument-related failures."""


class InstrumentConnectionError(InstrumentError):
    """The instrument could not be reached or the link dropped."""


class InstrumentTimeout(InstrumentError):
    """The instrument did not answer within the configured timeout."""


class InstrumentProtocolError(InstrumentError):
    """The instrument replied with something we could not parse or an error code."""


class SafetyLimitError(InstrumentError):
    """A requested value violates a configured safety limit. Nothing was sent."""


class ReadOnlyModeError(InstrumentError):
    """A state-changing operation was attempted while the server is read-only."""
