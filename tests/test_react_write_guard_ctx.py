# -*- coding: utf-8 -*-
"""写目标拒绝链接线锁（资产对齐 D+F，20261019）：**喂给生产的输入**与**拿回来的产物**。

## 为什么这条测试存在

react 臂此前**整条没有**写目标拒绝链（`agent/graph.py` 里 0 引用）。补齐的办法不是抄
一遍判据，而是把 `_decide_write_guard` 直调进来（理由见 `agent/react_parity.py` 的头注）。
"直调"这件事本身有两个**静默**失效面，这一份文件就是钉它们的：

  ① **入参缺一格**。`graph._DecideCtx` 是个没有默认值的 dataclass，可它的字段会漂
     （20261019 之前就加过好几格）。字段一多，接线处漏一格的形态是**两种**：
     漏了**没默认值**的 ⇒ 构造时 TypeError（响亮，白送）；漏了**带默认值**的 ⇒
     那一格永远吃默认值、判据**静默地少看一件事**。所以锁的对象必须是
     `dataclasses.fields(_DecideCtx)` 这个**名字集合**，不是"能不能构造出来"。
  ② **产物丢在半路**。拒绝链命中时改的是 `c.plan_obj`（换成收尾计划 + 挂 `refusal`），
     返回的是 `_Step.ret`。调用方要是拿原来那份继续往下走，形状是"判据算了、trace 落了、
     写还是执行了"——一次本该被拒的写照样落库，而**没有任何东西会红**。

## 锁的七条

  ①②③ 字段集双向差集（对象 = `dataclasses.fields(_DecideCtx)`）+ 真构造一次 + 反控；
  ④    `write_guard` 的两条出口都接对了（拒了 ⇒ 返回的是**零工具的收尾计划**、
        没命中 ⇒ None）；
  ⑤    没接线的动作必须**响亮地炸**（反控：`_decide_write_guard` 哪天多一种出口）；
  ⑥    接线位置锁（AST）：`planner_node` 里 `write_guard` → `finalize_dedupe` →
        `_sync_calls` 三处都在、且同步在最后（顺序是承重的，见 `react_arm` 那两段注）；
  ⑦    `_sync_calls` 的两条形态锁——**计划被重建**（`calls` 整格消失）时补回来、
        以及**同工具多调用**时一个名字一个坑。

离线、秒级、零网络（拒绝链那一格用的是"值在主人原话里找不到来源"，它在
`_name_arg_fix` 就返回了，走不到要读站内字典的那几条）。
用法：SAUDADE_IGNORE_ENV_FILE=1 .venv/bin/python tests/test_react_write_guard_ctx.py
"""
import ast
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as G  # noqa: E402
import agent.react_arm as R  # noqa: E402
import agent.react_parity as P  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


_PRINCIPAL = Principal(uid=1, role="admin")


def _kwargs(**over) -> dict:
    """接线处那一份入参（形状与 `planner_node` 的调用点同款）。"""
    kw = P.decide_ctx_kwargs(state={}, config=None, rounds=0, user_msg="",
                             role="admin", page_ctx="", principal=_PRINCIPAL,
                             plan_obj={}, has_frames=False, decided=None)
    kw.update(over)
    return kw


def _ctx(**over) -> dict:
    """`write_guard` 要的那个 ctx（本臂的 ctx，不是 `_DecideCtx`）。"""
    ctx = {"config": None, "user_msg": "", "role": "admin", "page_ctx": "",
           "principal": _PRINCIPAL, "draft": None}
    ctx.update(over)
    return ctx


# ── ①②③ 字段集锁 ─────────────────────────────────────────────────────────────
def test_field_lock() -> None:
    print("\n①②③ 字段集锁：`_DecideCtx` 的每一格都被显式喂上（漏一格 = 判据少看一件事）")
    fields = {f.name for f in dataclasses.fields(G._DecideCtx)}
    given = set(_kwargs())
    check("**没有漏喂的格**（缺一格 ⇒ 那一格永远吃默认值，而判据不报错）",
          not (fields - given), str(sorted(fields - given)))
    check("**没有多余的格**（多一格 ⇒ 构造时 TypeError，但那是撞上的、不是钉住的）",
          not (given - fields), str(sorted(given - fields)))
    check("字段数是**从生产那一侧现取**的（这里不写死数字：加一格时红的应该是差集那条）",
          len(fields) == len(given) and len(fields) > 0, f"{len(fields)} 格")
    # 真构造一次：差集对了但类型不对（比如某格传了 None 而它要求 str）照样会炸在这里。
    try:
        G._DecideCtx(**_kwargs())
        built = True
    except Exception as e:  # noqa: BLE001
        built = False
        print(f"      构造异常：{type(e).__name__}: {e}")
    check("真构造一次 `_DecideCtx(**kwargs)` 不炸（差集对 ≠ 类型对）", built)

    print("\n③ 反控：改掉一个键名 ⇒ ① 的差集当场红")
    fake = dict(_kwargs())
    fake["planner_notes"] = fake.pop("planner_note")   # 少一格、多一格，各一条
    check("少一格被认出来", bool(fields - set(fake)), str(sorted(fields - set(fake))))
    check("多一格被认出来", bool(set(fake) - fields), str(sorted(set(fake) - fields)))


# ── ④⑤ `write_guard` 的两条出口 ──────────────────────────────────────────────
def test_write_guard_arms() -> None:
    print("\n④ `write_guard` 的两条出口（拒了 ⇒ 零工具收尾计划；没命中 ⇒ None）")
    refused = P.write_guard({}, ctx=_ctx(user_msg="帮我建个标签"),
                            plan_obj=instantiate_plan("tag_create",
                                                      {"title": "编的名字"}, "admin"),
                            rounds=0, has_frames=False)
    check("值在主人原话里找不到来源 ⇒ **命中**（返回的不是 None）",
          refused is not None, str(refused)[:80])
    check("返回的是**收尾计划**、零工具（拿原来那份继续往下走 = 一次本该被拒的写照样落库）",
          bool(refused) and not (refused.get("tools") or []),
          str((refused or {}).get("workflow")) + str((refused or {}).get("tools")))
    check("`refusal` 挂在返回的那一份上（收尾措辞的出处；原计划那份**没有**这一格）",
          bool((refused or {}).get("refusal")), str((refused or {}).get("refusal"))[:80])
    check("收尾正文里说清了「没有改动任何内容」这类事实（主人读到的是「没做」而不是「失败」）",
          "没有" in str((refused or {}).get("note") or ""),
          str((refused or {}).get("note"))[:80])

    ok = P.write_guard({}, ctx=_ctx(user_msg="带我去留言板"),
                       plan_obj=instantiate_plan("navigate", {"target": "留言板"}, "admin"),
                       rounds=0, has_frames=False)
    check("非写计划 ⇒ **不命中**（`None`，调用方照常往下走）", ok is None, str(ok))

    print("\n⑤ 反控：没接线的动作必须**响亮地炸**（不猜、不静默放行）")
    # 替身打在 `react_parity` 那一侧（它是 `from agent.graph import _decide_write_guard`
    # 取进自己命名空间的）——打在 `agent.graph` 上不会生效，而那正是**这条反控自己**要
    # 防的形态：一个不生效的替身会让"响亮地炸"这句断言恒绿。
    old = P._decide_write_guard
    P._decide_write_guard = lambda c: G._Step("skip", None)  # noqa: ARG005
    try:
        try:
            P.write_guard({}, ctx=_ctx(), plan_obj=instantiate_plan("chat", {}, "admin"),
                          rounds=0, has_frames=False)
            raised = ""
        except RuntimeError as e:
            raised = str(e)
    finally:
        P._decide_write_guard = old
    check("第三种动作 ⇒ RuntimeError（生产只有 `next` / `return` 两个出口）",
          "skip" in raised, raised[:80])


# ── ⑥ 接线位置锁 ─────────────────────────────────────────────────────────────
def _calls_in(fn: ast.AST, name: str) -> list[int]:
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == name]


def test_wiring() -> None:
    print("\n⑥ 接线位置锁：`planner_node` 里三处都在、且 `_sync_calls` 在最后")
    tree = ast.parse((ROOT / "agent" / "react_arm.py").read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "planner_node"), None)
    check("`planner_node` 找得到", fn is not None)
    if fn is None:
        return
    guard, dedupe, sync = (_calls_in(fn, n) for n in ("write_guard", "finalize_dedupe",
                                                      "_sync_calls"))
    check("`write_guard` 被调了（拒绝链接上了）", bool(guard), str(guard))
    check("`finalize_dedupe` 被调了（八条去重/拦截接上了）", bool(dedupe), str(dedupe))
    check("**拒绝链在去重之前**（顺序照生产 `_planner_decide`：D5 在 `_finalize_dedupe` 前）",
          bool(guard) and bool(dedupe) and max(guard) < min(dedupe),
          f"guard={guard} dedupe={dedupe}")
    # `_sync_calls` 必须**在两次改写之后**各来一次：定稿清单不落回 `calls`，
    # execute 用的就是改写之前那份（判据的结论落不进执行）。
    check("`_sync_calls` 每次出口前都调了（两条出口各一次 ⇒ 至少两处调用点）",
          len(sync) >= 2, str(sync))
    check("**`_sync_calls` 排在两个改写步之后**（先定稿、再同步）",
          bool(sync) and bool(dedupe) and min(sync) > max(dedupe),
          f"dedupe={dedupe} sync={sync}")


# ── ⑦ `_sync_calls` 的两条形态锁 ─────────────────────────────────────────────
def test_sync_calls() -> None:
    print("\n⑦ `_sync_calls`：计划被重建 ⇒ 补回 `calls`；同工具多调用 ⇒ 一个名字一个坑")
    # 形态一：`_name_arg_fix` 那类校正是 `plan_obj.clear() + update(fresh)`，而 `fresh`
    # 是 `instantiate_plan` 的产物、**没有 `calls` 这一格**。不补回来的形状是
    # "判据把名字校正回来了、计划里也写着，而 execute 读到零条调用 ⇒ 零执行、不弹卡、
    # 不报错"（实测：⑥b′ 那一格一次写就这么静默消失过）。
    rebuilt = instantiate_plan("tag_delete", {"name": "大笨狗"}, "admin")
    check("重建出来的计划**确实没有** `calls` 这一格（前提守卫：没有它这条锁是空的）",
          "calls" not in rebuilt, str(sorted(rebuilt)))
    R._sync_calls(rebuilt)
    calls = rebuilt.get("calls") or []
    check("补回了一条调用，`specs` == 定稿清单（逐字）",
          len(calls) == 1 and calls[0]["specs"] == list(rebuilt["tools"]),
          str(calls))
    check("补回来的那条带着 `workflow` 与 `params` 两格（`params` 是无进展判据的键的取值格，"
          "缺席会让 execute 那一侧 `(c or {}).get(\"params\")` 取到空）",
          calls[0]["workflow"] == "tag_delete"
          and isinstance(calls[0]["params"], dict)
          and set(calls[0]) == {"workflow", "params", "specs"},
          str({k: calls[0][k] for k in ("workflow", "params")}))

    # 形态二：同一轮点两条同工具调用（"把樱花换成下雨"最自然的形态）。
    # "按名字集合捞"会让**第一条调用把两条都吃掉**、第二条只剩空清单 ⇒ 它退回
    # `run(workflow, params)` 重新实例化，凭空多跑一次（实测 2 件变 3 件）。
    plan = {"workflow": "effect", "params": {},
            "tools": ['toggle_effect({"effect": "sakura", "action": "off"})',
                      'toggle_effect({"effect": "rain", "action": "on"})'],
            "calls": [{"workflow": "effect", "params": {"effect": "sakura", "action": "off"},
                       "specs": ['toggle_effect({"effect": "sakura", "action": "off"})']},
                      {"workflow": "effect", "params": {"effect": "rain", "action": "on"},
                       "specs": ['toggle_effect({"effect": "rain", "action": "on"})']}]}
    R._sync_calls(plan)
    got = [c["specs"] for c in plan["calls"]]
    check("两条调用各认自己那一条（**第一条不许把两条都吃掉**）",
          got[0] == ['toggle_effect({"effect": "sakura", "action": "off"})']
          and got[1] == ['toggle_effect({"effect": "rain", "action": "on"})'], str(got))

    print("\n⑦b 反控：定稿清单里有**认领不到**的 spec ⇒ 响亮地炸（不静默丢）")
    # ⚠️ 夹具必须**两条调用**：单调用那一支是生产形状，它整份清单照单全收（那是刻意的，
    # 见 `_sync_calls` 的头注）——拿单调用来试这一条会恒绿。
    orphan = {"workflow": "navigate", "params": {},
              "tools": ['navigate_to({"path": "/"})', 'toggle_dark_mode({"mode": "on"})'],
              "calls": [{"workflow": "navigate", "params": {},
                         "specs": ['navigate_to({"path": "/"})']},
                        {"workflow": "navigate", "params": {},
                         "specs": ['navigate_to({"path": "/guestbook"})']}]}
    try:
        R._sync_calls(orphan)
        raised = ""
    except RuntimeError as e:
        raised = str(e)
    check("没人认领的 spec ⇒ RuntimeError（「这一条帧是谁发的」没有答案，不许猜）",
          "toggle_dark_mode" in raised, raised[:90])


def main() -> int:
    test_field_lock()
    test_write_guard_arms()
    test_wiring()
    test_sync_calls()

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
