"""``/api/event`` 订阅状态的**运行期通道**：把看护者那份快照落盘，给独立进程读。

为什么要有这个模块
==================

``fix-289`` 把「SSE 线程永久死亡」修好了（有界退避后重新订阅），``fix-294`` 补上了
``--status`` 里那一段视图 —— ⚠️ 但**中间那一段通道至今不存在**：

* :mod:`opencode_bridge.subscription_supervisor` 只把状态记在**进程内**的一份不可变
  快照里（:meth:`~opencode_bridge.subscription_supervisor.SubscriptionSupervisor.status`）；
* 而 ``--status`` 是**独立进程**（派单核实过：``grep run_status`` 命中 **7** 处，
  **没有一处**在桥进程内）⇒ 它拿得到 :class:`~opencode_bridge.core.BridgeCore` 吗？拿不到。

⇒ 于是「正在重连 / 线程已经死了」在真实运行里**仍然只有一行日志**。
本模块就是那条通道：**桥进程写，独立进程读**。

落盘形态
========

::

    {"recorded_at": <epoch float>,
     "pid": <写这一份的进程>,
     "subscription": {"phase": …, "subscriptions_started": …,
                      "reconnect_attempts": …, "frames_received": …,
                      "last_error": …}}

⚠️ 形态与 :func:`opencode_bridge.health.record_startup_probes` 那份
``{"recorded_at", "platforms"}`` **同形**，因为它们回答的是同一类问题
（**一份记录 = 一个时刻的结论**）⇒ 整份替换是对的，见下面「为什么是另开一份」。

为什么是**另开一份文件**（而不是并进已有那两份）
==============================================

⚠️ 两条理由都是**既有代码已经论证过的**，这里只复述结论并逐条对到本次情形：

1. ⛔ **并进 ``platform-health.json`` 会被整份替换抹掉。** 那一份的语义是
   「盘上这份 = **最近一次启动**的全部结论」（:mod:`opencode_bridge.health` 模块
   docstring），每次启动整份替换 ⇒ 而订阅状态是**运行期**的、用户恰恰在**桥还跑着**
   的时候查它 ⇒ 下一次启动顺手删掉的正是他最需要的那一份。
   ⛔ 而「让订阅状态参与整份替换」要动那条**刻意的不变量**（``failed`` 不改写），
   那是硬约束第 2 条明令不许碰的。
2. ⛔ **并进 ``outbound-failures.json`` 会污染平台键空间。** 那一份的 ``platforms``
   键是**平台键**，读侧 :func:`opencode_bridge.health.outbound_failure_from_record`
   按平台键逐个取、``--status`` 那一段再把「盘上的键」直接列成行 ⇒ 塞一个非平台键
   进去，用户就会在「上次出站失败」那一段看到一行叫 ``subscription`` 的**假平台**。
   ⇒ ⛔ 而把订阅状态**另开一个键**放进同一份文件同样踩这条（那一段的 ``listed``
   是从记录的全部键推出来的）。

⇒ 结论：**第三个平行文件 + 第三段 ``--status``**，与那两份**互不覆盖**
（理由与 :class:`opencode_bridge.health.OutboundFailureRecorder` 的先例逐条对应）。

落盘的节流判据：**相位边沿 + 重连次数**
======================================

⚠️ **本通道最要紧的一条**：:meth:`SubscriptionSupervisor._update` **每收到一帧**就调
一次（``frames_received`` 每帧 +1）⇒ 实测一条 5.7 KB 的回答就是 **3402** 个 delta
⇒ ⛔「回调来了就写」会把它变成每秒几十次 ``mkstemp`` + ``fsync`` + ``os.replace``，
**在排障功能自己的位置上**制造压力。

⇒ 判据是 **(phase, reconnect_attempts) 这一对**，与
:class:`opencode_bridge.health.OutboundFailureRecorder` 那张表同型：

========================  ==========================================================
观测                      写盘?   理由
========================  ==========================================================
帧来了（``frames_received`` +1） 否   只多收了一帧，**没有任何人要据此改主意**
「第一次订阅开始」            是    让 ``--status`` 早早有东西可读
「订阅坏了 → 正在重连」        是    这就是要让人看见的那一次
「重连完了 → 又在收」          是    不写就永远显示"正在重连"，而它其实好了
「线程结束 / 被叫停」          是    两者都要留下最后一句话
========================  ==========================================================

⇒ **上界是「每次订阅尝试 ≤ 2 次写盘」**（坏 + 好），与出站失败那份
「每平台每次失败连击 ≤ 2 次」**同形**。⚠️ 和那份一样，这个界**不是「每进程」**：
一个长跑进程可以有任意多次重连循环，每次各写自己的那几次。

⚠️ **两个字段被刻意排除在判据之外**：
* ``frames_received`` —— 每帧都变，把它算进去就是「回调来了就写」；
* ``last_error`` / ``recorded_at`` —— 它们只在**相位变了**的那几次里变，
  不构成独立触发（否则同一个变化会写两遍）。

⚠️ **pid 复用**（``pid_is_alive`` 的已知取舍）：持有者早退了、pid 却被无关进程占着
时，我们会把一份**过期**记录当成活的。⇒ 这是
:func:`opencode_bridge.__main__._runtime_state` **早就在接受的同一个代价**
（它读锁文件里的 pid 再判活），本模块与它**同向**，不引入新的失效模式。

安全红线
========

⚠️ **落盘内容里绝不许出现凭据片段**：``last_error`` 是
``"<异常类名>: <异常正文>"``，而正文来自 opencode 的 HTTP 响应体 ⇒ 与另两份记录
**同一条**红线，落盘前同样过
:meth:`opencode_bridge.redaction.default_redactor.scrub_persisted_value`。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from .instance_lock import pid_is_alive
from .pairing_cli import write_config_atomically
from .redaction import default_redactor
from .subscription_supervisor import SubscriptionStatus

__all__ = [
    "SUBSCRIPTION_HEALTH_FILE_NAME",
    "SubscriptionHealthRecorder",
    "install_subscription_health_recorder",
    "installed_subscription_health_recorder",
    "read_subscription_status",
]

logger = logging.getLogger("opencode_bridge.subscription_health")

#: 落盘文件名，落在 ``bridge_dir`` 下（目录由
#: :func:`opencode_bridge.__main__._bridge_dir` 给出 —— ⛔ 那套推导只有一份，
#: 本模块不自己再写一遍，否则「状态视图读的」与「启动时写的」会指向两个目录）。
SUBSCRIPTION_HEALTH_FILE_NAME = "subscription-health.json"

#: 记录里那份快照的键。⚠️ 是**子键**而不是顶层散键：外层留给 ``recorded_at`` 与
#: ``pid``，而这份记录的语义将来会扩（那时不必改已有键的位置）。
SUBSCRIPTION_RECORD_KEY = "subscription"

#: 落盘节流判据的那一对字段的名字。⚠️ 写成常量是为了让「读侧」与「写侧」引用
#: **同一份**定义 —— 两处各写一遍字面量就会漂（而漂了之后节流是「写太多」还是
#: 「写太少」，没有任何东西会报警）。
PERSISTED_MARKER_FIELDS = ("phase", "reconnect_attempts")


def _snapshot_document(status: SubscriptionStatus) -> dict:
    """把一份快照变成记录里的那个子文档（**读侧只认这几个键**）。"""
    return {
        "phase": status.phase,
        "subscriptions_started": status.subscriptions_started,
        "reconnect_attempts": status.reconnect_attempts,
        "frames_received": status.frames_received,
        "last_error": status.last_error,
    }


class SubscriptionHealthRecorder:
    """订阅状态的落盘方：**相位 / 重连次数变化时**才写一次。

    ⚠️ 它是 :class:`~opencode_bridge.subscription_supervisor.SubscriptionSupervisor`
    那个 ``on_status_change`` 回调的**实现**，而回调**每帧**都会被调
    ⇒ 「什么时候算值得写」的全部判据都在本类里，⛔ 不在那个状态机里
    （理由见模块 docstring「落盘的节流判据」）。

    ⚠️ **写盘失败绝不影响订阅线程**：每个方法自己兜住异常、记一行 warning、返回
    ``False`` —— 与 :class:`opencode_bridge.health.OutboundFailureRecorder`
    「排障记录绝不该决定桥的生死」同源。

    ⚠️ **整份替换、不加锁**：这一份记录**只有一个键**、语义是「此刻那条线程的状态」
    ⇒ 旧内容没有任何东西需要保住。而
    :func:`opencode_bridge.pairing_cli.write_config_atomically` 用的是
    ``tempfile.mkstemp``（**每次唯一**的临时名）+ ``os.replace`` ⇒ 两个写者并发也
    不会互相踩 ⇒ 不需要 :class:`opencode_bridge.health.OutboundFailureRecorder`
    那把 ``_file_lock``（它要锁是因为自己**先读后写**）。
    """

    def __init__(self, bridge_dir: str) -> None:
        self._bridge_dir = str(bridge_dir or "")
        #: 上一次**真的写下去**的那一对（:data:`PERSISTED_MARKER_FIELDS`）。
        #: ⚠️ 只装写成功的 —— 装进去一个没写成的，下一次状态变化就会**跳过**，
        # 于是那条状态变更永久丢失（与出站失败那份「``_failing`` 只装盘上真记着的」
        # 同一个纪律）。
        self._persisted_marker: Optional[tuple] = None

    # ------------------------------------------------------------------
    # 观测入口
    # ------------------------------------------------------------------
    def note(self, status: SubscriptionStatus) -> bool:
        """记一次订阅状态；**真的写了盘**才返回 ``True``。

        :param status: :meth:`SubscriptionSupervisor.status` 给出的那份快照。
        """
        marker = self._marker_of(status)
        if marker == self._persisted_marker:
            return False
        document = {
            "recorded_at": time.time(),
            "pid": os.getpid(),
            SUBSCRIPTION_RECORD_KEY: _snapshot_document(status),
        }
        path = os.path.join(self._bridge_dir, SUBSCRIPTION_HEALTH_FILE_NAME)
        try:
            write_config_atomically(
                path, default_redactor().scrub_persisted_value(document)
            )
        except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定线程的生死
            # ⚠️ **措辞说「这次观测没落下去」而不只是「写失败」**：没尝试写的时候说
            # 「写入失败」是假的（出站失败那份踩过同一个措辞问题）。
            logger.warning(
                "subscription-health: %s 写不下去（%s: %s）—— 不影响订阅线程",
                path, type(exc).__name__, exc,
            )
            return False
        # ⚠️ 顺序是承重的：**写成功之后**才推进已落盘标记 ⇒ 写失败时下一次状态
        # 变化会**重试**，而这次失败的那份状态不会被当成「已经说过了」。
        self._persisted_marker = marker
        return True

    @staticmethod
    def _marker_of(status: SubscriptionStatus) -> tuple:
        """节流判据那一对（:data:`PERSISTED_MARKER_FIELDS`）。"""
        return tuple(getattr(status, name) for name in PERSISTED_MARKER_FIELDS)


def read_subscription_status(bridge_dir: str) -> Optional[SubscriptionStatus]:
    """读回订阅状态；**没有记录 / 读不出来 / 写下它那个进程已经不在了**都是 ``None``。

    ⚠️ ``None`` 的含义必须是「**没有可用记录**」，而⛔ **不代表订阅正常** ——
    调用方（``--status`` 那一段）要把它显示成「读不到」，而不能显示成「正常」
    （与 :func:`opencode_bridge.health.read_platform_health` 同纪律）。

    :param bridge_dir: 同 :meth:`SubscriptionHealthRecorder` 的构造参数。
    """
    path = os.path.join(str(bridge_dir or ""), SUBSCRIPTION_HEALTH_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 手改坏的文件不该让 --status 崩
        logger.warning(
            "subscription-health: 读不出来（%s: %s）—— 只当它没有记录",
            exc, path,
        )
        return None
    if not isinstance(document, dict):
        return None
    if not _writer_is_still_running(document):
        # ⚠️⛔ **这是本模块最容易漏的一条**：写下它那个进程**已经不在了** ⇒ 盘上这份
        # 是它**临死前**说的最后一句，而⛔ **那不是「此刻」**。若照样显示，用户会在
        # 一个昨天崩掉的桥上读到「订阅正常，正在收事件」—— 那正是本模块要消灭的
        # 那类假话，只是换了个进程当受害者。
        #
        #: 判据用**已经标定过的** :func:`opencode_bridge.instance_lock.pid_is_alive`
        #（:func:`opencode_bridge.__main__._runtime_state` 判桥是否在跑用的就是它）
        # ⇒ ⛔ 不在这里自己发明一个「记录太旧了」的秒数：那需要标定，而没标定的阈值
        # 会把「桥健康但安静了很久」（相位不变 ⇒ 压根不写盘）误判成没记录。
        logger.debug(
            "subscription-health: 写下它那个进程（pid=%r）已经不在了 —— 只当它没有记录",
            document.get("pid"),
        )
        return None
    snapshot = document.get(SUBSCRIPTION_RECORD_KEY)
    if not isinstance(snapshot, dict):
        return None
    return _snapshot_from_document(snapshot)


def subscription_status_recorded_at(bridge_dir: str) -> Optional[float]:
    """这份记录**最后一次被写入**的时刻；没有记录 / 读不出来是 ``None``。

    ⚠️ 它是**写盘时刻**，⛔ **不是**「状态变化发生的时刻」—— 那两个在跨进程里分不开
    （出站失败那份的 ``recorded_at`` 有同一个限定词，见
    :func:`opencode_bridge.health.outbound_failure_recorded_at`）。
    诊断与测试用；``--status`` 那一段**不**用它下判断（判据是 pid 存活，不是时刻）。
    """
    path = os.path.join(str(bridge_dir or ""), SUBSCRIPTION_HEALTH_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except Exception:  # noqa: BLE001 - 诊断读侧不该抛
        return None
    recorded_at = document.get("recorded_at") if isinstance(document, dict) else None
    if isinstance(recorded_at, bool) or not isinstance(recorded_at, (int, float)):
        return None
    return float(recorded_at)


def _writer_is_still_running(document: dict) -> bool:
    """写下这份记录的进程**此刻**还活着吗。

    ⚠️ 缺 :data:`pid` ⇒ **当它不在**（⇒ 没有记录）：一份没有写者的记录分不出
    「谁说的」与「现在还有没有人说」。
    """
    writer_pid = document.get("pid")
    if isinstance(writer_pid, bool) or not isinstance(writer_pid, int):
        return False
    if writer_pid <= 0:
        return False
    return pid_is_alive(writer_pid)


def _snapshot_from_document(snapshot: dict) -> Optional[SubscriptionStatus]:
    """从盘上那个子文档还原出一份快照；**读不出来是** ``None``。

    ⚠️ **认不出的 ``phase`` 逐字传下去**、⛔ 不归一化成 ``idle`` / ``streaming`` ——
    视图层有它自己那一档「认不出来」（:mod:`opencode_bridge.subscription_status_view`
    的 ``SUBSCRIPTION_DISPLAY_UNRECOGNISED``，而那一档⛔ **绝不当成正常**）。
    ⇒ 读侧若在这里替它挑一个「看起来最像」的档位，那一档就是**编的**。
    """
    phase = snapshot.get("phase")
    if not isinstance(phase, str) or not phase:
        return None
    return SubscriptionStatus(
        phase=phase,
        subscriptions_started=_count_of(snapshot, "subscriptions_started"),
        reconnect_attempts=_count_of(snapshot, "reconnect_attempts"),
        frames_received=_count_of(snapshot, "frames_received"),
        last_error=_error_text_of(snapshot),
    )


def _count_of(snapshot: dict, key: str) -> int:
    """一个计数器；不是非负整数就当 **0**。

    ⚠️ ⛔ 认不出来的数**不许**被"就近取整"成一个看着合理的值（那是编造观测）。
    ⚠️ ``bool`` 是 ``int`` 的子类 ⇒ 必须显式排除，否则 ``True`` 会变成「1 次」。
    """
    value = snapshot.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _error_text_of(snapshot: dict) -> Optional[str]:
    """``last_error``；不是字符串就是 ``None``（⛔ 不 ``str()`` 一个结构当错误正文）。"""
    value = snapshot.get("last_error")
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------
# 进程级装配
#
# ⚠️ **为什么不放进出站失败那份的模块**（:mod:`opencode_bridge.outbound.py` 那一处）：
# 那一份的全局变量住在**消费者**模块里，而消费者是 ``outbound.py``；本模块的消费者
# 是 :mod:`opencode_bridge.event_stream`，而那个文件已经 800 多物理行、
# 在 AGENTS.md §5.0 的待拆名单上 ⇒ 把「记录器的生命周期」也塞进去是让那条债更长。
# ⇒ 记录器与它的装卸都住在**拥有它**的这个模块里。
# --------------------------------------------------------------------------
_installed_recorder: Optional[SubscriptionHealthRecorder] = None


def install_subscription_health_recorder(
    recorder: Optional[SubscriptionHealthRecorder],
) -> None:
    """装上（或卸下）进程级的 :class:`SubscriptionHealthRecorder`。

    由 :mod:`opencode_bridge.__main__` 在**启动时**调用一次，且⛔ **必须早于**
    :class:`~opencode_bridge.core.BridgeCore` 的**构造**（不是 ``start()``）——
    因为看护者是在 :class:`~opencode_bridge.event_stream.EventStream` 的构造里
    建的，而它当场就把记录器取成回调 ⇒ 晚一步装上，那条通道整个不存在，
    而**没有任何东西会报错**（只是「订阅状态永不落盘」）。

    ⚠️ **单向可加**：没装时订阅线程照旧工作，只是不落盘 —— 与
    :func:`opencode_bridge.outbound.install_outbound_failure_recorder` 同一个理由
    （只有运行器知道 ``bridge_dir``）。
    """
    global _installed_recorder
    _installed_recorder = recorder


def installed_subscription_health_recorder() -> Optional[SubscriptionHealthRecorder]:
    """当前装着的记录器（没装返回 ``None``）。诊断与测试用。"""
    return _installed_recorder
