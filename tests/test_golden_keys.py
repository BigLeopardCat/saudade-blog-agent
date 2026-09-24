# -*- coding: utf-8 -*-
"""golden 断言键的拼写校验（20260924）。

**要治的病**：`gold` 是个 dict，键名拼错**不报错、不告警、断言直接不执行**——用例照样绿。
判据看着在、其实不在，这是最难发现的一类失效。已经抓到一条活的：`attack_embed_command`
把注释键 `_note` 写成了 `note`（从未被读过，那句解释等于没写）。

三向交叉（硬编码表负责语义分类，反射负责防漂移，两者互为对方的哨兵）：
  ① **代码 → 表**：扫 `eval/run_golden.py` / `eval/golden_case_runner.py` 源码里所有
     `gold.get("X")` / `gold["X"]` / `g.get("X")` / `g["X"]` 字面量，必须都在
     `GOLD_ASSERT_KEYS ∪ GOLD_REQUEST_KEYS ∪ GOLD_ROUND_KEYS` 里（新读一个键却没改表 ⇒ 红）。
  ② **表 → 代码**：表里每个键必须真在源码里被读（删了实现却留着表项 ⇒ 红）。
  ③ **用例 → 四类之并**：逐条扫 `eval/golden/basic.jsonl`（多轮用例要**走进每一轮**的
     gold——双轮用例的判据全在轮里，只扫顶层等于它一个键都没受校验），每个 gold 键必须属于
     断言键 / 请求键 / 注释键 / 轮次键之一（`note` 这种拼错的第三个名字 ⇒ 红，并点名是哪条用例）。
另加一条**动态取键**的守卫：源码里任何 `g.get(` / `gold[` 后面不跟字符串字面量的写法，
都会让上面三条全部失效（键名在运行期才知道，反射扫不到）——一律判红，要求改成字面量。

秒级、纯文本 + json，无网络无 LLM；由 eval.yml 在 push 时跑。

用法：.venv/bin/python tests/test_golden_keys.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(ROOT))

import run_golden as rg  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# 读了 gold 键的两个文件（第三个消费方 golden_full_run.py 只读用例文件顶层字段，
# 不碰 gold 内部）。表里的键与这两个文件双向核对。
CONSUMERS = ("eval/run_golden.py", "eval/golden_case_runner.py")

# 字面量取键的两种写法（点号与下标），`(g|gold)` 覆盖两处局部变量名。
_LITERAL = (
    re.compile(r"""\b(?:gold|g)\.get\(\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1"""),
    re.compile(r"""\b(?:gold|g)\[\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]"""),
)
# 非字面量取键：`g.get(` 后面不是引号 ⇒ 键名运行期才知道，反射扫不到。
_DYNAMIC = re.compile(r"""\b(?:gold|g)\.get\(\s*[^"'\s)]|\b(?:gold|g)\[\s*[^"'\]]""")

KNOWN = rg.GOLD_ASSERT_KEYS | rg.GOLD_REQUEST_KEYS | rg.GOLD_ROUND_KEYS


def scan_keys(text: str) -> set[str]:
    out: set[str] = set()
    for pat in _LITERAL:
        out.update(m.group(2) for m in pat.finditer(text))
    return out


print("① 代码读了哪些键（反射扫源码字面量）")
_src: dict[str, str] = {}
_read: set[str] = set()
for rel in CONSUMERS:
    _src[rel] = (ROOT / rel).read_text(encoding="utf-8")
    _k = scan_keys(_src[rel])
    _read |= _k
    print(f"    {rel}: {len(_k)} 个键")
check("源码里没有动态取键（键名非字面量 ⇒ 反射扫不到，三向交叉全部失效）",
      not any(_DYNAMIC.search(t) for t in _src.values()),
      "；".join(rel for rel, t in _src.items() if _DYNAMIC.search(t)))
check("源码读到的每个键都在表里（新读一个键必须同步改表）",
      _read <= KNOWN, f"表外：{sorted(_read - KNOWN)}")

print("\n② 表里的每个键都真的被读（防「表里留着已删的键」）")
check("GOLD_ASSERT_KEYS 无孤儿", rg.GOLD_ASSERT_KEYS <= _read,
      f"未被读：{sorted(rg.GOLD_ASSERT_KEYS - _read)}")
check("GOLD_REQUEST_KEYS 无孤儿", rg.GOLD_REQUEST_KEYS <= _read,
      f"未被读：{sorted(rg.GOLD_REQUEST_KEYS - _read)}")
# 轮次键（`round`/`confirm_message`）与请求键同款：它们真被读，只是读的地方是轮次驱动
# （`run_case` 逐轮归一 gold），不是 `check_gold`。
check("GOLD_ROUND_KEYS 无孤儿", rg.GOLD_ROUND_KEYS <= _read,
      f"未被读：{sorted(rg.GOLD_ROUND_KEYS - _read)}")
_TABLES = {
    "GOLD_ASSERT_KEYS": rg.GOLD_ASSERT_KEYS,
    "GOLD_REQUEST_KEYS": rg.GOLD_REQUEST_KEYS,
    "GOLD_ROUND_KEYS": rg.GOLD_ROUND_KEYS,
    "GOLD_COMMENT_KEYS": rg.GOLD_COMMENT_KEYS,
}
_overlap = [f"{a}∩{b}={sorted(_TABLES[a] & _TABLES[b])}"
            for i, a in enumerate(_TABLES) for b in list(_TABLES)[i + 1:]
            if _TABLES[a] & _TABLES[b]]
check("四类键互不重叠（一个键只能属于一类，否则「是哪一类」没有答案）",
      not _overlap, "；".join(_overlap))
check("注释键只有 `_note` 一个", rg.GOLD_COMMENT_KEYS == {"_note"},
      str(sorted(rg.GOLD_COMMENT_KEYS)))

print("\n③ 逐条扫用例文件：每个 gold 键都属于四类之一")
CASES_FILE = ROOT / "eval/golden/basic.jsonl"
_lines = [ln for ln in CASES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
_cases = [json.loads(ln) for ln in _lines]
_unknown: list[str] = []
for case in _cases:
    for k in (case.get("gold") or {}):
        if k not in KNOWN | rg.GOLD_COMMENT_KEYS:
            _unknown.append(f"{case.get('id')}: gold.{k}")
    # 多轮用例（20260925 起的 `rounds`）的 gold 写在各轮里——本检查必须跟着走进去，
    # 否则双轮用例的全部断言键**一个都不受拼写校验**（本轮加的第一条真写用例正是这种
    # 形状：它整个判据都在轮里，漏扫 = 拼错也没人知道）。
    for i, rnd in enumerate(case.get("rounds") or [], 1):
        for k in (rnd.get("gold") or {}):
            if k not in KNOWN | rg.GOLD_COMMENT_KEYS:
                _unknown.append(f"{case.get('id')}: rounds[{i}].gold.{k}")
check("没有拼错的 gold 键（拼错 = 那段断言静默不执行）", not _unknown, "；".join(_unknown))
# 单独点名 `note`：它是抓到的第一例（`_note` 少一个下划线）。后来人若复制粘贴了那一行，
# 报错里直接给出正解。
check("没有裸 `note`（注释键是 `_note`；写成 `note` 等于这条注释不存在）",
      not any(u.endswith("gold.note") for u in _unknown))
_ids = [c.get("id", "?") for c in _cases]
check("用例数（127 条）", len(_ids) == 127, f"实际 {len(_ids)}")
check("用例 id 无重复", len(_ids) == len(set(_ids)),
      f"重复：{sorted({i for i in _ids if _ids.count(i) > 1})}")
# 每条用例至少带一个**断言**键——只有注释的用例等于没判。这不是拼写问题，但属同一族
# 失效（看着有、其实没有），顺手在同一处拦下。多轮用例的断言在各轮里，取并集
# （判据照 `rg.iter_rounds` 走，不在这里自己写一套"哪一轮算数"）。
_no_assert = [c.get("id") for c in _cases
              if not (set().union(*(set(r["gold"]) for r in rg.iter_rounds(c)))
                      & rg.GOLD_ASSERT_KEYS)]
check("每条用例都至少带一个断言键（只有注释的用例等于没判）", not _no_assert,
      "；".join(_no_assert))
# 反面：多轮用例顶上再写一个 `gold`。`iter_rounds` 有 `rounds` 时**只**看轮内的 gold
# （顶层那个从此没有任何读者）——写了它等于给自己一个"这条用例判过了"的错觉。同一族
# 失效，照上面的理由在这里一起拦。
_ghost_gold = [c.get("id") for c in _cases if c.get("rounds") and c.get("gold")]
check("多轮用例不写顶层 gold（有 rounds 时它一个读者都没有）", not _ghost_gold,
      "；".join(_ghost_gold))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
