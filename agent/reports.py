"""管理助手输出渲染（20260921）——**报表与后台留言名册的纯函数出口**。

## 为什么数字在工具侧算好，而不是把 JSON 丢给模型数

全仓既有惯例是工具返回 `_shape(data)`（把接口数据原样给 narrator），但报表类
**刻意偏离**：LLM 数数是幻觉的高发区（"待审 3 条"里数错一条，用户没有任何办法
发现），而这几张报表的价值恰恰在于数字可信。所以工具把计数、百分比、
排序、截断全做完，返回**确定性的中文报表文本**，narrator 只负责转述。
代价是这几个工具的输出不像别的工具那样"可再加工"——这个取舍是有意的。

## 不受信文本（重要）

`get_moderation_status` 会把**访客写的河灯留言**带进工具帧：那是全链路上第一处
"攻击者可控的文本进入 prompt"。本轮工具全只读，注入最坏也只是答错；但有一条
副作用必须在数据进入模型**之前**掐掉——**命令前缀**。前端的 `cleanAgentText` /
`execAgentCommands` 与 agent 的 gate 都按 `EFFECT:/NAVIGATE:/AUTO_NAVIGATE:/…`
前缀识别命令，所以一条内容是 `EFFECT:rain:on` 或
`AUTO_NAVIGATE:/dashboard` 的留言，只要被 narrator 原样复述出去，就可能在
**管理员自己的浏览器**里真的生效（同源路径还能过掉 BLOG_ROUTES 白名单）。
`sanitize_untrusted` 负责这件事，见它的注释与 `tests/test_reports.py` 的回归锁。
"""

from __future__ import annotations

import re
from datetime import datetime

from agent import hostinfo as H

# 报表总长上限（进 prompt 的文本，别让一张报表把上下文挤掉）
MAX_REPORT_CHARS = 2400

# 模块头注说的那个前缀词表：与前端 `chat-core.js` 的 `COMMAND_RE` 同源同序。
# `[A-Za-z0-9_]*EFFECT` 是为了覆盖 `SNOW_EFFECT:` 这类幻觉变体（前端也这么写）。
_CMD_WORD_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9_]*EFFECT|DARKMODE|NAVIGATE|AUTO_NAVIGATE|SUMMARY|\[?System\]?)"
    r"\s*[:：]")

# 零宽空格（U+200B）：肉眼不可见，但**不是** Unicode 空白（`'​'.isspace()` 为
# False，Python 与 JS 的 `\s` 都不匹配它），所以夹在命令词与冒号之间能让两侧正则
# 全部失配。比"删掉冒号"温和——内容照旧可读、可复制，只是不再是一条命令。
_ZWSP = "\u200b"  # 显式转义：源码里不留不可见字符


def sanitize_untrusted(text: str, limit: int = 40) -> str:
    """访客可控文本 → 可安全放进工具帧的一行。

    三件事：换行折叠（防它自己占一行被行首锚定的正则捞到）、长度截断、
    **打断命令前缀**（在命令词与冒号之间插零宽空格，见 `_ZWSP` 的注释）。

    刻意保留其余原样：这是给人看的举报线索，不是要清洗成安全 HTML。
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    s = _CMD_WORD_RE.sub(lambda m: m.group(0).replace(":", _ZWSP + ":").replace("：", _ZWSP + "："), s)
    if limit > 0 and len(s) > limit:
        s = s[:limit] + "…"
    return s


def _cap(text: str) -> str:
    if len(text) <= MAX_REPORT_CHARS:
        return text
    return text[:MAX_REPORT_CHARS] + "\n…（报表过长，已截断）"


def _ts(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")


# ── 报表 ①：服务器状态 ───────────────────────────────────────────────

def render_server_status(*, cpu_pct: float | None, cores: int,
                         load: tuple[float, float, float] | None,
                         mem: dict, disks: list[dict],
                         uptime_s: float | None,
                         now: datetime | None = None) -> str:
    lines = [f"服务器状态（{_ts(now)}）"]
    if cpu_pct is None:
        lines.append("- CPU：本次采样读不到（/proc/stat 不可读）")
    else:
        lines.append(f"- CPU：{cores} 核，使用率 {cpu_pct}%（0.25 秒采样）")
    if load is None:
        lines.append("- 负载：读不到")
    else:
        # 参考线按核数折算：loadavg 的"1.0"是单核满载，4 核机器上 4.0 才满载
        lines.append(f"- 负载：1 分钟 {load[0]:.2f}／5 分钟 {load[1]:.2f}／15 分钟 {load[2]:.2f}"
                     f"（{cores} 核，满载参考线 {cores}.0）")
    if not mem or not mem.get("total_kb"):
        lines.append("- 内存：读不到（/proc/meminfo 不可读）")
    else:
        lines.append(
            f"- 内存：总 {H.format_kb(mem['total_kb'])}，已用 {H.format_kb(mem['used_kb'])}"
            f"（{mem['pct']}%），可用 {H.format_kb(mem['avail_kb'])}")
        if mem.get("swap_total_kb"):
            lines.append(f"- Swap：总 {H.format_kb(mem['swap_total_kb'])}，"
                         f"已用 {H.format_kb(mem['swap_used_kb'])}（{mem['swap_pct']}%）")
        else:
            lines.append("- Swap：未启用")
    if not disks:
        lines.append("- 磁盘：读不到")
    for d in disks:
        lines.append(f"- 磁盘 {d['path']}：总 {H.format_bytes(d['total'])}，"
                     f"已用 {H.format_bytes(d['used'])}（{d['pct']}%），"
                     f"可用 {H.format_bytes(d['free'])}")
    lines.append(f"- 开机时长：{H.format_uptime(uptime_s)}")
    return _cap("\n".join(lines))


# ── 报表 ②：服务健康 ─────────────────────────────────────────────────

def render_service_health(*, services: list[tuple[str, dict]], health: dict,
                          traces: dict, sizes: list[dict],
                          now: datetime | None = None) -> str:
    lines = [f"服务健康（{_ts(now)}）"]
    for unit, info in services:
        if not info:
            # systemctl 查询失败/单元不存在——这是"读不到"，不是"服务挂了"，措辞要分清
            lines.append(f"- {unit}：读不到状态（systemctl 查询失败或单元不存在）")
            continue
        state = f"{info.get('ActiveState', '?')}/{info.get('SubState', '?')}"
        extra = f"，重启 {info.get('NRestarts', 0)} 次"
        started = _format_started(info.get("ExecMainStartTimestamp", ""))
        if started:
            extra += f"，启动于 {started}"
        lines.append(f"- {unit}：{state}{extra}")

    w = health or {}
    if not w:
        lines.append("- 心跳探针：读不到（logs/health.log 不可读）")
    else:
        span = ""
        if w.get("first") and w.get("last"):
            span = (f"（{w['first'].strftime('%m-%d %H:%M')} ~ "
                    f"{w['last'].strftime('%m-%d %H:%M')}）")
        verdict = "无异常" if not (w.get("warn") or w.get("fail")) else "有异常"
        lines.append(f"- 心跳探针（近 24 小时）：{verdict}——WARN {w.get('warn', 0)} 条、"
                     f"FAIL {w.get('fail', 0)} 条{span}")
        for line in (w.get("recent") or []):
            lines.append(f"  · {_short_health_line(line)}")

    t = traces or {}
    if t.get("readable"):
        lines.append(f"- 今日对话：{t.get('rounds', 0)} 轮；异常收尾 {t.get('abnormal', 0)} 轮；"
                     f"质检拦截 {t.get('fallback', 0)} 轮；"
                     f"shadow 权限拒绝 {t.get('denied', 0)} 次")
    else:
        lines.append("- 今日对话：读不到（trace 目录不可读）")

    if sizes:
        total = sum(s["size"] for s in sizes)
        top = "、".join(f"{s['path']} {H.format_bytes(s['size'])}" for s in sizes[:3])
        lines.append(f"- 存活日志：合计 {H.format_bytes(total)}；最大三份 {top}")
    return _cap("\n".join(lines))


def _format_started(raw: str) -> str:
    """systemd 的 `Sun 2026-09-20 22:00:41 CST` → `09-20 22:00`（认不出就原样）。"""
    if not raw:
        return ""
    m = re.search(r"\d{4}-(\d{2}-(\d{2})) (\d{2}:\d{2})", raw)
    return f"{m.group(1)} {m.group(3)}" if m else raw


def _short_health_line(line: str) -> str:
    """`2026-09-21 00:45:01 WARN 正文` → `09-21 00:45 WARN 正文`（截 80 字）。"""
    if len(line) >= 19 and line[4] == "-" and line[13] == ":":
        return (line[5:16] + " " + line[20:]).strip()[:80]
    return line[:80]


# ── 报表 ③：评论（河灯留言）审核状况 ────────────────────────────────
#
# 口径来自**生产代码**而不是这张报表的想象（20260922 逐条对齐 src/routes/talks.rs
# 的 board_approved）：河灯留言入库时算出一对 (approved, ai_result)——
#   · ai_result=pass   ：AI 判通过 → approved=1（无人工闸）或 0（人工闸开着 ⇒ 仍要人看）
#   · ai_result=reject ：AI 判驳回 → approved=2（直接驳回）或 0（人工闸开着）
#   · ai_result=flag   ：AI 存疑 → 一律 approved=0
#   · ai_result=NULL   ：没走 AI（AI 闸关 / agent 不可用降级）→ 1 或 0
# 人工后续裁决**只写 approved、不改写 ai_result**（`audit_board`），所以
# `ai_result=reject 且 approved=1` 是"AI 驳回但被人改判放行"的实证，不是脏数据。
#
# 上一版把 `reject` 混进了"未审"桶（只认 pass/flag），于是**AI 驳回的那批在报表里
# 看不见**——而"哪些被 AI 驳回"正是主人最常问的一句（20260922 用户点名要读）。
# 现在按 AI 侧四态 × 人工侧三态切分，三份名单各回答一个问题。

APPROVED_PENDING, APPROVED_OK, APPROVED_REJECT = 0, 1, 2

# 明细每条列出多少行：默认报表三类各列几条（其余只计数），
# `status` 聚焦某一类时把这一类放开（见 render_moderation_status）
MAX_LIST_DETAIL = 5
MAX_FOCUS_DETAIL = 20

# 聚焦参数 → (键, 报表里的名字)。键与工具参数同源（tools.base.get_moderation_status）
FOCUS_NAMES = {
    "ai_passed": "AI 直接通过",
    "ai_rejected": "AI 驳回",
    "pending": "需要人工复批",
}


def account_text(r: dict) -> str:
    """后台视图里的**发表账号**：`账号（userId:5／昵称 小猫咪／用户名 sora）`。

    后台留言行（`GET /api/protect/board`）每一行都带 `userId`/`username`/`nickname`
    ——**发留言必须登录**（`talks.rs::insert_talk` 里那道 `current_uid`），所以留空留名
    的"匿名"留言一样溯得到是谁发的。

    为什么要单独有这么一个渲染：公开视图（公开留言列表）里没有这三个字段，它透出的
    `author` 只是留言人**自己在留名框里填的自由文本**——可以填任何字（甚至填别人的
    昵称）、也可以留空。**它不是账号**，拿它当"是谁发的"去认人就是认错人。生产现场
    （trace `20260930T235232`）模型正是拿着留言正文当账号名去后台名录里找，回了
    "后台账号列表里并没有叫「博主是大笨狗」的账号"。

    昵称与用户名都给（主人认人两种叫法都有），两个都空时只剩 userId（**不许留白**：
    留白会让这一行看起来像"没有账号"，那正是公开视图的坑）。
    """
    uid = r.get("userId")
    head = f"userId:{uid}" if uid is not None else "userId 读不到"
    nick = sanitize_untrusted(str(r.get("nickname") or ""), 20)
    uname = sanitize_untrusted(str(r.get("username") or ""), 20)
    if not nick and not uname:
        return f"账号（{head}）"
    if not nick or nick == uname:
        return f"账号（{head}／{uname or nick}）"
    if not uname:
        return f"账号（{head}／昵称 {nick}）"
    return f"账号（{head}／昵称 {nick}／用户名 {uname}）"


def _detail_line(r: dict, extra: str = "") -> str:
    """一条留言的明细行：`talkId:<id> 时间 账号…（标注）「正文」`。

    **id 带命名空间**（20260926 批 4，理由见 tools/base.py::_board_label）：这份明细
    直接进 planner/narrator 的提示词，裸 id 会被当成别的物件。

    **账号与留名分开印**（20261001）：旧版这里是 `author or nickname`——留言时填的
    自由文本会**盖住真实账号**，于是"这条是谁发的"在报表里也没答案（匿名那条更是
    只剩一个 userId）。现在账号一律由 `account_text` 给出，留名只作为**附加的**线索
    跟在后面（备注里写明它是留名框填的字，不是账号）。

    `sanitize_untrusted` 是必须的（见模块头注）：这是全链路唯一"访客可控文本进
    prompt"的地方，命令前缀必须在这里拆掉——留名与正文同样访客可控。
    """
    who = account_text(r)
    sign = sanitize_untrusted(r.get("author") or "", 16)
    if sign:
        who += f"，留名「{sign}」"
    body = sanitize_untrusted(r.get("content") or "", 30)
    at = short_time(r.get("createTime"))
    return f"  · talkId:{r.get('talkKey')} {at} {who}（{extra}）「{body}」"


def _ai_bucket(v) -> str:
    """ai_result → 四态之一（认不出的值按"未审"——与上一版同取向，不新造桶）。"""
    return v if v in ("pass", "reject", "flag") else "none"


def render_moderation_status(rows: list[dict], status: str | None = None,
                             now: datetime | None = None) -> str:
    """`GET /api/protect/board` 的返回 → 审核状况报表。

    先给两张计数（人工侧三态 / AI 侧四态），再列**三份名单**——它们按主人问问题
    的方式切分，因此**口径不同、可以重叠**（一条"AI 驳回且还等人复批"的留言会
    同时出现在②③），报表里把这件事写明，免得被读成加法：

      ① AI 直接通过：`ai_result=pass` 且 `approved=1`（没经过人）
      ② AI 驳回    ：`ai_result=reject`（不论人工后续维持 / 改判 / 还等着）
      ③ 需要人工复批：`approved=0`（不论 AI 判了什么）

    `status` 给其中之一（`FOCUS_NAMES` 的键）时，只有这一类展开明细（放宽到
    `MAX_FOCUS_DETAIL` 条），另两类只留计数——"把被驳回的都列出来"这类追问靠它。
    """
    rows = rows or []
    focus = status if status in FOCUS_NAMES else None
    lines = [f"河灯留言审核状况（{_ts(now)}）"]
    total = len(rows)
    pending = [r for r in rows if r.get("approved") == APPROVED_PENDING]
    passed = [r for r in rows if r.get("approved") == APPROVED_OK]
    rejected = [r for r in rows if r.get("approved") == APPROVED_REJECT]
    lines.append(f"- 总计 {total} 条：待审 {len(pending)}、已通过 {len(passed)}、已驳回 {len(rejected)}")

    ai = {"pass": 0, "reject": 0, "flag": 0, "none": 0}
    for r in rows:
        ai[_ai_bucket(r.get("ai_result"))] += 1
    lines.append(f"- AI 侧判定：通过 {ai['pass']}、驳回 {ai['reject']}、"
                 f"存疑转人工 {ai['flag']}、未走 AI {ai['none']}")

    # 三份名单（口径见 docstring）。`ai_passed` 刻意要求 approved=1：AI 判 pass 但
    # 人工闸开着时那条**并没有直接露出**，它在③里等人工，混进①会让主人以为没人看过。
    ai_passed = [r for r in rows if r.get("ai_result") == "pass" and r.get("approved") == APPROVED_OK]
    ai_rejected = [r for r in rows if r.get("ai_result") == "reject"]
    keep_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_REJECT]
    open_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_OK]
    wait_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_PENDING]
    pend_flag = [r for r in pending if r.get("ai_result") == "flag"]
    pend_pass = [r for r in pending if r.get("ai_result") == "pass"]
    pend_none = [r for r in pending if _ai_bucket(r.get("ai_result")) == "none"]

    lines.append("- 三份名单口径不同、**可以重叠**（一条 AI 驳回又还等人复批的留言会同时"
                 "出现在②③）——不要把三个数相加当总数")
    # 一行图例买断整份报表的歧义（20261001）：明细行受字符预算所限，只在行内重复
    # 「不是账号」划不来；说一次，整份都算数。
    lines.append("- 明细里「账号（…）」是**真实发表账号**（发留言必须登录，留空留名的也溯得到）；"
                 "「留名「…」」是留言时自己填的字，**不是账号**，别拿它去认人")

    def _detail(group: list[dict], key: str, extra_of):
        """一类名单的明细行（空名单不写"明细"二字，免得列出个空标题）。"""
        if not group:
            return
        limit = MAX_FOCUS_DETAIL if focus == key else (0 if focus else MAX_LIST_DETAIL)
        if not limit:
            lines.append(f"  · 另有 {len(group)} 条未列出")
            return
        lines.append("  明细:")
        for r in group[:limit]:
            lines.append(_detail_line(r, extra_of(r)))
        rest = len(group) - limit
        if rest > 0:
            lines.append(f"  · 另有 {rest} 条未列出")

    lines.append(f"① AI 直接通过（AI 判通过且已展示，没经过人工）{len(ai_passed)} 条:")
    _detail(ai_passed, "ai_passed", lambda r: "AI通过")
    lines.append(f"② AI 驳回 {len(ai_rejected)} 条：人工维持驳回 {len(keep_rej)}、"
                 f"人工改判放行 {len(open_rej)}、还等着人工复批 {len(wait_rej)}")
    _detail(ai_rejected, "ai_rejected",
            lambda r: {APPROVED_REJECT: "人工已驳回", APPROVED_OK: "人工已改判放行",
                       APPROVED_PENDING: "仍待人工"}.get(r.get("approved"), "人工侧未知"))
    lines.append(f"③ 需要人工复批 {len(pending)} 条：AI 存疑 {len(pend_flag)}、"
                 f"AI 通过但人工闸 {len(pend_pass)}、AI 驳回但人工闸 {len(wait_rej)}、"
                 f"未走 AI {len(pend_none)}")
    _detail(pending, "pending",
            lambda r: {"flag": "AI存疑", "pass": "AI已过待人工", "reject": "AI驳回待人工"}
            .get(r.get("ai_result"), "AI未审"))

    # 分歧实证：AI 判过但被人驳回（漏放）、AI 判驳但被人放行（误伤）——两条都是
    # 复盘 AI 判定质量的**证据**（只有人工侧真裁过才会有，不含仍待审的）。
    missed = [r for r in rejected if r.get("ai_result") == "pass"]
    lines.append(f"- 分歧实证（人工已经裁过的）：AI 放过但被人驳回 {len(missed)} 条；"
                 f"AI 驳回但被人放行 {len(open_rej)} 条")
    # **处置能力**（20260928）：这张报表是"某条留言现在什么状态"的唯一事实源，但它此前
    # **只报状态、不说能对它做什么**，于是模型只能自己推——推出来的就是那句假话。
    # 事故实证（trace `20260928T032411`）：报表把两条被驳回的留言连 id 与正文一起列了
    # 出来，narrator 却对主人说"系统这边**没有删除被驳回留言的通道**……只能你自己进后台
    # 手动处理"。事实相反：`_board_index` 读的就是**这份**后台清单（`GET /api/protect/board`
    # 不过滤 approved），被驳回的留言既找得到也删得掉（驳回的更该能删）。
    # 修法取"把事实放在模型读得到的地方"，不是加一条禁令：**能力边界是系统数据**，
    # 与 NAV_MAP、站点地图同一类——模型不该靠推理去猜自己有什么权限。
    lines.append(
        "- **处置**：上面列出的每一条（含已驳回、待审的）都有两条通道——"
        "① 复核/改判：人工通过或驳回，**按 `talkId` 指认**（编号就是上面逐条印出来的"
        "那串数字，原样抄即可）；② 删除（**删掉取不回来**，没有回收站），"
        "按**正文原话**指认（把那条里的一小段原样抄给我）。"
        "两件事分属两条通道，指认方式不同、别混用。"
        "**没有哪一类是删不掉的**——后台清单里看得见的都删得掉。"
        "⚠️ 复核是**改判**、不是「走一遍流程」：待审的可通过可驳回，"
        "**已通过的可再驳回**（收回展示）、已驳回的可恢复通过——"
        "任何一条留言的当前结论都能改，改判同样按 `talkId` 指认。")
    if not pending:
        lines.append("- 当前没有待审留言")
    return _cap("\n".join(lines))


# ── 后台留言名册（20261001）─────────────────────────────────────────
#
# **这张名册存在的唯一理由**：公开留言接口看不到**发表账号**。它透出的 `author`
# 是留言时在留名框里自己填的自由文本（可以填任何字、也可以留空，留空就是"匿名"），
# 于是"这条是谁发的"在公开视图里**结构上无解**——而答案一直在后台视图里：
# `GET /api/protect/board` 的每行都带 userId/username/nickname，且**发布强制登录**
# （`talks.rs::insert_talk` 里那道 `current_uid`）——匿名只是没填留名，账号一样留存。
# 生产现场（trace `20260930T235232`）：模型手上只有公开列表，于是回了一句
# "后台账号列表里并没有叫「博主是大笨狗」的账号"——它把**留言正文**当成了账号名。
#
# 与报表③（`render_moderation_status`）的分工：那张按**审核状态**切三份名单、默认
# 每份只印 5 条，回答"积压了多少、哪些被 AI 驳回了"；这一张是**逐条名册**，回答
# "这条是谁发的、最近都说了什么"。两个问题、两张纸，刻意不合并（合并会让两边都
# 变长，而 2400 字的报表上限就摆在那里）。

# 名册的行数上限：与字符预算谁先到算谁（见 render_board_roster 的循环）。
BOARD_ROSTER_LIMIT = 20

# 关键词**筛空**时补印的最近条数（见 render_board_roster "筛空"分支）。
BOARD_ROSTER_FALLBACK = 5


def _roster_line(r: dict) -> str:
    """名册的一行：`talkId:<id> [状态] 时间 账号（…），留名「…」「正文节选」`。

    ⚠️ **账号在留名之前**——顺序就是判据：读的人先看到的是"谁发的"，留名只是附注
    （旧版两类东西挤在一个字段里，自由文本会盖住账号）。
    """
    from tools.base import BOARD_APPROVED_CN        # 一处实现（tools/base.py，写路径同源）
    state = BOARD_APPROVED_CN.get(r.get("approved"), "状态未知")
    sign = sanitize_untrusted(r.get("author") or "", 16)
    body = sanitize_untrusted(r.get("content") or "", 30)
    tail = f"，留名「{sign}」（留言时自己填的，不是账号）" if sign else \
           "，留名未填（匿名发表）"
    return (f"- talkId:{r.get('talkKey')} [{state}] {short_time(r.get('createTime'))} "
            f"{account_text(r)}{tail}「{body}」")


def render_board_roster(rows: list[dict], *, approved: int | None = None,
                        keyword: str | None = None) -> str:
    """`GET /api/protect/board` 的返回 → **逐条名册**（含发表账号，可筛选）。

    `approved` 传 0/1/2 只看某一类审核状态（调用方负责把实参归一成这三个值之一，
    认不出的实参**传 None**——本函数不猜它想筛哪一类）；`keyword` 在正文、留名、
    账号昵称/用户名里做不区分大小写的子串匹配。

    两条纪律：
      · **读不到 ≠ 没有**：`rows` 为空由调用方处理（那是"一条留言都没有"），本函数
        只负责"读到了、按条件筛"——所以筛选结果为空时明说**筛掉了多少**，
        绝不说成"站内没有"（同 gate 洞④ 的供体）。
      · **打印实际生效的条件**：抬头逐字写出这次筛了什么，读的人（与模型）才不会
        把"我筛过的那一类"说成"站里就这些"。
      · **筛空要给出路**：只有关键词把结果筛空时，补印**不带关键词**的最近
        `BOARD_ROSTER_FALLBACK` 条（状态筛选仍生效）并明说"它们不符合上面的关键词"
        ——空手而归的读法只剩"凭旧印象猜"这一条路（见该分支的 trace 注释）。
    """
    rows = rows or []
    kw = (keyword or "").strip()
    hits = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if approved is not None and r.get("approved") != approved:
            continue
        if kw:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("content", "author", "nickname", "username")).lower()
            if kw.lower() not in hay:
                continue
        hits.append(r)

    conds = []
    if approved is not None:
        from tools.base import BOARD_APPROVED_CN
        conds.append(f"状态={BOARD_APPROVED_CN.get(approved, approved)}")
    if kw:
        conds.append(f"关键词「{sanitize_untrusted(kw, 20)}」")
    cond_txt = f"；本次筛选：{'、'.join(conds)}" if conds else "；本次未筛选"

    head = [
        f"河灯留言后台名册（{_ts()}，后台共 {len(rows)} 条{cond_txt}）",
        "- **每条留言都有真实发表账号**：放灯必须登录，留名框里填的字只是自由文本"
        "（留空即匿名）——认人看「账号（…）」，**别拿正文或留名当账号**；"
        "匿名的留言一样溯得到是谁发的。",
    ]
    if not hits:
        head.append(f"- 按上面的条件一条都没匹配上（后台 {len(rows)} 条里筛出来的结果是空的）"
                    "——换个关键词、或不带条件看全部。")
        # 筛空是最危险的中间态：读的人手上什么都没有，只能拿会话历史里的旧印象凑
        # （trace `20261001T005722` 实证：关键词「骂」筛空 → 换词猜「垃圾博客」→
        # 再把另一条留言的**留名**讲成了它的"账号"）。所以这里不只说"没有"——
        # 把**不带关键词**的最近几条补上（状态筛选仍生效），让"换个词"有据可依。
        same_status = [r for r in rows
                       if isinstance(r, dict)
                       and (approved is None or r.get("approved") == approved)]
        if kw and same_status:
            head.append(f"- 关键词是**子串**匹配（不是语义匹配）：上面那几个字在正文/留名/"
                        f"账号里一个都没出现过。下面附上**不带关键词**的最近 "
                        f"{min(len(same_status), BOARD_ROSTER_FALLBACK)} 条"
                        "（**它们不符合上面的关键词**，只是此刻最新的几条，供你判断该换"
                        "哪个词、或直接按内容认人）：")
            for r in same_status[:BOARD_ROSTER_FALLBACK]:
                head.append(_roster_line(r))
            if len(same_status) > BOARD_ROSTER_FALLBACK:
                head.append(f"- 另有 {len(same_status) - BOARD_ROSTER_FALLBACK} 条未列出")
        return _cap("\n".join(head))

    lines = list(head)
    for r in hits[:BOARD_ROSTER_LIMIT]:
        line = _roster_line(r)
        # 字符预算（与条数上限谁先到算谁）：宁可少列几行并如实说"另有 N 条"，也不要
        # 让 _cap 把名册切成半截（半截的名单读起来像"就这些"，而它其实是被截断的）。
        if len("\n".join(lines + [line])) > MAX_REPORT_CHARS - 200:
            break
        lines.append(line)
    rest = len(hits) - (len(lines) - len(head))
    if rest > 0:
        lines.append(f"- 另有 {rest} 条未列出（可用 keyword 收窄，或不筛直接看全部）")
    return _cap("\n".join(lines))


def short_time(raw) -> str:
    """`2026-09-21 12:40:01` → `09-21 12:40`（认不出就空串）。"""
    s = str(raw or "")
    return s[5:16] if len(s) >= 16 else s[:16]


# ── 报表 ④：用户数据 ────────────────────────────────────────────────

def render_user_stats(data: dict, now: datetime | None = None) -> str:
    """`GET /api/protected/stats/users` 的返回 → 用户数据报表。

    ⚠️ **总数/活跃数一律用服务端聚合值，不要拿 `users[]` 重算**：那个列表封顶
    50 行（见 `routes/stats.rs`），重算会把一个 200 人的站报成 50 人。
    """
    data = data or {}
    lines = [f"用户数据报表（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）"]
    roles = "、".join(f"{r.get('role')} {r.get('count')}"
                      for r in (data.get("roleCounts") or [])) or "无"
    lines.append(f"- 用户总数 {data.get('totalUsers', 0)}（{roles}）")
    lines.append(f"- 会话 {data.get('totalConversations', 0)} 个、"
                 f"消息 {data.get('totalMessages', 0)} 条、"
                 f"执行回执 {data.get('totalExecutions', 0)} 条")
    lines.append(f"- 活跃（有会话或消息）：近 7 天 {data.get('activeUsers7d', 0)} 人、"
                 f"近 30 天 {data.get('activeUsers30d', 0)} 人")

    users = data.get("users") or []
    if not users:
        lines.append("- 没有任何用户产生过会话或消息")
    else:
        lines.append(f"- 明细（按消息数倒序，共 {data.get('listedUsers', len(users))} 人，最多 50 行）:")
        for u in users:
            name = sanitize_untrusted(u.get("name") or "", 20) or f"userId:{u.get('id')}"
            last = u.get("lastActiveAt") or "无活动"
            lines.append(f"  · userId:{u.get('id')} {name}（{u.get('role')}）"
                         f"会话 {u.get('conversations', 0)}／消息 {u.get('messages', 0)}"
                         f"／最近 {short_time(last)}")
    return _cap("\n".join(lines))


# ── 报表 ⑤：文章流量（20260930）────────────────────────────────────
# 数据源 = `GET /api/protected/stats/notes`（`src/routes/note_stats.rs`），口径三条
# 全在那个文件的模块头注里：只算**当前可见**的文章、三个榜**数组顺序即名次**（后端
# 没有 rank 字段，序号由这里按下标印）、`daily` 已补零成定长 30 天。
#
# 为什么要在这里把"第 N 名"印出来：主人问的是"排名前几的文章都排第几"——位置就是
# 答案本身。把数组丢给 narrator 让它自己数下标，等于把"数数"这件会出错的事交给它
# （与"数字在工具侧算好"同一条纪律，见模块头注）。

# 榜单长度 = Rust `note_stats::TOP_N`（**同值契约**，tests/test_note_stats.py 直接
# 读父仓源码对账）。它决定报表里那句"下列前 N 名"怎么写，改一侧必须同步另一侧。
_RANK_TOP = 10

# 一行的三个计数：键名（跨语言契约）→ 中文量词。顺序 = 报表里的展示顺序。
_COUNT_WORDS = (("views", "阅读"), ("likes", "点赞"), ("favorites", "收藏"))

# 合计那三个数是**另一套键名**（`totalViews` …），不能与行内的共用一份：
# 拿行里的键去取合计会一个也取不到——而它静默（`data.get` 返回 None），
# 报表上就只剩一句"本次没读到合计值"（首版就这么写错过一次）。
_TOTAL_WORDS = (("totalViews", "阅读"), ("totalLikes", "点赞"), ("totalFavorites", "收藏"))


def _count_int(v) -> int | None:
    """一个计数 → int；**认不出就是 None**（调用方据此整段不印，绝不印成 0）。

    报表里印出来的每个数都会被 narrator 当成事实转述，所以"没有这个数"与
    "这个数是 0"必须分得开（同 `notes.rs` 的 `Option` + `skip_serializing_if`）。
    `bool` 显式排除：它是 `int` 的子类，`True` 会被放行成 1。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _counts_text(row: dict, first: str = "") -> str:
    """一行的三个计数 → `阅读 120／点赞 8／收藏 2`（认不出的那些整个不出现）。

    `first` 是这一行的榜按哪个数排（那个数排到最前）——读的人（与看板娘）先看到的
    是排序依据，"第 3 名"后面紧跟的数就是它的名次依据。
    """
    keys = [k for k, _ in _COUNT_WORDS]
    if first in keys:
        keys.remove(first)
        keys.insert(0, first)
    words = dict(_COUNT_WORDS)
    parts = []
    for k in keys:
        n = _count_int(row.get(k))
        if n is not None:
            parts.append(f"{words[k]} {n}")
    return "／".join(parts)


def _rank_block(head: str, rows: list | None, metric: str) -> list[str]:
    """一个榜 → 行列表。`rows` 的顺序就是名次（后端已排好，这里只按下标编号）。

    **榜名的量词要进每一行**（`阅读榜` 的行里第一项也是"阅读 N"）：同一篇文章在
    两个榜上的名次可以不同，只印数字不印维度，看的人会把两个榜串成一串。

    ⚠️ `rows is None`（键不在/是 null）与 `rows == []` 是**两件事**，话必须分开：
    前者是"这一项这次没读到"（老后端 / 报表被裁），后者是"真的没人读过"。
    把前者说成后者，就是替站内下一个**"没有"的结论**（gate 洞④ 的供体）。
    """
    word = dict(_COUNT_WORDS)[metric]
    if rows is None:
        return [f"- {head}：本次没读到（后台没有返回这一项）"]
    if not rows:
        return [f"- {head}：全站没有任何文章有{word}记录"]
    # 后端最多给 TOP_N 行：正好给满时**不能**说成"全站就这些"（可能有第 11 名没给）
    scope = (f"下列前 {len(rows)} 名，报表最多只列到这里"
             if len(rows) >= _RANK_TOP else f"全站共 {len(rows)} 篇有{word}记录")
    lines = [f"- {head}（按{word}量倒序，{scope}）:"]
    for i, r in enumerate(rows, 1):
        title = sanitize_untrusted(str((r or {}).get("title") or ""), 40) or "（无标题）"
        counts = _counts_text(r or {}, first=metric)
        tail = f" {counts}" if counts else ""
        lines.append(f"  · 第 {i} 名 《{title}》（noteId {r.get('noteId')}）{tail}")
    return lines


def render_note_stats(data: dict, now: datetime | None = None) -> str:
    """`GET /api/protected/stats/notes` 的返回 → 文章流量报表。

    ⚠️ 三个总数（`totalViews`/`totalLikes`/`totalFavorites`）一律用服务端算好的聚合值，
    不要拿榜上的行去加：榜只有前 10，加起来只会比真值小。
    """
    data = data or {}
    totals = [(w, _count_int(data.get(k))) for k, w in _TOTAL_WORDS]
    lines = [f"文章流量报表（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）",
             "- 全站合计（只算当前可见文章）："
             + ("、".join(f"{w} {n}" for w, n in totals if n is not None)
                or "本次没读到合计值")]
    lines += _rank_block("阅读榜", data.get("topViewed"), "views")
    lines += _rank_block("点赞榜", data.get("topLiked"), "likes")
    lines += _rank_block("收藏榜", data.get("topFavorited"), "favorites")

    # `daily` 同样三态：没读到 / 读到但全是零 / 读到且有数（见 `_rank_block` 注）
    daily = data.get("daily")
    hit = [d for d in (daily or [])
           if isinstance(d, dict) and ((_count_int(d.get("views")) or 0)
                                       or (_count_int(d.get("likes")) or 0))]
    if daily is None:
        lines.append("- 近 30 天趋势：本次没读到（后台没有返回这一项）")
    elif not hit:
        lines.append("- 近 30 天趋势：这 30 天里没有任何阅读或点赞记录")
    else:
        shown = hit[-7:]
        days = "；".join((d.get("date") or "?") + " " + _counts_text(d) for d in shown)
        prefix = f"…（更早还有 {len(hit) - len(shown)} 天）" if len(hit) > len(shown) else ""
        lines.append(f"- 近 30 天趋势（只列有记录的日子，共 {len(hit)} 天）: {prefix}{days}")

    # 名次口径作为**事实**写进帧里（"哪张榜的第几名"是这张报表的结构，不是修辞）：
    # 三张榜各自独立排名，序号之间没有关系。怎么转述是技能 reply_contract 的事。
    lines.append("- 名次口径：阅读/点赞/收藏三张榜各自独立排名（同一篇文章在两张榜上"
                 "的名次可以不同）")
    return _cap("\n".join(lines))


# ── 报表 ⑥：文章分期（20261001）─────────────────────────────────────
# 数据源 = `GET /api/protected/stats/notes/periods`（`src/routes/note_stats.rs`）。
# 与上面那张快照报表**三条口径同源**（只算当前可见文章、名次=数组顺序、缺键 ≠ 0），
# 多出来的是**期**这一层，于是多出两条判据：
#
#   · **期界**：每期是闭区间 [start, end]，必须印出来——"上周"在主人嘴里与他心里
#     未必是同一个七天，而模型的下一步动作就是念着这一行回答他；
#   · **`partial` 是真话的一部分**：统计功能的起点落在某一期中间时，那一期只统计了
#     部分天数（`partial=true`）。不写它，"上线那一周只有两天数据"会被读成"那周流量
#     掉了"——这正是本模块反复强调的"缺数 ≠ 零"，只不过量词换成了"期"。
#
# 以及一条**口径事实**要写进帧里：每期的合计算的是**本期全量**，而榜只列前 N 篇
# （加起来必然比合计小）；名次是**本期阅读量**序，期与期各自独立（同一篇文章在两期
# 里的名次没有关系）。
_PERIOD_TOP = 5      # = Rust `note_stats::PERIOD_TOP_N`（**同值契约**，见
                     # tests/test_note_stats.py ⑨——改一侧必须同步另一侧）
# 粒度 → 中文纸名。**唯一一份**：动作行（`agent/action_text.py`）也从这里取——
# 两处各抄一份的话，「行里写的是月报、帧里印的是周报」这种偏差没有任何测试能发现
# （同 `BOARD_APPROVED_CN` 那条"别在这里再抄"的纪律）。
_KIND_CN = {"week": "周报", "month": "月报", "year": "年报"}


def _period_line(p: dict) -> str:
    """一期 → `- 2026-W40（10-01 ~ 10-07）阅读 120／点赞 8／收藏 2`（+ 不完整标记）。"""
    label = str(p.get("label") or p.get("key") or "?")
    span = f"（{short_time(p.get('start'))} ~ {short_time(p.get('end'))}）"
    counts = _counts_text(p, first="views") or "本次没读到合计"
    # `partial` 只认真正的 True：认不出（老后端没这个键）就**不写**这一句，
    # 而不是默认写"完整"——"不完整"是缺数，"完整"是一个我们没有的断言。
    flag = "，**本期不完整**（统计起点落在本期中间，只统计了部分天数）" \
        if p.get("partial") is True else ""
    return f"- {label}{span}{counts}{flag}"


def render_note_periods(data: dict, now: datetime | None = None) -> str:
    """`GET /api/protected/stats/notes/periods` 的返回 → 分期报表。

    ⚠️ 每期的合计一律用服务端算好的值，**不要拿该期的榜相加**：榜截到前 N 篇，
    那一和只等于前 N 篇（Rust 侧为此专门不这么算，见 `note_period_report` 的注）。
    """
    data = data or {}
    kind_cn = _KIND_CN.get(str(data.get("kind")), str(data.get("kind") or "分期"))
    lines = [f"文章{kind_cn}（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）"]

    # `since` 三态：**没读到这个键**（老后端/被裁）/ 是 null（一行记录都还没有）/
    # 有值（统计起点）。前两者的话不一样：null 是上游明说的"还没开始统计"。
    since = data.get("since", "absent")
    if since == "absent":
        lines.append("- 统计起点：本次没读到（后台没有返回这一项）")
    elif since is None:
        lines.append("- 统计起点：站内还**一行阅读记录都没有**，所以给不出任何一期"
                     "（这是「还没开始统计」，不是「这些期没人看」）")
    else:
        lines.append(f"- 统计起点（最早有记录的那天）：{since}")

    periods = data.get("periods")
    if periods is None:
        lines.append("- 分期明细：本次没读到（后台没有返回这一项）")
        return _cap("\n".join(lines))
    if not isinstance(periods, list) or not periods:
        # 与 `since is None` 分开说：这里起点是有的，只是**没有一期落在起点之后**。
        if since not in ("absent", None):
            lines.append(f"- 分期明细：**没有一期落在统计起点（{since}）之后**，"
                         f"报表给不出任何一期")
        return _cap("\n".join(lines))

    lines.append(f"- 共 {len(periods)} 期，**最新的在前**：")
    for p in periods:
        if not isinstance(p, dict):
            continue
        lines.append(_period_line(p))
        top = p.get("topNotes")
        if top is None:
            lines.append("    · 本期榜单：本次没读到（后台没有返回这一项）")
            continue
        if not top:
            # 榜为空 ⇔ 三个合计都是 0（Rust 侧一行都不推）。这里与"没读到"分得开。
            lines.append("    · 本期一篇被读被赞被收藏的文章都没有")
            continue
        lines.append(f"    · 本期阅读量前 {len(top)} 篇：")
        for i, r in enumerate(top, 1):
            title = sanitize_untrusted(str((r or {}).get("title") or ""), 40) or "（无标题）"
            counts = _counts_text(r or {}, first="views")
            tail = f" {counts}" if counts else ""
            lines.append(f"      · 第 {i} 名 《{title}》（noteId {r.get('noteId')}）{tail}")

    lines.append(f"- 口径：每期的合计是**本期全量**（榜单只列本期阅读量前 {_PERIOD_TOP} 篇，"
                 f"把榜上的行加起来会比合计小）；名次口径是**本期阅读量**，期与期之间"
                 f"各自独立排名（同一篇文章在两期里的名次没有关系）")
    return _cap("\n".join(lines))
