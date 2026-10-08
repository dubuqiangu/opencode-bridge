"""**运行期**凭据失效的落盘与读取 —— 与「上次启动时的探测结论」**分开的一份**。

为什么必须是另一份文件，而不是往 ``platform-health.json`` 里改
=========================================================

:mod:`opencode_bridge.health` 那一份的语义是「**上次启动那一刻**各平台探测出了什么」，
它由运行器在**全部**适配器都试过之后**整份替换**写一次（理由见 ``health`` 模块
docstring「为什么整份替换而不是与上一份合并」）。⇒ 它的形状**结构上**表达不了
"启动之后凭据又被吊销了"这件事：

* ⛔ **不能改写那条结论** —— ``core.BridgeCore.start`` 在 ``start()`` 返回**那一刻**
  同步读走 :attr:`~opencode_bridge.adapters.base.Adapter.startup_verdict` 并落盘
  （钉它的用例是 ``tests/test_telegram_credential_gate.py`` 里
  ``test_recovery_does_not_rewrite_the_startup_verdict_snapshot``）。
  让传输线程回头改它 ⇒ 盘上的值取决于线程调度，"上一次成功"与"最近一次失败"在
  那一刻不可分。
* ⛔ **也不能新增一档 verdict** —— ``health.VERDICTS`` 是**有序**序列，仓库外的
  ``bridge_setup`` 按**值**断言它（``tests/test_platform_health.py`` 逐档钉死）。

⇒ 所以「启动失败」与「运行期失败」**必须是两个不同的键**（本模块一份、
``health`` 一份），读的人也要分别说。这与
:data:`opencode_bridge.health.OUTBOUND_FAILURES_FILE_NAME` 是同一类决定
（出站失败也另开了一份），理由同源：**运行期的事实不该挤进"上次启动"的结论里**。

落盘形态
========

::

    {"recorded_at": <epoch float>,
     "platforms": {"<平台键>": {"at": <epoch float>, "code": <int|str>,
                                "detail": "<脱敏后的一行>",
                                "recovered_at": <epoch float|省略>}}}

⛔ **``recovered_at`` 缺失 = 之后没有再观测到成功**，而它**不是**"此刻一定还坏着"
—— 可能是压根没人再发消息（桥空闲）。所以读它的人必须把两种可能都说出来
（与 :func:`opencode_bridge.health.describe_outbound_failure` 同一条纪律）。

节流：状态机，不是时间窗
========================

判据与 :class:`opencode_bridge.health.OutboundFailureRecorder` **完全同型**
（"健康 → 失败"写、"失败 → 失败"不写、"失败 → 健康"写）⇒ **上界是每平台每次失败
连击 ≤ 2 次写盘**。为什么必须是状态机而不是时间窗：凭据失效可以**非常频繁**
（token 被吊销 / 平台侧鉴权抖动 ⇒ 每轮 ``getUpdates`` 都 401），而时间窗只会让
记录更不准、并不会更省；而状态机不需要任何"多久算一次"的常量 ——
⛔ 而那种常量需要标定，没标定的阈值会把"已经恢复"误判成"还在坏"。

⛔ **写盘失败绝不影响轮询**：每个方法自己兜住异常、记 warning、返回 ``False``
（与 ``OutboundFailureRecorder`` / :class:`opencode_bridge.subscription_health.
SubscriptionHealthRecorder` 同一个纪律 —— 排障记录不该决定桥的生死）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

from .pairing_cli import write_config_atomically
from .redaction import default_redactor

__all__ = [
    "CREDENTIAL_FAILURES_FILE_NAME",
    "NO_CREDENTIAL_FAILURE_TEXT",
    "CredentialFailureRecorder",
    "credential_failure_from_record",
    "credential_failure_recorded_at",
    "credential_failures_in_record",
    "describe_credential_failure",
    "install_credential_failure_recorder",
    "installed_credential_failure_recorder",
    "normalize_credential_failure",
    "read_credential_failures",
]

logger = logging.getLogger("opencode_bridge.credential_health")

#: 落盘文件名，落在 ``bridge_dir`` 下（目录由
#: :func:`opencode_bridge.__main__._bridge_dir` 给出 —— ⛔ 那套推导只有一份，
#: 本模块不自己再推一遍，否则「状态视图读的」与「写它的那条路」会指向两个目录）。
CREDENTIAL_FAILURES_FILE_NAME = "credential-failures.json"

#: 「这个平台没有运行期凭据失败记录」时的**逐字**文案（``--status`` 那一段逐行显示）。
#:
#: ⛔ 取自 :data:`opencode_bridge.health.NO_OUTBOUND_FAILURE_TEXT` 的同一条纪律：
#: 盘上没有记录时分不出"确实没失效过"与"这份记录还没被写过"，而把"没观测到"
#: 说成"一切正常"就是假话（那正是本任务要消灭的那类静默）。
#: 括号里那**三**半都是承重的，缺任何一半这句话就成了假话：
#:
#: * 「自该记录建立以来」—— 时效性只覆盖**记录建立之后**，不是"永远"。
#: * 「不代表此刻凭据有效」—— 没有失败记录**真的**推不出现在还能用（桥可能压根没轮询过）。
#: * 「也不代表没有观测到但没写下来的失败」—— ⚠️ 这一半是**反向**的免责：写盘失败时
#:   观测只进内存里的 :attr:`CredentialFailureRecorder._unwritten`，而这份文件是那条
#:   观测**唯一**的载体 ⇒ 「写不下去」跨进程**真的不可恢复**（AGENTS.md §8 第 3 条）。
#: ⇒ 不另造状态、不另造文件（那都是猜），**残留由这句措辞承担**。
NO_CREDENTIAL_FAILURE_TEXT = (
    "无记录 —— 自该记录建立以来未观测到凭据失效"
    "（不代表此刻凭据有效；也不代表没有观测到但没写下来的失败）"
)

#: 进程级记录器；⛔ **单向可加**（没装时轮询照旧，只是不落盘 —— 与
#: :func:`opencode_bridge.outbound.install_outbound_failure_recorder` 同一个理由：
#: 只有运行器知道 ``bridge_dir``）。
_installed_recorder: Optional["CredentialFailureRecorder"] = None


def _one_line(text: Any) -> str:
    """压成**单行**并截断（与 :func:`opencode_bridge.health._one_line` 同形）。

    ⚠️ **本模块刻意不 import ``health`` 来复用它**：``health`` 已经在
    ``adapters/telegram.py`` 的导入图里，而这条模块在**适配器**的导入图上 ——
    多一条依赖边就多一个循环导入的机会，而这份逻辑只有两行。
    上限取同一个常量 :data:`opencode_bridge.health.MAX_DETAIL_CHARS` 的值
    （``--status`` 的表格按同一份文档渲染）。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= 300:
        return flat
    return flat[:300] + "…"


def _normalize_epoch(value: Any) -> Optional[float]:
    """epoch 秒；不是数字 / 是 ``bool`` / 是 ``NaN`` 一律 ``None``。

    ⚠️ ``bool`` 是 ``int`` 的子类，而 ``True`` 当时刻会变成 1970-01-01 ⇒ 显式排除
    （与 :func:`opencode_bridge.health._normalize_epoch` 同一条）。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:            # NaN：自比较为假 ⇒ 用它把 NaN 挡在外面
        return None
    return number


def normalize_credential_failure(
    code: Any,
    detail: Any = "",
    *,
    at: Any = None,
    recovered_at: Any = None,
) -> dict:
    """把一次运行期凭据失效规范化成**唯一**的落盘形态。

    ::

        {"at": <epoch float>, "code": <int|str>,
         "detail": "<脱敏后的一行>", "recovered_at": <epoch float|省略>}

    ⚠️ ``code`` 沿用 :func:`opencode_bridge.health._normalize_code` 的约定：
    ``int`` 原样保留（要让 ``--status`` 显示 ``code=401``），其余一律转成
    **脱敏过**的字符串 —— 而 :class:`~opencode_bridge.hooks.SendError` 那份里
    "没有就省略" 的规矩**不适用**于这里：``code`` 是这次失效**唯一的机器可读线索**
    （401 = 被吊销 / 过期），缺了它用户只能去读 ``detail`` 的自由文本。
    """
    entry: dict = {
        "at": _normalize_epoch(at) if at is not None else time.time(),
        "code": _normalize_code(code),
    }
    scrubbed_detail = default_redactor().scrub(_one_line(detail))
    if scrubbed_detail:
        entry["detail"] = scrubbed_detail
    recovered = _normalize_epoch(recovered_at)
    if recovered is not None:
        entry["recovered_at"] = recovered
    return entry


def _normalize_code(code: Any) -> Any:
    """``int`` 原样保留，其余转成**脱敏过**的字符串；``None`` ⇒ 字符串 ``"?"``。

    ⚠️ 与 :func:`opencode_bridge.health._normalize_code` 的差别只有一处：
    那里「没有平台错误码」是**缺键**（``code`` 可以 ``None`` ⇒ 键被省略），而这里
    ``code`` **恒在**（``--status`` 与 ``--setup --json`` 的消费方按
    ``entry["code"]`` 取，缺键会 KeyError）⇒ 用字符串 ``"?"`` 占位，形态与
    :data:`opencode_bridge.adapters._redactable_ids.MISSING_ID` 的做法一致
    （缺信息要说出来，不能靠缺键蒙混过去）。
    """
    if code is None:
        return "?"
    if isinstance(code, bool):          # bool 是 int 的子类，不当错误码用
        return str(code)
    if isinstance(code, int):
        return code
    scrubbed = default_redactor().scrub(_one_line(code))
    return scrubbed if scrubbed else "?"


def describe_credential_failure(entry: Optional[Mapping]) -> str:
    """把一条运行期凭据失效渲染成给人看的一段（``--status`` 与日志共用这一份措辞）。

    ⚠️ 这里**只说那一次失效本身**（码 + 原因），**不说"什么时候"** —— 时效性由
    调用方的段标题与时间戳负责（理由与 :func:`opencode_bridge.health.
    describe_verdict` 相同：混在一行里会让"昨天那次失效"读起来像"现在是坏的"）。

    ``entry`` 为 ``None`` 时返回 :data:`NO_CREDENTIAL_FAILURE_TEXT` ——
    ⛔ 那句**不是**"正常"，读它的人不许这么转述（理由见该常量的注释）。
    """
    if not isinstance(entry, Mapping):
        return NO_CREDENTIAL_FAILURE_TEXT
    code = entry.get("code")
    detail = str(entry.get("detail") or "")
    rendered = "forbidden" if code == 401 else f"code={code}"
    return f"{rendered} —— {detail}" if detail else rendered


def read_credential_failures(bridge_dir: str) -> Optional[dict]:
    """读回整份**运行期**凭据失效记录；没有 / 读不出来都是 ``None``。

    ``None`` 的含义是「**没有记录**」，而它**不代表凭据有效**（见
    :data:`NO_CREDENTIAL_FAILURE_TEXT`）。

    ⚠️ 坏文件处置与 :func:`opencode_bridge.health.read_outbound_failures` 一致：
    记一条 warning 当它没有记录，而不是让 ``--status`` 崩 —— 排障通道自己坏了
    已经够糟。
    """
    path = os.path.join(str(bridge_dir or ""), CREDENTIAL_FAILURES_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 手改坏的文件不该让 --status 崩
        logger.warning(
            "credential-failures: %s 读不出来（%s）—— 只当它没有记录", path, exc
        )
        return None
    return document if isinstance(document, dict) else None


def credential_failure_from_record(
    record: Optional[Mapping], key: str
) -> Optional[dict]:
    """从整份运行期记录里取**一个平台**的凭据失效；没有就 ``None``（= 无记录）。

    读取侧**再过一次** :func:`normalize_credential_failure`：这份文件可能被用户手改过，
    而消费方不该为"文件里有个没见过的字段"自己兜底。

    ⛔ **读路径不许把「盘上没记时刻」当成「失效发生在现在」** ——
    :func:`normalize_credential_failure` 是**写**路径的规范化器，它给 ``at=None``
    补 ``time.time()``。那在写路径上是对的（刚刚真的失效了一次），而在**读**路径上
    就是**编造**。⇒ 读路径把 ``at`` 设成**解析出来的值**（解析不出就是 ``None``），
    ⛔ 不是补一个占位时刻、⛔ 也不是 ``pop`` 掉这个键（键恒在、值可空 ——
    缺键会让按 ``entry["at"]`` 取的外部消费者 KeyError）。
    与 :func:`opencode_bridge.health.outbound_failure_from_record` 同一条纪律。
    """
    if not isinstance(record, Mapping):
        return None
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return None
    entry = platforms.get(str(key))
    if not isinstance(entry, Mapping):
        return None
    stored_at = entry.get("at")
    normalized = normalize_credential_failure(
        entry.get("code"),
        entry.get("detail"),
        recovered_at=entry.get("recovered_at"),
    )
    normalized["at"] = _normalize_epoch(stored_at)
    return normalized


def credential_failures_in_record(record: Optional[Mapping]) -> tuple:
    """运行期记录里出现过的平台键（坏文件当空的）。"""
    if not isinstance(record, Mapping):
        return ()
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return ()
    return tuple(str(key) for key in platforms)


def credential_failure_recorded_at(record: Optional[Mapping]) -> Optional[float]:
    """这份运行期记录最后一次被写入的时刻（epoch 秒）；没有 / 不是数字则 ``None``。"""
    if not isinstance(record, Mapping):
        return None
    return _normalize_epoch(record.get("recorded_at"))


class CredentialFailureRecorder:
    """运行期凭据失效的写入方：**状态变化时才写盘**。

    ## 节流判据（为什么是状态机而不是时间窗）

    ==========================  ======  ====================================
    观测                          写盘?   理由
    ==========================  ======  ====================================
    「有效 → 失效」（首次失效）   是      这就是要让人看见的那一次
    「失效 → 失效」（连续失效）   **否**  记录已经写着"这个平台失效了"，
                                    再写一遍不增加任何信息（而凭据失效可以
                                    **每轮**都观测到一次）
    「失效 → 有效」（恢复）      是      不写就永远显示"正在失效"，而它其实已经好了
    「有效 → 有效」              否      压根没有记录要改
    ==========================  ======  ====================================

    ⇒ **上界是「每平台每次失效连击 ≤ 2 次写盘」**（进入 + 恢复）。

    ⚠️ **状态是进程内的，不从盘上恢复** ⇒ 桥重启后第一次失效会**再写一次**（把
    ``at`` 刷新到本次运行）—— 那一次写盘是有用的（用户查的就是"重启之后还有没有
    在失效"），而它是上面那个上界里的**第一次**。

    ## 写盘失败时内存态怎么办（**一条不许造出来的记录**）

    ⚠️ **写盘失败绝不允许留下"有个失效连击在结束"的印象**：那样下一次恢复会去给一条
    **盘上压根没有**的失效盖 ``recovered_at``，而盖不出时刻时唯一的办法就是**编一条**
    出来（那正是本模块要消灭的那类假话）。
    ⇒ 所以两件事都守住：① **写盘失败就不进连击**（:attr:`_failing` 只装"盘上真的
    记着"的）；② **恢复时盘上没有记录就不写**（:meth:`note_recovery` 不造）。

    ⚠️ 代价与它的边界（**为什么不是"每一次失效都重试"**）：写盘失败的那一次连击在
    **本进程内不再重试写盘**（记进 :attr:`_unwritten`），否则每次观测都要付一遍
    ``mkstemp`` + ``fsync`` 的钱，而那些钱买到的**必然是又一次失败**。
    ⇒ 边界是**按连击**划的：同一次连击里重复观测不重试，而**下一次**连击会重新尝试写。

    ⛔ **写盘失败绝不影响轮询**：每个方法都自己兜住异常、记 warning、返回 ``False``。
    """

    def __init__(self, bridge_dir: str) -> None:
        self._bridge_dir = str(bridge_dir or "")
        #: 正处于「失效连击」中、且**盘上真的记着**的平台（内存态）。
        #: ⛔ **只装写盘成功的** —— 见上面「写盘失败时内存态怎么办」。
        self._failing: set = set()
        #: 观测到失效、但那一次写盘**没成功**的平台（同一次连击内不再重试写盘）。
        self._unwritten: set = set()
        #: 把 :meth:`_write` 的**整个读改写**串行化的进程内锁。
        #:
        #: ⚠️ 名字是「锁住那个文件」而不是「锁住写这一步」—— 它覆盖的是
        #: **读 → 合并 → 写整段**；只包住写那一段的话，两次合并仍然基于同一份旧读，
        #: 后写的照样把先写的整份覆盖掉。⇒ **本仓库至少有三个线程走这条路**
        #: （SSE 线程、每个适配器的轮询线程、homeassistant 等各自的 worker）。
        self._file_lock = threading.Lock()

    # --- 观测入口 -----------------------------------------------------
    def note_failure(self, platform: str, code: Any, detail: Any = "") -> bool:
        """记一次运行期凭据失效；**真的写了盘**才返回 ``True``。

        :param platform: 平台键（:func:`opencode_bridge.health.platform_key` ——
            与 ``--status`` 列平台同一个来源，否则这条记录永远落不到用户看到的那一行上）。
        :param code: 平台自己的错误码（Telegram 的 401）。
        """
        key = str(platform or "")
        if not key:
            return False
        if key in self._failing:
            return False          # 失效连击中：记录已经写着"在失效"，不重复写
        if key in self._unwritten:
            return False          # 同一次连击的写盘已经失败过
        written = self._write(
            key, normalize_credential_failure(code, detail)
        )
        # ⚠️ **写盘成功之后才进连击** —— 顺序是承重的。先记进 `_failing` 的话，
        # 一次写盘失败会留下"盘上没有记录的失效连击"，而下一次恢复就会去给它盖
        # `recovered_at` ⇒ 盘上凭空多出一条**从未观测到**的失效。
        if written:
            self._failing.add(key)
        else:
            self._unwritten.add(key)
        return written

    def note_recovery(self, platform: str) -> bool:
        """记一次凭据恢复（**仅当它结束了一段失效连击**才写盘）。

        ⛔ 它**不会**删掉那条失效记录，而是给它盖一个 ``recovered_at`` 时间戳。
        理由与 :meth:`opencode_bridge.health.OutboundFailureRecorder.note_success`
        同源：恢复**不等于**"从那一刻起就再没失效过"，而用户要问的是
        "它刚才那次失效持续到几点"。
        """
        key = str(platform or "")
        if not key:
            return False
        if key in self._unwritten:
            # 这一段连击**从来没写下去过** ⇒ 盘上没有可盖戳的记录，什么都不写
            # （清掉标记，于是**下一次**失效连击会重新尝试写盘）。
            self._unwritten.discard(key)
            return False
        if key not in self._failing:
            return False          # 没有失效连击在结束：没有记录要改
        self._failing.discard(key)
        existing = credential_failure_from_record(self._read(), key)
        if existing is None:
            # ⛔ **盘上没有 = 没有可盖戳的观测，不造。** 这条兜底可达（盘上那份被
            # 用户删掉 / 改成坏 JSON，而进程内的连击还在），而正因可达才更不能造：
            # 这条路唯一的输入是"我这儿记着连击，盘上却没有"。
            return False
        existing["recovered_at"] = time.time()
        return self._write(key, existing)

    # --- 内部 ---------------------------------------------------------
    def _read(self) -> Optional[dict]:
        return read_credential_failures(self._bridge_dir)

    def _write(self, key: str, entry: Mapping) -> bool:
        """读改写一次整份记录；**任何异常都在这里兜住**，返回是否真写到盘上。

        ⚠️ 与 :func:`opencode_bridge.health.record_startup_probes` 一样复用
        :func:`~opencode_bridge.pairing_cli.write_config_atomically`，不复制函数体：
        截断一半的 JSON 会让下次 ``--status`` 读不到记录，而读不到记录正是本任务想
        消灭的那种"零线索"。

        ⚠️ 与那份的**整份替换**相反，这里是**读改写**：每个平台一条、彼此独立，必须
        保住别的平台已经记下的内容（启动探测那份之所以能整份替换，正是因为它的语义
        是"最近一次启动的全部结论"，而这里不是）。

        ⚠️ **读、合并、落盘整段都在同一个 ``try`` 里** —— 合并段落在 ``try`` 之外时，
        盘上 ``platforms`` 不是 JSON 对象时抛的 ``ValueError`` 会**穿透**
        :meth:`note_failure` ⇒ 那次观测一个字没落盘、:attr:`_failing` 与
        :attr:`_unwritten` 都空 ⇒ 在用户手工修好那个文件之前，这条通道对该平台
        **永久失效**，而**没有任何东西会报错**。
        """
        path = os.path.join(self._bridge_dir, CREDENTIAL_FAILURES_FILE_NAME)
        with self._file_lock:
            try:
                document = self._read()
                if not isinstance(document, dict):
                    document = {}
                # 只留别的平台 —— 本平台那条由本方法**整体替换**，否则残留的
                # ``recovered_at`` 会挂在一次全新的失效上（说"这次失效已恢复"）。
                platforms = {
                    name: value
                    for name, value in dict(document.get("platforms") or {}).items()
                    if str(name) != key and isinstance(value, Mapping)
                }
                platforms[key] = dict(entry)
                payload = {"recorded_at": time.time(), "platforms": platforms}
                write_config_atomically(
                    path, default_redactor().scrub_persisted_value(payload)
                )
            except Exception as exc:  # noqa: BLE001 - 这条观测写不下去绝不该打断轮询
                # ⚠️ 措辞覆盖的是「这次观测**没落下去**」而不只是「写失败」：现在读盘与
                # 合并也在这个 ``try`` 里，而 ``--status`` 那半句免责要交代的事，
                # 它唯一的诊断来源就是这一行，所以这里不许说一件没发生的事。
                logger.warning(
                    "credential-failures: %s 写不下去（%s: %s）—— 不影响桥的轮询",
                    path, type(exc).__name__, exc,
                )
                return False
        return True

    # --- 测试与诊断 ---------------------------------------------------
    def failing_platforms(self) -> tuple:
        """观测到失效、其中盘上记着一部分的平台（诊断用；``--status`` 读的是盘上那份）。

        ⚠️ **并上 :attr:`_unwritten`**：那次连击确实观测到失效了（哪怕一个字都没
        写下盘）—— 只报「盘上记着的」会把"刚失效过一次、但磁盘写不进去"说成没失效，
        而那是一条**假的否定观测**（与本模块消灭的是同一类病）。
        """
        return tuple(sorted(self._failing | self._unwritten))


def install_credential_failure_recorder(
    recorder: Optional[CredentialFailureRecorder],
) -> None:
    """装上（或卸下）进程级的 :class:`CredentialFailureRecorder`。

    由 :mod:`opencode_bridge.__main__` 在**启动时**调用一次，且⛔ **必须早于**
    :meth:`~opencode_bridge.adapters.base.Adapter.start` —— 因为适配器在
    :meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._poll_round` 里就
    会用它，而那条路**当场**取记录器 ⇒ 晚一步装上，那条通道整个不存在，
    而**没有任何东西会报错**（只是「凭据失效永不落盘」）。

    ⚠️ **单向可加**：没装时轮询照旧工作，只是不落盘 —— 与
    :func:`opencode_bridge.outbound.install_outbound_failure_recorder` 同一个理由。
    """
    global _installed_recorder
    _installed_recorder = recorder


def installed_credential_failure_recorder() -> Optional[CredentialFailureRecorder]:
    """当前装着的记录器（没装返回 ``None``）。诊断与测试用。

    ⚠️ 返回 ``None`` 是**正常**状态（``--setup`` / 测试 / 库式调用都不装配它）
    ⇒ 调用方必须自己判 ``None``，⛔ 不许假设它一定在。
    """
    return _installed_recorder
