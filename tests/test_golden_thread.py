# -*- coding: utf-8 -*-
"""golden 的线程坐标（20261018 批 6）：**每一条用例、每一遍、每一条臂各一条线程**。

**它防的是什么**：`eval/run_golden.py` 此前给每一轮传的是**常量** `"golden_thread"`。
今天没后果（两边都不挂 saver，`thread_id` 是死参数），但接上 saver 之后，常量线程意味着
**所有用例共用同一条线程** ⇒ 用例 N+1 恢复用例 N 的 state，而症状是"模型退化了"。
ADR-0011 把它写成「不复位就是假读数」，驱动侧已经照做——**但那个复位只活在驱动里**，
不经驱动的跑法（夜间、手跑、`golden_case_runner`）没有这一层。本件钉的是：复位**不必需**，
因为名字里本来就带着坐标。

五节：
  ① **三条不变式**：同坐标恒同名 / 不同用例恒不同名 / 臂·遍任一不同 ⇒ 名不同。
     "不同用例恒不同名"是本件的本体，另两条保证复位与写入对得上。
  ② **归一化**（`_slug`）：用例 id 里的 `.`/`-`/空格折成 `_`，空串折成 `x`。
     名字会原样进 PG 的 `thread_id` 列，而复位是 `DELETE ... WHERE thread_id = %s`
     ——**两处必须是同一个字符串**。
  ③ **语料上没有撞名**：把 `eval/golden/basic.jsonl` 的**全部用例 id** 过一遍 `_slug`，
     两两必须不同。归一化会把不同的 id 折成同一个 slug（`a.b` 与 `a-b` 都变成 `a_b`），
     那是**真实存在**的失效面——所以这条判据不靠"应该不会撞"的直觉，而是拿真语料去量。
     哪天有人加了一条 id 会撞的用例，这里当场红，而不是等到某晚读数漂了去查模型。
  ④ **接线锁（AST）**：`eval/run_golden.py` 里 `run_one(` 必须带 `thread=` 且值是
     `golden_thread.for_case(`；传给 `_run_agent_stream_to_queue` 的那个位置参数必须是
     `thread` 这个名字；且该文件里**不再**出现 `"golden_thread"` 这个字面常量。
  ⑤ **接线锁（AST）**：`eval/golden_trace.py` 落进 trace 的 `thread_id` 必须是**形参**，
     不是字面——否则 trace 里那一格与图实际用的线程不一致，而"线程有没有复用"要读它。

用法：.venv/bin/python tests/test_golden_thread.py
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import golden_thread as gt  # noqa: E402

FAILED: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {desc}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(desc)


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _no_rep(monkey: str = "") -> None:
    """把 `GOLDEN_REP` 拨到 `monkey`（空串 = 没设）。"""
    import os
    if monkey:
        os.environ[gt.ENV_REP] = monkey
    else:
        os.environ.pop(gt.ENV_REP, None)


print("① 三条不变式")
_no_rep()
_A = gt.for_case("case_a", arm="graph")
check("同一个坐标 ⇒ 恒同一个名字（复位与写入对得上）",
      _A == gt.for_case("case_a", arm="graph"), _A)
check("**不同用例 ⇒ 不同名字**（本件的本体：跨用例不串）",
      _A != gt.for_case("case_b", arm="graph"),
      f"{_A} / {gt.for_case('case_b', arm='graph')}")
check("不同臂 ⇒ 不同名字（三条臂读同一个库，撞了就是两臂混一条线程）",
      _A != gt.for_case("case_a", arm="react"),
      f"{_A} / {gt.for_case('case_a', arm='react')}")
check("不同 suffix（重跑那一轮）⇒ 不同名字",
      _A != gt.for_case("case_a", arm="graph", suffix="__rerun"),
      f"{_A} / {gt.for_case('case_a', arm='graph', suffix='__rerun')}")
_no_rep("3")
check("不同遍（`GOLDEN_REP`）⇒ 不同名字（逐遍跑不许在上一遍的 state 上接着跑）",
      _A != gt.for_case("case_a", arm="graph"),
      f"{_A} / {gt.for_case('case_a', arm='graph')}")
check("（对照）同一遍 ⇒ 还是同一个名字（上一条不是「凡设了 rep 就变」）",
      gt.for_case("case_a", arm="graph") == gt.for_case("case_a", arm="graph"))
_no_rep()
check("（对照）`GOLDEN_REP` 不设 ⇒ 名字里没有 `r` 段（手跑不分遍）",
      "_r" not in _A and not _A.endswith("_r"), _A)

print()
print("② 归一化：名字会原样进 PG 的 `thread_id` 列，两处必须是同一个字符串")
check("`.` / `-` / 空格一律折成 `_`",
      gt.for_case("a.b-c d", arm="graph") == "golden_graph_a_b_c_d",
      gt.for_case("a.b-c d", arm="graph"))
check("空用例 id ⇒ `x`（不编一个像用例名的东西顶上）",
      gt.for_case("", arm="graph") == "golden_graph_x",
      gt.for_case("", arm="graph"))
check("臂名也归一化（`native+react` 那种带 `+` 的）",
      gt.for_case("c", arm="native+react") == "golden_native_react_c",
      gt.for_case("c", arm="native+react"))
check("名字里只有 `[0-9A-Za-z_]`（`DELETE ... WHERE thread_id = %s` 的两侧同一个串）",
      all(ch.isalnum() or ch == "_" for ch in gt.for_case("a.b-c d", arm="n+r")),
      gt.for_case("a.b-c d", arm="n+r"))

print()
print("③ 真语料上没有撞名（归一化会把不同的 id 折成同一个 slug，那是真实的失效面）")
_cases = [json.loads(ln) for ln in
          (ROOT / "eval" / "golden" / "basic.jsonl").read_text(encoding="utf-8").splitlines()
          if ln.strip()]
_ids = [c["id"] for c in _cases]
_names: dict[str, list[str]] = {}
for _i in _ids:
    _names.setdefault(gt.for_case(_i, arm="graph"), []).append(_i)
_collide = {k: v for k, v in _names.items() if len(v) > 1}
check(f"{len(_ids)} 条用例的线程名两两不同", not _collide, json.dumps(_collide, ensure_ascii=False))
check("（反控）判据真的在比字符串：把两条 id 折成同一个 slug ⇒ 上一条必须能红",
      len({gt._slug("a.b"), gt._slug("a-b"), gt._slug("a b")}) == 1,
      f"{gt._slug('a.b')} / {gt._slug('a-b')} / {gt._slug('a b')}")

print()
print("④ 接线锁：`eval/run_golden.py` 真的在用这个坐标（AST，不读文本）")
_rg = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
_tree = ast.parse(_rg)


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


_rc = _fn(_tree, "run_case")
check("找得到 `run_case`（判据的前提自己摆）", _rc is not None)
_thr_kw = [kw for n in ast.walk(_rc or ast.Module(body=[], type_ignores=[]))
           if isinstance(n, ast.Call) and _call_name(n.func) == "run_one"
           for kw in n.keywords if kw.arg == "thread"]
check("`run_case` 调 `run_one` 时显式传 `thread=`（不靠缺省）", len(_thr_kw) == 1)
check("…而且值来自 `golden_thread.for_case(`（不是又一个常量）",
      bool(_thr_kw) and isinstance(_thr_kw[0].value, ast.Call)
      and _call_name(_thr_kw[0].value.func) == "for_case",
      ast.unparse(_thr_kw[0].value) if _thr_kw else "（没找到）")
_ro = _fn(_tree, "run_one")
# 调用形状是 `ex.submit(ctx.run, _run_agent_stream_to_queue, <messages>, <thread>, <queue>, …)`
# ——`_run_agent_stream_to_queue` 是**实参**不是被调名，所以按位置找而不是按被调名找。
_submits = [n for n in ast.walk(_ro or ast.Module(body=[], type_ignores=[]))
            if isinstance(n, ast.Call) and _call_name(n.func) == "submit"]
_sent = [ast.unparse(a) for n in _submits for a in n.args
         if "_run_agent_stream_to_queue" in ast.unparse(a)]
check("找得到把活儿交给 `_run_agent_stream_to_queue` 的那次 `submit`（前提自己摆）",
      len(_submits) == 1 and len(_sent) == 1, "；".join(_sent) or "（没找到）")
_stream_pos = [ast.unparse(a) for n in _submits for a in n.args]
check("传给图的线程是**形参** `thread`（位置：messages 之后、queue 之前），不是字面",
      len(_stream_pos) >= 5 and _stream_pos[3] == "thread", "；".join(_stream_pos))
check("文件里**不再**出现 `\"golden_thread\"` 这个字面常量（常量退场 = 本件的落点）",
      not [n for n in ast.walk(_tree)
           if isinstance(n, ast.Constant) and n.value == gt.LEGACY])
check("`run_one` 的 `thread` 缺省仍是 `LEGACY`（探针调用方不走用例主循环，行为不许变）",
      "thread: str = golden_thread.LEGACY" in _rg)

print()
print("⑤ 接线锁：落进 trace 的 `thread_id` 是形参，不是写死的字面")
_gtree = ast.parse((ROOT / "eval" / "golden_trace.py").read_text(encoding="utf-8"))
_sc = _fn(_gtree, "start_case")
_start = [n for n in ast.walk(_sc or ast.Module(body=[], type_ignores=[]))
          if isinstance(n, ast.Call) and _call_name(n.func) == "start_trace"]
check("`start_case` 调 `start_trace` 时第三个实参是形参 `thread`",
      bool(_start) and len(_start[0].args) >= 3 and ast.unparse(_start[0].args[2]) == "thread",
      ast.unparse(_start[0].args[2]) if _start and len(_start[0].args) >= 3 else "（没找到）")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
