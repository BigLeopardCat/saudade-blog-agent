"""Article splitting and bounded excerpts do not depend on a planner."""

import ast
import re

from agent.arg_refs import parse_data
from rag import sections
from rag.search import chunk_note


def check(label, passed, detail=""):
    assert passed, f"{label}: {detail}"


def body_of(sec: str) -> str:
    n = int(re.match("(\\d+)", sec).group(1))
    return f"§{n}正文标记MARK{n}。" + f"第{n}节的正文内容，用来把长度撑到超限。" * 150


def _check_index_chunks(title, content, section_names):
    print("① 索引切片保留标题路径、来源范围并限制大小")
    indexed = chunk_note(title, content)
    check(
        "每片有范围与小节 id，并保持大小限制",
        all(
            (
                c["section_id"] and c["end"] > c["start"] and (len(c["text"]) <= 1800)
                for c in indexed
            )
        ),
    )
    check("短文正文完整保留", chunk_note(title, "短正文")[0]["text"] == "短正文")
    check("长文的小节均可被索引定位", set(section_names) <= {c["section"] for c in indexed})


def test_sections():
    TITLE = "Saudade Blog AI Agent（泠月喵）架构文档"
    SEC_NAMES = [
        "1. 系统总览",
        "2. 组件与目录",
        "3. 一次对话的完整链路",
        "4. 记忆机制",
        "5. 工具系统",
        "6. 防幻觉与可靠性加固",
        "7. LLM 与配置",
        "8. 前端看板娘关键机制",
        "9. 部署与运维",
        "10. 已知边界与坑（维护必读）",
    ]
    CAP = 20000
    LONG = (
        TITLE
        + "\n\n开头段落。" * 40
        + "\n\n"
        + "\n\n".join((f"## {s}\n{body_of(s)}" for s in SEC_NAMES))
    )
    NEST = (
        TITLE
        + "\n\n开头段落。\n\n"
        + "## 5. 工具系统\n\n### 5.1 子平台\n工具正文MARK5\n\n"
        + "## 6. 防幻觉与可靠性加固（踩坑沉淀）\n\n"
        + "### 6.1 状态感知：以 context 为准\n子节正文MARK61\n\n"
        + "### 6.2 执行回执\n子节正文MARK62\n\n"
        + "## 7. LLM 与配置\n配置正文MARK7\n\n"
        + "## 8. 真空节\n\n## 9. 部署与运维\n部署正文MARK9"
    )
    LOSSY = (
        TITLE
        + "\n\n## 3. 某节\n某正文MARK3\n\n"
        + "# 场景：同事改了同一个文件\n\n"
        + "# <<<<<<< HEAD\n"
        + "## 5. 解决冲突\n\n### 5.1 处理\n处理正文MARK51"
    )
    _OCAP = 1000
    _OBUDGET = _OCAP - sections._OUTLINE_RESERVE
    _HEAD0 = len(TITLE) + 1
    _FIRST = "## 1. 第一节\n"
    _FILL = _OBUDGET - _HEAD0 - len(_FIRST) - 4
    OUTLINE_MIX = (
        TITLE
        + "\n\n"
        + _FIRST
        + "甲" * _FILL
        + "\n\n## 2. 真空节\n\n## 3. 第三节\n"
        + "乙" * 300
        + "\n\n### 3.1 子节\n"
        + "丙" * 300
    )
    _check_index_chunks(TITLE, LONG, SEC_NAMES)
    check(
        "小节名不含 `##`（与索引既有行为一致）",
        all((not c["section"].startswith("#") for c in chunk_note(TITLE, LONG))),
    )
    check(
        "识别 Markdown 1-6 级标题",
        sections.split("#### 四级\n正文", shortcut=False)[0]["level"] == 4,
    )
    check(
        "4 级标题有独立且可读取的小节名",
        sections.split("#### 四级\n正文", shortcut=False)[0]["section"] == "四级",
    )
    check("headings 只列真正的小节（不含开头段）", sections.headings(LONG) == SEC_NAMES)
    check(
        "level 记录标题级别（## → 2）",
        [c["level"] for c in sections.split(LONG, TITLE, shortcut=False)][1] == 2,
    )
    _FENCE = (
        TITLE
        + "\n\n## 3. 主流技术栈专属配置\n\n"
        + "```gitignore\n# ========== 依赖管理 ==========\nnode_modules/\n"
        + "# ========== 本地数据库文件 ==========\n*.db\n```\n\n"
        + "## 4. 下一节\n下一节正文MARK4"
    )
    _FCUT = sections.split(_FENCE, TITLE, shortcut=False)
    check(
        "围栏里的 `# 注释` 不是节边界（切出来只有 3 节，不是十几节）",
        [c["section"] for c in _FCUT] == [TITLE, "3. 主流技术栈专属配置", "4. 下一节"],
        str([c["section"] for c in _FCUT]),
    )
    check(
        "围栏内容留在节正文里（一个字都没丢，只是不再当标题）",
        "========== 本地数据库文件 ==========" in _FCUT[1]["text"] and "*.db" in _FCUT[1]["text"],
    )
    check(
        "围栏**认到关栏为止**：关栏之后的标题照常切（§4 是独立的一节）",
        "MARK4" in _FCUT[2]["text"] and "MARK4" not in _FCUT[1]["text"],
    )
    check(
        "正控：把围栏拿掉，同样的行立刻切出节来（不是判据自己跟自己玩）",
        len(
            sections.split(
                _FENCE.replace("```gitignore\n", "").replace("*.db\n```", "*.db"),
                TITLE,
                shortcut=False,
            )
        )
        > len(_FCUT),
    )
    _TILDE = TITLE + "\n\n## 3. 某节\n\n~~~\n# 注释\n~~~\n\n## 4. 下一节\n正文MARK4"
    check(
        "`~~~` 围栏同样认（不只 ``` ）",
        [c["section"] for c in sections.split(_TILDE, TITLE, shortcut=False)]
        == [TITLE, "3. 某节", "4. 下一节"],
    )
    check(
        "围栏外的标题一行都不受影响（正控：`# 标题` 仍切节）",
        len(sections.split(TITLE + "\n\n# 一节\n甲\n# 二节\n乙", TITLE, shortcut=False)) == 3,
    )
    check(
        "开栏没关 ⇒ 其后不再切节（不猜、不自作主张恢复）",
        len(
            sections.split(
                TITLE + "\n\n## 3. 某节\n甲\n```\n# 注释\n\n## 4. 名义上的下一节\n乙",
                TITLE,
                shortcut=False,
            )
        )
        == 2,
    )
    check(
        "`_is_lossy` 与 `split` 同一把尺子（围栏里的行两边都不算标题）",
        not sections._is_lossy(_FENCE, _FCUT)
        and sections._is_lossy(LOSSY, sections.split(LOSSY, TITLE, shortcut=False)),
    )
    check("标题全称", sections.pick(LONG, "9. 部署与运维", TITLE)["section"] == "9. 部署与运维")
    check("全称容忍空白差异", sections.pick(LONG, "  9.  部署与运维 ", TITLE) is not None)
    check('编号 "9"', sections.pick(LONG, "9", TITLE)["section"] == "9. 部署与运维")
    check('编号 "9."', sections.pick(LONG, "9.", TITLE)["section"] == "9. 部署与运维")
    check("唯一子串「部署」", sections.pick(LONG, "部署", TITLE)["section"] == "9. 部署与运维")
    check(
        "不唯一 → None（不赌一个）：'机制' 命中 4.记忆机制 与 8.前端看板娘关键机制",
        sections.pick(LONG, "机制", TITLE) is None,
    )
    check("不唯一时给候选清单", len(sections.candidates(LONG, "机制", TITLE)) == 2)
    check("完全对不上 → None", sections.pick(LONG, "不存在的节", TITLE) is None)
    check(
        "对不上时候选=全部小节（让模型照抄一个）",
        sections.candidates(LONG, "不存在的节", TITLE) == SEC_NAMES,
    )
    check("空指称 → None", sections.pick(LONG, "", TITLE) is None)
    check(
        "pick 取到的是该节正文（不含标题行）",
        "MARK9" in sections.pick(LONG, "9", TITLE)["text"]
        and "MARK10" not in sections.pick(LONG, "9", TITLE)["text"],
    )
    check("短文（无小节结构）pick 返回 None", sections.pick("短正文", "1", TITLE) is None)
    _h6 = sections.pick(NEST, "6")
    check(
        "父标题自身正文为空 ⇒ 并入子节正文（否则就是一个空串，模型无从下手）",
        "MARK61" in _h6["text"] and "MARK62" in _h6["text"],
        f"{len(_h6['text'])} 字",
    )
    check("只并到**本节的子节区**为止（下一节 §7 的正文不许进来）", "MARK7" not in _h6["text"])
    check(
        "子节标题行也在正文里（模型看得见自己读到的是哪几节）",
        "### 6.1 状态感知：以 context 为准" in _h6["text"],
    )
    check(
        "`subsections` 记下并入了谁（调用方据此如实说「含子节」）",
        _h6["subsections"] == ["6.1 状态感知：以 context 为准", "6.2 执行回执"],
        str(_h6["subsections"]),
    )
    check(
        '编号指称走同一条路（线上那三跑就是 `section="6"`）',
        sections.pick(NEST, "6")["text"] == _h6["text"],
    )
    check(
        "自身有正文的节**一字不变**（只回自身；`subsections` 空 = 没有并入谁）",
        "MARK7" in sections.pick(NEST, "7")["text"]
        and sections.pick(NEST, "7")["subsections"] == []
        and ("MARK9" not in sections.pick(NEST, "7")["text"]),
    )
    check(
        "真空节（无正文、无子节）如实回空串 + `subsections` 空",
        sections.pick(NEST, "8")["text"] == "" and sections.pick(NEST, "8")["subsections"] == [],
    )
    check(
        "`readable_sections` 只列读得出来的（真空节出局，空正文的父标题进得来）",
        sections.readable_sections(NEST, TITLE)
        == [
            "5. 工具系统",
            "5.1 子平台",
            "6. 防幻觉与可靠性加固（踩坑沉淀）",
            "6.1 状态感知：以 context 为准",
            "6.2 执行回执",
            "7. LLM 与配置",
            "9. 部署与运维",
        ],
        str(sections.readable_sections(NEST, TITLE)),
    )
    _fc = [c for c in sections.split(NEST, TITLE, shortcut=False) if c["level"]]
    _fi = {c["section"]: k for k, c in enumerate(_fc)}
    check(
        "`_is_readable` 的两种能：自身有正文 / 正文在子节里；真空节两种都不是",
        sections._is_readable(_fc, _fi["6. 防幻觉与可靠性加固（踩坑沉淀）"])
        and sections._is_readable(_fc, _fi["7. LLM 与配置"])
        and (not sections._is_readable(_fc, _fi["8. 真空节"])),
    )
    check(
        "正控：结构完整的文章**不算有损**（并入照常发生）",
        not sections._is_lossy(NEST, sections.split(NEST, TITLE, shortcut=False))
        and (not sections._is_lossy(LONG, sections.split(LONG, TITLE, shortcut=False))),
    )
    _lc = sections.split(LOSSY, TITLE, shortcut=False)
    _li = {c["section"]: k for k, c in enumerate(_lc)}
    check("判得有损（正文里 6 个标题行，chunk 只有 5 个）", sections._is_lossy(LOSSY, _lc))
    check(
        "未启用有损守卫时，丢失标题会导致子节范围越界",
        sections._child_span(_lc, _li["场景：同事改了同一个文件"], False)[1]
        > _li["场景：同事改了同一个文件"] + 1
        and sections._child_span(_lc, _li["场景：同事改了同一个文件"], True)[1]
        == _li["场景：同事改了同一个文件"] + 1,
    )
    check(
        "有损分节只返回自身，不并入无法核对的子节",
        "MARK51" not in sections.pick(LOSSY, "场景：同事改了同一个文件")["text"]
        and sections.pick(LOSSY, "场景：同事改了同一个文件")["subsections"] == [],
    )
    check(
        "有损时 `readable_sections` 只认自身正文（不认子节那条路）",
        "场景：同事改了同一个文件" not in sections.readable_sections(LOSSY, TITLE)
        and "5.1 处理" in sections.readable_sections(LOSSY, TITLE),
    )
    out = sections.excerpt(LONG, CAP, TITLE)
    check("输出不越上限", len(out) <= CAP, f"{len(out)} > {CAP}")
    check("带未展开清单标记", sections.UNEXPANDED_MARK in out)
    check(
        "§7-§10 被列为未展开（与 note 19 实测同一形状）",
        all((f"§{s}" in out for s in SEC_NAMES[6:])),
        "",
    )
    check("未展开的小节正文真的不在输出里（§9）", "MARK9" not in out)
    check("展开的小节正文在（§1）", "MARK1" in out)
    check(
        "保留的是**前缀**（整节，不切半节）：§6 在、§7 不在", "MARK6" in out and "MARK7" not in out
    )
    check("给出取回方式（section= 调用形式）", "get_article_detail" in out and "section=" in out)
    check("清单里出现的是小节名原文（可照抄）", "§9. 部署与运维" in out)
    check("短文原样返回（不加工）", sections.excerpt("短正文", CAP, TITLE) == "短正文")
    check("刚好等于上限也原样返回", sections.excerpt("字" * CAP, CAP, TITLE) == "字" * CAP)
    single = "字" * 30000
    out1 = sections.excerpt(single, CAP, TITLE)
    check(
        "无小节结构 → 头截断但**有声**（带原文总长）",
        len(out1) <= CAP and "原文共 30000 字" in out1 and ("无小节结构" in out1),
    )
    single_sec = "## 1. 巨型小节\n" + "字" * 30000
    out2 = sections.excerpt(single_sec, CAP, TITLE)
    check(
        "单节自己就超上限 → 退回头截断（仍带标注）",
        len(out2) <= CAP and "原文共" in out2 and ("无小节结构" in out2),
    )
    check("清单最多列 12 节，超出只说数量", "另有" not in sections.outline_text(SEC_NAMES[:10]))
    check("超过 12 节时折叠计数", "另有 8 节未列出" in sections.outline_text(SEC_NAMES * 2))
    _mix = sections.excerpt(OUTLINE_MIX, _OCAP, TITLE)
    check(
        "未展开清单不列真空节（`## 2.` 也没进正文 ⇒ 它确实是被丢掉的那一个）",
        "§2. 真空节" not in _mix and "## 2. 真空节" not in _mix,
        _mix[-160:],
    )
    check(
        "同一段里读得出来的节照列（只滤掉空的那一个，不是把整段清单吞了）",
        "§3. 第三节" in _mix and "§3.1 子节" in _mix,
        _mix[-160:],
    )
    check(
        "正控：清单没被滤空时**不退回原名**（§2 的位置不该冒出「另有 N 节未列出」）",
        "另有" not in _mix,
        _mix[-160:],
    )
    check("滤掉一节不改「怎么取回」那句", "get_article_detail" in _mix and "section=" in _mix)
    check(
        "清单与 `pick` 同一把尺子：列进去的节真读得出正文",
        all(
            (
                (sections.pick(OUTLINE_MIX, s) or {}).get("text", "").strip()
                for s in ("1. 第一节", "3. 第三节", "3.1 子节")
            )
        ),
    )
    check(
        "正控：结构完整的文章清单**一字不变**（LONG 里没有空节）",
        all((f"§{s}" in sections.excerpt(LONG, CAP, TITLE) for s in SEC_NAMES[6:])),
    )
    frame = str(
        {"noteKey": 19, "key": 19, "noteTitle": TITLE, "noteContent": LONG, "content": LONG}
    )
    check(
        "最小复现：repr 里真换行为 0、字面 \\n 很多", "\n" not in frame and frame.count("\\n") > 100
    )
    check(
        "直接按文本切节会切出 0 节（旧路径的死因）",
        len(sections.split(frame, TITLE, shortcut=False)) == 1,
    )
    cut = sections.frame_excerpt(frame, CAP)
    check("frame_excerpt 输出不越上限", len(cut) <= CAP, str(len(cut)))
    check("换行已还原（输出里有真换行、不再整块一行）", cut.count("\n") > 20)
    check("小节结构可用（保留节标题行）", "## 1. 系统总览" in cut)
    check("列出未展开小节", sections.UNEXPANDED_MARK in cut and "§9. 部署与运维" in cut)
    check("未展开的节正文不在输出里", "MARK9" not in cut)
    check("帧头保留 noteTitle（跨轮指代锚点）", TITLE in cut)
    check(
        "正文只留一份（noteContent/content 同文本去重，帧体积不再翻倍）",
        cut.count("MARK1") == 1,
        str(cut.count("MARK1")),
    )
    check("重复键不进帧头（否则又白占 200 字预算）", "'content'" not in cut)
    check(
        "非 dict 帧退回文本路径（转义还原 + 有标注）",
        "原文共" in sections.frame_excerpt("x" * 30000, CAP),
    )
    check(
        "小节名重复时要求消歧，不取第一个匹配",
        sections.pick(LONG + "\n\n## 9. 部署与运维\n重复节", "9", TITLE) is None,
    )
    slim = sections.slim_frame(frame)
    check("帧体积接近减半（省 ≥45%）", len(slim) < len(frame) * 0.55, f"{len(frame)} → {len(slim)}")
    check("重复键被去掉、留下的那个还在", "'content'" not in slim and "'noteContent'" in slim)
    check(
        "正文一个字都没少（去的是重复，不是内容）",
        slim.count("第1节的正文内容") == 150 and slim.count("第10节的正文内容") == 150,
    )
    check(
        "仍可 literal_eval（refs 的 $tool[N].field 靠它取值）",
        isinstance(ast.literal_eval(slim), dict),
    )
    check(
        "去重只删字节相等的键：内容不同（真有两段正文）时一个都不动",
        sections.slim_frame(str({"noteContent": "AAA", "content": "BBB"}))
        == str({"noteContent": "AAA", "content": "BBB"}),
    )
    _empty = str({"noteContent": "", "content": ""})
    check("空正文不去重（两个空串相等，但那不是同一段正文）", sections.slim_frame(_empty) == _empty)
    check(
        "判据不认识的帧恒等：`__ERROR__` 帧",
        sections.slim_frame("__ERROR__: 未知工具 x") == "__ERROR__: 未知工具 x",
    )
    check("判据不认识的帧恒等：列表帧", sections.slim_frame("[{'a': 1}]") == "[{'a': 1}]")
    check("判据不认识的帧恒等：命令帧", sections.slim_frame("NAVIGATE:/talk") == "NAVIGATE:/talk")
    check(
        "判据不认识的帧恒等：没有该键的 dict",
        sections.slim_frame(str({"noteKey": 1, "noteTitle": "T"}))
        == str({"noteKey": 1, "noteTitle": "T"}),
    )
    check(
        "判据不认识的帧恒等：不是 repr 的自由文本（含 noteContent 字样也不动）",
        sections.slim_frame("正文里提到 noteContent 这个词") == "正文里提到 noteContent 这个词",
    )
    ref_row = str({"noteKey": 19, "noteContent": LONG, "content": LONG})
    check(
        "原始字符串里 `content` 还在（`_shape` 没被动过 ⇒ $tool[N].content 不受影响）",
        "'content'" in ref_row and "content" in parse_data(ref_row),
    )
