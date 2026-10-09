# -*- coding: utf-8 -*-
"""待办台账帧：把「等着主人点头的那几件」按 id 摆上桌（20260929 批 H · S1）。

秒级、纯函数、零网络零 LLM；由 eval.yml 在 push 时跑。

要治的病（生产 trace 实证，会话 259 后两轮）：
  · 22:18:32「按你的想法来吧」——旧快道**读出了**台账（talkId:101 + 账号 sora）并把
    候选摆给了模型，但同一块末尾写着「读到本块 = 系统这一轮没能拼出那张卡……**一条都
    不要写成已办**（本轮零写）」⇒ 模型照办，回了一句 answer_only。
  · 22:18:54「全部批准」——快道第一行 `if _short_reply_kind(user_msg) != "auth"` 直接
    早退 ⇒ **台账一次都没读**；模型自己从上一轮的残留里取回了正确的那条留言原文、
    选中 board_audit，然后被「原话必须出自主人这句话」那条判据打死。
两次都是**模型决策对了、系统手里没有可给的事实**。本批把事实供给与决策分开：
台账每轮如实摆上桌（本模块），办不办、办哪几件由模型定，系统只在写之前校验 id。

覆盖十块：
  ① 触发器：族名命中 / 授权式全选式 / 上一轮真读过 ⇒ 摆；都不命中 ⇒ **一次都不读**。
  ② 形态族：「全部批准」/「都办」/「你看着办」命中；「现在几点了」这类**不许**因为
     「像承接」就去读两份队列。
  ③ 事实齐不齐：每族的 id 必须在帧里（talkId: / 账号 id=）、原文/理由/作者在。
  ④ 上限：每族 ≤5 条 + 「…还有 N 条未列出」（不静默截断）。
  ⑤ 读不到 ≠ 没有：读失败必须明说「没读到、不确定」，**绝不许**写成「没有/0 条」。
  ⑥ 0 条时的如实告知（0 条就是 0 条，不许编一条出来）。
  ⑦ 权限按族各判各的：非管理员两族都不读（读也只会白拿一次 403）。
  ⑧ 提示词接线：{pending_ledger} 槽在模板里、渲染函数收这个参数、渲染结果里出现。
  ⑨ 尾巴：旧快道那句「读到本块……本轮零写」**必须已经消失**（本批要消灭的形状本身）。
  ⑩ 台账不再挂在短应答块里（`_short_reply_hint` 的 system_facts 形参已删）。
  ⑪–⑮ S4 收尾两句话 / 名录够不着的那几件 / `list_quota_requests` 措辞同源。
  ⑯ narrator 侧的台账事实（20261001 加）：提问轮拿得到，有帧/有写/闲聊轮一个字不加。

用法：.venv/bin/python tests/test_pending_ledger.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 仓根
sys.path.insert(0, str(ROOT))

import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as g  # noqa: E402
import tools.base as tb  # noqa: E402
from agent import adminops as A  # noqa: E402
from agent import authz  # noqa: E402
from agent.context import (_ledger_frame_wanted, _short_reply_hint,  # noqa: E402
                           _short_reply_kind)
from agent.principal import ROLE_ADMIN, ROLE_USER, Principal  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


BOARD_PENDING = {
    101: {"talkKey": 101, "userId": 9, "author": "sora", "approved": 0,
          "createTime": "2026-09-29 22:10:00", "content": "画板我已经回退掉了。"},
    102: {"talkKey": 102, "userId": 11, "author": "路人乙", "approved": 0,
          "createTime": "2026-09-29 22:11:00", "content": "垃圾博客，全是广告"},
    103: {"talkKey": 103, "userId": 12, "author": "路人丙", "approved": 1,
          "createTime": "2026-09-29 22:12:00", "content": "这条早就通过了"},
}
QUOTA_PENDING = {5: {"id": 71, "userId": 5, "username": "guest5", "reason": "写长文不够用"}}


class _Ledger:
    """两份队列的读取桩：既当夹具，也当「读了几次」的判据（零读 = 一次都没连）。

    第三条桩是**账号名录**（`tb._user_directory`，台账帧拿它判"这一件够不够得着"）：
    `users=None` = 名录里什么账号都没有（这是**合法的**生产形态：额度队列可以出现
    超管或已注销的申请人）。它单独记在 `user_reads` 里而不是并进 `reads`——`reads`
    的三条断言读的是"两份**队列**读了没有"（零额外网络开销那条纪律），两种读数是
    两件事，混在一起会让那些断言变得看不懂。
    """

    def __init__(self, board=BOARD_PENDING, quota=QUOTA_PENDING, users=None):
        self.board, self.quota = board, quota
        self.users = users if users is not None else {
            int(r["userId"]) for r in quota.values() if r.get("userId") is not None}
        self.reads: list[str] = []
        self.user_reads: list[str] = []
        self.saved = None

    def install(self):
        self.saved = (tb._board_index, tb._quota_pending_index, tb._user_directory)

        def _board(config):
            self.reads.append("board")
            return self.board

        def _quota(config):
            self.reads.append("quota")
            return self.quota

        def _users(config):
            self.user_reads.append("users")
            return {u: {"id": u, "username": f"u{u}"} for u in self.users}

        tb._board_index, tb._quota_pending_index = _board, _quota
        tb._user_directory = _users
        return self

    def uninstall(self):
        if self.saved:
            tb._board_index, tb._quota_pending_index, tb._user_directory = self.saved
            self.saved = None
        return self


ADMIN = Principal(uid=7, role=ROLE_ADMIN)
GUEST = Principal(uid=9, role=ROLE_USER)


def _cfg(recent_tools=None):
    return {"configurable": {"principal": ADMIN, "user_id": 7, "conversation_id": 42,
                             "stop_event": None, "recent_tools": recent_tools or []}}


def _frame(user_msg, prev_ai="", principal=ADMIN, cfg=None):
    return g._pending_ledger_frame(user_msg, prev_ai, principal, cfg or _cfg())


print("① 触发器：命中才摆、都不命中就一次都不读")
_lg = _Ledger().install()
try:
    _t, _m = _frame("今天天气怎么样")
    check("闲聊轮：不摆台账", _t == "" and _m == {}, repr(_t[:40]))
    check("  且**一次都没读**（零额外网络开销的纪律照旧）", _lg.reads == [], str(_lg.reads))

    _lg.reads.clear()
    _t2, _m2 = _frame("留言那边有什么等着我吗")
    check("当场提到留言 ⇒ 只读这一族", _lg.reads == ["board"], str(_lg.reads))
    check("  帧里出现该族的待审条数", "待审" in _t2 and _m2.get("board") == 2, str(_m2))

    _lg.reads.clear()
    _t3, _m3 = _frame("你看着办")
    check("授权式 ⇒ **两族都读**", sorted(_lg.reads) == ["board", "quota"], str(_lg.reads))
    check("  两族都在帧里", "talkId:101" in _t3 and "guest5" in _t3)

    _lg.reads.clear()
    _frame("好的", "我把留言板上那条隐藏，并批准 guest5 的额度申请。")
    check("上一轮那句提议提到了两族 ⇒ 两族都读",
          sorted(_lg.reads) == ["board", "quota"], str(_lg.reads))

    _lg.reads.clear()
    _frame("还有别的吗", "", ADMIN, _cfg(recent_tools=["list_quota_requests"]))
    check("上一轮**真读过**那份队列 ⇒ 这一族接着摆", _lg.reads == ["quota"], str(_lg.reads))
finally:
    _lg.uninstall()

print("② 形态族：授权式/全选式的话才触发，「像承接」不算")
for t in ("全部批准", "都办", "两个都办", "全都要", "你看着办", "按你的想法来吧", "一起办吧"):
    check(f"「{t}」算授权式/全选式", _ledger_frame_wanted(t), _short_reply_kind(t))
for t in ("现在几点了", "然后呢", "这样可以吗", "通过搜索找到的那篇", "这是什么"):
    check(f"「{t}」**不**算（不许因此去读两份队列）", not _ledger_frame_wanted(t))

print("③ 事实齐不齐：id 与原文/理由都在帧里")
_lg = _Ledger().install()
try:
    _t, _m = _frame("你看着办")
    check("留言带 talkId（写通道收的就是它）", "talkId:101" in _t)
    check("  带作者与原文节选", "sora" in _t and "画板我已经回退掉了" in _t)
    check("额度带账号与账号 id", "guest5" in _t and "账号 id=5" in _t)
    check("  带他写的理由", "写长文不够用" in _t)
    check("已通过的那条**不**在帧里（只摆等着办的）", "talkId:103" not in _t)
    check("「只有主人这句话真的指向它们时才办」这句纪律在", "真的指向它们时才办" in _t)
    check("trace 元数据：逐条 id 都记着",
          _m.get("ids") == ["talkId:101", "talkId:102", "userId:5"], str(_m))
    check("trace 元数据：帧字数有记", isinstance(_m.get("chars"), int) and _m["chars"] > 0)
finally:
    _lg.uninstall()

print("④ 上限：每族 ≤5 条 + 如实说还有几条")
_many_b = {200 + i: {"talkKey": 200 + i, "author": f"a{i}", "approved": 0,
                     "createTime": "2026-09-29 22:00:00", "content": f"第{i}条"}
           for i in range(8)}
_many_q = {100 + i: {"id": i, "userId": 100 + i, "username": f"u{i}", "reason": "不够用"}
           for i in range(7)}
_lg = _Ledger(board=_many_b, quota=_many_q).install()
try:
    _t, _m = _frame("你看着办")
    check("留言：只列前 5 条", _t.count("　　· talkId:") == 5, str(_t.count("　　· talkId:")))
    check("额度：只列前 5 件", _t.count("账号 id=") - 1 == 5, str(_t.count("账号 id=") - 1))
    check("留言：如实说还有 3 条未列出", "还有 3 条未列出" in _t)
    check("额度：如实说还有 2 件未列出", "还有 2 件未列出" in _t)
    check("计数是**全部**条数（不是列出来的那 5 条）",
          _m["board"] == 8 and _m["quota"] == 7, str(_m))
finally:
    _lg.uninstall()

print("⑤ 读不到 ≠ 没有（两族 × 两种既有失败形状）")
for fam, kind in (("board", "none"), ("board", "unavailable"),
                  ("quota", "none"), ("quota", "unavailable")):
    _lg = _Ledger().install()
    try:
        _bad = (None if kind == "none" else tb.unavailable("后台接口报错"))
        if fam == "board":
            tb._board_index = lambda config, _b=_bad: _b
        else:
            tb._quota_pending_index = lambda config, _b=_bad: _b
        _t, _m = _frame("你看着办")
        check(f"{fam} 读不到（{kind}）⇒ 明说没读到", "没读到" in _t)
        check("  且**不许**说成「没有/0 条」",
              "没有任何待审留言" not in _t and "没有待处理的" not in _t)
        check("  且 trace 记下是哪一族没读到", _m.get("unread") == [fam], str(_m))
        check("  另一族照常摆（一族读不到不拖累另一族）",
              ("guest5" in _t) if fam == "board" else ("talkId:101" in _t))
    finally:
        _lg.uninstall()

print("⑥ 0 条就是 0 条（不许编一条出来）")
_lg = _Ledger(board={}, quota={}).install()
try:
    _t, _m = _frame("你看着办")
    check("留言 0 条：如实说没有任何待审", "没有任何待审留言" in _t)
    check("额度 0 条：如实说没有待处理的", "没有待处理的" in _t)
    check("  trace 计数是 0（与「没读到」是两回事）",
          _m["board"] == 0 and _m["quota"] == 0 and _m["unread"] == [], str(_m))
finally:
    _lg.uninstall()

print("⑦ 权限按族各判各的")
_lg = _Ledger().install()
try:
    _t, _m = _frame("你看着办", "", GUEST)
    check("普通用户：两族都不读", _lg.reads == [], str(_lg.reads))
    check("  也不摆帧（他看不到后台队列）", _t == "" and _m == {})
finally:
    _lg.uninstall()

print("⑧ 提示词接线：槽在模板里、渲染函数收这个参数、渲染结果里真的出现")
check("模板里有 {pending_ledger} 槽", "{pending_ledger}" in g._PLANNER_PROMPT)
_lg = _Ledger().install()
try:
    _t, _m = _frame("你看着办")
finally:
    _lg.uninstall()
_prompt = g._render_planner_prompt(
    "admin", "ctx", "round", user_msg="你看着办", intent_hints="i",
    doc_anchors="d", recent_context="r", short_reply_hint="s",
    tool_results="t", pending_ledger=_t, ref_hints="", reflector_feedback="",
    correction="", contract=g._PLANNER_OUTPUT_CONTRACT_NATIVE, slim_skills=True)
check("渲染结果里出现台账（talkId）", "talkId:101" in _prompt)
check("  且渲染函数**必须有**这个形参（漏传即 TypeError）", "pending_ledger" in
      g._render_planner_prompt.__code__.co_varnames)
# `planner_node` 里只有一处渲染调用（`_prompt_args` 一次算好、一次渲染）：缺了这一格
# 就是"问模型的那个问题里没有台账"（见 `_render_planner_prompt` 头注）。
# 刀 2（20261009）后 `_planner_decide` 拆成阶段函数、跨段量改从 ctx 上取
# （`rounds` → `c.rounds`）⇒ 本文件的整文件文本锁先去掉那个前缀再看：
# 判据文本与拆分之前**逐字相同**（前缀清单从 graph.py 的 AST 读，见 `_ctx_src`）。
check("  `planner_node` 把它算进 `_prompt_args`",
      "pending_ledger=ledger_frame" in _ctx_src.graph_deprefixed())

print("⑨ 旧快道那句尾巴必须已经消失（它是本批要消灭的形状本身）")
# 判据落在**渲染出来的东西**上，不是源码上：要消灭的是"模型读到的那句话"，
# 而代码里留着"这句话为什么被删"的说明是对的（docstring 不进提示词）。
# 源码级扫描会把这段说明本身当成违规命中（本测试第一版就这么假红过一次）。
_lg = _Ledger().install()
try:
    _t, _m = _frame("你看着办")
finally:
    _lg.uninstall()
for _gone in ("读到本块", "一条都不要写成已办", "没能拼出那张卡", "本轮零写"):
    check(f"台账帧里没有「{_gone}」", _gone not in _t)
    check(f"  渲染出的提示词里也没有「{_gone}」", _gone not in _prompt)
check("  取而代之的是「由你定」", "由你定" in _t)
check("  以及「用 id、不许用原话/账号名代替」", "不要" in _t and "代替 id" in _t)

print("⑩ 台账不再挂在短应答块里（分类分支的附注 ⇒ 独立槽）")
_hint = _short_reply_hint([HumanMessage("好"), AIMessage("要不要我把那条留言通过？")])
check("短应答块里没有 talkId/账号 id", "talkId" not in _hint and "账号 id" not in _hint)
try:
    _short_reply_hint([HumanMessage("好")], "系统台账：一条")
    check("  system_facts 形参已删除（多传即 TypeError）", False)
except TypeError:
    check("  system_facts 形参已删除（多传即 TypeError）", True)

print("⑪ 收尾那两句话（S4）：改完再询问 / 没动作就问一句")
# 判据全在结构上（回执 / 令牌 / 写计划 / 提问判据），事实**现场重读**台账。
# 两句都只进 narrator 的 [执行计划] 段（`_narrator_plan`），planner 那份帧文本一个字
# 都不复用（它的 header 是对 planner 说的，见 `_ledger_closing_note` 的头注）。
_CHAT_PLAN = ("SKILL=chat\nPARAMS={}\nTOOLS: （无）\nNOTE: （无）\nREPLY: 直接回答")
_WRITE_PLAN = ("SKILL=board_audit\nPARAMS={}\n"
               'TOOLS: audit_board_comment({"talk_id": 101, "verdict": "pass"})\n'
               "NOTE: （无）\nREPLY: 直接回答")
_GRANT_AUDIT = {"skill": "board_audit",
                "specs": [{"tool": "audit_board_comment",
                           "args": {"talk_id": 101, "verdict": "pass"}}]}
_GRANT_OFFLINE = {"skill": "article_status",
                  "specs": [{"tool": "set_article_status", "args": {"article_id": 3}}]}


def _receipt(tool, **kw):
    return {"skill": "review_inbox", "tool": tool, "args": {}, "result": "ok", **kw}


def _st(msgs, receipts=None, grant=None, plan=_CHAT_PLAN):
    return {"plan": plan, "messages": list(msgs), "receipts": receipts or [],
            "confirm_grant": grant}


check("回执判据只认**写**工具（读回执不是'办了几件'）",
      [r["tool"] for r in g._write_receipts(
          _st([HumanMessage("好")], [_receipt("audit_board_comment"),
                                     _receipt("list_notes")]))]
      == ["audit_board_comment"])
check("编号字段 → 队列的这张表与 `_LEDGER_TARGET_FIELDS` 的值集合**相等**"
      "（加一件新的台账编号工具而漏了这里，是静默少问一句）",
      set(g._LEDGER_FIELD_FAMILY) == set(g._LEDGER_TARGET_FIELDS.values()))
check("  且族名就是 `_read_ledger_family` 认的那两个",
      set(g._LEDGER_FIELD_FAMILY.values()) == {"board", "quota"})

_lg = _Ledger().install()
try:
    # ── 改完再询问：令牌里的工具反查得出队列 ⇒ 重读、报剩余 ──
    _n = g._ledger_closing_note(
        _st([HumanMessage("确定")], [_receipt("audit_board_comment")], _GRANT_AUDIT),
        _cfg())
    check("改完再询问：说明这一轮真执行了几件（回执口径，不是计划口径）",
          "真的执行了** 1 件" in _n, _n[:80])
    check("  并且报出台账里还剩几件、逐条念出来",
          "还剩 2 件" in _n and "talkId:102" in _n)
    check("  且明确要它问一句「要不要也一起办」", "要不要也一起办" in _n)
    check("  只重读**那一族**（额度队列一次都不读：这次点头的是一件留言审核）",
          _lg.reads == ["board"], str(_lg.reads))
    check("  也**不许**编一件没办的事（禁止句在）", "不许编一件他" in _n)

    _lg.reads.clear()
    _lg.board = {102: BOARD_PENDING[102]}      # 只剩一件还没办
    _n1 = g._ledger_closing_note(
        _st([HumanMessage("确定")], [_receipt("audit_board_comment")], _GRANT_AUDIT),
        _cfg())
    check("  剩下几件是**重读**后的现状（办掉那件就不在'还剩'里了）",
          "还剩 1 件" in _n1 and "talkId:101" not in _n1, _n1[:80])

    _lg.board = {}
    _n0 = g._ledger_closing_note(
        _st([HumanMessage("确定")], [_receipt("audit_board_comment")], _GRANT_AUDIT),
        _cfg())
    check("  台账清空 ⇒ 如实说没有别的待办了（不许为了接话编一件）",
          "一件等着办的都没有了" in _n0 and "不要**为了接话再编一件事" in _n0)

    tb._board_index = lambda config: None
    _nu = g._ledger_closing_note(
        _st([HumanMessage("确定")], [_receipt("audit_board_comment")], _GRANT_AUDIT),
        _cfg())
    check("  重读读不到 ⇒ 说「没读到、不确定」，**不许**说成「没有别的了」",
          "没读到" in _nu and "没有别的了" in _nu and "一件等着办的都没有" not in _nu)
finally:
    _lg.uninstall()

_lg = _Ledger().install()
try:
    # 与台账无关的确认轮（如改文章状态）⇒ 一个字都不加、台账一次都不读
    _no = g._ledger_closing_note(
        _st([HumanMessage("确定")], [_receipt("set_article_status")], _GRANT_OFFLINE),
        _cfg())
    check("与台账无关的确认轮：不加收尾那一问（不顺手提两句待审留言）",
          _no == "" and _lg.reads == [], f"{_no!r} {_lg.reads}")
    _ng = g._ledger_closing_note(
        _st([HumanMessage("好的")], [_receipt("audit_board_comment")]), _cfg())
    check("没有令牌的轮次（普通写）：不走进收尾那一问（它是确认兑现轮的专属）",
          _ng == "" and _lg.reads == [], repr(_ng[:60]))
finally:
    _lg.uninstall()

print("⑫ 没动作就问一句（删掉旧快道之后唯一的确定性兜底）")
_SILENT = "留言那边我来处理"          # 提到留言、不是提问、没有写
check(f"「{_SILENT}」不是提问（这条腿自己先核实判据，免得测的是空转）",
      not authz.is_question_like(_SILENT))
for _q in ("留言板现在还有什么等着办的吗", "额度申请批了会怎么样"):
    check(f"「{_q}」被判成提问", authz.is_question_like(_q), _q)
_lg = _Ledger().install()
try:
    _n = g._ledger_closing_note(_st([HumanMessage(_SILENT)]), _cfg())
    check("该摆台账 + 零写 + 不是提问 ⇒ 把台账念一遍、问他要办哪几件",
          "一条写操作都没有执行" in _n and "talkId:101" in _n and "只问，不替他挑" in _n,
          _n[:80])
    check("  且明令禁止「系统正等着您点一下」这类话（本轮结构上不会有卡）",
          "系统正等着您点一下" in _n and "禁止" in _n)
    check("  且不许把任何一条写成已办", "写成已经办了的" in _n)
    _both = g._ledger_closing_note(_st([HumanMessage("你看着办")]), _cfg())
    check("两族都该摆时（授权式）两族都念", "guest5" in _both and "talkId:101" in _both)
finally:
    _lg.uninstall()

_lg = _Ledger().install()
try:
    check("提问轮：一个字都不加（主人只是在问，不是让它办）",
          g._ledger_closing_note(
              _st([HumanMessage("留言板现在还有什么等着办的吗")]), _cfg()) == "")
    check("  且这一次**一次都没读**（不该为了问一句去连后台）",
          _lg.reads == [], str(_lg.reads))
    check("这一轮计划里有写 ⇒ 不加（真办了由'改完再询问'那一支说话）",
          g._ledger_closing_note(_st([HumanMessage(_SILENT)], plan=_WRITE_PLAN),
                                 _cfg()) == "")
    check("计划里已经有系统台账核对结论（确定性收尾轮）⇒ 不加，别抢那句话",
          g._ledger_closing_note(
              _st([HumanMessage(_SILENT)],
                  plan=_CHAT_PLAN + "\n" + g._LEDGER_NOTE_PREFIX + "站内没有这条留言"),
              _cfg()) == "")
finally:
    _lg.uninstall()

_lg = _Ledger(board={}, quota={}).install()
try:
    _n = g._ledger_closing_note(_st([HumanMessage(_SILENT)]), _cfg())
    check("台账空 ⇒ 如实说「现在没有等着处理的」，不许编一件出来",
          "现在没有任何等着办的事" in _n and "不要**为了接话编" in _n, _n[:80])
finally:
    _lg.uninstall()

_lg = _Ledger().install()
try:
    tb._board_index = lambda config: None
    _n = g._ledger_closing_note(_st([HumanMessage(_SILENT)]), _cfg())
    check("读不到 ⇒ 说「没读到、不确定」，**不许**说成「没有等着办的」",
          "没读到" in _n and "不要**说成「没有等着办的」" in _n, _n[:80])
finally:
    _lg.uninstall()

print("⑬ 接线：这一问真的进了 narrator 的 [执行计划] 段（能力有测试 ≠ 接线有测试）")
_lg = _Ledger().install()
try:
    _state = _st([HumanMessage(_SILENT)])
    _plan_text = g._narrator_plan(_state, _cfg())
    check("接上 config 后，收尾那一问进了计划段",
          g._LEDGER_ASK_MARK in _plan_text and "talkId:101" in _plan_text)
    check("  且不再追加 `_no_popup_fact`（两句会互相拆台：一个要问、一个禁问）",
          bool(g._no_popup_fact(_state)) and g._no_popup_fact(_state) not in _plan_text)
    _lg.reads.clear()
    check("  单参数调用（老路径/纯单测）行为不变、**一次都不读台账**",
          g._narrator_plan(_state) == _CHAT_PLAN + "\n" + g._no_popup_fact(_state)
          and _lg.reads == [], str(_lg.reads))
finally:
    _lg.uninstall()
_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("  `model_node` 把 config 传给了它（漏了 = 这一问永远算不出来）",
      "plan=_narrator_plan(state, config)," in _src)

print("⑭ 名录够不着的那几件：**照印但标注**（20260930 加）")
# 生产实证：唯一一件待处理的额度申请是 uid=1（超管）的，而 `GET /api/temp-users` 按
# `is_listable_role` 过滤、**超管不列**（注销过的账号同样不在：quota_request 无外键，
# 销号不带走申请行）。于是台账上摆着一行"等着办"、写通道却永远够不着它——模型每轮都
# 挑它、每轮白跑（「无法获取当前用户身份」那次是另一个 bug，这条是**并列**的一条）。
# 取向：行列出来（它是事实：确实有人等着），但把"agent 办不了"标在行末 + 另说一句件数；
# 名录**读不到**时一条都不标（不知道 ≠ 办不了）。
_lg = _Ledger(quota={1: {"id": 91, "userId": 1, "username": "sora", "reason": "想接着问"},
                     5: {"id": 71, "userId": 5, "username": "guest5", "reason": "写长文不够用"}},
              users={5}).install()
try:
    _t, _m = _frame("你看着办")
    check("够不着的那件**仍在帧里**（有人等着是事实，不许悄悄抹掉）", "账号 id=1" in _t)
    check("  且带上了「agent 办不了」的标注", "agent 的额度写通道办不了这一件" in _t)
    check("  够得着的那件**不带**这句（同一族里两行各说各的）",
          "账号 id=5" in _t and _t.count("agent 的额度写通道办不了这一件") == 1)
    check("  另说一句件数（模型扫一眼「共 2 件」会把它算进「我这就去办」）",
          "其中 1 件**不在账号名录里**" in _t and "得您到后台处理" in _t)
    check("  逐条 id 的 trace 元数据照旧两件都在（标注不改 id 清单）",
          [i for i in _m.get("ids", []) if i.startswith("userId:")] == ["userId:1", "userId:5"],
          str(_m))
    check("  两族队列都照常读", sorted(_lg.reads) == ["board", "quota"], str(_lg.reads))
    check("  名录只读一次（不是为了标注就对每个 uid 各读一次）",
          _lg.user_reads == ["users"], str(_lg.user_reads))
finally:
    _lg.uninstall()

_lg = _Ledger().install()          # 名录**读不到**（ToolResult）
try:
    tb._user_directory = lambda config: tb.unavailable("后台账号列表返回 HTTP 500")
    _t, _m = _frame("你看着办")
    check("名录读不到 ⇒ 一条都不标（不知道 ≠ 办不了）",
          "办不了这一件" not in _t and "不在账号名录里" not in _t)
    check("  且照旧把队列里的件报出来（读不到名录不拖累台账）", "账号 id=5" in _t)
finally:
    _lg.uninstall()

_lg = _Ledger().install()          # 额度 0 件 ⇒ 不必白读一次名录
try:
    tb._quota_pending_index = lambda config: {}
    _t, _m = _frame("你看着办")
    check("额度 0 件 ⇒ 一次都不读名录（零额外网络开销）",
          _lg.user_reads == [], str(_lg.user_reads))
finally:
    _lg.uninstall()

print("⑮ `list_quota_requests` 那一屏说同一句话（同一份实现，两处不许各写各的）")
_lg = _Ledger(quota={1: {"id": 91, "userId": 1, "username": "sora", "reason": "想接着问",
                         "status": 0, "used": 0, "limit": 0}}, users=set()).install()
_saved_get = tb._admin_get
try:
    # 这一屏的队列走 `_admin_get`（不是 `_quota_pending_index`），所以桩要打在那一处。
    tb._admin_get = lambda path, config=None: list(_lg.quota.values())
    _out = str(tb.list_quota_requests.invoke({}, config=_cfg()))
    check("申请清单里也标注了这一件（模型看得见的每一屏都说同一句）",
          "agent 的额度写通道办不了这一件" in _out, _out[:140])
    check("  用的是**同一份**措辞（不是第二句近义话）",
          A._UNREACHABLE_NOTE in _out)
finally:
    tb._admin_get = _saved_get
    _lg.uninstall()

print("⑯ narrator 侧的台账事实：提问轮拿得到；有帧/有写/闲聊轮一个字都不加")
# 治的是 S1 自己留下的缺口：台账只摆给了 planner（决策的那一方），可这一族人问的
# 常常正是"后台还有哪些等着办"——模型看完台账零工具作答，narrator（真正开口的那个）
# 就既没有工具帧也没有台账。它与 S4 第二支是同一个判据的正反两面，只在"是不是提问"
# 那一处岔开。
_Q = "留言板现在还有什么等着办的吗"          # 提问 + 只命中留言族
_Q2 = "留言和额度那边现在还有什么等着办的吗"  # 提问 + 两族都命中


def _frame_msg(name):
    return ToolMessage(content="{}", tool_call_id="t1", name=name)


_lg = _Ledger().install()
try:
    _state = _st([HumanMessage(_Q)])
    check("判据自核：这两句都是提问、且命中该摆的那几族",
          authz.is_question_like(_Q) and authz.is_question_like(_Q2)
          and g._ledger_fact_note(_st([HumanMessage(_Q2)]), _cfg()).count("】") >= 1)
    _n = g._ledger_fact_note(_state, _cfg())
    check("提问轮 ⇒ 事实照给（此前这一轮一个字都拿不到）",
          g._LEDGER_FACT_MARK in _n and "talkId:101" in _n, _n[:80])
    check("  逐条带 id 与原文（与 planner 那份帧同一个渲染器）",
          "talkId:102" in _n and "画板我已经回退掉了" in _n)
    check("  明说这是**系统读来的**、不许说成自己动手查的", "不是你自己去查的" in _n)
    check("  明说只许说这上面写着的（没写上的本轮没查过）", "本轮就没有查过" in _n)
    check("  不替主人挑、不问要不要办（要问的话归收尾那两支）",
          "要办哪几件" not in _n and "要不要" not in _n)
    check("  这一段真的进了 narrator 的 [执行计划] 段",
          _n in g._narrator_plan(_state, _cfg()))
    check("  且照旧追加 `_no_popup_fact`（没有写这条事实，本段不负责）",
          g._no_popup_fact(_state) in g._narrator_plan(_state, _cfg()))

    _lg.reads.clear()
    _framed = _st([HumanMessage(_Q), _frame_msg("list_admin_board")])
    check("narrator 手上**已有**那一族的帧 ⇒ 一个字都不加（同一件事不两处措辞）",
          g._ledger_fact_note(_framed, _cfg()) == "")
    check("  且这一次**一次都没读**（帧比系统摘要更全，不为补一段去连后台）",
          _lg.reads == [], str(_lg.reads))
    check("  帧反查走的是那张唯一映射表（`get_moderation_status` 同样算数）",
          g._ledger_frames_present(
              _st([HumanMessage(_Q), _frame_msg("get_moderation_status")])) == {"board"})

    _lg.reads.clear()
    _nb = g._ledger_fact_note(_st([HumanMessage(_Q2), _frame_msg("list_admin_board")]),
                              _cfg())
    check("逐族判：留言那一族有帧就不给，额度那一族照给",
          "guest5" in _nb and "talkId:101" not in _nb, _nb[:80])
    check("  且只读没帧的那一族（有帧的那族零额外读取）",
          _lg.reads == ["quota"], str(_lg.reads))

    _lg.reads.clear()
    check("非提问轮 ⇒ 这一段不发（S4 的「没动作就问一句」负责那一轮）",
          g._ledger_fact_note(_st([HumanMessage(_SILENT)]), _cfg()) == ""
          and g._ledger_closing_note(_st([HumanMessage(_SILENT)]), _cfg()) != "")
    _lg.reads.clear()
    check("闲聊轮 ⇒ 不发、也不读", g._ledger_fact_note(
        _st([HumanMessage("今天天气怎么样")]), _cfg()) == "" and _lg.reads == [],
        str(_lg.reads))
    check("这一轮真动了手 ⇒ 不加（归「改完再询问」那一支）",
          g._ledger_fact_note(_st([HumanMessage(_Q)], plan=_WRITE_PLAN), _cfg()) == "")
    check("确定性收尾轮（台账核对结论已在计划里）⇒ 不加，别抢那句话",
          g._ledger_fact_note(
              _st([HumanMessage(_Q)],
                  plan=_CHAT_PLAN + "\n" + g._LEDGER_NOTE_PREFIX + "站内没有这条留言"),
              _cfg()) == "")
    _lg.reads.clear()
    check("config 缺省（老的单参调用/纯单测）⇒ 一个字都不加、一次都不读",
          g._ledger_fact_note(_st([HumanMessage(_Q)]), None) == ""
          and _lg.reads == [], str(_lg.reads))
finally:
    _lg.uninstall()

_lg = _Ledger().install()
try:
    tb._board_index = lambda config: None
    _nu = g._ledger_fact_note(_st([HumanMessage(_Q)]), _cfg())
    check("读不到 ≠ 没有：事实段里明写「没读到、不确定」",
          "没读到" in _nu and "没有待审" in _nu, _nu[:100])
finally:
    _lg.uninstall()

_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("接线：`_narrator_plan` 收了这一段（能力有测试 ≠ 接线有测试）",
      "ledger = _ledger_fact_note(state, config)" in _src)
check("闸门只有一份实现（收尾那一支与事实这一段各调一次、都在这一处判）",
      _src.count("    families = _ledger_turn_families(state, config)") == 2,
      str(_src.count("    families = _ledger_turn_families(state, config)")))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
