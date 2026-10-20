"""权限影子读数的判据（20261018 批 5）：零 LLM、零网络、零真 trace。

**这一件为什么存在**：`eval/authz_shadow_divergence.py` 要回答"把 `AUTHZ_ENFORCE`
打开会拦掉什么"。它读的是**历史 trace**，而历史 trace 不会因为判据写错而变红 ——
一个恒返回 0 的读数与"真的零分歧"**长得一模一样**，而后者正是批 8 的触发条件。
所以这一件拿**合成 trace** 把读数的每一格都打一遍，并给每条正面断言配一条反控。

判据的形状（本仓纪律：正控不红不算数）：
  · ① **字面锁**：`REASON_NO_MANIFEST` 与 `authz.REASON_NO_MANIFEST` 逐字相同；
  · ② **格式往返锁**：读端解析 `Decision.__str__` / `Principal.__str__` —— 那两行**改了
    格式这里就红**。没有它，格式一变读端会**静默**解析出空串、按 reason 的分档全变 0；
  · ③ **正控（核心）**：影子判 denied + 同一工具 checker PASS ⇒ 分歧 1；
  · ④–⑦ 四条反控：BLOCK / 别的工具 PASS / 无影子事件 / 坏文档 ⇒ 分歧 0（且各自证明
    不是"恒 0"——③ 与它们共用同一个入口）；
  · ⑧ **`call` 不算"执行了"**：影子判 denied、该工具只出现在 `call` 事件里 ⇒ 分歧 0。
    这一条锁的是读数的**定义**（拦下的那些也照写 `call`，拿它当"执行了"会让读数恒等于
    分母）。配一条正控：补上 PASS 就变 1；
  · ⑨ **硬拦 scope 不许出现在影子事件里**：正控（塞一条 ⇒ 报出）+ 反控（软 scope ⇒ 不报）
    + **导出锁**（那组 scope 是现取的，不是手抄的：拨 `authz_enforce` ⇒ 它跟着变）；
  · ⑩ **`no_manifest` 两义**：名字在注册表里 / 不在，切开；不给注册表 ⇒ 明说"没切"；
  · ⑪ **退出码**：窗口内零份 trace ⇒ **3**（"没数据"不是"零分歧"）；结构违约 ⇒ 1。

用法：.venv/bin/python tests/test_authz_shadow.py
"""


import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import authz_shadow_divergence as S  # noqa: E402

from agent import authz  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def shadow_ev(tool: str, reason: str = authz.REASON_DENIED, scope: str | None = "read.public",
              role: str | None = "user", uid: int = 7) -> dict:
    return {"node": "execute", "event": S.SHADOW_EVENT, "tool": tool,
            "principal": str(Principal(uid=uid, role=role)),
            "decision": str(authz.Decision(False, reason, scope, tool))}


def check_ev(tool: str, verdict: str = S.VERDICT_PASS) -> dict:
    return {"node": "execute", "event": S.CHECK_EVENT, "tool": tool, "verdict": verdict,
            "reason": "ok" if verdict == S.VERDICT_PASS else "error_frame"}


def call_ev(tool: str) -> dict:
    return {"node": "execute", "event": "call", "name": tool, "args": {}}


def trace(name: str, events: list) -> tuple:
    return (name, {"events": events})


import tempfile  # noqa: E402


def _run_main(argv: list) -> tuple:
    _saved = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["authz_shadow_divergence.py"] + argv
        with redirect_stdout(buf):
            rc = S.main()
    finally:
        sys.argv = _saved
    return rc, buf.getvalue()


def test_authz_shadow():
    print("① 字面锁：读端抄的那个原因码与 authz 里那个逐字相同")
    check("REASON_NO_MANIFEST 同源", S.REASON_NO_MANIFEST == authz.REASON_NO_MANIFEST,
          f"{S.REASON_NO_MANIFEST!r} vs {authz.REASON_NO_MANIFEST!r}")
    check("  （反控）它确实是个非空字符串，不是两边都 None 的假绿",
          isinstance(S.REASON_NO_MANIFEST, str) and bool(S.REASON_NO_MANIFEST))
    print("\n② 格式往返锁：读端解析的就是写侧那两行产出的串")
    _lit = str(authz.Decision(False, "denied", "read.own", "add_favorite"))
    check("`Decision.__str__` 的字面格式没变", _lit
        == "deny add_favorite reason=denied scope=read.own",
          repr(_lit))
    _p = S.parse_decision(_lit)
    check("  解析回四个字段", _p == {"verdict": "deny", "tool": "add_favorite",
                                     "reason": "denied", "scope": "read.own"}, str(_p))
    _p2 = S.parse_decision(str(authz.Decision(False, authz.REASON_NO_MANIFEST, None, "tri")))
    check("  scope=None 落成 `-`、读回空串（不是字面 `-`）",
          _p2["scope"] == "" and _p2["reason"] == "no_manifest", str(_p2))
    _lit_p = str(Principal(uid=0, role=None))
    check("`Principal.__str__` 的字面格式没变", _lit_p == "uid=0 role=? src=body", repr(_lit_p))
    check("  解析回 uid/role/src", S.parse_principal(_lit_p) == {"uid": "0", "role": "?",
                                                                  "src": "body"},
          str(S.parse_principal(_lit_p)))
    check("  （反控）认不出的串 → 全空串、不抛",
          S.parse_decision("完全不是这个形状") == {"verdict": "", "tool": "", "reason": "",
                                                    "scope": ""}
          and S.parse_principal("")["role"] == "")
    print("\n③ 正控（核心）：影子判 denied，而那一件**真的执行成功** ⇒ 分歧 1")
    _rep = S.analyze([trace("a.json", [shadow_ev("list_tags"), check_ev("list_tags")])])
    check("分歧 1 条", _rep["diverged"] == 1, str(_rep["diverged"]))
    check("  分母 1 条（影子拒绝）", _rep["denials"] == 1, str(_rep["denials"]))
    check("  按原因码分档到位",
          _rep["by_reason"].get("denied") == {"denials": 1, "diverged": 1},
          str(_rep["by_reason"]))
    check("  按工具 / 按角色也各有一格",
          _rep["by_tool"].get("list_tags", {}).get("diverged") == 1
          and _rep["by_role"].get("user", {}).get("diverged") == 1)
    print("\n④ 反控：同一工具但 checker 判 BLOCK ⇒ 分歧 0（那条本来就没跑成）")
    _rep = S.analyze([trace("b.json", [shadow_ev("list_tags"),
                                       check_ev("list_tags", "BLOCK")])])
    check("分歧 0", _rep["diverged"] == 0, str(_rep["diverged"]))
    check("  但它仍算一条影子拒绝（没被静默丢掉）", _rep["denials"] == 1, str(_rep["denials"]))
    check("  且计入 not_executed", _rep["not_executed"] == 1, str(_rep["not_executed"]))
    print("\n⑤ 反控：PASS 的是**另一个**工具 ⇒ 分歧 0（工具粒度不许串）")
    _rep = S.analyze([trace("c.json", [shadow_ev("list_tags"), check_ev("list_guestbook")])])
    check("分歧 0", _rep["diverged"] == 0, str(_rep["diverged"]))
    print("\n⑥ 反控：干净 trace（无影子事件）⇒ 全 0，且 with_shadow 也是 0")
    _rep = S.analyze([trace("d.json", [check_ev("list_tags")])])
    check("分歧 0 且分母 0", _rep["diverged"] == 0 and _rep["denials"] == 0)
    check("  with_shadow 0（'扫过但没有'与'没扫到'是两件事，这一格记的是前者）",
          _rep["with_shadow"] == 0 and _rep["traces"] == 1)
    print("\n⑦ 反控：坏文档不炸、也不被当成'零分歧的干净 trace'")
    _rep = S.analyze([("x", None), ("y", {"events": None}), ("z", {"events": "不是列表"}),
                      ("w", {"events": [None, "字符串", {"event": "authz_shadow"}]})])
    check("不抛，且分歧 0", _rep["diverged"] == 0, str(_rep["diverged"]))
    check("  最后那份里那条**没有 decision/principal 的影子事件仍被数进来**"
      "（数得到、读不出 —— 宁多数一条，不少数）", _rep["denials"] == 1, str(_rep["denials"]))
    check("  读不出 reason 的那条单独报数", _rep["unparsed_reason"] == 1,
          str(_rep["unparsed_reason"]))
    print("\n⑧ `call` 不算'执行了'（锁读数的定义：拦下的那些也照写 call）")
    _rep = S.analyze([trace("e.json", [shadow_ev("list_tags"), call_ev("list_tags")])])
    check("只有 call、没有 PASS ⇒ 分歧 0", _rep["diverged"] == 0, str(_rep["diverged"]))
    check("  ⭐ 正控：同一个 trace 补上 PASS ⇒ 变 1（证明上一条不是恒 0）",
          S.analyze([trace("e2.json", [shadow_ev("list_tags"), call_ev("list_tags"),
                                       check_ev("list_tags")])])["diverged"] == 1)
    print("\n⑨ 硬拦 scope 不许出现在影子事件里（现取，不手抄）")
    _saved_enforce = settings.authz_enforce
    try:
        settings.authz_enforce = False                     # shadow 档 = 本读数成立的前提
        hard = frozenset(s for s in authz.ALL_SCOPES if authz.enforcing(s))
        check("正控：shadow 档下确实**有**这么一组 scope（空集会让下面两条变成废话）",
              len(hard) > 0, str(sorted(hard)))
        check("  CLI 现取的那组与它相同（`_hard_scopes()` 不是手抄的常量）",
              S._hard_scopes() == hard, str(sorted(S._hard_scopes() or [])))
        _hard = sorted(hard)[0]
        _rep = S.analyze([trace("f.json", [shadow_ev("x", scope=_hard)])], hard_scopes=hard)
        check("正控：塞一条带硬拦 scope 的影子事件 ⇒ 报出（非空）",
              len(_rep["hard_scope_events"]) == 1, str(_rep["hard_scope_events"]))
        _rep = S.analyze([trace("g.json", [shadow_ev("x", scope="read.public")])],
                         hard_scopes=hard)
        check("  （反控）软 scope ⇒ 不报", _rep["hard_scope_events"] == [], "")
        _rep = S.analyze([trace("h.json", [shadow_ev("x", scope=_hard)])])
        check("  （反控）不给 hard_scopes ⇒ **这一项没验**（不是'没有违规'）",
              _rep["hard_scopes"] is None and _rep["hard_scope_events"] == [])
        settings.authz_enforce = True                      # 反控：拨开关，导出值必须跟着变
        check("  （反控）拨 authz_enforce ⇒ 那组 scope 跟着变（证明它真的在现取）",
              S._hard_scopes() == frozenset(authz.ALL_SCOPES),
              f"{len(S._hard_scopes() or [])} 个 vs ALL_SCOPES {len(authz.ALL_SCOPES)} 个")
    finally:
        settings.authz_enforce = _saved_enforce
    print("\n⑩ no_manifest 两义：'漏声明的工具' vs '名字根本不是工具'")
    _evs = [shadow_ev("circle", reason=authz.REASON_NO_MANIFEST, scope=None),
            shadow_ev("device_oled_draw", reason=authz.REASON_NO_MANIFEST, scope=None)]
    _rep = S.analyze([trace("i.json", _evs)], known_tools={"device_oled_draw"})
    check("名字不在注册表里 ⇒ 归到'解析碎片/幻觉工具名'",
          _rep["no_manifest_phantom"] == {"circle": 1}, str(_rep["no_manifest_phantom"]))
    check("名字在注册表里 ⇒ 归到'真·漏声明'",
          _rep["no_manifest_real"] == {"device_oled_draw": 1}, str(_rep["no_manifest_real"]))
    check("  两半加起来 == no_manifest 那一格（不许漏项）",
          sum(_rep["no_manifest_real"].values()) + sum(_rep["no_manifest_phantom"].values())
          == _rep["by_reason"][S.REASON_NO_MANIFEST]["denials"])
    _rep = S.analyze([trace("j.json", _evs)])
    check("  （反控）不给注册表 ⇒ **没切**（两半都空 + 标记为没验），"
      "不是'两半都是 0'", _rep["known_tools_checked"] is False
          and not _rep["no_manifest_real"] and not _rep["no_manifest_phantom"])
    print("\n⑪ 退出码：'没数据'与'零分歧'必须分开")
    with tempfile.TemporaryDirectory() as _empty:
        rc, out = _run_main(["--root", _empty])
        check("窗口内零份 trace ⇒ 退 3（**不是 0**）", rc == 3, f"rc={rc}")
        check("  而且话说清楚了：'没数据'不是'零分歧'", "账不可用" in out, out.strip()[:60])
    with tempfile.TemporaryDirectory() as _d:
        # 一条**结构违约**的 trace 落盘 ⇒ 退 1（读数照出，但退出码要能当闸用）
        import json as _json
        _p = Path(_d) / "20261018T000000_1_deadbeef.json"
        _p.write_text(_json.dumps({"events": [shadow_ev("x", scope="write.console")],
                                   "end_reason": "producer_done"}, ensure_ascii=False),
                      encoding="utf-8")
        rc, out = _run_main(["--root", _d, "--since", "20261018"])
        check("结构违约（硬拦 scope 进了影子事件）⇒ 退 1", rc == 1, f"rc={rc}")
        check("  读数里点名了那一条", "write.console" in out, "")
    print("\n⑫ 窗口过滤：按文件名时间戳，取不到时间戳的**留着**（宁多看、不少看）")
    with tempfile.TemporaryDirectory() as _d:
        for _n in ("20261001T000000_1_aaaaaaaa.json", "20261018T000000_1_bbbbbbbb.json",
                   "没时间戳的.json"):
            (Path(_d) / _n).write_text("{}", encoding="utf-8")
        _files, _label = S._window_files(_d, 7, "20261018")
        _names = sorted(Path(p).name for p in _files)
        check("--since 20261018 ⇒ 只留当天的 + 没有时间戳的那份",
              _names == ["20261018T000000_1_bbbbbbbb.json", "没时间戳的.json"], str(_names))
        check("  窗口标签如实写出起点", "20261018" in _label, _label)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_authz_shadow()
