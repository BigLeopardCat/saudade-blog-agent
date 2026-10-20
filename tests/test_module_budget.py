"""模块体量**预算闸**（20261017）——治"改动不断叠进同一个文件"。

**要回答的问题**：本仓已经用「预算 + 台账」治过**函数级**复杂度
（`tests/test_complexity_budget.py` + `eval/complexity_baseline.json`），但**文件级**
没有任何机制看着。落地前实测：`agent/graph.py` **13994 行 / 259 个模块级定义**，
占全仓 Python 的 39%（31 个模块共 35927 行）——而它是**一天天长出来的**，不是一天
写成的。没有闸的时候，这件事唯一的可见性来自有人偶尔 `wc -l` 一下。

**判法**（`judge()`，一句话）：超上限的模块必须在台账里、且只许缩小；台账里的条目
一旦回到预算内就要销账；台账里找不到对应模块的条目是过期的。

台账 = `eval/module_baseline.json`，`--update` 是唯一的放宽通道——**reviewer 直接看
那份 diff**。"这一版又长胖了"必须在 diff 里留下痕迹，这正是本闸要买的东西。

**两个读数**：`lines`（文件行数）与 `top_defs`（**模块级**定义数：函数/类，不下钻
方法）。为什么两个都要——一个 3000 行的文件可以是 5 个大函数、也可以是 200 个小函数，
**治法是两回事**（前者拆函数，后者拆主题）。`top_defs` 只数模块级：嵌套函数与类方法
算在它们所属的那个顶层定义里，不另计。

**扫描面** = 全仓一级 `.py`（与 `test_complexity_budget.py` 同口径），只排掉产物目录。
**含 `tests/`**：同一条理由——「任何地方新写的大模块都要红」，不给测试开后门
（`tests/test_workflows.py` 5784 行本身就是一例）。

**本闸不是"禁止长文件"，是"长文件不许再长"**。真需要它变大，就在同一个提交里
`--update` 并说明为什么 ⇒ 把隐式增长变成一次需要辩护的动作。

**棘轮是双向的（20261018 补）**：只判"长大了"的话，一次大缩小之后台账会留下**永久免费
额度**——下一批增长只要没回到旧水位，就一格都不报。实测抓到过：`agent/graph.py` 台账记着
`13263 / 237`，盘上早就是 `13190 / 235`，那 73 行是**静默**的（`judge()` 当时只在
`实测 > 台账` 时红）。所以 `judge()` 现在两个方向都判：**比台账小也要红**（"台账该收紧"），
逼你在同一个提交里把那一格改小——`--update` 出来的账**恰好等于**当时的实测，两边本来就该相等。

用法（cd 仓根，用仓的 venv）：
  .venv/bin/python tests/test_module_budget.py                  # 检查（run_all.py 这样调）
  .venv/bin/python tests/test_module_budget.py --update         # 重写台账
  .venv/bin/python tests/test_module_budget.py --lines-max 400 --defs-max 30  # 临时换阈值
退出码：0 全在预算内 / 1 有模块超预算、长大了，或台账过期。
"""


import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


LEDGER = ROOT / "eval" / "module_baseline.json"


LINES_MAX = 800     # 文件行数上限：超过就进台账


DEFS_MAX = 60       # 模块级定义数上限：同上


_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "dist", ".mypy_cache"}


def measure(text: str) -> dict:
    """一份源码的两个读数。`lines` 与 `top_defs` 的口径见模块头注。"""
    tree = ast.parse(text)
    top = sum(1 for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return {"lines": len(text.splitlines()), "top_defs": top}


def scan() -> tuple[dict, int]:
    """扫描全仓，返回 `({模块: {"lines": n, "top_defs": m}}, 文件数)`。

    **解析失败不静默跳过**：一个读不进来的文件会让它整族从闸下溜走（"守卫静默跳过"
    正是本仓最贵的失效模式之一）⇒ 当场非零退出。
    """
    out: dict = {}
    n_files = 0
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        try:
            out[rel.as_posix()] = measure(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover
            raise SystemExit(f"❌ 解析不了 {rel.as_posix()}：{type(e).__name__}: {e}")
        n_files += 1
    return out, n_files


def judge(current: dict, ledger: dict, lines_max: int, defs_max: int) -> list[str]:
    """返回违规清单（空 = 全在预算内）。规则见模块头注。"""
    bad: list[str] = []
    for key, m in sorted(current.items()):
        base = ledger.get(key)
        if base is None:
            if m["lines"] > lines_max or m["top_defs"] > defs_max:
                bad.append(f"新增超预算  {key}  {m['lines']} 行（上限 {lines_max}）/ "
                           f"{m['top_defs']} 个模块级定义（上限 {defs_max}）"
                           f" —— 按主题拆开它，或 `--update` 记进台账")
            continue
        if m["lines"] <= lines_max and m["top_defs"] <= defs_max:
            bad.append(f"台账该销账  {key}  {m['lines']} 行 / {m['top_defs']} 定义"
                       f" 都已回到预算内 —— 删掉这一条（`--update` 会删）")
            continue    # 已在预算内，"收紧/长大"这两个方向都不再适用（只会重复报同一件事）
        if m["lines"] > base["lines"]:
            bad.append(f"行数长大    {key}  {m['lines']} 行（台账 {base['lines']}，只许缩小）")
        if m["top_defs"] > base["top_defs"]:
            bad.append(f"定义变多    {key}  {m['top_defs']} 个（台账 {base['top_defs']}，只许缩小）")
        # 反方向（见模块头注《棘轮是双向的》）：缩小了却没在同一个提交里收紧台账 ⇒ 也是红。
        # 不判这一条的话，一次大缩小会留下永久免费额度——`--update` 出来的账恰好等于当时的
        # 实测，所以"台账 == 实测"本来就是不变式，不等就是有人漏改了那一格。
        if m["lines"] < base["lines"] or m["top_defs"] < base["top_defs"]:
            bad.append(f"台账该收紧  {key}  实测 {m['lines']} 行 / {m['top_defs']} 定义，"
                       f"台账还记着 {base['lines']} / {base['top_defs']} —— 台账比实测松，"
                       f"把这一条改小（棘轮只许缩小）")
    for key in sorted(ledger):
        if key not in current:
            bad.append(f"台账过期    {key}  源码里没有这个文件了 —— 删掉这一条")
    return bad


def rewrite(current: dict, lines_max: int, defs_max: int) -> dict:
    """`--update` 的落点：台账 = **恰好**当前超预算的那批模块。

    ⚠️ 这里**不排序**：键序由 `dump()` 的 `sort_keys=True` 一处定死。两边都排会让
    "谁说了算"变成两处，而 `order_violation()` 的往返判据只在**一个**权威下成立。
    """
    return {k: v for k, v in current.items()
            if v["lines"] > lines_max or v["top_defs"] > defs_max}


def dump(ledger: dict) -> str:
    """`--update` 写盘用的**唯一**格式。读侧的 `order_violation()` 拿它做往返判据——
    两处**必须**是同一个函数，否则"写出来的"与"判出来的"会各走一套。
    """
    return json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def order_violation(raw: str) -> str | None:
    """盘上台账的**字节**是否就是 `--update` 会写出来的那一份；不是就返回一句说明。

    为什么这值得一条判据（20261018 审计 A5）：`--update` 的产物是排序过的，而盘上这份
    台账历史上是手写落地的、**键序不排序**。只要不校正一次，**下次任何一条触发
    `--update`，diff 就是整份文件重排**——"这一条真的变了"被埋进噪声里，而这道闸的
    全部价值就是让人看得见那份 diff（`--update` 是唯一的放宽通道）。

    判法是**往返比字节**（不是自己再写一套排序规则）：把盘上解析出来的 dict 按 `dump()`
    重新序列化，与原文逐字比。这样"格式"与"顺序"一次判完，将来改 `dump()` 也不会让
    判据与它脱钩。
    """
    want = dump(json.loads(raw))
    if want == raw:
        return None
    return (f"{LEDGER.relative_to(ROOT)} 的盘上格式/键序与 `--update` 的产物不一致"
            f"（往返 {len(want)} 字节 vs 盘上 {len(raw)} 字节）——"
            f"跑一次 `--update` 校正。⚠️ 那一次 diff **只该动键序与空白，值一个都不许变**；"
            f"值变了就是另一次放宽，要单独说理")


_SELF_FLAT = '''\
def a():
    pass


class B:
    def m(self):
        pass


async def c():
    pass
'''


_SELF_NESTED = '''\
def outer():
    def inner():
        pass

    class C:
        pass
'''


def _self_check() -> None:
    """正控 + 反控各来一遍：计数器数得对、`judge` 该红的红、该绿的不红。"""
    m = measure(_SELF_FLAT)
    assert m["top_defs"] == 3, f"模块级定义应数 3 个（a/B/c），实得 {m['top_defs']}"
    assert m["lines"] == 11, f"行数口径漂了：{m['lines']}"
    assert measure(_SELF_NESTED)["top_defs"] == 1, "嵌套函数/类方法不该另计"

    over = {"lines": 900, "top_defs": 70}
    small = {"lines": 100, "top_defs": 5}

    # ① 台账空 + 超预算 ⇒ 红；台账空 + 不超 ⇒ 绿
    assert judge({"x.py": over}, {}, 800, 60), "新增超预算没红 —— 闸是空的"
    assert not judge({"x.py": small}, {}, 800, 60), "没超预算却红了"

    # ② 台账在 + 长大 ⇒ 红；原样 ⇒ 绿；两个读数各自独立触发
    assert judge({"x.py": {"lines": 901, "top_defs": 70}},
                 {"x.py": over}, 800, 60), "行数长大没红"
    assert judge({"x.py": {"lines": 900, "top_defs": 71}},
                 {"x.py": over}, 800, 60), "定义变多没红"
    assert not judge({"x.py": over}, {"x.py": over}, 800, 60), "没动却红了"

    # ③ 回到预算内 ⇒ 该销账（红）；④ 源码里没有这个文件 ⇒ 过期（红）
    assert judge({"x.py": small}, {"x.py": over}, 800, 60), "回到预算内没提示销账"
    assert judge({}, {"x.py": over}, 800, 60), "台账过期没红"

    # ⑤ **反方向**：还在预算外、但比台账小了 ⇒ 也要红（否则缩小会留下永久免费额度）。
    #    两个读数各自独立触发，且"相等"不许红（那是 `--update` 之后的正常态）。
    _shrunk = judge({"x.py": {"lines": 850, "top_defs": 70}}, {"x.py": over}, 800, 60)
    assert _shrunk and any("收紧" in b for b in _shrunk), \
        f"台账比实测松却没红 —— 缩小之后会留下永久免费额度：{_shrunk}"
    assert judge({"x.py": {"lines": 900, "top_defs": 60}}, {"x.py": over}, 800, 60), \
        "定义变少却没提示收紧"
    assert not judge({"x.py": {"lines": 900, "top_defs": 70}},
                     {"x.py": {"lines": 900, "top_defs": 70}}, 800, 60), \
        "台账与实测相等却红了 —— 那是 `--update` 之后的正常态"

    # ⑥ 盘上格式/键序漂了 ⇒ 红；恰好是 `dump()` 的产物 ⇒ 绿（20261018 审计 A5）
    unordered = {"b.py": over, "a.py": small}
    assert order_violation(dump(unordered)) is None, "自写自读却判红 —— 判据是空的"
    assert order_violation(json.dumps(unordered, indent=2) + "\n") is not None, \
        "键序没排好却没红（`dump()` 排、盘上不排，正是 A5 那一格）"
    assert order_violation(json.dumps(dict(sorted(unordered.items())),
                                      ensure_ascii=False, indent=2)) is not None, \
        "少了结尾换行却没红"


def main() -> int:
    ap = argparse.ArgumentParser(description="模块体量预算闸")
    ap.add_argument("--update", action="store_true", help="按当前扫描重写台账")
    ap.add_argument("--lines-max", type=int, default=LINES_MAX)
    ap.add_argument("--defs-max", type=int, default=DEFS_MAX)
    args = ap.parse_args()

    _self_check()

    current, n_files = scan()
    if args.update:
        LEDGER.write_text(dump(rewrite(current, args.lines_max, args.defs_max)),
                          encoding="utf-8")
        print(f"台账已重写：{LEDGER.relative_to(ROOT)}（{len(rewrite(current, args.lines_max, args.defs_max))} 条）")
        return 0

    raw = LEDGER.read_text(encoding="utf-8") if LEDGER.exists() else ""
    ledger = json.loads(raw) if raw else {}
    bad = judge(current, ledger, args.lines_max, args.defs_max)
    print(f"扫描 {n_files} 个文件；台账 {len(ledger)} 条；"
          f"预算 lines≤{args.lines_max} / top_defs≤{args.defs_max}")
    if raw:
        drift = order_violation(raw)
        if drift:
            bad.append(drift)
    if bad:
        print("\n".join("✗ " + b for b in bad))
        print(f"\n{len(bad)} 条违规。**别急着 --update**：先看能不能按主题拆。")
        return 1
    print("✓ 全部模块都在预算内（或台账内只减不增）")
    return 0


def test_module_budget():


    _argv = sys.argv
    if "pytest" in sys.modules:      # 只在 pytest 轨收窄（直跑轨的 CLI 照旧可用）
        sys.argv = _argv[:1]
    try:
        _rc = main()
    finally:
        sys.argv = _argv
    assert _rc == 0, "main() 非零：上面那些 ❌ 就是原因"


if __name__ == "__main__":
    test_module_budget()
