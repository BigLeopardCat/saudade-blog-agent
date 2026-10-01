# -*- coding: utf-8 -*-
"""`__RESET__` 的 scope 契约（20261001）：命令缓冲该不该跟着一起作废。

## 为什么这条判据存在

`__CMD__` 帧是 checker PASS 之后下发的**已发生事实**（execute 真跑过）。
而 `__RESET__` 此前只有一种含义：连它一起作废。两个方向都实测过，都有害：

  · **无条件清** ⇒ 终局 fallback 之后，气泡最前面那块**系统自己印的**事实块写着
    「页面已跳转：…」，而命令已经被清掉、页面根本没跳——**系统说了它没做的事**
    （20261001 夜间 `nav_article_target` 实证：首跑 narrator 空内容 ⇒ 打回 ⇒ 主人
    读到"已跳转"、页面纹丝不动）。代价还有第二层：gate 的两条声称判据（5g/5h）
    当时都因为"判死就等于把已生效的命令吞掉"而**降级成只记不判**——一条真实的
    幻觉也就跟着漏过去了。
  · **无条件不清** ⇒ gate 打回重规划那一格：决策已经被推翻、新一轮要重新决定做什么，
    旧命令却照旧执行，主人看到的是"它道歉了但还是跳了"。

所以这不是取舍，是**两个不同的格**：帧形改成 `__RESET__:<scope>:<理由>`——
`all`（决策被推翻）清、`text`（决策没被推翻、只否掉措辞）不清。

## 缺省值为什么必须是 `all`

三端（Python / Rust / 前端）版本错配时按哪个走，是要选一侧的：缺省取 `all`，
错配退化成"命令被吞"（少跳一次、主人看得见事实块会去问），而不是"道歉了还是跳了"
（静默地把一个被否定的动作执行掉）。**这一侧不许改**。

## 判据落在哪

  · `parse_reset` 的行为（golden 读侧的唯一实现，纯函数）；
  · `server.py` 两个调用点的 scope 各归各（源码锚定 + 顺序断言）；
  · 前端读的是**同一份字面**（跨端守卫，父仓源码；跑不到时按 `_parent_repo` 的三分支）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import _parent_repo  # noqa: E402

from run_golden import parse_reset  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── ① parse_reset：golden 读侧的唯一实现 ─────────────────────────────────────
def test_parse_reset():
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


# ── ② server 侧：两个调用点各用各的 scope ────────────────────────────────────
def test_server_call_sites():
    print("\n[写侧] server.py 的两个分支发各自的 scope")
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("帧形是 `__RESET__:{scope}:{reason}` 一处实现",
          '__RESET__:{scope}:{reason}' in src)
    i_replan = src.find('if upd.get("gate_replan"):')
    i_all = src.find('emit_reset("all"')
    i_fb = src.find('elif upd.get("fallback_text"):')
    i_text = src.find('emit_reset("text"')
    check("四个锚点都在（缺一个说明分支被改名/搬走了）",
          -1 not in (i_replan, i_all, i_fb, i_text),
          f"{i_replan},{i_all},{i_fb},{i_text}")
    # 顺序断言：`all` 落在重规划那一支里、`text` 落在 fallback 那一支里。
    # 不写成"就近取周围 N 字符"是因为两个分支本来就相邻，窗口一宽就两可。
    check("重规划支发 all（决策被推翻 ⇒ 旧命令作废）", i_replan < i_all < i_fb,
          f"{i_replan} < {i_all} < {i_fb}")
    check("fallback 支发 text（决策没被推翻 ⇒ 只否掉措辞）", i_fb < i_text, f"{i_fb} < {i_text}")
    # 反向锁：`emit_reset(` 的每个调用都必须带 scope。漏一个参数就是 TypeError
    # （会红，不是静默），但**"两处传了 scope、第三处忘了"**这种红法出现得太晚——
    # 发出那一刻才炸。这里数调用点，加分支时逼人回来核一次。
    # （`(?<!def )` 把函数定义那一行排除掉——它当然也长得像一次调用）
    calls = re.findall(r"(?<!def )emit_reset\(([^)]*)\)", src)
    check(f"每个 emit_reset 调用都带 scope（实得 {len(calls)} 处：{calls}）",
          len(calls) == 2 and all("," in c for c in calls), str(calls))


# ── ③ 前端读同一份字面（跨端守卫）────────────────────────────────────────────
def test_frontend_reads_same_scope():
    print("\n[跨端] 前端解析的是同一份 scope 字面")
    s = _parent_repo.read(
        "frontend/public/live2d-widgets/chat-stream.js",
        why="`__RESET__:<scope>:<理由>` 的 scope 三端必须认同一份字面：前端按它决定"
            "清不清 programCmds（命令缓冲），与 server 发的 scope 对不上就会"
            "两个方向的病各犯一半（该清的没清 / 该留的清了）")
    if s is None:
        return
    check("前端只认 all / text 两个取值", bool(re.search(r"\(all\|text\)", s)),
          repr(re.findall(r"\([a-z|]+\):", s)[:5]))
    check("缺 scope 段时默认 all（与 Python 侧同一侧）",
          "const scope = mScope ? mScope[1] : 'all';" in s)
    check("只有 scope=all 才清命令缓冲",
          "if (scope !== 'text') programCmds = [];" in s)
    # 反向锁：除声明（`let programCmds = []`）之外，全文件清空这个缓冲只许出现一次
    # （就是上面那条带 scope 的）。多出来的一处必然是"又写了个无条件清"——
    # 而它长得完全正常，上面那三条正则一条都抓不住。
    clears = len(re.findall(r"(?<!let )programCmds = \[\]", s))
    check(f"除声明外清空命令缓冲只出现一次（实得 {clears}）", clears == 1, str(clears))


if __name__ == "__main__":
    for fn in (test_parse_reset, test_server_call_sites, test_frontend_reads_same_scope):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
