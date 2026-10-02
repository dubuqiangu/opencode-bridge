# opencode-bridge — Installation Guide

## For Humans

原生插件命令（推荐）：

```text
opencode plugin add github:dubuqiangu/opencode-bridge
opencode plugin update github:dubuqiangu/opencode-bridge
opencode plugin remove github:dubuqiangu/opencode-bridge
```

`add` 装好后只需填 bot token + `opencode service restart`（重启需用户点头）。

中文（发给 AI Agent）：

```
帮我安装 opencode-bridge：https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/install.md
```

English:

```
Install opencode-bridge: https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/install.md
```

---

## For AI Agents

### Goal

装好 opencode-bridge：Telegram/Slack/Discord ↔ 本机 opencode 的消息桥。
Python 3.10+ **仅标准库**，安装过程**不得 pip install 任何东西**。

### ⚠️ Boundaries

- DO NOT 执行 `opencode service restart|stop`（会打断用户当前会话）——**必须先征得用户明确同意**才可以执行
- DO NOT 调用 `POST /api/location/reload`
- DO NOT 修改 `~/.config/opencode/opencode.json`（全局配置）
- DO NOT 在 bridge 目录 / 插件目录之外创建或修改文件（临时文件用系统 `%TEMP%` / `/tmp/`）
- DO NOT sudo / apt / winget / 系统级装包
- DO NOT git commit / push
- DO NOT 打印、回显、提交 bot_token / opencode_password 等密钥
- DO NOT pip install（项目零第三方依赖）

### 📁 Directory Rules

| 目录 | Windows | macOS / Linux |
|---|---|---|
| bridge 目录（clone、cwd、config.json、日志） | `%USERPROFILE%\.config\opencode-bridge` | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge` |
| 插件目录（index.ts + package.json + config.json{bridgeDir}） | `%USERPROFILE%\.config\opencode\plugins\bridge` | `$HOME/.config/opencode/plugins/bridge` |

> **插件目录的 `config.json` 与 bridge 目录的 `config.json` 是两份，不要混**：
> - bridge 目录 `config.json`：用户配置（`adapters.*.bot_token`、`allowed_chat_ids`、`opencode_password`…）；
> - 插件目录 `config.json`：插件自身配置（`{"bridgeDir": "<bridge 目录的绝对路径>"}`，可加 `enabled: false`）。

### Step 1: 安装

#### 方式 1：原生 `plugin add`（首选，一条命令）

```text
opencode plugin add github:dubuqiangu/opencode-bridge
```

- `add <package>` 的 package 参数是 **npm registry or Git package specifier**；后续 `opencode plugin update github:dubuqiangu/opencode-bridge` / `opencode plugin remove github:dubuqiangu/opencode-bridge` 用**同一条完整 specifier**（不是短 id）。安装 = 把本仓库作为插件包装进全局配置。
- **自举**：插件首次启动会自动把包内 Python 源码与 `config.example.json` 铺到稳定 bridge 目录（Windows `%USERPROFILE%\.config\opencode-bridge`，Unix `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge`），已有 `config.json` **绝不覆盖**；若稳定目录本身是 git clone（脚本装法），自举**完全不碰它**（走安装器 / `git pull`）。
- **装完只剩两步**：① 按 Step 2 填 bot token；② `opencode service restart`（重启会打断会话，**需用户点头**）。
- **Python 3.10+ 仍需本机自带**（插件不安装 Python）。
- 建议在用户主目录 `~` 下执行 `plugin update`（工作区目录偶发 `Plugin is not configured`，换目录重试即可）；`plugin list` 显示的 commit 可能落后 update 一拍，再执行一次 update / list 对齐。

方式 1 失败或不可用时，用下面的脚本兜底（按顺序尝试，成功即停）。

#### 方式 2：安装脚本兜底（A/B/C）

**A. 标准（GitHub 域名可达时）**

Windows：

```powershell
iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 | iex
```

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash
```

**B. 兜底：jsDelivr 镜像**（`raw.githubusercontent.com` DNS 污染、jsDelivr 可达时）。
注意：jsDelivr 对 `.ps1` 返回 `application/octet-stream`，`iwr | iex` 会按**本地 ANSI 编码**解码 UTF-8 源码导致解析失败，所以 **Windows 必须先落盘 `-File` 执行**：

```powershell
$p="$env:TEMP\ocb-install.ps1"; iwr 'https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.ps1' -OutFile $p; powershell -NoProfile -ExecutionPolicy Bypass -File $p; Remove-Item $p
```

macOS / Linux：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.sh -o /tmp/ocb.sh && bash /tmp/ocb.sh
```

**C. 兜底：直接 git clone**（GitHub 443 可达但 raw 被墙时最简单）：

```powershell
git clone https://github.com/dubuqiangu/opencode-bridge "$env:USERPROFILE\.config\opencode-bridge"; powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.config\opencode-bridge\install.ps1" -Force
```

安装脚本会自动完成三件事（不联网执行任何安装后动作，**绝不会**替你执行 `opencode service restart`）：

1. 把本仓库 clone（或本地复制）到 **bridge 目录**，并从 `config.example.json` 生成 `config.json`；
2. 把插件文件 `index.ts` + `package.json` 安装到**插件目录**；
3. 写入插件 `config.json`（内容为 `{"bridgeDir": "<bridge 目录的绝对路径>"}`），已有 `bridgeDir` 时不覆盖（加 `-Force` / `--force` 可强制覆盖）。

### Step 2: 选择平台并配置 token

**先问用户要接哪个平台**（Telegram / Slack / Discord），然后按用户选择给对应引导。内容与仓库 README「接入平台引导」一致：

#### 配置文件位置

| | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.config\opencode-bridge\config.json` |
| macOS / Linux | `${XDG_CONFIG_HOME:-~/.config}/opencode-bridge/config.json` |

#### Telegram（支持双向）

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

> ⚠️ **`allowed_chat_ids` 是安全边界**：留空 = 任何人都能驱动你的 agent（任意能给 bot 发消息的人可以以用户权限执行操作）。务必填入用户自己的纯数字 chat id。

#### Slack（支持双向对话 · Socket Mode，无需公网地址）

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

   （要 `@` 才响应加 `app_mentions:read`；用私有频道加 `groups:history`）

5. 同页顶部 **Install to Workspace** → **Allow** → 复制 **Bot User OAuth Token**（`xoxb-` 开头）
   ⚠️ 之后**每改一次 scope 都要回来点一次 Reinstall to Workspace**
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
> - **Event Subscriptions 没打开、或事件没加在 *bot events* 下，会「静默收不到」且不报错**。事件必须用 **Add Bot User Event** 添加。
> - **只填 `bot_token` 也能启动，但那只发不收**：入站必须有 `app_token`（`xapp-`），桥接会记一条 warning。
>
> `allowed_chat_ids` 白名单同样适用：Slack 侧没有"谁能跟 bot 说话"的原生白名单，只能靠频道邀请与本桥接自己的白名单。

#### Discord（支持双向对话 · Gateway v10 WebSocket，无需公网地址）

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
> - **第 3 步的开关不开，网关会直接拒绝连接（close 4014）**，日志里会写明原因。
> - **bot 必须已被邀请进频道**，否则发消息报 `not_in_channel`。

#### Matrix（支持双向对话 · `/sync` 长轮询，无需公网地址）

Matrix 没有"建 App 再邀请进频道"的模型 —— 直接用**你的账号（或一个专门的服务账号）**作为对端。

1. 拿到 `access_token` 与自己的 user id，二者任一途径都能取到：
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

> **`user_id` 必须填对** —— 它是过滤自己回声的依据。留空或填错会让桥接把自己发出的消息
> 当成入站消息收回来，形成**无限回环**（表现为自己跟自己对话）。
>
> `allowed_chat_ids` 填**房间 id**（形如 `!abcDEF:example.org`），不是 user id。
> 编辑消息用 MSC2676 兼容写法（多发一条带 `* ` 前缀与 `m.replace` 关系的事件），
> 不支持该写法的客户端会当成一条新消息 —— 内容不丢，但会多一条。

#### Mattermost（支持双向对话 · WebSocket，无需公网地址）

1. 拿一枚 token：登录 Web UI → 左下头像 → **Profile** → **Security** → **Personal Access Tokens** → **Create New Token**（个人访问令牌可随时吊销且不影响登录）
   自己的 user id 不用手填 —— 桥接启动时自动调 `GET /api/v4/users/me` 取
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "mattermost": { "site_url": "https://mm.example.com", "token": "你的令牌" } }
   ```

3. 执行 `opencode service restart`
4. 私聊这个账号，或在**它已加入**的频道发一句 `hi`，收到回复即成功

> - **防回环只能靠"是不是我自己发的"**（比对 `users/me` 的 id）。取不到时桥接会**整个停摆入站**并在日志提示，而不是冒险无限回环。
> - `allowed_chat_ids` 填 **channel id**（26 位字符串），不是 user id。
> - **消息长度上限不在配置里**：由服务端运行时决定，桥接启动时读 `config/client` 的 `MaxPostSize` 用来分片。
> - 反向代理部署若握手 404/400，先查 `WebsocketURL` / `WebsocketPort` 与代理是否透传 `Upgrade` 头。

#### IRC（支持双向对话 · TCP）

1. 选一个 IRC 网络，确认端口与是否 TLS（明文常用 `6667`，TLS 常用 `6697`）
2. 编辑配置文件：

   ```json
   "adapters": { "irc": {
     "host": "irc.libera.chat", "nick": "你的昵称",
     "channels": ["#你的频道"], "use_tls": true
   } }
   ```

3. 执行 `opencode service restart`
4. 在频道里 **`@你的昵称` / `昵称:`** 发一句话，或直接私聊该昵称

> - **只响应提及或私聊**。**`channels` 是入站前提**，留空则只能主动发。
> - **IRC 无编辑消息**，长任务进度会退化成连续发多条消息。
> - **正文换行折成空格**（单行协议约束，原样发会被注入命令）。整行上限 512 字节。
> - 昵称冲突（`433`）会自动换名重试；`bot_password` 填密码即启用 SASL PLAIN。

#### Twitch（支持双向对话 · IRC over TLS WebSocket）

1. 打开 <https://dev.twitch.tv/console/apps> → **Register an Application** → 填 Name
2. 复制页面上的 **OAuth Token**（聊天用）与 **Client ID**
3. 编辑配置文件：

   ```json
   "adapters": { "twitch": { "token": "OAuth Token", "channel": "频道名" } }
   ```

4. 执行 `opencode service restart`
5. 在频道里 **`@你的昵称`** 发一句话，收到回复即成功

> - **token 粘 `OAuth Token` 原值**，桥接自动加 `oauth:` 前缀（自己加会变成 `oauth:oauth:...`）。
> - `channel` 填**小写频道名、不带 `#`**。**只响应提及**。命令前缀是 `!`。
> - **Twitch 无编辑消息**，长任务进度会退化成连续发多条消息。
> - 400 字符上限与限流阈值是**社区经验值**，非官方公开常量。

> 填好 token 后，可在 bot 里发送 **`/setup`** 查看 / 重温 Telegram / Slack / Discord 三个平台的引导（`/setup telegram`、`/setup slack`、`/setup discord` 可直达）。
> **Matrix / Mattermost / IRC / Twitch 暂未纳入 `/setup` 引导**（菜单是刻意维护的固定文案），请按本节配置；配置是否齐全一律用 `--status` 核对 —— 它会列出所有已注册平台，并区分「配置齐备」与「入站就绪」。

### Step 3: 激活（需要用户点头）

- 插件文件已更新，但**运行中的 opencode 进程持有旧副本**，生效需 `opencode service restart`（或关闭并重新打开 opencode TUI）。
- **必须先问用户是否现在重启**——这会打断当前会话，未获用户明确同意前不要执行。
- 用户拒绝时的替代方案（二选一，给用户说明即可，由用户自行决定）：
  1. **`enabled: false` 停用方案**：在插件目录的 `config.json` 中设 `"enabled": false`，插件什么都不做，等下次用户自行重启再改回；
     - Windows：`%USERPROFILE%\.config\opencode\plugins\bridge\config.json`
     - macOS / Linux：`$HOME/.config/opencode/plugins/bridge/config.json`
  2. **下次自行重启**：告诉用户「下次打开 / 重启 opencode 时新插件自动生效」，本次不再操作。

### Step 4: 验证

1. 连通性自检（只查 `/api/info`，不创建会话）：

   ```powershell
   cd <bridge 目录>
   python -m opencode_bridge --check
   ```

   期望输出含 `opencode service OK`，exit 0。

2. 单元测试：

   ```powershell
   python -m unittest discover -s tests
   ```

   期望 `Ran 113 tests` / `OK (skipped=1)`（若版本更新后数字变化，以 `OK` 为准，且 0 FAIL/ERROR）。
   注：`tests/` 与 `opencode_bridge/` 已随包分发；方式 1 自举到稳定 bridge 目录后，`python -m unittest discover -s tests` 用法不变（把目录指向稳定 bridge 目录执行即可）。

3. （可选）插件自检（离线，不碰 opencode 服务）：

   ```powershell
   cd <插件目录>
   bun harness.ts
   ```

   期望 `PASS 15/15`。

4. **未配置 token 时的行为（不要当成失败）**：bridge 打印 `没有任何可用适配器：请在 config.json 的 adapters 中配置 bot_token` + 提示后 **exit 0**——这是优雅退出，不是崩溃，插件**不会**进 backoff。配置完成即可正常运行。

5. 端到端：配好 token 后手动给 bot 发 `hi`，收到回复 = 成功。

### Step 5: 汇报

向用户汇报：

- 装到了哪两个目录（bridge 目录、插件目录的绝对路径）
- 填了哪个平台（Telegram / Slack / Discord）
- 是否重启了 opencode（或用户选择暂不重启）
- 验证结果（--check / unittest / 可选 harness）
- 还需要用户做的事（列清楚，如：自行填 token、下次重启、给 bot 发 hi 等）

### Quick Reference

| 操作 | 命令 / 说明 |
|---|---|
| 原生安装（首选） | `opencode plugin add github:dubuqiangu/opencode-bridge` |
| 原生更新 | `opencode plugin update github:dubuqiangu/opencode-bridge`（建议在 `~` 下执行） |
| 原生卸载 | `opencode plugin remove github:dubuqiangu/opencode-bridge` |
| 连通性自检 | `cd <bridge 目录> ; python -m opencode_bridge --check` |
| 调试日志 | `python -m opencode_bridge --verbose` |
| 手动运行 | `cd <bridge 目录> ; python -m opencode_bridge` |
| 卸载（Windows） | `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Uninstall` |
| 卸载（macOS/Linux） | `bash install.sh --uninstall` |
| 暂停插件 | 插件 `config.json` 中设 `"enabled": false` |
| 重温平台引导 | 在 bot 内发送 `/setup`（或 `/setup telegram` / `slack` / `discord`） |
