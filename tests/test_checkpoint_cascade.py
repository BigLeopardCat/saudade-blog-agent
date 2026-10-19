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
    ——写成"断言已接"就会在父仓未落地时一直红，而 `tests/run_all.py` 按磁盘枚举、
    **没有"预期红"这个档**（同 `tests/test_checkpoint_adoption.py` 头注记的那次）。
    判的是**父仓那两条删会话数据的路的一致**：会话臂 `delete_conversation` 与账号臂
    `delete_temp_user`，钩子**要么两臂都调、要么两臂都不调**。
    这不是编出来的判据——父仓自己在那份头注里写着，它上一版正是**两处各写一遍、
    写的不是同一份**（`delete_conversation` 删五张、`delete_temp_user` 只删三张），
    漏掉的三张表因此变成孤儿。**半接就是那个 bug 的形状。**
    （20261019 批 D 父仓接上了；同一批把锚点从"清单函数那两个 match 臂"挪到"两个调用点的
    函数体"，理由写在 `parent_halves` 的 docstring 里。）

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
print("③ 跨仓契约：父仓那两条删会话数据的路**要么都调钩子、要么都不调**")


def _fn_body(src: str, key: str) -> str | None:
    """某个函数的函数体（花括号配对）。找不到 ⇒ `None`（= 锚点没了）。"""
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


# 钩子的**函数名 + 调用形状**。带 `(&` 是为了把「调用」与「它自己的定义」分开——
# 定义那一行长这样：`pub(crate) async fn drop_checkpoint_threads(ids: &[i32])`。
HOOK = "drop_checkpoint_threads(&"


def parent_halves(conv_src: str, temp_src: str) -> tuple[str, str]:
    """`(形态, 人话)`。形态三档：`两臂都接` / `两臂都没接` / `半接`（只有一臂）。

    两臂 = 父仓那两条会删掉会话数据的路：
      · **会话臂** `delete_conversation`（`conversation.rs`，删一个会话）；
      · **账号臂** `delete_temp_user`（`temp_user.rs`，删账号时级联清它名下全部会话）。

    ⚠️ 20261019 批 D **换了锚点**（从"清单函数那两个 match 臂"改成"这两个调用点的函数体"）：
    接线那一刻落定了一个更靠外的约束——**HTTP 钩子不能进 DB 事务**（`delete_conversation`
    要把它放在 `commit()` 之后）。于是钩子由两个调用点各自发，id 仍由那份清单**返回**给它们
    （清单只有一份这条性质没动，`_conv_ids_from_one_place` 判它）。老锚点在新形状下会
    恒判「两臂都没接」——**一条恒绿的判据**，比没有更坏。
    """
    purge = _fn_body(conv_src, "async fn purge_conversation_data")
    if purge is None:
        return "锚点没了", "父仓里找不到 `purge_conversation_data`（判据的前提没了）"
    if "ConvScope::UserId(" not in purge:
        return "锚点没了", "那份清单里找不到 `ConvScope::UserId(`（两臂的形状变了）"
    a = _fn_body(conv_src, "async fn delete_conversation")
    b = _fn_body(temp_src, "async fn delete_temp_user")
    if a is None or b is None:
        return "锚点没了", ("父仓那两个调用点找不全（delete_conversation="
                          f"{'在' if a else '没了'} / delete_temp_user="
                          f"{'在' if b else '没了'}）")
    ha, hb = HOOK in a, HOOK in b
    if ha and hb:
        return "两臂都接", "两条路都调了 checkpoint 钩子"
    if not ha and not hb:
        return "两臂都没接", "两条路都没调 checkpoint 钩子"
    return "半接", (f"只有「{'会话' if ha else '账号'}」那一臂调了钩子 —— "
                  "这正是父仓自己记过的那个 bug 的形状（清单两处各写一遍、写的不是同一份）")


# 正控/反控用**合成源码**，不依赖父仓在不在（本仓纪律：判据的前提要自己摆出来）。
_CONV_FAKE = """
async fn purge_conversation_data<C>(db: &C, scope: ConvScope) -> Result<Vec<i32>, DbErr> {
    match scope {
        ConvScope::ConversationId(id) => {
            chat_history::Entity::delete_many().exec(db).await?;
            Ok(vec![id])
        }
        ConvScope::UserId(uid) => {
            chat_history::Entity::delete_many().exec(db).await?;
            Ok(conv_ids)
        }
    }
}
pub(crate) async fn drop_checkpoint_threads(ids: &[i32]) { /* 定义，不是调用 */ }
async fn delete_conversation() {
    %s
}
"""
_TEMP_FAKE = """
async fn delete_temp_user() {
    %s
}
"""
_CALL = "drop_checkpoint_threads(&threads).await;"

check("（反控）两臂都没接 ⇒ 判「两臂都没接」（不是含糊的'未接'）",
      parent_halves(_CONV_FAKE % "", _TEMP_FAKE % "")[0] == "两臂都没接")
check("（反控）**只有一臂接了** ⇒ 当场判「半接」",
      parent_halves(_CONV_FAKE % _CALL, _TEMP_FAKE % "")[0] == "半接",
      parent_halves(_CONV_FAKE % _CALL, _TEMP_FAKE % "")[1])
check("（反控）另一臂单独接 ⇒ 同样判「半接」（两个方向都抓）",
      parent_halves(_CONV_FAKE % "", _TEMP_FAKE % _CALL)[0] == "半接")
check("（正控）两臂都接 ⇒ 「两臂都接」",
      parent_halves(_CONV_FAKE % _CALL, _TEMP_FAKE % _CALL)[0] == "两臂都接")
check("（反控）**只有定义、没有调用** ⇒ 仍判「两臂都没接」（别把函数名当接线）",
      parent_halves(_CONV_FAKE % "", _TEMP_FAKE % "")[0] == "两臂都没接",
      "`_CONV_FAKE` 里那个 `drop_checkpoint_threads` 的定义就在那儿")
check("（正控）锚点没了要**报**出来，不许空过",
      parent_halves("fn other() {}", _TEMP_FAKE % _CALL)[0] == "锚点没了")
check("（正控）调用点之一没了也要报（不许当成'没接'——那会变成一条恒绿的判据）",
      parent_halves(_CONV_FAKE % _CALL, "fn other() {}")[0] == "锚点没了")

import _parent_repo  # noqa: E402

_rs = _parent_repo.read(
    "src/routes/conversation.rs",
    "本仓的 DELETE /checkpoint/{id} 钩子必须由父仓的级联清单调用（否则删了会话、"
    "checkpoint 还躺在库里，且没有任何读取路径）")
_ts = _parent_repo.read(
    "src/routes/temp_user.rs",
    "同上，**账号那一臂**（删账号时级联清它名下全部会话）也必须调那个钩子——"
    "只有一臂接了就是父仓自己记过的那个 bug 的形状")
if _rs is None or _ts is None:
    # `_parent_repo` 已经打过那行响亮的说明了（父仓不在场 / SAUDADE_REQUIRE_PARENT=1
    # 时它直接退 1）。这里**如实记成"没验"**，不记成通过——同族：
    # 父仓 `scripts/healthcheck.sh` 第 8 项在判据脚本不在场时写的是"这一项没验"。
    SKIPPED.append("父仓不在场 ⇒ 第 ③ 节没验")
else:
    _st, _why = parent_halves(_rs, _ts)
    print(f"     父仓：{_st} —— {_why}")
    check("父仓那两臂形态一致（**半接就是本件要抓的那一件事**）",
          _st != "半接", _why)
    # 「清单只有一份」这条性质本身也要判：钩子挪到调用点之后，两个调用点**各自再查一遍**
    # 会话 id 是最容易滑回去的写法（那正是 20261018 那个 bug 的形状）。判据 = 那份清单
    # **把 id 返回给调用方**（签名里有 `Vec<i32>`）。
    check("父仓那份清单**仍然只有一处**：会话 id 由 `purge_conversation_data` 返回给调用方",
          "-> Result<Vec<i32>" in _rs, "签名里看不到 `Vec<i32>` 返回值")
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
