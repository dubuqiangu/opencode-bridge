"""Lane B — Telegram Bot API adapter (CONTRACT.md §2.2). Standard library only.

HTTP 是裸 ``http.client.HTTPSConnection`` + JSON 请求体，所有调用都收敛在单个
可覆写方法 ``_post`` 后面，测试只需替换它即可离线验证协议层逻辑。

A1：轮询循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.PollingTransport`。本文件只留 Telegram 语义：
``getUpdates`` 的 offset 游标、事件过滤、授权闸门、callback query 与按钮、
429 ``retry_after``、出站分片。

``conversation_id`` 前缀：已从 ``chat:`` 切到 ``telegram:``
-------------------------------------------------------
:meth:`TelegramAdapter._conversation_id` 现在走
``identity.format_id("telegram", ...)``，产出统一格式 ``platform:local_id``。

切换的前置条件（旧前缀期间一直挂着的那条警告）已经满足：
:class:`~opencode_bridge.state.StateStore` 的键迁移
（旧键 → 新键的显式重写 + 版本门控）已上线，且 ``__main__`` **打开了**
``migrate_keys=True``。这两件事**必须同一个变更**：只切前缀而不开迁移，
``state.json`` 里所有 ``chat:`` 键会一次性变成孤儿，用户会在升级后一次性
"忘记"所有历史会话映射 —— 不报错，只表现为"agent 突然记错上下文"。

⚠️ **反向解析仍认旧前缀**（见 :attr:`TelegramAdapter._CONVERSATION_PREFIXES`）：
写前收件箱把 ``conversation_id`` **持久化**在 SQLite 里，升级前写入、
升级后才重放的未投递消息带着 ``chat:`` 前缀。认不出来就等于把那些回复永久丢弃。
``state.py`` 的"精确键 → 无歧义别名"回退是同一类问题的另一半，两者都要留到
slack / discord / mattermost 也切完之后才谈得上删。

Telegram 特有的四点
------------------
1. **offset 游标**：``getUpdates`` 的 ``offset`` 是"确认到此为止"的凭据，服务端
   **只返回 ``update_id >= offset`` 的 update**（服务端侧去重，重复确认不会重复发）。
   它保存在 :attr:`TelegramAdapter._offset` 上，跨多次调用存活。
   **顺序铁律：先推进 offset，再分发** —— 否则分发里单条 update 抛异常就会让这批
   update 被无限重放（这是 Telegram 迁移里最容易搞错的一条）。

2. **两套过滤，别搞混**：``getUpdates`` 请求里的 ``allowed_updates=["message",
   "callback_query"]`` 是**服务端侧**订阅范围（"我不想收别的"）；
   :meth:`TelegramAdapter._dispatch_update` 里的 ``edited_message`` /
   ``channel_post`` / 非文本 / 白名单判定是**客户端侧**入站过滤
   （"收到了也要丢"）。两者是**不同机制**，迁移不许混为一谈 ——
   服务端不推 ≠ 客户端不需要过滤（白名单与"非文本"必须在客户端拦）。

3. **按钮是本平台独有的能力**（``supports_inline_buttons = True``，仓库里唯一）：
   callback query 的接收 → :meth:`TelegramAdapter.answer` 应答是一整套顺序敏感的
   动作（见 :meth:`TelegramAdapter._handle_callback`：投递 inbound → ``on_callback``
   → ``answerCallbackQuery``，且后两步在 ``finally`` 里，**投递崩了也要应答**）。
   这些**全部留在适配器**，传输层只搬走"怎么持续拿到 update"。

4. **长轮询的两层超时**：``getUpdates`` 的 ``timeout`` 是**服务端挂起时长**
   （Bot API 上限 50s），HTTP socket 超时必须**大于**它，否则每轮都会在服务端返回前
   被本地掐断。迁移前取 ``poll_timeout + 15``（默认 25 + 15 = 40s，
   :data:`POLL_SOCKET_TIMEOUT`），本轮逐字保留 —— 有专门的用例锁住这个大小关系。

5. **凭据闸门：``getMe`` 探测是「会话建立」的一部分，而不是 ``start()`` 里的
   终局闸门**（见 :meth:`TelegramAdapter._probe_until_credentials_verified`）。
   其余 12 个适配器的 ``start()`` 里**没有任何凭据探测** —— 它们的鉴权在
   ``Transport._open`` 之后的握手里做，**抛异常 = 这次会话失败 → 退避重连**。
   ⚠️ 改之前的实现是 13 个里唯一一道**同步的、终局性**的凭据闸门：
   ``getMe`` 一失败就 ``return``、``self._transport`` 恒为 ``None`` ⇒
   **入站 100% 死掉**，而生产里**没有任何重试入口**（``adapter.start()`` 只有
   ``core.BridgeCore.start`` 一处调用点，且被 ``_started`` 守着）⇒ 一次超时 /
   一次 ``code=0``（笔记本睡眠唤醒、代理刚起、DNS 未就绪）就把入站**永久**掐到进程重启。
   ⇒ 现在闸门跑在**传输层那条已经存在的线程**里，带退避重试到通过为止。
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from typing import Any, List, Optional

from .. import health
from ..hooks import Button, Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text  # 统一分片实现（T1.4b），此处再导出保持向后兼容
from ..transport import NOTHING, PollingTransport, ReconnectNow
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.telegram")

__all__ = ["TelegramAdapter", "split_text"]

API_HOST = "api.telegram.org"
API_PORT = 443
MESSAGE_LIMIT = 4096          # Telegram max message length in characters
CALLBACK_DATA_LIMIT = 64      # inline callback_data limit in bytes
MIN_SEND_INTERVAL = 1.2       # per-conversation send/edit throttle (seconds)
BACKOFF_INTERVAL = 2.0        # 一次 getUpdates 失败后的重试间隔（秒，恒定）
POLL_LONG_TIMEOUT = 25        # getUpdates long-poll seconds
POLL_SOCKET_TIMEOUT = 40.0    # socket timeout must be > POLL_LONG_TIMEOUT
#: 空轮（``ok`` 但 ``result`` 为空）后的防御性节流（秒）。
#: 迁移前是 :meth:`TelegramAdapter._poll_once` 末尾的 ``_stop_event.wait(0.05)``：
#: 服务端立刻返回空批时不做任何等待会打爆 API。走传输层后它就是
#: ``PollingTransport`` 的 ``idle_sleep``（仍然是**可被 stop 打断**的
#: ``Event.wait``，不是 ``time.sleep``）。
EMPTY_ROUND_INTERVAL = 0.05
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0        # cap for Telegram 429 retry_after sleeps

# ---------------------------------------------------------------------------
# 「凭据闸门」（``getMe`` 探测）的退避阶梯
# ---------------------------------------------------------------------------
# ⚠️ **初值是候选值，不是标定出来的**：它**复用**本仓库已有的 2s
# （:data:`BACKOFF_INTERVAL`，也正是 ``PollingTransport`` 在 ``min_backoff ==
# max_backoff`` 下唯一会取的那个数），**没有**任何实测数据支撑。
# ⇒ **标定它需要哪一组数**：**恢复时延的分布** —— 首次成功时打的那行
# 「第 N 次尝试 / 历时 T 秒」（:meth:`TelegramAdapter._announce_credential_recovery`）
# 就是为了攒这组数：攒够 **≥20 个真实事件**、其中**至少 3 次**是「失败超过 5 分钟
# 才恢复」，再看 N 与 T 的 **p50 / p90**，再回头改这两个数。
# ⛔ 在那之前**不许**把它当成"测过了"。
# ⚠️ 「无抖动」同理是**借用**既有做法而不是结论：本仓库 5 套阶梯**一套都没有**
# 抖动，且 Telegram 的限流按 bot token 计、多实例之间不共享配额。
CREDENTIAL_PROBE_INITIAL_BACKOFF = 2.0
#: 封顶 60s = 传输层的默认值（``Transport.__init__`` 的 ``max_backoff``），
#: 也是 5 个适配器的既有取值 ⇒ 「永不放弃」的代价有界：最坏每分钟一次 ``getMe``。
CREDENTIAL_PROBE_MAX_BACKOFF = 60.0


def _retry_after(data: Any) -> Optional[float]:
    """Extract a 429 ``retry_after`` delay from a Telegram response body."""
    if not isinstance(data, dict):
        return None
    params = data.get("parameters")
    delay: Any = None
    if isinstance(params, dict):
        delay = params.get("retry_after")
    if delay is None and data.get("error_code") == 429:
        delay = 1
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return None
    if delay < 0:
        return None
    return min(float(delay), MAX_RETRY_AFTER)


@register("telegram")
class TelegramAdapter(Adapter):
    """Long-polling ``getUpdates`` Telegram adapter。

    线程与退避归 :class:`~opencode_bridge.transport.PollingTransport`；
    :attr:`running` / :meth:`stop` 是它的代理。

    ``_conversation_id`` 产出统一格式 ``telegram:<chat_id>``（已从 ``chat:`` 切过来），
    ``_chat_id`` 仍认旧前缀 —— 见模块 docstring「``conversation_id`` 前缀」一节。
    """

    name = "telegram"
    label = "Telegram"
    max_message_length = MESSAGE_LIMIT          # Bot API: 4096 字符
    supports_inbound = True
    #: principal = chat id：(a) 会话唯一且稳定，(b) 用户在 Telegram 里能直接看到它，
    #: (c) 平台对发件人做过认证 —— 三条判据都成立。
    pairing_supported = True
    supports_inline_buttons = True              # 仓库里唯一支持 inline 按钮的平台
    supports_media = True
    #: ``editMessageText`` 真能把占位气泡顶成最终答复，所以 ``⏳ 处理中…`` 发得。
    supports_message_edit = True

    # Class-level knobs (tests may override them on the instance).
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL
    #: 「凭据闸门没过时的重试阶梯」。⛔ **与 :attr:`backoff_interval` 是两件事**：
    #: 后者是 ``getUpdates`` 失败后的间隔（迁移前逐字保留的恒定 2s），前者是
    #: ``getMe`` 探测阶梯的**初值**。测试要缩短阶梯时覆盖这两个**实例**属性，
    #: 绝不能靠改另一个来"顺带"生效 —— 那是让两个语义不同的旋钮共用一个名字。
    credential_probe_initial_backoff = CREDENTIAL_PROBE_INITIAL_BACKOFF
    credential_probe_max_backoff = CREDENTIAL_PROBE_MAX_BACKOFF

    #: ``conversation_id`` 的合法前缀：当前格式 + 切换前的旧别名（``chat:``）。
    #: :meth:`_chat_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``chat:``，认不出来就等于把那些回复永久丢弃。
    #: 与 :data:`identity.LEGACY_PREFIXES` 同源（``chat`` → ``telegram``）。
    _CONVERSATION_PREFIXES = ("telegram:", "chat:")

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.poll_long_timeout = self._coerce_poll_timeout()
        self._offset = 0
        self._api_lock = threading.Lock()          # serializes _post calls
        self._throttle_lock = threading.Lock()     # guards _last_send
        self._last_send: dict[str, float] = {}
        self._transport: Optional[PollingTransport] = None
        #: 凭据闸门**已通过**（见 :meth:`_probe_until_credentials_verified`）。
        #: :attr:`running` 与它取与 —— 线程活着但凭据没过**不算**在跑
        #: （那正是入站一条都收不到的状态，报成"在跑"是假话）。
        self._credentials_verified = False
        #: 这一轮探测已尝试了几次（``start()`` 里那次同步探测**算第 1 次**）。
        self._credential_probe_attempts = 0
        #: 这一轮探测的**起点**（monotonic）—— 恢复行里的「历时 T 秒」用它算。
        self._credential_probe_started_at = 0.0
        #: 积压历史**已经丢弃过**。⛔ 每进程只该丢一次，理由见
        #: :meth:`_flush_history_once`。
        self._history_flushed = False
        #: 已取到、还没分发的 update 批次（getUpdates 一次最多回 100 条，
        #: 而传输层的 fetch 一次只交**一条**，所以批量挂在这里逐条取）。
        #: 只被消费线程读写（测试里由 :meth:`_poll_once` 单线程读写）。
        self._pending: list[dict] = []

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _coerce_poll_timeout(self) -> int:
        """``poll_timeout``（getUpdates 长轮询秒数）的取值。

        **没配** -> :data:`POLL_LONG_TIMEOUT`（静默）；**配了但非法** ->
        :data:`POLL_LONG_TIMEOUT` 并**告警**。纪律照抄本仓库既有的两处同款：
        :func:`opencode_bridge.adapters.a2a._coerce_positive`（"说清是哪个键、
        收到了什么、回落成多少"）与
        :meth:`opencode_bridge.adapters.nextcloud.NextcloudAdapter._config_int_value`
        （"配置非法 %r，按 %s 处理"）。⚠️ 不替用户决定成别的值：非正数也回落，
        而**不**静默采纳 —— 一个写错的长轮询时长会让 socket 超时的大小关系
        （:data:`POLL_SOCKET_TIMEOUT` 必须大于它）失效。

        ⚠️ **这里绝对不许让 ``int()`` 的异常逃出去**：``base.build`` 会把它包成
        :class:`~opencode_bridge.adapters.base.AdapterError`，``__main`` 那个循环
        ``continue`` 掉这个适配器 ⇒ 整个桥 ``usable == 0``，而用户看到的报错是
        「没有任何可用适配器」—— **一个字都不提 ``poll_timeout`` 非法**。
        也就是说，一个旋钮写错会打死这条零容错关键路径，而真正的死因不在错误
        信息里、只在日志的一行 ``failed to build adapter`` 里。
        """
        raw = self.config.get("poll_timeout")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return POLL_LONG_TIMEOUT
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "telegram: 配置项 poll_timeout=%r 不是整数（%s），已回落为 %d",
                raw, type(raw).__name__, POLL_LONG_TIMEOUT,
            )
            return POLL_LONG_TIMEOUT
        if seconds <= 0:
            logger.warning(
                "telegram: 配置项 poll_timeout=%r 非法（非正数），已回落为 %d",
                raw, POLL_LONG_TIMEOUT,
            )
            return POLL_LONG_TIMEOUT
        return seconds

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _post(
        self, method: str, payload: Optional[dict] = None, *, timeout: Optional[float] = None
    ) -> dict:
        """POST ``https://api.telegram.org/bot<token>/<method>`` (JSON body).

        Always returns a dict; network / decode failures are mapped to
        ``{"ok": False, "error_code": ..., "description": ...}`` so callers
        never see an exception from this layer unless they want to.
        """
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        path = f"/bot{self.bot_token}/{method}"
        sock_timeout = timeout if timeout is not None else DEFAULT_SOCKET_TIMEOUT
        status = 0
        raw = b""
        conn = http.client.HTTPSConnection(API_HOST, API_PORT, timeout=sock_timeout)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read()
        except Exception as exc:
            return {
                "ok": False,
                "error_code": status or 0,
                "description": f"transport error: {exc}",
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {
                "ok": False,
                "error_code": status,
                "description": f"non-JSON response (HTTP {status}): {raw[:200]!r}",
            }
        if not isinstance(data, dict):
            return {"ok": False, "error_code": status, "description": "unexpected payload"}
        if "error_code" not in data and status >= 400:
            data["error_code"] = status
        return data

    def _api(
        self, conversation_id: str, method: str, payload: dict, *, timeout: Optional[float] = None
    ) -> dict:
        """Throttled + 429-retried wrapper around ``_post``.

        Returns the final response dict; on transport failure returns
        ``{"ok": False}``. Never raises.
        """
        self._throttle(conversation_id)
        try:
            with self._api_lock:
                data = self._post(method, payload, timeout=timeout)
        except Exception:
            logger.exception("telegram: %s request failed", method)
            return {"ok": False, "error_code": 0, "description": "transport error"}
        if isinstance(data, dict) and data.get("ok") is False:
            delay = _retry_after(data)
            if delay is not None:
                logger.warning(
                    "telegram: %s rate-limited, sleeping %.1fs and retrying once",
                    method,
                    delay,
                )
                self._mark_backoff(conversation_id)
                time.sleep(delay)
                try:
                    with self._api_lock:
                        data = self._post(method, payload, timeout=timeout)
                except Exception:
                    logger.exception("telegram: %s retry failed", method)
                    return {"ok": False, "error_code": 0, "description": "transport error"}
        # No generic failure logging here: callers decide which errors are
        # silent (e.g. editMessageText "message is not modified").
        return data if isinstance(data, dict) else {"ok": False}

    def _throttle(self, conversation_id: str) -> None:
        """Enforce a minimum interval between send/edit per conversation."""
        interval = getattr(self, "min_interval", MIN_SEND_INTERVAL)
        while True:
            with self._throttle_lock:
                last = self._last_send.get(conversation_id)
                now = time.monotonic()
                if last is None or (now - last) >= interval:
                    self._last_send[conversation_id] = now
                    return
                wait = interval - (now - last)
            if self._stop_event.wait(wait):
                return

    def _mark_backoff(self, conversation_id: str) -> None:
        """After a 429, push the next allowed send further out (2s)."""
        backoff = getattr(self, "backoff_interval", BACKOFF_INTERVAL)
        with self._throttle_lock:
            self._last_send[conversation_id] = time.monotonic() + backoff - self.min_interval

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[PollingTransport]:
        """当前传输层（``start()`` 之后才有）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """轮询线程是否活着**且凭据闸门已过**（线程与退避归传输层）。

        ⚠️ **两个条件缺一不可**：``getMe`` 没过时消费线程是活的，但它停在
        :meth:`_credential_gate` 里、**一条 update 都不会分发** ⇒ 报成 ``True``
        就是对着「入站一条都收不到」说「在跑」。
        （生产里这个键唯一的消费者是 ``capabilities()["running"]``，而
        ``--status`` / ``--setup --json`` 是**重新构造一个全新适配器**去读它的，
        那一刻还没 ``start()`` ⇒ 那个值结构上恒为 ``False``。）
        """
        transport = self._transport
        return (
            transport is not None
            and transport.running
            and self._credentials_verified
        )

    def _make_transport(self) -> PollingTransport:
        """构造本次运行用的传输层（测试注入点：退避接线值）。

        ⚠️ ``on_open=self._credential_gate`` 就是「探测成为**会话建立的一部分**」
        那个接缝：钩子在 ``PollingTransport._open`` 里被调，而 ``_open`` 抛异常
        本来就等于"这次会话失败 → 退避重连"。我们让闸门自己在里面 park 到通过
        （阶梯见 :meth:`_probe_until_credentials_verified`），所以传输层的退避
        **一次也不会**在凭据这条路上被触发。
        """
        # 三处间隔**逐字对齐迁移前**的 ``_poll_loop`` / ``_poll_once``：
        #   * min_backoff —— getUpdates 调用**失败**（``ok`` 不为 True）后的重试间隔。
        #     迁移前是 ``self._stop_event.wait(2.0)``：一个字面量 2s，
        #     **没有指数增长**。所以 max_backoff 必须与 min 同值，
        #     否则会静默变成"2s → 4s → 8s…"的指数退避（= 行为变更）。
        #   * max_backoff —— 与 min 相同 ⇒ 退避**恒定**。
        #   * idle_sleep —— **空轮**（成功但 result 为空）后的 0.05s 防御性节流，
        #     迁移前在 ``_poll_once`` 末尾（同样是用 Event.wait，可被 stop 打断）。
        # reset_after=0 ⇒ 只要 fetch 成功过一次就重置退避（连上即重置 = 既有语义）。
        # ⚠️ **凭据阶梯绝不落在这个实例上**：min == max ⇒ 退避恒定，而
        # ``Transport._next_backoff`` 的 ``survived`` 由**调用点**判
        # （``lived >= reset_after``），轮询类 ``_open()`` 永远成功 ⇒
        # ``lived >= 0`` 恒真 ⇒ 永远取下限 ⇒ **min == max 时任何配置都升级不了退避**。
        backoff = float(self.backoff_interval)
        return PollingTransport(
            self._fetch_update,
            on_open=self._credential_gate,
            idle_sleep=EMPTY_ROUND_INTERVAL,
            name="telegram",
            min_backoff=backoff,
            max_backoff=backoff,
            reset_after=0.0,
        )

    def start(self) -> None:
        """Probe the token, then spawn the poller（**永不因探测失败而不启动**）。

        Missing token: log a warning and return (never raises) —— 那是**唯一**
        一条真的没有凭据可试、也就无从重试的分支（``bot_token`` 要用户去改，而
        改配置不会重启桥，所以这里既没意义也没必要 park）。

        ⚠️ **每个结局都调 :meth:`~opencode_bridge.adapters.base.Adapter.report_startup_probe`
        且**只在这一次调** —— ``getMe`` 失败曾经只留一行日志、适配器直接 ``return``，
        而 ``--status`` / ``--setup --json`` 只看 token 字符串非空就报"已配置 / 入站就绪"
        ⇒ **token 打错、被吊销、或网络被墙时，桥完全静默而状态视图说一切正常**。
        现在结论被 :mod:`opencode_bridge.health` 落盘并由那两个视图读出来。
        ⚠️ **传输线程里的重试不再改它** —— 运行器（``core.BridgeCore.start``）
        在 ``start()`` 返回**那一刻**同步读走 :attr:`startup_verdict`；让线程去改它
        会让落盘的值取决于线程调度（"上一次成功"与"最近一次失败"在那一刻不可分）。
        恢复的事实由**日志**承担，见 :meth:`_announce_credential_recovery`。

        ⚠️ 本方法里那两行 ``telegram: getMe failed ...`` **原文保留**：用户与文档
        （``docs/install.md`` / ``plugin/README.md``）都按这行字排障，改了会让
        已写的排障指引失效。所以基类那条规范化日志**不取代**它，两条并存。
        """
        if not self.bot_token:
            logger.warning("telegram: bot_token missing; adapter not started")
            self.report_startup_probe(
                health.VERDICT_SKIPPED, detail="bot_token 没填，连 token 都没得验"
            )
            return
        # 下面这一次**同步**探测是第 1 次尝试（计数从 1 起，恢复行的 N 与 T 才诚实）。
        # ⚠️ 代价：它仍是最长 8s 的同步等待 —— 与改动之前**一样**，不是新引入的；
        # 而它换来的是 ``startup_verdict`` 在 ``start()`` 返回时就已确定（见上一段）。
        self._credential_probe_started_at = time.monotonic()
        self._credential_probe_attempts = 1
        failure = self._probe_get_me_once()
        if failure is None:
            self.report_startup_probe(health.VERDICT_OK, detail="getMe 通过")
            self._flush_history_once()
            # ⚠️ 必须在这里置位：否则闸门会在传输线程里**再打一次** ``getMe``
            # （``_open`` → ``on_open`` → 闸门，而闸门只看这个标志）。
            # 那一次不是"多探一次"那么无害：正常启动每次都多一次 ``getMe``，
            # 而限流按 bot token 计 —— 且「一次就通过时零重试」这条反向护栏
            # 会因此变成假的。
            self._credentials_verified = True
        else:
            # 这一条分支同时覆盖两种失败：平台明确回的 API 错误
            # （``ok:false`` + error_code/description），以及 ``_post`` 把传输层
            # 异常包成的 ``error_code: 0`` —— 两者都要落盘，否则"网络被墙"
            # 这种最常见的失败恰恰不在状态视图里。
            self.report_startup_probe(
                health.VERDICT_FAILED, code=failure["code"], detail=failure["detail"]
            )
            self._log_credential_failure(failure)
        # ⚠️ **无论上面成没成都起传输层**：探测的重试循环就跑在它那条线程里
        # （见 ``_make_transport`` 的 on_open 注释）⇒ 失败时入站仍能自愈。
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_update)
        logger.info("telegram: polling started (offset=%s)", self._offset)

    # ------------------------------------------------------------------
    # 凭据闸门：``getMe`` 探测 + 退避重试（**不新增线程**）
    # ------------------------------------------------------------------
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
        """
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
            # ``attempts - 1`` = 这是第几次**等待**（线程里第一次失败是第 1 次等待
            # ⇒ 拿初值）。⚠️ 直接传 ``attempts`` 会让**初值那一档永远用不上**
            # （同步那次是第 1 次尝试、它不等），阶梯就变成"4s 起步" —— 而
            # 「初值 2s → ×2」这句话会被读成一句假话。
            if self._stop_event.wait(
                self._credential_probe_backoff_for(self._credential_probe_attempts - 1)
            ):
                raise ReconnectNow("getMe 探测已中止（stop() 已请求）")

    def _credential_probe_initial_backoff(self) -> float:
        """阶梯初值（秒）。夹到非负。"""
        return max(
            0.0,
            float(getattr(
                self, "credential_probe_initial_backoff",
                CREDENTIAL_PROBE_INITIAL_BACKOFF,
            )),
        )

    def _credential_probe_max_backoff(self) -> float:
        """阶梯封顶（秒）。⛔ 不许小于初值（那会让"封顶"把初值也改掉）。"""
        return max(
            self._credential_probe_initial_backoff(),
            float(getattr(
                self, "credential_probe_max_backoff",
                CREDENTIAL_PROBE_MAX_BACKOFF,
            )),
        )

    def _credential_probe_backoff_for(self, wait_number: int) -> float:
        """第 ``wait_number`` 次**等待**要等的秒数。**纯函数**：不睡眠、不碰线程。

        ⛔ 形参是「第几次**等待**」而不是「第几次**尝试**」：这两者差一，
        而差一就等于**初值那一档永远用不上**（同步那次探测之后是直接交棒给线程、
        不等），阶梯会变成"4s 起步"，让「初值 2s → ×2」这句话变成一句假话。

        阶梯 = 初值 → ``×2`` → 封顶，**无抖动**、**没有放弃点**。
        指数的来源是 ``transport/base.py`` 的基类不变式（那里也是
        ``min`` → ``×2`` → ``max``），而"无抖动"是借用本仓库既有做法
        （5 套阶梯一套都没有抖动；理由见那两个常量上面的注释）。

        ⚠️ 指数用 ``min(wait_number - 1, 30)`` 夹住：``wait_number`` 可以任意大
        （永不放弃 ⇒ 跑几个月就有几百万次），而 ``2.0 ** 3_000_000`` 会
        ``OverflowError`` —— 那是"永不放弃"这条设计**自己**引入的新失败模式。
        30 次 ``×2`` 早已越过任何合理封顶，所以夹它不改变答案。
        """
        initial = self._credential_probe_initial_backoff()
        ceiling = self._credential_probe_max_backoff()
        exponent = min(max(1, int(wait_number)) - 1, 30)
        return min(initial * (2.0 ** exponent), ceiling)

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
        attempts = self._credential_probe_attempts
        if attempts <= 1:
            return
        elapsed = max(0.0, time.monotonic() - self._credential_probe_started_at)
        logger.warning(
            "telegram: getMe 探测恢复：第 %d 次尝试 / 历时 %.1f 秒 —— 入站轮询开始",
            attempts, elapsed,
        )

    def _flush_history_once(self) -> None:
        """丢弃积压历史 —— ⛔ **每进程只跑一次**，不是每会话一次。

        :meth:`_flush_pending` 把 :attr:`_offset` 推到最后一个 ``update_id + 1``，
        而 Telegram 的 ``offset`` 语义是**确认到此为止** ⇒ 它的语义是
        **永久丢弃历史**。

        ⚠️ **正因为探测变成了"会话建立的一部分"，这里才必须显式加一次性闸门**：
        很容易顺手把它也搬进会话 ⇒ 网络抖一下就吃掉断线期间到达的消息，
        **而且不报错**。所以 :attr:`_history_flushed` 先置位再干活（置位在前：
        万一 ``_flush_pending`` 抛异常，也不许下一次再来一次）。
        """
        if self._history_flushed:
            return
        self._history_flushed = True
        try:
            self._flush_pending()
        except Exception:
            logger.exception("telegram: failed to flush pending updates")
        # _flush_pending 刚把历史全丢了 ⇒ 手上没分发的旧 update 也不能再投。
        self._pending.clear()

    def stop(self) -> None:
        """置停止位 → 关传输层 → join（**幂等**）。

        迁移前是 ``Adapter.stop()`` 置位 + join 5s；退避等待与空轮节流都用
        ``_stop_event.wait(...)``，所以都能被立刻打断。现在这两段都在
        :class:`~opencode_bridge.transport.PollingTransport` 里，语义不变。

        ⚠️ 已知限制（迁移前就有，不是本次引入的）：``stop()`` **打不断**正在挂起的
        ``getUpdates`` HTTP 请求（``http.client`` 没有可关的句柄）—— 最长要等
        ``poll_timeout + 15``（默认 40s）那一轮自己返回，join 会在 5s 处超时返回，
        线程（daemon）随后自然退出。至少不会泄漏线程。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    def _flush_pending(self) -> None:
        """Drop history: read the last update_id and skip everything before it."""
        data = self._post(
            "getUpdates", {"limit": 1, "offset": -1}, timeout=DEFAULT_SOCKET_TIMEOUT
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            logger.warning(
                "telegram: could not flush pending updates: %s",
                (data or {}).get("description"),
            )
            return
        results = data.get("result") or []
        if results:
            try:
                self._offset = int(results[-1].get("update_id")) + 1
            except (TypeError, ValueError):
                logger.warning("telegram: unexpected flush payload: %r", results[-1])

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _fetch_update(self) -> Any:
        """传输层的 fetch：**一条** update；这轮没有就返回 :data:`NOTHING`。

        ``getUpdates`` 一次最多回 100 条，而传输层的 fetch 一次只交**一条**，
        所以批量先挂进 :attr:`_pending` 再逐条取（与迁移前"一批处理完再发下一轮"
        顺序一致）。``_pending`` 空了才发下一轮请求。

        两条退出路径对应迁移前 ``_poll_loop`` 的两种等待，**不能混**：

        * 空轮（``ok`` 但没有 update）→ :data:`NOTHING` → 传输层按
          ``idle_sleep``（:data:`EMPTY_ROUND_INTERVAL` = 0.05s）节流后重问；
        * 请求失败 → :meth:`_poll_round` 抛异常 → 传输层按
          ``min_backoff``（:data:`BACKOFF_INTERVAL` = 2s）退避后重连。
        """
        if not self._pending:
            self._poll_round()
        if not self._pending:
            return NOTHING
        return self._pending.pop(0)

    def _poll_round(self) -> None:
        """一轮 ``getUpdates``：把 ``result`` 装进 :attr:`_pending`。失败抛异常。

        **抛异常 = 传输层故障**（基类会关连接、退避、从头再来），这与迁移前
        ``_poll_loop`` 里 ``_poll_once()`` 返回 False 后 ``wait(2.0)`` 是同一件事，
        只是把"等多久"交给传输层的退避状态机。

        offset 一字不动：失败轮不会推进任何游标（**铁律**）。
        """
        payload = {
            "timeout": self.poll_long_timeout,
            "offset": self._offset,
            # 服务端侧订阅范围（不是入站过滤）：只要 message + callback_query。
            "allowed_updates": ["message", "callback_query"],
            "limit": 100,
        }
        # socket 超时必须 > 服务端挂起时长（poll_timeout），否则会在服务端
        # 返回前先被本地掐断。迁移前是 ``self.poll_long_timeout + 15.0``（默认
        # 25 + 15 = 40s = POLL_SOCKET_TIMEOUT），逐字保留。
        data = self._post(
            "getUpdates", payload, timeout=self.poll_long_timeout + 15.0
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = (data or {}).get("error_code") if isinstance(data, dict) else None
            desc = (data or {}).get("description") if isinstance(data, dict) else None
            logger.warning(
                "telegram: getUpdates failed (code=%s): %s", code, desc
            )
            raise RuntimeError(f"getUpdates failed (code={code}): {desc}")
        # 非 dict 元素直接丢掉（迁移前循环里的 ``continue``）。
        self._pending = [u for u in (data.get("result") or []) if isinstance(u, dict)]

    def _on_update(self, update: Any) -> None:
        """传输层交给我们的**一条** update。

        **顺序铁律：先推进 offset，再分发** —— 这样分发里单条处理抛异常也不会让
        这一批 update 被无限重放（下一轮 ``getUpdates`` 已经带着新的 offset，
        服务端不会把那批再发一遍）。这段 try/except 与迁移前的循环体逐字一致：
        单条失败只记日志、继续处理同批的下一条。
        """
        if not isinstance(update, dict):
            return
        try:
            self._advance_offset(update)
            self._dispatch_update(update)
        except Exception:
            logger.exception("telegram: failed to handle update %r", update)

    def _poll_once(self) -> bool:
        """跑完一轮（请求一批 → 逐条「先推进 offset 再分发」）。返回 True 表示成功。

        这是 :meth:`_fetch_update` 的"永不抛异常"组合形态，供测试与手动诊断
        （拉一次看看 offset 对不对）使用；常驻轮询走传输层。
        """
        try:
            self._poll_round()
        except Exception:
            logger.exception("telegram: poll cycle failed")
            return False                      # 失败：offset 一字不动
        while self._pending:
            self._on_update(self._pending.pop(0))
        return True

    def _advance_offset(self, update: dict) -> None:
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            return
        if update_id + 1 > self._offset:
            self._offset = update_id + 1

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _dispatch_update(self, update: dict) -> None:
        cq = update.get("callback_query")
        if isinstance(cq, dict):
            self._handle_callback(cq)
            return
        # edited_message / channel_post / edited_channel_post have no
        # "message" key here -> they simply fall through and are ignored.
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str) or text == "":
            # Stickers, photos, voice notes, caption-only media: ignored.
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        conversation_id = self._conversation_id(chat.get("id"))
        if not self._allowed(chat.get("id")) and not self.answer_pairing_request(
            chat.get("id"), conversation_id, text
        ):
            logger.info(
                "telegram: dropped message from non-whitelisted chat %s",
                redactable_id(self.name, chat.get("id")),
            )
            return
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id")) if isinstance(from_user, dict) else None
        inbound = Inbound(
            conversation_id=conversation_id,
            text=text,
            kind="text",
            user_id=user_id,
            message_id=str(message.get("message_id"))
            if message.get("message_id") is not None
            else None,
            platform=self.name,
            raw=message,
        )
        self.hooks.on_inbound(inbound)

    def _handle_callback(self, cq: dict) -> None:
        """按钮点击（``callback_query``）—— 本平台**独有**的能力，整体留在适配器。

        顺序敏感，且是**行为契约**：① 投递 inbound → ② ``on_callback`` →
        ③ ``answerCallbackQuery``。后两步在 ``finally`` 里：即使用户回调抛异常、
        即使 ``on_callback`` 抛异常，**也必须**给 Telegram 应答，否则那个转圈圈会
        一直挂在用户界面上（Bot API 认为 query 未被 ack）。
        """
        data = cq.get("data")
        query_id = cq.get("id")
        chat_id = None
        message = cq.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                chat_id = chat.get("id")
        if chat_id is None:
            from_user = cq.get("from")
            if isinstance(from_user, dict):
                chat_id = from_user.get("id")
        if chat_id is None:
            # 只记**载荷的形状**，绝不记**载荷的内容**。
            # `keys=` 是排序后的顶层键名 —— 排障真正要的就是"有没有 message／
            # 有没有 from"，它不含任何用户数据；`from=` 取不到时由 redactable_id
            # 自己落到 MISSING_ID（见 _redactable_ids 的行为 1）。
            # ⛔ 绝不记 data 的值、from.username、first_name/last_name、
            # message.text 或 message 里任何 chat 字段：那是 PII **加上**
            # 用户自己那段正文，而这一行的用途只是解释"为什么没找到 chat"。
            from_user = cq.get("from")
            logger.warning(
                "telegram: callback without chat context: from=%s keys=%s",
                redactable_id(
                    self.name, from_user.get("id") if isinstance(from_user, dict) else None
                ),
                tuple(sorted(cq)) if isinstance(cq, dict) else (),
            )
            return
        conversation_id = self._conversation_id(chat_id)
        if not self._allowed(chat_id) and not self.answer_pairing_request(
            chat_id, conversation_id, None
        ):
            logger.info(
                "telegram: dropped callback from non-whitelisted chat %s",
                redactable_id(self.name, chat_id),
            )
            return
        payload = data if isinstance(data, str) else ""
        # 1) push the event into the core, 2) notify the core, 3) ack Telegram.
        try:
            from_user = cq.get("from")
            user_id = (
                str(from_user.get("id")) if isinstance(from_user, dict) else None
            )
            inbound = Inbound(
                conversation_id=conversation_id,
                text=payload,
                kind="callback",
                user_id=user_id,
                message_id=str(message.get("message_id"))
                if isinstance(message, dict) and message.get("message_id") is not None
                else None,
                callback_query_id=str(query_id) if query_id is not None else None,
                platform=self.name,
                raw=cq,
            )
            try:
                self.hooks.on_inbound(inbound)
            except Exception:
                logger.exception("telegram: on_inbound hook failed for callback")
        finally:
            try:
                if query_id is not None:
                    self.hooks.on_callback(conversation_id, payload, str(query_id))
            except Exception:
                logger.exception("telegram: on_callback hook failed")
            try:
                if query_id is not None:
                    self.answer(str(query_id))
            except Exception:
                logger.exception("telegram: answer failed for query %s", query_id)

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge a callback query (``answerCallbackQuery``). Never raises."""
        if not query_id:
            return
        payload: dict = {"callback_query_id": query_id}
        if text:
            payload["text"] = text
        try:
            data = self._post("answerCallbackQuery", payload, timeout=DEFAULT_SOCKET_TIMEOUT)
        except Exception:
            logger.exception("telegram: answerCallbackQuery failed")
            return
        if isinstance(data, dict) and data.get("ok") is not True:
            logger.debug(
                "telegram: answerCallbackQuery not ok: %s", data.get("description")
            )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(chat_id: Any) -> str:
        """``chat_id`` → ``telegram:...``（统一 ``platform:local_id`` 格式）。"""
        return format_id("telegram", chat_id)

    @staticmethod
    def _chat_id(conversation_id: str) -> Optional[int]:
        """``conversation_id`` → 数字 ``chat_id``；不认得就返回 ``None``。

        裸 ``chat_id``（``"55"``）也认，切换前后的两种前缀都认 ——
        理由见 :attr:`TelegramAdapter._CONVERSATION_PREFIXES`。
        """
        raw = str(conversation_id or "")
        for prefix in TelegramAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _allowed(self, chat_id: Any) -> bool:
        """入站闸门：委托基类统一判定（T1.2，语义与旧实现一致）。"""
        return self.admits(chat_id)

    def send(self, out: Outbound) -> MsgHandle | None:
        """Send text (split at 4096); returns the handle of the LAST chunk."""
        chat_id = self._chat_id(out.conversation_id)
        if chat_id is None:
            logger.warning("telegram: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("telegram: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        # prefix_fmt="" 保持既有出站行为（分段不加「（i/n）」前缀，且
        # "".join(chunks) == text）。前缀编号是 split.py 的可选能力，
        # 是否默认开启见 tasks.md T1.4 的后续决策。
        chunks = split_text(out.text, self.effective_max_length, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "telegram: splitting outbound message into %d chunks", len(chunks)
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            data = self._api(out.conversation_id, "sendMessage", payload)
            if not isinstance(data, dict) or data.get("ok") is not True:
                code = data.get("error_code") if isinstance(data, dict) else 0
                desc = (data.get("description") if isinstance(data, dict) else "") or ""
                logger.warning(
                    "telegram: sendMessage failed (code=%s): %s",
                    code if code is not None else "?",
                    desc or data,
                )
                params = data.get("parameters") if isinstance(data, dict) else None
                retry_after = None
                if isinstance(params, dict):
                    retry_after = params.get("retry_after")
                try:
                    self._note_send_failure(
                        classify_http(int(code or 0), str(desc)),
                        str(desc),
                        retry_after=float(retry_after) if retry_after else None,
                    )
                except (TypeError, ValueError):
                    self._note_send_failure(SendError.UNKNOWN, str(desc))
                if handle is None:
                    return None
                return handle  # partial send: keep the last good handle
            result = data.get("result") or {}
            message_id = result.get("message_id")
            if message_id is None:
                return handle
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(message_id),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a message (with optional single-column inline keyboard).

        Raises ``ValueError`` when the text exceeds 4096 characters — per
        CONTRACT.md §2.2 the caller is responsible for truncating.
        """
        if len(out.text) > self.effective_max_length:
            raise ValueError(
                f"telegram edit text too long: {len(out.text)} > {self.effective_max_length}"
            )
        if not out.text:
            logger.warning("telegram: refusing to edit with empty text")
            return False
        chat_id = self._chat_id(handle.conversation_id)
        if chat_id is None:
            logger.warning(
                "telegram: bad conversation_id %r in handle", handle.conversation_id
            )
            return False
        try:
            message_id = int(handle.message_id)
        except (TypeError, ValueError):
            logger.warning("telegram: bad message_id %r in handle", handle.message_id)
            return False
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": out.text}
        if out.buttons:
            # Single column: one button per row (CONTRACT.md §2.2).
            payload["inline_keyboard"] = [[_button_row(b)] for b in out.buttons]
        data = self._api(handle.conversation_id, "editMessageText", payload)
        if not isinstance(data, dict):
            return False
        if data.get("ok") is True:
            return True
        description = str(data.get("description") or "")
        if "message is not modified" in description:
            return False  # silent: identical content is not an error
        code = data.get("error_code")
        if isinstance(code, int) and 400 <= code < 500:
            logger.warning(
                "telegram: editMessageText rejected (HTTP %s): %s", code, description
            )
        else:
            logger.warning(
                "telegram: editMessageText failed (code=%s): %s", code, description
            )
        return False


def _button_row(button: Button) -> dict:
    """Serialize a Button to a Telegram inline keyboard button (<= 64 bytes)."""
    data = button.data.encode("utf-8")[:CALLBACK_DATA_LIMIT].decode(
        "utf-8", errors="ignore"
    )
    return {"text": button.label, "callback_data": data}
