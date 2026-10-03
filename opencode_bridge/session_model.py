"""``/model`` —— 查看与切换当前会话使用的模型。

``/model`` 的**全部**逻辑都在这里：参数解析、模型目录（``GET /api/model``）的查询
与缓存、以及回复文案的拼装。:class:`~opencode_bridge.core.BridgeCore` 那边只留一个
薄薄的 ``_cmd_model`` 转发——core 已经 50+ 个方法（AGENTS.md §5.1），新功能只能
进新模块。

与 opencode 服务端的两条契约（2026-10-03 运行时 ``GET /openapi.json`` + 真实闭环
调用核实，opencode 2.0.22）：

* ``POST /api/session/{id}/prompt`` 的请求体**没有** ``model`` 字段（完整字段只有
  ``agents / delivery / files / id / metadata / resume / skills / text``），
  所以换模型只能走 ``POST /api/session/{id}/model``；
* 那条路由的请求体是 ``Model.Ref`` **对象**而不是字符串，成功回 **204 No Content**
  （只给 ``providerID`` + ``id`` 时服务端会自己补上 ``variant``），
  因此 :meth:`~opencode_bridge.opencode_client.OpenCodeClient.set_session_model`
  什么都不解析。

模型挂在**服务端**的 session 上，桥这边不需要额外记账：``session_id`` 早就持久化
在 ``state.json`` 里，桥重启后模型原样还在。

权限：走各适配器既有的 ``admits()`` 闸门即可，本模块不加任何额外限制。
"""

from __future__ import annotations

import difflib
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CATALOG_TTL_SECONDS",
    "CatalogEntry",
    "CommandReply",
    "SessionModelCommand",
    "current_model_label",
]

logger = logging.getLogger("opencode_bridge.session_model")

#: 模型目录的缓存时长（秒）。
#:
#: 缓存是必须的：``GET /api/model`` 返回的是**整份**目录（本机实测 819 条，不是
#: 分页接口），而用户为了找一个模型往往会连着发好几次 ``/model`` 试不同关键词。
#: 5 分钟的过期时间换来的是"用户刚装完 provider、配置刚改完，下一次 ``/model``
#: 就看到新清单"——模型清单本来就是低频变化的资源。
CATALOG_TTL_SECONDS = 300.0

#: 搜索命中时最多回多少行。IM 消息有长度上限，而几百行目录没人看得下去；
#: 真的需要看全部就让用户换一个更精确的关键词。
SEARCH_RESULT_LIMIT = 10

#: ``provider/id`` 写错时给出的"你是不是想找"条数。
SUGGESTION_LIMIT = 5

#: ``difflib`` 认为"足够像"的相似度下限，用来滤掉只是长度碰巧相同的无关串。
_SIMILARITY_CUTOFF = 0.6

#: 服务端没告诉我们当前模型时的说法。不写"?"是因为用户看不懂那个符号。
UNKNOWN_MODEL_LABEL = "未知"

_USAGE_TEXT = (
    "用法: /model 查看当前模型，"
    "/model <provider>/<id> 切换模型，"
    "/model <关键词> 搜索可用模型。"
)

_HINT_TEXT = (
    "切换模型: /model <provider>/<id>"
    "    查找模型: /model <关键词>"
)


@dataclass(frozen=True)
class CatalogEntry:
    """``GET /api/model`` 里的一个模型，字段名用我们自己的读法。

    服务端字段叫 ``providerID`` / ``id`` / ``name``；这里改成 ``provider_id`` /
    ``model_id``，免得跟别的 ``id`` 混淆（协议字段名只在该读它们的地方保留）。
    """

    provider_id: str
    model_id: str
    name: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def matches(self, needle: str) -> bool:
        """``needle``（已转小写）是否命中 provider、id 或显示名任一项。"""
        haystack = f"{self.key} {self.name}".lower()
        return needle in haystack

    def line(self) -> str:
        """回给 IM 的那一行；有显示名就带上，没有就只给 ``provider/id``。"""
        return f"{self.key}  {self.name}" if self.name else self.key


@dataclass(frozen=True)
class CommandReply:
    """``/model`` 的一条回复。core 那边只负责把它发出去，不看内容。"""

    text: str
    kind: str = "text"
    session_id: str | None = None


def current_model_label(session: Mapping[str, Any]) -> str:
    """会话当前模型的 ``provider/id``；服务端没给就返回"未知"。

    ``GET /api/session/{id}`` 把模型放在 ``model`` 字段，形如
    ``{"providerID": ..., "id": ..., "variant": ...}``。老服务端可能直接给字符串，
    那种照原样回显。
    """
    model = session.get("model") if isinstance(session, Mapping) else None
    if isinstance(model, Mapping):
        provider_id = str(model.get("providerID") or "").strip()
        model_id = str(model.get("id") or "").strip()
        known = "/".join(part for part in (provider_id, model_id) if part)
        return known or UNKNOWN_MODEL_LABEL
    if isinstance(model, str) and model.strip():
        return model.strip()
    return UNKNOWN_MODEL_LABEL


def argument_problem(argument: str) -> str:
    """``/model <参数>`` 的格式问题，返回一句中文说明；没问题是空串。

    写成"返回原因"而不是抛异常，是因为这些**全是用户打错字**，不是程序故障：
    用户要看到一句清楚的话，而不是 ``命令执行失败: IndexError``。
    """
    if any(ch.isspace() for ch in argument):
        return "参数里不能有空格。"
    if argument.count("/") > 1:
        return "模型写法只有一层斜杠，例如 opencode/space-bunny-free。"
    if "/" not in argument:
        return ""  # 搜索关键词：没有斜杠就是合法的
    provider_id, _, model_id = argument.partition("/")
    if not provider_id:
        return "缺少 provider，写法是 <provider>/<id>。"
    if not model_id:
        return "缺少模型 id，写法是 <provider>/<id>。"
    return ""


def _usage_error(problem: str) -> str:
    return f"{problem}\n{_USAGE_TEXT}"


def suggest_entries(
    choice: CatalogEntry, catalog: Sequence[CatalogEntry]
) -> list[CatalogEntry]:
    """给写错的 ``provider/id`` 找最接近的候选，顺序即"最像"。

    先按"同一个模型 id 挂在别的 provider 下"来找——把 provider 写错远比把模型
    id 写错常见，这一条命中时几乎总是用户真正想要的那个。找不到再用 ``difflib``
    比字符串相似度（纯 stdlib，且只在报错这条冷路径上跑）。
    """
    same_model_id = [entry for entry in catalog if entry.model_id == choice.model_id]
    if same_model_id:
        return same_model_id[:SUGGESTION_LIMIT]

    by_key = {entry.key: entry for entry in catalog}
    close_keys = difflib.get_close_matches(
        choice.key, list(by_key), n=SUGGESTION_LIMIT, cutoff=_SIMILARITY_CUTOFF
    )
    if close_keys:
        return [by_key[key] for key in close_keys]

    needle = (choice.model_id or choice.provider_id).lower()
    partial = [entry for entry in catalog if needle in entry.key.lower()]
    return partial[:SUGGESTION_LIMIT]


class SessionModelCommand:
    """``/model`` 命令的全部逻辑（含模型目录缓存）。

    ``client`` 只用到 ``get_session`` / ``set_session_model`` / ``list_models``
    三个方法；``ensure_session`` 由 core 注入，这样本模块不依赖 ``BridgeCore``
    的内部状态，可以脱离 core 单独测试。

    ``clock`` 与 ``ttl_seconds`` 是公开可注入的（与 ``BridgeCore.clock`` 同一套
    做法），测试里把时钟拨快就能验证缓存过期，不必真的等 5 分钟。
    """

    def __init__(
        self,
        client: Any,
        *,
        ensure_session: Callable[[str], str],
        ttl_seconds: float = CATALOG_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ensure_session = ensure_session
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._catalog: tuple[CatalogEntry, ...] | None = None
        self._catalog_fresh_at = 0.0

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def reply_for(self, conversation_id: str, args: str) -> CommandReply:
        """执行一次 ``/model``，返回要回给 IM 的那一条消息。

        三种形态靠"参数里有没有 ``/``"区分：没有参数=查询当前模型，带斜杠=
        切换模型，只有词=搜索模型。
        """
        argument = str(args or "").strip()
        if not argument:
            return self._report_current_model(conversation_id)
        problem = argument_problem(argument)
        if problem:
            return CommandReply(_usage_error(problem), kind="error")
        if "/" in argument:
            provider_id, _, model_id = argument.partition("/")
            return self._switch_model(conversation_id, provider_id, model_id)
        return self._search_models(argument)

    # ------------------------------------------------------------------
    # 查询 / 切换 / 搜索
    # ------------------------------------------------------------------
    def _report_current_model(self, conversation_id: str) -> CommandReply:
        # 走 core 注入的 ``_ensure_session``，与 ``/new`` ``/cd`` 同一条路：
        # 没有会话就先建一个，裸 ``/model`` 才不会因为"还没开始对话"而报错。
        session_id = self._ensure_session(conversation_id)
        session = self._client.get_session(session_id)
        return CommandReply(
            "\n".join([f"当前模型: {current_model_label(session)}", _HINT_TEXT]),
            session_id=session_id,
        )

    def _switch_model(
        self, conversation_id: str, provider_id: str, model_id: str
    ) -> CommandReply:
        catalog = self._catalog_entries()
        choice = CatalogEntry(provider_id=provider_id, model_id=model_id)
        if not catalog:
            # 目录读空了就等于什么都验证不了。这时绝不能"先发了再说"——
            # 用户会拿到一条成功提示，真正失败发生在下一次对话时。
            return CommandReply(
                "opencode 没有返回可用模型清单，无法确认这个模型是否存在，本次未做切换。",
                kind="error",
            )
        if choice.key not in {entry.key for entry in catalog}:
            # 目录里没有就**不发**请求：opencode 会收下它自己不认识的模型，等到
            # 真正发起对话时才失败，那时用户只看到一句莫名其妙的报错。
            return CommandReply(self._not_found_text(choice, catalog), kind="error")

        session_id = self._ensure_session(conversation_id)
        previous_label = self._current_model_label(session_id)
        self._client.set_session_model(session_id, provider_id, model_id)
        logger.info(
            "session %s model switched: %s -> %s",
            session_id, previous_label, choice.key,
        )
        return CommandReply(
            f"已切换模型: {previous_label} -> {choice.key}",
            session_id=session_id,
        )

    def _search_models(self, keyword: str) -> CommandReply:
        catalog = self._catalog_entries()
        needle = keyword.lower()
        matches = [entry for entry in catalog if entry.matches(needle)]
        if not matches:
            return CommandReply(
                f"没有匹配「{keyword}」的模型。opencode 一共提供 {len(catalog)} 个模型，"
                "换个关键词试试。",
                kind="error",
            )
        shown = matches[:SEARCH_RESULT_LIMIT]
        lines = [
            f"匹配「{keyword}」的模型 {len(matches)} 个"
            "（用 /model <provider>/<id> 切换）："
        ]
        lines.extend(f"  {entry.line()}" for entry in shown)
        if len(matches) > len(shown):
            lines.append(
                f"  只列出前 {len(shown)} 个，另有 {len(matches) - len(shown)} 个没显示，"
                "换个更长的关键词试试。"
            )
        return CommandReply("\n".join(lines))

    # ------------------------------------------------------------------
    # 文案
    # ------------------------------------------------------------------
    def _not_found_text(
        self, choice: CatalogEntry, catalog: Sequence[CatalogEntry]
    ) -> str:
        lines = [f"没有这个模型: {choice.key}"]
        suggestions = suggest_entries(choice, catalog)
        if suggestions:
            lines.append("你要找的是不是下面这些：")
            lines.extend(f"  {entry.line()}" for entry in suggestions)
            lines.append("用 /model <provider>/<id> 可以切换。")
        else:
            lines.append(
                f"opencode 一共提供 {len(catalog)} 个模型，"
                "用 /model <关键词> 可以搜索。"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 目录缓存
    # ------------------------------------------------------------------
    def _current_model_label(self, session_id: str) -> str:
        """切换前的模型标签；读不到就报"未知"，但**不让这一步挡住切换本身**。

        老标签只是回复里的一个词。真正失败的信息量在 ``set_session_model`` 上，
        那一步的异常照常往上抛（core 会回"命令执行失败"）。
        """
        try:
            session = self._client.get_session(session_id)
        except Exception as exc:
            logger.warning("cannot read the current model of %s: %s", session_id, exc)
            return UNKNOWN_MODEL_LABEL
        return current_model_label(session)

    def _catalog_entries(self) -> tuple[CatalogEntry, ...]:
        """模型目录，带 TTL 缓存（见 :data:`CATALOG_TTL_SECONDS` 的理由）。"""
        with self._lock:
            if (
                self._catalog is not None
                and self.clock() - self._catalog_fresh_at < self.ttl_seconds
            ):
                return self._catalog
        # 网络调用放在锁外面：一次慢请求不该把并发着的另一个 /model 也堵住。
        fresh = self._load_catalog()
        with self._lock:
            self._catalog = fresh
            self._catalog_fresh_at = self.clock()
            return fresh

    def _load_catalog(self) -> tuple[CatalogEntry, ...]:
        entries: list[CatalogEntry] = []
        for raw_entry in self._client.list_models():
            if not isinstance(raw_entry, Mapping):
                logger.warning("skipping a non-object catalog entry: %r", raw_entry)
                continue
            provider_id = str(raw_entry.get("providerID") or "").strip()
            model_id = str(raw_entry.get("id") or "").strip()
            if not provider_id or not model_id:
                logger.warning("catalog entry without providerID/id: %r", raw_entry)
                continue
            entries.append(
                CatalogEntry(
                    provider_id=provider_id,
                    model_id=model_id,
                    name=str(raw_entry.get("name") or "").strip(),
                )
            )
        logger.info("model catalog loaded: %d models", len(entries))
        return tuple(entries)