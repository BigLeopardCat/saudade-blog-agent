# -*- coding: utf-8 -*-
"""输入防线单测（纯函数 + 一次打桩调用，零网络、零 LLM，秒级）。

被测 = 20261005 那一批"无界输入变有界"的改动：

  · `tools/base.py::_cap_rows` / `_clip_fields`（列表类工具的行数/字段封顶）
  · `agent/entities.py::_note_total`（跨轮摘要必须跟着说真话）
  · `agent/graph.py::_cap_frame_text`（帧文本的单帧硬顶 + 单轮总量顶）
  · `agent/context.py::_frame_texts(article_pointer=…)`（正文不再投喂两遍）

判据的核心不是"封顶生效了"，而是**没到上限时一个字节都不变**——三个上限的取值都是
"今天够不着"的量级（20261005 实测：board 25 行 / talk 11 行 / 公告 9 行，最长单帧
26,887 字）⇒ 防线必须是**潜伏**的。所以每条都成对写：触顶长什么样、不触顶长什么样。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import ToolMessage  # noqa: E402

import tools.base as base  # noqa: E402
from agent import entities  # noqa: E402
from agent.context import _frame_texts  # noqa: E402
from agent.graph import (_FRAME_HARD_MAX, _FRAME_MIN_KEEP,  # noqa: E402
                         _TURN_FRAME_TOTAL, _cap_frame_text)

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _board_rows(n: int, *, body: str = "想说的话", author: str = "") -> list:
    """造 n 行留言（字段名照抄线上 /api/public/board，20261005 采样）。

    号段**降序**（上游 `list_by_src` 是 CreatedAt DESC, Id DESC）⇒ 第一行最新，
    "只保留最近 N 条"的判据才有意义。
    """
    return [{"talkKey": 1000 - i, "talkTitle": "诉", "content": body, "cat": "诉",
             "v": 0, "author": author, "mine": False, "approved": 1,
             "createTime": "2026-10-01 09:35:09"} for i in range(n)]


def _parse(out: str) -> list:
    return ast.literal_eval(out)


# ── ① 行数封顶 ───────────────────────────────────────────────────────────────
print("① 列表行数封顶：只回最近 N 条，且说清一共多少条")

rows, note = base._cap_rows(_board_rows(200), base._LIST_ROWS_MAX, "留言")
check(f"200 行 ⇒ 只回 {base._LIST_ROWS_MAX} 行", len(rows) == base._LIST_ROWS_MAX,
      str(len(rows)))
check("保留的是**最近**的那批（上游序即最新在前，不重排）",
      [r["talkKey"] for r in rows] == [1000 - i for i in range(base._LIST_ROWS_MAX)])
check("截断说明写明了总数", "共 200 条" in note, note)
check("截断说明写明了本次带回多少", f"最近{base._LIST_ROWS_MAX}条" in note, note)

rows2, note2 = base._cap_rows(_board_rows(25), base._LIST_ROWS_MAX, "留言")
check("★ 25 行（今天的真实体量）⇒ 原样、**说明是空串**（今天一个字节都不变）",
      len(rows2) == 25 and note2 == "", repr(note2))

# 打桩真跑一遍工具：判据要落在"模型真看到的那串文本"上，不是落在纯函数上
_real_get = base._get
base._get = lambda path, **kw: _board_rows(200)
try:
    out = base.list_guestbook.invoke({})
finally:
    base._get = _real_get
parsed = _parse(out)
dict_rows = [r for r in parsed if isinstance(r, dict)]
notes = [x for x in parsed if not isinstance(x, dict)]
check("工具出口：dict 行只有 60 条", len(dict_rows) == 60, str(len(dict_rows)))
check("工具出口：注记是**一条**非 dict 尾元素（不新增第二条）", len(notes) == 1,
      str(notes))
check("工具出口：注记里同时有『只带回』与总数", "只带回" in notes[0] and "共 200 条" in notes[0],
      notes[0][:80])
check("工具出口：注记仍带着那条既有的审核边界事实（没被顶掉）",
      "已通过审核" in notes[0], notes[0][:80])
check("工具出口：行序未变 ⇒ `$ref` 下标（第 0 行）仍是最新那条",
      dict_rows[0]["talkId"] == 1000, str(dict_rows[0])[:60])

# ── ② 字段封顶 ───────────────────────────────────────────────────────────────
print("② 自由文本字段封顶：只改值、不动结构")

long_row = [{"talkKey": 7, "content": "喵" * 2000, "留名": "名" * 900,
             "cat": "诉", "approved": 1}]
clipped = base._clip_fields(long_row, base._LIST_FIELD_MAX)
check("超长 content 被截到上限并加 …",
      len(clipped[0]["content"]) == base._LIST_FIELD_MAX + 1
      and clipped[0]["content"].endswith("…"), str(len(clipped[0]["content"])))
check("超长的「留名」同样被截", len(clipped[0]["留名"]) == base._LIST_FIELD_MAX + 1,
      str(len(clipped[0]["留名"])))
check("**上游原名 `author` 不在表里**（工具是先 `_board_text_keys` 改名、再封字段）",
      base._clip_fields([{"author": "名" * 900}], base._LIST_FIELD_MAX)[0]["author"]
      == "名" * 900)
check("★ 键集合逐个不变（增删键会让 `$ref` 下标错位）",
      sorted(clipped[0]) == sorted(long_row[0]), str(sorted(clipped[0])))
check("短值原样（今天的体量：最长 54 字 ⇒ 空操作）",
      base._clip_fields([{"content": "小猫咪我好累"}], base._LIST_FIELD_MAX)
      == [{"content": "小猫咪我好累"}])

_real_get = base._get
base._get = lambda path, **kw: [{"id": 25, "title": "国庆快乐呀～",
                                 "content": "公" * 5000}]
try:
    ann_out = base.get_announcements.invoke({})
finally:
    base._get = _real_get
check("★ 公告**只封顶行、不封字段**（正文是管理员写的长文，截了就永远读不全）",
      "公" * 5000 in ann_out, str(len(ann_out)))

# ── ③ 跨轮摘要不许说假话 ─────────────────────────────────────────────────────
print("③ 封顶之后，跨轮实体摘要必须报总数")

base._get = lambda path, **kw: _board_rows(200)
try:
    capped_out = _parse(base.list_guestbook.invoke({}))
finally:
    base._get = _real_get
digest = entities.receipt_digest("list_guestbook", repr(capped_out))
check("摘要里同时出现『最近60条』与『共200条』",
      "最近60条/共200条" in digest, digest[:60])
check("`_note_total` 从注记里读得出总数", entities._note_total(capped_out) == 200,
      str(entities._note_total(capped_out)))
uncapped = _board_rows(25)
check("★ 没被裁过时摘要口径不变（仍是『最近25条』，一个字都没多）",
      entities.receipt_digest("list_guestbook", repr(uncapped)).startswith("最近25条: "),
      entities.receipt_digest("list_guestbook", repr(uncapped))[:30])
check("没有注记时 `_note_total` 安静地给 0（不猜）",
      entities._note_total(uncapped) == 0)

# ── ④⑤ 帧文本封顶 ───────────────────────────────────────────────────────────
print("④⑤ 帧文本封顶：单帧硬顶 + 单轮总量顶，且截断必须写出来")

big = "甲" * (_FRAME_HARD_MAX + 5000)
capped, info = _cap_frame_text(big, 0)
check(f"单帧超 {_FRAME_HARD_MAX} ⇒ 截到硬顶", info and info["kept"] == _FRAME_HARD_MAX
      and info["why"] == "frame", str(info))
check("截断处带〔系统注记〕，写明原始长度与保留长度",
      "已截断" in capped and f"原始 {len(big)} 字" in capped
      and f"保留前 {_FRAME_HARD_MAX} 字" in capped, capped[-60:])

small = "乙" * 1000
same, info2 = _cap_frame_text(small, 0)
check("★ 未触顶 ⇒ 逐字节原样返回、且**不记事件**（info 为 None）",
      same == small and info2 is None)

tight, info3 = _cap_frame_text("丙" * 40000, _TURN_FRAME_TOTAL - 30000)
check("单轮总量触顶 ⇒ 压到剩余额度（why=turn）",
      info3 and info3["why"] == "turn" and info3["kept"] == 30000, str(info3))
check("压过之后**不是空壳**（留下的远多于 _FRAME_MIN_KEEP）",
      info3["kept"] >= _FRAME_MIN_KEEP > 0)

kept, info4 = _cap_frame_text("丁" * 5000, _TURN_FRAME_TOTAL - 100)
check("★ 额度已不足 `_FRAME_MIN_KEEP` ⇒ 宁肯超总量，也把整帧留下（绝不砍成空）",
      kept == "丁" * 5000 and info4 is None)

# ── ⑥ 正文不再投喂两遍（只动"无损那一半"）──────────────────────────────────
print("⑥ 文章正文：narrator 拿指针，planner 等仍拿原文")

body = "正文内容" * 500          # 2000 字，远低于 _DETAIL_FRAME_PER
article = [ToolMessage(content=body, tool_call_id="t1", name="get_article_detail")]
check("默认（planner/reflector/第二条臂）**逐字节不变**",
      _frame_texts(article) == f"工具 get_article_detail 返回: {body}")
pt = _frame_texts(article, article_pointer=True)
check("narrator：换成指针句，正文不再出现第二次",
      body not in pt and "正文全文已随本条工具返回下发" in pt, pt[:70])
check("指针句写明『与本记录同等效力』（否则撞 NARRATOR_DISCIPLINE 第 2 条）",
      "与本记录同等效力" in pt, pt[:90])
check("指针句给了原文长度", "2000 字" in pt, pt[:90])

# 超预算那一支两份内容**不同**（系统这份是按节节选）⇒ 一个字节都不许动
huge = "文" * 60000
big_art = [ToolMessage(content=huge, tool_call_id="t2", name="get_article_detail")]
check("★ 超 _DETAIL_FRAME_PER 那一支：开关_true_false_ 两边完全一致（不碰）",
      _frame_texts(big_art, article_pointer=True) == _frame_texts(big_art))

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
    sys.exit(1)
print("全部通过 ✅")
