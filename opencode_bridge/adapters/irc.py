"""Lane B — IRC adapter (tasks.md T3.3). Standard library only.

传输层是**裸 TCP + 行协议**（不是 HTTP，也不是 WebSocket）：``socket.create_connection``
连上服务器，之后一切都是 ``\\r\\n`` 结尾的 UTF-8 行。全部收发收敛在
:meth:`IRCAdapter._write_line` / :meth:`IRCAdapter._read_loop` 两个方法后面，测试可以
用本机回环的真服务器线程验证真实字节行为（见 ``tests/test_irc.py``）。

三个 IRC 特有的点：

1. **512 字节行长上限**：RFC 2812 规定整行（含 ``:prefix``、命令名、``CRLF``）
   上限 512 字节。正文可用字节 = 512 − 命令开销 − 目标长度。中文一个字 3 字节，
   所以"字符数"永远推不出字节数 —— 发送前必须**按字符切、按字节校验**
   （:func:`_fit_utf8` / :func:`_byte_safe_split`），否则会切出乱码或直接被服务器
   截断。

2. **只响应提及**：频道是公共空间，``PRIVMSG #chan :hi bot`` 只有提到底层的 nick
   才算对我们说话（:meth:`IRCAdapter._mention`）。私聊（target == 我们的 nick）
   则直接当作发给我的消息。

3. **不能编辑**：IRC 没有编辑消息的概念。:meth:`IRCAdapter.edit` 永远返回 ``False``，
   上层 ``core.py`` 会退化成"发一条新消息"—— 这正是 IRC 该有的行为。
"""

from __future__ import annotations

import base64
import logging
import re
import socket
import ssl
import threading
import time
from typing import Any, List, Optional

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from .base import Adapter, register

logger = logging.getLogger("opencode_bridge.adapters.irc")

__all__ = ["IRCAdapter", "MESSAGE_LIMIT"]

#: RFC 2812 §2.3：整行（含 CRLF）上限 512 字节。
LINE_LIMIT = 512
#: 能力声明的**字符**上限：512 字节行上限减去 ``PRIVMSG `` + ``:`` + CRLF +
#: 目标名（频道/昵称）的开销后的保守值。真正的硬约束是字节，由
#: :meth:`IRCAdapter._body_budget` 在每条消息上重新计算。
MESSAGE_LIMIT = 400
DEFAULT_PORT = 6667
TLS_PORT = 6697
CONNECT_TIMEOUT = 15.0
SOCKET_TIMEOUT = 1.0        # 读循环的 tick 间隔：兼顾保活定时器与 stop 响应
REGISTER_TIMEOUT = 30.0     # 等 001 (RPL_WELCOME) 的上限，超时按断线重连
PING_INTERVAL = 300.0       # 服务端不主动 ping 时的自保活间隔（秒）
RECONNECT_DELAY = 5.0       # 首次重连延迟（秒）
MAX_RECONNECT_DELAY = 60.0  # 重连退避上限（秒）

# 数值回复（只需关心会影响注册/入站的那几个）
RPL_WELCOME = "001"
ERR_NICKNAMEINUSE = "433"
ERR_NOMOTD = "422"
AUTH_FAILURE = "904"
AUTH_ABORTED = "906"
AUTH_SUCCESS = "903"

# ---------------------------------------------------------------------------
# 控制码 / 提及
# ---------------------------------------------------------------------------

#: 颜色码：``\x03`` 后跟 1~2 位数字（``03,04`` 前景+背景）或 IRCv3 的十六进制 ``\x04``。
_COLOR_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|\x04[0-9A-Fa-f]{0,6}(?:,[0-9A-Fa-f]{0,6})?")
#: 格式控制码（RFC 2812 + IRCv3 formatting）：
#: ``\x02`` 粗体、``\x0f`` 复位、``\x11`` 等宽、``\x16`` 反色、``\x1d`` 斜体、
#: ``\x1e`` 删除线、``\x1f`` 下划线、``\x07`` BEL。
_FORMAT_RE = re.compile("[\x02\x07\x0f\x11\x16\x1d\x1e\x1f]")
#: 剩下的 C0/C1 控制字符（含 ``\n`` / ``\r`` / ``\t``，它们都会破坏行协议）。
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: 零宽 / 双向控制字符（用码点显式构造，避免源码里出现不可见字符）。
_ZERO_WIDTH_CHARS = "".join(
    chr(cp)
    for cp in (
        0x00AD,                  # soft hyphen
        *range(0x200B, 0x2010),   # ZWSP ZWNJ ZWJ LRM RLM
        *range(0x202A, 0x202F),   # LRE RLE PDF LRO RLO
        0x2060,                  # word joiner
        0xFEFF,                  # BOM / zero-width no-break space
    )
)
_ZERO_WIDTH_RE = re.compile("[" + re.escape(_ZERO_WIDTH_CHARS) + "]")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")

#: IRC nick 的常见字符：字母数字下划线，以及 RFC 2812 §2.3.1 的 special
#: （``[]\`_^{|}`` 等）。**词边界**是"不属于 nick 字符集"。
_NICK_SPECIALS = "\\-[]`^{}|"
_NICK_BASE_CHARS = "A-Za-z0-9_"


def _make_mention_re(nick: str) -> Optional[re.Pattern]:
    """编译 nick 的提及正则；nick 为空时返回 ``None``（无从匹配）。

    词边界 = "不是字母/数字/下划线"，**外加 nick 自己用到的 special 字符**：
    nick 含 ``-`` 时 ``foo-bar`` 不算提及 ``foo``；nick 不含 ``-`` 时
    ``[bot]:`` / ``foo-bot`` 这种真实频道写法又能正常识别 —— 比"永远按 RFC
    昵称字符集"更贴近实际用法。
    """
    if not nick:
        return None
    charset = _NICK_BASE_CHARS + "".join(
        re.escape(ch) for ch in _NICK_SPECIALS if ch in nick
    )
    return re.compile(
        rf"(?<![{charset}]){re.escape(nick)}(?![{charset}])",
        re.IGNORECASE,
    )


def _fit_utf8(text: str, max_bytes: int) -> str:
    """把 ``text`` 截到不超过 ``max_bytes`` 个 UTF-8 **字节**，且不切出乱码。

    按字符累加字节数，遇到装不下的字符就停 —— 因此返回串一定是合法 UTF-8
    （不会出现半个多字节字符），代价最多丢一个字符。
    """
    if max_bytes <= 0:
        return ""
    used = 0
    out: List[str] = []
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > max_bytes:
            break
        out.append(ch)
        used += size
    return "".join(out)


def _byte_safe_split(text: str, max_bytes: int) -> List[str]:
    """按**字节**预算把 ``text`` 切成若干段，每段 UTF-8 合法且不超过预算。

    ``split_text`` 的阈值是**字符**数，对中文无效（1 字 = 3 字节）。IRC 的硬约束
    是字节，所以字符切完之后还要按字节再切一遍；切点永远落在字符边界上。
    """
    if max_bytes <= 0 or not text:
        return []
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    out: List[str] = []
    buf: List[str] = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > max_bytes and buf:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(ch)
        used += size
    if buf:
        out.append("".join(buf))
    return out


def strip_control_codes(text: str) -> str:
    """清掉 IRC 控制码 / 零宽字符，换行折成空格。"""
    if not text:
        return ""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("\t", " ")
    text = _COLOR_RE.sub("", text)
    text = _FORMAT_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _SPACE_RUN_RE.sub(" ", text)
    return text.strip()


def _prefix_nick(prefix: str) -> str:
    """``:nick!user@host`` → ``nick``。"""
    return prefix.split("!", 1)[0].split("@", 1)[0]


def _parse_line(raw: str) -> tuple[str, str, List[str]]:
    """拆一行成 ``(prefix, COMMAND, params)``。``COMMAND`` 已大写。

    末位参数以 ``" :"`` 开头（可含空格），前面的按空格切。
    """
    line = raw.rstrip("\r\n")
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    if " :" in line:
        head, _, trailing = line.partition(" :")
        params = head.split()
        params.append(trailing)
    else:
        params = line.split()
    command = params.pop(0).upper() if params else ""
    return prefix, command, params


@register("irc")
class IRCAdapter(Adapter):
    """IRC client adapter（``PRIVMSG`` 收发 + 提及触发 + PING/PONG 保活）。"""

    name = "irc"
    label = "IRC"
    max_message_length = MESSAGE_LIMIT          # 512 字节行上限减去前缀开销后的安全值
    supports_inbound = True
    supports_inline_buttons = False             # IRC 无按钮
    supports_media = False                      # v1 只发纯文本

    #: IRC 不用 ``bot_token``。这里按**实际凭据键**声明：
    #: ``host`` + ``nick`` 是连上服务器的最低条件；``channels`` 是**入站**的最低
    #: 条件（没有频道可 JOIN 就永远收不到消息）—— 不列出来的话状态视图会把
    #: "只发出站"报成"入站已就绪"，正是基类注释里点名要避免的静默降级。
    required_tokens = ("host", "nick", "channels")
    # 只发 PRIVMSG 不需要频道列表（可以给私聊或任意已知目标发），所以出站
    # 凭据比入站少一项。状态视图据此区分"能发"与"能收"。
    outbound_tokens = ("host", "nick")

    # 类级旋钮（测试可在实例上覆盖）。
    message_limit = MESSAGE_LIMIT
    line_limit = LINE_LIMIT
    min_interval = 1.0          # 防 flood：两次 PRIVMSG 的最小间隔（秒）
    ping_interval = PING_INTERVAL
    register_timeout = REGISTER_TIMEOUT
    socket_timeout = SOCKET_TIMEOUT
    reconnect_delay = RECONNECT_DELAY
    max_reconnect_delay = MAX_RECONNECT_DELAY

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.host: str = str(self.config.get("host") or "").strip()
        self.nick: str = str(self.config.get("nick") or "").strip()
        self.channels: List[str] = self._channel_list(self.config.get("channels"))
        self.use_tls: bool = bool(self.config.get("use_tls"))
        self.port: int = self._resolve_port()
        self.server_password: str = str(self.config.get("server_password") or "")
        self.bot_password: str = str(self.config.get("bot_password") or "")
        self.realname: str = (
            str(self.config.get("realname") or "").strip() or self.nick or "opencode-bridge"
        )
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._registered = False
        self._registration_sent = False
        self._rbuf = bytearray()
        self._mention_re = _make_mention_re(self.nick)
        self._sasl_state = ""
        # 见过的对方昵称（入站时积累）：用来区分"私聊昵称"与"频道名"，
        # 否则回复私聊会被错误地补上 ``#`` 变成往频道发。
        self._known_nicks: set[str] = {self.nick.lower()} if self.nick else set()
        # 测试注入点：TLS 包装（真 TLS 需要证书，测试里替换成 no-op）
        self._tls_wrap = self._default_tls_wrap

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_list(raw: Any) -> List[str]:
        """``channels`` 接受字符串（逗号/空白分隔）或列表。"""
        items: List[str] = []
        if isinstance(raw, (list, tuple, set)):
            items = [str(x).strip() for x in raw]
        elif raw not in (None, ""):
            items = [x.strip() for x in str(raw).replace(",", " ").split()]
        return [x for x in items if x]

    def _resolve_port(self) -> int:
        raw = self.config.get("port")
        try:
            if raw not in (None, ""):
                return int(raw)
        except (TypeError, ValueError):
            logger.warning("irc: bad port %r; falling back to default", raw)
        return TLS_PORT if self.use_tls else DEFAULT_PORT

    # ------------------------------------------------------------------
    # 连接与 TLS
    # ------------------------------------------------------------------
    def _default_tls_wrap(self, sock: socket.socket) -> socket.socket:
        """``ssl.create_default_context().wrap_socket(...)``（默认校验证书）。"""
        context = ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=self.host)

    def _open_socket(self) -> socket.socket:
        """建 TCP 连接（必要时升级为 TLS）。抛出的异常由调用方退避重试。"""
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        try:
            if self.use_tls:
                sock = self._tls_wrap(sock)
            sock.settimeout(self.socket_timeout)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        return sock

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """缺 ``host`` / ``nick`` 时只告警并返回（不抛异常、不起线程）。"""
        if not self.host:
            logger.warning("irc: host missing; adapter not started")
            return
        if not self.nick:
            logger.warning("irc: nick missing; adapter not started")
            return
        if not self.channels:
            logger.warning(
                "irc: no channels configured; inbound will never fire "
                "(outbound still works)"
            )
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._session_loop, name="irc-client", daemon=True
        )
        self._thread = thread
        thread.start()
        logger.info(
            "irc: connecting to %s:%s as %s (tls=%s, channels=%s)",
            self.host,
            self.port,
            self.nick,
            self.use_tls,
            self.channels or "none",
        )

    def stop(self) -> None:
        """先 shutdown socket 让阻塞中的 ``recv`` 立刻返回，再停线程。

        顺序不能反：基类 ``stop()`` 会 join 线程（5s 超时），而读循环的
        ``recv`` 最长阻塞 ``socket_timeout``；不先关连接就会每次 stop 都白等。
        """
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        super().stop()

    # ------------------------------------------------------------------
    # 会话：注册 → JOIN → 读循环 → 断线重连
    # ------------------------------------------------------------------
    def _session_loop(self) -> None:
        """连接 + 注册 + 收消息，任何异常都在此被吞掉并退避重连。"""
        delay = self.reconnect_delay
        while not self._stop_event.is_set():
            try:
                sock = self._open_socket()
            except Exception as exc:
                logger.warning("irc: connect to %s:%s failed: %s", self.host, self.port, exc)
                if self._stop_event.wait(min(delay, self.max_reconnect_delay)):
                    return
                delay = min(delay * 2, self.max_reconnect_delay)
                continue
            with self._sock_lock:
                self._sock = sock
            delay = self.reconnect_delay   # 连上过一次就重置退避
            try:
                self._register(sock)
                if not self._stop_event.is_set():
                    self._join_channels(sock)
                    self._read_loop(sock)
            except Exception as exc:
                logger.warning("irc: session ended: %s", exc)
            finally:
                self._registered = False
                with self._sock_lock:
                    if self._sock is sock:
                        self._sock = None
                try:
                    sock.close()
                except Exception:
                    pass
            if self._stop_event.wait(self.reconnect_delay):
                return

    def _register(self, sock: socket.socket) -> None:
        """PASS / (CAP+SASL) / NICK / USER，并等待 001 (RPL_WELCOME)。

        没等到 001 就抛异常，由 :meth:`_session_loop` 当成断线重连 —— 静默卡在
        "已连上但没注册"是最难排查的故障。
        """
        # 每次会话重置协商状态与读缓冲（读缓冲必须跨 ``_read_loop`` 调用保留，
        # 否则"001 与第一条 PRIVMSG 在同一个 TCP 段里"时会丢掉后者）。
        self._registration_sent = False
        self._registered = False
        self._sasl_state = ""
        self._rbuf = bytearray()
        if self.server_password:
            self._write_line(sock, f"PASS {self.server_password}")
        if self.bot_password:
            # SASL 必须在 NICK/USER **之前**完成协商，否则服务器会直接断开。
            self._write_line(sock, "CAP LS 302")
            self._sasl_state = "ls_sent"
        else:
            self._start_registration(sock)
        deadline = time.monotonic() + self.register_timeout
        self._read_loop(sock, deadline=deadline)
        if not self._registered:
            raise TimeoutError("001 RPL_WELCOME not received in time")

    def _start_registration(self, sock: socket.socket) -> None:
        """发 NICK/USER。幂等：CAP 协商与 SASL 两条路都会走到这里。"""
        if self._registration_sent or self._stop_event.is_set():
            return
        self._registration_sent = True
        self._write_line(sock, f"NICK {self.nick}")
        self._write_line(sock, f"USER {self.nick} 0 * :{self.realname}")

    def _join_channels(self, sock: socket.socket) -> None:
        if not self.channels:
            return
        self._write_line(sock, "JOIN " + ",".join(self.channels))
        logger.info("irc: joined %s", ",".join(self.channels))

    def _read_loop(
        self, sock: socket.socket, *, deadline: Optional[float] = None
    ) -> None:
        """按行读取并派发，直到连接断开 / 超时 / 收到 stop。

        ``deadline`` 非 None 表示处于注册阶段：到点仍未收到 001 就返回。

        每轮先消化缓冲里**已有的整行**、再读新数据，且缓冲是实例字段
        :attr:`_rbuf` 并原地修改 —— 这样"001 与第一条 PRIVMSG 在同一个 TCP 段里
        到达"时，注册阶段读循环返回后，运行阶段会先处理遗留行再阻塞读，不会把
        那几条消息静默吞掉。
        """
        buf = self._rbuf
        last_ping = time.monotonic()
        while not self._stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return
            if b"\n" not in buf:
                try:
                    data: Optional[bytes] = sock.recv(4096)
                except socket.timeout:
                    data = None      # tick：检查保活定时器与 stop
                except OSError:
                    return          # socket 被 stop() 关掉了
                if data is None:
                    if time.monotonic() - last_ping >= self.ping_interval:
                        # 服务端不 ping 时我们自己保活，否则会被静默踢下线。
                        self._write_line(
                            sock, f"PING :opencode-{int(time.monotonic())}"
                        )
                        last_ping = time.monotonic()
                    continue
                if not data:
                    return          # 对端关闭 = 掉线，交给上层重连
                buf += data
                if b"\n" not in buf and len(buf) > self.line_limit:
                    logger.warning(
                        "irc: dropping oversized inbound line (%d bytes)", len(buf)
                    )
                    del buf[:]
                    continue
            raw, _, _rest = buf.partition(b"\n")
            del buf[: len(raw) + 1]   # 原地删除，保持 self._rbuf 是唯一缓冲
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            if not line:
                continue
            try:
                self._handle_line(sock, line)
            except Exception:
                logger.exception("irc: failed to handle line %r", line[:120])
            if self._registered and deadline is not None:
                return              # 001 已到，注册完成

    # ------------------------------------------------------------------
    # 行解析与派发
    # ------------------------------------------------------------------
    def _handle_line(self, sock: socket.socket, line: str) -> None:
        prefix, command, params = _parse_line(line)
        if command == "PING":
            # token 原样回：``PING :abc`` → ``PONG :abc``
            token = params[0] if params else self.host
            self._write_line(sock, f"PONG :{token}")
            return
        if command == "PRIVMSG":
            self._handle_privmsg(prefix, params)
            return
        if command == "ERROR":
            logger.warning("irc: server ERROR: %s", " ".join(params))
            return
        if command.isdigit():
            self._handle_numeric(sock, prefix, command, params)
            return
        if command == "CAP":
            self._handle_cap(sock, params)
            return
        if command == "AUTHENTICATE":
            self._handle_authenticate(sock, params)

    def _handle_numeric(
        self, sock: socket.socket, prefix: str, code: str, params: List[str]
    ) -> None:
        if code == RPL_WELCOME:
            self._registered = True
            logger.info("irc: registered as %s (welcome from %s)", self.nick, prefix)
        elif code == ERR_NICKNAMEINUSE:
            logger.warning("irc: nick %s is in use", self.nick)
        elif code in (AUTH_SUCCESS, AUTH_FAILURE, AUTH_ABORTED):
            logger.info("irc: SASL result %s", code)
            # SASL 结束（无论成败）都要 CAP END 才能继续 NICK/USER，否则卡在注册。
            self._cap_end(sock)
        elif code == ERR_NOMOTD:
            pass  # 无 MOTD 很正常

    def _cap_end(self, sock: socket.socket) -> None:
        self._sasl_state = ""
        self._write_line(sock, "CAP END")
        self._start_registration(sock)

    def _handle_cap(self, sock: socket.socket, params: List[str]) -> None:
        """CAP 协商：只有 SASL 一条支线，其余一律 ``CAP END`` 后进入注册。"""
        caps = params[-1] if params else ""
        if self.bot_password and self._sasl_state == "ls_sent" and "sasl" in caps.lower():
            self._write_line(sock, "AUTHENTICATE PLAIN")
            self._sasl_state = "auth_sent"
        else:
            self._cap_end(sock)

    def _handle_authenticate(self, sock: socket.socket, params: List[str]) -> None:
        """服务器回 ``AUTHENTICATE +``（同意）→ 回 base64(\\\\0user\\\\0pass)。"""
        if not params:
            return
        if params[0] != "+":
            # "-" 表示服务器拒绝该机制：放弃 SASL，照常注册（不要卡在这里）。
            logger.info("irc: AUTHENTICATE rejected (%s)", params[0])
            self._cap_end(sock)
            return
        if not self.bot_password:
            return
        payload = base64.b64encode(
            b"\0" + self.nick.encode("utf-8") + b"\0" + self.bot_password.encode("utf-8")
        ).decode("ascii")
        self._write_line(sock, f"AUTHENTICATE {payload}")
        self._sasl_state = "payload_sent"

    def _handle_privmsg(self, prefix: str, params: List[str]) -> None:
        if len(params) < 2:
            return
        target, body = params[0], params[-1]
        sender = _prefix_nick(prefix)
        if not sender:
            return
        self._known_nicks.add(sender.lower())
        if self.nick and sender.lower() == self.nick.lower():
            return  # 自己发的（可能来自另一个客户端）：防回环
        is_private = bool(self.nick) and target.lower() == self.nick.lower()
        if not is_private:
            # 频道消息只有提到我们才算数，否则整个频道的话都会被当成在跟我们说。
            match = self._mention(body)
            if match is None:
                return
            text = self._strip_mention(body, match)
        else:
            text = body
        text = strip_control_codes(text)
        if not text:
            return
        # 授权闸门在最前：未授权会话的消息不许进入上层（否则能用命令/审批字绕过）
        if not self.admits(target):
            logger.info("irc: dropping message from non-whitelisted target %s", target)
            return
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(target),
                    text=text,
                    kind="text",
                    user_id=sender,
                    message_id=f"{sender}:{len(text)}",
                    platform=self.name,
                    raw=f":{prefix} PRIVMSG {target} :{body}",
                )
            )
        except Exception as exc:
            logger.exception("irc: on_inbound failed: %s", exc)

    def _mention(self, text: str) -> Optional[re.Match]:
        """在频道消息里找对我们的提及；没提到返回 ``None``。"""
        if self._mention_re is None:
            return None
        return self._mention_re.search(text or "")

    def _strip_mention(self, text: str, match: re.Match) -> str:
        """剥掉提及本身以及紧随其后的分隔符，返回真正在对本 bot 说的话。

        丢弃提及之前的内容（``hey bot, hi`` → ``hi``），并吃掉常见的分隔形态：
        ``bot:`` / ``bot,`` / ``bot!user@host``（被复述的完整 prefix）。
        """
        end = match.end()
        if end < len(text) and text[end] == "!":
            end += 1
            while end < len(text) and not text[end].isspace():
                end += 1
        while end < len(text) and text[end] in ":,":
            end += 1
        return text[end:].lstrip()

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(target: Any) -> str:
        return f"irc:{target}"

    @staticmethod
    def _target(conversation_id: Any) -> Optional[str]:
        raw = str(conversation_id or "")
        if raw.startswith("irc:"):
            raw = raw[len("irc:"):]
        return raw or None

    def _normalize_target(self, target: str) -> str:
        """裸目标补 ``#`` 前缀（频道）；**已知昵称**原样保留（私聊）。

        ``irc:alice`` 这种私聊目标如果被无脑补成 ``#alice``，回复就会变成往频道发。
        入站时把见过的 sender 记进 :attr:`_known_nicks`，就能把两者区分开。
        """
        raw = target.strip()
        if not raw or raw.startswith(("#", "&", "+", "!")):
            return raw
        if raw.lower() in self._known_nicks:
            return raw
        return f"#{raw}"

    def _body_budget(self, target: str) -> int:
        """这条消息正文可用的**字节**数。

        行形状是 ``PRIVMSG <target> :<body>\\r\\n``，开销 = ``"PRIVMSG "`` + 目标 +
        ``" :"`` 的两个字节 + ``CRLF``。少算一个字节就会发出 513 字节的行被服务器丢包，
        所以这里按真实行形状逐字节数。
        """
        overhead = len("PRIVMSG ") + len(target.encode("utf-8")) + 1 + 1 + 2
        return max(0, self.line_limit - overhead)

    def _outbound_pieces(self, text: str, target: str) -> List[str]:
        """出站分片：先按字符上限切（与其它适配器同用 split_text），再按字节兜底。"""
        budget = self._body_budget(target)
        pieces: List[str] = []
        for chunk in split_text(text, self.message_limit, prefix_fmt=""):
            pieces.extend(_byte_safe_split(chunk, budget))
        return pieces

    def _throttle(self, conversation_id: str) -> None:
        """两次 PRIVMSG 的最小间隔，避免被服务器按 flood 断开。"""
        interval = getattr(self, "min_interval", 1.0)
        with self._throttle_lock:
            last = self._last_send.get(conversation_id)
            now = time.monotonic()
            if last is None or (now - last) >= interval:
                self._last_send[conversation_id] = now
                return
            wait = interval - (now - last)
        self._stop_event.wait(min(wait, self.min_interval))

    def _next_seq(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return str(self._seq)

    def _write_line(self, sock: socket.socket, line: str) -> bool:
        """发一条以 ``CRLF`` 结尾的行。**绝不在行内出现 CR/LF**。

        硬上限兜底：即使调用方绕过 :meth:`_outbound_pieces` 塞进超长文本，也按
        字符边界截到 512 字节（宁可少发也不让服务器丢整条连接）。
        """
        line = line.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        encoded = f"{line}\r\n".encode("utf-8")
        if len(encoded) > self.line_limit:
            # 去掉 CRLF 再按字节预算裁正文，保持行首的命令/目标完整。
            room = self.line_limit - 2
            encoded = _fit_utf8(line, room).encode("utf-8") + b"\r\n"
            logger.warning(
                "irc: truncating outbound line to %d bytes (limit %d)",
                len(encoded),
                self.line_limit,
            )
        try:
            with self._send_lock:
                sock.sendall(encoded)
            return True
        except Exception as exc:
            logger.warning("irc: write failed: %s", exc)
            return False

    def send(self, out: Outbound) -> MsgHandle | None:
        """``PRIVMSG <target> :<text>``（超长按 512 字节上限切成多条）。"""
        target = self._target(out.conversation_id)
        if not target:
            logger.warning("irc: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("irc: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        target = self._normalize_target(target)
        with self._sock_lock:
            sock = self._sock
        if sock is None:
            logger.warning("irc: not connected; dropping outbound message")
            self._note_send_failure(SendError.TRANSIENT, "not connected")
            return None
        pieces = self._outbound_pieces(out.text, target)
        if len(pieces) > 1:
            logger.info("irc: splitting outbound message into %d lines", len(pieces))
        handle: MsgHandle | None = None
        for piece in pieces:
            self._throttle(out.conversation_id)
            if not self._write_line(sock, f"PRIVMSG {target} :{piece}"):
                self._note_send_failure(SendError.TRANSIENT, "write failed")
                return handle if handle is not None else None
            # IRC 没有消息 id；用本地序号占位（edit 永远 False，仅供日志定位）
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=self._next_seq(),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """IRC 没有编辑消息的能力 —— **永远返回 False**。

        ``core.py`` 拿到 False 会退化成"发一条新消息"，这正是 IRC 该有的行为。
        这里刻意不吞掉也不改写原消息：IRC 协议里没有任何"修改已发消息"的操作。
        """
        logger.debug("irc: edit unsupported; caller should send a new message")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """IRC 没有 callback query 概念 —— no-op。"""
        return None
