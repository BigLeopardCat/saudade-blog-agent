# -*- coding: utf-8 -*-
"""参数矩阵：把"换个模型 / 换组参数"跑成**可比的臂**，并出一张表（20261006）。

## 为什么要有它

「不同模型、不同参数配置的调优实验」在本仓此前**没有载体**：`eval/run_golden.py`
只有一条配置（读 `.env`），要比较就得**改代码再跑**——而本仓的纪律是"换臂＝两臂
交替读计数"，改代码跑矩阵等于每臂一次提交，既不可比（中间夹着别的改动）也留不下
读数。`eval/golden_arm.py` 的 `GOLDEN_ARM` 是**另一回事**：它换的是"哪一套循环"
（graph / ReAct），要新建 `agent.<臂>_arm` 模块；本模块换的是**同一套循环的旋钮**，
所以走**环境变量覆盖**（pydantic-settings 里环境变量优先于 `.env`）。

## 两条纪律（都是本仓有前科的）

  · **一臂最少两遍**。同一份代码实测过 10 红 vs 3 红（`20261005_234824` vs
    `20261006_002223`）——单跑读数**不可判读**。`--reps` 默认 2，`--reps 1` 只许
    用来验管线（那次的表里会带 `single=1` 记号，不许当结论）。
  · **读数记计数、不记通过率**。表里给的是每遍的红数、红名单、以及**路由不稳
    用例数**；不给"通过率提升 X%"这类聚合差——聚合没退化 ≠ 没有一条变坏
    （`landing_gate --ab` 的 `per_case_rose` 是同一课）。

## 用法

```bash
# 跑两臂、每臂两遍（真链路，一轮 ≈20–25 分钟；中途别开第二轮）
.venv/bin/python eval/param_matrix.py --arms t0.0 t0.2 --reps 2
# 看表（只读 jsonl，不跑）
.venv/bin/python eval/param_matrix.py --report
```

每跑完一遍就**追加**一行到 `eval/report/param_matrix.jsonl`（append-only：中断了
再跑，前面那几遍的读数还在，不用重跑——与 `runs/*.json` 的 `O_EXCL` 同一条纪律）。

表里除红数/下界/延迟外还有**两列用量**（「输入tok」「命中率」）：按每遍的 `trace_run`
走 `eval/token_cost_report.py` 那**一个**聚合实现算（扫描只有一份实现），**没量到是
`—` 不是 `0`**；本列上线前写的老行在**读取端**按 `trace_run` 补，jsonl 一个字不改。
离线锁见 `tests/test_param_matrix_tokens.py`。

## 它**不**做什么

不写 `last_run.json`、不碰 `eval/golden/**`、不动 `TARGET/FLOOR/ENTRY`、不动分母。
判据的变更仍归 `eval/run_golden.py` 那一条路，本模块只**调旋钮、读数**。

⚠️ 上面那一条（不写 `last_run.json`）**在 20261006 之前是句空话**：本模块当时不给子进程
设 `GOLDEN_ARM`，于是 `golden_arm.arm_name()` 恒为 `graph` ⇒ `reports_dir` 恒为
`eval/report/runs`（每一遍都落进**生产档那个目录**）、`is_baseline_arm` 为真
（写 `last_run.json` 的资格是开着的）。**实测的后果只有前半截**：那 9 遍留档确实混进了
生产档目录，`landing_gate` 的 readiness / 慢性红榜按目录整扫 ⇒ 把温度臂、deepseek 臂的
读数（含两份 `ds-chat`，下界 0.643 / 0.635）**当成生产档的夜间读数**收进窗口。
后半截**没真的发生**：`last_run.json` 一次都没被覆盖——那 9 份报告的 `full_run` 全是
`False`（有 env 跳过 ⇒ `run_golden.is_full_run` 假，而写基线还有那道闸），**闸开着、
恰好没响**。别把"没响"读成"设计对了"：它与本仓今天另外两处是同一个形状。
现在本模块跑的是 `matrix` 臂（`golden_arm.ARM_MATRIX`：自己的目录
`eval/report/runs_matrix`、没有基线资格），那句话才**变成真的**。
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import golden_arm  # noqa: E402  （臂名/目录的**单一事实源**，别在这里再写一遍）
# 用量与枚举**各只有一个实现**：本模块只**调用**它们、不自己扫 trace（`trace_files` 是
# 枚举的唯一入口；`token_cost_report.totals` 是用量聚合的唯一入口——命中率的分母口径
# 就在那儿，见该函数 docstring）。
import token_cost_report  # noqa: E402
import trace_files  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# 留档目录与"能不能当基线"**同源**取（`golden_arm`）：本模块是 `matrix` 臂 ⇒ 目录是
# `eval/report/runs_matrix`，与生产档的 `runs/` 分开。
RUNS_DIR = ROOT / golden_arm.reports_dir(golden_arm.ARM_MATRIX)
MATRIX = ROOT / "eval" / "report" / "param_matrix.jsonl"
TRACE_ROOT = Path("/home/ubuntu/Saudade-Blog/logs/agent/golden_traces")

# 夜间那台用的身份变量。**必须带**：不带就只有 138/119 的分母，
# 与历史读数不可比（见 plan 的一、与 `scripts/nightly_regression.sh`）。
IDENTITY_ENV = {"GOLDEN_ADMIN_UID": "721", "GOLDEN_USER_UID": "722"}

# ── 臂的定义 ────────────────────────────────────────────────────────────
# 每个臂 = 一组**环境变量覆盖**。`why` 写清这个臂在问什么问题——一张没有问题的
# 表只会变成读数坟场。
ARM_SPECS: dict[str, dict] = {
    "live": {
        "title": "生产档（不改任何旋钮）",
        "env": {},
        "why": "对照臂：线上正在跑的那一套。任何结论都要对它说话。",
    },
    "t0.0": {
        "title": "规划温度 0.0（确定性档）",
        "env": {"PLANNER_TEMPERATURE": "0.0"},
        "why": "路由确定性的靶子：8 跑窗口里 134 条有 41 条换过 round 0 技能，"
               "其中 27 条**输入逐字相同**却换分支 ⇒ 纯采样，温度是唯一能治它们的旋钮。",
    },
    "t0.2": {
        "title": "规划温度 0.2（20261006 之前写死的值）",
        "env": {"PLANNER_TEMPERATURE": "0.2"},
        "why": "回退臂：把历史行为逐字节复现出来，作为 t0.0 的 A 侧。",
    },
    "think": {
        "title": "规划开思考（PLANNER_NATIVE_THINKING=1）",
        "env": {"PLANNER_NATIVE_THINKING": "1"},
        "why": "线上是关着思考跑的（native 三项之一，settings 注里写明是待拍板项）。"
               "开它买的是「分类更准」，付的是 max_tokens 1200 里思考链先吃掉一截。",
    },
    "ali-ds": {
        "title": "换模型：DeepSeek（阿里 API + QWEN_MODEL=deepseek-v4.1-flash）",
        "env": {"QWEN_MODEL": "deepseek-v4.1-flash"},
        "why": "跨模型那一格**正确的走法**（主人 20261006 指正）：阿里那套 API 的同一个 "
               "base_url/key 上就有 deepseek 档，换的只是**模型名**——不用切 `LLM_PROVIDER`。"
               "下面 `ds-chat` / `ds-flash` 两条走的是 DeepSeek **官方端点**（`DEEPSEEK_BASE_URL`），"
               "那是另一条通路（官方推理档要求回传 `reasoning_content`，本仓的回填器不填 ⇒ 必 400）。"
               "**那两条读数不能拿来回答「这个栈换 deepseek 行不行」**——型号对、通路不对。",
    },
    "ds-chat": {
        "title": "换模型：DeepSeek（**官方端点**，LLM_PROVIDER=deepseek，deepseek-chat）",
        "env": {"LLM_PROVIDER": "deepseek", "DEEPSEEK_MODEL": "deepseek-chat"},
        "why": "⚠️ 走的是 DeepSeek 官方端点、**不是**本栈换 deepseek 的走法（见 `ali-ds`）。"
               "留着是当负控的反面：同一个模型名换到官方通路上会成片地红。"
               "**模型名不能省**：settings 里配的 `deepseek-flash` "
               "是推理模型，一跑就 400 `The reasoning_content in the thinking mode "
               "must be passed back to the API`——本仓的 `with_tool_call_pairs` 只回填 "
               "tool_calls 的配对，不回填 reasoning_content，所以那条是**代码层不兼容**"
               "（预检实测 0/2），不是旋钮能拨的。改用非推理的 deepseek-chat。",
    },
    "ds-flash": {
        "title": "换模型：DeepSeek 推理档（**官方端点**，deepseek-flash，已知不可跑）",
        "env": {"LLM_PROVIDER": "deepseek"},
        "why": "留在这里是当**负控**：它每次都会以同一个 400 立刻失败。谁要是把"
               "「换个模型」当成纯配置动作，跑这个臂是最快的反例。"
               "⚠️ 这个 400 是**官方端点**的回填契约，不是「deepseek 换不了」——"
               "走阿里 API 换模型名的那条路见 `ali-ds`。",
    },
}

REDACTED = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "DSN", "DATABASE_URL")


def _env_for(arm: str) -> dict:
    env = dict(os.environ)
    env.pop("SAUDADE_IGNORE_ENV_FILE", None)      # 真链路**绝不能**带这一条
    for k in list(env):                            # 别让外层壳的旋钮渗进臂里
        if k.startswith(("PLANNER_", "LLM_PROVIDER", "LLM_SEED", "GOLDEN_")) \
                or k.endswith("_MODEL"):
            env.pop(k, None)
    env.update(IDENTITY_ENV)
    env.update(ARM_SPECS[arm]["env"])
    # ⚠️ **必须在上面那个 `GOLDEN_` 清除循环之后**（它也清 `GOLDEN_ARM`——那是刻意的：
    # 外层壳里若设着别的臂，不许渗进来）。这一句就是"调参档不写 last_run.json、
    # 也不落进生产档那个目录"的实现处：臂名不同 ⇒ `is_baseline_arm` 假、目录另开。
    env[golden_arm.ENV_ARM] = golden_arm.ARM_MATRIX
    return env


def _arm_banner(arm: str, rep: int, reps: int) -> str:
    spec = ARM_SPECS[arm]
    ov = " ".join(f"{k}={v}" for k, v in spec["env"].items()) or "(无覆盖)"
    return f"\n{'=' * 78}\n臂 {arm}（{spec['title']}）第 {rep}/{reps} 遍\n  覆盖: {ov}\n{'=' * 78}"


def _new_report(before: set[str]) -> Path | None:
    now = set(glob.glob(str(RUNS_DIR / "*.json")))
    new = sorted(now - before)
    return Path(new[-1]) if new else None


# 用量行的键：一处定义，`_row` 与报表都从这里取（缺哪一格就是 `None`，见下）。
_TOKEN_KEYS = ("token_traces", "llm_calls", "input_tok", "output_tok",
               "cache_hit_tok", "cache_seen", "cache_hit_rate")


def _token_usage(trace_run: str | None, root: Path | None = None) -> dict:
    """一遍全量 golden 的**用量与缓存命中率**，落在这一行的 `_TOKEN_KEYS` 上。

    **换模型那一格为什么必须带它**：换端点后缓存行为**可能整体变**，而缓存是成本的唯一
    大项（输入:输出 ≈ 173:1，见 `docs/param-tuning-20261006.md` §八.2）——一张只报红数的
    表会把"便宜了一半"和"贵了一倍"读成同一件事。

    **扫描不在这里实现**：走 `token_cost_report.totals`（那是唯一的用量聚合实现），
    本函数只做"目录在哪、有没有"这件事。**缺席 ≠ 0** 是本仓反复踩到的形状，这里按
    `None` 表达：

      · `trace_run` 为空 / 目录不存在 / 目录里一份 trace 都没有 ⇒ **全部 `None`**
        （"这次没量到"，不是"这次用量是 0"）；
      · 一次调用都没报缓存字段 ⇒ `cache_hit_rate` 单独是 `None`，而 `llm_calls` 照报
        ——率的口径（分子分母各是什么、`cache_seen` 与 `llm_calls` 差在哪）见
        `token_cost_report.totals`，别在这里重算一遍。
    """
    blank = {k: None for k in _TOKEN_KEYS}
    if not trace_run:
        return blank
    d = (root or TRACE_ROOT) / trace_run
    if not d.is_dir():
        return blank
    tot = token_cost_report.totals(list(trace_files.iter_trace_files(str(d))))
    if not tot["calls"] and not tot["traces"]:
        return blank          # 目录在、但里面没有可读的 trace：仍是"没量到"
    rate = tot["hit_rate"]
    return {
        "token_traces": tot["traces"],
        "llm_calls": tot["calls"],
        "input_tok": tot["in"],
        "output_tok": tot["out"],
        # 没人报过缓存字段 ⇒ 命中 tok **也**是 `None`（不是 0）：那次扫描里这个数
        # 根本没出现过，写 0 就是在说"一次都没命中"。
        "cache_hit_tok": tot["cache"] if tot["cache_seen"] else None,
        "cache_seen": tot["cache_seen"],
        "cache_hit_rate": None if rate is None else round(rate, 4),
    }


def _row(arm: str, rep: int, rc: int, rp: Path | None) -> dict:
    spec = ARM_SPECS[arm]
    row = {"arm": arm, "title": spec["title"], "rep": rep, "rc": rc,
           "env": dict(spec["env"]), "t": time.strftime("%Y-%m-%d %H:%M:%S")}
    if rp is None:
        row["error"] = "本次没产出报告（跑挂了/被中断）"
        row.update(_token_usage(None))   # 键齐、值全 None：没量到 ≠ 用量 0
        return row
    r = json.loads(rp.read_text(encoding="utf-8"))
    samp = (r.get("landing") or {}).get("sampled") or {}
    eff = r.get("efficiency") or {}
    pe = r.get("plan_efficiency") or {}
    lat = r.get("latency_s") or {}
    row.update({
        "report": str(rp), "ts": r.get("ts"), "trace_run": r.get("trace_run"),
        "total": r.get("total"), "passed": r.get("passed"), "failed": r.get("failed"),
        "sampled_total": samp.get("total"), "sampled_passed": samp.get("passed"),
        "sampled_lower": samp.get("lower"), "sampled_point": samp.get("point"),
        "target": samp.get("target"),
        "state": samp.get("state"),
        "red_ids": samp.get("failed_ids") or [],
        "hard_ok": ((r.get("landing") or {}).get("hard") or {}).get("ok"),
        "regression_failed_ids": (r.get("regression") or {}).get("failed_ids") or [],
        "resets_total": eff.get("resets_total"),
        "tool_calls_total": pe.get("tool_calls_total"),
        "p50": lat.get("p50"), "p95": lat.get("p95"),
        "engine": r.get("engine"),
        "model": (r.get("engine"), r.get("corpus_provenance") is not None),
    })
    # 用量/缓存：**报告已带上 trace_run 之后**才能取（上面那一块里 set 的）。
    row.update(_token_usage(row.get("trace_run")))
    return row


# ── 路由稳定性：从 trace 里逐用例取 round 0 决策 ────────────────────────
def _round0_by_case(trace_dir: str) -> dict[str, tuple]:
    """`{用例 id: (skill, finish, calls)}`——只取**第一条** planner 决策事件。"""
    out: dict[str, tuple] = {}
    for f in glob.glob(os.path.join(trace_dir or "", "*.json")):
        try:
            tr = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        ev = tr.get("events") if isinstance(tr, dict) else tr
        dec = [e for e in (ev or []) if e.get("node") == "planner"
               and e.get("event") in ("decision", "native_decision")]
        if not dec:
            continue
        e0 = dec[0]
        out[Path(f).stem] = (str(e0.get("skill")), str(e0.get("finish")),
                             str(e0.get("calls") or e0.get("tools") or ""))
    return out


def _route_stats(trace_dirs: list[str], coarse: bool = False) -> dict:
    """逐用例跨运行的 round 0 决策：**成对分歧率** + 不稳用例数。

    用成对分歧率而不是"出现过几种分支"是刻意的：后者**随窗口里跑几遍而单调变大**，
    3 遍窗口与 8 遍窗口直接比就是拿尺子量两次不同长度。成对分歧率 = 分歧对数 /
    总对数，对窗口长度归一。
    """
    per_run = [_round0_by_case(td) for td in trace_dirs if td]
    per_run = [p for p in per_run if p]
    # **丢掉退化的窗口成员**（`--only` / `--limit` / 跑挂了的半截轮）：只求交集的话，
    # 一个只跑了 9 条的窗口成员能把整窗交集打到 0——那时这张表会显示"分歧 0 对"，
    # 看着像"完全确定"，实际是**没有任何一条可比**。这条是实测踩到的：8 跑窗口里
    # 混进一份 total=9 与一份无 trace_dir 的，交集从 149 掉到 0。
    # 口径：留下用例数与最大者同量级的（≥50%），丢掉的**列出来**、不静默。
    # `default=0`：**一遍可比成员都没有**时（trace 目录还在、但里面的 trace 读不出
    # planner 决策——例如已被保留期压成 gz 之外的东西，或者 `--report` 时那些 trace
    # 早没了）不许在这里炸：这张表的意义就是"读不出就如实显示读不出"，下面的
    # 「不足两遍可比的全量」分支接得住 0 与 1 两种情形。
    n_max = max((len(p) for p in per_run), default=0)
    kept = [p for p in per_run if len(p) * 2 >= n_max]
    dropped = [len(p) for p in per_run if len(p) * 2 < n_max]
    if coarse:
        # **技能级口径**：只比 round 0 落到哪个技能，不比"这一轮是怎么表达的"。
        # 这条口径是为一个实测到的假信号加的——两个臂里都有大量用例在
        # `chat|stop|空`（一个函数都没点）与 `chat|tool_calls|chat`（显式点了 chat）
        # 之间翻，**落点是同一个 chat**，用户可见行为也相同（区别只是前者会多挨
        # 一次 `no_call_nudge` 纠偏、多花一轮 LLM）。不劈开这两层，那张"路由不稳"
        # 的表里一半是记账差异，会让人以为模型的决策在乱跳。
        # ⚠️ 归一化必须作用在**过滤后**的 `kept` 上：早先写成归一化 `per_run`，
        # 退化的 9 条成员又回来了，交集被打到 9 —— 与上面那条同一个坑，换个位置又踩。
        per_run = [{k: (v[0],) for k, v in p.items()} for p in kept]
    else:
        per_run = kept
    if len(per_run) < 2:
        return {"runs": len(per_run), "note": "不足两遍可比的全量，算不出分歧率",
                "pairs": 0, "disagree_pairs": 0, "rate": None, "unstable": None,
                "cases": 0, "dropped": dropped}
    cases = set.intersection(*[set(p) for p in per_run])
    pairs = dis = 0
    unstable = []
    for c in sorted(cases):
        seen = {p[c] for p in per_run}
        k = len(per_run)
        n_pair = k * (k - 1) // 2
        d = sum(1 for i in range(k) for j in range(i + 1, k)
                if per_run[i][c] != per_run[j][c])
        pairs += n_pair
        dis += d
        if d:
            branches = sorted({p[c] for p in per_run})
            if coarse:
                unstable.append((c, [b[0] for b in branches], d))
                continue
            # 技能名相同时**分支要看得见区别**：`chat/stop/空` 与 `chat/tool_calls/chat`
            # 是两种不同的决策形状（前者"一个函数都没点"、后者"显式点了 chat"），
            # 只打技能名会显示成 `['chat'] 分歧 1 对`——看着像自相矛盾。
            if len({b[0] for b in branches}) == 1:
                shown = [f"{b[0]}|{b[1]}|{b[2] or '—'}" for b in branches]
            else:
                shown = [b[0] for b in branches]
            unstable.append((c, shown, d))
    return {"runs": len(per_run), "cases": len(cases), "pairs": pairs,
            "disagree_pairs": dis, "rate": (dis / pairs) if pairs else None,
            "unstable": len(unstable), "unstable_list": unstable,
            "dropped": dropped}


# ── 跑 ──────────────────────────────────────────────────────────────────
def run(arms: list[str], reps: int) -> int:
    for a in arms:
        if a not in ARM_SPECS:
            print(f"未知臂 {a}；可选：{', '.join(ARM_SPECS)}")
            return 2
    py = str(ROOT / ".venv" / "bin" / "python")
    # 遍号**接着已有的行往下数**：分几次调用补跑时，`rep` 才有"第几遍"的含义
    # （否则第二次调用又从 1 开始，表里两行都叫"第 1 遍"）。
    seen = Counter()
    if MATRIX.exists():
        for l in MATRIX.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    seen[json.loads(l)["arm"]] += 1
                except Exception:
                    pass
    n = 0
    for arm in arms:
        for rep in range(seen[arm] + 1, seen[arm] + reps + 1):
            print(_arm_banner(arm, rep, reps), flush=True)
            before = set(glob.glob(str(RUNS_DIR / "*.json")))
            t0 = time.time()
            rc = subprocess.call([py, "eval/run_golden.py"],
                                 cwd=str(ROOT), env=_env_for(arm))
            rp = _new_report(before)
            row = _row(arm, rep, rc, rp)
            row["wall_s"] = round(time.time() - t0, 1)
            with MATRIX.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  rc={rc} 红={len(row.get('red_ids') or [])} "
                  f"下界={row.get('sampled_lower')} 用时 {row['wall_s']}s -> {MATRIX.name}",
                  flush=True)
            n += 1
    print(f"\ndone: {n} 遍。看表：.venv/bin/python eval/param_matrix.py --report")
    return 0


# ── 报表 ────────────────────────────────────────────────────────────────
def _usage_cells(rs: list[dict]) -> tuple[str, str, str | None]:
    """一臂的「输入tok / 命中率」两格，外加需要时冒出来的一句脚注。

    三条纪律都在这里，别在别处重算：`None` 显示 `—`（**不是 0**）；命中率 = 命中 tok /
    输入 tok（与 `token_cost_report.totals` 的合计行同式）；`cache_seen < llm_calls`
    （有调用没报缓存字段）时那个率**偏低**——分子少算了没报的那些、分母照旧，所以要点名。
    """
    tin = [r.get("input_tok") for r in rs if r.get("input_tok") is not None]
    hit = [r.get("cache_hit_rate") for r in rs
           if r.get("cache_hit_rate") is not None]
    partial = [r for r in rs
               if (r.get("llm_calls") or 0) > (r.get("cache_seen") or 0)]
    note = None
    if partial:
        miss = [(r.get("llm_calls") or 0) - (r.get("cache_seen") or 0) for r in partial]
        note = (f"有 {len(partial)} 遍出现调用没报缓存字段（最多的一遍少 {max(miss)} 次；"
                f"端点只对部分请求回该字段时就是这样）⇒ 那几遍的命中率**偏低**"
                f"（分子只算了报了的那些，分母仍是输入 tok 总数）。")
    return (" / ".join(f"{x / 1e6:.2f}M" for x in tin) or "—",
            " / ".join(f"{x:.1%}" for x in hit) or "—", note)


# 两列的口径说明（表后紧跟一行）——放常量里，免得 `report()` 被几行长句撑大。
_USAGE_FOOTNOTE = (
    "\n输入tok/命中率取自每遍报告 `trace_run` 指向的 trace，走"
    "`eval/token_cost_report.py` 那**同一份**扫描器（`llm_done` 里的 "
    "`input/output` 字段）。**`—` = 没量到，不是 0。** 命中率 = 命中 tok / 输入 tok"
    "（与 `token_cost_report.totals` 的合计行同式）；一次调用都没报缓存字段时它是"
    "`—` 而不是 `0.0`。⚠️ 换模型那一格**必须**连这一列一起看：换端点后缓存行为"
    "可能整体变，而输入侧占成本 ~99%。")


def _print_red_roll(detail: list[tuple]) -> None:
    """「红名单逐遍」一节：同一臂两遍都红 ⇒ ×2（比均值有信息量）。"""
    print("\n## 红名单逐遍（去重后按出现次数）\n")
    for arm, rs, _st in detail:
        cnt = Counter(c for r in rs for c in (r.get("red_ids") or []))
        if cnt:
            print(f"- `{arm}`: " + "，".join(f"{c}×{n}" for c, n in cnt.most_common()))
        else:
            print(f"- `{arm}`: 零红")


def _print_route_roll(detail: list[tuple]) -> None:
    """「路由」一节：逐臂列出跨遍换过 round 0 技能的用例（每臂最多列 12 条）。"""
    print("\n## 路由：跨遍换过 round 0 技能的用例\n")
    for arm, rs, st in detail:
        us = st.get("unstable_list") or []
        if not us:
            print(f"- `{arm}`: ——")
            continue
        print(f"- `{arm}`（{st['unstable']}/{st['cases']}，成对分歧 {st['rate']:.1%}）")
        for c, branches, d in sorted(us, key=lambda x: -x[2])[:12]:
            print(f"    · `{c}` 分支={branches} 分歧 {d} 对")


def _print_ab_diff(detail: list[tuple]) -> None:
    """两臂时逐用例点名红→绿 / 绿→红（只看全量轮）。聚合没退化 ≠ 没有一条变坏。"""
    full = {arm: rs for arm, rs, _st in detail}
    if len(full) != 2:
        return
    a, b = list(full)
    red = {x: Counter(c for r in full[x] for c in (r.get("red_ids") or []))
           for x in (a, b)}
    print(f"\n## 逐条点名 `{a}` → `{b}`（只看全量轮）\n")
    fell = sorted(set(red[a]) - set(red[b]))
    rose = sorted(set(red[b]) - set(red[a]))
    both = sorted(set(red[a]) & set(red[b]))
    print(f"- 只在 `{a}` 红（改后转绿）：{fell or '——'}")
    print(f"- 只在 `{b}` 红（改后新红，**这条最要命**）：{rose or '——'}")
    print(f"- 两边都红：{both or '——'}")


def report(only: list[str] | None = None) -> int:
    if not MATRIX.exists():
        print("还没有读数（先跑 --arms）")
        return 1
    rows = [json.loads(l) for l in MATRIX.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = defaultdict(list)
    for r in rows:
        # 老行（本列上线前写的）没有用量键——**在读取端按 `trace_run` 补**，不改 jsonl：
        # 那些 trace 还在盘上（保留 30 天），而"这一遍花了多少"是那份 trace 的纯函数。
        if "input_tok" not in r:
            r.update(_token_usage(r.get("trace_run")))
        if only and r["arm"] not in only:
            continue
        by[r["arm"]].append(r)
    if not by:
        print("筛完没有读数")
        return 1

    print("# 参数矩阵读数\n")
    print("「形态级」= round 0 的 (技能, finish, 点了什么) 三者全比；「技能级」只比落到哪个技能。"
          "两个都要看：差在形态级、平在技能级的那些是记账差异（`chat|stop|空` vs "
          "`chat|tool_calls|chat`，落点同一个 chat），不是决策乱跳。\n")
    print("| 臂 | 遍 | 采样层红数（逐遍） | 均值 | 下界（逐遍） | 硬层 | "
          "形态级分歧 | 技能级分歧 | p50/p95 | 工具调用合计 | 输入tok | 命中率 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    detail = []
    cache_notes: list[str] = []
    for arm, rs in by.items():
        rs = sorted(rs, key=lambda r: (r.get("rep") or 0))
        # **只拿全量轮进表**：`--only` / `--limit` 的半截轮分母不同（实测混进一份
        # total=9 的，红数会显示成 1，看着像 1/9 —— 与 134 分母的数根本不是一回事）。
        # 门槛取 100：两个合法分母是 119 与 134，都比它大；半截轮都比它小。
        partial = [r for r in rs if (r.get("sampled_total") or 0) < 100]
        rs = [r for r in rs if (r.get("sampled_total") or 0) >= 100]
        if not rs:
            print(f"| `{arm}` | 0 | —— 没有全量轮（{len(partial)} 份半截轮已排除）|")
            continue
        reds = [len(r.get("red_ids") or []) for r in rs if "error" not in r]
        lows = [r.get("sampled_lower") for r in rs if r.get("sampled_lower") is not None]
        tds = [str(TRACE_ROOT / r["trace_run"]) for r in rs if r.get("trace_run")]
        st = _route_stats(tds)
        st_coarse = _route_stats(tds, coarse=True)
        hard = all(r.get("hard_ok") for r in rs) and all(
            not r.get("regression_failed_ids") for r in rs)
        mean = (sum(reds) / len(reds)) if reds else None
        p50 = [r.get("p50") for r in rs if r.get("p50") is not None]
        p95 = [r.get("p95") for r in rs if r.get("p95") is not None]
        tc = [r.get("tool_calls_total") for r in rs if r.get("tool_calls_total") is not None]
        # 用量/缓存两格 + 需要时的一句脚注，全在 `_usage_cells` 里（`None` 显示 `—`，
        # 不是 0；命中率只有那一处式子）。
        tin_txt, hit_txt, usage_note = _usage_cells(rs)
        if usage_note:
            cache_notes.append(f"`{arm}`：{usage_note}")
        rate = st.get("rate")
        flag = "" if len(rs) >= 2 else " ⚠️单遍"
        if partial:
            flag += f"（另有 {len(partial)} 份半截轮已排除）"
        if st.get("dropped"):
            flag += f"（路由窗口丢掉退化成员 {st['dropped']}）"
        rate_txt = "—" if rate is None else (
            f"{rate:.1%} ({st['unstable']}/{st['cases']})")
        cr = st_coarse.get("rate")
        crate_txt = "—" if cr is None else (
            f"{cr:.1%} ({st_coarse['unstable']}/{st_coarse['cases']})")
        lat_txt = f"{min(p50):.1f}/{max(p95):.1f}" if p50 and p95 else "—"
        mean_txt = "—" if mean is None else f"{mean:.2f}"
        print(f"| `{arm}`{flag} | {len(rs)} | "
              f"{' / '.join(map(str, reds)) or '—'} | {mean_txt} | "
              f"{' / '.join(f'{x:.4f}' for x in lows) or '—'} | "
              f"{'✅' if hard else '❌'} | {rate_txt} | {crate_txt} | "
              f"{lat_txt} | "
              f"{' / '.join(map(str, tc)) or '—'} | {tin_txt} | {hit_txt} |")
        detail.append((arm, rs, st))

    print(_USAGE_FOOTNOTE)
    if cache_notes:
        print("\n⚠️ 有调用**没报**缓存字段（分子少算了它们，分母照旧）：")
        for n in cache_notes:
            print(f"- {n}")

    _print_red_roll(detail)
    _print_route_roll(detail)
    _print_ab_diff(detail)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="参数矩阵：逐臂跑全量 golden 并出表")
    ap.add_argument("--arms", nargs="*", default=[], metavar="ARM",
                    help=f"要跑的臂（可选：{', '.join(ARM_SPECS)}）")
    ap.add_argument("--reps", type=int, default=2,
                    help="每臂跑几遍（**默认 2**：单遍读数不可判读）")
    ap.add_argument("--list", action="store_true", help="列出臂的定义与它要问的问题")
    ap.add_argument("--report", action="store_true", help="只读 jsonl 出表，不跑")
    ap.add_argument("--only-arms", nargs="*", default=[], help="出表时只看这几臂")
    a = ap.parse_args()
    if a.list:
        for k, v in ARM_SPECS.items():
            ov = " ".join(f"{x}={y}" for x, y in v["env"].items()) or "(不改任何旋钮)"
            print(f"{k:10s} {v['title']}\n          覆盖: {ov}\n          问题: {v['why']}\n")
        return 0
    if a.report:
        return report(a.only_arms or None)
    if not a.arms:
        ap.error("要么 --arms 跑，要么 --report 出表（--list 看臂）")
    return run(a.arms, max(1, a.reps))


if __name__ == "__main__":
    sys.exit(main())
