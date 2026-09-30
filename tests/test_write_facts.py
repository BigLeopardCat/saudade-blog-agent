# -*- coding: utf-8 -*-
"""写工具的事实信封（F1，20260930）：`tools.base.fact()` / `tgt()` / `is_noop()`。

背景（为什么要有这一批）：写工具此前各自 handcraft 一个 `meta={"op": …}` 字典，
「这一下到底动没动站内数据」这个事实只能靠"工具有没有**额外**写一个 `noop: True`"
反推——30 件写工具里只有 12 件写得出那张证书，剩下 18 件一旦走了"现状即目标"那条
短路，上层只能按"改了"处理。两个读端都因此说反话：

  · gate 的洞⑩（本轮**真**改了却说"什么都没动"）把一句**如实**的叙述判成谎；
  · planner 的零改动重复裁剪（trace `20260930T192824`：同一件 `add_favorite`
    连跑 4 轮）认不出零改动，白跑一轮。

F1 把 meta 收成一个**信封**（`fact()` 一处构造）：`changed` 是那个唯一判据（noop
由它派生，不手写），`target` 是"动的是谁"（闭集 kind），`before/after` 只放状态词，
`evidence` 是写后读回复核读到的那句。既有跨语言键**原样带着、一个都不改**。

本套件锁四件事（秒级、无网络无 LLM；由 run_all.py 自动收）：

  ① **判据本身**（纯函数）：`fact()` 只加不改 / `changed=False` ⇒ 自动补 `noop` /
     `changed` 是**必填 keyword-only**（漏填要当场 TypeError，不许静默 False）；
     `tgt()` 的 id 缺省**不填 0**；`is_noop()` 的三态（认 `changed` / 回落老键
     `noop` / 两样都没有 ⇒ 按"改了"处理）。
  ② **覆盖锁**：`authz.WRITE_SCOPES` 枚举出来的**每一件**写工具的源码闭包里
     必须出现 `fact(`、且 `ok(` 的次数 ≤ `fact(` 的次数（每个出口都带信封）。
     闭包 = 该工具的 `StructuredTool.func` 源码 + 它 `return _helper(` 直接委托的
     同模块私有函数（6 件是薄壳：`freeze_account` → `_set_account_frozen` 等）——
     只看壳的源码会把 6 件误判成未迁移。
  ③ **没有漏网的旧形状**：写工具的闭包里不许再出现 `meta={` 的字面 dict
     （`meta=fact(` 之外的第二条路就是"人工同步"长出来的地方）；`tgt("…")` 的
     kind 必须全在 `TGT_KINDS` 里。
  ④ **跨语言契约**：`changed`/`target`/`evidence`/`noop` **不在** `_RCPT_META_KEYS`
     里（进去才需要同步 Rust：`src/routes/chat.rs::render_exec_row` 会开始读一个
     它没排版的键）。F1 刻意保持 Rust 零改动。

用法：.venv/bin/python tests/test_write_facts.py（或 tests/run_all.py 统一跑）
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.base as base  # noqa: E402
from agent import authz  # noqa: E402
from agent.graph import _RCPT_META_KEYS  # noqa: E402
from tools import get_all_tools  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# F1 只加的三个键 + 派生的 noop：它们**不进**跨语言白名单。
ENVELOPE_ONLY = ("changed", "target", "evidence", "noop")

# ══════════════════════════════════════════════════════════════════
print("\n① 信封的语义（纯函数，不起服务）")
f_min = base.fact("tag_create", changed=True)
check("changed=True ⇒ 只有 op/changed 两个键",
      sorted(f_min) == ["changed", "op"], str(sorted(f_min)))
check("  changed 一定是 bool（传进去的可以是任何真值）",
      f_min["changed"] is True and base.fact("x", changed=1)["changed"] is True)

f_noop = base.fact("tag_reuse", changed=False)
check("changed=False ⇒ 自动补 noop=True（一处派生，工具侧不许手写）",
      f_noop.get("noop") is True, str(f_noop))
check("changed=True ⇒ **没有** noop 这个键（不是 noop=False）",
      "noop" not in f_min)

f_full = base.fact("tag_update", changed=True, target=base.tgt("tag", 12, "随笔"),
                   before="旧名", after="新名", evidence="新名", tag_id=12, change="改名为 新名")
check("target/before/after/evidence 都在形状里",
      f_full["target"] == {"kind": "tag", "id": 12, "name": "随笔"}
      and f_full["before"] == "旧名" and f_full["after"] == "新名"
      and f_full["evidence"] == "新名", str(f_full))
check("既有跨语言键**原样带着**（fact 只加不改）",
      f_full["tag_id"] == 12 and f_full["change"] == "改名为 新名" and f_full["op"] == "tag_update")
check("空串/空 target 不塞空键（信封里没有「空值占位」这种东西）",
      "before" not in base.fact("x", changed=True, before="")
      and "target" not in base.fact("x", changed=True, target=None))

sig = inspect.signature(base.fact)
p_changed = sig.parameters["changed"]
check("changed 是**必填 keyword-only**（漏填当场 TypeError，不许静默 False）",
      p_changed.kind is inspect.Parameter.KEYWORD_ONLY
      and p_changed.default is inspect.Parameter.empty, str(p_changed))
try:
    base.fact("x")  # type: ignore[call-arg]
    _raised = ""
except TypeError:
    _raised = "TypeError"
check("  确实抛得出来", _raised == "TypeError", _raised)

check("tgt() 的 kind 落在闭集里（读端要按 kind 分派，自由字符串就是又一次人工同步）",
      base.tgt("tag", 1)["kind"] == "tag" and "tag" in base.TGT_KINDS
      and "quota_request" in base.TGT_KINDS and "board_comment" in base.TGT_KINDS)
check("id 拿不到时**不填 0**（0 在号段里是合法值，会被读成某个真的物件）",
      "id" not in base.tgt("todo", None, "买牛奶")
      and base.tgt("todo", None, "买牛奶") == {"kind": "todo", "name": "买牛奶"})

# ── is_noop：两个读端唯一的事实入口
check("is_noop：changed=False ⇒ True", base.is_noop(f_noop) is True)
check("is_noop：changed=True ⇒ False", base.is_noop(f_min) is False)
check("is_noop：老键 noop=True（F1 之前写的证书）⇒ True",
      base.is_noop({"op": "tag_reuse", "noop": True}) is True)
check("is_noop：两样都没有 ⇒ **False**（认不出按「改了」处理，绝不错判诚实的叙述）",
      base.is_noop({"op": "tag_create"}) is False)
check("is_noop：meta 不是 dict（None / 空）⇒ False，不抛异常",
      base.is_noop(None) is False and base.is_noop({}) is False and base.is_noop("x") is False)
check("is_noop：changed 是 bool 之外的脏值时回落老键",
      base.is_noop({"changed": "false", "noop": True}) is True)


# ══════════════════════════════════════════════════════════════════
print("\n② 覆盖锁：每一件写工具的每个出口都带信封")


def _delegates(src: str) -> set[str]:
    """源码里直接委托出去的同模块私有函数名（`return _helper(` / `_helper(`）。"""
    return set(re.findall(r"\b(_[A-Za-z_][A-Za-z0-9_]*)\(", src))


def closure_src(fn, depth: int = 3, seen: set | None = None) -> str:
    """工具的**源码闭包**：自己的源码 + 它委托的同模块私有函数的源码（递归，封顶 3 层）。

    ⚠️ 必须走 `StructuredTool.func` 取底层函数：`tools.base.<name>` 与
    `get_all_tools()` 里的都是 `StructuredTool` 对象，`getattr(base, name)` 拿到的
    仍是同一个对象（`@tool` 装饰器换掉了模块属性）⇒ 拿它去 `inspect.getsource`
    当场 `TypeError: module, class, method, function… expected`。"""
    seen = seen if seen is not None else set()
    if depth <= 0 or fn in seen:
        return ""
    seen.add(fn)
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):          # pragma: no cover - 取不到源码时按未覆盖处理
        return ""
    if re.search(r"\bfact\(", src):
        return src
    out = src
    for name in _delegates(src):
        helper = getattr(base, name, None)
        helper = getattr(helper, "func", helper)
        if not callable(helper) or getattr(helper, "__module__", "") != "tools.base":
            continue
        # 只跟"同模块的私有函数"：`ok()` 也是私有名但它是出口、没有下游
        if name in ("ok", "empty", "unavailable", "not_found"):
            continue
        out += "\n" + closure_src(helper, depth - 1, seen)
    return out


write_tools = [t for t in get_all_tools()
               if authz.required_scope(t.name) in authz.WRITE_SCOPES]
check("写工具枚举非空且来自 authz.WRITE_SCOPES（不是手写名单）",
      len(write_tools) >= 25, f"{len(write_tools)} 件")

no_fact, ok_gt_fact, old_shape, bad_kind = [], [], [], []
kinds_used: set[str] = set()
for t in write_tools:
    src = closure_src(getattr(t, "func", None))
    if not re.search(r"\bfact\(", src):
        no_fact.append(t.name)
    if len(re.findall(r"\bok\(", src)) > len(re.findall(r"\bfact\(", src)):
        ok_gt_fact.append(t.name)
    if "meta={" in src:
        old_shape.append(t.name)
    for k in re.findall(r'tgt\(\s*"([a-z_]+)"', src):
        kinds_used.add(k)
        if k not in base.TGT_KINDS:
            bad_kind.append(f"{t.name}:{k}")

check("每件写工具的源码闭包里都有 fact((不是只迁了一半)", not no_fact, str(no_fact))
check("ok( 的次数 ≤ fact( 的次数（每个出口都带信封，没有裸出口）",
      not ok_gt_fact, str(ok_gt_fact))
check("闭包里没有残留 `meta={` 的字面 dict（旧形状的第二条路）",
      not old_shape, str(old_shape))
check("tgt() 用到的 kind 全在 TGT_KINDS 里", not bad_kind, str(bad_kind))
check("  kind 覆盖面确实铺开了（不是三两个词凑数）",
      len(kinds_used) >= 8, str(sorted(kinds_used)))

# ══════════════════════════════════════════════════════════════════
print("\n③ 跨语言契约：F1 只在 Python 侧，Rust 零改动")
for k in ENVELOPE_ONLY:
    check(f"  `{k}` **不在** _RCPT_META_KEYS 里（进去就得同步 Rust）",
          k not in _RCPT_META_KEYS)
check("  既有键照旧在白名单里（op/before/after 的收窄就是跨语言破坏）",
      all(k in _RCPT_META_KEYS for k in ("op", "before", "after")))
check("  实体键一个都没被顺手删掉（下一轮「你刚冻的是谁」靠它）",
      all(k in _RCPT_META_KEYS
          for k in ("article_id", "tag_id", "tag_name", "category_id", "category_name",
                    "announcement_id", "announcement_title", "board_id", "board_author",
                    "account_id", "account_name", "change", "level")))

print()
if FAILED:
    print(f"{len(FAILED)} 条未通过：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
