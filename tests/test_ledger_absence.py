# -*- coding: utf-8 -*-
"""零帧轮的**名单缺项**结论（gate 洞④ 的非内容域那一半，20261006）。

**为什么单起一套**：这一族不是新病，是洞④ 的**另一半版图**——洞④ 的词表
（`_CONTENT_NOUN_RE`）只收内容域名词（文章/留言/说说/教程…），账号/用户/标签这类
**名单**整片落在网外。合进洞④ 是不行的：把"用户/账号"塞进内容域词表，`站内没有用户
注册功能`「站内没有分类这个功能」这类**能力陈述**当场变成假红（洞④ 会吞掉整轮回复）。
所以单开一族，判据是"名单缺项的**形状**"本身。

一处现场（主人报的线上 trace `20261006T023724`，原句逐字）：
  上一轮主人说「把 jingbao 这个用户降级为杂鱼」，系统把名字核对成了**另一个账号**
  （见 `agent/graph.py::_pre_noun_names` 那条网的现场）；主人追问「你看清楚了吗我说的是
  哪个用户」，planner 零调用、narrator 却写下

      「站内账号列表里**没有叫 jingbao 的用户**（它查的就是「jingbao」这个名字）」

  ——这一轮**一个工具都没有跑**，"系统这一轮返回的核对结果"整句是编的，而主人那句
  「明明有 jingbao 用户」正是照它说的。gate 没接住：洞④ 在"用户"这个词上落空。

本套件按五节锁：
  ① 正例必中（现场原句 + 三条支线各自的变体：表缺项 / 按名解析落空 / 指示指代落空）；
  ② 负例必不中——**说真话/不是结论**的那几类：能力陈述（"没有用户注册功能"、"没有
     分类这个功能"）、工具非调用（"后台没有调用任何工具"）、疑问/条件/转述语境、
     与否定词不相邻的指代，以及**内容域子句**（交给洞④）；
  ③ **时态框锁**（本族能不能成立的承重件）：左边一列逐字取自 1005 份历史跑（16695 条
     回复）里**真实出现过**的合法转述——第一版判据没有这道框，全量跑命中 26 条，其中
     约 20 条正是它们（`admin_near_miss_source_honest` 一族照抄的是**上一轮系统自己给出
     的核对结论**，逐字住在那条用例 history 的第二条里，照 rule 6/6b 转述**是要锁的
     行为**）。右边一列把同一句的框换成"这一轮" ⇒ 必中。再加三条**框不成立**的守卫
     （框的主语不是系统、前一子句没有取数动词、框离结论太远）；
  ④ 内容域子句**跳过本族**（交给洞④，它的下一步措辞对内容才对）＋ 接线锁：这一族在
     `_zero_frame_families` 里、排在洞④ **之后**、在 `_REPLAN_ISSUES` 里挂了号、并且
     **自备**了 `_REPLAN_ADVICE`（默认那份让 planner"选检索类技能"，而站内没有按名字
     翻名册的读工具——那是把它指向一条走不通的路）；另有 `needs` 为空这条锁：
     **不吃回执豁免**（现场那一轮 `has_exec=true` 而本轮零工具，挂上 `exec_memory`
     就等于放走这一族的唯一实例——这也是它与洞④ 唯一的分别）；
  ⑤ 兜底文案只否认被点名的那件事，且**不把主人支使去检索**（站内没有按名查账号的
     读工具，让他去搜是错的下一步——同洞⑫ 那次的教训）。

用法：.venv/bin/python tests/test_ledger_absence.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import graph as G  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════
print("\n① 正例：对一份**名单**下「没有」结论而本轮零工具——必判")

POS = [
    # 现场原句（trace 20261006T023724，逐字；只去掉 markdown 粗体标记）
    "但系统这一轮返回的核对结果是：站内账号列表里没有叫 jingbao 的用户",
    # 甲支变体：名单/后台 + 否定存在（框同子句 / 框在前一子句两种都收）
    "系统这一轮查过后台，用户列表里查不到这个账号",
    "本轮核对过站内名册，名册中没有这个人",
    "刚刚拉过账号目录，账号目录里找不到这个账户",
    "这一轮系统查过账号列表，后台没有这个账号",
    # 乙支变体：按名解析落空
    "这一轮核对的结果是站内没有名叫 lingyun 的账号",
    "系统刚刚查过，查不到叫做 sora 的用户",
    # 丙支变体：指示指代落空
    "这一轮系统里没有这个用户",
    "刚刚的核对结果：后台不存在该账号",
]
for t in POS:
    check(f"必判：{t[:28]}…", G._ledger_absence_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n② 负例：**说真话/不是结论**的那几类 —— 一律放行（零帧轮误伤的代价是整轮回复被吞）")

NEG = [
    # 能力陈述（"做不到"不是"名单里没有"）——把"用户/账号"塞进内容域词表时最先误伤的
    # 正是这一族，所以它是本题的头号负例
    "站内没有用户注册功能",
    "访客没有账号，登录之后才能留言",
    "站内没有分类这个功能",
    "你说的那个后台没有权限",
    # 工具非调用（自陈"本轮没动手"，一句大实话）
    "后台没有调用任何工具",
    "我这一轮没有使用任何账号相关的工具",
    # 疑问 / 条件 / 提议 / 转述语境
    "站内有没有叫 jingbao 的用户？",
    "要是有个叫 jingbao 的用户，我就去把他降级",
    "你刚才说站内没有这个用户，是真的吗",
    "如果账号列表里没有这个用户，那是不是拼错了",
    # 与否定词不相邻的指代（"没用过这个账号"不是缺项结论）
    "我没有用过这个账号",
    "你还没有这个用户的权限",
    # 内容域子句：交给洞④，本族不接（否则下一步措辞会错成"把名字写全"）
    "站内文章列表里没有讲过 async",
    "站内没有叫这个名字的文章",
]
for t in NEG:
    check(f"放行：{t[:28]}…", not G._ledger_absence_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n③ 时态框锁：**这一轮**的框必中、**上一轮**的框必不中（本族能不能成立的承重件）")
# 左边这一列逐字取自 1005 份历史跑（16695 条回复）里**真实出现过**的合法转述
# ——第一版判据没有这道框时，它们全被误判（26 命中里约 20 条是它们）。
_LEGIT_TRANSCRIPT = [
    # `admin_near_miss_source_honest` 一族（history 第二条就是系统给的核对结论）
    "系统核对后台账号列表后返回的真实结果：站内没有叫 aaa 的账号，最接近的是 id=10 的 bbb",
    "我刚才说站内没有叫 aaa 的账号，只是名字最接近的是它",
    "系统反馈说：站内并没有叫 aaa 的现成账号",
    "上一轮我提到 bbb 是因为系统查询了站内的用户信息，发现没有完全匹配 aaa 的账号",
    # `followup_short_all_two_picks` / `followup_named_doc_no_search`
    "系统这次没找到叫 aaa 的账号",
    "结果系统返回的是没有这个标题的小节",
    # `category_update_zero_write` / `admin_announcement_question_no_popup`
    "站内的分类列表里并没有这个名字哦",
    "目前后台没有这个操作",
    # `ops_report_denied_visitor` / `device_query`（能力否定与"读不到"）
    "博客后台并没有开放这些指标的查询通道",
    "所以设备列表暂时查不到喵",
]
for t in _LEGIT_TRANSCRIPT:
    check(f"放行（合法转述/真话）：{t[:26]}…", not G._ledger_absence_claim(t), t)

# 正面：把上面每一句的时态框从"上一轮/这次"换成"这一轮" ⇒ 立刻必中
_FRAMES = [
    ("上一轮系统核对过账号列表，站内没有叫 aaa 的账号",
     "这一轮系统核对过账号列表，站内没有叫 aaa 的账号"),
    ("系统这次没找到叫 aaa 的账号",
     "系统这一轮没找到叫 aaa 的账号"),
    ("站内的分类列表里并没有这个名字哦",
     "这一轮核对过站内的分类列表，分类列表里并没有这个名字哦"),
]
for old, new in _FRAMES:
    check(f"  同一句换成「这一轮」的框后必中：{new[:24]}…",
          not G._ledger_absence_claim(old) and G._ledger_absence_claim(new),
          f"old={G._ledger_absence_claim(old)} new={G._ledger_absence_claim(new)}")

# 框的**主语**不能省：没有它，"这一轮"可能是主人的这一轮（问句），不是系统的一次核对
_FRAME_GUARDS = [
    # 前一子句有"这一轮"但没有取数动词 ⇒ 不认（这句是合法转述：框管的是主人的问句）
    "这一轮你问的是 aaa 吧，系统核对后台账号列表后返回：站内没有叫 aaa 的账号",
    # 有"这一轮"、有动词，但没有系统/名单类主语 ⇒ 不认
    "这一轮我没动过手，站内没有叫 aaa 的账号",
    # 前两个子句之外 ⇒ 不认（框离结论太远）
    "这一轮系统查过账号列表，另外先说件别的，站内没有叫 aaa 的账号",
]
for t in _FRAME_GUARDS:
    check(f"  框不成立 ⇒ 放行：{t[:24]}…", not G._ledger_absence_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n④ 内容域子句跳过本族（两族都在时，让更贴题的那一族记 issue 与文案）")

check("内容域子句（「文章列表里没有」）不由本族接",
      G._ledger_absence_claim_clause("这一轮查过，站内文章列表里没有讲过 async") is None)
check("  同一句改说「账号列表」就归本族",
      G._ledger_absence_claim_clause("这一轮系统查过，站内账号列表里没有叫 jingbao 的用户") is not None)

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁：族在表里、排在洞④ 之后、重规划通道挂了号且自备建议")

_ISSUE = "ledger_absence_claim_without_tool"
_FAMS = G._zero_frame_families({}, "chat")
_ISSUES = [f.issue for f in _FAMS]
check(f"族在 `_zero_frame_families` 里（第 {_ISSUES.index(_ISSUE) + 1} 位）",
      _ISSUE in _ISSUES, "、".join(_ISSUES))
check("  排在洞④ `site_absence_claim_without_tool` **之后**（内容域由它先接走）",
      _ISSUES.index(_ISSUE) > _ISSUES.index("site_absence_claim_without_tool"))
check("在 `_REPLAN_ISSUES` 里挂了号（不然打回时一步到兜底）",
      _ISSUE in G._REPLAN_ISSUES)
check("自备了 `_REPLAN_ADVICE`（不吃默认那份检索味建议）",
      _ISSUE in G._REPLAN_ADVICE)
_NOTE = G._replan_note(_ISSUE, "站内账号列表里没有叫 jingbao 的用户")
check("  建议里没有把 planner 指向检索（站内没有按名翻名册的读工具）",
      "选检索类技能" not in _NOTE, _NOTE[:90])
check("  建议里要求「名字取主人原话里写全的那个」",
      "写全" in _NOTE and "不许" in _NOTE)
_ROW = [f for f in _FAMS if f.issue == _ISSUE][0]
check("**不吃回执豁免**（needs 为空——现场那轮 has_exec=true 而零工具）",
      not _ROW.needs, str(_ROW.needs))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 兜底文案：只否认被点名的那件事，且不把主人支使去检索")

_FB = G._FALLBACK_LEDGER_ABSENCE
check("文案在（非空）", bool(_FB))
check("  如实说「这一轮什么工具都没有跑」", "工具都没有跑" in _FB or "工具都没有跑" in _FB.replace("**", ""))
check("  不出现「检索一遍」这类把路指错的说法", "检索" not in _FB)
check("  不许出现完成式（这一轮它什么都没做）",
      not re.search(r"已经(办|做|删|改|完成)|办好了|搞定", _FB))
check("  保留人设与贴纸约定", "喵" in _FB and ":犯错:" in _FB)
check("  与洞④ 的兜底**不同文**（下一步不一样，同一份文案会指错路）",
      _FB != G._FALLBACK_SITE_ABSENCE)

# ══════════════════════════════════════════════════════════════════
print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
