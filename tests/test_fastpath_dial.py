# -*- coding: utf-8 -*-
"""快道定价档（`AGENT_FASTPATH`）的回归锁：五条快道**逐条**受这一档管，档位本身可读。

这一档不是给人用的开关，是**定价装置**（快道命中即拿走 planner 的决策权，删/收窄
哪一条要看读数）。所以这个文件要守的不是"功能对不对"，而是**两臂读数读得出来**：

① **拨盘没接上不会报错，只会让读数失去意义**。假如关档那一臂其实还在走快道，
   两臂的读数会一模一样，读的人只能得出"快道不承重"这个错结论。所以每一臂都断言
   **决策权在谁手里**：开档 = 零 LLM 且计划就是快道那一张；关档 = 恰好一次 LLM
   且计划是脚本给的那张。只断言"有没有 `fastpath` 事件"是不够的——那只证明事件没落。
② **"没有 `fastpath` 事件"本身证明不了任何事**（本仓纪要：trace 指纹只能证"在跑"、
   不能证"没跑"）。所以关档这一件事要**显式**落在 trace 里（`planner.context` 的
   `fastpath` 格），本文件也钉住那一格——否则"开档但没命中"与"根本没开"长得一样。
③ 关档不是"少一条快路"而是"换一个人做决定"，所以关档那一臂必须**仍能走完**：
   落回 planner LLM（`len(prompts) == 1`），不是崩、也不是零计划。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（套件统一住在 tests/）
sys.path.insert(0, str(ROOT))

from _planner_stub import bind_tools_stub, toolcall_reply  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.graph import parse_plan, planner_node  # noqa: E402
from agent.principal import Principal  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _cfg():
    return {"configurable": {"principal": Principal(uid=721, role="admin"),
                             "user_id": 721, "conversation_id": 42,
                             "stop_event": None}}


class _ScriptedLLM:
    """按顺序吐回复、留 prompt；夹具文本走 native 桩翻成 tool_calls（见 `_planner_stub`）。"""

    bind_tools = bind_tools_stub

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return toolcall_reply(self.replies.pop(0))


class _Dial:
    """把 `AGENT_FASTPATH` 拨到指定档位（退出时**必须**还原）。

    离线套件默认钉出厂档（`tests/run_all.py::_PINNED` 里 `AGENT_FASTPATH=1`），临时拨
    只许在这一段里——泄漏给后面的用例会让它们按另一档判，且**红得毫无理由**。
    """

    def __init__(self, on: bool):
        self.on = on

    def __enter__(self):
        self.old = G.settings.agent_fastpath
        G.settings.agent_fastpath = self.on
        return self

    def __exit__(self, *exc):
        G.settings.agent_fastpath = self.old
        return False


def _sys(ctx: str) -> HumanMessage:
    """`_build_messages` 写进消息流的那条 `[System: …]`（`_page_ctx` 的取值处）。"""
    return HumanMessage(content=f"[System: {ctx}]")


_PREV_WITH_LINK = ("给你找到啦：[文章向量空间图谱项目文档]"
                   "(https://saudade.site/article/19) 就是这篇～")

_CHAT_PLAN = "SKILL=chat\nPARAMS={}\nREPLY: 这个交给模型决定就好"

# 五个形状 × 一条一定能命中该快道的输入（页面上下文跟在后面的那条 System 消息里）。
# `kind` = `record("planner", "fastpath", kind=…)` 那一格的名字；`tools` = 快道计划的
# 工具名（只取"名字"这一段：参数是另一族判据的事，本文件只关心**谁做的决定**）。
# 特效切换那条是**两条** spec（关旧 + 开新）——双 spec 正是这条快道存在的理由
# （planner LLM 反复只解出半边），所以它对着的位置写两个名字。
#
# `off_calls` = 关档那一臂脚本要备几份"模型自己的决定"。**特效那条要两份**：
# 「把樱花换成下雨」带着写形态的动作词（换成）而脚本第一次排的是零写规格的 chat，
# 系统按既有纠偏规则再问一次（那条纠偏与快道档位无关，是 planner 自己的纪律）。
_CASES = [
    {"kind": "nav", "off_calls": 1,
     "msgs": [HumanMessage(content="带我去留言板")], "tools": ["navigate_to"]},
    {"kind": "referent_nav", "off_calls": 1,
     "msgs": [AIMessage(content=_PREV_WITH_LINK), HumanMessage(content="带我过去")],
     "tools": ["navigate_to"]},
    {"kind": "display", "off_calls": 1,
     "msgs": [HumanMessage(content="在屏幕上显示晚安")], "tools": ["device_oled_display"]},
    {"kind": "article_read", "off_calls": 1,
     "msgs": [_sys("user_id=1, page=https://saudade.site/article/12, "
                   "title=某篇文章, current_effects=none, current_darkmode=off"),
              HumanMessage(content="我正在读这篇")], "tools": ["get_article_detail"]},
    {"kind": "effect_switch", "off_calls": 2,
     "msgs": [_sys("user_id=1, page=https://saudade.site/, title=Saudade Blog, "
                   "current_effects=sakura, current_darkmode=off"),
              HumanMessage(content="把樱花换成下雨")],
     "tools": ["toggle_effect", "toggle_effect"]},
]


def _tool_names(plan: dict) -> list[str]:
    return [t.split("(")[0] for t in plan.get("tools") or []]


def test_on_arm_each_kind():
    """① 开档（出厂档）：五条快道逐条命中、零 LLM、计划就是快道那一张。"""
    print("[fastpath_dial] 开档：五条快道逐条命中（零 LLM）")
    _orig_llm, _orig_record = G.get_llm, G.record
    events: list[tuple] = []
    try:
        G.record = lambda *a, **k: events.append((a, k))       # noqa: ARG005
        for case in _CASES:
            kind, tools = case["kind"], case["tools"]
            events.clear()
            llm = _ScriptedLLM([])      # 一个都不该被消费（消费了会 IndexError）
            G.get_llm = lambda **kw: llm                            # noqa: ARG005
            out = planner_node({"messages": list(case["msgs"]), "plan_rounds": 0},
                               _cfg())
            plan = parse_plan(out["plan"])
            check(f"{kind}：命中（计划里是 {'+'.join(tools)}）",
                  _tool_names(plan) == tools, f'{plan["skill"]} {plan["tools"]}')
            check(f"  {kind}：零 LLM（快道没把问题退回模型）", len(llm.prompts) == 0,
                  str(len(llm.prompts)))
            _kinds = [k.get("kind") for a, k in events
                      if a[:2] == ("planner", "fastpath")]
            check(f"  {kind}：trace 落 `planner.fastpath`（kind={kind}）",
                  _kinds == [kind], str(_kinds))
            _dial = [k.get("fastpath") for a, k in events
                     if a[:2] == ("planner", "context")]
            check(f"  {kind}：`planner.context` 记着档位 = on", _dial == ["on"],
                  str(_dial))
    finally:
        G.get_llm, G.record = _orig_llm, _orig_record


def test_off_arm_each_kind():
    """② 关档：同一条消息**换人做决定**——planner LLM 被问恰好一次，计划是它的。

    这是本文件的核心：判据不是"没有 fastpath 事件"（那只说明事件没落），而是
    "决策权真的交回模型了"（计划变成脚本给的那张 chat）。
    """
    print("[fastpath_dial] 关档：决策权交回 planner LLM（五条逐条）")
    _orig_llm, _orig_record = G.get_llm, G.record
    events: list[tuple] = []
    try:
        G.record = lambda *a, **k: events.append((a, k))       # noqa: ARG005
        with _Dial(False):
            for case in _CASES:
                kind, calls = case["kind"], case["off_calls"]
                events.clear()
                llm = _ScriptedLLM([_CHAT_PLAN] * calls)
                G.get_llm = lambda **kw: llm                        # noqa: ARG005
                out = planner_node({"messages": list(case["msgs"]), "plan_rounds": 0},
                                   _cfg())
                plan = parse_plan(out["plan"])
                check(f"{kind}：关档后该轮**没有**快道事件",
                      [a[:2] for a, _ in events if a[:2] == ("planner", "fastpath")] == [],
                      # 只打事件名（整条事件里带着 page_ctx，打出来要几十行）
                      str([f"{a[0]}.{a[1]}" for a, _ in events]))
                check(f"  {kind}：决策权交回模型（planner 被问 {calls} 次，"
                      "一份不多一份不少）",
                      len(llm.prompts) == calls, str(len(llm.prompts)))
                check(f"  {kind}：计划是模型给的那张（不是快道那张 "
                      f"{'+'.join(case['tools'])}）",
                      plan["skill"] == "chat" and _tool_names(plan) == [],
                      f'{plan["skill"]} {plan["tools"]}')
                _dial = [k.get("fastpath") for a, k in events
                         if a[:2] == ("planner", "context")]
                check(f"  {kind}：`planner.context` 记着档位 = off"
                      "（否则'没开'与'开了没命中'在 trace 上分不开）",
                      _dial == ["off"], str(_dial))
    finally:
        G.get_llm, G.record = _orig_llm, _orig_record


def test_dial_not_leaked():
    """③ 拨盘退出后必须还原（泄漏会让后面的用例按另一档判，且红得毫无理由）。"""
    print("[fastpath_dial] 拨盘不泄漏")
    before = G.settings.agent_fastpath
    with _Dial(False):
        check("拨下去时读口跟着变（读口真读这一格）", G._fastpath_on() is False)
    check("退出后还原成出厂档", G.settings.agent_fastpath is before
          and G._fastpath_on() is True, str(G.settings.agent_fastpath))


def test_mutation_arm():
    """④ 变异臂：把**读口**换成恒 True（档位照旧关着）⇒ 同一判据当场翻红。

    这一臂证明 ② 断的是真东西：关档那几条不是恒真的——`_fastpath_on` 一旦不读档位，
    `False` 那一档立刻退回"照走快道"，而 ② 的断言会当场抓住它。
    """
    print("[fastpath_dial] 变异臂：架空读口 ⇒ 关档那一臂当场失效")
    _orig_llm, _orig_fn = G.get_llm, G._fastpath_on
    try:
        G._fastpath_on = lambda: True        # 架空：档位形同虚设
        llm = _ScriptedLLM([])
        G.get_llm = lambda **kw: llm                         # noqa: ARG005
        with _Dial(False):
            out = planner_node(
                {"messages": [HumanMessage(content="带我去留言板")], "plan_rounds": 0},
                _cfg())
        plan = parse_plan(out["plan"])
        check("  变异后：档位关着也照样走快道（零 LLM、计划还是 navigate）",
              len(llm.prompts) == 0 and _tool_names(plan) == ["navigate_to"],
              f'{len(llm.prompts)} {plan["tools"]}')
        check("  ⇒ 所以 ② 的『决策权交回模型』不是恒真的，它真的在读这一档",
              _tool_names(plan) != [])
    finally:
        G.get_llm, G._fastpath_on = _orig_llm, _orig_fn


def test_single_reader_and_pinned():
    """⑤ 单一读口 + 运行器钉住这一档（两处都是**源码级**的性质，AST 上读）。"""
    print("[fastpath_dial] 唯一读口 + 套件运行器钉档位")
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # `AGENT_FASTPATH` 只许在一个函数里被读：`_fastpath_on`。别处再读一遍 = 多一处
    # 可能漂移的说法（IoT 那个开关上吃过的教训：三处同源不是"一个开关"）。
    # 判据读的是**程序**（AST）不是源码文本——注释里写一句"这里读 agent_fastpath"
    # 会把文本判据截断（本仓三犯过的坑），AST 只认真的取值表达式。
    hits: list[str] = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr"
                    and len(n.args) > 1 and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value == "agent_fastpath"):
                hits.append(f"{fn.name}:{n.lineno}")
    check("  档位取值在 graph.py 里只有那一处（且在那个唯一读口里）",
          len(hits) == 1 and hits[0].startswith("_fastpath_on:"), str(hits))
    check("  读口是模块级函数（供 planner 与判据共用同一个读法）",
          any(isinstance(n, ast.FunctionDef) and n.name == "_fastpath_on"
              for n in tree.body))
    # 首轮那一段必须**真的**挂在这一档上（把判定写进条件，而不是算完不用）。
    check("  首轮快道那一段的守卫里带着 `fastpath_on`",
          "if rounds == 0 and not has_frames and fastpath_on:" in src)

    runner = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
    _pinned = {}
    for n in ast.walk(ast.parse(runner)):
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_PINNED":
            _pinned = ast.literal_eval(n.value)
    check("  `tests/run_all.py::_PINNED` 钉住 `AGENT_FASTPATH=1`"
          "（离线套件对档位是瞎的：不钉的话谁 export 过它，整套就按另一档判）",
          _pinned.get("AGENT_FASTPATH") == "1", str(_pinned.get("AGENT_FASTPATH")))


def test_settings_default():
    """⑥ 出厂档 = 开（与从前逐字节相同），且 `.env.example` 里写着这一档。"""
    print("[fastpath_dial] 出厂档与样例环境")
    from config.settings import Settings
    check("  `Settings()` 的默认值是 True（关档是**取数**才拨的，不是日常档）",
          Settings.model_fields["agent_fastpath"].default is True)
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    check("  `.env.example` 里登记了这一档（没登记 = 部署者不知道它存在）",
          "AGENT_FASTPATH=1" in env)


def main() -> int:
    print("=" * 60)
    test_on_arm_each_kind()
    test_off_arm_each_kind()
    test_dial_not_leaked()
    test_mutation_arm()
    test_single_reader_and_pinned()
    test_settings_default()
    print("=" * 60)
    if FAILS:
        print(f"FAIL: {len(FAILS)} 条")
        for d in FAILS:
            print("  - " + d)
        return 1
    print("PASS: 快道定价档（AGENT_FASTPATH）两臂读数可读、档位不泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
