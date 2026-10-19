# 全局工作规则

> 记**规则**，不记故事。每条都写清「做什么」和**判据**；踩过的坑只用一行记结论。

## 1. 动手前先上GitHub 搜

**非平凡**实现（新建工具/库/脚手架、选型、接入第三方能力、修别人可能已修的 bug），
动手前先搜 GitHub 确认没有现成实现。

跳过：改动 < ~20 行的仓库内局部逻辑；纯业务/配置/胶水代码；用户已指定实现方式或库。

**怎么搜**（成本由低到高）：① `gh search code '<符号名>' --limit 30` /
`gh search repos '<用途>' --sort stars --limit 20`（官方 MCP 同名工具也在本会话可用）；
② 社区信息 → `agent-reach` skill；③ 读依赖**内部实现** → `clonedeps` skill；
④ 核验 star/license/archived → `github-repo-verification` skill。

**搜完必须落到给用户的回答里**：有成熟实现 → 给对比结论（star/最近提交/license/活跃度/
技术栈是否匹配）并建议直接用或 fork；部分可借鉴 → 引具体文件/行；确实没有 →
一句话说明搜过什么（关键词+范围）再自行实现。**不许静默搜完就自己写。**

## 2. push 前必须脱敏

推送不可逆——推上去的隐私数据收不回来，删 commit 也救不回已被爬取的内容。
**顺序永远是先扫描、后推送。**

> ⚠️ **扫描必须是独立的一步，且要先读它的输出再推。**绝不把"扫描 + `git push` "
> 写进同一条命令——那样 `push` 会在你读到结果前就执行，等于没检查。

按 `git ls-files`（**已跟踪的文件**）扫，不只扫工作区。

### 2.1 凭据：按**形状**查，不按关键字

| 类型 | 形状 |
|---|---|
| Telegram bot token | `\d{8,10}:[A-Za-z0-9_-]{35}` |
| Slack | `xox[baprs]-[0-9A-Za-z-]{10,}`（含 `xapp-`） |
| GitHub | `gh[pousr]_[A-Za-z0-9]{20,}` |
| OpenAI | `sk-[A-Za-z0-9]{20,}` |
| AWS | `AKIA[0-9A-Z]{16}` |
| 私钥 | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |

外加长串字面量赋值：`(token|secret|password|api_key|app_secret)\s*[=:]\s*['"][^'"]{16,}['"]`
——**测试夹具也逃不掉**。

### 2.2 隐私（最常被漏，且与代码正确性无关）

本机用户名 / 用户目录（`C:\Users`、**当前用户名本身**）、绝对路径（盘符目录、
`/home/<用户名>`）、内部域名与内网 IP、公司名与项目代号、调试残留
（`_probe*.py`、`_full.txt`、临时 CSV/日志、截图）。
→ 测试里一律用通用占位符（`example-user`）+ `$USERNAME` 之类的动态断言。

### 2.3 运行期产物

`.gitignore` **只阻止将来添加，已跟踪的不会被移除**。显式确认
`config.json`/`state.json`/`inbox.db`/`*.log`/锁文件/`__pycache__`/`*.session`
都不在 `git ls-files` 里。历史遗留（`git diff --stat origin/main..HEAD` 里已在远端的）
是否清理要问用户，**新引入的一律不发**。

### 2.4 ⛔ 本仓库武装了推送保护 —— 合成夹具必须**拼接**

推送保护按**完整密钥形状**拦截整个 push，而 §2.1 说「测试夹具也逃不掉」——
脱敏器的测试**本质上就需要**真形状。两条规则在这里对撞。

**解法**：夹具由片段拼接，仓库里不出现任何连续匹配形状的字面量，而**运行值逐字节不变**
（脱敏器只认形状，测试行为与断言都不必改）：

```python
SLACK_TOKEN = "xox" + "b-" + "<数字段>" + "-" + "<密钥段>"   # 示例用占位段
```

- **只改表达方式，绝不改运行值**。值变了则行为变了，而 `assertNotIn(secret, scrubbed)`
  这类断言**可能照样过** —— 没有任何东西会报警。
- **文档本身也别抄完整密钥**。示例一律用占位段（这条规则写下时我在自己的示例里抄了
  真 token，自查当场抓出来）。
- **⛔ 不要用 GitHub 的 "unblock secret" 链接。** 那等于告诉 GitHub「这个仓库里这类
  形状别管了」，**永久削弱此后每次推送的保护**，代价仅为一个假阳性。它是**可点的、
  看起来很方便、后果不可逆**的那种操作。
- **自查必须用完整形状，不是裸前缀**：裸前缀（`xoxb-`/`ghp_`/`sk-`/`AKIA`）在仓库里出现
  几十处（README、`docs/`、错误文案）**从未被拦过**。扫描时**要排除形状表自身** ——
  本仓库因此把 Slack 形状写成 `x(?:ox[bp]|app)-`，语义相同但形状表不会匹配自己。
- 含完整形状的 commit **若从未推送**：`git reset --soft HEAD~1` 后重提，
  **不要**让它留在本地历史里。

## 3. 命名必须见名知意

变量/方法/类/文件/常量名都要见名知意。禁止：

- **单字母或无意义缩写**（循环变量 `i`/`j` 除外）：`d`/`tmp`/`val`/`obj`/`data`/`res`/`p`/`h`/`f`/`cfg`/`ctx`
- **带数字后缀的"第几个"**：`data1`/`msg2`/`handler3` —— 它们本该是**不同的概念**，
  该换个名字表达差异，而不是加数字
- **拼音或拼音缩写**：`biao`/`mingxi`/`canshu`
- **中英混杂的自造词**：`wenJian`（方法名驼峰而变量用下划线）
- **测试里的占位名**：`t1`/`t2`/`foo`/`bar`/`xxx` —— 断言的具体内容要出现在名字里
- **缩写歧义**：`cfg` 是config 还是 configure？`h` 是 height 还是 handle？**拿不准就写全**
- 文件名同理：`a2a.py` 可以（平台名），`utils2.py`/`common3.py`/`tmp_helper.py` 一律不行

**边界：协议自己的字段名不算违规。** `data`/`type`/`id`/`value`/`result`/`status`/`error`
在协议里本来就这么叫，沿用原名是**对的** —— 改名会让代码与线上格式对不上。
所以 `response.get("data")` ✅、`data = self._request(...)` ❌（自起名，必须改）、
`inner = data.get("data")` ❌（局部变量与 wire 字段同名，一个名字承载两层含义）。

**判据**：这个名字是协议给的，还是我们自己起的？**新读者不看注释能不能猜出它是什么。**
猜不出就改名，不要靠加注释补救——注释会过期，名字不会。

## 4. 其他

- 有专门的 skill 就先加载 skill，不要凭记忆硬做。
- 改配置文件前先备份（带时间戳的 `.bak.YYYYMMDD_HHMMSS`）。
- **不要编造** star 数、license、版本号 —— 用 `gh` 现查。

### 4.1 未完成的事必须声明下一步

**一条未完成的事，要么写下一步，要么【显式】写「无计划 + 为什么」** —— ⛔ 不许只记事实。

**为什么这条要单列**：本仓库台账里，有计划的那几条**全是表格行**（带优先级列 +
依赖列），没计划的那几条**全是散文里的记述** ⇒ 「忘了写计划」与「决定不写计划」
在数据上**长得一样**，无法区分。

⚠️ **它与本文件另外三条同源**，而那三条的成因都写着同一句话：
「**规则只活在派单 prompt 里就等于没有规则**」（`.tmp/` 不许删、lane 不许
`git stash`、权限按意图不按目录前缀）。⇒ 这一条更隐蔽：
它不是「规则写在别处」，而是**数据结构里没有那个字段**。

**判据**：写下「X 未做」的同一段里，必须出现下列之一：

1. **下一步** —— 做什么 / 谁做 / 何时；
2. ⛔ **无计划**，四选一，**且每类各自的必填件不同**（⛔ 一刀切会让某一类语义不兼容）：

   ⚠️⚠️ **而「有计划、有前置、就是没人认领」一个都不属于下面四类** ——
   实测本仓库的 D3 交互按钮就是这一类（优先级 M、前置 T1.1 已完成、
   能力声明已接进 `--status`、⛔ 零人接手也没有排期），而它当时被判成
   「3 表内 ⇒ 合规」。⇒ 所以它是**第五类**，必填件是
   **「认领人是谁 + 何时认领」**；⛔ 若确实没人可认领，**写明为什么**
   （例如「需要用户先拍板 X」），⛔ 不许用问号代替。
   （该实例已于 2026-10-11 由编排者认领并落地首个平台 Slack，见总表 ~~D3~~ 行——
   本条作为第五类的**出处**保留，实例现状以总表为准。）

   | 类别 | 必填件 |
   |---|---|
   | **产品判断等用户拍板** | 要拍的是**哪个问题**，一句话、可复述 |
   | **根因未证实** | **待查证据**：哪个文件 / 哪次运行 / 哪个数字；⛔ 没有就写「现在没有」 |
   | **代价已知且刻意不做** | **代价是什么、为什么可接受** —— ⛔ **不适用**上面那条「待查证据」 |
   | **判据尚未标定** | 标定它需要**哪组数** |

   ⚠️ **四类都不许只贴标签** —— 贴「根因未证实」而不指证据是**自证式出口**，
3. 归属到一张带优先级列与依赖列的表 —— ⛔ **但那只是落点，不构成声明**：
   该行仍须满足判据 1 的「**谁做 / 何时**」。
   ⚠️ **优先级格非空不构成声明** —— 实测那张表里 10 行有 **3 行**填着
   **「M」「待定」「TBD」**，它们与空格**同值**（无信息），其中一行正是**零人接手的 D3**
   ⇒ 「非空」抓不到它本要抓的那一行。
   ⇒ 判据：那一格必须**指向一个具体的人或一个具体的日期**。
   ⚠️ 同一条要求也适用于上面那个**第五类**（有计划但无认领人）：
   「M」+「排期人是？」**不构成声明** —— 前者是优先级、后者是提问，都不是认领。

⚠️ **可机械判定的触发句【可以】算下一步** —— 例如「`grep -c '^class ' adapters/` ≥ 14」
或「等上游 v0.10 出来」「等覆盖率连续三次低于 X」：它们比「谁做 / 何时」**更好**，
因为可复核。但必须同时留下两样东西：
**① 触发条件的判定方法**（一条可复核的命令，或一个可数的事实）、
**② 最近一次检查的结果与日期**。**缺任一项 ⇒ 不算下一步**，按「判据尚未标定」处理。
⚠️ 本仓库吃过一次：台账写「现在到了该上的时候」，触发条件是
「等到真要在群里开放给陌生人再上」—— 而**这个条件从未被检查过**，
所以「现在到了」没有依据。
⚠️ **不可判定**的触发（「等时机成熟」「等稳定下来」）**一律不算** ——
它们不是触发，是**没有判据的托词**。

⚠️ **这条规则在自己仓库里的第一次应用就漏了一个实例** —— 而那个实例
**就在本节下方 30 行**（§5.0 里的「判据缺失」）⇒ 所以「自查段」= **本小节末尾那四项列表**；它必须包含**紧邻的实例**（⛔ 不只是「远处的已知项」），
**且必须与 `tasks.md` 的「未完成项总表」逐行对齐** —— 两处数目不等就是漏了（判据：数一遍两边的行数）。
⚠️ **台账里已有的待办要用这条重新过一遍**（2026-10-11 A3 闭合 + D3 首个平台 Slack 落地 + telegram callback 文案零钉闭合后的状态：**20 项**——D3 行闭合、D3 剩余平台行新开，两抵仍 21；「callback 文案零钉」行闭合（`0e3794c`）⇒ 21 − 1 = 20；
逐项类别与必填件见 `tasks.md` 的「未完成项总表」⇒ ⚠️ **两边数目必须相等**，判据是数一遍行数。**2026-10-16 更新：当前 23 项**——2026-10-15 新增「dsh-im 借鉴面落地」行时此处未补记（彼时实为 21，今补记）；本轮再新增「dsh-im-connect 借鉴面落地」「参考项目定期回看」两行 ⇒ 23）：
~~D3 交互按钮~~ ⇒ **已闭合** ✅（2026-10-11 · 编排者认领并落地首个平台 Slack `432720e`，范围与证据见总表 ~~D3~~ 行——「多平台」只闭合了 Slack，出站/入站/answer 镜像 telegram 契约）；
**D3 剩余平台的交互按钮** ⇒ **第 1 类**（中 · 认领人=编排者 · 2026-10-11 起）——
要拍的问题（一句话可复述）：「Slack 已落稳，D3 是否连带 Discord，还是止步于 Telegram+Slack？」
（用户裁定后排期，⛔ 不编日期）；
**dsh-im 借鉴面落地（①②③）** ⇒ **第 1 类**（中 · 认领人=编排者 · 2026-10-15 认领起）——**用户 2026-10-15 已拍板：①渠道级故障隔离与子系统降级 ②跨 HTTP/RPC 单源错误码表 ③主动投递不可变目标模型与诚实投递语义，全部进入插件工作排期**（证据与分析见 §6.1 登记行，参考锁 `b7698a6`）；下一步 = 插件工作排期启动时逐项落地（排期由用户发起，⛔ 不编日期）；
**dsh-im-connect 借鉴面落地（A/B/C/D）** ⇒ **第 1 类**（中 · 认领人=编排者 · 2026-10-16 认领起）——**用户 2026-10-16 拍板：A 投递三态+手动补投、B 冷历史恢复答复、C 结构感知切分、D 审批超时语义，全部进入插件工作排期**（分析与适用性 = `docs/borrowing-catalog.md` §1 借鉴点清单 A~N，参考锁 `MichengAI/dsh-im-connect` commit `e271ec1`；落地跟踪见总表同名行）；落地状态 = **C 已落地**（`f39882d`，2026-10-16：`split.py` 结构感知切分 + 新增 `markdown_blocks.py`，守门 tests/test_split_markdown.py）；**A2 已落地**（`a21abfb`，2026-10-16：传输层失败按「请求有没有离开本机」分两档 —— 新增 `request_phase.py` + `OpenCodeError` keyword-only 字段 `request_never_sent`（默认 False ⇒ 既有行为逐字节不变）+ `_dispatch_prompt` 的 `status is None` 分支拆两档：可证明「从未提交」⇒ 复用既有 `failed`（可重放 ⇒ 自动重投），其余维持 `outcome_unknown`（只告警不重放），⛔ 不新增状态档；用户可见文案 `PROMPT_NEVER_SENT_NOTICE` 只承诺「已记录，重启后会自动重投」，⛔ 不写「请重新发送」；判据 = 链中 `ConnectionRefusedError`/`socket.gaierror` ⇒ True，`TimeoutError`/`ConnectionResetError`/`RemoteDisconnected`/`SSLError` 等一律 False（fail-safe）；守门 = tests/test_request_phase.py 15 用例 + 网关两臂 + `mark_failed` 调用点 AST 守门 3→4，定向 185/185 + 更宽 283/283 全绿、反向证明 3/3（M1 判据恒 False ⇒ 5 真红 / M2 网关消费侧短路 ⇒ 恰好 2 红 / M3 客户端不盖戳 ⇒ 1 红）；⚠️ 知情代价：从未提交的消息也走退避阶梯与预算、最终可能 abandoned）；**A1 已落地**（`b57341e`，2026-10-16：手动补投 = 群内命令 `/delivery retry`（列出本会话待补投项 + 按序号补投一条）—— 新增 `delivery_retry.py`（`DeliveryRetryCommand.reply_for(conversation_id, args) -> DeliveryReply`，⛔ 不加厚 `CommandHandler`，照 `SessionModelCommand` 委派模式放进新模块）+ 第 9 协作者 + 命令表键 `"delivery"` + `HELP_TEXT` 一行；候选集 = `pending_prompts()` + 到期的 `due_failed_prompts(now)`（按 `conversation_id` 过滤后拼接，pending 在前），⛔ 不列 `attempting` / `outcome_unknown` / 未到期 `failed` / `abandoned` / `delivered`；单条重投复用 `inbox_recovery.replay_one`（公开 + 加进 `__all__` + 四个返回常量，⛔ `not_attempted` 不许并进 `failed`）、`dispatch` 复用网关恢复路径 `dispatch_recovered`（⛔ 不另开一套映射）+ `core.py` late-bound 转发（`CommandHandler` 早于 `inbound_gateway` 构造）；判定**显式穷举** + fail-safe 兜底（§7.1 规则⑥），abandoned 判定不碰 `mark_failed` 契约；⭐ 用户 2026-10-16 拍板：只列 `pending` + 到期 `failed`、手动重投失败消耗退避预算；⚠️ 知情代价：409「agent 正忙」也落 `failed` 并消耗一级预算、序号在重投时重算（不便、非安全风险）；守门 = tests/test_delivery_retry.py 28 用例（含 AST 形状守门 + 对照臂），定向 236/236 全绿、反向证明 3/3（各自还原 SHA256 对基线 `9fe82cac…`））；**A/B/D 前置核实已完成**（结论 = catalog §1「A/B/D 落地核实」小节）——A：现有三档在 `_request` 层**无法区分「确定未提交」与「可能已提交」**且**手动补投面缺失**；B：`OpenCodeClient.messages` API 存在但**生产零调用点**、**失败路径拿不到 prompt 的 msg id**（§1-C 的 requestId 匹配不可照搬）；D：`_on_permission_asked` 只渲染 + 记账、**无等待 / 超时**（服务端 busy 间接门控）；**下一步 = D → B 逐项落地**（用户 2026-10-16 已拍板形状：A 手动补投 = **群内命令 `/delivery retry`**（列出本会话待补投项 + 按序号补投指定一条）· D = **桥侧定时器、等待上限 5 分钟、到期按取消（非拒绝）**，本轮范围只做超时（不含「群聊只认发起者」「`accepting` 旗」）· B 对接口径 = **先按 `queued.text` + 时间序**定位 assistant 正文、⛔ 不照搬 requestId 匹配），每项独立提交 + 独立守门 + 反向证明，编排者执行、⛔ 不编日期；**D 已落地**（未提交，2026-10-18）：**ledger-only 取消** —— 前置核实定案上游 v2.0.22 无「取消」API ⇒ 每 pending 请求 5 分钟 Timer 到期**不发任何 API**、ledger 标第五态 `ANSWER_CANCELLED` + IM 告知（⛔ 服务端那条请求永久挂到服务关闭）；新增 `permission_timeout.py`（`PermissionTimeoutWatcher`）+ `record_cancelled`（不重开已关闭守卫）+ 三处真回复落点 `cancel`，守门 = tests/test_permission_timeout.py 12 用例 + ledger 472/472 绿 + 反向证明 3/3（详情见总表「dsh-im-connect 借鉴面落地」行与 catalog §1-D）；**B 已落地**（`c411ea8`，2026-10-19：冷历史恢复答复 = 从冷历史恢复最终 assistant 正文投给用户、**不调用模型、不重放工具** —— 新增 `cold_history_recovery.py`（纯函数 `find_reply_for_prompt` + `_user_text` + `_assistant_text`，69 物理行 <400；⛔ 不照搬 requestId 匹配，按 `queued.text` + 时间序定位「首个匹配、且之后确有 assistant 正文」的 user 消息；interim 抑制 = 匹配 user 后面只有纯工具调用的 assistant ⇒ 返回 None）；`inbox_recovery.py` 加 `RecoveryOutcome.recovered` 字段 + `recover_pending` 可选 `recover_cold` callable（默认 None = 既有行为不变）+ step 3 拆分（`recover_cold` 非 None ⇒ `mark_delivered` + 入 `outcome.recovered` + 不入告警；None/抛异常 ⇒ 保留既有告警，fail-closed）；`inbound_gateway.py` 加 `SessionFor` 类型别名 + `session_for` 协作者（第 15 个）+ `recover_inbox` 定义本地 `recover_cold` 闭包（fail-closed：取历史失败/无会话 ⇒ 回退既有告警）+ `core.py` 注入 `session_for=self.conversation_state.get_session`（只读查找，不创建）；守门 = tests/test_cold_history_recovery.py 19 用例 + tests/test_inbound_gateway.py 协作者面更新（`build_gateway` 补 `session_for` 桩 + 15 协作者断言），定向 342/342 全绿、反向证明 3/3（M1 `find_reply_for_prompt` 恒回 None ⇒ 8 真红 / M2 interim 抑制被破坏 ⇒ 4 真红 / M3 `recover_pending` 忽略 recover_cold ⇒ 2 真红，各自还原 SHA256 对基线 `9AD30FFD…`/`E2C7FC64…`）；⚠️ 知情代价：同文本重发取**时间序内首个匹配、且之后确有正文**的 user 消息，弱于参考、会歧义，这是**确定性选择**（catalog §1-C 已记））；**下一步 = 排期面空闲时回第三刀 `OpenCodeClient` 类手术**（§5.1）；**B 已落地**（`<COMMIT>`，2026-10-19：冷历史恢复答复 = 从冷历史恢复最终 assistant 正文投给用户、**不调用模型、不重放工具** —— 新增 `cold_history_recovery.py`（纯函数 `find_reply_for_prompt` + `_user_text` + `_assistant_text`，69 物理行 <400；⛔ 不照搬 requestId 匹配，按 `queued.text` + 时间序定位「首个匹配、且之后确有 assistant 正文」的 user 消息；interim 抑制 = 匹配 user 后面只有纯工具调用的 assistant ⇒ 返回 None）；`inbox_recovery.py` 加 `RecoveryOutcome.recovered` 字段 + `recover_pending` 可选 `recover_cold` callable（默认 None = 既有行为不变）+ step 3 拆分（`recover_cold` 非 None ⇒ `mark_delivered` + 入 `outcome.recovered` + 不入告警；None/抛异常 ⇒ 保留既有告警，fail-closed）；`inbound_gateway.py` 加 `SessionFor` 类型别名 + `session_for` 协作者（第 15 个）+ `recover_inbox` 定义本地 `recover_cold` 闭包（fail-closed：取历史失败/无会话 ⇒ 回退既有告警）+ `core.py` 注入 `session_for=self.conversation_state.get_session`（只读查找，不创建）；守门 = tests/test_cold_history_recovery.py 19 用例 + tests/test_inbound_gateway.py 协作者面更新（`build_gateway` 补 `session_for` 桩 + 15 协作者断言），定向 342/342 全绿、反向证明 3/3（M1 `find_reply_for_prompt` 恒回 None ⇒ 8 真红 / M2 interim 抑制被破坏 ⇒ 4 真红 / M3 `recover_pending` 忽略 recover_cold ⇒ 2 真红，各自还原 SHA256 对基线 `9AD30FFD…`/`E2C7FC64…`）；⚠️ 知情代价：同文本重发取**时间序内首个匹配、且之后确有正文**的 user 消息，弱于参考、会歧义，这是**确定性选择**（catalog §1-C 已记）；）；**下一步 = 排期面空闲时回第三刀 `OpenCodeClient` 类手术**（§5.1）；
**参考项目定期回看** ⇒ **第 1 类**，下一步**可机械判定**（触发 = 每日首次开工时对四收录项目跑 HEAD SHA 比对 + `isArchived` 归档核验，判定方法全套见 `docs/borrowing-catalog.md`「定期回看机制」节；间隔 = **一天一次**，用户 2026-10-16 拍板；最近一次检查 = **2026-10-16**（基线建立同日）：四项均成功取到 HEAD SHA 且均未归档，基线四 SHA 见 catalog 项目一览表）；
~~`:882` 错误正文超长~~ ⇒ **已闭合** ✅（2026-10-09 · 核验 · 零代码）——
用户已拍「自动拆成多条连续消息」，而待核判据（找出调用点、看有没有走 `split_text`）已核：
调用点 = `_on_execution_failed` → `outbound.finalize(kind="error", shown_progress_text="")`，
今天的 `finalize` 没有「跳过编辑」的长度守卫 —— `_split_for_the_placeholder` 用
`split_text(prefix_fmt="")` 按 `one_message_budget` 切 head/tail，head 改写进占位气泡、
tail 与整段补发（`shown=""` ⇒ 整段）都走 `send_text` → `adapter.send()` → `split_text`
⇒ 拍板已由既有机制满足（守门 = tests/test_outbound.py 四条 + test_event_stream.py
错误文案三处，定向 111/111 绿）；
（**email 主题隐私** 2026-10-06 已决「不算隐私，照常记录」⇒ **已从本清单移出** ——
它不再是未完成项，决策与代价留在总表那一行，与 `~~698 行~~` 同一形态）
G1 桥每 5~7 分钟退出 ⇒ **第 2 类**（根因未证实；待查证据 =「退出前的日志与退出码」，
⛔ **现在没有**）；
（**G2 崩溃窗口残留** 2026-10-10 已闭合（`28b1343`）⇒ **已从本清单移出** ——
当初与 G1 并列为第 2 类，是 `ora-14` 复核**之前**的状态：其根因后经 `ora-14` 证实
（HELD 早退在写前收件箱之前、缓冲纯内存）⇒ 归第 1 类并于 2026-10-10 修复
（快照 + 重灌，守门与知情代价见总表 `~~G2 崩溃窗口残留~~` 行））；
测试侧 §5 触发线标定 ⇒ **第 4 类**（需要的数 =「单职责测试文件的长度分布」，⛔ 谁也别先拍）；
生产侧 §5 拆分 **16** 个文件 ⇒ **第 1 类**，下一步**可机械判定**（§5.0 口径表 + 最近一次实测值 = 2026-10-19 于 `c411ea8`：超线 **27** / 豁免 **11** / 待拆 **16**（tracked 200→**204**；`inbound_gateway.py` 1235→**1281**、`inbox_recovery.py` 417→**449**，个数逐字不变；B 新模块 `cold_history_recovery.py` 69 <400 不入名单；重算脚本 exit=0、锚点全复现 + 两陷阱自检通过，详情见 §5.0 数据节）；上一轮 = 2026-10-16 于 `b57341e`：超线 **27** / 豁免 **11** / 待拆 **16**：超线 26 / 豁免 10 / 待拆 16 **个数逐字不变**（`inbound_gateway.py` 45.8%/595 → **45.9%/618** ⇒ **债在长**、仍被②拦下 ⇒ 仍待拆；A2 新模块 `request_phase.py` 79 <400 不入名单；锚点全复现 + 两陷阱自检通过；tracked `.py` 194→198 = C 与 A2 各 +2）；再上一轮 = 2026-10-15 于 `983130b`：超线 26 / 豁免 10 / 待拆 16 个数不变，`a2a.py` 1273→1196 不退线、第十一刀新模块 `a2a_task_ledger.py` 174 <400 不入名单；更早 = 2026-10-15 于 `3702b15`：超线 26 / 豁免 10 / 待拆 16，`telegram.py` 1381→1107 不退线、第九刀新模块 `telegram_credential_lifecycle.py` 426 >400 但文档占比 50.7%/SLOC 186 入豁免）；⭐ **第十一刀已落地（类手术第二刀，`983130b`，2026-10-15）**：`A2aAdapter` 类手术（§5.1 获批后的第二剪；纯仓库内结构调整、§1 GitHub 搜不适用）——Task 账本域 `_Task` dataclass + 7 方法（`_lookup`/`_new_task`/`_register`/`_trim_locked`/`_drop_locked`/`_finalize`/`_fail_pending`）字节级迁入新类 `A2aTaskLedger`（`a2a_task_ledger.py`，174 物理行；logger 同通道名 `opencode_bridge.adapters.a2a`，日志逐字节不变）；状态留守 `_tasks`/`_order`/`_lock`/`max_tasks`，新类 4 个 read-only property 逐调用转发回 `self._adapter.X`（第九刀定案形态，方法体零字节改动）；适配器保面 = 5 转发方法（`_lookup`/`_new_task`/`_register`/`_finalize`/`_fail_pending`）+ `__init__` 挂 `self._task_ledger` + 顶部再进口 `A2aTaskLedger`/`_Task`，`dataclass`/`field` import 经 AST 判定零引用后修剪；依赖单向 a2a → a2a_task_ledger → a2a_protocol（TYPE_CHECKING 引 `A2aAdapter` 不成运行期回环）；AST 同一性 8/8 + 转发纯度 5/5 + self.X 闭包 7/7 + symtable 双模块逐作用域零缺口 + 双文件 compile 干净 + 方法名集 35→33（`_trim_locked`/`_drop_locked` 内化）；定向基线 7 模块 305/305 全绿零差异（test_a2a 134 + test_config_coerce 42 + test_config_runnable_verdict 33 + test_inbound_log_structure 5 + test_inbound_log_per_platform 18 + test_log_token_redaction_structure 14 + test_adapters 59）；反向证明 3/3（M1 `_finalize` 恒回 False ⇒ 19F+3E 真红 / M2 `_register` 早退 ⇒ 21F+5E 真红 / M3 `_fail_pending` 恒回 0 ⇒ 1 真红 = `test_stop_is_fast_even_with_a_parked_request`），各自还原 SHA256 对基线；⚠️ 守门缺口如实登记：`_trim_locked`（连带 `_drop_locked`）零咬合——`test_task_ledger_is_bounded` 断 ≤32 而 20 条挂账也过线、区分不了裁剪行为；M3 初选 `_trim_locked` 时的 1 条红经复跑证实为瞬时污染（413 尺寸闸测试、与 `_trim_locked` 唯一调用点 `_register` 无因果链）⇒ 已换 `_fail_pending`，缺口系既有、非本刀引入；completeness 6/6 + compileall 干净 + isolation scan exit=0；⛔ 未跑全量 ×3；`a2a.py` 1273→1196 **不退线**；术后残余 = `A2aAdapter` 33 方法 / SLOC 579（术前臂同脚本量于 `8b70a1d`：35/633）——**两项仍超 §5.1 线**，第二剪如实报、不声称退线，第三刀 `OpenCodeClient` 待排期；⭐ **第九刀已落地（类手术第一刀，`3702b15`，2026-10-15）**：`TelegramAdapter` 类手术（§5.1 获批后的第一剪；纯仓库内结构调整、§1 GitHub 搜不适用）——凭据生命周期域 8 方法（`_credential_gate`/`_probe_until_credentials_verified`/`_invalidate_credentials`/`_reverify_runtime_credentials`/`_announce_runtime_credential_recovery`/`_probe_get_me_once`/`_log_credential_failure`/`_announce_credential_recovery`）+ 5 状态属性字节级迁入新类 `TelegramCredentialLifecycle`（`telegram_credential_lifecycle.py`，426 物理行/50.7%/SLOC 186 ⇒ §5.0 豁免）；适配器保面 = 4 转发方法 + 2 property 对（`_credentials_verified`/`_runtime_credentials_invalid`）+ start 2 行重指向，留守 = 阶梯 3 方法（getattr 旋钮）+ 落盘记录 2 方法（`health.platform_key` 要适配器本体）；⚠️ 初版「构造时注入绑定可调用」被定向复跑证伪（构造后打桩 `adapter._post` ×11、`adapter._stop_event` ×2 全部失明 ⇒ credential_gate 13/17 真红、真网 89.5s）⇒ 改为 8 协作者 read-only property **逐调用转发回 `self._adapter.X`**——与手术前动态解析语义一致、方法体零字节改动（AST 同一性 8/8；教训写进新模块 docstring）；留守 5 方法 dump 不变 + 方法名集 40→38 + symtable v4 双模块零缺口 + self.X 引用闭合审计 + 新类供给面断言（`__init__` 5 状态属性 + 8 协作者 property）；定向基线 241/241 全绿零差异（134 + 23 + 84 三批逐模块对基线）；反向证明 3/3（M1 闸门早退 15 真红 / M2 探测恒回假 401 15 真红 / M3 失效置位早退 7 真红），各自还原 SHA256 对基线；completeness 6/6 + compileall 干净 + isolation scan exit=0；⛔ 未跑全量 ×3；`telegram.py` 1381→1107 **不退线**；术后残余 = 唯一方法名 38 / SLOC 622（原 40/751）——**两项仍超 §5.1 线**，第一剪如实报、不声称退线，后续剪与 A2aAdapter/OpenCodeClient 待排期；⭐ **第八刀已落地（`54ec0ec`，2026-10-14）**：`telegram.py` 文件刀（刀路依据 = 缝质量与零钉点，第五刀 a2a 同型；纯仓库内结构调整、§1 GitHub 搜不适用）——模块级「协议限额 + wire 纯函数」域 19 名字（17 常量 + `_retry_after` + `_button_row`）字节级按内容锚切块逐字移植 `telegram_protocol.py`（153 物理行）+ `telegram.py` 顶部再进口块保既有 import 路径与 `telegram.X` 属性语义（消费面普查：19 名在 tests/ 全是 import 形态、零 mock.patch 钉点；源码锚点扫描守卫 needle 全落类体、零重指向）；AST 同一性 19/19 + 顶层名字集全等 + symtable 逐作用域审计（v4）双模块零缺口；消费面基线全等 = 拆分前（干净 `52e974b`）telegram 定向 134/134 + 结构/消费 107/107 ≡ 拆分后逐模块零差异；completeness 6/6 + compileall 干净 + isolation scan exit=0；反向证明 3/3（M1 `_retry_after` 恒回 None ⇒ 5 真红 / M2 `_button_row` 丢长度闸 ⇒ 3 真红 / M3 `POLL_TIMEOUT_UPPER_LIMIT`=0 ⇒ 3 真红），各自还原 SHA256 对基线；⛔ 未跑全量 ×3；残留 `telegram.py` 1381 **不退线**——`TelegramAdapter` 类体（40 方法/SLOC 751，量于 `54ec0ec`）= §5.1 类手术区（**用户已批准类手术**；第一剪 = ⭐第九刀，见本行 saga 头部）；⭐ **第七刀已落地（`1ec8fd7`，2026-10-14）**：`__main__.py` 第二剪（装配 vs 域视图轴向，延续第四刀裁定；纯仓库内结构调整、§1 GitHub 搜不适用）——启动域（`ALREADY_RUNNING_MESSAGE`/`NO_ADAPTER_MESSAGE`/`REFUSAL_STAGE_*` 常量 + `_bridge_dir`/`_record_bridge_refusal`/`_inbox_path`/`_held_buffer_path`/`run_bridge`/`_run_bridge_locked`）逐字移植 `bridge_runner.py`（399 物理行）+ `--status/--check` 视图域（`_pid_alive`/`_NullHooks`/`_adapter_capabilities`/`_channel_config_rows`/`_runtime_state`/`_print_event_subscription`/`run_status`/`run_check`）逐字移植 `platform_status.py`（283 物理行）+ 交互配置视图域（`_platform_status`/`run_setup`）逐字移植 `setup_view.py`（196 物理行）；`__main__.py` 重写后 217 物理行**退线**，留守 = `{__all__, logger, _setup_logging, build_parser, main}` + 顶部再进口块保既有 import 路径与 patch 语义；⚠️ **拆分脚本「自由名分析无漏报」声明被证伪**：生成式 import 修剪按函数自身 import 计算需求，没算「原文靠源文件模块层 import 解析」的引用 ⇒ `platform_status._adapter_capabilities` 引 `build` 而模块层不供给（原文靠 `__main__` 模块顶层 `from .adapters import build` 解析）⇒ 按原文语义在模块层补同一条 import；其余 3 模块经 symtable 逐作用域审计（v4）零隐式全局缺口——**判据教训：模块级 Name 扫描（v1 误报局部量）/漏 def/class 名（v2）/合并作用域（v3 恰好掩掉真缺口）都不够，跨函数作用域必须逐作用域判（is_global() 且模块层不供给 = NameError 候选）**；消费面重排 = 5 个 mock 钉点文件 50 处 A 类站点（test_bridge_refusal_probe / test_conversation_id_cutover / test_core / test_inbox_wiring / test_platform_health；旧目标失效形态 = 响亮 AttributeError、非静默）+ ⚠️ **源码锚点扫描类**（mock 探针 census 天然不覆盖）：test_outbound_failure_channel 3 处锚点 `__main__.py` → `bridge_runner.py`（`MAIN_MODULE_PATH` → `STARTUP_MODULE_PATH`；⭐ 源码扫描形态拆分后是**响亮红**——空集断言当场失败，正是消费面基线抓出三红的方式，不像 mock 钉点经再进口块会静默失明）+ isolation 守卫扩展（新常量 `CLI_ENTRY_FUNCTION_GRAPH_MODULES` = 4 模块并集函数图 + 顶层重名响亮 assert，读不到任一文件仍回空集、空集≠不存在语义不变；拆分前后推导入集合逐层全等 = main/run_bridge/_run_bridge_locked/_record_bridge_refusal 同 4 层，status 路径本就只读盘、从来不在落盘入口集）；消费面基线全等 = 干净 HEAD（`632efdf`）worktree 19 模块 754/754 全绿 ≡ 拆分+重排后 754/754、逐模块零差异（旧 before 文件里 `test_platform_pairing` 1 红经 HEAD worktree 定基证实为瞬时污染、非真差异，已被干净基线取代）；反向证明 3/3（变异打行为·早退/返回值，⛔ 非常量文本）：M1 `_record_bridge_refusal` 早退 ⇒ test_bridge_refusal_probe 14F+2E 真红 / M2 `run_status` 早退 ⇒ test_status_credential_failures_section 7E 真红 / M3 `run_setup` 早退 ⇒ test_cli_config_view 2E 真红，各自还原 SHA256 对基线；telegram 定向 157/157 + completeness 6/6 + compileall 干净 + `bridge_dir_isolation_scan` exit=0；⛔ 未跑全量 ×3；⭐ **第六刀已落地（`734c0b1`，2026-10-13）**：`opencode_client.py` 文件刀——裁定路②「重排测试 patch 目标」（用户两度「继续执行」未选边 ⇒ 编排者按主线延续裁定走②：较轻、直接延续 §5 文件刀、不排斥日后①；纯仓库内结构调整、§1 GitHub 搜不适用）· **类留守形态**（与类搬出对比择小搬动面：搬走 131 行 vs 类搬出 374 行；`OpenCodeClient` 类体 341 行原样不动 = 120+ 条消费测试直接压着它，且 `:420/421` 的 `_RECONNECT_MIN/_MAX` patch 随消费者留守零改动）——搬走两个真正独立职责域：endpoint 发现域（`DEFAULT_SERVICE_URL`/`_service_file_candidates`/`_read_service_file`/`discover_endpoint`）逐字移植 `opencode_endpoint_discovery.py`（103 物理行；logger 同通道名 `opencode_bridge.opencode_client`，日志逐字节不变）+ wire 契约域（`OpenCodeError`/`Endpoint`/`_safe_read_body`/`_force_close_stream`，零日志调用）逐字移植 `opencode_wire.py`（78 物理行）；顶部再进口块保既有 import 路径（5 公开名全可从 `opencode_client` 解析 ⇒ 生产 7 模块 + 消费面其余 13 测试模块 import 原样）；测试重排恰 2 行：`test_opencode_client.py:184/194` patch 目标 `opencode_bridge.opencode_client._service_file_candidates` → `opencode_bridge.opencode_endpoint_discovery._service_file_candidates`（重指向消费者 `discover_endpoint` 所在模块；旧目标失效形态 = mock.patch 响亮 AttributeError、非静默）；依赖单向 `opencode_client → 两新模块`、`discovery → wire`、零回环（回环检查用 AST 口径——裸文本扫描被 docstring 通道名说明误命中，§7.1 先疑探针）；AST 同一性 4+4+7 节点逐节点 unparse 一致 + 自由名分析全覆盖 + 顶层零遗漏断言（搬走 8 + 留守 7 == 顶层名字全集，程序化取名）；拆分前基线消费面 14 模块 605/605 全等（第一轮跑出 2 红正是 `:184/194` patch 钉点的响亮 AttributeError，重排后回绿对基线）；反向证明 3/3（变异打行为·早退）：M1 `_read_service_file` 恒回 None ⇒ 3 真红（test_service_file_param / test_service_file_env_var / test_service_file_fills_only_missing）/ M2 `_service_file_candidates` 恒回 [] ⇒ 1 真红（test_service_file_env_var——**真路径**，与被 patch 的替身路径互补，证明重指向后真调用链仍咬合）/ M3 `Endpoint.auth_header` 恒回 {} ⇒ 3 真红（test_auth_header_is_basic_opencode / test_interrupt_sends_post_without_json_body / test_multi_frame_parse_ignores_heartbeat_and_junk），各自还原 SHA256 对基线；telegram 定向 157/157 + completeness 6/6 + compileall 干净 + `bridge_dir_isolation_scan` exit=0；⛔ 未跑全量 ×3。⚠️ 残留 `opencode_client.py` 390 物理行**退线但贴线**（余量 10，如实登记）——剩余体量即 `OpenCodeClient` 类自身（23 方法/SLOC 267，见类体量债务表）= §5.1 类手术区，**待用户裁定**；⭐ **第五刀已落地（`9615c7b`）**：`a2a.py` 文件刀（刀路依据 = 缝质量与零钉点、非体量，`exp-68` 否掉按 SLOC 拆最大）——a2a 零测试 patch 钉点（test_a2a.py 零 mock，回环起真 HTTP 服务）+ 领域缝最干净（协议常量段 L130-424 连续 + 配置闸尾段 L1545-1664 连续）⇒ 协议常量 + wire 纯函数域 52 名字（`PROTOCOL_VERSION`/状态表/错误码/路径/限额/`UNCONFIGURED_PORT` 等 + `_default_agent_name`..`extract_context_id`，含 lazy `import socket` 随块走）+ 配置闸域 5 名字（`_coerce_port`/`_coerce_positive`/`_coerce_turns`/`_parse_peer_tokens`/`_local_of`）逐字移植 2 个新模块（`a2a_protocol.py` 316 / `a2a_config_gates.py` 147 物理行，均 <400）+ `a2a.py` 顶部再进口块保既有 import 路径与 `a2a.X` 属性语义；`_METHOD_HANDLERS`/`_UNSUPPORTED_METHODS` 刻意留守（方法表 dispatch 域，与类耦合）；logger 同通道名 `opencode_bridge.adapters.a2a`（ws 刀先例，日志逐字节不变）；依赖单向 a2a → 两新模块、config_gates → protocol（只取 5 常量），零回环；残留 `a2a.py` 1273 物理行**不退线**——`A2aAdapter` 类体（34 方法/SLOC 603，见类体量债务表）= §5.1 类手术区，**待用户裁定**（与 `opencode_client.py` 拆分同型）；拆分脚本 AST 名字集硬断言 52+5（程序化取名含 AnnAssign——探针已知缺陷：顶层扫描漏 AnnAssign）+ 自由名分析证实新模块 imports 恰好覆盖（块2 确需 logger）；AST 同一性 57 搬逐节点 unparse 一致 + 留守名字集不变；消费面 6 模块 254/254 与拆分前基线全等 + telegram 定向 157/157 + completeness 6/6 绿；compileall 干净、`bridge_dir_isolation_scan` exit=0；反向证明 3/3（M1 `_coerce_port` 恒回 `UNCONFIGURED_PORT` ⇒ test_config_coerce + test_config_runnable_verdict 18 真红 / M2 `extract_text` 恒回空串 ⇒ test_a2a 27+1 真红 / M3 `_local_of` 恒回空串 ⇒ test_a2a 15+2 真红——变异打行为（早退改返回值）非常量文本（第四刀教训），各自还原 SHA256 对基线）；⛔ 未跑全量 ×3；⭐ **第四刀已落地（`facdaa9`）**：`__main__.py` 专轮（装配 vs 视图渲染轴向，`exp-68` 裁定）——配置就绪判定域 9 名字（`_token_present`/`_readiness_verdict`/`_missing_required_keys`/`_not_ready_reasons`/`_status_platform_keys`/`_config_file_in_use`/`_has_configured_adapter` + 常量 `_NOT_READY_MISSING_CREDENTIALS`/`_NOT_READY_NO_ALLOWLIST`）+ 宽度底座 `_dwidth`/`_pad`（被三个渲染器与留守 `run_status` 共用 ⇒ 独立成 `text_width.py`，避免视图模块间横向依赖）+ 启动探测视图（`NO_START_PROBE_TEXT`/`_print_last_start_probes`）+ 运行期失败视图（出站/凭据两段各含无记录文案与标题常量 + 渲染函数）逐字移植 4 个新模块（`config_readiness.py` 203 / `text_width.py` 27 / `startup_probe_view.py` 158 / `status_failure_view.py` 272 物理行，均 <400）+ `__main__` 顶部再进口块 21 行（19 名字）保 import 路径与 patch 语义；残留 `__main__` 985 物理行**不退线**（patch 钉点 `_bridge_dir`×11/`discover_endpoint`×5/`BridgeCore`/`InstanceLock`/`OpenCodeClient`/`StateStore`/`ProcessDiagnostics`/`build`/`threading` 的运行期调用者全部留守 + `_print_event_subscription` shim 按裁定留守），残留与钉点已登记、留待后续轮；搬走名字零测试 patch/setattr（探针实测 0 命中）、搬走函数体内 3 处 lazy import（`adapters`×2/`unicodedata`）逐字保留；AST 同一性 19 搬 + 26 留逐节点 unparse 一致；消费面 11 模块 521/521 与拆分前基线全等 + telegram 定向 157/157 + completeness 6/6 绿；compileall 干净、`bridge_dir_isolation_scan` exit=0（真实跑对上预检 A==B 模拟）；反向证明 3/3（M1 `_readiness_verdict` 恒 True ⇒ test_config_runnable_verdict 43 真红 / M2 `_print_last_outbound_failures` 早退 ⇒ test_outbound_failure_channel 22 真红 / M3 `_print_last_start_probes` 早退 ⇒ test_platform_health + test_bridge_refusal_probe 16 真红，各自还原 SHA256 对基线）；⚠️ 反向证明判据坑：第一版 M2 变异常量**文本**被「两侧同源」断言免疫（`SECTION_HEADER = cli._OUTBOUND_SECTION_HEADER` 再 `assertIn(SECTION_HEADER, body)`——两侧同取一处，改值恒绿；它量的是「渲染器用了这个常量」，不是常量的值）⇒ 变异必须打**渲染行为**（早退）才真红；⛔ 未跑全量 ×3；⭐ **第三刀已落地（`002abfb`）**：`ws.py` 协议原语域（常量块 `_GUID`/`_OP_*`/`_MAX_*` + `WebSocketError` + URL 解析段 `_Target`/`_parse_ws_url`/`_accept_for`/`_tokens` + 底层收发段 `_mask`/`_recv_exact`/`_read_http_response`/`_parse_response_head`/`_hard_close` + `_decode_text`，共 20 个名字）逐字移植 `ws_protocol.py`（201 物理行）+ 顶部 4 行再进口保 import 路径，残留 391 物理行退出名单；⚠️ 消费面零 `mock.patch` 钉点（生产侧 6 适配器仅延迟 import `connect`，留守 ws.py；test_ws 直取私有 `_parse_ws_url` 经再进口可解析）；logger 通道仍 `opencode_bridge.ws`（类体日志全留守，零行为变化）；单向依赖 ws → ws_protocol（新模块零运行期回 import）；AST 同一性 11 搬 + 2 留 + 9 常量逐节点 unparse 一致 + docstring 前缀 + 名字集 24=4+20 + 引用面 26 名字可解析；反向证明 M1（`_parse_ws_url` 默认端口 +1 ⇒ TestUrlParsing 2 用例真红）/M2（`_mask` 无异或 ⇒ 掩码往返用例真红）、SHA256 对基线；消费面 7 模块 455/455 与拆分前基线全等 + telegram 定向 157/157；⚠️ **第四刀前置裁定：`opencode_client.py`（526）暂不可拆**——mock.patch 钉点两处：`test_opencode_client.py:184/194` 钉 `_service_file_candidates` + 唯一调用者 `discover_endpoint`；`:420/421` 钉 `_RECONNECT_MIN/_MAX` + 唯一使用者 `subscribe`（类方法 ⇒ 类整体留守）⇒ 79+341+固定开销 ≈ 450 物理行下限、可搬仅 ~60 ⇒ 残留最低 ~453 > 400，现行三条纪律下无法退线；类搬出方向则 patch 静默失效（`_backup_file` 同型）⇒ 下一步 = `__main__.py` 专轮先行，`opencode_client.py` 留待「重排测试 patch 目标」或「§5.1 类手术」获裁定后另开一轮（⚠️ 此裁定已于 2026-10-13 第六刀 `734c0b1` 走完：「重排测试 patch 目标」路在用户「继续执行」指示下作为主线延续执行，`opencode_client.py` 390 退线，见 ⭐第六刀段）；上次实测值 = 2026-10-11 于 `d9554a6`：超线 28 / 豁免 9 / 待拆 19；⭐ **第二刀已落地（`d9554a6`）**：`state.py` 键迁移域（A2b：docstring 设计段 + 常量 + `MigrationReport` + `_classify_key` + `_resolve_collisions` + `migrate_document` + `_legacy_alias`）逐字移植 `state_migration.py`（314 物理行）+ 顶部再出口保 import 路径，残留 288 物理行退出名单；⚠️ `_backup_file` 刻意留下（`test_state.py` 钉 `mock.patch("opencode_bridge.state.shutil.copy2")`，搬走则 patch 静默失效）；AST 同一性 10+5、反向证明 M1/M2 真红（M2 = 21 处失败）、SHA256 对基线；⚠️ 前置桩修复 `03a8b5f`：G2 落地后 `_RecordingCore` 缺 `held_buffer_store` ⇒ `test_conversation_id_cutover` 9 用例 4 error 既有损伤（量于干净树 `3513a37`），先修后拆换回绿基线；⭐ **首个拆分已落地（`41bd2ce`）**：`status.py` 渲染段 8 个顶层定义逐字移植 `status_rendering.py`（93 物理行）+ 顶部再出口保 import 路径，残留 371 物理行退出名单；AST 同一性 8+13、反向证明 M1/M2 真红、SHA256 对基线；⚠️ 豁免 7→9（2026-10-11 重算）= +`transport/base.py` +`credential_health.py`（物理行长过 400 后入豁免侧），**与口径无关**（口径表未动）；⚠️ 重算脚本曾有两处「小数占比与整数 45 比较」同型 bug ⇒ 豁免侧恒空/条件②恒 0，已自审修掉；
文档里的「平台数」与「文档-代码交叉核对无对口护栏」⇒ 必填件已补齐；
（**`matrix.user_id` 缺失**、**`email.password` 缺失**、**telegram `getMe` 瞬时失败**
三条已于 2026-10-06 **落地**：前两条各补了一条点名该键的告警（行为零变化），
第三条把探测从终局闸门改成会话建立的一部分 + 退避重试 ⇒ **已从本清单移出**）
（**2026-10-07 已从本清单移出一条** —— `matrix.user_id` 缺失要不要改成「失败关闭」
（用户 2026-10-07 拍板 · `fix-301` 已落地）：**改成失败关闭**，
**失败关闭落在入站层**、⛔ **刻意不加第三道 `start()` 闸门**（那 = 降级，台账已否）——
⭐ 而**根因不是「收不到消息」，是回声过滤整个短路**：`user_id == ""` 时那个条件
**左侧短路** ⇒ 回声判定**永不被求值** ⇒ **无限自问自答**；而 `user_id` 又在
`required_tokens` 里 ⇒ `--status` 报「配好了」⇒ **用户看不到任何解释**。
⚠️ **知情代价**：没填 `user_id` 的 matrix 用户会**一个消息也收不到**。
⇒ ⛔ **未跑全量 ×3**（用户要求不占 CPU）。⚠️ 而原写的「**证据缺口**」
（「上游是否真会把自己的消息以事件形式发回来」我未核实）**仍然未核实** ——
拍板已定处置，那条事实仍未知）
~~telegram `dropped callback from non-whitelisted chat` **零钉**~~ ⇒ **已闭合** ✅（2026-10-11 · `0e3794c`，零生产改动）——「覆盖面缺口、非行为缺陷」的定性不变：守门 = tests/test_telegram_callback_drop_wording.py 渲染层逐字 + 白名单对照臂 + 源码锚点 + callback/message 两句措辞互钉，反向证明 2/2、SHA256 对基线，证据见总表 ~~…~~ 闭合行；
~~sms 平台文档缺位~~ ⇒ **已闭合** ✅（2026-10-11 · `e97bdd7`，零生产改动）——**登记即闭合**（A3 五件套从未含 docs ⇒ sms 落地后 README/plugin/README/install 文档面零覆盖），连同 plugin/README「14 个断言」过期（harness.ts 自述 25 个场景）与「a2a 是唯一方向相反」被 sms 入站 webhook 事实性打破一并修复（14 处替换各恰命中一次；「13 个」按 2626 钦定形状改为指向清单/`--status`，是执行拍板 ⛔ 非翻案）；定向 telegram 157/157 绿、⛔ 未跑全量 ×3，证据见总表 ~~sms 平台文档缺位~~ 闭合行；
~~**`--status` 出站失败段的同型缺陷**~~ ⇒ **已闭合** ✅（2026-10-06 · `77d439a`，2026-10-13 核对和解）——修法与已修那条（`_print_last_start_probes`）**同构**：`listed` 并入未注册键（label 取键本身）+「盘上有记录，但没有属于这些平台的」判据改问 `recorded_platform_keys`（记录自己说了什么，⛔ 不问混进「配没配」那层的 `listed`），第四刀随视图原样移植进 `status_failure_view.py`；守门 = tests/test_outbound_failure_channel.py `UnregisteredKeyStaysVisible`（判据自检：那个键必须真的未注册，否则整组恒真）；⚠️ 本节此前一直把它当开放项列着——修复与登记同落 `77d439a`，tasks.md 总表当日已 ~~划线~~ 闭合，而 2026-10-11 重算清单误按开放项抄入，今日逐字核对代码（`listed` 并入段 + `recorded_platform_keys` 判据俱在）与守门后和解，证据见总表 ~~…~~ 闭合行；
**`inbound_gateway` 的四条入站投递缺陷**（`ora-14` 复查，⚠️ 全部落在**入站路径**）：
**传输层失败被记成 `failed` ⇒ 下次启动重放 ⇒ agent 对同一条指令跑两遍**
（⚠️ 它**推翻了本仓库一直写的「at-most-once」** —— 2026-10-07 用户已拍板
**分出第三种状态「结果未知」，恢复层对它不重放**；⛔ **明确否掉「重试时用别的手段去重」**
（那不解决不确定性，只把它换成**确定性错误**）· ⚠️ **代价必须知情**：不重放 ⇒ 远端**真**没
收到时那条指令**丢了** ⇒ ⇒ 那条告警**必须同时写「请重新发送一次」**，
⛔ 只记一条「结果未知」等于**把负担转给用户却不告诉他该做什么**
（而收件箱存在的理由正是「别丢这条」）· ✅ **`fix-299` 已落地（2026-10-07）**：
　**「只告警不重放」现在覆盖两处** —— 崩在 `prompt()` 内部（`attempting`）与
　**传输层失败、拿不到 status**（`outcome_unknown`）⇒ 两者**只告警、绝不重放**；
　**仍然不覆盖有 status 的明确失败**（那是 `failed`，按退避阶梯重放）。
　⇒ ⭐ **判据**：`recover_pending` 的重放集合 = {`pending`, 到期的 `failed`}，
　　`outcome_unknown` **必须 0 命中** ⇒ 由一条**行为型覆盖面守门**钉住
　　（把 `DeliveryState` 全集逐档**真摆到盘上**跑一轮真恢复，逐集合比、不比顺序）
　　⇒ ⛔ 穷举表那条守门是**既有**的（`test_every_delivery_state_is_either_settled_or_unsettled`）。
　⇒ ⚠️ **知情代价**：`outcome_unknown` 行会**永久留在盘上**、每次启动再告警一次
　　（与 `attempting` 逐字同构）—— 那是「不重放」的直接后果，⛔ 不是缺陷。
　⇒ ⚠️ 而「请重新发送一次」有个**既有天花板**：平台**不给 `message_id`** 时去重键退回
　　`sha256(platform|conversation_id|text)` ⇒ **逐字节相同**的重发会被当去重挡掉
　　（给 `message_id` 的 telegram 等不受影响）⇒ ⛔ 未改：改去重键会削弱 at-most-once 的另一半。
　· **`_enqueue` 的早退与 `flush_queue` 是同型的丢唤醒面 —— 当时恰好无害** ⇒ **第 3 类**
（低 · 认领人=编排者 · 2026-10-08 起；⚠️ **登记于 2026-10-07**，由 `fix-301` 顺带量到，
　不在那一轮范围内 ⇒ 单立一条）：`inbound_gateway.py` 的早退（注释写
　「current drainer will pick this up」）与 `flush_queue` 是**同型**的面：
　**都假设「当前排空者会顺手取走」**。
　⇒ **为什么它恰好无害**：它 `append` 的消息**本来就在队列里**，而排空者的 `while True`
　　**一定会再看到它**；⇒ 而上面那条是 `insert(0, …)` **退回队首 + 交出排空权**
　　⇒ 之后**无人会再看它**。
　⇒ **必填件（代价是什么、为什么可接受）**：代价 = **无当前代价**（队列语义保证它会被看到）；
　　为什么可接受 = 它与已修那条的差别是「**消息还在队列里**」vs「**消息被退回后无人再看**」
　　⇒ 而前者由 `_drain` 的 `while True` 保证。
　⇒ ⛔ **刻意不改**：改它要动 `_enqueue` 的既有语义（「不并发」），而它**当前无害**。
　⇒ ⚠️ **而这条会过期**：若将来给 `_enqueue` 加「**早退时不排队**」或「**提前交权**」的改动，
　　⇒ 「排空者一定会再看到它」这个前提**不再成立**
　　⇒ ⇒ **判据（可复核）**：改 `_enqueue` 时**必须重验这一条**。
· **`_drain` 的异常分支让队列剩余消息滞留 RAM（`_drain` 正在抛异常时把队列丢在 RAM 里）**
　⇒ **第 2 类「根因未证实」**（中 · 认领人=编排者 · 2026-10-08 起；
　⚠️ **登记于 2026-10-07**，来自 `flush_queue` 丢唤醒闭合后的那轮**同族面普查**）：
　`inbound_gateway.py` 的 `except` 分支 `discard(_draining)` 之后直接 `return`、
　**不做 `continue`** ⇒ 正在抛异常的那个排空者把队列里剩下的消息丢在 RAM 里。
　⇒ ⭐ **与上面那条 `_enqueue` 的本质区别（已实测，不是读代码）**：`_enqueue` 早退时
　　**排空者还活着**、`while True` **一定会再看到**；⇒ 而这里**排空者正在死**。
　⇒ **实测（两组只差一个变量的臂，`threading.Event` 同步点、⛔ 不用 `time.sleep`）**：
　　注入 `mark_attempting` 抛 ⇒ 排空者死后队列**残留**；对照组（**卡在同一个同步点**、
　　不抛）⇒ **零残留** ⇒ **判据有辨别力**。
　⇒ ⭐ **代价（已量清）**：等读者下一条消息或下次进程启动 —— **不是永久丢失**
　　（残留在盘上是 `pending`，`recover_pending` 对它**直接重放**）；
　　⇒ 而被 `pop` 掉的那一条**有告知**，**剩下的几条一条告知都没有**。
　⇒ ⚠️ **待查证据**：生产 sqlite3 写失败的频率；⛔ **现在没有**
　　（需真实运行环境 / 线上日志；本机无外网凭据，**无法构造 sqlite3 写失败**）
　　⇒ 而这正是它归**第 2 类**而不是第 3 类的理由：代价虽已量清，
　　　但「**为什么可接受**」那一栏**填不出来** —— 正因为不知道它到底会不会发生
　　　⇒ ⛔ **不写「代价已知且刻意不做」**，那等于替一个**未证实的触发条件**背书。
　⇒ ⚠️ **两条误差点值得留一句**（「注入点要对」的证据）：① 在 `prompt()` 里抛**测不到**
　　（`_dispatch_prompt` **自己**就 `except` 了非 `OpenCodeError`，异常**根本没到**
　　`_drain` 的 `except` ⇒ 我一度据此判「不成立」）；② 对照组因同步点永不置位
　　**根本没跑到窗口内那一档**。⇒ 注入点必须**先读生产侧那条路径**逐行确认，⛔ 不许猜。
· **`_flush_requested` 在 `_drain` 的另两条退出路不清标记 ⇒ 残留标记让下一次 409
白白多试一次** ⇒ **第 3 类「代价已知且刻意不做」**（低 · 认领人=编排者 · 2026-10-08 起；
　⚠️ **登记于 2026-10-07**，同一轮同族面普查）：`_drain` 有**三条**退出路，
　只有 `busy` 那条会 `discard(_flush_requested)`；⇒ **空队列**与 **`except`** 那两条
　都**只 discard `_draining`、不清标记** ⇒ 若 flush 落在 `prompt()` **在途窗口**而
　那条 dispatch **成功**了 ⇒ **标记留下、无人兑现** ⇒ 下一次 409 替上一次白白兑现。
　⇒ **实测（三臂各只差「标记在不在」）**：保留标记 ⇒ 试 **4** 次；先清标记 ⇒ **3** 次；
　　不注入 flush 的对照组 ⇒ **3** 次 ⇒ **辨别力成立**（多出来的那次**由标记引起**）。
　⇒ **必填件（代价是什么、为什么可接受）**：代价 = 对一个已经 busy 的服务端
　　**多发一次请求**；⚠️ **方向与丢唤醒相反**（不是丢，是多试一次）⇒ 用户侧无可见症状。
　　**⭐ 上界 = 1**（第一次 409 就 discard 了）—— 这一句**必须写清**，
　　否则「多试一次」读起来像「可能无限重试」，而热循环正是反向证明 M3 盯的那个。
　　为什么可接受 = 上界有界、用户无可见症状、**不丢消息**（队列已 `insert(0, …)` 退回队首）。
　⇒ ⛔ **显式无计划**：⛔ **刻意不做** —— 改它要在另两条退出路各加一次 `discard`，
　　而收益只是省掉一次注定 409 的请求 ⇒ 收益小于改动面。
　⇒ ⚠️ **而这条会过期**：若将来 flush 的语义变成「必须兑现」（例如多级唤醒），
　　则残留标记就不再只是多试一次 ⇒ ⇒ **改 flush 语义时必须重验这一条**。
　· **`flush_queue` 丢唤醒 ⇒ 消息滞留 RAM
且零反馈**（自愈路径会**改变送达顺序**）· **长输入回执发在去重判定【之前】⇒ 平台重投时
给假回执然后永久沉默**（⛔ 不支持编辑的适配器受影响，telegram 不受影响）；
（**2026-10-07 已从本清单移出一条**：`merge_continue_timeout_seconds: 0` 被静默改成 15
—— `_positive_float` 判 `> 0` 而同族 `_positive_int` 判 `>= 0` ⇒ **漏改**，
`fix-292` 已让代码回到它**自己 docstring 写下的契约**。
⚠️ 而**本次修复带出一个新事实**：配 `0` 会把 G2 的暴露窗口从「≤ 15 秒、**有界**」
换成「**无界**」（`_held` 是纯内存、`flush`/`stop`/`held_conversation_ids` **零生产调用点**）⇒ **`0` 不等于更安全** —— 已作为独立一行登记，⛔ 文案层面未拍板）
　（**2026-10-10 更新（G2 已修）**：上句里「`_held` 是纯内存」这一**事实**已过期 ——
　缓冲每次变异都整份快照进 `held-buffer.json`、启动重灌找回
　（`held_buffer_store.py` + `InboundGateway._recover_held_buffer`），**不依赖计时器**，
　崩溃/退出不再丢缓冲；而「`0` 不等于更安全」的**结论仍成立**、只是换了依据的
　那一半：**等待仍无界** —— 可重灌回去的那行同样在等一条可能永远不来的下一行。
　WARNING 文案已随 G2 重写并钉在 tests/test_inbound_gateway.py
　（`test_the_warning_names_the_unbounded_wait_and_the_snapshot_net`））
· **提交消息的替换字符（U+FFFD）：向前的闸门已装，历史已清 8 条** ⇒ **第 1 类**
（认领人=编排者 · 2026-10-08 起）。⚠️ **2026-10-07 全历史扫描出 9 条 / 28 个**
　（⛔ 而我上一轮只报了「2 条 / 5 个」⇒ **汇报的前提本身是错的**，已更正）：
　⇒ **已改 8 条**，依据分三类：**`[证据]`** 同一句在 `tasks.md`/`AGENTS.md` 里逐字可查
　　（改因不治症 · 交互式配置的原文输出 · ≤ 15 秒、有界 · 优先级格都合格）
　⚠️ **而这一类里我自己猜错过一处**：猜「**原始**输出」，仓库落地的是「**原文**输出」
　　⇒ ⇒ **回仓库恢复原文，不靠语感**；
　**`[自述]`** 该 commit 正文自己写明了要改成什么（`2b1a666` 那两处 → `` `U+FFFD` ``）；
　**`[裁决]`** 仓库恢复不出来 ⇒ ⛔ 不猜 ⇒ **已问用户**（新增的第 4 步 · 要改 `a2a` ·
　　与原条件翻转前 · 从审查里抄来的）⇒ ⛔ **裁决不许冒充成证据**。
　⇒ ⛔ **刻意保留 1 处**：`同▓▓对端` 是**它当时在引用源码里的真实乱码**
　　（`a2a.py` 现含 U+FFFD = **0**）⇒ 改了就与当时的事实不符。
　⇒ **核对**：tree 逐字相同（内容零变化）· 提交数 226/226 · `--force-with-lease`。
　⇒ ⭐ **根因已修在闸门上**：旧闸门把消息路径**写死**、且**与提交分成两条命令**
　　⇒ 「闸门作用在错误的文件上」**没有任何机制会告诉我**。
　⇒ **新闸门三条**：① 路径来自 `sys.argv`，⛔ **不给默认路径**；
　　② **收尾与校验同一个函数**，⛔ 不分两条命令；③ **打印含它正在处理哪个文件**。
· **2026-10-07 已从本清单移出一条** —— `merge_continue_timeout_seconds` 配 `0` 的文档措辞（用户 2026-10-07 拍板，`fix-298` 已落地）：
　措辞 = 「**配 `0` = 立即返回、原样使用、不改写**」；配 `0` 时有一条**点名该键**的
　WARNING，**好处与代价写在同一条**（⛔ 只写好处会被读反）。
　⚠️ **而那处真正的用户可见落点是【缺一行】不是【措辞错】**：`README.md` §6 配置表列了
　4 个 `bridge.*` 键里的 2 个，`merge_continue_timeout_seconds` **从未被列过**。
　⚠️ **而 `config.py` 的旧措辞「那条消息会丢」是【事实错误】** —— 缓冲不会丢掉它，它一直
　留着等下一条非 `..` 行一起发 ⇒ ⇒ **别把错的文案当「已正确的文档」的基准**。
　⇒ ⭐⭐ **两条可复用的判据（都是我派单时写错、由实现者纠正的）—— 它们比措辞更值得留**：
　**① 「键在不在」是恒真的** —— `Config._merge_bridge` **总会**把 `_BRIDGE_DEFAULTS`
　　补进去 ⇒ 生产上那个键**永远在场**（没配时其值 `15.0`）⇒ 它**无法**区分
　　「显式给了 0」与「走默认值」⇒ 唯一成立的是「**算出来的值 == 0.0**」
　　（⛔ 不需要改签名）⇒ ⚠️ **前提是默认值不为 0**，否则每次启动一条噪音告警。
　**② WARNING 放在【唯一知道键名的调用点】，⛔ 不放进通用的 `_positive_float`** ——
　　把键名写死进那个 helper，**第二个调用点一出现**就会**点名一个与实际无关的键**（谎报）
　　⇒ **代价**：新增调用点时**必须自己再发一条点名自己那个键**，⛔ 忘了就静默漏掉。
　⇒ ⭐ 而 ① 与 ② 是**同一条纪律的两个面**：告警里的**键名**与它的**触发条件**，
　　都只能由**真正知道那件事的那个地方**给出。
　⇒ ⛔ **两个未裁决点**（留给编排者/用户）：`inbound_merge.py:256` 那句内部分支注释
　　是否同步 · `core.py` 那个**同名** `_positive_float`（它也接受 0，而那里 0 =「不节流」，
　　**两处语义确实不同** ⇒ ⛔ 只改一处会把两处语义拆开）。
（**2026-10-07 已从本清单移出一条**：`InboundInbox.record` 把「连接已关」报成「去重命中」
—— `fix-295` 已闭合：`RecordOutcome.CLOSED` 有了**自己的分支**、说真话
（点名 delivery_id +「永远不会重放、因此没有投递」），而**两句 `duplicate ignored`
逐字未动**、**投递行为零变化**（`CLOSED` 仍是假值）。
⚠️ **目标是「让日志说实话」不是「把行为改掉」** ⛔ 没有改成「连接关了就仍然投递」。
⚠️ 而**返回值契约一个字节没动**，靠一条 AST 普查守住：生产侧调用点**恰好 1 处**，
而**3 个测试替身的 `record()` 返回 `bool`** ⇒ 投递那一路**不能**改成身份判定。
⇒ **级别归属（编排者裁定）**：调用方那句**保持 `INFO`** —— 收件箱那档**每拒一条都
warning** 且掌握「不可重放」，而关停期间每个活着的适配器线程都会走到调用方
⇒ 再 warning 一次只是把同一次丢失数两遍。）
**另三项此前【散文里有、总表外】、2026-10-06 已补进总表**（§4.1：「要么写下一步、要么显式声明无计划 + 为什么」，而它们只有「记着」）：**`__main__.py` 该拆**（装配 vs 视图渲染，`exp-68` 裁定 ✅）⇒ **第 1 类**，⚠️ 而「按 SLOC 排序拆最大的」这条做法已被 `exp-68` **否掉**、替代顺序未给；~~**A3 inbound-push 的真实剩余工作**~~ ⇒ **已闭合** ✅（2026-10-11 · 基建 `b717137` + sms 适配器本体 `b4b5eaa`）——拍板史与证据留在总表 ~~A3~~ 行：拓扑（单端口共享 server）与第二个 adapter（sms/Twilio）均用户 2026-10-10 拍板、可达性暂不放宽（回环，有隧道/公网再议）；适配器本体 = `adapters/sms.py`（`X-Twilio-Signature` 唯一信任边界 + 端口双变体验签 + 回声防护 + 闸门纪律 + REST 出站 + `running` 显式覆写），守门 = tests/test_sms.py 31 用例 + 反向证明 3/3（M1 验签恒过 / M2 端口变体恒 None / M3 删回声防护，各让目标用例真红，还原后 SHA256 对基线）；⛔ 未跑全量 ×3，定向 596/596 绿；**测试侧 §5 分布测量** ⇒ ⚠️ **循环依赖**：「单职责」的分类标准本身就是待标定的东西，按机械判据量会把「一个文件多个职责」那类**排除掉**，而那正是要抓的 ⇒ **先定判据再量**；
**事件消费路径：四条已审完，三条已修、`/new` 仍未修**（`ora-15` 复查，⚠️ 至此 telegram /
`health.py` / `__main__.py` / 13 个适配器的配置面 / 入站投递 / 事件消费 ——
**本仓库的功能面已全部审过一遍**）：
（**2026-10-07 已从本清单移出两条** —— 决策与代价留在总表那些 `~~…~~` 行里。）
⚠️ **a2a 把「已取消上一条请求。」记成 `TASK_STATE_COMPLETED`**（`fix-293` 已修）⇒ 给取消一条独立 kind + 显式映射到 `TASK_STATE_CANCELED`；⚠️ **顺带修掉同类的第二个**：「未知 kind → COMPLETED」那条兜底（此前**任何没被显式列出的 kind 都被报成「完成」**）⇒ 改成查不到就**WARNING + `bad_format` 落盘 + 不判终态**。⇒ **⚠️ 代价（必须知情）**：那个 task **留在非终态**、等 `reply_timeout`（默认 300s）由超时那一支判 `FAILED` ⇒ 从「立刻假成功」换成「响亮地失败 + 占住一个 handler 线程」。⚠️ **而这条行为变更由【编排者】裁定、用户没参与**（代码注释原写「用户拍板」⇒ 已改）。⇒ 敢接受它的前提是**穷举 + 守门**：`Outbound.kind` 的穷举集合已被量出，且有一条 **AST 覆盖面守门**断言「生产里写出的每个 kind 字面量都在表里」
⚠️ **`fix-293` 顺手改了 `hooks.py`（我派单里没有这个文件）—— 已结案**：逐行核过是**恰好一行注释**，而它是全仓库唯一枚举 `Outbound.kind` 取值的地方 ⇒ **按内容判定它在范围内**；⚠️ 而这条判据（「内容不是文件名」）是我自己写的 ⇒ 留在这里是为了让读的人看到「它是被判过的，不是被忽略的」
· **占位消息正在飞行中的窄窗 ⇒ `/new` 弹掉 turn 时留下孤儿气泡** ⇒ **第 1 类**（窗口 = 一次 HTTP send；⚠️ **改动前就存在，不是回归**）
· **2026-10-07 已从本清单移出一条** —— **`ReconnectBackoff` 的夹逼把 `0` 判为合法**（用户拍板 + `fix-301` 已落地）：
　⇒ **共用常量** `transport.base.MIN_BACKOFF_SECONDS = 0.01`，`min_backoff` 与 `first_delay` **钳到它**（读代码判定二者是**同一个下界**）
　　⇒ ⛔ 不各写一份数字（各写一个正是它们会悄悄分叉的原因）。
　⇒ **做法 = 钳位 + 点名旋钮的 WARNING**（⛔ 不选 `ValueError`：它会打死适配器构造，
　　而那个 `except` 在 `BridgeCore.start()` 里 ⇒ 用户只看到「failed to build adapter」
　　⇒ **比静默降级更坏**，§8 第 4 条）。
　⇒ ⛔ **`reset_after=0` 与 `tick_interval<=0` 一字未动** —— 它们的 `0` **各有文档化含义**。
　⇒ ⚠️ **而本条最该留的是【编排者给的那条约束被实测推翻】**：我写「⛔ 不要拍一个比
　　生产默认值还小的下限」⇒ 而仓库里**有真实调用在传 `0.01`** ⇒ **我的前提是错的**
　　⇒ 正确判据是「**只有真的有人在用的值能通过**」。
　⇒ ⚠️ **口径更正**：我台账记的「13 处」是**正则**口径，**AST 精确口径 = 10 处**
　　（另 3 行是 `0.005`/`0.01`）⇒ 而那 10 处**不依赖 `0`** ⇒ **一条都没改**。
　⇒ **生产行为零变化**（九个适配器接线未变 · 新告警 **0 条** · SSE 首档仍 `0.5`）。
　⇒ ⭐⭐ **而它抓出一组恒真断言，那条最该留给下一个人**：把下限改成 `0.01 + 1e-9` 时
　　**其余用例全绿** ⇒ 因为它们都拿**导入的常量**当期望值 ⇒ **一整组恒真**。
　　⇒ ⭐ **判据**：断言「钳到了某个常量」时，**必须再有一条断言那个常量本身的值**。
　　⇒ 同族：⛔ 断言「它是不是正数」**恒真**（默认值本身是 `1.0`）
　　　⇒ **必须断言数列的第一项**，那才测到钳位。
· ~~**`ReconnectNow` 的零等待路径没有重复次数上界**~~ ⇒ **已闭合** ✅
　（2026-10-07 · 用户拍板 · `fix-301`）⇒ **上界 = 5.0 s，对齐 Discord 官方**
　「concurrent Identify requests allowed per 5 seconds / 撞到就回 op 9」
　⇒ 实现对齐 discord.js 的**每 5 秒一个额度滑动窗口**，⛔ 不是「每次 sleep(5)」；
　⛔ **不做成可配旋钮**（证据只支持这一个具体值）。
　⇒ 三个决定：① 计时起点 = 上一次**零等待**重连（⛔ 不是「任何重连」）
　　② **第一次不受限** ③ **无抖动**（⛔ 不照抄 `random()*1500`）。
　⇒ ⚠️ **知情代价**：正好停在 5.0 s 边界、无 discord.js 那 0~1.5 s 余量
　　⇒ ⛔ 未自行发明余量（那是另拍一个数）⇒ **留给用户裁定**。
　⇒ ⚠️ **既有判据冲突（已改）**：原文断言「5 次连接 3 s 内发生」，
　　⇒ **那条断言的正是缺陷本身** ⇒ 改为「第 2 次」，⭐ 辨别力不变（已复核）。
　⇒ ⭐ **反向证明抓到的自身缺陷**：第一版把时刻记成 `now`（应 `now + wait`）
　　⇒ 每 5 秒窗口 2 次 Identify、仍超额度 ⇒ 修正后超额度 **0**。
　⇒ ⛔ **未跑全量 ×3**（用户要求不占 CPU）。
· ⚠️ **一条关于【派单范围】的未完成事**：编排者在 `fix-293`（给取消通知一条独立 kind + a2a 映射）**在飞时**看到它顺手改了 `opencode_bridge/hooks.py`（2 行）—— 那个文件**不在**派单里。⇒ 按 §9「不在 lane 还在飞时裁决它的范围问题」，编排者**没有打断它**，而是**只提交别的文件** ⇒ **下一步 = 对账时逐个文件核**那 2 行改了什么、能不能不放在那个 commit 里；⚠️ 判据是**内容**不是文件名
⇒ 而 **`/new` 那条已于 2026-10-07 闭合**（`fix-293`，**修在根上** `SessionRegistry.drop_session`）：**用户拍板「占位消息改成已取消」**、⛔ **明确否掉**「发半截正文」⇒ 结构性保证（`cancel_turn` 拿不到 `turn.parts`）；⚠️ **修在根上的副作用**：`/cd <目录>` 走同一条路 ⇒ 也会说「已取消」（此前静默丢弃）⇒ **用户可见的行为变化，已知情**；
· ~~**telegram 重试的代价**（`platform-health.json` 的 `failed`
在进程余下整个生命周期不变 ⇒ **状态视图说坏了、实际在好好工作**）~~
⇒ **已闭合** ✅（2026-10-09 · 用户拍板①「维持现状」· 零代码）—— **第 4 类「代价已知且刻意不做」**：维持现状——`failed` 不改写、不给 `--status` 开新的运行期通道（候选②③均否），恢复的事实继续只由日志承担；知情代价：凭据恢复、入站自愈后 `--status` 仍显示那条 `failed`（历史事实、非实时状态）——排障以日志与 `credential-failures.json` 为准。⚠️ 而它与下面「`--status` 没接上」是**同一类问题的两个面**（那一行 2026-10-07 已闭合）；
（**2026-10-07 已闭合**：`--status` 那一行移出本清单 —— **视图 + 运行期通道两侧都落地**，
  ⚠️ 而「第三份平行文件 `subscription-health.json`」是**普查逼出来的**，
  三条理由编排者逐条验过：`platform-health.json` 整份替换（写前不读）会抹掉启动探测 ·
  `outbound-failures.json` 的 `platforms` 是**平台键**空间（塞非平台键会列出**假平台**）·
  `bridge-lifecycle.jsonl` **只在退出/崩溃时写**（「正在重连」进不去）。）
（**2026-10-07 已从本清单移出一条** —— 计时断言普查已审完（`fix-296`）。
  ⚠️⚠️ **而它推翻了我自己写在这里的数**：我写的是「8 处计时断言」，**AST 普查是 32** ⇒ **那个 8 是【下界】**；漏的是
  ① **跨行**写法（`assertLess(` 与 `time.monotonic()` 不在同一行）·
  ② **派生变量**（`elapsed = time.monotonic() - began` 后 `assertLess(elapsed, 2.0)`）⇒ **操作数里根本没有时钟字样**，任何行级正则都看不见。
  ⇒ 而这已是今晚**第三次**我的普查太窄 ⇒ **判据的宽度要单独受审**（§7.1 第 4 条）。
  ⇒ 判定：**A 21**（真在测墙钟，⛔ 按 §8 不许改假时钟）· **B 0** · **C 2**（已修，⛔ 都没有调带宽）· 另有 **9 条假阳性**（时钟只当**数据**用）。
  ⚠️ **而 A 类余量我原判「15 倍」是错的** —— 失败态上界不是退避档位（30 s）而是 `Transport.stop()` 的 `thread.join(timeout=5.0)` ⇒ **真实余量 2.5×**；
  它仍合法（实测现状 12 次全 `0.0000s`、退化后 `5.00~5.02s` ⇒ 两向分得开）。
  ⇒ ⚠️ **但 18 处时间戳数组 append 点是【已审但未逐条改】的** —— 判定它们「量的是什么」还没做 ⇒ 那是**另一条**，已单列。)
· **18 处时间戳数组 append 点：判定它们「量的是什么」** ⇒ **2026-10-07 已审完**
（`fix-296`，认领人=编排者）。**实测 17 处全部合法**、**零处代码改动**——
本条原写 18 处，差额是计时断言那一轮删掉的 `seen.append(...)` ⇒ **是过期，不是错**。
⇒ **判定**：真在测时序 **17** · 可换成同步点 **0** · 恒真 **0** · 假结论 **0**。
⇒ 而 17 处喂出来的判据**只有三种形态**，全部不含「拿时刻去比区间」那种脆弱形状：
　`len(数组) >= N`（**32 处断言**，量的是**条数**不是时刻）·
　`gap >= X`（**下界**，**11 处**，量的是**真的等了**）·
　`max(gaps) < X`（**上界**，**5 处**，量的是**没被拖住**）。
⇒ ⚠️ **而它顺带收窄了上面 §7.1 那条「16 ms ⇒ 计时判据一律不可信」** ⇒ 而不收窄的
代价是那 **16 处下界判据**会被当成「不可信」而白改（§7.1 第 4 条：误报制造无用工作）。
⇒ ⚠️ **而本条点名的那条形状【归属错了】**：原写 `tests/test_discord_gateway.py`
用时间戳数组判「区间是否重叠」⇒ ⛔ **实测那个文件从来没有过该判据**（它判**心跳周期下界**）；
真正的**重叠**守卫在 `tests/test_concurrent_send_failure_attribution.py`，而它**早已改成
进出台账**（该文件墙钟读取点 AST 实测 **0 命中**，其 docstring 里逐字记着那次
`434569.687 not less than 434569.687` 的踩坑）⇒ 「本仓库栽过这个形状」成立，
但**文件指错了** ⇒ 指向错文件会让下一个人去改一个**根本没坏**的地方。
⇒ **反向证明 8/8**（注入**生产侧缺陷** ⇒ 用例变红，⛔ 不是「改回去会红」）；
其中两条第一版报「无辨别力」⇒ **先怀疑探针**后确认是**缺陷注入无效**，改对注入点后都变红。
⇒ ⭐ **判据（可复核，且是这一整轮最该留下来的话）**：每条断言都要能说出「**什么情况下它会红**」—— 说不出的就是恒真的（§9）。
⚠️ **而普查探针本轮坏了四次，每次方向都是「报少」**（读完第一个名字就 break ·
只在断言操作数里找数组名 ⇒ 11 处全漏 · 把每个推导式的绑定名都算「派生」⇒ 301 处噪声 ·
数组名按全局取而非按文件）⇒ **报少不会报警** ⇒ 故每版都要先贴命中数再下结论。)
（**2026-10-07 已从本清单移出三条** —— 它们**不再是未完成的事**，决策与代价留在总表那些
`~~…~~` 行里，与 `~~698 行~~` 同一形态。⚠️ 顺带记下本轮的三条教训，因为它们治的
都是「我拿它当可信的东西」：
① **SSE 线程无监督者、`/api/event` 任何非 200 都重新抛出 ⇒ 线程永久死亡**（`fix-289`）
⇒ 新模块 `subscription_supervisor.py` 持有线程主体，**有界退避**后重新订阅
（首档 0.5s / 上限 5.0s / 收到帧即重置）；⚠️ **刻意不**照抄客户端内部 0.5→30s ——
那一层只在**一次连接之内**生效，30s 的代价是整整 30s 静默；
⚠️ **在途 turn 不断线时 finalize**（那会把半截正文当最终答复发出去、再让续跑的 delta
**另建一个 turn** ⇒ 同一条回复发两遍）⇒ 做法是**什么都不动**；
② **进度句柄交接竞态**（`fix-288`）⇒ 每轮一个**创建权** claim，锁内认领、锁外发送
（⚠️ 自查「`with lock` 里含 send 的处数 = **0**」——各适配器 `send()` 会 sleep）；
③ **`Adapter._last_send_error` 是无锁的槽**（`fix-286`）⇒ 改按**线程归属**、**未加锁**）

· ~~**独立复核 `flush_queue` 丢唤醒：丢唤醒的时序已被写实**~~ ⇒ **已闭合** ✅
（2026-10-07 · `ad9cce2`/`fix-301`，2026-10-13 核对和解）——
本行原文的「⛔ 没动」写的是 `fix-297` 复核**当时**（同日早些）的状态；
同日稍后的 `ad9cce2` 已根治（记忆唤醒 `_flush_requested` + `_drain` 仍握排空权的
409 分支兑现，生产净增 1 字段 + 2 分支），本行判据（「见 `_draining` 即返回」分支
**确定性触发一次**）已由 tests/test_inbound_gateway.py 满足：
`test_a_flush_landing_while_a_prompt_is_in_flight_is_not_lost`
（真排空线程卡在 `client.prompt()` 内、两个 `Event` 同步点、⛔ 不用 `time.sleep`，
判据 = 那条交错下消息确实回来）+ 对照臂 `test_the_control_interleaving_still_drains`
（两组只差 flush 落点）+ 热循环上界 `test_a_second_busy_hands_over_instead_of_looping_forever`；
⚠️ 本节此前一直按开放项列着——2026-10-11 重算清单误按 `fix-297` 复核的旧状态抄入，
今日逐字核对生产侧（`_flush_requested` 机制俱在）与守门后和解，
证据见总表 ~~独立复核…~~ 与 ~~`flush_queue` 与「409 退回队首」之间丢唤醒~~ 两行闭合行；
· ~~**a2a：用户只敲 `..` / `!!`，对端就收到 `task completed`**~~ ⇒ **已闭合** ✅
（2026-10-10 · `7721ed6`）—— **第 1 类「已确认缺陷」**（认领人=编排者）。
⚠️ **2026-10-07 由 `fix-297` 通读时顺带查出（此前台账里没有这一条）**：
三条缓冲回执（`BUFFERED_NOTICE` / `HELD_EXPIRED_NOTICE` / `HELD_NOT_DELIVERED_NOTICE`）
共用 `kind="text"`，而 a2a 映射表把 `"text"` 判成 `TASK_STATE_COMPLETED` ⇒
**agent 还没跑**，对端已收到 task completed（与 `fix-293`「取消被映成完成」形状不同、别混）。
⇒ **已落地（2026-10-10 · `7721ed6`）**：三条回执各获独立 kind 字面量
　（`buffered` / `held_expired` / `held_not_delivered`），映射表非终态档指向
　`STATE_WORKING`（判据「指向非终态」成立）；`send()` 对非终态档**不走 `_finalize`** ——
　不判终态、不唤醒阻塞的 SendMessage（提前唤醒 = 对端拿到非终态 Task 返回，违反 §3.1.1）、
　不用回执覆盖 `task.reply`；真正的答复仍由 `text` / `final` 写入，或 `reply_timeout`
　判 `TASK_STATE_FAILED`（响亮失败）。
⇒ ⚠️ **知情代价**：阻塞语义没有「中途消息」通道（推送订阅未实现），回执文本
　**不送达** a2a 对端 —— 它能感知的只有任务仍在 WORKING。
⇒ ⭐ **守门名单自身的缺口（本条顺带量出）**：AST 覆盖面守门只收 `Outbound` /
　`self.send_text` / `self.finalize` 的字面量，而真实调用点全叫 `self._send_text` /
　`self._finalize`（core 注入的名字）、`self.finalize` 生产 **0 命中** ⇒ 此前守门对
　回执调用点**失明**，「守门仍绿」是空转 ⇒ 名单已补（tests/test_a2a.py）；且守门对
　「回退 `kind="text"`」恒绿（text 已登记），只有 tests/test_inbound_gateway.py
　三处 kind 钉能抓（反向证明 M3 验过）。
⇒ 守门 = test_a2a 新行为测试（三 kind × 不判终态 / 不写 reply + 对照臂）+ 表穷举 +
　AST 守门扩名单 + test_inbound_gateway 三处 kind 钉；反向证明 M1（表项指回
　COMPLETED）/ M2（拿掉非终态分支）/ M3（调用点回退）真红、SHA256 对基线。
　⛔ 未跑全量 ×3（定向 tests.test_a2a + tests.test_inbound_gateway +
　tests.test_inbound_merge + tests.test_outbound = 324/324 绿）。
· **2026-10-07 已从本清单移出一条** —— `inbox_recovery._replay_one`：启动重放时 dispatch 异常仍记成 `failed`（`fix-300` 已落地）：
　根因 = §8 第 3 条**第二次**同型：`_dispatch_prompt` **判出了分档，却把它压成一个
　字符串交给恢复层** ⇒ 判定所需的信息压根没被记录 ⇒ 正确做法是**记录**它。
　⇒ ⭐⭐ **而本条最该留的是【编排者的派单前提是错的】**：我写「判 `status is None`
　　⇒ 落 `outcome_unknown`」，而 `dispatch_recovered` 的 `raise OpenCodeError(f"…")`
　　**没传 `status=`**、而默认值就是 `None` ⇒ **恢复路径上每一次非 ok 都是 status 恒为 None**
　　⇒ ⇒ 那会让**所有**重放失败落 `outcome_unknown` ⇒ **退避阶梯彻底失效**
　　⇒ ⇒ **一个比所修缺陷更糟的回归** —— 是实现者**当场驳回**的，⛔ 不是事后补的。
　⇒ ⭐ 而根因是**我量漏了**：上一条量的是 `_replay_one` 的 `except`，
　　⛔ **没量 `dispatch` 在恢复路径上抛什么**
　　⇒ **「量到了 except」不等于「量到了输入」** —— 而判据的输入在被测对象的**上游**。
　⇒ **两处恢复层分档** + **盖戳优先**（`mark_prompt_never_sent`，由唯一知道那件事的投递侧盖）
　　**缺省一律按「可能已跑过」处理**（fail-safe）⇒ 因为 `create_session` 阶段的
　　传输失败**也是** `status=None`，而那一刻 prompt **从没**交出去 ⇒ 只看 status 会说假话。
　⇒ ⭐ **守门**：AST 扫**生产侧全部 `.py`**，凡落 `failed` 的路径 `reason` 必须含 status
　　线索（生产侧调用点 **3** 处 · 违规 **0**）⇒ 且**先证明了辨别力**：注入「去掉 `status=`」
　　⇒ **只有那条守门红，其余 47 条照样绿**（§9：一条恒真的守门比没有守门更危险）。
　⇒ ⚠️ **知情代价**：恢复路径上服务端**真正的** status 没跨过边界 ⇒ **分档不受影响**、
　　丢的只是**排查用的那个数字** ⇒ **属「代价已知且刻意不做」**，⛔ 不是「未做」；
　　想带回来必须改 `_dispatch_prompt` 的返回契约 ⇒ **⛔ 不许在缺陷修复里顺手做**。
· **`outbound` 的失败可见性挂在【可被装配关掉的那个通道】上** ⇒ **第 3 类**
（中 · 认领人=编排者 · 2026-10-08 起；⚠️ **登记于 2026-10-07**，来自一轮审计结论，
　**零 `.py` 编辑**）：`_record_outcome` 把那条 `logger.warning("出站失败 …")` 放在
　`if recorder is None: return` **之后** ⇒ 没装记录器时**结构化失败零日志、零落盘**，
　而 `send_text` 返回 `None` ⇒ 与「没发过」**同形**。
　⇒ **实测三臂（各只差一个变量）**：装+失败 ⇒ WARNING **1** 条（点名平台/会话/分类/原因）；
　　**没装+失败 ⇒ 0 条**、无文件；装+成功（对照）⇒ 0 条。
　⇒ ⚠️ **但生产可达性实测为「基本不可达」**：记录器三条构造路径**都不抛**，
　　而它在 `core.start()` **之前无条件安装** ⇒ 「没装」在生产**不是**构造失败造成的。
　⇒ **必填件（代价是什么、为什么可接受）**：代价 = **唯一让失败可见的机制，
　　同时就是可被装配关掉的那个**；为什么可接受 = **解耦是刻意的**
　　（排障通道不该决定桥的生死）⇒ ⚠️ **知情代价：现状是解耦过头**。
· ~~**部分失败无回滚、无中止、无重发 ⇒ 读者拿到的是静默截断的答复**~~ ⇒ **已闭合** ✅（2026-10-09 · `bc926e8`）—— **第 3 类**
（高 · 认领人=编排者 · 用户 2026-10-09 拍板「加缺口标记」）：**实测两臂只差「第 2 段」那一次失败**
　⇒ 读者只收到 `["HEAD"]`，而全成功（对照）是 `["HEAD","TAIL"]`。
　⇒ **前段留着不回滚、后段失败循环继续** ⇒ 而正文**没有缺口标记** ⇒
　　只有日志与 `--status` 知道；⇒ 而缺口那一侧与上一条**同一个通道** ⇒
　　没装记录器时**彻底没有痕迹**。⇒ ⭐ **出站侧无去重键** ⇒ 两次逐字节相同的
　　`send_text` **2 次都真的发出去**（⚠️ `sha256` 那个「平台+会话+正文」三字段去重键
　　**只管入站去重**）。
　⇒ **代价 = 读者永远看不到 `TAIL`，而他没有任何办法知道**；
　　⇒ ⚠️ **为什么可接受 = 【待用户裁定】** —— ⛔ **不许编一个「可接受」的理由**：
　　审计明确说这一条它**没有下「该修」的结论**，而那是**产品判断**。
　⇒ **2026-10-09 用户裁定并拍板「加缺口标记」**：telegram `send()` 部分失败（前段已上线、后段失败）时追加缺口标记消息（`SEND_GAP_MARKER_TEXT`）；标记本身失败只留 WARNING；第 1 段就失败仍 `None`、零标记；返回值契约未动。守门 = tests/test_telegram_partial_send_gap_marker.py 四臂；反向证明 M1（拿掉标记块）/M2（熄掉「一段都没上线」闸）真红、SHA256 对基线。⛔ 未跑全量 ×3。
· **`discord.py` 的 429 单次重试用裸 `time.sleep` ⇒ `stop()` 打不断
⇒ 一次关停最坏被拖住 60 秒** ⇒ **第 4 类「判据尚未标定」**
（中 · 认领人=编排者 · 2026-10-08 起）
　⇒ ⚠️⚠️ **此结论曾错（commit `e090c60`）—— 已由 2026-10-08 的 telegram 审计更正**：
　　**它只点了 `discord.py` 的 429 那处**（`discord.py:1110`）而**没有枚举**
　　⇒ 读起来就成了「唯一会等 `retry_after` 的地方」⇒ 而**那是错、不是过期**
　　（§7.1：文件没变、结论本身就不全）。
　⇒ ⭐ **错因（这一条最该留的就是这句）**：上一轮 outbound 审计
　　**按「调用文本里含 `retry` 才算」筛等待点** ⇒ 而 telegram 那处写的是
　　`time.sleep(delay)`、**参数名不带 `retry`** ⇒ 被漏掉
　　⇒ ⇒ **判据宽度不足，让一条已交付（已推送）的结论出错**。
　⇒ **已修正判据口径**：改为**全量枚举 `sleep` / `wait` 调用点**，⛔ 不再按参数名筛。
　⇒ ⭐ **正确说法 = 至少 3 处**：`discord.py:1016` · **`discord.py:1110`** ·
　　**`telegram.py` 的 `_api` 里那处 `time.sleep(delay)`**（429 之后睡 `retry_after`、
　　**重试一次**；实测 `retry_after=5` ⇒ 真睡 **5.0 s** 且第二次真成功，喂 `9999` ⇒ 夹成
　　`MAX_RETRY_AFTER = 60.0`；对照臂 200 ⇒ **不等**、只调一次 `_post`）
　　⇒ ⚠️ **行号按 commit 标注**：telegram 那处审计时是 `:328`，而 ⚠️ HEAD `4285b5b`
　　（另一条 lane 落了 76 行）已移到 `:348` ⇒ ⇒ **以符号名为准**（§7.1：`file:line` 会过期）。
　⇒ **本条自己的结论一字未动**：`_stop_event.is_set() == True` 而工作线程**仍在等待**；
　　**上界 = `MAX_RETRY_AFTER`（从模块读）= 60.0**；**阶梯不存在**（只重试一次）；
　　`_throttle` 用 `_stop_event.wait(wait)` ⇒ **能打断** ⇒ 两个方向都在同一文件里。
　⇒ **知情代价**：一次关停最坏被拖住 **60 秒**（`join(timeout=)` 是否因此留下悬挂线程**未追**
　　—— 那属 `transport/base.py`）。
　⇒ **标定它需要的那组数 = 一次真实的关停耗时** ⇒ **下一步：量一次真实的关停耗时**；
　　⛔ **在量出来之前不许改**。
· ~~**telegram 的 `answerCallbackQuery` 返回 `ok:false` ⇒ WARNING 及以上 0 条
⇒ 按钮转圈停不掉，而默认档位无迹**~~ ⇒ **已闭合** ✅（2026-10-09 · `63d7434`）—— **第 1 类「已确认缺陷」**
（高 · 认领人=编排者 · 2026-10-08 起）：`answer` 抓到 `ok is not True` 只打一条
　`logger.debug` ⇒ **默认档位下零日志**；而转圈**只有** `answerCallbackQuery` 能停掉
　⇒ 用户看到的就是「点了没反应、也永远转下去」。
　⇒ **实测两臂只差「返回 ok:false」与「抛异常」**：ok:false ⇒ WARNING 及以上 **0** 条
　（只有那一条 debug）；抛异常（**对照臂**）⇒ **1** 条 ⇒ **判据有辨别力**
　（两臂都仍然调了 `on_callback`，`finally` 契约成立）。
　⇒ **已落地（2026-10-09 · `63d7434`）**：提到 `warning` + 点名 `query_id`；行为型守门
　`tests/test_telegram_answer_not_ok_warning.py` 三臂钉住「ok:false ⇒ 恰好 1 条
　WARNING 及以上；ok:true ⇒ 0 条；抛异常 ⇒ 1 条 ERROR 不被碰掉」⇒ 反向证明
　M1（降回 debug、去 `query_id`）⇒ 守门真红、还原后 SHA256 对基线。⛔ 未跑全量 ×3。
· ~~**telegram 的 `channel_post` / `edited_message` 等形状入站 ⇒ 投递 0 条 + 日志 0 条
⇒ 频道发言者永不解释**~~ ⇒ **已闭合** ✅（2026-10-09 · `63d7434`）—— **第 1 类「已确认缺陷」**
（高 · 认领人=编排者 · 2026-10-08 起）：**两臂只差 update 里的键名、正文逐字相同** ——
　`message` ⇒ 投递到上层 **1** 条 / 日志 **0** 条；`channel_post` ⇒ 投递 **0** 条 / 日志 **0** 条。
　⇒ ⚠️ **零投递是设计、零解释不是**（源码注释写着「fall through and are ignored」）
　⇒ 而两个臂日志都为空 ⇒ 从日志上**完全同形**。
　⇒ ⭐ **必填件**：**频道里的发言永远不会被回答，而没有任何一句解释**。
　⇒ **已落地（2026-10-09 · `63d7434`）**：fall-through 处补一条 INFO 只记形状（排序
　顶层键名，⛔ 不记载荷）；守门 `tests/test_telegram_non_message_shape_info_log.py`
　五用例钉住「无 message 键形状 ⇒ 投递 0 + 恰好 1 条 INFO；哨兵零泄漏；正常 message
　与贴纸形状零条该 INFO」⇒ ⛔ 仍不接入站（频道每条发言都触发 agent）；反向证明
　M2（拿掉 INFO）⇒ 守门真红、还原后 SHA256 对基线。⛔ 未跑全量 ×3。
· ~~**`callback_data` 【静默按字节截断】⇒ 点击回来的 data 与按钮绑定的不是同一个值**~~
⇒ **已闭合** ✅（2026-10-09 · `912dbb1`）—— **第 1 类「已确认缺陷」**（高 · 认领人=编排者 · 2026-10-08 起）：
　`button.data.encode("utf-8")[:CALLBACK_DATA_LIMIT].decode("utf-8", errors="ignore")`
　（上限从模块读 = **64**）⇒ **按字节切、再把切碎的半个字符 `ignore` 掉**。
　⇒ ⭐ **必填件（用户可见的后果）**：**用户点了却触发另一个动作**，而 Telegram **不校验**
　回调 data ⇒ **不会报错，只会静默走偏**。
　⇒ **实测四组（只差输入）**：ASCII **70** 字节 ⇒ **64** · `配`×**40**（**120** 字节）⇒ **63**
　（切在多字节中间）· `配`×**21** + `TAIL`（**67**）⇒ **64**（尾巴切在中间）· 恰好 **64** ⇒ **原样**。
　⇒ **下一步（编排者，2026-10-08 起）**：先加守门钉住「超限**不许静默**」；
　⇒ 处置（报错 / 改传 `message_id`）是**产品判断**，本条**不代拍**。
　⇒ **2026-10-09 用户拍板「报错拒绝」**（否掉截断与改传 `message_id`）：`_button_row` 超限抛 `ValueError`（与 `edit()` 超长正文闸同形）；`edit()` 逐按钮放弃 + WARNING 点名位置/label/字节数/上限，全部被拒则不下发空 `inline_keyboard`。守门 = tests/test_telegram_callback_data_reject.py 四臂（含「截断前缀绝不进 payload」的缺陷本体断言）；反向证明 M1（还原旧截断）/M2（闸放宽 `>=`）真红、SHA256 对基线。⛔ 未跑全量 ×3。
· ~~**`poll_timeout` 【无上界】⇒ 一个写错的旋钮造出约 31 年的 socket 超时**~~
⇒ **已闭合** ✅（2026-10-09 · `cd295d0`）—— **第 1 类「已确认缺陷」**（中 · 认领人=编排者 · 2026-10-08 起）：
　`_coerce_poll_timeout` **只有下侧那道闸**（`seconds <= 0` ⇒ 回落 **25** + 一条点名该键的
　WARNING）⇒ **上侧没有任何闸**。
　⇒ **实测四臂（只差配的值）**：没配 ⇒ **25** / socket **40.0** / 告警 **0** ·
　配 `10**9` ⇒ **原样 1000000000** / socket **1000000015.0** / 告警 **0** ·
　配 **0** ⇒ **25** + **1** · 配 `"不是数字"` ⇒ **25** + **1**（后两臂**点名 `poll_timeout`**）。
　⇒ ⭐ **必填件 = 那个不对称**：**下限有闸、上限没有** ⇒ `stop()` 的已知天花板本就是
　「最长等 `poll_timeout + 15`」⇒ 一次写错（多打一个零）就是**约 31 年**不可停止。
　⇒ **下一步（编排者，2026-10-08 起）**：给上侧补同形的闸 + 守门；⚠️ **上界是产品判断**，⛔ 不代拍。
　⇒ **2026-10-09 用户拍板上界 = 50**（`POLL_TIMEOUT_UPPER_LIMIT`，对齐 Bot API 服务端挂起上限）：越界一律回落默认 25 + 点名告警（不钳位，纪律照抄 nextcloud 先例——服务端会拒的值不采纳）。守门 = tests/test_telegram_startup_probe.py `TestPollTimeoutIsNotFatal` 三臂；反向证明 M1（拿掉整段闸）/M2（闸放宽 `>=`）真红、SHA256 对基线。⛔ 未跑全量 ×3。
· **telegram 的 `_last_send` 【永不淘汰】⇒ 纯内存、无界增长**
⇒ **第 3 类「代价已知且刻意不做」**（低 · 认领人=编排者 · 2026-10-08 起）：
　发过 **50** 个不同会话后 `len == 50`，**淘汰逻辑 = 无**（全部写入点里没有一处
　`del` / `pop` / 长度上限）。
　⇒ ⭐ **代价（已量清）**：**每会话一个 `float`、长期运行单调增长、无淘汰** ⇒
　纯内存、**不落盘**、**零告警**。
　⇒ ⚠️ **为什么可接受 = ⛔ 待用户裁定** —— ⛔ **不许编**：内存增长确实缓慢，但
　**没人量过它在多长的运行里有多大**；上限由「多少个不同 chat 用过这个 bot」决定 ⇒
　本机无外网凭据 ⇒ **这个分布现在量不出来**。
　⇒ ⛔ **刻意不做**：加淘汰要引入「多久算旧」这个阈值，而那是**没人量过的数**。
· **`_advance_offset` 对畸形 `update_id` 【静默 return】⇒ 该条 offset 不推进
⇒ 会被服务端重发** ⇒ **第 3 类「代价已知且刻意不做」**（低 · 认领人=编排者 · 2026-10-08 起）：
　`int(update.get("update_id"))` 抛错时**只 `return`**、**零日志**
　（⚠️ AST 普查 · HEAD `4285b5b`：**20** 个 `except` 处理器里 **8** 个不抛也不打日志 ⇒
　**这个数按 commit 标**，`e090c60` 时是 7）。
　⇒ ⭐ **为什么可接受**：Telegram 的 `offset` 是「确认到此为止」⇒ offset 不前进 ⇒
　**服务端会重发 ⇒ 方向是重复、不是丢失**（`offset` 前进才是丢弃的方向）。
　⇒ ⚠️ **知情代价**：重复处理；而**去重键能否挡住这次重发未追** ⇒ ⛔ 不许拿「有去重」当无害。
　⇒ ⭐⚠️ **而它与顺序铁律相反**：铁律是「乱序 / 回退的 update 要丢掉」⇒
　而**畸形 `update_id` 被留着等重发**。
· **`_flush_history_once` 【永久丢弃积压历史】（设计如此）
⇒ 停机期间的消息恢复后永远收不到** ⇒ **第 3 类「代价已知且刻意不做」**
（低 · 认领人=编排者 · 2026-10-08 起）：`_flush_pending` 把 `self._offset` 置成
　**最后一个 `update_id + 1`** ⇒ Telegram 的 `offset` 语义是「**确认到此为止**」⇒
　**永久丢弃**（不是延后、不是补投）⇒ 且**每进程只跑一次**。
　⇒ ⭐ **知情代价**：**那段时间的消息永远收不到，而日志里没有任何一行说「这些被丢了」**
　（唯一那条 `failed to flush pending updates` 还是**失败时才有**）。
　⇒ ⭐ **为什么可接受 = 设计如此**：`flush` 的定义就是丢弃（不是回放）⇒ 目标是
　**别把断线期间的旧消息灌给 agent**。
　⇒ ⚠️ **零告知那半边不是设计** ⇒ ⛔ 混在一起会让「刻意丢弃」替「零告知」背书。
· **平台 `description` 是【逐字信道且会落盘】（管道已实测，含 token 未测到）
⇒ 第 2 类「根因未证实」**（中 · 认领人=编排者 · 2026-10-08 起）：
　**实测**：探针串逐字覆盖 `failure.description` = true · 出现在日志里 = true ·
　出现在 `startup_verdict` 里 = true ⇒ 而它由 health **落盘** ⇒ WARNING 及以上 **2** 条。
　⇒ ⚠️ **未测到、⛔ 不许写成已证实**：真实的 `http.client` 失败消息**会不会含
　`/bot<token>/…` 这样的路径** —— 要一次**真实的 HTTPS 失败**，而本机**无外网凭据、无外网**
　⇒ **管道已实测是逐字信道，而那段内容会不会含 token 未测到**。
　⇒ ⚠️ **待查证据（现在没有）**：一次真实的 `getMe` 失败（TLS / DNS / 连接被拒），
　把 `str(exc)` 与 `description` 的**实际内容**逐字查是否含 `/bot<token>/`。
　⇒ **下一步（编排者，2026-10-08 起）**：在有外网凭据的机器上跑**一次**并把实际内容贴进台账
　⇒ 在那之前**保持第 2 类**，⛔ **不许归第 3 类**（「未测到」**不等于**「已证无害」）。
（**2026-10-08 telegram 审计的三个用户拍板已全部落地、以 `~~…~~` 闭合行登记** —— 它们从未以未完成形态进过本清单：**拍板一**（`bd20eeb`，2026-10-09）运行期 401 自愈 + `--status` 区分启动失败与运行期失效（两份文件 `platform-health.json` / `credential-failures.json`，⛔ 不混成一个键）；**拍板二/三**（`4285b5b`）`_retry_after` 只门 `error_code == 429`、`_on_update` 异常日志只记定位字段（不再 `%r` 整个 update）⇒ 决策与证据留在总表那些 `~~…~~` 行里。⚠️ **边界**：「启动失败→事后恢复」的可见性**不在**拍板一覆盖内（启动闸门的阶梯不写运行期记录器）⇒ 而「telegram 重试的代价」那一条也已于 2026-10-09 由用户拍板①「维持现状」闭合（零代码，证据见总表 ~~…~~ 闭合行）。）
## 5. 代码组织

按功能维度解耦，纯函数工具 / 数据源 / 事件处理 / UI 组件 / 配置各归各的模块。
**判定**：单文件 > ~400 **物理行**（含注释与空行），或出现多个互不相关的职责块 → 必须拆；
目标"改一处功能只碰一个文件"。入口文件只做组装（导入+装配+生命周期清理）。
**拆分时行为必须零变化。**

⚠️ **这里用物理行、而 §5.1 用 SLOC，不是笔误 —— 两条规则问的是不同的问题。**

| 规则 | 问的是 | 该用 | 为什么 |
|---|---|---|---|
| §5.1 类体量 | 这个**类**是否承担了过多职责？ | **SLOC**（去空行/纯注释/docstring） | 衡量**代码体量**；80 行解释性注释不是「胖」 |
| §5 文件 | 互不相关的职责是否**挤在一个文件**里？ | **物理行** | 物理行数恰好代理这件事，而 SLOC 会**藏住**真正的案例 |

SLOC 用在文件级会失效的具体反例：**一个文件里 400 行协议常量表 + 30 行逻辑，
SLOC 下「达标」，却恰恰是该拆的那个文件。**

⚠️ **因此两个数不可比** —— 别把它们并排放进同一张表当同一个尺度
（`a2a.py` 的 1510 是物理行，`EventStream` 的 449 是 SLOC，**并排看没有意义**）。
且这个数字有个已知偏向：**它高估了适配器的逻辑体量**（大段协议常量表被完整计入），
**同时低估了真正该拆的文件数** —— 这是两笔不同的债，一个数字把它们混在了一起。

### 5.0 文档豁免：一个文件要被拆，必须**同时**是两件事

> **不计入「超 400 物理行」名单 ⟺ 文档占比 > 45% 且 SLOC ≤ 400**（两条件同时成立）。

### ⚠️⚠️ 这两个数的口径（2026-10-06 补上 —— 此前**根本没定义**，而它们决定豁免）

**为什么必须补**：豁免与否**完全**由这两个数决定，而条文原先只写了阈值、**没写口径**
⇒ 实测同一个文件在不同口径下会在「豁免」与「必须拆」之间**翻面**
（⚠️ **这个现值已过期**：干净树 `4edd052`（**口径甲**）下
`tests/bridge_dir_isolation_scan.py` 文档占比 **45.4%**、SLOC **406**
—— ⛔ 原记「45.1% 与 SLOC 400、距两条线只有 0.1 个百分点与 **0 行**」里的
**那 0 行余量已被跨过** ⇒ 它现在是「满足①、被②拦下」⇒ **净结果仍待拆**）。
⚠️ 而下面「刀刃样本」那段给的是**分布**、**不再复述这两个数** ——
要核对就按上面那张口径表自己算。
⚠️ 后果已经发生：原先记的 `26 / 8 / 18` **用任何一组口径都复现不出来**
（实测 18 种组合：生产侧待拆在 **18~26** 之间，含 `tests/` 在 **63~72** 之间）。

**口径（照此算，别再换）**：

| 量 | 口径 |
|---|---|
| **扫描范围** | `git ls-files "*.py"` —— ⛔ **不是** `os.walk` 目录树：后者会把 gitignored 的目录**一并**算进来。
⚠️ **那个差值不可复现，⛔ 别把它写成数字**：`.tmp/` 是**共享 scratch**，里面有多少 `.py` 取决于当时在跑什么
⇒ 实测差值在 **2 ~ 24** 之间跳。⇒ **这正是必须钉住扫描范围的理由**：不是「会多算一点」，
而是「多算多少**不可预测**，所以那份名单本身不可复现」 |
| **物理行** | `len(io.open(p, encoding="utf-8").read().splitlines())` —— ⛔ 在 PowerShell 里用 `Measure-Object -Line` 会系统性少算约 13% |
| **文档占比** | `(docstring 行的集合 ∪ 纯注释行的集合) / 物理行`。⚠️ **裸**空行不算文档，**但 docstring 内部的空行照算** |
| **SLOC** | 对 `tree = ast.parse(src)` 做 `ast.walk(tree)`，取**每一个** `isinstance(n, ast.stmt)` 节点的行跨度之**并集**，**减** docstring 行，再滤掉空行与纯注释行。⛔ **枚举起点是 `tree` 本身，不要把 `tree` 传给 `spanned()`** —— `ast.Module` 没有 `lineno`，那样写会让**每个文件 SLOC = 0** ⇒ 条件②被静默关掉，**而豁免数与正确实现一模一样**（6 / 23）⇒ 零报警（详见 §5.1 代码块下方的警告） |

⚠️⚠️ **最后一行那个「不是同法」的坑，请照抄别改写**：§5.1 的代码块把 `cls`
写在 `spanned()` **外面**，所以最自然的文件级改法是**把 `cls` 换成 `tree`** ——
而 **`ast.Module` 没有 `lineno`** ⇒ `spanned(tree)` 返回**空集**
⇒ **每个文件的 SLOC 都变成 0** ⇒ 条件②被**静默关掉**，
**而且它给出的豁免数与正确实现一模一样**（都是 6 / 23）⇒ **没有任何东西会报警**。
⚠️ **可测文件里不会出现不可测的文件** —— 正确说法是：tracked `.py` 里**只有 `tests/__init__.py` 不可测**（它是空的）。
⚠️ 而这段警告与口径表里那一行、以及末尾「口径不可换」那段是**同一件事的三份拷贝** —— 以**表里那行**为准，⛔ 改一处就要同步另两处。

⚠️ **「裸空行」这个限定不能省**：docstring 跨度覆盖整个 `Expr`，
所以 **docstring 内部的空行本来就在分子里** —— 实测
`tests/bridge_dir_isolation_scan.py` 有 **64 行**、`opencode_bridge/health.py` 有 **82 行**
这样的行，而它们**确实承载信息**（段落分隔，是文档结构的一部分）。
⚠️ 本仓库**无反例**：149 个 tracked `.py` 里「docstring 跨度外的裸空行」占比最高（`test_normalize.py` 25.8%、`test_session_registry.py` 24.6% 紧随，⚠️ 三个都在 25% 上下 ⇒ **「裸空行占两成」是本仓库的常态**，不是个别文件的现象）
`tests/test_adapter_router.py` **25.9%**，且全是 PEP8 的两空行分隔 —— 没有任何文件拿空行当记录分隔符。
⇒ 而**把裸空行算进分子等于让「多敲回车」成为豁免理由**：
实测 5 个生产文件的文档占比全部骑在 45% 线上（`slack.py` 48.5 / `inbox.py` 51.8 /
`state.py` 45.5 / `status.py` 46.1 / `transport/base.py` 51.6），
剔掉裸空行后全部落到 42.5 / 43.7 / 37.5 / 35.3 / 43.9 ⇒ 豁免集**不该由回车次数决定**。

**范围：测试文件不适用【400 那条行数触发线】（它们走单独的、更高的那条），
但 §5.0 的文档豁免对测试文件【照常适用】—— ⚠️ **2026-10-07 重算后是【0 个】**（原先记的 1 个已不成立，成因是**文件变了**、⛔ 不是口径变了）。**

⚠️ **这是两件事，别混**：豁免看的是「是不是散文撑大的」，与是不是测试无关；
触发线看的是「每职责多少行」，测试与生产**不可共用一个数**。

⚠️ **为什么不能把 400 直接套到测试上**：一个 test case 要 10~15 行 arrange/assert，
再加协议 fixture 辅助函数 ⇒ **每条职责的行数天然比生产模块高**，
用生产模块标定的阈值去裁测试，是在**裁行数**而不是裁职责。
实测：测试侧 46 个超 400 的文件里 **10 个落在 403~581 区间**、16 个落在 403~700，
而 §5.0 在测试侧**当前豁免 0 个**（⚠️ 原先记的「1 个」在 `d98f723` 重算后不成立） ⇒ 套 400 就等于把本节变成一条**裸的配额**。

⛔ **但也别写「排除 `tests/`」** —— 那是把一个**测量问题**当成**人为豁免**。
⚠️ **那条更高的触发线目前【尚未标定】**（标定输入 =「单职责测试文件的长度分布」，
⛔ 我不拍这个数）⇒ 测试侧那 46 个的状态是**「判据缺失」** ——
⛔ **不是**「判过了所以合规」，**也不是**「判不过所以违规」。
⚠️ **「判据缺失」就是 §4.1 第四类「判据尚未标定」在本节的叫法**（§4.1 的名字是正名，台账也用它）—— ⚠️ 一个状态两个名字会让读者以为是两件事。
它有出口条件：一旦有人写下「单职责测试文件的长度分布」
这组数 ⇒ **阈值即可定** ⇒ 本条生效。⚠️ **写下分布不等于选出了阈值** —— 中间还差
「从分布里读出一个数」这一步 ⇒ **出口是两步，不是一步**。
在那之前，**任何人不得把测试侧的数字当作「已达标」或「已超线」来引用**。
（⇒ 它与 §4.1 的第四类「判据尚未标定」是同一个状态，两处必须一起改。）

**当前数据**（`git ls-files "*.py"` = **200** 个，其中空的 `tests/__init__.py` 不可测
⇒ **可测 199**；上表口径。⚠️ **量于 2026-10-16 于 `b57341e`（A1 提交自身的树，commit 后确认工作树干净、stash 0；生产侧超线 **27** / 豁免 **11** / 待拆 **16** · 测试侧 **58** / 0（⚠️ 新增入名单者 = `inbox_recovery.py` 417/51.3%/SLOC 163 ⇒ **入豁免侧** ⇒ 超线 26→27 与豁免 10→11 均由它引起；A1 新模块 `delivery_retry.py` 297/44.4%/SLOC 141 <400 不入名单；测试侧 +1 = `test_delivery_retry.py`；`inbound_gateway.py` 1236→**1235** 物理行、45.9%/SLOC 618 ⇒ **个数不减**、仍被②拦下 ⇒ 仍待拆）；`inbound_gateway.py` 45.8%/595 → **45.9%/618**（仍被②拦下 ⇒ 仍待拆），A2 新模块 `request_phase.py` 79/54.4%/SLOC 28 <400 不入名单；重算脚本自检断言本轮 exit=0：既有锚**全复现**（homeassistant 1401 / telegram 1107/36.9%/648 不豁免 / telegram_credential_lifecycle 426/50.7%/186 豁免 / **inbound_gateway 45.9%/618** / telegram_protocol <400 / bridge_dir_isolation_scan 45.0%/422 / adapters/a2a 1196/37.1%/679 不豁免 / a2a_task_ledger 174 <400）+ **两陷阱自检通过**（0 个文件 SLOC==0 ⇒ 枚举起点确是 `tree` 本身；不可测恰好 1 个 = `tests/__init__.py`）；文件数常量 194→198 系手维护项已同步 = 194（`983130b`）+ 2（C）+ 2（A2））；⚠️ **再重算（2026-10-19 于 B 落地 commit `c411ea8` 的树；recompute_s50_d.py 自检断言 exit=0、两陷阱通过）**：tracked `.py` **200→204**（B 的 `cold_history_recovery.py` + `test_cold_history_recovery.py` 各 1 + D 的 `permission_timeout.py` + `test_permission_timeout.py` 各 1，四个均 <400 不入名单）；超线 **27** / 豁免 **11** / 待拆 **16** · 测试侧 **58** / 0 **个数逐字不变** —— `inbox_recovery.py` 417→**449**（B 加 `recover_cold` + step 3 拆分，仍在豁免侧、占比与 SLOC 不越界）、`inbound_gateway.py` 1235→**1281**（B 加 `session_for` + `recover_cold` 闭包，**仍被②拦下** ⇒ 仍待拆，`permission_timeout.py` 170 / `permission_ledger.py` 293 均 <400）；锚点全复现 + 两陷阱自检通过；**下一步 = 排期面空闲时回第三刀 `OpenCodeClient` 类手术**（§5.1））**——
而这一节自己的规则是「**任何改动都要重算**」⇒ **一个 commit 就足以让这组数过期**；
读到过期数时**先按上面那张口径表重算**，⛔ 不要直接引用）：

| 范围 | 超 400 物理行 | 被 §5.0 豁免 | 待拆 |
|---|---|---|---|
| 生产侧 | 27 | **11** | **16** |
| 测试侧 | 58 | **0** | （触发线未标定 ⇒ 判据缺失） |
| 合计 | 85 | **11** | — |
⇒ ⚠️ **2026-10-07 再次重算（干净树 `4edd052`；⭐ 本次数取自干净树 `4edd052`）**——
**口径甲**（分子含 docstring 跨度的**内部空行**，⛔ 见下面那张口径表）：
　生产侧 **29 / 8 / 21**（原 29 / 7 / **22**）· 测试侧 **55 / 0**（与原记录一致）。
⇒ **变的是豁免数 7 → 8**，且成因**不是口径变了**（口径表一个字未动），而是
　**文件变了**（⚠️ 「过期」≠「错」，§7.1）：
　① `transport/base.py` **43.9% → 49.2%**（SLOC **296** / 物理 **683**，最后一次提交 `0ba5b1f`）
　　⇒ **新满足 ①②** ⇒ 豁免 ⇒ ⚠️ 下面那句「`transport/base.py` 43.9%【待拆】」**已过期**；
　② `inbound_gateway.py` 43.7% → **45.0%**（SLOC **557** > 400）⇒ 满足①、**被②拦下**
　　⇒ **净结果仍【待拆】**（⚠️ 「两个都挂」≠「只挂一个」，§5.0 末尾那条纪律）；
　③ `tests/bridge_dir_isolation_scan.py` 45.1% → **45.4%**（SLOC **400 → 406**）⇒ 同上
　　⇒ **净结果仍【待拆】**；而上面那句「距两条线分别只有 **0.1 个百分点**与 **0 行**」
　　—— ⛔ **那 0 行的余量已被跨过**（SLOC 400 → 406）。
⇒ ⭐ **独立复现（属"现状"不是"改动"）**：超线文件共 **84** 个，文档占比 **6.8% ~ 57.9%**；
　生产侧豁免数随阈值 **11/11/11/9/8/6/2/0**（>25/30/35/40/45/50/55/60%）
　⇒ **单调、全程无断点** ⇒ 独立复现「**45% 是一个未标定的候选值**，没有分布上的依据」。
　⚠️ **两条曲线【不可比】**：本条那条是**生产+测试合并**（84 个），
　而 §5.0 下面记的那条是**生产侧** ⇒ ⛔ 不要把两个数并列引用。
⚠️⚠️ **第三个口径缺口（2026-10-07 实测撞到两次）**：上面那张口径表钉住了
   「**扫描范围** = `git ls-files "*.py"`」，却**没钉住「内容取自工作树还是 `HEAD`」**
   ⇒ 而重算脚本读的是**工作树内容** ⇒ **有 lane 在飞时重算读到的是半成品**：
   实测① `outbound.py` 量到 **570** 行（`HEAD` 是 482）· `core.py` **495**（`HEAD` 491）
   实测② 测试侧超线数在**同一天里**给出 **47** 与 **52** 两个值
   ⇒ 两次都被当时在飞的那条 lane 的改动污染 ⇒ **那份数字不可复现**。
   ⇒ **动作**：**先确认工作树干净再重算**（`git status --porcelain` 为空）；
   有 lane 在飞时**不要**引用上一次的重算值当现状 ⛔ 也不要把这次的当现状。
   ⚠️ 而这也是「这条规则存在却一直没人跑它的机制」的第三例（前两例：
   §4.1 的对齐判据、§4.1 的优先级格）⇒ **判据要接到动作上，不是写上去就算**。

⚠️ **口径修正把生产侧的被豁免数从 11 降到 6**，恰好 5 个文件因此从豁免变成必须拆：
`adapters/slack.py`（48.5%→42.5%）、`inbox.py`（51.8%→43.7%）、`state.py`（45.5%→37.5%）、
`status.py`（46.1%→35.3%）、`transport/base.py`（51.6%→43.9%）
⇒ **五个全部骑在 45% 线上** —— 这就是「口径没定义」的实际代价。

⚠️⚠️ **上一段说的是【那次口径修正当时】，不是现在 —— 而今晚实测其中一条已经翻了**（§7.1 表最后一行治的就是「同一小节两个相反结论」）：
**`inbox.py` 又是豁免的**（文档占比 **48.3%**、SLOC **218**、物理 **497**）
⇒ 被豁免数因此是 **7** 而**不是 6**、待拆 **22** 而**不是 23**。
⚠️ **原因不是口径变了**（上面那张口径表一个字未动），而是**它本身被改了** —— `RecordOutcome` 三态 + `_record_inbound` 的分支（文档占比 43.7% → **48.3%**）
⇒ 又一次印证「豁免判定是量出来的产物」：它**会因为别的改动而翻转**，与口径无关。
⇒ **动作**：报「某文件豁免/不豁免」时，**必须带 commit** ——
  不带 commit 的豁免结论在一个 commit 后就开始过期。

**当前 7 个豁免（文档占比 / SLOC / 物理行，量于 `a0c932e` 之上的工作树）**：
`adapters/base.py` 57.9/325/917 · `allowlist.py` **46.4**/181/407 · `core.py` 53.9/**203**/495 ·
`health.py` 57.5/364/1005 · **`inbox.py` 51.7/232/563** · `outbound.py` 50.8/259/592 · `redaction.py` 54.3/265/713
⇒ **七个全部骑在 45% 线以上**，而最低的 `allowlist.py`（**46.4%**）距线只有 **1.4 个百分点**
⇒ 又一个刀刃样本：一次注释改写就可能再翻一个。
⚠️ 上面那 5 个里今天**仍未豁免**的是四个（`slack.py` 42.5 · `state.py` 37.5 · `status.py` 35.3 · `transport/base.py` 43.9），`inbox.py` 已不在其中。
⚠️⚠️ **这一段的「当前 7 个」已过期**（**7 → 8**，量于干净树 `4edd052`、**口径甲**）：
**新增的第八个 = `transport/base.py` 49.2/296/683**（原 **43.9%** ⇒ 已过 45% 线）
⇒ 上面那句「`transport/base.py` 43.9%【待拆】」**作废**，它现在是豁免侧。
⇒ 而上面那张名单里的其余**七个逐字未变**（本次重算未触碰它们）。
⚠️ **上面那行 `transport/base.py 43.9` 我留着没删** —— 因为它是**历史记录**（「那次重算时的值」），
　⛔ 拿掉它就等于抹掉「它后来翻了面」这件事 ⇒ 而 §5.0 明写「**只报当前态，不报差值**」
　与「不带 commit 的豁免结论在一个 commit 后就开始过期」，两者都要靠**带 commit 的并存**才成立。

⇒ ⚠️⚠️ **2026-10-07 深夜重算（`d98f723` 的干净树；工作树空、stash 0 已确认）——而这次重算与原记录的差异【不是口径变了】**：**生产侧 29 / 7 / 22 逐字未变**。
⇒ **变的是这三处**，且成因是**文件变了**（tracked `.py` **155 → 158**）：
　① **测试侧超线 52 → 55**、**豁免 1 → 0** ⇒ ⛔ **那一格测试侧的豁免不再成立**
　　⇒ 而 §5.0 明写「**豁免判定是量出来的产物、不是事实**」⇒ 报它要带 commit（此处即是）；
　② `core.py` **198 → 203 / 484 → 495** · `inbox.py` **48.5 → 51.7**
　　（218 → 232 / 499 → 563）· `outbound.py` **49.2 → 50.8**（217 → 259 / 482 → 592）
　　⇒ 三个都**仍豁免**（占比全部 > 45% 且 SLOC ≤ 400）⇒ **豁免数 7 未变**；
　③ `base.py` 57.9/325/917 · `allowlist.py` 46.4/181/407 · `health.py` 57.5/364/1005
　　· `redaction.py` 54.3/265/713 ⇒ **逐字未变**（今晚没碰那四个文件）。
⇒ ⚠️ **而「待拆 22」这个数虽未变，其中一个文件的体量明显长了**：
　`inbound_gateway.py` SLOC **552**、文档占比 **43.7%**（⚠️ 贴着线下面 1.3 个百分点）
　⇒ 它**本来就在待拆名单里**，今晚又因恢复层分档长了几十行 ⇒ **个数不变、债在长**。
⇒ ⚠️ **刀刃样本实测（±2 个百分点）**：`inbound_gateway.py` **43.7%**【待拆】·
　`transport/base.py` **43.9%**【待拆】· `allowlist.py` **46.4%**【豁免，距线 1.4 个百分点】
　⇒ 又一次印证「**别把某文件豁免当稳定事实**」—— `allowlist.py` 一次注释改写就可能翻面。
⇒ ⭐ **而这次重算把两个已知陷阱做成了断言**（§5.0 明写它们「零报警」）：
　① **没有任何文件 SLOC == 0** ⇒ 枚举起点确实是 `tree` 本身
　　（⛔ 若把 `tree` 塞进 `spanned()`，每个文件 SLOC = 0 ⇒ 条件②被静默关掉，
　　**而豁免数与正确实现一模一样** ⇒ 零报警）；
　② **不可测文件恰好 1 个** = `tests/__init__.py` ⇒ 符合「可测文件里不会出现不可测的文件」。
⇒ **一条检查项，代替三段认错**：报「因口径变化导致豁免状态变了」之前，
**先确认该文件在旧口径下【曾经】豁免**。
「现在不豁免」**不是**「因口径变化而不豁免」——
**只报当前态，不报差值**。（⚠️ **两个**条件都挂的文件最容易被误报成「只挂一个」。）

⚠️ **豁免判定会产出刀刃样本，而阈值挪不动它**：超线文件的文档占比分布大致均匀
（55.5 / 55.0 / 54.3 / 53.9 / 49.2 / 46.4 / 45.1 / 43.9 / 43.7 / 42.5 …）
⇒ **任何整数阈值附近都有人**；把线挪到 50%，只是换成 `outbound.py` 的 49.2% 骑在 0.8 个点上。
⇒ ⛔ **别把「某文件豁免」当稳定事实** —— 名单是**量出来的产物**，不是事实。
「换口径就得重算」的真正含义是**任何改动都要重算**，⛔ 不只是换口径时。

**为什么要有这个口子**：§5 的目的是抓「**互不相关的职责挤在一个文件**」。
**一个「例如 60%」是解释性文档的文件不构成这个证据**（⚠️ 60% 只是示意，
⛔ 别当成另一个阈值 —— 真正的线是上面表里那个未标定的候选值） —— §5.1 已经为类体量认下同一件事
（「80 行解释性注释不是『胖』」），文件级只是漏了。

**⚠️ 第二个条件（SLOC ≤ 400）不是冗余，但它原先那个理由已经失效** —— 条款**保留**，
理由要换成它真正的分工：

- **条件① 是比值** —— 放过「1500 行但一半是散文」的文件。
- **条件② 是绝对上限** —— 在 **① 成立的前提下**有
  `SLOC ≤ (1 − doc_ratio) × 物理行 < 0.55 × 物理行`
  （docstring 的 `Expr` 本身就是 `ast.stmt`，必被 SLOC 减掉 ⇒ 散文占的份量必然从 SLOC 里扣走）。
  于是一个 900 行、50% 是散文的文件 SLOC ≈ 450 ⇒ **能过①、被②拦下**。
  ⇒ ② 在**逻辑上**不冗余，它是必要的兜底。
  ⚠️ **⛔ 别把那个界当全局不变量**：实测 146 个可测文件里 **94 个违反它**
  （如 `adapters/__init__.py` SLOC/物理 = 0.842、`tests/test_twitch.py` = 0.817）
  ⇒ **它只在①成立时可用**，而这恰好就是条件②唯一有话说的场景。

⚠️ **但它目前拦下 2 个文件**（⚠️ **原记 0 个，已过期** —— 量于干净树 `4edd052`、**口径甲**）：
`inbound_gateway.py` 45.0% / SLOC **557** 与 `tests/bridge_dir_isolation_scan.py` 45.4% / SLOC **406**
⇒ 两者都满足①（占比 > 45%）而 SLOC > 400 ⇒ **被②拦下 ⇒ 净结果仍是「待拆」**。
⇒ 原先那句「实测把线放到 40% 会多放行 7 个文件」是在**旧口径**下数的，作废。
⇒ ⚠️ **而这恰好是 ①②「两个都挂」的那种情形**（§5.0 末尾那条纪律：「**只报当前态，不报差值**」）
　⇒ 报豁免时要**分别报两个条件**，⛔ 不许只报净结果。
⚠️ **留一个条款比删掉它更危险** —— 删了没人知道它曾经有用；它现在**有话说了**
　（上面那两个就是它拦下的），⛔ 而它**仍不是**当前多数文件的生效条件。

⚠️ **45% 这条线【没有算出来的依据】** —— 原先写的「它是『①与②不再互相矛盾』的那一档」
经重测**不成立**：生产侧豁免数随阈值**单调**变化
（>25% ⇒ 12、>30% ⇒ 12、>35% ⇒ 11、>40% ⇒ 9、**>45% ⇒ 6**、>50% ⇒ 4、
>55% ⇒ 2、>60% ⇒ 0），**全程没有任何断点** ⇒ 45% 只是单调曲线上的一个点。
⇒ **当前它是一个未标定的候选值**，⛔ 不是「算出来的结论」。
⚠️ 标定它需要的输入是「单职责生产文件的长度分布」与「文档占比分布」，
⛔ **我不拍这个数** —— 拍出来的线会以「权威」的样子被引用很多次，
而今晚已经证明**没有依据的数会被当成事实传下去**。

⚠️ 完整数据、口径变更史、以及「为什么否决 SLOC 口径」的记录在 `tasks.md` 的
「📌 §5.0 口径：认错过程与变更史」一节 —— **AGENTS.md 只留口径与带口径的当前数字**，
⛔ 认错过程记在那里（三段同款的第一人称认错块会让读者学会**跳过** ⚠️ 标记，
而 ⚠️ 标记的全部价值就在「不能跳」）。

**⚠️ 口径不可换**：三个量的口径以上面那张表为准。⛔ **别写「文件级同法」** ——
`ast.Module` 没有 `lineno`，照 §5.1 那样把 `tree` 塞进 `spanned()` 会让**每个文件 SLOC = 0**
⇒ 条件②被静默关掉，**而算出来的豁免数与正确实现一模一样** ⇒ 没有任何东西会报警。
**换口径就得重算这份名单**
⚠️⚠️ **口径事故已经发生了一次（2026-10-07）—— 而它正是这句要防的那类**：
同一个 `inbox.py`、同一段口径，两个脚本给出 **48.3%** 与 **45.5%** 两个数。
⇒ 编排者按口径表的**字面**独立量了一遍（`docstring` 跨度【整个】算）：
　`inbox.py` 物理 **499** · docstring 跨度 **203**（其中**裸空行 33**）· 纯注释 **39** · SLOC **218**
　⇒ **实现甲 48.5%** ⇒ 条件① 满足 ⇒ **豁免成立**（口径表明写「⛔**裸**空行不算文档、**但 docstring 内部的空行照算**」）
　⇒ **实现乙 41.9%**（把 docstring 内部空行减掉）⇒ 条件① **不满足** ⇒ 豁免**不成立**
⇒ **实现甲才是口径表规定的那个** ⇒ `45.5%` 那个数**不采信、也不许写进台账**
⚠️ 而 **48.3% → 48.5% 不是矛盾、也不是被推翻**：前次量的是 `62654e9` 的干净树（`inbox.py` 497 行），而它被改了 2 行 ⇒ **那是【过期】**（§7.1：「错」与「过期」不是一回事）。
⇒ **动作**：重算前**先确认脚本按口径表的字面实现** —— ⛔ 一个口径不符的脚本给出的数**比没有数更坏**，因为它会被当权威引用。⇒ 而判据：`doc_ratio` 的分子里**必须含** docstring 跨度的**内部空行**。
 —— `tasks.md` 里已经因为「只记数字不记口径」被重测过两次：
一次结论从「6 个」变成「8 个」；一次是那份 `26 / 8 / 18` **到今天仍然谁也算不出来**。

### 5.1 类体量也要管

> 文件级规则管不住"一个文件里只有一个巨物"。类同样会胖，而且更难发现——
> 因为它语法合法、测试照过、评审时看着"挺整齐"。

**判定**（满足任一即须拆）：方法数 **~15+**、类自身代码行 **~250+**、或出现多个
互不相关的职责块。

⚠️ **「类自身代码行」必须按下面这个口径数，否则判定等于掷硬币。**
本仓库实例：`BridgeCore` 的类跨度是 **281 行**，其中 **80 行纯注释 + 38 行 docstring
+ 22 空行**。于是——

| 口径 | 结果 | 对 250 阈值 |
|---|---|---|
| 跨度 − 空行 | **259** | 超标 → `core.py` 自称「远低于阈值」是假的 |
| 跨度 − 空行 − 纯注释 | 179 | 达标 |
| **语句行（去空行/纯注释/docstring）** | **147** | 达标 |

**两个都不算 cheating 的口径，恰好骑在阈值两侧。** 本规则采用**最后一个**
（SLOC，标准口径）：阈值要衡量的是**职责体量，不是文字体量**——80 行解释性注释
不是「胖」。

可复现的数法（别手数，手数会把这三种口径数成同一个数）：

> ⛔ **在 Windows / PowerShell 里数行数一律不可信。** 实测
> `Get-Content f.py | Measure-Object -Line` **系统性少算** —— **2026-10-06 重测**三个文件：
> `inbox.py` 报 **426** 而 `splitlines()` 是 **497**（少 14.3%）；
> `state.py` 报 **480** 而 **560**（少 14.3%）；`core.py` 报 **444** 而 **484**（少 8.3%）
> ⇒ 原文那个「约 13%」应读作**约 8–14%**（三个文件不等，它**不是常数**）。
> ⚠️ **这一段里每个数都只属于写下它那一刻**：原文记的 `inbox.py` 报 412 / `splitlines()` 474，
> 而 2026-10-06 在 `HEAD` 上量得已是 **423** ⇒ **那是过期、不是被推翻**
> （§7.1：「错」与「过期」不是一回事）；而上面那句「系统性少算」今天复测**仍然成立**。
> 我据这个数差点**推翻一条正确的记录**（把一条 lane 报的对数当成错的）。
> ⇒ **落盘成 `.py` 再跑**（`len(io.open(p, encoding="utf-8").read().splitlines())`）。
> ⚠️ **别拿 `count("\n") + 1` 当交叉验证** —— 它与 `splitlines()` **不是等价写法**：
> **文件以换行结尾时它多数 1**。⚠️ 原文那个例子（`inbox_states.py` 33 vs 34）**也已过期**；
> 而它顺带写的「`inbox.py` 不以换行结尾」**今天已不成立** ——
> 实测 `inbox.py` 与 `state.py` 都是 `splitlines()` 与「count + 末尾修正」**两者相等**。
> ⇒ 要交叉验证就换**另一个独立**的口径，例如
> `len(src.splitlines())` vs `src.count("\n") + (0 if src.endswith("\n") else 1)`，
> 或与 `git diff --numstat` / AST 的 `end_lineno` 对照。
> 这与下面「PowerShell 里做复杂检查会被反引号吃掉」是同一条纪律的两面：
> **凡是要复算的量，都不要在 shell 里算。**

```python
import ast, io
src = io.open(path, encoding="utf-8").read(); lines = src.split("\n")
cls = next(n for n in ast.parse(src).body
           if isinstance(n, ast.ClassDef) and n.name == CLASS_NAME)
def spanned(node):
    start = getattr(node, "lineno", None)
    return set() if start is None else set(range(start, (node.end_lineno or start) + 1))
docs = set()
for n in ast.walk(cls):                       # docstring 不算代码
    if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) \
            and isinstance(n.value.value, str):
        docs |= spanned(n)
code = {l for n in ast.walk(cls) if isinstance(n, ast.stmt) for l in spanned(n)} - docs
sloc = sum(1 for l in code
           if lines[l - 1].strip() and not lines[l - 1].strip().startswith("#"))
```

这段数法**已实测**（`BridgeCore` → **147 / 11**，达标；`CommandHandler` 262/13、
`InboundGateway` 349/12、`EventStream` 449/19、`DiscordAdapter` 536/43，均超标）——
**跨类给出一致且合理的判定**，不是只对一个类调通。

⚠️ 别把 `{...}` 写成**节点**集合再去减行号集合 —— 那样**不报错地算错**，
一路错到 `lines[n - 1]` 才炸。**规则里的代码必须真跑过** —— 这段的第一版就是这么错的。

**数出来的结果要连口径一起写进 `tasks.md`** —— 只写数字的话，下一个人会换口径重数，
然后得出相反的结论（这条已踩过：旧记录「449 行」在 `core.py` 还是 1315 行时是真的，
后来文件缩小了它就过期了 —— **「错」与「过期」不是一回事**）。

**两种形态，优先第一种**：
1. **抽出去** —— 一整块职责连同其私有状态搬进新类，原类只留调用
   （首选：新类能独立测试，不依赖原类内部状态）
2. **拆基类** —— 仅当多个类共享同一批方法时才用；否则只是把胖类换成胖基类

**顺带判据**：新增功能时发现只能往某个大方法里塞 `if` 分支，或只能给大类加第 15 个方法
—— **该拆了，别硬塞**。

⚠️ **现实前提**：本仓库 `tasks.md`「类体量债务」一节记录着超阈值的类。
**新代码不得继续加厚这些大类** —— 新功能要么进新模块，要么先拆。

## 6. 先查参考项目，不要靠推测

> **能抄就抄，不要重复造轮子、从头踩坑。** 动手写第一行实现前先搜一遍。

### 6.1 （A）IM 平台/网关工程范式

| 项目 | 用途 |
|---|---|
| `NousResearch/hermes-agent`（MIT） | **首选**，250806 stars |
| `zhuiyueya/dsh-im-gateway`（MIT） | 次选，49 stars |
| `xmanrui/dsh-im`（MIT） | 用户认可的**插件实现参考**（源码 clone 在宿主临时目录 `ref-xmanrui-dsh-im`，git 仓非快照，引用一律锁 commit `b7698a6`（2026-10-15 14:28 +0800）；2023 stars、MIT、未归档、纯 JavaScript，均 2026-10-15 `gh` 现查；DeepSeek Harness 官方 Token 赞助、TrendShift 上榜）：cordis/Koishi 双侧插件（client=设置面板 / host=连接投递，渠道目录两侧镜像），README 内置渠道表实为 **12 个 IM 渠道** + AI Office Connector（原记「9 渠道」按源码更正：入口 `index.mjs` 渠道表 14 项 = 13 IM + office，README 表 12 行 = 含实验性 Matrix、不含 office）；每渠道多机器人、扫码 / App Manifest / 凭据三种接入、本地 Harness 主动外连。**借鉴面分析已做（2026-10-15，证据=README 章节选读 + `host/index.mjs` + `delivery-service.mjs` + `PROACTIVE_DELIVERY.md` 错误码/语义节 + `credential-binding.js` + `harness-connection.mjs`）**：⭐ 可借鉴面（**用户 2026-10-15 拍板：①②③ 全部进入插件工作排期**；落地跟踪见 tasks.md 未完成项总表「dsh-im 借鉴面落地」行）——① 入口只做装配：DI 全量可注入 + 渠道 `Promise.all` 单败仅记日志继续、全败才 `AggregateError`，可选子系统逐个 try/catch 降级；② `delivery-service.mjs` 统一错误码表 + 适配器契约 `ADAPTER_METHODS` 注册校验 + `sendChecked` TOCTOU 防护（目标路由 SHA-256 摘要比对 + 账号指纹 + 能力协商 + `beforeSend` 复检 + `AbortSignal.any` 热替换在途操作）；③ `PROACTIVE_DELIVERY.md` 投递目标 `botId+targetId` 不可变寻址、`{sent:true}` 不承诺送达、不生成幂等键不自动重试（调用方自管业务幂等）、回环默认 +「`trusted-host` 是可达性边界、不是用户认证」；⛔ 结构性不适用：扫码 / App Manifest 接入流程依赖各平台专属配对端点（飞书扫码建 bot、微信 iLink、QQ 扫码）——本桥目标平台均为静态 token；client/host 设置面板镜像依赖 Koishi console UI 框架，opencode V2 无对应物（现用 `/setup` 命令 + `bridge_setup` 工具）；**交叉印证（既有拍板的旁证）**：超时补发「明确失败≤3 次重试、结果不确定保留记录停止自动重试」与 fix-299 `outcome_unknown` 只告警不重放完全同构；Telegram 占位消息编辑式进度与本桥占位消息模型同形 |
| `MichengAI/dsh-im-connect`（Apache-2.0） | **与本桥领域高度同构的借鉴源**（2026-10-16 收录；59 stars、Apache-2.0、未归档、2026-10-15 仍在推送，均 `gh` 现查；源码 clone 在宿主临时目录 `ref-michengai-dsh-im-connect`，git 仓非快照，引用一律锁 commit `e271ec1`（v0.1.77））：把主流 IM 平台（dingtalk/discord/feishu/lark/qq/slack/telegram/wecom/weixin 共 9 家）接入本机 DeepSeek Harness 的 TypeScript / Koishi(cordis) 双侧插件，引擎域与本桥一一对应（去重 / 按会话串行 / 合并窗口 / 流式回复组装 / 长文切分 / 占位进度 / 审批与提问交互 / 会话注册表 / 原子落盘）。**借鉴点全目录 = `docs/borrowing-catalog.md` §1（A~N 逐条：来源文件 + 对本桥适用性 + ⛔ 结构性不适用清单）**；⭐ **用户 2026-10-16 拍板：A 投递三态+手动补投、B 冷历史恢复答复、C 结构感知切分、D 审批超时语义进入插件工作排期**（落地跟踪见 tasks.md 未完成项总表「dsh-im-connect 借鉴面落地」行）；**定期回看**见总表「参考项目定期回看」行与 catalog「定期回看机制」节（间隔一天一次，用户 2026-10-16 拍板） |

平台清单取两者并集（≈31 家），见 `docs/platform-design-reference.md`。全部收录项目的总目录与回看机制 = `docs/borrowing-catalog.md`（四项目锁 SHA + 借鉴点清单 + 定期回看机制与基线）。

**本机已有两者的全量纯文本快照**（含目录树 + 全部文件内容），文件名里的
`8a5edab282632443` 是 commit SHA，**锁定了参考版本** —— 引用结论时务必连 SHA 一起写，
否则"参考版本变了"就成了无法复现的借口。

⚠️ **按文件名全盘搜，不要猜路径**：`Get-ChildItem -Path D:\,C:\ -Recurse -Depth 3
-Filter '*-8a5edab282632443.txt'`。⛔ 绝对路径**不写进仓库**（§2.2）—— 但**不写路径
不等于让执行者去猜**：「下载目录的 `brower\` 子目录」这种说法是**不可执行的**，
猜错会触发一个指向**不存在**的目录的权限请求（**踩过两次**）。
**搜不到就在报告里写搜不到，不要申请目录权限。**

- `zhuiyueya-dsh-im-gateway-8a5edab282632443.txt`（622 KB）
- `nousresearch-hermes-agent-8a5edab282632443.txt`（**64 MB / 134 万行**）

⚠️ **一律用 `grep` 定位，绝不整读**（134 万行整读会占满上下文且大概率找不到）。
先 `grep -n '<符号名>'` 拿行号，再 `read` 带 `offset`/`limit`。
快照里可能含密钥（第三方仓库偶尔误提交）—— **只读不抄**。
结论若涉及"最新版本如何"，仍需 `gh` 现查并核对 SHA。

### 6.2 （B）opencode 服务端契约

⚠️ **（A）那两个项目都不消费 opencode 事件流**，不能当这部分依据：hermes 全库 grep
`/api/event` 零命中（它的 `/api/events` 是自己的 dashboard WebSocket），dsh 全库零
opencode 引用。按权威度查：

| 用途 | 去哪查 |
|---|---|
| **权威定义** | `anomalyco/opencode` 的 `packages/schema/src/event-manifest.ts`、`session-event.ts`、`session-status-event.ts`、`permission.ts`（后两个文件名 `gh` 核实过确实存在）|
| **机器可读** | 运行时 `GET /openapi.json`（含 `V2Event` 完整 `oneOf`；文档站 `/doc` 在 2.0.22 已不存在） |
| **架构语义** | `specs/v2/event-stream-architecture.md`（队列/编码/溢出） |
| **怎么消费** | `grinev/opencode-telegram-bot` → `src/opencode/v2/events.ts` |
| **delta 合并** | `openchamber` → `event-stream/delta-coalescer.js` |
| **真实抓帧** | `pingdotgg/t3code` → `testkit/fixtures/opencode2_*/*.ndjson` |

⚠️ **别抄 `dev` 分支**（已把 `session.*` 改名成 `session.next.*`）。本项目运行时是 2.0.22。
⚠️ **文档站对 v2 已过时**（`docs/server.mdx` 列的还是 v1 事件）—— 这正是推测的来源。
**读实现，不读文档。**

### 6.3 四条铁律（每条都来自一次判断错的代价）

1. **等待必须由终止事件驱动，不能靠超时猜。** `session.text.delta` 迟迟不来是正常的
   （模型在思考），超时后当"没内容"就会误判。终止事件只有
   `session.execution.succeeded`/`.failed`/`.interrupted`，且 `interrupted` 的
   `reason == "shutdown"` **不算结束**（重启会续跑这一轮）。

   ⚠️ `session.idle` / `session.status` 的准确说法（`gh` 核实，**别再写成「从不发布」**）：
   两者在 `packages/schema/src/session-status-event.ts` 里**都有定义**，`session.status`
   还**真被消费**（`packages/opencode/src/acp/event.ts` 有 `case "session.status":`）。
   但它们被 `V1_API_MIGRATION.md` 列为 **transitional dependency、计划移除**，而
   **2.0.22 运行时我们从未观测到它们**。结论不变：**别拿它们当「一轮结束」的信号**，
   否则**永远等不到** —— 依据是「运行时没观测到 + 上游计划移除」，不是「协议里没有」。
2. **正文要合并后再发 —— 但本仓库的合并机制与这句话字面不同，别照字面去「修」。**
   delta 极度碎片化（实测 5.7 KB 回答 = 3402 个 delta，每个 1.7 字符），不合并就会让
   一条消息变几千帧。
   ⚠️ **但本仓库既没有时间窗合并、也没有大小窗合并**：`Turn.assemble` 是**按 ordinal
   分桶**（键为 `(assistantMessageID, ordinal)`）；而 `DEFAULT_EDIT_INTERVAL`
   **只约束已组装好的正文被改写的频率**，它**不是**这条的旋钮。
   长度阈值那一路也不是「窗口」：`event_stream.py` 的门是**超限就不发不改**这一轮，
   留给收尾整段发出（由适配器自己切分）。
   ⛔ **所以靠调 `1.5` 去「实现」这条铁律是找错了地方** —— 我自己派单时正是这么写的，
   被实现者驳回。
3. **⚠️ 上游有「权威全文」事件，但本仓库刻意不用 —— 这条不是对现有代码的描述。**
   `gh search code '"session.text.ended"' --repo anomalyco/opencode` **确实命中**
   （`packages/app/src/context/server-session-v2-reducer.ts` 里有 `case "session.text.ended":`），
   上游 schema 的注释正是本条的理由：
   `// Stream fragments are live-only; Text.Ended is the replayable full-value boundary.`
   ——**delta 是 live-only，`Text.Ended` 是可重放的全量边界。实质成立。**

   但要分清三件事：① `dev` 分支已改名 **`session.next.text.ended`**（见上方铁律区的
   「别抄 dev」）；② 本项目运行时是 **2.0.22**，该版本到底发哪个名字**未核实**；
   ③ **本仓库零实现** —— `grep -rn 'text\.ended' .`（含 tests）**零命中**，
   正文由 `event_stream.py` 的 `Turn.assemble` 从 delta 组装，并有跨 13 个平台的
   逐字节重组测试。

   ⛔ **所以：不要为了"符合这条铁律"去改成读那个事件。** 那会删掉或绕过一份
   正在通过的、有意的契约，去对齐一个**在本仓库从未出现过、且版本名未核实**的事件。
   真要改，先用 `gh` 核到 2.0.22 的确切事件名与载荷形状，再动。
4. **未知事件不许静默丢弃。** `if handler is None: return` 会让"事件名写错"这类错误
   在生产里表现为**完全无迹可循**。至少打一行日志。

### 6.4 怎么用

抄到就照抄，不要凭理解重写；抄完在进度日志里记一句差异与原因；抄不到才自己实现并
说明"查过什么、没查到"。**抄来的契约仍要用真实环境验证**（参考项目可能对着不同版本）。

## 7. `edit` 工具纪律

`edit` 要求 `oldString` 与磁盘内容**逐字节一致**，否则报
`Could not find oldString ... must match exactly`。**这个报错读起来像"内容写错了"，
实际绝大多数是重建失真。**

1. **先 `read`，再 `edit`** —— `oldString` 必须逐字节从 `read` 输出复制，禁止凭记忆补写。
2. 缩进按 `read` 里的实际列数抄，**只用空格**；数清嵌套层级。
3. **空行必须原样保留**（`read` 把空行渲染成行号+尾随空格，转录时最易丢）。
4. `oldString` 用 **3–8 行最小锚点**，不跨文件头 docstring、不跨整段 `class`/`def`。
5. **同一文件一轮只发一次 `edit`**；多处改动分轮串行，禁止在一个批次里对同一文件
   并发多个 `edit`（后发的会因前一个已改而失效）。
6. **失败后必须先 `read` 再重试**。连续两次失败禁止第三次盲猜——第二次仍失败说明是
   理解偏差，不是字符差异。
7. **多文件并行改动时，跨文件改完一轮后，任何已改文件的 `oldString` 一律视为失效快照**，
   必须重读。subagent 落盘后同样必须重读。
8. 怀疑改动可能已落地时先 `git status` + `git diff` 确认；`newString` 已存在说明已应用，
   不要下发同一 edit。
9. ⚠️ **`newString` 第一行的缩进是相对「匹配起点」的，不是相对行首。**
   `oldString` 若从**行中间**开始（前面那段空白没写进去），磁盘上原有的空白会**留在它前面**
   ⇒ 此时 `newString` 首行**绝不能再带缩进**，否则会变成「原有空白 + 你写的缩进」。
   实战：一条 lane 新建文件后连错两次，第三次 `edit` 报成功，
   而被替换那一行**保留了磁盘上原有的 4 空格** ⇒ `class` 那行留在 4 空格缩进里，
   文件当场 `IndentationError`。**是 `compileall` 抓到的，不是测试** ——
   那一刻那个文件还没进测试范围。
   ⇒ **要改一行的缩进，就必须把该行开头的空白也写进 `oldString`**（让它从列 0 起匹配）。

**排查口径**（按序）：① 缩进列数——最常见，逐列比对别凭观感；② 空行是否被合并；
③ 文件是否已被改过；④ 编码 —— 本仓库由 `.gitattributes`（`* text=auto eol=lf`）
统一 LF，`install.ps1` / `plugin/install.ps1` 的 UTF-8 BOM 是**故意保留**的
（Windows PowerShell 5.1 读中文需要它），不要为了"统一"而删。

### 7.1 复核纪律：**空集 ≠ 不存在**

> 一次实测：连续**八次**把自己的复核脚本写错，而**被复核的代码每一次都是对的**。

| 症状 | 真相 |
|---|---|
| `hasattr(o, "offer")` 找到 0 处 | 方法叫 `ingest` —— **匹配器坏了，不是目标不在** |
| `ast.literal_eval` 取模块常量得 `None` | 写的是 `max_message_length = MESSAGE_LIMIT` —— **名字要追到常量** |
| 造的「歧义」样本测保守分支 | 我把 `shown` 放在开头，于是它是**前缀** —— 测的是最不歧义的情形 |
| 断言「只增不删」失败 | **改写 docstring 本就会删掉一行** —— 该看删的是什么，不是有没有删 |
| 扫描 `ClassDef` 的直接子节点找不到函数 | 函数被装饰器/嵌套包着 —— **空集被当成了「不存在」** |
| 「两条 lane 的断言对不上，我要裁决」 | **冲突可能从两个方向各自化解** —— 一条修根因、另一条根本没触发。**落地前裁决 = 凭空造一条必须执行的指令** |
| 把自己当时的**证据不足**写成一条**绝对规则** | 同一个动作犯了两次：「45% 这条线是算出来的」（其实当时没有任何阈值能产生那个断点）→「触发句一律不算下一步」（其实「平台数超过 13」是**可机械判定**的正当触发）。⇒ **判据要写「缺什么」而不是「一律不许」**。⚠️ **但本条只针对依据是【证据】的规则**（阈值、口径、适用范围、判据覆盖面）。
| 把自查脚本的判据写成「**我以为会看到的样子**」 | ⚠️⚠️ **2026-10-07 实测：同一道闸门错了四版，而四次是同一个病根**：
　① 查字面 `⇒ **判据（可复核）**` ⇒ 新写的句子加了词就被判成「**0 = 失败**」⇒ **判据比被审对象还窄**；
　② 查「`tasks.md` 出现连续空行」⇒ 而 HEAD 里**本来就有 7 处**⇒ **绝对判据用在了增量问题上**；
　③ 查「那句错误的归属字面必须为 0」⇒ 而**更正说明本身要引述错误原文**⇒ 把「引述」当成了「残留」；
　④ 把方向写成「工作树 < HEAD 则失败」⇒ 而 AGENTS.md 是 `1 → 0`（**清干净了**）⇒ 却被报成「**反而变多了**」⇒ 判据**方向**写反。
⇒ ⭐ **判据要问「什么算好」，不是「我以为会发生什么」** —— 前者总能写成**性质**（每处出现都带更正标记 · 裸的为 0 · 只许不增），后者只能对着**字面**。
⇒ ⭐⭐ **而 2026-10-07 那条缺陷的收尾给出一条可复用判据（「四判据」扩到六条）——**
　它治的是**「把『可重放的状态』与『请求已经发出』凑到了一起」**（两者单看都安全，凑一起就双跑）：
　① 凡落 `failed` 的路径，`reason` 必须含 status 线索（**AST 守门**）—— 它保证**可诊断**，
　　⛔ **不是**分档依据（分档依据在 `state` 本身）；
　② 无 status 且**未被显式标「压根没提交」** ⇒ 落「结果未知」，**只告警不重放**；
　③ 「结果未知」**不碰重试预算**（本档没有「下次」）⇒ 用它量预算的用例会量到另一件事上；
　④ 那档的告警**必须**带「请重新发送一次」⇒ **只告警不给方法 = 把负担转给用户**；
　⑤ ⭐ **记不下「进行中」就不投** —— 恢复路径必须与实况路径**同为 fail-closed**，
　　⚠️ 而「单靠那一次写失败并不危险」：**是它把「行留在可重放状态」与「请求已发出」凑到了一起**；
　⑥ ⭐ **盖「可重放」戳的分支必须逐个枚举，缺省留给「不知道」那一侧**
　　⇒ 形状守门：**判定里不许有否定式条件**（`!= "ok"` 这种 ⇒ 新增一个返回值就落错档）。
　⇒ ⚠️ 而 ⑥ 是**实测出来的**：新增一个返回值、作者没改判定 ⇒ 落进 `!= "ok"` 那支
　　⇒ **没有任何行为用例会红**（既有用例在那条路径一条都不经过）⇒ **只有形状守门能抓**。
⇒ ⚠️ 而它与下面那行（把自己证据不足写成绝对规则）是**同一个病**的两种表现 ⇒ ⛔ 所以自查脚本**第一版不许直接用**：先让它**报出命中数与原文**，人读过再让它**拦住**。 |
　⚠️⚠️ **而 2026-10-07 深夜实测：守卫（决定**该不该写盘**）又错了四版，而那四次比上面四次更可迁移** ——
　⑤ **守卫查「某个词还在不在」** ⇒ 而**新替换文本会合法引用那个词**⇒ 把「闭合说明」当「残留」⇒ 守卫假报会**阻止一次正确的改动**；
　⑥ **守卫数「整段多行文本里的 `|`」** ⇒ 而表格行的结构**只由首个物理行决定**⇒ **数错了对象**；
　⑦ **守卫用 `count("|") - 1` 算格数** ⇒ 那个公式**只对「带尾随 `|`」的写法成立**⇒ 而判据的解析是 `strip().strip("|").split("|")`；
　⑧ **守卫查 `"\| ~~…"`** ⇒ 而那一行的**首字符就是 `~~`**⇒ 那个 `| ` 是 **unified diff 的上下文标记**、**不是内容** ⇒ 永远匹配不上。
⇒ ⭐⭐ **而这四次的病根比上面四次更具体**：⛔ **凭「输出大概长什么样」写断言，而不是从实际输出里抄**。
⇒ ⭐ **动作（顺序不能反）**：**① 先打实际值** → **② 从那个值里抄** → **③ 才写断言** ⇒ 而「我以为」只能出现在第 ③ 步的**期望值**里，⛔ 不能出现在**前缀**里。
⇒ ⭐ 而校验**形状**而非**标识符**：首行是否还在 · 行数 · 格数 · 是否以换行结尾
　（今晚实测：拿「某个标识符还在不在」当守卫 ⇒ **四次里四次误报**）。
**禁令类**规则（§2.4 的 unblock secret、§3 的不许编造、§7.1 的未知事件不许静默丢弃）**不适用** —— 它们没有「缺什么」，它们是决策。
⛔ **别把禁令改写成带条件的**：那会把一条永久规则降格成看起来可协商的规则，而「什么情况下可以 unblock」的答案必须是「没有」 |
| 给「正在被并行 lane 编辑的文件」写下 `file:line` | **写下的那一刻就开始过期** —— 实测同一处引用在几分钟内漂了 13 行，指向了完全无关的函数。**这类引用要用符号名（函数/常量名）**，符号不会因为插入行而失效 |
| 断言「这条路一条测试都没有」 | 实测有 6 条，但**测的是别的东西** —— 准确说法是「测了 A、没测 B」，而缺陷通常就活在 A 与 B 之间那条缝里。**说「没有」之前先数一遍** |
| 断言「这个文件塞了 N 类关注点」 | 我据以裁定「测试文件要纳入 §5」的那条举证：`pairing\|health\|command` 不区分大小写**只有 2 处命中**，而 24 个类全是同一个协议 ⇒ **一数就破**。⇒ **主张混了 N 类职责时，公开你用来判定每一类的检索词，并把每类的命中数贴出来**。
⚠️ **数是 0 也要贴** —— 「一个词都没命中」是**重要信息**，不是主张被否。
⚠️ **命中数只证明「词在不在」，不证明「职责在不在」**：两个关注点可以共用一个词（`TestSend`/`TestEdit` 属同一词族），
一个关注点也可以没有自己的词（`allowlist.py` 管访问控制，代码里不叫「配对」）⇒ **命中数与主张不符时，不要就地改数，
要改主张或换检索词并说明映射** —— 「改数让它对上」是这条判据要防的第一件事⚠️ 这是 §7.1 第一条（空集）的**另一半**：「没找到别下结论」之外，**「找到了也别下结论」** |
| `git status --porcelain` 的输出先 `.strip()` 再切 `l[3:]` | **状态位被吃掉了** —— 首行 ` M AGENTS.md` 变成 `M AGENTS.md` ⇒ 切出 `GENTS.md`，于是**「这是我的改动」被判成「别人的在制品」**。实测真发生过一次。**别 `.strip()` 整个输出**，用 `.splitlines()` 或先 `.rstrip("\n")` |
| `print("45% 且 SLOC %s" % v)` | `ValueError: unsupported format character` —— **字面量里的 `%` 被当成格式符**。本项目到处是百分比（文档占比、覆盖率），这条会反复踩。**用字符串拼接或 f-string，别用 `%` 格式化含 `%` 的字面量** |
| 用正则从源码里提取「CLI 有哪些 flag / 参数」，结果返回**空集** | 正则**匹配不到跨行**的实参、漏了前缀限定，**一个都没匹配上** ⇒ 我据此认定 `--verbose` 是子代理**编造的 flag**，差点从文档里删掉。跑 `python -m opencode_bridge --help` 才定案：**它真实存在**（`build_parser` 里 `"--verbose", action="store_true"`，`main()` 里 `_setup_logging("DEBUG" if args.verbose else cfg.log_level)`）。⇒ **凡是「程序对外暴露的命令面」（flag、命令行、配置键），权威来源是**跑一次它自己**（`--help`、实际调用），**不是正则扫源码**。§7.1 第一条规则在这里的代价是**删掉一个真实存在的功能** |
| 用**行级**正则去找一个**跨行**的表达式（如 `int(\n    self.config.get("k") or D\n)`） | **行级正则必然漏** —— `int(` 与 `self.config.get(` 不在同一行。实测我据此判定「代码里没有裸 `int()`」，实际有**两处**，而那两处正是**会打死整个适配器**的缺陷。⇒ **知道确切符号名时，直接按名字 `grep` 那个键**，别写通用扫描；真要通用扫描就上 AST，但 **`ast.walk` 会吐出没有位置信息的节点**（`ast.arguments` 等），**别在它的结果上直接取 `.lineno`** |
| 拿【某处一份拷贝】的内容，去判断【运行中的进程】跑的是哪份代码 | 桥同时存在**两份**同名代码：npm 插件缓存（`plugin update` 刷它）与 bridge 目录里那份 git clone（桥以 `python -m opencode_bridge` + cwd = bridge 目录启动 ⇒ **import 的是后者**）。我查缓存快照里的标记，五个全中 ⇒ 报了「桥已更新到今晚的代码」—— **结论对、对象错**：缓存确实变了，而运行中的桥落后 **129 个 commit**，自 10-03 23:06 起就没再被同步（≈68 小时），而我发现时又过了 5 小时。⇒ **判据必须问运行中的那一份**：`cd <bridge目录>` 后 `python -c "import opencode_bridge, os; print(os.path.abspath(opencode_bridge.__file__))"`。⚠️ 这是本表的**第三种**失效：匹配器没坏、目标存在、命中也是真的 —— **命中的是另一份同名的东西**。前两种是「没找到」与「找到了别急着下结论」，这一种是「找到的那份不是它」⇒ 症状与正确做法完全一样，所以它不会触发本表任何一条已有规则 |
| 拿一个**形状**当泄漏判据，而那个形状**正是规则规定的补救形式本身** | 脱敏扫描把 `C:\Users\` / `/home/<段>` 当泄漏形状 ⇒ 它把 `C:\Users\example-user\`（§2.2 钦定的占位符）、`C:\Users\<你的用户名>`、一字母假用户 `C:\Users\x` 全判成泄漏。实测它报的 13 处里**只有 4 行是真的**（另 9 是同一行被两个 pattern 各数一次的重复计数），7 行是误报。⇒ **照它去改的代价是实测的**：① 把一条事故记录改反了 —— 原文「曾把**真**用户名写进字面量」，改成 `%USERPROFILE%` 后变成「把 `%USERPROFILE%` 写进了字面量」，而那**正是当时没被写进去的东西**；② 新注释把「必须拆分」归给 §2.4，而 §2.4 只讲**凭据形状**的拼接、**没要求拆用户目录** ⇒ 等于给一条不存在的规定安了个出处。⇒ **判据要判「这台机器上的真值」，不是「某个形状」**：`os.environ["USERNAME"]` / `%USERPROFILE%` / 仓库根，**真值一律运行时取**，⛔ 不写死进脚本 |
| 用**行号**比对两个版本，找「新引入的」 | 插入任何一行都让后面所有行号移位 ⇒ 两条早已在 `origin/main` 的历史遗留被判成「新引入」。⚠️ 与上一条同型：**都是拿一个看起来精确、实则不成立的量当判据**。⇒ **比内容，不比行号** |

**四条规则：**

1. **报 0 命中时，先怀疑匹配器，再怀疑目标。** 判据：这条判据在**改动之前**跑过吗？
   改动之后突然 0 命中，而代码没被大改 —— 那是**判据坏了**。
2. **同一条轴上连错三次，就停下换方法。** 第 1 次是意外，第 2 次是巧合，第 3 次说明
   **方法本身选错了**，继续在同一轴上试只是换一种错法。
3. **⛔ 不要在相关 lane 还没落地时，裁决它们之间的冲突。** 先问「这个冲突**真的存在**吗」，
   办法是去读那条测试**现在**断言什么、以及它**为什么**通过 —— 不是推演它们落地后会怎样。
   **撤回一条写进规则/台账的错误裁决，比从未写过更费事**：它已经进了版本历史，
   下一个人读到的是一条看起来权威的指令。
4. **报命中时，同样先怀疑判据的宽度** —— 「0 命中」与「误报」是**同一个错误的两个方向**，
   而第 1 条只治了前一个。共用一条判据：**这条判据在改动之前跑过吗？它那时是否已经在误报？**
   ⚠️ **误报比漏报贵**：漏报只是漏掉一处；**误报会制造工作**，而本项目实测按误报的判据
   去改，13 处里 **7 处是无用改动**，其中 **2 处比不改更坏**（把一条事故记录改反、
   给一条不存在的规定安上出处）⇒ 那是**净负**的编辑。
   ⇒ **动手改之前先问：这一处命中的是「这台机器上的真值」，还是「某个形状」？**
   形状相同而真值不同的一律不算泄漏 —— **占位符**（§2.2 钦定 `example-user` 这类）、
   **假用户**（一字母 `x` / `y`）、**协议字段名**，以及**裸盘符**（`D:\` 谁都有）。
5. **⚠️ 判据与探针【本身】也是需要被审的对象。** 第 1 与第 4 条治的是「一次判据说错了」，
   而这一条治的是**「我一直拿它当可信的东西」**。
   **2026-10-06 实测**：§4.1 纪律 ③ 的对齐判据（两处未完成项清单必须逐行相等）连改**六版**才对，
   而六版**全是「报少 / 报 0」同一个方向** ——
   ① 忘了剥 markdown 装饰（`**`、反引号）② 把**字符偏移**当行号（算出 L101585，而文件只有 5495 行）
   ③ 扫描**不设上界** ⇒「在表内吗」**恒真** ⇒ 我差点采信「位置正确、无需移动」，
   而那行其实在文件末尾 ④ 固定行数切片 ⇒ 段落加长后静默截断
   ⑤ **只归一化干草堆、不归一化针** ⇒ 任何 key 带空格的项必然报「两侧都没有」
   ⇒ 而那 20 条能对上**纯属侥幸**（它们的 key 恰好无空格）。
   ⇒ **判据本身不在任何人的审阅范围内**，所以它坏了没有任何东西会报警。
   ⇒ **动作**：判据每被改一版，都要问「**它这次会把哪一类真项误判成没有**」——
   ⚠️ 只问「它现在对不对」**不够**：一个恒真 / 恒空的判据**看起来永远是对的**。
   ⛔ **还有一条同型的**：「我早就知道该怎么做」**不构成**「我会这么做」的证据** ——
   同一晚 `edit` 工具吞掉 `path` 参数**八次**，而「改用 Python 替换脚本」这条结论
   我在写这条规则的前一分钟就又违反了一次。

| 判据报出的「应列 N」是**手维护的清单长度**，而它同时**真的读了那个文件** | **2026-10-07 实测**：§4.1 纪律③ 的对齐判据里 `清单应列 = len(PAIRS)` —— 而 `PAIRS` 是一个**手写的 23 条 `(全名, key)` 列表**，于是那个数**按构造是常量**；可它又真的用 `in_list = key in tail` 读 `AGENTS.md` ⇒ **「它读文件」与「它报的数是常量」同时成立** ⇒ 那个数看起来像是量出来的。⚠️ **代价**：我连改**三轮** `AGENTS.md`（把闭合项写成 `~~…~~` → 降为散文 → 改项名），而 `清单应列` **纹丝不动** ⇒ 我据此判定「改法不对」，⛔ 却没去看它怎么算的。⇒ **更糟的是我连错三次的处置**：§7.1 第 2 条写的是「同一条轴上连错三次就停下**换方法**」，而我换了**内容**（散文怎么写、项名怎么改）、**没换方法**（去读判据源码）—— **换了内容不等于换了方法**。⚠️ **三轮失败的原因各不相同，且**都**是我的错**：① 我把闭合项写成 `~~…~~` ⇒ 而 `normalize()` 会**剥掉 `~~`** ⇒ 判据**看不见删除线**⇒ 它照旧报「清单里有」；② 我从散文里彻底删掉那三条 ⇒ 但 `PAIRS` 里**还有**这三条⇒ 于是变成「两侧都没有」—— 我不知道那份清单要手工同步；③ 键里带了行数（`生产侧§5拆分23个文件`）⇒ 我改了项名 23→22 就断，⛔ 而它断时报的是「**匹配器或内容要再查**」，指向完全错误的方向。⇒ **三条动作**：
① 看到一个数先问「它是**量**出来的，还是**数**出来的」—— 判据里的**列表字面量**与**它读的文件**是两种东西，前者**必须手工同步**；
② **手工同步的东西必须在打印时自曝** ⇒ 已让那一行打印 `⚠️【这一项是 PAIRS 列表的长度，是【手维护的常量】不是量出来的】`；
③ **键只取「身份」，⛔ 不许带会变的数**（行数是**数据**）⇒ 已改成 `生产侧§5拆分` |

| 自查脚本**打印**了结果，却没有**拦住**动作 | **2026-10-07 实测**：我给提交消息写的自查脚本打印出 `U+FFFD = 3`（消息里有 3 个替换字符），而我**接着就提交并推送了** ⇒ 那 3 个字符进了 git 历史。⚠️ **检查逻辑是对的** —— 它准确报出了那个数字 ⇒ 失效的不是判据，是**判据的结论没有接到动作上**。⇒ **与「没做自查」在后果上没有区别**：一个只观察、不阻断的检查等于没有检查。⚠️ **同型且更早的一次**：今晚第一次落 `AGENTS.md` 时，脚本打印了 `复查 … = False`（那次是**我的检查串写错**），我改了文件却**没把那条 False 当成要处理的东西** —— 它被我读到了、然后被忽略了。
⇒ **动作（三条，缺一条就退回原样）**：
① 自查脚本的结论必须是**前置条件**而不是输出 —— 要么脚本自己 `raise SystemExit(1)`，要么在 shell 里**判断退出码**再决定是否提交；
② 凡是以「复查 / 确认 / 校验 / 自查」开头的**输出行**，都必须能指出「不通过时怎么办」—— 说不出来 ⇒ 它只是装饰；
③ ⛔ **不要把「脚本打印了」当成「我检查过了」** —— 唯一算检查过的，是**它拦住过一次东西**（§7.1 第 5 条同族：「我早就知道该怎么做」不构成「我会这么做」的证据） |

| 判据的**解析比被审对象更宽松** ⇒ 它绿灯，而对象其实已经坏了 | **2026-10-07 实测**：对齐判据的解析是 `line.strip().strip("|").split("|")` ⇒ **先把两端的 `|` 剥掉** ⇒ 所以「表格某一行**开头缺 `|`**」它照样解析成功。⇒ 而 markdown 里一行**不以 `|` 开头就不属于那张表**（渲染成普通段落）⇒ 实测**8 行**这么坏了、跨 **5 个 commit**，**判据一次都没报过**。⚠️ 成因不在 markdown、在我那个**改表格的助手**：`cells[0] = "~~" + cells[0].lstrip("| ").strip() + "~~ |"` 把**前导 `|` 一起剥了**、拼回去时**没补**。⚠️ **同一个助手当晚还犯过一次同型的**：忘了补回**行尾的换行** ⇒ 一行与下一行被粘成一行（那一次**数目不等**，判据抓住了）。
⇒ **两条动作**：
① **判据的解析必须与被审对象的语法一样严格** —— ⛔ 凡是**先把分隔符剥掉**再切分的判据，就**看不见分隔符本身坏掉** ⇒ **动作**：判据要**额外**查一次「每行是否以该分隔符开头」。
② ⚠️ **同一个变换助手在一晚上错两次、而两次的错法不同** ⇒ **助手本身要受审**：凡是要**重写整行**的助手，⛔ 不许只测「内容对不对」，**必须**测「那行还是不是那一行的形状」（前导分隔符、行尾换行、格数）⇒ 而这三条**都可以机械判定** |

⚠️ **为什么这条要单列**：§9 讲了「恒真的断言比没有断言更危险」，但那是**被测代码**恒真；
这里是**判据本身**量的是一个空集，于是**结论看起来干净、方向却是反的**。
一个恒空的检查比一个恒真的更隐蔽 —— 恒真的至少和真值同向。

| 同一个对象在**同一小节**里出现了**两个相反结论** | 实测：§5.0 一处说某文件「翻成豁免」、一处说它「从豁免变成必须拆」、另两处说它「至今仍豁免」，而**两个错数在树上都不存在**。⇒ **任何按该小节自己算的读者都会得到与文件里一半文字相反的答案。**
⚠️ **豁免：同一对象出现两个数，若各自标明了口径，不算相反结论**（「含裸空行 48.5%」/「剔裸空行 42.5%」—— 那正是「口径没定义有代价」的证据）。
⛔ **不标口径的第二个值才是相反结论** —— 那才是读者无法判断该信哪个的情况。
⇒ **动作**：写下结论前，把本小节里所有提到它的句子找齐 |
| 用**计时**（`time.monotonic()` / `sleep`）来判定并发交错是否发生 | ⚠️⚠️ **2026-10-07 收窄过一次 —— 而收窄的方向是【原规则过宽】**：原写「本机 `time.monotonic()` 16 ms ⇒ **计时判据一律不可信**」⇒ 那条实测**只覆盖无 I/O 紧循环**，外推成「任何时刻度都是 16 ms」那一步**过宽**。
**编排者复核的三组数（同一线程、无竞态）**：
　① 无 I/O 紧循环 20 万次 ⇒ **2** 个不同值 ⇒ 原文记的「2 个」**是对的**；
　② `Event.wait(x)` 实测回来：`0.001→0.016`（+0.015）· `0.006→0.016`（+0.010）· `0.02→0.031`（+0.011）· `0.05→0.063`（+0.013）· `0.2→0.203`（+0.003）· `1.0→1.000`（+0.000）
　③ 「等待会**变短**」实测 **一次都没有**（120 次采样）
⇒ **被量化的是「睡多久」，不是「钟怎么读」** ⇒ **等待只会变长、不会变短**
⇒ ⭐ **所以【下界断言】（`gap >= X`）免疫机器负载** —— 量化与调度都只朝「更慢」方向动，而那只会让下界**更容易过**；
⚠️ **而【上界断言】（`max(gaps) < X`）没有这个豁免** —— 量化会让它**更容易红** ⇒ 那类必须换成同步点或等值断言。
⚠️ **为什么这条收窄必须做**：§7.1 第 4 条写着**误报制造无用工作**（本仓库实测 13 处里 **7 处**是无用改动、其中 2 处比不改更坏）⇒ 而**不**收窄的代价 = 那 **16 处下界判据**会被当成「不可信」而白改。
⚠️ **仍然禁止的**（收窄的是【适用范围】，不是【这条纪律】）：⛔ 不用 `sleep`；⛔ `Event.wait()` 的超时只能当**死锁守卫**（要 fail 得响亮）—— 而「被 `set()` 唤醒」那条路径（见 `request_stop`）是**有意**的，⛔ 不要拿它当时序断言。⇒ 同步点仍用 `Event` / `Barrier` / **离散顺序日志**。
⚠️ **同族**：编排者自己派单时也写过同一处过宽外推（「本机计时分辨率 16 ms ⇒ 并发测试只能用同步点」）⇒ **派单里的判据也要受审** |

**所以承重的复核只做三件事**：跑**我自己的**全量（不是采信对方的数）、**从源码重测**
关键数字、以及**每个引用的 `file:line` 都确认存在**。其余靠证据链，不靠再写一个脚本。

## 8. 方案有疑问时，一律选能根治的那个

> 几个方案拿不准、或每个都有代价时，**不要**挑"改动最小/代价最小"的，
> **挑从根上解决的那个**。**之前定过的结论可以被推翻** —— 判断依据变了就该改，
> 推翻时把依据一并写下来。

**为什么**：挑代价小的方案会把根因留在原地，同一个问题隔一阵就回来，而且每次都要
重做一遍当时的权衡；更糟的是这类方案**当下看不出坏处**。本仓库 G5 的 `channel:`
前缀先后否掉三个方案，理由相同——它们都在**恢复一份从来没记录过的信息**（平台归属
从未落盘），也就是猜。

**怎么判**：

1. **先问根因是什么**，而不是"哪个方案改动小"。根因通常是"某件事本来就不该发生"
   （往重载的键空间写 / 靠超时猜一轮结束 / 关键状态不落盘），修掉它，一类问题一起消失。
2. **区分症状和根因**：做起来快、效果看起来一样、换个输入还会回来的，是症状。
3. **警惕"恢复没记录过的信息"**：若某方案的正确性依赖"某件事以前发生过/没发生过"，
   而这件事在数据里**没有被记录**，那它**必然**在某些情况下猜错 —— 加警告、加配置项
   都救不了，缺的是信息本身。这类方案直接否掉。
4. **核对代价的种类**：根治方案的代价若是"多一处不统一 / 多一个配置项 / 多写一个模块"，
   而症状方案的代价是"用户会**静默**地遇到错的东西"，选根治。
5. ⛔ **不要要求代码区分它唯一的输入无法区分的两种情况。**
   实测踩过：我一边裁定「把免责半句扩成一个**无条件生效**的字符串常量」，
   一边要求「正常无失败时那句**不许**带免责」—— 而那个视图是独立进程、**只读盘**
   ⇒ 两种情况的输出**逐字节相同**，它**根本没有办法区分**。
   ⇒ 我那半句要求等于要它**猜**，而它唯一能做的猜就是「两个都带」或「两个都不带」。
   ⇒ 要真的压制其中一种，得改**机制**（比如把「写盘失败过」也落进那份文件），
   ⛔ 而那正是第 3 条禁止的「恢复没记录过的信息」。

   **正确处置**（三条，缺一条就会退回去）：
   ① 按**无条件**版本实现（诚实的覆盖面），不为了满足措辞而加机制；
   ② **另加一条测试把前提本身钉住** —— 断言「这两种情况逐字节相同，
   所以没有措辞能分开它们」。这样一旦有人真去记了写盘失败（前提被改），测试会红；
   ③ 在报告里**明说**「要压制正常态那句需要真机制改动」，⛔ 不默默照做。

**例外**（只有这三类允许选非根治）：用户已明确决定的硬约束；不可逆破坏（改写历史等）；
信息确实不可恢复且无解 —— 此时"不猜错"本身就是目标，但**必须把这个残留明确记下来，
不许假装解决了**。

**记录要求**：推翻旧结论时，把**当初为什么那么定**和**现在为什么改**一起写进
`tasks.md`，否则下一个人会把同样的坑再踩一遍。

## 9. 后台子代理：怎么判断它**停滞**了

> 「定时检查子代理状态」的正确形式**不是轮询** —— 状态查询本项目已证明几乎无用。

### ⛔ 状态查询不可信

同一次检查里三个来源可以互相矛盾，**矛盾时活着的那个才是真的**：后台任务板说
「已对账/可复用」是账本假象；`task_status` 报 `Unknown task ID` 时任务可能正在跑；
**`task_result` 说 `running` 且无终态文本才是可信的**。反复调用这三个工具不产生信息。

### ✅ 判停滞看**进程 CPU 时间**

真在跑测试会烧 CPU；**CPU 时间持续为 0 而墙钟在涨 = 阻塞，不是计算**，且不会自愈。

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'unittest|pytest' } |   # ⛔ 必须过滤，见下
  ForEach-Object { [pscustomobject]@{
      pid=$_.ProcessId
      分钟=[math]::Round(((Get-Date) - $_.CreationDate).TotalMinutes,1)
      CPU秒=[math]::Round($_.UserModeTime/10000000.0,1) } } |
  Where-Object { $_.CPU秒 -lt 5 -and $_.分钟 -gt 5 }   # 分钟在涨而 CPU 不涨 = 停滞
```

⛔ **那个 `CommandLine` 过滤是必需的，漏掉它这条判据就废了。** 只比 CPU 与墙钟
**不区分「卡住的任务」和「空闲的服务」**：本机常驻着桥实例、argo-search 的 MCP server
等好几个长期空闲的 python（实测 CPU 0 秒 / 墙钟 700~1700 分钟），**全都会被报成「停滞」** ——
一次就出了 5 个假阳性。**判据必须说清「在干什么」，否则它只是把空闲当卡住。**
同理，**只读任务（explorer）本就不该有落盘**，所以落盘判据对它无效，别拿「无落盘」判它停滞。

**「还在落盘」是比任何状态查询都硬的活性证据** —— 文件持续被写入就说明它在产出。

⚠️ **但「落盘」必须连 gitignore 的目录一起看。** 子代理按本规则把临时脚本放在
`.tmp/`（gitignored），所以 `git status` 干净 **不等于** 它没在干活。
踩过：据此判「没在产出」**错了两次**——而它正在 `.tmp/prof/` 里跑 `cProfile`，
时间戳就在十几秒前。

```powershell
Get-ChildItem -Recurse -File -Path opencode_bridge, tests, .tmp |
  Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-10) } |
  Sort-Object LastWriteTime -Descending | Select-Object -First 10
```

**这条判据与本规则给子代理的指令是配套的**：既然要求它们用 `.tmp/`，
判断它们是否在动就**必须**看 `.tmp/`。用 `git status` 判断活性 =
和自己的指令矛盾。

### ⛔ 顺序纪律：先取消并确认，再考虑动进程

`task_cancel` 返回 `best-effort/uncertain … no tracked generation` 就是**没取消掉**。
此时去杀进程等于**在一个活着的任务背后动手**。正确顺序：`task_cancel` → **读返回值** →
确认工作树（非空洞 harness 中途挂住时**不会还原文件**）→ **只有确认任务真死了**才动进程。

### ⛔ 不要与在跑的 fixer 并发跑全量

它正在编辑同一批文件时你跑全量，得到的失败是**它正在实现中的新测试**，不是回归。
并发既污染证据又拖慢双方（全量实测从 230s 涨到 407s）。
**要独立复核就等它落地并交付报告之后再跑** ——「树是红的」在对方正在改时什么都不能证明。

### ⛔ 有 lane 在跑时，**动任何文件之前先查 `git stash list`**

> 实测踩过：一条 lane 为了量 BASE 执行了 `git stash -u`，把整棵树收进
> `stash@{0}`（它自己命名的 `pairing-lane-verify`）。我在它 stash 之后**继续在
> 那批文件上编辑**——字全部写在一份**即将被恢复覆盖的树**上，几分钟后一次
> `git checkout .` 把它们抹平。而 `git status` 当时**报 0 行、看着像"树干净"**。

**三条规则：**

1. **有 lane 在跑时，编辑任何文件之前先 `git stash list` + `git status`。**
   `git status` 干净**不等于**树是稳定的——它可能整棵躺在别人的 stash 里。
   ⚠️ 本项目已踩过两次「工作树是空的」判断错：一次是 board 误报，一次是 stash。
2. **`git status` 突然从几十项变成 0 项，先当出事处理，不要当"干净"。**
   判据：那些文件**最后一次写入的时间戳是不是同一秒**——同一秒 = 一次批量操作
   （`checkout` / `restore` / `stash`），不是「有人改完了」。
3. **派单时要双向保留文件**：只写「B 不许碰 A 的文件」不够，**还要写明那些文件
   是我的、我在它们上面编辑**。否则对方做基线测量时把你的在制品一起收走。

⚠️ **lane 报告「我 stash 了以便量 BASE」时的处置顺序**：
① **先只读查 `git stash list` 与那个 stash 的内容**（`git stash show --name-only`），
**确认里面有什么**（`-u` 的 stash 有**第三个 parent**，未跟踪文件在那里，
`git stash show` 默认不列它们，必须 `git cat-file -p` + `git ls-tree` 才看得到）；
② **不要自己 pop**——那是活着的任务的状态，§9 的顺序纪律同样适用；
③ **要求它交付前 pop 并确认未跟踪文件回到磁盘**，且**明确告诉它不要把当时那个
干净的树当成交付物**。

**⛔ 派单里必须写死的一句**：**不要 `git stash` / `git clean` / `git checkout .` /
`git reset --hard`** —— 若确需 stash 测基线，先告知，并保证交付前恢复。
本项目那条「取 BASE」的指令只说了「先报 BASE」，**没说「怎么拿」**，于是执行者
自选了 stash，而它对**别的 lane 的在制品**与**我同时在写的文件**都造成了破坏。

### ⛔⛔ `.tmp/` 是**共享**的：**我（编排者）与子代理都不得删它**

踩过，而且**当事人是我**：给 lane 的派单里写着「⛔ 不要清理整个 `.tmp/`」，
而**我自己每一条全量命令末尾都写了 `Remove-Item '.tmp' -Recurse -Force`，一天执行了三次**
⇒ 一条 lane 的在制品目录被删、连带它正在写的测试文件丢失。
该 lane 排查后**说不清是谁删的、不编**（正确处置）；真相是我。

**为什么这条必须进规则文件而不是只活在派单里**：规则只存在于 prompt 里
就等于**没有规则** —— 本节上面那条 stash 禁令已经是这个教训，
而「不许删 `.tmp/`」连派单都没稳定写全。**两条同型失败。**

**三条规则：**

1. **只删自己那个子目录**（`.tmp/<lane 名>/`），⛔ 绝不删 `.tmp/` 本身。
2. **有 lane 在跑时，连子目录也不删** —— 你不知道它是不是正在写。
3. **要腾地方就把 scratch 放到仓库外**（宿主临时目录）。实测可行，
   且 lane 能自查「在制品还在不在」：全量前后各校验一次待交文件的 SHA。

⚠️ **别拿 `.tmp/` 干净当证据**：它 gitignored，删了 `git status` 也看不出来
—— 与 §7.1「空集 ≠ 不存在」同源：**删掉的东西和从未存在过的东西，看不出区别**。

### 工作树有产出但报告未到时，不要盲提交

先 `task_result` 取回结果；取不到就跑全量确认一致；**仍不能确认就重派**。
不要因为「测试大概会过」就提交 —— **恒真的测试比没有测试更危险**。

### 派单时怎么划权限范围 —— **按意图，不按目录前缀**

踩过的坑：派单里写过「**绝不碰 `%USERPROFILE%`**」，本意是「别翻用户私人文件、
别碰别人项目」。结果子代理要读**宿主自己的 shell scratch** 时被这条拦住，
**申请了一个权限请求卡在那里**。**前缀式的禁令会拦住无害的东西，却仍然没说出
真正担心的是什么** —— 约束写得太粗，就只能靠猜。

正确写法是**按意图列出可读与不可读**：

| 可读 | 不可读 |
|---|---|
| 仓库内（含 gitignored 的 `.tmp/`）、宿主自己的临时/工具目录 | 用户的文档、凭据目录、**其它项目** |

⚠️ 这条规则**原本只活在派单 prompt 里**，也就是**没进规则文件** —— 所以没人能纠正它，
而它一直在被复制、变形。**规则不住在规则文件里，就等于没有规则。**

### 指令必须**可解析**，不能靠猜

同型失败：让子代理「去下载目录的 `brower\` 子目录找快照」（§6.1 当时就是这么写的，
因为刻意不写绝对路径）。「下载目录」是**歧义名词短语**，它猜成
`%USERPROFILE%\Downloads\` → 那里没有 → **连烧 4 个权限请求**盲猜
`Temp` / `Downloads` / `Desktop`（`Desktop` 还重试了一次）。

**派单里要么给可解析的搜法**（按文件名全盘搜），**要么明确说「搜不到就写搜不到」** ——
**绝不能让执行者去猜**，猜错的代价是它自己发现不了的（那次它一直以为只是权限没批）。

### ⛔ 权限请求**无法作答**时：缩小范围，不要等

给对了路径**仍然**会被拦：读仓库外任何路径都要 `permission.reply`。实测两次
`task_reply` 都得到 `Permission request not found`，而期间子代理已卡住、反复重试。

**处置顺序**：① 先确认它卡住的原因（连烧几个请求 = 在盲猜）；② **取消**该 lane；
③ **重派时把范围改到不需要权限**。只读审计类的活通常**根本不需要**那些外部路径 ——
把「需要外部路径才能回答的那一问」显式标成 **不在范围内**，比申请权限便宜得多。

⚠️ **超时 ≠ 零命中**，**「无法确认」≠「已终止」**。前者会让你把网络失败当成否定答案，
后者会让你在**活着的**任务背后动手（认这个的判据是落盘 + 进程，不是状态字段）。
