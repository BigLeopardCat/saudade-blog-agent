# -*- coding: utf-8 -*-
"""台账年龄标注单测（纯函数、零网络、零 LLM，秒级）。

被测 = `server.annotate_exec_ages()`：给跨轮执行记忆每行的行首时间补一个**系统算好的**
相对年龄（`09-25 00:42（3 小时 20 分前·已过期）`），供规划纪律 6c（现时状态类询问必须
重查）与叙述纪律 22（过期记录不许说「刚才」）使用。

动机是 20260925 的生产实证（trace 20260925T035331）：问"现在服务器怎么了"，台账里最近
一条是 3 小时 11 分前的服务器状态，planner 零工具照抄摘要、narrator 写成"刚才查到的"。
**年龄是事实（系统算），要不要重查是决策（planner 的规则）**——所以这里只锁"算得对"，
决策那半由接线锁（下面最后两节）盯着提示词别被改回旧口径。

契约：
  · 行首 `MM-DD HH:MM` 后面紧跟 `（…前）`，超期再加 `·已过期`（阈值 = server.EXEC_STALE_MINUTES）；
  · 只有月日没有年 ⇒ 超前 6 小时以上按**去年**算（跨年那条记录不然会变成"十一个月后的未来"）；
  · 解析不了的时间戳原样留着（不猜、也不标"刚刚"）；
  · **只进注入**：gate 判据（洞⑦ 台账否认）读的 `_ledger_for_graph` 必须是原文。
"""
import json
import re
import sys
import types
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import server  # noqa: E402
import run_golden as rg  # noqa: E402  （夹具占位符展开：expand_now / build_request）

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


NOW = datetime(2026, 9, 25, 4, 2, 0)
SERVER_LINE = "09-25 00:42 查看服务器状态 — 服务器状态: CPU 30.0%／负载 0.45／内存 60%／磁盘 / 69%"


def test_annotation_semantics():
    print("[标注] 年龄与过期标记")
    check("3 小时 20 分前的记录标出年龄与过期",
          server.annotate_exec_ages(SERVER_LINE, NOW).startswith(
              "09-25 00:42（3 小时 20 分前·已过期） 查看服务器状态"),
          server.annotate_exec_ages(SERVER_LINE, NOW)[:40])
    check("台账原文一字不动地留在标记后面",
          "CPU 30.0%／负载 0.45／内存 60%／磁盘 / 69%" in server.annotate_exec_ages(SERVER_LINE, NOW))
    check("2 分钟前：只给年龄，不标过期",
          server.annotate_exec_ages("09-25 04:00 查看服务健康 — x", NOW)
          == "09-25 04:00（2 分钟前） 查看服务健康 — x")
    check("整点小时不带「0 分」（3 小时整 → 「3 小时前」）",
          "（3 小时前·已过期）" in server.annotate_exec_ages("09-25 01:02 跳转「/about」", NOW))
    check("阈值边界：正好 10 分钟不算过期",
          server.annotate_exec_ages("09-25 03:52 查看服务器状态", NOW) == "09-25 03:52（10 分钟前） 查看服务器状态")
    check("阈值边界：11 分钟算过期",
          server.annotate_exec_ages("09-25 03:51 查看服务器状态", NOW) == "09-25 03:51（11 分钟前·已过期） 查看服务器状态")
    check("不足 1 分钟写「刚刚」", "（刚刚）" in server.annotate_exec_ages("09-25 04:02 屏幕显示「x」", NOW))
    check("跨天写天数", "（2 天前·已过期）" in server.annotate_exec_ages("09-23 04:02 跳转「/talk」", NOW))
    check("阈值常量就是 10 分钟（改它要同步改叙述/规划纪律里那句）",
          server.EXEC_STALE_MINUTES == 10, str(server.EXEC_STALE_MINUTES))


def test_edge_cases():
    print("[边界] 跨年 / 脏行 / 无时间戳")
    check("跨年：1 月 1 日看 12-31 23:50 记成 12 分钟前，不是十一个月后",
          "（12 分钟前·已过期）" in server.annotate_exec_ages(
              "12-31 23:50 发布公告「今晚不许熬夜！」", datetime(2027, 1, 1, 0, 2, 0)))
    got = server.annotate_exec_ages("09-25 00:42 查看服务器状态 — 采样于 2026-09-05 14:06:57", NOW)
    check("正文里的完整日期时间不被当成行首时间（只标一次，秒的部分原样留着）",
          got.count("前") == 1 and "（3 小时 20 分前·已过期） 查看服务器状态" in got
          and "2026-09-05 14:06:57" in got, got)
    check("脏时间戳原样留着（不猜、也不标「刚刚」）",
          server.annotate_exec_ages("09-25 25:00 查看服务器状态", NOW) == "09-25 25:00 查看服务器状态")
    check("没有时间戳的行原样留着（golden 的 executions 就是这样）",
          server.annotate_exec_ages("· 屏幕显示「遨游星河，来见泠月喵」", NOW)
          == "· 屏幕显示「遨游星河，来见泠月喵」")
    check("空串原样返回", server.annotate_exec_ages("", NOW) == "")
    multi = "09-25 00:42 查看服务器状态 — a · 09-25 04:00 查看服务健康 — b"
    got = server.annotate_exec_ages(multi, NOW)
    check("多行各标各的（不把第一行的年龄粘到第二行）",
          "（3 小时 20 分前·已过期）" in got and "（2 分钟前）" in got and "已过期） 查看服务健康" not in got,
          got)


def test_raw_ledger_stays_raw():
    print("[注入边界] 只有注入块带年龄，gate 判据读的原文不带")
    req = types.SimpleNamespace(executions="09-25 00:42 查看服务器状态 — CPU 30.0%／负载 0.45",
                                pending_action="")
    raw = server._ledger_for_graph(req)
    check("_ledger_for_graph 是原文（洞⑦ 台账否认判据按子串核对，不许被标记污染）",
          "（" not in raw["executions"] and raw["executions"].startswith("09-25 00:42 查看服务器状态"),
          raw["executions"][:40])
    block = server._ledger_block(req)
    # 年龄是**相对此刻**算的，而这条台账行的时间是写死的（09-25 00:42）⇒ 断言不许钉在
    # 某一个年龄词上：写这行时是「3 小时 20 分前」，隔一天就成了「1 天前」，钉「小时」
    # 的写法会在某个夜里自己变红（20260926 实测）。判据改成"那一行的时间后面紧跟着一个
    # 带 ·已过期 的括号"——与日期无关，而且比原来那句**更严**（认的是行与标注的绑定，
    # 不是这段文本里出现过「小时」）。
    age = re.search(r"09-25 00:42（[^）]*·已过期）", block)
    check("注入块（_ledger_block）里带上年龄标记（行首时间后紧跟系统算的年龄 + ·已过期）",
          age is not None, age.group(0) if age else block[block.find("已执行"):][:80])
    check("注入块里写明「已过期」的语义与 6c 的指引",
          "已过期" in block and "6c" in block and "刚才" in block)


def test_prompt_wiring():
    print("[接线] 规划纪律 6c 与叙述纪律 22 都在，且与 6b 划清边界")
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("规划纪律有 6c 现时状态类询问", "6c. 现时状态类询问" in src)
    check("6c 要求重新调用同一个数据工具", "必须重新调用**同一个数据工具**" in src)
    check("6c 明确不适用于前端实时状态（current_* 字段）",
          "current_url`/`current_effects`/`current_darkmode` 字段" in src)
    check("6c 明确把取值指代（6b）摘出去", "走 6b" in src)
    check("规则 6 里那句格式说明提到了「（…前）」并指向 6c",
          "是**系统按当前时间算好的**" in src and "见 6c" in src)
    check("叙述纪律 22 管「刚才」措辞", "22. 时间措辞不许模糊过去的距离" in src)
    check("叙述纪律 22 要求照抄系统给的年龄",
          "照抄系统给的年龄" in src.replace("\n", "").replace("    ", ""))
    check("narrator 拿得到台账原文与年龄（注入块里两样都在）",
          "{page_ctx}" in src and "『已执行』行行首时间" in (ROOT / "server.py").read_text(encoding="utf-8"))


def test_fixture_intent():
    """台账时间戳的**两态声明**（20261001）：写死的日期 = 判定随挂钟漂移。

    台账行首时间会按**运行时刻**算年龄，超 10 分钟标「·已过期」，而规划纪律 6c 又规定
    过期读数不能拿来回答"现在怎样"——所以用例里那个时间戳是**语义的一部分**，不能随手
    写死一个日期。20261001 实证：三条 `followup_entity_slot_*`（写于 20260920）的夹具停在
    09-20，被标成「11 天前·已过期」后 planner 有一半的时候去重跑工具，用例红绿两态并存、
    还被复跑按 flake 放行——它本来要锁的"摘要有值就零调用"（6b）被这条无关的过期信号盖掉。
    判据（对台账两个字段逐条扫全量用例，两条通道都要，缺一个字段就是漏一半）：
      · 写了 `{now…}` 占位符 ⇒ 意图"新鲜"，合格（`run_golden.expand_now` 在构造请求时展开）；
      · 写了裸的 `MM-DD HH:MM` ⇒ 必须在 `_note` 里声明是**刻意的过期夹具**（写明
        「永远早于运行时刻」，即 `status_stale_ledger_requery` 那种）；
      · 两种都没有 ⇒ 无时间戳，合格。
    反向锁：全语料至少有一条真在用 `{now…}`（否则 expand_now 是死代码，守卫也就无从谈起）。
    """
    print("[夹具] 台账时间戳必须声明意图（相对占位符 / 刻意过期）")
    golden = json.loads("[" + ",".join(
        ln for ln in (ROOT / "eval" / "golden" / "basic.jsonl")
        .read_text(encoding="utf-8").split("\n") if ln.strip()) + "]")
    ts_rx = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}")
    fields = ("executions", "pending_action")
    used_placeholder: list[str] = []
    offenders: list[str] = []
    for case in golden:
        for rnd in (case["rounds"] if "rounds" in case else [case]):
            ctx = rnd.get("context", {})
            note = rnd.get("gold", {}).get("_note", "")
            for f in fields:
                txt = ctx.get(f, "")
                if not txt:
                    continue
                if "{now" in txt:
                    used_placeholder.append(f"{case['id']}.{f}")
                    continue
                if ts_rx.search(txt) and "永远早于运行时刻" not in note:
                    offenders.append(f"{case['id']}.{f}")
    check("没有裸时间戳（写死的日期必须声明是刻意的过期夹具）", not offenders,
          "、".join(offenders))
    check("至少一条用例在用 {now…} 占位符（否则相对计时是死代码）", bool(used_placeholder),
          "、".join(used_placeholder) or "一条都没有")
    # 能力有测试 ≠ 接线有测试：占位符展开必须真挂在**唯一那个请求构造点**上，
    # 否则夹具里写着 `{now…}` 而模型读到的是字面的「{now-3m}」（比过期更糟）。
    src = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    check("build_request 真的把两个台账字段都过了 expand_now",
          src.count('expand_now(ctx.get("executions"') == 1
          and src.count('expand_now(ctx.get("pending_action"') == 1)
    out = rg.expand_now("· {now-3m} 查看分类 — 5 个分类", datetime(2026, 10, 1, 21, 0, 0))
    check("expand_now 展开成台账行首同形的 MM-DD HH:MM 且按偏移量倒退",
          out == "· 10-01 20:57 查看分类 — 5 个分类", out)
    check("零占位符的文本原样返回（既有 150 条用例零变化）",
          rg.expand_now("· 09-01 12:00 查看服务器状态") == "· 09-01 12:00 查看服务器状态")


def main():
    for fn in (test_annotation_semantics, test_edge_cases,
               test_raw_ledger_stays_raw, test_prompt_wiring, test_fixture_intent):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
