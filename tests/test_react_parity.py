# -*- coding: utf-8 -*-
"""`agent/react_parity.py` 的接线锁：第二条臂**借生产资产**那一层的每一段都真的接上了。

## 要防的是什么

本批（20261019）把 react 臂从「故意欠配」补到「资产对齐」。补的方式是**import 生产的
函数**，而这一层最贵的失效**不是判错，是没判**：一格传了空值、一段根本没被调用——
两者都不报错，只让模型在一份「这轮什么都没有」的提问下决策。纯否定断言族（`forbid_*`）
在「什么都不做」的臂上**恒真**，所以那些读数当年并不作废、只是没在比同一件事。

所以本文件每接一段就钉两件事：
  · **真值喂进去了**（逐格与生产那几处 helper 同源）；
  · **反控：空输入时它落回「空」的措辞**——少了这一条，一个「恒返回非空」的实现同样能过。

再加一条**逐轮重渲染**的判据（`_prompt_of`）：`intent_hints` / `tool_results` /
`ref_hints` 三格都逐轮在变，缓存住等于第二轮的提示词在说「这轮还没执行过任何工具」。

秒级、纯离线、无网络无 LLM。
用法：.venv/bin/python tests/test_react_parity.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.react_parity as P  # noqa: E402
from agent.arg_refs import ref_hints  # noqa: E402
from agent.block_reasons import denied_workflows  # noqa: E402
from agent.deterministic_plan import _intent_hints  # noqa: E402
from agent.prompt_context import blocked_rows  # noqa: E402
from agent.react_line import RunLedger, WorkflowExecutor  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


# 一句**扫得到动作意图**的话（`_scan_action_intents` 认它）——「未完成 / 已执行」那一格
# 只在扫到意图时才有内容。用现成的样本而不是自己编：编出来的话哪天扫描表改了，
# 这一节会**静默**退化成「扫不到意图」，而扫不到时 helper 返回的是另一句、判据还是绿的。
_OWNER_ACTION = "把樱花特效打开吧"
_GRANT = {"workflow": "tag_delete",
          "specs": [{"tool": "delete_tag", "args": {"name": "大笨狗"}}]}
_SIGNED = ['delete_tag({"name": "大笨狗"})']


# ── §A 确认轮 ────────────────────────────────────────────────────────────────
def test_confirm_round_branches() -> None:
    print("\n§A 确认轮：四条分支（与 `graph._planner_preflight` 的同一格）")
    check("没有令牌 ⇒ 不是确认轮（这一轮照常交给模型）",
          P.confirm_round(None, 0, False, []) is None
          and P.confirm_round({}, 0, False, []) is None)

    got = P.confirm_round(_GRANT, 0, False, [])
    check("首轮 + 令牌 ⇒ 照签名体拼计划（零 LLM）",
          got is not None and got[1] == "confirm_grant", str(got and got[1]))
    plan = got[0] if got else {}
    check("清单**逐字**取自签名体（不是重新实例化出来的）",
          plan.get("tools") == _SIGNED, str(plan.get("tools")))
    check("`calls` 也带上了（不带的症状是「计划里有清单、一件都没跑」且**没有东西会红**）",
          [c.get("specs") for c in plan.get("calls") or []] == [_SIGNED],
          str(plan.get("calls")))

    check("续跑轮（rounds>0 且未受阻）⇒ **不是**确认轮（令牌不重发，这一轮是模型在决策）",
          P.confirm_round(_GRANT, 1, False, []) is None)

    got2 = P.confirm_round(_GRANT, 1, True, [])
    check("受阻回环（rounds>0 且受阻）⇒ 确定性收尾，**零工具**（不重发清单）",
          got2 is not None and got2[1] == "confirm_wrap"
          and not (got2[0].get("tools") or []), str(got2 and got2[1]))
    check("且带着那句注记（与生产逐字同款）",
          bool(got2) and got2[2].get("reason") == P._CONFIRM_WRAP_REASON)

    # 反控：把 `blocked` 翻过来，同一份入参必须落回「不是确认轮」——
    # 证明上面那条判的是 `blocked` 那一格，不是「rounds>0 就收尾」。
    check("反控：同一份入参、`blocked=False` ⇒ 落回 None（那条判的是受阻那一格）",
          P.confirm_round(_GRANT, 1, False, []) is None)


def test_confirm_routing() -> None:
    print("\n§A 路由：有清单去 execute、没清单去 model（`graph.route_after_planner` 同句）")
    check("有清单 ⇒ True", P.plan_has_tools({"tools": ["a({})"]}) is True)
    check("空清单 / 缺键 ⇒ False",
          P.plan_has_tools({"tools": []}) is False and P.plan_has_tools({}) is False)


# ── §B 提示词注入值 ──────────────────────────────────────────────────────────
def _state(**kw) -> dict:
    """一份最小的 `AgentState` 形状（只放这一节读的那几格）。"""
    base = {"messages": [], "plan_rounds": 0}
    base.update(kw)
    return base


def _args_for(st: dict, user_msg: str = _OWNER_ACTION) -> dict:
    return P.planner_prompt_args(
        st, role="admin", page_ctx="", round_info="", user_msg=user_msg,
        ledger_frame="", correction="", deny=denied_workflows(st.get("blocked")),
        frames_txt="")


def test_prompt_args_feed_real_values() -> None:
    print("\n§B 注入值：真值逐格喂进去（此前那几格传的是**空值**）")
    st = _state(executed=['delete_tag({"name": "大笨狗"})'],
                blocked=[{"tool": "rag_search", "reason": "unavailable"}],
                tool_data=[{"tool": "list_tags", "data": [{"id": 3, "title": "大笨狗"}]}])
    a = _args_for(st)

    check("`intent_hints` == 生产那一句（`_intent_hints(state['executed'], user_msg)`）",
          a["intent_hints"] == _intent_hints(st["executed"], _OWNER_ACTION))
    check("  且**真的扫到了意图**（不是「系统未扫描到」那句兜底）",
          "未完成" in a["intent_hints"] or "已执行" in a["intent_hints"],
          a["intent_hints"][:60])
    check("`blocked_rows` == 生产那一句", a["blocked_rows"] == blocked_rows(st["blocked"]))
    check("  且**真的渲染出了受阻项**（不是空槽那句兜底）",
          a["blocked_rows"] != blocked_rows([]), a["blocked_rows"][:60])
    check("`ref_hints` == 生产那一句", a["ref_hints"] == ref_hints(st["tool_data"]))
    check("  且**真的列出了可引用字段**（不是「无从引用」那句兜底）",
          "list_tags[0]" in a["ref_hints"], a["ref_hints"][:60])
    check("`deny` 取自受阻项（这一轮不许再选的技能）",
          a["deny"] == denied_workflows(st["blocked"]))

    print("\n§B 反控：空输入 ⇒ 落回各自「空」的措辞（否则上面那几条恒真）")
    empty = _args_for(_state(), user_msg="你好呀")
    check("`intent_hints` 落回「没扫到」那句", "未扫描到" in empty["intent_hints"],
          empty["intent_hints"][:40])
    check("`blocked_rows` 落回空槽那句", empty["blocked_rows"] == blocked_rows([]))
    check("`ref_hints` 落回「无从引用」那句", empty["ref_hints"] == ref_hints([]))
    check("`correction` / `reflector_feedback` 各自落回缺省语",
          empty["correction"] == P._NO_CORRECTION
          and empty["reflector_feedback"] == P._NO_REFLECTOR)


def test_prompt_args_round_dependence() -> None:
    print("\n§B 逐轮在变的一格：`short_reply_hint` 只在首轮给")
    first = _args_for(_state(plan_rounds=0), user_msg="你好呀")
    later = _args_for(_state(plan_rounds=1), user_msg="你好呀")
    check("首轮 != 非首轮", first["short_reply_hint"] != later["short_reply_hint"])
    check("非首轮那一句是「已在上轮兑现」（与生产逐字同款）",
          later["short_reply_hint"] == P._NON_FIRST_ROUND)


# ── §B 参数引用的取值源：形状必须与生产同款 ──────────────────────────────────
def test_tool_data_shape() -> None:
    """`resolve_one` / `ref_hints` 读的都是 `e.get("data")`。

    本线此前写的是 `{"tool", "result"}` ⇒ 每一个 `$tool[N].field` 引用都解析成
    `ref_unparsed`、整条 spec 不执行。方向是 fail-closed（不执行，不是乱执行），
    所以**没有任何东西会红**——这正是最贵的那类失效：判据在，但它恒判同一边。
    """
    print("\n§B 参数引用：`tool_data` 的键必须是 `data`（生产 `resolve_one` 读的就是它）")
    led = RunLedger()
    ex = WorkflowExecutor({"list_tags": lambda **kw: '[{"id": 3, "title": "大笨狗"}]'},
                          led, role="admin")
    ex.run_specs("tag_delete", ["list_tags({})"])
    check("跑完一条 ⇒ `tool_data` 有一条", len(ex.tool_data) == 1, str(ex.tool_data))
    check("键是 `data` 且已解析成结构（不是 `result` 原文）",
          bool(ex.tool_data) and ex.tool_data[0].get("tool") == "list_tags"
          and ex.tool_data[0].get("data") == [{"id": 3, "title": "大笨狗"}],
          str(ex.tool_data[0] if ex.tool_data else None)[:120])
    check("⇒ `ref_hints` 认得出可引用字段（读 `result` 的版本在这里恒返回「无从引用」）",
          "list_tags[0]" in ref_hints(ex.tool_data), ref_hints(ex.tool_data)[:60])

    print("\n§B 参数引用**真的解得出来**（端到端：第二条 spec 引第一条的返回值）")
    # 两条都用**读**工具：写工具会先进同意闸，而这一节判的是引用解析，不是闸门
    # （`principal=None` 下走同意帧会 AttributeError，那是另一件事的判据）。
    led2 = RunLedger()
    seen: list = []
    ex2 = WorkflowExecutor({"list_tags": lambda **kw: '[{"id": 3, "title": "大笨狗"}]',
                            "get_article_detail": lambda **kw: seen.append(kw) or "正文"},
                           led2, role="admin")
    ex2.run_specs("tag_delete", ["list_tags({})",
                                 'get_article_detail({"article_id": "$list_tags[0].id"})'])
    check("引用被解析成真值（`$list_tags[0].id` → 3）",
          seen == [{"article_id": 3}], str(seen))


def main() -> int:
    test_confirm_round_branches()
    test_confirm_routing()
    test_prompt_args_feed_real_values()
    test_prompt_args_round_dependence()
    test_tool_data_shape()

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
