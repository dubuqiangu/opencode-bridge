"""桥运行域（run_bridge 及其拒绝/路径助手）。

第七刀自 ``__main__.py`` 逐字移植；行为零变化，AST 同一性由拆分脚本断言。
原文件顶部再进口本模块全部名字，既有 import 路径与 ``cli.X`` 属性语义不变。
"""

from __future__ import annotations
import logging
import os
import sys
import threading
from typing import Mapping
from .adapters import build
from .config import Config, adapter_scoped_config
from .core import BridgeCore
from .diagnostics import ProcessDiagnostics, describe_environment
from . import credential_health, health
from .held_buffer_store import HELD_BUFFER_FILE_NAME, HeldBufferStore
from .instance_lock import InstanceLock
from .inbox import InboundInbox
from .opencode_client import OpenCodeClient, discover_endpoint
from .outbound import install_outbound_failure_recorder
from .state import StateStore
from .subscription_health import (
    SubscriptionHealthRecorder,
    install_subscription_health_recorder,
)
from .config_readiness import _has_configured_adapter, _missing_required_keys

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


def _held_buffer_path(cfg: Config) -> str:
    """续行缓冲崩溃快照的落盘位置：与 ``state.json`` **同一个目录**，文件名
    ``held-buffer.json``（G2）。

    与 :func:`_inbox_path` 同一条纪律、同一个理由：跟着 ``cfg.state_path``
    走 —— "还没兑现的处理义务"必须跟着状态一起搬家（这份快照装的正是
    另一类还没兑现的义务：敲了 ``..`` 的半行）。文件与两份既有状态文件
    各自独立、键不互混（这里只放 ``{conversation_id: 正文}``）。
    """
    return os.path.join(
        os.path.dirname(os.path.abspath(cfg.state_path)) or _bridge_dir(),
        HELD_BUFFER_FILE_NAME,
    )


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
    # ⚠️ **装在 ``BridgeCore(...)`` 构造【之前】**（不是 ``start()`` 之前）：看护者是在
    # :class:`~opencode_bridge.event_stream.EventStream` 的构造里建的，它当场把记录器
    # 取成 ``on_status_change`` ⇒ 晚一步装上，那条通道整个不存在而**没有任何东西会
    # 报错**。⛔ 装不上不许阻断启动（排障通道不该决定桥的生死）；退化行为就是
    # 「不落盘」，而 ``--status`` 那一段会说「读不到」（⛔ 不是「正常」）。
    try:
        install_subscription_health_recorder(
            SubscriptionHealthRecorder(_bridge_dir())
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "subscription-health: 记录器未装配（%s: %s）—— 不影响桥的订阅",
            type(exc).__name__, exc,
        )
    # G2：续行缓冲的崩溃快照层，**无条件启用**。它的构造不碰盘（读写失败
    # 各自只记 WARNING、绝不上抛，见 held_buffer_store 与
    # InboundGateway._persist_held_snapshot），所以没有 inbox 那个「打开失败
    # 降级」分支；启用与否照 inbox 的纪律**说出来**，不让人去翻代码。
    held_buffer_path = _held_buffer_path(cfg)
    held_buffer_store = HeldBufferStore(held_buffer_path)
    logger.info(
        "续行缓冲快照：已启用（%s）—— 崩溃后重启会把半行重灌回缓冲并告知",
        held_buffer_path,
    )
    core = BridgeCore(cfg, client, state, inbox, held_buffer_store=held_buffer_store)

    usable = 0
    # ⚠️ **装在 `core.start()` 之前**：适配器一启动就可能发信（探针回复、启动时
    # 排队消息的补发），而这一路此前完全没有落盘 ⇒ 那几条失败会在记录里缺一段。
    # ⛔ 装不上不许阻断启动（排障通道不该决定桥的生死）；退化行为就是「不记」，
    # 发送路径一个字不变。
    try:
        install_outbound_failure_recorder(health.OutboundFailureRecorder(_bridge_dir()))
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "outbound-failures: 记录器未装配（%s: %s）—— 不影响桥的发送",
            type(exc).__name__, exc,
        )
    # ⚠️ 同一条纪律、同一个时机（必须早于适配器启动）：telegram 的轮询线程在
    # **运行期**观测到 401 就会写这份记录（``_invalidate_credentials``）⇒ 晚一步
    # 装上，「启动之后 token 被吊销」这条事实就一个字都不落盘，而**没有任何东西会
    # 报错**（:func:`credential_health.install_credential_failure_recorder` 的
    # docstring 说的正是这一条）。
    try:
        credential_health.install_credential_failure_recorder(
            credential_health.CredentialFailureRecorder(_bridge_dir())
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "credential-failures: 记录器未装配（%s: %s）—— 不影响桥的轮询",
            type(exc).__name__, exc,
        )
    #: 「这个平台为什么没能进来」—— ``usable == 0`` 时它就是那条记录的 ``detail`` 来源。
    #: ⚠️ **只在那些 ``continue`` 分支里写** ⇒ 而能 attach 的早就把 ``usable`` 加上去
    #: 了 ⇒ 所以 ``usable == 0`` 时它**必然覆盖了配置里的每一个平台**。
    unusable_reasons: dict[str, str] = {}
    #: 「这一轮真的 attach 上去了」的平台键（:func:`~opencode_bridge.health.platform_key`
    #: —— ⛔ 必须与 :meth:`BridgeCore.start` 给 ``startup_probes`` 用的**同一个**来源，
    #: 否则集合相减会把"探测过的"误判成"没探测的"）。
    #:
    #: ⚠️ 它存在的唯一理由：**只有 telegram 会 ``report_startup_probe``**（只有它调
    #: ``getMe``）⇒ 其余平台压根不往 ``platforms`` 里写条目 ⇒ ``--status`` 上恒为
    #: 「无记录」，用户**分不清**是配置坏了还是这个平台压根不上报。⇒ 用
    #: ``attach 过`` 减 ``上报过`` 反推出来，让「不做探测」有一档可读的落点。
    #: ⛔ **这是推导、不是名单**：新增第 14 个平台不需要改这里（见
    #: :func:`opencode_bridge.health.add_platforms_without_startup_probe`）。
    #: ⛔ 它**不产生任何网络请求** —— 只用"谁没说话"这个已经发生的事实。
    attached_platforms: list[str] = []
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
        attached_platforms.append(health.platform_key(adapter))
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
    # ⚠️ ⚠️ **「已 attach 却没上报」的那些平台也要补一条**（只有 telegram 会
    # ``report_startup_probe`` ⇒ 其余平台的 ``startup_probes`` 里压根没有它们）：
    # 不补的话 ``--status`` 上它们恒为「无记录」，而那一段措辞是**通用**的
    # ⇒ 用户分不清「配置坏了」与「这个平台压根不上报」。
    # ⛔ 补的这一档由 :func:`~opencode_bridge.health.add_platforms_without_startup_probe`
    # 造，是**推导**（``attach 过`` 减 ``上报过``）⇒ **新增平台不用改这里**。
    # ⛔ 它**不做任何网络请求**：⚛️「构造成功」≠「凭据有效」，替别人编一个 ``ok``
    # 就是伪造观测。
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
    # 才是能让人查到的信号。⚠️ 同理**不**给 ``attached_platforms`` 加 ``getattr``
    # 兜底：它在上面那个循环里现填的，不是外部对象上的属性。
    try:
        probes = core.startup_probes
        health.record_startup_probes(
            _bridge_dir(),
            health.add_platforms_without_startup_probe(probes, attached_platforms),
        )
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
