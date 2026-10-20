#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHANGELOG 的**碎片机制**：碎片本身合不合法、组装器对不对、端到端幂等不幂等。

**这一套件为什么存在**（20261019）：`CHANGELOG.md` 只有一个插入点（当日那一节的组首）
⇒ 任何两个同时开着的 PR 都必然在那里冲突（实测 PR #21 / #22 / #23 连着三次同一处）。
换成"每条变更一个新文件"（`changelog.d/<YYYYMMDD>-<slug>.md`）之后，两个 PR 各加一个
文件 ⇒ 三路合并永远不冲突。工具在 `scripts/dev/changelog.py`，约定写在
`changelog.d/README.md`。

**机制换了，判据就得跟着换**：手写 CHANGELOG 时"忘了记"看得见（文件没动）；换成碎片之后
**开错了碎片名 / 正文写得不成条目 / 目录整个被删掉**，全都会**静默**变成"没有待汇总的碎片，
什么都不用做"——正是本仓点名的那类最贵失效。所以这里判的七条：

  ① `changelog.d/` 与它的 README **在位**（目录没了 ⇒ 下面全绿也是假的，这一条是覆盖率自检）；
  ② 真碎片逐个合法（文件名形状、真实日期、正文以 `- ` 开头、无冲突标记、无 `## ` 标题）；
  ③ 同一天里**没有两条正文相同**的碎片（同一段被抄两遍 ⇒ 会印两遍）；
  ④ 碎片日期**不是未来**（写错年份会让它永远排在最上面，而且没人会注意到）；
  ⑤ `CHANGELOG.md` 自己**没有冲突标记**（这条此前没人判——解冲突解歪了是静默的）；
  ⑥ 组装器：合成的 CHANGELOG + 合成的碎片，两侧都在、位置对、日期仍倒序；
  ⑦ 端到端：临时目录里跑一次 `build()` ⇒ 折进去且碎片被删，**再跑一次一字不变**。

**反控**（本仓纪律「正控不红不算数」）：七条合成输入各自必须报红——非法文件名 / 不存在的
日期 / slug 有大写 / 正文不以 `- ` 开头 / 正文里有冲突标记 / 正文里有 `## ` / 正文是空的。

用法：.venv/bin/python tests/test_changelog_fragments.py
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "dev"))

import changelog as C  # noqa: E402  （dev 工具，不是包；按路径取）

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── ① 机制在位（覆盖率自检）──────────────────────────────────────────────
def test_changelog_fragments():
    print("① 机制在位：目录与说明（目录没了 ⇒ 下面全绿也是假的）")
    check("`changelog.d/` 目录在位", C.FRAG_DIR.is_dir(), str(C.FRAG_DIR.relative_to(ROOT)))
    check("`changelog.d/README.md` 在位（约定写在里面，不是只写在脚本注释里）",
          (C.FRAG_DIR / "README.md").is_file())
    check("`scripts/dev/changelog.py` 在位", (ROOT / "scripts" / "dev" / "changelog.py").is_file())

    # ── ② 真碎片逐个合法 ────────────────────────────────────────────────────
    print("\n② 真碎片逐个合法（文件名形状 / 真实日期 / 正文成条目）")
    _frags = C.read_fragments()          # 不合法会抛 FragmentError ⇒ 这里当场炸
    check("真碎片全部读得出来（不合法会抛，不会静默跳过）", True, f"{len(_frags)} 条")
    check("碎片的正文都过 `body_problems`（空表 = 合法）",
          all(not C.body_problems(b) for _d, _n, b in _frags),
          str([(n, C.body_problems(b)) for _d, n, b in _frags if C.body_problems(b)]))
    check("碎片日期都是真实日期", all(C.is_real_date(d) for d, _n, _b in _frags),
          str([d for d, _n, _b in _frags if not C.is_real_date(d)]))

    # ── ③ 同一天里没有两条正文相同 ──────────────────────────────────────────
    print("\n③ 同一天里没有两条正文相同的碎片（同一段抄两遍会印两遍）")
    _dupes: list[str] = []
    _by_date: dict[str, set[str]] = {}
    for _d, _n, _b in _frags:
        if _b in _by_date.setdefault(_d, set()):
            _dupes.append(f"{_d}:{_n}")
        _by_date[_d].add(_b)
    check("没有重复正文", not _dupes, "、".join(_dupes))

    # ── ④ 日期不是未来 ─────────────────────────────────────────────────────
    print("\n④ 碎片日期不是未来（写错年份会让它永远排在最上面）")
    _today = C.today_cn()
    _future = [n for d, n, _b in _frags if d > _today]
    check(f"没有未来的碎片（本仓钟面今天 = {_today}）", not _future, "、".join(_future))

    # ── ⑤ CHANGELOG 自己不许留冲突标记 ──────────────────────────────────────
    print("\n⑤ `CHANGELOG.md` 里没有冲突标记（解冲突解歪了此前没人判）")
    _cl_text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    _marks = C.CONFLICT_RE.findall(_cl_text)
    check("没有 `<<<<<<<` / `=======` / `>>>>>>>`", not _marks, f"{len(_marks)} 处")

    # ── ⑥ 组装器：正控 ─────────────────────────────────────────────────────
    print("\n⑥ 组装器：合成输入上的正控与反控")

    _CL = "# 变更记录\n\n## 20261019\n\n- **既有条目**。\n\n## 20261018\n\n- **更早的条目**。\n"
    _ONE = ("20261019", "20261019-zzz.md", "- **ZZZ**（同一天、文件名靠后）。")
    _TWO = ("20261019", "20261019-aaa.md", "- **AAA**（同一天、文件名靠前）。")
    _NEW = ("20261020", "20261020-new.md", "- **NEW**（新的一天，要新建一节）。")
    _out = C.assemble(_CL, [_ONE, _TWO, _NEW])
    check("三条碎片都折进去了",
          all(s in _out for s in ("ZZZ", "AAA", "NEW", "既有条目", "更早的条目")))
    check("新建的日期节排在更早的日期之前", _out.index("## 20261020") < _out.index("## 20261019"))
    check("同一天内按**文件名**升序（AAA 在 ZZZ 之前）", _out.index("AAA") < _out.index("ZZZ"))
    check("插在当日那一节的组首（新条目在既有条目之前）",
          _out.index("ZZZ") < _out.index("既有条目"))
    check("结果仍是日期倒序",
          [d for d in re.findall(r"^## (\d{8})$", _out, re.M)]
          == ["20261020", "20261019", "20261018"])
    check("空碎片表 ⇒ 一字不动", C.assemble(_CL, []) == _CL)
    check("折完每节都还有 `- ` 正文（没有把某一节掏空）",
          re.search(r"^## 20261019\n\n- ", _out, re.M) is not None)

    for _tag, _name, _body in (
            ("文件名没有日期前缀", "zzz.md", "- 甲。"),
            ("日期不是真实日期", "20261332-x.md", "- 甲。"),
            ("slug 有大写", "20261019-Aaa.md", "- 甲。"),
            ("正文不以 `- ` 开头", "20261019-x.md", "甲。"),
            ("正文里有冲突标记", "20261019-x.md", "- 甲。\n<<<<<<< HEAD\n"),
            ("正文里有 `## ` 标题", "20261019-x.md", "- 甲。\n## 20261020\n"),
            ("正文是空的", "20261019-x.md", "   \n")):
        try:
            C.parse_fragment(_name, _body)
            _ok = False
        except C.FragmentError:
            _ok = True
        check(f"反控·{_tag} ⇒ 必须报错（不许静默跳过）", _ok, f"{_name}")

    # ── ⑦ 端到端：折进去 + 删碎片 + 幂等 ─────────────────────────────────────
    print("\n⑦ 端到端：临时目录里跑一次 ⇒ 折进去且碎片被删；再跑一次一字不变")
    _tmp = pathlib.Path(tempfile.mkdtemp(prefix="frag-"))
    try:
        (_tmp / "changelog.d").mkdir()
        (_tmp / "changelog.d" / "README.md").write_text("x\n", encoding="utf-8")
        (_tmp / "changelog.d" / "20261019-e2e.md").write_text(
            "- **端到端条目**。\n", encoding="utf-8")
        (_tmp / "CHANGELOG.md").write_text(_CL, encoding="utf-8")
        _save = (C.FRAG_DIR, C.CHANGELOG)
        C.FRAG_DIR, C.CHANGELOG = _tmp / "changelog.d", _tmp / "CHANGELOG.md"
        try:
            _n1, _msg1 = C.build(apply=True)
            _after1 = C.CHANGELOG.read_text(encoding="utf-8")
            _n2, _msg2 = C.build(apply=True)
            _after2 = C.CHANGELOG.read_text(encoding="utf-8")
        finally:
            C.FRAG_DIR, C.CHANGELOG = _save
        check("第一次折进 1 条", _n1 == 1 and "端到端条目" in _after1, _msg1)
        check("碎片被删掉（留着会被再折一遍）",
              not (_tmp / "changelog.d" / "20261019-e2e.md").exists())
        check("README.md 不被当碎片删掉（它是目录自己的说明）",
              (_tmp / "changelog.d" / "README.md").exists())
        check("★ 再跑一次**一字不变**（幂等：没有碎片就不该动 CHANGELOG）",
              _n2 == 0 and _after2 == _after1, _msg2)
        _save = (C.FRAG_DIR, C.CHANGELOG)
        (_tmp / "changelog.d" / "20261019-e2e.md").write_text(
            "- **再看一眼**。\n", encoding="utf-8")
        C.FRAG_DIR, C.CHANGELOG = _tmp / "changelog.d", _tmp / "CHANGELOG.md"
        try:
            _n3, _msg3 = C.build(apply=False)
        finally:
            C.FRAG_DIR, C.CHANGELOG = _save
        check("`--check` 只说会做什么、**不写盘**（写盘了就等于偷偷汇总）",
              _n3 == 1 and _tmp.joinpath("CHANGELOG.md").read_text(encoding="utf-8") == _after2,
              _msg3)
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    print()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print(f"✅ 全绿（待汇总碎片 {len(_frags)} 条）")


if __name__ == "__main__":
    test_changelog_fragments()
