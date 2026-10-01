"""Persistent conversation <-> OpenCode session mapping (Lane A).

The store keeps a small JSON document on disk::

    {"sessions": {"<conversation_id>": "<session_id>", ...},
     "meta": {"<conversation_id>": {"<key>": <value>, ...}, ...}}

Writes are atomic: a temporary file is created next to the target with
``tempfile.mkstemp`` and moved into place with ``os.replace``, so a crash can
never leave a half-written state file.  All public methods are guarded by an
``RLock`` and safe to call from multiple threads.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any

__all__ = ["StateStore"]

logger = logging.getLogger("opencode_bridge.state")


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = os.fspath(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"sessions": {}, "meta": {}}
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as exc:
            logger.warning("cannot read state file %s: %s", self._path, exc)
            return
        except ValueError as exc:
            logger.warning("invalid JSON in state file %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            logger.warning("state file %s is not a JSON object", self._path)
            return
        sessions = raw.get("sessions")
        meta = raw.get("meta")
        self._data = {
            "sessions": dict(sessions) if isinstance(sessions, dict) else {},
            "meta": dict(meta) if isinstance(meta, dict) else {},
        }

    def flush(self) -> None:
        """Atomically write the current state to disk."""
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".state-", suffix=".tmp", dir=directory
            )
        except OSError as exc:
            logger.warning("cannot create temp state file in %s: %s", directory, exc)
            raise
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def get_session(self, conversation_id: str) -> str | None:
        with self._lock:
            value = self._data["sessions"].get(conversation_id)
            return value if isinstance(value, str) else None

    def set_session(self, conversation_id: str, session_id: str) -> None:
        with self._lock:
            self._data["sessions"][conversation_id] = session_id
            self._write_locked()

    def drop_session(self, conversation_id: str) -> None:
        with self._lock:
            if conversation_id in self._data["sessions"]:
                del self._data["sessions"][conversation_id]
            meta = self._data["meta"].get(conversation_id)
            if isinstance(meta, dict) and meta:
                # keep meta: it may hold unrelated per-conversation data
                pass
            self._write_locked()

    def all_sessions(self) -> dict[str, str]:
        with self._lock:
            return {
                str(k): str(v)
                for k, v in self._data["sessions"].items()
                if isinstance(v, str)
            }

    # ------------------------------------------------------------------
    # metadata
    # ------------------------------------------------------------------
    def set_meta(self, conversation_id: str, key: str, value: Any) -> None:
        with self._lock:
            meta = self._data["meta"].setdefault(conversation_id, {})
            if not isinstance(meta, dict):
                meta = {}
                self._data["meta"][conversation_id] = meta
            meta[key] = value
            self._write_locked()

    def get_meta(self, conversation_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            meta = self._data["meta"].get(conversation_id)
            if isinstance(meta, dict) and key in meta:
                return meta[key]
            return default
