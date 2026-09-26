"""Simulation backend for the MS worklist server.

The backend is purely file-based (it writes import files; it never talks to an instrument), so
``--simulate`` runs the exact same code against a temporary output folder that is deleted when
the server disconnects.
"""

from __future__ import annotations

import tempfile

from labmcp_ms_worklist.driver import WorklistStore


def simulated_store() -> WorklistStore:
    """A :class:`WorklistStore` bound to a fresh temporary folder (removed on close)."""
    return WorklistStore(tempfile.mkdtemp(prefix="labmcp-ms-worklist-"), simulated=True)
