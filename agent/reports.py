"""管理助手报表渲染（20260921）——**四张报表的纯函数出口**。

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
`sanitize_untrusted` 负责这件事，见它的注释与 `test_reports.py` 的回归锁。
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


def _detail_line(r: dict, extra: str = "") -> str:
    """一条留言的明细行：`#id 时间 作者（标注）「正文」`。

    `sanitize_untrusted` 是必须的（见模块头注）：这是全链路唯一"访客可控文本进
    prompt"的地方，命令前缀必须在这里拆掉——作者名同样是访客可控的（留言时可填）。
    """
    who = sanitize_untrusted(r.get("author") or r.get("nickname") or "", 16)
    if not who:
        who = f"用户#{r.get('userId')}"
    body = sanitize_untrusted(r.get("content") or "", 30)
    at = _short_time(r.get("createTime"))
    return f"  · #{r.get('talkKey')} {at} {who}（{extra}）「{body}」"


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
    if not pending:
        lines.append("- 当前没有待审留言")
    return _cap("\n".join(lines))


def _short_time(raw) -> str:
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
            name = sanitize_untrusted(u.get("name") or "", 20) or f"用户#{u.get('id')}"
            last = u.get("lastActiveAt") or "无活动"
            lines.append(f"  · #{u.get('id')} {name}（{u.get('role')}）"
                         f"会话 {u.get('conversations', 0)}／消息 {u.get('messages', 0)}"
                         f"／最近 {_short_time(last)}")
    return _cap("\n".join(lines))
