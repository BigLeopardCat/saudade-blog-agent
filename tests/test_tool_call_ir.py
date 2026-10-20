"""计划里的调用清单是**结构**，不是一段被反复解析的文本（20261019 批 G6）。

**这一件为什么存在**：`plan_obj["tools"]` 里每一条此前都是一段**渲染好的文本**
（`search_notes({"keyword": "架构"})`），写端十余处各自 `json.dumps`，读端**五套解析器**
各自把它拆回来——形状正则（`tool_specs.split_tool_spec`）、json/ast 双容错
（`tool_specs._tool_args`）、严格 `json.loads`（`tasks.bound_steps`）、切第一个括号
（`deterministic_plan._tool_name`）、还有两条**正则抠值**的（`_spec_arg` 抠 `key: value`、
`_DETAIL_SPEC_RE` 抠 `article_id`）。写端多一处、读端就多一处漂移面；那两条正则更是
"从我们自己渲染出来的串里把值猜回来"，改一次 JSON 排版就静默取不到——而取不到的后果
是**判据悄悄变钝**（退回用户整句 / 同一篇被当成没读过再读一遍），不报错、不留痕。

批 G6 把本体与渲染分开：`agent/workflow_model.py::ToolCall`（`str` 子类，`.name`/`.args`
是本体的名字与参数，`str()` 是它的渲染，与 `tools/result.py::ToolResult` 同一形状）。
被测六块：

  · ① **渲染逐字节相同**：一批真形状（中文 / 引号 / 嵌套 / 空参 / 数字 / 布尔 / null /
    换行 / 分号 / emoji）上 `str(ToolCall(name, args))` 与写端旧那行 f-string 逐字相等。
    少了它，读端二十多个消费点（`graph`/`write_grounding` 里的 `==`、`in`、切片）会一起变。
  · ② **两条路给出同一个答案**：`_tool_name` / `_tool_args` / `split_tool_spec` 在
    `ToolCall` 与"纯文本"（过了 checkpoint 序列化、trace 回放、测试夹具的形状）上
    逐项相等——回落支必须**仍然活着**，它不是历史包袱（`plan_obj` 会落进 checkpoint
    再读回来，`str` 子类过一遍序列化就退化成纯 `str`）。
  · ③ **写幂等账的指纹一个字节不变**：`_spec_signature` 收的本来就是 `(名字, 参数字典)`，
    对拍两条路算出的指纹。这条是"改 IR 不动库"的全部依据。
  · ④ **接线**（行为级）：真跑 `instantiate_plan` 与 `plan_state`，断言生产端交出的
    每一条都是 `ToolCall`、且编码回文本后**一字不变**。源码里换成 `ToolCall` 而没接上
    线（或某一支漏改），是这一族最常见的假绿。
  · ⑤ 反控：**改渲染不改本体**——把 `str(call)` 整段打乱，`.args` 一个字节不变；
    并当场证明被删的那条正则在同一份数据上会**读残**（旧读器确实错，不是"等价改写"）。
  · ⑥ `isinstance(x, str)` 仍为真（二十多个消费点因此一个字都不用改），而
    `type(x) is str` 为假——后者是本仓明令不许的写法，钉住它免得下一个人踩。

用法：.venv/bin/python tests/test_tool_call_ir.py
"""


from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from agent.plan_codec import plan_state  # noqa: E402
from agent.tool_specs import (  # noqa: E402
    _spec_signature,
    _tool_args,
    _tool_name,
    split_tool_spec,
)
from agent.workflow_model import ToolCall  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _old_render(name: str, args: dict) -> str:
    """写端**旧**那一行的逐字复刻（十余处都是这一句）。"""
    return f"{name}({json.dumps(args, ensure_ascii=False)})"


def test_tool_call_ir():
    CASES: list[tuple[str, dict]] = [
        ("search_notes", {"keyword": "架构"}),
        ("get_article_detail", {"article_id": 21}),
        ("set_article_status", {"article_id": 7, "status": "public", "is_top": True}),
        ("create_tag", {"title": "临江仙", "parent_tag": "诗词", "color": "#8ab4f8"}),
        ("list_notes", {}),
        ("navigate_to", {"path": "/device-console/", "confirm": False}),
        ("reschedule_dashboard_todo", {"text": "写周报", "date": "无"}),
        ("set_article_tags", {"article_id": 3, "tags": ["架构", "复盘"]}),
        ("send_user_notice", {"name": "他说「这样」", "content": '引号 "双" 与 \'单\''}),
        ("update_tag", {"tag_id": 9, "desc": "第一行\n第二行"}),
        ("create_tag", {"title": "分号;在值里", "meta": {"nested": {"deep": [1, None, 2.5]}}}),
        ("account_mute", {"name": "🌙 月亮", "days": None}),
    ]
    print("\n① 渲染逐字节相同：`str(ToolCall(...))` == 写端旧那行 f-string")
    check("正控：用例表非空且形状多样（空表会让下面整块变成空过）",
          len(CASES) >= 10 and any("{" in _old_render(n, a) for n, a in CASES),
          f"{len(CASES)} 条")
    for _name, _args in CASES:
        _call = ToolCall(_name, _args)
        _old = _old_render(_name, _args)
        check(f"{_name}：逐字节相同", str(_call) == _old, _old)
        check(f"{_name}：`==`/`in`/切片按**串**语义（消费点一个字不用改）",
              _call == _old and _call in [_old] and _call[:4] == _old[:4])
    check("空参：`ToolCall(name)` 与 `ToolCall(name, {})` 与旧写端同形（都是 `{}`）",
          str(ToolCall("list_notes")) == str(ToolCall("list_notes", {})) == "list_notes({})")
    check("反控：`ensure_ascii` 一旦取 True，中文就变成 `\\uXXXX` ⇒ 上面那批必须能分辨出来",
          _old_render("search_notes", {"keyword": "架构"})
          != "search_notes(" + json.dumps({"keyword": "架构"}) + ")")
    print("\n② 两条路（结构 / 纯文本）给出同一个答案——回落支仍然活着")
    for _name, _args in CASES:
        _call = ToolCall(_name, _args)
        _text = str(_call)
        _from_obj = (_tool_name(_call), _tool_args(_call))
        _from_text = (_tool_name(_text), _tool_args(_text))
        check(f"{_name}：名字与参数两条路逐项相等",
              _from_obj == _from_text and _from_obj[0] == _name and _from_obj[1][0] == _args,
              f"{_from_obj[0]} {_from_obj[1][1]}")
        check(f"{_name}：`split_tool_spec` 在两条路上给出同一对",
              split_tool_spec(_call) == split_tool_spec(_text) == (_name, _text[len(_name) + 1:-1]))
    check("纯文本条目（checkpoint 读回 / trace 回放 / 测试夹具的形状）照旧读得出",
          _tool_name('search_notes({"keyword": "架构"})') == "search_notes"
          and _tool_args('search_notes({"keyword": "架构"})') == ({"keyword": "架构"}, True))
    check("容错支仍在（模型的漂移：单引号 / Python 字面量）",
          _tool_args("search_notes({'keyword': '架构', 'isTop': True})")[0]
              .get("keyword") == "架构")
    check("反控：形状不匹配的串**不许**被猜成一条调用",
          split_tool_spec("这不是一条调用") == ("", "")
          and _tool_args("这不是一条调用") == ({}, True))
    print("\n③ 写幂等账的指纹：换 IR 不动库")
    for _name, _args in CASES:
        _call = ToolCall(_name, _args)
        _text = str(_call)
        _sig_obj = _spec_signature(_call.name, _call.args)
        _sig_text = _spec_signature(_tool_name(_text), _tool_args(_text)[0])
        check(f"{_name}：两条路算出的指纹逐字节相等", _sig_obj == _sig_text, _sig_obj[1][:40])
    check("指纹本身按**回执侧形态**归一（`str(v)[:200]` + sort_keys）——`1` 与 `\"1\"` 同键",
          _spec_signature("set_article_status", {"isTop": 1})
          == _spec_signature("set_article_status", {"isTop": "1"}))
    check("反控：指纹确实随参数变（不是恒等的空跑）",
          _spec_signature("set_article_status", {"article_id": 7})
          != _spec_signature("set_article_status", {"article_id": 8}))
    print("\n④ 接线（行为级）：生产端真跑出来的每一条都是 `ToolCall`")
    ROLE = "superadmin"
    PROD = [
        ("dashboard_todo_add", {"text": "写周报", "date": "明天"}),
        ("navigate", {"target": "后台"}),
        ("tag_create", {"name": "临江仙"}),
        ("content_query", {"calls": [{"tool": "search_notes", "args": {"keyword": "架构"}},
                                     {"tool": "list_notes", "args": {}}]}),
    ]
    for _wf, _params in PROD:
        _plan = instantiate_plan(_wf, _params, ROLE)
        _tools = _plan.get("tools") or []
        check(f"{_wf}：展开出 ≥1 条调用", bool(_tools), f"{len(_tools)} 条")
        check(f"{_wf}：每一条都是 `ToolCall`（漏改某一支的写端会在这里现形）",
              all(isinstance(t, ToolCall) for t in _tools),
              str([type(t).__name__ for t in _tools]))
        check(f"{_wf}：`.args` 与文本里那一段逐键相同",
              all(t.args == _tool_args(str(t))[0] for t in _tools),
              str([t.args for t in _tools]))
        _st = plan_state(dict(_plan, reply="", note=_plan.get("note") or ""))
        check(f"{_wf}：编码回文本后 TOOLS 行一字不变（`str` 子类过 `\"; \".join` 不掉壳）",
              all(str(t) in _st["plan"] for t in _tools),
              _st["plan"].splitlines()[2] if len(_st["plan"].splitlines()) > 2 else "")
        check(f"{_wf}：`plan_obj` 里留的仍是**同一个** `ToolCall`（写端没顺手再拷一份）",
              all(isinstance(t, ToolCall) for t in _st["plan_obj"]["tools"]))
    check("反控：`str` 子类的身份在**过一遍序列化**后会退化——回落支不是死代码",
          not isinstance(json.loads(json.dumps([str(t) for t in
                                                instantiate_plan("tag_create", {"name": "临江仙"},
                                                                 ROLE)["tools"]]))[0], ToolCall))
    print("\n⑤ 反控：改**渲染**不动**本体**（读的是结构，不是文本）")
    _OLD_SPEC_ARG_RE = re.compile(r'keyword["\']?\s*[:=]\s*["\']([^"\']+)["\']')
    _TRICKY = {"keyword": '他说"架构"好'}
    _call = ToolCall("search_notes", _TRICKY)
    _text = str(_call)
    check("结构路：值原样取回（引号是值的一部分）",
          _tool_args(_call)[0]["keyword"] == '他说"架构"好', _tool_args(_call)[0]["keyword"])
    _m = _OLD_SPEC_ARG_RE.search(_text)
    check("反控：旧读器在同一份数据上**读残**（`[^\"']+` 停在值里第一个引号处）",
          _m is not None and _m.group(1) != _TRICKY["keyword"], f"旧读器给出 {_m.group(1)!r}")
    _scrambled = "search_notes({" + "×" * 12 + "})"
    check("反控：`str(call)` 整段打乱 ⇒ `.args` 一个字节不变（本体不随渲染走）",
          ToolCall("search_notes", _TRICKY).args == _TRICKY and _scrambled != _text)
    check("反控：拿打乱后的文本去读，读出来的东西与本体**不同**（证明 ⑤ 不是恒真）",
          _tool_args(_scrambled)[0] != _TRICKY)
    print("\n⑥ `isinstance(x, str)` 仍为真——二十多个消费点一个字都不用改")
    _call = ToolCall("navigate_to", {"path": "/device-console/", "confirm": False})
    check("`isinstance(call, str)` 为真（`json.dumps`/f-string/`==`/`in` 全走串语义）",
          isinstance(_call, str) and isinstance(_call, ToolCall))
    check("`type(call) is str` 为**假**——本仓明令不许那种写法（会静默变假）",
          type(_call) is not str)
    check("没有覆写 `__eq__`/`__hash__`：两个同名同参的调用**相等**、可进集合",
          ToolCall("x", {"a": 1}) == ToolCall("x", {"a": 1})
          and len({ToolCall("x", {"a": 1}), ToolCall("x", {"a": 1})}) == 1)
    check("不同参数的调用**不相等**（`==` 仍是比渲染串，不是比对象）",
          ToolCall("x", {"a": 1}) != ToolCall("x", {"a": 2}))
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_tool_call_ir()
