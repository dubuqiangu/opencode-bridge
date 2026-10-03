"""email 适配器测试（零真实网络：IMAP/SMTP 全部走可注入方法）。

IMAP / SMTP 的调用面被收敛到两个可覆写的方法：:meth:`EmailAdapter._imap_connect`
（返回连接对象）与 :meth:`EmailAdapter._smtp_send`（真正投递）。测试替换这两个，
**不去 mock 标准库内部** —— 这样测的是协议逻辑本身，而不是 mock 的行为。

覆盖重点（本平台最容易写错的七处）：
1. **防回环**：自己发出的信必须被丢；但用户点"回复"得到的 ``Re: [opencode] ...``
   **必须放行**（否则用户永远无法追问）。且**绝不用 From 地址判断**。
2. **Message-ID 去重**：重复投递只处理一次；集合满时按 FIFO 淘汰最旧。
3. **游标先推进再处理**：处理抛异常也不重放（否则毒邮件 = 死循环）。
4. **游标跨重启存活**：只有**首次**运行才跳过历史；重启必须从已存水位续跑，
   否则每次重启都会把整个未读收件箱悄悄丢掉。
5. **非 ASCII**：中文 Subject / 正文正确编码，且报文最长行不超过 RFC 5322 的 998。
6. **拿不到纯文本就丢**：HTML-only、未知 charset 都不能塞给 agent。
7. **只读**：不设 ``\\Seen``（用 ``BODY.PEEK[]``），且邮箱以 readonly 方式打开。
"""

from __future__ import annotations

import importlib
import os
import smtplib
import tempfile
import unittest

from opencode_bridge.adapters.base import adapter_class, registered_names
from opencode_bridge.adapters.email import (
    DEDUPE_CAPACITY,
    ECHO_PREFIX,
    MESSAGE_LIMIT,
    WRAP_COLUMNS,
    EmailAdapter,
    _BoundedIdSet,
    _max_line_octets,
    _strip_reply_prefixes,
)
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.identity import platform_of
from opencode_bridge.state import StateStore
from opencode_bridge.transport import NOTHING

_email_message = importlib.import_module("email.message")
_email_parser = importlib.import_module("email.parser")
_email_policy = importlib.import_module("email.policy")

EmailMessage = _email_message.EmailMessage
BytesParser = _email_parser.BytesParser
DEFAULT_POLICY = _email_policy.default


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class RecordingHooks:
    def __init__(self):
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))


class FakeIMAP:
    """可编排的假 IMAP 连接：记录调用，按脚本返回。

    只实现适配器真正用到的那几个方法（``uid`` / ``logout`` / ``shutdown``），
    不试图模拟 ``imaplib`` 的全部行为。
    """

    def __init__(self, script=None):
        #: ``[{"search": [...], "fetch": {uid: raw}}]`` 逐轮消耗；用尽后返回空。
        self.script = list(script or [])
        self.calls: list[tuple] = []
        self.logged_out = 0
        self.shutdowns = 0
        self._round = 0
        self._step: dict = {"search": [], "fetch": {}}

    def uid(self, command, *args):
        self.calls.append((command, args))
        if not self.script:
            return ("OK", [b""])
        # ⚠️ 轮次只在 **SEARCH** 上推进，且 FETCH 必须复用 SEARCH 选定的那一段：
        # FETCH 也是一次 ``uid()`` 调用，若跟着一起推进，同一轮的 FETCH 会读到
        # 下一段脚本（表现为"永远取不到正文"）。
        if command == "SEARCH":
            step = self.script[min(self._round, len(self.script) - 1)]
            self._round += 1
            self._step = step
        else:
            step = self._step
        if isinstance(step, BaseException):
            raise step
        if command == "SEARCH":
            return ("OK", [b" ".join(str(u).encode() for u in step.get("search", []))])
        if command == "FETCH":
            uid = int(args[0])
            raw = step.get("fetch", {}).get(uid)
            if raw is None:
                return ("NO", [b"no such message"])
            return ("OK", [(b"%d (UID %d BODY[] {%d}" % (uid, uid, len(raw)), raw), b")"])
        return ("BAD", [b"unsupported"])

    def logout(self):
        self.logged_out += 1
        return ("BYE", [b"bye"])

    def shutdown(self):
        self.shutdowns += 1


class FakeSMTPError(smtplib.SMTPResponseException):
    """带 SMTP 响应码的异常。

    ⚠️ 必须**真的继承** ``smtplib.SMTPResponseException``：适配器的
    :func:`_classify_smtp` 是按真实异常类型分派的（``isinstance``），用一个
    "长得像但不是"的替身会让分类全部落进 ``UNKNOWN`` —— 那就等于没测分类逻辑。
    """

    def __init__(self, code: int, message: str = "err"):
        super().__init__(code, message)


class CursorStoreHooks(RecordingHooks):
    """带真实 :class:`StateStore` 的 hooks：实现那两个**可选**的游标钩子。

    刻意**自己实现**一遍而不是直接用 :class:`BridgeCore`：下面绝大多数用例要验的是
    "适配器 ⇄ 存储"这条契约，把 core 也拉进来会让失败时分不清是谁的问题。
    （``BridgeCore`` 自己那一层由 :class:`TestCoreCursorHookWiring` 单独覆盖。）
    """

    def __init__(self, store: StateStore):
        super().__init__()
        self.store = store
        self.read_scopes: list[str] = []
        self.written_positions: list[tuple[str, int]] = []

    def load_stream_cursor(self, stream_scope: str):
        self.read_scopes.append(stream_scope)
        return self.store.get_meta(stream_scope, "stream_cursor", None)

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        self.written_positions.append((stream_scope, int(position)))
        self.store.set_meta(stream_scope, "stream_cursor", int(position))


class UnusedOpenCodeClient:
    """游标落盘这条路上 core 不会碰 opencode 服务，只需要一个占位实现。"""


class FailingCursorStoreHooks(CursorStoreHooks):
    """落盘必定失败的 hooks（模拟磁盘满 / state.json 不可写）。"""

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        raise OSError("disk full")


class UnreadableCursorStoreHooks(CursorStoreHooks):
    """读取必定失败的 hooks（模拟 state.json 被移走 / 权限不足）。"""

    def load_stream_cursor(self, stream_scope: str):
        raise OSError("state file is gone")


def make_mail(
    *,
    sender="user@example.com",
    subject="问题",
    body="正文内容",
    message_id="<m1@example.com>",
    extra_headers="",
    to="bot@example.com",
) -> bytes:
    """造一封原始邮件字节（走标准库序列化，测的才是真实报文）。"""
    msg = EmailMessage(policy=DEFAULT_POLICY)
    msg["From"] = sender
    msg["To"] = to
    if subject is not None:
        msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if extra_headers:
        for line in extra_headers.splitlines():
            name, _, value = line.partition(":")
            msg[name.strip()] = value.strip()
    msg.set_content(body, subtype="plain", charset="utf-8")
    return msg.as_bytes()


def make_email(*, hooks=None, **cfg) -> tuple[EmailAdapter, RecordingHooks]:
    """造一个不发真实网络的适配器。

    ``hooks`` 默认用 :class:`RecordingHooks`（它**不**实现游标钩子，正好代表
    "没有落盘通道"那种配置）；传入别的实现即可测游标持久化那条路。
    """
    base = {
        "address": "bot@example.com",
        "password": "app-password",
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
    }
    base.update(cfg)
    if hooks is None:
        hooks = RecordingHooks()
    adapter = EmailAdapter(base, hooks)
    adapter.min_interval = 0            # 测试里不要人为 sleep
    return adapter, hooks


def stub_network(adapter: EmailAdapter, imap_script=None, smtp_errors=None):
    """把 IMAP/SMTP 换成替身，并返回两个记录容器。"""
    fake_imap = FakeIMAP(imap_script)
    sent: list[tuple[str, bytes]] = []

    def _connect():
        return fake_imap

    def _send(recipient, raw):
        sent.append((recipient, raw))
        if smtp_errors:
            raise smtp_errors.pop(0)

    adapter._imap_connect = _connect
    adapter._smtp_close = lambda conn: None
    adapter._smtp_send = _send
    return fake_imap, sent


# ----------------------------------------------------------------------
# 能力 / 凭据声明
# ----------------------------------------------------------------------
class TestCapabilities(unittest.TestCase):
    def test_declarations(self):
        adapter, _ = make_email()
        self.assertEqual(adapter.name, "email")
        self.assertEqual(adapter.label, "Email")
        self.assertTrue(adapter.supports_inbound)
        self.assertFalse(adapter.supports_inline_buttons)
        self.assertFalse(adapter.supports_media)

    def test_no_typed_command_prefix(self):
        """邮件没有斜杠命令 —— 留空串，别假装支持。"""
        adapter, _ = make_email()
        self.assertEqual(adapter.typed_command_prefix, "")

    def test_max_message_length_is_the_rfc5322_line_limit(self):
        """998 = RFC 5322 §2.1.1 的**单行**硬上限，不是正文总量上限。"""
        adapter, _ = make_email()
        self.assertEqual(adapter.max_message_length, MESSAGE_LIMIT)
        self.assertEqual(MESSAGE_LIMIT, 998)
        self.assertEqual(adapter.capabilities()["max_message_length"], 998)

    def test_required_tokens_are_real_config_keys(self):
        adapter, _ = make_email()
        self.assertEqual(
            adapter.required_tokens,
            ("address", "password", "imap_host", "smtp_host"),
        )
        self.assertNotIn("bot_token", adapter.required_tokens)

    def test_outbound_tokens_subset_of_required(self):
        """仓库不变量：outbound_tokens ⊆ required_tokens。"""
        adapter, _ = make_email()
        self.assertLessEqual(set(adapter.outbound_tokens), set(adapter.required_tokens))
        self.assertEqual(adapter.outbound_tokens,
                         ("address", "password", "smtp_host"))

    def test_in_registration_table(self):
        self.assertIn("email", registered_names())

    def test_buildable(self):
        from opencode_bridge.adapters import build
        adapter = build("email", {"address": "a@b.com"}, object())
        self.assertIsInstance(adapter, EmailAdapter)

    def test_adapter_class_lookup(self):
        self.assertIs(adapter_class("email"), EmailAdapter)


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_tls_is_on_by_default(self):
        adapter, _ = make_email()
        self.assertEqual(adapter.imap_security, "ssl")
        self.assertEqual(adapter.smtp_security, "ssl")
        self.assertTrue(adapter.verify_tls)

    def test_verify_tls_off_requires_explicit_config(self):
        adapter, _ = make_email(verify_tls=False)
        self.assertFalse(adapter.verify_tls)

    def test_invalid_verify_tls_falls_back_to_secure(self):
        adapter, _ = make_email(verify_tls="banana")
        self.assertTrue(adapter.verify_tls, "非法值必须按 True（校验证书）处理")

    def test_starttls_selects_explicit_ports(self):
        adapter, _ = make_email(imap_security="starttls", smtp_security="starttls")
        self.assertEqual((adapter.imap_port, adapter.smtp_port), (143, 587))

    def test_explicit_port_wins(self):
        adapter, _ = make_email(smtp_port=2525)
        self.assertEqual(adapter.smtp_port, 2525)

    def test_invalid_port_falls_back_to_protocol_default(self):
        adapter, _ = make_email(imap_port="not-a-port")
        self.assertEqual(adapter.imap_port, 993)

    def test_plaintext_security_is_refused(self):
        """非法 TLS 方式**回落成加密**，绝不静默降级到明文。"""
        adapter, _ = make_email(smtp_security="none", imap_security="plain")
        self.assertEqual(adapter.smtp_security, "ssl")
        self.assertEqual(adapter.imap_security, "ssl")

    def test_socket_timeout_has_a_default(self):
        adapter, _ = make_email()
        self.assertGreater(adapter.socket_timeout, 0)

    def test_msgid_domain_survives_idn(self):
        """make_msgid 要求域匹配 [A-Za-z0-9.-]+，IDN 域必须被过滤掉。"""
        adapter, _ = make_email(address="bot@例子.中国")
        domain = adapter._msgid_domain()
        self.assertTrue(domain)
        self.assertTrue(all(c.isascii() for c in domain), domain)

    def test_msgid_domain_falls_back_to_localhost(self):
        adapter, _ = make_email(address="no-at-sign")
        self.assertEqual(adapter._msgid_domain(), "localhost")


# ----------------------------------------------------------------------
# 防回环
# ----------------------------------------------------------------------
class TestLoopPrevention(unittest.TestCase):
    def test_own_echo_is_dropped_by_subject_marker(self):
        """核心场景：自己发出去的信回到收件箱，必须被丢（否则无限自问自答）。"""
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(subject=f"{ECHO_PREFIX} Re: 问题", body="答案"))
        self.assertEqual(hooks.inbounds, [])

    def test_echo_dropped_even_when_sent_to_self(self):
        adapter, hooks = make_email()
        adapter._on_raw(
            make_mail(sender="bot@example.com", subject=f"{ECHO_PREFIX} reply")
        )
        self.assertEqual(hooks.inbounds, [])

    def test_known_sent_message_id_is_dropped_exactly(self):
        """Message-ID 命中"我们发过的" → 最精确的回声判据（主题被改写也认）。"""
        adapter, hooks = make_email()
        adapter._sent.add("<ours@example.com>")
        adapter._on_raw(make_mail(subject="网关改写过的主题",
                                  message_id="<ours@example.com>"))
        self.assertEqual(hooks.inbounds, [])

    def test_user_reply_to_our_mail_is_admitted(self):
        """用户点"回复"→ ``Re: [opencode] ...`` 且 In-Reply-To 指向我们 → 必须放行。

        没有这一条，agent 就只能单向说话，用户永远无法追问。
        """
        adapter, hooks = make_email()
        adapter._sent.add("<ours@example.com>")
        adapter._on_raw(
            make_mail(
                subject=f"Re: {ECHO_PREFIX} Re: 问题",
                body="追问：还有吗？",
                message_id="<user2@example.com>",
                extra_headers="In-Reply-To: <ours@example.com>",
            )
        )
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertIn("追问", hooks.inbounds[0].text)

    def test_user_reply_recognised_via_references_header(self):
        adapter, hooks = make_email()
        adapter._sent.add("<ours@example.com>")
        adapter._on_raw(
            make_mail(
                subject=f"Re: {ECHO_PREFIX} reply",
                message_id="<user3@example.com>",
                extra_headers="References: <orig@example.com> <ours@example.com>",
            )
        )
        self.assertEqual(len(hooks.inbounds), 1)

    def test_forwarded_echo_without_in_reply_to_is_dropped(self):
        """被自动回复/转发绕回来的回声：主题带标记但没指向我们的 Message-ID → 丢。"""
        adapter, hooks = make_email()
        adapter._on_raw(
            make_mail(subject=f"Fwd: {ECHO_PREFIX} reply",
                      message_id="<forwarded@example.com>")
        )
        self.assertEqual(hooks.inbounds, [])

    def test_from_address_is_never_used_as_the_judgement(self):
        """**绝不查 From**：用户多地址互发是正常用法，按 From 丢会误伤。"""
        adapter, hooks = make_email()
        adapter._on_raw(
            make_mail(sender="bot@example.com", subject="普通问题", body="你好")
        )
        self.assertEqual(len(hooks.inbounds), 1,
                         "发件地址与桥接相同不代表这封信是回声")

    def test_multi_address_user_mail_not_dropped(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(sender="alias2@example.com", subject="问题",
                                  message_id="<a2@x>"))
        adapter._on_raw(make_mail(sender="alias3@example.com", subject="问题",
                                  message_id="<a3@x>"))
        self.assertEqual(len(hooks.inbounds), 2)

    def test_echo_prefix_is_case_insensitive(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(subject="[OpenCode] reply"))
        self.assertEqual(hooks.inbounds, [])

    def test_echo_prefix_configurable(self):
        adapter, hooks = make_email(echo_prefix="[mybot]")
        adapter._on_raw(make_mail(subject="[mybot] reply"))
        self.assertEqual(hooks.inbounds, [])

    def test_disabled_prefix_cannot_turn_off_loop_protection(self):
        """**不允许**把 echo_prefix 配成空 —— 那样等于关掉防回环。"""
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING"):
            adapter, hooks = make_email(echo_prefix="")
        self.assertEqual(adapter.echo_prefix, ECHO_PREFIX)
        adapter._on_raw(make_mail(subject=f"{ECHO_PREFIX} reply"))
        self.assertEqual(hooks.inbounds, [], "回声仍必须被丢")


class TestStripReplyPrefixes(unittest.TestCase):
    def test_strips_chained_re_prefixes(self):
        self.assertEqual(
            _strip_reply_prefixes("Re: Re: Fwd: hello"), "hello"
        )

    def test_strips_chinese_prefixes(self):
        self.assertEqual(_strip_reply_prefixes("回复：问题"), "问题")
        self.assertEqual(_strip_reply_prefixes("转发: 问题"), "问题")

    def test_handles_fullwidth_colon_and_spacing(self):
        self.assertEqual(_strip_reply_prefixes("RE : hello"), "hello")

    def test_leaves_plain_subject_untouched(self):
        self.assertEqual(_strip_reply_prefixes("问题"), "问题")
        self.assertEqual(_strip_reply_prefixes("CI 挂了"), "CI 挂了")

    def test_strips_prefix_without_space(self):
        """``Re:able`` 这种没有空格��写法也要剥（各家 MUA 都这么干）。"""
        self.assertEqual(_strip_reply_prefixes("Re:able to help"), "able to help")

    def test_terminates_on_malformed_input(self):
        """畸形输入（``Re:`` 无限自指）不能死循环。"""
        self.assertEqual(_strip_reply_prefixes("Re:" * 200 + "x"), "x")
        # 远超轮数上限的输入也必须收敛（不抛、不挂）
        self.assertTrue(_strip_reply_prefixes("Re:" * 5000 + "x").endswith("x"))


# ----------------------------------------------------------------------
# Message-ID 去重
# ----------------------------------------------------------------------
class TestDeduplication(unittest.TestCase):
    def test_replayed_message_id_is_dropped(self):
        adapter, hooks = make_email()
        raw = make_mail(message_id="<dup@example.com>")
        adapter._on_raw(raw)
        adapter._on_raw(raw)
        self.assertEqual(len(hooks.inbounds), 1, "同一 Message-ID 只处理一次")

    def test_distinct_message_ids_both_delivered(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(message_id="<a@x>"))
        adapter._on_raw(make_mail(message_id="<b@x>"))
        self.assertEqual(len(hooks.inbounds), 2)

    def test_missing_message_id_is_not_deduped(self):
        """没有 Message-ID 的邮件不该被误判为重复（否则第二封就丢了）。"""
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(message_id=None))
        adapter._on_raw(make_mail(message_id=None))
        self.assertEqual(len(hooks.inbounds), 2)

    def test_seen_set_has_a_bounded_capacity(self):
        adapter, _ = make_email()
        self.assertEqual(len(adapter._seen), 0)
        for i in range(DEDUPE_CAPACITY + 50):
            adapter._seen.add(f"<m{i}@x>")
        self.assertEqual(len(adapter._seen), DEDUPE_CAPACITY)

    def test_seen_set_evicts_oldest_first_fifo(self):
        """淘汰策略：满了丢**最旧**的（FIFO），新条目一定留着。"""
        adapter, _ = make_email(dedupe_capacity=3)
        for mid in ("<a@x>", "<b@x>", "<c@x>"):
            adapter._seen.add(mid)
        adapter._seen.add("<d@x>")
        self.assertNotIn("<a@x>", adapter._seen, "最旧的应被淘汰")
        self.assertIn("<d@x>", adapter._seen)

    def test_readding_does_not_grow_the_set(self):
        """重复 add 同一条不得把集合撑大（否则会误触发淘汰）。"""
        adapter, _ = make_email(dedupe_capacity=3)
        for _ in range(5):
            adapter._seen.add("<a@x>")
        self.assertEqual(len(adapter._seen), 1)

    def test_bounded_set_ignores_empty_keys(self):
        bounded = _BoundedIdSet(4)
        bounded.add("")
        bounded.add(None)
        self.assertEqual(len(bounded), 0)


# ----------------------------------------------------------------------
# 游标：UID / 先推进再处理 / 启动不重放
# ----------------------------------------------------------------------
class TestCursor(unittest.TestCase):
    def test_bootstrap_does_not_replay_history(self):
        """首次连上只抬游标，**一封正文都不取**（等价 ntfy 的 since=<now>）。"""
        adapter, _ = make_email()
        fake, _ = stub_network(adapter, [{"search": [10, 20, 30], "fetch": {
            10: make_mail(), 20: make_mail(), 30: make_mail()}}])
        self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertEqual(adapter._uid, 30)
        fetch_calls = [c for c in fake.calls if c[0] == "FETCH"]
        self.assertEqual(fetch_calls, [], "bootstrap 阶段不得取任何正文")

    def test_bootstrap_uses_all_search_not_unseen(self):
        """必须用 UID SEARCH，绝不用 SEARCH UNSEEN（flag 会和人工读信冲突）。"""
        adapter, _ = make_email()
        fake, _ = stub_network(adapter, [{"search": [1]}])
        adapter._fetch_one()
        self.assertEqual(fake.calls[0][0], "SEARCH")
        self.assertIn("ALL", fake.calls[0][1])

    def test_search_uses_uid_range_after_bootstrap(self):
        adapter, _ = make_email()
        adapter._uid = 7
        fake, _ = stub_network(adapter, [{"search": []}])
        adapter._fetch_one()
        command, args = fake.calls[0]
        self.assertEqual(command, "SEARCH")
        self.assertIn("UID", args)
        self.assertIn("8:*", args)

    def test_no_new_mail_returns_nothing(self):
        adapter, _ = make_email()
        adapter._uid = 5
        stub_network(adapter, [{"search": []}])
        self.assertIs(adapter._fetch_one(), NOTHING)

    def test_messages_are_returned_one_at_a_time(self):
        adapter, _ = make_email()
        adapter._uid = 1
        first = make_mail(body="first", message_id="<1@x>")
        second = make_mail(body="second", message_id="<2@x>")
        fake, _ = stub_network(adapter, [{"search": [2, 3], "fetch": {
            2: first, 3: second}}])
        self.assertEqual(adapter._fetch_one(), first)
        calls_before = len(fake.calls)
        self.assertEqual(adapter._fetch_one(), second)
        self.assertEqual(len(fake.calls), calls_before,
                         "队列里还有货时不该再连 IMAP")

    def test_cursor_advances_before_processing(self):
        """先推进再处理：处理阶段抛异常也不该导致下一轮重放。"""
        adapter, hooks = make_email()
        adapter._uid = 1
        raw = make_mail(body="poison", message_id="<poison@x>")
        stub_network(adapter, [{"search": [2], "fetch": {2: raw}}])

        # 手动模拟"先 fetch_one 再处理"的顺序，并在处理阶段抛异常。
        item = adapter._fetch_one()
        self.assertEqual(adapter._uid, 2, "fetch 阶段就必须把游标抬上去")

        def boom(_inbound):
            raise RuntimeError("core 炸了")

        adapter.hooks = type(
            "Boom", (), {"on_inbound": staticmethod(boom),
                         "on_callback": staticmethod(lambda *a: None)}
        )()
        adapter._on_raw(item)          # 抛异常被吞掉，不影响游标
        self.assertEqual(adapter._uid, 2, "处理异常不得回退游标")

    def test_processing_exception_does_not_replay(self):
        """整条链路：处理抛异常后，下一轮不再取到同一封（否则是死循环）。"""
        adapter, _ = make_email()
        adapter._uid = 1
        raw = make_mail(body="poison", message_id="<p@x>")
        fake, _ = stub_network(adapter, [
            {"search": [2], "fetch": {2: raw}},
            {"search": []},
        ])
        item = adapter._fetch_one()
        adapter._on_raw(item)
        self.assertIs(adapter._fetch_one(), NOTHING)

    def test_fetch_failure_does_not_advance_cursor(self):
        """FETCH 失败**不能**抬游标 —— 否则那封信永远拿不到了。"""
        adapter, _ = make_email()
        adapter._uid = 1
        stub_network(adapter, [{"search": [2], "fetch": {}}])   # FETCH 返回 NO
        self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertEqual(adapter._uid, 1)

    def test_search_failure_raises_for_backoff(self):
        """SEARCH 失败要抛出去，交给传输层退避重连。"""
        adapter, _ = make_email()
        adapter._uid = 1
        stub_network(adapter, [{"search": [2]}])
        fake = FakeIMAP([RuntimeError("boom")])
        adapter._imap_connect = lambda: fake
        adapter._imap_close = lambda conn: None
        with self.assertRaises(RuntimeError):
            adapter._fetch_one()

    def test_connection_is_closed_after_each_pull(self):
        adapter, _ = make_email()
        adapter._uid = 1
        fake, _ = stub_network(adapter, [{"search": []}])
        adapter._fetch_one()
        # _imap_close 被替换了，但 _live_conn 必须已经清空（stop 才不用管残留）
        self.assertIsNone(adapter._live_conn)

    def test_uses_body_peek_not_body(self):
        """``BODY[]`` 会隐式设 ``\\Seen``；必须用 ``BODY.PEEK[]``。"""
        adapter, _ = make_email()
        adapter._uid = 1
        fake, _ = stub_network(adapter, [{"search": [2], "fetch": {2: make_mail()}}])
        adapter._fetch_one()
        fetch_calls = [c for c in fake.calls if c[0] == "FETCH"]
        self.assertTrue(fetch_calls)
        parts = " ".join(str(a) for a in fetch_calls[0][1])
        self.assertIn("BODY.PEEK[]", parts)
        self.assertNotIn("(BODY[]", parts)

    def test_imap_is_opened_readonly(self):
        """邮箱必须以 readonly（EXAMINE）打开 —— 从协议层面不改用户邮箱状态。

        这里测的是 :meth:`EmailAdapter._imap_connect` **本身**，所以替换的是它内部
        用的 ``imaplib.IMAP4_SSL``（而不是替换 ``_imap_connect`` —— 那会把被测
        逻辑一起替换掉，等于什么都没测）。
        """
        import opencode_bridge.adapters.email as email_mod

        captured: dict = {}

        class Recorder:
            def __init__(self, host, port, ssl_context=None, timeout=None):
                captured.update(host=host, port=port, ssl_context=ssl_context,
                                timeout=timeout)

            def starttls(self, ssl_context=None):
                captured["starttls"] = ssl_context

            def login(self, user, password):
                captured["login"] = (user, password)

            def select(self, mailbox="INBOX", readonly=False):
                captured["mailbox"] = mailbox
                captured["readonly"] = readonly
                return ("OK", [b"1"])

        original = email_mod.imaplib.IMAP4_SSL
        email_mod.imaplib.IMAP4_SSL = Recorder
        try:
            adapter, _ = make_email()
            conn = adapter._imap_connect()
        finally:
            email_mod.imaplib.IMAP4_SSL = original

        self.assertIsInstance(conn, Recorder)
        self.assertTrue(captured["readonly"], "必须 readonly=True（EXAMINE）")
        self.assertEqual(captured["mailbox"], "INBOX")
        self.assertEqual(captured["login"], ("bot@example.com", "app-password"))
        self.assertIsNotNone(captured["ssl_context"], "默认必须带校验证书的 context")

    def test_imap_timeout_is_passed_through(self):
        """每个网络操作都必须能超时，否则 stop() 会白等。"""
        import opencode_bridge.adapters.email as email_mod

        captured: dict = {}

        class Recorder:
            def __init__(self, host, port, ssl_context=None, timeout=None):
                captured["timeout"] = timeout

            def starttls(self, ssl_context=None):
                captured["starttls_ctx"] = ssl_context

            def login(self, user, password):
                pass

            def select(self, mailbox="INBOX", readonly=False):
                return ("OK", [b"1"])

        original = email_mod.imaplib.IMAP4_SSL
        email_mod.imaplib.IMAP4_SSL = Recorder
        try:
            adapter, _ = make_email()
            adapter._imap_connect()
        finally:
            email_mod.imaplib.IMAP4_SSL = original
        self.assertEqual(captured["timeout"], adapter.socket_timeout)

    def test_starttls_path_uses_imap4_and_upgrades(self):
        """starttls 模式必须先连 143 再升级，不能静默走明文。"""
        import opencode_bridge.adapters.email as email_mod

        captured: dict = {}

        class Plain:
            def __init__(self, host, port=143, timeout=None):
                captured.update(plain_port=port, timeout=timeout)

            def starttls(self, ssl_context=None):
                captured["upgraded"] = ssl_context is not None

            def login(self, user, password):
                pass

            def select(self, mailbox="INBOX", readonly=False):
                return ("OK", [b"1"])

        original = email_mod.imaplib.IMAP4
        email_mod.imaplib.IMAP4 = Plain
        try:
            adapter, _ = make_email(imap_security="starttls")
            adapter._imap_connect()
        finally:
            email_mod.imaplib.IMAP4 = original
        self.assertEqual(captured["plain_port"], 143)
        self.assertTrue(captured["upgraded"], "必须 starttls 升级到 TLS")


# ----------------------------------------------------------------------
# 入站解析
# ----------------------------------------------------------------------
class TestInboundDispatch(unittest.TestCase):
    def test_message_becomes_inbound(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(sender="user@example.com", body="你好"))
        self.assertEqual(len(hooks.inbounds), 1)
        ib = hooks.inbounds[0]
        self.assertEqual(ib.platform, "email")
        self.assertEqual(ib.user_id, "user@example.com")
        self.assertEqual(ib.message_id, "<m1@example.com>")
        self.assertEqual(platform_of(ib.conversation_id), "email")

    def test_conversation_id_uses_identity_format_id(self):
        """conversation_id 必须是 ``email:<地址>``，不自造前缀。"""
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(sender="user@example.com"))
        self.assertEqual(hooks.inbounds[0].conversation_id, "email:user@example.com")

    def test_subject_is_included_in_text(self):
        """邮件的问题常常写在标题里，所以主题要并入正文交给 agent。"""
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(subject="CI 挂了", body="流水线失败"))
        text = hooks.inbounds[0].text
        self.assertIn("CI 挂了", text)
        self.assertIn("流水线失败", text)

    def test_no_subject_means_plain_body(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(subject=None, body="只有正文"))
        self.assertEqual(hooks.inbounds[0].text.strip(), "只有正文")

    def test_display_name_is_stripped_from_sender(self):
        adapter, hooks = make_email()
        adapter._on_raw(make_mail(sender="张三 <zhang@example.com>"))
        self.assertEqual(hooks.inbounds[0].user_id, "zhang@example.com")

    def test_empty_body_dropped(self):
        adapter, hooks = make_email()
        for body in ("", "   "):
            adapter._on_raw(make_mail(body=body))
        self.assertEqual(hooks.inbounds, [])

    def test_non_bytes_payload_ignored(self):
        adapter, hooks = make_email()
        adapter._on_raw("not bytes")      # type: ignore[arg-type]
        adapter._on_raw(None)             # type: ignore[arg-type]
        adapter._on_raw(b"")
        self.assertEqual(hooks.inbounds, [])

    def test_garbage_bytes_are_dropped_not_raised(self):
        adapter, hooks = make_email()
        adapter._on_raw(b"\xff\xfe\x00 not really a message")
        self.assertEqual(hooks.inbounds, [])

    def test_authorization_gate_runs_before_inbound(self):
        adapter, hooks = make_email(allowed_chat_ids=["other@example.com"])
        adapter._on_raw(make_mail(sender="user@example.com"))
        self.assertEqual(hooks.inbounds, [], "白名单外的发件人必须被丢弃")

    def test_allowlisted_sender_accepted(self):
        adapter, hooks = make_email(allowed_chat_ids=["user@example.com"])
        adapter._on_raw(make_mail(sender="user@example.com"))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_hook_exception_does_not_propagate(self):
        adapter, hooks = make_email()

        def boom(_inbound):
            raise RuntimeError("boom")

        adapter.hooks = type(
            "Boom", (), {"on_inbound": staticmethod(boom),
                         "on_callback": staticmethod(lambda *a: None)}
        )()
        adapter._on_raw(make_mail())      # 不应抛


class TestPlainTextOnly(unittest.TestCase):
    """拿不到纯文本就丢 —— 不把 HTML 当纯文本塞给 agent。"""

    def test_html_only_is_dropped(self):
        adapter, hooks = make_email()
        raw = (
            b"From: user@example.com\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>hello</p>\r\n"
        )
        adapter._on_raw(raw)
        self.assertEqual(hooks.inbounds, [])

    def test_multipart_without_plain_part_is_dropped(self):
        adapter, hooks = make_email()
        raw = (
            b"From: user@example.com\r\n"
            b'Content-Type: multipart/alternative; boundary="bb"\r\n\r\n'
            b"--bb\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n--bb--\r\n"
        )
        adapter._on_raw(raw)
        self.assertEqual(hooks.inbounds, [])

    def test_attachment_only_multipart_is_dropped(self):
        adapter, hooks = make_email()
        raw = (
            b"From: user@example.com\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: application/octet-stream\r\n\r\nxx\r\n--b--\r\n"
        )
        adapter._on_raw(raw)
        self.assertEqual(hooks.inbounds, [])

    def test_unknown_charset_is_dropped(self):
        adapter, hooks = make_email()
        raw = (
            b"From: user@example.com\r\n"
            b"Content-Type: text/plain; charset=x-unknown-xyz\r\n\r\nhello\r\n"
        )
        adapter._on_raw(raw)
        self.assertEqual(hooks.inbounds, [])

    def test_multipart_with_plain_part_is_accepted(self):
        adapter, hooks = make_email()
        raw = (
            b"From: user@example.com\r\n"
            b'Content-Type: multipart/alternative; boundary="bb"\r\n\r\n'
            b"--bb\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            + "纯文本".encode("utf-8")
            + b"\r\n--bb\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n--bb--\r\n"
        )
        adapter._on_raw(raw)
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertIn("纯文本", hooks.inbounds[0].text)

    def test_base64_body_is_decoded(self):
        import base64

        adapter, hooks = make_email()
        payload = base64.b64encode("中文 base64".encode("utf-8")).decode()
        raw = (
            b"From: user@example.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + payload.encode()
            + b"\r\n"
        )
        adapter._on_raw(raw)
        self.assertIn("中文 base64", hooks.inbounds[0].text)


# ----------------------------------------------------------------------
# 非 ASCII 编码
# ----------------------------------------------------------------------
class TestNonAsciiOutbound(unittest.TestCase):
    def test_chinese_subject_and_body_round_trip(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        handle = adapter.send(Outbound("email:user@example.com", "这是中文答案。"))
        self.assertIsNotNone(handle)
        recipient, raw = sent[0]
        self.assertEqual(recipient, "user@example.com")
        parsed = BytesParser(policy=DEFAULT_POLICY).parsebytes(raw)
        self.assertEqual(str(parsed["Subject"]), f"{ECHO_PREFIX} reply")
        self.assertIn("这是中文答案。",
                      parsed.get_body(preferencelist=("plain",)).get_content())

    def test_subject_header_is_rfc2047_encoded(self):
        """中文主题必须是 RFC 2047 编码，不能是裸 UTF-8 塞进头里。"""
        adapter, _ = make_email()
        adapter._remember_thread("email:user@example.com", "<x@y>", "问题主题")
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "答案"))
        raw_headers = sent[0][1].split(b"\r\n\r\n")[0].split(b"\r\n")[0]
        self.assertIn(b"=?utf-8?", raw_headers)
        self.assertNotIn("主题".encode("utf-8"), raw_headers)

    def test_reply_threads_to_original_subject(self):
        adapter, _ = make_email()
        adapter._remember_thread("email:user@example.com", "<x@y>", "CI 挂了")
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "答案"))
        parsed = BytesParser(policy=DEFAULT_POLICY).parsebytes(sent[0][1])
        subject = str(parsed["Subject"])
        self.assertTrue(subject.startswith(ECHO_PREFIX))
        self.assertIn("Re: CI 挂了", subject)

    def test_reply_carries_in_reply_to_header(self):
        adapter, _ = make_email()
        adapter._remember_thread("email:user@example.com", "<orig@x>", "问题")
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "答案"))
        parsed = BytesParser(policy=DEFAULT_POLICY).parsebytes(sent[0][1])
        self.assertEqual(str(parsed["In-Reply-To"]), "<orig@x>")
        self.assertEqual(str(parsed["References"]), "<orig@x>")

    def test_message_id_is_non_empty_for_non_ascii_content(self):
        """非 ASCII 正文也必须拿到可用的 Message-ID（否则 edit/去重都无从谈起）。"""
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        handle = adapter.send(Outbound("email:user@example.com", "中文" * 200))
        self.assertIsNotNone(handle)
        self.assertTrue(handle.message_id)
        self.assertTrue(handle.message_id.isascii())
        self.assertIn("@", handle.message_id)

    def test_sent_message_id_is_recorded_for_echo_detection(self):
        adapter, _ = make_email()
        stub_network(adapter)
        handle = adapter.send(Outbound("email:user@example.com", "答案"))
        self.assertIn(handle.message_id, adapter._sent)

    def test_long_ascii_line_never_exceeds_rfc_limit(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "x" * 20000))
        self.assertLessEqual(_max_line_octets(sent[0][1]), MESSAGE_LIMIT)

    def test_long_cjk_line_never_exceeds_rfc_limit(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "中" * 5000))
        self.assertLessEqual(_max_line_octets(sent[0][1]), MESSAGE_LIMIT)

    def test_very_long_cjk_body_never_exceeds_rfc_limit(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "中文测试" * 5000))
        self.assertLessEqual(_max_line_octets(sent[0][1]), MESSAGE_LIMIT)

    def test_ascii_short_uses_7bit_for_readability(self):
        adapter, _ = make_email()
        self.assertEqual(adapter._cte_for("short ascii"), "7bit")
        self.assertEqual(adapter._cte_for("a" * WRAP_COLUMNS), "7bit")

    def test_non_ascii_uses_wrapping_encoding(self):
        adapter, _ = make_email()
        self.assertEqual(adapter._cte_for("中文"), "quoted-printable")
        self.assertEqual(adapter._cte_for("a" * (WRAP_COLUMNS + 1)),
                         "quoted-printable")

    def test_header_injection_is_rejected(self):
        """CRLF 注入必须被标准库拒掉（转成 BAD_FORMAT，不能发出去）。"""
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        handle = adapter.send(
            Outbound("email:user@example.com\r\nBcc: evil@x.com", "答案")
        )
        self.assertIsNone(handle)
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)
        self.assertEqual(sent, [])


# ----------------------------------------------------------------------
# 出站
# ----------------------------------------------------------------------
class TestSend(unittest.TestCase):
    def test_send_returns_handle_with_conversation_id(self):
        adapter, _ = make_email()
        stub_network(adapter)
        handle = adapter.send(Outbound("email:user@example.com", "hi"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.conversation_id, "email:user@example.com")
        self.assertEqual(handle.platform, "email")

    def test_email_prefix_is_stripped_from_conversation_id(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "hi"))
        self.assertEqual(sent[0][0], "user@example.com")

    def test_bare_address_without_prefix_is_accepted(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("user@example.com", "hi"))
        self.assertEqual(sent[0][0], "user@example.com")

    def test_single_email_never_split(self):
        """邮件不是聊天平台：一个答案**绝不**拆成多封邮件。"""
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        adapter.send(Outbound("email:user@example.com", "长答案 " * 5000))
        self.assertEqual(len(sent), 1)

    def test_empty_text_is_rejected(self):
        adapter, _ = make_email()
        stub_network(adapter)
        self.assertIsNone(adapter.send(Outbound("email:user@example.com", "")))
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_missing_recipient_is_rejected(self):
        adapter, _ = make_email()
        stub_network(adapter)
        self.assertIsNone(adapter.send(Outbound("", "hi")))
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_missing_smtp_host_is_reported(self):
        adapter, _ = make_email()
        adapter.smtp_host = ""
        stub_network(adapter)
        self.assertIsNone(adapter.send(Outbound("email:user@example.com", "hi")))
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_auth_failure_classified_as_forbidden(self):
        adapter, _ = make_email()
        stub_network(adapter, smtp_errors=[FakeSMTPError(535, "bad creds")])
        self.assertIsNone(adapter.send(Outbound("email:user@example.com", "hi")))
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)

    def test_rate_limit_classified(self):
        adapter, _ = make_email()
        stub_network(adapter, smtp_errors=[FakeSMTPError(451, "slow down")])
        adapter.send(Outbound("email:user@example.com", "hi"))
        self.assertEqual(adapter.last_send_error, SendError.RATE_LIMITED)

    def test_message_too_large_classified(self):
        adapter, _ = make_email()
        stub_network(adapter, smtp_errors=[FakeSMTPError(552, "too big")])
        adapter.send(Outbound("email:user@example.com", "hi"))
        self.assertEqual(adapter.last_send_error, SendError.TOO_LONG)

    def test_server_unavailable_classified_transient(self):
        adapter, _ = make_email()
        stub_network(adapter, smtp_errors=[FakeSMTPError(421, "closing")])
        adapter.send(Outbound("email:user@example.com", "hi"))
        self.assertEqual(adapter.last_send_error, SendError.TRANSIENT)

    def test_send_result_reports_failure(self):
        adapter, _ = make_email()
        stub_network(adapter, smtp_errors=[FakeSMTPError(535, "no")])
        result = adapter.send_result(Outbound("email:user@example.com", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.FORBIDDEN)

    def test_send_result_reports_success(self):
        adapter, _ = make_email()
        stub_network(adapter)
        result = adapter.send_result(Outbound("email:user@example.com", "hi"))
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.handle)

    def test_throttle_enforces_min_interval(self):
        """防刷屏：连续发送之间必须至少隔 min_interval。"""
        import time as _time

        adapter, _ = make_email()
        adapter.min_interval = 0.08
        stub_network(adapter)
        start = _time.monotonic()
        for _ in range(3):
            adapter.send(Outbound("email:user@example.com", "x"))
        # 3 次发送之间有 2 个间隔；sleep 可能略早返回，所以留一点余量。
        self.assertGreaterEqual(_time.monotonic() - start, 0.14)

    def test_throttle_is_noop_when_disabled(self):
        adapter, _ = make_email()
        adapter.min_interval = 0
        adapter._throttle()
        adapter._throttle()      # 不应抛、不应睡


class TestEditAndAnswer(unittest.TestCase):
    def test_edit_returns_false(self):
        """SMTP 无法编辑已发邮件 —— 诚实返回 False，让 core 退化成发新邮件。"""
        adapter, _ = make_email()
        handle = MsgHandle("email:user@example.com", "<m@x>", "email")
        self.assertIs(
            adapter.edit(handle, Outbound("email:user@example.com", "updated")),
            False,
        )

    def test_edit_does_not_send_anything(self):
        adapter, _ = make_email()
        _, sent = stub_network(adapter)
        handle = MsgHandle("email:user@example.com", "<m@x>", "email")
        adapter.edit(handle, Outbound("email:user@example.com", "updated"))
        self.assertEqual(sent, [])

    def test_answer_is_noop(self):
        adapter, _ = make_email()
        self.assertIsNone(adapter.answer("q1", "text"))


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
class TestLifecycle(unittest.TestCase):
    def test_missing_address_does_not_start(self):
        adapter = EmailAdapter(
            {"imap_host": "h", "smtp_host": "s"}, RecordingHooks()
        )
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING"):
            adapter.start()
        self.assertIsNone(adapter._transport)

    def test_missing_imap_host_does_not_start(self):
        adapter = EmailAdapter(
            {"address": "a@b.com", "smtp_host": "s"}, RecordingHooks()
        )
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING"):
            adapter.start()
        self.assertIsNone(adapter._transport)

    def test_missing_smtp_host_still_starts_inbound(self):
        adapter, _ = make_email(smtp_host="")
        stub_network(adapter)
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING"):
            adapter.start()
        adapter.stop()

    def test_stop_without_start_is_safe(self):
        adapter, _ = make_email()
        adapter.stop()

    def test_stop_is_idempotent(self):
        adapter, _ = make_email()
        stub_network(adapter, [{"search": []}])
        adapter.start()
        adapter.stop()
        adapter.stop()

    def test_stop_closes_live_imap_connection(self):
        """先关连接再 join —— 否则 stop 会白等满超时（transport/base 的教训）。"""
        adapter, _ = make_email()
        stub_network(adapter, [{"search": []}])
        adapter.start()
        adapter._live_conn = FakeIMAP()
        conn = adapter._live_conn
        adapter.stop()
        self.assertGreaterEqual(conn.logged_out, 1)
        self.assertGreaterEqual(conn.shutdowns, 1)
        self.assertIsNone(adapter._live_conn)

    def test_start_is_idempotent(self):
        adapter, _ = make_email()
        stub_network(adapter, [{"search": []}])
        adapter.start()
        transport = adapter._transport
        adapter.start()
        self.assertIs(adapter._transport, transport)
        adapter.stop()

    def test_poll_loop_delivers_inbound_end_to_end(self):
        """完整链路：假 IMAP → 传输线程 → on_inbound（不 mock 标准库内部）。"""
        import threading

        adapter, hooks = make_email(poll_interval=0.01)
        raw = make_mail(sender="user@example.com", body="端到端测试",
                         message_id="<e2e@x>")
        fake = FakeIMAP([
            {"search": [1, 2]},                                  # bootstrap
            {"search": [3], "fetch": {3: raw}},                   # 真正的消息
            {"search": []},
        ])
        adapter._imap_connect = lambda: fake
        adapter._imap_close = lambda conn: None
        stub_smtp = []
        adapter._smtp_send = lambda r, raw_: stub_smtp.append((r, raw_))

        try:
            adapter.start()
            deadline = threading.Event()
            deadline.wait(0.01)
            for _ in range(300):
                if hooks.inbounds:
                    break
                threading.Event().wait(0.02)
            self.assertEqual(len(hooks.inbounds), 1)
            self.assertIn("端到端测试", hooks.inbounds[0].text)
        finally:
            adapter.stop()
        self.assertFalse(adapter.running)


class TestCursorPersistence(unittest.TestCase):
    """游标必须**跨重启**存活：只有首次运行才跳过历史。

    这个 bug 的形态：游标只活在内存里，于是"跳过历史"那条分支**每次启动都重跑**，
    于是每次重启都把整个未读收件箱悄悄丢掉 —— 用户既收不到，日志里也没有任何
    解释。所以这里覆盖的是"两次运行"而不是"一次运行"。
    """

    def setUp(self):
        # ``TemporaryDirectory`` 由 ``addCleanup`` 关闭：Windows 上文件句柄没关
        # 干净时 ``rmtree`` 会 WinError 32。
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.state_path = os.path.join(temp_dir.name, "state.json")

    def open_store(self) -> StateStore:
        """新开一个 store —— 这就是"重启"：内存全空，只剩磁盘上的 ``state.json``。"""
        return StateStore(self.state_path)

    def run_first_start(self, mailbox_uids=(10, 20, 30)) -> str:
        """跑一次"首次启动"（收件箱里已有历史邮件），返回落盘作用域键。

        只断言内存里的游标抬到了水位；**不去断言落盘** —— 水位到底有没有写下去
        由各条用例自己验，否则"写盘"一旦坏掉，所有用例都在**准备阶段**报错，
        真正的场景断言根本没跑到。
        """
        adapter, _ = make_email(hooks=CursorStoreHooks(self.open_store()))
        stub_network(adapter, [{"search": list(mailbox_uids)}])
        self.assertIs(adapter._fetch_one(), NOTHING, "首次启动不该取任何正文")
        self.assertEqual(adapter._uid, max(mailbox_uids))
        return adapter._cursor_scope()

    # -- 首次运行：仍然跳过历史，但必须留下水位 -------------------------
    def test_first_run_still_skips_the_mailbox_history(self):
        """既有语义不许变：首次连接一封正文都不取（否则历史邮件各触发一次 agent）。"""
        adapter, hooks = make_email(hooks=CursorStoreHooks(self.open_store()))
        fake, _ = stub_network(adapter, [{"search": [10, 20, 30]}])
        with self.assertLogs("opencode_bridge.adapters.email", level="INFO") as logs:
            self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertEqual(adapter._uid, 30)
        self.assertEqual([c for c in fake.calls if c[0] == "FETCH"], [])
        self.assertTrue(
            any("不重放历史收件箱" in line for line in logs.output), logs.output
        )

    def test_first_run_records_the_watermark_on_disk(self):
        """跳过历史之后**立刻**落盘水位 —— 否则下一次启动又重来一遍。"""
        store_hooks = CursorStoreHooks(self.open_store())
        adapter, _ = make_email(hooks=store_hooks)
        stub_network(adapter, [{"search": [10, 20, 30]}])
        adapter._fetch_one()
        self.assertEqual(store_hooks.written_positions,
                         [(adapter._cursor_scope(), 30)])
        # 落盘而不只是内存：新 store 也要读得到。
        self.assertEqual(
            self.open_store().get_meta(adapter._cursor_scope(), "stream_cursor"), 30
        )

    # -- 重启：从已存水位续跑 -------------------------------------------
    def test_restart_resumes_from_the_stored_cursor(self):
        scope = self.run_first_start()

        restarted_hooks = CursorStoreHooks(self.open_store())
        restarted, _ = make_email(hooks=restarted_hooks)
        fake, _ = stub_network(restarted, [{"search": []}])
        restarted._fetch_one()

        self.assertEqual(restarted._uid, 30, "重启后必须接着上次的游标")
        self.assertEqual(restarted_hooks.read_scopes, [scope], "作用域必须稳定")
        command, args = fake.calls[0]
        self.assertEqual(command, "SEARCH")
        self.assertIn("UID", args)
        self.assertIn("31:*", args, "必须从已存水位之后开始搜，而不是重放全部")

    def test_mail_that_arrived_while_stopped_is_delivered_after_restart(self):
        """**真实场景**：停机期间用户发了一封信，重启后它必须被送到 agent。

        修复前这条路径的结果是 bootstrap 直接跳到最新 UID，那封信连同它的正文
        一起消失，且日志里只有"首次连接"那一行。
        """
        self.run_first_start(mailbox_uids=(10, 20, 30))

        waiting = make_mail(body="停机期间收到的信", message_id="<while-down@x>")
        restarted_hooks = CursorStoreHooks(self.open_store())
        restarted, _ = make_email(hooks=restarted_hooks)
        stub_network(restarted, [{"search": [31], "fetch": {31: waiting}}])

        item = restarted._fetch_one()
        self.assertEqual(item, waiting, "停机期间到达的信必须在重启后被取到")
        self.assertEqual(restarted._uid, 31)

        restarted._on_raw(item)
        self.assertEqual(len(restarted_hooks.inbounds), 1)
        self.assertIn("停机期间收到的信", restarted_hooks.inbounds[0].text)

    def test_restart_does_not_replay_already_handled_mail(self):
        """续跑的另一面：水位之前的那几封**不该**被重新投一次。"""
        self.run_first_start(mailbox_uids=(10, 20, 30))

        restarted_hooks = CursorStoreHooks(self.open_store())
        restarted, _ = make_email(hooks=restarted_hooks)
        stub_network(restarted, [{"search": [31], "fetch": {31: make_mail()}}])
        self.assertIsNotNone(restarted._fetch_one())

    # -- "先推进再处理"这条不变式现在还要同时落盘 -----------------------
    def test_advance_is_persisted_before_the_message_is_processed(self):
        """顺序不许反：先抬水位并落盘，再交给上层处理。

        反过来的话，崩溃窗口就从"最多丢本批未处理完的几封"变成"位置已存、正文
        还在队列里"，那封信**永远拿不到** —— 比 at-most-once 丢一封严重得多。
        """
        store_hooks = CursorStoreHooks(self.open_store())
        adapter, _ = make_email(hooks=store_hooks)
        adapter._uid = 1
        stub_network(adapter, [{"search": [2],
                                "fetch": {2: make_mail(message_id="<m2@x>")}}])

        item = adapter._fetch_one()
        self.assertEqual(adapter._uid, 2, "fetch 阶段就必须抬游标")
        self.assertEqual(store_hooks.written_positions,
                         [(adapter._cursor_scope(), 2)], "抬完就要落盘")

        def boom(_inbound):
            raise RuntimeError("core 炸了")

        store_hooks.on_inbound = boom
        adapter._on_raw(item)                 # 异常被吞，不影响游标
        self.assertEqual(adapter._uid, 2)
        self.assertEqual(store_hooks.written_positions,
                         [(adapter._cursor_scope(), 2)],
                         "处理阶段不得改动已落盘的水位")

    def test_fetch_failure_leaves_the_stored_cursor_untouched(self):
        """取不到正文时水位既不推进也不落盘（否则那封信永远拿不到）。"""
        store_hooks = CursorStoreHooks(self.open_store())
        adapter, _ = make_email(hooks=store_hooks)
        adapter._uid = 1
        stub_network(adapter, [{"search": [2], "fetch": {}}])   # FETCH 返回 NO

        self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertEqual(adapter._uid, 1)
        self.assertEqual(
            self.open_store().get_meta(adapter._cursor_scope(), "stream_cursor"), 1,
            "落盘的水位绝不许跳过取不到正文的那一封",
        )

    # -- 存不下 / 读不到时：退化成旧行为，但必须告警 ---------------------
    def test_missing_cursor_hooks_warn_that_history_will_be_skipped(self):
        """没有落盘通道时**必须**告警 —— 无解释的历史跳过就是丢信。"""
        adapter, _ = make_email()              # RecordingHooks 不实现游标钩子
        self.assertFalse(adapter._cursor_persistence.available)
        stub_network(adapter, [{"search": [10, 20]}])
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING") as logs:
            adapter._fetch_one()
        self.assertTrue(
            any("游标无法落盘" in line for line in logs.output), logs.output
        )

    def test_missing_cursor_hooks_still_skip_history(self):
        """退化方向不变：存不下时行为与修复前一致（跳过历史），只是不再沉默。"""
        adapter, _ = make_email()
        fake, _ = stub_network(adapter, [{"search": [10, 20, 30]}])
        adapter._fetch_one()
        self.assertEqual(adapter._uid, 30)
        self.assertEqual([c for c in fake.calls if c[0] == "FETCH"], [])

    def test_failing_write_does_not_break_inbound_delivery(self):
        """落盘失败只告警：抛出去会把轮询线程打断，连已经取到的正文一起丢。"""
        adapter, _ = make_email(hooks=FailingCursorStoreHooks(self.open_store()))
        adapter._uid = 1
        stub_network(adapter, [{"search": [2],
                                "fetch": {2: make_mail(message_id="<kept@x>")}}])
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING") as logs:
            item = adapter._fetch_one()
        self.assertIsNotNone(item, "落盘失败不许把已经取到的正文丢掉")
        self.assertTrue(any("cannot persist" in line for line in logs.output),
                        logs.output)

    def test_failing_read_falls_back_to_first_start_semantics(self):
        """读不到水位时退化成"首次启动"，而不是拿着半可信的值去算 UID 区间。"""
        adapter, _ = make_email(hooks=UnreadableCursorStoreHooks(self.open_store()))
        stub_network(adapter, [{"search": [7]}])
        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING") as logs:
            adapter._fetch_one()
        self.assertEqual(adapter._uid, 7)
        self.assertTrue(any("cannot read the stored UID cursor" in line
                            for line in logs.output), logs.output)

    # -- 作用域 ---------------------------------------------------------
    def test_cursor_scope_is_stable_across_instances(self):
        """作用域每次启动都必须算出同一个值，否则等于每次都是首次运行。"""
        store = self.open_store()
        first, _ = make_email(hooks=CursorStoreHooks(store))
        again, _ = make_email(hooks=CursorStoreHooks(store))
        self.assertEqual(first._cursor_scope(), again._cursor_scope())

    def test_cursor_scope_separates_mailboxes_and_accounts(self):
        """一个 IMAP 账号可以同时看多个邮箱，各自的 UID 空间不能共用一个游标。"""
        store = self.open_store()
        inbox, _ = make_email(hooks=CursorStoreHooks(store))
        archive, _ = make_email(mailbox="Archive", hooks=CursorStoreHooks(store))
        other_account, _ = make_email(address="other@example.com",
                                     hooks=CursorStoreHooks(store))
        self.assertNotEqual(inbox._cursor_scope(), archive._cursor_scope())
        self.assertNotEqual(inbox._cursor_scope(), other_account._cursor_scope())


class TestCoreCursorHookWiring(unittest.TestCase):
    """:class:`BridgeCore` 是那两个游标钩子的**生产实现**，必须真接到 state.json。"""

    SCOPE = "email.imap-uid-cursor:bot@example.com/INBOX"

    def test_hooks_protocol_declares_both_cursor_hooks(self):
        for name in ("load_stream_cursor", "save_stream_cursor"):
            self.assertTrue(hasattr(Hooks, name), f"Hooks 协议缺 {name}")

    def test_cursor_written_by_core_survives_a_restart(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        state_path = os.path.join(temp_dir.name, "state.json")

        core = BridgeCore(Config(), UnusedOpenCodeClient(), StateStore(state_path))
        self.assertIsNone(core.load_stream_cursor(self.SCOPE))
        core.save_stream_cursor(self.SCOPE, 42)

        # "重启"：全新的 core + 从磁盘重新加载的 store。
        restarted = BridgeCore(Config(), UnusedOpenCodeClient(),
                               StateStore(state_path))
        self.assertEqual(restarted.load_stream_cursor(self.SCOPE), 42)

    def test_core_ignores_a_corrupted_stored_cursor(self):
        """手改坏 / 旧版本写入的垃圾值必须按"没有已存位置"处理（并告警）。"""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        state_path = os.path.join(temp_dir.name, "state.json")

        store = StateStore(state_path)
        store.set_meta(self.SCOPE, "stream_cursor", "not-a-number")
        core = BridgeCore(Config(), UnusedOpenCodeClient(), store)
        with self.assertLogs("opencode_bridge.stream_cursor", level="WARNING"):
            self.assertIsNone(core.load_stream_cursor(self.SCOPE))

    def test_core_does_not_raise_when_the_store_cannot_be_written(self):
        """写盘失败只告警：异常冒到适配器的轮询线程会把收信循环打断。"""
        class UnwritableStore(StateStore):
            def set_meta(self, conversation_id, key, value):
                raise OSError("read-only file system")

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        core = BridgeCore(Config(), UnusedOpenCodeClient(),
                          UnwritableStore(os.path.join(temp_dir.name, "s.json")))
        with self.assertLogs("opencode_bridge.stream_cursor", level="WARNING"):
            core.save_stream_cursor(self.SCOPE, 7)


if __name__ == "__main__":
    unittest.main()