"""a2a 适配器 + 共享 HTTP 服务测试（**真服务器、零真实外网**）。

为什么用真服务器
----------------
a2a 是仓库里第一个**被调方**，它的问题几乎全在"HTTP 那一侧"：绑定地址、鉴权、
慢请求并发、停机是否干净、畸形 body 是否泄栈。这些用 mock 一个都测不出来 ——
mock 掉 ``http.server`` 就等于把被测对象本身删了。所以这里沿用 ``test_irc.py`` /
``test_ws.py`` 的手法：在 ``127.0.0.1:0`` 上起**真的** ``ThreadingHTTPServer``
（端口由操作系统分配，从 :attr:`HttpServer.port` 拿），用 ``urllib`` 发**真的**
HTTP 请求。零 mock，零外网。

覆盖清单
--------
1. 真服务器 -> 真 HTTP -> Inbound；reply 回到同一个 HTTP 请求里。
2. 默认绑定 127.0.0.1（含"非回环地址连不上"的实测）、放宽绑定需凭据。
3. 鉴权缺失时的行为（默认**不鉴权**是本平台的既有风险，逐条钉死）。
4. ``stop()`` 干净且**有耗时上界**（本项目栽过"stop 白等超时"的坑）。
5. 慢请求不阻塞其它请求（``ThreadingHTTPServer`` 的存在理由）。
6. 畸形 body / 处理器抛异常 -> 不回 500 栈信息。
7. ``edit()`` 恒 ``False``、注册表可发现、``outbound ⊆ required`` 不变量。
8. 防回环按 ``contextId`` 计轮次，**不做内容启发式**（反例钉死）。
9. 用完必须干净：端口释放、无残留线程。
"""

from __future__ import annotations

import ast
import io
import json
import logging
import pathlib
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge import httpsrv
from opencode_bridge.adapters import adapter_class, build, registered_names
from opencode_bridge.adapters import a2a as a2a_mod
from opencode_bridge.adapters.a2a import (
    AGENT_CARD_PATH,
    DEFAULT_MAX_TURNS,
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    ERR_PUSH_NOT_SUPPORTED,
    ERR_TASK_NOT_CANCELABLE,
    ERR_TASK_NOT_FOUND,
    ERR_UNSUPPORTED_OPERATION,
    ERR_VERSION_NOT_SUPPORTED,
    HARD_MAX_TURNS,
    HEALTH_PATH,
    LEGACY_AGENT_CARD_PATH,
    MAX_BODY_BYTES,
    MESSAGE_LIMIT,
    PROTOCOL_VERSION,
    RPC_PATHS,
    STATE_CANCELED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_REJECTED,
    STATE_WORKING,
    TERMINAL_STATES,
    A2aAdapter,
    _TASK_STATE_BY_OUTBOUND_KIND,
    _coerce_port,
    _local_of,
    _parse_peer_tokens,
    extract_text,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.httpsrv import HttpRequest, HttpResponse, Route, is_loopback_host
from opencode_bridge.identity import format_id, platform_of
from opencode_bridge.outbound import CANCELLED_TURN_KIND, CANCELLED_TURN_NOTICE_TEXT

#: ``stop()`` 的耗时上界（秒）。``HttpServer`` 的 serve 循环轮询间隔是 0.25s，
#: 所以正常停机应该在 1s 内完成。这里给 2.0s 留足余量，同时**足以抓住**
#: "忘了唤醒等待中的 handler，于是 join 等满 reply_timeout(300s)" 那个经典 bug。
STOP_BUDGET = 2.0

#: 本机拿不到非回环地址、或某些环境禁用 SO_REUSEADDR 时跳过个别实测用的地址。
UNASSIGNABLE_IP = "192.0.2.1"      # TEST-NET-1，RFC 5737 保留，不是本机地址


class Reply(NamedTuple):
    """一次 HTTP 调用的结果。

    用具名字段而不是位置元组：``status, headers, text = http(...)`` 这种解包
    一旦写错顺序，报错会表现为"字段缺失"这种**看起来像被测代码坏了**的现象。
    """

    status: int
    headers: dict
    text: str
    #: 状态码为 0 表示**连接层失败**（拒绝 / 超时 / 解析失败），不是 HTTP 错误。
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


# ----------------------------------------------------------------------
# HTTP 小工具
# ----------------------------------------------------------------------
def http(method: str, url: str, *, body: bytes | None = None,
         headers: dict | None = None, timeout: float = 15.0) -> Reply:
    """发一个**真** HTTP 请求。**不抛** ``HTTPError`` / ``URLError``。"""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return Reply(
                getattr(resp, "status", 200) or 200,
                dict(resp.headers),
                resp.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, dict(exc.headers),
                     exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as exc:
        # 连接层失败（拒绝连接 / 超时）。状态码用 0 —— 与"HTTP 404"区分开，
        # 否则"绑回环时外网地址连不上"这类断言会被当成"返回了 404"而假通过。
        return Reply(0, {}, "", repr(exc))


def rpc(adapter: "A2aAdapter", method: str, params: dict, *, req_id=1,
        path: Optional[str] = None, token: Optional[str] = None,
        version: Optional[str] = None, raw_body: Optional[bytes] = None,
        timeout: float = 15.0) -> tuple[int, Optional[dict], str]:
    """发一次**真** JSON-RPC 请求，返回 ``(status, parsed_or_None, text)``。"""
    url = adapter._server.url(path or RPC_PATHS[0])
    if raw_body is not None:
        payload = raw_body
    else:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if version is not None:
        headers["A2A-Version"] = version
    reply = http("POST", url, body=payload, headers=headers, timeout=timeout)
    try:
        return reply.status, json.loads(reply.text), reply.text
    except ValueError:
        return reply.status, None, reply.text


def post_raw(adapter: "A2aAdapter", payload: bytes, *, headers: dict | None = None,
             timeout: float = 15.0) -> Reply:
    base = {"Content-Type": "application/json"}
    base.update(headers or {})
    return http("POST", adapter._server.url(RPC_PATHS[0]), body=payload,
                headers=base, timeout=timeout)


def send_params(text: str = "hello", *, context_id: str = "", task_id: str = "",
                message_id: str = "", immediate: bool = True,
                role: str = "ROLE_USER") -> dict:
    """构造 ``SendMessageRequest``。

    ``immediate=True`` 默认带上 ``configuration.returnImmediately``：这样请求**不等**
    agent 答复就返回，测试不必为"没人回复"的那条路径付 30 秒。
    """
    message: dict = {
        "role": role,
        "messageId": message_id or "msg-1",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    params: dict = {"message": message}
    if immediate:
        params["configuration"] = {"returnImmediately": True}
    return params


def task_of(parsed: dict) -> dict:
    """从 JSON-RPC 响应里取 Task。

    ⚠️ **两种形状**：``SendMessage`` 的 ``result`` 是 ``SendMessageResponse`` 那个
    oneof（``{"task": ...}``，规范 §3.1.1 / §9.4.1），而 ``GetTask`` / ``CancelTask``
    的 ``result`` **就是** Task 本身（§3.1.3 / §3.1.5）。两种都接受。
    """
    result = parsed["result"]
    return result["task"] if isinstance(result, dict) and "task" in result else result


def _package_root() -> pathlib.Path:
    """``opencode_bridge/`` 包目录 —— 覆盖面守门要扫它下面所有 ``.py``。

    从**已导入的模块**取路径，而不是从 ``__file__`` 往上数层数：后者在测试文件
    被换目录跑时会指到别处（§7.1「找到的那份不是它」）。
    """
    return pathlib.Path(a2a_mod.__file__).resolve().parent.parent


def port_is_free(port: int, attempts: int = 60, delay: float = 0.1) -> bool:
    """端口能否被重新绑定（带 SO_REUSEADDR，容忍 TIME_WAIT）。

    重试到 6 秒而不是"试一次"：``server_close()`` 不会去 join daemon 的 handler
    线程（那是刻意的，见 :meth:`HttpServer.stop`），刚写完响应的连接可能还差
    几十毫秒才被操作系统彻底回收。一次就断言会把这种正常时序报成 flaky。
    """
    for _ in range(attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(delay)
        finally:
            sock.close()
    return False


def local_non_loopback_ipv4() -> Optional[str]:
    """本机除回环之外的 IPv4；拿不到返回 ``None``。"""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:  # pragma: no cover - 极端环境
        return None
    for info in infos:
        addr = info[4][0]
        if not addr.startswith("127."):
            return addr
    return None


class RecordingHooks:
    """记录入站的 hooks。可选 ``on_inbound_fn`` 在**记录之后**被调用。

    ⚠️ 先记录再回调：即使回调阻塞，测试也能先看到 Inbound 已经到达。
    """

    def __init__(self, on_inbound_fn=None) -> None:
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []
        self._cv = threading.Condition()
        self._fn = on_inbound_fn

    def on_inbound(self, inbound: Inbound) -> None:
        with self._cv:
            self.inbounds.append(inbound)
            self._cv.notify_all()
        if self._fn is not None:
            self._fn(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))

    def wait_for_inbounds(self, count: int, timeout: float = 10.0) -> list[Inbound]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self.inbounds) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            return list(self.inbounds)

    def wait_for_inbound(self, timeout: float = 10.0) -> Inbound:
        found = self.wait_for_inbounds(1, timeout)
        if not found:
            raise AssertionError("Inbound 没到达")
        return found[0]


# ----------------------------------------------------------------------
# 基类：起真服务器、保证干净收尾
# ----------------------------------------------------------------------
class A2aServerTestCase(unittest.TestCase):
    """起 ``bind_port: 0`` 的真服务器；提供"用完必须干净"的断言。"""

    def build(self, hooks, **cfg) -> A2aAdapter:
        base = {"bind_port": 0, "reply_timeout": 30.0}
        base.update(cfg)
        adapter = A2aAdapter(base, hooks)
        self.addCleanup(adapter.stop)
        return adapter

    def make(self, hooks: Optional[RecordingHooks] = None, **cfg):
        """起真服务器并断言它真的在跑。"""
        hooks = hooks if hooks is not None else RecordingHooks()
        adapter = self.build(hooks, **cfg)
        adapter.start()
        self.assertTrue(adapter.running, "真服务器没起来")
        return adapter, hooks

    def replying_hooks(self, text: str = "答复") -> tuple[RecordingHooks, dict]:
        """模拟 core 的完整发送序列：progress 占位 -> final 答复。"""
        holder: dict[str, A2aAdapter] = {}

        def _reply(inbound: Inbound) -> None:
            adapter = holder["adapter"]
            adapter.send(Outbound(inbound.conversation_id, "处理中…", kind="progress"))
            adapter.send(Outbound(inbound.conversation_id, text, kind="final"))

        return RecordingHooks(_reply), holder

    # --- 干净收尾断言 --------------------------------------------------
    def assert_clean(self, adapter: A2aAdapter) -> None:
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter._thread, "分发线程引用没清")
        leftovers = [
            t.name for t in threading.enumerate()
            if t.is_alive() and t.name.startswith(("httpsrv:", "a2a-dispatch"))
        ]
        self.assertEqual(leftovers, [], f"残留线程: {leftovers}")
        self.assertTrue(port_is_free(adapter.port), f"端口 {adapter.port} 没释放")


# ======================================================================
# 1. 能力声明 / 注册 / 不变量
# ======================================================================
class TestDeclarations(A2aServerTestCase):
    def test_capabilities_are_truthful(self):
        adapter, _ = self.make()
        self.assertEqual(adapter.name, "a2a")
        self.assertEqual(adapter.label, "A2A")
        self.assertTrue(adapter.supports_inbound)
        # A2A 的 Part 只有 text/raw/url/data（规范 §4.1.6），没有"按钮"概念。
        self.assertFalse(adapter.supports_inline_buttons)
        self.assertFalse(adapter.supports_media)
        self.assertEqual(adapter.typed_command_prefix, "/")

    def test_max_message_length_is_our_own_limit_not_an_invented_spec_number(self):
        """规范**没有**消息长度上限 —— 断言我们取的是自己兑现的上限，不是编的数字。"""
        adapter, _ = self.make()
        self.assertEqual(MESSAGE_LIMIT, MAX_BODY_BYTES)
        self.assertEqual(adapter.max_message_length, MAX_BODY_BYTES)
        # 明显偏大是故意的：宁可"在规范没规定的地方不分片"，也不要假装有权威上限。
        self.assertGreaterEqual(adapter.max_message_length, 1_000_000)
        # 真正生效的那个上限必须是**字节**级的请求体上限，两者是同一个数。
        self.assertEqual(adapter._server.max_body, MAX_BODY_BYTES)

    def test_declared_agent_card_capabilities_match_implementation(self):
        """声明 streaming/pushNotifications=False，就必须按规范回对应错误码（§3.3.4）。"""
        adapter, _ = self.make()
        card = http("GET", adapter._server.url(AGENT_CARD_PATH)).json()
        caps = card["capabilities"]
        self.assertFalse(caps["streaming"])
        self.assertFalse(caps["pushNotifications"])
        self.assertFalse(caps["extendedAgentCard"])
        _, body, _ = rpc(adapter, "SendStreamingMessage", {})
        self.assertEqual(body["error"]["code"], ERR_UNSUPPORTED_OPERATION)
        _, body, _ = rpc(adapter, "SubscribeToTask", {})
        self.assertEqual(body["error"]["code"], ERR_UNSUPPORTED_OPERATION)
        for method in ("CreateTaskPushNotificationConfig",
                       "GetTaskPushNotificationConfig",
                       "ListTaskPushNotificationConfigs",
                       "DeleteTaskPushNotificationConfig"):
            with self.subTest(method=method):
                _, body, _ = rpc(adapter, method, {})
                self.assertEqual(body["error"]["code"], ERR_PUSH_NOT_SUPPORTED)
        _, body, _ = rpc(adapter, "GetExtendedAgentCard", {})
        self.assertEqual(body["error"]["code"], ERR_UNSUPPORTED_OPERATION)

    def test_required_tokens_invariant(self):
        adapter, _ = self.make()
        required = set(adapter.required_tokens)
        outbound = set(adapter.outbound_tokens)
        # 仓库不变量（tests/test_cli.py 逐个平台强制）：两者都**非空**，且出站是
        # "已配置"的子集 —— 能发出去的前提一定是已配置。
        self.assertTrue(required, "a2a 必须声明 required_tokens")
        self.assertTrue(outbound, "a2a 必须声明 outbound_tokens")
        self.assertLessEqual(outbound, required)
        # a2a 没有"凭据"，但 bind_port 是被调方唯一不可省略的东西（出站也依赖它：
        # 没有等待中的 HTTP 请求就没有东西可交付）。
        self.assertEqual(adapter.required_tokens, ("bind_port",))
        self.assertEqual(adapter.outbound_tokens, ("bind_port",))

    def test_token_declarations_match_the_repo_shape(self):
        """与 ``test_cli.py`` 的形状检查对齐（tuple / 非空 / 键是字符串且无空格）。"""
        for attr in ("required_tokens", "outbound_tokens"):
            with self.subTest(attr=attr):
                keys = getattr(A2aAdapter, attr)
                self.assertIsInstance(keys, tuple)
                self.assertTrue(keys)
                for key in keys:
                    self.assertIsInstance(key, str)
                    self.assertNotIn(" ", key)

    def test_configured_only_with_a_bindable_port(self):
        """a2a「已配置」= **有一个能绑的端口**，不是"没有配置"。

        ⚠️ **这条断言的语义被反转过两次，痕迹留在这里**：

        第一次（最初）：``assertFalse(..., "缺 bind_port 时不该被判成配置齐备")``。
        那条守的是**一个真 bug** —— 预检只看 ``required_tokens``（且硬编码找
        ``bot_token``），于是**只配了 a2a 的用户被桥接拒绝启动**，与当年
        Matrix / IRC / Mattermost 被拒启动是同一类问题。第一次反转后引入了
        ``Adapter.config_optional``，判据变成 ``assertTrue(...)``，理由是
        「空配置即可运行（bind 127.0.0.1 + 端口 0 由系统分配）」。

        第二次（本次，2026-10-06）：**那条前提本身早已不成立** ——
        ``_coerce_port("")`` 给的是
        :data:`~opencode_bridge.adapters.a2a.UNCONFIGURED_PORT`（-1），而
        :meth:`A2aAdapter.start` 恰恰以 ``port < 0`` 为由打 error **不绑定就
        return**。⇒ 一个"曾经为真"的事实被当成了判定，于是 ``config.example.json``
        里那行空 ``bind_port`` 就足以让全新安装的预检从 ``False`` 翻成 ``True``，
        桥不再走 ``NO_ADAPTER_MESSAGE`` 那条提前退出。

        ⇒ 第三次反转：**判据换成「此刻这份配置能不能真跑起来」**，由
        ``Adapter.config_runnable``（a2a 覆写）回答；"当年那个被拒启动的 bug
        不许回来"这条守卫保留在下面**按新形态**写的断言里。
        """
        from opencode_bridge import __main__ as cli
        from opencode_bridge.config import Config

        # ⭐ 守卫：配了能绑的端口就不许拒绝启动 —— 那是当年那个 bug 的当前形态。
        self.assertTrue(
            cli._has_configured_adapter(Config(adapters={"a2a": {"bind_port": 9900}})),
            "只配 a2a 且端口可用时，桥接不许拒绝启动",
        )
        # bind_port: 0 也算（由系统分配，确实起得来）—— 拒掉它就是新的行为回退。
        self.assertTrue(
            cli._has_configured_adapter(Config(adapters={"a2a": {"bind_port": 0}}))
        )

        # 空配置 = **未配置**：start() 会拒绝启动，预检必须照实说。
        self.assertFalse(
            cli._has_configured_adapter(Config(adapters={"a2a": {}})),
            "空配置起不来，不许说成已配置",
        )

        # 状态视图两个口径都要照实说，并说出缺的是哪个键（那是可操作的提示）。
        entries = [e for e in cli._platform_status(Config(adapters={"a2a": {}}))
                   if e["key"] == "a2a"]
        self.assertTrue(entries, "a2a 没出现在 _platform_status 里")
        unconfigured = entries[0]
        self.assertFalse(unconfigured["configured"])
        self.assertFalse(unconfigured["inbound_ready"])
        self.assertFalse(unconfigured["outbound_ready"])
        self.assertEqual(unconfigured["missing"], ["bind_port"])

        ready = [e for e in cli._platform_status(Config(adapters={"a2a": {"bind_port": 9900}}))
                 if e["key"] == "a2a"]
        self.assertTrue(ready)
        self.assertTrue(ready[0]["configured"])
        self.assertTrue(ready[0]["inbound_ready"])
        self.assertTrue(ready[0]["outbound_ready"])
        self.assertEqual(ready[0]["missing"], [])

    def test_config_optional_exempts_running_not_declaring(self):
        """``config_optional`` 说的是「答案由我自己给」，**不豁免**"必须声明配置面"。

        ⚠️ 它的含义 2026-10-06 收紧过：此前它是「空配置即可运行」的豁免，而那条
        前提已不成立。现在它只表示「a2a 属于**没有凭据可填**的那一类平台，
        '够不够跑'由 :meth:`A2aAdapter.config_runnable` 回答」——
        断言本身不变（仍须为 True，且两条 token 列表仍须非空）。
        """
        self.assertTrue(A2aAdapter.config_optional)
        self.assertTrue(callable(A2aAdapter.config_runnable))
        # 声明义务照旧：两条token 列表仍须非空（test_cli 的守卫依赖这点）
        self.assertTrue(A2aAdapter.required_tokens)
        self.assertTrue(A2aAdapter.outbound_tokens)

    def test_registry_discovers_a2a(self):
        self.assertIn("a2a", registered_names())
        self.assertIs(adapter_class("a2a"), A2aAdapter)
        built = build("a2a", {"bind_port": 0}, object())
        self.addCleanup(built.stop)
        self.assertIsInstance(built, A2aAdapter)

    def test_capabilities_snapshot_exposes_bind_and_auth(self):
        adapter, _ = self.make()
        caps = adapter.capabilities()
        self.assertEqual(caps["bind_host"], "127.0.0.1")
        self.assertEqual(caps["bind_port"], adapter.port)
        self.assertTrue(caps["loopback_only"])
        self.assertFalse(caps["auth_enabled"])
        self.assertEqual(caps["well_known_path"], AGENT_CARD_PATH)
        self.assertTrue(caps["running"])
        self.assertFalse(caps["streaming"])
        self.assertFalse(caps["push_notifications"])

    def test_edit_returns_false(self):
        """A2A 规范 §3.1 的 11 个操作里**没有**编辑消息的能力 -> 必须诚实返回 False。"""
        adapter, _ = self.make()
        handle = MsgHandle("a2a:local:127.0.0.1", "task-x", "a2a")
        self.assertFalse(adapter.edit(handle, Outbound("a2a:local:127.0.0.1", "改了")))
        # 再调一次也必须稳定返回 False（不能"第一次成功"）。
        self.assertFalse(adapter.edit(handle, Outbound("a2a:local:127.0.0.1", "再改")))

    def test_answer_is_noop(self):
        adapter, _ = self.make()
        self.assertIsNone(adapter.answer("q1", "text"))


# ======================================================================
# 2. 绑定地址：默认回环，放宽需凭据
# ======================================================================
class TestBindHost(A2aServerTestCase):
    def test_default_bind_host_is_loopback(self):
        adapter = self.build(RecordingHooks())
        self.assertEqual(adapter.host, "127.0.0.1")

    def test_shared_module_default_is_loopback_and_has_no_escape_hatch(self):
        """共用模块的默认值必须是显式常量，且**不是** 0.0.0.0。"""
        self.assertEqual(httpsrv.DEFAULT_BIND_HOST, "127.0.0.1")
        self.assertTrue(is_loopback_host(httpsrv.DEFAULT_BIND_HOST))
        for bad in ("0.0.0.0", "", "::"):
            with self.subTest(host=bad):
                self.assertFalse(is_loopback_host(bad))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("127.0.0.5"))
        self.assertTrue(is_loopback_host("::1"))
        # 不传 host 时构造出的服务器也必须落在回环。
        server = httpsrv.HttpServer(port=0, name="probe")
        self.assertEqual(server.host, "127.0.0.1")

    def test_real_socket_is_bound_to_loopback(self):
        adapter, _ = self.make()
        self.assertEqual(adapter._server.host, "127.0.0.1")
        self.assertTrue(adapter._server.loopback_only)

    def test_non_loopback_address_is_refused_when_bound_to_loopback(self):
        """实测：只绑回环时，机器的非回环地址**连不上**（这才是"默认安全"的证据）。"""
        adapter, _ = self.make()
        external = local_non_loopback_ipv4()
        if external is None:
            self.skipTest("本机没有可用的非回环 IPv4，跳过实测")
        reply = http("GET", f"http://{external}:{adapter.port}{HEALTH_PATH}", timeout=3.0)
        self.assertNotEqual(reply.status, 200,
                            f"{external} 竟然连上了只绑回环的服务")
        self.assertNotEqual(reply.status, 404,
                            "拿到 404 说明服务其实在监听非回环地址")

    def test_widening_bind_without_credentials_falls_back_to_loopback(self):
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING"):
            adapter = self.build(RecordingHooks(), bind_host="0.0.0.0")
        self.assertEqual(adapter.host, "127.0.0.1")
        adapter.start()
        self.assertEqual(adapter._server.host, "127.0.0.1")

    def test_widening_bind_fallback_is_logged_loudly(self):
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING") as cap:
            self.build(RecordingHooks(), bind_host="0.0.0.0")
        blob = "\n".join(cap.output)
        # 必须同时点名"用户要了什么"、"实际变成什么"、"为什么"
        self.assertIn("0.0.0.0", blob)
        self.assertIn("127.0.0.1", blob)
        self.assertIn("auth_token", blob)

    def test_widening_bind_is_allowed_when_credentials_exist(self):
        adapter = self.build(RecordingHooks(), bind_host="0.0.0.0", auth_token="s3cret")
        self.assertEqual(adapter.host, "0.0.0.0")
        self.assertTrue(adapter._auth_enabled)

    def test_missing_port_does_not_start_and_does_not_raise(self):
        adapter = A2aAdapter({}, RecordingHooks())
        self.addCleanup(adapter.stop)
        with self.assertLogs("opencode_bridge.adapters.a2a", level="ERROR") as cap:
            adapter.start()          # 绝不抛异常
        self.assertFalse(adapter.running)
        self.assertIn("bind_port", "\n".join(cap.output))

    def test_invalid_port_does_not_start(self):
        for bad in ("not-a-port", "", None, 70000, -5):
            with self.subTest(bad=bad):
                adapter = A2aAdapter({"bind_port": bad}, RecordingHooks())
                self.addCleanup(adapter.stop)
                with self.assertLogs("opencode_bridge.adapters.a2a", level="ERROR"):
                    adapter.start()
                self.assertFalse(adapter.running)

    def test_start_is_idempotent_and_stop_is_idempotent(self):
        adapter, _ = self.make()
        port = adapter.port
        adapter.start()
        self.assertEqual(adapter.port, port, "第二次 start() 换端口了")
        adapter.stop()
        adapter.stop()               # 再来一次不能崩
        self.assertFalse(adapter.running)
        self.assert_clean(adapter)

    def test_bind_failure_is_logged_not_raised(self):
        """绑一个不属于本机的地址 -> ``BindError`` -> 适配器只记日志、如实报未运行。"""
        adapter = A2aAdapter({"bind_port": UNASSIGNABLE_IP}, RecordingHooks())
        self.addCleanup(adapter.stop)
        with self.assertLogs("opencode_bridge.adapters.a2a", level="ERROR") as cap:
            adapter.start()
        self.assertFalse(adapter.running)
        self.assertIn(UNASSIGNABLE_IP, "\n".join(cap.output))


# ======================================================================
# 3. 真服务器 -> 真 HTTP -> Inbound -> 答复回到同一个请求
# ======================================================================
class TestRealRoundTrip(A2aServerTestCase):
    def test_agent_card_has_every_required_field(self):
        adapter, _ = self.make(agent_name="my-agent")
        reply = http("GET", adapter._server.url(AGENT_CARD_PATH))
        self.assertEqual(reply.status, 200)
        card = reply.json()
        # 规范 §4.4.1 标 Required 的字段逐个断言。
        for field in ("name", "description", "version", "supportedInterfaces",
                      "capabilities", "defaultInputModes", "defaultOutputModes",
                      "skills"):
            self.assertIn(field, card, f"Agent Card 缺必填字段 {field}")
        self.assertEqual(card["name"], "my-agent")
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        self.assertEqual(
            card["supportedInterfaces"][0]["protocolVersion"], PROTOCOL_VERSION
        )
        self.assertTrue(
            card["supportedInterfaces"][0]["url"].endswith("/rpc"),
            "supportedInterfaces 的 URL 应指向 JSON-RPC 端点",
        )
        # §4.5：JSON 序列化必须 camelCase，不能出现 proto 的 snake_case。
        self.assertNotIn("supported_interfaces", card)
        self.assertIn("defaultInputModes", card)

    def test_agent_card_declares_no_security_when_unauthenticated(self):
        adapter, _ = self.make()
        card = http("GET", adapter._server.url(AGENT_CARD_PATH)).json()
        self.assertNotIn("securitySchemes", card)
        self.assertNotIn("securityRequirements", card)

    def test_legacy_agent_card_path_also_answers(self):
        adapter, _ = self.make()
        reply = http("GET", adapter._server.url(LEGACY_AGENT_CARD_PATH))
        self.assertEqual(reply.status, 200)
        self.assertIn("supportedInterfaces", reply.json())

    def test_health_endpoint(self):
        adapter, _ = self.make()
        reply = http("GET", adapter._server.url(HEALTH_PATH))
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json()["status"], "ok")
        self.assertEqual(reply.json()["protocolVersion"], PROTOCOL_VERSION)

    def test_real_http_request_produces_inbound_and_reply_returns_in_same_request(self):
        hooks, holder = self.replying_hooks("最终答复：42")
        adapter, _ = self.make(hooks)
        holder["adapter"] = adapter

        status, body, _ = rpc(adapter, "SendMessage", send_params("你好", immediate=False))
        self.assertEqual(status, 200)

        # 1) Inbound 真的产生了
        inbound = hooks.wait_for_inbound()
        self.assertEqual(inbound.text, "你好")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.platform, "a2a")
        # 2) conversation_id 走 identity.format_id("a2a", <对端>)
        self.assertEqual(inbound.conversation_id, format_id("a2a", "local:127.0.0.1"))
        self.assertEqual(platform_of(inbound.conversation_id), "a2a")
        self.assertEqual(inbound.user_id, "local:127.0.0.1")

        # 3) 答复**在同一个 HTTP 请求里**回来了（规范 §3.1.1 的阻塞语义）
        task = task_of(body)
        self.assertEqual(task["status"]["state"], STATE_COMPLETED)
        self.assertTrue(task["id"].startswith("task-"))
        self.assertTrue(task["contextId"].startswith("ctx-"))
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "最终答复：42")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "最终答复：42")
        self.assertEqual(task["status"]["message"]["role"], "ROLE_AGENT")
        # §4.1.4：Message 的必填字段是 messageId / role / parts
        self.assertTrue(task["status"]["message"]["messageId"])
        # §4.1.2：status.timestamp 是 ISO 8601
        self.assertRegex(task["status"]["timestamp"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_progress_message_alone_never_completes_the_task(self):
        """**本平台最容易写错的一处**。

        core 在 ``prompt()`` 返回后会先发一条 progress 占位消息，而 ``edit()``
        恒 False，所以最终答复一定在**另一次** ``send()``。若在 progress 就判定完成，
        对端只会收到"处理中…"，真正答案被丢掉。
        """
        holder: dict[str, A2aAdapter] = {}

        def _progress_only(inbound: Inbound) -> None:
            holder["adapter"].send(
                Outbound(inbound.conversation_id, "处理中…", kind="progress")
            )

        adapter, hooks = self.make(RecordingHooks(_progress_only))
        holder["adapter"] = adapter
        adapter.reply_timeout = 1.5

        status, body, _ = rpc(adapter, "SendMessage", send_params("慢活", immediate=False))
        self.assertEqual(status, 200)
        task = task_of(body)
        self.assertEqual(
            task["status"]["state"], STATE_FAILED,
            "progress 消息就把任务判终态了 —— 最终答复会被丢掉",
        )
        self.assertIn("did not reply", task["status"]["message"]["parts"][0]["text"])
        # progress 那次 send() 必须返回句柄（core 拿它去试 edit）
        self.assertEqual(len(hooks.inbounds), 1)

    def test_error_kind_maps_to_task_failed(self):
        holder: dict[str, A2aAdapter] = {}

        def _fail(inbound: Inbound) -> None:
            holder["adapter"].send(Outbound(inbound.conversation_id, "炸了", kind="error"))

        adapter, _ = self.make(RecordingHooks(_fail))
        holder["adapter"] = adapter
        _, body, _ = rpc(adapter, "SendMessage", send_params("会失败", immediate=False))
        task = task_of(body)
        self.assertEqual(task["status"]["state"], STATE_FAILED)
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "炸了")

    def test_progress_send_returns_a_handle(self):
        holder: dict[str, A2aAdapter] = {}
        handles: list = []

        def _progress(inbound: Inbound) -> None:
            handles.append(holder["adapter"].send(
                Outbound(inbound.conversation_id, "处理中…", kind="progress")
            ))

        adapter, _ = self.make(RecordingHooks(_progress))
        holder["adapter"] = adapter
        adapter.reply_timeout = 1.0
        rpc(adapter, "SendMessage", send_params("x", immediate=False))
        self.assertEqual(len(handles), 1)
        self.assertIsInstance(handles[0], MsgHandle)
        self.assertEqual(handles[0].platform, "a2a")

    def test_reply_without_a_waiting_request_is_reported_not_faked(self):
        adapter, _ = self.make()
        result = adapter.send(Outbound("a2a:nobody", "迟到的回复", kind="final"))
        self.assertIsNone(result)
        self.assertEqual(adapter.last_send_error.value, "not_found")

    def test_return_immediately_then_get_task_sees_the_late_reply(self):
        holder: dict[str, A2aAdapter] = {}

        def _late(inbound: Inbound) -> None:
            time.sleep(0.1)
            holder["adapter"].send(
                Outbound(inbound.conversation_id, "稍后到达的答复", kind="final")
            )

        adapter, hooks = self.make(RecordingHooks(_late))
        holder["adapter"] = adapter

        status, body, _ = rpc(adapter, "SendMessage",
                             send_params("异步", immediate=True))
        self.assertEqual(status, 200)
        task_id = task_of(body)["id"]
        self.assertIn(task_of(body)["status"]["state"],
                      (STATE_WORKING, STATE_COMPLETED))
        hooks.wait_for_inbound()

        # 等答复落库后 GetTask（§3.1.3）必须能看到终态
        deadline = time.monotonic() + 5.0
        got = {}
        while time.monotonic() < deadline:
            _, got, _ = rpc(adapter, "GetTask", {"id": task_id})
            if task_of(got)["status"]["state"] == STATE_COMPLETED:
                break
            time.sleep(0.05)
        self.assertEqual(task_of(got)["status"]["state"], STATE_COMPLETED)
        self.assertEqual(
            task_of(got)["artifacts"][0]["parts"][0]["text"], "稍后到达的答复"
        )


# ======================================================================
# 3b. 出站 kind -> A2A 终态（用户 2026-10-07 拍板：取消要有自己的 kind）
#
# 缺陷形态是 ``STATE_FAILED if out.kind == "error" else STATE_COMPLETED`` ——
# **任何没被显式列出的 kind 都被报成"完成"**，于是 ``/new`` 丢掉的那一轮在对端
# 看起来是"agent 正常答完了"。
# ======================================================================
class TestOutboundKindToTaskState(A2aServerTestCase):
    """``Outbound.kind`` -> A2A 终态。

    缺陷形态是 ``STATE_FAILED if out.kind == "error" else STATE_COMPLETED`` ——
    **任何没被显式列出的 kind 都被报成"完成"**，于是用户 ``/new`` 丢掉的那一轮在对端
    看起来是"agent 正常答完了"（用户 2026-10-07 拍板给了 ``cancelled`` 一条独立 kind）。

    ## 同步点，没有一处计时

    一律走 ``returnImmediately=True``（:func:`send_params` 的默认）建 task，再用
    ``hooks.wait_for_inbound()`` 拿到**那个对端的** ``conversation_id`` —— 那是
    :meth:`A2aAdapter.send` 按 ``_local_of`` 反查 task 的唯一钥匙，猜不出来。
    ``wait_for_inbound`` 返回即证明 task 已登记（登记发生在分发**之前**），
    所以 :meth:`A2aAdapter.send` 一定找得到它 ⇒ 全程零 ``sleep``。
    """

    def _running_task(self, adapter: "A2aAdapter", hooks: RecordingHooks,
                      text: str = "跑起来") -> tuple[str, str]:
        """建一个非终态的 task，返回 ``(task_id, conversation_id)``。"""
        _, body, _ = rpc(adapter, "SendMessage", send_params(text))
        task_id = task_of(body)["id"]
        self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING,
                         "前提不成立：这个 task 一开始就不是 WORKING")
        conversation_id = hooks.wait_for_inbound().conversation_id
        return task_id, conversation_id

    def _task_state(self, adapter: "A2aAdapter", task_id: str) -> tuple[str, dict]:
        _, body, _ = rpc(adapter, "GetTask", {"id": task_id})
        task = task_of(body)
        return task["status"]["state"], task

    def test_a_cancelled_turn_is_reported_as_canceled_never_as_completed(self):
        """⛔ 缺陷形态下这一条是红的：取消落在"其它"那一支 ⇒ ``COMPLETED``。

        症状 = 「取消被记成 COMPLETED」，⛔ 不是"测试本身坏了"。
        """
        adapter, hooks = self.make(RecordingHooks())
        task_id, conversation_id = self._running_task(adapter, hooks)

        handle = adapter.send(Outbound(conversation_id,
                                       CANCELLED_TURN_NOTICE_TEXT,
                                       kind=CANCELLED_TURN_KIND))

        self.assertIsInstance(handle, MsgHandle)
        state, task = self._task_state(adapter, task_id)
        self.assertEqual(state, STATE_CANCELED,
                         "取消被报成了 %r —— 对端会以为 agent 正常答完了" % state)
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"],
                         CANCELLED_TURN_NOTICE_TEXT,
                         "对端拿到的必须是出站那一侧真正发出去的那句")

    def test_each_kind_maps_to_the_state_the_a2a_spec_names(self):
        """穷举这张表：规范 §4.1.3 的终态名与表里的键一一对上。

        ⚠️ ``progress`` **不在**表里（它不判终态，见 :meth:`A2aAdapter.send` 开头）。
        这条断言兼作"新增 kind 时要改两处"的强制点：只改 a2a 的表不改这里 ⇒ 红。
        """
        self.assertEqual(
            _TASK_STATE_BY_OUTBOUND_KIND,
            {"text": STATE_COMPLETED, "final": STATE_COMPLETED,
             "error": STATE_FAILED, "cancelled": STATE_CANCELED},
            "kind -> A2A 终态的映射变了：新增 kind 必须同时更新 a2a 的表与本断言",
        )

    def test_the_cancelled_kind_survives_the_blocking_send_message_path(self):
        """端到端：core 真实走的那条路（blocking ``SendMessage``）。"""
        holder: dict[str, A2aAdapter] = {}

        def _cancel(inbound: Inbound) -> None:
            holder["adapter"].send(
                Outbound(inbound.conversation_id, CANCELLED_TURN_NOTICE_TEXT,
                         kind=CANCELLED_TURN_KIND)
            )

        adapter, _ = self.make(RecordingHooks(_cancel))
        holder["adapter"] = adapter
        adapter.reply_timeout = 1.5

        status, body, _ = rpc(adapter, "SendMessage",
                              send_params("跑起来", immediate=False))

        self.assertEqual(status, 200)
        self.assertEqual(task_of(body)["status"]["state"], STATE_CANCELED)

    def test_an_unregistered_kind_leaves_the_task_unfinished_and_is_recorded(self):
        """⛔ 未知 kind **不判终态**（用户 2026-10-07 拍板），只记一次失败。

        缺陷形态（``else STATE_COMPLETED``）下这一条**三处**都红：状态变成 COMPLETED、
        多出一个 artifact、且**完全没有**任何失败被记下。
        """
        adapter, hooks = self.make(RecordingHooks())
        task_id, conversation_id = self._running_task(adapter, hooks)

        handle = adapter.send(Outbound(conversation_id, "?", kind="没登记过的kind"))

        self.assertIsNone(handle, "未知 kind 不该交回句柄 —— 它没有送达任何东西")
        self.assertEqual(adapter.last_send_error.value, "bad_format",
                         "未知 kind 必须被记成失败，--status 靠这个分类")
        state, task = self._task_state(adapter, task_id)
        self.assertEqual(state, STATE_WORKING,
                         "未知 kind 把任务判成了终态 %r" % state)
        self.assertNotIn("artifacts", task, "未知 kind 不该产出任何 artifact")

    def test_every_kind_the_bridge_can_put_on_a_message_is_registered_here(self):
        """⚠️ 覆盖面守门（**AST**，不是行级正则 —— 后者匹配不到跨行的实参）。

        判据：把生产代码里真正给 ``Outbound`` / ``self.send_text`` / ``self.finalize``
        写的 ``kind=<字面量>`` 全收集起来，每一个都必须在
        ``{"progress"} | set(_TASK_STATE_BY_OUTBOUND_KIND)`` 里。

        ⇒ 新增 kind 却忘了在这里登记时，这一条会红 —— 而不登记的后果是「静默变成
        COMPLETED」，用户完全看不出来（那正是本缺陷）。
        """
        produced: dict[str, list[str]] = {}
        for path in sorted(_package_root().rglob("*.py")):
            tree = ast.parse(io.open(path, encoding="utf-8").read(), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if ast.unparse(node.func) not in ("Outbound", "self.send_text",
                                                  "self.finalize"):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "kind":
                        continue
                    value = keyword.value
                    if (isinstance(value, ast.Constant)
                            and isinstance(value.value, str)):
                        produced.setdefault(value.value, []).append(
                            "%s:%d" % (path.name, node.lineno))

        self.assertTrue(produced,
                        "前提不成立：一个 kind 字面量都没扫到 —— 判据坏了")
        unregistered = sorted(
            kind for kind in produced
            if kind != "progress" and kind not in _TASK_STATE_BY_OUTBOUND_KIND
        )
        self.assertEqual(
            unregistered, [],
            "这些 kind 会落到 a2a 的未知分支（不判终态）：%r"
            % {kind: produced[kind] for kind in unregistered},
        )
        self.assertIn(CANCELLED_TURN_KIND, _TASK_STATE_BY_OUTBOUND_KIND,
                      "取消的 kind 没有登记 —— 对端会收到 COMPLETED")


# ======================================================================
# 4. task 账本：GetTask / ListTasks / CancelTask / 多轮
# ======================================================================
class TestTaskLedger(A2aServerTestCase):
    def _make_task(self, adapter: "A2aAdapter", text: str = "hi", **kw) -> str:
        _, body, _ = rpc(adapter, "SendMessage", send_params(text, **kw))
        return task_of(body)["id"]

    def test_get_task_returns_the_task_directly_not_wrapped(self):
        """⚠️ 形状细节：``GetTask`` 的 ``result`` **就是** Task（§3.1.3），
        只有 ``SendMessage`` 才包一层 ``{"task": ...}``（§3.1.1 的 oneof）。"""
        adapter, _ = self.make()
        task_id = self._make_task(adapter)
        status, body, _ = rpc(adapter, "GetTask", {"id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["id"], task_id)
        self.assertNotIn("task", body["result"],
                         "GetTask 不该再包一层 task（§3.1.3）")
        self.assertEqual(body["result"]["status"]["state"], STATE_WORKING)

    def test_get_task_parameter_is_id_not_taskid(self):
        """``id`` 是 §3.1.3 的字段名；``taskId`` 是 §4.2.1 上事件对象的字段名。
        写错字段名 -> -32602 并在消息里点名正确字段，而不是静默接受。"""
        adapter, _ = self.make()
        task_id = self._make_task(adapter)
        _, body, _ = rpc(adapter, "GetTask", {"taskId": task_id})
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)
        self.assertIn("params.id", body["error"]["message"])
        _, body, _ = rpc(adapter, "GetTask", {})
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)

    def test_get_task_unknown_id_is_404_with_task_not_found(self):
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "GetTask", {"id": "task-nope"})
        self.assertEqual(status, 404, "规范 §5.4: TaskNotFoundError -> HTTP 404")
        self.assertEqual(body["error"]["code"], ERR_TASK_NOT_FOUND)

    def test_tasks_are_scoped_to_the_peer(self):
        adapter, hooks = self.make(RecordingHooks(), peer_tokens="alice:tok-a,bob:tok-b")
        _, first, _ = rpc(adapter, "SendMessage",
                          send_params("alice 的活", message_id="m-a"), token="tok-a")
        alice_task = task_of(first)["id"]
        _, second, _ = rpc(adapter, "SendMessage",
                           send_params("bob 的活", message_id="m-b"), token="tok-b")
        bob_task = task_of(second)["id"]

        _, body, _ = rpc(adapter, "ListTasks", {}, token="tok-a")
        ids = [t["id"] for t in body["result"]["tasks"]]
        self.assertIn(alice_task, ids)
        self.assertNotIn(bob_task, ids, "规范 §13.1 要求按已认证身份做作用域隔离")
        self.assertEqual(body["result"]["totalSize"], 1)

        # alice **查不到** bob 的 task，且错误信息不区分"不存在/无权"
        _, denied, _ = rpc(adapter, "GetTask", {"id": bob_task}, token="tok-a")
        self.assertEqual(denied["error"]["code"], ERR_TASK_NOT_FOUND)
        _, unknown, _ = rpc(adapter, "GetTask", {"id": "task-does-not-exist"},
                            token="tok-a")
        self.assertEqual(unknown["error"]["code"], denied["error"]["code"])
        # §3.3.2：两种情况必须给**同样形状**的信息，绝不能让对端靠错误文案
        # 探测"这个 task id 到底存不存在"。
        self.assertTrue(unknown["error"]["message"].startswith("task not found: "))
        self.assertTrue(denied["error"]["message"].startswith("task not found: "))
        self.assertNotIn("data", denied["error"])
        _, allowed, _ = rpc(adapter, "GetTask", {"id": bob_task}, token="tok-b")
        self.assertEqual(allowed["result"]["id"], bob_task)

    def test_list_tasks_pagination_contract(self):
        adapter, _ = self.make()
        for i in range(3):
            self._make_task(adapter, f"q{i}", message_id=f"m{i}")
        _, body, _ = rpc(adapter, "ListTasks", {"pageSize": 2})
        result = body["result"]
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["pageSize"], 2)
        self.assertEqual(result["totalSize"], 3)
        self.assertNotEqual(result["nextPageToken"], "", "还有下一页时不能是空串")
        # 规范 §3.1.4：按"最后更新时间倒序"
        _, last, _ = rpc(
            adapter, "ListTasks", {"pageSize": 2, "pageToken": result["nextPageToken"]}
        )
        self.assertEqual(len(last["result"]["tasks"]), 1)
        self.assertEqual(last["result"]["nextPageToken"], "",
                         "末页的 nextPageToken 必须是空串（§3.1.4）")

    def test_list_tasks_page_size_is_clamped_to_spec_range(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "ListTasks", {"pageSize": 5000})
        self.assertEqual(body["result"]["pageSize"], 100)     # 规范上限 100
        _, body, _ = rpc(adapter, "ListTasks", {"pageSize": 0})
        self.assertEqual(body["result"]["pageSize"], 1)       # 规范下限 1
        _, body, _ = rpc(adapter, "ListTasks", {})
        self.assertEqual(body["result"]["pageSize"], 50)      # 规范默认 50

    def test_list_tasks_can_filter_by_context_and_state(self):
        adapter, _ = self.make()
        self._make_task(adapter, "a", context_id="ctx-1", message_id="m1")
        self._make_task(adapter, "b", context_id="ctx-2", message_id="m2")
        _, body, _ = rpc(adapter, "ListTasks", {"contextId": "ctx-1"})
        self.assertEqual(body["result"]["totalSize"], 1)
        self.assertEqual(body["result"]["tasks"][0]["contextId"], "ctx-1")
        _, body, _ = rpc(adapter, "ListTasks", {"status": STATE_COMPLETED})
        self.assertEqual(body["result"]["totalSize"], 0, "还没有任务完成")

    def test_include_artifacts_false_omits_the_field_entirely(self):
        holder: dict[str, A2aAdapter] = {}

        def _reply(inbound: Inbound) -> None:
            holder["adapter"].send(
                Outbound(inbound.conversation_id, "有 artifact", kind="final")
            )

        adapter, _ = self.make(RecordingHooks(_reply))
        holder["adapter"] = adapter
        _, body, _ = rpc(adapter, "SendMessage", send_params("带答复", immediate=False))
        task_id = task_of(body)["id"]

        _, listed, _ = rpc(adapter, "ListTasks", {})
        for task in listed["result"]["tasks"]:
            if task["id"] == task_id:
                self.assertNotIn(
                    "artifacts", task,
                    "includeArtifacts 缺省为 false 时字段 MUST 整个省略（§3.1.4）",
                )
        _, listed, _ = rpc(adapter, "ListTasks", {"includeArtifacts": True})
        for task in listed["result"]["tasks"]:
            if task["id"] == task_id:
                self.assertIn("artifacts", task)

    def test_cancel_task_then_reports_not_cancelable(self):
        adapter, _ = self.make()
        task_id = self._make_task(adapter)
        status, body, _ = rpc(adapter, "CancelTask", {"id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["status"]["state"], STATE_CANCELED)
        # 规范 §5.4: TaskNotCancelableError -> HTTP 400 + -32002
        status, body, _ = rpc(adapter, "CancelTask", {"id": task_id})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_TASK_NOT_CANCELABLE)

    def test_message_referencing_unknown_task_is_task_not_found(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("续跑", task_id="task-nope"))
        self.assertEqual(body["error"]["code"], ERR_TASK_NOT_FOUND)

    def test_mismatched_context_and_task_is_rejected(self):
        """规范 §3.4.3 明要求拒绝 contextId / taskId 不一致的请求。"""
        adapter, _ = self.make()
        task_id = self._make_task(adapter, context_id="ctx-A")
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("改口", context_id="ctx-B", task_id=task_id))
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)

    def test_message_to_a_terminal_task_is_unsupported(self):
        adapter, _ = self.make()
        task_id = self._make_task(adapter)
        rpc(adapter, "CancelTask", {"id": task_id})
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("再来", task_id=task_id))
        self.assertEqual(body["error"]["code"], ERR_UNSUPPORTED_OPERATION)

    def test_follow_up_message_reuses_the_context(self):
        adapter, hooks = self.make()
        task_id = self._make_task(adapter, "第一轮", context_id="ctx-shared")
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("第二轮", context_id="ctx-shared",
                                     task_id=task_id, message_id="m2"))
        self.assertEqual(
            task_of(body)["contextId"], "ctx-shared",
            "带 taskId 续跑时 contextId 必须沿用",
        )
        inbounds = hooks.wait_for_inbounds(2)
        self.assertEqual(len(inbounds), 2)
        # 同一对端的 conversation_id 保持不变（= 一个对端一个会话）
        self.assertEqual(inbounds[0].conversation_id, "a2a:local:127.0.0.1")
        self.assertEqual(inbounds[1].conversation_id, "a2a:local:127.0.0.1")

    def test_client_supplied_context_id_is_preserved(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("带上下文", context_id="ctx-from-client"))
        self.assertEqual(task_of(body)["contextId"], "ctx-from-client")

    def test_task_ledger_is_bounded(self):
        adapter, _ = self.make(max_tasks=8)
        for i in range(20):
            self._make_task(adapter, f"q{i}", message_id=f"m{i}")
        # 上限是 8，但至少有下限保护（>=8），且不会无限增长
        self.assertLessEqual(len(adapter._tasks), 32)


# ======================================================================
# 5. JSON-RPC 层：版本、方法名、错误码、畸形输入
# ======================================================================
class TestJsonRpcLayer(A2aServerTestCase):
    def test_method_names_are_pascal_case_v1(self):
        """规范 §5.3 / §9.4：JSON-RPC 方法名是 PascalCase，不是 message/send。"""
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "SendMessage", send_params("hi"))
        self.assertEqual(status, 200)
        self.assertIn("task", body["result"])
        for name in ("GetTask", "ListTasks", "CancelTask"):
            with self.subTest(name=name):
                self.assertIn(name, a2a_mod._METHOD_HANDLERS)

    def test_unknown_method_is_method_not_found(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "NoSuchMethod", {})
        self.assertEqual(body["error"]["code"], ERR_METHOD_NOT_FOUND)
        self.assertIn("SendMessage", body["error"]["message"])

    def test_legacy_slash_method_names_are_rejected_explicitly(self):
        """刻意**不**兼容 ``message/send`` 这类 v0.x 方法名。

        理由（适配器 docstring 里也写了，这里钉住）：v1.0 的响应形状是
        ``{"result": {"task": {...}}}``，v0.3 是裸 Task。接受旧名却用新形状回，
        等于拿一个可能错位的包去糊弄对端；回 -32601 并列出支持的方法更诚实。
        """
        adapter, _ = self.make()
        for legacy in ("message/send", "tasks/get", "tasks/cancel"):
            with self.subTest(method=legacy):
                _, body, _ = rpc(adapter, legacy, send_params("hi"))
                self.assertEqual(body["error"]["code"], ERR_METHOD_NOT_FOUND)
                self.assertIn("SendMessage", body["error"]["message"])

    def test_unsupported_a2a_version_is_rejected(self):
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "SendMessage", send_params("hi"), version="0.3")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_VERSION_NOT_SUPPORTED)

    def test_supported_a2a_versions_are_accepted(self):
        adapter, _ = self.make()
        for version in ("1.0", "1.0.0"):
            with self.subTest(version=version):
                status, _, _ = rpc(adapter, "SendMessage", send_params("hi"),
                                   version=version)
                self.assertEqual(status, 200)

    def test_message_is_required(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "SendMessage", {})
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)
        self.assertIn("message", body["error"]["message"])

    def test_non_object_body_is_invalid_request(self):
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "x", {}, raw_body=b"[1, 2, 3]")
        self.assertEqual(status, 200)
        self.assertEqual(body["error"]["code"], ERR_INVALID_REQUEST)

    def test_params_must_be_object(self):
        adapter, _ = self.make()
        reply = post_raw(adapter, json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": "nope"}
        ).encode())
        self.assertEqual(reply.json()["error"]["code"], ERR_INVALID_PARAMS)

    def test_bad_jsonrpc_version_is_invalid_request(self):
        adapter, _ = self.make()
        reply = post_raw(adapter, json.dumps(
            {"jsonrpc": "1.0", "id": 7, "method": "GetTask", "params": {}}
        ).encode())
        self.assertEqual(reply.json()["error"]["code"], ERR_INVALID_REQUEST)

    def test_missing_method_is_invalid_request(self):
        adapter, _ = self.make()
        reply = post_raw(adapter, json.dumps(
            {"jsonrpc": "2.0", "id": 7, "params": {}}
        ).encode())
        self.assertEqual(reply.json()["error"]["code"], ERR_INVALID_REQUEST)

    def test_empty_body_is_invalid_request(self):
        adapter, _ = self.make()
        reply = post_raw(adapter, b"")
        self.assertEqual(reply.json()["error"]["code"], ERR_INVALID_REQUEST)

    def test_error_envelope_is_jsonrpc_shaped_without_fake_data(self):
        """规范 §9.5：``error.data`` 里的对象 MUST 含 ``@type``。

        我们没有 google.rpc 类型可填，所以**整个省略 data** —— 硬塞一个自造结构
        比省略更糟（严格 ProtoJSON 解析器会直接报错）。
        """
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "NoSuchMethod", {})
        error = body["error"]
        self.assertEqual(set(error) == {"code", "message"}, True,
                         f"error 对象只应有 code/message，实际 {sorted(error)}")
        self.assertNotIn("data", error)

    def test_empty_task_is_rejected_with_a_task_not_an_error(self):
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "SendMessage", send_params(""))
        self.assertEqual(status, 200)
        self.assertEqual(task_of(body)["status"]["state"], STATE_REJECTED)

    def test_rejected_task_id_is_still_queryable(self):
        adapter, _ = self.make()
        _, body, _ = rpc(adapter, "SendMessage", send_params(""))
        task_id = task_of(body)["id"]
        _, got, _ = rpc(adapter, "GetTask", {"id": task_id})
        self.assertEqual(task_of(got)["status"]["state"], STATE_REJECTED)

    def test_response_content_type_is_json(self):
        """规范 §9.1：JSON-RPC 绑定用 application/json（不是 a2a+json）。"""
        adapter, _ = self.make()
        for path in (AGENT_CARD_PATH, HEALTH_PATH):
            with self.subTest(path=path):
                reply = http("GET", adapter._server.url(path))
                self.assertIn("application/json", reply.headers.get("Content-Type", ""))

    def test_rpc_is_also_reachable_at_the_base_path(self):
        adapter, _ = self.make()
        status, body, _ = rpc(adapter, "ListTasks", {}, path="/")
        self.assertEqual(status, 200)
        self.assertIn("tasks", body["result"])


# ======================================================================
# 6. 畸形输入不泄栈
# ======================================================================
class TestMalformedInput(A2aServerTestCase):
    LEAKS = ("traceback", "most recent call last", ".py\"", "opencode_bridge",
             "line ", "site-packages")

    def _assert_no_leak(self, text: str) -> None:
        lowered = text.lower()
        for needle in self.LEAKS:
            self.assertNotIn(needle, lowered, f"响应体泄漏了内部信息: {needle!r}")

    def test_malformed_json_returns_parse_error_without_trace(self):
        adapter, _ = self.make()
        status, body, text = rpc(adapter, "SendMessage", {},
                                 raw_body=b"{not json at all")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_PARSE)
        self._assert_no_leak(text)

    def test_invalid_utf8_body_returns_parse_error(self):
        adapter, _ = self.make()
        status, body, text = rpc(adapter, "SendMessage", {},
                                 raw_body=b"\xff\xfe\x00garbage")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_PARSE)
        self._assert_no_leak(text)

    def test_handler_exception_returns_500_without_trace(self):
        adapter, _ = self.make()
        original = a2a_mod._render_task

        def boom(*args, **kwargs):
            raise RuntimeError("secret internal detail 内部细节")

        a2a_mod._render_task = boom
        try:
            with self.assertLogs("opencode_bridge.adapters.a2a", level="ERROR"):
                status, body, text = rpc(adapter, "SendMessage", send_params("hi"))
        finally:
            a2a_mod._render_task = original
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], ERR_INTERNAL)
        self.assertNotIn("secret internal detail", text)
        self._assert_no_leak(text)

    def test_body_over_the_limit_returns_413(self):
        """被拒的 body 会被**读掉再回响应**，所以客户端一定拿得到 413。

        （不这么做的话内核会发 RST 把响应丢掉，客户端只看到"连接错误"——
        这在 Windows 上是概率性的，实测会随机挂掉测试。）
        """
        adapter, _ = self.make()
        adapter._server.max_body = 64
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": send_params("x" * 512),
        }).encode()
        self.assertGreater(len(payload), 64)
        reply = http("POST", adapter._server.url(RPC_PATHS[0]), body=payload,
                     headers={"Content-Type": "application/json"})
        self.assertEqual(reply.status, 413)
        self.assertEqual(reply.json()["error"], "payload too large")
        self.assertEqual(reply.json()["limit"], 64)
        self._assert_no_leak(reply.text)

    def test_oversized_body_discard_is_bounded(self):
        """丢弃量有硬顶：``Content-Length`` 由客户端控制，无上限地读等于把 DoS 面
        又打开一次。超过 :data:`_MAX_DISCARD` 就放弃（连接被 RST，客户端只见
        "连接错误"）。"""
        adapter, _ = self.make()
        adapter._server.max_body = 1024        # body 必须超过它才会走丢弃分支
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": send_params("x" * (httpsrv._MAX_DISCARD + 4096)),
        }).encode()
        self.assertGreater(len(payload), httpsrv._MAX_DISCARD)
        reply = http("POST", adapter._server.url(RPC_PATHS[0]), body=payload,
                     headers={"Content-Type": "application/json"}, timeout=20.0)
        # 丢弃到上限就放弃 -> 剩余字节没被读走 -> 连接被 RST -> 客户端只见连接错误
        self.assertIn(reply.status, (0, 413), f"意外状态 {reply.status}")
        self._assert_no_leak(reply.text)
        # 服务本身还活着
        self.assertEqual(http("GET", adapter._server.url(HEALTH_PATH)).status, 200)

    def test_real_limit_never_gets_a_500(self):
        """真按 1 MiB 上限发一次：无论服务端是回 413 还是提前关连接，
        **都不能**变成 500，更不能泄栈，服务还得继续活着。"""
        adapter, _ = self.make()
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": send_params("x" * (MAX_BODY_BYTES + 4096)),
        }).encode()
        self.assertGreater(len(payload), MAX_BODY_BYTES)
        reply = http("POST", adapter._server.url(RPC_PATHS[0]), body=payload,
                     headers={"Content-Type": "application/json"}, timeout=20.0)
        self.assertIn(reply.status, (0, 413), f"意外状态 {reply.status}")
        self._assert_no_leak(reply.text)
        # 服务本身还活着
        self.assertEqual(http("GET", adapter._server.url(HEALTH_PATH)).status, 200)

    def test_chunked_encoding_is_rejected_explicitly(self):
        """用裸 socket 发，因为 ``urllib`` 会自己算 Content-Length、无法构造真正的
        chunked 请求。裸 socket 还顺带证明了服务端**不会**挂在"等 chunked body"上。"""
        adapter, _ = self.make()
        payload = (
            b"POST /rpc HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", adapter.port), timeout=10) as sock:
            sock.sendall(payload)
            sock.settimeout(10)
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", "replace")
        self.assertIn("400 Bad Request", raw.splitlines()[0])
        self.assertIn("chunked", raw)
        self._assert_no_leak(raw)

    def test_unknown_path_returns_404(self):
        adapter, _ = self.make()
        reply = http("GET", adapter._server.url("/nope"))
        self.assertEqual(reply.status, 404)
        self.assertIn("error", reply.json())

    def test_wrong_method_returns_405_with_allow_header(self):
        adapter, _ = self.make()
        reply = http("GET", adapter._server.url(RPC_PATHS[0]))
        self.assertEqual(reply.status, 405)
        self.assertIn("POST", reply.headers.get("Allow", ""))
        self.assertEqual(reply.json()["error"], "method not allowed")

    def test_handler_returning_garbage_is_500_not_a_crash(self):
        adapter, _ = self.make()
        bad = httpsrv.Route(HEALTH_PATH, lambda r: "not a response", methods=("GET",))
        adapter._server._routes[(HEALTH_PATH, "GET")] = bad
        with self.assertLogs("opencode_bridge.httpsrv", level="ERROR"):
            reply = http("GET", adapter._server.url(HEALTH_PATH))
        self.assertEqual(reply.status, 500)
        self.assertEqual(reply.json()["error"], "internal error")
        self._assert_no_leak(reply.text)

    def test_hook_exception_fails_the_task_without_breaking_the_response(self):
        def boom(inbound: Inbound) -> None:
            raise RuntimeError("hook 内部炸了")

        adapter, _ = self.make(RecordingHooks(boom))
        with self.assertLogs("opencode_bridge.adapters.a2a", level="ERROR"):
            status, body, text = rpc(adapter, "SendMessage", send_params("hi",
                                                                       immediate=False))
        self.assertEqual(status, 200)
        task = task_of(body)
        self.assertEqual(task["status"]["state"], STATE_FAILED)
        self.assertIn("hook 内部炸了", task["status"]["message"]["parts"][0]["text"])
        # 任务状态里的失败原因是给 agent / 对端看的，不该带栈
        self.assertNotIn("Traceback", task["status"]["message"]["parts"][0]["text"])

    def test_client_disconnect_does_not_break_the_server(self):
        """客户端在等答复时把连接掐了 —— 服务必须继续活着（_write 里吞掉 BrokenPipe）。"""
        adapter, hooks = self.make(RecordingHooks(), reply_timeout=5.0)
        sock = socket.create_connection(("127.0.0.1", adapter.port), timeout=5)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                              "params": send_params("hi", immediate=False)}).encode()
        sock.sendall(
            b"POST /rpc HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
        )
        hooks.wait_for_inbound()
        sock.close()                              # 立刻挂断
        time.sleep(0.3)
        # 服务仍能正常回答下一个请求
        status, _, _ = rpc(adapter, "SendMessage", send_params("还活着吗"))
        self.assertEqual(status, 200)
        self.assertTrue(adapter.running)


# ======================================================================
# 7. 鉴权：默认不鉴权是本平台的既有风险，逐条钉死
# ======================================================================
class TestAuth(A2aServerTestCase):
    def test_default_is_no_authentication(self):
        """**默认行为：任何本机进程都能驱动 agent。**

        这是刻意选择（见模块 docstring 与 docs/a2a.md），不是 bug。所以这里既断言
        "确实不鉴权"，下一条再断言我们会**大声说**。
        """
        hooks, holder = self.replying_hooks("ok")
        adapter, _ = self.make(hooks)
        holder["adapter"] = adapter
        self.assertFalse(adapter._auth_enabled)
        status, body, _ = rpc(adapter, "SendMessage", send_params("匿名请求",
                                                               immediate=False))
        self.assertEqual(status, 200, "默认就该放行 —— 否则风险描述是假的")
        self.assertEqual(task_of(body)["status"]["state"], STATE_COMPLETED)
        self.assertEqual(hooks.wait_for_inbound().user_id, "local:127.0.0.1",
                         "无鉴权时身份退化成来源 IP，并在标识里明说")

    def test_missing_auth_is_logged_loudly_at_start(self):
        adapter = A2aAdapter({"bind_port": 0}, RecordingHooks())
        self.addCleanup(adapter.stop)
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING") as cap:
            adapter.start()
        blob = "\n".join(cap.output)
        self.assertIn("未配置任何凭据", blob)
        # 必须**点名**攻击面：浏览器里的恶意网页能打这个 URL
        self.assertIn("浏览器", blob)
        self.assertIn(str(adapter.port), blob)
        self.assertIn("127.0.0.1", blob)

    def test_shared_module_also_warns_when_no_authenticator(self):
        """共用模块自己也要在日志里说"未鉴权"（A3 的使用者会看到这一行）。"""
        server = httpsrv.HttpServer(port=0, name="noauth")
        try:
            with self.assertLogs("opencode_bridge.httpsrv", level="INFO") as cap:
                server.start()
            self.assertIn("未鉴权", "\n".join(cap.output))
        finally:
            server.stop()

    def test_agent_card_advertises_bearer_when_configured(self):
        adapter, _ = self.make(auth_token="s3cret")
        card = http("GET", adapter._server.url(AGENT_CARD_PATH)).json()
        schemes = card["securitySchemes"]
        self.assertIn("bearer", schemes)
        # 规范 §4.5.3：HTTPAuthSecurityScheme 的字段是 `scheme`（不是 OpenAPI 的 `type`）
        self.assertEqual(schemes["bearer"]["httpAuthSecurityScheme"]["scheme"], "bearer")
        # 规范 §4.4.1：字段叫 securityRequirements（不是 OpenAPI 的 `security`）
        self.assertEqual(card["securityRequirements"], [{"schemes": {"bearer": {}}}])
        self.assertNotIn("security", card, "v1.0 没有 OpenAPI 的 `security` 字段")

    def test_shared_token_auth(self):
        adapter, _ = self.make(auth_token="s3cret")
        self.assertEqual(rpc(adapter, "ListTasks", {})[0], 401)
        self.assertEqual(rpc(adapter, "ListTasks", {}, token="wrong")[0], 401)
        status, body, _ = rpc(adapter, "ListTasks", {}, token="s3cret")
        self.assertEqual(status, 200)
        self.assertIn("tasks", body["result"])

    def test_unauthorized_carries_www_authenticate_challenge(self):
        """规范 §3.3.2：认证错误 SHOULD 带 challenge 信息。"""
        adapter, _ = self.make(auth_token="s3cret")
        reply = post_raw(adapter, json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
        ).encode())
        self.assertEqual(reply.status, 401)
        self.assertIn("Bearer", reply.headers.get("WWW-Authenticate", ""))

    def test_unauthorized_body_is_not_a_fake_jsonrpc_error(self):
        """规范把认证失败映射为 HTTP 401 且**未定义** JSON-RPC 码 -> 不凭空造一个。"""
        adapter, _ = self.make(auth_token="s3cret")
        _, _, text = rpc(adapter, "ListTasks", {})
        self.assertEqual(json.loads(text), {"error": "unauthorized"})

    def test_per_peer_tokens_give_named_identities(self):
        adapter, hooks = self.make(RecordingHooks(),
                                   peer_tokens="alice:tok-a,bob:tok-b")
        rpc(adapter, "SendMessage", send_params("alice 的活", message_id="m-a"),
            token="tok-a")
        rpc(adapter, "SendMessage", send_params("bob 的活", message_id="m-b"),
            token="tok-b")
        identities = sorted(ib.user_id for ib in hooks.wait_for_inbounds(2))
        self.assertEqual(identities, ["alice", "bob"])
        conversations = sorted({ib.conversation_id for ib in hooks.inbounds})
        self.assertEqual(conversations, ["a2a:alice", "a2a:bob"])

    def test_malformed_authorization_header_is_rejected(self):
        adapter, _ = self.make(auth_token="s3cret")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ListTasks",
                              "params": {}}).encode()
        for bad in ("Basic abc", "Bearer", "bearer  ", "Token s3cret", "Bearer "):
            with self.subTest(header=bad):
                reply = http("POST", adapter._server.url(RPC_PATHS[0]), body=payload,
                             headers={"Authorization": bad})
                self.assertEqual(reply.status, 401)

    def test_bearer_scheme_is_case_insensitive(self):
        """RFC 7235：认证方案名大小写不敏感。"""
        adapter, _ = self.make(auth_token="s3cret")
        reply = http("POST", adapter._server.url(RPC_PATHS[0]),
                     body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ListTasks",
                                      "params": {}}).encode(),
                     headers={"Authorization": "bearer s3cret"})
        self.assertEqual(reply.status, 200)

    def test_agent_card_and_health_are_public_even_with_auth(self):
        """Agent Card 必须**公开** —— 客户端要靠它才知道要不要带凭据（§7.3）。"""
        adapter, _ = self.make(auth_token="s3cret")
        self.assertEqual(http("GET", adapter._server.url(AGENT_CARD_PATH)).status, 200)
        self.assertEqual(http("GET", adapter._server.url(HEALTH_PATH)).status, 200)

    def test_route_requiring_auth_without_authenticator_fails_closed(self):
        """共用模块的纪律：路由说"要鉴权"而服务上没有鉴权器 -> **一律拒绝**。

        这条不是 a2a 的行为（a2a 总是提供鉴权器），但它保护的是 A3 的 webhook
        适配器：配错一次绝不能退化成"静默全开"。
        """
        server = httpsrv.HttpServer(
            port=0, name="failclosed", authenticate=None,
            routes=[Route("/secret", lambda r: HttpResponse.json({"ok": True}),
                          methods=("GET",), require_auth=True)],
        )
        try:
            server.start()
            with self.assertLogs("opencode_bridge.httpsrv", level="ERROR") as cap:
                reply = http("GET", server.url("/secret"))
            self.assertEqual(reply.status, 401)
            self.assertIn("fail closed", "\n".join(cap.output))
        finally:
            server.stop()

    def test_allowlist_gate_rejects_non_whitelisted_peer(self):
        adapter, hooks = self.make(RecordingHooks(),
                                   peer_tokens="alice:tok-a,bob:tok-b",
                                   allowed_chat_ids=["alice"])
        status, body, _ = rpc(adapter, "SendMessage", send_params("bob 的活"),
                              token="tok-b")
        self.assertEqual(status, 200)
        self.assertEqual(task_of(body)["status"]["state"], STATE_REJECTED)
        self.assertEqual(hooks.inbounds, [], "未授权对端的消息不能产生 Inbound")

        _, body, _ = rpc(adapter, "SendMessage", send_params("alice 的活"),
                         token="tok-a")
        self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING)
        self.assertEqual(hooks.wait_for_inbound().user_id, "alice")


# ======================================================================
# 8. 并发 + 干净停机
# ======================================================================
class TestConcurrencyAndStop(A2aServerTestCase):
    def test_slow_request_does_not_block_other_requests(self):
        """``HTTPServer`` 是**单线程**的；一个卡住的请求会把整个服务卡死。

        这里让两个 ``SendMessage`` 都停在"等 agent 答复"（真实场景：agent 要跑几分钟），
        同时验证**另一条连接**上的 ``/health`` 与**第二个** ``SendMessage`` 都能立刻被
        处理 —— 这正是选用 ``ThreadingHTTPServer`` 而不是 ``HTTPServer`` 的理由。
        """
        adapter, hooks = self.make(RecordingHooks(), reply_timeout=60.0)
        results: list = []

        def park(index: int) -> None:
            status, body, _ = rpc(adapter, "SendMessage",
                                  send_params(f"慢活 {index}", immediate=False,
                                              message_id=f"m-{index}"),
                                  timeout=60.0)
            results.append((index, status, body))

        workers = [threading.Thread(target=park, args=(i,)) for i in (1, 2)]
        for worker in workers:
            worker.start()
        inbounds = hooks.wait_for_inbounds(2, timeout=15.0)
        self.assertEqual(len(inbounds), 2, "第二个请求被第一个慢请求堵住了")

        started = time.monotonic()
        reply = http("GET", adapter._server.url(HEALTH_PATH), timeout=5.0)
        elapsed = time.monotonic() - started
        self.assertEqual(reply.status, 200)
        self.assertLess(elapsed, 2.0, f"/health 被慢请求拖慢到 {elapsed:.2f}s")
        self.assertEqual(reply.json()["pendingTasks"], 2)

        for inbound in inbounds:
            adapter.send(Outbound(inbound.conversation_id, "答复", kind="final"))
        for worker in workers:
            worker.join(timeout=15.0)
            self.assertFalse(worker.is_alive(), "等答复的请求没被唤醒")
        self.assertEqual([status for _, status, _ in results], [200, 200])
        self.assertTrue(all(task_of(b)["status"]["state"] == STATE_COMPLETED
                            for _, _, b in results))

    def test_two_peers_get_independent_conversations(self):
        adapter, hooks = self.make(RecordingHooks(), reply_timeout=60.0,
                                   peer_tokens="alice:tok-a,bob:tok-b")
        results: list = []

        def park(token: str, text: str) -> None:
            results.append(rpc(adapter, "SendMessage",
                               send_params(text, immediate=False, message_id=text),
                               token=token, timeout=60.0))

        threads = [threading.Thread(target=park, args=("tok-a", "alice 的活")),
                   threading.Thread(target=park, args=("tok-b", "bob 的活"))]
        for thread in threads:
            thread.start()
        inbounds = hooks.wait_for_inbounds(2, timeout=15.0)
        self.assertEqual({ib.conversation_id for ib in inbounds},
                         {"a2a:alice", "a2a:bob"})
        # 各自答复必须回到**各自**的请求
        for inbound in inbounds:
            adapter.send(Outbound(inbound.conversation_id, f"给 {inbound.user_id}",
                                  kind="final"))
        for thread in threads:
            thread.join(timeout=15.0)
        # rpc() 返回 (status, parsed, text)，中间那个才是解析后的响应体
        replies = {task_of(parsed)["artifacts"][0]["parts"][0]["text"]
                   for _status, parsed, _text in results}
        self.assertEqual(replies, {"给 alice", "给 bob"},
                         "两个对端的答复串了 —— 待答复任务没有按对端隔离")

    def test_stop_is_fast_even_with_a_parked_request(self):
        """**本项目栽过"stop 白等超时"的坑** —— 这里钉死耗时上界。

        若忘了唤醒等待中的 handler 线程，停机就会白等 ``reply_timeout``（默认 300s）。
        """
        adapter, hooks = self.make(RecordingHooks(), reply_timeout=300.0)
        parked: list = []

        def park() -> None:
            parked.append(rpc(adapter, "SendMessage", send_params("长活", immediate=False),
                              timeout=60.0))

        worker = threading.Thread(target=park)
        worker.start()
        hooks.wait_for_inbound(timeout=10.0)

        started = time.monotonic()
        adapter.stop()
        elapsed = time.monotonic() - started
        worker.join(timeout=15.0)

        self.assertLess(elapsed, STOP_BUDGET,
                        f"stop() 用了 {elapsed:.2f}s（预算 {STOP_BUDGET}s）"
                        f" —— 忘了先唤醒等待中的请求？")
        self.assertFalse(worker.is_alive(), "等待中的 HTTP 请求没被 stop() 唤醒")
        self.assertEqual(len(parked), 1)
        status, body, _ = parked[0]
        self.assertEqual(status, 200, "停机时等待中的请求也应拿到一个诚实的结果")
        self.assertEqual(task_of(body)["status"]["state"], STATE_CANCELED)
        self.assert_clean(adapter)

    def test_stop_cancels_tasks_that_were_never_dispatched(self):
        """停机后队列里没投递的任务必须判失败，而不是继续触发 agent 运行。"""
        adapter = A2aAdapter({"bind_port": 0, "reply_timeout": 30.0},
                             RecordingHooks())
        self.addCleanup(adapter.stop)
        # 不 start()：直接手工塞一条进队列，模拟"handler 收到请求但还没走到投递"
        orphan = adapter._new_task("ctx-orphan", "local:127.0.0.1")
        adapter._queue.put((orphan, Inbound(conversation_id="a2a:local:127.0.0.1",
                                             text="x", platform="a2a")))
        adapter._stop_event.set()
        self.assertEqual(adapter._cancel_queued(), 1)
        self.assertEqual(orphan.state, STATE_CANCELED)
        self.assertTrue(orphan.done.is_set())
        self.assertEqual(adapter.hooks.inbounds, [])

    def test_stop_cancels_queued_items_before_the_sentinel(self):
        adapter = A2aAdapter({"bind_port": 0, "reply_timeout": 30.0},
                             RecordingHooks())
        self.addCleanup(adapter.stop)
        tasks = []
        for i in range(3):
            task = adapter._new_task(f"ctx-{i}", "local:127.0.0.1")
            tasks.append(task)
            adapter._queue.put((task, Inbound(conversation_id="a2a:local:127.0.0.1",
                                              text="x", platform="a2a")))
        adapter._queue.put(None)          # 哨兵排在最后，模拟真实 stop() 顺序
        self.assertEqual(adapter._cancel_queued(), 3)
        for task in tasks:
            self.assertIn(task.state, TERMINAL_STATES)

    def test_stop_releases_the_port(self):
        adapter, _ = self.make()
        port = adapter.port
        adapter.stop()
        self.assertTrue(port_is_free(port), f"端口 {port} 没释放")

    def test_stop_before_start_is_a_noop(self):
        adapter = A2aAdapter({"bind_port": 0}, RecordingHooks())
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_restart_after_stop_works(self):
        adapter, _ = self.make()
        adapter.stop()
        adapter.start()
        self.assertTrue(adapter.running)
        self.assertGreater(adapter.port, 0)
        self.assertEqual(http("GET", adapter._server.url(HEALTH_PATH)).status, 200)

    def test_stats_expose_observability(self):
        adapter, _ = self.make()
        rpc(adapter, "SendMessage", send_params("q"))
        stats = adapter.stats()
        self.assertEqual(stats["tasks_created"], 1)
        self.assertEqual(stats["tasks_pending"], 1)
        self.assertEqual(stats["contexts"], 1)
        self.assertEqual(stats["http_requests"], 1)


# ======================================================================
# 9. 防回环：按 contextId 计轮次，**不做内容启发式**
# ======================================================================
class TestAntiLoop(A2aServerTestCase):
    def test_turn_budget_per_context(self):
        adapter, _ = self.make(max_turns=3)
        for i in range(3):
            _, body, _ = rpc(adapter, "SendMessage",
                             send_params(f"q{i}", context_id="ctx-loop",
                                         message_id=f"m{i}"))
            self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING,
                             f"第 {i + 1} 轮不该被拒")
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("q4", context_id="ctx-loop", message_id="m4"))
        task = task_of(body)
        self.assertEqual(task["status"]["state"], STATE_REJECTED)
        self.assertIn("anti-loop", task["status"]["message"]["parts"][0]["text"])

    def test_separate_contexts_have_separate_budgets(self):
        adapter, _ = self.make(max_turns=1)
        for ctx in ("ctx-a", "ctx-b", "ctx-c"):
            _, body, _ = rpc(adapter, "SendMessage",
                             send_params("hi", context_id=ctx, message_id=ctx))
            self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING,
                             f"{ctx} 的第一轮不该被拒（预算是按 contextId 记的）")

    def test_no_content_heuristic_drops_look_alike_messages(self):
        """**不做"看起来像自己发的就丢"的启发式**（反例钉死）。

        用户 / 对端很可能把我们上一条的答复原文再发回来（追问、引用、测试）。
        按内容丢会误伤真实请求 —— 本仓库在 ntfy（拿 ``title`` 当身份）和 email 上都
        因此吃过亏。所以防回环**只**按 ``contextId`` 计轮次。
        """
        previous_reply = "这是上一轮的答复原文，请照此继续"
        adapter, _ = self.make(max_turns=5)
        for i in range(5):
            _, body, _ = rpc(adapter, "SendMessage",
                             send_params(previous_reply, context_id="ctx-echo",
                                         message_id=f"m{i}"))
            self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING,
                             f"第 {i + 1} 轮因为内容像自己的输出被丢了 —— "
                             f"这是被明确禁止的启发式")

    def test_turn_counter_prunes_idle_contexts(self):
        adapter, _ = self.make()
        adapter._turns["ctx-touched"] = (99, time.time() - 10_000)
        adapter._turns["ctx-untouched"] = (99, time.time() - 10_000)
        rpc(adapter, "SendMessage",
            send_params("hi", context_id="ctx-touched", message_id="m"))
        # 被用到的那个：淘汰旧计数后重新从 1 开始
        self.assertEqual(adapter._turns["ctx-touched"][0], 1)
        # 没被用到的那个：必须被淘汰，否则对端可以无限堆 contextId 耗内存
        self.assertNotIn("ctx-untouched", adapter._turns)

    def test_reset_turns_clears_the_budget(self):
        adapter, _ = self.make(max_turns=1)
        rpc(adapter, "SendMessage",
            send_params("hi", context_id="ctx-r", message_id="m1"))
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("hi", context_id="ctx-r", message_id="m2"))
        self.assertEqual(task_of(body)["status"]["state"], STATE_REJECTED)
        adapter.reset_turns("ctx-r")
        _, body, _ = rpc(adapter, "SendMessage",
                         send_params("hi", context_id="ctx-r", message_id="m3"))
        self.assertEqual(task_of(body)["status"]["state"], STATE_WORKING)

    def test_max_turns_from_config_and_hard_cap(self):
        adapter = self.build(RecordingHooks(), max_turns=2)
        self.assertEqual(adapter.max_turns, 2)
        over = self.build(RecordingHooks(), max_turns=10_000)
        self.assertEqual(over.max_turns, HARD_MAX_TURNS,
                         "防回环不能被配置成无限")
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING"):
            bad = self.build(RecordingHooks(), max_turns=0)
        self.assertEqual(bad.max_turns, DEFAULT_MAX_TURNS)
        self.assertGreater(HARD_MAX_TURNS, DEFAULT_MAX_TURNS)

    def test_reply_timeout_from_config_and_fallback(self):
        adapter = A2aAdapter({"bind_port": 0, "reply_timeout": 12}, RecordingHooks())
        self.addCleanup(adapter.stop)
        self.assertEqual(adapter.reply_timeout, 12.0)
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING") as cap:
            bad = A2aAdapter({"bind_port": 0, "reply_timeout": "nope"}, RecordingHooks())
        self.addCleanup(bad.stop)
        self.assertGreater(bad.reply_timeout, 0)
        self.assertIn("reply_timeout", "\n".join(cap.output))
        with self.assertLogs("opencode_bridge.adapters.a2a", level="WARNING"):
            zero = A2aAdapter({"bind_port": 0, "reply_timeout": 0}, RecordingHooks())
        self.addCleanup(zero.stop)
        self.assertGreater(zero.reply_timeout, 0)
        # 没配就静默回落（不刷没意义的警告）
        quiet = A2aAdapter({"bind_port": 0}, RecordingHooks())
        self.addCleanup(quiet.stop)
        self.assertEqual(quiet.reply_timeout, 300.0)

    def test_max_tasks_from_config(self):
        adapter = A2aAdapter({"bind_port": 0, "max_tasks": 64}, RecordingHooks())
        self.addCleanup(adapter.stop)
        self.assertEqual(adapter.max_tasks, 64)
        self.assertEqual(A2aAdapter({"bind_port": 0}, RecordingHooks()).max_tasks,
                         a2a_mod.MAX_TASKS)


# ======================================================================
# 10. 纯函数：Part 抽取、配置强制转换
# ======================================================================
class TestExtractText(unittest.TestCase):
    def test_text_parts_are_joined(self):
        self.assertEqual(extract_text({"parts": [{"text": "a"}, {"text": "b"}]}), "a\nb")

    def test_missing_or_malformed_parts(self):
        self.assertEqual(extract_text({}), "")
        self.assertEqual(extract_text({"parts": "nope"}), "")
        self.assertEqual(extract_text(None), "")
        self.assertEqual(extract_text("not a dict"), "")
        self.assertEqual(extract_text({"parts": [None, 1, "x"]}), "")

    def test_non_text_parts_are_summarised_not_dropped(self):
        """丢掉会让对端发了文件却表现为"收到空消息" —— 那是最难查的故障。"""
        text = extract_text({"parts": [
            {"text": "看这个"},
            {"url": "http://x/y.pdf", "filename": "y.pdf",
             "mediaType": "application/pdf"},
            {"raw": "QUJD", "filename": "z.bin"},
            {"data": {"k": 1}, "mediaType": "application/json"},
        ]})
        self.assertIn("看这个", text)
        self.assertIn("y.pdf", text)
        self.assertIn("http://x/y.pdf", text)
        self.assertIn("base64", text)
        self.assertIn('"k": 1', text)

    def test_base64_length_is_not_claimed_to_be_bytes(self):
        text = extract_text({"parts": [{"raw": "QUJDRA=="}]})
        self.assertIn("8 chars", text)
        self.assertNotIn("8 bytes", text)


class TestCoercion(unittest.TestCase):
    def test_coerce_port_distinguishes_missing_from_ephemeral(self):
        self.assertEqual(_coerce_port(None), -1)
        self.assertEqual(_coerce_port(""), -1)
        self.assertEqual(_coerce_port("abc"), -1)
        self.assertEqual(_coerce_port(70000), -1)
        self.assertEqual(_coerce_port(-1), -1)
        self.assertEqual(_coerce_port(0), 0)          # 显式 0 = 临时端口（测试用）
        self.assertEqual(_coerce_port("9900"), 9900)
        self.assertEqual(_coerce_port(9900), 9900)

    def test_parse_peer_tokens_string_and_dict(self):
        self.assertEqual(_parse_peer_tokens("alice:t1,bob:t2"),
                         (("t1", "alice"), ("t2", "bob")))
        self.assertEqual(_parse_peer_tokens({"carol": "t3"}), (("t3", "carol"),))
        self.assertEqual(_parse_peer_tokens(None), ())
        self.assertEqual(_parse_peer_tokens(""), ())
        # 缺冒号 / 空值 / 空名 一律丢弃，不产出半个凭据
        self.assertEqual(_parse_peer_tokens("alice"), ())
        self.assertEqual(_parse_peer_tokens(":t1"), ())
        self.assertEqual(_parse_peer_tokens("alice:"), ())

    def test_local_of_keeps_colons_inside_the_peer_name(self):
        self.assertEqual(_local_of("a2a:local:127.0.0.1"), "local:127.0.0.1")
        self.assertEqual(_local_of("a2a:bearer:10.0.0.5"), "bearer:10.0.0.5")
        self.assertEqual(_local_of("alice"), "alice")
        self.assertEqual(_local_of(""), "")

    def test_local_of_is_inverse_of_conversation_id(self):
        adapter = A2aAdapter({"bind_port": 0}, RecordingHooks())
        for peer in ("alice", "local:127.0.0.1", "bearer:10.0.0.5"):
            cid = adapter._conversation_id(peer)
            self.assertEqual(platform_of(cid), "a2a")
            self.assertEqual(_local_of(cid), peer)


class TestSharedModule(A2aServerTestCase):
    """共用模块自身的少量契约（A3 复用它时依赖这些）。"""

    def test_normalize_path(self):
        self.assertEqual(httpsrv.normalize_path("rpc"), "/rpc")
        self.assertEqual(httpsrv.normalize_path("/rpc/"), "/rpc")
        self.assertEqual(httpsrv.normalize_path(""), "/")
        self.assertEqual(httpsrv.normalize_path("/"), "/")
        self.assertEqual(httpsrv.normalize_path("//a//b"), "//a//b")

    def test_routes_are_deduplicated_and_sorted(self):
        adapter, _ = self.make()
        paths = [r.path for r in adapter._server.routes()]
        self.assertEqual(paths, sorted(set(paths)))
        self.assertIn(AGENT_CARD_PATH, paths)
        self.assertIn(HEALTH_PATH, paths)
        for path in RPC_PATHS:
            self.assertIn(path, paths)
        # RPC 路由要求鉴权，Agent Card / health 不要求（公开发现）
        by_path = {r.path: r for r in adapter._server.routes()}
        self.assertTrue(by_path[RPC_PATHS[0]].require_auth)
        self.assertFalse(by_path[AGENT_CARD_PATH].require_auth)
        self.assertFalse(by_path[HEALTH_PATH].require_auth)

    def test_duplicate_route_is_rejected(self):
        server = httpsrv.HttpServer(port=0, name="dup")
        server.add_route(Route("/x", lambda r: HttpResponse.text("1")))
        with self.assertRaises(ValueError):
            server.add_route(Route("/x", lambda r: HttpResponse.text("2")))

    def test_same_path_different_method_is_allowed(self):
        """同一路径的不同方法各占一条（webhook 的 GET 探活 + POST 回调很常见）。"""
        server = httpsrv.HttpServer(port=0, name="twomethods")
        server.add_route(Route("/x", lambda r: HttpResponse.text("g"), methods=("GET",)))
        server.add_route(Route("/x", lambda r: HttpResponse.text("p"), methods=("POST",)))
        # 路由视图按路径去重（一个路径一条），方法集看内部表
        self.assertEqual(len(server.routes()), 1)
        self.assertEqual(sorted(m for _p, m in server._routes if _p == "/x"),
                         ["GET", "POST"])

    def test_cannot_add_routes_after_start(self):
        server = httpsrv.HttpServer(port=0, name="late")
        try:
            server.start()
            with self.assertRaises(RuntimeError):
                server.add_route(Route("/y", lambda r: HttpResponse.text("1")))
        finally:
            server.stop()

    def test_non_callable_handler_is_rejected(self):
        server = httpsrv.HttpServer(port=0, name="bad")
        with self.assertRaises(TypeError):
            server.add_route(Route("/x", "not callable"))

    def test_bind_error_message_names_the_address(self):
        server = httpsrv.HttpServer(host=UNASSIGNABLE_IP, port=0, name="busy")
        with self.assertRaises(httpsrv.BindError) as ctx:
            server.start()
        self.assertIn(UNASSIGNABLE_IP, str(ctx.exception))

    def test_start_is_idempotent(self):
        server = httpsrv.HttpServer(port=0, name="idem")
        try:
            first = server.start()
            self.assertEqual(server.start(), first)
            self.assertTrue(server.bound)
        finally:
            server.stop()
        self.assertFalse(server.bound)

    def test_url_uses_the_actual_bound_address(self):
        adapter, _ = self.make()
        self.assertEqual(adapter._server.url("/x"),
                         f"http://127.0.0.1:{adapter.port}/x")
        self.assertEqual(adapter._server.url(), f"http://127.0.0.1:{adapter.port}")

    def test_unauthorized_hook_result_is_validated(self):
        """``unauthorized()`` 自己出错时必须回默认 401，而不是崩掉连接。"""
        def bad(_request):
            raise RuntimeError("boom")

        server = httpsrv.HttpServer(
            port=0, name="unauth", authenticate=lambda r: None, unauthorized=bad,
            routes=[Route("/p", lambda r: HttpResponse.text("x"),
                          methods=("GET",), require_auth=True)],
        )
        try:
            server.start()
            with self.assertLogs("opencode_bridge.httpsrv", level="ERROR"):
                reply = http("GET", server.url("/p"))
            self.assertEqual(reply.status, 401)
        finally:
            server.stop()

    def test_authenticator_exception_fails_closed(self):
        def boom(_request):
            raise RuntimeError("auth exploded")

        server = httpsrv.HttpServer(
            port=0, name="authexplode", authenticate=boom,
            routes=[Route("/p", lambda r: HttpResponse.text("x"),
                          methods=("GET",), require_auth=True)],
        )
        try:
            server.start()
            with self.assertLogs("opencode_bridge.httpsrv", level="ERROR"):
                reply = http("GET", server.url("/p"))
            self.assertEqual(reply.status, 401)
        finally:
            server.stop()

    def test_request_helpers_read_case_insensitively(self):
        request = HttpRequest(
            method="POST", path="/x", query={"a": ["1", "2"]},
            headers={"authorization": "Bearer t", "content-type": "application/json"},
            body=b'{"k": 1}', client_host="127.0.0.1",
        )
        self.assertEqual(request.header("Authorization"), "Bearer t")
        self.assertEqual(request.header("CONTENT-TYPE"), "application/json")
        self.assertEqual(request.header("missing", "d"), "d")
        self.assertEqual(request.query_one("a"), "1")
        self.assertEqual(request.query_one("nope", "d"), "d")
        self.assertEqual(request.json(), {"k": 1})
        self.assertEqual(request.peer, "")

    def test_request_json_raises_on_empty_body(self):
        request = HttpRequest(method="POST", path="/x", query={}, headers={},
                              body=b"", client_host="")
        with self.assertRaises(ValueError):
            request.json()

    def test_http_response_json_keeps_unicode(self):
        response = HttpResponse.json({"t": "中文"})
        self.assertIn("中文".encode("utf-8"), response.body)
        self.assertEqual(json.loads(response.body.decode("utf-8")), {"t": "中文"})

    def test_route_normalizes_path_on_construction(self):
        route = Route("rpc/", lambda r: HttpResponse.text("x"), methods=("get",))
        self.assertEqual(route.path, "/rpc")
        self.assertEqual(route.methods, ("GET",))
        self.assertEqual(route.name, "/rpc")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()