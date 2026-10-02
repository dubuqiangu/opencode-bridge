# opencode-bridge

[![CI](https://github.com/dubuqiangu/opencode-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/dubuqiangu/opencode-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把 Telegram / Slack / Discord 的消息桥接到本机 [opencode](https://opencode.ai) 服务：你在 IM 里发一句话，本机的 agent 就在你的目录里干活，过程与结果再流式回到同一个会话里。**纯 Python 标准库实现，零第三方依赖。**

## 🚀 一行安装

### OpenCode 原生插件命令（推荐）

```text
opencode plugin add github:dubuqiangu/opencode-bridge
opencode plugin update github:dubuqiangu/opencode-bridge
opencode plugin remove github:dubuqiangu/opencode-bridge
```

- `opencode plugin add <package>` 的 package 参数是 **npm registry or Git package specifier**；`update` / `remove` 都用**同一条完整 specifier**（上面这条，不是短 id）。安装 = 把本仓库作为插件包装进全局配置。
- **装完只剩两步**：填 bot token → `opencode service restart`（重启会打断当前会话，**需你点头同意**才执行）。
- 插件**首次启动自动自举**：把包内 Python 源码与 `config.example.json` 铺到稳定 bridge 目录（Windows `%USERPROFILE%\.config\opencode-bridge`，Unix `${XDG_CONFIG_HOME:-$HOME}/.config/opencode-bridge`），已有 `config.json` **绝不覆盖**——无需任何安装脚本。
- **更新**用 `opencode plugin update github:dubuqiangu/opencode-bridge`（**建议在用户主目录 `~` 下执行**；工作区目录偶发 `Plugin is not configured`，换到 `~` 重试即可）；`plugin list` 显示的 commit 可能落后 update 一拍，再执行一次 update / list 对齐。
- Python **3.10+ 仍需本机自带**（插件不安装 Python）。

**AI Agent 一行（跨平台，由 Agent 按文档执行）：**

```
帮我安装 opencode-bridge：https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/install.md
```

**以后更新，把这行发给它即可：**

```
帮我更新 opencode-bridge：https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/update.md
```

**不想用原生命令？直接跑安装脚本（Windows PowerShell 5.1+）：**

**Windows（PowerShell 5.1+，直接粘贴回车）：**

```powershell
iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 | iex
```

**macOS / Linux：**

```bash
curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash
```

安装脚本会自动完成三件事（不联网执行任何安装后动作，**绝不会**替你执行 `opencode service restart`）：

1. 把本仓库 clone（或本地复制）到 **bridge 目录**，并从 `config.example.json` 生成 `config.json`；
2. 把插件文件 `index.ts` + `package.json` 安装到**插件目录**；
3. 写入插件 `config.json`（内容为 `{"bridgeDir": "<bridge 目录的绝对路径>"}`），已有 `bridgeDir` 时不覆盖（加 `-Force` / `--force` 可强制覆盖）。

安装后的固定布局：

| | Windows | macOS / Linux |
|---|---|---|
| bridge 目录（= 仓库 clone，`cwd`） | `%USERPROFILE%\.config\opencode-bridge` | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge` |
| 插件目录 | `%USERPROFILE%\.config\opencode\plugins\bridge` | `$HOME/.config/opencode/plugins/bridge` |

运行方式：插件随 opencode 启动，以 **cwd = bridge 目录** 执行 `python -m opencode_bridge`（单例，多 location 只起一次）；也可以在 bridge 目录里手动运行。

本地开发安装 / 自测（推送前验证用）：

```powershell
# Windows（-Source 指向本地仓库目录）
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Source <本地目录> -Force
```

```bash
# macOS / Linux
bash install.sh --source <本地目录> --force
```

### 一行命令打不开？

当 `raw.githubusercontent.com` 不可达（DNS 污染 / 被墙）时，按下面的顺序换兜底方式。

**A. 标准形式**（GitHub 域名可达时）：

```powershell
iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash
```

**B. 兜底：jsDelivr 镜像**（本机 `raw.githubusercontent.com` DNS 污染、jsDelivr 可达时）。
注意：jsDelivr 对 `.ps1` 返回 `application/octet-stream`，`iwr | iex` 会按**本地 ANSI 编码**解码 UTF-8 源码导致解析失败，所以**必须**先落到临时文件再用 `-File` 执行：

```powershell
$p="$env:TEMP\ocb-install.ps1"; iwr 'https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.ps1' -OutFile $p; powershell -NoProfile -ExecutionPolicy Bypass -File $p; Remove-Item $p
```

bash 对应：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.sh -o /tmp/ocb.sh && bash /tmp/ocb.sh
```

**C. 兜底：直接 git clone**（GitHub 443 可达但 raw 被墙时最简单）：

```powershell
git clone https://github.com/dubuqiangu/opencode-bridge "$env:USERPROFILE\.config\opencode-bridge"; powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.config\opencode-bridge\install.ps1" -Force
```

## 安装后做什么

1. **填 token**：编辑 bridge 目录下的 `config.json`
   - Windows：`%USERPROFILE%\.config\opencode-bridge\config.json`
   - macOS / Linux：`$XDG_CONFIG_HOME/opencode-bridge/config.json`（或 `~/.config/opencode-bridge/config.json`）

   至少填 `adapters.telegram.bot_token`（Telegram 里找 [@BotFather](https://t.me/BotFather) 创建 bot 获取），并把 `adapters.telegram.allowed_chat_ids` 设为你的 chat id（给 [@userinfobot](https://t.me/userinfobot) 发消息即可拿到自己的 id）。
2. **重启 opencode 服务让插件生效**：

   ```bash
   opencode service restart
   ```

   （或者关闭并重新打开 opencode TUI。安装脚本出于安全不会替你重启。）
3. **（可选）连通性自检**（只查 `/api/info`，不创建任何会话）：

   ```powershell
   cd <bridge 目录>
   python -m opencode_bridge --check
   ```

之后在 IM 里给 bot 发消息即可开始对话；收到权限请求时用 `/approve` / `/deny` 回复。

## 接入平台引导

各平台当前能力一览：

| 平台 | 接收消息 | 发送消息 | 状态 |
|---|---|---|---|
| Telegram | ✅ 长轮询 | ✅ | 完整，可双向对话 |
| Slack | ⬜ TODO | ✅ | 仅发送 |
| Discord | ⬜ TODO | ✅ | 仅发送 |

> 填好 token 后，可在 bot 里发送 **`/setup`** 查看 / 重温下面这套引导；
> `/setup telegram`、`/setup slack`、`/setup discord` 可直达对应平台的分步引导。

### 配置文件位置

| | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.config\opencode-bridge\config.json` |
| macOS / Linux | `${XDG_CONFIG_HOME:-~/.config}/opencode-bridge/config.json` |

### Telegram（支持双向）

1. 打开 Telegram，找 **@BotFather** → 发送 `/newbot`
2. 依次设置显示名、用户名（**必须以 `bot` 结尾**），复制返回的 token（形如 `123456789:AA...`）
3. 找 **@userinfobot** → 发送任意一句话 → 复制返回的**纯数字** chat id
4. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": {
     "telegram": { "bot_token": "123456789:AA...", "allowed_chat_ids": [123456789] }
   }
   ```

   `allowed_chat_ids` 是数组，**数字不要加引号**
5. 执行 `opencode service restart`
6. 在 Telegram 给你的 bot 发一句 `hi`，收到回复即成功

### Slack（支持双向对话 · Socket Mode，无需公网地址）

1. 打开 <https://api.slack.com/apps> → **Create New App** → **From scratch** → 选 workspace
2. 左侧 **Socket Mode** → 打开 **Enable Socket Mode**
3. 左侧 **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**
   → 命名 → 勾选 `connections:write` → 复制（`xapp-` 开头，**入站必需**）
4. 左侧 **OAuth & Permissions** → **Scopes** → **Bot Token Scopes** → **Add an OAuth Scope**，添加：

   ```
   chat:write
   channels:history
   im:history
   ```

   （要 `@` 才响应加 `app_mentions:read`；用私有频道加 `groups:history`；
   要往尚未加入的公开频道主动发言再加 `chat:write.public`）

5. 同页顶部 **Install to Workspace** → **Allow** → 复制 **Bot User OAuth Token**（`xoxb-` 开头）
   ⚠️ 之后**每改一次 scope 都要回来点一次 Reinstall to Workspace**，否则新 scope 不生效
6. 左侧 **Event Subscriptions** → 打开 **Enable Events**
   → **Subscribe to bot events** → **Add Bot User Event** → 添加 `message.channels`、`message.im`
7. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "slack": { "bot_token": "xoxb-...", "app_token": "xapp-..." } }
   ```

8. 在目标频道输入 `/invite @你的bot`（私有频道同样用 `/invite`；私聊可直接发消息）
9. 执行 `opencode service restart`
10. 在频道里发一句普通文字，收到回复即成功

> **两个常见坑**
> - **Event Subscriptions 没打开、或事件没加在 *bot events* 下，会「静默收不到」且不报错** —— 最常见的问题。事件必须用 **Add Bot User Event** 添加（不是 user event）。
> - **只填 `bot_token` 也能启动，但那只发不收**：入站必须有 `app_token`（`xapp-`）。桥接会在日志里记一条 warning。
>
> 另外：开启 Socket Mode 后事件 100% 走 WebSocket，即使之前填过 Request URL 也不会走 HTTP（两者互斥）；Socket Mode 也不需要校验签名。

### Discord（⚠️ v1 仅支持主动发送）

1. 打开 <https://discord.com/developers/applications> → **New Application** → 左侧 **Bot**
2. **Reset Token** → 复制 token
3. 同一页把 **Privileged Gateway Intents** 下的 **MESSAGE CONTENT INTENT** 打开（**必需**）
4. 左侧 **OAuth2 → URL Generator** → 勾选 scope: `bot` → Permissions: **Send Messages**
5. 用生成的 URL 把 bot 邀请进你的服务器
6. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "discord": { "bot_token": "..." } }
   ```

7. 执行 `opencode service restart`

> 能力说明：v1 仅实现主动 `send`/`edit`，入站轮询（`GET /channels/{id}/messages`）为 TODO，当前**无法在 Discord 里与 bot 双向对话**。

### Matrix（支持双向对话 · `/sync` 长轮询，无需公网地址）

Matrix 没有 Slack 那种"建 App 再邀请进频道"的模型 —— 这里直接用**你的账号（或一个专门的服务账号）**作为对端，所以第 1 步是取这个账号的凭据。

1. 拿到 `access_token` 与自己的 user id，二者任一都能取到：
   - **Element Web**：登录后 F12 → **Application → Local Storage**，`access_token` 与 `user_id` 都在里面
   - **直接登录**：`POST /_matrix/client/v3/login`（`{"type":"m.login.password", ...}`），响应里的 `access_token` / `user_id`
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "matrix": {
     "homeserver": "https://matrix.example.org",
     "access_token": "syt_...",
     "user_id": "@me:example.org"
   } }
   ```

3. 执行 `opencode service restart`
4. 私聊这个账号，或在**它已经加入**的房间发一句 `hi`，收到回复即成功

> **`user_id` 必须填对** —— 它是过滤自己回声的依据。留空或填错时，桥接会把自己发出的消息
> 当成入站消息收回来，形成无限回环（表现为自己跟自己对话）。排查时看日志里的
> `slack/matrix: dropping ...` 与入站条数是否异常增多。
>
> `allowed_chat_ids` 填**房间 id**（形如 `!abcDEF:example.org`），不是 user id。
> 另：编辑消息用的是 MSC2676 兼容写法（多发一条带 `* ` 前缀与 `m.replace` 关系的事件），
> 不支持该写法的客户端会当成一条新消息 —— 内容不丢，但会多一条。

## npm 方式（占位 / 待发布）

opencode 也支持通过 npm 包名启用插件——在 `opencode.json` 中配置：

```json
{ "plugins": ["opencode-bridge"] }
```

**本包当前尚未发布到 npm**，上面的写法暂时不可用。维护者发布流程：`npm login` 后在仓库根目录执行 `npm publish`（根 `package.json` 已就绪，`files` 只包含 `plugin/index.ts`、`plugin/package.json`、`README.md`、`LICENSE`）。发布之前请使用上面的「一行安装」，或手动把 `plugin/` 下的 `index.ts` + `package.json` 复制到插件目录。

## 1. 架构

```
 Telegram Bot ──长轮询 getUpdates──┐
 Slack (bot)   ──增量轮询 (TODO)───┤   事件 (SSE: GET /api/event)
 Discord (bot) ──增量轮询 (TODO)───┼──────────────────────────────┐
                                  ▼                              ▼
                     ┌──────────────────────────┐    ┌────────────────────────┐
                     │   opencode-bridge 进程    │───▶│   opencode HTTP API    │
                     │  adapters 轮询线程         │    │  /api/session          │
                     │  core (队列/命令/流式)     │◀───│  /api/session/{id}/... │
                     │  SSE 事件分发线程          │    │  /api/event (SSE)      │
                     └──────────────────────────┘    └────────────────────────┘
                                  │                              │
                     state.json   │  conversation ↔ session      ▼
                     (持久化)      │                        opencode agent
                                  ▼                     （读写你的文件 / 执行 shell）
```

- **上行**：适配器把 IM 消息转成 `conversation_id`（如 `chat:55`、`channel:C123`）+ 文本，交给 `core`；`core` 为每个会话维护（或复用）一个 opencode session，再调用 `prompt()`。
- **下行**：一条 SSE 读线程消费 opencode 事件（`session.text.delta` / `session.execution.succeeded` / `permission.asked` …），把流式增量**节流编辑**到同一条 IM 消息上，任务结束后定稿为最终结果。
- **排队**：会话忙（HTTP `409`）时消息进入 per-conversation 队列，`session.idle` 后自动补发；同一会话严格串行，不同会话可并发。

## 2. 前置条件

- Python **3.10+**（仅标准库，无需 `pip install`）
- 本机可运行的 `opencode` CLI 与服务（`opencode serve`，默认 `http://127.0.0.1:4096`）
- 至少一个消息平台的 Bot Token：
  - Telegram：找 [@BotFather](https://t.me/BotFather) 创建 bot 获取 `bot_token`
  - Slack：在 <https://api.slack.com/apps> 创建 App 获取 Bot Token
  - Discord：在 <https://discord.com/developers/applications> 创建 Application 获取 Bot Token
- （推荐）一个专用的工作目录，例如 `D:\work`，供 session 使用
- `git`（一行安装脚本用于 clone；本地开发安装时可用 `-Source` / `--source` 指向本地目录）

## 3. 手动安装 / 快速开始

一行安装会自动完成本节的复制与布局；若想手动部署，从仓库根目录执行：

```powershell
# 1) 复制配置模板（在 bridge 目录下）
copy config.example.json config.json

# 2) 编辑 config.json：至少填 telegram.bot_token，并把 allowed_chat_ids 设为你的 chat id
#    （在 Telegram 里给 @userinfobot 发消息即可拿到自己的 id）

# 3) 连通性自检（只查 /api/info，不创建任何会话）
$env:OPENCODE_URL='http://127.0.0.1:4096'
$env:OPENCODE_PASSWORD='<你的服务密码>'
python -m opencode_bridge --check

# 4) 正式运行（cwd 必须是 bridge 目录，以便读到 ./config.json 与 ./state.json）
python -m opencode_bridge
#    Ctrl+C 干净退出（停掉所有适配器、关闭 SSE、join 线程）
```

命令行参数：

```
python -m opencode_bridge [--config PATH] [--verbose] [--check]
```

| 参数 | 说明 |
|---|---|
| `--config PATH` | 指定配置文件（默认：`$OPENCODE_BRIDGE_CONFIG` → `./config.json`） |
| `--verbose` | 输出 DEBUG 日志 |
| `--check` | 只做连通性自检后退出（不创建 session、不启动适配器） |

退出码：`0` 正常（含**未配置任何 adapter** 的优雅退出，会打印提示）；`1` 自检失败 / 未捕获异常 / adapter 构建失败。

## 4. `--check` 输出示例

```text
opencode service OK
  version : 2.0.21
  pid     : 23060
  url     : http://127.0.0.1:4097
```

失败时（服务没起、密码错误）打印异常日志并以退出码 `1` 结束：

```text
ERROR opencode_bridge: opencode-bridge 异常退出
Traceback (most recent call last):
  ...
opencode_bridge.opencode_client.OpenCodeError: GET /api/info -> HTTP 401
```

## 5. 命令表

在会话里直接发送（Telegram 群内允许 `@BotName` 后缀，例如 `/new@your_bot`）：

| 命令 | 行为 |
|---|---|
| `/help` | 输出用法（含全部命令 + 一句安全提示） |
| `/new` `/reset` | 删除当前 session 并新建，回 `已新建会话 <sid 前 12 位>` |
| `/stop` | `interrupt` 当前正在执行的任务；失败回错误 |
| `/status` | 显示 `session_id / agent / model / cost / tokens / directory` |
| `/cd <目录>` | 切换该会话的工作目录（写入 `state.json`）并重建 session，回 `已切换到 <目录>` |
| `/approve <请求ID>` | 回复权限请求：允许一次 |
| `/approve <请求ID> always` | 回复权限请求：总是允许 |
| `/allow <请求ID>` | 同 `/approve` |
| `/deny <请求ID>` | 拒绝权限请求 |
| `/setup` | 查看分平台接入引导（`/setup telegram` / `slack` / `discord` 可直达） |
| 其它以 `/` 开头 | 视为未知命令，回一条用法提示（**不会**转发给模型） |

普通文本直接发送即可；权限请求也会以文字形式推送（形如 `🔐 权限请求 … 回复: /approve xxx`），用上面的命令回复。

### 在 opencode 里问接入引导（不用先进 bot 窗口）

接入前 bot 还没起不来，所以引导在 **opencode 内**也能拿——两种等价入口：

- **工具 `bridge_setup`**：会话里直接问「怎么接 Telegram / bot_token 填哪里 / 桥接配好了吗」，agent 会调用它，返回**配置状态（✓/✗ 各平台）+ 真实配置文件路径 + 该平台分步引导**。
- **命令 `/bridge-setup [平台]`**：出现在 opencode 斜杠命令面板（`/bridge-setup telegram`）。

命令行等价物（供脚本/文档引用）：

```bash
python -m opencode_bridge --setup                 # 平台菜单
python -m opencode_bridge --setup telegram        # 单平台分步引导
python -m opencode_bridge --setup --json          # {config_path, platforms:[{key,label,configured}]}
```

三处入口共用 `core.py` 的同一份冻结文案（`setup_reply()`），不复制副本；`--setup` 不连接 opencode、不需要 token，因此在配置之前就能运行。

## 6. 配置项说明

`config.json`（模板见 [`config.example.json`](config.example.json)）：

| 键 | 默认值 | 说明 |
|---|---|---|
| `opencode_url` | `""` | 服务地址；留空则读环境变量 `OPENCODE_URL` → `~/.local/state/opencode/service.json` 等自动发现 |
| `opencode_password` | `""` | 服务密码；留空则读环境变量 `OPENCODE_PASSWORD` → 服务注册文件 |
| `opencode_directory` | `"."` | 新建 session 的工作目录（可用 `/cd` 按会话覆盖） |
| `opencode_agent` | `""` | 指定 agent；留空使用服务端默认 |
| `permissions_mode` | `"ask"` | `ask` / `allow` / `deny`，对应新建 session 的权限规则集 |
| `log_level` | `"INFO"` | 日志级别（`--verbose` 会强制 `DEBUG`） |
| `state_path` | `"state.json"` | `conversation_id ↔ session_id` 与 per-conversation 元数据的持久化文件（原子写） |
| `bridge.edit_interval_seconds` | `1.5` | 流式增量编辑同一条 IM 消息的最小间隔（秒），用于节流 |
| `bridge.max_message_chars` | `4000` | 单条消息编辑的长度上限；定稿超过该长度时改为**直接发送**（交给适配器分块） |
| `adapters.telegram.bot_token` | `""` | Telegram bot token（`@BotFather`） |
| `adapters.telegram.allowed_chat_ids` | `[]` | **白名单**：空数组 = 全部允许；非空则只响应列表内的 chat id |
| `adapters.slack.bot_token` | `""` | Slack bot token（`xoxb-`）：**出站必需**；入站还需下面的 `app_token` |
| `adapters.slack.app_token` | `""` | Slack **app-level token**（`xapp-`）：Socket Mode 入站专用，缺它时降级为只发出站 |
| `adapters.matrix.homeserver` | `""` | Matrix homeserver 根地址（如 `https://matrix.example.org`，尾部斜杠会自动去掉） |
| `adapters.matrix.access_token` | `""` | Matrix access token（入站与出站都必需） |
| `adapters.matrix.user_id` | `""` | 自己的 Matrix user id（如 `@me:example.org`）：用于**过滤自己的回声**，留空会把自己发的消息当入站消息收到（无限回环） |
| `adapters.discord.bot_token` | `""` | Discord bot token（v1 仅支持主动发送，见"已知限制"） |

环境变量：`OPENCODE_URL` / `OPENCODE_PASSWORD` / `OPENCODE_DIRECTORY` 会覆盖配置文件中的同名项；`OPENCODE_BRIDGE_CONFIG` 指定配置文件路径。

插件目录下的 `config.json`（**仅脚本安装方式**由安装脚本生成；原生 `plugin add` 安装不产生它，插件改用自举稳定目录）：

| 键 | 说明 |
|---|---|
| `bridgeDir` | bridge 目录（= 仓库 clone 目录）的绝对路径；插件以它为 `cwd` 拉起 `python -m opencode_bridge`，日志与锁文件也写在这里 |

## 7. 安全须知

1. **IM 是低信任入口**：`permissions_mode` 默认 `ask`（每次敏感操作都要确认），**不要**轻易改成 `allow`——那等于允许任何能给 bot 发消息的人以你的权限执行任意操作。
2. **务必配置 `allowed_chat_ids` 白名单**：尤其在群里使用 bot 时，未列入白名单的 chat 的消息会在适配器层被直接丢弃。
3. **`opencode_url` 留空时会自动读取 `~/.local/state/opencode/service.json`**（含密码），因此本桥接**只应在本机 / 可信网络上运行**，不要把端口暴露到公网。
4. **桥接进程拥有和你一样的文件与 shell 权限**：它驱动的是本机 opencode agent，请只在你信任的目录、你信任的 bot token 下运行；配置文件与 `state.json` 含敏感信息，请妥善保管。

> 仓库已通过 `.gitignore` 排除 `config.json` / `state.json` / `plugin/config.json` / `*.log` / `.bridge-plugin.lock` 等运行态文件，请不要把它们提交上来。

## 8. 已知限制

- 流式显示依赖服务端事件 `session.text.delta`；若服务端不推送该事件，只能在任务结束时（`session.execution.succeeded` / `session.idle`）一次性收到结果。
- **Discord 的接收端尚未接入**：只实现了主动 `send` / `edit`，入站轮询（`GET /channels/{id}/messages` 增量回放）为 TODO，因此当前只有 Telegram 与 Slack 可以双向对话。
- 长任务在 IM 侧只有"一条进度消息 + 节流编辑"，**没有**中间逐步流式；工具调用只记录进内部 `tool_trace`，不会逐条推送（避免刷屏）。
- 权限请求 v1 只发文字提示，不使用 inline buttons（跨平台行为不一致）。
- 一条进度消息编辑失败时不会降级为重复发送（避免刷屏），定稿消息才具备 `send` 兜底。
- **同一种安装方式只能选一种**（原生 `plugin add` 与脚本安装并存会重复加载），切换方式需先清理旧的那份，见「11. 卸载」。
- 若稳定 bridge 目录是**脚本安装留下的 git clone**，第 4 级自举按设计完全不碰它 —— 此时 `opencode plugin update` 只更新插件壳，Python 侧需自行 `git -C <稳定目录> pull`。
- `bridge-output.log` 由 Python 按系统代码页写出（中文 Windows = GBK），非 UTF-8 环境用文本编辑器直接打开会显示乱码。

## 9. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `--check` 报 `HTTP 401` | 密码不对，或 opencode 服务未启动。确认服务在跑、`OPENCODE_PASSWORD` / `service.json` 中的密码一致 |
| 连接被拒绝（`Connection refused`） | 服务没起或端口不对；`opencode serve` 后确认 `url` |
| 发消息没有回复、日志见 `409` | 会话正忙（上一个任务还在跑）。消息会自动排队，当前任务结束后补发；也可 `/stop` 打断当前任务 |
| 日志见 `provider.transport` 重试（`⏳ 重试中 (attempt N): ...`） | 上游模型服务不可达 / 超时，opencode 正在按退避重试；检查网络与 provider 配置 |
| `没有任何可用适配器` | 配置未完成**不再报错退出**（exit 0 + 提示）。填好各平台 `bot_token`（见「接入平台引导」）后重启即可生效；也可在插件 `config.json` 设 `enabled: false` 暂停拉起 bridge |
| bot 无响应但日志有 `dropped message from non-whitelisted chat` | 该 chat 不在 `allowed_chat_ids` 白名单中 |
| 会话行为异常 / 想清空上下文 | 发送 `/new`（或 `/reset`）重建 session |
| 插件没拉起 bridge | 看 `<bridgeDir>\bridge-plugin.log` 与 `<bridgeDir>\.bridge-plugin.lock`；确认插件目录里的 `config.json` 中 `bridgeDir` 指向真实存在的目录 |
| 原生安装后日志里找不到「自举」字样 | 自举发生在配置解析阶段（`logDir` 还没确定），按设计只进 opencode 主日志：`~/.local/share/opencode/log/opencode.log` 搜 `[bridge-plugin]`。可直接检查稳定目录是否已铺好：`<稳定目录>\opencode_bridge\__init__.py` 是否存在 |
| `opencode plugin check …` 报 `Plugin is not configured` | 刚 `add` / `remove` 后偶发的瞬态错误，**重跑一次即可**；`opencode plugin list` 显示的版本才是权威状态 |
| `opencode plugin list` 里出现两条 `opencode-bridge` | 说明脚本安装产物与原生条目并存，见「11. 卸载」末尾的说明，只保留一种 |
| `bridge-output.log` 打开是乱码 | 该文件由 Python 按**系统代码页**写出（中文 Windows = GBK/cp936）。用 GBK 打开即可；这是既有行为，不影响功能 |
| 想看插件在 opencode 里的日志 | `~/.local/share/opencode/log/opencode.log` 中搜 `[bridge-plugin]` |

## 10. 开发与测试

```bash
# Python 单元测试（标准库 unittest，无需安装任何依赖）
python -m unittest discover -s tests -v

# 插件自检（25 个场景：生命周期 + bridgeDir 四级解析 + 自举铺装 + opencode 内引导面；离线运行，不碰 opencode 服务）
cd plugin
bun harness.ts

# 打包体检（npm 占位包，不发布也可本地验证）
npm pack --dry-run

# 安装脚本本地自测
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Source <本地目录> -Force   # Windows
bash install.sh --source <本地目录> --force                                                 # macOS / Linux
```

CI（GitHub Actions）会在 `ubuntu-latest` / `windows-latest` × Python 3.10 / 3.12 上跑 `compileall` + `unittest`，并做 `install.sh` / `install.ps1` 的语法检查。

## 11. 卸载

**原生方式（首选，一条命令）：**

```bash
opencode plugin remove github:dubuqiangu/opencode-bridge
```

它只把插件从全局配置里摘掉并清掉包缓存，**不动**稳定 bridge 目录（`config.json` / `state.json` / 日志都在里面）。彻底删除：

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.config\opencode-bridge"     # Windows
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge"                 # macOS / Linux
```

**脚本安装方式的卸载（若你是用 install.ps1 / install.sh 装的）：**

```powershell
# Windows（删除插件目录）
Remove-Item -Recurse -Force "$env:USERPROFILE\.config\opencode\plugins\bridge"
# 或重新下载安装器执行 -Uninstall
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path $env:TEMP 'opencode-bridge-install.ps1'; iwr 'https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1' -OutFile $p; & powershell -NoProfile -ExecutionPolicy Bypass -File $p -Uninstall"
```

```bash
# macOS / Linux
rm -rf "$HOME/.config/opencode/plugins/bridge"
# 或
curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash -s -- --uninstall
```

卸载后执行 `opencode service restart`（或重开 TUI）让插件停用。bridge 目录（含 `config.json` / `state.json`）会保留，如需彻底删除：见上方「原生方式」末尾的命令。

> ⚠️ **同一种安装方式只用一种**：不要让 `~/.config/opencode/plugins/bridge/`（脚本安装产物）与 `plugins[]` 里的 `github:dubuqiangu/opencode-bridge`（原生）同时存在。虽然插件有单例锁不会起两个 bridge 进程，但会重复加载、升级时也要两边同步。从脚本安装切到原生：删掉 `plugins/bridge/` 后执行一次 `opencode plugin add github:dubuqiangu/opencode-bridge`。

## License

[MIT](LICENSE)
