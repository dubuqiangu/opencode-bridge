# opencode-bridge 文档质量与可安装性审计报告

- 审计对象：`opencode-bridge`，HEAD `ee983eef36e48f27fa132b15ff6a34fea99b5e74`（2026-11-05）
- 性质：**只读审计**（未 commit / 未执行安装脚本 / 未触碰 `~/.config/opencode-bridge`）
- 工作区状态：仅 untracked `docs/borrowing-catalog.md.bak.20261021_review`
- 事实基线：仓库现有平台 **14 个**（`README.md:8` 权威清单），其中 SMS 适配器落地于 2026-10-11（`e97bdd7` / 本体 `b4b5eaa`）
- 本报告每个结论都带 `file:line` 或 `符号名` 证据；所有代码事实均于本 HEAD 现查

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| (a) 文档是否与当前代码同步 | **部分不同步。** 14 处「平台数/能力」硬编码仍停留在 13 平台（sms 之前）时代，且 **README §8 与 README 能力表自相矛盾** |
| (b) 正文是否清晰、结构是否「只在末尾追加」 | **主结构清晰**，但存在明确的往末尾追加痕迹：README 编号段（1..11）被放在一大段无编号内容之后；§9 故障排查在大表格后追加 3 个小节；`config_version` 说明在 5 个文件里各抄一份 |
| (c) 新手能否照文档走完 | **能走通 Telegram/Slack/Discord 全流程**；**另 11 个平台**在 `docs/install.md` 的 AI 向导里被 Step 2 明确排除（只问三平台），新手/agent 会漏掉 |
| (d) 命令文档是否支持复制粘贴一键安装 | **支持。** 三条一键路径（原生 / PowerShell `iwr\|iex` / `curl\|bash`）+ jsDelivr 兜底都在，且脚本按设计不替用户重启 |
| 最严重的单点 | `README.md:910` 与 `README.md:175` 对「哪些平台没有编辑能力」的说法**与同文件 `README.md:158–173` 的能力表直接冲突**（只点名 IRC/Twitch，实际 8 家无编辑） |

---

## 1. 逐文件结论

### 1.1 `README.md`（1159 行）

- **受众 / 定位**：面向最终用户 + AI agent 的首页。前半（`:15–596`）是「安装 + 逐平台接入引导」，后半（`:607–1157`）是编号的架构/配置/安全/排障/开发/卸载。定位清晰。
- **结构顺序**：总序合理（先装、再配、再解释、最后排障）。⚠️ 结构缺陷见 §2。
- **与代码一致性**：
  - ✅ 平台清单 `:8`（14 个）与能力表 `:158–173`（14 行）**正确且当前**——编辑能力列 6 家 ✅ / 8 家 ❌ 与 `opencode_bridge/adapters/*.py` 逐一吻合。
  - 🔴 `:910` §8「已知限制」：`IRC / Twitch 完全没有编辑` —— 漏掉 `a2a/email/homeassistant/ntfy/qqbot/sms` 共 6 家（表 `:158–173` 明确 ❌）；同时把 Nextcloud 漏出「支持编辑」名单。
  - 🔴 `:175` 同一错误的第二次出现：`IRC / Twitch 没有它`（应为 8 家）。
  - 🔴 `:914` 「权限请求 v1 …不使用 inline buttons（跨平台行为不一致）」——telegram 与 slack 现已 `supports_inline_buttons=True`（`:160` 自述「按钮交互已支持」），该断言过期。
  - 🟡 `:39` XDG 路径写成 `${XDG_CONFIG_HOME:-$HOME}/.config/opencode-bridge`，与 `:80` 的 `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge` 不同，且在设置了 `XDG_CONFIG_HOME` 时会拼出双 `.config`（见 §3 C15）。
- **末尾追加痕迹**：`:597` 有 `## npm 方式（占位 / 待发布）`——一个自称「占位/待发布」的小节插在正文中段。

### 1.2 `docs/install.md`（583 行）

- **受众 / 定位**：面向「AI agent 代装」的分步向导（`## For Humans` + `## For AI Agents`）。定位清晰。
- **结构顺序**：Goal → Boundaries → Directory Rules → Step 1 安装 → Step 2 配置（14 个平台小节 `:126–508`）→ Step 3 激活 → Step 4 验证 → Step 5 汇报 → Quick Reference。顺序良好。
- **与代码一致性**：
  - 🔴 `:33` Goal 写 `Telegram/Slack/Discord ↔ 本机 opencode 的消息桥`；`:117` Step 2 写 `先问用户要接哪个平台（Telegram / Slack / Discord）`；`:145` 写 `三个平台（Telegram / Slack / Discord）都是如此`——三处都把范围锁死在 3 平台，而**本文件自己在 `:126–508` 列出了 14 个平台小节**，README 也覆盖 14。AI agent 若严格照 `:117` 执行，会**只给三个平台引导**。
  - 🟡 `:124` XDG 又一种写法 `${XDG_CONFIG_HOME:-~/.config}/opencode-bridge`（与 `:51` 的 `${XDG_CONFIG_HOME:-$HOME/.config}` 不一致）。
- **末尾追加痕迹**：Telegram 小节（`:126–182`）里塞进了一大段 5 条「常见坑」blockquote（`:148–169`）。这些坑确实是 Telegram 专属（privacy mode、409 双消费者、群 id 为负、无 @ 过滤、仅文本），内容正确；但它是**在写完引导后追加的一整块**，且只挂在 Telegram 下。

### 1.3 `docs/architecture.md`（267 行）

- **受众 / 定位**：面向维护者，回答「这项目怎么搭起来的」。含「关键不变量」清单，权威度高。
- **结构顺序**：立身约束 → 分层 → 入站/出站路径 → 模块职责 → 关键不变量 → 加平台成本。顺序良好。
- **与代码一致性**：**本文件是平台数过期最密集的一处**，5 处硬编码数字全部落后（见 §3 C1–C5），且 `:15` 把已集成的 SMS 列进「接不进来」。因为这份文件是「不变量」的权威出处，过期数字影响面最大。

### 1.4 `docs/adding-a-platform.md`（305 行）

- **受众 / 定位**：面向「新增一个平台」的贡献者清单。
- **结构顺序**：合理（步骤式）。
- **与代码一致性**：
  - 🟡 `:222` `13 个调用点全部显式传 ""` —— 现为 14 处 `split_text(..., prefix_fmt="")` 调用点（`adapters/` 内 13 处含 mattermost 两处 + `outbound.py:590` 一处）。
  - 🟡 `:252` `在你加第 14 个平台那天` —— 14 个平台**已经存在**，此句把「未来第 14 个」当成了当前语境。
  - ✅ `:233–235` 关于 `/setup` 只列三平台的说明与代码（`commands.py` 冻结文案）一致；README `:177–180` 也如实披露。

### 1.5 `docs/a2a.md`（201 行）

- **受众 / 定位**：a2a 单平台接入说明。
- **结构顺序**：一句话 → 配置 → 安全。清晰。
- **与代码一致性**：🟡 `:13` `其它十个都是我们主动去连` —— 已过时（现有 13 个「其它」，且 SMS 入站方向是「平台调我们」，非「我们主动去连」）。另 `:7–9` 有一个**自述未并入 README** 的陈旧注记（说本文件尚未被 README 链接），而 README `:12` 已链接它。

### 1.6 `docs/update.md`（192 行）

- **受众 / 定位**：更新向导（Humans + AI Agents）。
- **结构顺序**：Goal → Step 1..5。清晰。
- **与代码一致性**：✅ 未见平台数错误；`:66` XDG 用的是正确形式。**本文件是 XDG 写法的正确样板。**

### 1.7 `docs/platform-design-reference.md`（508 行）

- **受众 / 定位**：对比报告（非手册），供设计取舍参考。
- **结构顺序**：修订记录 → 逐项对比。合理。
- **与代码一致性**：🔴 **整篇是 13 平台时代**（`:29 :50 :200 :279 :281 :368 :386 :395 :396 :431 :438` 均写「13 个平台」），其自称的修订记录为 `2026-10-05`——**早于 SMS 落地（2026-10-11）**，即这次「更正」本身也已过期。因为它是被引用的决策依据，过期数字会被当成现状引用（文件 `:10` 自己就警告过「过期对比比没有对比更糟」）。

### 1.8 `plugin/README.md`（211 行）

- **受众 / 定位**：插件壳（`index.ts`）说明。
- **结构顺序**：安装 → 装完之后 → bridgeDir 解析 → 安全。清晰。
- **与代码一致性**：
  - 🔴 `:60` `（三个平台各一份` —— 紧接其后的 `:62–75` 列举了 **14 个平台**，同一段自相矛盾。
  - 🟡 `:97` XDG 用了错误形式 `${XDG_CONFIG_HOME:-$HOME}/.config/opencode-bridge`（与 `README:39` 同错）。
  - ✅ `:22`/`:177` 自述 harness 场景数与 `plugin/harness.ts:4–7` 一致（25 个）。

### 1.9 `install.ps1`（305 行）/ `install.sh`（280 行）

- **受众 / 定位**：一键安装脚本。
- **与代码一致性**：🔴 缺 Python 的处置**不一致**：`install.sh:104–109` 直接 `exit 1`（硬失败）；`install.ps1:179–182` 只 `Write-Warn` 后继续。同一个「一键安装」入口，Windows 新手会在**没装 Python 时也被告知安装成功**，随后桥静默不跑。两脚本都按设计**不执行** `opencode service restart`（安全，符合 README `:70`）。

### 1.10 `config.example.json` / `package.json` / `index.ts`

- ✅ `config.example.json`：14 个适配器块（含 `:31 sms`），`config_version: 0`（`:10`），与文档一致。
- 🟡 `package.json:5` `description` = `Bridge opencode to Telegram / Slack / Discord`——元数据里的 3 平台表述，会出现在 npm/插件列表（低危）。
- ✅ `plugin/index.ts:180/183–184`：路径解析代码为 Windows `%USERPROFILE%\.config\opencode-bridge`、Unix `$XDG_CONFIG_HOME/opencode-bridge`（否则 `$HOME/.config/opencode-bridge`）。**README/plugin 的两处 XDG 写法与它不符**（见 C15）。

---

## 2. 结构与「末尾追加」观察

1. **README 编号段被放在无编号内容之后**（`:607` 起才是 `## 1. 架构`）。
   `:15–596` 是无编号的安装与平台引导，`:607` 才出现 `## 1. 架构`，`:1157 ## License`。
   读者看到 `## 1.` 时前面已有约 590 行正文，编号体系与文档前一半脱节。
2. **`## npm 方式（占位 / 待发布）`（`:597`）** ——一个自称占位的空壳小节夹在正文中段，紧接着才是 `## 1. 架构`。
3. **§9 故障排查「大表格 + 末尾追加 3 小节」**（`:920` 表 → `:945`、`:1043`、`:1083` 三个 `###`）。
   表行用「见本节末尾 XXX 小节」回指（如 `:929`→`:1043`、`:931`→`:1083`、`:945` 小节），内部自洽，但这是典型的「先写表、再把解释追加到后面」的形态。
4. **`config_version`「两步走」说明抄了 5 份**：`README.md`（约 `:137` 与 `:863`）、`docs/install.md:145–147`、`plugin/README.md:101–108`、`install.ps1`（约 `:207–222`）、`install.sh`（约 `:198–213`）。改一次要同步五处，任何一处漏改都会让「下一步迁移语义」在不同入口说法不一。
5. **install.md 的 Telegram 坑 blockquote**（`:148–169`）是追加块，且只挂在 Telegram 下。

> 判定：不是「整篇都在末尾追加」，而是**局部存在追加模式**（§9、install.md Telegram 块、README 编号错位、config_version 五份拷贝）。

---

## 3. 事实性矛盾清单（每条带证据）

### C1 — SMS 被列进「接不进来」的平台
- 证据：`docs/architecture.md:15` `7 个 webhook-only 平台（WhatsApp官方/LINE/Teams/SMS/Synology/Zalo/Google Chat）接不进来`
- 现况：SMS **已集成**——`opencode_bridge/adapters/sms.py`、`README.md:173`、`plugin/README.md:75`、`config.example.json:31`。
- 影响：读者/agent 会误以为 SMS 不可用。🔴

### C2 — 「12 个平台 + a2a」与「a2a 是唯一反方向」
- 证据：`docs/architecture.md:16` `12 个平台走"我们主动连出去"…外加 a2a 这个反方向的例外`
- 现况：平台共 14；且 `README.md:13` 明写 SMS 入站方向也是「平台调我们」。🔴

### C3 — 「13 个平台里除 a2a 外全部走 transport/」
- 证据：`docs/architecture.md:53`
- 现况：14 个平台；SMS 与 a2a 一样由共享 `httpsrv.py` 承载（`README.md:13`/`:173`），并非「除 a2a 外全部走 transport」。🔴

### C4 — 无编辑能力平台数 7 → 应为 8
- 证据：`docs/architecture.md:89` `13 个平台里有 7 个没有：a2a / email / homeassistant / irc / ntfy / qqbot / twitch`
- 现况：14 个平台，8 家无编辑（补 `sms`）；与 `README.md:158–173` 表一致。🔴

### C5 — 「其余十二个平台」
- 证据：`docs/architecture.md:152`
- 现况：除 a2a 外应为 13 个。🟡

### C6 — a2a「其它十个」
- 证据：`docs/a2a.md:13`
- 现况：应为 13 个「其它」，且 SMS 入站非「主动去连」。🟡

### C7 — `split_text` 调用点数 13 → 应为 14
- 证据：`docs/adding-a-platform.md:222` `13 个调用点全部显式传 ""`
- 现况：14 处（`irc:829 / discord:1033 / homeassistant:1337 / mattermost:976,991 / ntfy:363 / nextcloud:1028 / matrix:583 / qqbot:1290 / slack:594 / sms:371 / telegram:992 / twitch:920` + `outbound.py:590`）。🟡

### C8 — 「第 14 个平台」被当成未来
- 证据：`docs/adding-a-platform.md:252` `在你加第 14 个平台那天`
- 现况：14 个平台已存在；此处应指第 15 个。🟡

### C9 — README §8 编辑能力与同文件能力表冲突
- 证据：`README.md:910` `IRC / Twitch 完全没有编辑`
- 冲突：`README.md:158–173` 表列出 8 家「编辑消息 ❌」（`ntfy/email/IRC/Twitch/a2a/QQ Bot/Home Assistant/SMS`）。🔴

### C10 — README 表下注再次漏数
- 证据：`README.md:175` `IRC / Twitch 没有它`
- 冲突：同上，应为 8 家。🔴

### C11 — 「不使用 inline buttons」已过期
- 证据：`README.md:914` `权限请求 v1 只发文字提示，不使用 inline buttons（跨平台行为不一致）`
- 现况：telegram + slack `supports_inline_buttons=True`（`README.md:160` 自述「按钮交互已支持」）。🔴

### C12 — plugin/README 同段自相矛盾
- 证据：`plugin/README.md:60` `（三个平台各一份` vs `:62–75` 列举 14 个平台。🔴

### C13 — install.md 把范围锁死为 3 平台
- 证据：`docs/install.md:33`、`:117`、`:145`（三处均为 Telegram/Slack/Discord）
- 冲突：同文件 `:126–508` 有 14 个平台小节；README 覆盖 14。🔴（对「照本执行的 agent」影响最大）

### C14 — package.json 描述
- 证据：`package.json:5` `Bridge opencode to Telegram / Slack / Discord`。🟡

### C15 — XDG 路径三种写法，两种是错的
- 错误形式（设置了 `XDG_CONFIG_HOME` 时会得到 `<xdg>/.config/opencode-bridge`）：
  - `README.md:39` `${XDG_CONFIG_HOME:-$HOME}/.config/opencode-bridge`
  - `plugin/README.md:97` 同上
- 正确形式：`README.md:80`、`docs/install.md:51`、`docs/update.md:66` 用 `${XDG_CONFIG_HOME:-$HOME/.config}/opencode-bridge`
- 第三种：`docs/install.md:124` `${XDG_CONFIG_HOME:-~/.config}/opencode-bridge`
- 代码真值：`plugin/index.ts:183–184` = `$XDG_CONFIG_HOME/opencode-bridge` 否则 `$HOME/.config/opencode-bridge`。🔴

### C16 — 缺 Python 的两脚本行为相反
- 证据：`install.sh:104–109` `exit 1`（硬失败）vs `install.ps1:179–182` 仅 `Write-Warn` 后继续。🔴（新手可见后果不同）

### C17 — 设计参考整篇落后于 SMS
- 证据：`docs/platform-design-reference.md:8–9`（自述修订到「13 个平台」，日期 `2026-10-05`），及 `:29 :50 :200 :279 :281 :368 :386 :395 :396 :431 :438`。
- 现况：14 个平台，SMS 于 `2026-10-11` 落地。该文件的「更正版」本身已过期。🔴

### C18（元证据）— tasks.md 声称已修，实际未修完
- `tasks.md:219`（`sms 平台文档缺位`，2026-10-11 `e97bdd7` 闭合）称已把「13 个」措辞改为指向清单，但 `architecture.md` / `a2a.md` / `adding-a-platform.md` / `platform-design-reference.md` **仍写着 13**——该修复不完整。（tasks.md 不在本次范围，仅作证据。）

---

## 4. 新手全流程走查（安装 → 配置 → 启动 → 平台 → 验证）

按 README / install.md 逐段核对：

| 阶段 | 是否有清晰指引 | 证据 | 缺口 |
|---|---|---|---|
| 安装 | ✅ | `README.md:29–35` 原生；`:58–68` 脚本；`:97–128` jsDelivr 兜底；`install.ps1`/`install.sh` | 无 |
| 前置 | ✅ 但两脚本不一致 | `README.md:42` Python 3.10+ | Windows 脚本缺 Python 不报错（C16） |
| 配置 | ⚠️ 分叉 | `README.md:154–596` 14 节；`install.md:126–508` 14 节；`:177–180` 披露 `/setup` 仅 3 平台 | `install.md:117` 只问 3 平台（C13）；11 个平台**无 bot 内引导** |
| 启动 | ✅ | `README.md:38` 填 token → `opencode service restart`；`install.md` Step 3 激活 | 脚本刻意不重启，需用户点头（正确） |
| 平台（非 3 主平台） | ⚠️ | README 有 14 节；`/setup` 只覆盖 3 家 | 新手在 ntfy/email/IRC/a2a/QQ/HA/SMS/… 上无法在 bot 内看引导，只能回 README |
| 验证 | ✅ | 各平台节「发 hi 收回复」；`install.md` Step 4 验证；`--status` 全平台核对 | 无 |

**结论**：Telegram/Slack/Discord 全流程**可无缺口走通**；其余 11 平台**文档齐全但入口分叉**（README 有、bot 内没有、install.md 的 agent 向导不问）。这不是「缺文档」，是「入口不一致」。

---

## 5. 一键安装验证

| 路径 | 命令 / 位置 | 结论 |
|---|---|---|
| 原生插件（推荐） | `opencode plugin add github:dubuqiangu/opencode-bridge`（`README.md:31–35`） | ✅ 单命令 |
| Windows | `iwr https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.ps1 \| iex`（`README.md:61`） | ✅ 可直接粘贴 |
| macOS/Linux | `curl -fsSL https://raw.githubusercontent.com/dubuqiangu/opencode-bridge/main/install.sh \| bash`（`README.md:67`） | ✅ 可直接粘贴 |
| 镜像兜底 | `README.md:97–128`（jsDelivr） | ✅ 已提供 |
| 安装后动作 | 脚本只 clone/铺文件/写插件 config；**不执行** `opencode service restart`（`README.md:70`；`install.sh:271` 用 `--check`） | ✅ 安全、符合文档 |

- 依赖前置：两脚本都需要 `git`（`install.ps1:174` 报错引导）；Python 判定见 C16。
- 结论：**(d) 支持复制粘贴一键安装 = 成立**；唯一瑕疵是 Windows 缺 Python 不报错。

---

## 6. 优先级建议（如需修复）

| 优先级 | 项 |
|---|---|
| P0 | C9 / C10（README §8、`:175` 与能力表冲突）、C1–C5（architecture.md 平台数与 SMS）、C13（install.md 三平台锁定） |
| P1 | C11（inline buttons 过期）、C12（plugin/README 三段自相矛盾）、C15（XDG 错误形式 2 处）、C16（脚本缺 Python 行为不一致） |
| P2 | C6 / C7 / C8（a2a、调用点、第 N 个平台）、C14（package.json 描述）、C17（设计参考整篇）、C18（补全 tasks.md 声称的修复） |
| P3 | §2 结构项：README 编号错位、`npm 方式（占位/待发布）`、`config_version` 五份拷贝合并为单源 |

> 注：本报告只做审计取证，未改动任何仓库文件（除本报告自身写入 `.tmp/general-docs/`）。
