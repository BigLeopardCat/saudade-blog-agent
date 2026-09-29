#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native tool calls 探针：**发的是将要上线的真 schema 与真 planner 提示词**。

**只读实验**：只发 LLM 请求，不碰数据库、不写 trace、不改任何生产状态。需要网络与 API key，
**因此不入 L0 套件、不进夜间**（`tests/run_all.py` 只收 tests/*.py，本文件在 eval/）。

## ⚠️ 首版的两个偏差（20260927 修正，留着当教训）

首版把用例句子**直接当 user 消息**发出去，并在头注里写着"真提示词"——**那是假的**：

  ① 没发 `_PLANNER_PROMPT`（也就没有技能菜单、页面上下文、判定规则 7）。首版测的是
     "给一个光秃秃的问题 + tools，模型会不会用工具"——而线上要回答的是
     "**在完整提示词里**，模型会不会用工具"。两者不是同一个问题：真实提示词里规则 7
     白纸黑字写着"输出两行纯文本、不要任何其他文字"，**它与 tools 是打架的**。
  ② 由此暴露的设计缺口：native 档必须换掉规则 7（否则测的是"提示词与 schema 谁赢"，
     不是"接口层换没换"）⇒ `agent/graph.py` 的 `{output_contract}` 槽就是这么来的。

现在**渲染真提示词**再发（`_render_planner_prompt(contract=…_NATIVE)`）——与
`planner_node` 走的是**同一个**渲染入口，不再有第二份手工拼装。**这是"能力有测试 ≠
接线有测试"的又一例**：探针跑绿了，但它验的不是线上那条路。

## 它要回答什么（1A 的放行条件，不是"顺手测一下"）

D4 POC（`eval/d4_structured_output_poc.py`）验过 function calling 的支持度，但用的是**玩具
schema**，且**思考档只验了 json_schema、没验 tools**。这两个缺口正好压在 native 主路上：

  ① **`enable_thinking=True` 与 `tools` 同用** —— 端点接受吗？还是 400？
  ② **思考链会不会把 `tool_call.arguments` 截断** —— 这是最可能的失败形态，而且是**静默**的：
     文本档 max_tokens=400 时，思考先吃掉额度，`arguments` 断在半截，`finish_reason=length`。
     native 档因此把预算提到 1200 / timeout 60（见 `config/settings.py`）——**这里就是验它
     够不够的地方**。判据是 `json.loads(arguments)` 成功，不是"看着像"。
  ③ **延迟**：思考档实测 p50 6.4s / max 28.5s，正压文本档的 timeout=30。要拿 p50/max 说话，
     不能靠印象。
  ④ **`parallel_tool_calls=False` 真的被遵守吗** —— 网关忽略它的话，多意图那句会回两条调用，
     而下游"一张确认卡只装同一个技能的动作"是按一轮一条设计的（多了只取第一条，见
     `agent/native_plan.tool_calls_to_plan`）。**必须知道它是不是常态**。
  ⑤ **`tool_choice="auto"` 下闲聊轮会不会被逼出假技能调用** —— 这是不能用 `required` 的原因。

## 跑法（cd saudade-blog-agent，需网络）

  .venv/bin/python eval/native_tools_probe.py                       # 每句 3 次，思考关/开各一轮
  .venv/bin/python eval/native_tools_probe.py --trials 5
  .venv/bin/python eval/native_tools_probe.py --no-thinking         # 只跑思考关（快）
  .venv/bin/python eval/native_tools_probe.py --json                # 机器可读摘要

报告同时落 `eval/report/native_probe_<ts>.md`。**本脚本不改任何文档**，结论由人手写进
`docs/native-toolcalls-mainline.md` 或 ADR。
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime

from langchain_core.messages import HumanMessage
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.context import _doc_anchors, _frame_texts, _recent_tail, _short_reply_hint  # noqa: E402
from agent.graph import (  # noqa: E402
    _PLANNER_OUTPUT_CONTRACT_NATIVE,
    _intent_hints,
    _render_planner_prompt,
)
from agent.native_plan import build_tool_schema  # noqa: E402
from agent.refs import ref_hints  # noqa: E402
from config.settings import settings  # noqa: E402

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")

# native 档的预算（与 config/settings.py 的三个 planner_native_* 同值）——**探针必须用
# 上线要用的那一组**，否则测出来的"够不够"不是线上那个问题的答案。
NATIVE_ARGS = {"temperature": 0.2, "max_tokens": 1200, "timeout": 60}

# 探针用例：每句对应一个具体风险，不是随手挑的对话。
CASES = [
    ("nav_enum_arg", "带我去物联网平台", "闭集/映射：target 走 NAV_MAP"),
    ("effect_enum", "把樱花打开", "两个闭集参数同用（effect/action）"),
    ("chitchat", "你好呀，今天过得怎么样", "auto 不该被逼出假技能调用"),
    ("multi_intent", "把樱花打开，顺便切到夜间模式", "parallel_tool_calls=False 是否被遵守"),
    ("content_query_array", "留言板和说说里有没有人聊过 ESP32",
     "数组参数 + calls 的嵌套 object 闭集"),
]

# planner 的角色：探针默认用公开身份（技能 11 个）；--admin 换成 34 个，看 schema 变大后的稳定性
ROLE = None


def _client() -> OpenAI:
    return OpenAI(api_key=settings.active_llm_api_key,
                  base_url=settings.active_llm_base_url,
                  timeout=NATIVE_ARGS["timeout"])


def _real_prompt(user_msg: str) -> str:
    """渲染**线上真正会发的那份** planner 提示词（见头注：首版没发它）。

    走 `_render_planner_prompt` ——与 `planner_node` **同一个入口**，所以这里不可能
    悄悄漂移成第二份拼装。页面上下文/轮次/工具结果给的是"首轮、无帧"的常态值，
    其余全部走真函数（技能菜单、工具菜单、锚点、意图清单都由真代码生成）。
    唯一按 native 档取值的只有规则 7（输出契约）——那正是本探针要验的那一格。
    """
    msgs = [HumanMessage(content=user_msg)]
    return _render_planner_prompt(
        role=ROLE,
        page_ctx=("current_url=/\ncurrent_effects=（无）\ncurrent_darkmode=off"),
        round_info="当前决策：第 1/4 轮。本轮尚无工具执行，是首轮决策。",
        user_msg=user_msg,
        intent_hints=_intent_hints([], user_msg),
        doc_anchors=_doc_anchors(msgs),
        recent_context=_recent_tail(msgs),
        short_reply_hint=_short_reply_hint(msgs),
        # 待办台账（20260929 批 H · S1）：本探针只喂一句裸问题、也不带 config，
        # 拿不到现场队列 ⇒ 填"这一轮没去读"的缺省语（与 `planner_node` 同款）。
        # 要连台账一起验，得自己构造一份帧传进来（`graph._pending_ledger_frame`）。
        pending_ledger="（本轮没有去读待办台账）",
        tool_results=_frame_texts([]),
        ref_hints=ref_hints([]),
        reflector_feedback="（本决策轮无复盘建议）",
        correction="（本决策轮无纠偏提示）",
        contract=_PLANNER_OUTPUT_CONTRACT_NATIVE)


def _once(client: OpenAI, model: str, tools: list[dict], prompt: str,
          thinking: bool) -> dict:
    """发一次请求，回一行结构化记录。**异常也记成一行**（不是抛出去中断整轮）。

    `prompt` 传的是**用例那句话**；发出去的是它的真提示词渲染（`_real_prompt`）——
    线上 `planner_node` 也是把整份提示词作为**单条** user 消息发出去的，形状一致。
    """
    rec = {"prompt": prompt, "prompt_chars": 0, "thinking": thinking, "http_ok": False,
           "error": "", "n_calls": 0, "names": [], "args_parsed": None,
           "finish_reason": "", "content_len": 0, "ms": 0}
    full = _real_prompt(prompt)
    rec["prompt_chars"] = len(full)
    t0 = time.monotonic()
    try:
        body = dict(model=model, messages=[{"role": "user", "content": full}],
                    tools=tools, tool_choice="auto", parallel_tool_calls=False,
                    **{k: v for k, v in NATIVE_ARGS.items() if k != "timeout"})
        if settings.llm_provider.lower() == "qwen":
            body["extra_body"] = {"enable_thinking": bool(thinking)}
        resp = client.chat.completions.create(**body)
    except Exception as e:                      # noqa: BLE001 —— 探针要把失败也变成数据
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
        return rec
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    rec["http_ok"] = True
    choice = (resp.choices or [None])[0]
    if choice is None:
        rec["error"] = "choices 为空"
        return rec
    rec["finish_reason"] = str(getattr(choice, "finish_reason", "") or "")
    msg = choice.message
    rec["content_len"] = len(getattr(msg, "content", "") or "")
    calls = list(getattr(msg, "tool_calls", None) or ())
    rec["n_calls"] = len(calls)
    rec["names"] = [getattr(c.function, "name", "?") for c in calls]
    # 判据是**能不能解析**，不是"看着像 JSON"——截断的 arguments 也会以一串花括号开头
    parsed = []
    for c in calls:
        try:
            got = json.loads(getattr(c.function, "arguments", "") or "")
            parsed.append(isinstance(got, dict))
        except Exception:                       # noqa: BLE001
            parsed.append(False)
    rec["args_parsed"] = parsed
    return rec


def _pct(xs: list[int], q: float) -> int:
    return int(statistics.quantiles(xs, n=100)[min(98, int(q * 100))]) if len(xs) > 1 else (
        xs[0] if xs else 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--thinking", action="store_true",
                    help="只在思考档跑（默认思考关/开各跑一轮）")
    ap.add_argument("--no-thinking", action="store_true", help="只跑思考关（快）")
    ap.add_argument("--admin", action="store_true", help="用 admin 身份（34 个技能）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    global ROLE
    ROLE = "admin" if args.admin else None
    model = settings.active_llm_model
    tools = build_tool_schema(ROLE)
    schema_kb = len(json.dumps(tools, ensure_ascii=False)) / 1024
    prompt_chars = len(_real_prompt(CASES[0][1]))

    rounds = [True] if args.thinking else ([False] if args.no_thinking else [False, True])
    client = _client()
    rows: list[dict] = []
    print(f"模型 {model} | provider {settings.llm_provider} | role {ROLE} | "
          f"技能 {len(tools)} 个 / schema {schema_kb:.1f} KB | "
          f"真提示词 {prompt_chars} 字符 | 预算 {NATIVE_ARGS}")
    for thinking in rounds:
        print(f"\n=== 思考 {'开' if thinking else '关'} ===")
        for key, prompt, why in CASES:
            for i in range(args.trials):
                rec = _once(client, model, tools, prompt, thinking)
                rec.update(case=key, why=why, trial=i)
                rows.append(rec)
                flag = "✅" if rec["http_ok"] and rec["n_calls"] >= 0 else "❌"
                parsed = rec["args_parsed"]
                print(f"  {flag} {key:<20} {rec['ms']:>6}ms  calls={rec['n_calls']} "
                      f"{rec['names']} parsed={parsed} finish={rec['finish_reason']} "
                      f"{rec['error'][:80]}")

    # ── 判定：把"能不能上"压成几行可引用的结论 ──────────────────────────
    verdict: list[tuple[str, bool, str]] = []
    ok = [r for r in rows if r["http_ok"]]
    verdict.append(("端点接受 tools（无 400）", len(ok) > 0 and len(ok) == len(rows),
                    f"{len(ok)}/{len(rows)} 成功；错误样例：" +
                    (next((r['error'] for r in rows if not r['http_ok']), "无")[:120])))
    trunc = [r for r in ok if False in (r["args_parsed"] or [])]
    verdict.append(("arguments 全部可解析（无截断）", not trunc,
                    f"{len(trunc)} 条截断" + (f"：{trunc[0]['case']}" if trunc else "")))
    chat = [r for r in ok if r["case"] == "chitchat"]
    # ⚠️ 判据**不是**"零 tool_call"（20260927 实测纠错）：`chat` 本身就是注册表里的技能
    # （闲聊），点它是**对的**，而且比不点更好——回复文本进了 `reply` 参数而不是散在
    # content 里，两条路在 `tool_calls_to_plan` 里汇成同一个计划。要防的是 auto 把一句
    # 问候逼成一次**动作**技能调用（拿 effect/navigate 去"执行"问好）——那才是假的。
    bad_chat = [r for r in chat if any(n != "chat" for n in r["names"])]
    verdict.append(("闲聊轮不点动作技能（点 chat 可以，点别的不行）", not bad_chat,
                    f"names={[r['names'] for r in chat]}"))
    multi = [r for r in ok if r["case"] == "multi_intent"]
    multi_call = [r for r in multi if r["n_calls"] > 1]
    verdict.append(("parallel_tool_calls=False 被遵守（多意图只回 1 条）",
                    not multi_call, f"{len(multi_call)}/{len(multi)} 回了多条"))
    lat = [r["ms"] for r in ok]
    slow = [r for r in ok if r["ms"] > settings.planner_native_slow_s * 1000]
    if lat:
        verdict.append((f"延迟在预算内（max < {NATIVE_ARGS['timeout']}s）",
                        max(lat) < NATIVE_ARGS["timeout"] * 1000,
                        f"p50={_pct(lat, .5)}ms max={max(lat)}ms；"
                        f"超 {settings.planner_native_slow_s:.0f}s 的有 {len(slow)} 条"))

    print("\n=== 判定 ===")
    for desc, passed, detail in verdict:
        print(f"  {'✅' if passed else '❌'} {desc}  [{detail}]")

    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(REPORT_DIR, f"native_probe_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# native tool calls 探针 {ts}\n\n")
        f.write(f"- 模型 `{model}` / provider `{settings.llm_provider}` / role `{ROLE}`\n")
        f.write(f"- 技能 {len(tools)} 个，schema {schema_kb:.1f} KB，预算 {NATIVE_ARGS}\n")
        f.write("- 发的是**真 planner 提示词**"
                f"（`_render_planner_prompt`，native 档规则 7）："
                f"{prompt_chars} 字符\n")
        tf = "/".join("开" if t else "关" for t in rounds)
        f.write(f"- 每句 {args.trials} 次，思考档 {tf}\n\n")
        f.write("## 判定\n\n")
        for desc, passed, detail in verdict:
            f.write(f"- {'PASS' if passed else 'FAIL'} — {desc}（{detail}）\n")
        f.write("\n## 逐条原始记录\n\n")
        f.write("| case | thinking | ms | calls | names | parsed | finish | error |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['case']} | {r['thinking']} | {r['ms']} | {r['n_calls']} | "
                    f"{','.join(r['names'])} | {r['args_parsed']} | {r['finish_reason']} | "
                    f"{r['error'][:60]} |\n")
        f.write("\n## 用例动机\n\n")
        for key, prompt, why in CASES:
            f.write(f"- `{key}`：{prompt} —— {why}\n")
    print(f"\n报告已落 {path}")
    if args.json:
        print(json.dumps({"verdict": [{"desc": d, "pass": p, "detail": t}
                                      for d, p, t in verdict], "rows": rows},
                         ensure_ascii=False))
    return 0 if all(p for _, p, _ in verdict) else 1


if __name__ == "__main__":
    sys.exit(main())
