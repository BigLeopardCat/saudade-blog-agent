"""上下文组装（20260912 从 graph.py 拆出）——纯函数、零 LLM、零 LangGraph 依赖。

内容 = 注入给 planner/model 的事实文本生成：消息文本提取（多模态兼容）、页面
上下文 page_ctx、页面操作指南（GUESTBOOK_GUIDE / SITE_GUIDE）、工具帧摘要
（_frame_texts）、checker 回执摘要（_receipts_text）。

拆出动机：graph.py 曾是 2000+ 行单体——图拓扑（节点/边/路由）与该层纯文本
组装混在一起，读一处要翻半屏无关代码。本层被 graph 的节点调用，自身不依赖
图（decisions.py 也用它），是天然的叶子层。
"""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# ---------------------------------------------------------------------------
# 消息/上下文工具
# ---------------------------------------------------------------------------

def _msg_text(m) -> str:
    """多模态 content 兼容：数组（image_url+text 块）只取 text 文本部分。

    分类/注入只消费文本——base64 dataURL 不进 prompt，否则 content[-500:]
    截到图片垃圾。
    """
    content = getattr(m, "content", "")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return content if isinstance(content, str) else str(content)


# ── 页面操作指南（20260905：留言引导幻觉修复的确定性知识源）──
# 背景：3660 事故（用户问"怎么留言"，narrator 编出"昵称/邮箱输入框+右上角登录"
# 全套幻觉流程）——RAG 语料只有文章，没有留言板操作说明；模型没有真凭实据时
# 用"一般论坛经验"脑补填坑，叙述纪律 2 只禁"站内内容编造"、没盖住操作流程类。
# 解法：current_url 命中留言板时把真实 UI 流程以页面事实注入 page_ctx（planner
# 与 model 均可见），模型只转述；任何页面都适用的第 10 条纪律禁止无据脑补 UI。
# 文案与 RiverBoard/index.tsx 实际 UI 对齐（输入框/留名/匿名/我的河灯页签）。
GUESTBOOK_GUIDE = (
    "【河灯集留言板操作指南】（系统注入的页面事实，教访客如何操作时以此为准）"
    "页面下方有留言输入框（提示语「此刻想说的话…」），在框里写好内容即可放灯；"
    "留名框在输入框旁，默认预填当前登录账号昵称，清空留名或点「匿名」则以无名/"
    "匿名身份放灯；不需要注册或邮箱，输入框一直可见。"
    "放灯后页面顶部「我的河灯」页签可查看自己放过的灯。"
    "注意：本页面没有「昵称+邮箱+提交」式表单，也不需要先登录才能留言。"
)

# 留言板路径（/guestbook 与旧隐藏地址 /he 同页）
_GUESTBOOK_URL_RE = re.compile(r"/(?:guestbook|he)\b")


# ── 站内板块与技能清单（20260905：介绍"博客有哪些板块/你能做什么"讲不全的
# 确定性知识源）──
# 实证（trace 20260905T190827/190857/190926/191007）：narrator 看不到板块全图与
# 技能清单——"小猫咪你都可以做什么呀"只列闲聊/找文章/跳转/特效，刚玩过的
# IoT 设备显示与河灯留言都漏；"只有这些吗"提醒后才补、仍漏 IoT；"博客都有
# 哪些功能"靠检索文章撞出 2 个板块（AI 助手/IoT 控制台），留言板/说说/归档等
# 未命中即缺失；近义重问还模板复读同文（489=489）。纪律 2 要求"页面存在"须
# 有据——板块事实不注入，narrator 只能靠记忆碎片或检索命中拼。解法：常驻注入
# 页面上下文（planner 与 model 均可见，模型只转述），与 GUESTBOOK_GUIDE 同构。
# 板块路径与 NAV_MAP（skills.py 单一事实来源）保持一致，新增板块须同步此处与
# test_skills 断言。
SITE_GUIDE = (
    "【站内板块与技能清单】（系统注入的事实——介绍「博客有哪些板块/功能」或"
    "「你能做什么」时以此为准完整转述）站内板块：首页；文章（/article/<id> 单篇）；"
    "留言板=「河灯集」（/guestbook）；说说（/talk）；归档/时间轴（/times）；"
    "关于我（/about）；物联网平台控制台（/device-console/）；登录（/login）与"
    "后台管理（/dashboard）仅博主使用。"
    "你能做的：陪聊与回答站内问题；搜索/找文章并给链接，讲解访客正在读的文章；"
    "查说说、河灯留言、公告；跳转到上述任意板块；开关特效（樱花/雨/雪等）与"
    "夜间模式；让接入的 ESP32 OLED 屏幕显示文字、查询设备在线状态；看访客发来"
    "的图片并描述内容/颜色。介绍能力时按此完整列出，不要遗漏。"
)


def _attach_page_guide(page_ctx: str) -> str:
    """页面上下文常驻附板块/技能清单；命中留言板时再附操作指南（URL 是系统
    上报事实，非模型推断；两份指南同为"只转述"系统数据）。"""
    try:
        out = (page_ctx or "") + "\n" + SITE_GUIDE
        if _GUESTBOOK_URL_RE.search(page_ctx or ""):
            out += "\n" + GUESTBOOK_GUIDE
        return out
    except Exception:
        return page_ctx or ""


def _page_ctx(messages: list) -> str:
    """提取前端实时上报的页面上下文（page/title/特效/夜间），注入 planner/model。

    前端每轮请求都携带真实 current_url（window.location.href），_build_messages
    写入首条 [System: ...] 消息。planner/model 若只凭对话推断访客位置会脱节：
    用户手动转跳后对话历史不体现页面变化（曾见用户说"已经离开物联网控制台了，
    在首页"，模型仍延续上一轮的设备显示动作）。此处显式提取注入 prompt——
    事实以系统上报为准，不依赖模型推断。
    """
    for m in messages:
        content = _msg_text(m) or ""
        found = re.search(r"\[System:\s*(.*?)\]", content, re.DOTALL)
        if found:
            return _attach_page_guide(found.group(1).strip())
    return "（无）"


def _last_user_msg(messages: list) -> str:
    """当前请求的用户消息 = 最近一条 HumanMessage 的文本。

    注意不能取 messages[-1]：planner ⇄ execute 多轮循环时最后一条是 ToolMessage
    （planner 每轮回来看工具返回），只有首轮的最后一条才是用户消息。
    """
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _msg_text(m)[-500:]  # 只看最近一段，防止超长输入稀释决策
    return ""


def _recent_tail(messages: list, max_turns: int = 4, per: int = 160) -> str:
    """最近几轮对话节选（planner 语境补丁，20260903 nav_param_anchor_about 事故）。

    planner 是单消息决策（history-blind），用户催促/质疑（"你不直接转跳过去？"）
    所指的目标只存在于更早轮次里——不给节选就无法还原该跳哪页。取状态消息里
    最近几轮人机对话行（跳过工具帧——结果有专门区块）。跳过两样：注入的页面
    上文（[System:…] 开头的人类消息，planner 已有 page_ctx）与当前这条用户消息
    （它是决策对象，不是上下文）。逐条截断防超长输入稀释决策。
    """
    out: list[str] = []
    seen_current = False
    for m in reversed(messages):
        if not isinstance(m, (HumanMessage, AIMessage)):
            continue
        text = (_msg_text(m) or "").strip()
        if not text:
            continue
        if isinstance(m, HumanMessage):
            if text.startswith("[System:"):
                continue
            if not seen_current:  # 最近的用户消息 = 当前请求，不算上下文
                seen_current = True
                continue
            speaker = "用户"
        else:
            speaker = "泠月"
        text = text.replace("\n", " ")[-per:]
        out.append(f"{speaker}：{text}")
        if len(out) >= max_turns:
            break
    if not out:
        return "最近对话节选：（无更早轮次）"
    return ("最近对话节选（判断'催促/质疑'所指——目标通常在这些轮次里）：\n"
            + "\n".join(reversed(out)))


def _has_frames(messages: list) -> bool:
    """当前请求是否有工具执行帧（ToolMessage）。"""
    return any(isinstance(m, ToolMessage) for m in messages)


# get_article_detail 全文帧的节选上限：该帧是 narrator 引用文章细节的唯一依据，
# 深文事实常在文末（20260903 golden 实证：架构文档 note 19 的
# STREAM_TOTAL_TIMEOUT 在全文 19260 字符处、固件参考 note 14 的 esp_https_ota
# 在 3754 处，per=300 的旧截断让整条 rag 深文族 FAIL）。20000 覆盖站内全部
# 文章正文长度，超出部分带"节选"标注——narrator 不会把截断当全文。
_DETAIL_FRAME_PER = 20000


def _frame_texts(messages: list, limit: int = 5, per: int = 300) -> str:
    """最近的工具返回摘要（planner 下一轮决策依据 / narrator 叙述依据）。

    只取最近 limit 条。截断策略按帧型：普通帧（检索候选/列表）截 per 字符
    （行式精简，够看）；get_article_detail 是全文读取帧，按 _DETAIL_FRAME_PER
    大幅放宽并标注"节选"；__ERROR__ 信息完整保留（planner 需要据错误修正参数
    重试）。
    """
    frames = [m for m in messages if isinstance(m, ToolMessage)]
    if not frames:
        return "（本轮尚无工具执行）"
    parts = []
    for m in frames[-limit:]:
        name = getattr(m, "name", "") or ""
        text = _msg_text(m)
        if text.startswith("__ERROR__"):
            parts.append(f"工具 {name} 返回错误: {text}")
        elif name == "get_article_detail" and len(text) > _DETAIL_FRAME_PER:
            parts.append(
                f"工具 {name} 返回（节选，原文过长仅示前 {_DETAIL_FRAME_PER} 字）: "
                f"{text[:_DETAIL_FRAME_PER]}")
        elif name == "get_article_detail":
            parts.append(f"工具 {name} 返回: {text}")
        else:
            parts.append(f"工具 {name} 返回: {text[:per]}")
    return "\n".join(parts)


def _receipts_text(receipts: list) -> str:
    """checker 验收回执摘要（narrator 同轮如实转述依据，20260904）。

    工具帧是"执行了什么"的原始返回，回执是"系统验收确认执行成功"的收据——
    narrator 描述"实际显示了什么/跳转到哪"以回执为准（帧可能只含 ack 不含
    参数，回执的 args 是文案注入后值，含实际屏文）。空 → 本轮无已验收执行。
    """
    if not receipts:
        return "（本轮没有已验收的执行）"
    lines = []
    for r in receipts[-5:]:  # 同轮多 spec 时只取最近 5 条，防稀释
        args_txt = json.dumps(r.get("args") or {}, ensure_ascii=False)[:160]
        lines.append(f"- {r['tool']} args={args_txt} → {str(r.get('result', ''))[:160]}")
    return "\n".join(lines)
