#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话级任务状态（批 D）的两轮探针：**验的是那 4 条 golden 用例验不到的那一半**。

## 为什么需要它（先看这条实测，否则会以为 golden 已经验过了）

20260927 用 `--only multi_step_*` 把批 D 的"验收集"跑了 A/B 两遍：

    AGENT_TASK_STATE 关 → 4/4 通过      AGENT_TASK_STATE 开 → 4/4 通过

**两档逐条同分**，且开档那遍的 trace 里 `planner.task_declare` **一条都没有**、
`producer.task_inject` 恒 `n=0`。也就是说：那 4 条用例既没碰过登记、也没碰过注入与结算——
它们**结构上测不到本表**，原因有两条：

  ① 它们**单轮**（`user_input` 一次，没有第二回合），而本表治的是"这一轮做不完、下一轮接着做"；
  ② 它们**不带 `conversation_id`** ⇒ `frame_payload` 算不出会话维度、登记直接走 `task_declare_noconv`
     不落库，下一轮也就无从读回。

它们今天绿是因为**另一件事**：轮内多步（同一次对话里 planner⇄execute 跑两轮）早已由
native 引擎 + 目标来源态判据那批修好（轮 0 navigate → 轮 1 effect → 轮 2 chat 收尾），
与批 D 无关。**"验收用例在跑"不等于"被测的那条路被走到了"**——这就是本文件存在的理由
（同族教训见 `eval/native_tools_probe.py` 头注：探针跑绿了，但它验的不是线上那条路）。

## 它做什么

真 LLM、真链路（内部调用 `run_golden.run_one`，同 `server._run_agent_stream_to_queue`）：

  轮 1  真跑一句多步请求，**看模型会不会自己登记**（`__TASK__` 帧）。
        登记了就用手上这份真载荷；没登记就用手工构造的**等价载荷**继续，并在报告里
        **明写是哪一种**——模型不登记本身是要报出去的事实，不能悄悄替它圆上。
  轮 2  把载荷当 `agent_tasks` 喂回去（**与 Rust 读侧交回来的形状一致**：JSON 数组串），
        看三件事：注入进没进上下文、planner 认不认这是"上一轮自己登记的事"、
        做完之后 producer 有没有按**回执**把游标推进。
        外加一条负断言：**两轮都不该出现 `cancelled` 帧**——撤下只有 `task_drop` 一条
        通道（20260927 拆形状，"做完后顺手再登记一次空 steps"不再等于撤下，
        见 `agent/tasks.py` 的 `TASK_DROP` 头注）。

## 它不验什么（如实划界）

  · **Rust 那一段不在本探针里**：落库（`save_agent_task`）、读回（`load_agent_tasks` 的终态
    过滤与 72h 时效）只由真机链路覆盖。本探针喂的是"Rust 交回来的形状"，不是 Rust 本身。
  · **不是门禁**：结果打印给人看，默认退出码 0（同 `llm_judge.py` 的取向）。需要时加 `--strict`。
  · **不入 L0 套件、不进夜间**：要网络与 API key（`tests/run_all.py` 只收 `tests/*.py`）。

跑法（cd saudade-blog-agent，需网络）：

    .venv/bin/python eval/task_state_probe.py                  # 按本机档位（看 AGENT_TASK_STATE）
    AGENT_TASK_STATE=1 .venv/bin/python eval/task_state_probe.py
    .venv/bin/python eval/task_state_probe.py --strict         # 任一条不成立 → 退出码 1

报告落 `eval/report/task_state_probe_<ts>.md`。**本脚本不改任何文档**，结论由人手写进
`docs/native-toolcalls-mainline.md` 或 ADR。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "eval")

import run_golden as G  # noqa: E402
import trace_io  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.tasks import frame_payload, normalize_declaration, render_open_tasks  # noqa: E402
from config.settings import settings  # noqa: E402

# 会话 id：**探针自造的合成值**（不指向任何真实会话，也不落库——本探针走内部链路，
# Rust 那一段不在场）。用一个一眼看得出是探针的号码。
PROBE_CONV = 900001

# 轮 1 的请求：交接文档 §1.2 那条现场故障的原句形状（多步 + 第二步可做可不做）。
TURN1 = {
    "id": "probe_turn1",
    "user_input": "带我过去，然后帮我把樱花打开",
    "context": {
        "current_url": "/", "page_title": "首页",
        "current_effects": "none", "current_darkmode": "off",
        "conversation_id": PROBE_CONV,
        "history": [
            {"role": "user", "content": "物联网平台的控制台从哪儿进呀？"},
            {"role": "assistant",
             "content": "从 https://saudade.site/device-console/ 进喵～首页右上角那个"
                        "「物联网平台」入口就是它 ✨"},
        ],
    },
    "gold": {},
}

# 轮 2 的请求：主人说"继续"。**剩余的那一步只有一件**（开启樱花），导航已经做过了。
TURN2 = {
    "id": "probe_turn2",
    "user_input": "继续吧",
    "context": {
        "current_url": "/device-console/", "page_title": "物联网平台",
        "current_effects": "none", "current_darkmode": "off",
        "conversation_id": PROBE_CONV,
        # agent_tasks 由调用方按轮 1 的结果填（见 main）。
    },
    "gold": {},
}

# 模型这一轮没登记时用的**手工等价载荷**：目标取主人原话里的那件事，剩下的一步是
# 开启樱花（工具名取自闭集 `step_tool_enum`，`toggle_effect` 是技能模板里的动作工具）。
FALLBACK_DECL = {
    "goal": "带我过去后把樱花打开",
    "steps": [{"label": "开启樱花特效", "tool": "toggle_effect"}],
    "pending_question": "",
}


def _task_frames(res: dict) -> list:
    """这一轮发出的 `__TASK__` 载荷。

    **解析只有一处**（20260927）：`run_golden.run_one` 现在自己就把 `__TASK__` 帧解包成
    `result["task_frames"]`（同一个帧、两个读者，正是本文件头注警告的"两份拷贝各自漂移"
    的形状）——探针不再自己再解一遍。
    """
    return [f for f in (res.get("task_frames") or []) if isinstance(f, dict)]


def _load_case_trace(run_id: str, case_id: str) -> dict | None:
    """这一轮的 trace。**根只能走 `golden_trace.trace_root()`**（写仓内相对路径会让
    这里静默读到 None——同族坑见 `docs/native-toolcalls-mainline.md`）。"""
    path = os.path.join(G.golden_trace.trace_root(), run_id, f"{case_id}.json")
    return trace_io.load_trace(path)


def _decisions(trace: dict) -> list:
    if not trace:
        return []
    return [e for e in (trace.get("events") or [])
            if e.get("node") == "planner" and e.get("event") == "decision"]


def _events(trace: dict, node: str, event: str) -> list:
    if not trace:
        return []
    return [e for e in (trace.get("events") or [])
            if e.get("node") == node and e.get("event") == event]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="任一条不成立 → 退出码 1")
    args = ap.parse_args()

    # 编译图（生产侧由 `/chat/stream` 启动时建；进程内跑法必须自己建一次——
    # 不建的话 `server._agent` 是 None，producer 在 `_agent.stream` 上抛
    # AttributeError、**不报错也不落 trace**，只表现为"整轮什么都不发生"）。
    G.ensure_agent()

    flag = bool(getattr(settings, "agent_task_state", False))
    # 接口层不打了：20261004 起只有 native 一条（`PLANNER_ENGINE` 拨盘已删），
    # 每跑一次都印同一个常量只是噪声。
    print(f"[档位] AGENT_TASK_STATE={'1（开）' if flag else '0（关）'}")
    print(f"[会话] 探针合成会话 id={PROBE_CONV}（走内部链路，不落库）")

    run_id = f"taskprobe_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    checks: list = []

    def check(desc: str, ok: bool, detail: str = "") -> None:
        print(("  ✅ " if ok else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
        checks.append({"desc": desc, "ok": bool(ok), "detail": str(detail)})

    # ── 轮 1：真跑，看模型自己登不登记 ────────────────────────────────────
    print("\n[轮 1] " + TURN1["user_input"])
    t0 = time.monotonic()
    r1 = G.run_one(G.build_request(TURN1), G.build_principal(TURN1),
                   trace_ctx={"run": run_id, "case": TURN1["id"]})
    d1 = time.monotonic() - t0
    frames1 = _task_frames(r1)
    declared_by_model = bool(frames1)
    print(f"  回复：{(r1.get('text') or '')[:70]}…  {d1:.1f}s")
    print(f"  命令帧：{r1.get('commands')}  执行工具：{r1.get('exec_tools')}")
    if declared_by_model:
        print(f"  模型登记：{json.dumps(frames1, ensure_ascii=False)[:200]}")
        payload = frames1[0]
    else:
        print("  模型**没有**登记（这一轮它自己把两步都做了或直接收尾）→ 用手工等价载荷继续")
        payload = frame_payload(normalize_declaration(FALLBACK_DECL), PROBE_CONV)

    # 登记载荷的字段必须与表列一一对应（帧是跨语言契约，缺一列 Rust 就静默少写一格）。
    check("登记载荷字段齐（8 键，且不带身份两列）",
          set(payload) == {"task_id", "goal", "steps", "total_steps", "cursor",
                           "state", "pending_question", "idempotency_key"},
          str(sorted(payload)))
    check("轮 1 没有写工具被执行（这一步不该动数据）",
          not [t for t in (r1.get("exec_tools") or [])
               if t in ("create_tag", "update_tag", "delete_tag", "set_article_tags")],
          str(r1.get("exec_tools")))

    # ── 轮 2：把载荷喂回去 ───────────────────────────────────────────────
    raw = json.dumps([payload], ensure_ascii=False)
    TURN2["context"]["agent_tasks"] = raw
    print(f"\n[轮 2] {TURN2['user_input']}   （注入 {len(raw)} 字节）")
    print("  注入块预览：")
    for line in render_open_tasks(raw).splitlines():
        print("    " + line[:100])
    t0 = time.monotonic()
    r2 = G.run_one(G.build_request(TURN2), G.build_principal(TURN2),
                   trace_ctx={"run": run_id, "case": TURN2["id"]})
    d2 = time.monotonic() - t0
    tr2 = _load_case_trace(run_id, TURN2["id"])
    print(f"  回复：{(r2.get('text') or '')[:70]}…  {d2:.1f}s")
    print(f"  命令帧：{r2.get('commands')}  执行工具：{r2.get('exec_tools')}")

    inj = _events(tr2, "producer", "task_inject")
    check("轮 2 注入了未完结任务（producer.task_inject n≥1）",
          bool(inj) and int(inj[-1].get("n") or 0) >= 1, str(inj))
    dec = _decisions(tr2)
    print("  决策：")
    for e in dec:
        print(f"    round {e.get('round')} skill={e.get('skill')} tools={e.get('tools')} "
              f"status={e.get('status')}")
    picked = any("toggle_effect" in " ".join(e.get("tools") or []) for e in dec)
    check("planner 认下这件事并把剩余那一步规划出来了（tools 里有 toggle_effect）",
          picked, str([e.get("tools") for e in dec]))
    settle = [f for f in _task_frames(r2) if str(f.get("state")) in ("succeeded", "running")]
    print(f"  结算帧：{json.dumps(settle, ensure_ascii=False)[:200]}")
    check("producer 按回执推进了游标（游标 >0，全推进完则 succeeded）",
          bool(settle) and int(settle[-1].get("cursor") or 0) > 0, str(settle))
    check("结算帧认的是**回执里的工具**，不是模型的话",
          any("toggle_effect" in (r2.get("exec_tools") or []) for _ in [0]),
          str(r2.get("exec_tools")))

    # 撤下通道（20260927）：`cancelled` 只该由 `task_drop` 产生（主人明说"不做了"），
    # 不该由"模型把这一步做完了、顺手又登记一次"产生。两轮都不该出现 cancelled——
    # 轮 1 是"做了一步、剩下一步"，轮 2 是"把那一步做完"。
    _canc = [str(f.get("state")) for f in (frames1 + _task_frames(r2))
             if str(f.get("state")) == "cancelled"]
    check("两轮都没有 cancelled 帧（撤下只有 task_drop 一条通道）",
          not _canc, str(_canc) or "无")

    # ── 报告 ────────────────────────────────────────────────────────────
    os.makedirs("eval/report", exist_ok=True)
    path = f"eval/report/task_state_probe_{run_id.split('_', 1)[1]}.md"
    bad = [c for c in checks if not c["ok"]]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 会话级任务状态两轮探针 {run_id}\n\n")
        f.write(f"- 档位：`AGENT_TASK_STATE={'1' if flag else '0'}`\n")
        f.write(f"- 轮 1 登记来源：**{'模型自己登记' if declared_by_model else '手工等价载荷（模型本轮没登记）'}**\n")
        f.write(f"- 轮 1 `{d1:.1f}s` / 轮 2 `{d2:.1f}s`\n")
        f.write(f"- 载荷：`{json.dumps(payload, ensure_ascii=False)}`\n\n")
        f.write(f"| 判据 | 结果 | 现场 |\n|---|---|---|\n")
        for c in checks:
            f.write(f"| {c['desc']} | {'✅' if c['ok'] else '❌'} | `{c['detail'][:160]}` |\n")
        f.write(f"\n轮 1 回复：\n\n> {(r1.get('text') or '').strip()}\n\n")
        f.write(f"轮 2 回复：\n\n> {(r2.get('text') or '').strip()}\n")
    print(f"\n报告：{path}")
    print(f"trace：logs/agent/golden_traces/{run_id}/")
    print(("全部成立 ✅" if not bad else f"不成立 {len(bad)} 条 ❌"))
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
