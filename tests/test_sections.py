"""超长文章处理（rag/sections.py）的回归锁（20260920）。

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
     返回仍是可 `literal_eval` 的 dict（`agent/arg_refs.py` 的 `$tool[N].field` 靠它取值）；
  ⑧ 列表帧的紧凑渲染（20260921）：超预算的列表帧**一行一条**、按**整行**取舍、
     末尾如实标注「共 N 条」——旧路径是 `text[:300]` 裸切，会在一条记录中间断掉，
     planner 既读不出后半条的 id、也看不出后面还有多少条。
  ⑨ 「我自己的数据」帧（20260928）：信封**按结构认**（不再只看四个键名——`unread_items`
     就在那份清单之外，于是 ⑧ 那套渲染在一个真工具上从未生效）；预算与字段上限按
     **工具自报的上限**派生、族按 scope 认（`read.own`）。真事故：未读三条只进了两条，
     narrator 回了「系统返回的数据里被截断了，我这边看不到具体内容」。
"""


import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 pytest 当套件收）
from langchain_core.messages import ToolMessage  # noqa: E402

from agent.arg_refs import parse_data  # noqa: E402
from agent.deterministic_plan import _doc_title  # noqa: E402
from agent.prompt_context import _DETAIL_FRAME_PER, _compact_list_frame, _frame_texts  # noqa: E402
from rag import sections  # noqa: E402
from rag.search import chunk_note  # noqa: E402
from tools.base import _read_section, _shape  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def body_of(sec: str) -> str:
    n = int(re.match(r"(\d+)", sec).group(1))
    return f"§{n}正文标记MARK{n}。" + (f"第{n}节的正文内容，用来把长度撑到超限。" * 150)


_OLD_RE = re.compile(r"['\"]noteTitle['\"]\s*:\s*['\"]([^'\"]{1,80})")


def _old_doc_title(raw: str) -> str:
    m = _OLD_RE.search(raw)
    return m.group(1) if m else ""


from agent.authz import SCOPE_READ_OWN, TOOL_SCOPE  # noqa: E402
from agent.prompt_context import (  # noqa: E402
    _OWN_DATA_FIELD_CAP,
    _OWN_DATA_FRAME_PER,
    _frame_view,  # noqa: E402
)
from tools.base import _UNREAD_CONTENT_MAX, _UNREAD_ITEMS_MAX  # noqa: E402


def test_sections():
    TITLE = "Saudade Blog AI Agent（泠月喵）架构文档"
    SEC_NAMES = ["1. 系统总览", "2. 组件与目录", "3. 一次对话的完整链路", "4. 记忆机制",
                 "5. 工具系统", "6. 防幻觉与可靠性加固", "7. LLM 与配置",
                 "8. 前端看板娘关键机制", "9. 部署与运维", "10. 已知边界与坑（维护必读）"]
    CAP = 20000
    LONG = TITLE + "\n\n开头段落。" * 40 + "\n\n" + "\n\n".join(
        f"## {s}\n{body_of(s)}" for s in SEC_NAMES)
    NEST = (TITLE + "\n\n开头段落。\n\n"
            + "## 5. 工具系统\n\n### 5.1 子平台\n工具正文MARK5\n\n"
            + "## 6. 防幻觉与可靠性加固（踩坑沉淀）\n\n"
            + "### 6.1 状态感知：以 context 为准\n子节正文MARK61\n\n"
            + "### 6.2 执行回执\n子节正文MARK62\n\n"
            + "## 7. LLM 与配置\n配置正文MARK7\n\n"
            + "## 8. 真空节\n\n## 9. 部署与运维\n部署正文MARK9")
    LOSSY = (TITLE + "\n\n## 3. 某节\n某正文MARK3\n\n"
             + "# 场景：同事改了同一个文件\n\n"
             + "# <<<<<<< HEAD\n"                    # 紧跟标题行 ⇒ split 丢掉它
             + "## 5. 解决冲突\n\n### 5.1 处理\n处理正文MARK51")
    _OCAP = 1000
    _OBUDGET = _OCAP - sections._OUTLINE_RESERVE
    _HEAD0 = len(TITLE) + 1                       # 开头段那一节的实际长度（TITLE + 换行）
    _FIRST = "## 1. 第一节\n"
    _FILL = _OBUDGET - _HEAD0 - len(_FIRST) - 4   # 填到"再放一个空节(9 字)就超"
    OUTLINE_MIX = (TITLE + "\n\n" + _FIRST + "甲" * _FILL
                   + "\n\n## 2. 真空节\n\n## 3. 第三节\n" + "乙" * 300
                   + "\n\n### 3.1 子节\n" + "丙" * 300)
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
    print("①b 围栏代码块里的行不当标题（20261017）")
    _FENCE = (TITLE + "\n\n## 3. 主流技术栈专属配置\n\n"
              + "```gitignore\n# ========== 依赖管理 ==========\nnode_modules/\n"
              + "# ========== 本地数据库文件 ==========\n*.db\n```\n\n"
              + "## 4. 下一节\n下一节正文MARK4")
    _FCUT = sections.split(_FENCE, TITLE, shortcut=False)
    check("围栏里的 `# 注释` 不是节边界（切出来只有 3 节，不是十几节）",
          [c["section"] for c in _FCUT] == [TITLE, "3. 主流技术栈专属配置", "4. 下一节"],
          str([c["section"] for c in _FCUT]))
    check("围栏内容留在节正文里（一个字都没丢，只是不再当标题）",
          "========== 本地数据库文件 ==========" in _FCUT[1]["text"]
          and "*.db" in _FCUT[1]["text"])
    check("围栏**认到关栏为止**：关栏之后的标题照常切（§4 是独立的一节）",
          "MARK4" in _FCUT[2]["text"] and "MARK4" not in _FCUT[1]["text"])
    check("正控：把围栏拿掉，同样的行立刻切出节来（不是判据自己跟自己玩）",
          len(sections.split(_FENCE.replace("```gitignore\n", "").replace("*.db\n```", "*.db"),
                             TITLE, shortcut=False)) > len(_FCUT))
    _TILDE = TITLE + "\n\n## 3. 某节\n\n~~~\n# 注释\n~~~\n\n## 4. 下一节\n正文MARK4"
    check("`~~~` 围栏同样认（不只 ``` ）",
          [c["section"] for c in sections.split(_TILDE, TITLE, shortcut=False)]
          == [TITLE, "3. 某节", "4. 下一节"])
    check("围栏外的标题一行都不受影响（正控：`# 标题` 仍切节）",
          len(sections.split(TITLE + "\n\n# 一节\n甲\n# 二节\n乙", TITLE, shortcut=False)) == 3)
    check("开栏没关 ⇒ 其后不再切节（不猜、不自作主张恢复）",
          len(sections.split(TITLE + "\n\n## 3. 某节\n甲\n```\n# 注释\n\n## 4. 名义上的下一节\n乙",
                             TITLE, shortcut=False)) == 2)
    check("`_is_lossy` 与 `split` 同一把尺子（围栏里的行两边都不算标题）",
          not sections._is_lossy(_FENCE, _FCUT)
          and sections._is_lossy(LOSSY, sections.split(LOSSY, TITLE, shortcut=False)))
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
    print("②b 空正文的父标题：连子节一起给（20261009）")
    _h6 = sections.pick(NEST, "6")
    check("父标题自身正文为空 ⇒ 并入子节正文（否则就是一个空串，模型无从下手）",
          "MARK61" in _h6["text"] and "MARK62" in _h6["text"], f"{len(_h6['text'])} 字")
    check("只并到**本节的子节区**为止（下一节 §7 的正文不许进来）",
          "MARK7" not in _h6["text"])
    check("子节标题行也在正文里（模型看得见自己读到的是哪几节）",
          "### 6.1 状态感知：以 context 为准" in _h6["text"])
    check("`subsections` 记下并入了谁（调用方据此如实说「含子节」）",
          _h6["subsections"] == ["6.1 状态感知：以 context 为准", "6.2 执行回执"],
          str(_h6["subsections"]))
    check("编号指称走同一条路（线上那三跑就是 `section=\"6\"`）",
          sections.pick(NEST, "6")["text"] == _h6["text"])
    check("自身有正文的节**一字不变**（只回自身；`subsections` 空 = 没有并入谁）",
          "MARK7" in sections.pick(NEST, "7")["text"]
          and sections.pick(NEST, "7")["subsections"] == []
          and "MARK9" not in sections.pick(NEST, "7")["text"])
    check("真空节（无正文、无子节）如实回空串 + `subsections` 空",
          sections.pick(NEST, "8")["text"] == ""
          and sections.pick(NEST, "8")["subsections"] == [])
    check("`readable_sections` 只列读得出来的（真空节出局，空正文的父标题进得来）",
          sections.readable_sections(NEST, TITLE) ==
          ["5. 工具系统", "5.1 子平台", "6. 防幻觉与可靠性加固（踩坑沉淀）",
           "6.1 状态感知：以 context 为准", "6.2 执行回执", "7. LLM 与配置", "9. 部署与运维"],
          str(sections.readable_sections(NEST, TITLE)))
    _fc = [c for c in sections.split(NEST, TITLE, shortcut=False) if c["level"]]
    _fi = {c["section"]: k for k, c in enumerate(_fc)}
    check("`_is_readable` 的两种能：自身有正文 / 正文在子节里；真空节两种都不是",
          sections._is_readable(_fc, _fi["6. 防幻觉与可靠性加固（踩坑沉淀）"])
          and sections._is_readable(_fc, _fi["7. LLM 与配置"])
          and not sections._is_readable(_fc, _fi["8. 真空节"]))
    check("正控：结构完整的文章**不算有损**（并入照常发生）",
          not sections._is_lossy(NEST, sections.split(NEST, TITLE, shortcut=False))
          and not sections._is_lossy(LONG, sections.split(LONG, TITLE, shortcut=False)))
    print("②c 有损列表：丢过标题的文章不做层级推导（20261009）")
    _lc = sections.split(LOSSY, TITLE, shortcut=False)
    _li = {c["section"]: k for k, c in enumerate(_lc)}
    check("判得有损（正文里 6 个标题行，chunk 只有 5 个）",
          sections._is_lossy(LOSSY, _lc))
    check("红基线：**不戴守卫**时那条走法确实越界——`# 场景：…`（L1）后面跟着被丢掉的 "
      "`# <<<<<<< HEAD`，于是 §5/§5.1 被算成了它的子节",
          sections._child_span(_lc, _li["场景：同事改了同一个文件"], False)[1]
          > _li["场景：同事改了同一个文件"] + 1
          and sections._child_span(_lc, _li["场景：同事改了同一个文件"], True)[1]
          == _li["场景：同事改了同一个文件"] + 1)
    check("有损 ⇒ 一律不并入（宁可只回自身，也不把别人的内容端出来；"
      "过度并入会让模型以为自己读到了这一节）",
          "MARK51" not in sections.pick(LOSSY, "场景：同事改了同一个文件")["text"]
          and sections.pick(LOSSY, "场景：同事改了同一个文件")["subsections"] == [])
    check("有损时 `readable_sections` 只认自身正文（不认子节那条路）",
          "场景：同事改了同一个文件" not in sections.readable_sections(LOSSY, TITLE)
          and "5.1 处理" in sections.readable_sections(LOSSY, TITLE))
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
    _mix = sections.excerpt(OUTLINE_MIX, _OCAP, TITLE)
    check("未展开清单不列真空节（`## 2.` 也没进正文 ⇒ 它确实是被丢掉的那一个）",
          "§2. 真空节" not in _mix and "## 2. 真空节" not in _mix, _mix[-160:])
    check("同一段里读得出来的节照列（只滤掉空的那一个，不是把整段清单吞了）",
          "§3. 第三节" in _mix and "§3.1 子节" in _mix, _mix[-160:])
    check("正控：清单没被滤空时**不退回原名**（§2 的位置不该冒出「另有 N 节未列出」）",
          "另有" not in _mix, _mix[-160:])
    check("滤掉一节不改「怎么取回」那句", "get_article_detail" in _mix and "section=" in _mix)
    check("清单与 `pick` 同一把尺子：列进去的节真读得出正文",
          all((sections.pick(OUTLINE_MIX, s) or {}).get("text", "").strip()
              for s in ("1. 第一节", "3. 第三节", "3.1 子节")))
    check("正控：结构完整的文章清单**一字不变**（LONG 里没有空节）",
          all(f"§{s}" in sections.excerpt(LONG, CAP, TITLE) for s in SEC_NAMES[6:]))
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
    print("④b slim_frame：造帧那一刻去掉同文本的重复正文键（20260925）")
    slim = sections.slim_frame(frame)
    check("帧体积接近减半（省 ≥45%）", len(slim) < len(frame) * 0.55,
          f"{len(frame)} → {len(slim)}")
    check("重复键被去掉、留下的那个还在", "'content'" not in slim and "'noteContent'" in slim)
    check("正文一个字都没少（去的是重复，不是内容）",
          slim.count("第1节的正文内容") == 150 and slim.count("第10节的正文内容") == 150)
    check("仍可 literal_eval（refs 的 $tool[N].field 靠它取值）",
          isinstance(ast.literal_eval(slim), dict))
    check("去重只删字节相等的键：内容不同（真有两段正文）时一个都不动",
          sections.slim_frame(str({"noteContent": "AAA", "content": "BBB"})) ==
          str({"noteContent": "AAA", "content": "BBB"}))
    _empty = str({"noteContent": "", "content": ""})
    check("空正文不去重（两个空串相等，但那不是同一段正文）",
          sections.slim_frame(_empty) == _empty)
    check("判据不认识的帧恒等：`__ERROR__` 帧",
          sections.slim_frame("__ERROR__: 未知工具 x") == "__ERROR__: 未知工具 x")
    check("判据不认识的帧恒等：列表帧", sections.slim_frame("[{'a': 1}]") == "[{'a': 1}]")
    check("判据不认识的帧恒等：命令帧",
          sections.slim_frame("NAVIGATE:/talk") == "NAVIGATE:/talk")
    check("判据不认识的帧恒等：没有该键的 dict",
          sections.slim_frame(str({"noteKey": 1, "noteTitle": "T"})) ==
          str({"noteKey": 1, "noteTitle": "T"}))
    check("判据不认识的帧恒等：不是 repr 的自由文本（含 noteContent 字样也不动）",
          sections.slim_frame("正文里提到 noteContent 这个词") == "正文里提到 noteContent 这个词")
    ref_row = str({"noteKey": 19, "noteContent": LONG, "content": LONG})
    check("原始字符串里 `content` 还在（`_shape` 没被动过 ⇒ $tool[N].content 不受影响）",
          "'content'" in ref_row and "content" in parse_data(ref_row))
    print("⑤ 渲染链路：_frame_texts 真的走节选")
    msg = ToolMessage(content=frame, name="get_article_detail", tool_call_id="t1")
    rendered = _frame_texts([msg])
    check("帧注记说明了「超单帧上限，已按小节节选」",
          "超单帧上限" in rendered and "按小节节选" in rendered)
    check("未展开清单进了提示词", sections.UNEXPANDED_MARK in rendered)
    check("帧体积受控（≤ 生产上限 + 注记开销）",
          len(rendered) <= _DETAIL_FRAME_PER + 400, str(len(rendered)))
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
    check("取不到时也保留 noteTitle（结构那一路）", _doc_title(miss) == TITLE)
    for _t in ["It's a test", 'He said "hi"']:
        _payload = {"noteKey": 19, "noteTitle": _t, "noteContent": "正文"}
        check(f"结构那一路给出**完整**标题：{_t!r}", _doc_title(_payload) == _t,
              _doc_title(_payload))
        check("  ★ 反控：老正则那一路在同一个标题上给的是**另一个值**"
          "（证明上面那条不是白绿）", _old_doc_title(str(_payload)) != _t,
              _old_doc_title(str(_payload)))
    _no_quote = "带「书名号」与（括号）的标题"
    check("没有引号的标题老路本来就对（这一族不是'所有标题都错'）",
          _doc_title({"noteTitle": _no_quote}) == _old_doc_title(
              str({"noteTitle": _no_quote})) == _no_quote)
    _long_t = "标" * 120
    check("超长标题：截到 80 且**带 `…`**（截了要说出口，不许静默砍）",
          _doc_title({"noteTitle": _long_t}) == "标" * 80 + "…",
          _doc_title({"noteTitle": _long_t}))
    check("  ★ 反控：老正则那一路静默砍到 80、看不出被砍过（与'本来就这么长'同形）",
          _old_doc_title(str({"noteTitle": _long_t})) == "标" * 80)
    check("`ToolResult` 直接递进来也认（读它那一格 `.data`）",
          _doc_title(_shape({"noteKey": 19, "noteTitle": "结构本体", "noteContent": "x"}))
          == "结构本体")
    check("★ 回落支：**只有文本**的输入仍走老正则，结果与今天逐字相同（不是病）",
          _doc_title(str({"noteTitle": "架构漫谈"})) == _old_doc_title(
              str({"noteTitle": "架构漫谈"})) == "架构漫谈")
    check("没有 noteTitle 的 dict 与裸文本一样安静地给空串（不猜）",
          _doc_title({"noteKey": 19}) == "" and _doc_title("服务暂时不可用") == "")
    check("取不到时 kind 仍是 ok（有内容可读，不是'空结果'）",
          getattr(miss, "kind", "ok") == "ok")
    amb = _read_section({"noteKey": 19, "noteTitle": TITLE, "noteContent": LONG}, 19, "机制")
    check("指称不唯一 → 走候选分支而不是赌一个",
          "MEM" not in ast.literal_eval(str(amb))["sectionText"] + "X"
          and ast.literal_eval(str(amb))["sectionText"] == "")
    print("⑥b 按节读回：读到了说读到了、没读到说没读到（20261009）")
    _nd = {"noteKey": 19, "noteTitle": TITLE, "noteContent": NEST}
    _d6 = ast.literal_eval(str(_read_section(_nd, 19, "6")))
    check("空正文的父标题：读回来的是 §6 **加上它那几个子节**的正文（不再是空串）",
          "MARK61" in _d6["sectionText"] and "MARK62" in _d6["sectionText"]
          and _d6["readSection"] == "6. 防幻觉与可靠性加固（踩坑沉淀）",
          f"{len(_d6['sectionText'])} 字")
    check("note 如实说「含子节」并列名（不许谎称「只含这一小节」——那会让模型"
      "以为子节不在手里，再点名去读一遍）",
          "及其子节" in _d6["note"] and "§6.1 状态感知：以 context 为准" in _d6["note"],
          _d6["note"][:60])
    check("普通小节（自身有正文、无子节）的 note **一字不变**（既有行为不破）",
          ast.literal_eval(str(_read_section(_nd, 19, "7")))["note"]
          == "本节为节选读取（只含《7. LLM 与配置》这一小节，不含文章其他部分）。")
    _e = ast.literal_eval(str(_read_section(_nd, 19, "8")))
    check("真空节：`sectionText` 空 ⇒ **不许**说「本节为节选读取」"
      "（此前两种形状共用一个 note，模型收到空帧却被告知读到了）",
          _e["sectionText"] == "" and "本节为节选读取" not in _e["note"])
    check("真空节的 note 说清它是什么，并给候选（可行动）",
          "没有正文内容" in _e["note"]
          and _e["availableSections"] == sections.readable_sections(NEST, TITLE),
          _e["note"][:60])
    check("真空节的候选里没有它自己（按指称过滤只会把这个死节名原样还给它）",
          "8. 真空节" not in _e["availableSections"], str(_e["availableSections"]))
    check("真空节仍带 noteTitle（跨轮指代锚点不因空节而丢）",
          _doc_title(str(_read_section(_nd, 19, "8"))) == TITLE)
    check("真空节 kind 仍是 ok（有内容可读——候选清单——不是「空结果」）",
          getattr(_read_section(_nd, 19, "8"), "kind", "ok") == "ok")
    print("⑦ 接线：工具签名与调用面")
    src = "".join((ROOT / "tools" / f).read_text(encoding="utf-8")
                  for f in ("base.py", "notes.py"))
    check("get_article_detail 有 section 参数（str 注解 + 描述都在）",
          "section: Annotated[" in src and '"只读该文章的某一小节' in src)
    check("section 只对 note 生效（talk/board/announcement 走原路）",
          src.index("if not section or not isinstance(data, dict)") < src.index('"talk": ("/talk"'))
    ctx_src = (ROOT / "agent" / "prompt_context.py").read_text(encoding="utf-8")
    check("渲染侧用 frame_excerpt", "sections.frame_excerpt(text, _DETAIL_FRAME_PER)" in ctx_src)
    check("旧的无声硬截断已移除", "原文过长仅示前" not in ctx_src)
    g_src = _ctx_src.graph_deprefixed()
    _p_src = _ctx_src.prose()
    check("planner 规则含按节补读", "超长文章按节补读" in _p_src)
    check("narrator 纪律 14 在位", "14. 工具返回帧标注" in _p_src)
    check("造帧侧（execute）在 ToolMessage 出口去重",
          "sections.slim_frame(str(out))" in g_src)
    check("去重没有动 `_shape`（那是引用取值源，必须保持原样）",
          "slim_frame" not in "".join((ROOT / "tools" / f).read_text(encoding="utf-8")
                                      for f in ("base.py", "notes.py")))
    check("trace 记录帧体量 frames_chars", "frames_chars=len(frames_txt)" in g_src)
    rag_src = (ROOT / "rag" / "search.py").read_text(encoding="utf-8")
    check("索引切分只是转发（不再各写一份）",
          "from rag.sections import split" in rag_src and "def chunk_note" in rag_src)
    print("⑧ 列表帧的紧凑渲染（20260921：一行一条 + 整行取舍 + 共几条）")
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
    _big_row = "{" + ", ".join("'f%d': '%s'" % (i, "z" * 60) for i in range(1, 21)) + "}"
    c1 = _frame_texts([ToolMessage(content="[" + _big_row
        + "]", name="list_notes", tool_call_id="c2")])
    check("单条过长 → 截断 + 「单条过长已截断，共 1 条」", "单条过长已截断" in c1, c1[:80])
    cenv = _frame_texts([ToolMessage(content=str({"code"
        : 0, "data": [{"tagKey": 7, "title": "摄影"}]}),
                                     name="list_tags", tool_call_id="c3")])
    check("信封 dict 里的 data 数组同样紧凑渲染", "tagKey=7" in cenv and "摄影" in cenv, cenv)
    check("信封 dict 里的**同级标量**不再被丢掉（code=0 出一行抬头）",
          "code=0" in cenv and cenv.index("code=0") < cenv.index("tagKey=7"), cenv)
    chead = _frame_texts([ToolMessage(content=str({"unread": 2, "items": [
        {"id": 7, "title": "国庆维护公告", "isRead": False}]}),
        name="get_unread_summary", tool_call_id="c6")])
    check("计数与明细同时在场（抬头 unread=2 + 明细行）",
          "unread=2" in chead and "id=7" in chead and "国庆维护公告" in chead, chead)
    check("抬头不占明细行的编号（第一条仍是 1.）", "\n1. " in chead, chead)
    check("裸数组不产生抬头（明细首行即 1.）",
          c10.split("返回: ", 1)[1].startswith("1. "), c10[:40])
    _ctight = _frame_texts(
        [ToolMessage(content=str({"kind": "k" * 200, "items": [
            {"id": i, "title": "第%d条" % i} for i in range(1, 20)]}),
            name="x", tool_call_id="c7")], per=300)
    _ct_lines = _ctight.split("返回: ", 1)[1].splitlines()
    check("超长抬头不把明细挤成半截（仍是整行取舍 + 节选标注）",
          _ct_lines[0].startswith("kind=") and _ct_lines[-1].startswith("（节选")
          and all(re.match(r"^\d+\. id=\d+ title=第\d+条$", ln) for ln in _ct_lines[1:-1]),
          _ctight[:80])
    cshort = _frame_texts([ToolMessage(content="EFFECT:sakura:on", name="toggle_effect", tool_call_id="c4")])
    check("非列表短文本原样透出（不套列表标注）",
          cshort.endswith("EFFECT:sakura:on") and "节选" not in cshort, cshort)
    clong = _frame_texts([ToolMessage(content="命令帧" * 200, name="x", tool_call_id="c5")])
    check("非列表长文本仍走「节选，原文 N 字」老路径", "节选，原文" in clong, clong[:60])
    print("⑨ 「我自己的数据」帧：结构认信封 + 不比工具愿意给的更小（20260928）")
    _UNREAD_ROWS = [
        {"id": 102, "type": "notice", "title": "留言已通过审核",
         "content": "你的留言「博主是大笨狗」已通过审核，现在可以在留言板看到了。",
         "link": "/guestbook?lid=100", "createdAt": "2026-09-26 17:18:26"},
        {"id": 101, "type": "notice", "title": "留言未通过审核",
         "content": "你的留言「博主是SB」未通过审核，理由：包含辱骂攻击性词汇",
         "link": "/guestbook?lid=99", "createdAt": "2026-09-26 17:15:12"},
        {"id": 100, "type": "notice", "title": "留言已通过审核",
         "content": "你的留言「垃圾博客」已通过审核，现在可以在留言板看到了。",
         "link": "/guestbook?lid=97", "createdAt": "2026-09-26 17:13:39"},
    ]
    _UNREAD = str({"notifications": 3, "messages": 0, "total": 3, "pendingReview": 0,
                   "unread_items": _UNREAD_ROWS})
    cu = _frame_texts([ToolMessage(content=_UNREAD, tool_call_id="c8",
                                   name="get_unread_summary")])
    check("三条未读**全部**进帧（真事故那次第三条整条不见了）",
          all(f"id={i} " in cu for i in (102, 101, 100)), cu[:120])
    check("不再退化成半截 JSON（那次的 frame 是 `…'co` 断在第二条中间）",
          "节选" not in cu and "…" not in cu, cu[-80:])
    check("抬头仍是计数（notifications=3 与明细同在）",
          "notifications=3" in cu and cu.index("notifications=3") < cu.index("id=102"))
    _rec = str({"weird_key": [{"a": 1, "b": "x"}]})
    _c1 = _frame_texts([ToolMessage(content=_rec, name="get_unread_summary", tool_call_id="c9")])
    check("信封键名不在已知清单里也按列表渲染（结构判据，不是键名清单）",
          "a=1" in _c1 and "b=x" in _c1, _c1)
    _multi = str({"x": [{"a": 1}], "unread_items": [{"b": 2}]})
    check("多个候选数组时不猜（宁可回普通文本，也不挑一个当明细）",
          _compact_list_frame(_multi, 300) is None)
    _known = str({"items": [{"a": 1}], "other": [{"b": 2}]})
    check("已知键名优先（既有行为不破：items 赢）",
          "a=1" in _frame_texts([ToolMessage(content=_known, name="list_notifications",
                                             tool_call_id="c11")]))
    _own = sorted(t for t, s in TOOL_SCOPE.items() if s == SCOPE_READ_OWN)
    _bad = [t for t, s in TOOL_SCOPE.items()
            if (s == SCOPE_READ_OWN) != (_frame_view(t, 300)[0] > 300)]
    check(f"大预算严格跟着 scope 走（read.own 共 {len(_own)} 个工具，其余一个都不沾）",
          not _bad, "、".join(_bad))
    check("非 read.own 的预算与字段上限原样不动（300 / 60）",
          _frame_view("list_notes", 300) == (300, 60)
          and _frame_view("x", 300) == (300, 60), str(_frame_view("list_notes", 300)))
    _long = "驳" * 120
    _cl = _frame_texts([ToolMessage(content=str({"unread_items": [
        {"id": 1, "content": _long}]}), name="get_unread_summary", tool_call_id="c12")])
    _cn = _frame_texts([ToolMessage(content=str({"items": [
        {"id": 1, "content": _long}]}), name="x", tool_call_id="c13")])
    check("read.own 帧里 120 字正文不截（工具自己就是截到 120）", _long in _cl, _cl[-40:])
    check("对照：默认族同样长度仍截到 60（不是把全局上限抬了）",
          _long not in _cn and "…" in _cn, _cn[-40:])
    check("字段上限 == 工具的正文上限（_UNREAD_CONTENT_MAX）",
          _OWN_DATA_FIELD_CAP == _UNREAD_CONTENT_MAX,
          f"{_OWN_DATA_FIELD_CAP} vs {_UNREAD_CONTENT_MAX}")
    _full = str({"notifications": _UNREAD_ITEMS_MAX, "messages": 0,
                 "total": _UNREAD_ITEMS_MAX, "pendingReview": 0,
                 "unread_items": [dict(_UNREAD_ROWS[1], id=200 + i, content=_long)
                                  for i in range(_UNREAD_ITEMS_MAX)]})
    _cf = _frame_texts([ToolMessage(content=_full, name="get_unread_summary", tool_call_id="c14")])
    check(f"工具的条数上限（{_UNREAD_ITEMS_MAX} 条 × 正文顶格）也装得下",
          "节选" not in _cf, f"帧长 {len(_cf)} / 预算 {_OWN_DATA_FRAME_PER}")
    _many = str({"unread": 100, "items": [{"id": i, "title": "第%d条" % i,
                                           "content": _long} for i in range(100)]})
    _cm = _frame_texts([ToolMessage(content=_many, name="list_notifications", tool_call_id="c15")])
    check("100 条的帧仍按整行取舍 + 「节选：显示前 K 条，共 100 条」",
          "节选：显示前" in _cm and "共 100 条" in _cm, _cm[-60:])
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_sections()
