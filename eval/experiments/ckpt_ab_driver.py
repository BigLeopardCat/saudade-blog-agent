# -*- coding: utf-8 -*-
"""原生 checkpoint 实验的 A/B/D 驱动（`exp/native-checkpoint` 分支专用）。

**跑什么**：同一份 golden 语料、同一套判据，三条臂**逐用例交替**跑（消除"采样随
挂钟漂移"这一项），比对的是**逐用例/逐轮的 PASS/FAIL 差集 + resets/fallback 计数**，
不是通过率——通过率会把"B 少绿了一条、多绿了另一条"抹平成同一个数。

三条臂（见 `eval/golden_arm.py` 与 `agent/checkpoint_arm.py` 头注）：

    graph              臂 A：现状（无 saver，每请求随机线程）
    checkpoint         臂 B：持久化 saver + 线程 = 会话 id（恢复真的发生）
    checkpoint_random  反控 D：同一个 saver，线程照旧随机（只写不读）

**判读规则**（先写下来，免得事后按结果编解释）：

  · D 与 A 的差集**应当为空**（D 只多写了磁盘）。非空 ⇒ saver 本身有副作用，先查它；
  · B 与 A 的差集**就是**"复用线程"的代价/收益，与"存内存还是存磁盘"无关；
  · 三者读数完全一致时**先查臂标**（`CHECKPOINT_ARM_MARK`）——本仓吃过"两臂其实跑的
    是同一份代码"；
  · 上面两条的**前提**是"每一遍跑之前把两条 thread 都复位"（见 `reset_run_threads`）。
    不复位时 D 不是地板而是更重的处置，ab1 那张表就是这样作废的。

**跑法**（cwd 必须是主仓）：

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent
source /home/ubuntu/exp/ckpt.env
PYTHONPATH=/home/ubuntu/exp/saudade-agent-checkpoint:/home/ubuntu/exp/pydeps \\
  .venv/bin/python /home/ubuntu/exp/saudade-agent-checkpoint/eval/experiments/ckpt_ab_driver.py
```
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.dirname(os.path.dirname(HERE))          # 工作树根
MAIN = "/home/ubuntu/Saudade-Blog/saudade-blog-agent"  # 主仓（cwd + 解释器）
PYDEPS = "/home/ubuntu/exp/pydeps"
OUT = "/home/ubuntu/exp/ckpt_ab"
if PYDEPS not in sys.path:
    sys.path.insert(0, PYDEPS)


def reset_thread(thread_id: str) -> int:
    """删掉这个 thread 的 checkpoint（**跑前必须复位**）。

    为什么必须：本实验的命题就是"同一个 thread 的 checkpoint 会被读回来"——所以
    同一条用例跑第二遍时，臂 B 会**接着上一遍的 state 往下跑**（这正是生产语义：
    checkpoint 活在库里，进程重启不作废）。那会让第二次读数与第一次不可比
    （"第 1 轮"带着上一遍的消息）。每遍跑前复位 = 每个用例的每一遍都从"会话开头"
    开始，与臂 A 的语义对齐。
    """
    import psycopg
    dsn = os.environ.get("CHECKPOINT_PG_DSN") or ""
    if not dsn:
        raise SystemExit("CHECKPOINT_PG_DSN 没设（source /home/ubuntu/exp/ckpt.env）")
    n = 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        for tbl in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            n += conn.execute(f"DELETE FROM {tbl} WHERE thread_id = %s",
                              (thread_id,)).rowcount
    return n


# golden 传给图的 `thread_id` 是**常量** `"golden_thread"`（`eval/run_golden.py:452`），
# 不是生产那个每请求随机的 `user_{uid}_{hex}`（`server.py:1868`）。这是本驱动必须
# 知道的一条事实，因为三条臂对它的处置不同：
#
#   · 臂 A 无 saver ⇒ 常量线程是个死参数，接不接都无所谓；
#   · 臂 B 有 `conversation_id` 时改写成 `conv_<id>`，**缺席时退回这个常量**；
#   · 反控 D 永远用它（D 的语义就是"照旧用原 thread"）。
#
# **不复位它的后果是静默的**：第一次跑（ab1）就踩到了——反控 D 的 `msgs` 一路涨到
# 93，而本轮用例注入的历史最多 2 条。也就是说 D 在**整个库的历史**上接着往下跑
# （跨用例、跨遍、跨整轮实验），于是"D 是噪声地板"这句话不成立：D 变成了比 B 更重
# 的处置，ab1 那张"D 差 4 条 / B 差 2 条"的表**作废**（那不是噪声，是更强的处理）。
# 教训与 `eval/experiments/react_line_ab.py` 同源：**先写下来的判读规则，要先证明
# 它对得上自己造出来的那个环境**。
GOLDEN_THREAD = "golden_thread"


def reset_run_threads(conv) -> dict:
    """跑一条用例**之前**要复位的那些 thread（**两处**，缺一个就是假读数）。"""
    out = {}
    for t in ([GOLDEN_THREAD] + ([f"conv_{conv}"] if conv else [])):
        n = reset_thread(t)
        if n:
            out[t] = n
    return out


def pg_stats() -> dict:
    """实验库现状（行数 + 字节），用来量"写入放大"。"""
    import psycopg
    dsn = os.environ.get("CHECKPOINT_PG_DSN") or ""
    with psycopg.connect(dsn, autocommit=True) as conn:
        out = {}
        for tbl in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            r = conn.execute(f"SELECT count(*) AS n, "
                             f"coalesce(sum(pg_column_size(t.*)), 0) AS b FROM {tbl} t"
                             ).fetchone()
            out[tbl] = {"n": r[0], "bytes": r[1]}
        r = conn.execute("SELECT count(DISTINCT thread_id) FROM checkpoints").fetchone()
        out["threads"] = r[0]
    return out

ARMS = ("graph", "checkpoint", "checkpoint_delta", "checkpoint_random")
ARM_CN = {"graph": "A(现状)", "checkpoint": "B(saver+会话线程)",
          "checkpoint_delta": "C(停止注入历史)",
          "checkpoint_random": "D(saver+随机线程)"}

# 多轮族里**可跑**的那 5 条（另外 5 条 `needs_real_write` ⇒ 没有 GOLDEN_ALLOW_REAL_WRITE
# 就被跳过，那 5 条会真的写生产库，本轮**不碰**）。
MULTI = ["task_state_resume_settle", "mt2_card_then_cancel_no_write",
         "mt2_short_yes_resubmits_card", "mt3_detour_then_return_ledger_value",
         "admin_tag_attach_stale_pick_out_of_range"]
# 单轮对照（会话 id 缺席 ⇒ B 退回随机线程 ⇒ **B/D/A 应当逐字相同**）。
# 这一组是"差异确实来自线程、而不是来自 saver"的第二重证据。
SINGLE = ["nav_article_target", "article_read_current_eval", "nav_page_state_consistent",
          "guestboard_talk_double_source", "exec_memory_none_honest",
          "data_devices_online", "dark_state_consistent", "zako_hello_zero_tool"]


def _reports(arm: str) -> str:
    return os.path.join(MAIN, "eval/report/runs" if arm == "graph"
                        else f"eval/report/runs_{arm}")


#: 单条用例的挂钟上限。**必须比 `settings.llm_timeout`（120s）宽裕得多**（一条多轮用例
#: 是 3 轮 × 若干次调用），但**必须有**：20261016 试跑实测，服务商把连接半关（socket 停在
#: CLOSE-WAIT）而客户端不返回 ⇒ 子进程挂满 15 分钟。那时 `subprocess.run` 抛
#: `TimeoutExpired`，而 `run_case` 没接 ⇒ **整个驱动带崩**，前面跑完的用例读数一起丢。
#: 一份读数因为"某一条卡住"而全废，是本仓最不划算的一类失败。
CASE_TIMEOUT = 900


def run_case(arm: str, case: str, probe_dir: str, rep: int,
             prod_history: bool = False) -> dict:
    """跑一条用例的一条臂，返回报告里那条 case（含逐轮）。

    `prod_history`（批 3）：把 `GOLDEN_PROD_HISTORY=1` 透给子进程——多轮用例第 i>1 轮
    的 `history` 由**上一轮真发出去的那两句**合成（Rust `history[-20:]` 的形状）。
    **没有它这一格，臂 C 与臂 B 在今天的语料上逐字节相同**（多轮用例一条都不注入
    history）⇒ 会读成"停止注入没用"。四臂必须跑**同一份**驱动（要么都开、要么都不开）。
    """
    before = set(glob.glob(os.path.join(_reports(arm), "*.json")))
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": f"{WT}:{PYDEPS}",
        "GOLDEN_ARM": arm,
        "GOLDEN_ADMIN_UID": "721",
        "GOLDEN_USER_UID": "722",
        # **逐 (臂, 用例, 遍) 一个文件**：一份大 JSONL 分不出哪几行是哪条用例的
        # （读数里没有用例 id，只有 pid）。文件名就是坐标。
        "CHECKPOINT_PROBE_OUT": os.path.join(
            probe_dir, f"{arm}__{case}__r{rep}.jsonl"),
    })
    if prod_history:
        env["GOLDEN_PROD_HISTORY"] = "1"
    t0 = time.time()
    # **跑工作树的 run_golden.py**（不是主仓那份）：`run_golden.py` 自己
    # `sys.path.insert(0, "eval")` 是**相对 cwd** 的，而 cwd 必须是主仓（`.env` 是
    # 相对路径）；脚本路径给绝对路径才能让 `sys.path[0] = <工作树>/eval` 先命中
    # ——否则 `import golden_arm` 拿到主仓那份，臂名 `checkpoint` 当场不认识。
    # 三条臂走的是**同一份** run_golden/eval 判据，所以比对仍然同源。
    try:
        p = subprocess.run(
            [os.path.join(MAIN, ".venv/bin/python"),
             os.path.join(WT, "eval/run_golden.py"), "--only", case],
            cwd=MAIN, env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        # **超时不是"这一条跑得慢"，是"这一条没有读数"**：如实记 FAIL 并接着跑下一条，
        # 绝不带崩整轮（见 `CASE_TIMEOUT`）。子进程已被 `run()` 杀掉，但它的输出只存在于
        # 异常对象上——`e.stdout/e.stderr` 可能是 None（`text=True` 下通常是 str），
        # 所以要挡一层。
        tail = (e.stderr or e.stdout or "")
        tail = tail[-400:] if isinstance(tail, str) else repr(tail)[-400:]
        return {"id": case, "ok": False, "error": f"超时（>{CASE_TIMEOUT}s）",
                "fails": [f"子进程挂过 {CASE_TIMEOUT}s 被杀；stderr 尾部：{tail}"],
                "elapsed": round(time.time() - t0, 1), "rounds": []}
    elapsed = round(time.time() - t0, 1)
    new = sorted(set(glob.glob(os.path.join(_reports(arm), "*.json"))) - before)
    if not new:
        return {"id": case, "ok": False, "error": "没有产出报告",
                "fails": [f"没产出报告；stderr 尾部：{p.stderr[-400:]}"],
                "elapsed": elapsed, "rounds": []}
    rep = json.load(open(new[-1], encoding="utf-8"))
    case_rep = next((c for c in rep["cases"] if c["id"] == case), None)
    if case_rep is None:
        return {"id": case, "ok": False, "error": "报告里没有这条用例",
                "fails": [f"报告里没有 {case}"], "elapsed": elapsed, "rounds": []}
    case_rep["wall"] = elapsed
    return case_rep


#: 主判据要读的那几格（`agent/_ckpt_probe.py` 落的行）。**本批的命题是结构性的**
#: （"谁在持有上下文"），效应量是**计数级**的 ⇒ 这张表是主判据，PASS/FAIL 只是护栏。
#: `model.narr_ident`（批 3）= 窗口没动手；`planner.ident` = 本轮窗口就是整表。
_PROBE_COLS = ("msgs", "frames", "narr_ident", "prompt_chars")
_PLANNER_COLS = ("msgs", "has_frames", "ident", "start")


def read_probe(path: str) -> list:
    """一份 jsonl ⇒ **按文件顺序**（= 挂钟顺序）的行列表。

    ⚠️ **不要按 site 分桶再按下标对齐**：一次 planner 调用与一次 model 调用**不是一一对应**
    （planner ⇄ execute 会重入，实测一条 3 轮用例有 5 次 planner、3 次 model）。第一版就是
    分桶后按 `i` 对齐，于是把第 2 次 planner 和第 2 次 model 摆在同一行——**看起来像同一轮**，
    而它们中间隔着一整轮。文件顺序本身就是时间序（`_ckpt_probe.dump` 逐行 append），照它印。
    """
    out: list = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001 —— 读数坏了不该把驱动弄死
            continue
        out.append(rec)
    return out


def probe_table(probe_dir: str, cases: list, arms: list, reps: int) -> None:
    """探针计数表：逐 (用例, 臂, 遍) 按**时间序**印出每一次上下文组装的规模。

    这一节是批 3的主判据（见 `_PROBE_COLS` 的注）。四条预期（先写下来，免得事后按结果
    编解释）：

      · **A 与 D**：`narr_ident`/`ident` 恒 True、`msgs` 逐轮小 ⇒ 不窗口化也不需要；
      · **B**：`msgs` 随轮次**超线性**涨（注入的历史被 reducer 再收一遍）、第 2 轮起
        `has_frames` 恒 True、`prompt_chars` 涨——"复用线程 + 仍注入"的代价；
      · **C**：`narr_ident` 第 2 轮起 False、`msgs` 只随**本轮**长 ⇒ 闸门真开火；
      · **C vs B 的差**恰好是**注入进去的那一对**（恒等格让第 1 轮逐格相同）。

    读数**先写下来**再跑，跑完照它判——这是本仓反复付过学费的那条（见 `ab1` 那张作废的表）。
    """
    print("\n" + "=" * 104)
    print("探针计数表（**主判据**）：planner 看几条 / narrator 看几条 / 提示词多大（按时间序）")
    print("=" * 104)
    for case in cases:
        print(f"\n{case}")
        for arm in arms:
            for rep in range(1, reps + 1):
                rows = read_probe(os.path.join(probe_dir, f"{arm}__{case}__r{rep}.jsonl"))
                print(f"   {ARM_CN.get(arm, arm):<20} r{rep}  （{len(rows)} 次组装）")
                for k, r in enumerate(rows, 1):
                    if r.get("site") == "model":
                        print(f"        {k:>2} model   msgs={r.get('msgs'):<3} "
                              f"帧={r.get('frames'):<3} 窗口没动手={r.get('narr_ident')!s:<5} "
                              f"提示词={r.get('prompt_chars')}")
                    else:
                        print(f"        {k:>2} planner msgs={r.get('msgs'):<3} "
                              f"有帧={r.get('has_frames')!s:<5} "
                              f"本轮=整表={r.get('ident')!s:<5} "
                              f"起点={r.get('turn_start')} 本轮几条={r.get('turn_msgs')}")


def _round_view(r: dict) -> dict:
    """逐轮的可比视图（判据 = `landing_gate.fallback_resets` 的口径：scope=text）。"""
    scopes = r.get("reset_scopes") or []
    return {
        "round": r.get("round"),
        "resets": r.get("resets") or 0,
        "fb": sum(1 for s in scopes if s == "text"),
        "tools": list(r.get("tool_calls") or []),
        "exec": list(r.get("exec_tools") or []),
        "chars": len(r.get("text") or ""),
    }


# **"判出来的东西"**（决定差集的那几格）。`chars` 故意不在里面：叙述文本长度每跑
# 都不一样（采样），把它算进差集等于让每条用例都"有差异"、差集失去信息。
# 它仍然逐轮打印（膨胀趋势要看），只是不参与判定。
_JUDGED = ("round", "resets", "fb", "tools", "exec")


def _judged(rv: dict) -> tuple:
    return tuple(str(rv.get(k)) for k in _JUDGED)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="", help="逗号分隔的用例 id（缺省 = 多轮族 + 单轮对照）")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--no-reset", action="store_true",
                    help="不复位会话 thread（演示 checkpoint 跨遍存活）")
    ap.add_argument("--reps", type=int, default=1,
                    help="每条用例每臂跑几遍（判据是多遍 A/B，不是一遍）")
    ap.add_argument("--prod-history", action="store_true",
                    help="GOLDEN_PROD_HISTORY=1：多轮用例第 i>1 轮的 history 按生产形状"
                         "合成（见 run_golden.ENV_PROD_HISTORY）——**没有它臂 C ≡ 臂 B**")
    args = ap.parse_args()

    cases = ([c.strip() for c in args.cases.split(",") if c.strip()]
             or MULTI + SINGLE)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    probe_dir = os.path.join(OUT, args.tag)
    os.makedirs(probe_dir, exist_ok=True)
    print(f"[ab] tag={args.tag}  arms={arms}  cases={len(cases)}")
    print(f"[ab] 读数落点 {probe_dir}/")
    print(f"[ab] 库现状（跑前）：{pg_stats()}")

    # 用例 id → 会话 id（臂 B 的 thread 就是它）。单轮用例没有 conversation_id
    # ⇒ 臂 B 退回随机线程，与臂 D 同形（见 `checkpoint_arm._thread_for`）。
    conv_of = {}
    for line in open(os.path.join(MAIN, "eval/golden/basic.jsonl"), encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        conv_of[c["id"]] = (c.get("context") or {}).get("conversation_id")

    results: dict[str, dict[str, list]] = {}
    # **逐用例交替 + 逐遍交替**：外层遍、再用例、再臂 ⇒ 同一遍里三条臂挨着跑，
    # 采样窗口最接近（判据是多遍 A/B，不是一遍）。
    for rep in range(1, args.reps + 1):
        for i, case in enumerate(cases, 1):
            results.setdefault(case, {})
            for arm in arms:
                # **每条臂每一遍都复位**（不再只复位 B/D）：`golden_thread` 是三条臂
                # 共用的那个常量线程，A 臂虽然不写它，但上一遍 D 写进去的行会留在库里
                # 影响下一遍 D ⇒ 复位必须发生在**每一次跑之前**，与臂无关。
                if not args.no_reset:
                    n = reset_run_threads(conv_of.get(case))
                    if n:
                        print("        （复位 " + "、".join(
                            f"{t}：删 {c} 行" for t, c in n.items()) + "）", flush=True)
                pg0 = pg_stats()
                r = run_case(arm, case, probe_dir, rep, prod_history=args.prod_history)
                pg1 = pg_stats()
                # 这一遍往库里写了多少（写入放大，§4.7 ③ 那一格）
                r["pg_delta"] = {k: (pg1[k]["n"] - pg0[k]["n"],
                                     pg1[k]["bytes"] - pg0[k]["bytes"])
                                 for k in ("checkpoints", "checkpoint_writes",
                                           "checkpoint_blobs")}
                r["rep"] = rep
                results[case].setdefault(arm, []).append(r)
                flag = "PASS" if r.get("ok") else "FAIL"
                fb = sum(_round_view(x)["fb"] for x in (r.get("rounds") or []) if x)
                d = r["pg_delta"]
                print(f"[r{rep} {i:>2}/{len(cases)}] {case:<44} "
                      f"{ARM_CN.get(arm, arm):<20} {flag} {r.get('elapsed')}s "
                      f"resets={r.get('resets')} fb={fb} "
                      f"写库={d['checkpoints'][0]}行/{d['checkpoints'][1]}B"
                      f"{'  ' + str(r.get('fails'))[:70] if not r.get('ok') else ''}",
                      flush=True)

    # ── 汇总：逐用例 × 逐臂的 PASS 计数 + 逐轮判据序列 ──────────────────
    print("\n" + "=" * 104)
    print("逐用例 × 逐臂（每臂 N 遍的 PASS 数 / N；末列 = 各遍逐轮判据序列）")
    print("=" * 104)
    hdr = "  ".join(f"{ARM_CN.get(a, a)[:4]:>10}" for a in arms)
    print(f"{'用例':<46} {hdr}")
    for case in cases:
        cells = []
        for arm in arms:
            rs = results[case][arm]
            p = sum(1 for r in rs if r.get("ok"))
            cells.append(f"{p}/{len(rs)}")
        print(f"{case:<46} " + "  ".join(f"{c:>10}" for c in cells))
        # 逐轮判据（只有多轮用例有意义）：把每遍每轮的 judged 元组列出来
        for arm in arms:
            sigs = []
            for r in results[case][arm]:
                sigs.append("".join("." if (x.get("resets") or 0) == 0
                                    else f"R{x['resets']}"
                                    for x in [_round_view(y) for y in
                                              (r.get("rounds") or []) if y])
                            or "-")
            uniq = {}
            for s in sigs:
                uniq[s] = uniq.get(s, 0) + 1
            print(f"      {ARM_CN.get(arm, arm):<22} 逐轮签名 {uniq}")

    # ── 主判据：探针计数表 ──────────────────────────────────────────
    probe_table(probe_dir, cases, arms, args.reps)

    # ── 噪声地板 vs 臂间差（这是本实验唯一站得住的判读方式）────────────
    print("\n" + "=" * 104)
    print("噪声地板（反控 D 与 A 的差 = **结构上不可能有差异**的两臂之差）")
    print("=" * 104)
    for case in cases:
        a = [bool(r.get("ok")) for r in results[case].get("graph", [])]
        d = [bool(r.get("ok")) for r in results[case].get("checkpoint_random", [])]
        b = [bool(r.get("ok")) for r in results[case].get("checkpoint", [])]
        c = [bool(r.get("ok")) for r in results[case].get("checkpoint_delta", [])]
        if not (a and d):
            continue
        # 四条都同 ⇒ 这一条没话可说（省掉满屏同形行）。**C 也算进来**：本批的判决是
        # 「C vs A 差集为空」，一条只在 C 上分叉的用例必须印出来。
        if a == d == b == c:
            continue
        print(f"  {case:<44} A={a}  D={d}  B={b}  C={c}")

    stats = pg_stats()
    print(f"\n[ab] 库现状（跑后）：{stats}")
    with open(os.path.join(probe_dir, "ab_results.json"), "w", encoding="utf-8") as f:
        json.dump({"results": results, "pg_after": stats}, f, ensure_ascii=False, indent=1)
    print(f"[ab] 明细已写 {probe_dir}/ab_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
