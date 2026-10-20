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


def run_arm(arm: str, rep: int, ids: list[str]) -> dict:
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
    })
    t0 = time.time()
    try:
        p = subprocess.run([MAIN_PY, os.path.join(WT, "eval/run_golden.py"),
                            "--only", ",".join(ids)],
                           cwd=MAIN, env=env, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT)
        rc, tail = p.returncode, (p.stdout or "")[-1500:]
    except subprocess.TimeoutExpired as e:
        rc, tail = -1, f"超时（>{RUN_TIMEOUT}s）"
    elapsed = round(time.time() - t0, 1)
    new = sorted(set(glob.glob(os.path.join(rdir, "*.json"))) - before)
    if not new:
        return {"_error": f"没有产出报告（rc={rc}）", "_tail": tail, "_elapsed": elapsed}
    rep_doc = json.load(open(new[-1], encoding="utf-8"))
    rep_doc["_elapsed"] = elapsed
    rep_doc["_rc"] = rc
    return rep_doc


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
                seen.append("-" if c is None else ("绿" if _ok(c) else "红"))
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

    keep = [d for d in before if "cases" in d], [d for d in after if "cases" in d]
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
            cases_seen += len(doc.get("cases") or [])
            lat.append((doc.get("latency_s") or {}).get("p50"))
            pe = doc.get("plan_efficiency") or {}
            rounds += int(pe.get("tool_rounds_total") or 0)
            calls += int(pe.get("tool_calls_total") or 0)
            try:
                m = dial_matrix.trace_metrics_for(doc)
            except Exception:  # noqa: BLE001 —— 没落 trace 不该把整张表弄死
                continue
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
            for c in (reports.get((arm, r)) or {}).get("cases") or []:
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
    ap.add_argument("--tag", default=time.strftime("%Y%m%d_%H%M%S"))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    # **行缓冲**（20261020 实测）：stdout 重定向到文件时是块缓冲，而跑臂那几行用
    # `flush=True` 抢跑了 ⇒ 汇总那几节挤在缓冲里，`> out.txt` 之后**读到的文件短了一截**，
    # 症状是"矩阵与三对读数两节凭空消失、成本那节还在"（看着像代码没跑到那儿）。
    # 一次跑半小时，读数少一截 = 白跑。这一行把它钉死，别靠调用方记得加 `-u`。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args(argv)
    ids = load_ids(args.ids_file)
    if args.limit:
        ids = ids[: args.limit]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    free = disk_free_gb()
    print(f"[ab] tag={args.tag}  arms={arms}  ids={len(ids)}  reps={args.reps}")
    print(f"[ab] 盘余 {free:.2f}G" + ("  ⚠ 低于警戒线，先清 trace" if free < DISK_MIN_GB else ""))
    if free < DISK_MIN_GB:
        return 3
    for arm in arms:
        print(f"[ab] 臂标 {arm:<6} → {arm_marker(arm)}")
    print(f"[ab] 读数落点 {WT}/{golden_arm.reports_dir('graph').rsplit('/', 1)[0]}/runs*/")

    reports: dict[tuple, dict] = {}
    for rep in range(1, args.reps + 1):
        for i, arm in enumerate(arms, 1):
            t0 = time.time()
            doc = run_arm(arm, rep, ids)
            reports[(arm, rep)] = doc
            if "_error" in doc:
                print(f"[r{rep} {i}/{len(arms)}] {arm:<6} ✗ {doc['_error']} "
                      f"（{doc['_elapsed']}s）\n        {doc['_tail'][-300:]}", flush=True)
                continue
            ok = sum(1 for c in doc["cases"] if _ok(c))
            print(f"[r{rep} {i}/{len(arms)}] {arm:<6} {ok}/{doc['total']} "
                  f"engine={doc.get('engine')} 跳{len(doc.get('skipped_ids') or [])} "
                  f"真写跳{len(doc.get('skipped_real_write_ids') or [])} "
                  f"{round(time.time() - t0, 1)}s", flush=True)

    # ── ① 逐用例 × 逐臂（主判据：差集，不是通过率）──────────────────────
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
        for e in (v["per_case_fell"] or [])[:8]:
            print(f"    ↓ {e['id']:<44} {e['before_rate']} → {e['after_rate']}")
        for e in (v["per_case_rose"] or [])[:8]:
            print(f"    ↑ {e['id']:<44} {e['before_rate']} → {e['after_rate']}")

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
                   "matrix": m, "pairs": pairs, "cost": cost, "families": fam},
                  f, ensure_ascii=False, indent=1)
    print(f"\n[ab] 汇总已写 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
