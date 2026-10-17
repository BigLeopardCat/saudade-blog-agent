# -*- coding: utf-8 -*-
"""写技能的「靶通道」表：一处声明、四处派生（20261015 批 1）。

**要治的病**：写技能从前有**两份必须手工同步的表**——写名单（`WRITE_WORKFLOW_NAMES`，30 个
名字手抄）与靶通道（四个 frozenset，28 个名字手抄；剩下两件硬写在展开链的 `else` 里）。
漂移的后果都是**静默的**：新写技能漏进名单 ⇒ 它压根不进写分支，落进通用模板分支把
`{"ids": null}` 展开成一次"零范围"的写；漏进靶通道 ⇒ 落进 `_expand_write_workflow` 的尾部
兜底，零工具零写还不报错。现在只有 `_WRITE_TARGET_CHANNEL` 一处声明：名单由 plan 的工具
作用域派生、四个桶由通道值派生。

本套件判的是"**这处声明还在承重吗**"——九节：

  ① **冻结值**：派生结果与从前手抄的值**逐名相同**（这是这次改造"零行为变更"的凭据）
  ② **覆盖锁**（防漂移的本体）：注册表里任何"plan 带写作用域工具"的技能**必须**声明
     通道——漏一个 ⇒ 红（从前是静默落进通用分支，谁都不会发现）。判据写成纯函数，
     **另配一支变异臂**（抽掉一条声明 ⇒ 它当场点名那一件）：只判"没有漏"的话，
     "判据能抓住"与"判据恒真"长得一模一样
  ③ **反向**：声明了通道却**不是**写技能 ⇒ 红（死条目会让读的人以为它被展开了）；同样带
     变异臂（把 `chat` 塞进声明集 ⇒ 当场点名它）
  ④ **分区**：四桶两两不相交，且四桶 + article桶（2 件）== 派生名单（30 件）
  ⑤ **为什么收窄到两个 scope**：广义 `authz.is_write()` 会多出那四件（navigate / effect /
     darkmode / device_display——它们动的是**主人的浏览器**不是站内数据）⇒ 冻结那四件
  ⑥ **展开链只读一处**（源码锁）：`instantiate_plan` 里不许再出现那四个成员判定
  ⑦ **派生是"按作用域"而不是"按集合"**：喂它一个假写技能 ⇒ 进名单；喂一个假读技能 ⇒ 不进
  ⑧ **新写技能没声明通道 ⇒ 响亮拒绝**（端到端：临时把假技能接进 `WORKFLOW_MAP`，真调
     `instantiate_plan` ⇒ 零工具 + "配置缺项"注记）。没有这一节，"派生换来的是响亮失败"
     与"派生只是换了个人手抄"长得一模一样
  ⑨ **接线**：既有八个消费点读的还是这四个名字（`graph.py` 的可见性闸 + 各套件）

离线、秒级、无网络无 LLM。用法：.venv/bin/python tests/test_write_channel.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import authz  # noqa: E402
from agent import workflows as S  # noqa: E402
from agent.workflows import (  # noqa: E402
    _FREE_TEXT_WRITE_WORKFLOWS,
    _LEDGER_TARGET_WRITE_WORKFLOWS,
    _OWN_WRITE_WORKFLOWS,
    _WRITE_NAME_TARGET_WORKFLOWS,
    _WRITE_TARGET_CHANNEL,
    WORKFLOW_MAP,
    WORKFLOWS,
    WRITE_WORKFLOW_NAMES,
    Workflow,
)

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── ① 冻结值：本批"零行为变更"的凭据（照 20261015 改造之前的手抄名单一字不差）──
_FROZEN_ALL = frozenset({
    "tag_create", "tag_update", "tag_delete",
    "category_create", "category_update", "category_delete",
    "announcement_create", "announcement_update", "announcement_delete",
    "board_audit", "board_delete",
    "favorite_add", "favorite_remove", "notice_read", "message_read",
    "dashboard_todo_add", "dashboard_todo_done", "dashboard_todo_reschedule",
    "account_freeze", "account_unfreeze", "account_set_role",
    "notice_send", "quota_reset",
    "quota_approve", "quota_reject",
    "mute_account", "unmute_account",
    "review_inbox", "article_status", "article_tags",
})
_FROZEN_BUCKETS = {
    "name": frozenset({
        "tag_create", "tag_update", "tag_delete",
        "category_create", "category_update", "category_delete",
        "announcement_create", "announcement_update", "announcement_delete",
        "board_delete", "account_freeze", "account_unfreeze", "account_set_role",
        "notice_send", "quota_reset", "mute_account", "unmute_account"}),
    "ledger": frozenset({"board_audit", "quota_approve", "quota_reject", "review_inbox"}),
    "own": frozenset({"favorite_add", "favorite_remove", "notice_read", "message_read"}),
    "free_text": frozenset({"dashboard_todo_add", "dashboard_todo_done",
                            "dashboard_todo_reschedule"}),
    "article": frozenset({"article_status", "article_tags"}),
}
print("① 冻结值：派生结果与改造前的手抄名单逐名相同")
check("①a 派生名单 == 30 件（逐名）", WRITE_WORKFLOW_NAMES == _FROZEN_ALL,
      f"{len(WRITE_WORKFLOW_NAMES)} 件；差集 {sorted(WRITE_WORKFLOW_NAMES ^ _FROZEN_ALL)}")
check("①b 四个桶逐名相同",
      _WRITE_NAME_TARGET_WORKFLOWS == _FROZEN_BUCKETS["name"]
      and _LEDGER_TARGET_WRITE_WORKFLOWS == _FROZEN_BUCKETS["ledger"]
      and _OWN_WRITE_WORKFLOWS == _FROZEN_BUCKETS["own"]
      and _FREE_TEXT_WRITE_WORKFLOWS == _FROZEN_BUCKETS["free_text"])
check("①c 通道表 30 个键覆盖全部写技能",
      set(_WRITE_TARGET_CHANNEL) == _FROZEN_ALL, f"{len(_WRITE_TARGET_CHANNEL)} 个键")

print("\n② 覆盖锁：注册表里有写作用域工具的技能必须声明通道（漏 ⇒ 静默落进通用分支）")


def _write_workflows(workflows: list) -> set:
    """注册表里"计划带写作用域工具"的技能（含 `extra_plan_tools`）。"""
    return {s.name for s in workflows
            if any(authz.TOOL_SCOPE.get(n) in S._WRITE_SCOPES
                   for n in [n for n, _ in s.plan] + list(s.extra_plan_tools))}


def _uncovered(workflows: list, declared) -> list:
    """漏声明通道的技能——② 的判据本体（写成函数，好让下面那支变异臂喂它假数据）。"""
    return sorted(_write_workflows(workflows) - set(declared))


_declared = set(_WRITE_TARGET_CHANNEL)
check("②a 没有「带写工具却没声明通道」的技能",
      not _uncovered(WORKFLOWS, _declared), str(_uncovered(WORKFLOWS, _declared)))
check("②b 这条锁真的在看注册表（30 件里每件都能被它认出是写技能）",
      _write_workflows(WORKFLOWS) == _FROZEN_ALL)
check("②c 变异臂：抽掉通道表里的一条 ⇒ 同一判据当场命名那一件"
      "（只判 ②a 的话，「判据能抓住」与「判据恒真」长得一样）",
      _uncovered(WORKFLOWS, _declared - {"board_audit"}) == ["board_audit"])

print("\n③ 反向：声明了通道却不是写技能的 ⇒ 死条目")


def _dead_entries(declared, workflows: list) -> list:
    """通道表里有、但注册表里不是写技能的条目——③ 的判据本体。"""
    return sorted(set(declared) - _write_workflows(workflows))


check("③a 没有死条目", not _dead_entries(_declared, WORKFLOWS), str(_dead_entries(_declared, WORKFLOWS)))
check("③b 变异臂：把 chat 塞进声明集 ⇒ 当场点名它（同上：证明 ③a 不是恒真）",
      _dead_entries(_declared | {"chat"}, WORKFLOWS) == ["chat"])

print("\n④ 分区：五桶两两不相交，五桶并集 == 派生名单（一个技能只能有一个靶通道）")
_buckets = [_WRITE_NAME_TARGET_WORKFLOWS, _LEDGER_TARGET_WRITE_WORKFLOWS, _OWN_WRITE_WORKFLOWS,
            _FREE_TEXT_WRITE_WORKFLOWS, S._channel("article")]
_names = ["name", "ledger", "own", "free_text", "article"]
_pairs = [(a, b) for i, a in enumerate(_buckets) for b in _buckets[i + 1:]]
check("④a 五桶两两不相交（一个技能声明两个通道 ⇒ 展开链的 if/elif 会静默吃掉后一个）",
      all(not (a & b) for a, b in _pairs),
      str([sorted(a & b) for a, b in _pairs if a & b]))
check("④b 并集 == 派生名单（每个写技能都恰好落进一桶）",
      set().union(*_buckets) == WRITE_WORKFLOW_NAMES,
      str(sorted(set().union(*_buckets) ^ WRITE_WORKFLOW_NAMES)))
_chan_vals = {v for v in _WRITE_TARGET_CHANNEL.values()}
check("④c 通道取值域就是这五个（多一个值 ⇒ 展开链的 `_chan == …` 会有分支没人走）",
      _chan_vals == set(_names), str(sorted(_chan_vals)))
check("④d `_channel()` 支持多值查询（`_WRITE_SCOPES` 那一族共用它）",
      S._channel("name", "article")
      == _WRITE_NAME_TARGET_WORKFLOWS | S._channel("article"))

print("\n⑤ 为什么必须收窄到 write.console / write.own：广义 is_write 会多出这四件")
_broad = {s.name for s in WORKFLOWS
          if any(authz.is_write(n) for n, _ in s.plan)}
check("⑤a 广义谓词多出的正好是 navigate / effect / darkmode / device_display",
      _broad - WRITE_WORKFLOW_NAMES == {"navigate", "effect", "darkmode", "device_display"},
      str(sorted(_broad - WRITE_WORKFLOW_NAMES)))
check("⑤b 收窄用的两个 scope 就是它们（不是别处另抄的一份）",
      S._WRITE_SCOPES == frozenset({authz.SCOPE_WRITE_CONSOLE, authz.SCOPE_WRITE_OWN}))

print("\n⑥ 展开链只读一处（源码锁：第二份手抄不许长回来）")
# 取值点跟着定义走（20261017 批 2 刀 2f）：`instantiate_plan` 已搬进
# `agent/workflow_plan.py`——**指回 workflows.py 会让这三条恒真**（那条调用在新文件里，
# 旧文件里什么都没有 ⇒ 假绿），比"取不到"更坏。
_src = (ROOT / "agent" / "workflow_plan.py").read_text(encoding="utf-8")
check("⑥a `instantiate_plan` 读的是通道表",
      _src.count("_chan = _WRITE_TARGET_CHANNEL.get(workflow.name)") == 1)
_stale = [p for p in ("workflow.name in _WRITE_NAME_TARGET_WORKFLOWS",
                      "workflow.name in _LEDGER_TARGET_WRITE_WORKFLOWS",
                      "workflow.name in _OWN_WRITE_WORKFLOWS",
                      "workflow.name in _FREE_TEXT_WRITE_WORKFLOWS") if p in _src]
check("⑥b 四个成员判定一个都不剩（长回来就是第二份手抄）", not _stale, str(_stale))
check("⑥c 也没有硬写的技能名排除项（`not in (\"article_status\", …)`）",
      '"article_status", "article_tags"' not in _src)

print("\n⑦ 派生按**作用域**判，不按集合判（可扩、不是又一次手抄）")
_fake_write = Workflow(name="zz_fake_write", description="", inputs={},
                    plan=[("delete_board_comment", {})])
_fake_read = Workflow(name="zz_fake_read", description="", inputs={},
                   plan=[("list_guestbook", {})])
_derived = S._derive_write_workflow_names([*WORKFLOWS, _fake_write, _fake_read])
check("⑦a 假写技能（plan 里是写工具）⇒ 自动进名单",
      "zz_fake_write" in _derived and "zz_fake_read" not in _derived)
check("⑦b 派生名单不含假技能（真仓取值未被动过）", "zz_fake_write" not in WRITE_WORKFLOW_NAMES)
_fake_extra = Workflow(name="zz_fake_extra", description="", inputs={},
                    plan=[("list_tags", {})], extra_plan_tools=("create_tag",))
check("⑦c `extra_plan_tools` 里的写工具也算（`article_tags` 的 create_tag 就在那一格）",
      "article_tags" in _derived
      and "zz_fake_extra" in S._derive_write_workflow_names([*WORKFLOWS, _fake_extra]))

print("\n⑧ 新写技能没声明通道 ⇒ 响亮拒绝（零工具 + 注记，不是静默落进通用分支）")
_fake = Workflow(name="zz_fake_write", description="", inputs={},
              plan=[("delete_board_comment", {})])
WORKFLOW_MAP["zz_fake_write"] = _fake
# 桩要打在**实现读它的那个模块**上（20261017 批 2 刀 2f）：`instantiate_plan` 搬进
# `agent.workflow_plan` 之后，它读的是**自己模块的全局**，打 `agent.workflows` 上那一份
# 打不着——而打不着的样子正是"⑧ 静默变绿"（假技能落进通用分支也照样有工具）。
# 桩改打新模块；反控见本文件头注那条（把桩打回 `S` ⇒ ⑧a 当场红）。
import agent.workflow_plan as _P  # noqa: E402

_P.WRITE_WORKFLOW_NAMES = frozenset(WRITE_WORKFLOW_NAMES | {"zz_fake_write"})   # 派生的等价物
try:
    _plan = S.instantiate_plan("zz_fake_write", {})
finally:
    del WORKFLOW_MAP["zz_fake_write"]
    _P.WRITE_WORKFLOW_NAMES = WRITE_WORKFLOW_NAMES
check("⑧a 零工具", _plan["tools"] == [], str(_plan["tools"]))
check("⑧b 注记写明是系统侧的配置缺项（不是「主人没说清」）",
      "配置缺项" in _plan["note"] and "没有接入参数展开" in _plan["note"], _plan["note"][:70])
check("⑧c status=refused（「办不成」是系统已知的处境，不是模型判断）",
      _plan["status"] == "refused")
check("⑧d 声明了通道的技能**不会**走这条拒绝路（同一入口的正控）",
      S.instantiate_plan("board_audit", {}).get("status") != "refused")

print("\n⑨ 接线：既有消费点读的还是这四个名字")
_graph = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("⑨a `graph.py` 仍从 workflows 导入并读 `_WRITE_NAME_TARGET_WORKFLOWS`（可见性闸）",
      "_WRITE_NAME_TARGET_WORKFLOWS" in _graph
      and _graph.count("s.name in _WRITE_NAME_TARGET_WORKFLOWS") >= 2)
check("⑨b 四个桶都是 frozenset（既有调用方按成员判定用，不是 list）",
      all(isinstance(v, frozenset) for v in (_WRITE_NAME_TARGET_WORKFLOWS,
                                             _LEDGER_TARGET_WRITE_WORKFLOWS,
                                             _OWN_WRITE_WORKFLOWS,
                                             _FREE_TEXT_WRITE_WORKFLOWS)))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
