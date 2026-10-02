# 消息平台设计参考：Hermes × dsh-im-gateway × opencode-bridge

本文是**对比报告**，不是使用手册。目的：把两个成熟参照系的平台层讲清楚，逐项标出
opencode-bridge 该跟什么、跟到什么程度。

参照系：

- **Hermes**（本机已安装）——"平台 + agent 一体"，自建 prompt/tools/cron/session。
- **dsh-im-gateway**（`zhuiyueya/dsh-im-gateway`，49⭐ MIT TS）——同类竞品，
  DeepSeek Harness 的 IM 聚合网关，25 渠道。本地只读 clone 后侦察。

引用格式：`路径:行号` 相对各自仓库根。Hermes 侧与 dsh 侧结论均可在文件里复核。
取证方式：三轮只读侦察（Hermes 内核+授权 / Hermes 上层集成 / dsh 通道层 / dsh 核心引擎）
+ 人工阅读 `gateway/platforms/ADDING_A_PLATFORM.md`、`dsh README.md`。**非**逐行通读。

---

## Part 1 · 三种架构，一句话各一

| | 核心取向 | 一句话 |
|---|---|---|
| **Hermes** | 富基类 + 注册表 + 插件化 | 4 个 abstractmethod 打底，约 4900 行基类承载重试/媒体/会话闸门/授权回调装配；能力用**类属性常量 + 方法查询 + 模板方法 + feature-detect** 四种机制混合表达 |
| **dsh-im-gateway** | 极简契约 + 共享引擎 | 工厂函数返回 7 字段结构化对象；25 个渠道各约 100 行只做"协议 ↔ ImMessage"翻译，split/merge/路由/命令/审批全在网关侧 |
| **opencode-bridge** | 桥（只做转换） | 3 个平台、Python 侧无外部依赖；把 IM 消息转成 opencode 的 session prompt。平台层最薄，但身份模型与授权层也最薄 |

**关键认知**：Hermes 与 dsh 是**两种极端**，我们的问题不是"抄哪个"，而是
**按需取用哪几块**——它们各自的强项恰好是我们的缺口，反之亦然。

---

## Part 2 · Hermes 侧要点（详见早期结论）

四层分离：`① 适配器内核 / ② 身份与路由 / ③ 授权闸门 / ④ 上层集成`。

必须记住的四条工程纪律（它们比机制本身更值钱）：

1. **`SendResult.error_kind` 平台中立错误分类**（`gateway/platforms/base.py:1726-1782`）——
   7 类固定枚举 + `classify_send_error()`，消费方永不 substring-match 厂商报错。
2. **`build_session_key()` 唯一真源 + 适配器必须走 seam**（`gateway/session.py:682-723`）——
   runner 盖章 `profile` 在入站之后，直接调自由函数会让队列/busy 检测落到永不 pop 的 lane
   （`tests/gateway/test_adapter_session_key_seam.py` 钉住）。
3. **出站守卫的极性纪律**（`tools/send_message_tool.py:66-151`）——授权检查返回 `None`
   等于"已授权"，故"模块缺失（放行）"与"模块故障（拒绝）"必须严格区分。
4. **遮蔽结果不可复用哨兵**（`agent/redact.py:789-799`）——head/tail 掩码长得像被截断的真 key，
   agent 读回 config 再写回会永久损坏凭据（真实事故 #35519）。

⚠️ **Hermes 自己的文档已过时**：`ADDING_A_PLATFORM.md` 的 §1/§3/§7/§8/§11/§12 全部落后于代码
（平台适配器已迁 `plugins/platforms/`、工厂已变注册表、toolset 隐式生成…）。照抄会写进死代码。

---

## Part 3 · dsh-im-gateway 侧要点

### 3.1 通道契约（`src/core/types.ts:46-74`）

工厂函数返回结构化对象，非抽象基类：

```ts
createXxxChannel(config, log, stateDir?): ChannelAdapter | undefined   // 凭据缺失即 undefined
```

必选 7 个：`id` / `label` / `maxMessageLength` / `start` / `stop` / `send` / `setMessageHandler`。
可选 5 个：`sendAction?`（typing）/ `sendMedia?` / `loginUrl?` / `authorizes?`（三态）/ `status?`。
调用点全部 `?.` 优雅降级。

**能力表达 = "方法存不存在"（duck typing），不是常量标志位。**
实测覆盖：`sendMedia` 仅 1/25、`sendAction` 3/25。代价是**能力不可枚举**——
想知道"哪些渠道支持发图"只能遍历 25 个对象探测。

### 3.2 入站流水线固定 6 步（`src/core/gateway.ts:388`）

```
0 恢复该 chat 上次会话（让 /status 显示真状态）
1 `/` 命令 —— 不经合并窗口
2 「批准/拒绝」值匹配 —— ⚠️ 在白名单之前
3 白名单：渠道 authorizes? 三态 → 否则网关全局
4 ask_user_question 回答消费
5 媒体直接注入
6 合并窗口 → 注入 agent
```

**核心层没有去重**（`ImMessage` 连 `messageId` 字段都没有，`types.ts:24-37`），
去重只在渠道层（nostr 的 `seen` Set、wechat 的轮询游标）。**也没有 rate limit**。

### 3.3 会话路由（`src/core/router.ts`）

- 键 = `` `${channelId}:${chatId}` ``（`:102`），**只有两个维度**，无 chat_type/user_id/thread_id。
- sessionId = `` `im:${channelId}:${chatId}:${Date.now()}` ``（`:129`）。
- 多 chat 可共享同一 session：反向索引 `bySession: Map<sessionId, Set<ChatEntry>>`（`:59`），
  `/continue`（`:190`）、`/bind`（`:243`）都能让多个 chat 指向同一 session，出站广播到全部
  （`tests/gateway.test.mjs:606-639` 钉住微信+飞书互见）。
- 群私聊隔离**网关零防御**，靠渠道给的 chatId 天然不同。

### 3.4 交互桥（approval / questions）

| | approval | questions |
|---|---|---|
| 形态 | waterfall 钩子（`gateway.ts:173-177`） | 猴补 `userQuestions.ask`（`:914`） |
| 呈现 | **纯文本关键词**，10 个精确等值（`:402-409`） | 编号文本，多问题要 `序号: 答案`（`questions.ts:94-107`） |
| 超时 | 120s → 回落本机批准体系 | 600s → 广播"IM 窗口已结束"后交回 Web |
| 并发 | 任一 allow → `allowed-once`；**allow 判定在 reject 之前**（`:1019-1030`） | **第一份有效答案生效**，其余渠道收到已回答通知 |
| 防误答 | 同 key 重复 wait 立即 undefined 不排队（`approval.ts:31`） | `recent` 30s 去重窗 + `looksLikeExplicitReply` 门（`questions.ts:86,144-158`） |

### 3.5 定时（`src/core/cron.ts` + `cron-time.ts`，270+156 行，零第三方库）

- 模型**不是 cron 表达式也不是 interval**：墙钟 `HH:MM` + 星期集合 + IANA 时区，`setInterval` 每 30s 扫表。
- DST 两遍法：重叠取较早瞬时、间隙跳过该日（`cron-time.ts:85-100`），366 天扫描。
- **投递目标绑 `channelId + chatId`，与 session 无关**（`cron.ts:1-3`）——
  这是它对"定时提醒"的根本解法：`/new` 轮换与重启都不丢。
- 失败**仅日志**，无 IM 侧上报（`cron.ts:225-229`）。
- ⚠️ 死配置：`cronMaxConcurrent` 声明于 `index.ts:66`，全仓无读取点。

### 3.6 其他值得知道的机制

- **合并窗口**（`merge.ts:119`）：`..` 续传 / `!!` 立即提交 / 裸文本 5 秒窗口；
  被刻意排除在命令/审批/提问/媒体四条旁路之外（`gateway.ts:395-428`）。
- **扫码开通**（`provisioning.ts:271`）：4 渠道 provisioner，二维码**本地生成**
  （`qrcode.toDataURL`），凭据回流后 owner 自动进白名单（`manager.ts:352`）。
- **状态四态归一**（`manager.ts:29-44`）：把 adapter 的自由文本 `status()` 收敛成
  已连接/连接中/异常/未连接 —— 对齐 Hermes"status 借用 gateway verdict"纪律。
- **实例锁**（`instance-lock.ts`）：`linkSync` 硬链接原子创建，释放需 pid+token 双匹配。
- **用户侧授权**是另一套：未授权 → 待批队列 → Web 面板跨渠道横幅一键允许（`client.js:339-357`），
  与 provisioning 完全不交叉。

### 3.7 dsh 自身的坑（反面教材）

| 问题 | 位置 |
|---|---|
| `toTelegramHtml` 死码，且 telegram 仍用 `parse_mode:'HTML'` 发送**已去 markdown 的纯文本** —— 残留 `<`/`&` 触发实体非法，靠 `.catch` 兜底重发 | `format.ts:41`、`telegram.ts:98` |
| `ChannelRuntime`（含 `ready` 位）声明即废弃，全仓零引用 | `types.ts:77-83` |
| merge 快照恢复**未接通**：`snapshots()` 零调用，启动遍历刚 new 的空 Map | `merge.ts:91-95`、`gateway.ts:155,163` |
| 默认 `allowAllUsers: true` | `index.ts:57` |
| 审批应答在白名单**之前**处理 → 未授权用户能打"批准" | `gateway.ts:401-409` vs `:411` |
| 4000 是"猜的默认值"而非各平台真实上限（13 家都是 4000） | 各 `channels/*.ts` |

---

## Part 4 · 三方并排对比

| 维度 | Hermes | dsh-im-gateway | opencode-bridge |
|---|---|---|---|
| 抽象风格 | 4900 行富基类 + 插件注册表（22 钩子） | 7 字段结构化对象 + 可选方法 | 4 abstractmethod + 3 适配器 |
| 平台数 | 23 core + 插件 | 25（3 为 stub，真实可用 22） | 3（2 无入站） |
| 能力表达 | 常量标志位 + 方法查询 + 模板方法 + feature-detect | 可选方法 duck typing | try/except 降级 + `Base.answer` 空 stub |
| 会话身份 | `SessionSource` 20+ 字段 + 唯一真源 | `${channelId}:${chatId}` | 裸 `conversation_id: str` |
| 多端共享会话 | 支持（profile route） | 支持（`bySession` 反向索引，跨渠道互见） | 无 |
| 授权 | 三段闸门 + PairingStore + allowlist 并集 | 一层白名单（默认全开）+ UI 一键批准 | 仅 telegram 白名单 |
| 出站错误 | `SendResult` + 7 类 `error_kind` | `send` 返回 void，失败全吞 | 无分类 |
| 富交互 | 原生按钮 + 回调 id 硬约定 | 纯文本编号 + 关键词 | 纯文字提示 |
| 主动发送 | cron/CLI/MCP（**明确非模型工具**） | `im_send_file` / `im_cron_*`（做成工具） | 无 |
| 定时 | cron 表达式 + 双闸防 env 枚举 + 四 lane | 墙钟日历手写 + **绑 chat** | 无 |
| 入站合并 | `EphemeralReply` 等 | `..`/`!!` + 5s 窗口 | 无 |
| 渠道地址簿 | 有（仅已连接平台） | 无 | 无 |
| 去重 | `MessageDeduplicator` | 核心无（只在渠道层） | telegram offset 游标 |
| 限流 | 有 | 无 | telegram backoff |
| 单实例 | 无此概念 | DSH_HOME 硬链接锁 | 单例锁（跨进程杀旧重启） |

---

## Part 5 · 借鉴清单（分级）

### ✅ 可直接抄（低成本高收益）

| # | 借鉴项 | 来源 | 解决什么问题 | 代价 |
|---|---|---|---|---|
| 1 | **`splitText` 码点切分 + 断点优先级 + 前缀两遍法重编号** | `split.ts:31-103` | 我们目前是简单截断，会切坏 emoji、会在断点不佳处硬切、可能出现"第 3/2 段" | 极低。纯函数 + 9 个现成测试可读 |
| 2 | **入站 `..`/`!!` 合并窗口 + 长输入回执** | `merge.ts:45-75`、`gateway.ts:566-569` | IM 长按输入把一句话拆成 3~5 条，逐条注入 → LLM 轮次 ×N、上下文碎裂、成本暴涨 | 低。纯函数。**但必须同时实现快照落盘**，否则就是 dsh 那个半成品 |
| 3 | **状态四态归一纯函数** | `manager.ts:29-44` | 让 `--status` / `/status` / 日志三处对"算不算连上"用同一 verdict（对齐 Hermes 纪律） | 极低。一个纯函数 + 一个测试文件 |
| 4 | **迟到回答去重 + 显式回复门** | `questions.ts:86,144-158` | 权限/提问窗口关闭后，随口一句话被误当答案；并发回答重复处理 | 低。30s 窗 + 一个判定函数 |
| 5 | **"首答生效 + 同 key 不排队"** | `approval.ts:31`、`questions.ts:35` | 多端并发时的竞态：重复回答、错答串到别的会话 | 低。干净的小取舍 |

### ⚠️ 需改造（决策可抄，实现要重做）

| # | 借鉴项 | 源 | 我们要做什么 |
|---|---|---|---|
| 6 | **出站错误分类** | Hermes `error_kind` | 引入 `SendResult` 类结构 + 统一 `classify_send_error`，否则失败不可观测 |
| 7 | **cron 绑 chat 不绑 session** | `cron.ts:1-3` | 这是"提醒不消失"的正解。但 opencode 侧是否有等价 scheduler 未确认；调度实现另选，只抄"绑定维度"这个决定 |
| 8 | **能力表达** | 两者都不适合直接抄 | 我们只有 3 平台且要向用户报"不支持"，应采用 **Hermes 式显式能力枚举**，而非 dsh 的 duck typing（无法枚举能力） |
| 9 | **活跃渠道目录** | Hermes `channel_directory.py` | "我在哪些群"是主动发送与 channel 绑 cron 的前置 |

### ❌ 不建议抄

| 项 | 原因 |
|---|---|
| `toTelegramHtml` 死码 + `parse_mode:'HTML'` 组合 | dsh 现存隐性 bug（HTML 实体） |
| 默认 `allowAllUsers: true` / 空 `allowed_chat_ids` 即全开 | Hermes 要求显式 opt-in 才开放；我们目前是 **opt-out**，见下节 P0 |
| 实例锁 `instance-lock.ts` | 我们是插件进程，无共享 home 概念。仅"pid+token 双匹配释放"可作通用防误删模式 |
| `cronMaxConcurrent` 死配置 / merge 快照半成品 | 反面教材：声明了不接，等于活文档误导 |
| Hermes 的配对码（PairingStore） | 一整套限流/TTL/锁定状态机；等真要在群里开放给陌生人再上 |

---

## Part 6 · 修正后的 opencode-bridge 优先级

上一版（仅基于 Hermes）需两处修正，并新增一项：

| 优先 | 项 | 变化原因 |
|---|---|---|
| **P0** | 授权层下沉到 `base.py`，三平台共用闸门 | Hermes 三段闸门 + dsh 都有白名单层，我们只有 telegram 一家。**且 dsh 证明了"审批在白名单之前"的危险** |
| **P0** | 脱敏（`redact.py` + 不可复用哨兵） | 两家都有，我们 0。且我们的 `/status` 会把 session_id 回显到 IM |
| **P0** | **默认开放改为显式 opt-in** | 我们 `allowed_chat_ids: []` = 全部允许，与 dsh 的 `allowAllUsers: true` 同为 opt-out；Hermes 是"没配 allowlist → pair/ignore，且 `_ALLOW_ALL_USERS` 要显式开" |
| **P1** | 分片算法（码点 + 断点 + 两遍法） | 直接可抄，成本极低 |
| **P1** | 权限/提问应答的去重与超时 | dsh 的 30s 窗 + 显式回复门，比我们现有实现更完整 |
| **P2** | `SessionSource` 化身份模型 | **破坏性**（会话映射变了 = IM 会话失忆），需迁移。三方都做了，维度上 dsh 最弱、Hermes 最强 |
| **P2** | 出站错误分类 / 能力枚举 / `--status` 汇总 | 中等成本，用户可感知 |
| **P3** | 入站合并窗口 / cron 绑 chat / 渠道目录 / 主动发送 | 功能增量；cron 依赖 opencode 侧 scheduler 能力（未确认） |

---

## Part 7 · 与两家都无法对齐的结构性差异

1. **Hermes/dsh 都是"自带 agent 的宿主内插件"**（Hermes 自建 prompt/tools/cron；
   dsh 依赖 cordis 注入的 9 个 host service）。opencode-bridge 是**跨宿主桥**——
   它只能通过 opencode 的 hook（`session.hook("context")`、`ctx.tool.transform`、
   `ctx.command.transform`）参与，无法自己装配 prompt/toolset。
2. **主动发送的立场相反**：Hermes 明确"不做成模型工具"（防 agent 乱发），
   dsh 做成了 `im_send_file` 工具。要做 `bridge_send` 必须自行裁决：
   opt-in + 目标白名单，还是不暴露给模型。
3. **平台数与扩展方向相反**：两家都做了"加平台很容易"（注册表/开关/4 步），
   而我们的扩展方向是"在已有 3 个平台上做深"（token 引导、审批、提问、定时）。
   借鉴时应偏向**做深**那侧的能力，而非**做多**那侧的抽象。

---

## Part 8 · opencode 宿主能力边界（决定难度上限）

实测 `opencode --help` + V2 插件契约（`/build/plugins`）：

| 宿主能力 | 有无 | 对我们的意义 |
|---|---|---|
| **cron / scheduler** | ❌ **完全没有**（顶层子命令无 cron/schedule/job） | 定时任务必须**自己实现 tick + 日历解析**（dsh 手写 156 行零库）→ 这是"提醒"功能的成本主因 |
| `ctx.session.hook("context")` → `event.system.push(...)` | ✅ | **平台 prompt hint 可直接注入**（Hermes ④ 层的一半由宿主提供） |
| `ctx.session.hook("prompt")` | ✅ | 可拦截/改写入站 prompt |
| `ctx.permission.list()` / `.reply()` | ✅ | 我们现用的审批回传路径 |
| `ctx.permission.hook("evaluate")` | ✅ | 可改判定的 effect（allow/ask/deny）——比"推送+等回复"更适合做白名单自动放行 |
| `ctx.tool.hook("execute.before"/"after")` | ✅ | 可拦截任意工具输入/结果 → **agent 向 IM 提问**可由此实现 |
| `ctx.tool.transform` | ✅ | 已用（`bridge_setup`）→ `bridge_send` 走同路 |
| `ctx.command.transform` | ✅ | 已用（`/bridge-setup`） |
| `ctx.storage`（scan/set/get，per-plugin 持久 JSON） | ✅ | 地址簿 / cron 状态 / 合并缓冲快照的落盘去处 |
| `ctx.session.synthetic()` | ✅ | 可把"提醒"作为合成消息注入会话 |
| TUI 插件（slots / panel / keymap） | ✅（独立技能域） | 状态面板可行，但属新技能域 |
| 多端共享一个 session | ⚠️ 由我们自己的键设计决定（宿主不干预） | 与 Hermes/dsh 同构，可做 |

**结论**：宿主缺 cron（要自建），但**注入面比 Hermes 预想的更强**——
prompt hint、工具拦截、持久存储都现成。这让"复刻两方优点"的实际成本比报告 Part 6 估的低。

---

## Part 9 · 功能清单与实现难度

难度口径：**S** = 纯函数/局部改动、不破坏现有行为；**M** = 跨文件 + 需新抽象 + 测试；
**L** = 破坏性迁移或跨层状态机。括号内为工作量粗估（单人日）。

### A · 安全与授权（两家共同强调，我们最缺）

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| A1 | **脱敏引擎**：token/手机号/ID 模式 + 赋值形态 + 4 个 chokepoint + 误伤门 + **不可复用哨兵** | Hermes | **M**（3~5d） | 纯 Python 无宿主依赖。必须做词边界与值形状门，否则遮蔽正常中文 |
| A2 | **授权层统一 + 三段闸门**：入站先过"渠道忽略"，再过网关白名单 | Hermes | **M**（2~3d） | 现有 `TelegramAdapter._allowed()` 下沉到 `base.py`；dsh 的教训是**审批应答必须在白名单之后** |
| A3 | **默认收紧为显式 opt-in** | Hermes | **S**（0.5d）+ **行为变更** | `allowed_chat_ids: []` 现在=全开。改默认会打破现有用户 → 需迁移期/警告期/配置版本号 |
| A4 | **配对码**（8 位码 + TTL + 限流 + 失败锁定 + 别名集） | Hermes | **M**（3d） | 只在"要开放给陌生人"时需要 |
| A5 | **Bot 回声防护**（按 conversation 计预算） | Hermes | **S**（0.5d） | 防 agent 输出被当成新输入回灌 |
| A6 | **`pii_safe`**：prompt 里 id 用确定性 hash、路由用原值 | Hermes | **S**（1d） | 依赖 A1 |

### B · 身份与会话

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| B1 | **`SessionSource` 结构化身份 + `build_session_key()` 唯一真源 + 适配器 seam** | Hermes | **L**（5~8d） | **破坏性**：`state.json` 键变更 = IM 会话失忆。必须带迁移 + 旧键回退。收益：跨平台不串、thread/话题可表达 |
| B2 | 多 chat 共享一个 session（`bySession` 反向索引 + `/continue`） | dsh | **M**（2d） | 依赖 B1 |
| B3 | **渠道地址簿**（"我在哪些群"，仅索引已连接平台） | Hermes | **M**（2~3d） | C1/C2 的前置；落盘用 `ctx.storage` |

### C · 交互质量

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| C1a | **出站分片算法**：码点切分 + 断点优先级 + 前缀两遍法重编号 | dsh | **S**（0.5d） | 纯函数，dsh 有 9 个测试可读。**最高性价比** |
| C1b | **入站合并窗口** `..`/`!!` + 5s 超时 + 长输入回执 | dsh | **M**（2d） | 纯函数 + 必须同时做快照落盘（dsh 就漏了这条） |
| C1c | **审批流加固**：超时回落本地 + 首答生效 + 同 key 不排队 + **迟到回答 30s 去重 + 显式回复门** | dsh | **S~M**（1~2d） | 我们已有 `/approve` 文字版，补超时与去重即可 |
| C1d | **交互式提问桥**（agent 向 IM 提问，多选/单选/自由输入，第一份答案生效） | dsh + Hermes | **M**（3d） | 借 `ctx.tool.hook("execute.before")` 拦截 ask 类工具 |
| C1e | **富交互 inline 按钮**覆盖三平台（审批/提问都用按钮 + 回调 id 约定） | Hermes | **M**（3d） | Telegram 已具备（`/setup` 菜单用过），Slack/Discord 需补 |

### D · 输出与可观测

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| D1 | **出站错误分类**（`SendResult` + 平台中立 `error_kind`） | Hermes | **M**（2d） | 否则失败不可观测（dsh 就是 `.catch(() => undefined)` 全吞） |
| D2 | **显式能力枚举**（`maxMessageLength` / `supportsInlineButtons` / `typedCommandPrefix`…） | Hermes | **S**（1d） | 平台少 + 要向用户报"不支持"，故取 Hermes 式而非 dsh 的 duck typing |
| D3 | **状态四态归一 + `--status` 汇总** | dsh + Hermes 共同纪律 | **S**（1d） | 已有 `--check`/`--setup --json` 雏形；纯函数 + 一处汇总 |
| D4 | **平台 prompt hint 注入**（长度上限 / 无 markdown 渲染 / 回复期望） | Hermes | **S**（1d） | 借 `ctx.session.hook("context")`，**宿主已提供注入面** |
| D5 | hint 可被用户覆盖（append/replace） | Hermes | **S**（0.5d） | 依赖 D4 |

### E · 主动能力

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| E1 | **定时任务绑 chatId 而非 sessionId** | dsh | **M~L**（4~6d） | opencode **无 cron** → 要自写日历（时区/DST）+ tick + 落盘。**决策本身可抄，实现要自建** |
| E2 | **主动发送** `bridge_send` 工具 | Hermes（立场相反）/ dsh（做成工具） | **M**（2~3d） | 依赖 B3。需裁决：opt-in 白名单 vs 暴露给模型 |
| E3 | **推送文件/媒体** `bridge_send_file` | dsh | **M**（2d） | 三平台媒体能力差异大 |
| E4 | TUI 状态面板 | dsh `client.js` | **M**（3d） | 独立技能域（新语法/新 API） |

### F · 已对齐 / 不适用

| 功能 | 状态 |
|---|---|
| 应用内 setup 向导 | ✅ 已借鉴（`--setup` / `/setup` / `bridge_setup` / `--setup --json`） |
| 单例锁 | ✅ 已有（跨进程杀旧重启，与 dsh 的硬链接锁目标相同、实现不同） |
| 扫码开通（provisioning） | ❌ 不适用 —— 我们 3 平台都是 token，无扫码 |
| 25 渠道 / 平台插件化 | ❌ 不适用 —— 我们的方向是"做深"不是"做多" |
| 依赖动态安装 | ❌ 不适用 —— 我们零第三方依赖 |

---

## Part 10 · 「复刻两方全部优点」的实施波次

必须先说清：**全部优点 ≠ 全部机制**。Part 5 的"不建议抄"仍然成立
（dsh 的 `toTelegramHtml` 死码、Hermes 的配对码状态机在无开放场景时是纯负债）。
下面按"能拿到的优点全拿、该挡的挡住"排波次。

### Wave 0 · 低风险地基（S 为主，1~2 周，不破坏任何现有行为）
D2 能力枚举 · D3 状态四态归一 + `--status` · **D4 prompt hint 注入** · C1a 分片算法 · C1c 审批超时+迟到去重 · A5 回声防护

> 收益最直观（用户立刻能感到"消息不再被切坏""agent 知道自己在 IM 里""状态一目了然"），
> 零破坏性，可逐项独立发布与验证。

### Wave 1 · 安全闭环（M，1.5 周）
A1 脱敏引擎 · A2 授权三段闸门 · A6 `pii_safe` · D1 出站错误分类
　+ **A3 默认收紧**（独立决策：需迁移期与文档，先只加警告不改默认）

### Wave 2 · 交互深度（M，2 周）
C1b 合并窗口（含快照落盘）· C1d 提问桥 · C1e 三平台按钮 · D5 hint 可覆盖

### Wave 3 · 身份模型（L，1.5 周，**破坏性，需单独评审 + 迁移预案**）
B1 `SessionSource` + 唯一真源 + `state.json` 迁移 · B2 多 chat 共享

### Wave 4 · 主动能力（M~L，2~3 周，依赖 B3）
B3 渠道地址簿 → E2 主动发送 → E3 文件推送 → E1 定时（自建日历，最后做）

### 待定（按需，不排期）
A4 配对码（要开放给陌生人时）· E4 TUI 面板

### 关键路径与风险

- **Wave 3 是唯一的破坏性波次**，且是 E1/E2 的隐性前置（都要按 chat 寻址）。
  若不想付迁移代价，替代方案是保留 `conversation_id` 字符串但**加入平台前缀**
  （`telegram:<id>` / `slack:<id>` / `discord:<id>`），能解决"跨平台同名串台"，
  代价是 thread/话题仍不可表达——**这是个可以先做的 80% 方案**。
- **Wave 4 的 E1 成本主要在自建日历**（时区 + DST）。若接受"只支持每天 HH:MM + 星期"
  的子集，可省掉大部分 DST 复杂度（dsh 的两遍法收敛可读）。
- **A3 是行为变更**，会打破"配好即用"的现状。建议分两步：先加 `allowed_chat_ids_required`
  之类的显式开关与警告（不改默认），下个 minor 再切默认并给迁移指引。
