#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""单帧预算哨兵：`agent/context.py::_DETAIL_FRAME_PER` 今天还装得下最长的文章吗。

（20260925 批 D 起同时看**第二把尺子**：判官的材料上限
`utils/trace.GOLDEN_MATERIAL_LIMIT`——`execute_node` 落进 trace 的就是这份帧文本，
所以同一份帧长也回答"`eval/llm_judge.py` 手里的材料是不是完整的"。判据与第一把同形。）

## 它解决的是什么

那个常数的旧注释写着「20000 覆盖站内全部文章正文长度」——而实测最长那篇
（note 19）去重后是 26,794 字，**这句话早就不成立了**，且没有任何东西会告诉你它不成立：
超预算的帧会走 `sections.frame_excerpt` 的按节节选（保底是对的，但 planner 一次看不全
正文、要多花一轮补读），这件事在日志与 golden 里都看不出来。同族教训见
`eval/retention_manifest.py` 的头注（"策略写了、没人执行"）；这里是它的镜像形态：
**预算写了、也真的会被越过**，所以得有人报出来。

## 判据

对站内每篇可见文章，按**与 `execute_node` 造帧完全相同的那两步**算帧长：
`tools.base._note_row_with_tag_names(row)` → `agent.sections.slim_frame(str(row))`。
两个函数都是直接 import 的真实现（这里不重写一份"大概一样"的算法——那正是
本仓反复踩的"两侧各写一份、改了一边忘了另一边"）。然后与预算逐篇比：超了就是超了。

- 有超 ⇒ 退出码 1（**非门禁**：夜间只记一行，不把当天的回归标红）。两条出路：接受节选
  （那就把"现在超的是哪几篇"写进 `_DETAIL_FRAME_PER` 的注释），或者把预算改了。
- **不要**改成"按语料自动派生预算"：planner 的提示词规模一旦跟着站点内容浮动，
  发一篇长文就会静默改变决策侧的上下文规模。理由同写在 `_DETAIL_FRAME_PER` 的注释里。

跑法（cd saudade-blog-agent）：
  .venv/bin/python eval/frame_budget.py          # 人看
  .venv/bin/python eval/frame_budget.py --json   # 夜间日志一行（含预算与最长的 id）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import sections                                    # noqa: E402
from agent.context import _DETAIL_FRAME_PER                    # noqa: E402
from tools.base import _get, _note_row_with_tag_names          # noqa: E402
from utils import trace as trace_mod                           # noqa: E402


def summarize(rows: list[tuple[int, int, str]], budget: int,
              material_limit: int | None = None) -> dict:
    """[(帧长, id, 标题)] + 预算 → 报告用摘要。**纯函数**（离线可测，见 tests/test_frame_budget.py）。

    边界：帧长**等于**预算不算超——`_frame_texts` 那一支是 `len(text) <= _DETAIL_FRAME_PER`
    （相等走原样透出），这里必须与它逐字一致，否则哨兵会与真实行为错开一个字。

    `material_limit`（20260925 批 D）= golden 轮给判官的材料上限
    （`run_golden` 设的 `TRACE_TOOL_RESULT_LIMIT`）。**用同一份帧长去量**，因为
    `execute_node` 落进 trace 的就是这份帧文本（一个变量两处用）⇒ "最长帧 vs 它"
    就是"判官的材料有没有缺角"的判据。判据与上面那条同形（**等于**不算超：`len(text)
    <= limit` 时 `tool_result_text` 原样返回），**不是**"差不多就行"。
    """
    ordered = sorted(rows, reverse=True)
    over = [{"id": i, "chars": n, "title": t} for n, i, t in ordered if n > budget]
    longest = ordered[0] if ordered else (0, 0, "")
    rep = {
        "budget": budget,
        "total": len(rows),
        "longest": {"id": longest[1], "chars": longest[0], "title": longest[2]},
        "margin": budget - longest[0],
        "margin_pct": round(100.0 * (budget - longest[0]) / budget, 1) if budget else 0.0,
        "over": over,
        "top": [{"id": i, "chars": n, "title": t} for n, i, t in ordered[:5]],
    }
    if material_limit is not None:
        rep["material"] = {
            "limit": material_limit,
            "longest": longest[0],
            "margin": material_limit - longest[0],
            "cut": [{"id": i, "chars": n, "title": t} for n, i, t in ordered
                    if n > material_limit],
        }
    return rep


def frames_of_site() -> list[tuple[int, int, str]]:
    """站内每篇 → (帧长, id, 标题)。走的是工具那条路（`tools.base._get`）。"""
    listing = _get("/notes?page=1&page_size=200", not_found_text="")
    if not isinstance(listing, list):
        raise SystemExit(f"✗ 文章列表取不到（{listing!r}）——哨兵什么都没有量，"
                         "这不是『全部装得下』")
    out: list[tuple[int, int, str]] = []
    for row in listing:
        nid = row.get("noteKey") or row.get("key")
        if nid is None:
            continue
        detail = _get(f"/notes/{nid}")
        if not isinstance(detail, dict):
            print(f"   ⚠ note {nid} 详情取不到（{detail!r}）——按「没量到」看待，不按 0 计入",
                  file=sys.stderr)
            continue
        frame = sections.slim_frame(str(_note_row_with_tag_names(detail)))
        out.append((len(frame), int(nid), str(detail.get("noteTitle") or "")))
    return out


def render(rep: dict) -> str:
    lines = ["== 单帧预算哨兵（agent/context.py::_DETAIL_FRAME_PER）=="]
    lg = rep["longest"]
    lines.append(f"预算 {rep['budget']} 字；量了 {rep['total']} 篇；"
                 f"最长帧 {lg['chars']}（note {lg['id']}《{lg['title']}》），"
                 f"余量 {rep['margin']} 字（{rep['margin_pct']}%）")
    mat = rep.get("material")
    if mat:
        lines.append(f"判官材料上限 {mat['limit']} 字（golden 轮的 TRACE_TOOL_RESULT_LIMIT，"
                     f"trace 落的就是上面这份帧文本）：余量 {mat['margin']} 字")
        lines.append(("✅ 也装得下：判官手里是完整材料" if not mat["cut"] else
                      f"⚠ {len(mat['cut'])} 篇超上限 ⇒ **判官的材料被截断**，"
                      f"它会把文章里确有、只是没进材料的内容判成编造："
                      + "、".join(f"note {it['id']}（{it['chars']} 字）" for it in mat["cut"])))
    if not rep["over"]:
        lines.append("✅ 全部装得下：没有一篇会走按节节选")
        return "\n".join(lines)
    lines.append(f"⚠ {len(rep['over'])} 篇超预算 ⇒ 这几篇的帧会走**按节节选**"
                 f"（planner 一次看不全正文、要多花一轮补读）：")
    for it in rep["over"]:
        lines.append(f"   note {it['id']}《{it['title']}》{it['chars']} 字"
                     f"（超 {it['chars'] - rep['budget']}）")
    lines.append("   两条出路：接受节选（把这几篇写进 _DETAIL_FRAME_PER 的注释）或改预算；"
                 "**不是**按语料自动派生（理由见那里的注释）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="单帧预算哨兵（默认只报不改，非门禁）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON（夜间日志用）")
    args = ap.parse_args(argv)

    # 第二把尺子（20260925 批 D）：判官的材料上限。同一份帧长两用——`execute_node` 落进
    # trace 的就是这份帧文本（一个变量两处用），所以"最长帧 vs 这个上限"就是
    # "判官的材料有没有缺角"。超了它，判官会把文章里确有、只是没进材料的事实判成编造。
    rep = summarize(frames_of_site(), _DETAIL_FRAME_PER, trace_mod.GOLDEN_MATERIAL_LIMIT)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else render(rep))
    # 两把尺子任一被越过 ⇒ 退出 1（都是"该维护常数了"，但都要有人看见）。非门禁。
    return 1 if (rep["over"] or (rep.get("material") or {}).get("cut")) else 0


if __name__ == "__main__":
    sys.exit(main())
