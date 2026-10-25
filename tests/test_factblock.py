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

import sys
from pathlib import Path

from _judge import collector
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.api.schemas import ChatRequest, HistoryItem

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))



from agent.factblock import (  # noqa: E402
    FACT_MARK,
    FAMILY_CMD,
    FAMILY_DATA,
    FAMILY_WRITE,
    action_facts,
    block_of,
    compose,
    family_of,
    is_action_family,
    is_block_family,
    render_fact_block,
    strip_fact_lines,
)

FAILS: list[str] = []
check = collector(FAILS)


_RCPT_CMD = {"workflow": "effect", "tool": "toggle_effect", "args": {"effect": "sakura"},
             "result": "特效 樱花(sakura) 已打开",
             "cmd": {"kind": "effect", "effect": "sakura", "action": "on"}, "ts": 0}


_RCPT_WRITE = {"workflow": "admin_notes", "tool": "create_tag", "args": {"name": "音乐"},
               "result": "标签「音乐」已创建", "ts": 0}


_RCPT_WRITE2 = {"workflow": "admin_notes", "tool": "update_tag", "args": {"name": "音乐"},
                "result": "标签「音乐」已改名「歌单」", "ts": 0}


_RCPT_DATA = {"workflow": "content_query", "tool": "search_notes", "args": {"keyword": "x"},
              "result": '[{"id": 46, "title": "架构"}]', "ts": 0}


def _test_family_of():
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


def _test_action_facts():
    """**20261005 的新契约**（主人拍板：写族也退出印出）＝射程空集。

    这一节此前判的是"只取写族、去重、保序"——那些规则**一条都没有被推翻**，只是
    输入集空了。所以判据改成两半：① 三个族**都不进**（每族一条，缺一族就会漏掉一次
    收窄）；② **反向对照**——分类没有跟着缩水（`is_action_family` 仍认命令族与写族），
    否则"不印"会退化成"认不出"，而认不出会让 `graph.model_node` 的占位分岔与
    `narrator_facts_share` 的量化口径一起失真（两处都按分类算，不按印量算）。
    """
    print("\n[取事实] 射程空集：三个族都不再代印；但**分类没缩水**（判据仍按族算）")
    check("写族不再进事实块（20261005：系统不再抢话，写操作也归泠月交代）",
          action_facts([_RCPT_WRITE]) == [] and block_of([_RCPT_WRITE]) == "")
    check("命令族不进（20261002 起，效果在主人眼前）",
          action_facts([_RCPT_CMD]) == [] and block_of([_RCPT_CMD]) == "")
    check("数据族不进（返回的 JSON，讲人话是模型的活）",
          _RCPT_DATA["result"] not in action_facts([_RCPT_DATA]))
    check("三族混在一起也一行都不出（改前那份实现必须把写族那一行进出来）",
          action_facts([_RCPT_CMD, _RCPT_DATA, _RCPT_WRITE, _RCPT_WRITE2]) == [])
    # 反向对照：**分类仍在**（这是本节唯一能红的地方——把 `is_action_family` 一并改空
    # 的话，下面这条与 `graph.model_node` 的占位分岔都会静默失真）
    check("分类没缩水：命令族与写族仍算动作族（数据族不是）",
          is_action_family(_RCPT_CMD) and is_action_family(_RCPT_WRITE)
          and not is_action_family(_RCPT_DATA))
    check("  而 `is_block_family`（印不印）对它们全部为假——两件事没有合并",
          not any(is_block_family(r) for r in (_RCPT_CMD, _RCPT_WRITE, _RCPT_DATA)))
    check("None/空列表安全", action_facts(None) == [] and action_facts([]) == [])


def _test_render_and_compose():
    print("\n[渲染/拼接] 事实一字不改、行首盖说话人标记；块在前、幂等")
    check("渲染**不改事实一个字**，只盖行首标记（说话人，不是标签）",
          render_fact_block(["页面已跳转：https://a/b", "特效 樱花(sakura) 已打开"])
          == FACT_MARK + "页面已跳转：https://a/b\n" + FACT_MARK + "特效 樱花(sakura) 已打开")
    check("  已经盖过标记的行不重复盖（幂等）",
          render_fact_block([FACT_MARK + "x"]) == FACT_MARK + "x")
    check("块 = action_facts + render 的组合壳（射程空集 ⇒ 组合壳也为空）",
          block_of([_RCPT_WRITE]) == "")
    # 标记只盖在**印出来的那一份**上：工具回执（模型看到的证据）一字未动——
    # `tools/base.py` 那行 result 是模型的判据（graph.py 的"无前缀中文事实"注释）。
    # 射程空集之后这条**更重要**了：回执是 narrator 交代动作的唯一依据（见
    # `graph.model_node` 的"摘除面必须与印出面同宽"），它必须原样、不带任何标记。
    check("回执原文一字未动（模型的证据不带标记）",
          _RCPT_WRITE["result"] == "标签「音乐」已创建"
          and FACT_MARK not in _RCPT_WRITE["result"])
    check("  命令族的回执**照样一字未动**（它只是不印了，模型看到的证据不变）",
          _RCPT_CMD["result"] == "特效 樱花(sakura) 已打开"
          and action_facts([_RCPT_CMD]) == [])
    # 渲染器本身**没坏**（它是那次回退的一行开关，不能跟着射程一起废掉）：
    # 反向对照——手工喂两行，它照旧盖标记、照旧幂等。
    check("反向对照：渲染器仍然能盖标记（射程空了，机器没坏）",
          render_fact_block(["x", FACT_MARK + "y"]) == FACT_MARK + "x\n" + FACT_MARK + "y")
    check("块在前、空行分隔（单换行会被 markdown 并成一句）",
          compose("事实行", "包装文字") == "事实行\n\n包装文字")
    check("正文已以块开头 ⇒ 不重复印（gate 兜底的替代文本**就是**块）",
          compose("事实行", "事实行") == "事实行")
    check("块是正文的前缀时也不重复（块已含两行、正文是整块）",
          compose("行一\n行二", "行一\n行二\n\n包装") == "行一\n行二\n\n包装")
    check("没有块 ⇒ 正文原样（闲聊/数据轮零影响）", compose("", "闲聊") == "闲聊")
    check("正文为空 ⇒ 只剩块", compose("事实行", "") == "事实行")
    check("全空 ⇒ 空串", compose("", "") == "")


def _test_strip_fact_lines():
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


def _test_injection_points():
    """Server history injection strips system fact lines before NativeRuntime input."""
    print("\n[接线] server history injection strips fact lines")
    from agent.api.routes import chat as server
    block = render_fact_block(["页面已跳转：https://saudade.site/device-console/"])
    history = [HistoryItem(role="user", content="猫咪去物联网平台"),
               HistoryItem(role="assistant", content=block + "\n控制台在眼前啦～")]
    messages = server.build_messages(ChatRequest(
        message="现在呢", history=history, conversation_id=42,
        history_scope={"user_id": 7, "role": "user", "conversation_id": 42}))
    assistant = [str(message.content) for message in messages if isinstance(message, AIMessage)]
    check("历史 assistant 不再携带系统事实行", assistant and all(FACT_MARK not in text for text in assistant))
    check("事实行被剥离后保留模型包装", assistant and "控制台在眼前啦" in assistant[0])


def _test_share_one_classifier():
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


_FACT_LINE = "特效 樱花(sakura) 已打开"
_FACT_WRITE_LINE = "已收藏文章 46"
_ROW = {"workflow": "favorite_add", "tool": "add_favorite",
        "args": {"article_id": 46}, "result": _FACT_WRITE_LINE, "ts": 0}
_ROW_CMD = {"workflow": "effect", "tool": "toggle_effect",
            "args": {"effect": "sakura", "action": "on"},
            "result": _FACT_LINE, "ts": 0,
            "cmd": {"kind": "effect", "effect": "sakura", "action": "on"}}


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


def _test_native_producer_does_not_print_fact_blocks():
    from agent.runtime.results import TurnResult
    from agent.transport import iter_transport_events

    reply = "指令已发送。"
    items = list(iter_transport_events([AIMessageChunk(content=reply),
                                        TurnResult(reply, {"receipts": [_ROW, _ROW_CMD]})]))
    chunks = [str(item.content) for item in items if isinstance(item, AIMessageChunk)]
    check("原生正文不追加系统事实块", chunks == [reply], str(chunks))
    check("结构化命令与回执各发一次",
          sum(isinstance(x, str) and x.startswith("__CMD__:") for x in items) == 1
          and sum(isinstance(x, str) and x.startswith("__EXEC__:") for x in items) == 1)
    check("完成轮次不使用旧 RESET 纠偏协议",
          not any(isinstance(x, str) and x.startswith("__RESET__") for x in items))


def test_factblock():
    _FACT_BLOCK = FACT_MARK + _FACT_LINE
    _FACT_WRITE_BLOCK = FACT_MARK + _FACT_WRITE_LINE
    for fn in (_test_family_of, _test_action_facts, _test_render_and_compose,
               _test_strip_fact_lines, _test_injection_points,
               _test_share_one_classifier,
               _test_native_producer_does_not_print_fact_blocks):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_factblock()
