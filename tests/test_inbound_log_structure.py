"""**结构断言**（扫源码）：拒绝路径上的日志行不许再出现裸 id。

这是防回归的那一层 —— 改代码的人不会记得去读某一行日志，而这里会红。

## 判据形式

对每一处"拒绝一条入站消息路径上"的日志调用（怎么找出来的见
:func:`_rejection_log_calls`），**除格式串以外**的每一个实参必须满足下面
三者之一：

1. 字面量（``ast.Constant``）或 f-string（``ast.JoinedStr``）；
2. ``redactable_id(...)`` 的调用；
3. ``_drop_inbound`` 的 reason 形参（按**位置**认出，见 :data:`SAFE_ARGUMENT_NAMES`）
   或 :data:`SAFE_ARGUMENT_NAMES` 里的名字。

换句话说：**能落到日志里的值，要么是常量，要么一定经过
:func:`~opencode_bridge.adapters._redactable_ids.redactable_id`。**
判据**不需要**"哪些变量名是 id"的白名单 —— 它判的是**数据流**（有没有过那个
函数），所以给变量改名骗不过它（实测过：把 ``reason`` 改成 ``reason_text`` /
``why``，本文件仍然全绿）。

## 变异实验（判据必须真的能抓到回退）

``.tmp/drop_log_lane/mutation_experiment.py`` 跑完 3 个真变异 + 6 个对照：

* **真变异** ⇒ 全部**红**：① ``redactable_id(self.name, channel)`` 换回裸
  ``channel``；② telegram 闸门换回裸 ``chat.get("id")``；③ 往 ``reason`` 里塞
  ``f"…author={author}…"`。
  ⚠️ 第 ③ 条**曾经漏过**：允许 f-string 任意插值的那一版是绿的（``JoinedStr``
  无论插什么都是 ``JoinedStr``）—— 所以
  :meth:`RejectionLogStructureTests.test_drop_reason_only_quotes_declared_safe_values`
  不是"锦上添花的严格"，它是 :meth:`test_no_rejection_log_takes_a_bare_id` 的前提。
* **对照** ⇒ 全部**绿**：改注释、加注释块、改日志文案、给常量换字面量、
  给 ``reason`` 改名。

对照是必需的：只有"真变异红"而没有"对照绿"，无法区分"判据抓得住"与"判据恒红"。
"""

from __future__ import annotations

import ast
import io
import unittest

from tests.inbound_log_support import ADAPTERS_DIR, ALL_PLATFORMS

# ======================================================================
# 2. 结构断言（防回归的那一层）
# ======================================================================
#: 日志调用点的判定：``<something>.info(...)`` / ``logger.warning(...)`` / ...
_LOGGER_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
})

#: 闸门的**唯一**结构标记：13 个平台的闸门分支都写成
#: ``if not self.admits(p) and not self.answer_pairing_request(p, cid, text):``
#: —— 所以判"这是闸门拒绝"只需要认这一个符号，**不认任何平台特有的写法**
#: （telegram 走的是 ``self._allowed``，那是 ``admits`` 的委托；irc 用 ``target``；
#: qqbot/homassistant 传的是 ``cid`` …）。将来新增平台、换了 principal 名字，
#: 这一层照样覆盖得到。
_GATE_MARKER = "answer_pairing_request"

#: 允许**不经过** ``redactable_id`` 就进日志行的**名字**。只放一个，因为能靠
#: 结构解决的那一类（``reason``）不靠名字：
#:
#: * ``reason`` —— ``_drop_inbound`` 的 reason 形参，五个平台**全都是**
#:   ``self`` 之后的第 0 个形参。所以判据按**位置**认定（见
#:   :func:`_rejection_log_calls` 返回的 ``reason_param``），而不是按名字：
#:   把它改名**不该**把断言弄红（实测过），可它的安全性由
#:   :meth:`RejectionLogStructureTests.test_drop_reason_only_quotes_declared_safe_values`
#:   独立守（每个调用点传进来的东西只能是申报过的那几项）。
#: * ``is_bot`` —— discord 的 ``author.bot`` 布尔值，不是 id，且无法用位置认出来
#:   （它是 ``_drop_inbound`` 的最后一个形参，不是第 0 个）。
SAFE_ARGUMENT_NAMES = frozenset({"is_bot"})

#: ``reason`` 里**允许**被插值的值，以及各自"为什么它不是 id"。
#:
#: ⚠️ 这张表是**枚举**，不是模式 —— 它的作用是让"往 reason 里塞一个 id"变成一次
#: **红**（见真变异 4）。第一条允许 f-string 任意插值的版本已经被实测证伪：
#: ``f"系统消息 author={author} type={post_type!r}"`` 当时是**绿的**。
SAFE_REASON_NAMES = frozenset({
    "post_type",        # mattermost：Post.type，system_* 枚举（``""`` / ``system_join_channel``）
    "system_message",   # nextcloud：服务端生成的 systemMessage（"你已被移出…"），非用户正文
    "event_type",       # homeassistant：事件类型名（``state_changed`` / ``call_service``）
    "message_type",     # qqbot / nextcloud：平台签发的消息类型枚举（0 = 纯文本）
})
#: ``reason`` 里允许的**取值形式**：``<payload>.get('<字面量键>')``。
#: 目前只有 discord 的 ``data.get('type')`` 一处（消息类型枚举，int）。
SAFE_REASON_PAYLOAD_GET = frozenset({"data"})


def _is_logging_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOGGER_METHODS)


def _mentions_gate_marker(node: ast.AST) -> bool:
    """子树里有没有对 ``answer_pairing_request`` 的调用。"""
    return any(isinstance(inner, ast.Call)
               and isinstance(inner.func, ast.Attribute)
               and inner.func.attr == _GATE_MARKER
               for inner in ast.walk(node))


def _called_name(node: ast.Call) -> str:
    """取被调用者的名字（``redactable_id(...)`` → ``"redactable_id"``）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _rejection_log_calls(tree: ast.Module) -> list[tuple[int, ast.Call, str | None]]:
    """收集"拒绝一条入站消息"路径上的**全部**日志调用点。

    两条来源，覆盖两类路径：

    * **闸门分支** —— ``if`` 的判据里含 ``answer_pairing_request(...)``。
      取它的 ``body``（``orelse`` 是"已放行"那一侧，不该记丢弃日志）。
    * **``_drop_inbound``** —— 函数体里的日志。这是为了覆盖**非闸门**的丢弃理由
      （bot 自己发的 / 系统消息 / 空正文 / 格式非法 / 回环防护…）：它们与本次
      闸门翻转无关，但**同样打裸 id**，而陌生人一样能把它们刷满。

    两条都用**符号名**定位，不认行号 —— 行号会漂，符号不会。

    第三个返回值是**该调用点所在 ``_drop_inbound`` 的 reason 形参名**
    （闸门分支与不在 ``_drop_inbound`` 里的调用为 ``None``）：这样"这一处实参是
    ``reason``"是按**位置**判出来的（``self`` 之后的第 0 个形参），改名不会误报，
    而它引用的东西是否安全由 :func:`_is_safe_reason` 那条断言单独管。
    """
    found: list[tuple[int, ast.Call, str | None]] = []
    seen: set[int] = set()

    def remember(call: ast.Call, reason_param: str | None) -> None:
        if id(call) in seen:
            return
        seen.add(id(call))
        found.append((call.lineno, call, reason_param))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_drop_inbound":
            positional = list(node.args.posonlyargs) + list(node.args.args)
            if positional and positional[0].arg == "self":
                positional = positional[1:]
            reason_param = positional[0].arg if positional else None
            for inner in ast.walk(node):
                if _is_logging_call(inner):
                    remember(inner, reason_param)
        elif isinstance(node, ast.If) and _mentions_gate_marker(node.test):
            for statement in node.body:
                for inner in ast.walk(statement):
                    if _is_logging_call(inner):
                        remember(inner, None)
    return sorted(found, key=lambda found_item: found_item[0])


def _is_payload_get(node: ast.AST) -> bool:
    """``<payload>.get('<字面量键>')`` —— :data:`SAFE_REASON_PAYLOAD_GET` 里那一种。"""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and isinstance(node.func.value, ast.Name)):
        return False
    if node.func.value.id not in SAFE_REASON_PAYLOAD_GET:
        return False
    return (len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str))


def _is_safe_reason(reason: ast.AST) -> bool:
    """``reason`` 是否只由字面量与 :data:`SAFE_REASON_VALUES` 那几项拼成。"""
    if isinstance(reason, ast.Constant):
        return isinstance(reason.value, str)
    if not isinstance(reason, ast.JoinedStr):
        return False
    return all(
        (isinstance(part.value, ast.Name)
         and part.value.id in SAFE_REASON_NAMES) or _is_payload_get(part.value)
        for part in reason.values
        if isinstance(part, ast.FormattedValue)
    )


class RejectionLogStructureTests(unittest.TestCase):
    """扫 ``adapters/*.py``：拒绝路径上的日志行**不许**再出现裸 id 实参。

    ## 判据形式

    对每一处 :func:`_rejection_log_calls` 收集到的日志调用，
    **除格式串以外**的每一个实参必须满足下面三者之一：

    1. 字面量（``ast.Constant``）或 f-string（``ast.JoinedStr``）；
    2. ``redactable_id(...)`` 的调用；
    3. :data:`SAFE_ARGUMENT_NAMES` 里的名字（各自的理由写在那个常量上）。

    换句话说：**能落到日志里的值，要么是常量，要么一定经过
    :func:`~opencode_bridge.adapters._redactable_ids.redactable_id`。**
    这条判据**不需要**"哪些变量名是 id"的白名单 —— 它判的是**数据流**
    （有没有过那个函数），所以给变量改名骗不过它。

    ## 变异实验（判据必须真的能抓到回退）

    * **真变异**（把 ``redactable_id(self.name, channel)`` 换回裸 ``channel``）
      ⇒ 实参变成 ``ast.Name`` 且不在 :data:`SAFE_ARGUMENT_NAMES` 里 ⇒ **红**。
    * **对照变异**（改一个字面量、加一行注释、改一个与日志无关的常量）
      ⇒ 判据量的是同一批表达式，仍然成立 ⇒ **绿**。
      这条对照是必需的：只有"真变异红"而没有"对照绿"，无法区分"判据抓得住"
      与"判据恒红"。
    """

    def _adapter_sources(self):
        for path in sorted(ADAPTERS_DIR.glob("*.py")):
            if path.name in {"__init__.py", "base.py", "_redactable_ids.py"}:
                continue
            source = io.open(path, encoding="utf-8").read()
            yield path, source, ast.parse(source, str(path))

    def test_the_harness_actually_found_something_to_judge(self):
        """**空集 ≠ 不存在**（AGENTS.md §7.1）。

        判据量的是一个"收集到的集合"；集合为空时它会**恒绿**，而恒绿看起来
        像"没问题"。所以先证明它找到了东西 —— 而且找到了**每一处**该找的地方。
        """
        per_platform: dict[str, int] = {}
        for path, _source, tree in self._adapter_sources():
            calls = _rejection_log_calls(tree)
            if calls:
                per_platform[path.stem] = len(calls)
        self.assertEqual(
            sorted(per_platform), sorted(ALL_PLATFORMS),
            "拒绝路径上的日志调用点集合变了：要么某个平台被改得不再记丢弃日志，"
            "要么新增了平台（补一条逐平台测试）。实际找到：%r" % (per_platform,),
        )
        for platform, count in sorted(per_platform.items()):
            with self.subTest(platform=platform):
                self.assertGreaterEqual(
                    count, 1, "%s 找到了 0 处 —— 又是空集被当成了不存在" % platform,
                )

    def test_one_log_line_serves_every_drop_reason_of_those_five_platforms(self):
        """那条"改 ``_drop_inbound`` 内部的日志行（一次修好所有理由）"的取舍，
        由这条断言钉住：5 个平台各有**多个**丢弃理由，而它们**共用同一行日志**。

        若哪天有人为了"更好读"把某一支改成自己打一行日志，这里会红 ——
        那时必须记得新打的那一行也要走 :func:`redactable_id`。
        """
        shared_line_platforms = ("discord", "mattermost", "nextcloud", "qqbot", "homeassistant")
        for path, _source, tree in self._adapter_sources():
            if path.stem not in shared_line_platforms:
                continue
            reasons = [
                node for node in ast.walk(tree)
                if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_drop_inbound"
                    and isinstance(node.func.value, ast.Name)      # self._drop_inbound
                    and node.func.value.id == "self")
            ]
            log_lines = _rejection_log_calls(tree)
            with self.subTest(platform=path.stem):
                self.assertGreaterEqual(
                    len(reasons), 2,
                    "%s 只有一个 _drop_inbound 调用点 —— 那就没有'一次修好所有"
                    "理由'这回事，本测试的前提变了" % path.stem,
                )
                self.assertEqual(
                    len(log_lines), 1,
                    "%s 有 %d 处拒绝日志调用，而 _drop_inbound 只有一行 —— 多出来"
                    "的那些必须自己经过 redactable_id（结构断言会管），但"
                    "'共用一行'这个前提已经不成立了"
                    % (path.stem, len(log_lines)),
                )

    def test_no_rejection_log_takes_a_bare_id(self):
        offenders: list[str] = []
        for path, _source, tree in self._adapter_sources():
            for lineno, call, reason_param in _rejection_log_calls(tree):
                # args[0] 是格式串；args[1:] 才是会渲染进去的实参。
                for argument in call.args[1:]:
                    if isinstance(argument, (ast.Constant, ast.JoinedStr)):
                        continue
                    if isinstance(argument, ast.Call) \
                            and _called_name(argument) == "redactable_id":
                        continue
                    if isinstance(argument, ast.Name) and (
                            argument.id in SAFE_ARGUMENT_NAMES
                            or argument.id == reason_param):
                        continue
                    offenders.append(
                        "%s:%d  %s  →  实参 %s"
                        % (path.name, lineno,
                           ast.unparse(call).splitlines()[0][:90],
                           ast.unparse(argument))
                    )
        self.assertEqual(
            offenders, [],
            "拒绝路径上又出现裸 id 实参了 —— 经 redactable_id() 补上前缀，"
            "C2 才认得（它只脱敏 platform:local_id，裸值按形状放行）。"
            "逐处：\n  " + "\n  ".join(offenders),
        )

    def test_drop_reason_only_quotes_declared_safe_values(self):
        """``_drop_inbound`` 的第 0 个实参（``reason``）只许由字面量与
        :data:`SAFE_REASON_NAMES` 里的值拼成。

        这是 :data:`SAFE_ARGUMENT_NAMES` 里 ``reason`` 那一条**成立的前提**。
        ⚠️ 允许 f-string **任意**插值的版本已被实测证伪：把
        ``f"系统消息 type={post_type!r}"`` 改成
        ``f"系统消息 author={author} type={post_type!r}"`` 之后
        :meth:`test_no_rejection_log_takes_a_bare_id` **仍然是绿的** ——
        ``ast.JoinedStr`` 无论插什么都是 ``JoinedStr``。所以这一条不是
        "锦上添花的严格"，它是那条断言真正的前提。
        """
        offenders: list[str] = []
        for path, _source, tree in self._adapter_sources():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_drop_inbound"):
                    continue
                if not node.args:
                    continue
                reason = node.args[0]
                if _is_safe_reason(reason):
                    continue
                offenders.append("%s:%d  reason=%s"
                                 % (path.name, node.lineno, ast.unparse(reason)))
        self.assertEqual(
            offenders, [],
            "`_drop_inbound` 的 reason 里出现了未申报的值 —— 它可能是某个 id，"
            "而结构断言看不见 reason 的内部。逐处：\n  " + "\n  ".join(offenders),
        )

    def test_the_readme_troubleshooting_string_still_exists_in_the_source(self):
        """约束：**日志文案保持可 grep**。

        ``README.md`` 的排障表按字符串引用了
        ``dropped message from non-whitelisted chat``，用户照着它搜日志。
        这次只换**实参**，不换文案 —— 这一条把那个契约钉住，免得下次有人
        "顺手改得更好读"时把用户的排障路径一起改掉。
        """
        needle = "dropped message from non-whitelisted chat"
        telegram_source = io.open(ADAPTERS_DIR / "telegram.py", encoding="utf-8").read()
        readme = io.open(ADAPTERS_DIR.parent.parent / "README.md", encoding="utf-8").read()
        self.assertIn(needle, telegram_source, "telegram 的文案变了")
        self.assertIn(needle, readme, "README 的排障表引用的文案与代码不一致了")
