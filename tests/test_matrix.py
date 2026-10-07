"""T3.1 Matrix adapter tests（无网络：HTTP 层替换 ``_request``）。

A1 迁移（轮询循环收敛到 :mod:`opencode_bridge.transport.PollingTransport`）之后
新增 :class:`TestMatrixMigrationInvariants`：把「行为不变」逐条钉死 —— 退避接线值
（2s **恒定**，不是指数）、``reset_after=0``、stop 快且不泄漏线程、游标先推进再分发、
失败时游标不动、事件过滤 8 条一条不少；以及一条**端到端**用例：切换前落盘的 ``room:``
键在 ``state.json`` 的键迁移之后仍取回同一个会话（切前缀必须与 ``migrate_keys=True``
同一个变更上线）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import build
from opencode_bridge.adapters.matrix import (
    BACKOFF_INTERVAL,
    MESSAGE_LIMIT,
    ROOM_MSG_TYPE,
    SYNC_SOCKET_TIMEOUT,
    SYNC_TIMEOUT_MS,
    MatrixAdapter,
    _classify_matrix_error,
    _retry_after_seconds,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.identity import (
    LEGACY_PREFIXES,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore

ROOM = "!abc:example.org"
ROOM_CID = "matrix:!abc:example.org"
#: 切换**前**的前缀。断言"旧前缀不再出现"一律拿它和**字面量**比，绝不拿
#: ``identity.LEGACY_PREFIXES`` 比 —— 后者被改了就变成恒真（tasks.md 记的教训）。
LEGACY_ROOM_CID = "room:!abc:example.org"
BOT = "@bot:example.org"

MATRIX_LOGGER = "opencode_bridge.adapters.matrix"


class _WarningCollector(logging.Handler):
    """收集 WARNING 及以上的**已渲染**消息。

    ⚠️ 只收本 logger 自己的记录（handler 挂在它上面），所以 ``_request`` 里那句
    「transport error」**不会**混进来 —— 钉「点名 user_id 的那条」时不能被别的
    告警顶掉。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class RecordingHooks:
    """Minimal ``Hooks`` implementation that records every call."""

    def __init__(self, events=None):
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []
        self.events: list[tuple] = events if events is not None else []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)
        self.events.append(("inbound", inbound))

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))


def make_matrix(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {
        "homeserver": "https://matrix.example.org",
        "access_token": "syt_fake_token",
        "user_id": BOT,
    }
    if config:
        cfg.update(config)
    adapter = MatrixAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0  # no artificial sleeps in tests
    adapter.backoff_interval = 0.01
    return adapter, adapter.hooks


# --- sync payload helpers ---------------------------------------------------

def text_event(text="hi", sender="@alice:example.org", event_id="$e1", **extra):
    ev = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1700000000000,
        "content": {"msgtype": "m.text", "body": text},
    }
    ev.update(extra)
    return ev


def sync_payload(events=None, next_batch="s1", room=ROOM, **room_extra):
    room_obj = {"timeline": {"events": list(events or [])}}
    room_obj.update(room_extra)
    return {"next_batch": next_batch, "rooms": {"join": {room: room_obj}}}


class TestMatrixCapabilities(unittest.TestCase):
    """能力声明（T1.1）：调用方据此判断，不再靠 try/except 猜。"""

    def test_capabilities_truthful(self):
        adapter, _ = make_matrix()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "matrix")
        self.assertEqual(caps["label"], "Matrix")
        self.assertEqual(caps["max_message_length"], 4096)
        self.assertEqual(adapter.max_message_length, MESSAGE_LIMIT)
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "/")
        self.assertEqual(adapter.typed_command_prefix, "/")

    def test_registered_in_build_registry(self):
        adapter = build(
            "matrix",
            {"homeserver": "https://x.org", "access_token": "t", "user_id": BOT},
            RecordingHooks(),
        )
        self.assertIsInstance(adapter, MatrixAdapter)
        self.assertEqual(adapter.capabilities()["max_message_length"], 4096)

    def test_homeserver_trailing_slash_stripped(self):
        adapter, _ = make_matrix({"homeserver": "https://matrix.example.org/"})
        self.assertEqual(adapter.homeserver, "https://matrix.example.org")

    def test_answer_is_noop(self):
        adapter, _ = make_matrix()
        self.assertIsNone(adapter.answer("q1"))
        self.assertIsNone(adapter.answer("q1", "text"))


class TestMatrixLifecycle(unittest.TestCase):
    """缺配置 → 告警并返回，不抛异常、不起线程。"""

    def test_missing_homeserver_warns_and_does_not_start(self):
        adapter, _ = make_matrix({"homeserver": ""})
        with self.assertLogs("opencode_bridge.adapters.matrix", level="WARNING") as cm:
            adapter.start()  # must not raise
        self.assertTrue(any("homeserver" in line for line in cm.output))
        self.assertIsNone(adapter.transport)   # 传输层压根没建 → 没有线程
        self.assertFalse(adapter.running)

    def test_missing_access_token_warns_and_does_not_start(self):
        adapter, _ = make_matrix({"access_token": ""})
        with self.assertLogs("opencode_bridge.adapters.matrix", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("access_token" in line for line in cm.output))
        self.assertIsNone(adapter.transport)
        self.assertFalse(adapter.running)

    def test_missing_user_id_warns_by_key_name_and_still_starts(self):
        """缺 ``user_id`` 必须有一条**点名该键**的 WARNING。

        ⚠️ 断言钉的是**文案里出现 ``user_id``**，⛔ 不是「有一条 warning」——
        后者在缺 ``homeserver`` / ``access_token`` 时同样成立（上面那两条用例
        就会命中），等于恒真。

        为什么这条比 email 那条严重一量级：``user_id`` 是 :meth:`_handle_event`
        里过滤自己回声的**唯一**依据（``if self.user_id and sender == self.user_id``）
        ⇒ 空值时**整个条件短路**，桥接自己发出的消息一条都挡不住 ⇒ 无限自问自答。
        而修复前 ``start()`` 不查它、全文件零告警、``tests/`` 零覆盖。
        """
        adapter, _ = make_matrix({"user_id": ""})
        adapter._request = lambda method, path, payload=None, **kw: (
            200, {"next_batch": "s1", "rooms": {}}
        )
        warnings = self.collect_warnings()
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(
            adapter.running,
            "⛔ 行为不许变：缺 user_id 只是**告警**，轮询线程照常起来"
            "（是否改成失败关闭尚未拍板，见台账）",
        )
        self.assertTrue(
            any("user_id" in line for line in warnings.messages),
            "缺 user_id 时日志里没有点名该键的 WARNING：\n  %s"
            % "\n  ".join(warnings.messages),
        )

    def test_a_configured_user_id_produces_no_such_warning(self):
        """⭐ 反向护栏：``user_id`` **填了**就不许打那条告警。

        ⚠️ 少了这一条，那条 WARNING 会退化成常态噪音而被忽略 —— 而它唯一的作用
        就是「这个键没填」那一刻说出来。
        """
        adapter, _ = make_matrix()
        adapter._request = lambda method, path, payload=None, **kw: (
            200, {"next_batch": "s1", "rooms": {}}
        )
        warnings = self.collect_warnings()
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertFalse(
            [line for line in warnings.messages if "user_id" in line],
            "user_id 已配置却仍在告警（它会变成被忽略的噪音）：\n  %s"
            % "\n  ".join(warnings.messages),
        )

    def test_the_user_id_warning_must_describe_the_actual_refusal(self):
        """⭐ 那条告警必须说清**真实后果**，否则它把用户按一个已不存在的症状去排查。

        ⚠️ **这条是刻意与措辞耦合的**（改文案就会红），取舍是明知的：
        「原文那句『keep talking to itself』现在是**假话**」这件事没有别的可机械
        判定的形式 —— 断言「不等于某个字符串」会被未来一次合法的改写误伤。
        ⭐ 而漏掉它的代价**实测存在**：反向证明 V5（把这条告警改回原文）
        **零红** ⇒ 那条一致性此前无人看守。
        """
        adapter, _ = make_matrix({"user_id": ""})
        adapter._request = lambda method, path, payload=None, **kw: (
            200, {"next_batch": "s1", "rooms": {}}
        )
        warnings = self.collect_warnings()
        adapter.start()
        self.addCleanup(adapter.stop)

        named = [line for line in warnings.messages if "user_id" in line]
        self.assertTrue(named, "点名 user_id 的告警不见了：\n  %s"
                        % "\n  ".join(warnings.messages))
        message = named[0]
        self.assertTrue(
            "closed" in message or "dropped" in message,
            "告警必须说清「入站已被拒绝」这个真实后果：\n  %s" % message)
        self.assertNotIn(
            "keep talking to itself", message,
            "⚠️ 原文那句在失败关闭之后已是**假话**（桥不再把自己的消息当入站）"
            "—— 留着会把用户按一个不存在的症状去排查：\n  %s" % message)

    def collect_warnings(self) -> "_WarningCollector":
        """在 matrix logger 上挂一个收集器（由 ``addCleanup`` 摘掉）。

        ⛔ 为什么不用 ``assertLogs`` / ``assertNoLogs``：前者在**零条**记录时直接失败、
        后者在**任何**一条记录时失败，而这里要钉的是「**没有点名 user_id 的**那一条」
        —— 同一时刻别的告警（``/sync`` 失败等）出现与否都不该决定红绿。
        """
        collector = _WarningCollector()
        logger = logging.getLogger(MATRIX_LOGGER)
        logger.addHandler(collector)
        self.addCleanup(logger.removeHandler, collector)
        return collector

    def test_start_spawns_thread_and_stop_joins(self):
        adapter, _ = make_matrix()
        calls: list[str] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append(path)
            return 200, sync_payload([], next_batch="s-loop")

        adapter._request = fake_request
        adapter.start()
        try:
            self.assertTrue(adapter.running)
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.01)
        finally:
            adapter.stop()
        self.assertTrue(calls, "起线程后应至少同步一次")
        self.assertFalse(adapter.running)

    def test_send_without_credentials_is_classified(self):
        adapter, _ = make_matrix({"access_token": ""})
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)


class TestMatrixInbound(unittest.TestCase):
    """``/sync`` 解析、游标推进、过滤、授权。"""

    def _capture(self, adapter, responses):
        calls: list[tuple] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            return responses[min(len(calls) - 1, len(responses) - 1)]

        adapter._request = fake_request
        return calls

    def test_text_message_becomes_inbound(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="你好 matrix")], next_batch="s1"),
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual(len(hooks.inbounds), 1)
        ib = hooks.inbounds[0]
        self.assertIsInstance(ib, Inbound)
        self.assertEqual(ib.conversation_id, ROOM_CID)
        self.assertEqual(ib.text, "你好 matrix")
        self.assertEqual(ib.kind, "text")
        self.assertEqual(ib.user_id, "@alice:example.org")
        self.assertEqual(ib.message_id, "$e1")
        self.assertEqual(ib.platform, "matrix")
        self.assertEqual(ib.raw["event_id"], "$e1")

    def test_sync_request_shape_uses_bearer_and_cursor(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter, [(200, sync_payload([], next_batch="s9"))])
        adapter._sync_once()
        method, path, payload = calls[0]
        self.assertEqual(method, "GET")
        self.assertIsNone(payload, "GET 不应带请求体")
        self.assertTrue(path.startswith("_matrix/client/v3/sync?"), path)
        query = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(query["timeout"], ["30000"])
        self.assertNotIn("since", query, "首次同步不带 since")

    def test_next_batch_cursor_advances_across_calls(self):
        """Matrix 增量同步的核心：第二次请求必须带上第一次返回的 next_batch。"""
        adapter, _ = make_matrix()
        calls = self._capture(
            adapter,
            [
                (200, sync_payload([text_event()], next_batch="s100")),
                (200, sync_payload([], next_batch="s200")),
            ],
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s100")
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s200")

        first = urllib.parse.parse_qs(calls[0][1].split("?", 1)[1])
        second = urllib.parse.parse_qs(calls[1][1].split("?", 1)[1])
        self.assertNotIn("since", first)
        self.assertEqual(second["since"], ["s100"], "第二次请求必须带上第一次的游标")

    def test_cursor_missing_keeps_previous_value(self):
        adapter, _ = make_matrix()
        adapter._since = "s5"
        adapter._request = lambda *a, **k: (200, {"rooms": {}})
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s5", "响应没有 next_batch 时不能丢游标")

    def test_filtered_events_are_ignored(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [
                    text_event(text="我的回声", sender=BOT, event_id="$self"),
                    text_event(text="图片", event_id="$img",
                               content={"msgtype": "m.image", "body": "x", "url": "u"}),
                    {"type": "m.room.member", "event_id": "$member",
                     "sender": "@alice:example.org", "content": {}},
                    {"type": "m.reaction", "event_id": "$react",
                     "sender": "@alice:example.org",
                     "content": {"m.relates_to": {"rel_type": "m.annotation"}}},
                    text_event(text="", event_id="$empty"),
                    text_event(text="edited", event_id="$edit",
                               content={"msgtype": "m.text", "body": "edited",
                                        "m.relates_to": {"rel_type": "m.replace",
                                                         "event_id": "$old"}}),
                    text_event(text="reply", event_id="$reply",
                               content={"msgtype": "m.text", "body": "reply",
                                        "m.relates_to": {"rel_type": "m.thread"}}),
                    text_event(text="ack", event_id="$ack",
                               content={"msgtype": "m.notice", "body": "ack"}),
                    "not-a-dict",
                    text_event(text="真的来了", event_id="$keep"),
                ],
                next_batch="s1",
            ),
        )
        adapter._sync_once()
        self.assertEqual([ib.text for ib in hooks.inbounds], ["真的来了"])

    # ------------------------------------------------------------------
    # 缺 user_id ⇒ 失败关闭（用户 2026-10-07 拍板）
    # ------------------------------------------------------------------
    def test_missing_user_id_closes_inbound_instead_of_letting_echoes_through(self):
        """⭐ 缺 ``user_id`` ⇒ **一条都不转发**，且逐条记下「为什么丢」。

        改之前是 **fail-open**：``if self.user_id and sender == self.user_id``
        在空值时整个短路 ⇒ 回声**挡不住** ⇒ 桥无限自问自答。

        ⛔ 判据不能只写「没转发」—— 那对一个**无脑实现**（连
        ``homeserver`` 都不解析就 return）同样成立 ⇒ 正对面必须有
        :meth:`test_a_configured_user_id_still_forwards`。
        """
        adapter, hooks = make_matrix({"user_id": ""})
        collector = self._collect_matrix_records()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [
                    # ⭐ 这一条 sender 就是 bot 自己：改之前它会被当成入站收回来
                    text_event(text="我的回声", sender=BOT, event_id="$self"),
                    text_event(text="别人的话", sender="@alice:example.org",
                               event_id="$other"),
                ],
                next_batch="s1",
            ),
        )
        adapter._sync_once()

        self.assertEqual([ib.text for ib in hooks.inbounds], [],
                         "缺 user_id 时不许转发任何入站（失败关闭）")
        dropped = [text for text in collector.messages if "dropping inbound" in text]
        self.assertTrue(dropped, "⛔ 不许静默丢弃 —— 每条都要记下「为什么丢」：\n  %s"
                    % "\n  ".join(collector.messages))
        # 逐字点名那个键（⛔ 「有告警」不够：「配置错误」四个字也能通过）
        self.assertTrue(any("user_id" in text for text in dropped),
                        "丢弃原因必须点名 user_id 这个键：\n  %s"
                        % "\n  ".join(dropped))
        # ⚠️ 这里钉的是本仓库**实际**的约定，不是我想当然的那条：
        # `redactable_id()` 给本地侧 id **加平台前缀**（C2：同一条会话仍跨行可关联），
        # ⛔ **不是**哈希/遮蔽 ⇒ 断言「明文不出现」是错的（同族那条
        # non-whitelisted drop 与 nextcloud 的 `_drop_inbound` 都是前缀形态）。
        # ⇒ 这里钉的是「**与同族丢弃点同形**」：前缀形态。
        self.assertTrue(
            any("room=%s" % ("matrix:" + ROOM) in text for text in dropped),
            "房间 id 必须按 redactable_id 的前缀形态记（与同族丢弃点同形）：\n  %s"
            % "\n  ".join(dropped))

    def test_a_configured_user_id_still_forwards(self):
        """⭐⭐ 反面对照：**配齐** ``user_id`` ⇒ 照常转发，一条都不少。

        ⚠️ 这一条才是守住「⛔ 没把正常配置也堵死」的那条 —— 少了它，
        「缺 user_id 不转发」可以靠**无脑地拒绝一切**来满足。
        """
        adapter, hooks = make_matrix()          # 默认 user_id = BOT
        collector = self._collect_matrix_records()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [text_event(text="真的来了", event_id="$keep")], next_batch="s1",
            ),
        )
        adapter._sync_once()

        self.assertEqual([ib.text for ib in hooks.inbounds], ["真的来了"],
                         "配了 user_id 时必须照常转发")
        self.assertEqual([t for t in collector.messages if "dropping inbound" in t], [],
                         "配了 user_id 时不许出现那条丢弃日志")

    def test_a_configured_user_id_still_drops_its_own_echo(self):
        """⭐ 回声过滤**本身**没被失败关闭顺手削掉：bot 自己那条仍然被丢。

        ⛔ 这是防「把 ``if sender == self.user_id`` 整行删掉」那种无脑实现 ——
        那样两条对照都会绿，而 bot 会开始自问自答。
        """
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [
                    text_event(text="我的回声", sender=BOT, event_id="$self"),
                    text_event(text="别人的话", event_id="$other"),
                ],
                next_batch="s1",
            ),
        )
        adapter._sync_once()

        self.assertEqual([ib.text for ib in hooks.inbounds], ["别人的话"],
                         "bot 自己那条必须仍被回声过滤挡掉")

    def _collect_matrix_records(self):
        """收集 matrix logger 上的 INFO 及以上记录（逐条丢弃原因在 info 级）。

        ⚠️ **必须把 logger 本身的 level 降到 INFO**：``logger.isEnabledFor()``
        是在 logger 上判的，早于任何 handler ⇒ 只降 handler 的 level 的话，
        INFO 记录压根不会被发出来（根 logger 默认 WARNING）。
        """
        collector = _WarningCollector()
        collector.setLevel(logging.INFO)
        logger = logging.getLogger(MATRIX_LOGGER)
        previous_level = logger.level
        logger.addHandler(collector)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, collector)
        self.addCleanup(logger.setLevel, previous_level)
        return collector

    def test_authorization_gate_runs_before_inbound(self):
        """授权闸门在最前：白名单外的房间不许进入上层。"""
        hooks = RecordingHooks()
        adapter, _ = make_matrix({"allowed_chat_ids": ["!ok:example.org"]}, hooks)
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="未授权", event_id="$no")], next_batch="s1"),
        )
        adapter._sync_once()
        self.assertEqual(hooks.inbounds, [])

        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [text_event(text="授权", event_id="$yes")],
                next_batch="s2",
                room="!ok:example.org",
            ),
        )
        adapter._sync_once()
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertEqual(hooks.inbounds[0].conversation_id, "matrix:!ok:example.org")

    def test_empty_allowlist_admits_all_without_config_version(self):
        """无 ``config_version`` ⇒ 沿用旧的「空 = 全开」（本夹具不传该键）。"""
        adapter, hooks = make_matrix()
        self.assertTrue(adapter.admits(ROOM))
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="open")], next_batch="s1"),
        )
        adapter._sync_once()
        self.assertEqual(len(hooks.inbounds), 1)

    def test_empty_allowlist_drops_everything_once_config_version_flips(self):
        """``config_version >= 2`` ⇒ 空 = 全拒：**消息根本不该产生 Inbound**。"""
        adapter, hooks = make_matrix(config={"config_version": 2})
        self.assertFalse(adapter.admits(ROOM))
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="open")], next_batch="s1"),
        )
        adapter._sync_once()
        self.assertEqual(len(hooks.inbounds), 0, "全拒语义下不得产生任何 Inbound")

    def test_non_message_rooms_shapes_are_tolerated(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        ROOM: {"state": {"events": [{"type": "x"}]}},
                        "!bad:example.org": "not-a-dict",
                    },
                    "invite": {ROOM: {}},
                    "leave": {ROOM: {}},
                },
            },
        )
        self.assertTrue(adapter._sync_once())  # must not raise
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(adapter._since, "s1")

    def test_sync_http_error_keeps_cursor(self):
        adapter, _ = make_matrix()
        adapter._since = "s7"
        adapter._request = lambda *a, **k: (
            429,
            {"errcode": "M_LIMIT", "error": "slow down", "retry_after_ms": 2000},
        )
        self.assertFalse(adapter._sync_once())
        self.assertEqual(adapter._since, "s7")

    def test_loop_survives_single_sync_exception(self):
        """单次异常绝不能逃出线程：退避后继续，线程仍活着。"""
        adapter, _ = make_matrix()
        adapter._since = ""
        attempts: list[str] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            attempts.append(path)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            return 200, sync_payload([], next_batch="s1")

        adapter._request = fake_request
        adapter.start()
        try:
            deadline = time.time() + 3
            while len(attempts) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(attempts), 2, "异常后应退避并重试")
            self.assertTrue(adapter.running, "异常不得让线程退出")
            self.assertEqual(adapter._since, "s1")
        finally:
            adapter.stop()

    def test_consumer_loop_never_raises_even_if_fetch_always_throws(self):
        """迁移后没有 ``_inbound_loop`` 了；这里改成**更强**的断言。

        原来只是"预置停止位后调用循环方法不抛异常"。现在验证真实的消费线程：
        fetch 每轮都抛异常时，异常不逃出线程、线程不会静默死掉、而且会**持续**
        按退避重试（而不是重试一次就退出）。
        """
        adapter, _ = make_matrix()
        adapter._since = ""
        attempts: list[str] = []

        def boom(*a, **k):
            attempts.append("x")
            raise ValueError("always fails")

        adapter._request = boom
        adapter.start()
        try:
            self.assertTrue(
                wait_until(lambda: len(attempts) >= 3), "应持续按退避重试"
            )
            self.assertTrue(adapter.running, "线程不得静默退出")
            self.assertEqual(adapter._since, "", "全程失败 → 游标一字不动")
            self.assertGreaterEqual(adapter.transport.stats()["errors"], 1)
        finally:
            adapter.stop()

    def test_hook_exception_is_contained(self):
        class ExplodingHooks(RecordingHooks):
            def on_inbound(self, inbound):  # type: ignore[override]
                raise RuntimeError("hook down")

        adapter, _ = make_matrix(hooks=ExplodingHooks())
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event()], next_batch="s1"),
        )
        self.assertTrue(adapter._sync_once())


class TestMatrixSend(unittest.TestCase):
    """出站：URL / payload / txnId / 句柄 / 分片 / 结构化失败。"""

    def _capture(self, adapter, responder=None):
        calls: list[tuple] = []
        counter = {"n": 0}

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            counter["n"] += 1
            if responder is not None:
                return responder(method, path, payload, counter["n"])
            return 200, {"event_id": f"$evt{counter['n']}"}

        adapter._request = fake_request
        return calls

    def test_send_request_shape_and_handle(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = adapter.send(Outbound(ROOM_CID, "你好 matrix"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "matrix")
        self.assertEqual(handle.conversation_id, ROOM_CID)
        self.assertEqual(handle.message_id, "$evt1")

        method, path, payload = calls[0]
        self.assertEqual(method, "PUT")
        head, _, txn = path.rpartition("/")
        self.assertEqual(
            head,
            f"_matrix/client/v3/rooms/{ROOM}/send/m.room.message",
        )
        self.assertEqual(str(uuid.UUID(txn)), txn, "txnId 必须是合法 uuid")
        self.assertEqual(payload, {"msgtype": "m.text", "body": "你好 matrix"})

    def test_send_accepts_bare_room_id(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        adapter.send(Outbound(ROOM, "bare"))
        self.assertIn(f"_matrix/client/v3/rooms/{ROOM}/send/m.room.message/", calls[0][1])

    def test_send_splits_long_text(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        text = ("字" * 60 + "\n") * 80  # 4880 chars > 4096
        handle = adapter.send(Outbound(ROOM_CID, text))
        self.assertGreater(len(calls), 1)
        for _, _, payload in calls:
            self.assertLessEqual(len(payload["body"]), MESSAGE_LIMIT)
        self.assertEqual("".join(p["body"] for _, _, p in calls), text)
        # 每次 PUT 都要有独立 txnId（否则会被 Matrix 当成重试吞掉）
        txns = {c[1].rpartition("/")[2] for c in calls}
        self.assertEqual(len(txns), len(calls))
        self.assertEqual(handle.message_id, f"$evt{len(calls)}")

    def test_send_bad_conversation_id_classified(self):
        adapter, _ = make_matrix()
        result = adapter.send_result(Outbound("room:", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_empty_text_classified(self):
        adapter, _ = make_matrix()
        result = adapter.send_result(Outbound(ROOM_CID, ""))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_http_4xx_classified(self):
        for status, want in ((403, SendError.FORBIDDEN), (404, SendError.NOT_FOUND),
                             (400, SendError.BAD_FORMAT), (413, SendError.TOO_LONG)):
            with self.subTest(status=status):
                adapter, _ = make_matrix()
                self._capture(
                    adapter,
                    lambda m, p, pl, n, s=status: (
                        s, {"errcode": "M_X", "error": "nope"}
                    ),
                )
                result = adapter.send_result(Outbound(ROOM_CID, "hi"))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, want)
                self.assertNotEqual(result.error_kind, SendError.UNKNOWN)
                self.assertEqual(result.error_detail, "nope")

    def test_send_http_5xx_classified_transient(self):
        adapter, _ = make_matrix()
        self._capture(adapter, lambda m, p, pl, n: (503, {"errcode": "M_UNKNOWN",
                                                           "error": "unavailable"}))
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)

    def test_send_transport_error_classified(self):
        adapter, _ = make_matrix()

        def boom(*a, **k):
            return 0, {"errcode": "M_TRANSPORT", "error": "transport error: down"}

        adapter._request = boom
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertNotEqual(result.error_kind, SendError.UNKNOWN)

    def test_send_rate_limit_reports_retry_after(self):
        adapter, _ = make_matrix()
        self._capture(
            adapter,
            lambda m, p, pl, n: (429, {"errcode": "M_LIMIT", "error": "slow down",
                                       "retry_after_ms": 2500}),
        )
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.RATE_LIMITED)
        self.assertEqual(result.retry_after, 2.5)

    def test_send_partial_failure_is_marked_partial(self):
        """分片中途失败：前面成功的分片不能被记成全成功。"""
        adapter, _ = make_matrix()

        def responder(method, path, payload, n, *, timeout=None):
            if n == 1:
                return 200, {"event_id": "$evt1"}
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        self._capture(adapter, responder)
        result = adapter.send_result(Outbound(ROOM_CID, "x" * 9000))
        self.assertTrue(result.ok)
        self.assertTrue(result.partial)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertEqual(result.handle.message_id, "$evt1")

    def test_send_success_result_is_ok(self):
        adapter, _ = make_matrix()
        self._capture(adapter)
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertTrue(result.ok)
        self.assertFalse(result.partial)
        self.assertEqual(result.handle.message_id, "$evt1")


class TestMatrixMigrationInvariants(unittest.TestCase):
    """A1 迁移的「行为不变」清单：逐条钉死。

    退避/重置这类语义没法靠本机计时证明（迁移前就是 2s 这个量级），所以退避
    部分做成**白盒接线断言**（直接读传输层的退避状态机）：迁移最容易出错的
    恰恰是"接线值对不对"，而那正是这些用例要守的东西。
    """

    # ------------------------------------------------------------------
    # 前缀：已从 ``room:`` 切到 ``matrix:``
    # ------------------------------------------------------------------
    def test_conversation_id_uses_the_unified_platform_prefix(self):
        """显式防呆：有人把 ``_conversation_id`` 悄悄改回 ``room:`` 时这里会红。

        ⚠️ "不许出现 ``room:``"这条比的是**字面量**，不是 ``LEGACY_PREFIXES`` 常量 ——
        改了常量断言就恒真（tasks.md 记的教训）。反过来，"归一后等于新 id"那条**必须**
        引用常量：那是 :data:`identity.LEGACY_PREFIXES` 的契约本身。
        """
        self.assertEqual(LEGACY_PREFIXES["room"], "matrix",
                         "room 是旧别名，映射到 matrix")
        self.assertEqual(MatrixAdapter._conversation_id(ROOM), ROOM_CID)
        for room in ("!abc:example.org", "!x.y:host:8443", "+plus:example.org"):
            with self.subTest(room=room):
                self.assertEqual(MatrixAdapter._conversation_id(room),
                                 f"matrix:{room}")
                self.assertFalse(
                    MatrixAdapter._conversation_id(room).startswith("room:"),
                    "旧前缀不许复活：它已从 identity 的登记表里退出，"
                    "新写的键会与迁移后的键对不上",
                )
        # 已是合法新格式：parse_id 收它（含冒号的房间 id 也不影响解析）
        self.assertTrue(is_valid(ROOM_CID))
        self.assertEqual(parse_id(ROOM_CID).platform, "matrix")
        self.assertEqual(parse_id(ROOM_CID).local_id, ROOM)
        self.assertEqual(normalize(ROOM_CID), ROOM_CID, "新格式必须幂等")
        # 旧 id 仍能归一（迁移期在途的旧 conversation_id）
        self.assertEqual(normalize(LEGACY_ROOM_CID, platform_hint="matrix"),
                         ROOM_CID)
        self.assertEqual(MatrixAdapter._room_id(ROOM_CID), ROOM)

    def test_room_id_still_accepts_the_legacy_prefix_and_a_bare_room_id(self):
        """反向解析**必须**继续认旧前缀，否则盘上未投递的消息会被永久丢弃。

        写前收件箱把 ``conversation_id`` 持久化在 SQLite 里：切换前写入、切换后才
        重放的那几行带着 ``room:`` 前缀，认不出来就再也发不出去了。
        """
        for raw, expected in (
            (ROOM_CID, ROOM),
            (LEGACY_ROOM_CID, ROOM),   # 切换前落盘的旧 conversation_id
            (ROOM, ROOM),              # 裸房间 id
            ("room:", None),
            ("matrix:", None),
            ("", None),
            (None, None),
        ):
            with self.subTest(conversation_id=raw):
                self.assertEqual(MatrixAdapter._room_id(raw), expected)

    def test_legacy_room_key_survives_the_prefix_cutover_through_state_migration(self):
        """端到端：切换前落盘的 ``room:`` 键，升级后仍能取回同一个会话。

        ``conversation_id`` 是 :class:`StateStore` 的**不透明键**，所以切前缀必须与
        :class:`StateStore` 的键迁移（``migrate_keys=True``）**同一个变更**上线 ——
        否则已落盘的键全部作废，且不报错、只表现为"agent 突然记错上下文"。

        这里真写一份**旧格式** ``state.json``（模拟升级前的用户磁盘），再用开启迁移
        的 store 重开，断言新 id 取得到、且落盘键已改写成新格式。房间 id 自带冒号，
        所以这条同时钉住"含冒号的 local 段迁移后不漂移"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"sessions": {LEGACY_ROOM_CID: "sess-matrix"},
                           "meta": {}}, fh)

            store = StateStore(path, migrate_keys=True)

            report = store.last_migration
            self.assertEqual(report.migrated, 1)
            self.assertEqual(report.collisions, 0)
            # ⚠️ 用**字面量**断言旧键已消失，而不是拿 LEGACY_PREFIXES 常量比。
            self.assertEqual(store.all_sessions(), {ROOM_CID: "sess-matrix"})
            self.assertNotIn("room:", json.dumps(store.all_sessions()))
            with open(path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["sessions"], {ROOM_CID: "sess-matrix"})

            # 模拟进程重启：新 id 查得到，旧 id 也仍查得到（别名回退是活代码，
            # slack/discord/mattermost 的 channel: 键还在盘上，它不能被删）
            reopened = StateStore(path, migrate_keys=True)
            self.assertEqual(reopened.get_session(MatrixAdapter._conversation_id(ROOM)),
                             "sess-matrix")
            self.assertEqual(reopened.get_session(LEGACY_ROOM_CID), "sess-matrix")
            self.assertEqual(reopened.get_session(LEGACY_ROOM_CID),
                             reopened.get_session(ROOM_CID))

    # ------------------------------------------------------------------
    # 退避接线：2s 恒定（不是指数）
    # ------------------------------------------------------------------
    def test_backoff_wiring_matches_legacy_constant(self):
        adapter, _ = make_matrix()
        adapter.backoff_interval = BACKOFF_INTERVAL      # 2.0（make_irc 风格默认值）
        transport = adapter._make_transport()
        self.assertEqual(transport.min_backoff, BACKOFF_INTERVAL)
        self.assertEqual(transport.max_backoff, BACKOFF_INTERVAL,
                         "max == min ⇒ 退避恒定；迁移前本来就没有指数退避")
        self.assertEqual(transport._idle_delay(), BACKOFF_INTERVAL,
                         "HTTP 失败（fetch 返回 NOTHING）后的重试间隔 = 2s")
        self.assertEqual(transport.reset_after, 0.0,
                         "必须 0：fetch 成功一次就重置退避（连上即重置）")

    def test_backoff_is_constant_not_exponential(self):
        adapter, _ = make_matrix()
        adapter.backoff_interval = BACKOFF_INTERVAL
        transport = adapter._make_transport()
        waits = [transport._next_backoff(survived=False) for _ in range(4)]
        self.assertEqual(waits, [2.0, 2.0, 2.0, 2.0],
                         "连续失败也必须恒定 2s（迁移前只有一个常数 backoff）")

    def test_failed_sync_is_retried_after_the_backoff_interval(self):
        """端到端：HTTP 失败后确实等了一个 backoff_interval 才重试。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 0.1
        at: list[float] = []

        def failing(method, path, payload=None, *, timeout=None):
            at.append(time.monotonic())
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        adapter._request = failing
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 3))
            gaps = [b - a for a, b in zip(at, at[1:])]
            for gap in gaps:
                self.assertGreaterEqual(gap, 0.09, f"失败后没退避：{gaps}")
        finally:
            adapter.stop()

    def test_successful_sync_is_not_delayed_by_the_backoff(self):
        """成功的一轮**不等** backoff（否则入站会被 2s 拖死）。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 5.0                 # 故意设很大：一旦生效就会超时
        at: list[float] = []

        def ok(method, path, payload=None, *, timeout=None):
            at.append(time.monotonic())
            return 200, sync_payload([], next_batch="s1")

        adapter._request = ok
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 5, timeout=3.0),
                            "成功轮次之间不应有 5s 的等待")
            gaps = [b - a for a, b in zip(at, at[1:])]
            self.assertLess(max(gaps), 1.0, f"成功轮次被退避拖住了：{gaps}")
        finally:
            adapter.stop()

    # ------------------------------------------------------------------
    # stop()：快（打断退避等待）+ 不泄漏线程
    # ------------------------------------------------------------------
    def test_stop_interrupts_the_backoff_wait(self):
        """失败轮正在 2s 退避等待里时 stop() 必须立刻返回。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 5.0
        attempts: list[float] = []

        def failing(method, path, payload=None, *, timeout=None):
            attempts.append(time.monotonic())
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        adapter._request = failing
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 1))
        began = time.monotonic()
        adapter.stop()
        self.assertLess(time.monotonic() - began, 2.0,
                        "stop() 没打断退避等待（会白等满 backoff）")
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)
        adapter.stop()                                 # 幂等
        self.assertFalse(adapter.running)

    def test_stop_during_inflight_sync_does_not_leak_the_thread(self):
        """已知限制（迁移前就有）：stop() 打不断在挂起的 ``/sync``。

        至少保证不会永久泄漏线程：这一轮返回后消费线程自然退出。
        """
        adapter, _ = make_matrix()
        started = threading.Event()

        def slow(method, path, payload=None, *, timeout=None):
            started.set()
            time.sleep(0.3)
            return 200, sync_payload([], next_batch="s1")

        adapter._request = slow
        adapter.start()
        self.addCleanup(adapter.stop)
        transport = adapter.transport
        self.assertTrue(started.wait(3), "没进到 /sync")
        adapter.stop()
        self.assertTrue(wait_until(lambda: not transport.running, timeout=5),
                        "在途请求返回后线程必须退出")

    # ------------------------------------------------------------------
    # 游标语义：先推进再分发 / 失败不动
    # ------------------------------------------------------------------
    def test_cursor_advances_before_dispatch_even_if_dispatch_explodes(self):
        """铁律：分发崩了游标也必须**已经**推进（否则整页事件被无限重放）。

        注意 ``_sync_once`` 让分发异常继续往上抛（迁移前也是这样：异常由循环体
        的 try/except 接住），所以这里断言的是**游标状态**，不是返回值。
        """
        adapter, _ = make_matrix()
        adapter._since = ""
        paths: list[str] = []

        def ok(method, path, payload=None, *, timeout=None):
            paths.append(path)
            return 200, sync_payload([text_event(text="hi", event_id="$e1")],
                                     next_batch="s42")

        adapter._request = ok

        def boom(data):
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_sync = boom
        with self.assertRaises(RuntimeError):
            adapter._sync_once()
        self.assertEqual(adapter._since, "s42", "游标必须先推进（分发崩了也一样）")
        # 恢复后下一次请求必须带上 s42 ⇒ 服务器不会把那页事件再发一遍
        adapter._dispatch_sync = lambda data: None
        self.assertTrue(adapter._sync_once())
        query = urllib.parse.parse_qs(paths[-1].split("?", 1)[1])
        self.assertEqual(query["since"], ["s42"], "必须带上已推进的游标（否则重放）")

    def test_cursor_advance_survives_a_dispatch_blip_in_the_running_loop(self):
        """同一件事在真实消费线程上的版本：分发抛异常不许把线程带走。"""
        adapter, _ = make_matrix()
        adapter._since = ""
        paths: list[str] = []

        def ok(method, path, payload=None, *, timeout=None):
            paths.append(path)
            return 200, sync_payload([text_event(text="hi", event_id="$e1")],
                                     next_batch="s42")

        adapter._request = ok

        def boom(data):
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_sync = boom
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(paths) >= 2, timeout=3.0),
                            "分发异常后仍应继续轮询")
            query = urllib.parse.parse_qs(paths[-1].split("?", 1)[1])
            self.assertEqual(query["since"], ["s42"],
                             "游标已推进 ⇒ 这页事件不会被重放")
            self.assertTrue(adapter.running, "分发异常不得让线程静默退出")
        finally:
            adapter.stop()

    def test_transport_level_failure_leaves_cursor_untouched(self):
        """fetch 抛异常（传输层故障）时游标也不能动。"""
        adapter, _ = make_matrix()
        adapter._since = "s7"

        def boom(*a, **k):
            raise RuntimeError("socket died")

        adapter._request = boom
        self.assertFalse(adapter._sync_once())
        self.assertEqual(adapter._since, "s7")

    def test_cursor_never_moves_on_http_failure_through_the_transport(self):
        """整条链路（传输层线程）上验证：连续失败时游标不动、线程仍重试。"""
        adapter, _ = make_matrix()
        adapter._since = "s7"
        calls: list[str] = []

        def failing(method, path, payload=None, *, timeout=None):
            calls.append(path)
            return 503, {"errcode": "M_UNKNOWN", "error": "unavailable"}

        adapter._request = failing
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(calls) >= 3, timeout=5),
                        "HTTP 失败后必须继续轮询")
        self.assertEqual(adapter._since, "s7", "失败时游标一字不动")
        for path in calls:
            query = urllib.parse.parse_qs(path.split("?", 1)[1])
            self.assertEqual(query.get("since"), ["s7"], "失败时也不能换游标")
        self.assertTrue(adapter.running)

    # ------------------------------------------------------------------
    # 长轮询超时值
    # ------------------------------------------------------------------
    def test_long_poll_timeouts_are_unchanged(self):
        """``timeout=30000`` 与 socket 超时 35s 都不能被迁移改动。

        两者的大小关系是**功能约束**：socket 超时必须大于长轮询挂起时长，
        否则每一轮都会在拿到数据前被本地掐断。
        """
        self.assertEqual(SYNC_TIMEOUT_MS, 30000)
        self.assertEqual(SYNC_SOCKET_TIMEOUT, 35.0)
        self.assertGreater(
            SYNC_SOCKET_TIMEOUT, SYNC_TIMEOUT_MS / 1000.0,
            "socket 超时必须 > 长轮询挂起时长（35s > 30s）",
        )
        adapter, _ = make_matrix()
        seen: dict = {}

        def cap(method, path, payload=None, *, timeout=None):
            seen["timeout"] = timeout
            return 200, sync_payload([], next_batch="s1")

        adapter._request = cap
        adapter._request_sync()
        self.assertEqual(seen["timeout"], SYNC_SOCKET_TIMEOUT,
                         "轮询请求必须把 35s 的 socket 超时传下去")
        query = urllib.parse.parse_qs(adapter._sync_path().split("?", 1)[1])
        self.assertEqual(query["timeout"], ["30000"])

    # ------------------------------------------------------------------
    # 事件过滤：8 条一条不少
    # ------------------------------------------------------------------
    def test_all_eight_event_filters_still_apply(self):
        """一条事件对应一个过滤条件，全塞进同一页，只有一条该被投递。

        过滤条件（与迁移前逐字一致）：① 事件不是 dict ② ``type`` 不是
        ``m.room.message`` ③ ``content`` 不是 dict ④ ``msgtype`` 不是
        ``m.text`` ⑤ 自己发的回声 ⑥ 带 ``m.relates_to``（编辑/回复/表情）
        ⑦ ``body`` 非字符串或为空 ⑧ 房间不在白名单。
        """
        hooks = RecordingHooks()
        adapter, _ = make_matrix({"allowed_chat_ids": [ROOM]}, hooks)
        good = text_event(text="真消息", event_id="$keep")
        stranger = text_event(text="别的房间", event_id="$other")
        adapter._request = lambda *a, **k: (
            200,
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        ROOM: {
                            "timeline": {
                                "events": [
                                    "not-a-dict",                                   # ①
                                    {"type": "m.room.member", "event_id": "$m",      # ②
                                     "content": {"msgtype": "m.text", "body": "x"}},
                                    {"type": ROOM_MSG_TYPE, "event_id": "$c",       # ③
                                     "content": "not-a-dict"},
                                    text_event(text="图", event_id="$img",          # ④
                                               content={"msgtype": "m.image",
                                                        "body": "x", "url": "u"}),
                                    text_event(text="通知", event_id="$n",          # ④
                                               content={"msgtype": "m.notice",
                                                        "body": "n"}),
                                    text_event(text="回声", sender=BOT,            # ⑤
                                               event_id="$self"),
                                    text_event(text="编辑", event_id="$ed",          # ⑥
                                               content={"msgtype": "m.text", "body": "ed",
                                                        "m.relates_to": {
                                                            "rel_type": "m.replace",
                                                            "event_id": "$old"}}),
                                    text_event(text="回复", event_id="$rp",          # ⑥
                                               content={"msgtype": "m.text", "body": "rp",
                                                        "m.relates_to": {
                                                            "rel_type": "m.in_reply_to",
                                                            "event_id": "$old"}}),
                                    {"type": "m.reaction", "event_id": "$an",      # ⑥
                                     "sender": "@alice:example.org",
                                     "content": {"m.relates_to": {
                                         "rel_type": "m.annotation"}}},
                                    text_event(text="", event_id="$empty"),         # ⑦
                                    text_event(text=None, event_id="$nonstr",       # ⑦
                                               content={"msgtype": "m.text",
                                                        "body": 123}),
                                    good,
                                ]
                            }
                        },
                        "!stranger:example.org": {                                    # ⑧
                            "timeline": {"events": [stranger]}
                        },
                    }
                },
            },
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual([ib.text for ib in hooks.inbounds], ["真消息"])
        self.assertEqual(hooks.inbounds[0].conversation_id, ROOM_CID)
        self.assertEqual(hooks.inbounds[0].message_id, "$keep")


class TestMatrixEdit(unittest.TestCase):
    """编辑 = 兼容近似（``"* "`` 前缀 + ``m.new_content``），不是原生 API。"""

    def _capture(self, adapter):
        calls: list[tuple] = []
        counter = {"n": 0}

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            counter["n"] += 1
            return 200, {"event_id": f"$new{counter['n']}"}

        adapter._request = fake_request
        return calls

    def test_edit_uses_fallback_shape(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        result = adapter.edit(handle, Outbound(ROOM_CID, "改好了"))
        method, path, payload = calls[0]
        self.assertEqual(method, "PUT")
        self.assertIn("/send/m.room.message/", path)
        self.assertEqual(payload["msgtype"], "m.text")
        self.assertEqual(payload["body"], "* 改好了")
        self.assertEqual(
            payload["m.new_content"], {"msgtype": "m.text", "body": "改好了"}
        )
        self.assertEqual(
            payload["m.relates_to"],
            {"rel_type": "m.replace", "event_id": "$old"},
        )
        # 返回 True/False（基类契约）；core.py 只判真假
        self.assertTrue(result)

    def test_edit_failure_returns_false_and_classifies(self):
        adapter, _ = make_matrix()
        adapter._request = lambda *a, **k: (403, {"errcode": "M_FORBIDDEN",
                                                  "error": "no power"})
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        self.assertFalse(adapter.edit(handle, Outbound(ROOM_CID, "x")))
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)

    def test_edit_bad_handle_or_empty_text(self):
        adapter, _ = make_matrix()
        self.assertFalse(adapter.edit(MsgHandle("room:", "$old", "matrix"),
                                      Outbound("room:", "x")))
        self.assertFalse(adapter.edit(MsgHandle(ROOM_CID, "$old", "matrix"),
                                      Outbound(ROOM_CID, "")))

    def test_edit_over_limit_degrades_to_plain_send(self):
        """超限 → 退化为普通新消息（无 "* " 前缀、无 m.new_content）。"""
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        text = "字" * (MESSAGE_LIMIT + 100)
        result = adapter.edit(handle, Outbound(ROOM_CID, text))
        self.assertGreater(len(calls), 1, "超限应走分片发送")
        for _, _, payload in calls:
            self.assertNotIn("m.new_content", payload)
            self.assertFalse(payload["body"].startswith("* "))
            self.assertLessEqual(len(payload["body"]), MESSAGE_LIMIT)
        self.assertEqual("".join(p["body"] for _, _, p in calls), text)
        self.assertTrue(result)

    def test_edit_returns_bool_matching_base_contract(self):
        """core.py 用 ``if adapter.edit(...)`` 判成败，基类契约是 ``-> bool``。"""
        adapter, _ = make_matrix()
        self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        self.assertIs(adapter.edit(handle, Outbound(ROOM_CID, "v2")), True)


class TestMatrixErrorHelpers(unittest.TestCase):
    def test_classify_matrix_error_by_errcode(self):
        self.assertEqual(_classify_matrix_error(429, {"errcode": "M_LIMIT"}),
                         SendError.RATE_LIMITED)
        self.assertEqual(_classify_matrix_error(403, {"errcode": "M_FORBIDDEN"}),
                         SendError.FORBIDDEN)
        self.assertEqual(_classify_matrix_error(404, {"errcode": "M_NOT_FOUND"}),
                         SendError.NOT_FOUND)
        self.assertEqual(_classify_matrix_error(413, {"errcode": "M_TOO_LARGE"}),
                         SendError.TOO_LONG)
        self.assertEqual(_classify_matrix_error(400, {"errcode": "M_BAD_JSON"}),
                         SendError.BAD_FORMAT)
        # 未知 errcode → 回落到通用 HTTP 分类
        self.assertEqual(_classify_matrix_error(400, {"errcode": "M_WAT",
                                                     "error": "message is too long"}),
                         SendError.TOO_LONG)
        self.assertEqual(_classify_matrix_error(500, {"errcode": "M_UNKNOWN"}),
                         SendError.TRANSIENT)
        self.assertEqual(_classify_matrix_error(0, "transport error: down"),
                         SendError.TRANSIENT)
        self.assertEqual(_classify_matrix_error(200, "not a dict"),
                         SendError.UNKNOWN)

    def test_retry_after_seconds(self):
        self.assertEqual(_retry_after_seconds({"retry_after_ms": 2500}), 2.5)
        self.assertEqual(_retry_after_seconds({"retry_after_ms": 10 ** 9}), 60.0)
        self.assertIsNone(_retry_after_seconds({"retry_after_ms": -1}))
        self.assertIsNone(_retry_after_seconds({"retry_after_ms": "x"}))
        self.assertIsNone(_retry_after_seconds({}))
        self.assertIsNone(_retry_after_seconds("nope"))

    def test_conversation_id_round_trip(self):
        self.assertEqual(MatrixAdapter._conversation_id(ROOM), ROOM_CID)
        self.assertEqual(MatrixAdapter._room_id(ROOM_CID), ROOM)
        self.assertEqual(MatrixAdapter._room_id(ROOM), ROOM)
        self.assertIsNone(MatrixAdapter._room_id("matrix:"))
        self.assertIsNone(MatrixAdapter._room_id("room:"))
        self.assertIsNone(MatrixAdapter._room_id(""))

    def test_request_builds_bearer_url_without_network(self):
        """真实 ``_request`` 只在 stub 掉 urlopen 时被调用：验证 URL / 头 / 方法。"""
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()
        seen: dict = {}

        class FakeResp:
            status = 200

            def read(self):
                return b'{"event_id": "$real"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["headers"] = req.headers
            seen["body"] = req.data
            seen["timeout"] = timeout
            return FakeResp()

        old = matrix_mod.urllib.request.urlopen
        matrix_mod.urllib.request.urlopen = fake_urlopen
        try:
            status, data = adapter._request(
                "GET", adapter._sync_path(), None, timeout=1.0
            )
        finally:
            matrix_mod.urllib.request.urlopen = old

        self.assertEqual(status, 200)
        self.assertEqual(data, {"event_id": "$real"})
        self.assertTrue(seen["url"].startswith("https://matrix.example.org/_matrix/"))
        self.assertIn("timeout=30000", seen["url"])
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["body"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer syt_fake_token")
        self.assertEqual(seen["timeout"], 1.0)

    def test_request_maps_http_error_and_non_json(self):
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()
        old = matrix_mod.urllib.request.urlopen

        def http_error(req, timeout=None):
            raise matrix_mod.urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, None
            )

        matrix_mod.urllib.request.urlopen = http_error
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = old
        self.assertEqual(status, 429)
        self.assertEqual(data["errcode"], "M_HTTP_429")
        self.assertEqual(_classify_matrix_error(status, data), SendError.RATE_LIMITED)

        def junk(req, timeout=None):
            class R:
                status = 200

                def read(self_inner):
                    return b"<html>nope</html>"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return R()

        matrix_mod.urllib.request.urlopen = junk
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = old
        self.assertEqual(status, 200)
        self.assertEqual(data["errcode"], "M_NOT_JSON")

    def test_request_maps_transport_exception(self):
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()

        def boom(*a, **k):
            raise OSError("connection reset")

        prev = matrix_mod.urllib.request.urlopen
        matrix_mod.urllib.request.urlopen = boom
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = prev
        self.assertEqual(status, 0)
        self.assertEqual(data["errcode"], "M_TRANSPORT")
        self.assertEqual(_classify_matrix_error(status, data), SendError.TRANSIENT)


if __name__ == "__main__":
    unittest.main()
