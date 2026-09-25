# -*- coding: utf-8 -*-
"""真实对话语义告警巡检（20260912）。

golden 只测 66 条固定用例、trace_metrics 只测过程指标（轮次/工具/打回）——
开放对话"答得对不对"没有监控：9/8 用户 1 的架构文档跑题连错三轮，两套体系
都不告警，直到用户自己发现。本脚本用纯规则扫真实 trace 补这个盲区——9/8 的
现场（用户「？？？」+ narrator 连续两次道歉自认串台）规则就能抓到。

三条启发式规则（命中 = 人工复审入口，不是判决）：
  R1 HIGH  质疑后认错：用户输入含质疑/纠错词 且 该轮回复含认错/道歉词
  R2 MED   回复自我否定：回复含强自我否定词（串台/又错/读错…），弱信号
  R3 MED   高频拉锯：同一用户 10 分钟内 ≥3 轮对话（来回重试的形态指纹）

巡检非门禁：不置失败标记（避免与 golden 门禁混同制造红斑）。局限：抓的是
"用户有反应/agent 自知"的拉锯；无道歉词的静默跑题（9/8 第 2 轮「读正文」
那样闷头答错的）仍漏——后续可加信号。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/trace_alert.py               # 默认近 7 天
  .venv/bin/python eval/trace_alert.py --days 30
  .venv/bin/python eval/trace_alert.py --from 20260908 --to 20260909
"""
import argparse
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# trace 文件枚举的唯一实现（20260925：生产 trace 改按天分目录，四个读取端共用一处，
# 免得"改了布局漏改一个脚本"= 那天它少看一半数据）。只吃路径参数、不依赖应用配置。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files  # noqa: E402

TRACE_DIR = "/home/ubuntu/memory_blog_rust/logs/agent/traces"

# R1 用户侧：质疑/纠错信号（单独的「？」「？？？」是 9/8 现场的原始形态；
# 「老是」等抱怨词收进——「你老是读什么 git」需要触发）
USER_CHALLENGE = re.compile(
    r"[?？]|不对|不是|错了|错啦|搞错|串台|重来|再读|没读|无语|怎么回事|什么情况|老是")
# 认错/道歉词族（R1 与 R2 共用）
AGENT_ADMIT = re.compile(
    r"抱歉|对不起|不好意思|搞错|串台|又错|错了|我错|失误|没有真正|没能|漏了|看错|读错")
# R2 强自我否定（正常礼貌用「抱歉」不触发，需明确的认错措辞）
AGENT_SELF_DENY = re.compile(r"串台|又错|我(搞|弄|看|读)错|弄错|读错|看错|搞混|认错")

GAP = timedelta(minutes=10)  # R3 拉锯链的最大间隔


def load_trace(path: str) -> dict | None:
    try:
        if path.endswith(".gz"):
            return json.loads(gzip.decompress(open(path, "rb").read()))
        return json.load(open(path))
    except Exception:
        return None


def brief(s: str, n: int = 70) -> str:
    return s.replace("\n", " ")[:n]


def _chain_suspicious(chain: list, hits_stamps: set) -> bool:
    """R3 分级：链内任一条用户消息含质疑信号，或该链已被 R1/R2 命中 → 聚焦展示。"""
    return any(USER_CHALLENGE.search(x["msg"]) or x["stamp"] in hits_stamps for x in chain)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="近 N 天（默认 7；给定 --from 时忽略）")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260908")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260908T235959")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    until = args.until

    records, golden_skipped, scanned = [], 0, 0
    files = iter_trace_files(TRACE_DIR)
    for f in files:
        m = re.search(r"(20\d{6})T(\d{6})_(\d+)_", f)
        if not m:
            continue
        stamp = m.group(1) + "T" + m.group(2)
        if not (since <= stamp <= until):
            continue
        uid = int(m.group(3))
        if uid == 0:  # golden 评测产出（run_golden 内部链路同样落 trace）
            golden_skipped += 1
            continue
        d = load_trace(f)
        if d is None:
            continue
        scanned += 1
        msg = str((d.get("input") or {}).get("message") or "")
        reply = str(d.get("reply") or "")
        if not msg and not reply:
            continue
        records.append({"stamp": stamp, "uid": uid, "msg": msg, "reply": reply})

    # R1 / R2：逐条判定
    hits = []  # {stamp, uid, rule, msg, reply}
    for r in records:
        rules = []
        if USER_CHALLENGE.search(r["msg"]) and AGENT_ADMIT.search(r["reply"]):
            rules.append("R1")
        if AGENT_SELF_DENY.search(r["reply"]):
            rules.append("R2")
        if rules:
            hits.append({**r, "rule": "+".join(rules), "level": "HIGH" if "R1" in rules else "MED"})

    # R3：同一用户按时间切链（间隔 >10 分钟断开），段内 ≥3 条 = 一次拉锯。
    # 分级：链内含质疑信号或已被 R1/R2 命中的 → 聚焦明细（真实事故的形态指纹）；
    # 纯高频（正常连续聊天也常见）→ 只计数摘要，避免噪声淹没信号
    hits_stamps = {h["stamp"] for h in hits}
    r3_focus, r3_noise = [], []
    by_uid = defaultdict(list)
    for r in records:
        by_uid[r["uid"]].append(r)
    for uid, rs in by_uid.items():
        rs.sort(key=lambda x: x["stamp"])
        chain, prev_t = [], None
        for x in rs:
            t = datetime.strptime(x["stamp"], "%Y%m%dT%H%M%S")
            if prev_t and (t - prev_t) > GAP:
                if len(chain) >= 3:
                    (r3_focus if _chain_suspicious(chain, hits_stamps) else r3_noise).append((uid, chain))
                chain = []
            chain.append(x)
            prev_t = t
        if len(chain) >= 3:
            (r3_focus if _chain_suspicious(chain, hits_stamps) else r3_noise).append((uid, chain))

    # ── 输出 ──
    print(f"== trace 语义告警巡检 [{since} → {until}] ==")
    print(f"窗口内真实对话 {scanned} 条（另排除 golden 评测产出 {golden_skipped} 条）\n")

    for h in hits:
        print(f"[{h['level']:4s}] {h['stamp']} u{h['uid']}  {h['rule']}")
        print(f"   用户: {brief(h['msg'])}")
        print(f"   回复: {brief(h['reply'])}\n")
    for uid, chain in r3_focus:
        t0, t1 = chain[0]["stamp"], chain[-1]["stamp"]
        print(f"[MED ] u{uid} {t0[9:13]}–{t1[9:13]}（{t0[:8]}）共 {len(chain)} 条  R3 高频拉锯（含质疑/认错信号）")
        for x in chain:
            print(f"   {x['stamp'][9:]} {brief(x['msg'], 50)}")
        print()
    if r3_noise:
        print("其他高频链（未见质疑/认错信号，可能为正常连续对话，仅计数）:")
        for uid, chain in r3_noise:
            t0, t1 = chain[0]["stamp"], chain[-1]["stamp"]
            print(f"   u{uid} {t0[:8]} {t0[9:13]}–{t1[9:13]} 共 {len(chain)} 条")
        print()

    # 会话线索：按 用户-日 聚合，给人工复审一个入口清单
    clues = defaultdict(lambda: defaultdict(int))
    for h in hits:
        clues[(h["uid"], h["stamp"][:8])][h["rule"]] += 1
    for uid, chain in r3_focus:
        clues[(uid, chain[0]["stamp"][:8])]["R3"] += 1

    n_high = sum(1 for h in hits if h["level"] == "HIGH")
    print(f"=== 命中：HIGH {n_high} / MED {len(hits) - n_high} / 聚焦拉锯链 {len(r3_focus)}"
          f"（另 {len(r3_noise)} 条常规高频链；涉及 {len(clues)} 个用户-日）===")
    if clues:
        print("建议人工复审：")
        for (uid, day), rules in sorted(clues.items()):
            print(f"  u{uid} @ {day}：{' '.join(f'{k}×{v}' for k, v in sorted(rules.items()))}")
    else:
        print("窗口内无命中。")


if __name__ == "__main__":
    main()
