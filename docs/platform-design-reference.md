# 消息平台设计参考：Hermes × dsh-im-gateway × opencode-bridge

本文是**对比报告**，不是使用手册。目的：把两个成熟参照系的平台层讲清楚，逐项标出
opencode-bridge 该跟什么、跟到什么程度。

## ⚠️ 修订记录（2026-10-05）

本文的 `opencode-bridge` 列**整体过期过一次**：初稿写于 3 平台时代，此后项目扩到
**13** 个平台，并补上了脱敏、身份、错误分类、授权闸门、入站合并、延迟回答拦截等能力。
一份读起来像"当前现状"的过期对比**比没有对比更糟**（它会被当成现状引用），所以本次
逐条更正；并且**每条更正都写出"旧文怎么说 / 现在是什么"**，不静默改写（AGENTS.md §8）。

证据基线（两条，别混）：

- **本仓库**：引用一律给 `文件:行号`。
- **opencode 宿主**：读 **tag `v2.0.22`** 的源码，**不读文档站**（对 v2 已过时，
  AGENTS.md §6.2）。⚠️ **`gh search code` 搜的是默认分支 `dev`，而 `dev` 的插件 SDK
  目录结构已与 `v2.0.22` 不同**（`v2.0.22` 是 `packages/plugin/src/promise/`，
  `dev` 上是 `packages/plugin/src/index.ts` + `src/v2/`）。**核实宿主能力必须用
  `?ref=v2.0.22` 读文件，不能拿 dev 的搜索结果反推 v2.0.22。**

本次**被推翻的关键结论**（正文已就地标注；Part 4 的 `opencode-bridge` 列与 Part 8 的
宿主能力表另有**整表**更正）：

1. 「IM 长按输入把一句话拆成 3~5 条」——Part 5 #2。
2. 「4 个 chokepoint + 不可复用哨兵」——Part 9 A1。
3. 「审批超时回落本地 + 迟到回答 30s 去重」——Part 9 C1c（超时是幻影，30s 是 dsh 的数）。
4. 「`ctx.permission.list()`/`.reply()` 是我们现用的审批回传路径」——Part 8（走 REST）。
5. 「3 个平台 / 3（2 无入站）/ 4 abstractmethod」——Part 1 / Part 4（13 个平台，
   全部有入站，3 个 abstractmethod）。

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
| **opencode-bridge** | 桥（只做转换） | 13 个平台、Python 侧无外部依赖；把 IM 消息转成 opencode 的 session prompt。**平台层最薄**这条仍成立；但「身份模型与授权层也最薄」**已被推翻**——`identity.py` 的 `platform:local_id` 与 `adapters/base.py` 的 `admits()` 闸门都已落地（见 Part 4 更正栏） |

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
| merge 快照恢复**未接通**：`snapshots()` 零调用，启动遍历刚 new 的空 Map（⚠️ 本仓库已把这条链接通：2026-10-10 G2 —— `held_buffer_store.py` 整份快照落盘 + `InboundGateway._recover_held_buffer` 启动重灌，守门 `tests/test_held_buffer_recovery.py`） | `merge.ts:91-95`、`gateway.ts:155,163` |
| 默认 `allowAllUsers: true` | `index.ts:57` |
| 审批应答在白名单**之前**处理 → 未授权用户能打"批准" | `gateway.ts:401-409` vs `:411` |
| 4000 是"猜的默认值"而非各平台真实上限（13 家都是 4000） | 各 `channels/*.ts` |

---

## Part 4 · 三方并排对比

| 维度 | Hermes | dsh-im-gateway | opencode-bridge（**2026-10-05 逐行更正**） |
|---|---|---|---|
| 抽象风格 | 4900 行富基类 + 插件注册表（22 钩子） | 7 字段结构化对象 + 可选方法 | `adapters/base.py` **3** 个 abstractmethod（`start`/`send`/`edit`，`:292,311,315`）+ **13** 个适配器，各带一处入站闸门调用点。**「4 abstractmethod + 3 适配器」两个数都错** |
| 平台数 | 23 core + 插件 | 25（3 为 stub，真实可用 22） | **13，全部有入站**（`supports_inbound = True` × 13）。**「3（2 无入站）」已不成立** |
| 能力表达 | 常量标志位 + 方法查询 + 模板方法 + feature-detect | 可选方法 duck typing | **显式能力枚举**：`adapters/base.py:107-129` 的 4 个 flag（`supports_inbound` / `supports_inline_buttons` / `supports_media` / `supports_message_edit`）+ `Base.answer` 空 stub（`:319`）。**「try/except 降级」是 3 平台时代的做法**，现已换成 Hermes 式的显式枚举 |
| 会话身份 | `SessionSource` 20+ 字段 + 唯一真源 | `${channelId}:${chatId}` | `identity.py` 统一 **`platform:local_id`**；歧义前缀（`channel:` 被 slack/discord/mattermost 三家共用）**拒绝猜测**，抛 `AmbiguousConversationId`。**「裸 `conversation_id: str`」已不成立** |
| 多端共享会话 | 支持（profile route） | 支持（`bySession` 反向索引，跨渠道互见） | 无（全仓无反向索引）—— **仍为真** |
| 授权 | 三段闸门 + PairingStore + allowlist 并集 | 一层白名单（默认全开）+ UI 一键批准 | `adapters/base.py:278` 的 `admits()` + **13 处调用点**（每适配器一处）+ 极简配对码（`/pair` + `--pair`，无 TTL/限流/锁定）。⚠️ 白名单为空的语义现由顶层 `config_version` 决定：**`< 2`（含没有该键）= 旧语义「全放行」+ 启动预告，`>= 2` = 新语义「全拒」** ⇒ **不是一步翻转**（Part 9 A3）。**「仅 telegram 白名单」已不成立** |
| 出站错误 | `SendResult` + 7 类 `error_kind` | `send` 返回 void，失败全吞 | **7 类 `SendError`（`hooks.py:74`）+ `SendResult`（`hooks.py:91`，含 `partial` / `retry_after` / `error_detail`）**。**「无分类」已不成立** |
| 富交互 | 原生按钮 + 回调 id 硬约定 | 纯文本编号 + 关键词 | 实测 **inline 按钮 1/13**：只有 telegram 声明 `supports_inline_buttons = True`（`adapters/telegram.py:125`），其余 12 家显式 `False`。**「纯文字提示」低估、「覆盖三平台」高估，两个都不对** |
| 主动发送 | cron/CLI/MCP（**明确非模型工具**） | `im_send_file` / `im_cron_*`（做成工具） | 无（唯一注册的工具是只读的 `bridge_setup`）—— **仍为真** |
| 定时 | cron 表达式 + 双闸防 env 枚举 + 四 lane | 墙钟日历手写 + **绑 chat** | 无（全仓无 scheduler 实现）—— **仍为真** |
| 入站合并 | `EphemeralReply` 等 | `..`/`!!` + 5s 窗口 | **有**：`inbound_merge.py` 的 `ConversationMerger`（`:151`）。但**不是** dsh 那种窗口：**只有用户自己敲 `..` 才合并**，裸文本零延迟直发；超时是 **15s 保险丝**且**只在缓冲存在时存在**（`inbound_gateway.py:69`）。**「无」已不成立** |
| 渠道地址簿 | 有（仅已连接平台） | 无 | 无 —— **仍为真** |
| 去重 | `MessageDeduplicator` | 核心无（只在渠道层） | **消息级 at-most-once**：`inbound_gateway.py:134-155` 优先用平台 `message_id`，缺失时退回 `sha256(platform\|conversation_id\|text)`；`:158-180` 命中即**不再投递**。**「telegram offset 游标」只覆盖 telegram** |
| 限流 | 有 | 无 | telegram 429 `retry_after` + `getUpdates` 退避（`adapters/telegram.py:80,90,93`）—— **仍为真** |
| 单实例 | 无此概念 | DSH_HOME 硬链接锁 | 单例锁（跨进程杀旧重启，`instance_lock.py`）—— **仍为真** |

**⚠️ 一条纪律原来就写对了，而且已落地**：dsh 的反面教训是「审批应答在白名单**之前**」，
未授权用户能打"批准"（Part 3.7）。我们把这条写进了 `admits()` 的 docstring
（`adapters/base.py:283-285`），13 个调用点都必须遵守。

**仍为真的那几条不要当成"没进展"**：定时、渠道地址簿、主动发送、多端共享会话、单实例
五项确实**还没做**，它们仍是 Part 6/9 里的未完成项。

---

## Part 5 · 借鉴清单（分级）

### ✅ 可直接抄（低成本高收益）

| # | 借鉴项 | 来源 | 解决什么问题 | 代价 |
|---|---|---|---|---|
| 1 | **`splitText` 码点切分 + 断点优先级 + 前缀两遍法重编号** | `split.ts:31-103` | 我们目前是简单截断，会切坏 emoji、会在断点不佳处硬切、可能出现"第 3/2 段" | 极低。纯函数 + 9 个现成测试可读 |
| 2 | **入站 `..`/`!!` 合并 + 长输入回执** | `merge.ts:45-75`、`gateway.ts:566-569` | ⚠️ **初稿写的理由已被推翻**：原文「IM 长按输入把一句话拆成 3~5 条」**对 13 个平台全为假**（逐个核实见 `inbound_merge.py:11-24`：每条协议消息产出**恰好一个** `Inbound`，正文逐字取自协议的单个字段；`..`/`!!` 是 irssi/weechat 那种**终端客户端**的多行粘贴约定，由用户自己敲，这些 IM 客户端没有）。真正装不下长输入的只有 IRC（512 B/行）与 Twitch（400 字符）两家 | ✅ **已实现**，但**刻意不抄窗口**：裸文本零延迟直发、15s 保险丝、拼接用换行、缓冲不落盘（理由见 `inbound_merge.py:26-46`） |
| 3 | **状态四态归一纯函数** | `manager.ts:29-44` | 让 `--status` / `/status` / 日志三处对"算不算连上"用同一 verdict（对齐 Hermes 纪律） | 极低。一个纯函数 + 一个测试文件 |
| 4 | **迟到回答去重 + 显式回复门** | `questions.ts:86,144-158` | 权限/提问窗口关闭后，随口一句话被误当答案；并发回答重复处理 | 低。30s 窗 + 一个判定函数 |
| 5 | **"首答生效 + 同 key 不排队"** | `approval.ts:31`、`questions.ts:35` | 多端并发时的竞态：重复回答、错答串到别的会话 | 低。干净的小取舍 |

### ✅ 落地对照（2026-10-05）

下面的借鉴清单写于 **3 平台时代**，正文条目**保持原样**以便看出当初的判断依据；
本表只记「哪一条被推翻 / 哪一条已落地」。

| # | 初稿的说法 | 现状 |
|---|---|---|
| 1 | 「我们目前是简单截断」 | ✅ 已落地 `split.py`（比 dsh 多做了**字素簇原子**与前缀预估，见该模块 docstring） |
| 2 | 理由「长按输入拆成 3~5 条」 | ❌ **理由被推翻**（见正文 #2）；✅ 功能已落地，但形态不是 dsh 的窗口 |
| 3 | 「让三处用同一 verdict」 | ✅ 已落地 `status.py`（`ChannelState` / `normalize_platform_status` / `summarize` / `render_table`） |
| 4 | 「低。30s 窗 + 一个判定函数」 | ⚠️ **30s 是 dsh 的数字，不是我们的**。我们**没抄这个窗**：改用 `permission_ledger.py` 的 `PermissionLedger`，**无超时**（见 Part 9 C1c） |
| 5 | 「首答生效 + 同 key 不排队」 | ✅ 已落地，形态不同：`PermissionLedger` 在**两条回传路径上同时拒绝第二次回答** |
| 6 | 「引入 `SendResult` + `classify_send_error`」 | ✅ 已落地 `hooks.py:74,91` |
| 7 | cron 绑 chat | ⏳ 仍未做（全仓无 scheduler），决策本身依然成立 |
| 8 | 「我们只有 3 平台」 | ❌ **平台数错**（13）；✅ 取法正确并已落地：`adapters/base.py:107-129` 的显式 flag |
| 9 | 活跃渠道目录 | ⏳ 仍未做 |

### ⚠️ 需改造（决策可抄，实现要重做）

| # | 借鉴项 | 源 | 我们要做什么 |
|---|---|---|---|
| 6 | **出站错误分类** | Hermes `error_kind` | 引入 `SendResult` 类结构 + 统一 `classify_send_error`，否则失败不可观测 |
| 7 | **cron 绑 chat 不绑 session** | `cron.ts:1-3` | 这是"提醒不消失"的正解。但 opencode 侧是否有等价 scheduler 未确认；调度实现另选，只抄"绑定维度"这个决定 |
| 8 | **能力表达** | 两者都不适合直接抄 | ~~我们只有 3 平台~~ 且要向用户报"不支持"，应采用 **Hermes 式显式能力枚举**，而非 dsh 的 duck typing（无法枚举能力） |
| 9 | **活跃渠道目录** | Hermes `channel_directory.py` | "我在哪些群"是主动发送与 channel 绑 cron 的前置 |

### ❌ 不建议抄

| 项 | 原因 |
|---|---|
| `toTelegramHtml` 死码 + `parse_mode:'HTML'` 组合 | dsh 现存隐性 bug（HTML 实体） |
| 默认 `allowAllUsers: true` / 空 `allowed_chat_ids` 即全开 | Hermes 要求显式 opt-in 才开放；我们**已改**为「空 = 全拒」，但用 `config_version` 兜底分两步走（见下节 P0 / A3） |
| 实例锁 `instance-lock.ts` | 我们是插件进程，无共享 home 概念。仅"pid+token 双匹配释放"可作通用防误删模式 |
| `cronMaxConcurrent` 死配置 / merge 快照半成品 | 反面教材：声明了不接，等于活文档误导 |
| Hermes 的配对码（PairingStore） | 一整套限流/TTL/锁定状态机；等真要在群里开放给陌生人再上 |

---

## Part 6 · 修正后的 opencode-bridge 优先级

上一版（仅基于 Hermes）需两处修正，并新增一项：

⚠️ **这张表写于 3 平台时代，「变化原因」列里的"我们只有…"多数已经不成立。** 逐条现状：

| 优先 | 项 | 变化原因（初稿原文） | 现状 |
|---|---|---|---|
| **P0** | 授权层下沉到 `base.py`，三平台共用闸门 | Hermes 三段闸门 + dsh 都有白名单层，我们只有 telegram 一家。**且 dsh 证明了"审批在白名单之前"的危险** | ✅ 已落地并扩到 **13 平台**：`adapters/base.py:278` 的 `admits()` + 13 处调用点；dsh 那条教训写进了 docstring（`:283-285`） |
| **P0** | 脱敏（`redact.py` + 不可复用哨兵） | 两家都有，我们 0。且我们的 `/status` 会把 session_id 回显到 IM | ✅ 已落地 `redaction.py`，但**形态与初稿设想不同**：**2 个**卡口（不是 4 个），且**没有做 Hermes 那种"不可复用哨兵"**——凭据改成**只遮蔽不摘要**的 `[REDACTED:<类别>]`，靠不可逆达成同等目的（理由见 `redaction.py:40-51`） |
| **P0** | **默认开放改为显式 opt-in** | 我们 `allowed_chat_ids: []` = 全部允许，与 dsh 的 `allowAllUsers: true` 同为 opt-out；Hermes 是"没配 allowlist → pair/ignore，且 `_ALLOW_ALL_USERS` 要显式开" | ✅ **已做，但用 `config_version` 兜底分两步走**（不是一步翻）：新增顶层 `config_version` —— **`< 2`**（含没有该键；`config.example.json` 给的是 `0`）= 沿用旧语义「空 = 全放行」+ 启动时醒目预告；**`>= 2`** = 新语义「空 = 谁都不放行」。`--pair` 成功时顺手写上 `2`。详见 Part 9 的 **A3** |
| **P1** | 分片算法（码点 + 断点 + 两遍法） | 直接可抄，成本极低 | ✅ 已落地 `split.py` |
| **P1** | 权限/提问应答的去重与超时 | dsh 的 30s 窗 + 显式回复门，比我们现有实现更完整 | ⚠️ **"超时"是幻影**：本仓库从来没有审批超时，那是账本里一条被推翻的设想。真实落地的是 `permission_ledger.py` 的 `PermissionLedger`——**拒绝第二次回答**，无超时。**30s 是 dsh 的数字** |
| **P2** | `SessionSource` 化身份模型 | **破坏性**（会话映射变了 = IM 会话失忆），需迁移。三方都做了，维度上 dsh 最弱、Hermes 最强 | ✅ 取了下面「关键路径与风险」里那条 **80% 方案**（`platform:` 前缀），`identity.py` 已落地；完整 `SessionSource` 仍未做 |
| **P2** | 出站错误分类 / 能力枚举 / `--status` 汇总 | 中等成本，用户可感知 | ✅ 三项全部落地：`hooks.py:74,91` / `adapters/base.py:107-129` / `status.py` |
| **P3** | 入站合并窗口 / cron 绑 chat / 渠道目录 / 主动发送 | 功能增量；cron 依赖 opencode 侧 scheduler 能力（未确认） | 入站合并 ✅ 已落地（`inbound_merge.py`）；cron 绑 chat / 渠道目录 / 主动发送 ⏳ 仍未做。cron 侧已核实：**v2.0.22 确实没有 scheduler**（见 Part 8） |

---

## Part 7 · 与两家都无法对齐的结构性差异

1. **Hermes/dsh 都是"自带 agent 的宿主内插件"**（Hermes 自建 prompt/tools/cron；
   dsh 依赖 cordis 注入的 9 个 host service）。opencode-bridge 是**跨宿主桥**——
   ⚠️ 初稿说它「只能通过 opencode 的 hook（`session.hook("context")`、
   `ctx.tool.transform`、`ctx.command.transform`）参与」，**这句不准确**：
   ① 它同时走 **REST**（`opencode_client.py`，prompt / interrupt / 权限回传），
   ② `session.hook("context")` **我们从未调用**（见 Part 8），
   ③ 平台 hint 是**在 Python 侧拼字符串**注入的（`channel_profile.py:251` 的
   `with_channel_hint`，调用点 `inbound_gateway.py:597`），不经过任何 ctx。
   真正用到的 ctx 成员只有 `ctx.options` / `ctx.tool.transform` /
   `ctx.command.transform` / `ctx.session.prompt`（`plugin/index.ts`）。
2. **主动发送的立场相反**：Hermes 明确"不做成模型工具"（防 agent 乱发），
   dsh 做成了 `im_send_file` 工具。要做 `bridge_send` 必须自行裁决：
   opt-in + 目标白名单，还是不暴露给模型。**这一条仍然开放**（唯一注册的工具
   `bridge_setup` 是只读的）。
3. **扩展方向**：初稿写「我们的扩展方向是在已有 **3** 个平台上做深」——
   ⚠️ **这个前提已经不成立**：现在是 13 个平台，"做多"已经发生了。
   借鉴时该偏向**做深**那侧的能力（授权、错误分类、交互桥），这一点仍然成立；
   但别再把"平台少"当成本论证的前提——13 个平台 × 13 个适配器意味着
   **每加一个平台，上面那些机制都要再落一次地**。

---

## Part 8 · opencode 宿主能力边界（决定难度上限）

核实基线：**tag `v2.0.22`** 的源码（AGENTS.md §6.2「读实现，不读文档」）。
ctx 的完整形状在 `packages/plugin/src/promise/plugin.ts:26-54`（`Context` interface），
各域定义在同目录 `promise/<域>.ts`。

⚠️ **`gh search code` 搜的是默认分支 `dev`，而 `dev` 的插件 SDK 布局已与 `v2.0.22` 不同**
（`v2.0.22` 是 `src/promise/<域>.ts`；`dev` 上是 `src/index.ts` + `src/v2/`）。
**别拿 dev 的搜索结果反推 v2.0.22**——本表所有"上游存在"一列都读的是
`?ref=v2.0.22` 的文件正文。

⚠️ **初稿最大的问题是把「上游存在」和「我们在用」挤在一列里**，于是 7 行我们**从未
调用**的 `ctx.*` 看起来像现成能力。下面分成两列。

| 宿主能力 | 上游是否存在（v2.0.22） | 本仓库是否在用 | 证据 |
|---|---|---|---|
| **cron / scheduler** | ❌ **无** | ❌ 无 | v2.0.22 顶层子命令目录 `packages/cli/src/commands/handlers/` 全清单里没有 `cron` / `schedule` / `job`。定时必须**自己实现 tick + 日历解析**（dsh 手写 156 行零库）→ 这是"提醒"功能的成本主因。**这一行仍为真**（初稿靠 `opencode --help` 实证，现在换成源码清单） |
| `ctx.session.hook("context")` | ✅ 有 | ❌ **未用** | 上游 `promise/session.ts:140`（`SessionHooks.context`）、`:171`（`hook: ModelHooks<SessionHooks>`）。初稿说它能「直接注入平台 prompt hint」——**能力存在，但我们没走这条路** |
| `ctx.session.hook("prompt")` | ✅ 有 | ❌ **未用** | 上游 `promise/session.ts:139`（`SessionHooks.prompt`） |
| `ctx.session.prompt()` | ✅ 有 | ✅ **已用** | 上游 `promise/session.ts:160`（`Pick<SessionApi, … "prompt" …>`）；我们 `plugin/index.ts:482`（存在性检查）、`:494`（实际调用） |
| `ctx.session.synthetic()` | ✅ 有 | ❌ **未用** | 上游 `promise/session.ts:164`（`Pick<SessionApi, … "synthetic" …>`） |
| `ctx.permission.list()` / `.reply()` | ✅ 有 | ❌ **未用** | 上游 `promise/permission.ts:22`（`Pick<PermissionApi, "list" \| "get" \| "reply">`）。⚠️ **初稿说这是"我们现用的审批回传路径"——假**（见下） |
| `ctx.permission.hook("evaluate")` | ✅ 有 | ❌ **未用** | 上游 `promise/permission.ts:18-20,23`（`PermissionHooks.evaluate`，`effect` 可改）。可用于白名单自动放行 |
| `ctx.tool.hook("execute.before"/"execute.after")` | ✅ 有 | ❌ **未用** | 上游 `promise/tool.ts:38-64`（`ToolHooks`）、`:71`。→ **agent 向 IM 提问**可由此实现（Part 9 C1d） |
| `ctx.tool.transform` | ✅ 有 | ✅ **已用** | 上游 `promise/tool.ts:67`；我们 `plugin/index.ts:440,442`（注册 `bridge_setup`）→ `bridge_send` 确实走同路 |
| `ctx.command.transform` | ✅ 有 | ✅ **已用** | 上游 `README.md:69`；我们 `plugin/index.ts:482,484`（注册 `/bridge-setup`） |
| `ctx.storage`（`get`/`set`/`remove`/`scan`） | ✅ 有 | ❌ **未用** | 上游 `promise/storage.ts:4-8`（`StorageDomain`）。⚠️ 上游还有初稿没提的 `remove`。我们的持久化走自己的 `StateStore`（`state.py`），不用它 |
| `ctx.options` | ✅ 有 | ✅ **已用** | 上游 `promise/plugin.ts:29`；我们 `plugin/index.ts:580`、解析链起点 `:327` |
| TUI 插件 | ⚠️ **部分核实** | ❌ 未用 | v2.0.22 有独立的 TUI 插件包（`packages/plugin/src/tui/`，`plugin.ts` 的 `Definition` + `context.ts` 的 `Context`），但那个 context 是**观测型**（`on` / `listen` / `session` / `project` / `location` / 各域 `LocationCollection`）。⚠️ **初稿点名的「slots / panel / keymap」三个面本次未核实到，不作断言** |
| 多端共享一个 session | ⚠️ 由我们自己的键设计决定（宿主不干预） | ❌ 未做 | 与 Hermes/dsh 同构，可做；**本仓库仍无反向索引** |

**初稿的两处错，必须分开看**：

1. **`ctx.permission.list()` / `.reply()` 不是我们的审批回传路径。**
   我们走 **REST**：`opencode_client.py:374-393` 的
   `POST /api/session/{id}/permission/{requestID}/reply`（`reply_permission()`），
   **从不经过插件 ctx**。所以"ctx 权限面现成"对成本**没有贡献**——
   权限那条路本来就不依赖插件 API。
2. **「prompt hint、工具拦截、持久存储都现成 → 复刻两方优点更便宜」这个推论不成立。**
   三个上游能力**确实都现成**，但**我们一个都没接**。hint 注入走的是完全另一条路：
   `channel_profile.py:251` 的 `with_channel_hint`，由 `inbound_gateway.py:597`
   在**拼 prompt 字符串时**加，与 `ctx.session.hook("context")` 无关。

**更正后的结论**：宿主缺 cron（要自建）**这条仍成立**；但注入面**对我们几乎没用**——
`ctx.*` 里只有 4 个成员被碰过。**这不改变任何功能的可行性**：hint 走字符串拼接、
审批走 REST、提问桥若要做仍可接 `ctx.tool.hook`。
**但成本估算要回到 Part 6 / Part 9 的口径，不要因为"注入面现成"而低估。**

---

## Part 9 · 功能清单与实现难度

难度口径：**S** = 纯函数/局部改动、不破坏现有行为；**M** = 跨文件 + 需新抽象 + 测试；
**L** = 破坏性迁移或跨层状态机。括号内为工作量粗估（单人日）。

### A · 安全与授权（两家共同强调，我们最缺）

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| A1 | **脱敏引擎**：token/手机号/ID 模式 + 赋值形态 + 卡口 + 误伤门 | Hermes | **M**（3~5d） | ✅ **已落地 `redaction.py`**。⚠️ **初稿的两处细节已被推翻**：① 「**4 个** chokepoint」——实际是 **2 个**（挂在 logging **handler** 上的 `RedactingFilter`，与挂在 `StateStore` JSON 序列化边界上的 `redact_state_values`，理由见 `redaction.py:11-26`）；② 「**不可复用哨兵**」——那是 Hermes 的做法，我们**没做**，改用**不可逆整段遮蔽** `[REDACTED:<类别>]`（凭据**只遮蔽不摘要**，因为低熵口令的摘要等于离线验证器，理由 `redaction.py:40-51`）。**「确定性 key-hash 方案连带复用哨兵」这条路已被否决，不要当成可用选项**。「误伤门」✅ 保留（`redaction.py:99-102`，命中后再过一次确认门） |
| A2 | **授权层统一 + 三段闸门**：入站先过"渠道忽略"，再过网关白名单 | Hermes | **M**（2~3d） | ✅ **闸门那半已落地**：`adapters/base.py:278` 的 `admits()` + **13 处调用点**，且 dsh 那条教训写进了 docstring（`:283-285`，审批应答必须在白名单之后）。⚠️ 「三段闸门」中的"渠道忽略"那层未单独做 |
| A3 | **默认收紧为显式 opt-in** | Hermes | **S**（0.5d）+ **行为变更** | ✅ **已做，但按下面的「关键路径与风险」那条建议分两步走**：一次发，靠顶层 `config_version` 兜底 —— **`< 2`**（含没有该键；example 给的是 `0`）= 旧语义「空 = 全放行」+ 启动时醒目预告，**`>= 2`** = 新语义「空 = 谁都不放行」。⚠️ **诚实代价**：未配对用户的暴露**仍然存在**，且因为没强制，多数人不会去配 ⇒ 暴露只会**随时间慢慢收窄**，不会一天归零。若要立刻归零只能硬翻（无闸门），代价是所有空名单用户当场失声 —— **这是知情取舍，不是「问题已解决」** |
| A4 | **配对码** | Hermes | **M**（3d，原方案）/ **S**（极简版，已做） | ✅ **已做的是极简版**，不是本行初稿写的那套：8 字符 HMAC 派生码（`HMAC-SHA256(key=pairing_secret, msg="opencode-bridge/pair/v1\n"+platform+"\n"+conversation_id)`，取前 8 个 base32 小写字符），**无 TTL / 无签发记录 / 无一次性使用 / 无失败锁定**；触发词 `/pair`（⛔ 不复用 `/setup`，后者文案已冻结并被测试钉住），用户在未授权的 chat 上就能用 ⇒ 没人会被困死。轮换 `pairing_secret` 就是过期机制本身。⚠️ 初稿的 **「TTL + 限流 + 失败锁定 + 别名集」全部刻意不做**（YAGNI）；⛔ 轮换 secret **只让未兑换的码失效，不撤销已完成的配对**（授权已物化进 `allowed_chat_ids`）。`pairing_supported` 类属性默认 `False`、逐个 opt-in：**irc / twitch**（平台对发件人无认证，私聊 principal 是 bot 自己的 nick ⇒ **翻转后私聊对所有人不可用**）、**nextcloud**（principal 是 OCS token）、**homeassistant**（entity_id）、**a2a**（peer）、**qqbot**（principal 是同群所有人共享的会话 id，拿它当配对锚点会把整个群一起授权）**不支持配对**；telegram / slack / discord / matrix / mattermost / ntfy / email 支持 |
| A5 | **Bot 回声防护**（按 conversation 计预算） | Hermes | **S**（0.5d） | 防 agent 输出被当成新输入回灌 |
| A6 | **`pii_safe`**：prompt 里 id 用确定性 hash、路由用原值 | Hermes | **S**（1d） | ✅ **已落地**，但**不是裸确定性 hash**：会话 id / 手机号 / 邮箱一律用 **带进程内随机密钥的 HMAC-SHA256**（`redaction.py:331-350`），因为这三类都是低熵（手机号空间 ~10^10、telegram chat id 13 位内），裸 SHA 枚举几分钟就还原 |

### B · 身份与会话

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| B1 | **`SessionSource` 结构化身份 + `build_session_key()` 唯一真源 + 适配器 seam** | Hermes | **L**（5~8d） | 部分落地：取的是 Part 10「关键路径」那条 **80% 方案** —— `identity.py` 的 `platform:local_id`，歧义前缀拒绝猜测。完整 `SessionSource`（20+ 字段）仍未做，thread/话题仍不可表达 |
| B2 | 多 chat 共享一个 session（`bySession` 反向索引 + `/continue`） | dsh | **M**（2d） | ⏳ 依赖 B1，仍未做 |
| B3 | **渠道地址簿**（"我在哪些群"，仅索引已连接平台） | Hermes | **M**（2~3d） | ⏳ 仍未做。⚠️ 初稿写「落盘用 `ctx.storage`」——那能力上游有（`promise/storage.ts:4-8`）但**我们没接**；实际会走自己的 `StateStore` |

### C · 交互质量

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| C1a | **出站分片算法**：码点切分 + 断点优先级 + 前缀两遍法重编号 | dsh | **S**（0.5d） | ✅ **已落地 `split.py`**（另加了 dsh 没有的**字素簇原子**与前缀预估） |
| C1b | **入站合并** `..`/`!!` + 长输入回执 | dsh | **M**（2d） | ✅ **已落地**，但**数字与形态都被改过**：⚠️ 初稿的「**5s 超时**」是 **dsh 的数**、而且那是**窗口**不是超时；我们的是 **15s 保险丝**（`inbound_gateway.py:69`），**只在缓冲打开时存在**，裸文本**零延迟直发**（`inbound_merge.py:26-34`）。⚠️ 初稿要求「必须同时做快照落盘」——**刻意不做**：dsh 做了 `snapshots()` 但自己没接通（Part 3.7 那条仍然成立），而"别丢这条"是 `InboundInbox` 的职责，两者混在一起会替用户编出一句他没说的话（`inbound_merge.py:39-46`） |
| C1c | **审批流加固**：首答生效 + 同 key 不排队 + 迟到回答去重 | dsh | **S~M**（1~2d） | ⚠️ **初稿有两处幻影**：① 「**超时回落本地**」——**本仓库从来没有审批超时**，那是账本里一条被推翻的设想（`permission_ledger.py` 的 docstring 记着这次推翻）；② 「迟到回答 **30s** 去重」——**30s 是 dsh 的数，我们没抄这个窗**。✅ 真实落地的是 `permission_ledger.py` 的 `PermissionLedger`：**无超时**，只做一件事——在**两条回传路径上同时拒绝第二次回答**（`/approve` 文字命令与 `perm:` 回调） |
| C1d | **交互式提问桥**（agent 向 IM 提问，多选/单选/自由输入，第一份答案生效） | dsh + Hermes | **M**（3d） | ⏳ 仍未做。借 `ctx.tool.hook("execute.before")` 拦截 ask 类工具 —— 该 hook **上游存在但我们从未调用**（Part 8） |
| C1e | **富交互 inline 按钮**（审批/提问都用按钮 + 回调 id 约定） | Hermes | **M**（3d） | ⚠️ 初稿说「覆盖**三**平台…Slack/Discord 需补」——**平台数与现状都不对**：现在是 13 个平台，实测 **inline 按钮 1/13**，只有 telegram 声明 `supports_inline_buttons = True`（`adapters/telegram.py:125`），其余 12 家**显式 `False` 并注明原因**（slack/discord 的 blocks 未实现）。成本要按 12 个平台重估，不是 2 个 |

### D · 输出与可观测

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| D1 | **出站错误分类**（`SendResult` + 平台中立 `error_kind`） | Hermes | **M**（2d） | ✅ **已落地 `hooks.py:74,91`**（7 类 `SendError` + `SendResult`） |
| D2 | **显式能力枚举**（`maxMessageLength` / `supportsInlineButtons` / `typedCommandPrefix`…） | Hermes | **S**（1d） | ✅ **已落地 `adapters/base.py:107-129`** 的 4 个 flag。⚠️ 初稿的理由「**平台少** + 要向用户报不支持」——"平台少"已不成立（13 个），但**取 Hermes 式而非 dsh duck typing 的结论更对了**：13 个适配器正是"能力必须可枚举"的理由 |
| D3 | **状态四态归一 + `--status` 汇总** | dsh + Hermes 共同纪律 | **S**（1d） | ✅ **已落地 `status.py`**（`ChannelState` / `normalize_platform_status` / `summarize` / `render_table`） |
| D4 | **平台 prompt hint 注入**（长度上限 / 无 markdown 渲染 / 回复期望） | Hermes | **S**（1d） | ✅ **已落地**，⚠️ 但**不是**初稿说的「借 `ctx.session.hook("context")`，宿主已提供注入面」——那条路**我们没走**。真实做法：`channel_profile.py:251` 的 `with_channel_hint`，调用点 `inbound_gateway.py:597`，在**拼 prompt 字符串时**加 |
| D5 | hint 可被用户覆盖（append/replace） | Hermes | **S**（0.5d） | 依赖 D4 |

### E · 主动能力

| # | 功能 | 源自 | 难度 | 备注 |
|---|---|---|---|---|
| E1 | **定时任务绑 chatId 而非 sessionId** | dsh | **M~L**（4~6d） | opencode **无 cron** → 要自写日历（时区/DST）+ tick + 落盘。**决策本身可抄，实现要自建** |
| E2 | **主动发送** `bridge_send` 工具 | Hermes（立场相反）/ dsh（做成工具） | **M**（2~3d） | 依赖 B3。需裁决：opt-in 白名单 vs 暴露给模型 |
| E3 | **推送文件/媒体** `bridge_send_file` | dsh | **M**（2d） | ⚠️ 初稿说「**三**平台媒体能力差异大」——现在是 13 个平台，实测 **1/13 支持媒体**（只有 telegram，`adapters/telegram.py:126`），其余 12 家显式 `False` 并注明原因。差异比初稿设想的大得多 |
| E4 | TUI 状态面板 | dsh `client.js` | **M**（3d） | 独立技能域（新语法/新 API）。⚠️ 宿主侧 TUI 插件包 v2.0.22 **确实存在**，但初稿点名的 slots/panel/keymap **本次未核实到**（Part 8），成本估算前要先核实 |

### F · 已对齐 / 不适用

| 功能 | 状态 |
|---|---|
| 应用内 setup 向导 | ✅ 已借鉴（`--setup` / `/setup` / `bridge_setup` / `--setup --json`） |
| 单例锁 | ✅ 已有（跨进程杀旧重启，与 dsh 的硬链接锁目标相同、实现不同） |
| 扫码开通（provisioning） | ❌ 不适用 —— 13 个平台都用预置凭据（bot token / API key / 账号密码），无扫码 |
| 25 渠道 / 平台插件化 | ⚠️ 初稿写「我们的方向是"做深"不是"做多"」——**这个前提已不成立**：现在就是 13 个平台，"做多"已经发生。真正剩下的判断是"要不要继续加" |
| 依赖动态安装 | ❌ 不适用 —— 我们零第三方依赖 |

---

## Part 10 · 「复刻两方全部优点」的实施波次

必须先说清：**全部优点 ≠ 全部机制**。Part 5 的"不建议抄"仍然成立
（dsh 的 `toTelegramHtml` 死码、Hermes 的配对码状态机在无开放场景时是纯负债）。
下面按"能拿到的优点全拿、该挡的挡住"排波次。

⚠️ **这是 3 平台时代的计划表，波次本身已不是"待办"。** 下面每条波次后标出实际落地情况，
**保留原编号与原描述**以便看出当初的判断依据。

### Wave 0 · 低风险地基（S 为主，1~2 周，不破坏任何现有行为）
D2 能力枚举 · D3 状态四态归一 + `--status` · **D4 prompt hint 注入** · C1a 分片算法 · C1c 审批超时+迟到去重 · A5 回声防护

> 收益最直观（用户立刻能感到"消息不再被切坏""agent 知道自己在 IM 里""状态一目了然"），
> 零破坏性，可逐项独立发布与验证。

**现状**：D2 ✅ · D3 ✅ · D4 ✅ · C1a ✅ · A5 ⏳ · **C1c ⚠️ 条目本身写错了**——
「审批**超时**」是幻影（本仓库无审批超时），「迟到去重 30s」是 dsh 的数；实际落地的是
`PermissionLedger`（无超时，只拒绝第二次回答）。

### Wave 1 · 安全闭环（M，1.5 周）
A1 脱敏引擎 · A2 授权三段闸门 · A6 `pii_safe` · D1 出站错误分类
　+ **A3 默认收紧**（独立决策：需迁移期与文档，先只加警告不改默认）

**现状**：A1 ✅（2 卡口，非 4；无哨兵，用不可逆遮蔽）· A2 ✅ 闸门那半（13 调用点）·
A6 ✅（带密钥 HMAC，非裸 hash）· D1 ✅ · **A3 ⏳ 仍未做 —— 本波次唯一的遗留项**。

### Wave 2 · 交互深度（M，2 周）
C1b 合并窗口（含快照落盘）· C1d 提问桥 · C1e 三平台按钮 · D5 hint 可覆盖

**现状**：C1b ✅（但形态改了：`..` 才合并 + 15s 保险丝、**刻意不落盘**，见 Part 9 C1b）·
C1d ⏳ · **C1e ⚠️ 「三平台」已错**（13 个平台，inline 按钮实测 1/13，成本要按 12 个平台重估）·
D5 ⏳。

### Wave 3 · 身份模型（L，1.5 周，**破坏性，需单独评审 + 迁移预案**）
B1 `SessionSource` + 唯一真源 + `state.json` 迁移 · B2 多 chat 共享

**现状**：**这条波次已被下面的"80% 方案"取代**——`identity.py` 的 `platform:local_id`
已落地（13 个平台全部走统一 id，歧义前缀拒绝猜测）。完整 `SessionSource` 与 B2 仍未做。

### Wave 4 · 主动能力（M~L，2~3 周，依赖 B3）
B3 渠道地址簿 → E2 主动发送 → E3 文件推送 → E1 定时（自建日历，最后做）

**现状**：⏳ **整条未动**。B3/E2/E3/E1 全部仍是未完成项。

### 待定（按需，不排期）
A4 配对码（要开放给陌生人时）· E4 TUI 面板

### 关键路径与风险

- **Wave 3 是唯一的破坏性波次**，且是 E1/E2 的隐性前置（都要按 chat 寻址）。
  若不想付迁移代价，替代方案是保留 `conversation_id` 字符串但**加入平台前缀**
  （`telegram:<id>` / `slack:<id>` / `discord:<id>`），能解决"跨平台同名串台"，
  代价是 thread/话题仍不可表达——**这是个可以先做的 80% 方案**。
  ✅ **这条 80% 方案已被采纳并落地**（`identity.py`）。所以"Wave 3 是破坏性前置"这个
  风险**已经解除**：E1/E2 现在按 chat 寻址不再依赖 `SessionSource` 迁移。
- **Wave 4 的 E1 成本主要在自建日历**（时区 + DST）。若接受"只支持每天 HH:MM + 星期"
  的子集，可省掉大部分 DST 复杂度（dsh 的两遍法收敛可读）。
  ⚠️ E1 的另一半前提已核实：**v2.0.22 确实没有 scheduler**（Part 8），
  所以"宿主 someday 会给 cron"这个指望可以划掉了。
- **A3 是行为变更**，会打破"配好即用"的现状。建议分两步：先加 `allowed_chat_ids_required`
  之类的显式开关与警告（不改默认），下个 minor 再切默认并给迁移指引。
  ✅ **这条建议已被采纳（决策记录在 `tasks.md` 的「阶段 G」）—— 但采取的形态比这里建议的更好**：
  分两步**不等于两版发布**。本项目**一次发**，用顶层 `config_version` 兜底区分新旧配置：
  **`< 2`**（含没有该键）= 沿用旧语义「空 = 全放行」+ 启动时醒目预告，**`>= 2`** = 新语义「空 = 全拒」。
  配对命令 `/pair` 在**未授权**的 chat 上就能用 ⇒ **没人被困死**；而 `--pair` 成功时会顺手写上
  `config_version: 2`，所以配过对的用户立刻拿到新语义。⚠️ 诚实代价：未配对用户的暴露**仍然存在**，
  且因为没强制，**暴露只会随时间慢慢收窄，不会一天归零**。Part 6 那条 P0 因此**已完整落地**。

---

## Part 11 · A3 webhook 平台验签/回调形状对照（2026-10-10 供数）

> 来源：两份本地快照（文件名含 commit SHA `8a5edab282632443`，锁定参考版本）。
> 起因：`tasks.md`「A3 inbound-push 的真实剩余工作」行的拍板供数——7 家 webhook-only
> 平台（line / teams / sms / synology / zalo / google_chat / whatsapp 官方）在
> `opencode_bridge/adapters/` 全无适配器。用户 2026-10-10 拍板：**A3 拓扑 = 单端口共享
> server**；第二个 adapter 暂不选。本节只取证、不实现。

### 11.1 逐平台形状

| 平台 | 入站回调（参考形状） | 验签（参考实现里实际做的） | 出站 | 出处 |
|---|---|---|---|---|
| LINE | dsh：`/line-webhook`（路径可配），POST，注释明写「要求尽快 200」；hermes：端口 8646 | hermes 文档明写「HMAC-SHA256 signature verification」（适配器本体不在快照）；**dsh 读 `channelSecret` 但零使用（死配置，全快照 5 处命中全是声明+赋值）** | `api.line.me/v2/bot/message/push` + Bearer channelToken（dsh）；hermes 优先免费 reply token、过期退 Push API | dsh `lib/channels/line.js`；hermes `plugins/platforms/line/plugin.yaml` |
| Teams | hermes：SDK（microsoft-teams-apps）的 `POST /api/messages`；dsh：裸 `/teams-webhook`，POST，注释明写「需要 202 且不能处理过慢」 | hermes 由 SDK + Entra client credentials（CLIENT_ID / SECRET / TENANT_ID）；dsh 零验签 | Bot Framework connector：STS 换 bearer（缓存到过期前约 5 min）、按 conversation 存引用 | hermes `plugins/platforms/teams/adapter.py`；dsh `lib/channels/msteams.js` |
| sms（Twilio） | aiohttp webhook（默认 `127.0.0.1:8080`）；响应 = 空 TwiML，回复永不内联 | `X-Twilio-Signature` = HMAC-SHA1(auth_token, URL + 按键排序的键值拼接) 的 base64；带/不带默认端口的 URL **双变体都试**；`compare_digest` 字节比较；**fail-closed：没配公网 `SMS_WEBHOOK_URL` 拒绝启动**（`SMS_INSECURE_NO_SIGNATURE=true` 仅开发用） | REST `api.twilio.com/…/Messages.json`，HTTP Basic(sid:token)，From/To/Body 表单 | hermes `plugins/platforms/sms/adapter.py` |
| whatsapp 官方 | hermes：`webhook_port` 默认 8090；**适配器本体不在快照** | 配置键 `WHATSAPP_CLOUD_APP_SECRET`，setup 明写「App Secret (required for webhook signature verification)」；**验签头形状在快照零命中** ⇒ 选型时须查 Meta 官方文档 | 未见（本体缺席） | hermes `hermes_cli/setup_whatsapp_cloud.py`；⚠️ dsh 的 whatsapp 是 baileys 扫码配对，**不是**官方 API，不能当参考 |
| google_chat | dsh 自造 `/googlechat-webhook`（POST，零验签） | 两家都没有可抄的验签实现（hermes 的 `GoogleChatAdapter` adapter.py 本体不在快照） | dsh：POST webhookUrl（「服务账号方式待实现」） | dsh `lib/channels/googlechat.js`；hermes `plugins/platforms/google_chat/`（仅 stub + oauth/cards） |
| synology | 仅 dsh：`/synology-webhook`（POST，零验签），chatId 硬编码 `'synology'`（单通道） | 无（hermes 无此平台） | incoming webhook URL POST | dsh `lib/channels/synology.js` |
| zalo | 仅 dsh：`/zalo-webhook`（POST，零验签），事件 `user_send_text` + `message.text` | 无（官方 OA 验签形状快照内无实现） | `openapi.zalo.me/v3.0/im/oa/message` + `access_token` header | dsh `lib/channels/zalo.js` |

### 11.2 跨平台结论（供 A3 拍板与基建用）

1. **dsh 的 6 个 webhook 渠道全部零入站验签**（含 LINE 的 `channelSecret` 死配置）⇒ 照抄
   dsh = 开一个谁都能注入的本地端口。唯一有真验签可抄的参考实现是 hermes sms（Twilio），
   且它是 **fail-closed**（没有验签所需的公网 URL 就拒绝启动）——与 a2a
   `_resolve_bind_host`「非回环须有凭据」同一纪律方向。
2. **单端口共享拓扑有现成参考**：hermes `gateway/platforms/shared_ingress.py` 的
   `bind_listener` / `publish_shared_ingress`，sms / teams 适配器以
   `serves_profile_prefix = True` 挂到默认监听器的 `/p/<profile>/…` 路径——与用户拍板的
   「单端口共享 server」同构。
3. **验签形状各家各样**（Twilio HMAC-SHA1 表单排序 / LINE HMAC-SHA256（hermes 文档）/
   WhatsApp app secret 验签（算法细节快照内无）），而参考实现**全部**在各自
   handler/适配器内做验签、没有一家走「服务器级单一 authenticate」⇒ A3 取
   「`require_auth=False` + 验签进各自 handler」分支即可，不需要为异构验签扩
   httpsrv 的 per-route 鉴权。
4. **可抄实现覆盖度**：sms / teams 完整可抄；line / whatsapp 官方 / google_chat 在
   hermes 快照里只有配置键与文档描述（适配器本体缺席）⇒ 选这三家任何一家都要先查
   官方文档补验签细节；synology / zalo 仅 dsh 有（零验签）⇒ 选这两家等于没有验签参考。
