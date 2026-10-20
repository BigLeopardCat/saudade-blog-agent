"""「状态已达成 ⇒ 不弹卡、不执行、如实说」（20260926）。

用户实测报的：对一篇**已经收藏**的文章说"收藏这篇"，确认卡照弹、点确定还照走一遍写
通道。那个"怪"是**两个半边**合成的一件：

  ① **弹窗那一半**：`graph._confirm_popup` 的判据只看"主人这句话 + 惰性快照"，
     从不看**目标现状** ⇒ 状态已达成时卡照弹；
  ② **回执那一半**：工具层其实早就短路了（收藏/已读/文章状态/标签/公告/留言族：
     不发写请求、回 `noop: True`），但 `write.own` 的回执**不带 meta**（`graph.py`
     那道 meta 闸只放 `AUDIT_SCOPES` = `write.console`）⇒ Rust 只能从 args 渲染出
     「收藏文章 12」——一次根本没发生的写被写进了执行台账。

本套件锁四件事（秒级、无网络无 LLM；由 pytest 自动收）：

  ① **判据本身**（`adminops.reached_specs`，纯函数）：各族的「已达成 / 未达成 /
     读不到 ⇒ 照弹」判定表——**fail-open 的方向永远是弹卡**，任何一条不成立
     （读失败 / 认不准 / id 不在快照里）都必须落回"照弹"。
  ② **掏空走新出口**：真跑 `_confirm_popup`（只桩后端读），断言返回 `noop_note` +
     `noop_text`、**没有** `pending_confirm`，且文案里没有动作完成式。
  ③ **混合轮**：有该弹的、也有已达成的 ⇒ 卡在、`specs` 只含要办的那几件、
     `confirm_text` 末尾带补充句（否则主人点完发现有一件没动会以为系统漏办了）。
  ④ **出口与复位**：`route_after_execute` 见 `noop_note` 直接 END；`AgentState` 与
     `graph_input` 都**显式声明**了这两个字段（未声明的 key 会被 LangGraph 静默丢出
     updates 流——`fallback_text` 与 `gate_replan` 各栽过一次，同族）。
  ⑤ **回执那一半**：短路的 `noop` 回执落 `change`、真写路径一个字都不落。
  ⑥ **接线**（20260927 补）：**真跑 `execute_node`** 走 noop 出口——这一格此前没有任何
     判据，而生产正是在这里崩的：调用方只写了 `popup["pending_confirm"]` 一条路，
     第二种出口一命中就 `KeyError('pending_confirm')`，冒到流级 `__ERROR__`，
     主人在气泡里看到「网络错误: 'pending_confirm'」（20260927 07:29 真机，待办
     「解冻 niuniu」本来就是完成状态）。①②③④ 全绿也没拦住——**能力有测试 ≠ 接线有测试**。

用法：.venv/bin/python tests/test_idem_noop.py
"""


from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

from _judge import collector
from langchain_core.messages import HumanMessage

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent import adminops_noop as _noop  # noqa: E402
from agent import adminops_tags as _tags  # noqa: E402
from agent.graph import execute_node, graph_input, plan_state, route_after_execute  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


PLAN = {"workflow": "favorite_add", "params": {}, "tools": [], "note": "", "reply": "直接回答"}


def _popup(specs, msg, uid=7, role="user", plan=PLAN):
    """真跑 `_confirm_popup`：真判据、真签发，假的只有后端那几次读。"""
    return g._confirm_popup(
        {"messages": [HumanMessage(content=msg)], **g.plan_state(plan)},
        specs, Principal(uid=uid, role=role), msg,
        {"configurable": {"user_id": uid, "conversation_id": 42}})


def _ids(picks):
    return [str(p.get("tool")) for p in (picks or [])]


def _reach(spec, **snaps):
    """单个 spec → `(该弹卡的条数, 已达成话术)`。"""
    kept, already = _noop.reached_specs([spec], **snaps)
    return len(kept), (already[0]["why"] if already else "")


def _verdict(spec, want_reached: bool, **snaps) -> tuple[bool, str]:
    _k, why = _reach(spec, **snaps)
    return (bool(why) == want_reached), why


_ST_PLAN = {"workflow": "article_status", "params": {}, "tools": [], "note": "", "reply": "x"}


_ST_SPEC = 'set_article_status({"article_id": 12, "status": "public"})'


_MSG_ST = "文章 12 设成公开吧"


def _popup_done(receipts, noop_specs=()):
    """真跑 `_confirm_popup`，只把**本轮已有的回执**填进 state（后端读照旧桩掉）。"""
    st = {"messages": [HumanMessage(content=_MSG_ST)], **g.plan_state(_ST_PLAN)}
    st["receipts"] = list(receipts)
    st["noop_specs"] = list(noop_specs)
    return g._confirm_popup(st, [_ST_SPEC], Principal(uid=1, role="admin"), _MSG_ST,
                            {"configurable": {"user_id": 1, "conversation_id": 42}})


CALLS: list = []


class _FakeTool:
    """假写工具：记录参数、返回给定结果（绝不碰网络）。"""

    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        CALLS.append(args)
        return self.out


def _grant_state(tool, args):
    """确认轮状态：`confirm_grant` 在场 ⇒ 同意闸与目标有据两门都放行。"""
    obj = {"workflow": "favorite_add", "params": {}, "tools": [f"{tool}({_json(args)})"],
           "note": "x", "reply": "y"}
    return {**plan_state(obj), "plan_rounds": 1, "done": False,
            "messages": [HumanMessage(content="（点确定的执行轮）")],
            "confirm_grant": {"workflow": "favorite_add",
                              "specs": [{"tool": tool, "args": args}]}}


def _json(d):
    import json
    return json.dumps(d, ensure_ascii=False)


_MSG_FAV = "文章 19 这篇我先收藏一下吧"


_EXEC_CFG = {"configurable": {"principal": Principal(uid=7, role="user"), "user_id": 7,
                              "conversation_id": 42, "stop_event": None}}


def _exec_round(specs, msg=_MSG_FAV):
    st = graph_input([HumanMessage(content=msg)])
    st.update(plan_state({"workflow": "favorite_add", "params": {}, "tools": list(specs),
                          "note": "", "reply": ""}))
    return execute_node(st, _EXEC_CFG)


def test_idem_noop():
    settings.jwt_secret = "test-secret-for-idem-noops"
    FAV_ONLY_19 = [{"noteId": 19, "title": "架构漫谈"}]
    NOTIF_ALL_READ = {3: {"id": 3, "isRead": True}, 5: {"id": 5, "isRead": True}}
    NOTIF_HAS_UNREAD = {3: {"id": 3, "isRead": False}, 5: {"id": 5, "isRead": True}}
    MAIL_ALL_READ = {"inbox": [{"id": 9, "isRead": True}], "outbox": [], "unread": 0}
    print("① 判据：各族的「已达成 / 未达成 / 读不到 ⇒ 照弹」（纯函数，快照手工给全）")
    TAGS = _tags.build_tag_index(
        [{"tagKey": 1, "title": "Python", "level": 1},
         {"tagKey": 2, "title": "架构", "level": 1}],
        [{"tagKey": 10012, "title": "Rust", "level": 2, "fatherTag": "Python", "fatherKey": 1}])
    NOTES = {12: {"noteKey": 12, "noteTitle": "架构漫谈", "status": "published",
                  "isTop": 0, "noteTags": "1,2"}}
    USERS = {126: {"id": 126, "username": "guest5", "status": 0},
             127: {"id": 127, "username": "guest6", "status": 1}}
    TODOS = [{"text": "交房租", "done": True, "date": "2026-09-28"},
             {"text": "写周报", "done": False, "date": ""}]
    BOARDS = {94: {"talkKey": 94, "content": "泠月喵好笨啊", "approved": 1, "author": "guest5"}}
    ANNS = {1: {"id": 1, "title": "维护通知", "content": "今晚维护"}}
    ok1, why = _verdict({"tool": "add_favorite", "args": {"article_id": 19}}, True,
                        favorites=FAV_ONLY_19)
    check("收藏：已经收藏过 → 判定「已达成」（说现状、印出标题）",
          ok1 and "本来就在你的收藏夹里" in why and "架构漫谈" in why, why)
    ok2, _ = _verdict({"tool": "add_favorite", "args": {"article_id": 21}}, False,
                      favorites=FAV_ONLY_19)
    check("收藏：没收藏过 → **照弹**（这正是要办的那次写）", ok2)
    ok3, why = _verdict({"tool": "remove_favorite", "args": {"article_id": 21}}, True,
                        favorites=FAV_ONLY_19)
    check("取消收藏：本来就没收藏 → 判定「已达成」", ok3 and "本来就不在你的收藏夹里" in why, why)
    ok4, _ = _verdict({"tool": "remove_favorite", "args": {"article_id": 19}}, False,
                      favorites=FAV_ONLY_19)
    check("取消收藏：确实收藏着 → **照弹**", ok4)
    ok5, _ = _verdict({"tool": "add_favorite", "args": {"article_id": 19}}, False,
                      favorites=None)
    check("收藏：**读不到收藏夹 → 照弹**（fail-open 的方向永远是弹卡，绝不静默拒绝一次写）", ok5)
    ok6, _ = _verdict({"tool": "add_favorite", "args": {"article_id": "十九"}}, False,
                      favorites=FAV_ONLY_19)
    check("收藏：id 认不出 → 照弹（不猜）", ok6)
    for tool, snap, good, bad in (("read_notifications", NOTIF_ALL_READ, NOTIF_HAS_UNREAD, None),
                                  ("read_messages", MAIL_ALL_READ,
                                   {"inbox": [{"id": 9, "isRead": False}], "unread": 1}, None)):
        key = "notifications" if tool == "read_notifications" else "messages"
        oka, whya = _verdict({"tool": tool, "args": {"all": True}}, True, **{key: snap})
        check(f"{tool}：全已读 → 判定「已达成」",
              oka and "本来就没有未读的" in whya, whya)
        okb, _ = _verdict({"tool": tool, "args": {"all": True}}, False, **{key: bad})
        check(f"{tool}：还有未读 → **照弹**", okb)
        okc, _ = _verdict({"tool": tool, "args": {"all": True}}, False, **{key: None})
        check(f"{tool}：**读不到 → 照弹**", okc)
    okd, whyd = _verdict({"tool": "read_notifications", "args": {"ids": [3, 5]}}, True,
                         notifications=NOTIF_ALL_READ)
    check("已读（点名 id）：那几条都已是已读 → 判定「已达成」",
          okd and "通知 3、5" in whyd and "已读状态" in whyd, whyd)
    oke, _ = _verdict({"tool": "read_notifications", "args": {"ids": [3, 99]}}, False,
                      notifications=NOTIF_ALL_READ)
    check("已读（点名 id）：**名单里没有那个 id → 照弹**（那是工具的 not_found 路，"
      "不是「已达成」）", oke)
    okf, _ = _verdict({"tool": "read_notifications", "args": {"ids": [3]}}, False,
                      notifications=NOTIF_HAS_UNREAD)
    check("已读（点名 id）：那条还没读 → **照弹**", okf)
    okk, whyk = _verdict({"tool": "unfreeze_account",
        "args": {"name": "guest5"}}, True, users=USERS)
    check("解冻：账号本来就是正常 → 判定「已达成」",
          okk and "现在就是正常状态" in whyk and "126" in whyk, whyk)
    okl, _ = _verdict({"tool": "freeze_account", "args": {"name": "guest5"}}, False, users=USERS)
    check("冻结：账号正常着 → **照弹**", okl)
    okm, whym = _verdict({"tool": "freeze_account", "args": {"name": "guest6"}}, True, users=USERS)
    check("冻结：账号本来就冻结着 → 判定「已达成」", okm and "现在就是冻结状态" in whym, whym)
    okn, _ = _verdict({"tool": "freeze_account", "args": {"name": "查无此人"}}, False, users=USERS)
    check("账号：名录里没这个名字 → **照弹**（那是工具的拒绝路，不是已达成）", okn)
    oko, _ = _verdict({"tool": "freeze_account", "args": {"name": "guest5"}}, False, users=None)
    check("账号：**读不到名录 → 照弹**", oko)
    okp, whyp = _verdict({"tool": "complete_dashboard_todo", "args": {"text": "交房租"}}, True,
                         todos=TODOS)
    check("待办：本来就已完成 → 判定「已达成」",
          okp and "本来就是完成状态" in whyp and "排期" in whyp, whyp)
    okq, _ = _verdict({"tool": "complete_dashboard_todo", "args": {"text": "写周报"}}, False,
                      todos=TODOS)
    check("待办：还没完成 → **照弹**", okq)
    okr, _ = _verdict({"tool": "complete_dashboard_todo", "args": {"text": "交房租"}}, False,
                      todos=TODOS + [{"text": "交房租", "done": True, "date": "2026-09-28"}])
    check("待办：同名多条（分不清是哪一条）→ **照弹**", okr)
    oks, whys = _verdict({"tool": "set_article_status",
                          "args": {"article_id": 12, "status": "public"}}, True, notes=NOTES)
    check("文章状态：本来就是公开 → 判定「已达成」",
          oks and "本来就是公开" in whys, whys)
    okt, whyt = _verdict({"tool": "set_article_status",
                          "args": {"article_id": 12, "is_top": 0}}, True, notes=NOTES)
    check("文章状态：只点名置顶且本来就是未置顶 → 判定「已达成」（只判点名的字段）",
          okt and "未置顶" in whyt and "公开" not in whyt, whyt)
    oku, _ = _verdict({"tool": "set_article_status",
                       "args": {"article_id": 12, "status": "draft"}}, False, notes=NOTES)
    check("文章状态：目标值不同 → **照弹**", oku)
    okv, _ = _verdict({"tool": "set_article_status",
                       "args": {"article_id": 99, "status": "public"}}, False, notes=NOTES)
    check("文章状态：清单里没有这一篇 → **照弹**（工具会拒绝，不是已达成）", okv)
    okw, whyw = _verdict({"tool": "set_article_tags",
                          "args": {"article_id": 12, "replace": ["Python", "架构"]}}, True,
                         notes=NOTES, index=TAGS)
    check("文章标签：算出来跟现状一样 → 判定「已达成」",
          okw and "Python" in whyw and "架构" in whyw, whyw)
    okx, _ = _verdict({"tool": "set_article_tags",
                       "args": {"article_id": 12, "add": ["Rust"]}}, False,
                      notes=NOTES, index=TAGS)
    check("文章标签：要加一个新标签 → **照弹**", okx)
    oky, _ = _verdict({"tool": "set_article_tags",
                       "args": {"article_id": 12, "add": ["不存在的标签"]}}, False,
                      notes=NOTES, index=TAGS)
    check("文章标签：名字对不上 id → **照弹**（不猜）", oky)
    okz, whyz = _verdict({"tool": "audit_board_comment",
                          "args": {"talk_id": 94, "verdict": "pass"}}, True, boards=BOARDS)
    check("留言审核：现在就是通过状态 → 判定「已达成」",
          okz and "现在就是通过状态" in whyz, whyz)
    okaa, _ = _verdict({"tool": "audit_board_comment",
                        "args": {"talk_id": 94, "verdict": "reject"}}, False, boards=BOARDS)
    check("留言审核：目标裁决不同 → **照弹**", okaa)
    okab, _ = _verdict({"tool": "delete_board_comment", "args": {"quote": "泠月喵好笨啊"}}, False,
                       boards=BOARDS)
    check("删留言：**永不判定「已达成」**（删一条已删的没有同值可比）", okab)
    okac, whyac = _verdict({"tool": "create_tag", "args": {"title": "Python"}}, True, index=TAGS)
    check("建标签：同名一级标签已在站里 → 判定「已达成」",
          okac and "本来就在站里" in whyac and "10012" not in whyac, whyac)
    okad, _ = _verdict({"tool": "create_tag", "args": {"title": "Go"}}, False, index=TAGS)
    check("建标签：站里没有这个名字 → **照弹**", okad)
    okae, _ = _verdict({"tool": "create_tag", "args": {"title": "Python", "color": "#eb2f96"}},
                       False, index=TAGS)
    check("建标签：同名但**点名了别的颜色** → **照弹**（主人要的那个标签其实还没有）", okae)
    okaf, whyaf = _verdict({"tool": "update_announcement",
                            "args": {"title": "维护通知", "new_title": "维护通知",
                                     "content": "今晚维护"}}, True, announcements=ANNS)
    check("公告：标题与正文都跟现状一样 → 判定「已达成」",
          okaf and "本来就是现在这个标题与正文" in whyaf, whyaf)
    okag, _ = _verdict({"tool": "update_announcement",
                        "args": {"title": "维护通知", "content": "改成明晚"}}, False,
                       announcements=ANNS)
    check("公告：正文要改 → **照弹**", okag)
    okah, _ = _verdict({"tool": "create_announcement",
                        "args": {"title": "维护通知", "content": "今晚维护"}}, False,
                       announcements=ANNS)
    check("发公告：**永不判定「已达成」**（发一条公告没有同值这回事）", okah)
    okai, _ = _verdict({"tool": "send_user_notice", "args": {"name": "guest5", "body": "你好"}},
                       False, users=USERS)
    check("发通知：**永不判定「已达成」**（发一条通知没有同值这回事）", okai)
    QREQS = {126: {"id": 900, "userId": 126, "username": "guest5", "status": 0}}
    QZERO = {126: {**USERS[126], "chatQuotaUsed": 0, "chatQuotaLimit": 500}}
    QUSED = {126: {**USERS[126], "chatQuotaUsed": 137, "chatQuotaLimit": 500}}
    QADMIN = {126: {**USERS[126], "chatQuotaUsed": 0, "chatQuotaLimit": 0}}
    _QA = {"tool": "approve_quota_request", "args": {"user_id": 126}}
    _QJ = {"tool": "reject_quota_request", "args": {"user_id": 126, "reason": "先不给"}}
    _QR = {"tool": "reset_user_quota", "args": {"name": "guest5"}}
    okj, whyj = _verdict(_QA, False, users=QZERO, quota_requests=QREQS)
    check("⭐ 批准：**满额但有份待处理的申请 → 照弹**"
      "（判「已达成」是真缺陷——那条申请在主人手上永远挂起，"
      "trace 20261001T105116 实证）",
          okj and whyj == "", whyj)
    okak, whyak = _verdict(_QA, True, users=QZERO, quota_requests={})
    check("批准：他名下没有待处理的申请 → 判定「已达成」并如实说现状"
      "（**与用量无关**：这一支问的是「还有没有那行」，不是「额度满没满」）",
          okak and "现在没有待处理的额度申请" in whyak and "满" not in whyak, whyak)
    okal, _ = _verdict(_QA, False, users=QZERO, quota_requests=None)
    check("批准：**申请快照读不到 → 照弹**（当成「没有申请」= 把挂起那件事演一遍）", okal)
    okam, _ = _verdict(_QA, False, users=None, quota_requests=QREQS)
    check("批准：名录读不到 → **照弹**", okam)
    okan, whyan = _verdict(_QJ, True, users=QZERO, quota_requests={})
    check("驳回：他名下没有待处理的申请 → 判定「已达成」并如实说现状",
          okan and "现在没有待处理的额度申请" in whyan, whyan)
    okao, _ = _verdict(_QJ, False, users=QZERO, quota_requests=QREQS)
    check("驳回：**有待处理申请 → 照弹**（这正是要办的那一次）", okao)
    okap, whyap = _verdict(_QR, True, users=QZERO, quota_requests={})
    check("主动重置：used=0 → 判定「已达成」（**这一支才是看计数器的**）",
          okap and "本来就是满的" in whyap, whyap)
    okaq, _ = _verdict(_QR, False, users=QUSED, quota_requests={})
    check("主动重置：用掉过 137 轮 → **照弹**（清零真会改变东西）", okaq)
    okar, _ = _verdict(_QR, False, users=USERS, quota_requests={})
    check("主动重置：**用量读不出 → 照弹**（不许当成 0 轮）", okar)
    okas, whyas = _verdict(_QR, True, users=QADMIN, quota_requests={})
    check("主动重置：不限额的管理员 → 另说一句（不是「额度满」而是「本来就没有上限」）",
          okas and "不限额" in whyas, whyas)
    print("\n② 掏空 ⇒ 走新出口（真跑 _confirm_popup，只桩后端读）")
    with patch.object(base, "_tag_index", lambda config: {}), \
            patch.object(base, "_favorites_snapshot", lambda config, what: (FAV_ONLY_19, None)), \
            patch.object(base, "_notifications_snapshot",
                         lambda config, what: (NOTIF_ALL_READ, None)), \
            patch.object(base, "_mailbox_snapshot", lambda config, what: (MAIL_ALL_READ, None)):
        check("前置探针：此刻密钥在位、签得出令牌（下面'没有 pending_confirm'才有意义）",
              len(g.confirm.sign(7, 42, "favorite_add",
                                 [{"tool": "add_favorite", "args": {"article_id": 19}}])) > 20)

        r = _popup(['add_favorite({"article_id": 19})'], "文章 19 这篇我先收藏一下吧")
        check("已收藏再收藏一次 → **不弹卡**（没有 pending_confirm）",
              isinstance(r, dict) and not r.get("pending_confirm"), str(r)[:90])
        check("  走的是新出口（noop_note 在场，且说明是哪些工具）",
              bool((r or {}).get("noop_note")) and "add_favorite" in r["noop_note"],
              str((r or {}).get("noop_note")))
        _t = (r or {}).get("noop_text") or ""
        check("  正文说清了现状（主人能核对）", "文章 19" in _t and "收藏夹" in _t, _t[:60])
        check("  正文明说**没有做任何改动**、连写请求都没发出去",
              "没有做任何改动" in _t and "连写请求都没有发出去" in _t, _t[:120])
        check("  正文里**没有动作完成式**（「已完成/已收藏/已标记/已冻结/已取消」"
          "会被读成'系统替我做过了一次'）",
              not any(w in _t for w in ("已完成", "已收藏", "已标记", "已冻结", "已取消")), _t)
        check("  这一轮零执行、零新增回执（messages 为空、receipts 原样带过）",
              r.get("messages") == [] and r.get("receipts") == [], str(r)[:120])
        check("  掏空时**不签发令牌**（没有卡就没有要签的东西）",
              "token" not in str(r.get("noop_text") or "") and not r.get("pending_action"))
        # 三件一起掏空（跨族）——收尾那句把三件都印出来
        r_multi = _popup(['add_favorite({"article_id": 19})',
                          'read_notifications({"all": true})',
                          'read_messages({"all": true})'],
                         "文章 19 收藏一下，通知和站内信都标成已读吧")
        _tm = (r_multi or {}).get("noop_text") or ""
        check("跨族三件全已达成 → 一次掏空，三件现状都印在同一段里",
              not (r_multi or {}).get("pending_confirm") and "收藏夹" in _tm
              and "通知" in _tm and "收件箱" in _tm, _tm[:160])

        # 反面：**读不到**照样弹卡（同一次调用里把收藏夹读桩成失败）
        with patch.object(base, "_favorites_snapshot",
                          lambda config, what: (None, base.unavailable("读不到收藏列表"))):
            r_fail = _popup(['add_favorite({"article_id": 19})'], "文章 19 这篇我先收藏一下吧")
            check("读不到现状 → **照弹卡**（绝不因为一次读失败就静默拒绝一次写）",
                  bool((r_fail or {}).get("pending_confirm")), str(r_fail)[:90])
    print("\n②b 掏空 + 这一轮**真办过事** ⇒ 正文不许说「连写请求都没有发出去」（20261015）")
    _NOTICE_RCPT = {"workflow": "notice_send", "tool": "send_user_notice", "ts": 1.0,
                    "args": {"name": "guest5", "body": "来一趟"},
                    "result": "已把通知发给账号「guest5」（账号 id=126）：标题「站内通知」，"
                          "正文「来一趟」。"}
    with patch.object(base, "_note_index", lambda config: NOTES):
        r_done = _popup_done([_NOTICE_RCPT]) or {}
        _td = r_done.get("noop_text") or ""
        check("前置：文章 12 本来就是公开 ⇒ 这一批被掏空、不弹卡",
              not r_done.get("pending_confirm") and bool(_td), str(r_done)[:100])
        check("★ 这一轮确实发出过写请求 ⇒ 正文**不许**说「连写请求都没有发出去」"
          "（那是**整轮**的声称，而这一格只看得到它自己那一批）",
              "连写请求都没有发出去" not in _td, _td[:160])
        check("  真办过的那件用**回执原文**印出来（主人看得见自己点的这件事真的办了）",
              "已把通知发给账号「guest5」" in _td, _td[:200])
        check("  已达成那几件的现状照旧印出来（① 没被挤掉）",
              "文章 12" in _td and "本来就是公开" in _td, _td[:160])
        check("  收窄后的说法仍在（「没有为它们再发一次写请求」）",
              "一件都没有动过" in _td, _td[:200])

        # 反控①：这一轮零写回执 ⇒ **逐字**回到老话术（②③ 两条硬要求一个字没变）
        _t0 = (_popup_done([]) or {}).get("noop_text") or ""
        check("  反控：零写回执 ⇒ 老话术逐字不变（「没有做任何改动」+「连写请求都没有发出去」）",
              "没有做任何改动" in _t0 and "连写请求都没有发出去" in _t0 and "对账" not in _t0)
        # 反控②：那条回执是**短路的零改动写**（工具自报 `changed=False`，站内一个字节都没变）
        # ⇒ 它不算"真发出去过"，不许被拿去当"这一轮办了事"的证据（判据是 receipts×noop_specs，
        # 与洞⑩ 同一份；只读 receipts 会把零改动的那一族全算成真写）。
        _sig = [list(g._spec_signature("send_user_notice", _NOTICE_RCPT["args"]))]
        _t1 = (_popup_done([_NOTICE_RCPT], noop_specs=_sig) or {}).get("noop_text") or ""
        check("  反控：回执在 `noop_specs` 里（零改动）⇒ 照旧说「连写请求都没有发出去」",
              "连写请求都没有发出去" in _t1 and "对账" not in _t1, _t1[:160])
        # 反控③：只读那几条回执**不算数**——读工具的回执再多，也不构成"发出过写请求"
        _t2 = (_popup_done([{**{k: v for k, v in _NOTICE_RCPT.items() if k != "tool"},
                             "tool": "get_article_detail", "result": "文章 12《架构漫谈》"}])
               or {}).get("noop_text") or ""
        check("  反控：读回执 ⇒ 不算（这一格问的是**写请求**发没发出去）",
              "连写请求都没有发出去" in _t2, _t2[:160])
    print("\n③ 混合轮：该弹的照弹、已达成的摘掉并写进卡面补充句")
    with patch.object(base, "_tag_index", lambda config: {}), \
            patch.object(base, "_favorites_snapshot", lambda config, what: (FAV_ONLY_19, None)), \
            patch.object(base, "_notifications_snapshot",
                         lambda config, what: (NOTIF_HAS_UNREAD, None)):
        r_mix = _popup(['add_favorite({"article_id": 19})',
                        'read_notifications({"all": true})'],
                       "文章 19 我先收藏着，顺便我想把通知都标成已读")
        pc = (r_mix or {}).get("pending_confirm") or {}
        check("卡照弹（还有一件真的要办）", bool(pc), str(r_mix)[:90])
        check("  specs 只剩**要办的那几件**（已达成的不进令牌——签一份包含它的令牌"
          "等于让主人签一件系统根本不打算办的事）",
              _ids(pc.get("specs")) == ["read_notifications"], str(pc.get("specs"))[:120])
        check("  pending_action.specs 同源（下一轮照它重提交，不能多出一件）",
              _ids(((r_mix or {}).get("pending_action") or {}).get("specs"))
              == ["read_notifications"])
        _ct = (r_mix or {}).get("confirm_text") or ""
        check("  confirm_text 末尾带补充句（点「确定」只会办前面那几件）",
              "没有列进这一批" in _ct and "只会办前面那几件" in _ct, _ct[-90:])
        check("  补充句里印的是**那一件的现状**", "收藏夹" in _ct, _ct[-90:])
        # `target` 与 specs 同源：不能让卡面上的"要办什么"与令牌里的"办什么"分叉
        check("  pending_action.target 只写要办的那几件",
              "通知" in str(((r_mix or {}).get("pending_action") or {}).get("target") or "")
              and "收藏" not in str(((r_mix or {})
                  .get("pending_action") or {}).get("target") or ""))
        check("  问句本身**不追加**补充句（问句是给眼睛看的，越短越好）",
              "没有列进这一批" not in str(pc.get("q") or ""), str(pc.get("q"))[:100])
    print("\n④ 出口与复位：noop_note ⇒ END；两个字段都显式声明")
    check("route_after_execute：见 noop_note → end（绝不去 model：narrator 面对"
      "『零工具帧 + 一件本来就办好的事』最可能说的就是『我已经帮你办好啦』）",
          route_after_execute({"noop_note": "状态已是目标值（add_favorite），本轮零改动"}) == "end")
    check("  空 noop_note（正常轮）→ 照旧走 planner（不多一条分支）",
          route_after_execute({}) == "planner"
              and route_after_execute({"noop_note": ""}) == "planner")
    check("  pending_confirm 那条出口一行没动（同款形状）",
          route_after_execute({"pending_confirm": {"q": "x"}}) == "end")
    _ann = getattr(g.AgentState, "__annotations__", {})
    check("AgentState 显式声明了 noop_text / noop_note（未声明 = 静默丢弃）",
          "noop_text" in _ann and "noop_note" in _ann)
    _gi = graph_input([])
    check("graph_input 给了这两个字段的空初值（state 形状完整、可读）",
          _gi.get("noop_text") == "" and _gi.get("noop_note") == "")
    _src = open(ROOT / "server.py", encoding="utf-8").read()
    check("接线：server.py 有 noop_note 分支、发过程行与 AI 帧（Rust 照常落库）",
          'ex_upd.get("noop_note")' in _src and 'ex_upd.get("noop_text")' in _src
          and "状态已是这个值，本轮零改动" in _src)
    check("  过程行不写「✅」（这一轮零执行，✅ 会被读成'办好了'）",
          "⏭ 状态已是这个值，本轮零改动" in _src)
    print("\n⑤ 回执那一半：短路的 noop 回执落 change、真写路径一个字都不落")
    CFG = {"configurable": {"principal": Principal(uid=7, role="user"), "user_id": 7,
                            "conversation_id": 42, "stop_event": None}}
    _saved = g._TOOL_MAP.get("add_favorite")
    try:
        # 短路（工具没发写请求，状态本来就是目标值）
        g._TOOL_MAP["add_favorite"] = _FakeTool(
            base.ok("文章 12 本来就在你的收藏夹里，无需改动（没有发出写请求）。",
                    meta={"op": "favorite_add", "article_id": 12,
                          "change": "本来已收藏", "noop": True}))
        CALLS.clear()
        r = execute_node(_grant_state("add_favorite", {"article_id": 12}), CFG)
        _rc = (r.get("receipts") or [{}])[0]
        check("短路回执带 change（Rust 那四臂据此渲染「本来就已收藏（未改动）」）",
              _rc.get("change") == "本来已收藏", str(_rc))
        check("  短路回执**不带 principal_role**（`write.own` 不是审计域："
          "本人的收藏与'以管理身份改了站内数据'无关，AUDIT_SCOPES 不扩）",
              "principal_role" not in _rc, str(_rc))
        check("  PASS 进回执（没发出写请求也是系统确认过的事实：状态就是这个值）",
              len(r.get("receipts") or []) == 1 and not r.get("blocked"))

        # 真写路径（工具真发了写请求）
        g._TOOL_MAP["add_favorite"] = _FakeTool(
            base.ok("已收藏文章 12《架构漫谈》。",
                    meta={"op": "favorite_add", "article_id": 12, "change": "已收藏"}))
        CALLS.clear()
        r2 = execute_node(_grant_state("add_favorite", {"article_id": 12}), CFG)
        _rc2 = (r2.get("receipts") or [{}])[0]
        check("真写回执**不带 change**（写真的发生了 ⇒ Rust 照旧从 args 渲染"
          "「收藏文章 12」；change 若也出现在这种行上，那四臂会改读它 ⇒ "
          "整行只剩「已收藏」、哪一篇没了）",
              "change" not in _rc2, str(_rc2))
        check("  真写回执照旧不带 principal_role",
              "principal_role" not in _rc2, str(_rc2))
    finally:
        if _saved is None:
            g._TOOL_MAP.pop("add_favorite", None)
        else:
            g._TOOL_MAP["add_favorite"] = _saved
    print("\n⑥ 接线：真跑 execute_node 走 noop 出口（这一段此前没有任何判据）")
    print("  （口径：真调 execute_node、真跑判据，只桩后端那几次读；不桩返回值）")
    _SPEC_FAV = 'add_favorite({"article_id": 19})'
    with patch.object(base, "_tag_index", lambda config: {}), \
            patch.object(base, "_favorites_snapshot", lambda config, what: (FAV_ONLY_19, None)):
        # ── 正面：掏空 ⇒ 走 noop 出口，**不许抛异常**
        try:
            r_noop = _exec_round([_SPEC_FAV])
            _raised = ""
        except Exception as e:                      # noqa: BLE001 —— 这正是被测的东西
            r_noop, _raised = None, f"{type(e).__name__}: {e}"
        check("已收藏再收藏一次 → execute_node **不抛异常**（事故现场就是这一格）",
              not _raised, _raised)
        check("  真走到了 noop 出口（noop_note / noop_text 在场）",
              isinstance(r_noop, dict) and bool(r_noop.get("noop_note"))
              and bool(r_noop.get("noop_text")), str(r_noop)[:100])
        check("  没有 pending_confirm（这一轮不弹卡）",
              isinstance(r_noop, dict) and not r_noop.get("pending_confirm"), str(r_noop)[:100])
        check("  判别键 `kind` **不进 state**（AgentState 没有这个字段，LangGraph 会"
          "静默丢弃它、留一份'以为记下来了'的错觉）",
              isinstance(r_noop, dict) and "kind" not in r_noop, str(sorted(r_noop or {})))
        check("  零执行（messages 空、receipts 原样带过）",
              (r_noop or {}).get("messages") == [] and (r_noop or {}).get("receipts") == [],
              str(r_noop)[:120])
        check("  这一份增量交给 route_after_execute → end（不去 narrator）",
              route_after_execute(r_noop or {}) == "end")
        # **判据自检**（否则上面那条"不抛异常"可能只是"什么都没测到"）：把事故当时
        # 那一行原样写出来跑一遍，确认它在这一格上真的会炸。判据能红，才说明它能拦。
        try:
            _raw = _popup([_SPEC_FAV], _MSG_FAV)      # `_confirm_popup` 的原样返回
            _raw["pending_confirm"]["specs"]          # 事故当天的调用方写法
            _old_raised = ""
        except Exception as e:                        # noqa: BLE001
            _old_raised = f"{type(e).__name__}: {e}"
        check("  判据自检：老写法 `popup['pending_confirm']['specs']` 在这一格上确实 KeyError"
          "（上面那条断言抓得到事故）",
              "pending_confirm" in _old_raised, _old_raised or "老写法居然没炸——自检不成立")
    with patch.object(base, "_tag_index", lambda config: {}), \
            patch.object(base, "_favorites_snapshot", lambda config, what: ([], None)):
        try:
            r_conf = _exec_round([_SPEC_FAV])
            _raised2 = ""
        except Exception as e:                       # noqa: BLE001
            r_conf, _raised2 = None, f"{type(e).__name__}: {e}"
        check("收藏夹里没有它 → execute_node 不抛异常、照弹卡",
              not _raised2 and bool((r_conf or {}).get("pending_confirm")), _raised2 or str(r_conf)[:90])
        check("  弹卡那份增量里也没有 `kind`（判别键只在函数内部用）",
              isinstance(r_conf, dict) and "kind" not in r_conf, str(sorted(r_conf or {})))
    _saved_popup = g._confirm_popup
    try:
        g._confirm_popup = lambda *a, **k: {"kind": "drifted", "pending_confirm": {"specs": []}}
        try:
            r_bad = _exec_round([_SPEC_FAV])
            _raised3 = ""
        except Exception as e:                       # noqa: BLE001
            r_bad, _raised3 = None, f"{type(e).__name__}: {e}"
    finally:
        g._confirm_popup = _saved_popup
    check("判别键认不出 → 不抛异常（漂移要响亮，但不许炸流）", not _raised3, _raised3)
    check("  按零改动收尾（响亮的出口仍走 noop，不拿一个来路不明的 dict 当弹卡批次）",
          isinstance(r_bad, dict) and bool(r_bad.get("noop_note"))
              and not r_bad.get("pending_confirm"),
          str(r_bad)[:120])
    print("\n⑦ 「已达成」那一批进流尾结算（20261015）：不产生回执的出口也要能结账")
    with patch.object(base, "_tag_index", lambda config: {}), \
            patch.object(base, "_favorites_snapshot", lambda config, what: (FAV_ONLY_19, None)):
        r_noop_task = _popup(['add_favorite({"article_id": 19})'], "文章 19 这篇我先收藏一下吧")
        # 混合轮（这一节真正难的那一半）：一件已达成、一件要办 ⇒ 卡照弹，
        # 但**已达成的那件也要带出去**——只给掏空那支出的话，台账那一步在混合轮这个
        # 形状下照样永远结不掉，而混合轮恰恰是最常见的形状。
        with patch.object(base, "_notifications_snapshot",
                          lambda config, what: (NOTIF_HAS_UNREAD, None)):
            r_mixed_task = _popup(['add_favorite({"article_id": 19})',
                                   'read_notifications({"all": true})'],
                                  "收藏一下文章 19，顺便把通知都标成已读")
    _sat = (r_noop_task or {}).get("task_satisfied") or []
    check("⭐ 掏空出口带回 `task_satisfied`——**带的是 spec（含靶）不是光秃秃的工具名**"
      "（结算侧要比'是不是这一步那个靶'：只带名字，同工具换靶会静默算完成）",
          _sat == [{"tool": "add_favorite", "args": {"article_id": 19}}], str(_sat))
    check("  它**不是回执**：`receipts` 一格没动（多一条就是把没做的事说成做了）",
          (r_noop_task or {}).get("receipts") == [])
    check("⭐ 混合轮也带（卡照弹 + 已达成那件仍进证据）",
          ((r_mixed_task or {}).get("task_satisfied") or [])
          == [{"tool": "add_favorite", "args": {"article_id": 19}}],
          str((r_mixed_task or {}).get("task_satisfied")))
    check("  混合轮的卡没被这一格弄坏（要办的那件还在卡上）",
          [str(s.get("tool")) for s in ((r_mixed_task or {}).get("pending_confirm") or {})
           .get("specs") or []] == ["read_notifications"])
    _gsrc = open(ROOT / "agent" / "graph.py", encoding="utf-8").read()
    check("  两个出口用的是**同一个**算法（`_satisfied_specs` 只此一处定义、两处调用）",
          _gsrc.count("def _satisfied_specs(") == 1
          and _gsrc.count("_satisfied_specs(already)") == 2)
    check("AgentState 显式声明了 task_satisfied（未声明 = 静默丢弃）",
          "task_satisfied" in _ann)
    check("graph_input 给了空初值（`[]`——结算侧按 `or []` 读同一支）",
          graph_input([]).get("task_satisfied") == [])
    check("接线：producer 收 execute 的 `task_satisfied` 并累加"
      "（同轮可能多次 update）",
          'ex_upd.get("task_satisfied")' in _src and "satisfied_specs.extend(" in _src)
    _adv_calls = [n for n in ast.walk(ast.parse(_src))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "advance_by_tools"]
    check("⭐ 两种证据走**同一次**游标推进（分两次的话，推不动的那半永远补不上）",
          len(_adv_calls) == 1
          and len(_adv_calls[0].args) >= 2
          and [k.arg for k in _adv_calls[0].keywords] == ["satisfied"],
          f"调用处 {len(_adv_calls)} 个；"
      f"参={[ast.dump(a) for a in (_adv_calls[0].args if _adv_calls else [])][:3]} "
      f"kw={[k.arg for k in (_adv_calls[0].keywords if _adv_calls else [])]}")
    _emit_fn = [n for n in ast.walk(ast.parse(_src))
                if isinstance(n, ast.FunctionDef) and n.name == "_emit_task_frames"]
    _dumps_adv = [n for fn in _emit_fn for n in ast.walk(fn)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "dumps"
                  and isinstance(n.func.value, ast.Name) and n.func.value.id == "json"]
    _extra = [k.arg for n in _dumps_adv for k in n.keywords if k.arg not in ("ensure_ascii",)]
    _bare = [n for n in _dumps_adv if n.args and isinstance(n.args[0], ast.Name)]
    _emit_calls = [n for n in ast.walk(ast.parse(_src))
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_emit_task_frames"]
    _key_adding = [n for n in _emit_calls
                   if not n.args or isinstance(n.args[0],
                                               (ast.Dict, ast.DictComp, ast.Starred))]
    check("⭐ `__TASK__` 帧的载荷就是结算回写本身、一个额外键都不加"
      "（`via`/`satisfied` 只进 trace）",
          len(_emit_fn) == 1 and len(_dumps_adv) == 1 and not _extra
          and len(_bare) == 1 and len(_emit_calls) == 2 and not _key_adding,
          f"dumps={len(_dumps_adv)} extra={_extra} 裸参={len(_bare)} "
      f"调用={len(_emit_calls)} 加键形状={len(_key_adding)}")
    print()
    if FAILED:
        print(f"{len(FAILED)} 条未通过：")
        for f in FAILED:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_idem_noop()
