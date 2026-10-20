# -*- coding: utf-8 -*-
"""POC 步骤 3：三臂（`graph` / `react` / `tools`）交错子集 A/B 驱动。

**跑什么**：`eval/experiments/poc_menu_ids.txt` 里那批**预注册**的 id（跑前提交、跑后不许改），
三条臂**逐遍交替**各跑一遍（外层遍、内层臂）——采样窗口最接近，抵消端点漂移。

三条臂（见 `eval/golden_arm.py` 与 `agent/tools_arm.py` 头注）：

    graph   生产：受限规划（`planner ⇄ execute → model → gate`），模型点**技能名**，
            由 `instantiate_plan` 展开成固定工具序列；
    react   同一条循环的独立实现（**与 tools 共用同一张真图**）；
    tools   本 POC：模型直接点**真工具**，没有中间展开那一层。

**三对读数的可判读性不一样**（先写下来，免得事后按结果编解释）：

  · `react ↔ tools`：**最干净的一对**。同一张图、同一份判据，只差四个接缝 ⇒ 差异归"菜单形状"；
  · `graph ↔ tools`：**本题要答的那一对**，但掺着"循环形状"这个 confound（graph 是受限规划）；
  · `graph ↔ react`：**带缺资产 confound**——`main` 上的 react 臂是欠配版（资产对齐那 19 个提交
    在一条未 push 的分支上，且它落后 main 68 个提交）。**这一对不作为结论**，只当参照。

**判读规则**（同 `ckpt_ab_driver.py` 那条纪律：先写下来，再跑）：

  · 单遍不可判读（相邻两次同配置的红集只重叠 ~5 条）⇒ **3 遍是下限，不是奢侈**；
  · 逐条差集比通过率重要：`per_case_rose` / `per_case_fell` 逐条定性，
    "合计没退"不等于"没有单条变坏"；
  · `arm_capability --arm tools --probe` 若 `comparable=False` ⇒ **退出码 3、pass_rate 不许引用**
    （纯否定族在"结构上产不出"的臂上恒真）；
  · 三臂读数完全一致时**先查臂标**——本仓吃过"两臂其实跑的是同一份代码"。

**环境三件**（本机跑 golden 的硬要求，见 `docs/` 与 `scripts/nightly_regression.sh`）：
① cwd 必须是**主仓**（`.env` 是相对路径）；② `PYTHONPATH` 指向**本工作树**（venv 里那条
editable `.pth` 指向主仓，不压过它 ⇒ `import agent.graph` 静默解析到主仓那份）；③ 身份通道
必须显式导出（不设 ⇒ 真身份用例被静默跳过，读数漂亮但测的不是你以为的那份语料）。

跑法（cwd 任意，本脚本自己切）：

```bash
.venv/bin/python eval/experiments/menu_arm_ab.py --reps 3
```
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.dirname(os.path.dirname(HERE))            # 本工作树根
MAIN = "/home/ubuntu/Saudade-Blog/saudade-blog-agent"  # 主仓（cwd + 解释器）
MAIN_PY = os.path.join(MAIN, ".venv/bin/python")
IDS_FILE = os.path.join(HERE, "poc_menu_ids.txt")
OUT_JSON = os.path.join(WT, "eval/report/baseline_poc_menu_ab.json")

if os.path.join(WT, "eval") not in sys.path:
    sys.path.insert(0, os.path.join(WT, "eval"))

import golden_arm  # noqa: E402  —— 臂→留档目录那张表**只此一处**（别在这儿抄第二份）

ARMS = ("graph", "react", "tools")
ARM_CN = {"graph": "生产(受限规划)", "react": "react 臂", "tools": "工具臂(POC)"}

#: 单遍挂钟上限。**必须有**：20261016 实测服务商把连接半关而客户端不返回 ⇒ 子进程挂满 15 分钟，
#: `subprocess.run` 抛 `TimeoutExpired`，没接的话**整个驱动带崩**、前面跑完的读数一起丢。
#: 一份读数因为"某一条卡住"而全废，是本仓最不划算的一类失败。
RUN_TIMEOUT = 5400

#: 身份通道（**进程变量，不走 `.env`**）。756 = 专用夹具管理员（721 已降权为 user，
#: 理由见 `scripts/nightly_regression.sh` 那处长注）；722 = 夹具用户。三臂必须指同一个身份。
IDENTITY_ENV = {"GOLDEN_ADMIN_UID": "756", "GOLDEN_USER_UID": "722"}

#: 盘量警戒线（GB）。盘满会让 golden 中途死掉且**死得不响**（症状是最后一行
#: `trace dump failed`、没有 traceback）⇒ 开跑前先看，别把一份读数跑丢在半路。
DISK_MIN_GB = 1.0


def load_ids(path: str = IDS_FILE) -> list[str]:
    """预注册 id 表 → id 列表（`#` 起头的行是**口径说明**，不是用例）。"""
    out: list[str] = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def disk_free_gb() -> float:
    return shutil.disk_usage("/").free / (1024 ** 3)


def arm_marker(arm: str) -> str:
    """这一臂**真的**会 import 到哪个模块（子进程里问一次，打出来当臂标）。

    为什么非要有它：本仓吃过"两臂其实跑的是同一份代码"（同一进程只 `import agent.graph`
    一次 ⇒ 进程内换文件无效）。三臂读数完全一致时，第一件事就是回来看这一行。
    """
    if arm == "graph":
        code = (f"import sys; sys.path.insert(0,{WT!r});"
                "import agent, inspect; print(inspect.getsourcefile(agent.create_agent))")
    else:
        code = (f"import sys; sys.path.insert(0,{WT!r});"
                f"import agent.{arm}_arm as m; print(m.__file__)")
    p = subprocess.run([MAIN_PY, "-c", code], cwd=MAIN,
                       env={**os.environ, "PYTHONPATH": WT},
                       capture_output=True, text=True, timeout=180)
    out = (p.stdout or "").strip() or (p.stderr or "").strip()
    return out.splitlines()[-1] if out else "?"


def reports_dir_of(arm: str) -> str:
    """这一臂的报告目录（**主仓**下那份）。

    ⚠️ `run_golden.py` 里 `_REPORTS_DIR = golden_arm.reports_dir(_ARM)` 是**相对路径**
    （`eval/report/runs`），而子进程的 cwd 是主仓 ⇒ 报告落在**主仓**，不是本工作树。
    第一版按工作树去找，症状是"每一遍都报没有产出报告"，而 stdout 里明明印着那份文件名
    ——**读数在那里，只是找错了地方**。这一格与 `run_golden` 的 cwd 必须同源。
    """
    return os.path.join(MAIN, golden_arm.reports_dir(arm))


def run_arm(arm: str, rep: int, ids: list[str], provider: str = "") -> dict:
    """跑一遍（这一臂 × 这批 id），返回那份报告。

    读法同 `ckpt_ab_driver.run_case`：**跑前记下目录里已有的报告，跑后取新增的那一份**
    ——文件名顺序 = 时间顺序（`report_archive.open_archive` 的注），取最后一个。
    """
    rdir = reports_dir_of(arm)
    before = set(glob.glob(os.path.join(rdir, "*.json")))
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": WT,
        "GOLDEN_ARM": arm,
        # 线程坐标 = (用例, 重复, 臂)：不设 `GOLDEN_REP` 的话每一遍的线程名都一样 ⇒
        # 逐遍跑就变成"在上一遍的 state 上接着跑"，而症状是"读数在漂"。
        "GOLDEN_REP": str(rep),
        **IDENTITY_ENV,
        **provider_env(provider),
    })
    t0 = time.time()
    try:
        p = subprocess.run([MAIN_PY, os.path.join(WT, "eval/run_golden.py"),
                            "--only", ",".join(ids)],
                           cwd=MAIN, env=env, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT)
        rc, tail = p.returncode, (p.stdout or "")[-1500:]
    except subprocess.TimeoutExpired:
        # 超时不记异常正文：`TimeoutExpired` 的 `str()` 里只有命令行与秒数，而 `p` 此刻
        # 还没绑定 ⇒ 能拿到的读数一个不少（`rc=-1` + 那句 tail 已经说明是超时）。
        rc, tail = -1, f"超时（>{RUN_TIMEOUT}s）"
    elapsed = round(time.time() - t0, 1)
    new = sorted(set(glob.glob(os.path.join(rdir, "*.json"))) - before)
    if not new:
        return {"_error": f"没有产出报告（rc={rc}）", "_tail": tail, "_elapsed": elapsed}
    rep_doc = json.load(open(new[-1], encoding="utf-8"))
    rep_doc["_elapsed"] = elapsed
    rep_doc["_rc"] = rc
    rep_doc["_report"] = os.path.basename(new[-1])
    rep_doc["_outage_cases"] = outage_cases(rep_doc)
    return rep_doc


#: 服务商停摆的指纹。**必须按"整批不相关用例一起红"来认，不能按单条**：
#: 20261020 实测（本次）：11:05 那两遍 `graph 0/80`、`react 7/80`，红的原因逐字是
#: `Error code: 403 - Access to model denied … AccessDenied.Unpurchased`，而**同一批用例
#: 一小时前两臂都是 78–79/80**。不认出来的话，一份"工具臂比生产差 60 条"的结论会直接
#: 从这种噪声里长出来——而它比没有读数坏得多。
OUTAGE_MARKS = ("Access to model denied", "AccessDenied", "PermissionDeniedError",
                "AuthenticationError", "invalid_api_key", "insufficient_quota",
                "RateLimitError", "APIConnectionError", "APITimeoutError")


def outage_cases(doc: dict) -> list[str]:
    """这一遍里**因服务商停摆而红**的用例 id（不是行为差，是环境差）。

    两条口径，缺一条都会漏：

      · **指纹**：报错文本里出现 `OUTAGE_MARKS` 之一（403 / 401 / 限流 / 连接层）；
      · **整批同型**：臂把内层异常就地写成正文（`我停在这里了：内部出了点问题（X）`）
        ⇒ 报错原文**不在报告里**，指纹那条看不见它。20261020 实测：一遍 80 条里
        **80 条全是同一个 `BadRequestError`、中位耗时 0.4s**（= 每一次调用都被当场拒），
        而 7 分钟前同一臂同一批用例全绿、7 分钟后又全绿 ⇒ 那是服务商的一个窗口，
        不是臂退化。判据取**同一个异常类覆盖 ≥ 一半用例**——单条崩溃是真缺陷，留着；
        半数以上一起崩，只可能是环境。
    """
    cases = doc.get("cases") or []
    hits = []
    for c in cases:
        blob = " ".join(str(c.get(k) or "") for k in ("error", "fails", "text"))
        for r in c.get("rounds") or []:
            blob += " " + str(r.get("error") or "")
        if any(m in blob for m in OUTAGE_MARKS):
            hits.append(c["id"])
    cls, n = systemic_error(cases)
    if n:
        hits.extend(c["id"] for c in cases if cls in str(c.get("text") or ""))
    return sorted(set(hits))


#: 臂把内层异常就地写成正文时的那句壳（`agent/react_arm._crash_text`）。
CRASH_RE = re.compile(r"内部出了点问题（(\w+)）")

#: "同一个异常类覆盖这么多用例 ⇒ 判环境"的那条线。
SYSTEMIC_SHARE = 0.5


def systemic_error(cases: list) -> tuple[str, int]:
    """→ `(异常类名, 命中数)`；没到线就是 `("", 0)`。"""
    cnt: dict[str, int] = {}
    for c in cases:
        m = CRASH_RE.search(str(c.get("text") or ""))
        if m:
            cnt[m.group(1)] = cnt.get(m.group(1), 0) + 1
    if not cnt or not cases:
        return "", 0
    cls, n = max(cnt.items(), key=lambda kv: kv[1])
    return (cls, n) if n >= SYSTEMIC_SHARE * len(cases) else ("", 0)


def _ok(case: dict) -> bool:
    return bool(case.get("final_ok", case.get("ok")))


def case_matrix(reports: dict, ids: list[str], arms: list[str], reps: int) -> dict:
    """逐用例 × 逐臂：N 遍里绿了几遍。**这一张表是主判据**（差集比通过率有信息）。"""
    out: dict[str, dict[str, str]] = {}
    for cid in ids:
        row = {}
        for arm in arms:
            seen = []
            for r in range(1, reps + 1):
                doc = reports.get((arm, r)) or {}
                c = next((c for c in doc.get("cases") or [] if c["id"] == cid), None)
                # **"停"与"红"必须分开**（20261020）：服务商停摆那一遍的红不是行为差，
                # 混进同一个字符位就等于把环境故障读成了臂的退化。
                outage = cid in (doc.get("_outage_cases") or [])
                seen.append("-" if c is None else ("停" if outage else
                                                   ("绿" if _ok(c) else "红")))
            row[arm] = "".join(seen)
        out[cid] = row
    return out


def _pair(before: list, after: list) -> dict:
    """`landing_gate.ab_compare`（**判据只有一条**：下界不低于 before - tol）。

    传的是"报告列表"（不是拼好的 cases）⇒ 由它自己按多次采样合并统计。

    ⚠️ **只传真的报告**：跑失败那一遍的占位 dict 没有 `cases` 键，而 `ab_compare` 的
    `_flatten` 对"没有 cases 的 dict"走的是 `out.extend(item)` ⇒ 把**键名**当成用例摊进去，
    于是 `_rate` 拿着字符串去 `.get("tags")` 崩在 `AttributeError` 上。那一崩发生在**跑完
    半小时之后**，而它丢掉的是整份汇总——所以过滤放在这里，不放在调用方。
    """
    import landing_gate

    keep = ([d for d in before if "cases" in d and not d.get("_outage_cases")],
            [d for d in after if "cases" in d and not d.get("_outage_cases")])
    if not keep[0] or not keep[1]:
        return {"verdict": "no_data", "note": "有一侧没有产出报告 ⇒ 不判（别拿它当绿）"}
    return landing_gate.ab_compare(*keep)


def compare(reports: dict, arms: list[str], reps: int) -> dict:
    """三对读数：`react↔tools`（最干净）、`graph↔tools`（本题）、`graph↔react`（参照）。"""
    got = {a: [reports[(a, r)] for r in range(1, reps + 1) if (a, r) in reports]
           for a in arms}
    pairs = {}
    for before, after, name in (("react", "tools", "react↔tools（菜单形状）"),
                                ("graph", "tools", "graph↔tools（本题）"),
                                ("graph", "react", "graph↔react（参照，带缺资产 confound）")):
        if got.get(before) and got.get(after):
            pairs[name] = _pair(got[before], got[after])
    return pairs


def cost_of(reports: dict, arms: list[str], reps: int) -> dict:
    """成本对照：挂钟 p50 / 工具轮数 / 决策轮数 / **token 与缓存命中**（读 trace）。

    `cache_read` 缺席的调用**不进命中率分母**（缺席是"这个形状量不到缓存"，不是"没命中"，
    混进来会算出"缓存完全没命中"这种假结论——同 `dial_matrix.trace_metrics` 的纪律）。

    ⚠️ **两臂的 token 与决策轮量不出来，这不是"用了 0"**（20261020 实测）：`llm_done`
    与 `native_decision` 两个 trace 事件**只在 `graph.py` 的生产节点里记**，`react_arm`
    的决策节点一个都不记（`grep 'record(' agent/react_arm.py` 只有五处，全是 consent/
    fastpath/ledger 那几族）。⇒ `react` / `tools` 两臂的 `planner_*` 三格恒 0，而它们
    **真的调了 LLM**。所以：**跨臂只比挂钟与工具轮数**；token/缓存那一栏只在 graph 臂内
    有含义。要把它变成可比的，得先给 react 臂补 `llm_done`（本轮不做，如实记成缺口）。
    """
    import dial_matrix

    out: dict[str, dict] = {}
    for arm in arms:
        lat, rounds, calls, tok_in, tok_out, cache, cache_seen, dec = [], 0, 0, 0, 0, 0, 0, 0
        cases_seen = 0
        for r in range(1, reps + 1):
            doc = reports.get((arm, r)) or {}
            if doc.get("_outage_cases"):
                continue          # 停摆那一遍的挂钟与轮数都不可比（它根本没跑完）
            cases_seen += len(doc.get("cases") or [])
            lat.append((doc.get("latency_s") or {}).get("p50"))
            pe = doc.get("plan_efficiency") or {}
            rounds += int(pe.get("tool_rounds_total") or 0)
            calls += int(pe.get("tool_calls_total") or 0)
            try:
                m = dial_matrix.trace_metrics_for(doc)
            except Exception as exc:  # noqa: BLE001 —— 没落 trace 不该把整张表弄死
                # 读不出就**记成"这一遍没有可读的 trace"**（空表 ⇒ 下面那些 `.get` 全给 0），
                # 而不是 `continue` 跳过这一遍的余下几行——两者在**当前**代码里等价，但
                # `continue` 会在以后有人往 try 后面加一行时**静默**把它也跳掉。顺带这也是
                # bandit B112（`except: continue`）那条告警指的形状。
                print(f"[ab] ⚠ {arm} r{r} 读不出 trace（{type(exc).__name__}）⇒ 该遍用量记 0")
                m = {}
            dec += int(m.get("native_decisions") or 0)
            for node, u in (m.get("usage") or {}).items():
                if node != "planner":
                    continue
                tok_in += u.get("in", 0)
                tok_out += u.get("out", 0)
                cache += u.get("cache", 0)
                cache_seen += u.get("cache_seen", 0)
        lats = sorted(x for x in lat if isinstance(x, (int, float)))
        out[arm] = {
            "挂钟p50_中位": round(lats[len(lats) // 2], 1) if lats else None,
            "工具轮数合计": rounds, "工具调用合计": calls, "planner决策轮": dec,
            "planner_in_tokens": tok_in, "planner_out_tokens": tok_out,
            "cache_read": cache, "cache_seen_calls": cache_seen,
            "缓存命中率": (round(cache / tok_in, 4) if tok_in else None),
            # "这一臂跑了用例却一格 token 都没读到" ≠ "它没花 token"（见 docstring 那条）。
            "planner指标可用": bool(tok_in or dec),
            "本臂用例数": cases_seen,
        }
    return out


def families(reports: dict, arms: list[str], reps: int) -> dict:
    """逐 tag 的红率（"哪一族非模板不可"要靠这张表）。"""
    agg: dict[str, dict[str, list[int]]] = {}
    for arm in arms:
        for r in range(1, reps + 1):
            doc = reports.get((arm, r)) or {}
            if doc.get("_outage_cases"):
                continue          # 停摆那一遍不进红率（否则整张表被环境故障涂红）
            for c in doc.get("cases") or []:
                for t in c.get("tags") or []:
                    cell = agg.setdefault(t, {}).setdefault(arm, [0, 0])
                    cell[1] += 1
                    if not _ok(c):
                        cell[0] += 1
    return {t: {a: f"{v[0]}/{v[1]} 红" for a, v in by.items()} for t, by in agg.items()}


def _parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3, help="每条用例每臂跑几遍（判据是多遍 A/B）")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--ids-file", default=IDS_FILE)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用）")
    ap.add_argument("--provider", default="",
                    help="**只给子进程**的 LLM_PROVIDER 覆盖（不动 `.env`、不重启服务）。"
                         "空 = 用 `.env` 那一档。20261020 用它绕开 qwen 的账号级 403")
    ap.add_argument("--retry-outage", type=int, default=1,
                    help="一遍被判停摆/整批同型时重跑几次（缺省 1；作废那份照样记进汇总）")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M%S"))
    return ap.parse_args(argv)


def provider_env(provider: str) -> dict:
    """子进程的 provider 覆盖。**只改子进程环境**：`.env` 是生产那一份，改它等于改线上
    （见 [[saudade-llm-provider-outage-20260927]] 那条"改缺省值修不了产线"）；重启服务
    更不是本 POC 该做的事。pydantic-settings 的取值顺序是 环境变量 > `.env` ⇒ 这一格够用。
    """
    return {"LLM_PROVIDER": provider} if provider else {}


def _preflight(args) -> tuple[list[str], list[str]] | None:
    """id 表 / 臂表 / 盘余 / 臂标。盘不够 ⇒ `None`（调用方返回退出码 3）。"""
    ids = load_ids(args.ids_file)
    if args.limit:
        ids = ids[: args.limit]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    free = disk_free_gb()
    print(f"[ab] tag={args.tag}  arms={arms}  ids={len(ids)}  reps={args.reps}")
    print(f"[ab] provider={args.provider or '(.env 那一档)'}"
          + ("  ⚠️ **只给子进程**，`.env` 与生产服务一个字没动" if args.provider else ""))
    print(f"[ab] 盘余 {free:.2f}G" + ("  ⚠ 低于警戒线，先清 trace" if free < DISK_MIN_GB else ""))
    if free < DISK_MIN_GB:
        return None
    for arm in arms:
        print(f"[ab] 臂标 {arm:<6} → {arm_marker(arm)}")
    print(f"[ab] 读数落点 {WT}/{golden_arm.reports_dir('graph').rsplit('/', 1)[0]}/runs*/")
    return ids, arms


def _run_one_pass(arm: str, rep: int, i: int, n_arms: int, ids: list[str],
                  args, retries: dict) -> dict:
    """跑一遍（作废则按 `--retry-outage` 重跑），返回最后那一份报告。

    **为什么自动重跑**：一遍 15 分钟、九遍两小时，而服务商的一个 1 分钟窗口就废掉其中一遍
    ——20261020 实测那遍 `react 7/80` 全是瞬时 400（中位 0.4s），7 分钟后同一臂同一批用例
    全绿。作废那份不写进汇总，但**重跑次数记着**：丢掉几遍、为什么丢，本身就是读数的一部分。
    """
    doc = run_arm(arm, rep, ids, args.provider)
    for attempt in range(args.retry_outage):
        if "_error" not in doc and not doc.get("_outage_cases"):
            break
        retries[f"{arm}/r{rep}"] = retries.get(f"{arm}/r{rep}", 0) + 1
        bad = (f"{len(doc['_outage_cases'])} 条作废" if "_error" not in doc
               else doc["_error"])
        print(f"[r{rep} {i}/{n_arms}] {arm:<6} ⚠️ {bad} ⇒ 重跑"
              f"（第 {attempt + 1}/{args.retry_outage} 次）", flush=True)
        doc = run_arm(arm, rep, ids, args.provider)
    if "_error" in doc:
        print(f"[r{rep} {i}/{n_arms}] {arm:<6} ✗ {doc['_error']} "
              f"（{doc['_elapsed']}s）\n        {doc['_tail'][-300:]}", flush=True)
        return doc
    ok = sum(1 for c in doc["cases"] if _ok(c))
    out = doc.get("_outage_cases") or []
    cls, n = systemic_error(doc.get("cases") or [])
    why = (f"停摆/整批同型 {len(out)} 条" + (f"（{cls}×{n}）" if cls else "")) if out else ""
    print(f"[r{rep} {i}/{n_arms}] {arm:<6} {ok}/{doc['total']} "
          f"engine={doc.get('engine')} 跳{len(doc.get('skipped_ids') or [])} "
          f"真写跳{len(doc.get('skipped_real_write_ids') or [])} {doc['_elapsed']}s"
          + (f"  ⚠️ {why} ⇒ **这一遍作废**" if out else ""), flush=True)
    return doc


def _run_matrix(ids: list[str], arms: list[str], args) -> tuple[dict, dict]:
    """外层遍、内层臂（同一遍里三臂挨着跑，采样窗口最接近）。"""
    reports: dict[tuple, dict] = {}
    retries: dict[str, int] = {}
    for rep in range(1, args.reps + 1):
        for i, arm in enumerate(arms, 1):
            reports[(arm, rep)] = _run_one_pass(arm, rep, i, len(arms), ids, args, retries)
    return reports, retries


def _print_matrix(reports: dict, ids: list[str], arms: list[str], reps: int) -> dict:
    """① 逐用例 × 逐臂（主判据：差集，不是通过率）。"""
    print("\n" + "=" * 96)
    print("逐用例 × 逐臂（每格 = 各遍的绿/红/停，顺序 = 遍号）")
    print("=" * 96)
    m = case_matrix(reports, ids, arms, reps)
    hdr = "  ".join(f"{a:>{reps + 2}}" for a in arms)
    print(f"{'用例':<46} {hdr}")
    for cid in ids:
        row = m[cid]
        if all(set(v) <= {"绿"} for v in row.values()):
            continue                      # 三臂全绿 ⇒ 没话可说（省掉满屏同形行）
        print(f"{cid:<46} " + "  ".join(f"{row[a]:>{reps + 2}}" for a in arms))
    return m


def _print_per_case(v: dict) -> None:
    """逐条差集（`per_case_fell` / `per_case_rose`）。

    ⚠️ **这两个数字是红率、不是通过率**（`ab_compare` 里 `e[tag][0] += 1` 数的是
    `not _final_ok`）。所以 `↓`＝红率下降＝**变好**，`↑`＝红率上升＝**变坏**——与"通过率"
    的直觉**正好反着**。第一次读这份日志的人（包括写它的人）会按通过率读，于是把
    「工具臂在某条上从 0 红变 1 红」读成「工具臂赢了」。表头把这句写在数字旁边，因为
    箭头本身没法自证。逐条与聚合**必须分开读**：聚合没退不等于没有单条变坏。
    """
    if not (v.get("per_case_fell") or v.get("per_case_rose")):
        return
    print("    逐条（数字 = **红率**；↓ 红率降＝变好，↑ 红率升＝变坏）")
    for arrow, key in (("↓", "per_case_fell"), ("↑", "per_case_rose")):
        for e in (v[key] or [])[:8]:
            print(f"      {arrow} {e['id']:<44} {e['before_rate']} → {e['after_rate']}")


def _print_pairs(reports: dict, arms: list[str], reps: int) -> dict:
    """② 三对读数。"""
    pairs = compare(reports, arms, reps)
    print("\n" + "=" * 96)
    print("三对读数（`ab_compare`：下界不低于 before - 0.005 才算没退）")
    print("=" * 96)
    for name, v in pairs.items():
        print(f"\n{name}")
        if v.get("verdict") == "no_data":
            print(f"  {v['note']}")
            continue
        print(f"  判决 {v['verdict']}  before={v['before']['lower']} → "
              f"after={v['after']['lower']}（Δ={v['delta_lower']}）"
              f"  可比用例 {v['cases_compared']}")
        _print_per_case(v)
    return pairs


def main(argv=None) -> int:
    # **行缓冲**（20261020 实测）：stdout 重定向到文件时是块缓冲，而跑臂那几行用
    # `flush=True` 抢跑了 ⇒ 汇总那几节挤在缓冲里，`> out.txt` 之后**读到的文件短了一截**，
    # 症状是"矩阵与三对读数两节凭空消失、成本那节还在"（看着像代码没跑到那儿）。
    # 一次跑半小时，读数少一截 = 白跑。这一行把它钉死，别靠调用方记得加 `-u`。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args(argv)
    ready = _preflight(args)
    if ready is None:
        return 3
    ids, arms = ready
    reports, retries = _run_matrix(ids, arms, args)

    m = _print_matrix(reports, ids, arms, args.reps)
    pairs = _print_pairs(reports, arms, args.reps)

    cost = cost_of(reports, arms, args.reps)
    print("\n" + "=" * 96)
    print("成本对照")
    print("=" * 96)
    for arm in arms:
        print(f"  {ARM_CN.get(arm, arm):<16} {cost[arm]}")

    fam = families(reports, arms, args.reps)
    print("\n" + "=" * 96)
    print("逐 tag 红率")
    print("=" * 96)
    for t, by in sorted(fam.items()):
        print(f"  {t:<24} " + "  ".join(f"{a}:{by.get(a, '-')}" for a in arms))
    print("\n" + "=" * 96)
    print("逐用例 × 逐臂（每格 = 各遍的绿/红，顺序 = 遍号）")
    print("=" * 96)
    m = case_matrix(reports, ids, arms, args.reps)
    hdr = "  ".join(f"{a:>{args.reps + 2}}" for a in arms)
    print(f"{'用例':<46} {hdr}")
    for cid in ids:
        row = m[cid]
        if all(set(v) <= {"绿"} for v in row.values()):
            continue                      # 三臂全绿 ⇒ 没话可说（省掉满屏同形行）
        print(f"{cid:<46} " + "  ".join(f"{row[a]:>{args.reps + 2}}" for a in arms))

    # ── ② 三对读数 ────────────────────────────────────────────────────
    pairs = compare(reports, arms, args.reps)
    print("\n" + "=" * 96)
    print("三对读数（`ab_compare`：下界不低于 before - %.3f 才算没退）" % 0.005)
    print("=" * 96)
    for name, v in pairs.items():
        print(f"\n{name}")
        print(f"  判决 {v['verdict']}  before={v['before']['lower']} → "
              f"after={v['after']['lower']}（Δ={v['delta_lower']}）"
              f"  可比用例 {v['cases_compared']}")
        _print_per_case(v)

    cost = cost_of(reports, arms, args.reps)
    print("\n" + "=" * 96)
    print("成本对照")
    print("=" * 96)
    for arm in arms:
        print(f"  {ARM_CN.get(arm, arm):<16} {cost[arm]}")

    fam = families(reports, arms, args.reps)
    print("\n" + "=" * 96)
    print("逐 tag 红率")
    print("=" * 96)
    for t, by in sorted(fam.items()):
        print(f"  {t:<24} " + "  ".join(f"{a}:{by.get(a, '-')}" for a in arms))

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"tag": args.tag, "ids": ids, "arms": arms, "reps": args.reps,
                   "provider": args.provider or "(.env)",
                   "outage": {f"{a}/r{r}": len((reports.get((a, r)) or {})
                                               .get("_outage_cases") or [])
                              for a in arms for r in range(1, args.reps + 1)},
                   "retries": retries,
                   "matrix": m, "pairs": pairs, "cost": cost, "families": fam},
                  f, ensure_ascii=False, indent=1)
    print(f"\n[ab] 汇总已写 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
