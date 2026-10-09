#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档里那几个「已经漂过」的数字，改成**同源**（离线、秒级）。

要回答的问题：`README.md` 与 `docs/agent-architecture.md` 里手写的
「N 个工具 / N 个技能 / N 条 golden」是**抄**进来的，代码一长就漂——
这三处都漂过（63→67、44→47、155→188），而没有任何判据看着它们。
`tests/test_golden_keys.py` 的 188 只锁 golden 文件自己，管不到文档。

**判据**：文档里声明的数字 == 代码里的真值。真值只有三个来源，不重写：
  - 工具数 = `len(tools.base._TOOL_REGISTRY)`
  - 技能数 = `len(agent.skills.SKILLS)`
  - golden 条数 = `eval/golden/basic.jsonl` 行数

**为什么不是「把文档里的数删掉」**：工具数、技能数、golden 规模是读者真要知道的量，
删了等于少一段信息。留着数字、由判据把它钉在代码上，漂了就当场红。

**红基线（自查用）**：把 `README.md` 的「67 个工具」改回「63 个工具」⇒ 本节必红；
把任一正则的匹配数量改成 0（例如把「个工具」写成别的）⇒ 「模式一条都没匹配上」那条先红
——**这是正控**：正则失效（锁瞎了）与数字漂移是两种不同的红，都要有人喊。

用法：.venv/bin/python tests/test_docs_facts.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.skills import SKILLS  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


N_TOOLS = len(_TOOL_REGISTRY)
N_SKILLS = len(SKILLS)
N_GOLDEN = sum(1 for ln in (ROOT / "eval/golden/basic.jsonl")
               .read_text(encoding="utf-8").splitlines() if ln.strip())

README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs/agent-architecture.md").read_text(encoding="utf-8")

# (文件, 说明, 正则——第 1 组必须是数字, 真值)
_CLAIMS: list[tuple[str, str, str, str, int]] = [
    ("README.md", README, "目录树：tools/base.py 的工具数", r"tools/base\.py\s+(\d+) 个工具", N_TOOLS),
    ("README.md", README, "正文：技能注册表条数", r"技能注册表当前有 (\d+) 个技能", N_SKILLS),
    ("README.md", README, "评测：L2 golden 条数", r"真实 LLM 任务评测：(\d+) 条 golden", N_GOLDEN),
    ("agent-architecture.md", ARCH, "§1：LLM 调用旁的工具数", r"LLM 调用、(\d+) 个工具。", N_TOOLS),
    ("agent-architecture.md", ARCH, "§1 图注：mermaid 节点里的工具数", r"(\d+) 工具 · 4 workers", N_TOOLS),
    ("agent-architecture.md", ARCH, "§2：skills.py 的技能数", r"技能注册表：(\d+) 个技能静态定义", N_SKILLS),
    ("agent-architecture.md", ARCH, "§2：base.py 的工具数", r"(\d+) 个 @tool 工具", N_TOOLS),
    ("agent-architecture.md", ARCH, "§5 标题：工具系统小节标题", r"## 5\. 工具系统（(\d+) 个）", N_TOOLS),
    ("agent-architecture.md", ARCH, "§11：write.content 那句话里的工具数", r"当前 (\d+) 个工具里", N_TOOLS),
]

print("① 文档声明的数字 == 代码里的真值")
print(f"   真值：工具 {N_TOOLS} · 技能 {N_SKILLS} · golden {N_GOLDEN}")
for name, text, desc, pat, truth in _CLAIMS:
    hits = re.findall(pat, text)
    if not hits:
        # 正则一条都不匹配 = 判据瞎了（文案改形、文件被重排）——与"数字漂了"是两种红
        check(f"{name}：{desc}", False, f"模式一条都没匹配上（判据失明）：/{pat}/")
        continue
    bad = [h for h in hits if int(h) != truth]
    check(f"{name}：{desc}（{len(hits)} 处）", not bad,
          f"写着 {bad}，代码是 {truth}")

print("\n② golden 条数与 test_golden_keys 的锁同源")
_KEYS = (ROOT / "tests/test_golden_keys.py").read_text(encoding="utf-8")
_m = re.search(r'用例数（(\d+) 条）".*?==\s*(\d+)', _KEYS, re.S)
check("test_golden_keys 里那个 188 与 basic.jsonl 行数一致",
      bool(_m) and _m.group(1) == _m.group(2) == str(N_GOLDEN),
      f"锁里的数 = {_m.groups() if _m else None}，实际 {N_GOLDEN}")

print("\n③ 两处文档自洽：README 与架构文档声明的工具数一致")
check("README 与 agent-architecture 说同一个工具数",
      all(int(h) == N_TOOLS for _n, _t, _d, p, _v in _CLAIMS
          if "工具" in _d or "工具系统" in _d or "@tool" in _d
          for h in re.findall(p, _t)),
      "两处对不上")

print()
if FAILS:
    print(f"FAILS({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部通过。")
raise SystemExit(0)
