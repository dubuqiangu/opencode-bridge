"""安装向导域（--setup 的平台就绪渲染）。

第七刀自 ``__main__.py`` 逐字移植；行为零变化，AST 同一性由拆分脚本断言。
原文件顶部再进口本模块全部名字，既有 import 路径与 ``cli.X`` 属性语义不变。
"""

from __future__ import annotations
import json
from .allowlist import resolve_allowlist
from .config import Config
from .core import setup_platforms, setup_reply
from . import health
from .config_readiness import (
    _config_file_in_use,
    _missing_required_keys,
    _not_ready_reasons,
    _readiness_verdict,
    _status_platform_keys,
    _token_present,
)
from .bridge_runner import _bridge_dir
from .platform_status import _adapter_capabilities


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
    #: **运行期**出站失败记录（**另一个文件**，见 :mod:`opencode_bridge.health`
    #: 的「出站失败为什么另开一份文件」）。⛔ 与上面的 ``probe_record`` 时效语义
    #: 不同（一个是「上次启动那一刻」，一个是「上一次失败的那一刻」），所以连
    #: 载体都分开 —— 否则 :func:`health.record_startup_probes` 的整份替换会顺手
    #: 把它删掉。同样**纯本地读盘**：本函数不做任何网络请求。
    outbound_record = health.read_outbound_failures(_bridge_dir())
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
                # 建议用户改凭据。
                #
                # ⚠️ **还有一档 :data:`health.VERDICT_DOES_NOT_PROBE`**（``does_not_probe``）：
                # 它答的是「**本平台压根没有启动期凭据探测这个动作**」——
                # 目前只有 telegram 会探（``getMe``）。⛔ 它**不是** ``skipped``
                # （那一档说的是「没有可探测的凭据」＝"你少配了"），也**不是**
                # ``not_started``（那一档说的是"桥没起来"）。消费方读它就该知道
                # **这条通道压根没覆盖这个平台**，而**不是**"这里没问题"。
                #
                # ⛔ 既有三档（``ok`` / ``failed`` / ``skipped``）
                # **一个字没改**：本函数对 ``platforms`` 的其它 key 也一样，只增不改。
                "last_start_probe": health.probe_from_record(probe_record, key),
                # --- 运行期出站失败（新增；outbound_ready 的含义不变）----------
                # ⚠️ **不要把它读成"发送能用"**：``outbound_ready`` 的原义是
                # 「**出站凭据齐备**」，是一个**有测试钉住的既有契约**，一个字没改
                # （改它等于让每个既有消费者重新学一遍这套输出）。而"凭据齐"与
                # "真发得出去"是两件事 —— SMTP 密码改了、Telegram 把 bot 踢了、
                # 对端限流，凭据照样齐备而**一条都发不出去**。
                # ⇒ 所以**另加**这一个字段回答后者。
                #
                # ⛔ **它回答的是"上一次失败"，不是"现在能不能发"**：``at`` 是那
                # 一刻的时刻，``recovered_at`` 缺失 = 之后**没有观测到**成功
                # （可能是仍在失败，也可能是压根没人再发消息 —— 盘上分不出是哪
                # 一种，而猜一个就是编造，见 AGENTS.md §8）。
                #
                # ⛔ **``None`` 的含义是「没有记录」，不是「没问题」**：桥还没以
                # 当前配置运行过、或运行期间一条都没发出去，两者都是 ``None``。
                "last_outbound_failure": health.outbound_failure_from_record(
                    outbound_record, key,
                ),
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
