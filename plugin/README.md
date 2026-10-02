# opencode-bridge 插件（方案 A）

让 `opencode-bridge` 随 **opencode 启动**而自动启动：opencode 加载本插件时，
在 server 进程内拉起 `python -m opencode_bridge`（cwd = clone 下来的 bridge 目录），
并用锁文件保证**同一时刻只有一个** bridge 子进程。

插件是纯对象默认导出（不 `import "@opencode/plugin"`），与 `elapsed-timer` 同款写法，
opencode V2 会**自动发现** `~/.config/opencode/plugins/<name>/` 下的插件，
**不需要**在 `opencode.json` 里加 `plugins` 键（也可以加，见下文优先级）。

> **可分发**：本插件不含任何本机绝对路径。`bridgeDir` 由安装器写入的 `config.json`、
> `opencode.json` 的 `plugins` 条目选项或环境变量提供；三者都拿不到就不启动（只打一条错误日志）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `index.ts` | 插件本体（唯一实现） |
| `package.json` | 插件清单（`{"name":"opencode-bridge",...,"exports":{".":"./index.ts"}}`） |
| `config.example.json` | 配置模板（`bridgeDir` 留空，由安装器按脚本位置填充） |
| `install.ps1` | 一键安装到 `~\.config\opencode\plugins\bridge\`（支持 `-Force`） |
| `harness.ts` | 独立验证脚本（bun 运行，14 个断言） |
| `portable.spec.md` | bridgeDir 解析规则的简要规格 |
| `README.md` | 本文件 |

## 安装与布局

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1          # 已有 config.json 且含 bridgeDir 时不覆盖
powershell -ExecutionPolicy Bypass -File install.ps1 -Force   # 强制按当前脚本位置重写 config.json
```

安装后的布局（与仓库根安装器约定一致）：

```
$HOME/.config/opencode/plugins/bridge/
    index.ts        # 插件本体
    package.json    # 插件清单
    config.json     # {"bridgeDir": "<clone 目录绝对路径>", ...}
```

- Windows：clone 目录约定为 `%USERPROFILE%\.config\opencode-bridge`
- Linux/macOS：`${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge`

`install.ps1` 的 `bridgeDir` **由脚本自身所在目录推导**（`Split-Path -Parent $PSScriptRoot`
= `plugin\` 的上级 = 仓库根），因此 clone 到任何路径都正确；生成的 `config.json`
为 **UTF-8 with BOM**（插件读取时自动去 BOM）。脚本**不会**替你重启 opencode。

启用（需手动执行，脚本不代劳）：

```
opencode service restart
```

或关闭并重新打开 opencode TUI。

## 装完之后

插件装好只代表 bridge 能随 opencode 启动，**还要按平台填好 token** 才有消息进出。
完整的分步引导见仓库根 [`README.md`](../README.md) 的「接入平台引导」一节（三个平台各一份，含配置文件路径），这里只给一句话概述：

- **Telegram**：`@BotFather` 建 bot 拿 token、`@userinfobot` 查 chat id，填进 `adapters.telegram` → 支持双向对话；
- **Slack**：建 App 后开 Socket Mode 拿 `xapp-` app-level token、再取 `xoxb-` bot token，两枚都填进 `adapters.slack` → 支持双向对话；
- **Discord**：开发者后台建 Application、开 MESSAGE CONTENT INTENT、用 URL Generator 邀请进服务器 → v1 仅能主动发送。

填好 token 并 `opencode service restart` 之后，可在 bot 内发送 **`/setup`** 查看 / 重温这套引导
（`/setup telegram`、`/setup slack`、`/setup discord` 可直达对应平台）。

## bridgeDir 解析优先级

每级都容错（拿不到就落向下一级，绝不抛异常）。四级解析链：

1. **`ctx.options`** —— 来自 `opencode.json` 的 `plugins` 条目选项；
2. **`<插件目录>/config.json`** —— 与 `index.ts` 同目录，安装器写入，**主路径**（`import.meta.dirname` 在"安装形态"与"本地开发形态"下都指向插件自身目录）；
3. **环境变量 `OPENCODE_BRIDGE_DIR`**；
4. **默认自举稳定目录** —— 前三级都拿不到时，落到插件首启自动自举的稳定 bridge 目录：

   | 平台 | 路径 |
   | --- | --- |
   | Windows | `%USERPROFILE%\.config\opencode-bridge` |
   | macOS / Linux | `${XDG_CONFIG_HOME:-$HOME}/.config/opencode-bridge` |

   自举时已有 `config.json` **绝不覆盖**；稳定目录本身是 git clone（脚本装法）时**完全不碰它**；`enabled: false` 时**零副作用**（不自举、不 spawn）。

其它配置项（`enabled` / `python` / `args` / `logDir` / `backoffMs` / `lockName`）
的优先级是：`ctx.options` > 同目录 `config.json` > 内置默认值。

## 配置项

```jsonc
{
  "enabled": true,                       // false = 插件什么都不做
  "bridgeDir": "",                       // bridge 仓库(clone)目录的绝对路径；空 = 走上面的解析链，最终"未配置"则不启动
  "python": "python",                    // 子进程可执行文件（PATH 上任意：python/node/bun...）
  "args": ["-m", "opencode_bridge"],     // 启动参数
  "logDir": "",                          // 空 = bridgeDir；插件日志与 bridge 输出日志目录
  "backoffMs": 300000,                   // 子进程快速非零退出后的静默期（默认 5 分钟）
  "lockName": ".bridge-plugin.lock"      // 锁文件名（位于 bridgeDir 下）
}
```

字段缺失 / 类型不对 → 回退默认值并 `console.error` 提示，**绝不抛异常**。
JSON 解析失败（含 BOM / 截断）→ 忽略该文件并提示，不抛。

## 日志

- `console.log` / `console.error` 统一带 `[bridge-plugin]` 前缀 → 会进
  `~\.local\share\opencode\log\opencode.log`；
- 同时 append 到 `<logDir>\bridge-plugin.log`，每行带 ISO 时间戳；
- bridge 子进程的 stdout/stderr → `<logDir>\bridge-output.log`；
- 写日志本身全程 try/catch，绝不因日志失败影响 opencode。

`logDir` 默认为空 → 回退为 `bridgeDir`，即日志落在 clone 目录里。

## 单例与生命周期

锁文件 `<bridgeDir>\.bridge-plugin.lock`，内容形如：

```json
{ "pid": 1234, "servicePid": 5678, "startedAt": 1690000000000, "failedAt": 1690000001000 }
```

`setup(ctx)` 的判定（全部包在 try/catch 里，失败只 log）：

1. `enabled === false` → 直接返回无操作 cleanup；
2. `bridgeDir` 未配置或不存在 → log 后返回，不 spawn；
3. 读锁：
   - 锁存在、`pid` 存活、`servicePid === process.pid` → **同进程其它 location 已启动 → 采纳**（不 spawn、cleanup 不 kill 不删锁）；
   - 锁存在、`pid` 存活、`servicePid !== process.pid`（如上一次 opencode 重启残留）→ **杀掉旧进程**后重新 spawn；
   - 锁存在、`pid` 已死：若 `failedAt` 仍在 `backoffMs` 静默期内 → **保留锁**并跳过 spawn；否则删锁后 spawn；
   - 无锁 / 锁损坏（非法 JSON）→ 清理后 spawn；
4. spawn 前用 `fs.openSync(lockPath, "wx")`（O_EXCL）写锁，拿到 `EEXIST` 就重读锁重新判定，避免并发 location 双 spawn；
5. `stdout`/`stderr` 都重定向到 `<logDir>\bridge-output.log`；子进程**快速（<60s）非零退出** → 锁里记录 `failedAt` 进入 backoff，否则退出时删锁；
6. cleanup：**仅当子进程由本实例 spawn**（`ownsChild`）才 `child.kill()`，且锁 `pid` 匹配时才删锁；采纳来的进程不动。

三种"多实例"语义由此覆盖：

- **多 location**：同 opencode 进程内多次加载 → 第二次起采纳，不重复 spawn；
- **opencode 重启**：上个进程残留的 bridge（`servicePid` 不同）→ 杀旧换新；
- **快速失败 backoff**：启动后 <60s 非零退出 → 记 `failedAt`，`backoffMs` 内不再尝试（锁保留到静默期结束）。

存活检测用 `process.kill(pid, 0)`，任何异常（含 Windows 上的误判）一律当作"不存活"。

## 本地自检（不触碰 opencode 服务）

```powershell
cd opencode-bridge\plugin
bun build index.ts --no-bundle        # 语法/解析检查
bun harness.ts                        # 期望: PASS 14/14，退出码 0
```

`harness.ts` 覆盖 25 个场景：

- #1~#10 生命周期：首次 spawn、同进程采纳、双 cleanup 语义、残留锁（杀旧重启）、
  死 pid 锁、`enabled:false`、损坏锁、`bridgeDir` 缺失、快速失败 + backoff；
- #11~#14 可移植性：同目录 `config.json` 提供 `bridgeDir`（主路径）、`ctx.options` 优先于
  `config.json`、环境变量 `OPENCODE_BRIDGE_DIR` 兜底、三级皆缺且自举失败时不 spawn 不抛；
- #15 快速 `exit(0)`（未配置 adapter 的正常退出）不记 `failedAt`、不进 backoff、可 respawn；
- #16~#22 第 4 级自举：`deriveStableDir` 三分支、全新目录铺源并生成 `config.json`、
  已有 `config.json` 绝不覆盖、稳定目录是 git clone 时不碰、非 git 目录刷新 `.py`、
  `enabled:false` 零副作用、无 `bridgeDir` 时端到端自举 + 正常 spawn；
- #23~#25 opencode 内接入引导面：工具 `bridge_setup` 与命令 `/bridge-setup` 注册、
  工具把 `platform` 透传给 `--setup` 子进程并返回引导 + 状态、命令经 `session.prompt`
  把引导原文送达；
- 另断言 `index.ts` 源码不含 `D:\` / `D:/` 等硬编码盘符路径。

harness 结束会杀掉它 spawn 的全部子进程、删临时目录与锁，并还原被它临时改写的
`<插件目录>/config.json`、`OPENCODE_BRIDGE_DIR` 与 `USERPROFILE` / `HOME` / `XDG_CONFIG_HOME`。
自举相关用例各自使用独立的临时 home，互不污染，也绝不触碰真实的 `~/.config/opencode-bridge`。

> 注意：harness **不会**也不该运行真实的 `python -m opencode_bridge`
> （未配置 bot_token 时它会以退出码 1 结束，反而触发 backoff）。

## 卸载

```bash
# 原生安装（首选）：只摘配置 + 清包缓存，稳定目录（config.json / 日志）保留
opencode plugin remove github:dubuqiangu/opencode-bridge
```

```powershell
# 脚本安装产物：删除插件目录
Remove-Item -Recurse -Force "$env:USERPROFILE\.config\opencode\plugins\bridge"
```

然后重启 opencode。clone 目录本身（含日志与锁）按需自行删除。两种安装方式不要并存。
