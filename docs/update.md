# opencode-bridge — Update Guide

## For Humans

原生插件更新（推荐，建议在用户主目录 `~` 下执行）：

```text
opencode plugin update github:dubuqiangu/opencode-bridge
opencode plugin add github:dubuqiangu/opencode-bridge
opencode plugin remove github:dubuqiangu/opencode-bridge
```

`update` / `remove` 与 `add` 使用同一条完整 specifier；`plugin list` 显示的 commit 可能落后一拍，再执行一次 update/list 对齐。

中文（发给 AI Agent）：

```
帮我更新 opencode-bridge：https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/update.md
```

English:

```
Update opencode-bridge: https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/docs/update.md
```

---

## For AI Agents

### ⚠️ Workspace Rules

- 不碰用户工作区文件：只允许操作 bridge 目录（clone 目录）与插件目录
- DO NOT 修改 `~/.config/opencode/opencode.json`（全局配置）
- DO NOT 执行 `opencode service restart|stop`——**必须先征得用户明确同意**才可以执行
- DO NOT 调用 `POST /api/location/reload`
- DO NOT 打印、回显、提交 bot_token / opencode_password 等密钥
- DO NOT sudo / apt / winget / 系统级装包
- DO NOT git commit / push
- DO NOT pip install（项目零第三方依赖）
- 临时文件用系统 `%TEMP%` / `/tmp/`，不在 bridge / 插件目录之外创建或修改文件

目录约定（与 install 文档一致）：

| 目录 | Windows | macOS / Linux |
|---|---|---|
| bridge 目录 | `%USERPROFILE%\.config\opencode-bridge` | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge` |
| 插件目录 | `%USERPROFILE%\.config\opencode\plugins\bridge` | `$HOME/.config/opencode/plugins/bridge` |

> **插件目录的 `config.json` 与 bridge 目录的 `config.json` 是两份，不要混。**

### Goal

更新到最新版、**保留用户 `config.json`**、刷新插件文件、验证可用。

### Step 1: 检查当前版本

```powershell
git -C <bridge目录> rev-parse HEAD
```

对照远端 main：

```bash
gh api repos/dubuqiangu/opencode-bridge/commits/main --jq .sha
# gh 不可用时：
git ls-remote origin refs/heads/main
```

（在 bridge 目录里执行 `git ls-remote origin refs/heads/main` 即可。）

- 相同 → 已是最新，**跳到 Step 4 验证**。
- 不同 → 继续 Step 2。

### Step 2: 更新

#### 方式 1：原生 `plugin update`（首选，建议在 `~` 下执行）

```text
opencode plugin update github:dubuqiangu/opencode-bridge
```

- `update` 使用与 `add` 相同的完整 specifier（不是短 id）。
- **建议在用户主目录 `~` 下执行**：工作区目录偶发 `Plugin is not configured`，换到 `~` 重试即可。
- `plugin list` 显示的 commit 可能落后 update 一拍，再执行一次 update / list 对齐。
- 刷新语义：非 git 的自举目录 → 刷新 `opencode_bridge/` 源码、保留用户 config / state / 日志；git clone 目录 → 走安装器 / `git pull`。

方式 1 不可用时，方式 2 兜底。

#### 方式 2：重跑安装器

重跑安装器（按 install 文档的 A/B 兜底顺序，Windows / macOS 各给对应命令）——它内部就是 `git pull --ff-only` + 拷插件：

**A. 标准**：

```powershell
iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash
```

**B. jsDelivr 兜底**（raw 被墙时；Windows 必须落盘 `-File` 执行）：

```powershell
$p="$env:TEMP\ocb-install.ps1"; iwr 'https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.ps1' -OutFile $p; powershell -NoProfile -ExecutionPolicy Bypass -File $p; Remove-Item $p
```

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/dubuqiangu/opencode-bridge@main/install.sh -o /tmp/ocb.sh && bash /tmp/ocb.sh
```

安装器**自动保留**：

- `<bridge 目录>/config.json`（含用户 token）：已存在则「保持不变」；
- 插件 `config.json`：已有 `bridgeDir` 时不覆盖（`-Force` / `--force` 才强制覆盖）。

### Step 3: 共存规则（DO NOT）

- **不要**删除或重建用户的 `config.json` / `state.json`（只有卸载才动它们）
- **不要**随手加 `-Force`（`-Force` 只在修 `bridgeDir` 时需要）
- **不要** `git reset --hard` / 删 clone 目录来「重装」，除非用户明确要求
- 自举刷新只覆盖 `opencode_bridge/` 源码，用户 `config.json` / `state.json` **永不覆盖**
- 稳定目录本身是 git clone 时，自举**不碰它**（走安装器 / `git pull`）

### Step 4: 验证

1. SHA 追平远端 + 工作区干净：

   ```powershell
   git -C <bridge目录> rev-parse HEAD        # 应等于远端 main SHA
   git -C <bridge目录> status --porcelain     # 应无输出（clean）
   ```

2. 连通性自检：

   ```powershell
   cd <bridge目录>
   python -m opencode_bridge --check
   ```

   期望 `opencode service OK`，exit 0。

3. 单元测试：

   ```powershell
   python -m unittest discover -s tests
   ```

   期望末行为 `OK`（`OK (skipped=N)` 也算通过），0 FAIL/ERROR。⛔ **不要把 `Ran N tests` 的 N 抄进文档**——它随用例增删而变，判据只有 `OK`；条数以该命令的实跑输出为准。

4. （可选）插件自检：

   ```powershell
   cd <插件目录>
   bun harness.ts
   ```

   期望**全部场景 PASS 且退出码 0**——`harness.ts` 末行会打印 `PASS n/n`，任一失败退出码 1。⛔ **不要把 n 抄进文档**：n 随场景增删而变（本仓库曾在三处写下三个不同的 n），判据只有「全部 PASS + 退出码 0」。

> 未配置 token 时 bridge 打印「没有任何可用适配器」+ 提示后 **exit 0**（优雅退出、插件不进 backoff）——不要当成更新失败。

### Step 5: 汇报

- 新旧 SHA + 两者之间的 `git log --oneline` 摘要（改了什么）：

  ```powershell
  git -C <bridge目录> log --oneline <旧SHA>..<新SHA>
  ```

- 提醒用户：**运行中的 opencode 仍持旧插件 / 旧 bridge 进程**，要新功能生效需用户批准执行 `opencode service restart`（不要擅自执行；用户拒绝时可等下次自行重启）。
- 有任何需要用户做的事，列清楚（填 token、重启、给 bot 发消息验证等）。
