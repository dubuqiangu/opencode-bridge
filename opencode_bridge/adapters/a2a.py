"""a2a 适配器（``tasks.md`` B1）：**我们是被调方** —— 起一个本地 HTTP 服务，
让外部 agent 用 A2A（Agent2Agent）协议把请求送进桥接。

## 这个平台的方向和其它十个平台相反

前十个平台全是 outbound：我们主动去连（长轮询 / WebSocket 客户端）。**a2a 反过来**
—— 我们要监听端口，被别人调。这是仓库里第一个需要入站 socket 的平台，也是路线图
**A3（inbound-push 入口）的第一个使用方**，所以 HTTP 服务本身放在
:mod:`opencode_bridge.httpsrv` 里，本文件只提供"路径 + 处理器 + 是否鉴权"。

## 协议事实（全部核实过，来源见 :data:`_FACTS`）

* Agent Card 发现路径 ``/.well-known/agent-card.json``（规范 §8.2 / §14.3）。
* JSON-RPC 方法名是 **PascalCase**：``SendMessage`` / ``GetTask`` / ``ListTasks`` /
  ``CancelTask``（§5.3 / §9.4）。**不是** ``message/send`` 那种斜杠名。
* ``SendMessage`` 的响应是 ``{"task": {...}}`` 或 ``{"message": {...}}`` 二选一
  （§3.2.3 oneof），不能裸返回 Task。
* ``GetTask`` / ``CancelTask`` 的参数键是 ``id``（**不是** ``taskId``，§3.1.3/§3.1.5）；
  ``TaskStatusUpdateEvent.taskId`` 才是 ``taskId``。
* 状态值是 ``TASK_STATE_*``（§4.1.3），角色是 ``ROLE_USER`` / ``ROLE_AGENT``（§4.1.5）。
* ``Part`` 按 oneof 分派：``text`` / ``raw`` / ``url`` / ``data``（§4.1.6）。
* JSON 序列化必须 camelCase（§5.5）。

## 我们声明什么能力，就只实现什么

Agent Card 里 ``streaming: false`` / ``pushNotifications: false`` /
``extendedAgentCard: false``。规范 §3.3.4「能力校验」**要求**未声明的能力被调用时
回对应错误，于是 ``SendStreamingMessage`` 等 7 个方法全部回 ``-32004`` /
``-32003``（错误码见 §5.4）。这是本适配器最省事也最诚实的一块：**能力声明与实现
一一对应**，不需要猜测，也没有半吊子的实现。

## 安全：默认 bind 回环 + 默认无鉴权 = 本机攻击面

⚠️ **默认不鉴权。** 本机 localhost 也是攻击面：浏览器里的恶意网页可以直接打
``http://127.0.0.1:<port>/rpc``（``no-cors`` 的 POST 不发请求头就能触发 agent 运行）。
所以：

1. **默认只 bind ``127.0.0.1``**（显式常量 :data:`DEFAULT_BIND_HOST`，无后门）。
2. **请求更宽的绑定地址时**，若**没有**配任何凭据，则**回落回环 + 打 WARNING**
   —— 绝不开出一个无鉴权的局域网端口（与 Hermes 的 ``resolve_bind_host`` 同策）。
3. 配了 ``auth_token`` 或 ``peer_tokens`` 时按 **Bearer** 校验（规范 §4.5.3 的
   ``httpAuthSecurityScheme``），并把它**如实写进 Agent Card 的 ``securitySchemes``**
   —— 客户端靠这个字段知道要带凭据（§7.3）。
4. 身份只从**凭据**来，**绝不**从请求体取（这正是 ntfy 用 ``title`` 当身份被否掉的
   同一个坑）。未鉴权时身份是 ``local:<对端 IP>``，并在文档里写明它不是真身份。

## 防回环：按 contextId 计轮次，**不做内容启发式**

我们的回复走 :meth:`A2aAdapter.send` —— 它把文本交给**本地等待中的 HTTP 请求**，
**不会**再发一次 HTTP 出去，所以不存在"自己打自己"的回环。

真正的回环风险是 **A 问我们 → 我们答 → A 把答案又拿来问我们** 的乒乓。对策是按
A2A 的 ``contextId`` 计轮次（规范 §3.4.1 的原生分组原语，不是启发式），超过上限
就回 ``TASK_STATE_REJECTED``。

⚠️ **刻意不用**"看起来像自己发的就丢"这种内容启发式 —— 那会误伤用户的真实请求，
而且本仓库在 ntfy / email 上都因此踩过坑。轮次上限是可解释、可关闭（调大
``max_turns``）、且只按协议字段判定的。

## 消息与对端的对应关系

每个入站 ``SendMessage`` 都会建一个 task 记录，``conversation_id`` 用
``identity.format_id("a2a", <对端标识>)`` —— 即**一个对端 agent 一个会话**
（对端标识来自凭据，见上）。task / contextId ↔ 对端的映射放在**内存**里，
所以：

* **进程重启后**：等待中的 HTTP 请求全部作废（它们本来就在等 agent 的答复），
  对端若重发 ``SendMessage`` 会得到一个**新** task；但 ``conversation_id`` 不变，
  所以 opencode 侧的会话（``state.json``）与上下文**继续沿用**。也就是说"重启丢
  的是这一次 HTTP 往返"，不是"对话历史"。
* 同一个对端并发发多个 task 时，:meth:`send` 按 **FIFO 取最老的未完成任务**回复。
  这是已知的不精确点（``Outbound`` 里没有 task id，无法更精确匹配），已在此写明。

## 已知缺口（不是"实现了但用户用不了"，是"没做"）

* 无 SSE 流式（``message/stream`` / ``tasks/subscribe``）—— 已按规范回 -32004。
* 无 push notification 配置 —— 已按规范回 -32003。
* 无出站调用别的 agent 的能力（本适配器只做被调方）。
* 入站文本**未**加"这是不可信外部输入"的框架前缀。与 ntfy / email 一致（本仓库
  的既有做法是把信任模型写进文档而不是改写正文）；但 a2a 的入站确实来自任意外部
  agent，这一点比其它平台更值得后续补。
"""

from __future__ import annotations

import collections
import hmac
import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..config_coerce import coerce_int
from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..httpsrv import (
    DEFAULT_BIND_HOST,
    MAX_BODY_BYTES,
    BindError,
    HttpRequest,
    HttpResponse,
    HttpServer,
    Route,
    is_loopback_host,
)
from ..identity import format_id
from ._redactable_ids import redactable_id
from .base import Adapter, register

# ----------------------------------------------------------------------
# §5 拆分再进口：协议常量 / wire 纯函数域已逐字移植到 a2a_protocol，
# 配置闸域已逐字移植到 a2a_config_gates；这里再进口全部名字，保持
# ``from opencode_bridge.adapters.a2a import X`` 与 ``a2a.X`` 的既有
# 路径 / 属性语义不变（行为零变化）。
# ----------------------------------------------------------------------
from .a2a_config_gates import (
    _coerce_port, _coerce_positive, _coerce_turns, _local_of,
    _parse_peer_tokens,
)
from .a2a_protocol import (
    AGENT_CARD_PATH, BRIDGE_VERSION, DEFAULT_AGENT_DESCRIPTION, DEFAULT_AGENT_NAME,
    DEFAULT_MAX_TURNS, DEFAULT_REPLY_TIMEOUT, ERR_CONTENT_TYPE_NOT_SUPPORTED, ERR_INTERNAL,
    ERR_INVALID_PARAMS, ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND, ERR_PARSE,
    ERR_PUSH_NOT_SUPPORTED, ERR_TASK_NOT_CANCELABLE, ERR_TASK_NOT_FOUND, ERR_UNSUPPORTED_OPERATION,
    ERR_VERSION_NOT_SUPPORTED, HARD_MAX_TURNS, HEALTH_PATH, LEGACY_AGENT_CARD_PATH,
    MAX_BIND_PORT, MAX_TASKS, MESSAGE_LIMIT, MIN_BIND_PORT,
    PROTOCOL_VERSION, ROLE_AGENT, ROLE_USER, RPC_PATHS,
    STATE_CANCELED, STATE_COMPLETED, STATE_FAILED, STATE_INPUT_REQUIRED,
    STATE_REJECTED, STATE_SUBMITTED, STATE_WORKING, SUPPORTED_VERSION_HEADERS,
    TERMINAL_STATES, TURN_TTL_SECONDS, UNCONFIGURED_PORT, _ERROR_HTTP_STATUS,
    _TASK_STATE_BY_OUTBOUND_KIND, _default_agent_name, _message, _new_id,
    _now_iso, _render_task, _rpc_error, _rpc_response,
    _rpc_result, _text_part, extract_context_id, extract_text,
)

# §5.1 类手术再进口：task 账本域已迁入 a2a_task_ledger（``_Task`` 随域走）；
# 这里再进口保持 ``a2a._Task`` / ``a2a.A2aTaskLedger`` 可解析（留守侧注解
# 与转发方法签名仍引用它们）。
from .a2a_task_ledger import A2aTaskLedger, _Task


logger = logging.getLogger("opencode_bridge.adapters.a2a")

__all__ = [
    "A2aAdapter",
    "MESSAGE_LIMIT",
    "MAX_BODY_BYTES",
    "DEFAULT_BIND_HOST",
    "UNCONFIGURED_PORT",
    "MIN_BIND_PORT",
    "MAX_BIND_PORT",
    "RPC_PATHS",
    "AGENT_CARD_PATH",
    "PROTOCOL_VERSION",
]


@register("a2a")
class A2aAdapter(Adapter):
    """A2A（Agent2Agent）**被调方**：起一个本地 HTTP 服务接收外部 agent 的任务。

    完整的能力声明、安全模型与防回环取舍见**模块 docstring**。

    配置项（``config.json`` 的 ``adapters.a2a``）::

        {
          "bind_port": 9900,              # 必填，无默认值（见 required_tokens）
          "bind_host": "127.0.0.1",       # 可选，默认回环；放宽需先配凭据
          "auth_token": "…",              # 可选，共享 Bearer 凭据
          "peer_tokens": "alice:t1,bob:t2",  # 可选，每对端一个凭据，身份=名字
          "allowed_chat_ids": ["alice"],  # 可选，走基类 admits() 闸门
          "reply_timeout": 300,           # 可选，等 agent 答复的秒数
          "max_turns": 5,                 # 可选，每个 contextId 的轮次上限
          "agent_name": "…", "agent_description": "…"
        }
    """

    name = "a2a"
    label = "A2A"
    #: 见 :data:`MESSAGE_LIMIT` 的长注释：**规范没有规定**，这里取我们自己的 1 MiB
    #: 请求体上限，不是编出来的"权威数字"。
    max_message_length = MESSAGE_LIMIT
    #: 出站把整段文本放进**一个** artifact（:meth:`A2aAdapter.send` 从不切片），
    #: 所以上面那个数是请求体**字节**上限，不是"一条消息能装多少字"。
    splits_long_messages = False
    supports_inbound = True
    #: ⛔ **显式 False** —— principal 是**对端 peer**，判据 (a) 不成立：
    #: peer 是对方自报的标识，不同对端实现下同一个"会话"可以长得一模一样，
    #: 授权它等于授权一个**不可验证**的字符串。
    pairing_supported = False
    #: A2A 没有"按钮"这个概念（Part 只有 text/raw/url/data，§4.1.6）。
    supports_inline_buttons = False
    #: 同上：本适配器不落地文件，也不下载对端的 ``url`` / ``raw``。
    supports_media = False
    typed_command_prefix = "/"

    #: **没有"凭据"可言，但确实有"非配不可"的东西**：
    #: 没有端口就**根本无法作为 A2A 服务被发现** —— Agent Card 里公布的 URL 含端口，
    #: 端口 0 每次重启都变，对端永远找不到我们。所以 ``bind_port`` **必填、无默认值**，
    #: 这样状态视图的 "missing: ['bind_port']" 是一条**有用的**提示而不是走过场。
    #:
    #: 刻意**不**把 ``auth_token`` 列为必需：没有它服务照样能跑（绑回环），
    #: 把它列成必需会让"配齐"变成"必须承认自己没防护"，反而让用户随手填个假 token。
    required_tokens = ("bind_port",)
    #: a2a 的"出站"**根本不发网络请求**：:meth:`send` 只是把文本交给一个正在等待的
    #: 本地 HTTP 请求（规范 §3.1.1 的阻塞语义）。
    #:
    #: 那"出站需要什么凭据"？答案是**需要那个端口**：没有绑上端口就没有等待中的
    #: HTTP 请求，也就没有任何东西可交付。所以 ``bind_port`` 同时是出站的前置条件 ——
    #: 这不是为了让 ``outbound ⊆ required`` 的不变量好看而硬凑的，仓库的
    #: ``test_cli.py`` 也要求两个声明都**非空**。
    outbound_tokens = ("bind_port",)
    #: 「配好了没有」**由本平台自己回答**（读 :meth:`config_runnable`）——
    #: a2a 属于"没有凭据可填"的那一类：通用规则（"``required_tokens`` 的键非空"）
    #: 对它既不充分也不必要：``bind_port: ""`` 非空吗？不空 ⇒ 通用规则说"配好了"，
    #: 而 :meth:`start` 会打 ``port < 0`` 报错**根本不绑定**。
    #:
    #: ⛔ **"空配置即可运行"这条前提已经不成立**（它曾让本仓库把空模板判成已配置）：
    #: 本平台没有"没有配置也能跑"的默认端口 —— :data:`_coerce_port` 对空串给出
    #: :data:`UNCONFIGURED_PORT`，:meth:`start` 据此拒绝启动。所以这里**不再**
    #: 声明"可省略配置"，而是声明"**我的答案我自己给**"（配合下面的覆写）。
    config_optional = True

    @classmethod
    def config_runnable(cls, entry: dict) -> bool:
        """a2a 的答案是：**这份配置里有没有一个能绑的整数端口。**

        判据只有一条：``bind_port`` 强制成 ``int`` 后 ``>= 0``。而那个 ``int`` 是
        :func:`opencode_bridge.config_coerce.coerce_int` 给的（⛔ 不另写一份解析 ——
        本仓库的纪律是同一类解析只留一份，见该模块的模块 docstring），
        区间与 :func:`_coerce_port` 逐字一致（``[0, 65535]``），缺省值与它共用
        :data:`UNCONFIGURED_PORT`。

        * ``0`` 与正整数 ⇒ **能跑**。``0`` 是**合法配置**：绑到回环的临时端口，
          :meth:`start` 会把系统分配的端口写回 ``self.port``（见那里的注释）——
          只适合测试（每次重启都变），但它**确实起得来**。
        * 缺键 / 空串 / 空白 ⇒ **不能跑**（:data:`UNCONFIGURED_PORT`）。
        * 负数 / 超区间 / 解析失败 ⇒ **不能跑**，并且用户会同时拿到一条
          ``config_coerce`` 的 WARNING，点名是 ``bind_port`` 这个键配错了。

        ⚠️ **为什么不能只看"键非空"**（通用规则）：``bind_port: "nope"`` 非空，
        而 :meth:`start` 会拒绝启动 —— 那种配置一旦被说成"配好了"，用户就会得到
        一个**空转**的桥（它照常启动，只是 a2a 那个适配器永远没绑上）。

        :param entry: 用户配置里 ``adapters.a2a`` 那棵**原始条目**。非 ``dict``
            的传入按空条目处理（三条判定路径本来就都先归一成 dict，这里只是
            不给本方法留一个 ``AttributeError`` 的口子）。
        """
        settings = entry if isinstance(entry, dict) else {}
        raw = settings.get("bind_port")
        # ⛔ 先挡 ``bool`` / ``float``（**不是**另一种解析，只是"这个值的类型压根不是
        # 端口"）：``coerce_int`` 会把 ``True`` 读成 1、把 ``9900.7`` **截断**成 9900
        # （它文档里明写的行为），而 :func:`_coerce_port` 把两者都判成
        # :data:`UNCONFIGURED_PORT`。放过它们的话本判定会说"能跑"而
        # :meth:`start` 拒绝启动 —— 又是同一形态的谎。挡掉之后两条解析在**每一个**
        # 取值上都给出同一个答案（用例见 ``tests/test_config_runnable_verdict.py``）。
        if isinstance(raw, (bool, float)):
            return False
        port = coerce_int(
            settings, "bind_port", UNCONFIGURED_PORT,
            minimum=MIN_BIND_PORT, maximum=MAX_BIND_PORT, platform=cls.name,
        )
        return port >= 0

    # --- 类级默认值（实例属性在 __init__ 里被配置覆盖）---------------
    #: 见 :data:`DEFAULT_REPLY_TIMEOUT` 等常量；这里保留类级声明是为了
    #: "不配置时也有真值"和"测试可在实例上直接覆盖"。
    reply_timeout = DEFAULT_REPLY_TIMEOUT
    max_turns = DEFAULT_MAX_TURNS
    max_tasks = MAX_TASKS

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.port: int = _coerce_port(self.config.get("bind_port"))
        self.host: str = self._resolve_bind_host()

        # --- 凭据 ---------------------------------------------------
        self._auth_token: str = str(self.config.get("auth_token") or "").strip()
        #: ``(token, name)`` 列表；身份取 name（每个对端一个凭据，比共享 token 好定位）。
        self._peer_tokens: tuple[tuple[str, str], ...] = _parse_peer_tokens(
            self.config.get("peer_tokens")
        )
        self._auth_enabled: bool = bool(self._auth_token or self._peer_tokens)

        # --- Agent Card 文案 ----------------------------------------
        self.agent_name: str = str(
            self.config.get("agent_name") or DEFAULT_AGENT_NAME
        ).strip() or DEFAULT_AGENT_NAME
        self.agent_description: str = str(
            self.config.get("agent_description") or DEFAULT_AGENT_DESCRIPTION
        ).strip() or DEFAULT_AGENT_DESCRIPTION
        self.agent_version: str = str(
            self.config.get("agent_version") or BRIDGE_VERSION
        ).strip() or BRIDGE_VERSION

        # --- 可调旋钮（非法值一律回落默认值，绝不回落成"更危险"的那个）------
        #: 等 agent 答复的秒数。非法 / 非正 -> 默认值。
        self.reply_timeout: float = _coerce_positive(
            self.config.get("reply_timeout"), DEFAULT_REPLY_TIMEOUT, "reply_timeout"
        )
        #: 每个 contextId 的轮次上限（防乒乓）。**硬顶** :data:`HARD_MAX_TURNS`：
        #: 否则有人配 ``max_turns: 100000`` 就等于把防回环关掉了，而那正是它存在的
        #: 理由 —— 所以非法/过大的值回落成默认值，不是回落成"无限制"。
        self.max_turns: int = _coerce_turns(self.config.get("max_turns"))
        #: 内存里保留的 task 记录上限。
        self.max_tasks: int = _coerce_positive(
            self.config.get("max_tasks"), MAX_TASKS, "max_tasks"
        )

        # --- 运行时状态 ----------------------------------------------
        self._server: Optional[HttpServer] = None
        self._tasks: dict[str, _Task] = {}
        #: ``peer -> deque[task_id]``：:meth:`send` 按它取"最老的未完成任务"回复。
        self._order: dict[str, collections.deque[str]] = {}
        self._turns: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        #: ``(task, inbound) -> None``。HTTP handler 线程投递，分发线程消费
        #: —— 见 :meth:`_dispatch_loop` 的注释（为什么要这一层间接）。
        self._queue: "queue.Queue[Optional[tuple[_Task, Inbound]]]" = queue.Queue()
        #: task 账本域已抽出（§5.1 类手术）：账本方法在
        #: :class:`A2aTaskLedger`；状态（``_tasks``/``_order``/``_lock``/
        #: ``max_tasks``）留在适配器本体 —— 锁与留守侧轮次计数 / send /
        #: stats 共享，测试也直戳 ``adapter._tasks``；新类逐调用经
        #: read-only property 转发回来。
        self._task_ledger = A2aTaskLedger(self)

    # ==================================================================
    # 配置
    # ==================================================================
    def _resolve_bind_host(self) -> str:
        """实际绑定地址：默认回环；**放宽且无凭据时回落回环 + 警告**。

        为什么是"回落 + 警告"而不是"照做 + 警告"：本机无鉴权端口一旦绑到
        ``0.0.0.0``，同网段任何人都能驱动用户机器上的 agent。宁可让用户发现
        "我配的地址没生效"，也不要悄悄开出一个洞。与 Hermes 的
        ``A2ASecurityContext.resolve_bind_host`` 同策。
        """
        requested = str(self.config.get("bind_host") or DEFAULT_BIND_HOST).strip()
        requested = requested or DEFAULT_BIND_HOST
        if is_loopback_host(requested):
            return requested
        # 非回环：只有配了凭据才放行 —— 但凭据是在 __init__ 后半段才算出来的，
        # 这里直接读配置（同一份数据，顺序无关）。
        has_cred = bool(
            str(self.config.get("auth_token") or "").strip()
            or _parse_peer_tokens(self.config.get("peer_tokens"))
        )
        if has_cred:
            return requested
        logger.warning(
            "a2a: 请求的 bind_host=%r 不是回环地址且未配置 auth_token / peer_tokens —— "
            "已回落为 %s。任何本机进程都能驱动 agent，非回环暴露风险过高。"
            "要开放给其它机器，请先配 Bearer 凭据。",
            requested, DEFAULT_BIND_HOST,
        )
        return DEFAULT_BIND_HOST

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> None:
        """起 HTTP 服务 + 分发线程。**不抛异常**（仓库约定：启动失败只记日志）。"""
        if self.running:
            return
        if self.port < 0:
            logger.error(
                "a2a: 未配置 bind_port（%r），无法作为 A2A 服务被发现；适配器未启动。"
                "A2A 是被调方，Agent Card 里公布的 URL 必须含一个稳定端口。",
                self.config.get("bind_port"),
            )
            return

        server = HttpServer(
            host=self.host,
            port=self.port,
            name=self.name,
            authenticate=self._authenticate,
            unauthorized=self._unauthorized,
            max_body=MAX_BODY_BYTES,
            routes=self._routes(),
        )
        try:
            actual = server.start()
        except BindError as exc:
            logger.error("a2a: 无法绑定 %s:%s —— %s；适配器未启动", self.host, self.port, exc)
            return
        self._server = server
        self.port = actual                      # port=0 时是操作系统分配的那个
        self._stop_event.clear()

        thread = threading.Thread(
            target=self._dispatch_loop, name=f"{self.name}-dispatch", daemon=True
        )
        self._thread = thread
        thread.start()

        logger.info(
            "a2a: Agent Card %s%s（协议 %s，仅被调方向）",
            server.url(AGENT_CARD_PATH),
            "，JSON-RPC " + " / ".join(server.url(p) for p in RPC_PATHS),
            PROTOCOL_VERSION,
        )
        if not self._auth_enabled:
            logger.warning(
                "a2a: ⚠ 未配置任何凭据（auth_token / peer_tokens）—— 任何本机进程都能"
                "驱动 agent，浏览器里的恶意网页也能直接打 %s。"
                "绑定地址 %s%s。",
                server.url(RPC_PATHS[0]), server.host,
                "（回环）" if is_loopback_host(server.host) else "（⚠ 非回环）",
            )

    def stop(self, timeout: float = 5.0) -> None:
        """**干净关闭**：先让等待中的请求收工，再关服务，最后 join 分发线程。

        顺序即本仓库的铁律「**先关连接再 join**」：

        1. ``_stop_event.set()`` + 把所有未完成 task 判成 ``CANCELED`` 并 ``set()``
           它们的 ``done`` 事件 —— 否则那些正阻塞在 ``done.wait()`` 的 HTTP handler
           线程会一直等到 ``reply_timeout``（默认 300s）才醒，停机就白等 5 分钟。
        2. ``HttpServer.stop()`` 内部再 ``shutdown()`` → ``server_close()`` → join。
        3. 最后才 join 分发线程。
        """
        self._stop_event.set()
        was_running = self.running
        # (1) 先唤醒所有等待中的 handler
        self._fail_pending(STATE_CANCELED, "[bridge shutting down]")
        # (2) 关服务（内部顺序固定：shutdown → server_close → join → 等在途收尾）
        server, self._server = self._server, None
        if server is not None:
            server.stop(timeout=timeout)
        # (3) 停分发线程
        self._queue.put(None)
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        if was_running:
            logger.info("a2a: 已停止，端口 %d 已释放", self.port)

    @property
    def running(self) -> bool:
        """服务在跑 **且** 端口确实绑上了。

        与基类不同：a2a 的"活着"不是"有个线程"，而是"线程在 + 监听 socket 在"。
        绑端口失败时线程根本不会起，但这条属性也要如实报 False。
        """
        thread = self._thread
        server = self._server
        return (
            thread is not None
            and thread.is_alive()
            and server is not None
            and server.bound
        )

    def capabilities(self) -> dict:
        """基类快照 + 本平台特有的运行态（绑定地址 / 端口 / 鉴权状态）。

        多出来的字段是为了让 ``--status`` 能如实显示"到底绑在哪、有没有鉴权" ——
        这是个**被调方**，用户不问就没人知道。
        """
        caps = dict(super().capabilities())
        server = self._server
        caps.update({
            "bind_host": server.host if server is not None else self.host,
            "bind_port": self.port if self.port >= 0 else None,
            "loopback_only": is_loopback_host(
                server.host if server is not None else self.host
            ),
            "auth_enabled": self._auth_enabled,
            "auth_schemes": ("bearer",) if self._auth_enabled else (),
            "well_known_path": AGENT_CARD_PATH,
            "rpc_paths": RPC_PATHS,
            "protocol_version": PROTOCOL_VERSION,
            "streaming": False,
            "push_notifications": False,
        })
        return caps

    def stats(self) -> dict[str, int]:
        """计数快照（task / 轮次 / HTTP 层），纯可观测，不参与逻辑。"""
        with self._lock:
            tasks = list(self._tasks.values())
            contexts = len(self._turns)
        out = {
            "tasks_created": len(tasks),
            "tasks_pending": sum(
                1 for t in tasks if t.state not in TERMINAL_STATES
            ),
            "contexts": contexts,
        }
        server = self._server
        if server is not None:
            out.update({f"http_{k}": v for k, v in server.stats().items()})
        return out

    # ==================================================================
    # 路由（只声明"路径 + 处理器 + 是否鉴权"）
    # ==================================================================
    def _routes(self) -> list[Route]:
        return [
            Route(AGENT_CARD_PATH, self._handle_agent_card, methods=("GET",),
                  name="agent-card"),
            Route(LEGACY_AGENT_CARD_PATH, self._handle_agent_card, methods=("GET",),
                  name="agent-card-legacy"),
            Route(HEALTH_PATH, self._handle_health, methods=("GET",),
                  name="health"),
            # Agent Card 是**公开**的（规范 §14.3："MAY contain public information"），
            # 客户端要靠它才知道要不要带凭据 —— 鉴权它会让发现流程直接死掉。
            *[Route(path, self._handle_rpc, methods=("POST",), require_auth=True,
                    name=f"rpc:{path}")
              for path in RPC_PATHS],
        ]

    # --- 鉴权 ----------------------------------------------------------
    def _authenticate(self, request: HttpRequest) -> Optional[str]:
        """Bearer 校验（规范 §4.5.3 的 ``httpAuthSecurityScheme`` / §7.3）。

        返回对端标识或 ``None``。**标识只来自凭据**，绝不来自请求体或请求头里
        自称的名字 —— 后者由发请求的人任意填写，当身份等于没有认证。
        """
        if not self._auth_enabled:
            # 没有配凭据：身份退化成"来源 IP"，并在标识里明说是本地匿名。
            return f"local:{request.client_host or 'unknown'}"
        header = request.header("authorization")
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        presented = parts[1].strip()
        for token, name in self._peer_tokens:
            if hmac.compare_digest(presented, token):
                return name
        if self._auth_token and hmac.compare_digest(presented, self._auth_token):
            return f"bearer:{request.client_host or 'unknown'}"
        return None

    def _unauthorized(self, request: HttpRequest) -> HttpResponse:
        """401 + ``WWW-Authenticate: Bearer``。

        ⚠️ **刻意不套 JSON-RPC 信封**：规范把认证失败映射为 HTTP 401
        （§3.3.2 "Example error codes: HTTP 401 Unauthorized"）并**没有**定义对应的
        JSON-RPC 错误码。凭空造一个（比如 -32010）会被严格客户端当成"规范里有这个码"
        去查表 —— 如实只给 HTTP 语义更稳。
        """
        logger.info(
            "a2a: 拒绝未鉴权请求 %s %s（来源 %s）",
            request.method, request.path, request.client_host or "?",
        )
        return HttpResponse.json(
            {"error": "unauthorized"},
            401,
            headers=(("WWW-Authenticate", 'Bearer realm="a2a"'),),
        )

    # --- 非 JSON-RPC 端点 ----------------------------------------------
    def _handle_health(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse.json({
            "status": "ok",
            "agent": self.agent_name,
            "protocolVersion": PROTOCOL_VERSION,
            "pendingTasks": self.stats()["tasks_pending"],
        })

    def _handle_agent_card(self, request: HttpRequest) -> HttpResponse:
        """Agent Card（规范 §4.4.1）。

        必填字段全部给全：``name`` / ``description`` / ``supportedInterfaces`` /
        ``version`` / ``capabilities`` / ``defaultInputModes`` /
        ``defaultOutputModes`` / ``skills``。

        ⚠️ **只写规范里有的字段。** Hermes 的 ``build_agent_card`` 额外塞了顶层
        ``url`` 与 OpenAPI 形状的 ``security``/``securitySchemes``（``{"type":
        "http"}``）；那**不是** v1.0 的形状（§4.4.1 要的是 ``securityRequirements``，
        §4.5.3 要的是 ``httpAuthSecurityScheme`` 包装）。而 Hermes 自己在
        ``protocol.py::build_task`` 里写过"strict ProtoJSON parsers (a2a-sdk)
        reject unknown fields" —— 多写一个非规范字段就可能被严格解析器拒掉。
        """
        server = self._server
        rpc_url = server.url(RPC_PATHS[0]) if server is not None else ""
        card: dict[str, Any] = {
            "name": self.agent_name,
            "description": self.agent_description,
            "version": self.agent_version,
            "supportedInterfaces": [{
                "url": rpc_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": PROTOCOL_VERSION,
            }],
            "provider": {
                "organization": "opencode-bridge",
                "url": rpc_url,
            },
            "capabilities": {
                # 与实现严格一致：§3.3.4 规定未声明的能力被调用时 MUST 回
                # UnsupportedOperationError / PushNotificationNotSupportedError。
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [{
                "id": "opencode-coding-agent",
                "name": "opencode coding agent",
                "description": self.agent_description,
                "tags": ["coding", "opencode", "local"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }],
        }
        if self._auth_enabled:
            # §4.5.3 HTTPAuthSecurityScheme 的字段是 `scheme`；§4.4.1 的字段是
            # `securityRequirements`（示例见规范 §8.5）。
            card["securitySchemes"] = {
                "bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}},
            }
            card["securityRequirements"] = [{"schemes": {"bearer": {}}}]
        return HttpResponse.json(card)

    # ==================================================================
    # JSON-RPC
    # ==================================================================
    def _handle_rpc(self, request: HttpRequest) -> HttpResponse:
        """JSON-RPC 入口（规范 §9）。响应 ``Content-Type: application/json`` ——
        §9.1 对 JSON-RPC 绑定明确写的是 ``application/json``（``application/a2a+json``
        是 §14.1 为 HTTP+JSON/REST 绑定注册的媒体类型）。
        """
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else None
        except (UnicodeDecodeError, ValueError):
            # 畸形 JSON：回 -32700，**绝不**把解析异常或栈泄给对端。
            return _rpc_response(None, ERR_PARSE, "Invalid JSON payload")
        if not isinstance(payload, dict):
            return _rpc_response(None, ERR_INVALID_REQUEST,
                                 "JSON-RPC request must be an object")
        req_id = payload.get("id")

        # 版本协商（§3.2.6 的 A2A-Version 服务参数 -> §5.4 的 VersionNotSupportedError）
        version = (request.header("A2A-Version") or "").strip()
        if version and version not in SUPPORTED_VERSION_HEADERS:
            return _rpc_response(
                req_id, ERR_VERSION_NOT_SUPPORTED,
                f"unsupported A2A-Version: {version} (this agent speaks {PROTOCOL_VERSION})",
            )

        jsonrpc = payload.get("jsonrpc")
        if jsonrpc is not None and jsonrpc != "2.0":
            return _rpc_response(req_id, ERR_INVALID_REQUEST,
                                 "jsonrpc must be \"2.0\"")
        method = payload.get("method")
        if not isinstance(method, str) or not method.strip():
            return _rpc_response(req_id, ERR_INVALID_REQUEST,
                                 "method is required and must be a string")
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _rpc_response(req_id, ERR_INVALID_PARAMS,
                                 "params must be an object")

        peer = request.peer or f"local:{request.client_host or 'unknown'}"
        handler = _METHOD_HANDLERS.get(method)
        if handler is None:
            unsupported = _UNSUPPORTED_METHODS.get(method)
            if unsupported is not None:
                code, why = unsupported
                return _rpc_response(req_id, code, f"{method}: {why}")
            return _rpc_response(
                req_id, ERR_METHOD_NOT_FOUND,
                f"method not found: {method}. This agent implements "
                + ", ".join(sorted(_METHOD_HANDLERS)),
            )
        try:
            return handler(self, req_id, params, peer)
        except Exception:  # noqa: BLE001 - 兜底：绝不让栈泄给对端
            logger.exception("a2a: 处理 %s 失败", method)
            return _rpc_response(req_id, ERR_INTERNAL, "internal error")

    # --- SendMessage ----------------------------------------------------
    def _reject(self, task: "_Task", req_id: Any, reason: str) -> HttpResponse:
        """立刻判终态并把 task 记进账本，让对端拿到的 taskId 之后仍可 ``GetTask``。

        用 ``TASK_STATE_REJECTED``（§4.1.3："the agent has decided to not perform
        the task"）而不是 JSON-RPC 错误：被拒的是**这次任务**，请求本身是合法的。
        """
        self._register(task)
        self._finalize(task, STATE_REJECTED, reason)
        return HttpResponse.json(_rpc_result(req_id, {"task": _render_task(task)}))

    def _rpc_send_message(self, req_id: Any, params: dict, peer: str) -> HttpResponse:
        """``SendMessage``（规范 §3.1.1 / §9.4.1）。"""
        message = params.get("message")
        if not isinstance(message, dict):
            return _rpc_response(req_id, ERR_INVALID_PARAMS,
                                 "params.message (Message) is required")

        referenced_task = str(message.get("taskId") or params.get("taskId") or "").strip()
        claimed_context = extract_context_id(params)

        # §3.4.2：带 taskId 必须指向一个已存在的 task；§13.1：只能看见自己的 task。
        context_id = claimed_context
        if referenced_task:
            # 走 _lookup（持锁 + 校验对端归属），别直接读 _tasks ——
            # 后者会在 stop() 裁剪账本时与别的线程竞争。
            existing = self._lookup(referenced_task, peer)
            if existing is None:
                # 不区分"不存在"与"不是你的"（§3.3.2 明说不应区分，防信息泄漏）。
                return _rpc_response(
                    req_id, ERR_TASK_NOT_FOUND,
                    f"task not found: {referenced_task}",
                )
            if claimed_context and claimed_context != existing.context_id:
                # §3.4.3 明要求拒绝 contextId / taskId 不一致的请求。
                return _rpc_response(
                    req_id, ERR_INVALID_PARAMS,
                    "contextId does not match the contextId of the referenced task",
                )
            if existing.state in TERMINAL_STATES:
                # §3.1.1：给终态 task 发消息必须回 UnsupportedOperationError。
                return _rpc_response(
                    req_id, ERR_UNSUPPORTED_OPERATION,
                    f"task {referenced_task} is in terminal state {existing.state}",
                )
            context_id = existing.context_id
        if not context_id:
            context_id = _new_id("ctx")

        text = extract_text(message)
        task = self._new_task(context_id, peer)

        # --- 闸门在**任何**后续处理之前（T1.2 的调用顺序要求）----------
        # ⚠️ 本平台 :attr:`pairing_supported` = False（principal 是对端自报的
        # peer），配对分支恒不成立；保留那半行是为了与其它平台同一形态。
        if not self.admits(peer) and not self.answer_pairing_request(
            peer, f"{self.name}:{peer}", text
        ):
            logger.info(
                "a2a: 丢弃非白名单对端 %s 的任务（allowed_chat_ids）",
                redactable_id(self.name, peer),
            )
            return self._reject(
                task, req_id,
                "peer not authorised (not in allowed_chat_ids)",
            )

        # --- 防回环：按 contextId 计轮次 --------------------------------
        turn = self._bump_turn(context_id)
        if turn > self.max_turns:
            logger.warning(
                "a2a: 防回环触发 —— context %s 第 %d 轮 > 上限 %d，判定为乒乓",
                context_id, turn, self.max_turns,
            )
            return self._reject(
                task, req_id,
                f"anti-loop protection: context {context_id} exceeded {self.max_turns} "
                f"turns. Start a new context, or raise max_turns if this is legitimate.",
            )

        if not text:
            return self._reject(
                task, req_id, "empty task - no text part to act on"
            )

        # --- 投递 -------------------------------------------------------
        inbound = Inbound(
            conversation_id=self._conversation_id(peer),
            text=text,
            kind="text",
            user_id=peer,
            message_id=str(message.get("messageId") or "").strip() or task.task_id,
            platform=self.name,
            raw={
                "taskId": task.task_id,
                "contextId": context_id,
                "role": str(message.get("role") or ""),
                "mediaTypes": [
                    str(p.get("mediaType") or "") for p in (message.get("parts") or [])
                    if isinstance(p, dict)
                ],
                "metadata": params.get("metadata"),
            },
        )
        task.state = STATE_WORKING

        # §3.2.2：returnImmediately=true 时建好 task 就返回（对端随后用 GetTask 取）。
        configuration = params.get("configuration")
        immediate = isinstance(configuration, dict) and bool(
            configuration.get("returnImmediately")
        )
        if immediate:
            # 先同步登记，好让对端紧接着的 GetTask / ListTasks 能查到这条 task。
            # _register 是幂等的，分发线程随后再调一次不会重复记账。
            self._register(task)
        self._queue.put((task, inbound))
        if immediate:
            return HttpResponse.json(_rpc_result(req_id, {"task": _render_task(task)}))

        # 默认阻塞：等 agent 给出终态（§3.1.1 明确默认等终态）。
        if not task.done.wait(max(0.0, float(self.reply_timeout))):
            self._finalize(task, STATE_FAILED,
                           f"agent did not reply within {self.reply_timeout}s")
        return HttpResponse.json(_rpc_result(req_id, {"task": _render_task(task)}))

    # --- GetTask / ListTasks / CancelTask ------------------------------
    def _rpc_get_task(self, req_id: Any, params: dict, peer: str) -> HttpResponse:
        """``GetTask``（规范 §3.1.3 / §9.4.3）。

        ⚠️ 响应是 **Task 本身**放在 ``result`` 里，**不是** ``{"task": Task}`` ——
        只有 ``SendMessage`` 的响应才是 ``SendMessageResponse`` 那个 oneof
        （§3.1.1 "Outputs: Task ... OR Message"，§9.4.1 的示例写成
        ``result: { task: {...} }``）。这个区别很容易猜错，用例已把它钉死。
        """
        task_id, error = self._task_param(params, req_id)
        if error is not None:
            return error
        task = self._lookup(task_id, peer)
        if task is None:
            return _rpc_response(req_id, ERR_TASK_NOT_FOUND,
                                 f"task not found: {task_id}")
        return HttpResponse.json(_rpc_result(req_id, _render_task(task)))

    def _rpc_list_tasks(self, req_id: Any, params: dict, peer: str) -> HttpResponse:
        """``ListTasks``（规范 §3.1.4）。

        只列**本对端**的 task（§13.1 的授权作用域要求）。``nextPageToken`` 末页必须
        为空串（§3.1.4 明写），``totalSize`` 是分页前的总数。
        """
        try:
            page_size = int(params.get("pageSize", 50))
        except (TypeError, ValueError):
            page_size = 50
        page_size = max(1, min(page_size, 100))          # 规范：min 1 / max 100
        try:
            offset = int(params.get("pageToken", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        wanted_context = str(params.get("contextId") or "").strip()
        wanted_state = str(params.get("status") or "").strip()
        include_artifacts = bool(params.get("includeArtifacts"))

        with self._lock:
            # 规范：按"最后更新时间倒序"。
            rows = [
                self._tasks[tid] for tid in reversed(list(self._order.get(peer, ())))
                if tid in self._tasks
            ]
        rows = [
            t for t in rows
            if (not wanted_context or t.context_id == wanted_context)
            and (not wanted_state or t.state == wanted_state)
        ]
        total = len(rows)
        page = rows[offset:offset + page_size]
        rendered = []
        for task in page:
            payload = _render_task(task)
            if not include_artifacts:
                payload.pop("artifacts", None)     # §3.1.4：false 时 MUST 整个省略
            rendered.append(payload)
        next_token = str(offset + page_size) if offset + page_size < total else ""
        return HttpResponse.json(_rpc_result(req_id, {
            "tasks": rendered,
            "nextPageToken": next_token,
            "pageSize": page_size,
            "totalSize": total,
        }))

    def _rpc_cancel_task(self, req_id: Any, params: dict, peer: str) -> HttpResponse:
        """``CancelTask``（规范 §3.1.5）。终态 task 必须回 -32002 / HTTP 400。

        响应同样是 **Task 直接在 ``result`` 里**（§3.1.5 "Outputs: Updated Task"）。
        """
        task_id, error = self._task_param(params, req_id)
        if error is not None:
            return error
        task = self._lookup(task_id, peer)
        if task is None:
            return _rpc_response(req_id, ERR_TASK_NOT_FOUND,
                                 f"task not found: {task_id}")
        if task.state in TERMINAL_STATES:
            return _rpc_response(
                req_id, ERR_TASK_NOT_CANCELABLE,
                f"task {task.task_id} already {task.state}",
            )
        self._finalize(task, STATE_CANCELED, "")
        return HttpResponse.json(_rpc_result(req_id, _render_task(task)))

    def _task_param(self, params: dict, req_id: Any = None) -> tuple[str, Optional[HttpResponse]]:
        """取 ``id`` 参数（**规范 §3.1.3 / §3.1.5 的字段名就是 ``id``**）。

        返回 ``(task_id, None)`` 或 ``(task_id, 错误响应)``。刻意**不**接受
        ``taskId`` 作为别名：那是 :class:`TaskStatusUpdateEvent` 上的字段名
        （§4.2.1），用在请求参数上就是笔误 —— 静默接受笔误会让"字段名写错"
        这类问题一直到生产环境才暴露。回 -32602 并在消息里点名正确的字段名。

        :param req_id: 错误信封要回显的请求 id —— **必须**带上，否则对端无法把
            这条错误和它的请求对应起来（JSON-RPC 的基本要求）。
        """
        raw = params.get("id")
        if raw is None or not isinstance(raw, (str, int)) or isinstance(raw, bool):
            return "", _rpc_response(
                req_id, ERR_INVALID_PARAMS,
                "params.id (string) is required - see A2A spec 3.1.3 "
                "(note: 'taskId' is a TaskStatusUpdateEvent field, not a request param)",
            )
        return str(raw).strip(), None

    def _lookup(self, task_id: str, peer: str) -> Optional[_Task]:
        """转发 :meth:`A2aTaskLedger._lookup`（task 账本域已抽出）。"""
        return self._task_ledger._lookup(task_id, peer)

    # ==================================================================
    # task 账本（域已抽出 → a2a_task_ledger.A2aTaskLedger；留守转发方法
    # + 状态本体）
    # ==================================================================
    def _conversation_id(self, peer: str) -> str:
        """``conversation_id = a2a:<对端标识>``（一个对端 agent 一个会话）。

        对端标识来自凭据（见 :meth:`_authenticate`），不是请求体 —— 这与
        :mod:`identity` 的"不许静默猜"同源：猜错身份会把对端映射到别人的会话，
        而且不报错、只表现为"agent 突然记错上下文"。
        """
        return format_id(self.name, peer or "local:unknown")

    def _new_task(self, context_id: str, peer: str) -> _Task:
        """转发 :meth:`A2aTaskLedger._new_task`。"""
        return self._task_ledger._new_task(context_id, peer)

    def _register(self, task: _Task) -> None:
        """转发 :meth:`A2aTaskLedger._register`。"""
        self._task_ledger._register(task)

    def _finalize(self, task: _Task, state: str, reply: str = "") -> bool:
        """转发 :meth:`A2aTaskLedger._finalize`。"""
        return self._task_ledger._finalize(task, state, reply)

    def _fail_pending(self, state: str, reason: str) -> int:
        """转发 :meth:`A2aTaskLedger._fail_pending`。"""
        return self._task_ledger._fail_pending(state, reason)

    # ==================================================================
    # 防回环：轮次计数
    # ==================================================================
    def _bump_turn(self, context_id: str) -> int:
        """记一次 ``contextId`` 的入站轮次，返回累计值（顺便淘汰空闲项）。"""
        now = time.time()
        with self._lock:
            for key in [k for k, (_, seen) in self._turns.items()
                        if now - seen > TURN_TTL_SECONDS]:
                self._turns.pop(key, None)
            count = self._turns.get(context_id, (0, now))[0] + 1
            self._turns[context_id] = (count, now)
            return count

    def reset_turns(self, context_id: str) -> None:
        """清掉某上下文的轮次计数（例如用户在桥接里 ``/new`` 之后）。"""
        with self._lock:
            self._turns.pop(context_id, None)

    # ==================================================================
    # 分发线程
    # ==================================================================
    def _dispatch_loop(self) -> None:
        """消费队列、调 :meth:`Hooks.on_inbound`。

        **为什么要这一层间接**：core 选适配器时有一条启发式 —— "调用
        ``on_inbound`` 的线程就是该适配器的 poller 线程"（见 ``core._adapter_for``）。
        ``ThreadingHTTPServer`` 是**每连接一线程**，直接在 handler 线程里调
        ``on_inbound`` 会让那条启发式落空（core 会回落到"第一个适配器"，
        很可能发错平台）。挂在适配器自己拥有的线程上，core 才能认出来。

        这一层顺带给出两个好处：入站**按到达顺序**被消费（同一对端的请求不会乱序），
        以及 hook 抛异常不会波及 HTTP 响应。

        停机后**不再投递**：``stop()`` 已把在途任务判失败，但队列里可能还压着几条
        （handler 线程已经收到请求、还没走到投递那一步）。它们必须被判失败而不是
        触发一次 agent 运行 —— 桥接正在关闭时还启动 agent 运行是最难解释的行为。
        """
        while True:
            item = self._queue.get()
            if item is None:
                self._cancel_queued()
                return
            task, inbound = item
            if self._stop_event.is_set():
                self._register(task)
                self._finalize(task, STATE_CANCELED, "[bridge shutting down]")
                continue
            try:
                self._register(task)
                self.hooks.on_inbound(inbound)
            except Exception as exc:  # noqa: BLE001 - 不许让 hook 的 bug 毁掉响应
                logger.exception("a2a: on_inbound 失败（任务判失败）: %s", exc)
                self._finalize(task, STATE_FAILED, f"internal dispatch error: {exc}")

    def _cancel_queued(self) -> int:
        """分发线程退出时，把队列里**还没投递**的任务全部判失败。

        没有这一步它们会永远留在账本里处于非终态：``send()`` 找不到等待者（返回
        None + ``not_found``），而对端那边的 HTTP 请求已经没人管了。
        """
        count = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:          # 多个 stop() 时的多余哨兵
                continue
            task, _inbound = item
            self._register(task)
            if self._finalize(task, STATE_CANCELED, "[bridge shutting down]"):
                count += 1
        return count

    # ==================================================================
    # 出站（= 把答复交给正在等待的 HTTP 请求）
    # ==================================================================
    def send(self, out: Outbound) -> MsgHandle | None:
        """把 agent 的文本交给**正在等待的** ``SendMessage`` 请求。

        A2A 的出站**不发网络请求**：这是规范 §3.1.1 阻塞语义的另一半 —— 对端发的
        ``message/send`` 还在等，我们把终态直接还给它。

        ``kind`` 的处理（与 core 的发送序列严格对应，见 ``core._dispatch_prompt`` /
        ``_finalize``）：

        * ``progress`` —— **绝不**据此判定任务完成。core 在 ``prompt()`` 返回后会先发
          一条进度占位消息，而本适配器 ``edit()`` 恒 ``False``，所以最终答复一定
          是**另一次** ``send()``。若在这里就完成任务，对端只会收到"处理中…"。
        * 其余走 :data:`_TASK_STATE_BY_OUTBOUND_KIND` 这张**穷举**表，表分两档：
          终态档 —— ``text`` / ``final`` -> ``TASK_STATE_COMPLETED``，
          ``error`` -> ``TASK_STATE_FAILED``，
          ``cancelled`` -> ``TASK_STATE_CANCELED``（用户主动 ``/new`` 丢掉那一轮）；
          非终态档 —— 三条缓冲回执 ``buffered`` / ``held_expired`` /
          ``held_not_delivered``（``inbound_merge`` 的 ``..`` / ``!!`` 续行）映射
          ``TASK_STATE_WORKING``：只确认「已收到，答复还没开始」，**不判终态**、
          不唤醒阻塞者（2026-10-10 修复：此前三条回执共用 ``kind="text"`` ⇒ 对端在
          agent 还没跑时就收到 task completed）。
        * ⛔ **表里没有的 kind 一律不判终态**，只记一次 ``bad_format`` 失败。
          此前这里是 ``STATE_FAILED if out.kind == "error" else STATE_COMPLETED``
          ⇒ 未知 kind **静默变成"完成"**（用户 2026-10-07 拍板改掉）。
        """
        peer_key = _local_of(out.conversation_id)
        with self._lock:
            order = self._order.get(peer_key)
            task = None
            if order:
                for task_id in order:                 # FIFO：取最老的未完成任务
                    candidate = self._tasks.get(task_id)
                    if candidate is not None and candidate.state not in TERMINAL_STATES:
                        task = candidate
                        break
                if task is None and order:           # 没人等 -> 落到最近一条上（供 GetTask）
                    for task_id in reversed(order):
                        candidate = self._tasks.get(task_id)
                        if candidate is not None:
                            task = candidate
                            break

        if out.kind == "progress":
            if task is None:
                logger.debug("a2a: progress 消息没有对应任务（%s）", out.conversation_id)
                self._note_send_failure(SendError.NOT_FOUND, "no pending a2a task")
                return None
            return MsgHandle(out.conversation_id, task.task_id, self.name)

        if task is None:
            # 没有任何 task 可交付 —— 对端多半已经超时走了。如实记失败，
            # 不要假装成功（core 的 --status 靠这个分类）。
            logger.info(
                "a2a: 收到一条无处交付的回复（conversation=%s kind=%s）",
                out.conversation_id, out.kind,
            )
            self._note_send_failure(
                SendError.NOT_FOUND, "no pending a2a task for this conversation"
            )
            return None

        state = _TASK_STATE_BY_OUTBOUND_KIND.get(out.kind)
        if state is None:
            # ⚠️ **未知 kind 不判终态，只记账**。
            # ⚠️ **这一条是【编排者】裁定的，用户没参与** —— 它是一次**行为变更**，
            # 而「用户拍板」的措辞会让下一个人以为它有用户授权。
            #
            # 此前这一行是 ``STATE_FAILED if out.kind == "error" else
            # STATE_COMPLETED`` ⇒ **任何没被显式列出的 kind 都被报成"完成"**。
            # 那正是本缺陷的形状：``cancelled`` 落进来时，对端拿到的是一个
            # ``TASK_STATE_COMPLETED``，即"agent 正常答完了"—— 而用户刚把那一轮丢掉。
            #
            # ⛔ 这里**不用** ``raise``：``send`` 是出站通路，一个不认识的 kind
            # 不该让整条桥崩掉（与 :meth:`~opencode_bridge.outbound.
            # OutboundSender._record_outcome` 的同一条纪律）。
            # ⇒ 代价（已知且刻意）：那个 task **留在非终态**，等
            # ``reply_timeout`` 到点后由超时那一支判 ``STATE_FAILED``
            # （"did not reply"）。也就是说一个不认识的 kind 从"立刻假成功"变成
            # "响亮地失败 + ``--status`` 里看得见的 ``bad_format``" —— 这是要的。
            logger.warning(
                "a2a: 未知的出站 kind=%r（conversation=%s）—— 不判定任何终态，"
                "只记失败；新增 kind 时必须登记在 _TASK_STATE_BY_OUTBOUND_KIND",
                out.kind, out.conversation_id,
            )
            self._note_send_failure(
                SendError.BAD_FORMAT,
                "unknown outbound kind: %r" % (out.kind,),
            )
            return None
        if state not in TERMINAL_STATES:
            # ⚠️ 表项指向非终态 = 缓冲回执（``inbound_merge`` 的 ``..`` / ``!!``）。
            # ⛔ **不走** :meth:`_finalize`：它会 ``task.done.set()`` 唤醒阻塞中的
            # ``SendMessage`` —— 对端就提前拿到一个非终态 Task 返回（违反 §3.1.1
            # 「默认等终态」），且 ``task.reply`` 会被回执文本**覆盖**，真正的答复
            # 到达时反而写不进已醒的等待者。
            # ⇒ 什么都不碰：task 留在 WORKING，等 ``text`` / ``final`` 到达，或
            # ``reply_timeout`` 到点判 ``TASK_STATE_FAILED``（响亮失败）。
            # ⚠️ 知情代价：阻塞语义下没有「中途消息」通道（推送订阅未实现），回执
            # 文本**不送达** a2a 对端 —— 它能感知的只有任务仍在 WORKING。
            return MsgHandle(out.conversation_id, task.task_id, self.name)
        self._finalize(task, state, out.text or "")
        return MsgHandle(out.conversation_id, task.task_id, self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """**恒返回 ``False``**：A2A 规范里没有"编辑已发消息"这个操作。

        核实过 A2A v1.0 §3.1 Core Operations 的全部 11 个操作（Send Message /
        Send Streaming Message / Get Task / List Tasks / Cancel Task / Subscribe to
        Task / push notification 配置的 create·get·list·delete / Get Extended Agent
        Card）—— **没有任何 update/edit 类操作**。能改的只有 ``Artifact``，而它是通过
        ``TaskArtifactUpdateEvent`` **增量追加**的（§4.2.2），语义不是"改上一条"。

        返回 ``False`` 让 core 退化成"再发一条"（见 ``core._finalize``），这才是
        A2A 语义下唯一正确的行为 —— 假装成功会让对端拿到两份内容。
        """
        logger.debug("a2a: A2A 无编辑消息能力，调用方应改为发送新消息")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """A2A 没有 inline 按钮 / callback query 概念（§4.1.6）—— no-op。"""
        return None


# ----------------------------------------------------------------------
# 方法表
# ----------------------------------------------------------------------
#: 实现的 4 个方法（规范 §5.3 的 JSON-RPC 列）。
_METHOD_HANDLERS = {
    "SendMessage": A2aAdapter._rpc_send_message,
    "GetTask": A2aAdapter._rpc_get_task,
    "ListTasks": A2aAdapter._rpc_list_tasks,
    "CancelTask": A2aAdapter._rpc_cancel_task,
}

#: 未实现但**必须**回错的 7 个方法（规范 §3.3.4「能力校验」）。
#:
#: 之所以要显式列出而不是"统统 -32601 方法不存在"：规范要求的是
#: ``UnsupportedOperationError`` / ``PushNotificationNotSupportedError``
#: —— 客户端据此知道"换个 agent"，而不是"这个 agent 版本太老"。回错码的成本是 7 行。
_UNSUPPORTED_METHODS: dict[str, tuple[int, str]] = {
    "SendStreamingMessage": (
        ERR_UNSUPPORTED_OPERATION,
        "streaming is not supported (AgentCard.capabilities.streaming = false)",
    ),
    "SubscribeToTask": (
        ERR_UNSUPPORTED_OPERATION,
        "streaming is not supported (AgentCard.capabilities.streaming = false)",
    ),
    "GetExtendedAgentCard": (
        ERR_UNSUPPORTED_OPERATION,
        "extendedAgentCard is not configured "
        "(AgentCard.capabilities.extendedAgentCard = false)",
    ),
    "CreateTaskPushNotificationConfig": (
        ERR_PUSH_NOT_SUPPORTED,
        "push notifications are not supported "
        "(AgentCard.capabilities.pushNotifications = false)",
    ),
    "GetTaskPushNotificationConfig": (
        ERR_PUSH_NOT_SUPPORTED,
        "push notifications are not supported "
        "(AgentCard.capabilities.pushNotifications = false)",
    ),
    "ListTaskPushNotificationConfigs": (
        ERR_PUSH_NOT_SUPPORTED,
        "push notifications are not supported "
        "(AgentCard.capabilities.pushNotifications = false)",
    ),
    "DeleteTaskPushNotificationConfig": (
        ERR_PUSH_NOT_SUPPORTED,
        "push notifications are not supported "
        "(AgentCard.capabilities.pushNotifications = false)",
    ),
}
