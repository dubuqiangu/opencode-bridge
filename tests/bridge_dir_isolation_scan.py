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
"""

from __future__ import annotations

import ast
import pathlib
import re

import opencode_bridge
from opencode_bridge import health

TESTS_DIR = pathlib.Path(__file__).resolve().parent

#: 这两个文件名**不写死**：它们是 :mod:`opencode_bridge.health` 的契约常量，
#: 那边改名的话这里必须跟着变 —— 写死字符串会让改名悄悄绕过这条护栏。
RUNTIME_ARTIFACT_NAMES = (
    health.PLATFORM_HEALTH_FILE_NAME,
    health.OUTBOUND_FAILURES_FILE_NAME,
)

#: 直接落盘的两个入口（名字取自 :data:`opencode_bridge.health.__all__`）。
ARTIFACT_WRITING_CALLS = frozenset({"record_startup_probes", "OutboundFailureRecorder"})

#: ``test_*`` 模块里 ``__main__`` 一律以这个名字导入（``from opencode_bridge
#: import __main__ as cli``）⇒ 只有挂在它下面的函数才是本仓库的 CLI 入口，
#: 别的 ``.main()``（比如 ``threading.main``）不算。
CLI_MODULE_ALIAS = "cli"

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
    """
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
                return "%s：patch 了 _bridge_dir 但没给替换值" % PIN_UNUSING_PREFIX
            text = ast.unparse(replacement)
            if derived_from_repository_root(text):
                return "%s：patch 了 _bridge_dir，但替换值是 %s（仍指向仓库根）" % (
                    PIN_UNUSING_PREFIX, text)
            return "mock.patch 了 _bridge_dir（替换值 %s）" % text
    return None


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