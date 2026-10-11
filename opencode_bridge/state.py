"""Persistent conversation <-> OpenCode session mapping (Lane A).

The store keeps a small JSON document on disk::

    {"sessions": {"<conversation_id>": "<session_id>", ...},
     "meta": {"<conversation_id>": {"<key>": <value>, ...}, ...},
     "schema_version": 2}

Writes are atomic: a temporary file is created next to the target with
``tempfile.mkstemp`` and moved into place with ``os.replace``, so a crash can
never leave a half-written state file.  All public methods are guarded by an
``RLock`` and safe to call from multiple threads.

Key migration (A2b) — the design notes and the pure rewrite logic now live
in :mod:`opencode_bridge.state_migration`; ``STATE_SCHEMA_VERSION``,
``MigrationReport``, ``migrate_document``, ``_classify_key`` and
``_legacy_alias`` are re-exported below so existing import paths do not
change.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from typing import Any, Optional

from .redaction import redact_state_values
from .state_migration import (
    STATE_SCHEMA_VERSION,
    MigrationReport,
    _classify_key,
    _legacy_alias,
    migrate_document,
)


__all__ = [
    "StateStore",
    "MigrationReport",
    "STATE_SCHEMA_VERSION",
    "migrate_document",
]

logger = logging.getLogger("opencode_bridge.state")


def _backup_file(path: str) -> str:
    """把 ``path`` 另存为带时间戳的备份，返回备份路径。

    只在真正要改写键**之前**调用，且失败会向上抛 —— 没有备份就没有回滚路径，
    那就不该动用户的文件。
    """
    base = f"{path}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    target = base
    counter = 1
    while os.path.exists(target):  # 同一秒内重复迁移时不要覆盖已有备份
        target = f"{base}.{counter}"
        counter += 1
    shutil.copy2(path, target)
    return target


def _lookup(mapping: dict, key: object) -> Any:
    """精确键优先，miss 时按**无歧义**别名回退一次。"""
    value = mapping.get(key)
    if value is None:
        alias = _legacy_alias(key)
        if alias is not None:
            value = mapping.get(alias)
    return value


class StateStore:
    def __init__(self, path: str, *, migrate_keys: bool = False) -> None:
        self._path = os.fspath(path)
        self._lock = threading.RLock()
        self._migrate_enabled = bool(migrate_keys)
        self._data: dict = {"sessions": {}, "meta": {}}
        #: 最近一次加载的迁移结果（见 :class:`MigrationReport`）。
        self.last_migration = MigrationReport()
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
            # ⚠️ 损坏文件**绝不覆盖**：不迁移、不重写、只告警，让用户能自己抢救。
            # （之后的显式 ``set_session`` 仍按老行为覆写它 —— 那是调用方的决定，
            # 与"迁移不许破坏现场"不是一回事。）
            logger.error(
                "state file %s is not valid JSON (%s); leaving it untouched so it "
                "can be recovered by hand",
                self._path, exc,
            )
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
        self._migrate_loaded(raw)

    def _migrate_loaded(self, raw: dict) -> None:
        """加载后按 :func:`identity.normalize` 重写旧键并原子落盘。"""
        if not self._migrate_enabled:
            logger.info(
                "state key migration disabled; keys in %s kept verbatim "
                "(pass migrate_keys=True to rewrite legacy keys)",
                self._path,
            )
            return

        scanned = len(set(self._data["sessions"]) | set(self._data["meta"]))
        version = raw.get("schema_version")
        if (isinstance(version, int) and not isinstance(version, bool)
                and version >= STATE_SCHEMA_VERSION):
            # 版本门控：跳过扫描。即便标记丢了，下面的内容判定也会得出"无可迁键"
            # 而不写盘 —— 两道保险，幂等不依赖任何单一机制。
            self.last_migration = MigrationReport(scanned=scanned,
                                                 schema_version=version)
            logger.debug(
                "state file already at schema %d; key scan skipped (%d keys)",
                version, scanned,
            )
            return

        document, report = migrate_document(raw)
        if report.collisions:
            # 必须在"零迁移"早退**之前**告警：这一轮可能只撞了冲突、什么都没迁，
            # 但那条旧键确实被留在了原地，用户需要知道。
            logger.warning(
                "state: %d key(s) left in the legacy form because the migrated key "
                "already exists (both copies kept, nothing overwritten)",
                report.collisions,
            )
        if not report.changed:
            self.last_migration = report
            logger.info(
                "state: %s; nothing to migrate, file left untouched",
                report.summary(),
            )
            return

        # 顺序很重要：内存先切到新键，落盘**在备份之后**、用原子替换完成。
        # 万一落盘失败，磁盘上仍是完整的旧文件（外加备份），而内存里是新键 ——
        # 下一次写入会把新键持久化，自动收敛。
        self._data = {
            "sessions": document["sessions"],
            "meta": document["meta"],
            "schema_version": STATE_SCHEMA_VERSION,
        }
        try:
            backup = _backup_file(self._path)
        except OSError as exc:
            self.last_migration = replace(report, backup_path=None)
            logger.error(
                "cannot back up %s (%s); keeping the original file and skipping "
                "the rewrite so you keep a rollback path",
                self._path, exc,
            )
            return
        try:
            self._write_locked()
        except OSError as exc:
            self.last_migration = replace(report, backup_path=backup)
            logger.error(
                "cannot write the migrated state to %s (%s); the original file is "
                "intact and the backup is at %s",
                self._path, exc, backup,
            )
            return

        self.last_migration = replace(report, backup_path=backup)
        logger.info(
            "state: migrated %d legacy key(s) in %s; %s; backup kept at %s",
            report.migrated, self._path, report.summary(), backup,
        )

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
            # ⚠️ **键不动，只脱敏值**（C2）。键是 conversation_id，也就是这份文档的
            # **索引** —— 把它换成掩码文本会让每次查找落空、每条会话静默变成孤儿。
            # 而值上只跑**凭据**一类：``meta`` 的值是要读回来用的（``directory`` /
            # ``stream_cursor``），把手机号 / 邮箱规则套上去会让 ``/cd`` 静默去到
            # 不存在的地方。理由见 :mod:`opencode_bridge.redaction` 模块 docstring。
            # 脱敏结果是**新结构**，``self._data`` 本身不被改动，所以同进程内
            # ``get_meta()`` 读到的仍是原值。
            document = redact_state_values(self._data)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(document, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            # 原子替换：要么旧文件、要么新文件，**不存在**第三个状态。
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
    def get_session(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            value = _lookup(self._data["sessions"], conversation_id)
            return value if isinstance(value, str) else None

    def set_session(self, conversation_id: str, session_id: str) -> None:
        with self._lock:
            self._data["sessions"][conversation_id] = session_id
            self._write_locked()

    def drop_session(self, conversation_id: str) -> None:
        with self._lock:
            if conversation_id in self._data["sessions"]:
                del self._data["sessions"][conversation_id]
            # 同一条会话的别名也一起删，否则"迁移期删除"会留下一个删不掉的残留
            # 条目（调用方传旧键，而落盘的已经是新键）。
            alias = _legacy_alias(conversation_id)
            if alias is not None and alias in self._data["sessions"]:
                del self._data["sessions"][alias]
            meta = self._data["meta"].get(conversation_id)
            if isinstance(meta, dict) and meta:
                # keep meta: it may hold unrelated per-conversation data
                pass
            self._write_locked()

    def all_sessions(self) -> dict:
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
            meta = _lookup(self._data["meta"], conversation_id)
            if isinstance(meta, dict) and key in meta:
                return meta[key]
            return default