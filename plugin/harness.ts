// harness.ts — opencode-bridge 插件独立验证脚本（用 bun 跑，不触碰 opencode 服务）。
//   cd opencode-bridge\plugin
//   bun harness.ts
// 覆盖 25 个场景（10 个生命周期 + 4 个可移植性/bridgeDir 解析 + 1 个 exit-0 语义 +
//   7 个第 4 级自举：路径推导/铺源/不覆盖 config/git clone 不碰/刷新源码/enabled 零副作用/端到端、
//   3 个 opencode 内接入引导面：工具+命令注册/工具透传平台/命令经 session.prompt 送达），
//   全部断言通过才打印 PASS 25/25 并退出 0；任一失败退出码 1。结束时杀掉所有子进程、删临时目录与锁、
// 还原被它改动的 <插件目录>/config.json、OPENCODE_BRIDGE_DIR 与 home 相关环境变量。
// 注意：全程不运行真实的 python -m opencode_bridge，也不触碰 opencode 服务。

import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import plugin from "./index.ts"
import { deriveStableDir, ensureMaterialized } from "./index.ts"
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
// bridgeDir 第 4 级（自举稳定目录）按 USERPROFILE / HOME / XDG_CONFIG_HOME 推导真实用户目录。
// 测试期间一律改指 tmpRoot，绝不碰开发者/安装器已有的 ~/.config/opencode-bridge（teardown 还原）。
const originalHomeEnv: Record<string, string | undefined> = {
  USERPROFILE: process.env.USERPROFILE,
  HOME: process.env.HOME,
  XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME,
}
const FAKE_HOME = path.join(tmpRoot, "fakehome")

function setFakeHome(): void {
  process.env.USERPROFILE = FAKE_HOME
  process.env.HOME = FAKE_HOME
  delete process.env.XDG_CONFIG_HOME
}

/** 把 home 指向 tmpRoot 下**独立**目录（各自举场景互不污染），返回该环境下的稳定目录。 */
function useHome(name: string): string {
  const home = path.join(tmpRoot, name)
  process.env.USERPROFILE = home
  process.env.HOME = home
  delete process.env.XDG_CONFIG_HOME
  return deriveStableDir()
}

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
  setFakeHome() // 第 4 级自举的 home 推导也隔离到 tmpRoot
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
  await test(14, "前三级皆缺且自举失败 → 不 spawn、不抛、no-op cleanup；源码无硬编码盘符路径", async () => {
    resetPluginCfg()
    delete process.env.OPENCODE_BRIDGE_DIR
    assert(!fs.existsSync(pluginCfgPath), "预检失败: 插件目录不应存在 config.json")
    // 让第 4 级自举**确定失败**：把 home 指向一个“文件”→ 稳定目录 mkdir 必失败 → bridgeDir 保持空。
    // （第 4 级成功的正常安装路径由 #16~#22 覆盖）
    const blocker = path.join(tmpRoot, "t14-home-blocker")
    fs.writeFileSync(blocker, "not a directory")
    process.env.USERPROFILE = blocker
    process.env.HOME = blocker
    process.env.XDG_CONFIG_HOME = blocker
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

  // =========================================================================
  // bridgeDir 第 4 级：默认自举稳定目录（16~22）
  // =========================================================================

  /** 造一个“包根”夹具：<root>/plugin/index.ts + opencode_bridge/ + config.example.json。 */
  function makePkgRoot(name: string, withPy = true, withExample = true): string {
    const root = path.join(tmpRoot, name)
    fs.mkdirSync(path.join(root, "plugin"), { recursive: true })
    fs.writeFileSync(path.join(root, "plugin", "index.ts"), "// fixture\n")
    if (withPy) {
      fs.mkdirSync(path.join(root, "opencode_bridge"), { recursive: true })
      fs.writeFileSync(path.join(root, "opencode_bridge", "__init__.py"), "# v1\n")
      fs.writeFileSync(path.join(root, "opencode_bridge", "__main__.py"), "# main v1\n")
    }
    if (withExample) {
      fs.writeFileSync(
        path.join(root, "config.example.json"),
        JSON.stringify({ adapters: { telegram: { bot_token: "", allowed_chat_ids: [] } } }, null, 2),
      )
    }
    return root
  }

  await test(16, "deriveStableDir：Windows / Linux(XDG) / Linux(默认) 三分支", () => {
    // ⛔ `x` / `y` 是**一字母假用户**，不是本机用户名 —— AGENTS.md §2.2 要替换的
    //   是「这台机器上的真值」，而 §2.2 规定的补救形式是 `example-user` 这类占位符。
    //   曾把这里拆成片段拼接过一次：那是在满足一个把占位符误判成泄漏的扫描器，
    //   既无必要，又把一条不存在的规定归给了 §2.4（见 AGENTS.md §7.1「报命中先怀疑判据」）。
    const win = deriveStableDir({ USERPROFILE: "C:\\Users\\x" }, "win32")
    assert(win === path.join("C:\\Users\\x", ".config", "opencode-bridge"), `win32 分支错误: ${win}`)
    const xdg = deriveStableDir({ HOME: "/home/y", XDG_CONFIG_HOME: "/home/y/cfg" }, "linux")
    assert(xdg === path.join("/home/y/cfg", "opencode-bridge"), `XDG 分支错误: ${xdg}`)
    const dflt = deriveStableDir({ HOME: "/home/y" }, "linux")
    assert(dflt === path.join("/home/y", ".config", "opencode-bridge"), `默认分支错误: ${dflt}`)
  })

  await test(17, "自举：全新稳定目录铺入 Python 源码 + 由 example 生成 config.json", () => {
    const pkg = makePkgRoot("pkg17")
    const stable = useHome("home17")
    assert(!fs.existsSync(stable), "预检失败: 稳定目录应尚不存在")
    const r = ensureMaterialized(stable, pkg)
    // 目录本身由 copyTree 的 mkdirSync 顺带创建，故 created 只在“本函数创建了空目录”时为 true；
    // 这里关注的是源码与 config 落盘，created 不作断言（见 #22 端到端覆盖目录建立）。
    assert(r.copiedSource, "应铺入 Python 源码")
    assert(r.wroteConfig, "应由 config.example.json 生成 config.json")
    assert(r.hasSource, "结束时应有 Python 源码")
    assert(fs.existsSync(path.join(stable, "opencode_bridge", "__init__.py")), "源码文件应落盘")
    assert(!fs.existsSync(path.join(stable, "opencode_bridge", "__pycache__")), "不应拷贝 __pycache__")
  })

  await test(18, "自举：已有 config.json 绝不覆盖（token 安全核心断言）", () => {
    const pkg = makePkgRoot("pkg18")
    const stable = useHome("home18")
    fs.mkdirSync(stable, { recursive: true })
    const sentinel = JSON.stringify({ adapters: { telegram: { bot_token: "SENTINEL-TOKEN" } } })
    fs.writeFileSync(path.join(stable, "config.json"), sentinel)
    const r = ensureMaterialized(stable, pkg)
    assert(r.copiedSource, "仍应铺入 Python 源码")
    assert(!r.wroteConfig, "不应重写已存在的 config.json")
    assert(
      fs.readFileSync(path.join(stable, "config.json"), "utf8") === sentinel,
      "config.json 内容必须逐字节不变",
    )
  })

  await test(19, "自举：稳定目录是 git clone → 一个字节都不碰", () => {
    const pkg = makePkgRoot("pkg19")
    const stable = useHome("home19")
    fs.mkdirSync(path.join(stable, "opencode_bridge"), { recursive: true })
    fs.writeFileSync(path.join(stable, "opencode_bridge", "__init__.py"), "# git v1\n")
    fs.mkdirSync(path.join(stable, ".git"), { recursive: true })
    const r = ensureMaterialized(stable, pkg)
    assert(r.gitCloneUntouched, "应识别为 git clone 并跳过")
    assert(!r.copiedSource && !r.refreshedSource, "不应拷贝/刷新源码")
    assert(
      fs.readFileSync(path.join(stable, "opencode_bridge", "__init__.py"), "utf8") === "# git v1\n",
      "git clone 内源码不得被改写",
    )
  })

  await test(20, "自举：非 git 已有源码 → 刷新 .py（plugin update 语义）且保留 config.json", () => {
    const pkg = makePkgRoot("pkg20")
    const stable = useHome("home20")
    fs.mkdirSync(path.join(stable, "opencode_bridge"), { recursive: true })
    fs.writeFileSync(path.join(stable, "opencode_bridge", "__init__.py"), "# old v1\n")
    const sentinel = JSON.stringify({ keep: true })
    fs.writeFileSync(path.join(stable, "config.json"), sentinel)
    const r = ensureMaterialized(stable, pkg)
    assert(r.refreshedSource, "应刷新源码")
    assert(!r.gitCloneUntouched, "不应被当作 git clone")
    assert(
      fs.readFileSync(path.join(stable, "opencode_bridge", "__init__.py"), "utf8") === "# v1\n",
      "源码应被刷新为包内版本",
    )
    assert(fs.readFileSync(path.join(stable, "config.json"), "utf8") === sentinel, "config.json 必须不变")
  })

  await test(21, "enabled=false 时自举零副作用（稳定目录不被创建）", () => {
    const stable = useHome("home21")
    const m21 = path.join(tmpRoot, "t21.marker")
    const cleanup = plugin.setup({
      options: { enabled: false, logDir: logsDir, python: RUNNER, args: evalArgs(liveCode(m21)) },
    })
    cleanups.push(cleanup)
    assert(typeof cleanup === "function", "setup 应返回 cleanup 函数")
    assert(!fs.existsSync(stable), "enabled=false 不应创建稳定目录")
    assert(markerCount(m21) === 0, "不应 spawn 子进程")
  })

  await test(22, "第 4 级端到端：无 bridgeDir 时自举到稳定目录并正常 spawn", async () => {
    // 独立 fake home，避免与 #17~#20 复用同一稳定目录
    const home22 = path.join(tmpRoot, "t22home")
    process.env.USERPROFILE = home22
    process.env.HOME = home22
    delete process.env.XDG_CONFIG_HOME
    const stable = deriveStableDir()
    assert(!fs.existsSync(stable), "预检失败: t22 稳定目录应尚不存在")
    const m22 = path.join(tmpRoot, "t22.marker")
    // 只给 logDir/python/args：bridgeDir 四级全靠插件自己解析（第 4 级自举）
    const cleanup = plugin.setup({
      options: { logDir: logsDir, python: RUNNER, args: evalArgs(liveCode(m22)) } satisfies BridgePluginOptions,
    })
    cleanups.push(cleanup)
    seenLockPids.push(path.join(stable, ".bridge-plugin.lock"))
    assert(
      fs.existsSync(path.join(stable, "opencode_bridge", "__init__.py")),
      "setup 应已把包内 Python 源码铺到稳定目录",
    )
    await waitFor(() => markerCount(m22) >= 1, 5000, "自举后的 bridge 子进程已启动")
    const lock = readLock(path.join(stable, ".bridge-plugin.lock"))
    assert(lock, "锁应生成在自举出的稳定目录下")
    const pid = lock!.pid as number
    cleanup()
    await waitFor(() => !isAlive(pid), 5000, "子进程被 cleanup 终止")
  })

  // =========================================================================
  // opencode 内接入引导面：bridge_setup 工具 + /bridge-setup 命令（23~25）
  // =========================================================================

  /** 假 setup 子进程：把收到的 argv 打到 stdout，模拟 `python -m opencode_bridge --setup`。 */
  function setupEchoCode(): string {
    return `process.stdout.write('ARGV='+process.argv.slice(1).join(' '))`
  }

  /** 收集 ctx.tool.transform / ctx.command.transform 注册物的假 ctx。 */
  function fakeCtx() {
    const tools: Array<Record<string, unknown>> = []
    const commands: Array<Record<string, unknown>> = []
    const prompts: Array<Record<string, unknown>> = []
    return {
      tools,
      commands,
      prompts,
      ctx: {
        tool: { transform: (cb: (e: { add: (t: Record<string, unknown>) => void }) => void) => cb({ add: (t) => tools.push(t) }) },
        command: {
          transform: (cb: (e: { add: (d: Record<string, unknown>) => void }) => void) => cb({ add: (d) => commands.push(d) }),
        },
        session: { prompt: (i: Record<string, unknown>) => prompts.push(i) },
      },
    }
  }

  await test(23, "注册 bridge_setup 工具 + /bridge-setup 命令；无 bridgeDir 时不抛", async () => {
    const home23 = useHome("home23")
    const f = fakeCtx()
    plugin.setup({ ...f.ctx, options: { logDir: logsDir } })
    // 注册是异步的（等第一次 spawn 落盘），给点时间
    await waitFor(() => f.tools.length > 0 && f.commands.length > 0, 8000, "工具与命令完成注册")
    assert(f.tools.some((t) => t.name === "bridge_setup"), "应注册 bridge_setup 工具")
    assert(f.commands.some((c) => c.name === "bridge-setup"), "应注册 /bridge-setup 命令")
    assert(home23.length > 0, "stable 路径应可推导")
  })

  await test(24, "bridge_setup.execute 透传 platform 并返回引导 + 状态", async () => {
    const stable24 = useHome("home24")
    assert(stable24.length > 0, "stable 路径应可推导")
    const f = fakeCtx()
    // setupCommand 覆盖为假命令：记录 argv，验证平台确实被传下去
    plugin.setup({
      ...f.ctx,
      options: {
        logDir: logsDir,
        python: RUNNER,
        setupCommand: evalArgs(setupEchoCode()),
      } satisfies BridgePluginOptions,
    })
    await waitFor(() => f.tools.length > 0, 8000, "工具注册完成")
    const tool = f.tools.find((t) => t.name === "bridge_setup") as
      | { execute: (i: Record<string, unknown>) => Promise<{ content: string }> }
      | undefined
    assert(tool, "工具应存在")

    const res = await tool!.execute({ platform: "telegram" })
    const content = String(res.content)
    assert(content.includes("接入引导"), "返回应含「接入引导」")
    assert(content.includes("当前配置状态"), "返回应含「当前配置状态」")
    assert(content.includes("ARGV="), "应包含 setup 子进程输出")

    // 无参 → 平台菜单路径（不应抛）
    const res2 = await tool!.execute({})
    assert(String(res2.content).includes("接入引导"), "无参调用也应返回引导")

    // 非法平台不应抛（归一化为菜单）
    const res3 = await tool!.execute({ platform: "nope" })
    assert(String(res3.content).includes("接入引导"), "非法平台应降级为菜单，不抛")
  })

  await test(25, "/bridge-setup 命令把引导原文经 session.prompt 送达", async () => {
    useHome("home25")
    const f = fakeCtx()
    plugin.setup({
      ...f.ctx,
      options: { logDir: logsDir, python: RUNNER, setupCommand: evalArgs(setupEchoCode()) } satisfies BridgePluginOptions,
    })
    await waitFor(() => f.commands.length > 0, 8000, "命令注册完成")
    const cmd = f.commands.find((c) => c.name === "bridge-setup") as
      | { execute: (i: Record<string, unknown>) => Promise<void> }
      | undefined
    assert(cmd, "命令应存在")
    await cmd!.execute({ sessionID: "ses_test", prompt: "/bridge-setup slack", delivery: "steer" })
    await waitFor(() => f.prompts.length > 0, 5000, "session.prompt 被调用")
    const p = f.prompts[0]
    assert(p.sessionID === "ses_test", "prompt 应带 sessionID")
    assert(String(p.text).includes("原样"), "prompt 应要求原样展示")
    assert(String(p.text).includes("slack"), "prompt 应包含平台参数")
    assert(String(p.text).includes("ARGV="), "prompt 应含 setup 子进程的实际输出")
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
  // 第 4 级自举测试改过 home 相关 env，一并还原
  for (const [k, v] of Object.entries(originalHomeEnv)) {
    if (v === undefined) delete process.env[k]
    else process.env[k] = v
  }

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
