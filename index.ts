// opencode-bridge 包入口（`opencode plugin add github:dubuqiangu/opencode-bridge` 走这里）。
// 实现本体在 plugin/index.ts；本文件只做再导出，兼容两种加载方式：
//   - loader 解析 package.json 的 exports["."]  → 本文件
//   - loader 裸 glob 包根的 index.ts           → 本文件
// 插件本身是纯对象默认导出（不 import "@opencode/plugin"，避免构建期解析失败）。

export { default } from "./plugin/index.ts"
export type { BridgePluginOptions } from "./plugin/index.ts"
export { deriveStableDir, ensureMaterialized, packageRoot } from "./plugin/index.ts"
export type { MaterializeResult } from "./plugin/index.ts"
