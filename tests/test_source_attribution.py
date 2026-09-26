#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源归属（20260926 洞⑧）：系统给的事实，不许说成"我自己脑补的"。零网络零 LLM。

**这一条为什么单独成文。** 现场（trace `20260926T171235`，会话 247）：主人说「给 xinguan
什么的用户发个测试通知」，系统**确定性**核对后台账号名录，回了一句「没有叫「xinguan」的
账号…名字最接近的是 id=10 的「xinguanyu」」（真读数）；两分钟后主人追问「你是从哪知道的
xinguanyu」，模型答「**是我自己脑补的**…本轮的工具执行记录是空的」——把**有据**的事实
说成了无据。

根因不是模型退化，是纪律里缺了这一维：诚实底线此前只写「本轮没有工具返回时**不得声称**
查过/读过/打开过」——它管的是"别把没有的说成有"，而模型把它**反向**套到了"上一轮系统自己
写下的核对结论"上，于是"别吹"变成了"自我否认"。两条都叫不诚实，方向相反。修法分两处，
本套件锁的就是这两处**都在、且真的接上了**：

  1. `prompts.BLOG_ASSISTANT_PROMPT` 的诚实底线后补的来源归属规则；
  2. `graph.py` 那条确定性收尾注（`系统核对过{subject}…`）末尾的同义括注——
     它此前**只列禁止的说法、不给允许的说法**，模型在"不许说查过"和"不许说没查过"
     之间没有出口，只能自己编一个。

**纪律（同族的教训）**：措辞族在这儿是**松的**——只锁语义锚点（"来源说系统"、"照原样
转述"、"不许说成脑补"），不锁整句话。行为侧的判据在 golden `admin_near_miss_source_honest`
（真模型、两轮、history 逐字带那段系统核对文本），这儿锁的是**接线**：句子被删掉/被
改写掉语义时立刻红，而不是等某天夜里模型又答一次"我脑补的"。

用法：.venv/bin/python tests/test_source_attribution.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from agent import graph as g  # noqa: E402
from agent.prompts import BLOG_ASSISTANT_PROMPT  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════
print("\n① 诚实底线里两句话都在（「不得声称」与「不许自我否认」是两回事）")

_core = BLOG_ASSISTANT_PROMPT
check("原有的那一维没被顶掉：本轮没有工具返回时不得声称查过/读过/打开过/执行过",
      "本轮没有工具返回时不得声称查过/读过/打开过/执行过" in _core)
check("  质疑操作是否真执行时如实承认没有执行记录（绝不圆场说其实已经做了）",
      "这边没有看到执行记录" in _core and "其实已经做了" in _core)
check("新增的一维：**系统给过的东西**（工具帧/系统核对结论/页面上下文/上一轮的核对结果）",
      "系统给过的东西" in _core and "上一轮系统" in _core)
check("  并且点名了要允许的说法：来源就说系统（两个例子逐字在）",
      "来源就说系统" in _core and "系统核对过后台账号列表" in _core)
check("  且明写**不许**说成脑补/编的/猜的（这是这一维的反面）",
      "脑补" in _core and "我编的" in _core and "我猜的" in _core)
check("  零系统来源时的出口仍只是「这一轮我没看到」，不是自我否认代替核对",
      "这一轮我没有看到" in _core and "不要用自我否认代替核对" in _core)

# ══════════════════════════════════════════════════════════════════
print("\n② 接线：那条规则真的进了 narrator 的 system prompt（能力有测试≠接线有测试）")

_sys = g._EXECUTOR_PROMPT.format(
    persona=BLOG_ASSISTANT_PROMPT, audience="【对话者：访客】",
    plan="（本轮没有计划）", tool_frames="（本轮没有工具返回）",
    exec_receipts="（无）", page_ctx="（无）", sticker_guide="（无）")
check("narrator 的 system prompt 里含来源归属这条（不是只在常量里躺着）",
      "系统给过的东西" in _sys and "来源就说系统" in _sys)
check("  也含原有的那一维（两维同在，不是替换关系）",
      "本轮没有工具返回时不得声称查过" in _sys and "这边没有看到执行记录" in _sys)
check("  人设段确实是拼进 narrator prompt 的那一份（`persona=` 槽没接错常量）",
      BLOG_ASSISTANT_PROMPT.strip()[:30] in _sys)
check("  且 `.format` 的槽都填上了（漏一个槽会留一串花括号文本给模型看）",
      "{" not in _sys and "}" not in _sys)

# ══════════════════════════════════════════════════════════════════
print("\n③ 确定性收尾注也给出口（禁止句旁边必须有允许的说法）")

_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("注记里那句禁止句仍在（洞⑦ 的判据靠它，别被这轮的括注挤掉）",
      "不许**出现「看过/读过/查过/检索过/调用过工具」这类说法" in _src)
check("  紧跟着给了允许的说法：禁的是把系统的动作说成你做的",
      "禁的是把**系统的动作**说成你做的" in _src)
check("  并点明「照原样转述」与「也**不许**反过来」（两个方向都写全）",
      "照原样转述" in _src and "也**不许**反过来把它说成" in _src)
check("  且**排在禁止句之后**（注记是从上往下读的一段话，出口不能落在前面）",
      _src.index("不许**出现「看过/读过/查过/检索过/调用过工具」这类说法")
      < _src.index("禁的是把**系统的动作**说成你做的"))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
