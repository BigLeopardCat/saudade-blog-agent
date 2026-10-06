# -*- coding: utf-8 -*-
"""写目标名的**来源态**（20260924）：主人说出口的那几个字才是名字。

秒级、纯函数、无网络无 LLM；由 eval.yml 在 push 时跑。

要治的病（`admin_tag_delete_popup` 现场）：主人说「标签「大笨狗」我不想要了，删掉吧」，
planner 填 `name="删掉吧"`——它**恰是这句话的子串**，于是"值在原话里有据"那道判据放行，
校正不发生，弹卡问成「删除标签「删掉吧」」，而卡片文案是错值进库**唯一的防线**。
同族的还有泛称（`name="标签"`）与"站内正好有这个字"（`name="河灯留言"`）。

判据换成了结构性的：目标名的字面必须能由**三个具名抽取器**从主人这句话里取出来
（引号段 / 免引号目标名 / 目标槽位，见 `_msg_grounded_name`）——**"恰是整句的子串"不再是
出处**。这样"标签就叫「删掉吧」"自然正确（主人加了引号 ⇒ 引号段就是那个字面），
不需要任何"像不像动作短语"的词表分类。

覆盖七块：
  ① 三个抽取器各自取得到什么（正例）；
  ② 真 planner 采样值的校正（20260924 三条现场 + move 族的父子错位）；
  ③ 门本身（`_target_grounding_refusal`）：该拒的拒、话术不越界、五条早退；
  ④ 12 条弹卡用例的**正解不受误伤**（意图正确的 spec 原样通过——这条锁的是反向风险：
     判据收紧了，别把主人说过的话也拒掉）；
  ⑤ 判据里不再有词表（AST 级：`_owner_target_span` 规则④ 与门函数都不引用
     `_GENERIC_NAME_WORDS` / `_TARGET_ACTION_MARKS`——防止有人把补丁式的词表加回来）。
  ⑥ **值**通道的指代（20260927）：命名标记后面跟的是代词时不算名字，来源态判据也不
     认它——代词是原话的子串，那条逐字子串的地基对它本来是失效的（现场重放见下）。
  ⑦ 待办族"引用式唯一命中"的**来源**（20260929 批 G）：候选只来自主人**没加引号**的
     原话（引号里那一段归引号通道），且**没有第二个来源**（签名里没有 planner 的值）；
     行为面（唯一命中才校正、0/多即零写）在 `test_todo_schedule.py` ⑰b 用真门跑。

**令牌层的不变量**（弹卡印出来的那个名字可溯源）在**产物层**锁：需要两轮跑法把
`__CONFIRM__:` 帧里的令牌载荷带回来（`run_case` 的 `confirm_payloads`），随确认轮
评测支持一并落地；本文件是它的离线半边。

用法：.venv/bin/python tests/test_target_grounding.py
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from agent.graph import (  # noqa: E402
    _DEICTIC_WORDS, _bare_target_name, _grounded_value, _msg_grounded_name,
    _msg_name_slot, _msg_named_value, _msg_quote_spans, _name_arg_fix, _name_like,
    _name_target_fix, _target_grounding_refusal, _value_clean)

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _plan(tool: str, args: dict, skill: str = "chat") -> dict:
    """一条 spec 的计划对象（门只读 tools 里的参数，够用）。"""
    return {"skill": skill, "params": dict(args),
            "tools": [f"{tool}({json.dumps(args, ensure_ascii=False)})"],
            "note": "x", "reply": "y"}


def _args_of(plan_obj: dict) -> dict:
    spec = (plan_obj.get("tools") or [""])[0]
    return json.loads(spec[spec.index("(") + 1:spec.rindex(")")])


def _names_in(func) -> set[str]:
    """函数**代码**里出现的名字（不含 docstring 里的散文提及）。"""
    tree = ast.parse(inspect.getsource(func))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


print("① 三个具名抽取器（引号段 / 免引号目标名 / 目标槽位）")
_M = "帮我把标签 Asyncio 挪到「编程」下面"
check("引号段：加引号的那一段原样取出（可多段）",
      _msg_quote_spans(_M) == ["编程"]
      and _msg_quote_spans("我想在「编程」下面加一个二级标签，名字叫「向量数据库」")
      == ["编程", "向量数据库"])
check("免引号目标名：名词锚与动作锚之间那一段", _bare_target_name(_M) == "Asyncio")
check("目标槽位：同一段的**原始**字面（脏了也给，供判出处用）",
      _msg_name_slot("帮我把标签「编程」下面那个 Asyncio 删掉") == "「编程」下面那个 Asyncio"
      and _msg_name_slot(_M) == "Asyncio")
check("出处判定：三种位置任一命中即算取出",
      _msg_grounded_name("编程", _M) and _msg_grounded_name("Asyncio", _M))
check("「恰是整句的子串」**不算**出处（本轮要拿掉的那条假通道）",
      not _msg_grounded_name("删掉吧", "标签「大笨狗」我不想要了，删掉吧")
      and not _msg_grounded_name("挪到", _M)
      and not _msg_grounded_name("看着有点乱", "帮我把那条写着「泠月喵真棒！」的留言驳回吧，看着有点乱"))
check("名词前的同指语序也算出处（`大笨狗那个标签`，否则如实追问会自相矛盾）",
      _msg_grounded_name("大笨狗", "大笨狗那个标签我不想要了，删掉吧")
      and not _msg_grounded_name("删掉吧", "大笨狗那个标签我不想要了，删掉吧"))
# 20261006 改判：P 语序（名字在名词**前**）原来在 `_name_like` 这一门就被挡掉，于是
# 「泠月喵，把jingbao这个用户降级为杂鱼」+ planner 填了另一个名字时，整条校正通道
# **根本没进过门**（生产 trace `20261006T023655` 的 message 逐字）。上一行（本表第 97
# 条）早就在 `_msg_grounded_name` 里认这个语序了——两处口径不一致才是那次事故的一半。
# 现在 `_name_like` 收 P，判据是**剥掉处置词与同指限定词后还剩东西**
# （"把那个"剥剩"那个"∈指代表 ⇒ 仍是纯指代）。账号族的名词（用户/账号）要传该族的
# `lex`（默认那份只有标签/分类）——门里就是这么调的（`_target_grounding_refusal`）。
_ACC = g._lexicon("freeze_account")
check("纯指代（剥完只剩指代词）⇒ 本门不介入（`_name_like` 假）",
      not _name_like("把那个标签删掉吧")
      and not _name_like("把那个用户降级为杂鱼", _ACC)
      and not _name_like("把 那个 用户 降级", _ACC)
      and not _name_like("把那些标签都删了吧"))
check("P 语序（名字在名词前）⇒ 本门**介入**（与第 97 条的 `_msg_grounded_name` 同口径）",
      _name_like("大笨狗那个标签删掉吧")
      and _name_like("泠月喵，把jingbao这个用户降级为杂鱼", _ACC)
      # 夹空白的写法也要认（空白不是句读；拉丁账号名两侧常带空格）
      and _name_like("把 jingbao 这个用户降级为杂鱼", _ACC)
      and _name_like(_M))
check("  出处判定同步：`jingbao` 有据、planner 那个 `niuniu` 无据（现场那对取值）",
      _msg_grounded_name("jingbao", "泠月喵，把jingbao这个用户降级为杂鱼", lex=_ACC)
      and _msg_grounded_name("jingbao", "把 jingbao 这个用户降级为杂鱼", lex=_ACC)
      and not _msg_grounded_name("niuniu", "泠月喵，把jingbao这个用户降级为杂鱼", lex=_ACC))

print("\n② 真 planner 采样值的校正（20260924 现场）")
_SENT = "标签「大笨狗」我不想要了，删掉吧"
for _bad in ("删掉吧", "河灯留言", "标签"):
    po = {"skill": "tag_delete", "params": {"name": _bad, "level": "one"},
          "tools": [f'delete_tag({json.dumps({"name": _bad, "level": "one"}, ensure_ascii=False)})'],
          "note": "x", "reply": "y"}
    _name_target_fix(po, _SENT)
    check(f"  目标名校正：{_bad!r} → 主人引号里那一段（弹卡印的是它）",
          _args_of(po).get("name") == "大笨狗", str(po["tools"]))
po = {"skill": "tag_delete", "params": {"name": "大笨狗", "level": "one"},
      "tools": ['delete_tag({"name": "大笨狗", "level": "one"})'], "note": "x", "reply": "y"}
_name_target_fix(po, _SENT)
check("  正解不被改写（防线不是重写器）", _args_of(po).get("name") == "大笨狗")
po = {"skill": "tag_update",
      "params": {"name": "编程", "parent_tag": "父标签名", "level": "one"},
      "tools": ['update_tag({"name": "编程", "parent_tag": "父标签名", "level": "one"})'],
      "note": "x", "reply": "y"}
_name_target_fix(po, _M)
check("  父子错位两处同修：目标取回 Asyncio、父标签取回「编程」",
      _args_of(po).get("name") == "Asyncio" and _args_of(po).get("parent_tag") == "编程",
      str(po["tools"]))

print("\n③ 门（_target_grounding_refusal）")
_CTX = "我想在「编程」下面加一个二级标签，名字叫「向量数据库」"
po = _plan("create_tag", {"title": "向量数据库", "parent_tag": "父标签名"})
r = _target_grounding_refusal(po, _CTX)
check("父标签取不出处 → 拒绝（不回弹卡）", r is not None and r[0] == "create_tag")
if r:
    why = r[1]
    check("  话术只谈主人这句话：点名主人说过的那几段",
          "向量数据库" in why and "编程" in why, why)
    check("  话术不越界（本层没读过台账，不许说站内/字典/查不到）",
          not any(w in why for w in ("站内", "字典", "查不到", "没有叫")), why)
    check("  话术不替主人下结论（不说\"主人没说过这个名字\"——他可能是换了个语序）",
          "不是主人说出口" not in why and "没说过" not in why, why)
    check("  话术声明零改动", "没有改动" in why)
check("父标签就是主人点过名的那个 → 不拒", _target_grounding_refusal(
    _plan("create_tag", {"title": "向量数据库", "parent_tag": "编程"}), _CTX) is None)
check("引号里的目标（公告/留言族）→ 不拒",
      _target_grounding_refusal(_plan("delete_announcement", {"title": "公告"}),
                                "把标题是「公告」的那条公告删掉吧") is None
      and _target_grounding_refusal(_plan("delete_board_comment", {"quote": "泠月喵好笨啊"}),
                                    "把那条写着「泠月喵好笨啊」的留言删掉吧") is None)
check("早退① 文章族不在名字表（走 target_* 三条判据）",
      _target_grounding_refusal(
          _plan("set_article_status", {"article_id": 999999, "status": "private"}),
          "文章 999999 那篇我想设成私密，先别急着动") is None)
check("早退② 多 spec 混排不判",
      _target_grounding_refusal(
          {"skill": "chat", "params": {}, "note": "x", "reply": "y",
           "tools": ['delete_tag({"name": "删掉吧"})', 'delete_tag({"name": "删掉吧"})']},
          _SENT) is None)
check("早退③ 不在名字表的写工具（新建公告的标题走值通道，不在这一层判）",
      _target_grounding_refusal(_plan("create_announcement", {"title": "标题"}), _CTX) is None)
check("早退④ 参数解不出来 → 不判（不是这一层的事）",
      _target_grounding_refusal({"skill": "tag_delete", "params": {}, "note": "x",
                                 "reply": "y", "tools": ["delete_tag(不是 JSON)"]},
                                _SENT) is None)
check("早退⑤ 带 `$tool[N]` 引用的 spec → 不判（取值不来自这句话）",
      _target_grounding_refusal(
          {"skill": "tag_delete", "params": {}, "note": "x", "reply": "y",
           "tools": ['delete_tag({"name": "$list_tags[0].name"})']}, _SENT) is None)

print("\n④ 12 条弹卡用例：意图正确的 spec 一律原样通过（不误伤主人说过的话）")
# 表里的值是**从用例原话里读出来的**正解（不是实现里的常量）：gate 若把它拒掉，
# 就是"判据收紧了、把主人说过的话也拒了"——那比漏放更糟（弹卡直接消失）。
_POPUPS = {
    "admin_write_natural_confirm_popup":
        ("create_tag", {"title": "秋日随笔", "parent_tag": "", "color": "#eb2f96"}),
    # 20261004 改靶：golden 那条的前提（文章 1＋摄影）生产里双假（`get_article_detail(1)`
    # not_found、「摄影」noteCount 0）⇒ 换成 note 23 +「Python」。表里的值必须与
    # `basic.jsonl` 的原话同步——它是**从原话读出来的正解**，不同步这条锁就成了空断言。
    "admin_write_intent_tag_remove_popup":
        ("set_article_tags", {"article_id": 23, "remove": ["Python"], "add": []}),
    "admin_write_intent_named_target_only":
        ("set_article_status", {"article_id": 999999, "status": "private"}),
    "admin_tag_move_popup":
        ("update_tag", {"name": "Asyncio", "parent_tag": "编程", "level": "one"}),
    "admin_tag_delete_popup":
        ("delete_tag", {"name": "大笨狗", "level": "one"}),
    "admin_category_create_popup":
        ("create_category", {"title": "随手记"}),
    "admin_announcement_create_popup":
        ("create_announcement", {"title": "今晚维护", "content": "今晚 23 点开始维护，预计一小时"}),
    "admin_announcement_delete_popup":
        ("delete_announcement", {"title": "公告"}),
    # `admin_board_audit_popup` **撤出本表**（20260930 批 H）：复核那件的目标改成台账
    # 编号（`_ledger_target_refusal`），名字通道的表里已经没有它 ⇒ 留在这里也只是
    # 每跑一次都恒过的一条空断言（函数见工具名不在 `_WRITE_NAME_FIELDS` 里就直接放行）。
    # 它现在有两处真判据：离线那半边在 `test_tag_admin.py` ⑪（台账里在/不在/已复核
    # 三态），端到端那半边是 golden 的 `admin_board_audit_reviewed_refusal`。
    "admin_board_delete_popup":
        ("delete_board_comment", {"quote": "泠月喵好笨啊"}),
    "admin_tag_create_invented_name_popup":
        ("create_tag", {"title": "向量数据库", "parent_tag": "编程"}),
    "admin_tag_create_generic_name_popup":
        ("create_tag", {"title": "夜航船", "parent_tag": "", "color": "#eb2f96"}),
}
_cases = {c["id"]: c for c in (
    json.loads(ln) for ln in
    (ROOT / "eval/golden/basic.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip())}
check("11 条弹卡用例都在（用例文件改名/被删时这里要跟着改）",
      all(cid in _cases for cid in _POPUPS),
      "；".join(sorted(set(_POPUPS) - set(_cases))))
for cid, (tool, args) in _POPUPS.items():
    case = _cases.get(cid)
    if not case:
        continue
    msg = case["user_input"]
    got = _target_grounding_refusal(_plan(tool, args), msg)
    check(f"  {cid}：正解不被拒（卡片照弹）", got is None, str(got)[:120])
    # 目标名字段（表内那条）逐字段可溯源——"卡片上的名字是主人说出口的字"
    tkey, pkey = g._WRITE_NAME_FIELDS.get(tool) or (None, None)
    for key in (tkey, pkey):
        if key and str(args.get(key) or "").strip():
            check(f"    {cid}.{key}={args[key]!r} 可由主人这句话取出",
                  _msg_grounded_name(str(args[key]), msg))

print("\n⑤ 判据里不再有词表（AST 级：防补丁式词表加回来）")
check("_owner_target_span 规则④ 不引用泛称/动作词表",
      not (_names_in(g._owner_target_span)
           & {"_GENERIC_NAME_WORDS", "_TARGET_ACTION_MARKS"}),
      str(sorted(_names_in(g._owner_target_span))))
check("门函数不引用任何词表",
      not (_names_in(_target_grounding_refusal)
           & {"_GENERIC_NAME_WORDS", "_TARGET_ACTION_MARKS", "_GENERIC_VALUE_WORDS"}))
check("判据读的是主人原话本身（四个抽取器，全部由名词/引号/句读这些结构锚定）",
      {"_msg_quote_spans", "_msg_name_slot", "_bare_target_name", "_msg_pre_noun_runs"}
      <= _names_in(_msg_grounded_name),
      str(sorted(_names_in(_msg_grounded_name))))
check("P 以**剥过**的形态参与 `_name_like`（裸的 `_msg_pre_noun_runs` 仍不参与）",
      "_pre_noun_names" in _names_in(_name_like)
      and "_msg_pre_noun_runs" not in _names_in(_name_like))
check("  剥的规矩住在 `_pre_noun_names` 里（判据与取值同源，改一端会被抓住）",
      "_PRE_NOUN_LEAD_RE" in _names_in(g._pre_noun_names)
      and "_PRE_NOUN_TAIL_RE" in _names_in(g._pre_noun_names)
      and "_DEICTIC_WORDS" in _names_in(g._pre_noun_names))

print("\n⑥ 值通道的指代（20260927：代词是原话的子串 ⇒ 来源态判据被上游校正器自满足）")
# 现场（档位对照 trace 20260927T041139，native 不思考档）：原话
# 「给我建个新标签，然后把这篇文章的标签换成它」，模型给的值是自编的「AI Agent」，
# 而「换成」是命名标记 ⇒ 抽取器把紧跟其后的代词当成"新名字"，校正器据此把自编值
# 改写成「它」——**逐字子串判据对代词恒真**，于是错值一路进写工具，两不思考档 5/6。
_DEFECT_MSG = "给我建个新标签，然后把这篇文章的标签换成它"
check("抽取端：命名标记后面跟的是指代 ⇒ 不是名字（原话实证）",
      _msg_named_value(_DEFECT_MSG) == "" and _value_clean("它") == ""
      and _value_clean("这个") == "" and _value_clean("那些") == "")
check("引号 = 主人明说的字面量 ⇒ 起名叫「它」仍然合法（判据收窄不误伤）",
      _msg_named_value("把那个标签改名叫「它」") == "它"
      and _value_clean("「它」") == "它")
check("真名字两端都不受影响",
      _msg_named_value("在「编程」下面建一个二级标签，名字叫「向量数据库」") == "向量数据库"
      and _value_clean("向量数据库") == "向量数据库")
check("判据端：指代一律不算有据（就在原话里也不算）",
      not _grounded_value("它", _DEFECT_MSG) and not _grounded_value("那些", "把那些标签删掉")
      and _grounded_value("向量数据库", "名字叫「向量数据库」"))
# 现场重放：模型参数照 trace 原样（got={"title": "AI Agent"}），断言校正器**不再**
# 把它改写成代词，而是走"定不了 ⇒ 确定性零写 + 如实追问"那条路。
po = _plan("create_tag", {"title": "AI Agent"}, skill="tag_create")
_r = _name_arg_fix(po, _DEFECT_MSG, role="admin")
check("现场重放：返回值是零写 + 追问（不是校正）",
      _r is not None and _r[0] == "create_tag", str(_r)[:120])
check("现场重放：计划里的值一个字没动（没有「它」）",
      _args_of(po).get("title") == "AI Agent", str(po["tools"]))
check("现场重放：追问文案点名的是模型那个值（不是代词）",
      _r is not None and "AI Agent" in _r[1], (_r or ("", ""))[1][:160])
po = _plan("create_tag", {"title": "向量数据库"}, skill="tag_create")
check("反向：值本来就有据 ⇒ 校正器不介入（防线不是重写器）",
      _name_arg_fix(po, "在「编程」下面建一个二级标签，名字叫「向量数据库」",
                    role="admin") is None
      and _args_of(po).get("title") == "向量数据库")
check("两端同源（AST 级：抽取器与判据都得认这一族，改一端会被抓住）",
      "_DEICTIC_WORDS" in _names_in(_value_clean)
      and "_DEICTIC_WORDS" in _names_in(_grounded_value),
      f"{sorted(_names_in(_value_clean))} / {sorted(_names_in(_grounded_value))}")
check("这一族是**封闭类**（穷举得完：短词表，不是词形族的开放扩张）",
      len(_DEICTIC_WORDS) <= 40 and all(len(w) <= 3 for w in _DEICTIC_WORDS))

print("\n⑦ 待办的引用式唯一命中（20260929 批 G）：候选只来自主人**没加引号**的原话")
# P2 那一支（`_write_target_refusal` 的 `is_todo` 格）把"主人在原话里唯一指向台账一行"
# 当作候选来源。与 ①–⑥ 是同一个题材：**是主人的字才算数**。行为面（唯一命中 ⇒ 校正成
# 台账原文；0 行 / ≥2 行 ⇒ 零写）在 `test_todo_schedule.py` ⑰b 用三道真门跑；这一节钉的
# 是"来源"本身——纯函数、无夹具、无网络。
_ROWS = [{"text": "更新简历（国庆后）"}, {"text": "买猫粮"}, {"text": "菜"}]
check("没加引号的指称 ⇒ 命中那一行（主人说「简历那条」，台账行里含「简历」）",
      [r["text"] for r in g._todo_reference_rows(_ROWS, "把简历那条挪到 10 月 8 号")]
      == ["更新简历（国庆后）"])
check("★ 同一批字放进引号 ⇒ **不**命中（引号里那一段归引号通道，20260927 的纪律）",
      g._todo_reference_rows(_ROWS, "把「简历」那条挪到 10 月 8 号") == [])
check("  抹掉空白后仍判得动（主人/模型转写常带空格）",
      [r["text"] for r in g._todo_reference_rows(_ROWS, "把 简历 那条挪到 10 月 8 号")]
      == ["更新简历（国庆后）"])
check("一行都指向不到 ⇒ 空（调用方按「查无此条」如实拒绝，不替主人挑）",
      g._todo_reference_rows(_ROWS, "把那条挪到 10 月 8 号") == [])
check("多行被引用 ⇒ **全部**返回（歧义交给调用方拒绝，这里不做「挑一条」这件事）",
      [r["text"] for r in g._todo_reference_rows(
          [{"text": "更新简历"}, {"text": "简历附件"}], "把简历那条挪一下")]
      == ["更新简历", "简历附件"])
check("只有**一个字**的正文永远不入选（凑不出 2-gram ⇒ 一个字没有指认力）",
      g._todo_reference_rows(_ROWS, "把菜那条挪一下") == [])
check("★ 没有第二个来源：判据只看主人原话（签名里没有 planner 填的那个值）",
      list(inspect.signature(g._todo_reference_rows).parameters) == ["rows", "user_msg"],
      str(list(inspect.signature(g._todo_reference_rows).parameters)))
check("  去掉引号段 ≠ 去掉标点（只摘引号里那一段，别的字一个不动）",
      g._msg_without_quotes("把「简历」那条挪到 10 月 8 号").replace(" ", "")
      == "把那条挪到10月8号")

print("\n⑧ 第二本账：系统台账那一行「待主人点头（还没做）」（20261006 事故）")
# 出处闸**只认主人这一轮的话**，而 planner 的规则从 20260923 起就写着"短应答先还原
# 语义"（主人回「嗯」「排期到今天」，那件事的参数在上一轮那张卡的台账行里）——
# 两条规则对着同一件事给出相反判定。生产 trace `20261006T165209` / `T165231`：零写、
# 卡收回、那一行永远 pending，主人再说什么都撞同一堵墙。这一节钉第二本账：
#   ① 台账**在** ⇒ 放行（三种闸各自的形态）；
#   ② 台账**不在**（或那行里没有这个值）⇒ 一个字都不许松（反向对照，缺了它这节等于没测）；
#   ③ 脏判据（泛称/指代）对第二本账**同样生效**——它是先判的，不因为"台账里有"就放行。
from agent.graph import _board_quote_fix, _ledger_pending_text  # noqa: E402

check("接线：三道闸都吃 `ledger_src`，`planner_node` 一次算好往下传（漏传＝静默回旧行为）",
      "ledger_src" in inspect.signature(_name_arg_fix).parameters
      and "ledger_src" in inspect.signature(_board_quote_fix).parameters
      and "ledger_src" in inspect.signature(g._todo_text_fix).parameters
      and "ledger_src = _ledger_pending_text(state.get(\"ledger\"))"
      in (ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))
check("台账取用只认那一个键（`ledger` 缺席/不是 dict ⇒ 空串，不是异常、更不是 None 串）",
      _ledger_pending_text(None) == "" and _ledger_pending_text({}) == ""
      and _ledger_pending_text({"pending": None}) == ""
      and _ledger_pending_text({"pending": "X"}) == "X"
      and _ledger_pending_text({"executions": "Y"}) == "")

_TAG = "探针色_20261006"
_LED = ('新建标签「%s」；动作 create_tag；参数 '
        '[{"args":{"title":"%s"},"tool":"create_tag"}]；状态 awaiting（等主人点头，尚未执行）'
        % (_TAG, _TAG))
check("值那一族：`嗯` + 台账里记着这个新名字 ⇒ 放行（上一轮那张卡上就是它）",
      _name_arg_fix(_plan("create_tag", {"title": _TAG}), "嗯", role="admin",
                    ledger_src=_LED) is None)
check("  反向对照：台账缺席 ⇒ 仍然拒（这一支不能因为加了第二本账就松掉）",
      _name_arg_fix(_plan("create_tag", {"title": _TAG}), "嗯", role="admin") is not None)
check("  反向对照二：台账在、但里面**没有**这个值（模型新编的）⇒ 仍然拒",
      _name_arg_fix(_plan("create_tag", {"title": "AI Agent"}), "嗯", role="admin",
                    ledger_src=_LED) is not None)
check("  **故意**的宽边界：台账那一行是**渲染过的行**，骨架里的字（如 `awaiting`）"
      "也落在「逐字出现」里 ⇒ 本判据放行——它是行内的字符串判据，不是字段级对照。"
      "代价可接受的理由写在这里：那个值会进弹卡由主人过目（写面没有静默路）",
      _grounded_value("awaiting", "嗯", _LED) is True
      and _grounded_value("created_at_不存在", "嗯", _LED) is False)
check("  脏判据先判且对第二本账同样生效：指代词即使在台账里出现也不算有据",
      _grounded_value("它", "把它换成那个", "改名叫 它") is False
      and _grounded_value("标签", "标签", "标签") is False)
check("  `user_msg` 那一支照旧先成立（有第二本账也不改变原判据）；"
      "⚠️ 这个函数的 `sq_msg` 契约是**已归一**的（`_squash_spaces`）——"
      "传原话进去会静默判否，调用点一律先归一",
      _grounded_value("AI Agent", g._squash_spaces("给我建个新标签 AI Agent"), "") is True
      and _grounded_value("AI Agent", "给我建个新标签 AI Agent", "") is False)

_Q = "今天天气真好"
_PB = _plan("delete_board_comment", {"quote": _Q}, skill="board_delete")
_LED_B = ('删掉河灯集里的那条留言「%s」；动作 delete_board_comment；参数 '
          '[{"args":{"quote":"%s"},"tool":"delete_board_comment"}]；状态 awaiting'
          % (_Q, _Q))
check("留言那一族：`嗯` + 台账里记着那个片段 ⇒ 放行",
      _board_quote_fix(dict(_PB), "嗯", 0, "admin", ledger_src=_LED_B) is None)
check("  反向对照：台账缺席 ⇒ 仍然拒（这一族是 %s 里的工具，一定会有 pending 行）"
      % "`_ALWAYS_CONFIRM_TOOLS`",
      _board_quote_fix(dict(_PB), "嗯", 0, "admin") is not None)
check("  反向对照二：台账行里没有这个片段 ⇒ 仍然拒",
      _board_quote_fix(dict(_PB), "嗯", 0, "admin",
                       ledger_src="删掉河灯集里的那条留言「别的什么」") is not None)
check("  主人自己加引号给的片段照旧优先（引号通道一个字不变）",
      _board_quote_fix(_plan("delete_board_comment", {"quote": "天气"},
                             skill="board_delete"),
                       "删掉「今天天气真好」那条", 0, "admin") is None
      and _args_of(_plan("delete_board_comment", {"quote": "天气"},
                         skill="board_delete")).get("quote") == "天气")

print()

if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
