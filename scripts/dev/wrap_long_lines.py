"""把「套件迁移 +4 缩进」顶过 100 列的行**就地折行** —— 迁移配方的第三步。

为什么要有它：`scripts/dev/pytest_migrate.py` 把模块级判据搬进 `def test_<stem>()`，整块
+4 缩进 ⇒ 原本 97–100 列的行变成 101–104 列，`eval/lint_gate.py` 当场变脏。**基线不许因此
放宽**（"迁移顺带把闸放宽"是最容易蒙混过去的那种坏），所以那些行要折回来。

用法（`--base` 是**迁移前**的那个 ref，默认 `HEAD`）：

    .venv/bin/python scripts/dev/pytest_migrate.py --all
    .venv/bin/python scripts/dev/wrap_long_lines.py --base HEAD~1 tests/test_*.py
    .venv/bin/python scripts/dev/wrap_long_lines.py --base HEAD~1 --dry-run tests/test_*.py

⚠️ **迁移提交之后 `--base HEAD` 就是新文本** ⇒ 一条都认不出来（报"折了 0 行"）。这是
刻意的失败方向：认不出就不动，比乱折安全。

只碰**迁移顶长的**那些行，判据是三重指纹（缺一条都会误伤）：
  ① 现在 >100 列；② **去掉那 4 个前导空格后 ≤100 列**（⇒ 在 `--base` 那一版里它不是
  E501）；③ 去掉 4 个空格后**逐字出现在 `--base` 那一版里**（⇒ 就是这一行被顶过去的）。
基线里本来就长的行、以及 `def`/常量这些原样留下的行，一个字都不动。

折点（保守，只用**确定合法**的位置，`tokenize` 数括号深度，不靠正则猜）：
  ① 括号内的逗号之后；② 括号内的 `.` / `:` 之前；③ 括号内的二元运算符之前。
  三条都折不动（整行就是一个中文长句）⇒ 退到**劈字符串**：在标点处把那一个字面量劈成两个
  **相邻字面量**（隐式拼接，值逐字不变）。只在括号里劈 —— 括号外劈出来的续行会变成一条
  独立表达式语句，`unexpected indent`（`test_gate_replan` 实测）。

两个非显然的坑（都实测过）：
  · **ruff 的列宽不是字符数**：东亚宽字符算 2 列（实测 96 字符的 10 汉字行报
    `E501 Line too long (106 > 100)`）。本仓满屏中文，照字符数折会折了个寂寞。
  · **`f` 前缀要按半给**：劈点在最后一个 `{}` 之后时，后半段一个占位符都没有，还留着 `f`
    就是 `F541 f-string without any placeholders`（实测三处）。所以只在两半都有 `{` 时才劈
    f-string。同理反斜杠要数奇偶（`\\ ` 是被转义的空格，劈开就改值）。

折完当场 `ast.parse` 一遍，语法不过就**整个文件不写回**（宁可没折，不可折坏）。
"""
from __future__ import annotations

import argparse
import ast
import io
import pathlib
import re
import subprocess
import sys
import tokenize
import unicodedata

LIMIT = 100
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

_OPEN = {"(", "[", "{"}
_CLOSE = {")", "]", "}"}
_BINOPS = {"and", "or", "not", "is", "in", "==", "!=", "<", ">", "<=", ">=",
           "+", "-", "*", "/", "%", "|", "&", "^"}
# 可以劈在**这些字符之后**。中文句子里没有空格 ⇒ 只认空格等于对判据措辞束手无策；
# 而隐式拼接在**任何**位置都保义，选标点只是为了让折出来的代码还读得通。
_SPLIT_AT = " ，。、：；）】」》…,;"
_STR_HEAD = re.compile(r"^(?P<pre>[A-Za-z]*)(?P<q>'''|\"\"\"|'|\")")
_SKIP = (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
         tokenize.COMMENT, tokenize.ENDMARKER)


def _w(text: str) -> int:
    """ruff 的「列宽」：东亚宽字符算 2 列（见模块头注）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _scan(src: str) -> tuple[dict[tuple[int, int], int], dict[int, list]]:
    """(每个 token 起点 → 它**之前**的括号深度, 行号 → 该行的 token 列表)。"""
    depths: dict[tuple[int, int], int] = {}
    per_line: dict[int, list] = {}
    depth = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in _SKIP:
            continue
        depths[(tok.start[0], tok.start[1])] = depth
        per_line.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1],
                                                     tok.string, tok.type))
        if tok.type == tokenize.OP and tok.string in _OPEN:
            depth += 1
        elif tok.type == tokenize.OP and tok.string in _CLOSE:
            depth -= 1
    return depths, per_line


def _candidates(per_line: dict, depths: dict, line: int) -> list[int]:
    """结构折点（括号内的逗号之后 / `.`·`:` 之前 / 运算符之前）。"""
    out: list[int] = []
    for c0, c1, s, ty in per_line.get(line, []):
        if depths.get((line, c0), 0) < 1:
            continue
        if s == ",":
            out.append(c1)
        elif s in (".", ":"):
            out.append(c0)
        elif (ty == tokenize.NAME and s in _BINOPS) or \
                (ty == tokenize.OP and s in _BINOPS):
            out.append(c0)
    return out


def _string_cands(per_line: dict, depths: dict,
                  line: int) -> list[tuple[int, str, str]]:
    """字符串字面量里的折点：(偏移, 折点处补的收尾, 续行补的开头)。"""
    out: list[tuple[int, str, str]] = []
    for c0, c1, s, ty in per_line.get(line, []):
        if ty != tokenize.STRING or depths.get((line, c0), 0) < 1:
            continue                                     # 只在括号里劈（见模块头注）
        m = _STR_HEAD.match(s)
        if not m or m.group("q") in ("'''", '"""'):
            continue                                     # 三引号不碰（内容行要逐字保留）
        pre, quote = m.group("pre"), m.group("q")
        fstr = "f" in pre.lower()
        body_start, body_end = len(pre) + len(quote), len(s) - len(quote)
        braces = 0
        for k in range(body_start, body_end):
            ch = s[k]
            if fstr and ch == "{":
                braces += 1
            elif fstr and ch == "}":
                braces -= 1
            if ch not in _SPLIT_AT or braces:
                continue
            n, j = 0, k - 1                                # 反斜杠奇偶（`\\ ` 不能劈）
            while j >= body_start and s[j] == "\\":
                n, j = n + 1, j - 1
            if n % 2:
                continue
            if fstr and not ("{" in s[body_start:k + 1] and "{" in s[k + 1:body_end]):
                continue                                   # 有一半没占位符 ⇒ F541
            out.append((c0 + k + 1, quote, pre + quote))
    return out


def wrap_line(text: str, cands: list[int], strcands: list) -> str | None:
    """把一行折成两行；折不动返回 None。"""
    if _w(text) <= LIMIT:
        return None
    indent = len(text) - len(text.lstrip(" "))
    pad = " " * (indent + 4)
    best: tuple[int, str] | None = None

    def consider(off: int, head: str, tail: str) -> None:
        nonlocal best
        head, tail = head.rstrip(), tail.lstrip()
        if not tail or _w(head) > LIMIT or _w(pad + tail) > LIMIT:
            return
        score = max(_w(head), _w(pad + tail))
        if best is None or score < best[0]:
            best = (score, head + "\n" + pad + tail)

    for off in cands:
        consider(off, text[:off], text[off:])
    if best is None:                                       # 结构折点折不动 ⇒ 劈字符串
        for off, close, reopen in strcands:
            consider(off, text[:off] + close, reopen + text[off:])
    return best[1] if best else None


def _base_lines(path: pathlib.Path, base: str) -> set[str]:
    """`--base` 那一版里这个文件的逐行文本（rstrip）。取不到就返回空集 ⇒ 一行都不折。"""
    rel = path.resolve().relative_to(ROOT).as_posix()
    out = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=str(ROOT),
                         capture_output=True, text=True)
    return {ln.rstrip() for ln in out.stdout.split("\n")} if out.returncode == 0 else set()


def process(path: pathlib.Path, base: str, dry: bool) -> tuple[int, list[str]]:
    src = path.read_text(encoding="utf-8")
    depths, per_line = _scan(src)
    old = _base_lines(path, base)
    out: list[str] = []
    changed, stuck = 0, []
    for i, text in enumerate(src.split("\n"), start=1):
        # 三重指纹（见模块头注）—— 缺任何一条都会误伤"本来就长"的行
        if not (_w(text) > LIMIT and _w(text[4:]) <= LIMIT
                and text[4:].rstrip() in old):
            out.append(text)
            continue
        new = wrap_line(text, _candidates(per_line, depths, i),
                        _string_cands(per_line, depths, i))
        if new is None:
            stuck.append(f"{path.name}:{i}  {_w(text)} 列  {text.strip()[:66]}")
            out.append(text)
        else:
            changed += 1
            out.extend(new.split("\n"))
    if changed and not dry:
        new_src = "\n".join(out)
        try:
            ast.parse(new_src)                             # 折坏了当场炸，别写回去
        except SyntaxError as exc:
            stuck.append(f"{path.name}  ✗✗ 折坏了，整个文件没写回：{exc}")
            return 0, stuck
        path.write_text(new_src, encoding="utf-8")
    return changed, stuck


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=pathlib.Path)
    ap.add_argument("--base", default="HEAD", help="**迁移前**的那个 ref（默认 HEAD）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    total, stuck = 0, []
    for p in args.paths:
        n, s = process(p, args.base, args.dry_run)
        total += n
        stuck.extend(s)
    print(f"{'（dry-run）' if args.dry_run else ''}折了 {total} 行；折不动 {len(stuck)} 行")
    for s in stuck:
        print("  ✗ " + s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
