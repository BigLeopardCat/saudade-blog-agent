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

**第二个入口 `prose()`（20261014）**：模型可见正文已搬到 `agent/prompts.py`，
"整文件找散文"的判据同样会整族假红 ⇒ 同一套思路（取值点跟着正文搬、判据字面不动），
详见该函数自己的注释。

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


def plan_codec_src():
    """**计划文本契约**那一族的全量文本（20261017 起住在 `agent/plan_codec.py`）。

    给"整文件源码文本锁"取值：`test_plan_channel` ① 判"`plan_encode` 的代码调用点恰好
    一处、且在 `plan_state` 体内"，`test_status_judgements` ④ 判"写端一定写 `STATUS=`
    行、且排在 REPLY 之前"。这两条判的都是**这一族内部谁调用了谁**，而这一族
    20261017 从 `agent/graph.py` 搬进了 `agent/plan_codec.py` ⇒ 原来"读 graph.py 整文件"
    的取值点会整族变假红（代码还在、只是换了文件）。

    取值点改成这一族**全部文件**的拼接视图，于是那些断言**一个字都不用改**：判据没被
    改松，只是锚点跟着代码搬（同 `deprefix` / `prose()` 那两次的思路）。

    **列的是文件、不是"整个 agent 包"**：整个包拼起来会把别处同名的字面量也数进来，
    那是把锁放松，不是搬家。两份文件用 `\\n` 相接 ⇒ 行号会平移，但下面那几条判据只在
    这一份文本内部比大小/找区间，**两份共用同一张行号表**，所以自洽。
    """
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in (GRAPH, ROOT / "agent" / "plan_codec.py"))


WRITE_GUARD = ROOT / "agent" / "write_grounding.py"


def write_guard_src():
    """**写参数落位与拒答**那一族的全量文本（20261019 起住在 `agent/write_grounding.py`）。

    给"整文件源码文本锁"取值。这一族（planner 决定写哪一件之后、真正落到工具参数上
    之前那道关：落位 / 引文核对 / 拒答）20261019 从 `agent/graph.py` 整块搬走 ⇒
    原来"读 graph.py 整文件"的那批接线锁会整族假红（代码还在、只是换了文件）。
    取值点换成这一族**全部文件**的拼接视图，于是那些断言**一个字都不用改**
    （同 `deprefix` / `plan_codec_src` / `prose()` 三次的思路）。

    **列的是文件、不是"整个 agent 包"**：整个包拼起来会把别处同名的字面量也数进来，
    那是把锁放松，不是搬家。两份用 `\\n` 相接 ⇒ 行号会平移，但判据只在这一份文本内部
    找串/比先后，**两份共用同一张行号表**，自洽。

    ⚠️ **顺序是有讲究的：被搬走的那一族在前，graph.py 在后。** 因为这一族里也有
    **顺序锁**（"这一句排在那一句之前"），而拆完之后跨文件的那一对是
    `_decide_write_guard`（本体，搬走了）→ `_decide_late_correct`（收尾校正，留在图里）：
    原来在 graph.py 里就是本体在前（原 7207 < 7470）。口径按**本体在前、调用方在后**
    ——这一族是"写参数落位与拒答"的**实现**，graph.py 剩下的是**图与调用方**。
    （反过来拼会让 `tests/test_tag_admin.py` 那条顺序锁红，而红的理由看着像"接线断了"。）
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in (WRITE_GUARD, GRAPH))


def write_guard_deprefixed():
    """同上，但把 `c.<字段>` 还原成裸名（给那批本来用 `graph_deprefixed()` 的套件）。

    搬过去的那一族里也有 `c.plan_obj` / `c.user_msg` 这类取值（`_DecideCtx` 的字段），
    所以去前缀必须作用在**拼接后的全文**上，不能只作用在 graph.py 那一半。
    """
    return deprefix(write_guard_src())


def prose():
    """**模型可见正文**的全量文本（20261014 起住在 `agent/prompts.py`）。

    给"这段纪律/规则写下来了没有"这一类接线锁取值。正文按族搬出 graph.py 之后，
    原来"读 `agent/graph.py` 整文件找散文"的判据会整族变假红（句子还在、只是换了
    文件），而它们要判的东西一个字没变 ⇒ 加这一个入口，**判据文本逐字不动**、
    只把取值点从"图文件全文"换成"正文本体"。

    取值走 `agent.graph` 上的常量（那里按原名 re-export）⇒ 拿到的是**模型真读到的
    那串**，比读文件更严：读文件会把注释/别处抄一句也算过。

    组成：planner 模板 + 输出契约 + reflector 模板 + narrator 三段拼装后的那一份。
    **`_EXECUTOR_PROMPT` 里已逐字含 `_EXECUTOR_HEAD` 与 `NARRATOR_DISCIPLINE`**
    （23 + 6222 + 尾段 = 7197），所以这里不再重复拼进去——重复拼会让"计数类"
    断言（如 `count("[policy_refused]") >= 2`）凭空多算一份。改拼装方式时要来
    这里看一眼：真那样改，纪律类的断言会搜到一个不含纪律的串。
    """
    import agent.graph as G
    return "\n".join((G._PLANNER_PROMPT, G._PLANNER_OUTPUT_CONTRACT_NATIVE,
                      G._REFLECTOR_PROMPT, G._EXECUTOR_PROMPT))
