"""共用夹具、码位清单与两个扫描器：给「会话 id 紧跟中文尾注」这三份测试用。

**为什么单独一个模块**：三份测试按职责分工（见
:mod:`tests.test_redaction_cjk_tail` 修好的形态 /
:mod:`tests.test_redaction_cjk_exclusion_set` 排除集的形状与爆炸半径 /
:mod:`tests.test_redaction_cjk_id_integrity` 真 id 的完整性），而它们共用同一批
常量、同一个排除类探针、同一套 AST 扫描器。放在一起是「改一处判据要碰三个测试
文件」；塞进其中任一份里就是「改判据要碰两个文件」。

⚠️ 文件名**不**以 ``test_`` 开头，所以 pytest 不会收集它 —— 里面没有用例，
只有常量、替身与扫描器。

这个缺陷本身
============

:data:`~opencode_bridge.redaction._CONVERSATION_ID_PATTERN` 的 local-id 段是
贪婪的「非空白 / 非半角标点」类，而**全角标点与 CJK 字符都不在排除集里**
⇒ 紧跟 ``platform:local_id`` 之后的中文被当成 id 的一部分，替换时**一起**被摘要：

===========================  ==========================  ==================
输入                          修好后                      修好前
===========================  ==========================  ==================
``telegram:12345（判据）``    ``…conv#xxxx（判据）``       ``…conv#xxxx``
``telegram:12345，判据``      ``…conv#xxxx，判据``         ``…conv#xxxx``
``a2a:local:127.0.0.1（来源 peer）``  ``…conv#xxxx（来源 peer）``  ``…conv#xxxx peer）``
===========================  ==========================  ==================

第三行是**半吞**：一半被摘要、一半以明文留在日志里，读起来像乱码 ——
比整条尾注消失更难查。

⚠️ **探针必须喂「裸形态」**
=========================

被吞的是 :func:`~opencode_bridge.adapters._redactable_ids.redactable_id` 打进
日志的那一层 ``platform:local_id``；规则对**已脱敏**的 ``conv#<hex>`` 形态
**不匹配**（``(?![a-z]+#)`` 是幂等性的承重项）。
⇒ 喂 ``conv#`` 形态的探针会拿到 0 命中，从而「否证」一个真缺陷。
本模块所有用例都用**裸形态**。

「尾注还在」为什么不够
======================

「尾注在」与「id 没被切碎」是**两个独立的失败模式**：

* ⛔ 尾注消失 ⇒ 诊断信息从生产日志里消失（本次修的）；
* ⛔ id 被切碎 ⇒ ``conv#`` 对不上 :attr:`~opencode_bridge.hooks.Inbound.conversation_id`，
  排障时「日志说的」与「会话表说的」两边对不上号 —— **比吞尾注更糟**。

所以 :func:`expected_tail_note_output` 给的是**逐字节**的等式，
而「只断言尾注还在」的那版，在「排除所有非 ASCII」这个过度激进的修法下
**照样全绿**。
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

import opencode_bridge
from opencode_bridge import redaction
from opencode_bridge.redaction import Redactor
from tests.inbound_log_support import ALL_PLATFORMS
from tests.log_redaction_support import FIXED_REDACTION_KEY

__all__ = [
    "ALL_PLATFORMS",
    "ASCII_TAIL_NOTE_CASES",
    "BASELINE_LOCAL_ID_PATTERN",
    "CJK_ID_CASES",
    "EXPECTED_EXCLUDED_RANGES",
    "EXPECTED_KEPT_RANGES",
    "GENUINE_CJK_ID_CASES",
    "PACKAGE_DIR",
    "PLATFORM_SHAPES",
    "PROBE_LOCAL_ID",
    "TAIL_NOTE_CASES",
    "excluded_code_points",
    "expected_tail_note_output",
    "find_logger_calls_with_prefixed_id_arguments",
    "is_excluded_by",
    "is_excluded_by_rule",
    "make_redactor",
]

PACKAGE_DIR = pathlib.Path(opencode_bridge.__file__).parent

#: 探测用的平台前缀 local-id —— **纯 ASCII**，于是「模板里那些全角标点是不是
#: 全都留下来了」可以逐字符判定，不受 id 自身内容干扰。
PROBE_LOCAL_ID = "probe123id"


def make_redactor() -> Redactor:
    return Redactor(key=FIXED_REDACTION_KEY)


def expected_tail_note_output(redactor: Redactor, platform: str,
                              local_id: str, tail_note: str) -> str:
    """``scrub`` 在这条输入上**必须**产出的字符串。

    刻意写成「从 :class:`Redactor` 现算摘要」而不是抄一段字面量：摘要是
    带进程内随机密钥的 HMAC，抄不出来；而**期望值的形状**（``conv#<6>-<6>``）
    由 :func:`assert_digest_shape` 单独钉住。
    """
    digest = redactor.fingerprint("conv", local_id)
    return platform + ":" + digest + tail_note


def assert_digest_shape(test_case: unittest.TestCase, digest: str) -> None:
    """摘要必须真的是 ``<6 位>-<6 位>`` —— 防「期望值算错」。

    ⛔ 别省这条：``expected_tail_note_output`` 是拿被测类算的，若它算错，
    上面所有「逐字节等式」会一起红成一片看不懂的东西。
    """
    test_case.assertRegex(digest, r"^conv#[0-9a-f]{6}-[0-9a-f]{6}$")


# ----------------------------------------------------------------------
# 排除集的码位清单
# ----------------------------------------------------------------------
#: 这次改动**新排除**的码位区间，一处不多一处不少。
#:
#: ⚠️ 这张表问的是**增量**，不是绝对值 —— 改动前就被 ``\s`` / 半角标点排掉的
#: 空白字符（``U+2000``-``U+200A``、``U+2028``/``U+2029``、``U+202F``、
#: ``U+205F``、``U+3000``…）不在表里，因为它们不是本次引入的。
#: 改动这张表必须同时改 :data:`~opencode_bridge.redaction._CJK_PUNCTUATION_RANGES`，
#: 并想清楚「多收一个会切碎哪一类真 id、少收一个会漏掉哪一类尾注」。
EXPECTED_EXCLUDED_RANGES = (
    (0x2018, 0x2019),   # ‘ ’
    (0x201C, 0x201D),   # “ ”
    (0x2010, 0x2014),   # 连字符 / – —
    (0x2026, 0x2026),   # …
    (0x3001, 0x303F),   # CJK Symbols and Punctuation（U+3000 是空白，\s 早已排除）
    (0xFF01, 0xFF0F),   # 全角 ！＂＃＄％＆＇（）＊＋，－．／
    (0xFF1A, 0xFF20),   # 全角 ：；＜＝＞？＠
    (0xFF3B, 0xFF40),   # 全角 ［＼］＾＿｀
    (0xFF5B, 0xFF65),   # 全角 ｛｜｝～ ＋ 半角句点/片假名
)

#: **绝不能**进排除集的区间 —— 全角数字与全角字母是**文字**，
#: 与「保留 CJK 文字」同一条原则。它们落在上面那些区间的缝隙里，
#: 守着那些缝隙的就是这条清单。
EXPECTED_KEPT_RANGES = (
    (0xFF10, 0xFF19),   # ０-９
    (0xFF21, 0xFF3A),   # Ａ-Ｚ
    (0xFF41, 0xFF5A),   # ａ-ｚ
    (0x4E00, 0x9FFF),   # CJK 统一表意文字（基本区）
    (0x3040, 0x30FF),   # 假名
)

#: 扫「哪些码位进了排除集」时的范围：通用标点 + CJK 标点 + 全角区。
CODE_POINT_SCAN_RANGE = range(0x2000, 0xFF66)

#: **改动前**的 local-id 排除类，原样抄一份当**对照**。
#:
#: 为什么要在测试里留一份旧类：判据要问的是「这次改动**多**排除了哪些码位」，
#: 而不是「现在排除了哪些码位」—— 后者会把改动前就排除的空白字符也算成
#: 「多排除的」，于是这条断言在**改动之前**就是红的（§7.1：判据必须先在
#: 改动前跑过）。
#: ⛔ 它只是**对照**，不是第二份实现：期望值仍然由
#: :data:`EXPECTED_EXCLUDED_RANGES` 给出。
BASELINE_LOCAL_ID_PATTERN = re.compile(r"telegram:aa([^\s,;)\]\}\"'<>&]+)")


def excluded_code_points() -> set:
    """把排除集**清单**展开成码位集合。

    ⛔ 不是去读正则源码 —— 那是「按实现写判据」，实现错了断言会跟着错。
    这里问的是**行为**：拿一个只由单个候选字符构成的输入，看它还能不能被匹配上。
    """
    excluded = set()
    for low, high in EXPECTED_EXCLUDED_RANGES:
        excluded.update(range(low, high + 1))
    return excluded


def is_excluded_by(pattern: "re.Pattern[str]", character: str) -> bool:
    """这个字符落进 local-id 段的排除集了吗（**按行为**判定，不读正则源码）。

    ⚠️ 取的是 ``group(0)`` 再按第一个冒号切，**不是** ``group(1)``：
    :data:`~opencode_bridge.redaction._CONVERSATION_ID_PATTERN` 的 ``group(1)``
    是**平台段**（``telegram``），``group(2)`` 才是 local-id；写死组号的话这个
    探针会把「汉字不在 ``telegram`` 里」读成「汉字被排除了」——
    **恒真的探针比没有探针更危险**，而本函数**真的这样坏过一次**：
    那次「全角标点都被排除」那条断言照样绿（它只断言 ``True``）。
    ⇒ 它的非空洞性由 ``test_the_exclusion_probe_is_not_vacuous`` 钉住。
    """
    matched = pattern.search("telegram:aa" + character + "bb")
    if matched is None:
        return True                     # 连 `telegram:aa` 都没匹配上 ⇒ 字符是分隔符
    return character not in matched.group(0).split(":", 1)[1]


def is_excluded_by_rule(character: str) -> bool:
    """按**当前那条规则**判定一个字符是不是分隔符。"""
    return is_excluded_by(redaction._CONVERSATION_ID_PATTERN, character)


# ----------------------------------------------------------------------
# 用例表
# ----------------------------------------------------------------------
#: 修好的形态：``(输入, 平台, local_id, 尾注)``。
TAIL_NOTE_CASES = (
    ("telegram:12345（判据）", "telegram", "12345", "（判据）"),
    ("telegram:12345，判据", "telegram", "12345", "，判据"),
    ("telegram:12345。判据", "telegram", "12345", "。判据"),
    ("telegram:12345：判据", "telegram", "12345", "：判据"),
    ("telegram:12345；判据", "telegram", "12345", "；判据"),
    ("telegram:12345、判据", "telegram", "12345", "、判据"),
    ("telegram:12345？判据", "telegram", "12345", "？判据"),
    ("telegram:12345！判据", "telegram", "12345", "！判据"),
    ("telegram:12345…判据", "telegram", "12345", "…判据"),
    ("telegram:12345—判据", "telegram", "12345", "—判据"),
    ("telegram:12345「判据」", "telegram", "12345", "「判据」"),
    ("telegram:12345『判据』", "telegram", "12345", "『判据』"),
    ("telegram:12345《判据》", "telegram", "12345", "《判据》"),
    ("telegram:12345【判据】", "telegram", "12345", "【判据】"),
    ("telegram:12345～判据", "telegram", "12345", "～判据"),
    # ⚠️ 半吞那条：本地段**自带冒号**（a2a 未鉴权形态），修好后必须是
    # **完整尾注**而不是「一半摘要一半明文碎片」。
    ("a2a:local:127.0.0.1（来源 peer）", "a2a", "local:127.0.0.1", "（来源 peer）"),
    # 本地段自带**半角**标点是承重的（homeassistant / matrix / irc）
    ("homeassistant:light.kitchen（不在清单）", "homeassistant", "light.kitchen", "（不在清单）"),
    ("matrix:!abcdef:matrix.example.org（已退群）", "matrix", "!abcdef:matrix.example.org", "（已退群）"),
    ("irc:#中文频道（入站）", "irc", "#中文频道", "（入站）"),
)

#: ⛔ 反向闸：真的含 CJK 的 local-id —— ``(输入, 平台, local_id, 尾注)``。
#:
#: ``ntfy`` 的 ``topic`` 是**用户自取的名字**（``ntfy.py`` 类 docstring 第 (b) 条
#: 「用户自己命名的 topic」），代码里按 ``urllib.parse.quote(topic, safe="")``
#: 编码 —— 作者预期它可以含 URL 不安全字符。
GENUINE_CJK_ID_CASES = (
    ("ntfy:我的话题（备注）", "ntfy", "我的话题", "（备注）"),
    ("ntfy:我的话题，第二个话题", "ntfy", "我的话题", "，第二个话题"),
    ("ntfy:我的话题。", "ntfy", "我的话题", "。"),
    ("ntfy:我的话题：主线", "ntfy", "我的话题", "：主线"),
    ("a2a:我的对端（主）", "a2a", "我的对端", "（主）"),
    ("irc:#中文频道（入站）", "irc", "#中文频道", "（入站）"),
    ("irc:张三（私聊）", "irc", "张三", "（私聊）"),
    ("twitch:中文频道名（掉线）", "twitch", "中文频道名", "（掉线）"),
    ("homeassistant:light.厨房灯（不在清单）", "homeassistant", "light.厨房灯", "（不在清单）"),
    ("email:张三@example.com（不在白名单）", "email", "张三@example.com", "（不在白名单）"),
    ("qqbot:group:中文openid（未授权）", "qqbot", "group:中文openid", "（未授权）"),
)

#: 半角形态**本来**就被排除集挡住了 ⇒ 这次改动不许动它们。
#: ``(输入, local_id, 尾注)``。
ASCII_TAIL_NOTE_CASES = (
    ("telegram:12345 (判据)", "12345", " (判据)"),
    ("telegram:12345 判据", "12345", " 判据"),
    ("telegram:12345,判据", "12345", ",判据"),
    ("telegram:12345;判据", "12345", ";判据"),
    ("telegram:12345]判据", "12345", "]判据"),
    ("telegram:12345}判据", "12345", "}判据"),
    ("telegram:12345<判据", "12345", "<判据"),
    ("telegram:12345&判据", "12345", "&判据"),
)

#: 13 个**已落地**适配器的 local-id 真实形状（清单见 ``_redactable_ids.py`` 的
#: docstring「13 个平台的 id 形状」一段）：``(输入, 平台, local_id)``。
PLATFORM_SHAPES = (
    ("telegram:123456789", "telegram", "123456789"),
    ("telegram:-1001234567890", "telegram", "-1001234567890"),
    ("slack:C01ABCDEFGH", "slack", "C01ABCDEFGH"),
    ("discord:123456789012345678", "discord", "123456789012345678"),
    ("matrix:!abcdef:matrix.example.org", "matrix", "!abcdef:matrix.example.org"),
    ("mattermost:abcdefghijklmnopqrstuvwx", "mattermost", "abcdefghijklmnopqrstuvwx"),
    ("irc:#chan", "irc", "#chan"),
    ("irc:alice", "irc", "alice"),
    ("twitch:somechannel", "twitch", "somechannel"),
    ("nextcloud:tok3ncl0ud", "nextcloud", "tok3ncl0ud"),
    ("ntfy:my-topic", "ntfy", "my-topic"),
    ("email:bob@example.com", "email", "bob@example.com"),
    ("a2a:local:127.0.0.1", "a2a", "local:127.0.0.1"),
    ("qqbot:group:B2C3xxxx", "qqbot", "group:B2C3xxxx"),
    ("homeassistant:light.kitchen", "homeassistant", "light.kitchen"),
)


# ----------------------------------------------------------------------
# 仓库扫描器：实参是平台前缀 id 的日志调用点
# ----------------------------------------------------------------------
_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)
_ID_FACTORY_NAMES = frozenset({"redactable_id", "format_id"})
_PLACEHOLDER = re.compile(r"%(?:%|[-+ #0]*[0-9*]*(?:\.[0-9*]+)?[hlL]?)([srd])")


def _is_id_factory_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    return name in _ID_FACTORY_NAMES


def _id_producing_variable_names(tree: ast.AST) -> set:
    """本模块里被赋成 ``redactable_id(...)`` / ``format_id(...)`` 的变量名。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_id_factory_call(node.value):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return names


def find_logger_calls_with_prefixed_id_arguments() -> list:
    """``(相对路径, 行号, 模板, 实参下标, 紧随其后的字符)``。

    判据分两步，且**都在 AST 上做**（不是对渲染结果做正则）：

    1. 模板里的 ``%s`` 占位符按位置切分，得到每个占位符**紧随其后的那个字符**；
    2. 对应位置的实参是 ``redactable_id(...)`` / ``format_id(...)`` 的调用，
       或本模块里被赋成那种调用的变量。

    ⚠️ **为什么不用「占位符紧跟非空白」当判据**：那是模板里**任何一个**占位符的
    性质，与那个占位符是不是 id 无关。实测 ``nextcloud.py`` 的
    ``"会话 %s 的 200 响应缺 X-Chat-Last-Given，游标保持 %d（可能重复投递）"``
    里紧跟 ``（`` 的是 ``%d``（游标），而 id 那个 ``%s`` 后面是**空格** ——
    用前一个判据会把这条报成「在被吞」，而它的尾注其实好好地在那里。
    """
    found = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        id_names = _id_producing_variable_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LOG_LEVELS:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            template = node.args[0].value
            if not isinstance(template, str) or "%" not in template:
                continue
            positional = node.args[1:]
            for index, match in enumerate(_PLACEHOLDER.finditer(template)):
                if match.group(1) != "s" or index >= len(positional):
                    continue
                argument = positional[index]
                if not (_is_id_factory_call(argument)
                        or (isinstance(argument, ast.Name) and argument.id in id_names)):
                    continue
                found.append((
                    path.relative_to(PACKAGE_DIR.parent).as_posix(),
                    node.lineno,
                    template,
                    index,
                    template[match.end():match.end() + 1],
                ))
    return found
