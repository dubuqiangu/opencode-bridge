# opencode-bridge

[![CI](https://github.com/dubuqiangu/opencode-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/dubuqiangu/opencode-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把 **13 个消息平台**的消息桥接到本机 [opencode](https://opencode.ai) 服务：你在 IM 里发一句话，本机的 agent 就在你的目录里干活，过程与结果再流式回到同一个会话里。**纯 Python 标准库实现，零第三方依赖。**

支持的平台：Telegram / Slack / Discord / Matrix / Mattermost / Nextcloud Talk / ntfy / email / IRC / Twitch / a2a / QQ Bot / Home Assistant —— **十三个全部支持双向对话**。

> **a2a 是唯一方向相反的平台**：其余十二个都是我们主动连出去（长轮询 / WebSocket / IMAP），
> **a2a 是我们被调方** —— 起一个本机 HTTP 服务让外部 agent 调我们。因此它**默认无鉴权**，
> 接入前请先读 [`docs/a2a.md`](docs/a2a.md) 的风险一节。

## 📖 文档

| 想知道 | 看哪 |
|---|---|
| 每个平台怎么配、怎么验证 | [`docs/install.md`](docs/install.md) |
| **a2a 专属**（方向相反的本机服务、无鉴权风险、端点与握手示例） | [`docs/a2a.md`](docs/a2a.md) |
| **这项目是怎么搭起来的** | [`docs/architecture.md`](docs/architecture.md) |
| **怎么加一个新平台** | [`docs/adding-a-platform.md`](docs/adding-a-platform.md) |
| 做到哪一步了、为什么这样选 | [`tasks.md`](tasks.md) |
| 与 Hermes / dsh-im-gateway 的对比 | [`docs/platform-design-reference.md`](docs/platform-design-reference.md) |
| 如何更新已安装的版本 | [`docs/update.md`](docs/update.md) |

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
   ⚠️ 刚生成的 `config.json` 里 `allowed_chat_ids` 是空数组、且 `config_version` 是 `0`（< 2）⇒ 现在仍是「空 = 全部放行」，启动时你会看到预告。**这一步就是白名单的位置，别跳**；若想要另一条路（`/pair` + `--pair`），见「7. 安全须知」的「两步走」。
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

各平台当前能力一览（**十三个平台全部支持双向对话**）：

| 平台 | 接收消息 | 发送消息 | 编辑消息 | 备注 |
|---|---|---|---|---|
| Telegram | ✅ 长轮询 `getUpdates` | ✅ | ✅ 原生 | 最成熟，按钮交互已支持；**私聊 / 群消息皆收、群里无需 @ bot** —— ⚠️ 群里每条文本消息都会驱动 agent，加群前先看 [`docs/install.md`](docs/install.md) Telegram 节「收不到消息怎么查」第 4 条 |
| Slack | ✅ Socket Mode | ✅ | ✅ 原生 | 入站需另配 `app_token`（`xapp-`） |
| Discord | ✅ Gateway v10 | ✅ | ✅ 原生 | 需在后台开 **Message Content Intent** |
| Matrix | ✅ `/sync` 长轮询 | ✅ | ⚠️ 兼容近似 | 无标准编辑 API，见该节说明 |
| Mattermost | ✅ WebSocket | ✅ | ✅ 原生 | 消息上限运行时从服务端读取 |
| Nextcloud Talk | ✅ HTTP 长轮询 | ✅ | ✅ 原生（24 小时内） | 上限 32000 字符是源码硬编码常量 |
| ntfy | ✅ HTTP `poll=1` + `since` 游标 | ✅ | ❌ 无 | 上限 4096 **字节**；**无用户身份**，务必用私有话题 + token |
| email | ✅ IMAP `UID` 游标 | ✅ SMTP | ❌ 无 | 上限 998 是**单行**（RFC 5322）；**无用户身份**，任何能发信给你的人都能驱动 agent |
| IRC | ✅ TCP | ✅ | ❌ 无 | 仅响应提及；正文换行折成空格 |
| Twitch | ✅ IRC over TLS WebSocket | ✅ | ❌ 无 | 仅响应提及 |
| a2a | ✅ 本机 HTTP（**我们被调方**） | ✅ 回给等待方 | ❌ 无 | 默认 bind `127.0.0.1` + **默认无鉴权**；上限 1 MiB（规范未规定，自行声明） |
| QQ Bot | ✅ WebSocket 网关 | ✅ REST | ❌ 无 | 上限 2000（**官方未给数字**，保守自定）；群/私聊/频道三种作用域；⚠️ **入站不做 @ 过滤**（见该节） |
| Home Assistant | ✅ WebSocket 事件总线 | ✅ `call_service` | ❌ 无 | ⚠️ **默认一个事件都不收**，必须配 `entities`/`domains`/`accept_all`；上限 4096（官方未公布） |

> 「编辑消息」能力不一致会影响流式进度更新：IRC / Twitch 没有它，长任务的进度会**退化成连续发多条消息**。
>
> 填好 token 后，可在 bot 里发送 **`/setup`** 查看 / 重温 Telegram / Slack / Discord 三个平台的引导；
> `/setup telegram`、`/setup slack`、`/setup discord` 可直达对应平台的分步引导。
> **Matrix / Mattermost / IRC / Twitch / Nextcloud Talk / ntfy / email / a2a / QQ Bot / Home Assistant 暂未纳入 `/setup` 引导**（菜单是刻意维护的固定文案），请按下面各节配置；
> 配置是否齐全一律用 `--status` 核对 —— 它会列出所有已注册平台，并区分「配置齐备」与「入站就绪」。

### 配置文件位置

| | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.config\opencode-bridge\config.json` |
| macOS / Linux | `${XDG_CONFIG_HOME:-~/.config}/opencode-bridge/config.json` |

### Telegram（支持双向对话 · 长轮询，无需公网地址）

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

   （**默认不 @ 也会响应** —— 桥接对 Slack 入站**不做 mention 过滤**；想让它**只**响应
   @ 提及时，才另加 `app_mentions:read`。用私有频道加 `groups:history`；
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

### Discord（支持双向对话 · Gateway v10 WebSocket，无需公网地址）

1. 打开 <https://discord.com/developers/applications> → **New Application** → 左侧 **Bot**
2. **Reset Token** → 复制 token
3. 同一页把 **Privileged Gateway Intents** 下的 **Message Content Intent** 打开（**必需**）
4. 左侧 **OAuth2 → URL Generator** → 勾选 scope: `bot` → Permissions: **Send Messages**
5. 用生成的 URL 把 bot 邀请进你的服务器
6. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "discord": { "bot_token": "..." } }
   ```

7. 执行 `opencode service restart`
8. 在频道里发一句普通文字，收到回复即成功

> **两个常见坑**
> - **第 3 步的开关不开，网关会直接拒绝连接（close 4014）**，日志里会写明原因（`--status` 之外的 `bridge-plugin.log`）。该 intent 只接收 bot **已在其中**的频道的消息。
> - **bot 必须已被邀请进频道**，否则发消息报 `not_in_channel`。

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

### Mattermost（支持双向对话 · WebSocket，无需公网地址）

1. 拿一枚 token 与你自己的 user id：
   - **个人访问令牌**（推荐，可随时吊销且不影响登录）：登录 Web UI → 左下头像 → **Profile** → **Security** → **Personal Access Tokens** → **Create New Token**
   - 自己的 user id：桥接启动时会自动调 `GET /api/v4/users/me` 取，**无需手填**（也可在配置里用 `user_id` 显式指定）
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "mattermost": { "site_url": "https://mm.example.com", "token": "你的令牌" } }
   ```

3. 执行 `opencode service restart`
4. 私聊这个账号，或在**它已加入**的频道发一句 `hi`，收到回复即成功

> - **必须配置 `user_id` 路径能走通**：防回环的唯一依据就是"这条消息是不是我自己发的"，靠 `users/me` 取到的 id 比对。取不到时桥接会**整个停摆入站**并在日志里提示 —— 而不是冒险进入无限回环。
> - `allowed_chat_ids` 填 **channel id**（26 位字符串），不是 user id。
> - **消息长度上限不在配置里**：由服务端运行时决定，桥接启动时从 `config/client` 读取 `MaxPostSize` 来分片。网上流传的 4000 / 16383 都不是契约，别照抄。
> - 反向代理部署时若握手 404/400，先查 `ServiceSettings.WebsocketURL` / `WebsocketPort` / `WebsocketSecurePort` 与代理是否透传了 `Upgrade` 头。

### IRC（支持双向对话 · TCP，无公网地址要求）

1. 选一个 IRC 网络并确认端口与是否 TLS（明文常用 `6667`，TLS 常用 `6697`）
2. 编辑配置文件：

   ```json
   "adapters": { "irc": {
     "host": "irc.libera.chat", "nick": "你的昵称",
     "channels": ["#你的频道"], "use_tls": true
   } }
   ```

3. 执行 `opencode service restart`
4. 在频道里 **`@你的昵称` 或 `昵称:`** 发一句话，或直接私聊该昵称

> - **只响应提及**（频道消息）或私聊 —— 频道很吵，不做这个会被刷屏。
> - **`channels` 是入站前提**：留空时桥接**只打一条 warning、仍然连接**（出站正常），但**入站永不触发**（状态视图会显示"入站未就绪"）。
> - **IRC 没有编辑消息**，`edit()` 恒 `False` —— 长任务的进度更新会退化成连续发多条消息。
> - **正文里的换行会被折成空格**（单行协议无法承载，原样发会被注入命令）。
> - **整行上限 512 字节**（含 `PRIVMSG` 前缀与 CRLF），桥接逐字节算预算并按字符边界切分，不会切出半个多字节字符。`max_message_length=400` 是扣掉前缀后的保守字符值。
> - 服务器与昵称冲突（收到 `433`）会自动换名重试；`nick` 必须在该网络已注册。
> - SASL：`bot_password` 填密码即可（走 `PASS`/`SASL PLAIN` 协商）。
> - ⚠️ **翻转成「空 = 全拒」后，IRC 的私聊对所有人不可用。** 私聊时闸门比对的 principal 是 **bot 自己的 nick**（IRC 根本没有发件人认证，任何人都能声称任何 nick），所以闸门无法区分。**频道里照常工作**（频道名 `#channel` 是真会话标识）。⛔ **IRC 不提供 `/pair`**（平台必须对发件人做过认证），白名单**只能手填**。这不是「部分能用」，是**没人能用**。
> - ⛔ **`allowed_chat_ids` 里绝不能出现 `nick` 自己的值**（大小写任意写法都不行）：那一行不是「只授权你自己」，**这一条会把所有人的私聊一起放行** —— 私聊的 principal 就是这个 nick，所有人的私聊共用它。桥接在构造适配器时就**拒绝启动**并给出这句话（不是 warning），Twitch 也一样，且连「运行期由 Helix 查到的 nick」也照样拒。删掉那一行即可，频道授权不受影响。

### Twitch（支持双向对话 · IRC over TLS WebSocket，无需公网地址）

1. 打开 <https://dev.twitch.tv/console/apps> → **Register an Application** → 填 Name
2. 复制页面上的 **OAuth Token**（这就是聊天用的 token）与 **Client ID**
3. 编辑配置文件：

   ```json
   "adapters": { "twitch": {
     "token": "OAuth Token", "channel": "频道名", "nick": "你的 Twitch 用户名"
   } }
   ```

   ⚠️ **`nick` 与 `client_id` 至少要有一个** —— 两个都不配时**会话起不来**：每次注册都抛
   「既没有配置 `nick`，也没有 `client_id` 可查 Helix」，基类关连接、退避重试 ⇒ **无限重连**。
   不想手填 `nick` 就改填 `client_id`，让它查 Helix 自动取。

4. 执行 `opencode service restart`
5. 在该频道 **`@你的昵称`** 发一句话，收到回复即成功

> - **token 直接粘 `OAuth Token` 的原值**，桥接会自动加 `oauth:` 前缀（不要自己加，否则会变成 `oauth:oauth:...`）。
> - `channel` 填**小写频道名、不带 `#`**。
> - **只响应提及**（Twitch 频道很吵）。命令前缀是 `!`。
> - **Twitch 没有编辑消息**，`edit()` 恒 `False` —— 长任务进度会退化成连续发多条消息。
> - 消息长度上限（默认 400 字符）与限流阈值是**社区经验值**，非官方文档公开常量。
> - **回声过滤靠 `nick`**：`nick` 配了就能直接用（配置里**必须有**它）；没配 `nick` 但配了 `client_id` 时由 Helix 查出来，回声过滤更准。**两个都没有 ⇒ 会话起不来并无限重连**（见上面第 3 步）。⚠️ 回声过滤只管误回环，`allowed_chat_ids` 白名单与它无关。
> - ⚠️ **翻转成「空 = 全拒」后，Twitch 的私聊对所有人不可用**（原因同 IRC：私聊 principal 是 bot 自己的 nick）。**频道里照常工作。** ⛔ **Twitch 不提供 `/pair`**，白名单**只能手填**。
> - ⛔ **`allowed_chat_ids` 里绝不能出现 `nick` 自己的值**，理由与 IRC 相同：**这一条会把所有人的私聊一起放行**。⚠️ Twitch 的 nick 可能**不来自配置**（填了 `client_id` 时由 Helix 查出来，运行期才知道）⇒ 桥接在**每一次** nick 变成已知值时都重新查一次，查到就**拒绝**（`TwitchAdapter.nick` 的 setter 是唯一入口，配置里没写 nick 也一样）。

### Nextcloud Talk（支持双向对话 · HTTP 长轮询，无需公网地址）

1. 生成 **app password**：登录 Nextcloud → 右上角设置 → **安全** → **设备专属密码** → **创建新密码**。它与账号密码在 Basic Auth 里完全等价，但可单独吊销、不影响登录、不过期。**建议用独立的机器人账号**。
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "nextcloud": {
     "base_url": "https://cloud.example.com",
     "username": "my-bot", "password": "app-password-xxxx"
   } }
   ```

   `base_url` **要含子路径前缀**（如 `https://host/nextcloud`），原样填即可。
3. 执行 `opencode service restart`
4. 私聊机器人，或在它已加入的会话里发一句 `hi`，收到回复即成功

> **三个最容易写错、且都是"静默失败"的地方**（都已在代码里固定并有测试锁住）
> - **`OCS-APIRequest` 的值必须是字面量小写 `true`** —— 服务端是**严格字符串比较**（`=== 'true'`），写成 `True` / `1` / `yes` 会被当成 CSRF 攻击并返回 **403**。
> - **只走 `ocs/v2.php`** —— `ocs/v1.php` 入口的 HTTP 状态码**恒为 200**（失败也看不出来），v2 才返回真实状态码。
> - **`304` 不是错误** —— 长轮询"没有新消息"时服务端返回 304，而 `urllib` 会把它**抛成 `HTTPError`**。这是本适配器最容易写错的一处。
>
> 其它要点：
> - `allowed_chat_ids` 填**会话 token**（`ocs.data[].token`），不是 user id。⛔ **nextcloud 不支持 `/pair` 配对**（principal 就是那个 OCS token，用户既不知道也不该知道）⇒ 白名单**只能手填**。
> - 消息长度上限 **32000 字符，是源码里的硬编码常量、不可配置**（网上没有对应的 `occ config` 设置）。超限服务端返回 413。
> - **支持编辑消息**（`edit()` 会真正生效），但**超过 24 小时不能改**；且需会话权限含 128、且会话非只读 / 非 lobby。
> - `max_concurrent_polls`（默认 5）别调大：每个长轮询请求会占住一个服务端 worker 30 秒。`poll_timeout` **上限就是 30**（源码 clamp，填更大也会被服务端压回）。
> - `@提及` 不做渲染：`message` 字段是含 `{mention-call1}` 占位符的模板串，v1 原样透传。

### ntfy（支持双向对话 · HTTP 拉取，无需公网地址）

1. 建一个话题（topic）。**强烈建议用私有话题 + read token**，理由见下面的信任模型。
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "ntfy": {
     "server": "https://ntfy.sh", "topic": "my-private-topic",
     "token": "tk_..."
   } }
   ```

3. 执行 `opencode service restart`
4. 往这个话题发一条通知（`curl -d "hi" ntfy.sh/my-private-topic`），收到回复即成功

> ⚠️ **信任模型（重要）**：ntfy **没有用户身份概念** —— 任何能往话题发消息的人都会被当作用户。
> 用公共话题（`ntfy.sh/<topic>`）等于**把你的 agent 暴露给全网**，任何人都能驱动它执行
> 权限范围内的操作。**务必**用私有话题 + read token，或自建服务器开 access control。
>
> 其它要点：
> - `allowed_chat_ids` 填**话题名**。
> - 消息上限 **4096 是字节不是字符**（服务端受 FCM/APNS 约 4KB 约束），中文一字 3 字节，
>   所以实际能放的中文字数约为 1365。桥接按字节切分，不会切出半个字符。
> - **不支持编辑消息**（ntfy 没有 edit 端点），长任务的进度更新会退化成连续发多条通知。
> - **不会重放历史**：启动时游标设为当前时间，之前缓存里的通知不会被当成新消息触发 agent。
> - `echo_tag` 只用于**防回环**，不是身份认证 —— 别指望它挡住别人。

### email（支持双向对话 · IMAP + SMTP，无需公网地址）

1. **准备一个专用邮箱**（不要用主邮箱），并开 **app 专用密码**。
   Gmail / Outlook 都需要先在账号设置里启用两步验证才能生成 app password。
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "email": {
     "address": "mybot@example.com", "password": "abcd efgh ijkl mnop",
     "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"
   } }
   ```

3. 执行 `opencode service restart`
4. 给 `mybot@example.com` 发一封邮件，收到回复即成功

> ⚠️ **信任模型（重要）**：邮件**没有用户身份概念** —— 任何能给这个地址发信的人
> 都会被当作用户。所以**必须**用 `allowed_chat_ids` 限定具体发件人地址，否则等于
> 把 agent 暴露给任何知道你邮箱地址的人（爬虫、钓鱼、垃圾邮件都会驱动它执行
> 权限范围内的操作）。**强烈建议用专用邮箱。**
>
> ⚠️ **凭据即完整信箱权限**：配置里的密码能读**整个**邮箱（不只是桥接那个文件夹）。
> 所以要用 **app 专用密码**而不是主密码 —— 万一泄漏，损失被限制在那一个账号，
> 且可单独吊销。
>
> 其它要点：
> - **不重放历史**：首次连接只记录当前邮件水位线，一封旧邮件都不取。
> - **不碰你的已读状态**：取信用 `BODY.PEEK[]` 而不是 `BODY[]`，后者会隐式标记已读，
>   你手机端的未读数会突然少一封。
> - **不会自己回自己**：出站 Subject 带 `[opencode]` 前缀并记下 `Message-ID`，入站见到
>   就丢。你**回复**它时不会被误丢（那是真提问）。前缀不可配成空串。
> - 单行上限 **998 字符**（RFC 5322 硬上限）；正文按行折行，超限自动分片。
> - **不支持编辑邮件**（SMTP 没有这个概念），长任务的进度更新会退化成连续发多封。
> - 用的是 `UID` 游标而非 `UNSEEN` 标记 —— 你在手机上点开一封，桥接仍然能看见它。

### a2a（支持双向对话 · **我们是被调方**，无需公网地址）

这是唯一**方向相反**的平台：其余十二个是我们主动连出去，a2a 是**起一个本机 HTTP 服务**
让外部 A2A agent 调我们。协议细节见 [`docs/a2a.md`](docs/a2a.md)。

1. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "a2a": { "bind_host": "127.0.0.1", "bind_port": 9900 } }
   ```

   ⚠️ **`bind_port` 必填、无默认值** —— 缺了（或不是合法端口）时 **a2a 这个适配器不启动**
   （日志里一条 `ERROR a2a: 未配置 bind_port …，适配器未启动`；`--status` 显示 `missing: ['bind_port']`），
   **不会**降级到某个默认端口。
   **别照抄 `bind_port: 0`**：Agent Card 里公布的 URL 含端口，端口 0 每次重启都变，**对端永远连不上**，
   它只适合测试。
2. 执行 `opencode service restart`
3. 用上面配的那个端口访问：

   ```bash
   curl http://127.0.0.1:9900/.well-known/agent-card.json   # 应返回 Agent Card
   curl http://127.0.0.1:9900/health                          # 应返回 ok
   ```

> ⚠️ **默认无鉴权，请先读风险**：a2a 出站侧不需要任何凭据，所以**默认没有任何鉴权**。
> 此时**只有本机进程能访问**（`bind_host` 默认 `127.0.0.1`），但**本机也是攻击面** ——
> 浏览器里的恶意网页可以 POST 到 `http://127.0.0.1:<端口>/rpc`。
> 建议：① 保持 `127.0.0.1`；② 需要被其它机器访问时**必须**配 `auth_token`。
> ⚠️ 把 `bind_host` 改成非回环地址**且**没配**任何凭据**（`auth_token` **或** `peer_tokens` 都算）时，
> 桥接会**回落回环并告警** —— 绝不因为"方便调试"就开一个无鉴权的局域网端口。
>
> 其它要点：
> - **没有凭据可填**（`config_optional` 说的是「我是这一类」），但**端口是必填的**：`bind_port` 缺失时 `start()` 会明确报错并拒绝绑定。⚠️ 「这份配置此刻能不能跑」由适配器**自己**回答（`config_runnable`），**不是**看 `config_optional` —— 所以只配 a2a（含上面那个必填的 `bind_port`）时桥接**能正常启动**，而**留空 `bind_port` 则不会**。
> - 端点：`/rpc`（另接受 `/` 作别名）、`/health`、`/.well-known/agent-card.json`。
> - 只实现 `SendMessage`/`GetTask`/`ListTasks`/`CancelTask`；**流式与推送如实声明为
>   不支持**（Agent Card 里写 `false`，调用时返回规范错误码），**不做半成品接口**。
> - **不支持编辑**（回复发给正在等待的那个 HTTP 请求），长任务进度会退化成连续多条消息。
> - 未实现与真实 A2A 客户端的互操作验证（协议事实已逐条对照官方规范 v1.0.0）。

### QQ Bot（支持双向对话 · WebSocket 网关，无需公网地址）

1. 在 [QQ 开放平台](https://bot.q.qq.com/) 建机器人，拿 **AppID** 与 **AppSecret**
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "qqbot": { "app_id": "102xxxxx", "app_secret": "xxxx" } }
   ```

3. 执行 `opencode service restart`
4. 在开放平台后台把机器人加进群 / 频道，或直接私聊它

> 其它要点：
> - ⚠️ **入站不做 @ 过滤**（与 IRC / Twitch 相反）：只按 `message_type`（只收纯文本）与
>   **平台签发**的防回环字段（`author.bot` / `author.id` 对比 READY 的 `user.id`）过滤，
>   **不检查有没有 @ 机器人**。默认订阅的 intent `1<<25` 送来群里 **@ 消息**
>   （`GROUP_AT_MESSAGE_CREATE`）；若在开放平台另开「接收所有消息」，**非 @ 的群消息**
>   （`GROUP_MESSAGE_CREATE`）同样会驱动 agent ⇒ **群里很吵**，用 `allowed_chat_ids` 限定群，
>   或干脆别开「接收所有消息」。
> - 支持**群聊、私聊（C2C）、频道**三种作用域，`allowed_chat_ids` 填对应作用域前缀
>   （`qqbot:group:...` / `qqbot:c2c:...` / `qqbot:channel:...`）。
> - **主动消息在群里会失败**（`40034105`，除非开被动回复窗口），所以桥接会带上入站的
>   `msg_id` 与 `msg_seq`；被动回复有时效与次数上限（群 5 分钟 5 次、私聊 1 小时 4 次），
>   超了会可观测地报错。
> - **不支持编辑消息**：官方只有撤回（DELETE），频道那个 PATCH 改的是 keyboard 富文本
>   而不是正文，所以 `edit()` 诚实返回 `False`，长任务进度会退化成连续多条消息。
> - **未实现与真实 QQ 客户端的互操作验证**（协议事实已逐条对照官方文档）。

### Home Assistant（支持双向对话 · WebSocket 事件总线，无需公网地址）

1. 在 HA 的**个人档案页**生成**长期访问令牌**
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "homeassistant": {
     "url": "http://homeassistant.local:8123", "token": "eyJ...",
     "domains": ["light"]
   } }
   ```

3. 执行 `opencode service restart`
4. 在 HA 里开关一下灯，agent 应该收到事件

> ⚠️ **不配过滤条件就一个事件都收不到（这是刻意设计）**
>
> Home Assistant 推的是**设备状态变更**（"灯亮了"），而不是"某人给你发了条消息"。
> 把这类事件当对话喂给 agent，只在事件能追溯到**某个真人用户的操作**时才成立——
> 定时器、脚本、集成自己触发的事件（`context.user_id` 为空）根本不是"有人在跟你说话"，
> 拿它起对话只会污染上下文。所以默认**全丢**：
>
> - 必须给 `entities` / `domains`，或显式 `accept_all: true`，否则**收不到任何事件**
> - `require_user_context` 默认 `true`：只接收能归因到真人的事件
>
> ⚠️ **这一点很容易被"状态显示已就绪"误导**：只配 `url` + `token` 时 `--status` 会显示
> 「已配置 / 入站就绪」，但实际收不到东西。所以 `capabilities()` 里给了机器可读的判据
> —— `inbound_accepts_anything: false` 就是"配好了但收不到"的明确信号
> （`--setup --json` 的 `platforms[].capabilities` 能直接读到）。启动日志里也会打一次WARNING。
>
> 其它要点：
> - **两层保活方向相反**：传输层是 **aiohttp 每 55s 发 RFC 6455 ping**（`ws.py` 自动回
>   pong，零代码）；应用层 JSON `ping` 必须**客户端主动发**（HA 只回不主动发）。两者都实现了。
> - `event_types: ["*"]` 通配订阅**需要管理员**权限。
> - 出站走 `call_service`（HA 没有"发消息"原语），默认发 `persistent_notification.create`，
>   它**不改状态**所以天然不成环。若你改配成改状态的服务（如 `light.turn_on`），
>   会有自触发回声，靠 `ignore_entities` + 10 秒动作窗口压制。
> - **不支持编辑消息**（HA 只有 create/dismiss，没有"改一条"），进度更新会退化成连续多条。
> - 未与真实 HA 实例做过互操作验证（协议事实已逐条对照官方文档与源码）。

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
#       ⚠️ config.example.json 里 allowed_chat_ids 是 [] 且 config_version 是 0
#       ⇒ 现在仍是「空 = 全部放行」。别跳这一步，见「7. 安全须知」。

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
python -m opencode_bridge [--config PATH] [--verbose] [--check] --pair <码> --conversation platform:local_id
```

| 参数 | 说明 |
|---|---|
| `--config PATH` | 指定配置文件（默认：`$OPENCODE_BRIDGE_CONFIG` → `./config.json`） |
| `--verbose` | 输出 DEBUG 日志 |
| `--check` | 只做连通性自检后退出（不创建 session、不启动适配器） |
| `--conversation platform:local_id` | 配合 `--pair` 用：**首次配对必填**，值就是 `/pair` 回信里那个 `platform:local_id`（如 `telegram:12345`）。⚠️ **码绑定会话，只凭一串码无法确定是哪个** —— 那需要遍历所有可能的会话 id，是无界搜索、等于给 40 bit 造 oracle。**省略它不会「按码自动反查」**：那个兜底只在**已经授权过**的会话里找（重新配对已有会话的便利），首次配对时会直接报错退出并告诉你补上这个参数 |
| `--pair <码>` | 用 `/pair` 给出的授权码把**那个会话**写进 `allowed_chat_ids`，并顺手写上 `config_version: 2`。⚠️ **只允许本机执行**（没有网络 oracle）；⛔ **绝不创建不存在的 `config.json`**；**写完必须重启桥才生效**（本项目没有热重载，改 `bot_token` 同样要重启）。⚠️ 逐会话不是逐人；⚠️ 不提供 TTL / 一次性使用 / 锁定 |

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
| `/model` | 显示当前会话用的模型，并提示怎么切换 |
| `/model <provider>/<id>` | 切换模型（例如 `/model opencode/space-bunny-free`）。**不在 opencode 的模型目录里就拒绝切换**，并回最接近的几个候选；**不会**新建会话来校验 |
| `/model <关键词>` | 在模型目录里搜索（匹配 provider / id / 名称），列出 `provider/id  名称`，**不会**切换 |
| `/cd <目录>` | 切换该会话的工作目录（写入 `state.json`）并重建 session，回 `已切换到 <目录>` |
| `/approve <请求ID>` | 回复权限请求：允许一次 |
| `/approve <请求ID> always` | 回复权限请求：总是允许 |
| `/allow <请求ID>` | 同 `/approve` |
| `/deny <请求ID>` | 拒绝权限请求 |
| `/setup` | 查看分平台接入引导（`/setup telegram` / `slack` / `discord` 可直达） |
| `/pair` | 取一条**绑定到当前会话**的授权码，供本机 `python -m opencode_bridge --pair <码> --conversation platform:local_id` 完成白名单授权（**未授权的 chat 也能用**，这是「默认拒绝」不会把人困死的原因）。⛔ 需要 `config.json` 里有 `pairing_secret`；⛔ `irc / twitch / nextcloud / homeassistant / a2a / qqbot` 不支持。⚠️ **逐会话、不是逐人**：在群里拿到的码授权的是**那个群**；⚠️ 回信里只有命令形状、没有本机路径 |
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
| `config_version` | `0` | 授权闸门的语义版本。**`< 2`（包括 example 模板与安装脚本给的 `0`，以及没有这个键的老文件）** ⇒ 沿用旧语义「白名单为空 = 全部放行」，并在启动时打一条醒目预告；**`>= 2`** ⇒ 新语义「白名单为空 = 谁都不放行」。`--pair` 成功时会顺手写上 `2` |
| `pairing_secret` | `""` | `/pair` 授权码的派生密钥，**自己生成**（如 `python -c "import secrets; print(secrets.token_hex(32))"`）填在这里。⛔ **留空 = 不提供配对**（绝不等于「用空串派生」）。⚠️ 改这个值 = 让所有**未兑换**的码失效；**已完成的配对不受影响**（授权已物化进 `allowed_chat_ids`），所以换 secret **踢不掉已授权的人** |
| `bridge.edit_interval_seconds` | `1.5` | 流式增量编辑同一条 IM 消息的最小间隔（秒），用于节流 |
| `bridge.max_message_chars` | `4000` | 单条消息编辑的长度上限；定稿超过该长度时改为**直接发送**（交给适配器分块） |
| `adapters.telegram.bot_token` | `""` | Telegram bot token（`@BotFather`） |
| `adapters.telegram.allowed_chat_ids` | `[]` | **白名单**：非空时只响应列表内的 chat id。空数组的含义**取决于 `config_version`**（见上）—— `< 2` 时是「全部放行」，`>= 2` 时是「谁都不放行」。⚠️ **每个平台的默认 `[]` 在 `config_version < 2` 时都是全放行**，任何能给 bot 发消息的人都能以你的权限驱动 agent —— 见「7. 安全须知」 |
| `adapters.telegram.poll_timeout` | `25` | `getUpdates` 的**长轮询挂起秒数**（Bot API 上限 50）。⚠️ **写错不会打死桥接**：非整数（`"25s"`、`"25.5"`）或**非正数**（`0`、`-5`）都会**回落为默认值 `25` 并打一条 WARNING**（`telegram: 配置项 poll_timeout=… 已回落为 25`），适配器照常启动。留空 = 静默用默认值 |
| `adapters.slack.bot_token` | `""` | Slack bot token（`xoxb-`）：**出站必需**；入站还需下面的 `app_token` |
| `adapters.slack.app_token` | `""` | Slack **app-level token**（`xapp-`）：Socket Mode 入站专用，缺它时降级为只发出站 |
| `adapters.matrix.homeserver` | `""` | Matrix homeserver 根地址（如 `https://matrix.example.org`，尾部斜杠会自动去掉） |
| `adapters.matrix.access_token` | `""` | Matrix access token（入站与出站都必需） |
| `adapters.matrix.user_id` | `""` | 自己的 Matrix user id（如 `@me:example.org`）：用于**过滤自己的回声**，留空会把自己发的消息当入站消息收到（无限回环） |
| `adapters.matrix.sync_timeout_ms` | `30000` | `/sync` 长轮询的挂起毫秒数（客户端 `/sync` 的 `timeout` 参数；`0` = 不长轮询、立即返回；⚠️ **但那会让轮询从「每 30 秒一轮」变成「每 `backoff_interval`（默认 2 秒）一轮」，对 homeserver 的请求率约 ×15 —— 一般不要设它**）。⚠️ **写错不会打死桥接**：非整数（`"30s"`、`"30000.5"` 这类**字符串**）会**回落为默认值 `30000` 并打一条 WARNING**（`matrix: 配置项 sync_timeout_ms=… 不是整数 …，已回落为 30000`），适配器照常启动。留空 = 静默用默认值 |
| `adapters.matrix.since` | `""` | `/sync` 游标（上次拿到的 `next_batch`）。留空即从当前时刻起收，**不重放历史** |
| `adapters.discord.bot_token` | `""` | Discord bot token（出站与 Gateway 入站共用同一枚） |
| `adapters.discord.gateway_url` | `""` | Gateway 地址；留空则走 Discord 官方地址 |
| `adapters.discord.intents` | `37376` | Gateway intents 位掩码（默认 = 消息 + 私信 + **Message Content**，后者必须先在开发者后台勾上）。⚠️ **非法值会告警并回落默认**（`discord: intents 配置非法 …，改用默认 37376`），**不会**让适配器起不来 |
| `adapters.mattermost.site_url` | `""` | Mattermost 站点地址（如 `https://mm.example.com`）；WS 与 REST 的 scheme 由它推导。**别名 `server_url`**（同义，二选一即可） |
| `adapters.mattermost.token` | `""` | Mattermost bot / 用户 token（`Authorization: Bearer` 用它） |
| `adapters.mattermost.user_id` | `""` | 自己的 user id；留空则启动时自动调 `GET /api/v4/users/me` 取。⚠️ **取不到时桥接整个停摆入站**并在日志里提示（防回环的唯一依据就是"这条是不是我自己发的"，宁可停摆也不冒险回环） |
| `adapters.mattermost.team_id` | `""` | ⚠️ **仅用于日志**，v1 **不参与路由**（团队 / 多租户没实现） |
| `adapters.mattermost.verify_tls` | `true` | 校验证书链与主机名。⚠️ **只影响 REST**：WebSocket 侧的 `ws.py` 固定用 `ssl.create_default_context()`、**不提供关校验的开关** ⇒ 配成 `false` 也关不掉 WS 的证书校验 |
| `adapters.irc.host` | `""` | IRC 服务器地址（如 `irc.libera.chat`） |
| `adapters.irc.nick` | `""` | 使用的昵称 |
| `adapters.irc.channels` | `[]` | 自动 JOIN 的频道列表（如 `["#chan"]`）；**入站前提**，留空时**只告警、仍然连接**（出站正常），但**入站永不触发** |
| `adapters.irc.port` | `6667` | 端口；`use_tls` 为真时默认 `6697` |
| `adapters.irc.use_tls` | `false` | 是否用 TLS（IRC over TLS） |
| `adapters.irc.bot_password` | `""` | SASL PLAIN 的密码（可选，与 nick 组成 SASL 凭据） |
| `adapters.irc.server_password` | `""` | 服务器口令（`PASS` 命令；与 `bot_password`/SASL 是两回事） |
| `adapters.irc.realname` | `""` | `USER` 命令里的 realname；留空则用 `nick`，再留空用 `opencode-bridge` |
| `adapters.twitch.token` | `""` | Twitch bot token（从开发者控制台取，**不要**自己加 `oauth:` 前缀） |
| `adapters.twitch.channel` | `""` | 要接入的频道名（小写，不带 `#`） |
| `adapters.twitch.nick` | `""` | **IRC 协议用的 bot 用户名**。⚠️ 与 `client_id` **至少要有一个**：两个都没有时每次会话都抛「既没有配置 nick，也没有 client_id 可查 Helix」并退避重连 ⇒ **无限重连** |
| `adapters.twitch.client_id` | `""` | 开发者控制台的 Client ID（可选；填了就能用 Helix API 取自己的 user id 做回声过滤，也能顶替 `nick`） |
| `adapters.twitch.display_name` | `""` | `USER` 命令里的 realname；留空则用 `nick`。**只有它与 `nick` 都会被当作提及**做过滤 |
| `adapters.twitch.user_id` | `""` | 自己的 user id（可选；留空则用 Helix 查到的） |
| `adapters.twitch.membership` | `false` | `true` = 额外订阅 `twitch.tv/membership`，可维护成员名单（用于人名提及） |
| `adapters.twitch.endpoint` | `wss://irc-ws.chat.twitch.tv:443/` | IRC over TLS WebSocket 端点 |
| `adapters.nextcloud.base_url` | `""` | Nextcloud 站点地址（**含子路径前缀**，如 `https://host/nextcloud`） |
| `adapters.nextcloud.username` | `""` | 登录用户名（建议用独立的机器人账号） |
| `adapters.nextcloud.password` | `""` | **app password**（「设置 → 安全 → 设备专属密码」生成，可单独吊销且不影响登录） |
| `adapters.nextcloud.user_id` | `""` | 自己的 Nextcloud user id（**大小写敏感**）；留空则启动时自动调 `cloud/user` 取。⚠️ **自动取失败 ⇒ 暂停入站处理**并在日志里提示（防回环的唯一依据就是"这条是不是我自己发的"，宁可停摆也不冒险成环）⇒ 取不到时在配置里**显式写出** `user_id` |
| `adapters.nextcloud.max_concurrent_polls` | `5` | 同时长轮询的会话数上限；每个长轮询会占住一个服务端 worker 30 秒，不宜过大 |
| `adapters.nextcloud.poll_timeout` | `30` | 服务端长轮询秒数，**上限就是 30**（源码 clamp，再大也被服务端压回） |
| `adapters.nextcloud.full_refresh_seconds` | `300` | 每隔多久重扫一次会话列表（源码 clamp 到 `30`~`86400`） |
| `adapters.ntfy.server` | `"https://ntfy.sh"` | ntfy 服务器地址（可用自建） |
| `adapters.ntfy.topic` | `""` | 订阅的话题名（**必填**）；`allowed_chat_ids` 填的就是它 |
| `adapters.ntfy.token` | `""` | read token（`tk_…`）；私有话题必填 |
| `adapters.ntfy.user` / `password` | `""` | 也可用 Basic 认证（与 `token` 二选一，`token` 优先） |
| `adapters.ntfy.echo_tag` | `"opencode-bridge"` | 出站打这个 tag，入站见到即丢弃（**防回环**，不是身份认证） |
| `adapters.ntfy.poll_interval` | `5.0` | 轮询间隔（秒） |
| `adapters.email.address` | `""` | 桥接自己的邮箱地址（**必填**）；`allowed_chat_ids` 填对方地址 |
| `adapters.email.password` | `""` | **app 专用密码**（不是主密码！）（**必填**） |
| `adapters.email.imap_host` | `""` | IMAP 服务器（**必填，无默认值** —— 按域名猜对自建/企业邮箱是错的） |
| `adapters.email.smtp_host` | `""` | SMTP 服务器（**必填**，同上） |
| `adapters.email.imap_security` / `smtp_security` | `"ssl"` | `ssl`（隐式 TLS，通常 465）或 `starttls`（587）。**非法值回落成加密**，不会回落明文 |
| `adapters.email.imap_port` / `smtp_port` | `""` | 留空则按上面选的方式取默认端口 |
| `adapters.email.mailbox` | `"INBOX"` | 监听哪个文件夹 |
| `adapters.email.verify_tls` | `true` | 校验证书链与主机名。关掉必须显式配（自建/实验环境），会打警告 |
| `adapters.email.echo_prefix` | `"[opencode]"` | 出站 Subject 加此前缀，入站见到即丢。**不可配成空串** —— 空前缀等于关掉防回环 |
| `adapters.email.poll_interval` | `60.0` | 轮询间隔（秒）；每轮新建一次 IMAP 连接 |
| `adapters.email.socket_timeout` | `30.0` | IMAP / SMTP socket 超时（秒）；非正数或非法值回落默认 |
| `adapters.email.subject` | `"reply"` | 出站邮件的 Subject（会再加 `echo_prefix`） |
| `adapters.email.dedupe_capacity` | `2048` | 已处理 Message-ID 的记忆上限（FIFO 淘汰，防无界增长）。⚠️ **写错不会打死桥接**：非整数（`"2048条"` 这类字符串）会**回落为默认值 `2048` 并打一条 WARNING**（`email: 配置项 dedupe_capacity=… 不是整数 …，已回落为 2048`）；**小于 `1`**（`0` / `-5`）同样**回落默认 + 告警**、⛔ 不静默改成 `1`。留空 = 静默用默认值 |
| `adapters.a2a.bind_host` | `"127.0.0.1"` | 监听地址。**非回环地址且没配任何凭据（`auth_token` 或 `peer_tokens` 都算）时会回落回环 + 告警** —— 绝不因为方便就开一个无鉴权的局域网端口 |
| `adapters.a2a.bind_port` | **无默认值（必填）** | 监听端口。⚠️ **没有默认值**：缺了它（或不是合法端口）**a2a 适配器不启动**（日志一条 ERROR，`--status` 显示 `missing: ['bind_port']`），**不会**降级到某个默认端口。`0` = 由操作系统分配，但 **Agent Card 里公布的 URL 含端口，每次重启都变、对端永远找不到我们 ⇒ 只适合测试** |
| `adapters.a2a.auth_token` | `""` | 共享 Bearer token。**留空 = 无鉴权**，此时务必确认 `bind_host` 是回环地址 |
| `adapters.a2a.peer_tokens` | `""` | **每个对端一个凭据**，`"alice:tok1,bob:tok2"`（或 `{"alice": "tok1"}`）。身份直接取名字，比 `auth_token` 更好定位与限流；配了它**同样算「已配凭据」**（影响 `bind_host` 的回落判断）—— 详见 [`docs/a2a.md`](docs/a2a.md) |
| `adapters.a2a.reply_timeout` | `300.0` | 外部 agent 等待回复的超时（秒）。非法值回落默认并告警 |
| `adapters.a2a.max_turns` | `5` | 单个 `contextId` 的入站往返轮数上限，防乒乓。⚠️ **硬顶 20**，配更大会被**下调并告警** |
| `adapters.a2a.max_tasks` | `512` | 内存里保留的 task 记录条数（超了 FIFO 丢最老的终态） |
| `adapters.a2a.agent_name` / `agent_description` / `agent_version` | 主机名派生 / 内置文案 / `0.1.0` | Agent Card 上的三个字段 —— 详见 [`docs/a2a.md`](docs/a2a.md) |
| `adapters.qqbot.app_id` | `""` | QQ 开放平台的 AppID（**必填**）。**别名 `appid` / `appId`**（不同文档里拼法不同） |
| `adapters.qqbot.app_secret` | `""` | AppSecret（**必填**）；用它换 `access_token`，日志里会打码。**别名 `appsecret` / `appSecret` / `client_secret` / `clientSecret`** |
| `adapters.qqbot.api_base` | `https://api.bot.qq.com` | API 基址。**沙箱域名未在官方文档中核实**，默认走正式环境 |
| `adapters.qqbot.sandbox` | `false` | `true` = 走沙箱域名（配合 `api_base` 覆盖）。⚠️ 该域名**未经官方文档核实**，开启时会打一条告警 |
| `adapters.qqbot.gateway_url` | `""` | 留空则启动时 `GET /gateway` 自动取 |
| `adapters.qqbot.intents` | `1107296256` | 事件订阅位掩码（默认 = 群聊/单聊 `1<<25` + 频道 @ `1<<30`）。⚠️ **非法值会告警并回落默认**；官方明确「传递了无权限的 `intents`，websocket 会报错并直接关闭连接」，所以**少订阅**比多订阅安全 |
| `adapters.qqbot.shard` | `[0, 1]` | 分片位置/总数，如 `[0, 4]`。⚠️ **非法值会告警并回落默认** `[0, 1]`（单实例无需分片） |
| `adapters.homeassistant.url` | `http://homeassistant.local:8123` | HA 地址。⚠️ `homeassistant.local` 是 **mDNS 惯例**、不是官方规定；留空会用它兜底并告警。**别名 `site_url` / `base_url` / `hass_url` / `server_url`**（与 mattermost 的 `site_url` **同形**，跨平台抄错时没有任何提示） |
| `adapters.homeassistant.token` | `""` | **长期访问令牌**（HA 档案页生成）（**必填**）。**别名 `access_token` / `hass_token` / `long_lived_access_token`** |
| `adapters.homeassistant.entities` | `[]` | 只接收这些实体的事件，如 `["light.kitchen"]`。**别名 `watch_entities`** |
| `adapters.homeassistant.domains` | `[]` | 只接收这些域的事件，如 `["light","switch"]`。**别名 `watch_domains`** |
| `adapters.homeassistant.accept_all` | `false` | `true` = **接收全部事件**（很吵慎用） |
| `adapters.homeassistant.event_types` | `["state_changed"]` | 订阅哪些事件类型。⚠️ `["*"]` 通配**需要管理员**权限 |
| `adapters.homeassistant.require_user_context` | `true` | 只接收能归因到**真人用户**的事件（`context.user_id` 非空）。定时器/脚本触发的事件会被丢 |
| `adapters.homeassistant.ignore_entities` | `[]` | 收到事件后忽略这些实体（用于躲开自己触发的回声） |
| `adapters.homeassistant.service_domain` | `"persistent_notification"` | 出站 `call_service` 的 domain。**别名 `domain`** |
| `adapters.homeassistant.service_name` | `"create"` | 出站 `call_service` 的 service 名。**别名 `service`** |
| `adapters.homeassistant.notification_title` | `"opencode-bridge"` | 出站通知的标题。**别名 `title`** |

环境变量：`OPENCODE_URL` / `OPENCODE_PASSWORD` / `OPENCODE_DIRECTORY` 会覆盖配置文件中的同名项；`OPENCODE_BRIDGE_CONFIG` 指定配置文件路径。

插件目录下的 `config.json`（**仅脚本安装方式**由安装脚本生成；原生 `plugin add` 安装不产生它，插件改用自举稳定目录）：

| 键 | 说明 |
|---|---|
| `bridgeDir` | bridge 目录（= 仓库 clone 目录）的绝对路径；插件以它为 `cwd` 拉起 `python -m opencode_bridge`，日志与锁文件也写在这里 |

## 7. 安全须知

1. **IM 是低信任入口**：`permissions_mode` 默认 `ask`（每次敏感操作都要确认），**不要**轻易改成 `allow`——那等于允许任何能给 bot 发消息的人以你的权限执行任意操作。
2. **务必配置 `allowed_chat_ids` 白名单**：未列入白名单的 chat 的消息会在适配器层被直接丢弃。⚠️ 反过来更要注意 —— **刚由安装脚本 / 自举生成的 `config.json` 里 `allowed_chat_ids` 是 `[]` 且 `config_version` 不是 2 ⇒ 它现在仍是「空 = 全部放行」**：`permissions_mode` 默认 `ask` 只在 agent 想做敏感操作时才问你，而**读文件、跑命令、改代码本身不需要你确认**。也就是说把 bot 放进公开群、或让它能被陌生人私聊，就等于把你的 shell 交给了对方。启动时你会看到一条预告：**下一版起此处改为「空 = 谁都不放行」**。

   ### 两步走的过渡：怎么从「全放行」变成「默认拒绝」

   闸门语义由顶层键 `config_version` 决定（见「6. 配置项说明」）：

   | `config.json` 的 `config_version` | `allowed_chat_ids` 为空时 |
   |---|---|
   | **`< 2`**（含没有这个键的老文件、以及模板给的 `0`） | **全部放行**（旧语义）+ 启动时醒目预告 |
   | **`>= 2`** | **谁都不放行**（新语义） |

   ⇒ **没有人会被困死**：`/pair` 在**未授权**的 chat 上就能用，被拒的人发一条 `/pair` 就能拿到授权码。要走这条路：

   ```bash
   # 1) 自己生成一段随机串，填进 config.json 的顶层键 pairing_secret
   #    （⛔ 留空 = 不提供配对，不是「用空串派生」）
   python -c "import secrets; print(secrets.token_hex(32))"

   # 2) 重启桥，然后在 bot 里发 /pair —— 它会回一条绑定到**那个会话**的授权码

   # 3) 在 bridge 目录里执行（写盘 + .bak 备份 + 顺手写上 config_version: 2）
   python -m opencode_bridge --pair <码> --conversation platform:local_id

   # 4) 再重启一次 —— 改配置没有热重载（改 bot_token 也一样要重启）
   ```

   ⚠️ **三条必须知道的边界**：

   - **irc / twitch：翻转后私聊对所有人都不可用。** 它们的私聊 principal 是 **bot 自己的 nick**，不是对方的身份（IRC 根本没有发件人认证，任何人都能声称任何 nick），所以闸门无法区分。**频道里照常工作**（频道名是真会话标识）。⛔ **配对已对这两个平台禁用** —— 平台必须对发件人做过认证。这不是「从能用退化为部分能用」，是**从能用变成没人能用**。
   - **配对是逐会话的，不是逐人的。** 在群里执行 `--pair` 加进去的是**那个群**，不是你自己的私聊；群里其他人抄走码也能在你机器上执行它，效果相同（授权的是码所指向的那个会话）。
   - **轮换 `pairing_secret` 只让未兑换的码失效，不撤销已完成的配对** —— 授权已经物化进 `allowed_chat_ids` 了。换 secret **不能**用来把人踢出去，要踢就改 `allowed_chat_ids`。

   **逐平台是否支持配对**：

   | 支持 | 不支持 |
   |---|---|
   | telegram / slack / discord / matrix / mattermost / ntfy / email | **irc / twitch**（无发件人认证）、**nextcloud**（principal 是 OCS token，用户不知道也不该知道）、**homeassistant**（entity_id）、**a2a**（peer）、**qqbot**（principal 是同群所有人共享的会话 id，拿它当配对锚点会连整个群一起授权） |

   ⛔ 不支持的平台上，`/pair` 不会给码，未授权的会话**只能手改 `allowed_chat_ids`**。
3. **`opencode_url` 留空时会自动读取 `~/.local/state/opencode/service.json`**（含密码），因此本桥接**只应在本机 / 可信网络上运行**，不要把端口暴露到公网。
4. **桥接进程拥有和你一样的文件与 shell 权限**：它驱动的是本机 opencode agent，请只在你信任的目录、你信任的 bot token 下运行；配置文件与 `state.json` 含敏感信息，请妥善保管。

> 仓库已通过 `.gitignore` 排除 `config.json` / `state.json` / `plugin/config.json` / `*.log` / `.bridge-plugin.lock` 等运行态文件，请不要把它们提交上来。

## 8. 已知限制

- 流式显示依赖服务端事件 `session.text.delta`；若服务端不推送该事件，只能在任务结束时（`session.execution.succeeded` / `session.idle`）一次性收到结果。
- **各平台的「编辑消息」能力不一致**（流式进度更新依赖它）：Telegram / Discord / Slack / Mattermost 原生支持；**Matrix** 用 MSC2676 兼容近似（多发一条带 `* ` 前缀与 `m.replace` 关系的事件，不支持该写法的客户端会当成新消息 —— 内容不丢但会多一条）；**IRC / Twitch 完全没有编辑**，`edit()` 恒返回 `False`，此时进度更新会退化成连续发多条消息。
- **IRC 的正文换行会被折成空格**：IRC 是单行协议，无法承载换行；原样发出会截断整行甚至被注入命令。这是协议约束，不是静默丢内容。
- **Twitch 的消息长度上限（默认 400 字符）与限流阈值是社区经验值**，非官方文档公开常量，可能随平台调整。
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
| 发消息没有回复、日志见 **opencode 侧** `HTTP 409` | 会话正忙（上一个任务还在跑）。消息会自动排队，当前任务结束后补发；也可 `/stop` 打断当前任务 |
| telegram 发消息没反应、日志见 `transport[telegram]: 会话出错` 且含 `code=409` | ⚠️ **这个 409 与上面那个不是一回事**：它是**同一 bot token 有第二个 `getUpdates` 消费者**（你自己写的脚本 / 另一个 bot 程序 / 上一个实例没退干净）。**消息被第二消费者吃掉，不会排队、也不会补发** ⇒ 停掉那个消费者。详见「接入平台引导 → Telegram」的常见坑 |
| 日志见 `provider.transport` 重试（`⏳ 重试中 (attempt N): ...`） | 上游模型服务不可达 / 超时，opencode 正在按退避重试；检查网络与 provider 配置 |
| `没有任何可用适配器` | 配置未完成**不再报错退出**（exit 0 + 提示）。按「接入平台引导」填好**该平台自己的凭据键**（不都是 `bot_token`：Slack 入站另需 `app_token`、Matrix 用 `homeserver`/`access_token`/`user_id`、IRC 用 `host`/`nick`/`channels`、Mattermost 用 `site_url`/`token`、Twitch 用 `token`/`channel`）后重启即可生效；也可在插件 `config.json` 设 `enabled: false` 暂停拉起 bridge。用 `--status` 逐平台核对缺什么 |
| bot 无响应但日志有 `dropped message from non-whitelisted chat` | 该 chat 不在 `allowed_chat_ids` 白名单中。⚠️ 若是升级后**突然**收不到消息、而你的 `allowed_chat_ids` 是空的：多半是这次翻转。查 `config.json` 的 `config_version` —— **`< 2`**（含没有这个键）时是「空 = 全放行」，**`>= 2`** 则是新语义「空 = 全拒」。解法见「7. 安全须知」的「两步走」：`/pair` 在未授权的 chat 上就能用。⚠️ **日志里那个 chat 已经不是原值了**：脱敏层只认 `platform:local_id` 这种带前缀的形式（裸 id 按形状无法脱敏 —— discord 雪花号本身就是个合法纳秒时间戳），所以落盘形态是 `<平台>:conv#<6位>-<6位>`，例如 `telegram:conv#4f5307-ab5f8c`。**同一个会话在多行日志里仍是同一个 `conv#`** ⇒ 「是不是同一个人 / 同一个 chat 在刷屏」照样查得到；**跨进程 / 跨重启对不上**（摘要密钥只在内存里，别拿昨天的 `conv#` 对今天的） |
| irc / twitch 私聊没反应（频道里正常） | **已知的行为变更**：私聊的 principal 是 bot 自己的 nick，闸门无法区分 ⇒ 白名单为空且 `config_version >= 2` 时必然被拒。频道不受影响（频道名是真会话标识）。这两个平台**不提供 `/pair`**（无发件人认证），只能在 `allowed_chat_ids` 里手填 |
| irc / twitch 启动失败，日志有 `拒绝启动` 且写着「**这一条会把所有人的私聊一起放行**」 | `allowed_chat_ids` 里写了本适配器**自己的 nick**。⛔ 这一行不是「只授权你自己」：**这一条会把所有人的私聊一起放行**（私聊的 principal 就是 bot 自己的 nick，所有人的私聊共用它）⇒ 桥接**拒绝启动**，而不是打个 warning 继续跑。**改法**：把这一行从 `allowed_chat_ids` 里删掉；频道授权（`#channel`）不受影响。⚠️ Twitch 报这条时 nick 可能来自 Helix 而非配置（配置里没写 `nick` 也一样会被拒） |
| irc / twitch 日志有 `dropping private message from non-whitelisted nick`（**这条路径没有可用的发件人身份**） | 私聊被拒。**这不是「你不在白名单里」** —— 私聊的 principal 是 bot 自己的 nick，两个平台的协议层都没有发件人认证，闸门分不出是谁在私聊，所以这条路径**本来就没有可授权的身份**。频道里照常工作 |
| 收不到 `/pair` 的回信 | 多半是 `pairing_secret` 没填（**留空 = 不提供配对**，不会用空串派生）。生成后**要重启桥**才会生效；`irc / twitch / nextcloud / homeassistant / a2a / qqbot` 本身不支持配对 |
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
