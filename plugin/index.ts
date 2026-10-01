// opencode-bridge 插件：随 opencode 启动自动拉起 `python -m opencode_bridge`（单例守护）。
// 纯对象默认导出（与 elapsed-timer 相同）：不 import "@opencode/plugin"，避免构建期解析失败。
// 一切失败只 log 不 throw（插件在 opencode server 进程内执行，抛异常会污染加载流程）。

import { spawn, type ChildProcess } from "node:child_process"
import fs from "node:fs"
import path from "node:path"

const TAG = "[bridge-plugin]"

type LogLevel = "log" | "error"

/** 插件可配置项（ctx.options > <插件目录>/config.json > 内置默认值）。 */
export interface BridgePluginOptions {
  enabled?: boolean
  bridgeDir?: string
  python?: string
  args?: string[]
  logDir?: string
  backoffMs?: number
  lockName?: string
}

interface ResolvedConfig {
  enabled: boolean
  bridgeDir: string
  python: string
  args: string[]
  logDir: string
  backoffMs: number
  lockName: string
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
  // 空 = 未配置：只允许来自 ctx.options / <插件目录>/config.json / 环境变量 OPENCODE_BRIDGE_DIR，
  // 绝不内置任何本机绝对路径（插件必须可分发到任意机器 / 任意 clone 位置）。
  bridgeDir: "",
  python: "python",
  args: ["-m", "opencode_bridge"],
  logDir: "", // 空 = bridgeDir
  backoffMs: 300000,
  lockName: ".bridge-plugin.lock",
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
}

/** 优先级：ctx.options > <插件目录>/config.json > 环境变量 OPENCODE_BRIDGE_DIR(仅 bridgeDir) > 内置默认值。绝不抛异常。 */
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
  // 3) 环境变量：仅在前两者都没给出 bridgeDir 时兜底；拿不到就留空 → setup 判定“未配置”
  if (!cfg.bridgeDir) {
    try {
      const envDir = process.env.OPENCODE_BRIDGE_DIR
      if (typeof envDir === "string" && envDir.length > 0) cfg.bridgeDir = envDir
    } catch {
      /* ignore */
    }
  }
  if (!cfg.logDir) cfg.logDir = cfg.bridgeDir
  return cfg
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

    // bridgeDir 三级解析（options > 同目录 config.json > OPENCODE_BRIDGE_DIR）全落空：
    // 不 spawn、不抛异常，只打一条错误日志后返回 no-op cleanup。
    if (!cfg.bridgeDir) {
      log("未配置 bridgeDir，跳过启动…", "error")
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

        // 4) spawn：stdout/stderr 都重定向到同一个输出日志
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
                // 未配置 adapter 时 Python 侧会 exit 0 正常退出：这不是崩溃，不写 failedAt、不进 backoff。
                log(
                  `bridge 启动后即退出 (code=0, 存活 ${aliveMs}ms)：不是崩溃，不会进入 backoff —— ` +
                    `通常是因为尚未配置 adapter（三个 bot_token 均为空），属正常情况。` +
                    `填好 token 后下次 location 加载会自动启动；见 README「接入平台引导」与「未配置时的行为」，或在 bot 内发送 /setup`,
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
