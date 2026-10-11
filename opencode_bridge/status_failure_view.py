"""运行期失败记录视图（从 ``__main__.py`` 逐字移植）。

渲染 ``--status`` 的两段运行期记录：出站失败
（:func:`_print_last_outbound_failures`）与凭据失效
（:func:`_print_last_runtime_credential_failures`），连同各自的「无记录」
逐字文案与段落标题。只读 ``health`` / ``credential_health`` 落盘记录并
``print``，不触任何装配。

Provenance: 2026-10-12 §5 ``__main__.py`` 专轮（装配 vs 视图渲染轴向，exp-68 裁定）。逐字移植 + ``__main__`` 顶部再进口，行为零变化。
"""
from __future__ import annotations

import time

from . import credential_health
from . import health
from .text_width import _dwidth, _pad


#: 「这个平台没有出站失败记录」时 ``--status`` 那一段逐行显示的**逐字**文案。
#: 取 :data:`opencode_bridge.health.NO_OUTBOUND_FAILURE_TEXT` 而不是在这里另写一份：
#: 那份是 :func:`opencode_bridge.health.describe_outbound_failure` 也会用到的文案，
#: 两处各写一次就会漂 —— 而"没有记录"这句话一旦漂成"正常"，就是本任务要消灭的
#: 那类假话。
NO_OUTBOUND_FAILURE_TEXT = health.NO_OUTBOUND_FAILURE_TEXT

_OUTBOUND_SECTION_HEADER = "== 上次出站失败（运行期记录·变异） =="


def _print_last_outbound_failures(
    rows: list[tuple[str, str, bool, bool, dict]],
    bridge_dir: str,
) -> None:
    """打印「上一次出站发送失败是什么、为什么」。

    ⚠️ 这一段与上面两段**回答的都不是同一个问题**，所以必须分开说：

    * 「渠道配置与能力」答"凭据齐不齐"（纯本地可判）；
    * 「上次启动时的探测结论」答"上次**启动那一刻**平台**认不认**这个凭据"；
    * **这一段**答"**上一次**发送失败是什么时候、为什么" —— 那可能是启动之后
      十分钟才发生的（SMTP 密码被改、对端限流、bot 被踢、机器人退群）。

    它们的失效方式不同：凭据齐、平台认，而**发出去那一步**仍然失败时，前两段
    全都是绿的 —— 那正是本段存在的理由。

    ⚠️ **纯本地读盘**：与其它段一样不联网、不构造适配器（``--status`` 必须是
    "网络坏了也能看"的那条路）。

    ⚠️ **措辞必须带时效，而且不许把一条旧记录说成"现在坏了"**：
    * 有记录、``recovered_at`` **缺失** ⇒「此后**没有观测到**成功」。⚠️ 这里
      **不能**说"仍在失败" —— 盘上分不出"还在失败"与"没人再发消息"（见
      AGENTS.md §8：靠猜的方案必然在某些输入上错）。
    * 有记录、``recovered_at`` **有值** ⇒ 明确说"已恢复"，**并且**说明那次失败的
      答复**不会补发**（平台没有"重投"原语）—— 不说这句，用户会以为恢复之后
      那条消息就补上了。
    * 无记录 ⇒ :data:`NO_OUTBOUND_FAILURE_TEXT`，⛔ 既不显示成"正常"、也不显示成
      "失败"。

    ⚠️⚠️ **本段必须能看见盘上**任何**出站失败记录 —— 包括未注册键的**
    （实测缺陷，2026-10-06，与 :func:`_print_last_start_probes` **同一个病根**）。
    用户把 ``telegram`` 拼成 ``telegramm`` 时，那次失败的观测是以**那个键**落盘的，
    而上面那张「渠道配置与能力」表**只列注册表里的平台**、压根没有它那一行 ⇒ 而
    :data:`NO_OUTBOUND_FAILURE_TEXT` 与下面那三句免责**全都以"这一段列出来的键"**
    为前提 ⇒ 不并进来的话，那条记录连同原因一个字都不显示，用户看到的却是
    「无记录」或一句**假的**「盘上有记录，但没有属于这些平台的」。

    :param rows: :func:`_channel_config_rows` 的行，**复用**它（平台清单只有一份）。
    :param bridge_dir: :func:`_bridge_dir` 的推导结果 —— 记录就落在那里。
    """
    record = health.read_outbound_failures(bridge_dir)
    recorded = health.outbound_failure_recorded_at(record)
    recorded_platform_keys = tuple(health.outbound_failures_in_record(record))
    # 与 :func:`_print_last_start_probes` 同一个取舍：只列**已配置**或**盘上有记录**
    # 的平台。没配的平台既不该发过消息，也就不该有出站失败记录。
    recorded_platforms = set(recorded_platform_keys)
    #: 注册表里那些键（``rows`` 是**唯一**的平台清单，这里不重算一份）。
    registered_keys = {key for key, _label, _cfg, _inbound, _caps in rows}
    listed = [
        (key, label)
        for key, label, configured, _inbound_ready, _caps in rows
        if configured or key in recorded_platforms
    ]
    # ⚠️⚠️ **盘上的键不限于已注册的平台名**（实测缺陷，2026-10-06）：用户把平台键拼错
    # （``telegram`` → ``telegramm``）时，那次失败的观测是以**那个键**落盘的
    # （见 :meth:`health.OutboundFailureRecorder.note_failure`），而 ``rows`` **只含
    # 注册表里的平台名** ⇒ 不并进来的话，那条记录连同它的原因**一个字都不显示**。
    # ⇒ 与 :func:`_print_last_start_probes` 是**同一个病根、同一处修法**（那段是启动期
    # 探测结论，这一段是运行期出站失败；它们读的是两份不同的文件）。
    # label 取**键本身**：没有适配器类可以问 label，而用户要认的恰恰是自己敲错的那串。
    #
    # ⚠️ **下面那两套「已恢复 / 没有观测到成功」的措辞对未注册键同样有意义**：
    # ``recovered_at`` 落在**每个平台条目自己**身上（读侧
    # :func:`health.outbound_failure_from_record` 按同一个键取），而"平台注册没注册"
    # 与"发送后来成功没有"是**两根正交的轴** ⇒ ⛔ 不许出现"只有已注册键才走得到"的那
    # 种分支（那会让一个拼错的键**静默地**拿不到恢复状态）。
    listed += [
        (key, key) for key in recorded_platform_keys if key not in registered_keys
    ]
    print("")
    print(_OUTBOUND_SECTION_HEADER)
    print("  说明：以下是**运行期间**观测到的出站发送失败（落盘在")
    print("        outbound-failures.json；与上面那份 platform-health.json **是两份**")
    print("        记录，**各答各的**：那一份答「上次启动那一刻平台认不认凭据」，")
    print("        这一份答「上一次发送失败是什么时候、为什么」）。")
    print("        ⚠ 时间戳说的是**那一刻**发生的事，**不是**现在的连接状态。")
    print("        ⚠ 「没有观测到成功」≠「现在还坏着」：也可能是压根没人再发消息。")
    print("        ⚠ 「已恢复」指的是**发送又成功了**，而失败那次的答复")
    print("          **不会补发** —— 平台没有「重投」这个原语。")
    if recorded is not None:
        print(
            "  记录时间 : "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(recorded))
            + "（这份文件最后一次被写入的时刻，不是失败发生的时刻）"
        )
    if not listed:
        # ⛔ 「盘上有记录，但没有属于这些平台的」这一句**只有真的没有任何记录时才为真**
        # （实测缺陷，2026-10-06）。⚠️ 旧判据是 ``listed`` 为空 ⇒ 走这一支，而那两件事
        # 在缺陷现场**恰好相反**：文件存在、键未注册（``telegramm``）⇒ ``listed`` 空 ⇒
        # 输出「盘上有记录，但没有属于这些平台的」—— **而盘上确实有一条**，它是唯一能
        # 解释「为什么没看到失败原因」的线索，这句话却让用户以为那些记录与自己无关。
        # ⇒ 改问 ``recorded_platform_keys``（记录自己说了什么），而不是 ``listed``
        # （它混进了"这个平台配没配"这一层与记录无关的过滤）。
        # 上面 ``listed`` 已并入未注册的键，所以这一支**只在盘上真的没有任何记录时**
        # 才走得到 ⇒ 那句话从一句**断言**变成了一个**事实**。
        if not recorded_platform_keys:
            print(
                "  （没有已配置的平台，也没有任何出站失败记录）" if record is None
                else "  （盘上有记录，但没有属于这些平台的）"
            )
        return
    name_w = max(_dwidth("平台"), max(_dwidth(label) for label, _f in listed)) + 2
    for key, label in listed:
        failure = health.outbound_failure_from_record(record, key)
        if failure is None:
            print("  " + _pad(label, name_w) + NO_OUTBOUND_FAILURE_TEXT)
            continue
        line = "上次出站失败 " + health.describe_outbound_failure(failure)
        at = failure.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            line += "（%s）" % time.strftime("%m-%d %H:%M:%S", time.localtime(at))
        else:
            # ⛔ **不许整段沉默** —— 沉默恰好是那个歧义点：读者分不出「这条记录没有
            # 失败时刻」与「这个视图压根不显示时刻」，而后者是假的（本分支确实会显示）。
            # ⚠️ 可达而**不是假想场景**：`outbound_failure_from_record` 那段 docstring
            # 明说那份文件**可能被用户手改过**，而读路径在 ``at`` 解析不出来时把它置成
            # ``None``（⛔ 它不补 `time.time()` —— 那会把"读的那一刻"当成失败时刻打出来；
            # ⛔ 也不 ``pop`` 掉那个键 —— 键恒在、值可空，仓库外的消费者按
            # ``entry["at"]`` 取，缺键会 KeyError）。
            # ⇒ 显式说「未记录失败时刻」：缺信息要说出来，不能靠不显示蒙混过去。
            # ⚠️ 措辞里**必须限定「失败」二字**（⛔ 不是笼统的「时刻未知」）：这一行
            # 里紧挨着**另一个**真实时刻（` · 已恢复于 <…>`），而「时刻未知」会被
            # 粗扫的人读成「这一整行的时间信息都未知」⇒ 歧义的代价高于啰嗦。
            # ⚠️ 「未记录失败时刻」**只在这一处**出现；有真实时刻时仍打真实时刻
            # （那一支在上面），⛔ 不许让占位盖过真的时刻。
            line += "（未记录失败时刻）"
        recovered_at = failure.get("recovered_at")
        if isinstance(recovered_at, (int, float)) and not isinstance(recovered_at, bool):
            line += " · 已恢复于 %s" % time.strftime(
                "%m-%d %H:%M:%S", time.localtime(recovered_at)
            )
        else:
            line += " · 此后没有观测到成功"
        print("  " + _pad(label, name_w) + line)


#: ``--status`` 凭据失效段的「无记录」逐字文案：⛔ 不在这里另写一份 ——
#: 取 :data:`credential_health.NO_CREDENTIAL_FAILURE_TEXT`（那份同时是
#: :func:`credential_health.describe_credential_failure` 的兜底文案），两处各写
#: 一次就会漂，而「无记录」一旦漂成「正常」就是本任务要消灭的那类假话。
NO_CREDENTIAL_FAILURE_TEXT = credential_health.NO_CREDENTIAL_FAILURE_TEXT

_CREDENTIAL_FAILURE_SECTION_HEADER = "== 上次凭据失效（运行期记录） =="


def _print_last_runtime_credential_failures(
    rows: list[tuple[str, str, bool, bool, dict]],
    bridge_dir: str,
) -> None:
    """打印「启动之后凭据有没有再失效过」—— 与「上次启动时的探测结论」**分开答**。

    ⚠️ 这一段回答的问题上面两段**都答不了**：

    * 「渠道配置与能力」答"凭据齐不齐"（纯本地可判）；
    * 「上次启动时的探测结论」答"**上次启动那一刻**平台认不认这个凭据"
      （platform-health.json，``start()`` 返回那一刻定稿、之后不再改）；
    * **这一段**答"**启动之后**凭据又失效过没有"（credential-failures.json，
      由轮询线程在观测到 401 的那一刻写）⇒ **启动失败与运行期失效是两个不同的
      键、两份不同的文件**，谁也不许替谁说话。启动时是好的、运行中被吊销 ⇒
      上面那段继续说"上次启动 ok"、这一段说出事实 —— 那正是**区分**要落地的地方。

    ⚠️ 纯本地读盘：与其它段一样不联网、不构造适配器（``--status`` 必须是
    "网络坏了也能看"的那条路）。

    ⚠️ 措辞纪律与出站失败段同源：

    * 有记录、``recovered_at`` 缺失 ⇒「此后**没有观测到** getMe 通过」。
      ⛔ 不说"仍在失效"：盘上分不出"还坏着"与"重探已超限、不再有观测"
      （:data:`opencode_bridge.adapters.telegram.RUNTIME_CREDENTIAL_REVERIFY_LIMIT`
      —— 探满之后进程会**主动停止**重探，那之后没有新观测是**设计**，不是健康）。
    * 有记录、``recovered_at`` 有值 ⇒「已恢复于 …」。
    * 无记录 ⇒ :data:`NO_CREDENTIAL_FAILURE_TEXT`，⛔ 既不说"正常"也不说"失效"。

    ⚠️ **盘上任何键都要能看见**（与 :func:`_print_last_outbound_failures` 同一处
    2026-10-06 的病根与修法）：键未注册（用户把 ``telegram`` 拼成 ``telegramm``）
    时照样成行 —— 记录自己说了什么，就是这一段唯一的输入。

    :param rows: :func:`_channel_config_rows` 的行，**复用**它（平台清单只有一份）。
    :param bridge_dir: :func:`_bridge_dir` 的推导结果 —— 记录就落在那里。
    """
    record = credential_health.read_credential_failures(bridge_dir)
    recorded = credential_health.credential_failure_recorded_at(record)
    recorded_platform_keys = tuple(
        credential_health.credential_failures_in_record(record)
    )
    recorded_platforms = set(recorded_platform_keys)
    registered_keys = {key for key, _label, _cfg, _inbound, _caps in rows}
    listed = [
        (key, label)
        for key, label, configured, _inbound_ready, _caps in rows
        if configured or key in recorded_platforms
    ]
    # 未注册键并入（label 取键本身）：与出站失败段同一处修法 —— 拼错的键是
    # 唯一能解释「为什么没看到失效」的线索，⛔ 不许被「已配置与否」这层过滤吞掉。
    listed += [
        (key, key) for key in recorded_platform_keys if key not in registered_keys
    ]
    print("")
    print(_CREDENTIAL_FAILURE_SECTION_HEADER)
    print("  说明：以下是**运行期间**观测到的凭据失效（token 被吊销/过期等；落盘在")
    print("        credential-failures.json）。与「上次启动时的探测结论」**是两份")
    print("        记录、各答各的**：那一份答「上次**启动那一刻**平台认不认凭据」，")
    print("        这一份答「**启动之后**凭据有没有再失效过」⇒ 启动时是好的、")
    print("        运行中被吊销，两段会分别说，谁也不盖住谁。")
    print("        ⚠ 时间戳说的是**那一刻**发生的事，**不是**现在的连接状态。")
    print("        ⚠ 「此后没有观测到 getMe 通过」≠「现在仍坏着」：重探有次数上界，")
    print("          探满停止后不再有新观测，那是设计，不是健康。")
    if recorded is not None:
        print(
            "  记录时间 : "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(recorded))
            + "（这份文件最后一次被写入的时刻，不是失效发生的时刻）"
        )
    if not listed:
        # 与出站失败段同一条判据：这一支**只在盘上真的没有任何记录时**才走得到
        # （上面 ``listed`` 已并入未注册的键）⇒ 那句话是事实，不是断言。
        if not recorded_platform_keys:
            print(
                "  （没有已配置的平台，也没有任何凭据失效记录）" if record is None
                else "  （盘上有记录，但没有属于这些平台的）"
            )
        return
    name_w = max(_dwidth("平台"), max(_dwidth(label) for label, _f in listed)) + 2
    for key, label in listed:
        failure = credential_health.credential_failure_from_record(record, key)
        if failure is None:
            print("  " + _pad(label, name_w) + NO_CREDENTIAL_FAILURE_TEXT)
            continue
        line = "运行期失效 " + credential_health.describe_credential_failure(failure)
        at = failure.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            line += "（%s）" % time.strftime("%m-%d %H:%M:%S", time.localtime(at))
        else:
            # ⛔ 不许整段沉默（与出站段同一条）：缺信息要说出来，不能靠不显示蒙混。
            line += "（未记录失效时刻）"
        recovered_at = failure.get("recovered_at")
        if isinstance(recovered_at, (int, float)) and not isinstance(recovered_at, bool):
            line += " · 已恢复于 %s" % time.strftime(
                "%m-%d %H:%M:%S", time.localtime(recovered_at)
            )
        else:
            line += " · 此后没有观测到 getMe 通过"
        print("  " + _pad(label, name_w) + line)
