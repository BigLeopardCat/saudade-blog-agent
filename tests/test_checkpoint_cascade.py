# -*- coding: utf-8 -*-
"""级联清理的判据（20261018 批 7）：零 LLM、零网络、零真库。

**这一件为什么存在**：挂了 saver 之后，一个会话的对话正文会**多出一份**住在
checkpoint 库里（`conv_<id>` 线程）。删会话时不清它 ⇒ 那份正文**没有任何读取路径**，
也**没有任何东西会红**——同族的先例在父仓 `src/routes/conversation.rs` 的头注里写着：
`execution_log` / `pending_action` / `agent_task` 都栽过同一件事（"会话没了，行还在，
没人读、也没人报"）。

**本件判三件事**：
  · ① **机制**（`agent/checkpoint.py::delete_thread`）：删得掉，且返回值是**"删完读回来
    确认"**而不是"没抛异常"——一个恒不生效的删除与真删除**长得一模一样**，这是本件
    唯一真正难判的地方，所以它有一条专门的反控（no-op saver ⇒ 必须返回 False）；
  · ② **端点**（`server.checkpoint_delete`）：三条分支各判一次（没挂 saver / 会话 id
    非法 / 正常），外加"端点不许把没删掉报成删掉了"的反控；
  · ③ **跨仓契约**：删除动作在父仓，本仓只出钩子。⚠️ **这一条判的不是"父仓接了吗"**
    ——父仓今天**还没接**，写成"断言已接"就会一直红，而 `tests/run_all.py` 按磁盘枚举、
    **没有"预期红"这个档**（同 `tests/test_checkpoint_adoption.py` 头注记的那次）。
    判的是**父仓自己那两臂的一致**：`purge_conversation_data` 有两个臂
    （`ConversationId` / `UserId`），钩子**要么两臂都调、要么两臂都不调**。
    这不是编出来的判据——父仓自己在那份头注里写着，它上一版正是**两处各写一遍、
    写的不是同一份**（`delete_conversation` 删五张、`delete_temp_user` 只删三张），
    漏掉的三张表因此变成孤儿。**半接就是那个 bug 的形状。**

用法：`SAUDADE_PARENT_REPO=<父仓> .venv/bin/python tests/test_checkpoint_cascade.py`
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import checkpoint as C  # noqa: E402

FAILED: list[str] = []
SKIPPED: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✅ {desc}")
    else:
        FAILED.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


# ── 假 saver（不连库） ───────────────────────────────────────────────────────
class FakeSaver:
    """一份最小的 checkpoint 库：一个 `thread_id → 行` 的字典。

    只实现 `delete_thread` 与 `get_tuple` —— 正是 `C.delete_thread` 用到的两个
    （`tests/test_checkpoint_factory.py` ④b 已经把"真 saver 造不出来时响亮抛"判过了，
    所以这里不需要真 saver，也就不需要库）。
    """

    def __init__(self, *, noop: bool = False):
        self.rows: dict[str, object] = {}
        self.calls: list[str] = []
        self.noop = noop              # 反控用：假装删了、其实一行没动

    def delete_thread(self, tid: str) -> None:
        self.calls.append(tid)
        if not self.noop:
            self.rows.pop(tid, None)

    def get_tuple(self, config: dict):
        return self.rows.get(config["configurable"]["thread_id"])


print("① 机制：`delete_thread` 删得掉，且返回值是「读回来确认」")
_s = FakeSaver()
_s.rows["conv_42"] = {"checkpoint": "上一轮的状态"}
check("有 checkpoint 的会话 ⇒ 删掉并返回 True", C.delete_thread(_s, 42) is True)
check("删完真的读不到了", _s.get_tuple({"configurable": {"thread_id": "conv_42"}}) is None)
check("删的是**会话坐标**那个线程", _s.calls == ["conv_42"], str(_s.calls))

_s2 = FakeSaver()
check("删一个本来就不存在的会话 ⇒ 不抛、返回 True（读不到就是读不到）",
      C.delete_thread(_s2, 7) is True)

# 反控：这一条是本件的核心——**"删了没生效"必须判得出来**。
# `PostgresSaver.delete_thread` 是 `DELETE ... WHERE thread_id = %s`，删不存在的线程
# 同样不抛 ⇒ 只看"没抛"的话，一个恒不生效的删除与真删除**长得一模一样**。
_s3 = FakeSaver(noop=True)
_s3.rows["conv_42"] = {"checkpoint": "还在"}
check("（反控）删了没生效的 saver ⇒ **必须返回 False**",
      C.delete_thread(_s3, 42) is False,
      "返回了 True ⇒ 这条判据只会恒真，等于没判")

_s4 = FakeSaver()
_s4.rows["conv_None"] = {"checkpoint": "别人的"}
for _bad in (None, "", "   ", 0, "0", "None"):
    check(f"会话 id={_bad!r} ⇒ False 且**一次 saver 调用都不发**",
          C.delete_thread(_s4, _bad) is False and _s4.calls == [],
          f"{_s4.calls}")
check("（对照）上面那组不是恒 False：合法的 id 走得到 saver",
      C.delete_thread(_s4, 1) is True and _s4.calls == ["conv_1"], str(_s4.calls))

# ── 端点 ─────────────────────────────────────────────────────────────────────
print()
print("② 端点 `server.checkpoint_delete`：三条分支各判一次")

import server  # noqa: E402


def _call(conv_id):
    return asyncio.run(server.checkpoint_delete(conv_id))


_old_agent = server._agent
try:
    server._agent = None                     # 没挂 saver（= 今天生产）
    _r = _call("42")
    check("没挂 saver ⇒ `deleted=false` 且点名 `no_saver`（不假装成功）",
          _r.get("deleted") is False and _r.get("why") == "no_saver", str(_r))

    server._agent = FakeSaver()              # 挂上（`probe.saver_of` 认 `saver` 属性）
    server._agent.saver = server._agent
    _r = _call("0")
    check("会话 id 非法 ⇒ `bad_conversation_id`", _r.get("why") == "bad_conversation_id",
          str(_r))

    server._agent.rows["conv_9"] = {"checkpoint": "x"}
    _r = _call("9")
    check("正常 ⇒ `deleted=true`", _r.get("deleted") is True, str(_r))

    # 反控：端点**不许**把"没删掉"报成"删掉了"。
    _n = FakeSaver(noop=True)
    _n.saver = _n
    _n.rows["conv_9"] = {"checkpoint": "还在"}
    server._agent = _n
    _r = _call("9")
    check("（反控）delete 没生效 ⇒ 端点必须回 `deleted=false` / `still_readable`",
          _r.get("deleted") is False and _r.get("why") == "still_readable", str(_r))
finally:
    server._agent = _old_agent

# ── 跨仓契约 ─────────────────────────────────────────────────────────────────
print()
print("③ 跨仓契约：父仓 `purge_conversation_data` 的两臂**要么都调钩子、要么都不调**")


def _purge_body(src: str) -> str | None:
    """父仓那份级联清单的函数体（花括号配对）。找不到 ⇒ `None`（= 锚点没了）。"""
    key = "async fn purge_conversation_data"
    i = src.find(key)
    if i < 0:
        return None
    j = src.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    return None


HOOK = "checkpoint"


def parent_halves(src: str) -> tuple[str, str]:
    """`(形态, 人话)`。形态三档：`两臂都接` / `两臂都没接` / `半接`（只有一臂）。"""
    body = _purge_body(src)
    if body is None:
        return "锚点没了", "父仓里找不到 `purge_conversation_data`（判据的前提没了）"
    split = body.find("ConvScope::UserId(")
    if split < 0:
        return "锚点没了", "那份清单里找不到 `ConvScope::UserId(`（两臂的形状变了）"
    a, b = body[:split], body[split:]
    ha, hb = HOOK in a, HOOK in b
    if ha and hb:
        return "两臂都接", "两个臂都调了 checkpoint 钩子"
    if not ha and not hb:
        return "两臂都没接", "两个臂都没调 checkpoint 钩子"
    return "半接", (f"只有 {'ConversationId' if ha else 'UserId'} 那一臂调了钩子 —— "
                  "这正是父仓自己记过的那个 bug 的形状（清单两处各写一遍、写的不是同一份）")


# 正控/反控用**合成源码**，不依赖父仓在不在（本仓纪律：判据的前提要自己摆出来）。
_FAKE = """
async fn purge_conversation_data<C>(db: &C, scope: ConvScope) -> Result<(), DbErr> {
    match scope {
        ConvScope::ConversationId(id) => {
            chat_history::Entity::delete_many().exec(db).await?;
            %s
        }
        ConvScope::UserId(uid) => {
            chat_history::Entity::delete_many().exec(db).await?;
            %s
        }
    }
    Ok(())
}
"""
check("（反控）两臂都没接 ⇒ 判「两臂都没接」（不是含糊的'未接'）",
      parent_halves(_FAKE % ("", ""))[0] == "两臂都没接")
check("（反控）**只有一臂接了** ⇒ 当场判「半接」",
      parent_halves(_FAKE % (f'delete(".../{HOOK}/x")', ""))[0] == "半接",
      parent_halves(_FAKE % (f'delete(".../{HOOK}/x")', ""))[1])
check("（反控）另一臂单独接 ⇒ 同样判「半接」（两个方向都抓）",
      parent_halves(_FAKE % ("", f'delete(".../{HOOK}/x")'))[0] == "半接")
check("（正控）两臂都接 ⇒ 「两臂都接」",
      parent_halves(_FAKE % (f'"{HOOK}"', f'"{HOOK}"'))[0] == "两臂都接")
check("（正控）锚点没了要**报**出来，不许空过",
      parent_halves("fn other() {}")[0] == "锚点没了")

import _parent_repo  # noqa: E402

_rs = _parent_repo.read(
    "src/routes/conversation.rs",
    "本仓的 DELETE /checkpoint/{id} 钩子必须由父仓的级联清单调用（否则删了会话、"
    "checkpoint 还躺在库里，且没有任何读取路径）")
if _rs is None:
    # `_parent_repo` 已经打过那行响亮的说明了（父仓不在场 / SAUDADE_REQUIRE_PARENT=1
    # 时它直接退 1）。这里**如实记成"没验"**，不记成通过——同族：
    # 父仓 `scripts/healthcheck.sh` 第 8 项在判据脚本不在场时写的是"这一项没验"。
    SKIPPED.append("父仓不在场 ⇒ 第 ③ 节没验")
else:
    _st, _why = parent_halves(_rs)
    print(f"     父仓：{_st} —— {_why}")
    check("父仓那两臂形态一致（**半接就是本件要抓的那一件事**）",
          _st != "半接", _why)
    if _st == "两臂都没接":
        print("     · 父仓那一半**还没接上**：本仓的钩子今天没有任何调用方。"
              "这是已知缺口（roadmap 批 7 记着），不是判据失败——本件判的是"
              "「父仓自己两臂一致」，因为「断言父仓已接」会一直红、"
              "而红的套件进不了 CI（同 tests/test_checkpoint_adoption.py 头注）")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过" + (f"（{SKIPPED[0]}）" if SKIPPED else ""))
sys.exit(0)
