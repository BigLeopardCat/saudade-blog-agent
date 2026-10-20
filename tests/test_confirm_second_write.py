"""确认兑现轮里的**第二件写**：该弹的第二张卡，和兜住裸调用正文的那道网（20261012）。

## 现场（生产 trace `20261012T101621`，会话 342，一句话两件写）

主人第一轮：「猫咪给用户demo发个通知…**然后给他身份降级为杂鱼**」⇒ planner 两件都识别
出来了（`task_auto.goals` 两条）⇒ 第一件 `send_user_notice` 弹卡 ⇒ 主人点「确定」。确认
兑现轮里连着两处塌方：

  · **第二张卡不弹**：`execute_node` 把前端合成的**卡面文本**（`确认执行：…吗？点「确定」
    我就去办。`）当成了主人的话 ⇒ ①`_confirm_popup` 见问号判"像提问"⇒ 整批不弹；
    ②同意闸 `consent_granted(name, 卡面文本)` 也恒假 ⇒ 第二件落成 `consent_required`
    BLOCK。`blocked` 非空又把 `_planner_preflight` 的 `resumed` 判假 ⇒ planner 确定性
    收尾（`confirm_wrap`）⇒ 主人读到一句"你说一声我就去办"的征询，而那件事一件没做。
  · **裸调用正文**：最后一轮 narrator 干脆把 `set_account_role` 的**调用写成了正文**
    （服务商特判 token 形，逐字见 `_real_reply()`）。它不是响应里的 `tool_calls` 字段
    ⇒ 20261006 那层纠正（判字段的）看不见；正文又被逐字流给主人 ⇒ 主人读到一段裸标记。

## 锁住的七条

  ① **源头**：确认兑现轮 `execute_node` 判写用的是**主人原话**（`_intent_src`），于是
     第二件写真**弹卡**（`pending_confirm` 非空、零执行、零 BLOCK）。
  ② **反控（前提在场）**：同一个 state 只把主人原话拿掉（只剩卡面）⇒ 判成 BLOCK。
     没有这一条，①可能是"判据恒真"——本仓最贵的那种绿。
  ③ 换源**不外溢**：非确认轮仍读最后一条消息；只有一条消息时退回它（不抛）。
  ④ **兜底**：现场那段裸调用逐字过**真的** `gate_node` ⇒ `done=True` +
     `fallback_text=_FALLBACK_TOOLCALL_MARKUP`（server 据此 `__RESET__` 撤回已展示正文）
     + trace 里 `fallback.issue=toolcall_markup_reply`。
  ⑤ **反向对照**：正常回复一个字都不动（不设这张网时"每轮都被兜底"那种坏死法）。
  ⑥ **提及不是调用**：讲机制时在引号/内联代码/举例括号里写这些字样 ⇒ 放行（与命令前缀族
     同一条豁免；主人问"你怎么防止假装调工具"时正会这么答）。
  ⑦ **不去重规划**：`toolcall_markup_reply` **不在** `_REPLAN_ISSUES` 里（正确出路不是
     "再去调一次工具"——那一件该走确认流程），兜底文案也不许反问「要不要继续」
     （那正是台账续跑那条网要拦的征询，兜底自己再犯等于从后门漏掉）。

无网络 / 无 LLM：假工具 + 真 `execute_node` / 真 `gate_node`。
"""


import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_BAR = "｜"          # 全角竖线：服务商特判 token 用的是它，不是 ASCII 的 |


_TOKEN = _BAR * 2 + "DSML" + _BAR * 2


def _real_reply() -> str:
    """trace `20261012T101621` 的 reply **逐字**重建（不把那段字直接写进本文件）。"""
    return "\n".join([
        f"<{_TOKEN} calls>",
        f'<{_TOKEN} invoke name="set_account_role">',
        f'<{_TOKEN} parameter name="name" string="true">demo</{_TOKEN} parameter>',
        f'<{_TOKEN} parameter name="role" string="true">杂鱼</{_TOKEN} parameter>',
        f"</{_TOKEN} invoke>",
        f"</{_TOKEN} calls>"])


OWNER = ("猫咪给用户demo发个通知告诉他被降级为杂鱼了，理由你随便填，"
         "以你的身份口吻说，然后给他身份降级为杂鱼")


CARD = ("确认执行：要给账号「demo」（账号 id=747）发一条**站内通知**（标题「站内通知」）"
        "吗？点「确定」我就去办。")


NOTICE = {"tool": "send_user_notice", "args": {"name": "demo", "content": "喵～"}}


ROLE_SPEC = 'set_account_role({"name": "demo", "role": "zako"})'


PLAN_OBJ = {"workflow": "account_set_role", "params": {}, "tools": [ROLE_SPEC],
            "note": "x", "reply": "y"}


_CFG = {"configurable": {"principal": Principal(uid=1, role="superadmin"),
                         "user_id": 1, "conversation_id": 342, "stop_event": None}}


class _FakeWrite:
    """真去调后端就炸：这一格判的是"弹卡 ⇒ 一个工具都不执行"。"""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def invoke(self, args):  # pragma: no cover - 走到这里就是判据破了
        self.calls += 1
        raise RuntimeError("不该真的调用后端：" + self.name)


def _execute(messages: list) -> dict:
    plan_obj = G.plan_state(PLAN_OBJ)
    st = {**plan_obj, "plan_rounds": 1, "done": False, "messages": messages,
          # 令牌**只签了发通知那一件**（第二件正是本套件的主角）
          "confirm_grant": {"token": "tok", "specs": [dict(NOTICE)]},
          "receipts": [], "executed": []}
    fake = _FakeWrite("set_account_role")
    saved = G._TOOL_MAP.get("set_account_role")
    G._TOOL_MAP["set_account_role"] = fake
    try:
        out = G.execute_node(st, _CFG)
    finally:
        if saved is None:
            G._TOOL_MAP.pop("set_account_role", None)
        else:
            G._TOOL_MAP["set_account_role"] = saved
    out["_calls"] = fake.calls
    return out


def _test_second_write_pops_card():
    print("\n[源头] 确认兑现轮里，令牌没签的第二件写 ⇒ 弹卡（零执行）")
    out = _execute([HumanMessage(content=OWNER), HumanMessage(content=CARD),
                    ToolMessage(content="已把通知发给账号「demo」（账号 id=747）",
                                name="send_user_notice", tool_call_id="c1")])
    pop = out.get("pending_confirm") or {}
    check("弹了卡", bool(out.get("pending_confirm")),
          str(sorted(out.keys())))
    check("卡上就是第二件写",
          [s.get("tool") for s in pop.get("specs") or []] == ["set_account_role"],
          str(pop.get("specs")))
    check("没有一条写成 BLOCK", not out.get("blocked"), str(out.get("blocked")))
    check("一个工具都没执行", out["_calls"] == 0, str(out["_calls"]))
    check("没有新增回执", not out.get("receipts"), str(out.get("receipts")))


def _test_card_only_still_blocks():
    print("\n[反控] 前提不在场（只剩卡面文本）⇒ 判成 consent_required BLOCK")
    out = _execute([HumanMessage(content=CARD)])
    check("没弹卡", not out.get("pending_confirm"), str(out.get("pending_confirm")))
    check("落成 BLOCK（就是 trace 里那一条）",
          [b.get("tool") for b in out.get("blocked") or []] == ["set_account_role"],
          str(out.get("blocked")))
    check("零执行", out["_calls"] == 0, str(out["_calls"]))


def _test_intent_src_boundary():
    print("\n[边界] `_intent_src` 只在确认兑现轮换源")
    two = [HumanMessage(content=OWNER), HumanMessage(content=CARD)]
    check("确认兑现轮读主人原话",
          G._intent_src({"messages": two, "confirm_grant": {"token": "t"}}) == OWNER)
    check("非确认轮读最后一条（行为逐字节不变）",
          G._intent_src({"messages": two}) == CARD)
    check("只有一条消息时也读它（取不到第二条不抛）",
          G._intent_src({"messages": [HumanMessage(content=OWNER)],
                         "confirm_grant": {"token": "t"}}) == OWNER)


def _gate(reply: str, tid: str) -> tuple[dict, list]:
    rec = trace_mod.start_trace(tid, 1, "th", {}, dir=tempfile.mkdtemp(),
                                by_day=False)
    notice = {"tool": "send_user_notice", "args": dict(NOTICE["args"]),
              "result": "已把通知发给账号「demo」（账号 id=747）", "ts": 1.0}
    st = {"messages": [HumanMessage(content=OWNER), HumanMessage(content=CARD),
                       ToolMessage(content=notice["result"], name="send_user_notice",
                                   tool_call_id="c1"),
                       AIMessage(content=reply)],
          "plan": "WORKFLOW=content_query\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）",
          "receipts": [notice]}
    return G.gate_node(st, _CFG), rec.events


def _test_wire_markup_reply_falls_back():
    print("\n[兜底] 正文里的裸调用 ⇒ fallback（`__RESET__` 撤回已展示正文）")
    out, events = _gate(_real_reply(), "t_wire_real")
    ev = [e for e in events if e.get("event") == "fallback"]
    check("判成 fallback（done=True）", out.get("done") is True, str(out.get("done")))
    check("替换文本是那一句如实话术",
          out.get("fallback_text") == G._FALLBACK_TOOLCALL_MARKUP,
          repr(out.get("fallback_text")))
    check("trace 里 issue=toolcall_markup_reply",
          [e.get("issue") for e in ev] == ["toolcall_markup_reply"], str(ev))
    check("被否掉的那一句记进了 clause",
          bool(ev and ev[0].get("clause")), str(ev))


def _test_normal_reply_untouched():
    print("\n[反向对照] 正常回复 ⇒ 不兜底")
    out, events = _gate("好呀～通知已经送给 demo 啦，降级那件事我再跟你确认一次。",
                        "t_wire_ok")
    check("没兜底", not out.get("fallback_text"), repr(out.get("fallback_text")))
    check("没有 fallback 事件",
          [e for e in events if e.get("event") == "fallback"] == [])


def _test_mention_spans_exempt():
    print("\n[豁免] 讲机制时举例 ⇒ 放行")
    cases = [
        ("引号", '正文里写「<invoke name="x">」这种工具调用格式会被系统拦下，不会执行。'),
        ("内联代码", "比如 `tool_calls` 这种字段名，我这边是零工具的，不会真的调用。"),
        ("举例括号", "我不会输出这种格式（比如 <invoke> 这种标签），那是伪工具调用。"),
    ]
    for label, text in cases:
        check(f"{label}里的提及不判违规", G._toolcall_markup_hit(text) == "",
              repr(G._toolcall_markup_hit(text)))
    # 反控：把豁免区拿掉（裸写在正文里）⇒ 必须判
    check("反控：同一句话裸写在正文里 ⇒ 判违规",
          bool(G._toolcall_markup_hit('好的，<invoke name="x"> 我这就去办。')))


def _test_not_a_replan_issue():
    print("\n[结构] 这一族走兜底，不消耗那枚一次性的重规划令牌")
    check("不在 `_REPLAN_ISSUES` 里",
          "toolcall_markup_reply" not in G._REPLAN_ISSUES)
    check("兜底文案不反问「要不要继续」（那是台账续跑那条网拦的形状）",
          "要不要" not in G._FALLBACK_TOOLCALL_MARKUP
          and "你说一声我就去办" not in G._FALLBACK_TOOLCALL_MARKUP)
    # 判据是**同源那一份正则**，不是另抄一张字表：五种线格式都得进网。
    faces = [_real_reply(),
             '<invoke name="set_account_role">',
             "<tool_calls>\n<invoke name=\"x\">",
             'antml:invoke name="x"',
             '好的，<|tool_call|>{"name": "x"}',
             '<parameter name="name" string="true">demo</parameter>']
    miss = [f[:28] for f in faces if not G._toolcall_markup_hit(f)]
    check("五种线格式都在网内", not miss, str(miss))


TODO_SPEC = 'create_dashboard_todo({"text": "给用户niuniu重置额度"})'


_TODO_SPEC_D = {"tool": "create_dashboard_todo", "args": {"text": "给用户niuniu重置额度"}}


_GRANT_TODO = {"token": "tok", "workflow": "dashboard_todo_add", "specs": [_TODO_SPEC_D]}


_QUOTA_TOKEN = {"token": "tok", "workflow": "quota_reset",
                "specs": [{"tool": "reset_user_quota", "args": {"uid": 9}}]}


_OWNER_4 = ("猫咪加条待办给用户niuniu重置额度，然后去把他额度重置，"
            "然后把待审的留言你审核一下是否通过，然后再发一条公告，以你的口吻随便说点声明")


_CARD_TODO = ("确认执行：要在后台首页的待办里加一条「给用户niuniu重置额度」，排期 未排期"
              "（那是你自己那份列表，加完随时能改删）吗？点「确定」我就去办。")


def _test_grant_spent_after_first_use():
    print("\n[令牌] 一次点头 = 那一件只办一次（同一轮里第二次不再静默执行）")
    _fresh = {"confirm_grant": _GRANT_TODO, "executed": []}
    _spent = {"confirm_grant": _GRANT_TODO, "executed": [TODO_SPEC]}
    check("正面控制：本轮还没做过 ⇒ 覆盖（判据不是恒假）",
          G._grant_covers(_fresh, TODO_SPEC) is True)
    check("本轮已经执行过同一件 ⇒ **不再**覆盖", G._grant_covers(_spent, TODO_SPEC) is False)
    check("  整批那格随之判假 ⇒ 弹卡那一路不再闭嘴",
          G._all_granted(_fresh, [TODO_SPEC]) is True
          and G._all_granted(_spent, [TODO_SPEC]) is False)
    # 归一化与裁剪/回执同源（`_spec_signature`）：类型差一档仍是同一件
    check("  归一化同源：令牌里的 `{\"uid\": 9}` 与执行过的那份 `{\"uid\": \"9\"}` 是同一件",
          G._grant_covers({"confirm_grant": _QUOTA_TOKEN,
                           "executed": ['reset_user_quota({"uid": "9"})']},
                          'reset_user_quota({"uid": 9})') is False)
    check("  反控：执行过的是**另一件**（换参数）⇒ 这一件照旧被覆盖",
          G._grant_covers({"confirm_grant": _QUOTA_TOKEN,
                           "executed": ['reset_user_quota({"uid": 8})']},
                          'reset_user_quota({"uid": 9})') is True)

    # 端到端：重复那一件过**真的** `execute_node`（第 2 轮）⇒ 零执行、不留静默口子
    plan_obj = G.plan_state({"workflow": "dashboard_todo_add", "params": {},
                             "tools": [TODO_SPEC], "note": "x", "reply": "y"})
    st = {**plan_obj, "plan_rounds": 1, "done": False,
          "messages": [HumanMessage(content=_OWNER_4), HumanMessage(content=_CARD_TODO)],
          "confirm_grant": _GRANT_TODO,
          "receipts": [{"workflow": "dashboard_todo_add", "tool": "create_dashboard_todo",
                        "args": {"text": "给用户niuniu重置额度"},
                        "result": "已在后台首页的待办里加了一条", "ts": 1.0}],
          "executed": [TODO_SPEC], "noop_specs": [], "blocked": [], "blocked_seen": []}
    fake = _FakeWrite("create_dashboard_todo")
    saved = G._TOOL_MAP.get("create_dashboard_todo")
    G._TOOL_MAP["create_dashboard_todo"] = fake
    try:
        out = G.execute_node(st, _CFG)
    finally:
        if saved is None:
            G._TOOL_MAP.pop("create_dashboard_todo", None)
        else:
            G._TOOL_MAP["create_dashboard_todo"] = saved
    pop = out.get("pending_confirm") or {}
    check("一个工具都没执行（这一轮不再出现第二份同款回执）", fake.calls == 0, str(fake.calls))
    # 回执是**累积**返回的（进 state 那一份原样带出）⇒ 判的是"没有**新增**"，不是"空"
    _got = [r for r in (out.get("receipts") or [])
            if r.get("tool") == "create_dashboard_todo"]
    check("没新增回执（同款回执仍只有进场那一份）", len(_got) == 1, str(out.get("receipts")))
    check("退回到人的那一环：弹出的是这一件自己的卡（不是静默放行）",
          [s.get("tool") for s in (pop.get("specs") or [])] == ["create_dashboard_todo"],
          str(pop.get("specs")))


def test_confirm_second_write():
    _SAVED_SECRET = settings.jwt_secret
    settings.jwt_secret = "test-secret-for-confirm-tokens-not-a-real-secret"
    for fn in (_test_second_write_pops_card,
               _test_card_only_still_blocks,
               _test_intent_src_boundary,
               _test_wire_markup_reply_falls_back,
               _test_normal_reply_untouched,
               _test_mention_spans_exempt,
               _test_not_a_replan_issue,
               _test_grant_spent_after_first_use):
        fn()
    print()
    settings.jwt_secret = _SAVED_SECRET   # 全局单例：还原成进来时的样子
    if FAILS:
        print(f"❌ {len(FAILS)} 条判据未通过：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_confirm_second_write()
