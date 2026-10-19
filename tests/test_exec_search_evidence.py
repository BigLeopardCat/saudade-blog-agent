# -*- coding: utf-8 -*-
"""跨轮"检索痕迹"判据改读结构（20261020，批 G 第二段 ①）。

被测 = `agent/graph.py::_exec_memory_has_search`：gate 判"站内『没有』"时用的
`search_evidence`（有据 ⇒ 不下 fallback）。

**为什么改**（这段理由从代码注释里搬来的，代码那边只留一句）：

  ① 旧判据对**注入的那串渲染回执行**跑正则 `站内检索「|搜索「`。而
     `agent/action_text.py` 把 `search_notes` 渲染成「检索文章「…」」之后，该正则
     **再也匹配不上它** ⇒ 判据静默失效：上一轮真检索过、这一轮说"站内没有 X"，
     本该豁免却被判 fallback。
  ② 同一条正则连 **narrator 自己上一轮的措辞**、引述与访客留言正文里的同一串字都
     算证据 ⇒ **自证**：上一轮说过"我搜了"，这一轮就可以据此下"站内没有"的结论。

改后读 `config["configurable"]["recent_tools"]`（Rust 给的**工具名**，本会话最近 40
条回执、去重、上限 8）——**与注入的那串回执行同源**，"上一轮真检索过"这一格没少，
少掉的只有 narrator 散文那一半。

判据分三层，每层都配了**会红的反控**：
  ① 纯函数：工具名 → 真/假；
  ② gate 集成：结构里有检索 ⇒ pass；**台账文本有检索行、结构里没有 ⇒ fallback**
     （自证不再算依据——这条在改前是绿的）；
  ③ 接线：`graph.py` 里每个调用点的实参都是 `config`（有人改回 messages ⇒ 红）。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from agent.graph import (  # noqa: E402
    _FALLBACK_SITE_ABSENCE,
    _exec_memory_has_search,
    gate_node,
    instantiate_plan,
    plan_state,
)

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _cfg(tools) -> dict:
    return {"configurable": {"recent_tools": list(tools)}}


# ── ① 纯函数：读的是工具名 ────────────────────────────────────────────────────
print("① `_exec_memory_has_search`：工具名 → 真/假")

check("search_notes ⇒ 真（渲染成「检索文章「…」」后旧正则漏掉的那一格）",
      _exec_memory_has_search(_cfg(["search_notes"])) is True)
check("rag_search ⇒ 真", _exec_memory_has_search(_cfg(["rag_search"])) is True)
check("混在一串非检索工具里也认得出",
      _exec_memory_has_search(_cfg(["navigate_to", "rag_search", "list_notes"])) is True)
check("只有读取类工具 ⇒ 假",
      _exec_memory_has_search(_cfg(["get_article_detail", "list_notes"])) is False)
check("空列表 = 确定地什么都没执行 ⇒ 假", _exec_memory_has_search(_cfg([])) is False)
check("键缺席 ⇒ 假（旧 Rust 不发这个键）",
      _exec_memory_has_search({"configurable": {}}) is False)
check("config 是 None ⇒ 假（不炸）", _exec_memory_has_search(None) is False)
check("脏值（非字符串）不许当真", _exec_memory_has_search(_cfg([1, None, ""])) is False)

# ── ② gate 集成：证据来自结构，不来自那串文本 ──────────────────────────────
print("② gate：同一个『站内没有』结论，证据在哪一半决定 pass/fallback")

ABSENCE = "这个站里没有写过讲 async/await 的文章呢，不过原理本身我可以给你讲讲喵"
# 旧判据认的那行文本（Rust render_exec_row 当时的定稿措辞，逐字）
LEDGER_TEXT = ("确认与执行事实（系统台账）: · 已执行（系统验收过）: "
               "09-21 10:00 站内检索「Rust async」 — 2 篇")


def _state(extra: list, workflow: str = "chat") -> dict:
    return {**plan_state(instantiate_plan(workflow, {})), "done": False, "plan_rounds": 1,
            "messages": [HumanMessage(content="写过 async 的教程吗")] + extra}


def _run(state: dict, config: dict | None = None) -> dict:
    """gate 的终局形态：落兜底的直接用；落重规划的按"那次机会已用掉"再判一遍。"""
    out = gate_node(state, config)
    if isinstance(out, dict) and out.get("gate_replan") and not out.get("done"):
        out = gate_node({**state, "gate_replan": True}, config)
    return out


o_struct = _run(_state([AIMessage(content=ABSENCE)]), _cfg(["search_notes"]))
check("结构里有 search_notes ⇒ pass（改前这一格恒 fallback：正则认不出新措辞）",
      o_struct["done"] is True and not o_struct.get("fallback_text"),
      str(o_struct.get("fallback_text"))[:60])

o_prose = _run(_state([SystemMessage(content=LEDGER_TEXT), AIMessage(content=ABSENCE)]),
               _cfg([]))
check("★ 反控：台账文本里有检索行、结构里没有 ⇒ fallback（自证不再算证据）",
      o_prose["done"] is True and o_prose.get("fallback_text") == _FALLBACK_SITE_ABSENCE,
      str(o_prose.get("fallback_text"))[:60])

o_none = _run(_state([AIMessage(content=ABSENCE)]))
check("完全没有 config（离线老调用点）⇒ fallback（不静默放宽）",
      o_none["done"] is True and o_none.get("fallback_text") == _FALLBACK_SITE_ABSENCE)

# ── ③ 接线：调用点必须把 config 交进去 ─────────────────────────────────────
print("③ 接线锁：`graph.py` 里每个调用点的实参都是 `config`")

_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_calls = [n for n in ast.walk(ast.parse(_src))
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "_exec_memory_has_search"]
_args = [ast.get_source_segment(_src, c.args[0]) for c in _calls if c.args]
check("调用点取到了（防止选择器写错 ⇒ 判据恒绿）", len(_calls) >= 2, f"{len(_calls)} 处")
check("每个实参都是 `config`（改回 messages/窗口 ⇒ 红）",
      bool(_args) and all(a == "config" for a in _args), str(_args))

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
    sys.exit(1)
print("全部通过 ✅")
