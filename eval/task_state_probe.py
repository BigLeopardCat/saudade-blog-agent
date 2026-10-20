#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话级任务状态（批 D + 台账终态 20261015）的三轮探针：**验的是那 4 条 golden
用例验不到的那一半**。

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
（同族教训见 `eval/toolcall_probe.py` 头注：探针跑绿了，但它验的不是线上那条路）。

## 它做什么

真 LLM、真链路（内部调用 `run_golden.run_one`，同 `server._run_agent_stream_to_queue`）：

  轮 1  真跑一句多步请求，**看模型会不会自己登记**（`__TASK__` 帧）。
        登记了就用手上这份真载荷；没登记就用手工构造的**等价载荷**继续，并在报告里
        **明写是哪一种**——模型不登记本身是要报出去的事实，不能悄悄替它圆上。
  轮 2  把载荷当 `agent_tasks` 喂回去（**与 Rust 读侧交回来的形状一致**：JSON 数组串），
        看三件事：注入进没进上下文、planner 认不认这是"上一轮自己登记的事"、
        做完之后 producer 有没有按**回执**把游标推进。
        外加一条负断言：**前两轮都不该出现 `cancelled` 帧**——撤下只有 `task_drop` 一条
        通道（20260927 拆形状，"做完后顺手再登记一次空 steps"不再等于撤下，
        见 `agent/tasks.py` 的 `TASK_DROP` 头注）。
  轮 3  **台账终态**（20261015）：喂两条过期的行（13h / 49h），看**期限**那条路真的
        发得出帧——13h ⇒ `input_required` + 催办问句，49h ⇒ `cancelled`（终态）。
        这是新增的**第三结局**，前两轮结构上验不到（它们喂的是刚登记的新行）。

## 它不验什么（如实划界）

  · **Rust 那一段不在本探针里**：落库（`save_agent_task`）、读回（`load_agent_tasks` 的终态
    过滤与 72h 时效）只由真机链路覆盖。本探针喂的是"Rust 交回来的形状"，不是 Rust 本身。
  · **轮 3 的 `created_at` 是探针自己按"现在"减出来的**（13h / 49h）：真机上它是 Rust 写的
    落库时刻。所以轮 3 验的是"**期限判据在真链路里发得出帧**"，不是"Rust 写的时间对"。
  · **轮 3 的前提可能被别人动掉**（「前提在别人手里」那一族）：若这一轮 planner 真去办那两行，
    同意闸会当场挡下 ⇒ 走 `via=consent` 分支（**也对**，但不是期限那条路）。故轮 3 先判
    "这一轮没动那两行"这条前提，前提不成立时后面几条按**未评估**读，不许当期限判据通过。
  · **不是门禁**：结果打印给人看，默认退出码 0（同 `llm_judge.py` 的取向）。需要时加 `--strict`。
  · **不入 L0 套件、不进夜间**：要网络与 API key（pytest 只收 `tests/` 下的文件）。

跑法（cd saudade-blog-agent，需网络）：

    .venv/bin/python eval/task_state_probe.py
    .venv/bin/python eval/task_state_probe.py --strict         # 任一条不成立 → 退出码 1

（任务状态 20261015 起**常开**、没有档位，所以本探针也不用再拨任何开关。）

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

from utils import trace_io  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.tasks import (  # noqa: E402
    TASK_STALE_ASK,
    frame_payload,
    normalize_declaration,
    render_open_tasks,
)

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

# ── 轮 3：台账**终态**（20261015）────────────────────────────────────────
# 病：结不掉的账。窗口内 17 行 `running` 全部 `cursor=0`、声明的那一步工具一次都没执行过
# （同意闸挡下 / 换另一件工具办掉了）——**代码里没有任何一处会改 state**，那些行只能等
# 72h 读侧时效从**注入**里抹掉，行本身永远留在表里。这一轮验的是"期限那一半真的发得出帧"。
#
# 行的形状**逐字照 Rust 读侧交回来的**（`load_agent_tasks` 的 `created_at` 就是
# `MM-DD HH:MM`，只有月日没有年）。两条行的年龄刻意跨在两条期限的两侧。
TURN3 = {
    "id": "probe_turn3",
    "user_input": "今天就先这样吧，谢谢你",
    "context": {
        "current_url": "/", "page_title": "首页",
        "current_effects": "none", "current_darkmode": "off",
        "conversation_id": PROBE_CONV,
        # agent_tasks 由 main 按"现在"算好填进来（见 _stale_rows）。
    },
    "gold": {},
}

# 步子的工具挑 `create_announcement`：它的标题/正文**当场创作**、`args_are_bound` 绑不上
# （20261015 实测的另一副面孔）⇒ 这一轮**结构上**不会产生回执 ⇒ 不会把要验的那一行
# 提前结掉、把断言变成偶发红。这是刻意的，不是随手挑的。
STALE_TOOL = "create_announcement"


def _stale_rows(now: "datetime | None" = None) -> list:
    """两条过期的行（13 小时 / 49 小时），`created_at` 按 Rust 那格渲染。"""
    now = now or datetime.now()

    def at(hours: float) -> str:
        return datetime.fromtimestamp(now.timestamp() - hours * 3600).strftime("%m-%d %H:%M")

    def row(tid: str, goal: str, hours: float) -> dict:
        return {"task_id": tid, "goal": goal,
                "steps": [{"label": "发一条公告", "tool": STALE_TOOL}],
                "total_steps": 1, "cursor": 0, "state": "running",
                "pending_question": "", "created_at": at(hours)}

    return [row("at_5ta1e000", "给站上发一条公告", 13),      # ≥ 软期限 12h ⇒ 催办
            row("at_5ta1e001", "把上个月的公告都清掉", 49)]  # ≥ 硬期限 48h ⇒ 终态


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


def _run_round3(run_id: str, check) -> dict:
    """轮 3：喂两条**过期**的行，验期限那两条路真的发得出帧（20261015）。

    前两轮喂的是**刚登记的新行** ⇒ 结构上验不到期限那两条路，所以单开一轮。
    `check` 是 `main` 里那个闭包（同一个出口，不另立第二套记账）。
    """
    TURN3["context"]["agent_tasks"] = json.dumps(_stale_rows(), ensure_ascii=False)
    print(f"\n[轮 3] {TURN3['user_input']}   （喂两条过期的行：13h 软期限 / 49h 硬期限）")
    t0 = time.monotonic()
    r3 = G.run_one(G.build_request(TURN3), G.build_principal(TURN3),
                   trace_ctx={"run": run_id, "case": TURN3["id"]})
    d3 = time.monotonic() - t0
    tr3 = _load_case_trace(run_id, TURN3["id"])
    print(f"  回复：{(r3.get('text') or '')[:70]}…  {d3:.1f}s")
    print(f"  命令帧：{r3.get('commands')}  执行工具：{r3.get('exec_tools')}")

    # **前提**（「前提在别人手里」那一族）：这一轮没动那两行，才谈得上"期限把它们收掉"。
    # 声明的那一步是写工具 ⇒ 万一 planner 真去办，同意闸会当场挡下 ⇒ 那两行走
    # `via=consent` 分支（**也是对的**，但验的就不是期限那条路了）。所以先判前提，
    # 前提不成立时后面的读数按"未评估"看，别当成期限判据通过。
    exp = _events(tr3, "producer", "task_expire")
    via = " ".join(str(e.get("via")) for e in exp)
    touched = [t for t in (r3.get("exec_tools") or []) if t == STALE_TOOL]
    check("前提：这一轮没动那两行（无回执、无同意闸挡下）⇒ 期限判据才有意义",
          not touched and "consent" not in via,
          f"exec={touched} via={via or '（无）'}")

    frames = _task_frames(r3)
    print(f"  过期帧：{json.dumps(frames, ensure_ascii=False)[:300]}")
    by_id = {str(f.get("task_id") or ""): f for f in frames}
    soft = by_id.get("at_5ta1e000") or {}
    hard = by_id.get("at_5ta1e001") or {}
    check("软期限那行（13h）转成「在等你回话」：input_required + 催办问句",
          str(soft.get("state")) == "input_required"
          and str(soft.get("pending_question")) == TASK_STALE_ASK,
          json.dumps(soft, ensure_ascii=False))
    check("硬期限那行（49h）转成终态 cancelled、问句一并清空",
          str(hard.get("state")) == "cancelled"
          and str(hard.get("pending_question")) == "",
          json.dumps(hard, ensure_ascii=False))
    check("过期帧与结算帧**同形**（7 键，无 idempotency_key、无 via）",
          bool(frames) and all(
              set(f) == {"task_id", "goal", "steps", "total_steps", "cursor",
                         "state", "pending_question"} for f in frames),
          str([sorted(f) for f in frames]))
    check("trace 记下了这次过期（producer.task_expire，via 里有 stale）",
          bool(exp) and "stale" in via, str(exp)[:200])
    undec = _events(tr3, "producer", "task_expire_undecidable")
    check("没有认不出时间戳的行（不该出现 task_expire_undecidable）",
          not undec, str(undec) or "无")
    return {"text": r3.get("text") or "", "d": d3}


def _write_report(run_id: str, *, checks: list, payload: dict, declared_by_model: bool,
                  durations: tuple, texts: tuple) -> str:
    """落报告，返回路径。**纯排版**：判据的结果全在 `checks` 里，这里不重算。"""
    os.makedirs("eval/report", exist_ok=True)
    path = f"eval/report/task_state_probe_{run_id.split('_', 1)[1]}.md"
    d1, d2, d3 = durations
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 会话级任务状态三轮探针 {run_id}\n\n")
        f.write("- 档位：任务状态**常开**（20261015 撤掉 `AGENT_TASK_STATE`，无档位可拨）\n")
        f.write("- 轮 1 登记来源：**"
                f"{'模型自己登记' if declared_by_model else '手工等价载荷（模型本轮没登记）'}**\n")
        f.write(f"- 轮 1 `{d1:.1f}s` / 轮 2 `{d2:.1f}s` / 轮 3 `{d3:.1f}s`\n")
        f.write(f"- 轮 1/2 载荷：`{json.dumps(payload, ensure_ascii=False)}`\n")
        f.write(f"- 轮 3 载荷（两条过期的行）：`{TURN3['context']['agent_tasks']}`\n\n")
        f.write("| 判据 | 结果 | 现场 |\n|---|---|---|\n")
        for c in checks:
            f.write(f"| {c['desc']} | {'✅' if c['ok'] else '❌'} | `{c['detail'][:160]}` |\n")
        for i, t in enumerate(texts, 1):
            f.write(f"\n轮 {i} 回复：\n\n> {t.strip()}\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="任一条不成立 → 退出码 1")
    args = ap.parse_args()

    # 编译图（生产侧由 `/chat/stream` 启动时建；进程内跑法必须自己建一次——
    # 不建的话 `server._agent` 是 None，producer 在 `_agent.stream` 上抛
    # AttributeError、**不报错也不落 trace**，只表现为"整轮什么都不发生"）。
    G.ensure_agent()

    # 20261015：档位撤掉、任务状态常开 ⇒ 不再印档位（`config.settings` 也因而不再被
    # 本文件 import——一格恒定真值的"档位"是噪声，同 20261004 删 `PLANNER_ENGINE`）。
    print("[档位] 任务状态**常开**（20261015 撤掉 `AGENT_TASK_STATE`）")
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
        print(f"    round {e.get('round')} workflow={e.get('workflow')} tools={e.get('tools')} "
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
    check("前两轮都没有 cancelled 帧（撤下只有 task_drop 一条通道）",
          not _canc, str(_canc) or "无")

    # ── 轮 3：台账终态（20261015）────────────────────────────────────────
    r3 = _run_round3(run_id, check)

    # ── 报告 ────────────────────────────────────────────────────────────
    path = _write_report(run_id, checks=checks, payload=payload,
                         declared_by_model=declared_by_model,
                         durations=(d1, d2, r3["d"]),
                         texts=(r1.get("text") or "", r2.get("text") or "",
                                r3["text"]))
    bad = [c for c in checks if not c["ok"]]
    print(f"\n报告：{path}")
    print(f"trace：logs/agent/golden_traces/{run_id}/")
    print(("全部成立 ✅" if not bad else f"不成立 {len(bad)} 条 ❌"))
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
