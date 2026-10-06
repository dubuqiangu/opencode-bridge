"""「bridge 目录必须被隔离」的**共享件** —— AST 扫描工具与契约常量。

本模块**不以 ``test_`` 开头**，所以 unittest 的 discover **不会收集它**（它没有用例），
用法与拆分理由同 :mod:`tests.nick_trap_support`。

## 归属

从 :mod:`tests.test_artifact_isolation_contract` 里拆出来的**纯扫描逻辑**
（纯结构调整，判据逐字未变）—— 那个文件连同它的用例原本会超 §5 的 400 物理行。

## 判据为什么必须是结构而不是文本

本模块盯的那几个记号（``_bridge_dir`` / ``OPENCODE_BRIDGE_CONFIG``）在 docstring
与注释里**到处都是** —— :mod:`tests.test_platform_health` 与
:mod:`tests.test_bridge_refusal_probe` 的类 docstring 就在介绍它们。按文本匹配
会让判据**恒真**（永远"检测到隔离"）。⇒ 全部走 AST。

两条「结构上有隔离、实际没隔离」的同型洞（都已修，**实测**过的）
=========================================================

判据只问「有没有隔离动作」的话，下面两种写法会**恒绿地**通过，而它们都照样把文件
写进仓库根 —— 两者都是「动作在、值仍指向仓库根」：

1. ``mock.patch.object(cli, "_bridge_dir", lambda: os.getcwd())`` ⇒ :func:`patched_bridge_dir`
2. ``os.environ["OPENCODE_BRIDGE_CONFIG"] = os.path.join(仓库根, "config.json")``
   ⇒ :func:`bridge_config_env_pin`（:func:`opencode_bridge.__main__._bridge_dir`
   在那个变量指向**存在的文件**时返回的正是那个文件所在的目录）

⇒ 值的判据只有一份：:func:`derived_from_repository_root`（⛔ 不许再写第二份）。

另外三条「判据本身被绕过」的洞（也都已修，**实测**过的）
=========================================================

3. **判据的结论由源码顺序决定** ⇒ :func:`patched_bridge_dir`。原先命中第一个 pin 就
   ``return``，于是「先钉好、稍后再改回 cwd」那个形状**不报**，把两个顺序对调才报。
4. **判据③整个挂在「别名恒为 ``cli``」上，而那条约定只活在注释里** ——
   :func:`is_cli_entry_point` / :func:`cli_module_import_alias_violations`。
5. **4 那道护栏自己只覆盖两条 import 语句的写法，而它的 docstring 却宣称「机械强制」**
   ⇒ :func:`cli_module_binding_escape_violations`。原先它只看 ``ast.Import`` 与
   ``ast.ImportFrom`` ⇒ ``importlib.import_module('opencode_bridge.__main__')`` /
   裸 ``import opencode_bridge`` 之后再取 ``.__main__`` / ``from opencode_bridge
   import *`` 三条路**实测各自 0 violation** ⇒ 判据③对**整个文件**失效，
   :func:`unisolated_call_sites` 看不见那个文件里的任何间接落盘点 ⇒ 那两份产物落进
   **仓库根**而**没有任何测试变红**。⛔ 而那两份产物的名字都在 ``.gitignore`` 里
   ⇒ 这份污染**结构上看不见**（``git status`` 干净）。

⚠️ 五条都是**同一类**失败：判据在自己那侧就已经漏了，所以「它报了就等于没问题」
这个推理不成立 —— 而这类洞只有靠**喂反向样本**才看得见（AGENTS.md §9）。
"""

from __future__ import annotations

import ast
import pathlib
import re

import opencode_bridge
from opencode_bridge import health, subscription_health

TESTS_DIR = pathlib.Path(__file__).resolve().parent

#: 这几个文件名**不写死**：它们是 :mod:`opencode_bridge.health` 与
#: :mod:`opencode_bridge.subscription_health` 的契约常量，那边改名的话这里必须跟着变
#: —— 写死字符串会让改名悄悄绕过这条护栏。
#:
#: ⚠️ **三份**，不是两份（2026-10-08 加）：第三份是「/api/event 订阅线程此刻怎么样」
#: 那条运行期通道，它与前两份同样落在 ``__bridge_dir`` 下。
RUNTIME_ARTIFACT_NAMES = (
    health.PLATFORM_HEALTH_FILE_NAME,
    health.OUTBOUND_FAILURES_FILE_NAME,
    subscription_health.SUBSCRIPTION_HEALTH_FILE_NAME,
)

#: 直接落盘的那些入口（名字取自 :data:`opencode_bridge.health.__all__` 与
#: :data:`opencode_bridge.subscription_health.__all__`）。
#
#: ⚠️ 装订阅那条通道的那两个名字**本身不落盘**（一个只 ``open``、一个只赋值），
#: 而它们⛔ **必须**一起算进来 —— 一条用例只走「装配」那一路（像
#: :class:`~tests.test_inbox_wiring.CliWiresTheInbox`）时**症状层抓不到**
#: （没有观测就没有产物），而本层抓的正是那一路。
ARTIFACT_WRITING_CALLS = frozenset({
    "record_startup_probes",
    "OutboundFailureRecorder",
    "SubscriptionHealthRecorder",
    "install_subscription_health_recorder",
})

#: 本仓库 CLI 模块在包内的**名字**（:mod:`opencode_bridge.__main__` 那个 ``__main__``）。
#: ⚠️ 它只是**模块名**，不是导入时绑定的名字 —— 那件事由 :data:`CLI_MODULE_ALIAS` 管。
CLI_MODULE_NAME = "__main__"

#: ``test_*`` 模块里 ``__main__`` 一律以这个名字导入（``from opencode_bridge
#: import __main__ as cli``）⇒ 只有挂在它下面的函数才是本仓库的 CLI 入口，
#: 别的 ``.main()``（比如 ``threading.main``）不算。
#:
#: ⛔ **它不再只是注释里的约定**：:func:`cli_module_import_alias_violations` 与
#: :func:`cli_module_binding_escape_violations` 把它变成**机械强制**（见
#: :func:`is_cli_entry_point` 那条依赖它的前提）。
#:
#: ⚠️⚠️ **「机械强制」只在上面那两条判据覆盖到的形状内成立** —— 覆盖**不到**的
#: 形状（``getattr`` / ``exec`` / ``sys.modules[...]`` / 拼不出来的模块名 /
#: 跨文件传递）逐条列在 :func:`cli_module_binding_escape_violations` 的 docstring
#: 里。⛔ 别把这条读成「任何写法都逃不掉」：**那正是 5 那个洞的成因**（上一版
#: docstring 就是这么写的，而它当时只覆盖两条 import 语句）。
CLI_MODULE_ALIAS = "cli"

#: 上面那条机械强制只在 ``tests/`` 里扫这一个包 —— 别处（本仓库不存在的场景）
#: 换个包名进来时，那条护栏会开始对着无关代码报「别名不对」。
CLI_PACKAGE_NAME = "opencode_bridge"

#: 「按名字动态导入」那个函数的**末段名**（``importlib.import_module``）。
#: ⛔ 按**末段**比而不是按全名：写成 ``from importlib import import_module`` 之后
#: 全名就只剩这一段了。⚠️ 这里只决定「**哪个调用要查**」，查出来的模块名还要过
#: :func:`text_mentions_the_cli_module` 那一关 ⇒ 误认一个函数也只是白查一遍，
#: **不会**变成误报。
DYNAMIC_IMPORT_FUNCTION_NAME = "import_module"

#: 「这段文本静态不可知」那部分的占位字符。⚠️ 选它是因为它**绝不可能**出现在任何
#: 合法模块名里 ⇒ 静态拼不出来的形状只会让判据**看不见**（被如实记进
#: :func:`cli_module_binding_escape_violations` 的覆盖表），而不会让它**瞎报** ——
#: 一个恒报的判据等于没有判据（AGENTS.md §9）。
#:
#: ⚠️ 它同时充当**分隔符**：``'opencode_bridge.%s' % '__main__'`` 若把 ``%s``
#: 原样留着，``%s`` 的 ``s`` 会紧贴 ``__main__``，于是
#: :data:`MODULE_NAME_SEGMENT_PATTERN` 的**词边界**回看把它判成「同一个词」而漏掉
#: （实测踩过）。⇒ 转换说明符一律换成这个占位符。
UNKNOWN_TEXT_PLACEHOLDER = "\u0000"

#: ``%`` 格式化里的转换说明符（``%s`` / ``%.2f`` / ``%(name)r`` / ``%%``）。
#: ⚠️ 它只作用于**格式串**那一侧 —— 实参那一侧的值原样拼上（见
#: :func:`statically_known_module_name_text` 的 ``Mod`` 分支）。
PERCENT_CONVERSION_PATTERN = re.compile(r"%[-+ #0-9.*]*[a-zA-Z%]")

#: 「这段文本提到了包名**或** CLI 模块名」的那两种词段，各按**词边界**匹配。
#: ⚠️ 不能拿裸子串：``opencode_bridge_other`` / ``__main___doc`` 都不算。
#:
#: ⚠️ 这里只负责「**认出词段**」，「两个词段**都在**」那一关在
#: :func:`text_mentions_the_cli_module` 里 —— 一张正则认词段、一个函数判齐全，
#: ⛔ 不要合成一处：那样就没法说清「为什么不能直接找全名连在一起的那一串」。
MODULE_NAME_SEGMENT_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(?:%s|%s)(?![A-Za-z0-9_])"
                                         % (re.escape(CLI_PACKAGE_NAME),
                                            re.escape(CLI_MODULE_NAME)))

#: :func:`production_artifact_writing_entry_points` 从真源码推出来的那个**起点**
#: （:func:`opencode_bridge.__main__.main`）。⚠️ 写死是刻意的：改它的话下面那条
#: 自守测试会对着空集变红 —— 那比静默推成空集好。
CLI_TOP_LEVEL_FUNCTION = "main"

#: 间接落盘入口的**名字规则**（⛔ 是规则不是名字表 —— 理由见 :func:`is_cli_entry_point`）。
CLI_ENTRY_POINT_EXACT_NAMES = frozenset({CLI_TOP_LEVEL_FUNCTION})
CLI_ENTRY_POINT_NAME_FRAGMENT = "run_bridge"

#: 认得出「这个作用域把 bridge 目录钉住了」的两个记号（结构判据，见模块 docstring）。
BRIDGE_DIR_PIN_ATTRIBUTE = "_bridge_dir"
BRIDGE_DIR_ENV_KEY = "OPENCODE_BRIDGE_CONFIG"

#: 「目录是**推导**出来的（于是就是仓库根）」的记号：直调的实参里出现这些，
#: 补丁的**替换值**里出现这些，环境变量的**值**里出现这些，都不算隔离。
#: ⛔ 只有 :func:`derived_from_repository_root` 该直接读这张表。
#: ⚠️ ``REPOSITORY_ROOT`` 是 ``_REPOSITORY_ROOT`` 的**超集**（后者含前者），写成
#: 带下划线那个就认不出本仓库自己的 ``tests/test_repository_root_artifacts.py``
#: 里那个 ``REPOSITORY_ROOT``。
UNINJECTED_DIR_MARKERS = ("getcwd", "_bridge_dir", "REPOSITORY_ROOT", "__file__")

#: 「这个名字指的是仓库根」的**词**，按**词边界**、大小写不敏感地匹配。
#: ⚠️ 不能拿裸子串：``report_dir`` 里也含 "repo" ⇒ 误报一出现，就会有人把这条记号
#: 删掉（于是真洞回来）。所以 ``repository_root`` / ``repo`` / ``REPOSITORY_ROOT``
#: 认得，``report_dir`` / ``repos`` 不认。
REPOSITORY_NAME_PATTERN = re.compile(
    r"(?<![a-z])(?:repo|repository)(?![a-z])", re.IGNORECASE
)


def dotted_name(node: ast.AST) -> str:
    """``health.record_startup_probes`` → ``"health.record_startup_probes"``。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """⚠️ ``ast.walk`` 会吐出**没有位置信息**的节点，所以这里显式建父子表 ——
    要从调用点回溯到「它所在的函数/类」，没有这张表就无从下手。"""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def enclosing_function_and_class(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[ast.AST | None, ast.ClassDef | None]:
    """从调用点回溯到最近的「函数」与「类」（各取最近的**一个**）。"""
    function: ast.AST | None = None
    class_node: ast.ClassDef | None = None
    walk: ast.AST = node
    while walk in parents:
        walk = parents[walk]
        if isinstance(walk, (ast.FunctionDef, ast.AsyncFunctionDef)) and function is None:
            function = walk
        if isinstance(walk, ast.ClassDef) and class_node is None:
            class_node = walk
    return function, class_node


def scope_label(class_node: ast.ClassDef | None, function: ast.AST | None) -> str:
    """``Sample.run`` / ``<模块层>.<模块层>`` —— 报错文案里指认位置用。"""
    return "%s.%s" % (
        class_node.name if class_node is not None else "<模块层>",
        getattr(function, "name", "<模块层>"),
    )


#: 「这一层看起来钉了、但**值仍指向仓库根**」的文案前缀。⚠️ **承重**：
#: :func:`unisolated_call_sites` 靠它区分「这条是隔离措施」与「这条是缺陷」。
#: 两个 pin 函数因此共用**同一种**返回值形状（一句话），而不是两种类型。
PIN_UNUSING_PREFIX = "没钉住"
#: :func:`bridge_dir_pin_in_scope` 的结果之一：这一层**真的钉住了**。
PIN_ISOLATED = "隔离"


def derived_from_repository_root(source_text: str) -> bool:
    """这段**值表达式**是不是仍然推导自仓库根（于是落盘就落在仓库根）。

    ⛔ **只有这一份**「这个值是不是仓库根」的判据。⚠️ 写成多份就等于多份会各自
    漂移，而这件事**真的发生过**：``_bridge_dir`` 补丁的替换值那条路当初被实测出
    是恒绿的洞并修好了（改用「值必须指向临时目录」），而**同型**的环境变量那条路
    当时还停留在「设了没有」—— 指向仓库根照样算隔离。三处（补丁替换值、环境变量
    的值、直调入口的目录实参）现在一律问它。
    """
    if REPOSITORY_NAME_PATTERN.search(source_text):
        return True
    return any(marker in source_text for marker in UNINJECTED_DIR_MARKERS)


def patched_bridge_dir(scope: ast.AST) -> str | None:
    """这个作用域里有没有把 ``_bridge_dir`` 钉到**临时目录**上。

    :return: 命中时返回一句人话（「补丁钉到了 ``os.getcwd()``」那种），没命中返回 ``None``。
        以 :data:`PIN_UNUSING_PREFIX` 开头 = 看着钉了、实际没钉住 ⇒ **是缺陷**。

    ⚠️ **必须检查补丁的替换值**，不能只看「有没有 ``mock.patch.object(cli,
    "_bridge_dir", ...)``」：``mock.patch.object(cli, "_bridge_dir", lambda:
    os.getcwd())`` 那个补丁**照样把目录指回仓库根** —— 它结构上「有隔离」，
    实际一点没隔离。**实测过**：只判「补丁在不在」的话，这个变异让判据**恒绿**
    （AGENTS.md §9：恒真的断言比没有断言更危险）。

    ⚠️⛔ **必须扫完整个作用域，且任何一条「没钉住」优先于任何一条「钉住了」** ——
    「先把目录钉好、稍后再改回 cwd」正是变异测试会写出来的形状，而它**就是这条判据
    曾经的逃逸口**：这里原先命中第一个就 ``return``，于是判据的结论**由源码顺序决定**。
    **实测过**：「先 ``lambda: self.bridge_dir``、后 ``lambda: os.getcwd()``」**不报**，
    把两个顺序对调才报 ⇒ 同一份缺陷代码，两种写法一个红一个绿。
    ⇒ 现在两条都归拢后再决定，顺序不再影响结论（两个方向的用例见
    :class:`tests.test_artifact_isolation_defect_regressions.TestEveryBridgeDirPinInTheScopeIsJudged`）。

    ⛔ 而「优先」这个方向**两个方向都成立**：「先钉住、后没钉住」与「先没钉住、
    后钉住」都必须报 —— 只堵住其中一个顺序，另一个立刻变成同样的洞。
    """
    isolated_pin: str | None = None
    unusing_pin: str | None = None
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        callee = dotted_name(node.func)
        if not (callee.endswith("patch") or callee.endswith("patch.object")):
            continue
        arguments = list(node.args) + [keyword.value for keyword in node.keywords]
        for index, argument in enumerate(arguments):
            if not (
                isinstance(argument, ast.Constant)
                and argument.value == BRIDGE_DIR_PIN_ATTRIBUTE
            ):
                continue
            replacement = arguments[index + 1] if index + 1 < len(arguments) else None
            if replacement is None:
                # 没有替换值 ⇒ 补丁保留原函数 ⇒ 什么都没隔离。
                if unusing_pin is None:
                    unusing_pin = (
                        "%s：patch 了 _bridge_dir 但没给替换值" % PIN_UNUSING_PREFIX
                    )
                continue
            text = ast.unparse(replacement)
            if derived_from_repository_root(text):
                if unusing_pin is None:
                    unusing_pin = (
                        "%s：patch 了 _bridge_dir，但替换值是 %s（仍指向仓库根）"
                        % (PIN_UNUSING_PREFIX, text)
                    )
                continue
            if isolated_pin is None:
                isolated_pin = "mock.patch 了 _bridge_dir（替换值 %s）" % text
    if unusing_pin is not None:
        return unusing_pin
    return isolated_pin


def env_config_values(node: ast.AST) -> list[ast.expr]:
    """这个节点里所有「把 ``OPENCODE_BRIDGE_CONFIG`` 设成什么」的**值**表达式。

    三种写法都收：``os.environ[KEY] = 值`` / ``os.environ.setdefault(KEY, 值)`` /
    ``mock.patch.dict(os.environ, {KEY: 值})``。

    ⚠️ ``setdefault`` **只给键不给值**时不产出任何表达式：它保留原有值，而那个值
    指向哪里与这层隔离无关 ⇒ 不能算隔离（与「patch 了但没给替换值」同一条纪律）。
    """
    if isinstance(node, ast.Assign):
        if any(_is_env_subscript(target) for target in node.targets):
            return [node.value]
        return []
    if not isinstance(node, ast.Call):
        return []
    callee = dotted_name(node.func)
    if callee.endswith("setdefault") and node.args:
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value == BRIDGE_DIR_ENV_KEY:
            return [node.args[1]] if len(node.args) > 1 else []
    if not (callee.endswith("patch.dict") or callee.endswith("patch_dict")):
        return []
    values: list[ast.expr] = []
    for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
        if not isinstance(argument, ast.Dict):
            continue
        for key, value in zip(argument.keys, argument.values):
            if isinstance(key, ast.Constant) and key.value == BRIDGE_DIR_ENV_KEY:
                values.append(value)
    return values


def bridge_config_env_pin(scope: ast.AST) -> str | None:
    """这个作用域有没有把 ``OPENCODE_BRIDGE_CONFIG`` 指向**临时目录**。

    :return: 命中时一句人话；**根本没设这个变量**（或 ``setdefault`` 没给值）返回 ``None``。
        以 :data:`PIN_UNUSING_PREFIX` 开头 = 设了、但值仍指向仓库根 ⇒ **是缺陷**。

    ⚠️ **必须看值，不能只看「设了没有」** —— 与 :func:`patched_bridge_dir` **完全
    同型**的洞：``os.environ["OPENCODE_BRIDGE_CONFIG"] = <仓库根里的某个文件>``
    结构上「设了环境变量」，而 :func:`opencode_bridge.__main__._bridge_dir` 在那个
    变量指向一个**存在的文件**时返回的正是那个文件所在的目录 ⇒ 指向仓库根就是
    仓库根。**实测过**：只看「设了没有」的判据对这样的样本恒不报。⇒ 值的判据复用
    :func:`derived_from_repository_root`，⛔ 不另写一份。
    """
    isolated_pin: str | None = None
    for node in ast.walk(scope):
        for value in env_config_values(node):
            text = ast.unparse(value)
            if derived_from_repository_root(text):
                return "%s：设了 %s，但值是 %s（仍指向仓库根）" % (
                    PIN_UNUSING_PREFIX, BRIDGE_DIR_ENV_KEY, text)
            if isolated_pin is None:
                isolated_pin = "设了 %s（指向 %s）" % (BRIDGE_DIR_ENV_KEY, text)
    return isolated_pin


def bridge_dir_pin_in_scope(scope: ast.AST) -> tuple[str, str] | None:
    """这个作用域钉住 bridge 目录了吗；钉不住的话它是**缺陷**。

    :return: ``None``（这一层什么也没钉）/ ``(PIN_ISOLATED, 人话)`` /
        ``(PIN_UNUSING_PREFIX, 人话)``。

    ⚠️ **patch 优先于环境变量**：``_bridge_dir`` 被 :func:`mock.patch` 住时它
    **根本不读** ``OPENCODE_BRIDGE_CONFIG`` ⇒ 那时再看环境变量就是误报（本仓库很常见：
    类里把目录 patch 到临时目录，而进程里本来就带着仓库根的环境变量）。

    ⚠️⚠️ 「patch 优先」**不是**「先命中者胜」：一个作用域里**若干条** pin 的结论必须
    合起来算，且任何一条「没钉住」压过全部「钉住了」（见 :func:`patched_bridge_dir`）。
    ⚠️ 两个 pin 函数都已经满足这条不变量 —— 环境变量那条是「见到没钉住就立刻返回」，
    补丁那条是「扫完整作用域再取没钉住」，**都与源码顺序无关**。
    """
    patch_pin = patched_bridge_dir(scope)
    if patch_pin is not None:
        return _verdict_of_pin(patch_pin)
    env_pin = bridge_config_env_pin(scope)
    return None if env_pin is None else _verdict_of_pin(env_pin)


def _verdict_of_pin(pin: str) -> tuple[str, str]:
    """一句话 → 「它是隔离，还是一个看着像隔离的缺陷」。"""
    if pin.startswith(PIN_UNUSING_PREFIX):
        return PIN_UNUSING_PREFIX, pin
    return PIN_ISOLATED, pin


def _is_env_subscript(target: ast.AST) -> bool:
    """``os.environ["OPENCODE_BRIDGE_CONFIG"] = x`` 里的那个下标目标。"""
    if not isinstance(target, ast.Subscript):
        return False
    if not isinstance(target.value, ast.Attribute):
        return False
    if target.value.attr != "environ":
        return False
    return isinstance(target.slice, ast.Constant) and target.slice.value == BRIDGE_DIR_ENV_KEY


def same_module_base_chain(classes: dict[str, ast.ClassDef], name: str) -> list[ast.ClassDef]:
    """``name`` 及其**同文件**基类（深度优先）。

    ⚠️ 只解析同文件的基类：跨模块的隔离基类本仓库目前**不存在**（隔离措施都在被
    继承的那个文件里），解析不了就当「没有」而不是「有」。
    """
    chain: list[ast.ClassDef] = []
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen or current not in classes:
            continue
        seen.add(current)
        chain.append(classes[current])
        pending.extend(dotted_name(base) for base in classes[current].bases)
    return chain


def class_scopes(class_node: ast.ClassDef | None, classes: dict[str, ast.ClassDef]) -> list[ast.AST]:
    """所在类**连同它同文件的基类**（隔离措施通常在基类的 ``setUp`` 里）。"""
    if class_node is None:
        return []
    return same_module_base_chain(classes, class_node.name)


def bridge_dir_argument(call: ast.Call) -> str | None:
    """取那两个落盘入口的「bridge 目录」实参的源码文本（取不到就 ``None``）。"""
    if not call.args:
        return None
    return ast.unparse(call.args[0])


def module_level_functions(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    """``{函数名: 那个 FunctionDef}``，**只取模块层**（类里的方法不是 CLI 入口）。"""
    return {
        node.name: node
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.FunctionDef)
    }


def called_bare_names(function: ast.FunctionDef) -> set[str]:
    """这个函数体里被调用的**裸名**（``health.record_startup_probes`` → 那个末段）。"""
    return {
        dotted_name(node.func).rsplit(".", 1)[-1]
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }


def production_artifact_writing_entry_points() -> frozenset[str]:
    """从**真源码**推出 ``__main__.py`` 里哪些模块层函数会（传递地）写运行期产物。

    判据是「从 :data:`CLI_TOP_LEVEL_FUNCTION` 出发传递可达 **且**能到达落盘入口」，
    与**命名无关** ⇒ 改名或新增一层委托都不会漏。

    :return: 函数名集合。⛔ **源码读不到 / 解析不了时是空集** —— 那不是「没有入口」，
        所以 :class:`~tests.test_artifact_isolation_contract` 里有一条自守测试专门
        断言这个集合非空（AGENTS.md §7.1：空集 ≠ 不存在）。

    ⚠️ 传递可达里**包含**辅助函数（如 ``_record_bridge_refusal``）—— 这是对的：
    一条用例直接调它同样会把文件写进仓库根。
    """
    package_root = getattr(opencode_bridge, "__file__", None)
    if not package_root:
        return frozenset()
    main_path = pathlib.Path(package_root).resolve().parent / "__main__.py"
    if not main_path.is_file():
        return frozenset()
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    functions = module_level_functions(tree)
    calls = {name: called_bare_names(function) for name, function in functions.items()}

    def reaches_artifact_writing(name: str, visited: frozenset[str]) -> bool:
        if name in ARTIFACT_WRITING_CALLS:
            return True
        if name in visited or name not in functions:
            return False
        return any(
            reaches_artifact_writing(callee, visited | {name})
            for callee in calls[name]
        )

    reachable: set[str] = set()
    frontier = [CLI_TOP_LEVEL_FUNCTION]
    while frontier:
        current = frontier.pop()
        if current in reachable or current not in functions:
            continue
        reachable.add(current)
        frontier.extend(callee for callee in calls[current] if callee in functions)
    return frozenset(
        name for name in reachable if reaches_artifact_writing(name, frozenset())
    )


def _read_production_entry_points() -> frozenset[str]:
    """兜底：生产源码读不到就当「没有」，而不是让整个判据崩掉。"""
    try:
        return production_artifact_writing_entry_points()
    except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return frozenset()


#: 真启动路径上那些层的名字（:func:`production_artifact_writing_entry_points` 的结果）。
#: ⚠️ 它与 :data:`CLI_ENTRY_POINT_EXACT_NAMES` / :data:`CLI_ENTRY_POINT_NAME_FRAGMENT`
#: 是**并集**关系，不是替代：名字规则保证「源码读不到」时判据照样工作，而这一份
#: 保证「名字对不上」（`_record_bridge_refusal` 那类辅助函数）时也照样认出来。
PRODUCTION_ENTRY_POINTS = _read_production_entry_points()


def statically_known_module_name_text(node: ast.expr) -> str | None:
    """这个「模块名表达式」里**静态已知**的文本（``+`` 拼接 / ``%`` / f-string 都算）。

    :return: 拼出来的文本；**整个形状都不认识**时返回 ``None``（⇒ 判据闭嘴）。

    ⚠️ 静态不可知的那一段用 :data:`UNKNOWN_TEXT_PLACEHOLDER` 顶替，所以
    ``importlib.import_module('opencode_bridge.adapters.%s' % platform)``（本仓库
    ``tests/test_edit_length_guard.py`` 的**真实**写法）拼出的是
    ``opencode_bridge.adapters.<占位符>``，**不**含 ``__main__`` ⇒ 不报。⚠️ 反过来
    ``f'opencode_bridge.{CLI_MODULE_NAME}'`` 同样拼不出 ``__main__`` ⇒ **也不报**
    —— 那是覆盖不到的形状，见 :func:`cli_module_binding_escape_violations`。

    ⛔ 这里**不解析任何常量**：``CLI_MODULE = '__main__'`` 之后
    ``import_module(f'opencode_bridge.{CLI_MODULE}')`` 拼不出来 ⇒ 也不报。
    解析同文件常量会引入一个递归失败模式，换来的只是一个不那么现实的形状 ⇒ 不做。
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else UNKNOWN_TEXT_PLACEHOLDER
    if isinstance(node, ast.JoinedStr):
        pieces = [statically_known_module_name_text(value) for value in node.values]
        return "".join(piece if piece is not None else UNKNOWN_TEXT_PLACEHOLDER
                       for piece in pieces)
    if isinstance(node, ast.BinOp):
        left = statically_known_module_name_text(node.left)
        right = statically_known_module_name_text(node.right)
        if isinstance(node.op, ast.Add):
            return (left or UNKNOWN_TEXT_PLACEHOLDER) + (right or UNKNOWN_TEXT_PLACEHOLDER)
        if isinstance(node.op, ast.Mod) and left is not None:
            #: 右操作数静态已知就也拼上（``'opencode_bridge.%s' % '__main__'`` 得报）；
            #: 未知就只留格式串（``... % platform`` 不报，见上）。
            #: ⚠️ 格式串上的转换说明符先换成占位符，否则 ``%s`` 的尾巴会贴住下一段
            #: 而被**词边界**判成同一个词（见 :data:`PERCENT_CONVERSION_PATTERN`）。
            blanked = PERCENT_CONVERSION_PATTERN.sub(UNKNOWN_TEXT_PLACEHOLDER, left)
            return blanked if right is None else blanked + right
    return None


def text_mentions_the_cli_module(text: str) -> bool:
    """这段文本是不是**提到了**本仓库的 CLI 模块（包名与模块名**两个词段都在**）。

    ⚠️⚠️ 判的是「两段都在」而**不是**「全名连在一起」那一串：``'opencode_bridge.%s'
    % '__main__'`` 那种形状（占位符夹在中间）**拼不出**全名，而它**确实**是那个模块
    ⇒ 找全名会漏掉它（实测踩过）。
    """
    segments = set(MODULE_NAME_SEGMENT_PATTERN.findall(text))
    return CLI_PACKAGE_NAME in segments and CLI_MODULE_NAME in segments


def cli_module_import_bindings(source: str, filename: str) -> list[tuple[int, str]]:
    """这个文件里每一次「导入本仓库 CLI 模块」**绑定到的名字**：``[(行号, 名字), ...]``。

    ⛔ 别名对了的也照样列出来 —— 「判据看见了几个」是 :func:`cli_module_import_alias_violations`
    能不能**被信任**的前提（AGENTS.md §7.1：空集 ≠ 不存在）：只列错的那几个的话，
    一条「都对了」可以来自「一个都没看见」。

    ⚠️ 收四种写法，两种导入形式 × 有没有 ``as``：``from opencode_bridge import
    __main__``（不写 ``as`` 时绑定的名字是 ``__main__``，**错**）与
    ``import opencode_bridge.__main__``（不写 ``as`` 时绑定的名字是**包名**
    ``opencode_bridge``，同样**错**）。

    ⛔ 这里**只列绑定名可知**的那些：:func:`cli_module_binding_escape_violations
    收的三种写法绑定名**不可知**，硬塞一个假名字进来会污染「扫到几个」这个数 ——
    那条自守测试靠的就是这个数干净。
    """
    bindings: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.ImportFrom) and node.module == CLI_PACKAGE_NAME:
            for imported_name in node.names:
                if imported_name.name == CLI_MODULE_NAME:
                    bindings.append((node.lineno, imported_name.asname or imported_name.name))
        elif isinstance(node, ast.Import):
            for imported_name in node.names:
                if imported_name.name == "%s.%s" % (CLI_PACKAGE_NAME, CLI_MODULE_NAME):
                    bindings.append((node.lineno, imported_name.asname or CLI_PACKAGE_NAME))
    return sorted(bindings)


def _dynamic_import_of_cli_module_violations(tree: ast.AST, filename: str) -> list[str]:
    """逃逸口①：``importlib.import_module('<提到了 CLI 模块的字符串>')``。

    ⚠️ 判被调方的**末段名**而不是全名：``from importlib import import_module`` 之后
    全名就只剩 ``import_module`` 一段（按全名比会漏掉那一种写法）。

    ⚠️ 判「那个字符串**提到了** CLI 模块」而不是「那个字符串**就是** CLI 模块」：
    ``'opencode_bridge.' + '__main__'`` / ``'opencode_bridge.%s' % '__main__'`` 拼得出
    全名，而 ``f'opencode_bridge.__main__'`` 在没有占位符时也解析成 ``JoinedStr`` ——
    这几种都要算命中（用例见 :attr:`DYNAMIC_MODULE_NAME_VARIANTS
    <tests.test_cli_module_alias_guard_reporting_regressions.TestTheCliModuleAliasGuardSeesEverySecondaryPath.DYNAMIC_MODULE_NAME_VARIANTS>`）。

    ⛔ 静态**拼不出来**的那些（``f'opencode_bridge.{name}'`` / ``... % platform`` /
    ``import_module(常量名)``）一律**不报** —— 本仓库
    ``tests/test_edit_length_guard.py`` 的真实写法就是其中之一，判据若对它们也报，
    这条护栏今天就已经红了（实测 0 violation）。
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if dotted_name(node.func).rsplit(".", 1)[-1] != DYNAMIC_IMPORT_FUNCTION_NAME:
            continue
        text = statically_known_module_name_text(node.args[0])
        if text is None or not text_mentions_the_cli_module(text):
            continue
        violations.append(
            "%s:%d: import_module(%s) 动态导入了 CLI 模块 —— 绑定的名字由实参决定，"
            "而判据③只认 %r 前缀 ⇒ 认不出来"
            % (filename, node.lineno, ast.unparse(node.args[0]), CLI_MODULE_ALIAS)
        )
    return violations


def _package_attribute_cli_module_violations(tree: ast.AST, filename: str) -> list[str]:
    """逃逸口②：裸 ``import opencode_bridge``（或 ``as`` 成别的名字）之后取 ``.__main__``。

    ⚠️ **两件事都要在同一个文件里出现**才报：有裸导入却不碰 ``.__main__`` 是本仓库
    ``redaction_cjk_tail_support.py`` 等三个文件今天的写法（判据共享件自己也这么写）
    ⇒ 只看「有没有裸导入」会当场误报三个文件。⚠️ 而反过来 ``.__main__`` 的那个属性名
    必须是 ``ast.Attribute`` 节点 —— 写在 docstring 里不算（**实测**：共享件与
    ``test_redaction.py`` 的模块 docstring 里都出现过 ``opencode_bridge.__main__``
    这行字，扫全文必然误报）。
    """
    #: 两种绑定名都要认：``import opencode_bridge``（绑到 ``opencode_bridge``）与
    #: ``import opencode_bridge as pkg``（绑到 ``pkg``）—— 判据③在两种情况下都瞎。
    package_binding_lines = {
        imported_name.asname or CLI_PACKAGE_NAME: node.lineno
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for imported_name in node.names if imported_name.name == CLI_PACKAGE_NAME
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == CLI_MODULE_NAME):
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id in package_binding_lines):
            continue
        violations.append(
            "%s:%d: import %s 绑到 %r，这里用 %r.%s 取了 CLI 模块 —— 那里的调用点"
            "全名以 %r 开头而不是 %r ⇒ 判据③认不出来"
            % (filename, node.lineno, CLI_PACKAGE_NAME, node.value.id, node.value.id,
               CLI_MODULE_NAME, CLI_PACKAGE_NAME, CLI_MODULE_ALIAS)
        )
    return violations


def _star_import_of_cli_package_violations(tree: ast.AST, filename: str) -> list[str]:
    """逃逸口③：``from opencode_bridge import *``（绑定了哪些名字不可知）。

    ⚠️⚠️ **今天这条并不是活漏洞，判据仍然要求它存在** —— 因为**实测**那个星号导入
    绑到的是 ``Config`` / ``OpenCodeClient`` 等八个名字，**不含** ``__main__``（那个
    包有显式 ``__all__``，而 ``__main__`` 以下划线开头）。⇒ 它是**潜伏**的：哪天把
    ``"__main__"`` 加进 ``__all__``（「把 CLI 入口也导出」是个很自然的需求），
    这个形状立刻变成真洞，而那时**没有任何测试会红**。⇒ 判据不能去查那个包的
    ``__all__``（那又要读真源码，又是一处会自己漂移的地方），只能报。
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and not node.level
                and node.module == CLI_PACKAGE_NAME):
            continue
        if not any(imported_name.name == "*" for imported_name in node.names):
            continue
        violations.append(
            "%s:%d: from %s import * —— 绑定了哪些名字取决于那个包的 __all__，"
            "判据看不见 ⇒ 哪怕今天它**恰好**没绑到 __main__ 也必须报"
            % (filename, node.lineno, CLI_PACKAGE_NAME)
        )
    return violations


#: 那三条逃逸口各自由哪个判据负责（顺序 = 报错的顺序）。
#:
#: ⚠️ 做成一张**显式的表**而不是三句直写，是为了让「每一条都得独立被钉住」这件事
#: 有地方落脚：:class:`tests.test_artifact_isolation_defect_regressions` 逐个喂反样本，
#: 把某一条短路掉时**只**红对应那一条（而不是三条一起红 ⇒ 看不出是哪条坏了）。
CLI_MODULE_BINDING_ESCAPE_CHECKS = (
    _dynamic_import_of_cli_module_violations,
    _package_attribute_cli_module_violations,
    _star_import_of_cli_package_violations,
)


def cli_module_binding_escape_violations(source: str, filename: str) -> list[str]:
    """这个文件里「绕开 import 语法、把 CLI 模块绑到别的名字上」的三种写法。

    :return: ``["<文件名>:<行号>: ...", ...]``，**按行号排序**。

    ## 为什么要有这三道

    :func:`is_cli_entry_point` 先把**非 ``cli.`` 前缀**的调用全部 ``return False``
    ⇒ 只要一个文件把 CLI 模块绑到 ``cli`` 以外的任何名字上，判据③（从真源码推出来的
    那条）就**对整个文件失效** ⇒ :func:`unisolated_call_sites` 看不见那个文件里的
    **任何**间接落盘点 ⇒ 那两份产物落进**仓库根**而没有任何测试变红。

    ⚠️ 而这份污染**结构上看不见**：两个产物名都在 ``.gitignore`` 里，``git status``
    干净。⇒ 于是上一版 :func:`cli_module_import_alias_violations` 只看
    ``ast.Import`` / ``ast.ImportFrom`` 这件事**实测**是个洞：这三条路当时各自
    **0 violation**。

    ## ⛔ 覆盖**不到**的形状（这一节不许省，它就是 5 那个洞的同类）

    这些都**报不出来**（实测 0 violation），写在这里是为了**不让 docstring 替我
    们说它们被覆盖了**：

    1. ``getattr(opencode_bridge, "__main__")`` —— 属性名是**字符串实参**，
       不是 ``ast.Attribute`` 节点。
    2. ``exec("import opencode_bridge.__main__")`` / ``eval(...)`` —— 那段源码是
       字符串常量。
    3. ``sys.modules["opencode_bridge.__main__"]`` —— 下标是字符串常量。
    4. 模块名**静态拼不出来**的动态导入：``f'opencode_bridge.{name}'`` /
       ``'opencode_bridge.%s' % 变量`` / ``import_module(常量名)``
       （见 :func:`statically_known_module_name_text` 最后那段）。
    5. **跨文件传递**：helper 模块里绑好别名，测试文件只 ``from helper import cli``
       ⇒ 本函数只看**当前文件**的语法。
    6. ``from opencode_bridge.adapters import *`` —— 只认**包本身**那一层的 ``*``。
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for check in CLI_MODULE_BINDING_ESCAPE_CHECKS:
        violations.extend(check(tree, filename))
    return sorted(violations)


def cli_module_import_alias_violations(source: str, filename: str) -> list[str]:
    """这个文件里「把 CLI 模块导入成 :data:`CLI_MODULE_ALIAS` 以外的名字」的写法。

    :return: ``["<文件名>:<行号>: ...", ...]``。

    = 两部分，⛔ 少一部分就回到 5 那个洞

    ① :func:`cli_module_import_bindings` 过滤出来的**绑定名不对**（两种 import 语法）；
    ② :func:`cli_module_binding_escape_violations` 的三条**逃逸口**（绕开 import 语法）。

    ## 为什么需要这道机械护栏

    :func:`is_cli_entry_point` 先把**非 ``cli.`` 前缀**的调用全部 ``return False``，
    而「``test_*`` 一律 ``from opencode_bridge import __main__ as cli``」这条曾经**只
    活在注释里** ⇒ :data:`CLI_MODULE_ALIAS` 是个**没有机械保证**的名字约定，
    而判据③（真源码推出来的入口名单）**整个挂在这个前缀上**：

    ⛔ ``is_cli_entry_point('cli._run_bridge_locked')`` → ``True``，
    ``is_cli_entry_point('__main__._run_bridge_locked')`` → ``False``
    —— 尽管 ``_run_bridge_locked`` **确实在** :data:`PRODUCTION_ENTRY_POINTS` 里。
    ⇒ 有人写成 ``from opencode_bridge import __main__ as m`` 再调 ``m.run_bridge``，
    那一处隔离缺失就**静悄悄**不再被报（真源码那条兜底也一起失效）。

    **实测今天不是活 bug**（14 个测试文件全都写着 ``as cli``）—— 正是这一点让它危险：
    约定没坏，而**它随时可以坏、坏了没人拦**。⇒ 这里把它变成机械强制。

    ⚠️ 用 **AST** 而不是正则：``from opencode_bridge import __main__ as cli`` 这句话
    在好几个 docstring 与注释里被当作**范例**写着（就在本模块里），扫全文必然误报，
    而误报一出现就有人会去加豁免 —— 与本模块「不许扫全文」那条纪律同型。

    ⚠️⛔ **「机械强制」不等于「任何写法都逃不掉」** —— 上一版 docstring 就是这么宣称
    的，而它当时只覆盖 ① ⇒ 这正是 5 那个洞。覆盖不到的形状逐条列在
    :func:`cli_module_binding_escape_violations` 的 docstring 里。
    """
    violations = [
        "%s:%d: 导入 %s.%s 后它绑定的名字是 %r —— 恒为 %r"
        % (filename, lineno, CLI_PACKAGE_NAME, CLI_MODULE_NAME,
           bound_name, CLI_MODULE_ALIAS)
        for lineno, bound_name in cli_module_import_bindings(source, filename)
        if bound_name != CLI_MODULE_ALIAS
    ]
    return sorted(violations + cli_module_binding_escape_violations(source, filename))


def is_cli_entry_point(callee: str) -> bool:
    """这个调用是不是「**间接**写到运行期产物」的入口。

    ⛔ 必须挂在本仓库 CLI 模块的别名下（:data:`CLI_MODULE_ALIAS`）：别的 ``.main()``
    （``threading.main``）与本护栏无关。

    ⚠️ **必须扫间接入口**：本仓库的污染全部来自它们（``test_core`` 那三个文件里
    没有任何一处直接调 ``record_startup_probes``），只扫直接落盘那两个的话，
    「把 ``test_core`` 里那处 ``_bridge_dir`` 补丁删掉」这个变异根本不会被抓住。

    三条判据取并集，少一条就是一个洞：

    ① 名字**含** :data:`CLI_ENTRY_POINT_NAME_FRAGMENT` ⇒ 覆盖
       ``main`` → ``run_bridge`` → ``_run_bridge_locked`` 这条委托链上的任何一层。
       ⛔ **不是一张名字表**：``_run_bridge_locked`` 曾经漏在表外（**实测**：喂给
       :func:`unisolated_call_sites` 完全不报），而它才是真正写那两份文件的那层
       ⇒ 一张表只会在下次新增委托层时**再漏一次**。
    ② 名字**就是** :data:`CLI_TOP_LEVEL_FUNCTION`（argparse 那层，命名与①无关）。
    ③ 名字落在 :data:`PRODUCTION_ENTRY_POINTS` 里（从真源码推出来 ⇒ 改名、
       新增委托层、命名完全不同的辅助函数都不会漏）。

    ⚠️⚠️ **「与命名无关」只对①②成立；③还依赖「别名恒为 ``cli``」这个前提** ——
    实测：``_run_bridge_locked`` ∈ :data:`PRODUCTION_ENTRY_POINTS`，而
    ``is_cli_entry_point('__main__._run_bridge_locked')`` 是 ``False`` ⇒ 同一层换个
    别名，③立刻失效。这个前提**在 :func:`cli_module_import_alias_violations` 与
    :func:`cli_module_binding_escape_violations` 覆盖到的形状内**被机械保证了，
    所以「改名 / 新增委托层不会漏」这句话成立的前提是**那两条护栏还绿着** ——
    三处要一起看。

    ⛔ 而那两条护栏**不是**「任何写法都逃不掉」：``getattr`` / ``exec`` /
    ``sys.modules[...]`` / 拼不出模块名的动态导入 / 跨文件传递都**报不出来** ——
    逐条列在 :func:`cli_module_binding_escape_violations` 的 docstring 里。
    ⇒ 本函数那条「③ 与命名无关」的话**继承同一个前提**，不要单独拿出来当保证。
    """
    if not callee.startswith(CLI_MODULE_ALIAS + "."):
        return False
    bare = callee.rsplit(".", 1)[-1]
    return (
        bare in CLI_ENTRY_POINT_EXACT_NAMES
        or CLI_ENTRY_POINT_NAME_FRAGMENT in bare
        or bare in PRODUCTION_ENTRY_POINTS
    )


def classify_artifact_call(callee: str) -> tuple[bool, bool]:
    """这个调用是「直接落盘」/「间接入口」/ 两者都不是。"""
    bare = callee.rsplit(".", 1)[-1]
    return bare in ARTIFACT_WRITING_CALLS, is_cli_entry_point(callee)


def artifact_call_sites(source: str, filename: str) -> list[tuple[int, str, bool]]:
    """列出源码里**每一个**会写到运行期产物的调用点：``(行号, 全名, 是否直调)``。

    与 :func:`unisolated_call_sites` 分开，是因为「判据看得见调用点」
    和「判据认为它已隔离」是**两件要分别钉住的事** —— 只钉后者的话，一个恒空的
    遍历会连着让前者一起恒空（AGENTS.md §9）。
    """
    sites: list[tuple[int, str, bool]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        callee = dotted_name(node.func)
        is_direct, is_entry = classify_artifact_call(callee)
        if is_direct or is_entry:
            sites.append((node.lineno, callee, is_direct))
    return sorted(sites)


def unisolated_call_sites(source: str, filename: str) -> list[str]:
    """列出源码里**没有隔离措施**的落盘调用点。

    :return: ``["<文件名>:<行号>: <类>.<函数> 里的 <调用> —— <为什么算没隔离>", ...]``
    """
    tree = ast.parse(source, filename=filename)
    parents = parent_map(tree)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = dotted_name(node.func)
        is_direct, is_entry = classify_artifact_call(callee)
        if not (is_direct or is_entry):
            continue

        function, class_node = enclosing_function_and_class(node, parents)
        scopes: list[ast.AST] = []
        if function is not None:
            scopes.append(function)
        scopes.extend(class_scopes(class_node, classes))

        reasons: list[str] = []
        offending_pin: str | None = None
        for scope in scopes:
            pin = bridge_dir_pin_in_scope(scope)
            if pin is None:
                continue
            verdict, explanation = pin
            if verdict == PIN_UNUSING_PREFIX:
                # 「有隔离动作，但值仍指向仓库根」**不算**隔离 —— 它是本判据曾经
                # 恒绿的那两个洞（见 :func:`patched_bridge_dir` 与
                # :func:`bridge_config_env_pin` 的说明）。
                offending_pin = explanation
                break
            reasons.append(explanation)
        if offending_pin is not None:
            found.append(
                "%s:%d: %s 里的 %s —— %s"
                % (
                    filename, node.lineno,
                    scope_label(class_node, function), callee, offending_pin,
                )
            )
            continue
        if is_direct:
            argument = bridge_dir_argument(node)
            if argument is not None and not derived_from_repository_root(argument):
                reasons.append("目录是注入进来的（%s）" % argument)

        if reasons:
            continue
        found.append(
            "%s:%d: %s 里的 %s —— 所在函数/类里既没把 _bridge_dir 钉到临时目录、"
            "也没把 OPENCODE_BRIDGE_CONFIG 指向临时目录%s"
            % (
                filename,
                node.lineno,
                scope_label(class_node, function),
                callee,
                "，目录实参也不是注入的" if is_direct else "",
            )
        )
    return found