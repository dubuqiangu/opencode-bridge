"""配置就绪判定域（从 ``__main__.py`` 逐字移植）。

回答三类问题：某份配置此刻配好了没有（``_readiness_verdict`` /
``_missing_required_keys`` / ``_not_ready_reasons``）、``--status`` / ``--setup``
该列哪些平台（``_status_platform_keys``）、以及「桥此刻会加载哪份配置文件 /
有没有任何已配置的适配器」（``_config_file_in_use`` / ``_has_configured_adapter``）。

Provenance: 2026-10-12 §5 ``__main__.py`` 专轮（装配 vs 视图渲染轴向，exp-68 裁定）。逐字移植 + ``__main__`` 顶部再进口，行为零变化。
"""
from __future__ import annotations

import os

from .config import Config, DEFAULT_CONFIG_NAME
from .core import setup_platforms


def _token_present(entry: dict, key: str) -> bool:
    return bool(str(entry.get(key) or "").strip())


def _readiness_verdict(cls: object | None, entry: dict) -> bool | None:
    """「用户这份配置此刻配好了没有」的答案。``None`` = 由通用规则回答。

    ⚠️ **三处判定（preflight / ``--setup --json`` / ``--status``）必须问同一个函数。**
    此前它们各自读一遍 ``getattr(cls, "config_optional", False)`` —— 也就是说
    "某个平台的可省略配置"这个前提被**三份**代码各自无条件信任，改一处另外两处
    继续说同一个（过期的）谎。本函数是**唯一**那个入口。

    返回值三态，含义各不相同，别把 ``None`` 当成 ``False``：

    * ``True`` / ``False`` —— 该平台**自己**作答（``config_optional = True``：
      「我没有凭据可填，'够不够跑'只能我说」），答案来自
      :meth:`~opencode_bridge.adapters.base.Adapter.config_runnable`。
      ⇒ 它是**权威**答案，**不再**落回 ``required_tokens`` 那条通用规则。
    * ``None`` —— 该平台走通用规则：「``required_tokens`` 的键逐个非空」
      （多数平台）。此时 ``cls`` 甚至可能是 ``None``（未注册的适配器键）。

    ⛔ 消费方**只**调用 :meth:`~opencode_bridge.adapters.base.Adapter.config_runnable`，
    绝不能写 ``bool(getattr(cls, "config_runnable", False))`` —— 那样拿到的是类上
    绑定的函数对象（永远真值）⇒ 每个平台都会被判成"能跑"。

    :param cls: :func:`~opencode_bridge.adapters.adapter_class` 的结果（可为 ``None``）。
    :param entry: ``cfg.adapters`` 里该平台的**原始条目**（三条路径都先归一成 dict）。
    """
    if not bool(getattr(cls, "config_optional", False)):
        return None
    judge = getattr(cls, "config_runnable", None)
    # ⛔ 刻意不写 ``bool(getattr(...))``（见上面）：非可调用 ⇒ 失败关闭（"没配好"），
    # 而不是把一个布尔属性/方法对象当成答案。
    if not callable(judge):
        return False
    answered = judge(entry)
    # ⚠️⚠️ **``None`` 必须表示「我不自答」，不是「没配好」**（实测确认的潜在缺陷）：
    # 本函数自己的 docstring 写着「别把 ``None`` 当成 ``False``」，
    # 而一句 ``bool(...)`` 恰恰把它压成了 ``False`` ⇒ 一个「自答但此刻没意见」的
    # 平台**无法表达**这件事，只能撒谎说「没配好」、再也回不到通用规则。
    # ⚠️ 现有平台无人返回 ``None``（基类默认返 ``False``）⇒ **今天零行为变化**，
    # 这条是**给下一个覆写者**留的路；且对任何非 ``None`` 的返回值行为逐字不变。
    return None if answered is None else bool(answered)


def _missing_required_keys(cls: object | None, entry: dict) -> list[str]:
    """状态视图说的「还差哪些键」。空列表 = 配好了。

    ⚠️ **与 :func:`_readiness_verdict` 同源**：平台自己作答时它的答案**权威** ——
    否则 ``bind_port: "nope"`` 这种「键非空、但起不来」的配置会被通用规则重新
    判成"配好了"（那正是本缺陷的形态：一个说不过期的前提被无条件信任）。

    :func:`_has_configured_adapter` **不走**本函数：它对未注册的适配器键另有一条
    分支（"有像凭据的字段就别凭空拦住用户"），且 ``required_tokens`` 缺省值不同
    （``()`` vs ``("bot_token",)``）—— 那条路的形状照旧，不在这里合并。
    """
    required = tuple(getattr(cls, "required_tokens", ("bot_token",)) or ("bot_token",))
    verdict = _readiness_verdict(cls, entry)
    if verdict is True:
        return []
    if verdict is False:
        # 自家说"起不来"，而它要的正是这些键 —— 报"缺哪个"才有意义
        #（a2a 就是 ``["bind_port"]``，也是 ``docs/a2a.md`` 承诺的那条提示）。
        return list(required)
    return [key for key in required if not _token_present(entry, key)]


#: ``not_ready_reasons`` 的取值。**稳定 token**，不是给人看的话 —— 人看的是
#: ``detail`` 字段。理由码单独成常量，是为了让消费方（含测试）能按值断言，
#: 而不是去匹配会改字的提示语。
_NOT_READY_MISSING_CREDENTIALS = "missing_credentials"
_NOT_READY_NO_ALLOWLIST = "no_allowlist"


def _not_ready_reasons(*, configured: bool, has_allowlist: bool) -> list[str]:
    """这个平台能不能对用户说"配好了"。空列表 = 可以说。

    ⚠️ **判据里的关键一条：没配凭据的平台**不**报 ``no_allowlist``。**
    它收不到任何消息，"没授权任何人"对它是假话；只报 ``missing_credentials``。

    ⚠️ **``has_allowlist`` 就是全部判据了** —— 此前还有一个 ``accepts_any_sender``
    与它取合取，而翻转之后那个字段恒为 False（见
    :attr:`~opencode_bridge.allowlist.AllowlistResolution.admits_nobody` 的注释），
    于是合取项**冗余**。留着它只会让人以为"两个条件都得满足"，而去查它为什么总是
    成立 —— 一个恒真的合取项是**噪音**，不是保险。

    ⚠️ 本函数**跨两个配置版本都成立**，这正是 :data:`_NOT_READY_NO_ALLOWLIST` 作为
    **稳定 token** 的意义（见该常量注释）：空清单在旧语义下是"全开"（真的不能
    说配好了），在新语义下是"全拒"（**同样**不能说配好了）—— 结论一致，token 零断裂。
    """
    reasons: list[str] = []
    if not configured:
        reasons.append(_NOT_READY_MISSING_CREDENTIALS)
    elif not has_allowlist:
        reasons.append(_NOT_READY_NO_ALLOWLIST)
    return reasons


def _status_platform_keys() -> list[str]:
    """状态视图要列的平台：优先取注册表里全部已注册平台。

    冻结的 ``/setup`` 菜单刻意只列三平台（人工维护的引导文案），但 ``--status``
    与 ``--setup --json`` 是运行时视图 —— 新加的平台若不在这里出现，用户就根本
    看不到它。注册表读不到时退回菜单里的三家。
    """
    try:
        from .adapters import registered_names

        names = list(registered_names())
    except Exception:  # noqa: BLE001 - 状态视图不该崩
        names = []
    if not names:
        names = [key for key, _ in setup_platforms()]
    return names


def _config_file_in_use(cfg: Config | None = None) -> str:
    """Absolute path of the config the bridge would load right now.

    ⚠️ **优先读 ``cfg.source_path``**（实测缺陷，2026-10-06）：
    原来这个函数**自己重写了一遍搜索链**，只查环境变量与 cwd ——
    于是 ``--config /tmp/other.json --setup --json`` 明明用 ``--config`` 加载了，
    报出来的却是**另一个文件**。根因是「解析出的路径从未被记录」，
    已由 :meth:`Config.load` 记在实例上（``source_path``）。
    下面的兜底分支保留：``cfg`` 没传、或一个文件都没加载成时仍要给出「会去哪儿找」。

    参数可选 ⇒ 既有调用方与测试不必改。
    """
    if cfg is not None and getattr(cfg, "source_path", ""):
        return cfg.source_path
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DEFAULT_CONFIG_NAME)


def _has_configured_adapter(cfg: Config) -> bool:
    """Cheap pre-flight: is any adapter in the config actually usable?

    由各适配器**声明的** ``required_tokens`` 判定，而不是硬编码 ``bot_token``。
    硬编码会让"只配了 Matrix / IRC / Mattermost"的用户被判定成没配任何适配器，
    桥接直接拒绝启动 —— 这三个平台根本没有 ``bot_token`` 这个键。

    仍然刻意简单，好在 endpoint discovery 之前就给出有用的提示。

    ⚠️ **⛔ 不许问「有没有哪个 ``config_optional`` 平台」，要问「这份配置此刻够不够
    跑」。** 那是本函数此前的一个真缺陷：``a2a`` 声明 ``config_optional = True``
    （分类：我没有凭据可填），而**这个分类**曾被当成"空配置即可运行"的**判定**无条件
    信任 —— 那条前提早已不成立：``_coerce_port("")`` 是 ``UNCONFIGURED_PORT``（-1），
    ``A2aAdapter.start()`` 据此打 error 并**不绑定就 return**。⇒ 于是模板里那行
    ``"a2a": {"bind_port": "", …}`` 就能让全新安装的桥**跳过本函数**、不再走
    :data:`NO_ADAPTER_MESSAGE` 那条提前退出。

    现在分两种问法（见 :func:`_readiness_verdict`）：平台自己作答的走
    :meth:`~opencode_bridge.adapters.base.Adapter.config_runnable`
    （答案**权威**，不落回 ``required_tokens``）；其余走 ``required_tokens`` 通用规则
    —— 后者一个字没改，那正是「Matrix/IRC/Mattermost 不能被拒启动」的守卫。
    """
    entries = cfg.adapters or {}
    if not isinstance(entries, dict) or not entries:
        return False

    from .adapters import adapter_class

    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        cls = adapter_class(str(name))
        verdict = _readiness_verdict(cls, entry)
        if verdict is not None:
            return verdict
        required = tuple(getattr(cls, "required_tokens", ()) or ())
        if not required:
            # 未知/未注册的适配器：只要有像凭据的字段就别凭空拦住用户
            # （真正能不能用由后面的 build() 报明确的错）。
            if any(
                str(v).strip() for k, v in entry.items() if k != "allowed_chat_ids"
            ):
                return True
            continue
        if all(_token_present(entry, key) for key in required):
            return True
    return False
