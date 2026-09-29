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
check("  两条通道**各自**的指认方式都写清了（复核按编号、删除按原话）",
      "按 `talkId` 指认" in rep and "按**正文原话**指认" in rep
      and "别混用" in rep)
check("  明说复核这条通道只治还在待审的那几条（不承诺办已复核过的）",
      "还在待审" in rep and "后台留言管理页" in rep)
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
# 审核 20260929 起改走**台账编号**通道（批 H · S2）：目标不再是正文片段，所以
# 「按现场台账里的编号认、不许自己编」必须写在给模型读的那份描述里；删除那半未动。
check("（对照）审核技能改按**台账编号**指认，且明说编号不许自己编",
      "talk_id" in _a.inputs and "talk_id" in str(_a.plan)
      and "编号不许自己编" in _a.description)
check("  不再收正文片段（quote 已经不在它的参数里）", "quote" not in _a.inputs)

# ══════════════════════════════════════════════════════════════════
print("\n③ 工具的 `.description`（@tool 之后模型读的是它，不是 __doc__）")

_desc = B.delete_board_comment.description or ""
check("删除工具的说明里写着「待审与被驳回的留言删得掉」",
      "待审与被驳回的留言删得掉" in _desc, _desc[:60])
_src = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
check("  且注明后台清单不过滤 approved（能力有据，不是口头承诺）",
      "不过滤 approved" in _src)

# ══════════════════════════════════════════════════════════════════
print("\n④ 待办族：能力清单是**能力的上界**——清单里每一句都得有真通道，通道有的也得写得出")

from agent import context as C  # noqa: E402

# 这一节的由来（批 G 的 D3，trace `20260929T193152`）：主人问「猫咪今日待办有什么」
# ——**只读**的一轮——narrator 在末尾主动提议「要不要我帮你勾掉或者**重新排个日子**呀？」。
# 当时待办族只有「加一条」与「勾完成」，改日期**没有通道**；它是把 `dashboard_todo_add`
# 的 capability「加一条（可带排期日）」里的**参数**读成了**动作**。
# 一句文案能造成这种误读，是因为清单是**唯一**的承诺来源（planner 与 narrator 读同一份）
# ⇒ 判据也就只能落在这里：清单里说的每件事都得能兑现，做不到的别写得像能做。

_TODO_WRITES = sorted(n for n in _SK
                      if n.startswith("dashboard_todo_") and n in S.WRITE_SKILL_NAMES)
check("待办族的窄写恰好三件（再加一条窄写必须回来看这一节：下面的对称判据只对这三件成立）",
      _TODO_WRITES == ["dashboard_todo_add", "dashboard_todo_done",
                       "dashboard_todo_reschedule"], str(_TODO_WRITES))
check("  只剩读那件不在写名单里（读写名单不许串）",
      "dashboard_todo_list" not in S.WRITE_SKILL_NAMES)


def _tool_args(skill_name: str) -> list[str]:
    """技能模板里那件工具**真实**收哪些参数（从注册表取，不读文案）。"""
    tool = (_SK[skill_name].plan or [(None, None)])[0][0]
    t = next((x for x in B._TOOL_REGISTRY if x.name == tool), None)
    check(f"  {skill_name} 模板里的 {tool} 在注册表里（清单承诺的动作有真工具）", t is not None)
    return sorted(t.args or {}) if t is not None else []


_add_cap = _SK["dashboard_todo_add"].capability
_rsc_cap = _SK["dashboard_todo_reschedule"].capability
_done_cap = _SK["dashboard_todo_done"].capability

check("★ 加待办那件：提到排期时必须带上「新建」这个时机（旧文案只说了参数、没说时机，"
      "于是被读成了「能改日子」）",
      "排期" not in _add_cap or "新建" in _add_cap, _add_cap)
check("  且那句旧文案一字不留（它是 D3 的原文，回来即红）",
      "（可带排期日）" not in _add_cap, _add_cap)
check("  这份「能提排期」的资格**有据**：它模板里的工具真的收 `date`",
      "date" in _tool_args("dashboard_todo_add"), str(_tool_args("dashboard_todo_add")))

check("★ 改排期那件与它**对称**：一个说「新建时」、一个说「已有那一条」"
      "（两句一起读，日子归哪一件没有缝）",
      "新建" in _add_cap and "已有" in _rsc_cap, f"{_add_cap} / {_rsc_cap}")
check("  两件不许互相冒充（各自的动作词不进对方那一句）",
      "改掉" not in _add_cap and "加一条" not in _rsc_cap,
      f"{_add_cap} / {_rsc_cap}")
check("  ★ 勾完成那件**根本不该提排期**：它模板里的工具只收正文（提了就是又一次"
      "把参数说成能力）",
      "排期" not in _done_cap and _tool_args("dashboard_todo_done") == ["text"], _done_cap)
check("  改排期那件「能提排期」同样有据（工具收 `date`）",
      "date" in _tool_args("dashboard_todo_reschedule"))
check("  ★ 改排期那件承诺的那件事**有真通道**（P4 落地之前，清单里这句话是不能说的）",
      bool(_SK["dashboard_todo_reschedule"].plan)
      and _SK["dashboard_todo_reschedule"].plan[0][0] == "reschedule_dashboard_todo",
      str(_SK["dashboard_todo_reschedule"].plan))

_in_guide = any(c in C.site_guide("admin") for c in (_add_cap, _done_cap, _rsc_cap))
check("★ 三句 capability 都在管理员那份清单里（改在被渲染出来的那一份里才算数）",
      _in_guide, _rsc_cap)

# ── 收束句（对偶两句）─────────────────────────────────────────────────────
# 第一句防「漏列」（说「我不能改后台」），第二句防「超纲」（说「我可以重新排个日子」）。
# 两句话都在**同一个渲染点**，planner 与 narrator 各拿一次 ⇒ 一处覆盖两个节点。
check("★ 收束句在位，且是清单的**最后一句**（埋在中间等于没写）",
      C.site_guide("admin").endswith(C._SITE_GUIDE_CLOSING)
      and C.site_guide(None).endswith(C._SITE_GUIDE_CLOSING))
check("  对偶第二句写的是「清单之外的动作一律不许承诺」（D3 那句提议的判据形态）",
      "清单之外的动作一律不许承诺" in C._SITE_GUIDE_CLOSING)
check("  第一句（防漏列）仍在——两句并列，不许拿第二句换掉第一句",
      "按此完整列出，不要遗漏" in C._SITE_GUIDE_CLOSING)
# 诚实标注：这句话是**提示词级**，不是闸门。标注本身也要有锁，否则下一个人会把它
# 读成「已经有判据了」，然后在别处再补一条正则（本仓 20260928 审计点名的那个形状）。
_ctx_src = (ROOT / "agent" / "context.py").read_text(encoding="utf-8")
check("  且诚实标注在位：这是提示词级、不是闸门（判据分不出诚实与胡说的「做不到」）",
      "这是提示词级，不是闸门" in _ctx_src)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
