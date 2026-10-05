"""ntfy 适配器（``tasks.md`` B1）：HTTP 一次性拉取入站 + POST 出站。

ntfy 是"零妥协项"里唯一完全不需要公网回调的现代消息平台：订阅与发布都是
普通 HTTP，且**纯标准库可做**（``http.client`` 读 ndjson 即可）。

## 为什么用「一次性拉取」而不是持久流

ntfy 的 ``/json`` 端点默认是**长连接流**（Hermes 就这么做的）。但持久流在
纯标准库 ``urllib`` 下**无法被干净打断** —— 关一个正卡在 ``read()`` 上的 socket
不保证唤醒那次读，于是每次 ``stop()`` 都可能白等一个超时。

所以本适配器走 ``poll=1``：服务端读完缓存就**关闭连接**。代价是每次轮询多一次
HTTP 往返，收益是生命周期干净、能直接复用传输层的退避与停止语义。

## 启动为什么不重放历史缓存

``poll=1`` 不带 ``since`` 时返回**整个话题缓存**（最多 10MB，文档标注
``X-Messages-Truncated`` 头）。对桥接来说那是灾难：首次启动会把过去几小时的
历史通知全部当成新消息，各触发一次 agent 运行。

``since=`` 接受 Unix 时间戳，所以启动时用 ``since=<当前时间>`` —— 只收"从现在
开始"的。之后每条消息把游标推进到它的 **message id**（文档对轮询场景的明确建议：
"pass ``since=<last message ID>`` rather than re-fetching everything"）。

## 防回环：只用 tags，绝不用 title

ntfy **没有用户身份原语**。Hermes 的 plugin.yaml 把这点写得很明确：不得从
"publisher-controlled fields" 推导身份 —— ``title`` 是**发布者可控**的，任何人
都能给自己发一条带同样 title 的消息，所以拿 title 当身份等于没有认证。

本适配器与 Hermes 同策：出站消息打一个自定义 tag（``X-Tags``），入站见到该
tag 就丢弃。这是**回环**防护，不是**身份**防护；真正的信任边界要靠 ntfy 侧的
私有 topic + read token（见类 docstring）。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text
from ..transport import NOTHING, EventQueue, PollingTransport
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.ntfy")

__all__ = ["NtfyAdapter"]

DEFAULT_SERVER = "https://ntfy.sh"
#: ntfy 服务端消息体硬上限 4096（受 FCM/APNS 约 4KB 约束）。**注意是字节不是字符** ——
#: 见 :meth:`NtfyAdapter._fit_bytes`。
MESSAGE_LIMIT = 4096
#: 轮询间隔。ntfy 是"有消息才通知"的推送服务，5s 足够灵敏且不浪费带宽
#: （每次 poll 都是一次 HTTP 往返）。
DEFAULT_POLL_INTERVAL = 5.0
SOCKET_TIMEOUT = 30.0
#: 出站默认 message id 前缀无关；这里只是限流，避免把话题刷爆。
MIN_SEND_INTERVAL = 1.0
#: ntfy 的限流错误码（带宽预算耗尽），见官方 "Replay limits" 一节。
NTFY_RATE_LIMIT_CODE = "42905"


@register("ntfy")
class NtfyAdapter(Adapter):
    """ntfy push 通知网关。

    **信任模型**：ntfy 没有用户身份 —— 任何能往话题发消息的人都会被当作用户。
    所以务必使用**私有话题 + read token**（``token`` 配置项）或 access control；
    公共话题（``ntfy.sh/<topic>``）等于把 agent 暴露给全网。本类 docstring 明确
    记录该风险，``required_tokens`` 也只要求 topic 是为了让"未配置"能被状态视图
    如实报出。
    """

    name = "ntfy"
    label = "ntfy"
    max_message_length = MESSAGE_LIMIT          # 4096 —— 但单位是**字节**
    supports_inbound = True
    #: principal = topic：(a) 会话唯一且稳定，(b) 用户自己命名的 topic、知道它是什么，
    #: (c) ntfy 对发布者做过认证（token 鉴权）。三条都成立。
    pairing_supported = True
    supports_inline_buttons = False             # v1 不做 actions 按钮
    supports_media = False
    typed_command_prefix = "/"

    required_tokens = ("topic",)
    outbound_tokens = ("topic",)

    # 类级旋钮（测试可在实例上覆盖）
    poll_interval = DEFAULT_POLL_INTERVAL
    min_interval = MIN_SEND_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.server: str = str(
            self.config.get("server") or DEFAULT_SERVER
        ).strip().rstrip("/")
        self.topic: str = str(self.config.get("topic") or "").strip()
        # 认证：token（tk_… read token）优先；否则 user/password 走 Basic
        self._auth_header: Optional[str] = self._build_auth()
        # 回环标记 tag。**不要**改成 title —— title 是发布者可控字段。
        self.echo_tag: str = str(
            self.config.get("echo_tag") or "opencode-bridge"
        ).strip()

        self._queue = EventQueue()
        self._since: str = ""          # 游标；空 = 尚未 bootstrap
        self._transport: Optional[PollingTransport] = None
        self._throttle_lock = threading.Lock()
        self._last_send: float = 0.0

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    def _build_auth(self) -> Optional[str]:
        token = str(self.config.get("token") or "").strip()
        if token:
            return f"Bearer {token}"
        user = str(self.config.get("user") or "").strip()
        password = self.config.get("password")
        if user and password is not None and str(password) != "":
            raw = f"{user}:{password}".encode("utf-8")
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self, *, publish: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "opencode-bridge (ntfy, 1.0)"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        if publish:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            # 回环标记：入站见到这个 tag 就丢弃
            headers["X-Tags"] = self.echo_tag
        return headers

    def _poll_url(self) -> str:
        params = {"poll": "1"}
        if self._since:
            params["since"] = self._since
        else:
            # 首次：只收"从现在开始"的，避免重放整个话题缓存（最多 10MB）
            params["since"] = str(int(time.time()))
        return (
            f"{self.server}/{urllib.parse.quote(self.topic, safe='')}/json"
            f"?{urllib.parse.urlencode(params)}"
        )

    def _http(self, method: str, url: str, *, data: Optional[bytes] = None,
              headers: Optional[dict] = None) -> Tuple[int, Any, dict]:
        """发一个请求。返回 ``(status, 解析后的 JSON 或 None, 响应头)``。

        永不抛异常 —— 传输层靠异常触发退避重连，但这里要把"HTTP 4xx"与
        "网络故障"区分开：前者不该退避重试（如 401 凭据错），后者应该。
        """
        req = urllib.request.Request(
            url, data=data, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            hdrs = {}
        except Exception as exc:
            raise RuntimeError(f"transport error: {exc}") from exc
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
        return status, parsed, hdrs

    # ------------------------------------------------------------------
    # 入站：一次性拉取（适配 PollingTransport）
    # ------------------------------------------------------------------
    def _fetch_one(self) -> Any:
        """给 :class:`PollingTransport` 的 ``fetch``：返回**一条**消息或 ``NOTHING``。"""
        if not self._queue:
            self._pull_batch()
        if not self._queue:
            return NOTHING
        item = self._queue.pop()
        # 游标只在**队列排空**时推进：若推进过早而进程中途崩溃，队列里剩下的
        # 消息会被 since= 跳过，造成静默丢消息。
        if not self._queue:
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                self._since = str(mid)
        return item

    def _pull_batch(self) -> None:
        """向服务端拉一批消息填进队列。失败时抛异常（交给传输层退避重连）。"""
        status, payload, hdrs = self._http(
            "GET", self._poll_url(), headers=self._headers(publish=False)
        )
        if status == 429:
            # 带宽预算耗尽（ntfy 错误码 42905）。不是凭据问题，别无脑退避重试。
            logger.warning("ntfy: rate limited by server (429); slowing down")
            self._note_send_failure(SendError.RATE_LIMITED, "server 429 / 42905")
            return
        if status >= 400:
            kind = classify_http(status, "ntfy subscribe")
            if kind in (SendError.FORBIDDEN, SendError.BAD_FORMAT):
                # 凭据/地址写错，重试无意义 —— 明确记一次，别静默刷日志
                logger.error("ntfy: subscribe rejected (HTTP %s)；检查 token 与 topic", status)
            raise RuntimeError(f"subscribe failed: HTTP {status}")
        if hdrs.get("x-messages-truncated"):
            # 官方文档：响应被截断（超过 10MB/话题），更早的消息没拿到
            logger.warning(
                "ntfy: 响应被服务端截断（X-Messages-Truncated），"
                "较早的消息未收到；可考虑缩短轮询间隔"
            )
        items = payload if isinstance(payload, list) else []
        self._queue.push_many([
            m for m in items
            if isinstance(m, dict) and m.get("event") == "message"
        ])

    def _on_raw(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if self.echo_tag in tags:
            return                      # 自己发出去的，丢
        text = str(item.get("message") or "").strip()
        if not text:
            return
        topic = str(item.get("topic") or self.topic)
        # 授权闸门在产生 Inbound 之前。⚠️ /pair 在未授权时也要能进来，
        # 所以 conversation_id 提到闸门之前算。
        conversation_id = format_id(self.name, topic)
        if not self.admits(topic) and not self.answer_pairing_request(
            topic, conversation_id, text
        ):
            logger.info(
                "ntfy: dropping message from non-whitelisted topic %s",
                redactable_id(self.name, topic),
            )
            return
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=text,
                    kind="text",
                    user_id=None,          # ntfy 无用户身份，见类 docstring
                    message_id=str(item.get("id") or "") or None,
                    platform=self.name,
                    raw=item,
                )
            )
        except Exception as exc:
            logger.exception("ntfy: on_inbound failed: %s", exc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """轮询线程是否还在跑。

        ⚠️ **必须显式覆写**：基类的 ``running`` 读 ``self._thread``，而线程归
        ``Transport`` 所有、本类从不设它 —— 于是 ``capabilities()["running"]``
        会**永远是 False**，``--status`` / ``--setup --json`` 就会把一个正在收信的
        平台报成"没在跑"。这属于本项目反复修的那一类"状态被误报"。
        与已迁移的 irc / matrix / telegram / slack / qqbot 同款。
        """
        transport = self._transport
        return transport is not None and transport.running

    def start(self) -> None:
        if not self.topic:
            logger.warning("ntfy: topic missing; adapter not started")
            return
        if self._transport is None:
            self._transport = PollingTransport(
                self._fetch_one,
                idle_sleep=float(self.config.get("poll_interval") or self.poll_interval),
                name=self.name,
            )
        self._transport.start(self._on_raw)

    def stop(self, timeout: float = 5.0) -> None:
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.stop(timeout=timeout)

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    def _throttle(self, topic: str) -> None:
        interval = float(getattr(self, "min_interval", MIN_SEND_INTERVAL))
        if interval <= 0:
            return
        with self._throttle_lock:
            last = self._last_send
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_send = time.monotonic()

    def _fit_bytes(self, text: str, budget: int = MESSAGE_LIMIT) -> str:
        """把正文截到 ``budget`` **字节**（ntfy 的上限是字节，不是字符）。

        按字节上限截断时**不能**用 ``text[:budget]`` —— 那样中文会被切成半个
        字符，编码即报错。这里按字符累加字节，遇到装不下的字符就停。
        """
        out: list[str] = []
        used = 0
        for ch in text:
            size = len(ch.encode("utf-8"))
            if used + size > budget:
                break
            out.append(ch)
            used += size
        return "".join(out)

    def send(self, out: Outbound) -> MsgHandle | None:
        topic = str(out.conversation_id or self.topic).strip()
        if ":" in topic:
            topic = topic.split(":", 1)[1]      # 剥掉 ntfy: 前缀
        if not topic or not out.text:
            logger.warning("ntfy: refusing to send (topic=%r text=%r)", topic, bool(out.text))
            self._note_send_failure(SendError.BAD_FORMAT, "bad topic or empty text")
            return None

        handle: MsgHandle | None = None
        # ntfy 不支持"一条消息拆多段"的语义，但单条有 4096 字节硬上限，
        # 所以按**字节**预算切；split_text 是按字符切的（其它平台用），
        # 这里先用它做粗切，再用 _fit_bytes 兜住字节上限。
        for chunk in split_text(out.text, self.effective_max_length, prefix_fmt=""):
            piece = self._fit_bytes(chunk)
            if not piece:
                continue
            self._throttle(topic)
            url = f"{self.server}/{urllib.parse.quote(topic, safe='')}"
            status, payload, _ = self._http(
                "POST",
                url,
                data=piece.encode("utf-8"),
                headers=self._headers(publish=True),
            )
            if status < 200 or status >= 300:
                detail = ""
                if isinstance(payload, dict):
                    detail = str(payload.get("error") or payload.get("code") or "")
                self._note_send_failure(classify_http(status, detail), detail or f"HTTP {status}")
                return handle if handle is not None else None
            mid = ""
            if isinstance(payload, dict):
                mid = str(payload.get("id") or "")
            handle = MsgHandle(format_id(self.name, topic), mid, self.name)
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """ntfy **没有编辑消息**的能力（没有 edit 端点）。

        返回 ``False`` 让 core.py 退化成"再发一条新消息"，而不是假装成功 ——
        对推送通知来说这才是诚实的语义。
        """
        logger.debug("ntfy: edit not supported by ntfy; caller should send a new message")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """ntfy 没有 callback query 概念 —— no-op。"""
        return None
