# 架构总览

> 本文回答"**这项目是怎么搭起来的**"。要查"做到哪一步了"看 [`../tasks.md`](../tasks.md)；
> 要查"某个平台怎么配"看 [`install.md`](install.md)；要查"为什么这样选"看
> [`platform-design-reference.md`](platform-design-reference.md)。
>
> 本文描述的是**已提交**的状态。a2a 与 Telegram 迁移正在并行进行中，未包含在内。

## 立身约束（决定了很多设计取舍）

| 约束 | 直接后果 |
|---|---|
| **Python 3.10+，仅标准库** | WebSocket 必须自研（`ws.py`）；没有 `httpx`/`websockets` 可用 |
| **纯本机运行** | 7 个 webhook-only 平台（WhatsApp官方/LINE/Teams/SMS/Synology/Zalo/Google Chat）接不进来 —— 它们需要一个可达的公网回调地址 |
| **不要求公网回调地址** | 十个平台全部走"我们主动连出去"（长轮询 / WebSocket 客户端 / IMAP 轮询） |

**这三条不是技术偏好，是筛选平台时的第一道闸门。** 违反其中任何一条的平台，
即使协议再简单也不进B 组 —— 理由见 [`../tasks.md`](../tasks.md) 的阶段 E。

## 分层

```
┌──────────────────────────────────────────────────────────────┐
│ __main__.py    CLI：--setup / --status / --check              │
├──────────────────────────────────────────────────────────────┤
│ core.py        编排：消息进出的主循环、会话映射、冻结文案真源│
├──────────────────────────────────────────────────────────────┤
│ opencode_client.py  与 opencode 服务通信（会话 id 传递）     │
├──────────────────────────────────────────────────────────────┤
│ adapters/       平台适配器：字段映射 + 事件过滤 + 出站       │
│   base.py       注册表、能力声明、授权闸门、错误分类          │
│   <platform>.py  一个平台一个文件                          │
├──────────────────────────────────────────────────────────────┤
│ transport/      传输层：连接 · 收包 · 退避重连 · 线程 · 停止 │
├──────────────────────────────────────────────────────────────┤
│ ws.py           自研 RFC 6455 WebSocket 客户端              │
├──────────────────────────────────────────────────────────────┤
│ 共用纯函数层（无状态、无 IO、易测）                           │
│   identity.py  会话标识 · split.py 分片 · status.py 状态     │
│   state.py  会话映射 · config.py 配置 · hooks.py 契约类型    │
└──────────────────────────────────────────────────────────────┘
```

### 为什么传输层在适配器**下面**

迁移之前，十个适配器**各自手写**"连接 → 收包 → 退避重连 → 线程 → 停止"这套循环，
是十份重复逻辑。于是 `stop()` 的正确顺序（先关连接再 join）要在每个文件里各写对一次——
而它恰恰是最容易写错的那一处（关一个卡在 `read()` 上的 socket 不保证唤醒那次读）。

搬进传输层之后，**这类错误从"十处机会"变成"零处机会"**。迁移是逐步进行的
（IRC → Matrix → Telegram → …），`tasks.md` 的阶段 A 记录了进度。

**这不是"抽象洁癖"。** 迁移的实测收益是**概念性**的，不是行数：IRC 那家真实代码净减
42 行，Matrix 那家**反而多了 8 行**（多线程接缝与游标拆分的成本吃掉了省下的样板）。
换来的是循环/退避/线程/停止语义**只有一份、且有独立的不变量测试**。

## 一次入站消息的完整路径

```
传输层线程
  └─ Transport._next()  →  一条原始数据（或 NOTHING = 本轮无数据）
       └─ 适配器解析/过滤       ← 平台语义在这里：事件类型、字段、回声、白名单
            └─ Adapter.admits(principal)   ★ 授权闸门，必须在产生 Inbound 之前
                 └─ hooks.on_inbound(Inbound(...))
                      └─ core.py：查/建会话映射（state.py）
                           └─ opencode_client：把文本发给 opencode 会话
                                └─ 回复回到 Adapter.send() / .edit()
```

**顺序是有意义的，改动时不要挪：** `admits()` 之前不做任何解析后的业务动作，
否则被拒绝的消息已经产生了副作用。

## 一次出站消息的完整路径

```
opencode 回复 → core.py 按 conversation_id 找到适配器
  └─ split.split_text(text, max_message_length, prefix_fmt="")
       └─ Adapter.send(Outbound) → 逐片发送
            ├─ 成功 → MsgHandle(conversation_id, message_id, platform)
            └─ 失败 → _note_send_failure(SendError.X, detail)
       └─ 长任务进度更新：Adapter.edit(handle, out) → bool
```

`edit()` 返回 `False` 表示**该平台没有编辑能力**，core 会退化成"再发一条新消息"。
这是诚实降级，不是失败 —— 十平台里IRC / Twitch / ntfy / email 都没有编辑能力。

## 模块职责

| 文件 | 行数 | 职责 | 不该出现在这里的东西 |
|---|---|---|---|
| `hooks.py` | 122 | 契约类型：`Inbound`/`Outbound`/`MsgHandle`/`SendError`/`SendResult` | 任何 IO |
| `identity.py` | 241 | `platform:local_id` 的格式化、解析、校验、旧格式归一 | 任何平台特判 |
| `split.py` | 294 | 码点计长、组合序列原子切分、断点优先级、`（i/n）` 前缀两遍法 | 平台知识 |
| `state.py` | 136 | `conversation_id ↔ session_id` 映射、落盘 | 平台知识 |
| `status.py` | 436 | 状态四态归一、JSON 往返、表格渲染 | 平台知识 |
| `transport/base.py` | 395 | 线程、指数退避、**先关连接再 join**、`reset_after` | 任何平台知识 |
| `transport/polling.py` | 94 | HTTP 短轮询/长轮询 | — |
| `transport/websocket.py` | 119 | WebSocket 事件流 | — |
| `transport/tcp_lines.py` | 200 | TCP 行协议 | — |
| `transport/queue.py` | 112 | `EventQueue`（批量 fetch → 一次一条）+ `NOTHING` | — |
| `ws.py` | 534 | RFC 6455 客户端：握手三重校验、客户端掩码、分片、控制帧 | 平台知识 |
| `adapters/base.py` | 341 | 注册表、`capabilities()`、`admits()`、`classify_http`、`send_result()` | 循环/线程 |
| `core.py` | 1299 | 编排、会话映射、`_SETUP_GUIDES`/`SETUP_MENU_TEXT`（**冻结文案真源**） | 平台协议细节 |
| `__main__.py` | 483 | CLI | — |

## 关键不变量

**这一节是本文最有价值的部分** —— 每一条都是踩过坑之后定下的，改动时请先读懂再动手。

### 契约类

1. **`outbound_tokens ⊆ required_tokens`** —— "能发出去"的前提一定是"已配置"的子集。
   违反它就会出现"配得完全正确却被判成没配"。
2. **能力必须显式声明，禁止谎报** —— `supports_media=False` 不许偷偷发图片。
   调用点据 `capabilities()` 决策，所以谎报会直接导致运行时行为错误。
3. **`admits()` 必须在产生 `Inbound` 之前** —— 被拒绝的消息不得有任何副作用。
4. **`edit()` 诚实返回 `bool`** —— `False` = 该平台无此能力，core 退化成发新消息。
   不许假装成功。

### 会话标识

5. **统一 `platform:local_id`** —— `StateStore` 拿 `conversation_id` 当**不透明键**，
   所以历史上 `channel:` 被 slack/discord/mattermost **三家共用**，两个平台出现相同
   local id 就会**共用同一个会话**（用户在 A 平台的对话串到 B 平台）。
6. **歧义前缀绝不猜** —— `channel:` 无法从字符串判断来源，缺 `platform_hint` 时
   `normalize()` **抛错**。猜错的后果是把用户映射到别人的会话，不报错、只表现为
   "agent 突然记错上下文"，比直接失败难查得多。
7. **前缀切换必须与 `state.json` 键迁移一起发** —— 切前缀会让已落盘的键全部变孤儿，
   用户一次性丢失会话映射。已完成的迁移（irc/twitch/nextcloud）**字节级不变**，
   因为它们的前缀本就映射到自身；`chat:`/`room:`/`channel:` 尚待此步。

### 传输层

8. **`reset_after=0` = 连上即重置** —— 默认值必须是 0。改成"稳定存活 N 秒才重置"
   会让网络闪断时退避一路涨到上限，**与迁移前的既有行为不一致**。
9. **先关连接再 join** —— 否则 `stop()` 会白等一个完整超时。
10. **游标先推进再分发**（at-most-once）—— 单条处理抛异常不应导致重放死循环。
    与 telegram `offset` / matrix `next_batch` 同取舍。

### 与外部系统打交道

11. **不改用户的既有状态** —— email 用 `BODY.PEEK[]` 而非 `BODY[]`（后者隐式设
    `\Seen`，等于替用户把邮件标已读）；matrix 不用 `UNSEEN`（flag 会和人工读信冲突）。
12. **防回环只用"发布者不可控"的字段** —— ntfy 用自定义 `tags`、email 用
    `Message-ID` + Subject 前缀；**绝不**用 `title`/`From` 这类发布者可控字段推身份
    （任何人可以自称同样的 title）。真正的信任边界要靠平台侧的权限控制。
13. **失败必须可观测** —— `_note_send_failure(SendError.X, detail)`，不许静默吞。
    `--status` 与日志据此显示哪个平台哪次失败、属哪一类。
14. **上限的单位必须确认是字节还是字符** —— ntfy 的 4096 是**字节**（受 FCM/APNS 约束），
    中文一字 3 字节，所以 `text[:4096]` 会把中文切成半个字符、编码即报错。

### 测试

15. **断言"等了多久"一律优先断言内部状态** —— wall-clock 差分只允许做**下界**
    断言（等待只会比标称**更长**，绝不会更短）。这已经栽坑三次：transport reset 用例、
    IRC 退避、Matrix 迁移中发现的 flaky。
16. **协议事实先查证再写** —— 本项目多处凭记忆会写错（Slack 按钮实名、Discord
    `heartbeat_interval` 单位是毫秒、Nextcloud `timeout` 上限文档写 60 实为 30、
    Matrix 房间 id 含冒号会炸掉"禁冒号"的校验）。**查不到就别写，或明确标注不确定。**

## 加一个平台有多贵

见 [`adding-a-platform.md`](adding-a-platform.md)。结论：适配器本体约 150~380 行，
其中**零行**是连接/重连/线程/停止代码。