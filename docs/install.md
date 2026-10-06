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
>
> 这不是"配错了才出事"：安装脚本从 `config.example.json` 生成的 `config.json` 里，`allowed_chat_ids` **就是空数组**，而且 **`config_version` 是 `0`（< 2）** ⇒ 现在仍是「空 = 全部放行」。只填 `bot_token` 就重启的话，你得到的是一个**完全开放**的桥接。三个平台（Telegram / Slack / Discord）都是如此。
>
> ⚠️ **下一版起这里会变成「空 = 谁都不放行」**（由 `config_version` 兜底：`< 2` = 旧语义，`>= 2` = 新语义；启动时会有醒目预告）。届时**不必手改配置文件**：填顶层键 `pairing_secret` → 重启 → 在 bot 里发 `/pair` 拿码 → 本机 `python -m opencode_bridge --pair <码> --conversation platform:local_id` → **再重启一次**。三条边界见 README「7. 安全须知」，其中一条必须先知道：**irc / twitch 的私聊在翻转后对所有人都不可用**（这两个平台也不提供 `/pair`）。

> **收不到消息怎么查（五个常见坑）**
>
> 这五条的共同点是**几乎全部静默** —— 发什么都没人回，IM 里也不会有任何报错提示。
> **先定位日志**：手动运行（`python -m opencode_bridge`）时它直接打在终端；**由插件拉起**时进 **bridge 目录下的 `bridge-output.log`**（⚠️ 该文件由 Python 按**系统代码页**写出，中文 Windows 是 GBK，用文本编辑器打开是乱码就换 GBK 打开；想看更细的内容用 `--verbose`）。下面每条都给出日志里能搜到的**原文关键字**。
>
> 1. **token 无效 / 被吊销 / 复制时多打一个字符**。**症状：完全静默** —— 发什么都没人回，而 `--setup --json` 里照样是 `configured: true` / `inbound_ready: true`（这两个字段**只检查 token 字符串非空，不验证它是否真的有效**）。
>    **去日志搜**：`telegram: getMe failed` —— ⚠️ 它有**两种形态，两个都要认**：
>    - `telegram: getMe failed (code=401): ...; adapter not started` —— token 被 Telegram **拒绝**（无效 / 已吊销）。
>    - `telegram: getMe failed (<异常文字>); adapter not started` —— **压根没连上**（网络被墙、代理没配、DNS 不通）。**这一条里没有 `code=`**，只按 `code=` 去搜会**什么都搜不到**。
>    **处理**：回 **@BotFather** 重新 `/newbot` 拿一枚 token，逐字复制（别连空格/换行一起复制）填进 `config.json`，再 `opencode service restart`。
> 2. **同一个 bot token 有第二个 `getUpdates` 消费者** —— 你自己写的脚本、另一个 bot 程序，或上一个实例没退干净。Telegram 对"同一 bot 有两个长轮询消费者"返回 **409**，桥会**恒定每 2 秒**重试一次，日志因此反复刷同一行。**症状同样是静默**：消息被那个第二消费者吃掉了。
>    **去日志搜**：`transport[telegram]: 会话出错: ...`，且这一行里含 `code=409`。
>    **处理**：把第二个消费者停掉。⚠️ `opencode-bridge` 自己双开时会给明确提示（`已有另一个 bridge 实例在运行（pid=...）`），**这一条管的是本机之外的消费者**：脚本、另一个 bot 程序。
>    ⚠️ 这个 409 与 README「9. 故障排查」里"会话正忙"那个 409 **不是一回事**：后者是 opencode session 忙，消息会排队、当前任务结束后自动补发。
> 3. **在群里发消息，bot 一声不吭**。这有**两个互相独立**的原因，两个都要排干净：
>    - **@BotFather 的 privacy mode 默认开着** ⇒ Telegram **服务端根本不下发**非提及消息，这是**平台侧**的事，桥里**没有这个旋钮**。⇒ **去 BotFather 对该 bot 关掉 privacy mode**。
>    - **私聊 id ≠ 群 id**：群 chat id 是**负数**（形如 `-1001234567890`），而 **@userinfobot** 给的是**正数的 user id**（只有私聊能用它）。⇒ **去 `@RawDataBot`**（或在群内用 Telegram 客户端 / 第三方工具）**拿到那个负数群 id**，把**它**填进 `allowed_chat_ids`。
> 4. ⚠️ **群里不需要 @ bot —— 这有安全含义**。Telegram 适配器**没有任何 mention 过滤**：群里**每一条文本消息**都会被当成"有人在对 agent 说话"送进模型（对照 **IRC / Twitch 只响应提及**）。所以把群 id 加进 `allowed_chat_ids` 之前先想清楚：**那个群里所有人、所有话都会以你的权限驱动 agent**。
> 5. **只收文本消息**。图片 / 语音 / 贴纸等**非文本** update 一律**静默丢弃**（`text` 字段不是字符串就直接 return）。⚠️ 而 `capabilities()` 报的 `supports_media: true` 指的是「**能发出站媒体**」，**不是「能收媒体」** —— README 平台能力表里 Telegram 行的媒体能力也是这个意思。

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
> - ⚠️ **翻转成「空 = 全拒」（`config_version >= 2`）后，IRC 的私聊对所有人不可用** —— 私聊时闸门比对的 principal 是 **bot 自己的 nick**，IRC 根本没有发件人认证 ⇒ 闸门无法区分。**频道里照常工作**（`#channel` 是真会话标识）。⛔ **IRC 不提供 `/pair` 配对**，白名单只能手填。
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
> - ⚠️ **翻转成「空 = 全拒」（`config_version >= 2`）后，Twitch 的私聊对所有人不可用**（原因同 IRC）。**频道里照常工作。** ⛔ **Twitch 不提供 `/pair` 配对**。

#### Nextcloud Talk（支持双向对话 · HTTP 长轮询，无需公网地址）

1. 生成 **app password**：登录 Nextcloud → 右上角设置 → **安全** → **设备专属密码** → **创建新密码**（可单独吊销、不影响登录、不过期）。**建议用独立的机器人账号**。
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "nextcloud": {
     "base_url": "https://cloud.example.com",
     "username": "my-bot", "password": "app-password-xxxx"
   } }
   ```

   `base_url` **要含子路径前缀**（如 `https://host/nextcloud`）。
3. 执行 `opencode service restart`
4. 私聊机器人，或在它已加入的会话里发一句 `hi`，收到回复即成功

> **三个最容易写错、且都是"静默失败"的地方**
> - **`OCS-APIRequest` 必须是字面量小写 `true`** —— 服务端**严格字符串比较**，`True` / `1` / `yes` 一律被判 CSRF 攻击并返回 **403**。
> - **只走 `ocs/v2.php`** —— `ocs/v1.php` 的 HTTP 状态码**恒为 200**，失败看不出来。
> - **`304` 不是错误** —— 长轮询"无新消息"时服务端返回 304，而 `urllib` 把它**抛成 `HTTPError`**。
>
> 其它：`allowed_chat_ids` 填**会话 token**；⛔ **nextcloud 不支持 `/pair` 配对** —— 这里的 principal 就是那个 OCS token，用户既不知道也不该知道它，所以**只能手填**，不能靠配对自助授权；消息上限 **32000 字符是源码硬编码常量、不可配置**（超限 413）；**支持编辑消息**但**超 24 小时不能改**；`poll_timeout` **上限就是 30**（源码 clamp）；`@提及` 不做渲染（模板串原样透传）。

#### ntfy（支持双向对话 · HTTP 拉取，无需公网地址）

1. 建一个话题（topic）
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "ntfy": {
     "server": "https://ntfy.sh", "topic": "my-private-topic", "token": "tk_..."
   } }
   ```

3. 执行 `opencode service restart`
4. 往话题发一条通知（`curl -d "hi" ntfy.sh/my-private-topic`），收到回复即成功

> ⚠️ **ntfy 没有用户身份概念** —— 任何能往话题发消息的人都会被当作用户。用**公共话题**
> 等于把 agent 暴露给全网。**务必**用私有话题 + read token（或自建服务器开 access control）。
>
> 其它：`allowed_chat_ids` 填**话题名**；上限 **4096 是字节不是字符**（中文约 1365 字）；
> **不支持编辑消息**，长任务进度会退化成连续发多条通知；**启动不重放历史缓存**。

#### email（支持双向对话 · IMAP + SMTP，无需公网地址）

1. 准备一个**专用邮箱**，并开 **app 专用密码**（Gmail / Outlook 都需先启用两步验证）
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "email": {
     "address": "mybot@example.com", "password": "abcd efgh ijkl mnop",
     "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"
   } }
   ```

3. 执行 `opencode service restart`
4. 给 `mybot@example.com` 发一封邮件，收到回复即成功

> ⚠️ **邮件没有用户身份概念** —— 任何能给这个地址发信的人都会被当作用户，**必须**用
> `allowed_chat_ids` 限定发件人，否则等于把 agent 暴露给任何知道你邮箱地址的人。
>
> ⚠️ **凭据即完整信箱权限** —— 用 **app 专用密码**而非主密码：泄漏时损失被限制在
> 那一个账号，且可单独吊销。
>
> 其它：`imap_host` / `smtp_host` **无默认值**（按域名猜对自建/企业邮箱是错的）；
> **不重放历史**（首连只记水位线）；**不碰已读状态**（用 `BODY.PEEK[]`）；出站 Subject
> 带 `[opencode]` 且记 `Message-ID` 做**防回环**（你回复它不会被误丢）；单行上限 998
> （RFC 5322）；**不支持编辑邮件**；用 `UID` 游标而非 `UNSEEN`（你在手机点开仍能被看见）。

#### QQ Bot（支持双向对话 · WebSocket 网关，无需公网地址）

1. 在 [QQ 开放平台](https://bot.q.qq.com/) 建机器人，拿 **AppID** 与 **AppSecret**
2. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "qqbot": { "app_id": "102xxxxx", "app_secret": "xxxx" } }
   ```

3. 执行 `opencode service restart`
4. 在开放平台后台把机器人加进群 / 频道，或直接私聊它

> 其它：支持**群聊 / 私聊（C2C）/ 频道**三种作用域，`allowed_chat_ids` 填对应前缀
> （`qqbot:group:...` / `qqbot:c2c:...` / `qqbot:channel:...`）。**主动消息在群里会失败**
> （`40034105`），桥接会带上传入站的 `msg_id` 与 `msg_seq`；被动回复有时效与次数上限
> （群 5 分钟 5 次、私聊 1 小时 4 次）。**不支持编辑消息**（官方只有撤回）。
> 未与真实 QQ 客户端做过互操作验证。

#### Home Assistant（支持双向对话 · WebSocket 事件总线，无需公网地址）

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

> ⚠️ **不配过滤条件就一个事件都收不到（刻意设计）**：HA 推的是**设备状态变更**而不是
> "某人给你发消息"，只有能追溯到**真人用户操作**的事件才适合起对话（定时器 / 脚本触发
> 的事件 `context.user_id` 为空）。所以必须给 `entities` / `domains`，或显式
> `accept_all: true`；`require_user_context` 默认 `true`。
>
> ⚠️ 只配 `url` + `token` 时 `--status` 会显示「已配置 / 入站就绪」，**但实际收不到
> 任何事件** —— 所以 `capabilities()` 给了机器可读判据：`inbound_accepts_anything: false`
> 就是"配好了但收不到"的明确信号（`--setup --json` 的 `platforms[].capabilities` 可直接读到），启动日志也会打一次
> WARNING。
>
> 其它：`homeassistant.local` 是 **mDNS 惯例**、不是官方规定；`event_types: ["*"]`
> 通配订阅**需要管理员**；两层保活方向相反（传输层由 aiohttp 发 ping、`ws.py` 自动回，
> 应用层 JSON ping 必须客户端主动发）；出站走 `call_service`；**不支持编辑消息**。

> 填好 token 后，可在 bot 里发送 **`/setup`** 查看 / 重温 Telegram / Slack / Discord 三个平台的引导（`/setup telegram`、`/setup slack`、`/setup discord` 可直达）。
> **Matrix / Mattermost / IRC / Twitch / Nextcloud Talk / ntfy / email / a2a / QQ Bot / Home Assistant 暂未纳入 `/setup` 引导**（菜单是刻意维护的固定文案），请按本节配置；配置是否齐全一律用 `--status` 核对 —— 它会列出所有已注册平台，并区分「配置齐备」与「入站就绪」。
>
> ⚠️ 但 `--status` 的「入站就绪」只代表**凭据齐备且入站已实现**，不代表"真的会收到消息"：
> **Home Assistant** 默认**一个事件都不收**（必须另配 `entities`/`domains`/`accept_all`），
> 判据是 `--setup --json` 里 `platforms[].capabilities.inbound_accepts_anything`。

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
