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
     startswith / 子串）在**技能注册表全集**上判定相同；两处刻意的偏离各自单独钉住：
     技能名 `chat` 前缀的唯一性、以及 spec 里的 JSON 字符串含 `;` 时旧法会把一条动作
     切成两条（文本腔的析构，见 `server._specs_from_tools` 头注）。

**不测什么**：不测 `plan_encode` 的**排版**（`test_skills.py` 有往返用例）、不测
planner/gate 行为（各有专测）。这里只锁"通道的形状"。

跑法：`.venv/bin/python tests/run_all.py -k plan_channel`
"""
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
# 刻意偏离 2：spec 的 JSON 字符串里含 `;`（标题/正文带分号是常态）。旧法按 `;` 切文本 ⇒
# 一条动作被劈成两条残片（过程行显示成「计划：tag_delete、b"})…」）；新法直取对象，
# 切分这件事根本不存在。三段嗅探的判定不受影响，差的只是动作预览列表。
_spec = 'tag_delete({"name": "a;b"})'
_st = G.plan_state({"skill": "tag_delete", "tools": [_spec], "params": {},
                    "note": "", "reply": "r"})
_old_specs, _new_specs = _old_specs_from_plan(_st["plan"]), SRV._specs_from_tools([_spec])
check("旧法把含 `;` 的一条 spec 切成两条（文本腔的析构）",
      len(_old_specs) == 2, str(_old_specs))
check("新法仍是一条，且参数完整（直取对象没有切分这回事）",
      len(_new_specs) == 1 and _new_specs[0] == ("tag_delete", {"name": "a;b"}),
      str(_new_specs))
check("  同一条上三段嗅探的判定两边仍然一致（差的是预览列表，不是闸）",
      _old_verdict(_st["plan"]) == _new_verdict(_st["plan_obj"]))

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：" + "、".join(FAILS))
    sys.exit(1)
print("✅ 计划双态通道全部通过")
