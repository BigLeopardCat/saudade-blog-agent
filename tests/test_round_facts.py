# -*- coding: utf-8 -*-
"""账甲（本轮状态**一处**合成）+ gate 洞⑮「办成了却说还在等」（20261008）。

**病灶（一手现场）**：`logs/agent/traces/20261008/20261008T083153_1_rc5ac1e6.json`
——主人点「确定」批准 niuniu 的额度重置，`approve_quota_request` **真的 PASS**（回执写着
「他的额度现在读数是 剩 500/500」），gate `frames=1` 未打回，narrator 却写下：

    「主人，这一轮系统**没有执行任何操作**……他的申请**还在待处理队列里**（账号
     「niuniu」id=5，剩 **451/500** 轮）……**要不要我再走一遍**？」

451 是**卡面快照**里的旧数（确认轮的用户消息是前端合成的回显，见 `_CONFIRM_ROUND_NOTE`）。
整段里「没有执行任何操作」那一半当天已被洞⑩ 第 ⑤ 支（量词锚定的整轮零操作）接住；
**「还在待处理队列里／刚才那次确认没落地」这一半当天全站无网**——本套件第 ② 节把它
当**红基线**逐字锁住：改之前 `_claim_issue` 对「仅第 2 句」返回 `None`，改之后是
`round_not_landed`。

更深的病：**「本轮那件事」今天不是一个对象**。六处各算一份前提，基准还不一样——
`_wrote_this_round`（看**计划**）／`_write_receipts`（看**回执**）／`_has_real_change`
（回执×noop）／`_no_popup_fact`（`pending_confirm` + 计划）／洞⑥（零状态前提）／
`false_negative_claim`（`bool(receipts)`）——供给侧说「本轮一个写操作都没提出来」与判据侧
按回执判「办成了」**可以互相矛盾**。本套件第 ① 节锁那一处合成（`RoundFacts`），第 ③ 节
锁供给侧那几条被它顶掉的假事实。

用法：.venv/bin/python tests/test_round_facts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 现场那两句（逐字，见文件头。`**` 是 narrator 的加粗，原样进来）────────────
SENT_1 = ("主人，这一轮系统**没有执行任何操作**——我这边没看到这次批准的执行记录，"
          "所以 niuniu 的额度申请还没批、什么都没改。")
SENT_2 = ("我能确定的是：他的申请还在待处理队列里（账号「niuniu」id=5，剩 451/500 轮），"
          "刚才那次确认没落地。要不要我再走一遍？")

# 现场那条回执（写族、checker PASS、非 noop）与那份计划（**计划里没有写**——
# 这正是供给侧与判据侧打架的那条缝，见 `_round_fact_note` ②）。
_RECEIPT = {"skill": "quota_approve", "tool": "approve_quota_request",
            "args": {"user_id": 5},
            "result": "已批准账号「niuniu」的额度重置申请，他的额度现在读数是 剩 500/500",
            "ts": 0.0}
_CHAT_PLAN = "SKILL=chat\nTOOLS: （无）\nSTATUS=answer_only"
_WRITE_PLAN = ('SKILL=quota_approve\nPARAMS: {}\n'
               'TOOLS: approve_quota_request({"user_id": 5})\nSTATUS=executed')

# 一个写回执归一化后的签名（`noop_specs` 里存的就是 `list(_spec_signature(...))`）
_WRITE_SIG = list(g._spec_signature("approve_quota_request", {"user_id": 5}))


def _facts(**kw) -> "g.RoundFacts":
    """按默认（= 现场那一轮）造一份账甲，逐字段可覆盖。"""
    base = dict(granted=False, planned_writes=(), ok_writes=("approve_quota_request",),
                noop_writes=(), changed=True, blocked_writes=(), popup=False,
                ledger_pending=False, awaiting_owner=False)
    base.update(kw)
    return g.RoundFacts(**base)


def _issue(text: str, facts=None, **kw):
    """按现场状态喂进声称闸（`facts=None` ⇒ 走老形参现推那条路，与生产同源）。"""
    args = dict(skill="quota_approve", plan={}, frames_exist=True, has_popup=False,
                receipts=[_RECEIPT], noop_specs=[], page_ctx="", role="admin")
    args.update(kw)
    if facts is not None:
        args["facts"] = facts
    return g._claim_issue(text, **args)


print("① 账甲：`_round_facts` 一处合成（narrator 与 gate 共读的那一份）")
f0 = g._round_facts({})
check("零写轮：ok/noop/planned 全空、changed=False、没有人在等",
      not (f0.ok_writes or f0.noop_writes or f0.planned_writes)
      and f0.changed is False and f0.ledger_pending is False
      and f0.awaiting_owner is False and f0.granted is False)

f_wait = g._round_facts({"ledger": {"pending": "把文章 19 置顶"}})
check("**真在等**那一档：ledger_pending=True ⇒ awaiting_owner=True"
      "（这一档里「还有一件等着你点头」是**真话**，判据必须放行）",
      f_wait.ledger_pending is True and f_wait.awaiting_owner is True)

f_wait2 = g._round_facts({"ledger": {"pending": "x"}, "receipts": [_RECEIPT]})
check("在等 + 这一轮也办了别的 ⇒ awaiting_owner=False（那就不只是「在等」了）",
      f_wait2.awaiting_owner is False)
f_wait3 = g._round_facts({"ledger": {"pending": "x"}, "pending_confirm": {"spec": "s"}})
check("在等 + 本轮还弹了卡 ⇒ awaiting_owner=False（卡在自己手上，不是「等人点头」）",
      f_wait3.awaiting_owner is False)

f_ok = g._round_facts({"receipts": [_RECEIPT], "noop_specs": [], "plan": _WRITE_PLAN})
check("办成有改动：ok_writes=[approve_quota_request]、changed=True、planned_writes 从计划推",
      f_ok.ok_writes == ("approve_quota_request",) and f_ok.changed is True
      and f_ok.planned_writes == ("approve_quota_request",))

f_noop = g._round_facts({"receipts": [_RECEIPT], "noop_specs": [_WRITE_SIG]})
check("办成零改动：进 noop_writes、**不进** ok_writes、changed=False"
      "（那一档里「没有实际改动」有几分真 ⇒ 洞⑮ 刻意不判，见 `_ROUND_NOT_LANDED_RE` 尾注）",
      f_noop.noop_writes == ("approve_quota_request",) and not f_noop.ok_writes
      and f_noop.changed is False)

f_missing = g._round_facts({"receipts": [_RECEIPT]})
check("边界：**缺 `noop_specs`** ⇒ 从严，写回执全算真改动（changed=True）",
      f_missing.changed is True and f_missing.ok_writes == ("approve_quota_request",))

f_blk = g._round_facts({"receipts": [_RECEIPT],
                        "blocked": [{"spec": "x", "tool": "audit_board_comment",
                                     "reason": "r", "skill": "s", "result": ""},
                                    {"spec": "y", "tool": "search_notes", "reason": "r",
                                     "skill": "s", "result": ""}]})
check("blocked_writes 只收**写族**（`search_notes` 那种读工具不算）",
      f_blk.blocked_writes == ("audit_board_comment",))

f_g = g._round_facts({"confirm_grant": {"skill": "quota_approve"},
                      "receipts": [_RECEIPT]})
check("granted 档：`confirm_grant` 在场 + 回执有写 ⇒ 两个字段同时为真",
      f_g.granted is True and f_g.ok_writes == ("approve_quota_request",))

print()
print("② 洞⑮ 的**红基线**：08:31 那两句，逐句判（改前第 2 句必须是 None）")
own2 = g._strip_quoted_spans(SENT_2)
own1 = g._strip_quoted_spans(SENT_1)
check("**红基线**：仅第 2 句 —— 洞⑩ 判**判不到**它（`_change_denial_claim` 返回 False）"
      "，所以今天之前它整段逃过闸；这就是本族存在的理由",
      g._change_denial_claim(own2, True) is False)
check("仅第 2 句 ⇒ 洞⑮ 命中 `round_not_landed`",
      (_issue(SENT_2) or ("",))[0] == "round_not_landed")
check("仅第 1 句 ⇒ 仍是**洞⑩**（`write_change_denial`）——洞⑮ 排在它之后，"
      "不改今天已经在跑的 issue 名与 `_replan_note` 分族",
      (_issue(SENT_1) or ("",))[0] == "write_change_denial")
check("整段 ⇒ 仍是洞⑩（两族同时在场时保留先到者）",
      (_issue(SENT_1 + "\n\n" + SENT_2) or ("",))[0] == "write_change_denial")
check("被否掉的那一句落 trace（子句级，不是整段）",
      "待处理队列" in (_issue(SENT_2) or ("", "", ""))[2])

print()
print("③ 洞⑮ 的词形与三道闸（**宁漏勿误**，逐条都在这里锁住）")
check("队列名词（「还在待确认」）不要求作用域锚定 ⇒ 命中",
      (_issue("主人，那笔额度申请还在待确认，你点一下我就去办。") or ("",))[0]
      == "round_not_landed")
check("乙组：锚定（「刚才」）+ 未落地动词 ⇒ 命中",
      (_issue("刚才那次确认没落地，我再等等。") or ("",))[0] == "round_not_landed")
check("乙组：全称量词（「一个都没落地」）⇒ 命中",
      (_issue("你交代的那几件事一个都没落地。") or ("",))[0] == "round_not_landed")
check("**无锚定的「还没落地」放行**——「那篇的置顶还没落地」同轮可能是真话"
      "（取向同 `not blocked_writes`）",
      _issue("那篇的置顶还没落地，等下我再试试。") is None)
check("**计数句放行**——「留言板还有 5 条待处理」说的是访客看得见的**审核队列**，是真话",
      _issue("留言板还有 5 条待处理留言，我这就去看。") is None)
check("**提议式放行**——「要不要我再走一遍」只有长在假话后面才假，"
      "它自己（连系统的 `_FALLBACK_CONFIRM_CLAIM` 都这么写）是正常的礼貌收尾",
      _issue("要不要我再走一遍？") is None)
check("**裸执行动词放行**——「本轮没有执行删除操作，只读了那一篇」是如实的**具体某类**"
      "动作说明（洞③ 早划过这条界）",
      _issue("本轮没有执行删除操作，只读了那一篇。") is None)
check("条件框架豁免（「要是本轮还没落地，我就再办一次」）",
      _issue("要是本轮还没落地，我就再办一次。") is None)
check("转述豁免（引号内的访客原话）",
      _issue("访客留言里写着「还没落地」。") is None)
check("闸①：本轮**有受阻的写**时不判（「一件办成、另一件受阻」那一档）",
      _issue("本轮那件批准还没落地。",
             facts=_facts(blocked_writes=("audit_board_comment",))) is None)
check("闸②：台账里**真有一行在等**时不判（那一轮里说「还在等」是真话）",
      _issue("那件申请还在待处理队列里，等你点头。", facts=_facts(ledger_pending=True))
      is None)
check("闸③：**noop 轮**（工具自报零改动）不判——前提是 `ok_writes`，那一列不含它",
      _issue("这一轮什么都没改。",
             facts=_facts(ok_writes=(), noop_writes=("approve_quota_request",),
                          changed=False)) is None)
check("零写轮不判（没有任何写回执可当前提）",
      _issue("这轮我没动手。", facts=_facts(ok_writes=(), changed=False)) is None)
check("老形参路径（不给 `facts`，按 receipts 现推）同样命中 ⇒ 两条路同源",
      (_issue(SENT_2, facts=None) or ("",))[0] == "round_not_landed")

print()
print("④ 兜底文案：认错 + **摆回执原文**（不写「否认句」、不二次加工事实）")
fb = g._fallback_round_not_landed(_facts(), [_RECEIPT])
check("摆的正是回执里 `result` 的原文（打掉「剩 451/500」那个幻觉的凭据）",
      "剩 500/500" in fb)
check("如实说「这一轮系统是真的执行了」（不许再出现「什么都没做」的口气）",
      "是真的执行了" in fb)
check("回执里没有可用原文时**不硬编一个数**（尾巴整段省略）",
      "剩 500/500" not in g._fallback_round_not_landed(_facts(), []))
check("兜底文案不含任何可抄的否认句（「没有执行任何操作／还没落地／待处理队列」）",
      not any(k in fb for k in ("没有执行任何操作", "还没落地", "待处理队列")))

print()
print("⑤ 重规划通道：与洞⑩ 同一出处（同一件事实的两个方向）")
check("`round_not_landed` 已挂进 `_REPLAN_ISSUES`（挂不上 ⇒ 打回直落兜底道歉）",
      "round_not_landed" in g._REPLAN_ISSUES)
check("建议/原因两条**指向**洞⑩ 那两份（同一出处，不抄第二份会漂移的副本）",
      g._REPLAN_ADVICE["round_not_landed"] is g._REPLAN_ADVICE["write_change_denial"]
      and g._REPLAN_WHY["round_not_landed"] is g._REPLAN_WHY["write_change_denial"])
check("`_replan_note` 对两族给**同一份**提示（「这一轮是真的执行过并复核通过了」）",
      g._replan_note("round_not_landed", "x")
      == g._replan_note("write_change_denial", "x")
      and "没有执行任何操作" not in g._replan_note("round_not_landed", "x"))

print()
print("⑥ 供给侧（预防这一侧）：几条被账甲顶掉的假事实")
check("`_round_fact_note` ①：`confirm_grant` 在场 ⇒ 仍是那条快照事实（逐字保留）",
      g._round_fact_note({"confirm_grant": {"skill": "s"}}) == g._CONFIRM_ROUND_NOTE)
note_seam = g._round_fact_note({"plan": _CHAT_PLAN, "receipts": [_RECEIPT]})
check("②**计划没写、回执却有写**那条缝 ⇒ 补一条账甲事实（这正是打架的那一轮）",
      "账甲事实（本轮）" in note_seam and "已经执行并复核通过" in note_seam)
check("② 常规写轮（计划里就有写）**不补**——回执就在明面上，多注一句白烧 token",
      g._round_fact_note({"plan": _WRITE_PLAN, "receipts": [_RECEIPT]}) == "")
check("③ noop 那一档单说「本来就是目标值」，且不许说成「我刚给你改的」",
      "本来就是目标值" in g._round_fact_note(
          {"plan": _CHAT_PLAN, "receipts": [_RECEIPT], "noop_specs": [_WRITE_SIG]}))
check("`_wrote_this_round` 读账甲：计划没写、回执有写 ⇒ **True**"
      "（此前只看计划 ⇒ False，`_no_popup_fact` 会把「一个写操作都没提出来」当事实说）",
      g._wrote_this_round({"plan": _CHAT_PLAN, "receipts": [_RECEIPT]}) is True)
check("`_no_popup_fact` 闸一：回执里有写 ⇒ 返回空串（本段那句「事实」在那一档是假话）",
      g._no_popup_fact({"plan": _CHAT_PLAN, "receipts": [_RECEIPT]}) == "")
_no_popup_plain = g._no_popup_fact({"plan": _CHAT_PLAN})
check("闸一不误伤：零写轮仍是原来那段（逐字，「一个写操作都没提出来」）",
      "一个写操作都没提出来" in _no_popup_plain)
_ledger_wait = g._no_popup_fact({"plan": _CHAT_PLAN, "ledger": {"pending": "x"}})
check("闸二：台账里**真有一行在等** ⇒ 换档说「没有**新的**写操作 + 那一行仍原样在等」"
      "（原句禁的「系统正等着主人点一下」在那一档与台账事实**直接打架**）",
      "没有新的写操作" in _ledger_wait and "仍原样在等" in _ledger_wait
      and "禁止说任何" not in _ledger_wait)
check("`_narrator_plan` 把同一份账甲传给那几条注记（本回合读一次、不是各自再算）",
      "账甲事实（本轮）" in g._narrator_plan(
          {"plan": _CHAT_PLAN, "receipts": [_RECEIPT]}))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 条未通过：")
    for _n in FAILED:
        print(f"   - {_n}")
    sys.exit(1)
print("✅ 全部通过")
