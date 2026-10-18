# -*- coding: utf-8 -*-
"""手抄表的机器锁 · 第五站：**写通道的两个枚举词表**（20261013，行业规范收口批 3f）。

**契约**：agent 通过 `/api/protect/notes` 与标签接口写东西，有两个字段不是自由文本而是
**枚举**，取值域在两处各写一份：

  | 词表 | agent 侧产出 | Rust 侧认得 |
  |---|---|---|
  | 标签层级 | `normalize_level`、`level_of` → `one`/`two` | `tags.rs::parse_level` |
  | 文章状态 | `normalize_status` → `public`/`private`/`draft` | `notes.rs` 的两个写函数 |

层级两侧都**拒**认不出的词（`parse_level` 是唯一的解析点）；状态**只有 agent 侧在拒**
——后端照单全收（那几处比较在 `create_note` / `update_note` 里）。

**为什么值得上锁**（两条通道的失效形状不一样，这一点是本站的全部理由）：
  · **层级**：`parse_level` 认不出就返回 `None` ⇒ 后端把这次移动**拒掉**。危害有限
    （看得见的失败），但代价是"主人点了确定、什么也没发生"。
  · **状态**：后端**不拒未知状态**——`update_note` 是
    `active_model.status = Set(Some(v.clone()))` **原样写库**，只有它认得的那四个词
    才会**顺带**设 `is_public`。所以 agent 若发出一个后端不认的词（比如哪天归一化表里
    多了一档"hidden"），库里就留下一行 `status=hidden` 而 `is_public` 还是 `true` 的
    **矛盾行**——不报错、不告警，前台按 `is_public` 展示、后台按 `status` 分类，
    两边各说各话。**这条通道唯一的闸门就是 agent 侧那张白名单**，所以它值得上锁。

判据一律**从源码现取值**（Python 走 AST、Rust 走 `tests/_rust_src.py` 的函数体取值），
**绝不抄常量**——抄一份就是把"手抄表"搬进测试里。

**台账的方向**：agent 的**产出**必须是 Rust 认得的**子集**（发出去的每个词后端都认）。
反过来不要求——Rust 认得的比 agent 会写的多，那些词**逐条写在台账里**（现在只有
`published` 一个），漏写红、过期也红（同 `test_cross_language_tables.py` 的差集台账形状）。

**本套件不管什么**（写下来免得起假安心）：
  · `agent/workflow_defs/` 的提示词里写着 `one/two`——那是**话术**（告诉模型该填什么），
    不是判据，改了不影响行为；
  · `adminops._TOP_ALIASES`（置顶）——那是 `0/1` 整数，Rust 侧没有对应词表；
  · `STATUS_CN` 的中文名——前端那份界面标签在父仓前端，与本侧这张只用于"说给主人听"
    的表用途不同（同 `ROLE_CN` 与 `role_label` 的关系）。

用法：.venv/bin/python tests/test_write_param_words.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import adminops_tags as _tags  # noqa: E402
from tests import _parent_repo, _rust_src  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


_ADMINOPS = (ROOT / "agent" / "adminops_tags.py").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    """某个顶层函数的源码段（AST 限定范围，不看全文）。

    ⚠️ 判据必须只看这几个函数里写了什么：整个 `adminops.py` 里还有别的 `"one"`/`"two"`
    （`move_tag` 的参数校验、`workflow_defs/` 的提示词），全文搜会把它们当成"这里还有一个产出点"
    ——一条判据认错对象，比没有判据更坏（它会让真改错的那次看着是绿的）。
    """
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _dict_literal(name: str) -> dict:
    for node in ast.parse(_ADMINOPS).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return {}
    return {}


_LEVEL_SRC = _func_src(_ADMINOPS, "normalize_level")
_LEVEL_OF_SRC = _func_src(_ADMINOPS, "level_of")
_STATUS_ALIASES = _dict_literal("_STATUS_ALIASES")

_RUST_TAGS = _parent_repo.read(
    "src/routes/tags.rs",
    why="标签层级词表的**另一侧**：agent 把 `level` / `to_level` 发过去，Rust 用 "
        "`parse_level` 判；这个词表两侧不同源时，后端的表现是**直接拒掉这次移动**") or ""
_RUST_NOTES = _parent_repo.read(
    "src/routes/notes.rs",
    why="文章状态词表的**另一侧**：`update_note` 把未知状态**原样写库**、只有认得的那四个"
        "词才顺带设 `is_public` ⇒ 两侧不同源会留下一行 status 与 is_public 互相矛盾的记录，"
        "而且不报错") or ""

# ══ ① 层级词表：Rust 的"认"与"产出"两侧 ═══════════════════════════════════
print("① 层级词表：Rust `parse_level` 认的 == `from_code`/`to_code` 产出的")

_pl = _rust_src.fn_body(_RUST_TAGS, "parse_level")
_parse_set = set(re.findall(r'"([a-z]+)"\s*=>', _pl))
check("取到了 `parse_level` 的 match 臂字面量（取不到 ⇒ 下面每条都没有判据，直接红）",
      bool(_parse_set), f"{sorted(_parse_set)}")
check("  它有一个 `_ => None` 兜底（认不出的**拒**，不是随手当成一级）",
      re.search(r"_\s*=>\s*None", _pl) is not None, _pl.strip()[-40:])
_out_set: set[str] = set()
for _fn in ("from_code", "to_code"):
    _pairs = re.findall(r'\{\s*"(\w+)"\s*\}\s*else\s*\{\s*"(\w+)"\s*\}',
                        _rust_src.fn_body(_RUST_TAGS, _fn))
    check(f"  取到了 `{_fn}` 的两个产出字面量", bool(_pairs), f"{_pairs}")
    _out_set |= {w for pair in _pairs for w in pair}
check("⭐ 后端**认的**与后端**产出的**是同一组词（回执里的 fromLevel/toLevel 要能被再读回来）",
      bool(_parse_set) and _parse_set == _out_set,
      f"认={sorted(_parse_set)} 产出={sorted(_out_set)}")

# agent 侧的产出：两个产出点 + 真调一遍语料
_LVL_RETURNS = re.findall(r'return "(one|two)"', _LEVEL_SRC)
check("`normalize_level` 的产出点就是那两个字面量（不是从别处拼出来的）",
      set(_LVL_RETURNS) == _parse_set, f"{sorted(set(_LVL_RETURNS))}")

_LVL_INPUTS = ["one", "ONE", " One ", "1", "一", "一级", "two", "TWO", "2", "二", "二级",
               "二级 ", "三", "three", "3", "0", "", "   ", None, "one级", "两"]
_lvl_out = {_tags.normalize_level(v) for v in _LVL_INPUTS}
check("⭐ `normalize_level` 的**每一个产出**都在后端认的词表里（发出去的后端都认）",
      _lvl_out - {None} <= _parse_set and len(_lvl_out) > 1,
      f"产出={sorted(x for x in _lvl_out if x)} 后端={sorted(_parse_set)}")
check("⭐ 层级这一族两侧**完全同集**（没有台账：本侧不会产出后端不认的词，也不缺哪一档）",
      _lvl_out - {None} == _parse_set, f"本侧={sorted(x for x in _lvl_out if x)}")


# ══ ② 第二个产出点：`level_of`（TagInfo → 词）══════════════════════════════
print("\n② 第二个产出点 `level_of`（TagInfo → 词）：同一组词，同一份判据")

_taginfo = _tags.TagInfo
check("`level_of(None)` 进 None 出（缺信息不许兜成「一级」）", _tags.level_of(None) is None)
_lof = [_tags.level_of(_taginfo(id=1, name="x", level=1)), _tags.level_of(_taginfo(id=2, name="y",
                                                                        level=2))]
check("⭐ `level_of` 的产出也在后端词表里（一个是描述、一个是实参，写错哪一个后端都不认）",
      bool(set(_lof)) and set(_lof) <= _parse_set and len(set(_lof)) == 2, f"{_lof}")
check("  与 `normalize_level` 同源（同一组字面量，不是各写一份）",
      set(re.findall(r'"(one|two)"', _LEVEL_OF_SRC)) <= _parse_set,
      f"{sorted(set(re.findall(chr(34) + '(one|two)' + chr(34), _LEVEL_OF_SRC)))}")


# ══ ③ 状态词表：只有**单向**要求（后端认的比本侧会写的多）═════════════════
print("\n③ 文章状态：本侧产出的每一个词后端都认（后端认得多的那一族走台账）")

_recog: set[str] = set()
for _fn in ("create_note", "update_note"):
    _body = _rust_src.fn_body(_RUST_NOTES, _fn)
    _lits = set(re.findall(r'==\s*"([a-z]+)"', _body))
    _lits |= set(re.findall(r'unwrap_or\("([a-z]+)"', _body))     # 缺省值也是它认的词
    check(f"  从 `{_fn}` 取到了状态字面量", bool(_lits), f"{sorted(_lits)}")
    _recog |= _lits
check("⭐ 后端**认得**的状态词取全了（写库路径是 `Set(Some(v))`，只有认得的词才会顺带设 "
      "`is_public`）", _recog >= {"public", "private", "draft"}, f"{sorted(_recog)}")

_p_out = set(_STATUS_ALIASES.values())
check("`normalize_status` 的值域取自源码那张表（不是手抄一份）", bool(_p_out), f"{sorted(_p_out)}")
check("⭐ 本侧产出的**每一个**状态词后端都认（少一条就会写出一行 status 与 is_public "
      "互相矛盾的记录，而且没有任何东西会报错）",
      _p_out <= _recog, f"本侧={sorted(_p_out)} 后端={sorted(_recog)}")

# 台账**双向**：后端认得而本侧从不产出的词，逐条写下来；漏写红、过期也红。
LEDGER = {
    "published",   # 后端的历史写法（`create_note` 的缺省值就是它）：本侧一律归一到 "public"
}
_missing = sorted(_recog - _p_out - LEDGER)
_stale = sorted(LEDGER - (_recog - _p_out))
check("⭐ 后端认得而本侧不产出的词**逐条写在台账里**（漏一条红）",
      not _missing, "、".join(_missing))
check("⭐ 台账里也不许多写（本侧开始产出它了就要销账）", not _stale, "、".join(_stale))
check("  方向是安全的：本侧只会少写一个后端认得的旧词，不会多写一个后端不认的",
      not _missing and bool(LEDGER), f"台账 {len(LEDGER)} 条")

# 真调一遍：产出必须落在后端认得的集合里，且覆盖本侧值域的全部三档
_STATUS_INPUTS = ["public", "PUBLIC", " public ", "公开", "发布", "publish", "published", "1",
                  "private", "私密", "隐藏", "hidden", "0", "draft", "草稿", "drafted",
                  "半公开", "hidden2", "", "  ", None, "2"]
_st_out = {_tags.normalize_status(v) for v in _STATUS_INPUTS}
check("⭐ 语料里每一个非 None 的产出都在后端词表内",
      _st_out - {None} <= _recog, f"{sorted(x for x in _st_out if x)}")
check("  三档都产得出来（本侧值域 == 后端那三个词，不含 `published`）",
      _st_out - {None} == _p_out, f"{sorted(x for x in _st_out if x)}")


# ══ ④ 失败取向：两侧都是"认不出就拒"，且本侧**从不猜** ══════════════════════
print("\n④ 失败取向：认不出就拒（后端返回 None，本侧返回 None 让调用方拒）")

check("本侧层级：未知词一律 None（`三`/`three`/`3`/`0`/空串/None/`one级`）",
      all(_tags.normalize_level(v) is None
          for v in ("三", "three", "3", "0", "", "   ", None, "one级", "两")),
      f"{ {v: _tags.normalize_level(v) for v in ('三', '3', '', None)} }")
check("本侧状态：未知词一律 None（`半公开`/`hidden2`/`2`/空串/None）",
      all(_tags.normalize_status(v) is None
          for v in ("半公开", "hidden2", "2", "", "  ", None)),
      f"{ {v: _tags.normalize_status(v) for v in ('半公开', '2', None)} }")
check("  容错只做**大小写与首尾空白**（`ONE` / ` One ` 都认；不做别的模糊匹配）",
      _tags.normalize_level("ONE") == "one" and _tags.normalize_level(" One ") == "one"
      and _tags.normalize_status("PUBLIC") == "public")
check("★ 这一站补上了 `normalize_level` 的**行为覆盖**：此前全仓没有一处测试调过它"
      "（`normalize_status` 在 `test_admin_write.py` 里有，层级那半没有）",
      _tags.normalize_level("一级") == "one" and _tags.normalize_level("二级") == "two"
      and _tags.normalize_level("1") == "one" and _tags.normalize_level("2") == "two")


# ══ ⑤ agent 独有那几张表：只判自洽，并写下它们**没有** Rust 对应物 ═════════
print("\n⑤ agent 独有（Rust 侧没有对应物，故只判自洽）：")
print("   · 口语别名（`1`/`一`/`一级`、`公开`/`发布`/`隐藏`…）——入参归一化，后端不做这个")
print("   · `_TOP_ALIASES`（置顶）——`0/1` 整数，Rust 那边没有词表")
print("   · `STATUS_CN`——说给主人听的中文名，不是写回去的值")

check("  `_STATUS_ALIASES` 的**值**都在本侧值域里（认出一个不存在的档 = 拿它去问后端）",
      set(_tags._STATUS_ALIASES.values()) <= _p_out,
      f"越界：{sorted(set(_tags._STATUS_ALIASES.values()) - _p_out)}")
check("  `STATUS_CN` 的键 == 本侧值域（缺一档 = 显示时回落成「未知状态…」）",
      set(_tags.STATUS_CN) == _p_out, f"{sorted(_tags.STATUS_CN)}")
check("  `_TOP_ALIASES` 的值只有 0/1（置顶是布尔语义，不许出现第三个值）",
      set(_tags._TOP_ALIASES.values()) == {0, 1}, f"{sorted(set(_tags._TOP_ALIASES.values()))}")
check("  `normalize_top` 认不出也是 None（同一条纪律：宁可问一句，不猜）",
      _tags.normalize_top("差不多吧") is None and _tags.normalize_top(None) is None)

# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
