# -*- coding: utf-8 -*-
"""真实 trace 现场 → golden 用例**草稿**（20260921 拍板：每晚回灌、人审后入库）。

动机：golden set 的用例全靠手工回想写成——真实对话里已经烧过的事故（用户质疑、
narrator 认错、拉锯重试）跑完就散了，只有 trace_alert 的告警行留个印记，没人把
它养成回归用例。于是同一个坑可以再踩第二次（9/8 跑题、9/20 repeat_ask 复读都是
"现场在 trace 里、用例集里没有"）。本脚本把 trace_alert 命中的现场机械地整理成
**候选用例骨架 + 人审对照单**，把"想起来补一条"变成"每晚有一份待办清单"。

边界（三条硬约束，别越）：
  1. **只产草稿，绝不自动入库**：本脚本不碰 `eval/golden/basic.jsonl`（写它 =
     未审的真实用户文本进公开仓库 + 判据未经人看就变成门禁）。草稿一律落在
     `eval/report/`（.gitignore 已整体忽略），文件名 `golden_drafts_<ts>.*`。
  2. **判据不猜**：骨架里只写**确定性可得**的东西（真实 user_input、从 trace 解析
     的页面上下文、观测到的工具/打回/回复）。断言留空——"期望什么行为"必须由人
     判（很多现场的正确行为是"该拒答"而不是"该检索"，猜错了就是一条假判据）。
     人审单按观测证据给出**候选方向**，明确标注"待判"。
  3. **不做 LLM 判定**：与 trace_alert 同族——纯规则，跑多久都不花钱，也就不会有人
     为了省钱去调它。

与 trace_alert 的分工：trace_alert = 发现现场（R1 质疑后认错 / R2 回复自我否定 /
R3 高频拉锯），人看告警；本脚本 = 把同一批现场转成**待补用例的骨架与对照单**。
规则本体从 trace_alert import（单一事实来源，两边不许各写一份）。

人审 → 入库 的流程（四步，别跳步）：
  1. 看 `eval/report/golden_drafts_<ts>.md`：逐条判"这是真事故还是正常对话"、
     "正确行为是什么"（答对?该拒答?该检索?），在复选框上勾；
  2. 把要留的条目的 `user_input` / `context` 抄成一条用例，**断言按第 1 步的判定写**
     （优先帧级断言 require_tool_calls_any / no_tool_calls，其次文本断言；
     撤回类现场记得 not_contains_exempt_quote）；
  3. `id`/`tags` 自己起（事故类现场一律打 `regression`——回归组要求 100% 通过，
     见 run_golden.py 门禁；纯能力题不要打）；
  4. 先 `--only <新id>` 单跑一条验判据（真红 = 判据或行为有问题，别直接收工），
     再进全量。判据改了要同步 `eval/judge_offline_test.py` 的语料。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/golden_draft.py                 # 近 1 天（夜间用的口径）
  .venv/bin/python eval/golden_draft.py --days 7        # 补作业：近一周现场
  .venv/bin/python eval/golden_draft.py --from 20260918 --to 20260919
  .venv/bin/python eval/golden_draft.py --limit 5       # 只出前 5 条（别人审单太长）
"""
import argparse
import difflib
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace_alert as ta  # noqa: E402  （规则本体与 trace 读取的唯一来源）

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "basic.jsonl")
OUT_DIR = "eval/report"

# 页面上下文解析（planner/context 事件的 page_ctx 首行形如
# `user_id=17, page=https://saudade.site/, title=首页; current_effects=none; current_darkmode=off`）
_PAGE_RE = re.compile(r"page=(\S+?), title=([^;]+);")
_EFFECT_RE = re.compile(r"current_effects=([^;]+);")
_DARK_RE = re.compile(r"current_darkmode=([a-z]+)")   # 后面还跟着 `;`（见 page_ctx 原文）


def parse_page_ctx(events: list) -> dict:
    """从 planner/context 事件重建 golden 用例的 `context` 字段（取不到就给首页默认）。"""
    ctx = {"current_url": "/", "page_title": "首页",
           "current_effects": "none", "current_darkmode": "off"}
    for e in events:
        p = str(e.get("page_ctx") or "")
        if not p:
            continue
        m = _PAGE_RE.search(p)
        if m:
            url = m.group(1)
            ctx["current_url"] = "/" + url.split("saudade.site/", 1)[-1] if "saudade.site" in url else url
            ctx["page_title"] = m.group(2).strip()
        me = _EFFECT_RE.search(p)
        if me:
            ctx["current_effects"] = me.group(1).strip()
        md = _DARK_RE.search(p)
        if md:
            ctx["current_darkmode"] = md.group(1).strip()
        break
    return ctx


def build_record(path: str, d: dict, stamp: str, uid: int) -> dict:
    """一条候选现场：保留人工判定所需的全部确定性证据。"""
    evs = d.get("events") or []
    tools, gate, resets = [], [], []
    skill = ""
    for e in evs:
        if e.get("event") == "call":
            n = str(e.get("name") or "")
            if n and n not in tools:
                tools.append(n)
        elif e.get("event") == "decision" and e.get("skill"):
            skill = str(e["skill"])
        elif e.get("node") == "gate":
            if e.get("event") == "check":
                gate.append(f"check(skill={e.get('skill')}, frames={e.get('frames')})")
            elif e.get("event") == "pass":
                gate.append(f"pass(零工具轮)" if e.get("zero_frame") else "pass(有帧)")
            else:  # 打回类：issue 名由 trace 的 end_reason/fallback 记录承载
                gate.append(str(e.get("event")))
    if d.get("end_reason"):
        resets.append(str(d["end_reason"]))
    return {
        "path": path, "stamp": stamp, "uid": uid,
        "msg": str((d.get("input") or {}).get("message") or ""),
        "reply": str(d.get("reply") or ""),
        "skill": skill, "tools": tools, "gate": gate, "end_reason": resets,
        "context": parse_page_ctx(evs),
        "trace_id": d.get("trace_id"),
    }


def load_golden_inputs() -> dict:
    """已有用例的 user_input（去重提示用：现场可能已被某条用例覆盖）。"""
    out = {}
    for line in open(GOLDEN, encoding="utf-8"):
        if not line.strip():
            continue
        c = json.loads(line)
        out[c["id"]] = str(c.get("user_input") or "")
    return out


def nearest_case(msg: str, gold_inputs: dict) -> tuple[str, float]:
    """最相近的已有用例（字符相似度）——只为提示"可能已覆盖"，不做自动判定。"""
    best, ratio = "", 0.0
    for cid, inp in gold_inputs.items():
        r = difflib.SequenceMatcher(None, msg, inp).ratio()
        if r > ratio:
            best, ratio = cid, r
    return best, round(ratio, 2)


def _norm_msg(msg: str) -> str:
    """消息归一（去空白与标点）：同一句话在拉锯里会被反复问，输出前按它折叠。"""
    return re.sub(r"[\s。，、？！,.?!~～；;：:]+", "", msg or "")


def hint_lines(rec: dict) -> list:
    """候选断言方向（**待判**，只是把人审时最容易漏的观测证据摆出来）。"""
    h = []
    if rec["tools"]:
        h.append(f"本轮真跑过工具 {rec['tools']} → 若期望「必须真查」，可挂 "
                 f"`require_tool_calls_any`（帧级断言优于文本断言）")
    else:
        h.append("本轮**零工具** → 若期望「必须真查」，挂 `require_tool_calls_any`；"
                 "若期望「该如实说没查过」，挂 `no_tool_calls` + 文本断言")
    if any("fallback" in g for g in rec["gate"]):
        h.append("本轮触发了 gate fallback → 现场的正确行为可能正是**该被打回**，"
                 "此类用例挂 `forbid_fallback`（防「走了兜底却判 PASS」）")
    if rec["reply"]:
        h.append("回复里若有**引述自己上一轮**的措辞，文本禁词断言记得开 "
                 "`not_contains_exempt_quote`（否则撤回话术会被当成重新声称）")
    h.append("起名与打标签：事故类现场一律带 `regression`（回归组要求 100% 通过）；"
             "纯能力题不要打——混进回归组会让门禁天天红")
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="近 N 天（默认 1；给 --from 时忽略）")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260918")
    ap.add_argument("--to", dest="until", default="99999999", help="终点日期 20260919")
    ap.add_argument("--limit", type=int, default=0, help="只出前 N 条（0=不限）")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    until = args.until

    files = sorted(glob.glob(ta.TRACE_DIR + "/*.json")) + sorted(glob.glob(ta.TRACE_DIR + "/*.gz"))
    candidates, scanned, golden_skipped = [], 0, 0
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
        d = ta.load_trace(f)
        if d is None:
            continue
        scanned += 1
        msg = str((d.get("input") or {}).get("message") or "")
        reply = str(d.get("reply") or "")
        if not msg or not reply:
            continue
        rules = []
        if ta.USER_CHALLENGE.search(msg) and ta.AGENT_ADMIT.search(reply):
            rules.append("R1")
        if ta.AGENT_SELF_DENY.search(reply):
            rules.append("R2")
        if rules:
            rec = build_record(f, d, stamp, uid)
            rec["rules"] = "+".join(rules)
            candidates.append(rec)

    # R3 高频拉锯（同一用户 10 分钟内 ≥3 轮）：链内任一条就是候选现场——真事故
    # 常是"第一轮答跑题、后面全是拉锯"，被质疑的那一轮才带 R1 信号，链头才是根因
    by_uid = defaultdict(list)
    for f in files:
        m = re.search(r"(20\d{6})T(\d{6})_(\d+)_", f)
        if not m:
            continue
        stamp = m.group(1) + "T" + m.group(2)
        if not (since <= stamp <= until):
            continue
        uid = int(m.group(3))
        if uid == 0:
            continue
        d = ta.load_trace(f)
        if d is None:
            continue
        by_uid[uid].append((stamp, f, d))
    seen = {(c["stamp"], c["uid"]) for c in candidates}
    hits_stamps = {c["stamp"] for c in candidates}  # 已被 R1/R2 命中的轮次（R3 分级用）
    for uid, rows in by_uid.items():
        rows.sort(key=lambda x: x[0])
        chain, prev_t = [], None
        for stamp, f, d in rows:
            t = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
            if prev_t and (t - prev_t) > ta.GAP:
                if len(chain) >= 3:
                    _add_chain(candidates, seen, chain, uid, hits_stamps)
                chain = []
            chain.append((stamp, f, d))
            prev_t = t
        if len(chain) >= 3:
            _add_chain(candidates, seen, chain, uid, hits_stamps)

    # 折叠同一句话的重复现场，再按"信号强度 + 时间"排序：R1（用户质疑 + agent 认错）
    # 在最前，R2 次之，R3 垫底——人审单是给人看的，顺序就是优先级
    candidates = dedupe_across(candidates)
    candidates.sort(key=lambda r: (0 if "R1" in r["rules"] else 1 if "R2" in r["rules"]
                                   else 2, r["stamp"]))
    if args.limit:
        candidates = candidates[: args.limit]

    gold_inputs = load_golden_inputs()
    os.makedirs(OUT_DIR, exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = f"{OUT_DIR}/golden_drafts_{ts_str}.jsonl"
    md_path = f"{OUT_DIR}/golden_drafts_{ts_str}.md"

    drafts = []
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in candidates:
            cid = f"draft_{rec['stamp']}_u{rec['uid']}"
            near, ratio = nearest_case(rec["msg"], gold_inputs)
            draft = {
                "id": cid,
                "tags": ["draft"],           # 人审时改：事故类补 "regression"
                "user_input": rec["msg"],
                "context": rec["context"],
                "gold": {"nonempty": True},  # 只放确定性可得项，断言由人写（见文件头）
                "_draft": {
                    "rules": rec["rules"], "trace_id": rec["trace_id"],
                    "skill": rec["skill"], "tools": rec["tools"],
                    "gate": rec["gate"], "end_reason": rec["end_reason"],
                    "observed_reply": rec["reply"],
                    "nearest_case": near, "nearest_ratio": ratio,
                    "also_seen": rec.get("also_seen") or [],
                    "note": "草稿：断言留空是刻意的（正确行为需人判，见 golden_draft.py 头注释）",
                },
            }
            drafts.append(draft)
            f.write(json.dumps(draft, ensure_ascii=False) + "\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# golden 草稿人审单 {ts_str}\n\n")
        f.write(f"窗口 {since} → {until}：扫到真实对话 {scanned} 条"
                f"（另排除 golden 评测产出 {golden_skipped} 条），候选现场 {len(drafts)} 条。\n\n")
        f.write("**这不是新用例，是待判定现场。**逐条判：真事故还是正常对话？正确行为是"
                "什么（答对 / 该拒答 / 该先检索）？勾选后按文件头四步入库；"
                "**只产草稿不自动入库**——判据未经人看就进门禁 = 制造假红。\n\n"
                "两条先验（省人审时间）：用户粘贴进来的 trace/日志片段（形如"
                "`START | planner | 有工具计划 v execute`）不是用例素材，直接丢；"
                "「相近已有用例」相似度 ≥0.9 的先确认是不是已覆盖，别重复入库。\n\n")
        if not drafts:
            f.write("窗口内无候选现场。\n")
        for d in drafts:
            dr = d["_draft"]
            f.write(f"## {d['id']}\n\n")
            f.write(f"- 规则：{dr['rules']}｜planner 技能：{dr['skill'] or '（无决策事件）'}"
                    f"｜退出原因：{dr['end_reason'] or '正常'}\n")
            f.write(f"- 本轮工具：{dr['tools'] or '**零工具**'}｜gate：{dr['gate']}\n")
            f.write(f"- 页面上下文：`{json.dumps(d['context'], ensure_ascii=False)}`\n")
            f.write(f"- 相近已有用例：`{dr['nearest_case']}`（相似度 {dr['nearest_ratio']}）"
                    f"——相似度高先确认是不是已覆盖\n")
            if dr["also_seen"]:
                f.write(f"- 同一句话在别处也出现过：{dr['also_seen']}"
                        f"——重复出现 = 历史上没答好过，补用例的强信号\n")
            f.write("\n")
            f.write(f"**用户**：{d['user_input']}\n\n**agent 回复**：\n\n> "
                    + dr["observed_reply"].replace("\n", "\n> ") + "\n\n")
            f.write("候选断言方向（**待判**，不是结论）：\n\n")
            for h in hint_lines({"tools": dr["tools"], "gate": dr["gate"], "reply": dr["observed_reply"]}) :
                f.write(f"- {h}\n")
            f.write("\n- [ ] 该入库（id/tags/断言已填，先 `--only <id>` 单跑验判据）\n"
                    "- [ ] 已有用例覆盖 → 丢弃（写明哪条）\n"
                    "- [ ] 正常对话非事故 → 丢弃\n\n---\n\n")
        f.write("\n## 入库步骤（抄自文件头，防手滑）\n\n"
                "1. 用例追加进 `eval/golden/basic.jsonl`（一行一条 JSON）；\n"
                "2. 事故类现场打 `regression` 标签（回归组硬判 100%）；\n"
                "3. `.venv/bin/python eval/run_golden.py --only <新id>` 单跑验判据；\n"
                "4. 判据改动同步 `eval/judge_offline_test.py` 语料；草稿文件本身**不进 git**"
                "（`eval/report/*` 已忽略，且含真实用户文本）。\n")

    print(f"== golden 草稿回灌 [{since} → {until}] ==")
    print(f"窗口内真实对话 {scanned} 条（排除 golden 产出 {golden_skipped} 条）→ 候选 {len(drafts)} 条")
    for d in drafts:
        dr = d["_draft"]
        print(f"  [{dr['rules']:5s}] {d['id']}  {ta.brief(d['user_input'], 40)}"
              f"  → 相近 {dr['nearest_case']}({dr['nearest_ratio']})")
    print(f"草稿: {jsonl_path}")
    print(f"人审单: {md_path}")
    print("提醒：只产草稿不自动入库（人审后手抄进 eval/golden/basic.jsonl）；"
          "草稿含真实用户文本，不得进 git。")


def _add_chain(candidates: list, seen: set, chain: list, uid: int,
               hits_stamps: set) -> None:
    """R3 拉锯链 → 候选现场。

    两道过滤，都是被噪声逼出来的（首版把每条链的每一轮都出成草稿，一天几十条
    重复项，人审单直接没人看）：
      * **只留可疑链**（链内出现质疑信号或已被 R1/R2 命中）——纯高频也可能是正常
        连聊，trace_alert 早就分开了 focus/noise 两档，这里沿用同一判据；
      * **同链内按归一化消息折叠**（同一句话被追问 3 次只出一条，重问次数记进
        `repeat`）——重问本身是"没答好"的强信号，但不需要三条一样的草稿。
    """
    rows = [{"stamp": s, "msg": str((d.get("input") or {}).get("message") or ""),
             "reply": str(d.get("reply") or ""), "f": f, "d": d}
            for s, f, d in chain]
    if not ta._chain_suspicious(rows, hits_stamps):
        return
    by_msg: dict = {}
    for r in rows:
        key = _norm_msg(r["msg"])
        if not key:
            continue
        if key in by_msg:
            by_msg[key]["repeat"] += 1
            continue
        by_msg[key] = {"row": r, "repeat": 1}
    for key, item in by_msg.items():
        r = item["row"]
        if (r["stamp"], uid) in seen:
            continue
        rec = build_record(r["f"], r["d"], r["stamp"], uid)
        rec["rules"] = f"R3(链 {len(chain)} 条，同句追问 ×{item['repeat']})"
        candidates.append(rec)
        seen.add((r["stamp"], uid))


def dedupe_across(candidates: list) -> list:
    """跨链折叠同一句话（不同会话里问同一件事）：留最早一条，其余记进 also_seen。

    同一句用户输入重复出现 = 这条问题历史上没被答好过（或用户没看懂），
    是补用例的强信号；但草稿只需要一条。
    """
    first: dict = {}
    out = []
    for rec in candidates:
        key = _norm_msg(rec["msg"])
        if key and key in first:
            first[key]["also_seen"].append(f"{rec['stamp']}u{rec['uid']}")
            continue
        rec["also_seen"] = []
        if key:
            first[key] = rec
        out.append(rec)
    return out


if __name__ == "__main__":
    main()
