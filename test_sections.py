# -*- coding: utf-8 -*-
"""超长文章处理（agent/sections.py）的回归锁（20260920）。

被锁住的问题：`get_article_detail` 的全文帧按字符上限**硬截断**，而且**无声**——
实测站内最长文章 note 19 = 25,445 字（帧 repr 52,834 字），上限 20,000 意味着
§7-§10 四个整节从来没有进过任何一轮上下文，模型连"有东西被截掉了、截掉的是哪几节"
都无从知道，更没有任何取回手段（只会说"文档里没写"）。

三处共用同一套节边界（索引切片 / 帧渲染 / 按节取回），本套件按这三条链路分别断言
（纯函数 + 一次渲染集成，不联网、不调 LLM，秒级）：

  ① 切分与索引逐字一致（`chunk_note` 只是转发，短文短路/只认 1-3 级都不变）；
  ② `pick` 三级指称（全称 / 编号 / 唯一子串），不唯一时宁可 `None` 也不赌；
  ③ `excerpt` 整节取舍 + 未展开清单 + 不越上限；无小节结构时退回头截断但**有标注**；
  ④ `frame_excerpt` 对付 repr 层的两个坑（换行是字面 `\\n`、详情 dict 正文存两份）；
  ⑤ 渲染链路 `_frame_texts` 真的走到节选（帧文本带标记、长度受控）；
  ⑥ 跨模块契约不破：`noteTitle` 键在（`decisions._doc_title` 靠它做跨轮指代锚点）、
     返回仍是可 `literal_eval` 的 dict（`agent/refs.py` 的 `$tool[N].field` 靠它取值）；
  ⑧ 列表帧的紧凑渲染（20260921）：超预算的列表帧**一行一条**、按**整行**取舍、
     末尾如实标注「共 N 条」——旧路径是 `text[:300]` 裸切，会在一条记录中间断掉，
     planner 既读不出后半条的 id、也看不出后面还有多少条。
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import sections                       # noqa: E402
from agent.context import _frame_texts           # noqa: E402
from agent.decisions import _doc_title           # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from rag.search import chunk_note                # noqa: E402
from tools.base import _read_section             # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 语料：按 note 19 的真实形状造（10 节 × ~3000 字，总长超 20000 上限）──
TITLE = "Saudade Blog AI Agent（泠月喵）架构文档"
SEC_NAMES = ["1. 系统总览", "2. 组件与目录", "3. 一次对话的完整链路", "4. 记忆机制",
             "5. 工具系统", "6. 防幻觉与可靠性加固", "7. LLM 与配置",
             "8. 前端看板娘关键机制", "9. 部署与运维", "10. 已知边界与坑（维护必读）"]
CAP = 20000


def body_of(sec: str) -> str:
    n = int(re.match(r"(\d+)", sec).group(1))
    return f"§{n}正文标记MARK{n}。" + (f"第{n}节的正文内容，用来把长度撑到超限。" * 150)


LONG = TITLE + "\n\n开头段落。" * 40 + "\n\n" + "\n\n".join(
    f"## {s}\n{body_of(s)}" for s in SEC_NAMES)

print("① 切分：与索引同源（chunk_note 只是转发）")
check("chunk_note 输出 == sections.split 的 section/text 投影",
      chunk_note(TITLE, LONG) == [{"section": c["section"], "text": c["text"]}
                                  for c in sections.split(LONG, TITLE)])
check("短文（<2000）不切，整篇一节且节名=文章标题",
      chunk_note(TITLE, "短正文") == [{"section": TITLE, "text": "短正文"}])
check("长文按节切出 11 节（开头段 + 10 节）",
      len(chunk_note(TITLE, LONG)) == 11, str(len(chunk_note(TITLE, LONG))))
check("小节名不含 `##`（与索引既有行为一致）",
      all(not c["section"].startswith("#") for c in chunk_note(TITLE, LONG)))
check("只认 1-3 级：`####` 不是节边界",
      len(sections.split("#### 四级\n正文", shortcut=False)) == 1)
check("4 级标题落在正文里（不算节）",
      "#### 四级" in sections.split("#### 四级\n正文", shortcut=False)[0]["text"])
check("headings 只列真正的小节（不含开头段）",
      sections.headings(LONG) == SEC_NAMES)
check("level 记录标题级别（## → 2）",
      [c["level"] for c in sections.split(LONG, TITLE, shortcut=False)][1] == 2)

print("② pick：三级指称")
check("标题全称", sections.pick(LONG, "9. 部署与运维", TITLE)["section"] == "9. 部署与运维")
check("全称容忍空白差异", sections.pick(LONG, "  9.  部署与运维 ", TITLE) is not None)
check("编号 \"9\"", sections.pick(LONG, "9", TITLE)["section"] == "9. 部署与运维")
check("编号 \"9.\"", sections.pick(LONG, "9.", TITLE)["section"] == "9. 部署与运维")
check("唯一子串「部署」", sections.pick(LONG, "部署", TITLE)["section"] == "9. 部署与运维")
check("不唯一 → None（不赌一个）：'机制' 命中 4.记忆机制 与 8.前端看板娘关键机制",
      sections.pick(LONG, "机制", TITLE) is None)
check("不唯一时给候选清单", len(sections.candidates(LONG, "机制", TITLE)) == 2)
check("完全对不上 → None", sections.pick(LONG, "不存在的节", TITLE) is None)
check("对不上时候选=全部小节（让模型照抄一个）",
      sections.candidates(LONG, "不存在的节", TITLE) == SEC_NAMES)
check("空指称 → None", sections.pick(LONG, "", TITLE) is None)
check("pick 取到的是该节正文（不含标题行）",
      "MARK9" in sections.pick(LONG, "9", TITLE)["text"]
      and "MARK10" not in sections.pick(LONG, "9", TITLE)["text"])
check("短文（无小节结构）pick 返回 None",
      sections.pick("短正文", "1", TITLE) is None)

print("③ excerpt：整节取舍 + 未展开清单")
out = sections.excerpt(LONG, CAP, TITLE)
check("输出不越上限", len(out) <= CAP, f"{len(out)} > {CAP}")
check("带未展开清单标记", sections.UNEXPANDED_MARK in out)
check("§7-§10 被列为未展开（与 note 19 实测同一形状）",
      all(f"§{s}" in out for s in SEC_NAMES[6:]), "")
check("未展开的小节正文真的不在输出里（§9）", "MARK9" not in out)
check("展开的小节正文在（§1）", "MARK1" in out)
check("保留的是**前缀**（整节，不切半节）：§6 在、§7 不在",
      "MARK6" in out and "MARK7" not in out)
check("给出取回方式（section= 调用形式）", "get_article_detail" in out and "section=" in out)
check("清单里出现的是小节名原文（可照抄）", "§9. 部署与运维" in out)
check("短文原样返回（不加工）", sections.excerpt("短正文", CAP, TITLE) == "短正文")
check("刚好等于上限也原样返回", sections.excerpt("字" * CAP, CAP, TITLE) == "字" * CAP)
single = "字" * 30000
out1 = sections.excerpt(single, CAP, TITLE)
check("无小节结构 → 头截断但**有声**（带原文总长）",
      len(out1) <= CAP and "原文共 30000 字" in out1 and "无小节结构" in out1)
single_sec = "## 1. 巨型小节\n" + "字" * 30000      # 全文只有一节
out2 = sections.excerpt(single_sec, CAP, TITLE)
check("单节自己就超上限 → 退回头截断（仍带标注）",
      len(out2) <= CAP and "原文共" in out2 and "无小节结构" in out2)
check("清单最多列 12 节，超出只说数量", "另有" not in sections.outline_text(SEC_NAMES[:10]))
check("超过 12 节时折叠计数", "另有 8 节未列出" in sections.outline_text(SEC_NAMES * 2))

print("④ frame_excerpt：repr 层的两个坑")
frame = str({"noteKey": 19, "key": 19, "noteTitle": TITLE,
             "noteContent": LONG, "content": LONG})
check("最小复现：repr 里真换行为 0、字面 \\n 很多",
      "\n" not in frame and frame.count("\\n") > 100)
check("直接按文本切节会切出 0 节（旧路径的死因）",
      len(sections.split(frame, TITLE, shortcut=False)) == 1)
cut = sections.frame_excerpt(frame, CAP)
check("frame_excerpt 输出不越上限", len(cut) <= CAP, str(len(cut)))
check("换行已还原（输出里有真换行、不再整块一行）", cut.count("\n") > 20)
check("小节结构可用（保留节标题行）", "## 1. 系统总览" in cut)
check("列出未展开小节", sections.UNEXPANDED_MARK in cut and "§9. 部署与运维" in cut)
check("未展开的节正文不在输出里", "MARK9" not in cut)
check("帧头保留 noteTitle（跨轮指代锚点）", TITLE in cut)
check("正文只留一份（noteContent/content 同文本去重，帧体积不再翻倍）",
      cut.count("MARK1") == 1, str(cut.count("MARK1")))
check("重复键不进帧头（否则又白占 200 字预算）", "'content'" not in cut)
check("非 dict 帧退回文本路径（转义还原 + 有标注）",
      "原文共" in sections.frame_excerpt("x" * 30000, CAP))
check("小节名重复不误判（pick 取第一个匹配）",
      sections.pick(LONG + "\n\n## 9. 部署与运维\n重复节", "9", TITLE) is not None)

print("⑤ 渲染链路：_frame_texts 真的走节选")
msg = ToolMessage(content=frame, name="get_article_detail", tool_call_id="t1")
rendered = _frame_texts([msg])
check("帧注记说明了「超单帧上限，已按小节节选」",
      "超单帧上限" in rendered and "按小节节选" in rendered)
check("未展开清单进了提示词", sections.UNEXPANDED_MARK in rendered)
check("帧体积受控（≤ 上限 + 注记开销）", len(rendered) <= CAP + 400, str(len(rendered)))
small = ToolMessage(content=str({"noteKey": 5, "noteTitle": "短文章", "noteContent": "正文"}),
                    name="get_article_detail", tool_call_id="t2")
check("未超限的帧原样透出（既有行为不变）",
      "短文章" in _frame_texts([small]) and "节选" not in _frame_texts([small]))
sec_frame = str({"noteKey": 19, "noteTitle": TITLE, "readSection": "9. 部署与运维",
                 "sectionText": body_of("9. 部署与运维"), "note": "本节为节选读取"})
check("按节读回的帧不被二次节选（本来就没超限）",
      "MARK9" in _frame_texts([ToolMessage(content=sec_frame, name="get_article_detail",
                                           tool_call_id="t3")]))

print("⑥ 跨模块契约：noteTitle 在、返回仍是可解析 dict")
sec = _read_section({"noteKey": 19, "noteTitle": TITLE, "noteContent": LONG}, 19, "9")
check("按节读到的是该节正文", "MARK9" in sec and "MARK10" not in sec)
check("_doc_title 能从按节返回里取到标题（执行记忆的指代锚点）",
      _doc_title(str(sec)) == TITLE)
check("返回可 literal_eval（refs 的 $tool[N].field 靠它取值）",
      isinstance(ast.literal_eval(str(sec)), dict))
check("返回带 noteKey（清单让模型照抄 id 再读一次）",
      ast.literal_eval(str(sec))["noteKey"] == 19)
check("按节返回仍是 ok（不是空）——读了就是读了", getattr(sec, "kind", "ok") == "ok")
miss = _read_section({"noteKey": 19, "noteTitle": TITLE, "noteContent": LONG}, 19, "不存在的节")
d = ast.literal_eval(str(miss))
check("取不到小节 → 不返回空，列出候选（可行动）",
      d["availableSections"] == SEC_NAMES and d["sectionText"] == "")
check("取不到时也保留 noteTitle（_doc_title 不炸）", _doc_title(str(miss)) == TITLE)
check("取不到时 kind 仍是 ok（有内容可读，不是'空结果'）",
      getattr(miss, "kind", "ok") == "ok")
amb = _read_section({"noteKey": 19, "noteTitle": TITLE, "noteContent": LONG}, 19, "机制")
check("指称不唯一 → 走候选分支而不是赌一个",
      "MEM" not in ast.literal_eval(str(amb))["sectionText"] + "X"
      and ast.literal_eval(str(amb))["sectionText"] == "")

print("⑦ 接线：工具签名与调用面")
src = (Path(__file__).resolve().parent / "tools" / "base.py").read_text(encoding="utf-8")
check("get_article_detail 有 section 参数", "section: Annotated[str," in src)
check("section 只对 note 生效（talk/board/announcement 走原路）",
      src.index("if not section or not isinstance(data, dict)") < src.index('"talk": ("/talk"'))
ctx_src = (Path(__file__).resolve().parent / "agent" / "context.py").read_text(encoding="utf-8")
check("渲染侧用 frame_excerpt", "sections.frame_excerpt(text, _DETAIL_FRAME_PER)" in ctx_src)
check("旧的无声硬截断已移除", "原文过长仅示前" not in ctx_src)
g_src = (Path(__file__).resolve().parent / "agent" / "graph.py").read_text(encoding="utf-8")
check("planner 规则含按节补读", "超长文章按节补读" in g_src)
check("narrator 纪律 14 在位", "14. 工具返回帧标注" in g_src)
check("trace 记录帧体量 frames_chars", "frames_chars=len(frames_txt)" in g_src)
rag_src = (Path(__file__).resolve().parent / "rag" / "search.py").read_text(encoding="utf-8")
check("索引切分只是转发（不再各写一份）",
      "from agent.sections import split" in rag_src and "def chunk_note" in rag_src)

print("⑧ 列表帧的紧凑渲染（20260921：一行一条 + 整行取舍 + 共几条）")
# 被锁住的问题：普通帧此前是 `text[:300]` **裸切**——列表帧（dict repr）在**一行中间**
# 断掉，planner 既读不出后半条的 id，也看不出后面还有多少条（与"空结果 vs 没执行"同源）。
_rows10 = "[" + ", ".join(
    "{'noteKey': %d, 'noteTitle': '第%d篇', 'noteContent': '%s', 'status': 'public',"
    " 'isTop': 0, 'cover': 'http://cdn/x.png', 'coverZoom': 1.2}" % (i, i, "正" * 80)
    for i in range(1, 11)) + "]"
c10 = _frame_texts([ToolMessage(content=_rows10, name="list_notes", tool_call_id="c1")])
check("超预算的列表帧标注「节选：显示前 K 条，共 10 条」",
      "节选：显示前" in c10 and "共 10 条" in c10, c10[-60:])
check("显示出来的每一行都是**整条**（没有半截行）",
      all(re.match(r"^\d+\. noteKey=", ln) for ln in c10.splitlines()[1:-1] if ln.strip()), c10[:120])
check("首条的 id 与标题都在（planner 要照着抄 id）",
      "noteKey=1 " in c10 and "第1篇" in c10, c10[:120])
check("纯展示字段不进帧（封面/缩放）", "cover" not in c10 and "Zoom" not in c10)
check("正文长值截断带 …（不静默吃掉）", "…" in c10)
# 单条就超预算（字段多到一行放不下）：不静默、也不整条消失
_big_row = "{" + ", ".join("'f%d': '%s'" % (i, "z" * 60) for i in range(1, 21)) + "}"
c1 = _frame_texts([ToolMessage(content="[" + _big_row + "]", name="list_notes", tool_call_id="c2")])
check("单条过长 → 截断 + 「单条过长已截断，共 1 条」", "单条过长已截断" in c1, c1[:80])
# 信封形态（{"data": [...]}）与裸数组都要认
cenv = _frame_texts([ToolMessage(content=str({"code": 0, "data": [{"tagKey": 7, "title": "摄影"}]}),
                                 name="list_tags", tool_call_id="c3")])
check("信封 dict 里的 data 数组同样紧凑渲染", "tagKey=7" in cenv and "摄影" in cenv, cenv)
# 认不出的（非列表）走普通文本路径：短文原样、长文带既有（节选，原文 N 字）标注
cshort = _frame_texts([ToolMessage(content="EFFECT:sakura:on", name="toggle_effect", tool_call_id="c4")])
check("非列表短文本原样透出（不套列表标注）",
      cshort.endswith("EFFECT:sakura:on") and "节选" not in cshort, cshort)
clong = _frame_texts([ToolMessage(content="命令帧" * 200, name="x", tool_call_id="c5")])
check("非列表长文本仍走「节选，原文 N 字」老路径", "节选，原文" in clong, clong[:60])

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
