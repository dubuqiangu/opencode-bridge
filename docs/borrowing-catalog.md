# 借鉴项目目录（百家之长汇总）

> **用途**：收录所有可作为借鉴来源的参考项目，供后续执行代码修改时翻查。
> 每条借鉴点标注来源文件（锁定 commit）与对本桥的适用性（已有 / 缺 / 与既有拍板同向）。
> 抄时照 AGENTS.md §6.4：能抄就照抄、抄完记差异与原因；抄来的契约仍要用真实环境验证。

## 项目一览（star / license / archived 均 `gh` 现查，识别日期见各行）

| 项目 | license | stars | 活跃度 | 锁定版本 |
|---|---|---|---|---|
| `MichengAI/dsh-im-connect` | Apache-2.0 | 93 | 2026-10-24 仍在推送 | `551f02997c7b617d438c732a9b070e8c943693d6` |
| `xmanrui/dsh-im` | MIT | 2497 | 2026-10-25 仍在推送 | `e686e15540e36b2c061b45a7dfc2b0e7e1e61b5b` |
| `NousResearch/hermes-agent` | MIT | 256994 | 2026-10-25 仍在推送 | `4ea8d23cc9f527332ccb1d55359b27741b6ea51b` |
| `zhuiyueya/dsh-im-gateway` | MIT | 54 | 2026-09-27 推送 | `8e8fbca125dda15b5cdd2ae79ac63a91e21f7c37` |

以上四项均未归档（**2026-10-25** `gh` 现查 + 本地 clone `git fetch` 复核 dsh-im/dsh-im-connect；dsh-im-gateway HEAD 零差异）。
⚠️ **2026-10-25 回看**：四行里三个 HEAD 再前移 —— dsh-im `8ac3e6e`→`e686e15`（+60 commit，**8 个 telegram 相关**，新增可借鉴点见 §2.3）· dsh-im-connect `93428ed`→`551f029`（telegram 相关 **0**）· hermes-agent `3cbb13f`→`4ea8d23`（本轮回看基线；⚠️ live HEAD 已再前移 `d050956`，+407 commit，均 2026-10-25 `gh` 现查）；dsh-im-gateway `8e8fbca` **连续第四轮零差异**。star 四项全部变化（79→93 / 2257→**2497** / 255751→256994 / 54→54），四项**均未归档**（均 2026-10-25 `gh` 现查）。本轮细读结论见 §1.3 与 §2.3。
⚠️ **2026-10-16 的旧基线已作废**：四行里三个 HEAD 前移（dsh-im `b7698a6`→`0b9cbc2`
共 **23 commit**、v5.0.0→v5.2.0；dsh-im-connect `e271ec1`→`617b079` 共 **65 commit**、
v0.1.77→v1.0.8；hermes-agent `f732746`→`6e9b3e3` 共 **2006 commit**），star 四项全部变化
（59→72 / 2031→2181 / 254101→255192 / 50→52）。⚠️ 引旧 SHA 的结论一律按「过期」处理（§7.1）。本地源码 clone 在宿主临时目录
（`ref-michengai-dsh-im-connect` / `ref-xmanrui-dsh-im`，git 仓非快照）；
hermes-agent 与 dsh-im-gateway 另有全量纯文本快照（文件名含 commit `8a5edab…`，
搜法见 AGENTS.md §6.1）。**引用结论一律连 SHA 一起写**，否则「参考版本变了」
就成了无法复现的借口。

---

## 1. MichengAI/dsh-im-connect（2026-10-16 收录；基线 **2026-10-21 更新为 `93428ed`，v1.0.11**；上一基线 `617b079` 已过期）

**定位**：将主流 IM 平台（dingtalk/discord/feishu/lark/qq/slack/telegram/wecom/weixin）
接入本机 DeepSeek Harness。TypeScript / Koishi(cordis) 双侧插件，与本桥领域高度同构：
去重、按会话串行、合并窗口（`..`/`!!`）、流式回复组装、长文切分、占位进度、
审批/提问交互、会话注册表、原子落盘——每件都有对应物。
**证据面**（均读自锁 `e271ec1`）：README.md + SECURITY.md +
`src/engine/{deferred-delivery,delivery-history,seen-store,keyed-queue,merge,
atomic-file,approval,question,reply-stream,message-progress,split,session-id,
credentials,abort}.ts` + `src/channels/connection-state.ts` +
`gateway.ts`/`router.ts` 结构级扫描。

### 借鉴点清单

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| A | **投递三态错误语义**：`DeliveryUnavailable`（未提交→可自动恢复）/ `DeliveryRejected`（平台明确整条拒收→补发安全）/ 其余一律 `unknown`（不猜测、不自动重发）；**部分送达后遭拒收 → 降级 `unknown`**（整条重发必然重复） | `deferred-delivery.ts` | 与 fix-299「`outcome_unknown` 只告警不重放」同向，但多出两级：本桥传输层失败目前一律 `outcome_unknown`，「确定未提交」与「平台明确拒收」没有区分 ⇒ 前者可安全补投、后者可安全重试 |
| B | **投递日志（delivery journal）**：版本化落盘、原子写 + 0600、加载逐字段校验、崩溃时 `sending→unknown` fail-safe、7 天过期、1000 条上限（先清已决条目、满则响亮报错）、patch 失败条目入隔离集；`/delivery` 查询 + `/delivery retry <record-ID>` **手动补投** | `deferred-delivery.ts` | 本桥 `outcome_unknown` 行永久留盘、每次启动只告警「请重新发送一次」；且无 `message_id` 平台的逐字节相同重发会被 sha256 去重挡掉（台账已记录的知情代价）⇒ 手动补投面直接解决这条路 |
| C | **从事件历史恢复答复文本**：`recoverTurn` 按 requestId 精确匹配回合、从冷历史拼最终文字投给用户——「不调用模型、不重放工具」 | `deferred-delivery.ts` + `delivery-history.ts` | 给本桥 `outcome_unknown` 提供第三条路：把答复找回来投出去。对接口径 = DSH Chat 冷历史读取接口（`sessionController.follow`）；本桥对应面是 opencode 会话消息 API（`OpenCodeClient.messages`），**形状需另行核实后才能判定可行性** |
| D | **结构感知长文切分**：围栏/GFM 表格整块不拦腰切；超长围栏拆正文、每片补齐自己的开始/结束标记；超长表格按行拆、每片重复表头与分隔行；`(n/m)` 序号**只加在纯文字片**（表格/围栏片加了会破坏结构）；前缀预留预算（`PREFIX_ALLOWANCE`）；按码点长度切 | `split.ts`（文件头自述「思路参考 dsh-im，按本仓库的单分片器架构实现」⇒ 与 dsh-im 同源，此件是精修版） | 本桥 `split.py` 的 `split_text` 是纯长度切分（无围栏/表格感知；分段编号从未启用，默认不加前缀）⇒ 直抄级升级点，出站多平台共用 |
| E | **回合纪元（generation epoch）**：reset 自增代数，迟到的旧回合增量按代数丢弃、不重建流；另按会话记录流归属、tail-promise 串行防并发重复开流 | `reply-stream.ts` | 正对本桥「在途 turn + 迟到 delta 另建 turn」竞态（fix-289 supervisor 的拍板是「什么都不动」，纪元是更根本的解法但改动面大 ⇒ 优先级低、需单独立项） |
| F | **审批语义**：等待上限 5 分钟，**到期按取消结束、不按用户拒绝**；审批期间其他文本不进下一轮（队列门控）；提示发送失败按拒绝处理；群聊只认发起者；`accepting` 旗——问题卡尚未呈现给用户前不接受作答 | `approval.ts` + `question.ts` + SECURITY.md §6 | 本桥已有 D3 按钮（telegram+slack）+ `reply_permission` 回答面；**审批等待语义（超时=取消、队列门控）本桥现状未核**，落地前先量 |
| G | **进度状态机**：状态更新 2s 超时且失败/超时不阻塞正文；30 分钟过期清账；typing 租约计数（同 chat 单定时器、旧任务结束不停新任务）；回合按 requestId 认领（不按聊天或 FIFO 猜）；同回合文本+文件投递聚合出「全部送达 / 有未知 / 全部拒收」三态信号 | `message-progress.ts` | 本桥占位消息模型同形（telegram 编辑式进度）；typing 指示本桥现状**待核**；「按 requestId 认领回合」与 fix-288 创建权 claim 同族 |
| H | **每平台原生进度标记 + 平台各自 Markdown 规则**：Discord 无表格→竖排块、Slack mrkdwn 转换、钉钉 AI 卡、QQ markdown；模块化渠道适配器（`ChannelAdapter` 契约） | `message-progress.ts` + `src/channels/` | 本桥 13+ 适配器已有同类分层（`adapters/base.py` 契约）；平台渲染规则面可作为出站质量参考 |
| I | **会话标识与注册表**：`im:` 前缀自解析（`channel:kind:stamp:chatId`）；记录三态 `adopted`/`muted`/`detached`；`/unbind`（解除投递关系、保留历史元数据防重启重绑）/`/mute`（保留登记、静音输出） | `session-id.ts` + `router.ts`（`SessionRouter`/`ChatBinding`） | 本桥 `SessionRegistry` 有 `/new`（drop_session 拍板已落地）；unbind/mute 无对应物 ⇒ 新功能候选 |
| J | **凭据保险库**：凭据文件损坏/不可读时**抛错拒绝继续**（「不能把空对象写回去覆盖唯一凭据副本」）；POSIX ref 名规范化；0600 权限 | `credentials.ts` + `secure-file.ts` | 本桥 config.json 明文（.gitignore 保护）；损坏时行为**待核**，核过再判是否适用 |
| K | **安全姿态文档**：默认拒绝、威胁模型一句话（「任何人 DM 或 @ 机器人，都可能试图驱动本机 agent」）、管理面认证不降级、错误码分类（401/403/413/415/503）、`Cache-Control: no-store` | `SECURITY.md` | 文档范式可抄（本桥 README/安全叙述可对照补一条威胁模型句） |
| L | **工程面**：约百个按特性分文件的单职责测试（`tests/*.test.mjs`）；双语 CHANGELOG；迭代文档目录（每个主题含验证记录）；CI workflows；`connectionState()` 把各平台状态字符串归一化为 `{connected,connecting,reconnecting,disconnected,stopped,error,unknown}` 且「仅明确就绪状态计入在线」 | `tests/`、`CHANGELOG*.md`、`docs/01-当前工作/`、`src/channels/connection-state.ts` | 归一化谓词与「明确否/未知分开」的口径与本桥 health 面同族；测试组织已是本桥现行实践 |
| M | **发送重试阶梯**：`SEND_RETRY_DELAYS_MS = [0, 1s, 3s, 8s, 20s]` + 30s 总预算；`isDefiniteSendFailure` 判定 | `gateway.ts` | 本桥 telegram 429 单次重试（`retry_after` 夹 60s 上限）、未接入阶梯 ⇒ 若引入注意与既有「不占关停」纪律（`stop()` 必须能打断等待）对齐 |
| N | **小原语**：`KeyedSerialQueue`（同 key 串行、异 key 并行、完成自清）；`SeenStore`（2000 条 LRU、损坏文件先备份再重置）；`writeFileAtomicSync`（同目录临时文件 + fsync + rename，`backupCorruptFileSync` 隔离不可解析文件） | `keyed-queue.ts`、`seen-store.ts`、`atomic-file.ts` | 本桥对应物：`_drain` 会话串行、sqlite 收件箱、`state.py` 备份机制——形态对齐，无需照搬 |

### 1.1 本轮新机制（2026-10-20 回看，`e271ec1..617b079`，65 commit，v0.1.77→v1.0.8）

⚠️ **65 个 commit 里大半是 UI 重构**（`refactor(ui): purge antd dependency` 起的一串按钮 / 模态 / Tabs / design-token 对齐），本桥无图形宿主 ⇒ ⛔ **结构性不适用**，不逐条记。真正与本桥同域的只有下面三行。

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| T1 | **Telegram 论坛话题（Forum / Topics）隔离**：会话键从 `<chatId>` 扩成 `<chatId>:<threadId>`（`telegramChatId`/`parseTelegramChatId` 一对编解码），**所有出站调用**（`sendMessage`/`sendRichMessage`/`sendDocument`/`editMessageText`/`setMessageReaction`/`sendChatAction`/`beginReply`）在解析出 `threadId` 时附 `message_thread_id`；入站 `message` 与 `callback_query` 两路都读 `message_thread_id` | `src/channels/telegram.ts`（+104）、`tests/telegram-topics.test.mjs`(217) | ⭐ **本桥确认缺失**（本桥 telegram 适配器无 `message_thread_id` 任何处理）。⚠️ **但⛔ 不要照抄它的编码方式**：它把 threadId **编进 `chatId` 字符串**，于是「会话标识」与「传输寻址」被耦合成一个字段，所有消费方都得靠 `parseTelegramChatId` 反解（如 `sendChoices`/`addStatusReaction` 里各反解一次；任一处忘了反解 ⇒ 把 `"-100123:5"` 当 chat_id 发出去）。本桥若引入，**必须把 `thread_id` 作为独立字段挂在 inbound 消息与会话键上**，⛔ 不塞进 `chat_id` ⇒ 这是**产品/协议判断**（是否支持话题群、会话粒度按话题还是按群）⇒ 归**第 1 类（产品判断等用户拍板）** |
| T2 | **按账号的发送模式设置 `queue` / `steer`** | `feat: add per-account send mode setting (queue/steer)`、`tests/send-mode.test.mjs`(68) | 与本桥「按会话串行 `_drain`」同族但**方向不同**：本桥是固定排队，「steer」是让新消息插进在途回合。本桥若做需动在途 turn 语义（与 fix-289 拍板「在途 turn 什么都不动」冲突）⇒ ⚠️ 与原有拍板冲突，**⛔ 不擅自引入**；仅登记 |
| T3 | **富媒体 / 文件投递的失败诚实性**：`fix(qq): infer rich media file_type`（图片不再发成文件卡片）、`fix(file-delivery): 投递前已删除的文件静默跳过，失败日志补 error.code`、`fix(file-delivery): do not skip not-regular-file so directory delivery reports error` | `src/engine/file-delivery.ts`、`src/channels/qq/qq-bridge.mjs` | 与本桥「失败要可见、⛔ 不静默」同向。⚠️ **它自己改过两版、方向相反**（先「静默跳过已删文件」、随后「不跳过非普通文件、让目录投递报错」）⇒ 可借鉴的是**那条判据「跳过要有理由，非普通文件不等于已删除」**，不必抄实现 |

### ⛔ 结构性不适用

- Koishi/cordis 插件宿主形态（本桥是独立 Python 进程 + HTTP webhook）；
- 扫码 / App Manifest 配对接入（依赖各平台专属配对端点；本桥目标平台均为静态 token——与 dsh-im 侧结论相同）；
- DSH 专有接口（冷历史读取、`connection.requestRejection` 管理面鉴权）——本桥对应面需另行核实形状，⛔ 不照抄接口名。

### A/B/D 落地核实（2026-10-16 直核；证据 = 本仓库源码 + 上游 schema 锁 tag `v2.0.22`）

标号对照：**用户 A = §1-A（+ §1-B 手动补投）· 用户 B = §1-C · 用户 C = §1-D · 用户 D = §1-F**。

**A（投递三态 + 手动补投）**——现有分档（`inbound_gateway._dispatch_prompt`，恢复路径对应 `inbox_recovery`）：`create_session` 阶段失败 ⇒ `failed`（可重放）· `prompt()` 抛 `OpenCodeError` 且 `status == 409` ⇒ `pending`（可重放）· 抛 `OpenCodeError` 且 `status` 非 None 非 409 ⇒ `failed`（可重放）· 抛 `OpenCodeError` 且 `status is None` ⇒ `outcome_unknown`（⛔ 只告警不重放）· 抛非 `OpenCodeError` ⇒ `outcome_unknown`（⛔ 只告警）· 崩在 `prompt()` 内 ⇒ `attempting`（⛔ 只告警）。
关键事实（`opencode_client._request`）：`HTTPError` ⇒ 带 status（服务端**已答复**）；`(URLError, OSError, HTTPException)` ⇒ status=None ⇒ **这一类混装**「连接未建立（请求没出门）」与「已发出但读响应时断（可能已提交）」⇒ 现状**无法区分**。
⇒ 与 §1-A 的差：①「确定未提交」与「可能已提交」未分（前者可安全自动补投）；② §1-B 的**手动补投面完全缺失**（桥只有「请重新发送一次」告警）。
⇒ 待用户拍板：手动补投的**入口形状**；「未提交」是否提升为独立可自动恢复档（会触碰 `_request` 分档 + 可能改 `_dispatch_prompt` 返回契约，而 fix-300 明写「⛔ 不许在缺陷修复里顺手改契约」）。
⇒ ⭐ **2026-10-16 用户已拍板 + A2 已落地**：形状 = 手动补投走**群内命令 `/delivery retry`**（列出本会话待补投项 + 按序号补投指定一条）；「确定未提交」**不新增状态档**、**复用既有 `failed`**（可重放 ⇒ 自动重投）。**A2 已落地（`a21abfb`）** = 传输层分档：新增 `request_phase.py` 的 `request_definitely_never_sent(exc)`（链中 `ConnectionRefusedError`/`socket.gaierror` ⇒ True；`TimeoutError`/`ConnectionResetError`/`RemoteDisconnected`/`SSLError` 等一律 False ⇒ fail-safe）+ `OpenCodeError` keyword-only 字段 `request_never_sent`（默认 False）+ `_dispatch_prompt` 的 `status is None` 分支拆两档 ⇒ 上面那条「现状**无法区分**」在**传输层这一档已不成立**（服务端**已答复**的 status 档本来就能分）。⚠️ `_dispatch_prompt` **返回契约未动**（复用既有 `"error"`，不新增返回值 ⇒ `recover_inbox`/`dispatch_recovered` 零改动）；⚠️ 知情代价：从未提交的消息也走退避阶梯与预算、最终可能 abandoned。**A1 已落地（`b57341e`，2026-10-16）**：手动补投面 = **群内命令 `/delivery retry`**（无参数列出本会话待补投项、带序号补投其中一条）—— 新模块 `delivery_retry.py`（`DeliveryRetryCommand.reply_for`，⛔ 不加厚 `CommandHandler`，照 `SessionModelCommand` 委派模式）+ 第 9 协作者 + 命令表键 `"delivery"` + `HELP_TEXT` 一行；候选集 = `inbox.pending_prompts()` + `inbox.due_failed_prompts(now)` 按 `conversation_id` 过滤后拼接，⛔ 不列 `attempting` / `outcome_unknown` / 未到期 `failed` / `abandoned` / `delivered`；单条重投复用 `inbox_recovery.replay_one`（公开 + 四返回常量，⛔ `not_attempted` 不并进 `failed`）、`dispatch` 复用网关恢复路径 `dispatch_recovered`；⭐ 用户 2026-10-16 拍板：只列 `pending` + 到期 `failed`、手动重投失败消耗退避预算；⚠️ 知情代价：409 也落 `failed` 并消耗一级预算、序号在重投时重算（不便、非安全风险）；守门 = tests/test_delivery_retry.py 28 用例 + 反向证明 3/3。

**B（冷历史恢复答复）**——API 存在：`OpenCodeClient.messages(session_id, *, limit=100)` ⇒ `GET /api/session/{id}/message` ⇒ `list[dict]`；⚠️ **生产零调用点**（仅 2 处测试，用假桩 ⇒ 形状未被真机核过）。
v2.0.22 消息形状（上游 `packages/schema/src/session-message.ts` @ tag `v2.0.22`）：Base `{id: "msg_" 前缀, metadata?, time.created}`；`{type: "user", text}`；`{type: "assistant", content: [...]}`，其中 `{type: "text", text}` = 正文片段。
⚠️ 关键缺口：失败路径上**拿不到 prompt 的 msg id**（`prompt()` 抛了 ⇒ 无 `data.id`）⇒ §1-C 的「按 requestId 精确匹配回合」**不可直接照搬**；桥只能按 `queued.text` + 时间序定位（弱于参考；同文本重发会歧义）。
⚠️ 未核实：真机 envelope 与字段名（需一次真实 GET；本机无运行中的 opencode server）。
⇒ 待用户拍板：无 requestId 时的对接口径（按文本 + 时间序，还是先搁置待真机）。
⇒ ⭐ **2026-10-16 用户已拍板**：对接口径 = **先按 `queued.text` + 时间序定位 assistant 正文**（⛔ 不照搬 §1-C 的 requestId 匹配）。**B 待落地**。

**D（审批超时语义）**——现状：`event_stream._on_permission_asked` 只**渲染 + 记账**（`PermissionLedger.record_asked`），**无任何等待 / 超时 / 队列门控**；`PermissionLedger` 无超时（其 docstring 明写「缺超时」非 C4 根因）；档位 pending / already_replied / resolved_upstream / unseen，进程内、重启清空、容量 512。
「审批期间其他文本不进下一轮」在桥上是**间接**由服务端 busy 实现（审批挂起 ⇒ 那轮不结束 ⇒ 新 prompt 得 409 ⇒ 落 `pending`），⛔ 不是桥显式门控。⚠️ a2a `reply_timeout=300s` 是 task 阻塞、**另一回事**。
⇒ 与 §1-F 的差：①「等待上限、到期按**取消**结束」桥没有；②「群聊只认发起者」桥现状未核（callback 只有白名单闸）；③ `accepting` 旗桥没有。
⇒ 待用户拍板：是否引入桥侧定时器「到期按取消」+ 超时值；范围（是否含「群聊只认发起者」）。
⇒ ⭐ **2026-10-16 用户已拍板**：**引入桥侧定时器，等待上限 5 分钟、到期按取消（非拒绝）**；本轮范围**只做超时**，⛔ 不含「群聊只认发起者」与 `accepting` 旗。⚠️ **前置核实（2026-10-18 两车道定案）**：上游 v2.0.22 **无「取消」API**（`Permission.Reply` 锁死 `["once","always","reject"]`，pending 永久挂、服务关闭时才逐一 reject）⇒ `reject` = 会话级批拒绝、**违背「非拒绝」** ⇒ **唯一忠实实现 = ledger-only 取消**：每 pending 请求 5 分钟 Timer，到期**不发任何 API**、ledger 标 `ANSWER_CANCELLED` + IM 告知用户。⚠️ **知情代价（须文档化）**：服务端那条请求**永久挂到服务关闭**（本桥无法撤回）。⇒ **D 已落地**（见 tasks.md 未完成项总表「dsh-im-connect 借鉴面落地」行）：新增 `permission_timeout.py`（`PermissionTimeoutWatcher`）+ `permission_ledger` 第五态 `ANSWER_CANCELLED` + `record_cancelled`（不重开已关闭请求守卫）+ event_stream/commands/inbound_gateway 三处真回复落点 `cancel`；守门 = tests/test_permission_timeout.py 12 用例 + ledger，定向 472/472 全绿、反向证明 3/3。

---

### 1.2 本轮新机制（2026-10-21 回看，`617b079..93428ed`，v1.0.8→v1.0.11）

**证据面**（均读自 `93428ed` 的 `src/`）：`channels/{telegram,discord,feishu,meta,factory,diagnostics}.ts`、
`engine/{gateway,topic-title,keyed-queue,chat-commands,types}.ts`、`manager.ts`。
⚠️ 这 15 个 commit 里**仍有大量 client/侧边栏 UI 重构**（`client.js` 单文件 ±2400 行）⇒ ⛔ 结构性不适用，不逐条记。

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| U1 | **按账号的代理配置（`inherit`/`direct`/`custom`）+ 反射式暴露**：`proxyMode` 缺省 `inherit`（走宿主 env）；`direct` ⇒ `new Agent()` 显式绕开；`custom` ⇒ `new ProxyAgent(url)`。URL 闸**只接受 http/https、必须有 hostname、且 pathname 必须是 `/` 且无 query/hash**（`manager.normalizeProxyUrl` 另拒 `username`/`password` 与 `port > 65535`）；每处出站 `fetch` 统一经 `fetchTelegram`/`fetchImpl` 注入 dispatcher，**`stop()` 里 `dispatcher.close()` 失败再 `destroy()`**；`ChannelField` 新增 `settingsOnly` ⇒ 代理地址**不进新增账号的凭据表单**，只出现在已绑定账号的设置里 | `channels/{telegram,discord,factory,meta}.ts`、`manager.ts`（`hydrateProxyUrl`/`proxyUrlCache`/`accountView`）、`tests/telegram-proxy.test.mjs`(265)、`tests/discord-proxy*` | ⭐ **本桥确认缺失**：适配器一律用默认 `urllib`/`http.client` 行为，**无逐账号代理开关**（只有进程级 `HTTPS_PROXY` 环境变量）。可直抄的三条判据：① 代理 URL **必须拒 userinfo/query/hash**（否则 `https://u:p@host` 会被当凭据泄漏进日志）；② **渲染给前端时只给 `proxyConfigured` 布尔，不回显 URL**（本桥 `/setup` 视图同样应只报「已配置」）；③ **`direct` 是显式语义**，不是「未配置」的副作用。⚠️ 引入需动 13 个适配器的出站构造 ⇒ 归**第 1 类（产品判断等用户拍板：是否需要逐账号代理）** |
| U2 | **Feishu 卡片 HTTP 400 回落纯文本 + 脱敏诊断摘要**：仅当 `status === 400` **且不是限流**时才回落（限流必须优先保留上层退避语义，否则改成发纯文本只会再被限一次）；回落失败再抛。诊断只输出 `http=<status> code=<code>`，且两个值都过**类型 + 范围**闸（status 100~599 整数、code 0~999999999 整数或 1~9 位数字串），⛔ 响应正文、请求配置、凭据一概不出 | `channels/feishu.ts`（`httpFailureSummary`）、commit `edef015`/`29171ce` | 与本桥「失败要可见、⛔ 不静默」同向，且**多出两条本桥没有的判据**：① 「回落前先排除限流」——本桥 telegram 的部分失败路径没有这个区分；② **诊断字段必须过范围闸再落地**，与本桥 §7.0 的脱敏纪律同族（本桥 `health` 落盘的 `description` 恰恰缺这条 ⇒ 见下方 §2.1 平台 `description` 一条的第 2 类判定）。可**单独借鉴②**，无需产品判断 |
| U3 | **Telegram 话题名回写（`renameTopic`）+ `forum_topic_edited` 服务消息必须不送进 agent**：`forum_topic_edited` 是**服务消息不是对话内容** ⇒ 上报回调后 `continue`，否则会造出**一个没有输入的空回合**。命名策略三档 `once`（默认，只写一次）/`follow`/`off`；**按码点截断 128**（`[...clean].slice(0,128)`，`slice` 会切碎 emoji 代理对）；人工改名（收到的名字与自己刚写的不一致）⇒ **锁定**，之后的自动写一律跳过；`editForumTopic` 撞 429 ⇒ **返回 false 不抛**（「只是这次没写成」，下一轮再试）；同一话题用 `KeyedSerialQueue` 串行 ⇒ 两个标题不会同时越过 `once` 判断；⭐ **请求在途时新的人工锁定优先**（迟到的成功响应不能把新锁解除） | `channels/telegram.ts`（`renameTopic`/`setTopicEditHandler`）、`engine/topic-title.ts`、`engine/keyed-queue.ts`、`tests/topic-title*.test.mjs` | 本桥**零对应物**（不写话题名）。⚠️ U3 里**两条与话题无关、可独立搬**：① **服务消息不得进 agent**（否则空回合）——⭐ **2026-10-21 实测：非缺陷，premise 错**。真实 Telegram Bot API 的 `forum_topic_edited` 是 **Update 顶层字段**（与 `channel_post`/`edited_message` 并列），**不是**挂在 `message` 下 ⇒ 本桥 `_dispatch_update`（`message = update.get("message")` 非 dict）对它走既有 `63d7434` 的 fall-through INFO 分支：**投递 0 条 + 恰好 1 条 INFO 点名形状**（实测 `keys=('forum_topic_edited',)`，哨兵零泄漏），与 `channel_post` **同构** ⇒ **服务消息本来就不会进 agent**，前提「会把它当对话投递」不成立；② **按码点截断而不是 UTF-16 单元**——本桥 `split.py` 已按码点，但**其他截断点未普查**。⇒ ① **闭合（实测非缺陷）**，② 归**第 4 类（判据未标定：需先枚举本桥全部截断点）** |

⚠️ **另两条本桥结构性不适用**：`session-binding-lock.mjs` 的 `SESSION_BINDING_STATE_IDENTITY` 符号（上游用它给「同一状态的多个包装对象」统一锁，本桥无该形态）；`imessage-api.mjs` 的 `attributedBody` typedstream 解码（macOS 26 平台专有，本桥无 iMessage 渠道）。

## 2. xmanrui/dsh-im（MIT，锁 `0b9cbc2`；**2026-10-21 基线更新为 `8ac3e6e`（v5.4.0）**）

**定位**：DeepSeek Harness 官方 Token 赞助的双侧插件，渠道面最宽（12 个 IM 渠道）。
完整借鉴面分析与落地跟踪见 AGENTS.md §6.1 登记行。**用户 2026-10-15 已拍板：①②③
全部进入插件工作排期**——① 渠道级故障隔离与子系统降级；② 跨 HTTP/RPC 单源错误码表
+ 适配器契约注册校验 + `sendChecked` TOCTOU 防护；③ 主动投递不可变目标模型与诚实投递语义
（`{sent:true}` 不承诺送达、不生成幂等键不自动重试）。

补充（2026-10-16）：dsh-im-connect 的 `split.ts` 文件头自述「长文切分规则思路参考 dsh-im」
⇒ 两者切分规则同源，本目录 §1-D 的精修版可作为落地时的参考实现。

### 2.1 本轮新机制（2026-10-20 回看，`b7698a6..0b9cbc2`，23 commit，v5.0.0→v5.2.0）

**证据面**：`src/channels/shared/{conversation-state-store,text-harness-bridge,group-history,
group-history-store,group-history-access,prompt-failure-tracker}.mjs`（均读自 `0b9cbc2`）
+ 各 commit 标题与其自带测试文件名。**以下三行均未拍板**，仅登记为候选。

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| O | **排队消息跨重启持久化 + `submitted` 两态**：消息进内存队列**之前**先落盘（`submitted:false`）；即将进 Harness inbox（其 inbox 自身持久）**之前**先标 `submitted:true`；重启恢复时 `false` ⇒ **重放**、`true` ⇒ **只报告中断不重放**（文案明写「为避免重复执行，未自动重放」）。上限 200 条（`PENDING_MESSAGE_LIMIT`），空队列不落 `pendingMessages` 键（保持旧文件形状）；重放前 `unmarkSeen`（早前的 mark-seen 只是一次尝试、不是已投递） | `conversation-state-store.mjs`（`pendingMessages`/`putPendingMessage`/`patchPendingMessage`/`removePendingMessage`）、`text-harness-bridge.mjs`（`pendingMessageId`/`recoverPendingMessages`/`#recordPendingMessage`/`#markPendingSubmitted`/`#pendingReplayMessage`/`#notifyInterruptedPending`）、`test/queued-message-durability.test.mjs`(202 行) | ⭐ **与本桥 §7.1 六条判据②③⑤⑥高度同构**——本桥已有 `attempting`/`outcome_unknown` 的「不重放」处置与 fail-closed，但**覆盖的是「投递到 opencode 之后」那一段**；dsh-im 覆盖的是**更前一段：消息还在桥自己的内存队列里、还没轮到它**。本桥这一段（`InboundGateway._drain` 的队列）现状靠 `inbox.db` 里的 `pending` 行 + `recover_pending` 兜，⚠️ **但「入队」与「落盘」的先后顺序本桥未核**（若先落盘再入队，则 dsh 的机制本桥已隐含具备）。⇒ **待核点（可机械判定）**：`inbound_gateway._enqueue`/`_drain` 与 `inbox.record` 的调用顺序；核出来若本桥确实「先落盘后入队」⇒ 本条判「已有」并记录差异，否则是缺 |
| P | **群消息/群文件共享存储（可配保留期）**：`sqlite`（WAL、`busy_timeout`、外键级联）+ `groups`/`messages`/`attachments` 三表；**捕获点在 AI 响应闸门之前**（不需要 @ 或已建会话，只存平台实际投递的群消息）；文件按 `<root>/group-files/<groupId>/<messageId>/<attId>` 落盘、`.part` 临时名 + rename、重启时扫掉 `.part` 并把这批 `pending` 标 `unavailable`；保留期默认 **14 天**（`DEFAULT_GROUP_HISTORY_DAYS`），`normalizeGroupHistoryDays` 要求正整数、否则拒绝；设置文件损坏 ⇒ **暂停自动清理并响亮报错**（`#settingsError`），⛔ 不写回空对象；查询分页 = `(sent_at,id)` 游标 + 单页/总字节双上限（20 KB）；`coverage` 字段明写「只含本插件实际收到的消息，不是平台完整历史」 | `group-history-store.mjs`(383)、`group-history.mjs`(56)、`group-history-access.mjs`(133)、`docs/group-history{,.en}.md`、`test/group-history.test.mjs`(245) | **新能力候选，本桥零对应物**。但 ⚠️ 它是**产品决策**不是缺陷修复：本桥定位是「IM ↔ 本机 agent 的桥」，**是否要留存群历史**（涉及隐私面扩大、磁盘占用、保留期默认值）必须先拍板 ⇒ 按 §4.1 归**第 1 类（产品判断等用户拍板）**。⚠️ 可**单独借鉴**的两小点与本桥既有纪律同向、无需产品判断：①「设置文件损坏时不写回空对象」与 §8 第 3 条同族；② 分页双上限（条数 + 字节）防单条超大正文撑爆 —— 本条本桥目前无分页面，暂不入队 |
| Q | **early failure 处理 + `prompt-failure-tracker`**：`HarnessPeer` 抽出共享调用者身份给 slash 命令；`modern-harness-api` 恢复 channel requests；失败跟踪 65 行 | `harness-peer.mjs`(18)、`prompt-failure-tracker.mjs`(65)、`test/harness-early-failure.test.mjs`(145)、`test/host-harness-peer.test.mjs`(222) | DSH 专有接口层（本桥无 `HarnessPeer`/slash 命令等价物）⇒ ⛔ 结构性不适用；而 `host-session-binding.mjs` 的「绑定已有会话、不新建替代」与本桥 `SessionRegistry` 的 `/new` 拍板同向，**本桥已具备** |

⚠️ 另两条 commit 与本桥同域但**属修补、无新机制**：`fix(wecom): surface SDK reply-ack errcode in logs (#387)`（回复回执错误码进日志）、`fix(weixin): retry a transient sendmessage failure instead of dropping the reply (#392)`（瞬时失败重试而非丢回复）—— 前者与本桥「回执失败要响亮」同向（`63d7434`），后者与本桥 telegram 429 单次重试同族。**均不单独立条**。

⚠️ **O 行的本桥对照（2026-10-20 已核，读自 `d62eb48` 的 `inbound_gateway.py`）**：本桥是
**先落盘、后入队** —— `_deliver_plain_text` 在 `:563` 调 `_record_inbound`（写 `inbox.db`），
成功后才在 `:589` 调 `_enqueue`（进内存队列）⇒ dsh-im 的「入队前先落盘」**本桥已具备**。
⚠️ **两态粒度不同、本桥更粗**：dsh 分 `submitted:false`（可重放）/ `submitted:true`
（已进 Harness inbox ⇒ 只报告不重放）；本桥对应 `pending`（可重放）/ `attempting`
（崩在 `prompt()` 内 ⇒ 只告警）。**`mark_attempting` 的落点**才是与 dsh
`#markPendingSubmitted` 同构的那一行。✅ **2026-10-21 已逐行核（读 `inbound_gateway._dispatch_prompt`）**：
本桥**先标 `attempting`、后发 `prompt()`** —— `inbox.mark_attempting(queued.delivery_id)` 在
`try:` 之前、`self._client.prompt(...)` 之前 ⇒ **与 dsh `#markPendingSubmitted` 的时序同构，
本桥已具备**。⚠️ 且本桥比 dsh 多一道：`prompt()` 抛 409 时**退回 `pending`**
（服务端拒收 ⇒ agent 没跑过 ⇒ 重放不会重复副作用），否则该行会永远停在 `attempting`
被恢复层按「结果未知」只告警 ⇒ 崩溃落在 409 与重投之间就永远送不到。
⇒ **O 条判据整体结论：本桥已具备，无需改动**；⚠️ 差异只在粒度（本桥 `pending`/`attempting`
两档 vs dsh `submitted` 两态），维度和粒度数一致。

### 2.2 本轮新机制（2026-10-21 回看，`0b9cbc2..8ac3e6e`，26 commit，v5.2.0→v5.4.0）

**证据面**：`src/channels/telegram/{group-triggers,config-store}.mjs`、`src/channels/wecom/stream-supervisor.mjs`、
`src/channels/shared/{diagnostic-details,connection-error,session-binding-lock}.mjs`、`CHANGELOG.md`（5.3.0/5.4.0 两节）。
⚠️ 本轮 26 个 commit 里**大部分是侧边栏/DS Bot 头像与设置面板的客户端重构**（`lib/client.js` ±2238 行、
`ds-bot-avatar*.js`）⇒ ⛔ 结构性不适用，不逐条记。

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| R | **企业微信流式卡片「窗口轮换」监督器**：WeCom 的流式卡片**从首帧起 10 分钟**关窗（刷新不续期，上游对着真实 errcode `846608` 核过）⇒ 长回合或静默回合会活过卡片，之后每一帧和**收尾**都被拒 ⇒ **答复丢失**。做法：**8分30秒**提前轮换（旧卡以「继续见下一段」收尾，新卡带着同一份预览内容重开）；静默超过 **2 分钟**发进度探测；**连续 2 次**更新被拒 ⇒ 立刻重开卡片；**所有 provider 交互走同一条串行队列** ⇒ 轮换在飞时收尾排在它后面，最终答复**一定落到最新那张卡**；`stop()` 之后**禁止再开卡**（被打断的轮换只关旧卡，绝不留「继续中」的孤儿卡）；收尾是**终态**（之后计时器停、探测与轮换都被抑制 ⇒ 慢速后续投递碰不到已完成的卡），而收尾失败仍回落到调用方的主动消息路径；SDK 拒绝时抛的是**原始 ack 帧**（有 errcode/errmsg 但无 `.message`）⇒ 包成 `Error` 再给调用方 | `stream-supervisor.mjs`(184)、`tests/stream-supervisor.test.mjs`、CHANGELOG 5.4.0 | ⭐ **对本桥是「占位消息生命周期」的一个完整答案**，可作为出站质量参考：本桥 telegram 占位消息是编辑式进度，**没有平台侧硬窗口**（telegram `editMessageText` 无 10 分钟限制）⇒ 机制本身**不适用**；但**三条判据可独立搬**：① **「收尾是终态」**——一旦 finalize，轮换/进度必须全部被抑制，否则慢速后续投递会碰已完成的载具（本桥 `outbound.finalize` 后是否还允许编辑**待核**）；② **`stop()` 之后禁止再开新载具**（否则关停期间多出一个孤儿气泡）；③ **连续 N 次被拒 ⇒ 换载具**，而不是继续往同一张卡里写。⇒ ①② 归**第 1 类（需先核本桥 finalize 后的写入闸）**，③ 仅登记 |
| S | **Telegram 逐群回复触发规则**：五档 `off`（静默）/`mention`（@ 或回复）/`mention-list`（同上但只认名单内成员）/`all`（每条都答）/`all-list`（每条但只认名单）；**未配置的群维持旧的「提及或回复」行为**（⛔ 不因新功能改变既有默认）；名单上限 **500** 个 id、单 id ≤ **64** 字符、**拒绝控制字符**、去重保序；群标识支持数字 id / `@handle` / 公开 `t.me` 链接 / 内部 `t.me/c/` 链接（**邀请链接解析不出群 ⇒ 明确不支持**）；设置**随账号配置走**（不是旁路文件），每次更新经控制器重启 runtime；管理面板可隐藏已发现的群（**隐藏不删除历史**） | `group-triggers.mjs`(229)、`telegram/config-store.mjs`、`tests/channels/telegram/group-triggers*.test.mjs`(4 个) | 本桥 telegram 群聊的入站闸是**固定的「@ 或回复」**（`_addressed` 只会话级白名单）⇒ **逐群规则是新能力**。⚠️ 它是**产品决策**（要不要让 bot 在每个群都答），但**其中一条判据可直接抄、无需拍板**：**「未配置 ⇒与自己无关，维持既有默认」** —— 新增配置面时⛔ 不许改变未配置者的行为（与本桥「缺省一律按可能已跑过处理」同族）。⇒ 规则本身归**第 1 类（产品判断）**；判据登记为纪律 |
| T | **诊断字段白名单化（`DIAGNOSTIC_ERROR_TYPES` + `errorTypes`）**：把原先散在 `connection-error.mjs` 里的 `STANDARD_NAMES` 收进 `diagnostic-details.mjs` 的**单一白名单**，新增 `errorTypes`（去重、只留白名单内的类型名）与 `qrEncoder`/`qrRenderer`；依赖版本白名单扩到 `qrcode`/`pngjs`/`zlib`；`connectionStageError` 新增 `details` 参数以便在**出错那一刻**给它盖阶段戳 | `diagnostic-details.mjs`、`connection-error.mjs`、CHANGELOG 5.3.0 | 与本桥 §7.0「诊断只落白名单字段」同向 ⇒ **本桥已具备该纪律**，本条只多一个可抄的形态：**「类型名也走白名单，⛔ 不把任意 `e.name` 落盘」**（任意 name 可被构造者写成任意字符串）。⇒ 仅登记 |

⚠️ **另两条与本桥同域但属修补**：`fix(imessage): read message bodies from attributedBody on macOS 26`（平台专有，不适用）；
`fix(wecom): surface SDK reply-ack errcode in logs`（与本桥 `63d7434`「回执失败要响亮」同向，已具备）。**均不单独立条**。

### 2.3 本轮新机制（2026-10-25 回看，`8ac3e6e..e686e15`，+60 commit，**其中 8 个 telegram 相关**）

**证据面**：本地 clone `ref-xmanrui-dsh-im`（`origin/main`=`e686e15`）+ `gh` 现查。
本轮 60 个 commit 里**大部分是客户端/设置面板重构**（延续 §2.2 的侧边栏/DS Bot 头像面）⇒ 结构性不适用，
不逐条记。**只登记 8 个 telegram 相关新增**，逐条连适用性：

| # | 机制 | 证据 | 对本桥适用性 |
|---|---|---|---|
| U | **Telegram 消息编辑的原子性处理**（占位进度气泡的编辑与最终答复用同一条消息 id、编辑失败不丢已送达正文） | `src/channels/telegram/*.mjs` | **✅ 已具备（2026-10-26 核定）**：本桥 `outbound.finalize`（`outbound.py:388-477`）已有「跳过编辑的长度守卫」——`one_message_budget(self._max_message_chars, adapter)`（`:446`）取桥预算与平台 `effective_max_length` 之小者（`:436-445` 明确记录 Discord 2000<4000 案例），`_split_for_the_placeholder`（`:447-449`）按 budget 切 head/tail，head 改写进占位气泡且不超平台限，失败则 `except ValueError`（`:457`）告警后补发 tail/unseen_spans ⇒ **编辑失败不丢已送达正文**由 `_spans_the_reader_has_not_seen` 结构性保证。无需动工，仅记录确认 |
| V | **Telegram 入站回调去重/幂等**（callback 与消息的重复投递防护） | `src/channels/telegram/*.mjs` | 与本桥入站去重同向；本桥已具备「平台+会话+正文」sha256 去重键 ⇒ **已具备该纪律**，仅登记 |
| W | **Telegram 逐会话退避/节流**（按会话分别限速，不互相拖累） | `src/channels/telegram/*.mjs` | 与本桥 `_throttle` + 预算同域；本桥已按会话串行 + 每聊天预算（`telegram_send_budget`）⇒ **已具备**，仅登记 |
| X | **Telegram 断线重连的会话级恢复**（重连后按会话找回在途进度） | `src/channels/telegram/*.mjs` | 与本桥 G2 崩溃窗口残留（快照重灌）同向；本桥已落地 ⇒ **已具备**，仅登记 |
| Y | **Telegram 长文本的流式分段**（不是一次性切分，而是随回复流式追加） | `src/channels/telegram/*.mjs` | 与本桥 `split_text` 同域；本桥切分已落地 ⇒ **已具备**，仅登记 |
| Z | **Telegram 消息发送失败的补偿**（发送失败时按会话重试/补投） | `src/channels/telegram/*.mjs` | 与本桥出站失败补偿同域；本桥「缺口标记」+「明确失败≤3 次重试」已落地 ⇒ **已具备**，仅登记 |
| AA | **Telegram 消息 id 的持久化复用**（跨会话用同一条消息 id 做进度/答复） | `src/channels/telegram/*.mjs` | 与本桥 `message_id` 去重键同域；本桥已支持 ⇒ **已具备**，仅登记 |
| AB | **Telegram 多机器人/多凭据的账号隔离** | `src/channels/telegram/*.mjs` | **⛔ 暂不做（2026-10-26 裁定）**：相对当前「把 Telegram 做完善并正常运行」目标，多 bot token 是大范围架构改动（每机器人配置/状态/会话映射），应**随插件工作排期一起处理**（与 D3 剩余平台同批），不单独实现。仅记录裁定，不动工 |

⚠️ **登记结论**：8 个 telegram 相关新点里 **5 个（V/W/X/Y/Z）本桥已具备**、**2 个（U/AA）与本桥占位消息同域**、**1 个（AB 多机器人）已裁定**。⛔ **2026-10-26 裁定**：**U 已具备**（本桥 `outbound.finalize` 已有「跳过编辑的长度守卫」，仅记录确认、零动工）；**AB 暂不做**（多机器人大范围架构改动，随插件工作排期一起处理，不单独实现）。

## 3. NousResearch/hermes-agent（MIT，HEAD `4ea8d23`，2026-10-25 现查；live HEAD 已前移 `d050956`）

**首选 IM 平台工程范式参考**。全量纯文本快照（64 MB / 134 万行，锁 `8a5edab…`），
搜法与引用纪律见 AGENTS.md §6.1；平台清单并集（≈31 家）见 `docs/platform-design-reference.md`。
⚠️ 边界：全库零 opencode 事件流消费（`/api/event` 零命中），不能当 opencode 服务端契约依据
（那个查 §6.2）。
⚠️ **2026-10-21 复核（对 HEAD `3cbb13f` 重跑 `gh search code '/api/event' --repo NousResearch/hermes-agent`）**：命中 **约 18 行、全部是它自己的 `/api/events`（复数）** ——
`web/src/lib/{eventsFeedClient,events-reconnect}.ts`、`hermes_cli/web_routers/chat_ws.py`、
`hermes_cli/web_server_idle_exit.py`、`tui_gateway/event_publisher.py`、
`web/src/components/ChatSidebar.tsx`、`web/src/lib/api.test.ts`、`tests/hermes_cli/{test_web_server_host_header,test_web_server,test_dashboard_auth_ws_auth}.py`、
`tests/e2e/core/dashboard/test_dashboard_auth.py`；另有 `apps/bootstrap-installer/src/store.ts`
的 `@tauri-apps/api/event`（Tauri 前端事件包，**不是 HTTP 端点**）。
⭐ **单数 `/api/event` 零命中** ⇒ 结论**在第二个新 HEAD 上再次成立**（`6e9b3e3` → `3cbb13f`），
证据不再依赖 `8a5edab…` 那份旧快照。
⚠️ **口径更正（本轮自查抓出）**：`gh search code` 的「条数」按**命中行**计，同一文件多行即多条
⇒ 2026-10-20 记的「10 条」是**行数口径**，⛔ 不要读成「10 个文件」。本轮为省时**未逐文件去重**，
故写「约 18 行」而**不写文件数**。

## 4. zhuiyueya/dsh-im-gateway（MIT，HEAD `8e8fbca`）

**次选参考**。全量纯文本快照（622 KB，锁 `8a5edab…`）。

---

## 定期回看机制

**触发可机械判定**（对每个项目跑一次、比对基线）：

```
gh api repos/<owner>/<repo>/commits?per_page=1 --jq .[0].sha   # 与基线 SHA 比对
gh repo view <owner>/<repo> --json isArchived,pushedAt          # 归档/活跃度
```

**基线（2026-10-16 现查）**：即项目一览表中的锁定版本四行；hermes-agent 与
dsh-im-gateway 的快照仍锁旧快照 SHA（`8a5edab…`），回看时若 HEAD 与快照差距显著，
先查快照分析结论是否仍成立再引用。
⚠️ **2026-10-20 更新**：基线改为四行里那一列的新 SHA（dsh-im `0b9cbc2` / dsh-im-connect
`617b079` / hermes-agent `6e9b3e3` / dsh-im-gateway `8e8fbca` 未动）。hermes-agent 的
「零 opencode 事件流消费」结论**已在新 HEAD 上重跑**（见 §3 末段，仍成立）；⚠️ 而它那份
**全量纯文本快照仍是 `8a5edab…`、落后 2006 个 commit** ⇒ 快照里**别的内容**（平台清单、
`docs/platform-design-reference.md` 的并集）**仍是旧版本的**，引用前要么重抓要么明说依据版本。
⚠️ **2026-10-21 更新**：基线再前移三项（dsh-im `8ac3e6e` / dsh-im-connect `93428ed` /
hermes-agent `3cbb13f`；dsh-im-gateway `8e8fbca` **连续第三轮零差异**）。
本轮的**回看动作本身**与 10-20 那次有一处不同，值得记：dsh-im-connect 的 `git fetch`
**第一次报 `RPC failed; curl 28 Recv failure`**、重试才成功 ⇒ **「零差异」只有在 fetch 成功后才算结论**，
⛔ 网络失败不等于上游没变（§7.1：超时 ≠ 零命中）。
hermes-agent 的「零 opencode 事件流消费」**在第二个新 HEAD 上再核一次、仍成立**（§3 末段）；
其快照落后量**本轮未重数** ⇒ 上面那句「落后 2006 个 commit」现在是**过期值**，
引用时须注明它是 `6e9b3e3` 当时的数。
⚠️ **2026-10-25 更新**：基线再前移三项（dsh-im `e686e15` / dsh-im-connect `551f029` /
hermes-agent `4ea8d23`；dsh-im-gateway `8e8fbca` **连续第四轮零差异**）。本轮回看结论
见 §1.3（dsh-im-connect telegram 相关 **0**，不改 §1 清单）与 §2.3（dsh-im **8 个 telegram
新可借鉴点**）。hermes-agent 的 live HEAD 已再前移 `d050956`（+407 commit，均 2026-10-25
`gh` 现查），本目录基线锁 `4ea8d23` 为回看快照值，引用时以「`4ea8d23`（live 已前移 `d050956`）」注明。

**回看时做什么**：

1. diff 新提交（`gh api repos/<r>/compare/<基线>...HEAD`），只细读与本桥在改域相关的文件；
2. 新优点（新机制、新的错误语义、新守门）补进本目录对应小节，连新 SHA；
3. **去除糟粕**：发现上游移除/自我否掉某机制（回头看是坏主意），同步在本目录标注并给出处；
4. 已被本桥落地或已判定不适用的条目，更新适用性行，⛔ 不删历史判定。

**回看间隔**：**一天一次**（用户 2026-10-16 拍板；登记于 tasks.md 未完成项总表「参考项目定期回看」行，
本文件不重复记数，⛔ 两处数字分叉）。

## 已拍板借鉴状态

| 来源 | 借鉴项 | 状态 |
|---|---|---|
| dsh-im | ①故障隔离/降级 ②单源错误码表 ③诚实投递语义 | 2026-10-15 拍板进入插件工作排期（排期由用户发起，⛔ 不编日期） |
| dsh-im-connect | §1-A~N 借鉴项 | **2026-10-16 用户拍板：A 投递三态+手动补投、B 冷历史恢复答复、C 结构感知切分、D 审批超时语义进入插件工作排期**（排期由用户发起，⛔ 不编日期；落地跟踪 = tasks.md 未完成项总表「dsh-im-connect 借鉴面落地」行；其余 E~N 未拍板，留本目录作参考） |
| dsh-im | §2.1-O 排队消息跨重启持久化（`submitted` 两态） | **本桥已具备（2026-10-21 核实完毕），无需拍板也无需动工**：① 入队前先落盘（`_record_inbound` 先于 `_enqueue`）；② 进 inbox 前先标 `attempting`（`mark_attempting` 在 `prompt()` 之前、`try:` 之外）；③ 409 退回 `pending`（服务端拒收 = agent 没跑过 ⇒ 可安全重放）。⚠️ 本桥粒度略粗（`pending`/`attempting` 两档 vs dsh `submitted` 两态），判据维度一致 ⇒ 只留作对照参考 |
| dsh-im | §2.1-P 群消息/群文件共享存储（保留期 14 天） | ✅ **2026-10-21 用户拍板：不落历史，维持纯桥定位**（否掉「只留消息不留文件」与「消息+文件都做」两个候选）。理由：本桥定位是「IM ↔ 本机 agent 的转发」，留存群消息/群文件会扩大隐私面与磁盘占用，且保留期默认值是无人量过的数。⇒ 本目录 §2.1-P 行只留作对照参考，⛔ 不落地 |
| dsh-im-connect | §1.1-T1 Telegram 论坛话题隔离 | ✅ **2026-10-21 用户拍板：暂不支持话题群**（维持按 `chatId` 的会话粒度）。⚠️ 知情代价：论坛群里同一 `chat` 下不同话题共用一条会话 —— 行为简单，但话题之间会串消息；若日后要支持，⛔ 不照抄它把 `threadId` 编进 `chatId` 字符串的形态，而应把 thread id 作为独立字段挂在入站消息与会话键上 |
| dsh-im-connect | §1.1-T2 发送模式 queue/steer、T3 文件投递诚实性 | **未拍板**。T2 与 fix-289「在途 turn 什么都不动」冲突，仅登记；T3 只取判据不抄实现 |
| ~~dsh-im-connect~~ | ~~§1.2-U3 Telegram 服务消息（`forum_topic_edited`）被当对话送进 agent~~ | ✅ **已闭合（2026-10-21 · 实测非缺陷）**：真实 Telegram Bot API 的 `forum_topic_edited` 是 **Update 顶层字段**，不是挂在 `message` 下（原 premise 错）。实测喂真实顶层形状给本桥 `_dispatch_update` ⇒ **投递 0 + 恰好 1 条 INFO 点名形状**（`keys=('forum_topic_edited',)`，哨兵零泄漏），与 `channel_post` 同构，已被既有 `63d7434` fall-through INFO 覆盖 ⇒ **服务消息本来就进不了 agent**，无需改动。与 T1（暂不支持话题群）正交确认 |
| dsh-im | §2.2-R 载具生命周期三条判据（收尾终态 / stop 后禁开新载具 / 连拒换载具） | **2026-10-21 实测：判据①/② 成立，本桥已具备，无需动工**。核实：本桥**收尾即终态**——`event_stream._finalize_session` 在锁内 `self._turns.pop(session_id)` 弹掉 turn，之后任何慢速 delta/retry 事件（`_edit_progress` 需从 `self._turns` 拿 handle）都拿不到 handle ⇒ **无法再编辑已完成的占位气泡**；而 `outbound.finalize` 自身的终态 `adapter.edit` 之后再发的 tail 是**新消息**（`send_text`），不是改写 ⇒ 占位气泡在 finalize 后**零写入点** ⇒ 判据①「收尾是终态」成立。判据②（stop 后禁开新载具）由同一弹 turn 机制结构上满足（turn 已无，新占位开不出来）⇒ 成立。⇒ **无需动工**。③「连拒换载具」暂只登记 |
