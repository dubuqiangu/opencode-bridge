"""配对的**接线层**：适配器闸门 + 14 处调用点 + ``--pair`` 兑换。

这里守的是三件接线的事，:mod:`tests.test_pairing`（纯函数那层）都管不到：

1. **结构**：每一个闸门调用点都必须长成「``admits`` 或配对分支」的样子 ——
   新增一个绕过配对的闸门调用点，这里会红。
2. **行为**：未授权会话发 ``/pair`` 真的收到配对文本；``pairing_supported = False``
   的平台**不**回。
3. **兑换**：``--pair`` 用正确的码写入、用错误的码**一个字都不写**、配置不存在时
   **不创建**。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from typing import Any

from opencode_bridge import __main__ as cli
from opencode_bridge import pairing_cli
from opencode_bridge.adapters import build
from opencode_bridge.pairing_cli import run_pair
from opencode_bridge.config import ADAPTER_SCOPED_TOP_LEVEL_KEYS, Config, adapter_scoped_config
from opencode_bridge.pairing import PAIRING_SECRET_KEY, derive_pairing_code

SECRET = "本机配对密钥-勿外传"
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 三条判据全部成立的平台 ⇒ 应当 opt-in 配对。
PAIRING_ENABLED_PLATFORMS = (
    "telegram", "slack", "discord", "matrix", "mattermost", "ntfy", "email",
)
#: 三条判据里至少一条不成立的平台 ⇒ 必须**显式** False。
PAIRING_DISABLED_PLATFORMS = (
    "irc", "twitch", "nextcloud", "homeassistant", "a2a", "qqbot",
)

#: 全部已注册的适配器（结构断言要覆盖的集合）。
ALL_PLATFORMS = PAIRING_ENABLED_PLATFORMS + PAIRING_DISABLED_PLATFORMS


def _is_docstring_stmt(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


class RecordingHooks:
    """只收入站。"""

    def __init__(self) -> None:
        self.inbounds: list[Any] = []

    def on_inbound(self, inbound: Any) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


def build_capturing(platform: str, config: dict) -> tuple[Any, RecordingHooks]:
    """建一个适配器，并把它的**配对投递**换成"记下文本"。

    ⚠️ 为什么换掉 :meth:`Adapter._deliver_pairing_reply` 而不是去 stub 网络层：
    各家 ``send`` 走的是真实 HTTP / WebSocket。抓"那条码真的投出去了"这件事，
    断言**投递缝收到的文本**就够，而让测试真的发请求只会让套件依赖网络。

    缝隙本身另有一条用例（``test_the_delivery_seam_uses_the_real_send_path``）
    钉住它确实调 :meth:`Adapter.send`，免得这里变成"测的是一个假的替身"。
    """
    hooks = RecordingHooks()
    adapter = build(platform, config, hooks)
    delivered: list[str] = []
    adapter._delivered_pairing_replies = delivered  # type: ignore[attr-defined]
    adapter._deliver_pairing_reply = (  # type: ignore[method-assign]
        lambda conversation_id, text: delivered.append(text)
    )
    return adapter, hooks


def outbound_texts(adapter: Any) -> list[str]:
    return list(getattr(adapter, "_delivered_pairing_replies", []))


class TestEveryGateCallSiteOffersPairing(unittest.TestCase):
    """⚠️ **结构断言**：14 处调用点每一处都必须改到了。

    这条防的是一类具体事故：将来有人新增一条入站路径（或在某个平台里再写一次
    闸门），只写 ``if not self.admits(x): return`` —— 那条路径上 ``/pair``
    永远收不到回信，而**没有任何测试会红**（闸门本身是对的，配对只是没接上）。

    所以这里不看运行时行为，只看**源码形状**：每个 ``admits`` / ``_allowed``
    调用点都必须与一个配对调用点在同一条 ``if`` 里。
    """

    #: 期望的调用点数。改动时**必须**同时改这里 —— 那正是"新增了一条入站路径，
    #: 你要决定它要不要支持配对"的时刻。
    EXPECTED_GATE_CALL_SITES = 14

    @staticmethod
    def _gate_calls(source_path: str) -> list[tuple[int, str]]:
        """该文件里所有**入站路径上**的 ``admits(...)`` / ``_allowed(...)`` 调用点。

        ⚠️ 必须排除**私有转发包装**（``telegram._allowed`` 里的 ``return
        self.admits(chat_id)``）：那不是一条入站路径，而是基类闸门在 telegram
        身上的一层壳。把它算进来会让本该是 14 的数字变成 15 —— 而**恒错的期望值
        比没有断言更坏**（它会让人以为"计数漂了"，于是下调期望值去迁就实现）。

        排除方式是"该调用所在函数的 ``return`` 直接返回它"，而不是按名字黑名单
        —— 后者会在有人改名时静默失效（§7.1）。
        """
        source = io.open(source_path, encoding="utf-8").read()
        tree = ast.parse(source)
        lines = source.split("\n")

        # 哪些函数是"纯转发壳"：函数体里唯一的语句就是 return 那个调用。
        forwarding_lines: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [
                stmt for stmt in node.body if not _is_docstring_stmt(stmt)
            ]
            if len(body) != 1 or not isinstance(body[0], ast.Return):
                continue
            value = body[0].value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr in ("admits", "_allowed"):
                    for lineno in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                        forwarding_lines.add(lineno)

        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("admits", "_allowed"):
                continue
            if node.lineno in forwarding_lines:
                continue
            found.append((node.lineno, lines[node.lineno - 1]))
        return found

    def _adapter_sources(self) -> list[str]:
        adapters_dir = os.path.join(PACKAGE_DIR, "opencode_bridge", "adapters")
        return [
            os.path.join(adapters_dir, name)
            for name in sorted(os.listdir(adapters_dir))
            if name.endswith(".py") and name not in ("__init__.py", "base.py")
        ]

    def test_the_expected_number_of_gate_call_sites_exists(self):
        """⚠️ 先确认**判据本身**找得到东西 —— 匹配器坏了会假装"全都改到了"（§7.1）。"""
        all_sites: list[tuple[str, int]] = []
        for path in self._adapter_sources():
            for lineno, _line in self._gate_calls(path):
                all_sites.append((os.path.basename(path), lineno))
        self.assertEqual(
            len(all_sites), self.EXPECTED_GATE_CALL_SITES,
            f"闸门调用点数变了（找到 {len(all_sites)} 处，期望 "
            f"{self.EXPECTED_GATE_CALL_SITES} 处）。若确实新增/删除了一条入站路径，"
            f"请同步改这个数字并逐处决定它要不要支持配对。实际：{all_sites}",
        )

    def test_no_gate_call_site_skips_the_pairing_branch(self):
        """每一处闸门都必须与配对分支同在一条 ``if`` 里。"""
        offenders: list[str] = []
        for path in self._adapter_sources():
            platform = os.path.splitext(os.path.basename(path))[0]
            source = io.open(path, encoding="utf-8").read()
            lines = source.split("\n")
            for lineno, _line in self._gate_calls(path):
                # 该闸门必须被一个 ``if not self.admits(...) and not
                # self.answer_pairing_request(...)`` 包住。逐行往上找一个覆盖窗口
                # （本仓库一律是多行括号续写，窗口取 4 行足够且不会误吞下一个闸门）。
                window = "\n".join(lines[max(0, lineno - 4) : lineno])
                if "answer_pairing_request" in window:
                    continue
                offenders.append(f"{platform}:{lineno}")
        self.assertEqual(
            offenders, [],
            "这些闸门调用点没有配对分支 ⇒ 它们的入站路径上 /pair 永远收不到回信，"
            f"而且不会有任何其它测试报警：{offenders}",
        )

    def test_the_pairing_branch_always_guards_the_gate(self):
        """反向：配对分支**不许**出现在闸门之外。"""
        offenders: list[str] = []
        for path in self._adapter_sources():
            platform = os.path.splitext(os.path.basename(path))[0]
            source = io.open(path, encoding="utf-8").read()
            lines = source.split("\n")
            for lineno, line in enumerate(lines, start=1):
                if "answer_pairing_request" not in line:
                    continue
                window = "\n".join(lines[lineno - 1 : lineno + 3])
                if "admits(" in window or "_allowed(" in window:
                    continue
                offenders.append(f"{platform}:{lineno}")
        self.assertEqual(
            offenders, [],
            f"配对调用点必须与闸门同在一条 if 里（否则会绕过闸门）：{offenders}",
        )


class TestPlatformOptIn(unittest.TestCase):
    """``pairing_supported`` 必须**逐平台显式**声明，不能靠继承默认值蒙混。"""

    def test_enabled_platforms_opt_in(self):
        for platform in PAIRING_ENABLED_PLATFORMS:
            with self.subTest(platform=platform):
                adapter = build(platform, {"bot_token": "t"}, RecordingHooks())
                self.assertTrue(
                    adapter.pairing_supported,
                    f"{platform} 三条判据都成立，必须 opt-in",
                )

    def test_qqbot_is_closed_because_a_group_id_is_shared_by_every_member(self):
        """⚠️ qqbot = False 是**已裁决**的结论，这条把裁决理由钉住。

        它的 principal（``group_openid``）确实唯一且稳定 —— 但**同一个群里的任何人
        都共享它**，所以授权它等于把整个群授权进白名单。
        """
        hooks = RecordingHooks()
        adapter = build(
            "qqbot",
            {
                "app_id": "id", "app_secret": "secret",
                PAIRING_SECRET_KEY: SECRET, "config_version": 2,
            },
            hooks,
        )
        self.assertFalse(adapter.pairing_supported)
        self.assertFalse(
            adapter.answer_pairing_request("GROUP", "qqbot:GROUP", "/pair"),
            "qqbot 上 /pair 不得触发配对回信",
        )
        self.assertEqual(outbound_texts(adapter), [])

    def test_disabled_platforms_say_so_explicitly(self):
        """⚠️ 显式 ``False``：让"关"是一个**声明**，而不是"忘了写"。

        判据取自 :attr:`~opencode_bridge.adapters.base.Adapter.pairing_supported`。
        """
        reasons = {
            "irc": "IRC 无认证；私聊 principal 是 bot 自己的 nick",
            "twitch": "Twitch IRC 通道不认证用户",
            "nextcloud": "principal 是 OCS token，用户不该知道",
            "homeassistant": "principal 是 entity_id",
            "a2a": "principal 是对端自报的 peer",
            # ⚠️ qqbot 不是"principal 不稳定"，而是"principal 被群内所有人共享"——
            # 两条不同的理由，所以它的裁决单独立了一条用例。
            "qqbot": "group_openid 被群内所有成员共享",
        }
        for platform in PAIRING_DISABLED_PLATFORMS:
            with self.subTest(platform=platform, reason=reasons[platform]):
                adapter = build(platform, {"bot_token": "t"}, RecordingHooks())
                self.assertFalse(
                    adapter.pairing_supported,
                    f"{platform} 必须显式关闭：{reasons[platform]}",
                )

    def test_no_unlisted_platform_inherits_the_default(self):
        """每个平台都必须在上面两张表里恰好出现一次。"""
        listed = set(PAIRING_ENABLED_PLATFORMS) | set(PAIRING_DISABLED_PLATFORMS)
        self.assertEqual(
            listed, set(ALL_PLATFORMS),
            "平台表与实际注册的适配器集合不符 —— 有新平台没被点名",
        )
        self.assertEqual(
            set(PAIRING_ENABLED_PLATFORMS) & set(PAIRING_DISABLED_PLATFORMS),
            set(),
            "同一个平台不能既开又关",
        )

    def test_disabled_platform_does_not_answer_pairing(self):
        """⚠️ 行为层：关闭的平台上，``/pair`` **不**触发配对文本。"""
        for platform in PAIRING_DISABLED_PLATFORMS:
            with self.subTest(platform=platform):
                hooks = RecordingHooks()
                adapter = build(
                    platform,
                    {PAIRING_SECRET_KEY: SECRET, "config_version": 2, "bot_token": "t"},
                    hooks,
                )
                answered = adapter.answer_pairing_request(
                    "some-principal", f"{platform}:some-principal", "/pair"
                )
                self.assertFalse(answered, "配对已禁用 ⇒ 不得声称已回信")
                self.assertEqual(outbound_texts(adapter), [], "且不许发出任何回信")


class TestPairingReplyAtTheGate(unittest.TestCase):
    """未授权会话发 ``/pair`` ⇒ 收到配对文本，且**消息仍被丢弃**。"""

    def _build(self, platform: str, **extra: Any):
        config = {
            "bot_token": "t",
            PAIRING_SECRET_KEY: SECRET,
            "config_version": 2,
            **extra,
        }
        return build_capturing(platform, config)

    def test_unauthorised_chat_asking_for_pairing_gets_a_code(self):
        adapter, hooks = self._build("telegram")
        conversation_id = adapter._conversation_id("12345")
        answered = adapter.answer_pairing_request(
            "12345", conversation_id, "/pair"
        )
        self.assertTrue(answered, "未授权 + /pair ⇒ 必须回一条")
        self.assertEqual(hooks.inbounds, [], "回信之后**仍不许**把消息放进来")
        texts = outbound_texts(adapter)
        self.assertEqual(len(texts), 1, f"应恰好回一条，实际 {texts!r}")
        expected = derive_pairing_code(SECRET, "telegram", conversation_id)
        self.assertIn(expected, texts[0], "回信里必须含该会话的正确码")

    def test_the_reply_carries_no_path(self):
        adapter, _hooks = self._build("telegram")
        adapter.answer_pairing_request(
            "12345", adapter._conversation_id("12345"), "/pair"
        )
        text = outbound_texts(adapter)[0]
        self.assertNotIn("\\", text)
        for needle in ("Users", "C:", "config.json", "~"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_the_reply_carries_the_scope_statement(self):
        adapter, _hooks = self._build("telegram")
        conversation_id = adapter._conversation_id("12345")
        adapter.answer_pairing_request("12345", conversation_id, "/pair")
        text = outbound_texts(adapter)[0]
        self.assertIn(conversation_id, text, "必须点名授权哪个会话")
        self.assertIn("群里其他人", text)
        self.assertIn("私聊", text)

    def test_ordinary_unauthorised_message_gets_no_reply(self):
        """⚠️ 关键的反向用例：非 ``/pair`` 的消息**不许**收到配对码。

        误判会让**任何**一条未授权消息都收到一条码 —— 那既是噪音，也把
        "未授权"这件事变成了可被反复触发的动作。
        """
        adapter, hooks = self._build("telegram")
        conversation_id = adapter._conversation_id("12345")
        self.assertFalse(
            adapter.answer_pairing_request("12345", conversation_id, "今天天气")
        )
        self.assertEqual(outbound_texts(adapter), [])

    def test_already_authorised_chat_gets_no_reply(self):
        """已授权的人发 ``/pair`` 不该收到码 —— 他不需要。"""
        adapter, _hooks = self._build("telegram", allowed_chat_ids=["12345"])
        self.assertFalse(
            adapter.answer_pairing_request(
                "12345", adapter._conversation_id("12345"), "/pair"
            ),
            "已授权 ⇒ 闸门本来就放行，不该再回一条码",
        )
        self.assertEqual(outbound_texts(adapter), [])

    def test_without_a_secret_no_pairing_reply_is_sent(self):
        adapter, _hooks = self._build("telegram", **{PAIRING_SECRET_KEY: ""})
        self.assertFalse(
            adapter.answer_pairing_request(
                "12345", adapter._conversation_id("12345"), "/pair"
            )
        )
        self.assertEqual(outbound_texts(adapter), [], "无 secret ⇒ 不提供配对")

    def test_the_delivery_seam_uses_the_real_send_path(self):
        """⚠️ 守住 :meth:`Adapter._deliver_pairing_reply` 真的调 :meth:`send`。

        没有这条，上面那些用例就只是在测一个**替身**：哪天有人把投递缝改成
        ``logger.info``，配对码就再也不出门了，而全部用例照样绿。
        """
        adapter = build(
            "telegram",
            {"bot_token": "t", PAIRING_SECRET_KEY: SECRET, "config_version": 2},
            RecordingHooks(),
        )
        sent: list[Any] = []
        adapter.send = lambda outbound: sent.append(outbound) or None  # type: ignore[method-assign]
        conversation_id = adapter._conversation_id("12345")
        adapter.answer_pairing_request("12345", conversation_id, "/pair")
        self.assertEqual(len(sent), 1, "必须真的走 send() 出去")
        self.assertEqual(sent[0].conversation_id, conversation_id)
        self.assertIn(
            derive_pairing_code(SECRET, "telegram", conversation_id), sent[0].text
        )

    def test_a_failing_delivery_does_not_escape(self):
        """配对是**旁路**：投递炸了不许打断轮询线程。"""
        adapter = build(
            "telegram",
            {"bot_token": "t", PAIRING_SECRET_KEY: SECRET, "config_version": 2},
            RecordingHooks(),
        )

        def _boom(outbound: Any) -> None:
            raise RuntimeError("网络炸了")

        adapter.send = _boom  # type: ignore[method-assign]
        conversation_id = adapter._conversation_id("12345")
        self.assertTrue(
            adapter.answer_pairing_request("12345", conversation_id, "/pair"),
            "投递失败也仍算「已回过」 —— 调用方照旧别把消息放进来",
        )

    def test_legacy_config_still_admits_and_needs_no_pairing(self):
        """旧语义下（无 ``config_version``）闸门照旧放行，配对分支不会被走到。"""
        adapter, _hooks = self._build("telegram", config_version=0)
        self.assertTrue(adapter.admits("12345"), "无 config_version ⇒ 仍是空 = 全开")


class TestIrcPrivateMessages(unittest.TestCase):
    """irc 私聊（target = bot 自己的 nick）在新语义下被拒 + 有启动告警。"""

    IRC_CONFIG = {
        "host": "irc.example.test",
        "nick": "opencodebot",
        "channels": ["#lab"],
        PAIRING_SECRET_KEY: SECRET,
    }

    def test_private_message_is_rejected_under_the_new_semantics(self):
        adapter = build("irc", {**self.IRC_CONFIG, "config_version": 2}, RecordingHooks())
        self.assertFalse(adapter.pairing_supported, "IRC 无认证 ⇒ 配对已禁用")
        # 私聊的 target 就是 bot 自己的 nick（见 irc._handle_line 的 is_private）。
        self.assertFalse(
            adapter.admits("opencodebot"),
            "私聊 principal = bot 自己的 nick；新语义下不得放行",
        )

    def test_private_message_still_works_under_the_legacy_semantics(self):
        adapter = build("irc", dict(self.IRC_CONFIG), RecordingHooks())
        self.assertTrue(
            adapter.admits("opencodebot"),
            "旧语义（无 config_version）下私聊照旧可用 —— 不许突然打断既有用户",
        )

    def test_startup_warns_that_private_messages_are_unreachable(self):
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.adapters.irc")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            build("irc", {**self.IRC_CONFIG, "config_version": 2}, RecordingHooks())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        joined = "\n".join(warnings)
        self.assertTrue(warnings, "私聊不可用这件事必须**响亮** —— 半通不通最糟")
        self.assertIn("私聊", joined, "必须点名私聊")
        self.assertIn("allowed_chat_ids", joined, "必须指出该动哪个键")

    def test_no_warning_under_the_legacy_semantics(self):
        """旧语义下私聊能用 ⇒ 喊"私聊不可用"是**假话**。"""
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.adapters.irc")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            build("irc", dict(self.IRC_CONFIG), RecordingHooks())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(
            [r.getMessage() for r in records if "私聊不可用" in r.getMessage()], []
        )

    def test_no_warning_when_the_allowlist_is_populated(self):
        """清单非空时私聊本来就有入口 ⇒ 喊"进不来"是假话。

        ⚠️ 清单里那一条**刻意用频道名**而不是本适配器自己的 nick：后者会被
        :class:`~opencode_bridge.adapters.base.AdapterError` 直接拒掉
        （见 :mod:`tests.test_nick_in_allowlist`）—— 而这条要验的是
        "清单非空 ⇒ 不发那条告警"，用频道名才验得到它本来要验的东西。
        """
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.adapters.irc")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            build(
                "irc",
                {**self.IRC_CONFIG, "config_version": 2, "allowed_chat_ids": ["#lab"]},
                RecordingHooks(),
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(
            [r.getMessage() for r in records if "私聊不可用" in r.getMessage()], []
        )


class TestTopLevelScalarsReachAdapters(unittest.TestCase):
    """顶层标量（``pairing_secret`` / ``config_version``）怎么到适配器手里。"""

    def test_the_projection_covers_exactly_those_two_keys(self):
        self.assertEqual(
            set(ADAPTER_SCOPED_TOP_LEVEL_KEYS),
            {PAIRING_SECRET_KEY, "config_version"},
        )

    def test_projection_injects_the_scalars(self):
        cfg = Config(pairing_secret=SECRET, config_version=2)
        projected = adapter_scoped_config(cfg, {"bot_token": "t"})
        self.assertEqual(projected[PAIRING_SECRET_KEY], SECRET)
        self.assertEqual(projected["config_version"], 2)
        self.assertEqual(projected["bot_token"], "t", "原有键必须原样保留")

    def test_projection_does_not_mutate_the_source(self):
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        before = json.dumps(cfg.adapters, sort_keys=True)
        adapter_scoped_config(cfg, cfg.adapters["telegram"])
        self.assertEqual(
            json.dumps(cfg.adapters, sort_keys=True), before,
            "投影不许改 cfg.adapters —— 否则 --status 读到的不是用户写的那份",
        )

    def test_top_level_wins_and_says_so(self):
        """子树里同名且值不同 ⇒ 顶层胜出 + 记一条 warning。"""
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.config")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            projected = adapter_scoped_config(
                Config(pairing_secret=SECRET),
                {"bot_token": "t", PAIRING_SECRET_KEY: "子树里那个不被用"},
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(projected[PAIRING_SECRET_KEY], SECRET, "顶层一律胜出")
        self.assertTrue(
            any("覆盖" in r.getMessage() for r in records),
            "一个「看着生效了其实没生效」的授权键比没有它危险得多 ⇒ 必须说出来",
        )

    def test_adapters_built_through_the_projection_see_the_flip(self):
        """端到端：投影后的子树建出的适配器，空清单**真的**全拒。"""
        cfg = Config(
            pairing_secret=SECRET,
            config_version=2,
            adapters={"telegram": {"bot_token": "t"}},
        )
        entry = cfg.adapters["telegram"]
        adapter = build("telegram", adapter_scoped_config(cfg, entry), RecordingHooks())
        self.assertFalse(adapter.admits("12345"))


class TestPairCliRedemption(unittest.TestCase):
    """``--pair``：正确码写入、错误码**一个字都不写**、配置不存在**不创建**。"""

    def setUp(self) -> None:
        self.bridge_dir = tempfile.mkdtemp(prefix="pair-cli-")
        self.addCleanup(shutil.rmtree, self.bridge_dir, ignore_errors=True)
        self.config_path = os.path.join(self.bridge_dir, "config.json")

    def _write_config(self, extra: dict | None = None) -> dict:
        document = {
            "opencode_url": "http://127.0.0.1:4096",
            "log_level": "INFO",
            PAIRING_SECRET_KEY: SECRET,
            "adapters": {"telegram": {"bot_token": "t", "allowed_chat_ids": []}},
        }
        if extra:
            document.update(extra)
        with io.open(self.config_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
        return document

    def _run(self, code: str, conversation: str | None = "telegram:12345"):
        return self._run_in(
            env_config=self.config_path,
            argv=(["--pair", code] + (["--conversation", conversation] if conversation else [])),
        )

    def _run_in(self, *, env_config: str, argv: list[str]) -> int:
        """在**临时目录里**跑一次 CLI，并恢复 cwd 与环境变量。

        cwd 必须切走：``_existing_config_path`` 的查找链里有"当前目录"，而本仓库
        自己就有一份 ``config.json`` —— 不切走的话这条链会捡到**它**，
        于是"文件不存在"那条分支根本走不到（这类假绿本项目踩过）。
        """
        previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        os.environ["OPENCODE_BRIDGE_CONFIG"] = env_config
        previous_cwd = os.getcwd()
        os.chdir(self.bridge_dir)
        try:
            # 收掉 CLI 自己的输出：``--pair`` 会 print 结果与"要重启才生效"，
            # 混进 unittest 的输出里既看不清，也可能被误读成失败信息。
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                return cli.main(argv)
        finally:
            os.chdir(previous_cwd)
            if previous is None:
                os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            else:
                os.environ["OPENCODE_BRIDGE_CONFIG"] = previous

    def _backups(self) -> list[str]:
        return [
            name
            for name in os.listdir(self.bridge_dir)
            if ".bak." in name
        ]

    def _read(self) -> dict:
        with io.open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_correct_code_writes_the_conversation_and_flips_the_version(self):
        self._write_config()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self.assertEqual(self._run(code), 0)
        document = self._read()
        self.assertIn(
            "telegram:12345", document["adapters"]["telegram"]["allowed_chat_ids"]
        )
        self.assertGreaterEqual(
            document["config_version"], 2,
            "同一次写盘必须顺手写上 config_version: 2 —— 否则这次授权本身就要重启"
            "两次才生效",
        )

    def test_correct_code_leaves_a_timestamped_backup(self):
        self._write_config()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self._run(code)
        backups = self._backups()
        self.assertEqual(len(backups), 1, f"应恰好留一份备份，实际 {backups}")
        self.assertRegex(backups[0], r"\.bak\.\d{8}-\d{6}$")
        with io.open(os.path.join(self.bridge_dir, backups[0]), encoding="utf-8") as fh:
            self.assertEqual(
                json.load(fh)["adapters"]["telegram"]["allowed_chat_ids"], [],
                "备份必须是**改动前**的那份",
            )

    def test_correct_code_preserves_the_other_keys(self):
        self._write_config()
        before = self._read()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self._run(code)
        after = self._read()
        self.assertEqual(after["opencode_url"], before["opencode_url"])
        self.assertEqual(after["log_level"], before["log_level"])
        self.assertEqual(
            after["adapters"]["telegram"]["bot_token"], "t",
            "只许改那**一个数组** —— 其余键原样带回",
        )

    def test_existing_entries_are_kept(self):
        self._write_config()
        document = self._read()
        document["adapters"]["telegram"]["allowed_chat_ids"] = ["telegram:777"]
        with io.open(self.config_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self._run(code)
        self.assertEqual(
            sorted(self._read()["adapters"]["telegram"]["allowed_chat_ids"]),
            ["telegram:12345", "telegram:777"],
        )

    def test_wrong_code_writes_nothing_at_all(self):
        """⚠️ 承重：错误的码**不许**留下任何痕迹，连 ``.bak`` 都不许产生。"""
        self._write_config()
        before = self._read()
        self.assertNotEqual(self._run("zzzzzzzz"), 0, "错误的码必须以非零退出")
        self.assertEqual(self._read(), before, "配置一个字都不许变")
        self.assertEqual(self._backups(), [], "错误的码不许产生备份 —— 有备份就意味着动过文件")

    def test_a_code_from_another_conversation_is_refused(self):
        self._write_config()
        other = derive_pairing_code(SECRET, "telegram", "telegram:99999")
        self.assertNotEqual(self._run(other), 0, "别的会话的码不得授权这个会话")
        self.assertEqual(self._read()["adapters"]["telegram"]["allowed_chat_ids"], [])
        self.assertEqual(self._backups(), [])

    def test_a_code_with_odd_spelling_still_works(self):
        """归一化：用户照抄时带空格 / 连字符 / 大写都该过。"""
        self._write_config()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        half = len(code) // 2
        self.assertEqual(self._run(f" {code[:half].upper()}-{code[half:]} "), 0)
        self.assertIn(
            "telegram:12345",
            self._read()["adapters"]["telegram"]["allowed_chat_ids"],
        )

    def test_missing_config_is_an_error_and_is_not_created(self):
        """⛔ 没有配置文件 ⇒ 报错退出，且**绝不**创建它。

        ⚠️ 刻意**同时**断言目录仍然为空：只断言"文件不存在"会被"创建了另一个
        名字"骗过去，而用户看到的是一个凭空多出来的配置文件。
        """
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        exit_code = self._run_in(
            env_config=os.path.join(self.bridge_dir, "does-not-exist.json"),
            argv=["--pair", code, "--conversation", "telegram:12345"],
        )
        self.assertNotEqual(exit_code, 0, "没有可授权的桥 ⇒ 必须报错退出")
        self.assertFalse(
            os.path.exists(self.config_path),
            "⛔ 绝不许凭空创建 config.json —— 用户会以为配好了",
        )
        self.assertEqual(os.listdir(self.bridge_dir), [], "目录必须仍然为空")

    def test_missing_config_branch_is_reachable_with_a_secret(self):
        """⚠️ 守**可达性**：没有配置文件 ⇒ ``Config`` 解析不出 secret。

        若 :func:`run_pair` 先查 secret 再查文件，"文件不存在"那条分支就**永远
        不可达** —— 上面的端到端用例会因为走的是 secret guard 而**假绿**，而
        "绝不创建 config.json" 这条承诺实际上没人测过。这里直接给它一个带
        secret 的 :class:`Config`，把那条分支单独钉住。
        """
        self._write_config()  # 让 cwd 里**有**配置（否则 _existing_config_path 会捡到它）
        os.remove(self.config_path)
        previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        os.environ["OPENCODE_BRIDGE_CONFIG"] = os.path.join(self.bridge_dir, "gone.json")
        previous_cwd = os.getcwd()
        os.chdir(self.bridge_dir)
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                exit_code = run_pair(
                    Config(pairing_secret=SECRET), "abcd2345", "telegram:12345"
                )
        finally:
            os.chdir(previous_cwd)
            if previous is None:
                os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            else:
                os.environ["OPENCODE_BRIDGE_CONFIG"] = previous
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(
            os.listdir(self.bridge_dir), [],
            "⛔ 这条分支绝不许创建任何文件",
        )

    def test_missing_config_says_so_instead_of_blaming_the_secret(self):
        """⚠️ 没有配置文件时**不许**让他去填 pairing_secret —— 那是误导。

        他要先有一个配置文件才谈得上填 secret，而那条报错完全不提这一步。
        """
        self._write_config()
        os.remove(self.config_path)
        previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        os.environ["OPENCODE_BRIDGE_CONFIG"] = os.path.join(self.bridge_dir, "gone.json")
        previous_cwd = os.getcwd()
        os.chdir(self.bridge_dir)
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured):
                cli.run_pair(Config(pairing_secret=SECRET), "abcd2345", "telegram:1")
        finally:
            os.chdir(previous_cwd)
            if previous is None:
                os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            else:
                os.environ["OPENCODE_BRIDGE_CONFIG"] = previous
        message = captured.getvalue()
        self.assertIn(
            "config.json", message,
            "必须点名缺的是哪个文件 —— 否则用户不知道要建什么",
        )
        self.assertNotIn(
            "pairing_secret", message,
            "一个连配置文件都没有的用户，被要求「填 pairing_secret」是误导",
        )

    def test_missing_secret_is_refused(self):
        self._write_config({PAIRING_SECRET_KEY: ""})
        before = self._read()
        self.assertNotEqual(self._run("abcd2345"), 0)
        self.assertEqual(self._read(), before)
        self.assertEqual(self._backups(), [])

    def test_missing_conversation_is_refused_rather_than_guessed(self):
        """⛔ 只凭一串码**无法**确定是哪个会话 ⇒ 不许猜。"""
        self._write_config()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self.assertNotEqual(self._run(code, conversation=None), 0)
        self.assertEqual(self._read()["adapters"]["telegram"]["allowed_chat_ids"], [])
        self.assertEqual(self._backups(), [])

    def test_redeeming_twice_is_idempotent_and_creates_no_second_backup(self):
        self._write_config()
        code = derive_pairing_code(SECRET, "telegram", "telegram:12345")
        self.assertEqual(self._run(code), 0)
        backups_after_first = self._backups()
        self.assertEqual(self._run(code), 0, "重复兑换应当成功（幂等）")
        self.assertEqual(
            self._read()["adapters"]["telegram"]["allowed_chat_ids"],
            ["telegram:12345"],
            "不许写重复项",
        )
        self.assertEqual(
            self._backups(), backups_after_first,
            "已授权 ⇒ 不该动文件 ⇒ 不该多一份备份",
        )

    def test_conversation_splitting_handles_ids_that_contain_colons(self):
        """⚠️ Matrix 的 room id 自身含冒号（``!room:server``）—— 必须按**第一个**切。"""
        self.assertEqual(
            pairing_cli.split_conversation("matrix:!room:example.org"),
            ("matrix", "!room:example.org"),
        )
        self.assertIsNone(pairing_cli.split_conversation("no-platform-prefix"))
        self.assertIsNone(pairing_cli.split_conversation("trailing:"))


if __name__ == "__main__":
    unittest.main()