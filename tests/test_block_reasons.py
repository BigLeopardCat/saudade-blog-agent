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
  · ④ `agent/prompt_context.py::blocked_rows` —— 渲染形状：技能名/原因中文（**不裸英文码**）/
    "能不能改参重试"两族措辞/无受阻项时渲染空串（整块只在有受阻项时才付）；
  · ⑤ 接线 —— 模板有槽、渲染函数有形参、`planner_node` 每轮都传、`execute` 往受阻项
    里写了 `workflow`。

为什么主断言落在**表与形状**上而不是"提示词里有没有那句话"：这一件的失败面是"类型
没接上线、planner 又得靠猜"。断一句具体措辞是假绿——换一句话就过（20261007 两次
A/B 的教训正是"改措辞救不回来"）。

用法：.venv/bin/python tests/test_block_reasons.py
"""


import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 pytest 当套件收）

import agent.graph as g  # noqa: E402
import agent.prompt_context as ctx  # noqa: E402
import server as srv  # noqa: E402
from agent import adminops_target as _target  # noqa: E402
from agent import authz, idem  # noqa: E402
from agent.arg_refs import ref_error_frame  # noqa: E402
from agent.block_reasons import REASONS, block_reason_type  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def produced_codes(sources: dict) -> set:
    """源码 → 生产侧能发出的原因码集合。**纯函数**，正控/反控直接喂它。"""
    out = set()
    for text in sources.values():
        tree = ast.parse(text)
        for node in tree.body:                       # ① 模块级 `REASON_XXX = "code"`
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
               and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.startswith("REASON_"):
                        out.add(node.value.value)
        for node in ast.walk(tree):                  # ② `return <BLOCK|None>, "code"`
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                e = node.value.elts
                if len(e) == 2 and isinstance(e[1], ast.Constant) \
                   and isinstance(e[1].value, str):
                    h = e[0]
                    if getattr(h, "id", None) == "_VERDICT_BLOCK" or \
                       (isinstance(h, ast.Constant) and h.value is None):
                        out.add(e[1].value)
    # **只认原因码的形状**（小写 ASCII + 下划线）：`return None, "读不到现有的标签字典…"`
    # 那种人话是同一个 AST 形状（`tools/base.py` 里有十来处），不筛的话这一条会变成
    # "见谁都红"。筛掉的是形状不对的，不是"看着不顺眼"的。
    return {c for c in out if re.fullmatch(r"[a-z][a-z_]*", c)}


def test_block_reasons():
    _GRAPH_SRC = _ctx_src.graph_deprefixed()
    _NON_RETRY = {"unavailable", "cmd_shape", "policy_refused", "consent_required",
                  "denied", "no_manifest", "unknown_role", "write_outcome_unknown"}
    check("表非空，每项形如 (非空中文, bool)",
          bool(REASONS) and all(isinstance(v, tuple) and len(v) == 2 and v[0]
                                and not v[0].isascii() and isinstance(v[1], bool)
                                for v in REASONS.values()))
    check("「改参数重试无效」那一族就是这八个（多一个少一个都要有人复核）",
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
    check("键集合与 server._REASON_CN **相等**（一侧加码，另一侧必须同改）",
          set(REASONS) == set(srv._REASON_CN),
          f"仅本表={sorted(set(REASONS) - set(srv._REASON_CN))} "
      f"仅 server={sorted(set(srv._REASON_CN) - set(REASONS))}")
    check("逐值相等（同一个码在两侧是同一句中文）",
          all(REASONS[k][0] == srv._REASON_CN[k] for k in REASONS if k in srv._REASON_CN),
          str([(k, REASONS[k][0], srv._REASON_CN.get(k)) for k in REASONS
               if k in srv._REASON_CN and REASONS[k][0] != srv._REASON_CN[k]]))
    _BODY = _GRAPH_SRC.split("def _check_spec(", 1)[1].split("\ndef ", 1)[0]
    _LITS = set(re.findall(r'_VERDICT_BLOCK, "([a-z_]+)"', _BODY))
    check("源码级：`_check_spec` 里 `_VERDICT_BLOCK, \"…\"` 形状的字面码全都登记了",
          bool(_LITS) and _LITS <= set(REASONS), str(sorted(_LITS)))
    check("源码级：or 链末端的 error_frame 也登记了",
          'or "error_frame"' in _BODY and "error_frame" in REASONS)
    check("源码级：六个取回通道都还接在 `_check_spec` 上（摘一个就少一族码）",
          all(fn in _BODY for fn in
              ("ref_error_reason", "scope_error_reason", "consent_error_reason",
               "target_error_reason", "policy_error_reason", "replay_reason")))
    _r = g._check_spec("read_notifications", {}, True,
                       "__ERROR__: 权限不足[denied] —— 普通用户 无权调用 read_notifications",
                       "notice_read")
    check("行为级：权限不足帧 → (BLOCK, denied)，且 denied 在表里",
          _r == ("BLOCK", "denied") and "denied" in REASONS, str(_r))
    _r2 = g._check_spec("read_notifications", {}, True, "…", "notice_read", kind="unavailable")
    check("行为级：kind=unavailable → 表里标的是不可重试",
          _r2 == ("BLOCK", "unavailable") and block_reason_type(_r2[1])[1] is False, str(_r2))
    _r3 = g._check_spec("set_article_status", {}, True,
                        g.idem.unknown_frame("set_article_status"), "article_status")
    check("行为级：幂等账的「结果未知」帧 → (BLOCK, write_outcome_unknown)，且不可重试",
          _r3 == ("BLOCK", "write_outcome_unknown")
          and block_reason_type(_r3[1])[1] is False, str(_r3))
    _r3n = g._check_spec("set_article_status", {}, True, "__ERROR__: 随便什么别的失败",
                         "article_status")
    check("反向对照：没有那个原因码的错帧取不回这一族（判的是码，不是帧形）",
          _r3n == ("BLOCK", "error_frame"), str(_r3n))
    _UNAVAIL = [{"spec": 'list_accounts({"role": "user"})', "tool": "list_accounts",
                 "reason": "unavailable", "workflow": "account_roster",
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
                                "reason": "args_parse", "workflow": "content_query",
                                "result": "bad json"}])
    check("可救那一族写「改对参数再试一次仍可能成功」，且不误标无效",
          "仍可能成功" in row_ok and "无效" not in row_ok, row_ok)
    check("缺 workflow 也不炸（标「未标注」而不是编一个技能名）",
          "技能=（未标注）" in ctx.blocked_rows([{"tool": "t", "reason": "denied"}]))
    check("空/None → **渲染成空串**（没有受阻项时整块都不出现）",
          ctx.blocked_rows([]) == "" and ctx.blocked_rows(None) == ""
          and ctx.BLOCKED_ROWS_EMPTY == "",
          repr(ctx.blocked_rows([])))
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
    check("模板里有 {blocked_rows} 槽", "{blocked_rows}" in g._PLANNER_PROMPT)
    check("§1.55 的推断（不可重试 ⇒ 目标不存在）被那句话说掉了：在**渲染值**里、不在模板里",
          "不可重试 ≠ 你要办的事" in ctx.blocked_rows(_UNAVAIL)
          and "不可重试 ≠ 你要办的事" not in g._PLANNER_PROMPT)
    _TPL = g._PLANNER_PROMPT
    check("槽在工具结果之后、可引用字段之前（它说的是刚发生的那一轮）",
          all(s in _TPL for s in ("{tool_results}", "{blocked_rows}", "{ref_hints}"))
          and _TPL.index("{tool_results}") < _TPL.index("{blocked_rows}")
          < _TPL.index("{ref_hints}"))
    check("渲染函数**必须有**这个形参（漏传即 TypeError）",
          "blocked_rows" in g._render_planner_prompt.__code__.co_varnames)
    check("`planner_node` 每轮把它算进 `_prompt_args`",
          "blocked_rows=blocked_rows(state.get(\"blocked\") or [])" in _GRAPH_SRC)
    _APPEND = _GRAPH_SRC.split("blocked.append(", 1)[1].split("})", 1)[0]
    check("`execute` 往受阻项里写了 workflow（类型接上线的另一半，另一半在 checker）",
          '"workflow"' in _APPEND and 'plan["workflow"]' in _APPEND, _APPEND[:120])
    _prompt = g._render_planner_prompt(
        "admin", "ctx", "round", user_msg="u", intent_hints="i", doc_anchors="d",
        recent_context="r", short_reply_hint="s", tool_results="t", pending_ledger="p",
        ref_hints="", reflector_feedback="", correction="", blocked_rows=row)
    check("端到端：渲染出来的提示词里真的有这一行（不是只写进了模板）",
          "技能=account_roster" in _prompt and "改参数重试无效" in _prompt)
    _NOT_REASONS = {"ok"}
    _SRC = {_p: (ROOT / _p).read_text(encoding="utf-8")
            for _p in ("agent/arg_refs.py", "agent/authz.py", "agent/adminops_target.py",
                       "agent/idem.py", "agent/graph.py")}
    _PRODUCED = produced_codes(_SRC) - _NOT_REASONS
    check("正控：生产侧确实扫到了码（扫不到的话下面那条是**空过**——"
      "\"没产出\"与\"产出的都登记了\"同形）",
          len(_PRODUCED) >= 12, f"只扫到 {len(_PRODUCED)} 个：{sorted(_PRODUCED)}")
    check("⭐ 生产侧能发出的每一个码都在 `REASONS` 里（新增码忘了登记 ⇒ 当场红）",
          _PRODUCED <= set(REASONS),
          "未登记：" + "、".join(sorted(_PRODUCED - set(REASONS))))
    check("  豁免的 `ok` 确实**不是**受阻原因码（它进不了 REASONS）——"
      "哪天它真成了受阻码，这条豁免就成了掩护",
          "ok" not in REASONS)
    _FAKE = produced_codes({"x.py": 'REASON_NEW = "brand_new_code"\n'}) - _NOT_REASONS
    check("  反控：合成 `REASON_NEW = \"brand_new_code\"` ⇒ 扫得到、且不在表里",
          _FAKE == {"brand_new_code"} and not (_FAKE <= set(REASONS)), str(sorted(_FAKE)))
    check("  反控：`return None, \"读不到现有的标签字典\"` 那种人话不算码",
          produced_codes({"x.py": 'def f():\n    return None, "读不到现有的标签字典"\n'}) == set())
    _DEN = authz.Decision(False, authz.REASON_DENIED, "write.console", "set_article_status")
    _PRN = authz.Principal(uid=1, role="user")
    _FRAMES = {
        "denial": authz.denial_frame(_DEN, _PRN),
        "consent": authz.consent_frame("set_article_status", _PRN),
        "action_conflict": authz.action_conflict_frame("remove_favorite", "add_favorite"),
        "unknown_target": _target.unknown_target_frame("set_article_status"),
        "target_mismatch": _target.target_conflict_frame("set_article_status", {12}, 46),
        "policy_refused": _target.policy_frame("后端说不行"),
        "write_outcome_unknown": idem.unknown_frame("set_article_status"),
        "ref_unparsed": ref_error_frame("ref_unparsed"),
    }
    _FRAME_CODES = {k: (getattr(v, "meta", None) or {}).get("reason") for k, v in _FRAMES.items()}
    check("正控：八个帧生产方**都**交出了 `meta[\"reason\"]`（少一个下面那条就是空过）",
          all(_FRAME_CODES.values()), str(_FRAME_CODES))
    check("⭐ 交出的每一个码都在 `REASONS` 里（`_check_spec` 拿它当 blocked 的原因）",
          set(_FRAME_CODES.values()) <= set(REASONS),
          "未登记：" + "、".join(sorted(set(_FRAME_CODES.values()) - set(REASONS))))
    check("结构与帧文里那个方括号**说的是同一件事**（两条通道不许分岔）",
          all(f"[{c}]" in str(_FRAMES[k]) for k, c in _FRAME_CODES.items()),
          str({k: c for k, c in _FRAME_CODES.items() if f"[{c}]" not in str(_FRAMES[k])}))
    _ref_f = ref_error_frame("ref_unknown_tool:$fake_search[0].noteKey")
    check("引用失败：帧文带全串、`meta` 只放冒号前的码",
          _ref_f.meta["reason"] == "ref_unknown_tool"
          and "ref_unknown_tool:$fake_search[0].noteKey" in str(_ref_f),
          f"{_ref_f.meta} / {str(_ref_f)[:60]}")
    check("  ⇒ 真的走一遍 `_check_spec`：得到的是那个**查得到中文**的码",
          g._check_spec("search_notes", {}, True, _ref_f, "content_query",
                        meta=_ref_f.meta) == ("BLOCK", "ref_unknown_tool"))
    _MANGLED = str(_FRAMES["denial"]).replace("权限不足", "这条不行")
    check("  反控的正控：帧文改掉之后，**文本那一支**取不回那个码了（下面那条不是白绿）",
          g._check_spec("set_article_status", {}, True, _MANGLED, "article_status")
          == ("BLOCK", "error_frame"),
          str(g._check_spec("set_article_status", {}, True, _MANGLED, "article_status")))
    check("⭐ 反控：同一份帧**带上 `meta`** ⇒ 原因码仍然准（措辞改一百遍也不影响分叉）",
          g._check_spec("set_article_status", {}, True, _MANGLED, "article_status",
                        meta=_FRAMES["denial"].meta) == ("BLOCK", "denied"))
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_block_reasons()
