"""确定性决策层（20260912 从 graph.py 拆出）——零 LLM，消息/执行事实 → 计划。

四组：
  1. 快道（fast path）：当前文章读取 / 特效切换 / 导航 / 屏幕显示——强模式命中
     即实例化计划，不请 planner LLM（映射表白名单校验已在 skills.instantiate_plan
     兜底，命中即确定性识别，无"模型猜错"通道）；不命中一律 None 落回 planner。
  2. 动作意图扫描：_scan_action_intents 确定性扫出消息里的动作指令，_intent_hints
     标注完成状态后注入 planner 提示词——planner 仍是唯一决策者，它只是不再
     "看不见"一句话里的第二个动作（多意图丢失修复，20260912）。
  3. 检索候选裁决：_candidate_detail_plan 从检索帧候选里挑"标题对得上检索实词"
     的未读文档读全文（位置规则加固，20260912）——读错一篇的代价是整轮跑题。
  4. 终局计划：_terminal_plan / _wrap_up_plan 确定性收尾（轮次上限 / 复盘终局 /
     检索重复拦截共用），不经 LLM、不静默 accept。

与 graph.py 的关系：graph.py 定义图拓扑与节点（planner/execute/model/gate），
节点调用本层；本层只依赖 skills / rag.search / context（无反向依赖，避免循环导入）。
命名沿用原下划线前缀（test_skills.py 与 graph.py 按原名引用，重构不改契约）。

依赖：skills（技能表/实例化）、rag.search（分词）、context（消息文本）。
"""

import ast
import json
import logging
import re

from langchain_core.messages import ToolMessage

from agent.context import _msg_text
from agent.skills import FUZZY_NAV_RULES, NAV_MAP, SKILL_MAP, instantiate_plan
# 与检索侧同一分词（2/3-gram）——候选标题相关性判定复用，避免两套词法
from rag.search import tokenize as _rag_tokenize

logger = logging.getLogger(__name__)

# 规划轮次上限：planner ⇄ execute 循环最多决策 MAX_PLAN_ROUNDS 轮，之后强制
# 收尾（基于已有工具返回如实作答）。防止 planner LLM 无限追问/重复调用烧钱。
# 每轮 = planner 一次决策；收尾轮（计划 TOOLS 为空）直接走 model，不占用。
MAX_PLAN_ROUNDS = 4


# 导航确定性快道（零 LLM）：动词 + 页面别名强模式 → 直接实例化 navigate 计划。
# 用户实测"规划要7秒/10几秒"——planner LLM 对导航这类最常见的固定流程任务
# 没必要调用（映射表白名单校验已确定性兜底，触发词也足够窄）。命中 → 秒级出
# 计划；不命中任何映射 → None → 落回 planner LLM（模糊表达/未知页面交给模型）。
# 20260828 事故加固（"你读到留言为什么没有按留言执行任务"被误判成导航请求）：
#  1. 疑问/质疑句式整体排除（_QUESTION_RE）——质疑不是导航请求；问路类
#     （"怎么去留言板"）排除后由 planner LLM 识别为导航意图，功能不丢只多一次调用；
#  2. 动词改为 match（必须句首，允许剥离称呼前缀）而非 search——"读到"里的"到"
#     曾命中句中任意位置的正则；
#  3. 目标串收紧到 8 字——16 字会整段捕获噪声目标（曾捕获"留言为什么没有按留言执行任务"）。
_QUESTION_RE = re.compile(r"为什么|怎么|如何|啥|什么|为何|哪儿|哪|吗$|么$|[？?]")

_NAV_VERB_RE = re.compile(
    r"^(?:小猫咪|喵喵|主人|猫猫|喵)?[,，、\s]*"
    r"(?:去一下|回到|返回|跳转到|前往|转到|转跳|打开|进入|带我(?:去|到)|去|进|回|到|访问)"
    r"\s*([^\s，。！？!?～~、；;：:]{1,8})$"
)


# 当前文章读取确定性快道（20260901 系统性修复，零 LLM）。
# 根因（用户评审定性，声称闸补丁被拒）：模型对"用户当前在读的文章"只有
# page_ctx 文本提示（current_url=/article/21），无结构化事实、无强制读取——
# 于是模型凭 URL 文本知道在读哪篇、却永远不真的读，回答全靠想象。事故实证：
# 232107「这篇文章你怎么看」→ 模型声称"这篇我读完了"编造 600 字全文细节。
# 20260903 架构后依然保留：这是固定流程任务——current_url 解析出文章 ID 是
# 系统数据（非模型推断），计划 TOOLS 行强制 get_article_detail → execute 必须
# 调用（execute 无自由意志，比旧 reflector 兜底更硬）。
# 触发语域限"这篇/正在读/读到这"等强指称，宁多勿漏（文章页上误触发成本 =
# 一次毫秒级读取，漏触发 = 幻觉重演）；不命中 → 落回 planner LLM。
_ARTICLE_URL_RE = re.compile(r"/article/(\d+)")
_ARTICLE_REF_RE = re.compile(
    r"这篇|这篇文章|这篇文"
    r"|我(?:现在|正在|当前)?(?:在读|读的)|我现在读|正在读|现在在读|正在看|现在看"
    r"|(?:读|看)到(?:这里|这篇)|看完这篇|读完了这篇"
    r"|这篇文章(?:讲|写|说|聊|介绍|什么|怎么|如何|怎样|你)"
)


def _article_fast_path(user_msg: str, page_ctx: str) -> dict | None:
    """当前文章读取快道：current_url 匹配 /article/<id> 且消息引用当前文章
    （"这篇/我正在读/读到这"…）→ read_article 计划（TOOLS 强制 get_article_detail）。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None 落回 planner LLM。
    文章 ID 从 page_ctx 的 current_url 正则解析——系统数据，不存在模型猜错通道；
    read_article 技能对 planner LLM 不可见（build_planner_context 过滤），仅本
    快道注入（instantiate_plan 缺 article_id 时按 chat 兜底，防误用）。
    """
    m = _ARTICLE_URL_RE.search(page_ctx)
    if not m:
        return None
    if not _ARTICLE_REF_RE.search(user_msg):
        return None
    article_id = m.group(1)
    plan_obj = instantiate_plan("read_article", {"article_id": article_id})
    plan_obj["params"] = {"article_id": article_id}
    logger.info("[planner] 当前文章读取快道命中（零 LLM）: %s", plan_obj["tools"])
    return plan_obj


# 特效切换快道（20260904）：把 X 换成/改成 Y → 关当前效果 + 开目标效果双 spec。
# 事故实证：planner LLM 对"不要樱花了，改成下雨吧"反复只解出"关樱花"半边——
# 10 轮采样 8 轮丢 rain:on（4 轮收尾"只关了樱花"、4 轮明言"雨没法帮你切换"），
# 目标效果半边的规划在模型侧不稳定。切换是固定流程任务：旧效果来自 current_effects
# 系统状态、目标效果是消息动词后的字面量，无模型推断空间 → 与 read_article 快道
# 同理（宁多勿漏：误触发成本 = 一次幂等检查，漏触发 = 用户要求只做一半）。
_EFFECT_ALIASES = {  # 别名 → 规范化 effect id（匹配按别名长度降序，长名优先）
    "樱花": "sakura", "sakura": "sakura",
    "下雨": "rain", "大雨": "rain", "rain": "rain", "雨": "rain",
    "雪花": "snow", "下雪": "snow", "snow": "snow", "雪": "snow",
}
_SWITCH_VERB_RE = re.compile(r"换成|改成|改为|切换|调成|变为|换")
# 内容改写语境排除（"把文章里的雨字改成雪字"不是特效请求）——不用"文章/留言"
# 这类词（会误杀"换特效顺便查文章"的混合意图），只排改写对象的强标记词
_EFFECT_TALK_GUARD = re.compile(r"内容|文字|标题|代码|字|词|称呼|名字")


def _effect_switch_fast_path(user_msg: str, current_effects: str) -> dict | None:
    """特效切换快道：切换动词 + 目标效果名（消息动词后）→ effect 双 spec 计划。

    返回 plan dict（与 instantiate_plan 产物同构，tools 可含两条 toggle_effect），
    或 None 落回 planner LLM。旧效果取值顺序：消息点名（动词前）→ 当前开着且
    非目标的其它效果（"改成下雨"不点名时以 current_effects 实况补旧）；
    目标已开着时只关旧（幂等，不重复开）。
    """
    if _EFFECT_TALK_GUARD.search(user_msg):
        return None
    m = _SWITCH_VERB_RE.search(user_msg)
    if not m:
        return None
    after = user_msg[m.end():]
    before = user_msg[:m.start()]
    # 目标效果 = 动词后第一个别名命中（长名优先：先试"下雨"再试"雨"）
    target = None
    for alias in sorted(_EFFECT_ALIASES, key=len, reverse=True):
        if alias in after:
            target = _EFFECT_ALIASES[alias]
            break
    if target is None:
        return None
    old = None
    for alias in sorted(_EFFECT_ALIASES, key=len, reverse=True):
        if alias in before:
            old = _EFFECT_ALIASES[alias]
            break
    cur = {e for e in (current_effects or "").split(",") if e and e != "none"}
    if old is None:
        on_others = [e for e in cur if e != target]
        old = on_others[0] if on_others else None
    if old == target:
        return None  # 换到当前已开效果 = 幂等，落回 planner 叙述
    tools = []
    if old is not None and old in cur:
        tools.append(f"toggle_effect({json.dumps({'effect': old, 'action': 'off'}, ensure_ascii=False)})")
    if target not in cur:
        tools.append(f"toggle_effect({json.dumps({'effect': target, 'action': 'on'}, ensure_ascii=False)})")
    if not tools:
        return None
    note = f"特效切换快道（确定性，非模型决策）：{'、'.join(tools)}"
    return {
        "skill": "effect",
        "tools": tools,
        "note": note,
        "reply": SKILL_MAP["effect"].reply_contract,
        "chat": False,
        "params": {"effect_switch": f"{old or '（无）'}→{target}"},
    }


def _nav_fast_path(user_msg: str) -> dict | None:
    """导航快道：整句映射命中，或动词+目标强模式 + 映射/模糊归一命中 → navigate 计划。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None。
    目标校验走与 instantiate_plan 完全相同的 NAV_MAP/FUZZY_NAV_RULES 白名单——
    快道命中 = 确定性识别，不存在"模型猜错"通道；未知页面（"去火星基地"）不命中
    映射 → None → planner LLM 按"如实告知没有该页面"处理。已下线页面（友链）同样
    命中（NAV_MAP 值 None），实例化后 note 会要求如实告知、零工具。
    """
    msg = user_msg.strip().strip("，。！？!?～~、")
    # 疑问/质疑句式（"为什么""？"等）不是导航请求，直接排除（20260828 事故加固，
    # 见 _NAV_VERB_RE 上方注释）；问路类由 planner LLM 兜底识别为导航意图
    if _QUESTION_RE.search(msg):
        return None
    # 否定式排除（20260920b）：与显示快道对齐（_NEGATION_RE，见其上方注释）。动词**之后**
    # 的否定词原先穿不过去——"带我去留言板不用了"被 `_NAV_VERB_RE` 的 $ 锚捕获成目标
    # "留言板不用了"，再被模糊归一规则命中（"留言板" ∈ t）⇒ 拿一句否定语当导航目标。
    # 命中即回落 planner LLM（多一次调用，行为正确）——快道只是提速，误判才是真代价。
    if _NEGATION_RE.search(msg):
        return None
    target = msg if msg in NAV_MAP else None
    if target is None:
        m = _NAV_VERB_RE.match(msg)  # match 而非 search：动词必须句首，避免句中误匹配
        if m:
            t = m.group(1)
            if t in NAV_MAP or t.startswith("/"):
                target = t
            else:
                fuzzy = next(
                    (p for kws, p in FUZZY_NAV_RULES if any(kw in t for kw in kws)), None
                )
                if fuzzy:
                    target = t
    if target is None:
        return None
    plan_obj = instantiate_plan("navigate", {"target": target, "mode": "direct"})
    plan_obj["params"] = {"target": target, "mode": "direct"}
    return plan_obj


# 显示意图确定性快道（零 LLM，20260828 影子系统重构）：屏幕类名词 + 写/显示类动词
# 强模式 → 直接实例化 device_display 计划。不经过 planner LLM、更不经过提取器——
# "显示内容由 execute 在工具调用时创作"（execute 内 _create_display_text）。
# 与导航快道同构：命中 = 确定性识别（无模型猜测通道）；不命中 → 落回 planner LLM。
# 排除项（防误伤）：
#  - 疑问句式（为什么/怎么/吗/？）——问路不是命令；
#  - 否定式（不用/不要/别）——"不用在屏幕上显示"不是显示命令；
#  - 仅"设备"名词不触发（与 device_query 冲突："设备显示什么"是查询）。
_NEGATION_RE = re.compile(r"不(用|要|想|必|需要)|别|不要")
_DISPLAY_FAST_RE = re.compile(
    r"(屏幕|OLED|显示屏|显示器|大屏)[^\n。！？!?]{0,12}(写|显示|展示|换上|换成|改成|放|打上)"
    r"|(写|显示|展示|换上|换成|改成)[^\n。！？!?]{0,12}(屏幕|OLED|显示屏|显示器|大屏)"
)


def _display_fast_path(user_msg: str) -> dict | None:
    """显示意图快道：屏幕类名词+写/显示动词强模式 → device_display 计划（零 LLM）。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None 落回 planner LLM。
    内容由 execute 节点创作（PARAMS 不填 text，见 _create_display_text）——屏幕
    文案不进 planner 文本通道，杜绝"指令原文残缺片段上屏"。
    """
    if _QUESTION_RE.search(user_msg) or _NEGATION_RE.search(user_msg):
        return None
    if not _DISPLAY_FAST_RE.search(user_msg):
        return None
    plan_obj = instantiate_plan("device_display", {})
    plan_obj["params"] = {}
    logger.info("[planner] 显示意图快道命中（零 LLM，内容由 execute 创作）")
    return plan_obj


# 动作意图清单（20260912，多意图丢失修复）
# 事故实证：golden multi_intent_two_effects 21 次留档 3 次 FAIL（≈14%），失败回执
# 恒为 exec：['toggle_effect']——"帮我把樱花特效打开，顺便切一下夜间模式"这类**跨
# 技能并列意图**：一轮只能选一个技能（SKILL= 单值），必须靠 planner⇄execute 多轮
# 完成；但 planner 第 2 轮按 rule5"动作已执行 → 本轮收尾"直接收尾，第二个动作永久
# 丢失（narrator 只好如实承认"没切换成功"）。与 effect 切换快道同源的历史证据：
# "不要樱花了，改成下雨吧"10 轮采样 8 轮丢 rain:on——**"一句话多个动作"是本模型已
# 知的稳定弱点**，靠提示词相信它"记得住"是打地鼠。
# 系统侧补齐事实（不夺决策权）：确定性扫描消息里的动作意图 + 用已执行 spec 标注
# 完成状态，作为 intent_hints 每轮注入——planner 仍是唯一决策者，它不再"看不见"
# 第二个意图；是否执行、怎么执行仍由它决定（误扫命中由它否掉即可）。
_ACTION_VERB_ON = r"打开|开启|开一下|开|切到|切成|切换|切|换成|改成|调成|启动|来一个|下起来"
_ACTION_VERB_OFF = r"关掉|关闭|关一下|关|停掉|停|去掉|撤掉|取消"
_ACTION_VERB_RE = re.compile(f"(?:{_ACTION_VERB_ON}|{_ACTION_VERB_OFF})")
_ACTION_VERB_OFF_RE = re.compile(f"(?:{_ACTION_VERB_OFF})")
_DARKMODE_ALIASES = ("夜间模式", "夜晚模式", "暗色模式", "深色模式", "夜间", "暗色", "深色")


def _scan_action_intents(user_msg: str) -> list[dict]:
    """确定性扫描用户消息里的动作意图 → [{"key","family","label","tool","args"}]。

    只收"明确下指令"的形态（动作动词 + 宾语，同一窗口内）；两种情况整体不收
    （判错方向的代价大于收益——多报会让 planner 白跑一轮，报错方向会多执行动作）：
      * 疑问句（"怎么开夜间模式？"）——是问法不是命令；
      * 否定式（"别开樱花"）——不做极性推理，直接跳过该意图。
    结果只作提示注入（intent_hints），最终决策仍在 planner。
    """
    if _QUESTION_RE.search(user_msg):
        return []
    intents: list[dict] = []
    spans: list[tuple[int, int]] = []

    def _hit_span(i: int, n: int) -> bool:
        return any(s <= i < e for s, e in spans)

    def _verb_action(i: int, n: int) -> str | None:
        """别名邻域（前 8 字 / 后 8 字）里的动作动词 → "on"/"off"/None。

        切换句式（"把樱花换成下雨"）里，切换动词**之前**的别名是被换掉的旧效果
        （→ off），之后的才是目标（→ on）——与特效切换快道同语义。
        """
        lo = max(0, i - 8)
        win = user_msg[lo: i + n + 8]
        sw = _SWITCH_VERB_RE.search(win)
        if sw and (lo + sw.start()) > i:
            return "off"
        m = _ACTION_VERB_RE.search(win)
        if not m:
            return None
        return "off" if _ACTION_VERB_OFF_RE.fullmatch(m.group(0)) else "on"

    # 特效（复用快道别名表；长名优先，命中即占位防"下雨"里的"雨"重复计）
    for alias in sorted(_EFFECT_ALIASES, key=len, reverse=True):
        i = user_msg.find(alias)
        while i >= 0:
            j = i + len(alias)
            if not _hit_span(i, len(alias)):
                spans.append((i, j))
                act = (None if _NEGATION_RE.search(user_msg[max(0, i - 4):i])
                       else _verb_action(i, len(alias)))
                if act:  # 只有动作动词在场才算指令（"樱花真好看"不是请求）
                    eff = _EFFECT_ALIASES[alias]
                    intents.append({
                        "key": f"effect:{eff}={act}", "family": "effect",
                        "label": f"{alias}特效{'关' if act == 'off' else '开'}",
                        "tool": "toggle_effect", "args": {"effect": eff, "action": act}})
            i = user_msg.find(alias, j)
    # 夜间模式
    for alias in _DARKMODE_ALIASES:
        i = user_msg.find(alias)
        while i >= 0:
            j = i + len(alias)
            if not _hit_span(i, len(alias)):
                spans.append((i, j))
                act = (None if _NEGATION_RE.search(user_msg[max(0, i - 4):i])
                       else _verb_action(i, len(alias)))
                if act:
                    intents.append({
                        "key": f"darkmode={act}", "family": "darkmode",
                        "label": f"夜间模式{'关' if act == 'off' else '开'}",
                        "tool": "toggle_dark_mode", "args": {"mode": act}})
            i = user_msg.find(alias, j)
    # 屏幕显示
    if _DISPLAY_FAST_RE.search(user_msg) and not _NEGATION_RE.search(user_msg):
        intents.append({"key": "display", "family": "device_display",
                        "label": "屏幕显示文字", "tool": "device_oled_display", "args": {}})
    # 导航（动词必须在句首，与导航快道同判据）
    if _NAV_VERB_RE.match(user_msg.strip().strip("，。！？!?～~、")):
        intents.append({"key": "navigate", "family": "navigate",
                        "label": "页面跳转", "tool": "navigate_to", "args": {}})
    return intents


def _intent_done(intent: dict, executed: list) -> bool:
    """该意图是否已有执行事实（executed spec 的工具名 + 参数片段齐备）。"""
    for s in executed:
        if _tool_name(s) != intent["tool"]:
            continue
        if all(f'"{k}"' in s and f'"{v}"' in s for k, v in intent["args"].items()):
            return True
    return False


def _intent_hints(executed: list, user_msg: str) -> str:
    """planner 提示词的动作意图区块（每轮重算，完成状态随执行事实变化）。"""
    intents = _scan_action_intents(user_msg)
    if not intents:
        return "（系统未扫描到明确的动作指令——按常规规则决策）"
    out = []
    for it in intents:
        done = _intent_done(it, executed)
        out.append(f"- {it['label']}（{it['key']}）："
                   + ("已执行" if done else "**未完成**"))
    return "\n".join(out)


# 检索候选行解析（确定性拦截用，见 planner_node"检索重复清单拦截"）
# 经验记录类标题：机制型问题的答案在「参考/指南」类文档，这类标题延后读。
_EXPERIENCE_TITLE_RE = re.compile(
    r"问题与解决记录|问题记录|踩坑|复盘|FAQ|排错|故障|心得|备忘")
# rag_search 行式候选（例：`1. type=note id=19 score=5.82 title=… 命中节=…`）
# score 要捕获（20260920 候选改读闸用，见 _candidate_detail_plan）。
_RAG_ROW_RE = re.compile(
    r"^\s*\d+\.\s*type=(\w+)\s+id=(\d+)\s+score=([\d.]+)\s+title=(.*)$", re.M)
_DETAIL_SPEC_RE = re.compile(r'article_id["\']?\s*[:=]\s*(\d+)')

# 候选相关性判定（20260912，检索重复拦截的位置规则加固）
# 关键词检索（search_notes）的候选行序：**20260912 起 = 后端相关度降序**（Rust
# notes.rs 补了 search_score：标题 +100 / 标签 +30 / 正文次数 ≤10），在此之前
# 是主键序（当时该 LIKE 查询无 ORDER BY，返回顺序纯属存储顺序、无相关度含义）。
# 无论哪种序，位置规则都不能用：排序只保证"更好的在前"，不保证第一条就对得上号。
# 9/8 事故链条：用户要"讲你架构的技术文档文章" → planner 误规划了 search_notes("架构")
# → 候选[0] 是《Git从入门到入土》（正文表格里出现过"架构"一词、noteKey 更小）→
# 拦截器按位置规则把候选[0] 当目标读全文 → 整轮跑题、连错三轮。位置规则必须换成
# "标题与检索实词对得上"才读。
_CANDIDATE_STOPWORDS = {
    "文章", "文档", "内容", "东西", "一篇", "这篇", "那篇", "哪些", "什么", "怎么",
    "如何", "站内", "博客", "相关", "有没有", "关于", "一个", "这个", "那个", "一下",
}


def _spec_arg(spec: str, key: str) -> str:
    """取 TOOLS 行 spec 的字符串参数（'search_notes({"keyword": "架构"})' → 架构）。"""
    m = re.search(key + r'["\']?\s*[:=]\s*["\']([^"\']+)["\']', spec)
    return m.group(1) if m else ""


def _search_terms(plan_obj: dict, executed: list, user_msg: str) -> set[str]:
    """本轮检索的实词集合：优先取检索 spec 里的关键词原文（planner 抽的词），
    没有可用 spec 时退回用户消息。用于判定候选标题是否"对得上"检索意图。

    取 spec 而非用户整句，是因为判断对象是"这次检索查的是什么"——planner 抽的
    关键词才是候选集的成因；用户整句里还混着称呼/语气词，会稀释判断。
    """
    terms: list[str] = []
    specs = [s for s in executed if _tool_name(s) in ("search_notes", "rag_search")]
    specs += [s for s in (plan_obj.get("tools") or [])
              if _tool_name(s) in ("search_notes", "rag_search")]
    for s in specs:
        for key in ("keyword", "query"):
            v = _spec_arg(s, key)
            if v:
                terms.append(v)
    text = " ".join(terms) if terms else user_msg
    return {t for t in _rag_tokenize(text)
            if len(t) >= 2 and t not in _CANDIDATE_STOPWORDS}


def _title_relevant(title: str, terms: set[str]) -> bool:
    """标题与检索实词有词元重叠（同一 2/3-gram 分词）→ 该候选"对得上号"。"""
    return bool(terms & set(_rag_tokenize(title)))


def _candidate_detail_plan(messages: list, executed: list, terms: set[str]) -> dict | None:
    """重复拦截的确定性出路：从最近检索帧候选行里挑"能对上号"的未读文档读全文。

    20260912 位置规则加固——候选行按来源分流处置（两者顺序语义完全不同）：
      * search_notes（关键词命中）：行序 = **相关度降序**（20260912 起后端按
        标题+100/标签+30/正文次数打分排序，`notes.rs::search_score`；此前确为主键序、
        无相关度含义，本节旧注释与 9/8 事故同源），但仍必须标题与检索实词有词元
        重叠才可自动读——排序只保证"更好的在前"，不保证第一条就对得上号；
      * rag_search（本地 BM25）：行序 = 相关度序 → 关键词候选全不匹配时兜底
        （语义检索的价值正是"标题不含查询词也能命中"，不该被标题过滤否定）。
    经验记录类标题在存在机制文档时延后（20260903 rag_ota_http 实证：只命中
    《问题与解决记录》踩坑史）。候选全已读 / 无一能对上号 → None：调用方如实
    收尾并列出候选——**诚实优于硬读**（读错一篇的代价是整轮跑题 + 跨轮锚点污染）。

    20260920 加**相关度闸**（"只允许越读越高分"）：rag 候选按分数取最高分去重、
    池内按分降序，且分数不高于"已读候选最高分"的一律不读——读全文的收益只来自
    最好的那份证据，低分候选是小语料 top_k 的填充物（见函数内注释的实证数据）。
    """
    done_ids = {m.group(1) for s in executed for m in [_DETAIL_SPEC_RE.search(s)]
                if m is not None}
    frames = [m for m in messages if isinstance(m, ToolMessage)]
    # (id, doc_type, title, src, score)——score 只有 rag 行有（BM25 分），kw 行 None
    rows: list[tuple[str, str, str, str, float | None]] = []
    for m in reversed(frames):
        name = getattr(m, "name", "") or ""
        text = _msg_text(m)
        try:
            if name == "search_notes":
                obj = ast.literal_eval(text)
                if isinstance(obj, list):
                    for r in obj:
                        if isinstance(r, dict) and r.get("noteKey") is not None:
                            rows.append((str(r["noteKey"]), "note",
                                         str(r.get("noteTitle") or ""), "kw", None))
            elif name == "rag_search":
                for typ, rid, score, title in _RAG_ROW_RE.findall(text):
                    dt = ("talk" if typ == "talk" else "board" if typ == "board"
                          else "note")
                    rows.append((rid, dt, title.split(" 命中节=")[0], "rag",
                                 float(score)))
        except Exception:
            continue
    # 同一篇在多轮检索里出现 → 取**最高分**（相关度 = 它拿到过的最好成绩）；行序按首次
    # 出现（_EXPERIENCE_TITLE_RE 的经验记录延后仍按原始序生效）。
    best: dict[str, tuple] = {}
    order: list[str] = []
    for r in rows:
        if r[0] not in best:
            best[r[0]] = r
            order.append(r[0])
        elif r[4] is not None and (best[r[0]][4] is None or r[4] > best[r[0]][4]):
            best[r[0]] = r
    ordered = [best[i] for i in order]
    unread = [r for r in ordered if r[0] not in done_ids]
    if not unread:
        return None
    # 相关度闸（20260920）：**只允许越读越高分**——已读候选里的最高分即"手头最好的
    # 证据"，再读不高分候选是纯噪声。事故（真实 trace 20260920 00:55:28）：问"有没有
    # 你的设计文档"，rag 候选 7.54 / 2.76(Git 教程) / 1.53 / 1.45 / 1.41，改读却连着
    # 读了 19→16→46 三篇全文（40.7s），后两篇对回答零贡献。语料只有 10 篇而 top_k=5，
    # 每次检索固定倒回半个语料库，低分行不是"漏网的语义命中"而是 top_k 的填充物——
    # 全库 88 次 rag_search 里 id=19 出现 83 次、id=16 出现 79 次即是证据。kw 行不受
    # 此闸约束（无分数，相关性由 _title_relevant 的标题词元重叠保证）。
    read_scores = [r[4] for r in ordered if r[0] in done_ids and r[4] is not None]
    if read_scores:
        read_max = max(read_scores)
        kept = [r for r in unread if r[3] == "kw" or (r[4] or 0) > read_max]
        if len(kept) < len(unread):
            logger.info("[planner] 候选改读闸：已读最高分 %.4f，跳过 %d 条不高分候选",
                        read_max, len(unread) - len(kept))
        unread = kept
    if not unread:
        return None
    kw_hit = [r for r in unread if r[3] == "kw" and _title_relevant(r[2], terms)]
    rag_pool = sorted((r for r in unread if r[3] == "rag"),
                      key=lambda r: r[4] or 0.0, reverse=True)
    pick_pool = kw_hit or rag_pool
    if not pick_pool:
        logger.info("[planner] 候选无一与检索实词（%s）对得上号 → 不硬读，如实收尾",
                    "、".join(sorted(terms)) or "（无实词）")
        return None
    mech = [r for r in pick_pool if not _EXPERIENCE_TITLE_RE.search(r[2])]
    pick = (mech or pick_pool)[0]
    plan_obj = instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail",
         "args": {"article_id": int(pick[0]), "doc_type": pick[1]}}]})
    plan_obj["params"] = {"calls": [{"tool": "get_article_detail",
                                     "args": {"article_id": int(pick[0]),
                                              "doc_type": pick[1]}}]}
    plan_obj["note"] = ((plan_obj.get("note") or "")
                        + f"（确定性改读候选《{pick[2][:24]}》全文）")
    return plan_obj


def _doc_title(raw: str) -> str:
    """从详情工具返回（Python repr 的 dict）里取标题——跨轮执行记忆的指代锚点。

    20260912：execution_log 行此前只记"读取文章 19"，下轮用户说"那篇讲架构的"
    无从核对（id 无语义）；回执带标题后 render 侧可展示《标题》，跨轮指代与
    核对才有依据。
    """
    m = re.search(r"['\"]noteTitle['\"]\s*:\s*['\"]([^'\"]{1,80})", raw)
    return m.group(1) if m else ""


def _any_error_frame(messages: list) -> bool:
    """帧里是否有 __ERROR__（错误修正重试合法，跳过重复拦截）。"""
    return any(str(getattr(m, "content", "")).lstrip().startswith("__ERROR__")
               for m in messages if isinstance(m, ToolMessage))


def _terminal_plan(has_frames: bool, reason: str, note: str = "") -> dict:
    """确定性收尾计划（不经 LLM）：有工具帧 → content_query 如实收尾；无帧
    → chat 如实说明无法确认。reason 注入 note 说明收尾原因（轮次上限/受阻
    复盘终局共用——reflector wrap_up 与规划超限同性质，不静默 accept）。

    `note` 可直接覆盖整段 note（20260921 剔空纠偏失败时用）：拼出来的句子会变成
    "……且无任何工具执行记录：如实告知……"这种叠句，而 narrator 对叠句的处置是
    抓一个它记得住的——纪律文案要一句话说清。传了 note 就不再拼 reason。
    """
    if has_frames:
        if note:
            return {"skill": "content_query", "tools": [], "note": note,
                    "reply": SKILL_MAP["content_query"].reply_contract, "chat": False}
        return {
            "skill": "content_query",
            "tools": [],
            "note": (f"{reason}：基于以上已有工具返回如实收尾作答；工具返回不足"
                     "以回答时如实告知'站内没有找到/暂时无法确认'，不得再用模型"
                     "记忆硬答"),
            "reply": SKILL_MAP["content_query"].reply_contract,
            "chat": False,
        }
    if note:
        return {"skill": "chat", "tools": [], "note": note,
                "reply": "直接回答", "chat": True}
    return {
        "skill": "chat",
        "tools": [],
        "note": (f"{reason}且无任何工具执行记录：如实告知暂时无法确认/无法回答，"
                 "不得编造"),
        "reply": "直接回答",
        "chat": True,
    }


def _wrap_up_plan(has_frames: bool, reason: str = "", note: str = "") -> dict:
    """规划轮次上限强制收尾计划（确定性，不经 LLM，20260903 语义不变）。

    reason 可覆盖默认文案（20260912：检索重复拦截改判收尾时若仍写"已达轮次上限"
    会误导 narrator 与事后复盘——收尾原因要如实）；note 直接覆盖整段注记。
    """
    return _terminal_plan(has_frames,
                          reason or f"已达规划轮次上限（{MAX_PLAN_ROUNDS}）",
                          note=note)


def _tool_name(tool_spec: str) -> str:
    """TOOLS 行条目 → 工具名（'get_article_detail({"article_id": 21})' → get_article_detail）。"""
    return tool_spec.split("(", 1)[0].strip()
