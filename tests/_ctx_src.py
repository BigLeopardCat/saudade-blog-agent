# -*- coding: utf-8 -*-
"""`_DecideCtx` 的**去前缀视图**：给"整文件源码文本锁"用的。

**要解决的问题**（20261009 刀 2）：`_planner_decide` 拆成七个阶段函数后，跨段共享量
不再由一堆局部变量承载，改成从 ctx 对象上取（`rounds` → `c.rounds`）。而一批接线锁
是**整文件源码文本**判据（`test_menu_deny` / `test_intents_backfill` /
`test_block_reasons` / `test_pending_ledger` / `test_sections` /
`test_target_grounding` / `test_tag_admin`），断言写成「这行代码长这样」——
一加前缀就整族红，**而代码的语义一个字没变**。

本模块把 `c.<字段>` 还原成 `<字段>`，于是那些断言**一个字都不用改**：测试里的判据
文本与拆分之前逐字相同，reviewer 看到的 diff 是"锚点换了个取源码的方式"，
不是"判据被改松了"。

两条边界，都写在这里免重议：

  · **字段清单从 `agent/graph.py` 自己的 AST 读**（`_DecideCtx` 的注解字段），
    **不许手抄**——手抄的那份会漂，而漂了以后是"判据安静地判了个别的东西"。
    找不到 `_DecideCtx` 就**响亮地退出**（`SystemExit`），不许静默原样返回：
    静默返回会让上面七个套件整族红在一句看不懂的断言上。
  · **只还原真正的 `_DecideCtx` 字段**。graph.py 里还有别的叫 `c` 的局部量
    （`c.strip()` / `c.group(0)` / `for c in …`），它们的属性名不在字段清单里 ⇒
    一个都不碰（落地时实测：全文件只有决策区那 240 行会变，其它一行不动）。

为什么不直接改那七个套件的断言字面量：那些断言的力气全在"逐字"二字上，
逐字改一遍等于让"判据没动"这件事**没有证据**；去前缀视图让"判据没动"可核对。
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent      # agent 仓根
GRAPH = ROOT / "agent" / "graph.py"


def ctx_fields(src=None):
    """`_DecideCtx` 的字段名集合（从 AST 的注解取，不执行那段代码）。"""
    tree = ast.parse(src if src is not None else GRAPH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_DecideCtx":
            names = frozenset(
                t.target.id for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name))
            if names:
                return names
    raise SystemExit(
        "❌ `agent/graph.py` 里找不到 `_DecideCtx` 的注解字段——去前缀视图没有依据。\n"
        "   先看它是不是改名/拆走了；确认之后同步本模块与那七个套件的锚点，别让它静默原样返回。")


def deprefix(src):
    """把 `c.<字段>` 还原成 `<字段>`（字段清单见 `ctx_fields`），其余一字不动。"""
    fields = sorted(ctx_fields(src), key=len, reverse=True)
    # 长名优先：`_base_llm` 必须先于 `llm` 试，否则 `c._base_llm` 会被切成 `c._base_` + `llm`
    return re.sub(r"\bc\.(?:%s)\b" % "|".join(re.escape(f) for f in fields),
                  lambda m: m.group(0)[2:], src)


def graph_deprefixed():
    """读 `agent/graph.py` 并返回去前缀后的全文（那七个套件的入口就这一行）。"""
    return deprefix(GRAPH.read_text(encoding="utf-8"))
