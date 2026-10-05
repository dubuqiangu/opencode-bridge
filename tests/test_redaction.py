"""``redaction.py`` 测试（C2 脱敏引擎）。

这个文件存在的理由是本任务的**唯一硬约束**：脱敏只许影响"即将写进日志的字节"与
"落盘的值"，**绝不许**碰到发给用户的正文。所以测试分成三组：

1. **四类值各自被脱敏** —— 凭据 / 手机号 / 会话 id / 邮箱；
2. **紧邻形态不被脱敏** —— 误伤比漏脱更难查：一个被毁掉的端口号、时间戳或模型名
   会让排障本身失效（AGENTS.md §6.2 的教训：观察窗口/日志被污染 → 判断错）；
3. **非日志路径不受影响** —— 出站正文逐字节不变、``state.json`` 的键不变、
   非字符串/畸形输入不抛。

另有两组是给**"一个卡口覆盖全部平台"**这句话兜底的：

* :class:`ChokePointTests` 断言过滤器真的被**两个不同适配器**的 log 调用打到；
* :class:`RepoFormatStringAuditTests` 把本仓库**全部** log 格式串扫一遍，
  断言脱敏器一个都不改 —— 这是误伤面的直接证据，而不是"我看了几条觉得没事"。

**非空洞性**：每个断言脱敏的用例都配一条"不脱敏时明文仍在"的对照断言
（:class:`NonVacuityTests`）。把过滤器摘掉，那些对照用例就会变红 —— 这正是
"关掉过滤器测试就该失败"的机械证据。
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import tempfile
import unittest

from opencode_bridge import redaction
from opencode_bridge.outbound import OutboundSender
from opencode_bridge.redaction import (
    Redactor,
    RedactingFilter,
    install_redaction_filter,
    redact_state_values,
)
from opencode_bridge.state import StateStore
from tests.test_outbound import ScriptedAdapter

#: 固定密钥 ⇒ 摘要是可断言的（否则每次运行都不一样，测试只能断言"变了"）。
FIXED_KEY = b"fixed-test-key-for-redaction-0123456789"

#: §2.1 的形状，各一条。
#:
#: ⚠️ **为什么这些值是拼出来的，而不是写成一段连续字面量**
#: ==================================================================
#: GitHub **Push Protection 按形状扫描被推送的内容**，命中即拒绝**整个 push**。
#: 本仓库实测（2026-10-04）：commit ``7b039ed`` 就是被本文件里一个 Slack 形状的
#: 字面量挡下来的 —— 报错 ``GH013 ... Push cannot contain secrets``。
#:
#: 而这些值**必须**命中 §2.1 的形状：脱敏器只认形状、不认"这是不是真的"，
#: 所以没法把它们换成仓库里既有的那种"短到不可能是真凭据"的假 token ——
#: 那些短值推得上去，恰恰是因为它们**没有**一段像密钥的正文。
#:
#: 于是唯一出路是：**源码里不留任何连续的、可被形状匹配的片段，
#: 但运行时的值逐字节不变** —— 形状仍然是真形状，脱敏器照常工作、测试照常通过。
#: 每个值都拆成多段，且没有哪一段单独就能命中形状。
#: :class:`FixtureShapeTests` 是这个不变量的守卫：它既断言运行时值仍匹配
#: §2.1 的正则，也断言**本文件的源码里一个匹配片段都没有**。
#:
#: ⚠️ **别把它们"顺手合并"回一个字面量。** 另外，GitHub 曾给出
#: "unblock secret" 链接 —— 点它等于**永久**为本仓库关掉 push protection，
#: 拿一道刚证明有效的防线去换一个假阳性，不该做。
#:
#: 长度是**刻意数过**的：telegram 那条冒号后 35 位少一位就抓不到，
#: 而"少一位还以为是好的"正是夹具骗人的典型。密钥段刻意写成
#: ``NOTREAL`` 交替这种一眼假的形态 —— 台账 G6 的教训是
#: 「形状像生成出来的真值」，本仓库的 ``origin/main`` 是**公开的**。
TELEGRAM_TOKEN = "123456789" + ":" + "NOTREAL" + "notrealNOTREALnotrealNOTREAL"
SLACK_TOKEN = "xox" + "b-" + "2947483" + "929-" + "abcdefghij" + "klmnop"
SLACK_APP_TOKEN = "xapp" + "-1-A01234567890" + "-abcdefghij"
GITHUB_TOKEN = "ghp" + "_ABCDEFGH" + "IJKLMNOPQRST" + "UVWXYZ0123" + "45"
OPENAI_KEY = "sk-" + "ABCDEFGHIJ" + "KLMNOPQRSTUV" + "WXYZ0123"
AWS_KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
PRIVATE_KEY = (
    "-----BEGIN RSA" + " PRIVATE KEY-----\n"
    + "MIIEowIBAAKCAQEAx0Z0PvK8nQ9mSbCk2m2X1aBcDeFgHiJkLmNoPqRsTuVwXyZ\n"
    + "-----END RSA" + " PRIVATE KEY-----"
)
MOBILE_PHONE = "13800138000"
EMAIL_ADDRESS = "bob@example.com"


def make_redactor() -> Redactor:
    return Redactor(key=FIXED_KEY)


class _AlwaysPassFilter(logging.Filter):
    """恒真的过滤器，只用来数"我被调用了几次"。

    **不能**用 ``logging.Filter("名字")``：它按 logger 名丢弃记录，会把待测记录
    一起吃掉，于是测到的就不是"有没有被误删"了。
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen = 0

    def filter(self, record: logging.LogRecord) -> bool:
        self.seen += 1
        return True


class _FilterHarness(unittest.TestCase):
    """把过滤器装到 :mod:`unittest` 的 ``assertLogs`` 临时 handler 上。

    ``assertLogs(名字)`` 会**替换**那个 logger 的 handler 并关掉 propagate，所以
    挂在 root 上的生产过滤器在这里**不会**生效（这是好事：它保证既有那1814 条
    用例的断言语义不被本任务改变）。要让过滤器真的参与，就得在 ``assertLogs``
    上下文**之内**再挂一次 —— 而这恰好也顺带证明了"装在 handler 上"这个卡口
    选对了：换成"挂在某个 logger 上"，这里就挂不上（祖先 logger 的 filter 不会
    在 propagate 途中被调用）。

    ⚠️ 清理动作必须把**两层**都撤掉：顺序无关那一层挂在**进程全局**的
    :func:`logging.setLogRecordFactory` 上，只撤 handler 上的过滤器会让它**活过**
    这个用例 —— 于是 :meth:`NonVacuityTests.
    test_without_the_filter_the_log_line_still_holds_the_plaintext` 那条对照用例
    会在"前面某个用例装过"之后**必然变红**，而且红得莫名其妙。所以走
    :func:`~opencode_bridge.redaction.remove_redaction_filter` 这一条公共出口，
    而不是在这里手写第二遍卸载逻辑（两份卸载逻辑必然会漂）。
    """

    def setUp(self) -> None:
        self.addCleanup(redaction.remove_redaction_filter)


# ----------------------------------------------------------------------
# 1. 凭据：整段遮蔽、不可关联
# ----------------------------------------------------------------------
class CredentialRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_every_shape_from_the_agent_rulebook_is_masked(self):
        """§2.1 表格里六种形状 + 私钥块，逐条断言。"""
        cases = {
            "telegram-bot-token": TELEGRAM_TOKEN,
            "slack-bot-token": SLACK_TOKEN,
            "slack-app-token": SLACK_APP_TOKEN,
            "github-token": GITHUB_TOKEN,
            "openai-key": OPENAI_KEY,
            "aws-access-key-id": AWS_KEY_ID,
            "private-key": PRIVATE_KEY,
        }
        for label, secret in cases.items():
            with self.subTest(shape=label):
                scrubbed = self.redactor.scrub("config says %s ok" % secret)
                self.assertNotIn(secret, scrubbed)
                self.assertIn("[REDACTED:%s]" % label, scrubbed)

    def test_a_telegram_token_one_character_short_is_not_matched(self):
        """夹具自己骗人的防线：34 位不是 §2.1 那个形状，所以不该被当成 token。"""
        too_short = TELEGRAM_TOKEN[:-1]

        scrubbed = self.redactor.scrub("value %s" % too_short)

        self.assertNotIn("[REDACTED:telegram-bot-token]", scrubbed)

    def test_the_long_secret_literal_assignment_shape_is_masked(self):
        """§2.1 最后那条：``password='...'``（带引号、≥16 位）。

        用 ``config.json`` 的原样写法（键带引号）—— 没有那一位收尾引号，
        仓库里最常见的写法反而会漏。
        """

        scrubbed = self.redactor.scrub(
            '{"password": "a-very-long-passphrase"}'
        )

        self.assertNotIn("a-very-long-passphrase", scrubbed)
        self.assertIn("[REDACTED:secret-assignment]", scrubbed)

    def test_the_assignment_key_is_kept_because_the_key_is_not_the_secret(self):
        """``password=`` 这种**键名**要留着 —— 丢掉就看不出"这里本来有口令"。"""

        scrubbed = self.redactor.scrub("password=a-very-long-passphrase!")

        self.assertTrue(scrubbed.startswith("password="), scrubbed)
        self.assertNotIn("a-very-long-passphrase!", scrubbed)

    def test_an_unquoted_secret_in_a_query_string_is_masked(self):
        """``httpsrv.log_message`` 会把整条请求行打进日志，query 里的 token 就漏那。"""

        scrubbed = self.redactor.scrub(
            "GET /a2a/task/7?token=0123456789abcdefghij HTTP/1.1"
        )

        self.assertNotIn("0123456789abcdefghij", scrubbed)
        self.assertIn("token=", scrubbed)

    def test_a_bearer_authorization_header_is_masked(self):
        scrubbed = self.redactor.scrub(
            "Authorization: Bearer abcdefghijklmnopqrstuvwx"
        )

        self.assertNotIn("abcdefghijklmnopqrstuvwx", scrubbed)
        self.assertIn("[REDACTED:bearer-token]", scrubbed)

    def test_credentials_are_not_correlatable_by_digest(self):
        """凭据**只遮蔽、不摘要**。

        理由（模块 docstring 有完整版）：§2.1 那条赋值形状**必然**覆盖低熵口令，
        一条可复现的摘要 + 一份口令字典 = 一个离线验证器。所以同一个凭据出现两次
        得到的是同一个**标签**，而不是同一个**摘要** —— 标签不泄漏任何值信息。
        """
        first = self.redactor.scrub("token=abcdefghijklmnopqrst")
        second = self.redactor.scrub("token=abcdefghijklmnopqrst")

        self.assertEqual(first, second)
        self.assertNotIn("abcdefghij", first)

    def test_a_short_secret_below_the_threshold_is_left_alone(self):
        """``{16,}`` 是下界；比它短的赋值不该被脱敏（否则"token=ok"这类
        计数/状态字段会全被吃掉）。"""
        scrubbed = self.redactor.scrub("token=shortish")

        self.assertEqual(scrubbed, "token=shortish")


# ----------------------------------------------------------------------
# 2. 手机号
# ----------------------------------------------------------------------
class PhoneRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_a_mainland_mobile_number_is_replaced_by_a_correlatable_digest(self):
        scrubbed = self.redactor.scrub("我的手机号是 %s" % MOBILE_PHONE)

        self.assertNotIn(MOBILE_PHONE, scrubbed)
        self.assertRegex(scrubbed, r"phone#[0-9a-f]{6}-[0-9a-f]{6}")

    def test_the_same_number_keeps_the_same_digest_across_lines(self):
        """这是"摘要而非掩码"的理由：日志行要能互相对齐。"""
        first = self.redactor.scrub("call %s now" % MOBILE_PHONE)
        second = self.redactor.scrub("again %s" % MOBILE_PHONE)

        self.assertEqual(first.split()[1], second.split()[1])

    def test_different_numbers_get_different_digests(self):
        first = self.redactor.scrub("call %s" % MOBILE_PHONE)
        second = self.redactor.scrub("call 13900139000")

        self.assertNotEqual(first, second)

    def test_the_country_code_prefix_is_swallowed_with_the_number(self):
        for written in ("+86 13800138000", "+8613800138000", "86 13800138000"):
            with self.subTest(written=written):
                scrubbed = self.redactor.scrub("dial %s ok" % written)

                self.assertNotIn("13800138000", scrubbed, scrubbed)


# ----------------------------------------------------------------------
# 3. 会话 id
# ----------------------------------------------------------------------
class ConversationIdRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_the_platform_prefix_stays_readable_and_the_local_id_is_hashed(self):
        """平台名是**公开词表**（``identity.KNOWN_PLATFORMS``），不是秘密；
        真正要藏的是 local 段。保住平台段，日志可读性损失很小。"""
        scrubbed = self.redactor.scrub("dropped from telegram:123456789")

        self.assertTrue(scrubbed.startswith("dropped from telegram:conv#"), scrubbed)
        self.assertNotIn("123456789", scrubbed)

    def test_every_declared_platform_prefix_is_recognised(self):
        """平台词表是**生成**出来的，所以新平台落地就自动覆盖 —— 这里逐个断言。"""
        from opencode_bridge import identity

        for platform in sorted(identity.KNOWN_PLATFORMS):
            with self.subTest(platform=platform):
                scrubbed = self.redactor.scrub("%s:abc123" % platform)

                self.assertIn("%s:conv#" % platform, scrubbed)
                self.assertNotIn("abc123", scrubbed)

    def test_the_legacy_prefixes_are_recognised_too(self):
        """``chat:`` / ``room:`` / ``channel:`` **仍在使用**（``state.py`` 的迁移是
        加载期重写，而歧义的 ``channel:`` 按 G5 的结论**永不迁移**）。"""
        for legacy in ("chat:55", "room:!abcDEF:example.org", "channel:C01ABCDEFGH"):
            with self.subTest(legacy=legacy):
                scrubbed = self.redactor.scrub("lookup %s" % legacy)

                self.assertIn("conv#", scrubbed)
                self.assertNotIn(legacy.split(":", 1)[1], scrubbed)

    def test_two_conversations_of_the_same_platform_are_distinguishable(self):
        first = self.redactor.scrub("slack:C01ABCDEFGH")
        second = self.redactor.scrub("slack:C01ABCDEFGI")

        self.assertNotEqual(first, second)

    def test_an_email_conversation_id_is_reported_as_a_conversation_not_an_email(self):
        """顺序是有意义的：带平台前缀的先吃，所以标签说的是"这是哪一类会话"。"""
        scrubbed = self.redactor.scrub("email:%s" % EMAIL_ADDRESS)

        self.assertIn("email:conv#", scrubbed)
        self.assertNotIn(EMAIL_ADDRESS, scrubbed)

    def test_a_legacy_qqbot_id_with_two_colons_is_handled(self):
        """qqbot 的 local 段本身含冒号（``qqbot:group:target``）。"""

        scrubbed = self.redactor.scrub("dropped qqbot:group:B2C3xxxx")

        self.assertNotIn("B2C3xxxx", scrubbed)


# ----------------------------------------------------------------------
# 4. 邮箱
# ----------------------------------------------------------------------
class EmailRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_an_address_is_replaced_by_a_correlatable_digest(self):
        scrubbed = self.redactor.scrub("mail %s now" % EMAIL_ADDRESS)

        self.assertNotIn(EMAIL_ADDRESS, scrubbed)
        self.assertRegex(scrubbed, r"email#[0-9a-f]{6}-[0-9a-f]{6}")

    def test_the_same_address_keeps_the_same_digest(self):
        first = self.redactor.scrub("from %s" % EMAIL_ADDRESS)
        second = self.redactor.scrub("to %s" % EMAIL_ADDRESS)

        self.assertEqual(first.split()[1], second.split()[1])

    def test_a_dotted_local_part_and_trailing_punctuation_are_handled(self):
        scrubbed = self.redactor.scrub("ping first.last+tag@sub.example.co.uk,")

        self.assertNotIn("first.last", scrubbed)
        self.assertTrue(scrubbed.endswith(","), scrubbed)


# ----------------------------------------------------------------------
# 5. 误伤：这些必须原样通过
# ----------------------------------------------------------------------
class FalsePositiveTests(unittest.TestCase):
    """**紧邻形态**。误伤比漏脱更难查：被毁掉的时间戳/端口会让排障本身失效。"""

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def assertUntouched(self, text: str, reason: str) -> None:
        with self.subTest(text=text, reason=reason):
            self.assertEqual(self.redactor.scrub(text), text)

    def test_a_tcp_port_is_not_a_phone_number(self):
        """本仓库的默认端口就是 4097，配置里到处都是 —— 这是最该守住的一条。"""
        self.assertUntouched(
            "opencode listening on 127.0.0.1:4097", "tcp port"
        )
        self.assertUntouched("port=8080 timeout=30", "short numbers")

    def test_model_names_are_not_phone_numbers(self):
        for name in (
            "gpt-4o-mini", "claude-3-5-sonnet-20241022", "gemini-1.5-pro-002",
            "anthropic/claude-sonnet-4-20250514", "qwen2.5-coder-32b",
        ):
            self.assertUntouched("model %s" % name, "model name")

    def test_a_version_like_pair_is_not_an_email(self):
        self.assertUntouched("sonnet@1.2", "version pair")
        self.assertUntouched("build 3.11.4 on 2026-10-04", "version + date")

    def test_platform_local_ids_are_not_timestamps(self):
        """discord 雪花号 17~20 位、mattermost 恰好 26 位 —— 两边都被手机号规则的
        边界断言挡住了（它们不是"恰好 11 位"的裸数字）。"""
        self.assertUntouched(
            "discord snowflake 123456789012345678", "discord snowflake"
        )
        self.assertUntouched(
            "mattermost channel abcdefghijklmnopqrstuvwx01", "mattermost id"
        )

    def test_long_timestamps_are_not_phone_numbers(self):
        for stamp in (
            "1759553296123456789", "1759553296123456", "19700101000000",
        ):
            self.assertUntouched("at %s" % stamp, "timestamp")

    def test_a_hex_digest_is_not_a_phone_number(self):
        self.assertUntouched(
            "sha e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "sha256",
        )

    def test_a_log_line_prefix_is_not_a_conversation_id(self):
        """本仓库大量日志以 ``<platform>: ...`` 开头。冒号后**带空格**时不是
        ``platform:local_id``，所以不该被吃掉。"""
        for line in (
            "matrix: transport error on /sync",
            "email: dropping message",
            "irc: not connected; dropping outbound message",
            "slack: splitting outbound message into 3 chunks",
            "telegram: polling started (offset=42)",
        ):
            self.assertUntouched(line, "log prefix")

    def test_a_counter_named_like_a_secret_is_not_masked(self):
        """``token_count=`` 里的 ``token`` 后面跟的是 ``_count=``，不匹配。"""
        self.assertUntouched(
            "token_count=12345678901234567890", "counter named token_count"
        )

    def test_a_short_bare_number_is_not_a_phone_number(self):
        for bare in ("138ms", "12345 pieces", "42 items", "0.85s"):
            self.assertUntouched("took %s" % bare, "short numeric")


# ----------------------------------------------------------------------
# 6. 幂等 + 不崩
# ----------------------------------------------------------------------
class IdempotencyTests(unittest.TestCase):
    """脱敏必须是**不动点**。

    一条记录会经过**每一个** handler 的过滤器；第二个过滤器若再改一次，
    日志里就会出现两个不同的假摘要 —— 那比不脱敏更难查（"同一个会话两个 id"）。
    """

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_scrubbing_twice_equals_scrubbing_once(self):
        samples = [
            "conv telegram:123456789",
            "mail %s phone %s" % (EMAIL_ADDRESS, MOBILE_PHONE),
            "token=0123456789abcdefghij",
            "conv telegram:-1001234567890",
            "conv email:%s" % EMAIL_ADDRESS,
            "Authorization: Bearer abcdefghijklmnopqrstuvwx",
            "%s" % PRIVATE_KEY,
            "conv chat:55 and conv slack:C01ABCDEFGH",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                once = self.redactor.scrub(sample)

                self.assertEqual(once, self.redactor.scrub(once))
                self.assertEqual(once, self.redactor.scrub(self.redactor.scrub(once)))

    def test_a_digest_never_contains_enough_digits_to_look_like_a_phone(self):
        """结构性质，不是运气：摘要每 6 位隔断，最多连续 6 位数字。

        没有这一条，一个 12 位纯十六进制摘要有约 1.4% 的概率含 11 位连续数字，
        于是手机号规则会**把脱敏层自己的输出再脱敏一次**。
        """
        import re

        for number in range(200):
            digest = self.redactor.fingerprint("conv", "value-%d" % number)
            self.assertIsNone(
                re.search(r"[0-9]{11}", digest), digest
            )


class FailSafeTests(unittest.TestCase):
    """脱敏层**不许抛**。会抛的脱敏层比没有更糟。"""

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_non_string_input_is_returned_unchanged(self):
        for value in (None, 42, 3.5, True, b"bytes", ["a"], {"k": "v"}, object()):
            with self.subTest(value=type(value).__name__):
                self.assertIs(self.redactor.scrub(value), value)

    def test_a_bad_hmac_key_falls_back_to_a_random_one_instead_of_raising(self):
        for key in (b"", "a string key", bytearray(b"bytearray key"), 12345):
            with self.subTest(key=repr(key)):
                digest = Redactor(key=key).fingerprint("conv", "123456789")

                self.assertRegex(digest, r"^conv#")

    def test_a_value_with_a_lone_surrogate_does_not_raise(self):
        """``normalize._clean`` 专门处理过落单代理字符，日志里可能有。"""
        scrubbed = self.redactor.scrub("conv telegram:\ud800abc")

        self.assertNotIn("\ud800", scrubbed)

    def test_a_record_whose_format_string_is_broken_still_passes_through(self):
        """``%s`` 少了参数时 ``getMessage()`` 会抛 —— 过滤器必须吞掉它，
        而不是把这条记录变成"日志系统报错的源头"。"""
        record = logging.LogRecord(
            "opencode_bridge.test", logging.INFO, __file__, 1,
            "value is %s and %s", ("only-one",), None,
        )
        the_filter = RedactingFilter(self.redactor)

        self.assertTrue(the_filter.filter(record))

    def test_a_record_whose_message_is_not_a_string_is_not_mangled(self):
        record = logging.LogRecord(
            "opencode_bridge.test", logging.INFO, __file__, 1,
            {"not": "a string"}, None, None,
        )
        original = record.msg
        the_filter = RedactingFilter(self.redactor)

        self.assertTrue(the_filter.filter(record))
        self.assertIs(record.msg, original)

    def test_the_filter_never_drops_a_record(self):
        """脱敏层只许改内容，不许丢记录 —— 吞掉一行日志比漏一个值难查得多。"""
        record = logging.LogRecord(
            "opencode_bridge.test", logging.INFO, __file__, 1,
            "conv telegram:%s", ("123456789",), None,
        )

        self.assertTrue(RedactingFilter(self.redactor).filter(record))
        self.assertIn("conv#", record.getMessage())


# ----------------------------------------------------------------------
# 7. 卡口：过滤器真的被多个适配器打到
# ----------------------------------------------------------------------
class ChokePointTests(_FilterHarness):
    ADAPTER_LOGGERS = (
        "opencode_bridge.adapters.telegram",
        "opencode_bridge.adapters.slack",
        "opencode_bridge.adapters.discord",
        "opencode_bridge.adapters.matrix",
        "opencode_bridge.adapters.irc",
        "opencode_bridge.adapters.email",
        "opencode_bridge.adapters.nextcloud",
    )

    def test_one_installed_filter_covers_records_from_several_adapters(self):
        """这是"一个卡口覆盖 13 个平台"那句话的**唯一**硬证据。

        用 ``assertLogs("opencode_bridge")``（**父** logger）：子 logger 的记录
        propagate 上来、经过父 logger 上的 handler，所以装在那个 handler 上的
        过滤器对全部适配器生效。而每个适配器**一行代码都没改**。
        """
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            covered = install_redaction_filter(redactor=make_redactor())
            self.assertTrue(covered, "过滤器没有挂到任何 handler 上")
            for name in self.ADAPTER_LOGGERS:
                logging.getLogger(name).info(
                    "conv telegram:%s token=0123456789abcdefghij", "123456789"
                )

        self.assertEqual(len(captured.records), len(self.ADAPTER_LOGGERS))
        for record in captured.records:
            with self.subTest(logger=record.name):
                text = record.getMessage()
                self.assertNotIn("123456789", text)
                self.assertNotIn("0123456789abcdefghij", text)
                self.assertIn("conv#", text)

    def test_the_same_record_passes_through_two_handlers_unchanged(self):
        """幂等性在**真实的多 handler 路径**上也成立。"""
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            covered = install_redaction_filter(redactor=make_redactor())
            covered[0].addFilter(RedactingFilter(make_redactor()))
            logging.getLogger("opencode_bridge.adapters.telegram").info(
                "conv telegram:123456789"
            )

        self.assertEqual(len(captured.records), 1)
        self.assertIn("conv#", captured.records[0].getMessage())

    def test_installing_twice_replaces_rather_than_stacking(self):
        """重装必须确定：同一个 handler 上不能叠两层，否则换个密钥不生效。"""
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO"):
            first = install_redaction_filter(redactor=Redactor(key=b"first"))
            second = install_redaction_filter(redactor=Redactor(key=b"second"))
            logging.getLogger("opencode_bridge.adapters.slack").info("conv slack:x")
            ours = [
                existing
                for existing in first[0].filters
                if getattr(existing, "_opencode_bridge_redaction", False)
            ]

        self.assertEqual(len(ours), 1)
        self.assertIs(first[0], second[0])

    def test_a_foreign_filter_on_the_same_handler_is_left_alone(self):
        """重装只摘**自己**挂上去的实例。

        用一个恒真的过滤器当"别人的"：``logging.Filter("某名字")`` 会按 logger 名
        丢弃记录，拿它当夹具会把记录一起吃掉，测的就不是"有没有被误删"了。
        """
        marker = _AlwaysPassFilter()

        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO"):
            covered = install_redaction_filter(redactor=make_redactor())
            covered[0].addFilter(marker)
            install_redaction_filter(redactor=make_redactor())
            logging.getLogger("opencode_bridge.adapters.matrix").info("conv matrix:x")

        self.assertIn(marker, covered[0].filters)
        # 只打了一条记录 ⇒ 它被走过一次；"还在"且"还在干活"，两条都要断。
        self.assertEqual(marker.seen, 1)

    def test_the_production_entry_point_installs_the_filter(self):
        """``__main__._setup_logging`` 必须真的调它 —— 否则整个卡口是死的。"""
        import inspect

        from opencode_bridge.__main__ import _setup_logging

        self.assertIn("install_redaction_filter", inspect.getsource(_setup_logging))

    def test_installing_covers_the_root_handler_that_basic_config_creates(self):
        """真实运行路径：handler 在 root 上，而全部 logger 都 propagate 到 root。

        root 的级别必须真的降到 INFO 才发得出记录 —— 生产里
        ``basicConfig(level=...)`` 就是干这个的，所以这里照做。
        """
        import io

        root = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        original_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, original_level)
        original = list(handler.filters)

        try:
            covered = install_redaction_filter(redactor=make_redactor())
            logging.getLogger("opencode_bridge.adapters.twitch").info(
                "conv twitch:alice phone %s", MOBILE_PHONE
            )
        finally:
            handler.filters = original

        self.assertIn(handler, covered)
        written = stream.getvalue()
        self.assertIn("conv#", written)
        self.assertNotIn(MOBILE_PHONE, written)
        self.assertNotIn("twitch:alice", written)


# ----------------------------------------------------------------------
# 8. 出站正文逐字节不变（本任务最重要的约束）
# ----------------------------------------------------------------------
class OutboundTextUntouchedTests(_FilterHarness):
    """**出站正文绝不许被脱敏。**

    用户必须收到 agent 产出的原文。所以这里用真的 :class:`OutboundSender` +
    真的 ``Adapter`` 基类，把一段"满是敏感形态"的正文发出去，逐字节对比。
    """

    #: 一段同时含手机号、邮箱、token、连接串与 conversation id 的正文。
    #:
    #: telegram token **引用 :data:`TELEGRAM_TOKEN`**，不写第二份 ——
    #: 留一个副本就等于把刚拆掉的连续字面量又拼回来，推送保护照样拦。
    DELIVERED = (
        "这是你要的结果：\n"
        "手机 13800138000，邮箱 bob@example.com\n"
        "token=0123456789abcdefghij\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwx\n"
        f"telegram bot {TELEGRAM_TOKEN}\n"
        "会话 telegram:123456789\n"
        "端口 127.0.0.1:4097，模型 gpt-4o-mini"
    )

    def setUp(self) -> None:
        super().setUp()
        self.adapter = ScriptedAdapter()
        self.sender = OutboundSender(
            adapter_for=lambda conversation_id: self.adapter,
            max_message_chars=4000,
        )

    @staticmethod
    def _probe(captured, level: str = "INFO") -> str:
        """同一段上下文里再打一条**含敏感值**的日志。

        作用有两个：让 ``assertLogs`` 真的有记录可断言，以及**证明脱敏在那一刻
        确实是活的** —— 否则"正文没变"可能只是因为压根没有过滤器在跑。
        """
        logging.getLogger("opencode_bridge.adapters.telegram").log(
            logging.getLevelName(level),
            "probe conv telegram:123456789 phone %s", MOBILE_PHONE,
        )
        return captured.records[0].getMessage()

    def test_the_adapter_receives_the_exact_agent_text(self):
        """**过滤器已装上**的时候发的 —— 证明"脱敏生效"与"正文不变"不冲突。"""
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            install_redaction_filter(redactor=make_redactor())
            probe = self._probe(captured)
            self.sender.send_text("telegram:123456789", self.DELIVERED)

        self.assertNotIn("123456789", probe)
        self.assertNotIn(MOBILE_PHONE, probe)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.sent[0].text, self.DELIVERED)

    def test_a_failing_send_still_delivers_nothing_but_does_not_rewrite_the_text(self):
        """失败路径也走一遍：``logger.exception`` 被脱敏，但**正文对象**没被动过。"""
        self.adapter.send_error = RuntimeError("boom")
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="ERROR") as captured:
            install_redaction_filter(redactor=make_redactor())
            self._probe(captured, level="ERROR")
            self.sender.send_text("telegram:123456789", self.DELIVERED)

        self.assertEqual(self.adapter.sent, [])
        self.assertIn("13800138000", self.DELIVERED)
        self.assertTrue(captured.records)

    def test_the_progress_edit_and_the_finalise_paths_are_also_untouched(self):
        """改写（edit）与收尾（finalize）是另外两条出站路径。"""
        from opencode_bridge.hooks import MsgHandle

        handle = MsgHandle("telegram:123456789", "m1", "telegram")
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            install_redaction_filter(redactor=make_redactor())
            self._probe(captured)
            self.sender.edit_progress("telegram:123456789", handle, self.DELIVERED, "s1")
            self.sender.finalize("telegram:123456789", handle, self.DELIVERED, "s1")

        delivered = [out.text for _handle, out in self.adapter.edited]
        delivered.extend(out.text for out in self.adapter.sent)
        self.assertTrue(delivered)
        for text in delivered:
            self.assertEqual(text, self.DELIVERED)

    def test_the_callback_answer_is_also_untouched(self):
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            install_redaction_filter(redactor=make_redactor())
            self._probe(captured)
            self.sender.answer(self.adapter, "Q1", self.DELIVERED)

        self.assertEqual(self.adapter.answers, [("Q1", self.DELIVERED)])


# ----------------------------------------------------------------------
# 9. state.json：键不动、值脱敏凭据
# ----------------------------------------------------------------------
class StateRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(dir=os.path.join(os.getcwd(), ".tmp"))
        self.addCleanup(self._cleanup)
        self.path = os.path.join(self.directory, "state.json")

    def _cleanup(self) -> None:
        for root, _dirs, names in os.walk(self.directory, topdown=False):
            for name in names:
                os.unlink(os.path.join(root, name))
        os.rmdir(self.directory)

    def read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_conversation_id_keys_are_kept_verbatim(self):
        """**键就是索引** —— 遮掉它会让每次查找落空、每条会话静默变成孤儿，
        那正是 G5 花两个 commit 修回来的失败模式。"""
        store = StateStore(self.path)
        store.set_session("telegram:123456789", "ses_abc")
        store.set_session("email:bob@example.com", "ses_def")

        on_disk = self.read()
        self.assertEqual(
            sorted(on_disk["sessions"]),
            ["email:bob@example.com", "telegram:123456789"],
        )
        self.assertEqual(store.get_session("telegram:123456789"), "ses_abc")

    def test_a_credential_shaped_value_is_scrubbed_on_disk_only(self):
        store = StateStore(self.path)
        store.set_meta("telegram:1", "last_error", "password=a-very-long-passphrase")

        on_disk = self.read()
        self.assertNotIn(
            "a-very-long-passphrase", on_disk["meta"]["telegram:1"]["last_error"]
        )
        # 内存里不动 ⇒ 同进程内读回行为零变化
        self.assertEqual(
            store.get_meta("telegram:1", "last_error"),
            "password=a-very-long-passphrase",
        )

    def test_an_integer_meta_value_is_never_touched(self):
        """``stream_cursor`` 是整数游标，改它会让邮件重投逻辑失效。"""
        store = StateStore(self.path)
        store.set_meta("email:bob@example.com", "stream_cursor", 1234567890)

        self.assertEqual(
            self.read()["meta"]["email:bob@example.com"]["stream_cursor"], 1234567890
        )
        self.assertEqual(
            store.get_meta("email:bob@example.com", "stream_cursor"), 1234567890
        )

    def test_a_directory_meta_value_is_never_touched(self):
        """``directory`` 是 ``/cd`` 的目标，被改会让 agent 静默去到别处。"""
        store = StateStore(self.path)
        store.set_meta("telegram:1", "directory", "/srv/work/13800138000")

        self.assertEqual(
            self.read()["meta"]["telegram:1"]["directory"], "/srv/work/13800138000"
        )
        self.assertEqual(
            store.get_meta("telegram:1", "directory"), "/srv/work/13800138000"
        )

    def test_a_round_trip_through_disk_keeps_every_lookup_working(self):
        store = StateStore(self.path)
        store.set_session("telegram:123456789", "ses_abc")
        store.set_meta("telegram:123456789", "directory", "/srv/app")

        reopened = StateStore(self.path)

        self.assertEqual(reopened.get_session("telegram:123456789"), "ses_abc")
        self.assertEqual(
            reopened.get_meta("telegram:123456789", "directory"), "/srv/app"
        )

    def test_redacting_a_document_leaves_the_callers_structure_alone(self):
        """脱敏必须产出一份**新**结构：改原对象会连带改掉同进程内的读回结果。"""
        document = {"meta": {"telegram:1": {"last_error": "token=0123456789abcdefghij"}}}

        redacted = redact_state_values(document, redactor=make_redactor())

        self.assertEqual(
            document["meta"]["telegram:1"]["last_error"], "token=0123456789abcdefghij"
        )
        self.assertNotIn(
            "0123456789abcdefghij", redacted["meta"]["telegram:1"]["last_error"]
        )
        self.assertIn("telegram:1", redacted["meta"])


# ----------------------------------------------------------------------
# 10. 全仓库格式串审计：误伤面的直接证据
# ----------------------------------------------------------------------
class RepoFormatStringAuditTests(unittest.TestCase):
    """把本仓库**全部** log 格式串扫一遍，断言脱敏器一个都不改。

    这是误伤面的**直接**证据，而不是"我抽查了几条觉得没事"。
    覆盖面随仓库增长自动变大 —— 下一个适配器落地时这条会继续守着。
    """

    @staticmethod
    def _format_strings() -> list:
        import opencode_bridge

        package_dir = pathlib.Path(opencode_bridge.__file__).parent
        found = []
        for path in sorted(package_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "logger"
                ):
                    continue
                if func.attr not in (
                    "debug", "info", "warning", "error", "exception",
                    "critical", "log",
                ):
                    continue
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(
                        argument.value, str
                    ):
                        found.append((path.name, node.lineno, argument.value))
        return found

    def test_no_repository_log_format_string_is_altered(self):
        redactor = make_redactor()
        altered = [
            (name, line, text, redactor.scrub(text))
            for name, line, text in self._format_strings()
            if redactor.scrub(text) != text
        ]

        self.assertEqual(altered, [], "这些格式串被误伤了")

    def test_the_audit_actually_found_the_repository_log_calls(self):
        """防空跑：如果解析器哪天匹配不到东西，上面那条断言会变成永远绿。"""
        self.assertGreater(len(self._format_strings()), 100)


# ----------------------------------------------------------------------
# 11. 非空洞性：关掉过滤器这些用例就该红
# ----------------------------------------------------------------------
class NonVacuityTests(_FilterHarness):
    """**对照**用例：证明"过滤器没装时明文确实会漏出去"。

    没有这一组，上面那些断言有可能是因为某些无关原因变绿的。把过滤器摘掉，
    :meth:`test_without_the_filter_the_log_line_still_holds_the_plaintext`
    就会失败 —— 这就是"关掉过滤器测试就该红"的机械证据。
    """

    def test_without_the_filter_the_log_line_still_holds_the_plaintext(self):
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            logging.getLogger("opencode_bridge.adapters.telegram").info(
                "conv telegram:%s phone %s mail %s token=0123456789abcdefghij",
                "123456789", MOBILE_PHONE, EMAIL_ADDRESS,
            )

        text = captured.records[0].getMessage()
        self.assertIn("telegram:123456789", text)
        self.assertIn(MOBILE_PHONE, text)
        self.assertIn(EMAIL_ADDRESS, text)
        self.assertIn("0123456789abcdefghij", text)

    def test_with_the_filter_the_same_line_holds_none_of_them(self):
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="INFO") as captured:
            install_redaction_filter(redactor=make_redactor())
            logging.getLogger("opencode_bridge.adapters.telegram").info(
                "conv telegram:%s phone %s mail %s token=0123456789abcdefghij",
                "123456789", MOBILE_PHONE, EMAIL_ADDRESS,
            )

        text = captured.records[0].getMessage()
        self.assertNotIn("telegram:123456789", text)
        self.assertNotIn(MOBILE_PHONE, text)
        self.assertNotIn(EMAIL_ADDRESS, text)
        self.assertNotIn("0123456789abcdefghij", text)

    def test_the_traceback_of_a_logged_exception_is_scrubbed_too(self):
        """栈里可能有请求头/URL；``logger.exception`` 的 ``exc_text`` 是 formatter
        之后才渲染的，所以必须在过滤器里预先填好一份脱敏过的。"""
        with self.assertLogs(redaction.LOGGER_NAMESPACE, level="ERROR") as captured:
            install_redaction_filter(redactor=make_redactor())
            try:
                raise RuntimeError(
                    "GET /a2a?token=0123456789abcdefghij failed for %s"
                    % EMAIL_ADDRESS
                )
            except RuntimeError:
                logging.getLogger("opencode_bridge.adapters.a2a").exception("boom")

        rendered = captured.output[0]
        self.assertIn("Traceback", rendered)
        self.assertNotIn("0123456789abcdefghij", rendered)
        self.assertNotIn(EMAIL_ADDRESS, rendered)

    def test_a_traceback_that_is_not_an_exception_is_left_alone(self):
        record = logging.LogRecord(
            "opencode_bridge.test", logging.ERROR, __file__, 1, "no exc", None, None
        )

        RedactingFilter(make_redactor()).filter(record)

        self.assertIsNone(record.exc_text)


# ----------------------------------------------------------------------
# 12. 守卫：拼出来的值**仍是真形状**，而源码里**没有**可匹配的连续片段
# ----------------------------------------------------------------------
#: GitHub Push Protection 实际会拦的形状 —— 即 §2.1 那六条，但 Slack 那条
#: **要求前缀后面还有一段正文**（``{10,}``）。
#:
#: 为什么不给 Slack 照抄 §2.1 的前缀：§2.1 列的是**前缀**，而前缀在本仓库
#: 到处都是 —— README、docs/、``commands.py`` 的报错文案、既有测试里那些
#: 6 字符的假值。它们**从来没被拦过**；被拦的是"前缀 + 一段像密钥的正文"，
#: 也就是本文件这批夹具当初撞上 GH013 的原因。把"前缀"当判据会得出
#: "仓库本来就不合规"，那是错的前提。
#:
#: Slack 那条写成 ``x(?:ox[bp]|app)-``：与 §2.1 的三个前缀**完全等价**，
#: 但源码里不出现任何一个前缀的完整字面量，于是这张表**不会自己匹配自己**。
PUSH_PROTECTION_SHAPES = (
    ("telegram-bot-token", r"\d{8,10}:[A-Za-z0-9_-]{35}"),
    ("slack", r"x(?:ox[bp]|app)-[A-Za-z0-9-]{10,}"),
    ("github", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("openai", r"sk-[A-Za-z0-9]{20,}"),
    ("aws", r"AKIA[0-9A-Z]{16}"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    (
        "literal-assignment",
        r"""(token|secret|password|api_key|app_secret)\s*[=:]\s*['"][^'"]{16,}['"]""",
    ),
)


class FixtureShapeTests(unittest.TestCase):
    """把"拆碎但形状不变"这条不变量钉住。

    GitHub Push Protection 拦的是**源码里连续的、可按形状匹配的完整凭据**；
    而脱敏器只认**运行时值的形状**。两者必须同时成立，所以两个方向都要断：
    值仍匹配（否则测试测不到东西），源码不匹配（否则推送被拒）。
    """

    #: 常量 → 上表里的形状标签。用标签而不是再抄一遍正则，
    #: 这样"判定标准"只有 :data:`PUSH_PROTECTION_SHAPES` 一处。
    FIXTURES = (
        ("telegram-bot-token", lambda: TELEGRAM_TOKEN),
        ("slack", lambda: SLACK_TOKEN),
        ("slack", lambda: SLACK_APP_TOKEN),
        ("github", lambda: GITHUB_TOKEN),
        ("openai", lambda: OPENAI_KEY),
        ("aws", lambda: AWS_KEY_ID),
        ("private-key", lambda: PRIVATE_KEY),
    )

    def test_every_fragmented_fixture_still_matches_its_push_protection_shape(self):
        for shape, read_value in self.FIXTURES:
            with self.subTest(shape=shape):
                self.assertRegex(read_value(), _shape_pattern(shape))

    def test_the_outbound_body_carries_the_very_same_telegram_token(self):
        """``DELIVERED`` 引用常量而不是抄一份，所以两者必然同一。"""
        self.assertIn(TELEGRAM_TOKEN, OutboundTextUntouchedTests.DELIVERED)

    def test_this_source_file_has_no_credential_shaped_literal(self):
        """**验收条件本身**：push protection 就是按这个扫的。

        2026-10-04 实测：commit ``7b039ed`` 因本文件里的 Slack 字面量被
        ``GH013`` 拒下。所以这条断言不是"洁癖"，是"别再被拒一次"。
        """
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        found = [
            "%s at line %d: %r" % (label, lineno, match.group(0))
            for lineno, line in enumerate(source.split("\n"), 1)
            for label, pattern in PUSH_PROTECTION_SHAPES
            for match in re.finditer(pattern, line)
        ]

        self.assertEqual(found, [], "源码里出现了可被推送保护匹配的片段：%s" % found)

    def test_the_shape_table_itself_is_not_matched_by_its_own_patterns(self):
        """防空跑：若哪天有人把某条形状写成**自匹配**的写法，
        上面那条就会因为"自己人"而失败、变得无法解释。"""
        table = "".join(pattern for _label, pattern in PUSH_PROTECTION_SHAPES)

        for label, pattern in PUSH_PROTECTION_SHAPES:
            with self.subTest(shape=label):
                self.assertIsNone(re.search(pattern, table))


def _shape_pattern(shape: str) -> str:
    for label, pattern in PUSH_PROTECTION_SHAPES:
        if label == shape:
            return pattern
    raise KeyError(shape)


if __name__ == "__main__":
    unittest.main()
