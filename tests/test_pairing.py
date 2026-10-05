"""配对码：派生、归一化、比对，以及**未授权时那条回复**。

这个模块（:mod:`opencode_bridge.pairing`）是纯函数，所以这些测试全都直接钉它的
输出，不经由适配器 —— 适配器那层的接线由 ``test_adapters.py`` /
``test_platform_pairing.py`` 覆盖。

⚠️ 这里断言的**不是**"某个码长什么样"（那是实现细节，会被合法改动打断），而是
**四条承重性质**：绑定会话、域分隔、空 secret 不派生、归一化两侧一致。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import unittest

from opencode_bridge.config import Config

from opencode_bridge.pairing import (
    CONFIG_VERSION_KEY,
    EMPTY_ALLOWLIST_DENY_FROM_VERSION,
    PAIRING_CODE_LENGTH,
    PAIRING_SECRET_KEY,
    PAIRING_TRIGGER,
    derive_pairing_code,
    empty_allowlist_is_open,
    normalize_pairing_code,
    pairing_code_matches,
    pairing_is_available,
    pairing_reply_text,
    reads_pairing_trigger,
    warn_if_pairing_unavailable,
)

SECRET = "本机配对密钥-勿外传"
PLATFORM = "telegram"
CONVERSATION = "telegram:12345"
OTHER_CONVERSATION = "telegram:99999"


def a2a_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "opencode_bridge",
        "adapters",
        "a2a.py",
    )


class TestDerivation(unittest.TestCase):
    """派生的四条性质。"""

    def test_code_has_eight_characters_from_the_base32_alphabet(self):
        """8 字符，且**只含** base32 的字母表。

        ⛔ 刻意断言"不含 0/1/8/9"：base32 的字母表是 ``A-Z2-7``，这从**根上**
        消灭了 ``O/0``、``I/l/1`` 这类抄错（base36 会包含它们）。只断言长度的话，
        有人换成 base36 这条测试照样绿，而用户会开始抄错码。
        """
        code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        self.assertEqual(len(code), PAIRING_CODE_LENGTH)
        self.assertEqual(len(code), 8)
        alphabet = set("abcdefghijklmnopqrstuvwxyz234567")
        self.assertTrue(
            set(code) <= alphabet,
            f"码含 base32 字母表外的字符 {sorted(set(code) - alphabet)} —— "
            f"O/0、I/l/1 抄错会因此重新出现",
        )
        self.assertEqual(code, code.lower(), "必须是小写 base32")

    def test_same_inputs_give_the_same_code(self):
        self.assertEqual(
            derive_pairing_code(SECRET, PLATFORM, CONVERSATION),
            derive_pairing_code(SECRET, PLATFORM, CONVERSATION),
        )

    def test_code_is_bound_to_the_conversation(self):
        """⚠️ 承重：同一 secret 在**另一个会话**上得出**另一个**码。

        没有这条，群里看到的码就能授权群外的会话（而授权范围正是回信里承诺的
        "只授权这一个会话"）。
        """
        here = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        elsewhere = derive_pairing_code(SECRET, PLATFORM, OTHER_CONVERSATION)
        self.assertNotEqual(here, elsewhere)

    def test_code_is_bound_to_the_platform(self):
        """同一个 local id 在两个平台上必须是**两个不同的码**。

        没有这条，"telegram:12345" 的码就能拿去授权 "slack:12345"。
        """
        self.assertNotEqual(
            derive_pairing_code(SECRET, "telegram", "telegram:12345"),
            derive_pairing_code(SECRET, "slack", "slack:12345"),
        )

    def test_code_changes_with_the_secret(self):
        self.assertNotEqual(
            derive_pairing_code(SECRET, PLATFORM, CONVERSATION),
            derive_pairing_code("另一个密钥", PLATFORM, CONVERSATION),
        )

    def test_uses_hmac_not_a_bare_hash(self):
        """⚠️ 必须证明用的是 **HMAC** 而不是裸 hash —— 两者输出完全不同。

        这条断言的是"**不是**裸 hash"，而不是"等于某个值"：裸 SHA-256 的前缀
        一旦被当成期望值写死，实现就再也换不回 HMAC 了，而两者在密码学性质上
        差着 PRF 与长度扩展。
        """
        code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        bare = hashlib.sha256(
            b"opencode-bridge/pair/v1\n" + PLATFORM.encode() + b"\n" + CONVERSATION.encode()
        ).digest()
        import base64

        bare_code = base64.b32encode(bare).decode("ascii").lower()[:8]
        self.assertNotEqual(
            code, bare_code,
            "派生结果与裸 SHA-256 相同 ⇒ 用的是裸 hash，不是 HMAC",
        )
        # 而它必须**恰好**等于按文档公式手算的 HMAC。
        expected = hmac.new(
            SECRET.encode("utf-8"),
            b"opencode-bridge/pair/v1\n"
            + PLATFORM.encode("utf-8")
            + b"\n"
            + CONVERSATION.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        self.assertEqual(
            code,
            base64.b32encode(expected).decode("ascii").lower()[:8],
            "必须与模块 docstring 里写的公式逐字一致（HMAC-SHA256 + 域分隔 + base32）",
        )

    def test_domain_separation_is_present(self):
        """⛔ 域分隔串必须**真的参与**派生。

        做法：手算一个**不带**域分隔串的 HMAC，断言结果**不同**。若哪天有人把
        ``_PAIRING_DOMAIN`` 删掉，这条会红 —— 而删掉它意味着同一个 secret 在别的
        用途下派生出的值可以直接当码用。
        """
        import base64

        code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        without_domain = hmac.new(
            SECRET.encode("utf-8"),
            PLATFORM.encode("utf-8") + b"\n" + CONVERSATION.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        self.assertNotEqual(
            code,
            base64.b32encode(without_domain).decode("ascii").lower()[:8],
            "去掉域分隔串后派生结果必须不同 —— 否则别的用途派生的值能当码用",
        )


class TestEmptySecretMeansNoPairing(unittest.TestCase):
    """⛔ 空 secret 的语义是「不提供配对」，**绝不是**「用空串派生」。"""

    def test_empty_secret_derives_nothing(self):
        for empty in ("", "   ", None):
            with self.subTest(secret=repr(empty)):
                self.assertEqual(derive_pairing_code(empty, PLATFORM, CONVERSATION), "")

    def test_empty_secret_is_not_a_constant_anyone_can_compute(self):
        """回归防线：万一有人"顺手"让空 secret 走派生路径。"""
        from_empty = derive_pairing_code("", PLATFORM, CONVERSATION)
        self.assertEqual(from_empty, "", "空 secret 必须返回空串（= 无配对）")
        self.assertNotEqual(from_empty, derive_pairing_code(SECRET, PLATFORM, CONVERSATION))

    def test_pairing_is_available_reflects_the_secret(self):
        self.assertTrue(pairing_is_available(SECRET))
        for empty in ("", "  ", None):
            with self.subTest(secret=repr(empty)):
                self.assertFalse(pairing_is_available(empty))

    def test_a_code_never_matches_without_a_secret(self):
        self.assertFalse(
            pairing_code_matches("abcd2345", "", PLATFORM, CONVERSATION),
            "无 secret ⇒ 任何码都不该通过验证",
        )


class TestNormalization(unittest.TestCase):
    """归一化：两侧必须**同一函数**，否则用户会把 ``abcd-efgh`` 的失败当成 bug。"""

    def test_strips_lowercases_and_drops_dashes_and_spaces(self):
        self.assertEqual(normalize_pairing_code("  ABCD-2345  "), "abcd2345")
        self.assertEqual(normalize_pairing_code("ABCD 2345"), "abcd2345")
        self.assertEqual(normalize_pairing_code("a-b-c-d"), "abcd")

    def test_matching_accepts_every_spelling_the_user_might_type(self):
        code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        half = len(code) // 2
        for spelling in (
            code,
            code.upper(),
            f"  {code}  ",
            f"{code[:half]}-{code[half:]}",
            f"{code[:half]} {code[half:]}",
            f"{code[:half]} - {code[half:]}",
        ):
            with self.subTest(spelling=spelling):
                self.assertTrue(
                    pairing_code_matches(spelling, SECRET, PLATFORM, CONVERSATION),
                    f"用户照抄成 {spelling!r} 必须仍能通过 —— "
                    f"归一化失败会被当成 bug 报上来",
                )

    def test_wrong_codes_are_rejected(self):
        code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        for wrong in ("", "        ", "zzzzzzzz", code[:-1] + ("a" if code[-1] != "a" else "b")):
            with self.subTest(code=repr(wrong)):
                self.assertFalse(
                    pairing_code_matches(wrong, SECRET, PLATFORM, CONVERSATION)
                )

    def test_a_code_from_another_conversation_does_not_match(self):
        elsewhere = derive_pairing_code(SECRET, PLATFORM, OTHER_CONVERSATION)
        self.assertFalse(
            pairing_code_matches(elsewhere, SECRET, PLATFORM, CONVERSATION),
            "另一个会话的码不得通过验证",
        )

    def test_comparison_uses_the_constant_time_primitive(self):
        """⚠️ 这条守的是**惯例**而不是防护（这里没有时序攻击可打）。

        它存在的理由是 :mod:`opencode_bridge.pairing` 的 docstring 里那句
        "重访条件"：一旦验证真的上了网络面，**恒定时间**必须已经在位。而
        ``==`` 与 ``compare_digest`` 在本地 CLI 上**行为完全等价** —— 所以纯行为
        断言永远抓不到有人把它换掉，这条是唯一能抓的。

        换掉它的代价：将来把验证暴露到网络面时，"限流 + 恒定时间"那条备忘就
        失效了，而没人会记得去补。
        """
        import ast
        import inspect

        import opencode_bridge.pairing as pairing_module

        # ⚠️ **只看代码，不看 docstring**。第一版这条断言是
        # ``assertIn("compare_digest", inspect.getsource(...))`` —— 而那个
        # docstring 里恰好写着 ``hmac.compare_digest``（解释它在这里**不是**安全
        # 控制），于是断言匹配到了**散文**而不是代码：把实现换成 ``==`` 照样绿。
        # 这正是本仓库反复出现的"恒真的测试比没有测试更危险"。
        # 所以改成在 AST 上找 ``hmac.compare_digest`` 这个**调用节点**。
        tree = ast.parse(inspect.getsource(pairing_module.pairing_code_matches))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn(
            "compare_digest", called,
            "码的比对必须用 hmac.compare_digest —— 它在这里不是安全控制，"
            "而是「将来验证上网络面时这条已经就位」的保证",
        )
        self.assertIn(
            "compare_digest",
            io.open(a2a_path(), encoding="utf-8").read(),
            "仓库里已有的惯例来自 adapters/a2a.py —— 若那边没了，这条断言的"
            "「照既有做法」前提就失效了，该重新评估而不是照抄",
        )


class TestConfigVersionGate(unittest.TestCase):
    """「空清单」的含义按 ``config_version`` 分两套 —— 判定只有一处。"""

    def test_missing_or_zero_keeps_the_legacy_open_semantics(self):
        for version in (None, 0, 1):
            with self.subTest(config_version=version):
                self.assertTrue(
                    empty_allowlist_is_open(version),
                    "没有 config_version 的既有配置必须仍是「空 = 全开」，"
                    "否则配好凭据却没配白名单的用户会突然被关在门外",
                )

    def test_two_and_above_denies_everybody(self):
        for version in (2, 3, 99, "2", " 3 "):
            with self.subTest(config_version=repr(version)):
                self.assertFalse(empty_allowlist_is_open(version))

    def test_unreadable_values_fall_back_to_the_open_semantics(self):
        """读不懂就当没写（= 旧语义）—— 与 ``Config._coerce_ok`` 同一条纪律。

        方向是刻意的：多放行等于"回到改动前的行为"，而少放行会把用户锁在外面。
        """
        for junk in (True, False, None, [], {}, object(), "2.0", "0x2", "two", ""):
            with self.subTest(value=repr(junk)):
                self.assertTrue(empty_allowlist_is_open(junk))

    def test_the_threshold_constant_is_two(self):
        self.assertEqual(EMPTY_ALLOWLIST_DENY_FROM_VERSION, 2)
        self.assertEqual(CONFIG_VERSION_KEY, "config_version")
        self.assertEqual(PAIRING_SECRET_KEY, "pairing_secret")


class TestTriggerRecognition(unittest.TestCase):
    """适配器层必须**自己**再认一次命令 —— 未授权的消息活不到命令解析那一步。"""

    def test_recognises_the_trigger(self):
        for text in (
            "/pair",
            "  /pair",
            "/pair ",
            "/PAIR",
            "/pair@my_bot",
        ):
            with self.subTest(text=repr(text)):
                self.assertTrue(reads_pairing_trigger(text))

    def test_ignores_everything_else(self):
        for text in (
            "", "   ", None,
            "/help", "/pairing", "/setup", "pair", "please /pair",
            "/pairs", "/pairing now", 1,
            # ⚠️ 承重的反例：去掉"必须带前导斜杠"那道检查之后，这两个会**误判**
            # （切词后砍掉首字符，"xpair"→"pair"）。它们是那道检查存在的唯一理由。
            "xpair", "/xpair", "//pair",
        ):
            with self.subTest(text=repr(text)):
                self.assertFalse(
                    reads_pairing_trigger(text),
                    f"{text!r} 不是配对请求 —— 误判会让任何一条消息都收到一条配对码",
                )

    def test_tolerates_trailing_arguments_like_every_other_command(self):
        """``/pair 现在就配`` 仍算配对请求 —— 与 ``/approve 1`` 同一套切词。

        刻意**宽松**：拒绝它等于让一次真诚的配对尝试**静默丢弃**，而用户的
        诊断会是"bot 不理我"。多回一条码的代价远小于那个。
        """
        self.assertTrue(reads_pairing_trigger("/pair 现在就配"))
        self.assertTrue(reads_pairing_trigger("/pair abc"))

    def test_trigger_is_a_single_literal_and_not_setup(self):
        """⛔ 触发词必须是 ``/pair``，且**不是** ``/setup``。

        ``/setup`` 的文案冻结在 ``commands.py`` 且被测试钉住，让同一个词在
        授权 / 未授权两种状态下表示两件完全不同的事，等于把冻结输出变成状态相关的。
        """
        self.assertEqual(PAIRING_TRIGGER, "/pair")
        self.assertNotEqual(PAIRING_TRIGGER, "/setup")
        self.assertFalse(reads_pairing_trigger("/setup"))


class TestReplyText(unittest.TestCase):
    """⚠️ 回信内容是**安全措施**，不是措辞偏好。"""

    def setUp(self) -> None:
        self.code = derive_pairing_code(SECRET, PLATFORM, CONVERSATION)
        self.text = pairing_reply_text("Telegram", CONVERSATION, self.code)

    def test_names_the_platform_the_conversation_and_the_code(self):
        self.assertIn("Telegram", self.text)
        self.assertIn(CONVERSATION, self.text)
        self.assertIn(self.code, self.text)

    def test_carries_the_scope_statement(self):
        """承重：范围声明必须**完整**出现，一句都不能省。"""
        self.assertIn("只", self.text)
        self.assertIn(CONVERSATION, self.text)
        self.assertIn("群里其他人", self.text)
        self.assertIn("私聊", self.text)

    def test_contains_no_filesystem_path(self):
        """⛔ 回信里不许出现任何**路径**。

        理由：``config_path`` 已由 ``--setup --json`` 暴露，而把它打进 chat 等于
        **把本机目录发到群里**（``AGENTS.md`` §2.2）。只回命令形状。

        ⚠️ 刻意**不**断言"没有正斜杠"：``/pair`` 与 ``--pair`` 里的斜杠是命令
        本身的一部分，不是路径。按字符黑名单来判会把这个正确的文案判成红的 ——
        那才是"恒红的断言"，比没有断言更坏。所以这里判的是**路径的形状**
        （盘符、用户目录、家目录、配置文件名），不是某一个字符。
        """
        import os

        self.assertNotIn("\\", self.text, "不许出现反斜杠（Windows 路径分隔符）")
        self.assertNotIn(os.sep, self.text, "不许出现本机路径分隔符")
        for needle in ("Users", "C:", "~", "config.json", ".py", "/home/", "/etc/"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.text, f"回信里出现了 {needle!r}")
        # 逐个斜杠检查：每个都必须属于 ``/pair`` 或 ``--pair`` 这**两条命令**本身。
        # 按斜杠切分后，第一段是命令前缀（含 ``-``），其余段是紧随其后的中文。
        segments = self.text.split("/")
        self.assertGreaterEqual(len(segments), 2, "至少要有 /pair 这一处")
        for index, segment in enumerate(segments[1:], start=1):
            head = segment.split(" ", 1)[0]
            with self.subTest(index=index, head=head):
                self.assertIn(
                    head, ("pair",),
                    f"第 {index} 处斜杠后是 {head!r} —— 只有 /pair 与 --pair "
                    f"里的斜杠是合法的，其余都是路径",
                )

    def test_is_not_an_enumeration_oracle(self):
        """⛔ 不含配置回显、条目数、"这个 chat 是否已授权"。"""
        for needle in ("allowed_chat_ids", "项", "条", "已授权", "尚未授权", "在白名单里"):
            with self.subTest(needle=needle):
                self.assertNotIn(
                    needle, self.text,
                    f"回信里出现 {needle!r} —— 那是给未授权者的**枚举 oracle**",
                )

    def test_tells_the_user_a_restart_is_needed(self):
        self.assertIn("--pair", self.text)
        self.assertIn("重启", self.text)

    def test_reply_gives_the_runnable_command(self):
        """⚠️ 回信里那条命令必须**能真敲出来**。

        本仓库 ``pyproject.toml`` **没有** ``[project.scripts]`` ⇒
        ``opencode-bridge`` 这个命令**不存在**，全仓库一律
        ``python -m opencode_bridge``。回一句不存在的命令，用户照抄后拿到
        "No such file or directory" —— 而他此刻正被自己的桥接挡在门外。
        """
        self.assertIn(
            f"python -m opencode_bridge --pair {self.code} --conversation {CONVERSATION}",
            self.text,
            "回信必须给出完整的、可执行的命令",
        )

    def test_reply_does_not_contain_the_nonexistent_console_script(self):
        """⛔ 裸形式 ``opencode-bridge --`` 不许出现。

        ⚠️ 判据是**带连字符**的 ``opencode-bridge --``：正确形式里的
        ``opencode_bridge --pair``（**下划线**）合法，而断言若写成
        ``"opencode_bridge --"`` 就会把**正确**的命令判成红的 —— 那是一条恒红的
        断言，比没有断言更坏。
        """
        self.assertNotIn(
            "opencode-bridge --", self.text,
            "那个命令不存在（pyproject.toml 无 [project.scripts]）—— "
            "用户照抄只会得到 No such file or directory",
        )

    def test_the_given_command_covers_both_required_arguments(self):
        """⛔ **首次配对必须带** ``--conversation`` —— 少一个就一定失败。

        码绑定会话，只凭一串码无法确定是哪个（要枚举所有可能的会话 id = 无界
        搜索，也等于给 40 bit 造一个 oracle）。所以这不是"写全一点"，是**正确性**。
        """
        self.assertIn("--pair", self.text)
        self.assertIn("--conversation", self.text)
        self.assertIn(
            CONVERSATION, self.text,
            "--conversation 后面必须**填上**那个值，而不是只给个参数名 —— "
            "参数名对用户没有任何用处",
        )

    def test_the_command_in_the_reply_actually_parses(self):
        """⚠️ 把回信里那条命令**真的**喂给 CLI 的 argparse。

        这条比"断言字符串存在"强一档：它让回信文案与 CLI 的真实参数面**绑在一起**。
        将来有人给 ``--pair`` 改名或改语义，这条会红 —— 而那正是"用户照抄回信里的
        命令却跑不起来"的那一天。

        ⚠️ argparse 只看得到 ``sys.argv[1:]``，也就是**去掉解释器前缀之后**的部分。
        所以这里先断言前缀恰好是 ``python -m opencode_bridge``（顺带守住"不许用
        那个不存在的 console script"），再把剩下的交给解析器。
        """
        import shlex

        from opencode_bridge.__main__ import build_parser

        tokens = shlex.split(self._command_line())
        self.assertEqual(
            tokens[:3], ["python", "-m", "opencode_bridge"],
            "回信里的命令必须用**模块**形式 —— 那个 console script 不存在",
        )
        parsed = build_parser().parse_args(tokens[3:])
        self.assertEqual(
            parsed.pair, self.code, "回信里的码必须能被 --pair 接住"
        )
        self.assertEqual(
            parsed.conversation, CONVERSATION,
            "回信里的会话必须能被 --conversation 接住",
        )

    def _command_line(self) -> str:
        """回信里"在本机跑："后面那一条命令。"""
        line = next(
            line for line in self.text.splitlines() if "在本机跑：" in line
        )
        return line.split("在本机跑：", 1)[1].strip()

    def test_the_given_command_actually_redeems_the_code(self):
        """⚠️ **端到端**：把回信里那条命令原样跑一遍，必须真的授权成功。

        这是整套回信文案唯一的"它是否有用"的判据。前面的用例都在断字符串，
        而这条把它接到 :func:`opencode_bridge.pairing_cli.run_pair` 上 ——
        照抄不出来 / 少个参数 / 会话名写错，三种失败在这里都会变红。
        """
        import shlex
        import tempfile

        from opencode_bridge import pairing_cli
        from opencode_bridge.__main__ import build_parser

        argv = shlex.split(self._command_line())[3:]
        parsed = build_parser().parse_args(argv)

        with tempfile.TemporaryDirectory() as bridge_dir:
            config_path = os.path.join(bridge_dir, "config.json")
            with io.open(config_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(
                    {
                        PAIRING_SECRET_KEY: SECRET,
                        "adapters": {
                            "telegram": {"bot_token": "t", "allowed_chat_ids": []}
                        },
                    },
                    fh,
                )
            previous_env = os.environ.get("OPENCODE_BRIDGE_CONFIG")
            previous_cwd = os.getcwd()
            os.environ["OPENCODE_BRIDGE_CONFIG"] = config_path
            os.chdir(bridge_dir)
            try:
                # ⚠️ **必须**用 ``Config.load``，不能手搓一个 ``Config``：
                # :func:`run_pair` 会检查 ``cfg.adapters`` 里有没有那个平台，而
                # ``adapters`` 只有从文件加载才会有。手搓一个 ``Config(secret=...)``
                # 会让这条用例**因为错误的原因失败**（报「配置里没有
                # adapters.telegram」）—— 而那正是第一版栽的坑。
                loaded = Config.load(config_path)
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    exit_code = pairing_cli.run_pair(
                        loaded, parsed.pair, parsed.conversation
                    )
            finally:
                os.chdir(previous_cwd)
                if previous_env is None:
                    os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
                else:
                    os.environ["OPENCODE_BRIDGE_CONFIG"] = previous_env
            with io.open(config_path, encoding="utf-8") as fh:
                written = json.load(fh)
        self.assertEqual(exit_code, 0, "回信里那条命令必须真的跑得通")
        self.assertEqual(
            written["adapters"]["telegram"]["allowed_chat_ids"], [CONVERSATION]
        )
        self.assertGreaterEqual(written["config_version"], 2)


class TestUnavailablePairingIsStated(unittest.TestCase):
    """空 secret 的可见性：一行 log，不打断启动。"""

    def test_logs_once_when_credentials_are_present(self):
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.pairing")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            warn_if_pairing_unavailable("Telegram", "", has_credentials=True)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertTrue(records, "空 secret 必须有一行日志，否则用户不知道 /pair 不会回信")
        message = records[0].getMessage()
        self.assertIn("Telegram", message)
        self.assertIn(PAIRING_SECRET_KEY, message)

    def test_stays_quiet_without_credentials(self):
        import logging

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("opencode_bridge.pairing")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            warn_if_pairing_unavailable("Telegram", "", has_credentials=False)
            warn_if_pairing_unavailable("Telegram", SECRET, has_credentials=True)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(
            records, [],
            "没凭据（收不到消息）或已配 secret 时都不许喊 —— 否则是纯噪音",
        )


if __name__ == "__main__":
    unittest.main()