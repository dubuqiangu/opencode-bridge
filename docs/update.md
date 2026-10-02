# opencode-bridge — Update Guide

## For Humans

中文：

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

   期望 `OK`，0 FAIL/ERROR（当前为 `Ran 113 tests` / `OK (skipped=1)`，数字随版本变化以 `OK` 为准）。

4. （可选）插件自检：

   ```powershell
   cd <插件目录>
   bun harness.ts
   ```

   期望 `PASS 15/15`。

> 未配置 token 时 bridge 打印「没有任何可用适配器」+ 提示后 **exit 0**（优雅退出、插件不进 backoff）——不要当成更新失败。

### Step 5: 汇报

- 新旧 SHA + 两者之间的 `git log --oneline` 摘要（改了什么）：

  ```powershell
  git -C <bridge目录> log --oneline <旧SHA>..<新SHA>
  ```

- 提醒用户：**运行中的 opencode 仍持旧插件 / 旧 bridge 进程**，要新功能生效需用户批准执行 `opencode service restart`（不要擅自执行；用户拒绝时可等下次自行重启）。
- 有任何需要用户做的事，列清楚（填 token、重启、给 bot 发消息验证等）。
