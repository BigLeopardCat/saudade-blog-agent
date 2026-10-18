# -*- coding: utf-8 -*-
"""原生 checkpoint 实验的读数仪器（`exp/native-checkpoint` 分支专用）。

三个子命令，各自回答一个问题：

    --deterministic   线程一复用，`state["messages"]` 累积之后，**读端**会读出
                      什么？（零 LLM、零采样、可重复 ⇒ 这是最硬的一格证据）
    --crash-resume    崩在"一个节点中途"时，恢复会从哪儿重放？——**整节点重跑**，
                      所以写操作**会执行两次**（安全相关，见那里的注）
      --guard         加这一档：真子进程崩在工具调用中途，**幂等账**把写压成一次
                      （2× → 1×；不带 --guard 的那条就是它的内置反控）
      --guard-unsafe  上一条的反控：占位留在一笔**没提交**的事务里 ⇒ 读数应回到两次
      --prod-shape    配 --guard：恢复那一次传 **dict 输入**（= 生产每轮的形状）
                      ⇒ 20261019 批 F 起读数 **1×**（改前是 2×：账恒不命中）。
                      两格反控 = 同夹具不带本开关（1×）+ 干净轮之后重复同一件写（2×）
    --pg-stats        实验库里有什么（逐 thread 的 checkpoint 行数/字节、单行大小）
    --backend-parity  两档账本（PG / SQLite）跑同一份序列 ⇒ 读数必须**逐字相等**
    --summarize       把 `CHECKPOINT_PROBE_OUT` 那份 JSONL 汇总成逐轮曲线

**跑法**（cwd 必须是主仓 —— `.env` 是相对路径；解释器用主仓 `.venv`）：

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent
source /home/ubuntu/exp/ckpt.env
PYTHONPATH=/home/ubuntu/exp/saudade-agent-checkpoint:/home/ubuntu/exp/pydeps \\
  .venv/bin/python /home/ubuntu/exp/saudade-agent-checkpoint/eval/experiments/checkpoint_probe.py --deterministic
```

`--deterministic` 为什么能不用 LLM 就说明问题：要证的命题是**"恢复真的发生了、
而读端把上一轮的东西当本轮"**——这两件事都住在 reducer 与纯函数里，与模型无关。
它用**真的 saver**（`agent.checkpoint_arm.make_saver`）跑一张**真的带 `AgentState`
的小图**，三轮同一 thread，然后拿 langgraph 还回来的 `messages` 去喂**真的读端**
（`prompt_context._page_ctx` / `prompt_context._has_frames` / `prompt_context._frame_texts` /
`graph.frame_chars`）。一个字节的判据都没有抄第二份。
"""
import argparse
import json
import os
import sys
import time

# 让"从主仓 cwd 直接跑本文件"也能 import 到工作树的 agent 包（`run_golden.py`
# 同款：sys.path[0] 是脚本所在目录，PYTHONPATH 在工作树那一侧）。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

from agent import prompt_context  # noqa: E402
from agent import checkpoint  # noqa: E402
from agent import graph as G  # noqa: E402
from agent import idem  # noqa: E402
from agent.checkpoint_arm import make_saver  # noqa: E402
from tools.base import ToolResult  # noqa: E402

THREAD = "probe-conv-20260925"
# 三轮各自的页面上下文（`_build_messages` 写进首条 [System: …] 的那一串）。
# 第二轮起**换一个页面**：这是 `_page_ctx` 取"第一条"会踩到的那一格。
PAGES = [
    "/article/62 page=文章详情 title=原生checkpoint effects=sakura darkmode=on",
    "/about page=关于 effects=rain darkmode=off",
    "/guestbook page=留言板 effects=none darkmode=off",
]
# 每轮 execute 产出的工具帧（真 ToolMessage：`_has_frames` / `_frame_texts` 认它）。
FRAME_BODIES = [
    "【文章详情】《原生 checkpoint 与持久化后端》正文约 6800 字 ……" + "文" * 1200,
    "【留言板】共 12 条：1. 诉「这个页面好看」 2. 无尘「沙发」 ……" + "言" * 1200,
    "【公告】3 条：20261001 维护通知 ……" + "告" * 1200,
]


def _user_turn(page: str, text: str) -> HumanMessage:
    """`server._build_messages` 造的那一条：页面上下文 + 用户原话。"""
    return HumanMessage(content=f"[System: current_url=https://saudade.site{page}]\n{text}")


def _mini_graph(saver):
    """带真 `AgentState` 的最小图：一个节点，把"这一轮的产出"写回 state。

    用 `AgentState` 而不是自己编一个 TypedDict，是为了让 reducer（`add_messages`）
    与线上**同源**——本实验要量的正是那个 reducer 的行为。
    """

    def turn(state):
        i = state.get("plan_rounds", 0)
        return {"messages": [AIMessage(content=f"第 {i} 轮回复"),
                             ToolMessage(content=FRAME_BODIES[(i - 1) % 3],
                                         tool_call_id=f"c{i}", name="get_article_detail")],
                "plan_rounds": i}

    g = StateGraph(G.AgentState)
    g.add_node("turn", turn)
    g.add_edge(START, "turn")
    g.add_edge("turn", END)
    return g.compile(checkpointer=saver)


def cmd_deterministic(keep_thread: bool = False) -> int:
    saver = make_saver()
    mini = _mini_graph(saver)
    # 同一个 thread 的旧 checkpoint **会**被读回来（这正是本实验的命题）——
    # 所以不复位的话，第二遍跑出来的"第 1 轮"带着上一遍的 9 条消息。
    # 那既是个真事实（**跨进程**也在累积：checkpoint 活在库里，进程重启不作废），
    # 也会让读数不可重复。缺省复位；`--keep-thread` 留着演示那个跨进程事实。
    if not keep_thread:
        saver.delete_thread(THREAD)
    cfg = {"configurable": {"thread_id": THREAD}}

    print(f"saver      : postgres（真 saver，见 agent/checkpoint_arm.make_saver）")
    print(f"thread_id  : {THREAD}"
          f"{'（保留旧 checkpoint：演示跨进程累积）' if keep_thread else '（已复位）'}")
    print(f"图         : StateGraph(AgentState) 单节点，reducer 与线上同源")
    print()

    def _view(msgs: list) -> dict:
        """读端在**这一坨消息**上会读出什么（四个读端全是真实现）。"""
        return {
            "msgs": len(msgs),
            "chars": sum(len(prompt_context._msg_text(m) or "") for m in msgs),
            # state 里累计的 ToolMessage 条数（`_frame_texts` 只取最近 limit=5 条，
            # 所以超过 5 之后条数不再涨、但字符仍涨——两列一起看才读得对）。
            "tool_msgs": sum(1 for m in msgs if isinstance(m, ToolMessage)),
            "has_frames": prompt_context._has_frames(msgs),
            # `_frame_texts` 返回的是**渲染好的字符串**（不是列表）⇒ 量它的长度。
            "frame_render": len(prompt_context._frame_texts(msgs)),
            "frame_chars": G._frame_chars(msgs),
            "page_ctx_read": prompt_context._page_ctx(msgs),
        }

    rows = []
    for i, page in enumerate(PAGES, start=1):
        # **planner 看见的那一坨**：本轮输入合并进 checkpoint 之后、execute 动手之前。
        # 有 checkpointer 时 `mini.get_state()` 拿到的就是恢复出来的 state，而
        # planner 节点的 `state["messages"]` 与它逐条相同（`graph_input` 走
        # add_messages ⇒ 追加，不覆盖）。
        pre = _view((mini.get_state(cfg).values or {}).get("messages") or [])
        inp = G.graph_input([_user_turn(page, f"第 {i} 轮的问题")])
        inp["plan_rounds"] = i
        list(mini.stream(inp, cfg, stream_mode=["updates"]))
        # **narrator 看见的那一坨**：execute 追加完帧之后的 state。
        post = _view(mini.get_state(cfg).values["messages"])
        rows.append({"round": i, "page_this_round": page.split(" ")[0],
                     "pre": pre, "post": post})

    print(f"{'轮':>2}  {'本轮页面':<12} | {'planner 看见（execute 之前）':<44} | "
          f"narrator 看见（execute 之后）")
    print(f"{'':>2}  {'':<12} | {'msgs':>5} {'字符':>7} {'有帧?':>6} {'帧渲染':>7} "
          f"{'page_ctx 读到':<10} | {'msgs':>5} {'字符':>7} {'帧字符':>7}")
    for r in rows:
        pre, post = r["pre"], r["post"]
        url = pre["page_ctx_read"].split(" ")[0].removeprefix("current_url=")
        print(f"{r['round']:>2}  {r['page_this_round']:<12} | {pre['msgs']:>5} "
              f"{pre['chars']:>7} {str(pre['has_frames']):>6} {pre['frame_render']:>7} "
              f"{url:<10} | {post['msgs']:>5} {post['chars']:>7} {post['frame_chars']:>7}")

    print()
    ok = True

    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} —— {detail}")

    r1, r2, r3 = rows

    def _url(r):  # 读端返回的是 [System: …] **里面**那一串
        return r["pre"]["page_ctx_read"].split(" ")[0].removeprefix("current_url=")

    check("恢复真的发生（planner 看见的消息逐轮叠加）",
          r1["pre"]["msgs"] < r2["pre"]["msgs"] < r3["pre"]["msgs"],
          f"{r1['pre']['msgs']} → {r2['pre']['msgs']} → {r3['pre']['msgs']}")
    check("_page_ctx 读到的是**第一轮**的页面（第 2/3 轮问的是别的页面）",
          _url(r2).endswith("/article/62") and _url(r3).endswith("/article/62"),
          f"第 2 轮问 /about 读到 {_url(r2)}；第 3 轮问 /guestbook 读到 {_url(r3)}")
    check("_has_frames 第 1 轮 False、第 2 轮起恒 True（首轮五条快道从此永不命中）",
          (not r1["pre"]["has_frames"]) and r2["pre"]["has_frames"]
          and r3["pre"]["has_frames"],
          "上一轮的 ToolMessage 被算成本轮证据 ⇒ 快道入口条件 `rounds==0 and not "
          "has_frames` 结构上不再成立")
    check("state 里的 ToolMessage 逐轮累积",
          r1["post"]["tool_msgs"] < r2["post"]["tool_msgs"] < r3["post"]["tool_msgs"],
          f"{r1['post']['tool_msgs']} → {r2['post']['tool_msgs']} "
          f"→ {r3['post']['tool_msgs']}")
    check("execute 的帧封顶预算开局就被历史吃掉一块（_frame_chars 起点非 0）",
          r2["post"]["frame_chars"] > 0
          and r3["post"]["frame_chars"] > r2["post"]["frame_chars"],
          f"{r1['post']['frame_chars']} → {r2['post']['frame_chars']} "
          f"→ {r3['post']['frame_chars']} 字符")
    check("喂给模型的材料单调膨胀",
          r1["post"]["chars"] < r2["post"]["chars"] < r3["post"]["chars"],
          f"{r1['post']['chars']} → {r2['post']['chars']} → {r3['post']['chars']} 字符")

    print()
    print("结论（第一格）：**接上 saver 并复用线程，恢复是真的，而读端没有一处知道"
          "「本轮」到哪儿为止**。这是纯函数层的证据，与模型采样无关。")
    return 0 if ok else 1


def cmd_history_dup(n: int = 20) -> int:
    """**注入的历史会被 reducer 再追加一遍**——逐轮复制。

    这是上一条的直接推论，但生产上真正要量的是**倍数**：`server._build_messages`
    每轮都把那 20 条历史（+摘要+回执）塞进 `graph_input(messages=…)`，而
    `add_messages` 是追加 reducer ⇒ 有 saver 时**第 r 轮的状态里有 r 份历史**。

    为什么必须单独量而不能"从代码上看出来就行"：本仓的语料里**没有任何一条多轮用例
    带 `history`**（`eval/golden/basic.jsonl`：带 `rounds` 的 5 条 hist 全是 0，
    `hist=20` 的那条是单轮）⇒ 上一条实测的 +4 条/轮里**不含**注入历史那一份，
    倍数效应在 golden 上是**量不到的**。这里用真 saver + 真 reducer 把它补上。

    预言（写在这里，免得事后按结果编）：第 r 轮的 `msgs` = r×(n+2) + 1。
    """
    saver = make_saver()
    thread = "probe-history-dup"
    saver.delete_thread(thread)
    cfg = {"configurable": {"thread_id": thread}}
    mini = _mini_graph(saver)

    def _hist() -> list:
        """每轮**新建**一份（`server._build_messages` 就是这么干的：从请求体的
        `history[]` 重新构造消息对象）。

        **必须新建**：`add_messages` 的去重条件是"`id` 相同"，而 langchain 在
        消息**第一次**进 reducer 时给它就地补一个 uuid —— 同一个对象第二轮再传进来
        时它**已经有 id 了** ⇒ 走替换、不走追加。第一版探针就是把 `hist` 建在循环
        外面，于是量出来"零复制"（假绿）。生产的注入侧每轮重建，**不共享对象**。
        """
        out = []
        for i in range(1, n + 1):
            out.append(HumanMessage(content=f"HIST#{i} 用户问的第 {i} 件事"))
            out.append(AIMessage(content=f"HIST#{i} 助手的回答"))
        return out

    print(f"thread     : {thread}（已复位）")
    print(f"注入历史   : {n} 条（{n} 组 Human/AI，每条都带可数标记 HIST#i）")
    print(f"预言       : 第 r 轮 HIST 条数 = r×{n * 2}")
    print()
    print(f"{'轮':>2} | {'msgs':>5} {'HIST 条数':>9} {'= 几份':>7} {'总字符':>8}")
    rows = []
    for r in range(1, 4):
        inp = G.graph_input(_hist() + [_user_turn(PAGES[0], f"第 {r} 轮的问题")])
        inp["plan_rounds"] = r
        list(mini.stream(inp, cfg, stream_mode=["updates"]))
        msgs = (mini.get_state(cfg).values or {}).get("messages") or []
        copies = sum(1 for m in msgs if "HIST#" in (prompt_context._msg_text(m) or ""))
        chars = sum(len(prompt_context._msg_text(m) or "") for m in msgs)
        rows.append((r, len(msgs), copies, chars))
        print(f"{r:>2} | {len(msgs):>5} {copies:>9} {copies / (n * 2):>7.1f} {chars:>8}")
    print()
    ok = all(c == r * n * 2 for r, _, c, _ in rows)
    print(f"  [{'PASS' if ok else 'FAIL'}] 历史逐轮**复制**（第 r 轮 = r 份）："
          + "、".join(f"第{r}轮×{c // (n * 2)}" for r, _, c, _ in rows))
    if rows:
        base = rows[0][3]
        print(f"  · 第 3 轮喂进图的字符 {rows[-1][3]}，是第 1 轮 {base} 的 "
              f"{rows[-1][3] / base:.2f} 倍（同一段历史被说了 3 遍）")
    print()
    print("结论：**注入侧每轮重发的历史，在有 saver 的线程上会被再存一份**。"
          "「接持久化 saver」与「继续由 Rust 注入历史」这两件事不能同时做——"
          "要么图不再注入历史（臂 C 那条路），要么每轮开新线程（今天那条路）。")
    return 0 if ok else 1


def cmd_crash_resume() -> int:
    """崩在节点中途 ⇒ 恢复**整节点重跑** ⇒ 节点里的写操作执行两次。

    这不是 langgraph 的缺陷，是"恢复单位 = superstep"的定义。危险在于**本仓的
    `execute_node` 一个节点里顺序执行 N 个写 spec**（`agent/graph.py`：`for idx, spec
    in enumerate(specs)`），而**没有任何幂等键**（全仓零 checkpoint/replay 测试）：

      · 无 checkpointer（今天）：进程崩了，这一轮整个没了 —— **不会写两次**；
      · 有 checkpointer：恢复从"最后一个**完成**的 superstep"接着跑 ⇒ 崩溃前已经
        执行过的第 1 件会**再执行一次**，然后才轮到第 2 件。

    这里用一个**外部列表当"数据库"**（侧效）来演示这件事——与 `execute_node` 里
    "调一个工具、它写了库"逐字同形。
    """
    saver = make_saver()
    thread = "probe-crash-resume"
    saver.delete_thread(thread)
    side_effects: list[str] = []   # 模拟"已经被写进库里的东西"
    attempts = {"n": 0}

    def execute_like(state):
        """一次节点执行里做 N 件写操作；第 2 次调用时在中途抛（模拟崩溃）。"""
        attempts["n"] += 1
        done = list(state.get("executed") or [])
        for i, spec in enumerate(["写A", "写B", "写C"]):
            if i in done:
                continue                      # 节点**内部**的续跑记账
            side_effects.append(spec)         # ← 真正的副作用（写库）
            done.append(i)
            if attempts["n"] == 1 and spec == "写B":
                raise RuntimeError("进程在这里崩了（模拟 OOM / 被杀 / 部署重启）")
        return {"executed": done}

    g = StateGraph(G.AgentState)
    g.add_node("execute", execute_like)
    g.add_edge(START, "execute")
    g.add_edge("execute", END)
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": thread}}

    print(f"thread : {thread}")
    print(f"节点   : execute（一个节点里顺序做 3 件写：写A/写B/写C）")
    print()
    try:
        list(app.stream(G.graph_input([]), cfg, stream_mode=["updates"]))
    except Exception as e:  # noqa: BLE001
        print(f"第 1 次跑：崩在节点中途（{type(e).__name__}: {e}）")
    print(f"  此刻「库」里已有：{side_effects}")
    print()
    print("模拟重启后再来一次（输入 None = 从 checkpoint 恢复）：")
    list(app.stream(None, cfg, stream_mode=["updates"]))
    print(f"  跑完后「库」里是：{side_effects}")
    print()
    ok = side_effects.count("写A") == 2 and side_effects.count("写B") == 2
    print(f"  [{'PASS' if ok else 'FAIL'}] 写A / 写B **各执行了两次**"
          f"（写A×{side_effects.count('写A')}、写B×{side_effects.count('写B')}、"
          f"写C×{side_effects.count('写C')}）")
    print()
    print("结论：**恢复的单位是 superstep，不是「这一轮」**。本仓 `execute_node` 一个"
          "节点里跑 N 件写、且没有幂等键 ⇒ 接上持久化 saver 之后，"
          "「崩溃后重放导致写操作重复」从「结构上不可能」变成「可能」。")
    print("（对照组：今天没有 saver ⇒ 进程崩了整轮就没了，不会写两次。）")
    return 0 if ok else 1


# ── `--crash-resume --guard`：幂等账的读数（20261016 批 2）──────────────────
# 上面那条 `--crash-resume` 演示的是"**没有账**会写两次"（今天的现状）。这一条演示
# "有账只写一次"——所以上面那条 2× 的输出就是本条**内置的反控**，不必另造一个。
#
# 为什么**必须真起子进程**：进程内抛 `BaseException` 只能证明"节点没返回"，证明不了
# "占位已经提交到库里"。而"占位已提交"正是这本账的**全部价值**——占位若随崩溃回滚，
# 账就白做了，而且**表面上看不出来**（表在、代码在，只有崩溃那一刻的读数不对）。
# 子进程用 `os._exit` 硬杀：不走 atexit、不走连接收尾，与 OOM / 被 kill 同形。
#
# ⚠️ **两处 `stream` 都必须 `durability="sync"`**（`_guard_worker` 与恢复那一次）：
# 缺省 `async` 下 checkpoint 由后台线程写、主循环不等待，硬杀时那一步可能没落盘 ⇒
# 恢复时 `__start__` 重跑、`__pregel_task_id` 变了 ⇒ **账被绕过，读数回到 2×**。
# 这不是探针的讲究，是**账的前提**（推导与实测对照见
# `agent/idem.py::require_sync_durability` 与 ADR-0012 的《触发条件》）。反过来说：
# 把这两处改回 async，本探针就会红——那是这套判据
# 的价值。（`idem.require_sync_durability` 把这条断言搬进了代码本身：档不对当场抛。）
#
# 「库」= 一个盘上的文件（跨进程、跨崩溃都在），与 `execute_node` 里"调一个工具、
# 它写了库"逐字同形。
#
# **账落在 PG**（与 saver 同一个库、同一张表 `agent_write_idem`），而且**不传 `ledger=`**
# ⇒ 走的是 `idem._shared(config)` 那条真路：它看见 saver 是 PG 的，就把账挂到 saver 那条
# 池上。这一段**后端选择**的代码是这一批新加的，除了这里没有第二处能真机验它——CI 里
# 没有 PG，块⑪ 只能钉"psycopg 没漏进 idem.py"，钉不了"真的选对了"。
#
# **清场靠换一个没人用过的 thread 名**（`uuid` 后缀）：键是 sha256，表里没有 thread_id
# 列，没有 DELETE 可做。换了 thread 名 ⇒ 新的 `thread_id`（20261019 批 F 起它是**键**的
# 第一格）⇒ 一片干净的行，而且**顺带**证明了"每次全新输入都是新键"（见下面竞态那格的 ②）。
#
# **DSN 不上屏**：它是凭据（`.env` 里那份都没它），日志里只印"落在 PG 哪张表"。

class _SideEffectTool:
    """把"写库"换成一个**真的落到盘上**的副作用；`PROBE_CRASH` 在场就硬杀。"""

    def invoke(self, args):
        with open(os.environ["PROBE_SIDE"], "a", encoding="utf-8") as f:
            f.write("写一件\n")
            f.flush()
        if os.environ.get("PROBE_CRASH"):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(9)          # 硬杀：**不**走任何收尾（模拟 OOM / 被 kill / 部署重启）
        return "写好了"


def _note_ns(config) -> None:
    """把这一格节点看到的 `checkpoint_ns` 记进 `PROBE_NS` 那个文件。

    这一格要回答的是「**它在崩溃前后逐字相同吗**」，所以必须从**节点内**取：它是
    `idem.attempt_for`（= "这一次尝试"的身份）的成分，从图外面取拿到的是另一个东西。
    ⚠️ **20261019 批 F 起它不再是键的成分**（键是会话级的）——所以"逐字变了"不再等于
    "账认不出同一件写"，本文件里凡按它下结论的地方都改过了。
    没设 `PROBE_NS` 就什么都不做（其余档位零影响）。
    """
    path = os.environ.get("PROBE_NS")
    if not path:
        return
    ns = ((config or {}).get("configurable") or {}).get("checkpoint_ns")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{ns}\n")


def _guard_graph(saver, ledger):
    """真图：唯一那次写走**账**（与 `graph.execute_node` 里那一行同一个入口）。

    节点名固定 `execute`（与夹具①同名）。`ledger=None`（安全档）⇒ `guarded_invoke`
    自己去 `idem._shared(config)` 按 saver 选后端（**生产那条路**）；两个反控档才由闭包
    塞一条账进来。**账本本体不进 `config["configurable"]`**：`idem` 的闸是"挂没挂
    checkpointer"，账从 saver 上认。
    """
    def execute(state, config):
        _note_ns(config)
        out = idem.guarded_invoke(_SideEffectTool(), "set_article_status", {}, config,
                                  fingerprint="probe", ledger=ledger)
        return {"messages": [ToolMessage(content=str(out), tool_call_id="probe")]}

    g = StateGraph(G.AgentState)
    g.add_node("execute", execute)
    g.add_edge(START, "execute")
    g.add_edge("execute", END)
    return g.compile(checkpointer=saver)


def _pg_conn(autocommit: bool):
    """一条**裸** psycopg 连接（两个反控用）：与 saver 同一个库，但**不经池**。

    DSN 取 `checkpoint_arm._dsn()`（那个"缺了就炸、绝不静默降级"是**同一份**纪律，
    在这里重写一遍就多了一处会漂的副本）。`autocommit=False` 是反控的一半：要的就是
    "占位留在一笔没提交的事务里"。
    """
    import psycopg

    from agent.checkpoint_arm import _dsn
    return psycopg.connect(_dsn(), autocommit=autocommit)


def _init_idem_table() -> None:
    """先把表建好（自动提交，走 `idem_pg` 那份 DDL 的**同一处**源码）。

    只给反控档用：它们故意不做 DDL，免得"建表"混进反控读数。**不另抄一份 CREATE
    TABLE**——抄一份就多了一处会与实现漂开的表结构，而漂开之后的表现是"账看起来是空的"。
    """
    from agent import idem_pg
    conn = _pg_conn(autocommit=True)
    try:
        conn.execute(idem_pg._DDL)
    finally:
        conn.close()


def _idem_key_rows(key: str, done: bool = False) -> int:
    """账里**这一个键**的行数（按主键查）。

    为什么不能"数全表"：键是 sha256，表里**没有 thread_id 这一列**，而这张表是
    saver 那个库里的**共用**一张表（其他探针、其他运行的行都在里面）⇒ 全表行数与
    本格无关。`done=True` 数的是"结局已记"的行（旧表的 `outcome IS NOT NULL`）。
    """
    from agent import idem_pg
    conn = _pg_conn(autocommit=True)
    try:
        sql = f"SELECT count(*) FROM {idem_pg.TABLE} WHERE key=%s"
        if done:
            sql += " AND status='done'"
        return conn.execute(sql, (key,)).fetchone()[0]
    finally:
        conn.close()


class _UnsafeLedger:
    """**反控（崩溃那一格）**：占位写在一笔**不提交**的事务里。

    进程被硬杀 ⇒ 连接断开 ⇒ PG 回滚那笔事务 ⇒ 占位没了 ⇒ 账白做、写被重放（读数
    回到 2）。这一条钉的是"占位必须**按语句**即时落库"（`idem_pg.ensure_autocommit`
    与 `idem.Ledger` 的 `isolation_level=None` 是同一件事的两档写法）：没有它，
    "账只在进程活着的时候有效"这件事**表面上看不出来**。

    ⚠️ 裸连接**不能**只把池设成非自动提交来模拟：`with conn:` 退出时
    `psycopg.Connection.__exit__` 会 `commit()`，那样它照样提交（见
    `idem_pg.ensure_autocommit`）。"不提交"必须是一条**裸连接 + 一句 COMMIT 都不发**。

    ⚠️ **不能拿它当并发那一格的反控**：一笔没提交的 INSERT 会把同一个键上的
    `ON CONFLICT` 判定**堵住**（PG 得先知道那行到底提不提交）⇒ 后到的不是"也写了一遍"，
    而是等锁/被拒。**"不提交"与"非原子"是两件事**，并发那格的反控是 `_RaceUnsafeLedger`。
    """

    def __init__(self, dsn: str):
        from agent import idem_pg
        self.table = idem_pg.TABLE
        self.conn = _pg_conn(autocommit=False)   # ← 从这一刻起，占位都留在一笔没提交的事务里

    def claim(self, key, tool, attempt, *, resuming=True):
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT status, text, kind, meta FROM {self.table} WHERE key=%s",
                        (key,))
            row = cur.fetchone()
            if row is not None:
                return row[0], row
            cur.execute(f"INSERT INTO {self.table}(key, tool, fp, status)"
                        " VALUES (%s,%s,%s,'intent')", (key, tool, attempt))
        return "new", None             # ← **没有 COMMIT**，这就是"不安全"的全部

    def finish(self, key, out):
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {self.table} SET status='done', text=%s, kind=%s,"
                        " meta=%s WHERE key=%s",
                        (str(out), str(getattr(out, "kind", "ok") or "ok"),
                         json.dumps(getattr(out, "meta", None) or {},
                                    ensure_ascii=False), key))
        return False

    def close(self):
        self.conn.close()


class _RaceUnsafeLedger:
    """**反控（并发那一格）**：占位的**原子性**被拿掉。

    `claim` 先 SELECT、再 `INSERT ... ON CONFLICT DO NOTHING`，但**不看返回的行** ⇒
    谁都以为自己抢到了。自动提交（每条语句即时落库）⇒ 不靠"不提交"制造假象，只把
    "判定与插入是一条原子语句"这件事拆开。N 个 worker 于是都真调工具 ⇒ N 次写。

    为什么需要它（而不是复用 `_UnsafeLedger`）：见那个类的 ⚠️ —— 不提交的版本会堵住
    别人的 `ON CONFLICT` 判定，根本走不到"也写一遍"。**"不提交"与"非原子"是两件事**，
    崩溃那格量前者、并发这格量后者。

    ⚠️ **SELECT 与 INSERT 之间要停一下**（`_WINDOW`），否则这一档的读数是**抖的**：
    PG 的自动提交是立刻可见的，某个 worker 的 SELECT 只要晚于别人的 INSERT 就会
    **真的看到那一行** ⇒ 它走了回放 ⇒ 读数是 3 而不是 4（20261017 实测）。那不是说
    "反控不成立"，而是**这个仪器没量准**：非原子性才是被量的事，而"谁先谁后"是采样
    运气。把窗口撑开之后，"所有人都读到空、所有人都以为自己是第一个"就变成确定的
    ——与 `--guard-slow` 用 2s 撑开 in-doubt 窗口是同一个手法。
    """

    _WINDOW = 0.5          # 秒；见类注的 ⚠️

    def __init__(self, dsn: str):
        from agent import idem_pg
        self.table = idem_pg.TABLE
        self.conn = _pg_conn(autocommit=True)

    def claim(self, key, tool, attempt, *, resuming=True):
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT status, text, kind, meta FROM {self.table} WHERE key=%s",
                        (key,))
            row = cur.fetchone()
            if row is not None:
                return row[0], row
        time.sleep(self._WINDOW)       # ← 让**所有人**都读完（都读到空）之后才开始插
        with self.conn.cursor() as cur:
            cur.execute(f"INSERT INTO {self.table}(key, tool, fp, status)"
                        " VALUES (%s,%s,%s,'intent') ON CONFLICT (key) DO NOTHING",
                        (key, tool, attempt))
        return "new", None             # ← 不看有没有插进去：被挡下也当自己抢到了

    def finish(self, key, out):
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {self.table} SET status='done', text=%s, kind=%s,"
                        " meta=%s WHERE key=%s",
                        (str(out), str(getattr(out, "kind", "ok") or "ok"),
                         json.dumps(getattr(out, "meta", None) or {},
                                    ensure_ascii=False), key))
        return False

    def close(self):
        self.conn.close()


# 三档账本。**按名取**（不是 `unsafe: bool`）：两个反控各治一件事，混成一个布尔值
# 就会像上面那条 ⚠️ 一样，把"被堵住"读成"反控不成立"。
# `safe` 那档是 `None` —— **不是**"忘了传"，而是"这一档就是不传账本"：不传才会走
# `idem._shared(config)` 那条真路（见本节头注）。
_LEDGERS = {"safe": None, "crash-unsafe": _UnsafeLedger, "race-unsafe": _RaceUnsafeLedger}


def _ledger_for(kind: str, dsn: str):
    cls = _LEDGERS[kind]
    return None if cls is None else cls(dsn)


def _stream_once(saver, thread: str, ledger, prod_shape: bool) -> None:
    """跑一轮。**那一格（`idem_resuming`）按生产同一份推导现算**。

    生产是 `server._idem_resuming(thread_id)` ⇒ `checkpoint.thread_is_resuming`（读图状态
    的 `next`）。探针**走同一个函数**，不在探针里手摆这个前提——手摆的话，这里量的就只剩
    "账本自己那一侧"，而"服务端算得对不对"那一半没人管（本仓纪律：判据的前提别自己摆）。

    必须在 `stream` **之前**算：它问的是"这一轮**开始时**上一轮跑没跑完"。
    """
    app = _guard_graph(saver, ledger)
    cfg = {"configurable": {
        "thread_id": thread,
        "idem_resuming": checkpoint.thread_is_resuming(app, thread)}}
    list(app.stream(G.graph_input([]) if prod_shape else None, cfg,
                    stream_mode=["updates"], durability=idem.REQUIRED_DURABILITY))


def _guard_worker(thread: str, unsafe: bool, dsn: str) -> None:
    """子进程体。**必须是模块级函数**：`spawn` 要 pickle 它（同 `_conc_worker`）。"""
    os.environ["PROBE_CRASH"] = "1"
    _stream_once(make_saver(), thread,
                 _ledger_for("crash-unsafe" if unsafe else "safe", dsn), prod_shape=True)
    os._exit(3)          # 到不了：工具里就硬杀了


def _side_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def _scalar(conn, sql: str, params: tuple = ()):
    """取一个标量。**别写 `fetchone()[0]`**：`saver.conn` 那条池的 `row_factory` 是
    `dict_row`（`checkpoint_arm._make_pool`，langgraph 要的），行是 **dict** ⇒
    `[0]` 抛的是 `KeyError: 0`，而它长得像"没有这一列"，很容易被读成别的问题
    （20261016 在竞态探针里真踩过一次）。"""
    row = conn.execute(sql, params).fetchone()
    return None if row is None else next(iter(row.values()))


def cmd_crash_resume_guard(unsafe: bool = False, prod_shape: bool = False) -> int:
    """`--crash-resume --guard`：真子进程崩在工具调用中途 ⇒ 账把重放压住（2× → 1×）。

    `--prod-shape`（20261019 补）：**恢复那一次改成传 dict 输入**，也就是**生产每轮
    的形状**。这一档量的是"这本账在生产那条路上认不认得出同一件写"。改前答案是
    **不认**（读数 2×）：生产形状下 langgraph **丢弃未完成任务、从 START 重启**
    （`langgraph/pregel/_loop.py:977-979`），重启后 `checkpoint_ns` 逐字变了 ⇒ 键变。
    **批 F（20261019）之后读数是 1×**——键降成会话级、`checkpoint_ns` 降级成 `attempt`
    的成分，跨轮命中时按 `idem_resuming`（= 上一轮跑没跑完）分流。

    两格内置反控，各治一件事（本仓纪律：正控不红不算数）：
      · 不带 `--prod-shape` 的 `stream(None)` 同夹具（读数 1×）——归因于"输入形状"；
      · **干净轮之后重复同一件写 ⇒ 必须 2×**（批 F 加的）——证明"读 1"不是"账退化成
        从不执行"，两格只差**上一轮跑没跑完**。
    """
    import multiprocessing as mp
    import tempfile
    import uuid

    from agent.checkpoint_arm import _dsn

    saver = make_saver()
    dsn = _dsn()
    # 唯一 thread 名 = 清场（键是 sha256，表里没有 thread 列可删，见本节头注）。
    thread = f"probe-crash-guard-{uuid.uuid4().hex[:8]}" + ("-unsafe" if unsafe else "")
    if unsafe:
        _init_idem_table()   # 反控那条路不做 DDL（见 `_UnsafeLedger`），表先建好

    with tempfile.NamedTemporaryFile(prefix="probe-guard-", suffix=".txt",
                                     delete=False) as fh:
        side = fh.name
    with tempfile.NamedTemporaryFile(prefix="probe-ns-", suffix=".txt",
                                     delete=False) as fh:
        ns_path = fh.name
    os.environ["PROBE_SIDE"] = side
    os.environ["PROBE_NS"] = ns_path
    os.environ.pop("PROBE_CRASH", None)

    print(f"thread : {thread}")
    print(f"「库」  : {side}（盘上的文件，跨进程、跨崩溃都在）")
    print(f"账     : PG（与 saver **同一个库**、表 `agent_write_idem`；DSN 是凭据，不上屏）"
          + ("\n          **不安全的**：占位留在一笔没提交的事务里 —— 这是反控"
             if unsafe else "\n          占位按语句即时落库（`ensure_autocommit`）"))
    print()
    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_guard_worker, args=(thread, unsafe, dsn))
    p.start()
    p.join()
    after_crash = _side_lines(side)
    print(f"第 1 次跑（真子进程 pid={p.pid}，退出码 {p.exitcode}）：崩在工具调用中途")
    print(f"  此刻「库」里已有：{after_crash}")
    print()
    if prod_shape:
        print("模拟重启后再来一次（输入 **dict** = 生产每轮的形状 ⇒ langgraph 丢弃未完成"
              "任务、从 START 重启）：")
    else:
        print("模拟重启后再来一次（输入 None = 从 checkpoint 恢复，**本进程**接着跑）：")
    _stream_once(saver, thread, _ledger_for("crash-unsafe" if unsafe else "safe", dsn),
                 prod_shape=prod_shape)
    final = _side_lines(side)
    print(f"  跑完后「库」里是：{final}")
    print()
    ns = _side_lines(ns_path)
    print("  `checkpoint_ns`（**「这一次尝试」的身份**，节点内实测）两遍分别是：")
    for i, v in enumerate(ns, 1):
        print(f"    第 {i} 遍：{v}")
    if len(ns) >= 2:
        same = ns[0] == ns[-1]
        print(f"    逐字相同？ **{same}**"
              + ("" if same else "  ⇒ 换了一轮（20261019 起它是 `attempt` 的成分，不再是键的）"))
    print()
    n = len(final)
    # 批 F：`--prod-shape` 的期望从 2 改成 1 —— 键降成会话级之后，换了一轮也认得出来。
    want = 2 if unsafe else 1
    ok = n == want
    print(f"  [{'PASS' if ok else 'FAIL'}] 同一件写执行了 {n} 次（期望 {want}）："
          + ("账把重放压住了" if want == 1 else "账**没**压住"))
    print()
    # ── 内置反控（批 F）：干净轮之后重复同一件写 ⇒ **必须照常执行** ──────────────
    # 没有这一格，"读 1"可能只是"账退化成从不执行"——那比双写更坏，而且同样**不报错**
    # （主人说"再显示一次"，系统回"已经显示过了"）。它与主读数只差**一轮有没有跑完**：
    # 主读数是"上一轮崩了"，这里是"上一轮干净收尾"。
    if not unsafe:
        ctl_thread = f"{thread}-repeat"
        with tempfile.NamedTemporaryFile(prefix="probe-repeat-", suffix=".txt",
                                         delete=False) as fh:
            ctl_side = fh.name
        os.environ["PROBE_SIDE"] = ctl_side
        os.environ.pop("PROBE_CRASH", None)
        for _ in range(2):        # 第 1 轮干净跑完（`next` 空）⇒ 第 2 轮 = 主人又要了一次
            _stream_once(saver, ctl_thread, None, prod_shape=True)
        ctl = len(_side_lines(ctl_side))
        ok = ok and ctl == 2
        print(f"  [{'PASS' if ctl == 2 else 'FAIL'}] 内置反控：干净轮之后重复同一件写 ⇒ "
              f"{ctl} 次（期望 2）："
              + ("合法的重复照常执行" if ctl == 2 else "**重复被吞了** ⇒ 账退化成「从不执行」"))
        print()
        os.unlink(ctl_side)
        os.environ["PROBE_SIDE"] = side
    if unsafe:
        print("结论：**占位必须按语句即时落库**。占位留在一笔没提交的事务里时，它随崩溃"
              "一起回滚 ⇒ 重放照旧发生，而表面上一切正常。")
    elif prod_shape:
        print("结论：**这本账在生产那条路上认得出同一件写了**（批 F，20261019）。改前这里"
              "读 2×：键里含 `checkpoint_ns`，而生产每轮传 dict 输入 ⇒ langgraph 从 START "
              "重启 ⇒ 那个值逐字变 ⇒ 键变。现在键是**会话级**的（跨重启稳定），"
              "`checkpoint_ns` 降级成 `attempt` 的成分，跨轮命中时按「上一轮跑没跑完」"
              "（`idem_resuming`，本档由 `checkpoint.thread_is_resuming` 现算）分流。"
              "上面那条**内置反控**（干净轮之后重复同一件写 ⇒ 2×）证明它没有退化成"
              "「从不执行」——两格只差**上一轮跑没跑完**。")
    else:
        print("结论：真子进程被硬杀之后**占位仍在库里**，恢复那一轮的第 1 件走"
              "「结果未知」（不重发、判 BLOCK）⇒ 这一格从「可能双写」变成"
              "「at-most-once」。这一档走的是 `stream(None)`（**续跑**）；生产每轮的形状"
              "（dict 输入）见 `--prod-shape`——批 F 之后**两档读数都是 1×**。")
    os.unlink(side)
    os.unlink(ns_path)
    return 0 if ok else 1


def _conc_worker(k: int, thread: str, out) -> None:
    """并发探针的子进程体。**必须是模块级函数**：`spawn` 要 pickle 它，
    局部函数 pickle 不了（`Can't pickle local object`）。"""
    from langgraph.graph import END, START, StateGraph

    def turn(state):
        return {"messages": [AIMessage(content=f"worker{k}")]}

    g = StateGraph(G.AgentState)
    g.add_node("turn", turn)
    g.add_edge(START, "turn")
    g.add_edge("turn", END)
    try:
        app = g.compile(checkpointer=make_saver())
        cfg = {"configurable": {"thread_id": thread}}
        list(app.stream(G.graph_input([HumanMessage(content=f"w{k}")]),
                        cfg, stream_mode=["updates"]))
        out.put((k, "ok", ""))
    except Exception as e:  # noqa: BLE001
        out.put((k, "err", f"{type(e).__name__}: {e}"[:200]))


def cmd_concurrency(n: int = 4) -> int:
    """同一个 thread 上 **N 个进程并发写**（= 生产 4 个 uvicorn worker 的同一会话）。

    PG 相对 SQLite 的卖点就是"多进程真并发"——所以要量的是：并发写同一 thread 时，
    是**报错**、是**串行化**、还是**静默丢**？本仓 `uvicorn --workers 4`（见 CLAUDE.md
    §4.10），而一个用户完全可能连点两次/多标签页同发 ⇒ 同一 conversation 两条请求
    落在两个 worker 上，thread_id 相同。
    """
    import multiprocessing as mp

    saver = make_saver()
    thread = "probe-concurrent"
    saver.delete_thread(thread)
    dsn = os.environ["CHECKPOINT_PG_DSN"]

    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_conc_worker, args=(k, thread, out))
             for k in range(n)]
    t0 = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
    res = [out.get() for _ in range(n)]
    elapsed = round(time.time() - t0, 1)

    print(f"thread     : {thread}  并发进程数 : {n}   耗时 {elapsed}s")
    for k, st, msg in sorted(res):
        print(f"  worker{k}: {st} {msg}")
    errs = [r for r in res if r[1] != "ok"]
    print()
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        r = conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id=%s",
                         (thread,)).fetchone()
        print(f"  该 thread 落下的 checkpoint 行数：{r[0]}（{n} 个 worker 各写各的）")
    print(f"  [{'PASS' if not errs else 'FAIL'}] 并发写同一 thread "
          f"{'全部成功（PG 串行化，无报错）' if not errs else f'{len(errs)} 个进程报错'}")
    return 0 if not errs else 1


# ══════════════════════════════════════════════════════════════════════════════
# `--concurrency N --guard`：N 个真进程**同时重放同一个未完成的 superstep**
# ══════════════════════════════════════════════════════════════════════════════
# 上一格（`--concurrency`）量的是 **checkpoint 层**：并发写同一个 thread 会不会报错、
# 会不会丢。这一格量的是**账的 claim 在真并发下是不是原子的**——也就是 ADR-0012 决策五
# 里"为什么不用 LangGraph 的 `BaseStore`"那个理由：`BaseStore.put` 是 upsert、
# **没有 compare-and-set**，占位不原子。自建表 + 主键 + `ON CONFLICT DO NOTHING`
# 一条语句才原子——**这一条是断言，本格是它的读数**。
#
# 夹具三步，缺一步读数就不成立：
#
#   ① **造一个"待跑的 superstep"，且账里没有它的占位**：跑一次图，节点在**占位之前**
#      抛 `BaseException` ⇒ 节点不返回 ⇒ step 0 不提交 ⇒ 线程上留着一个待跑的 `execute`。
#      ⚠️ 与 `--crash-resume --guard` 的差别**就在这一点上**：那一格崩在**占位之后**
#      （所以读数是"总共 1 次，恢复那轮走 in-doubt"）；这一格崩在**占位之前**，
#      所以键是**空的**，N 个 worker 是"谁先抢到谁写"。两格不是同一个问题。
#
#   ② **N 个 worker 恢复同一个 checkpoint**：键是 `sha256([thread_id, 工具名, 参数指纹])`
#      （20261019 批 F 起），N 个 worker 的 thread 与工具与指纹都相同 ⇒ 键**逐字相同**。
#      而"这一次尝试"那一格 `attempt_for` 用 `checkpoint_ns` = `f"{节点名}:{task_id}"`，
#      `__pregel_task_id` 由 `(checkpoint_id, ns, step, 节点名, triggers)` 算出
#      （`_algo.py:616`）⇒ 读的是**同一个** checkpoint、节点名都是 `execute`、指纹相同
#      ⇒ `attempt` 也逐字相同 ⇒ 走"同一 superstep 的重放"那一支（今天的行为）。
#      ⚠️ 这条前提是**实测**的，不是推的：崩那次与恢复那次拿到的 task_id 同一个
#      （`tid_probe2` 的读数）。
#      ⚠️ **它曾经顺手钉住的那条结论（"新的一轮永远是新键、永远不会被这本账压住"）
#      20261019 批 F 起已经不成立**——那正是被修掉的缺口：键降成会话级之后，新的一轮
#      与上一轮**同键**，靠 `idem_resuming`（上一轮跑没跑完）分流。见 ADR-0012 的
#      《修法（20261019，批 F）》。
#
#   ③ **节点第一行 `barrier.wait()`**：保证 N 个 worker **都读完 checkpoint、都算完
#      task_id** 之后才开抢。少了它，先跑完的那个提交了 step 0，后到的 worker
#      `stream(None)` 会**根本没有待跑的任务**（实测过）⇒ 读数退化成"只有一个 worker
#      跑过"的**空判据**（"写了 1 次"恒真，而闸没起作用）。收尾那行
#      `0 个没跑` 就是盯这一点的：任何 worker 报"没跑"⇒ 本格读数作废。
#
# 三档，第一档还会分两种形态（都合法，取决于谁先跑完）：
#   · `--guard`        真账（占位按语句即时落库 + 主键）：**恰好 1 个真调了工具**、
#                      **1 次写**；后到的那些走「回放已记的结局」（赢家已 `finish`）
#                      或「结果未知」（赢家还没记）——两种都**不再调工具**；
#   · `--guard-slow`   同上，但工具在**占位之后**停 2 秒 ⇒ 把「占位在、结局未知」那个
#                      窗口撑开，后到的**必然**全部读到「结果未知」。这一档量的是
#                      "**正在进行中**也压得住"——与"事后回放"是 at-most-once 的两半；
#   · `--guard-unsafe` 占位的**判定与插入被拆成两句**（`_RaceUnsafeLedger`，自动提交、
#                      先 SELECT 再 `INSERT OR IGNORE` 且不看 rowcount）：非原子 ⇒
#                      N 个都以为自己是第一个 ⇒ **N 次写**。
# 反控这一档**比「干脆不带账」更值钱**：它把「有没有账」与「**原子性**」分开了——不带账
# 红了只说明夹具在并发跑（那一格 `--concurrency` 已经单独量过），带一个**非原子**的账
# 还是红，才说明干活的是**主键那一句 INSERT**。
# ⚠️ 崩溃那一格的 `--guard-unsafe` 用的是**另一个**反控（`_UnsafeLedger`：占位不提交）。
# 两者不可互换，理由见各自类的 ⚠️。

# 一个 worker 的三种结局（`raw` = 拿到占位、**真调了工具**那一个；后两种都**没有**再调）：
#   `ok`      = 占位在、**结局已记** ⇒ 逐字回放赢家的结局（`idem._rehydrate` 造的 `ToolResult`）
#   `unknown` = 占位在、**结局未知** ⇒ 不重发、判 BLOCK（`idem.unknown_frame` 造的错帧）
# ⚠️ 三者的形状**不足以区分**：赢家拿到的是工具**原样**的输出（这里是个 str），
# 而"结局未知"那一路返回的是**错误帧字符串**（也是 str）⇒ 光看 `getattr(res,"kind",…)`
# 会把 in-doubt 认成赢家（两者都没有 `.kind`，缺省值都落到 `raw`），读数于是变成
# "2 个赢家"这种**看着像并发坏了**的假红。所以 in-doubt 只能**按原因码**认，且用的是
# 账本自己那个取回函数（`idem.replay_reason`）——另抄一句正则会在码改名时静默失配。
_RAW, _REPLAYED, _DOUBT = "raw", "ok", "unknown"
# 第四种：`claim` 抛了 ⇒ `guarded_invoke` 的 fail-closed 把它变成一条 `unavailable`
# 回执（**这次写被拒、一件都没执行**，不是双写）。它**不是**本格要量的东西，但必须在
# 读数里认得出——20261017 实测它在 SQLite 那一档上会偶发出现（冷启竞态，见
# `idem.Ledger._connect`），而它在读数里长得像"worker 报错"。
_UNAVAIL = "unavailable"
_RACE_KIND_CN = {_RAW: "赢家（真调了工具）", _REPLAYED: "回放赢家的结局",
                 _DOUBT: "结局未知 ⇒ BLOCK",
                 _UNAVAIL: "**账本不可用 ⇒ 这次写被拒**（fail-closed）"}


# 夹具①那个节点看到的 `checkpoint_ns`（= `f"{节点名}:{task_id}"`）。**20261019 批 F 起
# 它只用于打印**：键降成会话级之后，父进程算键只需要 `thread_id`（它本来就拿得到）。
# 留着它是因为"这一格到底变没变"在并发那一格仍然是可读的事实。
# 模块级的一个列表就够了：夹具①是在**父进程**里跑的。
_PREP_NS: list[str] = []


def _prep_crash_node(state, config):
    """夹具①：节点**在占位之前**就崩（`BaseException` 拦不住 ⇒ 节点不返回 ⇒ 不提交）。

    崩之前把 `checkpoint_ns` 记下来（见 `_PREP_NS`）——只为了把它**打印**出来。
    """
    _PREP_NS.append(str((config.get("configurable") or {}).get("checkpoint_ns") or ""))
    raise BaseException("prep: 崩在占位之前")


def _conc_guard_worker(k: int, thread: str, out, sink: str, barrier, mode: str,
                       dsn: str) -> None:
    """竞态探针的子进程体。**必须是模块级函数**：`spawn` 要 pickle 它。

    `mode` ∈ {`on`, `slow`, `unsafe`}（后两档见 `cmd_concurrency_guard` 的《三档》）。
    节点名固定叫 `execute`（与夹具①同名 ⇒ 同键）。
    """
    from langgraph.graph import END, START, StateGraph

    class _Sink:
        """"写库"= 往一个**跨进程共享**的文件追加一行；与 `_SideEffectTool` 同形。"""

        def invoke(self, args):
            if mode == "slow":
                # 把「占位已提交、结局还没记」那个窗口**撑开**（2s）：账的全部价值就在这个
                # 窗口里——别人此刻来重放，看到的是"占位在、结局未知" ⇒ 不许再写一遍。
                # 不撑开也测得出"最终只写了一次"，但那可能只是"赢家早就写完了、别人在回放"，
                # 与"**正在进行中**也压得住"是两件事。两档各钉一行。
                time.sleep(2.0)
            with open(sink, "a", encoding="utf-8") as f:
                f.write(f"写一件（worker{k}）\n")
                f.flush()
            return f"写好了 worker{k}"

    def execute(state, config):
        conf = config["configurable"]
        barrier.wait(timeout=180)          # ← 见上面第 ③ 条：先让所有人都算完 task_id
        # 本进程算出来的键（与 `guarded_invoke` 内部那一句**同一个函数**）：父进程要靠它
        # 判"N 个 worker 抢的真是同一个键"，而且那一条要与"夹具①那个待跑任务的键"对上。
        key = idem.key_for(config, "set_article_status", "probe")
        res = idem.guarded_invoke(_Sink(), "set_article_status", {}, config,
                                  fingerprint="probe",
                                  ledger=_ledger_for("race-unsafe" if mode == "unsafe" else "safe", dsn))
        # 先按原因码认 in-doubt（见上面 `_DOUBT` 那条注），剩下的才看 `.kind`：
        # 回放者拿到 `_rehydrate` 造的 `ToolResult`，赢家拿到工具原样的 str。
        kind = (_DOUBT if idem.replay_reason(str(res)) is not None
                else str(getattr(res, "kind", _RAW)))
        out.put((k, "ran", {"kind": kind,
                            "text": str(res),
                            "key": key,
                            "task_id": conf.get("__pregel_task_id")}))
        return {"messages": [ToolMessage(content=str(res), tool_call_id=f"c{k}")]}

    g = StateGraph(G.AgentState)
    g.add_node("execute", execute)
    g.add_edge(START, "execute")
    g.add_edge("execute", END)
    saver = make_saver()
    app = g.compile(checkpointer=saver)
    try:
        list(app.stream(None, {"configurable": {"thread_id": thread}},
                        stream_mode=["updates"], durability=idem.REQUIRED_DURABILITY))
        out.put((k, "done", ""))
    except Exception as e:  # noqa: BLE001 —— 屏障超时/并发报错都要看得见，不许吞
        out.put((k, "err", f"{type(e).__name__}: {e}"[:200]))


def _race_prepare(saver, thread: str, sink: str) -> tuple[bool, int, str]:
    """夹具①：造一个「待跑的 superstep」，且账里**没有**它的占位。

    返回 `(崩没崩, 竞态之前账里那个键的行数, 那个键)`。崩法用 `BaseException`（不是
    `os._exit`）：节点没返回 ⇒ step 0 不提交 ⇒ 线程上留着待跑的 `execute`，
    而**占位一次都没写**（崩在 `guarded_invoke` 之前）。这就是"键是空的、谁先抢到谁写"。
    """
    _PREP_NS.clear()
    g = StateGraph(G.AgentState)
    g.add_node("execute", _prep_crash_node)
    g.add_edge(START, "execute")
    g.add_edge("execute", END)
    prep = g.compile(checkpointer=saver)
    try:
        list(prep.stream(G.graph_input([]), {"configurable": {"thread_id": thread}},
                         stream_mode=["updates"], durability=idem.REQUIRED_DURABILITY))
        crashed = False
    except BaseException as e:  # noqa: BLE001 —— 夹具就是靠它崩的
        crashed = True
        print(f"夹具① 崩在占位之前：{type(e).__name__}: {e}")
    ns = _PREP_NS[0] if _PREP_NS else ""
    # ⚠️ **20261019 批 F 起键不再含 `checkpoint_ns`**（它降级成 `attempt` 的成分）⇒ 本格
    # 的键直接由 `thread_id` 算，与 worker 那边（节点里拿真 config 算）逐字同源。
    # `checkpoint_ns` 现在只用于**打印**：它仍然是"哪一次尝试"的身份（见 `_note_ns`）。
    key = idem.key_for({"configurable": {"thread_id": thread}}, "set_article_status", "probe")
    rows = _idem_key_rows(key)
    print(f"  待跑任务的 checkpoint_ns：{ns!r}")
    print(f"  它的幂等键：{key[:16]}…（sha256 前 16 位）")
    print(f"  此刻账里那个键的行：{rows}（**必须是 0**：键是空的，谁先抢到谁写）")
    print(f"  此刻「库」里：{_side_lines(sink)}（夹具①崩在写之前 ⇒ 也是空的）")
    print()
    return crashed, rows, key


def _race_run(n: int, thread: str, sink: str, mode: str, dsn: str) -> dict:
    """夹具②③：N 个 worker 同时恢复同一个 checkpoint；把读数打印出来并分组返回。"""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    barrier = ctx.Barrier(n)      # ← 见模块头注第 ③ 条：先让所有人算完 task_id
    procs = [ctx.Process(target=_conc_guard_worker,
                         args=(k, thread, out, sink, barrier, mode, dsn))
             for k in range(n)]
    t0 = time.time()
    for p in procs:
        p.start()
    msgs = []
    deadline = time.time() + 300
    while len(msgs) < 2 * n and time.time() < deadline:
        try:
            msgs.append(out.get(timeout=5))
        except Exception:  # noqa: BLE001 —— 队列空了就再等一轮，直到 deadline
            if all(not p.is_alive() for p in procs):
                break
    for p in procs:
        p.join(30)
    run = {"ran": {m[0]: m[2] for m in msgs if m[1] == "ran"},
           "done": {m[0] for m in msgs if m[1] == "done"},
           "errs": [m for m in msgs if m[1] == "err"],
           "elapsed": round(time.time() - t0, 1)}
    print(f"worker 读数（耗时 {run['elapsed']}s）：")
    for k in range(n):
        if k in run["ran"]:
            v = run["ran"][k]
            print(f"  worker{k}: {_RACE_KIND_CN.get(v['kind'], v['kind'])}  正文={v['text']!r}")
        elif k in run["done"]:
            print(f"  worker{k}: **没跑**（stream(None) 时已经没有待跑的任务了）")
        else:
            print(f"  worker{k}: **没报告**（进程没起来/被屏障卡住）")
    for m in run["errs"]:
        print(f"  worker{m[0]}: err {m[2]}")
    print()
    return run


def _race_facts(sink: str, run: dict, key: str) -> dict:
    """赛后的三样事实：「库」里的行、账里那个键的行、其中「结局已记」的行。

    ⚠️ 账里的行数**必须在这里现读**：`_race_prepare` 那个读数是**竞态之前**的
    （它检查的是"夹具①没占位"），拿它当赛后的行数会让那一格**恒红**——20261016
    真踩过，库里明明只有 1 行。

    按**键**数（不是数全表）：这张表在 saver 那个库里是**共用**的一张（其他探针、
    其他运行的行都在里面），键是 sha256、表里也没有 thread_id 列 ⇒ 只有"这一个键
    有几行"是本格的读数。
    """
    writes = _side_lines(sink)
    rows_after = _idem_key_rows(key)
    recorded = _idem_key_rows(key, done=True)
    winners = [k for k, v in run["ran"].items() if v["kind"] == _RAW]
    losers = {k: v["kind"] for k, v in run["ran"].items() if v["kind"] != _RAW}
    if winners:
        print(f"赢家 worker{winners[0]} 的正文：{run['ran'][winners[0]]['text']!r}；"
              f"回放者拿到的正文："
              f"{[run['ran'][k]['text'] for k in sorted(losers) if losers[k] == _REPLAYED]!r}")
        print()
    return {"writes": writes, "rows_after": rows_after, "recorded": recorded,
            "winners": winners, "losers": losers,
            "tids": {str(v["task_id"]) for v in run["ran"].values()},
            "keys": {str(v.get("key")) for v in run["ran"].values()}}


def _race_checks(n: int, mode: str, prep: tuple, run: dict, facts: dict) -> list:
    """本格的判据。`prep` = `(崩没崩, 竞态前那个键的行数, 那个键)`。"""
    crashed, rows_before, key = prep
    unavail = sum(1 for v in run["ran"].values() if v["kind"] == _UNAVAIL)
    checks = [
        (crashed and rows_before == 0, "夹具① 留下一个待跑的 superstep，且账里没有占位"),
        (len(run["ran"]) == n, f"{n} 个 worker **都真跑了**（没有空判据）"),
        (len(facts["tids"]) == 1 and "" not in facts["tids"],
         f"{n} 个 worker 的 `__pregel_task_id` 逐字相同（实测：{sorted(facts['tids'])}）"),
        # 键是幂等判定的**全部身份**：N 个 worker 抢的必须是同一个键，而且必须**就是**
        # 夹具①那个待跑任务的键（后者是"重放认得出是同一件"的正面读数——它若不成立，
        # 下面的行数会变成 0，而那条 FAIL 看着像"账没写进去"）。
        (facts["keys"] == {key},
         f"{n} 个 worker 算出的键与夹具①那个待跑任务的键逐字相同"
         f"（{len(facts['keys'])} 个不同的键）"),
        (not run["errs"], "没有 worker 报错"),
        (len(facts["writes"]) == (1 if mode != "unsafe" else n),
         f"同一件写执行了 {len(facts['writes'])} 次"
         f"（期望 {1 if mode != 'unsafe' else n}）"),
    ]
    if mode == "unsafe":
        return checks
    kinds = set(facts["losers"].values())
    checks += [
        # 账本不可用 = `claim` 抛了 ⇒ 这次写**被拒**（fail-closed，不是双写）。它与
        # "1 次写"不冲突，但**不能算过**：一个"偶尔把合法写拒掉"的账不是可用的账。
        # 反控档不查这条——那一档的账是**故意**不可用的。
        # ⚠️ PG 档**必须恒为 0**：SQLite 那档的偶发不可用是冷启竞态
        # （`idem.Ledger._connect` 那条重试治的就是它），PG 没有开库竞态这回事。
        (unavail == 0,
         f"账本在并发下可用（不可用 {unavail}/{n} 次；PG 档必须恒为 0）"),
        (len(facts["winners"]) == 1,
         f"恰好 1 个 worker 真调了工具（赢家：worker{facts['winners']}）"),
        (len(facts["losers"]) == n - 1 and kinds <= {_REPLAYED, _DOUBT},
         f"其余 {n - 1} 个一件都没再调"
         f"（结局：{ {_RACE_KIND_CN.get(v, v) for v in kinds} }）"),
        (facts["rows_after"] == 1 and facts["recorded"] == 1,
         f"账里恰好 {facts['rows_after']} 行、且结局已记（主键挡住了第二次 claim）"),
    ]
    if mode == "slow":
        checks.append((kinds == {_DOUBT},
                       "工具慢（占位已提交、结局还没记）⇒ 后到的全都读到"
                       "「占位在、结局未知」⇒ in-doubt，**不是**等赢家写完再回放"))
    else:
        checks.append((kinds <= {_REPLAYED, _DOUBT},
                       "后到的两种结局都是合法的（回放 / in-doubt），**没有第三种**"))
    return checks


_RACE_CONCLUSION = {
    "on": ("结论：**claim 在真并发下是原子的**——N 个进程读同一个 checkpoint、算同一个键，"
           "只有 1 个 INSERT 成功，其余走「回放已记的结局」或「in-doubt」。写**只发生了一次**，"
           "而这一条正是当初不用 `BaseStore.put`（upsert、没有 CAS）的理由（ADR-0012 决策五）。"),
    "slow": ("结论：把「占位已提交、结局未记」那个窗口撑到 2s，后到的 worker 仍然"
             "**一次都没写**——读到的是 in-doubt（判 BLOCK），不是「等赢家写完再回放」。"
             "⇒ 这本账压住的不只是「事后回放」，还有「**正在进行中**」"
             "（at-most-once 的另一半）。"),
    "unsafe": ("结论：**反控成立**。占位的判定与插入被拆成两句（先 SELECT 再 `INSERT ... "
               "ON CONFLICT DO NOTHING`、且不看有没有插进去），N 个 worker 都以为自己是"
               "第一个 ⇒ N 次写。⇒ 光「有账」不够，**原子性**（主键 + `ON CONFLICT DO "
               "NOTHING` 的**返回行**，一条语句）才是干活的那一句。"
               "⚠️ 这一档量的是**非原子**，不是「不提交」——「不提交」那个反控在崩溃那一格"
               "（`--crash-resume --guard-unsafe`）：一笔没提交的 INSERT 会把同一个键上的 "
               "`ON CONFLICT` 判定堵住（PG 得先知道那行提不提交），后到的不是「也写了一遍」"
               "而是等锁，量不出 N 次写。"),
}


def cmd_concurrency_guard(n: int = 4, mode: str = "on") -> int:
    """`--concurrency N --guard`：N 个进程同时重放同一个未完成的 superstep。

    三段（夹具① / 夹具②③ / 判据）各自成函数——`test_complexity_budget.py` 的预算
    是 C ≤ 30，一坨写下来是 46（**不许**靠 `--update` 记进台账来消掉，那是放宽）。
    """
    import tempfile
    import uuid

    from agent.checkpoint_arm import _dsn, make_saver

    label = {"on": "真账（PG：自动提交 + 主键 + `ON CONFLICT DO NOTHING`）",
             "slow": "真账，但工具**慢**（占位提交后停 2s 才记结局）",
             "unsafe": "**不安全的**（先 SELECT 再 INSERT、不看有没有插进去）—— 这是反控"}[mode]
    # 唯一 thread 名 = 清场（键是 sha256，表里没有 thread 列可删，见本节头注）。
    thread = f"probe-conc-guard-{mode}-{uuid.uuid4().hex[:8]}"
    saver = make_saver()
    dsn = _dsn()
    if mode == "unsafe":
        # 只给反控档预建表：`on`/`slow` 两档**故意**不预建，让它们走真实的
        # `idem._shared(config)` ⇒ 第一次真用时由它自己 `setup`（幂等建表）。
        _init_idem_table()
    with tempfile.NamedTemporaryFile(prefix="probe-race-", suffix=".txt",
                                     delete=False) as fh:
        sink = fh.name

    print(f"thread : {thread}    worker 数 : {n}")
    print(f"账     : PG（与 saver **同一个库**、表 `agent_write_idem`；DSN 是凭据，不上屏）")
    print(f"         {label}")
    print(f"「库」  : {sink}（盘上的文件，跨进程都在）")
    print()

    prep = _race_prepare(saver, thread, sink)
    run = _race_run(n, thread, sink, mode, dsn)
    facts = _race_facts(sink, run, prep[2])
    checks = _race_checks(n, mode, prep, run, facts)
    ok = all(c for c, _ in checks)
    for c, t in checks:
        print(f"  [{'PASS' if c else 'FAIL'}] {t}")
    print()
    print(_RACE_CONCLUSION[mode])
    os.unlink(sink)
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════════════════
# `--backend-parity`：同一份序列走两档账本，读数必须逐字相等
# ══════════════════════════════════════════════════════════════════════════════
# 两档账本是**两份实现**（`idem.Ledger` = SQLite 文件 / `idem_pg.PostgresLedger` = PG），
# 而它们要满足**同一份契约**：`claim` 交出的东西的形状、`finish` 的撞顶判定、重放还回的
# (文本, kind, meta)。任何一条不同，同一个写走哪一档就会得到**不同的重放结果**——
# 而**两档各自的判据都是绿的**（CI 只跑 SQLite、探针其余几格只跑 PG）。这正是本仓
# 反复吃过的"两份实现各自绿、合起来错"。
#
# 先例：`tests/test_action_text.py` ⑤「同一份输入走两侧、结果直接相等」。
#
# ⚠️ 键**必须每次现造**（`uuid` 后缀）：这张表是 saver 那个库里**共用**的一张，写死键
# 会让第二次运行读到第一次留下的行 ⇒ 第一步从 `new` 变成 `done`，而那看起来像
# "两个后端行为不同"。

_PARITY_TOOL = "set_article_status"


def _parity_sequence(led, cases: list) -> list:
    """在**一本账**上跑同一份序列，把每一步的读数收成一份可逐字比较的形状。

    一件写六步：`claim`（新键 ⇒ 必须 `new`）→ `finish` → 再 `claim`（⇒ `done` + 重放）
    → **换一个 attempt 且 `resuming=True`**（⇒ 仍是 `done`：上一轮没跑完）→
    **再换一个 attempt 且 `resuming=False`**（⇒ `new`：主人又要了一次，那行被改回
    `intent`）→ 同一 attempt 再 `claim`（⇒ `intent`）。

    后三步是 20261019 批 F 加的：那一支（"原子地改回 intent"）**两个后端各写了一遍**，
    而"两份实现各自绿、合起来错"正是本仓反复吃过的形状——`UPDATE ... WHERE` 的
    `rowcount` 语义、`ts` 的写法（`now()` vs `time.time()`）都可能在这一档上分家。

    记下**行的类型**（`type(row).__name__`）是刻意的：那是 `_rehydrate` 那个坑的真机
    判据——CI 的块⑩ 只能喂一个**手搓的** dict，证明不了"PG 那条路真的会交元组"。
    """
    seq = []
    for key, tool, fp, out in cases:
        state, row = led.claim(key, tool, fp)
        seq.append(("claim-new", state, type(row).__name__))
        seq.append(("finish", bool(led.finish(key, out)), None))
        state, row = led.claim(key, tool, fp)
        r = idem._rehydrate(row)
        seq.append(("replay", state, type(row).__name__, str(r),
                    getattr(r, "kind", None), getattr(r, "meta", None)))
        state, row = led.claim(key, tool, fp + "-turn2", resuming=True)
        seq.append(("turn2-resuming", state, type(row).__name__))
        state, row = led.claim(key, tool, fp + "-turn3", resuming=False)
        seq.append(("turn3-repeat", state, type(row).__name__))
        state, row = led.claim(key, tool, fp + "-turn3", resuming=False)
        seq.append(("turn3-again", state, type(row).__name__))
    return seq


def cmd_backend_parity() -> int:
    """`--backend-parity`：同一份写序列在 SQLite 与 PG 两档上跑，读数逐字比。"""
    import tempfile
    import uuid

    from agent import idem_pg
    from agent.checkpoint_arm import make_saver

    tag = uuid.uuid4().hex[:8]
    cases = [
        (f"parity-{tag}-1", _PARITY_TOOL, "fp1",
         ToolResult("正文一", "ok", {"cmd": {"kind": "navigate", "url": "/about"}})),
        (f"parity-{tag}-2", _PARITY_TOOL, "fp2", ToolResult("x" * 50, "ok", {"n": 2})),
        # 撞顶那件：两档**必须都**判 capped、**都**不给正文（存 NULL）
        (f"parity-{tag}-3", _PARITY_TOOL, "fp3",
         ToolResult("y" * (idem._RESULT_MAX + 10), "ok", {})),
    ]
    saver = make_saver()
    idem_pg.setup(saver.conn)          # 幂等建表（与 `_shared` 里那句同一处源码）
    sqlite = idem.Ledger(os.path.join(tempfile.mkdtemp(prefix="parity-"), "idem.sqlite3"))
    pg = idem_pg.PostgresLedger(saver.conn)
    try:
        a = _parity_sequence(sqlite, cases)
        b = _parity_sequence(pg, cases)
    finally:
        sqlite.close()

    print(f"同一份 {len(cases)} 件写 × 6 步（含批 F 的换轮两支），走两档账本：")
    print(f"  · SQLite：{sqlite.path}")
    print(f"  · PG    ：与 saver **同一个库**、表 `{idem_pg.TABLE}`（DSN 是凭据，不上屏）")
    print()
    for i, (x, y) in enumerate(zip(a, b)):
        print(f"  {'  ' if x == y else '≠ '}{i:2} {x!r}")
        if x != y:
            print(f"     ↳ 另一侧：{y!r}")
    print()
    replays = [s for s in a if s[0] == "replay"]
    capped = [s for s in a if s[0] == "finish" and s[1] is True]
    checks = [
        (len(a) == len(b) and not [1 for x, y in zip(a, b) if x != y],
         f"两档 {len(a)} 步读数**逐字相等**（不等 ⇒ 同一个写走哪一档会得到不同的重放结果）"),
        (all(s[2] == "tuple" for s in replays),
         f"两档重放拿到的行**都是元组**（实测 {sorted({s[2] for s in replays})}）——"
         "PG 那条池是 `dict_row`，交 dict 会**不报错地**回放字面串 `'text'`"),
        (len(capped) == 1,
         f"撞 `_RESULT_MAX` 那件两档都判 capped（实测 {len(capped)} 件）"),
        (any(s[3] == str(idem._NOREPLAY_FRAME) for s in replays),
         "撞顶那件的重放帧是**同一句**降级帧（写确实发生了，只是原始返回没留档）"),
        (any(isinstance(s[5], dict) and s[5].get("cmd") for s in replays),
         "`meta` 逐字还回（命令类写工具的连线命令就住在 `meta['cmd']`）"),
    ]
    ok = all(c for c, _ in checks)
    for c, t in checks:
        print(f"  [{'PASS' if c else 'FAIL'}] {t}")
    return 0 if ok else 1


def cmd_pg_stats() -> int:
    """实验库里现在有什么（逐 thread 行数 / 字节 / 单行大小）。"""
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("CHECKPOINT_PG_DSN") or ""
    if not dsn:
        print("CHECKPOINT_PG_DSN 没设（source /home/ubuntu/exp/ckpt.env）", file=sys.stderr)
        return 2
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        print("== 表级 ==")
        for r in conn.execute("""
            SELECT c.relname AS tbl, pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY bytes DESC""").fetchall():
            print(f"  {r['tbl']:<24} {r['bytes']:>12,} B")
        print()
        print("== 逐 thread ==")
        rows = conn.execute("""
            SELECT thread_id,
                   count(*) AS ckpts,
                   count(DISTINCT checkpoint_ns) AS namespaces,
                   pg_size_pretty(sum(pg_column_size(checkpoint))::bigint) AS payload,
                   round(avg(pg_column_size(checkpoint)))::int AS avg_row
            FROM checkpoints GROUP BY thread_id ORDER BY ckpts DESC LIMIT 30""").fetchall()
        if not rows:
            print("  （空）")
        for r in rows:
            print(f"  {r['thread_id']:<34} ckpts={r['ckpts']:>5} "
                  f"ns={r['namespaces']:>3} payload={r['payload']:>10} "
                  f"avg_row={r['avg_row']:>7} B")
        print()
        print("== blobs（序列化后的 state，含 messages 与 tool_data）==")
        r = conn.execute("""
            SELECT count(*) AS n, pg_size_pretty(sum(pg_column_size(blob))::bigint) AS sz,
                   max(pg_column_size(blob)) AS max_row
            FROM checkpoint_blobs""").fetchone()
        print(f"  n={r['n']} 总={r['sz']} 单行最大={r['max_row']} B")
    return 0


def cmd_summarize(path: str) -> int:
    """把 `CHECKPOINT_PROBE_OUT` 的 JSONL 汇总成"逐轮曲线"。"""
    if not os.path.exists(path):
        print(f"没有这个文件：{path}", file=sys.stderr)
        return 2
    by_site: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_site.setdefault(rec.get("site") or "?", []).append(rec)
    for site, recs in sorted(by_site.items()):
        print(f"== {site}（{len(recs)} 次）==")
        msgs = [r.get("msgs") for r in recs if isinstance(r.get("msgs"), int)]
        if msgs:
            print(f"  msgs      : min={min(msgs)} max={max(msgs)} "
                  f"avg={sum(msgs)/len(msgs):.1f}")
        pc = [r.get("prompt_chars") for r in recs if isinstance(r.get("prompt_chars"), int)]
        if pc:
            print(f"  prompt_chars: min={min(pc)} max={max(pc)} avg={sum(pc)/len(pc):.0f}")
        fr = [r.get("frames") for r in recs if isinstance(r.get("frames"), int)]
        if fr:
            print(f"  frames    : min={min(fr)} max={max(fr)} avg={sum(fr)/len(fr):.1f}")
        pages = [r.get("page_ctx") for r in recs if r.get("page_ctx")]
        if pages:
            uniq = {}
            for p in pages:
                head = str(p).split(" ")[0]
                uniq[head] = uniq.get(head, 0) + 1
            top = sorted(uniq.items(), key=lambda kv: -kv[1])[:6]
            print(f"  page_ctx  : " + "、".join(f"{k}×{v}" for k, v in top))
        print()
    return 0


def _windowed_mini_graph(saver, sink: list):
    """与 `_mini_graph` 同构，但节点的**动作顺序照线上**：先冻结、再读、最后追加。

    顺序是这个实验的全部要害：

      · 冻结必须在**还没人追加**的时候做（= `planner_node` 的位置，`START → planner`
        是唯一入口）。晚一步算出来就偏，窗口会把开头的 `[System: …]` 挤出去；
      · 本轮族的读端在冻结之后、追加之前读（= planner 的判据）；
      · 追加之后还有读端（= model / gate）——它们也走**同一个** `turn_start`，
        因为起点已经冻住，追加再多条也不会漂。

    `sink` 收每轮的观察值（节点里 print 会被 stream 的输出搅乱，收出来统一渲染）。
    """

    def turn(state):
        i = state.get("plan_rounds", 0)
        # ① 冻结（与 `graph.planner_node` **同源**：同一个纯函数，不是重写一遍）
        start = prompt_context.freeze_turn_start(state.get("messages"), state.get("turn_msgs"),
                                          state.get("turn_start", -1))
        # ② 本轮窗口（图内唯一的取值口，线上走 `graph._turn(state)`）
        win = prompt_context.turn_window(state.get("messages"), start)
        whole = state.get("messages") or []
        sink.append({
            "round": i, "frozen_start": start,
            "turn_msgs": state.get("turn_msgs"), "len": len(whole),
            "win_msgs": len(win), "identity": win is whole,
            # 四个读端各读**两遍**：窗口 vs 整表。两列并排才看得出这一刀在治什么。
            "page_win": prompt_context._page_ctx(win),
            "page_whole": prompt_context._page_ctx(whole),
            "frames_win": prompt_context._has_frames(win),
            "frames_whole": prompt_context._has_frames(whole),
            "fchars_win": G._frame_chars(win),
            "fchars_whole": G._frame_chars(whole),
        })
        # ③ 追加本轮产出（= `execute_node` 干的事）
        return {"messages": [AIMessage(content=f"第 {i} 轮回复"),
                             ToolMessage(content=FRAME_BODIES[(i - 1) % 3],
                                         tool_call_id=f"c{i}", name="get_article_detail")],
                "plan_rounds": i,
                # 冻结结果写回 state：追加之后的读端（model/gate）靠它，不重算。
                "turn_start": start}

    g = StateGraph(G.AgentState)
    g.add_node("turn", turn)
    g.add_edge(START, "turn")
    g.add_edge("turn", END)
    return g.compile(checkpointer=saver)


def cmd_turn_window() -> int:
    """**本轮窗口**的判据（20261015 批 1）：有窗口时读对本轮页面，人为关掉窗口时
    必须**复现今天的错读**。

    为什么必须是"两臂 + 真 saver"而不是纯函数单测：纯函数那一层（`tests/
    test_turn_window.py`）只证"切片算得对"，证不了"读端真的因此读对了"——
    中间隔着"谁在什么时候把 `turn_start` 写进 state"这一跳，而那一跳正是这一刀
    最容易接错的地方（接晚了 ⇒ 窗口偏 ⇒ 注入块被挤出 ⇒ 页面读错，**且不报错**）。
    这里用真 reducer + 真 checkpoint 把那一跳也走一遍。

    `turn_msgs = 0`（⇒ `turn_start = -1` ⇒ 不窗口化）就是**今天的行为**：那一臂是
    反向对照，它必须**红**（复现 `/article/62`）。否则前面那格是恒绿的。
    """
    rows_all = {}
    for arm, turn_msgs in (("窗口", None), ("无窗口（今天）", 0)):
        saver = make_saver()
        thread = f"{THREAD}-turnwin"
        saver.delete_thread(thread)   # 跨臂不许串：同 thread 的 checkpoint 会累积
        sink: list = []
        mini = _windowed_mini_graph(saver, sink)
        cfg = {"configurable": {"thread_id": thread}}
        for i, page in enumerate(PAGES, start=1):
            inp = G.graph_input([_user_turn(page, f"第 {i} 轮的问题")])
            inp["plan_rounds"] = i
            if turn_msgs is not None:
                # 反向对照臂：把"本轮几条"人为设成 0 = 没记边界 = 老行为。
                inp["turn_msgs"] = turn_msgs
            list(mini.stream(inp, cfg, stream_mode=["updates"]))
        rows_all[arm] = sink

    def _url(s: str) -> str:
        return (s or "").split(" ")[0].removeprefix("current_url=")

    for arm, rows in rows_all.items():
        print(f"\n【{arm}】")
        print(f"  {'轮':>2} {'本轮页面':<11} | {'冻结起点':>8} {'窗口':>7} {'恒等?':>6} | "
              f"{'page_ctx(窗口)':<22} {'page_ctx(整表)':<22} | {'有帧(窗口)':>9} "
              f"{'有帧(整表)':>9}")
        for r in rows:
            print(f"  {r['round']:>2} {PAGES[r['round'] - 1].split(' ')[0]:<11} | "
                  f"{r['frozen_start']:>8} {r['win_msgs']:>3}/{r['len']:<3} "
                  f"{str(r['identity']):>6} | {_url(r['page_win']):<22} "
                  f"{_url(r['page_whole']):<22} | {str(r['frames_win']):>9} "
                  f"{str(r['frames_whole']):>9}")

    print()
    ok = True

    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} —— {detail}")

    w = rows_all["窗口"]
    n = rows_all["无窗口（今天）"]

    # 比的是**读端看见的东西**，不是 `frozen_start`：第 1 轮窗口臂冻成 0、无窗口臂是 -1，
    # 两者**本来就不该相等**（一个记了边界、一个没记），而 `turn_window` 对 0 和 -1 都是
    # 恒等 ⇒ 读端逐格相同。把 `frozen_start` 塞进比对里等于拿"实现细节"当判据，会假红。
    check("第 1 轮两臂**读端逐格相同**（无历史可窗口 ⇒ 这一刀在第一轮是恒等）",
          all(w[0][k] == n[0][k] for k in
              ("win_msgs", "len", "identity", "page_win", "frames_win", "fchars_win")),
          f"窗口 {w[0]['win_msgs']}/{w[0]['len']} vs 无窗口 {n[0]['win_msgs']}/{n[0]['len']}")
    check("第 1 轮的差别只在「记没记边界」这一个内部标记上（0 = 表头，-1 = 没记）",
          (w[0]["frozen_start"], n[0]["frozen_start"]) == (0, -1),
          f"窗口臂 {w[0]['frozen_start']}（= 表头，整个列表）vs 无窗口臂 {n[0]['frozen_start']}（没记）")

    check("窗口臂：第 2/3 轮的窗口读到**本轮**页面（这是这一刀要的结果）",
          _url(w[1]["page_win"]).endswith("/about")
          and _url(w[2]["page_win"]).endswith("/guestbook"),
          f"第 2 轮 {_url(w[1]['page_win'])}；第 3 轮 {_url(w[2]['page_win'])}")

    check("窗口臂：窗口真的在收窄（起点 > 0 且 窗口 < 整表）",
          w[1]["frozen_start"] > 0 and w[1]["win_msgs"] < w[1]["len"]
          and w[2]["frozen_start"] > 0 and w[2]["win_msgs"] < w[2]["len"],
          f"第 2 轮 {w[1]['win_msgs']}/{w[1]['len']}，第 3 轮 {w[2]['win_msgs']}/{w[2]['len']}")

    check("窗口臂：第 2/3 轮「本轮还没执行」⇒ 窗口内无帧（首轮快道条件恢复成立）",
          (not w[1]["frames_win"]) and (not w[2]["frames_win"]),
          f"窗口内 {w[1]['frames_win']}/{w[2]['frames_win']}；"
          f"同一时刻整表读是 {w[1]['frames_whole']}/{w[2]['frames_whole']}")

    check("窗口臂：冻结结果写回了 state（追加之后的读端也用它 ⇒ 起点不漂）",
          w[1]["frozen_start"] == w[1]["turn_msgs"] + 2,   # 前一轮 1 条输入 + 2 条产出
          f"第 2 轮起点 {w[1]['frozen_start']}（= 上一轮留下的 3 条）")

    check("反向对照：**关掉窗口**必须复现今天的错读（第 2/3 轮读到第 1 轮页面）",
          _url(n[1]["page_whole"]).endswith("/article/62")
          and _url(n[2]["page_whole"]).endswith("/article/62"),
          f"第 2 轮问 /about 读到 {_url(n[1]['page_whole'])}；"
          f"第 3 轮问 /guestbook 读到 {_url(n[2]['page_whole'])}")

    check("反向对照：**关掉窗口**时上一轮的帧被算成本轮证据（第 2 轮起恒 True）",
          (not n[0]["frames_whole"]) and n[1]["frames_whole"] and n[2]["frames_whole"],
          "首轮五条快道入口条件 `rounds==0 and not has_frames` 结构上不再成立")

    check("反向对照臂里「窗口 = 整表」是恒等（证明它确实什么都没做）",
          all(r["identity"] for r in n),
          f"identity = {[r['identity'] for r in n]}")

    print()
    print("结论（第三格）：**有窗口 ⇒ 读端回到本轮；关掉窗口 ⇒ 逐格复现今天的错读。**"
          "反向对照那一臂不是恒绿的，所以前一格不是判据自己跟自己玩。")
    return 0 if ok else 1


# ── 批 3：narrator 窗口的**正向收缩**合成探针 ─────────────────────────────
#: 轮数要**明显超过** `prompt_context._NARRATOR_TURNS`（10），否则窗口一次都不触发。
NARR_ROUNDS = 12


def _narr_mini_graph(saver, sink: list):
    """与 `_windowed_mini_graph` 同构，节点里量的是**narrator 那一刀**。

    动作顺序照线上：① `planner_node` 冻结 `turn_start`；② `model_node` 取 narrator 的
    消息表（线上走 `graph._narrator_msgs(state)`，这里直接调它委派的那个纯函数
    `prompt_context.narrator_messages`——**同一个函数**，不是重写一遍）；③ 追加本轮产出。

    每轮额外记两列：`with_tool_call_pairs(win)` 的字符数（narrator 真正发出去的那一段，
    与 `graph.model_node` 量 `_prompt_chars` 同口径）与整表的同一量。两列并排才看得出
    "封顶"与"线性膨胀"。
    """

    def turn(state):
        i = state.get("plan_rounds", 0)
        msgs = state.get("messages") or []
        # ① 冻结（同源纯函数）
        start = prompt_context.freeze_turn_start(msgs, state.get("turn_msgs"),
                                          state.get("turn_start", -1))
        # ② narrator 的窗口（线上是 `graph._narrator_msgs(state)`）
        win = prompt_context.narrator_messages(msgs, start)
        win_wire = prompt_context.with_tool_call_pairs(win)
        whole_wire = prompt_context.with_tool_call_pairs(msgs)
        sink.append({
            "round": i, "frozen_start": start, "len": len(msgs),
            "win_msgs": len(win), "ident": win is msgs,
            "first_is_sys": bool(win) and prompt_context._is_system_block(win[0]),
            "chars_win": sum(len(prompt_context._msg_text(m) or "") for m in win_wire),
            "chars_whole": sum(len(prompt_context._msg_text(m) or "") for m in whole_wire),
            "frames_win": len([m for m in win_wire if type(m).__name__ == "ToolMessage"]),
        })
        return {"messages": [AIMessage(content=f"第 {i} 轮回复"),
                             ToolMessage(content=FRAME_BODIES[(i - 1) % 3],
                                         tool_call_id=f"c{i}", name="get_article_detail")],
                "plan_rounds": i,
                "turn_start": start}

    g = StateGraph(G.AgentState)
    g.add_node("turn", turn)
    g.add_edge(START, "turn")
    g.add_edge("turn", END)
    return g.compile(checkpointer=saver)


def cmd_narrator_window() -> int:
    """**narrator 窗口**的正/反向对照（20261016 批 3）。

    为什么要合成：golden 的多轮用例最多 **3 轮**（实测 `rounds <= 3`），而窗口深度
    `_NARRATOR_TURNS = 10` ⇒ 在真链路上窗口**一次都不会触发**（端到端只能验"不过度
    触发"这一侧）。正向收缩这一半必须自己造 12 轮出来。

    两臂只差一格：`turn_msgs` 报不报（`graph_input` 在线上**恒报**，见
    `graph.py:14013`）。不报 ⇒ `freeze_turn_start` 返回 -1 ⇒ `narrator_messages` 原样
    返回整表 = **今天的行为**。所以反向对照臂必须**复现线性膨胀**，否则前一臂是恒绿的。
    """
    rows_all = {}
    for arm, report in (("窗口（批 3）", True), ("无窗口（今天）", False)):
        saver = make_saver()
        thread = f"{THREAD}-narrwin"
        saver.delete_thread(thread)   # 跨臂不许串（同 thread 的 checkpoint 会累积）
        sink: list = []
        mini = _narr_mini_graph(saver, sink)
        cfg = {"configurable": {"thread_id": thread}}
        for i in range(1, NARR_ROUNDS + 1):
            page = PAGES[(i - 1) % len(PAGES)]
            inp = G.graph_input([_user_turn(page, f"第 {i} 轮的问题")])
            inp["plan_rounds"] = i
            if not report:
                inp["turn_msgs"] = 0     # 反向对照：没记边界 = 老行为
            list(mini.stream(inp, cfg, stream_mode=["updates"]))
        rows_all[arm] = sink

    for arm, rows in rows_all.items():
        print(f"\n【{arm}】")
        print(f"  {'轮':>2} {'起点':>4} {'窗口/整表':>10} {'恒等?':>6} {'首条是注入块?':>12} "
              f"{'帧(窗口)':>8} {'narrator段字符':>14} {'整表字符':>10}")
        for r in rows:
            print(f"  {r['round']:>2} {r['frozen_start']:>4} "
                  f"{r['win_msgs']:>4}/{r['len']:<5} {str(r['ident']):>6} "
                  f"{str(r['first_is_sys']):>12} {r['frames_win']:>8} "
                  f"{r['chars_win']:>14} {r['chars_whole']:>10}")

    print()
    ok = True

    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} —— {detail}")

    w = rows_all["窗口（批 3）"]
    n = rows_all["无窗口（今天）"]
    # ⚠️ **下标写全，别用"末轮"这种相对说法**：第一版把 `NARR_ROUNDS-1` 同时当"倒数第二轮"
    # 用，于是两处判据都在**拿一行跟它自己比**——恒真，而且看起来还印了具体数字。
    # 判据里的"两行之比"必须一眼看出是**两行**。
    i_last = NARR_ROUNDS - 1                    # 第 12 轮
    i_sat = prompt_context._NARRATOR_TURNS - 1         # 第 10 轮：历史刚超过窗口深度 ⇒ 从这轮起饱和
    tail_w = w[i_sat:]

    check("窗口臂：第 1 轮恒等（本轮起点就在表头 ⇒ 这一刀在第一轮无事可做）",
          w[0]["ident"] is True and w[0]["frozen_start"] == 0,
          f"起点 {w[0]['frozen_start']}、恒等 {w[0]['ident']}")
    check("窗口臂：第 2 轮起闸门真的开火（起点 > 0 ⇒ 窗口不再等于整表）",
          w[1]["frozen_start"] > 0 and w[1]["ident"] is False,
          f"第 2 轮起点 {w[1]['frozen_start']}、窗口 {w[1]['win_msgs']}/{w[1]['len']}")
    check("窗口臂：切的是**轮**不是条数（窗口首条恒为 `[System: ` 注入块）",
          all(r["first_is_sys"] for r in w),
          f"{NARR_ROUNDS} 轮里首条是注入块的有 {sum(1 for r in w if r['first_is_sys'])} 轮")
    check(f"窗口臂：饱和之后**条数恒定**（第 {i_sat + 1}..{NARR_ROUNDS} 轮同宽，"
          "而整表仍在长）",
          len({r["win_msgs"] for r in tail_w}) == 1
          and w[i_last]["len"] > w[i_sat]["len"],
          f"窗口条数 {[r['win_msgs'] for r in tail_w]}；"
          f"整表 {[r['len'] for r in tail_w]}")
    check("窗口臂：饱和之后 narrator 段字符数**不随轮次增长**（极差 < 单轮注入的量级）",
          max(r["chars_win"] for r in tail_w) - min(r["chars_win"] for r in tail_w) < 250,
          f"字符数 {[r['chars_win'] for r in tail_w]}"
          f"（极差 {max(r['chars_win'] for r in tail_w) - min(r['chars_win'] for r in tail_w)}）")
    check("整表在同样这几轮里**仍在长**（窗口的平不是「整表也平」造成的）",
          w[i_last]["chars_whole"] - w[i_sat]["chars_whole"] > 2000,
          f"整表字符数 {[r['chars_whole'] for r in tail_w]}"
          f"（涨了 {w[i_last]['chars_whole'] - w[i_sat]['chars_whole']}）")
    check("窗口臂：历史段的工具帧被丢掉（`_build_messages` 从不注入帧，形状对齐）",
          w[i_last]["frames_win"] == 0 and n[i_last]["frames_win"] > 0,
          f"窗口臂末轮窗口内帧 {w[i_last]['frames_win']}；"
          f"整表里帧 {n[i_last]['frames_win']}")
    check("反向对照：**关掉闸门**时窗口恒等（证明它确实什么都没做）",
          all(r["ident"] for r in n),
          f"identity = {[r['ident'] for r in n]}")
    check("反向对照：**关掉闸门**必须复现线性膨胀（每轮都比上一轮长）",
          all(n[i]["chars_win"] < n[i + 1]["chars_win"] for i in range(NARR_ROUNDS - 1)),
          f"第 2 轮 {n[1]['chars_win']} → 第 {i_last + 1} 轮 {n[i_last]['chars_win']}")
    check("反向对照：末轮字符数远大于窗口臂（膨胀真的被闸门挡住了）",
          n[i_last]["chars_win"] > 5 * w[i_last]["chars_win"],
          f"无窗口 {n[i_last]['chars_win']} vs 窗口 {w[i_last]['chars_win']}"
          f"（{n[i_last]['chars_win'] / w[i_last]['chars_win']:.1f}×）")

    print()
    print(f"结论（批 3）：**有闸门 ⇒ narrator 的提示词从第 {i_sat + 1} 轮起封顶；关掉闸门 ⇒ "
          "逐轮线性膨胀。** 反向对照那一臂不是恒绿的，所以前一格不是判据自己跟自己玩。")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="原生 checkpoint 实验读数仪器")
    ap.add_argument("--turn-window", action="store_true",
                    help="本轮窗口的正/反向对照（有窗口读对本轮页面 / 关掉窗口复现错读）")
    ap.add_argument("--narrator-window", action="store_true",
                    help=f"narrator 消息窗口的正/反向对照（{NARR_ROUNDS} 轮同一线程："
                         "有闸门提示词封顶 / 关掉闸门复现线性膨胀）")
    ap.add_argument("--deterministic", action="store_true",
                    help="零 LLM：真 saver + 真读端，证「恢复发生了、读端不知道本轮到哪儿」")
    ap.add_argument("--keep-thread", action="store_true",
                    help="不复位探针 thread（演示 checkpoint 跨进程存活）")
    ap.add_argument("--history-dup", type=int, default=0, metavar="N",
                    help="每轮注入 N 条历史 ⇒ 数它被 reducer 复制了几份")
    ap.add_argument("--crash-resume", action="store_true",
                    help="崩在节点中途 ⇒ 恢复整节点重跑 ⇒ 写操作执行两次")
    ap.add_argument("--guard", action="store_true",
                    help="配 --crash-resume / --concurrency：账上场（崩在占位后 / N 个进程抢同一个键）")
    ap.add_argument("--guard-slow", action="store_true",
                    help="配 --concurrency：工具在占位之后停 2s ⇒ 后到的必读 in-doubt"
                         "（量「正在进行中也压得住」那一半）")
    ap.add_argument("--guard-unsafe", action="store_true",
                    help="上两条的反控（两格各一个：崩档 = 占位不提交 ⇒ 回到两次；"
                         "并发档 = 占位判定与插入拆成两句 ⇒ 回到 N 次）")
    ap.add_argument("--prod-shape", action="store_true",
                    help="配 --crash-resume --guard：恢复那一次传 **dict 输入**（= 生产"
                         "每轮的形状）。20261019 批 F 起读数 1×（改前 2×：账恒不命中）。"
                         "两格反控 = 同夹具不带本开关（1×）+ 干净轮之后重复同一件写（2×）")
    ap.add_argument("--concurrency", type=int, default=0, metavar="N",
                    help="同一 thread 上 N 个进程并发写（生产 4 worker 那一格）；配 --guard "
                         "则改成 N 个进程**同时重放同一个未完成的 superstep**")
    ap.add_argument("--pg-stats", action="store_true", help="实验库现状")
    ap.add_argument("--backend-parity", action="store_true",
                    help="同一份写序列走两档账本（PG / SQLite）⇒ 读数必须逐字相等")
    ap.add_argument("--summarize", metavar="PROBE_JSONL", help="汇总探针 JSONL")
    args = ap.parse_args()
    if args.turn_window:
        return cmd_turn_window()
    if args.narrator_window:
        return cmd_narrator_window()
    if args.deterministic:
        return cmd_deterministic(keep_thread=args.keep_thread)
    if args.history_dup:
        return cmd_history_dup(args.history_dup)
    if args.crash_resume:
        if args.guard or args.guard_unsafe or args.prod_shape:
            return cmd_crash_resume_guard(unsafe=args.guard_unsafe,
                                          prod_shape=args.prod_shape)
        return cmd_crash_resume()
    if args.concurrency:
        if args.guard or args.guard_unsafe or args.guard_slow:
            mode = ("unsafe" if args.guard_unsafe else
                    "slow" if args.guard_slow else "on")
            return cmd_concurrency_guard(args.concurrency, mode=mode)
        return cmd_concurrency(args.concurrency)
    if args.pg_stats:
        return cmd_pg_stats()
    if args.backend_parity:
        return cmd_backend_parity()
    if args.summarize:
        return cmd_summarize(args.summarize)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
