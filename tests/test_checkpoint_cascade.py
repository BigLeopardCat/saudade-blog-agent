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
    ——写成"断言已接"就会在父仓未落地时一直红，而 套件是按目录收集的、
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


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✅ {desc}")
    else:
        FAILED.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


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


import server  # noqa: E402
from config.settings import settings  # noqa: E402


class _Req:
    """`_resolve_principal` 只看 `headers`；断言头缺席 ⇒ 走"开关未强制"的回退分支。

    **本节判的是机制**（删没删掉、读回来确认），与调用者是谁无关——身份那一格
    （20261020 安全审计 SAU-006）在 `tests/test_hardening.py::test_internal_endpoint_identity`
    里单独判（那里真签一条断言）。所以这里**把开关自己钉成 0**：`tests/conftest.py` 的
    `_PINNED` 把出厂档钉成 1，不拨回来的话这一节会**全部 401**、红得看不出原因。
    """

    def __init__(self, headers=None):
        self.headers = headers or {}


def _call(conv_id):
    return asyncio.run(server.checkpoint_delete(conv_id, _Req()))


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


def hook_identity(conv_src: str) -> tuple[str, str]:
    """父仓那两个钩子**带不带身份断言**。三档：`都带` / `都不带` / `半带`。

    为什么判的是**一致性**而不是"已带"：父仓是另一个仓、CI 里的父仓取自它默认分支的
    当前状态（`eval.yml` 那一处 checkout 没写 `ref:`）⇒ 写成"断言已带"，在父仓那一行
    落地之前就是一条**恒红**的判据，而红的套件进不了 CI（同 `parent_halves` 头注记的
    那次）。三档里**只有「半带」是缺陷形状**：两个钩子住在同一个文件、同一套调用约定，
    一个带一个不带，说明有人只改了一半。

    为什么必须有它：agent 侧这两处现在**要求**断言（`_resolve_principal`，SAU-006）
    ⇒ 父仓不带 = 这两条钩子 401 = "删了会话、checkpoint 还躺在库里"，而钩子是
    **best-effort**（失败只 warn）⇒ 这条契约断了**不会响**。

    ⚠️ **认的是头名 `X-Agent-Assertion`，不是 `create_agent_assertion`**：反控实测过
    ——把 `.header("X-Agent-Assertion", &assertion)` 那一行删掉、只留签名那一句，
    判据照样判「都带」。**"签了但没带上"正是这条契约最可能的失效形状**（签名与发送
    隔了十几行），所以字面落在"头发出去"这一端。（父仓另外四处也是这个字面。）
    """
    marks = []
    for key in ("async fn drop_checkpoint_threads", "async fn drop_checkpoint_turn"):
        body = _fn_body(conv_src, key)
        if body is None:
            return "锚点没了", f"父仓里找不到 `{key}`（判据的前提没了）"
        marks.append("X-Agent-Assertion" in body)
    if all(marks):
        return "都带", "两个钩子都随请求发身份断言"
    if not any(marks):
        return "都不带", ("两个钩子都没带身份断言 —— agent 侧已要求断言（SAU-006）⇒ "
                       "这两条今天会 401，而钩子是 best-effort，失败只 warn（不会响）")
    return "半带", ("只有「%s」那个钩子带了断言 —— 同一个文件、同一套调用约定，"
                  "只改了一半" % ("drop_checkpoint_threads" if marks[0]
                                else "drop_checkpoint_turn"))


import _parent_repo  # noqa: E402


def test_checkpoint_cascade():
    SKIPPED: list[str] = []
    print("① 机制：`delete_thread` 删得掉，且返回值是「读回来确认」")
    _s = FakeSaver()
    _s.rows["conv_42"] = {"checkpoint": "上一轮的状态"}
    check("有 checkpoint 的会话 ⇒ 删掉并返回 True", C.delete_thread(_s, 42) is True)
    check("删完真的读不到了", _s.get_tuple({"configurable": {"thread_id": "conv_42"}}) is None)
    check("删的是**会话坐标**那个线程", _s.calls == ["conv_42"], str(_s.calls))
    _s2 = FakeSaver()
    check("删一个本来就不存在的会话 ⇒ 不抛、返回 True（读不到就是读不到）",
          C.delete_thread(_s2, 7) is True)
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
    print()
    print("② 端点 `server.checkpoint_delete`：三条分支各判一次")
    _old_agent = server._agent
    _old_flag = settings.agent_require_assertion
    try:
        settings.agent_require_assertion = False   # 见 `_Req` 的注（测试自己钉，不读环境）
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
        settings.agent_require_assertion = _old_flag
    print()
    print("③ 跨仓契约：父仓那两条删会话数据的路**要么都调钩子、要么都不调**")
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
    _HOOKS_FAKE = """
pub(crate) async fn drop_checkpoint_threads(ids: &[i32], uid: i32) {
    %s
}
pub(crate) async fn drop_checkpoint_turn(conversation_id: i32, text: &str, uid: i32) {
    %s
}
"""
    _HDR = '.header("X-Agent-Assertion", crate::auth_jwt::create_agent_assertion(uid, None))'
    check("（反控）两个钩子都不带断言 ⇒ 判「都不带」",
          hook_identity(_HOOKS_FAKE % ("", ""))[0] == "都不带")
    check("（正控）两个钩子都带 ⇒ 判「都带」",
          hook_identity(_HOOKS_FAKE % (_HDR, _HDR))[0] == "都带")
    check("（反控）只改了一个钩子 ⇒ 当场判「半带」（两个方向都抓）",
          hook_identity(_HOOKS_FAKE % (_HDR, ""))[0] == "半带"
          and hook_identity(_HOOKS_FAKE % ("", _HDR))[0] == "半带")
    check("（正控）钩子函数没了要**报**出来，不许空过",
          hook_identity("fn other() {}")[0] == "锚点没了")
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
        # 身份那一档（20261020 安全审计 SAU-006）：本仓这两处现在**要求**断言 ⇒ 父仓不带
        # 就是 401，而钩子 best-effort（失败只 warn）⇒ 断了不会响。判**一致性**、不判"已带"
        # 的理由写在 `hook_identity` 的 docstring 里。
        _ist, _iwhy = hook_identity(_rs)
        print(f"     父仓钩子的身份：{_ist} —— {_iwhy}")
        check("父仓两个钩子带不带身份断言**一致**（半带＝只改了一半）",
              _ist != "半带", _iwhy)
        if _ist == "都不带":
            print("     · 父仓那一半**还没带断言**：agent 侧已要求（SAU-006）⇒ 这两条钩子今天"
              "会 401，而它们 best-effort、失败只 warn。这是已知缺口（跨仓同批推进），"
              "不是判据失败——同上面那条'只报不判'的理由。")
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

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_checkpoint_cascade()
