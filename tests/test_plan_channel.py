# -*- coding: utf-8 -*-
"""计划双态通道（20260928 批 C）：写端唯一入口 + 两态不分叉 + 新旧判据等价。

被测 = 计划的**两态**：`agent/graph.py::plan_state` 一次写出 `plan`（人读的契约文本）与
`plan_obj`（程序读的字段），以及"读端直取对象"这件事本身。

**为什么存在**：批 C 之前有四处读端靠**抠文本**认计划——`server.py` 的
`plan.startswith("SKILL=")`（这是不是一份真计划）、`startswith("SKILL=chat")`（闲聊轮）、
`"\nTOOLS: " in plan and "TOOLS: （无）" not in plan`（有没有执行清单），外加
`_plan_skill` 自己写的一条 `SKILL=` 正则（与 `parse_plan` 里那条是两份拷贝）。四处都是
"改 `plan_encode` 的排版就得同步改读端"的人工约定（全仓 94 处同族约定里的一处，见
`docs/` 那次架构审计）。现在读端直取 `plan_obj`，这份文件把新形状钉住：

  ① **写端唯一入口**（源码锁）：`plan_encode` 的**代码**调用点恰好一处、且在 `plan_state`
     体内；`agent/graph.py` 里再没有第二处手写 `"plan": …`（只有 `graph_input` 的初值 `""`）。
     —— 想绕开 `plan_state` 自己拼文本，得先删掉这条锁。
  ② **两态不分叉**：`parse_plan(plan_state(obj)["plan"])` 与 `obj` 在
     skill/tools/params/note/reply/status 六项上逐项相等（两处**已知不同形**在下面显式归一）。
     文本仍是四个节点的读端（`route_after_planner` / `execute_node` / `gate_node` /
     `_wrote_this_round` 都用 `parse_plan`）——这条保证"改了排版，文本那一态的语义没变"。
     顺带钉住 **STATUS 的兼容派生在真计划上恒不触发**（缺 STATUS 才会走到按 NOTE 措辞
     反推 nav_offline/nav_unresolved/target_unreachable 那段）——它只是给旧文本与手写夹具
     留的路，见 `parse_plan` 里那段注。
  ③ **判据等价**：新读法（看 `skill == "chat"` / `tools` 非空）与旧读法（在文本上
     startswith / 子串）在**技能注册表全集**上判定相同；一处刻意的偏离单独钉住：
     技能名 `chat` 前缀的唯一性。
  ④ **分隔符不进值**（20260929）：TOOLS 行是 `"; ".join(specs)` 拼的，`parse_plan` 用
     `split(";")` 读回 ⇒ 参数值里一个裸 `;` 就把**一条调用撕成 N 条**（`_tool_args` 的
     贪婪正则兜得住值里的 `(`/`)`，兜不住 `;`）。写端现在把值里的 `;` 转义成 JSON
     的 `\u003b`（读回仍是 `;`，语义一个字节不变）。④ 锁的就是这条不变量：
     TOOLS 行的裸 `;` 只许是分隔符。

**不测什么**：不测 `plan_encode` 的**排版**（`test_skills.py` 有往返用例）、不测
planner/gate 行为（各有专测）。这里只锁"通道的形状"。

跑法：`.venv/bin/python tests/run_all.py -k plan_channel`
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import graph as G        # noqa: E402
from agent import skills as S       # noqa: E402
import server as SRV                # noqa: E402  （③ 读它的新读法：`_spec_one`/`_specs_from_tools`）

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── ① 写端唯一入口（源码锁）─────────────────────────────────────────────────
# 锁的是"代码里在哪调用了谁"：文档字符串与注释里提到 `plan_encode` 不算（`plan_state` 的
# 头注就在讲它），所以先把三引号段整段抹成空行、再排除纯注释行。
_GRAPH_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
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


print("① 写端唯一入口（源码锁）")
_enc = _code_hits(r"\bplan_encode\s*\(")
_ps_lo, _ps_hi = _span_of("plan_state")
_calls = [(i, l) for i, l in _enc if not l.startswith("def plan_encode")]
check("plan_encode 的代码调用点恰好一处", len(_calls) == 1, str(_calls))
check(f"那一处落在 plan_state 体内（行 {_ps_lo}–{_ps_hi}）",
      bool(_calls) and _ps_lo <= _calls[0][0] <= _ps_hi, str(_calls[:1]))

_plan_keys = _code_hits(r'"plan"\s*:')
check("graph.py 里手写 \"plan\": 只剩两处（plan_state 的返回 + graph_input 初值）",
      len(_plan_keys) == 2, str([h[1][:60] for h in _plan_keys]))
check("  其一在 plan_state 体内（与 plan_encode 同一行）",
      any(_ps_lo <= i <= _ps_hi and "plan_encode" in l for i, l in _plan_keys),
      str([i for i, _ in _plan_keys]))
check("  另一处是 graph_input 的初值，且同一行给了 plan_obj 的初值",
      any('"plan": ""' in l and "plan_obj" in l for _, l in _plan_keys),
      str([l[:70] for _, l in _plan_keys if '"plan": ""' in l]))


# ── ② 两态不分叉 ───────────────────────────────────────────────────────────
# 两处**已知不同形**（改造前就是如此，不是批 C 引入的）：显式归一后再逐项比。
#   · params：对象里可能是 None，文本里恒是 `{}`（`plan_encode` 用 `json.dumps(… or {})`）
#   · note  ：对象的空值渲染成文本的 `（无）`，解析回来是那三个字
_NORM = {
    "params": lambda v: v or {},
    "note": lambda v: "" if v in (None, "", "（无）") else v,
    "reply": lambda v: v or "",
    "tools": lambda v: v or [],
    "skill": lambda v: v or "",
    "status": lambda v: v or "",
}


def _real_plans() -> list[tuple[str, dict]]:
    """真计划的取样：注册表全集（空参）+ navigate 的四条出口 + 一条手写对象。"""
    out = [(f"{n}(空参)", S.instantiate_plan(n, {})) for n in S.SKILL_MAP]
    for target in ("物联网平台", "友链", "/iot", "量子对撞机车间"):
        out.append((f"navigate({target})", S.instantiate_plan("navigate", {"target": target})))
    return out


print("② 两态不分叉（对象 ↔ 文本）")
_cases = _real_plans()
check("取样非空（注册表全集 + navigate 四出口）", len(_cases) >= len(S.SKILL_MAP),
      f"{len(_cases)} 例")
_div, _derived = [], []
for label, obj in _cases:
    st = G.plan_state(obj)
    if st["plan_obj"] is not obj:
        _div.append(f"{label}:plan_obj 不是同一份对象")
        continue
    parsed = G.parse_plan(st["plan"])
    for k, norm in _NORM.items():
        if norm(obj.get(k)) != norm(parsed.get(k)):
            _div.append(f"{label}:{k} 对象={norm(obj.get(k))!r} 文本={norm(parsed.get(k))!r}")
    # STATUS 兼容派生在当前真计划上恒不触发：对象没给 status 时，文本也不该"读出"一个
    if not (obj.get("status") or "") and (parsed.get("status") or ""):
        _derived.append(f"{label}:文本派生出 {parsed['status']!r}")
check("六项逐项相等（skill/tools/params/note/reply/status，两处不同形已归一）",
      not _div, "；".join(_div[:5]))
check("STATUS 兼容派生在真计划上恒不触发（NOTE 措辞不再决定 status）",
      not _derived, "；".join(_derived[:5]))
def _keyerror(fn) -> bool:
    try:
        fn()
    except KeyError:
        return True
    return False


check("空对象进 plan_encode 会 KeyError（读端因此用 `if pobj:` 认「本轮没有计划」）",
      _keyerror(lambda: G.plan_encode({})))


# ── ③ 判据等价（新旧读法在同一份计划上判定相同）──────────────────────────────
def _old_verdict(plan_text: str) -> tuple:
    """批 C 之前 `server.py` 的三段文本嗅探（逐字照抄，作对照臂）。"""
    return (plan_text.startswith("SKILL="),
            plan_text.startswith("SKILL=chat"),
            "\nTOOLS: " in plan_text and "TOOLS: （无）" not in plan_text)


def _new_verdict(pobj: dict) -> tuple:
    """现在的读法（`server.py` 现状：真计划 / 闲聊轮 / 有执行清单）。"""
    return (bool(pobj),
            str(pobj.get("skill") or "") == "chat",
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


print("③ 判据等价（新旧读法）")
_bad = []
for n in S.SKILL_MAP:
    for obj in ({"skill": n, "tools": [], "params": {}, "note": "", "reply": "r"},
                {"skill": n, "tools": [f'list_tags({{"n": "{n}"}})'], "params": {},
                 "note": "", "reply": "r"}):
        st = G.plan_state(obj)
        if _old_verdict(st["plan"]) != _new_verdict(obj):
            _bad.append(f"{n}:{obj['tools']}")
check("注册表全集 × {零工具, 一件工具}：新旧判定逐项相同", not _bad, "；".join(_bad[:5]))
check("空计划（没有 plan_obj / 空文本）两边都判「不是真计划」",
      _old_verdict("") == _new_verdict({}) == (False, False, False))
# 离意偏离 1：新读法用 `== "chat"`，旧读法用的是 startswith —— 只有当出现以 chat 开头的
# 另一个技能名时两者才分岔。这条断言就是那个前提：真分岔了，先看这里再改判据。
_dup = [n for n in S.SKILL_MAP if n != "chat" and n.startswith("chat")]
check("技能名里以 chat 开头的只有 chat 自己（新读法 `== \"chat\"` 与旧 startswith 等价的唯一前提）",
      not _dup, str(_dup))
# 刻意偏离：spec 的 JSON 字符串里含 `;`（标题/正文带分号是常态）。三段嗅探的判定不受影响
# ——差的只是动作预览列表。撕成两条那件事已由写端堵上（见 ④），这里只留判定一致。
_spec = 'tag_delete({"name": "a;b"})'
_st = G.plan_state({"skill": "tag_delete", "tools": [_spec], "params": {},
                    "note": "", "reply": "r"})
check("值里含 `;` 时三段嗅探的判定两边仍然一致（差的是预览列表，不是闸）",
      _old_verdict(_st["plan"]) == _new_verdict(_st["plan_obj"]))
check("  新读法（直取对象）照旧一条、参数完整",
      SRV._specs_from_tools([_spec]) == [("tag_delete", {"name": "a;b"})],
      str(SRV._specs_from_tools([_spec])))


# ── ④ 分隔符不进值（20260929，生产缺陷）──────────────────────────────────────
# TOOLS 行 = `"; ".join(specs)`，读端 = `split(";")` ⇒ **参数值里的裸 `;` 是一条调用的
# 断点**。写端现在把值里的 `;` 转义成 JSON 的 `\u003b`（读回仍是 `;`，语义一个字节不
# 变）。④ 锁的是不变量本身：**TOOLS 行的裸分号只许是分隔符**——漏转义（值里的分号当分隔
# 符 ⇒ 条数变多）与转多（连分隔符一起转 ⇒ 条数变少）**两个方向都会被下面这条红**。
print("④ 分隔符不进值：TOOLS 行的裸 `;` 只许是分隔符，不许长在参数值里")


def _spec_with(name: str, key: str, value) -> str:
    """按 `instantiate_plan` 的落盘形状造一条 spec（参数走 json.dumps）。"""
    return f"{name}({json.dumps({key: value}, ensure_ascii=False)})"


def _tools_line(txt: str) -> str:
    return next(l for l in txt.splitlines() if l.startswith("TOOLS"))


def _plan_with(tools: list[str]) -> str:
    return G.plan_state({"skill": "tag_delete", "tools": tools, "params": {},
                         "note": "", "reply": "r"})["plan"]


check("_esc_spec 只动 ASCII 分号：全角 `；`、`(`/`)`/`,`/反斜杠一律原样",
      G._esc_spec("；") == "；" and G._esc_spec("a(b),c\\d") == "a(b),c\\d")
check("  且幂等（转义产物里没有裸分号，再来一次不再变）",
      G._esc_spec(G._esc_spec("a;b")) == G._esc_spec("a;b") == "a\\u003bb")

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

# 邻接的隐患：`parse_plan` 的 TOOLS 正则是 `(.+)`（**不带 DOTALL**）⇒ 值里的换行若原样落
# 进 TOOLS 行，那一行会被截断在换行处。挡住它的是 `json.dumps`（换行转义成 `\n`），
# 不是 `_esc_spec`——所以这条与上面那条分开钉，别把功劳记错人。
_txt = _plan_with([_spec_with("tag_delete", "name", "上\n下")])
_got = G.parse_plan(_txt)["tools"]
_val = G._tool_args(_got[0])[0].get("name") if len(_got) == 1 else None
check("值里含换行时 TOOLS 行仍是一行（json.dumps 转义），读回来逐字节含那个换行",
      len(_got) == 1 and _val == "上\n下", f"条数={len(_got)} 读回={_val!r}")

# 正向控制：PARAMS 行不参与任何 `;` 切分，转义器也不碰它（改坏了这条会红）。
_st2 = G.plan_state({"skill": "navigate", "tools": [], "params": {"target": "a;b;c"},
                     "note": "", "reply": "r"})
check("PARAMS 行不受影响（分号原样进去、原样读回）",
      G.parse_plan(_st2["plan"])["params"] == {"target": "a;b;c"},
      str(G.parse_plan(_st2["plan"])["params"]))

# 生产复现（20260929 01:11 那份 trace 的形状）：模型给 `device_oled_draw` 的 ops 写了一段
# 用 `;` 分隔的伪指令（13 段、12 个分号）。当时 `plan_encode` 照原样拼进 TOOLS 行 ⇒ 一条
# 调用裂成 13 条 spec：碎片被当成未知工具（`tri`/`circle`/`line`/`text`…）逐个拒掉，剩下的
# 那条截断在第一个分号上（`args_parse`）⇒ **屏幕上真画了、台账却没记一笔**。
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
_txt = G.plan_state(S.instantiate_plan("device_draw", {"ops": _PROD_SHAPE}))["plan"]
_specs = G.parse_plan(_txt)["tools"]
_args, _ok = G._tool_args(_specs[0]) if len(_specs) == 1 else ({}, False)
check("生产复现：走真技能（instantiate_plan → plan_state → parse_plan）恰好一条 spec",
      len(_specs) == 1, f"{len(_specs)} 条：{[s[:22] for s in _specs[:4]]}")
check("  名是 device_oled_draw、ops 逐字节等于原文（转义读回还原成分号）",
      bool(_specs) and _specs[0].startswith("device_oled_draw(") and _ok
      and _args.get("ops") == _PROD_SHAPE,
      f"ok={_ok} 读回 {len(_args.get('ops') or '')} 字 / 原文 {len(_PROD_SHAPE)} 字")
check("  且 TOOLS 行 0 个裸分号（只有一条 spec，连分隔符都不该有）",
      _tools_line(_txt).count(";") == 0, str(_tools_line(_txt).count(";")))

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：" + "、".join(FAILS))
    sys.exit(1)
print("✅ 计划双态通道全部通过")
