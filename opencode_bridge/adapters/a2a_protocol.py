"""A2A 协议常量与 wire 纯函数域（§5 拆分：自 ``adapters/a2a.py`` 逐字移植）。

只承载**与适配器状态无关**的两段：协议常量表（状态 / 错误码 / 路径 / 限额 / 版本，
出处逐条注明）与 wire 纯函数（JSON-RPC 信封、Message/Task 渲染、入站文本抽取）。
``adapters/a2a.py`` 顶部再进口全部名字 ⇒ ``from opencode_bridge.adapters.a2a import X``
与 ``a2a.X`` 的既有路径与语义不变（§5 拆分纪律：行为零变化）。

依赖方向：``adapters.a2a`` → 本模块；本模块零回 import 适配器（无回环）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..httpsrv import HttpResponse, MAX_BODY_BYTES



# ======================================================================
# 协议常量 —— 逐条注明出处，避免"凭记忆写字段名"
# ======================================================================
#: 本适配器实现的协议版本。Agent Card 的 ``supportedInterfaces[].protocolVersion``
#: 与 :data:`SUPPORTED_VERSION_HEADERS` 都用它。
PROTOCOL_VERSION = "1.0"

#: ``AgentCard`` 必须用 ``TASK_STATE_*`` 字面量（规范 §4.1.3 逐个列出）。
STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_WORKING = "TASK_STATE_WORKING"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"
STATE_CANCELED = "TASK_STATE_CANCELED"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
STATE_REJECTED = "TASK_STATE_REJECTED"

#: 终态集合（规范 §3.1.1 把这四个列为 terminal）。
TERMINAL_STATES = frozenset(
    {STATE_COMPLETED, STATE_FAILED, STATE_CANCELED, STATE_REJECTED}
)

#: ``Outbound.kind`` -> 本适配器据它判定的任务状态。**穷举**，**刻意不设兜底**
#: （理由见 :meth:`A2aAdapter.send` 里那段注释）。
#:
#: ⛔ ``progress`` **不在**表里：它不碰状态 —— 进 :meth:`A2aAdapter.send` 必须先返回
#: 句柄而**不**碰状态（core 拿那个句柄去改写占位消息），所以它由方法开头那一支处理。
#:
#: 表的键分两档：
#: * **终态档**（值 ∈ :data:`TERMINAL_STATES`）：真正的答复 / 失败 / 取消，走
#:   :meth:`_finalize` —— 判终态并唤醒阻塞中的 ``SendMessage``。
#: * **非终态档**：``inbound_merge`` 的三条缓冲回执（用户只敲 ``..`` / ``!!``）——
#:   它们只告知「已收到，答复还没开始」，⛔ 绝不判终态（判了 = 对端在 agent 还没跑
#:   时就收到 task completed，2026-10-10 修复）。:meth:`A2aAdapter.send` 对非终态档
#:   **不碰状态、不唤醒、不改写 ``task.reply``**。
#:
#: ⚠️ **新增 kind 时必须在这里加一行**：`tests/test_a2a.py` 里有一条断言把本表
#: （含每个键映射到哪个状态）钉成一个字面集合，另有一条 AST 覆盖面守门把生产里
#: 写出的每个 ``kind=`` 字面量对本表登记 —— 只改一边，那条断言会红并指出缺哪一行
#: （比"静默变成 COMPLETED"好得多）。
_TASK_STATE_BY_OUTBOUND_KIND = {
    "text": STATE_COMPLETED,
    "final": STATE_COMPLETED,
    "error": STATE_FAILED,
    #: 用户主动丢弃那一轮（``/new`` / ``/reset`` / ``/cd``）—— 既不是完成也不是失败，
    #: A2A 规范 §4.1.3 为此列了 ``TASK_STATE_CANCELED``。
    "cancelled": STATE_CANCELED,
    #: ⬇ 非终态档：三条缓冲回执。2026-10-10 修复前它们共用 ``kind="text"`` ⇒ 对端在
    #: agent 还没跑时就收到 task completed。值 = 任务此刻实际所处的非终态：工作仍在
    #: 等真正的答复（``text`` / ``final``），或等 ``reply_timeout`` 到点判
    #: ``TASK_STATE_FAILED``（响亮失败，而不是假成功）。
    "buffered": STATE_WORKING,
    "held_expired": STATE_WORKING,
    "held_not_delivered": STATE_WORKING,
}

#: ``Message.role``（规范 §4.1.5）。
ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

# --- 错误码：JSON-RPC 标准码（规范 §9.5）---
ERR_PARSE = -32700                 # Invalid JSON payload
ERR_INVALID_REQUEST = -32600        # Request payload validation error
ERR_METHOD_NOT_FOUND = -32601       # Method not found
ERR_INVALID_PARAMS = -32602         # Invalid parameters
ERR_INTERNAL = -32603               # Internal error

# --- 错误码：A2A 自有码（规范 §5.4 的权威映射表）---
ERR_TASK_NOT_FOUND = -32001         # -> HTTP 404
ERR_TASK_NOT_CANCELABLE = -32002    # -> HTTP 400
ERR_PUSH_NOT_SUPPORTED = -32003     # -> HTTP 400
ERR_UNSUPPORTED_OPERATION = -32004  # -> HTTP 400
ERR_CONTENT_TYPE_NOT_SUPPORTED = -32005   # -> HTTP 400
ERR_VERSION_NOT_SUPPORTED = -32009  # -> HTTP 400

#: 错误码 -> HTTP 状态码。**只有**规范给了映射的才写死（§5.4 表）；JSON-RPC 标准码
#: 规范未给 HTTP 映射，用 200（请求本身是合法 JSON-RPC，只是方法/参数不对）。
_ERROR_HTTP_STATUS: dict[int, int] = {
    ERR_TASK_NOT_FOUND: 404,
    ERR_TASK_NOT_CANCELABLE: 400,
    ERR_PUSH_NOT_SUPPORTED: 400,
    ERR_UNSUPPORTED_OPERATION: 400,
    ERR_CONTENT_TYPE_NOT_SUPPORTED: 400,
    ERR_VERSION_NOT_SUPPORTED: 400,
    ERR_PARSE: 400,
    ERR_INTERNAL: 500,
}

#: Agent Card 发现路径（规范 §8.2 / §14.3 "URI suffix: agent-card.json"）。
AGENT_CARD_PATH = "/.well-known/agent-card.json"
#: v0.x 时代的发现路径。**仍然应答**，但这是兼容性别名，不是规范路径。
LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"
#: JSON-RPC 端点。规范 §9.3 的示例是 ``POST /rpc``；同时接受 ``/``（不少客户端
#: 直接打基址），两个路径等价。
RPC_PATHS: tuple[str, ...] = ("/rpc", "/")
#: 存活探针。**不是**协议的一部分，只是让"服务在不在"可观测（本仓库一贯做法）。
HEALTH_PATH = "/health"

#: ``A2A-Version`` 服务参数（规范 §3.2.6）我们接受的值。其它值按 §5.4 回
#: ``VersionNotSupportedError``（-32009）。
SUPPORTED_VERSION_HEADERS = frozenset({"1.0", "1.0.0"})


# ======================================================================
# 本平台配置常量
# ======================================================================
#: :attr:`A2aAdapter.max_message_length`。
#:
#: ⚠️ **A2A 规范没有给消息长度上限。** 已逐节核对 A2A v1.0 规范全文，唯一相关的一句
#: 是 §13.4 "Input Validation" 里的 *"Agents **SHOULD** implement appropriate limits
#: on message sizes, file sizes, and request complexity"* —— 是 SHOULD，不是硬数字；
#: 全文再无第二个长度约束（§3.1.4 的 ``pageSize`` 上限 100 是条数不是字符）。
#:
#: 所以这里**不编一个"权威数字"**，而是取**我们自己真正兑现的上限**：请求体上限
#: :data:`MAX_BODY_BYTES`（1 MiB）。也就是说超过它的出站内容对端基本收不下，分片
#: 也没有意义；低于它的都不受本平台限制。**明显偏大是故意的** —— 在一个规范没规定
#: 的地方假装有个权威上限，会让 ``split_text`` 在没必要的地方把答案切碎。
MESSAGE_LIMIT = MAX_BODY_BYTES

#: 等待 agent 给出终态的秒数。规范 §3.1.1：``message/send`` 默认**阻塞**，直到 task
#: 进入终态，所以这个超时直接决定对方的 HTTP 请求会挂多久。
DEFAULT_REPLY_TIMEOUT = 300.0

#: 每个 ``contextId`` 允许的入站轮次上限（乒乓保护）。超过回 ``TASK_STATE_REJECTED``。
DEFAULT_MAX_TURNS = 5
#: 上限的硬顶（防止有人配成 100000 把防回环关掉）。与 Hermes 的
#: ``_HARD_MAX_PINGPONG`` 同值。
HARD_MAX_TURNS = 20
#: 轮次计数的空闲淘汰时间。``contextId`` 是对端给的、不受我们控制，不能无限增长。
TURN_TTL_SECONDS = 3600.0

#: 内存里保留的 task 记录上限（含终态）。超了按 FIFO 丢最老的**终态**记录。
#: 为什么必须封顶：每个入站请求留一条，只增不减会把内存吃光。
MAX_TASKS = 512

#: Agent Card 的 ``version`` 字段（规范 §4.4.1 标 Required）。默认对齐
#: ``pyproject.toml`` 的 ``version``；可用 ``agent_version`` 覆盖。
BRIDGE_VERSION = "0.1.0"

#: Agent Card 上的 ``description``（规范 §4.4.1 的必填字段）。
DEFAULT_AGENT_DESCRIPTION = (
    "opencode-bridge: a local coding agent exposed over the A2A protocol."
)

#: :func:`_coerce_port` 用它表示「**未配置**」（与"配了 0"严格分开，见那里）。
#: 抽成常量是因为**两处**必须对这个约定用同一个数：``__init__`` 解析出来的那一个，
#: 以及 :meth:`A2aAdapter.config_runnable` 回答「这份配置够不够跑」时强制出来的
#: 那个缺省值。各自写一个字面量 ``-1`` 时，改动只会落在一处 ⇒ 两处悄悄不再一致
#: ⇒ 判定说"配好了"而 ``start()`` 打 ``port < 0`` 报错不绑定（本缺陷的形态）。
UNCONFIGURED_PORT = -1
#: 合法端口区间（与 :func:`_coerce_port` 逐字一致：``0 <= port <= 65535``）。
MIN_BIND_PORT = 0
MAX_BIND_PORT = 65535


def _default_agent_name() -> str:
    """Agent Card 的默认名字。

    带上主机名是 A2A 的惯例：同一台机器上可能跑着多个 agent，对端（和用户看
    ``--status`` 时）要能靠名字区分服务方。取不到主机名就退化成一个固定串 ——
    **不抛异常**：起个 Agent Card 不该因为 ``gethostname()`` 失败而起不来。
    """
    import socket

    try:
        host = socket.gethostname().strip()
    except Exception:  # pragma: no cover - 防御
        host = ""
    return f"opencode-bridge@{host}" if host else "opencode-bridge"


#: Agent Card 上的 ``name`` 默认值（见 :func:`_default_agent_name`）。每次 import
#: 求值一次即固定值；想让每个实例拿到当时的主机名，用 ``agent_name`` 配置项覆盖。
DEFAULT_AGENT_NAME = _default_agent_name()


def _now_iso() -> str:
    """ISO 8601 UTC 毫秒时间戳（规范 §4.1.2 的例子是 ``2023-10-27T10:00:00Z``）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id(prefix: str) -> str:
    """服务端生成的任务 / 上下文标识（规范 §3.4.2 要求 taskId 由服务端生成）。"""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _rpc_result(req_id: Any, result: Any) -> dict:
    """JSON-RPC 成功信封（规范 §9.4）。"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict:
    """JSON-RPC 错误信封（规范 §9.5）。

    ⚠️ 刻意**不带** ``error.data``：规范 §9.5 说 data 数组里每个对象 **MUST** 含
    ``@type``（google.rpc 的 ProtoJSON ``Any``）。我们没有 google.rpc 类型可填，硬塞
    一个自造结构比省略更糟 —— 严格 ProtoJSON 解析器会直接报错。
    """
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _rpc_response(req_id: Any, code: int, message: str) -> HttpResponse:
    """错误码 -> HTTP 响应（状态码见 :data:`_ERROR_HTTP_STATUS`）。"""
    status = _ERROR_HTTP_STATUS.get(code, 200)
    return HttpResponse.json(_rpc_error(req_id, code, message), status)


def _text_part(text: str) -> dict:
    """``Part``：规范 §4.1.6 的 oneof 之一 ``text`` + 可选 ``mediaType``。"""
    return {"text": text, "mediaType": "text/plain"}


def _message(role: str, text: str, context_id: str = "", message_id: str = "") -> dict:
    """``Message``（规范 §4.1.4 的必填字段只有 ``messageId`` / ``role`` / ``parts``）。"""
    msg: dict[str, Any] = {
        "messageId": message_id or uuid.uuid4().hex,
        "role": role,
        "parts": [_text_part(text)],
    }
    if context_id:
        msg["contextId"] = context_id
    return msg


def _render_task(task: "_Task") -> dict:
    """把内部 task 记录渲染成规范的 ``Task``（规范 §4.1.1）。

    ``artifacts`` 只在真的有回答时带（规范说它是 Optional）。``history`` 我们不填 ——
    桥接的对话历史在 opencode 侧（按 ``conversation_id`` 存 ``state.json``），
    在这里复制一份只会两处失同步。
    """
    status: dict[str, Any] = {"state": task.state, "timestamp": _now_iso()}
    if task.reply:
        status["message"] = _message(ROLE_AGENT, task.reply, task.context_id)
    out: dict[str, Any] = {
        "id": task.task_id,
        "contextId": task.context_id,
        "status": status,
    }
    if task.reply:
        out["artifacts"] = [{
            "artifactId": uuid.uuid4().hex,
            "name": "response",
            "parts": [_text_part(task.reply)],
        }]
    return out


def extract_text(message: Any) -> str:
    """从 ``Message`` 里抽出可交给 agent 的纯文本。

    规范 §4.1.6 的 ``Part`` 是 oneof（``text`` / ``raw`` / ``url`` / ``data``）。
    桥接只处理文本，另外三种渲染成**短提示**而不是丢掉 —— 丢掉的话对端发了文件
    我们会表现成"收到一条空消息"，那是最难查的一种故障。

    非文本 part **不下载、不解码**：``raw`` 是 base64，本适配器不替对端把二进制塞进
    agent 的上下文。
    """
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, (list, tuple)):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str) and part["text"]:
            chunks.append(part["text"])
            continue
        filename = str(part.get("filename") or "")
        media = str(part.get("mediaType") or "")
        if isinstance(part.get("url"), str) and part["url"]:
            label = filename or "file"
            chunks.append(f"[file: {label}] {part['url']}")
        elif isinstance(part.get("raw"), str) and part["raw"]:
            # base64 长度不是字节数，如实说是字符数，别谎报字节。
            label = filename or "file"
            chunks.append(f"[file: {label}] base64, {len(part['raw'])} chars")
        elif part.get("data") is not None:
            try:
                rendered = json.dumps(part["data"], ensure_ascii=False)
            except (TypeError, ValueError):
                rendered = str(part["data"])
            chunks.append(f"[data ({media or 'application/json'})] {rendered}")
    return "\n".join(chunks).strip()


def extract_context_id(params: Any) -> str:
    """从 ``SendMessageRequest`` 取 ``contextId``（规范 §3.2.1：它在 ``message`` 里）。"""
    if not isinstance(params, dict):
        return ""
    message = params.get("message")
    if isinstance(message, dict) and message.get("contextId"):
        return str(message["contextId"]).strip()
    return str(params.get("contextId") or "").strip()
