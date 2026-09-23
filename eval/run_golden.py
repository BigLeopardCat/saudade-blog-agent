# -*- coding: utf-8 -*-
"""L2 golden set 最小版运行器：真实 agent 端到端（真实 LLM + 真实工具）。

每条 golden 样本断言"行为"而非"实现"：
  - 动作通道：命令帧（EFFECT:/NAVIGATE:/AUTO_NAVIGATE:/DARKMODE:）是否如期望产生/禁止
  - 文本关键词 / 非空
  - 文本语义断言的两种兜底（20260912，防"判据脆弱→红斑常态化"）：
      text_any_regex           正断言的正则族，与 text_contains 为 OR（近义表述任一命中）
      not_contains_exempt_quote 负断言的否认豁免（opt-in）：模型撤回上一轮谎称时必然
                               引述那句话，邻域含撤回标记（「之前说…是错的」）不算违规；
                               同理紧贴否定词的「没成功显示」是诚实否认，也不算（9/16）
    两项均由夜间假失败实证引入（见 ~/agent_regression.log 9/10、9/11、9/16 与 eval/report/review_*.md）
  - 参数族断言：require_arg_from_result（消费者参数取自生产者回执的结构化字段）
                require_exec_args（执行回执里某工具某参数**等于**期望值——没有
                producer 可挂时用，如 20260920 确定性文档锚点解析出的 id）

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/run_golden.py               # 全量（本机=生产链路，耗时基线有效）
  .venv/bin/python eval/run_golden.py --limit 3     # 前 3 条（调试）
  .venv/bin/python eval/run_golden.py --only nav_friends_down
  .venv/bin/python eval/run_golden.py --only rag_python_is,rag_arch_components  # 多选（链路诊断）
  .venv/bin/python eval/run_golden.py --min-pass-rate 0.9 --skip-ids device_query  # CI 口径
退出码：0=达到 --min-pass-rate（默认 1.0，即全过）1=低于门禁
回归组（tags 含 regression）另按硬判（100%，不受 --min-pass-rate 放宽）；20260924 起
判据以**首跑红后复跑一次**的终判为准，复跑绿的那批记进 regression.flaked_ids 并单列
打印（放行但必须有人看——首跑红/复跑绿两条都在报告与复审单里，不许静默宽恕）。
⚠ 通过率 vs 全过：本机（生产链路）默认全过；CI 在北美 runner 上跨网调用 LLM/站点，
  单条超时类波动与"环境不可达"用例不该让整轮门禁变红——门禁按**通过率**判，
  失败清单仍逐条打印、报告随 artifact 上传（见 .github/workflows/eval.yml）。
"""
import argparse
import asyncio
import contextvars
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 复用 server 内部链路（不走 HTTP，与 test_fallback_replay.py 同模式）
import server
from server import ChatRequest, _build_messages, _run_agent_stream_to_queue
from agent import create_agent
from agent.principal import Principal  # 管理助手用例的调用者身份（20260921）
from langchain_core.messages import AIMessageChunk, ToolMessage

import golden_trace  # 同目录（eval/ 在 sys.path 上，同 corpus_check 的用法）

CMD_PREFIXES = ("EFFECT:", "NAVIGATE:", "AUTO_NAVIGATE:", "DARKMODE:")
# 导航命令帧族：AUTO_NAVIGATE 与 NAVIGATE 同属"导航已执行"，断言时视为一族
# （golden 里 require/forbid "NAVIGATE:" 时 AUTO_NAVIGATE 帧同样计入/计入禁止）
CMD_FAMILIES = {
    "NAVIGATE:": ("NAVIGATE:", "AUTO_NAVIGATE:"),
    "AUTO_NAVIGATE:": ("NAVIGATE:", "AUTO_NAVIGATE:"),
    "EFFECT:": ("EFFECT:",),
    "DARKMODE:": ("DARKMODE:",),
}
GOLDEN_FILE = "eval/golden/basic.jsonl"
REPORT_FILE = "eval/report/last_run.json"


def _cmd_matches(pre: str, c: str) -> bool:
    """命令帧 c 是否属于前缀族 pre（导航族归一化）。"""
    fam = CMD_FAMILIES.get(pre, (pre,))
    return any(c.startswith(x) for x in fam)


def ensure_agent() -> None:
    if server._agent is None:
        t0 = time.time()
        server._agent = create_agent()
        print(f"[init] 编译图构建完成：{time.time() - t0:.1f}s")


def build_request(case: dict) -> ChatRequest:
    """用例 dict → ChatRequest。**两个跑法（本脚本 / golden_case_runner.py）共用的唯一构造点**。

    20260920：进程隔离跑法（golden_full_run.py → golden_case_runner.py）此前自己手写了一份
    ChatRequest，漏了 `executions`（20260904 才加进用例 context 的字段）⇒ 那 3 条
    「执行记忆 / 实体摘要」用例在隔离跑法下**必然假失败**（模型看不到 recent_executions，
    如实答"没有执行记录"/重跑工具）。同一份用例两个跑法结论不同的根因就是这份复制粘贴——
    字段表只留这一处，再不许各自维护。
    """
    g = case["gold"]
    ctx = case.get("context", {})
    return ChatRequest(
        message=case["user_input"],
        image=case.get("image", []),  # 多模态：dataURL 数组（image_color_red 等用例；服务端兼容单串）
        current_url=ctx.get("current_url", "/"),
        page_title=ctx.get("page_title", ""),
        user_id=ctx.get("user_id", 0),
        needs_summary=g.get("needs_summary", False),
        current_effects=ctx.get("current_effects", ""),
        current_darkmode=ctx.get("current_darkmode", ""),
        history=ctx.get("history", []),
        summary=ctx.get("summary", ""),
        # 20260904 C3：跨轮执行记忆（模拟 Rust 侧 execution_log 渲染注入——
        # 二轮用例把首轮回执作为 executions 传进来，锁"据记忆如实回答"路径）
        executions=ctx.get("executions", ""),
        # 20260924：跨轮待办（Rust 侧从 pending_action 表读回注入的另一半台账，
        # 与 executions 合一成"确认与执行事实"块，见 server._ledger_block）——
        # 用例带了才能在 golden 里跑到那条路径（gate 洞⑦ 的判据也看这一半）。
        pending_action=ctx.get("pending_action", ""),
    )


def build_principal(case: dict) -> "Principal":
    """用例 dict → 本轮调用者身份（20260921 管理助手用例）。

    **身份不走请求体**（与生产一致）：生产上 role 只来自 Rust 签的
    `X-Agent-Assertion` 头，body 里的角色一律不信；golden 直调内部链路，
    没有那层 HTTP，所以在这里显式构造——形状与 server 解析断言后的
    `Principal` 完全一致（uid 取自 `context.user_id`）。

    缺省（用例不写 `context.role`）= `role=None` = 零权限，**既有 78 条用例
    行为不变**（它们的 context 里本来就没有 role 字段）。
    """
    ctx = case.get("context", {})
    return Principal(uid=ctx.get("user_id", 0), role=ctx.get("role"),
                     source="golden")


def run_one(req: ChatRequest, principal: "Principal | None" = None,
            trace_ctx: dict | None = None) -> dict:
    """跑一轮真实对话（内部链路），从帧流提取最终文本 / 命令帧 / 事件。

    `trace_ctx`（20260922）：`{"run": <run_id>}` 时给这一轮落一份 trace（见
    eval/golden_trace.py，落 golden_traces/<run_id>/<case_id>.json）。**start 必须在
    这里、在把活儿提交给线程池之前**——recorder 靠 contextvar + copy_context 传进
    producer 线程，晚一步落下来的就是空壳。
    """
    trace_id = None
    t_trace0 = time.monotonic()
    if trace_ctx:
        trace_id = golden_trace.start_case(
            trace_ctx["run"], trace_ctx.get("case", ""), req.message or "",
            (principal.role if principal is not None else None))
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    frames = []

    def drain():
        while True:
            item = loop.run_until_complete(queue.get())
            if item is None:
                break
            frames.append(item)
            if isinstance(item, BaseException):
                # producer 异常入队后（server 侧已补 None 哨兵）仍尽早终止——
                # 错误帧不参与正常帧处理，run_one 后处理会把 BaseException
                # 落进 result["error"]（20260905：缺此 break 曾整进程挂死）
                break

    t = threading.Thread(target=drain)
    t.start()
    # **contextvar 要显式传给线程池**（20260922）：`ThreadPoolExecutor.submit` 不拷贝
    # contextvars（只有 asyncio.to_thread 自动做）——照生产 `server._submit_with_context`
    # 的办法，提交前 `copy_context()` 快照、线程内 `ctx.run` 恢复。不这么做的话
    # trace recorder 进不了 producer 线程：落下来的是一份只有元数据的**空壳 trace**，
    # 而"评测有 trace 了"看起来完全正常（实测踩到，靠 golden_trace 的空事件警告抓出）。
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(
            ctx.run, _run_agent_stream_to_queue,
            _build_messages(req), "golden_thread", queue, loop, req.user_id,
            None, principal,
        ).result()
    t.join()
    loop.close()
    # trace 收尾（生产侧这一步在 event_stream 的 finally 里；golden 没有那层壳）
    trace_path = golden_trace.finish_case(trace_id, time.monotonic() - t_trace0,
                                          len(frames))

    final_text = ""
    commands: list[str] = []
    tool_calls: list[str] = []  # 20260902：已调用的工具名（require_tool_calls 断言用）
    exec_rows: list = []  # 20260904：checker 验收回执（__EXEC__ 帧，系统确认事实）
    resets = 0
    resets_reasons: list[str] = []
    tool_rounds = 0   # 效率基线：planner 发出 TOOLS 清单的轮数（🛠 过程帧计数）
    error = None
    # 控制帧原文（20260921）：__PROCESS__ / __RESET__ / __EXEC__ / __CONFIRM__ …
    # ——写操作确认弹窗是**帧级**行为（golden 点不了按钮），只能在帧上断言：
    # "该弹窗时弹了窗、且什么都没写"。刻意收原文而不是布尔：断言侧自己取前缀，
    # 判据与 server/Rust/前端三端的帧契约对得上。
    control_frames: list[str] = []
    for item in frames:
        if isinstance(item, str) and item.startswith("__RESET__"):
            final_text = ""  # REVISE/兜底轮作废 → 清空（与前端最终显示一致）
            commands.clear()  # 被作废轮的命令帧同样作废（前端 RESET 清空 cmdText 后不执行）
            tool_calls.clear()  # 被作废轮的工具调用不算数（断言的是最终采纳轮的执行）
            resets += 1
            reason = item.removeprefix("__RESET__").lstrip(":")
            if reason:
                resets_reasons.append(reason)
        elif isinstance(item, str) and item.startswith("__EXEC__:"):
            # 20260904 C3：跨轮执行记忆帧（__RESET__ 不清——回执是已发生事实，
            # gate fallback 只否定叙述文本不否定执行）
            try:
                rows = json.loads(item[len("__EXEC__:"):])
                if isinstance(rows, list):
                    exec_rows.extend(r for r in rows if isinstance(r, dict))
            except Exception:
                pass
        elif isinstance(item, AIMessageChunk) and item.content:
            final_text += str(item.content)
        elif isinstance(item, ToolMessage) and item.content:
            name = getattr(item, "name", "") or ""
            if name:
                tool_calls.append(name)
            for line in str(item.content).splitlines():
                s = line.strip()
                if s.startswith(CMD_PREFIXES):
                    commands.append(s)
        elif isinstance(item, BaseException):
            error = str(item)
        elif isinstance(item, str) and item.startswith("__PROCESS__"):
            # 效率基线（20260919 / 口径修正 20260919b）：planner 每发一次带 TOOLS
            # 清单的决策就有一条"🧭 计划：<动作>"过程帧 → 其计数即"planner 规划
            # 了几轮工具"。**不能用 "🛠 正在调用工具…"**：那条帧在 server.py 带
            # key=tool_running 去重，每请求最多一条，计数器结构上不可能 >1（首版
            # 口径错误，据此报的"多轮绕圈例 0"作废）。
            if "🧭 计划：" in item:
                tool_rounds += 1
        elif isinstance(item, str) and item.startswith("__"):
            # 其余控制帧（今天只有 __CONFIRM__:）——没有专门分支的走这里收原文，
            # 否则新帧类型在 golden 里静默不可见（"断言写不出来"= 回归测不到）。
            control_frames.append(item)
    return {"text": final_text, "commands": commands, "tool_calls": tool_calls,
            "frames": control_frames,
            "exec_rows": exec_rows,
            "exec_tools": [r.get("tool", "") for r in exec_rows],
            "tool_rounds": tool_rounds,
            "trace": trace_path,
            "resets": resets, "resets_reasons": resets_reasons, "error": error}


# 引述豁免的撤回语境标记（20260912）：关于"模型说过什么"的撤回措辞。刻意不含
# 「没有/并没有/没有真正」等关于"系统做了什么"的否定词——否则"系统没有记录，但已经
# 显示在屏幕上了"这类**重新声称**会被误豁免（禁用词判据要抓的正是它）。
EXEMPT_WINDOW = 24  # 邻域半径（字）：引述通常紧跟撤回语（「…所以我之前说“X”是错的」）
EXEMPT_MARKERS = (
    "之前说", "之前提到", "之前回复", "之前那句", "我前面说", "我说过", "当时说", "刚才说",
    "记错", "我错了", "说错", "讲错", "是错的", "不对的", "不准确", "收回", "更正",
)
# 引述型撤回的第二支（20260913 实证）：禁用词落在成对引号内 + 邻域含自省语。9/13
# 全量回归现场：模型撤回时用 ASCII 双引号包住原话（不该那么快就说"已经打开啦"），
# 邻域只有 抱歉/不该/没有看到，不在上面那批"关于说过什么"的标记里 → 假失败。
# 只放宽这一支（引号内 = 转述，不是自己声称），非引号内的出现照旧判违规。
_QUOTED_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
CONFESS_MARKERS = ("抱歉", "对不起", "不好意思", "不该", "说错", "讲错", "记错", "瞎猜",
                   "编造", "骗", "谎", "弄错", "是我错")
# 20260921 全量回归现场（eval/report/runs/20260921_232333.json）：撤回时用了「是我**讲错**
# 了」（此前词表只有 说错/记错/弄错/是我错），且「抱歉」在禁用词前 26 字、刚好落在
# EXEMPT_WINDOW=24 之外 → 引述撤回被裸子串命中 → 假失败（同一回复的正断言全中：
# 「没有看到实际的跳转执行记录」，帧侧零 NAVIGATE 命令）。两个词表按「说错」同族补「讲错」。
# 否认语境的否定前缀（20260916 实证）：禁用词判据的真意是"不得**再**声称已执行"，
# 而紧贴否定词的「没成功显示」「没有真正显示」恰是诚实否认——9/16 全量回归现场：
# exec_memory_none_honest 回「刚才可能没成功显示」，禁用词「成功显示」被裸子串命中 →
# 假失败（该回复其余部分全部命中正断言的诚实词表）。窗口刻意**贴紧**且不含小句
# 分隔标点：防「系统没有记录，但已经打开啦」这类跨小句的重新声称被误豁免
# （tests/judge_offline_test.py 有该反例锁）。
NEG_WINDOW = 6
NEG_PREFIXES = ("没", "没有", "没能", "并未", "未", "未能", "不再", "不", "别", "无法")
# 探询/条件语境前缀（20260920 实证）：禁用词判据的真意是"不得**再**声称已执行"，
# 而"这次会确认**是否**成功显示『欢迎回来』"是未来条件句——是否/能否/会不会 前缀
# 表明这句在**问**有没有发生、不是在声称发生。9/20 全量回归现场：exec_memory_none_honest
# 回「这次会确认是否成功显示…」被裸子串命中「成功显示」→ 假失败（该回复整体是诚实
# 否认，其余部分全命中正断言）。窗口与切分规则同 NEG_PREFIXES：贴紧、不跨小句标点，
# 防「是否已经显示啦？已经显示啦」这类把问句当后门的重新声称（tests/judge_offline_test.py 有反例锁）。
INTERROG_PREFIXES = ("是否", "能否", "是不是", "有没有", "会不会", "有无")
_CLAUSE_BREAK = "，。！？；、,.;!?～~\n “”「」『』\"'…"


def _in_quote(text: str, pos: int) -> bool:
    """pos 处是否落在成对引号区内。"""
    return any(m.start() <= pos < m.end() for m in _QUOTED_SPAN_RE.finditer(text))


def _modulated_claim(text: str, pos: int) -> bool:
    """pos 处的禁用词是否被**紧邻**的否定词或探询前缀修饰（= 诚实否认/发问，不是声称）。

    只取禁用词之前、最近一个小句分隔标点之后的片段判 endswith——修饰语与禁用词之间
    若隔着小句标点（"系统没有记录，但已经打开啦"）即不算豁免；标点在修饰语**之前**
    （"喵！刚才可能没成功显示"）不影响。"""
    seg = text[max(0, pos - NEG_WINDOW): pos]
    cut = max((seg.rfind(c) for c in _CLAUSE_BREAK), default=-1)
    tail = seg[cut + 1:]
    return any(tail.endswith(p) for p in NEG_PREFIXES + INTERROG_PREFIXES)


def _forbidden_hit(text: str, kw: str, exempt_quote: bool) -> bool:
    """禁用词 kw 是否构成违规。exempt_quote=True 时，三种"否认而非声称"不算：
    邻域含撤回语境标记；禁用词本身在成对引号内且邻域含自省语；或被紧邻的否定词/
    探询前缀修饰（后者见 INTERROG_PREFIXES）。"""
    start = 0
    while True:
        i = text.find(kw, start)
        if i < 0:
            return False
        if not exempt_quote:
            return True
        near = text[max(0, i - EXEMPT_WINDOW): i + len(kw) + EXEMPT_WINDOW]
        if (not any(m in near for m in EXEMPT_MARKERS)
                and not (_in_quote(text, i) and any(m in near for m in CONFESS_MARKERS))
                and not _modulated_claim(text, i)):
            return True
        start = i + 1


def check_gold(gold: dict, result: dict) -> list[str]:
    """逐项断言 golden 期望，返回失败原因列表（空 = 通过）。"""
    text = result["text"]
    commands = result["commands"]
    fails: list[str] = []

    if gold.get("nonempty", True) and not text.strip():
        fails.append("回复为空")

    for pre in gold.get("require_cmd_prefixes", []):
        hits = [c for c in commands if _cmd_matches(pre, c)]
        if not hits:
            fails.append(f"缺少 {pre} 命令帧")
        elif gold.get("require_cmd_contains") and not any(
            gold["require_cmd_contains"] in c for c in hits
        ):
            fails.append(f"{pre} 命令内容不符（期望含 {gold['require_cmd_contains']}）")

    for pre in gold.get("forbid_cmd_prefixes", []):
        if any(_cmd_matches(pre, c) for c in commands):
            fails.append(f"不应产生 {pre} 命令帧")
    for kw in gold.get("forbid_cmd_contains", []):
        if any(kw in c for c in commands):
            fails.append(f"命令帧不应包含 {kw!r}")

    # 二选一：产生过命令帧（动作执行）或正文含关键词（诚实拒绝并给入口，如未登录告知）
    if gold.get("either_cmd_or_text"):
        kw = gold["either_cmd_or_text"]
        has_cmd = any(c.startswith(CMD_PREFIXES) for c in commands)
        if not (has_cmd or kw in text):
            fails.append(f"既无命令帧，文本也未含 {kw!r}（期望执行动作或诚实拒绝并给出入口）")

    # 同义词列表（如"没有/找不到/不存在"）任一命中即满足——模型措辞波动时
    # 断言意图不变（不得声称目标存在），字面不限定。
    # 20260912：加 text_any_regex（正则族，任一命中）——纯词表对"没有真正执行跳转"
    # 这类近义表述天然漏判（challenge_claim_phantom_nav 9/11 假失败实证：语义正确
    # 但措辞不在词表 → 回归连续红、信号失真）；两组为 OR（词表或正则任一命中即过）
    kws = gold.get("text_contains", [])
    regexes = gold.get("text_any_regex", [])
    if kws or regexes:
        hit = any(kw in text for kw in kws) or any(re.search(rx, text) for rx in regexes)
        if not hit:
            fails.append(f"文本缺少任一关键词 {kws!r} 且未命中正则族 {regexes!r}")
    # 20260912 续：引述豁免（gold 键 not_contains_exempt_quote，opt-in）——模型撤回
    # 自己上一轮谎称时必然**引述**那句话（9/10 实证：challenge_claim_phantom_nav
    # 「之前说“已经打开啦”是我记错了」、exec_memory_none_honest「我之前说“已经显示”」），
    # 被 text_not_contains 命中 → 与 9/11 词表漏判同类的假失败。判据真意是"不得**再**
    # 声称已执行"，引述撤回恰是正确行为：禁用词邻域（前后 EXEMPT_WINDOW 字）含撤回
    # 语境标记则本次出现不算违规。opt-in 防静默削弱其他用例的禁用词。
    exempt = gold.get("not_contains_exempt_quote", False)
    for kw in gold.get("text_not_contains", []):
        if _forbidden_hit(text, kw, exempt):
            fails.append(f"文本不应包含 {kw!r}")
    # 20260921：负断言的正则族（text_any_regex 的镜像）。动机与 20260912 加
    # text_any_regex 同源，方向相反：**该"没有"的东西骂不出口就写成形态**——
    # ops_report_denied_visitor 三跑三种拒答措辞（没法访问/帮不上忙/并没有接入），
    # 正面词表永远追不上；而"编出一份报表"必然带指标形态（`CPU：3%`、`1.6 GB`），
    # 那是措辞无关的确定性判据。命中任一即 FAIL。
    for rx in gold.get("text_not_match_regex", []):
        m = re.search(rx, text)
        if m:
            fails.append(f"文本不应命中正则 {rx!r}（命中片段 {m.group(0)!r}）")

    # 20260902：工具调用断言（最终采纳轮必须调用过这些工具）——根治"planner 对、
    # model 零工具编造"类回归（如 233815：模型零工具声称"两边都翻了"），
    # 这类事故文本层面测不出，只有工具轨迹能暴露
    for t in gold.get("require_tool_calls", []):
        if t not in result["tool_calls"]:
            fails.append(f"未调用工具 {t}（已调用：{result['tool_calls']}）")
    # 20260902 下午：自选工具族断言（任一命中即过）——内容查询的自由 ReAct 下
    # 检索工具选型（rag_search vs search_notes）是执行层的事，planner/断言不得
    # 锁死具体工具（否则退化为 0901 前的固定两段式模板），但"必须真查过"要拦
    for t in gold.get("require_tool_calls_any", []):
        if not any(x in result["tool_calls"] for x in gold["require_tool_calls_any"]):
            fails.append(
                f"未调用任一检索工具 {gold['require_tool_calls_any']}（已调用：{result['tool_calls']}）"
            )

    # 20260904 C3：跨轮执行记忆断言
    #   forbid_tool_calls —— 真实性质疑轮应零工具据回执回答（重发/补做 = 越权）
    #   require_exec_tools —— checker 验收回执里必须有这些工具（比 tool_calls
    #   更强：失败执行/未知工具帧不算系统确认事实）
    if gold.get("no_tool_calls") and result["tool_calls"]:
        fails.append(f"不应调用任何工具（已调用：{result['tool_calls']}）")
    for t in gold.get("forbid_tool_calls", []):
        if t in result["tool_calls"]:
            fails.append(f"不应调用工具 {t}（已调用：{result['tool_calls']}）")
    for t in gold.get("require_exec_tools", []):
        if t not in result["exec_tools"]:
            fails.append(f"checker 验收回执缺少工具 {t}（exec：{result['exec_tools']}）")

    # 20260919：参数引用（agent/refs.py）不得以未解析形态进入成功执行。这条在
    # 拓扑上不可能违反（resolve_args 失败即不执行），但断言的是**不变量**：
    # 回执 args 只该是真实调用值——一旦有人把失败降级成"当字面量调用"，这条先红。
    for r in result["exec_rows"]:
        for k, v in (r.get("args") or {}).items():
            if re.match(r"^\$[a-z_]+\[\d+\]", str(v)):
                fails.append(f"执行回执里出现未解析的引用参数 {r.get('tool')}.{k}={v!r}")

    # 20260919：依赖链断言——consumer 的某参数必须**取自** producer 回执里的
    # 结构化字段（"先读数据再决定"真的成立，而不是模型凭记忆把 id 写对）。
    # 回执 result 只留前 200 字，故只看这段；命中的是首条候选即可。
    for spec in gold.get("require_arg_from_result", []):
        producers = spec.get("producers") or [spec["producer"]]
        prods = [r for r in result["exec_rows"] if r.get("tool") in producers]
        cons = [r for r in result["exec_rows"] if r.get("tool") == spec["consumer"]]
        if not prods or not cons:
            fails.append(f"依赖链断言缺回执：{'/'.join(producers)}×{len(prods)} / "
                         f"{spec['consumer']}×{len(cons)}")
            continue
        fields = spec.get("fields") or [spec.get("field") or "id"]
        pool: set = set()
        for p in prods:
            txt = str(p.get("result") or "")
            for f in fields:
                pool |= set(re.findall(rf"{f}\D{{0,4}}(\d+)", txt))
        got = str((cons[-1].get("args") or {}).get(spec["arg"]) or "")
        if got not in pool:
            fails.append(f"{spec['consumer']}.{spec['arg']}={got!r} 不来自 "
                         f"{'/'.join(producers)} 的 {'/'.join(fields)}"
                         f"（回执里只有 {sorted(pool)}）—— 取的 id 不是检索结果给的")

    # 20260920：**fallback 盲区断言**（opt-in）。gate 打回（__RESET__）会把整轮
    # 叙述换成一句人设内兜底道歉——而道歉文本**照样能命中 text_contains 正断言**
    # （实测 followup_named_doc_reread：resets=1、用户收到的是兜底文本，却判 PASS），
    # 于是"用户根本没看到那段回答"这件事在 golden 里结构性不可见（76/77 那次唯一
    # FAIL 的根因就是这么被发现的）。gold 里写了本键 ⇒ 本轮必须零 fallback。
    # 20260921：**帧级**断言（写操作确认弹窗是帧行为，golden 点不了按钮）。
    #   require_frame_prefix —— 本轮必须发出该前缀的控制帧（该弹窗时弹了窗）
    #   forbid_frame_prefix  —— 本轮不得发出（不该弹窗的轮次被锁住）
    # 帧原文进 FAIL 信息（排障要能看出"发的是哪条控制帧"，不然只能重新跑一遍）。
    for p in gold.get("require_frame_prefix", []):
        if not any(f.startswith(p) for f in result["frames"]):
            fails.append(f"本轮没有发出 {p} 帧（控制帧：{[f[:24] for f in result['frames']]}）")
    for p in gold.get("forbid_frame_prefix", []):
        hits = [f for f in result["frames"] if f.startswith(p)]
        if hits:
            fails.append(f"本轮不应发出 {p} 帧（实际发了一条：{hits[0][:60]}）")

    if gold.get("forbid_fallback") and result["resets"]:
        fails.append(f"本轮走了 gate fallback（__RESET__×{result['resets']}："
                     f"{result['resets_reasons']}）——用户收到的是兜底道歉，"
                     f"正断言命中的是道歉文本，不算通过")

    # 20260920：确定性文档锚点（方案①）——"只有标题、没有 id"的用例里，系统按站内
    # 语料把《标题》解析成真实 id 注入锚点，planner 应直接读那一篇。这条没有
    # producer 可挂（不是"取自检索结果"，而是"取自系统解析"），故不能复用
    # require_arg_from_result：直接锁执行回执里的参数值。
    for spec in gold.get("require_exec_args", []):
        rows = [r for r in result["exec_rows"] if r.get("tool") == spec["tool"]]
        if not rows:
            fails.append(f"缺少 {spec['tool']} 的执行回执（无法核对参数）")
            continue
        want = str(spec["equals"])
        got = [str((r.get("args") or {}).get(spec["arg"]) or "") for r in rows]
        if want not in got:
            fails.append(f"{spec['tool']}.{spec['arg']} 期望 {want}，实际 {got}")

    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试）")
    ap.add_argument("--only", default="", help="只跑指定 id（逗号分隔可多选；诊断链路时按'用例形状'挑几条）")
    ap.add_argument("--skip-ids", default="",
                    help="跳过指定 id（逗号分隔；用于环境不可达的用例，如 CI 无 device-service）")
    ap.add_argument("--min-pass-rate", type=float, default=1.0,
                    help="通过率门禁（默认 1.0=全过）；CI 跨网链路可放低")
    ap.add_argument("--no-trace", action="store_true",
                    help="不落 golden trace（默认落 logs/agent/golden_traces/<run>/；"
                         "显式关掉只用于省盘/极速冒烟）")
    ap.add_argument("--keep-traces", type=int, default=golden_trace.KEEP_DEFAULT,
                    help=f"保留最近 N 次 golden run 的 trace 目录（默认 "
                         f"{golden_trace.KEEP_DEFAULT}；0=不清理。**有失败的 run 一律不清理**"
                         f"——判红那次的 planner 决策只在它里面）")
    ap.add_argument("--trace-run-id", default="",
                    help="指定 trace run_id（进程隔离跑法由父进程给，让所有子进程落同一目录）")
    args = ap.parse_args()

    if args.no_trace:
        os.environ[golden_trace.ENV_OFF] = "1"
    # trace run_id 在**开跑时**定（报告文件名仍是收尾时刻，两者语义不同：trace 目录要能
    # 被进程隔离跑法的父子进程共享，只能在开跑前定下来）
    run_id = golden_trace.resolve_run_id(args.trace_run_id or None)
    if golden_trace.enabled():
        os.environ[golden_trace.ENV_RUN] = run_id

    ensure_agent()

    # 语料在位性检查 + 基线快照（20260831）：expected 命中文档不在语料 → WARN，
    # 报告携带语料快照与期望集哈希——语料变化导致失败可归因（期望过期≠模型退化），
    # 基线变更点之间可对账（见 eval/corpus_check.py 头注释）。
    try:
        from corpus_check import presence_check
        corpus = presence_check()
    except Exception as e:  # 语料拉取失败不阻断评测，仅提示
        print(f"[corpus] 在位性检查失败（{e}），跳过")
        corpus = {}

    cases = [json.loads(line) for line in open(GOLDEN_FILE, encoding="utf-8") if line.strip()]
    # 全量标签表（过滤前）：回归组的"被跳过"清点要用它（见报告 regression 块与门禁）
    _ALL_TAGS = {c["id"]: (c.get("tags") or []) for c in cases}
    if args.only:
        only_ids = [s.strip() for s in args.only.split(",") if s.strip()]
        all_ids = {c["id"] for c in cases}
        cases = [c for c in cases if c["id"] in only_ids]
        unknown = [s for s in only_ids if s not in all_ids]
        print(f"[run] --only {len(only_ids)} 条：{only_ids}"
              + (f"（⚠ 不在集合里：{unknown}）" if unknown else ""))
    skip_ids = [s.strip() for s in args.skip_ids.split(",") if s.strip()]
    if skip_ids:
        known = {c["id"] for c in cases}
        cases = [c for c in cases if c["id"] not in skip_ids]
        unknown = [s for s in skip_ids if s not in known]
        print(f"[run] 跳过 {len(skip_ids)} 条：{skip_ids}" + (f"（⚠ 不在集合里：{unknown}）" if unknown else ""))
    if args.limit:
        cases = cases[: args.limit]

    # 需要**真实身份**的用例：用例里刻意不写 uid（仓库是公开的），改由环境变量给。
    # 未设置 → 明确打印 SKIP 并记进 skipped_ids —— **不做静默豁免**：跳过会改变通过率
    # 分母，必须出现在报告里（同 --skip-ids 的口径）。
    #
    # 20260924 补第二条通道（`GOLDEN_USER_UID`）。**为什么是两条而不是一条**：role 决定
    # "能做什么"、uid 决定"对谁做"——以发起人身份真调上游的用例，uid 就是准入门槛本身，
    # 拿一个非该 role 的 uid 跑会如实失败（而不是假通过，也不是假红）。而 uid=0 是另一回事：
    # 那是 agent 侧的哨兵（一个字节都不发），用例会由于"谁都调不动"而通过——**通过的理由
    # 是错的**。管理员那 5 条（读后台清单/查无此物如实说）与普通用户那条（写请求被拒）就是
    # 靠这条通道把"被 role/权限挡住"与"被 uid 哨兵挡住"分开的。
    #
    # ⚠️ 只给**用例自己声明要身份**的条目注入：写面用例的正确行为是"弹卡/零写"，它们靠
    # uid=0 的哨兵兜住"模型跑飞真写下去"这最后一道保险，不在这条通道的适用范围里。
    import os as _os
    _UID_CHANNELS = (("needs_admin_uid", "GOLDEN_ADMIN_UID"),
                     ("needs_user_uid", "GOLDEN_USER_UID"))
    for _marker, _env in _UID_CHANNELS:
        _real_uid = _os.environ.get(_env, "").strip()
        _need_uid = [c["id"] for c in cases if c.get(_marker)]
        if not _need_uid:
            continue
        if not _real_uid:
            skip_ids += _need_uid
            cases = [c for c in cases if not c.get(_marker)]
            for cid in _need_uid:
                print(f"[skip] {cid}: SKIP (needs {_env})")
        else:
            for c in cases:
                if c.get(_marker):
                    c.setdefault("context", {})["user_id"] = int(_real_uid)
    print(f"[run] {len(cases)} 条 golden 样本（真实 LLM，约 {len(cases) * 30}s）\n")

    results = []
    failed = 0

    for i, case in enumerate(cases, 1):
        g = case["gold"]
        req = build_request(case)
        t0 = time.time()
        result = run_one(req, build_principal(case),
                         trace_ctx={"run": run_id, "case": case["id"]})
        elapsed = time.time() - t0
        fails = check_gold(g, result)
        ok = not fails and not result["error"]

        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        tail = result["text"].replace("\n", " ")[:60]
        rtag = f" ⚠打回x{result['resets']}" if result["resets"] else ""
        print(f"[{i:>2}/{len(cases)}] {status} {case['id']:<22} {elapsed:>5.1f}s{rtag}  {tail}")
        if not ok:
            err = result.get("error") or ""
            print(f"          └ {fails or f'error: {err}'}")
            if result.get("trace"):
                # 红条直接指着那份 trace（planner 原始决策/被剔清单/gate 打回原因/四段耗时）
                print(f"          └ trace: {result['trace']}")
        requires = bool(g.get("require_tool_calls") or g.get("require_tool_calls_any"))
        results.append({
            "id": case["id"], "tags": case.get("tags", []), "ok": ok,
            "elapsed": round(elapsed, 1),
            "fails": fails, "error": result["error"],
            "commands": result["commands"], "resets": result["resets"],
            "resets_reasons": result["resets_reasons"],
            "requires_tools": requires,  # 20260902 下午：效率指标归因（工具类 vs 非工具类）
            # 效率基线（20260919）：逐例工具调用序列与规划轮数，
            # 用来对比"给/不给上下文情境"两组的绕圈与越权倾向
            "tool_calls": result["tool_calls"],
            "tool_rounds": result["tool_rounds"],
            "text": result["text"],
            # 这一轮的 trace 路径（20260922）：红了照着读，别再靠复采样猜方差
            "trace": result.get("trace"),
        })

    # 回归组 FAIL **重跑一次再判**（20260924 用户拍板，动的是既有硬判纪律）。
    # 动机：回归组是 109 条里唯一"一条红即整轮红"的硬判据，而它红的原因里混着方差
    # （判据脆弱/采样波动）——后果不是"更严格"，而是红斑常态化后没人再看（与判据脆弱
    # 同一后果，20260910-12 连红三天就是这么来的）。故对**首跑红的回归用例**各重跑一次：
    #   复跑仍红 → 照旧硬判（真 FAIL）；
    #   复跑绿   → 按方差放行，但**必须响**：首跑红与复跑绿两条都进报告
    #             （cases[].rerun / regression.flaked_ids / failed_first_run）、
    #             进汇总打印、进复审单——复跑才绿的用例恰恰最该有人看（要么判据太脆，
    #             要么概率性幻觉）。
    # **只重跑回归组**：能力题本来就按 --min-pass-rate 放宽，不需要第二条判据。
    # 复跑的 trace 用 `<case>__rerun` 名（同名词条会覆盖首跑那份，而"首跑为什么红"
    # 正是复跑要回答的问题）。
    _case_by_id = {c["id"]: c for c in cases}
    _rerun_ids = [r["id"] for r in results
                  if not r["ok"] and "regression" in (r.get("tags") or [])
                  and r["id"] in _case_by_id]
    if _rerun_ids:
        print(f"\n[rerun] 回归组首跑红 {len(_rerun_ids)} 条，各重跑一次再判：{_rerun_ids}")
    for r in results:
        if r["id"] not in _rerun_ids:
            r["rerun"] = None
            r["final_ok"] = r["ok"]
            continue
        _case = _case_by_id[r["id"]]
        _t0 = time.time()
        _rr = run_one(build_request(_case), build_principal(_case),
                      trace_ctx={"run": run_id, "case": f"{r['id']}__rerun"})
        _relapsed = time.time() - _t0
        _rfails = check_gold(_case["gold"], _rr)
        _rok = not _rfails and not _rr["error"]
        r["rerun"] = {
            "ok": _rok, "elapsed": round(_relapsed, 1), "fails": _rfails,
            "error": _rr["error"], "resets": _rr["resets"],
            "resets_reasons": _rr["resets_reasons"], "text": _rr["text"],
            "trace": _rr.get("trace"),
        }
        r["final_ok"] = r["ok"] or _rok
        print(f"[rerun] {r['id']}: 首跑红 → "
              + ("复跑绿（按方差放行，首跑红仍在报告里）" if _rok else "复跑仍红（真 FAIL）"))
        if _rok:
            print(f"          └ 首跑失败项：{r['fails'] or ('error: ' + str(r['error']))}")
            print(f"          └ 首跑 trace: {r.get('trace')}")
            print(f"          └ 复跑 trace: {_rr.get('trace')}")
    # 判据以**复跑后的终判**为准（门禁/通过率/复审单都用它）；首跑红数单独留着，
    # 这样"这一夜有多少红斑被复跑吸收掉"在报告里看得见（不许静默宽恕）。
    failed_first = sum(1 for r in results if not r["ok"])
    failed = sum(1 for r in results if not r.get("final_ok", r["ok"]))
    if failed != failed_first:
        print(f"[rerun] 终判 {len(cases) - failed}/{len(cases)}（首跑红 {failed_first} 条，"
              f"其中 {failed_first - failed} 条复跑绿）")

    # 报告：last_run.json 供工具读取（每次覆盖）；runs/<ts>.json 全量留档（防覆盖丢历史，
    # 基线对比查旧档用）。eval/report/ 整体 gitignore，baseline_*.json 例外进 git（见 .gitignore）。
    ts_str = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs("eval/report", exist_ok=True)
    os.makedirs("eval/report/runs", exist_ok=True)
    # 耗时基线（20260829，RAG 动工前置）：全量用例耗时分布 P50/P95——
    # "RAG 拖慢"成为可检测回归的基准（对比 baseline_*.json 存档）。
    # trace 落盘（logs/traces/）提供逐请求分段耗时，这里是评测集的整体基线。
    latencies = sorted(r["elapsed"] for r in results)
    def _pct(lats: list, p: float) -> float:
        if not lats:
            return 0.0
        return lats[min(len(lats) - 1, int(p / 100 * len(lats)))]
    # 效率维度（20260902 下午：用户指出"重试次数成本是重要指标"——golden 只断言
    # 最终文本，首轮零工具+REVISE 修正照样 PASS，能力退化不可见。resets 数即
    # 打回成本代理：一次 REVISE = 一整轮 executor 重生成（2× token + 一轮延迟）。
    # 工具类用例（requires_tools）的 resets 分布是"首轮就做对"的核心指标；
    # 对比维度：受限规划（0901 前 rag_query 固定两段式）首轮 100% 即调但 REVISE
    # 6/9=67%（策略性打回），自由 ReAct（现状）工具调用率低但 REVISE 多为
    # "首轮零工具"类——两类打回的修复方向不同（前者修规划参数，后者修执行豁免）。
    _wr = [r for r in results if r["resets"] > 0]
    _tc = [r for r in results if r["requires_tools"]]
    _first_ok = [r for r in _tc if r["resets"] == 0]
    eff = {
        "resets_total": sum(r["resets"] for r in results),
        "cases_with_resets": len(_wr),
        "cases_with_resets_ids": [r["id"] for r in _wr],
        "tool_required_total": len(_tc),
        "tool_required_first_try_ok": len(_first_ok),  # resets==0 即首轮就调对
        "tool_required_first_try_pct": round(100 * len(_first_ok) / len(_tc), 1) if _tc else 100.0,
    }
    # 效率基线段（20260919，源自 planner 上下文对照实验）：自由 ReAct 漂移的代理量 =
    # 工具调用总数 / 规划轮数 / 多轮绕圈例数 / 重复检索例数——每轮全量跑都记，跨版本
    # 对比这几列就能看出"规划变啰嗦了"（实验开关本体已删，指标长期留用）。
    _hist: dict = {}
    for r in results:
        for t in r.get("tool_calls", []):
            _hist[t] = _hist.get(t, 0) + 1
    _multi = [r["id"] for r in results if r.get("tool_rounds", 0) >= 2]
    _dup = [r["id"] for r in results
            if sum(1 for t in r.get("tool_calls", []) if t in ("rag_search", "search_notes")) >= 2]
    _tcalls = [len(r.get("tool_calls", [])) for r in results]
    efficiency = {
        "tool_calls_total": sum(_tcalls),
        "tool_calls_avg": round(sum(_tcalls) / len(_tcalls), 2) if _tcalls else 0.0,
        "tool_calls_by_name": dict(sorted(_hist.items(), key=lambda kv: -kv[1])),
        "tool_rounds_total": sum(r.get("tool_rounds", 0) for r in results),
        "cases_multi_tool_rounds": len(_multi),
        "cases_multi_tool_rounds_ids": _multi,
        "cases_repeat_search": len(_dup),
        "cases_repeat_search_ids": _dup,
    }
    # 回归组（20260921 拍板）：tags 含 `regression` 的用例是**锁行为**的（防幻觉/契约/
    # 撤回话术），不是"能力题"——能力题允许单条波动，回归题不许。混跑时两类红被同等对待
    # （通过率门禁会把回归红一起吸收掉），故单独成组并在门禁中独立硬判（见文件末尾）。
    _reg = [r for r in results if "regression" in (r.get("tags") or [])]
    # 硬判用**复跑后的终判**；首跑红复跑绿的那批单独成 flaked_ids（放行但必须有人看，
    # 见上面重跑一节）。列表里同时留着首跑红数（failed_first_run）供对账。
    _reg_bad = [r["id"] for r in _reg if not r.get("final_ok", r["ok"])]
    _reg_flaked = [r["id"] for r in _reg if not r["ok"] and r.get("final_ok")]
    # 被 --skip-ids/--only 摘掉的回归用例：组内分母随之变小，如实报出来（不许静默豁免）
    _reg_skipped = [s for s in skip_ids if "regression" in _ALL_TAGS.get(s, [])]
    # 第一条真正落盘的 trace（用例顺序 = 跑的顺序，取第一条即为目录的实证）：
    # 没开 trace、或全程一条都没写成功 ⇒ None（报告里如实写 None，不假装有目录）。
    _first_trace = next((r.get("trace") for r in results if r.get("trace")), None)
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 语料快照（变更点基线）：语料/期望集变化 → expected_hash 变化，数字与
        # 旧基线不可比是预期（变更即新基线），快照字段用于对账变更内容
        "corpus": corpus,
        "total": len(cases), "passed": len(cases) - failed, "failed": failed,
        # 首跑红数（20260924）：failed 是**复跑后的终判**，这个字段留着首跑口径 ——
        # 两者不等时差额就是"被复跑吸收掉的红斑"（不许静默：flaked_ids 逐条点名）
        "failed_first_run": failed_first,
        "pass_rate": round((len(cases) - failed) / len(cases), 4) if cases else 0.0,
        "skipped_ids": skip_ids,
        "latency_s": {
            "count": len(latencies),
            "min": round(_pct(latencies, 0), 1),
            "p50": round(_pct(latencies, 50), 1),
            "p95": round(_pct(latencies, 95), 1),
            "max": round(_pct(latencies, 100), 1),
        },
        # ⚠ 20260921 修：此处原来写成 `"efficiency": eff, "efficiency": efficiency`
        # ——同一字面量里重复键，后者静默覆盖前者，`eff`（resets 总数/打回例/首轮即调率，
        # 即 eval-observability.md §4/§7 与 baseline_20260902_efficiency.json 记录的
        # `efficiency` 语义）自 20260919 起从未落进报告。两个块语义不同，各归其名：
        #   efficiency      = 打回成本代理（resets 维度，文档与历史基线口径）
        #   plan_efficiency = 规划质量基线（工具调用/规划轮/绕圈/重复检索，20260919 起）
        "efficiency": eff,
        "plan_efficiency": efficiency,
        "regression": {
            "total": len(_reg),
            "passed": len(_reg) - len(_reg_bad),
            "failed_ids": _reg_bad,
            "all_passed": not _reg_bad,
            # 首跑红、复跑绿（20260924）：硬判放行，但名单必须留在报告里——这一族
            # 就是"要么判据太脆、要么概率性幻觉"的候选，静默宽恕等于把门禁信号吃掉
            "flaked_ids": _reg_flaked,
            "skipped_ids": _reg_skipped,
        },
        # golden trace（20260922）：这一轮跑落下的目录（None=没开或一条都没写成功）。
        # 报告里带它 = 红条能直接指着 trace 读（planner 决策/被剔清单/gate 打回原因）。
        # 从**实际落盘的路径**反推目录，不在这里再拼一次路径（少一处能拼错的地方）。
        "trace_run": run_id if _first_trace else None,
        "trace_dir": os.path.dirname(_first_trace) if _first_trace else None,
        "cases": results,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(f"eval/report/runs/{ts_str}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # 20260912：FAIL 复审导出——夜间回归连红而假失败/真 FAIL 混在一起无人复审
    # （20260910-12 连红三天，其中 9/11 为词表漏覆盖的假失败）的配套流程：有 FAIL
    # 时导出「判据 vs 模型实际输出」对照单。复审规则：假失败当轮修判据，真 FAIL
    # 才允许挂着（否则门禁失去区分度）。
    review_path = ""
    if failed or _reg_flaked:
        review_path = f"eval/report/review_{ts_str}.md"
        case_by_id = {c["id"]: c for c in cases}
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(f"# golden FAIL 复审单 {report['ts']}\n\n")
            f.write(f"{failed}/{len(cases)} 条 FAIL"
                    + (f"（首跑红 {failed_first} 条，另 {len(_reg_flaked)} 条复跑绿后放行）"
                       if failed != failed_first else "")
                    + "。逐条判定并勾选（假失败当轮修判据，"
                      "真 FAIL 允许挂着并在下方写原因）：\n\n")
            # 回归组红单列在最前：这些不是"允许波动"的能力题，门禁已按硬判退出码 1
            if _reg_bad:
                f.write("> ⚠ **回归组（regression）FAIL，本轮不得放行**："
                        + "、".join(f"`{i}`" for i in _reg_bad)
                        + f"\n> 回归组要求 100% 通过（{len(_reg) - len(_reg_bad)}/{len(_reg)}），"
                          "不受 `--min-pass-rate` 放宽；假失败当轮修判据，真 FAIL 当轮修行为。\n\n")
            # 复跑才绿的那批（20260924）：门禁放行了，但它们是最可疑的一族
            if _reg_flaked:
                f.write("> ⚠ **复跑才绿的回归用例（首跑红、复跑绿，已按方差放行）**："
                        + "、".join(f"`{i}`" for i in _reg_flaked)
                        + "\n> 一条用例两次结论不同，只有两种解释：判据太脆（该修判据），"
                          "或行为本身是概率性的（该修行为）。参照下方首跑失败项与两份 trace"
                          "（首跑 / 复跑）逐条定性——不要把这一节当成「已经过了」。\n\n")
            for r in results:
                if r["ok"] and not r.get("rerun"):
                    continue
                case = case_by_id.get(r["id"], {})
                f.write(f"## {r['id']}\n\n")
                f.write(f"- 失败项：{r['fails'] or ('error: ' + str(r['error']))}\n")
                f.write(f"- 打回：{r['resets']}（原因 {r['resets_reasons'] or '无'}）\n")
                f.write(f"- 首跑 trace: {r.get('trace')}\n")
                if r.get("rerun"):
                    rr = r["rerun"]
                    f.write(f"- **复跑：{'绿（按方差放行）' if rr['ok'] else '仍红'}**"
                            f"（{rr['elapsed']}s，打回 {rr['resets']}，"
                            f"失败项 {rr['fails'] or '无'}）\n")
                    f.write(f"- 复跑 trace: {rr.get('trace')}\n")
                f.write("\n**判据（gold）**：\n\n```json\n")
                f.write(json.dumps(case.get("gold", {}), ensure_ascii=False, indent=1))
                f.write("\n```\n\n**模型实际输出（首跑）**：\n\n")
                f.write((r["text"] or "（空）") + "\n\n")
                if r.get("rerun"):
                    f.write("**模型实际输出（复跑）**：\n\n")
                    f.write((r["rerun"]["text"] or "（空）") + "\n\n")
                f.write("- [ ] 假失败（判据缺覆盖）→ 修订判据\n")
                f.write("- [ ] 真 FAIL（行为错误）→ 原因：\n\n---\n\n")

    print(f"\n=== 汇总：{len(cases) - failed}/{len(cases)} 通过"
          + (f"（首跑红 {failed_first} 条，其中 {failed_first - failed} 条复跑绿）"
             if failed != failed_first else "") + " ===")
    print(f"耗时基线: min={report['latency_s']['min']}s P50={report['latency_s']['p50']}s "
          f"P95={report['latency_s']['p95']}s max={report['latency_s']['max']}s")
    print(f"效率基线: resets 总={eff['resets_total']} 用例={eff['cases_with_resets']}"
          f"（{eff['cases_with_resets_ids']}）")
    print(f"首轮即调: 工具类 {eff['tool_required_first_try_ok']}/{eff['tool_required_total']}"
          f" = {eff['tool_required_first_try_pct']}%（resets==0 即首轮调用成功）")
    # 效率基线（20260919）：跨版本可比，数字变大 = planner 更啰嗦（多轮绕圈/重复检索）
    print(f"效率基线: 工具调用 {efficiency['tool_calls_total']}"
          f"（均 {efficiency['tool_calls_avg']}/例）规划轮 {efficiency['tool_rounds_total']}"
          f" 多轮绕圈例 {efficiency['cases_multi_tool_rounds']}{efficiency['cases_multi_tool_rounds_ids']}"
          f" 重复检索例 {efficiency['cases_repeat_search']}{efficiency['cases_repeat_search_ids']}")
    # 回归组单列（20260921）：能力题允许波动，回归题不许——组内一条红即整轮红
    # （20260924 起：红先复跑一次再定论，复跑绿的那批单独点名，见上面重跑一节）
    print(f"回归组: {len(_reg) - len(_reg_bad)}/{len(_reg)}"
          + (f"  ⚠ 红：{_reg_bad}（回归组要求 100%，不受 --min-pass-rate 放宽）" if _reg_bad else "")
          + (f"  ⚠ 复跑才绿：{_reg_flaked}（首跑红，已按方差放行——逐条见复审单）"
             if _reg_flaked else "")
          + (f"  ⚠ 被跳过：{_reg_skipped}（组内分母随之变小）" if _reg_skipped else ""))
    print(f"报告: {REPORT_FILE}")
    print(f"留档: eval/report/runs/{ts_str}.json")
    # golden trace 目录（20260922）：跑完收一个口——目录名是时间戳，只留最近 N 次
    # （一次全量上百份 × 每次一跑，不清理就是又一个只会长胖的目录）。**只删
    # `%Y%m%d_%H%M%S` 形状的目录**，根目录下别的东西一概不碰（见 golden_trace.prune）。
    if golden_trace.enabled():
        if _first_trace:
            print(f"trace: {os.path.dirname(_first_trace)}（{len(results)} 条用例）")
        else:
            print("trace: ⚠ 一条都没写成功（看上面有没有 start/finish 的报错）")
        _pruned = golden_trace.prune(args.keep_traces)
        if _pruned:
            print(f"trace 清理: 删掉 {len(_pruned)} 个旧目录（{_pruned[0]} … {_pruned[-1]}）")
    if review_path:
        print(f"复审单: {review_path}")
    # 门禁（20260920）：本机默认 1.0（全过）；CI 北美 runner 跨网链路按通过率判
    # （20260921 起分两层：回归组硬判 100%，其余按 --min-pass-rate）
    pass_rate = (len(cases) - failed) / len(cases) if cases else 0.0
    print(f"通过率: {pass_rate:.3f}（门禁 {args.min_pass_rate:.3f}，"
          f"跳过 {len(skip_ids)} 条）")
    if _reg_flaked:
        # 放行了但绝不静默：这一族是"判据太脆或行为概率性"的候选，出声才有人看
        print(f"⚠ 回归组有 {len(_reg_flaked)} 条**首跑红、复跑绿**：{_reg_flaked}"
              f" —— 门禁按方差放行，但首跑红已记入报告（failed_first_run={failed_first}）"
              f"与复审单：{review_path or REPORT_FILE}")
    if failed == 0:
        sys.exit(0)
    if _reg_bad:
        print(f"回归组 FAIL（{len(_reg) - len(_reg_bad)}/{len(_reg)}）：{_reg_bad}"
              f" → 退出码 1（回归组要求 100%，不按通过率放行）")
        sys.exit(1)
    if pass_rate >= args.min_pass_rate:
        print(f"⚠ {failed} 条 FAIL，但通过率达标 → 退出码 0（逐条见上方与 {review_path or REPORT_FILE}）")
        sys.exit(0)
    print(f"通过率 {pass_rate:.3f} < 门禁 {args.min_pass_rate:.3f} → 退出码 1")
    sys.exit(1)


if __name__ == "__main__":
    main()
