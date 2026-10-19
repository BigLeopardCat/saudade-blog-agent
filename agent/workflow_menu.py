# -*- coding: utf-8 -*-
"""planner 菜单的**渲染层**：把注册表、参数规格与工具清单印成提示词里的那几段文本。

四件东西，都是"给模型看"而不是"给机器判"的：

- `build_planner_context(role)` —— planner 的**主菜单**：可见技能（按角色）、每条技能的
  参数签名与描述、导航映射表、点名工具清单与收尾约定，拼成一段注入文本；
- `render_workflow_params` —— 菜单里那条 `名字:类型` 的参数行（写法与工具菜单
  `graph._menu_arg_signature` 逐字一致——两个菜单在 planner 眼里是同一张表的两段）；
- `render_tool_marks` —— 把技能描述里的工具枚举标记按**本轮角色**展开；
- `_nav_map_lines` / `_NAV_MAP_LINES` —— 导航映射表的分组形态。

**为什么单独成模块**（20261017 批 2 刀 2e-2）：这一族的出边全在**已经搬出去的层**上
（`workflow_model` 的标记与数据模型、`workflow_params` 的规格派生、`workflow_tools` 的
工具清单、`workflow_defs` 的可见性判据与导航表、`tools.base` 的 IoT 开关），
**一条边都不指向 `agent/workflows.py` 里留下的实例化层**——"出边为零"是这一轮拆分的
唯一选刀判据。依赖方向是 `menu → {tools, params, defs, model}`，单向。

⚠️ `_NAV_MAP_LINES` 是**模块级常量、import 期算一次**：它读的是 `NAV_MAP`（唯一数据源
在 `agent/workflow_defs/nav_map.py`），搬到这里只是换个地方算，取值逐字不变。
"""

from __future__ import annotations

from agent.workflow_defs import visible_workflows
from agent.workflow_defs.nav_map import _IOT_NAV_ALIASES, DASHBOARD_PANELS, NAV_MAP
from agent.workflow_model import (
    _EXPLICIT_TOOLS_MARK,
    _NO_DEFAULT,
    _PARAM_TOOLS_MARK,
    ParamSpec,
    ToolCall,
    Workflow,
)
from agent.workflow_params import workflow_param_specs
from agent.workflow_tools import explicit_tools, param_tools
from tools.base import IOT_ENABLED

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["render_tool_marks", "render_workflow_params", "build_planner_context"]


def render_tool_marks(text: str, role: str | None) -> str:
    """把技能描述里的工具枚举标记按本轮角色展开（`build_planner_context` 调）。"""
    return (text.replace(_EXPLICIT_TOOLS_MARK, "/".join(explicit_tools(role)))
                .replace(_PARAM_TOOLS_MARK, "/".join(param_tools(role))))


def render_workflow_params(workflow: Workflow, specs: dict[str, ParamSpec] | None = None) -> str:
    """planner 菜单里的参数一行：`名字:类型`，必填加 `*`、有默认值加 `=值`、后跟中文说明。

    写法与工具菜单（`graph._menu_arg_signature`）**逐字一致**——两个菜单在 planner
    眼里是同一张表的两段，记号不同会让它两套读法。推不出类型的参数（`any`）不标
    `*`：**宁可少说，不说错**。
    """
    specs = specs if specs is not None else workflow_param_specs(workflow)
    if not specs:
        return ""
    parts = []
    for name, sp in specs.items():
        seg = f"{name}:{sp.type}"
        if sp.required:
            seg += "*"
        elif sp.default is not _NO_DEFAULT and sp.default is not None:
            shown = (f'"{sp.default}"' if isinstance(sp.default, str) and sp.default
                     else str(sp.default))
            seg += "=" + shown[:12]
        if sp.desc:
            seg += f"（{sp.desc}）"
        parts.append(seg)
    return "、".join(parts)


# ---------------------------------------------------------------------------
# prompt 注入块构建（planner 提示词注入用）
# ---------------------------------------------------------------------------

def _nav_map_lines() -> str:
    """导航映射表的提示词形态（**分组**，20260926）。

    后台面板那十几个别名单独一行：混进主行后，模型要在三四十个 `别名→路径`
    里挑，而后台名彼此长得很像。分组只是排版——数据源仍是 NAV_MAP 一处
    （后台那组按 DASHBOARD_PANELS 的路径认出来，不维护第二份名单）。
    """
    dash_paths = {p for _, p in DASHBOARD_PANELS}
    plain: list[str] = []
    dash: list[str] = []
    for alias, path in NAV_MAP.items():
        # IoT 别名在开关关掉时**整条不出现**（不是写成"已下线"）：提示词里出现的
        # 每一个入口都是 planner 可以照着去填 target 的候选，写"→（已下线）"等于
        # 把"本站有这个页面"这件事再讲一遍——`navigate` 那一支的未部署话术是给
        # 用户**主动问起**时用的，不是给 planner 当备选清单用的。
        if not IOT_ENABLED and alias in _IOT_NAV_ALIASES:
            continue
        line = f"{alias}→{path}" if path else f"{alias}→（已下线，如实告知）"
        (dash if path in dash_paths else plain).append(line)
    return ("、".join(plain)
            + "\n后台面板（仅博主可用；用户说「转跳后台/去后台」而没点名板块时，"
              "target 填「后台」即可——那就是后台主页，不要追问是哪个板块）："
            + "、".join(dash))


_NAV_MAP_LINES = _nav_map_lines()


def _workflow_plan_seq(s) -> str:
    """技能的固定工具序列渲染成一行（`工具(参数模板) → 工具(参数模板)`）。

    这是**给 planner 看的菜单文本**，`ToolCall` 在这里只是借它的渲染（`str()`），
    不参与任何解析。
    """
    return " → ".join(ToolCall(t, a) for t, a in s.plan)


def build_planner_context(role: str | None = None, *, slim: bool = False,
                          deny: frozenset[str] | set[str] | None = None) -> str:
    """planner 注入：技能表（触发条件 + 参数 + 工具序列 + 完成判定）+ 导航映射表。

    read_article 不列出——系统快道专用（article_id 是 current_url 解析的系统数据，
    planner 无参可填，误选只能产出 null 工具调用），planner 不可见即不可选。

    `role`（20260921）= 本轮调用者角色（graph 从 principal.known_role 取）：
    技能的 `roles` 非空且不含该角色 → **整个技能不列出来**。非 admin 的管理助手
    技能因此选不出来。传 None（未知身份/老路径/单测）等价于"只有公开技能"——
    失败取向往保守一侧倒，与本仓 authz 的取向一致。

    `slim=True`（20260927）：去掉每条技能的**触发条件/参数/完成判定**三行，只留技能名
    与它的固定工具序列。**20261004 起它是唯一的渲染形态**（`_render_planner_prompt` 的
    `slim_workflows` 默认 True、`planner_node` 也只传这一个值）——那时删掉了文本契约档，
    而"同一份技能表下发两份形态"是 native 独有的处境。`slim=False` 那一支**只剩测试在
    用**（`tests/test_slim_workflows.py` 拿它当对照臂：判据①要求"删掉的必须是 schema 里
    逐字有的"，没有完整那一份就无从比）。**它不是第二个拨盘**：别再给它接任何生产开关。
    理由是这三样在同一轮的 `tools`
    里逐字重复出现（schema 的 `description` = `s.description` + `完成判定：` +
    `complete_when`，`properties` = 同一份 `workflow_param_specs`），而**同一事实写两遍
    是有代价的**：接口层面临的是两份形态不同的同一张表（散文 vs JSON Schema），
    模型得自己判断听哪一份，且两份会漂移——本批落地时就抓到一处：schema 侧的
    `content_query` 描述里原样漏着未展开的 `__无参只读工具清单__` 标记（已修，
    见 `planner_llm.py`）。**不删的**：固定工具序列（`tools` 的 schema 里没有，
    而 `content_query` 的 calls 通道要求写工具名）、导航映射表、能力边界兜底段、
    口语变体说明——这些是这一块独有的信息，删了就真丢。

    ⚠️ `slim` **只影响渲染，不影响任何判据**：谁能选（`visible_workflows`）、参数怎么
    校验（`workflow_param_specs` / `check_workflow_params`）都不看这段文本。

    `deny`（20261007，1d）= **这一轮不许再选的技能名**（来源见
    `agent/block_reasons.py::denied_workflows`：上一轮受阻、且原因是"改参数重试无效"
    那一族）。它与 接口层的 `tools` schema 收的是**同一个集合**——"菜单"横跨
    两处（这张自动生成的技能表 + `tools` schema），只摘一处等于没摘（提示词里还列着，
    模型照着名字点，只是 schema 里没有那个函数了）。**传空集/None 时渲染逐字节不变**，
    无阻碍轮一分钱不花。
    ⚠️ planner 提示词的**判定规则 1** 里还有一份**手写**的「- 技能名：什么时候用它」，
    那一份**刻意不摘**（它不是可点的菜单，只是"这技能干什么用"；真正的闸是 schema）。
    两处一张表、一处散文，各自的取义见 `tests/test_menu_deny.py` 里那条点名用例。
    ⚠️ 它**不是第二份可见性名单**（那件事仍只在 `visible_workflows` 一处判）：它只在
    "上一轮同一个技能刚失败过"时才非空，是**状态**不是**权限**。

    **契约句（"必须怎么做"）与描述同住 `description`**（20261017 起；此前是独立的
    `planner_contract` 字段，20260927–20261017）：当初拆字段的依据是一次五遍 A/B——
    `rag_talk_rag` 完整菜单 5/5 绿 → slim 0/5 红。20261017 四臂重验
    （`eval/contract_position_ab.py`：① 正文 ② 只在 schema ④ 并进 description
    ③ 整句删掉）把它**证伪**了：② 与 ④ 逐条相同（各 84/84），掉的是 ③
    （`pair_one_talks` 1/12）⇒ 位置无关、承重的是那句话本身；原 0/5 的真因是
    **信息丢失**（当时 slim 的 schema 里 `content_query` 的描述原样漏着未展开的占位
    标记，`tools` 参数描述又被 `_SCHEMA_OVERRIDES` 覆盖成没有工具名的一句）——
    归因错在"位置"。字段因此撤掉、并回描述，`tools[]` 回到行业惯例的形状（每个函数
    一个 `description`，没有第二个字段）。**别再拆出来**：拆一次就多守一条"两份会漂"
    的纪律。⚠️ slim 档因此**不再单独渲染契约行**——它随描述一起交给 schema 那一份。
    """
    lines = ["可调用的技能（函数名即技能名，一次选一个；每个技能的触发条件、参数与"
             "完成判定见本轮 tools 里同名函数的 schema，此处不重复，只列它展开成哪些工具）："
             if slim else
             "可用技能（只能从以下技能中选择一个，不得自创步骤或自由编写执行计划）："]
    for s in visible_workflows(role):
        if deny and s.name in deny:
            continue
        if slim:
            # 没有固定工具序列的技能（如 chat）也列一行：它是"可选集合"的一员，
            # 缺了会让模型以为没有这个出口（schema 里有、但这里是同一张表的读法）。
            lines.append(f"- {s.name}：{_workflow_plan_seq(s) if s.plan else '（无固定工具）'}")
            continue
        lines.append(f"- {s.name}：{s.description}")
        # 参数一行 = `名字:类型` + 必填 `*` + 默认值 `=值` + 中文说明（20260925）。
        # 此前这里是 `json.dumps(s.inputs)` ——**只有散文**，必填/类型/默认值一概
        # 看不见，planner 只能靠常识猜，而 `instantiate_plan` 拿到 PARAMS 后什么
        # 都不查（缺参要等工具侧报错，白烧一轮）。记号与工具菜单（`_menu_arg_signature`）
        # 逐字一致：两个菜单在 planner 眼里是同一张表的两段，记号不同会让它两套读法。
        sig = render_workflow_params(s)
        if sig:
            lines.append(f"  参数：{sig}")
        if s.plan:
            lines.append(f"  执行步骤：{_workflow_plan_seq(s)}")
        if s.complete_when:
            lines.append(f"  完成判定：{s.complete_when}")
    # 兜底语义（20260926 洞⑥ 复盘）：这张表此前只有「能做什么」，**没有「做不到怎么办」**。
    # 请求落在表外时（trace 20260926T082919：主人要"给某个用户发个通知"——**当时**通知类
    # 工具只有"读自己的"、站内没有给单个用户发通知的通道），planner 唯一的去处是 chat，
    # 而 chat 的语义是"闲聊"⇒ "这件事我做不到"**整条链路上没有任何一层被要求说出来**，
    # narrator 便用一句承诺句式（"点「确定」我就去办"）把它盖住了。确定性出口在别的维度
    # 早就有先例（NAV_MAP 值为 None = 已下线 ⇒ 如实告知），能力的边界一直没有对应物。
    #
    # ⚠️ 上面那个例子**当天就被建出来了**（技能 notice_send / 工具 send_user_notice）
    # ⇒ 拿它当"站内没有的能力"的举例**现在是假话**，别抄回来。这一段举例只用
    # **已核实真没有**的能力（见下方"登录记录/登录 IP"那一条的依据）。
    #
    # **刻意不写进 chat 的 description**：那会让"没有对应能力"看起来像 chat 的常规用法，
    # planner 可能据此把**真有技能**的请求也判成"做不到"（代价是不动手、主人原地等着，
    # 比多问一句贵）。写在这里，并要求"拿不准就照常选技能"——失败取向往能干活那侧倒。
    lines.append(
        "\n以上技能都不覆盖主人这一轮的请求时（他要办的事站内**根本没有**对应的能力或"
        "通道——例如查某个账号的**登录记录 / 登录 IP / 登录时间**，站内只记账号的"
        "创建时间与角色，没有登录审计）："
        "选 chat，回复里**如实说做不到**，并告诉他你能做的替代是什么（例如公告是全站"
        "可见的）。**拿不准就照常选技能**：把有技能的事判成做不了，比多问一句贵得多。"
        "**禁止**用 chat 收尾去索要「要不要我办／你说一声我就去办」——不排出写操作的"
        "清单就不会有任何确认卡，那句承诺保证是假的（系统只在真排了写操作时才弹卡）。")
    lines.append(f"\n导航映射表（navigate 的 target 参数从这里取值）：\n{_NAV_MAP_LINES}")
    lines.append(
        "口语变体（大小写 IOT/IoT/iot、'设备面板''管理设备'等同义说法）由系统自动归一，"
        "PARAMS.target 直接填映射表中最接近的别名即可，无需自创目标名"
    )
    # 技能描述里的工具枚举按角色展开（20260924）：那些标记是角色相关的，
    # 用哪个角色渲染这条注入，白名单就必须用哪个角色判——两处同一个 role。
    return render_tool_marks("\n".join(lines), role)
