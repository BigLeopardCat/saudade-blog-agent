# -*- coding: utf-8 -*-
"""写技能的**展开层**：`Workflow` + 参数 → （TOOLS 行清单, 注记）。

按名字写数据的技能（`_expand_write_workflow` 一族：标题/正文/标签/分类/公告/配额/看板…）
与变更集（`_expand_change_set`）怎么翻成一条条调用，连同它们共用的原语：参数值 → 字符串
（`_write_arg` / `_write_list_arg`）、占位符与未填充检测（`_unfilled_placeholder` /
`_notice_placeholder`）。

**为什么单独成模块**（20261017 批 2 刀 2d-2）：从 `agent/workflow_expand.py` 里再切一刀，
为的是模块预算（那一刀之后 1062 行 > 800 上限，本仓的规矩是**先看能不能按主题拆**，
不是 `--update` 把超预算记进台账）。切法仍是**出边为零**：这一族只调本文件里的名字 +
`agent.adminops` 的纯函数 + `agent.workflow_defs` 的注册表 + `tools.base` 的三个上限常量，
**一条边都不指向 `agent/workflow_expand.py`**；反过来那边有三处（`_expand_todo_*` 拼调用
清单）要 `_write_arg`，于是依赖是单向的 `expand → expand_write`。

⚠️ 与 `agent/workflow_params.py`（刀 2c）的分工：那边管**事前校验与规格派生**（"参数
齐不齐、值归得了吗、菜单怎么印"），这边管**值 → 调用清单**。两者都读技能模板，但谁也
不 import 谁——`workflow_params` 的消费方是菜单与校验，本模块的消费方是实例化。

⚠️ 三个上限（`_NOTICE_CONTENT_LIMIT` / `_NOTICE_TITLE_LIMIT` / `_QUOTA_NOTE_LIMIT`）在
展开层挡住超长只是为了"零写 + 说清原因"，真正的硬闸在工具与服务端那两道。
"""

from __future__ import annotations

import json
import re

import agent.adminops as A
from agent.workflow_defs import WORKFLOW_MAP
from tools.base import (
    _NOTICE_CONTENT_LIMIT,
    _NOTICE_TITLE_LIMIT,
    _QUOTA_NOTE_LIMIT,
)

# 本模块**刻意不写 `__all__`**：整个面都是 `_` 前缀的内部件（展开层只被 `instantiate_plan`
# 与各展开分支互相调用）。按本仓"`__all__` 只列公开名"的口径（见 `agent/workflow_model.py`
# 与 `agent/workflow_defs/nav.py`），这里能列的只有空表——而空表会被读成"导出的东西一个
# 都没有"，与事实相反：跨模块 import 走的是显式名字（`from agent.workflows import
# _norm_id_list` 等），显式 import 从来不看 `__all__`。


def _write_arg(value) -> str:
    """写技能参数值 → 字符串（**不做类型强转、不吞引用**）。

    20260921 事故的核心就在这一步：旧代码用 `_norm_pos_int(params.get("parent_id"))`
    把 `$list_tags[3].tagId` 变成 `None` ⇒ 参数**静默消失** ⇒ 注记还肯定地写下
    「（一级标签）」。现在：空 → 空串（调用方剔掉）；其余**原样字符串化**——
    引用字面量照原样进 TOOLS 行，交给 execute 的 `resolve_args` 解析；解析不出来
    会产带原因码的 `__ERROR__` 帧（响亮、零执行），解析得出来就是真取到了值。
    两条路都比"悄悄当你没填"好。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # 这几个写技能的参数没有布尔语义（"有没有填"由键是否存在表达，不由值表达）。
        # 落成 "True" 会让工具收到一个名叫「True」的标签名——当没填更接近实情。
        return ""
    return str(value).strip()


def _write_list_arg(value) -> list[str]:
    """写技能的**数组**参数值 → 值清单（保序、去重、去空）。

    与 `_write_arg` 同一套取向（不做类型强转、不吞引用、宁可少说），差在收的是数组：
      · 单个字符串也认——文本档 planner 把 `titles: ["Git","SVN"]` 写成
        `titles: "Git"` 是同一层意思的另一种写法，不是错误（判它错只会白烧一轮）；
      · 对象元素里认得出 `title` / `name` 就取出那个字符串（原生档的 schema 把
        items 收成 string，文本档收不紧——同族形态在别的槽位实测出现过）；
      · 认不出的元素**丢掉**。它不可能是主人说的名字，而让一个 `{'a': 1}` 变成
        标签名比少建一个糟得多；少掉的那一个在确认卡上看得到（卡面逐条印名字），
        主人核对时就能纠正——所以这条不是静默的。
    """
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("title") or item.get("name")
        if isinstance(item, list) or isinstance(item, dict):
            continue
        s = _write_arg(item)
        if s and s not in out:
            out.append(s)
    return out


def _tag_names(params: dict) -> list[str]:
    """`tag_create` 的目标名字清单：`title`（单数槽位）与 `titles`（复数槽位）合并。

    两个槽位**都读**、而不是二选一：老语料里"建一个"一直填 `title`，新开的 `titles`
    只该在主人一口气点了好几个名字时出现，但两格同时有值是**合法输入**（原话里既
    点了一个名字又列了一串）——合并去重才是主人真正说的那一批。顺序先 `title`
    后 `titles`：TOOLS 行与卡面都按这个顺序印，主人核对时对得上自己的话。
    """
    out = _write_list_arg(params.get("title"))
    for one in _write_list_arg(params.get("titles")):
        if one not in out:
            out.append(one)
    return out


_TEMPLATE_TOKEN_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")


def _unfilled_placeholder(params: dict) -> tuple[str, str] | None:
    """参数值**就是技能模板自己的占位符**时返回 `(键, 值)`，否则 None（20260928）。

    技能 `plan` 模板里的参数位写的是 `$name` / `$title` 这种**填充记号**（由
    `expand_template_args` 拿同名参数替换，见它的头注）——它**不是**一个可以照抄的值。
    模型偶尔会把模板整行原样搬进 PARAMS（全库 trace 仅 1 例：`{"name": "$name"}`，
    trace `20260927_235721` 的 `account_freeze`），而那一例真的把「冻结账号「$name」」
    印上了确认卡：主人唯一能核对的那行字是个记号，他一点下去，动的就是一个活人的
    登录能力。所以判据必须在**展开这一层**拦下（零工具 + 交回 planner 重决策），
    而不是等卡片印出来才靠人眼发现。

    只认两个**机器能保证**的形态，不判"填得好不好"（那一维靠弹卡给人眼看）：
      ① 值恰好是 `$` + **它自己这个参数的键名**（模板照抄，实测的唯一形态）；
      ② 值是一个**裸的 `$词`**（`_TEMPLATE_TOKEN_RE`）——模板记号换了个键名抄进来。
    正常的主人原话里取到的名字不会长这样；万一真撞上（有人就管一个标签叫 `$foo`），
    代价是主人被反问一句、而那句话本来办得成——比"印一张记号卡让他盲签"小得多。

    与**参数引用**（`$tool[0].field`）不冲突：引用必须带方括号（`agent/refs.py` 的
    `REF_RE`），撞不上这两个形态。那一层由 `check_workflow_params` / `resolve_args` 管，
    本函数不碰。
    """
    if not isinstance(params, dict):
        return None
    for key, val in params.items():
        # **数组元素逐项看**（20261008，`tag_create.titles` 新开的复数槽位）：元素
        # 恰好是模板记号时的形状与单数槽位一模一样（`{"titles": ["$titles"]}`），
        # 而下面那层写展开会把每一项都变成一次真写——只看顶层 str 会整格漏过去，
        # 印出一张写着记号的卡，主人一点，站里就多一个叫 `$titles` 的标签。
        for one in (val if isinstance(val, list) else [val]):
            if not isinstance(one, str):
                continue
            tok = one.strip()
            if tok == "$" + str(key) or _TEMPLATE_TOKEN_RE.match(tok):
                return str(key), tok
    return None


_PLACEHOLDER_WRAPS = (("[" , "]"), ("【", "】"), ("（", "）"), ("(", ")"), ("{", "}"),
                      ("<", ">"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))


def _notice_placeholder(text: str) -> str:
    """通知正文看起来是**占位符**时给一句原因，否则空串（20260926 真机实测）。

    只认两种**机器能保证**的形态，不判"整理得好不好"（那一维靠弹卡给人眼）：
      ① 整串就是一个工具名或技能名（实测出现过正文＝`notice_send`——模型把技能名
         填进了正文槽）；
      ② 整串被一对括号/引号包住（实测出现过正文＝`[当前登录用户]`——模板占位符）。
    正常的人类句子（含"你好，这是一条测试通知。"这种改写过的一句）**一个都不拦**：
    拦错的代价是主人被反问一句、而那句话本来办得成。
    """
    s = (text or "").strip()
    if not s:
        return ""
    low = s.lower()
    try:
        from tools.base import get_all_tools
        tool_names = {getattr(t, "name", "") for t in get_all_tools()}
    except Exception:            # 注册表取不到就只查技能名（不因为取不到而放行①）
        tool_names = set()
    if low in {n.lower() for n in tool_names if n} or s in WORKFLOW_MAP:
        return f"正文是工具名/技能名「{s}」"
    for open_c, close_c in _PLACEHOLDER_WRAPS:
        if (len(s) <= 44 and s.startswith(open_c) and s.endswith(close_c)
                and (open_c == close_c or s.count(open_c) == 1)):
            return f"正文整个被括号包住「{s}」"
    return ""


def _expand_change_set(workflow, params: dict) -> tuple[list[str], str]:
    """**变更集**技能（`review_inbox`）的展开：`params["calls"]` 逐条照抄成调用清单。

    与其余展开函数的根本差别：**这里没有"解释"这一步**。`calls` 里的每一条已经是
    「工具 + 参数」的完整调用（形状与 `content_query.calls` **逐字同构**：模型侧由
    `planner_llm._SCHEMA_OVERRIDES[("review_inbox","calls")]` 给出闭集）——这一层与
    它之间是一条**恒等**通道：不补参、不改写、不排序、不合并。理由是同一条纪律的
    另一面："参数不全时猜一个"在别处只是办不成，在这一族是**拿别人的一句话做裁决**。

    两条形状判据（缺一即**零工具 + 注记**，交 planner 如实收尾）：
      · `calls` 必须是非空列表，每条是 `{"tool": 名字, "args": 非空对象}`；
      · 工具名必须在本技能 `plan` 声明的**全集**里——与 `_confirm_grant_plan` 执行轮
        那条"技能与工具必须对得上"是同一条判据，在这里**先判一次**的理由是：展开
        结果一旦含 plan 之外的工具，令牌签发（按技能名）与执行（按 plan 逐条核对）
        会得出不同结论，而"写操作跑在一份没人预期它会跑的技能名下"是最难查的那类事故。
        这一条同时是**跨族安全性的来源**：模型能在 `calls` 里写的工具名被收在这两件里，
        别的写工具（删留言 / 重置额度 / 发通知…）拼不进来。

    **任何一条不合格 → 整批零工具**（不是"跳过坏的那条"）：卡是为这一批签的，
    少办一件而主人以为全办了，比一件都不办更坏（同 `_drop_correction` 的剔空取向）。
    """
    allowed = {t for t, _ in workflow.plan}
    calls = params.get("calls")
    if not isinstance(calls, list) or not calls:
        return [], ("变更集里没有一条可执行的调用（`calls` 是空的）："
                    "不调用任何工具，如实告诉主人这一轮没有能替他办的事")
    tools: list[str] = []
    bad: list[str] = []
    for s in calls:
        if not isinstance(s, dict):
            bad.append("（有条目不是对象）")
            continue
        name = str(s.get("tool") or "").strip()
        args = s.get("args")
        if not name or not isinstance(args, dict) or not args:
            bad.append(f"「{name or '没有工具名'}」缺参数")
            continue
        if name not in allowed:
            bad.append(f"「{name}」不在本技能的允许清单里")
            continue
        # 调用行的写法是**跨模块契约**：`<工具名>(<JSON>)`，读端是
        # `graph._tool_name` / `_tool_args` 与 `_confirm_grant_plan`（它拼的是同一形状）。
        tools.append(f"{name}({json.dumps(args, ensure_ascii=False)})")
    if bad or not tools:
        return [], ("变更集里有条目不合格（" + "、".join(bad[:5])
                    + "）：不调用任何工具，如实告诉主人这一轮什么都没有办")
    return tools, f"变更集：按清单逐条执行（共 {len(tools)} 件）"


def _expand_write_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """写技能（WRITE_WORKFLOW_NAMES）模板 + 参数 → (TOOLS 行清单, 注记)。

    注记非空**且 tools 为空** = "参数不齐/认不出"的零工具路径（由 planner 去追问）；
    注记非空且有 tools = 正常路径的说明。写成函数是因为这条分支已经装不下：
    八个写技能共用同一套纪律（缺参零工具 / 只落点名的参数 / 归一在确定性层做）。

    **目标一律按名字**（20260921 第四轮）：planner 写「编程」而不是 id——用户嘴里
    说的就是名字，跨轮执行记忆里也只有名字没有编号；名字→id 的解析在工具侧
    确定性完成（`tools.base._find_named_tag`），对不上就零写 + 如实说明。
    """
    name = workflow.name

    def _spec(tool_name: str, args: dict) -> str:
        return f"{tool_name}({json.dumps(args, ensure_ascii=False)})"

    if name == "tag_create":
        names = _tag_names(params)
        if not names:
            return [], ("tag_create 缺少标签名（title）：不调用任何工具，"
                        "如实向主人问清要建的标签叫什么名字")
        # 颜色（20260921）：点名了就**在这里解析成站内色板 hex**——确定性、且只有
        # 这一处知道色板；执行轮（含确认轮）拿到的就是色值，弹窗问句与工具参数都
        # 从同一个值渲染。点名的色认不出 → 零工具 + 注记（**绝不回落到哈希**）。
        color_spec = _write_arg(params.get("color"))
        hexval = A.match_tag_color(color_spec) if color_spec else None
        if color_spec and hexval is None:
            return [], (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                        "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
        pname = _write_arg(params.get("parent_tag"))
        # **一名一 spec**（20261008 批：主人一次点名好几个新标签时不许只建第一个）。
        # 此前这一支恒产出一条 spec，于是"建 Git 和 SVN 两个标签"这种话在契约层就
        # 表达不出来：planner 只能挑一个填进 title，另一个在**没有任何一层**留下痕迹
        # ——实测连着两轮原型一模一样的一张单标签卡（trace `20261008T065806` /
        # `20261008T065826`），主人说"两个你漏了"也没用。现在 `titles` 与 `title`
        # 合并成 N 条同形 spec，N≥2 时下游**现成的**多 spec 通道全部自动接上：确认卡
        # 印成清单（「全部办」/「只办第 N 件」，`render_confirm_text`）、逐条执行、
        # 逐条回执。父标签与颜色是**整批共用**的（主人说「在编程下面建 A 和 B」时
        # 只有一个父标签），所以循环里带着同一份值。
        # 展开层的三条防线一个都没松：名字空 → 上面已零工具；名字已被站里占用 →
        # `adminops.reached_specs` 把**那一条**摘掉（其余照弹卡，不是整批放行）；
        # 名字在主人这句话里找不到出处 → `_ident_grounded` 逐 spec 判、至少一条没据
        # 就退成弹卡给人眼看（见 graph._confirm_popup 的循环）。
        specs: list[str] = []
        for nm in names:
            args: dict = {"title": nm}
            if hexval:
                args["color"] = hexval
            if pname:
                args["parent_tag"] = pname
            specs.append(_spec("create_tag", args))
        # 注记只写**已知事实**：父标签名是 planner 给的，它是不是真存在由工具去核，
        # 这里**不许**替它断言层级（旧注记无条件写「（一级标签）」——参数被吞掉时
        # 那句就成了一个肯定的错误事实，落进执行记忆被下一轮照念）。
        said = f"挂在父标签「{pname}」下" if pname else "一级标签（未给父标签）"
        if len(names) > 1:
            head = ("新建标签" + "、".join(f"「{n}」" for n in names)
                    + f"（共 {len(names)} 个，{said}）")
        else:
            head = f"新建标签「{names[0]}」（{said}）"
        return specs, (
            head
            + (f"，颜色 {A.describe_color(hexval)}" if hexval else "")
            + "；同名已存在时工具会复用而不是重复建")

    if name in ("tag_update", "tag_delete"):
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少标签名（name）：不调用任何工具，"
                        "如实向主人问清说的是哪一个标签")
        lv = _write_arg(params.get("level"))
        args = {"name": target}
        if lv:
            code = A.normalize_level(lv)
            if code is None:
                return [], (f"level「{lv}」认不出来（只支持一级 / 二级）："
                            "不调用任何工具，如实向主人问清")
            args["level"] = code
        if name == "tag_delete":
            what = ("一级" if args.get("level") == "one"
                    else ("二级" if args.get("level") == "two" else ""))
            return [_spec("delete_tag", args)], (
                f"删除{what}标签「{target}」"
                + ("。**这是不可撤销的**：一级标签会连带删掉它下面的二级标签，"
                   "并把这些标签从所有文章上摘掉" if what != "二级" else "。**不可撤销**"))
        bits = []
        new_title = _write_arg(params.get("new_title"))
        if new_title:
            args["new_title"] = new_title
            bits.append(f"改名→{new_title}")
        color_spec = _write_arg(params.get("color"))
        if color_spec:
            hexval = A.match_tag_color(color_spec)
            if hexval is None:
                return [], (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                            "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
            args["color"] = hexval
            bits.append(f"颜色→{A.describe_color(hexval)}")
        pname = _write_arg(params.get("parent_tag"))
        tl = _write_arg(params.get("to_level"))
        if tl:
            code = A.normalize_level(tl)
            if code is None:
                return [], (f"to_level「{tl}」认不出来（只支持一级 / 二级）："
                            "不调用任何工具，如实向主人问清")
            args["to_level"] = code
        if pname:
            if args.get("to_level") == "one":
                return [], ("既说要挪到某个父标签下、又说要改成一级标签——两者矛盾："
                            "不调用任何工具，如实向主人问清到底要哪一种")
            args["parent_tag"] = pname
            bits.append(f"移到「{pname}」下面")
        if args.get("to_level") == "one":
            bits.append("改成一级标签")
        elif args.get("to_level") == "two":
            bits.append("改成二级标签（需同时给父标签名）")
        if not bits:
            return [], ("tag_update 没有指出要改什么（new_title / color / parent_tag / "
                        "to_level）：不调用任何工具，如实向主人问清要改成什么")
        return [_spec("update_tag", args)], f"修改标签「{target}」：{'、'.join(bits)}"

    if name in ("category_create", "category_update", "category_delete"):
        if name == "category_delete":
            cname = _write_arg(params.get("name"))
            if not cname:
                return [], ("category_delete 缺少分类名（name）：不调用任何工具，"
                            "如实向主人问清说的是哪一个分类")
            return [_spec("delete_category", {"name": cname})], (
                f"删除分类「{cname}」（文章不会被删，它们会变成没有分类）")
        if name == "category_create":
            title = _write_arg(params.get("title"))
            if not title:
                return [], ("category_create 缺少分类名（title）：不调用任何工具，"
                            "如实向主人问清要建的分类叫什么")
            args = {"title": title}
            bits = [f"新建分类「{title}」"]
        else:
            cname = _write_arg(params.get("name"))
            if not cname:
                return [], ("category_update 缺少分类名（name）：不调用任何工具，"
                            "如实向主人问清说的是哪一个分类")
            args = {"name": cname}
            bits = [f"修改分类「{cname}」"]
        color_spec = _write_arg(params.get("color"))
        if color_spec:
            # 分类用宽一档的色表（任意 6 位 hex 也收——前端色块白名单已放宽）
            picked = A.match_any_color(color_spec)
            if picked is None:
                return [], (f"color「{color_spec}」认不出来（中文色名或 6 位色值如 #eb2f96）："
                            "不调用任何工具，如实向主人问清")
            args["color"] = picked
            color_shown = A.describe_color(picked) if picked in A.NEW_TAG_COLORS else picked
            bits.append(f"颜色→{color_shown}")
        for key in ("new_title", "path_name", "introduce", "icon"):
            val = _write_arg(params.get(key))
            if val:
                args[key] = val
                bits.append(f"{key}→{val}")
        if name == "category_update" and len(bits) == 1:
            return [], ("category_update 没有指出要改什么（new_title / path_name / "
                        "introduce / icon / color）：不调用任何工具，如实向主人问清")
        # **技能名 ≠ 工具名**：分类三件的技能名是 `category_*`、工具名是 `*_category`
        # （建标签那两件恰好同名，所以这里一旦顺手写 name 就会只有分类两件坏掉）。
        # 写错的代价不是"少做一件事"而是 execute 的"未知工具"错误帧——用户看到的
        # 是一句"创建失败"的系统报错（20260922 全量 golden 实测：admin_category_create_popup
        # 整轮 FAIL、零弹窗）。锁：test_tag_admin 的工具名 ∈ 注册表 那条。
        tool_name = "create_category" if name == "category_create" else "update_category"
        return [_spec(tool_name, args)], "、".join(bits)

    if name.startswith("announcement_"):
        # 公告（20260922 第五轮）：目标按**标题**指认。正文**原样透传**——这一层只做
        # "空/非空"的判断，一个字都不改。
        # ⚠️ "正文该由谁成文"（20261006 改口径）：**两种情形分开**——主人明确给了原文
        # （「正文写：…」）⇒ 照录，一个字的改动都算改了他的意思（由
        # `graph._announcement_text_fix` 校正回原话）；只给了意思（「发个公告祝大家
        # 国庆快乐，以你的口吻」）⇒ **由 planner 按他的意思组织措辞**，这一层原样放行。
        # 这里原先写的是"正文原样透传、一个字都不改……替主人润色等于替他发言"，
        # 那句话**把只给意思的那种也一起禁了**：现场代价见 trace `20261001T061023`
        # ——主人点名要"以你的口吻"，模型照旧只回声一句「祝大家国庆节快乐！」，
        # 主人下一句就是「太干巴了，而且我要求以你的身份」。
        title = _write_arg(params.get("title"))
        if not title:
            return [], (f"{name} 缺少公告标题（title）：不调用任何工具，"
                        "如实向主人问清是哪一条公告（新建时问清标题叫什么）")
        if name == "announcement_delete":
            return [_spec("delete_announcement", {"title": title})], (
                f"删除公告「{title}」（删掉后首页立刻看不到，取不回来）")
        if name == "announcement_create":
            content = _write_arg(params.get("content"))
            if not content:
                # **槽位空着就不许往下走**：正文由 planner 成文（可以润色），但不能是空。
                return [], ("announcement_create 缺少公告正文（content）：不调用任何工具，"
                            "如实向主人问清公告要写什么")
            return [_spec("create_announcement", {"title": title, "content": content})], (
                f"发布公告「{title}」（正文**全文**会印在确认卡上由主人核对，"
                "他点了确定才真发出）")
        new_title = _write_arg(params.get("new_title"))
        content = _write_arg(params.get("content"))
        if not new_title and not content:
            return [], ("announcement_update 没有指出要改什么（new_title / content）："
                        "不调用任何工具，如实向主人问清要改标题还是正文")
        args = {"title": title}
        bits = []
        if new_title:
            args["new_title"] = new_title
            bits.append(f"改名为「{new_title}」")
        if content:
            args["content"] = content
            bits.append("改正文")
        return [_spec("update_announcement", args)], f"修改公告「{title}」：{'、'.join(bits)}"

    if name == "board_audit":
        # 河灯留言复核（20260929 批 H 起按 **talkId**）：编号是系统给的、模型只负责
        # 原样抄 ⇒ 这里只做**归一**（`talkId:101` / `"101"` / `101` / `#101` 都收），
        # 解不出就零工具 + 追问。行是否存在由 `graph._ledger_target_refusal` 现场读
        # 台账判（比"这段字面出自主人原话"更强：可验证、不可能编造）。
        # **那一行是什么状态不参与判**（20260930：撤掉了"必须还在待审"那条，主人
        # 要能驳回已经通过的留言）——"这一下是不是他要的"由人闸回答（恒弹卡）。
        tid = A.normalize_target_id(params.get("talk_id"))
        if tid is None:
            return [], (f"board_audit 缺少指认用的留言编号（talk_id，给的是"
                        f"「{_write_arg(params.get('talk_id')) or '空'}」）：不调用任何"
                        "工具，如实向主人说明要先把那条留言的编号读出来"
                        "（台账帧或审核状况明细里印着 `talkId:<编号>`，原样抄那个数字）")
        verdict = A.normalize_verdict(params.get("verdict"))
        if verdict is None:
            return [], (f"verdict「{params.get('verdict')}」认不出来（只能是通过/pass "
                        "或驳回/reject）：不调用任何工具，如实向主人问清要放行还是驳回")
        return ([_spec("audit_board_comment", {"talk_id": tid, "verdict": verdict})],
                (f"把留言 talkId:{tid} 人工复核为{A.BOARD_VERDICT_CN[verdict]}"
                 "（卡面会印出那条留言的原文与作者，由主人核对）"))

    if name == "board_delete":
        # 河灯留言删除（20260922 第六轮）：**目标仍按正文片段**指认——主人指一条
        # 要删的留言时说的是那句内容（"把那条写广告的删了"），不是编号；改按编号会
        # 逼他多报一个数字，而正文片段撞车时工具侧零写、不会选错。审核那件不同：
        # 它要记的是"哪一行、改成了什么结论"，结论必须落在编号上才不会串行。
        # quote 原样透传——它是模型从留言原文里抄的一段，**不许在这里改写/截断**：
        # 片段越碎越容易撞到别的留言（工具侧命中多条会零写，不会选错）。
        quote = _write_arg(params.get("quote"))
        if not quote:
            return [], (f"{name} 缺少指认用的正文片段（quote）：不调用任何工具，"
                        "如实向主人问清说的是哪一条留言（或先去后台审核状况里把"
                        "那几条列出来让他指认）")
        return [_spec("delete_board_comment", {"quote": quote})], (
            f"删除含「{A.clip(quote, 20)}」的那条河灯留言（删掉取不回来）")

    if name in ("account_freeze", "account_unfreeze"):
        # 账号冻结 / 解冻（20260926 第九轮）。它与上面那几件同族（目标是一个
        # **名字**、解析在工具侧对着实时名录做），但两条注记的措辞要更硬一档：
        # 这一族说错话的代价是**把一个活人踢下线**（不是"标签建歪了"）。
        #   · 缺名字 ⇒ 明写"不要拿你猜的名字顶上"——这一族唯一的定位方式就是名字；
        #   · 纯数字 ⇒ 后台账号列表里只有名字，没有编号可写（工具**没有** user_id
        #     参数：列表不列超管那一行，那条防线只在"定位必须经过列表"时成立）。
        # 方向由**技能名**定死而不是参数：`frozen: bool` 那种参数在
        # `graph._confirm_grant_plan` 的"工具 ⊆ 技能 plan"判据下看不见——卡上写
        # 「冻结」、实际执行解冻，且无人可见。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        is_freeze = name == "account_freeze"
        return ([_spec("freeze_account" if is_freeze else "unfreeze_account",
                       {"name": target})],
                (f"{'冻结' if is_freeze else '解冻'}后台账号「{target}」"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))

    if name in ("mute_account", "unmute_account"):
        # 禁言 / 解除禁言（20261002 内容风控下放给 agent）。与冻结族同走名字通道、
        # 同样两条硬注记（缺名字不许猜 / 编号通道不存在），差别在**多一个 Duration
        # 参数**（只有禁言方向有）：
        #   · **归一放在这一层，且认不出就零工具**（同 `account_set_role` 的"认不出的
        #     身份"）：主人说的是「三天」，进 TOOLS 行/进卡面的必须是同一个归一结果；
        #     归不出就把问题问回去，**绝不许静默按永久办**——那会把「禁他三天」办成
        #     永久禁言，而卡面与实际同源（都走这一份归一），两边都看不出来。
        #   · 解禁方向**没有时长这回事**：多填的 hours 一律丢掉（不是宽容，是这件事
        #     没有那个维度；带进工具会让后端收到一个它明确忽略的字段）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        if name == "unmute_account":
            return ([_spec("account_unmute", {"name": target})],
                    (f"解除后台账号「{target}」的禁言"
                     "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                     "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))
        raw_hours = params.get("hours")
        ok_hours, hours_n = A.normalize_mute_hours(raw_hours)
        if not ok_hours:
            return [], (f"{name} 给的禁言时长「{A.clip(str(raw_hours or ''), 20)}」"
                        "认不出是多少小时：不调用任何工具，如实向主人问清要禁多久"
                        "（例如几个小时、几天，或者说「永久」）"
                        "——**不要**自己挑一个时长顶上，也**不要**当成永久")
        span = A.mute_span_cn(hours_n)
        args = {"name": target} if hours_n is None else {"name": target, "hours": hours_n}
        return ([_spec("account_mute", args)],
                (f"禁言后台账号「{target}」"
                 f"（期限：{span}）"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**；"
                 "禁言不踢人下线，他照常登录浏览、只是发不出评论与留言"))

    if name == "account_set_role":
        # 变更账号身份（20261002：下放给管理员 + agent 代理）。与上面冻结族同走名字
        # 通道、同样两条硬注记（缺名字不许猜 / 编号通道不存在），差别在两处：
        #   · **多一个必填的取值参数 `role`**——它不是"要写进去的值"（那种值走
        #     `_WRITE_VALUE_FIELDS` 的逐字闸），而是一次**归一**：主人说「杂鱼」
        #     与「zako」是同一件事，而进 TOOLS 行的必须是后端认的身份码。归一放在
        #     这一层而不是工具里（工具那层也做一次，兜直接调用），是因为**卡面与
        #     TOOLS 行必须同源**：卡上写「改成杂鱼」而参数是别的东西，主人签的
        #     就不是执行的那一件。
        #   · **认不出的身份零工具**（同「缺名字」那条）：让一个认不出的字面落到
        #     后端，主人会在卡上看到一句他没说过的身份；而这里能说的是一句真话
        #     ——站内没有这个身份。
        # ⚠️ 身份**只在这里归一，权限一个字都不判**：能改谁、能改成什么由后端
        #    `authz::check_role_change` 一项判据说了算（用户拍板：agent 侧不写
        #    第二份权限表）。上面那条硬注记的方向也据此改了措辞——"管理员只能改
        #    两档"这类话**不许**写进 agent 的任何文案里：它是后端的原话，agent
        #    复述一遍就会在政策变更那天变成假话（同 `_policy_post` 那条纪律）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        want = _write_arg(params.get("role"))
        if not want:
            return [], (f"{name} 缺少目标身份（role）：不调用任何工具，"
                        "如实向主人问清要把「" + target + "」改成什么身份")
        code = A.normalize_role(want)
        if code is None:
            return [], (f"{name} 的目标身份「{want}」站内没有：不调用任何工具，"
                        "如实向主人说明没有这个身份**并请他换一个说法**"
                        "——**不要**自己挑一个身份顶上")
        return ([_spec("set_account_role", {"name": target, "role": code})],
                (f"把后台账号「{target}」的身份改成{A.role_cn(code)}"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))

    if name == "notice_send":
        # 给单个账号发通知（20260926 第十一轮）。三件与上面两支不同的事：
        #   · **正文是必填的**（缺了零工具 + 问清要发什么——**绝不替主人编一句**：
        #     那是拿他的名义对第三方说话，收不回）；
        #   · 正文**允许整理**（用户拍板），所以这里只挡长度、不做"必须逐字"的校验
        #     ——那一层刻意不接（`graph._WRITE_VALUE_FIELDS` 的注写了为什么）；
        #   · `title` **可留空**（服务端会落一个系统默认标题），点名了才进 args。
        # 缺收件人名字那一路的措辞与冻结族同硬（名字是唯一通道，不许猜、不许用编号）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], ("notice_send 缺少收件账号名（name）：不调用任何工具，"
                        "如实向主人问清这条通知要发给**哪一个账号**"
                        "——**不要**拿你猜的名字顶上，也不要用账号编号")
        if target.isdigit():
            return [], ("notice_send 给的是一串数字「" + target + "」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        content = _write_arg(params.get("content"))
        if not content:
            return [], ("notice_send 缺少正文（content）：不调用任何工具，"
                        "如实向主人问清这条通知要说什么——**不要**替他写一句："
                        "那段话会以他的名义发给对方，且发出后没有撤回的通道")
        if len(content) > _NOTICE_CONTENT_LIMIT:
            return [], (f"notice_send 的正文太长（{len(content)} 字，上限 "
                        f"{_NOTICE_CONTENT_LIMIT} 字）：不调用任何工具，"
                        "如实向主人说明太长、请他说短一点")
        ph = _notice_placeholder(content)
        if ph:
            # 20260926 真机实测（本技能上线当天，10 次采样）：planner 有 2 次把正文
            # 填成了**系统自己的字**——一次 `notice_send`（技能名）、一次
            # `[当前登录用户]`（模板占位符），另有几次把主人的话整个换成一句通用语。
            # 正文是"以主人的名义发给第三方、且发出去删不掉"的那一段 ⇒ 占位符必须
            # 在**展开这一层**就挡下来（零工具 + 交回 planner 重决策；这里只认机器能
            # 保证的形态，不替它判断"整理得好不好"——那一维靠弹卡给人眼看）。
            return [], (f"notice_send 的正文看起来是**占位符**（{ph}）：不调用任何工具，"
                        "重新决策——正文要写主人这轮真正要说的那句话（可以按他的意思"
                        "整理成一句得体的通知，但**不能**拿工具名/技能名/`[这样]`的"
                        "占位符顶上，也不能整个换成一句与主人原话无关的通用语）")
        title = _write_arg(params.get("title"))
        if title and len(title) > _NOTICE_TITLE_LIMIT:
            return [], (f"notice_send 的标题太长（{len(title)} 字，上限 "
                        f"{_NOTICE_TITLE_LIMIT} 字）：不调用任何工具，"
                        "如实向主人说明太长、请他给短一点的标题")
        args = {"name": target, "content": content}
        if title:
            args["title"] = title
        return ([_spec("send_user_notice", args)],
                (f"给后台账号「{target}」发一条站内通知"
                 f"（标题「{title or '站内通知'}」）"
                 "——**收件人的账号名必须能在后台账号列表里看到**；"
                 "正文会印在确认卡上由主人核对**全文**（发出后没有撤回的通道）"))

    if name in ("quota_approve", "quota_reject"):
        # 队列里的那两件（20260929 批 H 起按 **user_id**）：它们治的是"申请表里等着
        # 办的那一行"，编号由系统印在台账帧与 `list_quota_requests` 的行上
        # （`账号 id=<编号>`），模型只负责原样抄。**账号 id 与账号名只在这一支上
        # 分家**——`quota_reset` 仍按名字，那一件不需要他申请过、也就不在任何队列里
        # （见技能定义上的注）。
        #   · 解不出编号 ⇒ 零工具 + 追问（**响亮**，不当"没填"）：工具侧已经没有名字
        #     通道了，拿一个名字去猜 uid 等于把额度清给另一个人。
        # 方向由**技能名**定死而不是参数（同冻结族）：`approved: bool` 那种参数在
        # `graph._confirm_grant_plan` 的"工具 ⊆ 技能 plan"判据下看不见——卡上写
        # 「批准」、实际执行驳回，且无人可见。
        raw = params.get("user_id")
        uid = A.normalize_target_id(raw)
        if uid is None:
            said = _write_arg(raw)
            return [], (f"{name} 缺少申请人的账号 id（user_id，给的是「{said or '空'}」）："
                        "不调用任何工具，如实向主人说明要先把申请队列读一次、"
                        "把申请人那一行的编号原样抄下来"
                        "（台账帧或申请队列的行上印着 `账号 id=<编号>`；"
                        "这一件**不认账号名**）")
        if name == "quota_reject":
            # 驳回理由**必填**，而且与 `notice_send` 的正文同一条纪律：它是**以主人
            # 的名义**发给申请人的那句话（会原样进站内通知）。主人没说就问他要——
            # **绝不替他编一句**（申请人收到的是主人的裁决，理由写错比不写更糟）。
            reason = _write_arg(params.get("reason"))
            if not reason:
                return [], ("quota_reject 缺少驳回理由（reason）：不调用任何工具，"
                            "如实向主人问一句为什么驳回——**不要**替他写一个理由："
                            "那条理由会以他的名义发给申请人")
            if len(reason) > _QUOTA_NOTE_LIMIT:
                return [], (f"quota_reject 的理由太长（{len(reason)} 字，上限 "
                            f"{_QUOTA_NOTE_LIMIT} 字）：不调用任何工具，"
                            "如实向主人说明太长、请他说短一点")
            ph = _notice_placeholder(reason)
            if ph:
                return [], (f"quota_reject 的理由看起来是**占位符**（{ph}）："
                            "不调用任何工具，重新决策——理由要写主人这轮真正给的"
                            "那一句（不能拿技能名/工具名/`[这样]`的占位符顶上）")
            return ([_spec("reject_quota_request", {"user_id": uid, "reason": reason})],
                    (f"驳回账号 id={uid} 的对话额度重置申请（额度不变，"
                     f"但会给他发一条站内通知、理由「{A.clip(reason, 30)}」"
                     "——**发出后没有撤回的通道**；卡面会印出申请人是谁）"))
        return ([_spec("approve_quota_request", {"user_id": uid})],
                (f"批准账号 id={uid} 的对话额度重置申请（他的额度恢复到上限、"
                 "马上能继续提问；**批准不可撤销**；卡面会印出申请人是谁）"))

    if name == "quota_reset":
        # **主动**重置（20260929）：与上面两件不同——它不需要对方申请过，因此目标
        # 不在队列里、没有系统印的编号可抄 ⇒ 仍走**账号名**通道（与冻结/通知族同）。
        #   · 缺名字 ⇒ 明写"不要拿你猜的名字顶上"（名字是唯一通道）；
        #   · 纯数字 ⇒ 后台账号列表里只有名字，没有编号可写（工具**没有** user_id
        #     参数：账号族不列超管那一行，那条防线只在"定位必须经过列表"时成立）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], ("quota_reset 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要重置的是哪一个后台账号——**不要**拿你猜的"
                        "名字顶上，也不要用账号编号")
        if target.isdigit():
            return [], (f"quota_reset 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "这一件不按编号操作账号）")
        return ([_spec("reset_user_quota", {"name": target})],
                (f"**主动**把后台账号「{target}」的对话额度恢复满额"
                 "（**不需要他申请过**的一次重置，他那边不会收到任何提示；"
                 "恢复满额不可撤销）"
                 "——**要动的那个名字必须能在后台账号列表里看到**"))

    return [], f"{name}：未知的写技能（不调用任何工具）"
