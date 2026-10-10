"""续行缓冲（``..`` / ``!!``）的落盘快照 —— G2 崩溃窗口的持久层。

``ConversationMerger._held`` 原先是纯内存：用户被告知「已收到、在等续行」的那几行，
在进程崩溃后**静默消失**——写前收件箱救不了它（HELD 早退发生在落盘之前），恢复层
也找不到它（盘上没有行）。本模块把「还在等续行」这个**状态**整份快照进
``held-buffer.json``，启动时由
:meth:`opencode_bridge.inbound_gateway.InboundGateway._recover_held_buffer`
重灌回合并器（恢复语义 = 重灌，不是立即投递——半句话绝不当完整消息发，理由见该方法
docstring）。

**为什么是整份快照而不是事件流**：缓冲的正确状态是「最后写入的那份」，不是
「历次变更的并集」——把已投递的并集重灌回去等于替用户重发。所以每次变更都整份
覆盖写，且**空快照也照写**（崩溃窗口里的旧档必须被显式清掉，否则下次启动会把已经
投递过的内容重灌一遍）。

**损坏处置（与收件箱 ``outcome_unknown`` 同一条纪律：宁可保留旧档）**：读到
非法 JSON 或形状不对时打一条 WARNING、返回空 dict，⛔ **不清掉盘上那份**——损坏
可能只是「写了一半就被杀」，清掉等于把一次可挽救的丢失变成确定丢失；下一次成功的
快照写会自然覆盖它。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = ["HELD_BUFFER_FILE_NAME", "HeldBufferStore"]

logger = logging.getLogger("opencode_bridge.held_buffer_store")

#: 快照文件名。与 ``state.json`` / ``inbox.db`` 同一目录（路径由
#: :func:`opencode_bridge.__main__._held_buffer_path` 拼接），⛔ 与任何现有
#: 状态文件**不共用键空间**（两份状态文件纪律）。
HELD_BUFFER_FILE_NAME = "held-buffer.json"


class HeldBufferStore:
    """``held-buffer.json`` 的读写。自身无状态：每次调用都走一次磁盘。"""

    def __init__(self, path: str) -> None:
        self._path = path

    @property
    def path(self) -> str:
        """快照文件的完整路径（告警与启动日志都点名它）。"""
        return self._path

    def read(self) -> dict[str, str]:
        """读快照：conversation_id -> 还在等续行的正文。缺文件 = ``{}``（首启）。

        ⚠️ 损坏（读失败 / 非法 JSON / 形状不对）⇒ WARNING + ``{}``，盘上那份
        **原样保留**——理由见模块 docstring 的损坏处置。
        """
        try:
            raw_text = Path(self._path).read_text(encoding="utf-8")
        except FileNotFoundError:
            # 首次启动：还没有快照文件是正常态，不打日志。
            return {}
        except OSError as exc:
            logger.warning(
                "held buffer: cannot read %s (%s); treating the held buffer"
                " as empty",
                self._path, exc,
            )
            return {}
        try:
            parsed = json.loads(raw_text)
        except ValueError:
            logger.warning(
                "held buffer: %s is not valid JSON; treating the held buffer as"
                " empty and keeping the file as-is until the next snapshot"
                " overwrites it",
                self._path,
            )
            return {}
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parsed.items()
        ):
            logger.warning(
                "held buffer: %s has the wrong shape (want dict[str, str]);"
                " treating the held buffer as empty and keeping the file as-is",
                self._path,
            )
            return {}
        return dict(parsed)

    def write(self, snapshot: Mapping[str, str]) -> None:
        """整份覆盖写快照。原子：同目录暂存文件 + :func:`os.replace`。

        ⚠️ 写失败**上抛**——「要不要打断入站」只有调用方知道，本模块不替它定
        （调用方 :meth:`InboundGateway._persist_held_snapshot` 只记 WARNING、
        绝不上抛）。
        ⚠️ 不做 ``fsync``：G2 修的是**进程**崩溃（页缓存在进程死后仍在）；断电
        语义与 ``state.json`` 保持一致，不在这里单方面加厚。
        """
        payload = json.dumps(dict(snapshot), ensure_ascii=False)
        target = Path(self._path)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=target.name + ".", suffix=".staging", delete=False,
        ) as staging_file:
            staging_file.write(payload)
            staging_path = staging_file.name
        os.replace(staging_path, target)
