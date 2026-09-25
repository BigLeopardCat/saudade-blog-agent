# -*- coding: utf-8 -*-
"""单帧预算哨兵（`eval/frame_budget.py`）的离线回归锁（20260925）。

哨兵本身要联网（量的是线上语料），所以这里锁的是它的**判据**——`summarize()` 这个
纯函数。真正会悄悄出错的只有两件事，两条都钉住：

  ① **边界**：帧长**等于**预算算不算超？真实渲染那一支是
     `len(text) <= _DETAIL_FRAME_PER`（相等走原样透出）⇒ 哨兵必须也判"不算超"。
     差一个字，哨兵报的就不是线上真实行为（"哨兵与代码各判一份"正是本仓踩过的坑）。
  ② **不静默**：超预算的篇目必须逐篇出现在 `over` 里（带上超了多少），
     而且 `longest` 与 `over` 不能互相矛盾（最长那篇超了、`over` 却空 = 报告在骗人）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import frame_budget as fb  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def main() -> int:
    print("① 边界：等于预算不算超（与渲染侧 `<=` 逐字一致）")
    r = fb.summarize([(1000, 7, "刚好"), (999, 8, "差一点")], 1000)
    check("帧长 == 预算 ⇒ 不算超", r["over"] == [] and r["margin"] == 0, str(r["over"]))
    r1 = fb.summarize([(1001, 7, "多一个字")], 1000)
    check("帧长 == 预算 + 1 ⇒ 算超（差一个字就报）",
          len(r1["over"]) == 1 and r1["over"][0]["id"] == 7, str(r1["over"]))
    check("报了超就给差额（人不用自己减）",
          r1["over"][0]["chars"] - r1["budget"] == 1)

    print("② 报告不许自相矛盾")
    r2 = fb.summarize([(5000, 19, "最长"), (4000, 46, "次长")], 3000)
    check("最长那篇也在 over 里", any(x["id"] == 19 for x in r2["over"]))
    check("over 逐篇列出、不折叠成一个数字",
          [x["id"] for x in r2["over"]] == [19, 46], str(r2["over"]))
    check("余量是负数（超了就不该装成正数）", r2["margin"] == -2000, str(r2["margin"]))
    r3 = fb.summarize([(500, 1, "短")], 3000)
    check("没超 ⇒ over 空、余量为正", r3["over"] == [] and r3["margin"] == 2500)

    print("③ 空语料不是「全部装得下」")
    r4 = fb.summarize([], 3000)
    check("零篇时至少不炸、且 total=0（让调用方看得出没量到）",
          r4["total"] == 0 and r4["longest"]["chars"] == 0, str(r4["longest"]))
    check("top 只留前 5（语料变大时报告不膨胀）",
          len(fb.summarize([(i, i, f"n{i}") for i in range(1, 20)], 3)["top"]) == 5)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
