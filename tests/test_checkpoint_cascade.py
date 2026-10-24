"""Verify checkpoint deletion, truthful receipts and authenticated host delivery."""


from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(Path(__file__).resolve().parent))


from agent import checkpoint as C  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


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


HOOK = "checkpoint_cleanup::dispatch(&"


def parent_halves(conv_src: str, temp_src: str) -> tuple[str, str]:
    """Locate durable cleanup delivery in both host deletion paths."""
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


def hook_identity(conv_src: str, delivery_src: str | None = None) -> tuple[str, str]:
    """Check service identity headers at both actual HTTP request sites."""
    marks = []
    for source, key in ((delivery_src or conv_src, "async fn request"),
                        (conv_src, "async fn drop_checkpoint_turn")):
        body = _fn_body(source, key)
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
    _CALL = "checkpoint_cleanup::dispatch(&db, &threads).await;"
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
async fn request(client: &Client, base: &str, job: &Job, assertion: &str) {
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
    _ds = _parent_repo.read("src/checkpoint_cleanup.rs", "删除交付必须携带服务身份并验证结果")
    if _rs is None or _ts is None or _ds is None:
        # `_parent_repo` 已经打过那行响亮的说明了（父仓不在场 / SAUDADE_REQUIRE_PARENT=1
        # 时它直接退 1）。这里**如实记成"没验"**，不记成通过——同族：
        # 父仓 `scripts/healthcheck.sh` 第 8 项在判据脚本不在场时写的是"这一项没验"。
        SKIPPED.append("父仓不在场 ⇒ 第 ③ 节没验")
    else:
        _st, _why = parent_halves(_rs, _ts)
        print(f"     父仓：{_st} —— {_why}")
        check("父仓两条删除路径都交付持久清理任务", _st == "两臂都接", _why)
        check("级联清单在业务删除前登记清理任务",
              "checkpoint_cleanup::enqueue(db, row.id, row.user_id)" in _rs)
        # 「清单只有一份」这条性质本身也要判：钩子挪到调用点之后，两个调用点**各自再查一遍**
        # 会话 id 是最容易滑回去的写法（那正是 20261018 那个 bug 的形状）。判据 = 那份清单
        # **把 id 返回给调用方**（签名里有 `Vec<i32>`）。
        check("父仓那份清单**仍然只有一处**：会话 id 由 `purge_conversation_data` 返回给调用方",
              "-> Result<Vec<i32>" in _rs, "签名里看不到 `Vec<i32>` 返回值")
        # 身份那一档（20261020 安全审计 SAU-006）：本仓这两处现在**要求**断言 ⇒ 父仓不带
        # 就是 401，而钩子 best-effort（失败只 warn）⇒ 断了不会响。判**一致性**、不判"已带"
        # 的理由写在 `hook_identity` 的 docstring 里。
        _ist, _iwhy = hook_identity(_rs, _ds)
        print(f"     父仓钩子的身份：{_ist} —— {_iwhy}")
        check("父仓两个清理请求都携带身份断言", _ist == "都带", _iwhy)
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


pytestmark = pytest.mark.usefixtures("service_workers")
