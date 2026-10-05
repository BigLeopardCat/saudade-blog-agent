# -*- coding: utf-8 -*-
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import agent.graph as g  # noqa: E402
from agent import authz  # noqa: E402
from agent import confirm  # noqa: E402
from agent.graph import _confirm_popup, plan_state  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 密钥桩（同 test_confirm.py 的理由：密钥空缺 ⇒ 签不出令牌 ⇒ 正例全变 None）──
settings.jwt_secret = "test-secret-for-dead-end-guard"

ADMIN = Principal(uid=7, role="admin")
CFG = {"configurable": {"principal": ADMIN, "user_id": 7, "conversation_id": 42,
                        "stop_event": None}}


def _popup(msg: str, tools: list, skill: str):
    plan = {"skill": skill, "params": {}, "tools": list(tools), "note": "x", "reply": "y"}
    st = {"messages": [HumanMessage(content=msg)], **plan_state(plan),
          "plan_rounds": 0, "done": False}
    return _confirm_popup(st, list(tools), ADMIN, msg, CFG)


def _spec(tool: str, args: dict) -> str:
    import json
    return f"{tool}(" + json.dumps(args, ensure_ascii=False) + ")"


# ── 逐工具构造：**全部 17 个恒弹卡工具**，每个一条"命令 + 尾挂疑问词正文" ──────
#   形态统一：前半句是明确的命令（`帮我…` / `把…`），后半句是主人要**写进站内**的
#   那段话（或他顺口的追问）。后半句里的疑问词**不是主人在问系统**——它是内容。
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
]

print("① 完备性：这张构造表**必须**覆盖 `_ALWAYS_CONFIRM_TOOLS` 里的每一个工具")
_covered = {c[0] for c in CASES}
_missing = sorted(set(authz._ALWAYS_CONFIRM_TOOLS) - _covered)
_extra = sorted(_covered - set(authz._ALWAYS_CONFIRM_TOOLS))
check(f"恒弹卡工具一条不落（现 {len(authz._ALWAYS_CONFIRM_TOOLS)} 个，缺 {len(_missing)} 个）",
      not _missing, ",".join(_missing))
check("表里没有非恒弹卡工具（加了它就会红在一个不相干的工具上）", not _extra,
      ",".join(_extra))

print("\n② 形态前置：这 17 句话**确实**被判成「像提问」（不成立的话下面全是假绿）")
for tool, msg, _specs, _skill in CASES:
    check(f"{tool}: is_question_like 为真（疑问词在内容里，不是主人在问）",
          authz.is_question_like(msg) is True, msg)

print("\n③ 死路守卫：命令说清了 ⇒ 卡**必须**弹得出来")
for tool, msg, specs, skill in CASES:
    popup = _popup(msg, [specs], skill)
    kind = (popup or {}).get("kind")
    check(f"{tool}: 弹得出卡（kind=confirm）", kind == "confirm",
          f"kind={kind!r} msg={msg[:34]}")

print("\n④ 反例：**真提问**照样不弹（把提问读成意图是用户拍板明确不许的）")
#   这几句与 ③ 的区别只有一个：**没有命令骨架**。疑问词不是内容，是主人在问。
for tool, msg, specs, skill in [
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
    popup = _popup(msg, [specs], skill)
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

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
