"""SMS（Twilio）适配器（A3 第二阶段）：共享 webhook 入站 + REST 出站。

拓扑与拍板（2026-10-10 用户）
--------------------------
* 单端口共享 server：本适配器**只交 routes**（:meth:`SmsAdapter.webhook_routes`），
  端口绑定、线程与优雅关闭全归 :class:`opencode_bridge.webhook_hub.WebhookHub`。
* 第二个 adapter = sms（Twilio）：7 家 webhook 对照表里唯一有**官方真验签算法**
  可照抄的族（``docs/platform-design-reference.md`` Part 11）。
* 可达性暂不放宽：默认回环 bind；本机自测验签链路先行，有隧道/公网再议。

配置键（``adapters.sms``）
-------------------------
``account_sid`` / ``auth_token`` / ``from_number`` / ``webhook_url`` +
``allowed_chat_ids``（E.164 电话号码，如 ``+15551234567``）。

fail-closed 语义
----------------
* :meth:`webhook_routes` 仅在 ``auth_token`` **与** ``webhook_url`` 都配齐时返回
  路由，否则交空 —— hub 零路由不绑端口，本适配器**绝不**挂一个不验签的路径。
  （webhook_url 属入站必需：Twilio 签名的就是它，缺了连验签的输入都没有。）
* ⛔ 刻意**不加** ``insecure_no_signature`` 逃逸口（YAGNI + 安全旁路）：
  要在无公网回调的前提下自测，把 ``webhook_url`` 配成回环地址即可走完整的
  真验签链路，不需要旁路。
* 验签对**配置的 webhook_url** 做（隧道/公网场景下 Twilio 签的就是那个 URL），
  不是本地请求 URL；带/不带默认端口两个变体都试（hermes 同款，见下）。

与 hermes 参考实现的差异（§6.4：能抄的照抄，差异记在这里）
-------------------------------------------------------
照抄的部分（hermes 快照 commit ``8a5edab2``，行 1271555-1271859）：
X-Twilio-Signature 验签算法（HMAC-SHA1 + base64 + 按键排序拼接）、
默认端口双变体、回声防护（``From == from_number`` 丢弃）、
缺 From/Body 静默 200、出站 REST 端点与 Basic 认证形状。

刻意不抄的部分（每条记原因）：
* **markdown 剥离** —— 本仓 ``Outbound.text`` 已是纯文本（分片/改写在出站
  上游完成），hermes 需要剥是因为它直接把 agent 的 markdown 原文塞进短信。
* **入站正文 ``text[:80]`` 进日志** —— 正文不进日志是本仓纪律；
  对 SMS 而言正文与号码（``sms:+1555…``）都是 PII。
* **asyncio task 分发** —— hub 的 server 是 ThreadingHTTPServer，handler
  本来就跑在请求线程上；``on_inbound`` 只入队不阻塞，无需再分派。
* **鉴权模型** —— hermes 用自己的 auth 层；本仓授权闸门
  （:meth:`Adapter.admits`，产生 Inbound 之前，配对兜底在同一个 ``if`` 里）
  是适配器纪律，必须遵守。

``pairing_supported = False`` 的理由
------------------------------------
三条判据里的 (c)（**平台对发件人做过认证**）对 SMS 不成立到可以依赖的程度：
``From`` 号码是**运营商转写**的，SMS spoofing 在协议层是可能的 —— 配对码会
发给一个可能被伪造的号码。失败关闭：用户把真实号码填进 ``allowed_chat_ids``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..httpsrv import HttpRequest, HttpResponse, Route
from ..identity import format_id
from ..split import split_text
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.sms")

__all__ = ["SmsAdapter"]

#: Twilio REST API 的 Messages 端点根（hermes 同款）。
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
#: 单条出站上限（字符）。1600 ≈ 10 个 SMS segment，是 Twilio 单次请求体
#: 的安全上限（hermes 同款）；更长的由 :meth:`SmsAdapter.send` 分片。
MAX_SMS_LENGTH = 1600
#: 入站 webhook 的固定路径（不可配 —— 冲突归 hub 整体拒绝）。
TWILIO_WEBHOOK_PATH = "/webhooks/twilio"
#: Twilio webhook 载荷很小；hermes 同款 64 KiB 上限（httpsrv 全局上限是 1 MiB）。
TWILIO_WEBHOOK_MAX_BODY_BYTES = 65_536
#: 空 TwiML 应答：回复一律走 REST，绝不 inline TwiML（hermes 同款）。
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

SOCKET_TIMEOUT = 30.0


def _twiml_response(status: int = 200) -> HttpResponse:
    """空 TwiML 应答 —— 所有入站分支都回它（或 4xx 变体）。"""
    return HttpResponse(
        status=status,
        body=_EMPTY_TWIML.encode("utf-8"),
        content_type="application/xml",
    )


def _basic_auth(account_sid: str, auth_token: str) -> str:
    """Twilio REST 的 HTTP Basic 认证头（hermes 同款）。"""
    encoded = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("utf-8")
    return f"Basic {encoded}"


def _port_variant_url(url: str) -> Optional[str]:
    """把 URL 的默认端口**翻转**（带上↔剥掉）；非默认端口返回 ``None``。

    Twilio 可能按"带默认端口"或"不带默认端口"的 URL 计算签名，两种都要试
    （官方 security 文档 + hermes 同款）。
    """
    parsed = urllib.parse.urlsplit(url)
    default_port = {"https": 443, "http": 80}.get(parsed.scheme)
    if default_port is None:
        return None
    if parsed.port == default_port:
        netloc = parsed.hostname
    elif parsed.port is None:
        netloc = f"{parsed.hostname}:{default_port}"
    else:
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


@register("sms")
class SmsAdapter(Adapter):
    """Twilio SMS 网关。

    **信任模型**：``X-Twilio-Signature`` 是唯一信任边界 —— 只有持有
    ``auth_token`` 的我方与 Twilio 能算出合法签名，所以**任何**入站请求都
    必须先过 :meth:`_validate_twilio_signature`（fail-closed，403 拒绝）。
    验签之后仍有第二道闸：``allowed_chat_ids``（E.164 号码白名单）。
    """

    name = "sms"
    label = "SMS (Twilio)"
    max_message_length = MAX_SMS_LENGTH
    supports_inbound = True
    supports_inline_buttons = False
    supports_media = False
    # supports_message_edit / local_id_pattern / legacy_conversation_prefix
    # 都用基类默认（False / None / None）：SMS 改不了已发出的短信，
    # 也没有歧义旧前缀要划分。
    typed_command_prefix = "/"
    # (c) 不成立：From 号码是运营商转写、可被伪造（见模块 docstring）。
    pairing_supported = False

    required_tokens = ("account_sid", "auth_token", "from_number", "webhook_url")
    outbound_tokens = ("account_sid", "auth_token", "from_number")

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self._account_sid = str(self.config.get("account_sid") or "").strip()
        self._auth_token = str(self.config.get("auth_token") or "").strip()
        self._from_number = str(self.config.get("from_number") or "").strip()
        self._webhook_url = str(self.config.get("webhook_url") or "").strip()
        # 无自有线程：入站由 WebhookHub 持有，出站是无状态 REST。
        # ``_started`` 是「start() 被调且没被 stop」的标志，见 ``running``。
        self._started = False

    # ------------------------------------------------------------------
    # 配置就绪判定
    # ------------------------------------------------------------------
    def _inbound_ready(self) -> bool:
        """入站就绪 = 能验签（auth_token）且知道签的是哪个 URL（webhook_url）。"""
        return bool(self._auth_token and self._webhook_url)

    def _outbound_ready(self) -> bool:
        """出站就绪 = REST 三键齐（发信不需要 webhook_url）。"""
        return bool(self._account_sid and self._auth_token and self._from_number)

    # ------------------------------------------------------------------
    # 入站：共享 webhook 路由（契约三条，见 Adapter.webhook_routes）
    # ------------------------------------------------------------------
    def webhook_routes(self) -> tuple[Route, ...]:
        if not self._inbound_ready():
            # fail-closed：缺任一 ⇒ 不贡献路由（hub 零路由不绑端口），
            # 绝不挂一个无法验签的路径。
            return ()
        return (
            Route(
                path=TWILIO_WEBHOOK_PATH,
                handler=self._on_twilio_webhook,
                methods=("POST",),
                # 契约 2：hub 层无统一鉴权，验签在本 handler 里做。
                require_auth=False,
                name=f"{self.name}-webhook",
            ),
        )

    # -- Twilio 签名验证（hermes 逐字同款）--------------------------------

    def _validate_twilio_signature(
        self, url: str, post_params: dict, signature: str
    ) -> bool:
        """验 ``X-Twilio-Signature``：先按原 URL 算，不过再试默认端口变体。"""
        if self._check_signature(url, post_params, signature):
            return True
        variant = _port_variant_url(url)
        return bool(variant and self._check_signature(variant, post_params, signature))

    def _check_signature(
        self, url: str, post_params: dict, signature: str
    ) -> bool:
        if not self._auth_token:
            # 路由存在的前提就是 auth_token 已配齐；这条是防御性的失败关闭。
            return False
        data_to_sign = url + "".join(
            key + post_params[key] for key in sorted(post_params.keys())
        )
        mac = hmac.new(
            self._auth_token.encode("utf-8"),
            data_to_sign.encode("utf-8"),
            hashlib.sha1,
        )
        computed = base64.b64encode(mac.digest()).decode("utf-8")
        # 按 bytes 比：compare_digest 对非 ASCII str 会抛 TypeError，
        # 而签名是原始请求头，可能是任意字节。
        return hmac.compare_digest(computed.encode(), signature.encode())

    # -- webhook handler ---------------------------------------------------

    def _on_twilio_webhook(self, request: HttpRequest) -> HttpResponse:
        """处理一条 Twilio 入站回调。所有分支都回 TwiML（含 4xx 变体）。

        顺序（hermes 同款 + 本仓闸门纪律）：大小 → 解析 → 验签 →
        必需字段 → 回声防护 → 授权闸门 → Inbound。
        """
        if len(request.body) > TWILIO_WEBHOOK_MAX_BODY_BYTES:
            return _twiml_response(413)
        try:
            # Twilio 发的是 form-encoded，不是 JSON；parse_qs 的值是列表。
            form_values = urllib.parse.parse_qs(
                request.body.decode("utf-8"), keep_blank_values=True
            )
        except Exception:
            return _twiml_response(400)
        flat_params = {key: values[0] for key, values in form_values.items() if values}
        twilio_signature = request.header("X-Twilio-Signature")
        if not twilio_signature:
            logger.warning("sms: 拒绝 —— 缺少 X-Twilio-Signature 头")
            return _twiml_response(403)
        if not self._validate_twilio_signature(
            self._webhook_url, flat_params, twilio_signature
        ):
            logger.warning("sms: 拒绝 —— X-Twilio-Signature 不匹配")
            return _twiml_response(403)
        from_number = flat_params.get("From", "").strip()
        text = flat_params.get("Body", "").strip()
        message_sid = flat_params.get("MessageSid", "").strip()
        if not from_number or not text:
            # 缺字段 ⇒ 静默 200（hermes 同款）：回 4xx 会让 Twilio 反复重投
            # 并向账号发告警，而这些是畸形载荷，重投也不会变好。
            return _twiml_response()
        if from_number == self._from_number:
            # 回声防护：自己发出的短信被运营商回环（hermes 同款）。
            logger.debug("sms: 丢弃回声（来自自己的号码）")
            return _twiml_response()
        # 授权闸门在产生 Inbound 之前（本仓纪律）；⛔ 正文与裸号码不进日志，
        # 号码一律走 redactable_id（"sms" 必须在 identity.KNOWN_PLATFORMS 里，
        # 否则 redaction 不认 sms:+1555… 这个形状）。
        conversation_id = format_id(self.name, from_number)
        if not self.admits(from_number) and not self.answer_pairing_request(
            from_number, conversation_id, text
        ):
            logger.info(
                "sms: dropping message from non-whitelisted number %s",
                redactable_id(self.name, from_number),
            )
            return _twiml_response()
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=text,
                    kind="text",
                    user_id=from_number,
                    message_id=message_sid or None,
                    platform=self.name,
                    raw=flat_params,
                )
            )
        except Exception as exc:
            logger.exception("sms: on_inbound failed: %s", exc)
        return _twiml_response()

    # ------------------------------------------------------------------
    # 生命周期（无线程；入站归 hub，出站无状态）
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """已 start 且未被 stop。

        ⚠️ **必须显式覆写**：基类读 ``self._thread``，而本类从无线程 ⇒
        ``capabilities()["running"]`` 会永远 False，``--status`` 把一个正在
        收发的平台报成"没在跑"（ntfy docstring 点名的那类"状态被误报"）。
        """
        return self._started and not self._stop_event.is_set()

    def start(self) -> None:
        if self._inbound_ready() or self._outbound_ready():
            self._started = True
            return
        logger.warning("sms: account_sid/auth_token/from_number/webhook_url 均未配齐；adapter not started")

    def stop(self) -> None:
        self._started = False
        super().stop()

    # ------------------------------------------------------------------
    # 出站：REST（hermes 同款端点与认证形状）
    # ------------------------------------------------------------------
    def _post_message(self, form_body: bytes) -> Tuple[int, Any]:
        """POST Messages.json，返回 ``(HTTP 状态, 解析后的 JSON 或 None)``。

        传输层异常抛 :class:`RuntimeError`（与 ntfy 的 ``_http`` 同形：
        HTTP 4xx 不抛、网络故障才抛，由调用方区分对待）。
        """
        url = f"{TWILIO_API_BASE}/{self._account_sid}/Messages.json"
        request = urllib.request.Request(
            url,
            data=form_body,
            method="POST",
            headers={
                "Authorization": _basic_auth(self._account_sid, self._auth_token),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except Exception as exc:
            raise RuntimeError(f"transport error: {exc}") from exc
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
        return status, parsed

    def send(self, out: Outbound) -> MsgHandle | None:
        to_number = str(out.conversation_id or "").strip()
        if ":" in to_number:
            to_number = to_number.split(":", 1)[1]      # 剥掉 sms: 前缀
        if not self._outbound_ready():
            logger.warning("sms: 出站凭据未配齐（account_sid/auth_token/from_number），拒绝发送")
            self._note_send_failure(SendError.FORBIDDEN, "sms outbound credentials incomplete")
            return None
        if not to_number or not out.text:
            logger.warning(
                "sms: refusing to send (to=%r text=%r)",
                redactable_id(self.name, to_number), bool(out.text),
            )
            self._note_send_failure(SendError.BAD_FORMAT, "bad recipient or empty text")
            return None
        handle: MsgHandle | None = None
        # 部分成功语义与 ntfy 同形：前段已上线、后段失败 ⇒ 保留已有 handle
        # 返回（send_observed 据此判 partial），首段失败 ⇒ None。
        for chunk in split_text(out.text, self.effective_max_length, prefix_fmt=""):
            form_body = urllib.parse.urlencode(
                {"From": self._from_number, "To": to_number, "Body": chunk}
            ).encode("utf-8")
            status, payload = self._post_message(form_body)
            if status < 200 or status >= 300:
                detail = ""
                if isinstance(payload, dict):
                    detail = str(payload.get("message") or "")
                self._note_send_failure(
                    classify_http(status, detail), detail or f"HTTP {status}"
                )
                return handle if handle is not None else None
            message_sid = ""
            if isinstance(payload, dict):
                message_sid = str(payload.get("sid") or "")
            handle = MsgHandle(format_id(self.name, to_number), message_sid, self.name)
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """SMS 改不了已发出的短信 —— 恒 ``False``，让 core 退化成"再发一条"。"""
        logger.debug("sms: edit not supported by SMS; caller should send a new message")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """SMS 没有 callback query 概念 —— no-op。"""
        return None
