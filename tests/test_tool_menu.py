#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/tool_menu.py` 的单测：离线、秒级、零网络零 LLM。

**这份文件锁的是"工具级菜单凭什么能当菜单用"**——它要能替掉技能菜单去喂 `bind_tools`，
就必须满足三件事，缺一条那个 POC 就不是在比"菜单形状"：

  ① **可见性只有一处判据**（`authz` 的 scope 表）。写第二份名单 = 两份迟早分家，而分家的
     方向恰好是**越权**（菜单给出去、执行期才拒，或者更坏：执行期也放行）。这里逐角色
     断言"菜单集合 == 按 authz 表算出来的集合"，并用一条**正控**证明这条断言真的在读表
     （改表 ⇒ 菜单真的变）——不然"两边都空"也能让它绿。
  ② **形状与技能级那份逐格对齐**（`planner_llm.build_tool_schema`）。对不齐的话，模型看到
     的是两套 schema 约定，A/B 比的就不再是菜单内容而是接口形态。
  ③ **跨切面槽位在场**（`intents` / `reply_kind` / 三个伪函数）。它们不是模板资产，是
     harness 资产：少了它们，工具臂会同时丢掉"交意图清单"与"短应答自我申报"两条通道，
     而报告里看起来像"模型变笨了"。

⚠️ **这里的绿不代表"模型会用这张菜单"**。菜单是给模型看的，能不能用只有真链路 A/B
（`eval/experiments/menu_shape_ab.py`）答得了——同 `tests/test_react_line.py` 末尾那条纪律。
"""
import sys
from copy import deepcopy
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from agent import authz  # noqa: E402
from agent.graph import _TOOL_MENU_LINES  # noqa: E402
from agent.planner_llm import build_tool_schema  # noqa: E402
from agent.prompt_context import REPLY_KIND_ARG  # noqa: E402
from agent.tasks import INTENTS_ARG, TASK_DROP, TASK_HOLD, TASK_INTENTS  # noqa: E402
from agent.tool_menu import all_tool_names, build_tool_menu, visible_tools  # noqa: E402
from tools.base import get_all_tools  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)

# 各角色可见的工具数**台账**（与 `tests/test_workflows.py` 的工具数台账同性质）：
# 新增工具或改 `TOOL_SCOPE` 时这个数会变 ⇒ 那一笔改动必须显式改这里，
# 于是"可见面悄悄扩大"在 diff 里留下痕迹。
LEDGER = {"user": 31, "admin": 70, "zako": 0, None: 0}
TOTAL_TOOLS = 70
PSEUDO = (TASK_HOLD, TASK_DROP, TASK_INTENTS)



def _fn(entry: dict) -> dict:
    return entry["function"]


def _from_authz(role) -> list[str]:
    """按 authz 表算出来的可见集合（**与实现同源，但由本文件独立写一遍算式**）。

    独立写一遍是刻意的：若实现改成读别的表，这里就会红——这正是"判据只有一个"的回归锁。
    """
    scopes = authz.scopes_for(role)
    return sorted(n for n in all_tool_names() if authz.required_scope(n) in scopes)


def _sec_role_filter() -> None:
    """① 可见性只有一处判据（含一条**正控**，见那段注）。"""
    print("\n① 角色过滤 —— 判据只有 authz 那张表一处：")
    check("注册表里的工具全部在 TOOL_SCOPE 里有声明（漏声明 ⇒ fail-closed 恒不可见）",
          [t.name for t in get_all_tools() if authz.required_scope(t.name) is None] == [],
          str([t.name for t in get_all_tools() if authz.required_scope(t.name) is None]))
    check(f"工具总数台账 = {TOTAL_TOOLS}", len(all_tool_names()) == TOTAL_TOOLS,
          str(len(all_tool_names())))
    for role, want in LEDGER.items():
        got = visible_tools(role)
        check(f"{role!r}: 菜单 == 按 authz 表算出来的集合", got == _from_authz(role),
              f"{len(got)} vs {len(_from_authz(role))}")
        check(f"{role!r}: 可见数台账 = {want}", len(got) == want, str(len(got)))
    check("未知角色（None）零工具（fail-closed，与执行期同取向）",
          visible_tools(None) == [])
    check("zako 零工具（它在 _ROLE_SCOPES 里是空集）", visible_tools("zako") == [])
    check("顺序确定：按名字升序", visible_tools("admin") == sorted(visible_tools("admin")))

    # 正控：**改表必须让菜单真的变**。不改表也绿 ⇒ 上面那条断言在自说自话
    # （"两边都空"也能让它绿，那正是判据被架空的形状）。
    probe = "search_notes"
    check("正控前置：改表之前它在菜单里", probe in visible_tools("admin"))
    saved = deepcopy(authz.TOOL_SCOPE)
    try:
        authz.TOOL_SCOPE[probe] = "scope.that.nobody.has"
        check("正控：把它的 scope 改成没人有的 ⇒ 菜单里消失（断言真的在读表）",
              probe not in visible_tools("admin"))
    finally:
        authz.TOOL_SCOPE.clear()
        authz.TOOL_SCOPE.update(saved)
    check("正控还原后 search_notes 回来（否则下面的用例都在被污染的表上跑）",
          "search_notes" in visible_tools("admin"))


def _sec_shape() -> None:
    """② 形状与技能级那份逐格对齐（对不齐 ⇒ A/B 比的就不是菜单内容）。"""
    print("\n② 形状 —— 与 planner_llm.build_tool_schema 逐格同形：")
    for role in ("user", "admin"):
        tools = build_tool_menu(role)
        skills = build_tool_schema(role)
        check(f"{role}: 顶层键都是 type/function",
              all(set(e) == {"type", "function"} for e in tools))
        check(f"{role}: 与技能级那份的 function 键集相同",
              {k for e in tools for k in _fn(e)} == {k for e in skills for k in _fn(e)},
              str({k for e in tools for k in _fn(e)}))
        check(f"{role}: 每个 function 都带 name/description/parameters",
              all(set(_fn(e)) == {"name", "description", "parameters"} for e in tools))
        check(f"{role}: parameters 是 object 且 additionalProperties=False",
              all(_fn(e)["parameters"]["type"] == "object"
                  and _fn(e)["parameters"]["additionalProperties"] is False for e in tools))
        check(f"{role}: 无必填的工具**不写** required 键（少一处网关差异）",
              all(("required" in _fn(e)["parameters"])
                  == bool(_fn(e)["parameters"].get("required")) for e in tools))

def _sec_slots() -> list[dict]:
    """③ 跨切面槽位在场（少了就是把"菜单形状"与"丢了两条通道"混成一个变量）。

    返回 `real`（不带三个伪函数的那批）——④ 接着用它。
    """
    print("\n③ 跨切面槽位 —— harness 资产照搬，不是模板资产：")
    tools = build_tool_menu("admin")
    real = [e for e in tools if _fn(e)["name"] not in PSEUDO]
    check("每个工具都带 intents 槽（意图清单的第二个出口）",
          all(INTENTS_ARG in _fn(e)["parameters"]["properties"] for e in real))
    check("每个工具都带 reply_kind 槽（短应答自我申报）",
          all(REPLY_KIND_ARG in _fn(e)["parameters"]["properties"] for e in real))
    check("intents / reply_kind **不进 required**（不填 = 与它们不存在时逐字节相同）",
          all(INTENTS_ARG not in _fn(e)["parameters"].get("required", [])
              and REPLY_KIND_ARG not in _fn(e)["parameters"].get("required", [])
              for e in real))
    check("三个伪函数在末尾，且都在",
          [ _fn(e)["name"] for e in tools[-3:] ] == list(PSEUDO),
          str([_fn(e)["name"] for e in tools[-3:]]))
    check("evidence 出厂关（缺省不出现）",
          all("evidence" not in _fn(e)["parameters"]["properties"] for e in real))
    ev = build_tool_menu("admin", evidence=True)
    ev_real = [e for e in ev if _fn(e)["name"] not in PSEUDO]
    check("evidence=True 时只挂在写工具上（射程由 authz.is_write 派生）",
          all(("evidence" in _fn(e)["parameters"]["properties"])
              == authz.is_write(_fn(e)["name"]) for e in ev_real))
    check("evidence=True 时至少有一个工具带它（否则上一条是空过）",
          any("evidence" in _fn(e)["parameters"]["properties"] for e in ev_real))
    return real


def _sec_descriptions(real: list[dict]) -> None:
    """④ 描述同源：人工说明仍住在 `graph._TOOL_MENU_LINES`，这里只读不抄。"""
    print("\n④ 描述 —— 人工说明仍住在 graph._TOOL_MENU_LINES，这里只读：")
    by_name = {t.name: t for t in get_all_tools()}
    overridden = [e for e in real if _fn(e)["name"] in _TOOL_MENU_LINES]
    check("覆盖面非空（否则下面那条是空过）", len(overridden) > 0, str(len(overridden)))
    check("被覆盖的工具：描述逐字等于 _TOOL_MENU_LINES 那条",
          all(_fn(e)["description"] == _TOOL_MENU_LINES[_fn(e)["name"]] for e in overridden))
    rest = [e for e in real if _fn(e)["name"] not in _TOOL_MENU_LINES]
    check("未覆盖的工具：回退注册表 docstring",
          all(_fn(e)["description"] == (by_name[_fn(e)["name"]].description or "")
              for e in rest))
    check("描述一个都不为空（空描述 = 模型只看得见名字）",
          all(_fn(e)["description"] for e in build_tool_menu("admin")))

def _sec_determinism() -> None:
    """⑤ 确定性（前缀缓存按内容寻址 ⇒ 菜单抖动会整片打掉命中率）。"""
    print("\n⑤ 确定性 —— 同一角色两次调用逐字节相同：")
    check("user 菜单两次相同", build_tool_menu("user") == build_tool_menu("user"))
    check("admin 菜单两次相同", build_tool_menu("admin") == build_tool_menu("admin"))


def test_tool_menu():
    FAILED.clear()
    _sec_role_filter()
    _sec_shape()
    _sec_descriptions(_sec_slots())
    _sec_determinism()

    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print("\n✅ 全部通过")


if __name__ == "__main__":
    test_tool_menu()
