# -*- coding: utf-8 -*-
"""Read-only heuristic review of conversation traces.

Signals identify challenges, repeated apologies and conversation friction
for human review. They do not prove semantic correctness or set release gates.
Planner-specific sections describe archived traces only."""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# Use the shared iterator for plain and compressed trace files.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.settings import settings  # noqa: E402
from utils.trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from utils.trace_io import load_trace  # noqa: E402  （读取的唯一实现，含 gz）

TRACE_DIR = settings.trace_dir

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

# R4 的拒绝来源族（与 `agent/graph.py` 里 `write_target_unresolved` 落 `values=` 那一处
# 的判据同源）：`quote`/`value`/`todo_text`/`grounding` 四支拒的都是"这个值没有出处"。
# `ledger_id` / `policy` 两支拒的是"台账上没有这一行 / 规则不许"——它们与"值有没有出处"
# 无关，把它们的值拿来原话里找必然找不到，是假阳性流水线，故**不收**。
REFUSAL_SOURCES = ("quote", "value", "todo_text", "grounding")

# R5 用的：**现行**五条快道写进 trace 的 `kind`。这不是"词表"——它是本系统自己写的
# **事件名**（属"系统数据"，与 wire 前缀同类；判据层那四类里不含中文语义表）。
# ⚠️ 加第六条快道时这里不改也**不会静默漏**：R5 把窗口里出现过的 kind **全部**列出来，
# 不在这一组里的单独标「不在现行五条里」——所以漏的是"归类"，不是"看不见"。
FASTPATH_KINDS = ("nav", "referent_nav", "display", "article_read", "effect_switch")
_MAX_LIST = 40   # R5 逐条清单的上限（命中本来就少，这是防它哪天变成流水账）


def _squash(s: str) -> str:
    """Remove whitespace for an independent heuristic comparison.

    Keep punctuation, case and width unchanged; stricter matching limits false
    positives and does not import runtime text-normalization behavior."""
    return re.sub(r"\s+", "", str(s or ""))


def brief(s: str, n: int = 70) -> str:
    return s.replace("\n", " ")[:n]


def _chain_suspicious(chain: list, hits_stamps: set) -> bool:
    """R3 分级：链内任一条用户消息含质疑信号，或该链已被 R1/R2 命中 → 聚焦展示。"""
    return any(USER_CHALLENGE.search(x["msg"]) or x["stamp"] in hits_stamps for x in chain)


def _rule_hits(records: list) -> list:
    """R1 / R2：逐条判定 → `[{stamp, uid, msg, reply, rule, level}]`。"""
    hits = []
    for r in records:
        rules = []
        if USER_CHALLENGE.search(r["msg"]) and AGENT_ADMIT.search(r["reply"]):
            rules.append("R1")
        if AGENT_SELF_DENY.search(r["reply"]):
            rules.append("R2")
        if rules:
            hits.append({**r, "rule": "+".join(rules),
                         "level": "HIGH" if "R1" in rules else "MED"})
    return hits


def _r3_chains(records: list, hits_stamps: set):
    """R3：同一用户按时间切链（间隔 >10 分钟断开），段内 ≥3 条 = 一次拉锯 → `(聚焦, 噪声)`。

    分级：链内含质疑信号或已被 R1/R2 命中的 → 聚焦明细（真实事故的形态指纹）；
    纯高频（正常连续聊天也常见）→ 只计数摘要，避免噪声淹没信号。
    """
    focus, noise = [], []
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
                    (focus if _chain_suspicious(chain, hits_stamps) else noise).append(
                        (uid, chain))
                chain = []
            chain.append(x)
            prev_t = t
        if len(chain) >= 3:
            (focus if _chain_suspicious(chain, hits_stamps) else noise).append((uid, chain))
    return focus, noise


def _refusal_hits(doc: dict, stamp: str, uid: int, msg: str):
    """Collect target-provenance refusals and their readable values.

    Missing values are not empty targets. Report structured, archived-text and
    unreadable counts separately so absent evidence cannot look like zero defects."""
    cands, seen, readable, legacy = [], 0, 0, 0
    sq_msg = _squash(msg)
    for ev in (doc.get("events") or []):
        if (ev.get("node"), ev.get("event")) != ("planner", "write_target_unresolved"):
            continue
        if ev.get("source") not in REFUSAL_SOURCES:
            continue
        seen += 1
        vals = [str(v) for v in (ev.get("values") or []) if str(v).strip()]
        old = False
        if vals:
            readable += 1
        else:
            vals = [m for m in re.findall(r"「([^」]{1,60})」",
                                          str(ev.get("reason") or ""))]
            old = bool(vals)
            if old:
                legacy += 1
        found = [v for v in vals if sq_msg and _squash(v) in sq_msg]
        if found:
            cands.append({"stamp": stamp, "uid": uid, "msg": msg,
                          "tool": str(ev.get("tool") or ""),
                          "source": str(ev.get("source") or ""),
                          "values": vals, "found": found, "legacy": old})
    return cands, seen, readable, legacy


def _print_refusals(r4: list, seen: int, readable: int, legacy: int) -> None:
    """Print refusal candidates with the rejected value and corresponding user request."""
    print(f"R4 出处闸：窗口内拒绝 {seen} 次，其中**读得到被拒字面**的 {readable} 次"
          + (f"（另 {legacy} 次走旧 trace 的 `reason` 引号降级读）" if legacy else "")
          + f"，候选误拒 {len(r4)} 条")
    for x in r4:
        tag = "（旧trace·读reason）" if x["legacy"] else ""
        print(f"[MED ] {x['stamp']} u{x['uid']}   R4{tag} tool={x['tool']}"
              f" source={x['source']}  命中 {x['found']}/{x['values']}")
        print(f"   主人: {brief(x['msg'])}\n")
    print()


def _fastpath_rows(doc: dict, stamp: str, uid: int, conv: int, msg: str) -> list[dict]:
    """Read archived fastpath events and associated planner calls and fallbacks.

    A hit does not prove a zero-model-call turn. Missing configuration fields
    remain unknown rather than being treated as enabled."""
    hits, llm_done, fallback, dial = [], 0, False, None
    for ev in (doc.get("events") or []):
        key = (ev.get("node"), ev.get("event"))
        if key == ("planner", "fastpath"):
            hits.append({"stamp": stamp, "uid": uid, "conv": conv, "msg": msg,
                         "kind": str(ev.get("kind") or ""),
                         "tools": [str(t) for t in (ev.get("tools") or [])]})
        elif key == ("planner", "llm_done"):
            llm_done += 1
        elif key == ("gate", "fallback"):
            fallback = True
        elif key == ("planner", "context"):
            dial = ev.get("fastpath")          # 缺键 = 本档上线前的存量（**不是**"开"）
    for h in hits:
        h["llm_done"] = llm_done
        h["fallback"] = fallback
        h["dial"] = dial
    return hits


def _reply_kind_rows(doc: dict, stamp: str, uid: int, msg: str) -> list[dict]:
    """Read archived model-declared short-reply kinds and pending-target availability.

    An authorization reading without a target is a review candidate, not a refusal rule."""
    out = []
    for ev in (doc.get("events") or []):
        if (ev.get("node"), ev.get("event")) != ("planner", "reply_kind"):
            continue
        out.append({"stamp": stamp, "uid": uid, "msg": msg,
                    "kind": str(ev.get("kind") or ""),
                    "short": ev.get("short"), "target": ev.get("target"),
                    "round": ev.get("round")})
    return out


def _print_reply_kind(rows: list, scanned: int) -> None:
    """Display archived short-reply interpretations for human review without deciding intent."""
    if not rows:
        print("R6 短应答申报：窗口内无 `planner.reply_kind` 事件"
              "（⚠️ 这条 20261015 才上线 ⇒ 窗口跨上线日时，前段没有是正常的）\n")
        return
    by_kind: dict = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    over = [r for r in rows if r["kind"] == "auth" and not r["target"]]
    n_nonshort = sum(1 for r in rows if r["short"] is False)
    print(f"R6 短应答申报：窗口内 {scanned} 轮里模型申报了 {len(rows)} 次"
          f"（{', '.join(f'{k}×{v}' for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]))}）")
    print(f"   其中**过度授权候选**（申报 auth 而本轮无台账目标）：{len(over)} 次"
          "  ← 只列不判：主人当场交权 + 确实没有等着办的事，零写收尾本就是对的那条路")
    print(f"   另有 {n_nonshort} 次申报发生在**非短消息**上（提示块写的是「当前消息不是"
          "短应答」⇒ 按纪律不该填；填了说明模型没照块读，值一看）")
    for r in over[:_MAX_LIST]:
        print(f"   过度授权候选 {r['stamp']} u{r['uid']} round={r['round']}  "
              f"主人: {brief(r['msg'], 40)}")
    if len(over) > _MAX_LIST:
        print(f"   …另有 {len(over) - _MAX_LIST} 条未列")
    print()


def _next_turn_challenged(row: dict, seq: dict) -> bool:
    """命中那一轮的**下一条**（同一用户同一会话）里，主人有没有质疑/纠错信号。

    复用 R1 的 `USER_CHALLENGE`（同一个词表、同一个含义，不再抄第二份）。口径上的
    两处诚实：① 排序靠 trace 文件名里的时间戳（同一秒的先后靠文件名，可能反）；
    ② 会话 id 缺失（旧 trace）时归到 0 —— 那一段的"下一轮"只能同用户粗配。
    """
    chain = seq.get((row["uid"], row["conv"])) or []
    for i, (stamp, _msg) in enumerate(chain):
        if stamp == row["stamp"] and i + 1 < len(chain):
            return bool(USER_CHALLENGE.search(chain[i + 1][1] or ""))
    return False


def _print_fastpath(rows: list, dials: dict, scanned: int, seq: dict) -> None:
    """Display archived fastpath metrics; the current runtime has no fastpath switch."""
    if not rows:
        print("R5 快道定价：窗口内无 `planner.fastpath` 命中（⚠️ 这不等于五条都没用——"
              "先把 `planner.context` 的档位格数一遍再看）\n")
        return
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    _n = {"on": dials.get("on", 0), "off": dials.get("off", 0), "none": dials.get(None, 0)}
    print(f"R5 快道定价：窗口内真实对话 {scanned} 轮，快道命中 {len(rows)} 次"
          f"（{len(by_kind)} 种 kind）")
    print(f"   档位格：开 {_n['on']} / 关 {_n['off']} / **没有这一格** {_n['none']}"
          "（没有 = 本档上线前的存量 trace，不是'开'）")
    for kind, rs in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        extra = "" if kind in FASTPATH_KINDS else "  ← **不在现行五条里**（已删的快道 or 新加的？）"
        n_fb = sum(1 for r in rs if r["fallback"])
        n_ch = sum(1 for r in rs if _next_turn_challenged(r, seq))
        n_extra = sum(1 for r in rs if r["llm_done"] > 1)      # 比下限 1 多花的那几次
        tools = sorted({len(r["tools"]) for r in rs})
        print(f"   kind={kind:18s} 命中 {len(rs):3d} 次 | 本轮工具数 {tools} | "
              f"命中后另有后续意图（planner 调用 >1 次）{n_extra} 次 | "
              f"gate 打回 {n_fb} 次 | 下一轮主人质疑 {n_ch} 次{extra}")
    print(f"   —— 未在现行五条里的 kind 已在上表标出；现行五条里**窗口内一次都没命中**的："
          f"{[k for k in FASTPATH_KINDS if k not in by_kind] or '无'}")
    # 逐条摊开**必须**有：命中是"这条快道替主人做了这件事"，而"该不该替"是语义判断，
    # 机器在这一层判不了（判得了的只有上面那几个结构数）。R4 同一体例。
    print(f"   （命中逐条列出，供人工看'这次命中该不该'；共 {len(rows)} 条"
          + (f"，只列前 {_MAX_LIST}" if len(rows) > _MAX_LIST else "") + "）")
    for r in rows[:_MAX_LIST]:
        print(f"     {r['stamp']} u{r['uid']:<4d} [{r['kind']}] "
              f"{brief(r['msg'], 40)}  →  {brief('、'.join(r['tools']), 60)}")
    if len(rows) > _MAX_LIST:
        print(f"     …另有 {len(rows) - _MAX_LIST} 条未列")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=TRACE_DIR,
                    help="Trace root; defaults to TRACE_DIR configuration")
    ap.add_argument("--days", type=int, default=7, help="近 N 天（默认 7；给定 --from 时忽略）")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260908")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260908T235959")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    until = args.until

    records, golden_skipped, scanned = [], 0, 0
    # Collect refusal evidence separately from the conversation-frequency records.
    r4, r4_seen, r4_readable, r4_legacy = [], 0, 0, 0
    # Collect archived fastpath events and configuration separately.
    r5, dials, seq = [], defaultdict(int), defaultdict(list)
    # Collect archived model-declared short-reply interpretations.
    r6: list = []
    files = iter_trace_files(args.dir)
    for f in files:
        stamp, uid_s = parse_trace_name(f)
        if not stamp or not (since <= stamp <= until):
            continue
        uid = int(uid_s)
        if uid == 0:  # golden 评测产出（run_golden 内部链路同样落 trace）
            golden_skipped += 1
            continue
        d = load_trace(f)
        if d is None:
            continue
        scanned += 1
        _in = d.get("input") or {}
        msg = str(_in.get("message") or "")
        reply = str(d.get("reply") or "")
        _conv = int(_in.get("conversation_id") or 0)
        seq[(uid, _conv)].append((stamp, msg))
        r5.extend(_fastpath_rows(d, stamp, uid, _conv, msg))
        r6.extend(_reply_kind_rows(d, stamp, uid, msg))
        for ev in (d.get("events") or []):
            if (ev.get("node"), ev.get("event")) == ("planner", "context"):
                dials[ev.get("fastpath")] += 1     # 缺键 ⇒ 计到 None 那一档（存量）
        _cands, _seen, _read, _legacy = _refusal_hits(d, stamp, uid, msg)
        r4.extend(_cands)
        r4_seen += _seen
        r4_readable += _read
        r4_legacy += _legacy
        if not msg and not reply:
            continue
        records.append({"stamp": stamp, "uid": uid, "msg": msg, "reply": reply})

    hits = _rule_hits(records)                      # R1 / R2
    hits_stamps = {h["stamp"] for h in hits}
    r3_focus, r3_noise = _r3_chains(records, hits_stamps)   # R3

    # ── 输出 ──
    print(f"== trace 语义告警巡检 [{since} → {until}] ==")
    print(f"窗口内真实对话 {scanned} 条（另排除 golden 评测产出 {golden_skipped} 条）\n")

    for h in hits:
        print(f"[{h['level']:4s}] {h['stamp']} u{h['uid']}  {h['rule']}")
        print(f"   用户: {brief(h['msg'])}")
        print(f"   回复: {brief(h['reply'])}\n")
    _print_refusals(r4, r4_seen, r4_readable, r4_legacy)
    _print_fastpath(r5, dials, scanned, seq)
    _print_reply_kind(r6, scanned)
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
    for x in r4:
        clues[(x["uid"], x["stamp"][:8])]["R4"] += 1

    n_high = sum(1 for h in hits if h["level"] == "HIGH")
    print(f"=== 命中：HIGH {n_high} / MED {len(hits) - n_high} / 聚焦拉锯链 {len(r3_focus)}"
          f" / 出处候选误拒 {len(r4)}（拒绝 {r4_seen} 次、读到字面 {r4_readable} 次）"
          f"（另 {len(r3_noise)} 条常规高频链；涉及 {len(clues)} 个用户-日）===")
    if clues:
        print("建议人工复审：")
        for (uid, day), rules in sorted(clues.items()):
            print(f"  u{uid} @ {day}：{' '.join(f'{k}×{v}' for k, v in sorted(rules.items()))}")
    else:
        print("窗口内无命中。")


if __name__ == "__main__":
    main()
