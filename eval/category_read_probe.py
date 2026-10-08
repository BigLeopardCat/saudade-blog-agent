#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「按分类读后台文章清单」的真链路探针：**验的是 golden 验不到的那一半**。

## 为什么必须单独探（先看这条，否则会以为全量跑绿就等于这件事修好了）

20261009 的改动让 `list_admin_notes` 长出 `category` 参数、并在每行末尾印分类名。
全量 golden（00:16–00:40 那一跑）**结构上测不到它**：178 条用例里没有一条的主人话
是「某个分类下的文章」——`category` 参数为新，就没有旧用例用得上它；而用例不许新增
（分母 178 不动）。跑了、绿了、红数没涨，只说明"没伤到别处"，**不说明这条新路被走到过**
（同族教训见 `eval/task_state_probe.py` 头注：验收用例在跑 ≠ 被测的那条路被走到）。

## 现场（它治的是什么）

生产 trace `logs/agent/traces/20261008/20261008T234509_1_r16c4ec8.json`：主人说
「猫猫帮我把你个测试分类的的文章全部转为私密」，planner 选了 `list_admin_notes`，
但**只填了 keyword="测试"**——那是**搜索**（切词匹配标题/正文/标签名），搜出来的是
标题带"测试"的 8 篇，与"归在「测试」分类下"是两回事。模型于是如实说它手上没有那份
清单，并把「你去后台笔记页按分类筛一遍」这个**本可自动完成**的步骤退给了主人。

而站内这个筛选一直存在（后台笔记页走的就是同一个端点 + `categories` 字段）。缺的两处
都在 agent 这一侧：① 工具参数没暴露；② 行里连分类名都不印。两处 20261009 都已补上
（离线锁在 `tests/test_admin_write.py` §㉕）。

**本探针问的只有一件事**：同一句话再说一遍，planner 现在选不选 `category`。

## 它不验什么（如实划界）

- **不验写侧**：主人那句的后半截是"全部转为私密"。站内**没有**按分类批量改状态的接口，
  agent 也**没有**暴露批量写——这一半今天是真缺（本批明确不做，见工单）。所以本探针
  只看**读**那一半有没有走到；别把"读对了"读成"事情办完了"。
- **不是门禁**：默认退出码 0（同 `llm_judge.py` / `task_state_probe.py` 的取向）；
  `--strict` 才在任一断言不成立时退出 1。
- **单跑不作结论**（本仓定论）：结论一律按**计数**读——跑 N 遍，报"几遍选了 category"。
  N 由 `--runs` 给（默认 3）。

跑法（cd saudade-blog-agent，需网络与 LLM key）：

    GOLDEN_ADMIN_UID=<管理员 uid> .venv/bin/python eval/category_read_probe.py
    GOLDEN_ADMIN_UID=<管理员 uid> .venv/bin/python eval/category_read_probe.py --runs 5 --strict

报告落 `eval/report/category_read_probe_<ts>.md`，同时打印到 stdout。**本脚本不改任何
文档**，结论由人手写进 CHANGELOG / 工单。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "eval"))

import run_golden as G  # noqa: E402
import trace_io  # noqa: E402

# 会话 id：**探针自造的合成值**（不指向任何真实会话，也不落库——探针走内部链路，
# Rust 那一段不在场）。用一个一眼看得出是探针的号码。
PROBE_CONV = 900002

# 主人这一句照现场原样抄（含错别字「你个」「的的」）：探针要验的正是**这句话**会不会
# 被认成"按分类筛"，换个干净的句子就换了题。
UTTERANCE = "猫猫帮我把你个测试分类的的文章全部转为私密"

# 「这是一句占位词，不是站内有过的分类名」的机器判据。**不是**"不在主人原话里就算占位"
# ——合法取值也可能是模型从 `list_categories` 名单里挑的。这里只认**明显是标记而非取值**
# 的形态：指代/待办/占位三类词。
_PLACEHOLDER_RE = re.compile(
    r"未确认|待确认|待定|未知|某分类|某个分类|分类名|占位|placeholder|^X+$|^x+$|^\.\.\.$")

CASE = {
    "id": "probe_category_read",
    "user_input": UTTERANCE,
    "context": {
        "current_url": "/dashboard", "page_title": "后台管理",
        "current_effects": "none", "current_darkmode": "off",
        "role": "admin",
        "conversation_id": PROBE_CONV,
    },
    "gold": {},
}


def _decisions(trace: dict) -> list:
    """这一轮的 planner 决策事件。`run_one` 回的是 trace **路径**（不是内容），
    所以这里自己读一次——读法走 `trace_io.load_trace`（仓内相对路径会让它静默读到
    None，同族坑见 `docs/native-toolcalls-mainline.md`）。"""
    return [e for e in ((trace or {}).get("events") or [])
            if e.get("node") == "planner" and e.get("event") == "decision"]


def _read_one(run_id: str, tag: str) -> dict:
    """跑一轮，把"读侧走没走到分类"所需的三个事实取回来。"""
    case = json.loads(json.dumps(CASE))       # 深拷：run_one 会往里填 uid
    case["context"]["user_id"] = int(os.environ["GOLDEN_ADMIN_UID"])
    case["id"] = f"probe_category_read_{tag}"
    res = G.run_one(G.build_request(case), G.build_principal(case),
                    trace_ctx={"run": run_id, "case": case["id"]})
    trace_path = res.get("trace") or ""
    trace = trace_io.load_trace(trace_path) or {}
    dec = _decisions(trace)
    # 计划里那几行 `工具名({...})`——**参数在这里**（`res["tool_calls"]` 只有名字）。
    tool_lines = [t for d in dec for t in (d.get("tools") or [])]
    cats = [d.get("params", {}).get("category") for d in dec
            if isinstance(d.get("params"), dict)]
    kws = [d.get("params", {}).get("keyword") for d in dec
           if isinstance(d.get("params"), dict)]
    # `params.category` 只覆盖"按技能模板填参"那一路；模型也可能把这次读挂在
    # `content_query` 的 `calls` 里（`{"calls": [{"tool": …, "args": {...}}]}`）——
    # 那时 category 嵌在 calls 里，只读 params 会**漏计**。两条都取（计数是结论本身，
    # 漏一半就等于把 5/5 报成 3/5）。
    nested = [ca.get("args", {}).get("category")
              for d in dec for ca in (d.get("params") or {}).get("calls", [])
              if isinstance(ca, dict) and isinstance(ca.get("args"), dict)]
    real_cats = sorted({str(c).strip() for c in (cats + nested) if c})
    placeholders = [c for c in real_cats if _PLACEHOLDER_RE.search(c)]
    return {
        "text": res.get("text") or "",
        "skills": [d.get("skill") for d in dec],
        "categories": real_cats,
        "good_cats": [c for c in real_cats if c not in placeholders],
        "placeholders": placeholders,
        "keywords": [k for k in kws if k],
        "tool_lines": tool_lines,
        "exec_tools": res.get("exec_tools") or [],
        "error": res.get("error"),
        "trace": trace_path,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="跑几遍（结论按计数读，别单跑定论）")
    ap.add_argument("--strict", action="store_true", help="任一条断言不成立 → 退出码 1")
    args = ap.parse_args()

    uid = os.environ.get("GOLDEN_ADMIN_UID", "").strip()
    if not uid:
        # 仓库是公开的，管理员 uid 不写进文件里（同 golden 的口径）。
        print("[SKIP] 没设 GOLDEN_ADMIN_UID —— 真身份用例跑不了（uid=0 是 agent 侧的"
              "哨兵，一个字节都不发，那样的绿是假绿）")
        return 0

    G.ensure_agent()   # 进程内跑法必须自己建图（不建 = 整轮什么都不发生，且不落 trace）

    run_id = f"catprobe_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"[身份] GOLDEN_ADMIN_UID={uid}（role=admin）")
    print(f"[话] {UTTERANCE}")
    print(f"[跑法] {args.runs} 遍（真 LLM、真链路；单跑不作结论）\n")

    rows: list[dict] = []
    for i in range(1, args.runs + 1):
        t0 = time.monotonic()
        r = _read_one(run_id, f"r{i}")
        r["elapsed"] = round(time.monotonic() - t0, 1)
        rows.append(r)
        print(f"[{i}/{args.runs}] {r['elapsed']}s  skill={r['skills']}  "
              f"category={r['categories']}  keyword={r['keywords']}")
        for line in r["tool_lines"]:
            print(f"          · {line}")
        print(f"          回复：{(r['text'] or '')[:90].replace(chr(10), ' ')}…")

    with_cat = [r for r in rows if r["good_cats"]]
    only_kw = [r for r in rows if not r["categories"] and r["keywords"]]
    neither = [r for r in rows if not r["categories"] and not r["keywords"]]
    ph_rows = [r for r in rows if r["placeholders"]]

    print(f"\n== 计数（{args.runs} 遍）==")
    print(f"  选了 category（真名字）: {len(with_cat)}/{args.runs}")
    print(f"  给 category 填了占位词 : {len(ph_rows)}/{args.runs}"
          + (f"  {[r['placeholders'] for r in ph_rows]}" if ph_rows else ""))
    print(f"  只用了 keyword        : {len(only_kw)}/{args.runs}")
    print(f"  两样都没填（整张清单） : {len(neither)}/{args.runs}")

    checks = [
        ("至少一遍读的是分类（改前这里是 0 —— 参数当时根本不存在）",
         bool(with_cat), f"{len(with_cat)}/{args.runs}"),
        ("没有一遍还在拿 keyword 去凑分类",
         not only_kw, f"only_kw={len(only_kw)}"),
        ("没有一遍给 category 填占位词（填了 = 白烧一轮，主人只看得到「没有这个分类」）",
         not ph_rows, f"{len(ph_rows)}/{args.runs}"),
    ]
    failed = []
    for desc, ok, detail in checks:
        print(("  ✅ " if ok else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
        if not ok:
            failed.append(desc)

    lines = [
        f"# 按分类读后台清单 · 真链路探针 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 主人原话：`{UTTERANCE}`",
        f"- 身份：role=admin（uid 取自环境变量，不落文件） · 跑 {args.runs} 遍",
        f"- 结论按**计数**读（单跑不作结论，本仓定论）",
        "",
        f"| # | 秒 | skill | category | keyword | 计划里的工具行 |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        tools = " / ".join(r["tool_lines"]) or "（无）"
        lines.append(f"| {i} | {r['elapsed']} | {r['skills']} | {r['categories']} | "
                     f"{r['keywords']} | `{tools}` |")
    lines += ["", f"**计数**：选了 category（真名字）**{len(with_cat)}/{args.runs}**；"
                  f"给 category 填了占位词 {len(ph_rows)}/{args.runs}；"
                  f"只用了 keyword {len(only_kw)}/{args.runs}；"
                  f"两样都没填 {len(neither)}/{args.runs}", ""]
    for i, r in enumerate(rows, 1):
        lines += [f"## 第 {i} 遍", "", f"trace: `{r['trace']}`", "",
                  "```", (r["text"] or "").strip(), "```", ""]
    out = os.path.join(ROOT, "eval", "report",
                       f"category_read_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\n报告：{out}")
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
