# Requires: PowerShell 5.1+ (Windows). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Force]
# One-click installer for the opencode-bridge plugin (方案 A：随 opencode 启动的 bridge 守护)。
# OpenCode auto-discovers plugins in ~/.config/opencode/plugins/<name>/, so this
# copies index.ts + package.json there and generates config.json. No opencode.json
# edits needed.
#
# 可移植性：本脚本不含任何硬编码绝对路径 —— bridgeDir 由脚本自身位置推导
#   （$PSScriptRoot = <clone>/opencode-bridge/plugin，其上级 = <clone>/opencode-bridge），
#   所以在任意 clone 目录下运行都能生成正确的 config.json。
#
# NOTE: the script never restarts opencode for you (a running session would be
# interrupted) - see the "如何启用" hint at the end.

[CmdletBinding()]
param(
    # 已有 config.json 且含 bridgeDir 时默认不覆盖；加 -Force 才按当前脚本位置重写
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
if (-not $src) { $src = Split-Path -Parent $MyInvocation.MyCommand.Path }

# bridgeDir = 本脚本所在目录(plugin\)的上级目录 = opencode-bridge\（clone 位置无关）
$repoDir = Split-Path -Parent $src

# 安装根：$HOME/.config（Windows 上 $HOME 即 %USERPROFILE%）
$homeDir = $env:USERPROFILE
if (-not $homeDir) { $homeDir = $HOME }
$configDir = Join-Path $homeDir ".config\opencode"
$dest      = Join-Path $configDir "plugins\bridge"

Write-Host "== opencode-bridge plugin installer =="
Write-Host "Source:    $src"
Write-Host "Target:    $dest"
Write-Host "bridgeDir: $repoDir"

if (-not (Test-Path (Join-Path $repoDir "opencode_bridge"))) {
    Write-Warning "在 $repoDir 下未找到 opencode_bridge 包，bridgeDir 可能不正确（请确认脚本位于 <clone>\opencode-bridge\plugin\ 下）"
}

# 1. Copy plugin files (this script / README / harness / example config stay out
#    of the plugin dir - only what opencode needs to load the plugin)
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Force (Join-Path $src "index.ts")    $dest
Copy-Item -Force (Join-Path $src "package.json") $dest
Write-Host "Copied plugin files (index.ts, package.json)."

# 2. 生成 config.json：bridgeDir 用脚本推导出的绝对路径。
#    已有 config 且含非空 bridgeDir → 不覆盖（除非 -Force）；缺失/为空/损坏 → 补写。
$cfgPath = Join-Path $dest "config.json"
$existing = $null
if (Test-Path $cfgPath) {
    try { $existing = Get-Content -Raw $cfgPath | ConvertFrom-Json } catch { $existing = $null }
}
$hasBridgeDir = $false
if ($null -ne $existing) {
    $names = @($existing.PSObject.Properties.Name)
    if ($names -contains 'bridgeDir' -and $existing.bridgeDir) { $hasBridgeDir = $true }
}

if ($hasBridgeDir -and -not $Force) {
    Write-Host "config.json already exists with bridgeDir - left untouched: $cfgPath"
    Write-Host "(use -Force to regenerate from the current script location, or edit it manually)"
} else {
    if ($null -ne $existing) {
        $cfg = $existing          # 保留用户已改的 python/args/logDir/... 字段
    } else {
        $example = Get-Content -Raw (Join-Path $src "config.example.json")
        $cfg = $example | ConvertFrom-Json
    }
    $cfg | Add-Member -NotePropertyName bridgeDir -NotePropertyValue $repoDir -Force
    # logDir 留空 => 插件默认把 bridge-plugin.log / bridge-output.log 写到 bridgeDir
    $json = $cfg | ConvertTo-Json -Depth 10
    # UTF-8 with BOM（约定写法；插件读取时会自动去 BOM）
    $utf8Bom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($cfgPath, $json, $utf8Bom)
    Write-Host "Generated config.json (bridgeDir=$repoDir)."
}

Write-Host ""
Write-Host "Done. 如何启用:"
Write-Host "  1. 本脚本不会自动重启 opencode（避免中断你当前的会话），请手动执行其一:"
Write-Host "       opencode service restart     # 重启 opencode 服务"
Write-Host "       或关闭并重新打开 opencode TUI"
Write-Host "  2. 之后每次 opencode 启动，插件会自动拉起 python -m opencode_bridge（单例，多 location 只起一次）"
Write-Host "  3. 确认 bridge 在跑: 看 $repoDir\.bridge-plugin.lock 与日志"
Write-Host "  4. 配置平台: 本脚本生成的 config.json 里**没有任何平台**（只写了 bridgeDir），"
Write-Host "     所以桥还没有可用的适配器。跑 `python -m opencode_bridge --setup <平台>` 走分步引导，"
Write-Host "     它给出的配置模板里带 allowed_chat_ids。"
Write-Host "     ⚠️ allowed_chat_ids 留空数组 = **不限制发件人**：任何能私聊或 @ 到 bot 的人"
Write-Host "        都能以你的权限驱动 agent（读文件 / 改代码 / 执行命令）。请填上自己的 chat id。"
Write-Host ""
Write-Host "日志:"
Write-Host "  插件日志:     $repoDir\bridge-plugin.log"
Write-Host "  bridge 输出:  $repoDir\bridge-output.log"
Write-Host "  opencode 主日志: $homeDir\.local\share\opencode\log\opencode.log   (搜 [bridge-plugin])"
Write-Host ""
Write-Host "如何卸载: 删除插件目录后重启 opencode:"
Write-Host "  Remove-Item -Recurse -Force `"$dest`""
