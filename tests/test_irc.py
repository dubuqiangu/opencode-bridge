"""T3.3 IRC adapter tests（**不 mock socket**：本机回环真服务器线程）。

``tests/test_ws.py`` 用 ``socket`` + ``threading`` 在 ``127.0.0.1:0`` 上起真服务器，
这轮沿用同一套组织方式写一个 IRC 版 :class:`IrcServer`：它按 IRC 行协议收
``CRLF`` 行、默认自动回 001/366、并把收到的原始行记下来供断言。这样
"512 字节行长上限"这类**字节级**行为能被真正验证，而不是只测内部实现。

覆盖：能力声明、注册时序、PING/PONG 与保活、私聊/提及入站、控制码清理、
防回环、PRIVMSG 行形状、512 字节截断、分片、edit 恒 False、缺配置不起线程、
断线重连、结构化发送失败、stop 立刻唤醒阻塞 recv。
"""

from __future__ import annotations

import base64
import logging
import socket
import threading
import time
import unittest

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import adapter_class, build, registered_names
from opencode_bridge.adapters.irc import (
    LINE_LIMIT,
    MESSAGE_LIMIT,
    IRCAdapter,
    _byte_safe_split,
    _fit_utf8,
    _parse_line,
    _prefix_nick,
    strip_control_codes,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError

NICK = "bot"
CHAN = "#chan"


# ----------------------------------------------------------------------
# 本机回环的假 IRC 服务器（真 socket，照 test_ws.py 的 Server 写法）
# ----------------------------------------------------------------------
class IrcServer(threading.Thread):
    """在 ``127.0.0.1:0`` 上说 IRC 行协议的真服务器线程。

    * 收到的每一行都记进 :attr:`lines`（不含 ``CRLF``），可用 :meth:`wait_for`
      阻塞等待某一行的出现；
    * ``auto_welcome=True`` 时自动用 001 应答 ``NICK``、用 366 应答 ``JOIN``；
    * :meth:`drop` 主动断开当前连接，用来验证重连。
    """

    def __init__(self, *, auto_welcome: bool = True, nick: str = NICK) -> None:
        super().__init__(daemon=True)
        self.auto_welcome = auto_welcome
        self.nick = nick
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.lines: list[str] = []
        self.connections = 0
        self.error: BaseException | None = None
        self._conns: list[socket.socket] = []
        self._cv = threading.Condition()
        self._closing = False
        self._begin = False  # 不能叫 _started（Thread 内部属性）

    # -- 生命周期 ------------------------------------------------------
    def start_server(self) -> None:
        if not self._begin:
            self._begin = True
            self.start()
        deadline = time.time() + 5
        while self.port == 0 and time.time() < deadline:  # pragma: no cover
            time.sleep(0.01)

    def run(self) -> None:
        self._listener.settimeout(0.2)
        while not self._closing:
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            with self._cv:
                self._conns.append(conn)
                self._cv.notify_all()
            threading.Thread(
                target=self._serve, args=(conn,), daemon=True
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = bytearray()
        conn.settimeout(0.2)
        try:
            while not self._closing:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    raw, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    line = raw.rstrip(b"\r").decode("utf-8", "replace")
                    with self._cv:
                        self.lines.append(line)
                        self._cv.notify_all()
                    try:
                        self._react(line)
                    except OSError:
                        return
        finally:
            with self._cv:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _react(self, line: str) -> None:
        if not self.auto_welcome:
            return
        head, _, _ = line.partition(" ")
        if head == "NICK":
            self.send(f":irc.test 001 {self.nick} :Welcome to the test IRC network")
        elif head == "JOIN":
            for chan in line.split(" ", 1)[1].split(","):
                self.send(f":irc.test 366 {self.nick} {chan} :End of /NAMES list.")

    # -- 断言辅助 ------------------------------------------------------
    def send(self, line: str) -> None:
        """向**当前**连接发一行（无连接时静默忽略）。"""
        with self._cv:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.sendall(f"{line}\r\n".encode("utf-8"))
                return
            except OSError:
                continue

    def send_raw(self, data: bytes) -> None:
        with self._cv:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.sendall(data)
                return
            except OSError:
                continue

    def wait_for(self, pred, timeout: float = 5.0) -> str | None:
        """等一行满足 ``pred(line)`` 的行出现；返回该行（超时返回 None）。"""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for line in self.lines:
                    if pred(line):
                        return line
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.2))

    def wait_connections(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        with self._cv:
            while self.connections < count:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.2))
            return True

    def privmsg_lines(self, target: str | None = None) -> list[str]:
        """收到过的 ``PRIVMSG`` 行（按到达顺序）。"""
        out = []
        for line in self.lines:
            if not line.startswith("PRIVMSG "):
                continue
            if target is not None and line.split(" ")[1] != target:
                continue
            out.append(line)
        return out

    def wait_privmsg(self, count: int = 1, timeout: float = 3.0) -> list[str]:
        """等服务器**记录到**至少 ``count`` 条 PRIVMSG（发送是异步的，必须等）。"""
        deadline = time.time() + timeout
        while True:
            lines = self.privmsg_lines()
            if len(lines) >= count or time.time() >= deadline:
                return lines
            time.sleep(0.01)

    def wait_privmsg_quiet(
        self, timeout: float = 3.0, quiet: float = 0.25
    ) -> list[str]:
        """等到"不再有新 PRIVMSG 到达"为止 —— 多行分片必须这么等。"""
        deadline = time.time() + timeout
        last, changed_at = -1, time.time()
        while time.time() < deadline:
            count = len(self.privmsg_lines())
            if count != last:
                last, changed_at = count, time.time()
            elif count > 0 and time.time() - changed_at >= quiet:
                break
            time.sleep(0.02)
        return self.privmsg_lines()

    def drop(self) -> None:
        """断开当前连接（模拟掉线）。"""
        with self._cv:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._closing = True
        self.drop()
        if self._begin:
            self.join(5)
        else:
            self._listener.close()
        try:
            self._listener.close()
        except Exception:
            pass

    def __enter__(self) -> "IrcServer":
        self.start_server()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------
class RecordingHooks:
    """Minimal ``Hooks`` implementation that records every call."""

    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


def make_irc(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {"host": "127.0.0.1", "nick": NICK, "channels": CHAN}
    if config:
        cfg.update(config)
    adapter = IRCAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0        # no artificial sleeps in tests
    adapter.reconnect_delay = 0.05
    adapter.ping_interval = 300.0   # keepalive off unless a test asks for it
    return adapter, adapter.hooks


class IRCTestCase(unittest.TestCase):
    """带真服务器线程的测试基类。"""

    def make_server(self, **kw) -> IrcServer:
        server = IrcServer(**kw)
        self.addCleanup(server.stop)
        server.start_server()
        return server

    def connect_adapter(self, adapter, server: IrcServer) -> None:
        """把适配器指到测试服务器上（不自动 start）。"""
        adapter.host = "127.0.0.1"
        adapter.port = server.port

    def start_and_wait_registered(self, adapter, server: IrcServer):
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(
            server.wait_for(lambda line: line.startswith("JOIN ")), "未发出 JOIN"
        )
        deadline = time.time() + 5
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(adapter._registered, "未收到 001")
        return adapter


# ----------------------------------------------------------------------
# 能力声明 / 配置
# ----------------------------------------------------------------------
class TestIRCCapabilities(unittest.TestCase):
    """T1.1 能力显式声明 + T3.x required_tokens。"""

    def test_capabilities_truthful(self):
        adapter, _ = make_irc()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "irc")
        self.assertEqual(caps["label"], "IRC")
        self.assertEqual(caps["max_message_length"], MESSAGE_LIMIT)
        self.assertEqual(MESSAGE_LIMIT, 400)
        self.assertEqual(MESSAGE_LIMIT, LINE_LIMIT - 112, "应为 512 字节减去前缀开销")
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "/")

    def test_required_tokens_are_real_credential_keys(self):
        """IRC 没有 bot_token；host/nick/channels 缺一不可（channels 决定入站）。"""
        adapter, _ = make_irc()
        self.assertEqual(adapter.required_tokens, ("host", "nick", "channels"))
        self.assertNotIn("bot_token", adapter.required_tokens)
        self.assertEqual(adapter_class("irc").required_tokens, ("host", "nick", "channels"))

    def test_registered_in_registry(self):
        self.assertIn("irc", registered_names())
        adapter = build("irc", {"host": "h", "nick": "n"}, RecordingHooks())
        self.assertIsInstance(adapter, IRCAdapter)

    def test_appears_in_status_rows(self):
        """--status 的数据源必须自动列出 IRC（证明无需改核心文件）。"""
        from opencode_bridge import __main__ as cli
        from opencode_bridge.config import Config

        cfg = Config(adapters={"irc": {"host": "h", "nick": "n", "channels": CHAN}})
        rows = {r[0]: r for r in cli._channel_config_rows(cfg)}
        self.assertIn("irc", rows)
        self.assertEqual(rows["irc"][1], "IRC")
        self.assertTrue(rows["irc"][2], "三项齐备应算 configured")
        self.assertTrue(rows["irc"][3], "三项齐备 + supports_inbound 应算 inbound_ready")

        partial = {r[0]: r for r in cli._channel_config_rows(Config(adapters={"irc": {"host": "h"}}))}
        self.assertFalse(partial["irc"][2], "缺 nick/channels 不该算已配置")

    def test_config_parsing(self):
        adapter, _ = make_irc({"channels": "#a, #b  #c"})
        self.assertEqual(adapter.channels, ["#a", "#b", "#c"])
        adapter, _ = make_irc({"channels": ["#x", " #y "]})
        self.assertEqual(adapter.channels, ["#x", "#y"])
        adapter, _ = make_irc({"channels": None})
        self.assertEqual(adapter.channels, [])
        adapter, _ = make_irc({"realname": ""})
        self.assertEqual(adapter.realname, NICK, "realname 缺省回落到 nick")

    def test_port_defaults(self):
        adapter, _ = make_irc({"host": "h", "nick": "n"})
        self.assertEqual(adapter.port, 6667)
        adapter, _ = make_irc({"host": "h", "nick": "n", "use_tls": True})
        self.assertEqual(adapter.port, 6697, "TLS 默认端口 6697")
        adapter, _ = make_irc({"host": "h", "nick": "n", "port": 7001})
        self.assertEqual(adapter.port, 7001)
        adapter, _ = make_irc({"host": "h", "nick": "n", "port": "not-a-port"})
        self.assertEqual(adapter.port, 6667, "非法端口回落到默认")

    def test_conversation_id_conversion(self):
        self.assertEqual(IRCAdapter._conversation_id(CHAN), "irc:#chan")
        self.assertEqual(IRCAdapter._conversation_id("alice"), "irc:alice")
        self.assertEqual(IRCAdapter._target("irc:#chan"), "#chan")
        self.assertEqual(IRCAdapter._target("#chan"), "#chan")
        self.assertEqual(IRCAdapter._target("alice"), "alice")
        self.assertIsNone(IRCAdapter._target("irc:"))

    def test_normalize_target_adds_hash_for_channels_only(self):
        adapter, _ = make_irc()
        self.assertEqual(adapter._normalize_target("chan"), "#chan")
        self.assertEqual(adapter._normalize_target("#chan"), "#chan")
        self.assertEqual(adapter._normalize_target("&local"), "&local")
        self.assertEqual(adapter._normalize_target(""), "")
        # 未见过的裸名字按频道处理；见过之后按昵称处理（私聊回信）
        self.assertEqual(adapter._normalize_target("alice"), "#alice")
        adapter._known_nicks.add("alice")
        self.assertEqual(adapter._normalize_target("alice"), "alice")
        self.assertEqual(adapter._normalize_target("ALICE"), "ALICE")


# ----------------------------------------------------------------------
# 纯函数：控制码 / 字节截断 / 行解析
# ----------------------------------------------------------------------
class TestIRCPureHelpers(unittest.TestCase):
    def test_strip_control_codes(self):
        raw = (
            "\x02粗体\x02 \x0304红\x03 \x0freset\x0f "
            "\x1d斜体\x1d\x1e删除\x1e\x1f下划线\x1f\x11mono\x11 "
            "\x03\x02\x0304,08彩\x0f \x07beep "
            "零宽\u200d\u200b\u200c\ufeff\u2060软连\u00ad "
            "RTL\u202eoverride\u202c 换行\n第二行\ttab"
        )
        out = strip_control_codes(raw)
        for bad in ("\x02", "\x03", "\x0f", "\x1d", "\x1e", "\x1f", "\x11", "\x07",
                    "\u200d", "\u200b", "\u200c", "\ufeff", "\u2060", "\u00ad",
                    "\u202e", "\u202c", "\n", "\t"):
            self.assertNotIn(bad, out, f"{bad!r} 未被清理：{out!r}")
        self.assertIn("粗体", out)
        self.assertIn("红", out)
        self.assertIn("零宽", out)
        self.assertIn("第二行", out)
        self.assertEqual(strip_control_codes(""), "")
        self.assertEqual(strip_control_codes("  hi  "), "hi")

    def test_hex_colour_code_stripped(self):
        self.assertEqual(strip_control_codes("\x04FF0000red\x0f"), "red")

    def test_fit_utf8_never_splits_a_character(self):
        text = "字" * 300
        out = _fit_utf8(text, 100)
        self.assertLessEqual(len(out.encode("utf-8")), 100)
        self.assertGreater(len(out), 0)
        # 关键：结果必须是合法 UTF-8，且不含替换字符（切出乱码的信号）
        self.assertEqual(out.encode("utf-8").decode("utf-8"), out)
        self.assertNotIn("\ufffd", out)
        self.assertEqual(_fit_utf8("abc", 0), "")
        self.assertEqual(_fit_utf8("abc", -1), "")
        self.assertEqual(_fit_utf8("abc", 10), "abc")

    def test_byte_safe_split_respects_budget_and_keeps_content(self):
        text = "字" * 500
        pieces = _byte_safe_split(text, 300)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece.encode("utf-8")), 300)
            self.assertNotIn("\ufffd", piece)
        self.assertEqual("".join(pieces), text, "按字节切分不能丢字符")
        self.assertEqual(_byte_safe_split("hi", 10), ["hi"])
        self.assertEqual(_byte_safe_split("", 10), [])
        self.assertEqual(_byte_safe_split("hi", 0), [])

    def test_body_budget_accounts_for_target(self):
        adapter, _ = make_irc()
        budget = adapter._body_budget(CHAN)
        # 开销 = "PRIVMSG " + 目标 + " :" 两字节 + CRLF
        self.assertEqual(budget, LINE_LIMIT - len("PRIVMSG ") - len(CHAN) - 2 - 2)
        # 整行拼起来必须 ≤ 512
        line = f"PRIVMSG {CHAN} :{'x' * budget}\r\n"
        self.assertLessEqual(len(line.encode("utf-8")), LINE_LIMIT)
        # 再多一个字节就超限（这正是曾经差 1 字节的 bug）
        over = f"PRIVMSG {CHAN} :{'x' * (budget + 1)}\r\n"
        self.assertGreater(len(over.encode("utf-8")), LINE_LIMIT)

    def test_parse_line(self):
        self.assertEqual(
            _parse_line(":alice!a@h PRIVMSG #chan :hello world"),
            ("alice!a@h", "PRIVMSG", ["#chan", "hello world"]),
        )
        self.assertEqual(_parse_line("PING :tok"), ("", "PING", ["tok"]))
        self.assertEqual(_parse_line(":srv 001 nick :Welcome"),
                         ("srv", "001", ["nick", "Welcome"]))
        self.assertEqual(_parse_line("PONG a : b"), ("", "PONG", ["a", " b"]),
                         "末位参数可含冒号与空格")
        self.assertEqual(_parse_line("PONG :a : b"), ("", "PONG", ["a : b"]),
                         "第一个 ' :' 之后全部属于末位参数")
        self.assertEqual(_parse_line(""), ("", "", []))
        self.assertEqual(_parse_line(":only-prefix"), ("only-prefix", "", []))
        self.assertEqual(_prefix_nick("alice!user@host"), "alice")
        self.assertEqual(_prefix_nick("alice"), "alice")
        self.assertEqual(_prefix_nick(""), "")


# ----------------------------------------------------------------------
# 提及触发
# ----------------------------------------------------------------------
class TestMentionRules(unittest.TestCase):
    def _m(self, text: str) -> bool:
        adapter, _ = make_irc({"nick": "bot"})
        return adapter._mention(text) is not None

    def test_common_mention_forms_trigger(self):
        for text in (
            "bot: hello",
            "@bot hello",
            "bot, hello",
            "bot hello",
            "hello bot",
            "hey bot, how are you",
            "bot!user@host hi",
            "BOT: shouty",
            "@BOT, shouty",
            "[bot]: bracketed nick",
            "bot's question",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._m(text), f"{text!r} 应识别为提及")

    def test_non_mentions_do_not_trigger(self):
        for text in (
            "hello everyone",
            "bots are cool",
            "robot",
            "bot_1 hi",
            "abot hi",
            "botbot hi",
            "",
            "nickname: hi",
        ):
            with self.subTest(text=text):
                self.assertFalse(self._m(text), f"{text!r} 不该算提及")

    def test_empty_nick_never_matches(self):
        adapter, _ = make_irc({"nick": ""})
        self.assertIsNone(adapter._mention("bot: hi"))
        self.assertIsNone(adapter._mention(""))

    def test_strip_mention_removes_prefix_and_separator(self):
        adapter, _ = make_irc({"nick": "bot"})
        cases = {
            "bot: hello there": "hello there",
            "@bot hello there": "hello there",
            "bot, hello there": "hello there",
            "bot hello there": "hello there",
            "hey bot, hello there": "hello there",
            "bot!user@host hello": "hello",
            "@bot: hello": "hello",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                match = adapter._mention(raw)
                self.assertIsNotNone(match, raw)
                self.assertEqual(adapter._strip_mention(raw, match), want)


# ----------------------------------------------------------------------
# 注册时序 / PING/PONG / 入站
# ----------------------------------------------------------------------
class TestIRCRegistration(IRCTestCase):
    def test_full_registration_sequence(self):
        server = self.make_server()
        adapter, _ = make_irc({"channels": ["#a", "#b"]})
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)

        lines = list(server.lines)
        nick_line = next(x for x in lines if x.startswith("NICK "))
        self.assertEqual(nick_line, f"NICK {NICK}")
        user_line = next(x for x in lines if x.startswith("USER "))
        self.assertEqual(user_line, f"USER {NICK} 0 * :{NICK}")
        join_line = next(x for x in lines if x.startswith("JOIN "))
        self.assertEqual(join_line, "JOIN #a,#b")
        # JOIN 只在 001 之后发（未注册就 JOIN 会被服务器拒绝）
        self.assertLess(lines.index(nick_line), lines.index(join_line))
        self.assertTrue(adapter.running)

    def test_server_password_is_sent_first(self):
        server = self.make_server()
        adapter, _ = make_irc({"server_password": "s3cret"})
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        self.assertEqual(server.lines[0], "PASS s3cret")

    def test_sasl_plain_exchange(self):
        server = self.make_server(auto_welcome=False)
        server.wait_connections(1, timeout=0.2)
        adapter, _ = make_irc({"bot_password": "hunter2"})
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)

        # CAP LS → 宣告 sasl → AUTHENTICATE PLAIN → + → 凭据 → 903 → CAP END → NICK
        def on_cap_ls(line: str) -> bool:
            if line.startswith("CAP LS"):
                server.send(":irc.test CAP * LS :sasl")
            return line.startswith("NICK ")

        def on_auth_plain(line: str) -> bool:
            if line == "AUTHENTICATE PLAIN":
                server.send("AUTHENTICATE +")
            return line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE PLAIN"

        self.assertIsNotNone(server.wait_for(on_cap_ls), "未进入注册")
        self.assertIsNotNone(server.wait_for(on_auth_plain), "未发送 SASL 凭据")
        payload = server.wait_for(
            lambda x: x.startswith("AUTHENTICATE ") and x != "AUTHENTICATE PLAIN"
        ).split(" ", 1)[1]
        self.assertEqual(
            base64.b64decode(payload), b"\0bot\0hunter2", "SASL PLAIN 应为 \\0user\\0pass"
        )
        server.send(":irc.test 903 bot :SASL authentication successful")
        self.assertIsNotNone(
            server.wait_for(lambda x: x == "CAP END"), "SASL 结束后应 CAP END"
        )
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("USER ")))

    def test_sasl_rejected_still_registers(self):
        server = self.make_server(auto_welcome=False)
        adapter, _ = make_irc({"bot_password": "bad"})
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)

        def feed(line: str) -> bool:
            if line.startswith("CAP LS"):
                server.send(":irc.test CAP * LS :sasl")
            elif line == "AUTHENTICATE PLAIN":
                server.send("AUTHENTICATE +")
            elif line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE PLAIN":
                server.send(":irc.test 904 bot :SASL authentication failed")
            return line.startswith("NICK ")

        self.assertIsNotNone(server.wait_for(feed), "失败后仍应继续注册")

    def test_no_cap_negotiation_without_bot_password(self):
        server = self.make_server()
        adapter, _ = make_irc()
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        self.assertEqual([x for x in server.lines if x.startswith("CAP")], [])

    def test_registration_timeout_triggers_reconnect(self):
        """收不到 001 必须重连，而不是静默卡在"已连上"。"""
        server = self.make_server(auto_welcome=False)
        adapter, _ = make_irc()
        self.connect_adapter(adapter, server)
        adapter.register_timeout = 0.2
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(server.wait_connections(2, timeout=5), "超时后应重连")

    def test_join_absent_when_no_channels(self):
        server = self.make_server()
        adapter, _ = make_irc({"channels": []})
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        deadline = time.time() + 3
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(adapter._registered)
        self.assertEqual([x for x in server.lines if x.startswith("JOIN")], [])
        self.assertEqual([x for x in server.lines if x.startswith("CAP")], [],
                         "SASL/CAP 协商也不该发")


class TestIRCPingPong(IRCTestCase):
    def test_ping_is_answered_with_same_token(self):
        server = self.make_server()
        adapter, _ = make_irc()
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        before = len(server.lines)
        server.send("PING :abc123")
        self.assertEqual(
            server.wait_for(lambda x: x.startswith("PONG")), "PONG :abc123"
        )
        self.assertGreater(len(server.lines), before)

    def test_ping_with_no_token_uses_host(self):
        adapter, _ = make_irc()
        server = self.make_server()
        adapter._write_line = lambda sock, line: sent.append(line) or True
        sent: list[str] = []
        adapter._handle_line(None, "PING")
        self.assertEqual(sent, ["PONG :127.0.0.1"])

    def test_keepalive_ping_is_sent_by_timer(self):
        server = self.make_server()
        adapter, _ = make_irc()
        adapter.ping_interval = 0.05
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        line = server.wait_for(lambda x: x.startswith("PING :opencode-"), timeout=3)
        self.assertIsNotNone(line, "服务端不 ping 时我们自己要保活")
        self.assertLessEqual(len(f"{line}\r\n".encode("utf-8")), LINE_LIMIT)


class TestIRCInbound(IRCTestCase):
    def _run_one(self, raw: str, config: dict | None = None, hooks=None, expect=True):
        """发一行给客户端，等 Inbound（或确认没有 Inbound）。"""
        server = self.make_server()
        adapter, rec = make_irc(config, hooks)
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        before = len(rec.inbounds)
        server.send(raw)
        if expect:
            deadline = time.time() + 3
            while len(rec.inbounds) == before and time.time() < deadline:
                time.sleep(0.01)
        else:
            # 反向断言只需要"等一小会儿"，不必等满超时（否则每个反向用例白等 3s）
            time.sleep(0.3)
        return adapter, rec

    def test_private_message_becomes_inbound(self):
        _, rec = self._run_one(f":alice!a@h PRIVMSG {NICK} :hello bot")
        self.assertEqual(len(rec.inbounds), 1)
        ib = rec.inbounds[0]
        self.assertIsInstance(ib, Inbound)
        self.assertEqual(ib.conversation_id, f"irc:{NICK}")
        self.assertEqual(ib.text, "hello bot", "私聊不应剥提及")
        self.assertEqual(ib.kind, "text")
        self.assertEqual(ib.user_id, "alice")
        self.assertEqual(ib.platform, "irc")
        self.assertIn("PRIVMSG", ib.raw)

    def test_channel_mention_becomes_inbound(self):
        _, rec = self._run_one(f":alice!a@h PRIVMSG {CHAN} :{NICK}: what is 2+2?")
        self.assertEqual(len(rec.inbounds), 1)
        ib = rec.inbounds[0]
        self.assertEqual(ib.conversation_id, f"irc:{CHAN}")
        self.assertEqual(ib.text, "what is 2+2?", "提及应被剥掉")
        self.assertEqual(ib.user_id, "alice")

    def test_channel_message_without_mention_ignored(self):
        for raw in (
            f":alice!a@h PRIVMSG {CHAN} :hello everyone",
            f":alice!a@h PRIVMSG {CHAN} :bots are cool",
            f":alice!a@h PRIVMSG {CHAN} :robot",
            f":bob!b@h PRIVMSG {CHAN} :hi {NICK}bot",
        ):
            with self.subTest(raw=raw):
                _, rec = self._run_one(raw, expect=False)
                self.assertEqual(rec.inbounds, [], f"{raw!r} 不该产生 Inbound")

    def test_control_codes_are_cleaned(self):
        raw = f":alice!a@h PRIVMSG {CHAN} :{NICK}: \x0304red\x03 \x02bold\x02 \u200dzw"
        _, rec = self._run_one(raw)
        self.assertEqual(len(rec.inbounds), 1)
        text = rec.inbounds[0].text
        self.assertEqual(text, "red bold zw")
        for bad in ("\x03", "\x02", "\u200d"):
            self.assertNotIn(bad, text)

    def test_own_message_is_skipped(self):
        for prefix in (NICK, NICK.upper()):
            with self.subTest(prefix=prefix):
                _, rec = self._run_one(f":{prefix}!me@h PRIVMSG {CHAN} :{NICK}: echo",
                                       expect=False)
                self.assertEqual(rec.inbounds, [], "自己发的消息必须跳过以免回环")

    def test_authorization_gate_runs_before_inbound(self):
        hooks = RecordingHooks()
        _, rec = self._run_one(
            f":alice!a@h PRIVMSG {CHAN} :{NICK}: secret",
            {"allowed_chat_ids": ["#allowed"]},
            hooks,
            expect=False,
        )
        self.assertEqual(rec.inbounds, [])
        _, rec = self._run_one(
            f":alice!a@h PRIVMSG #allowed :{NICK}: ok",
            {"allowed_chat_ids": ["#allowed"]},
            hooks,
        )
        self.assertEqual(len(rec.inbounds), 1)

    def test_malformed_privmsg_ignored(self):
        _, rec = self._run_one(":alice PRIVMSG", expect=False)
        self.assertEqual(rec.inbounds, [])
        _, rec = self._run_one(f": PRIVMSG {CHAN} :{NICK}: no nick", expect=False)
        self.assertEqual(rec.inbounds, [])
        # 只有控制码 → 清理后为空 → 不投递
        _, rec = self._run_one(f":alice!a@h PRIVMSG {CHAN} :{NICK}: \x03\x0f", expect=False)
        self.assertEqual(rec.inbounds, [])

    def test_messages_arriving_in_same_segment_as_001(self):
        """001 与第一条 PRIVMSG 在同一个 TCP 段里时也不能丢。"""
        server = self.make_server(auto_welcome=False)
        adapter, rec = make_irc()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(
            server.wait_for(lambda x: x.startswith("USER ")), "未进入注册"
        )
        server.send_raw(
            (f":irc.test 001 {NICK} :Welcome\r\n"
             f":alice!a@h PRIVMSG {CHAN} :{NICK}: first\r\n").encode("utf-8")
        )
        deadline = time.time() + 3
        while not rec.inbounds and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1, "与 001 同段的 PRIVMSG 被吞了")
        self.assertEqual(rec.inbounds[0].text, "first")

    def test_unknown_commands_are_ignored_without_raising(self):
        _, rec = self._run_one(":irc.test 332 bot #chan :topic", expect=False)
        self.assertEqual(rec.inbounds, [])
        _, rec = self._run_one("NOTICE #chan :server notice", expect=False)
        self.assertEqual(rec.inbounds, [])

    def test_inbound_hook_exception_is_contained(self):
        class Exploding(RecordingHooks):
            def on_inbound(self, inbound: Inbound) -> None:  # type: ignore[override]
                raise RuntimeError("hook down")

        server = self.make_server()
        adapter, _ = make_irc(hooks=Exploding())
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        server.send(f":alice!a@h PRIVMSG {CHAN} :{NICK}: boom")
        self.assertIsNotNone(
            server.wait_for(lambda x: x.startswith("PING"), timeout=0.2) or "",
            "hook 抛异常后读循环应继续存活",
        ) if adapter.ping_interval < 1 else None
        server.send(f":alice!a@h PRIVMSG {CHAN} :{NICK}: again")
        time.sleep(0.2)
        self.assertTrue(adapter.running, "hook 异常不得让线程退出")


# ----------------------------------------------------------------------
# 出站
# ----------------------------------------------------------------------
class TestIRCSend(IRCTestCase):
    def _started(self, config: dict | None = None, hooks=None):
        server = self.make_server()
        adapter, rec = make_irc(config, hooks)
        self.connect_adapter(adapter, server)
        self.start_and_wait_registered(adapter, server)
        return adapter, rec, server

    def test_send_line_shape(self):
        adapter, _, server = self._started()
        before = len(server.lines)
        handle = adapter.send(Outbound("irc:#chan", "hello irc"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "irc")
        self.assertEqual(handle.conversation_id, "irc:#chan")
        self.assertEqual(handle.message_id, "1", "IRC 无消息 id，用本地序号占位")
        self.assertEqual(server.wait_privmsg(1), ["PRIVMSG #chan :hello irc"])
        self.assertGreater(len(server.lines), before)

    def test_channel_target_gets_hash(self):
        adapter, _, server = self._started()
        adapter.send(Outbound("irc:chan", "no hash given"))
        self.assertEqual(server.wait_privmsg(1), ["PRIVMSG #chan :no hash given"])

    def test_private_target_keeps_nick(self):
        """回复私聊不能被补成 ``#alice``（那就变成往频道发了）。"""
        adapter, rec, server = self._started()
        server.send(f":alice!a@h PRIVMSG {NICK} :psst")
        deadline = time.time() + 3
        while not rec.inbounds and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1, "未收到 alice 的私聊")
        adapter.send(Outbound("irc:alice", "back at you"))
        lines = server.wait_privmsg(1)
        self.assertEqual(lines, ["PRIVMSG alice :back at you"])
        self.assertEqual(server.privmsg_lines("#alice"), [], "不能变成 #alice")

    def test_every_line_respects_512_byte_limit(self):
        adapter, _, server = self._started()
        # 中文 3 字节/字：400 字符 = 1200 字节 > 512，必须按字节再切
        text = "字" * 900
        adapter.send(Outbound("irc:#chan", text))
        lines = server.wait_privmsg_quiet()
        self.assertGreater(len(lines), 1, "超 512 字节必须切成多行")
        bodies = []
        for line in lines:
            encoded = line.encode("utf-8")
            self.assertLessEqual(len(encoded) + 2, LINE_LIMIT, f"行超限：{len(encoded) + 2}")
            # 必须是合法 UTF-8（没有替换字符 = 没切出乱码）
            self.assertEqual(encoded.decode("utf-8"), line)
            self.assertNotIn("\ufffd", line)
            body = line.split(" :", 1)[1]
            self.assertLessEqual(len(body), adapter.max_message_length, "每片在上限内")
            bodies.append(body)
        self.assertEqual("".join(bodies), text, "切分不能丢内容")

    def test_write_line_truncates_oversized_line(self):
        """绕过 send() 直接写超长行时按字符边界截到 512 字节内。"""
        adapter, _, server = self._started()
        sock = adapter._sock
        adapter._write_line(sock, "PRIVMSG #chan :" + "字" * 900)
        lines = server.wait_privmsg(1)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0].encode("utf-8")) + 2, LINE_LIMIT)
        self.assertNotIn("\ufffd", lines[0])
        self.assertTrue(lines[0].startswith("PRIVMSG #chan :"))
        self.assertEqual(lines[0].split(" :", 1)[1], "字" * 165,
                         "截断应正好落在字符边界上（512-17 字节预算）")

    def test_write_line_strips_embedded_newlines(self):
        adapter, _, server = self._started()
        adapter._write_line(adapter._sock, "PRIVMSG #chan :a\r\nJOIN #evil")
        lines = server.wait_privmsg(1)
        self.assertEqual(lines, ["PRIVMSG #chan :a JOIN #evil"])
        self.assertEqual([x for x in server.lines if x.startswith("JOIN #evil")], [],
                         "注入的换行绝不能变成一条真命令")

    def test_split_returns_multiple_privmsg(self):
        adapter, _, server = self._started()
        text = ("x" * 200 + "\n") * 30  # 6030 字符 > 400
        handle = adapter.send(Outbound("irc:#chan", text))
        lines = server.wait_privmsg_quiet()
        self.assertGreater(len(lines), 1)
        # PRIVMSG 正文里不能有换行（会破坏行协议），所以 \n 在发送时折成空格
        self.assertEqual(
            "".join(x.split(" :", 1)[1] for x in lines),
            text.replace("\n", " "),
        )
        self.assertEqual(handle.message_id, str(len(lines)), "句柄指向最后一行")

    def test_send_not_connected_is_structured_failure(self):
        adapter, _ = make_irc()
        result = adapter.send_result(Outbound("irc:#chan", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertIn("not connected", result.error_detail)

    def test_send_write_failure_is_structured_failure(self):
        adapter, _, server = self._started()
        server.drop()
        time.sleep(0.2)
        if adapter._sock is not None:  # 读循环还没察觉断开
            adapter._sock.close()
        result = adapter.send_result(Outbound("irc:#chan", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertNotEqual(result.error_kind, SendError.UNKNOWN)

    def test_send_bad_arguments_are_bad_format(self):
        adapter, _, _ = self._started()
        for out, why in (
            (Outbound("irc:", "hi"), "空目标"),
            (Outbound("irc:#chan", ""), "空正文"),
        ):
            with self.subTest(why=why):
                result = adapter.send_result(out)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_edit_always_returns_false(self):
        """IRC 没有编辑消息；core.py 会退化成发一条新消息。"""
        adapter, _, server = self._started()
        handle = MsgHandle("irc:#chan", "1", "irc")
        self.assertIs(adapter.edit(handle, Outbound("irc:#chan", "edited")), False)
        self.assertEqual(server.privmsg_lines("#chan"), [], "edit 不该发出任何东西")
        self.assertIs(adapter.edit(handle, Outbound("irc:#chan", "")), False)

    def test_answer_is_noop(self):
        adapter, _ = make_irc()
        self.assertIsNone(adapter.answer("q1"))
        self.assertIsNone(adapter.answer("q1", "text"))

    def test_send_success_result_is_ok(self):
        adapter, _, _ = self._started()
        result = adapter.send_result(Outbound("irc:#chan", "hi"))
        self.assertTrue(result.ok)
        self.assertFalse(result.partial)
        self.assertIsNotNone(result.handle)


# ----------------------------------------------------------------------
# 生命周期 / 重连
# ----------------------------------------------------------------------
class TestIRCLifecycle(IRCTestCase):
    def test_missing_host_warns_and_does_not_start(self):
        adapter, _ = make_irc({"host": ""})
        with self.assertLogs("opencode_bridge.adapters.irc", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("host" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_missing_nick_warns_and_does_not_start(self):
        adapter, _ = make_irc({"nick": ""})
        with self.assertLogs("opencode_bridge.adapters.irc", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("nick" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_no_channels_warns_but_still_starts(self):
        server = self.make_server()
        adapter, _ = make_irc({"channels": []})
        self.connect_adapter(adapter, server)
        with self.assertLogs("opencode_bridge.adapters.irc", level="WARNING") as cm:
            adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(any("channels" in line for line in cm.output))
        self.assertTrue(adapter.running, "缺频道只影响入站，不该阻止连接")

    def test_start_stop_roundtrip(self):
        server = self.make_server()
        adapter, _ = make_irc()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.assertTrue(adapter.running)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_wakes_blocked_recv_quickly(self):
        """stop() 先 shutdown socket，阻塞中的 recv 立刻返回（不能挂满超时）。"""
        server = self.make_server()
        adapter, _ = make_irc()
        adapter.socket_timeout = 30.0  # 让"没关 socket"必然挂满 30s
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        began = time.monotonic()
        adapter.stop()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 2.0, f"stop 耗时 {elapsed:.2f}s，说明没唤醒 recv")
        self.assertFalse(adapter.running)

    def test_reconnect_after_disconnect(self):
        server = self.make_server()
        adapter, rec = make_irc()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        server.drop()
        self.assertTrue(server.wait_connections(2, timeout=5), "断线后应自动重连")
        # 重连后仍能正常收发（而不是"连上但已死"）
        deadline = time.time() + 5
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(adapter._registered, "重连后应重新完成注册")
        server.send(f":alice!a@h PRIVMSG {CHAN} :{NICK}: after reconnect")
        deadline = time.time() + 3
        while not rec.inbounds and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1)
        self.assertEqual(rec.inbounds[0].text, "after reconnect")

    def test_connect_failure_retries_with_backoff(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()  # 端口已释放 → 连接必被拒
        adapter, _ = make_irc()
        adapter.host, adapter.port = "127.0.0.1", dead_port
        adapter.reconnect_delay = 0.05
        adapter.max_reconnect_delay = 0.1
        adapter.start()
        self.addCleanup(adapter.stop)
        time.sleep(0.4)
        self.assertTrue(adapter.running, "连不上也不能让线程退出")
        self.assertIsNone(adapter._sock)

    def test_stop_before_start_is_harmless(self):
        adapter, _ = make_irc()
        adapter.stop()
        self.assertFalse(adapter.running)


class TestIRCTLS(unittest.TestCase):
    """真 TLS 需要证书，这里只验证"确实走了 TLS 包装"这个接缝。"""

    class FakeSocket:
        """只实现 ``_open_socket`` 用到的那几个方法。"""

        def __init__(self) -> None:
            self.timeout: float | None = None
            self.closed = False

        def settimeout(self, value) -> None:
            self.timeout = value

        def close(self) -> None:
            self.closed = True

    def _patched_connect(self, fake):
        import opencode_bridge.adapters.irc as irc_mod

        old = irc_mod.socket.create_connection
        irc_mod.socket.create_connection = fake
        self.addCleanup(setattr, irc_mod.socket, "create_connection", old)

    def test_use_tls_wraps_socket(self):
        adapter, _ = make_irc({"use_tls": True, "host": "irc.example.org"})
        self.assertEqual(adapter.port, 6697)
        raw = self.FakeSocket()
        wrapped = self.FakeSocket()
        seen: list[socket.socket] = []

        def fake_wrap(sock):
            seen.append(sock)
            return wrapped

        adapter._tls_wrap = fake_wrap
        self._patched_connect(lambda addr, timeout=None: raw)
        self.assertIs(adapter._open_socket(), wrapped)
        self.assertEqual(seen, [raw], "use_tls 时必须经过 TLS 包装")
        self.assertEqual(wrapped.timeout, adapter.socket_timeout)

    def test_plain_connection_is_not_wrapped(self):
        adapter, _ = make_irc({"host": "irc.example.org"})
        raw = self.FakeSocket()
        adapter._tls_wrap = lambda sock: (_ for _ in ()).throw(
            AssertionError("明文连接不应走 TLS 包装")
        )
        self._patched_connect(lambda addr, timeout=None: raw)
        self.assertIs(adapter._open_socket(), raw)

    def test_default_tls_wrap_is_installed(self):
        adapter, _ = make_irc({"use_tls": True})
        self.assertTrue(callable(adapter._tls_wrap))
        self.assertEqual(adapter._tls_wrap.__name__, "_default_tls_wrap")

    def test_failed_tls_wrap_closes_socket(self):
        adapter, _ = make_irc({"use_tls": True})
        raw = self.FakeSocket()

        def boom(sock):
            raise OSError("tls handshake failed")

        adapter._tls_wrap = boom
        self._patched_connect(lambda addr, timeout=None: raw)
        with self.assertRaises(OSError):
            adapter._open_socket()
        self.assertTrue(raw.closed, "TLS 失败必须把底层 socket 关掉，不能泄漏 fd")


if __name__ == "__main__":
    unittest.main()
