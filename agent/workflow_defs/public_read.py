# -*- coding: utf-8 -*-
"""技能注册表·公开读两条技能（原序列 6–7，**顺序是契约**）。

`content_query` 是"查站内内容"的技能面；`read_article` 是**系统专用**的当前文章快道
——它的 `article_id` 由 `current_url` 解析、planner 无参可填，因此不进 planner 菜单
（见 `__init__.py` 的 `_SYSTEM_ONLY_WORKFLOWS`），但字面量仍住在这里：它是同一族的
读技能，拆到别处只会多一处"谁看得见它"的判据。
"""

from __future__ import annotations

from agent.workflow_model import (
    _EXPLICIT_TOOLS_MARK,
    _PARAM_TOOLS_MARK,
    Workflow,
)

__all__ = ["WORKFLOWS"]


WORKFLOWS: list[Workflow] = [
    Workflow(
        name="content_query",
        capability="查站内内容并给真实来源：文章/说说/河灯留言/公告/站点信息",
        # 20260903 架构裁决（planner 全权，自由 ReAct 废除）：内容查询不再有
        # "执行层自由选择"——planner 每轮直接产出调用清单（PARAMS.calls，带参
        # 白名单校验），execute 节点确定性执行，多轮规划由 planner 驱动：
        # 先检索定位 → 看工具帧 → 决定 get_article_detail 读哪篇 / 换词再搜 /
        # 收尾如实答复。检索器选型（search_notes 关键词 vs rag_search 相关度）
        # 与关键词抽取都是 planner 决策，模型/执行层零自由——"跳过检索直接答"
        # 在结构上不可能（调用清单是计划的组成部分）。
        description=(
            "用户询问博客内容时使用（文章/说说/留言/公告/站点信息里的内容）——包括："
            "知识型问题（文章里写了什么、怎么做、是什么，"
            "如\"Git 和 SVN 有什么区别\"\"ESP32 的 OTA 怎么配置\"）；"
            "数据/列表型查询（最新留言/说说/公告、文章列表、封面图片、分类/标签/天气/时间/"
            "站点信息/社交链接/置顶文章）；"
            "页面/内容存在性质疑（如\"真有这个页面？确定有这篇？\"——查证页面或"
            "内容是否存在；"
            "执行是否属实的问题归跨轮执行记忆（页面上下文『确认与执行事实』块里『已执行』那半），"
            "见规划规则 6，不在本技能范围）。"
            # 「必须怎么做」那两条（20261017 并回本字段，见 Workflow 里那段注释）：
            # ① 必须给调用清单——它原住在这里，随 slim 交给 schema 时实测路由回归
            #    （`rag_git_svn` slim 5/9 vs 完整 9/9：模型改用通用知识直接作答、一个
            #    检索工具都没调）。规划规则 3 说的是同一件事，但那是七条规则里的一段，
            #    **不等于**技能旁边那句——与成对点名同一个洞。
            # ② 成对点名——原写在 description 与 inputs.tools 两处（同一句话两份），
            #    20260927 收成一处。
            "\n涉站内容**必须先取回真实数据再作答**：知识型/验证型给 calls（search_notes/"
            "rag_search 定位、get_article_detail 读全文），数据/列表型点名 tools；"
            "除收尾轮（已有工具返回且信息足够 / 明确查无结果）外不许空着手回答。"
            "问「留言板/说说里有没有人聊过、写过 X」时必须**成对**点名两个数据源："
            "list_guestbook 与 list_talks（只点一个 = 少查一半，等于没查）"
        ),
        inputs={
            "tools": (
                f"（可选）无参只读数据工具点名列表，仅限 {_EXPLICIT_TOOLS_MARK}"
            ),
            "calls": (
                "（可选）带参调用清单：[{\"tool\": \"search_notes\", \"args\": {\"keyword\": "
                f"\"用户原词\"}}]；工具仅限 {_PARAM_TOOLS_MARK}；"
                "get_article_detail 的 id 只能取自上一轮工具返回"
            ),
        },
        plan=[],  # 调用清单由 planner 经 PARAMS.tools/calls 注入（本技能实例化白名单校验展开）
        complete_when="回答基于工具返回的数据",
        reply_contract=(
            "回答基于工具返回的数据，不得编造；检索无结果或无关时如实告知"
            "（'站内没有找到相关资料'是正当结论，不得改用模型记忆硬答）。"
            # 20261007 补（行为红 `rag_project_files`：清洗后窗口 10/62 轮、本机基线 2/4）：
            # 命中了几篇、但那几篇
            # **都没写到被问的那一项**时，模型把"常见工程惯例"补成了《架构文档》里的
            # 目录结构（答复形如"根据架构文档…配置文件主要有这些"）——命中文章成了
            # 挡箭牌，常识换了个出处照样当站内事实。两条：常识与站内**分开落款**、
            # 命中文章讲的是别的对象时不许顶替。
            "命中了几篇但都**没写到**被问的那一项时，如实说那一项站内没有写到，"
            "**不许**用通用常识/行业惯例把它补成一份看着像站内给出的清单；"
            "要补充常识就明说那是常识、不是站内文章里写的；"
            "命中的文章讲的是**别的对象**（别的仓库、别的子系统）时，"
            "不许把它说成回答了所问的那一项。"
            "查询'博客/留言板/说说里有没有人聊过/写过 X'这类问题时，"
            "以工具返回为准如实告知两个数据源都查过了什么；"
            "问题针对用户当前正在阅读的文章（页面上下文 current_url 为 /article/:id）时，"
            "必须先经 get_article_detail 读取该文章，基于真实全文回答"
        ),
    ),
    Workflow(
        name="read_article",
        # 20260901 系统性修复（用户评审定性："读当前文章"是固定流程任务）：
        # 模型对"用户当前在读的文章"只有 page_ctx 文本提示（current_url=/article/21），
        # 无结构化事实、无强制读取——于是零工具声称"这篇我读完了"编造全文
        # （232107：600 字细节全部虚构；232302：模型自己承认没读过、但系统
        # 没有机制强制去读）。本技能 = 固定流程：planner_node 的确定性快道
        # _article_fast_path 从 current_url 解析文章 ID 后注入本技能实例化，
        # TOOLS 行强制 get_article_detail → execute 必执行（有执行必有帧）。
        # 文章 ID 是系统数据，不经 planner 决策。
        description=(
            "（系统确定性快道专用，planner 不得选择——由 planner_node 在用户当前页面为"
            "文章详情页且消息引用当前文章时注入）读取用户当前正在阅读的文章全文后回答"
        ),
        inputs={"article_id": "当前文章 ID（系统从页面上下文 current_url 解析，planner 不决策）"},
        plan=[("get_article_detail", {"article_id": "$article_id"})],
        complete_when="get_article_detail 返回文章全文",
        reply_contract=(
            "必须调用 get_article_detail 读取用户当前阅读的文章全文后再回答；"
            "对文章内容的所有引用（标题/观点/细节/写法评价）必须来自工具返回，不得编造；"
            "工具返回读取失败（文章不存在）时如实告知"
        ),
    ),
]
