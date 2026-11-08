# 参考项目调查（2026-11-08）

> **用途**：本轮对 `docs/borrowing-catalog.md` 收录的四个参考项目做一次**回看 + 借鉴面盘点**，
> 并据此给出本桥的落地优先级。本文件是**一次性调查快照**，⛔ 不是 catalog 的第二份基线 ——
> 锁定 SHA 与借鉴点清单的**权威出处仍是 `docs/borrowing-catalog.md`**，本文件只做指向与汇总。
>
> **取证口径（三条，缺一条本文件即失效）**：
> 1. **上游结论一律连 SHA**：凡引用参考项目行为，必须写清依据的 commit；⛔ 不写「最新版本如何」。
> 2. **引用本桥代码用符号名，⛔ 不用 `file:line`** —— 行号在别的 lane 插入行后立刻过期（`AGENTS.md` §7.1）。
> 3. **未细读就写「本轮未细读，未结论」**，⛔ 不用「大概/应该」补空。
>
> ⚠️ **本轮取证深度**：只做了 `gh` 现查（HEAD / stars / archived / pushedAt）+ `compare` 计数；
> **⛔ 未逐 commit 读任何参考项目的 diff** ⇒ 本文件**不新增**任何上游借鉴点结论，
> 只登记「HEAD 移动了」与本桥侧（P0）的源码事实。

---

## 0. 摘要

- **四项目本轮均未归档**，三个 HEAD 再前移、一个（gateway）**连续第七轮零差异**。
- 本桥侧两名 **P0** 已由源码坐实（见 §3）：`Turn.tool_trace` 只收集、**零渲染消费**；
  HEAD 的 `TelegramHttp._api` 把 429 重试门在 `_BILLABLE_METHODS` 上 ⇒ **`editMessageText` 撞 429 零重试**。
- ⚠️ **起草期观察到另一条 lane 正在改动 telegram 出站面**（工作树中 `opencode_bridge/adapters/telegram_http.py`
  与未跟踪的 `tests/test_telegram_edit_retry.py`），其内容**指向 P0-2 正在实现**。本文件以**已提交 HEAD** 为基线，
  ⛔ 不据未落地状态下结论（`AGENTS.md` §9：⛔ 不在相关 lane 落地前裁决它们之间的冲突）。
- **license 本轮未独立复核**：四项 `gh repo view --json licenseInfo --jq .licenseInfo.spdxId` **均返回空** ⇒
  沿用 catalog 既有 license 值（Apache-2.0 / MIT / MIT / MIT），**不声称本轮复核过 license**。
- **优先级结论**：P0 = 上述两项；P1 / P2 / P3 是**登记候选**（本轮**未逐项核本桥源码**），见 §5。

---

## 1. 现查事实表

`gh` 现查日期 **2026-11-08**；`gh` v2.96.0；认证账号 `dubuqiangu`。

| 项目 | license（沿用 catalog） | stars（本轮现查） | archived | pushedAt（本轮现查） | 基线 SHA（catalog） | 本轮 HEAD | `compare 基线..HEAD` |
|---|---|---|---|---|---|---|---|
| `MichengAI/dsh-im-connect` | Apache-2.0 | **127** | false | 2026-11-06 | `0e66d949fab51ebfac8948a11f086dee5ac7cff7` | `d869823dac938cc5fc1dbfee3dce23907d3dd8e9` | ahead，**+25 commit / 70 files** |
| `xmanrui/dsh-im` | MIT | **3339** | false | 2026-11-07 | `14f8648e49502bad6b5caf54fea7b7c77dcdfb0c` | `fc2ceab217309f8973afa03c58cdd8ff3e900130` | ahead，**+49 commit / 141 files** |
| `NousResearch/hermes-agent` | MIT | **261525** | false | 2026-11-08 | `016b76da3a8a88e28775e08b6ca2846f471b8eda` | `93a2ae2eac31ccd4d9811411e54338e6a9cfe5ee` | ahead，**+2677 commit / 300 files**（300 = `gh compare` **上限**） |
| `zhuiyueya/dsh-im-gateway` | MIT | **59** | false | 2026-09-27 | `8e8fbca125dda15b5cdd2ae79ac63a91e21f7c37` | `8e8fbca125dda15b5cdd2ae79ac63a91e21f7c37` | **identical（0）** |

**star 变化（catalog 表内值 → 本轮）**：113 → 127 · 3017 → 3339 · 259200 → 261525 · 58 → 59。

**判定依据（可复核命令）**：

```text
gh api "repos/<owner>/<repo>/commits?per_page=1" --jq '.[0].sha'
gh repo view <owner>/<repo> --json stargazerCount,isArchived,pushedAt,licenseInfo
gh api "repos/<owner>/<repo>/compare/<基线>...<HEAD>" --jq '"\(.status) \(.total_commits) \(.files|length)"'
```

⚠️ **本轮判据只有 `gh api` 这一路**（对四个项目都是）—— 本文件**不引用**任何本地 clone 的 `origin/main`
作为判据，也不写「已 `--unshallow` 后 fetch 复核」这类本文件**没有实际执行**的动作。

### 1.1 与编排者所附 FACTS 快照的分歧（逐条；以本表为准）

本轮派单所附的 FACTS 快照与**本表的 `gh` 现查**在下列项不一致 ⇒ 按「star 必须来自自己的 `gh` 调用」
与「⛔ 不编造」纪律，**一律采信本表**：

| 项 | FACTS 快照（⛔ 未采信） | 本轮 `gh` 现查（采信） | 成因 |
|---|---|---|---|
| `xmanrui/dsh-im` stars | 3331 | **3339** | 上游持续涨星；起草期一次中间 poll 已见 3338 ⇒ **同日内在涨** |
| `NousResearch/hermes-agent` stars | 261419 | **261525** | 同上 |
| `NousResearch/hermes-agent` HEAD | `a9ff9799e273f525b82718cdc69fdec71b1dca5a` | **`93a2ae2eac31ccd4d9811411e54338e6a9cfe5ee`** | 上游在一日内**又前移**（起草期中间 poll 见 `2281116525106e3c109993ccb5dc74d02bfc1427`，仍非终值） |

⇒ ⚠️ **这不是「快照错了」，是上游在动**：hermes-agent 的 `pushedAt` = 2026-11-08，与调查同日。
⇒ **动作**：本表是**本节时刻**的快照；catalog 项目一览表按本表就地更新（见 §8 与 catalog 的 2026-11-08 回看段）。

### 1.2 license 复核的边界（本轮未做）

`gh repo view --json licenseInfo --jq '.licenseInfo.spdxId'` 对四项**均返回空字符串** ⇒
**本轮无法据 `spdxId` 复核 license**。⇒ 本表 license 列**沿用 catalog 既有值**，
⛔ **不写成「本轮已复核」**。（`licenseInfo.key` 有值：`apache-2.0` / `mit` / `mit` / `mit`，
但它与 catalog 值一致、且**不是** `spdxId` 口径 —— 记下这一点，⛔ 不拿它冒充独立复核。）

---

## 2. 逐项目回看

> 四节各自四问：**定位** / **本轮回看增量** / **与本桥差距** / **还可借鉴什么**。
> ⚠️ 本轮**未逐 commit 读 diff** ⇒ 「本轮回看增量」只登记 HEAD 移动与计数，
> 「还可借鉴什么」**只汇总 catalog 既有条目**（逐条连其原 SHA），⛔ 不新增结论。

### 2.1 `MichengAI/dsh-im-connect`（Apache-2.0）

- **定位**：把主流 IM 平台（dingtalk / discord / feishu / lark / qq / slack / telegram / wecom / weixin）
  接入本机 DeepSeek Harness 的 TypeScript / Koishi(cordis) 双侧插件；引擎域（去重、按会话串行、合并窗口、
  流式回复组装、长文切分、占位进度、审批/提问交互、会话注册表、原子落盘）与本桥**高度同构**。
  完整借鉴点清单 = catalog §1（A~N，锁 `e271ec1`）。
- **本轮回看增量**：HEAD `0e66d949…` → `d869823d…`（**+25 commit / 70 files**，`pushedAt` 2026-11-06）。
  ⚠️ **本轮未逐 commit 读 diff**（只 poll HEAD + compare 计数）⇒ **不新增借鉴点**；
  catalog §1 / §1.1~§1.4 的结论仍以各自标注的 SHA 为准。
- **与本桥差距**：catalog §1-A/B/C/D **已全部落地**（C `f39882d` · A2 `a21abfb` · A1 `b57341e` · D `2a2e368` · B `c411ea8`）。
  **仍未落地 / 未拍板**：T1 话题群（用户 2026-10-21 拍「暂不支持话题群」）、T2 queue/steer 发送模式
  （与 fix-289「在途 turn 什么都不动」冲突，仅登记）、E 回合纪元（改动面大、需单独立项）、
  U1 逐账号代理（需动 13 个适配器的出站构造 ⇒ 产品判断）、V1 会话轮换（已在台账「dsh-im-connect V1」行）。
- **还可借鉴什么**（均来自 catalog 既有条目，未新增）：§1-E 回合纪元（对「在途 turn + 迟到 delta 另建 turn」
  竞态的根本解法）、§1-G 进度状态机（30 分钟过期清账、typing 租约、按 requestId 认领回合）、
  §1-I 会话注册表 `unbind`/`mute`（本桥 `SessionRegistry` 无对应物）、§1-K 安全姿态文档（威胁模型一句话）、
  §1-M 发送重试阶梯（`SEND_RETRY_DELAYS_MS` + 总预算 —— 与 **P0-2** 同域）、
  §1.2-U2-② 「诊断字段必须过**类型 + 范围**闸再落地」（本桥 `health` 落盘的 `description` 缺这条闸）。

### 2.2 `xmanrui/dsh-im`（MIT）

- **定位**：DeepSeek Harness 官方 Token 赞助的双侧插件，渠道面最宽（README 内置渠道表 12 个 IM 渠道 +
  AI Office Connector）。用户认可的**插件实现参考**；借鉴面分析与落地跟踪见 `AGENTS.md` §6.1 登记行。
- **本轮回看增量**：HEAD `14f8648e…` → `fc2ceab2…`（**+49 commit / 141 files**，`pushedAt` 2026-11-07，
  stars 3017 → **3339**）。⚠️ **本轮未逐 commit 读 diff** ⇒ **不新增借鉴点**；U / AB 裁定不变
  （U 已具备 · AB 暂不做）。catalog §2.5（B1~B10，锁 `e278ac7`）与 §2.1~§2.4 结论仍以各自 SHA 为准。
- **与本桥差距**：`dsh-im 借鉴面落地（①②③）` 行 —— **① 已落地**（渠道级故障隔离与子系统降级，`82f6825`），
  随行 **B3**（Telegram `setMyCommands`，`c4f1571`）与 **B8**（`/status` §3–§8 逐段独立降级，`e92226e`）并入 ①；
  **②（跨 HTTP/RPC 单源错误码表）③（主动投递不可变目标 + 诚实投递语义）仍未落地**。
  B1（按 RPC ID 回查）/ B10（预指定 sessionId）经上游核查 ⇒ 同一条链、缺前提已部分解除；
  B5（按会话隔离工作目录）为产品判断。
- **还可借鉴什么**（均来自 catalog 既有条目，未新增）：§2.5-B2 诊断信息白名单化（沿本桥「平台 `description`
  逐字信道」旧条目）、§2.5-B6 反向查找 + 诚实投递（归 ③）、§2.5-B7 `privateOnly` 单源门控（归 ③ 邻域）、
  §2.5-B9 非 IM 轮次落点记忆（③ 前置件）。

### 2.3 `NousResearch/hermes-agent`（MIT）

- **定位**：**首选 IM 平台工程范式参考**；全量纯文本快照（64 MB / 134 万行，锁 `8a5edab…`）；
  平台清单并集（≈31 家）见 `docs/platform-design-reference.md`。
- **本轮回看增量**：HEAD `016b76da…` → `93a2ae2e…`（**+2677 commit / 300 files**，`pushedAt` 2026-11-08；
  300 是 `gh compare` **上限**，真实文件数 ≥ 300 且未知）。
  ⚠️ **本轮未重跑 `gh search code '/api/event'`，也未逐 commit 读那 ≥300 个文件** ⇒
  「全库零 opencode 事件流消费」这条结论**本轮不更新**：它**最后一次被证据支持**是在 `3cbb13f`（2026-10-21）。
  ⇒ 引用它时必须写明依据版本是 `3cbb13f`，⛔ 不写成「在最新 HEAD 上成立」（那是**未测**）。
- **与本桥差距**：hermes-agent 提供的是**平台工程范式**（目录组织、连接状态归一化、安全姿态），
  而非逐项与本桥对齐的功能。⚠️ 它与 dsh-im / dsh-im-connect **都不消费 opencode 事件流** ⇒
  不能当 opencode 服务端契约依据（那个查 `AGENTS.md` §6.2）。
- **还可借鉴什么**：**本轮未细读，未结论**（本文件只有 2026-10-21 的 `3cbb13f` 证据，且那是别轮的结论）。

### 2.4 `zhuiyueya/dsh-im-gateway`（MIT）

- **定位**：**次选参考**；全量纯文本快照（622 KB，锁 `8a5edab…`）。
- **本轮回看增量**：HEAD `8e8fbca…` **连续第七轮零差异**（`compare` = `identical`，stars 58 → 59）。
  ⚠️ 本仓**无本地 clone** ⇒ 本轮判据只有 `gh api` 这一路。
- **与本桥差距 / 还可借鉴什么**：catalog §4 **未为其单列借鉴点清单**（仅「次选参考」定位）⇒
  **本轮未细读，未结论**。

---

## 3. 本桥基线（P0 的源码依据）

> 本节只记**本桥自己**的事实，引用一律用**符号名**。

### 3.1 P0-1：`Turn.tool_trace` 只收集、零渲染消费

- `event_stream.py` 的 `Turn` 有字段 `tool_trace: list[str]`（声明处），在换轮时 `turn.tool_trace.clear()`
  （`_on_execution_started` 内），在工具事件时 `turn.tool_trace.append(f"▶ {name}")`（`_on_tool_event` 内）。
- ⚠️ **全仓库检索 `tool_trace`：生产侧只有上述三处**（声明 + 清空 + 追加），
  **没有任何一处把它读进发出给用户的进度文本** —— 进度文本来自 `turn.assemble()`（在 `_on_text_delta` 内），
  而 `assemble` 只用 `turn.parts`（assistant 正文分桶），**不含 `tool_trace`**。
- ⇒ **判据（可复核）**：`grep -rn "tool_trace" opencode_bridge/` 的命中里，**除声明/清空/追加外是否还有读取点？**
  本轮答案 = **没有**。⇒ 该字段是「采集了但从未呈现」的**半成品能力**。
- ⇒ **差距**：用户在一轮工具密集的执行里，占位气泡只显示正文流，**看不到正在调什么工具** ——
  而参考项目（如 dsh-im-connect §1-G 进度状态机、hermes-agent 的进度叙述）都把「当前动作」呈现给用户。

### 3.2 P0-2：HEAD 的 `editMessageText` 撞 429 **零重试**

- HEAD（`opencode_bridge/adapters/telegram_http.py`）的 `TelegramHttp._api` 里，
  429 重试**门在** `if method not in self._adapter._BILLABLE_METHODS:` 上 ⇒
  **编辑 / 草稿类方法撞 429 时直接返回失败、不重试**（注释自述「下一帧再改」）。
- ⇒ **后果**：一次流式改写撞上 429 就**白丢当帧**，只能等**下一帧**再改；
  若这一轮没有下一帧（例如正文已停止增长、只剩收尾），**那次改写就永久没发生**。
- ⇒ **参照**：`MAX_RETRY_AFTER`（= 60.0，从 `telegram_protocol.py` 读）与 `_retry_after` 的既有语义
  —— 429 意味着请求被平台**拒绝、无副作用** ⇒ 重试是安全的。本桥的「计费资格」与「重试资格」被**混成了一件**。
- ⚠️ **起草期观察（不作结论）**：工作树中 `opencode_bridge/adapters/telegram_http.py` 被改动、
  且存在未跟踪的 `tests/test_telegram_edit_retry.py` ⇒ 疑似**另一条 lane 正在实现本条**。
  本文件以**已提交 HEAD** 为基线；⛔ 不据未落地状态下结论（`AGENTS.md` §9）。

### 3.3 本桥已落地（⛔ 不重复做）

- **dsh-im-connect A/B/C/D**：C `f39882d` · A2 `a21abfb` · A1 `b57341e` · D `2a2e368` · B `c411ea8`。
- **dsh-im ① + B3 + B8**：`82f6825` / `c4f1571` / `e92226e`。
- **dsh-im §2.1-O**（排队消息跨重启持久化）**本桥已具备**（`_record_inbound` 先于 `_enqueue`；
  先标 `attempting` 后 `prompt()`；409 退回 `pending`）。
- **dsh-im §2.2-R 判据①②**（收尾即终态 / stop 后禁开新载具）**本桥已具备**（`_finalize_session` 弹 turn）。

### 3.4 台账状态

- 未完成项总表**本轮前 = 22 开放行**（与 `tests/test_ledger_plan_completeness.py` 的 PAIRS 22 条逐行对齐）。
- 本批（本调查落地）**新增 1 行**（参考项目借鉴面落地的 P1/P2/P3 轴）⇒ **22 → 23**，PAIRS 同步 23。

### 3.5 落地追记（P0 两项均已落地）

> ⚠️ §3.1 / §3.2 记的是**调查当时（已提交 HEAD）的缺陷态**，作为该次调查的历史快照保留。
> 下记两项的**落地事实**（判据见各自提交与守门）：

- **P0-1 已落地** = `78b01a6`（`feat(event_stream): 占位进度消息渲染 tool_trace`）——
  新增 `Turn.progress_display_text()`（trace 行 + 正文；trace 为空时**逐字节等于** `assemble()`）
  + `EventStream._refresh_progress_with_trace()`（**仅句柄已存在时**编辑，⛔ 不因工具事件新建占位）；
  不变式：`assemble()`（=最终答复）逐字节未变、`shown_progress_text` 三处一律记 body、
  两道长度闸按 display 判（更严 ⇒ body ≤ `one_message_budget` 保持）。
  守门 = `tests/test_event_stream.py::ToolTraceRenderingTests` 8 用例。
- **P0-2 已落地** = `5b37676`（`fix: Telegram 编辑撞 429 改为重试（解耦重试资格与计费）`）——
  `TelegramHttp._api` 的 429 重试门**移除对 `_BILLABLE_METHODS` 的依赖**（计费仍只 gate `_await_send_budget`，未动）。
  守门 = `tests/test_telegram_edit_retry.py` 5 用例 + 既有 `test_telegram_edit_send_budget_wiring.py` 反向锁定。
- ⚠️ **§3.2 那段「起草期观察（不作结论）」现已兑现**：当时在飞的那条 lane 即本条 P0-2，已按 §9
  等它落地并核对（⛔ 未在其在飞时裁决）。

---

## 4. 三视角

### 4.1 架构视角

- **同构度高、差在「能力面」而非「结构」**：本桥的适配器分层（`adapters/base.py` 契约）、
  会话串行（`_drain`）、去重（sqlite 收件箱）、占位进度（编辑式）、入站投递分档
  （`pending` / `attempting` / `outcome_unknown` / `failed`）与 dsh-im / dsh-im-connect 的核心引擎**一一对应**。
- **本轮没有发现「结构级」缺口** —— 参考项目本轮新增的仍是 nao 面（UI、平台专属、产品判断），
  与本桥架构不强相关。
- **真正待补的是横切能力**：会话环/导航、多模态、会话目录隔离 —— 这些**动的是产品边界**，⛔ 不是结构债。

### 4.2 工程视角

- 本桥的**守门 + 反向证明 + SHA 对基线**文化已在多个批次稳定运行（见 `AGENTS.md` §7/§9 与各落地段），
  参考项目本轮提供的多是**判据**（如 U2-② 的「诊断字段过范围闸」），**不是实现**。
- **债务在两类**（`AGENTS.md` §5 / §5.1）：① 生产侧 §5 待拆文件；② 类体量超阈值的类。
  本轮回看**不改变**这两笔债的现状，也**不新增**。
- ⚠️ **纪律成本**：本桥的每一条改动都要求守门 + 反向证明 ⇒ **P0 两项也必须照此办**，⛔ 不因「改动小」而省。

### 4.3 产品视角

- 本轮浮现的候选里，**多条是产品判断、须用户拍板**（⛔ 编排者不代拍）：
  话题群（已拍暂不支持）· 群历史留存（已拍不做）· 逐账号代理 · 会话轮换（长空闲自动开新会话）·
  多机器人（已裁定随插件工作排期）· 多模态 · 会话目录隔离。
- ⇒ 本文件对这类条目的处置是**登记 + 指向**，⛔ 不排期、⛔ 不代拍。

---

## 5. 优先级表

> ⚠️ **P1 / P2 / P3 是登记候选**：本轮**未逐项核本桥源码**，⛔ 未拍板、⛔ 未排期。
> 落任何一条之前**先按 §7 核实 + 量现状**。**P0 两项已由 §3 源码坐实。**

| 级别 | 项 | 依据 / 出处 | 状态与下一步 |
|---|---|---|---|
| **P0-1** | **`Turn.tool_trace` 渲染进占位进度** | 本桥 `Turn.tool_trace`（§3.1：三处收集、零渲染） | ✅ **已落地** `78b01a6`（判据见 §3.5） |
| **P0-2** | **Telegram 编辑瞬时重试**（`editMessageText` 撞 429 零重试） | HEAD `TelegramHttp._api`（§3.2：重试门在 `_BILLABLE_METHODS` 上） | ✅ **已落地** `5b37676`（判据见 §3.5） |
| **P1** | 会话环 / 导航 · `/reset` 语义 · `/new` 只增不删 · 会话列表 / 归档 · 群聊 @闸门 · 多模态 | 本桥现状 vs 参考项目能力面（§4.1） | **登记候选**；本轮未核源码。落地前提见 §7（尤其 `session.list`/`.archive`） |
| **P2** | 占位清理 / 429 冷却 · 投递幂等 · overtaken · deferred turn · transcript 分页 · 命令目录按渠道过滤 · 适配器能力协商 | 参考项目同名机制 + 本桥既有面 | **登记候选**；本轮未核源码 |
| **P3** | i18n · 按钮文案净化 · logging 组件路由 | 参考项目同名机制 + 本桥既有面 | **登记候选**；本轮未核源码 |

---

## 6. 明确不做（附理由）

| 项 | 结论 | 理由与出处 |
|---|---|---|
| 群历史 / 群文件共享存储 | **不做**（用户 2026-10-21 拍板） | 场定位是「IM ↔ 本机 agent 的转发」；留存群消息/文件会扩大隐私面与磁盘占用，保留期默认值是无人量过的数（catalog「已拍板借鉴状态」） |
| Telegram 论坛话题隔离（T1） | **暂不支持**（用户 2026-10-21 拍板） | 维持按 `chatId` 的会话粒度；⚠️ 知情代价：论坛群同一 `chat` 下不同话题共用一条会话 ⇒ 会串消息。日后若支持，⛔ 不照抄「把 threadId 编进 chatId 字符串」的形态（catalog §1.1-T1 / 已拍板状态） |
| 多机器人 / 多凭据账号隔离（AB） | **暂不做**（2026-10-26 裁定） | 大范围架构改动（每机器人配置/状态/会话映射），随插件工作排期一起处理（catalog §2.3-AB） |
| T2 发送模式 queue / steer | **不擅自引入**（仅登记） | 与 fix-289「在途 turn 什么都不动」拍板冲突（catalog §1.1-T2） |
| 按 SLOC / 行数「拆最大的」 | **已被否**（`exp-68`） | ⛔ 不按体量排序拆最大（`AGENTS.md` §5 相关段） |

---

## 7. 需核实（**核实前不动工**）

> 判据：以下每条的**待查证据**都必须先拿到，才能进入实现；⛔ 不凭推测开工。

| 项 | 待查证据 |
|---|---|
| opencode `session.list` / `.archive` / `.fork`（P1 会话环/列表/归档/导航的落地前提） | 一次上游能力核查（`gh` / openapi，锚定运行时 v2.0.22）：这三个 API **是否存在、形状如何** |
| 多模态**出站**与 host 工具产出**形状** | 一次真实抓帧 / schema 核查：出站多模态载荷形状、host 工具产出的文件/图片形状 |
| opencode subagent / 委派 | 核实后决定**桥应透传而非自建**（⛔ 不自造一层委派） |
| transcript 分页**契约** | 上游 `session` 消息列表的分页参数与游标形状（本桥 `OpenCodeClient.messages` 现为 `limit=100` 单页） |

---

## 8. 下一步

1. ~~**落地 P0-1 / P0-2**（§5）~~ ⇒ ✅ **已落地**（P0-1 `78b01a6` · P0-2 `5b37676`，各自独立提交 + 独立守门 + 反向证明，判据见 §3.5）；⚠️ P0-2 落地前已按 §9 等另一 lane 终态、⛔ 未与之并发改同一文件。
2. **P1 各条先核 §7 第 1 项**（`session.list`/`.archive`/`.fork`）；核出形状后再由用户拍板产品边界。
3. **P2 / P3 维持登记**：⛔ 未核源码前不动工。
4. **catalog 基线就地更新为 §1 四新 SHA**（含 2026-11-08 回看段），并记 FACTS 分歧（§1.1）。
5. **继续每日回看**（间隔一天一次，用户 2026-10-16 拍板）；下一次回看仍按 `docs/borrowing-catalog.md`「定期回看机制」节的四步动作。
