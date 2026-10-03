"""T3.4 Twitch adapter tests（**不 mock socket**：本机回环真 WebSocket 服务器）。

照 ``tests/test_ws.py`` 的 ``Server`` 写法起一个只说 RFC 6455 字节的真服务器线程
（绑 ``127.0.0.1:0``），握手后往连接里写**未掩码**的文本帧。适配器用真实的
``opencode_bridge.ws`` 客户端连上去（只把 ``endpoint`` 覆盖成 ``ws://127.0.0.1:port/``），
因此能真正验证"一帧多行 / 一行被拆成多帧 / TLS 之外的全部协议行为"。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import unittest
import urllib.request

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.twitch as twitch_mod
from opencode_bridge.adapters import adapter_class, build, registered_names
from opencode_bridge.adapters.twitch import (
    CAP_COMMANDS,
    CAP_MEMBERSHIP,
    CAP_TAGS,
    LINE_BUDGET,
    MESSAGE_LIMIT,
    TMI_HOST,
    TwitchAdapter,
    _parse_tags,
    _parse_twitch_line,
    _unescape_tag_value,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.transport import WebSocketTransport

NICK = "opencodebot"
CHAN = "#foo"
TOKEN = "oauth:abcdefgh"

# 一条真实的、字段齐全的 Twitch PRIVMSG（display-name 用 IRCv3 的 \s 转义空格）
PRIVMSG_FULL = (
    r"@badge-info=;color=#FF0000;display-name=Someone;emotes=;id=abc-123-def;"
    r"mod=0;room-id=12345;subscriber=0;turbo=0;user-id=67890;user-type= "
    r":someone!someone@someone.tmi.twitch.tv PRIVMSG #foo :Hello world"
)
PRIVMSG_SPACED_NAME = (
    r"@display-name=Some\sOne;id=m2;user-id=99;room-id=12345 "
    r":some!some@some.tmi.twitch.tv PRIVMSG #foo :hi"
)


# ----------------------------------------------------------------------
# 本机回环的假 WebSocket 服务器（真 socket + 真握手）
# ----------------------------------------------------------------------
class WsIrcServer(threading.Thread):
    """在 ``127.0.0.1:0`` 上完成 RFC 6455 握手，然后把文本帧当 IRC 行发出去。

    * :meth:`send_frame` 写一个**未掩码**的文本帧（服务端按 RFC 不加掩码），
      内容可以是任意字符串 —— 因此能构造"一帧多行"和"一行拆多帧"；
    * 收到客户端的文本帧后按行拆分记录（客户端帧可能把多行塞在一起）；
    * :meth:`drop` 裸断连接（不发 close 帧），用来验证重连。
    """

    def __init__(self, *, auto_register: bool = True, nick: str = NICK) -> None:
        super().__init__(daemon=True)
        self.auto_register = auto_register
        self.nick = nick
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.lines: list[str] = []          # 收到的 IRC 行
        self.sent_lines: list[str] = []     # **发出**的 IRC 行（屏障用）
        self.frames: list[str] = []         # 收到的原始文本帧
        self.connections = 0
        self.error: BaseException | None = None
        self._conns: list[socket.socket] = []
        self._cv = threading.Condition()
        self._closing = False
        self._begin = False

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def start_server(self) -> None:
        if not self._begin:
            self._begin = True
            self.start()

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
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    # -- 服务端协议 ----------------------------------------------------
    def _serve(self, conn: socket.socket) -> None:
        try:
            handshake = self._read_head(conn)
            if handshake is None:
                return
            conn.sendall(self._handshake_response(handshake))
            self._read_frames(conn)
        except OSError:
            pass
        finally:
            with self._cv:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _read_head(conn: socket.socket, timeout: float = 5.0) -> bytes | None:
        conn.settimeout(timeout)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            try:
                chunk = conn.recv(1)
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    @staticmethod
    def _handshake_response(head: bytes) -> bytes:
        import base64
        import hashlib

        key = ""
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        return (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("latin-1")

    def _read_frames(self, conn: socket.socket) -> None:
        """读客户端帧（必须解掩码），按行拆开记录。"""
        conn.settimeout(0.2)
        while not self._closing:
            try:
                head = self._recv_exact(conn, 2)
            except socket.timeout:
                continue
            except OSError:
                return
            if head is None:
                return
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            size = b1 & 0x7F
            if size == 126:
                ext = self._recv_exact(conn, 2)
                if ext is None:
                    return
                size = int.from_bytes(ext, "big")
            elif size == 127:
                ext = self._recv_exact(conn, 8)
                if ext is None:
                    return
                size = int.from_bytes(ext, "big")
            mask = self._recv_exact(conn, 4) if masked else None
            payload = self._recv_exact(conn, size) if size else b""
            if payload is None:
                return
            if mask:
                payload = bytes(a ^ mask[i % 4] for i, a in enumerate(payload))
            if opcode == 0x8:      # close
                return
            if opcode not in (0x1, 0x0):
                continue           # ping/pong 不关心
            text = payload.decode("utf-8", "replace")
            with self._cv:
                self.frames.append(text)
                for line in text.replace("\r\n", "\n").split("\n"):
                    if line:
                        self.lines.append(line)
                self._cv.notify_all()
            self._react(line_or_text=text)

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int, timeout: float = 5.0):
        conn.settimeout(timeout)
        buf = bytearray()
        while len(buf) < size:
            chunk = conn.recv(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _react(self, line_or_text: str) -> None:
        if not self.auto_register:
            return
        for line in line_or_text.replace("\r\n", "\n").split("\n"):
            head, _, _ = line.partition(" ")
            if head == "NICK":
                self.send_line(f":tmi.twitch.tv 001 {self.nick} :Welcome, GLHF!")
            elif head == "CAP" and line.startswith("CAP REQ"):
                self.send_line(f":tmi.twitch.tv CAP {self.nick} ACK :{line.split(':', 1)[-1]}")
            elif head == "JOIN":
                self.send_line(
                    f":{self.nick}!{self.nick}@{self.nick}.tmi.twitch.tv "
                    f"JOIN {line.split(' ', 1)[1]}"
                )
                self.send_line(f":tmi.twitch.tv 366 {self.nick} {line.split(' ', 1)[1]} :End")

    # -- 发 ------------------------------------------------------------
    def send_frame(self, payload: str) -> None:
        """写一个**未掩码**文本帧（内容任意：可含多行 / 半行）。

        注意记录顺序：``sendall`` **成功之后**才记进 ``sent_lines``。反过来的话，
        屏障可能在字节还没上线时就放行，用例注入的帧会抢在自动回显前面 ——
        那是构造出来的畸形输入，测不出适配器真实行为。
        """
        with self._cv:
            conns = list(self._conns)
        data = payload.encode("utf-8")
        for conn in conns:
            try:
                if len(data) < 126:
                    head = bytes((0x81, len(data)))
                elif len(data) < 65536:
                    head = bytes((0x81, 126)) + len(data).to_bytes(2, "big")
                else:
                    head = bytes((0x81, 127)) + len(data).to_bytes(8, "big")
                conn.sendall(head + data)
            except OSError:
                continue
            with self._cv:                      # sendall 成功后才算"已发出"
                self.sent_lines.extend(
                    x for x in payload.replace("\r\n", "\n").split("\n") if x
                )
                self._cv.notify_all()
            return

    def send_line(self, line: str) -> None:
        self.send_frame(f"{line}\r\n")

    # -- 断言辅助 ------------------------------------------------------
    def wait_for(self, pred, timeout: float = 5.0) -> str | None:
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

    def wait_lines(self, count: int, timeout: float = 5.0) -> list[str]:
        deadline = time.time() + timeout
        while True:
            if len(self.lines) >= count or time.time() >= deadline:
                return list(self.lines)
            time.sleep(0.01)

    def wait_frames(self, count: int, timeout: float = 5.0) -> list[str]:
        deadline = time.time() + timeout
        while True:
            if len(self.frames) >= count or time.time() >= deadline:
                return list(self.frames)
            time.sleep(0.01)

    def wait_connections(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        with self._cv:
            while self.connections < count:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.2))
            return True

    def wait_sent(self, pred, timeout: float = 5.0) -> str | None:
        """等**服务器发出**的一行满足 ``pred``（用于确定性屏障）。"""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for line in self.sent_lines:
                    if pred(line):
                        return line
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.2))

    def privmsg_lines(self, target: str | None = None) -> list[str]:
        out = []
        for line in self.lines:
            if not line.startswith("PRIVMSG "):
                continue
            if target is not None and line.split(" ")[1] != target:
                continue
            out.append(line)
        return out

    def drop(self) -> None:
        """裸断（不发 close 帧）。"""
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
        try:
            self._listener.close()
        except Exception:
            pass

    def __enter__(self) -> "WsIrcServer":
        self.start_server()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------
class RecordingHooks:
    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


def make_twitch(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {"token": TOKEN, "channel": "foo", "nick": NICK}
    if config:
        cfg.update(config)
    adapter = TwitchAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0          # no artificial sleeps in tests
    adapter.reconnect_delay = 0.05
    return adapter, adapter.hooks


class TwitchTestCase(unittest.TestCase):
    def make_server(self, **kw) -> WsIrcServer:
        server = WsIrcServer(**kw)
        self.addCleanup(server.stop)
        server.start_server()
        return server

    def connect_adapter(self, adapter, server: WsIrcServer) -> None:
        """指向回环假服务器（只覆盖 endpoint，客户端仍是真实 ws.py）。"""
        adapter.endpoint = server.url

    def start_and_registered(self, adapter, server: WsIrcServer) -> None:
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")), "未发出 JOIN")
        deadline = time.time() + 5
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(adapter._registered, "未收到 001")
        # **确定性屏障**：等服务器把 JOIN 的自动回显（含 366）也发完。否则用例注入的
        # 帧可能插进"上一条命令的回显"中间 —— 那样字节流本身就是坏的（这不是适配器
        # 的 bug，而是构造出来的畸形输入）。
        self.assertIsNotNone(
            server.wait_sent(lambda x: x.startswith(":tmi.twitch.tv 366 ")),
            "未收到 JOIN 的 366 回显",
        )

    def send_and_wait(self, server, raw: str, rec, timeout: float = 3.0):
        """发一行并等 Inbound 出现；``timeout`` 很小 = 反向断言"不该有 Inbound"。"""
        before = len(rec.inbounds)
        server.send_line(raw)
        deadline = time.time() + timeout
        while len(rec.inbounds) == before and time.time() < deadline:
            time.sleep(0.01)
        return rec.inbounds[before:]


# ----------------------------------------------------------------------
# 能力 / 凭据声明
# ----------------------------------------------------------------------
class TestTwitchCapabilities(unittest.TestCase):
    def test_capabilities_truthful(self):
        adapter, _ = make_twitch()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "twitch")
        self.assertEqual(caps["label"], "Twitch")
        self.assertEqual(caps["max_message_length"], MESSAGE_LIMIT)
        self.assertEqual(MESSAGE_LIMIT, 400, "社区上限 500，这里取 400 的保守值")
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "!", "Twitch 习惯 !command")
        self.assertEqual(adapter.typed_command_prefix, "!")

    def test_credential_declarations_are_consistent(self):
        """仓库不变量：outbound_tokens ⊆ required_tokens。"""
        adapter, _ = make_twitch()
        self.assertEqual(adapter.required_tokens, ("token", "channel"))
        self.assertEqual(adapter.outbound_tokens, ("token", "channel"))
        self.assertLessEqual(set(adapter.outbound_tokens), set(adapter.required_tokens))
        cls = adapter_class("twitch")
        self.assertEqual(cls.required_tokens, ("token", "channel"))
        self.assertEqual(cls.outbound_tokens, ("token", "channel"))

    def test_registered_in_registry(self):
        self.assertIn("twitch", registered_names())
        adapter = build("twitch", {"token": TOKEN, "channel": "foo"}, RecordingHooks())
        self.assertIsInstance(adapter, TwitchAdapter)

    def test_appears_in_status_rows(self):
        from opencode_bridge import __main__ as cli
        from opencode_bridge.config import Config

        cfg = Config(adapters={"twitch": {"token": "oauth:x", "channel": "foo"}})
        rows = {r[0]: r for r in cli._channel_config_rows(cfg)}
        self.assertIn("twitch", rows)
        self.assertEqual(rows["twitch"][1], "Twitch")
        self.assertTrue(rows["twitch"][2], "token+channel 齐备应算 configured")
        self.assertTrue(rows["twitch"][3], "supports_inbound + 齐备应算 inbound_ready")
        partial = {
            r[0]: r
            for r in cli._channel_config_rows(Config(adapters={"twitch": {"token": "x"}}))
        }
        self.assertFalse(partial["twitch"][2], "缺 channel 不该算已配置")

    def test_endpoint_is_official_wss_by_default(self):
        adapter, _ = make_twitch()
        self.assertTrue(adapter.endpoint.startswith("wss://"), "生产必须走 TLS")
        self.assertIn("irc-ws.chat.twitch.tv", adapter.endpoint)

    def test_oauth_prefix_is_not_duplicated(self):
        adapter, _ = make_twitch({"token": "oauth:abc"})
        self.assertEqual(adapter.raw_token, "abc")
        adapter, _ = make_twitch({"token": "abc"})
        self.assertEqual(adapter.raw_token, "abc")
        adapter, _ = make_twitch({"token": "OAUTH:abc"})
        self.assertEqual(adapter.raw_token, "abc")

    def test_channel_normalization(self):
        self.assertEqual(TwitchAdapter._normalize_channel("foo"), "#foo")
        self.assertEqual(TwitchAdapter._normalize_channel("#foo"), "#foo")
        self.assertEqual(TwitchAdapter._normalize_channel("  Foo  "), "#Foo")
        self.assertEqual(TwitchAdapter._normalize_channel(""), "")
        self.assertEqual(TwitchAdapter._conversation_id("#foo"), "twitch:#foo")
        self.assertEqual(TwitchAdapter._target("twitch:#foo"), "#foo")
        self.assertEqual(TwitchAdapter._target("#foo"), "#foo")
        self.assertIsNone(TwitchAdapter._target("twitch:"))


# ----------------------------------------------------------------------
# 标签 / 行解析（纯函数）
# ----------------------------------------------------------------------
class TestTwitchParsing(unittest.TestCase):
    def test_full_privmsg_tags(self):
        tags, prefix, command, params = _parse_twitch_line(PRIVMSG_FULL)
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#foo", "Hello world"])
        self.assertEqual(prefix, "someone!someone@someone.tmi.twitch.tv")
        self.assertEqual(tags["display-name"], "Someone")
        self.assertEqual(tags["user-id"], "67890")
        self.assertEqual(tags["id"], "abc-123-def")
        self.assertEqual(tags["room-id"], "12345")
        self.assertEqual(tags["mod"], "0")
        self.assertEqual(tags["subscriber"], "0")
        self.assertEqual(tags["user-type"], "")
        self.assertEqual(tags["badge-info"], "", "空值应保留为空串而不是丢键")

    def test_display_name_with_escaped_space(self):
        """``display-name=Some\\sOne`` → "Some One"，且不能被后续切分弄错。"""
        tags, _, command, params = _parse_twitch_line(PRIVMSG_SPACED_NAME)
        self.assertEqual(tags["display-name"], "Some One")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#foo", "hi"])

    def test_missing_and_empty_tags_are_safe(self):
        cases = [
            "",
            ":nick!n@h PRIVMSG #foo :hi",                 # 完全无标签
            "@ PRIVMSG #foo :hi",                          # 空标签段
            "@mod=1;sub=0 PRIVMSG #foo :hi",               # 只有部分标签
            "@user-type=mod PRIVMSG #foo :hi",             # 只剩一个
            "@mod PRIVMSG #foo :hi",                       # 有键无 =（空值）
            "@display-name=;user-id= PRIVMSG #foo :hi",    # 显式空值
            "@no-command-only-tags",                       # 只有标签没有命令
            "PING :tmi.twitch.tv",
            ":irc.test 001 bot :Welcome",
            "@badge-info=sub/12;badges=moderator/1 PRIVMSG #foo :hi",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                tags, prefix, command, params = _parse_twitch_line(raw)
                self.assertIsInstance(tags, dict)
                self.assertIsInstance(command, str)
                self.assertIsInstance(params, list)
        # 无标签时命令仍要正确识别
        _, _, command, params = _parse_twitch_line(":nick!n@h PRIVMSG #foo :hi")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#foo", "hi"])

    def test_tag_order_is_irrelevant(self):
        a = _parse_twitch_line("@user-id=1;display-name=A;id=z #p:f!a@h PRIVMSG #c :x")[0]
        b = _parse_twitch_line("@id=z;display-name=A;user-id=1 #p:f!a@h PRIVMSG #c :x")[0]
        self.assertEqual(a, b)
        self.assertEqual(a, {"user-id": "1", "display-name": "A", "id": "z"})

    def test_tag_escapes(self):
        self.assertEqual(_parse_tags(r"display-name=A\;B"), {"display-name": "A;B"})
        self.assertEqual(_parse_tags(r"a=1\;2;b=x"), {"a": "1;2", "b": "x"})
        self.assertEqual(_parse_tags(r"emotes=25:0-4,12-16/1902:6-10"),
                         {"emotes": "25:0-4,12-16/1902:6-10"})
        self.assertEqual(_parse_tags("empty="), {"empty": ""})
        self.assertEqual(_parse_tags("noequals"), {"noequals": ""})
        self.assertEqual(_parse_tags(""), {})
        self.assertEqual(_parse_tags("a=1;;b=2"), {"a": "1", "b": "2"})
        self.assertEqual(_unescape_tag_value(r"a\sb"), "a b")
        self.assertEqual(_unescape_tag_value(r"a\\b"), "a\\b")
        self.assertEqual(_unescape_tag_value(r"a\:b"), "a;b")
        self.assertEqual(_unescape_tag_value(r"a\rb"), "a\rb")
        self.assertEqual(_unescape_tag_value(r"a\nb"), "a\nb")
        self.assertEqual(_unescape_tag_value("a\\"), "a\\", "落单反斜杠按字面处理")

    def test_trailing_param_keeps_spaces_and_colons(self):
        _, _, _, params = _parse_twitch_line(":a@h PRIVMSG #c :look at this : ok")
        self.assertEqual(params, ["#c", "look at this : ok"])

    def test_prefix_only_and_empty_lines(self):
        self.assertEqual(_parse_twitch_line(":only-prefix")[1:], ("only-prefix", "", []))
        self.assertEqual(_parse_twitch_line("")[1:], ("", "", []))


# ----------------------------------------------------------------------
# 注册时序
# ----------------------------------------------------------------------
class TestTwitchRegistration(TwitchTestCase):
    def test_registration_sequence_and_content(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)

        lines = list(server.lines)
        self.assertEqual(lines[0], f"PASS oauth:{TOKEN[len('oauth:'):]}",
                         "PASS 必须补 oauth: 前缀且不能重复")
        self.assertIn(f"NICK {NICK}", lines)
        self.assertFalse(any(x.startswith(f"NICK {NICK} ") for x in lines),
                         "Twitch 只认纯用户名，不能是 'NICK user justin' 那种老写法")
        user_line = next(x for x in lines if x.startswith("USER "))
        self.assertEqual(user_line, f"USER {NICK} 0 * :{NICK}")
        cap_line = next(x for x in lines if x.startswith("CAP REQ"))
        self.assertIn(CAP_TAGS, cap_line)
        self.assertIn(CAP_COMMANDS, cap_line)
        self.assertNotIn(CAP_MEMBERSHIP, cap_line, "默认不协商 membership")
        # JOIN 必须在 001 之后
        self.assertLess(lines.index(f"NICK {NICK}"), lines.index("JOIN #foo"))

    def test_membership_cap_is_requested_when_enabled(self):
        server = self.make_server()
        adapter, _ = make_twitch({"membership": True})
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        cap_line = next(x for x in server.lines if x.startswith("CAP REQ"))
        self.assertIn(CAP_MEMBERSHIP, cap_line)
        deadline = time.time() + 3
        while adapter.room_user_count() < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(adapter.room_user_count(), 1, "JOIN 后成员计数 +1")

    def test_ping_is_answered_with_pong(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        server.send_line(f"PING :{TMI_HOST}")
        self.assertEqual(server.wait_for(lambda x: x.startswith("PONG")), f"PONG :{TMI_HOST}")

    def test_cap_ack_is_harmless(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        server.send_line(f":tmi.twitch.tv CAP {NICK} ACK :{CAP_TAGS}")
        server.send_line(f":tmi.twitch.tv 375 {NICK} :-")
        server.send_line(f":tmi.twitch.tv 372 {NICK} :- You are in a maze of twisty passages.")
        time.sleep(0.2)
        self.assertTrue(adapter.running, "MOTD/CAP ACK 不该影响会话")

    def test_registration_timeout_triggers_reconnect(self):
        """收不到 001 必须重连，而不是静默卡住。"""
        server = self.make_server(auto_register=False)
        adapter, _ = make_twitch()
        adapter.register_timeout = 0.2
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(server.wait_connections(2, timeout=5), "超时后应重连")

    def test_missing_nick_without_client_id_refuses_to_register(self):
        """既无 nick 又无 client_id 时不能瞎发一个 NICK 上去。"""
        server = self.make_server()
        adapter, _ = make_twitch({"nick": "", "client_id": ""})
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_connections(1, timeout=3), "应尝试连接")
        time.sleep(0.4)
        self.assertEqual([x for x in server.lines if x.startswith("NICK ")], [],
                         "拿不到用户名时不能发 NICK")
        self.assertTrue(adapter.running, "线程不应退出（等配置补齐后会重试）")

    def test_nick_can_come_from_helix(self):
        server = self.make_server()
        adapter, _ = make_twitch({"nick": "", "client_id": "cid", "user_id": ""})
        self.connect_adapter(adapter, server)
        calls: list[urllib.request.Request] = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            body = json.dumps(
                {"data": [{"id": "99999", "login": "helixbot", "display_name": "HelixBot"}]}
            ).encode("utf-8")

            class R:
                status = 200

                def read(self_inner):
                    return body

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return R()

        import opencode_bridge.adapters.twitch as twitch_mod

        old = twitch_mod.urllib.request.urlopen
        twitch_mod.urllib.request.urlopen = fake_urlopen
        try:
            self.start_and_registered(adapter, server)
        finally:
            twitch_mod.urllib.request.urlopen = old

        self.assertEqual(adapter.user_id, "99999")
        self.assertEqual(adapter.nick, "helixbot")
        self.assertIn("NICK helixbot", server.lines)
        req = calls[0]
        self.assertEqual(req.get_header("Client-id"), "cid")
        self.assertEqual(req.get_header("Authorization"), "Bearer abcdefgh")

    def test_helix_failure_degrades_and_records_failure(self):
        """Helix 不可用时降级为 nick 比较，并记一条可观测失败（**不真联网**）。"""
        server = self.make_server()
        adapter, _ = make_twitch({"nick": NICK, "client_id": "cid", "user_id": ""})
        self.connect_adapter(adapter, server)
        adapter._http_get = lambda url: (401, {"message": "Unauthorized"})
        adapter._resolve_identity()
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)
        self.assertIn("identity", str(adapter._last_send_error[1]))
        # 降级后仍然能连（用配置里的 nick）
        self.start_and_registered(adapter, server)
        self.assertIn(f"NICK {NICK}", server.lines)

    def test_http_get_maps_transport_and_bad_json(self):
        """``_http_get`` 的错误映射（全部打桩，绝不真联网）。"""
        import opencode_bridge.adapters.twitch as twitch_mod

        adapter, _ = make_twitch({"token": "abc", "client_id": "cid"})
        old = twitch_mod.urllib.request.urlopen

        def boom(req, timeout=None):
            raise OSError("network is down")

        twitch_mod.urllib.request.urlopen = boom
        try:
            status, data = adapter._http_get("https://api.twitch.tv/helix/users")
        finally:
            twitch_mod.urllib.request.urlopen = old
        self.assertEqual(status, 0)
        self.assertIn("transport error", data["message"])

        def html(req, timeout=None):
            class R:
                status = 200

                def read(self_inner):
                    return b"<html>not json</html>"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return R()

        twitch_mod.urllib.request.urlopen = html
        try:
            status, data = adapter._http_get("https://api.twitch.tv/helix/users")
        finally:
            twitch_mod.urllib.request.urlopen = old
        self.assertEqual(status, 200)
        self.assertEqual(data["message"], "non-JSON response")


# ----------------------------------------------------------------------
# 入站
# ----------------------------------------------------------------------
class TestTwitchInbound(TwitchTestCase):
    def _started(self, config=None, hooks=None, **server_kw):
        server = self.make_server(**server_kw)
        adapter, rec = make_twitch(config, hooks)
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        return adapter, rec, server

    def test_channel_mention_becomes_inbound_with_tags(self):
        adapter, rec, server = self._started()
        raw = PRIVMSG_FULL.replace(":Hello world", f":{NICK}: 你好呀")
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1)
        ib = got[0]
        self.assertIsInstance(ib, Inbound)
        self.assertEqual(ib.conversation_id, "twitch:#foo")
        self.assertEqual(ib.text, "你好呀", "提及应被剥掉")
        self.assertEqual(ib.kind, "text")
        self.assertEqual(ib.user_id, "67890", "user-id → author.id")
        self.assertEqual(ib.message_id, "abc-123-def", "id → 消息 id")
        self.assertEqual(ib.platform, "twitch")
        self.assertEqual(ib.raw, raw)

    def test_display_name_with_space_is_kept(self):
        adapter, rec, server = self._started()
        raw = PRIVMSG_SPACED_NAME.replace(":hi", f":@{NICK} hi")
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "hi")
        self.assertEqual(got[0].user_id, "99")

    def test_private_message_needs_no_mention(self):
        adapter, rec, server = self._started()
        raw = (
            "@display-name=Someone;id=m3;user-id=67890 "
            ":someone!s@s.tmi.twitch.tv PRIVMSG opencodebot :psst"
        )
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].conversation_id, f"twitch:{NICK}")
        self.assertEqual(got[0].text, "psst")

    def test_channel_message_without_mention_ignored(self):
        adapter, rec, server = self._started()
        for raw in (
            r"@display-name=Someone;id=m4;user-id=1 :s!s@h PRIVMSG #foo :hi everyone",
            r"@display-name=Someone;id=m5;user-id=1 :s!s@h PRIVMSG #foo :opencodebots rule",
            r"@display-name=Someone;id=m6;user-id=1 :s!s@h PRIVMSG #foo :robot",
        ):
            with self.subTest(raw=raw):
                before = len(rec.inbounds)
                server.send_line(raw)
                time.sleep(0.25)
                self.assertEqual(len(rec.inbounds), before, f"{raw!r} 不该产生 Inbound")

    def test_own_message_is_skipped_by_user_id(self):
        """主判据是 user-id（需要 Helix 查到），不是 user-type。"""
        adapter, rec, server = self._started({"user_id": "67890"})
        raw = PRIVMSG_FULL.replace(":Hello world", f":{NICK}: echo")
        self.assertEqual(self.send_and_wait(server, raw, rec, timeout=0.4), [],
                         "自己发的必须跳过")
        # 别人的 user-id 相同文本要正常进来
        other = raw.replace("user-id=67890", "user-id=12345")
        got = self.send_and_wait(server, other, rec)
        self.assertEqual(len(got), 1)

    def test_own_message_is_skipped_by_nick_when_no_user_id(self):
        """拿不到 user id 时降级为 nick / display-name 比较。"""
        adapter, rec, server = self._started({"user_id": "", "nick": NICK})
        raw = f"@display-name={NICK};id=m7;user-id=67890 :{NICK}!x@y PRIVMSG #foo :{NICK}: echo"
        self.assertEqual(self.send_and_wait(server, raw, rec, timeout=0.4), [],
                         "prefix nick 是自己 → 跳过")
        # nick 对不上、但 display-name 是自己 —— 需要配了 display_name 才有判据
        raw2 = f"@display-name={NICK};id=m8;user-id=67890 :other!x@y PRIVMSG #foo :{NICK}: echo"
        self.assertEqual(len(self.send_and_wait(server, raw2, rec, timeout=0.4)), 1,
                         "没配 display_name 时这条判据不存在，应该照常处理")
        adapter2, rec2, server2 = self._started({"user_id": "", "display_name": NICK})
        raw3 = f"@display-name={NICK};id=m9;user-id=67890 :other!x@y PRIVMSG #foo :{NICK}: echo"
        self.assertEqual(self.send_and_wait(server2, raw3, rec2, timeout=0.4), [],
                         "配了 display_name 后也能按它识别自己")

    def test_mod_flag_is_not_used_as_echo_criterion(self):
        """``user-type=mod`` 属于别人 —— 不能因为有权限标记就丢弃。"""
        adapter, rec, server = self._started({"user_id": "111"})
        raw = (
            "@display-name=Streamer;id=m9;user-id=222;mod=1;user-type=mod "
            ":streamer!s@h PRIVMSG #foo :" + NICK + ": hello boss"
        )
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1, "别人的 mod 消息必须照常进来")
        self.assertEqual(got[0].user_id, "222")

    def test_control_codes_are_cleaned(self):
        adapter, rec, server = self._started()
        raw = (
            "@display-name=Someone;id=m10;user-id=67890 "
            ":s!s@h PRIVMSG #foo :" + NICK + ": \x0304red\x03 \x02bold\x02  zw\u200d"
        )
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "red bold zw")

    def test_only_control_codes_produces_no_inbound(self):
        adapter, rec, server = self._started()
        raw = f"@display-name=X;id=m11;user-id=1 :s!s@h PRIVMSG #foo :{NICK}: \x02\x0f"
        self.assertEqual(self.send_and_wait(server, raw, rec, timeout=0.4), [])

    def test_authorization_gate_runs_before_inbound(self):
        hooks = RecordingHooks()
        adapter, rec, server = self._started({"allowed_chat_ids": ["#allowed"]}, hooks)
        raw = f"@display-name=X;id=m12;user-id=1 :s!s@h PRIVMSG #other :{NICK}: secret"
        self.assertEqual(self.send_and_wait(server, raw, rec, timeout=0.4), [])
        raw2 = f"@display-name=X;id=m13;user-id=1 :s!s@h PRIVMSG #allowed :{NICK}: ok"
        got = self.send_and_wait(server, raw2, rec)
        self.assertEqual(len(got), 1)

    def test_duplicate_message_id_is_dropped(self):
        """重连后 Twitch 可能重发同一条；用 id tag 去重。"""
        adapter, rec, server = self._started()
        raw = f"@display-name=X;id=dup-1;user-id=1 :s!s@h PRIVMSG #foo :{NICK}: once"
        self.assertEqual(len(self.send_and_wait(server, raw, rec)), 1)
        self.assertEqual(self.send_and_wait(server, raw, rec, timeout=0.4), [],
                         "同 id 重复投递应被丢弃")

    def test_malformed_lines_do_not_raise(self):
        adapter, rec, server = self._started()
        for raw in ("", ":a@h PRIVMSG", "@only-tags", "PING", "PING :",
                    ":a@h PRIVMSG #foo :", "garbage line without prefix"):
            with self.subTest(raw=raw):
                server.send_line(raw)
        server.send_line(PRIVMSG_FULL.replace(":Hello world", f":{NICK}: still alive"))
        got = self.send_and_wait(server, "", rec) if False else None
        deadline = time.time() + 3
        while len(rec.inbounds) < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1, "畸形行不应影响后续消息")
        self.assertTrue(adapter.running)

    def test_hook_exception_is_contained(self):
        class Exploding(RecordingHooks):
            """第一条记录后抛异常，用来验证读循环不被 hook 拖死。"""

            def on_inbound(self, inbound: Inbound) -> None:
                self.inbounds.append(inbound)
                if inbound.text == "boom":
                    raise RuntimeError("hook down")

        adapter, rec, server = self._started(hooks=Exploding())
        raw = f"@display-name=X;id=m14;user-id=1 :s!s@h PRIVMSG #foo :{NICK}: boom"
        self.send_and_wait(server, raw, rec, timeout=0.5)
        raw2 = f"@display-name=X;id=m15;user-id=1 :s!s@h PRIVMSG #foo :{NICK}: again"
        got = self.send_and_wait(server, raw2, rec)
        self.assertEqual(len(got), 1, "hook 异常后读循环要继续")
        self.assertTrue(adapter.running)


# ----------------------------------------------------------------------
# 帧 / 行重组
# ----------------------------------------------------------------------
class TestTwitchFraming(TwitchTestCase):
    def _started(self, config=None):
        server = self.make_server()
        adapter, rec = make_twitch(config)
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        return adapter, rec, server

    def test_multiple_lines_in_one_frame(self):
        adapter, rec, server = self._started()
        two = (
            f"@display-name=A;id=f1;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: first\r\n"
            f"@display-name=B;id=f2;user-id=2 :b!b@h PRIVMSG #foo :{NICK}: second\r\n"
        )
        server.send_frame(two)
        deadline = time.time() + 3
        while len(rec.inbounds) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual([ib.text for ib in rec.inbounds], ["first", "second"])

    def test_one_line_split_across_frames(self):
        adapter, rec, server = self._started()
        raw = f"@display-name=A;id=f3;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: split me"
        server.send_frame(raw[:40])
        time.sleep(0.15)
        self.assertEqual(rec.inbounds, [], "半行不应产生 Inbound")
        server.send_frame(raw[40:] + "\r\n")
        deadline = time.time() + 3
        while len(rec.inbounds) < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1, "跨帧的半行必须重组")
        self.assertEqual(rec.inbounds[0].text, "split me")

    def test_line_split_into_three_frames_plus_trailing_partial(self):
        """一行拆成 3 帧；帧尾再留**半行**，下一帧补齐后两条都要正确重组。"""
        adapter, rec, server = self._started()
        raw = f"@display-name=A;id=f4;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: abc"
        # 三帧拼出第一行（含 CRLF），第三帧末尾故意留半行 "@display-name=B;id=f5"
        for chunk in (raw[:20], raw[20:45], raw[45:] + "\r\n@display-name=B;id=f5"):
            server.send_frame(chunk)
            time.sleep(0.05)
        deadline = time.time() + 3
        while len(rec.inbounds) < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual([ib.text for ib in rec.inbounds], ["abc"],
                         "第一行应已由三帧重组出来；残留半行不产生 Inbound")
        # 补齐残留半行（send_and_wait 走 send_line，会自己补 CRLF）
        got = self.send_and_wait(
            server, f";user-id=2 :b!b@h PRIVMSG #foo :{NICK}: x", rec
        )
        self.assertEqual(len(got), 1, "残留半行必须与后一帧拼成完整的一行")
        self.assertEqual(got[0].text, "x")
        self.assertEqual(got[0].message_id, "f5", "补齐后 id tag 要解析对")
        self.assertEqual([ib.text for ib in rec.inbounds], ["abc", "x"])

    def test_frame_without_trailing_newline_waits_for_next_frame(self):
        adapter, rec, server = self._started()
        server.send_frame(f"@display-name=A;id=f6;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: held")
        time.sleep(0.3)
        self.assertEqual(rec.inbounds, [], "没有 \\n 就不算一行")
        server.send_frame("\r\n")
        deadline = time.time() + 3
        while len(rec.inbounds) < 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1)

    def test_long_line_with_tags_exceeds_512_bytes(self):
        """Twitch 发的行远超 IRC 的 512 字节上限 —— 入站不能按 512 截断。"""
        adapter, rec, server = self._started()
        body = "x" * 900
        raw = (
            f"@badge-info=;color=#1E90FF;display-name=Long;emotes=;"
            f"id=long-1;mod=0;room-id=12345;subscriber=0;turbo=0;user-id=777;user-type= "
            f":long!l@h PRIVMSG #foo :{NICK}: {body}"
        )
        self.assertGreater(len(raw.encode("utf-8")), 512)
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1, "超 512 字节的入站行必须照常处理")
        self.assertEqual(got[0].text, body)

    def test_partial_line_is_dropped_on_disconnect(self):
        adapter, rec, server = self._started()
        server.send_frame("@display-name=A;id=partial;user-id=1 :a!a@h PRIVMSG #foo :hal")
        time.sleep(0.15)
        self.assertEqual(rec.inbounds, [])
        server.drop()
        self.assertTrue(server.wait_connections(2, timeout=5))
        # 残留缓冲必须被丢弃：重连后的一行不会被前面的半行污染
        deadline = time.time() + 5
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        raw = f"@display-name=A;id=fresh;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: clean"
        got = self.send_and_wait(server, raw, rec)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "clean", "残留半行污染了新连接")


# ----------------------------------------------------------------------
# 出站
# ----------------------------------------------------------------------
class TestTwitchSend(TwitchTestCase):
    def _started(self, config=None):
        server = self.make_server()
        adapter, rec = make_twitch(config)
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        return adapter, rec, server

    def test_send_line_shape(self):
        adapter, _, server = self._started()
        handle = adapter.send(Outbound("twitch:#foo", "hello twitch"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "twitch")
        self.assertEqual(handle.conversation_id, "twitch:#foo")
        self.assertEqual(handle.message_id, "1")
        self.assertEqual(server.wait_for(lambda x: x == "PRIVMSG #foo :hello twitch"),
                         "PRIVMSG #foo :hello twitch")

    def test_channel_gets_hash(self):
        adapter, _, server = self._started()
        adapter.send(Outbound("twitch:foo", "no hash"))
        self.assertEqual(server.wait_for(lambda x: x == "PRIVMSG #foo :no hash"),
                         "PRIVMSG #foo :no hash")

    def test_long_text_is_split(self):
        adapter, _, server = self._started()
        text = "y" * 1000
        adapter.send(Outbound("twitch:#foo", text))
        deadline = time.time() + 3
        while len(server.privmsg_lines("#foo")) < 3 and time.time() < deadline:
            time.sleep(0.01)
        lines = server.privmsg_lines("#foo")
        self.assertEqual(len(lines), 3, "1000 字符 / 400 = 3 条")
        self.assertEqual("".join(x.split(" :", 1)[1] for x in lines), text)
        for line in lines:
            self.assertLessEqual(len(line.split(" :", 1)[1]), MESSAGE_LIMIT)

    def test_chinese_message_fits_one_line(self):
        """400 个中文字 = 1200 字节，仍应单条发出（字节预算 2048）。"""
        adapter, _, server = self._started()
        adapter.send(Outbound("twitch:#foo", "字" * MESSAGE_LIMIT))
        deadline = time.time() + 3
        while not server.privmsg_lines("#foo") and time.time() < deadline:
            time.sleep(0.01)
        msgs = server.privmsg_lines("#foo")
        self.assertEqual(len(msgs), 1, f"应单条发出，实际 {len(msgs)} 条")
        self.assertEqual(msgs[0].split(" :", 1)[1], "字" * MESSAGE_LIMIT)
        self.assertLessEqual(len(msgs[0].encode("utf-8")), LINE_BUDGET)

    def test_every_line_respects_byte_budget(self):
        adapter, _, server = self._started()
        adapter.send(Outbound("twitch:#foo", "字" * (MESSAGE_LIMIT * 3)))
        deadline = time.time() + 3
        while len(server.privmsg_lines("#foo")) < 2 and time.time() < deadline:
            time.sleep(0.01)
        for line in server.privmsg_lines("#foo"):
            self.assertLessEqual(len(line.encode("utf-8")), LINE_BUDGET)
            self.assertNotIn("\ufffd", line, "不能切出乱码")

    def test_send_line_truncates_oversized_line(self):
        """绕过 ``send()`` 直接写超长行时按字符边界截到字节预算内。"""
        adapter, _, server = self._started()
        base = len(server.frames)
        ws = adapter._current_ws()          # _send_line 要的是 WS 客户端，不是裸 socket
        self.assertIsNotNone(ws)
        self.assertTrue(adapter._send_line(ws, "PRIVMSG #foo :" + "字" * 3000))
        frames = server.wait_frames(base + 1)
        self.assertEqual(len(frames), base + 1)
        payload = frames[-1]
        self.assertLessEqual(len(payload.encode("utf-8")), LINE_BUDGET)
        self.assertNotIn("\ufffd", payload, "截断必须落在字符边界上")
        self.assertTrue(payload.startswith("PRIVMSG #foo :"))
        # 行内没有 CR/LF：整条就是**一个** IRC 行
        self.assertNotIn("\r", payload)
        self.assertNotIn("\n", payload)
        # 截断后的正文正好是预算允许的长度，且仍能还原成合法 UTF-8
        self.assertEqual(payload.encode("utf-8").decode("utf-8"), payload)

    def test_send_strips_embedded_newlines(self):
        """注入的 \\r\\n 必须被折成空格 —— 绝不能变成一条真的 JOIN 命令。"""
        adapter, _, server = self._started()
        ws = adapter._current_ws()
        self.assertTrue(adapter._send_line(ws, "PRIVMSG #foo :a\r\nJOIN #evil"))
        got = server.wait_for(lambda x: x == "PRIVMSG #foo :a JOIN #evil")
        self.assertEqual(got, "PRIVMSG #foo :a JOIN #evil",
                         "换行应折成空格后仍是一条 PRIVMSG")
        # 服务器侧只看到一条 PRIVMSG；"JOIN #evil" 没有变成独立命令
        time.sleep(0.2)
        self.assertEqual([x for x in server.lines if x.startswith("JOIN #evil")], [],
                         "注入的换行绝不能变成一条真命令")
        self.assertEqual([x for x in server.lines if x.startswith("JOIN ")], ["JOIN #foo"],
                         "整个会话里只应有一次正常 JOIN")
        self.assertEqual(server.privmsg_lines("#foo"), ["PRIVMSG #foo :a JOIN #evil"])

    def test_throttle_enforces_min_interval(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        adapter.min_interval = 0.4       # 测试里放大，便于观测
        self.connect_adapter(adapter, server)
        self.start_and_registered(adapter, server)
        base = len(server.privmsg_lines("#foo"))
        began = time.monotonic()
        adapter.send(Outbound("twitch:#foo", "one"))
        adapter.send(Outbound("twitch:#foo", "two"))
        elapsed = time.monotonic() - began
        deadline = time.time() + 3
        while len(server.privmsg_lines("#foo")) < base + 2 and time.time() < deadline:
            time.sleep(0.01)
        lines = server.privmsg_lines("#foo")
        self.assertEqual(len(lines) - base, 2)
        self.assertGreaterEqual(elapsed, 0.35, f"第二条被限流了？实际 {elapsed:.2f}s")

    def test_edit_always_false_and_answer_noop(self):
        adapter, _, server = self._started()
        handle = MsgHandle("twitch:#foo", "1", "twitch")
        self.assertIs(adapter.edit(handle, Outbound("twitch:#foo", "edited")), False)
        self.assertIs(adapter.edit(handle, Outbound("twitch:#foo", "")), False)
        self.assertEqual(server.privmsg_lines("#foo"), [], "edit 不该发任何东西")
        self.assertIsNone(adapter.answer("q1"))
        self.assertIsNone(adapter.answer("q1", "text"))

    def test_send_not_connected_is_structured_failure(self):
        adapter, _ = make_twitch()
        result = adapter.send_result(Outbound("twitch:#foo", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertIn("not connected", result.error_detail)

    def test_send_bad_arguments_are_bad_format(self):
        adapter, _, _ = self._started()
        for out, why in ((Outbound("twitch:", "hi"), "空目标"),
                         (Outbound("twitch:#foo", ""), "空正文")):
            with self.subTest(why=why):
                result = adapter.send_result(out)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_without_token_is_bad_format(self):
        adapter, _ = make_twitch({"token": ""})
        result = adapter.send_result(Outbound("twitch:#foo", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_after_disconnect_is_structured_failure(self):
        adapter, _, server = self._started()
        server.drop()
        deadline = time.time() + 3
        while adapter._current_ws() is not None and time.time() < deadline:
            time.sleep(0.01)
        result = adapter.send_result(Outbound("twitch:#foo", "hi"))
        self.assertFalse(result.ok)
        self.assertNotEqual(result.error_kind, SendError.UNKNOWN)
        self.assertIn(result.error_kind, (SendError.TRANSIENT, SendError.BAD_FORMAT))

    def test_send_success_result_is_ok(self):
        adapter, _, _ = self._started()
        result = adapter.send_result(Outbound("twitch:#foo", "hi"))
        self.assertTrue(result.ok)
        self.assertFalse(result.partial)
        self.assertIsNotNone(result.handle)


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
class TestTwitchLifecycle(TwitchTestCase):
    def test_missing_token_does_not_start(self):
        adapter, _ = make_twitch({"token": ""})
        with self.assertLogs("opencode_bridge.adapters.twitch", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("token" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_missing_channel_does_not_start(self):
        adapter, _ = make_twitch({"channel": ""})
        with self.assertLogs("opencode_bridge.adapters.twitch", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("channel" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_start_stop_roundtrip(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(adapter.running)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_stop_wakes_blocked_recv_quickly(self):
        """stop() 必须先关 WS，阻塞中的 recv 立刻返回（不能挂满 ws_timeout）。"""
        server = self.make_server()
        adapter, _ = make_twitch()
        adapter.ws_timeout = 30.0
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
        adapter, rec = make_twitch()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        server.drop()
        self.assertTrue(server.wait_connections(2, timeout=5), "断线后应自动重连")
        deadline = time.time() + 5
        while not adapter._registered and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(adapter._registered, "重连后应重新注册")
        raw = f"@display-name=A;id=rc1;user-id=1 :a!a@h PRIVMSG #foo :{NICK}: after"
        before = len(rec.inbounds)
        server.send_line(raw)
        deadline = time.time() + 3
        while len(rec.inbounds) == before and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(rec.inbounds), 1)
        self.assertEqual(rec.inbounds[0].text, "after")

    def test_server_requested_reconnect(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        server.send_line("RECONNECT")
        self.assertTrue(server.wait_connections(2, timeout=5),
                        "RECONNECT 命令应触发重连")

    def test_connect_failure_retries_with_backoff(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        adapter, _ = make_twitch()
        adapter.endpoint = f"ws://127.0.0.1:{dead_port}/"
        adapter.reconnect_delay = 0.05
        adapter.max_reconnect_delay = 0.1
        adapter.start()
        self.addCleanup(adapter.stop)
        time.sleep(0.5)
        self.assertTrue(adapter.running, "连不上也不能让线程退出")
        self.assertIsNone(adapter._current_ws())

    def test_stop_before_start_is_harmless(self):
        adapter, _ = make_twitch()
        adapter.stop()
        self.assertFalse(adapter.running)


# ----------------------------------------------------------------------
# 传输层接线值（G4 迁移）
# ----------------------------------------------------------------------
class TestTwitchTransportWiring(TwitchTestCase):
    """退避 / 钩子接线：这些值决定"什么时候重连、等多久重连"。

    迁移前它们散在 ``_session_loop`` 里，没有测试守着；迁移后成了构造
    :class:`~opencode_bridge.transport.WebSocketTransport` 的实参 —— **值改错不会
    让任何功能用例失败**（重连只是慢一点/快一点），所以必须显式钉住。
    """

    def test_backoff_wiring_matches_the_legacy_semantics(self):
        adapter, _ = make_twitch()
        transport = adapter._make_transport()
        self.assertIsInstance(transport, WebSocketTransport)
        self.assertEqual(transport.min_backoff, adapter.reconnect_delay,
                         "迁移前'连上过之后'每次都等固定的 reconnect_delay")
        self.assertEqual(transport.max_backoff, adapter.max_reconnect_delay)
        self.assertEqual(
            transport.reset_after, twitch_mod.RECONNECT_STABLE_AFTER,
            "建连失败那一支是 wait(min(delay,max)); delay*=2 —— 所以 reset_after 必须是"
            "**正数**（极小值即可）；传 0 会让连不上时永远只等下限，退避形同虚设",
        )
        self.assertGreater(transport.reset_after, 0.0)
        self.assertEqual(transport.label, "twitch")
        self.assertEqual(transport._idle_delay(), 0.0,
                         "WS 类传输阻塞在 recv()，不该有空转节流")
        # make_twitch 把 reconnect_delay 调成 0.05s；max_reconnect_delay 仍是类默认 60s
        self.assertEqual(
            [transport._next_backoff(survived=False) for _ in range(4)],
            [0.05, 0.1, 0.2, 0.4],
        )
        self.assertEqual(transport.max_backoff, twitch_mod.MAX_RECONNECT_DELAY)
        # 建连成功 ⇒ 立刻重置回下限（迁移前那句 delay = self.reconnect_delay）
        self.assertEqual(transport._next_backoff(survived=True), 0.05)
        self.assertEqual(transport._next_backoff(survived=False), 0.05,
                         "重置之后的第一拍就是下限，不能接着上一次的增长继续涨")

    def test_session_hooks_are_wired_to_the_adapter(self):
        adapter, _ = make_twitch()
        transport = adapter._make_transport()
        self.assertEqual(transport._on_open_fn, adapter._on_open)
        self.assertEqual(transport._on_message, adapter._on_frame)
        self.assertEqual(transport._on_close_fn, adapter._on_close)

    def test_thread_and_connection_belong_to_the_transport(self):
        server = self.make_server()
        adapter, _ = make_twitch()
        self.connect_adapter(adapter, server)
        self.assertIsNone(adapter._thread, "入站线程归传输层所有，_thread 必须恒为 None")
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertIsNotNone(server.wait_for(lambda x: x.startswith("JOIN ")))
        self.assertTrue(adapter.running, "running 必须代理到传输层")
        self.assertIsInstance(adapter.transport, WebSocketTransport)
        self.assertIsNotNone(adapter._current_ws())
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport, "stop() 之后传输层引用必须清掉")

    def test_running_is_overridden_not_inherited(self):
        """基类 ``running`` 读 ``self._thread``（永远是 None）⇒ 继承它就永远 False。"""
        from opencode_bridge.adapters.base import Adapter as BaseAdapter

        self.assertIn("running", TwitchAdapter.__dict__)
        self.assertIsNot(TwitchAdapter.running, BaseAdapter.running)

    def test_dedup_window_survives_reconnects(self):
        """去重窗口是**实例级**的（不随会话重置）—— 重连后 Twitch 重发要靠它挡掉。"""
        adapter, _ = make_twitch()
        self.assertTrue(adapter._remember_id("msg-1"))
        self.assertFalse(adapter._remember_id("msg-1"))
        self.assertTrue(adapter._remember_id("msg-2"))


if __name__ == "__main__":
    unittest.main()
