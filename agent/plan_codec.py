# -*- coding: utf-8 -*-
"""计划文本契约的**编解码**：`plan_encode`（写端）/ `parse_plan`（读端）一族。

**为什么单独成模块**（20261017 拆 `graph.py` 第一刀）：这一族此前住在 `agent/graph.py`，
而那份文件已经长到 13994 行 / 259 个模块级定义（占全仓 Python 的 39%，见
`tests/test_module_budget.py` 的台账）。它同时是图文件里**边界最干净**的一块：对外只有
三个名字（`plan_encode` / `plan_state` / `parse_plan`）、一套文本协议、**零图拓扑依赖**
——进一份计划对象、出一段文本，反过来一样。

**这一族是什么**：planner / execute / gate / narrator 之间传的那份**内部计划文本协议**
（`SKILL=` / `STATUS=` / `PARAMS=` / `TOOLS:` / `NOTE:` / `REPLY:`；第 6 行 `TODO:` 于
20261016 整行退休）。写端唯一入口是 `plan_state`——一次写出两态：人读的文本（`plan`）
＋ 程序读的对象（`plan_obj`）；读端是 `parse_plan`——容错全解析，按 `KEY[:=]` 搜索、
**不依赖行序与分隔符**。

⚠️ **别与"模型自己写契约行"那条通道搞混**：那条（`extract_plan_fields` 一族）20261004
随文本契约档一起删除。**这份协议仍在用**——系统自己写、系统自己读回来，模型一个字都不
填（模型那一侧是工具调用，见 `agent/planner_llm.py`）。

**一条硬不变量**：TOOLS 行是 `"; ".join(specs)` 拼的、`parse_plan` 用 `split(";")` 读回
⇒ 值里一个裸 `;` 就把**一条调用撕成 N 条**（20260929 生产实证，取证见 `_esc_spec`）。
所以**分隔符不许长在值里**，转义只有这一处。

**搬家方式**：`agent/graph.py` 按原名 re-export 这三个名字 ⇒ 那边剩的 **6 处**调用点与
全仓 `from agent.graph import plan_state` 那几十处引用（含测试）**一个都不用改**。判据侧
同理：`tests/_ctx_src.plan_codec_src()` 把两份文件拼成一份取值视图，`test_plan_channel`
① 与 `test_status_judgements` ④ 的**断言字面一个字没动**——判据没被改松，只是锚点跟着
代码搬（同 `_ctx_src.deprefix` / `.prose()` 那两次）。
搬动时正文**逐字未动**，只同步了一处路径指针（`agent/skills.py` 里"同族教训见 …"那句）。
"""

import json
import re

from agent.skills import PLAN_STATUS_VALUES, SKILL_MAP

# ---------------------------------------------------------------------------
# 容错解析工具（计划文本 → 结构化）
# ---------------------------------------------------------------------------

# ⚠️ 这里曾经还有一族"从 planner 自由文本里抠 SKILL=/PARAMS="的容错解析器
# （`extract_plan_fields` / `_PLANNER_OUTPUT_RE` / `_SKILL_QUOTED_RES` / `_parse_params`
# / `_plan_body_of`）——20261004 随文本契约档一起删除。它与下面 `parse_plan` 读的
# **内部计划文本协议**是两件事：那套协议（`SKILL=` / `PARAMS=` / `TOOLS:` / `NOTE:` /
# `REPLY:`，由 `plan_encode` 写、`parse_plan` 读）由 execute/gate 消费，
# **仍在用**；删掉的只是"模型自己写契约行"那条通道。
# （协议里那个可选的 `TODO:` 行 20261016 也删了——它不是"模型写契约行"那条通道的
#   一部分，是**系统自己写、再自己读回来**的一格，读端为零，见 `plan_encode`。）


def _loads_tolerant(text: str):
    """JSON 容错解析：常见漂移（单引号、尾逗号、行注释）逐个修正后重试。

    解析失败返回 None（调用方决定兜底），不抛异常。

    **保留**（不是文本档私有）：`parse_plan` 读内部计划文本协议的 `PARAMS=` 时用它
    （见本文件下方 `parse_plan`），删掉会把保留下来的那套协议一起弄坏。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r"//.*$", "", text, flags=re.M)
        fixed = fixed.replace("'", '"')
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None


def _esc_spec(tool_spec: str) -> str:
    """单条 spec 里的 `;` 转义成 `\\u003b`——**文本通道的分隔符不能出现在值里**。

    `plan_encode` 用 `"; ".join(tools)` 拼 TOOLS 行，`parse_plan` 读回时 `split(";")`
    （`_tool_args` 的贪婪正则能兜住参数里的 `(`/`)`，**兜不住 `;`**）。20260929 生产
    实证：模型给 `device_oled_draw` 的 `ops` 写了一串用 `;` 分隔的伪指令（12 个分号，
    `tri(30,6,…); circle(64,38,26,F); …`）⇒ **一条调用裂成 13 条 spec**：12 条被当成
    不存在的工具（`tri`/`circle`/`line`/`text`）逐个拒掉，第 13 条（真名那条）截断在
    第一个 `;` 上 ⇒ `args_parse` BLOCK。最狠的一层是它**不像失败**：那一轮工具其实
    跑了、设备也回了执（"已下发、设备已确认执行"都印出来了），但 spec 解析不出参数 ⇒
    checker 判 BLOCK ⇒ 无回执 ⇒ **屏幕画了、台账没记一笔**，跨轮执行记忆里什么都没有。
    （`device_oled_draw` 当天随画板功能一起撤掉了，此处是历史取证；这个转义与绘图
    无关——任何参数值里带 `;` 的调用都会中招。）

    `\\u003b` 是合法 JSON 转义，`json.loads`/`ast.literal_eval` 读回都会还原成 `;`，
    **语义一个字节没变**，只是文本里不再有裸 `;`。写在这里而不是那 13 个拼 spec 的地方
    （`skills.py` 12 处 + 本文件 1 处）：写端只有这一处，且"改一处要同步十三处"正是
    全仓审计点名的主导特征。分隔符本身由 `join` 产出、不经过这里，所以转义的只有值里的。
    **PARAMS 行不用**：它不参与任何 `;` 切分（`json.dumps` 已把换行转义掉）。
    """
    return tool_spec.replace(";", "\\u003b")   # 落盘即 `;`（一个反斜杠，JSON 转义）


def plan_encode(plan_obj: dict) -> str:
    """结构化计划（instantiate_plan 产物）→ plan 字段（契约的写端）。

    `STATUS=` 行（20260926 批 3）**由系统写死**，模型一个字都不填——所以它一定
    排在 SKILL 之后（紧跟"这是份什么计划"，与人读的顺序一致），而**不是**塞在
    REPLY 附近：`parse_plan` 的 REPLY 正则吃 DOTALL，它必须是末行。

    派生规则（`plan_obj` 没带 status 时）：有工具 → `executed`；chat 技能 →
    `answer_only`；其余留空。**留空不是"忘了填"的唯一形态，也不全是缺陷**：写技能
    零工具那一族（缺必填/目标查无此名）目前也落在这里，而它们各自都带了系统写的
    注记——所以 `""` 只说明"没有构造点用一个值认领这一轮"，消费侧据此 fail-open
    （判据读不到就跳过），`eval/corpus_invariants.py` 的 I2 拿它当"未记账"档、
    **不拿它当缺陷计数**。**别把这条派生当成判据的常态入口**——各构造点都显式给值
    （navigate 三出口 / `_param_problem_plan` / `_terminal_plan` / fail-closed 写技能），
    派生只兜住"手写的夹具文本"与"改造前留在 state 里的旧计划"。
    """
    tools = ("（无）" if not plan_obj.get("tools")
             else "; ".join(_esc_spec(t) for t in plan_obj["tools"]))
    status = plan_obj.get("status") or (
        "executed" if plan_obj.get("tools")
        else ("answer_only" if plan_obj.get("chat") else ""))
    lines = [
        f"SKILL={plan_obj['skill']}",
    ]
    if status:
        lines.append(f"STATUS={status}")
    lines += [
        f"PARAMS={json.dumps(plan_obj.get('params', {}), ensure_ascii=False)}",
        f"TOOLS: {tools}",
        f"NOTE: {plan_obj.get('note') or '（无）'}",
    ]
    # ── 第 6 行 `TODO:` 于 20261016 整行退休（写端 + 读端 + 提示词契约同批）──
    # 它当年是"多步链的中间轮声明后续依赖步骤"的一格。退休的取证是三件事叠起来：
    #   ① **读端为零**。全仓只被 `plan_encode` 自己回渲染一遍，再由 `parse_plan`
    #      读回同一个列表（`plan_obj["todo"]`）——一个闭环，没有消费方；
    #   ② **它的名义消费者早已被换掉**。`agent/tasks.py`（20260927 批 D）就是来补
    #      "还剩什么没做完"这个缺口的，那个模块自己的头注写着这一行"只有 trace 消费
    #      （有写无读）"；`eval/corpus_invariants.py` 的 I6 则把它当成"模型写了没人读"
    #      的**反面教材**引用。当初注释里写的"供 reflector 判链依赖"，实测不成立：
    #      全量 1194 份 trace 里 `planner.todo` 只出现过 **7 个请求 / 10 次**，而
    #      reflector 跑过的请求是 51 个，**两者交集为 0**——reflector 从没收到过它；
    #   ③ **它在场时还常常是垃圾**。`_parse_todo` 吃的是模型正文，而模型会照抄提示词
    #      里的占位符模板 ⇒ 生产 trace 里真出现过
    #      `todo=['<步骤1', '<步骤2', '`". So I\'ll write TODO: …']` 这种把模板和
    #      自述一起切进来的结果。`native-toolcalls-mainline.md` §6.6 的纪律是
    #      "必须有消费者，否则不许写"——两条出路里选了删掉这条（另一条是补一个
    #      扫描不变量），因为消费者已经在 `agent_task` 表上了，再补一个只是把
    #      同一件事记第二遍。
    lines.append(f"REPLY: {plan_obj['reply']}")
    return "\n".join(lines)


def plan_state(plan_obj: dict) -> dict:
    """**写计划的唯一入口**：同一份计划出两态，一次写入——人读的契约文本
    （`plan`，进提示词/进 trace/进测试夹具）＋ 程序读的结构化对象（`plan_obj`）。

    **为什么要有它（20260928，架构审计第 ② 条的一半）**：此前 24 个计划构造点各自写
    `{"plan": plan_encode(plan_obj), …}`，而**读端**要拿其中某个字段时只能再去抠那段
    文本——`server.py` 用 `plan.startswith("SKILL=")` 判"这是不是一份真计划"、
    `"\nTOOLS: " in plan` 判"有没有执行清单"，`_plan_skill` 又自己写了一条
    `SKILL=` 正则（与 `parse_plan` 里那条是两份拷贝）。三处都是"改 `plan_encode`
    的排版必须同步改三个读端"的人工约定。现在读端直取 `state["plan_obj"]`。

    **两态必须**由这一处一起给**（而不是各构造点自己填两遍）：文本是派生物，
    谁漏了 `plan_obj` 谁就让读端退回文本抠字——`tests/test_plan_channel.py` 用源码锁
    钉住"`plan_encode` 只许在这个函数里被调用"，构造点想绕开它就得先删掉那条锁。

    ⚠️ 别把 `plan_obj` 当成"文本的缓存"来用后又去改它：它进了 state 就是**程序读
    计划的首选**来源，两态一旦分叉，判据会照着对象走、而人照着文本吵。

    **文本仍然有人读**（别把"改掉三处"读成"文本没人读了"）：`plan` 进 narrator
    提示词（`_narrator_plan`）、进 trace、进测试夹具；`parse_plan` 这个**容错全解析器**
    仍在 `route_after_planner`/`execute_node`/`gate_node`/`_wrote_this_round` 四处用它
    （它按 `KEY[:=]` 搜索、不依赖行序与分隔符，是"契约的读端"而不是"排版的嗅探"）。
    批 C 治的是**排版嗅探**，不是要废掉文本态。
    """
    return {"plan": plan_encode(plan_obj), "plan_obj": plan_obj}


def parse_plan(raw: str) -> dict:
    """解析 plan 字段（契约的读端）。容错：解析失败 → 按 chat 兜底（宁可少干活，不硬猜）。

    返回 {"skill", "params", "tools", "note", "reply", "chat", "status"}。
    `status` 见 `PLAN_STATUS_VALUES`（批 3）：系统自己写的计划一定带 `STATUS=` 行，
    缺了才走下面那段兼容派生（判据别依赖派生，它只是给旧文本留的路）。
    容错原则：所有"LLM 输出 → 程序消费"的边界都要能优雅降级——LLM 不是
    JSON 解析器，输出格式漂移是常态（解析失败 → 按 chat 兜底，宁可少干活）。

    ⚠️ 这里的输入是**系统自己写的**契约文本（`plan_encode` 的产物，只经过一次
    `state` 存取），不是模型原始输出 ⇒ 用顶格正则即可。模型那一侧 20261004 起
    根本不写文本契约了（native tool calls，见 `agent/planner_llm.py`），所以
    这里**不需要**也不该再有"容错抠模型自由文本"的第二套解析器——曾经有过一份
    （`extract_plan_fields`，20260926 为 JSON 漂移加的），随文本档一起删除。
    """
    m = re.search(r"SKILL\s*[:=]\s*(\w+)", raw or "", re.IGNORECASE)
    skill = m.group(1) if m else "chat"
    params = {}
    pm = re.search(r"PARAMS\s*[:=]\s*(\{.*?\})\s*\n", raw or "", re.IGNORECASE | re.DOTALL)
    if pm:
        obj = _loads_tolerant(pm.group(1).strip().strip("`"))
        if isinstance(obj, dict):
            params = obj
    tools = []
    tm = re.search(r"TOOLS\s*[:=]\s*(.+)", raw or "", re.IGNORECASE)
    if tm:
        tools = [s.strip() for s in tm.group(1).split(";") if s.strip() and s.strip() != "（无）"]
    nm = re.search(r"NOTE\s*[:=]\s*(.+)", raw or "", re.IGNORECASE)
    note = nm.group(1).strip() if nm else ""
    rm = re.search(r"REPLY\s*[:=]\s*(.+)", raw or "", re.IGNORECASE | re.DOTALL)
    reply = rm.group(1).strip() if rm else ""
    sm = re.search(r"STATUS\s*[:=]\s*(\w+)", raw or "", re.IGNORECASE)
    status = sm.group(1).strip().lower() if sm else ""
    if status not in PLAN_STATUS_VALUES:
        status = ""
    if not status:
        # 兼容派生（20260926 批 3）：**只兜旧文本与手写夹具**。系统自己写的计划
        # 一定有 STATUS 行（`plan_encode`），所以下面这段不会在常态里跑到——
        # `tests/test_status_judgements.py` 有源码锁钉住这一点。
        #
        # 为什么不留空、非要派生一遍：留空 = 判据 fail-open，而"缺 STATUS"最可能
        # 的来历正是**改造前留在 state 里的旧计划文本**——那时判据判的是注记措辞，
        # 派生一遍等于把旧行为原样接上，不会因为升级而突然少拦一类（也不会突然
        # 多拦——派生只认系统自己那三条注记的**固定前缀**，见 skills.py）。
        if tools:
            status = "executed"
        elif skill == "chat":
            status = "answer_only"
        elif skill == "navigate" and "不调用任何工具" in note:
            # ⚠️ "未部署" 必须排在 "已下线" **之前**：`_IOT_OFF_NOTE` 里那句叮嘱
            # （"不要说「已下线」"）本身含"已下线"三个字，先判它就会把"IoT 没装"
            # 派生成"页面下线了"——正是这两个值分开要防的那件事，从这里漏回来。
            if "未部署" in note:
                status = "nav_iot_off"
            elif "已下线" in note:
                status = "nav_offline"
            elif "无法识别" in note:
                status = "nav_unresolved"
            elif "不存在" in note:
                status = "target_unreachable"
    return {
        "skill": skill if skill in SKILL_MAP else "chat",
        "params": params,
        "tools": tools,
        "note": note,
        "reply": reply,
        "chat": (skill in SKILL_MAP and SKILL_MAP[skill].chat) or skill == "chat",
        "status": status,
    }
