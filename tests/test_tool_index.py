# -*- coding: utf-8 -*-
"""技能索引档（`build_tool_schema(..., index=True)`）的判据：离线、零网络零 LLM。

**为什么单起一套**：索引档要动的正是"模型看到的菜单"，而它**缺省关着**——所以它的
失效方式不是"跑起来报错"，是**悄悄与缺省档分叉**：取回的原文少了一段、索引行与原文
对不上、`describe_skill` 越权返回了看不见的技能。这三种都不会让别的套件红。

判据分四组：
  ① **缺省档没被碰**——`index=False`（以及不传）时，schema 里没有 `describe_skill`，
     每一格的 description 就是 `tool_index.skill_description` 的原文；
  ② **只换 `description` 那一格**——索引档与缺省档剥掉 description 后**逐字节相同**
     （函数名 / type / enum / required / 嵌套 schema 一个字符都不许动）；
  ③ **取回的原文 == 索引化前模型看到的东西**——这一条是整次改造的**承重点**：
     `describe_skill(name)` 的返回必须**逐字等于**缺省档里那一格 description；
  ④ **索引行本身**——非空、互不相同、是原文的**前缀**（只切不写）。

`index_line` 的机械规则（区间内找最靠前的句读，找不到就硬截）**不在这里重复写一遍**：
重写规则等于把判据变成"实现抄了自己"，所以这里只判**性质**（前缀性/非空/唯一/长度），
不判"切在第几个字符"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from agent import planner_llm as N  # noqa: E402
from agent import tool_index as I  # noqa: E402
from agent.workflow_defs import visible_workflows  # noqa: E402

FAILED: list[str] = []

ROLES = (None, "user", "admin")


check = collector(FAILED)


def _strip(node):
    """递归删掉**所有** `description` 键——剩下的就是"结构"。"""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip(x) for x in node]
    return node


def _by_name(tools: list[dict]) -> dict[str, dict]:
    return {t["function"]["name"]: t["function"] for t in tools}


# ── ① 缺省档没被碰 ─────────────────────────────────────────────────────────
def test_default_path_is_untouched():
    FAILED.clear()
    print("\n[① 缺省档] 不传 / index=False 都不含取回口，且描述就是原文")
    for role in ROLES:
        plain = _by_name(N.build_tool_schema(role))
        explicit = _by_name(N.build_tool_schema(role, index=False))
        check(f"role={role!r} 不传与 index=False 逐字节相同",
              json.dumps(plain, ensure_ascii=False, sort_keys=True)
              == json.dumps(explicit, ensure_ascii=False, sort_keys=True))
        check(f"role={role!r} 缺省档里没有 {I.DESCRIBE_SKILL}",
              I.DESCRIBE_SKILL not in plain, str(sorted(plain)[:3]))
        # 缺省档的每一格 description **逐字**等于 `skill_description`：这条钉住
        # "把展开那两行搬进 tool_index 时没有顺手改文本"（搬错了 ③ 就恒真了）。
        bad = [w.name for w in visible_workflows(role)
               if plain[w.name]["description"] != I.skill_description(w, role)]
        check(f"role={role!r} 缺省档 description == skill_description（原文未改）",
              not bad, "、".join(bad[:3]))
    assert not FAILED, "；".join(FAILED)


# ── ② 只换 description 那一格 ──────────────────────────────────────────────
def test_structure_is_byte_identical():
    FAILED.clear()
    print("\n[② 结构] 索引档剥掉 description 后与缺省档逐字节相同")
    for role in ROLES:
        full = [t for t in N.build_tool_schema(role)]
        idx = [t for t in N.build_tool_schema(role, index=True)
               if t["function"]["name"] != I.DESCRIBE_SKILL]
        a = json.dumps(_strip(full), ensure_ascii=False, sort_keys=True)
        b = json.dumps(_strip(idx), ensure_ascii=False, sort_keys=True)
        check(f"role={role!r} 结构逐字节相同（名字/type/enum/required/嵌套 schema）",
              a == b, f"len {len(a)} vs {len(b)}")
        # 函数**数量**也要相等：多一格少一格（比如顺手删了个技能）在剥 description
        # 之后未必看得出来——上面那条已经覆盖，这里给一个读起来更直白的读数。
        check(f"role={role!r} 技能数不变", len(full) == len(idx),
              f"{len(full)} vs {len(idx)}")
    assert not FAILED, "；".join(FAILED)


# ── ③ 取回的原文 == 索引化前那一格 ─────────────────────────────────────────
def test_describe_skill_returns_the_original():
    FAILED.clear()
    print("\n[③ 承重点] describe_skill 的返回逐字等于缺省档的 description")
    for role in ROLES:
        plain = _by_name(N.build_tool_schema(role))
        for w in visible_workflows(role):
            got = I.describe_skill_text(w.name, role)
            if got != plain[w.name]["description"]:
                check(f"role={role!r} {w.name} 取回原文", False,
                      f"{len(got or '')} vs {len(plain[w.name]['description'])}")
                break
        else:
            check(f"role={role!r} {len(plain) - 3} 条技能全部取回原文", True)
    # 索引行**必须是**取回原文的前缀：不满足时，索引里提了一件事而原文里没有
    # （模型照索引做决定、取回后发现对不上）。
    bad = []
    for role in ROLES:
        for w in visible_workflows(role):
            line = I.index_line(I.skill_description(w, role))
            if not I.skill_description(w, role).startswith(line):
                bad.append(w.name)
    check("索引行恒为原文前缀（只切不写）", not bad, "、".join(sorted(set(bad))[:3]))
    assert not FAILED, "；".join(FAILED)


# ── ④ 索引行本身 + 不扩权 ─────────────────────────────────────────────────
def test_index_lines_and_no_privilege_escalation():
    FAILED.clear()
    print("\n[④ 索引行 / 不扩权]")
    for role in ROLES:
        ws = visible_workflows(role)
        lines = {w.name: I.index_line(I.skill_description(w, role)) for w in ws}
        empty = [n for n, line in lines.items() if not line.strip()]
        check(f"role={role!r} 索引行非空", not empty, "、".join(empty[:3]))
        vals = list(lines.values())
        dup = {line for line in vals if vals.count(line) > 1}
        check(f"role={role!r} 索引行互不相同（菜单还有区分度）", not dup,
              str(sorted(dup)[:2]))
        over = [n for n, line in lines.items() if len(line) > I._MAX_LEN]
        check(f"role={role!r} 索引行不超过 {I._MAX_LEN} 字符", not over,
              "、".join(over[:3]))
    # 取回口**不扩权**：够不着的名字一律 None，且 admin 专属技能对公开身份取不回。
    check("不存在的名字取不回", I.describe_skill_text("no_such_skill_xyz", "admin") is None)
    check("空名字取不回", I.describe_skill_text("", "admin") is None
          and I.describe_skill_text(None, "admin") is None)
    admin_only = ({w.name for w in visible_workflows("admin")}
                  - {w.name for w in visible_workflows(None)})
    leaked = [n for n in sorted(admin_only)
              if I.describe_skill_text(n, None) is not None]
    check("admin 专属技能对公开身份取不回（不扩权）", not leaked, "、".join(leaked[:3]))
    # 取回口排在**最后**：它是对上面那份菜单的注释，不是菜单里的一项。
    idx = N.build_tool_schema("admin", index=True)
    check(f"{I.DESCRIBE_SKILL} 排在数组最后",
          idx[-1]["function"]["name"] == I.DESCRIBE_SKILL,
          idx[-1]["function"]["name"])
    check(f"{I.DESCRIBE_SKILL} 不与任何技能名相撞（同一命名空间）",
          I.DESCRIBE_SKILL not in {w.name for w in visible_workflows("admin")})
    assert not FAILED, "；".join(FAILED)


# ── ⑤ 体积棘轮（这一档存在的理由就是省字符） ───────────────────────────────
def test_size_ratchet():
    FAILED.clear()
    """索引档必须真的小一截，否则 `index_line` 被改成了恒等函数。

    棘轮钉在**描述文本**上（`索引合计 ≤ 原文的 25%`）而不是整份 schema 的比例：整份的
    比例随角色漂得厉害（admin 78.1% / user 89.4%——小菜单里结构占比本来就高），
    而"索引该有多短"是 `index_line` 自己的性质，与角色无关。实测 12.5%（admin）/
    14.9%（user），留了一倍余量。

    放宽这两个数是**文本编辑**，不是跑 `--update`：它红了应当先问"索引是不是切得太松
    （或太狠、切掉了承重信息）"，而不是把线往下挪。
    """
    print("\n[⑤ 棘轮] 索引合计 ≤ 描述原文的 25%，且整份 schema 严格更小")
    for role in ROLES:
        ws = visible_workflows(role)
        full_d = sum(len(I.skill_description(w, role)) for w in ws)
        idx_d = sum(len(I.index_line(I.skill_description(w, role))) for w in ws)
        check(f"role={role!r} 索引/描述 = {100 * idx_d / full_d:.1f}% ≤ 25%",
              idx_d <= full_d * 0.25, f"索引 {idx_d} / 原文 {full_d}（上限 {int(full_d * 0.25)}）")
        a = len(json.dumps(N.build_tool_schema(role), ensure_ascii=False))
        b = len(json.dumps(N.build_tool_schema(role, index=True), ensure_ascii=False))
        check(f"role={role!r} 整份 schema 严格更小（{b}/{a} = {100 * b / a:.1f}%）",
              b < a, f"{b} vs {a}")
    assert not FAILED, "；".join(FAILED)


if __name__ == "__main__":
    for fn in (test_default_path_is_untouched,
               test_structure_is_byte_identical,
               test_describe_skill_returns_the_original,
               test_index_lines_and_no_privilege_escalation,
               test_size_ratchet):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILED else f"失败 {len(FAILED)} 项 ❌: {FAILED}"))
    sys.exit(1 if FAILED else 0)
