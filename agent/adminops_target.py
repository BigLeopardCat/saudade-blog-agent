# -*- coding: utf-8 -*-
"""写操作的目标点名解析，与"没读到过就不许写"的目标校验。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。形态与 `agent/authz.py`
的三件套（`REASON_*` / `*_frame` / `*_error_reason`）一致。"""

from __future__ import annotations

import re

# ── 目标 id 的归一（20260929 批 H · S2）─────────────────────────────────
# 写通道从"名字/正文片段"改成"台账里的 id"之后，模型填进来的值有两种合法写法：
# 裸数字（`101`）与**帧里印出来的带命名空间写法**（`talkId:101` / `账号 id=3` /
# `userId:3` / `#101`）。`_board_label` 印的就是前者，而 `_render_quota_facts`
# 印的是「（账号 id=3，…）」——模型照抄整段时会把命名空间一起抄进来。
#
# **宽容的只是写法，不是"猜"**：剥掉命名空间后必须剩下**一个纯整数**，多一个
# 字符都算认不出（返回 None ⇒ 零工具/零写，交回 planner）。所以这里既不是模糊
# 匹配、也不替模型解释它想说的是谁——"这个 id 是不是真的存在"由**现场台账**判
# （`tools.base` 的 id 分支与 `graph._ledger_target_refusal`）。
_TARGET_ID_NS_RE = re.compile(
    r"(?:talkId|talk_id|talkid|userId|user_id|userid|账号\s*id|account_id|id|#)"
    r"\s*[:=#]?\s*#?(\d+)", re.IGNORECASE)


def normalize_target_id(value) -> int | None:
    """写目标 id（留言 talkId / 额度申请人的 userId）→ 正整数；认不出 → None。

    认三种形态：`int`、纯数字串、带命名空间的写法（见上面那张正则的注）。
    **不认**：负数、0（id 从 1 起）、浮点、带别的内容的串——一律 None。
    `bool` 显式排除（`True` 是 `int` 的子类，`id=True` 会变成 1）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value or "").strip()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return n if n > 0 else None
    m = _TARGET_ID_NS_RE.fullmatch(s)
    if not m:
        return None
    n = int(m.group(1))
    return n if n > 0 else None


# ── 写操作确认框（20260921）：问句与回复文本都是**确定性中文**────────────
# 与 agent/reports.py 同一条纪律：能算的都不交给 LLM。这两段文本会直接进
# ①确认框的问题行 ②那一轮的对话气泡，都是用户一眼看到的东西——让模型写它，
# 就又多了一处"它可能把没执行的说成已执行"的地方（而这一轮恰好什么都没执行）。
def _name_list(value) -> list[str]:
    """spec 里的标签名列表 → 干净字符串列表（渲染用；非列表/空值一律当空）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x).strip() for x in value if str(x or "").strip()]


def _match_board(boards, quote) -> dict | None:
    """在留言清单快照里按正文片段找**唯一**一条（问句渲染用）。

    与 `tools.base._find_board_comment` 同一判据（原文子串 → 去空白子串兜底），
    但**只用于渲染**：这里返回 None 只是"问句里写不出具体是哪一条"，表示"没核对上"
    （读不到清单 / 命中 0 条 / 命中多条都算），渲染方据此如实标注、不装作核对过。
    真正的"能不能动"由规划轮的目标预检与工具侧的复核各自独立判一次。
    """
    if not boards:
        return None
    want = str(quote or "").strip()
    if not want:
        return None
    rows = list(boards.values()) if isinstance(boards, dict) else list(boards)
    hits = [r for r in rows if want in str(r.get("content") or "")]
    if not hits:
        squashed = re.sub(r"\s+", "", want)
        if squashed:
            hits = [r for r in rows
                    if squashed in re.sub(r"\s+", "", str(r.get("content") or ""))]
    return hits[0] if len(hits) == 1 else None


# ── 后台写：目标校验（"没读到过就不许写"）────────────────────────────
# 形态与 agent/authz.py 的三件套（REASON_* / *_frame / *_error_reason）一致——
# 原因码是 planner 的输入（rule5 按原因码决定改参还是去问），帧是给 planner 读的，
# 取回函数给 checker 用（判 BLOCK 并把原因码带进 blocked 链路）。
REASON_UNKNOWN_TARGET = "unknown_target"
# 目标与用户点名不一致（20260921 第三轮）：与 unknown_target 分开——那条是"根本没读到过"，
# 这条是"读到了、但读的是别的"（planner 把清单第一行当成了用户点的那一篇）。
REASON_TARGET_MISMATCH = "target_mismatch"


_NAMED_ARTICLE_RES = re.compile(r"(?:文章|笔记|帖子|文档)\s*[#＃No.、]?\s*(\d{1,7})")
_NAMED_ORDINAL_RES = re.compile(r"第\s*(\d{1,7})\s*(?:篇|章|条|个)")
_NAMED_ID_RES = re.compile(r"\bid\s*[=:：]?\s*(\d{1,7})", re.I)
# 枚举延伸（"文章 12 和 14"/"文章 12、13"）：第一个点名之后紧跟连接词的数字
# 也算点名——**漏认的代价是不对称的**：少认一个，合法命令里那第二篇会被
# target_mismatch 判成"改错篇"而拒执行（用户点了三篇只改一篇）；多认一个只是
# 放宽（命中任一即算对上）。故宁可延伸，但**量词守卫照旧**（"文章 12 和 3 个要点"
# 里的 3 仍是计数，不是 id）。
# 20261008：连接词后面容一个「第」（「第 13 篇、第 11 篇」）。枚举延伸此前只接在
# 「文章 13」这一种标记后面，序数式（「第 13 篇和 11」）与 `id N` 式
# （「把 id 13、11、10 设为私密」——现场，见 `user_named_article_ids` 的头注）
# 都只认到第一个数，后面两篇于是被判成"改错篇"。
_NAMED_LIST_ITEM_RE = re.compile(
    r"\s*(?:和|与|跟|及|以及|还有|、|,|，)\s*(?:第\s*)?[#＃]?\s*(\d{1,7})")
_COUNT_TAIL_RE = re.compile(r"^\s*(?:篇|个|条|次|字|行|块|张|页)")


def user_named_article_ids(user_msg: str) -> set[int]:
    """用户**本轮原话里点名**的文章 id 集合——写侧目标的唯一权威（空集 = 没点名）。

    只认明确指称，不猜：`文章 12` / `文章#12` / `第 12 篇` / `id=12`；同义名词
    （笔记/帖子/文档）一并认；**枚举也认**（"文章 12 和 14"、"文章 12、13"，
    见 `_named_list_tail`）——但必须有**第一个带标记的点名**打头，裸数字不猜。
    **枚举延伸对三种标记一视同仁**（20261008）：`id 13、11、10` 与 `第 13 篇、第 11 篇`
    此前只认到第一个数——延伸只接在「文章」那一种标记后面，于是后两个 id 落了空集，
    被 `target_conflict` 判成"改错篇"而拒执行（现场 trace `20261008T064432`：主人一句
    「把 id 13、11、10 设为私密」，13 真的改了，11 被拒，收尾那句还否认改过任何一篇）。
    宽松的方向是有意的：多认一个只是放宽（命中任一即算对上），少认一个却会让
    合法命令里那第二篇被判成"改错篇"而拒执行。**计数形态不算**：数字在名词之前
    （"读了 12 篇文章"）结构上不匹配，"文章 12 篇"/"文章 12 和 3 个要点"这种后面
    跟量词的显式排除（见 `_COUNT_TAIL_RE`），否则"这篇文章 3 个要点"会被读成
    点了 id=3。
    """
    text = str(user_msg or "")
    out: set[int] = set()
    for rx in (_NAMED_ARTICLE_RES, _NAMED_ORDINAL_RES, _NAMED_ID_RES):
        for m in rx.finditer(text):
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if n <= 0 or n > 9_999_999:
                continue
            if rx is _NAMED_ARTICLE_RES and _COUNT_TAIL_RE.match(text[m.end():m.end() + 2]):
                continue  # 「文章 12 篇」= 计数，不是目标
            out.add(n)
            # 三种标记都往下吃枚举（20261008）：`id N、N、N` 与 `第 N 篇、第 M 篇`
            # 与「文章 N、M」是同一种语言事实，没有理由只认后者。
            out |= _named_list_tail(text, m.end())
    return out


def _named_list_tail(text: str, pos: int) -> set[int]:
    """点名之后的枚举延伸（"文章 12 **和 14**"）——从 pos 起逐个吃，断了就停。

    只在第一个点名**成立**之后调用（量词守卫已过），逐项仍要过量词守卫，否则
    "文章 12 和 3 个要点里的那篇"会把 3 认成文章 id。
    """
    out: set[int] = set()
    while True:
        m = _NAMED_LIST_ITEM_RE.match(text, pos)
        if not m:
            return out
        if _COUNT_TAIL_RE.match(text[m.end():m.end() + 2]):
            return out
        n = int(m.group(1))
        if 0 < n <= 9_999_999:
            out.add(n)
        pos = m.end()


def target_named(article_id, named: set[int]) -> bool:
    """id 是否落在用户点名的集合里。**空集 = 判据不启用**（恒 True）。"""
    if not named:
        return True
    try:
        return int(article_id) in named
    except (TypeError, ValueError):
        return False


def target_mentioned(article_id, texts) -> bool:
    """文章 id 是否在**本轮可见的材料**里作为独立数字出现过。

    `(?<!\\d)N(?!\\d)` 两端都不许挨着数字：id=12 不能被 "id=123"、"1912" 里的
    子串冒充命中（那正是"凭记忆写了个 id，恰好与列表里另一篇的编号部分重合"）。

    这是**供方**判据（调用方把本轮读过的帧 + 页面上下文 + 用户消息拼成 texts），
    不是"id 一定对"的保证——它拦的是"整轮什么都没读却写一个凭记忆的 id"。
    """
    try:
        aid = int(article_id)
    except (TypeError, ValueError):
        return False
    if aid <= 0:
        return False
    rx = re.compile(rf"(?<!\d){aid}(?!\d)")
    return any(rx.search(str(t)) for t in texts if t)


# 目标校验帧里的**动作词**（20260923 批 7）：帧文本会进 planner 上下文、也会被
# narrator 念给用户，"改"字用在收藏上读起来像系统认错了事（"你要**改**的那篇文章"）。
# 缺省仍是"改"（文章写那两件的措辞一个字节都没变）。
_TARGET_VERB = {
    "add_favorite": "收藏",
    "remove_favorite": "取消收藏",
}


def target_verb(name: str) -> str:
    """目标校验帧里该用的动作词（未登记的工具 = 文章写，"改"）。"""
    return _TARGET_VERB.get(name or "", "改")


def unknown_target_frame(name: str) -> str:
    """目标无据时的错误帧（checker 判 BLOCK，planner 先读再写）。"""
    verb = target_verb(name)
    return (f"__ERROR__: 目标未经确认[{REASON_UNKNOWN_TARGET}]"
            f"（{name} 要{verb}的那篇文章这一轮没有读到过——先读后台文章列表或正文拿到 id，"
            f"或让主人点名是哪一篇，再{verb}；不要凭记忆写一个 id）")


def target_conflict_frame(name: str, named: set[int], got) -> str:
    """写目标与用户点名不一致时的错误帧（checker 判 BLOCK）。

    20260921 第三轮活体探针实证：管理员说「把文章 1 置顶」，planner 把
    `list_admin_notes` 返回的**第一行**（id=46）填进了 article_id——它读到了 46，
    所以"目标有据"那条判据放行（46 确实在本轮帧里），但那不是用户点的那一篇。
    用户原话点名的 id 是权威：不一致就不执行，把"应该改哪一篇"明确写回帧里，
    让 planner 下一轮自己改回来（禁止由系统替它改写参数——目标必须由用户/planner
    决定，系统只做否决）。
    """
    verb = target_verb(name)
    want = "、".join(str(x) for x in sorted(named))
    return (f"__ERROR__: 目标与主人点名的不是同一篇[{REASON_TARGET_MISMATCH}]"
            f"（{name} 这一轮填的是文章 {got}，而主人点名的是文章 {want}——"
            f"以主人点名的为准，下一轮把 article_id 改成 {want} 再{verb}；"
            f"若主人确实要动文章 {got}，先把这一点问清楚）")


def target_error_reason(text: str) -> str | None:
    """从错误帧取回原因码（非本族帧 → None），供 _check_spec 用。"""
    s = str(text or "")
    if f"[{REASON_TARGET_MISMATCH}]" in s:
        return REASON_TARGET_MISMATCH
    return REASON_UNKNOWN_TARGET if f"[{REASON_UNKNOWN_TARGET}]" in s else None


# ── 后台写的**政策拒绝**（20260926）：后端说"这件事规则上不许做" ─────────────
# 与上面两族（目标没读到 / 目标读错了）并列的第三族，形态完全一样：原因码 + 帧 +
# 取回函数，`_check_spec` 在 `target_error_reason` 那个 or 链上多接一条。
#
# 为什么走**错误帧族**而不是新增一个 `ToolResult` kind（这一条想清楚过）：
# `_check_spec` 是一条**显式 if 链**。新 kind 若忘了加分支，会 fallthrough 到
# "文本非空 ⇒ PASS"⇒ 产回执 ⇒ 进 execution_log ⇒ 下一轮 `recent_executions` 把
# "冻结账号 X"当成**已完成的事实**喂给 narrator。这是本族最不能出现的错，而且
# **完全静默**。错误帧族的"忘记接线"后果是良性的：落到最后一个 `or` 的
# `error_frame`，照样 BLOCK、照样不进回执，只是过程行说「执行出错」而不是
# 「后台规则拒绝」。此外只有错误帧族能吃到 gate 5a（错误帧 + 完成式声称 → fallback）
# 这条结构保证——一般的 ToolResult 不是 err frame，5a 根本不触发。
REASON_POLICY_REFUSED = "policy_refused"


def policy_frame(message: str) -> str:
    """后端政策拒绝的错误帧（checker 判 BLOCK；planner 读到原因码后应当**如实
    转述、不再改参重试**）。

    `message` 是**后端给的那句中文原话**，逐字放进帧里：政策只在 Rust 侧实现一次
    （见 docs/security-boundary.md §7⑫），agent 这边任何"翻译"都会在政策变更那天
    变成假话。帧里的指引也据此写成"如实转告"，而不是"换个参数再试"：这条请求
    再试一百次也是同一个结果，而每一轮重试都会让主人在等一件永远不会发生的事。
    """
    return (f"__ERROR__: 后台规则拒绝[{REASON_POLICY_REFUSED}]"
            f"（{str(message or '').strip() or '后台拒绝了这次改动'}——"
            f"这条不是系统故障、也不是你没填对参数，是后台的规则不许这件事发生。"
            f"**不要换参数重试**（再试多少次都是这个结果），把上面这句话如实转告主人，"
            f"并说明本次**一个字节都没有改动**）")


def policy_error_reason(text: str) -> str | None:
    """从错误帧取回原因码（非本族帧 → None），供 _check_spec 用。"""
    return (REASON_POLICY_REFUSED
            if f"[{REASON_POLICY_REFUSED}]" in str(text or "") else None)
