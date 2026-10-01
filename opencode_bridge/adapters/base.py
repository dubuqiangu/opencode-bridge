"""Lane B — adapter ABC and registry (CONTRACT.md §2.1 / §2.4)."""

from __future__ import annotations

import abc
import logging
import threading
from typing import Dict, Type

from ..hooks import Hooks, MsgHandle, Outbound

logger = logging.getLogger("opencode_bridge.adapters.base")

__all__ = ["Adapter", "AdapterError", "build", "register"]


class AdapterError(Exception):
    """Raised for adapter configuration / registration problems."""


# Registry populated by the individual adapter modules via ``register``.
_REGISTRY: Dict[str, Type["Adapter"]] = {}


def register(name: str):
    """Class decorator: add ``cls`` to the build registry under ``name``."""

    def deco(cls: Type["Adapter"]) -> Type["Adapter"]:
        _REGISTRY[name] = cls
        return cls

    return deco


class Adapter(abc.ABC):
    """Base class for messaging platform adapters.

    Lifecycle: ``start()`` spawns a poller thread (non-blocking, never raises
    to the caller); ``stop()`` sets the stop flag and joins the thread with a
    5 second timeout.
    """

    name: str = ""

    def __init__(self, config: dict, hooks: Hooks) -> None:
        self.config: dict = dict(config or {})
        self.hooks = hooks
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """Start the adapter (non-blocking). Must not raise to the caller."""

    def stop(self) -> None:
        """Request the polling thread to stop and join it (timeout 5s)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- messaging -----------------------------------------------------
    @abc.abstractmethod
    def send(self, out: Outbound) -> MsgHandle | None:
        """Send one message; return a handle. Failure -> log, return None."""

    @abc.abstractmethod
    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a previously sent message. Failure -> log, return False."""

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge an inline-keyboard callback query (optional)."""
        return None


def build(name: str, config: dict, hooks: Hooks) -> Adapter:
    """Registry lookup: ``telegram`` / ``slack`` / ``discord``.

    Unknown ``name`` raises :class:`KeyError`. Adapter modules are imported
    lazily on first use so that importing this module alone stays free of
    circular imports.
    """
    key = str(name)
    if key not in _REGISTRY:
        # Deferred import: the adapter modules register themselves on import.
        from . import discord, slack, telegram  # noqa: F401  (side effect)

    if key not in _REGISTRY:
        raise KeyError(f"unknown adapter: {name!r}")
    try:
        cls = _REGISTRY[key]
        return cls(config, hooks)
    except KeyError:
        raise
    except Exception as exc:  # configuration errors -> AdapterError
        raise AdapterError(f"failed to build adapter {name!r}: {exc}") from exc
