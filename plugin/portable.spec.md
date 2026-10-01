# portable.spec — bridgeDir 解析规则（可移植性规格）

本文件只记录 `index.ts` 中 `bridgeDir` 的解析与容错规则，供实现与 `harness.ts` 对照。

## 解析优先级（每级都容错，绝不抛异常）

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `ctx.options.bridgeDir` | 来自 `opencode.json` 的 `plugins` 条目；类型须为非空字符串，否则 warn 并落向下一级 |
| 2 | `path.join(import.meta.dirname, "config.json")` | **与 index.ts 同目录**的 config.json，安装器写入，**主路径** |
| 3 | 环境变量 `OPENCODE_BRIDGE_DIR` | 仅在 1、2 都未给出非空 `bridgeDir` 时生效 |
| 4 | （无默认值） | 三者皆缺 → **不 spawn** |

第 4 级行为：

```
console.error("[bridge-plugin] 未配置 bridgeDir，跳过启动…")
return () => {}   // no-op cleanup
```

- 不 spawn、不写锁、不抛异常；
- 源码中**不得**残留任何本机硬编码绝对路径（如 `D:\`、`D:/`）。

## 其它配置项

`enabled` / `python` / `args` / `logDir` / `backoffMs` / `lockName` 的优先级：

```
ctx.options  >  <插件目录>/config.json  >  内置默认值
```

类型校验失败只 `console.error` warn，不抛；`logDir` 为空时回退为 `bridgeDir`。

## `import.meta.dirname` 的两种部署形态

| 形态 | 插件位置 | 同目录 config.json |
| --- | --- | --- |
| 安装形态 | `$HOME/.config/opencode/plugins/bridge/` | 安装器写入（含 `bridgeDir` = clone 目录绝对路径） |
| 本地开发 | `<clone>/opencode-bridge/plugin/index.ts` | 手写或不存在（走 options / 环境变量） |

两种形态下 `import.meta.dirname` 都指向插件自身目录，因此第 2 级在两种形态下均正确。

## 配置文件读取约定

- 安装器以 **UTF-8 with BOM** 保存 `config.json`；插件读取时先去 BOM 再 `JSON.parse`
  （`JSON.parse` 不接受以 `U+FEFF` 开头的字符串）。
- JSON 损坏 → 忽略该文件并 warn，回落到下一级来源。

## 对应断言（`harness.ts`）

- #11：同目录 `config.json` 提供 `bridgeDir` → 正确 spawn（主路径）
- #12：`ctx.options.bridgeDir` 与 config.json 同时存在 → 用 options
- #13：无前两者时 `OPENCODE_BRIDGE_DIR` 生效
- #14：三者皆缺 → 不 spawn、不抛、cleanup 为 no-op；且 `index.ts` 不含 `D:\` / `D:/`
