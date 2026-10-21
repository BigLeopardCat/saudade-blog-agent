# -*- coding: utf-8 -*-
"""能力索引（`agent/capability_index.py`）的判据：离线、零网络零 LLM。

**为什么单起一套**：索引是 planner 菜单与执行层工具面的**共同上游**，而它的失效方式
全是静默的——索引行改写了原文（模型看到的东西与注册表不一致）、工具面漏了一件
（B 族的 `checker 验收回执缺少工具 X` 就是从这儿来的）、射程把写族圈了进去
（写族要过 9 个参数校正器，圈进来等于绕过它们）。这三种都不会让别的套件红。

判据分六组：
  ① **派生**：索引里出现的名字集合 == `visible_workflows(role)` 的名字集合（**逐字**，
     顺序也要对）；工具面 == `plan` ∪ `extra_plan_tools`（与写名单派生**同一条算法**）。
  ② **只切不写**：索引行是原文的**前缀**、非空、≤ 80 显示列。
  ③ **承重格在场**：`index_entry` 里有工具名（B 族）、`capability_brief` 里有
     `complete_when` 与 `reply_contract`（C 族）。
  ④ **射程**：`react_scoped` 与 `WRITE_WORKFLOW_NAMES` 互补；`chat` 恒 False。
  ⑤ **不扩权**：够不着的能力名一律 `None`。
  ⑥ **执行方式**：`kind` 与 `react_scoped` **同源**（`skill` ⟺ ReAct），注册表外的
     名字响亮抛、不回落成 `tool`。

**反控写在套件里**：① 的工具面判据同时验"单工具能力**不**含别的工具"（否则一句
常量 `""` 也能过）；⑤ 拿 `read_article`（系统专用，不在任何角色菜单里）当反控。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from agent import capability_index as CI  # noqa: E402
from agent import workflow_defs as W  # noqa: E402
from agent.tool_index import skill_description  # noqa: E402

FAILED: list[str] = []

ROLES = (None, "user", "admin")


check = collector(FAILED)


def _plan_union(wf) -> tuple[str, ...]:
    """判据侧**独立算一遍**工具面（不调被测函数——调它等于抄一遍实现）。"""
    return tuple(dict.fromkeys([n for n, _ in wf.plan] + list(wf.extra_plan_tools)))


# ── ① 派生 ────────────────────────────────────────────────────────────────
def test_index_is_derived():
    for role in ROLES:
        caps = CI.capabilities_for(role)
        want = [w.name for w in W.visible_workflows(role)]
        check(f"索引名字与可见技能逐字相等（role={role}）",
              [c.name for c in caps] == want,
              f"{len(caps)} vs {len(want)}")
        check(f"索引非空（role={role}）", bool(caps))
        for cap in caps:
            wf = W.WORKFLOW_MAP[cap.name]
            if cap.dynamic:
                # `content_query` 那一条：工具面按角色现算，判据是"它就是白名单并集"
                check(f"{cap.name} 的 dynamic 标记与 plan 空且非 chat 同真",
                      (not wf.plan) and (not wf.chat))
                check(f"{cap.name} 的工具面非空（role={role}）", bool(cap.tools))
                continue
            check(f"{cap.name} 的工具面 == plan ∪ extra_plan_tools（role={role}）",
                  cap.tools == _plan_union(wf),
                  f"{cap.tools} vs {_plan_union(wf)}")

    # 反控：一句常量（比如恒返回全部工具名）过不了上面那条——挑一条**单工具**能力，
    # 判它**不**含同族的另一件工具。
    nav = CI.capability_of("navigate", "user")
    check("反控：navigate 的工具面只有 navigate_to（不含 toggle_effect）",
          nav is not None and nav.tools == ("navigate_to",), str(nav and nav.tools))
    eff = CI.capability_of("effect", "user")
    check("反控：effect 的工具面只有 toggle_effect",
          eff is not None and eff.tools == ("toggle_effect",), str(eff and eff.tools))


# ── ② 只切不写 ────────────────────────────────────────────────────────────
def test_index_line_is_a_prefix():
    for role in ROLES:
        for cap in CI.capabilities_for(role):
            src = skill_description(W.WORKFLOW_MAP[cap.name], role)
            check(f"{cap.name} 的索引行是原文第一行的前缀（role={role}）",
                  src.split("\n", 1)[0].startswith(cap.line), cap.line[:40])
            check(f"{cap.name} 的索引行非空（role={role}）", bool(cap.line.strip()))
            check(f"{cap.name} 的索引行 ≤ 80 列（role={role}）",
                  len(cap.line) <= 80, str(len(cap.line)))


# ── ③ 承重格在场 ──────────────────────────────────────────────────────────
def test_carrying_fields_present():
    """B 族与 C 族的修复点各有一条判据——**没有它们，索引就只是一张更短的菜单**。"""
    for role in ROLES:
        for cap in CI.capabilities_for(role):
            entry = CI.index_entry(cap)
            brief = CI.capability_brief(cap)
            check(f"{cap.name} 的菜单项含一行索引（role={role}）", cap.line in entry)
            if not cap.dynamic:
                for t in cap.tools:
                    check(f"{cap.name} 的菜单项点名工具 {t}（role={role}）", t in entry)
            if cap.complete_when:
                check(f"{cap.name} 的菜单项含完成判定（role={role}）",
                      f"完成判定：{cap.complete_when}" in entry)
                check(f"{cap.name} 的执行层简报含完成判定（role={role}）",
                      f"完成判定：{cap.complete_when}" in brief)
            if cap.reply_contract:
                check(f"{cap.name} 的执行层简报含回复契约（role={role}）",
                      f"回复契约：{cap.reply_contract}" in brief)
            check(f"{cap.name} 的执行层简报点名能力名（role={role}）",
                  cap.name in brief)

    # 反控：把"承重格在场"与"简报是常量"区分开——两份简报必须**互不相同**。
    caps = CI.capabilities_for("admin")
    briefs = {CI.capability_brief(c) for c in caps}
    check("反控：执行层简报逐条互不相同（不是一句常量）", len(briefs) == len(caps),
          f"{len(briefs)} vs {len(caps)}")


# ── ④ 射程 ────────────────────────────────────────────────────────────────
def test_scope():
    for name in sorted(W.WRITE_WORKFLOW_NAMES):
        check(f"写族走模板：{name}", CI.react_scoped(name) is False)
    check("chat 走模板（它零工具，靠 run 的纯应答分支留痕）",
          CI.react_scoped("chat") is False)
    for name in ("navigate", "effect", "darkmode", "device_display", "device_query",
                 "content_query", "ops_report"):
        check(f"射程内：{name}", CI.react_scoped(name) is True)
    # 射程与写名单**互补**：可见技能里，非写非 chat 的全部在射程内。
    for wf in W.visible_workflows("admin"):
        want = wf.name not in W.WRITE_WORKFLOW_NAMES and not wf.chat
        check(f"射程与写名单互补：{wf.name}", CI.react_scoped(wf.name) is want)


# ── ⑤ 不扩权 ──────────────────────────────────────────────────────────────
def test_no_privilege_growth():
    for role in ROLES:
        visible = {w.name for w in W.visible_workflows(role)}
        for wf in W.WORKFLOWS:
            cap = CI.capability_of(wf.name, role)
            check(f"够不着的名字一律 None：{wf.name}（role={role}）",
                  (cap is not None) is (wf.name in visible))
    for junk in ("", "  ", "read_article", "no_such_skill", "chat "):
        check(f"反控：{junk!r} 取不到（或按可见性如实返回）",
              CI.capability_of(junk, "admin") is None or junk.strip() == "chat")
    check("反控：系统专用技能 read_article 不在任何角色的索引里",
          CI.capability_of("read_article", "admin") is None)


# ── ⑥ 执行方式（`kind`）────────────────────────────────────────────────────
def test_kind_matches_execution_mode():
    """`kind` 决定"路由到它之后执行层怎么跑它"，所以它必须**与射程判据同源**。

    判据取**派生关系**，不取"今天有几条"：条数是会变的，关系不该变。
    三条：
      · `kind == "skill"` ⟺ `react_scoped`（ReAct 那一档）；
      · `kind == "workflow"` ⟺ **不是** `react_scoped`（确定性快道那一档）；
      · 注册表里**没有**任何条目落在 `tool`——那不是"没实现"，是事实：70 件工具里
        没有技能包裹的 21 件全是 `content_query` 那条 `dynamic` 能力的工具面，
        它们已经有家。反控：拿一个注册表外的名字调 `capability_kind` ⇒ **响亮抛**，
        不许回落成 `tool`（回落会把"技能名打错一个字"静默变成"这是件裸工具"）。
    """
    print("\n⑥ 执行方式 `kind`：与射程判据同源")
    seen: set[str] = set()
    for role in ROLES:
        for cap in CI.capabilities_for(role):
            seen.add(cap.kind)
            check(f"`kind` 与 `react_scoped` 一致：{cap.name}（role={role}）",
                  (cap.kind == "skill") is cap.react, f"{cap.kind} / react={cap.react}")
    check("三档取值里 `workflow` 与 `skill` 都有实例（否则判的是空气）",
          {"workflow", "skill"} <= seen, str(sorted(seen)))
    check("`tool` 今天一条都没有——裸工具已有家（`content_query` 的 dynamic 面）",
          "tool" not in seen, str(sorted(seen)))
    for junk in ("", "  ", "no_such_skill", "list_notes"):
        try:
            got = CI.capability_kind(junk)
        except KeyError:
            got = "<抛>"
        check(f"反控：注册表外的名字 {junk!r} 响亮抛，不回落成 `tool`",
              got == "<抛>", str(got))

    # 反控二：把一条技能的 `kind` 手工翻面 ⇒ 上面那条等价式**必须**判红。
    # （真的把那个表达式再算一遍——不是"看着像能红"。）
    _nav = CI.capability_of("navigate", "admin")
    check("反控的前提：navigate 在索引里（否则下面那条判的是空气）", _nav is not None)
    if _nav is not None:
        _flip = replace(_nav, kind="workflow")
        _flipped_ok = bool((_flip.kind == "skill") is _flip.react)
        check("反控：手工翻面后等价式判红（判据不是恒真）", not _flipped_ok,
              f"{_flip.kind} / react={_flip.react}")


def test_capability_index():
    """收集器入口（`tests/test_tests_dir_shape.py` 要一个顶层 `def test_*`）。"""
    FAILED.clear()
    test_index_is_derived()
    test_index_line_is_a_prefix()
    test_carrying_fields_present()
    test_scope()
    test_no_privilege_growth()
    test_kind_matches_execution_mode()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)


if __name__ == "__main__":
    test_capability_index()
    print("\n✅ 全部通过")
