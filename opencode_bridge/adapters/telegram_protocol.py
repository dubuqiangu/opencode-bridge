"""Lane B — Telegram 协议原语域（§5 第八刀，自 ``telegram.py`` 逐字移植）。

Telegram Bot API 的**协议限额与 wire 纯函数**：消息/回调字节上限、轮询与退避
节拍、凭据闸门退避阶梯，以及两个不依赖适配器状态的纯函数 —— 429
``retry_after`` 解析（:func:`_retry_after`）与内联键盘按钮序列化
（:func:`_button_row`）。

## 为什么拆（刀路依据 = 缝质量与零钉点，与第五刀 a2a 同型）

``telegram.py`` 1495 物理行里，模块级协议域与 ``TelegramAdapter`` 类体是两份
可分开的职责；消费面普查（2026-10-14）证实这 19 个名字在 tests/ 里全是
import 形态、零 ``mock.patch`` 钉点 ⇒ 缝干净，照 ``ws_protocol.py`` /
``a2a_protocol.py`` 先例成模块。

⚠️ ``telegram.py`` 顶部再进口块保既有 import 路径（7 个测试模块
``from opencode_bridge.adapters.telegram import …``）与 ``telegram.X`` 属性语义；
``status_failure_view.py`` 对 ``RUNTIME_CREDENTIAL_REVERIFY_LIMIT`` 的 ``:data:``
文档角色引用同样经再进口继续可解析。

⚠️ ``_button_row`` docstring 里的「本文件 ``edit()``」在移植后指
``telegram.py``（调用方所在文件）；docstring 逐字保留（AST 同一性），指称以
本段为准。
"""

from __future__ import annotations

from typing import Any, Optional

from ..hooks import Button

API_HOST = "api.telegram.org"
API_PORT = 443
MESSAGE_LIMIT = 4096          # Telegram max message length in characters
CALLBACK_DATA_LIMIT = 64      # inline callback_data limit in bytes
#: 部分发送（前段已上线、后段失败）时追加给读者的缺口标记 —— 用户拍板 2026-10-09
#: 「加缺口标记」：读者必须知道拿到的答复不完整，不能只有日志与 ``--status`` 知道。
SEND_GAP_MARKER_TEXT = (
    "—— ⚠️ 以上回复不完整：后续部分发送失败，未送达。如需完整内容请重新提问。"
)
MIN_SEND_INTERVAL = 1.2       # per-conversation send/edit throttle (seconds)
BACKOFF_INTERVAL = 2.0        # 一次 getUpdates 失败后的重试间隔（秒，恒定）
POLL_LONG_TIMEOUT = 25        # getUpdates long-poll seconds
POLL_TIMEOUT_UPPER_LIMIT = 50  # Bot API 的服务端挂起上限（50s）：超过即回落默认并告警
POLL_SOCKET_TIMEOUT = 40.0    # socket timeout must be > POLL_LONG_TIMEOUT
#: 空轮（``ok`` 但 ``result`` 为空）后的防御性节流（秒）。
#: 迁移前是 :meth:`TelegramAdapter._poll_once` 末尾的 ``_stop_event.wait(0.05)``：
#: 服务端立刻返回空批时不做任何等待会打爆 API。走传输层后它就是
#: ``PollingTransport`` 的 ``idle_sleep``（仍然是**可被 stop 打断**的
#: ``Event.wait``，不是 ``time.sleep``）。
EMPTY_ROUND_INTERVAL = 0.05
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0        # cap for Telegram 429 retry_after sleeps

#: Telegram 的 ``Unauthorized`` 码。**只有它**会让运行期的凭据失效判据成立。
#:
#: ⚠️ 为什么只认 401 而不认 403/404：Bot API 的 403 是"bot 被拉黑 / 无权访问该聊天"，
#: 404 是"方法或聊天不存在"，而 ``getUpdates`` 用的是**同一个 token** ⇒ 401 才是
#: "这个 token 现在不被承认"这一件事的机器可读形式。
#: ⛔ 而"非 401 的失败会不会也是 token 吊销"**未核实**（本机无外网凭据）⇒ 所以按
#: **语义**收窄，而不是"任何失败都当成凭据失效"（那会让一次网络抖动去惊动凭据闸门）。
UNAUTHORIZED_ERROR_CODE = 401

#: 运行期重新探 ``getMe`` 的**重试上界**（§5.1 第一刀后见
#: :meth:`TelegramCredentialLifecycle._reverify_runtime_credentials`）。
#:
#: ⭐ **为什么是 5 次**（2026-10-08 拍板时的取舍，写在这里是为了可复核）：
#: 这一段重试与启动段那个**永不放弃**的阶梯是**两件事**，判据不同 ——
#:
#: * 启动段永不放弃，是因为"用户在**进程活着**的期间第一次配 token"是常态；
#: * 运行段的触发条件是"**曾经好过、现在 401 了**" ⇒ 用户极可能正在**看日志**，
#:   而每一次重试都打一行 —— 无界的重试在这里等于**刷屏**，把真正的排障现场淹掉。
#:
#: ⇒ 所以运行段给一个**明确的次数上界**，超了之后那一行会**说清**「已停止重探、
#: 需要重启桥」并**保留** :attr:`TelegramCredentialLifecycle._runtime_credentials_invalid`（⇒ ``running``
#: 继续为假、入站继续不放行，**不会**因为放弃重探而偷偷恢复）。
#: ⇒ 5 次 × 最长一档 60s ≈ **5 分钟**的自动恢复窗口：覆盖"token 被临时吊销后
#: 几分钟内换回来"这一类，而不会让一个永久吊销的 token 被探上一整夜。
RUNTIME_CREDENTIAL_REVERIFY_LIMIT = 5

# ---------------------------------------------------------------------------
# 「凭据闸门」（``getMe`` 探测）的退避阶梯
# ---------------------------------------------------------------------------
# ⚠️ **初值是候选值，不是标定出来的**：它**复用**本仓库已有的 2s
# （:data:`BACKOFF_INTERVAL`，也正是 ``PollingTransport`` 在 ``min_backoff ==
# max_backoff`` 下唯一会取的那个数），**没有**任何实测数据支撑。
# ⇒ **标定它需要哪一组数**：**恢复时延的分布** —— 首次成功时打的那行
# 「第 N 次尝试 / 历时 T 秒」（:meth:`TelegramCredentialLifecycle._announce_credential_recovery`）
# 就是为了攒这组数：攒够 **≥20 个真实事件**、其中**至少 3 次**是「失败超过 5 分钟
# 才恢复」，再看 N 与 T 的 **p50 / p90**，再回头改这两个数。
# ⛔ 在那之前**不许**把它当成"测过了"。
# ⚠️ 「无抖动」同理是**借用**既有做法而不是结论：本仓库 5 套阶梯**一套都没有**
# 抖动，且 Telegram 的限流按 bot token 计、多实例之间不共享配额。
CREDENTIAL_PROBE_INITIAL_BACKOFF = 2.0
#: 封顶 60s = 传输层的默认值（``Transport.__init__`` 的 ``max_backoff``），
#: 也是 5 个适配器的既有取值 ⇒ 「永不放弃」的代价有界：最坏每分钟一次 ``getMe``。
CREDENTIAL_PROBE_MAX_BACKOFF = 60.0


def _retry_after(data: Any) -> Optional[float]:
    """Extract a 429 ``retry_after`` delay from a Telegram response body.

    ⚠️ **门控是 ``error_code == 429``，不是「``ok`` 为 false」**（2026-10-08 修）。
    改之前的门控是「只要带得出 ``parameters.retry_after`` 就照等」，于是
    ``{"error_code": 400, "parameters": {"retry_after": 7}}`` 会返回 **7.0** ——
    而 400 是**请求本身不合法**，睡 7 秒再原样重试一次，100% 还是 400
    ⇒ 纯浪费一轮墙钟，且那条 ``rate-limited`` 告警会指向一个根本没被限流的调用。

    ⚠️ **为什么按语义收窄而不是「来者不拒」**：「非 429 却带 ``retry_after`` 的
    响应在生产里会不会出现」**未核实** —— 本机无外网凭据、无法对真实平台取值。
    正因为不知道，就更不该让一个**语义上与限流无关**的字段决定我们睡多久
    （AGENTS.md §8 第 3 条：靠一个没被记录的事实去猜，必然在某些情况下猜错）。
    ⇒ 门控答的是「这个码是不是限流」，而不是「来者不拒」。

    ⛔ **畸形输入的安全行为逐项保留**（它们各自都有专测）：没有 ``parameters``
    且码是 429 ⇒ **1.0**（默认等一秒）；``retry_after`` 是负数 / 布尔 / 字符串 /
    非数值 ⇒ **``None``**（调用方**不许**当成 0 秒，那会变成立刻重试的紧循环）；
    ``data`` 压根不是 dict ⇒ ``None``。
    """
    if not isinstance(data, dict):
        return None
    if data.get("error_code") != 429:
        return None
    params = data.get("parameters")
    delay: Any = None
    if isinstance(params, dict):
        delay = params.get("retry_after")
    if delay is None:
        delay = 1
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return None
    if delay < 0:
        return None
    return min(float(delay), MAX_RETRY_AFTER)


def _button_row(button: Button) -> dict:
    """Serialize a Button to a Telegram inline keyboard button.

    ⚠️ Telegram 把 ``callback_data`` 按 **64 字节静默截断**（切在多字节字符中间
    还会把半个字符丢掉），且**不校验、不报错** ⇒ 点击回来的 data 与按钮绑定的
    不是同一个值，用户点了却**触发另一个动作**。所以这里绝不截断：

    ``button.data`` 的 UTF-8 编码超过 :data:`CALLBACK_DATA_LIMIT` 即抛
    ``ValueError`` —— 与 ``edit()`` 对超长正文的闸同一形态（不静默降级）。
    调用方负责拒绝该按钮（本文件 ``edit()``：放弃该按钮 + WARNING，
    绝不带截断的 data 上线）。
    """
    encoded = button.data.encode("utf-8")
    if len(encoded) > CALLBACK_DATA_LIMIT:
        raise ValueError(
            "telegram callback_data too long: %d > %d bytes"
            % (len(encoded), CALLBACK_DATA_LIMIT)
        )
    return {"text": button.label, "callback_data": button.data}
