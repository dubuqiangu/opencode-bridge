"""§4.1 纪律 ③ 的机器守门：tasks.md 未完成项总表 与 AGENTS.md §4.1 自查清单逐行对齐。

前身是连改六版才改对的判据脚本（check_alignment.py），2026-10-09 收编为本测试：
结论从「打印后由人读」升级为「断言即前置条件」—— 只观察不阻断的检查等于没有检查（§7.1）。

六版教训里不可丢的四条（每条都真实踩过）：
  1. 两侧与【针】都必须先归一化（剥 `**`/反引号/`~~`、去空白）—— 否则表里整批项报「不在」。
  2. 搜整行（行名+优先级+状态+正文）而不是只搜行名 —— key 写在别的格时只搜行名必然报「表里没有」。
  3. PAIRS 是【手维护的常量】，不是量出来的：台账行数一变（新增/闭合/删除）必须人工同步。
     闭合只加 ~~，闭合行【不需要】PAIRS 条目；已闭合项的条目随之移出，历史见总表 ~~…~~ 行。
  4. 必须反向查「表里有、但没有任何 PAIRS 键覆盖的行」—— 只遍历 PAIRS 的循环从不检查表里
     每一行，新增一行会静默逃出比对。⚠️ 原脚本这一检查写成了恒真（行名集合与它自己比，
     2026-10-09 收编时按其注释的本意修正：每一行都要被至少一个 key 覆盖）。

key 的取法（三条，违反即断）：取两侧都出现的连续字面片段；⛔ 不自己造短键；
⛔ 不带会变的数（行数/字节数/计数值是数据不是身份，换个数字就断）。

已知边界（刻意不修）：解析用 strip("|") ⇒ 看不见「行首缺 |」这类结构坏掉 ——
表格形状是写入时的关切，判据只管内容对齐；格数 != 4 的未完行由
test_no_row_is_silently_dropped_by_cell_count 报出（已闭合的畸形行无害，刻意不报）。

文件定位：从本测试文件出发取仓库根 —— 原脚本按 cwd 读两个文件，换个目录跑就读到
不存在的文件，收编时改为 cwd 无关。
"""
import io
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS_PATH = REPO_ROOT / "tasks.md"
AGENTS_PATH = REPO_ROOT / "AGENTS.md"

TABLE_HEADER_PREFIX = "## ⛔ 未完成项总表"
AGENTS_TAIL_MARKER = "台账里已有的待办要用这条重新过一遍"

# 「第二样东西」= 认领人 / 日期 / 时点 / 明确写「无人认领」/ 明确写「无排期」
POINTER = re.compile(r"(认领人|20\d\d-\d\d-\d\d|时点|排期|无人认领|无认领人|无排期|随症状)")
# 两种都合法，⛔ 不能只认「下一步」（否则「代价已知且刻意不做」那类会被误判成没计划）
NEXT_STEP = re.compile(r"(下一步|必填件|待查证据|第一步)")
EXPLICIT_NO_PLAN = re.compile(r"(代价已知|⛔ 刻意不做|刻意不做)")


def normalize(text):
    """剥 markdown 装饰与空白：反引号、星号、删除线、连续空白一律压平。"""
    cleaned = text.replace("`", "").replace("*", "")
    cleaned = cleaned.replace("~~", "").replace("**", "")
    return re.sub(r"\s+", "", cleaned)


PAIRS = [
    # ⚠️ 21 条 = 2026-10-28 定期回看（dsh-im / dsh-im-connect / hermes-agent / dsh-im-gateway
    #   四基线重钉）把「参考项目定期回看」那行的回看增量并回第 4 格之后的行数。
    #   ⚠️ 前史：23 条 = 2026-10-16 补录（`2a940b1` 收录 dsh-im-connect + `cc4b9d6` 台账同步）后；
    #   其中「长输入回执」`3588a6c`、「占位消息飞行中的窄窗」`68d74df` 两条已闭合 ⇒ 条目移出。

    #   本轮补录的三条键见列表末尾 —— 成因是「本轮往总表加行时漏同步本常量」，
    #   ⚠️ 该遗漏在 `cc4b9d6` 提交时就已存在、且当时**没有任何东西会报警**（守门只被这两条用例拦，
    #   而它们当时没被跑）⇒ 2026-10-16 由 A2 落地前的守门复跑抓出，单开一个提交补齐。
    #   前史：20 条 = 2026-10-11 telegram callback 文案零钉闭合（`0e3794c`）后
    #   （此前 21 条 = D3 首个平台 Slack 落地后：D3 行闭合、D3 剩余平台行新开，两抵）；
    #   台账行数一变必须人工同步（docstring 第 3 条）。
    # ⚠️ key 逐字取自表侧行名/正文里一段【两侧都出现】的连续字面，⛔ 不自己造短键 ——
    #   两侧都会被 normalize（剥 `**`/反引号、去空白），两边写法归一化后相同即可。
    # ⚠️ key 含 `【…】` 时那是行名的一部分 —— ⛔ 不要因为「括号看着像装饰」改成裸词。
    # D3 交互按钮 2026-10-11 已闭合（Slack 落地 `432720e`，证据见总表 ~~D3~~ 行）
    #   ⇒ 原条目移出，由「D3 剩余平台」新行接续。
    ("D3 剩余平台的交互按钮", "D3剩余平台的交互按钮"),
    ("`_enqueue` 的早退与 `flush_queue` 是同型的丢唤醒面 —— 当时恰好无害",
     "_enqueue 的早退"),
    # 2026-10-07 同族面普查新增两条（flush_queue 丢唤醒闭合后的余波）：
    ("`_drain` 的异常分支让队列剩余消息滞留 RAM（`_drain` 正在抛异常时把队列丢在 RAM 里）",
     "`_drain` 的异常分支让队列剩余消息滞留 RAM"),
    ("`_flush_requested` 在 `_drain` 的另两条退出路不清标记 ⇒ 残留标记让下一次 409 白白多试一次",
     "`_flush_requested` 在 `_drain` 的另两条退出路不清标记"),
    # 2026-10-07 outbound 审计结论新增三条（零 .py 编辑，实测数字见 tasks.md）：
    ("`outbound` 的失败可见性挂在【可被装配关掉的那个通道】上",
     "`outbound` 的失败可见性挂在"),
    ("`discord.py` 的 429 单次重试用裸 `time.sleep` ⇒ `stop()` 打不断 ⇒ 一次关停最坏被拖住 60 秒",
     "的 429 单次重试用裸"),
    # email 主题 2026-10-06 已决「不算隐私」⇒ 已不是未完成项，⛔ 不再进「应列」。
    ("G1 桥每 5~7 分钟退出", "G1桥每5~7分钟退出"),
    ("测试侧 §5 触发线标定", "测试侧§5触发线标定"),
    # ⚠️ 键里【不许带行数】：行数是【数据】不是【身份】—— 换个数字就断，
    #   而断的时候报「匹配器或内容要再查」，极难定位。
    ("生产侧 §5 拆分 N 个文件", "生产侧§5拆分"),
    # G2 崩溃窗口残留 2026-10-10 已闭合（28b1343，快照+重灌）⇒ 条目随之移出。
    ("文档里的「平台数」这个易变数字", "文档里的「平台数」"),
    ("文档-代码交叉核对无对口护栏", "文档-代码交叉核对无对口护栏"),
    # telegram callback 文案零钉 2026-10-11 已闭合（`0e3794c`，渲染层逐字 +
    #   白名单对照臂 + 源码锚点三层守门，反向证明 2/2）⇒ 条目随之移出，
    #   历史见总表 ~~…~~ 闭合行。
    # ⚠️ key 必须取【两侧都出现】的片段：表里写「出站失败段的【同型】缺陷」、
    #   清单里写「出站失败段的同型缺陷」—— 差一方括号就必然一边报缺（该行已闭合，教训仍在）。
    # 长输入回执（ora-14 查出的入站投递缺陷）2026-10-26 已闭合（`3588a6c`，回执移进去重闸内）
    #   ⇒ 条目随之移出，历史见总表 ~~…~~ 闭合行。
    # 占位消息飞行中的窄窗（孤儿气泡）2026-10-27 已闭合（`68d74df`，方案 B：pending 集 + 锁外真句柄取消）
    #   ⇒ 条目随之移出，历史见总表 ~~…~~ 闭合行。

    # 2026-10-08 telegram 适配器审计新增八条，2026-10-09 已闭合四条（PAIRS 条目随之移出：
    # answerCallbackQuery / channel_post（守门 = tests/test_telegram_answer_not_ok_warning.py 与
    # tests/test_telegram_non_message_shape_info_log.py）· callback_data `912dbb1` ·
    # poll_timeout `cd295d0`，证据见总表 ~~…~~ 闭合行）：
    ("telegram 的 `_last_send` 【永不淘汰】⇒ 纯内存、无界增长",
     "`_last_send` 【永不淘汰】"),
    ("`_advance_offset` 对畸形 `update_id` 【静默 return】⇒ 该条 offset 不推进 ⇒ 会被服务端重发",
     "`_advance_offset` 对畸形 `update_id` 【静默 return】"),
    ("`_flush_history_once` 【永久丢弃积压历史】（设计如此）⇒ 停机期间的消息恢复后永远收不到",
     "`_flush_history_once` 【永久丢弃积压历史】"),
    ("平台 `description` 是【逐字信道且会落盘】（管道已实测，含 token 未测到）",
     "平台 `description` 是【逐字信道且会落盘】"),
    # 散文里有、总表外 ⇒ 2026-10-06 补录：
    # A3 inbound-push 2026-10-11 已闭合（基建 b717137 + sms 适配器本体 b4b5eaa，
    # 反向证明 3/3、SHA256 对基线）⇒ 条目随之移出，历史见总表 ~~A3~~ 行。
    ("__main__.py 该拆", "该拆"),
    ("测试侧 §5 分布测量", "循环依赖"),
    ("提交消息的替换字符 U+FFFD 的前向闸门", "提交消息的替换字符"),
    # 2026-10-16 补录三条（`2a940b1` + `cc4b9d6` 往总表加行时漏同步本常量，
    #   由 A2 落地前的守门复跑抓出）。三条键都取【两侧都出现】的连续字面，
    #   归一化后各自唯一 —— ⛔ 前两条不能合并成一个裸「借鉴面落地（」：
    #   那会让 dsh-im 与 dsh-im-connect 两行互相顶替，一行covering另一行的缺口。
    ("dsh-im 借鉴面落地（①②③）", "dsh-im 借鉴面落地（"),
    ("dsh-im-connect 借鉴面落地（A/B/C/D）", "dsh-im-connect 借鉴面落地（"),
    ("参考项目定期回看", "参考项目定期回看"),
]


def _split_cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _looks_like_header_or_rule(cells):
    return cells[0] == "项" or set(cells[0]) <= set("-: ")


def _row_is_finished(cells):
    return cells[0].startswith("~~") or "✅" in cells[2]


def _malformed_row_is_already_closed(cells):
    return cells[0].startswith("~~") or any("✅" in cell for cell in cells[2:])


def _iter_table_section_lines(lines):
    # ⛔ 不要固定行数切片：这段被加长过两次，固定切片会静默截断 ⇒ 又是「探针坏了」。
    header_index = next(i for i, line in enumerate(lines)
                        if line.startswith(TABLE_HEADER_PREFIX))
    for offset, line in enumerate(lines[header_index:]):
        if offset > 0 and line.startswith("## "):
            break
        yield line


def _row_full_text(row):
    """整行拼接后归一化 —— key 落在任意一格都能命中（第六版教训）。"""
    return normalize(row["name"] + "||" + row["priority"] + "||"
                     + row["kind"] + "||" + row["next_step"])


def load_ledger_rows():
    """读 tasks.md 总表 → (全部数据行, 被静默丢掉的未完行)。"""
    with io.open(TASKS_PATH, encoding="utf-8") as ledger_file:
        lines = ledger_file.read().splitlines()
    rows = []
    silently_dropped = []
    for line in _iter_table_section_lines(lines):
        if not line.startswith("|"):
            continue
        cells = _split_cells(line)
        if len(cells) != 4 or _looks_like_header_or_rule(cells):
            # ⚠️ 只报【未完】的被丢行：已闭合的畸形行本来就被 ~~ 过滤排除，从来无害。
            if (len(cells) != 4 and set(cells[0]) - set("-: ")
                    and not _malformed_row_is_already_closed(cells)):
                silently_dropped.append((len(cells), cells[0][:36]))
            continue
        rows.append({"name": cells[0], "priority": cells[1], "kind": cells[2],
                     "next_step": cells[3],
                     "finished": _row_is_finished(cells)})
    return rows, silently_dropped


def load_agents_tail_normalized():
    with io.open(AGENTS_PATH, encoding="utf-8") as agents_file:
        lines = agents_file.read().splitlines()
    tail_start = next(i for i, line in enumerate(lines)
                      if AGENTS_TAIL_MARKER in line)
    tail_end = next((i for i in range(tail_start + 1, len(lines))
                     if lines[i].startswith("## ")), len(lines))
    return normalize("\n".join(lines[tail_start:tail_end]))


class TestLedgerPlanCompleteness(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.all_rows, cls.silently_dropped = load_ledger_rows()
        cls.open_rows = [row for row in cls.all_rows if not row["finished"]]
        cls.agents_tail = load_agents_tail_normalized()

        cls.pairs_missing_from_agents_list = []  # 键在表里能对上、AGENTS 清单缺
        cls.pairs_missing_from_table = []         # 清单里有、表里对不上（或两侧都缺）
        for full_name, key in PAIRS:
            key = normalize(key)
            in_table = any(key in _row_full_text(row) for row in cls.open_rows)
            in_list = key in cls.agents_tail
            if in_table and in_list:
                continue
            if in_table:
                cls.pairs_missing_from_agents_list.append(full_name)
            elif in_list:
                cls.pairs_missing_from_table.append(
                    full_name + "（清单里有、表里没有或已闭合）")
            else:
                cls.pairs_missing_from_table.append(
                    full_name + "（两侧都没有 —— 匹配器或内容要再查）")

        # 第四类（原脚本写成恒真，按其注释本意实现）：
        # 表里的每一行都必须被至少一个 PAIRS 键覆盖，否则它静默逃出上面的比对。
        cls.rows_without_pair_key = [
            row["name"] for row in cls.open_rows
            if not any(normalize(key) in _row_full_text(row)
                       for _full, key in PAIRS)]

        # 第五类：优先级格必须指向一个具体的人或一个具体的日期。
        cls.rows_without_owner_or_date = [
            row["name"] for row in cls.open_rows
            if not POINTER.search(row["priority"])]

        # 第六类：每一行都要有「下一步」或「显式无计划」—— 状态格与正文格都要搜，
        # ⛔ 只搜其中一格会漏。
        cls.rows_without_plan = []
        for row in cls.open_rows:
            plan_blob = row["kind"] + "\n" + row["next_step"]
            if not (NEXT_STEP.search(plan_blob)
                    or EXPLICIT_NO_PLAN.search(plan_blob)):
                cls.rows_without_plan.append(row["name"])

    def test_open_row_count_matches_hand_maintained_pairs(self):
        self.assertEqual(
            len(self.open_rows), len(PAIRS),
            "表侧未完成 %d 行 ≠ PAIRS %d 条 —— ⚠️ PAIRS 是手维护常量："
            "台账新增/闭合/删除后必须人工同步本文件"
            % (len(self.open_rows), len(PAIRS)))

    def test_pair_keys_appear_on_both_sides(self):
        self.assertEqual(
            self.pairs_missing_from_agents_list, [],
            "这些 PAIRS 键在表里能对上、但 AGENTS §4.1 清单缺 —— 两侧必须同现")
        self.assertEqual(
            self.pairs_missing_from_table, [],
            "这些 PAIRS 键在 AGENTS 清单里有、但表里对不上（或两侧都缺）—— "
            "先怀疑匹配器（key 太窄/带了会变的数），再怀疑内容")

    def test_every_open_row_is_covered_by_a_pair_key(self):
        self.assertEqual(
            self.rows_without_pair_key, [],
            "表里这些未完成行没有任何 PAIRS 键覆盖（会静默逃出比对）—— "
            "必须给 PAIRS 加一条，或把它写成 ~~…~~（已闭合）")

    def test_no_row_is_silently_dropped_by_cell_count(self):
        self.assertEqual(
            self.silently_dropped, [],
            "有未完行被静默丢掉（格数 != 4 ⇒ 从未进入任何统计）—— "
            "最常见成因：单元格里写了 markdown 转义的 \\| ⇒ 按字面 | 切出多余的格；"
            "⛔ 不要去查「两侧不同步」，症状指向那边，真因在这行自己")

    def test_priority_cells_name_a_person_or_date(self):
        self.assertEqual(
            self.rows_without_owner_or_date, [],
            "优先级格没有指向具体的人或日期的行（§4.1：那一格非空不构成声明）—— "
            "修法：写认领人或时点；⛔ 不许用「谁做？/何时做？」那种提问代替")

    def test_every_open_row_has_next_step_or_explicit_no_plan(self):
        self.assertEqual(
            self.rows_without_plan, [],
            "既没有下一步、也没有显式无计划的行（§4.1：非空的分类格不构成计划）—— "
            "修法：写下一步（谁·做什么·量到再定）或显式无计划 + 代价")


if __name__ == "__main__":
    unittest.main()
