# A2A（Agent-to-Agent）接入说明

> 对应 `tasks.md` 阶段 B 波次的 **B1 · a2a**，实现见
> [`opencode_bridge/adapters/a2a.py`](../opencode_bridge/adapters/a2a.py)，
> 共享的本地 HTTP 服务见 [`opencode_bridge/httpsrv.py`](../opencode_bridge/httpsrv.py)。
>
> ⚠️ **本文件尚未被 `README.md` 链接**（本轮改动的授权范围限定为"只新增 a2a 适配器、
> 它的测试和一个共用模块"，不得改既有文件）。合并时应把下面「一句话」与「安全」
> 两节并进 `README.md` 的平台表与接入小节。

## 一句话

a2a 是本项目**第一个方向相反的平台**：其它十个都是我们主动去连（长轮询 / WebSocket
客户端），**a2a 是我们被调方** —— 它在本机起一个 HTTP 服务，让别的 agent 用 A2A
协议（Google 发起、现由 Linux Foundation 维护的开放规范）把任务送进桥接。

## 配置

`config.json`：

```json
{
  "adapters": {
    "a2a": {
      "bind_port": 9900,
      "bind_host": "127.0.0.1",
      "auth_token": "换成你自己的长随机串",
      "allowed_chat_ids": ["researcher"],
      "reply_timeout": 300,
      "max_turns": 5
    }
  }
}
```

| 键 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `bind_port` | ✅ **是** | 无 | 监听端口。**没有默认值**：Agent Card 里公布的 URL 含端口，端口 0 每次重启都变，对端永远找不到我们。缺了它桥接会明确报错并拒绝启动（`--status` 显示 `missing: ['bind_port']`）。`bind_port: 0` 表示"由操作系统分配"，只适合测试 —— 但它**确实起得来**，所以 `--status` 会显示已配置。 |
| `bind_host` | | `127.0.0.1` | 绑定地址。**放宽到非回环必须先配凭据**，否则会回落回环 + 打 WARNING（见下）。 |
| `auth_token` | | 无 | 共享 Bearer 凭据。配了之后身份退化成 `bearer:<对端 IP>`。 |
| `peer_tokens` | | 无 | `"alice:tok1,bob:tok2"`（或 `{"alice": "tok1"}`）。**每个对端一个凭据**，身份直接取名字（`alice`），限流 / 排障 / `allowed_chat_ids` 都更好定位。推荐优先用它。 |
| `allowed_chat_ids` | | 空（含义取决于 `config_version`，见下） | 走基类统一的授权闸门 `admits()`，比对的是**对端标识**。⚠️ `config.example.json` 里它**就是空**、且 `config_version` 是 `0`（< 2）⇒ 现在是「空 = 全放行」，也就是**任何能访问该端口的对端都能驱动你的 agent**（没配凭据时，对端标识只是来源 IP）。与下面「安全」一节是同一条风险的两个面。⛔ **a2a 不支持 `/pair` 配对**（principal 是 peer，不是人能提供的一个值）⇒ 白名单**只能手填** |
| `reply_timeout` | | `300` | 等 agent 给出终态的秒数（规范 §3.1.1 默认阻塞语义）。非法值回落默认值并告警。 |
| `max_turns` | | `5` | 每个 `contextId` 的入站轮次上限（防乒乓）。**硬顶 20**，配更大会被下调并告警。 |
| `max_tasks` | | `512` | 内存里保留的 task 记录条数（超了 FIFO 丢最老的终态）。 |
| `agent_name` / `agent_description` / `agent_version` | | 主机名派生 / 内置文案 / `0.1.0` | Agent Card 上的字段。 |

`required_tokens = ("bind_port",)`、`outbound_tokens = ("bind_port",)`：a2a **没有
凭据**，出站也**不发网络请求**（`send()` 只是把答复交给正在等待的本地 HTTP 请求）——
但交付的前提是"有一个等待中的请求"，也就是"端口已绑上"，所以两个方向都以
`bind_port` 为前置条件。这样 `--status` 的 `missing: ['bind_port']` 是一条**有用的**
提示，而不是走过场。

## ⚠️ `--status` 的「配好了没有」是怎么判的

a2a 属于**没有凭据可填**的那一类平台，所以"够不够跑"由它自己回答
（`A2aAdapter.config_runnable`），而不是通用规则（"`required_tokens` 的键逐个非空"）：

| `bind_port` | `--status` / `--setup --json` | 桥接 |
|---|---|---|
| `9900`、`"9900"`、`0`…`65535` | 已配置（`missing: []`） | 起得来 |
| 缺键、`""`、空白 | **未配置**（`missing: ['bind_port']`） | 拒绝启动，打 `NO_ADAPTER_MESSAGE` |
| 负数、`> 65535`、非数字 | **未配置**（同左，并另有一条 WARNING 点名这个键） | 同左 |
| 布尔或小数（`true` / `9900.7`） | **未配置**（端口是整数；JSON 里也请写整数） | 同左 |

⚠️ **`bind_port: 0` 算"已配置"**：它由操作系统分配端口（`--status` 的
`bind_port` 字段会显示实际拿到的那个），确实能跑 —— 只是每次重启都变，对端找不到你。
⇒ 生产环境请填一个**固定的**端口。

⚠️ **"键非空"不等于"能绑"**：`bind_port: "nope"` 是非空值，而 `start()` 会拒绝启动。
所以这里由 a2a 自己判定，状态视图不会把一份起不来的配置报成"已配置"。

## ⚠️ 白名单为空时的语义（`config_version` 兜底）

`allowed_chat_ids` 为空时**放不放行**由顶层键 `config_version` 决定：

| `config.json` 的 `config_version` | `allowed_chat_ids` 为空时 |
|---|---|
| **`< 2`**（含没有这个键的老文件、以及模板给的 `0`） | **全部放行**（旧语义）+ 启动时醒目预告「下一版起改为『空 = 全拒』」 |
| **`>= 2`** | **谁都不放行**（新语义） |

⇒ **a2a 用户不会被困死，但也不能靠 `/pair` 自助** —— a2a 的 principal 是 peer（见上表），不在配对支持的平台之列。白名单**只能手填**：填 `peer_tokens` 里用的那些名字（配了 `peer_tokens` 时）或对端标识，然后重启桥。

## ⚠️ 安全：默认无鉴权 = 任何本机进程都能驱动 agent

这是**刻意的默认**，不是疏漏，但必须知道代价：

1. **默认只 bind `127.0.0.1`**（显式常量 `opencode_bridge.httpsrv.DEFAULT_BIND_HOST`，
   没有任何"为了测试方便"就能改成 `0.0.0.0` 的后门）。
2. **但本机 localhost 也是攻击面。** 浏览器里的恶意网页可以直接打
   `http://127.0.0.1:<port>/rpc` —— 无鉴权时任何能发 HTTP 请求的东西都能让
   你的 agent 跑活。所以启动时**必打一条 WARNING**，点名端口、回环地址和"浏览器
   里的恶意网页也能打这个 URL"。
3. **放宽绑定地址需要先配凭据。** 配了 `bind_host: "0.0.0.0"` 却没配
   `auth_token` / `peer_tokens` → **回落回环 + WARNING**，不会开出一个无鉴权的
   局域网端口。
4. 配了凭据后按 **Bearer** 校验（规范 §4.5.3 的 `httpAuthSecurityScheme`），
   并**如实写进 Agent Card 的 `securitySchemes` / `securityRequirements`** ——
   客户端靠这两个字段知道要不要带凭据（规范 §7.3）。401 响应带
   `WWW-Authenticate: Bearer`。
5. **身份只来自凭据，绝不来自请求体。** 未鉴权时身份是 `local:<对端 IP>`，
   并在标识里明说它不是真身份。（这与 ntfy 拿 `title` 当身份被否掉是同一个坑。）
6. Agent Card 与 `/health` **公开**（客户端要靠 Agent Card 才知道要不要带凭据）。

用 `python -m opencode_bridge --status` 可以看到当前的绑定地址、端口、是否启用鉴权。

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/.well-known/agent-card.json` | 否 | Agent Card（规范 §8.2 / §14.3 的规范路径） |
| GET | `/.well-known/agent.json` | 否 | v0.x 兼容别名，非规范路径 |
| GET | `/health` | 否 | 存活探针（非协议的一部分，只是让状态可观测） |
| POST | `/rpc`、`/` | **是**（配了凭据时） | JSON-RPC 2.0（规范 §9） |

已实现的方法：**`SendMessage` / `GetTask` / `ListTasks` / `CancelTask`**。

**未实现但按规范回明确错误码**的方法（Agent Card 里 `streaming` /
`pushNotifications` / `extendedAgentCard` 都声明为 `false`，规范 §3.3.4 要求
未声明的能力被调用时 MUST 回对应错误）：

| 方法 | 错误码 |
|---|---|
| `SendStreamingMessage`、`SubscribeToTask` | `-32004` `UnsupportedOperationError` |
| `CreateTaskPushNotificationConfig` / `Get…` / `List…` / `Delete…` | `-32003` `PushNotificationNotSupportedError` |
| `GetExtendedAgentCard` | `-32004` `UnsupportedOperationError` |

## 握手示例

```bash
# 1. 读 Agent Card
curl -s http://127.0.0.1:9900/.well-known/agent-card.json

# 2. 发一个任务（默认阻塞到 agent 给出终态）
curl -s -X POST http://127.0.0.1:9900/rpc \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer 你的token' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{
        "message":{"role":"ROLE_USER","messageId":"m-1",
                   "parts":[{"text":"帮我看看 main.py 里这段逻辑"}]}}}'

# 3. 之后查任务
curl -s -X POST http://127.0.0.1:9900/rpc -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer 你的token' \
  -d '{"jsonrpc":"2.0","id":2,"method":"GetTask","params":{"id":"task-…"}}'
```

## 常见问题

**Q：进程重启后 A2A 的对话会丢吗？**
不会。`conversation_id = a2a:<对端标识>` 是稳定的，opencode 侧的会话映射存在
`state.json` 里，重启后继续沿用。丢的只是"这一次 HTTP 往返"—— 正在等待答复的
请求会被判 `TASK_STATE_CANCELED`，对端重发即可。

**Q：为什么 `edit()` 恒返回 `False`？**
A2A 规范 §3.1 的 11 个 Core Operations 里**没有任何"编辑已发消息"的操作**；能改的
只有 `Artifact`，而它是通过 `TaskArtifactUpdateEvent` **增量追加**的（§4.2.2）。
返回 `False` 让 core 退化成"再发一条"，这才是 A2A 语义下唯一正确的行为。

**Q：为什么防回环按 `contextId` 计轮次，而不是"看起来像自己发的就丢"？**
后者会误伤用户的真实请求（追问、引用、把上一条答复原样发回来），本仓库在 ntfy
（拿 `title` 当身份）和 email 上都因此吃过亏。`contextId` 是规范 §3.4.1 的原生分组
原语，按它计数是可解释、可关闭（调大 `max_turns`）且不会误伤真实请求的。

**Q：同网段另一台机器能调我吗？**
默认不能（只绑回环）。要开放：先配 `auth_token` 或 `peer_tokens`，再把
`bind_host` 改成 `0.0.0.0` 或具体 IP。注意规范 §7.1 要求生产部署走 HTTPS，
而本项目是纯本机、无 TLS —— 所以只应在**可信网络**里这么用。

## 已知缺口

- 无 SSE 流式（`message/stream` / `tasks/subscribe`）—— 已按规范回 `-32004`。
- 无 push notification —— 已按规范回 `-32003`。
- 无出站调用别的 agent 的能力（本适配器只做被调方）。
- 入站文本**未**加"这是不可信外部输入"的框架前缀。与 ntfy / email 一致（本仓库的
  做法是把信任模型写进文档而不是改写正文）；但 a2a 的入站确实来自任意外部 agent，
  这点比其它平台更值得后续补。
- 同一对端**并发**发多个 task 时，`send()` 按 FIFO 取最老的未完成任务回复
  （`Outbound` 里没有 task id，无法更精确匹配）。这是已知的不精确点。

## 给 A3（inbound-push 入口）的复用说明

a2a 的 HTTP 服务**不是**为它自己写的一次性实现 —— 它在
[`opencode_bridge/httpsrv.py`](../opencode_bridge/httpsrv.py) 里，a2a 只提供
"路径 + 处理器 + 是否鉴权"三样东西（见 `A2aAdapter._routes()`）。

A3 落地时：

1. 建**一个** `HttpServer`（单端口），用 `add_route()` 把各 webhook 适配器的
   `Route` 挂上去 —— 验签 / 解密逻辑都写在各自的 handler 里，传输与生命周期由
   共用模块管。`routes()` 按路径去重，`(path, method)` 是真正的键，所以同一路径
   的 GET 探活 + POST 回调不会冲突。
2. `require_auth` 决定该路由是否走 `authenticate(request) -> 标识 | None`。
   ⚠️ **路由要求鉴权但没配 `authenticate` 时，共用模块 fail closed（一律 401 +
   ERROR 日志）** —— 配错一次绝不会退化成"静默全开"。
3. 默认仍是 `DEFAULT_BIND_HOST = "127.0.0.1"`，`start()` 会把**实际**绑定地址
   （不是请求的地址）打进 INFO 日志。
4. 平台侧不需要写：线程、优雅关闭、在途请求计数、异常兜底（回 500 且不泄栈）、
   body 大小上限、chunked 拒绝。这些都已经在共用模块里，各有测试钉住。

所以 A3 加一个 webhook 平台的增量成本 ≈ **一个 `Route` + 一个 handler**。