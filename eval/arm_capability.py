# -*- coding: utf-8 -*-
"""**能力台账**：这条臂能不能被这份语料证伪？——A/B 之前必须先回答的那个问题。

**为什么必须有这一层**（20261004，头号风险）：一条**什么都不产出**的臂，在**纯否定族**上
全绿。语料里有六族这样的键（`forbid_cmd_prefixes` / `forbid_cmd_contains` /
`forbid_frame_prefix` / `forbid_exec_tools` / `forbid_task_state` / `forbid_fallback`，
再加 `forbid_tool_calls`）——「本轮**没有**发命令」「**没有**弹出确认卡」「**没有**调这个
工具」在一台什么都不做的机器上**恒真**。于是"第二条臂打平第一条臂"可以完全是假象：
它什么都没做，所以什么都没违规。

**判据（本文件唯一的判据）**：一族被"武装" ⟺ **这臂被观察到能产出该族的指涉物**。
两条独立的路径，命中任一即武装（两条都记，读的人自己看是哪条）：

  · `by_sibling`   —— 语料里的**正向兄弟**（`require_cmd_prefixes` ↔ `forbid_cmd_prefixes`）
    至少有一条在本臂的归档报告里**通过**了。通过了 ⇒ 指涉物真的到场过；
  · `by_own_run`   —— 本臂自己的归档报告里**直接见到**过该族的指涉物（例如某轮的
    `reset_scopes` 里有 `text`、某轮 `commands` 非空）。

**`forbid_fallback` 的特殊性**：语料里**没有** `require_fallback` / `require_reset` 这样的
正向兄弟，所以第一条路径对它**永远不成立**。第二条路径只有在"这臂曾经真的兜过底"时才
成立——对一条还没接闸门（P4）的臂，它天然为假。所以这一族要一个**合成正控**：

    python eval/arm_capability.py --arm react --probe

它把 `server._agent` 临时包一层，在**这条臂自己吐完所有帧之后**补一条
`("updates", {"gate": {"fallback_text": …}})`（**就是 `graph.gate_node` 交回生产的同一个
形状**），让这条臂在**真生产者**（`server._run_agent_stream_to_queue`）上真的走一次兜底，
然后断言 ① 那一帧真的变成了用户可见的兜底文本（`reset_scopes` 里出现 `text`）、
② `forbid_fallback` **变红**。②不成立 ⇒ 判据没接到底，此时这臂的"零兜底"**不算数**
（记 `falsifiable:false`）。**两条臂都不用改一个字节**——后门留在臂里，日后没人清。
react 臂这次用脚本化假模型（零 LLM 零网络）；**未评估绝不许报成通过**（同仓里既有的
`[未评估]` 约定与空分母退出码 3）。

**这一层不判对错，只判"这份读数配不配被读"**：`comparable:false` 时不许引用该臂的
`pass_rate` / `--ab`。首选消费方式 = 用它给出的 `comparable_ids` 做**同一份 `--skip-ids`**
喂给两臂（两侧分母同缩，相对比较可接受）；**绝不许拿子集跑与全量跑相比**。

用法：
    python eval/arm_capability.py --arm graph              # 读 eval/report/runs/
    python eval/arm_capability.py --arm react --json
    python eval/arm_capability.py --arm react --probe      # 跑合成正控（离线）
    python eval/arm_capability.py --arm react --print-skip-ids
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

CORPUS = ROOT / "eval" / "golden" / "basic.jsonl"

# 语料里所有**断言类**的键（与 `run_golden.GOLD_ASSERT_KEYS` 同源；这里只用于"这条用例
# 的 gold 键认不认识"，不重复那边的判据实现）。
ASSERT_KEYS = frozenset({
    "nonempty", "text_contains", "text_not_contains", "text_any_regex",
    "not_match_exempt_mention", "not_match_exempt_conditional", "not_match_exempt_refuted",
    "not_match_exempt_doc_fact", "not_match_exempt_consequence", "not_contains_exempt_quote",
    "require_denial", "require_absence",
    "require_cmd_prefixes", "require_cmd_contains", "require_cmd_all",
    "forbid_cmd_prefixes", "forbid_cmd_contains", "either_cmd_or_text",
    "require_tool_calls", "require_tool_calls_any", "no_tool_calls", "forbid_tool_calls",
    "require_exec_tools", "require_exec_args", "require_arg_from_result",
    "require_exec_result", "require_zero_exec", "forbid_exec_tools",
    "require_frame_prefix", "forbid_frame_prefix", "forbid_fallback",
    "require_confirm_payload",
    "require_ledger_frame", "require_ledger_rows", "require_card_targets_from_ledger",
    "require_task_state", "forbid_task_state", "require_doc_terms",
    "needs_summary", "round", "confirm_message",
})

# ── 指涉物的"见证者" ────────────────────────────────────────────────────────
# 每一族的指涉物**在归档报告里长什么样**。报告有两层（用例级扁平 + `rounds[]` 逐轮），
# 见证者两层都看：有的字段只在逐轮那份里（`exec_tools` / `frames`），有的两层都有。
# **只在报告里真看得见的东西才算见证**——拿不到见证的族宁可判"未武装"（并因此让这臂
# 不可比），也不许用"我觉得它能产出"来顶替（那正是这个文件要防的那种自欺）。
def _rounds(case: dict) -> list[dict]:
    return list(case.get("rounds") or [])


def _frames(case: dict) -> Iterator[str]:
    for r in _rounds(case):
        for f in (r.get("frames") or []):
            yield str(f)


def _w_cmd(case: dict, _values: list) -> bool:
    return (any(r.get("commands") for r in _rounds(case)) or bool(case.get("commands")))


def _w_tool(case: dict, _values: list) -> bool:
    return (any(r.get("tool_calls") for r in _rounds(case)) or bool(case.get("tool_calls")))


def _w_exec(case: dict, _values: list) -> bool:
    return any(r.get("exec_tools") for r in _rounds(case))


def _w_frame_prefix(case: dict, values: list) -> bool:
    """见过**该族自己禁用的那几种前缀**里的任意一种 —— 前缀取自语料断言值，不写死。"""
    pre = tuple(str(v) for v in values)
    return bool(pre) and any(x.startswith(pre) for x in _frames(case))


def _w_task(case: dict, _values: list) -> bool:
    """见过 `__TASK__` 帧（`forbid_task_state` 禁的是它的 state 取值）。

    两处都看：`__TASK__` 解析后**不进** `frames`，所以 20261004 起报告另留了逐轮
    `task_frames`（`eval/run_golden.py` 那段）；旧归档没有这一格，只能靠正向兄弟武装。
    """
    return (any(x.startswith("__TASK__") for x in _frames(case))
            or any(r.get("task_frames") for r in _rounds(case)))


def _w_fallback(case: dict, _values: list) -> bool:
    """见过 **text-scope** 的 `__RESET__`（终局兜底）。

    不是"有 reset 就算"——`scope=all` 是"打回重规划"，用户最终看到的是重查之后的真回答，
    与"用户收到了道歉"正好相反（见 `landing_gate.fallback_resets` 的头注）。
    """
    z = [str(s) for s in (case.get("reset_scopes") or [])]
    z += [str(s) for r in _rounds(case) for s in (r.get("reset_scopes") or [])]
    return "text" in z


# ── 族表 ────────────────────────────────────────────────────────────────────
# 每行 = 一个**纯否定族**（负向断言，其绿在"什么都不做"的臂上恒真）。
#   key      ：负向族本身
#   siblings ：语料里的正向兄弟（同一指涉物的"有"断言）
#   witness  ：`(一条报告用例, 该族在语料里的断言取值) -> 这臂见过指涉物吗`
FAMILIES: tuple[dict, ...] = (
    {"key": "forbid_cmd_prefixes", "witness": _w_cmd,
     "siblings": ("require_cmd_prefixes", "require_cmd_contains", "require_cmd_all",
                  "either_cmd_or_text")},
    {"key": "forbid_cmd_contains", "witness": _w_cmd,
     "siblings": ("require_cmd_prefixes", "require_cmd_contains", "require_cmd_all",
                  "either_cmd_or_text")},
    {"key": "forbid_tool_calls", "witness": _w_tool,
     "siblings": ("require_tool_calls", "require_tool_calls_any")},
    {"key": "forbid_exec_tools", "witness": _w_exec,
     "siblings": ("require_exec_tools", "require_exec_args", "require_arg_from_result")},
    {"key": "forbid_frame_prefix", "witness": _w_frame_prefix,
     "siblings": ("require_frame_prefix",)},
    {"key": "forbid_task_state", "witness": _w_task,
     "siblings": ("require_task_state",)},
    # 没有正向兄弟（语料里不存在 require_fallback）⇒ 只能靠实证或 --probe 的合成正控。
    {"key": "forbid_fallback", "witness": _w_fallback,
     "siblings": (), "probe": "forced_fallback"},
)


def load_corpus(path: Path = CORPUS) -> list[dict]:
    """语料 → `[{id, keys: set, values: {key: [取值…]}}]`（多轮用例按轮并集）。

    取值也要留：`forbid_frame_prefix` 这类族"禁用什么前缀"是**语料自己写的**，
    见证者照着它认（写死 `__CONFIRM__` 就是把两处口径分开养，改一处另一处静默失效）。
    """
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        rounds = c.get("rounds") or [c]
        keys: set[str] = set()
        values: dict[str, list] = {}
        for r in rounds:
            for k, v in (r.get("gold") or {}).items():
                if k not in ASSERT_KEYS:
                    continue
                keys.add(k)
                values.setdefault(k, []).extend(v if isinstance(v, list) else [v])
        out.append({"id": c["id"], "keys": keys, "values": values})
    return out


def load_reports(pattern: str) -> tuple[list[dict], list[dict]]:
    """归档报告 → `(报告列表, 逐例记录列表)`；目录为空就返回空表（不报错）。"""
    reports = []
    for p in sorted(glob.glob(pattern)):
        try:
            reports.append(json.load(open(p, encoding="utf-8")))
        except Exception:  # noqa: BLE001 —— 半个文件不该让整个分析器倒掉
            continue
    cases: list[dict] = []
    for r in reports:
        for c in r.get("cases") or []:
            cases.append({"report": Path(r.get("_path") or "").name, **c})
    return reports, cases


def _passed(case: dict) -> bool:
    """这一例在这一份报告里通过了没有（键名跨版本兼容：`ok` / `final_ok`）。"""
    return bool(case.get("ok") if case.get("ok") is not None else case.get("final_ok"))


def analyze(arm: str, *, reports_pattern: str = "", probe: dict | None = None,
            corpus: list[dict] | None = None, reports: list[dict] | None = None) -> dict:
    corpus = load_corpus() if corpus is None else corpus
    pattern = reports_pattern or f"eval/report/runs{'' if arm == 'graph' else '_' + arm}/*.json"
    if reports is None:
        reports, cases = load_reports(pattern)
    else:
        cases = [c for r in reports for c in (r.get("cases") or [])]
    by_id: dict[str, list[dict]] = {}
    for c in cases:
        by_id.setdefault(str(c.get("id") or ""), []).append(c)

    fams: dict[str, dict] = {}
    for fam in FAMILIES:
        key = fam["key"]
        asserted_ids = [c["id"] for c in corpus if key in c["keys"]]
        asserted = len(asserted_ids)
        values = sorted({str(v) for c in corpus for v in c["values"].get(key, [])})
        sib_ids = [c["id"] for c in corpus if c["keys"] & set(fam["siblings"])]
        by_sib = sorted({cid for cid in sib_ids if any(_passed(r) for r in by_id.get(cid, []))})
        # 见证**不限于**断言这一族的用例：指涉物在别处的运行里出现过，同样证明这臂产得出它
        # （例如 `forbid_tool_calls` 的见证 = 任何一例真的调过工具）。
        by_own = sorted({cid for cid, rs in by_id.items()
                         if any(fam["witness"](r, values) for r in rs)})
        armed = bool(by_sib or by_own)
        note = ""
        if not fam["siblings"]:
            # 只有这一族走这条路：语料里没有正向兄弟，所以"没发生"既可能是真没发生，
            # 也可能是这台机器压根发不出那一帧。要么归档里**真的见过**（by_own），
            # 要么拿合成正控把"会红"这件事直接证出来。
            note = (f"语料里没有正向兄弟，但归档里真的出现过 text-scope 兜底 {len(by_own)} 次 ⇒ 已凭实证武装"
                    if by_own else
                    "语料里没有正向兄弟 ⇒ 只能靠合成正控（--probe）；正控为红才算这臂的『没兜底』可读")
        elif not armed:
            note = "既没有通过过的正向兄弟，也没在归档里直接见到过指涉物"
        if fam.get("probe") and probe:
            armed = bool(probe.get("falsifiable"))
            note = probe.get("detail") or note
        fams[key] = {
            "asserted": asserted, "cases": asserted_ids,
            "forbidden_values": values, "siblings_in_corpus": sib_ids,
            "armed": armed, "armed_by_sibling": by_sib, "armed_by_own_run": by_own,
            "note": note,
        }

    # ── 用例级：它的**每一个** gold 键都可证伪吗 ────────────────────────────
    # 判的是**用例**（"它有没有一条绿可能是真空的"），不是"表上还有几个族没武装"：
    # 语料里没人断言的那一族，不会让任何一条用例变得不可信。两种说法在真语料上等价
    # （七族每一族都有用例断言），但只有前者在前一种情况下不会把 comparable 永久钉成 false。
    unarmed = {k for k, v in fams.items() if not v["armed"]}
    unarmed_asserted = {k for k in unarmed if fams[k]["asserted"]}
    comparable_ids: list[str] = []
    unfalsifiable_ids: list[str] = []
    for c in corpus:
        hit = c["keys"] & unarmed
        if hit:
            unfalsifiable_ids.append(c["id"])
        else:
            comparable_ids.append(c["id"])

    total_inst = sum(v["asserted"] for v in fams.values())
    fals_inst = sum(v["asserted"] for v in fams.values() if v["armed"])
    return {
        "arm": arm,
        "reports": {"pattern": pattern, "n": len(reports),
                    "cases": len(cases),
                    "caveat": "没有任何归档报告 ⇒ 每一族都只能靠语料侧的正向兄弟判定"
                    if not reports else ""},
        "corpus": {"path": str(CORPUS.relative_to(ROOT)), "cases": len(corpus)},
        "families": fams,
        "unarmed_families": sorted(unarmed),
        # `comparable` 只看**有断言在身的**那些族（协议原话：「每个含断言的纯否定族」）——
        # 语料里没人断言的一族不会让任何一条用例的绿变成真空绿。两种写法在真语料上等价
        # （七族各有用例），但写成断言侧才读得出"为什么没人断言的族不算数"。
        "unarmed_asserted_families": sorted(unarmed_asserted),
        "comparable": not unarmed_asserted,
        "coverage": {
            "asserted_instances": total_inst,
            "falsifiable_instances": fals_inst,
            "ratio": round(fals_inst / total_inst, 4) if total_inst else None,
        },
        "comparable_ids": comparable_ids,
        "unfalsifiable_ids": unfalsifiable_ids,
        "probe": probe or {},
    }


# ── 合成正控 ────────────────────────────────────────────────────────────────
_FALLBACK_MARK = "gate fallback"   # `check_gold` 那一条红的原话里的固定片段（见 run_golden.py:2042）


def probe_forced_fallback(arm: str, *, uid: int = 1) -> dict:
    """让这臂在**真生产者**上发一次兜底，确认 `forbid_fallback` **会变红**。

    链路四段**全走真的**：臂 → `server._run_agent_stream_to_queue`（`emit_reset("text",…)`）
    → 帧 → `run_golden.run_one` 的解析（`parse_reset` / `fallback_resets`）→ `check_gold`。
    自己拼一份 `result` dict 只验最后一段，而"判据没接到底"恰恰最爱漏在中间那几段——
    所以这里**不合成 result**，真的跑一遍 producer。

    **注入点不在这条臂里**（两条臂一个字节都不用改）：把 `server._agent` 临时包一层，
    在**它自己吐完所有帧之后**追加一条
    `("updates", {"gate": {"fallback_text": …}})` —— 那就是 `graph.gate_node` 交回生产的
    **同一个形状**（producer 照 `emit_reset("text", …)` 处理并把最终回复换成它）。
    这么做同时换掉"给每条臂各加一个 debug 后门"这件事：后门留在臂里，日后没人清。

    离线：react 臂的 `_llm` 用脚本化假模型替掉（零网络零费用）；其他臂没有替身就用真模型
    ——**所以默认只在需要它的那条臂上跑**（graph 臂的兜底有归档实证，不需要探针）。
    判据是**红里写着"gate fallback"**（不是"有红就行"：别的通用检查也可能顺带开火，
    那种红不算正控命中）。
    """
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from agent.principal import Principal

    import run_golden
    import server

    os.environ["GOLDEN_ARM"] = arm
    run_golden.ensure_agent()
    base = server._agent
    forced = "抱歉，这句我核不实，先如实回你：这一轮我没有能确认的结论。"
    fake_llm = False
    mod = sys.modules.get(f"agent.{arm}_arm")
    if mod is not None and hasattr(mod, "_llm"):
        class _S(GenericFakeChatModel):
            def bind_tools(self, tools, **kw):  # noqa: ANN001, ANN003
                return self

        mod._llm = lambda: _S(messages=iter([AIMessage(content="这句是模型自己写的正文。")]),
                              ai_message_chunk=iter([]))
        fake_llm = True

    class _ForcedGate:
        """臂外层：帧一个不少地转发，最后补一条真闸门那格。"""

        name = f"{getattr(base, 'name', '')}+forced_gate"

        def stream(self, state, config=None, stream_mode=None):  # noqa: ANN001
            yield from base.stream(state, config, stream_mode)
            yield ("updates", {"gate": {"fallback_text": forced}})

        def __getattr__(self, k):  # 生产者今天只调 .stream；其余属性原样透传
            return getattr(base, k)

    server._agent = _ForcedGate()
    try:
        case = {"id": "_probe_forced_fallback", "user_input": "谢谢你",
                "context": {}, "gold": {"forbid_fallback": True}}
        res = run_golden.run_one(run_golden.build_request(case),
                                 Principal(uid=uid, role="superadmin"))
        fails = run_golden.check_gold({"forbid_fallback": True}, res)
    finally:
        server._agent = base

    scopes = list(res.get("reset_scopes") or [])
    replaced = str(res.get("text") or "").strip() == forced
    hit = [f for f in fails if _FALLBACK_MARK in f]
    detail = (f"强制兜底一次后：reset_scopes={scopes}，用户可见文本被换成兜底={replaced}，"
              f"forbid_fallback {'变红 ✅' if hit else '**没变红 ❌**（判据没接到底）'}"
              + ("" if fake_llm else "（⚠️ 这条臂没有假模型替身，本次用的是真模型）"))
    return {"ok": bool(hit) and replaced and "text" in scopes, "falsifiable": bool(hit),
            "reset_scopes": scopes, "text_replaced": replaced,
            "fails": fails, "detail": detail}


def main() -> int:
    ap = argparse.ArgumentParser(description="能力台账：这条臂能不能被语料证伪")
    ap.add_argument("--arm", default="graph", help="graph / react …（决定默认的报告目录）")
    ap.add_argument("--reports", default="", help="报告 glob（缺省按臂推目录）")
    ap.add_argument("--probe", action="store_true", help="跑合成正控（forbid_fallback）")
    ap.add_argument("--uid", type=int, default=1, help="正控用的 uid（默认 1）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--print-skip-ids", action="store_true",
                    help="打印**不可证伪**的用例 id（逗号分隔，直接喂 --skip-ids）")
    ap.add_argument("--report-out", default="", help="把 JSON 也写一份到该路径")
    a = ap.parse_args()

    probe = probe_forced_fallback(a.arm, uid=a.uid) if a.probe else None
    out = analyze(a.arm, reports_pattern=a.reports, probe=probe)

    if a.print_skip_ids:
        print(",".join(out["unfalsifiable_ids"]))
        return 0
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"臂 = {out['arm']}  归档 = {out['reports']['pattern']}"
              f"（{out['reports']['n']} 份 / {out['reports']['cases']} 例）")
        if out["reports"]["caveat"]:
            print("  ⚠️ " + out["reports"]["caveat"])
        print(f"语料 = {out['corpus']['path']}（{out['corpus']['cases']} 条）")
        print("\n族                                   断言  正向兄弟通过  归档里见到  armed")
        for k, v in out["families"].items():
            print(f"  {k:32s} {v['asserted']:4d}  {len(v['armed_by_sibling']):9d}"
                  f"      {len(v['armed_by_own_run']):6d}   {'✅' if v['armed'] else '❌'}")
            if v["note"]:
                print(f"      ↳ {v['note']}")
        cov = out["coverage"]
        print(f"\n可证伪键实例 {cov['falsifiable_instances']}/{cov['asserted_instances']}"
              f" = {cov['ratio']}；comparable = {out['comparable']}")
        if out["unarmed_asserted_families"]:
            print("未武装族（语料里有断言的）：" + "、".join(out["unarmed_asserted_families"]))
        _rest = [k for k in out["unarmed_families"] if k not in out["unarmed_asserted_families"]]
        if _rest:
            print("（另有一族语料里没人断言，不影响可比）：" + "、".join(_rest))
        print(f"可比用例 {len(out['comparable_ids'])} 条 / 不可证伪 {len(out['unfalsifiable_ids'])} 条"
              f"（后者用 --print-skip-ids 取）")
        if probe:
            print("\n[合成正控] " + str(probe.get("detail")))
    if a.report_out:
        json.dump(out, open(a.report_out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    return exit_code(out)


def exit_code(out: dict) -> int:
    """0 = 这份读数可以读；3 = **未评估**（同仓里既有的约定，见空分母那族）。

    单独抽出来是为了能被测：这条约定一旦写成"总是 0"，所有下游的"未评估"就全变成通过了。
    """
    return 0 if out.get("comparable") else 3


if __name__ == "__main__":
    sys.exit(main())
