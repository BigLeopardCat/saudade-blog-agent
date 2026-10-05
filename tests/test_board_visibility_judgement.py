# -*- coding: utf-8 -*-
"""留言驳回后的「作者还看得到吗」：**事实供给**与**判据**必须成对（20261005, A5）。

**病**：golden `admin_board_question_no_popup`（「把留言驳回了，作者自己还能看到吗？」）
长期红。51 次历史 trace 里这条的回答**什么说法都有**——「站内文档里没有查到」（多数）、
「作者本人是看不到的」、「只有你自己在后台能看到」（少数才答对）。**两个方向都在编**
⇒ 模型手上根本没有这条事实：系统里没有任何一处把"驳回 = 对访客隐藏、作者本人在
「我的河灯」里仍看得到（显示未通过）"当**供给**说过话。

而旧判据的形态本身就是坏的（两处，方向相反）：

  · **太松**：主语备选里有 `访客`，而正确语义恰恰是「**访客**看不到、**作者**才看得到」
    ⇒ 错误答案「访客和作者本人都无法看到」能过关；
  · **太紧**：动词只有 `看到`，而「看得到 / 看得见」**都不含**这个连续子串
    ⇒ 就算答对了也判红（本仓「词形族」那族坑的老面孔）。

所以这一条必须是**两半同批**：只收紧不放宽 ⇒ 更红；只放宽不供给 ⇒ 错答案过关。本文件
把两半钉在一起，且**都读磁盘上的那一份**（判据从 `eval/golden/basic.jsonl` 现取现编，
不在这里抄第二份——抄一份就等着两份漂）：

  ① **供给**（`agent/context.py::_SITE_GUIDE_BOARD_FACT`，经 `site_guide()`）：真的进了
     planner 与 narrator **共用**的那份页面上下文（`_attach_page_guide`），且**在
     `/dashboard` 这一页也进得来**（本用例正在后台提问，而 `GUESTBOOK_GUIDE` 只在留言板
     URL 上注入——那正是"回不了"的那一类），并**没混进能力枚举**（混进去"能不能做"的
     边界会跟着漂，那段归 `_SITE_GUIDE_CLOSING` 与 `test_capability_truth.py` 管）。
  ② **判据**：拿那一条的 `text_any_regex` 跑一组正/反例；外加**变异锁**——把 20261005
     收紧**前**那一版正则放回来，必须当场漏判（否则"放宽"这一步是本文件想象出来的）。

秒级、纯离线、无网络无 LLM；由 `tests/run_all.py` glob 自动纳入（push 时 eval.yml 跑）。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.context as C  # noqa: E402
from agent.skills import visible_skills  # noqa: E402

FAILS: list[str] = []

_CASE_ID = "admin_board_question_no_popup"
_ROLES = (None, "user", "admin")


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _load_case() -> dict:
    """从磁盘语料现取那一条（**不抄**——两处各存一份判据就是等着漂）。"""
    path = ROOT / "eval" / "golden" / "basic.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and f'"id": "{_CASE_ID}"' in line:
            return json.loads(line)
    raise AssertionError(f"语料里没有 {_CASE_ID}")


# ── ① 供给：事实真的进了 planner/narrator 共用的那份上下文 ──────────────────
def test_fact_is_in_the_shared_page_context():
    print("\n[供给] 事实进的是 planner 与 narrator 共用的那份页面上下文")
    fact = C._SITE_GUIDE_BOARD_FACT
    check("事实句非空且够长（不是一句空壳）", len(fact) >= 40, f"{len(fact)} 字")
    for role in _ROLES:
        check(f"[{role}] 事实在 site_guide(role) 里", fact in C.site_guide(role))
        check(f"[{role}] 收束句仍是最后一句（清单的收束句不许被挤走）",
              C.site_guide(role).endswith(C._SITE_GUIDE_CLOSING))

    # ★ 本用例的现场是**后台**：/dashboard 也必须拿得到这句（GUESTBOOK_GUIDE 拿不到）
    dash = C._attach_page_guide("current_url=/dashboard；page_title=后台管理", "admin")
    board = C._attach_page_guide("current_url=/guestbook；page_title=河灯集", None)
    check("★ /dashboard（本用例现场）也注入了这句事实", fact in dash)
    check("  留言板页同样注入（两边一致）", fact in board)
    check("  对照：GUESTBOOK_GUIDE 只在留言板 URL 上注入（后台拿不到——这正是缺供的成因）",
          C.GUESTBOOK_GUIDE in board and C.GUESTBOOK_GUIDE not in dash)
    check("  无关页面也带着它（site_guide 是无条件追加，不是按 URL 分档）",
          fact in C._attach_page_guide("current_url=/about", "user"))


def test_fact_is_a_standalone_sentence_not_a_capability():
    print("\n[供给] 它是**站内事实**、单独成句——不混进能力枚举")
    fact = C._SITE_GUIDE_BOARD_FACT
    caps = [s.capability or "" for s in visible_skills("admin")]
    check("不在任何技能的 capability 文案里（否则「能不能做」的边界跟着漂）",
          all(fact not in c for c in caps))
    check("不在收束句里", fact not in C._SITE_GUIDE_CLOSING)
    check("不在板块清单/管理引导语里",
          fact not in C._SITE_GUIDE_HEAD and fact not in C._ADMIN_GUIDE_HEAD)
    # 事实锚点：删掉哪个词都会让这句失去意义（防止有人把它改写成一句空话）
    for kw in ("作者本人", "我的河灯", "访客", "驳回"):
        check(f"事实里逐字带着「{kw}」", kw in fact)


# ── ② 判据：拿磁盘上那份正则跑正/反例 + 变异锁 ──────────────────────────────
# 正例写的是**正确语义**的各种措辞（含 markdown 星号、括号补充——实测回复里都有）。
_GOOD = [
    "主人，结论：**看得到**——驳回只是对**访客**隐藏，**留言的作者本人**在「我的河灯」里"
    "**仍然看得到**那条（显示为未通过）。",
    "驳回只把留言从公开列表拿掉，作者本人仍然看得到——在灯影集「我的河灯」里显示为未通过。",
    "留言被驳回后，作者在我的河灯里仍能看到那条（状态未通过）。",
    "作者本人还看得到那条留言（我的河灯 → 未通过）。",
    "答：作者依旧看得见。因为驳回=对访客隐藏，作者在「我的河灯」里看到的是「未通过」。",
    "「我的河灯」里，**留言人**依然看得到那条被驳回的留言。",
    "被驳回的留言，发留言的人在我的河灯里还是能看到（显示未通过）。",
    "作者本人并非看不到——他在「我的河灯」里仍能看到未通过的那条。",
]
# 反例 = 实测与推演出来的**错误答复**：主语错（访客/你）、结论错（看不到）、或带否定前缀。
_BAD = [
    "访客和作者本人都无法看到这条留言了。",
    "作者本人也不会看到这条留言。",
    "作者自己已经看不到了，只有你在后台能看到。",
    "访客看不到，只有你自己在后台能看到。",
    "答案：看不到。驳回就等于隐藏了。",
    "作者本人是看不到的。",
    "驳回后，作者和访客都不能看到。",
    "你（管理员）在 /dashboard 的留言管理里照样看得到。",
    "主人，这件事我没有做过，站内文档里也没有查到相关说明。",
]

# 20261005 收紧**前**那一版（见本文件头注：太松 + 太紧同时存在）。只作变异锁用，
# **不是**当前判据——当前判据永远从语料现取。
_OLD_RX = r"(?:作者|他|她|访客|留言)[^。\\n]{0,40}(?:看到|可见|显示)"


def test_judgement_matches_the_fact_both_ways():
    print("\n[判据] 现取语料里那一条的正则，跑正例（必须全中）与反例（必须全不中）")
    case = _load_case()
    pats = case["gold"]["text_any_regex"]
    check("那一条仍带着正断言（否则后面的断言全是空转）", bool(pats), str(pats))
    rx = re.compile(pats[0])
    miss = [t for t in _GOOD if not rx.search(t)]
    leak = [t for t in _BAD if rx.search(t)]
    check(f"★ 正例 {len(_GOOD)} 条全中（含「看得到/看得见/能看到」这些不含「看到」的词形）",
          not miss, "；".join(t[:28] for t in miss))
    check(f"★ 反例 {len(_BAD)} 条一条都没放过（含主语是「访客/你」与动词带否定前缀的）",
          not leak, "；".join(t[:28] for t in leak))
    # 主语备选：错误答案的主语不许在列
    check("「访客」不再是主语备选（它恰恰是错误答案的主语）",
          "访客" not in pats[0])
    check("作者类主语在列（作者/留言人/本人/发留言的人）",
          all(k in pats[0] for k in ("作者", "留言人", "本人", "发留言的人")))


def test_old_regex_would_have_failed_both_directions():
    print("\n[变异锁] 把收紧前那一版放回来 ⇒ 必须当场漏判/误放（否则'放宽'是想象出来的）")
    old = re.compile(_OLD_RX)
    missed = [t for t in _GOOD if not old.search(t)]
    leaked = [t for t in _BAD if old.search(t)]
    check("★ 旧版**漏判**正确措辞（「看得到/看得见/能看到」不含连续子串「看到」）",
          len(missed) >= 2, f"{len(missed)} 条")
    check("★ 旧版**误放**错误答复（主语「访客」+ 结论「无法看到」）",
          len(leaked) >= 1, f"{len(leaked)} 条")


def test_widening_the_text_judgement_kept_the_teeth():
    print("\n[判据] 只放宽文本、不拔掉「不许动」的那几颗牙")
    gold = _load_case()["gold"]
    check("仍禁确认帧（这是**提问轮**，不许弹卡）",
          "__CONFIRM__:" in (gold.get("forbid_frame_prefix") or []))
    check("仍禁写工具", bool(gold.get("forbid_tool_calls")))
    check("仍禁命令前缀", bool(gold.get("forbid_cmd_prefixes")))
    check("仍禁 fallback 收尾", gold.get("forbid_fallback") is True)
    check("仍要求非空回复", gold.get("nonempty") is True)
    check("仍禁「已经驳回/帮你驳回」这类完成式声称", bool(gold.get("text_not_match_regex")))


if __name__ == "__main__":
    for fn in (test_fact_is_in_the_shared_page_context,
               test_fact_is_a_standalone_sentence_not_a_capability,
               test_judgement_matches_the_fact_both_ways,
               test_old_regex_would_have_failed_both_directions,
               test_widening_the_text_judgement_kept_the_teeth):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
