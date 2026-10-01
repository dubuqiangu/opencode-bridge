"""opencode-bridge — Lane A (OpenCode side) public symbols.

Only the OpenCode-facing modules are exported here; the adapter layer and
the core live in separate lanes and are intentionally not imported.
"""

from __future__ import annotations

from .config import DEFAULT_CONFIG_NAME, Config
from .opencode_client import (
    DEFAULT_SERVICE_URL,
    Endpoint,
    OpenCodeClient,
    OpenCodeError,
    discover_endpoint,
)
from .state import StateStore

__all__ = [
    "DEFAULT_CONFIG_NAME",
    "DEFAULT_SERVICE_URL",
    "Config",
    "Endpoint",
    "OpenCodeClient",
    "OpenCodeError",
    "StateStore",
    "discover_endpoint",
]
