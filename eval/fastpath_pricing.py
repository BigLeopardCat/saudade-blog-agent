# -*- coding: utf-8 -*-
"""快道定价（20261015）：五条确定性快道**到底值不值**的取数装置。

为什么要有它：`agent/decisions.py` 那五条快道（导航 / 指代型导航 / 显示 / 读当前文章 /
特效切换）命中即**拿走 planner 的决策权**，而每一条都有真实事故依据（最有名的是
「不要樱花了改成下雨」——planner LLM 十轮里八轮只做半边）⇒ **不能纸面推定删**。
但这个仓库的判据一向"要么说清代价、要么别动"，所以先把价钱量出来。

两条腿（缺一不可，别只跑一条就下结论）：
  ① **静态映射**（`map`，零 LLM、零成本）：语料里哪些用例会被快道接走。它只回答
     "拨这一档**会不会**影响这条"，不回答"影响了是好是坏"。
  ② **两臂交替**（`run`）：同一批用例 `AGENT_FASTPATH=1/0` **交替**跑，读**逐条
    通过计数**（不读总通过率——本仓纪要：换臂读计数）。关档后由绿转红的用例 =
    那条快道在这条用例上**承重**；两臂同色的用例 = 这一档对它无影响。

⚠️ 三条纪律（照本仓 A/B 的老账）：
  · **交替**而不是"先跑完一臂再跑另一臂"：模型的方差会跟着挂钟漂（夜间、限流），
    连着跑会把"时段差"读成"手臂差"。
  · **读计数不读百分比**：14 条用例上 1 条红是噪声还是承重，只有多遍看得清。
  · **回来后要核手臂身份**（`_arm_identity`）：`AGENT_FASTPATH` 没生效（拼错、被
    `.env` 覆盖、跑在别的 cwd）时两臂读数会一模一样，那会被读成"快道不承重"——
    所以直接从 golden trace 里数 `planner.fastpath` 与 `planner.context` 的档位格。

用法（cd saudade-blog-agent；本机即生产链路，真 LLM）：
  .venv/bin/python eval/fastpath_pricing.py map                    # 零成本，先看影响面
  GOLDEN_ADMIN_UID=721 GOLDEN_USER_UID=722 \
    .venv/bin/python eval/fastpath_pricing.py run --reps 2          # 两臂交替 2 遍
"""
import argparse
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from golden_trace import trace_root  # noqa: E402  （golden trace 根目录的唯一实现）
from trace_io import load_trace  # noqa: E402  （与 trace_alert 同一个读取实现）

import agent.decisions as D  # noqa: E402

GOLDEN = os.path.join(ROOT, "eval", "golden", "basic.jsonl")


# ── ① 静态映射 ────────────────────────────────────────────────────────────
def _page_ctx(ctx: dict) -> str:
    """与 `agent/context.py::_page_ctx` 拿到的那串同形（`run_golden` 也照这个拼）。"""
    return (f"user_id={ctx.get('user_id', 0)}, page={ctx.get('current_url', '/')}, "
            f"title={ctx.get('page_title', '')}, current_effects={ctx.get('current_effects', '')}, "
            f"current_darkmode={ctx.get('current_darkmode', '')}")


def _rounds(case: dict):
    """用例 → [(第几轮, 用户原话, 生效的 context)]；`confirm_message` 那几轮不是主人说话，跳过。"""
    if "rounds" in case:
        base = dict(case.get("context") or {})
        for r in case["rounds"]:
            yield r.get("round", 1), r.get("user_input"), {**base, **(r.get("context") or {})}
    else:
        yield 1, case["user_input"], case.get("context") or {}


def hits_of(msg: str, ctx: dict, prev_ai: str) -> list[str]:
    """这句话会被哪几条快道接走（顺序 = `planner_node` 里的判定顺序）。"""
    if not msg:
        return []
    out = []
    for kind, hit in (("nav", D._nav_fast_path(msg)),
                      ("referent_nav", D._referent_nav_fast_path(msg, prev_ai)),
                      ("display", D._display_fast_path(msg)),
                      ("article_read", D._article_fast_path(msg, _page_ctx(ctx))),
                      ("effect_switch", D._effect_switch_fast_path(
                          msg, str(ctx.get("current_effects") or "")))):
        if hit is not None:
            out.append(kind)
    return out


def cmd_map() -> int:
    """语料 → 会被快道接走的用例清单（纯离线）。"""
    rows = []
    for line in open(GOLDEN, encoding="utf-8"):
        case = json.loads(line)
        for rd, msg, ctx in _rounds(case):
            hist = ctx.get("history") or []
            prev = (hist[-1].get("content") or "") if (hist and isinstance(hist[-1], dict)) else ""
            for kind in hits_of(msg or "", ctx, prev):
                rows.append((case["id"], rd, kind, msg))
    print(f"== 快道静态映射：{len(rows)} 处命中 / {len({r[0] for r in rows})} 条用例 ==")
    for cid, rd, kind, msg in rows:
        print(f"  {cid:42s} 第{rd}轮 [{kind}] {msg[:34]}")
    print(f"\n（`run` 会拿这 {len({r[0] for r in rows})} 条用例做两臂交替）")
    return 0


# ── ② 两臂交替 ────────────────────────────────────────────────────────────
def _ids() -> list[str]:
    seen = []
    for line in open(GOLDEN, encoding="utf-8"):
        case = json.loads(line)
        for rd, msg, ctx in _rounds(case):
            hist = ctx.get("history") or []
            prev = (hist[-1].get("content") or "") if (hist and isinstance(hist[-1], dict)) else ""
            if hits_of(msg or "", ctx, prev):
                seen.append(case["id"])
                break
    return seen


def _run_arm(arm: str, ids: list[str], run_id: str) -> int:
    env = dict(os.environ, AGENT_FASTPATH="1" if arm == "on" else "0")
    cmd = [sys.executable, os.path.join("eval", "run_golden.py"),
           "--only", ",".join(ids), "--trace-run-id", run_id, "--min-pass-rate", "0"]
    print(f"  → 手臂 {arm}（run_id={run_id}）…", flush=True)
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    print(f"     退出码 {rc}")
    return rc


def _report_of(run_id: str) -> dict | None:
    for p in sorted(glob.glob(os.path.join(ROOT, "eval", "report", "runs", "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("trace_run") == run_id:
            return d
    return None


def _arm_identity(run_id: str) -> tuple[int, dict]:
    """从**跑出来的 trace**核这一臂是不是真的是那一档（别只信环境变量）。

    返回 `(档位格里写着 off 的轮数, {kind: 命中次数})`。`AGENT_FASTPATH` 没生效时，
    关档那一臂照样会落 `fastpath` 事件——两臂读数会一模一样，而那是假的。
    """
    dial_off, kinds = 0, {}
    # ⚠️ 目录从 `golden_trace.trace_root()` 取，**不要自己拼仓根**：golden trace 落在
    # 生产 trace 目录的**兄弟**位置（`settings.trace_dir` 指父仓 `Saudade-Blog/logs/agent/
    # traces`），那既不在 agent 仓里、也不是相对本文件的路径。20261015 第一版就是硬拼
    # `ROOT/logs/agent/golden_traces` ⇒ 一个文件都读不到 ⇒ 手臂身份那一格恒报"（无）"，
    # 而"（无）"看起来正好像"这一臂没走快道"——**判据自己瞎了却长得像绿**。
    for f in glob.glob(os.path.join(trace_root(), run_id, "*.json")):
        d = load_trace(f)
        if d is None:
            continue
        for ev in (d.get("events") or []):
            if (ev.get("node"), ev.get("event")) == ("planner", "context"):
                if ev.get("fastpath") == "off":
                    dial_off += 1
            elif (ev.get("node"), ev.get("event")) == ("planner", "fastpath"):
                k = str(ev.get("kind") or "")
                kinds[k] = kinds.get(k, 0) + 1
    return dial_off, kinds


def cmd_run(reps: int, only: str = "", tag: str = "") -> int:
    # `--ids` 是**复采样**用的：第一遍 2 轮只够把"承重"挑出来，单条承重的那几条
    # （计数 1/2 那种）与噪声分不开，得单独多跑几遍；`--tag` 让这几遍的 run_id 与
    # 主跑错开（`_report_of` 靠 trace_run 反查报告，run_id 撞车会读错臂）。
    ids = [s for s in only.split(",") if s] or _ids()
    if not ids:
        print("语料里没有会被快道接走的用例 ⇒ 这一档无从定价（不是'不承重'，是'不适用'）")
        return 1
    print(f"== 快道两臂交替：{len(ids)} 条用例 × {reps} 遍 × 开/关 ==")
    print(f"   {ids}")
    tally: dict[tuple[str, str], int] = {}       # (case_id, arm) → 通过次数
    for i in range(reps):
        order = ("on", "off") if i % 2 == 0 else ("off", "on")   # 交替（含顺序）
        for arm in order:
            rid = f"fp{tag}_{arm}_{i}"
            _run_arm(arm, ids, rid)
            rep = _report_of(rid)
            if rep is None:
                print(f"     ⚠️ 找不到 run_id={rid} 的报告，这一遍不计入")
                continue
            for c in rep.get("cases") or []:
                key = (c["id"], arm)
                tally[key] = tally.get(key, 0) + (1 if c.get("ok") else 0)
            _off, kinds = _arm_identity(rid)
            print(f"     trace 里的快道命中：{kinds or '（无）'}"
                  + ("   ← ⚠️ 关档这一臂**仍有命中**，档位没生效，读数作废"
                     if arm == "off" and kinds else ""))

    print("\n== 逐条（读计数，不读通过率）==")
    print(f"  {'用例':44s} {'开':>8s} {'关':>8s}   判读")
    bearing, same = [], []
    for cid in ids:
        on = tally.get((cid, "on"), 0)
        off = tally.get((cid, "off"), 0)
        if off < on:
            tag = "**快道承重**（关了就红）"
            bearing.append(cid)
        elif off > on:
            tag = "关档反而更好（快道在这条上是负的）"
            bearing.append(cid)
        else:
            tag = "两臂同色（这一档对它无影响）"
            same.append(cid)
        print(f"  {cid:44s} {on:>4d}/{reps} {off:>4d}/{reps}   {tag}")
    print(f"\n  承重 {len(bearing)} 条：{bearing}")
    print(f"  无影响 {len(same)} 条：{same}")
    print("\n⚠️ 这只是**一个语料的读数**：承重 ≠ 不能删（可能是那条用例在替快道写账，"
          "也可能是模型自己不稳——单条承重的那几条要再用'空对照臂'复采样才敢下结论）；"
          "无影响 ≠ 该删（生产 trace 的命中分布是另一半，见 `eval/trace_alert.py` 的 R5）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("map", "run"))
    ap.add_argument("--reps", type=int, default=2, help="两臂各跑几遍（交替；默认 2）")
    ap.add_argument("--ids", dest="ids", default="",
                    help="只跑这几条用例（逗号分隔）。**复采样用**：2 遍只够挑出"
                         "'承重'，单条计数 1/2 那几条要单独多跑几遍才与噪声分得开")
    ap.add_argument("--tag", dest="tag", default="",
                    help="run_id 前缀（默认 `fp`）。复采样时换个 tag，免得与主跑的"
                         " run_id 撞车把另一臂的读数读回来")
    a = ap.parse_args()
    return cmd_map() if a.what == "map" else cmd_run(a.reps, a.ids, a.tag)


if __name__ == "__main__":
    sys.exit(main())
