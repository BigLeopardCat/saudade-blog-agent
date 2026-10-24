"""Read reset scopes in archived evaluation reports; live SSE has typed events."""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


from run_golden import fallback_resets, parse_reset  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _test_parse_reset():
    print("\n[读侧] `__RESET__:<scope>:<理由>` 的解析")
    check("all + 理由", parse_reset("__RESET__:all:叙述缺少依据，正在重新查证")
          == ("all", "叙述缺少依据，正在重新查证"), str(parse_reset("__RESET__:all:x")))
    check("text + 理由", parse_reset("__RESET__:text:叙述校验未通过，已替换为如实回复")
          == ("text", "叙述校验未通过，已替换为如实回复"), str(parse_reset("__RESET__:text:x")))
    # 理由里带冒号（中文全角/半角都可能有）⇒ 只切第一段，其余原样
    check("理由里的冒号不被吃掉",
          parse_reset("__RESET__:text:原因: 还有后半句") == ("text", "原因: 还有后半句"),
          str(parse_reset("__RESET__:text:原因: 还有后半句")))
    # 旧帧（裸 `__RESET__` / 带理由但没有 scope 段）：一律按 all = 保守那一侧
    check("裸 __RESET__ → all，理由为空", parse_reset("__RESET__") == ("all", ""),
          str(parse_reset("__RESET__")))
    check("旧帧 `__RESET__:<理由>` → all，且理由整串保留",
          parse_reset("__RESET__:empty_reply") == ("all", "empty_reply"),
          str(parse_reset("__RESET__:empty_reply")))
    # 认不出的 scope 不当成 scope：整串都是理由（否则会把一句中文理由的首段当 scope）
    check("生 scope（如 `__RESET__:gate:理由`）→ all，整串当理由",
          parse_reset("__RESET__:gate:理由") == ("all", "gate:理由"),
          str(parse_reset("__RESET__:gate:理由")))
    check("只有 all / text 两个取值（不另造同义词）",
          parse_reset("__RESET__:replan:x")[0] == "all"
          and parse_reset("__RESET__:fallback:x")[0] == "all")


def _test_frontend_reads_same_scope():
    # Archived evaluation reports still have reset scopes. Live streams have no
    # reset command: model text cannot erase replies or previously queued actions.
    src = (ROOT / "frontend/public/live2d-widgets/chat-stream.js").read_text(encoding="utf-8")
    check("运行时消费具名事件", "__waifuChatProtocol.createStream" in src)
    check("正文不再识别重置控制前缀", "text === '__RESET__'" not in src)
    check("命令缓冲只初始化一次", src.count("programCmds = []") == 1)


def _test_forbid_fallback_reads_scope():
    """同一条键、同一个 `resets` 计数，两个 scope 的结论**相反**。

    旧口径（`if gold["forbid_fallback"] and result["resets"]`）把"打回重规划"与"终局
    兜底"当成同一件事——20261002 全量跑里 5 条 `forbid_fallback` 红有 4 条是假红
    （逐条读 trace：gate 抓的都是真编造，重查后收尾如实），FAIL 文案还断言"用户收到的
    是兜底道歉"这件没发生的事。判据换成 `fallback_resets`（只认 scope=text）。

    反向对照**同时**钉住两端：只留第一条 ⇒ 老口径照样绿；只留第二条 ⇒ 放宽成"一律放行"
    也绿。缺一条这个锁就证明不了任何事（同族教训见 `tests/test_confirm.py` 的变异锁）。
    """
    print("\n[判据] `forbid_fallback` 认的是 scope（all=重规划 / text=终局兜底）")
    check("scope=all（打回 ⇒ planner 重规划）⇒ 不算兜底，放行",
          fallback_resets({"resets": 1, "reset_scopes": ["all"],
                           "resets_reasons": ["叙述缺少依据，正在重新查证"]}) == [])
    _fb = fallback_resets({"resets": 1, "reset_scopes": ["text"],
                           "fallback_reasons": ["叙述校验未通过，已替换为如实回复"]})
    check("scope=text（终局兜底）⇒ 算兜底，要判红", _fb == ["叙述校验未通过，已替换为如实回复"],
          str(_fb))
    check("一轮里两种都有（先打回、重规划后仍不过）⇒ 兜底那一次照样算数",
          fallback_resets({"resets": 2, "reset_scopes": ["all", "text"],
                           "fallback_reasons": ["叙述校验未通过，已替换为如实回复"]})
          == ["叙述校验未通过，已替换为如实回复"])
    # 键缺席 = "不知道是哪种" ⇒ 退回旧口径（按计数），宁严勿松——与 parse_reset 对
    # 缺 scope 段取保守侧同一条取向（老归档的报告就是这个形状）。
    check("没有 reset_scopes 的 result（老归档/合成）⇒ 退回按 resets 计数判红",
          fallback_resets({"resets": 1, "resets_reasons": ["旧帧没有 scope 段"]}) != [])
    check("零 RESET ⇒ 两种形状都放行",
          fallback_resets({"resets": 0}) == [] and fallback_resets({"resets": 0,
                                                                   "reset_scopes": []}) == [])
    # 接线（源码锚定）：run_one 必须**逐条**把 parse_reset 的 scope 收进 reset_scopes，
    # 否则上面这些语义都无从谈起（判据拿不到形状）。三条一起才说明"记账记得是全的"。
    src = (ROOT / "eval/run_golden.py").read_text(encoding="utf-8")
    check("每条 RESET 都记 scope（不是只在 text 那支记）",
          "_scope, _reason = parse_reset(item)" in src
          and "reset_scopes.append(_scope)" in src)
    check("text 那支的逐条理由单独收（不混进 resets_reasons 的下标）",
          "fallback_reasons.append(_reason)" in src)
    check("结果 dict 里带出去了（判据读得到）",
          '"reset_scopes": reset_scopes, "fallback_reasons": fallback_reasons,' in src)


def test_reset_scope():
    for fn in (_test_parse_reset, _test_frontend_reads_same_scope,
               _test_forbid_fallback_reads_scope):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_reset_scope()
