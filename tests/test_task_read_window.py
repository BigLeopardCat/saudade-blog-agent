# -*- coding: utf-8 -*-
"""跨轮台账**读侧**的射程锁（20261013）——"给模型看几行"与"结得掉几行"必须解耦。

**病灶一句话**：Rust `load_agent_tasks` 读回来的那份列表同时喂三个地方——
① 提示词里的注入文本（`server._build_messages` → `render_open_tasks`）、
② 流尾结算（`server.rows_to_settle` → `tasks.advance_by_receipts`）、
③ 续跑判据（`graph._ledger_next` → `tasks.next_actionable`）。
②③ 是**判据**，① 是**排版**。给读侧加窗口，就等于让"哪几件事结得掉账"跟着**提示词的
尺寸**走——而提示词尺寸是主人一句话里挂几件事决定的，不是代码决定的。

**现场（会话 347，20261013 23:07–23:10，站内通知弹了两次）**：主人一口气挂了三件事、
第一轮又新登记了一件 ⇒ 那第 4 行落在"最近 3 行"的窗口外，结算时**看不见它**、自然结
不掉；随后两轮新行挤进来，它又滑进窗口，变成"下一件还没做的事" ⇒ 同一件站内通知
**发了两遍**（`send_user_notice` 没有撤回通道、也没有去重）。窗口拆掉之后，同一份回执
按行配对时能把重复登记的几行**一起**结掉，台账自愈。

**本套件锁的是形状、不是那一次现场**（现场不可复现，形状天天都在）：
读侧不许有窗口、终态过滤必须在 SQL 里、时效还在、排序仍是**最新优先**
（注入端 `render_open_tasks` 取的是前 N 行 ⇒ 排序一改，注入文本就逐字节变、
graph 臂的基线跟着动）。

用法：.venv/bin/python tests/test_task_read_window.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.tasks as T  # noqa: E402
from tests import _parent_repo, _rust_src  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


_RUST = _parent_repo.read(
    "src/routes/chat.rs", "台账读侧的射程（读回来几行 = 结得掉几行，见本文件头注）")
_BODY = _rust_src.fn_body(_RUST or "", "load_agent_tasks")
# ⚠️ `fn_body` 返回的是两个花括号之间的**原样切片**（注释/字符串都在里面，它只在配对
# 花括号时跳过它们）。本文件下面每一条断言都问"代码里有没有 X"，所以一律跑在 `_CODE`
# 上；`_BODY` 只在要**引用原文**时用（例如下面那条注释的反控）。
_CODE = _rust_src.strip_comments(_BODY)

# ══════════════════════════════════════════════════════════════════════════
print("① 正控：取到的是 `load_agent_tasks` 的**函数体**（不是空串、不是隔壁那个函数）")
check("  函数体非空，且含只属于它的两句（查表 + 拼 JSON）",
      bool(_BODY) and "agent_task::Entity::find()" in _BODY and "serde_json::json!" in _BODY)

# ══════════════════════════════════════════════════════════════════════════
print("\n② 读侧**没有** `.limit(...)`（这条是本套件存在的理由）")
check("  `load_agent_tasks` 的**代码**里搜不到 `.limit(`",
      ".limit(" not in _CODE)

# 反控：判据本身认得出窗口。不跑这一条，上面那句在"助手取空了/正则写歪了"时**也是绿的**。
_SYNTH = (
    "async fn load_agent_tasks(db: &DatabaseConnection, conversation_id: i32) -> String {\n"
    "    let rows = agent_task::Entity::find().limit(3).all(db).await.unwrap_or_default();\n"
    "    rows.len().to_string()\n"
    "}\n"
)
check("  反控：把窗口插回合成函数里，同一段判据当场判否（否则②可能只是空转）",
      ".limit(" in _rust_src.fn_body(_SYNTH, "load_agent_tasks"))

# 反控之二：那句"别加回来"的注释里**确实**写着 `.limit(` —— ② 之所以是绿的，是因为
# 断言跑在**去注释后的代码**上，不是因为那行字在文件里不存在（这正是本套件落地时
# 踩到的第一颗钉子：`fn_body` 返回的是原样切片，注释一起交回来）。
check("  反控：注释里那句 `.limit(` 真实存在，只是不算代码",
      "别加回来" in _BODY and ".limit(" in _BODY and ".limit(" not in _CODE)

# ══════════════════════════════════════════════════════════════════════════
print("\n③ 终态过滤**在查询里**，取值与本侧同集合（跨语言契约，两侧各判一次）")
check("  `Column::State.is_in(TASK_OPEN_STATES)` 在 SQL 里，不是取回来再筛",
      re.search(r"Column::State\.is_in\(\s*TASK_OPEN_STATES\s*\)", _CODE) is not None)

_M = re.search(r"const\s+TASK_OPEN_STATES\s*:\s*\[&str;\s*\d+\]\s*=\s*\[([^\]]*)\];",
               _RUST or "")
_RUST_OPEN = [s for s in re.findall(r'"([^"]+)"', _M.group(1))] if _M else []
check("  Rust 那三档 == 本侧 `TASK_OPEN_STATES`",
      _RUST_OPEN == list(T.TASK_OPEN_STATES), f"{_RUST_OPEN} vs {list(T.TASK_OPEN_STATES)}")
check("  本侧 `render_open_tasks` 用的也是同一组（第二道，读脏数据时兜底）",
      "TASK_OPEN_STATES" in (T.render_open_tasks.__doc__ or "")
      and set(T.TASK_OPEN_STATES) == {"submitted", "running", "input_required"})

# ══════════════════════════════════════════════════════════════════════════
print("\n④ 拆窗口 ≠ 取消时效：72h 那道还在")
check("  查询里留着 `CreatedAt.gte(cutoff)`，cutoff 由 `TASK_READ_TTL_HOURS` 算",
      "Column::CreatedAt.gte(cutoff)" in _CODE and "TASK_READ_TTL_HOURS" in _CODE)

# ══════════════════════════════════════════════════════════════════════════
print("\n⑤ 排序仍是**最新优先**（注入文本靠它才逐字节不变）")
check("  `order_by_desc(Column::Id)` 是读侧唯一的排序",
      "order_by_desc(agent_task::Column::Id)" in _CODE
      and "order_by_asc" not in _CODE)

# ══════════════════════════════════════════════════════════════════════════
print("\n⑥ 解耦的落地形状：`limit` 截**文本**，不截**判据**")
# 三行正等着主人回话（`pending_question` 非空 ⇒ `next_actionable` 按设计跳过），
# 第 4 行才是唯一现在就能动手的——正是现场那个形状。
_WAITING = [
    {"task_id": f"at_w{i}", "goal": "给他降成什么？", "state": "input_required",
     "steps": [{"label": "降级", "tool": "set_account_role"}],
     "total_steps": 1, "cursor": 0, "pending_question": "降成哪一档？"}
    for i in (1, 2, 3)
]
_FREE = {"task_id": "at_free", "goal": "以你的口吻通知他", "state": "running",
         "steps": [{"label": "发通知", "tool": "send_user_notice"}],
         "total_steps": 1, "cursor": 0}
_ROWS = _WAITING + [_FREE]

_txt = T.render_open_tasks(_ROWS, limit=3)
check("  4 行进、`limit=3` ⇒ 文本里只有 3 行（第 4 行**不**印给模型看）",
      "at_free" not in _txt and _txt.count("· at_w") == 3, f"{_txt.count('· at_w')} 行")
_pick = T.next_actionable(_ROWS, [])
check("  正控：同一份台账里，第 4 行对**判据**是可见的（读侧不设窗口的意义就在这）",
      _pick is not None and str(_pick[0].get("task_id")) == "at_free",
      f"{_pick[0].get('task_id') if _pick else None}")

# 反控（真反控，不是同义反复）：把台账**只截成前 3 行**——也就是旧读侧交回来的形状
# ——判据当场丢失第 4 行。这一条才是"那次通知发了两遍"的成因复现。
check("  反控：台账只给前 3 行（旧读侧的形状）⇒ 判据当面丢失第 4 行",
      T.next_actionable(_ROWS[:3], []) is None)
check("  而注入端的 `limit` 调大到 9 只是**多印一行**，与判定无关",
      "at_free" in T.render_open_tasks(_ROWS, limit=9)
      and str(T.next_actionable(_ROWS, [])[0].get("task_id")) == "at_free")

# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
