"""技能索引：把 schema 里那份**内联的目录**换成一行索引 + 按需取回（ADR-0015 阶段 0）。

**问题**：`build_tool_schema` 发给模型的每个技能函数里，`description` 是一整段散文。
实测（20261020，`docs/prefix-cache-audit-20261020.md` §1）：planner 单次输入 52,145 tok，
其中 tools schema 38,738（74%）；而 schema 里 **50.2% 是描述散文**。那批散文**不是签名，
是一份目录**——它描述"这个站能干什么"。

**做法**：描述**不删、只外置**。函数名 / type / enum / required / 嵌套 schema **一个字符
不动**（`enum` 白名单是带参清单的拦截点），散文压成一行索引；需要细节时模型调
`describe_skill(name)` 取回**原文**。

**为什么是"外置"不是"删掉"**：`eval/contract_position_ab.py`（7 用例 × 12 遍）实测
**位置无关**——那句话住正文 84/84、住 schema 84/84、**整句删掉 73/84**。⇒ 它**本身承重**，
但放哪不重要。删掉是拆契约，换位置不是。

**出厂关**：`build_tool_schema(..., index=False)` 是缺省值，本模块在缺省档**一次都不被
调用** ⇒ 生产路径逐字节不变（`tests/test_prompt_prefix.py` 钉住这一条）。

**索引行是机械切出来的**（`index_line`），不是手写的第二份名单：手写会立刻漂，
而漂了没有判据看得见（同 `workflow_menu` 那条"展开器只能有一个"的教训）。
"""

from __future__ import annotations

from typing import Any

from agent.workflow_defs import WORKFLOW_MAP, visible_workflows
from agent.workflow_menu import render_tool_marks

# 取回工具的名字。**与技能名同一命名空间**（都在 `tools` 数组里当 function name），
# 所以它不许与任何技能名撞——`tests/test_prompt_prefix.py` 有一条断言钉这个。
DESCRIBE_SKILL = "describe_skill"

# 一句话索引的切法（机械规则，不许手写）：
#   ① 只看**第一行**——`完成判定：…` 那一段永远在第二行起，索引是"这个本领管什么"，
#      不是"它算做完了没"（后者由取回原文给）；
#   ② 在第一行的 [8, 80] 区间里按**句读优先级**找最靠前的边界：
#      `。` > `；` > `——` > `，`。分级而不是"谁最靠前用谁"：逗号在枚举句里来得太早
#      （`闲聊、问候、情感交流、纯文字问答（不需要任何工具）时使用。` 按逗号切会切出
#      `闲聊`），而句号切出来的是完整的一句；
#   ③ 一个都没找到就硬截 80。
# 下界 8 是"别切出一个没有信息量的残句"（`用户要求。`），上界 80 是"一行"的物理上限。
_CUT_MARKS = ("。", "；", "——", "，")
_MIN_CUT = 8
_MAX_LEN = 80


def index_line(text: str) -> str:
    """一段技能描述 → 一行索引（**原文第一行的前缀**，绝不改写、绝不缩写）。

    只切不写：切出来的一定是 `text` 的前缀，所以"索引说了什么"永远能在原文里逐字对上
    ——这条让 `describe_skill` 的返回**必然覆盖**索引行（不会出现"索引里提了一件事、
    取回的原文里没有"）。`tests/test_tool_index.py` 有一条断言钉住前缀性。
    """
    first = text.split("\n", 1)[0]
    if len(first) <= _MIN_CUT:
        return first
    for mark in _CUT_MARKS:
        i = first.find(mark, _MIN_CUT, _MAX_LEN + 1)
        if i != -1:
            return first[:i]
    return first[:_MAX_LEN]


def skill_description(workflow: Any, role: str | None) -> str:
    """技能的**完整**描述（索引化前 `description` 那一格的全部内容）。

    **单一来源**：`build_tool_schema` 的散文档与 `describe_skill` 的返回都走这里。
    写两处的话，取回的内容会与"索引化前模型看到的东西"悄悄分叉，而分叉没有判据看得见
    ——那正是这次改造最容易出的错。

    两段与 `build_tool_schema` 原来那两行逐字相同：渲染工具标记（角色相关）+ 完成判定。
    """
    desc = render_tool_marks(workflow.description, role)
    if workflow.complete_when:
        desc = f"{desc}\n完成判定：{workflow.complete_when}"
    return desc


def describe_skill_schema() -> dict:
    """`describe_skill` 的 OpenAI function schema（只走索引档）。

    `name` **不写 `enum`**（同 `tasks.task_intents_schema` 那条）：闭集是全部可见技能名，
    而技能名**已经**是 `tools` 数组里的 function name ⇒ 枚举一遍是把同一份名单发第二遍，
    代价落在每一次 planner 调用上。写错的名字由 `describe_skill_text` 挡掉（返回 None）。
    """
    return {
        "type": "function",
        "function": {
            "name": DESCRIBE_SKILL,
            "description": (
                "查看某个本领的**完整说明**。上面每个函数只有一行索引，"
                "当你不确定某个本领到底管什么、或者要判断两三个近义本领该选哪个时，"
                "用这个工具把它的完整说明取回来再决定。一次可以查一到三个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "要查看的本领名（就是上面那些函数名）"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


def describe_skill_text(name: Any, role: str | None) -> str | None:
    """取回一个技能的完整说明；**够不着的一律 None**（不猜、不兜）。

    可见性判据用 `visible_workflows(role)`——与 `build_tool_schema`、planner 菜单、
    `task_intents` 归一化是**同一张表**。所以"能取回什么"恒等于"这一轮本来就能选什么"，
    **这不是扩权**：取回的是模型自己菜单上那些函数的说明，一个字节都不多。
    """
    key = str(name or "").strip()
    if not key:
        return None
    workflow = WORKFLOW_MAP.get(key)
    if workflow is None:
        return None
    if key not in {w.name for w in visible_workflows(role)}:
        return None
    return skill_description(workflow, role)
