"""「主人这一轮说了什么」只剩下**一个**来源（20261012 批 C ①）。

**病**：同一个轮次里住着两个答案——`_planner_preflight` 的 `user_msg`（给二十来处
出处闸、歧义判据、`_forbid_tools` 读）取 `_last_user_msg`，而 `intent_msg`（给意图
清单读）取 `_intent_src`。两者只在**确认兑现轮**上分岔：那一轮最后一条 user 消息是
前端合成的**卡面回显**（`确认执行：……吗？点「确定」我就去办。`）——那是**我们自己的
话**。分岔当场的两处代价（现场 `20261012T101621`，会话 342，一句话两件写）：

  · 出处闸拿卡面去比对实参 ⇒ **主人自己的话被判成捏造**（待办正文那一族四道闸全接在
    `c.user_msg` 上，实测拒得干干净净、卡收回、那一件事一件没做）；
  · `_confirm_popup` 见卡面里的问号判"像提问" ⇒ 第二张卡根本不弹。

`_intent_src` 的注记着同一根错源此前修过的**三处**，本批是**第四处、也是最后一处**。
这一改**只在令牌在场时换源** ⇒ 无令牌的轮次逐字不变（②锁的就是这条影响面）。

本套件锁四件事：① 名字只剩一个（源码锁 + 结构锁）；② 无令牌轮**逐字不变**；
③ 有令牌轮回到主人原话；④ 现场那一格：出处闸在卡面下拒绝、在主人原话下放行，
且它读的确实是同一个 `c.user_msg`。

用法：.venv/bin/python tests/test_session_input_source.py
"""


from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 pytest 当套件收）
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILED: list[str] = []


SRC = _ctx_src.write_guard_src()


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


OWNER = "想给账号「agent_fixture_freeze_a」发一条通知；另外还想在后台待办里加一条「身份变更复核」"


ECHO = "确认执行：就按刚才那张卡办"


TEXT = "身份变更复核"


def _msgs():
    return [HumanMessage(content=OWNER), AIMessage(content="好呀，两件都记下了，先办哪一件？"),
            HumanMessage(content=ECHO)]


def _test_one_name_one_source():
    print("\n① 名字只剩一个：`user_msg` 就是 `_intent_src`，别名 `intent_msg` 已删")
    check("源码锁：`user_msg = _intent_src(state)` 恰好一处（planner 的装配点）",
          SRC.count("user_msg = _intent_src(state)") == 2,
          "两处 = planner 与 execute（执行层 20261012 已先换过）")
    check("源码锁：确认轮上**没有**任何人再读 `_last_user_msg` 当主人的话",
          "user_msg = _last_user_msg(" not in SRC)
    check("结构锁：`_Preflight` 不再有 `intent_msg` 这一格",
          "intent_msg" not in G._Preflight.__annotations__)
    check("结构锁：`_DecideCtx` 不再有 `intent_msg` 这一格",
          "intent_msg" not in {f.name for f in dataclasses.fields(G._DecideCtx)})
    check("  装配段真的把同一个值传下去（`_Preflight` ⇒ `_DecideCtx`，中间没有第二份）",
          "resumed=resumed, user_msg=user_msg," in SRC
          and "user_msg=pre.user_msg, role=role," in SRC)


def _test_no_grant_is_byte_identical():
    print("\n② 无令牌的轮次**逐字不变**（影响面只有确认兑现轮这一格）")
    shapes = [
        [HumanMessage(content="你好")],
        [HumanMessage(content="先带我去留言板"), AIMessage(content="好"), HumanMessage(content="继续吧")],
        _msgs(),
        [],                                    # 路由函数拿最小状态喂进来的那一格
    ]
    for i, msgs in enumerate(shapes):
        st = {"messages": msgs}
        check(f"  形状 {i}：`_intent_src` == `_last_user_msg`",
              G._intent_src(st) == G._last_user_msg(msgs),
              repr(G._intent_src(st))[:40])
    check("对照：**有**令牌时才换源（否则上面那四条是空跑）",
          G._intent_src({"messages": _msgs(), "confirm_grant": {"specs": []}}) != ECHO)


def _test_grant_reads_the_owner_words():
    print("\n③ 确认兑现轮读的是**主人原话**，不是前端合成的卡面回显")
    got = G._intent_src({"messages": _msgs(), "confirm_grant": {"workflow": "x", "specs": []}})
    check("⭐ 回到上一条主人消息（他当初那句话）", got == OWNER, repr(got)[:40])
    check("  卡面回显**不在**这个值里（它是我们自己的话）", ECHO not in got)
    check("  `_last_user_msg` 照旧取最后一条（它是另一个语义，不是错）",
          G._last_user_msg(_msgs()) == ECHO)


def _plan(text: str) -> dict:
    """planner 那一份计划体的形状（与 `tests/test_todo_schedule.py` 的夹具同源）。"""
    obj = instantiate_plan("dashboard_todo_add", {"text": text}, "admin")
    obj["params"] = {"text": text}
    return obj


def _test_the_gate_flips_with_the_source():
    print("\n④ 现场那一格：同一份实参，读错源被判『编造』、读对源放行")
    echo_ref = G._todo_text_fix(_plan(TEXT), ECHO, "admin")
    check("⭐ 拿卡面回显当主人的话 ⇒ **拒绝**（主人自己的话被系统判成捏造，"
          "卡收回、那件事一件没做——这就是要被治掉的那一格）",
          echo_ref is not None, repr(echo_ref)[:60])
    owner_ref = G._todo_text_fix(_plan(TEXT), OWNER, "admin")
    check("⭐ 拿主人原话 ⇒ **放行**（同一个计划、同一个实参，只有一个变量不同）",
          owner_ref is None)
    check("对照：这一族真的从这句话里抽得出那件事（抽不出来时上面两条无意义）",
          G._msg_todo_text(OWNER) == TEXT and G._msg_todo_text(ECHO) is None)
    check("接线锁：这一族读的是**同一个** `c.user_msg`（换源的地方就是它生效的地方）",
          "_todo_text_fix(c.plan_obj, c.user_msg" in SRC
          and "_name_arg_fix(c.plan_obj, c.user_msg" in SRC)


def test_session_input_source():
    for fn in (_test_one_name_one_source, _test_no_grant_is_byte_identical,
               _test_grant_reads_the_owner_words, _test_the_gate_flips_with_the_source):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILED else f"失败 {len(FAILED)} 项 ❌: {FAILED}"))

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_session_input_source()
