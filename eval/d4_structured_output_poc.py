#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D4 POC：生产模型对 `response_format=json_schema` 与 function calling 的支持度/稳定性/延迟。

**只读实验**：只发 LLM 请求，不碰数据库、不写 trace、不改任何生产状态。需要网络与 API key，
**因此不入 L0 套件、不进夜间**（`tests/run_all.py` 按磁盘枚举 tests/*.py，本文件在 eval/）。

## 它要回答什么（roadmap D4 的"前置未知量"）

roadmap D4 的目标是把 planner 的 7 行文本协议换成 JSON Schema 结构化输出，验收判据是
`args_parse` / pydantic `ValidationError` / `unknown_target` 三族受阻归零。**但生产模型
到底支不支持、支持到什么程度，此前全是推测**——所以 POC 挡在动手之前。

关键在于把三件长得像的事分开，本脚本每个探针都是为"分开它们"设计的：

  ① **端点直接拒绝**（HTTP 400）——最容易看清，也最不可能被误读成"支持"；
  ② **接受了但静默忽略**——最危险的一种。它照样返回一段像模像样的 JSON，看着能用，
     实际约束一条没有（schema 里的 enum/类型全是装饰）。**判据必须是"模型做不到违抗"，
     不是"输出看着对"**：所以有 P1b 那个"提示词故意要求违反 schema 类型"的探针——
     真做了约束解码时它**结构上**给不出字符串，被忽略时它会顺着提示词给出字符串；
  ③ **真约束**——连"提示词要求违约"都违不了。

function calling 侧同理：`tool_choice` 强制指定函数时，支持 ⇒ 必然回 tool_call；
不支持/被忽略 ⇒ 回文本或 400。

## 跑法（cd saudade-blog-agent，需网络）

  .venv/bin/python eval/d4_structured_output_poc.py                 # 默认每档 3 次
  .venv/bin/python eval/d4_structured_output_poc.py --trials 5
  .venv/bin/python eval/d4_structured_output_poc.py --thinking      # 额外跑"思考开"那档
  .venv/bin/python eval/d4_structured_output_poc.py --json          # 机器可读摘要

报告同时落 `eval/report/d4_poc_<ts>.md`（原始证据留档；结论由人手写进
`docs/toolcall-stability-roadmap.md`，本脚本**不改那份文档**）。
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime

from openai import OpenAI

from config.settings import settings

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")

# 与 planner 完全同参（agent/graph.py：temperature=0.2 / max_tokens=400 / thinking 关）——
# 换一个参数测出来的"支持度"就不是 planner 那个场景的支持度了。
PLANNER_ARGS = {"temperature": 0.2, "max_tokens": 400, "timeout": 30}

# 待验证的 plan schema：形状照着 D4 目标（技能 + 参数 + 调用清单），
# 里面**故意**有 enum、有嵌套 object、有 integer——这三样正是文本协议最容易写错的。
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "enum": ["navigate", "effect", "chat", "content_query"]},
        "params": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "count": {"type": "integer"},
                "action": {"type": "string", "enum": ["on", "off"]},
            },
            "required": ["target", "count"],
            "additionalProperties": False,
        },
        "tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skill", "params", "tools"],
    "additionalProperties": False,
}

# 普通提示词：与 schema 一致
PROMPT_OK = (
    "把用户的请求映射成一个 JSON 计划。用户说：「帮我把樱花关掉」。"
    "target 填页面别名，count 填涉及的数量（整数）。"
)
# 对抗提示词：**故意要求违反 schema 的类型**。这是"真约束 vs 静默忽略"的判别探针（见头注②）
PROMPT_ADVERSARIAL = (
    "把用户的请求映射成一个 JSON 计划。用户说：「帮我把樱花关掉」。"
    "注意：count 这个字段请写成字符串形式（例如 \"1\"），不要写数字；"
    "另外 action 请填 \"关闭\"，不要用别的词。"
)

TOOL = {
    "type": "function",
    "function": {
        "name": "navigate_to",
        "description": "跳转到站内页面",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "enum": ["/", "/talk", "/device-console/"]},
                "confirm": {"type": "boolean"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
PROMPT_TOOL = "用户说：「带我去留言板看看」。需要跳转就调用工具。"


def _client() -> OpenAI:
    return OpenAI(api_key=settings.active_llm_api_key, base_url=settings.active_llm_base_url)


def _check_schema(obj) -> tuple[bool, str]:
    """手写校验（不引 jsonschema 依赖，判据与本 schema 一一对应）。返回 (过?, 原因)。"""
    if not isinstance(obj, dict):
        return False, f"顶层不是 object（{type(obj).__name__}）"
    for k in ("skill", "params", "tools"):
        if k not in obj:
            return False, f"缺必填键 {k}"
    extra = set(obj) - {"skill", "params", "tools"}
    if extra:
        return False, f"多出键 {sorted(extra)}（additionalProperties=false 被违反）"
    if obj["skill"] not in ("navigate", "effect", "chat", "content_query"):
        return False, f"skill 不在 enum 内：{obj['skill']!r}"
    p = obj["params"]
    if not isinstance(p, dict):
        return False, "params 不是 object"
    if "target" not in p or "count" not in p:
        return False, "params 缺必填键"
    if not isinstance(p["count"], int) or isinstance(p["count"], bool):
        return False, f"count 不是 integer：{p['count']!r}（{type(p['count']).__name__}）"
    if "action" in p and p["action"] not in ("on", "off"):
        return False, f"action 不在 enum 内：{p['action']!r}"
    if set(p) - {"target", "count", "action"}:
        return False, f"params 多出键 {sorted(set(p) - {'target', 'count', 'action'})}"
    if not isinstance(obj["tools"], list) or not all(isinstance(x, str) for x in obj["tools"]):
        return False, "tools 不是字符串数组"
    return True, ""


def _call(client, messages, *, response_format=None, tools=None, tool_choice=None,
          strict=False, thinking=False, max_tokens=None):
    """发一次请求。返回 (结果 dict, 失败原因 or None)。异常一律收成原因，不抛。"""
    kw = dict(model=settings.active_llm_model,
              messages=messages,
              temperature=PLANNER_ARGS["temperature"],
              max_tokens=max_tokens or PLANNER_ARGS["max_tokens"],
              timeout=PLANNER_ARGS["timeout"],
              extra_body={"enable_thinking": bool(thinking)})
    if response_format:
        kw["response_format"] = response_format
    if tools:
        use = json.loads(json.dumps(tools))  # 深拷贝，别把 strict 写回共享常量
        if strict:
            use[0]["function"]["strict"] = True
        kw["tools"] = use
        kw["tool_choice"] = tool_choice
    t0 = time.monotonic()
    try:
        r = client.chat.completions.create(**kw)
    except Exception as e:  # HTTP 400 / 超时 / 连接失败
        return {"latency": round(time.monotonic() - t0, 2), "http_error": _err_text(e)}, str(e)
    dt = round(time.monotonic() - t0, 2)
    msg = r.choices[0].message
    return {"latency": dt,
            "finish_reason": r.choices[0].finish_reason,
            "content": msg.content or "",
            "tool_calls": [{"name": c.function.name, "arguments": c.function.arguments}
                           for c in (msg.tool_calls or [])],
            "reasoning_chars": len(getattr(msg, "reasoning_content", "") or ""),
            "usage": {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens}}, None


def _err_text(e: Exception) -> str:
    """错误文本截断留档（**可能很长且含请求体，不落 API key**：key 在 header 不在 body）。"""
    return f"{type(e).__name__}: {str(e)[:400]}"


# ── 探针 ──────────────────────────────────────────────────────────────────────
def probe_json_schema(client, prompt: str = PROMPT_OK, *, thinking=False) -> dict:
    return _call(client, [{"role": "user", "content": prompt}],
                 response_format={"type": "json_schema",
                                  "json_schema": {"name": "plan", "strict": True,
                                                  "schema": PLAN_SCHEMA}},
                 thinking=thinking)


def probe_json_object(client) -> dict:
    return _call(client, [{"role": "user", "content": PROMPT_ADVERSARIAL + " 只输出 JSON。"}],
                 response_format={"type": "json_object"})


def probe_tools(client, tool_choice, *, strict=False) -> dict:
    return _call(client, [{"role": "user", "content": PROMPT_TOOL}],
                 tools=[TOOL], tool_choice=tool_choice, strict=strict)


def probe_baseline(client) -> dict:
    """今天的形态：无 response_format、无 tools，纯文本里写 JSON（对照组）。"""
    return _call(client, [{"role": "user", "content": PROMPT_OK + " 只输出 JSON，不要别的字。"}])


def _verdict(name: str, runs: list[dict], judge) -> dict:
    """把同一档的多次运行收成一行结论。judge(结果) → (是否达标, 说明)。

    **"JSON 可解析" 与 "满足 schema" 分开数**：两者混在一列会把"形状不对"
    读成"解析失败"，而它们对 D4 的意义完全不同（前者是本方向要消灭的，后者是模型
    按自己的理解裁剪——今天的协议就是这个症状）。
    """
    ok, parsed, notes, lat, errs, frs, rc = 0, 0, [], [], [], [], []
    for r, fail in runs:
        if r.get("http_error"):
            errs.append(r["http_error"])
            continue
        if r.get("tool_calls"):
            parsed += 1  # 工具档：判据在 arguments 上，"可解析"看 tool_call 在不在
        else:
            body, _why = _parse_json(r.get("content", ""))
            parsed += 1 if body is not None else 0
        good, why = judge(r)
        ok += 1 if good else 0
        if not good:
            notes.append(why)
        if r.get("latency"):
            lat.append(r["latency"])
        if r.get("finish_reason"):
            frs.append(r["finish_reason"])
        rc.append(r.get("reasoning_chars") or 0)
    n = len(runs)
    return {"probe": name, "trials": n, "ok": ok, "json_parsed": parsed,
            "http_errors": len(errs), "error_sample": errs[0] if errs else "",
            "fail_samples": notes[:3], "finish_reasons": sorted(set(frs)),
            # 思考模式下正文之外还有多少 reasoning 字符（结构化输出与思维链能不能共存）
            "reasoning_chars_max": max(rc) if rc else 0,
            "latency_p50": round(statistics.median(lat), 2) if lat else None,
            "latency_max": round(max(lat), 2) if lat else None}


def _parse_json(text: str):
    t = (text or "").strip()
    if t.startswith("```"):  # 围栏（文本协议的老毛病，结构化输出不该有）
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t), ""
    except Exception as e:
        return None, f"JSON 解析失败：{str(e)[:80]}"


def judge_content(r: dict) -> tuple[bool, str]:
    obj, why = _parse_json(r.get("content", ""))
    if obj is None:
        return False, why
    return _check_schema(obj)


def judge_tool_call(r: dict) -> tuple[bool, str]:
    tcs = r.get("tool_calls") or []
    if not tcs:
        return False, f"没回 tool_calls（回了 {len(r.get('content') or '')} 字文本）"
    try:
        args = json.loads(tcs[0]["arguments"])
    except Exception as e:
        return False, f"tool_calls.arguments 不是合法 JSON：{str(e)[:60]}"
    if args.get("path") not in ("/", "/talk", "/device-console/"):
        return False, f"path 不在 enum 内：{args.get('path')!r}"
    return True, ""


def judge_json_parsed(r: dict) -> tuple[bool, str]:
    """弱档（`json_object`）只看"是不是一段 JSON"——它**不承诺**任何形状，因此
    它的价值不在自己那列，而在与 P1b 的对照：同一个对抗提示词下，
    弱档会不会顺着提示词把 integer 写成字符串。"""
    body, why = _parse_json(r.get("content", ""))
    return (body is not None), why


def judge_off_schema(r: dict) -> tuple[bool, str]:
    """决策质量旁证（**不是支持度**）：意图落不进 enum 时，模型怎么降级。
    真约束解码下它**只能**从 enum 里挑一个——问题是挑得像不像话。"""
    body, why = _parse_json(r.get("content", ""))
    if body is None:
        return False, why
    ok, why2 = _check_schema(body)
    return ok, why2


def main() -> int:
    ap = argparse.ArgumentParser(description="D4 POC：结构化输出与 function calling 支持度（只读）")
    ap.add_argument("--trials", type=int, default=3, help="每档重复次数（默认 3）")
    ap.add_argument("--thinking", action="store_true", help="额外跑「思考开」那一档")
    ap.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    ap.add_argument("--out", default="", help="报告落盘路径（默认 eval/report/d4_poc_<ts>.md）")
    ap.add_argument("--no-report", action="store_true", help="不落报告文件")
    args = ap.parse_args()

    n = max(1, args.trials)
    client = _client()
    # 档位表：(名字, 跑一次的函数, 判据, 这一档回答的问题)
    specs = [
        ("P6 对照：纯文本写 JSON（今天的形态）", lambda: probe_baseline(client),
         judge_content, "今天 planner 走的那条路的基线：解析成功率与延迟"),
        ("P1 json_schema + strict（普通提示词）", lambda: probe_json_schema(client),
         judge_content, "端点是否接受该参数；输出是否满足 enum/类型/additionalProperties"),
        ("P1b json_schema + strict（**对抗提示词**：要求违约）",
         lambda: probe_json_schema(client, PROMPT_ADVERSARIAL), judge_content,
         "判别★：真约束解码 ⇒ 违不了；静默忽略 ⇒ 顺着提示词给出字符串 count / 非 enum action"),
        ("P2 json_object（弱档）+ 同一对抗提示词", lambda: probe_json_object(client),
         judge_json_parsed, "对照 P1b：弱档只保证「是一段 JSON」、不保证形状——它会不会违约？"),
        ("P8 json_schema：意图落不进 enum 时怎么降级", lambda: probe_json_schema(
            client, "把用户的请求映射成一个 JSON 计划。用户说：「帮我删掉「大笨狗」这个标签」。"),
         judge_off_schema, "决策质量旁证（不是支持度）：真约束下它只能选 enum 内的值，选得像不像话"),
        ("P3 tools + tool_choice=auto", lambda: probe_tools(client, "auto"),
         judge_tool_call, "模型是否自发调用工具、arguments 是否为合法 JSON"),
        ("P4 tools + 强制指定函数", lambda: probe_tools(
            client, {"type": "function", "function": {"name": "navigate_to"}}),
         judge_tool_call, "判别★：支持 ⇒ 必然回 tool_call；被忽略 ⇒ 文本"),
        ("P5 tools + strict=True（函数侧结构化）", lambda: probe_tools(
            client, {"type": "function", "function": {"name": "navigate_to"}}, strict=True),
         judge_tool_call, "函数参数是否也支持严格模式（OpenAI 的 structured-outputs-on-tools）"),
    ]
    if args.thinking:
        specs.insert(3, ("P7 json_schema + 思考开", lambda: probe_json_schema(client, thinking=True),
                         judge_content, "Qwen 思考模式与结构化输出能否共存（reasoning_content 是否挤掉正文）"))

    rows, raw = [], []
    for name, fn, judge, question in specs:
        runs = []
        for _ in range(n):
            r, _fail = fn()
            runs.append((r, _fail))
        v = _verdict(name, runs, judge)
        v["question"] = question
        v["sample"] = (runs[0][0].get("content") or
                       json.dumps(runs[0][0].get("tool_calls") or [], ensure_ascii=False))[:300]
        rows.append(v)
        raw.append({"probe": name, "runs": [r for r, _ in runs]})
        print(f"  · {name}: {v['ok']}/{v['trials']} 达标"
              f"{'，HTTP 错误 ' + str(v['http_errors']) if v['http_errors'] else ''}"
              f"（p50 {v['latency_p50']}s）")

    head = (f"# D4 POC：生产端点的结构化输出与 function calling\n\n"
            f"- 端点：`{settings.active_llm_base_url}`\n"
            f"- 模型：`{settings.active_llm_model}`（provider={settings._provider_prefix}）\n"
            f"- 参数：与 planner 同参（temperature={PLANNER_ARGS['temperature']}、"
            f"max_tokens={PLANNER_ARGS['max_tokens']}、timeout={PLANNER_ARGS['timeout']}、"
            f"enable_thinking=False{'' if not args.thinking else '（另有一档思考开）'}）\n"
            f"- 每档 {n} 次，时刻 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **只读实验**：无库写入、无 trace 落盘、不改进程内任何状态\n")
    table = ["", "| 档位 | 达标 | JSON 可解析 | HTTP 错误 | p50 延迟 | finish_reason |",
             "|---|---|---|---|---|---|"]
    for v in rows:
        table.append(f"| {v['probe']} | {v['ok']}/{v['trials']} | {v['json_parsed']}/{v['trials']} | "
                     f"{v['http_errors']} | {v['latency_p50']}s | "
                     f"{'/'.join(v['finish_reasons']) or '—'} |")
    qs = ["", "各档要回答的问题："] + [f"- **{v['probe']}**：{v['question']}" for v in rows]
    detail = ["", "## 逐档证据", ""]
    for v in rows:
        detail.append(f"### {v['probe']}")
        detail.append(f"- 达标 {v['ok']}/{v['trials']}；JSON 可解析 {v['json_parsed']}/{v['trials']}；"
                      f"p50 {v['latency_p50']}s / 最大 {v['latency_max']}s；"
                      f"finish_reason={v['finish_reasons']}；"
                      f"reasoning 字符数上限={v['reasoning_chars_max']}")
        if v["error_sample"]:
            detail.append(f"- HTTP 错误样本：`{v['error_sample']}`")
        if v["fail_samples"]:
            detail.append(f"- 未达标样本：{'；'.join(v['fail_samples'])}")
        detail.append(f"- 首次输出节选：`{v['sample']}`")
        detail.append("")
    md = "\n".join([head] + table + qs + detail)

    if args.json:
        print(json.dumps({"model": settings.active_llm_model, "trials": n, "rows": rows},
                         ensure_ascii=False, indent=1))
    else:
        print(md)

    if not args.no_report:
        os.makedirs(REPORT_DIR, exist_ok=True)
        out = args.out or os.path.join(
            REPORT_DIR, f"d4_poc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
