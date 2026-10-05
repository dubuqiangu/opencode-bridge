<#
.SYNOPSIS
    opencode-bridge 一行安装脚本（Windows / PowerShell 5.1 与 7+ 通用）。

.DESCRIPTION
    用法（三选一）：
      1) 一行安装（无需下载文件）：
           iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 | iex
      2) 本地开发安装（推送前自测）：
           powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Source <本地目录> [-Force]
      3) 卸载（删除插件目录）：
           powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Uninstall

    脚本只做三件事：把仓库 clone/复制到 bridge 目录、生成配置、把插件文件安装到
    ~/.config/opencode/plugins/bridge/。它绝不会替你执行 `opencode service restart`。

.NOTES
    本文件必须以 UTF-8 with BOM 保存（PowerShell 5.1 对无 BOM 中文脚本按 GBK 解析）。
#>
param(
    [string]$Source = "https://github.com/dubuqiangu/opencode-bridge",
    [switch]$Uninstall,
    [switch]$Force
)

# 以 -File / -Command 调用时 $PSCommandPath 非空 → 可以安全地用 exit 返回退出码；
# `iwr ... | iex` 交互执行时为空 → 绝不能 exit（否则会关掉用户当前终端）。
$script:IsFileRun = -not [string]::IsNullOrEmpty($PSCommandPath)

function Write-Info([string]$Message) { Write-Host $Message }
function Write-Warn([string]$Message) { Write-Host $Message -ForegroundColor Yellow }
function Write-Bad([string]$Message) { Write-Host $Message -ForegroundColor Red }
function Write-Ok([string]$Message) { Write-Host $Message -ForegroundColor Green }

function Test-HasCommand([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-InstallPaths {
    $configHome = Join-Path $env:USERPROFILE ".config"
    return @{
        BridgeDir = Join-Path $configHome "opencode-bridge"
        PluginDir = Join-Path $configHome "opencode\plugins\bridge"
    }
}

# ---------------------------------------------------------------------------
# 卸载：只删除插件目录（bridge 目录与用户配置保留）
# ---------------------------------------------------------------------------
function Invoke-Uninstall {
    $p = Get-InstallPaths
    Write-Info "== opencode-bridge 卸载 =="
    if (Test-Path -LiteralPath $p.PluginDir) {
        Remove-Item -LiteralPath $p.PluginDir -Recurse -Force
        Write-Ok "已删除插件目录: $($p.PluginDir)"
    } else {
        Write-Info "插件目录不存在，无需卸载: $($p.PluginDir)"
    }
    Write-Info ""
    Write-Info "注: bridge 目录及其配置会保留: $($p.BridgeDir)"
    Write-Info "    如需彻底删除: Remove-Item -Recurse -Force `"$($p.BridgeDir)`""
    Write-Info '最后请执行 opencode service restart（或重开 opencode TUI）让插件停用。'
    return 0
}

# ---------------------------------------------------------------------------
# 同步源码到 bridge 目录：git clone/pull（远程源）或目录复制（本地源）
# ---------------------------------------------------------------------------
function Sync-Source([string]$SourcePath, [string]$BridgeDir) {
    $isLocal = Test-Path -LiteralPath $SourcePath -PathType Container

    if ($isLocal) {
        $srcFull = (Resolve-Path -LiteralPath $SourcePath).Path
        $dstFull = $null
        if (Test-Path -LiteralPath $BridgeDir) { $dstFull = (Resolve-Path -LiteralPath $BridgeDir).Path }
        if ($srcFull -and $dstFull -and ($srcFull -ieq $dstFull)) {
            Write-Info "[1/4] 源与目标是同一目录，跳过复制"
            return 0
        }
        Write-Info "[1/4] 从本地目录复制: $srcFull"
        New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null
        # 排除开发态/运行态：.git、__pycache__、.pytest_cache、node_modules、
        # config.json（用户配置，绝不动）、state.json、*.log、.bridge-plugin.lock
        $rcArgs = @(
            $srcFull, $BridgeDir, "/E",
            "/XD", ".git", "__pycache__", ".pytest_cache", "node_modules",
            "/XF", "config.json", "state.json", "*.log", ".bridge-plugin.lock",
            "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1"
        )
        & robocopy @rcArgs | Out-Null
        if ($LASTEXITCODE -ge 8) {
            Write-Bad "[错误] robocopy 复制失败 (exit code $LASTEXITCODE)"
            return 1
        }
        Write-Info "       复制完成（已排除 .git / __pycache__ / .pytest_cache / node_modules / config.json / state.json / *.log / .bridge-plugin.lock）"
        return 0
    }

    # 远程 git 源
    if (Test-Path -LiteralPath (Join-Path $BridgeDir ".git")) {
        Write-Info "[1/4] 更新已有 clone: git -C `"$BridgeDir`" pull --ff-only"
        # git 的 stdout 属于本函数的"返回流"，会被调用方 $code = Sync-Source ... 捕获成数组，
        # 导致 if ($code -ne 0) 对数组求值恒为真而提前返回（安装在第 1 步后静默中断）。
        # 因此只把 stdout 送往 host；stderr 保持原样直通控制台（PS 5.1 下加 2>&1 会把它
        # 包成 NativeCommandError 红字）。
        git -C $BridgeDir pull --ff-only | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "       [警告] git pull 失败（本地有改动或网络问题），继续使用现有副本"
        }
        return 0
    }

    $dirExists = (Test-Path -LiteralPath $BridgeDir) -and $null -ne (Get-ChildItem -LiteralPath $BridgeDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
    $savedCfg = $null
    if ($dirExists) {
        # 目录非空且无 .git：区分「用户真实副本」与「运行态残留」
        $hasSource = Test-Path -LiteralPath (Join-Path $BridgeDir 'opencode_bridge\__init__.py')
        if (-not $Force -and $hasSource) {
            Write-Bad "[错误] $BridgeDir 已存在且不是 git 仓库。"
            Write-Bad "       加 -Force 删除后重新 clone，或改用 -Source <本地目录>。"
            return 1
        }
        # 先把用户 config.json 存到临时文件（绝不能丢），再清理残留
        $cfgPath = Join-Path $BridgeDir 'config.json'
        if (Test-Path -LiteralPath $cfgPath) {
            $savedCfg = Join-Path ([System.IO.Path]::GetTempPath()) ("opencode-bridge-config-" + [Guid]::NewGuid().ToString('N') + '.json')
            Copy-Item -LiteralPath $cfgPath -Destination $savedCfg -Force
        }
        Write-Info "[1/4] 删除非 git 残留目录: $BridgeDir"
        Remove-Item -LiteralPath $BridgeDir -Recurse -Force
    } else {
        Write-Info "[1/4] git clone $SourcePath"
    }
    $parent = Split-Path -Parent $BridgeDir
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    git clone $SourcePath $BridgeDir | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "[错误] git clone 失败 (exit code $LASTEXITCODE)"
        if ($savedCfg) {
            # clone 失败也要把用户配置还回去
            New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null
            Copy-Item -LiteralPath $savedCfg -Destination (Join-Path $BridgeDir 'config.json') -Force
            Remove-Item -LiteralPath $savedCfg -Force
            Write-Warn "       已把原 config.json 还原到: $BridgeDir\config.json"
        }
        return 1
    }
    if ($savedCfg) {
        Copy-Item -LiteralPath $savedCfg -Destination (Join-Path $BridgeDir 'config.json') -Force
        Remove-Item -LiteralPath $savedCfg -Force
        Write-Info "[1/4] 已清理非 git 目录中的残留并重新 clone（已保留 config.json）"
    } elseif ($dirExists) {
        Write-Info "[1/4] 已清理非 git 目录中的残留并重新 clone"
    }
    return 0
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
function Invoke-Installer {
    $p = Get-InstallPaths
    $bridgeDir = $p.BridgeDir
    $pluginDir = $p.PluginDir

    if ($Uninstall) { return (Invoke-Uninstall) }

    # --- 0. 依赖检查 -------------------------------------------------------
    if (-not (Test-HasCommand "git")) {
        Write-Bad "[错误] 未检测到 git，无法获取 opencode-bridge 源码。"
        Write-Bad "       请先安装 Git for Windows: https://git-scm.com/download/win"
        Write-Bad "       安装完成后（或重开一个终端）重新运行本脚本。"
        return 1
    }
    if (-not (Test-HasCommand "python")) {
        Write-Warn "[警告] 未检测到 python。bridge 运行需要 Python 3.10+："
        Write-Warn "       https://www.python.org/downloads/ （安装时勾选 Add python.exe to PATH）"
    }

    Write-Info "== opencode-bridge 安装器 =="
    Write-Info "来源 (Source): $Source"
    Write-Info "bridge 目录:   $bridgeDir"
    Write-Info "插件目录:      $pluginDir"
    Write-Info ""

    # --- 1. 同步源码 -------------------------------------------------------
    # Sync-Source 的返回值必须是单个 int：取最后一个输出并强制转换，
    # 即便函数内再混入裸命令的 stdout 也不会让 if ($code -ne 0) 对数组求值。
    $code = Sync-Source -SourcePath $Source -BridgeDir $bridgeDir | Select-Object -Last 1
    if ([int]$code -ne 0) { return [int]$code }

    # --- 2. 生成 config.json（模板 = config.example.json）-------------------
    Write-Info "[2/4] 检查配置文件..."
    $bridgeCfg = Join-Path $bridgeDir "config.json"
    $exampleCfg = Join-Path $bridgeDir "config.example.json"
    if (-not (Test-Path -LiteralPath $bridgeCfg)) {
        if (-not (Test-Path -LiteralPath $exampleCfg)) {
            Write-Bad "[错误] 缺少配置模板: $exampleCfg"
            return 1
        }
        Copy-Item -LiteralPath $exampleCfg -Destination $bridgeCfg
        Write-Info "       已从 config.example.json 生成: $bridgeCfg"
        # 模板里各平台的 allowed_chat_ids 都是空数组，且 config_version 不是 2 ⇒ 现在
        # 仍然是旧语义「空 = 全部放行」。这里必须点破两件事：
        #   ① 现在就是开放的（照着下一步只填 bot_token 就重启 = 任何人都能驱动 agent）；
        #   ② 下一版起改为「空 = 谁都不放行」，且届时**不必手改本文件**（/pair + --pair）。
        # 只说「留空 = 全部放行」不够 —— 那样用户不知道自己接下来该做什么。
        Write-Warn "       ⚠️ 模板里的 allowed_chat_ids 是空数组，且这份 config.json 的 config_version 不是 2："
        Write-Warn "          ⇒ 现在仍是「空 = 全部放行」：任何能私聊/@ 到 bot 的人都能以你的权限"
        Write-Warn "            驱动 agent（读文件 / 改代码 / 执行命令）。"
        Write-Warn "       ⚠️ 下一版起此处改为「空 = 谁都不放行」。届时不必手改本文件："
        Write-Warn "          1) 自己生成一段随机串，填进顶层键 pairing_secret（留空 = 不提供配对）"
        Write-Warn "             例：python -c 'import secrets; print(secrets.token_hex(32))'"
        Write-Warn "          2) 在 bot 里发 /pair，拿到一条授权码"
        Write-Warn "          3) 在 bridge 目录里执行 python -m opencode_bridge --pair <码> --conversation platform:local_id"
        Write-Warn "             （它会把那个 chat 写进 allowed_chat_ids，并顺手写上 config_version: 2）"
        Write-Warn "          4) 重启桥 —— 改配置没有热重载，不重启不生效"
        Write-Warn "       现在就手填也行：把 allowed_chat_ids 填上自己的 chat id（数字不要加引号）。"
    } else {
        Write-Info "       config.json 已存在，保持不变: $bridgeCfg"
    }

    # --- 3. 安装插件文件 ---------------------------------------------------
    Write-Info "[3/4] 安装 opencode 插件 → $pluginDir"
    $pluginSrc = Join-Path $bridgeDir "plugin"
    foreach ($f in @("index.ts", "package.json")) {
        $s = Join-Path $pluginSrc $f
        if (-not (Test-Path -LiteralPath $s)) {
            Write-Bad "[错误] 源码中缺少 plugin\$f（clone/复制不完整？）: $s"
            return 1
        }
    }
    New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
    Copy-Item -Force -LiteralPath (Join-Path $pluginSrc "index.ts") -Destination $pluginDir
    Copy-Item -Force -LiteralPath (Join-Path $pluginSrc "package.json") -Destination $pluginDir
    Write-Info "       已复制 index.ts + package.json"

    # 插件 config.json：{"bridgeDir": "<clone 目录绝对路径>"}（反斜杠转义的合法 JSON）
    $pluginCfg = Join-Path $pluginDir "config.json"
    $writeCfg = $true
    if ((Test-Path -LiteralPath $pluginCfg) -and -not $Force) {
        try {
            $obj = Get-Content -LiteralPath $pluginCfg -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            $hasBridgeDir = $null -ne $obj -and ($obj.PSObject.Properties.Name -contains "bridgeDir") -and -not [string]::IsNullOrEmpty([string]$obj.bridgeDir)
        } catch {
            $hasBridgeDir = $false
        }
        if ($hasBridgeDir) {
            $writeCfg = $false
            Write-Info "       config.json 已含 bridgeDir，保持不变（-Force 可覆盖）: $pluginCfg"
        } else {
            Write-Warn "       现有 config.json 无效/缺 bridgeDir，重新生成: $pluginCfg"
        }
    }
    if ($writeCfg) {
        $json = ConvertTo-Json -InputObject @{ bridgeDir = $bridgeDir } -Compress -Depth 5
        Write-Utf8NoBom -Path $pluginCfg -Content $json
        Write-Info "       已写入 config.json → bridgeDir = $bridgeDir"
    }

    # --- 4. Next steps -----------------------------------------------------
    Write-Info ""
    Write-Ok "[4/4] 安装完成 ✔"
    Write-Info ""
    Write-Info "接下来 (Next steps):"
    Write-Info "  1. 编辑配置填 token（配置文件绝对路径）:  $bridgeCfg"
    Write-Info "     Telegram: 在 @BotFather 发 /newbot 拿 bot_token；给 @userinfobot 发一句拿纯数字 chat id"
    Write-Info "       adapters.telegram.bot_token         ← 形如 123456789:AA..."
    Write-Info "       adapters.telegram.allowed_chat_ids   ← 数组，数字不要加引号，如 [123456789]"
    Write-Info "       adapters.slack.allowed_chat_ids      ← 数组，填频道 id，如 [`"C0123456789`"]"
    Write-Info "       adapters.discord.allowed_chat_ids    ← 数组，填频道 id，如 [`"123456789012345678`"]"
    Write-Info "       ⚠️ 这三项留空 = 全部放行（见上面 [2/4] 的警告）。下一版起改为「空 = 谁都不放行」："
    Write-Info "          届时用 /pair + --pair 一步授权即可，不必手改本文件。做法见 [2/4] 的警告。"
    Write-Info "     不想等下一版的话，现在就把白名单填上（填完重启 opencode 生效）。"
    Write-Info "     Slack / Discord 的 token 获取步骤见 README「接入平台引导」；也可在 bot 内发送 /setup 查看分步引导"
    Write-Info "  2. 重启 opencode 服务让插件生效:"
    Write-Info "       opencode service restart"
    Write-Info "     （本脚本不会自动重启 opencode，避免打断你当前的会话）"
    Write-Info "  3. 连通性自检（可选，只查 /api/info，不创建会话）:"
    Write-Info "       cd `"$bridgeDir`""
    Write-Info "       python -m opencode_bridge --check"
    Write-Info ""
    Write-Info "日志:"
    Write-Info "  插件日志:        $bridgeDir\bridge-plugin.log"
    Write-Info "  bridge 输出:     $bridgeDir\bridge-output.log"
    Write-Info "  opencode 主日志: $env:USERPROFILE\.local\share\opencode\log\opencode.log   (搜 [bridge-plugin])"
    Write-Info ""
    Write-Info "卸载（删除插件目录）:"
    Write-Info "  Remove-Item -Recurse -Force `"$pluginDir`""
    Write-Info "  或重新执行本脚本并加 -Uninstall:"
    Write-Info "    powershell -NoProfile -ExecutionPolicy Bypass -Command `"`$p = Join-Path `$env:TEMP 'opencode-bridge-install.ps1'; iwr 'https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1' -OutFile `$p; & powershell -NoProfile -ExecutionPolicy Bypass -File `$p -Uninstall`""
    return 0
}

$exitCode = Invoke-Installer
if ($script:IsFileRun) {
    # -File / -Command 方式：用退出码告诉调用方成败（git 缺失等错误 → 1）
    exit $exitCode
}
# `iwr ... | iex` 交互执行：绝不 exit（否则会关掉用户当前终端），错误信息已打印完
return
