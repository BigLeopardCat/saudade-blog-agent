# -*- coding: utf-8 -*-
"""槽稳定性读数（`eval/slot_stability.py`）的**聚合层**单测。

离线、秒级、零网络、零 LLM——本套件只喂**合成的** RESULT 给那几个纯函数。
真跑（N 遍真对话）是手动的，见那边模块头注；这里判的是"读数算得对不对"。

**为什么这个套件必须存在**：`eval/slot_stability.py` 的存在理由是"把不稳定量出来"。
一个**永远报 1.0** 的读数比没有读数更坏——它会让人以为某个槽是稳的。所以这里判的不是
"它能跑"，而是"**它在该掉下来的时候真的会掉下来**"（第 ② 组的反控），以及
"它拿不到值时不会静默跳过"（第 ③ 组：跳过等于把最该看见的那一遍从分母里删掉）。

用法：.venv/bin/python tests/test_slot_stability.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import retention_manifest as rm  # noqa: E402
import slot_stability as ss  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def _result(specs, *, top=False):
    """造一条 RESULT：`specs` = `[(工具名, {参数}), …]` 装进一张确认载荷。"""
    payload = {"workflow": "dashboard_todo_add",
               "specs": [{"tool": t, "args": a} for t, a in specs]}
    if top:
        return {"id": "x", "ok": True, "confirm_payloads": [payload], "rounds": []}
    return {"id": "x", "ok": True, "rounds": [{"round": 1, "confirm_payloads": [payload]}]}


def test_slot_stability():
    FAILED.clear()
    print("① 取槽：两个产出者的位置都收（顶层与 rounds[]）")
    _v = ss.extract_slot(_result([("create_dashboard_todo", {"text": "亲亲泠月喵"})]),
                         "create_dashboard_todo", "text")
    check("  rounds[].confirm_payloads 里取得到", _v == "亲亲泠月喵", repr(_v))
    _v2 = ss.extract_slot(_result([("create_dashboard_todo", {"text": "A"})], top=True),
                          "create_dashboard_todo", "text")
    check("  顶层 confirm_payloads 里也取得到（两个产出者都写过这个位置）",
          _v2 == "A", repr(_v2))
    check("  工具名对不上 ⇒ 取不到（不许拿别的工具的参来顶）",
          ss.extract_slot(_result([("delete_tag", {"text": "X"})]),
                          "create_dashboard_todo", "text") is None)
    check("  参数缺席 ⇒ 取不到（`None` 不是空串，两者必须分得开）",
          ss.extract_slot(_result([("create_dashboard_todo", {})]),
                          "create_dashboard_todo", "text") is None)
    check("  同工具出现两次 ⇒ 取**第一个**（一张卡只装同一个本领的一件事）",
          ss.extract_slot(_result([("create_dashboard_todo", {"text": "先"}),
                                   ("create_dashboard_todo", {"text": "后"})]),
                          "create_dashboard_todo", "text") == "先")

    print("\n② 聚合：一致率算得对不对，以及**它在该掉的时候真的掉**")
    _same = ss.summarize(["A"] * 12)
    check("  12 遍全一样 ⇒ 1.0", _same["agreement"] == 1.0, str(_same))
    _mostly = ss.summarize(["A"] * 8 + ["B"] * 4)
    check("  8A+4B ⇒ 67%（众数是 A）", abs(_mostly["agreement"] - 8 / 12) < 1e-9, str(_mostly))
    check("  且两个取值都列出来了（不是只留众数）",
          _mostly["values"] == {"A": 8, "B": 4}, str(_mostly["values"]))
    check("  ★ 反控：改掉一遍就会掉下来 ⇒ 这个读数不是恒 1.0 的摆设",
          ss.summarize(["A"] * 11 + ["B"])["agreement"] < 1.0)
    check("  全不一样 ⇒ 1/N（不是 0，也不是 1）",
          abs(ss.summarize(["A", "B"])["agreement"] - 0.5) < 1e-9)

    print("\n③ 缺席**算进分母**，不静默跳过")
    _miss = ss.summarize(["A", "A", ss.MISSING, ss.MISSING])
    check("  2A + 2 缺席 ⇒ 50%，分母是 4 不是 2",
          abs(_miss["agreement"] - 0.5) < 1e-9, str(_miss))
    check("  缺席单独计数（读的人看得见「这一遍根本没产出那个参数」）",
          _miss["missing_n"] == 2, str(_miss["values"]))
    check("  空表 ⇒ agreement 是 None（判不了，不许编一个 1.0 出来）",
          ss.summarize([])["agreement"] is None)
    # 这一条是本套件的**边界声明**，不是缺陷：读数只回答"稳不稳"，不回答"对不对"。
    # 每一遍都抄错是不稳定读数的反面——它稳定地错，必须由 gold 断言抓，不能指望这里。
    check("  边界：12 遍都错得一模一样 ⇒ 仍读 1.0（对错不归本读数管）",
          ss.summarize(["整条原话"] * 12)["agreement"] == 1.0)

    print("\n④ 用例取不到就响亮退出（静默跑空表 = 一份「全绿」的假读数）")
    try:
        ss.load_cases(["根本没有这条用例"])
        check("  未知 id ⇒ SystemExit", False)
    except SystemExit as e:
        check("  未知 id ⇒ SystemExit", "根本没有这条用例" in str(e), str(e)[:60])
    _ids = ss.load_cases(["admin_todo_add_popup"])
    check("  真金标里取得到，且取回的是那一条",
          len(_ids) == 1 and _ids[0]["id"] == "admin_todo_add_popup")
    check("  用例声明的就是待办那条写通道（否则这条读数会恒「缺席」）",
          "dashboard_todo_add" in json.dumps(_ids[0], ensure_ascii=False))

    print("\n⑤ 留档有主：新目录不许变成「未登记」的缺口")
    check("  `slot_stability/**` 归属登记为 slot-stability",
          rm.owner_of("slot_stability/20261020_120000_123.json", rm.DEFAULT_REPORT_ROOT)
          == "slot-stability")
    _cls = [c for c in rm.CLASSES if c["key"] == "slot-stability"]
    check("  而且它**真的**在那张表里（不是靠兜底规则蒙对的）", len(_cls) == 1)
    check("  是 managed 且有执行者 + 保留期（不是挂着没人清）",
          _cls and _cls[0]["status"] == "managed" and (_cls[0].get("rule") or {}).get("keep"))
    check("  报告名与 name_re 对得上（对不上就会被记进 unknown ⇒ 永远不删）",
          bool(rm.SLOT_STABILITY_RE.match("20261020_120000_123.json")))

    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print("\n全部通过")


if __name__ == "__main__":
    test_slot_stability()
