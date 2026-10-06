"""Lane C — CLI entry point: ``python -m opencode_bridge``.

Usage::

    python -m opencode_bridge [--config PATH] [--verbose] [--check]
    python -m opencode_bridge --setup [平台] [--json]

* ``--check`` only resolves the endpoint and calls ``GET /api/info`` — it
  never creates a session and never starts an adapter.
* ``--setup`` prints the frozen per-platform onboarding copy and exits: it
  contacts nothing and needs no ``bot_token``, so it also works *before* the
  bridge is configured. It is the single source of truth behind both the
  in-bot ``/setup`` command and the ``bridge_setup`` plugin tool inside OpenCode.
* The normal run builds every configured adapter, starts the SSE reader and
  blocks until ``Ctrl+C``; shutdown always goes through ``BridgeCore.stop()``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Mapping, Sequence

from .adapters import build
from .allowlist import resolve_allowlist
from .config import Config, DEFAULT_CONFIG_NAME, adapter_scoped_config
from .core import BridgeCore, setup_platforms, setup_reply
from .diagnostics import ProcessDiagnostics, describe_environment
from . import health
from .instance_lock import InstanceLock, pid_is_alive
from .inbox import InboundInbox
from .opencode_client import OpenCodeClient, discover_endpoint
from .pairing import empty_allowlist_is_open
from .pairing_cli import run_pair
from .redaction import install_redaction_filter
from .state import StateStore

__all__ = ["main"]

logger = logging.getLogger("opencode_bridge")

#: 已有实例在跑时的提示。**必须说清三件事**：谁占着（pid）、为什么不能并存
#: （同一 bot 的收消息接口只能有一个消费者）、以及怎么解决（停掉那个）。
#: 只说"已有实例在运行"会让人以为是崩溃或配置错误。
ALREADY_RUNNING_MESSAGE = (
    "已有另一个 bridge 实例在运行（pid={pid}），本次不启动。\n"
    "同一个 bot 的收消息接口同时只允许一个消费者：两个实例并存会导致\n"
    "  · 消息随机丢失（谁抢到算谁的）\n"
    "  · 同一条消息被处理两次，用户收到两份一样的回复\n"
    "如果你要调试，请先停掉那个实例（或停用 opencode 的 bridge 插件），\n"
    "不要让两个同时轮询。"
)

#: 未配置任何可用适配器时的提示。**不能**只说 ``bot_token``：Matrix / IRC /
#: Mattermost 根本没有这个键，只提它会让那三类用户以为自己配错了。
NO_ADAPTER_MESSAGE = (
    "没有任何可用适配器：请在 config.json 的 adapters 里配置对应平台的凭据"
    "（Telegram/Slack/Discord 用 bot_token，Slack 入站另需 app_token，"
    "Matrix 用 homeserver/access_token/user_id，IRC 用 host/nick/channels，"
    "Mattermost 用 site_url/token）"
)


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # 脱敏过滤器必须挂在 **basicConfig 之后**：它装在 handler 上，而 handler 是
    # basicConfig 建的。全仓库 34 个 logger 都是 ``opencode_bridge.*`` 的后代、
    # 记录一律 propagate 到 root，所以这一行就覆盖了全部 13 个适配器以及将来
    # 新增的任何一个 —— 而**没有任何一个调用点**需要改。
    install_redaction_filter()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencode_bridge",
        description=(
            "把 Telegram/Slack/Discord 消息桥接到本机 opencode 服务。"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="配置文件路径（默认：环境变量 OPENCODE_BRIDGE_CONFIG 或 ./config.json）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="输出 DEBUG 级别日志"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做与 opencode 服务的连通性自检，然后退出",
    )
    parser.add_argument(
        "--setup",
        nargs="?",
        const="",
        metavar="平台",
        default=None,
        help=(
            "打印接入引导后退出（telegram|slack|discord 或 1|2|3；不带参数则打印平台菜单）。"
            "不需要连接 opencode，也不需要配置 token"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "与 --setup 搭配：输出机器可读 JSON（配置路径 + 各平台是否已配 token"
            " + 授权白名单状态 + 是否谁都能驱动）"
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="汇总服务连通性 / 各平台配置与能力 / bridge 运行态证据，然后退出",
    )
    parser.add_argument(
        "--pair",
        metavar="配对码",
        help=(
            "把 bot 里 /pair 给出的那串码兑换成 allowed_chat_ids 里的一项，"
            "然后退出（要重启桥接才生效）"
        ),
    )
    parser.add_argument(
        "--conversation",
        metavar="platform:local_id",
        help=(
            "配合 --pair 使用：要授权哪个会话（形如 telegram:12345，"
            "就是 /pair 回信里那个值）。码绑定会话，只凭码无法确定是哪个。"
        ),
    )
    return parser


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


def _platform_status(cfg: Config) -> list[dict[str, object]]:
    """Per-platform 配置状态 for ``--setup --json``（数据驱动，不硬编码平台表）。

    ``configured`` 要求该平台 ``required_tokens`` **全部**齐备。Slack 缺
    ``app_token`` 时"只发出站"仍可用，但入站根本没通 —— 这种情况必须能被机器
    读出来，只看 ``bot_token`` 会把"入站没通"误报成已配置。

    ⚠️ **本函数不新增"已配好"的含义，只新增独立的授权暴露面字段。**
    ``configured`` 的原义（凭据齐备）一个字没改，否则每个既有消费者都得重新学一遍
    这套输出。真正要挡的坑是：**凭据齐了 + 空=全开** 曾被报成"配置好了"，而
    ``bridge_setup`` 工具会照着这句话告诉用户"配好了"。所以另给一个
    :attr:`_NOT_READY_NO_ALLOWLIST` 级别的诚实判定 :func:`_not_ready_reasons`。

    ⛔ **同一条纪律也适用于 :func:`health` 带来的 ``last_start_probe``：只增不改。**
    现有 key（``admits_nobody`` / ``admits_any_sender`` / ``not_ready_reasons`` …）
    一个都不许删、不许改名 —— 仓库外的消费者（``bridge_setup``）按**值**断言它们，
    而我们读不到它的源码。任何"顺手清理"都可能打掉别人的判据。
    """
    from .adapters import adapter_class

    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    labels = dict(setup_platforms())
    #: 「上次启动时」的探测结论（:mod:`opencode_bridge.health`）。⛔ **纯本地读盘**：
    #: 本函数不做任何网络请求 —— 把 ``getMe`` 之类塞进来会让 ``--setup --json``
    #: 在网络被墙时也发不出那条最该看的错误信息（且 ``run_check`` 的 docstring
    #: 明写「no sessions, **no adapters**」）。读一次就够，**不逐平台重读**。
    probe_record = health.read_platform_health(_bridge_dir())
    out: list[dict[str, object]] = []
    for key in _status_platform_keys():
        cls = adapter_class(key)
        entry = entries.get(key) if isinstance(entries.get(key), dict) else {}
        outbound = tuple(getattr(cls, "outbound_tokens", ("bot_token",)) or ("bot_token",))
        # 「这份配置此刻配好了没有」问**唯一**那个判定入口（见 :func:`_readiness_verdict`）。
        # ⚠️ **不再**问 ``config_optional`` 那个静态声明：它记录的是"曾经为真"的事实
        # —— 注释曾断言"a2a 空配置即可运行（端口由系统分配）"，而 ``_coerce_port("")``
        # 给的是 ``UNCONFIGURED_PORT``（-1）、``A2aAdapter.start()`` 据此打 error
        # **不绑定就 return** ⇒ 那条前提早就不成立了。
        verdict = _readiness_verdict(cls, entry)
        missing = _missing_required_keys(cls, entry)
        supports_inbound = bool(getattr(cls, "supports_inbound", False))
        configured = not missing
        # 授权面与闸门读**同一个**解析函数（``allowlist.resolve_allowlist``）——
        # 状态视图说"有限白名单"而闸门放行一切，比没有状态视图更坏。
        allowlist = resolve_allowlist(entry)
        not_ready = _not_ready_reasons(
            configured=configured,
            has_allowlist=bool(allowlist.entries),
        )
        out.append(
            {
                "key": key,
                "label": str(getattr(cls, "label", "") or labels.get(key) or key),
                # 全部必需 token 齐备才算配好（入站必需项也算在里面）
                "configured": configured,
                # 出站凭据各平台不同（Matrix 用 homeserver/access_token、IRC 用
                # host/nick…），必须由适配器声明，不能硬编码 bot_token。
                # 平台自己作答时（``verdict`` 非 None）它的答案是权威的 ——
                # 与 :attr:`configured` 同一个来源，两列不会互相矛盾。
                "outbound_ready": verdict if verdict is not None
                else all(_token_present(entry, k) for k in outbound),
                # 入站要"能力已实现"且"配置齐备"两个条件同时成立
                "inbound_ready": supports_inbound and not missing,
                "inbound_implemented": supports_inbound,
                "missing": missing,
                # --- 授权暴露面（新增；configured 的含义不变）----------------
                # 解析出的白名单条目数。0 = 空 = **闸门不放行任何人**
                # （新语义）或 **放行所有人**（旧语义）—— 见下面两个字段。
                "allowed_chat_ids_count": len(allowlist.entries),
                # 有没有真的限人。读它来回答"别人能不能开我的 bot"。
                "allowlist_configured": bool(allowlist.entries),
                # ⛔ **键已改名**：``accepts_any_sender`` → ``admits_nobody``。
                # **不删键** —— 删了就逼消费者从 ``count == 0`` 反推策略，
                # 那正是 :mod:`opencode_bridge.allowlist` 当初被拆出来要消灭的事。
                # 新名下的判定式一个字没动，变的只是"空清单"指向哪一边。
                "admits_nobody": allowlist.admits_nobody,
                # 这个配置下闸门**是否真的会放行一切**。消费者要答"别人能不能开我的
                # bot"就读它 —— 它把 :attr:`admits_nobody` 与配置版本合成一个答案，
                # 于是它在新旧两种语义下都是**同一件事**：闸门此刻的实际行为。
                "admits_any_sender": allowlist.gate_admits_everyone(
                    cfg.config_version
                ),
                # 配置里出现过的授权键名（可能多于一个 → 见 allowlist_conflict）
                "allowlist_keys_present": list(allowlist.present_keys),
                # 多个授权键解析出不同结果时的诊断（含实际生效的键与项数）；
                # 无冲突为 None。两个键值相同**不算**冲突 —— 结果毫无歧义。
                "allowlist_conflict": (
                    allowlist.conflict.as_dict() if allowlist.conflict else None
                ),
                # 能不能对用户说"这个平台配好了"。空 = []。
                "not_ready_reasons": not_ready,
                "ready_for_agent": not not_ready,
                # --- 适配器自己声明的运行判据 ---------------------------------
                # ⚠️ **为什么必须在这儿透出 `capabilities()`**（实测缺陷，2026-10-06）：
                # 有些平台「凭据齐备」**不等于**「能收到东西」——
                # homeassistant 默认**一个事件都不收**（`capabilities()` 里的
                # `inbound_accepts_anything=False` 就是那个明确信号）。
                # `homeassistant.py` 的 docstring 写着「`--setup --json` /
                # `--status` 的 JSON 输出能直接读到」，而**本函数原来根本没调它**
                # ⇒ 文档承诺的判据任何命令都读不到。
                #
                # 只在**已配置**时构造适配器：未配置时构造只会抛，而「配齐了却收不到」
                # 正是我们要暴露的那个情形。`_NullHooks` 与构造方式沿用
                # `_channel_config_rows`，⛔ 不新造第二套。
                "capabilities": (
                    _adapter_capabilities(key, cfg, entry) if configured else None
                ),
                # --- 「上次启动」时的探测结论（新增；configured 的含义不变）----
                # ⚠️ **它回答的不是"现在能不能用"**：``configured`` / ``inbound_ready``
                # 只看凭据齐不齐，而"token 到底有效吗"只有平台能回答。桥启动时
                # 已经问过一次（Telegram 的 ``getMe``），结论落在这里（见
                # :mod:`opencode_bridge.health`），本函数只**读盘**、不联网。
                #
                # ⛔ **``None`` 的含义是「没有记录」，不是「没问题」**：桥还没以
                # 当前配置启动过、或从没启动过，两者都是 ``None``。把它显示成
                # "ok"就是把"没验"说成"验过了"。
                #
                # ⚠️ **取值域多了一档 :data:`health.VERDICT_NOT_STARTED`**（``not_started``）：
                # 它答的是「**桥这一轮压根没起来**」，不是「这个平台起不来」——
                # 拒绝启动的两条路径（见 :func:`_record_bridge_refusal`）现在也落
                # 这样的记录，所以消费方读它就知道**别**把它当成平台级线索去
                # 建议用户改凭据。⛔ 既有三档（``ok`` / ``failed`` / ``skipped``）
                # **一个字没改**：本函数对 ``platforms`` 的其它 key 也一样，只增不改。
                "last_start_probe": health.probe_from_record(probe_record, key),
            }
        )
    return out


def run_setup(cfg: Config, platform: str, as_json: bool) -> int:
    """Print the frozen onboarding copy. Never contacts opencode."""
    if as_json:
        payload = {
            "config_path": _config_file_in_use(cfg),
            "platforms": _platform_status(cfg),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(setup_reply(platform))
    return 0


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


def _bridge_dir() -> str:
    """手动运行时 cwd 通常就是 bridge 目录；``OPENCODE_BRIDGE_CONFIG`` 指向别处时用其目录。"""
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.dirname(os.path.abspath(env_path)) or os.getcwd()
    return os.getcwd()


#: 桥**拒绝启动**的两条路径。⚠️ 它们都**必须**落一条
#: :data:`~opencode_bridge.health.VERDICT_NOT_STARTED` 的结论 —— 不落的话，盘上留下的
#: 就是上一次**成功启动**的那条 ``ok``（内容与 mtime 都不变），而 ``--status`` 会
#: 把它与「未配置」并排显示 ⇒ 用户刚把配置改坏时，看到的仍是上一轮的好消息。
#: ``_has_configured_adapter`` 为否的那条在 ``discover_endpoint`` **之前**，
#: ``usable == 0`` 的那条在它**之后**、``core.start()`` **之前**。
REFUSAL_STAGE_PREFLIGHT = "preflight_no_configured_adapter"
REFUSAL_STAGE_NO_USABLE_ADAPTER = "no_usable_adapter"

#: 每条拒绝路径的**一句话原因**（全局的）。⚠️ 只说"是什么"；具体"哪些平台缺什么"
#: 由 :func:`_record_bridge_refusal` 逐平台算出来交给
#: :func:`~opencode_bridge.health.bridge_refusal_probes` 拼在后面。
_REFUSAL_REASON_BY_STAGE = {
    REFUSAL_STAGE_PREFLIGHT: "预检未通过：配置里没有任何适配器此刻够跑",
    REFUSAL_STAGE_NO_USABLE_ADAPTER: "没有任何适配器构造成功，桥因此没起来",
}


def _record_bridge_refusal(
    cfg: Config,
    stage: str,
    platform_reasons: Mapping[str, str] | None = None,
) -> None:
    """把「桥拒绝启动」这条结论写进**同一份** ``platform-health.json``。

    ⚠️ **为什么必须写**：不写的话，这条路上盘上留下的就是上一次成功启动的结论，
    而那正是用户最需要线索的时刻（他刚把配置改坏了）——状态视图在那一刻显示上一轮
    的好消息，等于在最关键的时候说假话。

    ⚠️ **落盘只有 :func:`~opencode_bridge.health.record_startup_probes` 那一个入口**：
    本函数只**造**出 ``{平台键: 结论}``（由
    :func:`~opencode_bridge.health.bridge_refusal_probes` 按
    :data:`~opencode_bridge.health.VERDICT_NOT_STARTED` 成形），不自己写文件。

    ⚠️ **保护必须包住「实参求值」，不只是那次调用** —— 与
    :func:`_run_bridge_locked` 里那次调用是**同一条**纪律、**同一个**理由：
    ``adapter_class`` 的 import 与 :func:`_missing_required_keys` 的逐平台判定都在
    这个 ``try`` 里面求值，它们抛了的话，这条**排障辅助**通路就有权把
    「拒绝启动 + 一条明明白白的提示」变成「拒绝启动 + 一个堆栈」。
    ⇒ 退化行为是「记一条 warning + 不写记录」，**不是**崩。

    ⚠️ 预检那条路**在 ``discover_endpoint`` 之前**，判断不依赖网络 ⇒ opencode 服务
    不可达也照样写。

    :param stage: :data:`REFUSAL_STAGE_PREFLIGHT` / :data:`REFUSAL_STAGE_NO_USABLE_ADAPTER`。
    :param platform_reasons: 只有 ``usable == 0`` 那条路需要 —— 「为什么构造不出来」
        只有那个构造循环知道（未注册 / ``build()`` 抛错 / ``bot_token`` 为空）。
    """
    try:
        reasons = {str(key): str(text) for key, text in (platform_reasons or {}).items()}
        if not reasons:
            # 预检那条路没有「循环」可问：缺什么只能由缺失键清单回答，而它走的是
            # **唯一**那个判定入口（``--status`` 的「配置」列也是用它）。
            from .adapters import adapter_class

            for key, entry in (cfg.adapters or {}).items():
                if not isinstance(entry, dict):
                    continue
                missing = _missing_required_keys(adapter_class(str(key)), entry)
                reasons[str(key)] = (
                    "缺 " + "、".join(missing) if missing else "未配置"
                )
        detail = _REFUSAL_REASON_BY_STAGE.get(stage, "桥拒绝启动")
        probes = health.bridge_refusal_probes(detail, reasons)
        written = health.record_startup_probes(_bridge_dir(), probes)
        # ⚠️ **别在写盘失败时还说"已记"** —— 那是把一条失败的排障记录报成成功的，
        # 与本模块「没有记录 ≠ 成功」是同一条纪律（失败时那条 warning 由
        # record_startup_probes 自己打）。
        logger.warning(
            "platform-health: %s —— %s",
            detail,
            "已记「桥未启动」" if written else "未落盘",
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "platform-health: 「拒绝启动」这条结论未落盘（%s: %s）—— 不影响桥的退出",
            type(exc).__name__,
            exc,
        )


def _pid_alive(pid: int) -> bool:
    """进程是否存活。委托给 :func:`instance_lock.pid_is_alive`，避免两处实现。"""
    return pid_is_alive(pid)


def _inbox_path(cfg: Config) -> str:
    """写前收件箱的落盘位置：与 ``state.json`` **同一个目录**，文件名 ``inbox.db``。

    跟着 ``cfg.state_path`` 走而不是死用 :func:`_bridge_dir`：用户把
    ``state_path`` 指到别处时，待投递的行必须跟着走 —— 否则状态与"还没兑现的
    处理义务"分居两处，备份与清理都得记两遍，而漏掉哪一处都不会有人发现。
    ``state_path`` 是裸文件名（默认就是 ``state.json``，相对当前目录）时，
    它的目录恰好就是 :func:`_bridge_dir`。
    """
    return os.path.join(
        os.path.dirname(os.path.abspath(cfg.state_path)) or _bridge_dir(),
        "inbox.db",
    )


class _NullHooks:
    """只为读取适配器能力快照而存在：不消费任何事件。"""

    def on_inbound(self, inbound: object) -> None:  # pragma: no cover - 空实现
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:  # pragma: no cover
        return None


def _adapter_capabilities(key: str, cfg: Config, entry: dict) -> dict:
    """构造适配器并读它的 ``capabilities()``。

    ⚠️ **这是 ``_channel_config_rows`` 里那段构造逻辑的提取，不是第二份实现** ——
    投影（:func:`adapter_scoped_config`）、hooks、错误兜底三处都与它一致，
    改动面才只在一个文件里（§5）。实参形态也逐字照抄：``build(key, …)``
    （第一参是**平台键字符串**，不是类 —— `build` 的签名是 ``(name, config, hooks)``）。

    为什么 ``--setup --json`` 需要它：有些平台「凭据齐备」**不等于**「能收到东西」。
    homeassistant 默认**一个事件都不收**，``inbound_accepts_anything=False`` 才是那个
    「配好了但收不到」的明确信号 —— 而 homeassistant 的 docstring 明写这个信号要能从
    ``--setup --json`` 读到，**原来却没有任何命令输出它**（实测，2026-10-06）。
    """
    try:
        # 与 run_bridge 走**同一个**投影，否则状态视图会在一个真实运行着的
        # 适配器上读出另一套语义（它也构造适配器，见 :class:`_NullHooks` 的说明）。
        return dict(
            build(key, adapter_scoped_config(cfg, entry), _NullHooks()).capabilities()
        )
    except Exception as exc:  # 能力读取失败不该让 --status / --setup --json 崩
        return {"error": str(exc)[:80]}


def _channel_config_rows(cfg: Config) -> list[tuple[str, str, bool, bool, dict]]:
    """``(key, label, configured, inbound_ready, capabilities)``。

    能力来自各适配器的显式声明（T1.1），必需 token 来自 ``required_tokens``，
    平台清单来自注册表 —— 三者都不在这里硬编码。

    ``capabilities`` 里会**额外并入**授权面的几个键（键名是否出现、是否冲突）。
    为什么不在 :meth:`opencode_bridge.adapters.base.Adapter.capabilities` 里加：
    那个快照的键集合被 ``test_adapters`` 逐个钉死，而这里是**状态视图自己的**
    组装处 —— 派生事实并进状态行，不必去动适配器的公共契约。
    """
    from .adapters import adapter_class, build

    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    rows: list[tuple[str, str, bool, bool, dict]] = []
    for key in _status_platform_keys():
        cls = adapter_class(key)
        raw = entries.get(key)
        entry = raw if isinstance(raw, dict) else {}
        # 同 _platform_status / _has_configured_adapter：问**唯一**那个判定入口。
        # 少改这一处的代价是它继续说"a2a 空配置已配置"，而 `--status` 的
        # 「配置 / 入站」两列正是用户决定填不填 bind_port 的依据。
        missing = _missing_required_keys(cls, entry)
        caps: dict = {}
        try:
            # 与 run_bridge 走**同一个**投影，否则状态视图会在一个真实运行着的
            # 适配器上读出另一套语义（它也构造适配器，见 :class:`_NullHooks` 的说明）。
            caps = dict(
                build(key, adapter_scoped_config(cfg, entry), _NullHooks()).capabilities()
            )
        except Exception as exc:  # 能力读取失败不该让 --status 崩
            caps = {"error": str(exc)[:80]}
        # 授权面与闸门同源：同一个 resolve_allowlist，不另写一份解析。
        allowlist = resolve_allowlist(entry)
        caps["allowlist_keys_present"] = list(allowlist.present_keys)
        caps["allowlist_conflict"] = (
            allowlist.conflict.as_dict() if allowlist.conflict else None
        )
        label = str(caps.get("label") or getattr(cls, "label", "") or key)
        supports_inbound = bool(getattr(cls, "supports_inbound", False))
        rows.append((key, label, not missing, supports_inbound and not missing, caps))
    return rows


def _runtime_state(bridge_dir: str) -> tuple[str, str]:
    """从锁文件推断 bridge 运行态，返回 ``(state, detail)``。

    只用**可验证的证据**：锁文件 + pid 存活 + failedAt。
    ``--status`` 是独立进程，看不到 bridge 进程内状态，因此这里刻意保守。
    """
    from .status import ChannelState

    lock_path = os.path.join(bridge_dir, ".bridge-plugin.lock")
    if not os.path.isfile(lock_path):
        return ChannelState.DISABLED.value, "无锁文件：bridge 未在运行"
    try:
        with open(lock_path, "r", encoding="utf-8-sig") as fh:
            lock = json.load(fh)
    except Exception as exc:
        return ChannelState.ERROR.value, f"锁文件不可读（{exc}）"
    pid = int(lock.get("pid") or 0)
    failed_at = lock.get("failedAt")
    if pid and _pid_alive(pid):
        return ChannelState.CONNECTED.value, f"bridge 运行中 (pid={pid})"
    if isinstance(failed_at, (int, float)):
        wait = max(0, int(300 - (time.time() - failed_at)))
        return (
            ChannelState.ERROR.value,
            f"上次快速失败于 {time.strftime('%H:%M:%S', time.localtime(failed_at))}，"
            f"backoff 中（约剩 {wait}s）",
        )
    return ChannelState.DEGRADED.value, f"锁存在但 pid={pid} 已不存在（上次异常退出）"


def _dwidth(text: str) -> int:
    """终端**显示宽度**：CJK / 全角字符占 2 列（用 ``unicodedata`` 判定）。

    ``f"{s:<n}"`` 是按**字符数**补齐的，中文表头（"平台" 2 字却占 4 列）会让整张
    表在终端里错位；平台名变长（如 "Nextcloud Talk" 14 字符）时更会挤掉与下一列
    之间的空格。两者都要按显示宽度算才对齐。
    """
    import unicodedata

    return sum(
        2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(0, width - _dwidth(text))


#: 「上次启动时没有探测记录」时的**逐字**文案。
#: ⛔ 它必须既不像"正常"也不像"失败"：那是"**没验过**"，不是"验过了、没问题"。
#: 措辞刻意把两种可能都说出来（"尚未以当前配置启动过" / "从未启动过"），
#: 因为盘上没有记录时**分不出是哪一种** —— 而猜一个就是在编造（AGENTS.md §8）。
NO_START_PROBE_TEXT = "无记录 —— 桥尚未以当前配置启动过，或从未启动过"


def _print_last_start_probes(
    rows: list[tuple[str, str, bool, bool, dict]],
    bridge_dir: str,
) -> None:
    """打印「上次启动那一刻，各平台探测出了什么」。

    ⚠️ 这一段与上面那张表**回答的不是同一个问题**，所以必须分开说：

    * 「渠道配置与能力」表答"凭据齐不齐" —— 纯本地可判（它看的是
      ``required_tokens`` 全部非空）；
    * 这一段答"上次启动时平台**认不认**这个凭据" —— 只有平台能判（Telegram 的
      ``getMe``、Slack 的 ``auth.test``）。

    两者分开是因为**失效方式不同**：token 打错 / 被吊销 / 网络被墙时，那张表仍会
    显示「已配置 / 就绪」，而桥实际上一条消息都收不到 —— 这正是本段存在的理由。

    ⚠️ **纯本地读盘**：不联网、不构造适配器（``--status`` 必须是"网络坏了也能看"
    的那条路）。结论由 :mod:`opencode_bridge.health` 在启动那一刻写好。

    ⚠️ **措辞必须带时效性**：这是「**上次启动时**」的结论，不是实时探测。
    ⚠️ 「启动」指的是**尝试启动**（两条拒绝启动的路也会记一条
    :data:`~opencode_bridge.health.VERDICT_NOT_STARTED`，见 :func:`_record_bridge_refusal`）。
    **唯一的例外**是「已有另一个实例在运行」：那次**不写**记录 —— 覆盖掉运行中那个
    实例写下的好结论，比留着一条旧的更坏。那条例外必须**在下面说出来**，
    否则「上一次启动尝试」这句话就会在这一种情形下说假话。

    :param rows: :func:`_channel_config_rows` 的行，**复用**它而不重算平台清单与
        ``configured``（同一份判定只能有一处）。
    :param bridge_dir: :func:`_bridge_dir` 的推导结果 —— 记录就落在那里。
    """
    record = health.read_platform_health(bridge_dir)
    recorded = health.recorded_at(record)
    # 只列**已配置**或**上次探测过**的平台：把十三个平台各写一行「无记录」既吵
    # 又是废话（没配的平台压根不该被启动过）。而"上次探测过"的那些必须留着 ——
    # 用户把它移出配置之后，正是最需要看见"上次启动时说它是好的"。
    probed = set(health.platforms_in_record(record))
    listed = [
        (key, label)
        for key, label, configured, _inbound_ready, _caps in rows
        if configured or key in probed
    ]
    # 「桥这一轮没起来」是**全局**的一件事（不是某个平台的结论）⇒ 由下面那条 ⚠ 行
    # 说一次，并且**不带平台行**：空模板那种 13 个平台全被拒的情况，逐平台再写一遍
    # 就是一屏复读，而它们各自的「缺什么」上面那张「渠道配置与能力」表已经在说了。
    refused = None
    platform_rows = []
    for key, label in listed:
        probe = health.probe_from_record(record, key)
        if probe is not None and probe["verdict"] == health.VERDICT_NOT_STARTED:
            # ⚠️ 记**第一个**（注册表顺序 = 上面那张表的顺序）：13 个平台各说一遍是
            # 复读，而它们各自的「缺什么」上面那张表已经在说了。
            if refused is None:
                refused = probe
            continue
        platform_rows.append((label, probe))
    print("")
    print("== 上次启动时的探测结论 ==")
    print("  说明：以下结论取自**桥上一次启动尝试那一刻**的结果（落盘在")
    print("        platform-health.json）：桥起来了就写平台探测的答复；桥**拒绝启动**")
    print("        就写「桥未启动」并说明原因，**不会**留下一条上一轮的好消息。")
    print("        仅「已有另一个实例在运行」这一次**不写**记录（那种情况下盘上仍是")
    print("        那个运行中的实例启动时写下的结论）。")
    print("        **不是现在的连接状态，也不是实时探测**。")
    if recorded is not None:
        print(
            "  记录时间 : "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(recorded))
        )
    if refused is not None:
        print("  ⚠ " + health.describe_verdict(refused))
    if not platform_rows:
        # ⛔ 区别「压根没有记录」与「有记录、但里面没有平台级结论」：后者是桥刚
        # 拒绝启动、或它启动了一个都不上报结论的适配器 —— 说成"没有任何探测记录"
        # 会让用户以为盘上是空的，而它其实刚被写过。
        if not listed:
            print(
                "  （没有已配置的平台，也没有任何探测记录）" if record is None
                else "  （盘上有记录，但这次启动没有得到任何平台的结论）"
            )
        return
    name_w = max(_dwidth("平台"), max(_dwidth(label) for label, _p in platform_rows)) + 2
    for label, probe in platform_rows:
        if probe is None:
            print("  " + _pad(label, name_w) + NO_START_PROBE_TEXT)
            continue
        line = "上次启动 " + health.describe_verdict(probe)
        if probe["verdict"] == health.VERDICT_OK and recorded is not None:
            # 只给「正常」配时间戳：它是**唯一**可能被误读成"现在是好的"的那一行。
            line += "（%s）" % time.strftime("%m-%d %H:%M", time.localtime(recorded))
        print("  " + _pad(label, name_w) + line)


def run_status(cfg: Config) -> int:
    """汇总视图：服务连通性 + 各平台配置与能力 + bridge 运行态证据（T1.5）。"""
    bridge_dir = _bridge_dir()
    print("== opencode 服务 ==")
    try:
        endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
        client = OpenCodeClient(endpoint)
        try:
            info = client.info()
        finally:
            client.close()
        print(f"  状态   : OK  version={info.get('version', '?')} pid={info.get('pid', '?')}")
        print(f"  地址   : {endpoint.url}")
    except Exception as exc:
        print(f"  状态   : 不可达 —— {exc}")

    print("")
    print("== 渠道配置与能力 ==")
    print("  说明：「配置」= 必需 token 全部齐备；「入站」= 入站已实现且配置齐备。")
    print("        两者都不代表连接状态（连接状态见下方运行态）")
    # ⚠️ **图例要按配置版本给两套文案** —— 写死一套会让另一个版本的用户读到假话，
    # 而这张表唯一的作用就是"用户读到的 == 闸门的真实行为"。
    if empty_allowlist_is_open(cfg.config_version):
        print("        「白名单」为空 = 未设 = **全开**：任何能联系到 bot 的人都能驱动它")
        print("        （你的配置没有 config_version 或 < 2 ⇒ 沿用旧的「空 = 全开」。")
        print("          下一版起此处改为「空 = 全拒」；届时在 bot 内发 /pair 一步授权，")
        print("          不必手改本文件。）")
    else:
        print("        「白名单」为空 = 未设 = **全拒**：任何人都进不来（最安全的状态）。")
        print("        （你的配置是 config_version >= 2。在 bot 内发 /pair 即可授权那个会话。）")
    rows = _channel_config_rows(cfg)
    # 列宽按**显示宽度**自适应：平台名长短不一（"IRC" 3 字符、"Nextcloud Talk" 14），
    # 写死宽度会让长名字挤掉与下一列之间的空格。
    name_w = max([_dwidth("平台")] + [_dwidth(r[1]) for r in rows]) + 2
    print(
        "  " + _pad("平台", name_w) + _pad("配置", 8) + _pad("入站", 8)
        + _pad("按钮", 6) + _pad("媒体", 6) + _pad("长度上限", 12) + "白名单"
    )
    conflicts: list[tuple[str, str]] = []
    for key, label, configured, inbound_ready, caps in rows:
        if caps.get("error"):
            print(
                "  " + _pad(label, name_w) + _pad("已配置" if configured else "未配置", 8)
                + f"能力读取失败：{caps['error']}"
            )
            continue
        allowed = caps.get("allowed_chat_ids_count")
        # ⚠️ 空白的白名单不是"没填"，是**闸门的一个具体行为**，而那个行为取决于
        # 配置版本：旧语义下是**全开**（危险，要警示符），新语义下是**全拒**
        # （最安全，不该报警 —— 对安全状态报警会教会用户忽略这一列）。
        # 两套文案**都**要短：这是个状态表，长句会把表撑烂。
        if allowed:
            wl = f"{allowed} 项"
        elif empty_allowlist_is_open(cfg.config_version):
            wl = "⚠ 未设=全开"
        else:
            wl = "未设=全拒"
        conflict_detail = caps.get("allowlist_conflict")
        if conflict_detail:
            wl += " ⚠键冲突"
            conflicts.append((label, str(conflict_detail.get("detail") or "")))
        print(
            "  " + _pad(label, name_w)
            + _pad("已配置" if configured else "未配置", 8)
            + _pad("就绪" if inbound_ready else "否", 8)
            + _pad("是" if caps.get("supports_inline_buttons") else "否", 6)
            + _pad("是" if caps.get("supports_media") else "否", 6)
            + _pad(str(caps.get("max_message_length")), 12) + wl
        )

    # 键冲突不塞进表格列里：那句说明有一百多字，塞进去会把整张表撑烂。
    # 单独列在表下 —— 用户扫表时已经看到「⚠键冲突」的标记了。
    if conflicts:
        print("")
        print("== ⚠ 授权键冲突（实际生效的是哪一个键已在此说明） ==")
        for label, detail in conflicts:
            print(f"  {label}: {detail}")

    _print_last_start_probes(rows, bridge_dir)

    print("")
    print("== bridge 运行态 ==")
    print(f"  bridge 目录 : {bridge_dir}")
    state, detail = _runtime_state(bridge_dir)
    print(f"  状态        : {state} —— {detail}")
    for name in ("bridge-plugin.log", "bridge-output.log", "config.json", "state.json"):
        p = os.path.join(bridge_dir, name)
        if os.path.isfile(p):
            size = os.path.getsize(p)
            mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
            print(f"  {name:<20} {size:>8} 字节  最后修改 {mtime}")
    print("  注：--status 是独立进程，看不到 bridge 进程内状态；进程内健康请看 bridge-plugin.log")
    return 0


def run_check(cfg: Config) -> int:
    """Connectivity self-test: ``info()`` only, no sessions, no adapters."""
    endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
    client = OpenCodeClient(endpoint)
    try:
        info = client.info()
    finally:
        client.close()
    print("opencode service OK")
    print(f"  version : {info.get('version', '?')}")
    print(f"  pid     : {info.get('pid', '?')}")
    print(f"  url     : {endpoint.url}")
    return 0


def run_bridge(cfg: Config) -> int:
    if not _has_configured_adapter(cfg):
        # ⚠️ 这条路在 discover_endpoint **之前**返回 ⇒ 「桥没起来」这件事曾经
        # 完全不落盘，而判断**不依赖网络**，所以写记录也不会被"服务不可达"影响。
        _record_bridge_refusal(cfg, REFUSAL_STAGE_PREFLIGHT)
        print(NO_ADAPTER_MESSAGE, file=sys.stderr)
        print(
            "提示：配置完成后下次启动自动生效；也可在 bot 内发送 /setup 查看分平台接入引导。",
            file=sys.stderr,
        )
        return 0

    # 单实例闸门：同一台机器上不允许两个桥同时轮询同一个 bot。
    # 必须在建连接之前拦下——否则消息已经被抢走一半了。
    instance_lock = InstanceLock(_bridge_dir())
    acquired, holder_pid = instance_lock.acquire()
    if not acquired:
        print(ALREADY_RUNNING_MESSAGE.format(pid=holder_pid), file=sys.stderr)
        return 0

    try:
        return _run_bridge_locked(cfg)
    finally:
        instance_lock.release()


def _run_bridge_locked(cfg: Config) -> int:
    """真正启动桥。调用方必须已持有单实例锁。"""

    endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
    client = OpenCodeClient(endpoint)
    # ⚠️ ``migrate_keys=True`` 是**必须**的，不是可选项：telegram / matrix 已改用
    # ``platform:local_id``，而 StateStore 拿 conversation_id 当不透明键存
    # ``conversation_id ↔ session_id``。不开这个开关，已落盘 ``state.json`` 里的
    # ``chat:`` / ``room:`` 键会一次性变成孤儿 —— 用户升级后一次性"忘记"所有历史
    # 会话，不报错，只表现为"agent 突然记错上下文"。歧义的 ``channel:`` 键
    # （slack / discord / mattermost 共用）原样保留、继续可用。
    state = StateStore(cfg.state_path, migrate_keys=True)
    # 写前收件箱必须在 state 旁边，且**开/关都要说出来**：它是可选注入的
    # （None = 关闭），静默关闭正好让这次要修的丢消息 bug 重新变得看不见 ——
    # 所以启动日志里必须能一眼看出当前是开是关。
    inbox_path = _inbox_path(cfg)
    try:
        inbox = InboundInbox(inbox_path)
    except Exception:
        logger.exception("写前收件箱打开失败（%s）", inbox_path)
        inbox = None
    if inbox is None:
        logger.warning(
            "写前收件箱：未启用 —— 投递窗口里崩溃仍会静默丢消息"
        )
    else:
        logger.info("写前收件箱：已启用（%s）", inbox_path)
    core = BridgeCore(cfg, client, state, inbox)

    usable = 0
    #: 「这个平台为什么没能进来」—— ``usable == 0`` 时它就是那条记录的 ``detail`` 来源。
    #: ⚠️ **只在那些 ``continue`` 分支里写** ⇒ 而能 attach 的早就把 ``usable`` 加上去
    #: 了 ⇒ 所以 ``usable == 0`` 时它**必然覆盖了配置里的每一个平台**。
    unusable_reasons: dict[str, str] = {}
    for name, entry in list((cfg.adapters or {}).items()):
        try:
            # ⚠️ **必须过** ``adapter_scoped_config``：适配器只看得到自己的子树，
            # 而 ``pairing_secret`` / ``config_version`` 是**顶层**标量 —— 不投影
            # 进去，闸门就永远读不到（``--status`` 那条路径同样投影，两边必须一致）。
            adapter = build(name, adapter_scoped_config(cfg, entry), core)
        except KeyError:
            logger.warning("unknown adapter %r in config; skipped", name)
            unusable_reasons[str(name)] = "不是已注册的适配器键"
            continue
        except Exception as exc:
            logger.exception("failed to build adapter %r; skipped", name)
            unusable_reasons[str(name)] = "构造失败（%s: %s）" % (
                type(exc).__name__, exc,
            )
            continue
        token = getattr(adapter, "bot_token", None)
        if token is not None and not str(token).strip():
            logger.warning("%s: bot_token missing; adapter skipped", name)
            unusable_reasons[str(name)] = "bot_token 为空"
            continue
        core.attach(adapter)
        usable += 1

    if usable == 0:
        # ⚠️ 这条路在 discover_endpoint **之后**、``core.start()`` **之前** ⇒ 它同样
        # 曾经完全不写记录，而盘上留下的是上一次**成功启动**的结论（见
        # :func:`_record_bridge_refusal` 的 docstring）。注意此处预检**已经过了**：
        # 凭据是齐的，只是构造不出来 ⇒ 结论绝不能写成"未探测／你没填 token"。
        _record_bridge_refusal(cfg, REFUSAL_STAGE_NO_USABLE_ADAPTER, unusable_reasons)
        print(NO_ADAPTER_MESSAGE, file=sys.stderr)
        client.close()
        if inbox is not None:
            inbox.close()
        return 1

    core.start()
    # ⚠️ **整份写一次，且只在这一处写**：``core.start()`` 刚把每个适配器的启动探测
    # 结论收进 ``core.startup_probes``（见 :meth:`BridgeCore.start`），而
    # ``_bridge_dir()`` 只有这里知道 —— 适配器不知道、本类也不该自己推一遍。
    #
    # ⚠️⚠️ **保护必须包住「实参求值」，不只是那次调用**（实测回归，2026-10-06）：
    # ``record_startup_probes`` 内部兜住了写盘失败，但 ``core.startup_probes`` 这个
    # **实参**是在它外面求值的 —— core 若是鸭子类型替身（测试里就有两个刻意最小的
    # ``patch`` 替身）没有这个属性，``AttributeError`` 就在 ``core.start()`` **成功之后**
    # 把桥打死 ⇒ **一个排障辅助功能有权杀掉正在运行的桥**，直接违反上面那句
    # 「写盘失败绝不打断启动」。
    # ⇒ 这里连求值一起兜住：退化行为是「记一条 warning + 本轮不写记录」，
    # **不是**让桥起不来。⚠️ 刻意**不用** ``getattr(core, "startup_probes", None)``：
    # 那会把「真实类改了名」变成静默写空记录；异常里带着 ``AttributeError``
    # 才是能让人查到的信号。
    try:
        probes = core.startup_probes
        health.record_startup_probes(_bridge_dir(), probes)
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "platform-health: 本轮探测结论未落盘（%s: %s）—— 不影响桥的运行",
            type(exc).__name__,
            exc,
        )
    logger.info("bridge running against %s — press Ctrl+C to stop", endpoint.url)
    stop_event = threading.Event()

    # 台账 G1 的取证：桥会不明原因退出，而 Python 层日志在进程被外部杀死时
    # **什么都不会留下**。这里补上崩溃栈 + 生命周期账本，**只取证不自愈**
    # （死因未知时加自愈机制等于用一层自愈掩盖真正的问题）。
    # 详见 opencode_bridge/diagnostics.py 的模块说明。
    diagnostics = ProcessDiagnostics(_bridge_dir())
    diagnostics.install()
    exit_reason = "stopped"
    try:
        while not stop_event.wait(1.0):  # periodic -> Ctrl+C always lands
            pass
    except KeyboardInterrupt:
        exit_reason = "keyboard-interrupt"
        logger.info("收到中断信号 (Ctrl+C)，正在停止 ...")
    except BaseException as exc:  # 含 SystemExit；被信号打断时可能走到这
        exit_reason = "unhandled:%s" % type(exc).__name__
        raise
    finally:
        # 顺序很关键：**先留证据再停 core**。core.stop() 会关掉线程，
        # 那时线程栈已经没有诊断价值了。
        diagnostics.record(exit_reason, detail=describe_environment())
        diagnostics.dump_stacks(exit_reason)
        diagnostics.close()
        core.stop()
        if inbox is not None:
            inbox.close()   # 生命周期清理：关掉 SQLite 连接（WAL 落盘）
    logger.info("已退出")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        cfg = Config.load(args.config)
    except Exception:
        logger.exception("加载配置失败")
        return 1
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)

    try:
        if args.check:
            return run_check(cfg)
        # --setup 走最前：不连 opencode、不需要 token，接入前也能看引导
        if args.setup is not None:
            return run_setup(cfg, args.setup, bool(args.json))
        if args.status:
            return run_status(cfg)
        if args.pair is not None:
            # 改完配置就退出，不去连 opencode、不建适配器。
            return run_pair(cfg, args.pair, args.conversation)
        return run_bridge(cfg)
    except KeyboardInterrupt:
        logger.info("已中断")
        return 1
    except Exception:
        logger.exception("opencode-bridge 异常退出")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
