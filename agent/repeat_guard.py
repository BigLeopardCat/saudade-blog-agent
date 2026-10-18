# -*- coding: utf-8 -*-
"""逐字复读判据（gate 1b）：**本轮回复是不是把前几轮自己的回复抄了一遍**。

**为什么单独成模块**（20261017 拆 `graph.py` 第三刀；前两刀见 `agent/plan_codec.py`
与 `agent/tool_specs.py`）：这一族此前住在 `agent/graph.py`（本刀之前实测
13682 行 / 250 个顶层定义），而它对 graph.py 的**出边为零**——只用 `difflib`、
`re`、`agent.prompt_context._msg_text` 与 `AIMessage` / `HumanMessage`，一个 graph.py
里的名字都不需要。前两刀的判据不同：第一刀靠"边界干净"、第二刀靠"出边为零"，
本刀**同时满足**（族内 5 个函数 + 9 个独占常量自成一圈，常量连注释一起搬走）。

**它是什么**：gate 判完"该查而没查"之后的**第二道闸**——不许把复读当成回答。
两档判据（距离 1 走老档"最长连续块"，距离 ≥2 只接新档"整体重合率 + 最长块"）
＋一档放行（用户点名重做 ⇒ 高重合是被要求的）。门槛与误伤面都是按真实语料量过的，
各段注释连数值一起原样搬来——**这些注释就是判据的出处，不能只搬代码**。

**搬家方式**：`agent/graph.py` 按**原名** re-export 这 14 个名字（5 个函数 + 9 个常量）
⇒ `gate_node` 里那两处调用点（`_repeat_reason(reply, _recent_ai_replies(msgs), ...)`）
与全仓 `from agent.graph import _REPEAT_MIN_RUN, _prev_ai_reply, ...`
（`tests/test_workflows.py`）**一处都不用改**。判据侧一个字都没动：两条源码锁
（`test_workflows` 的 `_GSRC` 断言那句调用、`test_turn_window` 的 `_FULL_VIEW` 扫调用节点）
判的都是**调用点**，而调用点留在 graph.py。

⚠️ 本族带一个**中文正则**（`_REDO_REQUEST_RE`，`eval/judge_registry.json` 里
`category=open`）⇒ 搬它要**手工**把登记条目的 `id` / `file` 改到本文件，并同批改
两边的 `metric.by_file`（那条闸按文件记穷举量，缺席按 0 算）。前两刀是 `[0,0]`
所以没撞上这条契约。
"""

import difflib
import re

from langchain_core.messages import AIMessage, HumanMessage

from agent.prompt_context import _msg_text

# ── gate 1b：逐字复读上一轮回复（20260920）──────────────────────────────────
# 纪律 11（"绝不把历史里自己的回复原文再输出一遍"）只是**软约束**，压不住：
# 20260920 实证 narrator（温度 0.7）在"本轮用户消息模糊 + 上下文里摆着上轮一份
# 完整答案"时会把上轮回复整段抄出来——11:15:44 那条与 11 小时前 00:23:52 那条
# **逐字节相同**（781 字，difflib diff 为空）。0.7 采样自由生成撞出 781 字全同的
# 概率可忽略 ⇒ 它是从注入历史（最近 20 条纯历史，那条回复正好落在窗口里）里抄的。
# 抄写有机会是因为被 gate 否定的叙述仍进了历史（fallback_text channel 缺失，见
# _fallback_result）；修好 channel 后本判据是第二道闸：**不许把复读当成回答**。
# 判据（确定性、纯字面）：本轮回复与上一轮 assistant 回复的最长逐字连续片段
# ≥ max(200 字, 60% × 本轮长度) ⇒ 复读。门槛刻意高（宁漏勿误伤）：**只抓"整段
# 照抄"**。门槛由全量 trace 回放定（426 对相邻轮，见 eval 之外的一次性脚本能力）：
# floor=80 命中 4 对，其中两对是 90 字级的"两次都回答我不会做饭/烤蛋糕"——同义
# 寒暄撞同一句模板，应答本身合理，拦下来才是误伤；一对是用户点名重做（走
# _REDO_REQUEST_RE 放行）；抬到 200 后只剩 1 对真复读（09-05T19:10:07，489 字
# 逐字照抄）。合理复用（引用同一段工具返回、复述要点的两三句）远达不到门槛。
# 代价：≤200 字的回复不判复读——短回复的整段重合几乎都是模板复用，宁漏勿误伤。
# 与 _repeat_ask_note（server.py：用户**原句重发**时注入提示）分工不同——那个管
# 输入端（让模型别复读），本判据管输出端（真复读了就拦），触发条件也无关。
_REPEAT_MIN_RUN = 200
_REPEAT_COVER = 0.6

# ── 20261003 加：比对面从"紧邻一轮"扩到"最近 N 轮"，并补一档"整段抄写变体" ──────
# 现场（uid=1 会话 320，19:41）：用户问「我有哪些未读通知呀」，planner 落成 chat 零工具，
# narrator 把**上 2 轮**那份"我没有翻日志的工具"的答案搬了过来（整体重合 87.9%、最长
# 连续块 568 字）。上面两条现存限制同时把它放过去：① `_prev_ai_reply` 只取紧邻那一条
# （本轮那条与紧邻那条的相似度只有 9.7%，压根不在比对范围）；② 老判据量的是**最长
# 连续块**，门槛 max(200, 60%×967)=580 > 568——差 12 个字。
# 修法就对应这两条：比对面扩到最近 `_REPEAT_RECENT_N` 条；再补一档"整体重合率 + 最长块"
# 双条件。**老门槛不能照搬到更早轮次**：确认卡这类模板整段相同（259 字的卡在老门槛
# max(200, 60%×259)=200 下当场误伤），所以距离 ≥2 只接新档。
#
# 误伤面按两份真实语料量过（本机一次性脚本、按会话配对，不进仓）：
#   · chat_history 全量（26 会话 / 321 条 assistant）：距离 2..5 共 936 对 —— 新档命中
#     **1**（就是上面那一对），老档命中 0；
#   · trace 全量按 `input.conversation_id` 配对（55 个多轮会话 / 306 对相邻轮）——
#     新档命中 0、老档命中 0（对照：全量 1106 份 trace 里老档历史上只开火过 2 次）。
# 两档共用 `_REDO_REQUEST_RE` 豁免（用户点名重做 ⇒ 高重合是被要求的）。
_REPEAT_RECENT_N = 5      # 比对面：当前用户消息之前最近的 N 条 AI 发言（含紧邻那条）
_REPEAT_FAR_COVER = 0.75  # 「抄写变体」档：整体重合率（difflib ratio）下限
_REPEAT_FAR_RUN = 300     #                  且最长连续块下限（两条**同时**满足才判）
# 预筛（把 difflib 挡在门外）：40 字探针每 50 字取一个，命中 ≥4 个才做精确比对。
# 这是**必要条件**：最长连续块 ≥300 ⇒ 至少 5 个探针整块落在里面（300/50-1）⇒ 取 4 留
# 余量不会漏真命中。实测 chat_history 936 对里只有 1 对过筛（正是要抓的那对），
# 每轮开销 = 每个比对面约 20 次 C 级 `in`（~1000 字），可忽略。
_REPEAT_PROBE_LEN = 40
_REPEAT_PROBE_STEP = 50
_REPEAT_PROBE_MIN = 4

# 用户点名要求重做/重发 → 本轮高重合是**被要求的**，不得判复读（20260916 09:25:34
# 实证：用户说"flowchart 换回 graph 试试"，回复把 1400 字 mermaid 图原样重画、
# 只换了栅栏语言与开头一句——那是正确行为，拦下来等于把用户点名要的东西吞掉）。
# 只用于**放行**（宁漏勿误伤）：用户没这么说而复读 = 真复读。
_REDO_REQUEST_RE = re.compile(
    r"再(?:画|说|写|发|来|贴|试)|重新|重来|重发|重画|换个|换成|换回|换用|换一版|"
    r"改成|改一下|改一版|另一(?:个|种|版)|同一(?:个|张)图")


def _recent_ai_replies(msgs: list, n: int = _REPEAT_RECENT_N) -> list:
    """当前用户消息之前**最近 n 条** assistant 回复原文（**从近到远**）。

    注入历史形状 = [System 上下文 Human] + 历史(Human/AI 交替) + 当前 Human +
    本轮工具帧 + 本轮 AI 回复——从末尾往前先定位"当前用户消息"（最后一条非
    `[System:` 的 HumanMessage），再往前收最近的 AIMessage。

    为什么收 n 条而不是只收一条（20261003）：只取紧邻那条时，"抄上 2 轮那份现成
    答案"整族从判据底下过——`_prev_ai_reply` 拿到的对象与真被抄的那条毫不相干。
    距离越远越不像复读（长会话里"翻旧账"），所以距离 ≥2 只接严格档（见
    `_repeat_reason`）。无对比对象（首轮）→ 空列表，判据自动放行。
    """
    cur = None
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, HumanMessage) and not (_msg_text(m) or "").lstrip().startswith("[System:"):
            cur = i
            break
    if cur is None:
        return []
    out: list = []
    for m in reversed(msgs[:cur]):
        if isinstance(m, AIMessage):
            out.append((_msg_text(m) or "").strip())
            if len(out) >= n:
                break
    return out


def _prev_ai_reply(msgs: list) -> str:
    """紧邻上一轮 assistant 回复原文（= `_recent_ai_replies(msgs, 1)` 的第 0 条）。

    保留这个名字与签名：老判据（`_repeat_of_prev_reply`）与它的单测都按"只有一条"
    使用；要更早的轮次走 `_recent_ai_replies`。无则返回空串（自动放行）。
    """
    got = _recent_ai_replies(msgs, 1)
    return got[0] if got else ""


def _repeat_of_prev_reply(reply: str, prev: str, user_msg: str = "") -> bool:
    """本轮回复是否逐字复读上一轮回复（门槛见 _REPEAT_MIN_RUN 注释）。

    实现是最朴素的滑窗：取较短文本为窗口源、较长者为被查串，窗口长度 = 门槛，
    首个命中即判真（只需"是否达到门槛"，不求真正的最长值）。回复量级千字、
    str 查找是 C 级，无需后缀自动机。`user_msg` 带重做语（_REDO_REQUEST_RE）时
    直接放行——那是照办，不是复读。
    """
    if not reply or not prev:
        return False
    if user_msg and _REDO_REQUEST_RE.search(user_msg):
        return False
    short, long_ = (reply, prev) if len(reply) <= len(prev) else (prev, reply)
    thr = max(_REPEAT_MIN_RUN, int(len(reply) * _REPEAT_COVER))
    if len(short) < thr:
        return False
    return any(short[i:i + thr] in long_ for i in range(len(short) - thr + 1))


def _repeat_far(reply: str, prev: str) -> bool:
    """「整段抄写变体」档（20261003，见 `_REPEAT_FAR_COVER` 注释）：整体重合率达标
    **且**最长连续块达标。

    为什么不能只看最长连续块：抄的人只要在中间插自己的一句话，那一段逐字重合就被
    切成两半——本轮那条正是这样，568 字的长块（整体重合 87.9%）从 580 的门槛底下
    溜走。这一档问的是"整篇到底有多少字是抄来的"。

    先过廉价预筛（`_REPEAT_PROBE_*`，必要条件、不漏真命中），只有过筛才对两条文本
    做 difflib——生产每轮最多 5 个比对面，实测全量语料 936 对里只有 1 对值得精算。
    """
    if not reply or not prev:
        return False
    if len(reply) < _REPEAT_FAR_RUN or len(prev) < _REPEAT_FAR_RUN:
        return False
    hits = 0
    for i in range(0, len(reply) - _REPEAT_PROBE_LEN + 1, _REPEAT_PROBE_STEP):
        if reply[i:i + _REPEAT_PROBE_LEN] in prev:
            hits += 1
            if hits >= _REPEAT_PROBE_MIN:
                break
    else:
        return False
    sm = difflib.SequenceMatcher(None, reply, prev)
    block = max(m.size for m in sm.get_matching_blocks())
    return block >= _REPEAT_FAR_RUN and sm.ratio() >= _REPEAT_FAR_COVER


def _repeat_reason(reply: str, prevs: list, user_msg: str = ""):
    """复读判据总入口：命中返回 `("near"|"far", 距离, 那条回复原文)`，否则 None。

    · 距离 1（紧邻）：**老档** `_repeat_of_prev_reply`（门槛由 426 对相邻轮回放定过，
      一字不动）；新档在同一条上也接——"最近 N 轮"是同一份规则，距离 1 没必要留空洞
      （实测 trace 306 对相邻轮上新档新增 0 命中）。
    · 距离 ≥2：只接**新档**。老门槛在更早轮次上会误伤：模板类回复（确认卡等）整段
      相同，259 字的卡在老门槛 max(200, 60%×259)=200 下当场判复读。
    `_REDO_REQUEST_RE` 豁免在这里统一做（比 `_repeat_of_prev_reply` 内部那道更早）。
    """
    if not reply or not prevs:
        return None
    if user_msg and _REDO_REQUEST_RE.search(user_msg):
        return None
    for dist, prev in enumerate(prevs, start=1):
        if not prev:
            continue
        if dist == 1 and _repeat_of_prev_reply(reply, prev):
            return ("near", dist, prev)
        if _repeat_far(reply, prev):
            return ("far", dist, prev)
    return None
