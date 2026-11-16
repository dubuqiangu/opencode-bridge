"""Telegram 凭据生命周期域（§5.1 第一刀：自 ``telegram.py`` 的 ``TelegramAdapter`` 抽出）。

本模块持有启动凭据闸门探测、运行期 401 自愈与恢复播报的完整生命周期：
8 个方法自 ``TelegramAdapter`` **逐字节移植**（AST 同一性由手术脚本逐节点断言），
5 个状态属性连同 ``#:`` 注释随方法一起迁来。

**两个留守域**（⛔ 刻意不随迁，留在 ``telegram.py`` 的适配器上）：

- 探测退避**阶梯三方法**（``_credential_probe_initial_backoff`` /
  ``_credential_probe_max_backoff`` / ``_credential_probe_backoff_for``）——
  阶梯用 ``getattr(self, "credential_probe_*_backoff", 常量)`` 读**适配器实例**上的
  可配旋钮，测试在构造后直接 ``adapter.credential_probe_initial_backoff = ...`` 设置；
  搬走会让 ``getattr`` 落到本类实例上、**静默回落常量**（比响亮红更坏）。
- 运行期失效**落盘记录两方法**（``_record_runtime_credential_failure`` /
  ``_record_runtime_credential_recovery``）—— 它们以
  ``health.platform_key(self)`` 把**适配器本体**传给记录器，搬走会静默算出错误的
  平台键。

**协作解析**：上述留守成员经本类的 8 个 read-only property **逐调用转发**回
适配器本体（``self._adapter._post`` 等）—— 与手术前 ``self.X`` 在适配器实例上
**动态解析**的语义完全一致：凭据闸门 / selfheal 测试在构造后替换
``adapter._post`` / ``adapter._stop_event`` 的打桩手法**继续生效**
（初版「构造时注入绑定可调用」被基线复跑证伪：冻结引用看不见构造后的替换，
13/17 真红 + 真网 8s 超时）⇒ 移植来的方法体一个字节都不用改。

⚠️ 随方法迁来的 docstring/注释里，对**留守**成员的 ``:meth:` / ``:attr:` 裸引用
（如 ``start()``、``_flush_history_once``）指向它们在 ``telegram.py`` 里的原主 ——
语义未变，只是跨模块了。

logger 通道名保持 ``opencode_bridge.adapters.telegram``（日志逐字节不变）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from ..hooks import SendError
from ..transport import ReconnectNow
from .base import classify_http
from .telegram_protocol import (
    CREDENTIAL_PERMANENT_FAILURE_COOLDOWN,
    CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD,
    PERMANENT_FAILURE_LOG_MARKER,
    RUNTIME_CREDENTIAL_REVERIFY_LIMIT,
    _clamp_permanent_failure_cooldown,
)

logger = logging.getLogger("opencode_bridge.adapters.telegram")


class TelegramCredentialLifecycle:
    """Telegram 凭据生命周期：启动闸门探测 + 运行期自愈 + 恢复播报。

    由 ``TelegramAdapter`` 构造并以绑定协作者注入；适配器侧保留
    ``_credentials_verified`` / ``_runtime_credentials_invalid`` 两个 property 与
    4 个转发方法，保住既有测试与留守调用点（消费面零改动）。
    """

    def __init__(self, *, adapter: Any) -> None:
        # 协作者经 ``_adapter`` **逐调用动态解析**（见下面 8 个 read-only property）
        # —— 与手术前 ``self.X`` 在适配器实例上的解析语义完全一致：测试在构造后
        # 替换 ``adapter._post`` / ``adapter._stop_event`` 的打桩继续生效，
        # 8 个方法体逐字节不动。构造时只存适配器引用，不冻结任何成员。
        self._adapter = adapter
        #: 凭据闸门**已通过**（见 :meth:`_probe_until_credentials_verified`）。
        #: :attr:`running` 与它取与 —— 线程活着但凭据没过**不算**在跑
        #: （那正是入站一条都收不到的状态，报成"在跑"是假话）。
        self._credentials_verified = False
        #: 这一轮探测已尝试了几次（``start()`` 里那次同步探测**算第 1 次**）。
        self._credential_probe_attempts = 0
        #: ⭐「**同一张凭据**」连续失败了几次（见 ``telegram_protocol`` 里
        #: :data:`~opencode_bridge.adapters.telegram_protocol.CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD`
        #: 的注释）。与 :attr:`_credential_probe_attempts` **不是同一个计数器**，⛔ 不许合并：
        #: 前者是"这一轮会话里打过几次 getMe"（启动段与运行段**各自**用它说"第 N 次"），
        #: 后者是"这张凭据自己已经连挂了几次、中间有没有活过来一次"。
        #: ⚠️ 前者在运行期每一轮重新探测时**归零**，而后者必须**跨会话、跨轮次**活着 ——
        #: 否则 60s 一次的重连会把同一条墙重新数成第 1 次，阈值永远到不了（永久的墙
        #: 会被永远当成"刚开始抖"）。两条分支（启动段 / 运行段）**共用**这一个计数。
        self._consecutive_failures_for_same_credential = 0
        #: ⭐ 上面那个计数**是在哪张凭据上**数出来的。凭据变了 ⇒ 换了一条连续的
        #: 失败链 ⇒ 计数从 0 起、而且新一条**有权再报一次**永久失败（见
        #: :meth:`_is_same_credential_as_last_failure`）。
        self._credential_fingerprint_of_failure_streak = ""
        #: ⭐ **永久失败的那一行**这一条失败链里已经报过没有。
        #: ⛔ 不拿「计数 >= 阈值」当判据：那样一旦计数不归零，每一轮等待都会再报一行
        #: ⇒ 故障一小时 = 60 行同样的 ERROR，而那正是这条要修的"刷屏"。
        #: ⇒ 报完置真，唯一的复位点是**探通**（:meth:`_announce_credential_recovery`
        #: 那条路径）与**换了凭据**（另起一条链）。
        self._permanent_failure_announced_for_credential = False
        #: 这一轮探测的**起点**（monotonic）—— 恢复行里的「历时 T 秒」用它算。
        self._credential_probe_started_at = 0.0
        #: ⭐ **运行期**凭据失效（启动之后 ``getUpdates`` 回过 401）。与
        #: :attr:`startup_verdict`（**启动那一刻**的结论）是**两个不同的键** ——
        #: 前者只由 :meth:`start` 写一次，后者由传输线程在**观测到 401 那一刻**置位。
        #: ⇒ ``--status`` 因此能**分开说**"上次启动时是好的 / 但运行期失效过"
        #: 而不是把两件事混成一个值（理由见
        #: :mod:`opencode_bridge.credential_health` 模块 docstring）。
        #: ⚠️ 只由**传输线程**读写（``_poll_round`` → :meth:`_invalidate_credentials`）。
        self._runtime_credentials_invalid = False
        #: 运行期那一轮重新探测已尝试了几次（上界
        #: :data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT`）。⛔ **不给它设放弃点** ——
        #: 放弃之后那一行必须**说清**"需要重启桥"，而 :attr:`_runtime_credentials_invalid`
        #: 会**继续为真** ⇒ 入站继续不放行（理由见 :meth:`_reverify_runtime_credentials`）。
        self._runtime_reverify_attempts = 0
        #: ⭐ 越过阈值之后的**固定复查间隔**（见
        #: :data:`~opencode_bridge.adapters.telegram_protocol.CREDENTIAL_PERMANENT_FAILURE_COOLDOWN`）。
        #: 与 :attr:`_credential_probe_initial_backoff` / ``_max_backoff`` 同一个纪律：
        #: **旋钮在适配器实例上、调度器经 ``getattr`` 逐调用读** ⇒ 测试在构造后直接
        #: ```adapter.credential_permanent_failure_cooldown = 0.01``` 就能让它生效，
        #: ⛔ 不必去改模块常量（改了模块常量就波及同一进程里所有别的用例）。
        #: ⚠️ 而它**不是** :data:`CREDENTIAL_PROBE_MAX_BACKOFF` 的别名（理由见
        #: ``telegram_protocol`` 里那段「不是同一个旋钮」）。
        #: ⚠️ 旋钮**不在本类实例上**：本类是协作者，测试拿到的是适配器
        #: ⇒ 名字与另外三个阶梯旋钮同住 ``TelegramAdapter``（``telegram.py``），
        #: 这里只在构造时**种一次**类属性默认值（模型来自本模块 import 的常量）。
        if not hasattr(adapter, "credential_permanent_failure_cooldown"):
            adapter.credential_permanent_failure_cooldown = (
                CREDENTIAL_PERMANENT_FAILURE_COOLDOWN
            )

    # ------------------------------------------------------------------
    # 协作者转发面：8 个 read-only property，**逐调用**经适配器本体解析 ——
    # 这正是手术前 ``self.X`` 的语义（在适配器实例上每次调用重新查找），
    # 测试在构造后替换 ``adapter._post`` / ``adapter._stop_event`` 照常生效。
    # ------------------------------------------------------------------
    @property
    def _post(self):
        """发一次平台请求（适配器方法；``bot_token`` 在适配器解析）。"""
        return self._adapter._post

    @property
    def _stop_event(self):
        """终止事件（测试会整只替换 ``adapter._stop_event``，必须动态取）。"""
        return self._adapter._stop_event

    @property
    def _flush_history_once(self):
        """丢弃积压历史（留守适配器方法，⛔ 每进程只跑一次的语义在那边）。"""
        return self._adapter._flush_history_once

    @property
    def _credential_probe_backoff_for(self):
        """退避阶梯的取值函数（留守：经 ``getattr`` 读适配器实例上的旋钮）。"""
        return self._adapter._credential_probe_backoff_for

    @property
    def _credential_probe_initial_backoff(self):
        """退避阶梯初值（留守：旋钮在适配器实例上）。"""
        return self._adapter._credential_probe_initial_backoff

    @property
    def _credential_probe_max_backoff(self):
        """退避阶梯封顶（留守：旋钮在适配器实例上）。"""
        return self._adapter._credential_probe_max_backoff

    @property
    def _record_runtime_credential_failure(self):
        """运行期失效的落盘记录（留守：``health.platform_key`` 要适配器本体）。"""
        return self._adapter._record_runtime_credential_failure

    @property
    def _record_runtime_credential_recovery(self):
        """运行期恢复的落盘盖章（留守：同上，``platform_key`` 要适配器本体）。"""
        return self._adapter._record_runtime_credential_recovery


    def _credential_gate(self) -> None:
        """``PollingTransport`` 的 ``on_open`` 钩子：**凭据闸门**。

        它跑在 :meth:`~opencode_bridge.transport.base.Transport.start` 建的
        **那条已经存在的 daemon 线程**里 ⇒ **新增线程数 = 0**，调用链与终止路径
        与今天完全同一条（``stop()`` → 置停止位 → 关传输层 → join）⇒ 桥关闭时
        不可能留下悬挂的探测线程，因为**根本没有新线程**。

        ⛔ **绝不许**改成让 ``BridgeCore`` 反复驱动 ``adapter.start()``：
        ``start()`` 是**一次性生命周期钩子**（它有 ``_stop_event.clear()``、
        会走 :meth:`_flush_history_once`（里面那个 ``_pending.clear()``），且
        **无条件**覆写 ``self._transport``）⇒ 重跑它要么重复丢历史，
        要么在已经起来的适配器上**泄漏一条 transport 线程**。

        ⚠️ **运行期 401 走的是另一条分支**（:meth:`_reverify_runtime_credentials`）：
        启动段那个阶梯**永不放弃**、而运行段**有次数上界**（理由与取舍见
        :data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT`）。两条分支的判据不同，所以这里
        **必须**按"这是不是运行期失效"分流，⛔ 不许把它们合成一条循环。
        """
        if self._runtime_credentials_invalid:
            self._reverify_runtime_credentials()
            return
        self._probe_until_credentials_verified()

    def _probe_until_credentials_verified(self) -> None:
        """重试 ``getMe`` 到通过为止。**永不放弃**。

        **为什么永不放弃**（理由与「代价有界」一起看）：改 ``config.json``
        **不会**重启桥（``run_bridge`` 起完就 ``while stop_event.wait(1.0)`` 直到
        Ctrl+C）⇒ 任何"放弃点"都会把用户**改完配置**这条唯一的自助修法变成
        "必须重启进程"，那正是本次要修的缺陷在**确定性失败**那一档上原样保留。
        代价有界：封顶 60s ⇒ 最坏每分钟一次 ``getMe``（可忽略）。

        ⚠️ **阶梯由本适配器自己按尝试次数算**，park 在 ``_stop_event.wait()`` 上
        （``stop()`` 一置位就立刻可打断，**不是** ``time.sleep``），**不落在**那个
        ``PollingTransport`` 实例上 —— 理由见 :meth:`_make_transport` 末尾那段。

        抛 :class:`~opencode_bridge.transport.ReconnectNow` 表示"这次会话作废"：
        传输层会立刻重连而**不退避**。它只在 ``stop()`` 已请求时抛出（本次会话
        永远不会通过）⇒ 借它把中止变成一次干净的会话结束，而不是让 ``_open()``
        返回后继续往下跑 ``getUpdates``。
        """
        if self._credentials_verified:
            # 已经验过：后续会话（``getUpdates`` 失败后的重连）不再重复打 getMe。
            # ⚠️ 启动**之后** token 被吊销这件事不由这里管：`getUpdates` 自己带
            # 鉴权，被吊销时它回 401 ⇒ ``_poll_round`` 抛异常 ⇒ 传输层按既有退避
            # 一直重连（入站不会被掐死，只是不再有消息进来）—— 那是既有行为。
            return
        while True:
            self._credential_probe_attempts += 1
            failure = self._probe_get_me_once()
            if failure is None:
                self._announce_credential_recovery()
                # ⛔ 积压历史**只在这里丢一次**（见 :meth:`_flush_history_once`）。
                self._flush_history_once()
                self._credentials_verified = True
                return
            self._log_credential_failure(failure)
            self._note_permanent_failure_streak_advanced()
            # ⚠️ 过了阈值之后换**固定冷却**，不再走那条翻倍阶梯 ——
            # 两条阶梯的判据不同（见 :data:`CREDENTIAL_PERMANENT_FAILURE_COOLDOWN`），
            # 而这条必须是 ``_stop_event.wait``：``stop()`` 一置位就立刻醒。
            # ⛔ 注释必须夹在两个分支**之间**也照样成立 —— 而它们属于 ``else``，
            # 所以整段搬进 ``else`` 里，⛔ 不留「替不属于 ``if`` 的话打掩护」的余地。
            if self._permanent_failure_escalation_due():
                self._announce_permanent_failure_once()
                delay_seconds = _clamp_permanent_failure_cooldown(
                    # 旋钮读**适配器实例**（⛔ 不是本协作者）—— 名字与另外三个
                    # 阶梯旋钮同住 ``TelegramAdapter``，测试在构造后给它赋值。
                    getattr(
                        self._adapter,
                        "credential_permanent_failure_cooldown",
                        CREDENTIAL_PERMANENT_FAILURE_COOLDOWN,
                    ),
                    # 下界跟**生效中的**初值走（同一个实例旋钮，见
                    # :func:`~opencode_bridge.adapters.telegram_protocol._clamp_permanent_failure_cooldown`）。
                    self._credential_probe_initial_backoff,
                )
            else:
                # ``attempts - 1`` = 这是第几次**等待**（线程里第一次失败是第 1 次等待
                # ⇒ 拿初值）。⚠️ 直接传 ``attempts`` 会让**初值那一档永远用不上**
                # （同步那次是第 1 次尝试、它不等），阶梯就变成"4s 起步" —— 而
                # 「初值 2s → ×2」这句话会被读成一句假话。
                delay_seconds = self._credential_probe_backoff_for(
                    self._credential_probe_attempts - 1
                )
            if self._stop_event.wait(delay_seconds):
                raise ReconnectNow("getMe 探测已中止（stop() 已请求）")

    def _invalidate_credentials(self, code: Any, description: Any) -> None:
        """``getUpdates`` 回 401 ⇒ **凭据在运行期失效**，把闸门重新关上。

        改之前这一格是空的：凭据闸门**验过一次就永不再探**（``_credentials_verified``
        为真时 :meth:`_credential_gate` 直接返回）⇒ token 在启动之后被吊销时，
        ``getUpdates`` 连续回 401，而每一轮都只是「抛异常 → 恒定 2.0s 退避 → 再抛」，
        ``getMe`` **一次都不打** ⇒ 换回 token 也不会自愈。

        为什么放在 ``_poll_round`` 而不是闸门里：401 是**在轮询里观测到的**，
        只有真正发了 ``getUpdates`` 的那个线程知道 ⇒ 判据与观测必须同一个地方。

        ⚠️ **只认 401**（:data:`UNAUTHORIZED_ERROR_CODE`）—— 按**语义**判，不按
        "任何失败都算"（理由见那个常量的注释）。

        ⚠️ **绝不碰** :attr:`startup_verdict`：那条是「上次**启动**那一刻」的结论，
        由 :meth:`start` 写一次就定（理由见 :meth:`start` 的 docstring）⇒ 运行期的
        事实走**另一个键** :attr:`_runtime_credentials_invalid` + 落盘那份
        :mod:`opencode_bridge.credential_health`，⛔ 两件事不许混成一个值。
        """
        if self._runtime_credentials_invalid:
            return          # 已经在失效态：重复的 401 不重复置位、不重复打日志
        self._runtime_credentials_invalid = True
        self._credentials_verified = False
        # 这一轮重新探测的计数与起点**重置**：恢复行里的「第 N 次 / 历时 T 秒」
        # 必须描述**这一轮运行期故障**，而不是把启动段那几个数字接着往上加
        # （否则用户读到「第 37 次尝试 / 历时 4 小时」而故障才刚发生 3 秒）。
        self._runtime_reverify_attempts = 0
        self._credential_probe_started_at = time.monotonic()
        self._record_runtime_credential_failure(code, description)
        logger.error(
            "telegram: 运行期凭据失效（code=%s）：%s —— 入站已停止，"
            "将按退避重新探 getMe（初值 %.0fs、×2、封顶 %.0fs、至多 %d 次）；"
            "请检查/更新 config.json 里的 bot_token（@BotFather 重新签发）",
            code, description or "（平台没给描述）",
            self._credential_probe_initial_backoff(),
            self._credential_probe_max_backoff(),
            RUNTIME_CREDENTIAL_REVERIFY_LIMIT,
        )

    def _reverify_runtime_credentials(self) -> None:
        """运行期失效后重新探 ``getMe``：**至多** :data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT` 次。

        ✅ 探通 ⇒ **自愈**：:attr:`_credentials_verified` 置回真、入站继续收发，
        并把落盘那条盖上 ``recovered_at``。

        ⛔ **探满仍失败 ⇒ 说清「要重启桥」并停在这里**（见
        :data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT` 上面那段取舍）。

        ⚠️ **放弃之后不许偷偷恢复**：:attr:`_runtime_credentials_invalid` 与
        :attr:`_credentials_verified` 都**留在假** ⇒ :attr:`running` 继续为假、
        入站继续不放行。而下一次会话进来时上面那句 ``attempts >= 上界`` 会让本方法
        **直接返回**（连日志都不再打）⇒ ``getMe`` 的调用总数**严格有界**，
        不会退化成"每 2 秒探一次、探一整夜"（那正是今晚反复出现的形态）。

        ⚠️ 等待 park 在 ``_stop_event.wait()`` 上（``stop()`` 一置位就立刻可打断），
        阶梯**复用** :meth:`_credential_probe_backoff_for`（初值 → ``×2`` → 封顶）
        ⇒ **不另写一份阶梯**，也不落在这个 ``PollingTransport`` 实例上
        （理由见 :meth:`_make_transport` 末尾那段）。
        """
        if self._runtime_reverify_attempts >= RUNTIME_CREDENTIAL_REVERIFY_LIMIT:
            return      # 已经探满：那一行「需要重启桥」在超限那一刻打过，不再重复
        while self._runtime_reverify_attempts < RUNTIME_CREDENTIAL_REVERIFY_LIMIT:
            self._runtime_reverify_attempts += 1
            failure = self._probe_get_me_once()
            if failure is None:
                self._announce_runtime_credential_recovery()
                self._record_runtime_credential_recovery()
                self._runtime_credentials_invalid = False
                self._credentials_verified = True
                return
            self._log_credential_failure(failure)
            if self._runtime_reverify_attempts >= RUNTIME_CREDENTIAL_REVERIFY_LIMIT:
                break       # 超限：不再多等一档，直接说清「需要重启桥」
            if self._stop_event.wait(
                self._credential_probe_backoff_for(self._runtime_reverify_attempts)
            ):
                raise ReconnectNow("getMe 运行期重探已中止（stop() 已请求）")
        logger.error(
            "telegram: 已重新探 getMe %d 次仍失败 —— **停止重探，需要重启桥**。"
            "（改完 bot_token 必须重启；本进程不会自己去读 config.json）",
            RUNTIME_CREDENTIAL_REVERIFY_LIMIT,
        )

    def _announce_runtime_credential_recovery(self) -> None:
        """运行期自愈的那一行：必须**响亮**（WARNING）且带「第 N 次 / 历时 T 秒」。

        ⚛️ 与启动段那条（:meth:`_announce_credential_recovery`）**形状一致但分开**：
        两者的「第 N 次」数的是**不同的两轮**（启动段那轮 vs 这一次运行期故障），
        混成一行会让用户对不上"到底是第几次"。
        """
        elapsed = max(0.0, time.monotonic() - self._credential_probe_started_at)
        logger.warning(
            "telegram: 运行期凭据恢复：重新探 getMe 第 %d 次通过 / 历时 %.1f 秒 —— "
            "入站轮询继续",
            self._runtime_reverify_attempts, elapsed,
        )

    def _probe_get_me_once(self) -> Optional[dict]:
        """跑**一次** ``getMe``。**返回 ``None`` = 通过**；否则返回失败记录。

        失败记录四个键：``code``（平台 ``error_code``；载荷不是 dict 时是 ``"?"``）、
        ``description``、``detail``（上报口径的一行）、``raised``
        （走没走异常路径 —— **只**用来选那行**文档引用**的日志措辞）。

        ⚠️ **不要拿「异常类型」当生产里的瞬时判据**：:meth:`_post` 自己把传输
        异常包成 ``{"ok": False, "error_code": 0, "description": "transport error: …"}``
        再 ``return`` ⇒ 生产里**根本走不到**下面的 ``except``（它只对被替换的
        实现 / 未预料的异常可达）。分类一律走
        :func:`~opencode_bridge.adapters.base.classify_http`，而它对
        ``status <= 0`` 的判法就是 :attr:`~opencode_bridge.hooks.SendError.TRANSIENT`。

        ⚠️ **台账：``429`` 的 ``retry_after`` 在这条路径上仍未被处理。**
        ``getMe`` 走 :meth:`_post` 而**不是** :meth:`_api`，所以只有 ``_api``
        里那段"按 ``retry_after`` 睡一次再重试"在这里**没有**。
        ⚠️ 这条**在本次改动之前就存在**，⛔ **不许顺手把它混进来**（那是另一件事、
        该有另一条测试），写在这里是为了让它留在台账上。
        """
        try:
            response = self._post("getMe", {}, timeout=8.0)
        except Exception as exc:
            # ``_post`` 自己会把传输失败包成 ``{"ok": False, "error_code": 0, …}``
            # —— 所以这里的 ``code=0`` 与那条路径**同一个含义**：没拿到 HTTP 状态码
            # （同 ``classify_http`` 对 ``status <= 0`` 的判法）。不是"没有码"。
            return {
                "code": 0,
                "description": str(exc),
                "detail": f"getMe 抛出异常 {type(exc).__name__}: {exc}",
                "raised": True,
            }
        if isinstance(response, dict) and response.get("ok") is True:
            return None
        if isinstance(response, dict):
            code = response.get("error_code")
            description = response.get("description")
        else:
            code = "?"
            description = repr(response)
        return {
            "code": code,
            "description": description,
            "detail": f"getMe: {description or '（平台没给描述）'}",
            "raised": False,
        }

    def _note_permanent_failure_streak_advanced(self) -> None:
        """这一次探测失败了 ⇒ 「同一张凭据连续失败」+1。

        ⚠️ 与 :attr:`_credential_probe_attempts` **不是同一个计数器**：
        后者数"这一轮会话里打过几次 ``getMe``"，而这里数"这张凭据自己连挂了几次"。
        差别在跨轮次：``_credential_probe_attempts`` 会在运行期每一轮重新探测时归零，
        而本计数器必须活着 —— 否则 60s 一次的重连会把同一条墙重新数成第 1 次，
        阈值永远到不了（见 ``telegram_protocol`` 里那个常量的注释）。

        凭据换了（指纹不同）⇒ 另起一条失败链：计数归零、并允许新链再报一次永久失败。
        """
        fingerprint = self._credential_fingerprint()
        if fingerprint != self._credential_fingerprint_of_failure_streak:
            self._credential_fingerprint_of_failure_streak = fingerprint
            self._consecutive_failures_for_same_credential = 0
            self._permanent_failure_announced_for_credential = False
        self._consecutive_failures_for_same_credential += 1

    def _announce_permanent_failure_once(self) -> None:
        """连续失败越过阈值 ⇒ 这条失败链**恰好**打一行 ERROR，然后闭嘴。

        ⚠️ 判据是 :attr:`_permanent_failure_announced_for_credential`，⛔ 不是
        ``count >= threshold`` —— 后者在计数不归零时会让每一轮等待都再打一行，
        故障一小时就是 60 行同样的 ERROR，而那正是这条要修的刷屏。
        唯一的复位点是**探通**（:meth:`_announce_credential_recovery` 那条路径）
        与**换了凭据**（:meth:`_note_permanent_failure_streak_advanced` 另起一条链）。

        文案落在 ``PERMANENT_FAILURE_LOG_MARKER`` 上，与首条失败那两行
        （``getMe failed ... adapter not started``）**不同字面** —— 那两行是
        ``docs/install.md`` / ``plugin/README.md`` 引用的排障入口，
        ⛔ 不许把计数注记混进去。
        """
        if self._permanent_failure_announced_for_credential:
            return
        self._permanent_failure_announced_for_credential = True
        logger.error(
            "telegram: %s（同一张凭据连续 %d 次 getMe 失败）—— 仍会每隔 %.0f 秒"
            "重试一次，不放弃；若这是配置错误，改完 ``bot_token`` 无需重启桥",
            PERMANENT_FAILURE_LOG_MARKER,
            self._consecutive_failures_for_same_credential,
            getattr(
                self._adapter,
                "credential_permanent_failure_cooldown",
                CREDENTIAL_PERMANENT_FAILURE_COOLDOWN,
            ),
        )

    def _permanent_failure_escalation_due(self) -> bool:
        """这次失败是否已经越过阈值 —— 越过了就**再也不要短阶梯**。

        判据是 :attr:`_consecutive_failures_for_same_credential`（同一张凭据的
        连续失败数），⛔ 不是 ``_credential_probe_attempts``：后者在长时间故障里
        会因为每一轮重新探测而反复归零，把它当判据等于"墙还在，但每次都重新开始数"。

        ⚠️ 越过阈值后**不回落**：即使计数继续增长，这一支也恒真 ⇒ 冷却保持固定
        ``CREDENTIAL_PERMANENT_FAILURE_COOLDOWN``，⛔ 不再翻倍。翻倍的终点还是某个
        封顶，而它唯一的效果是让"改完配置之后下一次探测"来得更晚 —— 那正是要修的。

        永久失败那**一行日志**由 :meth:`_note_permanent_failure_streak_advanced`
        的调用方判断是否首次越过；这里只回答"用哪条延迟"。
        """
        return (
            self._consecutive_failures_for_same_credential
            >= CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD
        )

    def _credential_fingerprint(self) -> str:
        """这张凭据的指纹 —— **只为"换没换"服务**，⛔ 不记录、不落盘、不打日志。

        ⚠️ 刻意用 ``hashlib`` 的摘要，而不是把 token 本身存进这个属性：
        本属性会跟着实例活很久，而 token 是凭据（AGENTS.md §2.2）。
        摘要不足以还原出 token，且这里只需要"两次是不是同一张"这一个比特。
        """
        token_value = str(getattr(self._adapter, "bot_token", "") or "")
        return hashlib.sha256(token_value.encode("utf-8")).hexdigest()

    def _log_credential_failure(self, failure: dict) -> None:
        """一次失败的 ``getMe``：**按分类选日志档位与文案**。

        ⛔ **绝不碰** :attr:`startup_verdict` —— 上报只有 :meth:`start` 那一处
        （理由见 :meth:`start` 的 docstring）。

        ⚠️ **分类只用来选档位与文案，绝不用来门控重试**：两类都重试。
        ``_post`` 把传输失败包成 ``error_code: 0``（= 没拿到 HTTP 状态码），
        于是"超时 / DNS 未就绪 / 代理刚起"这类**瞬时**失败在生产里长成
        :attr:`~opencode_bridge.hooks.SendError.TRANSIENT`；401/404 是
        :attr:`~opencode_bridge.hooks.SendError.FORBIDDEN` /
        :attr:`~opencode_bridge.hooks.SendError.NOT_FOUND`。**要不要设放弃点**
        取决于"用户会不会去改配置"，而他不会（改配置不重启桥，见
        :meth:`_probe_until_credentials_verified`）⇒ 不设。
        """
        code = failure["code"]
        description = failure["description"]
        # ``code`` 可能是 ``"?"``（载荷不是 dict）—— 那不是数字，别让它炸掉分类。
        try:
            numeric_code = int(code or 0)
        except (TypeError, ValueError):
            numeric_code = 0
        failure_kind = classify_http(numeric_code, str(description or ""))
        detail = description or "（平台没给描述）"
        if self._credential_probe_attempts > 1:
            logger.warning(
                "telegram: getMe 探测第 %d 次尝试仍失败（%s code=%s）：%s —— "
                "将继续按退避重试",
                self._credential_probe_attempts, failure_kind.value, code, detail,
            )
            return
        # ⛔ 下面那两行是**文档引用**的排障入口（``docs/install.md`` /
        # ``plugin/README.md`` 都让用户按这两行字排障），**原文保留**。
        # ⚠️ 句尾的 "adapter not started" 现在只表示「**入站轮询还没开始跑**」，
        # 而**不是**「这个进程再也不会收到消息」—— 凭据闸门会在传输线程里带退避
        # 重试到通过。改成别的字会让已写的排障指引失效，所以那句话照旧留着。
        if failure["raised"]:
            logger.error(
                "telegram: getMe failed (%s); adapter not started", description
            )
        else:
            logger.error(
                "telegram: getMe failed (code=%s): %s; adapter not started",
                code,
                description,
            )
        credential_problem = failure_kind in (
            SendError.FORBIDDEN,
            SendError.NOT_FOUND,
        )
        logger.error(
            "telegram: getMe 探测未通过（第 1 次尝试；%s code=%s）：%s —— %s；"
            "将按退避重试（初值 %.0fs、×2、封顶 %.0fs、**永不放弃**）",
            failure_kind.value, code, detail,
            (
                "请检查/更新 config.json 里的 bot_token（@BotFather 重新签发）；"
                "改完不必重启桥，本进程会自动重新探测"
                if credential_problem else
                "先查网络/代理/DNS；恢复后不必重启桥，本进程会自动开始收消息"
            ),
            self._credential_probe_initial_backoff(),
            self._credential_probe_max_backoff(),
        )

    def _announce_credential_recovery(self) -> None:
        """恢复必须**响亮**地落在日志里：带「第 N 次尝试 / 历时 T 秒」。

        ⚛️ 只有 ``N >= 2``（**真的失败过**）才打：一次就通过不是"恢复"，
        打出来只是把事故那一段淹没。

        ⚠️ 档位取 **WARNING** 而不是 INFO：默认档位是 INFO，两档都看得见；
        但**事故的两端（失败 / 恢复）落在同一档位上**才搜得到 —— 用户在故障那
        段时间最常做的动作是把档位提到 WARNING 来抓现场，而恢复行若在 INFO，
        那段日志里就只剩"还在坏"的印象。⇒ 这一行属于**这条既有排障路径**
        （搜 ``getMe``）的一部分，⛔ 不许降成 DEBUG。

        这行同时是**标定阶梯初值**的数据源（需要的数见
        :data:`CREDENTIAL_PROBE_INITIAL_BACKOFF` 上面的注释）。
        """
        # ⛔ 归零必须**在** ``attempts <= 1`` 那道早退**之前** —— 否则
        # 「第一次就探通」（``attempts == 1``，最常走的那条）会从下面出去，
        # 而它恰恰是最该把上一条失败链抹掉的那种恢复。
        self._consecutive_failures_for_same_credential = 0
        self._permanent_failure_announced_for_credential = False
        self._credential_fingerprint_of_failure_streak = ""
        attempts = self._credential_probe_attempts
        if attempts <= 1:
            return
        elapsed = max(0.0, time.monotonic() - self._credential_probe_started_at)
        logger.warning(
            "telegram: getMe 探测恢复：第 %d 次尝试 / 历时 %.1f 秒 —— 入站轮询开始",
            attempts, elapsed,
        )
