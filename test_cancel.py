# -*- coding: utf-8 -*-
"""协作式取消（stop_event）的**能力边界**回归测试（20260916）。

背景：断连取消是"协作式"的——stop_event 置位后由**图内各节点**在检查点主动退出，
不是抢占式中断。能力边界与承诺写在这里，测试把它们钉住：

  ① 每个节点入口都检查（planner/execute/reflector/model/gate）——用户离开后不再推进；
  ② **写操作绝不发生在用户离开之后**：execute 的工具调用前必过检查；
  ③ 多写操作清单**中途**取消：剩下的 spec 不执行（20260916 补的逐 spec 检查——
     此前只在节点入口检查一次，[导航, 屏显] 这种清单在中途断连时会把屏显也写掉）；
  ④ 能力边界（**已知限制**）：`model` 等节点的 LLM 调用**本身不可打断**，取消要等这次
     调用自然结束才在**下一个检查点**生效——但"下一个检查点"一定是拦得住的，
     所以"用户走了还写设备"这件事不会发生。

纯函数 / 无网络 / 不起图（直接调节点函数，与 test_skills.py 同款）。
"""
import sys
import threading

from langchain_core.messages import HumanMessage

import agent.graph as g
from agent.graph import AgentCancelled, execute_node, gate_node, model_node, planner_node, reflector_node
from agent.skills import instantiate_plan
from agent.graph import plan_encode

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def _cfg(stop: bool = False):
    ev = threading.Event()
    if stop:
        ev.set()
    return {"configurable": {"stop_event": ev}}, ev


class _Spy:
    """假工具：记录调用，按需在"调用期间"置位 stop_event（模拟用户就在这一刻断开）。"""

    def __init__(self, name, result="ok", on_call=None):
        self.name = name
        self.calls: list = []
        self._result = result
        self._on_call = on_call

    def invoke(self, args):
        self.calls.append(args)
        if self._on_call is not None:
            self._on_call()
        return self._result


def _state(**kw):
    base = {"messages": [HumanMessage(content="现在有哪些设备在线")],
            "plan": "", "plan_rounds": 1, "done": False}
    base.update(kw)
    return base


# ────────────────────────── ① 节点入口检查

def test_node_entry_checks():
    print("[cancel] 各节点入口检查")
    cfg, _ = _cfg(stop=True)
    for fn, name in ((planner_node, "planner"), (execute_node, "execute"),
                     (reflector_node, "reflector"), (model_node, "model"),
                     (gate_node, "gate")):
        try:
            fn(_state(), cfg)
            check(f"{name} 节点在取消后拒绝推进", False, "没有抛 AgentCancelled")
        except AgentCancelled:
            check(f"{name} 节点在取消后拒绝推进", True)
        except Exception as e:      # 别的异常说明状态夹具不对，同样算失败
            check(f"{name} 节点在取消后拒绝推进", False, f"{type(e).__name__}: {e}")


# ────────────────────────── ②③ 写操作绝不发生在用户离开之后

def test_write_never_runs_after_cancel():
    print("[cancel] 写操作与取消的关系")
    # ② 入口取消：一个写操作都不该被调用
    oled = _Spy("device_oled_display", "已送达")
    orig_oled = g._TOOL_MAP.get("device_oled_display")
    orig_reader = g._TOOL_MAP.get("list_devices")
    g._TOOL_MAP["device_oled_display"] = oled
    try:
        obj = instantiate_plan("content_query", {"calls": [{"tool": "list_devices"}]})
        obj["tools"] = ['device_oled_display({"text": "晚上好"})']
        cfg, _ = _cfg(stop=True)
        try:
            execute_node(_state(plan=plan_encode(obj)), cfg)
            check("入口取消 → execute 抛 AgentCancelled", False, "没抛")
        except AgentCancelled:
            check("入口取消 → execute 抛 AgentCancelled", True)
        check("入口取消 → 写工具零调用", oled.calls == [], oled.calls)

        # ③ 中途取消：清单 [读, 写]，读的时候用户断开 → 写不许执行
        def _disconnect_midway():
            ev.set()

        cfg2, ev = _cfg(stop=False)
        reader = _Spy("list_devices", "在线设备: 无", on_call=_disconnect_midway)
        g._TOOL_MAP["list_devices"] = reader
        oled.calls.clear()
        obj2 = instantiate_plan("content_query", {"calls": [{"tool": "list_devices"}]})
        obj2["tools"] = ['list_devices({})', 'device_oled_display({"text": "晚上好"})']
        out = execute_node(_state(plan=plan_encode(obj2)), cfg2)
        check("中途取消 → 已执行的那个照常调用一次", reader.calls == [{}], reader.calls)
        check("中途取消 → 其后的写操作**不执行**（逐 spec 检查）", oled.calls == [], oled.calls)
        check("中途取消 → 已执行项的回执仍保留（真发生过的事实）",
              len(out.get("receipts") or []) == 1, out.get("receipts"))
        check("中途取消 → 只产出已执行的那一帧", len(out.get("messages") or []) == 1,
              [m.name for m in (out.get("messages") or [])])
    finally:
        g._TOOL_MAP["device_oled_display"] = orig_oled
        g._TOOL_MAP["list_devices"] = orig_reader


# ────────────────────────── ④ 能力边界：LLM 调用不可打断，但下一个检查点一定拦得住

def test_cancel_during_blocking_llm():
    print("[cancel] 取消发生在阻塞的 LLM 调用期间（能力边界）")
    cfg, ev = _cfg(stop=False)

    class _LLMCancelMidway:
        """invoke 期间置位 stop_event —— 模拟用户在 LLM 生成中离开。
        这次调用**照常返回**（LLM 调用本身不可打断），节点自己也照常产出计划。

        点名的工具必须是白名单内的：白名单外的点名会被剔空，而 20260921 起剔空会
        触发确定性纠偏/收尾（见 test_skills.test_drop_correction）——那会把计划
        改写掉，这条用例就测不到"取消不打断 LLM 调用"这件事了。"""

        def invoke(self, *a, **kw):
            ev.set()
            return type("R", (), {
                "content": 'SKILL=content_query\nPARAMS={"calls": [{"tool": "search_notes",'
                           ' "args": {"keyword": "取消"}}]}'})()

    orig_llm = g.get_llm
    g.get_llm = lambda **kw: _LLMCancelMidway()
    try:
        out = planner_node(_state(), cfg)
        check("LLM 期间的取消不打断这次调用（planner 照常返回计划）",
              "search_notes" in (out.get("plan") or ""), str(out.get("plan"))[:80])
    finally:
        g.get_llm = orig_llm

    # 下一个检查点 = execute 入口：拦住，且一个工具都不调
    oled = _Spy("device_oled_display", "已送达")
    orig_map = g._TOOL_MAP.get("device_oled_display")
    g._TOOL_MAP["device_oled_display"] = oled
    try:
        obj = instantiate_plan("content_query", {"calls": [{"tool": "list_devices"}]})
        obj["tools"] = ['device_oled_display({"text": "晚上好"})']
        try:
            execute_node(_state(plan=plan_encode(obj)), cfg)
            check("下一个检查点（execute 入口）拦下取消", False, "没抛")
        except AgentCancelled:
            check("下一个检查点（execute 入口）拦下取消", True)
        check("LLM 期间取消 → 写操作依然零调用", oled.calls == [], oled.calls)
    finally:
        g._TOOL_MAP["device_oled_display"] = orig_map


def main():
    for fn in (test_node_entry_checks, test_write_never_runs_after_cancel,
               test_cancel_during_blocking_llm):
        print(f"\n── {fn.__name__} ──")
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
