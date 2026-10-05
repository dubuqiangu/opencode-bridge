"""断言：配置读取器**永远不会被喂进凭据形状的键名**。

背景（`tasks.md`「第二类已查清」一节）
------------------------------------
各适配器有一批「类型化配置读取器」：值非法时把**原始配置值** ``%r`` 进日志。
实测（`exp-54` + 独立复核）今天流入的全是标量键、凭据键全部在读取器之外 ——
**但那是「约定」，不是「强制」**：没有人拦得住将来有人写
``self._config_int_value("bot_token", …)``，而那样 ``%r`` 就会明文打凭据，
**且当时不会有任何测试变红**。

本断言做在**调用侧**，因为这些读取器有四种签名形态，其中形态 C
（``_truthy`` / ``_coerce_turns``）拿到的已经是被取出来的值、
**键名在它作用域里根本不存在** —— 在函数内断言键名做不到（§8：不自授权改跨适配器的
公共读取路径），而在调用侧四个形态全部覆盖。

判据是**形状**而不是文案
------------------------
⚠️ 前两版都栽在文案匹配上：

- **过宽版**：收集「模块里所有被调用的名字」⇒ 348 个「读取器」（含 ``Lock`` /
  ``Thread`` / ``ValueError``）⇒ 把 ``self.config.get("auth_token")`` 里的 ``get``
  当成了读取器 ⇒ 假阳性。
- **过窄版**：只匹配中文文案（``配置非法|非法|…``）⇒ **漏掉英文的
  ``irc: bad port %r; falling back to default``（``_resolve_port``）**
  与「不是**合法**端口」（``_port``，文案是 ``合法`` 而我匹配 ``非法``）。

⇒ 现在改成两条**与语言无关**的形状判据，取并集：

**A.** 函数自己身上：某个 ``logger.…(…, "%r", …)`` 的实参里有 ``config.get(...)``
   —— 覆盖 ``nextcloud._config_int_value(key, …)``（键名是参数、取值在体内）。
**B.** 函数的调用点：``F(…, self.config.get("x"), …)`` ⇒ ``F`` 是读取器
   —— 覆盖 ``homeassistant._truthy(config.get("accept_all"), False)``。

两条都不依赖任何文案，新增读取器自动被覆盖。
"""

import ast
import io
import re
import unittest
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"

#: 只有形状可疑的**键名**才要拦。取值（``raw``）本身不在本断言范围内 ——
#: 本文件断言的是「**键名**不会是指向凭据的那个字符串」。
CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:token|password|passwd|secret|cookie|credential|auth|apikey|api_key)(?:$|_)",
    re.IGNORECASE,
)

SENSITIVE_READERS = ("debug", "info", "warning", "error", "exception")


def _parent_map(tree: ast.AST) -> dict:
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _enclosing_function(node: ast.AST, parent: dict):
    cur = node
    while cur in parent:
        cur = parent[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return None


def _is_logger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SENSITIVE_READERS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    )


def _reads_config(node: ast.AST) -> bool:
    """表达式里是否出现 ``config.get(...)``（含 ``self.config.get`` / 局部 ``config``）。"""
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
            continue
        if inner.func.attr != "get":
            continue
        base = inner.func.value
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
        if name == "config":
            return True
    return False


def _called_name(node: ast.Call):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _string_literals(node: ast.AST) -> list:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def reader_names_in(src: str) -> set:
    """从源码里推导「读取器」的名字集合。

    **定义：函数体里有一条用 ``%r`` 记日志的语句。** 就这一条 —— 与语言无关、
    与文案无关、也不看 ``config.get`` 出现在哪一层。

    ⚠️ 前三版都栽在「判据选错了轴」上：

    - **过宽**：收集模块里所有被调用的名字 ⇒ 348 个（含 ``Lock``/``Thread``）。
    - **过窄**：只匹配中文文案 ⇒ **漏掉英文的 ``irc: bad port %r``（``_resolve_port``）**
      与「不是**合法**端口」（``_port``，文案是 ``合法`` 而我匹配 ``非法``）。
    - **形状但轴选错**：把「表达式里含 ``config.get(...)``」当判据 ⇒ 因为
      ``str(self.config.get("auth_token") or "").strip()`` 的**外层** ``str``/``strip``
      也满足它，名单涨到 45 个并产生 52 处假阳性（``irc.py:363 server_password``
      之类**正当的凭据读取**被误判）。

    ⇒ 「会用 ``%r`` 记日志」既覆盖了 exp-54 实证过的全部 11 个读取器，
    又天然排除了 ``get`` / ``str`` / ``_first`` 这类**并不记日志**的辅助。
    名单偏大是**偏严**（不是偏松），方向安全。
    """
    tree = ast.parse(src)
    parent = _parent_map(tree)
    readers = set()
    for node in ast.walk(tree):
        if not _is_logger_call(node) or not node.args:
            continue
        head = node.args[0]
        if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
            continue
        if "%r" not in head.value:
            continue
        owner = _enclosing_function(node, parent)
        if owner:
            readers.add(owner)
    return readers


def _all_sources() -> dict:
    return {
        path: io.open(path, encoding="utf-8").read()
        for path in sorted(ADAPTERS_DIR.glob("*.py"))
    }


def violations() -> list:
    """扫全部适配器，返回 [(文件, 行号, 读取器名, 键名)]。"""
    sources = _all_sources()
    readers = set()
    for src in sources.values():
        readers |= reader_names_in(src)

    found = []
    for path, src in sources.items():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name is None or name not in readers:
                continue
            for literal in _string_literals(node):
                if CREDENTIAL_KEY.search(literal):
                    found.append((path.name, node.lineno, name, literal))
    return found


class ConfigReaderCredentialKeyTests(unittest.TestCase):
    """§7.1「空集 ≠ 不存在」：先证明判据找到了东西，再谈它有没有命中。"""

    def test_the_criterion_finds_readers_without_blowing_up(self):
        """前提断言：名单**非空、且不泛化** —— 两个方向都要卡。"""
        readers = set()
        for src in _all_sources().values():
            readers |= reader_names_in(src)
        self.assertGreater(
            len(readers), 5, "名单异常地小 ⇒ 判据坏了（空集不等于不存在）"
        )
        self.assertLess(
            len(readers), 40, "名单异常地大 ⇒ 判据过宽（第一版的错法），请收窄"
        )

    def test_it_covers_the_readers_we_know_about(self):
        """反向前提：``exp-54`` 实证过的读取器**必须**在名单里。

        ⚠️ 这条正是为了钉住「过窄」那个坑：文案匹配的版本漏掉了英文的
        ``_resolve_port``（``irc: bad port %r``）与 ``_port``（文案是「合法」）。
        """
        readers = set()
        for src in _all_sources().values():
            readers |= reader_names_in(src)
        for expected in (
            "_config_int_value",     # nextcloud，形态 A
            "_config_float",         # nextcloud
            "_security",             # email，形态 A
            "_port",                 # email
            "_config_verify_tls",    # email / mattermost
            "_config_intents",       # discord / qqbot
            "_config_shard",         # qqbot
            "_truthy",               # homeassistant，形态 C
            "_coerce_positive",      # a2a，形态 D
            "_coerce_turns",         # a2a，形态 C
            "_resolve_port",         # irc ← 文案是英文，最容易漏
        ):
            self.assertIn(
                expected, readers, "名单里少了 " + expected + " ⇒ 归属规则收窄过头了"
            )

    def test_no_credential_shaped_key_reaches_a_config_reader(self):
        """主断言：凭据形状的键名不得作为读取器的实参出现。"""
        found = violations()
        detail = "\n".join(
            "  %s:%s  %s(%r)" % (f, ln, name, key) for f, ln, name, key in found
        )
        self.assertEqual(
            found, [], "凭据形状的键名被喂进了配置读取器（会被 %%r 明文打出来）：\n" + detail
        )


if __name__ == "__main__":
    unittest.main()
