"""Lane B — adapter ABC and registry (CONTRACT.md §2.1 / §2.4)."""

from __future__ import annotations

import abc
import importlib
import importlib.util
import logging
import os
import pkgutil
import threading
from typing import Dict, Type

from ..hooks import Hooks, MsgHandle, Outbound, SendError, SendResult

logger = logging.getLogger("opencode_bridge.adapters.base")

__all__ = [
    "Adapter",
    "AdapterError",
    "build",
    "register",
    "classify_http",
    "adapter_class",
    "registered_names",
]


def classify_http(status: int, detail: str = "") -> SendError:
    """把 HTTP 状态码 / 平台描述收敛成平台中立的失败分类（T1.3）。

    消费方因此不必对厂商报错文本做 substring-match。平台特有的补充判定
    （如 Telegram 400 + "message is too long" -> ``TOO_LONG``）由各适配器自己
    在 :meth:`Adapter._note_send_failure` 处传入更精确的 ``kind``。
    """
    if status <= 0:
        return SendError.TRANSIENT          # 传输层失败（未拿到状态码）
    if status == 429:
        return SendError.RATE_LIMITED
    if status in (403, 401):
        return SendError.FORBIDDEN
    if status == 404:
        return SendError.NOT_FOUND
    if status == 413:
        return SendError.TOO_LONG
    if 400 <= status < 500:
        low = (detail or "").lower()
        if "too long" in low or "too large" in low or "message is too long" in low:
            return SendError.TOO_LONG
        return SendError.BAD_FORMAT
    if status >= 500:
        return SendError.TRANSIENT
    return SendError.UNKNOWN


class AdapterError(Exception):
    """Raised for adapter configuration / registration problems."""


# Registry populated by the individual adapter modules via ``register``.
_REGISTRY: Dict[str, Type["Adapter"]] = {}


def register(name: str):
    """Class decorator: add ``cls`` to the build registry under ``name``."""

    def deco(cls: Type["Adapter"]) -> Type["Adapter"]:
        _REGISTRY[name] = cls
        return cls

    return deco


class Adapter(abc.ABC):
    """Base class for messaging platform adapters.

    Lifecycle: ``start()`` spawns a poller thread (non-blocking, never raises
    to the caller); ``stop()`` sets the stop flag and joins the thread with a
    5 second timeout.

    能力以**类属性显式声明**（T1.1），调用方据此判断"能不能发按钮 / 该不该分片"，
    不再靠 try/except 撞运气。取值必须与各平台官方限制一致。
    """

    name: str = ""

    # --- capabilities（显式声明；子类必须按平台真值覆盖）------------------
    #: 展示名（状态视图 / /setup 引导用），如 ``"Telegram"``。
    label: str = ""
    #: 单条消息的字符上限；出站分片（T1.4）以此为阈值。
    max_message_length: int = 4000
    #: 是否具备入站（接收）能力。False = 只能主动发送。
    supports_inbound: bool = False
    #: 是否支持 inline 按钮 / 卡片式交互。
    supports_inline_buttons: bool = False
    #: 是否支持发送图片 / 文件等媒体。
    supports_media: bool = False
    #: 命令前缀（Telegram/Slack 用 ``/``，部分平台习惯 ``!``）。
    typed_command_prefix: str = "/"
    #: 配齐才算"该平台可用"的 token 键（``--status`` / ``--setup --json`` 消费）。
    #: 必须把**入站**必需的键也列进来：Slack 缺 ``app_token`` 会静默降级为"只发出
    #: 站"，若这里只列 ``bot_token``，状态视图就会把"入站根本没通"报成已配置。
    required_tokens: tuple[str, ...] = ("bot_token",)
    #: 只做出站所需的凭据键（``--status`` 的 ``outbound_ready`` 消费）。
    #: 默认与多数平台一致；**没有 bot_token 概念的平台必须覆盖**
    #: （Matrix 用 homeserver/access_token、IRC 用 host/nick、Mattermost 用 site_url/token），
    #: 否则状态视图会把它们一律报成"发不出去"。
    outbound_tokens: tuple[str, ...] = ("bot_token",)

    def __init__(self, config: dict, hooks: Hooks) -> None:
        self.config: dict = dict(config or {})
        self.hooks = hooks
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.allowed_chat_ids: set[str] = set()
        self._last_send_error: tuple[SendError, str, float | None] | None = None
        self._init_access()

    # --- capabilities ----------------------------------------------------
    def capabilities(self) -> Dict[str, object]:
        """Machine-readable capability snapshot (``--status`` / 状态视图消费）。"""
        return {
            "name": self.name,
            "label": self.label or self.name,
            "max_message_length": self.max_message_length,
            "supports_inbound": self.supports_inbound,
            "supports_inline_buttons": self.supports_inline_buttons,
            "supports_media": self.supports_media,
            "typed_command_prefix": self.typed_command_prefix,
            "allowed_chat_ids_count": len(self.allowed_chat_ids),
            "running": self.running,
        }

    # --- 授权闸门（T1.2：统一到基类，所有平台同一套判定）-------------------
    def _init_access(self) -> None:
        """从配置读 ``allowed_chat_ids`` 到统一形态（三种键名都认）。"""
        raw: object = None
        for key in ("allowed_chat_ids", "allowed_chats", "allowlist"):
            if key in self.config:
                raw = self.config.get(key)
                break
        items: list[object] = []
        if isinstance(raw, (list, tuple, set)):
            items = list(raw)
        elif raw not in (None, ""):
            items = [raw]
        self.allowed_chat_ids = {str(x).strip() for x in items if str(x).strip()}

    def admits(self, principal: object) -> bool:
        """入站闸门：**任何**入站消息（文本 / 命令 / 回调）都必须先过这里。

        语义（v1 保持现状）：白名单为空 = 全部允许；非空 = 只放行列表内的 chat。

        ⚠️ 调用顺序要求（对照 dsh 的反面教训）：授权判定必须在**命令解析与
        审批应答之前**，否则未授权者能用 ``/approve`` 这类命令字绕过闸门。
        入站适配器应在本方法返回 False 时**直接丢弃**，不要把消息交给上层。
        """
        if not self.allowed_chat_ids:
            return True
        return str(principal).strip() in self.allowed_chat_ids

    # --- lifecycle -----------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """Start the adapter (non-blocking). Must not raise to the caller."""

    def stop(self) -> None:
        """Request the polling thread to stop and join it (timeout 5s)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- messaging -----------------------------------------------------
    @abc.abstractmethod
    def send(self, out: Outbound) -> MsgHandle | None:
        """Send one message; return a handle. Failure -> log, return None."""

    @abc.abstractmethod
    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a previously sent message. Failure -> log, return False."""

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge an inline-keyboard callback query (optional)."""
        return None

    # --- 出站结果可观测（T1.3）-------------------------------------------
    def _note_send_failure(
        self,
        kind: SendError,
        detail: str = "",
        *,
        retry_after: float | None = None,
    ) -> None:
        """适配器在检测到失败时调用，记录**结构化**原因（供 ``send_result`` /
        ``--status`` 消费）。``send()`` 的既有签名与行为保持不变。"""
        self._last_send_error: tuple[SendError, str, float | None] = (
            kind,
            str(detail)[:400],
            retry_after,
        )

    def _clear_send_failure(self) -> None:
        self._last_send_error = None

    @property
    def last_send_error(self) -> SendError | None:
        """最近一次发送失败的分类（成功过则为 ``None``）。"""
        return self._last_send_error[0] if self._last_send_error else None

    def send_result(self, out: Outbound) -> SendResult:
        """结构化出站结果（T1.3 新增，**向后兼容**）。

        默认实现包装既有的 ``send()``：拿到句柄即成功，否则回读适配器在
        ``send()`` 内部记下的 ``_note_send_failure``。子类可覆写以给出更精确的
        ``partial`` / ``retry_after``。
        """
        self._clear_send_failure()
        try:
            handle = self.send(out)
        except Exception as exc:  # 适配器不应抛出；真抛了也不让上层崩
            self._note_send_failure(SendError.TRANSIENT, f"send() raised: {exc}")
            return SendResult(
                platform=self.name,
                ok=False,
                error_kind=SendError.TRANSIENT,
                error_detail=f"send() raised: {exc}",
            )
        if handle is not None:
            # 分片发送中"部分成功"：send() 返回了最后一个好句柄，但过程中记过失败。
            # 这种情况必须显式带出 partial，否则调用方会把未送达当成已送达。
            if self._last_send_error:
                kind, detail, retry_after = self._last_send_error
                return SendResult(
                    platform=self.name,
                    ok=True,
                    handle=handle,
                    error_kind=kind,
                    error_detail=detail,
                    retry_after=retry_after,
                    partial=True,
                )
            return SendResult(platform=self.name, ok=True, handle=handle)
        kind, detail, retry_after = self._last_send_error or (
            SendError.UNKNOWN,
            "send() returned None",
            None,
        )
        return SendResult(
            platform=self.name,
            ok=False,
            error_kind=kind,
            error_detail=detail,
            retry_after=retry_after,
        )


def _ensure_loaded(name: str) -> None:
    """确保 ``name`` 对应的适配器模块已被导入（导入即自我注册）。

    **新增平台不再需要改本文件**：只要新建 ``adapters/<name>.py`` 并加上
    ``@register("<name>")``，``build("<name>")`` 就能找到它。阶段 3 要批量加
    Matrix / Mattermost / IRC / Twitch 等平台，这里硬编码模块名会让"每加一个
    平台改一次核心文件"成为固定摩擦。

    名字不是合法标识符、或没有同名模块时，退回导入既有三家（保持"一次 import
    全部"的老行为），随后由调用方抛出 ``KeyError``。
    """
    if name in _REGISTRY:
        return
    if not name.isidentifier():
        return
    dotted = f"{__package__}.{name}"
    try:
        found = importlib.util.find_spec(dotted) is not None
    except (ImportError, AttributeError, ValueError):
        found = False
    if found:
        # 故意不吞异常：模块存在但导入失败要报真实原因，不能伪装成"未知适配器"。
        importlib.import_module(dotted)
        return
    from . import discord, slack, telegram  # noqa: F401  (side effect)


def adapter_class(name: str) -> Type[Adapter] | None:
    """按名取**已注册的适配器类**（不实例化），没有则 ``None``。

    供 ``--status`` / ``--setup --json`` 查询各平台**声明**的必需 token 与能力。
    这样新增平台只要写好适配器就会被状态视图自动列出，不必改核心文件。
    """
    key = str(name)
    _ensure_loaded(key)
    return _REGISTRY.get(key)


def registered_names() -> tuple[str, ...]:
    """扫描本包内的模块并导入，返回全部已注册适配器名（按字母序）。

    冻结的 ``/setup`` 菜单刻意只列三平台（那是人工维护的引导文案），但
    ``--status`` / ``--setup --json`` 是运行时视图，应该**自动**反映所有可用
    平台，否则新加的平台用户在状态里根本看不到。

    某个模块导入失败只记 warning 并跳过 —— 状态视图不该被一个坏适配器整个拖垮。
    """
    # 注意：base 是**模块**而非包，没有 ``__path__``；要扫的是本包所在目录。
    for mod in pkgutil.iter_modules([os.path.dirname(os.path.abspath(__file__))]):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        try:
            importlib.import_module(f"{__package__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - 状态视图要尽量出得来
            logger.warning("adapters: 跳过无法导入的 %s: %s", mod.name, exc)
    return tuple(sorted(_REGISTRY))


def build(name: str, config: dict, hooks: Hooks) -> Adapter:
    """Registry lookup: ``telegram`` / ``slack`` / ``discord`` / ...

    Unknown ``name`` raises :class:`KeyError`. Adapter modules are imported
    lazily on first use so that importing this module alone stays free of
    circular imports.
    """
    key = str(name)
    _ensure_loaded(key)

    if key not in _REGISTRY:
        raise KeyError(f"unknown adapter: {name!r}")
    try:
        cls = _REGISTRY[key]
        return cls(config, hooks)
    except KeyError:
        raise
    except Exception as exc:  # configuration errors -> AdapterError
        raise AdapterError(f"failed to build adapter {name!r}: {exc}") from exc
