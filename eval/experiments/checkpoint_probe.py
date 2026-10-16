# -*- coding: utf-8 -*-
"""原生 checkpoint 实验的读数仪器（`exp/native-checkpoint` 分支专用）。

三个子命令，各自回答一个问题：

    --deterministic   线程一复用，`state["messages"]` 累积之后，**读端**会读出
                      什么？（零 LLM、零采样、可重复 ⇒ 这是最硬的一格证据）
    --crash-resume    崩在"一个节点中途"时，恢复会从哪儿重放？——**整节点重跑**，
                      所以写操作**会执行两次**（安全相关，见那里的注）
    --pg-stats        实验库里有什么（逐 thread 的 checkpoint 行数/字节、单行大小）
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
（`context._page_ctx` / `context._has_frames` / `context._frame_texts` /
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

from agent import context  # noqa: E402
from agent import graph as G  # noqa: E402
from agent.checkpoint_arm import make_saver  # noqa: E402

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
            "chars": sum(len(context._msg_text(m) or "") for m in msgs),
            # state 里累计的 ToolMessage 条数（`_frame_texts` 只取最近 limit=5 条，
            # 所以超过 5 之后条数不再涨、但字符仍涨——两列一起看才读得对）。
            "tool_msgs": sum(1 for m in msgs if isinstance(m, ToolMessage)),
            "has_frames": context._has_frames(msgs),
            # `_frame_texts` 返回的是**渲染好的字符串**（不是列表）⇒ 量它的长度。
            "frame_render": len(context._frame_texts(msgs)),
            "frame_chars": G._frame_chars(msgs),
            "page_ctx_read": context._page_ctx(msgs),
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
        copies = sum(1 for m in msgs if "HIST#" in (context._msg_text(m) or ""))
        chars = sum(len(context._msg_text(m) or "") for m in msgs)
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
        start = context.freeze_turn_start(state.get("messages"), state.get("turn_msgs"),
                                          state.get("turn_start", -1))
        # ② 本轮窗口（图内唯一的取值口，线上走 `graph._turn(state)`）
        win = context.turn_window(state.get("messages"), start)
        whole = state.get("messages") or []
        sink.append({
            "round": i, "frozen_start": start,
            "turn_msgs": state.get("turn_msgs"), "len": len(whole),
            "win_msgs": len(win), "identity": win is whole,
            # 四个读端各读**两遍**：窗口 vs 整表。两列并排才看得出这一刀在治什么。
            "page_win": context._page_ctx(win),
            "page_whole": context._page_ctx(whole),
            "frames_win": context._has_frames(win),
            "frames_whole": context._has_frames(whole),
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
#: 轮数要**明显超过** `context._NARRATOR_TURNS`（10），否则窗口一次都不触发。
NARR_ROUNDS = 12


def _narr_mini_graph(saver, sink: list):
    """与 `_windowed_mini_graph` 同构，节点里量的是**narrator 那一刀**。

    动作顺序照线上：① `planner_node` 冻结 `turn_start`；② `model_node` 取 narrator 的
    消息表（线上走 `graph._narrator_msgs(state)`，这里直接调它委派的那个纯函数
    `context.narrator_messages`——**同一个函数**，不是重写一遍）；③ 追加本轮产出。

    每轮额外记两列：`with_tool_call_pairs(win)` 的字符数（narrator 真正发出去的那一段，
    与 `graph.model_node` 量 `_prompt_chars` 同口径）与整表的同一量。两列并排才看得出
    "封顶"与"线性膨胀"。
    """

    def turn(state):
        i = state.get("plan_rounds", 0)
        msgs = state.get("messages") or []
        # ① 冻结（同源纯函数）
        start = context.freeze_turn_start(msgs, state.get("turn_msgs"),
                                          state.get("turn_start", -1))
        # ② narrator 的窗口（线上是 `graph._narrator_msgs(state)`）
        win = context.narrator_messages(msgs, start)
        win_wire = context.with_tool_call_pairs(win)
        whole_wire = context.with_tool_call_pairs(msgs)
        sink.append({
            "round": i, "frozen_start": start, "len": len(msgs),
            "win_msgs": len(win), "ident": win is msgs,
            "first_is_sys": bool(win) and context._is_system_block(win[0]),
            "chars_win": sum(len(context._msg_text(m) or "") for m in win_wire),
            "chars_whole": sum(len(context._msg_text(m) or "") for m in whole_wire),
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
    i_sat = context._NARRATOR_TURNS - 1         # 第 10 轮：历史刚超过窗口深度 ⇒ 从这轮起饱和
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
    ap.add_argument("--concurrency", type=int, default=0, metavar="N",
                    help="同一 thread 上 N 个进程并发写（生产 4 worker 那一格）")
    ap.add_argument("--pg-stats", action="store_true", help="实验库现状")
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
        return cmd_crash_resume()
    if args.concurrency:
        return cmd_concurrency(args.concurrency)
    if args.pg_stats:
        return cmd_pg_stats()
    if args.summarize:
        return cmd_summarize(args.summarize)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
