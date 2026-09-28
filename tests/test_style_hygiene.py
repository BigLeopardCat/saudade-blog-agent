# -*- coding: utf-8 -*-
"""仓库形态的四条纪律（秒级、零网络、零 LLM、只读）。

**为什么值得有这一套**：这四样都属于"不会因为谁做坏事而出错、只会因为没人执行而慢慢烂掉"
的东西——它们的失效**没有任何东西会报**：

  ① **编码与换行**：本仓正文全是中文。一处 GBK 字节、一处 CRLF，就足以让 diff 显示成整文件
     重写、让按行处理的脚本读到乱码（`.gz` 语料那一族的教训同型：报表照样出、只是小一号）。
  ② **`.editorconfig` 是声明不是执行者**：声明与判据分家（文件里写 LF、测试查 CRLF）正是审计
     里"改一处必须同步另一处"那 94 处的形状 ⇒ 这里把声明**解析出来**与判据常量同源比对。
  ③ **CHANGELOG 的价值全在"能信"**：日期倒序、每条有正文、最新日期不晚于最后一笔提交。
     最后一条是**强制决定**：那一天"没有值得记的行为变更"也是一个决定，写成一行
     `- （这天只有文档/排版与测试补件，无行为变更）` 即可——不许默默不写。
  ④ **版本号只有一个来源**（`pyproject.toml`）：另立 `VERSION` 文件或 `__version__` 常量
     就是第二份名单，迟早对不上（同族教训：`rotate 14` 与实际最老 26 天、R2 的 `--keep 3`
     写了没人执行）。

用法：.venv/bin/python tests/test_style_hygiene.py
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 手写文本文件的扩展名（扫的就是这些）。**生成物不在内**：`eval/report/*.json` 是
# `json.dump` 写的历史读数，不带末尾换行是它的原生形态，改它没有意义（也改不回来）。
TEXT_EXT = {".py", ".md", ".toml", ".sh", ".yml", ".yaml", ".txt", ".cfg", ".ini",
            ".json", ".html", ".mjs", ".ts", ".css"}
TEXT_NAMES = {".gitignore", ".editorconfig", ".env.example"}
GENERATED = re.compile(r"^eval/report/.*\.json$")

# 判据常量：与 `.editorconfig` 的 `[*]` 段**同源**（下面 ② 会去解析那份文件并比对）
WANT = {"charset": "utf-8", "end_of_line": "lf",
        "insert_final_newline": "true", "trim_trailing_whitespace": "true"}


def _tracked() -> list[str] | None:
    """tracked 文件清单（`git ls-files`）。取不到 ⇒ None（调用方判红，不当跳过）。"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _parse_editorconfig(text: str) -> tuple[dict, dict]:
    """`.editorconfig` → (段外键, {段名: {键: 值}})。

    为什么不用 `configparser`：`.editorconfig` 允许 `root = true` 出现在**任何段之前**
    （那是它的规范），configparser 会直接抛 `MissingSectionHeaderError`。为一行约定引一个
    会抛的解析器不值当，这里的语法就三类行：注释 / `[段]` / `键 = 值`。
    """
    top: dict = {}
    sections: dict = {}
    cur = None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln[0] in "#;":
            continue
        if ln.startswith("[") and ln.endswith("]"):
            cur = ln[1:-1].strip()
            sections.setdefault(cur, {})
            continue
        if "=" in ln:
            k, v = ln.split("=", 1)
            (sections.setdefault(cur, {}) if cur is not None else top)[k.strip().lower()] = v.strip()
    return top, sections


def main() -> int:
    files = _tracked()
    print("① 编码与换行：tracked 的手写文本文件一律 UTF-8 / LF / 末尾换行 / 不用制表符缩进")
    if files is None:
        check("取到 tracked 清单（`git ls-files`）——取不到这条判据就没跑成，不算通过",
              False, "git 不可用或不在仓里")
        files = []

    bad_enc, bad_crlf, bad_nl, bad_tab, bad_ws, scanned = [], [], [], [], [], 0
    for rel in files:
        p = ROOT / rel
        if not p.is_file() or GENERATED.match(rel):
            continue
        if p.suffix not in TEXT_EXT and p.name not in TEXT_NAMES:
            continue
        scanned += 1
        raw = p.read_bytes()
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad_enc.append(rel)
            continue
        if b"\r\n" in raw:
            bad_crlf.append(rel)
        if txt and not txt.endswith("\n"):
            bad_nl.append(rel)
        if any(ln.startswith("\t") for ln in txt.splitlines()):
            bad_tab.append(rel)
        if any(ln != ln.rstrip() for ln in txt.splitlines()):
            bad_ws.append(rel)

    check("扫到了足量文件（不是把 glob 写空了）", scanned >= 100, f"{scanned} 份")
    check("全部是 UTF-8", not bad_enc, "、".join(bad_enc[:6]))
    check("没有 CRLF（换行一律 LF）", not bad_crlf, "、".join(bad_crlf[:6]))
    check("每个文件末尾都有换行", not bad_nl, "、".join(bad_nl[:6]))
    check("没有用制表符缩进的文件", not bad_tab, "、".join(bad_tab[:6]))
    check("没有行尾空白", not bad_ws, "、".join(bad_ws[:6]))

    print("\n② .editorconfig：声明与上面的判据同源（声明不是执行者）")
    ec = ROOT / ".editorconfig"
    check(".editorconfig 在位", ec.is_file())
    if ec.is_file():
        # 自己解析、不引第三方：`[*]` 段就是判据作用的范围
        top, sections = _parse_editorconfig(ec.read_text(encoding="utf-8"))
        star = sections.get("*", {})
        got = {k: str(star.get(k, "")).strip().lower() for k in WANT}
        check("`[*]` 段声明的四项与 ① 的判据逐项相同（改一处必须改另一处）",
              got == WANT, f"声明={got}")
        check("root = true（否则会去继承上层目录的约定）",
              str(top.get("root", "")).strip().lower() == "true", str(top))
        check("生成物目录有显式例外（声明与判据的例外范围一致）",
              "eval/report/*.json" in sections, str(sorted(sections)))
        check("　且例外只在生成物上（没把别的目录也放出去）",
              sum(1 for s in sections if "report" in s) == 1, str(sorted(sections)))

    print("\n③ CHANGELOG：日期倒序、每条有正文、最新日期不晚于最后一笔提交")
    cl = ROOT / "CHANGELOG.md"
    check("CHANGELOG.md 在位", cl.is_file())
    if cl.is_file():
        text = cl.read_text(encoding="utf-8")
        # 只认行首的 `## YYYYMMDD`（正文里提到日期的句子不算条目）
        secs = [(m.group(1), m.start(), m.end())
                for m in re.finditer(r"^##\s+(\d{8})\s*$", text, re.M)]
        check("至少有一条日期条目", len(secs) >= 1, f"{len(secs)} 条")
        dates = [d for d, _, _ in secs]
        check("日期倒序（最新在最上）", dates == sorted(dates, reverse=True), "、".join(dates))
        empty = []
        for i, (d, _s, e) in enumerate(secs):
            body = text[e:secs[i + 1][1] if i + 1 < len(secs) else len(text)]
            if not re.search(r"^-\s+\S", body, re.M):
                empty.append(d)
        check("每条日期下都有 `- ` 正文（哪怕是「这天无行为变更」那一行）",
              not empty, "、".join(empty))
        # 与真实历史对齐：这一条是**强制决定**——那天没什么可记也要写一行
        head = subprocess.run(["git", "log", "-1", "--date=short", "--format=%ad"],
                              cwd=ROOT, capture_output=True, text=True)
        latest = head.stdout.strip().replace("-", "")
        if len(latest) == 8 and latest.isdigit():
            check(f"最新条目日期 == 最后一笔提交日期（{latest}）", dates and dates[0] == latest,
                  f"条目={dates[0] if dates else None}")
        else:
            check("取到最后一笔提交的日期（取不到就不算通过）", False, repr(latest))
        check("最新条目日期不是未来", bool(dates) and dates[0] <= date.today().strftime("%Y%m%d"),
              dates[0] if dates else "")

    print("\n④ 版本号只有一个来源（pyproject.toml）")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    mv = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    check("pyproject 声明了 version", mv is not None, mv.group(1) if mv else "")
    check("没有 VERSION 文件（第二个来源＝迟早对不上的那份名单）",
          not (ROOT / "VERSION").exists() and not (ROOT / "VERSION.txt").exists())
    dup = []
    for rel in files:
        if not rel.endswith(".py") or rel.startswith("tests/") or rel.startswith("eval/"):
            continue
        if re.search(r"^__version__\s*=", (ROOT / rel).read_text(encoding="utf-8"), re.M):
            dup.append(rel)
    check("运行时代码里没有 __version__ 常量（要用版本就从 pyproject 读）",
          not dup, "、".join(dup))

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
