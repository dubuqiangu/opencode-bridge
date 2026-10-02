# 如何加一个平台

> 这是本项目重构的**立身目标**：让"再加一个平台"从一次中等规模开发降为声明式工作。
>
> 前置阅读：[`architecture.md`](architecture.md) 的「关键不变量」一节 —— 下面每一步
> 都引用那里的一条编号。

## 成本预期

| 项 | 行数 |
|---|---|
| 适配器本体（`adapters/<name>.py`） | 150~380 行（上限看平台有多少协议特例） |
| 单元测试（`tests/test_<name>.py`） | 40~110 个用例 |
| **连接/ 重连 / 线程 / `stop()` 代码** | **0 行** —— 来自 `transport/` |
| 要改的核心文件（`core.py` / `__main__.py` / 注册表） | **0 行** —— 注册表动态发现 |

参考量级：`ntfy.py` 365 行（最干净的一家）、`telegram.py` 553 行、
`email.py` 1015 行（IMAP+SMTP+MIME，本质上更重）。

## 筛选：先判断这个平台值不值得接

进 B 组必须**同时**满足三条，任一不满足就停下：

1. **纯标准库可行** —— 没有官方 SDK 就得自研，而自研 TLS/加密/图形栈会突破"仅标准库"的立身约束
2. **不需要公网回调地址** —— 违反"纯本机"约束的平台进 [`../tasks.md`](../tasks.md) 阶段 E
3. **活跃度够** —— 主仓 2025 年后停滞的（nostr）、README 自列大量"🚧 Being wired up"的（buzz）

> **教训**：`WhatsApp 官方 API 不存在`这个判断是错的 —— Meta WhatsApp Cloud API 确实存在。
> 真正的分类轴不是"难/易"而是**"入站是否需要公网回调"**。

## 八步

### 1. 先查证协议事实，别凭记忆写

本项目多处凭记忆会写错，且**写错的方式都是静默的**。至少确认：

- 端点 URL 与路径形状（v1/v2 入口的差别可能让失败返回恒 200）
- 认证方式与 header 名（大小写敏感吗？）
- **上限的单位是字节还是字符**
- 保活机制（心跳是 JSON 事件还是 WebSocket 控制帧？）
- 入站字段里**哪些是平台签发的、哪些是用户可控的**（决定防回环怎么做）

查不到就别写，或在 docstring 里明确标注"此处不确定"。**把推断写成事实是最贵的错误。**

### 2. 声明能力（不变量 2）

```python
name = "myplatform"
label = "My Platform"          # 人类可读，会进 --status 表格（注意中文按 2 列宽渲染）
max_message_length = 4000      # 真实上限，不许编
supports_inbound = True
supports_inline_buttons = False   # 没有按钮就False，别谎报
supports_media = False
typed_command_prefix = ""        # 没有斜杠命令就空串
```

### 3. 声明凭据（不变量 1）

```python
required_tokens  = ("address", "password", "api_host")   # 缺任何一个 = 不能工作
outbound_tokens  = ("address", "password")                 # ⊆ required_tokens
```

两条纪律：

- **只列"缺了就不能工作"的键。** 没有默认值的才列（像 email 的 `imap_host` ——
  按域名猜`imap.gmail.com` 对自建邮箱是错的启发式）。宁可报 `missing: imap_host`，
  也不要给一个含混的连接失败。
- **`outbound_tokens ⊆ required_tokens` 是硬不变量**，仓库里有测试守它。历史上正是在
  缺这道防线的情况下，Matrix 忘了声明 `required_tokens`，配得完全正确的用户被判成没配。

### 4. 接线传输层（不变量 8、9）

选一种：

| 传输 | 适用 |
|---|---|
| `PollingTransport` | HTTP 短轮询 / 长轮询（绝大多数） |
| `WebSocketTransport` | WS 事件流（Slack / Discord / Mattermost） |
| `TcpLineTransport` | TCP 行协议（IRC） |

子类**只实现** `_open` / `_next`，不要自己写轮询循环、退避、线程。

```python
self._transport = PollingTransport(
    self._fetch_one,              # 返回一条数据或 NOTHING
    idle_sleep=5.0,               # 本轮没数据时等多久再问
    name=self.name,
)
self._transport.start(self._on_raw)
```

三条容易踩的：

- **`reset_after` 保持默认 0**（连上即重置）。改成正数会让退避在网络闪断时一路涨到上限，
  与其余平台的既有行为不一致。
- **别用长连接伪装成轮询。** 持久流在纯标准库下**无法干净打断** —— 关一个卡在
  `read()` 上的 socket 不保证唤醒那次读，于是每次 `stop()` 都白等一个超时。
  改用"服务端读完就关闭连接"的一次性拉取（如 ntfy 的 `poll=1`），代价是多一次
  HTTP 往返，收益是生命周期干净。
- **一轮返回多条**时用 `EventQueue`（`push_many` + `pop`），别自己写闭包队列。

### 5. 入站过滤，授权闸门在最前（不变量 3）

```python
def _on_raw(self, item):
    if not self._is_echo(item):        # ① 先丢回环
        return
    principal = item.get("...")        # ② 取出平台签发的对端标识
    if not self.admits(principal):     # ③ ★ 闸门必须在产生 Inbound 之前
        return
    self.hooks.on_inbound(Inbound(...))
```

过滤顺序：**回环 → 授权闸门 → 业务过滤 → 产生 Inbound**。别把闸门放在解析之后。

### 6. 防回环只用"平台签发"的字段（不变量 12）

**这是自问自答死循环的源头**：自己发出的消息回到收件箱 → 触发 agent → agent 回复 →
再回到收件箱……

| 平台 | 可用判据 |
|---|---|
| ntfy | 自定义 `tags`（**不可**用 `title`，那是发布者可控字段） |
| email | `Message-ID` 精确命中 + Subject 前缀（**不可**用 `From`，会误伤多地址互发） |
| Slack / Discord | 平台签发的 `user id` / `author.id`（**不可**用 `author.bot`，会误伤别的 bot） |
| Matrix / IRC | 自己的 user id / nick（**不可**用"看起来像自己发的"启发式） |

同时确认：**哪些用户动作不该被当回环丢掉**。比如 email 里用户点"回复"得到的
`Re: [opencode] ...` 必须是**真提问**，不能丢 —— 所以 email 用了三级判据。

### 7. 出站

```python
def send(self, out: Outbound) -> MsgHandle | None:
    for chunk in split_text(out.text, self.max_message_length, prefix_fmt=""):
        ...
        if status_error:
            self._note_send_failure(classify_http(status, detail), detail)  # 不变量 13
            return handle_or_none
        handle = MsgHandle(self._conversation_id(target), mid, self.name)
    return handle
```

- **`prefix_fmt=""`** —— 分段不加 `(i/n)` 前缀（与既有平台一致；前缀是可选项，默认不开）。
- **失败必须记**，否则 `--status` 与日志里什么都看不到（不变量 13）。
- **`edit()` 老实返回 `bool`。** 没有编辑能力就返回 `False`，core 会退化成发新消息。
  假装成功会让长任务的进度更新**静默失效**。

### 8. 别忘用户看得见的四处

- **`README.md`**：配置项表+ 逐平台接入小节（写上该平台最容易踩的坑）+ 能力表
- **`docs/install.md`**：镜像上面那节
- **`plugin/README.md`**：凭据键一句话
- **`--setup` 引导**：**默认不要动。** `/setup` 菜单是`core.py` 里**刻意维护的冻结文案**
  （`_SETUP_GUIDES` / `SETUP_MENU_TEXT`），只列三平台；配置齐全一律用 `--status` 核对。

> 历史教训：曾有 **9 处**文档在 T2.1 落地后仍写着"仅支持主动发送"，用户照着走会以为
> 入站不可用、也不知道要配 `app_token`。**新增平台时请顺手核对旧说法是否已被证伪。**

## 测试

- **零真实外网**。但**允许起真服务器绑 `127.0.0.1:0`** —— 本项目对IRC 用的是这个手法，
  真服务器比 mock 有价值得多（测试里服务端用独立实现算一次握手，两边各算一遍才有意义）。
- 必须覆盖：注册表可发现、`required_tokens`/`outbound_tokens` 不变量、回环被丢、
  闸门在产生 Inbound 之前、上限单位（**中文/emoji 用例**，字节上限平台尤其要）、
  `edit()` 返回 `False`、`stop()` 干净且**断言耗时上界**、畸形输入不崩。
- ⚠️ **本项目铁律（不变量 15）**：断言"等了多久"**一律优先断言内部状态**而不是
  wall-clock 差分。墙钟只允许做**下界**断言。这已经栽坑三次。
- ⚠️ **测试自己会骗你**：造一个"脚本用完就返回 200"的假响应器时，它会把第二次调用的
  失败**掩盖掉** —— 本项目因此误判过一次`send_result` 的结果。凡是断言失败路径，
  检查你的替身会不会在第二次调用时悄悄返回成功。

## 验收命令

```powershell
cd D:\workSpace\python\aicode\opencode\opencode-bridge
python -m unittest tests.test_<name>          # 专项
python -m unittest discover -s tests          # 全量（不得出现新失败）
python -m compileall -q opencode_bridge      # 必须 exit 0
python -m opencode_bridge --status            # 新平台必须**自动出现**，且表格列对齐
```

**新平台必须自动出现在 `--status` 里** —— 注册表是动态发现的，**全程未改任何平台注册代码**
就是这一层的验收标准（Matrix 当年就是这么验收的）。

## 完成后

1. 在 [`../tasks.md`](../tasks.md) 的阶段 B 表格勾上 ☑，并**追加一条进度日志**，
   写清协议事实来源、踩到的坑、以及**你做的取舍与理由** —— 那部分比"做了什么"值钱得多。
2. 更新上节列出的四处文档。
3. 扫一遍过时说法（尤其"仅支持主动发送"这类假话会不会复活）。