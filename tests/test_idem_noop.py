# -*- coding: utf-8 -*-
"""「状态已达成 ⇒ 不弹卡、不执行、如实说」（20260926）。

用户实测报的：对一篇**已经收藏**的文章说"收藏这篇"，确认卡照弹、点确定还照走一遍写
通道。那个"怪"是**两个半边**合成的一件：

  ① **弹窗那一半**：`graph._confirm_popup` 的判据只看"主人这句话 + 惰性快照"，
     从不看**目标现状** ⇒ 状态已达成时卡照弹；
  ② **回执那一半**：工具层其实早就短路了（收藏/已读/文章状态/标签/公告/留言族：
     不发写请求、回 `noop: True`），但 `write.own` 的回执**不带 meta**（`graph.py`
     那道 meta 闸只放 `AUDIT_SCOPES` = `write.console`）⇒ Rust 只能从 args 渲染出
     「收藏文章 12」——一次根本没发生的写被写进了执行台账。

本套件锁四件事（秒级、无网络无 LLM；由 run_all.py 自动收）：

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

用法：.venv/bin/python tests/test_idem_noop.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent import adminops as A  # noqa: E402
from agent.graph import execute_node, graph_input, plan_encode, route_after_execute  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# 令牌密钥桩：`_confirm_popup` 在密钥空缺时**不弹窗**（宁可退回追问，也不发一个验不过
# 的令牌）⇒ 不立桩的话下面所有"该弹卡"的正例都会静默变成"不弹"，而"掏空"那几条
# （期望不弹）照样绿——**反例恒真的假绿**。CI 里没有 .env、默认就是空串，
# 这条桩是 CI 能红的前提（test_confirm.py 同一处理）。
settings.jwt_secret = "test-secret-for-idem-noops"

# ── 只桩后端读（不桩判据）──────────────────────────────────────────────
# `_confirm_popup` 里那几份快照各自都自带 try/except → None，真去读会打到本机
# Rust（3000）。测试里一律桩掉读取**本身**，判据一行不改。
FAV_ONLY_19 = [{"noteId": 19, "title": "架构漫谈"}]
# ⚠️ 快照形态**两族不一样**（`reached_specs` 的入参口径，照 `_confirm_popup` 传的
# 原样）：通知给的是 **`{id: 行}` 映射**（`tools.base._note_items` 的产物），
# 站内信给的是**接口原始返回**（它上面还有第二个事实——未读封数——给工具当写前基线，
# 所以由 `_mailbox_inbox` 在判据里现取那半）。写成同一个形状会两边都判不出。
NOTIF_ALL_READ = {3: {"id": 3, "isRead": True}, 5: {"id": 5, "isRead": True}}
NOTIF_HAS_UNREAD = {3: {"id": 3, "isRead": False}, 5: {"id": 5, "isRead": True}}
MAIL_ALL_READ = {"inbox": [{"id": 9, "isRead": True}], "outbox": [], "unread": 0}

PLAN = "SKILL=favorite_add\nPARAMS={}\nTOOLS: \nNOTE: \nREPLY: 直接回答"


def _popup(specs, msg, uid=7, role="user", plan=PLAN):
    """真跑 `_confirm_popup`：真判据、真签发，假的只有后端那几次读。"""
    return g._confirm_popup(
        {"messages": [HumanMessage(content=msg)], "plan": plan},
        specs, Principal(uid=uid, role=role), msg,
        {"configurable": {"user_id": uid, "conversation_id": 42}})


def _ids(picks):
    return [str(p.get("tool")) for p in (picks or [])]


# ══════════════════════════════════════════════════════════════════
print("① 判据：各族的「已达成 / 未达成 / 读不到 ⇒ 照弹」（纯函数，快照手工给全）")

TAGS = A.build_tag_index(
    [{"tagKey": 1, "title": "Python", "level": 1},
     {"tagKey": 2, "title": "架构", "level": 1}],
    [{"tagKey": 10012, "title": "Rust", "level": 2, "fatherTag": "Python", "fatherKey": 1}])
NOTES = {12: {"noteKey": 12, "noteTitle": "架构漫谈", "status": "published",
              "isTop": 0, "noteTags": "1,2"}}
USERS = {126: {"id": 126, "username": "guest5", "status": 0},
         127: {"id": 127, "username": "guest6", "status": 1}}
TODOS = [{"text": "交房租", "done": True, "date": "2026-09-28"},
         {"text": "写周报", "done": False, "date": ""}]
BOARDS = {9: {"talkKey": 9, "content": "泠月喵好笨啊", "approved": 1, "author": "guest5"}}
ANNS = {1: {"id": 1, "title": "维护通知", "content": "今晚维护"}}


def _reach(spec, **snaps):
    """单个 spec → `(该弹卡的条数, 已达成话术)`。"""
    kept, already = A.reached_specs([spec], **snaps)
    return len(kept), (already[0]["why"] if already else "")


def _verdict(spec, want_reached: bool, **snaps) -> tuple[bool, str]:
    _k, why = _reach(spec, **snaps)
    return (bool(why) == want_reached), why


# 收藏两件：收藏夹里有 19
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

# 已读两件
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

# 账号族
okk, whyk = _verdict({"tool": "unfreeze_account", "args": {"name": "guest5"}}, True, users=USERS)
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

# 待办
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

# 文章状态 / 标签
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

# 留言审核（只判审核：删除没有"同值"这回事）
okz, whyz = _verdict({"tool": "audit_board_comment",
                      "args": {"quote": "泠月喵好笨啊", "verdict": "pass"}}, True, boards=BOARDS)
check("留言审核：现在就是通过状态 → 判定「已达成」",
      okz and "现在就是通过状态" in whyz, whyz)
okaa, _ = _verdict({"tool": "audit_board_comment",
                    "args": {"quote": "泠月喵好笨啊", "verdict": "reject"}}, False, boards=BOARDS)
check("留言审核：目标裁决不同 → **照弹**", okaa)
okab, _ = _verdict({"tool": "delete_board_comment", "args": {"quote": "泠月喵好笨啊"}}, False,
                   boards=BOARDS)
check("删留言：**永不判定「已达成」**（删一条已删的没有同值可比）", okab)

# 标签新建（重名复用）与公告
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

# ══════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════
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
          and "收藏" not in str(((r_mix or {}).get("pending_action") or {}).get("target") or ""))
    check("  问句本身**不追加**补充句（问句是给眼睛看的，越短越好）",
          "没有列进这一批" not in str(pc.get("q") or ""), str(pc.get("q"))[:100])

# ══════════════════════════════════════════════════════════════════
print("\n④ 出口与复位：noop_note ⇒ END；两个字段都显式声明")

check("route_after_execute：见 noop_note → end（绝不去 model：narrator 面对"
      "『零工具帧 + 一件本来就办好的事』最可能说的就是『我已经帮你办好啦』）",
      route_after_execute({"noop_note": "状态已是目标值（add_favorite），本轮零改动"}) == "end")
check("  空 noop_note（正常轮）→ 照旧走 planner（不多一条分支）",
      route_after_execute({}) == "planner" and route_after_execute({"noop_note": ""}) == "planner")
check("  pending_confirm 那条出口一行没动（同款形状）",
      route_after_execute({"pending_confirm": {"q": "x"}}) == "end")

# 「必须显式声明」这一条要**真的锁住**（不是看一眼源码字符串）：未声明的 key 会被
# LangGraph 静默丢出 updates 流 ⇒ server.py 的 upd.get("noop_text") 恒为假、
# 这条如实的回复永远发不出去。fallback_text 与 gate_replan 各栽过一次，同族。
_ann = getattr(g.AgentState, "__annotations__", {})
check("AgentState 显式声明了 noop_text / noop_note（未声明 = 静默丢弃）",
      "noop_text" in _ann and "noop_note" in _ann)
_gi = graph_input([])
check("graph_input 给了这两个字段的空初值（state 形状完整、可读）",
      _gi.get("noop_text") == "" and _gi.get("noop_note") == "")

# server.py 那一支必须**真的接上**（"能力有测试 ≠ 接线有测试"）
_src = open(ROOT / "server.py", encoding="utf-8").read()
check("接线：server.py 有 noop_note 分支、发过程行与 AI 帧（Rust 照常落库）",
      'ex_upd.get("noop_note")' in _src and 'ex_upd.get("noop_text")' in _src
      and "状态已是这个值，本轮零改动" in _src)
check("  过程行不写「✅」（这一轮零执行，✅ 会被读成'办好了'）",
      "⏭ 状态已是这个值，本轮零改动" in _src)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 回执那一半：短路的 noop 回执落 change、真写路径一个字都不落")

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
    obj = {"skill": "favorite_add", "params": {}, "tools": [f"{tool}({_json(args)})"],
           "note": "x", "reply": "y"}
    return {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
            "messages": [HumanMessage(content="（点确定的执行轮）")],
            "confirm_grant": {"skill": "favorite_add",
                              "specs": [{"tool": tool, "args": args}]}}


def _json(d):
    import json
    return json.dumps(d, ensure_ascii=False)


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

print()
if FAILED:
    print(f"{len(FAILED)} 条未通过：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
