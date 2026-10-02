# opencode-bridge 路线图 · tasks.md

> 本文件是**执行台账**：每完成一项就地勾选并追加进度日志。
> 设计依据见 [`docs/platform-design-reference.md`](docs/platform-design-reference.md)（Hermes / dsh-im-gateway 三方对比）。
> 扩展前稳定点：tag `backup/pre-platform-expansion-20261002`（`06a7d2f`）。

## 目标

**breadth 优先**：把 opencode-bridge 做成能接通各大消息平台的桥。
横向前提（授权 / 能力声明 / 分片 / 可观测）是"加平台"的杠杆，不是可选项。

## 难度口径

| 档 | 含义 |
|---|---|
| **S** | 纯函数或局部改动，不破坏现有行为 |
| **M** | 跨文件 + 新抽象 + 测试 |
| **L** | 破坏性迁移或跨层状态机 |

---

## 阶段 1 · 共用层加固（加平台的前置）

| # | 任务 | 难度 | 验收 | 状态 |
|---|---|---|---|---|
| T1.1 | **能力显式声明**：`max_message_length` / `supports_inline_buttons` / `typed_command_prefix` 等类属性，三平台各填真值 | S | 各适配器单测断言自己的上限；调用点不再靠 try/except 猜 | ☑ |
| T1.2 | **授权层统一到 `base.py`**：三平台共用同一入站闸门；**审批/命令类消息必须在闸门之后** | M | 三平台共用同一判定函数；无白名单时入站被拒；Slack/Discord 具备与 Telegram 同等的白名单能力 | ☑ |
| T1.3 | **出站错误分类**：`SendResult` + 平台中立 `error_kind`（7 类），替代现在全吞 | M | 失败可观测：`--status` / 日志能看出哪个平台哪次失败 | ☐ |
| T1.4 | **分片算法**：码点切分 + 断点优先级 + `（i/n）` 前缀两遍法重编号 | S | 长文本/emoji/中文断行测试全绿 | ☐ |
| T1.5 | **状态四态归一 + `--status`**：已连接/连接中/异常/未连接，`--status` 汇总各平台 + 运行时 | S | `--status` 一次给出服务连通 + 各平台状态 + 锁/最近退出 | ☐ |

**拆分原则**：每项独立可发布、可验证、独立 commit。

---

## 阶段 2 · 补齐已有平台的入站（最快见效）

| # | 任务 | 难度 | 要点 | 状态 |
|---|---|---|---|---|
| T2.1 | **Slack 入站** | M | Socket Mode（`apps.connections.open` 换 WSS）；**每个 envelope 必须 ack**（否则 Slack 重发）；3s 重连 | ☐ |
| T2.2 | **Discord 入站** | M | Gateway v10 WS + heartbeat + 事件去重；REST 侧复用现有发送 | ☐ |

---

## 阶段 3 · 零依赖平台族（每家 0.5~1 天）

| # | 平台 | 难度 | 传输方式 | 状态 |
|---|---|---|---|---|
| T3.1 | Matrix | S | `/sync` 长轮询 + `next_batch` 游标 | ☐ |
| T3.2 | Mattermost | S | WebSocket + `authentication_challenge` + ping | ☐ |
| T3.3 | IRC | S | `socket` 手写客户端 + PING/PONG + 仅响应提及 | ☐ |
| T3.4 | Twitch | S | WebSocket IRC + IRCv3 tags + 限速节流 | ☐ |
| T3.5 | Nextcloud Talk | S | REST 轮询 | ☐ |

---

## 阶段 4 · 横向增强（平台越多越值钱）

| # | 任务 | 难度 | 收益 | 状态 |
|---|---|---|---|---|
| T4.1 | **入站合并窗口** `..`/`!!` + 5s 超时 + 长输入回执（**含快照落盘**） | M | 手机长按拆条不再变成 N 次提问 | ☐ |
| T4.2 | **prompt hint 注入**（借 `ctx.session.hook("context")`） | S | agent 知道消息长度上限 / 无 markdown 渲染 / 回复非即时 | ☐ |
| T4.3 | **脱敏引擎** | M | 手机号 / token / chat id 不再明文进日志与 `state.json` | ☐ |
| T4.4 | **审批超时回落 + 迟到回答去重** | S | 窗口关闭后随口一句不会被误当批准 | ☐ |

---

## 阶段 5 · 主动能力

| # | 任务 | 难度 | 依赖 | 状态 |
|---|---|---|---|---|
| T5.1 | 渠道地址簿（"我在哪些群"，仅索引已连接平台） | M | — | ☐ |
| T5.2 | 主动发送 `bridge_send` 工具 | M | T5.1 | ☐ |
| T5.3 | 富交互按钮覆盖三平台 | M | T1.1 | ☐ |
| T5.4 | 定时任务绑 chatId（**opencode 无 cron，需自建日历 + tick + 落盘**） | L | T5.1 | ☐ |

---

## TODO · 有真实门槛，暂不排期

| # | 项 | 门槛 | 状态 |
|---|---|---|---|
| TODO-1 | **WhatsApp** | 官方无 Bot API；dsh 依赖 `baileys`（Node 生态重型库）。Python 侧需另选方案（自研 WebSocket 逆向 / 第三方网关服务 / 桌面端桥），**未评估可行性** | ☐ |
| TODO-2 | LINE / Google Chat / Zalo / Teams | 需**公网回调地址**（隧道 / 备案域名），非纯本地可解 | ☐ |
| TODO-3 | 飞书 / 钉钉 / 企微 | 官方签名校验与加解密，各自 1~2 天 | ☐ |
| TODO-4 | 微信 | 需专用账号 + 设备扫码，账号风险自负 | ☐ |
| TODO-5 | iMessage | 仅 macOS，依赖 `imsg`/`osascript` | ☐ |

---

## 不做（已决策）

| 项 | 原因 |
|---|---|
| 配对码（陌生人配对流程） | 一整套限流/TTL/锁定状态机；等到真要在群里开放给陌生人再上 |
| 25 渠道全量 / 平台插件化 | 方向是"做深"不是"做多" |
| 实例锁 | 纯插件进程，无共享 home 概念 |
| 默认改显式 opt-in | 会破坏"配好即用"现状；若要改需单独决策（迁移期 + 警告期） |

---

## 进度日志

- **2026-10-02** 备份完成：tag `backup/pre-platform-expansion-20261002`（`06a7d2f`），远端仍在 `91c4478`。开始阶段 1。
- **2026-10-02** T1.1 + T1.2 完成（未推送）：`base.py` 新增能力类属性与 `capabilities()` 快照、
  统一授权闸门 `admits()`（认 `allowed_chat_ids`/`allowed_chats`/`allowlist` 三种键，
  空白名单=全开保持 v1 语义）；三平台按真值声明能力（telegram 4096/入站/按钮/媒体，
  slack 40000 仅出站，discord 2000 仅出站）；telegram 的 `_allowed` 改为委托基类。
  新增 10 个用例（`TestCapabilities` / `TestAccessGate`），全量 **123 tests OK**、compileall 0。
