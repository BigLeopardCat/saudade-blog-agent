"""动作族轮次的"系统事实块"（roadmap D3：把动作轮的叙述从"生成"降级为"包装"）。

**为什么存在**：narrator 是回复的**作者**，而动作轮的事实（跳转到哪、特效开没开、
标签建没建成）本来就在系统手里——回执是 checker 验收过的事实、命令本体已经走
`cmd` 字段离开了文本（20260926 批 2）。让模型当这段事实的作者，换来的是两个洞的
温床（洞① 凭空声称完成、洞⑤ 照抄工具返回里的工具名），而它写的字里 75%–82%
本来就是复述系统已有的那句话（`eval/narrator_facts_share.py` 的量化，命令族+写族
153 轮）。所以：**事实由系统印，模型只写包装**。

**射程只有两族**（命令族 + 写族，与量化脚本**同一套分类**——口径与数字必须同源，
否则"能砍多少"这句话就不可核）：

- **命令族**（`cmd` 非空：导航/特效/夜间）与 **写族**（工具名前缀，见
  `_WRITE_PREFIX_RE`）：它们的 `result` 文本**本来就是给人看的中文事实**
  （「页面已跳转：…」「标签「音乐」已创建」）⇒ 系统原样印出来，一个字不用重写。
- **数据族**（其余）：`result` 是 JSON，模型现在干的事恰恰是"把 JSON 讲成人话"——
  砍掉它等于把 JSON 甩给用户，比现在还差。**刻意不收**。

**但"分族"与"印不印"是两件事**，而且这一侧被**两次**收窄过：

- **20261002（主人拍板）命令族退出**：跳转/特效/夜间的效果就发生在主人眼前的页面上，
  他当场看得见，而那句话本该由泠月自己交代——系统插一行"公告"会把回答变成机房播报。
- **20261005（主人拍板）写族也退出**：`BLOCK_FAMILIES = ()`，**系统不再代印任何一行**。
  理由是同一件的另一面——「〔系统〕 …」是**机器在气泡里说话**，主人要的是泠月把它
  说出来；写操作主人看不见，但那不等于"系统得替他说"，而是"他更得说清楚"。

**所以本模块现在只剩两件活**：① 分族（`family_of` / `is_action_family`，量化脚本
`eval/narrator_facts_share.py` 的"能砍多少"按它算，跨模块同源不能动）；②
**历史剥离**（`strip_fact_lines`）——库里 20261005 之前的回复仍带着那种标记行，
它们进模型语境时必须照旧抹掉（现场见 `strip_fact_lines` 头注）。
印出与渲染的函数（`action_facts` / `render_fact_block`）**原样留着**：它们是那次
回退的一行开关，删掉它们等于把"印不印"从可配置变成从代码里消失。

详见 `BLOCK_FAMILIES` 的注释。

**边界（如实记）**：

- 块里是**回执的 result 文本**，而回执在 agent 侧已截断（`result` `[:200]`，见
  `agent/graph.py` 的回执构造）⇒ 块解决的是"谁来陈述"，不是"陈述得更全"；
- 只收 **checker PASS** 的回执（失败执行/`__ERROR__` 帧从来进不了 receipts）——
  这是本模块敢自称"事实"的全部依据，别在别处另接一份数据源；
- 块与 narrator 的关系是**分工不是去重**：模型被要求不复述（见 `_EXECUTOR_PROMPT`
  的纪律 23），但即便它复述了，用户读到的仍是系统那句（两块并存，事实以系统为准）；
- **块只给主人看，不给模型看**（20261002 补）：正文进库、进历史，下一轮它就成了模型
  "自己说过的话"。实测被原样抄进下一轮开头（见 `strip_fact_lines`），所以注入点在
  **行首标记**上做确定性剥离——模型的证据是工具回执与执行台账，不是这段印出来的字。

**跨模块**：`eval/narrator_facts_share.py` 读 trace 做同一套分族（trace 里
`execute.call` 事件带 `cmd` 与 `name`）——两边分类必须一致，所以族的判据只有这里
一份实现，那边 import 它。
"""

from __future__ import annotations

import re

FAMILY_CMD = "cmd"        # 命令族：导航/特效/夜间（命令本体走 cmd，result 是中文事实）
FAMILY_WRITE = "write"    # 写族：后台写工具（result 是 adminops.render_* 的产物）
FAMILY_DATA = "data"      # 数据族：返回是 JSON，讲人话是模型的活（D3 不碰）

ACTION_FAMILIES = (FAMILY_CMD, FAMILY_WRITE)

# **印给主人看的那一半**（20261002 收窄到写族、20261005 收成空集——两次都是主人拍板）。
#
# **为什么命令族先退出**（20261002）：跳转/特效/夜间的效果就发生在主人眼前的页面上——
# 他**当场看得见**，系统再播报一遍「〔系统〕 页面已跳转：…」是多余的；更要紧的是那句话
# 本该由泠月自己交代，系统插一行"公告"会把回答变成机房播报（现场：整页目标那行还带着
# "（本条回复说完再跳）"，主人读到的是一句**系统在解释自己的时序**，而不是 agent 在回答）。
#
# **为什么写族也退出**（20261005，主人报"agent 最近又出现一次[系统]这种回复"）：气泡里
# 那行「〔系统〕 已在后台首页的待办里加了一条…」（生产 trace `20261005T000153`，uid=1
# 会话 320）**不是模型抄的，是服务端印的**——而主人要的不是"系统替它说"，是"它自己说"。
# 当初留下写族的理由是"主人看不见标签被创建，那是他唯一的确定性事实来源"；这条理由
# **站不住**：事实的来源是**回执**，不是**气泡里那行字**——回执、执行台账、跨轮记忆
# 一个字都没少给，少掉的只是"系统抢过话筒"。
#
# **事实供给一个字没少**（这条是"退出印出"安全的全部依据，20261005 逐条核对）：
#   · 模型照旧拿到工具帧与执行回执（`graph.model_node` 的 `_drop` 由 `is_block_family`
#     算 ⇒ 空集 ⇒ **一个字都不摘**，此前写族的帧恰恰是被摘掉的）；
#   · 执行台账照旧落 `execution_log`（跨轮问"刚才真跳了吗/标签建了吗"仍有据可查）；
#   · 命令帧照旧驱动浏览器（`__CMD__` 与印不印无关）。
BLOCK_FAMILIES: tuple[str, ...] = ()

# 事实行的**说话人标记**：盖在每一行行首，进用户可见的正文（`render_fact_block`），
# 但**绝不进模型可见的历史**（`strip_fact_lines` 在注入点抹掉）。它有两个用途，缺一
# 不可：① 对主人如实署名——这句话是系统印的，不是泠月的措辞；② 给"剥离"一个**确定性
# 判据**（按形状猜"哪句像系统写的"必然漏；gate 5g 的豁免至今也只是"这个形状只有系统
# 会写"的**假设**——标记把假设变成可判的事实）。改这个常量要连着 `tests/test_factblock.py`
# 与 golden 里的事实行断言一起改。
FACT_MARK = "〔系统〕 "

# 写族工具的命名族。**不追求穷举**：分不出来的落 data，那是保守方向（把写算成
# data 只会让"能收归系统的比例"被低估，不会把数据族的 JSON 甩给用户）。
WRITE_PREFIX_RE = r"^(create|update|delete|set|add|remove|move|audit|freeze|unfreeze|send|complete)_"


def family_of(tool: str, has_cmd: bool) -> str:
    """回执属于哪一族。`has_cmd` 由调用方给（回执顶层有没有 `cmd` 键）。"""
    if has_cmd:
        return FAMILY_CMD
    if re.match(WRITE_PREFIX_RE, tool or ""):
        return FAMILY_WRITE
    return FAMILY_DATA


def is_action_family(receipt: dict) -> bool:
    """动作族（**分类概念**：有回执、结果是人话）——不等于"会印给主人看"。"""
    return family_of(str(receipt.get("tool") or ""), bool(receipt.get("cmd"))) in ACTION_FAMILIES


def is_block_family(receipt: dict) -> bool:
    """会不会进**用户可见**的事实块（= `BLOCK_FAMILIES`，理由见那个常量）。

    分族与"印不印"是两件事，所以两个判据都留着：`eval/narrator_facts_share.py` 的量化
    口径按**分族**算（"这一轮有几个动作"与"气泡里印了几行"不是一个数），事实块按这个
    算。合成一个的话，改印量会顺手改掉一个评测指标的含义。

    **20261005 起恒为 `False`**（`BLOCK_FAMILIES` 空集）。它在 `graph.model_node` 里还有
    第二个用处——**决定哪些帧/回执从提示词里摘掉**，而"摘"必须与"印"同宽：印了才摘
    （主人已经读到，不必再邀请复述），不印就一个字都不许摘（那是 narrator 唯一的依据）。
    空集让它一起归零，正是这条纪律应有的结果。
    """
    return family_of(str(receipt.get("tool") or ""), bool(receipt.get("cmd"))) in BLOCK_FAMILIES


def action_facts(receipts: list) -> list[str]:
    """**印给主人看的**事实文本（去重、保持顺序）——只收 `BLOCK_FAMILIES`。

    20261005 起 `BLOCK_FAMILIES` 是空集 ⇒ **本函数恒返回 `[]`**（写族也退出印出，
    见那个常量的注释）。留着它是因为两个调用点（`server.py` 的 producer、
    `agent/graph.py:model_node`）都按"可能有一块"的形状写的：恢复印出是改那一个
    常量的事，不是把四处接线重新接一遍。

    顺序 = 执行顺序（receipts 是累计语义），所以调用方直接拼接即可。
    去重是必要的而不是好看：同一轮的重复调用（同一次导航跑三遍，实测有）在
    用户可见文本里没有第二次的意义，而没有它主人在气泡里会读到三行一模一样的话。
    """
    out: list[str] = []
    for r in receipts or []:
        if not isinstance(r, dict) or not is_block_family(r):
            continue
        text = str(r.get("result") or "").strip()
        if not text or text.startswith("__ERROR__"):
            continue
        if text not in out:
            out.append(text)
    return out


def render_fact_block(lines: list) -> str:
    """事实块正文：一行一条、行首盖**说话人标记**（`FACT_MARK`），事实文本一字不改。

    **标记不是标签，是说话人**（20261002 补）：这句话此前没有任何署名，读者（主人
    **和下一轮的模型**）只能按"气泡里的话都是泠月说的"去归属——而它恰恰不是泠月说的。
    "不改写、不加标签"防的是**改动事实本身**（把 URL 抹掉、把"已创建"说成"已提交"），
    盖一个"这句来自系统"的说话人标记不在此列：它一个字都没动。而且标记正是
    `strip_fact_lines` 敢做**确定性**剥离的前提（按形状猜"哪句像系统印的"必然漏）。
    """
    out = []
    for x in lines:
        s = str(x).strip()
        if not s:
            continue
        out.append(s if s.startswith(FACT_MARK) else FACT_MARK + s)
    return "\n".join(out)


def strip_fact_lines(text: str) -> str:
    """把系统印的事实行从**模型可见的文本**里抹掉（历史注入用，20261002）。

    为什么必须抹（生产实证 `20261001T230954`）：事实块随回复一起落库，下一轮作为
    assistant 历史回到模型眼前时就变成了"**它自己说过的话**"——那一轮只跑了
    `device_oled_display`、一次导航都没有，narrator 却把上一轮那句
    「页面已跳转：https://saudade.site/device-console/」**原样抄在了本轮回复的开头**，
    主人连着两个气泡读到同一句"已经跳到某个 URL"（用户报的"显示两行已经转跳"）。
    系统印的话不该变成模型的范文；同一批 `__RESET__:<scope>` 的取舍也是这条纪律
    （作废的叙述不让它留在历史里当先例）。

    **只认行首标记**（`render_fact_block` 是唯一盖章处）。抹完若整段为空 ⇒ 返回空串，
    调用方据此把这一轮整个跳过（一条空的 assistant 会让模型把上一轮的用户问题当成
    待答问题——与"孤儿 user"同一个坑，见 `server.py::_build_messages`）。
    """
    keep = [ln for ln in str(text or "").splitlines() if not ln.lstrip().startswith(FACT_MARK)]
    return "\n".join(keep).strip()


def block_of(receipts: list) -> str:
    """`action_facts` + `render_fact_block` 的组合壳（调用方最常用的两步）。"""
    return render_fact_block(action_facts(receipts))


def compose(prelude: str, body: str) -> str:
    """用户可见正文 = 事实块（前） + narrator 正文（后），空行分隔。

    三个调用点共用（`server.py` 的流式收尾/fallback 与非流式），值得一个纯函数——
    "块在前"是这一批的语义（主人先读到事实，再读包装），分散在三处拼字符串正是
    某一天漂移成"块跑到了后面"的形状。
    幂等：正文已经以块开头时不再重复它。这条不是洁癖——下游有 2 条"正文已经是块"
    的来路（`__RESET__` 之后的重印、以及任何一条下发文本恰好等于块的兜底），没有它
    主人会读到同一行事实两遍。"""
    p, b = (prelude or "").strip(), (body or "").strip()
    if not p:
        return b
    if not b or b.startswith(p):
        return b or p
    return p + "\n\n" + b

