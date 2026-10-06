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
"""

from __future__ import annotations

import ast
import pathlib

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

#: **间接**写到同一批文件的两个 CLI 入口。⚠️ 必须一起扫：只扫上面那两个的话，
#: 「把 ``test_core`` 里那处 ``_bridge_dir`` 补丁删掉」这个变异**不会**被本判据
#: 抓住 —— 那三个文件里没有任何一处直接调 ``record_startup_probes``。
CLI_ENTRY_POINTS = frozenset({"run_bridge", "main"})

#: ``test_*`` 模块里 ``__main__`` 一律以这个名字导入（``from opencode_bridge
#: import __main__ as cli``）⇒ 只有挂在它下面的 ``main`` / ``run_bridge`` 才是本仓库
#: 的 CLI 入口，别的 ``.main()``（比如 ``threading.main``）不算。
CLI_MODULE_ALIAS = "cli"

#: 认得出「这个作用域把 bridge 目录钉住了」的两个记号（结构判据，见模块 docstring）。
BRIDGE_DIR_PIN_ATTRIBUTE = "_bridge_dir"
BRIDGE_DIR_ENV_KEY = "OPENCODE_BRIDGE_CONFIG"

#: 「目录是**推导**出来的（于是就是仓库根）」的记号：直调的实参里出现这些，
#: 或者补丁的**替换值**里出现这些，都不算隔离。
UNINJECTED_DIR_MARKERS = ("getcwd", "_bridge_dir", "_REPOSITORY_ROOT", "__file__")


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


def patched_bridge_dir(scope: ast.AST) -> str | None:
    """这个作用域里有没有把 ``_bridge_dir`` 钉到**临时目录**上。

    :return: 命中时返回一句人话（「补丁钉到了 ``os.getcwd()``」那种），没命中返回 ``None``。

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
                return "patch 了 _bridge_dir 但没给替换值"
            text = ast.unparse(replacement)
            if any(marker in text for marker in UNINJECTED_DIR_MARKERS):
                return "patch 了 _bridge_dir，但替换值是 %s（仍指向仓库根）" % text
            return "mock.patch 了 _bridge_dir（替换值 %s）" % text
    return None


def sets_bridge_config_env(scope: ast.AST) -> bool:
    """这个作用域里有没有**设** ``OPENCODE_BRIDGE_CONFIG``（而不只是读它）。"""
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and any(
            _is_env_subscript(target) for target in node.targets
        ):
            return True
        if isinstance(node, ast.Call):
            callee = dotted_name(node.func)
            if callee.endswith("setdefault") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == BRIDGE_DIR_ENV_KEY:
                    return True
            if not (callee.endswith("patch.dict") or callee.endswith("patch_dict")):
                continue
            for argument in list(node.args) + [kw.value for kw in node.keywords]:
                if not isinstance(argument, ast.Dict):
                    continue
                for key in argument.keys:
                    if isinstance(key, ast.Constant) and key.value == BRIDGE_DIR_ENV_KEY:
                        return True
    return False


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


def classify_artifact_call(callee: str) -> tuple[bool, bool]:
    """这个调用是「直接落盘」/「间接入口」/ 两者都不是。"""
    bare = callee.rsplit(".", 1)[-1]
    is_direct = bare in ARTIFACT_WRITING_CALLS
    is_entry = bare in CLI_ENTRY_POINTS and callee.startswith(CLI_MODULE_ALIAS + ".")
    return is_direct, is_entry


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

        reasons: list[str] | None = []
        for scope in scopes:
            pinned = patched_bridge_dir(scope)
            if pinned is None:
                continue
            if pinned.startswith("patch 了"):
                # 「补丁在，但替换值仍指向仓库根」**不算**隔离 —— 它是本判据
                # 曾经恒绿的那个洞（见 :func:`patched_bridge_dir` 的说明）。
                found.append(
                    "%s:%d: %s 里的 %s —— %s"
                    % (
                        filename, node.lineno,
                        scope_label(class_node, function), callee, pinned,
                    )
                )
                reasons = None
                break
            reasons.append(pinned)
        if reasons is None:
            continue
        if any(sets_bridge_config_env(scope) for scope in scopes):
            reasons.append("设了 OPENCODE_BRIDGE_CONFIG")
        if is_direct:
            argument = bridge_dir_argument(node)
            if argument is not None and not any(
                marker in argument for marker in UNINJECTED_DIR_MARKERS
            ):
                reasons.append("目录是注入进来的（%s）" % argument)

        if reasons:
            continue
        found.append(
            "%s:%d: %s 里的 %s —— 所在函数/类里既没把 _bridge_dir 钉到临时目录、"
            "也没设 OPENCODE_BRIDGE_CONFIG%s"
            % (
                filename,
                node.lineno,
                scope_label(class_node, function),
                callee,
                "，目录实参也不是注入的" if is_direct else "",
            )
        )
    return found