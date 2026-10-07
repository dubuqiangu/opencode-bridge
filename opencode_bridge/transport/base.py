"""传输层基类：生命周期 + 退避重连 + 线程 + 停止（**零业务语义**）。

为什么要有这一层
----------------
仓库支持 8 个平台（Telegram / Slack / Discord / Matrix / Mattermost / IRC /
Twitch / Nextcloud Talk），而每个适配器当初都**自己手写了一遍**同样的循环：
「连上 → 注册 → 读 → 退避重连 → 停止」，并且都踩过同一批坑：

* ``stop()`` 里必须**先关连接再 join**。反过来（先置停止位再 join）时，基类
  ``join(5s)`` 会等满 5 秒，而读循环还阻塞在 ``recv()`` 上（WS 30s / IRC 30s）。
* 退避要么忘记封顶、要么忘记在连接稳定后重置 —— 一次网络抖动能把后续重连
  越推越久（60s、120s…），网络恢复后也缓不过来。
* 用户回调（``on_event``）抛异常会直接把消费线程带走。

这些**与业务无关的样板**收敛到这里，适配器只需实现 ``_open`` / ``_next`` 两个
方法（外加可选的 ``_on_open`` / ``_on_close`` 钩子），就能拿到一整套正确的
连接管理。授权闸门、防回环、系统消息过滤、字段映射**仍然全部归适配器** ——
传输层不该知道消息平台的存在（本模块刻意不 import ``opencode_bridge.adapters``）。

线程模型
--------
``start()`` 起一个 daemon 线程，串行执行：``_open()`` → ``_on_open()`` →
反复 ``_next()``；任何异常都会关掉连接、退避、从 ``_open()`` 重新开始。
``_next()`` 的返回值**原样**交给 ``start()`` 时传入的回调，基类不解释、不过滤。

两条"约定俗成"的返回值（都在本模块定义，因为它们是基类的契约）：

* :data:`NOTHING` —— "这轮暂时没有"。轮询类（:mod:`.polling`）的常态；
  收到它基类**不会**调用回调，而是按 :meth:`Transport._idle_delay` 等一会儿再问。
  为什么不用 ``None``：``None`` 已经被 ``_next`` 的约定占用（长连接类靠它表示
  对端已关闭），而且"没有数据"与"连接结束"必须能区分开。
* :class:`ReconnectNow` —— 在 ``_next`` / ``_on_open`` / ``on_message`` 里抛它，
  表示"这次连接**主动**作废，但不要退避，立刻重连"（Slack Socket Mode 服务端
  下发 ``disconnect`` 就是这种语义）。

周期钩子
--------
"没有入站数据时也要定期做点事"这件事（Discord 心跳、Twitch 保活与注册超时
判定）以前每家都在适配器里**自己写一个线程或子类**（见
:mod:`opencode_bridge.adapters.irc` 的 ``_IrcTransport._tick``），于是同一份机制
会有三份。现在它是传输层的一等能力：``on_tick`` + ``tick_interval``，两种触发
源，语义分别是：

* ``tick_interval <= 0``（**循环驱动**）：基类在**每次取下一条数据之前**调一次。
  IO 超时也算一轮 —— :mod:`.tcp_lines` 把读超时表达成 :data:`NOTHING`，所以
  ``io_timeout`` 到了就会重新问一轮、也就重新 tick。**这与 IRC 迁移前的子类
  逐项等价**，IRC 因此保持原样不动（见 ``tests/test_transport.py`` 的
  ``TestPeriodicHook``）。
* ``tick_interval > 0``（**定时驱动**）：起一个 daemon 定时线程，每
  ``tick_interval`` 秒调一次，**只在有活动会话时调**。给"``_next`` 可能长时间
  阻塞、循环自己转不起来"的长连接用 —— Discord 的 WS 读要阻塞整整 60s，而心跳
  周期是 41~45s：靠循环驱动的话心跳会被拖到 60s 一次（> 服务端容忍的 1.25 倍），
  连接会被直接判死。``tick_interval`` **可在运行期调整**（用
  :meth:`Transport.tune_tick_interval` 会立刻唤醒定时线程），所以适配器能在协商到
  周期之后把粒度收紧。

两种模式下钩子抛异常都**不许**杀死循环：记进 ``stats()["errors"]`` 后继续。
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from typing import Any, Callable

__all__ = ["Transport", "ReconnectNow", "NOTHING", "MIN_BACKOFF_SECONDS"]

logger = logging.getLogger("opencode_bridge.transport.base")


#: ``min_backoff`` 的**正下限**（秒）。⛔ ``0`` 不是合法下限：退避为 0 时
#: :meth:`Transport._run` 每次会话失败都「不等就重连」，而**下一次等待仍是 0**
#: ⇒ 紧循环空转。实测（``min_backoff=0``、``_open`` 立即失败）**7.5~8.0 万次
#: 重连/秒、CPU 秒 ≈ 墙钟秒**；而 ``min_backoff=1.0`` 是 4.03 s 内**恰好 5 次**。
#:
#: **取值依据（三条，全部可复核）**：
#:
#: 1. **它是仓库里最小的一个真实取值**：生产侧九个适配器传的全是
#:    ``1.0 / 2.0 / 3.0 / 5.0``（模块常量），而测试侧 24 处传 ``0.01``
#:    ⇒ 取 ``0.01`` 意味着「**只有仓库里真的有人在用的值**能通过」。
#: 2. **同子系统已有同款先例**：:mod:`.tcp_lines` 对另一个时间类旋钮用的正是
#:    ``max(0.01, float(io_timeout))`` ⇒ 「时间类旋钮的下限定在 10 ms 量级」
#:    这件事在本层不是新发明。
#: 3. ⛔ **刻意不取 1.0**（哪怕它是生产侧的最小值）：实测取 1.0 会钳住 28 个
#:    既有测试实参、**两条既有用例变红**，套件从 3.6 s 涨到 27.6 s；更糟的是
#:    那两条断言的正是「退避 ×2 增长后被重置」的**数列**，下限 1.0 会让它们的
#:    ``min_backoff`` 追上 ``max_backoff`` ⇒ 数列退化成常数 ⇒ **失去覆盖**。
#:    「下限」要解决的是「``0`` 让重连循环无节流」这**一个**缺陷，
#:    ⛔ 不顺带发明一条限流策略 —— 那是产品决策、不是缺陷修复。
#:
#: ⚠️ **只改这一个旋钮**：``reset_after``（``0`` = 只要连上过就重置）与
#: ``tick_interval``（``<= 0`` = 循环驱动）的 ``0`` **各自都有文档化的含义**
#: （见 :meth:`Transport.__init__` 的参数说明）⇒ ⛔ 不许跟着一起改成正下限 ——
#: 那会改掉既有行为。
MIN_BACKOFF_SECONDS = 0.01


# ----------------------------------------------------------------------
# 契约里的两个特殊值
# ----------------------------------------------------------------------
class ReconnectNow(Exception):
    """抛它 = 立刻重连、**跳过**这次退避等待。

    典型用途：Slack Socket Mode 服务端下发 ``disconnect``、Discord 要求换
    resume-gateway —— 这类"服务端主动要求换连接"不是故障，不该被当成失败
    指数退避（否则用户会看到 5s→10s→20s 的莫名延迟）。在 ``on_message`` /
    ``_next`` / ``_on_open`` 里抛本异常，基类立即进入下一次 ``_open()``。

    在 ``_on_close`` 里抛也会被识别；其他位置的异常一律按"连接出错"退避。
    """


class _NothingSentinel:
    """「这轮没有」的哨兵类型（见 :data:`NOTHING`）。**单例**。"""

    _instance: "_NothingSentinel | None" = None

    def __new__(cls) -> "_NothingSentinel":
        # 单例：``type(NOTHING)() is NOTHING`` 恒成立，适配器误建也不会出两个。
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<NOTHING>"

    def __bool__(self) -> bool:
        return False


#: ``_next()`` 可以返回它表示"暂时没有新事件"（见模块 docstring）。
NOTHING: Any = _NothingSentinel()


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------
class Transport(abc.ABC):
    """传输层：只管"怎么持续拿到原始事件"与生命周期，**完全不管业务语义**。

    授权闸门、防回环、系统消息过滤、字段映射 —— 全部归适配器；这里只负责
    连接、重连、线程、停止。这样"加一个平台"不必再写一遍这些样板。

    基类保证的不变量（每条都有用例锁住，见 ``tests/test_transport.py``）：

    1. :meth:`stop` **先关连接再 join 线程**（顺序不能反，否则每次 stop 都白等
       满超时 —— 8 个适配器都踩过这个坑）。
    2. 指数退避（``min_backoff`` → ``×2`` → ``max_backoff`` 封顶）；某次连接
       **存活超过** ``reset_after`` 则退避重置回下限（一次偶发抖动不该把后续
       重连越推越久）。
    3. ``_open`` / ``_next`` / ``_on_open`` / ``_on_close`` 抛出的异常**不得逃出
       线程**，只记 log。
    4. 用户回调 ``on_event`` 抛异常**不得杀死消费循环**（记 log 后继续）。
    5. :meth:`start` 与 :meth:`stop` 都幂等（不会起第二个线程 / 不会二次崩）。
    6. 退避等待可被 :meth:`stop` **立即打断**（用 ``Event.wait``，不是 ``sleep``）。
    7. :meth:`stop` 的顺序是**停周期钩子 → 关连接 → join 消费线程**（顺序与第 1 条
       同源：适配器的心跳/保活必须先停，否则关连接时它还会往一条正在关的连接上写）。

    :param name: 日志前缀（给适配器传平台名即可，便于按平台过滤日志）。
    :param min_backoff: 首次失败后的等待秒数，也是重置后的下限。
        ⛔ **下限是正数**（:data:`MIN_BACKOFF_SECONDS`）：传 ``0`` 或更小的值会被
        钳到那个下限并打一条 WARNING —— 退避为 0 会让重连紧循环空转。
    :param max_backoff: 等待上限（封顶后不再增长）。
    :param reset_after: 连接存活超过这么久就认为"稳定"，退避重置回下限。
        ⚠️ 与 ``min_backoff`` 不同，**这里的 ``0`` 是合法的**（= 只要连上过就重置，
        既有语义，见下面那句注释）。
    :param on_tick: 可选的**周期钩子**（无参可调用）。见模块 docstring「周期钩子」。
        ``None`` = 完全关闭（不额外起线程、循环里只有一次 ``is None`` 判断）。
    :param tick_interval: 周期钩子的间隔秒数。``<= 0`` = 循环驱动（每次取下一条
        数据之前调一次，IO 超时也算）；``> 0`` = 定时驱动（另起一个 daemon 定时
        线程，运行期可用 :meth:`tune_tick_interval` 调整）。
    """

    def __init__(
        self,
        *,
        name: str = "",
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        reset_after: float = 0.0,
        on_tick: Callable[[], None] | None = None,
        tick_interval: float = 0.0,
    ) -> None:
        if on_tick is not None and not callable(on_tick):
            raise TypeError("on_tick 必须可调用")
        self.name = str(name or "")
        # 夹逼一下，免得 min > max 这种手滑配置让退避第一次就"封顶"在错误值上。
        #
        # ⚠️ **下界是正数**（:data:`MIN_BACKOFF_SECONDS`），⛔ 不是 0：
        # 退避为 0 ⇒ :meth:`_run` 每次失败都「不等就重连」、下一次等待**仍是 0**
        # ⇒ 紧循环空转（实测 7.5~8.0 万次/秒、CPU 打满）。详见该常量。
        # ⚠️ 告警**只打在显式传了非法值时** —— 生产侧九个适配器传的全是
        # 1.0/2.0/3.0/5.0 ⇒ 一次都不会触发；而「每次构造都喊一遍」会让真正
        # 需要看的那条被淹掉。
        self.min_backoff = max(MIN_BACKOFF_SECONDS, float(min_backoff))
        if float(min_backoff) < MIN_BACKOFF_SECONDS:
            logger.warning(
                "transport[%s]: min_backoff=%r 小于下限 %r，已按 %r 处理"
                "（退避为 0 会让重连紧循环空转）",
                self.label, min_backoff, MIN_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS,
            )
        self.max_backoff = max(self.min_backoff, float(max_backoff))
        # 默认 0 =「只要连上过就重置退避」——这是迁移前 8 个适配器的既有语义，
        # 默认取它才能保证迁移行为不变。传正数才启用"需稳定存活 N 秒"的保守模式。
        self.reset_after = max(0.0, float(reset_after))

        # --- 周期钩子（见模块 docstring「周期钩子」）---------------------
        self.on_tick = on_tick
        #: 公开属性：定时驱动模式下适配器可在运行期收紧粒度（每轮重读）。
        self.tick_interval = max(0.0, float(tick_interval))
        # 两种触发源在这里定型：谁也不许在运行期换模式（换模式必须重建传输层，
        # 否则 stop() 的"先停钩子"顺序会与实际模式对不上）。
        self._loop_ticks = on_tick is not None and self.tick_interval <= 0.0
        self._timer_ticks = on_tick is not None and self.tick_interval > 0.0
        self._tick_stop = threading.Event()
        #: 只用来"提前唤醒"定时线程（调粒度 / 收工），本身不携带状态。
        self._tick_wake = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._last_tick = 0.0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._conn_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._on_event: Callable[[Any], None] | None = None
        #: 下一次要等的退避秒数（只被消费线程读写）。
        self._backoff = self.min_backoff
        #: 可观测计数（给测试与 /status 用；不是业务指标）。
        self._stats: dict[str, int] = {
            "connects": 0,     # _open 成功的次数
            "sessions": 0,     # 会话结束次数（不含仍在跑的）
            "events": 0,       # 交给 on_event 的条数
            "errors": 0,       # 被吞掉的异常次数
            "idle": 0,         # 收到 NOTHING 的次数
            "ticks": 0,        # 周期钩子实际被调用的次数
        }

    # --- 属性 ----------------------------------------------------------
    @property
    def label(self) -> str:
        """日志前缀：``name`` 优先，否则用类名。"""
        return self.name or type(self).__name__

    @property
    def thread_name(self) -> str:
        return f"transport:{self.label}"

    @property
    def tick_thread_name(self) -> str:
        """周期钩子定时线程名（仅 ``tick_interval > 0`` 时存在）。"""
        return f"transport-tick:{self.label}"

    @property
    def running(self) -> bool:
        """消费线程是否活着。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def connection(self) -> Any:
        """当前连接对象（**未连接时为 ``None``**）。

        适配器发消息时用得上（Slack 要 ack 某条消息、IRC 要发 ``PRIVMSG``）。
        线程安全：与消费线程的赋值用同一把锁。
        """
        with self._conn_lock:
            return self._conn

    def stats(self) -> dict[str, int]:
        """计数快照（拷贝，可安全读取）。"""
        return dict(self._stats)

    # --- 生命周期（基类统一实现，勿覆写）-------------------------------
    def start(self, on_event: Callable[[Any], None]) -> None:
        """起 daemon 线程消费事件。**幂等**：已在跑时直接返回，不起第二个线程。

        线程里任何一个异常都不会传播到这里（不抛给调用方），与"适配器启动
        失败不应拖垮进程"的既有约定一致 —— 启动失败会以退避重试的形式留在
        日志里。停过一次之后可以再次 ``start()`` 重启。
        """
        if not callable(on_event):
            raise TypeError("start() 需要一个可调用的 on_event")
        with self._lifecycle_lock:
            current = self._thread
            if current is not None and current.is_alive():
                logger.debug("transport[%s]: 已在运行，start() 忽略", self.label)
                return
            self._on_event = on_event
            self._stop_event.clear()
            self._backoff = self.min_backoff
            if self._timer_ticks:
                self._tick_stop.clear()
                tick_thread = threading.Thread(
                    target=self._tick_loop, name=self.tick_thread_name, daemon=True
                )
                self._tick_thread = tick_thread
                tick_thread.start()
            thread = threading.Thread(
                target=self._run, name=self.thread_name, daemon=True
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """**停周期钩子 → 关连接 → join 消费线程**（顺序不能反）。**幂等**。

        顺序为什么重要：消费线程多半阻塞在 ``_next()`` 里（``recv()`` 可能要
        30s 才超时），而 ``join`` 只等 ``timeout``（默认 5s）。不先把连接关掉、
        把那个阻塞唤醒，每次 stop 都会白等满 5 秒。

        周期钩子排在最前面，是因为适配器的心跳/保活会往 ``connection`` 上写 ——
        连接正在被关时还写，会抛异常、也会把"我们主动关的"记成"发失败"。
        这正是 Discord ``stop()`` 的「停心跳 → 关 WS → join」三段式，现在由基类
        统一保证。

        从消费线程内部调用是安全的（不会 join 自己；定时线程照常被停掉）。
        """
        self._stop_event.set()
        self._stop_ticking()               # ① 先停周期钩子（不等它发完一拍）
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            self._close_conn(conn)        # ② 让阻塞中的 _next 立刻返回
        with self._lifecycle_lock:
            thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)          # ③ 最后才 join

    # --- 子类实现这三个（每次重连调一遍）-------------------------------
    @abc.abstractmethod
    def _open(self) -> Any:
        """建立一次连接。失败应抛异常（基类会退避后重试）。

        返回的连接对象会被 :attr:`connection` 暴露出来，并在会话结束时交给
        :meth:`_close_conn`。允许返回 ``None``（轮询类没有长连接）。
        """

    @abc.abstractmethod
    def _next(self, conn: Any) -> Any:
        """从连接取**下一条**原始事件。

        * 返回值原样交给 ``on_event``（不解释、不过滤）
        * 抛异常 = 这次连接出问题 → 基类关连接、退避、重连
        * 返回 :data:`NOTHING` = 暂时没有（轮询类）；**WebSocket / TCP 类应
          阻塞到真有事件**，不要用"轮询式返回 NOTHING + 短睡眠"来假装长连接
        * 抛 :class:`ReconnectNow` = 立刻重连、不退避
        """

    # --- 可选钩子 ------------------------------------------------------
    def _on_open(self, conn: Any) -> None:
        """连接建立后（发登录 / Identify / CAP 协商等）。默认 no-op。

        抛异常 = 这次连接失败（基类会关连接并退避重连）——"连上但注册不成功"
        就该重连，而不是卡在那里。
        """

    def _on_close(self, conn: Any) -> None:
        """连接关闭前清理。**必须不抛异常**（基类兜底吞掉并记 log）。"""

    def _close_conn(self, conn: Any) -> None:
        """真正把连接关掉。**必须不抛异常**。

        默认只调 ``conn.close()``。子类若有阻塞中的 ``recv()``（WS / TCP）应
        覆写成"先 ``shutdown`` 唤醒读、再 ``close``"（见 :mod:`.websocket`）。
        注意 ``stop()`` 与会话结束都会调本方法，因此必须**幂等**。
        """
        close = getattr(conn, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - 关闭路径不许抛
            logger.debug("transport[%s]: 关闭连接失败（忽略）: %s", self.label, exc)

    def _idle_delay(self) -> float:
        """收到 :data:`NOTHING` 后要等的秒数（0 = 立刻再问一次）。

        基类返回 0（长连接类阻塞在 ``_next`` 里，不该有空转）；轮询类覆写。
        等待用 ``Event.wait``，所以 :meth:`stop` 能立即打断。
        """
        return 0.0

    # --- 消费循环（子类不用碰）----------------------------------------
    def _run(self) -> None:
        """消费线程主体：连接 → 消费 → 关连接 → 退避 → 重连。"""
        try:
            while not self._stop_event.is_set():
                conn: Any = None
                opened_at: float | None = None
                immediate = False
                try:
                    conn = self._open()
                    opened_at = time.monotonic()
                    self._stats["connects"] += 1
                    with self._conn_lock:
                        self._conn = conn
                    if self._stop_event.is_set():
                        break          # stop() 刚跑过；别再往这条连接上登录
                    self._on_open(conn)
                    if self._stop_event.is_set():
                        break
                    self._pump(conn)
                except ReconnectNow as exc:
                    logger.info("transport[%s]: 立即重连: %s", self.label, exc)
                    immediate = True
                except Exception as exc:  # noqa: BLE001 - 异常不许逃出线程
                    self._stats["errors"] += 1
                    logger.warning("transport[%s]: 会话出错: %s", self.label, exc)
                finally:
                    with self._conn_lock:
                        if self._conn is conn:
                            self._conn = None
                    if conn is not None:
                        immediate = self._safe_on_close(conn) or immediate
                        self._close_conn(conn)

                if self._stop_event.is_set():
                    break
                self._stats["sessions"] += 1
                lived = time.monotonic() - opened_at if opened_at is not None else 0.0
                wait = 0.0 if immediate else self._next_backoff(
                    survived=lived >= self.reset_after
                )
                if wait > 0 and self._stop_event.wait(wait):
                    break              # 退避期间被 stop 打断
        finally:
            with self._lifecycle_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _pump(self, conn: Any) -> None:
        """反复 ``_next`` 并派发，直到连接出问题或被 stop。"""
        while not self._stop_event.is_set():
            if self._loop_ticks:           # 未配置钩子时只是一次布尔判断
                self._fire_tick()
            item = self._next(conn)
            if item is NOTHING:
                self._stats["idle"] += 1
                idle = self._idle_delay()
                if idle > 0 and self._stop_event.wait(idle):
                    return
                continue
            self._dispatch(item)

    def _dispatch(self, item: Any) -> None:
        """把事件交给用户回调；**回调抛异常不许杀死循环**。"""
        self._stats["events"] += 1
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(item)
        except Exception as exc:  # noqa: BLE001 - 用户的解析 bug 不该断连
            logger.warning(
                "transport[%s]: on_event 回调抛出异常（已忽略，继续消费）: %s",
                self.label,
                exc,
            )

    # --- 周期钩子（子类不用碰）------------------------------------------
    def _fire_tick(self) -> None:
        """跑一次周期钩子，并把异常**关进笼子**。

        钩子抛异常 = 这一次周期没做成，**不许**因此终止会话或杀死消费线程：
        记进 ``stats()["errors"]`` 后继续下一轮。（与 IRC 迁移前的 ``_tick`` 不同 ——
        那个钩子靠抛异常表达"本次会话作废"，语义不同，所以 IRC 保留了它自己的接缝。）

        定时驱动模式额外做一次间隔闸门：定时线程本身已经按 ``tick_interval`` 睡过，
        这一次判断只是让"运行期调小间隔"立即生效（等下一拍就行，不必等下一轮）。
        """
        hook = self.on_tick
        if hook is None:                  # 零开销路径：没配钩子时直接返回
            return
        if self._timer_ticks:
            now = time.monotonic()
            if now - self._last_tick < self.tick_interval:
                return
            self._last_tick = now
        self._stats["ticks"] += 1
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - 钩子的 bug 不该断连
            self._stats["errors"] += 1
            logger.warning(
                "transport[%s]: on_tick 钩子抛出异常（已忽略，继续消费）: %s",
                self.label,
                exc,
            )

    def _tick_loop(self) -> None:
        """定时驱动的周期钩子线程（仅 ``tick_interval > 0`` 时存在）。

        两点与循环驱动的差别：

        1. **只在有活动会话时调**：退避期间 ``connection`` 是 ``None``，这时调钩子
           会让适配器对着一条已经关掉的连接做保活/心跳（写出错、还刷无意义的告警）。
        2. **每轮重读 ``tick_interval``**：适配器可以在协商到周期之后（心跳 41s）
           把粒度收紧到周期的一小部分（否则心跳会晚最多一整个 ``tick_interval``）。
           用 :meth:`tune_tick_interval` 还能**立刻**唤醒，不必睡满当前那一拍。

        等待用 ``Event.wait``，所以 :meth:`stop` 能立即打断（不必等满一拍）。
        """
        try:
            while not self._tick_stop.is_set():
                self._tick_wake.clear()
                if self._tick_wake.wait(max(0.001, self.tick_interval)):
                    continue         # 被 tune_tick_interval / stop 唤醒 → 重读粒度或退出
                if self.connection is None:
                    continue
                self._fire_tick()
        finally:
            with self._lifecycle_lock:
                if self._tick_thread is threading.current_thread():
                    self._tick_thread = None

    def tune_tick_interval(self, interval: float) -> None:
        """运行期调整周期钩子的粒度，并**立刻**唤醒定时线程。

        为什么必须唤醒：定时线程正阻塞在 ``Event.wait(当前粒度)`` 里，光改属性要等
        它睡满那一拍才生效。适配器拿到协商值之前的初始粒度往往很粗（Discord 是
        5s），第一次心跳因此会被拖掉整整 5s —— 那正是"周期看起来慢一倍"的错觉来源。

        循环驱动模式（``tick_interval <= 0``）下本方法只改属性、不唤醒（那个模式
        根本没有等待）。
        """
        self.tick_interval = max(0.0, float(interval))
        if self._timer_ticks:
            self._tick_wake.set()

    def _stop_ticking(self, timeout: float = 1.0) -> None:
        """停掉周期钩子线程并 join（幂等；未配置钩子时是 no-op）。

        ⚠️ 必须**两个**事件都置位：定时线程阻塞在 ``self._tick_wake.wait(粒度)`` 上，
        只置 ``_tick_stop`` 唤不醒它 —— 于是 ``join`` 会白等满 ``timeout``，而那个
        线程还会在关连接之后继续往一条正在关的连接上写（生产里粒度是 5s，所以这是
        "stop() 之后还会冒出一拍心跳"）。置上 ``_tick_wake`` 才能立刻唤醒。
        """
        self._tick_stop.set()
        self._tick_wake.set()
        with self._lifecycle_lock:
            thread = self._tick_thread
            self._tick_thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)

    # --- 退避 ----------------------------------------------------------
    def _next_backoff(self, *, survived: bool) -> float:
        """返回"下次重连要等几秒"，并推进退避状态机。

        这是一个**纯状态机**（不睡眠、不碰线程），单独可测：

        * ``survived=False``：本次等当前退避，然后 ``×2`` 封顶 →
          ``min, 2min, 4min, …, max, max…``
        * ``survived=True``：**本次就**只等 ``min_backoff``，状态也重置回下限。

        ⚠️ ``survived`` 由**调用点**判定（见 :meth:`_run`：
        ``survived = lived >= self.reset_after``），本方法只看这个布尔值。
        所以 ``reset_after`` 的语义体现在调用点，不在这里：

        - ``reset_after <= 0``（**默认**）→ ``lived >= 0`` 恒真 → **只要连上过就重置**。
          这与迁移前 8 个适配器一致（见 ``irc.py`` 的「连上过一次就重置退避」），
          **默认取它才能保证迁移行为不变**。
        - ``reset_after > 0`` → 要求本次连接稳定存活 ≥ 该秒数才重置，避免
          "连上 5s 就掉"被误判为健康而反复猛重连。**更保守，但不是现状**；
          迁移某适配器时不要顺手改成正数，那构成行为变更。

        ``ReconnectNow`` 触发的立即重连不经过本方法。
        """
        if survived:
            wait = self.min_backoff
            self._backoff = self.min_backoff
        else:
            wait = self._backoff
            self._backoff = min(self._backoff * 2.0, self.max_backoff)
        return wait

    def _safe_on_close(self, conn: Any) -> bool:
        """调 :meth:`_on_close` 并兜底异常。返回 ``True`` 表示要求立即重连。"""
        try:
            self._on_close(conn)
        except ReconnectNow:
            return True
        except Exception as exc:  # noqa: BLE001 - 清理路径不许崩
            self._stats["errors"] += 1
            logger.warning("transport[%s]: _on_close 抛出异常（已忽略）: %s",
                           self.label, exc)
        return False