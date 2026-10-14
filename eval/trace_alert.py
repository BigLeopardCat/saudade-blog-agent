# -*- coding: utf-8 -*-
"""真实对话语义告警巡检（20260912）。

golden 只测 66 条固定用例、trace_metrics 只测过程指标（轮次/工具/打回）——
开放对话"答得对不对"没有监控：9/8 用户 1 的架构文档跑题连错三轮，两套体系
都不告警，直到用户自己发现。本脚本用纯规则扫真实 trace 补这个盲区——9/8 的
现场（用户「？？？」+ narrator 连续两次道歉自认串台）规则就能抓到。

四条启发式规则（命中 = 人工复审入口，不是判决）：
  R1 HIGH  质疑后认错：用户输入含质疑/纠错词 且 该轮回复含认错/道歉词
  R2 MED   回复自我否定：回复含强自我否定词（串台/又错/读错…），弱信号
  R3 MED   高频拉锯：同一用户 10 分钟内 ≥3 轮对话（来回重试的形态指纹）
  R4 MED   出处候选误拒：写目标被出处闸拒了，而被拒的那个**字面就在主人原话里**
           （判据读事件的结构化 `values=`，不读拒绝文案——文案一改就瞎）
  R5  —    快道定价（20261015）：五条确定性快道各命中几次、每次的价钱（命中轮的工具数、
           命中后还问了 planner 几次、有没有 gate 打回、下一轮主人有没有质疑）。
           这一节不是告警，是 `AGENT_FASTPATH` 那一档的**取数出口**——删/收窄一条
           快道要拿它 + golden 两臂说话，不能纸面推定。

巡检非门禁：不置失败标记（避免与 golden 门禁混同制造红斑）。局限：抓的是
"用户有反应/agent 自知"的拉锯；无道歉词的静默跑题（9/8 第 2 轮「读正文」
那样闷头答错的）仍漏——后续可加信号。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/trace_alert.py               # 默认近 7 天
  .venv/bin/python eval/trace_alert.py --days 30
  .venv/bin/python eval/trace_alert.py --from 20260908 --to 20260909
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# trace 文件枚举的唯一实现（20260925：生产 trace 改按天分目录，四个读取端共用一处，
# 免得"改了布局漏改一个脚本"= 那天它少看一半数据）。只吃路径参数、不依赖应用配置。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from trace_io import load_trace  # noqa: E402  （读取的唯一实现，含 gz）

TRACE_DIR = "/home/ubuntu/Saudade-Blog/logs/agent/traces"

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
    """去空白（R4 用的那一把尺子）。

    **刻意不复用 `agent/textnorm.norm`**（那里还有全角→半角 + `casefold`），两个理由：
      ① 引它要 `import agent.textnorm`，而包名解析走的是 **.venv 里可编辑安装指向的
         那个 checkout**——在 worktree 里跑本脚本会读**产线那份代码**（本仓踩过这个
         假红：套件在 worktree 里读产线 checkout）；按路径直载又要多一层兜底与静默分支。
      ② R4 是**独立巡检**，比判据更严只会更少命中、不会造出"系统自己判自己"的循环
         证据——全角/大小写造成的漏报，人眼在下面那行原样打印的 `values=` 里看得见。
    """
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
    """一份 trace 里的**出处族拒绝** → `(候选, 拒绝次数, 读得到字面的次数, 旧trace降级次数)`。

    判据在事件里：`write_target_unresolved` 且 `source` 在 `REFUSAL_SOURCES` 内（= 拒的
    是"这个值没有出处"），被拒的那个**字面**取结构化 `values=`（与 `write_value_unresolved`
    同形，见 `graph._decide_write_guard`）。**缺键 ≠ 空**：`values` 不在场就跳过这条，
    不当成"拒了个空值"。

    三个计数**必须都印出来**（见 `eval/trace_io.py` 头注那条纪律）："候选误拒 0"与
    "一次都没读到被拒的字面"是两件事，只印前者会把后者读成绿。`readable` 与 `legacy`
    之和才是"读到了字面"的完整口径——20261014 之前的 trace 只把值写在 `reason` 那段
    散文里，退一步读它的「」是这段历史窗口的临时通道；新 trace 有 `values=` 就再也不走它。
    """
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
    """R4 那一节（**这一节是 20261014 那股改造的验收判据之一**）。

    判据在事件里、不在这段打印里；这里只把候选摊开给复审的人看：被拒的字面 + 那一轮
    主人的原话（两者都在场，才判得出"是不是误拒"）。
    """
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
    """一份 trace → 快道命中行（R5 的原料：**命中次数 + 命中后那一轮的结构信号**）。

    判据全在**结构化事件**里，一个字的文案都不读：
      · 命中 = `planner.fastpath`，它的 `kind=`／`tools=`（工具数 = 这一轮快道替主人做了几件）；
      · 本轮的 planner 调用次数 = `planner.llm_done` 的条数。⚠️ **别把它读成"快道省了几次
        LLM"**：execute 跑完控制权一律回到 planner，所以命中轮恒有 ≥1 次——"零 LLM"说的是
        **命中那一刻**不叫模型，不是"整轮不叫"。有意义的是**比 1 多出来的那几次**：那才是
        快道没接住的后续意图（trace 20260923T123437 主人说「把这篇文章收藏了」，快道先读了
        一遍文章，planner 紧接着又为收藏跑了一轮）；
      · 打回 = `gate.fallback`（打回 = 主人看到的是兜底文本，是命中的代价之一）；
      · 档位 = `planner.context` 的 `fastpath` 格。**缺那一格 ≠ 开档**：它是本档
        上线（20261015）之前的存量 trace，与"开了但没命中"必须分开数。
    """
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
    """一份 trace → 短应答**自我申报**行（R6 的原料；20261015 批 3 的取数口）。

    `planner.reply_kind` 是模型对这一轮那句短话读法的**唯一**读数口（三张手写词表
    删掉之后，系统侧不再有第二份判断可以对照）。两个字段缺一不可：
      · `kind` —— 模型读成 pos/neg/auth 里的哪一类（认不出的写法原样留 16 字）；
      · `target` —— **系统这一轮手里有没有可承接的目标**（台账帧非空）。
    `kind=auth` 而 `target=False` 就是"模型以为主人把决定权交出来了、系统手里却一件
    都拿不出来"——旧版由词表在这里兜底，现在这条只记账、由人复核（**不拦截**）。
    """
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
    """R6 那一节（短应答自我申报的取数出口）。

    不定结论：这一节摊开的是"模型把短话读成了哪一类、当时手里有没有东西可给"。
    「过度授权候选」（auth + 无目标）**只列不判**——它也可能是主人当场把决定权交出来、
    而这一轮确实没有等着办的事（那种情况零写收尾才是对的）。判"该不该"是人的事。
    """
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
    """R5 那一节（**20261015 快道定价档的取数出口**）。

    这一节不定结论，只把"命中几次、每次的价钱"摊开。读的人要拿它与
    `AGENT_FASTPATH=1/0` 两臂的 golden 对照（跑法见 `config/settings.py` 那条注）。
    ⚠️ 窗口里若"关档轮次"不为 0，说明有人拨过档，这一节的读数是两档混着的。
    """
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
    ap.add_argument("--days", type=int, default=7, help="近 N 天（默认 7；给定 --from 时忽略）")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260908")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260908T235959")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    until = args.until

    records, golden_skipped, scanned = [], 0, 0
    # R4 的候选（20261014）：**独立一趟扫事件**，不往 `records` 里塞 events——R3 的链
    # 只吃 stamp/uid/msg，而 events 是每份 trace 里最大的一块。
    r4, r4_seen, r4_readable, r4_legacy = [], 0, 0, 0
    # R5（20261015）：命中行 + 档位格计数 + 会话内时序（`_next_turn_challenged` 用）。
    r5, dials, seq = [], defaultdict(int), defaultdict(list)
    # R6（20261015 批 3）：短应答自我申报行（`planner.reply_kind`）。
    r6: list = []
    files = iter_trace_files(TRACE_DIR)
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
