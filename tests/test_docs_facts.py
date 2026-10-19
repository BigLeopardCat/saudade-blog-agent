#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档里那几个「已经漂过」的数字，改成**同源**（离线、秒级）。

要回答的问题：`README.md` 与 `docs/agent-architecture.md` 里手写的
「N 个工具 / N 个技能 / N 条 golden」是**抄**进来的，代码一长就漂——
这三个数都漂过（63→67、44→47、155→188），而没有任何判据看着它们。
`tests/test_golden_keys.py` 的 188 只锁 golden 文件自己，管不到文档。
**20261011 又抓到第四处**：§1 目录树那行 `basic.jsonl（158 条）` 同样是手抄的，同样漂到了
188——它一直没被抓住，是因为下面那几条正则的射程都在正文，够不到树里那行。

**20261017 第五处（依赖条数）**：`docs/rag-design.md` 两处与 `docs/multimodal-retrieval.md`
一处写着"生产 venv 钉死 11 个包"——而 `pyproject.toml` 那天加了 3 个 checkpoint 后端依赖
⇒ 三行**当场全漂**。它一直没人看着，理由与第四处同形：那三处不在本文件的射程里（两个文件
根本没被读进来）。补锁，真值 = `[project].dependencies` 的条数。

**20261018 第六、七处（审计 A2）**：`docs/README.md` 的"（N 份 ADR…）"与
`eval/fixtures/README.md` 开头的"那 N 条用例"同样是手抄的（真值 12 / 192，写着 7 / 158），
同样没人看着——理由还是那一条：**那两个文件本文件从没读进来**。前者补一条新真值
（ADR 份数），后者复用 golden 条数。

**判据**：文档里声明的数字 == 代码里的真值。真值只有五个来源，不重写：
  - 工具数 = `len(tools.base._TOOL_REGISTRY)`
  - 技能数 = `len(agent.workflow_defs.WORKFLOWS)`
  - golden 条数 = `eval/golden/basic.jsonl` 行数
  - 依赖条数 = `pyproject.toml` 的 `[project].dependencies` 条数
  - ADR 份数 = `docs/adr/adr-*.md` 的个数

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
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.workflow_defs import WORKFLOWS  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


N_TOOLS = len(_TOOL_REGISTRY)
N_WORKFLOWS = len(WORKFLOWS)
N_GOLDEN = sum(1 for ln in (ROOT / "eval/golden/basic.jsonl")
               .read_text(encoding="utf-8").splitlines() if ln.strip())

N_DEPS = len(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
             ["project"]["dependencies"])
# 第六处（20261018 审计 A2 补锁）：ADR 份数。`docs/README.md` 那句"（N 份…）"手抄，
# 真值 = `docs/adr/` 下的 `adr-*.md` 个数（`README.md` 自己不算一份 ADR）。
N_ADRS = len(sorted((ROOT / "docs/adr").glob("adr-*.md")))

README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs/agent-architecture.md").read_text(encoding="utf-8")
RAG_DESIGN = (ROOT / "docs/rag-design.md").read_text(encoding="utf-8")
MULTIMODAL = (ROOT / "docs/multimodal-retrieval.md").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs/README.md").read_text(encoding="utf-8")
FIXTURES = (ROOT / "eval/fixtures/README.md").read_text(encoding="utf-8")

# (文件, 说明, 正则——第 1 组必须是数字, 真值)
_CLAIMS: list[tuple[str, str, str, str, int]] = [
    ("README.md", README, "目录树：tools/base.py 的工具数", r"tools/base\.py\s+(\d+) 个工具", N_TOOLS),
    ("README.md", README, "正文：技能注册表条数", r"技能注册表当前有 (\d+) 个技能", N_WORKFLOWS),
    ("README.md", README, "评测：L2 golden 条数", r"真实 LLM 任务评测：(\d+) 条 golden", N_GOLDEN),
    ("agent-architecture.md", ARCH, "§1：LLM 调用旁的工具数", r"LLM 调用、(\d+) 个工具。", N_TOOLS),
    ("agent-architecture.md", ARCH, "§1 图注：mermaid 节点里的工具数", r"(\d+) 工具 · 4 workers", N_TOOLS),
    ("agent-architecture.md", ARCH, "§2：workflow_defs 的技能数", r"技能注册表：(\d+) 个技能静态定义", N_WORKFLOWS),
    ("agent-architecture.md", ARCH, "§2：base.py 的工具数", r"(\d+) 个 @tool 工具", N_TOOLS),
    ("agent-architecture.md", ARCH, "§5 标题：工具系统小节标题", r"## 5\. 工具系统（(\d+) 个）", N_TOOLS),
    ("agent-architecture.md", ARCH, "§11：write.content 那句话里的工具数", r"当前 (\d+) 个工具里", N_TOOLS),
    # 第四处同类漂移（20261011 补锁）：§1 目录树里 `basic.jsonl（N 条）` 一直是手抄的，
    # 抄进来时是 158、真值是 188，而前面那三条正则一条都盖不到它（树里那行不在它们射程内）。
    ("agent-architecture.md", ARCH, "§1 目录树：golden 用例文件的条数",
     r"eval/golden/basic\.jsonl（(\d+) 条）", N_GOLDEN),
    # 第五处（20261017 补锁）：三处"生产 venv 钉死 N 个包"一直是手抄的，而它们**分布在
    # 两个本文件从没读过的文档里** ⇒ 加依赖那天三行一起漂，没有任何东西会红。
    ("multimodal-retrieval.md", MULTIMODAL, "§3 表格：pyproject 的依赖条数",
     r"pyproject\.toml` 的 (\d+) 个依赖", N_DEPS),
    ("rag-design.md", RAG_DESIGN, "§9 索引与增量：生产 venv 的包数",
     r"生产 venv 仍是那 (\d+) 个钉死的包", N_DEPS),
    ("rag-design.md", RAG_DESIGN, "§10.5 边界：生产 venv 的包数",
     r"生产 venv 钉死 (\d+) 个包", N_DEPS),
    # 第六、七处（20261018 审计 A2 补锁）：都是"同一个量、另一个文件里也手抄了一份，
    # 而那个文件**从来没被本文件读过**"——与第四、五处同形，也是同一个失效模式：
    # 锁的射程没盖到的那一份，漂了没人喊。
    ("README.md（docs/）", DOCS_INDEX, "架构决策记录索引那句里的 ADR 份数",
     r"adr/README\.md\)（(\d+) 份", N_ADRS),
    ("README.md（eval/fixtures/）", FIXTURES, "开头那句引用的 golden 条数",
     r"`eval/golden/basic\.jsonl` 那 (\d+) 条用例", N_GOLDEN),
]

print("① 文档声明的数字 == 代码里的真值")
print(f"   真值：工具 {N_TOOLS} · 技能 {N_WORKFLOWS} · golden {N_GOLDEN} · 依赖 {N_DEPS}"
      f" · ADR {N_ADRS}")
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

# 第五处同类漂移（20261018 补锁）：`docs/README.md` 里那句「架构决策记录另有索引：
# adr/README.md（N 份，…）」的 N 是**手抄**的——抄进来时是 7，而 `docs/adr/` 下当时
# 已经有 12 份。它没被上面那几条正则盖到，是因为那几条的射程都在 README 与架构文档的
# 正文数字上，够不到索引尾注这一行。真值只有一个来源：盘上有几份 `adr-*.md`。
# （`N_ADRS` / `DOCS_INDEX` 在顶上已经取过，这里不再重复定义——同一个量两处取值迟早分岔。）
print("\n④ docs/README.md 尾注里手抄的 ADR 份数 == docs/adr/ 下盘上的份数")
_ADR_PAT = r"（(\d+) 份，记"
_hits = re.findall(_ADR_PAT, DOCS_INDEX)
if not _hits:
    # 同①：正则一条都不匹配 = 判据瞎了（那句话被改形/搬家），与"数字漂了"是两种红
    check("docs/README.md 尾注：ADR 份数", False,
          f"模式一条都没匹配上（判据失明）：/{_ADR_PAT}/")
else:
    _bad = [h for h in _hits if int(h) != N_ADRS]
    check(f"docs/README.md 尾注：ADR 份数（{len(_hits)} 处）", not _bad,
          f"写着 {_bad}，盘上是 {N_ADRS} 份")
check("  adr/ 下确实数到了文件（否则上一条是空过）", N_ADRS > 0, f"N_ADRS={N_ADRS}")

print()
if FAILS:
    print(f"FAILS({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部通过。")
raise SystemExit(0)
