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

#### Slack（⚠️ v1 仅支持主动发送）

1. 打开 <https://api.slack.com/apps> → **Create New App** → **From scratch** → 选 workspace
2. 左侧 **OAuth & Permissions** → **Bot User OAuth Token**（`xoxb-` 开头）→ 复制
3. **Event Subscriptions 先保持关闭**（v1 入站轮询尚未接入）
4. 编辑配置文件（路径见上面的「配置文件位置」）：

   ```json
   "adapters": { "slack": { "bot_token": "xoxb-..." } }
   ```

5. 左侧 **Install App** → **Install to Workspace**，把 App 邀请进目标频道
6. 执行 `opencode service restart`

> 能力说明：v1 仅实现主动 `send`/`edit`，入站轮询（`conversations.history`）为 TODO，当前**无法在 Slack 里与 bot 双向对话**。

#### Discord（⚠️ v1 仅支持主动发送）

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

> 填好 token 后，可在 bot 里发送 **`/setup`** 查看 / 重温这套引导；`/setup telegram`、`/setup slack`、`/setup discord` 可直达对应平台的分步引导。

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
