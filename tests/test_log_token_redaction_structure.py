"""结构断言：判据是**数据流**，不是变量名 —— 给两处「日志不该打凭据 / 载荷内容」用。

为什么结构断言必须与行为断言**分开**一个文件：它读源码，而行为断言跑代码。
混在一起时，一旦结构判据先红，读的人会以为行为也坏了。

三份测试的分工：

* :mod:`tests.test_nextcloud_log_token_redaction` —— 跑代码，逐处断言落盘那一行；
* :mod:`tests.test_telegram_callback_log_shape` —— 同上，telegram 那一处；
* **本文件** —— 读源码，判"每一处都被包住了，且没顺手改别的"。

⚠️ 每条结构断言都配一条**前提断言**（``空集 ≠ 不存在``）：先证明判据找得到东西，
下面那些"不许有 X"才有意义 —— 否则判据坏了会给出**恒绿**。
"""

from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"
NEXTCLOUD_SOURCE = ADAPTERS_DIR / "nextcloud.py"
TELEGRAM_SOURCE = ADAPTERS_DIR / "telegram.py"

#: nextcloud 里"实参出现 token"的 logger 调用总数 = 本次 9 处 + a6136e9 那 1 处。
NEXTCLOUD_TOKEN_SITES = 10


def read_source(path: Path) -> str:
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def logger_calls(source: str):
    """源码里所有 ``logger.<level>(...)`` 调用（按出现顺序）。

    ⚠️ 判据是**结构**（谁调了 logger），不是行号 —— 行号会漂，符号不会。
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "logger"):
            continue
        yield node


def arguments_naming(node: ast.Call, name: str) -> list[ast.expr]:
    """实参里（**含嵌套**）出现了 ``name`` 这个名字的那些实参。"""
    return [arg for arg in node.args
            if any(isinstance(sub, ast.Name) and sub.id == name
                   for sub in ast.walk(arg))]


class NextcloudTokenSiteStructureTest(unittest.TestCase):
    """每一处打 token 的日志都必须把它包进 ``redactable_id(self.name, token)``。"""

    def setUp(self):
        self.source = read_source(NEXTCLOUD_SOURCE)
        self.sites = [node for node in logger_calls(self.source)
                      if arguments_naming(node, "token")]

    def test_criterion_finds_every_site(self):
        """前提：判据找得到 10 处。判据坏了的话下面全是恒绿。"""
        self.assertEqual(len(self.sites), NEXTCLOUD_TOKEN_SITES,
                         "判据应当找到 %d 处，实际 %d 处 ⇒ 先怀疑判据，再怀疑代码"
                         % (NEXTCLOUD_TOKEN_SITES, len(self.sites)))

    def test_every_token_argument_is_wrapped(self):
        """给变量改名骗不过这条：判据看的是**实参的结构**。"""
        offending = []
        for node in self.sites:
            for arg in arguments_naming(node, "token"):
                if ast.unparse(arg) != "redactable_id(self.name, token)":
                    offending.append((node.lineno, ast.unparse(arg)))
        self.assertEqual(offending, [],
                         "这些实参仍在打裸 token：%r" % (offending,))

    def test_non_token_arguments_are_untouched(self):
        """``resp.status`` / ``code`` / ``room.cursor`` / ``_error_detail(...)`` 原样保留。"""
        rendered = {ast.unparse(arg) for node in self.sites for arg in node.args}
        for expected in ("resp.status", "code", "room.cursor",
                         "_error_detail(resp.data)", "reason"):
            self.assertIn(expected, rendered,
                          "实参 %s 不见了 —— 本次只许换 token 那一个" % expected)

    def test_room_cursor_is_deliberately_not_redacted(self):
        """``room.cursor`` 是消息 id，不是凭据 —— 脱敏它是**扩大范围**。"""
        cursor_sites = [node for node in logger_calls(self.source)
                        if any(ast.unparse(arg) == "room.cursor" for arg in node.args)]
        self.assertTrue(cursor_sites, "判据坏了：找不到传 room.cursor 的日志调用")
        for node in cursor_sites:
            self.assertEqual(
                [ast.unparse(arg) for arg in node.args],
                ["'nextcloud: 会话 %s 的 200 响应缺 X-Chat-Last-Given，游标保持 %d"
                 "（可能重复投递）'", "redactable_id(self.name, token)", "room.cursor"],
                "游标那个实参的形态被改了（游标刻意不脱敏）")

    def test_logger_call_count_is_unchanged(self):
        """防「顺手加了一行 / 删了一行」：调用数必须与 a6136e9 时一致。"""
        self.assertEqual(len(list(logger_calls(self.source))), 40)

    def test_changed_sites_use_percent_s_not_percent_r(self):
        """本次改的 9 处只能是 ``%s``（或 ``%d``）——不许顺手加成 ``%r``。

        ⚠️ **刻意只查这 9 处**：nextcloud 里另有 5 处**早于本次**就在用 ``%r``
        （``配置非法 %r`` / ``bad conversation_id %r`` / ``bad handle %r`` /
        ``capabilities … chat.max-length %r``）。把它们一并断言进来就是
        **扩大范围**（本次目标是"token 不再明文"，不是"nextcloud 不用 %r"），
        而那 5 处属于另一笔债，已在交付报告里列为「发现但刻意没改」。
        """
        offenders = []
        for node in self.sites:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "%r" in arg.value or "%a" in arg.value:
                        offenders.append((node.lineno, arg.value))
        self.assertEqual(offenders, [],
                         "这 9 处里有 %%r：%r" % (offenders,))


class TelegramCallbackLogStructureTest(unittest.TestCase):
    """``callback without chat context`` 那一行：记形状、不记内容。"""

    def setUp(self):
        self.source = read_source(TELEGRAM_SOURCE)
        self.tree = ast.parse(self.source)
        handler = next(node for node in ast.walk(self.tree)
                       if isinstance(node, ast.FunctionDef)
                       and node.name == "_handle_callback")
        self.warn_calls = [
            node for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.func.attr == "warning"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "callback without chat context" in str(node.args[0].value)
        ]

    def test_the_warning_is_found(self):
        """前提：判据找得到那条告警。"""
        self.assertEqual(len(self.warn_calls), 1,
                         "应当恰好找到一条，实际 %d ⇒ 先怀疑判据" % len(self.warn_calls))

    def test_it_does_not_pass_the_whole_payload(self):
        """⛔ ``cq`` 不许**原样**出现在实参里（那正是 ``%r`` 泄露的写法）。

        ⚠️ 判据必须区分「原样传 ``cq``」与「**只用它的键名**」——
        ``tuple(sorted(cq))`` 是刻意允许的：它取的是键，而键名不是用户数据。
        所以这里查的是"有没有一个实参**就是** ``cq``"，而不是"名字里含 cq"。
        §7.1 的反面：判据太宽会把正确实现判成错的，那也是判据坏了。
        """
        node = self.warn_calls[0]
        offenders = [ast.unparse(arg) for arg in node.args[1:]
                     if isinstance(arg, ast.Name) and arg.id == "cq"]
        self.assertEqual(offenders, [],
                         "仍把整个载荷原样当实参：%r" % (offenders,))

    def test_key_names_come_from_sorted_not_from_the_values(self):
        """✅ 记的是**键名**（``sorted`` ⇒ 元组），不是键对应的值。"""
        node = self.warn_calls[0]
        key_arg = next(arg for arg in node.args[1:] if "sorted" in ast.unparse(arg))
        self.assertIn("sorted", ast.unparse(key_arg))
        self.assertNotIn(".values(", ast.unparse(key_arg))
        self.assertNotIn(".items(", ast.unparse(key_arg))
        self.assertNotIn(".get(", ast.unparse(key_arg),
                         "键名那一半不许去取值")

    def test_it_logs_key_names(self):
        """✅ ``keys=`` 那一半必须在 —— 这是防「靠少记信息来修」的闸。"""
        node = self.warn_calls[0]
        rendered = " ".join(ast.unparse(arg) for arg in node.args)
        self.assertIn("keys=%s", rendered,
                      "形状信息（顶层键名）被删掉了 ⇒ 排障能力被换成了隐私")

    def test_it_logs_the_sender_id_through_redactable_id(self):
        node = self.warn_calls[0]
        redacted = [arg for arg in node.args[1:]
                    if "redactable_id" in ast.unparse(arg)]
        self.assertEqual(len(redacted), 1,
                         "应当恰好一处 redactable_id，实际 %d" % len(redacted))
        self.assertIn("self.name", ast.unparse(redacted[0]))

    def test_it_never_touches_user_facing_fields(self):
        """⛔ 载荷里那些带用户数据的字段一个都不许被**取值**。

        ⚠️ 判据查的是 **``Subscript`` / ``.get(...)`` 的键**，不是整段源码里
        出现过这个词 —— ``.get("id")`` 是允许的（那是唯一要记的 id），
        而 ``from_user`` 这个名字里含 ``user`` 也不构成违规。
        查词只会既漏又误报。
        """
        node = self.warn_calls[0]
        read_keys = set()
        for arg in node.args[1:]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Subscript):
                    try:
                        read_keys.add(ast.literal_eval(sub.slice))
                    except (ValueError, TypeError):
                        pass
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "get" and sub.args
                        and isinstance(sub.args[0], ast.Constant)):
                    read_keys.add(sub.args[0].value)
        for field in ("username", "first_name", "last_name", "text",
                      "data", "chat", "message"):
            self.assertNotIn(field, read_keys,
                             "实参去取了载荷字段 %r" % (field,))
        # 唯一被允许取的键就是 id
        self.assertIn("id", read_keys,
                      "from.id 是**要**记的（脱敏后）；一条都没取说明实现变了")

    def test_level_is_still_warning(self):
        self.assertEqual(self.warn_calls[0].func.attr, "warning")


class PlatformKeyLegalityTest(unittest.TestCase):
    """13 个已注册适配器的 ``name`` 全是合法平台键。

    这是 :data:`~opencode_bridge.adapters._redactable_ids.MISSING_ID` 的第二种成因
    （"平台键非法"）**结构上不可达**的保证 —— 否则日志里的 ``?`` 会同时意味着
    "平台没给 id" 和 "平台键非法"，一个符号两种含义是不可接受的歧义。
    """

    def test_every_registered_platform_name_is_legal(self):
        from opencode_bridge.adapters import registered_names
        from opencode_bridge.identity import InvalidConversationId, format_id

        names = sorted(registered_names())
        self.assertGreaterEqual(len(names), 13, "平台数不该变少：%r" % (names,))
        for name in names:
            try:
                format_id(name, "x")
            except InvalidConversationId as exc:
                self.fail("平台键 %r 非法：%s" % (name, exc))


if __name__ == "__main__":
    unittest.main()
