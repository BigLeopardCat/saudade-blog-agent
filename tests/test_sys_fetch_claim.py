# -*- coding: utf-8 -*-
"""零帧轮的"第三人称系统取数"声称（gate，20260928）。

**为什么单起一套**：这条判据的射程是"叙述里的**第二个施事**"。此前的声称族全是第一
人称（"我用了 X 工具""我读过""我翻了一圈"）——主语换成一个**系统**，句法上就躲开了
每一个模式。事故实证（trace `20260928T032502`，用户全程看着）：主人问「97删了」，
planner 判 chat（**本轮 execute 零事件**），narrator 却回"**刚才系统重新拉了一次留言板**，
返回的最近 21 条里已经没有 97 了"；下一轮（`20260928T032549`）又说"**这一轮**系统重新
拉回的留言板列表里，没有这条"。两句都在**声称一个没有发生的取数动作**，还拿它当
"那东西真的不在了"的证据——比第一人称版本更毒：它不是"我查过"，是**伪造证据**。

本套件锁四条：
  ① 正例必中（两条真实事故原句 + 两条同族变体）；
  ② 负例必不中——**据实转述跨轮执行记忆**（rule 6a 的合法形态，"系统记录里最近一次
     查看留言板是 03:23"）、假设句（"如果系统重新拉一次"）、否定句（"系统没有重新拉"）
     都必须放行：零帧轮误伤的代价是**整轮回复被 fallback 吞掉**（本仓一贯的取向：
     宁漏勿误伤）；
  ③ 接线锁：判据真的挂在**零帧路径**上（有帧轮不查——有帧轮的"系统做过"有回执撑着，
     那里另有一族判据管"谎称没执行"）；
  ④ 兜底文案只否认**被点名的那件事**（同 `_FALLBACK_PHANTOM_CLAIM` 的教训：断言
     "这一轮什么都没发生"会被回执打脸）。

用法：.venv/bin/python tests/test_sys_fetch_claim.py
"""
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
print("\n① 正例：本轮的「系统又取了一次数」必判（零帧轮里都是编的）")

POS = [
    # 事故原文（trace 20260928T032502 / 032549）
    "刚才系统重新拉了一次留言板，返回的最近 21 条里已经没有 97 了",
    "这一轮系统重新拉回的留言板列表里，没有这条",
    # 同族变体（换施事/换动词/换宾语）
    "后台又刷新了一遍通知列表",
    "系统重新加载了数据",
    "站内又同步了一次记录",
    # 第一人称的同族（"我刚重新查了一遍站内列表"）——`_CHAT_TOOL_CLAIM_RE` 接不住
    # 泛指取数（它只认点名工具/说"工具"），故并入同一条判据
    "我刚才重新查了一遍站内列表，还是没有",
]
for t in POS:
    check(f"必判：{t[:24]}…", G._sys_fetch_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n② 负例：据实转述 / 假设 / 否定 / 宾语不是取数对象 —— 一律放行")

NEG = [
    # 据实转述跨轮执行记忆（rule 6a）：**必须留的路**
    "系统记录里最近一次查看留言板是 03:23，当时它有 21 条",
    "系统执行记录里写着「查看留言板」，那是 03:23 的事",
    # 假设/条件句
    "如果系统重新拉一次列表，就能看到它了",
    "要是系统重新拉一遍，说不定它又回来了",
    # 否定句
    "这一轮系统没有重新拉列表",
    "不是系统重新拉的，是上一轮那条记录",
    # 宾语不是取数对象（"消息/留言"是内容不是台账——"我又看了一遍你的消息"）
    "我又看了一遍你的消息",
    "我刚才重新读了一遍你那条留言",
    # 完成态缺失（"重新拉一次"没有"了/过/一遍"以外的完成标记时不算声称）
    "我随时可以重新拉一次列表给你看",
]
for t in NEG:
    check(f"放行：{t[:24]}…", not G._sys_fetch_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n③ 接线锁：判据挂在**零帧路径**上，有帧轮不查")

_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")


def _fn_src(name: str) -> str:
    """取某个顶格函数的函数体（到下一个顶格 def 为止）。"""
    at = _SRC.index(f"def {name}(")
    body = _SRC[at:]
    return body[:body.index("\ndef ", 10)]


# 20260928 架构规范化 ③ 起，零帧轮的声称族**写成一张表**（`_zero_frame_families`），
# 由 `_claim_issue` 单循环过。判定的内容（谓词/子句/豁免/兜底）全在表里 ⇒ 接线锁跟着
# 搬家：**族在不在表里** + **表是不是在 `if frames_exist` 之后才过**。
_BODY = _fn_src("_zero_frame_families")
_RUNNER = _fn_src("_claim_issue")
check("这一族在零帧族表里（判定写在一处，不再往 `_claim_issue` 里手抄一段）",
      "_sys_fetch_claim" in _BODY and "_sys_fetch_claim_clause" in _BODY)
check("  且 `_claim_issue` 过表排在 `if frames_exist: return None` **之后**（有帧轮不查）",
      _RUNNER.index("if frames_exist") < _RUNNER.index("_zero_frame_families("))
check("  子句版也接了（trace 要能指出判的是哪句话）",
      "_sys_fetch_claim_clause" in _BODY)
check("  返回的原因码是 `sys_fetch_claim_without_tool`",
      '"sys_fetch_claim_without_tool"' in _BODY)
# 行为锁（比子串锁抗重构）：这一族**不吃**回执豁免——豁免放的是"追述"（"记录里那次…"），
# 而本条说的必是本轮。传 exec_memory=True（乃至 exec_search_evidence=True）也必须照判。
_CLAIM_TEXT = "刚才系统重新拉了一次留言板，返回的最近 21 条里已经没有 97 了"
check("  传 exec_memory=True 也照判（这一族不吃回执豁免）",
      (lambda r: bool(r) and r[0] == "sys_fetch_claim_without_tool")(
          G._claim_issue(_CLAIM_TEXT, "chat", {"note": "", "status": ""}, False,
                         exec_memory=True, exec_search_evidence=True)),
      str(G._claim_issue(_CLAIM_TEXT, "chat", {"note": "", "status": ""}, False,
                         exec_memory=True)))
check("兜底文案存在且只否认被点名的那件事（不许说「这一轮什么都没有发生」）",
      bool(G._FALLBACK_SYS_FETCH_CLAIM)
      and "没有再取一次数据" in G._FALLBACK_SYS_FETCH_CLAIM
      and "没有任何工具执行" not in G._FALLBACK_SYS_FETCH_CLAIM)
check("  （对照）第一人称那条的文案确实说的是「没有任何工具执行」——两条刻意不同形",
      "没有任何工具执行" in G._FALLBACK_SEARCH_CLAIM)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
