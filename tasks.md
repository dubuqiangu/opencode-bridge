# opencode-bridge 路线图 · tasks.md

> 本文件是**执行台账**：每完成一项就地勾选并追加进度日志。
>
> **本文件是"做到哪一步了"，不是"这项目长什么样"。** 想理解架构看
> [`docs/architecture.md`](docs/architecture.md)（分层、数据流、以及**踩坑总结出的关键不变量**）；
> 想**加一个新平台**看 [`docs/adding-a-platform.md`](docs/adding-a-platform.md)（八步 + 坑清单）。
> 设计依据见 [`docs/platform-design-reference.md`](docs/platform-design-reference.md)（Hermes / dsh-im-gateway 三方对比）。
> 扩展前稳定点：tag `backup/pre-platform-expansion-20261002`（`06a7d2f`）。

## 目标

**融合两个参考项目的平台覆盖面**（Hermes 22 个 + dsh-im-gateway 22 个，取并集 ≈ 31 个），
每个平台按**难度由易到难**实现，整体架构足够清晰完整，使"再加一个平台"变成低成本的声明式工作。

参考项目的平台清单（权威来源已核对，非记忆）：
- **Hermes**（`hermes-agent/plugins/platforms/`，每家带 `plugin.yaml`）：a2a / buzz / dingtalk / discord / email / feishu / google_chat / homeassistant / irc / line / matrix / mattermost / ntfy / photon / raft / simplex / slack / sms / teams / telegram / wecom / whatsapp
- **dsh-im-gateway**（`ref-dsh-im-gateway/src/channels/`）：discord / irc / line / matrix / mattermost / msteams / nextcloud / googlechat / nostr / qqbot / signal / slack / synology / telegram / twitch / wechat / wecom / whatsapp / zalo / imessage / feishu / dingtalk

**关键约束（本项目的立身之本）**：Python 3.10+ **仅标准库**、**纯本机运行**、不要求公网回调地址。

## 难度口径

| 档 | 含义 |
|---|---|
| **S** | 纯函数或局部改动，不破坏现有行为 |
| **M** | 跨文件 + 新抽象 + 测试 |
| **L** | 破坏性迁移或跨层状态机 |

---

## 架构 · 让"加平台"降为声明式工作

已完成的地基：能力显式声明（`capabilities()`）、统一授权闸门（`admits()`）、
平台注册表**动态发现**（`registered_names()`，新增平台不改核心文件）、
凭据声明（`required_tokens` / `outbound_tokens`，且有 `outbound ⊆ required` 不变量）。

**还缺的、决定了后续加平台成本的三件事**（阶段 A 补齐）：

| # | 缺口 | 现状 | 目标 |
|---|---|---|---|
| A1 | **传输层抽象** | 每个适配器各自手写"连接 + 收包 + 退避重连 + 停止"循环（8 份重复逻辑） | 抽出可复用 transport：HTTP 长轮询 / HTTP 短轮询 / WebSocket / TCP 行协议。基类统一生命周期、指数退避、**stop 语义（先关连接再 join，否则每次 stop 等满超时）** |
| A2 | **会话标识不统一** | 逐平台临时约定：`chat:` / `channel:` / `room:` / `irc:` / `nextcloud:` 各不相同，且没有解析与校验入口 | 统一为 `platform:local_id`；提供 `format` / `parse` / `platform_of` / 合法性校验，并**向后兼容**已写入 `state.json` 的旧格式 |
| A3 | **缺 inbound-push（webhook）入口** | 架构只有 outbound（我们主动连/拉），所以**7 个 webhook-only 平台根本接不进来**（line / teams / sms / synology / zalo / google_chat / whatsapp官方）——而 Hermes（`webhook.py` + `shared_ingress.py`）与 dsh（各 channel 起本地 HTTP server）**两家都有这条路径** | 单端口 HTTP 服务 + 按路径路由到各适配器；平台侧只需声明"我的 webhook 路径 + 我要验签/解密"，传输与生命周期由基类管 |

**加一个平台的目标形态**：写一个 ~150 行适配器（凭据键 + 能力 + 字段映射 + 事件过滤），
复用 transport 的生命周期；不写连接循环、不写退避、不写线程管理。

---

## 阶段 1 · 共用层加固（加平台的前置）

| # | 任务 | 难度 | 验收 | 状态 |
|---|---|---|---|---|
| T1.1 | **能力显式声明**：`max_message_length` / `supports_inline_buttons` / `typed_command_prefix` 等类属性，三平台各填真值 | S | 各适配器单测断言自己的上限；调用点不再靠 try/except 猜 | ☑ |
| T1.2 | **授权层统一到 `base.py`**：三平台共用同一入站闸门；**审批/命令类消息必须在闸门之后** | M | 三平台共用同一判定函数；无白名单时入站被拒；Slack/Discord 具备与 Telegram 同等的白名单能力 | ☑ |
| T1.3 | **出站错误分类**：`SendResult` + 平台中立 `error_kind`（7 类），替代现在全吞 | M | 失败可观测：`--status` / 日志能看出哪个平台哪次失败 | ☑ |
| T1.4 | **分片算法**：码点切分 + 断点优先级 + `（i/n）` 前缀两遍法重编号 | S | 长文本/emoji/中文断行测试全绿 | ☑ |
| T1.4b | **分片接入**：三个适配器改用 `opencode_bridge.split`，移除 telegram 内的旧 `split_text` | S | 适配器与测试都指向新实现，无两份算法 | ☑ |
| T1.5 | **状态四态归一 + `--status`**：已连接/连接中/异常/未连接，`--status` 汇总各平台 + 运行时 | S | `--status` 一次给出服务连通 + 各平台状态 + 锁/最近退出 | ☑ |

**拆分原则**：每项独立可发布、可验证、独立 commit。

---

## 阶段 2 · 补齐已有平台的入站（最快见效）

| # | 任务 | 难度 | 要点 | 状态 |
|---|---|---|---|---|
| T2.0 | **最小 WebSocket 客户端**（纯标准库，RFC 6455） | M | `T2.1`/`T2.2` 的共同前置：项目零第三方依赖（Node 有内置 `WebSocket`，我们必须自研）。握手（`Sec-WebSocket-Key` + `Upgrade` 校验）、客户端掩码、分片与控制帧（ping/pong/close）、`recv()` 返回完整文本消息 | ☑ |
| T2.1 | **Slack 入站** | M | Socket Mode：`apps.connections.open`（需 **app-level token `xapp-`**）换 WSS URL；**每个 envelope 必须 ack**（否则 Slack 重发）；3s 重连；`message`/`app_mention` → `Inbound`，先过 `admits()` 闸门 | ☑ |
| T2.2 | **Discord 入站** | M | Gateway v10 WS + heartbeat + 事件去重；REST 侧复用现有发送 | ☑ |

---

## 阶段 3 · 零依赖平台族（每家 0.5~1 天）

| # | 平台 | 难度 | 传输方式 | 状态 |
|---|---|---|---|---|
| T3.1 | Matrix | S | `/sync` 长轮询 + `next_batch` 游标 | ☑ |
| T3.2 | Mattermost | S | WebSocket + `authentication_challenge` + ping | ☑ |
| T3.3 | IRC | S | `socket` 手写客户端 + PING/PONG + 仅响应提及 | ☑ |
| T3.4 | Twitch | S | WebSocket IRC + IRCv3 tags + 限速节流 | ☑ |
| T3.5 | Nextcloud Talk | S | REST 轮询 | ☑ |

---

## 阶段 A · 架构地基（决定后续每加一个平台的成本，必须先做）

| # | 任务 | 难度 | 验收 | 状态 |
|---|---|---|---|---|
| A1 | **传输层抽象**：`opencode_bridge/transport/` —— `HttpPollTransport`（长轮询）/ `IntervalTransport`（短轮询）/ `WebSocketTransport`（包 `ws.py`）/ `TcpLineTransport`（行协议）。基类统一线程、指数退避、**先关连接再 join 的 stop 语义** | L | 四个 transport 各自有测试；迁移 2~3 个现有适配器后行为**不变**（现有 703 用例全绿） | ☑ 包已建成（56 用例）；**迁移进度 4/8：IRC ✓ Matrix ✓ Telegram ✓ Slack ✓**。剩余：**Discord**（需"周期钩子"跑心跳）→ Mattermost → Twitch（IRC-over-WS，最别扭，或考虑不迁）。⚠️ **Nextcloud 不迁**：它是 5 worker 轮转池（在飞长轮询恒 ≤ worker 数），映射不到单`fetch` 回调模型，硬迁会破坏结构 |
| A2 | **会话标识统一**：`opencode_bridge/identity.py` —— `platform:local_id`，提供 `format` / `parse` / `platform_of` / 校验；**向后兼容**已落盘的 `chat:` `channel:` `room:` 旧格式 | S | 各适配器不再自造前缀；旧 `state.json` 仍能读；跨平台同名 chat id 不再混淆 | ◐ 包已建成（27 用例）。**分两类**：<br>· **映射到自身、切换零风险**（`conversation_id` 字节级不变）：**irc ✓ twitch ✓ nextcloud ✓ 已全部完成**<br>· **切换会改键格式**（须先有 A2b）：telegram(`chat:`) / matrix(`room:`) / slack / discord / mattermost(`channel:`，歧义还需知道是哪一家) ☐ |
| A2b | **`state.json` 键迁移**：加载时按 `identity.normalize(..., platform_hint=)` 重写旧键并原子落盘 | M | 用旧 `state.json` 起一次，`chat:`/`room:`/`channel:` 键全部变新格式；**中途中断不丢数据**；旧文件保留备份 | ☐ **A2 的前置**，见下方备注 |
| A3 | **inbound-push 入口**：单端口 HTTP 服务 + 按路径路由到适配器（webhook 类平台的唯一可行入口） | M | 起一个本地 HTTP 服务，两个 webhook 适配器能各自收到 POST 并鉴权；停机干净 | ☐ `httpsrv.py` 已建成（a2a 首个使用方），A3 只需 `add_route()` |
| A4 | **真实服务端到端验证**：至少让一个平台对着**真实服务器**跑通入站+ 出站 | M | 有一条真实会话的端到端记录（收发各一条），并把踩到的协议差异写回文档 | ☐ **⚠️ 当前 12 个平台无一验证过**，详见下方备注 |

**A2b 为什么必须先做**：切前缀会改 `conversation_id` 的**字符串格式**，而 `StateStore`
拿它当**不透明键**存会话映射 —— 于是已落盘 `state.json` 里的旧键全部变成孤儿，
用户会**一次性丢失会话映射**，而且**不报错**（只表现为"agent 突然记错上下文"，
比直接失败难查得多）。所以这不是格式美化，是**数据迁移**。
歧义前缀 `channel:` 还多一层：它被 slack / discord / mattermost **三家共用**，
无法从字符串判断来源，必须由调用方（知道自己是哪家的适配器或 core 的路由层）传
`platform_hint`，**绝不猜**。

**A4 为什么必须做（当前最大的已知风险）**：12 个平台的协议事实全部来自
**官方文档 + 参考项目源码 + 本机回环/假服务器测试**，手法本身可靠，但
**没有任何一个平台对着真实服务器跑通过** —— 手上没有任何 token。
已知的三类残余风险：① 协议字段猜错（症状多为静默收不到消息）；
② 权限/限流语义猜错（如 intents 没勾、scope 不对，症状是"连上了但没事件"）；
③ 各家服务端实现与文档不一致。**只要有一个平台跑通真实收发，就能把①②类风险
从"未知"降为"已验证"**，所以这一项的性价比高于再加一个平台。

---

## 阶段 B · 平台接入波次（按难度由易到难，全部来自两家清单的并集）

难度判据：**纯标准库可行 + 不需公网回调 + 活跃度** 三者同时满足才进 B 组。

| 波次 | 平台 | 入站机制 | 难度 | 关键点 / 风险 |
|---|---|---|---|---|
| **B1** | ntfy | HTTP 拉取（`poll=1` + `since` 游标） | S | 已完成。**偏离原计划**：不用长连接流而用一次性拉取 —— 持久流在 `urllib` 下无法干净打断（`stop()` 会白等超时，见 Nextcloud 的教训）。启动用 `since=<当前时间戳>` **不重放历史缓存**（否则首次启动会把最多 10MB 缓存全当新消息触发 agent）；之后游标推进到 **message id** |
| **B1** | email | IMAP 轮询（`UID` 游标） | S | 已完成。`imaplib`/`smtplib` 全标准库，协议通用永不废弃；**必须用专用邮箱 + app 专用密码**；**无用户身份**（任何能发信给你的人都能驱动 agent）→ 必须用 `allowed_chat_ids` 限定发件人；不支持编辑 |
| **B1** | a2a | 本地 HTTP server（**我们是被调方**） | S | 已完成。方向与其他平台相反；`httpsrv.py` 是**可复用共用模块**（A3 的地基，第一个使用方是 a2a）；默认 bind 127.0.0.1；**无凭据可填** → 需要 `config_optional`（见阶段 A 备注） |
| **B2** | qqbot | WebSocket 网关 | M | 已完成。⚠️ **原描述两处需更正**：① "Discord 风格变体"只对了一半 —— opcode 表像，但**鉴权完全不同**（QQ 握手**不带凭据**，靠 op 2 Identify 传 `QQBot {token}`）；② **"防回环天然"不成立** —— `GROUP_MESSAGE_CREATE` 文档写的是"群里的**每一条**消息"、**不承诺排除 bot**。改用平台签发字段（`author.bot` / `author.id`）。心跳 `heartbeat_interval` 单位是**毫秒**（与 Discord 同坑） |
| **B2** | homeassistant | WebSocket 事件总线（本机/局域网） | M | 已完成。⚠️ **"是不是 IM"这个问题现在有答案了**：结论是**默认不成立** —— HA 推的是设备状态变更，只有能追溯到**真人用户操作**的事件才适合起对话。故默认**一个事件都不收**（须给 `entities`/`domains`/`accept_all`）+ `require_user_context` 默认真。**保活分两层且方向相反**（传输层 aiohttp 发 ping、`ws.py` 自动回；应用层 JSON ping 必须客户端主动发）。`call_service` 出站，不支持编辑 |
| **B3** | feishu / lark | WebSocket 长连接 | M | 官方明确"免内网穿透、**事件明文免解密**"；代价是 SDK 的 WS 握手 + `app_ticket` 刷新要自己实现；⚠️ **无 fromMe 字段**，防回环需自己记 message_id |
| **B3** | wecom | WebSocket（`openws.work.weixin.qq.com`） | M | 必须选"智能机器人"（自建应用只有 callback 模式，要 AES 加解密 + 公网）；流式回复 `streamId` 机制较复杂 |
| **B3** | dingtalk | WebSocket Stream 模式 | L | 二进制帧 + 签名校验，自己实现成本高；出站 `sessionWebhook` 是会话内下发的 URL，须做 **hostname 白名单**防 SSRF |
| **B4** | wechat / weixin | 官方长轮询（`ilink`） | L | HTTP 层可标准库做，**纯文本不需要加密**（媒体才需自写 AES-128-ECB）；代价是**扫码登录流程要自己实现**（二维码 → 状态轮询 → token 持久化）；**仅私聊、一账号一 poller**，须专用小号 |
| **B4** | nostr | WebSocket relay | L | 传输可标准库做，但 **NIP-04/44 要 secp256k1**（NIP-44 是继任者，同样要）→ 要自己实现约 500 行椭圆曲线算术，或接受明文 kind-1（无加密无隐私）；主仓 2025-06 后停滞、dsh 自标"实验性" |

---

## 阶段 C · 横向增强（平台越多越值钱）

| # | 任务 | 难度 | 收益 | 状态 |
|---|---|---|---|---|
| C1 | **prompt hint 注入**（借 `ctx.session.hook("context")`） | S | agent 知道自己在 400 字符的 IRC 上说话，还是 40000 的 Slack；无 markdown 渲染；回复非即时 | ☐ |
| C2 | **脱敏引擎** | M | 手机号 / token / chat id 不再明文进日志与 `state.json`（横跨 8+ 个平台） | ☐ |
| C3 | **入站合并窗口** `..`/`!!` + 5s 超时 + 长输入回执（含快照落盘） | M | 手机长按拆条不再变成 N 次提问 | ☐ |
| C4 | **审批超时回落 + 迟到回答去重** | S | 窗口关闭后随口一句不会被误当批准 | ☐ |

---

## 阶段 D · 主动能力

| # | 任务 | 难度 | 依赖 | 状态 |
|---|---|---|---|---|
| D1 | 渠道地址簿（"我在哪些群"，仅索引已连接平台） | M | A2 | ☐ |
| D2 | 主动发送 `bridge_send` 工具 | M | D1 | ☐ |
| D3 | 富交互按钮覆盖多平台（目前**只有 Telegram** `supports_inline_buttons=True`） | M | T1.1 | ☐ |
| D4 | 定时任务绑 chatId（**opencode 无 cron，需自建日历 + tick + 落盘**） | L | D1 | ☐ |

---

## 阶段 E · 需公网回调的 7 家 —— **最后优先级**（用户已决定）

> **决策（2026-10-02，用户）**：这 7 家放到**最后**，等其余消息平台全部实现完再讨论。
> 因此 A3（webhook 入口）**不阻塞** B/C/D 任何阶段 —— 它们全都只需 outbound 方向。
> 阶段 E 与其余阶段**无依赖关系**，可随时独立开工。



⚠️ 这 7 家经核实**全部是 webhook-only 且无官方轮询替代**。要接就必须有一个可达的回调地址
（cloudflared 隧道 / 备案域名），这**违背本项目"纯本机"的立身约束** —— 所以它们不是"排期问题"，
而是**要不要放宽约束**的决策点。未放宽前不做。

| 平台 | 出站可行 | 入站 | 备注 |
|---|---|---|---|
| WhatsApp（**Meta 官方 Cloud API**） | ✅ Graph API `POST /{PHONE_NUMBER_ID}/messages` | ❌ webhook | ⚠️ **修正此前记录**：官方 API 是存在的（`whatsapp_cloud.py` 首行即 Meta 官方 Business Platform）。两家走的是**另一条**路 —— dsh 与 Hermes 都用 `@whiskeysockets/baileys`（Node，非官方 Web 客户端，有封号风险），Hermes 还要 Node 桥进程 |
| LINE | ✅ `/v2/bot/message/reply`（5000 字符/bubble 硬上限，每次 ≤5 bubble） | ❌ webhook | 无轮询替代 |
| Teams / msteams | ✅ Bot Framework `/v3/conversations/{id}/activities` | ❌ webhook | Bot Framework 无轮询替代 |
| SMS（Twilio） | ✅ | ❌ webhook | Twilio 入站只有 webhook |
| Synology Chat | ✅ | ❌ webhook | 群晖私有，需把 outgoing webhook 指向本机公网地址 |
| Zalo | ✅ `openapi.zalo.me/v3.0/im/oa/message` | ❌ webhook | 活跃度未确认 |
| Google Chat | ✅ | ❌ webhook | 唯一非 webhook 替代是 GCP Pub/Sub pull，但需 GCP 项目 + 服务账号 + `google-cloud-pubsub` 库（**双重违背零依赖**） |

---

## 不做（已决策，附理由）

| 项 | 原因 |
|---|---|
| 配对码（陌生人配对流程） | 一整套限流/TTL/锁定状态机；等到真要在群里开放给陌生人再上 |
| 25 渠道全量 / 平台插件化 | 方向是"做深"不是"做多" |
| 实例锁 | 纯插件进程，无共享 home 概念 |
| 默认改显式 opt-in（`allowed_chat_ids` 空=全开 → 默认拒绝） | 会破坏"配好即用"现状；若要改需单独决策（迁移期 + 警告期）。⚠️ **但平台从 1 个变 8+ 个后风险面显著变大，这项应尽快决** |
| **Signal** | 需 `signal-cli`（Java 二进制）+ **单独注册号**（不能复用主号，有封号风险）；Signal 官方无 bot API |
| **imessage** | macOS 专有 + `imsg`/`osascript`，非 macOS 直接出局 |
| **simplex** | WS 协议简单但必须跑 Haskell 守护进程 —— 只是把重型依赖换个地方装 |
| **photon**（Photon Spectrum） | SDK 是 TypeScript-only，必须 Node ≥18.17 sidecar；商业服务 |
| **buzz**（Block，Nostr 之上） | 需 Rust CLI 二进制 + Nostr secp256k1 签名；README 自列大量"🚧 Being wired up" |
| **raft** | 需 `raft` CLI；消息体根本不过我们的进程（只有"wake"提示），是"挂到别人平台"而非 IM 网关 |
| **whatsapp（baileys 路线）** | 需 Node 运行时 + npm 动态安装；违背零依赖；非官方客户端有封号风险 |

---


## 进度日志

- **2026-10-02** 备份完成：tag `backup/pre-platform-expansion-20261002`（`06a7d2f`），远端仍在 `91c4478`。开始阶段 1。
- **2026-10-02** T1.1 + T1.2 完成（未推送）：`base.py` 新增能力类属性与 `capabilities()` 快照、
  统一授权闸门 `admits()`（认 `allowed_chat_ids`/`allowed_chats`/`allowlist` 三种键，
  空白名单=全开保持 v1 语义）；三平台按真值声明能力（telegram 4096/入站/按钮/媒体，
  slack 40000 仅出站，discord 2000 仅出站）；telegram 的 `_allowed` 改为委托基类。
  新增 10 个用例（`TestCapabilities` / `TestAccessGate`），全量 **123 tests OK**、compileall 0。
- **2026-10-02** T1.3 完成：新增 `hooks.SendError`（7 类平台中立分类）+ `hooks.SendResult`
  （含 `partial` 表达"分片部分成功"）；`base.classify_http()` 做通用 HTTP→分类映射，
  Slack 另有 `_classify_slack_error()` 处理 `ok:false`+错误串（多为 HTTP 200），
  Telegram/Discord 从 `error_code`/状态码 + `retry_after` 取值；
  `Adapter.send_result()` 包装既有 `send()`，**向后兼容**（签名与行为不变），
  子类无需改动即获得结构化结果；分片部分成功由基类识别（句柄存在但记过失败 → `partial=True`）。
  新增 9 个用例（`TestSendResult`）。过程中修掉一个自己引入的缺陷：插入 Slack 错误分类函数时
  把 `@register("slack")` 装饰器劫持到了该函数上，导致 `SlackAdapter` 未注册、`build("slack")` 崩溃。
- **2026-10-02** T1.5 完成：纯函数层 `opencode_bridge/status.py`（`ChannelState` 五态、
  `ChannelStatus`、JSON 往返、`summarize`、`render_table`、30 项 `LEGACY_TEXT_STATES`
  **保守整串查表而非正则**，47 个用例）+ 我侧完成 `--status` CLI 接线。
  `--status` 实跑验证：服务 OK(2.0.21) / 三平台如实显示"未配置"与能力真值 / 运行态
  `disabled`（无锁文件）。**刻意不把"已配置"谎报为"已连接"**——独立进程看不到 bridge
  进程内状态，运行态只依据锁文件 + pid 存活 + failedAt 三类可验证证据，并在输出末尾注明该限制。
  新增 `capabilities().allowed_chat_ids_count` 供状态视图消费。
- **2026-10-02** T1.4 + T1.4b 完成：新增 `opencode_bridge/split.py`（码点计长 + 组合序列
  原子切分 + 断点优先级 + `（i/n）` 前缀两遍法，24 个用例）。接入三个适配器并删除 telegram 内的
  旧 `split_text`——顺带修掉一处不当耦合：slack/discord 原先 `from .telegram import split_text`
  （跨平台依赖 telegram 模块）。**调用点显式传 `prefix_fmt=""`**，保持既有出站行为不变
  （分段不加前缀、`"".join(chunks) == text` 仍是断言的不变量）；前缀编号作为可选能力保留，
  是否默认开启留待后续决策。旧 `TestSplitText` 的覆盖已被 `tests/test_split.py` 完全包含，删除以免
  两处维护同一行为。验证：200 tests OK、compileall 0、ZWJ 守恒（1800 个 ZWJ / 82 段无孤立
  ZWJ 开头）、国旗与键帽未拆。
- **阶段 1 完成**（T1.1~T1.5 全部 ☑）。下一阶段：T2.1 Slack 入站。
- **2026-10-02** T2.0 + T2.1 完成（同一个 commit，两者必须一起落地）：
  - **T2.0** `opencode_bridge/ws.py`（531 行，RFC 6455，纯标准库）：握手三重校验
    （`101` / `Upgrade` / `Sec-WebSocket-Accept = base64(sha1(key+GUID))`）、客户端强制掩码、
    分片聚合、控制帧可插在分片中间（ping 自动回 pong）、`recv()` 只在收到对端 close 时返回
    `None`（超时/裸断都抛 `WebSocketError`，**不用 `None` 混淆"关闭"与"超时"**）。
    47 个用例用**真 socket 服务器线程**（绑 `127.0.0.1:0`）跑，**0 mock**；测试里服务端用
    一份独立实现算 `Accept`，两边各算一遍握手才有意义。
  - **T2.1** Slack Socket Mode 入站：新增 `app_token`（`xapp-`）配置；**先 ack 再过滤**
    （`hello` 与无关事件也要回 `envelope_id`，漏 ack 会让 Slack 无限重发）；授权闸门
    `admits(channel)` 在最前（被丢弃的 envelope 仍 ack）；丢弃 bot 回声与 `subtype`；
    3s 重连；`stop()` **先关 WS 再停线程**（否则基类 join 5s 而 `recv()` 可能阻塞 30s）；
    缺 `app_token` 时降级为"只发出站"并告警。
  - **补上了一个真实覆盖缺口**：`TestSlackInbound` 用假 WS 测协议层、`test_ws.py` 用裸服务器
    测传输层，但"两者对接处"此前无任何覆盖。新增 `TestSlackInboundRealWebSocket`：
    真服务器 + 真 `ws.connect` 跑通 握手 → 收包 → 授权 → Inbound → ack 掩码帧回到服务器。
  - 验证：**257 tests OK (skipped=1)**、compileall 0、两种跑法（`discover -s tests` 的顶层
    模块名 与 `python -m unittest tests.test_x` 的包名）导入均正常。
- **2026-10-02** 接入文案纠错（先核实再写，不凭记忆）：`/setup 2` 的冻结文案此前写着
  「v1 入站轮询尚未接入」「当前无法在 Slack 里与 bot 双向对话」——T2.1 落地后这已经是
  **假话**，用户照着走会以为入站不可用、也不会知道要配 `app_token`。经查 Slack 官方文档
  （2026 年已迁至 `docs.slack.dev`）重写为 Socket Mode 流程：app-level token
  （`connections:write` → `xapp-`）、bot scopes（`chat:write` + `channels:history` +
  `im:history`）、`Add Bot User Event`、以及「每改一次 scope 要重新 Install」和
  「事件没加在 bot events 下会静默收不到」两个坑。核实过程**纠正了我两处想当然**
  （按钮名不是 "Add Bot Token Event"；发私有频道不需要 `groups:write`），并**证伪了
  我记忆里的「Show bot metadata」**（官方文档零命中，刻意不写进文案）。
  README / docs/install.md 同步镜像；把断言旧说法的测试改成断言新事实 + 反向断言
  （防止假话复活）。
- **2026-10-02** 修掉一个**静默失败点**：`--setup --json` 与 `--status` 原本只看
  `bot_token`，于是 Slack 只填 `bot_token`、缺入站必需的 `app_token` 时仍报
  `configured: true` —— 用户会以为双向对话已经通了。改为由适配器**声明**
  `required_tokens`，状态视图据此判定，并区分 `outbound_ready` / `inbound_ready` /
  `inbound_implemented`（入站要"已实现"且"配置齐备"两个条件同时成立）。
  同时把平台清单改为**从注册表自动发现**（`registered_names()` 扫包内模块），
  新增平台不必改核心文件即出现在状态视图；冻结的 `/setup` 菜单仍刻意只列三平台。
  新增 `tests/test_cli.py`（14 个用例）覆盖这些语义。
- **2026-10-02** Socket Mode 加固：WSS URL 约 1 小时过期，Slack 会先发
  `{"type":"disconnect"}`，原实现被动等对端关 socket 才重连。现改为收到 disconnect
  **主动重连**（重新取 URL），并断言 disconnect 之后旧连接上的消息被丢弃
  （否则等于静默丢消息）。
- **2026-10-02** T3.1 Matrix 完成：`adapters/matrix.py`（`/sync` 长轮询 + `next_batch`
  游标跨调用保存，42 个用例）。游标**先推进再分发**（与 telegram 的 offset 一致），
  失败时不动游标。事件过滤 8 条（自己发的回声、非 `m.text`、含 `m.relates_to` 的
  编辑/回复/表情、白名单外的房间等）。编辑用 MSC2676 兼容近似
  （`"* "` 前缀 + `m.new_content` + `m.relates_to{rel_type:"m.replace"}`），
  超限退化为普通发送。
  复核时**推翻了 lane 的偏差说明**：它称"兄弟适配器都返回 `MsgHandle | None`"，
  实测 `base.py` 与 telegram/slack/discord 三家都声明 `-> bool`，且 `core.py` 只对
  `edit()` 判真假 —— 是它自己偏离了契约，已改回 `bool`（而非反过来改契约迁就它）。
  验证：Matrix 在 `--status` 实跑中**自动出现**，全程未改任何平台注册代码。
  全量 **318 tests OK (skipped=1)**、compileall 0。
- **2026-10-02** T2.2 Discord 入站完成：Gateway v10 over WebSocket，47 个用例（假 WS 注入，
  零真实网络）。协议常量全部来自**官方文档 + 源码级核实**，其中纠正了若干凭记忆会写错的
  地方：`heartbeat_interval` 单位是**毫秒**（按秒用会让心跳快 1000 倍、瞬间触发 4008
  限流）；心跳的 `d` 键**不能省**（未收到事件时也要发 `{"op":1,"d":null}`）；网关主机名
  已是 `gateway.discord.gg`，**必须**用 `GET /gateway/bot` 返回的 URL 而非硬编码；
  `Resume` 必须用 READY 里的 `resume_gateway_url`（用错会显著提高断线率）。
  另：invalid token 是 **4004** 而不是常被误传的 4010（4010 是 shard 参数错）；**4013 是
  intent 位值非法、4014 是 intent 未在后台开启**，两者都属"停止重连"集合。
  实现要点：
  - intents 用位值表达式 `512|4096|32768`（=37376）而非裸数字：MESSAGE_CONTENT
    (1<<15) 必须在 Developer Portal → **Bot 页 → Privileged Gateway Intents** 勾选，
    否则 close 4014。
  - 停止重连的判定：`{4004,4010,4011,4012,4013,4014}` → 打**带具体原因**的
    error 后直接退出，不做无脑 while True 重连。
  - 防回环**只用** `author.id == 自己的 user id`（READY 里缓存），**不用** `author.bot`
    ——后者会把别的 bot 的消息也全丢掉。另有关 `author.bot` 键可能整个不存在。
  - 事件过滤 7 条（含 IS_CROSSPOST 去重、`type != 0` 系统消息、webhook 消息）。
  - 主动断开一律用 close 4000 而非 1000，保住 Resume 能力。
  - `stop()` 三段式：停心跳线程 → 关 WS 唤醒阻塞 recv → 才 join。
  顺带核实：现有 REST 出站**已有** `User-Agent` 且已显式 `/api/v10/`，无既有缺陷。
  复核时处理了 lane 诚实报告的两条**旧断言**（它们描述的正是 T2.2 要废掉的 v1 状态）：
  改为断言新事实；由于**所有真实平台现在都实现了入站**，"入站未实现"这一分支改用
  **合成适配器**守护，否则该层语义将失去测试。
  验证：**366 tests OK (skipped=1)**、compileall 0、`--status` 自动列出全部五个平台。
- **2026-10-02** T3.2 Mattermost + T3.3 IRC 完成（同一批两个平台一起落地）：
  - **Mattermost**（84 用例）：WS 入站复用 `ws.py`，鉴权走**握手 header**
    `Authorization: Bearer`（不进 query）。保活最关键：服务器每 60s 发 **ping 控制帧**、
    100s 无 pong 即断开，且**读超时只由 pong 续期**——好消息是 `ws.py` 已自动回 pong，
    适配器一行心跳代码都不用写（并写了测试锁住这个保证，防"顺手清理"时被删掉）。
    读超时取 75s：比 ping 周期长、比服务端 100s 短。**两个信封必须分清**：
    事件 `{event,data,broadcast,seq}` vs 响应 `{status,seq_reply,...}`，按有无 `status` 判别。
    **消息长度上限不硬编码**——官方没给这个数，真实上限由服务端运行时从 DB 列宽算出，
    网上流传的 16383/4000 都不是契约；改为启动时读 `config/client` 的 `MaxPostSize`
    （是**字符串**）细化，失败回落静态下限 4000。
    ⚠️ **Post 对象上没有任何 bot 字段**（已核实），所以防回环**只能**用 `user_id` 比对；
    且自己的 user id 取不到时必须**整个入站停摆**，否则每发一条回复就再触发一条。
  - **IRC**（63 用例，本机回环**真 IRC 服务器**，不 mock）：手写行协议
    （NICK/USER → 等 001 → JOIN），PING/PONG 原样回 token，SASL PLAIN 走完整
    `CAP LS 302` 协商（不先协商 CAP 服务器会直接断开）。
    **512 字节整行上限**（RFC 2812）按真实行形状逐字节算预算，再用 `_byte_safe_split`
    **按字符累加字节**切分，切点永不落在多字节字符中间（"".join 恒等于原文）；
    `max_message_length=400` 是扣掉前缀开销后的保守**字符**上限。
    仅响应**提及**（正则边界含 nick 自身的 special 字符，nick 含 `-` 时
    `foo-bar` 不算提及 `foo`）；清理颜色/格式/零宽/双向控制共 6 类；
    `edit()` 恒 `False`（IRC 无编辑，core.py 会退化成发新消息）。
    换行折成空格是**协议约束**（单行协议无法承载换行，原样发会被注入命令）。
- **2026-10-02** 修掉三个**"平台实现了但用户用不了"级别**的 bug（本轮的核心收获）：
  1. **`_has_configured_adapter` 硬编码找 `bot_token`** —— 只配了 Matrix / IRC /
     Mattermost 的用户会被判成"没配任何适配器"，桥接**直接拒绝启动**。这三个平台
     根本没有 `bot_token` 这个键。改为由适配器声明的 `required_tokens` 驱动。
  2. **`outbound_ready` 硬编码 `bot_token`** —— IRC 与 Mattermost 配齐了也被报成
     "发不出去"。新增 `outbound_tokens` 声明，各平台填自己的出站凭据。
  3. **Matrix 根本没声明 `required_tokens`** —— 继承了基类默认的 `bot_token`，
     于是配得完全正确的 Matrix 会被报成 `missing: ['bot_token']`。
     顺带把 `user_id` 也列入必需：它是过滤自己回声的唯一依据，缺了会无限回环。
  `NO_ADAPTER_MESSAGE` 也不再只提 `bot_token`（那会让这三类用户以为自己配错了）。
  并加了一条**能在将来抓住同类 bug 的不变量**：
  `outbound_tokens ⊆ required_tokens`（能发出去的前提一定是"已配置"的子集）——
  这条不变量正是当初漏掉 Matrix 的那道防线。
  验证：**520 tests OK (skipped=1)**、compileall 0；只配 Matrix / IRC / Mattermost
  的三种配置实测 preflight 均为 True、状态视图均如实报就绪。
- **2026-10-02** T3.4 Twitch 完成 + **文档全面纠错**：
  - Twitch（IRC over TLS WebSocket，64 用例）。行分帧不假设"一帧一行"（`_rbuf` 缓冲按
    `\n` 切行，**断线时丢弃半行**）；IRCv3 tag 按第一个 `;` 切 tag、第一个 `:` 切参数
    前缀，反转义逐个单扫（不能用 `str.replace` 连做，否则 `\\\\s` 会错还原成 `\s`）。
    换行注入防护的测试被**改强**：既断言服务器看到折行后的整行，又断言**不存在**独立的
    `JOIN` 行、且会话内唯一的 JOIN 是那条合法的。
  - **文档纠错**：T2.2 落地后 Discord 在 **9 处**仍被描述为"仅支持主动发送"（README
    能力表还写着「⬜ TODO / 仅发送」），而实际七个平台全部支持双向。逐处改为真实状态，
    并补齐 Mattermost / IRC / Twitch 的接入小节与配置项（含各自最易踩的坑：
    Mattermost 取不到 user id 会整个停摆入站、IRC 换行折成空格、Twitch token 不要自己
    加 `oauth:` 前缀）。能力表新增**编辑消息**一列 —— 这项能力平台间不一致，IRC / Twitch
    没有它，长任务进度会退化成连续发多条消息，这是不该被"能力一致"掩盖的事实。
  - 顺带修掉故障排查里"填好各平台 `bot_token`"这句（对六成平台是错的），改成按平台列
    各自的凭据键并指向 `--status` 核对。
  - 测试侧同步：Discord 引导的后台开关现行名称是 `Message Content Intent`（旧的
    `MESSAGE CONTENT INTENT` 是历史标签），并加**反向断言**防止"仅能主动发送"复活。
  - 排障记录：本机 `grep` 工具对 README.md 的中文匹配失效、PowerShell `-match` 默认
    大小写不敏感（把新写的正确标签误报成过时的旧标签），最后改用**逐行 `-cmatch`
    穷举扫描**才拿到可信结论。
  验证：**584 tests OK (skipped=1)**、compileall 0、过时说法 0 处残留。
- **2026-10-02** T3.5 Nextcloud Talk 完成 —— **阶段 3 收官，八个平台全部支持双向对话**：
  - 入站是 HTTP 长轮询（`lookIntoFuture=1` 挂住最多 30 秒），出站走 OCS REST。协议细节
    全部来自官方文档 + `nextcloud/server` / `nextcloud/spreed` **源码逐行核实**，其中
    **纠正了官方文档的一处错误**：文档写 `timeout` 最大 60 秒，源码实际 clamp 到 **30**
    （main 与 stable31~35 六个分支一致）。三个最容易写错且都是**静默失败**的地方：
    1. **`OCS-APIRequest` 必须是字面量小写 `true`** —— 服务端是**严格字符串比较**
       （`=== 'true'`），`True`/`1`/`yes` 一律被判 CSRF 攻击并返回 403。
    2. **只走 `ocs/v2.php`** —— v1 入口的 HTTP 状态码**恒为 200**（失败看不出来）。
    3. **`304` 不是错误** —— "无新消息"时服务端返回 304，而 `urllib` 把它**抛成
       `HTTPError`**。这是本适配器最容易写错的一处。
  - **反直觉的过滤规则**（源码级）：`systemMessage != ""` 能证明是系统消息，但**反向不成立**
    —— `file_shared` / `object_shared` 会被服务端改写成 `messageType="comment"` **且**
    `systemMessage=""`。所以还必须额外丢弃 `messageParameters` 里含 `file` / `object` 的。
    该用例**先断言前提成立再断言被丢弃**，所以将来谁改了过滤顺序它也会失败。
  - **游标先推进再处理**（与 telegram 推进 offset、matrix 推进 next_batch 同理）：单条
    处理抛异常不会导致重放。`X-Chat-Last-Given` 只在 200 响应里有；304 时游标一字不动；
    200 但 header 缺失时**保持旧游标并告警**（宁可重复也不丢，且这是显式的保守选择而非
    官方要求）。
  - **会话列表是 `api/v4`**（用 v1/v2/v3 会 404）；`modifiedSince` 增量**检测不到"被移出/会话
    删除"**，所以按官方要求**每 5 分钟全量刷新一次**并清掉已消失的会话 —— 否则会拿着一个
    已不存在的 token 一直轮询 404。
  - **worker 池而非每会话一线程**：默认 5 个 worker 轮转取会话（`pop(0)` 再 `append`），
    在飞的长轮询数恒 ≤ worker 数。每个长轮询占住一个服务端 worker 30 秒，所以并发必须保守。
  - **支持编辑消息**（`edit()` 真返回 `True`），但**超 24 小时不能改**、需会话权限含 128、
    会话非只读/非 lobby —— 限制写进 docstring 并在失败时如实归因。消息上限 **32000 是源码
    硬编码常量、不可配置**（明确写清"别去找 occ config 设置"）。
  - 115 个用例，连跑 5 次无抖动。
  - **修掉一个 `--status` 表格对齐 bug**（lane 诚实指出后我核实确认）：根因不是某一行太长，
    而是 `f"{s:<n}"` 按**字符数**补齐，而中文在终端占 2 列 —— 中文表头本来就错位，平台名
    再变长（`Nextcloud Talk` 14 字符 > 写死的 12）就把与下一列的空格挤掉，输出成
    `Nextcloud Talk未配置`。改为按**显示宽度**（`unicodedata.east_asian_width`）补齐 +
    列宽按最长平台名自适应，并加 4 个用例锁住。
  - 复核要点：源码里唯一的 `ocs/v1.php` 出现在**模块 docstring 的告诫文字**里（"不要用
    `ocs/v1.php`"），`_url()` 只拼 `{base_url}/ocs/v2.php/...` —— 抽查脚本报 False 是误报。
  验证：**703 tests OK (skipped=1)**、compileall 0、表格对齐实测正确。
- **2026-10-02** 路线图按"Hermes × dsh 融合"重写（A1/A2/A3 + B/C/D/E 阶段）：
  - 从**权威来源**核对两份平台清单（Hermes 22 个来自 `plugins/platforms/*/plugin.yaml`；
    dsh 22 个来自 `src/channels/*.ts`），并对 23 个候选平台逐个定性。
  - ⚠️ **推翻了原 TODO-1 的前提**：我原写"WhatsApp 官方无 Bot API、未评估可行性"是**错的**。
    官方 **Meta WhatsApp Cloud API** 存在（`whatsapp_cloud.py` 首行即"official Meta WhatsApp
    Business Platform"）。但**入站必须公网 webhook 且无轮询替代**；而 dsh 与 Hermes 走的
    其实是**另一条路** —— `@whiskeysockets/baileys`（Node 非官方客户端，两家都依赖，有封号风险）。
  - 真正的分类轴不是"难/易"而是 **"入站是否需要公网回调"**：23 个候选里 **7 个是
    webhook-only 且无官方替代**（WhatsApp官方 / LINE / Teams / SMS / Synology / Zalo /
    Google Chat）。要接这 7 家必须先有 A3 的 webhook 入口，且**需要可达的回调地址** ——
    这违背本项目"纯本机"的立身约束，所以列为**约束决策点**（阶段 E），不是排期问题。
  - 新增阶段 B 波次（按难度由易到难）：B1 `ntfy` / `email` / `a2a` → B2 `qqbot` /
    `homeassistant` → B3 `feishu` / `wecom` → B4 `dingtalk` / `wechat` / `nostr`。
  - 新增"不做"条目并附理由：Signal（需单独注册号，有封号风险）、imessage（macOS 专有）、
    simplex（须跑 Haskell 守护进程）、photon（SDK 仅 TypeScript）、buzz（Rust CLI + secp256k1）、
    raft（消息体不过本进程）。
- **2026-10-02** A1 传输层 + A2 会话标识（**包已建成，适配器迁移未做**）：
  - **A1** `opencode_bridge/transport/`：`Transport` 基类把"连接 → 收包 → 退避重连 →
    线程 → 停止"这套 8 份重复样板收敛掉；`PollingTransport` / `WebSocketTransport` /
    `TcpLineTransport` 三个具体实现 + `EventQueue`（批量 fetch → 一次一条）。55 个用例。
    基类 6 条不变量各有测试，其中"**先关连接再 join**"是用**事件顺序**（不是"stop 有没有
    返回"）证明的 —— 那是 8 个适配器都踩过的坑。
  - **两处我拍板的语义修正**（lane 主动指出，抓到了会破坏"行为不变"的真问题）：
    1. `reset_after` 原默认 60s = "稳定存活 60s 才重置退避"，而现有适配器是
       **"连上过一次就重置"**（`irc.py:372` 明写）。直接迁移会让网络闪断时退避一路涨到
       上限。**改为默认 `reset_after=0`（连上就重置 = 行为不变）**，保守模式改为显式可选，
       并加两条用例：默认值为 0 的承诺、正数时确由调用点闸门生效（防它退化成死参数）。
    2. `_next` 一次只交付一条，而 Telegram/Matrix/Nextcloud 一轮返回**多条** →
       新增 `EventQueue` 共用件，而不是让每个适配器各写一遍闭包队列。
  - **A2** `opencode_bridge/identity.py`：统一 `platform:local_id`。发现并修掉两个真 bug：
    1. `channel:` 被 **slack / discord / mattermost 三家共用**，而 `StateStore` 用
       `conversation_id` 当不透明键存会话映射 → 两平台出现相同 local id 就会**共用会话**
       （用户在 A 平台的对话串到 B 平台）。迁移后不可能撞车。
    2. `channel:` 属歧义前缀且**无法从字符串判断来源**，所以 `normalize()` **缺
       `platform_hint` 就抛错、绝不猜** —— 猜错会把用户映射到别人的会话，
       这类错误不报错、只表现为"agent 突然记错上下文"，比直接失败难查得多。
  - 过程中自己的测试抓到**两个我自己引入的 bug**：`is_valid("chat:55")` 原本返回 `True`
    （`chat` 在语法上是合法平台名，故旧格式与新格式无法区分）—— 改为以 legacy 登记表
    为准，旧别名一律拒绝并指向 `normalize`；以及 `_check_local` 禁冒号会**炸掉 Matrix 的
    真实房间 id** `!abcDEF:example.org`。
  - 另修一条**测试自身**的缺陷：`reset_after` 用例原本断言 wall-clock 间隔，
    **单独跑通过、全量跑失败**（机器忙时线程调度抖动）→ 改为断言内部退避状态，
    确定性与负载无关，连跑 3 次无 flaky。
  - ⚠️ **待办**：A1/A2 目前只是**包**，八个适配器**还没迁过来**，所以运行时行为零变化。
    迁移是有风险的一步（会让 `conversation_id` 变格式、进而影响已落盘的 `state.json`），
    按 lane 建议的顺序逐步迁：IRC（最干净）→ Matrix/Nextcloud（轮询）→ Slack
    （`ReconnectNow` 有真实收益但**属行为变更，要单列**）→ Discord（需"周期钩子"跑心跳）
    → Twitch（IRC-over-WS，需 WS 行子类，最后迁或考虑不迁）。
  验证：**782 tests OK (skipped=1)**、compileall 0、transport 与 identity 连跑 3 次无 flaky。
- **2026-10-02** B1 ntfy 完成 —— **第一个建在新传输层上的平台**，用来验证"新平台成本
  是否真的降到声明式"。**我没有再派 lane**：派单连续三次被 provider 传输错误打断
  （`Decode error ... /chat/completions`），于是改为自己实现 —— 这也顺带让我直接
  拿到了第一手的"新平台到底要写多少"的数据。
  - **协议细节取自官方文档**（`docs.ntfy.sh/subscribe/api`），并读本机 Hermes 的
    ntfy 适配器作参考（只读）。**偏离了原计划的流式订阅**：改用 `poll=1` 一次性拉取，
    因为持久流在 `urllib` 下**无法干净打断**（Nextcloud 那次已踩过：`stop()` 会白等
    超时）。代价是多一次 HTTP 往返，收益是生命周期干净且能直接复用传输层。
  - **启动不重放历史缓存**：`poll=1` 不带 `since` 会返回**整个话题缓存**（最多 10MB）。
    对桥接是灾难 —— 首次启动会把过去几小时的通知全当新消息，各触发一次 agent 运行。
    文档确认 `since=` 接受 Unix 时间戳，所以启动用 `since=<当前时间>`，之后游标推进到
    **message id**（文档对轮询场景的明确建议，否则每次重读整个缓存、还会撞
    `X-Messages-Truncated` 与 429/`42905` 带宽预算）。
  - **游标只在队列排空时推进**：推进过早 + 进程中途崩溃 = 剩余消息被 `since=` 跳过，
    静默丢消息。这条有专门用例。
  - **防回环只用 `tags`，绝不用 `title`**：ntfy 无用户身份原语，而 `title` 是
    **发布者可控**字段 —— 任何人可以给自己发一条带同样 title 的消息，拿它当身份等于
    没有认证。文档级要求写在类 docstring 里。
  - **4096 是字节不是字符**（服务端受 FCM/APNS 约 4KB 约束），中文一字 3 字节。
    所以出站不能直接 `text[:4096]` —— 那会把中文切成半个字符、编码即报错。实现按
    字符累加字节截断，并有专门用例（含 emoji 四字节）。
  - 不支持编辑（无 edit 端点）→ `edit()` 诚实返回 `False`，让 core 退化成发新消息。
  - **信任模型警告写进文档**：公共话题等于把 agent 暴露给全网，务必私有话题 + token。
  - 46 个用例；过程中自己的测试抓到**一个实现 bug**（`_fetch_one` 在队列非空时走
    早返回，**完全跳过了游标推进逻辑**，导致游标永不推进）与**一个测试 bug**
    （`Responder` 脚本耗尽后默认返回 200，把第二次 send 的失败掩盖了）。
  验证：**828 tests OK (skipped=1)**、compileall 0；ntfy 在 `--status` 实跑中自动出现。
- **2026-10-02** **A1/A2 迁移第 1 家：IRC**（首个迁移，验证"迁移不改变行为"这个命题）：
  - `irc.py` 删掉 `_session_loop`/`_read_loop`/`_open_socket` 与手写退避，改用
    `TcpLineTransport`（子类只加 tick + TLS + on_close 三个接缝）；`_conversation_id`
    改走 `identity.format_id`。
  - **A2 在 IRC 上是零风险的**：`LEGACY_PREFIXES["irc"] == "irc"`，前缀映射到自身，
    所以 `conversation_id` **字节级不变** —— 已落盘的 `state.json` 不受影响，
    并用 9 类 target 的逐字节相等断言钉死。**这三个平台（irc / twitch / nextcloud）
    迁移时都享有这个性质**，而 telegram / matrix / slack / discord 不享有。
  - 退避接线逐项核对与迁移前等价：`min_backoff=5.0` / `max_backoff=60.0` /
    **`reset_after=0.0`**（连上即重置 = 迁移前语义）/ `max_line_bytes=512`。
  - **代码量只净减 42 行真实代码**（不是 lane 预期的 150 行）—— 诚实记录：省下的
    150 行样板被"tick 接缝"吃掉了大半。真正的收益是**概念性**的：
    `stop()` 顺序、退避封顶、重置时机这三件事以后**不可能再被写错**。
    下一个适配器的增量成本是"两个接缝 ≈ 57 行"，不是零，但比重写 150 行循环小。
  - ⚠️ **lane 主动标出两处真实的行为差异**，我不替它粉饰：
    1. **出站分片跨重连**：迁移前 `send()` 一次抓一个 socket 发完所有分片，连接在
       分片中途断掉时剩余分片写失败 → `TRANSIENT` + 部分句柄；现在每片重新读
       `transport.connection`，后半片会发到新连接上。**我判定新行为更好**
       （IRC 不关心 TCP 边界，每片本就是独立 PRIVMSG，能发完比报错更符合预期），
       但它确实是差异，且**没有测试覆盖**（稳定复现需要在分片之间掐连接）。
    2. **读超时语义**：迁移前 `recv` 超时**一定**回到读循环顶部；现在超时返回
       `NOTHING`，若缓冲里已有整行则该轮 tick 被跳过（连续多行时 tick 间隔变短）。
       对 30s 注册超时 / 300s 保活无影响，但若将来把 `register_timeout` 调到比
       "一行到达间隔"还小，检测会有微小延迟。
    另两处：shutdown 期的 `OSError` 转 `NOTHING`（避免正常停机刷 WARNING），代价是
    真实 FIN 与本地关 socket 的竞态下可能少一行日志；`_target()` 保留手写剥前缀，
    换成 `identity.local_of` 会改变 `"foo:bar"` 这类畸形输入的行为 —— 那是行为变更，
    超出"零风险"授权范围。
  - **补掉一个真实回归缺口**（lane 提出，我核实后确认成立并动手补）：此前只有
    "默认值是 0"这个属性断言，以及"`reset_after>0` 时**不**重置"的反面用例，
    **没有任何用例证明 `reset_after=0` 在线程里真的生效** —— 而这正是 IRC 等适配器的
    既有依赖，且它们自己的用例是**白盒读 `_backoff`**（传输层改判定方式就集体失效）。
    新增 `test_reset_after_zero_resets_even_a_brief_connection`：与反面用例**同一套脚本**、
    只把 `reset_after` 从 10 改成 0，构成正反对照；判据取"退避不再越过 0.25"这个
    **稳定**谓词，刻意不用 wall-clock 间隔（那正是本项目踩过的 flaky 坑）。连跑 5 次一致。
  - lane 改了 6 个现有用例（`_thread`/`_sock`/`_open_socket` 这些私有字段随迁移消失）。
    我逐条核对了 diff：**新增 55 行 assert、删除 5 行 assert**，且 5 处删除全部有
    **更强**的替代（`assertIsNone(_thread)` → `assertIsNone(transport)` +
    `assertFalse(running)` + 新增 `assertFalse(_registered)`）。无一条断言被削弱。
  - lane 顺带发现：**"读超时要给适配器一个 tick"是行协议类适配器的普遍需求**
    （Twitch 的注册超时 + 保活同款）。若 Twitch 也迁，应把这个子类提到 `transport/`
    做成 `LineTransport(on_tick=...)`，而不是让每家各写一遍。
  验证：**840 tests OK (skipped=1)**、compileall 0；IRC 74 例 + transport 56 + identity 24，
  连跑 5 次无抖动。
- **2026-10-02** **B1 email 完成**（第十个平台，第二个建在传输层上的新平台）：
  `adapters/email.py`（1015 行，真实代码约 728）+ 111 个用例，IMAP 收 / SMTP 发。
  **比 ntfy 大 3 倍是有理由的**：IMAP + SMTP + MIME 构造与解析 + TLS 配置 + 线程引用 +
  有界去重 + SMTP 错误分类。逐个方法核过，未发现多余抽象或范围膨胀。
  - **防回环（本平台最致命的一处）**：自己发出的信会回到收件箱，处理不好就是
    **agent 无限自问自答**。三条判据按优先级：① `Message-ID` 命中已发集合（精确）
    → ② 剥掉 `Re:`/`Fw:` 链后主题带 `[opencode]` 前缀**且**这封信不是对我们那封的
    回复（`In-Reply-To`/`References` 都没命中）→ ③ 其余**放行**，所以用户点"回复"
    能正常追问。**任何情况下都不查 From 地址** —— 那会误伤用户多地址互发。
    `echo_prefix` **刻意不可配成空串**（空前缀 = 关掉防回环），非法值回落默认值 + 告警。
  - **游标用 `UID` 而非 `UNSEEN`**：flag 会和人工读信冲突（用户在手机点开一封，桥接就
    再也看不见它），且 flag 语义各服务端不一致；UID 单调不漂移，是唯一可靠的增量游标。
    **首次连接只取水位线、一封正文都不取**（与 ntfy 的 `since=<当前时间>` 同构）。
    游标**先推进再处理**（at-most-once，与 telegram offset / matrix next_batch 同取舍）；
    取不到正文时**停在连续前缀末尾**，绝不跳过失败的那封。
  - **不改用户的邮箱状态**：取信用 `BODY.PEEK[]` 而非 `BODY[]` —— 后者**隐式设 `\Seen`**，
    等于替用户把邮件标已读、手机端未读数突然少一封。**绝不主动发 `STORE`**。
  - **不用 IMAP IDLE**（长连接在标准库下无法干净打断，ntfy 与 Nextcloud 已各踩一次），
    每轮新建并关闭一次连接，60s 轮询下握手开销可忽略。
  - **TLS 默认安全且非法值回落成加密**：`imap_security`/`smtp_security` 默认 `ssl`，
    **非法值回落加密、绝不回落明文**；`verify_tls` 默认 `True`，非法值回落 `True` + 告警，
    关掉时打显式警告。这套"非法值回落成安全值 + 告警"的纪律是 lane 自己提出的。
  - `imap_host`/`smtp_host` **无默认值**，因此如实列进 `required_tokens`
    （我给 lane 的示例只写了 address/password，它改宽了 —— **这个改动是对的**：
    按地址域名猜 `imap.gmail.com` 对自建/企业邮箱是错的启发式）。宁可报
    `missing: imap_host`，也不要给一个含混的连接失败。
  - 单行上限 **998 是 RFC 5322 硬上限**（真实约束是**行长**而非正文总长，正文可多行）。
  - 复核确认禁用项只在注释里出现、代码零调用：`IDLE` / `STORE` / `\Seen`。
    测试全程注入假 IMAP/SMTP、**零真实 socket**。
  - ⚠️ **lane 主动交代了一个我早先观察到的异常**：它为重算基线把两个文件临时`move`
    到 `_hold/`，我的采样正好落在那个窗口，一度看到"email 文件不存在"。解释与我的
    观察吻合；它也承认该用 copy 而非 move（一次计数不该动工作树上的文件）。
  - 关于基线数字：lane 报的"基线 853"里**包含 Matrix lane 正在改的测试**，
    我提交 IRC 后的 840 才是准确值 —— 不是真分歧。
  验证：**111 用例 OK**、`--status` 实跑出现 `Email`、compileall 0。
- **2026-10-03** **A1 迁移第 2 家：Matrix**（42 → 56 用例），并修掉一个 flaky 用例：
  - **前缀刻意保持 `room:` 不变**（派单时明确划的界）。`room:` 在 `LEGACY_PREFIXES` 里是
    **指向别的平台的旧别名**，切前缀会改变 `conversation_id` 格式，而 `StateStore` 拿它
    当**不透明键** → 已落盘 `state.json` 的键全部变孤儿，用户**一次性丢失会话映射**且
    不报错（只表现为"agent 突然记错上下文"）。模块 docstring 写清了前置条件：
    切换必须与 `state.py` 的键迁移一起发。用例显式断言 `_conversation_id(room) == f"room:{room}"`，
    **防止有人在后续迁移里偷偷切前缀**。
  - **游标先推进再分发**守住：`_on_sync` 里 `self._since = next_batch` 在 `_dispatch_sync`
    之前逐字照搬迁移前顺序，并加了两条用例（分发抛异常 / 真实消费线程上分发抖动，均断言
    下一次请求带新游标 = 不重放）。**失败时游标一字不动**也有用例（连续 3 次 503 期间
    每次请求都带旧游标）。
  - **退避是常数不是指数** —— 核实迁移前 `BACKOFF_INTERVAL = 2.0` 就是一个常数，
    所以 `idle_sleep` / `min_backoff` / `max_backoff` **三者同为 2.0**（`max==min` ⇒恒定不增长）
    + `reset_after=0.0`。**若只设 `min_backoff=2.0` 而不管 `max_backoff`，传输层默认会把它
    变成指数退避** —— 那是行为变更。lane 还钉了一条"不许被顺手优化成指数"的用例。
  - **事件过滤 8 条**用一个 payload 装 12 个事件做总断言，并**补上了此前没被测的第 ③ 条**
    （`content` 不是 dict）。`_handle_event` 一个字符未改。
  - ⚠️ **一处真实行为变化：成功路径的 0.01s 限速消失了**。迁移前每次成功都有
    `wait(0.01)`（同时是让 `stop()` 能立刻退出的手段），现在成功轮直接进下一轮。
    判断生产影响可忽略：`/sync` 挂起 30s 才返回，提前返回意味着"有事件"，频率上限是
    事件到达率。**但若某个不合规的 homeserver 立刻返回空批次，就会变成对本机
    homeserver 的热循环** —— 迁移前那个 0.01 恰好是兜底。修它需要传输层加 pacing
    （属于 `transport/` 的事），本轮没做，已记为已知限制。
  - **代码量这次是变多的**（真实代码 +8 行）：删掉的样板只有 22 行（`_inbound_loop` 17 +
    建线程 5），新增接缝约 30 行 —— 因为"继承基类 1 行"变成"显式 10 行"，且轮询要拆
    `_request_sync`/`_on_sync` 两个接缝。诚实结论：**Matrix 这家迁移的收益是概念性的、
    不是行数的**；下一家轮询适配器能直接复用，边际成本约 15 行，不需要付这次
    "游标拆分"的额外成本（那部分本就是 Matrix 业务逻辑，迁不迁都得写）。
  - **Nextcloud 不适合迁移**（我复核后调整了原计划）：它是 **5 worker 轮转池**（在飞的长
    轮询数恒 ≤ worker 数），映射不到 `PollingTransport` 的单`fetch` 回调模型，硬迁会破坏
    结构。所以迁移顺序从"IRC → Matrix/Nextcloud"改为 IRC → Matrix → （Telegram / Slack）。
  - **修掉一个 flaky 用例**（lane 报告、属我的文件、我核实后确认成立）：
    `test_invariant2_backoff_reset_threaded` 高负载下 15 次里间歇失败 1 次
    （墙钟差分测到 0.172，标称 0.1，旧断言 `assertAlmostEqual(delta=0.06)`）。
    根因不是实现（等待只会**比标称更长**），而是**断言形状错了** —— 带容差的墙钟差分
    既会在机器慢时误报，又**抓不到**"等待比标称更短"这类真bug。
    **我没有按建议只放宽容差**，而是给 `_ScriptedBase` 加了 `backoff_at_open`（每次 `_open`
    时刻的退避快照），把断言换成**确定性状态序列** `0.1 → 0.2 → 0.4 → 0.1 → 0.2`
    （涨、涨、**重置**、再涨）。第 4 项是核心，第 5 项防止"用永不重置来让第 4 项成立"；
    重置若坏掉序列会变成 `0.1,0.2,0.4,0.8,0.8` 立刻失败。墙钟只保留**下界**断言。
    压测：**静默 0/20 失败，人为负载下 0/12 失败**。
    ⚠️ 这已经是本项目第三次因墙钟断言踩坑（前两次：transport reset 用例、IRC 退避），
    **教训应固化为规范**：凡是断言"等了多久"的用例，一律优先断言内部状态。
  - lane 改了 3 个现有用例。核对 diff：**新增 54 行 assert、删除 2 行**，删除的两行都是
    `assertIsNone(adapter._thread)`（线程归传输层后该字段**恒为 None**，断言它等于断言
    恒真），已替换为`assertIsNone(adapter.transport)` + `not running`。
  验证：**965 tests OK (skipped=1)**、compileall 0、`--status` 十个平台。
- **2026-10-03** **补上两份缺失的文档**（用户提出"是否一边实现一边记录、方便以后了解项目"，
  清点后确认记录一直在做，但**有两个真实缺口**）：
  -清点结论：记录机制**已存在且够细** —— 本台账579 行、每个 commit 带完整实现理由、
    模块docstring 写了协议事实与踩坑。**缺的是两样**：
    ①**没有架构总览** —— 想理解"分层怎么搭起来"只能读本台账 579 行**按时间排**的日志；
    ② **没有"如何加一个平台"指南** —— 而这正是整个重构的立身目标，目前只散落在
    本台账与 commit 里。
  - **`docs/architecture.md`（164 行）**：分层图、入站/出站两条完整数据流、模块职责表
    （含"**不该出现在这里的东西**"一列，防止职责漂移）、以及最有价值的
    **关键不变量清单** —— 每条都是踩坑后定下的（契约类 6 条、会话标识 3 条、传输层 3 条、
    与外部系统打交道 5 条、测试 2 条，共 19 条），并标注了编号供其它文档引用。
    ⚠️ 写成"16 条"过时了 —— a2a 落地时新增了两条（`config_optional`、默认只bind 回环）
    与一条修正（入站归属靠 `Inbound.platform` 而非猜）。
  - **`docs/adding-a-platform.md`（192 行）**：**筛选闸门**（三条准入，任一不满足就停）
    → **八步**（查证协议事实 / 声明能力 / 声明凭据 / 接线传输层 / 授权闸门在最前 /
    防回环 / 出站 / 用户可见的四处文档）→ **坑清单**（按类分，含"防回环只能用平台签发
    字段"对照表：ntfy 用 `tags` 不可用 `title`、email 用 `Message-ID` 不可用 `From`、
    Discord 用 `author.id` 不可用 `author.bot`）→ 测试要求与验收命令。
  - 两份文档都写明了**用户可见的四处**（README / install.md / plugin/README.md /
    tasks.md 进度日志），并特别标注 **`/setup` 引导是刻意维护的冻结文案、默认不要动**。
  - **顺手修掉一处过时说法**：README 第一句仍写"把 Telegram / Slack / Discord 的消息
    桥接"，而实际已是**十个平台全部双向对话** —— 这正是"新增平台时顺手核对旧说法"那条
    纪律要防的事 itself，与历史上"9 处文档仍写仅支持主动发送"同类。
  - 文档也写了**迁移的诚实结论**：IRC 迁移真实代码净减 42 行、**Matrix 反而多 8 行** ——
    收益是**概念性**的（不变量由一份实现 + 独立测试守住），不是行数。
  - 验证：14 条 markdown 内部链接**零断链**（第一次的检查脚本自身有 bug，误报 8 条
    BROKEN，改正后为 0）；**无任何测试读取 markdown**（故改文档不影响测试）；compileall 0。
- **2026-10-03** **A1 迁移第 3 家：Telegram**（34 个新用例），并因此**发现并修掉一个
  A1迁移自己引入的跨平台误路由回归**：
  - Telegram 侧：删掉 `_poll_loop()`（手写循环 + 字面量 `wait(2.0)` + 建线程），
    改用 `PollingTransport`。前缀**保持 `chat:` 不变**并加两条显式断言钉住，其中一条
    真写 `StateStore` 再重开，**证明切前缀会让键成孤儿**。
    - **迁移前是常数退避**（`_poll_loop` 里一个字面量 `wait(2.0)`，无任何增长），
      所以 `min_backoff` 与 `max_backoff` **接成同值** → 传输层退避恒定。若只设
      `min_backoff` 而不管 `max_backoff`，传输层默认会把它变成**指数退避**。
      用例锁在"连续 4 次全是2.0"。
    - **长轮询超时的两层关系**：socket 超时必须 **>** 服务端 `timeout`（25 → 40），
      否则会在服务端返回前先本地超时；并验证它**跟着 `poll_timeout` 走**（不是常数 40）。
    - **按钮能力不许丢**（Telegram 是仓库里唯一 `supports_inline_buttons=True`）：
      5 条用例，含"回调 hook 崩了**仍**在 `finally` 里应答 callback query"。
    - **`allowed_updates`（服务端侧订阅范围）与客户端入站过滤是两处不同机制**，
      刻意分开断言 —— 迁移极易把两者搞混。
    - **一个现有用例都没改**：Telegram 的既有用例住在 `tests/test_adapters.py`（未动），
      11 个全绿。靠的是保留了 `_post`/`_flush_pending`/`_poll_once`/`_dispatch_update`
      这些注入点与签名。
    - 代码量**变多 +50 行真实代码**（第三次同向）：删的只是 5~10 行胶水，加的是必须逐条
      说明"迁移前是什么"的接缝与docstring。**迁移的收益是概念性的，不是行数。**
  - ⚠️ **回归（本轮最重要的收获）**：Telegram lane 报告 `core.py::_adapter_for` 靠
    `getattr(adapter, "_thread")` 匹配**调用线程**来判定归属，而迁移后该字段恒为
    `None` → 这层保护失效，落到了 `_route_by_prefix`。我核实后发现**问题比它报告的更严重**：
    1. `Inbound` **本来就有 `platform` 字段**（适配器自己知道是谁），却从没用过；
    2. 旧 `_route_by_prefix` **只硬编码了 `chat:` 与 `channel:` 两种前缀**，其余一律
       `return adapters[0]` —— 而 `attach()` 顺序就是**配置文件字典顺序**（用户可控）；
    3. `_adapter_for` 会把**猜出来的**名字写进 `_conv_adapter` 缓存，**第一次猜错会粘住**
       后续所有查找；
    4. **`_adapter_for` / `_route_by_prefix` / `_conv_adapter`此前零测试覆盖** ——
       这就是回归能活下来的原因。
    **用旧逻辑复刻验证**（挂载顺序 matrix 在前）：`irc:#chan`、`room:!a:b`、
    `ntfy:topic`、`email:bot@x.com`、`chat:55` 五个里有**四个**被路由到**错误的适配器**。
    即：用户配了 Matrix + IRC，**IRC 收到的消息回复会发到 Matrix**，且不报错。
    连 `chat:` 也不安全 —— 旧逻辑 `named("telegram") or adapters[0]`，telegram 未挂载时
    照样落到别人。
    - **修法（两层）**：① 新增 `_remember_platform()`，在 `on_inbound` 里用
      `Inbound.platform`（**准确**信息）种下映射 —— 且必须**在 `is_callback` 早退之前**，
      否则按钮回调那条路会漏；② 重写 `_route_by_prefix` 覆盖**全部**前缀，借
      `identity.LEGACY_PREFIXES` 解析（旧别名为 `None` 即歧义，保留启发式并注明它只是兜底），
      避免此处变成第二份真相。
    - 新增 `tests/test_routing.py`（17 用例）补上这块的**零覆盖**。其中回归本体那条
      **故意把挂载顺序设成与前缀相反**（先matrix 后 irc）。
    - 我**没有**只靠"新用例通过"就收工 —— 通过不等于有效，所以额外**复刻旧逻辑验证过
      它确实会错**。过程中我自己的一个断言也写错了（把"缓存里有键"当成污染，
      实际那是 `_adapter_for` 本来就会做的正确缓存行为），已改为守更精确的不变量：
      **空 `platform` 不许覆盖已有的准确映射**。
- **2026-10-03** **B1 a2a 完成**（第十一个平台，本项目**第一个方向相反**的平台：我们是被调方）
  + **新增共用模块 `httpsrv.py`**（A3 的地基）+ **新增 `Adapter.config_optional`**：
  - `httpsrv.py`（674 行）：`HttpServer`/`Route`/`HttpRequest`/`HttpResponse`。
    线程、优雅停机（`shutdown→server_close→join→等在途请求`）、异常隔离、请求体上限、
    **鉴权 fail-closed**（声明要鉴权却没给 `authenticate` → 401+ERROR，
    **绝不允许配置错误悄悄变成"完全敞开"**）全在共用层。a2a 只提供"路径 + 处理器"。
    **这正是派单时要求"不许埋在适配器内部"的价值** —— A3 将来只需 `add_route()`。
  - `a2a.py`（1311 行）：Agent Card（`/.well-known/agent-card.json`）、`/health`、
    JSON-RPC（`SendMessage`/`GetTask`/`ListTasks`/`CancelTask`）。
    **能力只声明已实现的**：流式与 push 在 Agent Card 里如实写 `false`，
    未实现的方法返回规范要求的 `-32004`/`-32003`，**不做半成品接口面**。
  - 协议事实**逐条查官方规范**（v1.0.0）而非凭记忆，几个反直觉点：
    方法名是 **PascalCase**（`message/send` 那种斜杠名**在 v1.0 已不存在**，属pre-1.0）；
    `GetTask`/`CancelTask` 的参数叫 **`id` 而不是 `taskId`**；
    认证失败是 **HTTP 401 且规范没定义对应 JSON-RPC code**。
    规范**没有**任何消息长度上限（只有 §13.4 的 SHOULD），所以自行声明 1 MiB 请求体
    上限并写明理由，**没有编造一个权威数字**。
  - **`config_optional`（本轮第二个收获，由复核 a2a 时发现）**：
    lane 把 `required_tokens = outbound_tokens = ("bind_port",)`，并诚实标注了
    "否则状态视图会显示 `missing: ['bind_port']` 这种**误导性**文案"。
    我核实后发现问题**比这更严重**：**只配 a2a 的用户被桥接拒绝启动**
    （实测 `preflight=False`）—— 与当年 Matrix/IRC/Mattermost 被拒启动**同一类**bug。
    根因是仓库那条"两个 token 列表必须非空"的守卫把两件事混为一谈：
    **必须声明**配置面 vs **必须显式配置**才能跑。a2a 满足前者、不满足后者。
    **修法**：新增 `Adapter.config_optional`（默认 `False`），a2a 置 `True`；
    显式豁免 preflight 与**两个**状态视图判定点（`_platform_status` /
    `_channel_config_rows`），**但不豁免声明义务**（两个列表仍须非空）。
    另有 4 条新用例守边界：默认必须是 False（防被人顺手默认成 True 而拆掉所有门槛）、
    豁免不影响声明义务、以及**普通平台的拒绝路径仍必须有效**（4 个残缺配置实测仍 False）。
    验证：修复后 `只配 a2a（空配置）→ preflight=True`，`--status` 里 A2A 显示"已配置"。
  - **一处断言被我反转并留下痕迹**：`test_configured_when_only_the_port_is_set`
    原本断言 `assertFalse(... "缺 bind_port 时不该被判成配置齐备")` —— 它守的其实是
    **那个真bug**；lane 当时没有第二个选项才这么写。已改为断言"空配置即放行"，
    并在 docstring 里写清**为什么反转**，防止有人"顺手改回去"。
  - lane 主动列出**它不确定的 6 处**（未与真实 A2A 客户端对过 interop、
    `SecurityRequirement` 形状只照规范示例抄、同 peer 并发任务按 FIFO、
    `pageToken` 是整数偏移而非真游标、重启后内存态丢失、非回环暴露未测）——
    这种诚实标注比"全绿即完成"有价值得多。
  验证：**1150 tests OK (skipped=1)**、compileall 0；`--status` 实跑**11 个平台**，
  a2a 自动出现且显示"已配置"，其余 10 个仍如实"未配置"。
- **2026-10-03** **A1 迁移第 4 家：Slack**（29 个新用例，`+225/−60`）：
  - 迁移前退避已确认是**常数**（`RECONNECT_DELAY = 3.0` 字面量），
    `min_backoff`/`max_backoff` 接同值 + `reset_after=0.0`。
  - **Socket Mode 四条铁律全部保住**（每条都是本项目踩过的坑）：
    ① `disconnect` **主动重连**（重取 WSS URL —— 它约 1 小时过期）；② **每个 envelope
    都必须 ack，包括被过滤掉的**（漏 ack → Slack **无限重发**）；③ 授权闸门在最前，
    但**被丢弃的 envelope 仍要 ack**；④ **先 ack 再过滤**。
    机制上：`ReconnectNow` 放在**传输层的 `on_message` 钩子**里而不是塞进读循环，
    基类捕获后 `immediate=True` **跳过退避**；而 `_handle_envelope` **函数体逐字未动**
    （已核对 diff 确认），ack 仍写在第一个 `return` 之前，所以每个提前 return 的分支
    都已 ack。**旧连接消息被丢弃是结构保证**（连接已被 `_close_conn` 关掉，`_pump`
    不会再 `_next` 它），用例断言旧 WS 的 `recv_calls` **冻结**在 1。
  - **⚠️ 一处真实的行为差异（我核实后确认并如实记录）**：迁移前 disconnect 走
    `wait(RECONNECT_DELAY)` **等 3 秒**才重连；迁移后走 `ReconnectNow` **立刻重连
    （零等待）**。既有测试把 `RECONNECT_DELAY` patch 成 0.01，**分辨不出**这个差异，
    所以靠代码注释 + 新用例（`RECONNECT_DELAY=30` 时仍立刻重连）留痕。
    **我的判断**：这是改善而非退化 —— `disconnect` 意味着 WSS URL 即将失效、
    本来就必须换一条连接，等 3 秒没有意义。但它确实是差异，**不该被静默接受**。
  - **顺带修掉一个真bug**：迁移前 `start()` 漏了 `_stop_event.clear()`，
    所以 **stop 后重启同一实例会静默不工作**。已补（irc/matrix/telegram 迁移时也补了）。
  - **零改动既有测试**：`git diff --stat` 只有 `slack.py` 一个文件，`tests/` 零改动、
    **零断言被删**。既有 Slack 相关 59 条用例全绿 —— 靠的是保留了
    `_handle_envelope(ws, raw) -> bool` 等注入点签名。
  - **一处必要的结构决定**：`WebSocketTransport` 会把同一帧**既给 `on_message` 又给
    `on_event`**，而既有测试钉死了 `_handle_envelope` 的签名，所以全部语义走
    `on_message`，`on_event` 是**显式空实现 + 长注释**；并专门加了"一帧只投递一次"
    的用例证明**没有双重投递**。
  - **代码量**：净 +165 行，但按 tokenize 拆开，**其中 140 行是 docstring**，
    **真实代码只多了 4 行**（277 → 281）。删掉的样板 49 行、加的接缝 97 行。
    与 Matrix / Telegram 同向"变多"，但这家的增量**几乎全在解释"为什么"**。
  - lane 主动标注的诚实项：`stop()` 时会多一条 `transport[slack]: 会话出错` WARNING
    （唤醒阻塞 `recv()` 以异常收场，**功能无影响**）；防走偏前缀的 `assert` 在
    `python -O` 下会被剥掉（所有 assert 都如此，真要硬保证应改显式 `raise`；
    matrix/telegram 同款，保持一致故未改）。
  验证：**test_slack 29 OK**、跨平台 **644 OK**、compileall 0。
- **2026-10-03** **B2 qqbot 完成**（第十二个平台）+ **修掉一个高影响真 bug：`ws.py` 丢首帧**
  - qqbot（1094 行 + 113 用例，走 `WebSocketTransport` + 自研 `ws.py`）。
    **我在派单时的两条推断都被证伪，已在阶段 B 表格里更正**：
    ① "Discord 风格变体"只对一半——opcode 表像，但**鉴权完全不同**：QQ 的握手
       **不带任何凭据**（匿名），靠连接后 op 2 Identify 传 `QQBot {token}`；
    ② **"防回环天然"不成立** —— `GROUP_AT_MESSAGE_CREATE`/`C2C_MESSAGE_CREATE`
       确实是"用户→bot"，但 `GROUP_MESSAGE_CREATE`（同一 intent、另一个后台开关）
       文档写的是"群里的**每一条**消息"，**不承诺排除 bot**。改用平台签发字段：
       `author.bot === true` 或 `author.id == READY.user.id`（**绝不用内容启发式**）。
    - **`heartbeat_interval` 单位是毫秒**（与 Discord 同一个坑），lane 用**三层证据**确认：
      官方文档两处明写毫秒 + 同一 `45000` 示例；代码里换算时**同时打印原值与折算值**；
      真服务器上**双向**验证（400ms 必须出第二拍、45000ms 必须不出）。另加了
      `[100, 600000] ms` 的钳制（我自己定的范围，文档未给，防止 0 变忙等）。
    - **诚实标注为"推断"而非事实**：沙箱域名（文档只有图片，查不到）、消息长度上限
      （**官方根本没给数字也没给单位**，`MESSAGE_LIMIT=2000` 是自行保守取值并在代码里
      标注 + 用测试守住那句免责声明）、close 码语义、群/私聊 `author.id` 与 READY id
      空间不同（故只在 guild 范围依赖该比较）。真实溢出会映射成
      `SendError.TOO_LONG`，**可观测、不静默**。
  - ⚠️ **高影响真bug（本轮最重要的收获，来自 lane 对 `ws.py` 的越权举报）**：
    `_read_http_response()` 为找 `\r\n\r\n` 会多读出后面的字节，而 `_check_handshake()`
    只在"握手被拒"分支用到它们 —— **走101 成功路径时直接丢弃**。
    于是服务端若把 101 响应与第一帧放进**同一个 TCP 段**（**流水线化**），
    那一帧被**静默吞掉**，客户端连得上却**永远收不到消息**，直到读超时。
    **症状是"平台连上了但一条消息都收不到"，且不报错** —— 极难定位。
    影响 **Discord / Slack / Mattermost**（Discord 网关确实会流水化第一帧）。
    - **我没有采信它的结论，自己复现**：真 socket 服务器一次 `sendall` 发
      握手 + 第一帧 → `recv()` **1.5s 后超时**；对照延迟 50ms 单独发 → **0.046s 拿到**。
    - 修法：`connect()` 把多读字节存进 `_prefetch`，`_read_frame` 经`_read_bytes()`
      **优先消费**它。修复后同一复现脚本 **0.000s** 拿到帧。
    - 回归测试 `tests/test_ws.py::TestPipelinedFirstFrame`（4 条），其中一条专门断言
      预读缓冲**恰好消费完、不丢也不重复** —— 重复是最隐蔽的失败模式（同一条消息被处理
      两次，例如触发两次 agent 运行）。
    - **顺带暴露并修掉一个测试反模式**：`test_mattermost.py` 用
      `WebSocketClient.__new__()` **绕过 `__init__`** 再手工赋 13 个私有属性，
      于是我新增一个实例字段就让它`AttributeError` 挂掉 —— 而挂掉原因与它测的
      ping/pong 保证毫无关系。已改用正式 `__init__`（它本来就不做连接），
      13 行手工赋值缩成 2 行，**这个地雷从根上拆掉**。
    - **顺带退役一处 workaround**：qqbot 测试里的 `FIRST_FRAME_DELAY = 0.05`
      正是为绕开此bug 加的，而那条守卫用例还**断言"设成 0 就会失败"**——
      它锁定的正是现已修好的 bug。已把延时置 0（**让 qqbot 整套真服务器测试顺带
      覆盖"同段"这条路径，即现实里服务端的行为**）并把守卫用例**反转**为正向断言。
  验证：**qqbot 113 OK**、**全量 1296 tests OK (skipped=1)**、compileall 0、12 个平台。
- **2026-10-03** **跨适配器机械审计**（应用户要求"审视已完成的功能是否存在 bug"）：
  写脚本对 12 个适配器逐项检查这一整轮反复暴露的风险类，而不是靠回忆逐个翻文件。
  - **发现 1 个真 bug**：`ntfy` 与 `email` **继承了基类的 `running`**，而基类读
    `self._thread` —— 这两家把线程交给了 `Transport`、**从不设** `_thread`，
    于是 `capabilities()["running"]` **永远是 False**，`--status` /
    `--setup --json` 会把一个**正在收信**的平台报成"没在跑"。
    属于本项目反复修的那一类"**状态被误报**"（此前修过"配了但报未配置"、
    "发不出去"、"入站没通"）。
    实证：修复前 `ntfy` 在 `start()` 之后仍报 `running=False`；修复后
    `start()` → True、`stop()` → False，与 irc 一致。
    - **新增跨适配器回归测试**（`TestTransportBasedRunningIsReported`，2 条）：
      凡源码里出现 `_transport` 的适配器**必须**在自己的 `__dict__` 里覆写
      `running`，并列出 `ntfy/email/irc/telegram` 作为"本用例确实覆盖到了"的断言；
      另有一条**反面对照**保证"未迁移、仍用 `_thread` 的平台继承基类是对的"。
      **并且验证了这条测试确实有效** —— 临时删掉 ntfy 的覆写，用例立刻失败。
  - **审计的 2 处误报也如实记录**（机械检查的局限）：
    ① `a2a: 未接transport` —— **设计使然**，它是服务器不是轮询器，走 `httpsrv.py`；
    ② `ntfy: start() 未 clear _stop_event` —— **不是 bug**：ntfy 的 `stop()` 既不调
       `super().stop()` 也从不碰 `_stop_event`，而 `start()` 每次都新建 transport，
       所以那个陈旧 event 毫无影响。这条与我在 Slack 上修的"stop 后重启静默不工作"
       表面相似、机制不同 —— **不能看到同一个 grep 命中就照抄结论**。
  - 审计顺带确认无问题的项：凭据不变量 12 家全过（含 `config_optional` 只有 a2a
    为真）、能力声明无谎报、`edit()` 返回注解均为 `bool`、12 家失败路径都有记账。
  - **同时补齐了 4 个计划缺口**（应用户要求"未实现的功能是否有安排好的执行计划"）：
    ① 阶段 A 表格里**根本没有 "`state.json` 键迁移"这一行** —— 而它是 5 个平台
       （telegram/matrix/slack/discord/mattermost）切前缀的**硬前置**，之前只作为
       Matrix 进度日志里的一句背景提到，**没有被排期**。已补为 **A2b** 并写清
       "这不是格式美化，是数据迁移"+ 歧义前缀那多出来的一层；
    ② **A2 的进度数字过时**（写着 1/9），现已按"映射到自身（零风险）"与
       "切换会改键格式（须先有 A2b）"**分类**列出；
    ③ **A1 也没记下 Nextcloud 不可迁移的理由**（5 worker 轮转池），导致"下一个"
       看起来像还有 5 家；现明确剩余为 Discord → Mattermost → Twitch（3 家）；
    ④ 新增 **A4 真实服务端端到端验证** —— 这是**当前最大的已知风险**：
       12 个平台的协议事实全来自官方文档 + 参考源码 + 本机假服务器，
       **但没有任何一个对着真实服务器跑通过**（手上无 token）。已写明三类残余风险
       与"跑通一个平台的性价比高于再加一个平台"的判断。
  验证：**全量 1298 tests OK (skipped=1)**、compileall 0。

- **2026-10-03** **A2 的零风险部分收官**：twitch 与 nextcloud 的 `_conversation_id`
  从手写 `f"prefix:{x}"` 改走 `identity.format_id`（与 IRC 同款）。
  - **零风险的根据**：这两家在 `LEGACY_PREFIXES` 里**映射到自身**，所以产物与原来
    **逐字节相同** —— 已落盘 `state.json` 不受影响，用户无感。
    实测覆盖空值类、**含冒号的 id**（local 段允许冒号）、CJK、超长串，全部相同。
  - **`_target()` / `_token()` 刻意保留手写剥前缀**：它们必须容忍**无前缀的裸
    target**，而 `identity.local_of()` 对那种输入会抛错 —— 换成它就是行为变更。
    这与 IRC 迁移时的判断一致。
  - 新增**跨适配器**回归断言（`TestSelfMappingPrefixesStayByteIdentical`，3 条），
    放在 `test_identity.py` 而非各平台测试文件 —— **一处覆盖全部三家**，且含一条
    **反向断言**确认它们真的改走了 `format_id`（而不是碰巧输出相同）。
    还钉住了那个前提本身："这几个前缀在登记表里映射到自身" —— 若哪天有人把它改成
    指向别的平台，"零风险"就不成立了，用例会立刻失败。
  验证：**223 OK**（twitch + nextcloud + identity + routing）、compileall 0。

- **2026-10-03** **B2 homeassistant 完成**（第十三个平台）+ **复核发现并修掉一个
  「状态说就绪、实际不工作」的可用性缺陷**：
  - `adapters/homeassistant.py`（约 1000 行）+ 42 个用例（真 WebSocket 服务器，
    服务端独立实现 `base64(sha1(key+GUID))` 与 `ws.py` 各算一遍）。
  - **协议事实逐条对照官方文档与 HA 源码**（`websocket_api/const.py`、`auth.py`、
    `commands.py`、`messages.py`、`auth/permissions/events.py`、`connection.py`），
    几个反直觉点：握手**不带任何鉴权**（服务端先发 `auth_required`）；`auth` 消息
    **不能带 `id`**（`vol.Exclusive`）；命令 `id` 必须**严格递增**否则 `ERR_ID_REUSE`；
    `event_type: "*"` 通配订阅**需要管理员**；`ActiveConnection.context()`
    固定返回服务端自己的 user id ⇒ **不能**用平台签发字段给事件打回环标记。
  - **保活分两层且方向相反**（本项目此前只有 Mattermost「服务端 ping」与 Twitch
    「客户端主动发」两种，这次是**同一个平台两层都要**）：
    传输层 = aiohttp `heartbeat=55` 发 RFC 6455 ping ⇒ `ws.py` 自动回 pong、零代码；
    应用层 JSON `ping` HA **只回不主动发** ⇒ 必须客户端主动发。两者都实现了，
    并有用例断言"服务端只收到客户端发来的 ping"。
  - **「设备事件管道算不算对话」我要求它先回答**：结论是**默认不成立** ——
    HA 推的是设备状态变更，只有能追溯到**真人用户操作**的事件才适合起对话
    （定时器/脚本触发的事件 `context.user_id` 为空）。显式化成三条默认保守的规则：
    ① 默认**一个事件都不收**（须给 `entities`/`domains`/`accept_all`）；
    ② `require_user_context` 默认 `True`；③ 防回环用"我们刚调过哪些实体"的动作记录
    做 10 秒窗口抑制（因为平台签发字段不可用，见上）。
  - `edit()` 恒 `False`，依据是**核实过的**：`persistent_notification` 组件只注册
    `create`/`dismiss`/`dismiss_all`，`dismiss` 是删掉而非改内容；命令表里也没有
    "改一条已有内容"的命令。
  - lane 主动做了**10 个反向变异**并全部被抓到（谎报能力、跳过闸门、令牌进日志、
    去掉回声抑制、`edit` 返回 True…），**主动验证自己的测试有效**，而不是只报"全绿"。
  - ⚠️ **复核发现的缺陷（本轮最值得记的一类）**：只配齐 `required_tokens`
    （`url`+`token`）后，`--status` 显示「已配置 / **入站就绪**」，而适配器默认
    **丢弃全部事件** —— **状态视图说就绪，实际收不到任何东西，且不报错**。
    这与本项目修过多次的"配得完全正确却被判成不可用"同族。
    启动日志里确实有 WARNING，但只查 `--status` 的人看不到。
    **修法**：`capabilities()` 覆写并暴露 `inbound_accepts_anything`（机器可读判据，
    `--status --json` / `--setup --json` 直接可读）+ 过滤条件计数 +
    `require_user_context`；`docs/install.md` 与 `plugin/README.md` 都写明
    "入站就绪 ≠ 会收到消息"。
    新增 4 条用例，其中一条断言**快照判据与运行时 `_accepts_everything()` 一致**
    （防两套逻辑分叉），并**验证过它能抓住谎报**（把判据写死为 True → 用例失败）。
  - **我自己漏掉的文档同步**：这条 lane 被禁止改 README/install.md，所以
    qqbot 与 homeassistant 的接入文档我**当时没补**（`docs/install.md` 与
    `plugin/README.md` 连 a2a/qqbot/homeassistant 三家都缺）—— 这次一并补齐，
    并在 install.md 里点明"入站就绪 ≠ 会收到消息"。
  验证：**homeassistant 42 OK**、compileall 0、**十三个平台**。

- **2026-10-03** **A2b `state.json` 键迁移完成**（`state.py` 136 → 488 行 + 33 个用例）：
  - lane **没有替我静默决定一个冲突**，而是把两个需求冲突原样报上来，这个态度比
    "全绿"有价值得多（裁决见下）。
  - **歧义 `channel:` 的策略：原样保留，一个字节都不改。** 分类只调
    `identity.normalize(key)`、**永不传 `platform_hint`** —— 传了就等于编造一个
    store 并不掌握的线索。测试用**三种形状刻意可区分**的 local id
    （`channel:C123` / `channel:123456789012345678` / `channel:abcdefghijklmnopqrstuvwxyz`）
    钉住这条：即使形状可区分也**不区分**。理由：形状启发式（Slack `C`/`D`、
    Discord 全数字、Mattermost 26 位 base32）**正是 `identity.py` 明文禁止的那一套**，
    而猜错的后果是把用户映射到**别人的会话**上 —— 不报错、难复现、已被污染。
    保留的代价几乎为零：这些键继续走适配器现有的旧路径工作，只是没有"统一格式"这个属性。
  - **碰撞策略同源**：目标键已被占用时**两份都留**，绝不覆盖、绝不合并。
  - **原子性**：先纯内存重写（`migrate_document` 不碰磁盘）→ `shutil.copy2` 备份
    → 走既有的 `mkstemp` + `flush` + `fsync` + **`os.replace`**（Windows 与 POSIX 上
    都是原子的，文件要么全旧要么全新，**没有第三种状态**）。
    **备份失败就整趟放弃**（没有备份 = 没有回滚路径 = 不动用户的文件）。
  - **幂等靠两套机制，且正确性不依赖标记**：(a) **基于内容** —— 重写后不再有可无歧义
    迁移的键，于是报告 `changed=False`，不写盘也不备份；(b) 版本闸门 ——
    `schema_version >= 2` 直接跳过扫描。标记**故意不声称"所有键都是新格式"**
    （保留的 `channel:` 是有意为之）。
  - **损坏文件**（JSON 解析失败）打 ERROR 且**既不迁移也不重写**，留给用户手工抢救。
    ⚠️ 这里有个与既有契约的张力：既有 `StateStore(path)` 遇到损坏文件会得到空 store，
    而后续 `set_session` **会**覆盖它（`tests/test_opencode_client.py:536` 有断言）。
    lane 把"不覆盖"的范围限定在**迁移路径**，显式写入保持旧行为——这个边界划得对，
    但若意图是"损坏文件一律永不覆盖"，那是 `set_session` 的行为变更，需要单独决策。
  - **超出字面要求的一处**：因为适配器目前还在发 `chat:`/`room:`，若只迁文件，
    迁移期每次查找都会落空 —— 正是 A2b 要防的症状，只是换了个位置。
    于是 `_lookup` 加了"精确键 → 无歧义别名"的一次回退（**歧义前缀没有别名，
    所以它不会猜**），前缀切换后这段自动变成死代码。lane 核实了这不影响路由。
  - ✅ **裁决（我确认 lane 的判断正确）**：它把 `migrate_keys` **默认设为关**，
    因为**四个既有用例**（`test_telegram:193` / `test_matrix:596` / `test_slack:325` /
    `test_discord_gateway:851`）断言的正是"切前缀会让会话映射丢失"，自动迁移会让那四条
    断言变假，而其中两个文件属于当时在跑的 lane。默认关闭 ⇒ **零行为变化**、
    文件逐字节不变（已实测），且符合仓库自己的规矩"切换必须与键迁移一起发"。
    **后续动作**：翻转前缀的那一个变更里，必须同时（a）传 `migrate_keys=True`、
    (b) 改写那 4 条用例、(c) 删掉 `_lookup` 的别名回退。三件事**同一个 commit**，
    否则仓库停在半迁移态 —— 那正是"agent 突然忘事"的成因。
  - 复核（我独立实测）：默认路径**文件逐字节不变、无备份、无临时文件**；
    opt-in 后三种形状的 `channel:` **全部原样保留**、`chat:`→`telegram:`、
    `room:`→`matrix:`、`meta` 与 `sessions` **同键映射不漂移**；
    **幂等**（再加载两次文件逐字节不变、只产生 1 个备份）、`schema_version=2`；
    **损坏文件原样保留且未被覆盖**并打 ERROR。
  - lane 报的一条全量偶发失败（在我那条 homeassistant 用例上）我**独立排查了**：
    连跑 15 次 + 全套 5 次**均 0 失败** —— 那次是它的全量跑撞上了另一条 lane 正在
    写 `homeassistant.py` 的窗口，不是真flake。它没去追一个不属于它的根因，判断正确。
  验证：**test_state 33 OK**、identity+core+routing+cli **133 OK**、
  legacy 前缀适配器组（telegram/matrix/slack/discord/opencode_client）**215 OK**、
  全量 **1404 OK (skipped=1)**、compileall 0。
  文档：修正 `docs/architecture.md` 里过时的"`state.py` 136 行"（现 488 行），
  并把不变量 9 从"提醒"升级为指向具体机制（含半迁移态为何危险）。
