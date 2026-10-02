# opencode-bridge 路线图 · tasks.md

> 本文件是**执行台账**：每完成一项就地勾选并追加进度日志。
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
| A1 | **传输层抽象**：`opencode_bridge/transport/` —— `HttpPollTransport`（长轮询）/ `IntervalTransport`（短轮询）/ `WebSocketTransport`（包 `ws.py`）/ `TcpLineTransport`（行协议）。基类统一线程、指数退避、**先关连接再 join 的 stop 语义** | L | 四个 transport 各自有测试；迁移 2~3 个现有适配器后行为**不变**（现有 703 用例全绿） | ☑ 包已建成（56 用例）；**迁移进度 2/10：IRC ✓ Matrix ✓**（下一个：Telegram / Slack） |
| A2 | **会话标识统一**：`opencode_bridge/identity.py` —— `platform:local_id`，提供 `format` / `parse` / `platform_of` / 校验；**向后兼容**已落盘的 `chat:` `channel:` `room:` 旧格式 | S | 各适配器不再自造前缀；旧 `state.json` 仍能读；跨平台同名 chat id 不再混淆 | ☑ 模块已建成（24 用例）；**迁移进度 1/9：IRC ✓**（`irc` 前缀在 `LEGACY_PREFIXES` 里映射到自身，故 `conversation_id` 字节级不变，已用断言钉死） |
| A3 | **inbound-push 入口**：单端口 HTTP 服务 + 按路径路由到适配器（webhook 类平台的唯一可行入口） | M | 起一个本地 HTTP 服务，两个 webhook 适配器能各自收到 POST 并鉴权；停机干净 | ☐ |

---

## 阶段 B · 平台接入波次（按难度由易到难，全部来自两家清单的并集）

难度判据：**纯标准库可行 + 不需公网回调 + 活跃度** 三者同时满足才进 B 组。

| 波次 | 平台 | 入站机制 | 难度 | 关键点 / 风险 |
|---|---|---|---|---|
| **B1** | ntfy | HTTP 拉取（`poll=1` + `since` 游标） | S | 已完成。**偏离原计划**：不用长连接流而用一次性拉取 —— 持久流在 `urllib` 下无法干净打断（`stop()` 会白等超时，见 Nextcloud 的教训）。启动用 `since=<当前时间戳>` **不重放历史缓存**（否则首次启动会把最多 10MB 缓存全当新消息触发 agent）；之后游标推进到 **message id** |
| **B1** | email | IMAP 轮询（`UID` 游标） | S | 已完成。`imaplib`/`smtplib` 全标准库，协议通用永不废弃；**必须用专用邮箱 + app 专用密码**；**无用户身份**（任何能发信给你的人都能驱动 agent）→ 必须用 `allowed_chat_ids` 限定发件人；不支持编辑 |
| **B1** | a2a | 本地 HTTP server（**我们是被调方**） | S | 方向与其他平台相反；默认 bind 127.0.0.1 **天然满足"不需公网"**；Hermes 已验证纯 `http.server` 无需 SDK |
| **B2** | qqbot | WebSocket 网关 | M | 协议是 Discord 风格变体（op 码 + intents + `heartbeat_interval`），可直接复用 `ws.py`；防回环天然（bot 消息不推回给自己） |
| **B2** | homeassistant | WebSocket 事件总线（本机） | M | WS 极简；HA 极活跃。**但它是设备事件管道不是 IM**，取决于定位是否要收 |
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

