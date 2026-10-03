# -*- coding: utf-8 -*-
"""登记表互锁：写能力那条链上的几处手工登记必须彼此一致（20261003）。

**为什么单起一套**：20260928 架构审计的主结论是——本仓最大的系统性风险不是某个判据
写错，而是**同一件事要在多处手工登记**（那次点出 94 处），漏一处**不报错**，只在线上
表现成"看着有这个能力，其实没有入口 / 没有契约 / 没有判据"。批 D 给 `account_set_role`
登记时要改四处（`_WRITE_NAME_FIELDS` / `_NAME_TARGET_TOOLS` / `WRITE_CLAIM_ROOTS` /
技能），同一类手工同步在写能力这条链上已经重复了十几次。

本套件**不改任何行为**，只把现在成立的几条互锁钉住：谁加了写能力却漏了某一处，这里
当场红，且红在哪一处写在提示里。既有的两条同步锁不重复（词根锁在
`test_write_done_claim.py` ③、同意话术锁在 `test_authz.py`），本套件补的是**没锁的
那几格**：

  ① `_TOOL_MAP` ↔ `TOOL_SCOPE` 双射：多一个 = 登记了不存在的工具名；少一个 = 裸工具
     没进作用域表 ⇒ 鉴权、写判据、弹卡三处都看不见它；
  ② 每件 write scope 工具都能被某个技能的计划调到（否则工具躺在柜子里、planner 没有
     入口 ⇒ 主人怎么问都到不了）；
  ③ 每个技能计划里引用的工具名都真实存在（改工具名漏改技能 = 运行期才炸）；
  ④ 每个技能都有非空 `complete_when` 与 `reply_contract`（回执驱动与声称判据的共同
     前提，空契约 ⇒ 这一族声称没有任何判据可依）；
  ⑤ `_NAME_TARGET_TOOLS ⊆ _WRITE_NAME_FIELDS`（名字目标工具必须登记目标字段，否则
     弹卡与写后复核拿不到"改的是谁"）；
  ⑥ `_ALWAYS_CONFIRM_TOOLS ⊆ write scope`（读类进了这张表 ⇒ 只读动作平白多一个弹窗）；
  ⑦ **例外账本**：几处已知留白逐条写在下面，双向断言。新增任何一条同类留白，要么改
     账本、要么改代码——**不许静默多出来**（让例外可见，正是"人工同步"那种失效的反面）。

用法：.venv/bin/python tests/test_registry_sync.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.authz as authz              # noqa: E402
from agent import graph as G             # noqa: E402
from agent import skills as S            # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _planned_tools() -> dict[str, list[str]]:
    """工具名 → 调得到它的技能名（计划里出现的都算，不区分先后）。"""
    out: dict[str, list[str]] = {}
    for sk in S.SKILLS:
        for step in sk.plan or ():
            if isinstance(step, (list, tuple)) and step and isinstance(step[0], str):
                out.setdefault(step[0], []).append(sk.name)
    return out


def _diff(a, b) -> list[str]:
    return sorted(set(a) - set(b))


# ══════════════════════════════════════════════════════════════════
print("\n① 工具柜 ↔ 作用域表：同一批工具，不许有孤儿、也不许有僵尸")

_live = set(G._TOOL_MAP)
_scoped = set(authz.TOOL_SCOPE)
check(f"柜里有 {len(_live)} 件工具，每件都登记了作用域",
      not _diff(_live, _scoped), "没登记：" + ", ".join(_diff(_live, _scoped)))
check("  作用域表里没有已不存在的工具（改名 / 删工具要一起收拾）",
      not _diff(_scoped, _live), "僵尸：" + ", ".join(_diff(_scoped, _live)))
check("非空（空集合会让上面两条假绿）", bool(_live) and bool(_scoped))

# ══════════════════════════════════════════════════════════════════
print("\n② 每件写工具都要有入口：某个技能的计划里出现它")

_write = {n for n, sc in authz.TOOL_SCOPE.items() if sc in authz.WRITE_SCOPES}
_planned = _planned_tools()
_unreachable = _diff(_write, _planned)
check(f"write scope {len(_write)} 件全部可达", not _unreachable,
      "没有技能能规划：" + ", ".join(_unreachable))
check("  反向对照：把写工具喂给空计划表，这条判据必须报得出人",
      _diff(_write, set()) == sorted(_write) and bool(_write))

# ══════════════════════════════════════════════════════════════════
print("\n③ 技能计划引用的工具名都真实存在（改工具名漏改技能 = 运行期才炸）")

_ghost = [n for n in _planned if n not in _live]
check(f"计划共引用 {len(_planned)} 个工具名，全部在柜里", not _ghost, "查无此工具：" + ", ".join(sorted(_ghost)))
check("  反向对照：假工具名必须被抓出来", "no_such_tool_xyz" not in _live)

# ══════════════════════════════════════════════════════════════════
print("\n④ 每个技能都有契约：complete_when 与 reply_contract 非空")

_empty_cw = [sk.name for sk in S.SKILLS if not (sk.complete_when or "").strip()]
_empty_rc = [sk.name for sk in S.SKILLS if not (sk.reply_contract or "").strip()]
check(f"{len(S.SKILLS)} 个技能都有 complete_when", not _empty_cw, "缺：" + ", ".join(_empty_cw))
check("  都有 reply_contract", not _empty_rc, "缺：" + ", ".join(_empty_rc))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 名字目标工具 ⊆ 登记了目标字段的工具")

_missing_field = _diff(G._NAME_TARGET_TOOLS, G._WRITE_NAME_FIELDS)
check(f"_NAME_TARGET_TOOLS {len(G._NAME_TARGET_TOOLS)} 件全部登记了字段",
      not _missing_field, "没登记目标字段：" + ", ".join(_missing_field))
check("  非空（否则这条假绿）", bool(G._NAME_TARGET_TOOLS))

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 一律弹卡的工具都必须是写类（读类进来 = 只读动作平白多一个弹窗）")

_not_write = _diff(authz._ALWAYS_CONFIRM_TOOLS, _write)
check(f"`_ALWAYS_CONFIRM_TOOLS` {len(authz._ALWAYS_CONFIRM_TOOLS)} 件都是 write scope",
      not _not_write, "不是写类：" + ", ".join(_not_write))

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 例外账本：已知留白逐条登记，双向断言（静默多一条就红）")

# 没有计划步骤的技能：`chat` 是自由作答那一档、`content_query` 走 calls 数组自带参数校验。
_NO_PLAN = {"chat", "content_query"}
# 没有 capability 一句话的技能（能力清单靠别处叙述）。
_NO_CAPABILITY = {"read_article"}
# 模板里写了、但由技能自己的代码算出来的占位符（**死代码**，见 `skill_param_specs` 注：
# navigate 的 path 由 NAV_MAP 映射结果填，planner 不填）。
_DEAD_TEMPLATE_TOKENS = {("navigate", "navigate_to", "path")}

_got_no_plan = {sk.name for sk in S.SKILLS if not sk.plan}
check("无计划的技能只有账本里那两件", _got_no_plan == _NO_PLAN,
      f"多出来：{sorted(_got_no_plan - _NO_PLAN)} / 少了：{sorted(_NO_PLAN - _got_no_plan)}")
_got_no_cap = {sk.name for sk in S.SKILLS if not (sk.capability or "").strip()}
check("无 capability 的技能只有账本里那一件", _got_no_cap == _NO_CAPABILITY,
      f"多出来：{sorted(_got_no_cap - _NO_CAPABILITY)} / 少了：{sorted(_NO_CAPABILITY - _got_no_cap)}")

_got_dead = {(sk.name, tn, arg)
             for sk in S.SKILLS
             for tn, tmpl in (sk.plan or ())
             for arg, val in tmpl.items()
             if isinstance(val, str) and val.startswith("$")
             and not (val[1:].split("[", 1)[0] in set(sk.inputs or {}))}
check("计划里指向未声明输入的占位符只有账本里那一处",
      _got_dead == _DEAD_TEMPLATE_TOKENS,
      f"多出来：{sorted(_got_dead - _DEAD_TEMPLATE_TOKENS)} / 少了：{sorted(_DEAD_TEMPLATE_TOKENS - _got_dead)}")

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 技能本身：名字唯一、角色档只有五档里有")

_roles = {"user", "zako", "secretary", "admin", "superadmin"}
_names = [sk.name for sk in S.SKILLS]
_dupes = sorted({n for n in _names if _names.count(n) > 1})
check(f"{len(_names)} 个技能名唯一", not _dupes, "重名：" + ", ".join(_dupes))
_bad_roles = sorted({r for sk in S.SKILLS for r in sk.roles} - _roles)
check("roles 只出现已知五档", not _bad_roles, "不认识的档：" + ", ".join(_bad_roles))
check("  非空（否则上面两条假绿）", bool(_names))

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"❌ {len(FAILS)} 条红：")
    for f in FAILS:
        print("   ·", f)
    sys.exit(1)
print("✅ 全绿")
