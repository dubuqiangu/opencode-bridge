// harness.ts — opencode-bridge 插件独立验证脚本（用 bun 跑，不触碰 opencode 服务）。
//   cd opencode-bridge\plugin
//   bun harness.ts
// 覆盖 15 个场景（10 个生命周期 + 4 个可移植性/bridgeDir 解析 + 1 个 exit-0 语义），全部断言通过才
// 打印 PASS 15/15 并退出 0；任一失败退出码 1。结束时杀掉所有子进程、删临时目录与锁、
// 还原被它改动的 <插件目录>/config.json 与环境变量 OPENCODE_BRIDGE_DIR。
// 注意：全程不运行真实的 python -m opencode_bridge，也不触碰 opencode 服务。

import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import plugin from "./index.ts"
import type { BridgePluginOptions } from "./index.ts"

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

function isAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function readLock(p: string): Record<string, unknown> | null {
  try {
    const o = JSON.parse(fs.readFileSync(p, "utf8"))
    return o && typeof o === "object" ? o : null
  } catch {
    return null
  }
}

function markerCount(p: string): number {
  try {
    return fs
      .readFileSync(p, "utf8")
      .split("\n")
      .filter((s) => s.length > 0).length
  } catch {
    return 0
  }
}

async function waitFor(cond: () => boolean | Promise<boolean>, timeoutMs: number, label: string): Promise<void> {
  const end = Date.now() + timeoutMs
  for (;;) {
    if (await cond()) return
    if (Date.now() > end) throw new Error(`超时(${timeoutMs}ms): ${label}`)
    await sleep(50)
  }
}

// ---------------------------------------------------------------------------
// 运行器预检：优先 node，退回 bun 自身（-e eval）
// ---------------------------------------------------------------------------

function pickRunner(): { runner: string; evalArgs: (code: string) => string[] } {
  const n = spawnSync("node", ["--version"], { encoding: "utf8" })
  if (n.status === 0) {
    return { runner: "node", evalArgs: (code: string) => ["-e", code] }
  }
  const b = spawnSync(process.execPath, ["-e", "process.exit(0)"], { encoding: "utf8" })
  if (b.status === 0) {
    return { runner: process.execPath, evalArgs: (code: string) => ["-e", code] }
  }
  throw new Error("node 与 bun -e 均不可用，无法运行 harness")
}

const { runner: RUNNER, evalArgs } = pickRunner()

// ---------------------------------------------------------------------------
// 假子进程：启动时往 marker 追加一行（marker 行数 = spawn 次数），然后长期存活
// ---------------------------------------------------------------------------

function liveCode(marker: string): string {
  return `require('fs').appendFileSync(${JSON.stringify(marker)},'s\\n');setInterval(()=>{},1000)`
}

function fastFailCode(marker: string): string {
  return `require('fs').appendFileSync(${JSON.stringify(marker)},'s\\n');process.exit(3)`
}

/** 快速 exit(0)：模拟「未配置 adapter」时 Python 侧的正常退出（不应进 backoff）。 */
function fastOkExitCode(marker: string): string {
  return `require('fs').appendFileSync(${JSON.stringify(marker)},'s\\n');process.exit(0)`
}

// ---------------------------------------------------------------------------
// 临时目录与全局清理
// ---------------------------------------------------------------------------

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-plugin-harness-"))
const logsDir = path.join(tmpRoot, "logs")
const cleanups: Array<() => void> = []
const extraChildren: ChildProcess[] = []
const seenLockPids: string[] = [] // 出现过的锁路径，最终兜底清理

// ---------------------------------------------------------------------------
// 可移植性测试所需的“插件自身状态”：与 index.ts 同目录的 config.json 与环境变量。
// 测试会临时改写它们，teardown 必须原样还原（绝不破坏开发者/安装器已有的配置）。
// ---------------------------------------------------------------------------

const HERE: string =
  (import.meta as unknown as { dir?: string }).dir ||
  (import.meta as unknown as { dirname?: string }).dirname ||
  process.cwd()

const indexTsPath = path.join(HERE, "index.ts")
const pluginCfgPath = path.join(HERE, "config.json")
const originalPluginCfg: string | null = fs.existsSync(pluginCfgPath)
  ? fs.readFileSync(pluginCfgPath, "utf8")
  : null
const originalEnvBridgeDir: string | undefined = process.env.OPENCODE_BRIDGE_DIR

/** 清掉测试写入的插件目录 config.json（还原成“原本有没有”的状态）。 */
function resetPluginCfg(): void {
  try {
    if (originalPluginCfg === null) fs.rmSync(pluginCfgPath, { force: true })
    else fs.writeFileSync(pluginCfgPath, originalPluginCfg)
  } catch {
    /* ignore */
  }
}

/** 临时写一份“安装器风格”的 config.json（含指定 bridgeDir）。 */
function writePluginCfg(bridgeDir: string, marker: string): void {
  fs.writeFileSync(
    pluginCfgPath,
    JSON.stringify(
      {
        enabled: true,
        bridgeDir,
        logDir: logsDir,
        python: RUNNER,
        args: evalArgs(liveCode(marker)),
        backoffMs: 0,
      },
      null,
      2,
    ),
  )
}

/** 不带 bridgeDir 的 options（用于验证 config.json / 环境变量两条来源）。 */
function optsWithoutBridgeDir(marker: string, over: Partial<BridgePluginOptions> = {}) {
  return {
    options: {
      logDir: logsDir,
      python: RUNNER,
      args: evalArgs(liveCode(marker)),
      backoffMs: 0,
      ...over,
    } satisfies BridgePluginOptions,
  }
}

function dirFor(name: string): string {
  const d = path.join(tmpRoot, name)
  fs.mkdirSync(d, { recursive: true })
  return d
}

function opts(bridgeDir: string, marker: string, over: Partial<BridgePluginOptions> = {}) {
  return {
    options: {
      bridgeDir,
      logDir: logsDir,
      python: RUNNER,
      args: evalArgs(liveCode(marker)),
      backoffMs: 0,
      ...over,
    } satisfies BridgePluginOptions,
  }
}

function lockPathOf(dir: string): string {
  return path.join(dir, ".bridge-plugin.lock")
}

// ---------------------------------------------------------------------------
// 测试收集器
// ---------------------------------------------------------------------------

interface Result {
  n: number
  name: string
  pass: boolean
  err?: string
}
const results: Result[] = []

async function test(n: number, name: string, fn: () => Promise<void> | void): Promise<void> {
  try {
    await fn()
    results.push({ n, name, pass: true })
    console.log(`  PASS #${n} ${name}`)
  } catch (e) {
    results.push({ n, name, pass: false, err: String(e) })
    console.error(`  FAIL #${n} ${name} -> ${String(e)}`)
  }
}

// ---------------------------------------------------------------------------
// 10 个场景
// ---------------------------------------------------------------------------

async function run() {
  // ---- 可移植性测试前置：清掉插件目录 config.json 与 OPENCODE_BRIDGE_DIR（teardown 还原）----
  delete process.env.OPENCODE_BRIDGE_DIR
  try {
    fs.rmSync(pluginCfgPath, { force: true }) // 原内容已在 originalPluginCfg 中备份
  } catch {
    /* ignore */
  }

  // ---- 1~4 共用一个 bridgeDir：spawn / 采纳 / cleanup 语义 ----
  const d1 = dirFor("t1")
  const m1 = path.join(tmpRoot, "t1.marker")
  const l1 = lockPathOf(d1)
  let pid1 = -1
  let cleanup1: (() => void) | null = null
  let cleanup2: (() => void) | null = null

  await test(1, "首次 setup() 生成子进程与锁 (servicePid===process.pid)", async () => {
    cleanup1 = plugin.setup(opts(d1, m1))
    assert(typeof cleanup1 === "function", "setup 应返回 cleanup 函数")
    cleanups.push(cleanup1!)
    const lock = readLock(l1)
    assert(lock, "锁文件应已生成")
    assert(lock!.servicePid === process.pid, `servicePid 应为 ${process.pid}，实际 ${String(lock!.servicePid)}`)
    assert(typeof lock!.pid === "number" && lock!.pid > 0, "锁里应有子进程 pid")
    pid1 = lock!.pid as number
    seenLockPids.push(l1)
    await waitFor(() => markerCount(m1) >= 1, 5000, "子进程写入 marker (真正跑起来)")
  })

  await test(2, "同进程第二次 setup() 采纳，不再 spawn", async () => {
    const before = fs.readFileSync(l1, "utf8")
    cleanup2 = plugin.setup(opts(d1, m1))
    assert(typeof cleanup2 === "function", "setup 应返回 cleanup 函数")
    cleanups.push(cleanup2!)
    await sleep(500) // 给“若误 spawn”留出写 marker 的时间
    assert(markerCount(m1) === 1, `spawn 次数应为 1，实际 ${markerCount(m1)}`)
    const after = fs.readFileSync(l1, "utf8")
    assert(before === after, "锁文件内容不应变化")
    assert(readLock(l1)!.pid === pid1, "锁 pid 不应变化")
  })

  await test(3, "第二个(采纳者) cleanup 不 kill、不删锁", async () => {
    cleanup2!()
    await sleep(300)
    assert(isAlive(pid1), "采纳者的 cleanup 不应杀死子进程")
    assert(fs.existsSync(l1), "采纳者的 cleanup 不应删除锁")
    assert(markerCount(m1) === 1, "不应产生新 spawn")
  })

  await test(4, "第一个(拥有者) cleanup 杀死子进程并删除锁", async () => {
    cleanup1!()
    await waitFor(() => !isAlive(pid1), 5000, "子进程被终止")
    assert(!fs.existsSync(l1), "锁应被删除")
  })

  // ---- 5: 残留锁（servicePid 不同、pid 存活）→ 杀旧 + spawn 新 ----
  await test(5, "外来 servicePid 且 pid 存活的锁 → 杀掉旧进程并重新 spawn", async () => {
    const d5 = dirFor("t5")
    const m5 = path.join(tmpRoot, "t5.marker")
    const l5 = lockPathOf(d5)
    const stale = spawn(RUNNER, evalArgs(liveCode(m5)), { stdio: "ignore", windowsHide: true })
    extraChildren.push(stale)
    assert(typeof stale.pid === "number", "预检: 无法启动 stale 进程")
    await waitFor(() => markerCount(m5) >= 1, 5000, "stale 进程写入 marker")
    const stalePid = stale.pid!
    fs.writeFileSync(
      l5,
      JSON.stringify({ pid: stalePid, servicePid: process.pid + 1, startedAt: Date.now() }),
    )
    seenLockPids.push(l5)

    const cleanup = plugin.setup(opts(d5, m5))
    cleanups.push(cleanup)

    await waitFor(() => !isAlive(stalePid), 5000, "旧 pid 被杀掉")
    const lock = readLock(l5)
    assert(lock, "应生成新锁")
    assert(lock!.pid !== stalePid, "锁应指向新 pid")
    assert(lock!.servicePid === process.pid, "新锁 servicePid 应为本进程")
    await waitFor(() => markerCount(m5) >= 2, 5000, "新子进程已 spawn")
    cleanup()
    const newPid = lock!.pid as number
    await waitFor(() => !isAlive(newPid), 5000, "新子进程被 cleanup 终止")
    assert(!fs.existsSync(l5), "cleanup 后锁应被删除")
  })

  // ---- 6: pid 已死的锁 → 清理并 spawn ----
  await test(6, "pid 已死的锁 → 清理失效锁并重新 spawn", async () => {
    const DEAD = 4294967295 // 0xFFFFFFFF：Windows 上必定是无效 pid
    assert(!isAlive(DEAD), "预检失败: 4294967295 竟然存活，换一个哨兵值")
    const d6 = dirFor("t6")
    const m6 = path.join(tmpRoot, "t6.marker")
    const l6 = lockPathOf(d6)
    fs.writeFileSync(l6, JSON.stringify({ pid: DEAD, servicePid: process.pid + 2, startedAt: 0 }))
    seenLockPids.push(l6)

    const cleanup = plugin.setup(opts(d6, m6))
    cleanups.push(cleanup)

    await waitFor(() => markerCount(m6) >= 1, 5000, "新子进程已 spawn")
    const lock = readLock(l6)
    assert(lock, "锁应被重写")
    assert(lock!.pid !== DEAD, "失效 pid 应被替换")
    assert(lock!.servicePid === process.pid, "新锁 servicePid 应为本进程")
    cleanup()
    await waitFor(() => !isAlive(lock!.pid as number), 5000, "新子进程被终止")
  })

  // ---- 7: enabled=false → 不 spawn、不抛 ----
  await test(7, "enabled=false → 不 spawn、不抛异常", () => {
    const d7 = dirFor("t7")
    const m7 = path.join(tmpRoot, "t7.marker")
    const cleanup = plugin.setup({
      options: { enabled: false, bridgeDir: d7, logDir: logsDir, python: RUNNER, args: evalArgs(liveCode(m7)) },
    })
    assert(typeof cleanup === "function", "setup 应返回 cleanup 函数")
    cleanup()
    assert(!fs.existsSync(lockPathOf(d7)), "不应生成锁")
    assert(markerCount(m7) === 0, "不应 spawn 子进程")
  })

  // ---- 8: 损坏的锁 → 不抛，正常 spawn ----
  await test(8, "损坏的锁文件(非法 JSON) → 不抛，正常 spawn", async () => {
    const d8 = dirFor("t8")
    const m8 = path.join(tmpRoot, "t8.marker")
    const l8 = lockPathOf(d8)
    fs.writeFileSync(l8, "this is not json {{{")
    seenLockPids.push(l8)

    const cleanup = plugin.setup(opts(d8, m8))
    cleanups.push(cleanup)
    await waitFor(() => markerCount(m8) >= 1, 5000, "子进程已 spawn")
    const lock = readLock(l8)
    assert(lock && typeof lock.pid === "number", "损坏锁应被替换为合法锁")
    assert(lock!.servicePid === process.pid, "新锁 servicePid 应为本进程")
    cleanup()
    await waitFor(() => !isAlive(lock!.pid as number), 5000, "子进程被终止")
    assert(!fs.existsSync(l8), "cleanup 后锁应被删除")
  })

  // ---- 9: bridgeDir 不存在 → 不抛，log 后返回，不 spawn ----
  await test(9, "bridgeDir 不存在 → 不抛、不 spawn", () => {
    const missing = path.join(tmpRoot, "t9-does-not-exist")
    const m9 = path.join(tmpRoot, "t9.marker")
    const cleanup = plugin.setup(opts(missing, m9))
    assert(typeof cleanup === "function", "setup 应返回 cleanup 函数")
    cleanup()
    assert(!fs.existsSync(missing), "不应创建 bridgeDir")
    assert(markerCount(m9) === 0, "不应 spawn 子进程")
  })

  // ---- 10: 快速非零退出 → failedAt + backoff 不 respawn ----
  await test(10, "快速非零退出 → 记录 failedAt，backoff 内不 respawn", async () => {
    const d10 = dirFor("t10")
    const m10 = path.join(tmpRoot, "t10.marker")
    const l10 = lockPathOf(d10)
    const o = {
      options: {
        bridgeDir: d10,
        logDir: logsDir,
        python: RUNNER,
        args: evalArgs(fastFailCode(m10)),
        backoffMs: 60000,
      } satisfies BridgePluginOptions,
    }
    const cleanup1 = plugin.setup(o)
    cleanups.push(cleanup1)
    seenLockPids.push(l10)

    await waitFor(() => markerCount(m10) >= 1, 5000, "快速退出进程已启动")
    await waitFor(() => {
      const l = readLock(l10)
      return !!l && typeof l.failedAt === "number"
    }, 5000, "failedAt 被写入锁")
    const lock1 = readLock(l10)!

    const cleanup2 = plugin.setup(o)
    cleanups.push(cleanup2)
    await sleep(600) // 给“若误 respawn”留出写 marker 的时间
    assert(markerCount(m10) === 1, `backoff 内 spawn 次数应为 1，实际 ${markerCount(m10)}`)
    const lock2 = readLock(l10)
    assert(lock2 && lock2.pid === lock1.pid, "锁应保持原样")
    assert(typeof lock2.failedAt === "number", "failedAt 应仍在锁里")
    cleanup2()
    cleanup1() // 拥有者 cleanup：子进程已退出，仅删锁
    assert(!fs.existsSync(l10), "最终锁应被删除")
  })

  // =========================================================================
  // 可移植性 / bridgeDir 三级解析（11~14）
  // =========================================================================

  // ---- 11: 与 index.ts 同目录的 config.json 提供 bridgeDir → 正确 spawn（主路径）----
  await test(11, "同目录 config.json 提供 bridgeDir → 正确 spawn (主路径)", async () => {
    const d11 = dirFor("t11")
    const m11 = path.join(tmpRoot, "t11.marker")
    const l11 = lockPathOf(d11)
    try {
      writePluginCfg(d11, m11) // 安装器风格：全部配置都来自 config.json

      const cleanup = plugin.setup({}) // ctx.options 不提供任何配置
      cleanups.push(cleanup)
      seenLockPids.push(l11)

      await waitFor(() => markerCount(m11) >= 1, 5000, "config.json 驱动的子进程写入 marker")
      const lock = readLock(l11)
      assert(lock, "锁应生成在 config.json 指定的 bridgeDir 下")
      assert(lock!.servicePid === process.pid, `servicePid 应为 ${process.pid}`)
      const pid = lock!.pid as number
      cleanup()
      await waitFor(() => !isAlive(pid), 5000, "子进程被 cleanup 终止")
      assert(!fs.existsSync(l11), "cleanup 后锁应被删除")
    } finally {
      resetPluginCfg()
    }
  })

  // ---- 12: ctx.options.bridgeDir 优先于同目录 config.json ----
  await test(12, "ctx.options.bridgeDir 优先于同目录 config.json (两者都给时用 options)", async () => {
    const dCfg = dirFor("t12-from-config")
    const dOpt = dirFor("t12-from-options")
    const m12 = path.join(tmpRoot, "t12.marker")
    const lCfg = lockPathOf(dCfg)
    const lOpt = lockPathOf(dOpt)
    try {
      writePluginCfg(dCfg, m12) // config.json 说 bridgeDir = dCfg
      const cleanup = plugin.setup(opts(dOpt, m12)) // options 说 bridgeDir = dOpt
      cleanups.push(cleanup)
      seenLockPids.push(lCfg, lOpt)

      await waitFor(() => markerCount(m12) >= 1, 5000, "子进程写入 marker")
      assert(fs.existsSync(lOpt), "锁应生成在 options.bridgeDir 指定的目录")
      assert(!fs.existsSync(lCfg), "不应使用 config.json 里的 bridgeDir")
      const lock = readLock(lOpt)
      assert(lock, "options 目录下应有锁")
      const pid = lock!.pid as number
      cleanup()
      await waitFor(() => !isAlive(pid), 5000, "子进程被 cleanup 终止")
      assert(!fs.existsSync(lOpt), "cleanup 后锁应被删除")
    } finally {
      resetPluginCfg()
    }
  })

  // ---- 13: 环境变量 OPENCODE_BRIDGE_DIR 在没有前两者时生效 ----
  await test(13, "环境变量 OPENCODE_BRIDGE_DIR 在没有 options/config.json 时生效", async () => {
    resetPluginCfg()
    assert(!fs.existsSync(pluginCfgPath), "预检失败: 插件目录不应存在 config.json")
    delete process.env.OPENCODE_BRIDGE_DIR
    const d13 = dirFor("t13")
    const m13 = path.join(tmpRoot, "t13.marker")
    const l13 = lockPathOf(d13)
    process.env.OPENCODE_BRIDGE_DIR = d13
    try {
      const cleanup = plugin.setup(optsWithoutBridgeDir(m13))
      cleanups.push(cleanup)
      seenLockPids.push(l13)

      await waitFor(() => markerCount(m13) >= 1, 5000, "环境变量驱动的子进程写入 marker")
      const lock = readLock(l13)
      assert(lock, "锁应生成在 OPENCODE_BRIDGE_DIR 指定的目录")
      assert(lock!.servicePid === process.pid, "servicePid 应为本进程")
      const pid = lock!.pid as number
      cleanup()
      await waitFor(() => !isAlive(pid), 5000, "子进程被 cleanup 终止")
      assert(!fs.existsSync(l13), "cleanup 后锁应被删除")
    } finally {
      delete process.env.OPENCODE_BRIDGE_DIR
      resetPluginCfg()
    }
  })

  // ---- 14: 三者皆缺 → 不 spawn、不抛、no-op cleanup（外加源码静态断言）----
  await test(14, "三者皆缺 → 不 spawn、不抛、no-op cleanup；源码无硬编码盘符路径", async () => {
    resetPluginCfg()
    delete process.env.OPENCODE_BRIDGE_DIR
    assert(!fs.existsSync(pluginCfgPath), "预检失败: 插件目录不应存在 config.json")
    const m14 = path.join(tmpRoot, "t14.marker")

    // 捕获 console.error，断言插件按约定打印“未配置 bridgeDir”而不是抛异常
    const captured: string[] = []
    const origErr = console.error
    console.error = (...a: unknown[]) => {
      captured.push(a.map(String).join(" "))
    }
    let cleanup: (() => void) | null = null
    try {
      cleanup = plugin.setup(optsWithoutBridgeDir(m14)) // options / config.json / 环境变量都没有 bridgeDir
    } finally {
      console.error = origErr
    }
    assert(typeof cleanup === "function", "setup 应返回 cleanup 函数（不抛异常）")
    cleanup!()
    await sleep(600) // 给“若误 spawn”留出写 marker 的时间
    assert(markerCount(m14) === 0, "未配置 bridgeDir 时不应 spawn 子进程")
    assert(
      captured.some((s) => s.includes("[bridge-plugin]") && s.includes("未配置 bridgeDir")),
      `应打印“[bridge-plugin] 未配置 bridgeDir，跳过启动…”，实际捕获: ${JSON.stringify(captured)}`,
    )

    // 源码静态断言：index.ts 不得残留任何本机硬编码盘符路径（D:\ 或 D:/）
    const src = fs.readFileSync(indexTsPath, "utf8")
    assert(!/D:[\\/]/.test(src), "index.ts 不得包含硬编码盘符路径 D:\\ 或 D:/")
  })

  // ---- 15: 快速 exit(0)（未配置 adapter 的正常退出）→ 不记 failedAt、不 backoff、可 respawn ----
  await test(15, "快速 exit(0) → 不记 failedAt、不进 backoff、可 respawn、打印“不是崩溃”日志", async () => {
    const d15 = dirFor("t15")
    const m15 = path.join(tmpRoot, "t15.marker")
    const l15 = lockPathOf(d15)
    const o = {
      options: {
        bridgeDir: d15,
        logDir: logsDir,
        python: RUNNER,
        args: evalArgs(fastOkExitCode(m15)),
        backoffMs: 60000, // 与 #10 相同：若误进快失败分支，第二次 setup 会被挡住
      } satisfies BridgePluginOptions,
    }
    // 日志文件是所有场景共用的 append 文件（#10 已写入过“快速非零退出”）：
    // 先等前序场景的异步 exit 日志落盘，再记录偏移，只对本场景新增的日志行做断言。
    const logPath = path.join(logsDir, "bridge-plugin.log")
    const readLog = (): string => {
      try {
        return fs.readFileSync(logPath, "utf8")
      } catch {
        return ""
      }
    }
    let logPrev = readLog()
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 25))
      const now = readLog()
      if (now === logPrev) break
      logPrev = now
    }
    const logBefore = logPrev
    const cleanup1 = plugin.setup(o)
    cleanups.push(cleanup1)
    seenLockPids.push(l15)

    // 1) 子进程被 spawn
    await waitFor(() => markerCount(m15) >= 1, 5000, "exit-0 子进程已启动")
    // 2) 退出处理跑完（exit(0) 很快发生）后：锁里不得有 failedAt（= 没进 backoff）
    await waitFor(() => !fs.existsSync(l15) || readLock(l15) === null || !("failedAt" in (readLock(l15) || {})), 5000, "exit(0) 后锁中不出现 failedAt")
    const lock15 = readLock(l15)
    assert(!lock15 || !("failedAt" in lock15), `exit(0) 后锁不应包含 failedAt，实际: ${JSON.stringify(lock15)}`)

    // 4) 日志出现“不是崩溃 / 不进 backoff / 尚未配置”语义（至少命中其一）
    const logText = readLog()
    const delta = logText.slice(logBefore.length)
    assert(
      delta.includes("不是崩溃") || delta.includes("不会进入 backoff") || delta.includes("尚未配置"),
      `日志应出现“不是崩溃/不会进入 backoff/尚未配置”之一，实际新增尾部: ${JSON.stringify(delta.slice(-800))}`,
    )
    // 5) 本场景新增日志不得出现“快速非零退出”（确认没走快失败分支）
    assert(
      !delta.includes("快速非零退出"),
      "exit(0) 不应打印“快速非零退出”（说明误入了快失败分支）",
    )

    // 3) 紧接着再跑一次 setup()：能正常 respawn（spawn 次数 +1），证明没被 backoff 挡住
    const cleanup2 = plugin.setup(o)
    cleanups.push(cleanup2)
    await waitFor(() => markerCount(m15) >= 2, 5000, "exit(0) 后再次 setup() 应正常 respawn")
    assert(markerCount(m15) === 2, `exit(0) 后 respawn 次数应为 2，实际 ${markerCount(m15)}`)

    cleanup2()
    cleanup1()
    assert(!fs.existsSync(l15), "最终锁应被删除")
  })
}

// ---------------------------------------------------------------------------
// 收尾：杀掉一切残留、删除临时目录、打印结果
// ---------------------------------------------------------------------------

async function teardown() {
  // 1) 逆序执行所有 cleanup（幂等，重复调用无害）
  for (const c of [...cleanups].reverse()) {
    try {
      c()
    } catch {
      /* ignore */
    }
  }
  // 2) 杀掉 harness 自己直接 spawn 的进程
  for (const ch of extraChildren) {
    try {
      ch.kill()
    } catch {
      /* ignore */
    }
  }
  // 3) 兜底：扫描仍未删除的锁，杀掉其中 pid 仍存活的
  for (const lp of seenLockPids) {
    try {
      const l = readLock(lp)
      if (l && typeof l.pid === "number" && isAlive(l.pid)) {
        try {
          process.kill(l.pid)
        } catch {
          /* ignore */
        }
      }
      if (fs.existsSync(lp)) fs.unlinkSync(lp)
    } catch {
      /* ignore */
    }
  }
  // 4) 还原被可移植性测试改过的插件自身状态
  resetPluginCfg()
  if (originalEnvBridgeDir === undefined) delete process.env.OPENCODE_BRIDGE_DIR
  else process.env.OPENCODE_BRIDGE_DIR = originalEnvBridgeDir

  // 5) 等待退出，再删临时目录
  const end = Date.now() + 5000
  const pending = (): number[] => {
    const pids: number[] = []
    for (const lp of seenLockPids) {
      const l = readLock(lp)
      if (l && typeof l.pid === "number" && isAlive(l.pid)) pids.push(l.pid)
    }
    for (const ch of extraChildren) if (ch.pid && isAlive(ch.pid)) pids.push(ch.pid)
    return pids
  }
  while (Date.now() < end && pending().length > 0) await sleep(100)
  try {
    fs.rmSync(tmpRoot, { recursive: true, force: true })
  } catch {
    /* ignore */
  }
}

async function main() {
  console.log(`harness: runner=${RUNNER} pid=${process.pid} tmp=${tmpRoot}`)
  try {
    await run()
  } finally {
    await teardown()
  }

  const passed = results.filter((r) => r.pass).length
  const failed = results.filter((r) => !r.pass)
  console.log("")
  if (failed.length === 0) {
    console.log(`PASS ${passed}/${results.length}`)
    process.exit(0)
  } else {
    for (const f of failed) console.error(`FAILED #${f.n} ${f.name}: ${f.err}`)
    console.log(`FAIL ${passed}/${results.length}`)
    process.exit(1)
  }
}

main().catch((e) => {
  console.error(`harness 崩溃: ${String(e)}`)
  process.exit(1)
})
