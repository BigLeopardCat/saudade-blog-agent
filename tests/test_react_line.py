"""`agent/react_line.py` 的单测：离线、秒级、零网络零 LLM。

**20261016 重写**。这份文件此前驱动的是 `create_agent` + `ConvergenceMiddleware` +
`wrap_tools_with_receipts` 那一套骨架（三条收敛判据挂在 `before_model` / `after_model`
两个钩子上，工具级回执挂在包装层上）。那三件**整体退场**了——第二条臂现在是一张真的
`StateGraph`（`agent/react_arm.py`），判据由 `planner` 节点**直接调**。所以这份文件
换了两件事：

  · **循环级的行为**（无进展 / 预算 / 纠偏 / 权限闸真的拦住了执行）搬去了
    `tests/test_react_arm.py`——那边跑的是真图、真节点、真帧；在这里再搭一个假循环
    只会搭出**第二份实现**，而两份实现对同一件事的看法迟早会分家。
  · **本文件改判剩下的四件纯资产**：`spec_signature` / `RunLedger` / `WorkflowExecutor` /
    `convergence_verdict` / `wrap_up_text`。它们是判据的**全部输入输出面**，逐格锁住
    就等于把循环级的判据锁住了一大半（剩下的那一半是"节点有没有真的调它"，归
    `test_react_arm.py`）。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · **签名归一**：`(工具, 参数)` 的键序/类型漂移都算同一个调用；
  · **台账分列**：checker PASS 进 `receipts`、BLOCK 进 `blocked` 带原因码、纯应答技能
    进 `replies`（**不进 receipts**——receipts 的语义是"系统确认的事实"）；
  · **技能级展开**：一次技能调用 = 实例化计划 + 逐工具验收，`run()` 回报真跑了几件；
  · **台账登记**（20261016 新增，本批 ④b 那条判据的燃料）：`WorkflowExecutor.run` 记的是
    **技能级**签名、且取参在**字面路径修正之前**——两条口径都必须与
    `convergence_verdict` 的对照方（模型的原始 `tool_calls`）同源，错一条 ⇒ 无进展判据
    恒不触发（症状是"同一个动作原地打转到撞预算"，报告里看着像模型不听话）；
  · **三条收敛判据**：预算（入口相）/ 无进展（出口相）/ 意图纠偏（只此一次）；
  · **权限闸**（P1，20261004）：判据与 `graph.execute_node` **同一条**
    （`not allowed and authz.must_enforce(decision, principal)`，20261019 起从
    `enforcing(scope)` 换来），被拒的调用**也记进台账**。

⚠️ 这里**没有假模型**：本文件不再跑循环。循环级用例（含假模型）在 `test_react_arm.py`，
那是"接线"那一层；"模型会不会用工具"只有真链路的 A/B
（`eval/experiments/react_line_ab.py`）能回答，别拿这份绿灯去替它背书。
"""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from langchain_core.messages import AIMessage  # noqa: E402

from agent import authz  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.react_line import (  # noqa: E402
    RunLedger,
    WorkflowExecutor,
    convergence_verdict,
    spec_signature,
    wrap_up_text,
)

ADMIN = Principal(uid=1, role="superadmin")


FAILS: list[str] = []
check = collector(FAILS)


def authz_deny(principal, tool: str) -> bool:
    """这个身份调这个工具**会不会被闸拦下**（判据与 `graph.execute_node` 同一条）。

    20261019 起是 `must_enforce(decision, principal)` 而不是 `enforcing(decision.scope)`：
    "这一次拦不拦"多看一眼这次是谁在调（未验成的身份不进灰度，见 tests/test_authz.py ⑪）。
    """
    d = authz.check(principal, tool)
    return not d.allowed and authz.must_enforce(d, principal)


def _call(name: str, **args) -> AIMessage:
    """模型这一轮点名的一个**技能**（第二条臂绑的是技能菜单，不是裸工具）。"""
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call_{name}_{len(args)}", "type": "tool_call"}])


def main() -> int:  # noqa: C901
    # ── ① 签名归一 ────────────────────────────────────────────────────────
    print("\n① 签名归一：键序、类型漂移都算同一个调用")
    check("键序无关", spec_signature("t", {"a": 1, "b": "2"})
          == spec_signature("t", {"b": 2, "a": "1"}))
    check("工具名不同 ⇒ 签名不同", spec_signature("t1", {}) != spec_signature("t2", {}))
    check("`None` 参数与空串同归一（JSON 里 `null` 与 `\"\"` 是同一个意思：没填）",
          spec_signature("t", {"a": None}) == spec_signature("t", {"a": "  "}))
    check("不可序列化的参数降级成字面量、不炸",
          bool(spec_signature("t", {"a": object()})))

    # ── ② 台账分列 ────────────────────────────────────────────────────────
    print("\n② 台账分列：事实 / 受阻 / 应答 三本账，混装会改读数")
    led = RunLedger()
    led.receipts.append({"tool": "freeze_account", "args": {"uid": "12"}, "result": "已冻结"})
    led.blocked.append({"tool": "freeze_account", "reason": "unknown_target", "args": {}})
    led.replies.append({"workflow": "chat", "reply": "好呀"})
    check("`passed_tools` 只读 receipts（BLOCK 的那条不算「办成了」）",
          led.passed_tools() == ["freeze_account"] and led.block_reasons() == ["unknown_target"])
    check("`executed` 是签名计数器（不是布尔）——重复阈值靠它",
          not led.seen("t::{}") and led.record("t::{}") is None and led.seen("t::{}")
          and led.executed["t::{}"] == 1)

    # ── ③ 技能级展开：一次技能调用 = 实例化计划 + 逐工具验收 ───────────────
    print("\n③ 技能级回执（WorkflowExecutor）")
    led = RunLedger()
    ex = WorkflowExecutor({"get_article_detail": lambda article_id: f"文章 {article_id} 的正文"},
                       led, role="admin")
    text, n_tools = ex.run("read_article", {"article_id": 23})
    check("技能被展开成它计划里的工具（不是拿技能名去验收）",
          led.receipts and led.receipts[0]["tool"] == "get_article_detail",
          str([r["tool"] for r in led.receipts]))
    check("参数从技能参数实例化进工具（article_id=23）",
          led.receipts[0]["args"] == {"article_id": "23"}, str(led.receipts[0]["args"]))
    check("正文里带上了工具输出与回执行", "文章 23 的正文" in text and "内部回执" in text)
    check("回报执行了几个工具（无进展判据的输入）", n_tools == 1, str(n_tools))
    check("真工具名进了 `calls` 留痕（golden 的 `tool_calls` 输入）",
          [c["tool"] for c in ex.calls] == ["get_article_detail"], str(ex.calls))

    print("\n③b 计划里的工具不在可执行清单 ⇒ 响亮地 BLOCK，不静默跳过")
    led = RunLedger()
    WorkflowExecutor({}, led, role="admin").run("read_article", {"article_id": 1})
    check("缺工具 ⇒ BLOCK（不是「这条计划没写工具」）",
          len(led.blocked) == 1 and not led.receipts, str(led.block_reasons()))

    print("\n③c 纯应答技能（chat）留痕在 replies，**不进 receipts**")
    led = RunLedger()
    _txt, n_tools = WorkflowExecutor({}, led, role="admin").run("chat", {"reply": "好呀"})
    check("chat 没有回执（receipts 的语义是「系统确认的事实」）",
          not led.receipts, str(led.receipts))
    check("留痕在 replies 里", [r["workflow"] for r in led.replies] == ["chat"],
          str(led.replies))
    check("回报执行 0 个工具（⇒ 重复调用不判无进展）", n_tools == 0, str(n_tools))

    print("\n③d 命令工具没有 meta[cmd] ⇒ 生产 checker 判 BLOCK（判据没被换掉）")
    led = RunLedger()
    WorkflowExecutor({"navigate_to": lambda path, confirm=False: "页面已跳转"},
                  led, role="admin").run("navigate", {"target": "首页"})
    check("navigate_to 缺 cmd ⇒ BLOCK cmd_shape", led.block_reasons() == ["cmd_shape"],
          str(led.block_reasons()))

    # ── ④ 台账登记：无进展判据的燃料（20261016，本批 ④b 那条判据的根因）─────────
    print("\n④ 台账登记：签名必须与判据的对照方**同源**")
    led = RunLedger()
    WorkflowExecutor({"get_article_detail": lambda article_id: "正文"}, led,
                  role="admin").run("read_article", {"article_id": 7})
    check("跑过工具的技能 ⇒ 记了一条**技能级**签名（不是工具级 `get_article_detail`）",
          led.executed.get(spec_signature("read_article", {"article_id": 7})) == 1
          and not led.executed.get(spec_signature("get_article_detail", {"article_id": 7})),
          str(dict(led.executed)))
    check("**判据照着模型的原始 `tool_calls` 算得出同一把键**（这条不对，无进展恒不触发）",
          convergence_verdict(led, rounds=1, budget=6,
                              last_ai=_call("read_article", article_id=7))["kind"]
          == "no_progress")
    led2 = RunLedger()
    WorkflowExecutor({}, led2, role="admin").run("chat", {"reply": "好呀"})
    check("纯应答技能（0 个工具）**不登记**（把没做过的事记成做过 ⇒ 收尾印出假件数）",
          not led2.executed, str(dict(led2.executed)))

    print("\n④b 字面路径修正**之前**取参（判据的对照方是模型的原始参数）")
    # 主人原话带 `/guestbook`，模型点的是「物联网平台」（**语义替身**）⇒
    # `WorkflowExecutor.run` 会把 target 强制改成 `/guestbook`。签名若取**修正后**的参，
    # 这一格就永远对不上模型的原始 `tool_calls`。
    led = RunLedger()
    WorkflowExecutor({"navigate_to": lambda **kw: "页面已跳转"}, led, role="admin",
                  user_msg="带我去 /guestbook 看看").run("navigate", {"target": "物联网平台"})
    check("记的是**模型原样**的那个 target（修正后的 `/guestbook` 不在账上）",
          led.executed.get(spec_signature("navigate", {"target": "物联网平台"})) == 1
          and not led.executed.get(spec_signature("navigate", {"target": "/guestbook"})),
          str(dict(led.executed)))

    # ── ⑤ 权限闸（P1）：无身份 ⇒ 硬 scope 不执行 ──────────────────────────
    # 靶子选 `account_roster`（读后台账号列表）而不是 `account_freeze`：后者的 scope 是
    # `write.console`，它**先被同意闸拦下**（未获同意 ⇒ 一个都不执行、记 `pending`），
    # 根本走不到权限闸那一行——拿它当靶子测的是同意闸，不是权限闸。
    # `list_accounts` 是**读**工具、不吃同意闸，而 `admin.console` 在 `_HARD_SCOPES` 里
    # （不吃 shadow 开关）⇒ 它是这条判据唯一能真的开火的靶子。
    print("\n⑤ 权限闸：身份不明不执行（fail-closed），且记的是受阻不是事实")
    _ran: list = []
    led = RunLedger()
    WorkflowExecutor({"list_accounts": lambda **kw: _ran.append(kw) or "账号列表"},
                  led, role="admin", principal=Principal(uid=0, role=None)).run(
        "account_roster", {})
    check("**工具一次都没真的跑**", _ran == [], str(_ran))
    check("记的是受阻、不是事实",
          not led.receipts and len(led.blocked) == 1, f"blocked={led.block_reasons()}")
    check("受阻带原因码（`_check_spec` 认得权限帧 ⇒ 走同一条受阻链路）",
          led.block_reasons() == ["unknown_role"], str(led.block_reasons()))
    _led_u = RunLedger()
    WorkflowExecutor({"list_accounts": lambda **kw: "账号列表"}, _led_u, role="admin",
                  principal=Principal(uid=7, role="user")).run("account_roster", {})
    check("普通用户同样越不过硬 scope（原因码是「角色认得、但这个 scope 没授予」，"
          "与「身份不明」分开——两族读数不同）",
          _led_u.block_reasons() == ["denied"] and not _led_u.receipts,
          str(_led_u.block_reasons()))
    check("管理员则放行（闸不是「一律拦」）", not authz_deny(ADMIN, "list_accounts"))

    # ── ⑥ 收敛判据（纯函数）：入口相 / 出口相 ─────────────────────────────
    print("\n⑥ 收敛判据：`rounds >= budget` 只在**入口相**（`last_ai is None`）判")
    _empty = RunLedger()
    check("入口相、轮数没到 ⇒ 继续", convergence_verdict(_empty, rounds=0, budget=3)["kind"]
          == "continue")
    check("入口相、轮数用满 ⇒ budget",
          convergence_verdict(_empty, rounds=3, budget=3)["kind"] == "budget")
    check("出口相**不判预算**（撞预算的那一轮照常执行——判在这里会把模型已经点名的调用"
          "整个吞掉，主人等了半天、动作没发生）",
          convergence_verdict(_empty, rounds=9, budget=3,
                              last_ai=_call("navigate", target="首页"))["kind"] == "continue")

    print("\n⑥b 无进展：这一轮点的每个签名都执行过 ⇒ 不执行、收尾")
    _led = RunLedger()
    _led.record(spec_signature("navigate", {"target": "首页"}))
    check("同签名再点 ⇒ no_progress（重复那次不执行）",
          convergence_verdict(_led, rounds=1, budget=6,
                              last_ai=_call("navigate", target="首页"))["kind"]
          == "no_progress")
    check("换了参数 ⇒ continue（不是「同一个工具点两次就算卡住」）",
          convergence_verdict(_led, rounds=1, budget=6,
                              last_ai=_call("navigate", target="留言板"))["kind"]
          == "continue")
    check("**混合轮不判停**（既有新调用又有重复：那一轮里还有没做过的事，停掉等于把"
          "主人的活儿丢了）",
          convergence_verdict(_led, rounds=1, budget=6,
                              last_ai=AIMessage(content="", tool_calls=[
                                  {"name": "navigate", "args": {"target": "首页"}, "id": "a",
                                   "type": "tool_call"},
                                  {"name": "navigate", "args": {"target": "留言板"}, "id": "b",
                                   "type": "tool_call"}]))["kind"] == "continue")
    check("`repeat_limit` 可配（=2 时执行过一次还不算卡住）",
          convergence_verdict(_led, rounds=1, budget=6, repeat_limit=2,
                              last_ai=_call("navigate", target="首页"))["kind"] == "continue")

    print("\n⑥c 零工具轮 = 模型自己宣布收尾：三条守卫，缺一不纠")
    check("首轮零工具（台账空）⇒ 不纠（没看过任何返回，再问一次通常还是零工具）",
          convergence_verdict(RunLedger(), rounds=0, budget=6, user_msg="把樱花特效打开",
                              last_ai=AIMessage(content="开好啦"))["kind"] == "continue")
    check("`user_msg` 空 ⇒ 不纠（fail-safe：绝不凭一个读不到的字段把这一轮改道）",
          convergence_verdict(_led, rounds=1, budget=6, user_msg="",
                              last_ai=AIMessage(content="开好啦"))["kind"] == "continue")
    check("`corrected` 已置 ⇒ 不纠（纠第二次是把「它坚持不做」读成「它没听懂」）",
          convergence_verdict(_led, rounds=2, budget=6, user_msg="把樱花特效打开",
                              corrected=True,
                              last_ai=AIMessage(content="开好啦"))["kind"] == "continue")
    _led2 = RunLedger()
    _led2.receipts.append({"tool": "toggle_effect",
                           "args": {"effect": "rain", "action": "off"}, "result": "已关"})
    _v = convergence_verdict(_led2, rounds=1, budget=6, user_msg="把樱花特效打开",
                             last_ai=AIMessage(content="都弄好了"))
    check("台账非空 + 原话里还有没做过的动作 ⇒ correct，且点名那一件",
          _v["kind"] == "correct" and any("sakura" in i["key"] for i in _v["pending"]),
          str(_v))
    _led3 = RunLedger()
    _led3.receipts.append({"tool": "toggle_effect",
                           "args": {"effect": "sakura", "action": "on"}, "result": "已开"})
    check("那件**已经做过** ⇒ 不纠（判据是「清单里还有没做过的」，不是「有没有零工具」）",
          convergence_verdict(_led3, rounds=1, budget=6, user_msg="把樱花特效打开",
                              last_ai=AIMessage(content="开好啦"))["kind"] == "continue")
    check("BLOCK 过的那件**也算做过**（生产口径：上过计划的 spec 都进了 executed，"
          "被拒过再推它重试一次是另一种病）",
          convergence_verdict(
              _l4 := _with_blocked(), rounds=1, budget=6, user_msg="把樱花特效打开",
              last_ai=AIMessage(content="开好啦"))["kind"] == "continue")

    # ── ⑦ 收尾正文：只说系统确认过的事实 ──────────────────────────────────
    print("\n⑦ 收尾正文（`wrap_up_text`）：为什么停 + 真办成几件")
    _done = RunLedger()
    _done.receipts.append({"tool": "navigate_to", "args": {"path": "/"}, "result": "已跳转"})
    _np = wrap_up_text(_done, "no_progress")
    _bg = wrap_up_text(_done, "budget")
    check("两种停因各说各的话（措辞不自己拼）",
          "没进展" in _np and "步骤数用满了" in _bg, _bg[:40])
    check("件数与工具名来自台账", "确实办成 1 件" in _np and "navigate_to" in _np, _np[:80])
    check("零件时如实说零件（**不假装办好了**，也不承诺「稍后再办」）",
          "没有任何一件办成" in wrap_up_text(RunLedger(), "budget")
          and "稍后" not in _np)

    # ── ⑧ `tool_data` 的取值格：`data` 不是 `result` ───────────────────────
    print("\n⑧ 工具结果入 `tool_data`：`$tool[i].field` 引用必须**真的解得开**")
    # 两个读端（`arg_refs.resolve_one` / `ref_hints`）读的都是 `entry["data"]`，而
    # 20261021 之前这里写的是 `"result": text[:2000]` ⇒ 本线每条臂上引用**恒判
    # `ref_unparsed`（BLOCK）**、"引用解析"看着在跑其实一次都没成功过。判据取**行为**
    # （第二条 spec 的实参是不是真拿到了第一条的返回值），不取键名——键名是手段。
    from agent.arg_refs import ref_hints as _rh
    from tools.result import ToolResult
    _seen: list[dict] = []

    def _list_notes(**kw) -> ToolResult:            # noqa: ARG001
        return ToolResult("共 1 篇", data=[{"noteId": 7, "title": "标题"}])

    def _detail(article_id) -> str:                 # noqa: ANN001
        _seen.append({"article_id": article_id})
        return f"文章 {article_id} 的正文"

    _l8 = RunLedger()
    _txt8, _n8 = WorkflowExecutor({"list_notes": _list_notes, "get_article_detail": _detail},
                                  _l8, role="admin").run_specs(
        ['list_notes({})', 'get_article_detail({"article_id": "$list_notes[0].noteId"})'],
        workflow="content_query")
    check("两条都真执行了（引用没把它拦成 BLOCK）", _n8 == 2, str(_n8))
    check("**引用真的解开了**：第二条拿到的是第一条返回里的 noteId=7",
          _seen == [{"article_id": 7}], str(_seen))
    check("  帧文本里也是解开后的值，不是 `ref_unparsed`",
          "文章 7" in _txt8 and "ref_unparsed" not in _txt8, _txt8[:120])
    # 反控：把 `data` 换成 `result`（就是修之前那个形状）⇒ 引用当场解不开。
    # 判据必须能红，否则上面两条只是"恰好通过"。
    _bad_td = [{"tool": "list_notes", "result": [{"noteId": 7, "title": "标题"}]}]
    from agent.arg_refs import resolve_args as _ra
    _got, _err = _ra({"article_id": "$list_notes[0].noteId"}, _bad_td)
    check("反控：键名退回 `result` ⇒ 引用立刻解不开（报 `ref_unparsed`）",
          _got is None and str(_err).startswith("ref_unparsed"), f"{_got} / {_err}")
    check("`ref_hints` 也读同一格 ⇒ 现在能列出可引用字段（此前恒回「无从引用」）",
          "list_notes" in _rh([{"tool": "list_notes", "data": [{"noteId": 7}]}]))
    check("  反控：同一份退回 `result` ⇒ `ref_hints` 什么都列不出来",
          "list_notes" not in _rh(_bad_td))

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


def _with_blocked() -> RunLedger:
    """台账里有一件**被拒过**的 `effect:sakura=on`（BLOCK 也算做过）。"""
    led = RunLedger()
    led.blocked.append({"tool": "toggle_effect",
                        "args": {"effect": "sakura", "action": "on"},
                        "reason": "unknown_role"})
    return led


def test_react_line():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_react_line()
