#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限影子读数的定点统计：**只测量，不切换**。

不判"该不该拦"（那是人的裁决）；不改任何配置；不跑 LLM、不联网。**只读 trace。**
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from utils.trace_io import load_trace  # noqa: E402

TRACE_DIR = "/home/ubuntu/Saudade-Blog/logs/agent/traces"

SHADOW_EVENT = "authz_shadow"
CHECK_EVENT = "check"
VERDICT_PASS = "PASS"

# "工具没声明 scope" 的原因码。**这是从 `agent/authz.py::REASON_NO_MANIFEST` 抄来的一个字面**
# —— 本脚本刻意不 import agent（离线读取端只吃路径参数），所以这一格靠判据锁：
# `tests/test_authz_shadow.py` ① 断言它与 `authz.REASON_NO_MANIFEST` 逐字相同。
# 不锁的话，authz 那边改了码名，这里会**静默**把真·漏声明归到"其他"里（读数少一格、不报错）。
REASON_NO_MANIFEST = "no_manifest"

# 分歧明细最多列几条（读数是全量的，这里只是给人看的样本）
SAMPLE_MAX = 20

# ── 两个字符串的解析（都有往返锁，见 tests/test_authz_shadow.py ⑧）────────────
# 写侧今天只落 `decision=str(decision)` / `principal=str(principal)`，**没有结构化键**。
# 所以这里从字符串里抠字段 —— 这条解析**必须**有锁：`Decision.__str__` 一改格式，
# 这里会静默解析出空串 ⇒ 按 reason 分档全变 0（"看着在跑、其实没跑"那一族）。
# 判据拿真的 `authz.Decision` / `Principal` 做一次往返，改格式就红。
#
# `Decision.__str__` 的机器可读半边：`deny <tool> reason=<reason> scope=<scope|->`
_DECISION_RE = re.compile(
    r"^(?P<verdict>allow|deny)\s+(?P<tool>\S+)\s+reason=(?P<reason>\S+)"
    r"(?:\s+scope=(?P<scope>\S+))?$")
# `Principal.__str__`：`uid=7 role=admin src=assertion`
_PRINCIPAL_RE = re.compile(
    r"^uid=(?P<uid>\S+)\s+role=(?P<role>\S+)\s+src=(?P<src>\S+)$")


def parse_decision(text: str) -> dict:
    """`Decision.__str__` → `{verdict, tool, reason, scope}`；解析不出 ⇒ 全空串。

    解析不出**不抛**（老 trace / 手改过的 trace 不该让整条扫描中断），但也**不猜**：
    调用端把 reason 为空的那一族单独数出来报 —— "读不出原因"本身是读数。
    """
    m = _DECISION_RE.match(text or "")
    if not m:
        return {"verdict": "", "tool": "", "reason": "", "scope": ""}
    d = m.groupdict()
    return {"verdict": d["verdict"], "tool": d["tool"], "reason": d["reason"],
            "scope": "" if d["scope"] in ("-", "") else d["scope"]}


def parse_principal(text: str) -> dict:
    """`Principal.__str__` → `{uid, role, src}`；解析不出 ⇒ 全空串（同上，不抛不猜）。"""
    m = _PRINCIPAL_RE.match(text or "")
    return m.groupdict() if m else {"uid": "", "role": "", "src": ""}


def shadow_denials(doc: dict) -> list[dict]:
    """一份 trace 里的**影子拒绝**事件（`authz_shadow`），按事件序返回。"""
    out: list[dict] = []
    for ev in doc.get("events") or []:
        if not isinstance(ev, dict) or ev.get("event") != SHADOW_EVENT:
            continue
        dec = parse_decision(str(ev.get("decision") or ""))
        who = parse_principal(str(ev.get("principal") or ""))
        out.append({
            # 顶层 `tool=` 是写侧的结构化键，优先用它；缺失才退回解析出来的那个
            "tool": str(ev.get("tool") or dec["tool"]),
            "reason": dec["reason"],
            "scope": dec["scope"],
            "role": who["role"],
            "uid": who["uid"],
        })
    return out


def executed_tools(doc: dict) -> set:
    """这份 trace 里**真的执行成功**的工具（checker 判 PASS）。

    为什么读 `check` 的 verdict 而不是 `call` 事件：`call` 对**被拦下**的那些也照写 ——
    参数引用失败 / 权限拒绝 / 同意闸 / 目标校验都先产 `__ERROR__` 帧、再照常走一遍
    checker（见 `execute_node` 那一串 elif）。拿 `call` 当"执行了"会把**每一条**影子
    拒绝都算成分歧，读数恒等于分母、一格信息都没有。PASS/BLOCK 才是 checker 对
    "这一件到底成没成"的裁决（`_check_spec`）。
    """
    out = set()
    for ev in doc.get("events") or []:
        if not isinstance(ev, dict) or ev.get("event") != CHECK_EVENT:
            continue
        if ev.get("verdict") == VERDICT_PASS and ev.get("tool"):
            out.add(str(ev["tool"]))
    return out


def analyze(items: list, hard_scopes=None, known_tools=None) -> dict:
    """全部读数的**纯函数**入口（judge 打这里，不碰文件系统）。

    `items` = `[(trace 名, trace 文档)]`；`hard_scopes` = 那组不吃 shadow 的 scope，
    `None` 表示"没取到 ⇒ 这一项没验"（**不是"没有违规"**）。`known_tools` = 工具注册表
    里的名字集合，用来把 `no_manifest` 那一格切成"真·漏声明"与"名字根本不是工具"
    （两义见模块头注）；`None` 同样表示**这一刀没切**，不是"两半都是空的"。
    """
    n = denials = diverged = 0
    with_shadow = 0
    unparsed = 0
    by_reason: dict = {}
    by_tool: dict = {}
    by_role: dict = {}
    samples: list = []
    hard_hits: list = []
    nm_real: dict = {}      # no_manifest 且名字在注册表里 ⇒ 真·漏声明
    nm_phantom: dict = {}   # no_manifest 且名字不在注册表里 ⇒ 解析碎片/幻觉工具名

    for name, doc in items:
        if not isinstance(doc, dict):
            continue
        n += 1
        rows = shadow_denials(doc)
        if not rows:
            continue
        with_shadow += 1
        passed = executed_tools(doc)
        for r in rows:
            denials += 1
            if not r["reason"]:
                unparsed += 1
            hit = r["tool"] in passed
            diverged += int(hit)
            for table, key in ((by_reason, r["reason"] or "(读不出)"),
                               (by_tool, r["tool"] or "(无工具名)"),
                               (by_role, r["role"] or "(身份读不出)")):
                g = table.setdefault(key, {"denials": 0, "diverged": 0})
                g["denials"] += 1
                g["diverged"] += int(hit)
            if r["reason"] == REASON_NO_MANIFEST and known_tools is not None:
                bucket = nm_real if r["tool"] in known_tools else nm_phantom
                bucket.setdefault(r["tool"], []).append(name)
            if hard_scopes and r["scope"] in hard_scopes:
                hard_hits.append({"trace": name, **r})
            if hit and len(samples) < SAMPLE_MAX:
                samples.append({"trace": name, **r})

    return {"traces": n, "with_shadow": with_shadow, "denials": denials,
            "diverged": diverged, "not_executed": denials - diverged,
            "unparsed_reason": unparsed,
            "by_reason": by_reason, "by_tool": by_tool, "by_role": by_role,
            "hard_scope_events": hard_hits, "hard_scopes": (
                None if hard_scopes is None else sorted(hard_scopes)),
            "no_manifest_real": {k: len(v) for k, v in sorted(nm_real.items())},
            "no_manifest_phantom": {k: len(v) for k, v in sorted(nm_phantom.items())},
            "known_tools_checked": known_tools is not None,
            "samples": samples}


def _hard_scopes():
    """Frozen mandatory scopes for interpreting the retired shadow trace format."""
    try:
        from agent import authz
        return frozenset({authz.SCOPE_ADMIN_CONSOLE, authz.SCOPE_WRITE_CONSOLE,
                          authz.SCOPE_WRITE_DEVICE})
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 取不到 agent.authz（{type(exc).__name__}: {exc}）⇒ 硬拦 scope 这一项**没验**")
        return None


def _known_tools():
    """工具注册表里的名字集合 —— **现取，不手抄**（`tools.get_all_tools()` 是既有取法，
    同 `eval/zero_call_residual_probe.py::TOOL_NAMES`）。

    取不到 ⇒ `None` = **这一刀没切**（不是"两半都是空的"）：那会把"注册表漏声明"与
    "解析碎片"混回一格，所以 render 会明说没切。
    """
    try:
        from agent.domains.registry import operation_names
        from tools import get_all_tools
        return frozenset({*(t.name for t in get_all_tools()), *operation_names()})
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 取不到工具注册表（{type(exc).__name__}: {exc}）"
              "⇒ no_manifest 那一格**没切开**（真漏声明与解析碎片混在一起）")
        return None


def _pct(a: int, b: int) -> str:
    return f"{a / b:.1%}" if b else "—"


def render(rep: dict, window: str) -> str:
    L: list = []
    L.append(f"== 权限影子读数（窗口 {window}）==")
    L.append(f"trace {rep['traces']} 份，其中有影子事件的 {rep['with_shadow']} 份")
    if not rep["denials"]:
        L.append("影子拒绝 0 条 —— **这个窗口里没有任何一条工具调用被判过 denied**。")
        L.append("  注意这不是'授予表配得刚好'的证据：它同样可能是**影子事件没写出来**"
                 "（例如所有 scope 都在 enforce 下）。要区分，看 trace 里有没有 "
                 f"`{SHADOW_EVENT}` 这个事件名。")
        return "\n".join(L)
    L.append(f"影子拒绝 {rep['denials']} 条：**分歧 {rep['diverged']} 条"
             f"（{_pct(rep['diverged'], rep['denials'])}）**"
             f"，没执行 {rep['not_executed']} 条")
    L.append("  分歧 = 判 denied 而那一件**真的执行成功**（checker PASS）"
             "⇒ 今天开 enforce 会拦掉这一条。")
    L.append("  没执行 = 判 denied 而那一件本来就没跑（同意闸/目标校验拦了，或 planner 改了主意）"
             "⇒ 开 enforce 不改变行为。")
    if rep["unparsed_reason"]:
        L.append(f"  ⚠ 其中 {rep['unparsed_reason']} 条的 reason 读不出"
                 "（`Decision.__str__` 格式变了？往返锁在 tests/test_authz_shadow.py ⑧）"
                 "——按 reason 的分档因此不完整。")

    L.append("\n-- 按原因码 --")
    for k, g in sorted(rep["by_reason"].items(), key=lambda kv: -kv[1]["denials"]):
        L.append(f"  {k:<16} 拒 {g['denials']:>4}  其中真执行 {g['diverged']:>4}"
                 f"（{_pct(g['diverged'], g['denials'])}）")

    if REASON_NO_MANIFEST in rep["by_reason"]:
        L.append("\n-- no_manifest 再切一刀（**两义，修的不是同一处**）--")
        if not rep["known_tools_checked"]:
            L.append("  ⚠ **没切**（取不到工具注册表）—— 下面两半合起来才是上面那一格。")
        else:
            real, ph = rep["no_manifest_real"], rep["no_manifest_phantom"]
            L.append(f"  名字**在**注册表里（真·漏声明，应该是 0）："
                     f"{sum(real.values())} 条 {real or '{}'}")
            L.append(f"  名字**不在**注册表里（解析碎片/幻觉工具名）："
                     f"{sum(ph.values())} 条 {ph or '{}'}")
            if ph:
                L.append("    后者不是'漏声明的工具'：20260929 生产实证是计划文本的分隔符"
                         "把一条调用撕成 N 条（已由写端 `_esc_spec` 修，"
                         "锁在 tests/test_plan_channel.py ④）。**别把它读成注册表漏项。**")

    L.append("\n-- 按工具（拒得最多的几个）--")
    for k, g in sorted(rep["by_tool"].items(), key=lambda kv: -kv[1]["denials"])[:12]:
        L.append(f"  {k:<28} 拒 {g['denials']:>4}  其中真执行 {g['diverged']:>4}")

    L.append("\n-- 按角色 --")
    for k, g in sorted(rep["by_role"].items(), key=lambda kv: -kv[1]["denials"]):
        L.append(f"  {k:<16} 拒 {g['denials']:>4}  其中真执行 {g['diverged']:>4}")

    if rep["hard_scopes"] is None:
        L.append("\n-- 结构不变量（硬拦 scope 不许出现在影子事件里）--\n"
                 "  ⚠ **这一项没验**（取不到 agent.authz）——别把它读成'没有违规'。")
    else:
        L.append(f"\n-- 结构不变量（硬拦 scope {rep['hard_scopes']} 不许出现在影子事件里）--")
        if rep["hard_scope_events"]:
            L.append(f"  ✗ **{len(rep['hard_scope_events'])} 条违约**：这些影子事件带着"
                     "不吃 shadow 的 scope ⇒ 写侧那行 `must_enforce(decision, principal)`"
                     " 被绕过了。")
            for e in rep["hard_scope_events"][:10]:
                L.append(f"      {e['trace']}  {e['tool']} scope={e['scope']} role={e['role']}")
        else:
            L.append("  ✓ 0 条")

    if rep["samples"]:
        L.append(f"\n-- 分歧明细（最多列 {SAMPLE_MAX} 条）--")
        for s in rep["samples"]:
            L.append(f"  {s['trace']}  {s['tool']:<26} reason={s['reason']:<14} "
                     f"role={s['role']} uid={s['uid']}")
    return "\n".join(L)


def _window_files(root: str, days: int, since: str) -> tuple:
    """窗口过滤（按 trace 文件名里的时间戳，与 `parse_trace_name` 同源）。

    `since` 给了就用它（`YYYYMMDD` 或 `YYYYMMDDTHHMMSS`），否则按 `days` 天回推。
    返回 `(文件表, 窗口标签)`。
    """
    if since:
        lo, label = since[:8], f"≥ {since[:8]}"
    else:
        lo = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        label = f"近 {days} 天（≥ {lo}）"
    keep = []
    for p in iter_trace_files(root):
        stamp, _uid = parse_trace_name(p)
        # 取不到时间戳的（存量平铺命名不规范）**留着**：宁可多看，不可少看
        if not stamp or stamp[:8] >= lo:
            keep.append(p)
    return keep, label


def main() -> int:
    ap = argparse.ArgumentParser(description="权限影子读数（只测量，不切换）")
    ap.add_argument("--root", default=TRACE_DIR, help=f"trace 根目录（默认 {TRACE_DIR}）")
    ap.add_argument("--days", type=int, default=7, help="近 N 天（默认 7；给定 --since 时忽略）")
    ap.add_argument("--since", default="", help="起点 20261018（或 20261018T000000）")
    ap.add_argument("--json", default="", help="把读数写到这个文件")
    a = ap.parse_args()

    files, label = _window_files(a.root, a.days, a.since)
    if not files:
        print(f"== 权限影子读数（窗口 {label}）==")
        print(f"窗口内一份 trace 都没有（根目录 {a.root}）。")
        print("**退 3：账不可用** —— '没数据'不是'零分歧'，别把它读成后者。")
        return 3

    items, bad = [], 0
    for p in files:
        doc = load_trace(p)
        if doc is None:
            bad += 1
            continue
        items.append((os.path.basename(p), doc))

    rep = analyze(items, _hard_scopes(), _known_tools())
    print(render(rep, label))
    if bad:
        print(f"\n⚠ {bad} 份 trace 读不出来（坏文件/半写）—— 读数**不含**它们。")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"\n读数写到 {a.json}")
    return 1 if rep["hard_scope_events"] else 0


if __name__ == "__main__":
    sys.exit(main())
