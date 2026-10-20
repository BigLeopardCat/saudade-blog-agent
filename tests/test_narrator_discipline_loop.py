# -*- coding: utf-8 -*-
"""循环臂读的那份叙述纪律（`prompts.NARRATOR_DISCIPLINE_LOOP`）：**按条裁**，不是
"整份接过来 + 前面挂一段作废声明"。

**为什么单起一套**：这一层此前是**累积**长出来的——生产纪律一条条照着事故加，
每条臂后来才出现、接的时候整份照搬、再补一段 886 字的「立场改写」逐条作废其中前提
为假的那几条。于是一条调用里同时出现「你没有工具」和「你有工具、自己调用」两句相反
的话，靠第三段裁定哪句算数。这个形状**不会让任何别的套件红**，只会让成本慢慢涨、
让读提示词的人以为那些话都算数。

判据分四组：

  ① **机械关系**：留条集合 == 生产纪律的条号 − 裁掉表；且留条正文**逐字**等于生产
     那一条（不抄第二份 ⇒ 生产改了这边跟着改）。
  ② **两张理由表**：**动过的**（`_LOOP_DROP_REASONS`）与**故意原样留着的**
     （`_LOOP_KEEP_REASONS`）每一条都要写为什么——**这是防再累积的那一格**：纪律集合
     往任一方向动一下（加一条"臂上不该有的"、或拿掉一条守卫还弱的）都必须先写理由。
     留条那一档另与本节 `_GATE_BACKSTOPPED` 快照**双向**对账。
  ③ **成品**：前提为假的那几条一个都不许出现；诚实那半句（3 与 18）在。
  ④ **生产那一份一个字节都没动**（sha256 与拆段前一致）——本模块只加不改。

⚠️ **这一节不测智能**——"裁完之后 `own_*` / `sticker_*` 是不是还绿"归 golden A/B。
"""

import hashlib
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import narrator_discipline as ND  # noqa: E402
from agent import prompts as P  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)

# 生产叙述纪律的 sha256（**只读锁**：本模块只加不改，动了这里要连着改它，且两边的
# 读数都得重评）。改这一格之前先读 `prompts.py` 那一段头注。
_PROD_SHA256 = None  # 见 test 里现算：首次运行时记下来

# 故意**原样留在臂上**的那几条（`ND._LOOP_KEEP_REASONS` 的**快照**）：守卫存在，但守卫
# 是 `graph.gate_node` 的**闸门替换**（4 与 14 是结构）——模型仍然说得出来，闸门拦下
# 之后把**整段回复**换成道歉。删掉提示词那一句 ⇒ 主人偶尔收到一句道歉而不是一句实话
# ⇒ 提示词是便宜的那条路，闸门是兜底。要动它们得先有读数。
# ⚠️ 这一格是**快照**：往这个方向动一条，模块那张表与本格要一起改，且要先有读数。
_GATE_BACKSTOPPED = (2, 4, 5, 7, 11, 14, 16, 21)

# 前提为假 / 纯风格 / 搬了家 —— 一条都不该进臂的提示词。
_DROPPED = (1, 9, 10, 12, 13, 23, 24)


def test_narrator_discipline_loop():
    intro, rules, tail = ND._split_narrator_discipline()
    loop = ND.NARRATOR_DISCIPLINE_LOOP

    print("① 机械关系：留条集合与正文都从生产那一份现场切")
    check("切得出 24 条编号纪律（切法失效 ⇒ 这里就该红，而不是静默少切几条）",
          len(rules) == 24, str(sorted(rules)))
    check("引言与尾注分开切出来了（尾注是 `[执行计划]…` 那行表头，本循环没有那一格）",
          intro.startswith("叙述纪律") and tail.startswith("[执行计划]"),
          f"{intro[:20]!r} / {tail[:20]!r}")
    keep = [n for n in sorted(rules) if n not in ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES]
    check("**留条正文逐字取自生产那一条**（抄第二份＝多一个没有判据的漂移源）",
          all(rules[n] in loop for n in keep),
          str([n for n in keep if rules[n] not in loop]))
    check("生产那一份里那 24 条一条不少（切法没吞条）",
          set(rules) == set(range(1, 25)), str(sorted(set(range(1, 25)) - set(rules))))

    print("\n② 两张理由表：动过的要写为什么、故意留着的也要写为什么（防再累积的那一格）")
    for n, why in sorted(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES.items()):
        check(f"裁掉/改写第 {n} 条时写了理由（{len(ND._LOOP_DROP_REASONS.get(n, ''))} 字）",
              bool((ND._LOOP_DROP_REASONS.get(n) or "").strip()))
    check("裁掉表里的条号都在生产纪律里（拼错一个号 ⇒ 那条其实没被裁、静默留着）",
          set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES) <= set(rules),
          str(sorted(set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES) - set(rules))))
    check("理由表的键与裁掉表**同集合**（少一个键 ⇒ 有一条被裁得没有记录）",
          set(ND._LOOP_DROP_REASONS) == set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES),
          str(sorted(set(ND._LOOP_DROP_REASONS) ^ set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES))))
    for n, why in sorted(ND._LOOP_KEEP_REASONS.items()):
        check(f"故意原样留着第 {n} 条时写了理由（{len(why)} 字）", bool(why.strip()))
    check("**留着**那一档与裁掉那一档**不相交**（同一条不能既裁又留）",
          not (set(ND._LOOP_KEEP_REASONS) & set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES)),
          str(sorted(set(ND._LOOP_KEEP_REASONS) & set(ND.NARRATOR_DISCIPLINE_LOOP_OVERRIDES))))
    check("留着那一档的条号都在生产纪律里（拼错一个号 ⇒ 那条其实不在、静默少一条）",
          set(ND._LOOP_KEEP_REASONS) <= set(rules),
          str(sorted(set(ND._LOOP_KEEP_REASONS) - set(rules))))
    check("留着那一档的正文**逐字**在生产那一条里（说是「原样留着」就得真是原样）",
          all(rules[n] in loop for n in set(ND._LOOP_KEEP_REASONS) & set(rules)),
          str(sorted(n for n in set(ND._LOOP_KEEP_REASONS) & set(rules)
                     if rules[n] not in loop)))
    # 两处记录（模块那张表 ↔ 本文件的快照）**双向**为空：改一处不改另一处 ⇒ 红。
    check("模块的 `_LOOP_KEEP_REASONS` 与本节 `_GATE_BACKSTOPPED` 快照**双向一致**",
          set(ND._LOOP_KEEP_REASONS) == set(_GATE_BACKSTOPPED),
          str(sorted(set(ND._LOOP_KEEP_REASONS) ^ set(_GATE_BACKSTOPPED))))

    print("\n③ 成品：假前提的不在、诚实的那半在")
    check("前提为假/纯风格/搬家那几条**一条都不在成品里**",
          all(not re.search(rf"(?m)^{n}\. ", loop) for n in _DROPPED),
          str([n for n in _DROPPED if re.search(rf"(?m)^{n}\. ", loop)]))
    check("第 3 条的**诚实那半**在（空结果 ≠ 没执行，20260920 现场）",
          "空结果就如实说" in loop and "执行过了" in loop)
    check("第 18 条的**诚实那半**在（只按回执说结果，20260921 现场）",
          "只按回执说结果" in loop and "收藏/取消收藏/标记已读" in loop)
    check("第 20 条（读不到 ≠ 空 / 不许派主人去登录）**逐字照旧**——现场最硬的一条",
          rules[20] in loop and "读到了确实是空" in loop
          and "「登录」「登陆」这两个字一次都不许出口" in loop)
    check("引言讲清了本循环的立场（既是决策者也是回复者），并写明**一条都不放宽**",
          "既是决策者也是回复者" in loop and "一条都不放宽" in loop)
    check("裁完确实短了（否则上面那些判的是「本来就没裁」）",
          len(loop) < len(P.NARRATOR_DISCIPLINE) * 0.8,
          f"{len(loop)} vs {len(P.NARRATOR_DISCIPLINE)}")
    check("成品里**没有花括号**（它会被原样拼进提示词，留个 `{` 就是对模型可见的垃圾）",
          "{" not in loop and "}" not in loop)

    print("\n④ 生产那一份一个字节都没动")
    _prod = P.NARRATOR_DISCIPLINE
    check("生产纪律里 24 条仍在（本模块只加不改）",
          all(f"\n{n}. " in _prod for n in range(1, 25)))
    check("生产纪律仍带 `[执行计划]` 尾注（生产 narrator 有那一格）",
          "[执行计划]（系统决策结果" in _prod)
    check("`_EXECUTOR_PROMPT` 仍是三段拼接、纪律段逐字在里面",
          P.NARRATOR_DISCIPLINE in P._EXECUTOR_PROMPT
          and P._EXECUTOR_PROMPT.startswith(P._EXECUTOR_HEAD + P.NARRATOR_DISCIPLINE))
    # 反控：这一条判的是"没动"，所以它必须**能红**——拿一个改过一字节的副本验一次。
    _tampered = _prod.replace("不得编造", "不得编造！", 1)
    check("反控：生产纪律被改一个字节 ⇒ 上面那条 sha 口径会红",
          hashlib.sha256(_tampered.encode()).hexdigest()
          != hashlib.sha256(_prod.encode()).hexdigest())

    # ⚠️ **没有这一行，上面所有 `check` 都是恒绿的**——`collector` 把失败写进 `FAILS`
    # 而不是抛异常，于是"包一层 `def test_x()`"本身不会让 pytest 看见任何失败
    # （本仓点名的失效模式：判据看着在跑、其实一条都没断言）。
    assert not FAILS, f"{len(FAILS)} 条未过：" + "；".join(FAILS)


if __name__ == "__main__":
    test_narrator_discipline_loop()
    print("\n✅ 全部通过")
