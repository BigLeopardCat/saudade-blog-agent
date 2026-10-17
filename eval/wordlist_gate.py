# -*- coding: utf-8 -*-
"""中文词表闸：判据层里的「词表形状」**只许变小，不许长新的**（20261014）。

**为什么要有它**：判据层的历史做法是"用手写中文词表回答语义问题"——几十张表散在
`agent/` 里，每遇一次新说法就假红一次、就往表里加一个词。本仓自己早写下了这条纪律
（`docs/toolcall-stability-roadmap.md` §6：「不为判据加更多词表：先减后加」，D6 的 KPI
就是不许加；`adr-0002` 同款），但**它只是一句散文**：除 `_DEICTIC_WORDS` 外其余几十张
表处于"默认可扩"状态，没有任何机器判据拦得住新表长出来。这份闸门把那句散文变成判据。

**两层，各治一个病**（两层都必需，别只留一层）：

  ① **登记表**（`entries`）——把"哪些表是封闭类、哪些是开放类、开放类的替代是什么"
     从散文注释变成机器判据。**未登记即红**（这就是 roadmap §6 那条 KPI 的可执行形态）；
     已登记的**只许变小**；`open` 类必须写明 `replacement` 与 `sunset`。
     入表用的是**窄口径**：只收「**被当匹配器用**」的中文表（见下 `_matcher_names`）。

  ② **穷举量**（`metric`）——按文件数**所有**"中文串住在集合字面量里"的数量 +
     所有含中文的正则字符数。窄口径的登记表**按构造是有洞的**：一张表若被当参数传给
     某个助手函数去匹配，`_matcher_names` 看不见那个调用点。②不判形状、只数总量，
     因此**任何**新中文词表都会让所在文件的数变大 ⇒ 红。它是①的补网。
     ⚠️ 它数的是**位置**，不是**用意**：同一个字面量搬个地方（比如从 `note = "中文…"`
     变成 `return [], "中文…", x` 这种元组的直接元素）就多计一处、当场红。这不是漏网，
     是"宁可多报"那一边——红了的解法有两个：把字面量挂回变量上（改形状），或确认它真是
     一张新表（那就该进①）。实测吃过一次：20261015 批 1 拆函数时 `is_top` 那条注记
     变成了元组元素，`agent/workflows.py` 234→235。

**为什么非要①不可**：②只数总量，验不了"这张表该不该存在"；而"哪张表是病、替代是什么"
正是这次要交付的清单（roadmap §6 的待办）。①提供姓名、类别与出路，②保证①没有漏网。

**③ eval 侧那一段**（20261015 批 4）：上面两层查的是**运行时判据面**（`agent/`/`tools/`），
而 `eval/` 里还有**同一副病**的第二张脸——golden 的负正则/正反词表、`run_golden.py` 的四张
共享族、六个豁免开关。它们此前是"判据的判据"，谁都没看着：往 golden 里加一条负正则不需要
任何理由，往豁免开关上再挂一条用例也不需要。现在这一段把同一套棘轮搬过去（只许变小、
新增模式要先把理由写进 why、`--update` 是唯一放宽通道）。
⚠️ **它与①②的量分开读，不并进同一个计数**（`TARGETS` 还是 `agent`/`tools`/`server.py`）：
"运行时判据面少了一张表"与"评测判据面多了一条正则"不是同一个量，混在一起会让加一条负正则
看起来像删了一张表。两段的红各自报各自的。

**五种类别**（每一类都必须能对"它为什么不是病"给出一句话）：

  | 类别 | 意思 | 判据 |
  |---|---|---|
  | `closed` | **封闭类**：定义域划得完、不会长出新成员（代词、中文数字、标点级引号） | 穷举得完 |
  | `system_data` | **系统数据**：站内自己的字面量（页面映射、状态字、Rust 文案） | 事实源在站内 |
  | `parser` | **解析器**：拆固定格式的 wire 文本 / 系统产出的行（不是主人话） | 输入不是主人话 |
  | `prompt_text` | **话术**：只进提示词/卡面，没有任何**判定**分支读它 | 无判定读它 |
  | `open` | ★**开放类**：病。须带 `replacement`（换什么表征）与 `sunset`（哪批收口） | 措辞无底 |

不确定就填 `open`——这一族里 `open` 是**安全方向**（多一条待办看得见），`closed` 是危险方向
（把病登记成正常）。**不许**为了"让闸门变绿"把类别往 `closed` 上凑。

**判据来源是 AST 不是源码文本**：注释里写一个中文选择器会把按文本做的断言截断
（本仓三犯过的老坑，见 `tests/` 里 `_strip_comments` 那一族）；而"词表"这个形状
（集合型字面量 / 含中文的正则）用 AST 判得又准又稳。

用法：
    python eval/wordlist_gate.py            # 判（退出码 0 通过 / 1 变差 / 3 跑不起来）
    python eval/wordlist_gate.py --dump     # 出登记骨架（给人填 category/why，不覆盖已填）
    python eval/wordlist_gate.py --update   # 重出登记表（唯一放宽通道）
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "eval" / "judge_registry.json"

# 查什么：**运行时的判据面**。`tests/`、`eval/` 不进这道闸的前两层——它们是判据的**判据**，
# 自己就充满中文断言。`eval/` 侧那一份由下面 ③ 那一段（`scan_eval`）单独管，**量不合并**。
TARGETS = ["agent", "tools", "server.py"]

CJK = re.compile(r"[一-鿿]")
# 收集器包裹函数：`frozenset({...})` / `tuple([...])` 与裸字面量是同一个形状。
WRAPPERS = {"frozenset", "set", "tuple", "list"}
COLLECTIONS = (ast.Tuple, ast.List, ast.Set, ast.Dict)
# "被当匹配器用"的形态：这些方法名 + 这些调用位置。
MATCH_METHODS = {"search", "match", "fullmatch", "finditer", "findall", "sub", "split", "get"}


@dataclass
class Cand:
    """一个「词表形状」的现场。"""
    ident: str            # 稳定身份：`文件::顶层符号[#序号]`（行号会漂，不用它当身份）
    file: str
    kind: str             # collection | regex
    name: str             # 顶层符号名或赋值目标名
    size: int             # 中文词条数（集合）/ 中文字符数（正则）——棘轮量的就是它
    lineno: int
    sample: str = ""
    hits: list[str] = field(default_factory=list)   # 命中的中文片段（给人看现场）

    def as_json(self) -> dict:
        d = {"id": self.ident, "file": self.file, "kind": self.kind,
             "name": self.name, "size": self.size}
        if self.sample:
            d["sample"] = self.sample
        return d


# ── 窄口径：这张中文表**被当匹配器用**了吗 ────────────────────────────────────

def _direct_cjk(node: ast.AST) -> list[str]:
    """集合字面量的**直接**元素里的中文串（不下钻到嵌套集合/调用）。

    不下钻是关键：`WORKFLOWS = [Workflow(name="…", desc="中文"), …]` 的直接元素是 Call、
    不是字符串，它不是词表；而 `("好", "好的")` 是。
    """
    items = (list(node.keys) + list(node.values)) if isinstance(node, ast.Dict) else list(node.elts)
    return [it.value for it in items
            if isinstance(it, ast.Constant) and isinstance(it.value, str) and CJK.search(it.value)]


def _matcher_names(tree: ast.AST) -> set[str]:
    """模块里**出现在匹配位置**的符号名。

    覆盖四种"拿词表回答语义问题"的调用形状：
      · `x in TABLE` / `TABLE == x`        （成员判定）
      · `for w in TABLE` / `any(w in t for w in TABLE)`（遍历判定）
      · `TABLE.search(t)` / `.match/.sub/.split/.get`（方法判定）
      · `re.search(TABLE, t)` / `re.match(TABLE, t)`  （正则当参数传出）
    ⚠️ 它**看不见**"表被当参数传给某个助手函数"那种间接用法——这正是穷举量那一层的由来。
    """
    used: set[str] = set()

    def add(node: ast.AST) -> None:
        for s in ast.walk(node):
            if isinstance(s, ast.Name):
                used.add(s.id)

    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and any(
                isinstance(o, (ast.In, ast.NotIn, ast.Eq, ast.NotEq)) for o in n.ops):
            add(n.left)
            for c in n.comparators:
                add(c)
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            add(n.generators[0].iter)
        elif isinstance(n, ast.For):
            add(n.iter)
        elif isinstance(n, ast.Call):
            f = n.func
            if getattr(f, "attr", "") in MATCH_METHODS:
                add(f.value)
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "re" and n.args):
                add(n.args[0])
    return used


def _collect(body: list[ast.stmt], scope: str, rel: str, out: list[Cand],
             counter: dict[str, int], matchers: set[str]) -> None:
    """递归收集。`scope` = 当前顶层符号（模块级是 `""`）。"""
    def emit(name: str, kind: str, size: int, lineno: int, hits: list[str],
             sample: str = "") -> None:
        base = name or scope or "<module>"
        counter[base] = counter.get(base, 0) + 1
        seq = counter[base]
        ident = f"{rel}::{base}" + (f"#{seq}" if seq > 1 else "")
        out.append(Cand(ident=ident, file=rel, kind=kind, name=base,
                        size=size, lineno=lineno, sample=sample,
                        hits=sorted(set(hits))[:6]))

    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counter.clear()
            _collect(stmt.body, stmt.name, rel, out, counter, matchers)
            continue
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            targets, value = [], None
        if value is not None:
            nm = next((_target_name(t) for t in targets if _target_name(t)), scope)
            inner = value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in WRAPPERS and len(value.args) == 1):
                inner = value.args[0]
            if isinstance(inner, COLLECTIONS):
                hits = _direct_cjk(inner)
                # 窄口径：≥2 个中文词条、**且这个符号被当匹配器用**过。
                if len(hits) >= 2 and nm in matchers:
                    emit(nm, "collection", len(hits), stmt.lineno, hits)
                    continue
            if _is_cjk_regex(value) and _is_matcher_regex(nm, matchers):
                pat = value.args[0].value
                emit(nm, "regex", len(CJK.findall(pat)), value.lineno,
                     CJK.findall(pat), sample=pat[:80])


def _target_name(t: ast.AST) -> str:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    return ""


def _is_cjk_regex(value: ast.AST) -> bool:
    return (isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "compile"
            and bool(value.args) and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str) and bool(CJK.search(value.args[0].value)))


def _is_matcher_regex(nm: str, matchers: set[str]) -> bool:
    """正则的两种认领方式：被匹配器调用（`_X_RE.search(...)`），或**命名就写明是正则**
    （`_FOO_RE` / `_foo_re`）——后者是与本仓既有命名约定对齐的兜底，否则 `re.search(_X_RE, t)`
    这种把正则当参数传出的写法会整族漏掉。
    """
    return nm in matchers or (nm.startswith("_") and nm.endswith(("_RE", "_re")))


def scan() -> tuple[list[Cand], dict]:
    """返回（登记表候选，穷举量）。

    穷举量按文件分别记 `[集合中文串数, 正则中文字符数]`——**分文件**是判据的一部分：
    只看总数会让"这边删两个、那边加三个"在总数上抵消（`type_gate.py` 同款教训）。
    """
    out: list[Cand] = []
    metric: dict[str, list[int]] = {}
    for t in TARGETS:
        p = ROOT / t
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as e:      # 语法错不该在闸门里静默跳过
                raise RuntimeError(f"{rel} 解析不了：{e}") from e
            _collect(tree.body, "", rel, out, {}, _matcher_names(tree))
            metric[rel] = _file_metric(tree)
    total = [sum(v[0] for v in metric.values()), sum(v[1] for v in metric.values())]
    return sorted(out, key=lambda c: (c.file, c.lineno)), {"by_file": metric, "total": total}


def _file_metric(tree: ast.AST) -> list[int]:
    """穷举量：**所有**住在集合字面量里的中文串（不论多少、不论是否被匹配）+ 所有含中文的
    正则字符数。不判形状、只数总量 ⇒ 窄口径漏掉的间接用法在这里仍会被看见。"""
    coll = regex = 0
    parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str) and CJK.search(n.value)
                and isinstance(parents.get(id(n)), COLLECTIONS)):
            coll += 1                       # 每个串只数一次（不论嵌多深）
        elif _is_cjk_regex(n):
            regex += len(CJK.findall(n.args[0].value))
    return [coll, regex]


# ── ③ eval 侧：评测判据面的棘轮（批 4）───────────────────────────────────────
#
# 这一段的判据来源与①②不同：那里扫**代码形状**，这里的三块各扫一种"评测判据面"——
#   · `keys`    ：golden 三个文本断言键的**模式/词条全集** + 格数 + 项数（数据在 jsonl 里）
#   · `exempt`  ：六个豁免开关**各被哪些用例打开**（开关是判据替自己打的补丁：加了一处
#                 就说明有一条判据又在误伤，值得被人看见一次）
#   · `families`：`run_golden.py` 里四张共享族（诚实拒绝 / 目标不存在 / 存在性引导词 /
#                 派登录）的元素数 + 元素集哈希。**哈希是为了抓"数量不变、条目被换掉"**
#                 （逐条替换措辞正是这一族的病，见 `ABSENCE_COMPOUND` 头注那次假红）。
#   另加 `judge_metric`：`run_golden.py` 走一遍①②的穷举量（集合里的中文串 + 含中文的正则
#   字符数）——上面三块是**窄口径**（我点得出名字的才数），这句是补网。
EVAL_GOLDEN = "eval/golden/basic.jsonl"
EVAL_JUDGE = "eval/run_golden.py"
EVAL_KEYS = ("text_not_match_regex", "text_contains", "text_not_contains")
EVAL_EXEMPT_KEYS = (
    "not_contains_exempt_quote", "not_match_exempt_conditional",
    "not_match_exempt_refuted", "not_match_exempt_mention",
    "not_match_exempt_doc_fact", "not_match_exempt_consequence",
)
EVAL_FAMILIES = ("DENIAL_FAMILY", "ABSENCE_COMPOUND", "ABSENCE_LEADS", "LOGIN_DEMAND_RE")


def _golden_golds(case: dict) -> list[dict]:
    """一条用例里的 gold 对象（多轮取每一轮的）。

    ⚠️ 这是 `eval/run_golden.py::iter_rounds` 的**镜像**，只镜像"gold 在哪"这一点。
    镜像而非 import 是必要的：闸门不许把 langchain/LLM 拖进来（`tests/test_wordlist_gate.py`
    与 `--dump` 都要秒级跑完）。镜像就有漂移风险，所以 ⑪ 用一条"顶层 gold 与 rounds 并存"
    的用例钉住它的形状（**只读 rounds**，与 `iter_rounds` 同一取舍）。
    """
    rounds = case.get("rounds")
    if rounds:
        return [r.get("gold") or {} for r in rounds]
    return [case.get("gold") or {}]


def _sha(parts: list[str]) -> str:
    """元素集的稳定指纹（`ast.unparse` 之后——不受排版/注释影响，元素换掉才变）。"""
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:10]


def _literal_cjk(node: ast.AST) -> tuple[int, bool]:
    """`a + b + r"中文"` 这种拼接里能数到的中文字符数，以及"是否全是字面量"。

    为什么不能只认 `Constant`：`LOGIN_DEMAND_RE` 就是三段拼接（中间那段是
    `_LOGIN_NOUN_TAIL` 这个变量名）。全认不出 ⇒ 这张族在闸门里是隐形的，
    而它正是 `不能删的五张共享族` 之一。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(CJK.findall(node.value)), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, lc = _literal_cjk(node.left)
        r, rc = _literal_cjk(node.right)
        return l + r, lc and rc
    return 0, False


def _family_shape(value: ast.AST) -> dict:
    """一张共享族的形状：元素数 + 指纹。元组数元素，正则数中文字符（同①②的口径）。

    拼接出来的正则只能数到字面量那几段 ⇒ 记 `partial: true`（读数下界）——指纹仍是全式的，
    所以"多加一段"或"换掉中间那个变量"都会让 sha 变。
    """
    inner = value
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in WRAPPERS and len(value.args) == 1):
        inner = value.args[0]
    if isinstance(inner, COLLECTIONS):
        elts = (list(inner.keys) + list(inner.values)) if isinstance(inner, ast.Dict) else inner.elts
        parts = [ast.unparse(e) for e in elts]
        return {"kind": "collection", "size": len(parts), "sha": _sha(parts)}
    if (isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "compile" and value.args):
        n, complete = _literal_cjk(value.args[0])
        return {"kind": "regex", "size": n, "sha": _sha([ast.unparse(value)]),
                **({} if complete else {"partial": True})}
    return {"kind": "?", "size": 0, "sha": ""}


def scan_eval() -> dict:
    """扫 eval 侧那三块 + 补网那个量。

    **文件不在就整块缺席**（`{}` 里的三段各留空）：反控用的临时树只放 `agent/`，那时这一层
    必须**沉默**而不是报"文件没了"。真仓里两个文件都在，所以 ③ 在真仓上是常开的。
    """
    out: dict = {"keys": {}, "exempt": {}, "families": {}}
    gpath, jpath = ROOT / EVAL_GOLDEN, ROOT / EVAL_JUDGE
    if gpath.is_file():
        keys = {k: {"sites": 0, "items": 0, "patterns": set()} for k in EVAL_KEYS}
        exempt = {k: set() for k in EVAL_EXEMPT_KEYS}
        for line in gpath.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            name = str(case.get("name") or case.get("id") or "?")
            for gold in _golden_golds(case):
                for k in EVAL_KEYS:
                    vals = gold.get(k)
                    if not vals:
                        continue
                    vals = vals if isinstance(vals, list) else [vals]
                    keys[k]["sites"] += 1
                    keys[k]["items"] += len(vals)
                    keys[k]["patterns"].update(str(v) for v in vals)
                for k in EVAL_EXEMPT_KEYS:
                    if gold.get(k):
                        exempt[k].add(name)
        out["keys"] = {k: {"sites": v["sites"], "items": v["items"],
                           "patterns": sorted(v["patterns"])} for k, v in keys.items()}
        out["exempt"] = {k: sorted(v) for k, v in exempt.items()}
    if jpath.is_file():
        try:
            tree = ast.parse(jpath.read_text(encoding="utf-8"))
        except SyntaxError as e:          # 与①②同一条纪律：语法错不许静默跳过
            raise RuntimeError(f"{EVAL_JUDGE} 解析不了：{e}") from e
        fam: dict = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                nm = next((_target_name(t) for t in targets if _target_name(t)), "")
                if nm in EVAL_FAMILIES:
                    fam[nm] = _family_shape(n.value)
        out["families"] = fam
        out["judge_metric"] = _file_metric(tree)
    return out


# ── 判 ────────────────────────────────────────────────────────────────────

def _rel(p: pathlib.Path) -> str:
    """相对路径；基线可能落在工作区之外（反控/临时树），相对不出来就原样打印。"""
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def _load_registry() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _entries() -> list[dict]:
    return _load_registry().get("entries", [])


def _verdicts(cands: list[Cand], reg: dict) -> tuple[list, list, list, list]:
    """窄口径那一层的四条：未登记 / 已登记但变大 / 没分类 / open 缺出路。"""
    bad, grew, unfilled, opens_bad = [], [], [], []
    for c in cands:
        e = reg.get(c.ident)
        if e is None:
            bad.append(c)
            continue
        if e.get("category") in (None, "", "?"):
            unfilled.append(c.ident)
            continue
        if c.size > int(e.get("size") or 0):
            grew.append((c, int(e.get("size") or 0)))
        if e.get("category") == "open" and not (e.get("replacement") and e.get("sunset")):
            opens_bad.append(c.ident)
    return bad, grew, unfilled, opens_bad


def _grown_files(now: dict, _oldby: dict) -> list:
    """穷举量那一层变大的文件。

    ⚠️ 形参名 `_oldby` 是**故意的**：`tests/test_wordlist_gate.py` ⑥ 的变异锁按字面钉住下面
    那句推导式（挖掉它 ⇒ 第二层不再红）。改这句要连那条锁一起改。
    **缺席按 0 算**（不是"跳过"）：新文件里的中文词表同属新增词表面，必须红。
    """
    return [(f, _oldby.get(f, [0, 0]), v) for f, v in now.items()
            if any(v[i] > int(_oldby.get(f, [0, 0])[i]) for i in (0, 1))]


def _print_reds(bad, grew, unfilled, opens_bad, metric_grew) -> int:
    """打出全部红项，返回红的总条数（0 = 没有红）。①②两层共用一个出口，别再各写一份。"""
    for c in bad:          # ① 窄口径：未登记
        print(f"  ❌ 未登记：{c.ident}  [{c.kind}, {c.size} 个中文"
              f"{'字符' if c.kind == 'regex' else '词条'}]  {c.sample}")
    for c, was in grew:
        print(f"  ❌ {c.ident}：{was} → {c.size} —— **只许变小**"
              f"（新的说法不是往表里加词的理由，见模块头注）")
    for i in unfilled:
        print(f"  ❌ 登记里 category 还没填（不确定就填 `open`，别凑 closed）：{i}")
    for i in opens_bad:
        print(f"  ❌ open 类没出路（必须同时给 replacement 与 sunset）：{i}")
    for f, was, now in sorted(metric_grew):   # ② 穷举量：任何文件变大
        print(f"  ❌ {f} 穷举量变大：集合中文串 {was[0]}→{now[0]}、正则中文字符 {was[1]}→{now[1]}"
              f" —— 新增中文词表不许悄悄进来（窄口径看不见的表也会在这里现形）")
    return len(bad) + len(grew) + len(unfilled) + len(opens_bad) + len(metric_grew)


def _print_greens(gone: list, opens: list) -> None:
    """没有红时打的两段：已销账（表没了 = 目标达成）与 open 的待收口清单。"""
    for i in sorted(gone):
        print(f"  💚 已销账（表没了 = 目标达成）：{i} —— 跑 `--update` 收紧")
    if opens:
        print(f"  ⏳ open 类还剩 {len(opens)} 条待收口：")
        for e in sorted(opens, key=lambda x: x["id"])[:14]:
            print(f"     · {e['id']}  → {e.get('replacement') or '（没写替代表征！）'}"
                  f"   [{e.get('sunset') or '（没写收口批次）'}]")
        if len(opens) > 14:
            print(f"     … 另有 {len(opens) - 14} 条，见 {_rel(BASELINE)}")


def _eval_filled(d: dict) -> bool:
    return bool(d.get("category")) and bool(d.get("why"))


def _print_eval_reds(now: dict, reg: dict) -> int:
    """③ eval 侧的红。**与①②的计数分开**（见模块头注 ③）。

    缺席两种，都不算红：①临时树里没有 eval 文件（`now` 空）；②这一层的关键词一个都没
    出现（例如只放了 `agent/` 的树）。真仓上两个文件都在 ⇒ 真仓上这一层常开。
    """
    if not now or not any(now.values()):
        return 0
    if not reg:
        print(f"  ❌ 登记表里**没有 `eval_vocab` 这一段** —— `eval/` 侧的词表族（golden 的"
              f"负正则/正反词表、六个豁免开关、run_golden 的四张共享族）此刻**没有任何机器"
              f"判据拦着**（跑 `--update` 出一次，再把每一条为什么不改写进 why）")
        return 1
    reds: list[str] = []
    reds += _eval_key_reds(now, reg)
    reds += _eval_exempt_reds(now, reg)
    reds += _eval_family_reds(now, reg)
    reds += _eval_metric_reds(now, reg)
    for r in reds:
        print(f"  ❌ {r}")
    return len(reds)


def _eval_key_reds(now: dict, reg: dict) -> list[str]:
    """四个文本断言键：未登记 / why 空 / 新模式 / 格数项数变大。"""
    reds: list[str] = []
    for k in EVAL_KEYS:
        got = now["keys"].get(k) or {}
        want = (reg.get("keys") or {}).get(k)
        if want is None:
            reds.append(f"`{k}` 在登记表里没有（新长出来的一个文本断言键？先登记理由）")
            continue
        if not _eval_filled(want):
            reds.append(f"`{k}` 的 category/why 还是空的（新增模式要在 why 里写清它守什么不变量）")
        newpat = [p for p in got.get("patterns", []) if p not in set(want.get("patterns") or [])]
        if newpat:
            show = "、".join(f"'{p[:44]}'" for p in newpat[:3])
            more = f" 等 {len(newpat)} 个" if len(newpat) > 3 else ""
            reds.append(f"`{k}` 多了 {len(newpat)} 个模式：{show}{more} —— 往评测判据里加词"
                        f"**先在这条的 why 里写理由**（或改成结构断言），再跑 `--update`")
        if got["sites"] > want["sites"] or got["items"] > want["items"]:
            reds.append(f"`{k}` 变大：{want['sites']} 格→{got['sites']} 格、"
                        f"{want['items']} 项→{got['items']} 项 —— **只许变小**")
    return reds


def _eval_exempt_reds(now: dict, reg: dict) -> list[str]:
    """六个豁免开关：未登记却被打开 / why 空 / 多被用例打开（豁免在替谁让路要看得见）。"""
    reds: list[str] = []
    for k in EVAL_EXEMPT_KEYS:
        got = set(now["exempt"].get(k) or [])
        want = (reg.get("exempt") or {}).get(k) or {}
        if not want:
            if got:
                reds.append(f"豁免开关 `{k}` 在登记表里没有，却被 {len(got)} 条用例打开了"
                            f"（豁免是判据替自己打的补丁，登记了才看得见它在为谁让路）")
            continue
        if not _eval_filled(want):
            reds.append(f"豁免开关 `{k}` 的 category/why 还是空的")
        added = sorted(got - set(want.get("cases") or []))
        if added:
            reds.append(f"豁免开关 `{k}` 多被 {len(added)} 条用例打开了："
                        f"{'、'.join(added[:4])}{' 等' if len(added) > 4 else ''} —— 这一处"
                        f"让路的判据是哪一个、为什么必须让，写进它的 why")
    return reds


def _eval_family_reds(now: dict, reg: dict) -> list[str]:
    """四张共享族：未登记 / why 空 / 变大 / **同数量改写**（逐条换措辞正是这一族的病）。"""
    reds: list[str] = []
    for nm, cur in (now.get("families") or {}).items():
        want = (reg.get("families") or {}).get(nm)
        if want is None:
            reds.append(f"共享族 `{nm}` 在登记表里没有（{EVAL_JUDGE} 里新长出来的一张表）")
            continue
        if not _eval_filled(want):
            reds.append(f"共享族 `{nm}` 的 category/why 还是空的")
        if cur["size"] > want.get("size", 0):
            reds.append(f"共享族 `{nm}` 变大：{want.get('size')} → {cur['size']} 个"
                        f"（{'中文字符' if cur['kind'] == 'regex' else '元素'}）—— 只许变小")
        elif cur["size"] == want.get("size") and cur["sha"] != want.get("sha"):
            reds.append(f"共享族 `{nm}` 被**同数量改写**（{cur['size']} 个元素，指纹 "
                        f"{want.get('sha')}→{cur['sha']}）—— 逐条替换措辞正是这一族的病"
                        f"（见 `ABSENCE_COMPOUND` 头注那次假红）")
    return reds


def _eval_metric_reds(now: dict, reg: dict) -> list[str]:
    """整份判据面的总量（集合中文串 / 含中文正则字符）——窄口径看不见的表在这里现形。"""
    jm_now, jm_old = now.get("judge_metric"), (reg.get("judge_metric") or None)
    if jm_now and jm_old and any(jm_now[i] > jm_old[i] for i in (0, 1)):
        return [f"`{EVAL_JUDGE}` 的判据面总量变大：集合中文串 {jm_old[0]}→{jm_now[0]}、"
                f"含中文正则字符 {jm_old[1]}→{jm_now[1]} —— 窄口径看不见的表在这里现形"
                f"（这是 eval 侧的量，别与上面那份合并）"]
    return []


def _print_eval_greens(now: dict, reg: dict) -> None:
    """③ 的收缩提示：**变小是目标达成、不是变差**，但要人跑一次 `--update` 把基线收紧。"""
    hints: list[str] = []
    for k in EVAL_KEYS:
        got = now["keys"].get(k) or {}
        want = (reg.get("keys") or {}).get(k) or {}
        if want and (got.get("sites", 0) < want.get("sites", 0)
                     or got.get("items", 0) < want.get("items", 0)):
            hints.append(f"`{k}` 已收缩 {want.get('sites')}→{got.get('sites')} 格 /"
                         f" {want.get('items')}→{got.get('items')} 项")
    for nm, cur in (now.get("families") or {}).items():
        want = (reg.get("families") or {}).get(nm) or {}
        if want and cur["size"] < want.get("size", 0):
            hints.append(f"共享族 `{nm}` 已收缩 {want.get('size')}→{cur['size']}")
    for k in EVAL_EXEMPT_KEYS:
        got, want = set(now["exempt"].get(k) or []), set(
            ((reg.get("exempt") or {}).get(k) or {}).get("cases") or [])
        if want and got < want:
            hints.append(f"豁免开关 `{k}` 少了 {len(want - got)} 处")
    if hints:
        print(f"  💚 eval 侧收缩（{len(hints)} 处）——跑 `--update` 收紧：")
        for h in hints[:8]:
            print(f"     · {h}")


def _print_eval_summary(now: dict) -> None:
    """③ 的一句话现状（红绿都打）。三个键各自的格/项/模式数是最常被引用的那三个读数。"""
    if not now or not now.get("keys"):
        return
    k = now["keys"]

    def brief(name: str) -> str:
        d = k.get(name) or {}
        return f"{d.get('sites', 0)} 格/{d.get('items', 0)} 项/{len(d.get('patterns') or [])} 模式"

    ex = sum(len(v) for v in (now.get("exempt") or {}).values())
    jm = now.get("judge_metric")
    print(f"[eval] 负正则 {brief('text_not_match_regex')}；正词表 {brief('text_contains')}；"
          f"反词表 {brief('text_not_contains')}；豁免 {ex} 处；"
          f"共享族 {len(now.get('families') or {})} 张；判据面 "
          f"{jm[0] if jm else '-'}/{jm[1] if jm else '-'}（集合中文串/含中文正则字符）")


def check() -> int:
    if not BASELINE.is_file():
        print(f"❌ 没有登记表 {_rel(BASELINE)}——先跑 `--dump` 出一次。")
        return 3
    try:
        cands, metric = scan()
        ev_now = scan_eval()
    except RuntimeError as e:
        print(f"❌ 跑不起来：{e}")
        return 3
    whole = _load_registry()
    # **扫描面是这个量的一部分**：改了 TARGETS ⇒ 拿旧基线的条目去比新面，多出来的半张脸会
    # 全被记成"未登记"、少掉的那半张脸会被记成"已销账"。这不是变差，是**换了一个量** ⇒ 退 3
    # （同 eval/type_gate.py 的 mypy 版本/解释器目标：量换了就不是同一个读数）。
    if list(whole.get("targets", [])) != list(TARGETS):
        print(f"❌ 换了个量：登记表 targets={whole.get('targets')} ≠ 扫描 targets={TARGETS}"
              f"——确认这是有意的之后跑 `--update` 重出（别把它当成「变好/变差」读）。")
        return 3
    reg = {e["id"]: e for e in _entries()}
    opens = [e for e in reg.values() if e.get("category") == "open"]
    gone = [i for i in reg if i not in {c.ident for c in cands}]
    metric_grew = _grown_files(metric["by_file"], whole.get("metric", {}).get("by_file", {}))
    verdicts = _verdicts(cands, reg)
    print(f"[扫描] 登记表候选 {len(cands)} 个 / 已登记 {len(reg)} 条"
          f"（open {len(opens)} 条待收口）；穷举量 {metric['total'][0]} 集合中文串 + "
          f"{metric['total'][1]} 正则中文字符 / {len(metric['by_file'])} 文件")
    _print_eval_summary(ev_now)
    n_red = _print_reds(*verdicts, metric_grew)
    n_eval = _print_eval_reds(ev_now, whole.get("eval_vocab") or {})
    if n_red or n_eval:
        if n_red:
            print(f"\n❌ {len(verdicts[0])} 未登记、{len(verdicts[1])} 条变大、"
                  f"{len(verdicts[2])} 未分类、{len(verdicts[3])} 条 open 缺出路、"
                  f"{len(metric_grew)} 个文件穷举量变大")
        if n_eval:
            print(f"\n❌ eval 侧 {n_eval} 项变差（**判据的判据**，与上面那份计数分开读："
                  f"运行时判据面少一张表 ≠ 评测判据面多一条正则）")
        return 1
    _print_greens(gone, opens)
    _print_eval_greens(ev_now, whole.get("eval_vocab") or {})
    print("✅ 没有变差")
    return 0


# ── 出表 / 放宽 ───────────────────────────────────────────────────────────

_NOTE = ("判据层「词表形状」的登记表。未登记即红；已登记的**只许变小**。"
         "category ∈ closed | system_data | parser | prompt_text | open；"
         "open 是病，必须带 replacement 与 sunset。`metric` 是穷举量（按文件），"
         "`eval_vocab` 是**评测判据面**（判据的判据）那一段——两段的量分开读，"
         "各自的意义见 eval/wordlist_gate.py 头注。")

_EVAL_NOTE = ("`eval/` 侧（判据的**判据**）的词表族棘轮。四块：`keys` = golden 三个文本断言键的"
              "模式/词条**全集** + 格数 + 项数；`exempt` = 六个豁免开关各被哪些用例打开；"
              "`families` = `eval/run_golden.py` 四张共享族的元素数 + 元素集指纹"
              "（抓「数量不变、条目被换掉」；拼接出来的正则只数得到字面量那几段，那种条目带 "
              "`partial: true` 标记、读作下界）；`judge_metric` = 该文件走一遍①②穷举量得到的补网。"
              "只许变小；**新增模式要先在这条自己的 why 里写清它守哪个不变量**再跑 `--update`。"
              "⚠️ 与上面 agent/tools 那份计数**分开读**，别混成一个量。"
              "⚠️ category 仍取那五档（不新开档）：这一段的 `closed` 读作**已定价接受、只许变小**"
              "——不是「划得完」（它们逐条都是中文词表，理由与代价写在各自的 why 里）。"
              "`--update` 只刷新读数，**category/q/why 一律保留**。")


def _keep_eval(old: dict, kind: str, name: str, now: dict) -> dict:
    """把已填的 category/q/why 保留下来，其余用现场重出（与 `entries` 同一条纪律）。"""
    prev = ((old or {}).get(kind) or {}).get(name) or {}
    keep = {k: v for k, v in prev.items() if k in ("category", "q", "why")}
    return {**now, **keep}


def _eval_vocab(old: dict | None) -> dict:
    """eval 侧那一段的新值（`--dump` 与 `--update` 共用）。"""
    old = old or {}
    now = scan_eval()
    return {
        "_note": _EVAL_NOTE,
        "golden": EVAL_GOLDEN,
        "judge": EVAL_JUDGE,
        "keys": {k: _keep_eval(old, "keys", k, v) for k, v in now["keys"].items()},
        "exempt": {k: _keep_eval(old, "exempt", k, {"cases": v})
                   for k, v in now["exempt"].items()},
        "families": {k: _keep_eval(old, "families", k, v)
                     for k, v in now["families"].items()},
        **({"judge_metric": now["judge_metric"]} if "judge_metric" in now else {}),
    }


def _write(payload: dict) -> None:
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def _dump() -> int:
    """出登记骨架：给每条一个空壳，category 留 `?` 逼人填。**不覆盖已有登记**。"""
    cands, metric = scan()
    whole = _load_registry() if BASELINE.is_file() else {}
    have = {e["id"]: e for e in whole.get("entries", [])}
    entries = []
    for c in cands:
        entries.append(have.get(c.ident) or {**c.as_json(), "category": "?", "q": "", "why": ""})
    _write({"_note": _NOTE, "targets": TARGETS, "metric": metric, "entries": entries,
            "eval_vocab": _eval_vocab(whole.get("eval_vocab"))})
    n_q = sum(1 for e in entries if e.get("category") == "?")
    print(f"已写出登记骨架：{len(entries)} 条 → {_rel(BASELINE)}"
          f"（其中 {n_q} 条 category 待填）")
    return 0


def update() -> int:
    """重出登记表：保留已填的 category/why/replacement/sunset，同步 size、现场与两份量。"""
    cands, metric = scan()
    whole = _load_registry() if BASELINE.is_file() else {}
    reg = {e["id"]: e for e in whole.get("entries", [])}
    entries = []
    for c in cands:
        keep = {k: v for k, v in reg.get(c.ident, {}).items()
                if k in ("category", "q", "why", "replacement", "sunset")}
        entries.append({**c.as_json(), **keep})
    _write({"_note": _NOTE, "targets": TARGETS, "metric": metric, "entries": entries,
            "eval_vocab": _eval_vocab(whole.get("eval_vocab"))})
    print(f"已重出：{len(entries)} 条 → {_rel(BASELINE)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="中文词表闸（未登记即红、只许变小）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dump", action="store_true", help="出登记骨架（不覆盖已填的内容）")
    g.add_argument("--update", action="store_true", help="重出登记表（唯一放宽通道）")
    args = ap.parse_args()
    if args.dump:
        return _dump()
    if args.update:
        return update()
    return check()


if __name__ == "__main__":
    sys.exit(main())
