# -*- coding: utf-8 -*-
"""接口层档位对照矩阵：同一套用例在多个档下各跑 N 次（20260927 主线批 B）。

**它回答什么**：`docs/native-toolcalls-mainline.md` §7 那三个待拍板项——
技能模板去留（text vs native）、开思考的预算（thinking on/off）、是否换 max（flash/max）。
每一项要的都是**同一套用例上的成组对照**，不是两次单跑拼出来的印象：换档即换采样，
单跑一次的结论会与另一次相反（§1.4 实测：同一组 4 条在 6 次跑里出现过 3/4、4/4、2/4）。
所以本脚本只做一件事：**把"档"变成自变量，其余全部固定**。

**档的表示**＝三个环境变量（`PLANNER_ENGINE` / `PLANNER_NATIVE_THINKING` / `QWEN_MODEL`），
每个 (档, 第 i 次) 起一个独立子进程（环境变量按档注入 ⇒ 不共享 settings 单例）。
⚠️ `QWEN_MODEL` 是**全链路**的模型（planner 与 narrator 共用一个默认值），所以"换 max"
这一档量到的是**整链路换模型**的效果与耗时——这正是"是否换 max"这个决定在运维上的含义
（没有 per-node 模型开关，也别为了做对照临时加一个）。
⚠️ 顺序：**外层是第 i 次、内层是档**（不是一档跑完再跑下一档）。同一时刻的端点负载被所有
档共享，档间差异才不会被"前半小时快、后半小时慢"冒充成档的效果。

**判据分两堆（20260928）**：`跑不成` 与 `跑得差` 是两件事。首跑 provider 对照 24 例里 21 例
压根没发出请求（400 孤儿 tool 消息），那一格若只有一个通过率，读起来就是"这个服务商质量
只有 12.5%"——**一个不存在结论**。所以失败按**错误签名**分堆（`is_provider_error`：服务商
HTTP 错误码 / 连接超时 / 两类协议非法消息），报成一列 `跑不成/用例次`；剩下的才是内容判据
失败。钱与量同理：逐节点 token 与命中率单列（见下）。

**四个指标，口径写死**（否则换档比的是不同的东西）：
  · `pass_rate`  逐条"几次里绿几次"的合计（计数复用 `baseline_group.aggregate`，
                 不在这里另写一套）
  · `planner_round_s`  **planner 决策轮**这一腿的耗时（trace 里 `planner/llm_done` 的
                 `duration_s`）。刻意不用端到端：端到端混着 narrator 与检索，换模型时那两段
                 也在变，分不清是谁的账 ⇒ **两个都给**，标签写清楚谁含什么
  · `fallback_rate`  native 档决策轮里"判不了、退回文本解析"的比例
                 （`native_fallback / (native_decision + native_fallback)`）；
                 text 档没有这个概念，如实记 `null`——**0 与 null 不是一回事**
                 （0 = 量到了、没发生；null = 这一档没有这个概念）
  · `tool_call_completeness`  1 − `finish_reason=length` 的比例。截断是静默失败
                 （arguments 断在半截 JSON），`length` 占比正是"预算够不够"的答案
  · `tokens`     逐节点 token 与**前缀缓存命中率**（`cache_read/input`，`input` 含命中那
                 部分）。**按节点切**：planner 是一份几乎不变的长提示词（命中率高）、
                 narrator 的提示词随对话增长（命中率低），一个合计数正好把"该优化哪条腿"
                 抹掉。命中率只对上报了缓存字段的调用算，`cache_seen` 与 `calls` 一起进
                 报告——分母被缩小这件事必须看得见；`cache_read` 缺席算 **null**，不是 0

**两道自检**（本脚本自己也要有判据，否则"档没拨过去"会伪装成"这个档更慢/更差"）：
  · 前置探针：每个档先起一个只读子进程打印 `settings` 解析结果，断言 engine/thinking/model
     与档的声明相等 —— 证明环境变量真的到了 settings 那一层；
  · 收尾对账：报告里的 `engine` 字段（跑法自己记的）必须等于档声明的 engine。
两道都对不上就抛，不产出报告。

用法（仓库根 cwd）：
  .venv/bin/python eval/dial_matrix.py --label multi_step_plus_control --runs 3 \\
      --ids multi_step_referent_nav_effect,multi_step_effect_then_nav,\\
multi_step_missing_param_asks,multi_step_search_then_read_top,eff_on_sakura,\\
rag_ota_partition,casual_intro,data_tags \\
      --out eval/report/baseline_20260927_dial_matrix.json

报告路径从子进程 stdout 取，trace 目录从报告里的 `trace_run` 取（**不自己造 run_id**——
造出来的目录不受 `golden_trace.prune` 的保留期管辖，会变成没人清理的垃圾）。
"""
import argparse
import glob
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, "eval")

from baseline_group import aggregate          # noqa: E402,I001  （逐条计数只有一处实现）
from golden_trace import trace_root           # noqa: E402  （trace 根只有一处派生）
from trace_io import load_trace               # noqa: E402  （trace 读取只有一处实现）

# stdout 编码：本仓 eval/ 脚本靠**被 import 时的副作用**包一次 utf-8（`baseline_group` 顶部那行），
# 所以这里只补一次、且**只在还没包过的时候**。二次包裹会让前一个 wrapper 失去引用被 GC、
# 它持有的底层 buffer 跟着被关，表现成第一次 print 就 `I/O operation on closed file`（实测踩到）。
if getattr(sys.stdout, "encoding", "").lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

PY = ".venv/bin/python"

# 五个档（20260927）。**顺序是输出表的行序**，别乱动：报告要能横向比。
# `text` 第一行＝生产现状，其余四行是它的候选替代（同一套用例、同一份语料）。
# `engine` 是期望值（报告里那一格必须等于它）；没有 QWEN_MODEL 的档＝跟随 `.env` 默认，
# 观察到的实际模型照实记进报告（"flash 到底解析成什么"本身就是要存档的事实）。
DIALS: dict[str, dict] = {
    "text":                 {"env": {"PLANNER_ENGINE": "text"},
                             "engine": "text", "model": None},
    "native-think-flash":   {"env": {"PLANNER_ENGINE": "native",
                                     "PLANNER_NATIVE_THINKING": "true"},
                             "engine": "native", "model": None},
    "native-nothink-flash": {"env": {"PLANNER_ENGINE": "native",
                                     "PLANNER_NATIVE_THINKING": "false"},
                             "engine": "native", "model": None},
    "native-think-max":     {"env": {"PLANNER_ENGINE": "native",
                                     "PLANNER_NATIVE_THINKING": "true",
                                     "QWEN_MODEL": "qwen3.8-max"},
                             "engine": "native", "model": "qwen3.8-max"},
    "native-nothink-max":   {"env": {"PLANNER_ENGINE": "native",
                                     "PLANNER_NATIVE_THINKING": "false",
                                     "QWEN_MODEL": "qwen3.8-max"},
                             "engine": "native", "model": "qwen3.8-max"},
    # 换**服务商**（20260928）：与上面四档不同轴——那四档换的是引擎/思考/型号，
    # 这一档换的是 endpoint + key + 模型族。加它是因为"换模型"也是候选替代之一
    # （生产档 = native + 不思考 + flash），而换服务商的决定必须与 qwen 档落在
    # 同一张表上才可比。加入前先探过（只读探针，未记入报告）：deepseek-flash 认
    # `tools` 字段、**真的回 tool_calls**（`navigate({"target": "留言板"})` 一次命中）。
    # ⚠️ `PLANNER_NATIVE_THINKING` 对这一档是**空开关**（`models/llm.py` 只对 qwen
    # 发 `enable_thinking` 的 extra_body）——写 false 是为了与生产档**同形**，不是
    # 因为它读这个值；标 `provider` 让档自检能验"服务商真的拨过去了"。
    # ⚠️ **首跑（20260928）的 3/24 不是模型结论，别照着引用**——它红在两个**前置缺陷**
    # 上，两个都在换服务商之前就修掉了：
    #   ① narrator 的消息序列非法：唯一绿的那条（casual_intro）恰好是**零工具**用例，
    #      其余全红于 `400 Messages with role 'tool' must be a response to a preceding
    #      message with 'tool_calls'`——`model_node` 把 `[system] + state["messages"]`
    #      交给服务商，而那些 ToolMessage 的 `tool_call_id` 是自造的（`graph.py` 的
    #      `execute_{idx}`）、前面没有带 `tool_calls` 的 assistant 消息。qwen 端点容忍，
    #      deepseek 严格拒。修法 = `agent/context.py::with_tool_call_pairs`（**只补形状、
    #      不动材料**）。
    #   ② 档里原先写的是 **思考模型**：`deepseek-flash` / `deepseek-v4-flash` 默认回
    #      `reasoning_content`，补形状之后紧接着 400「The reasoning_content in the
    #      thinking mode must be passed back to the API.」——而我们补出来的 assistant
    #      本来就是模型没发过的话，没有推理链可回传 ⇒ **思考型号结构上跑不了这条链**。
    #      `deepseek-chat` 是**非思考**型号，实测接受补出来的形状并正常作答（探针：
    #      同一段孤儿帧，chat ✅ / flash、v4-flash ❌）。
    #      ⇒ 这一档比的是"服务商"，因此必须选**能力面对齐**的型号；拿思考型号去比，
    #      量到的是推理开销不是服务商差异（qwen 侧同轴的那一档是 `native-think-flash`）。
    "native-nothink-deepseek": {
                            "env": {"PLANNER_ENGINE": "native",
                                    "PLANNER_NATIVE_THINKING": "false",
                                    "LLM_PROVIDER": "deepseek",
                                    "DEEPSEEK_MODEL": "deepseek-chat"},
                            "engine": "native", "provider": "deepseek",
                            "model": "deepseek-chat"},
}

_REPORT_LINE = re.compile(r"留档:\s*(\S+)")

# 「这一跑没跑成」与「这一跑跑成了但答得不对」是两件事，混在一格里就会得出
# "换了服务商质量下降"这种**没有数据支撑的结论**（首跑 3/24 全红于 400 就是活例）。
# 判据只看失败文本里的**错误签名**，不看措辞质量：
#   · 服务商 HTTP 错误码（`Error code: 400/401/403/429/5xx`）
#   · 请求根本没发出去（连接/超时/鉴权）
#   · 请求发出去了但**协议非法**（服务商的 400 里那两类点名消息）
# 只认这些签名，不认"回复里没有 X"之类的断言失败——那是内容判据，归质量。
_PROVIDER_ERR_RE = re.compile(
    r"Error code: [45]\d\d"                       # 服务商 HTTP 错误码
    r"|Connection error|ConnectError|APITimeoutError|APIConnectionError|Timeout"
    r"|RateLimitError|invalid_api_key"
    r"|role 'tool' must be a response to a preceding"      # 协议：孤儿 tool 消息
    r"|reasoning_content in the thinking mode must be passed back",  # 协议：思考链回传
    re.I)


def is_provider_error(text: str) -> bool:
    """这句话是"没跑成"（服务商/协议错误）还是"跑成了但不对"（内容判据失败）。"""
    return bool(_PROVIDER_ERR_RE.search(text or ""))


def provider_error_stats(reports: list) -> dict:
    """把**原始 run_golden 报告**的逐用例失败按上面那把尺子分两堆。

    读的是 `reports` 里原始报告那份（`cases` 是 list、`fails` 是字符串列表）；`summarize`
    另有一份经过 `aggregate` 的按 id 合并版（`cases` 是 dict），**这里不吃那个形状**——
    两个形状同名不同义，混用会静默数错（已踩过一次）。

    `case_runs` = 用例次数（一次采样算一次，与 `aggregate` 的分母同源）；
    `broken_runs` = 其中**至少有一条** provider/协议错误的采样数——它才是"这一跑没跑成"；
    `messages` / `quality_messages` 是消息级计数，用来区分"一跑错三次"与"三跑各错一次"。
    """
    case_runs = broken = messages = quality = 0
    for _, rep in reports:
        cases = rep.get("cases")
        if isinstance(cases, dict):
            # 按 id 合并过的形状（`baseline_group.aggregate` 的产物）：同名不同义，
            # 照 list 迭代会得到 [] 并安静地数出 0——**空指标不许过关**，直接响。
            raise RuntimeError("provider_error_stats 收到的是按 id 合并的 cases（dict），"
                               "这一份要的是原始 run_golden 报告（cases 是 list）")
        for c in (cases or []):
            case_runs += 1
            texts = [str(x) for x in (c.get("fails") or [])]
            if c.get("error"):
                texts.append(str(c["error"]))
            if any(is_provider_error(t) for t in texts):
                broken += 1
            for t in texts:
                if is_provider_error(t):
                    messages += 1
                else:
                    quality += 1
    return {"case_runs": case_runs, "broken_runs": broken,
            "messages": messages, "quality_messages": quality}

# 前置探针：只读 settings，不连库不发请求。打印的键名与 settings 字段同名，便于对照。
_PROBE = ("import json;from config.settings import settings;"
          "print(json.dumps({'engine': settings.planner_engine,"
          "'thinking': settings.planner_native_thinking,"
          "'provider': settings.llm_provider,"
          "'model': settings.active_llm_model}))")


def _run_env(env_over: dict) -> dict:
    env = dict(os.environ)
    env.update(env_over)
    return env


def pct_stats(values: list[float]) -> dict:
    """p50 / max / n（**空样本给 None 不给 0**——没量到与量到 0 是两件事）。"""
    if not values:
        return {"n": 0, "p50": None, "max": None}
    s = sorted(values)
    return {"n": len(s), "p50": round(statistics.median(s), 2), "max": round(s[-1], 2)}


def preflight(dial: str, spec: dict, timeout: int = 120) -> dict:
    """档自检：环境变量注入之后，settings 里解析出来的值是否等于本档的声明。"""
    proc = subprocess.run([PY, "-c", _PROBE], capture_output=True, text=True,
                          encoding="utf-8", env=_run_env(spec["env"]), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"[{dial}] 探针跑不起来：{(proc.stderr or '')[-300:]}")
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    want_engine = spec["engine"]
    if got["engine"] != want_engine:
        raise RuntimeError(f"[{dial}] engine 没拨过去：settings={got['engine']} 期望={want_engine}")
    want_think = spec["env"].get("PLANNER_NATIVE_THINKING")
    if want_think is not None and bool(got["thinking"]) is not (want_think == "true"):
        raise RuntimeError(f"[{dial}] thinking 没拨过去：settings={got['thinking']} "
                           f"期望={want_think}")
    if spec["model"] and got["model"] != spec["model"]:
        raise RuntimeError(f"[{dial}] model 没拨过去：settings={got['model']} 期望={spec['model']}")
    # 服务商只在**声明了**的档上验（换服务商的档才需要；qwen 那几档由 model 那一格
    # 间接管住——型号名对不上就抛）
    want_provider = spec.get("provider")
    if want_provider and got.get("provider") != want_provider:
        raise RuntimeError(f"[{dial}] provider 没拨过去：settings={got.get('provider')} "
                           f"期望={want_provider}")
    return got


def trace_metrics(run_dir: str) -> dict:
    """一次 run 的 trace 目录 → planner 决策轮的计数、耗时、截断，外加**逐节点 token 用量**。

    只认 `planner` 节点的事件；`__rerun` 那份**排除**（回归组复跑的 trace 是同一用例的第二次
    采样，混进来会把一条用例算两次——本脚本的用例集里没有回归组，但判据不该依赖那个巧合）。

    token 那一段与耗时**共用这一次扫描**（同一份 trace 读两遍是白花钱）：
    `llm_done` 带 `input/output/cache_read`（契约见 `agent/llm_usage.py`）。
    **`cache_read` 缺席的调用不进命中率分母**——缺席是"这个形状量不到缓存"，不是"没命中"，
    混进来会算出"缓存完全没命中"这种假结论（该文件与 `eval/token_cost_report.py` 同纪律）。
    """
    dur: list[float] = []
    decisions = fallbacks = truncated = files = 0
    usage: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(run_dir, "*.json"))):
        if "__rerun" in os.path.basename(path):
            continue
        t = load_trace(path)
        if not isinstance(t, dict):
            continue        # 读不到、或不是 trace（合法 JSON 但不是对象）——都不算语料
        files += 1
        for e in t.get("events") or []:
            if e.get("event") == "llm_done" and "input" in e:
                u = usage.setdefault(e.get("node") or "?", {"calls": 0, "in": 0, "out": 0,
                                                             "cache": 0, "cache_seen": 0})
                u["calls"] += 1
                u["in"] += int(e.get("input") or 0)
                u["out"] += int(e.get("output") or 0)
                if "cache_read" in e:
                    u["cache_seen"] += 1
                    u["cache"] += int(e.get("cache_read") or 0)
            if e.get("node") != "planner":
                continue
            ev = e.get("event")
            if ev == "llm_done":
                if isinstance(e.get("duration_s"), (int, float)):
                    dur.append(round(float(e["duration_s"]), 2))
            elif ev == "native_decision":
                decisions += 1
                if e.get("finish") == "length":
                    truncated += 1
            elif ev == "native_fallback":
                fallbacks += 1
                if e.get("finish") == "length":
                    truncated += 1
    return {"trace_files": files, "planner_round_s_raw": dur,
            "native_decisions": decisions, "native_fallbacks": fallbacks,
            "truncated": truncated, "usage": usage}


def trace_metrics_for(rep: dict) -> dict:
    """报告 → 它的 trace 目录 → 指标。目录不在就抛。

    **空指标不许过关**：`planner_round_s` 的空样本在报告里与"这一档真没数据"长得一样，
    而真实原因往往是这次没落 trace（`GOLDEN_NO_TRACE=1`）或路径写错——那样整张对照表会
    少掉耗时与截断率两列却照常出数（本脚本第一版就把 golden 根写成了仓内相对路径，
    实测这一格全 `None`）。
    """
    run_id = rep.get("trace_run")
    if not run_id:
        raise RuntimeError("报告里没有 trace_run —— 对照表会少 planner 耗时与截断率两列")
    run_dir = os.path.join(trace_root(), run_id)
    if not os.path.isdir(run_dir):
        raise RuntimeError(f"trace 目录不在：{run_dir}（是不是设了 GOLDEN_NO_TRACE？）")
    return trace_metrics(run_dir)


def token_stats(nmetrics: list) -> dict:
    """逐节点 token：调用数、每次输入/输出、命中率。

    命中率 = `cache_read / input`（`input` **含**命中那部分，契约见 `agent/llm_usage.py`），
    只对"端点回了缓存字段的那些调用"算，并把 `cache_seen` 一起给出——分母被悄悄缩小的比率
    必须能从报告里看出来。**没量到给 None 不给 0**（同 `pct_stats` 的纪律）。

    为什么要按**节点**切而不是一个总数：planner 与 narrator 的成本结构完全不同——
    前者是一份几乎不变的长提示词（命中率高），后者是随对话增长的短前缀（命中率低）。
    一个合计数会把两者的差异平均掉，正好抹掉"该优化哪一条腿"这个唯一的决策信息。
    """
    merged: dict[str, dict] = {}
    for m in nmetrics:
        for node, u in (m.get("usage") or {}).items():
            t = merged.setdefault(node, {"calls": 0, "in": 0, "out": 0, "cache": 0,
                                         "cache_seen": 0})
            for k in t:
                t[k] += u.get(k, 0)
    out = {}
    for node, t in merged.items():
        n = t["calls"] or 1
        out[node] = {
            "calls": t["calls"],
            "in_per_call": round(t["in"] / n), "out_per_call": round(t["out"] / n),
            "in_total": t["in"], "out_total": t["out"],
            "cache_seen": t["cache_seen"], "cache_total": t["cache"],
            "hit_rate": (round(t["cache"] / t["in"], 4)
                         if t["cache_seen"] and t["in"] else None),
        }
    return out


def run_once(dial: str, spec: dict, ids: list[str], timeout: int) -> tuple[str, dict]:
    """起一个 run_golden 子进程 → (报告路径, 报告 dict)。失败直接抛（测量工具不许静默少样本）。"""
    t0 = time.time()
    proc = subprocess.run([PY, "eval/run_golden.py", "--only", ",".join(ids)],
                          capture_output=True, text=True, encoding="utf-8",
                          env=_run_env(spec["env"]), timeout=timeout)
    out = proc.stdout or ""
    m = _REPORT_LINE.search(out)
    if not m:
        raise RuntimeError(f"[{dial}] 跑法没打印留档路径（rc={proc.returncode}）："
                           f"{(out or proc.stderr or '')[-300:]}")
    with open(m.group(1), encoding="utf-8") as f:
        rep = json.load(f)
    rep["_wall_s"] = round(time.time() - t0, 1)
    # 收尾对账：报告自己记的 engine 必须等于本档声明（"档没拨过去"伪装成"这档更差"的入口）
    got_engine = str(rep.get("engine") or "unknown")
    if got_engine != spec["engine"]:
        raise RuntimeError(f"[{dial}] 报告记的 engine={got_engine} 与档声明 {spec['engine']} 不符"
                           f"（{m.group(1)}）")
    return m.group(1), rep


def green(rep: dict) -> tuple[int, int]:
    ok = sum(1 for c in rep["cases"] if c.get("final_ok", c.get("ok")))
    return ok, len(rep["cases"])


def summarize(dial: str, spec: dict, acc: dict) -> dict:
    """一个档跑完 N 次之后 → 报告里那一块。计数复用 aggregate（**不过它的两道门槛**：
    进这份表的报告全是本脚本为这个档现跑的，engine 门在这里恰好是反的——本脚本要的就是
    比不同档；tag 门也不适用——对照组那几条不带 multi_step 标签）。"""
    reports = acc["reports"]
    agg = aggregate(reports, "dial_matrix")
    nmeta = acc["nmetrics"]
    merged = {k: sum(m[k] for m in nmeta) for k in
              ("trace_files", "native_decisions", "native_fallbacks", "truncated")}
    rounds = merged["native_decisions"] + merged["native_fallbacks"]
    fb_rate = round(merged["native_fallbacks"] / rounds, 4) if rounds else None
    dur = [v for m in nmeta for v in m["planner_round_s_raw"]]
    pe = acc["plan_eff"]
    return {
        "env": spec["env"], "engine": spec["engine"],
        "observed": acc["observed"],                       # 探针看到的 settings 实值
        "runs": agg["runs"], "case_runs": agg["case_runs"], "case_passed": agg["case_passed"],
        "pass_rate": agg["pass_rate"], "pass_rate_ci95": agg["pass_rate_ci95"],
        "cases": agg["cases"],
        "case_elapsed_s": pct_stats(acc["elapsed"]),       # 端到端（含 narrator 与检索）
        "planner_round_s": pct_stats(dur),                 # 只 planner 决策这一腿
        "planner_round_s_raw": dur,
        "native": {**merged,
                   "fallback_rate": fb_rate,
                   "tool_call_completeness": (round(1 - merged["truncated"] / rounds, 4)
                                              if rounds else None)},
        # provider/协议错误与内容失败分两堆：**"没跑成"不是"跑得差"**。首跑 3/24 就是
        # 24 例里 21 例压根没发出去请求（400 孤儿 tool 消息），那一格若不分列，读起来
        # 就是"deepseek 质量只有 12.5%"。
        "fails": provider_error_stats(reports),
        "tokens": token_stats(nmeta),
        "plan_efficiency": ({"tool_calls_total": sum(p.get("tool_calls_total", 0) for p in pe),
                             "tool_rounds_total": sum(p.get("tool_rounds_total", 0) for p in pe),
                             "cases_multi_tool_rounds": sum(p.get("cases_multi_tool_rounds", 0)
                                                            for p in pe)} if pe else None),
        "reports": [p for p, _ in reports],
        "trace_runs": [r.get("trace_run") for _, r in reports],
        # 辅助字段（子进程墙钟，含语料热身），不是判据输入 ⇒ 缺了记 None 不抛：
        # 四个判据指标在别处，每一个缺了都要响（见 trace_metrics_for）。
        "wall_s": [r.get("_wall_s") for _, r in reports],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="接口层档位对照矩阵")
    ap.add_argument("--ids", required=True, help="用例 id，逗号分隔")
    ap.add_argument("--label", required=True, help="用例集的名字（写进报告，如 multi_step）")
    ap.add_argument("--runs", type=int, default=3, help="每档跑几次（默认 3）")
    ap.add_argument("--dials", default=",".join(DIALS), help="只跑这几档（逗号分隔）")
    ap.add_argument("--out", required=True, help="落盘路径（eval/report/baseline_*.json）")
    ap.add_argument("--timeout", type=int, default=1200, help="单次子进程超时（秒）")
    args = ap.parse_args()
    ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    dials = [s.strip() for s in args.dials.split(",") if s.strip()]
    bad = [d for d in dials if d not in DIALS]
    if bad:
        print(f"[x] 认不出的档：{bad}（可选：{list(DIALS)}）", flush=True)
        return 2

    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "label": args.label,
                 "ids": ids, "runs_per_dial": args.runs, "dials": {}}
    acc: dict[str, dict] = {}
    for dial in dials:
        print(f"\n=== 档 {dial}  env={DIALS[dial]['env']}", flush=True)
        got = preflight(dial, DIALS[dial])
        print(f"  探针：engine={got['engine']} thinking={got['thinking']} model={got['model']}",
              flush=True)
        acc[dial] = {"observed": got, "reports": [], "nmetrics": [], "elapsed": [], "plan_eff": []}

    try:
        # 外层第 i 次、内层档：同一时刻的端点负载被所有档共享（见模块 docstring）
        for i in range(1, args.runs + 1):
            for dial in dials:
                a = acc[dial]
                path, rep = run_once(dial, DIALS[dial], ids, args.timeout)
                a["reports"].append((path, rep))
                ok, n = green(rep)
                print(f"  [{dial} {i}/{args.runs}] {ok}/{n} 绿  {rep['_wall_s']}s  "
                      f"{os.path.basename(path)}", flush=True)
                a["elapsed"] += [c["elapsed"] for c in rep["cases"]
                                 if isinstance(c.get("elapsed"), (int, float))]
                if isinstance(rep.get("plan_efficiency"), dict):
                    a["plan_eff"].append(rep["plan_efficiency"])
                a["nmetrics"].append(trace_metrics_for(rep))
    except Exception:
        # 半途挂掉也要留证据（已跑成的样本 + 挂在哪一档），但不假装完整
        out["partial"] = True
        out["dials"] = {d: summarize(d, DIALS[d], acc[d]) for d in dials if acc[d]["reports"]}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n[x] 中断：已把跑成的样本落盘 {args.out}（**标了 partial，不可当完整对照**）",
              flush=True)
        raise

    for dial in dials:
        out["dials"][dial] = summarize(dial, DIALS[dial], acc[dial])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n{'档':<22}{'通过率':<16}{'端到端 p50/max':<20}{'planner p50/max':<20}"
          f"{'planner tok 入/出(次)':<24}{'命中率':<10}{'跑不成/用例次':<16}"
          f"{'fallback':<12}{'完整率':<10}", flush=True)
    for dial in dials:
        d = out["dials"][dial]
        ce, pr = d["case_elapsed_s"], d["planner_round_s"]
        rate = f"{d['case_passed']}/{d['case_runs']} {d['pass_rate']:.3f}"
        e2e = f"{ce['p50']}/{ce['max']}s"
        round_s = f"{pr['p50']}/{pr['max']}s"
        # 命中率那一格取 planner（跨档唯一可比的那条腿）：narrator 的提示词随对话增长、
        # 前缀稳定度天生不同，两腿混着看会把"该优化哪条腿"这个决策信息抹掉。
        # `n/a` = 这一档一次都没量到缓存字段（**不是 0%**）。
        pl = (d.get("tokens") or {}).get("planner") or {}
        tok = (f"{pl['in_per_call']}/{pl['out_per_call']}"
               if pl.get("in_per_call") is not None else "—")
        hit = f"{pl['hit_rate']:.1%}" if pl.get("hit_rate") is not None else "n/a"
        fl = d.get("fails") or {}
        broken = f"{fl.get('broken_runs', 0)}/{fl.get('case_runs', 0)}"
        print(f"{dial:<22}{rate:<16}{e2e:<20}{round_s:<20}"
              f"{tok:<24}{hit:<10}{broken:<16}"
              f"{str(d['native']['fallback_rate']):<12}"
              f"{str(d['native']['tool_call_completeness']):<10}", flush=True)

    # 逐节点 token 明细：主表只放 planner（可比性），narrator 与其它节点在这里单列。
    nodes = sorted({n for d in out["dials"].values() for n in (d.get("tokens") or {})})
    if nodes:
        print(f"\n{'档':<22}" + "".join(f"{n + ' 入/出(次)':<22}" for n in nodes), flush=True)
        for dial in dials:
            t = out["dials"][dial].get("tokens") or {}
            cells = ""
            for n in nodes:
                v = t.get(n)
                cells += (f"{v['in_per_call']}/{v['out_per_call']} 命中"
                          f"{v['hit_rate']:.1%}" if v and v.get("hit_rate") is not None
                          else (f"{v['in_per_call']}/{v['out_per_call']}" if v
                                else "—")).ljust(22)
            print(f"{dial:<22}{cells}", flush=True)
    print(f"\n[矩阵] 落盘：{args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
