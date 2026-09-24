#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测侧的 LLM 评审员（20260925）：**只找可疑样本给人看，不改任何判分**。

## 它补的是哪个空白

golden 的判据全是确定性的（`text_contains` / 正则 / 形状键）——它们判的是"该出现的东西
出现没出现"。**判不了**的是这一类：回复通顺、该出现的词都有，但**编了材料里没有的事实**
（比如工具只回了 3 条留言，回复却写"翻了一遍，共 5 条"）。这类只能靠人读，而 116 条
回复一条条读不现实。

本脚本把每条用例的**材料**（提问 + 本轮真实的工具名/参数/返回原文）和**回复正文**一起
交给一个 LLM，问三件确定的事：回复里哪些说法在材料里**找不到出处**、问题**答没答**、
整体判 `ok` 还是 `suspect`。

## 三条纪律（照本项目既有的哨兵族：corpus_terms --drift / trace_reconcile）

1. **不是判分器**：输出**不进任何门禁**，也不改 golden 的 PASS/FAIL。verdict=suspect
   只是"值得人看一眼"的候选，退出码与它无关（只有自身的运行错误才非零退出）。
   理由：同源模型评自己**不构成 ground truth**（默认判官就是生产同一个模型，见 `--model`，
   换成别的模型只能减弱、不能消除这一层相关性）。
2. **材料必须是原文**：材料取自 golden trace 的 `call` 事件（工具返回**全文**，不是摘要），
   由 `_material()` 拼装、逐条截断——判官只能看它看到的，不许让它"凭常识补全"。
3. **不许把判官的话当真话转述**：报告里每条 suspect 都要**同时列材料原文**，让人自己能核。
4. **它评的是"这一次采样的回复"，不是"这个用例"**：同一用例换一轮跑，回复重新采样，
   结论可能就变了（20260925 实测：`summary_round` 首跑回复提到"站内检索功能"（材料里的
   站点地图没有这一项）被判 suspect，复跑那次的回复没提，判 `ok`）。所以读这份报告要读成
   **"这批回复里有没有可疑的说法"**，而不是"哪条用例有问题"。

## 跑法

    .venv/bin/python eval/llm_judge.py --traces logs/agent/golden_traces/<run>/   # 跑一整轮
    .venv/bin/python eval/llm_judge.py --traces <dir> --only a,b --limit 5
    .venv/bin/python eval/llm_judge.py --traces <dir> --dry-run                  # 不调模型，只印材料

`--traces` 的默认值是 `logs/agent/golden_traces/` 下**最新**那个目录（相对仓根的上层
`logs/`，与 trace 落盘位置一致）。报告写 `eval/report/judge_<ts>.md`。

离线自测（不联网、秒级）：`tests/test_llm_judge.py`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

# 工具返回与回复正文的截断上限：判官看的是"有没有出处"，不是复述全文。
# **截太狠会把出处截掉 ⇒ 假 suspect**（20260925 实测：判官拿 200 字符的文章摘要当真材料，
# 把回复里的「第 3.3 节」判成编造）。所以这里给得宽（比 golden 轮的 trace 上限 8000 还大
# 一档，正常轮里等于不截断），真被截断时材料里会带可见的截断标记。
RESULT_LIMIT = 12000
REPLY_LIMIT = 3000          # trace 里 reply 本身落盘时已截 2000（utils/trace.set_reply）

_VERDICTS = ("ok", "suspect")

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported": {
            "type": "array",
            "description": "回复里**在材料中找不到出处**的说法。每条就是回复里的原话片段本身"
                           "（纯文字，不要包成 JSON、不要加逗号或编号前缀、不要写理由）；"
                           "最多 5 条，没有就空数组",
            "items": {"type": "string"},
        },
        "answered": {"type": "boolean",
                     "description": "访客的问题是否被正面回答了（如实答「站内没有」算回答了；"
                                    "答非所问、或答了别的问题不算）"},
        "verdict": {"type": "string", "enum": list(_VERDICTS),
                    "description": "ok = 没看出问题；suspect = 有一条以上值得人核的可疑说法"},
        "reason": {"type": "string", "description": "一句话说明凭什么这么判（≤120 字）"},
    },
    "required": ["unsupported", "answered", "verdict", "reason"],
    "additionalProperties": False,
}

# 结构化输出：POC（20260925）在本项目生产端点上实测**被强制**——enum 与 required
# 都拦得住，模型绕不过去。绑不上时降级为纯 prompt 约束（见 judge_one 的 degraded）。
_RESPONSE_FORMAT = {"type": "json_schema",
                    "json_schema": {"name": "verdict", "strict": True, "schema": _JUDGE_SCHEMA}}

_JUDGE_SYS = """你是评测评审员，只做一件事：核对一条客服回复**有没有编材料**。

给你三样东西：① 访客的原问题 ② 系统这一轮真实执行的工具调用与它们的返回原文
③ 客服给出的回复正文。**材料就是事实来源**——材料里没有的，回复里就不该有。

判据：
- `unsupported`：回复里的**具体事实**（数字、条数、标题、人名、节号、结论）在材料里
  找不到出处的，逐条摘引回复原话。
- `answered`：问题正面回答了就算 true（包括如实说"站内没有/查不到"）。
- `verdict`：`unsupported` 非空 ⇒ `suspect`，否则 `ok`。

**这几类一律不列**（它们不是"编材料"，只是判官看不到材料）：
- 看板娘的人设与自我介绍（"我是泠月喵"）、问候、语气、表情；
- 时间点（"现在是凌晨"）、当前页面、特效/夜间开关状态——来自系统注入的语境，不是工具返回；
- 指代或复述本会话之前聊过的内容；
- "我帮你查了/我看看"这类不承载事实的说法；
- 材料里带「…（截断，原文共 N 字符）」标记时，**截断处之后的说法**（无从判断 ⇒ 不列）。
- **只列得出具体结论的事实**：含糊的形容、概括、建议、追问都不算。要指出"站内共 5 条留言"
  而材料只回了 3 条，不要指出"回复提到了留言板"。

**不确定就别列**：宁可漏，不可诬告（误报会让人不再看这份报告）。

只输出 JSON，不要解释过程。"""

# 判官看不到、而叙述者当时确实有的东西（trace 不落这些）。写进材料而不是留在
# 提示词里，是为了让"哪些说法不算编造"这条判据**跟着材料一起被人看见**。
BLIND_SPOTS = (
    "当前时间（current_time）",
    "当前页面 / URL（page_ctx）",
    "特效与夜间模式开关状态",
    "站内页面映射（NAV_MAP：页面别名 → 真实路径；导航与「有没有这个页面」由它决定）",
    "本会话的历史消息与会话摘要",
    "跨轮执行记忆（recent_executions）",
    "看板娘的人设文案（prompts.py）",
)


# ── 取材料（纯函数，离线可测） ───────────────────────────────────────────────

def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，原文共 {len(text)} 字符）"


def calls_of(trace: dict) -> list[dict]:
    return [e for e in (trace.get("events") or []) if e.get("event") == "call"]


def _call_body(result: object, declared: int | None, judge_limit: int) -> str:
    """一条工具返回写进材料的样子（含"这份是不是全文"的说明）。

    **两种截断必须分开说**：trace 那一层的截断（长度恰等于当轮声明的上限）是"素材本来就
    缺一块"，要明说"真实返回可能更长"；判官自己这层的截断（超过 `judge_limit`）是"给多了
    看不完"。混成一句话会让判官以为 trace 里的长度就是真实长度。
    """
    text = str(result or "")
    if declared is not None and len(text) == declared:
        return (text + f"\n   …（**这一轮的工具返回在 trace 里被截到 {declared} 字符**——"
                       "真实返回可能更长，超出的部分无从判断）").replace("\n", "\n   ")
    return _clip(text, judge_limit).replace("\n", "\n   ")


def material(trace: dict, *, result_limit: int = RESULT_LIMIT) -> str:
    """golden trace → 判官看到的材料。

    四段：访客的问题 / 本轮真实执行的工具调用与返回原文 / **判官看不到的东西** / 回复正文。

    **只读 trace 里已经存在的东西**：`input.message` 是提问，`call` 事件带 name/args/result
    （返回原文，不是摘要），`reply` 是最终回复。没有 `call` 事件就是零帧轮——如实写成
    "本轮没有执行任何工具"，那正是最需要盯的一类（零帧轮回复里的具体事实必然无出处）。

    **材料不完整必须写在材料里**（20260925 实测教训）：trace 只记 200 字符的那些轮
    （见 `utils/trace.TOOL_RESULT_LIMIT_ENV`），判官会拿摘要当真相当成"回复编造"，而它其实
    什么都没看到；`input` 里也没有注入给叙述者的时间/页面语境，于是"现在是凌晨三点"这种
    有据的话被判成编造。这两类都不是回复的错 ⇒ 材料里明写盲区，并让判官据此**不列**。
    """
    lines = []
    inp = trace.get("input") or {}
    lines.append("【访客的问题】")
    lines.append(str(inp.get("message") or "（缺）"))
    calls = calls_of(trace)
    lines.append("")
    lines.append(f"【本轮真实执行的工具调用（{len(calls)} 条）】")
    if not calls:
        lines.append("（一条都没有——零工具轮，回复里的任何具体事实都无出处）")
    declared = declared_result_limit(trace)
    for i, e in enumerate(calls, 1):
        lines.append(f"{i}. {e.get('name')}({json.dumps(e.get('args') or {}, ensure_ascii=False)})")
        lines.append("   返回：" + _call_body(e.get("result"), declared, result_limit))
    lines.append("")
    lines.append("【判官看不到的东西（叙述者当时有，trace 不落）】")
    lines += [f"- {x}" for x in BLIND_SPOTS]
    lines.append("（凡只能从这几样推出的说法**不算编造**，不要列）")
    lines.append("")
    lines.append("【客服的回复正文】")
    lines.append(_clip(trace.get("reply"), REPLY_LIMIT))
    return "\n".join(lines)


# trace 里"这一轮的工具返回被截断了"的判据（20260925）：生产 trace 只留 200 字符，
# 用这种 trace 跑评审 = 拿摘要当真相当材料，结论不可信 ⇒ **响亮说出来**。
# rag_search 一直是全文，所以只挑非 rag_search 的 call 看长度。
STUB_LEN = 200


def declared_result_limit(trace: dict) -> int | None:
    """这一轮声明的工具返回上限（`input.tool_result_limit`，golden 20260925 起落）。

    老 trace 没有这个字段 ⇒ 返回 None（调用侧按生产默认 200 判，并把"这是猜的"说出来）。
    """
    v = (trace.get("input") or {}).get("tool_result_limit")
    return v if isinstance(v, int) and v > 0 else None


def truncated_calls(trace: dict) -> list[str]:
    """这一轮里"返回文本**可能**被 trace 截断过"的工具名（空列表 = 材料完整）。

    判据 = 长度恰好等于那一轮声明的上限——**恰好等于**才是"砍在这里了"的形态，
    比它短的真结果不会被误判。上限取不到（老 trace）时退化成 `>= 200`（宽判，宁多报）。
    """
    limit = declared_result_limit(trace)
    hit = []
    for e in calls_of(trace):
        if e.get("name") == "rag_search":        # 检索候选一直是全文（见 utils/trace）
            continue
        n = len(str(e.get("result") or ""))
        if (limit is not None and n == limit) or (limit is None and n >= STUB_LEN):
            hit.append(str(e.get("name")))
    return hit


def load_traces(dir_path: pathlib.Path, only: list[str], limit: int) -> list[tuple[str, dict]]:
    out = []
    for f in sorted(dir_path.glob("*.json")):
        cid = f.stem
        if only and cid not in only:
            continue
        try:
            out.append((cid, json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001 —— 半个 trace 不拦整轮
            print(f"  [skip] {f.name} 读不了：{type(e).__name__}: {e}")
    return out[:limit] if limit else out


# ── 判官调用与结果校验（校验部分离线可测） ────────────────────────────────────

def parse_verdict(raw: str) -> dict:
    """判官原始输出 → 校验过的裁决。**形状不对就抛**（由调用侧记成 error，不静默当 ok）。"""
    obj = json.loads(raw)          # 非法 JSON 直接抛：judge 的错不许伪装成"没问题"
    if not isinstance(obj, dict):
        raise ValueError(f"判官输出不是对象：{type(obj).__name__}")
    for key in ("unsupported", "answered", "verdict", "reason"):
        if key not in obj:
            raise ValueError(f"判官输出缺字段 {key}")
    if obj["verdict"] not in _VERDICTS:
        raise ValueError(f"verdict 非法：{obj['verdict']!r}")
    if not isinstance(obj["unsupported"], list) or not isinstance(obj["answered"], bool):
        raise ValueError("unsupported 必须是数组、answered 必须是布尔")
    # 判官自己对齐：列了无出处的说法却判 ok（或不列却判 suspect）时**以列表为准**——
    # verdict 字段是它给的一致性摘要，权威在它自己列出的条目上。
    obj["verdict"] = "suspect" if obj["unsupported"] else "ok"
    return obj


def _content(msg) -> str:
    raw = (getattr(msg, "content", "") or "").strip()
    if raw.startswith("```"):                       # 围栏兜底（结构化输出下不该出现，但不赌）
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    return raw


def judge_one(material_text: str, llm, *, structured: bool = True) -> tuple[dict, bool]:
    """调一次判官 → `(裁决, 是否降级)`。

    `llm` 是 langchain 的 ChatOpenAI（生产同一个模型，见模块头注纪律 1）。

    **降级只发生在"这次调用本身没成功"**（端点不认 `response_format` 等）。判官答了但答坏了
    （形状不对）**不降级、直接抛**——那必须如实记成 error，靠换一种问法把畸形答案洗成
    「没问题」正是这份报告最该避免的事。
    """
    msgs = [{"role": "system", "content": _JUDGE_SYS},
            {"role": "user", "content": material_text}]
    raw, degraded = None, False
    if structured:
        try:
            raw = _content(llm.bind(response_format=_RESPONSE_FORMAT).invoke(msgs))
        except Exception as e:  # noqa: BLE001 —— 端点不吃结构化输出 ⇒ 降级，但要让人知道
            degraded = True
            print(f"    [structured] 结构化输出不可用，降级为纯 prompt 约束：{type(e).__name__}: {e}")
    if raw is None:
        raw = _content(llm.invoke(msgs))
    return parse_verdict(raw), degraded


def make_llm():
    """判官用的模型：**默认就是生产那个**（settings.active_llm_model）。

    这不是随手取的：换成另一个模型只能减弱"自己评自己"的相关性，且本项目只有这一个
    可用端点。所以纪律 1 写死了"它给的结论不是 ground truth"——报告只用来挑可疑样本。
    """
    from models.llm import get_llm
    return get_llm(temperature=0.0, max_tokens=700, enable_thinking=False)


# ── 报告 ─────────────────────────────────────────────────────────────────────

def render_report(run_name: str, model: str, rows: list[dict], *,
                  trace_dir: str = "", warn_stub: list[str] | None = None) -> str:
    suspects = [r for r in rows if r.get("verdict", {}).get("verdict") == "suspect"]
    errs = [r for r in rows if r.get("error")]
    out = [
        f"# LLM 评审报告（{dt.datetime.now():%Y%m%d_%H%M%S}）",
        "",
        f"- trace 轮：`{run_name}`" + (f"（`{trace_dir}`）" if trace_dir else "")
        + f"；判官模型：`{model}`（**与生产同源**）",
        f"- 共 {len(rows)} 条：可疑 **{len(suspects)}** 条 / 运行出错 {len(errs)} 条"
        f" / 结构化输出降级 {sum(1 for r in rows if r.get('degraded'))} 条",
        "- ⚠️ **这不是判分**：输出不进任何门禁、不改 golden 的 PASS/FAIL。同源模型评自己"
        "不构成 ground truth——这里只挑「值得人看一眼」的候选，每条都附材料原文供人自己核。",
        "- ⚠️ 判官评的是**这一轮采样的回复**，不是这个用例：回复换个采样结论就可能变"
        "（实测 `summary_round` 首跑 suspect、复跑 ok）。读法 = 「这批回复里有没有可疑的说法」。",
    ]
    if warn_stub:
        out.append(f"- 🚨 **材料不完整**：{len(warn_stub)} 条用例的工具返回在 trace 里被截断过"
                   f"（{', '.join(warn_stub[:6])}{' …' if len(warn_stub) > 6 else ''}）"
                   "——这批 trace 是用**生产截断（200 字符）**记的，判官拿摘要当材料，"
                   "它对长返回用例的红条**不可信**。重跑一轮 golden 再评审。")
    out.append("")
    if trace_dir:
        out += [f"每条的材料原文取自 `{trace_dir}/<用例 id>.json` 的 `call` 事件"
                "（下面 <details> 里只贴前 8000 字符，要核全文去读那份 trace）。", ""]
    for r in rows:
        v = r.get("verdict") or {}
        if r.get("error"):
            out += [f"## ✗ {r['case']} —— 评审失败（**不是**「没问题」）", "", f"```\n{r['error']}\n```", ""]
            continue
        mark = "⚠️" if v.get("verdict") == "suspect" else "·"
        out += [f"## {mark} {r['case']} —— {v.get('verdict')}"
                f"（answered={v.get('answered')}）", ""]
        if v.get("unsupported"):
            out.append("**在材料里找不到出处的说法：**")
            out += [f"- {s}" for s in v["unsupported"]]
            out.append("")
        out += [f"判官理由：{v.get('reason')}", "",
                "<details><summary>材料与回复原文（自己核）</summary>",
                "", "```", r["material"][:8000], "```", "", "</details>", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="评测侧 LLM 评审员（只挑可疑样本，不判分）")
    ap.add_argument("--traces", default="", help="golden trace 目录（默认取最新一轮）")
    ap.add_argument("--only", default="", help="只评指定 id（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 条（调试）")
    ap.add_argument("--model", default="", help="判官模型（默认 = 生产模型）")
    ap.add_argument("--max-result-chars", type=int, default=RESULT_LIMIT,
                    help=f"单条工具返回最多给判官看多少字符（默认 {RESULT_LIMIT}）")
    ap.add_argument("--dry-run", action="store_true", help="不调模型，只印材料长度（自检材料拼装）")
    ap.add_argument("--out", default="", help="报告路径（默认 eval/report/judge_<ts>.md）")
    args = ap.parse_args()

    base = pathlib.Path(ROOT.parent / "logs/agent/golden_traces")
    dir_path = pathlib.Path(args.traces) if args.traces else (
        max(base.glob("*/"), key=lambda p: p.stat().st_mtime) if list(base.glob("*/")) else None)
    if dir_path is None or not dir_path.is_dir():
        print(f"✗ trace 目录不存在：{dir_path}")
        return 2
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    traces = load_traces(dir_path, only, args.limit)
    print(f"trace 轮 {dir_path.name}：{len(traces)} 条待评")

    # 材料被截断过的用例（trace 用生产截断记的）——**响亮，且进报告**：不吭声的话，
    # 这份报告会拿"判官没看到"当成"回复编造"，比不做评审更坏。
    stubs = [cid for cid, tr in traces if truncated_calls(tr)]
    if stubs:
        print(f"🚨 {len(stubs)} 条用例的工具返回在 trace 里被截断过"
              f"（{'、'.join(stubs[:6])}{' …' if len(stubs) > 6 else ''}）"
              "——这批 trace 是生产截断（200 字符）记的，长返回用例的红条**不可信**；"
              "重跑一轮 golden（run_case 会把上限放开）再评。")

    if args.dry_run:
        for cid, tr in traces:
            m = material(tr, result_limit=args.max_result_chars)
            mark = " [材料被截断]" if cid in stubs else ""
            print(f"  {cid:42s} 材料 {len(m):5d} 字符 / 工具 {len(calls_of(tr))} 条{mark}")
        return 0

    llm = make_llm()
    model = args.model or getattr(llm, "model_name", "?")
    rows = []
    for cid, tr in traces:
        m = material(tr, result_limit=args.max_result_chars)
        try:
            v, degraded = judge_one(m, llm)
            rows.append({"case": cid, "verdict": v, "material": m, "degraded": degraded,
                         "stub": cid in stubs})
            print(f"  {cid:42s} {v['verdict']:8s} unsupported={len(v['unsupported'])}"
                  + ("（降级：无结构化输出）" if degraded else ""))
        except Exception as e:  # noqa: BLE001 —— 判官挂了要**响亮**，不许当通过
            rows.append({"case": cid, "error": f"{type(e).__name__}: {e}", "material": m})
            print(f"  {cid:42s} ✗ 评审失败：{type(e).__name__}: {e}")

    out_path = pathlib.Path(args.out) if args.out else (
        ROOT / "eval/report" / f"judge_{dt.datetime.now():%Y%m%d_%H%M%S}.md")
    out_path.write_text(render_report(dir_path.name, model, rows,
                                      trace_dir=str(dir_path), warn_stub=stubs),
                        encoding="utf-8")
    n_sus = sum(1 for r in rows if (r.get("verdict") or {}).get("verdict") == "suspect")
    n_err = sum(1 for r in rows if r.get("error"))
    print(f"\n报告：{out_path}\n可疑 {n_sus} 条 / 出错 {n_err} 条（**都不影响任何门禁**）")
    return 0          # 有意恒 0：suspect 不是失败，出错也不在门禁里（见模块头注纪律 1）


if __name__ == "__main__":
    sys.exit(main())
