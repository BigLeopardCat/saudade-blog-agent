# -*- coding: utf-8 -*-
"""planner 接口层的**接线**单测：离线、秒级、零网络零 LLM。

**为什么单独一套**：`agent/native_plan.py` 的纯函数测试（`tests/test_native_plan.py`）
只验"给一个响应，映射对不对"。而真正会出事的缝在**接线**上——`planner_node` 有没有
把 schema 绑上、预算有没有取错、模型"一个函数都不点"时系统怎么处置、`decided is None`
落到哪一轨。**"能力有测试 ≠ 接线有测试"**是这个仓反复吃过亏的一类洞（探针跑绿了、
线上那条路没接上）。本文件从 `test_planner_engine.py` 改名而来（20261004）——那个名字
指的是一个**已经不存在的拨盘**（`settings.planner_engine`）：接口层只剩 native
tool calls 一条路，没有第二档可比。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · **单通道**：预算取 `settings.planner_native_*`、schema 来自 `visible_skills(role)`
    （与菜单同源，不扩权是结构性的）、提示词是"用工具调用表达决定"那一份；
  · **零调用 ≠ 闲聊**（本批的靶子）：一个函数都没点、正文却非空 ⇒ 走既有纠偏通道
    **一次**（`no_call_nudge`），第二次仍零调用才认成 `chat`（`no_call_accepted`）；
  · **已有帧之后的零调用不纠偏**：那是合法的收尾轮（实测 48 次），重复打扰是净损失；
  · **该取数却点了 `chat` 也不认**（20261004 第二批）：`chat` 的语义是"这一轮不需要
    任何站内数据"，而主人问的偏偏是站内 / 他自己账号里查得到的东西 ⇒ 同样走那条
    一次性纠偏（`data_question_no_tool`），第二次仍点 `chat` 才记
    `data_question_still_no_tool` 放行。判据是 `authz` 里那两条已拿全量语料量过的窄
    判据（`is_own_read_question` / `is_site_corpus_question`），此前只有 `gate_node`
    一个消费方——**这是"判据前移"，不是新判据**：闸门那两条原样留着当兜底。
    三条反锁：`has_frames`（有帧的收尾轮）、`uid <= 0`（零工具身份）、以及不带站内
    指称的闲聊句（`chat` 在那里是**对的**）；
  · **截断 ≠ 闲聊**：`finish_reason=="length"` 走截断轨（`disposition="truncated_wrapup"`），
    直接确定性收尾、不发 `no_call_nudge`——那是预算失败不是采样失败；
  · **认成 chat 时仍是 `answer_only`**：**绝不改成 `wrapped`**——`answer_only` 才是那几条
    零帧声称判据的开火前提，换成 wrapped 等于把闸门悄悄卸掉。

⚠️ 本文件**必须**能证明自己驱动到了 LLM 决策轮：快道（导航/文章/特效切换/屏幕显示）
会零 LLM 直接出计划，用例句子一旦命中快道，下面所有断言都会"绿得毫无意义"。每个用例
因此都断言了"假 LLM 确实被调用过"。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.skills import visible_skills  # noqa: E402
from config import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 这句**不命中任何快道**（导航/文章/特效切换/屏幕显示都要更具体的形态），
# 保证每一轮都真的走到 LLM 决策——见文件头注的"绿得毫无意义"那条。
_MSG = "帮我把樱花打开"
# 零调用反锁那一节要一句**不含任何动作意图**的话：有帧 + 零工具 + 清单里还剩没做过的
# 动作 ⇒ 会触发另一条纠偏（"收尾丢意图"），把这一段要测的东西盖掉。
_MSG_NO_INTENT = "谢谢你呀"
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}


class _FakeLLM:
    """记录构造参数与 `bind_tools` 入参；`invoke` 按脚本返回（脚本见底则复用最后一个）。

    复用最后一个是有意的：纠偏用例要演"同一版响应再来一次"，而"脚本用完"说明这一轮
    调用的次数超出剧本——那是接线出了问题，必须响（`AssertionError`），不能静默。
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.kw: dict = {}
        self.bound: dict | None = None
        self.prompts: list[str] = []

    def bind_tools(self, tools, **kw):
        self.bound = {"tools": tools, **kw}
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("假 LLM 的脚本用完了（本轮调用了不止一次）")
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _zero_call(text: str = "你好呀～", *, finish: str = "stop") -> AIMessage:
    """零 tool_calls 的响应 = "一个函数都没点"（native 契约里那一格）。"""
    return AIMessage(content=text, response_metadata={"finish_reason": finish})


def _call(name: str = "effect", args: dict | None = None) -> AIMessage:
    return AIMessage(content="", response_metadata={"finish_reason": "stop"}, tool_calls=[
        {"name": name, "args": args or {"effect": "sakura", "action": "on"},
         "id": "c1", "type": "tool_call"}])


def _run(replies, *, state_over: dict | None = None,
         cfg: dict | None = None) -> tuple[dict, dict, _FakeLLM]:
    """跑一次 `planner_node`，回 `(出参, trace, 假 LLM)`。`cfg` 只给身份类用例换调用者。"""
    rec = trace_mod.start_trace("t_native", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    llm = _FakeLLM(replies)

    def _get(**kw):                       # 预算断言要看构造参数（同 `_FakeLLM.kw`）
        llm.kw = kw
        return llm

    st = {"messages": [HumanMessage(content=_MSG)], "plan_rounds": 0,
          "executed": [], "tool_data": []}
    st.update(state_over or {})
    old_get, G.get_llm = G.get_llm, _get
    try:
        out = G.planner_node(st, cfg or _CFG)
    finally:
        G.get_llm = old_get
    return out, rec, llm


def _plan_obj(out: dict) -> dict:
    """计划的结构化那一态（`plan_state` 与文本一起写进 `state["plan_obj"]`）。"""
    return out.get("plan_obj") or {}


def _events(rec, name: str) -> list[dict]:
    return [e for e in rec.events if e.get("node") == "planner" and e.get("event") == name]


# ── ① 单通道接线：schema / 预算 / 提示词 / 计划来源 ─────────────────────────
def test_binds_schema_and_takes_the_tool_call_as_the_plan():
    print("\n[接线] 预算取 native 那一组、schema 与 visible_skills 同源、计划来自工具调用")
    out, rec, llm = _run([_call()])
    check("确实走到了 LLM 决策轮（没被快道截走）", len(llm.prompts) == 1)
    check("预算取 settings.planner_native_*（不是历史那组 400/30s）",
          llm.kw.get("max_tokens") == settings.planner_native_max_tokens
          and llm.kw.get("timeout") == settings.planner_native_timeout
          and llm.kw.get("enable_thinking") == settings.planner_native_thinking,
          str({k: v for k, v in llm.kw.items() if k != "temperature"}))
    names = {t["function"]["name"] for t in (llm.bound or {}).get("tools") or []}
    check("绑的 schema 与 visible_skills(admin) 名字集合相等（不扩权是结构性的）",
          names == {s.name for s in visible_skills("admin")}, str(len(names)))
    check("tool_choice=auto + parallel_tool_calls=False",
          (llm.bound or {}).get("tool_choice") == "auto"
          and (llm.bound or {}).get("parallel_tool_calls") is False,
          str(llm.bound and {k: v for k, v in llm.bound.items() if k != "tools"}))
    check("提示词是工具契约那一份、且不含旧文本契约行",
          "工具调用表达" in llm.prompts[0]
          and "SKILL: <技能名>" not in llm.prompts[0])
    check("计划就是那次工具调用（技能级参数）",
          "SKILL=effect" in out["plan"] and '"effect": "sakura"' in out["plan"],
          out["plan"].splitlines()[:2])
    dec = _events(rec, "decision")
    check("decision 事件带 engine=native（键保留：评测按它切语料）",
          len(dec) == 1 and dec[0].get("engine") == "native", str(dec))
    nd = _events(rec, "native_decision")
    check("native_decision 记了函数名与 finish_reason",
          len(nd) == 1 and nd[0].get("calls") == "effect"
          and nd[0].get("finish") == "stop", str(nd))
    check("这一轮不是纠偏轮（契约第 7 条说清了，不该白多问一次）",
          not _events(rec, "no_call_nudge") and not _events(rec, "no_call_accepted"))


def test_multi_call_takes_first_and_accounts():
    print("\n[接线] 多调用：只取第一条 + 记账进 decision 事件")
    two = AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "a", "type": "tool_call"},
        {"name": "darkmode", "args": {"mode": "on"}, "id": "b", "type": "tool_call"}])
    out, rec, _llm = _run([two])
    check("取第一条（effect）", "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    check("darkmode 没有被拼进同一份计划（一张确认卡只装同一个技能的动作）",
          "SKILL=darkmode" not in out["plan"]
          and "darkmode" not in out["plan"].split("\n")[0])
    dec = _events(rec, "decision")
    check("记账进了 decision 事件（native_multi_call:…）",
          len(dec) == 1 and "native_multi_call:effect|darkmode" in str(dec[0].get("native_note")),
          str(dec[0].get("native_note")))


# ── ② 零调用纠偏：本批的靶子 ───────────────────────────────────────────────
def test_zero_call_is_nudged_once_then_accepted_as_chat():
    print("\n[零调用] 第一次纠偏、第二次认成 chat（`no_call_accepted` 恰一条）")
    out, rec, llm = _run([_zero_call(), _zero_call()])
    check("LLM 被问了两次（一版响应 + 一次纠偏）", len(llm.prompts) == 2, str(len(llm.prompts)))
    check("第二次的提示词里带着纠偏（讲的是「这一轮一个函数都没点」这个事实）",
          "一个函数都没有点" in llm.prompts[1])
    nudge = _events(rec, "no_call_nudge")
    check("`no_call_nudge` 恰一条、记了 finish 与正文长度",
          len(nudge) == 1 and nudge[0].get("finish") == "stop"
          and nudge[0].get("text_len", 0) > 0, str(nudge))
    acc = _events(rec, "no_call_accepted")
    check("`no_call_accepted` 恰一条（纠偏后仍零调用才记）",
          len(acc) == 1 and acc[0].get("finish") == "stop", str(acc))
    check("计划落在 chat（认下来了）", "SKILL=chat" in out["plan"], out["plan"].splitlines()[0])
    check("★ 状态是 `answer_only` 而**不是** `wrapped`（改 wrapped = 把零帧声称判据卸掉）",
          _plan_obj(out).get("status") == "answer_only"
          and _plan_obj(out).get("chat") is True, str(_plan_obj(out).get("status")))
    check("零工具（认成闲聊不产生任何执行）",
          _plan_obj(out)["tools"] == [], str(_plan_obj(out)["tools"]))


def test_zero_call_then_a_real_tool_call_lands_on_the_skill():
    print("\n[零调用] 纠偏真的把工具调用找回来了（第二次点了函数）")
    out, rec, llm = _run([_zero_call(), _call()])
    check("LLM 被问了两次", len(llm.prompts) == 2, str(len(llm.prompts)))
    check("计划落在被点中的那个技能上（不是 chat）",
          "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    check("★ 没有 `no_call_accepted`——纠偏成功就不该记「认了」",
          not _events(rec, "no_call_accepted"))
    check("`no_call_nudge` 仍在（第一次确实零调用，这件事要留痕）",
          len(_events(rec, "no_call_nudge")) == 1)


def test_zero_call_with_frames_is_not_nudged():
    print("\n[零调用·反锁] 已有工具帧之后的零调用 = 合法收尾轮，不打扰")
    frames = [HumanMessage(content=_MSG_NO_INTENT),
              ToolMessage(content="特效 樱花(sakura) 已打开", tool_call_id="c1")]
    out, rec, llm = _run([_zero_call("已经帮你打开啦")], state_over={"messages": frames})
    check("确实走到了 LLM 决策轮", len(llm.prompts) == 1, str(len(llm.prompts)))
    check("★ 一次都不纠偏（`no_call_nudge` 缺席）", not _events(rec, "no_call_nudge"))
    check("★ 也不记 `no_call_accepted`（它不是「认下来」的那一格）",
          not _events(rec, "no_call_accepted"))
    check("计划仍是 chat", "SKILL=chat" in out["plan"], out["plan"].splitlines()[0])


# ── ②′ 该取数却点 `chat`：判据前移到决策层（20261004 第二批）────────────────
# 这句命中 `authz.is_own_read_question`（"我都有哪些收藏" = 自己账号里的私有数据），
# 且**不命中任何快道**。带上 `state_over` 换掉默认消息即可（`_run` 先铺默认再 update）。
_MSG_OWN_DATA = "小猫咪我都有哪些收藏"
# 语义上正确的落点：`content_query` 点名无参只读工具 `list_my_favorites`（见 skills.py
# 的 `_EXPLICIT_TOOLS_ORDER`——"我收藏了哪些文章"正是那一族被加进菜单的理由）。
_READ_ARGS = {"tools": ["list_my_favorites"]}


def _chat() -> AIMessage:
    """**显式**点 `chat`：契约第 7 条要求的形态，`undecided` 为假。

    这正是本批要抓的残余：契约改动把"什么都不点"变成了"点 `chat`"，而纠偏只看
    `undecided` ⇒ 洞没有消失，只是**挪了一格**（探针实测，读计数：同 60 格数据型轮里
    「一个都不点」8 格 → 2 格，而显式 `chat` 4 格 → 6 格；总量 12/60 → 8/60）。
    明细见 `docs/zero-call-residual.md` §3.1。
    """
    return AIMessage(content="", response_metadata={"finish_reason": "stop"}, tool_calls=[
        {"name": "chat", "args": {}, "id": "c0", "type": "tool_call"}])


def test_chat_on_a_data_question_is_nudged_once_then_lands_on_a_tool():
    print("\n[该取数却点 chat] 纠偏一次、第二次落到真工具（`data_question_no_tool` 恰一条）")
    out, rec, llm = _run([_chat(), _call("content_query", _READ_ARGS)],
                         state_over={"messages": [HumanMessage(content=_MSG_OWN_DATA)]})
    check("LLM 被问了两次（一版决策 + 一次纠偏）", len(llm.prompts) == 2, str(len(llm.prompts)))
    check("纠偏提示讲的是「主人在问站内/你自己的数据」这个事实",
          "系统判定" in llm.prompts[1] and "站内" in llm.prompts[1])
    nud = _events(rec, "data_question_no_tool")
    check("`data_question_no_tool` 恰一条（不是 `no_call_nudge`：那是零调用那一格）",
          len(nud) == 1 and not _events(rec, "no_call_nudge"), str(nud))
    check("★ 纠偏后落到真工具上（`SKILL=content_query` + 点名的只读工具）",
          "SKILL=content_query" in out["plan"] and "list_my_favorites" in out["plan"],
          out["plan"].splitlines()[:2])
    check("★ 没有 `no_call_accepted`（那是「认成零调用」的账，不许串台）",
          not _events(rec, "no_call_accepted"))
    check("也没有 `data_question_still_no_tool`（纠偏成功了）",
          not _events(rec, "data_question_still_no_tool"))


def test_chat_still_on_a_data_question_is_released_not_nudged_twice():
    print("\n[该取数却点 chat·第二次] 不救第二遍：记账放行，闸门仍是兜底")
    out, rec, llm = _run([_chat()],
                         state_over={"messages": [HumanMessage(content=_MSG_OWN_DATA)]})
    check("★ 只纠偏一次就放行（不再多花一次 LLM）", len(llm.prompts) == 2, str(len(llm.prompts)))
    still = _events(rec, "data_question_still_no_tool")
    check("`data_question_still_no_tool` 恰一条（供全量 trace 复扫盯残余）",
          len(still) == 1, str(still))
    check("计划仍是 chat/answer_only、零工具（**绝不改成 wrapped**：那是把闸门卸掉）",
          _plan_obj(out).get("status") == "answer_only"
          and _plan_obj(out).get("chat") is True and _plan_obj(out)["tools"] == [],
          str(_plan_obj(out).get("status")))


def test_chat_without_a_data_question_is_not_nudged():
    print("\n[该取数却点 chat·反锁 ①] 闲聊句点 chat 是**对的**，不许打扰")
    out, rec, llm = _run([_chat()],
                         state_over={"messages": [HumanMessage(content=_MSG_NO_INTENT)]})
    check("一次都不多问", len(llm.prompts) == 1, str(len(llm.prompts)))
    check("★ `data_question_no_tool` 与 `no_call_nudge` 都缺席",
          not _events(rec, "data_question_no_tool") and not _events(rec, "no_call_nudge"))
    check("计划就是 chat/answer_only、零工具",
          _plan_obj(out).get("status") == "answer_only" and _plan_obj(out)["tools"] == [],
          str(_plan_obj(out).get("status")))


def test_chat_on_a_data_question_with_frames_is_not_nudged():
    print("\n[该取数却点 chat·反锁 ②] 已有工具帧之后 = 合法收尾轮，不打扰")
    frames = [HumanMessage(content=_MSG_OWN_DATA),
              ToolMessage(content="收藏夹里共有 3 篇", tool_call_id="c1")]
    out, rec, llm = _run([_chat()], state_over={"messages": frames})
    check("一次都不多问", len(llm.prompts) == 1, str(len(llm.prompts)))
    check("★ 不纠偏（`data_question_no_tool` 缺席）",
          not _events(rec, "data_question_no_tool"))
    check("也不记 `data_question_still_no_tool`（它不是「纠偏后被放行」那一格）",
          not _events(rec, "data_question_still_no_tool"))
    check("计划仍是 chat", "SKILL=chat" in out["plan"], out["plan"].splitlines()[0])


def test_chat_on_a_data_question_without_a_uid_is_not_nudged():
    print("\n[该取数却点 chat·反锁 ③] uid<=0（零工具身份）不催它取数")
    cfg = {"configurable": {"principal": Principal(uid=0, role="visitor"),
                            "user_id": 0, "conversation_id": 42, "stop_event": None}}
    _out, rec, llm = _run([_chat()], state_over={
        "messages": [HumanMessage(content=_MSG_OWN_DATA)]}, cfg=cfg)
    check("★ 不催（它本来就没有那条取数通道，催了只会换个说法）",
          not _events(rec, "data_question_no_tool"), str(len(llm.prompts)))


# ── ③ 截断轨：`finish=length` 不是闲聊 ─────────────────────────────────────
def test_truncated_output_goes_to_the_truncation_track():
    print("\n[截断] finish=length → 确定性收尾，且**不**发零调用纠偏（两种病不混）")
    out, rec, llm = _run([_zero_call("半截的正文……", finish="length")])
    check("只问了 LLM 一次（截断是预算失败，同一条消息再问多半截在同一处）",
          len(llm.prompts) == 1, str(len(llm.prompts)))
    fb = _events(rec, "native_fallback")
    check("落了 native_fallback 且 disposition=truncated_wrapup",
          len(fb) == 1 and fb[0].get("disposition") == "truncated_wrapup"
          and fb[0].get("finish") == "length", str(fb))
    check("★ 没有 `no_call_nudge`（截断 ≠ 闲聊，别混成一条路）",
          not _events(rec, "no_call_nudge"))
    check("也没记 `no_call_accepted`", not _events(rec, "no_call_accepted"))
    check("本轮零执行（收尾轮不成事）",
          _plan_obj(out)["tools"] == [], str(_plan_obj(out)["tools"]))
    check("计划是确定性收尾（status=wrapped）",
          _plan_obj(out).get("status") == "wrapped", str(_plan_obj(out).get("status")))


def test_unparseable_output_is_nudged_once_then_wrapped():
    print("\n[不可解析] 形态坏 → 纠偏一次；两次都坏 → 确定性收尾")
    broken = AIMessage(content="", invalid_tool_calls=[
        {"name": "effect", "args": '{"effect": "sak', "id": "c", "error": "parse"}])
    out, rec, llm = _run([broken, broken])
    check("问了两次（形态轨给一次重决议）", len(llm.prompts) == 2, str(len(llm.prompts)))
    fb = _events(rec, "native_fallback")
    check("第一次记 disposition=retry、第二次记 unparseable_wrapup",
          [e.get("disposition") for e in fb] == ["retry", "unparseable_wrapup"], str(fb))
    check("没有走零调用那条路（「没点」与「读不出」是两种病）",
          not _events(rec, "no_call_nudge") and not _events(rec, "no_call_accepted"))
    check("收尾计划 status=wrapped、零工具",
          _plan_obj(out).get("status") == "wrapped" and _plan_obj(out)["tools"] == [],
          str(_plan_obj(out).get("status")))


# ── ④ 契约源码锁 ───────────────────────────────────────────────────────────
def test_contract_no_longer_licenses_an_empty_decision():
    print("\n[契约] 规则 7 不再许可「什么都不点」，且把 chat 说成显式动作")
    n = G._PLANNER_OUTPUT_CONTRACT_NATIVE
    check("★ 没有「可以不调用任何函数」这类许可句（那就是本批要消灭的那条合法退路）",
          "可以不调用任何函数" not in n)
    check("讲明了闲聊也要**显式**点 `chat`", "chat" in n and "显式" in n)
    check("讲明了「一个函数都不点 = 没有做出决策」",
          "一个函数都不点" in n and "没有做出决策" in n)
    check("不含旧文本契约行（两版混写就是让模型二选一）",
          "SKILL: <技能名>" not in n and "PARAMS: <JSON>" not in n)
    check("保留 TODO 行（多步链声明仍走正文，`_parse_todo` 读它）", "TODO:" in n)
    check("仍是规则 7（改契约不该改规则编号）", n.startswith("7. "))


# ── ⑬ 路由确定性：规划温度必须**从 settings 读**，不许再有写死的字面量 ───────
# 为什么值得单独钉一条：planner 的 `temperature` 曾经是**写死的 0.2**，而它同时
# 是"路由确定性"这枚旋钮和调优实验的因子——一旦有人把 `settings.planner_temperature`
# 换回字面量，改设置**静默无效**（本仓吃过这个亏：`from __future__ import annotations`
# 让 config 注入静默失效那一类洞）。所以这里钉的不是"0.0 这个值"（调优实验结论若指向
# 别的值就该改默认，那条不该被测试挡住），而是**"planner 读的是设置"这条接线**。
def test_planner_temperature_comes_from_settings():
    print("\n[接线] 规划温度取 settings.planner_temperature（不是写死的字面量）")
    old = settings.planner_temperature
    settings.planner_temperature = 0.37      # 一个绝不会与字面量撞车的哨兵值
    try:
        _out, _rec, llm = _run([_call()])
    finally:
        settings.planner_temperature = old
    check("planner 把哨兵值原样传给了 get_llm",
          llm.kw.get("temperature") == 0.37, str(llm.kw.get("temperature")))
    check("默认值是 0.0（确定性那一档，20261006）",
          old == 0.0, str(old))


# ── ⑭ 臂的身份证：trace 里必须能读出这次是哪组旋钮跑的 ───────────────────
# 为什么值得钉：调参实验的全部意义是"逐臂比较"，而在此之前 planner 的温度/种子/
# 模型在 trace 里**一个字都没有**——一堆报告跑完，谁也说不清哪份对应哪臂。
def test_decision_event_carries_the_arm_identity():
    print("\n[接线] native_decision 带臂的身份证（provider/model/temp/seed/thinking）")
    _out, rec, _llm = _run([_call()])
    nd = _events(rec, "native_decision")
    check("恰好一条", len(nd) == 1, str(nd))
    e = nd[0] if nd else {}
    check("温度那一格 == planner_temperature（与 get_llm 同源）",
          e.get("temp") == settings.planner_temperature, str(e.get("temp")))
    check("种子那一格 == llm_seed",
          e.get("seed") == settings.llm_seed, str(e.get("seed")))
    check("provider 与 model 都在",
          e.get("provider") == settings.llm_provider
          and e.get("model") == settings.active_llm_model,
          f"{e.get('provider')}/{e.get('model')}")
    check("thinking 那一格在（native 三项的第三项）",
          e.get("thinking") == settings.planner_native_thinking, str(e.get("thinking")))


if __name__ == "__main__":
    for fn in (test_binds_schema_and_takes_the_tool_call_as_the_plan,
               test_multi_call_takes_first_and_accounts,
               test_zero_call_is_nudged_once_then_accepted_as_chat,
               test_zero_call_then_a_real_tool_call_lands_on_the_skill,
               test_zero_call_with_frames_is_not_nudged,
               test_chat_on_a_data_question_is_nudged_once_then_lands_on_a_tool,
               test_chat_still_on_a_data_question_is_released_not_nudged_twice,
               test_chat_without_a_data_question_is_not_nudged,
               test_chat_on_a_data_question_with_frames_is_not_nudged,
               test_chat_on_a_data_question_without_a_uid_is_not_nudged,
               test_truncated_output_goes_to_the_truncation_track,
               test_unparseable_output_is_nudged_once_then_wrapped,
               test_contract_no_longer_licenses_an_empty_decision,
               test_planner_temperature_comes_from_settings,
               test_decision_event_carries_the_arm_identity):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
