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

用法：.venv/bin/python tests/test_pending_ledger.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as g  # noqa: E402
import tools.base as tb  # noqa: E402
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
    """两份队列的读取桩：既当夹具，也当「读了几次」的判据（零读 = 一次都没连）。"""

    def __init__(self, board=BOARD_PENDING, quota=QUOTA_PENDING):
        self.board, self.quota = board, quota
        self.reads: list[str] = []
        self.saved = None

    def install(self):
        self.saved = (tb._board_index, tb._quota_pending_index)

        def _board(config):
            self.reads.append("board")
            return self.board

        def _quota(config):
            self.reads.append("quota")
            return self.quota

        tb._board_index, tb._quota_pending_index = _board, _quota
        return self

    def uninstall(self):
        if self.saved:
            tb._board_index, tb._quota_pending_index = self.saved
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
# 影子档（`_planner_engine == "shadow"`）走的是同一个 `_prompt_args`：影子两侧比的必须
# 是"同一个提问"，缺了这一格就成了"两个不同的提问"（见 `_render_planner_prompt` 头注）。
check("  `planner_node` 把它算进 `_prompt_args`（两档共用同一份提问）",
      "pending_ledger=ledger_frame" in
      (ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))

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

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
