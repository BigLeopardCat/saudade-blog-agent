"""计划双态通道（20260928 批 C）：写端唯一入口 + 两态不分叉 + 新旧判据等价。

被测 = 计划的**两态**：`agent/plan_codec.py::plan_state` 一次写出 `plan`（人读的契约文本）与
`plan_obj`（程序读的字段），以及"读端直取对象"这件事本身。

**为什么存在**：批 C 之前有四处读端靠**抠文本**认计划——`server.py` 的
`plan.startswith("WORKFLOW=")`（这是不是一份真计划）、`startswith("WORKFLOW=chat")`（闲聊轮）、
`"\nTOOLS: " in plan and "TOOLS: （无）" not in plan`（有没有执行清单），外加
`_plan_workflow` 自己写的一条 `WORKFLOW=` 正则（与 `parse_plan` 里那条是两份拷贝）。四处都是
"改 `plan_encode` 的排版就得同步改读端"的人工约定（全仓 94 处同族约定里的一处，见
`docs/` 那次架构审计）。现在读端直取 `plan_obj`，这份文件把新形状钉住：

  ① **写端唯一入口**（源码锁）：`plan_encode` 的**代码**调用点恰好一处、且在 `plan_state`
     体内；`agent/graph.py` 里再没有第二处手写 `"plan": …`（只有 `graph_input` 的初值 `""`）。
     —— 想绕开 `plan_state` 自己拼文本，得先删掉这条锁。
  ② **两态不分叉**：`parse_plan(plan_state(obj)["plan"])` 与 `obj` 在
     workflow/tools/params/note/reply/status 六项上逐项相等（两处**已知不同形**在下面显式归一）。
     这条保证"改了排版，文本那一态的语义没变"。**20261017 批 1 起文本不再是任何节点的读端**
     （四个生产读点都改走 `plan_view(state)`，见 ⑤）——② 判的是"渲染仍是保真的渲染"，
     不再是"渲染就是读端"。
     顺带钉住 **STATUS 的兼容派生在真计划上恒不触发**（缺 STATUS 才会走到按 NOTE 措辞
     反推 nav_offline/nav_unresolved/target_unreachable 那段）——它只是给旧文本与手写夹具
     留的路，见 `parse_plan` 里那段注。
  ③ **判据等价**：新读法（看 `workflow == "chat"` / `tools` 非空）与旧读法（在文本上
     startswith / 子串）在**技能注册表全集**上判定相同；一处刻意的偏离单独钉住：
     技能名 `chat` 前缀的唯一性。
  ④ **分隔符不进值**（20260929）：TOOLS 行是 `"; ".join(specs)` 拼的，`parse_plan` 用
     `split(";")` 读回 ⇒ 参数值里一个裸 `;` 就把**一条调用撕成 N 条**（`_tool_args` 的
     贪婪正则兜得住值里的 `(`/`)`，兜不住 `;`）。写端现在把值里的 `;` 转义成 JSON
     的 `\u003b`（读回仍是 `;`，语义一个字节不变）。④ 锁的就是这条不变量：
     TOOLS 行的裸 `;` 只许是分隔符。

  ⑤ **读侧改结构化**（20261017 批 1）：四个生产读点全部收到 `plan_view(state)`（对象那一态），
     不再读渲染文本。三条判据 = 源码锁（`agent/` 下零个 `parse_plan` 代码调用点）＋
     正控（文本在场、对象缺席 ⇒ 路由去 model、零执行；**改之前这条必红**）＋
     等价（`plan_view` 与 `parse_plan` 在契约七格上逐项相等）。三条各带反控。

**不测什么**：不测 `plan_encode` 的**排版**（`test_workflows.py` 有往返用例）、不测
planner/gate 行为（各有专测）。这里只锁"通道的形状"。

跑法：`.venv/bin/python tests/run_all.py -k plan_channel`
"""


import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

import server as SRV  # noqa: E402  （③ 读它的新读法：`_spec_one`/`_specs_from_tools`）
from agent import deterministic_plan as D  # noqa: E402  （② 取样 `_terminal_plan`/`_wrap_up_plan`）
from agent import graph as G  # noqa: E402
from agent.plan_codec import (  # noqa: E402  （④ 的写端转义：20261017 起住这里）
    _CONTRACT_KEYS,
    _esc_spec,
)
from agent.principal import SOURCE_ASSERTION, Principal  # noqa: E402
from agent.workflow_defs import WORKFLOW_MAP  # noqa: E402
from agent.workflow_params import _param_problem_plan  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_GRAPH_SRC = _ctx_src.plan_codec_src()


_DOCSTR = re.compile(r'""".*?"""', re.DOTALL)


_CODE_LINES = _DOCSTR.sub(lambda m: "\n" * m.group(0).count("\n"), _GRAPH_SRC).splitlines()


def _code_hits(pattern: str) -> list[tuple[int, str]]:
    """代码行里命中正则的 (行号, 行内容)；纯注释行不算。"""
    out = []
    for i, ln in enumerate(_CODE_LINES, 1):
        if ln.lstrip().startswith("#"):
            continue
        if re.search(pattern, ln):
            out.append((i, ln.strip()))
    return out


def _span_of(name: str) -> tuple[int, int]:
    """`def name(` 的函数体区间（1-based，含首尾；到下一个 def/class/装饰器为止）。"""
    lines = _CODE_LINES
    start = next(i for i, l in enumerate(lines) if l.startswith(f"def {name}("))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith(("def ", "class ", "@"))), len(lines))
    return start + 1, end


_NORM = {
    "note": lambda v: "" if v in (None, "", "（无）") else v,
    "tools": lambda v: [G._tool_args(s) for s in (v or [])],
}


_KEYS = _CONTRACT_KEYS


def _real_plans() -> list[tuple[str, dict]]:
    """真计划的取样：注册表全集（空参）+ navigate 四条出口 + 四条**确定性构造路径**。

    后四条是 20261017 批 1 补的：它们不走 `instantiate_plan`，此前**没有任何判据**
    盯过"这两态在它们身上是否也不分叉"——而 `_confirm_grant_plan` 恰恰是"对象少一个
    `status` 键"的那条路径（`normalize_plan` 就是为它这一类存在的）。
    """
    out = [(f"{n}(空参)", instantiate_plan(n, {})) for n in WORKFLOW_MAP]
    for target in ("物联网平台", "友链", "/iot", "量子对撞机车间"):
        out.append((f"navigate({target})", instantiate_plan("navigate", {"target": target})))
    out.append(("_terminal_plan", D._terminal_plan(True, "max_rounds")))
    out.append(("_wrap_up_plan", D._wrap_up_plan(True)))
    out.append(("_param_problem_plan", _param_problem_plan(
        WORKFLOW_MAP["navigate"],
        {"fixed": [], "unknown": [], "missing": ["target"], "bad": []})))
    out.append(("_confirm_grant_plan", G._confirm_grant_plan(
        {"workflow": "tag_create",
         "specs": [{"tool": "create_tag", "args": {"name": "甲"}}]})))
    # 值里带 `;` 的一条（`_esc_spec` 的射程）。**少了它 `_NORM["tools"]` 从不被触发**
    # ——上面 55 例的参数值里一个分号都没有，于是"比语义不比字节"那句归一在 ② 里是空跑，
    # 一条从不被触发的归一器与没有它一样（本仓最贵的那类失效模式）。
    out.append(("值里带 `;`", {"workflow": "tag_delete",
                               "tools": ['delete_tag({"name": "甲;乙"})'],
                               "params": {}, "note": "", "reply": "r"}))
    return out


def _keyerror(fn) -> bool:
    try:
        fn()
    except KeyError:
        return True
    return False


def _old_verdict(plan_text: str) -> tuple:
    """批 C 之前 `server.py` 的三段文本嗅探（逐字照抄，作对照臂）。"""
    return (plan_text.startswith("WORKFLOW="),
            plan_text.startswith("WORKFLOW=chat"),
            "\nTOOLS: " in plan_text and "TOOLS: （无）" not in plan_text)


def _new_verdict(pobj: dict) -> tuple:
    """现在的读法（`server.py` 现状：真计划 / 闲聊轮 / 有执行清单）。"""
    return (bool(pobj),
            str(pobj.get("workflow") or "") == "chat",
            bool(pobj.get("tools")))


def _old_specs_from_plan(plan_text: str) -> list:
    """批 C 删掉的那段"从文本切 TOOLS 行"（照抄，作对照臂）。"""
    out = []
    m = re.search(r"TOOLS\s*[:=]\s*(.+)", plan_text or "", re.IGNORECASE)
    if not m:
        return out
    for spec in m.group(1).split(";"):
        spec = spec.strip()
        if not spec or spec in ("（无）",):
            continue
        out.append(spec.split("(", 1)[0].strip())
    return out


def _spec_with(name: str, key: str, value) -> str:
    """按 `instantiate_plan` 的落盘形状造一条 spec（参数走 json.dumps）。"""
    return f"{name}({json.dumps({key: value}, ensure_ascii=False)})"


def _tools_line(txt: str) -> str:
    return next(l for l in txt.splitlines() if l.startswith("TOOLS"))


def _plan_with(tools: list[str]) -> str:
    return G.plan_state({"workflow": "tag_delete", "tools": tools, "params": {},
                         "note": "", "reply": "r"})["plan"]


class _Spy:
    """只为数调用次数（与真工具无关）：`_TOOL_MAP` 的调用面就是 `.invoke(args)`。"""

    def __init__(self):
        self.calls: list = []

    def invoke(self, args):
        self.calls.append(args)
        return "ok"


def _divergences(st: dict) -> list:
    """对象那一态与文本那一态在契约七格上的差（`_NORM` 归一掉两处已知渲染）。"""
    view, parsed = G.plan_view(dict(st)), G.parse_plan(st.get("plan") or "")
    out = []
    for k in _KEYS:
        norm = _NORM.get(k, lambda v: v)
        if norm(view.get(k)) != norm(parsed.get(k)):
            out.append(f"{k}: 对象={norm(view.get(k))!r} 文本={norm(parsed.get(k))!r}")
    return out


def test_plan_channel():
    print("① 写端唯一入口（源码锁）")
    _enc = _code_hits(r"\bplan_encode\s*\(")
    _ps_lo, _ps_hi = _span_of("plan_state")
    _calls = [(i, l) for i, l in _enc if not l.startswith("def plan_encode")]
    check("plan_encode 的代码调用点恰好一处", len(_calls) == 1, str(_calls))
    check(f"那一处落在 plan_state 体内（行 {_ps_lo}–{_ps_hi}）",
          bool(_calls) and _ps_lo <= _calls[0][0] <= _ps_hi, str(_calls[:1]))
    _plan_keys = _code_hits(r'"plan"\s*:')
    check("契约族（graph.py + plan_codec.py）里手写 \"plan\": 只剩两处"
      "（plan_state 的返回 + graph_input 初值）",
          len(_plan_keys) == 2, str([h[1][:60] for h in _plan_keys]))
    check("  其一在 plan_state 体内（与 plan_encode 同一行）",
          any(_ps_lo <= i <= _ps_hi and "plan_encode" in l for i, l in _plan_keys),
          str([i for i, _ in _plan_keys]))
    check("  另一处是 graph_input 的初值，且同一行给了 plan_obj 的初值",
          any('"plan": ""' in l and "plan_obj" in l for _, l in _plan_keys),
          str([l[:70] for _, l in _plan_keys if '"plan": ""' in l]))
    print("② 两态不分叉（对象 ↔ 文本）")
    _cases = _real_plans()
    check("取样非空（注册表全集 + navigate 四出口 + 四条确定性构造路径 + 一条带 `;` 的值）",
          len(_cases) >= len(WORKFLOW_MAP) + 5, f"{len(_cases)} 例")
    _div, _derived = [], []
    for label, obj in _cases:
        st = G.plan_state(obj)
        if st["plan_obj"] is not obj:
            _div.append(f"{label}:plan_obj 不是同一份对象")
            continue
        parsed = G.parse_plan(st["plan"])
        for k in _KEYS:
            norm = _NORM.get(k, lambda v: v)
            if norm(obj.get(k)) != norm(parsed.get(k)):
                _div.append(f"{label}:{k} 对象={norm(obj.get(k))!r} 文本={norm(parsed.get(k))!r}")
        # 两态的 STATUS 派生必须同源：写侧 `normalize_plan` 派生了什么，文本读回来就得是
        # 同一个值（它们共用 `_derive_status`；这条盯的是"别哪天有人只改一边"）。
        if (obj.get("status") or "") != (parsed.get("status") or ""):
            _derived.append(f"{label}:对象 {obj.get('status')!r} / 文本 {parsed.get('status')!r}")
    check("契约七格逐项相等（workflow/params/tools/note/reply/chat/status，两处渲染已还原）",
          not _div, "；".join(_div[:5]))
    check("STATUS 的派生两态同源（写侧派生的值与文本读回来的一致）",
          not _derived, "；".join(_derived[:5]))
    check("空对象进 plan_encode 会 KeyError（读端因此用 `if pobj:` 认「本轮没有计划」）",
          _keyerror(lambda: G.plan_encode({})))
    print("③ 判据等价（新旧读法）")
    _bad = []
    for n in WORKFLOW_MAP:
        for obj in ({"workflow": n, "tools": [], "params": {}, "note": "", "reply": "r"},
                    {"workflow": n, "tools": [f'list_tags({{"n": "{n}"}})'], "params": {},
                     "note": "", "reply": "r"}):
            st = G.plan_state(obj)
            if _old_verdict(st["plan"]) != _new_verdict(obj):
                _bad.append(f"{n}:{obj['tools']}")
    check("注册表全集 × {零工具, 一件工具}：新旧判定逐项相同", not _bad, "；".join(_bad[:5]))
    check("空计划（没有 plan_obj / 空文本）两边都判「不是真计划」",
          _old_verdict("") == _new_verdict({}) == (False, False, False))
    _dup = [n for n in WORKFLOW_MAP if n != "chat" and n.startswith("chat")]
    check("技能名里以 chat 开头的只有 chat 自己（新读法 "
        "`== \"chat\"` 与旧 startswith 等价的唯一前提）",
          not _dup, str(_dup))
    _spec = 'tag_delete({"name": "a;b"})'
    _st = G.plan_state({"workflow": "tag_delete", "tools": [_spec], "params": {},
                        "note": "", "reply": "r"})
    check("值里含 `;` 时三段嗅探的判定两边仍然一致（差的是预览列表，不是闸）",
          _old_verdict(_st["plan"]) == _new_verdict(_st["plan_obj"]))
    check("  新读法（直取对象）照旧一条、参数完整",
          SRV._specs_from_tools([_spec]) == [("tag_delete", {"name": "a;b"})],
          str(SRV._specs_from_tools([_spec])))
    print("④ 分隔符不进值：TOOLS 行的裸 `;` 只许是分隔符，不许长在参数值里")
    check("_esc_spec 只动 ASCII 分号：全角 `；`、`(`/`)`/`,`/反斜杠一律原样",
          _esc_spec("；") == "；" and _esc_spec("a(b),c\\d") == "a(b),c\\d")
    check("  且幂等（转义产物里没有裸分号，再来一次不再变）",
          _esc_spec(_esc_spec("a;b")) == _esc_spec("a;b") == "a\\u003bb")
    _bad = []
    for _v in ("a;b", "a;b;c", "；全角不算分隔符；", "分号后带空白 ; ", "a" + ";" * 12 + "z"):
        _txt = _plan_with([_spec_with("tag_delete", "name", _v)])
        _got = G.parse_plan(_txt)["tools"]
        _args, _ok = G._tool_args(_got[0]) if len(_got) == 1 else ({}, False)
        if _tools_line(_txt).count(";") or len(_got) != 1 or not _ok or _args.get("name") != _v:
            _bad.append(f"{_v!r}: 裸分号={_tools_line(_txt).count(';')} 条数={len(_got)} "
                    f"读回={_args.get('name')!r}")
    check("单条 spec：值里 1~12 个 `;` 都不裂（TOOLS 行 0 个裸分号、解析回来一条、值逐字节相同）",
          not _bad, "；".join(_bad[:3]))
    _two = [_spec_with("tag_delete", "name", "甲;一"), _spec_with("tag_delete", "name", "乙;二")]
    _txt = _plan_with(_two)
    _got = G.parse_plan(_txt)["tools"]
    _vals = [G._tool_args(s)[0].get("name") for s in _got]
    check("两条 spec：TOOLS 行的裸分号数 == 分隔符数（N-1）——漏转义与转多两个方向都在这一条里",
          _tools_line(_txt).count(";") == len(_two) - 1 and len(_got) == 2
          and _vals == ["甲;一", "乙;二"],
          f"裸分号={_tools_line(_txt).count(';')} 条数={len(_got)} 读回={_vals}")
    _txt = _plan_with([_spec_with("tag_delete", "name", "上\n下")])
    _got = G.parse_plan(_txt)["tools"]
    _val = G._tool_args(_got[0])[0].get("name") if len(_got) == 1 else None
    check("值里含换行时 TOOLS 行仍是一行（json.dumps 转义），读回来逐字节含那个换行",
          len(_got) == 1 and _val == "上\n下", f"条数={len(_got)} 读回={_val!r}")
    _st2 = G.plan_state({"workflow": "navigate", "tools": [], "params": {"target": "a;b;c"},
                         "note": "", "reply": "r"})
    check("PARAMS 行不受影响（分号原样进去、原样读回）",
          G.parse_plan(_st2["plan"])["params"] == {"target": "a;b;c"},
          str(G.parse_plan(_st2["plan"])["params"]))
    _PROD_SHAPE = ('tri(30,6,18,24,42,24,F); tri(98,6,86,24,110,24,F); circle(64,38,26,F); '
               'circle(54,34,4,T); circle(74,34,4,T); line(64,42,64,46,F); '
               'line(64,46,58,50,F); line(64,46,70,50,F); line(20,40,44,44,F); '
               'line(20,48,44,48,F); line(84,44,108,40,F); line(84,48,108,48,F); '
               'text(40,58,"meow")')
    check("复现串就是那次事故的形状（13 段、12 个分号）", _PROD_SHAPE.count(";") == 12,
          str(_PROD_SHAPE.count(";")))
    _old_specs = _old_specs_from_plan("TOOLS: " + _PROD_SHAPE)
    check("对照臂：同一串不经转义（= 改写前写端的产物）会被旧法撕成 13 条",
          len(_old_specs) == 13, str(len(_old_specs)))
    _txt = G.plan_state(instantiate_plan("tag_delete", {"name": _PROD_SHAPE}))["plan"]
    _specs = G.parse_plan(_txt)["tools"]
    _args, _ok = G._tool_args(_specs[0]) if len(_specs) == 1 else ({}, False)
    check("生产复现：走真技能（instantiate_plan → plan_state → parse_plan）恰好一条 spec",
          len(_specs) == 1, f"{len(_specs)} 条：{[s[:22] for s in _specs[:4]]}")
    check("  名是 delete_tag、name 逐字节等于原文（转义读回还原成分号）",
          bool(_specs) and _specs[0].startswith("delete_tag(") and _ok
          and _args.get("name") == _PROD_SHAPE,
          f"ok={_ok} 读回 {len(_args.get('name') or '')} 字 / 原文 {len(_PROD_SHAPE)} 字")
    check("  且 TOOLS 行 0 个裸分号（只有一条 spec，连分隔符都不该有）",
          _tools_line(_txt).count(";") == 0, str(_tools_line(_txt).count(";")))
    print("⑤ 读侧改结构化：生产链路不再读渲染出来的文本")
    _AGENT_PY = sorted((ROOT / "agent").rglob("*.py"))
    _hits = []
    for _p in _AGENT_PY:
        if _p.name == "plan_codec.py":
            continue                     # 文本读器自己：`parse_plan` 的定义与文档就在这儿
        for _n in ast.walk(ast.parse(_p.read_text(encoding="utf-8"))):
            if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
                    and _n.func.id == "parse_plan"):
                _hits.append(f"{_p.relative_to(ROOT)}:{_n.lineno}")
    check(f"⑤a 扫面非空（agent/ 下 {len(_AGENT_PY)} 个模块，plan_codec.py 除外）",
          len(_AGENT_PY) > 30, f"{len(_AGENT_PY)} 个")
    check("⑤a `agent/` 下（`plan_codec.py` 自身除外）`parse_plan(` 的**代码调用点为零**"
      "（谁再把渲染文本读回生产链路，当场红）", not _hits, str(_hits))
    _pobj = {"workflow": "content_query", "tools": ['spy_read({"n": 1})'], "params": {},
             "note": "", "reply": "r"}
    _text_only = {"plan": G.plan_state(dict(_pobj))["plan"], "plan_obj": {},
                  "messages": [], "plan_rounds": 1, "done": False}
    check("⑤b 前提：这份 state 的**文本里确实有调用清单**（旧读法会照着它去 execute）",
          G.parse_plan(_text_only["plan"])["tools"] == ['spy_read({"n": 1})'],
          str(G.parse_plan(_text_only["plan"])["tools"]))
    _spy = _Spy()
    _orig_spy = G._TOOL_MAP.get("spy_read")
    G._TOOL_MAP["spy_read"] = _spy
    _cfg = {"configurable": {"user_id": 0,
                             "principal": Principal(uid=7, role="user",
                                                    source=SOURCE_ASSERTION)}}
    try:
        _r = G.execute_node(dict(_text_only), _cfg)
        check("⑤b 正控（改读对象之后必红的那条）：文本在场、对象缺席 ⇒ 路由去 model、执行零条",
              G.route_after_planner(dict(_text_only)) == "model"
              and _r["messages"] == [] and _spy.calls == [],
              f"路由={G.route_after_planner(dict(_text_only))} "
          f"帧={len(_r['messages'])} 调用={_spy.calls}")
        # 反控：同一个计划**给了对象** ⇒ 路由去 execute、工具真被调用一次。少了这一条，
        # 上面那条"零执行"既可能是判据生效，也可能是夹具本身根本执行不动（本仓
        # 「正控不红不算数」——一条永远不会红的判据与没有它一样）。
        _both = {**G.plan_state(dict(_pobj)), "messages": [], "plan_rounds": 1, "done": False}
        _r2 = G.execute_node(dict(_both), _cfg)
        check("⑤b 反控：同一个计划给了对象 ⇒ 路由去 execute、工具真被调用一次"
          "（证明上面那条不是夹具坏掉导致的假绿）",
              G.route_after_planner(dict(_both)) == "execute"
              and len(_r2["messages"]) == 1 and _spy.calls == [{"n": 1}],
              f"路由={G.route_after_planner(dict(_both))} "
          f"帧={len(_r2['messages'])} 调用={_spy.calls}")
    finally:
        if _orig_spy is None:
            G._TOOL_MAP.pop("spy_read", None)
        else:
            G._TOOL_MAP["spy_read"] = _orig_spy
    print("⑤c 读侧入口与文本读器等价（plan_view ↔ parse_plan）")
    _bad = []
    for _label, _obj in _real_plans():
        _d = _divergences(G.plan_state(dict(_obj)))
        if _d:
            _bad.append(f"{_label}: " + "；".join(_d))
    check("⑤c 契约七格逐项相等（注册表全集 + navigate 四出口 + 四条确定性构造路径 + 一条带 `;`）"
      "——改读对象没顺手改掉语义",
          not _bad, "；".join(_bad[:3]))
    check("⑤c 没有计划时两边兜底同源（`plan_view({})` 逐项等于 `parse_plan(\"\")`）",
          not _divergences({}), str(_divergences({})))
    _st3 = G.plan_state({"workflow": "chat", "tools": [], "params": {}, "note": "", "reply": "r"})
    _fake = dict(_st3, plan_obj={**_st3["plan_obj"], "workflow": "navigate"})
    check("⑤c 反控：对象与文本不一致时判得出来（不是恒真的空跑）",
          bool(_divergences(_fake)), "伪造的分叉没被抓到 = 这条判据没在看两态")
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：" + "、".join(FAILS))
        sys.exit(1)
    print("✅ 计划双态通道全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_plan_channel()
