#!/usr/bin/env bash
# opencode-bridge 一行安装脚本（macOS / Linux；亦可在 Git Bash 中运行）。
#
#   curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash
#   bash install.sh --source <本地目录> [--force]     # 本地开发安装（推送前自测）
#   bash install.sh --uninstall                        # 卸载（删除插件目录）
#
# 脚本只做三件事：把仓库 clone/复制到 bridge 目录、生成配置、把插件文件安装到
# ~/.config/opencode/plugins/bridge/。它绝不会替你执行 `opencode service restart`。
set -euo pipefail

SOURCE_DEFAULT="https://github.com/dubuqiangu/opencode-bridge"
SOURCE="$SOURCE_DEFAULT"
FORCE=0
UNINSTALL=0

log() { printf '%s\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

usage() {
  cat <<'EOF'
用法: install.sh [--source <本地目录或 git 地址>] [--force] [--uninstall]
  --source <path>  源目录（本地路径）或 git clone 地址，默认克隆 GitHub 仓库
  --force          覆盖已有的插件 config.json / 删除非 git 旧目录后重新 clone
  --uninstall      删除插件目录（bridge 目录与用户配置保留）
  -h, --help       显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source)
      [ $# -ge 2 ] || { err "错误: --source 需要一个参数"; exit 1; }
      SOURCE="$2"
      shift 2
      ;;
    --source=*) SOURCE="${1#--source=}"; shift ;;
    --force|-f) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "错误: 未知参数 $1"; usage; exit 1 ;;
  esac
done

# Windows 风格路径（D:\a\b）转成 POSIX 风格（D:/a/b），方便 bash 直接 cd
case "$SOURCE" in
  [A-Za-z]:[\\/]*|[A-Za-z]:*) SOURCE="${SOURCE//\\//}" ;;
esac

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BRIDGE_DIR="$CONFIG_HOME/opencode-bridge"
PLUGIN_DIR="$HOME/.config/opencode/plugins/bridge"

# MSYS/Cygwin 下把 POSIX 路径转成 Windows 原生路径（插件在 Windows 上要用）；
# 其它平台原样返回。
win_path() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
      ;;
    *) printf '%s' "$1" ;;
  esac
}

# 反斜杠与双引号转义成合法 JSON 字符串内容
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# 卸载：只删除插件目录
# ---------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  log "== opencode-bridge 卸载 =="
  if [ -d "$PLUGIN_DIR" ]; then
    rm -rf "$PLUGIN_DIR"
    log "已删除插件目录: $PLUGIN_DIR"
  else
    log "插件目录不存在，无需卸载: $PLUGIN_DIR"
  fi
  log ""
  log "注: bridge 目录及其配置会保留: $BRIDGE_DIR"
  log "    如需彻底删除: rm -rf \"$BRIDGE_DIR\""
  log "最后请执行 opencode service restart（或重开 opencode TUI）让插件停用。"
  exit 0
fi

# ---------------------------------------------------------------------------
# 0. 依赖检查
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  err "错误: 未找到 git，无法获取 opencode-bridge 源码。"
  err "      请先安装 git:"
  err "        Debian/Ubuntu : sudo apt install git"
  err "        macOS         : brew install git"
  err "        Fedora/RHEL   : sudo dnf install git"
  err "      安装完成后重新运行本脚本。"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  err "错误: 未找到 python3，bridge 运行需要 Python 3.10+。"
  err "      Debian/Ubuntu : sudo apt install python3"
  err "      macOS         : brew install python3"
  err "      安装完成后重新运行本脚本。"
  exit 1
fi

log "== opencode-bridge 安装器 =="
log "来源 (Source): $SOURCE"
log "bridge 目录:   $BRIDGE_DIR"
log "插件目录:      $PLUGIN_DIR"
log ""

# ---------------------------------------------------------------------------
# 1. 同步源码到 bridge 目录
# ---------------------------------------------------------------------------
if [ -d "$SOURCE" ]; then
  # 本地目录：复制（排除开发态/运行态）
  SRC_ABS="$(cd "$SOURCE" && pwd)"
  CUR_ABS=""
  if [ -d "$BRIDGE_DIR" ]; then CUR_ABS="$(cd "$BRIDGE_DIR" && pwd)"; fi
  if [ -n "$CUR_ABS" ] && [ "$SRC_ABS" = "$CUR_ABS" ]; then
    log "[1/4] 源与目标是同一目录，跳过复制"
  else
    log "[1/4] 从本地目录复制: $SRC_ABS"
    mkdir -p "$BRIDGE_DIR"
    while IFS= read -r f; do
      mkdir -p "$BRIDGE_DIR/$(dirname "$f")"
      cp "$SRC_ABS/$f" "$BRIDGE_DIR/$f"
    done < <(
      cd "$SRC_ABS" && find . \
        \( -name .git -o -name __pycache__ -o -name .pytest_cache -o -name node_modules \) -prune -o \
        \( -name 'config.json' -o -name 'state.json' -o -name '*.log' -o -name '.bridge-plugin.lock' \) -prune -o \
        -type f -print | sed 's|^\./||'
    )
    log "       复制完成（已排除 .git / __pycache__ / .pytest_cache / node_modules / config.json / state.json / *.log / .bridge-plugin.lock）"
  fi
elif [ -d "$BRIDGE_DIR/.git" ]; then
  log "[1/4] 更新已有 clone: git -C \"$BRIDGE_DIR\" pull --ff-only"
  if ! git -C "$BRIDGE_DIR" pull --ff-only; then
    warn "       [警告] git pull 失败（本地有改动或网络问题），继续使用现有副本"
  fi
elif [ -d "$BRIDGE_DIR" ] && [ -n "$(ls -A "$BRIDGE_DIR" 2>/dev/null || true)" ]; then
  # 目录非空且无 .git：区分「用户真实副本」与「运行态残留」
  if [ -f "$BRIDGE_DIR/opencode_bridge/__init__.py" ] && [ "$FORCE" != 1 ]; then
    err "错误: $BRIDGE_DIR 已存在且不是 git 仓库。"
    err "      加 --force 删除后重新 clone，或改用 --source <本地目录>。"
    exit 1
  fi
  # 先把用户 config.json 存到临时文件（绝不能丢），再清理残留
  saved_cfg=""
  if [ -f "$BRIDGE_DIR/config.json" ]; then
    saved_cfg="$(mktemp)"
    cp "$BRIDGE_DIR/config.json" "$saved_cfg"
  fi
  log "[1/4] 删除非 git 残留目录: $BRIDGE_DIR"
  rm -rf "$BRIDGE_DIR"
  mkdir -p "$(dirname "$BRIDGE_DIR")"
  if ! git clone "$SOURCE" "$BRIDGE_DIR"; then
    if [ -n "$saved_cfg" ]; then
      # clone 失败也要把用户配置还回去
      mkdir -p "$BRIDGE_DIR"
      cp "$saved_cfg" "$BRIDGE_DIR/config.json"
      rm -f "$saved_cfg"
      warn "      已把原 config.json 还原到: $BRIDGE_DIR/config.json"
    fi
    err "错误: git clone 失败"
    exit 1
  fi
  if [ -n "$saved_cfg" ]; then
    cp "$saved_cfg" "$BRIDGE_DIR/config.json"
    rm -f "$saved_cfg"
    log "[1/4] 已清理非 git 目录中的残留并重新 clone（已保留 config.json）"
  else
    log "[1/4] 已清理非 git 目录中的残留并重新 clone"
  fi
else
  log "[1/4] git clone $SOURCE"
  mkdir -p "$(dirname "$BRIDGE_DIR")"
  git clone "$SOURCE" "$BRIDGE_DIR"
fi

# ---------------------------------------------------------------------------
# 2. 生成 config.json（模板 = config.example.json）
# ---------------------------------------------------------------------------
log "[2/4] 检查配置文件..."
if [ ! -f "$BRIDGE_DIR/config.json" ]; then
  if [ ! -f "$BRIDGE_DIR/config.example.json" ]; then
    err "错误: 缺少配置模板: $BRIDGE_DIR/config.example.json"
    exit 1
  fi
  cp "$BRIDGE_DIR/config.example.json" "$BRIDGE_DIR/config.json"
  log "       已从 config.example.json 生成: $BRIDGE_DIR/config.json"
  # 模板里各平台的 allowed_chat_ids 都是空数组，且 config_version 不是 2 ⇒ 现在
  # 仍然是旧语义「空 = 全部放行」。这里必须点破两件事：
  #   ① 现在就是开放的（照着下一步只填 bot_token 就重启 = 任何人都能驱动 agent）；
  #   ② 下一版起改为「空 = 谁都不放行」，且届时**不必手改本文件**（/pair + --pair）。
  # 只说「留空 = 全部放行」不够 —— 那样用户不知道自己接下来该做什么。
  warn "       ⚠️ 模板里的 allowed_chat_ids 是空数组，且这份 config.json 的 config_version 不是 2："
  warn "          ⇒ 现在仍是「空 = 全部放行」：任何能私聊/@ 到 bot 的人都能以你的权限"
  warn "            驱动 agent（读文件 / 改代码 / 执行命令）。"
  warn "       ⚠️ 下一版起此处改为「空 = 谁都不放行」。届时不必手改本文件："
  warn "          1) 自己生成一段随机串，填进顶层键 pairing_secret（留空 = 不提供配对）"
  warn "             例：python3 -c 'import secrets; print(secrets.token_hex(32))'"
  warn "          2) 在 bot 里发 /pair，拿到一条授权码"
  warn "          3) 在 bridge 目录里执行 python3 -m opencode_bridge --pair <码> --conversation platform:local_id"
  warn "             （它会把那个 chat 写进 allowed_chat_ids，并顺手写上 config_version: 2）"
  warn "          4) 重启桥 —— 改配置没有热重载，不重启不生效"
  warn "       现在就手填也行：把 allowed_chat_ids 填上自己的 chat id（数字不要加引号）。"
else
  log "       config.json 已存在，保持不变: $BRIDGE_DIR/config.json"
fi

# ---------------------------------------------------------------------------
# 3. 安装插件文件
# ---------------------------------------------------------------------------
log "[3/4] 安装 opencode 插件 → $PLUGIN_DIR"
for f in index.ts package.json; do
  if [ ! -f "$BRIDGE_DIR/plugin/$f" ]; then
    err "错误: 源码中缺少 plugin/$f（clone/复制不完整？）"
    exit 1
  fi
done
mkdir -p "$PLUGIN_DIR"
cp "$BRIDGE_DIR/plugin/index.ts" "$PLUGIN_DIR/index.ts"
cp "$BRIDGE_DIR/plugin/package.json" "$PLUGIN_DIR/package.json"
log "       已复制 index.ts + package.json"

# 插件 config.json：{"bridgeDir": "<clone 目录绝对路径>"}
BRIDGE_DIR_W="$(win_path "$BRIDGE_DIR")"
plugin_cfg="$PLUGIN_DIR/config.json"
write_cfg=1
if [ -f "$plugin_cfg" ] && [ "$FORCE" != 1 ]; then
  if grep -q '"bridgeDir"[[:space:]]*:' "$plugin_cfg" 2>/dev/null; then
    write_cfg=0
    log "       config.json 已含 bridgeDir，保持不变（--force 可覆盖）: $plugin_cfg"
  else
    warn "       现有 config.json 缺 bridgeDir，重新生成: $plugin_cfg"
  fi
fi
if [ "$write_cfg" = 1 ]; then
  printf '{"bridgeDir":"%s"}\n' "$(json_escape "$BRIDGE_DIR_W")" > "$plugin_cfg"
  log "       已写入 config.json → bridgeDir = $BRIDGE_DIR_W"
fi

# ---------------------------------------------------------------------------
# 4. Next steps
# ---------------------------------------------------------------------------
log ""
log "[4/4] 安装完成 ✔"
log ""
log "接下来 (Next steps):"
log "  1. 编辑配置填 token（配置文件）: \${XDG_CONFIG_HOME:-\$HOME/.config}/opencode-bridge/config.json"
log "     Telegram: 在 @BotFather 发 /newbot 拿 bot_token；给 @userinfobot 发一句拿纯数字 chat id"
log "       adapters.telegram.bot_token         ← 形如 123456789:AA..."
log "       adapters.telegram.allowed_chat_ids   ← 数组，数字不要加引号，如 [123456789]"
log "       adapters.slack.allowed_chat_ids      ← 数组，填频道 id，如 [\"C0123456789\"]"
log "       adapters.discord.allowed_chat_ids    ← 数组，填频道 id，如 [\"123456789012345678\"]"
log "       ⚠️ 这三项留空 = 全部放行（见上面 [2/4] 的警告）。下一版起改为「空 = 谁都不放行」："
log "          届时用 /pair + --pair 一步授权即可，不必手改本文件。做法见 [2/4] 的警告。"
log "     不想等下一版的话，现在就把白名单填上（填完重启 opencode 生效）。"
log "     Slack / Discord 的 token 获取步骤见 README「接入平台引导」；也可在 bot 内发送 /setup 查看分步引导"
log "  2. 重启 opencode 服务让插件生效:"
log "       opencode service restart"
log "     （本脚本不会自动重启 opencode，避免打断你当前的会话）"
log "  3. 连通性自检（可选，只查 /api/info，不创建会话）:"
log "       cd \"$BRIDGE_DIR_W\" && python3 -m opencode_bridge --check"
log ""
log "日志:"
log "  插件日志:        $BRIDGE_DIR_W/bridge-plugin.log"
log "  bridge 输出:     $BRIDGE_DIR_W/bridge-output.log"
log "  opencode 主日志: ~/.local/share/opencode/log/opencode.log   (搜 [bridge-plugin])"
log ""
log "卸载（删除插件目录）:"
log "  bash install.sh --uninstall"
log "  或: curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh | bash -s -- --uninstall"
