# 借鉴项目目录（百家之长汇总）

> **用途**：收录所有可作为借鉴来源的参考项目，供后续执行代码修改时翻查。
> 每条借鉴点标注来源文件（锁定 commit）与对本桥的适用性（已有 / 缺 / 与既有拍板同向）。
> 抄时照 AGENTS.md §6.4：能抄就照抄、抄完记差异与原因；抄来的契约仍要用真实环境验证。

## 项目一览（star / license / archived 均 `gh` 现查，识别日期见各行）

| 项目 | license | stars | 活跃度 | 锁定版本 |
|---|---|---|---|---|
| `MichengAI/dsh-im-connect` | Apache-2.0 | 59 | 2026-10-15 仍在推送 | `e271ec169c7ea743c88562461b3366e62473bf14`（v0.1.77） |
| `xmanrui/dsh-im` | MIT | 2031 | 2026-10-15 仍在推送 | `b7698a6dc8087f7d67480b972e7f45c3773243ad` |
| `NousResearch/hermes-agent` | MIT | 254101 | 2026-10-15 仍在推送 | `f732746bb4b9ccfa4dad90bb21e823fe3e1871ab` |
| `zhuiyueya/dsh-im-gateway` | MIT | 50 | 2026-09-27 推送 | `8e8fbca125dda15b5cdd2ae79ac63a91e21f7c37` |

以上四项均未归档（2026-10-16 `gh` 核验）。本地源码 clone 在宿主临时目录
（`ref-michengai-dsh-im-connect` / `ref-xmanrui-dsh-im`，git 仓非快照）；
hermes-agent 与 dsh-im-gateway 另有全量纯文本快照（文件名含 commit `8a5edab…`，
搜法见 AGENTS.md §6.1）。**引用结论一律连 SHA 一起写**，否则「参考版本变了」
就成了无法复现的借口。

---

## 1. MichengAI/dsh-im-connect（2026-10-16 新收录）

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

**B（冷历史恢复答复）**——API 存在：`OpenCodeClient.messages(session_id, *, limit=100)` ⇒ `GET /api/session/{id}/message` ⇒ `list[dict]`；⚠️ **生产零调用点**（仅 2 处测试，用假桩 ⇒ 形状未被真机核过）。
v2.0.22 消息形状（上游 `packages/schema/src/session-message.ts` @ tag `v2.0.22`）：Base `{id: "msg_" 前缀, metadata?, time.created}`；`{type: "user", text}`；`{type: "assistant", content: [...]}`，其中 `{type: "text", text}` = 正文片段。
⚠️ 关键缺口：失败路径上**拿不到 prompt 的 msg id**（`prompt()` 抛了 ⇒ 无 `data.id`）⇒ §1-C 的「按 requestId 精确匹配回合」**不可直接照搬**；桥只能按 `queued.text` + 时间序定位（弱于参考；同文本重发会歧义）。
⚠️ 未核实：真机 envelope 与字段名（需一次真实 GET；本机无运行中的 opencode server）。
⇒ 待用户拍板：无 requestId 时的对接口径（按文本 + 时间序，还是先搁置待真机）。

**D（审批超时语义）**——现状：`event_stream._on_permission_asked` 只**渲染 + 记账**（`PermissionLedger.record_asked`），**无任何等待 / 超时 / 队列门控**；`PermissionLedger` 无超时（其 docstring 明写「缺超时」非 C4 根因）；档位 pending / already_replied / resolved_upstream / unseen，进程内、重启清空、容量 512。
「审批期间其他文本不进下一轮」在桥上是**间接**由服务端 busy 实现（审批挂起 ⇒ 那轮不结束 ⇒ 新 prompt 得 409 ⇒ 落 `pending`），⛔ 不是桥显式门控。⚠️ a2a `reply_timeout=300s` 是 task 阻塞、**另一回事**。
⇒ 与 §1-F 的差：①「等待上限、到期按**取消**结束」桥没有；②「群聊只认发起者」桥现状未核（callback 只有白名单闸）；③ `accepting` 旗桥没有。
⇒ 待用户拍板：是否引入桥侧定时器「到期按取消」+ 超时值；范围（是否含「群聊只认发起者」）。

---

## 2. xmanrui/dsh-im（MIT，锁 `b7698a6`）

**定位**：DeepSeek Harness 官方 Token 赞助的双侧插件，渠道面最宽（12 个 IM 渠道）。
完整借鉴面分析与落地跟踪见 AGENTS.md §6.1 登记行。**用户 2026-10-15 已拍板：①②③
全部进入插件工作排期**——① 渠道级故障隔离与子系统降级；② 跨 HTTP/RPC 单源错误码表
+ 适配器契约注册校验 + `sendChecked` TOCTOU 防护；③ 主动投递不可变目标模型与诚实投递语义
（`{sent:true}` 不承诺送达、不生成幂等键不自动重试）。

补充（2026-10-16）：dsh-im-connect 的 `split.ts` 文件头自述「长文切分规则思路参考 dsh-im」
⇒ 两者切分规则同源，本目录 §1-D 的精修版可作为落地时的参考实现。

## 3. NousResearch/hermes-agent（MIT，HEAD `f732746`）

**首选 IM 平台工程范式参考**。全量纯文本快照（64 MB / 134 万行，锁 `8a5edab…`），
搜法与引用纪律见 AGENTS.md §6.1；平台清单并集（≈31 家）见 `docs/platform-design-reference.md`。
⚠️ 边界：全库零 opencode 事件流消费（`/api/event` 零命中），不能当 opencode 服务端契约依据
（那个查 §6.2）。

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
