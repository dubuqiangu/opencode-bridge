"""Persistent conversation <-> OpenCode session mapping (Lane A).

The store keeps a small JSON document on disk::

    {"sessions": {"<conversation_id>": "<session_id>", ...},
     "meta": {"<conversation_id>": {"<key>": <value>, ...}, ...},
     "schema_version": 2}

Writes are atomic: a temporary file is created next to the target with
``tempfile.mkstemp`` and moved into place with ``os.replace``, so a crash can
never leave a half-written state file.  All public methods are guarded by an
``RLock`` and safe to call from multiple threads.

键迁移（A2b）
============
统一 ``platform:local_id`` 之后，历史上五个平台用的前缀是**别名**而不是平台键
（``identity.LEGACY_PREFIXES``）：

===================  =====================  ==========================
legacy 前缀           指向                    用它的平台
===================  =====================  ==========================
``chat:``            ``telegram``           telegram
``room:``            ``matrix``             matrix
``channel:``         **歧义（值为 None）**   slack / discord / mattermost
===================  =====================  ==========================

切前缀会改变键的**字符串格式**，而已落盘的 ``state.json`` 里的旧键会全部变成孤儿 ——
用户一次性丢失会话映射，而且**不报错**（只表现为"agent 突然记错上下文"）。
所以这不是格式美化，是**数据迁移**：本模块在**加载时**把旧键重写成新键。

歧义 ``channel:`` 键怎么办（本模块最重要的一个决定）
--------------------------------------------------
:func:`identity.normalize` 对歧义前缀且没有 ``platform_hint`` 时**抛**
:class:`~opencode_bridge.identity.AmbiguousConversationId`，而 ``StateStore`` 拿到的
只是一个**不透明字符串**，它无从判断那是 slack、discord 还是 mattermost。

策略：**只迁移能无歧义判定的键**（``chat:`` → ``telegram:``、``room:`` →
``matrix:``），歧义的 ``channel:`` 键**原样保留**。

理由：

1. **猜错比不迁更糟**。三家任选其一都会把一部分用户的会话映射指向**别人的会话**，
   而症状是"agent 记错了上下文"，不报错、难复现、且会污染已经建立的会话。
2. **保留的代价很小**。``channel:`` 键继续走各适配器现有的旧路径 —— 功能完全
   不受影响，缺的只是"键格式统一"这一个属性。
3. **不许启发式**。本模块**绝不**用 local id 的形状（Slack 以 ``C``/``D`` 开头、
   Discord 纯数字、Mattermost 26 位 base32）、"哪个适配器已挂载"之类的巧合去猜。
   :meth:`identity.normalize` 在缺 ``platform_hint`` 时抛错，本模块只做**捕获**，
   绝不给 ``platform_hint`` —— 那等于伪造一条自己都不知道真假的线索。

什么时候做（``migrate_keys`` 开关）
----------------------------------
迁移**默认关闭**，由构造参数显式开启::

    StateStore(path, migrate_keys=True)

原因：**本模块的默认值必须保持关闭**，打开与否是调用方的决定。

发版顺序必须是**同一个变更里**同时（a）给适配器切前缀、（b）打开本开关、
（c）改写那几条编码"迁移前键会成孤儿"的用例 —— 只做一半就会让仓库停在
"半迁移"状态，而那正是"agent 记错上下文"的温床。

**当前进度（2026-10-04，分两步走）**：

- **第一步已完成**：``telegram``（``chat:`` -> ``telegram:``）与
  ``matrix``（``room:`` -> ``matrix:``）已切前缀，且 ``__main__`` 在同一个
  commit 里传了 ``migrate_keys=True``。存量 ``chat:`` / ``room:`` 键会在加载时
  被重写，会话映射不丢。``__main__`` 打开开关而**本模块默认仍关闭** ——
  这样库本身不替调用方做决定，单测也因此能覆盖关闭态。
- **第二步未做**：``slack`` / ``discord`` / ``mattermost`` 共用歧义前缀
  ``channel:``，``normalize()``缺 ``platform_hint`` 无法归一，而线索**不在
  ``state.json`` 里**。这一步需要单独的设计决定（按已挂载适配器逐个尝试归一），
  所以本模块至今**不给任何 ``platform_hint``**，``channel:`` 键原样保留
  （见 :attr:`MigrationReport.ambiguous_kept`）。

对应用例已改名为 ``test_legacy_chat_key_survives_the_prefix_cutover_through_state_migration``
与 ``test_legacy_room_key_survives_the_prefix_cutover_through_state_migration``
（``tests/test_conversation_id_cutover.py`` 里另有``channel:`` 存活的覆盖）。

迁移期的兼容读（``_legacy_alias``）
----------------------------------
即便开了开关，在适配器还没切完前缀的那段窗口里，适配器**查询用的**仍是旧键。
:func:`_lookup` 因此做一次**无歧义别名回退**：先按精确键查，查不到再按
:meth:`identity.normalize` 归一后的键查一次。它和迁移用同一个判定，所以
**不会猜**（``channel:`` 永远没有别名可回退）。适配器全部切完之后这段回退恒不
命中，可以删掉。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from . import identity

__all__ = [
    "StateStore",
    "MigrationReport",
    "STATE_SCHEMA_VERSION",
    "migrate_document",
]

logger = logging.getLogger("opencode_bridge.state")

#: 键迁移完成后的 schema 版本。写进 ``state.json`` 有两个作用：可观测 + 短路扫描。
#: ⚠️ **它不代表所有键都已是新格式** —— 歧义的 ``channel:`` 键是**故意**保留的。
#: 真正的幂等保证来自内容判定（没有可迁的键 ⇒ 不备份、不写盘），不依赖这个标记。
STATE_SCHEMA_VERSION: int = 2

_NEW = "new"
_MIGRATE = "migrate"
_AMBIGUOUS = "ambiguous"
_INVALID = "invalid"


@dataclass(frozen=True)
class MigrationReport:
    """一次加载期键迁移的**只读结果**。

    存在的理由：用例应该断言**内部状态**（条数、备份路径），而不是去解析日志文本。
    """

    #: 参与判定的键数（``sessions`` 与 ``meta`` 的并集）。
    scanned: int = 0
    #: 实际改写的键数。
    migrated: int = 0
    #: 因前缀歧义而**原样保留**的键数（``channel:``）。
    ambiguous_kept: int = 0
    #: 无法识别、**原样保留**的键数。
    invalid_kept: int = 0
    #: 因目标键已被占用而放弃迁移的键数（两条都保留，绝不覆盖）。
    collisions: int = 0
    #: 迁移前的备份路径；没发生迁移或备份失败时为 ``None``。
    backup_path: Optional[str] = None
    #: 落盘时写入的 schema 版本。
    schema_version: int = 0

    @property
    def changed(self) -> bool:
        """是否真的改写了键（= 是否需要备份与落盘）。"""
        return self.migrated > 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} migrated={self.migrated} "
            f"ambiguous_kept={self.ambiguous_kept} "
            f"invalid_kept={self.invalid_kept} collisions={self.collisions}"
        )


def _classify_key(key: object) -> tuple[str, Optional[str]]:
    """判定一个键属于哪一类，返回 ``(类别, 新键或None)``。

    判定**只**依赖 :meth:`identity.normalize`，且**永远不传** ``platform_hint`` ——
    本模块没有资格知道一个不透明键来自哪个平台。
    """
    if not isinstance(key, str):
        return _INVALID, None
    try:
        new_key = identity.normalize(key)
    except identity.AmbiguousConversationId:
        # 先接子类：歧义与"根本不认识"的处理不同 —— 前者等平台线索，后者没救。
        return _AMBIGUOUS, None
    except identity.InvalidConversationId:
        return _INVALID, None
    return (_NEW, None) if new_key == key else (_MIGRATE, new_key)


def _resolve_collisions(
    candidates: dict[str, str], present: set
) -> tuple[dict[str, str], int]:
    """从候选重写里挑出可以安全执行的，返回 ``(计划, 被放弃的候选数)``。

    放弃而不是合并，理由和歧义前缀同源：**宁可两条都在，也不覆盖任何一条**。
    """
    plan: dict[str, str] = {}
    owner: dict[str, str] = {}  # 新键 -> 旧键
    blocked: set = set()
    for old in sorted(candidates):
        new = candidates[old]
        if new in blocked:
            continue  # 同一目标键已被判为冲突，整组作废
        previous = owner.get(new)
        if previous is not None or (new in present and new not in candidates):
            blocked.add(new)
            if previous is not None:
                plan.pop(previous, None)
                owner.pop(new, None)
            continue
        plan[old] = new
        owner[new] = old
    return plan, len(candidates) - len(plan)


def migrate_document(
    raw: dict
) -> tuple[dict, MigrationReport]:
    """纯函数：把 ``state.json`` 文档里的旧键改写成新键。

    不碰磁盘、不记日志、不看平台 —— 于是可以单独测。不认识 / 歧义的键**原样保留**，
    返回的条目数与输入严格一致（改的只是键名）。
    """
    sessions_in = raw.get("sessions")
    meta_in = raw.get("meta")
    sessions = dict(sessions_in) if isinstance(sessions_in, dict) else {}
    meta = dict(meta_in) if isinstance(meta_in, dict) else {}
    before = (len(sessions), len(meta))

    present = set(sessions) | set(meta)
    candidates: dict = {}
    ambiguous = 0
    invalid = 0
    for key in sorted(present, key=str):  # 排序 ⇒ 冲突处理可复现
        verdict, new_key = _classify_key(key)
        if verdict == _MIGRATE:
            candidates[key] = new_key
        elif verdict == _AMBIGUOUS:
            ambiguous += 1
        elif verdict == _INVALID:
            invalid += 1

    plan, collisions = _resolve_collisions(candidates, present)

    out_sessions = dict(sessions)
    out_meta = dict(meta)
    for old, new in plan.items():
        if old in out_sessions:
            out_sessions[new] = out_sessions.pop(old)
        if old in out_meta:
            out_meta[new] = out_meta.pop(old)

    if (len(out_sessions), len(out_meta)) != before:
        # 迁移只许改键名、不许增删条目。将来谁把逻辑改坏了，这里当场暴露而不是
        # 悄悄少一条映射（症状同样是"agent 忘了一部分对话"，极难查）。
        logger.error(
            "key migration broke the entry-count invariant (%d/%d -> %d/%d); "
            "keeping the document unchanged",
            before[0], before[1], len(out_sessions), len(out_meta),
        )
        out_sessions, out_meta = sessions, meta
        collisions += len(candidates)
        plan = {}

    document = dict(raw)
    document["sessions"] = out_sessions
    document["meta"] = out_meta
    if plan:
        document["schema_version"] = STATE_SCHEMA_VERSION
    report = MigrationReport(
        scanned=len(present),
        migrated=len(plan),
        ambiguous_kept=ambiguous,
        invalid_kept=invalid,
        collisions=collisions,
        schema_version=STATE_SCHEMA_VERSION if plan else 0,
    )
    return document, report


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


def _legacy_alias(key: object) -> Optional[str]:
    """旧键 → 新键；**只对无歧义别名**成立（``chat:`` / ``room:``）。

    迁移期适配器还在用旧键查询，没有这段回退就会在"键已迁、适配器未切"的窗口里
    查不到映射 —— 用户看到的就是"agent 突然忘了一部分对话"。
    """
    verdict, new_key = _classify_key(key)
    return new_key if verdict == _MIGRATE else None


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
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
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