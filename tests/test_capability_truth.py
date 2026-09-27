# -*- coding: utf-8 -*-
"""能力边界必须写在模型读得到的地方（20260928）。

**动机**：narrator 说过一句**假的能力否定**——trace `20260928T032411`，回复原文
"系统这边**没有删除被驳回留言的通道**……这一步只能你自己进后台手动处理"。事实相反：
`_board_index` 读的就是**同一份**后台清单（`GET /api/protect/board` 只过滤
`Src=board`、不过滤 `approved`），被驳回的留言既找得到也删得掉。

这句假话为什么拦不住，也不该拦：gate 有"站内没有 X"的结论判据，但**能力否定被刻意
豁免**（`_CAPABILITY_NEG_RE`，20260924）——"我没有这个权限/没有这条通道"是**诚实拒答**
的形态，把它换成道歉反而更坏。判据分不出"诚实地说做不到"与"胡说地做不到"，
**因为这两件事在文本上一模一样**。⇒ 修法不是加一条禁令，而是**把事实放到模型读得到
的地方**（与 NAV_MAP、站点地图同类：能力边界是**系统数据**，不该靠模型推理去猜）。

三处载体，本套件逐处钉住：
  ① 审核状况报表（`reports.render_moderation_status`）——它是"某条留言现在什么状态"
     的唯一事实源，此前**只报状态、不说能对它做什么**；
  ② 技能描述与回复契约（`skills.board_delete`）——planner 选技能、narrator 组织话术，
     两边读的是同一份；
  ③ 工具自己的 `.description`（`@tool` 之后模型读的是它，**不是** `__doc__`）。

用法：.venv/bin/python tests/test_capability_truth.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import reports as R  # noqa: E402
from agent import skills as S  # noqa: E402
from tools import base as B  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════
print("\n① 审核状况报表：每一条都说得清「能对它做什么」")

# 两条被驳回的 + 一条待审的（与事故那一轮同形：报表把被驳回的连 id 与正文都列了出来）
ROWS = [
    {"talkKey": 99, "author": "某人", "content": "博主是SB", "approved": 2,
     "ai_result": "reject", "createTime": "2026-09-27 10:00:00"},
    {"talkKey": 92, "author": "阿岚", "content": "广告广告", "approved": 2,
     "ai_result": "pass", "createTime": "2026-09-27 11:00:00"},
    {"talkKey": 96, "author": "小舟", "content": "谢谢站长的分享！", "approved": 0,
     "ai_result": "flag", "createTime": "2026-09-27 12:00:00"},
]
rep = R.render_moderation_status(ROWS)
check("报表给出了**处置**那一条（否则模型只能自己推，推出来的就是那句假话）",
      "处置" in rep, rep[-120:])
check("  明说两条通道都在：改判与删除", "改判" in rep and "删除" in rep)
check("  明说**没有哪一类是删不掉的**（直接对上那句「被驳回的删不掉」）",
      "没有哪一类是删不掉的" in rep)
check("  明说写通道**只认正文原话**（免得它让主人「报编号」——那条路走不通）",
      "正文原话" in rep and "不按 `talkId` 认" in rep)
check("  明说要动多条就逐条来（批量通道今天不存在，不许承诺）",
      "逐条来" in rep)
check("  （边界）报表仍是**事实**而不是承诺：删除那半必须写明取不回来",
      "取不回来" in rep)

# ══════════════════════════════════════════════════════════════════
print("\n② 技能描述与回复契约（planner 与 narrator 读的同一份）")

_SK = {s.name: s for s in S.SKILLS}
_del = _SK["board_delete"]
check("board_delete 的技能描述里写着「待审与被驳回的留言同样删得掉」",
      "待审与被驳回的留言同样删得掉" in _del.description)
check("  回复契约里禁止那句假否定（「没有删除被驳回留言的通道」）",
      "没有删除被驳回留言的通道" in _del.reply_contract
      and "删得掉" in _del.reply_contract)
check("  回复契约里写着**不要让他报编号**（写通道不认编号，报了也做不成）",
      "不要让他报编号" in _del.reply_contract)
check("  回复契约里写着多条要一条一段原话（不许承诺一次删多条）",
      "一条一段原话" in _del.reply_contract)
_a = _SK["board_audit"]
check("（对照）审核技能仍按正文片段指认，措辞未被动过",
      "quote" in _a.inputs and "audit_board_comment" in str(_a.plan))

# ══════════════════════════════════════════════════════════════════
print("\n③ 工具的 `.description`（@tool 之后模型读的是它，不是 __doc__）")

_desc = B.delete_board_comment.description or ""
check("删除工具的说明里写着「待审与被驳回的留言删得掉」",
      "待审与被驳回的留言删得掉" in _desc, _desc[:60])
_src = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
check("  且注明后台清单不过滤 approved（能力有据，不是口头承诺）",
      "不过滤 approved" in _src)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
