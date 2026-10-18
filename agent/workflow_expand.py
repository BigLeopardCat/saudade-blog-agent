# -*- coding: utf-8 -*-
"""技能模板的**通用展开层**：`Workflow` + 参数 → （TOOLS 行清单, 注记）。

`instantiate_plan` 把 planner 选中的技能与它填的参数翻成一条条可执行调用，而"怎么翻"
按技能的**靶通道**分族。住在本模块的是：用户自己数据的（`_expand_own_workflow`）、
后台首页待办三件（`_expand_todo_*`）、文章状态那一半（`_expand_article_status`），
加上它们共用的原语（id 与清单归一：`_norm_pos_int` / `_norm_id_*` / `_norm_true` /
`_pos_int_list`）与收尾后缀（`_workflow_no_calls_suffix`）。

**写技能那一族（`_expand_write_workflow` / `_expand_change_set` / `_write_arg` …）在
`agent/workflow_expand_write.py`**（20261017 批 2 刀 2d-2 为模块预算再切的一刀）：
本模块从那里 import `_write_arg`——`_expand_todo_*` 拼调用清单时要用它。依赖是单向的，
那边一条边都不指向本模块。

**为什么单独成模块**（20261017 批 2 刀 2d）：这一族**一条出边都没有**——只读技能自己的
`plan`/`inputs`、注册表（`WORKFLOW_MAP`）、工具层的上限常量与 `agent.adminops` 的纯
函数，**不调用本文件里留下的任何名字**（`instantiate_plan` 反过来调它们，那是**入边**，
靠 `agent/workflows.py` 的转发层收着）。"出边为零"是这一轮拆分的唯一选刀判据：新模块
不许 import 回 `agent/workflows.py`（那会成环），而入边只要转发层还在就无所谓。

⚠️ 与 `agent/workflow_params.py`（刀 2c）的分工：那边管**事前校验与规格派生**（"参数
齐不齐、值归得了吗、菜单怎么印"），这边管**值 → 调用清单**。两者都读技能模板，但谁也
不 import 谁——`workflow_params` 的消费方是菜单与校验，本模块的消费方是实例化。

⚠️ 与 `_instantiate_plan` 里那几支的分工：本模块的函数**只管展开**，不含"参数不齐怎么
办"的追问话术——缺参/认不出时它们返回空清单 + 一句原因，由 `instantiate_plan` 决定那是
`param_missing` 还是照常执行。
"""

from __future__ import annotations

import json

import agent.adminops_tags as _tags
import agent.adminops_todo as _todo
from agent.arg_refs import is_ref
from agent.workflow_expand_write import _write_arg
from agent.workflow_model import DROP_SUFFIX_WORKFLOW_NO_CALLS
from tools.base import _TODO_TEXT_LIMIT, _as_bool

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = []

# 本模块**刻意不写 `__all__`**：整个面都是 `_` 前缀的内部件（展开层只被 `instantiate_plan`
# 与各展开分支互相调用）。按本仓"`__all__` 只列公开名"的口径（见 `agent/workflow_model.py`
# 与 `agent/workflow_defs/nav_map.py`），这里能列的只有空表——而空表会被读成"导出的东西一个
# 都没有"，与事实相反：跨模块 import 走的是显式名字（`from agent.workflows import
# _norm_id_list` 等），显式 import 从来不看 `__all__`。


def _norm_pos_int(value) -> int | None:
    """实参 → 正整数；否则 None（缺参守卫用，与 tools.base._as_article_id 同判据）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


# 数字清单的**定格式**（见 `_pos_int_list`）：剥掉成对的括号/引号，再按逗号/顿号/
# 分号/斜杠/空白切分。这两条是**标点级**的封闭类，不是"中文词表"——它们拆的是
# **固定格式**的文本，与 `agent/provenance.py::parse_evidence` 收五种形状是同一条
# 纪律（形状漂移的代价不该由主人承担）。挂在变量上而不是塞进集合字面量里，也顺带
# 不惊动 `eval/wordlist_gate.py` 的穷举量（它数的是"住在集合里的中文串"，标点不算）。
_LIST_WRAP = "[](){}<>（）【】「」『』“”\"'` \t\r\n"


_LIST_BREAK = ",;|/、，；\t \r\n"


def _pos_int_list(value) -> tuple[list[int], list[str]]:
    """实参 → （**去重保序**的正整数表，**认不出的原样片段**）。

    多篇形态用（`article_status.article_ids`）。两类实参都收：真数组，或**一段写着
    清单的文本**——后者是模型交上来的常见形状（20261015 生产 trace
    `20261015T080030` 实证：`article_ids` 填成 `"[54, 13, 11, 10]"`），从前一律丢掉
    ⇒ 只算 `article_id` 那一篇、其余几篇**不留任何痕迹**（20261009「剩下的篇不留
    痕迹」换了一张脸）。文本形态按上面的**定格式**解析，认不出的片段一个都不猜。

    **认不出的必须回传给调用方**：旧签名两种情况都返回空表，而"没给"与"给的东西
    一个都认不出"**不是同一件事**——调用方那条 `not aids` 守卫会把后一种也当成"缺
    id"，于是多篇请求静默降级成单篇。回传片段让调用方**响亮地零工具问清**（同
    `_norm_pos_int` 的 None：同一个语义只有一种表示，这是 20260928 那条教训的另一半）。
    """
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for x in value:
            n = _norm_pos_int(x)
            if n is not None and n not in out:
                out.append(n)
        return out, []
    if isinstance(value, bool) or value is None:
        return [], []
    text = str(value).strip()
    if not text:
        return [], []
    flat = text.strip(_LIST_WRAP).strip()
    one = _norm_pos_int(flat)
    if one is not None:
        return [one], []          # 单个值（`"22"` / `"[22]"`）——与旧行为逐字节同
    for ch in _LIST_BREAK:
        flat = flat.replace(ch, " ")
    out2: list[int] = []
    bad: list[str] = []
    for piece in flat.split():
        n = _norm_pos_int(piece.strip(_LIST_WRAP))
        if n is None:
            if piece not in bad:
                bad.append(piece[:20])
        elif n not in out2:
            out2.append(n)
    return out2, bad


def _norm_id_or_ref(value):
    """实参 → 正整数 **或** 参数引用字面量（`$list_notes[0].noteId`）；都认不出 → None。

    为什么 id 位置也要认引用（20260923 批 7）：planner 提示词规则 3b 说得很死——
    "只要某个参数的值来自本轮已执行工具的返回，就**一律优先用引用**"。它照办时写
    `{"article_id": "$search_notes[0].noteId"}`，而 `_norm_pos_int` 只认数字 ⇒ 一条
    合法的计划被当成"缺参"退回追问（步骤白跑一轮）。取值由 `agent/arg_refs.py` 在
    execute 调用前完成（解析失败给原因码，走既有 blocker 链路），这一层只负责
    **认得出那是个引用**、原样透传。

    反面教训（20260921 复盘）：写技能把"解不出的引用"当"没填"是**静默降级**——
    能力看似在、实际永远走不到。这里的态度是"认得就放行，认不得就响亮地问"。
    """
    if is_ref(value):
        return str(value).strip()
    return _norm_pos_int(value)


def _norm_id_list(value) -> list[int]:
    """实参 → 正整数 id 列表（去重保序；认不出的项丢掉——**绝不猜编号**）。

    与 `tools.base._as_ids` 同判据（那一层还要再收一遍）：plan 是模型产出的文本，
    `ids` 可能是 `[7, "8"]`、`"7"`、`None`，甚至一句自然语言（丢掉 → 空列表 →
    零工具追问，方向与全表一致）。
    """
    if value is None or isinstance(value, bool):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for it in items:
        n = _norm_pos_int(it)
        if n is not None and n not in out:
            out.append(n)
    return out


def _norm_true(value) -> bool:
    """实参 → 是不是"全部"（认不出 = 不是）。词表 = `tools.base._as_bool`（20261018 A2）。"""
    return _as_bool(value) is True


def _expand_own_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """用户**自己**的数据写技能（20260923 批 7）→ (TOOLS 行清单, 注记)。

    与 `_expand_write_workflow` 分开写而不是塞进去：这几件的差别不在措辞而在**参数
    形状**——收藏两件是"一个 id"，标记已读是"一串 id 或者'全部'"这个**二选一**，
    且二选一里"都没给"必须**零工具**（不能默认 all=true：那是替主人把全部未读
    一次清掉，而这一步**不可逆**）。塞进那个函数只会让"名字通道"那套判据在这里
    变成恒假的噪声。

    注记非空 + tools 为空 = "参数不齐"的零工具路径（planner 据此去追问）；
    两头都非空 = 正常路径的说明。
    """
    name = workflow.name

    def _spec(tool_name: str, args: dict) -> str:
        return f"{tool_name}({json.dumps(args, ensure_ascii=False)})"

    if name in ("favorite_add", "favorite_remove"):
        aid = _norm_id_or_ref(params.get("article_id"))
        if aid is None:
            return [], (f"{name} 缺少文章 id（article_id）：不调用任何工具，"
                        "如实向主人问清是哪一篇文章；若不知道 id，"
                        "先用 search_notes / list_notes 读回确切 id，"
                        "或用参数引用（$<工具名>[<序号>].<字段名>）让系统去取"
                        "（取消收藏还可以先用 list_my_favorites 看他收藏夹里有哪几篇）")
        # 注记里不回显引用语法（plan 文本会进 narrator 的系统提示——规则 3b 明确要求
        # 别把这段语法展示出来；取值由 refs 层完成，叙述层只需要知道"是上一步那篇"）。
        shown = "上一步读到的文章" if is_ref(aid) else f"文章 {aid}"
        if name == "favorite_add":
            return [_spec("add_favorite", {"article_id": aid})], (
                f"把{shown}收进**他自己**的收藏夹（已经收藏过时工具会如实说明，"
                "不会重复收藏；不改文章的公开状态）")
        return [_spec("remove_favorite", {"article_id": aid})], (
            f"把{shown}从**他自己**的收藏夹里去掉（本来就没收藏时工具会如实说明）")

    if name in ("notice_read", "message_read"):
        # 两件同一形状（"一串 id 或者全部"这个二选一，都没给就零工具追问）；
        # 差别只在**物件**与措辞：通知 vs 收到的信（且信只有收到的能标）。
        thing = "通知" if name == "notice_read" else "站内信"
        read_tool = "read_notifications" if name == "notice_read" else "read_messages"
        list_tool = "list_notifications" if name == "notice_read" else "list_my_messages"
        ids = _norm_id_list(params.get("ids"))
        want_all = _norm_true(params.get("all"))
        if want_all and ids:
            return [], (f"{name} 同时给了 all 和具体 id（一个说\"全部都标\"、"
                        f"一个说\"就这几条\"）：不调用任何工具，如实向主人问清要标哪一些")
        if want_all:
            return [_spec(read_tool, {"all": True})], (
                f"把**全部**未读的{thing}标记为已读（不可逆：标的就不再是未读）")
        if ids:
            shown = "、".join(str(i) for i in ids)
            return [_spec(read_tool, {"ids": ids})], (
                f"把{thing} {shown} 标记为已读（不可逆：标的就不再是未读）；"
                f"id 没对上的（不在他收件箱里 / 本来就不存在）会如实说明")
        # 既没给 id 也没说"全部"：**零工具**，问清楚再来（见函数头注：默认全部
        # 等于替主人做一次不可逆的操作）。
        return [], (f"{name} 没有指出要标记哪些{thing}（ids 与 all 都没给）："
                    f"不调用任何工具，如实向主人问清是把**全部**未读标掉、还是只标某几条"
                    f"（若是某几条，先用 {list_tool} 读出它们的 id 再回来）")

    return [], f"{name}：未知的写技能（不调用任何工具）"


def _expand_todo_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """后台首页待办 / 日程（20260926 第八轮）→ (TOOLS 行清单, 注记)。

    与 `_expand_write_workflow` 分开写（同 `_expand_own_workflow` 的理由）：那两套的前提
    都是"目标能在站内核对"——一个按名字解析 id，一个认 article_id / 通知 id。这一件
    的目标是主人随口说的一件事，**站内没有东西可核对**；把它塞进名字通道，只会让
    `_owner_target_span` 那套"目标名必须能从主人原话里抽出来"的判据作用在一段自由
    文本上（那套判据是为了防"模型自己编了一个目标名"，而这里正文本来就该是主人的话
    ——**判据错位比没有判据更糟**：它会把好参数判红）。

    判据只剩两条，都是"缺了就不写"：
      · 正文空 → 零工具 + 注记（"记一下"三个字本身就是正文时，那是主人的口误，
        不是一件待办）；
      · 排期翻不出来 → 零工具 + 注记，**绝不挑一个日子顶上**（翻成什么由
        `A.normalize_due_date` 一处决定；这里翻出来的值同时进 TOOLS 行、确认框与
        令牌载荷，三处是同一个值）。
    超长也在这里挡（**不截断**：截断等于替主人改字，同 Rust 侧的取舍）。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_add 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清要记的是哪一件事；**不要**拿他这句话本身当正文猜一个")
    if len(text) > _TODO_TEXT_LIMIT:
        # 上限与服务端同源（tools/base.py 的 `_TODO_TEXT_LIMIT` = Rust MAX_TEXT_CHARS）；
        # 这里挡一道只是为了"零写 + 能说清原因"，服务端那一道才是判据。
        return [], (f"dashboard_todo_add 的正文太长（{len(text)} 字，上限 {_TODO_TEXT_LIMIT} 字）："
                    "不调用任何工具，如实请主人把这件事说短一点（**不许**替他截断）")
    raw = _write_arg(params.get("date"))
    due = _todo.normalize_due_date(raw) if raw else None
    if raw and due is None:
        return [], (f"dashboard_todo_add 的排期日「{raw}」认不出来"
                    f"（只认 {_todo.DUE_DATE_FORMS_CN}）：不调用任何工具，"
                    "如实向主人问清是哪一天——**不许**自己挑一个日子顶上")
    args: dict = {"text": text}
    if due:
        args["date"] = due
    return ([f"create_dashboard_todo({json.dumps(args, ensure_ascii=False)})"],
            (f"往他**自己**后台首页的待办里加一条「{_tags.clip(text, 20)}」"
             + (f"，排期 {_todo.due_date_cn(due)}" if due else "（未排期）")
             + "；只追加这一条，列表里原有的都不动"))


def _expand_todo_done_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """待办「勾完成」（20260926 第十轮）→ (TOOLS 行清单, 注记)。

    与 `_expand_todo_workflow`（追加一条）共用"目标是自由文本"这个前提，**但判据更少**：
    那一件还要多一道"排期翻不翻得出来"（它有 date 参数），这一件只认 text。剩下的
    两条都是"缺了就不写"：

      · 正文空 → 零工具 + 注记（"勾一下"三个字里没有可勾的对象，去问主人）；
      · 正文超长 → 零工具 + 注记（**不截断**，同 add 的取舍：截断等于替主人改字，
        而这里改字会让"同一行"变成"另一行"）。

    **计划层不判"这一条在不在列表里"**：那份列表在这一层读不到（它要经弹卡那一轮
    惰性读一次，见 `graph._confirm_popup`），而把"查无此条 ⇒ 零工具"写在这里会把
    "读不到列表"与"列表里没有这一条"混成同一件事——前者是系统读不到，后者是主人
    说错了正文。真正的定位判据在工具侧对着**实时**列表做一次，那一次同时是写前读。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_done 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清指的是哪一条（可先选 dashboard_todo_list 把列表读出来"
                    "给他挑，**不要**替他挑一条）")
    if len(text) > _TODO_TEXT_LIMIT:
        return [], (f"dashboard_todo_done 的正文太长（{len(text)} 字，"
                    f"上限 {_TODO_TEXT_LIMIT} 字）：不调用任何工具，"
                    "如实请主人照列表里的写法说短一点（**不许**替他截断）")
    return ([f"complete_dashboard_todo({json.dumps({'text': text}, ensure_ascii=False)})"],
            (f"把他自己后台首页的待办里「{_tags.clip(text, 20)}」那一条勾成完成"
             f"（只翻完成标记，正文与排期都不动）"))


def _expand_todo_reschedule_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """改某一条待办的排期日（20260929 批 G）→ (TOOLS 行清单, 注记)。

    与 `_expand_todo_done_workflow` 同一族（目标是一段自由文本，站内没有东西可核对，
    连"这一条在不在列表里"都要到工具侧对着实时列表判），差别只有一处：**日期是
    这一件事的全部内容**，所以它必须说清楚。而"说清楚"要分**三**种处境，别混：
      · 契约词「未排期」⇒ **清空**（主人说"不用排期了/把日子清掉"就是它，见
        `adminops._TODO_CLEAR_WORD`；这是与"没说日子"完全不同的第三态）；
      · 说了日子、但翻不出来 ⇒ 零写 + 追问（**绝不挑一天顶上**——错一天的日程会
        静静地躺在后台日历的错误格子里）；
      · **压根没说**日子 ⇒ 零写 + 追问，问的是另一句（"要改到哪一天"）。
    三种出口的注记各不相同：planner 同轮纠偏读的就是这段正文（`graph.param_correct`）。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_reschedule 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清改的是哪一条（可先选 dashboard_todo_list 把列表读出来"
                    "给他挑，**不要**替他挑一条）")
    if len(text) > _TODO_TEXT_LIMIT:
        return [], (f"dashboard_todo_reschedule 的正文太长（{len(text)} 字，上限 "
                    f"{_TODO_TEXT_LIMIT} 字）：不调用任何工具，如实请主人照列表里的写法"
                    f"说短一点（**不许**替他截断）")
    raw = _write_arg(params.get("date"))
    if not raw:
        return [], ("dashboard_todo_reschedule 没给日期（date）：不调用任何工具，"
                    f"如实向主人问清要改到哪一天（他要是说不要排期了，date 就填"
                    f"「{_todo._TODO_CLEAR_WORD}」）")
    if raw in _todo._TODO_CLEAR_DATES:
        # 清空：传**契约词**（不是主人嘴里那个说法）——工具与卡面都只认这一个字面。
        _clear = json.dumps({"text": text, "date": _todo._TODO_CLEAR_WORD},
                            ensure_ascii=False)
        return ([f"reschedule_dashboard_todo({_clear})"],
                f"把他自己后台首页的待办里「{_tags.clip(text, 20)}」那一条的**排期清掉**"
                f"（只动排期，正文与完成标记都不动）")
    due = _todo.normalize_due_date(raw)
    if due is None:
        return [], (f"dashboard_todo_reschedule 的排期日「{raw}」认不出来"
                    f"（只认 {_todo.DUE_DATE_FORMS_CN}；清空排期填"
                    f"「{_todo._TODO_CLEAR_WORD}」）：不调用任何工具，"
                    "如实向主人问清是哪一天——**不许**自己挑一个日子顶上")
    return ([f"reschedule_dashboard_todo("
             f"{json.dumps({'text': text, 'date': due}, ensure_ascii=False)})"],
            (f"把他自己后台首页的待办里「{_tags.clip(text, 20)}」那一条的排期改成"
             f"{_todo.due_date_cn(due)}（只动排期，正文与完成标记都不动）"))


def _expand_article_status(params: dict,
                           aids: list[int]) -> tuple[list[str], str, str | None]:
    """文章类写技能的**状态**那一半：把点名的 N 篇改成点名的 status / is_top。

    （20261015 批 1 从 `_instantiate_plan` 抽出来：文章通道那两支合起来让那个函数顶到
    行数棘轮，而这一支自身就是一件完整的事——C=13 / 32 行，两项都在预算内。）
    ⚠️ 另一半 `article_tags` **没有**一起抽：它单独成函数是 93 行 / C=32，当场撞上本仓
    `C ≤ 30` 的预算（`tests/test_complexity_budget.py`）⇒ 要么再拆两半、要么别动；它与
    `graph._drop_satisfied_tag_creates` 的摘除判据紧耦合（见那段长注），拆它得把那一对
    同源关系一起重读，留到那一族下次改动时一起做。

    归一（status / is_top）在**这一层**做，归不出来 ⇒ 零工具 + 问清：写操作最不该做的事
    就是"参数看不懂时猜一个"（猜出来的那一版会当真写进去）。

    ⚠️⚠️ **第三个返回值是既有行为里的一处同名捕获，本批原样保留**：从前这一支住在一个
    也叫 `status` 的局部变量**同名**的 `_instantiate_plan` 里（那个 `status` 是导航族
    用来记 `nav_iot_off` / 最终写进计划 `status` 字段的那个字符串）⇒ 这一支的归一结果
    **会盖掉它**：`instantiate_plan("article_status", {"article_id": 46, "status":
    "private"})` 的计划 `status` 字段是 **`"private"`** 而不是 `"executed"`（两臂实测：
    611 格里恰好这 3 格不同）。它同时**关掉**了下面那句"零工具轮不许不成账"（那个判据
    读的也是 `status`）。**看起来像缺陷**（计划 status 字段的语义应该是执行状态，不是
    文章的目标状态），但改它是**行为变更**、要单独一趟验证 ⇒ 这里按原样把值回传出去，
    不在"零行为变更"这一批里顺手改掉；换名 = 那 3 格当场变成 `executed`，谁要动它请连
    这条注一起改并跑 golden。
    ⇒ 因此签名第三项**如实**写 `str | None`（旧代码里 `status` 被重新绑定成 `str | None`
    也是同一个 mypy 错误）；照原样回传的代价是**调用点那一句留住了一个既有的计数错误**
    （本仓没有一处 `# type: ignore`，220 个错误就是这么记着账的）——别拿 ignore 把它抹掉。
    """
    status = _tags.normalize_status(params.get("status"))
    top = _tags.normalize_top(params.get("is_top"))
    # ⚠️ 注记**先落到变量上再回传**，别直接写成 `return [], "中文…", status`：`eval/wordlist_gate.py`
    # 的第二层按 AST 数"住在集合字面量里的中文串"，元组的直接元素也算一处 ⇒ 同一个字面量
    # 换个位置就会被记成"新增了一张中文表"（实测：这一支原本就是这样，红了一次）。
    if params.get("status") not in (None, "") and status is None:
        note = (f"status「{params.get('status')}」认不出来（只支持 "
                "public/private/draft）：不调用任何工具，如实向主人问清")
        return [], note, status
    if params.get("is_top") not in (None, "") and top is None:
        note = (f"is_top「{params.get('is_top')}」认不出来（只支持 1/0）："
                "不调用任何工具，如实向主人问清")
        return [], note, status
    if status is None and top is None:
        note = ("article_status 没有指出要改什么（status / is_top）："
                "不调用任何工具，如实向主人问清要改成什么")
        return [], note, status
    # **一篇一条 spec**（20261009，与 `tag_create.titles` 同一条纪律）：N 篇 ⇒ N 条
    # `set_article_status`，多 spec 的现成通道全部自动接上（卡印成清单 + 「全部办」、
    # 逐条执行、逐条回执、逐条"已是目标值"摘除）。N=1 时逐字节等于旧行为（键序
    # article_id→status→is_top 不变）。
    # `dict[str, object]`：两个值的类型不同（归一后的 status 是 `str`、is_top 是 `int`），
    # 而它们要一起进 `json.dumps` ⇒ 按值域写 object（旧代码写 `fields = {}`，mypy 按
    # 第一笔赋值推成 `dict[str, str]`，于是 `fields["is_top"] = top` 一直是条计数错误）。
    fields: dict[str, object] = {}
    if status is not None:
        fields["status"] = status
    if top is not None:
        fields["is_top"] = top
    out = [f"set_article_status({json.dumps({'article_id': one, **fields}, ensure_ascii=False)})"
           for one in aids]
    note = (f"修改文章 {'、'.join(str(x) for x in aids)}："
            + "、".join(filter(None, [
                f"状态→{_tags.status_cn(status)}" if status is not None else "",
                f"置顶→{_tags.top_cn(top)}" if top is not None else ""]))
            + "（只改点名的字段）")
    return out, note, status


def _workflow_no_calls_suffix(workflow_name: str) -> str:
    """点名写在 `workflow_name` 里、而该技能不读调用清单。见 `instantiate_plan` 收尾处。"""
    return (f"{DROP_SUFFIX_WORKFLOW_NO_CALLS}{workflow_name}："
            "该技能的模板不执行 PARAMS.tools/PARAMS.calls）")
