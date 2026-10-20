"""恒弹卡族的**死路守卫**：这批工具「目标说清了就必有一个入口」。

秒级、纯函数、无网络无 LLM（`_confirm_popup` 对恒弹卡族不读台账：`consent_granted`
恒 False ⇒ 免弹窗那一支短路，`_ident_grounded` 根本不会被调到 ⇒ 无需打桩字典）。

**为什么要有这个文件**（本仓已经栽过两次的同一个病）：

`_ALWAYS_CONFIRM_TOOLS`（authz.py，17 个）在 `consent_granted` 里**恒 False**
⇒ **确认弹卡是这 17 个工具唯一的执行路径**。而 `_confirm_popup` 的第一句是
`if grant or authz.is_question_like(user_msg): return None`——**一个否决位管着全部入口**。
于是"这句话像提问"一旦误判，后果不是"少问一次"，是**这条写能力没有任何入口**
（20260924T234402 现场：一份公告连着四轮没有执行途径，planner 每轮改写正文，
其中一轮开始替主人编造原话去够同意闸）。

那个现场的词根（裸名词「要求/注意」）20260925 已修，但**同一条死路在别的词位上活了下来**：
- I 表的**裸短语**（`有什么` / `有多少` / `多少`）：「…发一个公告，说说这次活动**有什么**注意事项」；
- Q 表的 `？` 落在**引号内容**里：「帮我发个公告，正文写「今晚几点睡？」」。

本文件就是这两次的网：**对 17 个恒弹卡工具逐个枚举**，断言"命令句 + 尾挂一段带
疑问词的正文/补充"仍然弹得出卡；同时把"真提问照样不弹"钉成反例。
下次再冒出一个裸词，先红在这里，而不是先炸在生产。

**红基线（20261005 落地前实测，先红后修）**：③ 那一节 17 条里 **15 条红**
（`kind=None`——命令说清了、目标也在原话里，卡就是不弹），绿的 2 条恰好是正文里
没带疑问词的那两个工具；⑤ 的判据层则整节红在"`agent.graph` 里没有
`_question_words_are_prose`"。修完这批后 ①②③④⑤ 全绿。

用法：.venv/bin/python tests/test_write_dead_end_guard.py
"""


from __future__ import annotations

import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from langchain_core.messages import HumanMessage  # noqa: E402

import agent.graph as g  # noqa: E402
from agent import (
    authz,  # noqa: E402
    confirm,  # noqa: E402
)
from agent.graph import _confirm_popup, plan_state  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


ADMIN = Principal(uid=7, role="admin")


CFG = {"configurable": {"principal": ADMIN, "user_id": 7, "conversation_id": 42,
                        "stop_event": None}}


def _popup(msg: str, tools: list, workflow: str):
    plan = {"workflow": workflow, "params": {}, "tools": list(tools), "note": "x", "reply": "y"}
    st = {"messages": [HumanMessage(content=msg)], **plan_state(plan),
          "plan_rounds": 0, "done": False}
    return _confirm_popup(st, list(tools), ADMIN, msg, CFG)


def _spec(tool: str, args: dict) -> str:
    import json
    return f"{tool}(" + json.dumps(args, ensure_ascii=False) + ")"


def _card_tools(popup) -> set:
    """卡上那几件的**工具名**（`pending_confirm.specs` 里是 {tool, args} 字典）。"""
    pc = (popup or {}).get("pending_confirm") or {}
    return {str(s.get("tool")) for s in (pc.get("specs") or [])}


def test_write_dead_end_guard():
    settings.jwt_secret = "test-secret-for-dead-end-guard-not-a-real-secret"
    CASES: list[tuple[str, str, str, str]] = [
        # (工具名, 命令句（尾挂疑问词正文）, spec 原文, 技能名)
        ("create_announcement",
         "帮我发个公告，正文就说这次活动有什么注意事项",
         _spec("create_announcement", {"title": "活动", "content": "这次活动有什么注意事项"}),
         "announcement_create"),
        ("update_announcement",
         "帮我把那条公告改一下，正文改成提醒大家注意身体，还有什么要补的你看着办",
         _spec("update_announcement", {"title": "今晚不许熬夜！", "content": "提醒大家注意身体"}),
         "announcement_update"),
        ("delete_announcement",
         "帮我把公告「今晚不许熬夜！」删掉，正文里那句「今晚怎么还不睡？」也别留了",
         _spec("delete_announcement", {"title": "今晚不许熬夜！"}),
         "announcement_delete"),
        ("delete_board_comment",
         "帮我把写着「今晚怎么还不睡？」的那条留言删掉",
         _spec("delete_board_comment", {"quote": "今晚怎么还不睡？"}),
         "board_comment_delete"),
        ("audit_board_comment",
         "帮我复核一下 #44 那条留言，他问的到底是什么，直接通过吧",
         _spec("audit_board_comment", {"talk_id": 44, "verdict": "pass"}),
         "board_audit"),
        # 文章评论的两件（20261019）：与上面两件同族、同一形态（命令 + 尾挂疑问词正文）。
        # ⚠️ 两句都带「评论」二字——两族的死路守卫共用一套形态，措辞里少一个字不影响
        # 本判据（它判的是"卡弹不弹得出来"），但会影响下面 ② 那半的 `is_question_like`
        # 前置（尾挂的疑问词必须在）。
        ("audit_comment",
         "帮我复核一下那条评论 #37，他问的到底是什么，直接驳回吧",
         _spec("audit_comment", {"comment_id": 37, "verdict": "reject"}),
         "comment_audit"),
        ("delete_comment",
         "帮我把那条评论 #37 删掉，还有多少条评论是没审的",
         _spec("delete_comment", {"comment_id": 37}),
         "comment_delete"),
        ("create_dashboard_todo",
         "帮我记一条待办：周五交房租，顺便看看我还有多少事没做",
         _spec("create_dashboard_todo", {"text": "周五交房租"}),
         "dashboard_todo_add"),
        ("complete_dashboard_todo",
         "帮我把买菜那条勾成完成，还有多少条是没勾的",
         _spec("complete_dashboard_todo", {"text": "买菜"}),
         "dashboard_todo_done"),
        ("reschedule_dashboard_todo",
         "帮我把交房租那条改到周五，还有几天到期",
         _spec("reschedule_dashboard_todo", {"text": "交房租", "date": "2026-10-09"}),
         "dashboard_todo_reschedule"),
        ("freeze_account",
         "帮我把 guest5 冻结掉，说说他这几天发了多少条",
         _spec("freeze_account", {"name": "guest5"}),
         "account_freeze"),
        ("unfreeze_account",
         "帮我把 guest5 解冻，他已经被关了有多少天了",
         _spec("unfreeze_account", {"name": "guest5"}),
         "account_unfreeze"),
        ("account_mute",
         "帮我把 guest5 禁言 72 小时，说说他这几天发了多少条",
         _spec("account_mute", {"name": "guest5", "hours": 72}),
         "account_mute"),
        ("account_unmute",
         "帮我把 guest5 解除禁言，已经禁了多少天了",
         _spec("account_unmute", {"name": "guest5"}),
         "account_unmute"),
        ("set_account_role",
         "帮我把 guest5 改成杂鱼，杂鱼到底有什么权限",
         _spec("set_account_role", {"name": "guest5", "role": "zako"}),
         "account_role"),
        ("send_user_notice",
         "帮我给 guest5 发个通知，正文写最近站内有什么新功能",
         _spec("send_user_notice", {"name": "guest5", "content": "最近站内有什么新功能"}),
         "user_notice"),
        ("reset_user_quota",
         "帮我把 guest5 的额度重置了，他这个月还剩多少轮",
         _spec("reset_user_quota", {"name": "guest5"}),
         "user_quota_reset"),
        ("approve_quota_request",
         "帮我批准 5 号那条额度申请，还有多少人在等",
         _spec("approve_quota_request", {"user_id": 5}),
         "quota_approve"),
        ("reject_quota_request",
         "帮我驳回 5 号那条额度申请，理由写「本月的量已经够了，还有什么问题再问」",
         _spec("reject_quota_request", {"user_id": 5,
                                        "reason": "本月的量已经够了，还有什么问题再问"}),
         "quota_reject"),
        # 20261019（SAU-014）从两档族迁进来的：命令骨架在场、尾挂的是**内容**
        # （主人顺口问的后果），按本文件的病根这就是同一类死路形态。
        ("delete_tag",
         "帮我把标签「大笨狗」删掉，还有多少文章挂着它",
         _spec("delete_tag", {"name": "大笨狗"}),
         "tag_delete"),
    ]
    print("① 完备性：这张构造表**必须**覆盖 `_ALWAYS_CONFIRM_TOOLS` 里的每一个工具")
    _covered = {c[0] for c in CASES}
    _missing = sorted(set(authz._ALWAYS_CONFIRM_TOOLS) - _covered)
    _extra = sorted(_covered - set(authz._ALWAYS_CONFIRM_TOOLS))
    check(f"恒弹卡工具一条不落（现 {len(authz._ALWAYS_CONFIRM_TOOLS)} 个，缺 {len(_missing)} 个）",
          not _missing, ",".join(_missing))
    check("表里没有非恒弹卡工具（加了它就会红在一个不相干的工具上）", not _extra,
          ",".join(_extra))
    print(f"\n② 形态前置：这 {len(CASES)} 句话**确实**被判成「像提问」（不成立的话下面全是假绿）")
    for tool, msg, _specs, _workflow in CASES:
        check(f"{tool}: is_question_like 为真（疑问词在内容里，不是主人在问）",
              authz.is_question_like(msg) is True, msg)
    print("\n③ 死路守卫：命令说清了 ⇒ 卡**必须**弹得出来")
    for tool, msg, specs, workflow in CASES:
        popup = _popup(msg, [specs], workflow)
        kind = (popup or {}).get("kind")
        check(f"{tool}: 弹得出卡（kind=confirm）", kind == "confirm",
              f"kind={kind!r} msg={msg[:34]}")
    print("\n④ 反例：**真提问**照样不弹（把提问读成意图是用户拍板明确不许的）")
    for tool, msg, specs, workflow in [
        ("create_announcement", "发公告有什么注意事项", _spec(
            "create_announcement", {"title": "活动", "content": "x"}), "announcement_create"),
        ("create_announcement", "发公告的流程是什么", _spec(
            "create_announcement", {"title": "活动", "content": "x"}), "announcement_create"),
        ("create_announcement", "公告的标题和正文要怎么写", _spec(
            "create_announcement", {"title": "活动", "content": "x"}), "announcement_create"),
        ("freeze_account", "把 guest5 冻结掉会有什么影响？", _spec(
            "freeze_account", {"name": "guest5"}), "account_freeze"),
        ("freeze_account", "如果我把 guest5 冻结掉的话", _spec(
            "freeze_account", {"name": "guest5"}), "account_freeze"),
        ("delete_announcement", "把公告「今晚不许熬夜！」删掉好吗", _spec(
            "delete_announcement", {"title": "今晚不许熬夜！"}), "announcement_delete"),
    ]:
        popup = _popup(msg, [specs], workflow)
        check(f"真提问不弹：{msg[:24]}", popup is None, f"kind={(popup or {}).get('kind')!r}")
    print("\n⑤ 判据层：疑问词「在内容里」的两种形态各自成立（真值只此一处）")
    _prose = getattr(g, "_question_words_are_prose", None)
    if _prose is None:
        check("agent.graph 里有 `_question_words_are_prose`（否决位的收窄判据）", False,
              "未定义")
    else:
        check("命令骨架在场 ⇒ 判成内容（「帮我发个公告，说说有什么注意事项」）",
              _prose("帮我发个公告，说说这次活动有什么注意事项") is True)
        check("疑问词全在引号里 ⇒ 判成内容（「正文写「今晚几点睡？」」）",
              _prose("帮我发个公告，正文写「今晚几点睡？」") is True)
        check("纯提问（无骨架、无引号）⇒ **不是**内容，照旧否决",
              _prose("发公告有什么注意事项") is False
              and _prose("公告的标题和正文要怎么写") is False)
        check("问后果（谓词位）⇒ 不是内容，照旧否决",
              _prose("把标签 Rust 挪到「嵌入式」下面会有什么影响？") is False)
        check("假设句 ⇒ 不是内容，照旧否决（有骨架也一样）",
              _prose("如果帮我把公告删掉的话") is False)
        check("句尾疑问 ⇒ 不是内容，照旧否决（把提问读成意图用户拍板不许）",
              _prose("把公告「今晚不许熬夜！」删掉好吗") is False
              and _prose("把 guest5 禁言 72 小时行吗") is False)
        check("句尾疑问**不在**这几种收尾上 ⇒ 仍是内容（别把「吧 / 了」当问号）",
              _prose("帮我把公告删了吧") is True
              and _prose("帮我发个公告，说说这次活动有什么注意事项") is True)
    print("\n⑥ 按件判（20261012）：整句有疑问词时，只否掉「自己那句在问」的那几件")
    _REAL_MSG = ("猫咪加条待办给用户niuniu重置额度，然后去把他额度重置，"
             "然后把待审的留言你审核一下是否通过，然后再发一条公告，以你的口吻随便说点声明")
    _REAL_TOOLS = [
        _spec("create_dashboard_todo", {"text": "给用户niuniu重置额度"}),
        _spec("reset_user_quota", {"uid": 9, "user": "niuniu"}),
        _spec("audit_board_comment", {"talk_id": 1, "verdict": True}),
        _spec("create_announcement", {"title": "声明", "content": "随便说点声明"}),
    ]
    _real = _popup(_REAL_MSG, _REAL_TOOLS, "chat")
    check("现场那句：锚得住的那两件**当场弹卡**（不再是整批零卡）",
          ({"create_dashboard_todo", "reset_user_quota"} <= _card_tools(_real)),
          f"卡上={sorted(_card_tools(_real))}")
    _MIX_MSG = ("给 niuniu 发通知说「你的额度已重置」会有什么影响？"
            "另外发条公告，标题「周末维护」正文随便写点")
    _MIX_TOOLS = [
        _spec("send_user_notice", {"uid": 9, "title": "额度通知", "content": "你的额度已重置"}),
        _spec("create_announcement", {"title": "周末维护", "content": "随便写点"}),
    ]
    _mix = _popup(_MIX_MSG, _MIX_TOOLS, "chat")
    check("逐件隔离：A 件自己那句在问 ⇒ A 不弹、B 照弹（卡上只有公告）",
          _card_tools(_mix) == {"create_announcement"}, f"卡上={sorted(_card_tools(_mix))}")
    for _m, _why in [
        ("，然后去把 guest5 冻掉会有什么影响？", "问后果"),
        ("然后把 guest5 冻掉会怎么样？", "问后果"),
        ("，然后去把 guest5 冻结好吗？", "句尾疑问"),
        ("先说说把 guest5 冻结的流程是什么", "无骨架提问"),
    ]:
        check(f"反控·{_why}照样不弹：{_m[:20]}",
              _popup(_m, [_spec("freeze_account", {"name": "guest5"})], "account_freeze") is None)
    check("正控（反控不假绿）：「，然后去把 guest5 冻结」弹得出卡",
          _popup("，然后去把 guest5 冻结",
                 [_spec("freeze_account", {"name": "guest5"})], "account_freeze") is not None)
    _DEL = [_spec("delete_announcement", {"title": "今晚不许熬夜！"})]
    for _m, _why in [
        ("把公告「今晚不许熬夜！」删掉好吗", "句尾疑问（锚点值里带「！」）"),
        ("把公告「今晚不许熬夜！」删掉，好吗？", "句尾疑问（逗号隔开）"),
        ("把公告「今晚不许熬夜！」删掉会有什么影响", "问后果（锚点值里带「！」）"),
    ]:
        check(f"反控·{_why}照样不弹：{_m[:22]}", _popup(_m, _DEL, "announcement_delete") is None)
    check("正控（上面三条不假绿）：同一句去掉疑问那一截 ⇒ 弹得出卡",
          _popup("把公告「今晚不许熬夜！」删掉", _DEL, "announcement_delete") is not None)
    _sc, _asks = getattr(g, "_spec_clause", None), getattr(g, "_spec_asks_question", None)
    check("agent.graph 里有 `_spec_clause` / `_spec_asks_question`（按件判的两个判据）",
          _sc is not None and _asks is not None,
          "" if (_sc is not None and _asks is not None) else "未定义")
    if _sc is not None and _asks is not None:
        _anchor = _spec("create_dashboard_todo", {"text": "给用户niuniu重置额度"})
        _noanchor = _spec("audit_board_comment", {"talk_id": 1, "verdict": True})
        check("锚得住 ⇒ 只取那一小段（逗号也是断点，四件不粘成一句）",
              _sc(_REAL_MSG, _anchor) == "加条待办给用户niuniu重置额度，",
              repr(_sc(_REAL_MSG, _anchor)))
        check("锚不住（参数全是模型编的）⇒ **退回整句**（fail-closed，与引入前逐字节相同）",
              _sc(_REAL_MSG, _noanchor) == authz.strip_user_shell(_REAL_MSG),
              repr(_sc(_REAL_MSG, _noanchor))[:40])
        check("`_spec_asks_question`：命令那件 False、只问一件的那段 True",
              _asks(_REAL_MSG, _anchor) is False
              and _asks(_MIX_MSG, _MIX_TOOLS[0]) is True
              and _asks(_MIX_MSG, _MIX_TOOLS[1]) is False)
    _frame = g._ORDER_FRAME_RE
    check("老骨架一条不落（请/帮我/给我…、句首与逗号后）",
          all(_frame.search(m) for m in (
              "把文章 12 设为私密", "请把文章 12 设为私密", "帮我删掉文章 12",
              "记得给 niuniu 发通知", "，把文章 12 设为私密", "给我把公告删了")))
    check("新收：并列命令的连接词（然后/再/接着… + 去/来）",
          all(_frame.search(m) for m in (
              "，然后去把文章 12 设为私密", "然后再把公告删了", "，接着把我们那个标签建了")))
    check("新收的连接词**认不出**「然后」后面不是把/将/给的句子（照旧按提问处理）",
          _prose("然后把文章 12 设为私密会怎么样？") is False
          and _prose("，然后去把他额度重置") is True)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_write_dead_end_guard()
