# -*- coding: utf-8 -*-
"""账乙（跨轮任务台账）接进判据侧的**只读通道 + 观测探针**（20261008 批 2）。

**为什么这条通道此前不存在**：台账原文（Rust 读回的 `agent_tasks`）只到两处——planner
的提示词（`render_open_tasks` 注入 system 上下文）与 producer 的流尾结算。narrator 与
gate **看不到它**。这不是"少一条判据"，是**少一个视角**：跨轮那本账是"还剩什么没做完"
的唯一载体，而"叙述对不对"正是拿它来对的（`tests/test_round_facts.py` 治的是同一件事的
**本轮**那一半，即账甲）。

**为什么这一批只观测、不设网**（探针的 docstring 里有完整理由，这里锁的是结论）：
这条网的前提 `tasks.settled_by_receipts(row, receipts)` 通常**已被洞⑮ 覆盖**——
能登记的行，步骤由 `Skill.plan` 模板推，而 navigate / effect / darkmode / device_display
这四族的工具**都在 `authz.WRITE_SCOPES` 里**（`write.page` / `write.device`）⇒ 行被结算
= 本轮有 checker PASS 的写回执 = 洞⑮ 的前提（`RoundFacts.ok_writes` 非空）也成立，
同一条回复会**先被洞⑮ 接住**（它前提更宽、判在 `_claim_issue` 里）。第 ③ 节把这条
"射程被覆盖"的关系锁成可执行断言：**同一个现场，洞⑮ 命中，而探针记的 `issue` 就是它**。

**真正的差额只有一支**：步骤**全是只读工具**的行（今天 = 后台只读那几族，
`admin.console` 在 `WRITE_SCOPES` 之外）——第 ④ 节锁它：洞⑮ **不**命中（`ok_writes` 空），
而探针记下分子。那一支**至今没有现场**（产线 979 份 trace 登记 0 次；这条通道
20261008 晚才随 `intents` 上线），所以现在设网就是**给判据发一个没有靶的准星**。
探针的分子/分母分开记（`clause` 空/非空、`issue` 是谁），攒出分子再决定要不要设网。

用法：.venv/bin/python tests/test_task_ledger_probe.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _row(tid: str, tools: list[str]) -> dict:
    """一行未完结任务（形状照 Rust 读回来的那几列，见 `tasks.task_rows`）。"""
    return {"task_id": tid, "goal": "把「多肉浇水」这件事办完", "state": "running",
            "cursor": 0, "total_steps": len(tools),
            "steps": [{"tool": t} for t in tools]}


def _rcpt(tool: str, ts: float = 1.0) -> dict:
    """一条 checker PASS 的回执。`ts > 0` 是硬要求（结算判据是 `ts > declared_after`，
    而 Rust 读回来的行 declared_after=0 ⇒ 夹具给 0.0 的话**永远不会被结算**）。"""
    return {"skill": "effect", "tool": tool, "args": {}, "result": "已执行", "ts": ts}


def _cfg(open_tasks) -> dict:
    return {"configurable": {"open_tasks": open_tasks}}


_DENIAL = "主人，那件事我这一轮还没办成，应该还在待处理队列里等着，要不要我再走一遍？"
_PLAIN = "主人，那件事已经办好了，你就放心吧～"

print("① 读入口的形状判据（只信 `tasks.task_rows`，别再写一份）")
check("正常输入 ⇒ 行列表", len(g._open_tasks_rows(_cfg(
    '[{"task_id": "t1", "steps": [{"tool": "toggle_effect"}]}]'))) == 1)
check("config 缺失 / 空 ⇒ 空表（不抛）",
      g._open_tasks_rows(None) == [] and g._open_tasks_rows({}) == [])
check("缺 `configurable` ⇒ 空表", g._open_tasks_rows({"x": 1}) == [])
check("坏 JSON ⇒ 空表（不阻断对话，形状判据在 `task_rows` 那一处）",
      g._open_tasks_rows(_cfg("{不是 JSON")) == [])
check("没有 task_id 的行被丢掉（`task_rows` 的既有口径）",
      g._open_tasks_rows(_cfg('[{"goal": "没有 id"}]')) == [])

print()
print("② 探针：分母记（结算了）／分子记（结算了**而且**回复说还没办）")
_tmp = tempfile.mkdtemp()
_SEQ = 0


def _probe(reply: str, state: dict, open_tasks, issue: str = "") -> dict | None:
    global _SEQ
    _SEQ += 1
    tid = f"tlp_{_SEQ:03d}"
    trace_mod.start_trace(tid, user_id=0, thread_id="t", dir=_tmp, by_day=False,
                          name="task_ledger_probe")
    try:
        g._task_ledger_probe(reply, state, _cfg(open_tasks), issue=issue)
        evs = [e for e in trace_mod.events_of(tid) if e.get("event") == "task_ledger_probe"]
        return evs[0] if evs else None
    finally:
        trace_mod.finish_trace(tid, "test", 0.0, 0)


_NAV_ROW = '[{"task_id": "t1", "goal": "带我去留言板", "state": "running", "cursor": 0,' \
           ' "total_steps": 1, "steps": [{"tool": "navigate_to"}]}]'
_SAME = {"receipts": [_rcpt("navigate_to")]}
_ev = _probe(_PLAIN, _SAME, _NAV_ROW)
check("分母：这一行被本轮回执结算掉了 ⇒ 记一条（`clause` 空、`issue` 空）",
      _ev is not None and _ev.get("settled") == ["t1"]
      and _ev.get("clause") == "" and _ev.get("issue") == "",
      str(_ev))
_ev = _probe(_DENIAL, _SAME, _NAV_ROW)
check("分子：结算了 + 回复说「还没办／还在等」⇒ 同一条事件带子句",
      _ev is not None and _ev.get("settled") == ["t1"] and bool(_ev.get("clause")),
      str(_ev))
check("没结算 ⇒ **不记**（一行都没推进的轮次不进 trace）",
      _probe(_DENIAL, {"receipts": []}, _NAV_ROW) is None)
check("没有台账行 ⇒ 不记（这一格是「账乙的前提出现频率」的分母，不含空台账）",
      _probe(_PLAIN, _SAME, "[]") is None)
check("另一行没被结算时不冒充（只记真结算的那些 id）",
      (_ev := _probe(_DENIAL, _SAME, '[{"task_id": "t9", "goal": "别的", "state":'
                                    ' "running", "cursor": 0, "total_steps": 1,'
                                    ' "steps": [{"tool": "create_tag"}]}]')) is None)
check("`clause` 走的是洞⑮ 同一条子句抽取器（族同源，不另写正则）",
      g._round_not_landed_clause(g._strip_quoted_spans(_DENIAL)) != "")

print()
print("③ 与洞⑮ 的关系（射程被覆盖那一格）：**同一条回复**两族都够得着 ⇒ 洞⑮ 先接住")
_issue = g._claim_issue(_DENIAL, "effect", g.parse_plan("SKILL=effect\nSTATUS=executed"),
                        True, False, False, receipts=[_rcpt("navigate_to")])
check("洞⑮ 命中（本轮有 checker PASS 的**写**回执：navigate_to 在 WRITE_SCOPES 里）",
      _issue is not None and _issue[0] == "round_not_landed", str(_issue and _issue[0]))
check("探针把 `issue` 一起记下来 ⇒ trace 里读得出「这一笔是不是已经被洞⑮ 接住了」",
      (_ev := _probe(_DENIAL, _SAME, _NAV_ROW, issue=_issue[0])).get("issue")
      == "round_not_landed", str(_ev))
check("**顺序**也锁住：探针在 `if issue:` 之前调用（否则洞⑮ 命中那几笔一个都不进 trace，"
      "分母会被截断成「只有没被接住的那些」）",
      (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
      .index("_task_ledger_probe(reply, state, config")
      < (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
      .index("if issue:\n        i_name, i_text, i_clause = issue"))

print()
print("④ 差额那一支：步骤**全是只读工具**的行 ⇒ 洞⑮ 够不着，而探针记分子")
_ADMIN_ROW = ('[{"task_id": "t2", "goal": "看看服务和文章统计", "state": "running",'
              ' "cursor": 0, "total_steps": 1, "steps": [{"tool": "get_server_status"}]}]')
_ADMIN = {"receipts": [_rcpt("get_server_status")]}
_i2 = g._claim_issue(_DENIAL, "admin_stats", g.parse_plan("SKILL=chat\nSTATUS=answer_only"),
                     True, False, False, receipts=[_rcpt("get_server_status")])
check("洞⑮ **不**命中（后台只读不吃 `WRITE_SCOPES` ⇒ `ok_writes` 空 ⇒ 前提取不到）",
      _i2 is None, str(_i2))
check("`authz.is_write('get_server_status')` 确为 False（上面那条的根据，不是猜的）",
      not g.authz.is_write("get_server_status"))
check("`authz.is_write('navigate_to')` 确为 True（②③ 节那条覆盖关系的根据）",
      g.authz.is_write("navigate_to"))
_ev = _probe(_DENIAL, _ADMIN, _ADMIN_ROW)
check("**这一支就是本批观察到的东西**：行被结算、回复说没办、洞⑮ 无网 ⇒ 探针是唯一读数",
      _ev is not None and _ev.get("settled") == ["t2"] and bool(_ev.get("clause")),
      str(_ev))

print()
shutil.rmtree(_tmp, ignore_errors=True)
if FAILED:
    print(f"❌ {len(FAILED)} 条未通过：")
    for _n in FAILED:
        print(f"   - {_n}")
    sys.exit(1)
print("✅ 全部通过")
