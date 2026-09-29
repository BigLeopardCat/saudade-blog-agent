# -*- coding: utf-8 -*-
"""提示词的**稳定前缀**（= 前缀缓存的命中长度）单测：离线、秒级、零网络零 LLM。

**为什么存在**：前缀缓存按**渲染后字符串的连续前缀**逐字节命中，命中那部分按缓存价
计费。所以"模板里哪一段算稳定前缀"不是文风问题，是钱——而它**只能被测量**，看不出
来（同一份提示词读起来完全一样，缓存命中长度可以从 2 万字掉到 4 千字）。这个套件把
它钉成一个数：把易变块换成另一组值再渲染一次，**最长公共前缀**就是缓存能命中的长度。

判据的形状（两条，第二个才是真正的目的）：
  ① `LCP ≥ 门槛`——抓"某次改动把易变块插到固定文本前面去了"这类**整体**回退；
  ② **固定文本的最后一行落在前缀里**——门槛是拍出来的数，这一条才说明"整段规则正文
     都在缓存里"。改模板时把某块挪回来，②先红，且它直接指向原因。

第三条锁的是**方向词**（"上方/下方 X"）：规则正文用相对位置指代各数据块，块一挪，
方向词就变成假话，而模型照着假话去"上方"找是找不到的。这一族在本仓有前科
（前端「上方」文案与断言不同步，CI 直接红）。这里用**结构**判：块渲染在规则正文
之前 ⇒ 引用只能写「上方」，之后 ⇒ 只能写「下方」，两边不一致即红。

⚠️ 判据只用**渲染后的字符串**，不读模板常量——模板可以随便重排，只要渲染出来的
前缀长度与方向词自洽。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as G  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def lcp(a: str, b: str) -> int:
    """最长公共前缀长度（= 缓存能命中的字节数）。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# 两组"另一轮"的值：每个易变块都不一样。里面刻意放了对**位置**敏感的长文本
# （页面上下文/工具帧本来就可能很长），免得前缀恰好被某个短值蒙对。
_VOLA = dict(page_ctx="页面A：/article/7（特效 sakura 开着）", round_info="当前决策：第 1/4 轮。",
             intent_hints="本句动作意图：开特效（未完成）", doc_anchors="已点名文档：19《架构文档》",
             recent_context="泠月：好呀～", short_reply_hint="这是短应答，承接上一轮的提议",
             pending_ledger="台账帧：talkId:101 访客（2026-09-29 22:10）「画板我已经回退掉了。」",
             tool_results="（本轮尚无工具执行）", ref_hints="search_notes: noteKey/title",
             reflector_feedback="（本决策轮无复盘建议）", correction="（本决策轮无纠偏提示）")
_VOLB = {k: v.replace("A", "B").replace("1", "2").replace("7", "8").replace("19", "46")
         for k, v in _VOLA.items()}
_VOLB.update(page_ctx="页面B：/dashboard", tool_results="get_article_detail 返回：正文……",
             ref_hints="list_notes: noteKey", recent_context="访客：那篇讲什么的？")


def _planner_pair():
    kw = dict(user_msg="帮我把樱花打开", contract=G._PLANNER_OUTPUT_CONTRACT_TEXT)
    return (G._render_planner_prompt("admin", **kw, **_VOLA),
            G._render_planner_prompt("admin", **kw, **_VOLB))


def _narrator_pair():
    kw = dict(persona=G.BLOG_ASSISTANT_PROMPT, audience="受众段", sticker_guide=G.STICKER_GUIDE)
    a = G._EXECUTOR_PROMPT.format(**kw, plan="SKILL=effect（第 1 轮）", tool_frames="（本轮尚无工具执行）",
                                 exec_receipts="（无）", fact_block="（本轮没有动作族执行）",
                                 page_ctx="页面A：/article/7")
    b = G._EXECUTOR_PROMPT.format(**kw, plan="SKILL=chat（第 2 轮）", tool_frames="工具返回：正文……",
                                 exec_receipts="读取文章 46《标题》", fact_block="· 跳转「/dashboard」",
                                 page_ctx="页面B：/dashboard")
    return a, b


# ── ① planner：固定文本整段落在稳定前缀里 ──────────────────────────────────
def test_planner_prefix_covers_rules():
    print("\n[planner] 稳定前缀必须盖住 技能菜单 + 工具菜单 + 判定规则正文")
    a, b = _planner_pair()
    n = lcp(a, b)
    check("两次渲染的易变块确实不同（否则下面的数字毫无意义）",
          a != b and n < len(a), f"len={len(a)} lcp={n}")
    # 规则正文的最后一条（6c）在渲染串里的位置：它落在前缀里 = 整段规则都被缓存
    last_rule = a.index("6c. 现时状态类询问")
    check("整段判定规则正文都在稳定前缀里（含最后一条 6c）",
          last_rule < n, f"6c 在 {last_rule}，前缀只到 {n}")
    check("稳定前缀 ≥ 30,000 字（菜单 20,387 + 规则 10,108 的量级）",
          n >= 30000, f"lcp={n}")
    # 反向：易变块**必须排在规则正文之后**（这才说明缓存前缀里没有它们）。
    # 比的是"块值的位置 > 规则最后一条的位置"，不是"块值的位置 > 前缀长度"——
    # 后者会被两组取值恰好相同的前缀字符（如都以"页面"开头）多算两个字符。
    tail = a.index("6c. 现时状态类询问")
    for name, val in (("页面上下文", _VOLA["page_ctx"]), ("工具帧", _VOLA["tool_results"])):
        check(f"{name} 排在规则正文之后（不进缓存前缀）",
              a.index(val) > tail, f"{name} 在 {a.index(val)}，规则最后一条在 {tail}")


# ── ② narrator：纪律块整段落在稳定前缀里 ──────────────────────────────────
def test_narrator_prefix_covers_disciplines():
    print("\n[narrator] 稳定前缀必须盖住 叙述纪律正文")
    a, b = _narrator_pair()
    n = lcp(a, b)
    check("两次渲染的易变块确实不同", a != b and n < len(a), f"len={len(a)} lcp={n}")
    last = a.index("19. 系统说")
    check("整段叙述纪律都在稳定前缀里（含最后一条 19）",
          last < n, f"19 在 {last}，前缀只到 {n}")
    check("稳定前缀 ≥ 5,000 字（纪律块 5,401 的量级）", n >= 5000, f"lcp={n}")
    check("[执行计划] 排在纪律正文之后（不进缓存前缀）",
          a.index("SKILL=effect（第 1 轮）") > last,
          f"计划在 {a.index('SKILL=effect（第 1 轮）')}，纪律最后一条在 {last}")


# ── ③ 方向词与真实位置自洽 ─────────────────────────────────────────────────
# 块名（规则正文里的指代词）→ 渲染串里该块的**表头**（固定文本，用它定位）。
# 表头一律取**行首**那一段（下面按 `"\n" + header` 找）：块名在规则正文里也会被
# 提到（如「下方"本会话已点名文档"」），按裸子串找会定位到那句引用上——位置错了，
# 判据却可能恰好判成"通过"（第一版就是这么写的，靠运气绿了一条）。
_BLOCKS = {
    "技能注册表": "技能注册表（唯一可选集合",
    "菜单": "本轮可规划执行的查询工具",
    "页面上下文": "当前页面上下文（前端实时上报",
    "动作意图清单": "用户消息里的动作意图清单",
    "本会话已点名文档": "本会话已点名文档（系统从对话历史",
    "可引用字段": "本轮已执行工具的**可引用字段**",
    "短应答提示": "短应答提示（当前消息只是",
    "复盘建议": "复盘建议（reflector",
}


def test_direction_words_match_real_positions():
    print("\n[方向词] 规则正文写「上方/下方 X」必须与 X 的真实位置一致")
    import re
    a, _b = _planner_pair()
    rules_at = a.index("判定规则：")
    # 规则正文区间：从「判定规则：」到 `{output_contract}` 那一版正文开头
    rules = a[rules_at:a.index(G._PLANNER_OUTPUT_CONTRACT_TEXT)]
    hits = 0
    for word, header in _BLOCKS.items():
        if "\n" + header not in a:            # 表头本身改了 ⇒ 这条判据要跟着改
            check(f"渲染串里找得到「{word}」块的表头", False, header)
            continue
        pos = a.index("\n" + header)
        want = "上方" if pos < rules_at else "下方"
        # 「上方X」「下方X」（中间允许 0–2 个引号/「」等装饰字符）
        for d in ("上方", "下方"):
            for m in re.finditer(d + r".{0,2}" + word, rules):
                hits += 1
                check(f"「{m.group(0)}」：{word} 实际在规则正文"
                      f"{'之后' if want == '下方' else '之前'} ⇒ 应写「{want}」",
                      d == want, f"{word} 块渲染在 {pos}，规则正文在 {rules_at}")
    check("方向词引用确实扫到了（不是零命中蒙混过关）", hits >= 6, f"命中 {hits} 处")


if __name__ == "__main__":
    for fn in (test_planner_prefix_covers_rules,
               test_narrator_prefix_covers_disciplines,
               test_direction_words_match_real_positions):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
