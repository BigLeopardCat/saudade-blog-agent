# -*- coding: utf-8 -*-
"""动作事实块（D3，`agent/factblock.py` + 接线）单测：离线、秒级、零网络零 LLM。

**为什么单独一套**：D3 把动作族轮次的**叙述权**从模型搬到系统——用户可见正文 =
系统印的事实块 + 模型的包装。这一搬动有三处会静默出错，每一处都只能靠判据钉：

  ① **分类**（`family_of`）：射程（命令族 + 写族）与 `eval/narrator_facts_share.py`
     的量化口径是同一份实现——两处各写一份正则会漂移，而漂移之后"能砍多少"这句话
     就不可核（数字还在、说的是另一件事）；
  ② **顺序**（`server.py` 的 producer）：事实块必须在 narrator 的文本**之前**流出去
     （主人先读事实、再读包装），且 `__RESET__`（gate 兜底/重规划）会把已发文本清掉
     ⇒ 块必须跟着重发。顺序错了在界面上只是"那段解释跑到了事实前面"，没人会报 bug；
  ③ **复述的判据**（gate 的 `action_restate` 网）：模型仍作完成式声称时**只记不判**
     （计数落 trace，正文照常放行）——这条网唯一的产出是"纪律 23 达没达标"的数据，
     所以它必须**抓得准**：错抓成 fallback 会连同已发的 `__CMD__` 一起被 RESET 清掉
     （页面没跳却说已跳，实测三条动作族 golden），漏抓则数据虚高、看着像达标。
     护栏因此是双向的：6 个必拦 + 6 个必放，外加一条"命中也不许动正文"。

本套件分三节：纯函数（①② 的分类/渲染/拼接）、真图（③ 的判据与提示词接线）、
假 producer（② 的时序与 RESET 重发）。**真图那一节跑的是 build_graph() 的真图**
（假 LLM + 假动作工具，零网络零真写），理由与 `test_gate_replan.py` 相同：判据在
节点里，纯函数级断言证明不了它真的接上了（"能力有测试 ≠ 接线有测试"）。

用法：.venv/bin/python tests/test_factblock.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

# 出厂档钉子（与 `tests/run_all.py` 的 `_PINNED` 同值）：本机 `.env` 是产线那份
# （20260927 起 `PLANNER_ENGINE=native`），而假 LLM 桩没有 `bind_tools` ⇒ 单跑本套件
# 会在 planner 里 AttributeError。**必须在 import `agent.graph` 之前设**（settings 在
# 那一刻构造）。run_all 下是同值覆盖，等于没设。
os.environ.setdefault("PLANNER_ENGINE", "text")
os.environ.setdefault("AGENT_TASK_STATE", "0")

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from agent.factblock import (  # noqa: E402
    FACT_MARK, FAMILY_CMD, FAMILY_DATA, FAMILY_WRITE, action_facts, block_of,
    compose, family_of, is_action_family, is_block_family, render_fact_block,
    strip_fact_lines,
)
from agent.graph import build_graph, graph_input  # noqa: E402
from agent.principal import Principal  # noqa: E402
from tools import base as _base  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 回执行形状（与 `execute_node` 的构造同形；`cmd` 非空 = 命令族）────────────
_RCPT_CMD = {"skill": "effect", "tool": "toggle_effect", "args": {"effect": "sakura"},
             "result": "特效 樱花(sakura) 已打开",
             "cmd": {"kind": "effect", "effect": "sakura", "action": "on"}, "ts": 0}
_RCPT_WRITE = {"skill": "admin_notes", "tool": "create_tag", "args": {"name": "音乐"},
               "result": "标签「音乐」已创建", "ts": 0}
_RCPT_WRITE2 = {"skill": "admin_notes", "tool": "update_tag", "args": {"name": "音乐"},
                "result": "标签「音乐」已改名「歌单」", "ts": 0}
_RCPT_DATA = {"skill": "content_query", "tool": "search_notes", "args": {"keyword": "x"},
              "result": '[{"id": 46, "title": "架构"}]', "ts": 0}


# ── ① 分类：射程只有命令族 + 写族（数据族刻意不收）───────────────────────
def test_family_of():
    print("\n[分类] 射程＝命令族 + 写族（照 JSON 讲人话是模型的活，D3 不碰）")
    check("回执带 cmd → 命令族（与工具名无关）",
          family_of("toggle_effect", True) == FAMILY_CMD)
    check("写工具名前缀 → 写族", family_of("create_tag", False) == FAMILY_WRITE)
    check("其余 → 数据族", family_of("search_notes", False) == FAMILY_DATA)
    check("空工具名 + 无 cmd → 数据族（分不出别乱收，保守方向）",
          family_of("", False) == FAMILY_DATA)
    check("is_action_family 与 family_of 同判据",
          is_action_family(_RCPT_CMD) and is_action_family(_RCPT_WRITE)
          and not is_action_family(_RCPT_DATA))
    # 写族前缀表的**唯一实现**在 `agent/factblock.py`：这里钉住它的覆盖面，
    # 免得改名（如 create_* → add_*）之后写族静默缩水、射程跟着变。
    check("写族前缀表认得住既有写工具（create/update/set/delete/move/audit/freeze/send…）",
          all(family_of(n, False) == FAMILY_WRITE for n in (
              "create_tag", "update_tag", "delete_tag", "set_article_status",
              "audit_board_comment", "freeze_account", "unfreeze_account",
              "send_user_notice", "complete_dashboard_todo", "add_favorite",
              "remove_favorite", "move_tag")))


def test_action_facts():
    print("\n[取事实] 只取写族、去重、保序、跳过错误帧")
    got = action_facts([_RCPT_CMD, _RCPT_DATA, _RCPT_WRITE])
    check("命令族不进事实块（效果在主人眼前，那句话归泠月自己说）",
          got == [_RCPT_WRITE["result"]], str(got))
    check("  数据族也不进（它返回的是 JSON）",
          _RCPT_DATA["result"] not in got, str(got))
    check("  **分类没变**：命令族仍是动作族，只是不印（改的是射程不是分族）",
          is_action_family(_RCPT_CMD) and not is_block_family(_RCPT_CMD))
    check("纯命令轮 ⇒ 一行都不印（事实块整个不发生，正文从 narrator 开始）",
          action_facts([_RCPT_CMD]) == [] and block_of([_RCPT_CMD]) == "")
    check("顺序＝执行顺序（receipts 是累计语义）",
          action_facts([_RCPT_WRITE, _RCPT_WRITE2]) == [_RCPT_WRITE["result"], _RCPT_WRITE2["result"]])
    check("同一次动作重复执行只印一行（主人不该读到三行一样的话）",
          action_facts([_RCPT_WRITE, _RCPT_WRITE, _RCPT_WRITE]) == [_RCPT_WRITE["result"]])
    check("__ERROR__ 不是事实",
          action_facts([{**_RCPT_WRITE, "result": "__ERROR__: 未知工具"}]) == [])
    check("空 result 不算一行",
          action_facts([{**_RCPT_WRITE, "result": "   "}]) == [])
    check("形状不对的项跳过，不抛",
          action_facts([None, "x", 42, _RCPT_WRITE]) == [_RCPT_WRITE["result"]])
    check("None/空列表安全", action_facts(None) == [] and action_facts([]) == [])


def test_render_and_compose():
    print("\n[渲染/拼接] 事实一字不改、行首盖说话人标记；块在前、幂等")
    check("渲染**不改事实一个字**，只盖行首标记（说话人，不是标签）",
          render_fact_block(["页面已跳转：https://a/b", "特效 樱花(sakura) 已打开"])
          == FACT_MARK + "页面已跳转：https://a/b\n" + FACT_MARK + "特效 樱花(sakura) 已打开")
    check("  已经盖过标记的行不重复盖（幂等）",
          render_fact_block([FACT_MARK + "x"]) == FACT_MARK + "x")
    check("块 = action_facts + render 的组合壳",
          block_of([_RCPT_WRITE]) == FACT_MARK + _RCPT_WRITE["result"])
    # 标记只盖在**印出来的那一份**上：工具回执（模型看到的证据）一字未动——
    # `tools/base.py` 那行 result 是模型的判据（graph.py 的"无前缀中文事实"注释）
    check("回执原文一字未动（模型的证据不带标记，标记只在给人读的那份上）",
          _RCPT_WRITE["result"] == "标签「音乐」已创建"
          and render_fact_block(action_facts([_RCPT_WRITE])) == FACT_MARK + _RCPT_WRITE["result"])
    check("  命令族的回执**照样一字未动**（它只是不印了，模型看到的证据不变）",
          _RCPT_CMD["result"] == "特效 樱花(sakura) 已打开"
          and action_facts([_RCPT_CMD]) == [])
    check("块在前、空行分隔（单换行会被 markdown 并成一句）",
          compose("事实行", "包装文字") == "事实行\n\n包装文字")
    check("正文已以块开头 ⇒ 不重复印（gate 兜底的替代文本**就是**块）",
          compose("事实行", "事实行") == "事实行")
    check("块是正文的前缀时也不重复（块已含两行、正文是整块）",
          compose("行一\n行二", "行一\n行二\n\n包装") == "行一\n行二\n\n包装")
    check("没有块 ⇒ 正文原样（闲聊/数据轮零影响）", compose("", "闲聊") == "闲聊")
    check("正文为空 ⇒ 只剩块", compose("事实行", "") == "事实行")
    check("全空 ⇒ 空串", compose("", "") == "")


def test_strip_fact_lines():
    print("\n[剥离] 系统印的事实行不进模型语境（按行首标记，不靠形状猜）")
    block = render_fact_block(["页面已跳转：https://a/b", "特效 樱花(sakura) 已打开"])
    check("整段都是事实行 ⇒ 抹成空串（调用方据此整轮跳过）",
          strip_fact_lines(block) == "")
    check("  纯事实行**不残留空行**（会变成一条空的 assistant）",
          strip_fact_lines(block + "\n\n") == "")
    check("事实行 + 泠月的包装 ⇒ 只剩包装（块在前）",
          strip_fact_lines(block + "\n\n要我读一下这篇吗？") == "要我读一下这篇吗？")
    check("包装在块中间也照抹（块不总在最前：RESET 重印/兜底那两条来路）",
          strip_fact_lines("前半句\n" + FACT_MARK + "页面已跳转：https://a/b\n后半句")
          == "前半句\n后半句")
    check("没有标记的行一个字不动（闲聊/数据轮零影响）",
          strip_fact_lines("已经帮你打开啦～\n页面已跳转：https://a/b")
          == "已经帮你打开啦～\n页面已跳转：https://a/b")
    check("空/None 安全", strip_fact_lines("") == "" and strip_fact_lines(None) == "")
    # 反向哨兵：**模型自己写**的"页面已跳转：…"（无标记）留在原地——剥离只认标记，
    # 这就是标记存在的理由（按形状猜"哪句像系统印的"会把模型的真话也抹掉）
    check("无标记的同形句不被误抹（判据是标记，不是这行长得像事实）",
          strip_fact_lines("页面已跳转：https://a/b") == "页面已跳转：https://a/b")


def test_injection_points():
    """接线：三处模型可见的读点都真的过了一遍剥离（能力有测试 ≠ 接线有测试）。"""
    print("\n[接线] 历史注入点与 planner 读点都剥离（生产实证 20261001T230954）")
    block = render_fact_block(["页面已跳转：https://saudade.site/device-console/"])
    from agent.context import _last_assistant_utterance, _recent_tail

    tail = _recent_tail([HumanMessage(content="猫咪我们去物联网平台"),
                         AIMessage(content=block + "\n\n控制台就在眼前啦～")])
    check("_recent_tail 的「泠月：」那半不带事实行（节选是范文，抄过去的就是这行）",
          "页面已跳转" not in tail and "控制台就在眼前啦" in tail, tail[-80:])
    tail2 = _recent_tail([HumanMessage(content="在物联网设备上对我说些什么"),
                          AIMessage(content=block)])
    check("  整条回复只有事实行 ⇒ 该轮「泠月：」是（未及回复），不是那行事实",
          "页面已跳转" not in tail2, tail2[-80:])
    check("_last_assistant_utterance 同样的剥离",
          _last_assistant_utterance([HumanMessage(content="嗯"),
                                     AIMessage(content=block + "\n\n还要做什么？")])
          == "还要做什么？")
    check("  最近一条**只有**事实行 ⇒ 给空、**不往前捞**一条更早的发言当上一句"
          "（20260923 事故的形状：拿历史里的旧事项当此刻的应答对象）",
          _last_assistant_utterance([HumanMessage(content="甲"),
                                     AIMessage(content="要我把 OTA 章节读一遍吗？"),
                                     HumanMessage(content="嗯"),
                                     AIMessage(content=block)]) == "")

    import server  # 只在用到时 import（server import 会拉起 FastAPI 应用）
    hist = [server.HistoryItem(role="user", content="猫咪我们去物联网平台"),
            server.HistoryItem(role="assistant", content=block + "\n\n控制台就在眼前啦～"),
            server.HistoryItem(role="user", content="在物联网设备上对我说些什么"),
            server.HistoryItem(role="assistant", content=block)]
    msgs = server._build_messages(server.ChatRequest(message="现在呢", history=hist))
    ai = [str(m.content) for m in msgs if isinstance(m, AIMessage)]
    check("_build_messages 注入的 assistant 历史里没有事实行",
          ai and all("页面已跳转" not in a for a in ai), str(ai))
    check("  只有事实行的那一轮**整轮不注入**（留一条空的 assistant 会让模型"
          "把上一轮的用户问题当待答问题——孤儿 user 的镜像）",
          len(ai) == 1 and "控制台就在眼前啦" in ai[0], str(ai))
    check("  它的 user 侧也一起不注入（成对，不留孤儿）",
          all("在物联网设备上对我说些什么" not in str(m.content) for m in msgs if isinstance(m, HumanMessage)),
          str([str(m.content)[:20] for m in msgs if isinstance(m, HumanMessage)]))


def test_share_one_classifier():
    print("\n[同源] 量化脚本与射程共用同一份分类（两处各写一份必然漂移）")
    import eval.narrator_facts_share as nfs  # noqa: E402
    from agent import factblock as fb  # noqa: E402
    check("量化脚本 import 的就是 agent.factblock 的 family_of",
          nfs.family_of is fb.family_of)
    check("量化脚本不再自带一份写族正则（重复实现就是漂移的起点）",
          not hasattr(nfs, "_WRITE_PREFIX_RE"))
    check("族常量同源（报告里的分档与射程同集合）",
          {fb.FAMILY_CMD, fb.FAMILY_WRITE, fb.FAMILY_DATA}
          == {nfs.FAMILY_CMD, nfs.FAMILY_WRITE, nfs.FAMILY_DATA})


# ── ③ 判据：动作族轮次的复述式声称（gate 5g）───────────────────────────────
_RESTATE_HITS = [
    "已经帮你打开啦～",                      # 施事前缀 + 完成态（旧网也能抓）
    "页面也跳转过去啦",                      # 无施事：旧网的①支抓不到（新增的那半）
    "标签「音乐」已经建好了哦",               # 写族 + 完成态
    "樱花特效已经打开了",                     # 时间副词 + 动词（①支要施事，这里不要）
    "通知也发送完成",                         # 完成态的另一形态
    "那条留言我驳回掉了",                     # 写族（审核）
]
_RESTATE_PASS = [
    "樱花特效现在是开启状态",                 # 状态陈述：幂等轮的正确答案（不带完成标记）
    "已经打开的樱花会一直飘",                 # 定语用法：描述状态不是声称动作
    "要不要我帮你把夜间模式也关掉呢？",         # 提议（豁免：要不要/呢/？）
    "要是跳过去了，应该能直接看到留言板",        # 假设（豁免：要是）
    "这一轮什么都没做，因为还没确认",           # 否定（豁免：没）
    "页面已跳转：https://saudade.site/talk",  # 系统块原文**不是**模型的话（判据扫的是叙述，
    FACT_MARK + "页面已跳转：https://saudade.site/talk",  # 20261002 起它带说话人标记）
]


def test_action_restate_regex():
    print("\n[判据] 完成式复述抓得住；状态陈述/提议/假设放行")
    for s in _RESTATE_HITS:
        check("拦：%s" % s,
              bool(g._clause_hit(s, g._ACTION_RESTATE_RE, g._STATE_ACTION_EXEMPT_RE)))
    for s in _RESTATE_PASS:
        check("放：%s" % s,
              g._clause_hit(s, g._ACTION_RESTATE_RE, g._STATE_ACTION_EXEMPT_RE) is None)


# ── ③ 真图：提示词接线 + 兜底替代文本 ──────────────────────────────────────
class _ScriptedLLM:
    """按入参形态分流 planner / model（同 `test_gate_replan.py` 的理由）。"""

    def __init__(self, plans: list, narrations: list):
        self.plans, self.narrations = list(plans), list(narrations)
        self.model_prompts: list = []
        self.exhausted: list = []

    def invoke(self, prompt):
        if isinstance(prompt, list):
            self.model_prompts.append(prompt)
            if not self.narrations:
                self.exhausted.append("model")
                return AIMessage(content="（脚本用尽）")
            return AIMessage(content=self.narrations.pop(0))
        if not self.plans:
            self.exhausted.append("planner")
            return AIMessage(content="SKILL: chat\nPARAMS: {}")
        return AIMessage(content=self.plans.pop(0))


class _FakeTool:
    def __init__(self, result, name="toggle_effect"):
        self.name = name
        self.result = result
        self.calls: list = []

    def invoke(self, args):
        self.calls.append(args)
        return self.result


_PLAN_EFFECT = 'SKILL: effect\nPARAMS: {"effect": "sakura", "action": "on"}'
_PLAN_CHAT = "SKILL: chat\nPARAMS: {}"
_FACT_LINE = "特效 樱花(sakura) 已打开"          # 命令族：**不印**（20261002）
_FACT_BLOCK = FACT_MARK + _FACT_LINE

# 写族（**系统印这一族**）——两笔夹具都用它，理由见 `agent/factblock.py` 的
# `BLOCK_FAMILIES`：`add_favorite` 是写族里最轻的一件（自己的数据、不弹卡、无管理
# 角色要求、不需要确认令牌），离线真图上跑得通；`create_tag` 走 `_ALWAYS_CONFIRM_TOOLS`
# ⇒ 测试环境没有 jwt_secret 时它连工具都到不了（会停在 consent_required）。
_PLAN_FAV = 'SKILL: favorite_add\nPARAMS: {"article_id": 46}'
_FAV_MSG = "把文章 46 收藏一下"
_FACT_WRITE_LINE = "已收藏文章 46"
_FACT_WRITE_BLOCK = FACT_MARK + _FACT_WRITE_LINE


def _run_graph(plans, narrations, result=None, tool_name="toggle_effect",
               message="把樱花打开", role=None):
    """跑一轮**真图**（不是假 producer）。两个夹具形态由 `tool_name` 决定：

    · `toggle_effect`（命令族）——验"不印但帧还在"（20261002 的新契约）；
    · `add_favorite`（写族）——验"印了、且族内帧从两个记录段摘掉"。
    """
    llm = _ScriptedLLM(plans, narrations)
    if tool_name == "toggle_effect":
        default = _base.ok(
            _FACT_LINE, {"cmd": {"kind": "effect", "effect": "sakura", "action": "on"}})
    else:
        default = _base.ok(_FACT_WRITE_LINE)
    tool = _FakeTool(result or default, name=tool_name)
    events: list = []
    orig_llm, orig_record, orig_tool = g.get_llm, g.record, g._TOOL_MAP.get(tool_name)
    g.get_llm = lambda **kw: llm
    g.record = lambda node, event, **data: events.append((node, event, data))
    g._TOOL_MAP[tool_name] = tool
    try:
        cfg = {"configurable": {"thread_id": "t-factblock", "user_id": 5,
                                "principal": Principal(uid=5) if role is None
                                else Principal(uid=5, role=role),
                                "conversation_id": 1, "stop_event": None}}
        out = build_graph().invoke(graph_input([HumanMessage(content=message)]), cfg)
    finally:
        g.get_llm, g.record = orig_llm, orig_record
        if orig_tool is None:
            g._TOOL_MAP.pop(tool_name, None)
        else:
            g._TOOL_MAP[tool_name] = orig_tool
    return out, llm, tool, events


def _system_prompt(llm: "_ScriptedLLM") -> str:
    """最后一轮 narrator 的 system 提示词（model 传的是 [system] + messages）。"""
    return str(llm.model_prompts[-1][0].content)


def test_graph_command_round_not_printed():
    """**20261002 的新契约**（主人拍板：命令族不印）：一轮真跳了页/开了特效的对话——

    ① 系统**不印**那一行（气泡里只有泠月的话）；② 但工具帧与回执**一个字都不摘**
    （`_drop` 由 `is_block_family` 算，不是 `is_action_family`）：那是 narrator 唯一的
    依据，它得自己把这件事说出来；③ 那一格是**占位文本**而不是"本轮没有动作族执行"
    ——后者在一轮真的执行过的对话里是句假话，会把 narrator 引到"什么都没干"；
    ④ 5g（复述式声称）**不再命中这一族**：系统不印了，"那句话"就该由泠月说，
    再罚它就是罚它去做被要求的事。
    """
    print("\n[真图·命令族] 不印、但帧照给；那句话归 narrator 自己说")
    out, llm, tool, events = _run_graph([_PLAN_EFFECT, _PLAN_CHAT], ["已经帮你打开啦～"])
    check("脚本足够跑完这一轮（没有靠「脚本用尽」混过去）",
          llm.exhausted == [], str(llm.exhausted))
    check("动作工具真的执行了一次", len(tool.calls) == 1, str(tool.calls))
    sys_p = _system_prompt(llm)
    check("**没有** model/fact_block 事件（命令族不进印出射程）",
          ("model", "fact_block") not in [(n, e) for n, e, _ in events],
          str([(n, e) for n, e, _ in events]))
    check("  印出那一格是**占位**，且占位写的是「没有代印」不是「没有执行」",
          "本轮没有系统代印的事实" in sys_p
          and "本轮没有动作族执行" not in sys_p)
    check("  占位里点名了「那件事由你自己说」（否则模型以为系统还会说一遍）",
          "那件事由你自己说" in sys_p)
    check("工具帧**照给**（没被摘掉：它是 narrator 唯一的依据）",
          _FACT_LINE in sys_p and "本轮这些工具返回已由系统印给主人" not in sys_p,
          str([ln for ln in sys_p.splitlines() if _FACT_LINE in ln]))
    check("  回执段的兜底句也没被触发（回执原样在提示词里）",
          "本轮已验收的执行都已由系统印给主人" not in sys_p)
    check("5g 不再对命令族记事件（系统不印了，那句话本就该泠月说）",
          ("gate", "action_restate") not in [(n, e) for n, e, _ in events],
          str([(n, e) for n, e, _ in events]))
    check("最终回复就是 narrator 那句（没被替换）",
          str(out["messages"][-1].content).strip() == "已经帮你打开啦～",
          repr(str(out["messages"][-1].content)[:40]))


def test_graph_wiring():
    print("\n[真图·写族] 事实块进提示词、族内帧从记录段摘掉、复述只记不判")
    out, llm, tool, events = _run_graph(
        [_PLAN_FAV, _PLAN_CHAT], ["已经帮你收藏啦～"],
        tool_name="add_favorite", message=_FAV_MSG)
    check("脚本足够跑完这一轮（没有靠「脚本用尽」混过去）",
          llm.exhausted == [], str(llm.exhausted))
    check("写工具真的执行了一次（真回执，不是弹窗截停）",
          len(tool.calls) == 1 and [r.get("tool") for r in out.get("receipts") or []]
          == ["add_favorite"], str(tool.calls))
    sys_p = _system_prompt(llm)
    check("提示词里有 [本轮已由系统印出的事实] 段与那行事实",
          "[本轮已由系统印出的事实]" in sys_p and _FACT_WRITE_LINE in sys_p)
    check("  系统明说那几行**已经印在气泡最前面**（否则模型会以为主人没看到、去复述）",
          "已经印在气泡最前面" in sys_p)
    check("族内事实从 [本轮工具执行记录] 摘掉了（同一份事实出现两次＝邀请复述）",
          "[本轮工具执行记录]" in sys_p
          and "本轮这些工具返回已由系统印给主人" in sys_p)
    check("  也从 [本轮执行回执] 摘掉了",
          "本轮已验收的执行都已由系统印给主人" in sys_p)
    check("纪律 23 在场且写明「不限长度，只限内容」",
          "不限长度，只限内容" in sys_p)
    check("trace 有 model/fact_block 事件（判据可回溯）",
          any(n == "model" and e == "fact_block" for n, e, _ in events),
          str([(n, e) for n, e, _ in events]))
    check("复述被记下来了（gate.action_restate，soft=True）",
          any(n == "gate" and e == "action_restate" and d.get("soft") is True
              for n, e, d in events),
          str([(n, e) for n, e, _ in events]))
    # **这一节是 20260927 实测改口的锁**：这条网原先是 fallback，跑动作族 golden 时
    # 三条被它命中、三条都因为 RESET 连命令一起清而"页面没跳却说已跳"。所以断言从
    # "兜底文本是事实块"改成"正文一个字都不许动"——想改回 fallback 的人，先看
    # gate 5g 那段注释里的三条 trace。
    # ⚠️ 那个**代价**（RESET 吞命令）20261001 起不成立了（RESET 带 scope，见
    # `tests/test_reset_scope.py`），但这条断言**照旧**：剩下的理由是"罚得不对"，
    # 而要不要升回 fallback 动的是闸门严重度，判据只能靠多遍 A/B——别把"前提没了"
    # 读成"这条锁该拆"。
    check("  正文**一字未动**（不是 fallback：拿文案去换命令是这一批最贵的错）",
          not out.get("fallback_text")
          and str(out["messages"][-1].content).strip() == "已经帮你收藏啦～",
          repr(str(out["messages"][-1].content)[:40]))
    check("  不进 _REPLAN_ISSUES（重规划会把副作用工具再跑一遍）", "action_restate" not in g._REPLAN_ISSUES)
    check("  没走重规划（gate_replan 未被置真）", not out.get("gate_replan"))


def test_graph_no_false_positive():
    print("\n[真图] 非完成式包装照常通过（零回归：这条网不是「一律兜底」）")
    wrapper = "这次的动作用的是页面特效那一档，想换别的风格随时说～"
    out, llm, tool, events = _run_graph([_PLAN_EFFECT, _PLAN_CHAT], [wrapper])
    check("脚本足够跑完这一轮", llm.exhausted == [])
    check("执行了一次", len(tool.calls) == 1)
    check("没有 action_restate（它没说完成式）",
          ("gate", "action_restate") not in [(n, e) for n, e, _ in events])
    check("最终回复就是那段包装（没被替换）",
          str(out["messages"][-1].content).strip() == wrapper,
          repr(str(out["messages"][-1].content)[:40]))
    check("同时没有走兜底", not out.get("fallback_text"))


def test_graph_zero_frame_no_authorization():
    """**20261002 二改的锁**：零命令轮的占位**不发授权、不点族名**（02:02 实证）。

    现场（trace 20261002T020256）：主人「猫咪带我去你的设计文档」（站内无此页），
    planner 判 `chat`/`answer_only`（零帧零回执），narrator 编出「物联网平台页面已经
    打开啦～你现在应该能看到设备控制台了」——而同一轮 `page_ctx` 的 `current_url` 是
    首页。占位文本当时写着"跳转/特效/夜间那几种的效果……**那几句话由你自己说**"，
    那是系统在**没有那一族回执**的轮次里发的一张空授权：模型拿着它去认领了一件根本
    没发生的事。所以占位必须分岔（有命令族回执才点名那一族、才授权；没有就只报
    "没有代印"）——这条锁与 `test_graph_command_round_not_printed` 是**方向相反的两半**，
    缺一半都会退回"占位是常量、写死一句话"。
    """
    print("\n[真图·零命令轮] 占位不点族名、不发「由你自己说」的授权")
    _out, llm, tool, _events = _run_graph([_PLAN_CHAT], ["站内没有这个页面呀～"])
    check("这一轮零工具（chat 不执行任何东西）", len(tool.calls) == 0, str(tool.calls))
    sys_p = _system_prompt(llm)
    # 族名只许在**那一格**里查：叙述纪律第 1 条本身就写着"站内查询、跳转、特效/夜间
    # 切换"，全提示词 grep 会恒真（那才是这条断言最容易写成哑判据的地方）。取
    # **最后一次**出现：纪律 23 正文里也引用了这个槽名，split 第一次会切到纪律那段。
    _slot = sys_p.rsplit("[本轮已由系统印出的事实]", 1)[1].split("当前页面上下文")[0]
    check("那一格是占位（不是空字段）", "本轮没有系统代印的事实" in _slot)
    check("  **一个族名都不提**（提了就是邀请它去认领一件没发生的事）",
          all(w not in _slot for w in ("跳转", "特效", "夜间")), repr(_slot[:80]))
    check("  **不发**「由你自己说」这条授权（那一轮没有该由它说的事）",
          "由你自己说" not in _slot, repr(_slot[:80]))


def test_graph_data_round_untouched():
    print("\n[真图] 数据族轮次不产生事实块（讲 JSON 人话是模型的活）")
    plan = ('SKILL: content_query\nPARAMS: {"calls": [{"tool": "search_notes", '
            '"args": {"keyword": "架构"}}]}')
    llm = _ScriptedLLM([plan], ["站内检索到 1 篇讲架构的文章。"])
    tool = _FakeTool(_base.ok('[{"id": 46, "title": "架构"}]'))
    tool.name = "search_notes"
    events: list = []
    orig_llm, orig_record = g.get_llm, g.record
    orig_tool = g._TOOL_MAP.get("search_notes")
    g.get_llm, g.record = lambda **kw: llm, lambda node, event, **data: events.append((node, event, data))
    g._TOOL_MAP["search_notes"] = tool
    try:
        cfg = {"configurable": {"thread_id": "t-factblock-data", "user_id": 5,
                                "principal": Principal(uid=5),
                                "conversation_id": 1, "stop_event": None}}
        out = build_graph().invoke(graph_input([HumanMessage(content="有讲架构的文章吗")]), cfg)
    finally:
        g.get_llm, g.record = orig_llm, orig_record
        if orig_tool is None:
            g._TOOL_MAP.pop("search_notes", None)
        else:
            g._TOOL_MAP["search_notes"] = orig_tool
    sys_p = _system_prompt(llm)
    check("提示词那一格是占位文本（不是空字段）",
          "本轮没有系统代印的事实" in sys_p)
    # 同一条分岔的另一半（20261002 二改）：这一轮有**数据族**帧、没有命令族回执 ⇒
    # 占位**不许**点名跳转/特效/夜间、也不许发"由你自己说"的授权。
    check("  数据轮同样不发授权、不点族名",
          all(w not in sys_p.rsplit("[本轮已由系统印出的事实]", 1)[1]
                  .split("当前页面上下文")[0]
              for w in ("跳转", "特效", "夜间", "由你自己说")))
    check("  数据帧**没有**被摘掉（记录段照常给模型）",
          "本轮这些工具返回已由系统印给主人" not in sys_p)
    check("没有 model/fact_block 事件", ("model", "fact_block") not in [(n, e) for n, e, _ in events])
    check("叙述照常通过（没有 action_restate）",
          ("gate", "action_restate") not in [(n, e) for n, e, _ in events]
          and not out.get("fallback_text"))
    check("最终回复就是那条叙述", str(out["messages"][-1].content).strip() == "站内检索到 1 篇讲架构的文章。")


# ── ② 假 producer：时序（块先于 narrator）与 RESET 重发 ────────────────────
class _FakeAgent:
    """假 `_agent`：按脚本产出 (mode, data)，让 producer 的时序完全可测。"""

    def __init__(self, script: list):
        self.script = script

    def stream(self, inp, cfg, stream_mode=None):
        for item in self.script:
            yield item


# 假 producer 用的回执行：**写族**（20261002 起只有写族会真印出块）。
# 命令族的行仍然可以进 receipts（台账/跨轮记忆照旧），但它渲染出来是空串 ——
# `test_producer_no_facts_for_cmd_round` 专门钉这条。
_ROW = {"skill": "favorite_add", "tool": "add_favorite",
        "args": {"article_id": 46},
        "result": _FACT_WRITE_LINE, "ts": 0}
_ROW_CMD = {"skill": "effect", "tool": "toggle_effect",
            "args": {"effect": "sakura", "action": "on"},
            "result": _FACT_LINE, "ts": 0,
            "cmd": {"kind": "effect", "effect": "sakura", "action": "on"}}


def _drain(script: list) -> list:
    import server  # 只在用到时 import（server import 会拉起 FastAPI 应用）
    orig = server._agent
    server._agent = _FakeAgent(script)
    try:
        async def run():
            q: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            t = threading.Thread(
                target=server._run_agent_stream_to_queue,
                args=([], "t-producer", q, loop, 1),
                kwargs={"principal": Principal(uid=1)},
                daemon=True)
            t.start()
            out = []
            while True:
                item = await asyncio.wait_for(q.get(), timeout=10)
                if item is None:
                    break
                out.append(item)
            t.join(timeout=5)
            return out
        return asyncio.run(run())
    finally:
        server._agent = orig


def _ai_chunks(items: list) -> list:
    return [str(i.content) for i in items if isinstance(i, AIMessageChunk)]


def test_producer_order_and_reset():
    print("\n[真 producer] 事实块先于 narrator 的文本；RESET 之后重发（不丢事实）")
    script = [
        ("updates", {"execute": {"receipts": [_ROW]}}),
        ("messages", (AIMessageChunk(content="已经帮你收藏好啦～"),
                      {"langgraph_node": "model"})),
        ("updates", {"model": {"messages": [AIMessage(content="已经帮你收藏好啦～")]}}),
        ("updates", {"gate": {"done": True, "fallback_text": _FACT_WRITE_BLOCK,
                              "gate_replan": False}}),
    ]
    items = _drain(script)
    chunks = _ai_chunks(items)
    check("三段 AI 文本：块 → narrator（它确实先流出去了）→ 兜底替换文本",
          chunks == [_FACT_WRITE_BLOCK + "\n\n", "已经帮你收藏好啦～", _FACT_WRITE_BLOCK],
          str(chunks))
    check("事实块在 narrator 之前（主人先读事实）",
          chunks and chunks[0] == _FACT_WRITE_BLOCK + "\n\n",
          repr(chunks[0] if chunks else ""))
    check("兜底那一帧**没有把块印两遍**（compose 幂等：替代文本就是块）",
          chunks[-1] == _FACT_WRITE_BLOCK
          and chunks[-1].count(_FACT_WRITE_LINE) == 1, repr(chunks[-1]))
    check("__CMD__ 帧在事实块之前（机器读的命令与给人读的事实各就各位）",
          next((i for i, x in enumerate(items) if isinstance(x, str)
                and x.startswith("__CMD__:")), -1)
          < next((i for i, x in enumerate(items)
                  if isinstance(x, AIMessageChunk)), -1),
          str(items))
    _reset = next(i for i, x in enumerate(items) if isinstance(x, str)
                  and x.startswith("__RESET__"))
    _last_ai = max(i for i, x in enumerate(items) if isinstance(x, AIMessageChunk))
    check("RESET 在兜底文本之前（前端先清空再重绘，否则被否定的叙述留在界面上）",
          _reset < _last_ai, f"reset=#{_reset} last_ai=#{_last_ai}")


def test_producer_pass_round():
    print("\n[真 producer] 通过的一轮：块 + 包装都在，顺序不变")
    script = [
        ("updates", {"execute": {"receipts": [_ROW]}}),
        ("messages", (AIMessageChunk(content="想收别的随时说～"),
                      {"langgraph_node": "model"})),
        ("updates", {"model": {"messages": [AIMessage(content="想收别的随时说～")]}}),
        ("updates", {"gate": {"done": True, "gate_replan": False}}),
    ]
    chunks = _ai_chunks(_drain(script))
    check("两段：块（带空行）+ 包装",
          chunks == [_FACT_WRITE_BLOCK + "\n\n", "想收别的随时说～"], str(chunks))
    check("数据族回执不发块（只印写族）",
          _ai_chunks(_drain([
              ("updates", {"execute": {"receipts": [_RCPT_DATA]}}),
              ("messages", (AIMessageChunk(content="查到 1 篇。"),
                            {"langgraph_node": "model"})),
              ("updates", {"gate": {"done": True, "gate_replan": False}}),
          ])) == ["查到 1 篇。"])
    check("**命令族回执也不发块**（20261002：效果主人当场看得见，那句话归泠月）",
          _ai_chunks(_drain([
              ("updates", {"execute": {"receipts": [_ROW_CMD]}}),
              ("messages", (AIMessageChunk(content="这就带你过去～"),
                            {"langgraph_node": "model"})),
              ("updates", {"gate": {"done": True, "gate_replan": False}}),
          ])) == ["这就带你过去～"])
    check("  但命令帧照发（机器读的那一半不受印量影响）",
          any(isinstance(i, str) and i.startswith("__CMD__:") for i in _drain([
              ("updates", {"execute": {"receipts": [_ROW_CMD]}}),
              ("messages", (AIMessageChunk(content="这就带你过去～"),
                            {"langgraph_node": "model"})),
              ("updates", {"gate": {"done": True, "gate_replan": False}}),
          ])))


if __name__ == "__main__":
    for fn in (test_family_of, test_action_facts, test_render_and_compose,
               test_strip_fact_lines, test_injection_points,
               test_share_one_classifier, test_action_restate_regex,
               test_graph_command_round_not_printed,
               test_graph_wiring, test_graph_no_false_positive,
               test_graph_zero_frame_no_authorization,
               test_graph_data_round_untouched,
               test_producer_order_and_reset, test_producer_pass_round):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
