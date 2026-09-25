"""Test helpers for LabMCP servers.

Typical server test::

    from labmcp.testing import simulated_client
    from labmcp_mettler_toledo.server import server

    async def test_read_weight():
        async with simulated_client(server) as client:
            result = await client.call_tool("read_weight", {})
            assert result.data["unit"] == "g"
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Client

from labmcp.audit import AuditLog
from labmcp.server import InstrumentServer


@asynccontextmanager
async def simulated_client(server: InstrumentServer[Any], **settings: Any) -> AsyncIterator[Client]:
    """Configure ``server`` in simulate mode and yield an in-memory FastMCP client.

    Extra keyword arguments are passed to :meth:`InstrumentServer.configure`
    (e.g. ``read_only=True`` or ``limits={"max_temperature_c": 50}``).
    """
    settings.setdefault("simulate", True)
    settings.setdefault("read_only", False)
    settings.setdefault("options", {})
    server.configure(**settings)
    if "audit_log" not in settings:
        server.audit = AuditLog()  # each test starts with an empty command log
    try:
        async with Client(server.mcp) as client:
            yield client
    finally:
        server.disconnect()


async def tool_names(client: Client) -> set[str]:
    return {tool.name for tool in await client.list_tools()}
