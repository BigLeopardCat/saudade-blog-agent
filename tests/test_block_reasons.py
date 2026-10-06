# -*- coding: utf-8 -*-
"""受阻原因码的**类型**单测：纯查表 + 纯渲染 + 源码接线，零网络、零 LLM。

**这一件为什么存在**：`_check_spec` 判 BLOCK 给出的原因码，此前只有两个去处——trace
与 reflector 的提示词。**planner 拿不到**，只能从错误帧那句话里猜"刚才发生了什么"，
而"服务这一轮给不出数据"与"你参数写错了"在帧文本里长得一模一样 ⇒ planner 把
`unavailable` 当成参数错、原地重点一次同一个调用 ⇒ 同键二次受阻 ⇒ `wrap_up` ⇒
主人那件**完全能办**的事整条没有入口（现场与读数：`docs/问题记录.md` §1.55；
两版提示词纠偏被 A/B 否掉的负结果：那里的 1b 与 `docs/adr/adr-0007…md`）。

被测五块：
  · ① `agent/block_reasons.py` —— 表本身：形状、"不可重试"那一族**就是那七个**
    （加码要有人复核）、以及 fail-safe（未登记的码按可重试处理）；
  · ② 与 `server._REASON_CN` 的关系 —— **键集合相等 + 逐值相等**。两张表分居两侧
    （server 贴过程行、本表贴喂给 planner 的类型），漏登记一个码不该是静默的；
  · ③ `_check_spec` 的每个 BLOCK 出口都在表里 —— 源码级总括（含那条 `or` 链末端的
    `error_frame`）+ 行为级抽查（真的调一次，看返回的码进不进表）；
  · ④ `agent/context.py::blocked_rows` —— 渲染形状：技能名/原因中文（**不裸英文码**）/
    "能不能改参重试"两族措辞/无受阻项时渲染空串（整块只在有受阻项时才付）；
  · ⑤ 接线 —— 模板有槽、渲染函数有形参、`planner_node` 每轮都传、`execute` 往受阻项
    里写了 `skill`。

为什么主断言落在**表与形状**上而不是"提示词里有没有那句话"：这一件的失败面是"类型
没接上线、planner 又得靠猜"。断一句具体措辞是假绿——换一句话就过（20261007 两次
A/B 的教训正是"改措辞救不回来"）。

用法：.venv/bin/python tests/test_block_reasons.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.context as ctx  # noqa: E402
import agent.graph as g  # noqa: E402
import server as srv  # noqa: E402
from agent.block_reasons import REASONS, block_reason_type  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_GRAPH_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")


# ── ① 类型表本身 ────────────────────────────────────────────────────────────
# "不可重试"这一族是**刻意**列出来的：每多一个 False，就等于对 planner 说"这一条
# 重试没用、换路或如实说"。多写一个的代价是掐掉一条合法重试，所以这个集合要有人复核
# （这条断言就是这个作用——与 planner 模板占位符集合那条同一个纪律）。
_NON_RETRY = {"unavailable", "cmd_shape", "policy_refused", "consent_required",
              "denied", "no_manifest", "unknown_role"}
check("表非空，每项形如 (非空中文, bool)",
      bool(REASONS) and all(isinstance(v, tuple) and len(v) == 2 and v[0]
                            and not v[0].isascii() and isinstance(v[1], bool)
                            for v in REASONS.values()))
check("「改参数重试无效」那一族就是这七个（多一个少一个都要有人复核）",
      {k for k, v in REASONS.items() if not v[1]} == _NON_RETRY,
      str(sorted(k for k, v in REASONS.items() if not v[1])))
check("unavailable 与 target_not_found 是**两族**（一个重试无效、一个换 id 可救）",
      block_reason_type("unavailable")[1] is False
      and block_reason_type("target_not_found")[1] is True)
check("fail-safe：未登记的码 → 可重试 + 中文退回码本身（行为不变，不误判成不可重试）",
      block_reason_type("brand_new_code") == ("brand_new_code", True),
      str(block_reason_type("brand_new_code")))
check("空值/None 不炸，且仍按可重试兜底",
      block_reason_type(None)[1] is True and bool(block_reason_type(None)[0])
      and block_reason_type("")[1] is True)


# ── ② 与 server._REASON_CN 的关系：两张表、一份清单 ─────────────────────────
# 键集合相等是**总括锁**：任何一侧加了码而另一侧没加，这条立刻红。逐值相等让两句话术
# 不会各说各的（同一件事在 planner 眼里叫「服务不可用」、在访客眼里也得是）。
check("键集合与 server._REASON_CN **相等**（一侧加码，另一侧必须同改）",
      set(REASONS) == set(srv._REASON_CN),
      f"仅本表={sorted(set(REASONS) - set(srv._REASON_CN))} "
      f"仅 server={sorted(set(srv._REASON_CN) - set(REASONS))}")
check("逐值相等（同一个码在两侧是同一句中文）",
      all(REASONS[k][0] == srv._REASON_CN[k] for k in REASONS if k in srv._REASON_CN),
      str([(k, REASONS[k][0], srv._REASON_CN.get(k)) for k in REASONS
           if k in srv._REASON_CN and REASONS[k][0] != srv._REASON_CN[k]]))


# ── ③ `_check_spec` 的每个 BLOCK 出口都在表里 ───────────────────────────────
_BODY = _GRAPH_SRC.split("def _check_spec(", 1)[1].split("\ndef ", 1)[0]
_LITS = set(re.findall(r'_VERDICT_BLOCK, "([a-z_]+)"', _BODY))
check("源码级：`_check_spec` 里 `_VERDICT_BLOCK, \"…\"` 形状的字面码全都登记了",
      bool(_LITS) and _LITS <= set(REASONS), str(sorted(_LITS)))
# 那条 or 链（ref / scope / consent / target / policy 五族）不是上面那个形状，
# 末端兜底的 `error_frame` 单独锁一句——它是最容易被漏掉的那个码。
check("源码级：or 链末端的 error_frame 也登记了",
      'or "error_frame"' in _BODY and "error_frame" in REASONS)
check("源码级：五个取回通道都还接在 `_check_spec` 上（摘一个就少一族码）",
      all(fn in _BODY for fn in
          ("ref_error_reason", "scope_error_reason", "consent_error_reason",
           "target_error_reason", "policy_error_reason")))
# 行为级抽查：不走源码文本，真的调一次。取的是 or 链上的一族（权限不足帧），
# "从源码里看见字面"与"真的从帧里取回这个码"是两件事。
_r = g._check_spec("read_notifications", {}, True,
                   "__ERROR__: 权限不足[denied] —— 普通用户 无权调用 read_notifications",
                   "notice_read")
check("行为级：权限不足帧 → (BLOCK, denied)，且 denied 在表里",
      _r == ("BLOCK", "denied") and "denied" in REASONS, str(_r))
_r2 = g._check_spec("read_notifications", {}, True, "…", "notice_read", kind="unavailable")
check("行为级：kind=unavailable → 表里标的是不可重试",
      _r2 == ("BLOCK", "unavailable") and block_reason_type(_r2[1])[1] is False, str(_r2))


# ── ④ 渲染：blocked_rows 的形状 ────────────────────────────────────────────
_UNAVAIL = [{"spec": 'list_accounts({"role": "user"})', "tool": "list_accounts",
             "reason": "unavailable", "skill": "account_roster",
             "result": "UPSTREAM_DOWN\n第二行不该出现"}]
row = ctx.blocked_rows(_UNAVAIL)
check("行里有**技能名**（planner 选的是技能，这一格必须跟着走）",
      "技能=account_roster" in row, row)
check("行里有原因**中文**且不裸英文码", "服务不可用" in row and "unavailable" not in row, row)
check("不可重试那一族写明「改参数重试无效」",
      "改参数重试无效" in row and "仍可能成功" not in row, row)
_row_line = [l for l in row.splitlines() if l.startswith("· ")][0]
check("工具返回被压成一行（换行不炸行结构）",
      "UPSTREAM_DOWN 第二行不该出现" in _row_line, _row_line)
row_ok = ctx.blocked_rows([{"spec": 'rag_search({"q": "x"})', "tool": "rag_search",
                            "reason": "args_parse", "skill": "content_query",
                            "result": "bad json"}])
check("可救那一族写「改对参数再试一次仍可能成功」，且不误标无效",
      "仍可能成功" in row_ok and "无效" not in row_ok, row_ok)
check("缺 skill 也不炸（标「未标注」而不是编一个技能名）",
      "技能=（未标注）" in ctx.blocked_rows([{"tool": "t", "reason": "denied"}]))
check("空/None → **渲染成空串**（没有受阻项时整块都不出现）",
      ctx.blocked_rows([]) == "" and ctx.blocked_rows(None) == ""
      and ctx.BLOCKED_ROWS_EMPTY == "",
      repr(ctx.blocked_rows([])))
# 成本锁（20261007 定稿的口径）：模板每一轮都要发，**整块**——表头（含「受阻项」三个
# 字）、前言、行——都只在真有受阻项时才该付。写死在模板里、或给空值配一句占位语，
# 都等于每一轮替一件没发生的事付费；且会把"受阻"这个概念摆到**没有受阻项**的那一轮
# 眼前（实测那 33 token 的账：planner 提示词从 26512 → 26545）。两处各锁一遍。
check("整块（含表头）只在有受阻项时出现：缺省语里没有「受阻项」也没有前言",
      "受阻项" not in ctx.blocked_rows([]) and "不可重试" not in ctx.blocked_rows([])
      and "受阻项" in row and "不可重试" in row,
      repr(ctx.blocked_rows([])))
check("模板里**没有**写死表头/前言（表头住在 ctx.blocked_rows 里，模板那一格只有槽）",
      "本轮工具调用的" not in g._PLANNER_PROMPT
      and "不可重试" not in g._PLANNER_PROMPT)
check("形参默认值与常量同源（缺省即空串；两处不一致就分不清'没受阻'与'没接上'）",
      (g._render_planner_prompt.__kwdefaults__ or {}).get("blocked_rows")
      == ctx.BLOCKED_ROWS_EMPTY == "",
      str(g._render_planner_prompt.__kwdefaults__))


# ── ⑤ 接线：模板 / 渲染函数 / 调用点 / 写入点 ──────────────────────────────
check("模板里有 {blocked_rows} 槽", "{blocked_rows}" in g._PLANNER_PROMPT)
check("§1.55 的推断（不可重试 ⇒ 目标不存在）被那句话说掉了：在**渲染值**里、不在模板里",
      "不可重试 ≠ 你要办的事" in ctx.blocked_rows(_UNAVAIL)
      and "不可重试 ≠ 你要办的事" not in g._PLANNER_PROMPT)
# 存在性先判、再比顺序：短路是刻意的——槽被删掉时这条要**红**，不是抛 ValueError
# （抛异常会让整个文件在这里停住，后面 ⑤ 的几条谁也不跑，"红"就变成"崩"）。
_TPL = g._PLANNER_PROMPT
check("槽在工具结果之后、可引用字段之前（它说的是刚发生的那一轮）",
      all(s in _TPL for s in ("{tool_results}", "{blocked_rows}", "{ref_hints}"))
      and _TPL.index("{tool_results}") < _TPL.index("{blocked_rows}")
      < _TPL.index("{ref_hints}"))
check("渲染函数**必须有**这个形参（漏传即 TypeError）",
      "blocked_rows" in g._render_planner_prompt.__code__.co_varnames)
check("`planner_node` 每轮把它算进 `_prompt_args`",
      "blocked_rows=blocked_rows(state.get(\"blocked\") or [])" in _GRAPH_SRC)
# 判据取的是 `blocked.append(...)` **那一段**里的 skill，不是全文件找字面——
# `rcpt = {"skill": plan["skill"], ...}`（回执那一行）逐字相同，全文件找的话
# 这一条**永远不会红**（写这条时实测过：把 append 里那一行删掉，全文件找仍然绿）。
_APPEND = _GRAPH_SRC.split("blocked.append(", 1)[1].split("})", 1)[0]
check("`execute` 往受阻项里写了 skill（类型接上线的另一半，另一半在 checker）",
      '"skill"' in _APPEND and 'plan["skill"]' in _APPEND, _APPEND[:120])
_prompt = g._render_planner_prompt(
    "admin", "ctx", "round", user_msg="u", intent_hints="i", doc_anchors="d",
    recent_context="r", short_reply_hint="s", tool_results="t", pending_ledger="p",
    ref_hints="", reflector_feedback="", correction="", blocked_rows=row)
check("端到端：渲染出来的提示词里真的有这一行（不是只写进了模板）",
      "技能=account_roster" in _prompt and "改参数重试无效" in _prompt)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
