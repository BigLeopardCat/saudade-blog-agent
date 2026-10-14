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

**为什么非要①不可**：②只数总量，验不了"这张表该不该存在"；而"哪张表是病、替代是什么"
正是这次要交付的清单（roadmap §6 的待办）。①提供姓名、类别与出路，②保证①没有漏网。

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
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "eval" / "judge_registry.json"

# 查什么：**运行时的判据面**。`tests/`、`eval/` 不进这道闸——它们是判据的**判据**，
# 自己就充满中文断言（`eval/` 侧的词表族由批 4 单独处置，别在这里混成一个量）。
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

    不下钻是关键：`SKILLS = [Skill(name="…", desc="中文"), …]` 的直接元素是 Call、
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


def check() -> int:
    if not BASELINE.is_file():
        print(f"❌ 没有登记表 {_rel(BASELINE)}——先跑 `--dump` 出一次。")
        return 3
    try:
        cands, metric = scan()
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
    n_red = _print_reds(*verdicts, metric_grew)
    if n_red:
        print(f"\n❌ {len(verdicts[0])} 未登记、{len(verdicts[1])} 条变大、"
              f"{len(verdicts[2])} 未分类、{len(verdicts[3])} 条 open 缺出路、"
              f"{len(metric_grew)} 个文件穷举量变大")
        return 1
    _print_greens(gone, opens)
    print("✅ 没有变差")
    return 0


# ── 出表 / 放宽 ───────────────────────────────────────────────────────────

_NOTE = ("判据层「词表形状」的登记表。未登记即红；已登记的**只许变小**。"
         "category ∈ closed | system_data | parser | prompt_text | open；"
         "open 是病，必须带 replacement 与 sunset。`metric` 是穷举量（按文件），"
         "两个层各自的意义见 eval/wordlist_gate.py 头注。")


def _write(payload: dict) -> None:
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def _dump() -> int:
    """出登记骨架：给每条一个空壳，category 留 `?` 逼人填。**不覆盖已有登记**。"""
    cands, metric = scan()
    have = {e["id"]: e for e in _entries()} if BASELINE.is_file() else {}
    entries = []
    for c in cands:
        entries.append(have.get(c.ident) or {**c.as_json(), "category": "?", "q": "", "why": ""})
    _write({"_note": _NOTE, "targets": TARGETS, "metric": metric, "entries": entries})
    n_q = sum(1 for e in entries if e.get("category") == "?")
    print(f"已写出登记骨架：{len(entries)} 条 → {_rel(BASELINE)}"
          f"（其中 {n_q} 条 category 待填）")
    return 0


def update() -> int:
    """重出登记表：保留已填的 category/why/replacement/sunset，同步 size、现场与穷举量。"""
    cands, metric = scan()
    reg = {e["id"]: e for e in _entries()} if BASELINE.is_file() else {}
    entries = []
    for c in cands:
        keep = {k: v for k, v in reg.get(c.ident, {}).items()
                if k in ("category", "q", "why", "replacement", "sunset")}
        entries.append({**c.as_json(), **keep})
    _write({"_note": _NOTE, "targets": TARGETS, "metric": metric, "entries": entries})
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
