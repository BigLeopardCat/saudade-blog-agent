# -*- coding: utf-8 -*-
"""跨源对账（20260924）：trace ↔ agent.log ↔ monitor.log，对不上就是异常。

为什么要有这一层：现有的 `trace_metrics.py` / `trace_alert.py` 都是**单源规则扫描**
——只看 trace 一个文件，规则写在 trace 内部（轮次/工具/语义告警）。而 20260920 那起
"数据真改了、回执落了库、前端只见报错"（路由表里没有 `model` 的落点 ⇒ 每次点确定都
报错）单源扫描结构上看不见：trace 里每一段都自洽，错在**两个源之间**。

本脚本做确定性对账：零 LLM、零网络、零 DB、**只读**。三个源与各自权威的东西：
  · `logs/agent/traces/*.json` / `*.json.*.gz`  每轮一份 trace（trace_id / user_id /
    started_at / end_reason / frames / events）
  · `logs/agent/agent.log(.N.gz)`               每轮收尾一行 `[stream] end reason=…
    duration=…s frames=…`（同一行带 `tid=`）
  · `logs/frontend/monitor.log`                 前端错误上报（POST /api/monitor/log，匿名可写）

判据 v1（全部确定性）：
  ① TRACE_WITHOUT_END  trace 有、收尾行没有
  ② END_WITHOUT_TRACE  收尾行有、trace 没有。子类：
       · `dump_failed`  agent.log 里有 `trace dump failed trace_id=` 佐证（写盘失败）
       · `process_death` 无佐证——进程被杀 / OOM / 重启，dump 从未执行
  ③ DUPLICATE_TID      同一个 trace_id 落在多个 trace 文件里（`utils/trace.py` 的
       `_ACTIVE` 是"后写覆盖"语义 ⇒ 前一份被静默丢弃）
  ④ FIELD_MISMATCH     配上了但 end_reason / frames 不等（验收基线上三者全等，不等即有信息）
  ⑤ MONITOR_ANOMALY    前端上报里的 `confirm_card`（弹窗链路只在失败分支调用）与
       `orphan_dom_drop`（消息流 DOM 清理）
  ⑥ CLUE               不做硬判：弹窗轮（`execute.consent_popup` 事件）与 `confirm_card`
       的候选配对，**必须 uid 相同且 |时间差| ≤ CLUE_MIN**——否则就是巧合检测器
  ⑦ CLUE               不做硬判：弹窗链的逐跳记录（前端 `confirm_flow` 埋点，20260924）
       ——同一枚待办靠帧里的 id 串成 frame→card→click→sent→settle，跳数对不上即线索

三条纪律（都是踩过的坑）：
  · **只在两个源覆盖范围的交集里判 ①②**：logrotate 00:00 对 `*.log` 用 copytruncate
    ⇒ 午夜必然缺行；trace 目录只留最近两周。交集之外一律记 UNCOVERED、不报缺失。
  · **tid 只从文件里的 `trace_id` 字段取、绝不切文件名**（既有 `diag_1790032810_0_…`
    这类非 hex 的 tid；文件名只有前 8 位）。
  · **monitor.log 只能是线索、不能当证据**：`type=` 是匿名可写的原样插值
    （`monitor.rs`），内容可以被伪造。所以 ⑤ 只报计数与原文，⑥ 只做候选配对。

⑦ 为什么也**只出线索、不进 has_anomaly**（后来者别顺手改）：链上跳数不齐的真实成因
里混着"用户关掉了标签页"——点了确定、请求还没收尾就把页面关了，这类天天可能有几条，
一旦接进 health.log 那条通道，每晚一条 WARN 会让整条告警通道失真（本文件开头就写着
"响多了就没人看了"）。它的价值在于**单源看不见的那类**：链在日志里存在、跳数却不齐，
夜里对账时能把"点了没结论""请求出去了没结论""帧到手卡片没挂上"这几种形态分出来并
点名，人再去看报告。计数进 stdout 一行结论（夜间日志收它）。

输出：一行结论到 stdout（夜间日志收它）+ `eval/report/reconcile_<ts>.md` +
`eval/report/last_reconcile.json`；**仅在异常时**往 `logs/health.log` 追加一条 WARN
（与既有的一分钟心跳探针同一条告警通道）。巡检非门禁：**永远 exit 0**。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/trace_reconcile.py              # 默认对账**昨夜**（近 1 天）
  .venv/bin/python eval/trace_reconcile.py --days 3
  .venv/bin/python eval/trace_reconcile.py --from 20260922 --to 20260923   # 指定闭合窗口
  .venv/bin/python eval/trace_reconcile.py --json       # 只打印 JSON 摘要（管道用）

为什么默认 1 天而不是 7：对账要的是"刚过去这一夜有没有对不上"，窗口一开大，
轮转缺口、已复核过的历史异常、旧格式变化全被翻出来，噪声会盖住新伤。
"""
import argparse
import glob
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# trace 文件枚举的唯一实现（20260925：生产 trace 改按天分目录 `<root>/<YYYYMMDD>/`，
# 四个读取端共用一处，免得"改了布局漏改一个脚本"= 那天它少看一半数据）。
# 与 `trace_files.py` 同目录，直接按脚本目录导入（本模块本来就被 tests 以同样方式加载）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files  # noqa: E402

# ── CLI 默认路径（读者函数一律把路径当参数收，模块常量只给 CLI 与自测用）──────
TRACE_DIR = "/home/ubuntu/memory_blog_rust/logs/agent/traces"
AGENT_LOG_DIR = "/home/ubuntu/memory_blog_rust/logs/agent"
MONITOR_DIR = "/home/ubuntu/memory_blog_rust/logs/frontend"
HEALTH_LOG = "/home/ubuntu/memory_blog_rust/logs/health.log"
REPORT_DIR = "eval/report"

TS_FMT = "%Y-%m-%d %H:%M:%S"
CLUE_MIN = 15        # 弹窗轮 ↔ confirm_card 的配对窗口（分钟），见判据 ⑥
SHOW_MAX = 8         # 报告里每类最多列几条明细

# `2026-09-24 00:12:25 | server | INFO | tid=r32bdcf8… | [stream] end reason=… duration=…s frames=…`
# tid 用非贪婪（它后面紧跟 ` | [stream] end`）；行首 19 字符是本地钟面时间戳。
STREAM_END_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?tid=(.*?) \| \[stream\] end "
    r"reason=(\S+) duration=([\d.]+)s frames=(\d+)")
DUMP_FAILED_RE = re.compile(r"trace dump failed trace_id=(\S+)")
# `2026-09-24 00:00:44.731 ERROR [monitor] type=fetch_fail uid=guest url=… msg=… stack=`
MON_HEAD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[.,]\d+)? \S+ \[monitor\] ")
# 前端上报里"只在失败分支出现"的两个 type（前者是弹窗链路，后者是消息流 DOM 清理）
MON_FAILURE_TYPES = ("confirm_card", "orphan_dom_drop")
# 弹窗链的**正常分支**逐跳记录（20260924 前端埋点）。**不能并进 MON_FAILURE_TYPES**：
# 那是"数异常"的口径，把正常链路算进去，等于每点一次确定都算一次异常。
FLOW_TYPE = "confirm_flow"
# 弹窗链上五个阶段（判据 ⑦ 按它数跳、缺哪一跳点名哪一跳）
FLOW_STAGES = ("frame", "card", "click", "sent", "settle")
# `type=confirm_flow uid=… url=… msg=stage=frame n=1 id=c1 opts=2 exp=no q=… stack=`
# 埋点把阶段与字段拼在 message 里（`k=v` 空格分隔，q 里的空白已被前端压成单空格）。
FLOW_MSG_RE = re.compile(r"msg=(.*?)(?: stack=|$)")
FLOW_KV_RE = re.compile(r"([A-Za-z_][\w]*)=(\S*)")

# 生产 trace 的正常文件名：`<时间戳>_<uid>_<trace_id 前 8 位>.json`（golden/diag 不落这里）
TRACE_NAME_RE = re.compile(r"^\d{8}T\d{6}_\d+_[0-9a-zA-Z]+\.json$")


def load_trace(path: str) -> dict | None:
    """读一份 trace（含 gz）；坏文件返回 None 而不是抛（扫描不能因为一个坏文件中断）。"""
    try:
        if path.endswith(".gz"):
            return json.loads(gzip.decompress(open(path, "rb").read()))
        return json.load(open(path))
    except Exception:
        return None


def _parse_ts(s: str) -> datetime | None:
    """解析时间戳。**两种分隔符都要收**：trace 的 `started_at` 是 `2026-09-24T00:12:20`
    （ISO 的 T，文件名去掉它才成 `20260924T001220`），日志行首是空格分隔的钟面。"""
    try:
        return datetime.strptime(s[:19].replace("T", " "), TS_FMT)
    except Exception:
        return None


def _iter_lines(paths: list) -> list:
    """按文件顺序读全部行（gz 也读）；坏文件跳过。"""
    out = []
    for p in paths:
        try:
            if p.endswith(".gz"):
                out.extend(gzip.open(p, "rt", encoding="utf-8", errors="replace").read().splitlines())
            else:
                out.extend(open(p, encoding="utf-8", errors="replace").read().splitlines())
        except Exception:
            continue
    return out


def read_traces(trace_dir: str, since: datetime, until: datetime) -> dict:
    """扫 trace 目录。返回 {index, records, parsed, bad, uid0, uid0_names, shape_odd}。

    `index` 是**全量**（不按窗口过滤）的 tid → [rec…]，只用于"这个 tid 到底有没有
    trace"的存在性判定；`records` 才是窗口内的，用于 ①②③④ 的明细与统计。
    uid==0 的是 golden/诊断产出的形状（`golden_trace.py` 恒传 0），跳过并计数。
    """
    index = defaultdict(list)
    records, bad, uid0, odd = [], 0, 0, []
    paths = iter_trace_files(trace_dir)
    for p in paths:
        name = os.path.basename(p).replace(".gz", "")
        # 轮转归档名是 `<原名>.json.N.gz` ⇒ 去掉 .gz 后再去掉 .N 才是原始名
        name = re.sub(r"\.json\.\d+$", ".json", name)
        not_standard = not TRACE_NAME_RE.match(name)
        d = load_trace(p)
        if not isinstance(d, dict) or not d.get("trace_id"):
            bad += 1
            continue
        tid = str(d["trace_id"])
        started = _parse_ts(str(d.get("started_at") or ""))
        uid = int(d.get("user_id") or 0)
        if not_standard:
            odd.append((os.path.basename(p), started, "文件名不是 `时间戳_uid_…` 形状"))
        if uid == 0:
            uid0 += 1
            odd.append((os.path.basename(p), started, "uid=0（golden/诊断产出）"))
            continue
        rec = {
            "tid": tid,
            "uid": uid,
            "started_at": started,
            "end_reason": d.get("end_reason"),
            "frames": d.get("frames"),
            "popup": any(e.get("event") == "consent_popup" for e in (d.get("events") or [])),
            "path": os.path.basename(p),
        }
        index[tid].append(rec)
        if started is not None and since <= started <= until:
            records.append(rec)
    # 卫生项（异物）只报**窗口内**的：2026-09-01/03 那几份早年诊断 trace 躺在生产目录里
    # 是既有事实，若按"看见就报"会每晚重报一次已复核过的旧账，把新伤淹掉。
    # 时间戳解析不出来的（bad）保留在列表里——坏文件值得每晚提醒到有人清掉为止。
    # 同一份文件可能同时命中两条理由（uid=0 且文件名非标准）⇒ 按文件合并成一条。
    in_win, why_by_file = [], defaultdict(list)
    for n, t, w in odd:
        if t is None or since <= t <= until:
            if n not in why_by_file:
                in_win.append(n)
            if w not in why_by_file[n]:
                why_by_file[n].append(w)
    shape_odd = [{"file": n, "why": "；".join(why_by_file[n])} for n in in_win]
    return {"index": dict(index), "records": records, "parsed": len(records),
            "bad": bad, "uid0": uid0, "shape_odd": shape_odd}


def read_stream_ends(log_dir: str) -> dict:
    """读 agent.log 及其轮转归档，抽 `[stream] end` 行与 `trace dump failed` 佐证行。"""
    paths = sorted(glob.glob(os.path.join(log_dir, "agent.log*")))
    ends, dump_failed, raw = [], set(), 0
    for line in _iter_lines(paths):
        raw += 1
        m = STREAM_END_RE.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            if ts is None:
                continue
            ends.append({"ts": ts, "tid": m.group(2), "reason": m.group(3),
                         "dur": float(m.group(4)), "frames": int(m.group(5))})
            continue
        m = DUMP_FAILED_RE.search(line)
        if m:
            dump_failed.add(m.group(1))
    return {"ends": ends, "dump_failed": dump_failed, "raw_lines": raw}


def read_monitor(monitor_dir: str) -> dict:
    """读 monitor.log 及其轮转归档。**只当线索**：type/uid 都是匿名可写的原样插值。"""
    paths = sorted(glob.glob(os.path.join(monitor_dir, "monitor.log*")))
    events, raw = [], 0
    for line in _iter_lines(paths):
        raw += 1
        m = MON_HEAD_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        rest = line[m.end():]
        # type= / uid= 的值都用"到下一个已知字段为止"的非贪婪截法：`type` 是原样插值的，
        # 里面可以有空格（甚至伪造出别的字段名）——这里不校验，把原文照抄进报告即可。
        t = re.search(r"type=(.*?)(?: uid=| url=| msg=| stack=|$)", rest)
        u = re.search(r"uid=(.*?)(?: url=| msg=| stack=| type=|$)", rest)
        events.append({"ts": ts, "type": (t.group(1) if t else "").strip(),
                       "uid": (u.group(1) if u else "").strip(), "line": line[:300],
                       "msg": (FLOW_MSG_RE.search(rest).group(1).strip()
                               if FLOW_MSG_RE.search(rest) else "")})
    return {"events": events, "raw_lines": raw}


def flow_kv(ev: dict) -> dict:
    """把逐跳埋点的 message 拆成 {stage, n, id, result, …}。

    message 是 `stage=frame n=1 id=… q=…` 这种空格分隔的 k=v（前端把值里的空白压成
    单空格）。**认不出的整段照抄进报告、不做推断**——那份 message 是唯一原文。
    """
    return dict(FLOW_KV_RE.findall(ev.get("msg") or ""))


def reconcile(trace_dir: str, log_dir: str, monitor_dir: str,
              since: datetime, until: datetime, clue_min: int = CLUE_MIN) -> dict:
    """三源对账主体。返回结果 dict（纯数据，渲染与断言都吃它）。"""
    tr = read_traces(trace_dir, since, until)
    lg = read_stream_ends(log_dir)
    mo = read_monitor(monitor_dir)

    ends = [e for e in lg["ends"] if since <= e["ts"] <= until]
    ends_by_tid = defaultdict(list)
    for e in ends:
        ends_by_tid[e["tid"]].append(e)
    index = tr["index"]

    # ── 覆盖范围：两个源各有各的边界，只判交集 ──────────────────────────────
    trace_ts = [r["started_at"] for r in tr["records"] if r["started_at"]]
    end_ts = [e["ts"] for e in ends]
    t_lo, t_hi = (min(trace_ts), max(trace_ts)) if trace_ts else (None, None)
    l_lo, l_hi = (min(end_ts), max(end_ts)) if end_ts else (None, None)
    # 判 ① 的区间 = [trace 起点, 收尾行起点] 的**上界取 trace 自己的**：
    # 下界取两源的较大者（trace 早于日志覆盖起点时，它的收尾行可能只是被 00:00 的
    # copytruncate 截掉了，不能判缺失）；上界不能取日志的上界——最后一份 trace 的
    # 收尾行本来就在它之后几秒，取日志上界会把**最新的一轮**漏出判定之外。
    interval = (max(t_lo, l_lo), t_hi) if (t_lo and l_lo) else (None, None)
    judge = (interval[0] is not None and interval[0] <= interval[1])

    def in_interval(t):
        return bool(judge and interval[0] <= t <= interval[1])

    # 自检：一条收尾行都没有而 trace 一堆 ⇒ 更像是"日志级别/轮转改了"，不是"N 条对不上"
    self_check = ""
    if not ends and tr["records"]:
        self_check = (f"窗口内 trace {len(tr['records'])} 份、收尾行 0 条——"
                      f"日志级别或轮转方式变了？（先查 agent.log 还在不在写 `[stream] end`）")

    # ① trace 有、收尾行没有
    trace_without_end = [r for r in tr["records"]
                         if not ends_by_tid.get(r["tid"]) and in_interval(r["started_at"])]
    # ② 收尾行有、trace 没有（子类靠佐证行分）。
    # 这一类**不按区间裁**：存在性是对着**全量索引**（不按窗口过滤）查的，而 trace 与
    # 日志的轮转是同一套 14 天节奏——tid 在盘上就该找得到；找不到就是真丢了（写盘失败
    # 或有佐证行，或者进程死在 dump 之前）。按区间裁反而会把**窗口末尾**那些"trace 没落成"
    # 的行全漏掉，而那正是要抓的东西。
    end_without_trace = sorted(
        [{**e, "kind": "dump_failed" if e["tid"] in lg["dump_failed"] else "process_death"}
         for e in ends if not index.get(e["tid"])], key=lambda x: x["ts"])
    # ③ 一个 tid 多份 trace
    dup_tid = [{"tid": tid, "paths": [r["path"] for r in recs]}
               for tid, recs in index.items() if len(recs) > 1]
    # ④ 配上了但字段不等（同样不按区间裁：配上了就说明两个源都在，不该有边界豁免）
    mismatch = []
    for tid, recs in index.items():
        for e in ends_by_tid.get(tid, []):
            r = recs[0]
            if r["end_reason"] != e["reason"] or r["frames"] != e["frames"]:
                mismatch.append({"tid": tid, "path": r["path"],
                                 "trace": [r["end_reason"], r["frames"]],
                                 "log": [e["reason"], e["frames"]]})

    # ⑤ 前端上报里"只在失败分支出现"的两种
    mon = [ev for ev in mo["events"] if since <= ev["ts"] <= until]
    mon_fail = [ev for ev in mon if ev["type"] in MON_FAILURE_TYPES]
    confirm_cards = [ev for ev in mon_fail if ev["type"] == "confirm_card"]

    # ⑥ 线索：弹窗轮 ↔ confirm_card（必须 uid 相同 + 时间接近，否则是巧合检测器）
    popups = [{"ts": r["started_at"], "uid": r["uid"], "tid": r["tid"]}
              for r in tr["records"] if r["popup"] and r["started_at"]]
    clues, orphan_cards = [], []
    for ev in confirm_cards:
        near = [p for p in popups if str(p["uid"]) == ev["uid"]
                and abs((p["ts"] - ev["ts"]).total_seconds()) <= clue_min * 60]
        if near:
            p = min(near, key=lambda x: abs((x["ts"] - ev["ts"]).total_seconds()))
            clues.append({"card": ev, "popup": p,
                          "gap_s": int((ev["ts"] - p["ts"]).total_seconds())})
        else:
            # 反方向更强：报了 confirm_card 却在附近找不到任何弹窗轮
            orphan_cards.append(ev)

    # ⑦ 弹窗链的逐跳记录（正常分支埋点）：同一枚待办靠 id 串，跳数不齐即线索。
    # 为什么按**跳数**判而不是按顺序判：一条链的几跳可能落在同一秒里，而日志行只有
    # 整秒 ⇒ 顺序不可靠；跳数（以及每条链自己的 n 单调）是可靠的。顺序另有 n 兜底。
    flow = [ev for ev in mon if ev["type"] == FLOW_TYPE]
    chains = {}
    flow_noid = 0
    for ev in flow:
        kv = flow_kv(ev)
        cid = kv.get("id") or ""
        if not cid:
            flow_noid += 1        # 埋点丢了 id ⇒ 这条链串不起来，单独计数（不猜）
            continue
        ch = chains.setdefault(cid, {"id": cid, "uid": ev["uid"], "ts": ev["ts"],
                                     "stage": {s: 0 for s in FLOW_STAGES},
                                     "results": {}, "n_max": {}, "unknown_stage": 0})
        st = kv.get("stage") or ""
        if st in ch["stage"]:
            ch["stage"][st] += 1
        else:
            ch["unknown_stage"] += 1      # 埋点新增了阶段而这里没同步 ⇒ 要看得见
        if st == "settle":
            r = kv.get("result") or "(无 result)"
            ch["results"][r] = ch["results"].get(r, 0) + 1
        # 序号在同一枚待办里必须单调（前端靠它绕开上报链的去重）：回退即线索
        try:
            n = int(kv.get("n") or 0)
        except ValueError:
            n = 0
        ch["n_max"][st] = max(ch["n_max"].get(st, 0), n)

    flow_clues = []
    for cid, ch in sorted(chains.items(), key=lambda kv: kv[1]["ts"]):
        s = ch["stage"]
        why = []
        if s["sent"] > s["click"]:
            why.append("有确认请求却没有对应的点击记录")
        if s["click"] > s["settle"] and not any(
                str(ev["uid"]) == ch["uid"]
                and abs((ev["ts"] - ch["ts"]).total_seconds()) <= clue_min * 60
                for ev in confirm_cards):
            # 忙守卫挡下是**合法**的"点了没结论"（卡片保留、另有 confirm_card 留痕），
            # 所以只有"附近连一条失败上报都没有"才算线索——不然这条判据天天响。
            why.append("点了却没有任何结论（附近也没有该轮次的失败上报）")
        if s["sent"] > s["settle"]:
            why.append(f"发出了 {s['sent']} 次确认请求、只结算了 {s['settle']} 次")
        if s["frame"] and not s["card"]:
            why.append("确认帧到手但卡片没挂上（20260923 那类形态）")
        if ch["unknown_stage"]:
            why.append(f"{ch['unknown_stage']} 条埋点的阶段名不认识（埋点与对账要同步）")
        if why:
            flow_clues.append({**{k: ch[k] for k in ("id", "uid", "ts", "results")},
                               "stage": s, "why": why})
    flow_results = {}
    for ch in chains.values():
        for r, c in ch["results"].items():
            flow_results[r] = flow_results.get(r, 0) + c

    # 卫生项（生产目录里的异物）**不算异常**：它是"有人把 golden/诊断产物落错了地方"，
    # 不是这一夜的对账结果，报在报告里、进一行结论，但不该让 health.log 每晚响一次
    # （响多了就没人看了——那条通道要留给真异常）。
    anomalies = {
        "trace_without_end": trace_without_end,
        "end_without_trace": end_without_trace,
        "dup_tid": dup_tid,
        "mismatch": mismatch,
        "monitor_failure": mon_fail,
        "orphan_card": orphan_cards,
    }
    has_anomaly = any(anomalies.values()) or bool(self_check)

    return {
        "since": since.strftime(TS_FMT), "until": until.strftime(TS_FMT),
        "trace_records": len(tr["records"]), "trace_parsed_all": sum(len(v) for v in index.values()),
        "bad_trace": tr["bad"], "uid0": tr["uid0"], "shape_odd": tr["shape_odd"],
        "ends": len(ends), "raw_lines": lg["raw_lines"], "dump_failed": sorted(lg["dump_failed"]),
        "monitor_lines": len(mon), "popup_rounds": len(popups),
        "coverage": {"trace": [str(t_lo), str(t_hi)], "log": [str(l_lo), str(l_hi)],
                     "interval": [str(interval[0]), str(interval[1])], "judged": judge},
        "uncovered_traces": len([r for r in tr["records"]
                                 if r["started_at"] and not in_interval(r["started_at"])]),
        "trace_without_end": trace_without_end, "end_without_trace": end_without_trace,
        "dup_tid": dup_tid, "mismatch": mismatch,
        "monitor_failure": mon_fail, "clues": clues, "orphan_card": orphan_cards,
        "flow_chains": len(chains), "flow_events": len(flow), "flow_noid": flow_noid,
        "flow_results": flow_results, "flow_clues": flow_clues,
        "self_check": self_check, "has_anomaly": has_anomaly,
    }


def render_md(r: dict) -> str:
    """把结果渲染成人看的报告（判据 → 计数 → 明细，明细截断到 SHOW_MAX 条）。"""
    L = []
    A = L.append
    A(f"# 跨源对账 {r['since']} → {r['until']}")
    A("")
    cov = r["coverage"]
    A(f"源：trace {r['trace_records']} 份（全量 {r['trace_parsed_all']}，坏文件 {r['bad_trace']}，"
      f"uid=0 跳过 {r['uid0']}）· 收尾行 {r['ends']} 条（读 {r['raw_lines']} 行）"
      f"· monitor {r['monitor_lines']} 条 · 弹窗轮 {r['popup_rounds']} 个（trace 侧）")
    A(f"覆盖：trace {cov['trace'][0]} → {cov['trace'][1]}｜log {cov['log'][0]} → {cov['log'][1]}"
      f"｜**判定区间** {cov['interval'][0]} → {cov['interval'][1]}"
      f"{'' if cov['judged'] else '（两源无交集 ⇒ 本轮不做缺失判定）'}")
    A(f"区间外未覆盖（不判缺失）：trace {r['uncovered_traces']} 份"
      f"（收尾行不按区间裁：tid 在盘上就该找得到，见判据 ②）")
    A("")
    if r["self_check"]:
        A(f"⚠️ {r['self_check']}")
        A("")
    if r["flow_chains"] or r["flow_noid"]:
        # 弹窗链是**信息**不是异常（判据 ⑦ 的说明在文件头）：这一行说的是"这一夜里
        # 用户点过多少次确定、各自以什么结束"，跳数不齐的另在下面点名。
        res = "、".join(f"{k} {v}" for k, v in sorted(r["flow_results"].items())) or "（无结论记录）"
        A(f"弹窗链：{r['flow_chains']} 条（{r['flow_events']} 条埋点）"
          f"｜结论分布：{res}"
          f"{'｜' + str(r['flow_noid']) + ' 条埋点没有 id（串不成链）' if r['flow_noid'] else ''}"
          f"｜跳数不齐的线索 {len(r['flow_clues'])} 条")
        A("")
    A(f"## 结论：{'有异常' if r['has_anomaly'] else '干净'}")
    A("")
    A("| 判据 | 数量 |")
    A("|---|---|")
    A(f"| trace 有、收尾行没有 | {len(r['trace_without_end'])} |")
    A(f"| 收尾行有、trace 没有 | {len(r['end_without_trace'])} |")
    A(f"| 同一 trace_id 多份文件 | {len(r['dup_tid'])} |")
    A(f"| 配上了但 end_reason/frames 不等 | {len(r['mismatch'])} |")
    A(f"| 前端上报（失败分支）| {len(r['monitor_failure'])} |")
    A(f"| 其中配对不上任何弹窗轮 | {len(r['orphan_card'])} |")
    A(f"| 弹窗链跳数不齐（正常分支埋点，线索、不计异常）| {len(r['flow_clues'])} |")
    A(f"| 生产 trace 目录里的异物（窗口内，卫生项、不计异常）| {len(r['shape_odd'])} |")
    A("")

    def block(title, items, fmt, note=""):
        A(f"### {title}（{len(items)}）" + (f"  _{note}_" if note else ""))
        if not items:
            A("- （无）")
        for it in items[:SHOW_MAX]:
            A(f"- {fmt(it)}")
        if len(items) > SHOW_MAX:
            A(f"- … 另 {len(items) - SHOW_MAX} 条（报告只列前 {SHOW_MAX} 条）")
        A("")

    block("trace 有、收尾行没有", r["trace_without_end"],
          lambda x: f"`{x['started_at']}` u{x['uid']} tid=`{x['tid']}` {x['path']}"
                    f"（end_reason={x['end_reason']} frames={x['frames']}）")
    block("收尾行有、trace 没有", r["end_without_trace"],
          lambda x: f"`{x['ts']}` tid=`{x['tid']}` {x['kind']}"
                    f"（{x['reason']} frames={x['frames']}）")
    block("同一 trace_id 多份文件", r["dup_tid"],
          lambda x: f"tid=`{x['tid']}` → {'、'.join(x['paths'])}")
    block("字段不符", r["mismatch"],
          lambda x: f"tid=`{x['tid']}` trace={x['trace']} vs log={x['log']}")
    block("前端上报（只在失败分支出现）", r["monitor_failure"],
          lambda x: f"`{x['ts']}` type={x['type']} uid={x['uid']}",
          "monitor.log 匿名可写、`type` 原样插值 ⇒ **只是线索，不是证据**")
    block("配对不上弹窗轮的 confirm_card", r["orphan_card"],
          lambda x: f"`{x['ts']}` uid={x['uid']}（前后 {CLUE_MIN} 分钟内没有同 uid 的弹窗轮）",
          "反方向是更强的异常：报了卡片却没弹过窗")
    block("线索：弹窗轮 ↔ confirm_card 配对", r["clues"],
          lambda x: f"{x['popup']['ts']} u{x['popup']['uid']} 弹窗 tid=`{x['popup']['tid']}`"
                    f" ↔ {x['card']['ts']} 上报（差 {x['gap_s']}s）")
    block("线索：弹窗链跳数不齐（正常分支埋点）", r["flow_clues"],
          lambda x: f"`{x['ts']}` id={x['id']} u{x['uid']}"
                    f" frame{x['stage']['frame']}/card{x['stage']['card']}"
                    f"/click{x['stage']['click']}/sent{x['stage']['sent']}"
                    f"/settle{x['stage']['settle']}"
                    f" → {'；'.join(x['why'])}",
          "用户关掉页签也会留下不齐的链 ⇒ **只是线索**，不进 health.log（见判据 ⑦）")
    block("生产 trace 目录里的异物（窗口内）", r["shape_odd"],
          lambda x: f"`{x['file']}`：{x['why']}——golden/诊断产出不该落生产目录")
    A(f"（另有 {r['uid0']} 份 uid=0 的历史 trace 在目录里，不在窗口内 ⇒ 只计数不报）")
    A("")
    return "\n".join(L)


def summarize(r: dict) -> str:
    """一行结论（夜间日志与 stdout 用）。"""
    return (f"[对账 {r['since'][:10]}] trace {r['trace_records']} / 收尾行 {r['ends']} / "
            f"缺收尾 {len(r['trace_without_end'])} / 缺trace {len(r['end_without_trace'])} / "
            f"重复tid {len(r['dup_tid'])} / 字段不符 {len(r['mismatch'])} / "
            f"前端上报 {len(r['monitor_failure'])}"
            f"{'（其中 ' + str(len(r['orphan_card'])) + ' 条配对不上弹窗轮）' if r['orphan_card'] else ''}"
            f" / 弹窗链 {r['flow_chains']} 条（跳数不齐 {len(r['flow_clues'])}）"
            f"{' / 目录异物 ' + str(len(r['shape_odd'])) if r['shape_odd'] else ''}"
            f" ⇒ {'有异常' if r['has_anomaly'] else '干净'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                    help="近 N 天（默认 1：只对账刚过去这一夜，见文件头）")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260922")
    ap.add_argument("--to", dest="until", default="", help="终点日期 20260923（含当天）")
    ap.add_argument("--trace-dir", default=TRACE_DIR)
    ap.add_argument("--log-dir", default=AGENT_LOG_DIR)
    ap.add_argument("--monitor-dir", default=MONITOR_DIR)
    ap.add_argument("--health-log", default=HEALTH_LOG)
    ap.add_argument("--report-dir", default=REPORT_DIR)
    ap.add_argument("--json", action="store_true", help="只打印结果 JSON（管道/程序读）")
    args = ap.parse_args()

    now = datetime.now()
    if args.since:
        since = datetime.strptime(args.since[:8], "%Y%m%d")
    else:
        since = now - timedelta(days=args.days)
    if args.until:
        until = datetime.strptime(args.until[:8], "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
    else:
        until = now

    r = reconcile(args.trace_dir, args.log_dir, args.monitor_dir, since, until)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, default=str))
        return 0

    ts = now.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.report_dir, exist_ok=True)
    md = os.path.join(args.report_dir, f"reconcile_{ts}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(render_md(r) + "\n")
    with open(os.path.join(args.report_dir, "last_reconcile.json"), "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1, default=str)

    print(summarize(r))
    print(f"报告: {md}")
    if r["has_anomaly"]:
        # 只在异常时进 health.log（同既有约定：干净就不写，一行都不多）
        try:
            with open(args.health_log, "a", encoding="utf-8") as f:
                f.write(f"{now.strftime(TS_FMT)} WARN [trace-reconcile] {summarize(r)}\n")
        except Exception as e:  # 写不了告警不是对账失败的理由
            print(f"（health.log 写入失败：{e}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
