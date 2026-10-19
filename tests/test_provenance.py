# -*- coding: utf-8 -*-
"""写参数**出处**的引文核对通道（20261014 第一批，纯函数、零网络、零 LLM、秒级）。

被测 = `agent/provenance.py`（判据本体）+ `agent/textnorm.py`（两把尺子）+
`agent/planner_llm.py` 的 schema 注入/摘取 + `agent/graph.py` 的影子接线。

**这一批换的是什么**：写通道上「这个值有没有出处」从**约 40 张手抄中文词表 + 位置窗口**
（「主人这句话里有没有我认识的那个动词」）换成**模型给逐字引文、系统只做逐字核对**
（「这个值的原文出处在哪里」）。失效形状从**假拒**（每遇一个新词形复现一次）变成
**引文缺失**（可计数、可巡检、可观测）。理由与两条生产现场见 `agent/provenance.py` 头注。

覆盖九块：
  ① 判据一逐字核对：值 ⊆ 引文 ⊆ 来源，来源分档（user > ledger > exec）；
  ② 三种失败各有一种**机器码**（no_quote / value_not_in_quote / quote_not_in_source）；
  ③ 唯一保留的结构 = **主人自己打的引号**，且它的方向是**退回弹卡、不是拒绝**
     （这一条是本次改造最容易改反的地方，单独一节钉住）；
  ④ 容错：模型那一格是**第一次见**的自由容器，四形状都收；空值与 `$ref` 不判；
  ⑤ 归一：全角/大小写/空白（键盘噪声）——**只归一「比不比得上」，绝不改值**；
  ⑥ 强制域的**派生**：射程 = authz 的写集 − 一律弹窗族，零手抄名单（四条历史豁免
     自动收在里面）；
  ⑦ schema 面：`evidence` 进 `props` **不进** `required`、射程外的技能没有它、
     摘走之后**不进** `params`（这是「关了档逐字节不变」的判据形状）；
  ⑧ AST 级：`agent/provenance.py` 里**不许再长出中文词表**（本次改造的病根）；
  ⑨ 影子通道：today 与旧闸**同源**（跑的是它自己那两个函数）、关档不落事件、
     开启时落**结构化**字段——分歧报告全靠这一格。

**这一批不改任何行为**（影子）：所以本文件**不**断言「拒绝姿势」（refusal_source=evidence）
——那是第二批的事，见 `docs/adr/` 那条与 `agent/provenance.py` 头注的「上线形状」。
这里只钉「判据本身对不对」与「影子接线通不通」。
（**20261015 补**：那句话术里的**死路措辞**已经先单独修掉了——「把那个名字原样再说一次」
不再出现，追问只给"加引号"这条真的走得通的路，它的断言住在
`tests/test_target_grounding.py` ③；第二批剩下的是**拒绝姿势**本身，仍然挂起。）

用法：.venv/bin/python tests/test_provenance.py
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

from agent import authz  # noqa: E402
from agent import provenance as P  # noqa: E402
from agent import textnorm as T  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _plan(tool: str, args: dict, workflow: str = "chat") -> dict:
    """一条 spec 的计划对象（判据只读 `tools` 里的参数，够用）。"""
    return {"workflow": workflow, "params": dict(args),
            "tools": [f"{tool}({json.dumps(args, ensure_ascii=False)})"],
            "note": "x", "reply": "y"}


# ── ① 判据一：值 ⊆ 引文 ⊆ 来源 ──────────────────────────────────────────
print("① 逐字核对：值 ⊆ 引文 ⊆ 来源，来源分档（user > ledger > exec）")
_msg = "把标签「大笨狗」和 Python 两个标签都删掉"
_rows = P.review([{"arg": "name", "quote": "大笨狗"}, {"arg": "title", "quote": "Python"}],
                 [("name", "大笨狗"), ("title", "Python")], _msg)
check("逐参数配对的两格都判有出处", all(r.ok for r in _rows) and len(_rows) == 2, str(_rows))
check("来源记 user（本轮主人原话）", all(r.kind == P.KIND_USER for r in _rows), str(_rows))
check("`review` 回的是 Ruling 而不是布尔 —— 拒绝时要印得出来源",
      all(isinstance(r, P.Ruling) and isinstance(r.as_trace(), dict) for r in _rows))

_r = P.review([{"arg": "name", "quote": "demo"}], [("name", "demo")], "嗯，要",
              ledger_src="要我把账号「demo」的身份改成普通用户吗")[0]
check("只在上一张卡的台账行里出现 ⇒ kind=ledger（不是 user）",
      _r.ok and _r.kind == P.KIND_LEDGER, str(_r))
_r2 = P.review([{"arg": "name", "quote": "demo"}], [("name", "demo")], "把 demo 处理一下",
               ledger_src="要我把账号「demo」的身份改成普通用户吗")[0]
check("两处都找得到 ⇒ 记 user（弹卡要印主人那一份原文）",
      _r2.kind == P.KIND_USER, str(_r2))
_r3 = P.review([{"arg": "name", "quote": "demo"}], [("name", "demo")], "嗯，要",
               exec_src="给「demo」发过一条通知")[0]
check("只在执行记忆里 ⇒ kind=exec", _r3.ok and _r3.kind == P.KIND_EXEC, str(_r3))

# ── ② 三种失败各有机器码 ────────────────────────────────────────────────
print("② 三种失败各有一种**机器码**（判据读 why，不读文案）")
_w = {}
for tag, ev, wanted in (
        ("no_quote", [], [("name", "大笨狗")]),
        ("value_not_in_quote", [{"arg": "name", "quote": "把标签删掉"}], [("name", "大笨狗")]),
        ("quote_not_in_source", [{"arg": "name", "quote": "大笨狗在别处"}], [("name", "大笨狗")])):
    _w[tag] = P.review(ev, wanted, _msg)[0]
check("没给引文 ⇒ no_quote", _w["no_quote"].why == "no_quote" and not _w["no_quote"].ok)
check("引文里没有这个值 ⇒ value_not_in_quote",
      _w["value_not_in_quote"].why == "value_not_in_quote")
check("引文本身不在任何来源里 ⇒ quote_not_in_source（模型编了一段话）",
      _w["quote_not_in_source"].why == "quote_not_in_source")
check("三个机器码全是 ASCII token（是判据用的，不是给主人看的话）",
      all(re.fullmatch(r"[a-z_]+", r.why) for r in _w.values()),
      str([r.why for r in _w.values()]))
_oracle = P.review([{"arg": "name", "quote": "「大笨狗」"}], [("name", "大笨狗")], _msg)[0]
check("引文可以带主人的引号外壳（归一后仍是子串）——不因为抄了标点就判它没出处",
      _oracle.ok, str(_oracle))

# ── ③ 唯一保留的结构：引号（方向是「退回弹卡」，不是「拒绝」）──────────────
print("③ 唯一保留的结构 = 主人自己打的引号；它的**方向**是退回弹卡、不是拒绝")
_b = P.review([{"arg": "title", "quote": "Python"}], [("title", "Python")], _msg)[0]
check("值落在引号**外面** ⇒ bounded=False（主人标了界碑的那些字）",
      _b.bounded is False, str(_b))
check("★ 但它**仍然 ok=True**：判据一不因这条拒绝人（「和 Python 两个标签都删掉」"
      "是主人的原话，拒了就是新的假拒）——这条只能让弹卡**必须弹**",
      _b.ok and _b.kind == P.KIND_USER, str(_b))
_nb = P.review([{"arg": "name", "quote": "大笨狗"}], [("name", "大笨狗")], "把标签 大笨狗 删掉")[0]
check("消息里一段引号都没有 ⇒ bounded 自然成立（这条不是新门槛）", _nb.bounded is True)
check("引号段读的是**标点**：四族成对引号都认，半角单引号刻意不收",
      P.quote_spans("「甲」『乙』“丙”\"丁\" 半角'戊'") == ["甲", "乙", "丙", "丁"],
      str(P.quote_spans("「甲」『乙』“丙”\"丁\" 半角'戊'")))

# ── ④ 容错：四形状 + 边界 ──────────────────────────────────────────────
print("④ 容错：模型那一格是第一次见的自由容器，四形状都收")
check("裸字符串", P.parse_evidence("大笨狗") == (("", "大笨狗"),))
check("{参数名: 引文} 映射", P.parse_evidence({"name": "大笨狗"}) == (("name", "大笨狗"),))
check("{arg,quote} 单对象", P.parse_evidence({"arg": "name", "quote": "大笨狗"})
      == (("name", "大笨狗"),))
check("数组（契约形状）", P.parse_evidence([{"arg": "a", "quote": "q"}, "w", {"b": "c"}])
      == (("a", "q"), ("", "w"), ("b", "c")))
check("空/垃圾输入**不猜**（None / 空数组 / 缺 quote 一律空元组）",
      P.parse_evidence(None) == () and P.parse_evidence([{"arg": "a"}]) == ()
      and P.parse_evidence([]) == ())
check("★ 已归一的 `((arg, quote), ...)` 再喂一次**不许静默变空**"
      "（影子与第二批核验吃的就是这个形态）",
      P.parse_evidence((("name", "大笨狗"),)) == (("name", "大笨狗"),))
check("顶层裸数组仍是「几段裸引文」，不被读成一对（否则语义整个反过来）",
      P.parse_evidence(["甲", "乙"]) == (("", "甲"), ("", "乙")))
check("通配（arg 为空）能配任何一格：语义是这段原文就是这个值的出处",
      P.review(["大笨狗"], [("name", "大笨狗")], _msg)[0].ok)
_skip = P.review([], [("note_id", ""), ("ids", "$tool[0].id")], _msg)
check("空值不判（那是缺参问题，不是没出处）", _skip == ())
check("`$tool[N].field` 是机器解析出来的引用，没有字面可核 ⇒ 不判",
      len(P.review([], [("x", "$tool[1].title")], _msg)) == 0)

# ── ⑤ 归一：键盘噪声 ───────────────────────────────────────────────────
print("⑤ 归一：全角/大小写/空白只归一**比不比得上**，绝不改值")
_gt = P.review([{"arg": "add", "quote": "把git，代码版本管理挂上去"}], [("add", "Git")],
               "把git，代码版本管理挂上去")[0]
check("★ 站内正字 Git vs 主人打的 git（大小写）⇒ 算有出处（20261008 现场）",
      _gt.ok, str(_gt))
_fw = P.review([{"arg": "add", "quote": "挂上 Ｇｉｔ"}], [("add", "Git")], "挂上 Ｇｉｔ")[0]
check("全角字母 ⇒ 算有出处", _fw.ok, str(_fw))
_sp = P.review([{"arg": "name", "quote": "标签 「大\n笨狗」"}], [("name", "大笨狗")], _msg)[0]
check("换行/空格抹平", _sp.ok, str(_sp))
check("`fold_typing` **不去空白**（它的契约是传进来的已经 squash 过）",
      T.fold_typing("a b") == "a b" and T.norm("a b") == "ab")
check("`norm` = 两把一起上（新调用方的唯一入口）", T.norm(" ＧＩＴ ") == "git")
check("值是**站内那一份**，归一不改它（`Ruling.value` 原样回）",
      _gt.value == "Git", _gt.value)

# ── ⑥ 强制域的派生 ─────────────────────────────────────────────────────
print("⑥ 强制域**派生**（零手抄名单）：射程 = authz 写集 − 一律弹窗族")
_scope = P.write_scope_tools()
_deny = P.always_confirm_tools()
check("射程里一个一律弹窗族的工具都没有（卡面印全值 ⇒ 人眼是核验方）",
      not (_scope & _deny), str(sorted(_scope & _deny)))
check("射程 ⊇ 免弹窗写族（判据一的射程与今天那道闸是同一族）",
      # `delete_tag` 20261019 起进了 `_ALWAYS_CONFIRM_TOOLS`（SAU-014：删一级
      # 标签连坐子标签、无回收站 ⇒ 卡面是核验方，不再要点名出处）⇒ 它**应该**在射程外。
      # 这一格断言的是"射程跟着闸走"，所以族别一变、期望也得跟着变——不是放宽。
      {"create_tag", "update_tag", "set_article_tags",
       "create_category", "update_category", "delete_category"} <= _scope,
      str(sorted(_scope)))
check("★ 四条历史刻意豁免自动落在射程**外**（通知正文/角色档位/禁言时长/待办正文）",
      not ({"send_user_notice", "set_account_role", "account_mute",
            "create_dashboard_todo"} & _scope))
check("射程是**算出来的**、不是第二份名单：`is_write ∧ ∉ always_confirm` 逐项复算一致",
      _scope == frozenset(t for t in authz.TOOL_SCOPE
                          if authz.is_write(t) and t not in _deny))
check("`always_confirm_tools()` 是 `authz` 那一份的**公开读口**（同名同源）",
      P.always_confirm_tools() is authz.always_confirm_tools())

# ── ⑦ schema 面 ────────────────────────────────────────────────────────
print("⑦ schema 面：进 props **不进** required；射程外的技能没有；摘走不进 params")
from agent import planner_llm as N  # noqa: E402


def _schema(role, **kw):
    return {t["function"]["name"]: t["function"] for t in N.build_tool_schema(role, **kw)}


_off, _on = _schema("admin"), _schema("admin", evidence=True)
_with = sorted(k for k in _on if "evidence" in _on[k]["parameters"]["properties"])
check("开档：射程内的技能多出 evidence 一格",
      "tag_update" in _with and "tag_create" in _with, str(_with))
check("射程外的技能**没有**它（通知/公告/留言族走弹卡，卡面就是核验方）",
      not [k for k in ("send_user_notice", "account_set_role", "board_audit",
                       "announcement_create") if k in _with])
check("★ evidence **不进 required**（不填与这一格不存在时逐字节相同）",
      all("evidence" not in (_on[k]["parameters"].get("required") or []) for k in _with))
check("关档 == 不传该形参：`evidence=False` 的 schema 逐字节等于改动前",
      json.dumps(N.build_tool_schema("admin"), ensure_ascii=False, sort_keys=True)
      == json.dumps(N.build_tool_schema("admin", evidence=False), ensure_ascii=False,
                    sort_keys=True))
check("开档不改变射程外那批函数的任何一格（只多不少，不动别人的形状）",
      all(json.dumps(_on[k], ensure_ascii=False, sort_keys=True)
          == json.dumps(_off[k], ensure_ascii=False, sort_keys=True)
          for k in _off if k not in _with))

from langchain_core.messages import AIMessage  # noqa: E402

_msg_resp = AIMessage(content="", tool_calls=[{
    "name": "tag_delete", "id": "c1", "type": "tool_call",
    "args": {"name": "大笨狗", "evidence": [{"arg": "name", "quote": "标签「大笨狗」"}]}}])
_dec = N.tool_calls_to_plan(_msg_resp, "admin")
check("★ 摘走：evidence **不进 params**（漏进去会走 PARAMS= 计划文本、还会被 "
      "check_workflow_params 记成未知参数，把漂移探针淹掉）",
      _dec is not None and "evidence" not in (_dec.params or {}), str(_dec and _dec.params))
check("摘出来的引文进 `PlannerDecision.evidence`（归一成 (arg, quote) 对）",
      _dec.evidence == (("name", "标签「大笨狗」"),), str(_dec.evidence))
check("技能参数原样留着（摘的是那一格，不是这个技能的参数）",
      (_dec.params or {}).get("name") == "大笨狗")
_dec2 = N.tool_calls_to_plan(AIMessage(content="", tool_calls=[{
    "name": "chat", "id": "c2", "type": "tool_call", "args": {"evidence": "x"}}]), "admin")
check("★ **无条件**摘（档位只管模型发不发得出，摘永远是安全的）："
      "模型照旧报出这一格时也不许漏进 params",
      _dec2 is not None and "evidence" not in (_dec2.params or {}), str(_dec2 and _dec2.params))

# ── ⑧ AST 级：不许再长出中文词表 ────────────────────────────────────────
print("⑧ AST 级：`agent/provenance.py` 里**不许再长出中文词表**（本次改造的病根）")
_src = (ROOT / "agent" / "provenance.py").read_text(encoding="utf-8")
_tree = ast.parse(_src)
_CJK = re.compile(r"[一-鿿]")


def _cjk_collections(tree):
    """模块里**集合型字面量**含中文的（= 词表形状）—— 一行一条 [(行号, 类型)]。"""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            continue
        vals = [v.value for v in ast.walk(node)
                if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if any(_CJK.search(v) for v in vals):
            bad.append((node.lineno, type(node).__name__))
    return bad


_cjk_bad = _cjk_collections(_tree)
check("判据本体里没有任何中文集合字面量（词表/名单的形状）",
      not _cjk_bad, str(_cjk_bad))
_regexes = [n for n in ast.walk(_tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "compile"]
_pats = [a.value for n in _regexes for a in n.args
         if isinstance(a, ast.Constant) and isinstance(a.value, str)]
check("唯一的正则里**没有一个中文词**（只有成对引号那四族标点）",
      len(_pats) == 1 and not _CJK.search(_pats[0]), str(_pats))
check("引号正则与归一器都还在（没有被搬走）",
      hasattr(P, "QUOTE_SPAN_RE") and hasattr(T, "norm"))
_mods = sorted({(a or b) for a, b in re.findall(
    r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", _src, re.M)})
check("`provenance` 的依赖面就是那五个（不 import graph ⇒ 不成环）",
      all(m in ("__future__", "re", "dataclasses", "agent", "agent.authz",
                "agent.textnorm") for m in _mods), str(_mods))
check("★ 明写不 import graph（graph 是消费方，反向 import 会成环）",
      not re.search(r"^(?:import|from)\s+agent\.graph", _src, re.M))

# ── ⑨ 影子通道 ──────────────────────────────────────────────────────────
print("⑨ 影子通道：today 与旧闸同源、关档不落事件、开档落**结构化**字段")
import agent.graph as g  # noqa: E402

# ⑨ 这一段打的是 `record` 的桩——**桩必须打在"那一族现在住的那个模块"上**。
# 20261019 起 `_evidence_shadow` 搬进了 `agent/write_grounding.py`，它那边是
# `from utils.trace import record` 绑到**自己的**模块全局上的；继续打
# `g.record` 会**静默失效**：桩照样装上、事件一条也不落，而判据读到的只是空列表
# （本节 ⑨ 那一整片会以"什么都没发生"的形式变红，看着像功能坏了）。
# 所以按**函数对象自己**的 `__module__` 取模块，不写死名字——下次再搬家也不会漏。
_wg = sys.modules[g._evidence_shadow.__module__]
_events: list[tuple] = []
_backup_record = _wg.record
_wg.record = lambda node, event, **kw: _events.append((node, event, kw))
_backup_dial = getattr(g.settings, "agent_evidence", None)
try:
    g.settings.agent_evidence = False
    _events.clear()
    g._evidence_shadow(SimpleNamespace(plan_obj=_plan("tag_delete", {"name": "大笨狗"}),
                                       user_msg=_msg, state={}, decided=None, rounds=1), "")
    check("关档：一条事件都不落（影子不是少算一点，是整段不跑）", not _events, str(_events))

    # 开档 + 「旧闸判否、新判据（有引文时）判是」的形状 ⇒ 分歧可见。
    #
    # 20261015 换样本（**不是放宽断言**）：这里原来用的是 175114 的现场
    # 「猫猫给用户niuniu升级为管理员」——那正是本批补的「名词紧跟」格的例子，
    # 补完之后 `_msg_grounded_name`（today）也判 True ⇒ 旧样本上**两个判据不再分歧**，
    # 这条断言的**意思**（"分歧看得见"）就没东西可量了。换成一个仍然分歧的真实语序：
    # `给用户小猫升级为管理员`（中文名，N 格**刻意**不认——要认得回答"哪一个字算名字"，
    # 那是又一张中文词表）⇒ today=False（旧闸取不出），ok=True/kind=user（引文对得上）。
    g.settings.agent_evidence = True
    _events.clear()
    _p = _plan("set_account_role", {"name": "小猫", "role": "admin"},
               workflow="account_set_role")
    _ctx = SimpleNamespace(plan_obj=_p, user_msg="猫猫给用户小猫升级为管理员", state={},
                           decided=SimpleNamespace(evidence=(
                               ("name", "猫猫给用户小猫升级为管理员"),)),
                           rounds=0)
    _fields, _widen = g._evidence_fields(_p, _ctx.user_msg)
    check("`_evidence_fields` 收的是今天那道闸判的同一批格子（目标名段在里面）",
          [k for _t, k, _v, _d in _fields] == ["name"], str(_fields))
    check("★ today 与旧闸**同源**：中文名那个值在旧判据下判否（这正是要换掉的那一问）",
          _fields[0][3] is False, str(_fields))
    _lex = g._lexicon("set_account_role")
    check("（复算：today 就是 `_msg_grounded_name` 自己的结论，不是另写的一问）",
          bool(_fields[0][3]) == bool(g._msg_grounded_name(
              "小猫", _ctx.user_msg, g._msg_quote_spans(_ctx.user_msg), _lex)))
    g._evidence_shadow(_ctx, "")
    _sh = [kw for _n, e, kw in _events if e == "evidence_shadow"]
    check("开档：没出处的格落一条 `planner.evidence_shadow`", len(_sh) == 1, str(_events))
    check("★ 落的是**结构化**字段（tool/arg/value/quote/kind/ok/bounded/why/today），"
          "不是一段散文 —— R4 巡检与分歧报告全靠它",
          bool(_sh) and {"tool", "round", "arg", "value", "quote", "kind",
                         "ok", "bounded", "why", "today"} <= set(_sh[0]), str(_sh))
    check("分歧可见：**旧判据说没出处（today=False）、新判据说有**（ok=True, kind=user）"
          "—— 这一格就是本次改造要消掉的那类假拒",
          bool(_sh) and _sh[0]["today"] is False and _sh[0]["ok"] is True
          and _sh[0]["kind"] == P.KIND_USER, str(_sh))
    check("★ 换样本的正控：N 格认得的那个语序（`给用户niuniu升级为管理员`）在旧闸下"
          "**已经判是** ⇒ 它不再是分歧样本（拿它量「分歧可见」会静默变空）",
          bool(g._msg_grounded_name("niuniu", "猫猫给用户niuniu升级为管理员",
                                    g._msg_quote_spans("猫猫给用户niuniu升级为管理员"),
                                    g._lexicon("set_account_role"))))
    check("tool 名归位（不是空串）", bool(_sh) and _sh[0]["tool"] == "set_account_role",
          str(_sh))

    _events.clear()
    g._evidence_shadow(SimpleNamespace(
        plan_obj=_plan("set_article_status", {"article_id": 12, "status": "public"}),
        user_msg="把那篇文章置顶", state={}, decided=SimpleNamespace(evidence=()),
        rounds=0), "")
    _sc = [kw for _n, e, kw in _events if e == "evidence_scope"]
    check("射程候选的写格只记 `planner.evidence_scope`、**不落** evidence_shadow"
          "（第一批不许净收紧）",
          len(_sc) == 1 and not [e for _n, e, _k in _events if e == "evidence_shadow"],
          str(_events))
    check("候选里带着工具名（逐格定射程时要看得见是哪一件）",
          "set_article_status" in str(_sc), str(_sc))

    # ★ 数组值那一格（`add`/`titles`）：旧闸自己**逐项**判（`_name_arg_fix` 的
    # `bad = [v for v in vals if not _grounded_value(v, ...)]`），影子必须逐项——
    # 整份数组喂 `_grounded_value` 恒 False（实测现场：
    # `admin_two_tags_case_variant_card`「把 git 和代码版本管理两个标签加到文章 16」），
    # 分歧报告会因此整族失真：把"旧判据说没出处"记成噪音，看的人会以为那一族全是假拒。
    _events.clear()
    _m2 = "把 git 和代码版本管理两个标签加到文章 16"
    _p2 = _plan("set_article_tags", {"article_id": 16, "add": ["git", "代码版本管理"]},
                workflow="article_tags")
    _f2, _ = g._evidence_fields(_p2, _m2)
    check("数组值**逐项**进影子（不是把整份数组当一格）",
          [(k, v, d) for _t, k, v, d in _f2] == [("add", "git", True),
                                                 ("add", "代码版本管理", True)]
          , str(_f2))
    g._evidence_shadow(SimpleNamespace(plan_obj=_p2, user_msg=_m2, state={},
                                       decided=SimpleNamespace(evidence=(("add", _m2),)),
                                       rounds=0), "")
    _sh2 = [kw for _n, e, kw in _events if e == "evidence_shadow"]
    check("★ 同一格里两个值时 today **各配各的**（按参数名取值会把 a 的判决串到 b 上）",
          len(_sh2) == 2 and all(x["today"] is True for x in _sh2), str(_sh2))
finally:
    _wg.record = _backup_record
    if _backup_dial is not None:
        g.settings.agent_evidence = _backup_dial

check("影子**不许**弄坏一轮对话：核验内部抛异常时吞掉并只记一条 warning",
      "except Exception" in inspect.getsource(g._evidence_shadow))

print("⑨b 拒绝事件与产出物：被拒的那个**字面**进结构化字段（判据读字段不读措辞）")
# 取值点跟着代码搬（20261019）：这一族住进了 `agent/write_grounding.py`，
# 判据字面一个字没动，只把"读 graph.py 全文"换成"读这一族的全部文件"。
_gsrc = _ctx_src.write_guard_src()
check("`write_target_unresolved` 事件带上 values=（与 `write_value_unresolved` 同形）",
      "values=list(dict.fromkeys(refused_values))[:3]" in _gsrc)
check("来源限定在**出处族**（台账编号/政策两族不混进来 ⇒ R4 不造假阳性）",
      re.search(r'refusal_source in \("quote", "value", "todo_text",\s*\n\s*'
                r'"grounding"\)', _gsrc) is not None)
check("影子跑在**校正之前**（两边判的是模型自己填的那个值，不是校正后的产物）",
      _gsrc.index("_evidence_shadow(c, ledger_src)")
      < _gsrc.index("_board_quote_fix(c.plan_obj"))

print("⑨c 档位与巡检：开关只有一个读口；R4 认得这一族事件")
check("`_evidence_on()` 是唯一读口（多一处读 settings 就多一处说法）",
      _gsrc.count('getattr(settings, "agent_evidence"') == 1, "见 graph.py")
_ta = (ROOT / "eval" / "trace_alert.py").read_text(encoding="utf-8")
check("R4 的来源族与 graph 侧落 values= 的那一族同源",
      'REFUSAL_SOURCES = ("quote", "value", "todo_text", "grounding")' in _ta)
check("R4 的候选判据读事件里的结构化字段，不读 reason 措辞",
      'ev.get("values")' in _ta and "REFUSAL_SOURCES" in _ta)
check("旧 trace 没有 values= 时**单独计数**（「读不到」与「没有误拒」不许混成一个 0）",
      "r4_readable" in _ta and "r4_seen" in _ta)
_ra = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
check("出厂档进了 `_PINNED`（否则谁 export 过就按另一档判）",
      '"AGENT_EVIDENCE": "0"' in _ra)

print("\n" + ("全部通过" if not FAILED else f"失败 {len(FAILED)} 项：" + "; ".join(FAILED)))
raise SystemExit(1 if FAILED else 0)
