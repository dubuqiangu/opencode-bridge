# 全局工作规则

## 1. 写代码/做项目之前，先上 GitHub 搜

任何**非平凡**的实现任务（新建工具/库/脚手架、选型、接入第三方能力、修一个别人可能已经修过的 bug），
在动手写第一行代码**之前**必须先搜 GitHub，确认没有现成实现，避免重复造轮子。

跳过条件（可以直接实现）：
- 改动 < ~20 行且是本仓库内的局部逻辑；
- 纯业务/领域逻辑、配置、胶水代码；
- 用户已明确指定了实现方式或指定了要用的库。

### 怎么搜（按成本从低到高）
1. **gh CLI 已装**（v2.96），优先用它：
   - 代码片段级：`gh search code '<关键 API 或函数名>' --limit 30`
   - 库/工具选型：`gh search repos '<用途关键词>' --sort stars --limit 20`
   - 官方 MCP 也在本会话可用：`github.search_code` / `github.search_repositories`。
2. **X/Twitter、小红书、V2EX 等社区信息** → 加载 `agent-reach` skill（它负责互联网取数）。
3. 需要读某个依赖的**内部实现**（不是 API 文档能解决的）→ `clonedeps` skill。
4. 事实核验（star 数、license、是否 archived）→ `github-repo-verification` skill。

### 搜到什么之后
- **有成熟实现** → 先给出对比结论（star / 最近提交 / license / 维护活跃度 / 与本项目技术栈是否匹配），
  建议直接用或 fork；只有明确说清为什么不合适才自己写。
- **只有部分可借鉴** → 引用具体文件/行作为设计参考，在最终说明里给链接。
- **确实没有** → 一句话说明搜过什么（关键词 + 范围），然后自行实现。
- 搜的结果要落到给用户的回答里，不要静默地搜完就自己写。

## 2. 推送（push）前必须脱敏

**推送是不可逆的**——推上去的隐私数据收不回来，删 commit 也救不回已被爬取的内容。
所以顺序永远是**先扫描、后推送**，不是"推完再检查"。

> ⚠️ **扫描必须是独立的一步，并且要先读它的输出再推。**
> 绝对不要把"扫描 + `git push`"写进同一条命令 —— 那样 `push` 会在你读到扫描结果前
> 就执行，等于没有检查。（这条是踩过的：本人有一次把扫描与推送串在一起，
> 结果 `push` 先跑了，事后才看到 33 处告警——那次内容恰好干净，但流程是错的。）

按 `git ls-files`（**已跟踪的文件**）扫，不要只扫工作区。四类必须查：

### 2.1 真实凭据（按形状查，不要只搜关键字）

关键字搜索会漏——按各平台的**格式**查才有意义：

| 类型 | 形状 |
|---|---|
| Telegram bot token | `\d{8,10}:[A-Za-z0-9_-]{35}` |
| Slack | `xoxb-`/`xoxp-`/`xapp-`（app-level token） |
| GitHub | `gh[pousr]_[A-Za-z0-9]{20,}` |
| OpenAI | `sk-[A-Za-z0-9]{20,}` |
| AWS | `AKIA[0-9A-Z]{16}` |
| 私钥 | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |

同时扫 `(token|secret|password|api_key|app_secret)\s*[=:]\s*['"][^'"]{16,}['"]`
这种长串字面量赋值——测试夹具也逃不掉。

### 2.2 隐私数据（**最常被漏，且与代码正确性无关**）

- **本机用户名 / 用户目录**：搜 `Users\\`、`C:\Users`、`%USERPROFILE%`、以及
  **当前机器的用户名本身**（如 `whoami` 的输出）。测试里写死真实用户名 =
  自己泄漏它。→ 用通用占位符（`example-user`）+ `$USERNAME` 之类的动态断言。
- **绝对路径**：Windows 的盘符目录、POSIX 的 `/home/<用户名>` 等。测试里要用占位符。
- 内部域名、内网 IP、公司名、项目代号、客户名。
- 调试残留：`_probe*.py`、`_full.txt`、`_hold/`、临时 CSV/日志、截图。

### 2.3 运行期产物

`.gitignore` **只阻止将来添加，已跟踪的文件不会被它移除**。所以要显式确认
`config.json` / `state.json` / `*.log` / 锁文件 / `__pycache__` / `*.session`
都不在 `git ls-files` 里。

### 2.4 历史遗留也要管

扫**将要推送的那个范围**（`git diff --stat origin/main..HEAD`），
并注意有些东西可能**已经在远端**了——那是历史遗留，是否清理要问用户，
但**新引入的一律不发**。

## 3. 命名必须见名知意

**变量名、方法名、类名、文件名、常量名——都要见名知意。不许出现混乱的字母数字名称。**

具体禁止：

- **单字母或无意义缩写**（局部循环变量 `i`/`j` 除外）：`d`/`tmp`/`val`/`obj`/`data`/`res`/`p`/`h`/`f`/`cfg`/`ctx`
- **带数字后缀的"第几个"**：`data1`/`data2`/`msg2`/`handler3`/`tmp2` —— 说明它们本该是
  **不同的概念**，该换个名字表达差异，而不是加数字
- **拼音或拼音缩写**（中文项目尤其容易犯）：`biao`、`mingxi`、`canshu`、`huanjieqi`
- **中英混杂的自造词**：`wenJian`、`messageList`（方法名用驼峰而变量用下划线）
- **测试里的占位名**：`t1`/`t2`/`foo`/`bar`/`xxx`（断言的具体内容要出现在名字里）
- **缩写歧义**：`cfg` 到底是 config 还是 configure？`h` 是 height 还是 handle？**拿不准就写全**

### 边界：协议自己的名字不算违规

`data` `type` `id` `value` `result` `status` `error` 这些词**在协议里本来就这么叫**时，
沿用协议原名是**对的**——改名会让代码与线上格式对不上，读代码的人反而要再翻译一次。

- ✅ `response.get("data")` —— opencode API 的字段就叫 `data`，沿用才对
- ✅ `:data:` 跨文档引用角色（Sphinx 语法，根本不是变量名）
- ❌ `data = self._request(...)` —— 这是**我们自己**起的名字，必须换成 `response`
- ❌ `inner = data.get("data")` —— **最糟的一种**：我们的局部变量和 wire 字段同名，
  一个名字承载两层含义，读的人必须回头查协议才知道 `data` 指哪个

判定：**这个名字是协议给的，还是我们自己起的？** 自己起的才改名。

正确做法：

```python
# 差：一个变量名承载了三个含义，于是只能靠注释解释
def proc(d, t):
    ...
# 好：名字自己说明意图，注释只补"为什么"
def handle_inbound_message(payload, conversation_id):
    ...
```

判定标准：**新读者不看注释，能不能猜出这个变量是什么、这个函数做什么。**
猜不出就改名，不要靠加注释补救——注释会过期，名字不会。

这条同样适用于**文件名**：`a2a.py` 叫 `a2a.py` 可以（平台名），
但 `utils2.py` / `common3.py` / `tmp_helper.py` 一律不行。

## 4. 其他

- 有专门的 skill 就先加载 skill，不要凭记忆硬做。
- 改动配置文件前先备份（用带时间戳的 `.bak.YYYYMMDD_HHMMSS`）。
- 不要编造 star 数、license、版本号——用 `gh` 现查。

## 5. 代码组织

- **不要把所有代码塞进一个文件**。实现过程中按功能维度解耦：
  该建新文件/新文件夹就建——纯函数工具、数据源、事件处理、UI 组件、配置各归各的模块。
- 判定标准：单文件超过 ~400 行或出现多个互不相关的职责块时，就必须拆分；
  拆分以"改一处功能只碰一个文件"为目标。
- 入口文件只做组装(导入 + 装配 + 生命周期清理)，不承载业务逻辑。
- 拆分时行为必须零变化(纯结构调整)，命名见名知意规则同样适用于新文件名。

### 5.1 类体量也要管（用户定，2026-10-03）

> 文件级规则管不住"一个文件里只有一个巨物"。类同样会胖，而且更难发现——
> 因为它语法合法、测试照过、评审时看着"挺整齐"。

- **不要把所有功能都放进同一个类。** 一个类如果同时负责几件互不相关的事
  （协议解析 + 状态持久化 + 重试策略 + 格式化），就该拆成协作的多个类。
- 判定标准（满足任一即须拆）：方法数 ~15+、类自身代码行 ~250+、
  或出现多个互不相关的职责块。
- 拆分的两种正确形态，**优先第一种**：
  1. **抽出去** —— 把一整块职责连同其私有状态搬进新类，原类只留调用
     （这是首选：新类能独立测试，不依赖原类的内部状态）
  2. **拆基类** —— 只有当多个类共享同一批方法时才用；否则会把耦合搬进基类，
     只是把胖类换成了胖基类
- 拆分以"**改一处功能只碰一个类**"为目标。若拆完还要同时改两个类，说明拆错了。
- 拆分时**行为必须零变化**（纯结构调整），命名见名知意规则同样适用于新类名。
- **顺带判据**：新增功能时如果发现只能往某个已有大方法里塞 if 分支，
  或只能给一个大类加第 15 个方法，那说明**该拆了，别硬塞**。

⚠️ **这条规则的现实前提**：本仓库当前有 **18/65 个类超阈值**（详见
`tasks.md`「类体量债务」一节），`BridgeCore` 49 方法 / 1120 行是最大的一处。
**新代码不得继续加厚这些大类** —— 新功能要么进新模块，要么先拆。

---

## 6. 先查参考项目，不要靠推测（本项目专用，全局第1 条的具体化）

> **能抄就抄，能借鉴就直接复刻，不要重复造轮子、从头踩坑。**
> 动手写第一行实现之前，先在下面的参考项目里搜一遍。

### 6.1 参考项目分两类，别搞混

**（A）IM 平台 / 网关工程范式** —— 查平台协议、接入方式、心跳、桥接、会话管理

| 优先级 | 项目 | 规模 | license | 本机可读副本 |
|---|---|---|---|---|
| **首选** | [`NousResearch/hermes-agent`](https://github.com/nousresearch/hermes-agent) | 250806 stars | MIT | `%LOCALAPPDATA%\hermes\hermes-agent` |
| **次选** | [`zhuiyueya/dsh-im-gateway`](https://github.com/zhuiyueya/dsh-im-gateway) | 49 stars | MIT | 临时目录下的 `ref-dsh-im-gateway` 副本（本会话用的是 `%LOCALAPPDATA%\Temp\opencode\ref-dsh-im-gateway`） |

平台清单取两者并集（≈31 家），见 `docs/platform-design-reference.md`。

### 6.1b 已下载的仓库全量快照（**优先用这个，比联网克隆快**）

本机已把上面两个仓库**整个下载成纯文本**，含目录树 + 全部文件内容。
文件名里的 `8a5edab282632443` 是 commit SHA，**锁定了参考版本**——
引用结论时务必连 SHA 一起写，否则"参考版本变了"就成了无法复现的借口。

**定位方式：按文件名搜**（仓库名 + SHA 唯一），它们在本机的下载目录下的
`brower\` 子目录里——刻意不写绝对路径（仓库公开，机器路径不该跟出去）；
换机器或换了位置请按文件名重新定位。

| 仓库 | 文件名（搜索用） | 大小 |
|---|---|---|
| `zhuiyueya-dsh-im-gateway` | `zhuiyueya-dsh-im-gateway-8a5edab282632443.txt` | 622 KB / 1.4 万行 |
| `NousResearch/hermes-agent` | `nousresearch-hermes-agent-8a5edab282632443.txt` | **64 MB / 134 万行** |

**⚠️ 用法纪律**：

- **一律用 `Grep` / `grep` 定位，绝不整读。** hermes 那份 134 万行，
  整读会占满上下文、而且大概率什么都找不到。
  先 `grep -n '<符号名>' <file>` 拿到行号，再 `read` 带 `offset`/`limit` 取那一段。
- 这两份是**快照**，不是活代码。若结论涉及"最新版本如何"，
  仍需 `gh` 现查并核对 SHA 是否仍是最新的 `main`。
- 快照里可能包含密钥/令牌等敏感字符串（第三方仓库偶尔会误提交）——
  **只读不抄**，绝不要把里面的任何长串字面量搬进本项目。

**（B）opencode 服务端契约** —— 查事件流、API 字段、错误语义

⚠️ **（A）里的两个项目都不消费 opencode 事件流**，不能当这部分依据：

- `hermes-agent`：全库 grep `/api/event` **零命中**；它的 `/api/events` 是自己的
  dashboard WebSocket（`hermes_cli/web_routers/chat_ws.py:646`）。它提到 OpenCode
  的地方是**把 OpenCode Zen 当 LLM provider 中转**（模型端点），与 coding agent
  的事件流无关。
- `dsh-im-gateway`：全库零 opencode 引用；它是 `@deepseek-ai/dsh-agent` + cordis
  的 IM 网关。

这部分要看这些（按权威度）：

| 用途 | 去哪查 |
|---|---|
| **权威定义** | `anomalyco/opencode` 源码：`packages/schema/src/event-manifest.ts`（`/api/event` 白名单）、`session-event.ts`、`permission.ts` |
| **机器可读 schema** | 运行时拉 `GET /openapi.json`，含 `V2Event` 完整 `oneOf` 枚举（文档站的 `/doc` 在 2.0.22 已不存在） |
| **架构语义** | `specs/v2/event-stream-architecture.md`（队列/编码/溢出语义） |
| **怎么消费（最贴本项目）** | `grinev/opencode-telegram-bot` → `src/opencode/v2/events.ts` |
| **delta 合并算法** | `openchamber` → `event-stream/delta-coalescer.js` |
| **真实抓帧样本** | `pingdotgg/t3code` → `testkit/fixtures/opencode2_*/opencode_transcript.ndjson` |

⚠️ **别抄 `dev` 分支**：它已把 `session.*` 改名成 `session.next.*`
（`textID`/`reasoningID` 取代 `ordinal`）。本项目运行时是 2.0.22。
⚠️ **文档站对 v2 已过时**（`docs/server.mdx` 列的还是 v1 事件）——这正是推测的来源。
**读实现，不读文档。**

### 6.2 真实教训（结论已被我第一次判断错，必须写准）

2026-10-03 的 A4 真实验证：我一度断定"事件名全错，只认出1 种"。**那个结论是错的。**
@librarian 核实源码后：映射表 **13 个里 11 个是对的**，真正的原因有三个：

1. **观察窗口太短**（只等 25 秒）—— 那轮模型一直在 thinking，167 条
   `session.reasoning.delta` 就是证据。**turn 根本没结束**，
   自然等不到 `session.execution.succeeded`。
2. **`session.status` 与 `session.idle` 在 v2.0.22 从不发布**（源码里 `session.idle`
   甚至标着 `// deprecated`）—— 而桥把 `session.idle` 当作"一轮结束"的信号之一，
   于是**永远等不到**。这才是"用户只看到 `⏳ 处理中…`"的真正根因。
3. **`execution.interrupted` 的 `reason == "shutdown"` 不算结束** —— 服务重启会
   保留 claim 并续跑这一轮，不特判就会永远等不到 settle 事件。

**教训不是"我猜错了名字"，而是三条更具体的东西**：

- **等待必须由终止事件驱动，不能靠超时猜**。`session.text.delta` 迟迟不来是正常的
  （模型在思考），超时后当作"没内容"就会误判。
- **正文要合并后再发**：delta 极度碎片化（实测 5.7 KB 回答 = 3402 个 delta，每个
  1.7 字符），IM 场景必须做时间窗/大小窗合并，否则一条消息变几千帧。
- **权威全文用 `session.text.ended.data.text`**，不要自己拼 delta——delta 是
  ephemeral，断连即丢。

还有一条通用铁律，**与具体项目无关**：

> **未知事件不许静默丢弃。** `_dispatch` 里 `if handler is None: return` 会让
> "事件名写错"这类错误在生产里表现为**完全无迹可循**。至少打一行日志。

### 6.3 怎么用

1. 动手前先`Grep` 本机副本 / `gh search code '<符号名>' --limit 30`
2. 找到就照抄，不要凭理解重写——参考项目已经踩过那些坑
3. 抄完在进度日志里记一句差异与原因
4. 抄不到才自己实现，日志里说明"查过什么、没查到"
5. **抄来的契约仍要用真实环境验证**（参考项目可能对着不同版本，这步不能省）

## 7. `edit` 工具纪律（`Could not find oldString` 专项）

`edit` 要求 `oldString` 与磁盘内容**逐字节一致**。不满足时报
`Could not find oldString ... must match exactly, including whitespace and
indentation`——这个报错读起来像"内容写错了",实际绝大多数是重建失真。

本仓库已实测踩中:`opencode_bridge/adapters/slack.py` 字节级检查完全干净
(无 BOM、无 CR、无 tab、无行尾空格、合法 UTF-8),失败原因只能是下发的
`oldString` 与磁盘不一致,而当时 `base.py` / `discord.py` / `slack.py`
**三个文件同时处于已修改状态**——多文件重构进行中,旧快照已失效。

固定下列纪律:

1. **先 `read`,再 `edit`。** `oldString` 必须逐字节从 `read` 输出复制,禁止凭记忆补写。
2. **Python 缩进按 `read` 里的实际列数抄,只用空格,绝不 tab。** 本仓库 `src/` 全部
   是 4 空格缩进;Python 对缩进敏感,缩进错一格不只是匹配失败,直接是语法错误。
   嵌套层级要数清楚——`try {` / `with` / `for` 里面的语句比外层多 4 格。
3. **空行必须原样保留。** `read` 把空行渲染成 `3: `(行号 + 尾随空格),该尾随空格
   不可见,转录时最容易丢失。本仓库 `src/` 平均每 9 行就有 1 个空行,命中概率很高。
4. **`oldString` 用 3–8 行最小锚点。** 不跨文件头 docstring,不跨整段 `class` / `def`
   声明,越大越容易失真。
5. **同一文件一轮只发一次 `edit`。** 多处改动分轮串行,或直接 `write` 整文件重写;
   禁止在一个批次里对同一文件并发多个 `edit`(后发的会因前一个已改而失效)。
6. **失败后必须先 `read` 再重试。** 连续两次失败禁止第三次盲猜——第二次仍失败说明是
   理解偏差,不是字符差异。
7. **本仓库是多适配器并行改动(`adapters/` 下 telegram / slack / discord / base 常常
   一起动),跨文件改完一轮后,对任何已改文件的 `oldString` 一律视为失效快照**,必须重读。
   同理,fixer / designer subagent 落盘后也必须重读。
8. **怀疑改动可能已落地时先 `git status` + `git diff` 确认。** 若 `newString` 的内容已
   存在于文件里,说明改动已应用,不要再下发同一 edit。

### 排查口径

`Could not find oldString` 与"多处匹配 / 需更多上下文"是两种不同错误,前者只可能是内容
不一致。按这个顺序查:

1. **缩进列数**——最常见。逐列比对,别凭观感。
2. **空行**——段落之间的空行是否被合并。
3. **文件是否已被改过**——`git status` 看工作树;本仓库经常有未提交改动。
4. **编码**——本仓库已由 `.gitattributes`(`* text=auto eol=lf`)统一 LF;
   `install.ps1` / `plugin/install.ps1` 的 UTF-8 BOM 是**故意保留**的
   (Windows PowerShell 5.1 读中文需要它),不要为了"统一"而删。
