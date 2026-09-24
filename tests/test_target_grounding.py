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

覆盖五块：
  ① 三个抽取器各自取得到什么（正例）；
  ② 真 planner 采样值的校正（20260924 三条现场 + move 族的父子错位）；
  ③ 门本身（`_target_grounding_refusal`）：该拒的拒、话术不越界、五条早退；
  ④ 12 条弹卡用例的**正解不受误伤**（意图正确的 spec 原样通过——这条锁的是反向风险：
     判据收紧了，别把主人说过的话也拒掉）；
  ⑤ 判据里不再有词表（AST 级：`_owner_target_span` 规则④ 与门函数都不引用
     `_GENERIC_NAME_WORDS` / `_TARGET_ACTION_MARKS`——防止有人把补丁式的词表加回来）。

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
    _bare_target_name, _msg_grounded_name, _msg_name_slot, _msg_quote_spans,
    _name_like, _name_target_fix, _target_grounding_refusal)

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
check("指代型（这句里连目标槽位都没有）⇒ 本门不介入（`_name_like` 假）",
      not _name_like("把那个标签删掉吧")
      and not _name_like("大笨狗那个标签删掉吧")   # 无槽位无引号 ⇒ 与指代同判（同款语序）
      and _name_like(_M))

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
    "admin_write_intent_tag_remove_popup":
        ("set_article_tags", {"article_id": 1, "remove": ["摄影"], "add": []}),
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
    "admin_board_audit_popup":
        ("audit_board_comment", {"quote": "泠月喵真棒！"}),
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
check("12 条弹卡用例都在（用例文件改名/被删时这里要跟着改）",
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
check("指代型仍不介入（P 不参与 `_name_like`：`把那个标签删掉吧` 判假）",
      "_msg_pre_noun_runs" not in _names_in(_name_like))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
