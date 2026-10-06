// opencode-bridge 插件：随 opencode 启动自动拉起 `python -m opencode_bridge`（单例守护）。
// 纯对象默认导出（与 elapsed-timer 相同）：不 import "@opencode/plugin"，避免构建期解析失败。
// 一切失败只 log 不 throw（插件在 opencode server 进程内执行，抛异常会污染加载流程）。

import { spawn, type ChildProcess } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

const TAG = "[bridge-plugin]"

type LogLevel = "log" | "error"

/** 插件可配置项（ctx.options > <插件目录>/config.json > 环境变量 OPENCODE_BRIDGE_DIR >
 *  默认自举稳定目录 > 内置默认值）。 */
export interface BridgePluginOptions {
  enabled?: boolean
  bridgeDir?: string
  python?: string
  args?: string[]
  logDir?: string
  backoffMs?: number
  lockName?: string
  /** `--setup` 引导用的子进程命令前缀（默认 `-m opencode_bridge --setup`）；仅供测试覆盖。 */
  setupCommand?: string[]
}

interface ResolvedConfig {
  enabled: boolean
  bridgeDir: string
  python: string
  args: string[]
  logDir: string
  backoffMs: number
  lockName: string
  setupCommand: string[]
}

/** <bridgeDir>\<lockName> 的内容。 */
interface LockInfo {
  pid: number
  servicePid: number
  startedAt: number
  failedAt?: number
}

const DEFAULTS: ResolvedConfig = {
  enabled: true,
  // 空 = 未配置：只允许来自 ctx.options / <插件目录>/config.json / 环境变量 OPENCODE_BRIDGE_DIR /
  // 运行时推导的稳定目录（见 bootstrapBridgeDir），绝不内置任何本机绝对路径
  // （插件必须可分发到任意机器 / 任意 clone 位置）。
  bridgeDir: "",
  python: "python",
  args: ["-m", "opencode_bridge"],
  logDir: "", // 空 = bridgeDir
  backoffMs: 300000,
  lockName: ".bridge-plugin.lock",
  setupCommand: ["-m", "opencode_bridge", "--setup"],
}

/** 子进程存活时间低于该值的非零退出视为“快速失败”，记录 failedAt 触发 backoff。 */
const FAST_FAIL_MS = 60000

// ---------------------------------------------------------------------------
// 日志：console（带 [bridge-plugin] 前缀，会进 opencode.log）+ <logDir>/bridge-plugin.log
// ---------------------------------------------------------------------------

function emit(logDir: string | null, level: LogLevel, msg: string): void {
  const line = `${TAG} ${msg}`
  try {
    if (level === "error") console.error(line)
    else console.log(line)
  } catch {
    /* console 不可用时忽略 */
  }
  if (!logDir) return
  try {
    fs.mkdirSync(logDir, { recursive: true })
    fs.appendFileSync(path.join(logDir, "bridge-plugin.log"), `${new Date().toISOString()} ${line}\n`)
  } catch {
    /* 写文件失败只忽略，绝不上抛 */
  }
}

function warn(msg: string): void {
  emit(null, "error", msg) // 配置阶段：只进 console，logDir 尚未确定
}

// ---------------------------------------------------------------------------
// 凭据脱敏：写入日志**之前**过一道
// ---------------------------------------------------------------------------

/** 遮蔽标记。与 Python 侧 `redaction.py` 的 `_MASK_TEMPLATE` 同形，于是子进程
 *  stderr 落进日志后与它自己过滤过的行看起来是同一种东西，排障时不用先分辨这行
 *  是谁写的。 */
function redactionMarker(label: string): string {
  return `[REDACTED:${label}]`
}

/** 凭据形状 → 标签，逐条对应 AGENTS.md §2.1 的表格（即 `redaction.py` 的
 *  `_CREDENTIAL_RULES` 前六条）。正则里不出现任何凭据实例。
 *
 *  为什么插件侧还需要一道：Python 那道 `install_redaction_filter()` 只挂在**它自己
 *  的 logging handler** 上，子进程里绕过 logging 直接写 stderr 的路径（traceback、
 *  `print(..., file=sys.stderr)`）不保证被覆盖 —— 而 stderr 现在会被本文件落进日志。
 *
 *  ⚠️ Slack 一条刻意写成 `x(?:ox[bp]|app)-` 而非 `xox[bp]-`/`xapp-`：形状表不该匹配
 *  它自己（AGENTS.md §2.4）。本文件任何地方都**不得**出现凭据的完整形状字面量 ——
 *  推送保护会拦掉整个 push。 */
const CREDENTIAL_SHAPES: ReadonlyArray<readonly [RegExp, string]> = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/g, "private-key"],
  [/\d{8,10}:[A-Za-z0-9_-]{35}/g, "telegram-bot-token"],
  [/x(?:ox[bp]|app)-[A-Za-z0-9-]{10,}/g, "slack-bot-token"],
  [/gh[pousr]_[A-Za-z0-9]{20,}/g, "github-token"],
  [/sk-[A-Za-z0-9]{20,}/g, "openai-key"],
  [/AKIA[0-9A-Z]{16}/g, "aws-access-key-id"],
  [/(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/=-]{16,}/g, "bearer-token"],
]

/** `token=xxx` 这类赋值：键名不是秘密，保留；值整段遮蔽。名字前用否定环视而不是
 *  `\b`（`_` 是单词字符，`\b` 在 `bot_token` 里反而匹配不到），与 `redaction.py` 的
 *  `secret-assignment-unquoted` 同判据。值字符类排除 `[` / `]` ⇒ 已经遮蔽过的
 *  `[REDACTED:...]` 不会被二次遮蔽，标签也就不会退化成更泛的那个。 */
const SECRET_ASSIGNMENT_SHAPE =
  /(?<![A-Za-z0-9])((?:token|secret|password|passwd|api[_-]?key|app[_-]?secret|access[_-]?token|client[_-]?secret)["']?\s*[=:]\s*)([^\s\[\]&"',;)\]}<>]{16,})/gi

/** 把文本里的凭据形状换成遮蔽标记。幂等：遮蔽标记自身不含任何形状，同一段文本过
 *  两道与过一道结果相同。
 *
 *  只用 `replace`、不用 `test`：带 `g` 的正则在 `test` 里会推进 `lastIndex`，而上面
 *  这些是模块级共享常量，复用同一个对象做第二次匹配会从半路开始。 */
function redactCredentials(text: string): string {
  let scrubbed = text
  for (const [shape, label] of CREDENTIAL_SHAPES) {
    scrubbed = scrubbed.replace(shape, redactionMarker(label))
  }
  return scrubbed.replace(
    SECRET_ASSIGNMENT_SHAPE,
    (_matched, keyName: string) => keyName + redactionMarker("secret-assignment"),
  )
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

/** 插件自身所在目录（index.ts 同目录）。两种部署形态都正确：
 *  - 安装形态：~/.config/opencode/plugins/bridge/（config.json 由安装器写在这里）
 *  - 本地开发：直接用 clone 目录里的 opencode-bridge/plugin/index.ts
 * 取不到时退回 cwd（仅兜底，不影响“不抛异常”承诺）。 */
function pluginDir(): string {
  try {
    const meta = import.meta as unknown as { dir?: string; dirname?: string }
    if (typeof meta.dir === "string" && meta.dir) return meta.dir // bun
    if (typeof meta.dirname === "string" && meta.dirname) return meta.dirname // node >= 20.11 / deno
  } catch {
    /* fallthrough */
  }
  return process.cwd()
}

/** 插件所在「包根目录」= index.ts 的上一级。
 *  - `opencode plugin add github:<owner>/<repo>` 形态：包根 = npm/git cache 里的包目录，
 *    里面带着 opencode_bridge/ 源码与 config.example.json（见 package.json 的 files 字段）。
 *  - 脚本安装形态：上一级 = plugins/ 目录，没有这些文件 → 自举跳过拷贝，仅用稳定目录。 */
export function packageRoot(): string {
  return path.resolve(pluginDir(), "..")
}

/** 稳定 bridge 目录（与 install.ps1 / install.sh 的 BridgeDir 推导保持一致）：
 *  - Windows: `%USERPROFILE%\.config\opencode-bridge`
 *  - 其他:    `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge`
 *  env / platform 可注入，便于 harness 跨平台断言。 */
export function deriveStableDir(
  env: Record<string, string | undefined> = process.env,
  platform: string = process.platform,
): string {
  if (platform === "win32") {
    const home = env.USERPROFILE || env.HOME || os.homedir()
    return path.join(home, ".config", "opencode-bridge")
  }
  const home = env.HOME || os.homedir()
  const base = env.XDG_CONFIG_HOME && env.XDG_CONFIG_HOME.length > 0 ? env.XDG_CONFIG_HOME : path.join(home, ".config")
  return path.join(base, "opencode-bridge")
}

const PY_PKG = "opencode_bridge"
const PY_INIT = path.join(PY_PKG, "__init__.py")
const EXAMPLE_CFG = "config.example.json"

/** <root>/opencode_bridge/__init__.py 是否存在。 */
function hasPythonSource(root: string): boolean {
  try {
    return fs.existsSync(path.join(root, PY_INIT))
  } catch {
    return false
  }
}

function isGitRepo(dir: string): boolean {
  try {
    return fs.existsSync(path.join(dir, ".git"))
  } catch {
    return false
  }
}

/** 递归拷贝目录（跳过 __pycache__ / *.pyc），已存在的文件会被覆盖。 */
function copyTree(src: string, dest: string): void {
  fs.mkdirSync(dest, { recursive: true })
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name.endsWith(".pyc")) continue
    const s = path.join(src, entry.name)
    const d = path.join(dest, entry.name)
    if (entry.isDirectory()) copyTree(s, d)
    else fs.copyFileSync(s, d)
  }
}

export interface MaterializeResult {
  stable: string
  created: boolean // 本次新建了稳定目录
  copiedSource: boolean // 首次铺入 opencode_bridge/
  refreshedSource: boolean // 已有源码（非 git clone）时刷新了 .py
  wroteConfig: boolean // 由 config.example.json 生成了 config.json
  gitCloneUntouched: boolean // 稳定目录是 git clone → 完全没碰
  hasSource: boolean // 结束时稳定目录里有可运行的 Python 源码
}

/** 把包内 Python 运行时铺到稳定 bridge 目录（幂等）。
 *  A. 包内有源 + stable 缺源 → 铺源码 + 由 example 生成 config.json（仅当目标不存在）
 *  B. stable 已有源 → 是 git clone 就**完全不碰**（脚本安装，更新走安装器/git pull）；
 *     非 git 则刷新 .py（这就是 `plugin update` 刷新 Python 侧的机制），config.json 永不覆盖
 *  C. 包内无源（脚本安装形态）→ 不拷，直接把 stable 当 bridgeDir
 *  硬规则：绝不覆盖 config.json / state.json / 日志 / 锁，绝不删除任何东西，
 *  绝不写 stable 以外（pkgRoot 只读）；fs 异常只记 log 不抛。 */
export function ensureMaterialized(stable: string, pkgRoot: string): MaterializeResult {
  const res: MaterializeResult = {
    stable,
    created: false,
    copiedSource: false,
    refreshedSource: false,
    wroteConfig: false,
    gitCloneUntouched: false,
    hasSource: false,
  }
  const pkgHasPy = hasPythonSource(pkgRoot)
  const dstPy = path.join(stable, PY_PKG)
  const stableHasPy = hasPythonSource(stable)

  // B-git：脚本安装的 git clone 优先判定 → 一个字节都不改
  if (stableHasPy && isGitRepo(stable)) {
    res.gitCloneUntouched = true
    res.hasSource = true
    warn(`稳定目录是 git clone (${stable})，自举不修改任何文件（更新请用安装器或 git pull）`)
    return res
  }

  if (pkgHasPy && !stableHasPy) {
    try {
      copyTree(path.join(pkgRoot, PY_PKG), dstPy)
      res.copiedSource = true
      emit(null, "log", `自举：已铺设 Python 运行时 → ${dstPy}`)
    } catch (e) {
      warn(`自举铺设 Python 运行时失败: ${String(e)}`)
    }
  } else if (pkgHasPy && stableHasPy) {
    // B-nongit：plugin update 刷新 Python 侧（config.json 等用户数据不在 opencode_bridge/ 内，不受影响）
    try {
      copyTree(path.join(pkgRoot, PY_PKG), dstPy)
      res.refreshedSource = true
      emit(null, "log", `自举：已刷新 Python 运行时 → ${dstPy}`)
    } catch (e) {
      warn(`自举刷新 Python 运行时失败: ${String(e)}`)
    }
  }

  // config.json：只在目标缺失时由 example 生成（用户 token 永不被覆盖）
  const dstCfg = path.join(stable, "config.json")
  try {
    if (!fs.existsSync(dstCfg)) {
      const src = path.join(pkgRoot, EXAMPLE_CFG)
      if (fs.existsSync(src)) {
        fs.copyFileSync(src, dstCfg)
        res.wroteConfig = true
        emit(null, "log", `自举：已生成 config.json（需填写 bot_token）→ ${dstCfg}`)
        // 模板里各平台的 allowed_chat_ids 都是空数组（example 里的 config_version
        // 是 0，不是 2）⇒ 现在仍然是旧语义「空 = 全部放行」。不说这一句，就等于默默
        // 递了一份「谁都能驱动 agent」的配置 —— 与 a2a.py 拒绝静默开洞同一取舍：宁可让
        // 用户当场看见这句提醒，也不要事后才发现。**只陈述事实与出路，不改默认**
        // （默认收紧是产品决定，自举不该替用户做），也不替用户生成 pairing_secret。
        //
        // 两件事都必须点破：① 现在就是开放的；② 下一版起改为「空 = 谁都不放行」，
        // 且届时**不必手改本文件**（/pair + --pair）。只说「留空 = 全部放行」不够 ——
        // 那样用户不知道自己接下来该做什么。
        warn(
          `自举生成的 ${dstCfg} 里 allowed_chat_ids 是空数组、config_version 也不是 2：` +
            `现在仍是「空 = 全部放行」= 不限制发件人 —— 任何能私聊/@ 到 bot 的人都能以你的权限` +
            `驱动 agent（读文件 / 改代码 / 执行命令）。` +
            `⚠️ 下一版起此处改为「空 = 谁都不放行」，届时不必手改本文件：` +
            `生成随机串填进顶层键 pairing_secret（留空 = 不提供配对）→ 在 bot 里发 /pair 拿码 → ` +
            `在本机执行 python -m opencode_bridge --pair <码> --conversation platform:local_id （会顺手写上 config_version: 2）→ ` +
            `重启桥（改配置没有热重载）。` +
            `现在就手填也行：请在 ${dstCfg} 里填上自己的 chat id；不确定填什么就先跑 ` +
            `"python -m opencode_bridge --setup <平台>" 看分步引导。`,
        )
      }
    }
  } catch (e) {
    warn(`自举生成 config.json 失败: ${String(e)}`)
  }

  res.hasSource = hasPythonSource(stable)
  return res
}

/** 解析链第 4 级：默认稳定目录自举。仅当前三级（options / config.json / 环境变量）全落空、
 *  且 enabled=true 时执行；失败不抛、也不强行设置 bridgeDir（交给既有“未配置”分支）。 */
function bootstrapBridgeDir(cfg: ResolvedConfig): void {
  try {
    const stable = deriveStableDir()
    const r = ensureMaterialized(stable, packageRoot())
    if (r.gitCloneUntouched || r.copiedSource || r.refreshedSource || r.created) {
      cfg.bridgeDir = stable
      emit(null, "log", `使用自举稳定目录作为 bridgeDir: ${stable}`)
    } else if (fs.existsSync(stable)) {
      // C：目录已存在（如脚本安装但没有包内源）→ 直接用
      cfg.bridgeDir = stable
      emit(null, "log", `使用已存在的稳定目录作为 bridgeDir: ${stable}`)
    } else {
      warn(`自举未能建立稳定目录: ${stable}`)
    }
  } catch (e) {
    warn(`自举稳定目录失败: ${String(e)}`)
  }
}

/** 去掉 UTF-8 BOM：安装器按约定用 UTF-8 with BOM 写 config.json，
 *  而 JSON.parse 不接受以 U+FEFF 开头的字符串。 */
function stripBom(s: string): string {
  return s.charCodeAt(0) === 0xfeff ? s.slice(1) : s
}

function mergeInto(cfg: ResolvedConfig, src: unknown, origin: string): void {
  if (!src || typeof src !== "object") return
  const s = src as Record<string, unknown>

  if ("enabled" in s) {
    if (typeof s.enabled === "boolean") cfg.enabled = s.enabled
    else warn(`${origin}.enabled 类型无效(需 boolean)，使用默认值 ${cfg.enabled}`)
  }
  if ("bridgeDir" in s) {
    if (typeof s.bridgeDir === "string" && s.bridgeDir.length > 0) cfg.bridgeDir = s.bridgeDir
    else warn(`${origin}.bridgeDir 类型无效(需非空字符串)，保留 ${cfg.bridgeDir}`)
  }
  if ("python" in s) {
    if (typeof s.python === "string" && s.python.length > 0) cfg.python = s.python
    else warn(`${origin}.python 类型无效(需非空字符串)，保留 ${cfg.python}`)
  }
  if ("args" in s) {
    if (Array.isArray(s.args) && s.args.every((x) => typeof x === "string")) cfg.args = s.args as string[]
    else warn(`${origin}.args 类型无效(需字符串数组)，保留 [${cfg.args.join(", ")}]`)
  }
  if ("logDir" in s) {
    if (typeof s.logDir === "string") cfg.logDir = s.logDir // 允许空串 = 回退 bridgeDir
    else warn(`${origin}.logDir 类型无效(需字符串)，保留 ${cfg.logDir || cfg.bridgeDir}`)
  }
  if ("backoffMs" in s) {
    if (typeof s.backoffMs === "number" && Number.isFinite(s.backoffMs) && s.backoffMs >= 0)
      cfg.backoffMs = s.backoffMs
    else warn(`${origin}.backoffMs 类型无效(需 >= 0 的数字)，保留 ${cfg.backoffMs}`)
  }
  if ("lockName" in s) {
    if (typeof s.lockName === "string" && s.lockName.length > 0) cfg.lockName = s.lockName
    else warn(`${origin}.lockName 类型无效(需非空字符串)，保留 ${cfg.lockName}`)
  }
  if ("setupCommand" in s) {
    if (Array.isArray(s.setupCommand) && s.setupCommand.every((x) => typeof x === "string"))
      cfg.setupCommand = s.setupCommand as string[]
    else warn(`${origin}.setupCommand 类型无效(需字符串数组)，保留默认值`)
  }
}

/** 优先级：ctx.options > <插件目录>/config.json > 环境变量 OPENCODE_BRIDGE_DIR(仅 bridgeDir) >
 *  默认自举稳定目录（仅 enabled 时，惰性副作用）> 内置默认值。绝不抛异常。 */
function resolveConfig(options: unknown): ResolvedConfig {
  const cfg: ResolvedConfig = { ...DEFAULTS, args: [...DEFAULTS.args] }
  // 2) 与 index.ts 同目录的 config.json（安装器写入，bridgeDir 的主来源）
  try {
    const p = path.join(pluginDir(), "config.json")
    if (fs.existsSync(p)) {
      try {
        mergeInto(cfg, JSON.parse(stripBom(fs.readFileSync(p, "utf8"))), "config.json")
      } catch (e) {
        warn(`config.json 解析失败(${String(e)})，该文件已忽略，使用默认值`)
      }
    }
  } catch (e) {
    warn(`读取 config.json 失败(${String(e)})，使用默认值`)
  }
  // 1) ctx.options（优先级最高）
  try {
    mergeInto(cfg, options, "options")
  } catch (e) {
    warn(`ctx.options 解析失败(${String(e)})，该来源已忽略`)
  }
  // 3) 环境变量：仅在前两者都没给出 bridgeDir 时兜底
  if (!cfg.bridgeDir) {
    try {
      const envDir = process.env.OPENCODE_BRIDGE_DIR
      if (typeof envDir === "string" && envDir.length > 0) cfg.bridgeDir = envDir
    } catch {
      /* ignore */
    }
  }
  // 4) 默认自举稳定目录：仅前三级全落空且 enabled 时执行（惰性副作用，enabled=false 零副作用）
  if (!cfg.bridgeDir && cfg.enabled) bootstrapBridgeDir(cfg)
  if (!cfg.logDir) cfg.logDir = cfg.bridgeDir
  return cfg
}

// ---------------------------------------------------------------------------
// opencode 内接入引导：bridge_setup 工具 + /bridge-setup 命令
// 文案不在 TS 侧复制 —— 一律 spawn `python -m opencode_bridge --setup`，
// 与 bot 内 /setup、CLI --setup 共用 core.py 的同一份冻结文案。
// ---------------------------------------------------------------------------

const SETUP_PLATFORMS = ["telegram", "slack", "discord"] as const

/** 跑一次 `python -m opencode_bridge --setup [platform] [--json]`，拿 stdout，
 *  同时收集 stderr（逐行脱敏后进插件日志）。
 *
 *  ⛔ stderr **只进日志与返回值**，绝不 `console.log`：插件的 stdout 是宿主消费的
 *  协议通道，子进程的告警混进去会污染它。下面的 `log(..., "error")` 走
 *  console.error，那条不是协议通道（见 emit）。 */
function runSetup(
  cfg: ResolvedConfig,
  platform: string | null,
  asJson: boolean,
  log: (msg: string, level?: LogLevel) => void,
): Promise<string> {
  return new Promise((resolve) => {
    const args = [...cfg.setupCommand]
    if (asJson) args.push("--json")
    if (platform) args.push(platform)
    let out = ""
    let errRaw = ""
    // chunk 边界不保证落在 \n 上 ⇒ 未成行的尾巴留到下一块，close 时再补一次。
    let stderrTail = ""
    /** 一行 stderr 进插件日志（写入前脱敏）。纯空白行跳过：Python 的 logging 基本
     *  不产空白行，留着只会给日志添噪。 */
    const logStderrLine = (line: string): void => {
      const scrubbed = redactCredentials(line)
      if (scrubbed.trim().length > 0) log(`setup 子进程 stderr: ${scrubbed}`, "error")
    }
    let c: ChildProcess
    try {
      // 第三管道由 "ignore" 改 "pipe"：以前 stderr 被直接丢掉，而引导命令的失败原因
      // （例如 telegram 凭据校验失败）**只**写在 stderr 上 —— 丢掉它，这条路就只剩
      // 一句「无输出」。这是本条修复的全部起因。
      c = spawn(cfg.python, args, { cwd: cfg.bridgeDir, stdio: ["ignore", "pipe", "pipe"], windowsHide: true })
    } catch (e) {
      resolve(`[bridge-plugin] 无法启动引导命令(${cfg.python} ${args.join(" ")}): ${String(e)}`)
      return
    }
    /** 补上最后那一块未成行的 stderr（子进程可能不带末尾换行就退出）。 */
    const flushStderrTail = (): void => {
      if (stderrTail.trim().length > 0) logStderrLine(stderrTail)
      stderrTail = ""
    }
    /** 超时/非零退出时把 stderr 附在返回值后面：挂住或失败的引导命令，原因往往已经
     *  写在 stderr 上了。已脱敏。 */
    const stderrExcerpt = (): string => {
      const scrubbed = redactCredentials(errRaw).trim()
      return scrubbed.length > 0 ? `\n[bridge-plugin] 它的 stderr：\n${scrubbed}` : ""
    }
    const timer = setTimeout(() => {
      try {
        c.kill()
      } catch {
        /* ignore */
      }
      resolve(out.trim() || `[bridge-plugin] 引导命令超时无输出${stderrExcerpt()}`)
    }, 20000)
    c.stdout?.on("data", (chunk: Buffer) => {
      out += chunk.toString("utf8")
    })
    c.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8")
      errRaw += text
      // **逐行**写，不能把整块丢给 log：emit() 自己补一个换行，整块里已有的 \n 会让
      // 两行日志粘成一行（末尾那个 \n 则变成多余空行）。
      const parts = (stderrTail + text).split(/\r?\n/)
      stderrTail = parts.pop() ?? "" // 末段可能只有半行，留给下一块
      for (const part of parts) logStderrLine(part)
    })
    c.on("error", (err) => {
      clearTimeout(timer)
      resolve(`[bridge-plugin] 引导命令失败：${err.message}`)
    })
    // 用 'close' 而不是 'exit'：Node 保证 close 在子进程结束**且** stdio 流都关闭之后
    // 才发，所以 errRaw 一定是完整的（否则会缺尾部那一半，凭证校验失败那行就断在这里）。
    c.on("close", (code) => {
      clearTimeout(timer)
      flushStderrTail()
      const text = out.trim()
      // 「无输出」原来只查 stdout、stderr 又被丢掉，于是异常只表现为「没输出」，用户会
      // 以为命令压根没跑。现在 stderr 也读了，所以：① 文案点明是 **stdout** 空
      // （不点明就仍然误导 —— 子进程明明有输出）；② 附上 stderr，那才是真正的原因。
      if (!text) resolve(`[bridge-plugin] 引导命令无 stdout 输出 (code=${String(code)})${stderrExcerpt()}`)
      else if (code === 0) resolve(text) // 正常路径：stderr 已在日志里，不往引导正文里混
      else resolve(`${text}\n[bridge-plugin] 引导命令返回 code=${String(code)}${stderrExcerpt()}`)
    })
  })
}

/** 工具 + 命令注册。ctx 缺 tool/command 能力时静默跳过，绝不抛。 */
async function registerSetupSurfaces(
  ctx: unknown,
  cfg: ResolvedConfig,
  log: (msg: string, level?: LogLevel) => void,
): Promise<void> {
  const c = ctx as
    | {
        tool?: {
          transform: (
            cb: (editor: {
              add: (t: {
                name: string
                description: string
                input: { type: "string"; properties: Record<string, unknown>; required?: string[] }
                execute: (input: Record<string, unknown>) => Promise<{ content: string }>
              }) => void
            }) => void,
          ) => unknown
        }
        command?: {
          transform: (
            cb: (editor: {
              add: (d: {
                name: string
                description?: string
                execute: (input: { sessionID: string; prompt: unknown; delivery: string }) => Promise<void>
              }) => void
            }) => void,
          ) => unknown
        }
        session?: { prompt: (input: { sessionID: string; text: string; delivery?: string }) => unknown }
      }
    | undefined
  if (!c) return

  const norm = (v: unknown): string | null => {
    const t = String(v ?? "").trim().toLowerCase()
    if (!t) return null
    const map: Record<string, string> = { "1": "telegram", "2": "slack", "3": "discord" }
    const key = map[t] ?? t
    return (SETUP_PLATFORMS as readonly string[]).includes(key) ? key : null
  }

  // ---- 工具：模型可在会话里直接问「怎么配 Telegram」 ----
  if (typeof c.tool?.transform === "function") {
    try {
      c.tool.transform((editor) => {
        if (typeof editor?.add !== "function") return
        editor.add({
          name: "bridge_setup",
          description:
            "opencode-bridge（Telegram/Slack/Discord 消息桥）的接入引导与配置状态。" +
            "回答用户「怎么接入 Telegram / Slack / Discord」「bot_token 填哪里」" +
            "「桥接配置好了吗」这类问题时调用。不带参数返回平台菜单与配置路径；" +
            "带 platform 返回该平台分步引导。",
          input: {
            type: "string",
            properties: {
              platform: {
                type: "string",
                enum: [...SETUP_PLATFORMS],
                description: "可选；只查某个平台。省略则返回平台菜单与配置文件路径。",
              },
            },
          },
          execute: async (input) => {
            const key = norm((input as { platform?: unknown }).platform)
            const platform = key ?? null
            const text = await runSetup(cfg, platform, false, log)
            const status = await runSetup(cfg, null, true, log)
            const head = platform
              ? `opencode-bridge 接入引导（${platform}）`
              : "opencode-bridge 接入引导（平台菜单）"
            return {
              content: [`# ${head}`, "", "## 当前配置状态", "", "```", status, "```", "", text].join("\n"),
            }
          },
        })
      })
      log("已注册工具 bridge_setup（opencode 内可直接问接入引导）")
    } catch (e) {
      warn(`注册 bridge_setup 工具失败: ${String(e)}`)
    }
  }

  // ---- 命令：/bridge-setup [平台] 出现在 opencode 斜杠命令面板 ----
  if (typeof c.command?.transform === "function" && typeof c.session?.prompt === "function") {
    try {
      c.command.transform((editor) => {
        if (typeof editor?.add !== "function") return
        editor.add({
          name: "bridge-setup",
          description: "opencode-bridge 接入引导（Telegram / Slack / Discord），可选参数：平台",
          execute: async (input) => {
            const raw = String((input as { prompt?: unknown }).prompt ?? "")
            const m = raw.match(/bridge-setup\s+(\S+)/i)
            const key = m ? norm(m[1]) : null
            const text = await runSetup(cfg, key, false, log)
            await c.session!.prompt({
              sessionID: input.sessionID,
              text:
                `用户请求 opencode-bridge 接入引导。请**原样**把下面内容展示给用户，` +
                `不要改写、不要补充其它内容：\n\n${text}`,
              delivery: input.delivery === "queue" ? "queue" : "steer",
            })
          },
        })
      })
      log("已注册命令 /bridge-setup（opencode 斜杠命令面板）")
    } catch (e) {
      warn(`注册 /bridge-setup 命令失败: ${String(e)}`)
    }
  }
}

// ---------------------------------------------------------------------------
// 锁文件
// ---------------------------------------------------------------------------

function readLock(lockPath: string): { lock: LockInfo | null; corrupt: boolean } {
  let raw: string
  try {
    raw = stripBom(fs.readFileSync(lockPath, "utf8"))
  } catch {
    return { lock: null, corrupt: false } // 不存在(或不可读)
  }
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === "object" && typeof obj.pid === "number" && typeof obj.servicePid === "number") {
      return { lock: obj as LockInfo, corrupt: false }
    }
  } catch {
    /* fallthrough: 非法 JSON */
  }
  try {
    fs.unlinkSync(lockPath) // 损坏的锁直接清掉，让本次 setup 正常走 spawn
  } catch {
    /* ignore */
  }
  return { lock: null, corrupt: true }
}

/** Windows 上 process.kill(pid, 0) 可能误判：任何异常一律当作“不存活”。 */
function isAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function deleteLockIfPid(lockPath: string, pid: number | null): void {
  if (pid == null || !lockPath) return
  try {
    const { lock } = readLock(lockPath)
    if (lock && lock.pid === pid) fs.unlinkSync(lockPath)
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// 插件本体
// ---------------------------------------------------------------------------

export default {
  id: "opencode-bridge",

  /**
   * 插件按 location 加载：同一 opencode server 进程内可能执行多次。
   * 通过 <bridgeDir>\.bridge-plugin.lock 做单例（同进程采纳 / 跨进程杀旧重启）。
   * 返回 cleanup：仅当子进程由本实例 spawn 时才 kill + 删锁。
   */
  setup(ctx?: { options?: unknown }): () => void {
    const noop = () => {}

    let logDir: string | null = null
    const log = (msg: string, level: LogLevel = "log") => emit(logDir, level, msg)

    // 1) 配置（失败只 log）
    let cfg: ResolvedConfig
    try {
      cfg = resolveConfig(ctx && (ctx as { options?: unknown }).options)
      logDir = cfg.logDir
    } catch (e) {
      emit(null, "error", `配置解析失败，本次跳过: ${String(e)}`)
      return noop
    }

    if (!cfg.enabled) {
      log("enabled=false，不启动 bridge")
      return noop
    }

    // opencode 内接入引导面（工具 + 命令）：不依赖 bridge 是否起得来，注册失败也只 log
    void registerSetupSurfaces(ctx, cfg, log)

    // bridgeDir 四级解析（options > 同目录 config.json > OPENCODE_BRIDGE_DIR > 自举稳定目录）
    // 全落空：不 spawn、不抛异常，只打一条错误日志后返回 no-op cleanup。
    if (!cfg.bridgeDir) {
      log(
        "未配置 bridgeDir，跳过启动…（可用任一方式恢复：`opencode plugin add github:dubuqiangu/opencode-bridge` " +
          "一行安装自举，或在插件目录写 config.json 指定 bridgeDir，或设置环境变量 OPENCODE_BRIDGE_DIR）",
        "error",
      )
      return noop
    }

    let lockPath = ""
    try {
      if (!fs.existsSync(cfg.bridgeDir)) {
        log(`bridgeDir 不存在: ${cfg.bridgeDir}，跳过启动`, "error")
        return noop
      }
      lockPath = path.join(cfg.bridgeDir, cfg.lockName)
    } catch (e) {
      log(`初始化失败: ${String(e)}`, "error")
      return noop
    }

    let child: ChildProcess | null = null
    let ownsChild = false // 只有自己 spawn 的才 kill / 删锁
    let adoptedPid: number | null = null // 采纳（不管理）的 bridge pid
    let outFd: number | null = null
    let spawnedPid: number | null = null // 本实例 spawn 的 pid
    let lostRace = false // 写锁 EEXIST 重试后：对方是新声明者 → 采纳而非杀掉

    try {
      // 2~4) 判定 + backoff + spawn + 写锁（循环用于处理 wx EEXIST 并发竞态）
      for (let attempt = 0; attempt < 3 && !child && adoptedPid == null; attempt++) {
        const { lock, corrupt } = readLock(lockPath)
        if (corrupt) log(`锁文件损坏已清除: ${lockPath}`, "error")

        if (lock) {
          if (isAlive(lock.pid)) {
            if (lock.servicePid === process.pid) {
              // 同进程其它 location 已启动 → 采纳
              adoptedPid = lock.pid
              log(`bridge 已由本进程的其它 location 启动 (pid=${lock.pid})，采纳，不重复 spawn`)
              break
            }
            if (lostRace) {
              // 刚刚写锁输给了并发 location（对方刚声明且进程存活）→ 采纳，不杀
              adoptedPid = lock.pid
              log(`锁被并发 location 抢占 (pid=${lock.pid}, servicePid=${lock.servicePid})，采纳对方进程`)
              break
            }
            // 上一个 opencode 进程残留的 bridge → 杀掉后重新 spawn
            try {
              process.kill(lock.pid)
              log(`发现残留 bridge pid=${lock.pid} (servicePid=${lock.servicePid})，已终止，准备重新启动`)
            } catch (e) {
              log(`终止残留 bridge pid=${lock.pid} 失败: ${String(e)}`, "error")
            }
            // 确认旧进程已死就顺手清锁，避免下面写锁必然 EEXIST 多走一轮 spawn/kill；
            // 若旧进程没死成则保留锁，交给 EEXIST 重试分支重新判定。
            if (!isAlive(lock.pid)) deleteLockIfPid(lockPath, lock.pid)
          } else {
            // pid 已死：先看 backoff（failedAt 需要保留到 backoff 结束，因此此时不删锁）
            if (
              typeof lock.failedAt === "number" &&
              cfg.backoffMs > 0 &&
              Date.now() - lock.failedAt < cfg.backoffMs
            ) {
              log(
                `上一次启动快速失败于 ${new Date(lock.failedAt).toISOString()}，` +
                  `backoff ${cfg.backoffMs}ms 内不再启动 (剩余 ${lock.failedAt + cfg.backoffMs - Date.now()}ms)`,
              )
              return noop
            }
            try {
              fs.unlinkSync(lockPath)
            } catch {
              /* ignore */
            }
            log(`清理失效锁 (pid=${lock.pid} 已不存在)`)
          }
        }

        // 3) backoff（锁里带 failedAt 且仍在静默期 → 不 spawn）
        //    注：上面 pid 已死分支已处理；这里兜底“锁存在但 pid 存活且带 failedAt”的边角情况。
        if (
          lock &&
          typeof lock.failedAt === "number" &&
          cfg.backoffMs > 0 &&
          Date.now() - lock.failedAt < cfg.backoffMs &&
          isAlive(lock.pid)
        ) {
          log(`bridge 近期失败过 (failedAt=${lock.failedAt})，backoff 内跳过启动`)
          return noop
        }

        // 4) spawn：stdout/stderr 都重定向到同一个输出日志（**已经是 pipe 到 fd，不是
        //    ignore** —— 与 runSetup 那处不同，这里 stderr 一直是被记下来的）
        // ⚠️ 已知残留（明说，不假装解决）：fd 直写绕过了本文件的 redactCredentials，
        //    所以这条路的脱敏**只有** Python 侧那道 install_redaction_filter()。
        //    不改成「pipe + 逐行脱敏」是刻意的：fd 直写不占本进程事件循环、也不会因
        //    插件侧异常丢输出，而这正是零容错关键路径最想要的两点。代价就是这条路上
        //    子进程绕过 logging 直写 stderr 时可能漏出凭据片段 —— 修它要先解决
        //    「插件挂了就丢日志」，那是另一个改动。
        const outPath = path.join(cfg.logDir, "bridge-output.log")
        let fd: number
        try {
          fs.mkdirSync(cfg.logDir, { recursive: true })
          fd = fs.openSync(outPath, "a")
        } catch (e) {
          log(`无法打开输出日志 ${outPath}: ${String(e)}`, "error")
          return noop
        }

        let c: ChildProcess
        try {
          c = spawn(cfg.python, cfg.args, {
            cwd: cfg.bridgeDir,
            stdio: ["ignore", fd, fd],
            windowsHide: true,
            detached: false,
          })
        } catch (e) {
          log(`spawn 失败: ${String(e)}`, "error")
          try {
            fs.closeSync(fd)
          } catch {
            /* ignore */
          }
          return noop
        }

        if (typeof c.pid !== "number") {
          // 立即失败（例如可执行文件找不到），error 事件会另行记录
          try {
            fs.closeSync(fd)
          } catch {
            /* ignore */
          }
          c.on("error", (err) => log(`bridge 子进程启动失败: ${err.message}`, "error"))
          log(`spawn 未产生 pid（${cfg.python} ${cfg.args.join(" ")}），放弃`, "error")
          return noop
        }

        // 写锁：O_EXCL 语义，拿不到(EEXIST)说明并发 location 已写 → 重读重新判定
        const startedAt = Date.now()
        try {
          const lfd = fs.openSync(lockPath, "wx")
          try {
            fs.writeSync(lfd, JSON.stringify({ pid: c.pid, servicePid: process.pid, startedAt }))
          } finally {
            fs.closeSync(lfd)
          }
        } catch (e) {
          const code = (e as NodeJS.ErrnoException)?.code
          try {
            fs.closeSync(fd)
          } catch {
            /* ignore */
          }
          if (code === "EEXIST") {
            try {
              c.kill()
            } catch {
              /* ignore */
            }
            lostRace = true
            log(`写锁竞争失败(EEXIST)，重读锁重新判定`, "error")
            continue
          }
          try {
            c.kill()
          } catch {
            /* ignore */
          }
          log(`写锁失败(${String(e)})，已终止刚启动的子进程避免失控`, "error")
          return noop
        }

        child = c
        ownsChild = true
        spawnedPid = c.pid
        outFd = fd

        const cmdLine = `${cfg.python} ${cfg.args.join(" ")}`
        c.on("spawn", () => log(`spawned pid=${c.pid} cwd=${cfg.bridgeDir} cmd=${cmdLine}`))
        c.on("error", (err) => log(`bridge 子进程错误 pid=${c.pid}: ${err.message}`, "error"))
        c.on("exit", (code, signal) => {
          const aliveMs = Date.now() - startedAt
          try {
            const { lock } = readLock(lockPath)
            if (!lock || lock.pid !== c.pid) {
              log(`bridge pid=${c.pid} 退出 (code=${code} signal=${signal})，锁已不属于它，不处理`)
              return
            }
            if (code !== null && code !== 0 && aliveMs < FAST_FAIL_MS) {
              lock.failedAt = Date.now()
              try {
                fs.writeFileSync(lockPath, JSON.stringify(lock))
              } catch {
                /* ignore */
              }
              log(
                `bridge pid=${c.pid} 快速非零退出 code=${code} (存活 ${aliveMs}ms)，记录 failedAt 进入 backoff`,
                "error",
              )
            } else {
              deleteLockIfPid(lockPath, c.pid)
              if (code === 0 && aliveMs < FAST_FAIL_MS) {
                // 「快速 exit 0」有**两个**原因，原来只说了其中一个，会把人引到错误的排查方向：
                //   1. 未配置 adapter（三个 bot_token 均为空）
                //   2. **已有另一个 bridge 实例在跑** —— Python 侧的单实例锁会干净退出
                //      （见 opencode_bridge/instance_lock.py）。常见于有人手动
                //      `python -m opencode_bridge` 调试，或上一实例还没退干净。
                // 之前只报 1：用户会照着去填 token，而真正的原因是 2，怎么填都没用。
                log(
                  `bridge 启动后即退出 (code=0, 存活 ${aliveMs}ms)：不是崩溃，不会进入 backoff。` +
                    `两种可能：① 尚未配置 adapter（bot_token 为空）—— 填好 token 后下次 location 加载会自动启动，` +
                    `见 README「接入平台引导」或在 bot 内发送 /setup；` +
                    `② 已有另一个 bridge 实例在运行（单实例锁拦下了本次启动）—— 先确认没有手动启动的 ` +
                    "`python -m opencode_bridge` 残留，必要时停掉它再让插件拉起。" +
                    ` 两种情况都会在 bridge-output.log 末尾留下原因（NO_ADAPTER_MESSAGE / 已有另一个 bridge 实例在运行）。`,
                )
              }
              log(`bridge pid=${c.pid} 退出 (code=${code} signal=${signal}, 存活 ${aliveMs}ms)，锁已删除`)
            }
          } catch (e) {
            log(`exit 处理异常: ${String(e)}`, "error")
          }
        })
      }
    } catch (e) {
      log(`setup 异常（已降级为空操作）: ${String(e)}`, "error")
      if (outFd != null) {
        try {
          fs.closeSync(outFd)
        } catch {
          /* ignore */
        }
        outFd = null
      }
      return noop
    }

    if (child && ownsChild && spawnedPid != null) {
      log(`bridge 启动完成 pid=${spawnedPid} (servicePid=${process.pid})`)
    } else if (adoptedPid != null) {
      // 采纳：不管理其生命周期
    } else if (!child) {
      // 未启动（disabled / backoff / 目录缺失 / spawn 失败）
    }

    // 5) cleanup：仅当 ownsChild 才 kill；锁仅当 ownsChild 且 pid 匹配才删
    return () => {
      try {
        if (ownsChild && child) {
          const pid = spawnedPid
          try {
            child.kill()
            log(`cleanup: 已终止本实例 spawn 的 bridge pid=${pid}`)
          } catch (e) {
            log(`cleanup: 终止 bridge pid=${pid} 失败: ${String(e)}`, "error")
          }
        } else if (adoptedPid != null) {
          log(`cleanup: 采纳的 bridge pid=${adoptedPid} 不由本实例管理，保持运行`)
        }
        if (outFd != null) {
          try {
            fs.closeSync(outFd)
          } catch {
            /* ignore */
          }
          outFd = null
        }
        if (ownsChild) deleteLockIfPid(lockPath, spawnedPid)
      } catch (e) {
        try {
          console.error(`${TAG} cleanup 异常: ${String(e)}`)
        } catch {
          /* ignore */
        }
      }
    }
  },
}
