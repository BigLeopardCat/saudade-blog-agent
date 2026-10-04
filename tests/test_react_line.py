# -*- coding: utf-8 -*-
"""ReAct 试验线（`agent/react_line.py`）的接线单测：离线、秒级、零网络零 LLM。

**为什么必须单独一套**：这条线的两件资产（收敛判据、工具级回执）都是"跑起来才知道
接没接上"的东西。判据写在 `before_model` 里，而 `before_model` 会不会被 `create_agent`
调用、`jump_to="end"` 会不会真的终止、包装层的回执有没有进台账——这些都不是纯函数
能验的。**"能力有测试 ≠ 接线有测试"**（本仓的既定纪律）。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · **无进展收敛**：同一个 `(工具, 参数)` 签名执行过一次之后，第二轮**不再执行**它，
    跳 `end` 产收尾，**不抛 `GraphRecursionError`**（自由 ReAct 的默认形态是抛）；
  · **轮次预算**：模型轮数用满即收尾，且**撞预算的那一轮照常执行**（与生产
    `MAX_PLAN_ROUNDS` 同序——本文件的用例会断言最后一条 ToolMessage 在场）；
  · **回执台账**：checker PASS 进 `ledger.receipts`、BLOCK 进 `ledger.blocked` 带原因码，
    且工具返回文本末尾**附了一行回执**（模型看得见，这是治"同一步反复重试"的燃料）；
  · **自然收尾不受打扰**：模型自己给出无 tool_calls 的回复 ⇒ 零收尾注入、`stop_reason`
    为空（判据不许把"正常答完"也当成"卡住"）。

⚠️ 用例里的模型是**脚本化的假模型**（`_Scripted`）：它不看提示词、不看工具菜单，只按
脚本吐 `AIMessage`。所以本文件证明的是**接线**，不是"模型会不会用工具"——后者只有
真链路的 A/B（`eval/experiments/react_line_ab.py`）能回答，别拿这份绿灯去替它背书。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

from agent.react_line import (  # noqa: E402
    ConvergenceMiddleware,
    RunLedger,
    SkillExecutor,
    build_agent,
    spec_signature,
    wrap_tools_with_receipts,
)

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_UID_SCHEMA = {"type": "object", "properties": {"uid": {"type": "integer"}},
               "required": ["uid"]}


def _stub(name: str, body) -> StructuredTool:
    return StructuredTool(name=name, description=f"{name} 的离线桩",
                          args_schema=_UID_SCHEMA, func=body)


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型。忽略绑定与提示词——**验的是接线不是智能**。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        # 假模型是 pydantic 模型（`GenericFakeChatModel`）⇒ 不许挂任意属性；
        # 它本来就忽略菜单（只按脚本吐消息），记不记这笔都不影响判据。
        return self


def _call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call_{name}_{len(args)}", "type": "tool_call"}])


def _run(script: list, tools: list, **kw) -> tuple[dict, RunLedger]:
    model = _Scripted(messages=iter(script), ai_message_chunk=iter([]))
    agent, ledger = build_agent(model, tools, system_prompt="你是测试用助手。", **kw)
    state = agent.invoke({"messages": [HumanMessage(content="动手吧")]}, {"recursion_limit": 25})
    return state, ledger


def main() -> int:  # noqa: C901
    ok_tool = _stub("freeze_account", lambda uid: f"已冻结账号 {uid}")

    # ── ① 一次调用 + 自然收尾：不该有任何收尾注入 ─────────────────────────
    print("\n① 正常一轮：调用 → 答完")
    state, led = _run([_call("freeze_account", uid=12), AIMessage(content="办好了")], [ok_tool])
    check("工具真的被执行（台账 1 条 PASS 回执）", len(led.receipts) == 1,
          f"receipts={led.receipts}")
    check("工具结果进了消息流（ToolMessage 在场）",
          any(isinstance(m, ToolMessage) for m in state["messages"]))
    check("自然收尾：零收尾注入、stop_reason 为空", led.stop_reason == "", led.stop_reason)
    check("最后一条是模型自己的回复", str(state["messages"][-1].content) == "办好了")

    # ── ② 同工具同参反复点：停在第二次之前，不执行重复 ────────────────────
    print("\n② 无进展：同一签名连点 20 次")
    same = [_call("freeze_account", uid=12) for _ in range(20)]
    state, led = _run(same, [ok_tool])
    check("**没有**抛 GraphRecursionError（自由 ReAct 的默认形态是抛）", True)
    check("停止原因 = no_progress", led.stop_reason == "no_progress", led.stop_reason)
    check("重复的那次**没有执行**（台账只 1 条）", len(led.receipts) == 1,
          f"executed={dict(led.executed)}")
    check("模型轮数远小于脚本长度（第二轮就被判停）",
          sum(1 for m in state["messages"] if isinstance(m, AIMessage)) <= 3)
    check("收尾正文是中文且说清为什么停",
          ("停在这里了" in led.wrap_up and "没进展" in led.wrap_up), led.wrap_up[:60])

    # ── ③ 轮次预算：每一步都是新调用 ⇒ 用满预算才停，且末轮照样执行 ────────
    print("\n③ 轮次预算：每轮换新参数（不会判无进展）")
    many = [_call("freeze_account", uid=i) for i in range(1, 20)]
    state, led = _run(many, [ok_tool], budget=3)
    check("停止原因 = budget", led.stop_reason == "budget", led.stop_reason)
    check("执行次数 == 预算（撞预算那一轮照常执行，与生产 MAX_PLAN_ROUNDS 同序）",
          len(led.receipts) == 3, f"receipts={len(led.receipts)}")
    check("末轮的工具结果在场（不是\"点了名却没跑\"）",
          any(isinstance(m, ToolMessage) for m in state["messages"]))
    check("收尾正文带台账：说清真办成了几件",
          "确实办成 3 件" in led.wrap_up and "freeze_account" in led.wrap_up, led.wrap_up[:80])

    # ── ④ 回执分族：错误帧 ⇒ BLOCK、带原因码、不进 receipts ────────────────
    print("\n④ 工具报错：回执判 BLOCK")
    # 帧的形态照抄生产（`adminops.unknown_target_frame`）：`__ERROR__: …[原因码]（…）`，
    # 原因码由 `_check_spec` 的 `target_error_reason` 那一链取回——自己编一个
    # "看着像"的帧，验的是我编的帧不是真判据。
    def _missing(uid):  # noqa: ANN001, ANN202
        return f"__ERROR__: 目标账号不存在[unknown_target]（{uid} 不在我能确认的范围内）"
    bad_tool = _stub("freeze_account", _missing)
    state, led = _run([_call("freeze_account", uid=999), AIMessage(content="没办成")],
                      [bad_tool])
    check("BLOCK 不进 receipts", not led.receipts, f"receipts={led.receipts}")
    check("BLOCK 带原因码 unknown_target", led.block_reasons() == ["unknown_target"],
          str(led.block_reasons()))
    tool_msg = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    check("回执行附在工具结果末尾（模型看得见）", "内部回执" in str(tool_msg.content)
          and "未生效" in str(tool_msg.content), str(tool_msg.content)[:80])

    # ── ⑤ 工具抛异常 ⇒ 变成错误帧 + BLOCK，不吞掉整轮 ─────────────────────
    print("\n⑤ 工具抛异常：转成错误帧")
    def _boom(uid):  # noqa: ANN001, ANN202
        raise RuntimeError("后端炸了")
    state, led = _run([_call("freeze_account", uid=1), AIMessage(content="知道了")],
                      [_stub("freeze_account", _boom)])
    check("异常没把整轮掀掉（还有最终回复）",
          str(state["messages"][-1].content) == "知道了")
    check("异常变成 BLOCK 台账", len(led.blocked) == 1 and not led.receipts,
          f"blocked={led.block_reasons()}")

    # ── ⑥ 纯函数：签名归一 ───────────────────────────────────────────────
    print("\n⑥ 签名归一")
    check("键序无关", spec_signature("t", {"a": 1, "b": "2"})
          == spec_signature("t", {"b": 2, "a": "1"}))
    check("工具名不同 ⇒ 签名不同", spec_signature("t1", {}) != spec_signature("t2", {}))

    # ── ⑦ 包装层不动菜单：description / args_schema 逐字保留 ───────────────
    print("\n⑦ 包装层不改工具形状")
    led = RunLedger()
    wrapped = wrap_tools_with_receipts([ok_tool], led)[0]
    check("name/description/schema 逐字一致",
          (wrapped.name, wrapped.description) == (ok_tool.name, ok_tool.description)
          and wrapped.args_schema == ok_tool.args_schema)

    # ── ⑧ 收尾判据不许误伤：已有帧之后模型正常答完 ⇒ 不注入收尾 ────────────
    print("\n⑧ 反锁：正常收尾不受打扰")
    state, led = _run([_call("freeze_account", uid=3), AIMessage(content="好了"),
                       AIMessage(content="还有别的事吗")], [ok_tool])
    check("两轮无工具回复都没被当成卡住", led.stop_reason == "" and not led.wrap_up,
          f"stop={led.stop_reason}")

    # ── ⑨ 中间件的预算与重复阈值可配（别写死） ─────────────────────────────
    print("\n⑨ 阈值可配")
    led = RunLedger()
    mw = ConvergenceMiddleware(led, budget=9, repeat_limit=3)
    check("阈值原样存下", (mw.budget, mw.repeat_limit) == (9, 3))

    # ── ⑩ 技能级回执：一次技能调用 = 实例化计划 + 逐工具验收 ────────────────
    print("\n⑩ 技能级回执（SkillExecutor）")
    led = RunLedger()
    ex = SkillExecutor({"get_article_detail": lambda article_id: f"文章 {article_id} 的正文"},
                       led, role="admin")
    text, n_tools = ex.run("read_article", {"article_id": 23})
    check("技能被展开成它计划里的工具（不是拿技能名去验收）",
          led.receipts and led.receipts[0]["tool"] == "get_article_detail",
          str([r["tool"] for r in led.receipts]))
    check("参数从技能参数实例化进工具（article_id=23）",
          led.receipts[0]["args"] == {"article_id": "23"}, str(led.receipts[0]["args"]))
    check("正文里带上了工具输出与回执行", "文章 23 的正文" in text and "内部回执" in text)
    check("回报执行了几个工具（无进展判据的输入）", n_tools == 1, str(n_tools))

    print("\n⑩b 计划里的工具不在可执行清单 ⇒ 响亮地 BLOCK，不静默跳过")
    led = RunLedger()
    SkillExecutor({}, led, role="admin").run("read_article", {"article_id": 1})
    check("缺工具 ⇒ BLOCK（不是\"这条计划没写工具\"）", len(led.blocked) == 1 and not led.receipts,
          str(led.block_reasons()))

    print("\n⑩c 纯应答技能（chat）留痕在 replies，**不进 receipts**")
    led = RunLedger()
    _txt, n_tools = SkillExecutor({}, led, role="admin").run("chat", {"reply": "好呀"})
    check("chat 没有回执（receipts 的语义是「系统确认的事实」）",
          not led.receipts, str(led.receipts))
    check("留痕在 replies 里", [r["skill"] for r in led.replies] == ["chat"],
          str(led.replies))
    check("回报执行 0 个工具（⇒ 重复调用不判无进展）", n_tools == 0, str(n_tools))

    print("\n⑩d 命令工具没有 meta[cmd] ⇒ 生产 checker 判 BLOCK（判据没被换掉）")
    led = RunLedger()
    SkillExecutor({"navigate_to": lambda path, confirm=False: "页面已跳转"},
                  led, role="admin").run("navigate", {"target": "首页"})
    check("navigate_to 缺 cmd ⇒ BLOCK cmd_shape", led.block_reasons() == ["cmd_shape"],
          str(led.block_reasons()))

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
