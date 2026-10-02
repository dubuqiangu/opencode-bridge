"""Lane B — Discord adapter (CONTRACT.md §2.3). Standard library only.

Outbound (``POST /channels/{id}/messages`` / ``PATCH .../{message_id}``) 与
**入站 Gateway v10 WebSocket**（tasks.md T2.2）都可用。入站传输层复用
:mod:`opencode_bridge.ws`（T2.0，纯标准库自研的最小 RFC 6455 客户端），
因此本文件不引入任何第三方依赖。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.discord")

__all__ = ["DiscordAdapter"]

API_BASE = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000         # Discord message content limit
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0

# ----------------------------------------------------------------------
# Gateway v10 入站常量（T2.2）
# ----------------------------------------------------------------------
GATEWAY_API_VERSION = 10           # 连接串固定 ?v=10（REST 侧已由 API_BASE 固定）
GATEWAY_ENCODING = "json"          # 不加 compress=（那需要 zlib-stream 协商）
GATEWAY_MAX_PAYLOAD = 4096         # 单个网关 payload 上限，超了服务端 close 4002
#: recv 超时（秒）。心跳周期实测 41~45s 且每次心跳都有 op 11 往返，正常不会触发；
#: 它只是"Linux 上 close() 不保证唤醒阻塞 recv"的兜底（见 _inbound_loop 注释）。
GATEWAY_RECV_TIMEOUT = 60.0
RECONNECT_MIN = 1.0                # 重连退避下限（秒）
RECONNECT_MAX = 60.0               # 重连退避上限（秒）
HEARTBEAT_DEFAULT = 45.0           # Hello 没给 heartbeat_interval 时的兜底（秒）

#: intents 是**整数 bitmask**（按位或），不是数组。本项目只要消息类 intent：
INTENT_GUILD_MESSAGES = 1 << 9     # 512   服务器内频道消息
INTENT_DIRECT_MESSAGES = 1 << 12   # 4096  私聊
INTENT_MESSAGE_CONTENT = 1 << 15   # 32768 消息正文（不开就收不到 content）
DEFAULT_INTENTS = (
    INTENT_GUILD_MESSAGES | INTENT_DIRECT_MESSAGES | INTENT_MESSAGE_CONTENT
)  # = 37376

#: 我们主动断开时使用的 close code。**不能用 1000/1001**：那样会让 session 失效、
#: bot 在开发者后台显示离线；用 4000 才能保住 session 供 Resume。
GATEWAY_CLOSE_REQUESTED = 4000

#: IS_CROSSPOST：转发消息会在源频道与每个目标频道各触发一次 MESSAGE_CREATE
FLAG_IS_CROSSPOST = 1 << 1

# 网关 opcode（5 号已废弃，不存在）
OP_DISPATCH = 0            # 服务器 → 客户端事件（唯一带 s / t 的 opcode）
OP_HEARTBEAT = 1           # 双向：客户端周期发，服务端可要求立即发
OP_IDENTIFY = 2            # 客户端 → 服务器
OP_RESUME = 6              # 客户端 → 服务器（续用 session，不重新 Identify）
OP_RECONNECT = 7           # 服务器要求重连
OP_INVALID_SESSION = 9     # d=true 可 Resume / d=false 必须重新 Identify
OP_HELLO = 10              # 服务器开场包（带 heartbeat_interval）
OP_HEARTBEAT_ACK = 11      # 心跳确认

#: 这些 close code 是**配置/权限层面的错**，重连一万次也不会好，必须停下并给出
#: 可执行的诊断（官方 Gateway 文档的 close code 表）。
FATAL_CLOSE_CODES = {
    4004: "认证失败：bot_token 无效或已重置",
    4010: "shard 参数非法（本适配器固定 [0] 单分片，正常不该出现）",
    4011: "该连接需要分片：intent 太多，请减少 intent 位",
    4012: "API 版本非法：v10 不被支持",
    4013: "intent 位值非法：intents 算错了（检查 config.intents）",
    4014: "intent 未在开发者后台开启：去 Portal 勾选对应 intent 后重连",
}

# _handle_payload / 收包循环的返回值（动作）
_ACT_CONTINUE = "continue"      # 继续收下一包
_ACT_RECONNECT = "reconnect"    # 重连（尽量 Resume）
_ACT_REIDENTIFY = "reidentify"  # 重连 + 丢弃 session 走 Identify
_ACT_FATAL = "fatal"            # 停止重连


@register("discord")
class DiscordAdapter(Adapter):
    """Discord adapter: REST 出站 + Gateway v10 入站。"""

    name = "discord"
    label = "Discord"
    max_message_length = MESSAGE_LIMIT          # 消息内容上限 2000 字符
    supports_inbound = True                    # Gateway v10 入站（T2.2）
    supports_inline_buttons = False            # components 未实现
    supports_media = False
    required_tokens = ("bot_token",)           # 入站只多要一个 bot_token（默认值）

    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}

        # --- Gateway 入站状态（T2.2）----------------------------------
        # intents 是整数 bitmask：允许配置覆盖（例如只想收私聊），默认三项全开。
        self.intents: int = self._config_intents()
        # gateway_url 允许配置覆盖（自建网关 / 测试）；留空则启动时问 REST 要。
        self._gateway_url: Optional[str] = (
            str(self.config.get("gateway_url") or "").strip() or None
        )
        self._resume_url: Optional[str] = None      # READY 给的 resume_gateway_url
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None        # 最近一次非 null 的 s
        self._my_user_id: Optional[str] = None     # READY.d.user.id（过滤自己）
        self._ack_received: bool = False            # 最近一次心跳是否被 ACK
        self._heartbeat_interval: float = 0.0       # 秒（Hello 给的是毫秒）
        self._ws = None                              # 当前连接，stop() 时关掉
        self._ws_factory = None                      # 测试注入点
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()

    def _config_intents(self) -> int:
        """读 ``intents`` 配置；非法值退回默认 37376 而不是静默发 0。"""
        raw = self.config.get("intents")
        if raw in (None, ""):
            return DEFAULT_INTENTS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("discord: intents 配置非法 %r，改用默认 %d", raw, DEFAULT_INTENTS)
            return DEFAULT_INTENTS
        return value if value > 0 else DEFAULT_INTENTS

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: dict,
        *,
        timeout: float = SOCKET_TIMEOUT,
    ) -> Tuple[int, dict]:
        """Call ``{API_BASE}/{path}``. Never raises; returns (status, body)."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "opencode-bridge (discord, 1.0)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except Exception as exc:
            logger.warning("discord: transport error on %s: %s", path, exc)
            return 0, {"ok": False, "message": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return status, {"ok": False, "message": f"non-JSON response (HTTP {status})"}
        if not isinstance(data, dict):
            return status, {"ok": False, "message": "unexpected payload"}
        if status >= 400 and "code" not in data:
            data.setdefault("ok", False)
        return status, data

    def _throttle(self, conversation_id: str) -> None:
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动入站（Gateway v10 WebSocket）。缺 bot_token 时只警告不起线程。"""
        if not self.bot_token:
            logger.warning("discord: bot_token missing; adapter not started")
            return
        thread = threading.Thread(
            target=self._inbound_loop, name="discord-gateway", daemon=True
        )
        self._thread = thread
        thread.start()

    # ------------------------------------------------------------------
    # Inbound (Gateway v10)
    # ------------------------------------------------------------------
    @property
    def my_user_id(self) -> Optional[str]:
        """READY 里拿到的 bot 自身 user id（过滤自己消息的唯一可靠判据）。"""
        return self._my_user_id

    @property
    def session_id(self) -> Optional[str]:
        """当前会话 id（Resume 用）。"""
        return self._session_id

    @property
    def last_seq(self) -> Optional[int]:
        """最近一次收到的非 null ``s``（心跳与 Resume 都复用它）。"""
        return self._last_seq

    @property
    def ack_received(self) -> bool:
        """最近一次心跳是否已收到 op 11 ACK。"""
        return self._ack_received

    @property
    def gateway_url(self) -> Optional[str]:
        """缓存的网关 URL（``GET /gateway/bot`` 返回的原始值，未取到则 ``None``）。"""
        return self._gateway_url

    def _should_resume(self) -> bool:
        """有 session 且有 resume URL 才 Resume，否则走 Identify。"""
        return bool(self._session_id and self._resume_url)

    def _clear_session(self) -> None:
        """丢弃会话：下一次连接必须 Identify（op 9 d=false / 4011 等）。"""
        self._session_id = None
        self._last_seq = None

    def _resolve_gateway_url(self) -> str:
        """本次连接用的 WSS URL（带 ``?v=10&encoding=json``）。

        Resume 用 READY 给的 ``resume_gateway_url``；否则用缓存的初始 URL。
        初始 URL 来自 ``GET /gateway/bot``（**不硬编码主机名**：官方会换域名），
        也允许 ``gateway_url`` 配置覆盖（自建网关 / 测试）。
        """
        base = self._resume_url if self._should_resume() else self._gateway_url
        if not base:
            status, data = self._request("GET", "gateway/bot", {})
            url = str(data.get("url") or "")
            if status != 200 or not url:
                raise RuntimeError(
                    f"GET /gateway/bot failed: HTTP {status} "
                    f"{data.get('message') or data.get('code') or ''}".strip()
                )
            self._gateway_url = url
            base = url
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}v={GATEWAY_API_VERSION}&encoding={GATEWAY_ENCODING}"

    def _make_ws(self, url: str):
        """建 WS 连接；``_ws_factory`` 为测试注入点，生产走标准库实现（T2.0）。"""
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory  # 延迟导入：没开入站也不加载它
        return factory(url, timeout=GATEWAY_RECV_TIMEOUT)

    def _inbound_loop(self) -> None:
        """连接 → 收包 → 决策（继续 / 重连 / 重新 Identify / 停止）→ 退避。

        ``recv()`` 阻塞时：Windows 上 ``close()`` 会立刻唤醒它；Linux 上不保证，
        所以 :data:`GATEWAY_RECV_TIMEOUT` 是兜底（心跳每 ~45s 就有一次 ACK 往返，
        正常连接不会因为它被误判）。
        """
        delay = RECONNECT_MIN
        while not self._stop_event.is_set():
            ws = None
            action = _ACT_RECONNECT
            started = time.monotonic()
            try:
                ws = self._make_ws(self._resolve_gateway_url())
                self._ws = ws
                logger.info(
                    "discord: gateway 已连接（%s 路径）",
                    "Resume" if self._should_resume() else "Identify",
                )
                while not self._stop_event.is_set():
                    raw = ws.recv()
                    if raw is None:  # 对端关闭 → 看 close code 决定要不要再连
                        action = self._action_for_close(getattr(ws, "close_code", None))
                        break
                    action = self._handle_payload(ws, raw)
                    if action != _ACT_CONTINUE:
                        break
            except Exception as exc:  # noqa: BLE001 - 线程里绝不外抛
                if not self._stop_event.is_set():
                    logger.warning("discord: gateway 异常：%s", exc)
                action = _ACT_RECONNECT
            finally:
                self._stop_heartbeat()
                current, self._ws = self._ws, None
                if current is not None:
                    try:
                        current.close()
                    except Exception:  # noqa: BLE001
                        pass
                if action == _ACT_REIDENTIFY:
                    self._clear_session()
            if action == _ACT_FATAL:
                logger.error("discord: close code 表示配置/权限错误，停止重连")
                return
            if self._stop_event.is_set():
                break
            # 退避：连上并活过一段时间就把间隔重置回下限
            if time.monotonic() - started >= RECONNECT_MIN:
                delay = RECONNECT_MIN
            if self._stop_event.wait(delay):
                break
            delay = min(delay * 2, RECONNECT_MAX)

    # -- 收包 -------------------------------------------------------------
    def _handle_payload(self, ws, raw: str) -> str:
        """处理一个网关包，返回下一步动作（continue/reconnect/reidentify/fatal）。

        只有 ``op == 0`` 的包才带 ``s`` / ``t``；``s`` 为 null 时**不能**覆盖
        ``_last_seq``（否则 Resume 会跳事件）。
        """
        try:
            packet = json.loads(raw)
        except Exception:
            logger.debug("discord: 非 JSON 帧，忽略")
            return _ACT_CONTINUE
        if not isinstance(packet, dict):
            return _ACT_CONTINUE
        try:
            op = int(packet.get("op"))
        except (TypeError, ValueError):
            logger.debug("discord: 包里没有合法 op，忽略")
            return _ACT_CONTINUE

        seq = packet.get("s")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._last_seq = seq
        data = packet.get("d")

        if op == OP_DISPATCH:
            self._handle_dispatch(str(packet.get("t") or ""), data)
            return _ACT_CONTINUE
        if op == OP_HEARTBEAT:
            # 服务端要求**立即**心跳，不能等下一个周期
            self._send_op(ws, OP_HEARTBEAT, self._last_seq)
            return _ACT_CONTINUE
        if op == OP_HELLO:
            self._on_hello(ws, data)
            return _ACT_CONTINUE
        if op == OP_HEARTBEAT_ACK:
            self._ack_received = True
            return _ACT_CONTINUE
        if op == OP_RECONNECT:
            # 官方：几秒后服务端也会关掉，别在那儿干等
            logger.info("discord: op 7 Reconnect —— 立即断开并重连")
            self._close_ws(ws)
            return _ACT_RECONNECT
        if op == OP_INVALID_SESSION:
            if bool(data):
                logger.info("discord: op 9 Invalid Session(d=true) —— 可 Resume")
                return _ACT_RECONNECT
            logger.warning("discord: op 9 Invalid Session(d=false) —— session 失效，改走 Identify")
            self._clear_session()
            return _ACT_REIDENTIFY
        logger.debug("discord: 忽略未处理的 op=%s", op)
        return _ACT_CONTINUE

    def _action_for_close(self, code: object) -> str:
        """按对端 close code 决定是否重连（4004/4010-4014 属于配错，重连无用）。"""
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if code_int is None:
            logger.info("discord: 对端关闭但没给 close code，按可 Resume 处理")
            return _ACT_RECONNECT
        if code_int in FATAL_CLOSE_CODES:
            logger.error(
                "discord: close %s —— %s", code_int, FATAL_CLOSE_CODES[code_int]
            )
            return _ACT_FATAL
        logger.info("discord: close %s —— 可 Resume 重连", code_int)
        return _ACT_RECONNECT

    # -- 会话建立 ---------------------------------------------------------
    def _on_hello(self, ws, data: object) -> None:
        """HELLO → 定心跳周期 → 立刻发一次心跳 → Identify / Resume → 起心跳线程。

        官方推荐顺序是 HELLO → op1 → Identify（Identify 24h 内全局限 1000 次，
        所以有 session 就优先 Resume）。第一次心跳**立即**发（这样 Identify 前一定
        有心跳），jitter 加在第一次**周期**心跳前。
        """
        raw = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and raw > 0
        ):
            interval = float(raw) / 1000.0  # ⚠️ 官方文档的单位是**毫秒**
        else:
            interval = HEARTBEAT_DEFAULT
            logger.warning(
                "discord: Hello 未给合法 heartbeat_interval（%r），按 %.1fs 处理",
                raw,
                interval,
            )
        self._heartbeat_interval = interval
        self._ack_received = False

        # 第一次心跳：d 键必须存在，一个事件都没收到时就是 null
        self._send_op(ws, OP_HEARTBEAT, self._last_seq)
        if self._should_resume():
            self._send_op(
                ws,
                OP_RESUME,
                {
                    "token": self.bot_token,
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            )
        else:
            self._send_op(
                ws,
                OP_IDENTIFY,
                {
                    "token": self.bot_token,
                    "intents": self.intents,
                    "properties": {
                        "os": "python",
                        "browser": "opencode-bridge",
                        "device": "opencode-bridge",
                    },
                },
            )
        self._start_heartbeat(ws, interval)

    def _send_op(self, ws, op: int, data: Any = None, seq: Any = None) -> bool:
        """发一个网关 payload。

        * 心跳的 ``d`` **恒存在**（没有事件时是 ``null``），不能省掉这个键；
        * ``s`` 只在明确要带时才出现（Identify / Resume 不带）；
        * payload 超 4096 字节服务端会 close 4002，所以本地先拦一道。
        """
        packet: dict = {"op": op, "d": data}
        if seq is not None:
            packet["s"] = seq
        text = json.dumps(packet)
        if len(text.encode("utf-8")) > GATEWAY_MAX_PAYLOAD:
            logger.error(
                "discord: 网关 payload %d 字节超过 %d，拒发并断开",
                len(text.encode("utf-8")),
                GATEWAY_MAX_PAYLOAD,
            )
            self._close_ws(ws, 4002, "payload too large")
            return False
        try:
            ws.send(text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord: 发送 op=%s 失败: %s", op, exc)
            return False

    def _close_ws(self, ws, code: int = GATEWAY_CLOSE_REQUESTED, reason: str = "") -> None:
        """主动断开：默认用非 1000 的 code，保住 session 以便 Resume。"""
        try:
            ws.close(code, reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("discord: ws.close 失败（忽略）: %s", exc)

    # -- 心跳线程 ---------------------------------------------------------
    def _start_heartbeat(self, ws, interval: float) -> None:
        self._stop_heartbeat()
        stop = threading.Event()
        self._hb_stop = stop
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(ws, interval, stop),
            name="discord-heartbeat",
            daemon=True,
        )
        self._hb_thread = thread
        thread.start()

    def _heartbeat_jitter(self, interval: float) -> float:
        """第一次**周期**心跳前的抖动（官方：``interval * random(0, 1)``）。

        单独抽成方法是给测试一个注入点（避免测试依赖随机值）。
        """
        return random.uniform(0.0, interval)

    def _heartbeat_loop(self, ws, interval: float, stop: threading.Event) -> None:
        """周期心跳 + ACK 监测。

        官方要求：一个心跳周期内没收到 op 11 就判定连接已死，用**非 1000** 的
        close code 主动断开（1000/1001 会让 session 失效），随后 Resume 重连。
        """
        first = True
        while not stop.is_set() and not self._stop_event.is_set():
            wait = interval
            if first:
                wait = interval + self._heartbeat_jitter(interval)
                first = False
            if stop.wait(wait):
                return
            if getattr(ws, "closed", False):
                return
            self._send_op(ws, OP_HEARTBEAT, self._last_seq)
            if stop.wait(interval):  # 等一个周期的 ACK
                return
            if not self._ack_received:
                logger.warning(
                    "discord: %.1fs 内没收到 op 11 ACK，判定连接已死，断开重连", interval
                )
                self._close_ws(ws, GATEWAY_CLOSE_REQUESTED, "heartbeat ack timeout")
                return
            self._ack_received = False

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()
        thread = self._hb_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        self._hb_thread = None

    # -- dispatch / 事件过滤 ---------------------------------------------
    def _handle_dispatch(self, name: str, data: object) -> None:
        if name == "READY":
            self._handle_ready(data)
        elif name == "MESSAGE_CREATE":
            self._handle_message_create(data)
        else:
            logger.debug("discord: 忽略 dispatch t=%s", name)

    def _handle_ready(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        self._my_user_id = str(user.get("id") or "") or None
        self._session_id = str(data.get("session_id") or "") or None
        self._resume_url = str(data.get("resume_gateway_url") or "") or None
        logger.info(
            "discord: READY（user=%s session=%s）", self._my_user_id, self._session_id
        )

    def _drop_inbound(self, reason: str, channel: str, author: str, is_bot: bool) -> None:
        logger.info(
            "discord: 丢弃消息（%s）channel=%s author=%s author_is_bot=%s",
            reason,
            channel or "?",
            author or "?",
            is_bot,
        )

    def _handle_message_create(self, data: object) -> bool:
        """``MESSAGE_CREATE`` → 过滤 → Inbound。返回是否真的放行了一条。"""
        if not isinstance(data, dict):
            return False
        author = data.get("author")
        author = author if isinstance(author, dict) else {}
        author_id = str(author.get("id") or "")
        # ``author.bot`` 是**可选**字段（可能整个键不存在），而且**不能**拿它当过滤
        # 判据 —— 那会把别的 bot 发的消息也全丢掉；这里只用于日志。
        is_bot = bool(author.get("bot", False))
        channel_id = str(data.get("channel_id") or "")
        content = str(data.get("content") or "")

        # 1) 自己发的（否则发出去的消息会回到我们这里，无限回环）
        if self._my_user_id and author_id == self._my_user_id:
            self._drop_inbound("bot 自己发的", channel_id, author_id, is_bot)
            return False
        # 2) 只收普通消息（6=频道置顶、7=有人加入、18=新线程… 全是系统消息）
        if data.get("type", 0) != 0:
            self._drop_inbound(f"非普通消息 type={data.get('type')!r}", channel_id, author_id, is_bot)
            return False
        # 3) crosspost 转发会在每个频道各触发一次
        flags = data.get("flags") or 0
        if isinstance(flags, int) and not isinstance(flags, bool) and flags & FLAG_IS_CROSSPOST:
            self._drop_inbound("crosspost 转发", channel_id, author_id, is_bot)
            return False
        # 4) webhook 消息的 author.id 是 webhook id，不是真人
        if data.get("webhook_id"):
            self._drop_inbound("webhook 消息", channel_id, author_id, is_bot)
            return False
        # 5) 空正文（附件消息 content 为空）
        if not content:
            self._drop_inbound("空正文", channel_id, author_id, is_bot)
            return False
        if not channel_id:
            self._drop_inbound("缺 channel_id", channel_id, author_id, is_bot)
            return False
        # 6) 授权闸门必须在产生 Inbound **之前**（否则能用命令/审批字绕过）
        if not self.admits(channel_id):
            self._drop_inbound("未在白名单", channel_id, author_id, is_bot)
            return False
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(channel_id),
                    text=content,
                    kind="text",
                    user_id=author_id or None,
                    message_id=str(data.get("id") or "") or None,
                    platform=self.name,
                    raw=data,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord: on_inbound 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """先停心跳线程、再关 WS、最后 ``super().stop()``（顺序与 Slack 一致）。

        顺序不能反：基类 ``stop()`` 会 join 线程（5s 超时），而 ``recv()`` 在
        ``GATEWAY_RECV_TIMEOUT``(60s) 内可能一直阻塞；不先关连接就会每次 stop
        都等满超时。
        """
        self._stop_heartbeat()
        ws = self._ws
        self._ws = None
        if ws is not None:
            self._close_ws(ws)
        super().stop()

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_id(conversation_id: str) -> Optional[str]:
        raw = conversation_id
        if raw.startswith("channel:"):
            raw = raw[len("channel:"):]
        return raw or None

    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        return f"channel:{channel_id}"

    def _send_chunk(
        self, channel: str, content: str, conversation_id: str
    ) -> Tuple[int, dict]:
        """POST one message chunk, retrying once on 429 (retry_after)."""
        self._throttle(conversation_id)
        status, data = self._request(
            "POST", f"channels/{channel}/messages", {"content": content}
        )
        if status == 429:
            delay = data.get("retry_after")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                delay = min(float(delay), MAX_RETRY_AFTER)
                logger.warning(
                    "discord: rate limited, sleeping %.1fs and retrying once", delay
                )
                time.sleep(delay)
                self._throttle(conversation_id)
                status, data = self._request(
                    "POST", f"channels/{channel}/messages", {"content": content}
                )
        return status, data

    def send(self, out: Outbound) -> MsgHandle | None:
        channel = self._channel_id(out.conversation_id)
        if not channel:
            logger.warning("discord: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("discord: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        chunks: List[str] = split_text(out.text, self.message_limit, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "discord: splitting outbound message into %d chunks", len(chunks)
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            status, data = self._send_chunk(channel, chunk, out.conversation_id)
            if status < 200 or status >= 300 or "id" not in data:
                detail = str(data.get("message") or data.get("code") or "")
                logger.warning(
                    "discord: send failed (HTTP %s): %s",
                    status,
                    data.get("message") or data.get("code"),
                )
                # Discord 在 429 的响应体里给 retry_after（秒，float）
                retry_after = data.get("retry_after")
                self._note_send_failure(
                    classify_http(status, detail),
                    detail,
                    retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
                )
                return handle if handle is not None else None
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(data.get("id")),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        channel = self._channel_id(handle.conversation_id)
        if not channel or not handle.message_id:
            logger.warning("discord: bad handle %r", handle)
            return False
        if not out.text:
            logger.warning("discord: refusing to edit with empty text")
            return False
        self._throttle(handle.conversation_id)
        status, data = self._request(
            "PATCH",
            f"channels/{channel}/messages/{handle.message_id}",
            {"content": out.text},
        )
        if status == 429:
            delay = data.get("retry_after")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                time.sleep(min(float(delay), MAX_RETRY_AFTER))
                self._throttle(handle.conversation_id)
                status, data = self._request(
                    "PATCH",
                    f"channels/{channel}/messages/{handle.message_id}",
                    {"content": out.text},
                )
        if status < 200 or status >= 300:
            logger.warning(
                "discord: edit failed (HTTP %s): %s",
                status,
                data.get("message") or data.get("code"),
            )
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        return None  # Discord components: deferred via interactions (TODO v2)
