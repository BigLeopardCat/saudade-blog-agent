# -*- coding: utf-8 -*-
"""手抄表的机器锁 · 第六站：**河灯留言的一条 DTO，两侧各写一份它的键**（20261013，收口批 3g）。

**契约**：留言板两条通道（公开池 `list_guestbook`、我自己的 `list_my_board`）拿回来的
都是后端 `TalkDto`（父仓 `src/routes/talks.rs`）那一个 JSON。agent 侧有两处**按名字**读它：

  · `agent/entities.py` 的两个摘要器（`_entry_digest` / `_my_board_digest`）——跨轮执行
    记忆就靠它们；
  · `tools/base.py::_shape` 出口的**改名表**（`_FRAME_ID_KEYS`：`talkKey` → `talkId`）
    与 `_board_text_keys`（`author` → `留名`）——帧里给模型看的那一列。

**为什么值得上锁**（改键名**不报错**，而且症状是"少了半句"）：

  · Rust 把 `#[serde(rename = "talkTitle")]` 改掉（或换字段名）而 agent 没改 ⇒
    `r.get("cat")` / `r.get("talkTitle")` 恒 None ⇒ 那一行的〔分类〕**整段消失**；
    再狠一点（`content` 改名）⇒ `if not body: continue` 把整行**跳过** ⇒ 模型看到
    "最近 5 条"却只有 2 条——**不报错、不告警**，只是记忆里少了几条。
  · 审核态取值域（`approved`）多一档或改一档 ⇒ 把待审说成通过、或把通过的写成
    "状态未知"，而这正是 `list_my_board` 这条通道存在的**全部理由**。

**两侧的关系不止"相等"**，有三条要分开说：
  ① 上游键名：Rust `TalkDto` 发的键 == agent 按名字读的那些（**去掉改名表的映射之后**）；
  ② 审核态取值域：`_BOARD_STATE_WORD` 的键 == Rust `review_notice_text_for` 的 match 臂
     （两侧都不认就拒，兜底**刻意不同**，见下）；
  ③ 位置形态：`/guestbook?lid=<talkId>` 的取值来自 `talkKey`，而 agent 只在
     `isinstance(tid, int)` 时才印它——Rust 那个字段是 `i32`（**非** Option）⇒ 恒成立。

判据一律**从源码现取值**（Rust 走 `tests/_rust_src.fn_body` + 结构体切片，Python 走 AST），
**绝不抄常量**；台账双向（漏一条红、过期也红）。

**本套件不管什么**（写下来免得起假安心）：
  · **中文状态词本身**：agent 的「通过/待审/未通过」是**卡面**用语（`_my_board_digest`
    印给模型看），Rust 那三句是**站内通知标题**（「留言已通过审核」）。两款说的是同一组
    取值，但**语域不同**（同 `ROLE_CN` 与 `role_label` 的关系），刻意不逐字比；
  · `rejectReason` / `nickname` / `avatar` / `createTime` 这些键：Rust 发、agent 的
    摘要器**不读**（读它们的地方在别处，各自有各自的锁）；本套件只判"读到的都在"；
  · 帧里的**值**（谁写的、写了什么）：那是 DTO 内容，不是键。

用法：.venv/bin/python tests/test_board_dto_keys.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import adminops_quota as _quota  # noqa: E402
from agent import entities as E  # noqa: E402
from tests import _parent_repo, _rust_src  # noqa: E402
from tools import base as B  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


_RUST_TALKS = _parent_repo.read(
    "src/routes/talks.rs",
    why="留言板那一个 DTO 的**定义方**：键名、`approved` 的取值域都在这里；agent 按名字读"
        "它，改一侧不改另一侧的代价是摘要**静默少半句**（甚至整行被跳过）") or ""

# ══ 取值：Rust 的 DTO 字段表（serde rename 优先，否则字段名）══════════════════
# 只切 `pub struct TalkDto { … }` 这一段：同一个文件里还有别的 DTO（`CreateTalkRequest`
# 等），全文搜会把它们的键当成这条通道发的键——判据认错对象比没有判据更坏。
_DTO_RE = re.compile(r"^[ \t]*pub struct TalkDto \{(?P<body>.*?)^\}", re.S | re.M)
_FIELD_RE = re.compile(r"^[ \t]*pub (?P<name>\w+):\s*(?P<ty>[^,]+),\s*$", re.M)
_RENAME_RE = re.compile(r'#\[serde\(rename = "(?P<key>[^"]+)"\)\]')


def _dto_keys(src: str) -> dict[str, str]:
    """`{发出的键名: 字段类型}`。rename 属性**贴在字段上一行**（本仓的写法）。"""
    m = _DTO_RE.search(src)
    if not m:
        return {}
    out: dict[str, str] = {}
    pending = ""
    for line in m.group("body").splitlines():
        r = _RENAME_RE.search(line)
        if r:
            pending = r.group("key")
            continue
        f = re.match(r"^[ \t]*pub (\w+):\s*([^,]+),\s*$", line)
        if f:
            out[pending or f.group(1)] = f.group(2).strip()
            pending = ""
    return out


_RUST_KEYS = _dto_keys(_RUST_TALKS)

# ══ 取值：agent 读的键（AST 取 `X.get("字面量")`，只在那两个摘要器里）══════════
_ENTS = (ROOT / "agent/entities.py").read_text(encoding="utf-8")


def _get_keys(func: str) -> dict[str, str]:
    """`func` 里所有 `X.get("字面量")` 的键 → 所在函数名。

    **只取函数段**（同 `test_write_param_words.py` 的理由）：整个 `entities.py` 里到处
    都是 `.get("…")`，全文搜会把别的通道的键算作"留言板读的键"。
    """
    node = next((n for n in ast.parse(_ENTS).body
                 if isinstance(n, ast.FunctionDef) and n.name == func), None)
    if node is None:
        return {}
    out: dict[str, str] = {}
    for x in ast.walk(node):
        if (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                and x.func.attr == "get" and x.args
                and isinstance(x.args[0], ast.Constant)
                and isinstance(x.args[0].value, str)):
            out[x.args[0].value] = func
    return out


_READ: dict[str, str] = {}
for _f in ("_entry_digest", "_my_board_digest"):
    _READ |= _get_keys(_f)

# 帧里给模型看的名字 ← 上游名字 的**改向表**（本侧出口那一步）。
# ⚠️ 方向别写反：表里是 `上游键 -> 帧里的名字`，所以判"读到的键在后端发的键里"要**逆过来**。
_FRAME_ID_KEYS = dict(B._FRAME_ID_KEYS)
_UPSTREAM = {v: k for k, v in _FRAME_ID_KEYS.items()}      # talkId -> talkKey

# ══ ① 键名：agent 读的（映射回上游名）⊆ Rust 发的 ═══════════════════════════
print("① 键名：agent 摘要器读的键（去掉帧改名之后）都在 Rust `TalkDto` 发的键里")

check("从 `TalkDto` 取到了字段表（取不到 ⇒ 下面每条都没有判据，直接红）",
      len(_RUST_KEYS) >= 8, f"{len(_RUST_KEYS)} 个键")
check("从两个摘要器取到了 `.get(\"…\")` 的键", len(_READ) >= 4, f"{sorted(_READ)}")

_resolved = {_UPSTREAM.get(k, k): k for k in _READ}
LEDGER = {
    # 本侧读了、而后端**从不发**的键。逐条写清楚它是什么、为什么还在：
    "talkContent": "历史兜底（`content` 的旧名？）。父仓 `src/` 与前端的全仓搜索都"
                   "查不到这个键——**它是死的**，读到的恒 None。留着无害（`or` 短路），"
                   "但别让它长成第二处：新读一个键必须走这张台账。",
}
_missing = sorted(set(_resolved) - set(_RUST_KEYS) - set(LEDGER))
_stale = sorted(set(LEDGER) - (set(_resolved) - set(_RUST_KEYS)))
check("⭐ agent 读的每个键（映射回上游名后）都是 Rust **真的会发**的键（改一侧不改"
      "另一侧 = 摘要静默少半句，甚至整行被跳过）",
      not _missing, "、".join(f"{k}（读自 {_resolved[k]}）" for k in _missing))
check("⭐ 台账双向：本侧读了后端不发的那几个键**逐条写下来**，且不许过期",
      not _stale and bool(LEDGER),
      f"过期：{_stale}；台账 {len(LEDGER)} 条")
check("  取到的键里两样都在（`talkId` 那条：键名只有映射之后才对得上）",
      "talkId" in _READ and "approved" in _READ and "cat" in _READ,
      f"{sorted(_READ)}")
check("  摘要器读的是**帧里的名字**（`talkId`），不是上游的 `talkKey`——同一个数字"
      "只以一种身份露面（`_shape` 出口那一步改名）",
      "talkKey" not in _READ and _FRAME_ID_KEYS.get("talkKey") == "talkId"
      and _UPSTREAM.get("talkId") == "talkKey",
      f"{ {k: v for k, v in _FRAME_ID_KEYS.items()} }")


# ══ ② 审核态取值域：`_BOARD_STATE_WORD` == `review_notice_text_for` 的 match 臂 ══
print("\n② 审核态：agent 那张状态词表的键 == Rust 认的那几档（两侧都不认就拒）")

_WORD = next((ast.literal_eval(n.value) for n in ast.parse(_ENTS).body
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "_BOARD_STATE_WORD"
                      for t in n.targets)), {})
_nrt = _rust_src.fn_body(_RUST_TALKS, "review_notice_text_for")
_arms = {int(x) for x in re.findall(r"^\s*(\d+)\s*=>", _nrt, re.M)}
check("取到了 `_BOARD_STATE_WORD`（agent 侧）与 `review_notice_text_for` 的 match 臂"
      "（后端侧）", bool(_WORD) and bool(_arms), f"agent={_WORD} Rust={sorted(_arms)}")
check("⭐ 两侧取值域**相等**（后端加一档没同步 ⇒ 那条留言被写成「状态未知」；本侧多一档 "
      "⇒ 印出一个后端永远不会给的状态）",
      set(_WORD) == _arms, f"agent={sorted(_WORD)} Rust={sorted(_arms)}")
check("  三档都在（1=通过 / 0=待审 / 2=未通过——这条通道存在的全部理由就是它）",
      _arms == {0, 1, 2}, f"{sorted(_arms)}")
check("  Rust 有 `_ => None` 兜底（不认识的 approved **不发通知**，不猜它是哪一档）",
      re.search(r"_\s*=>\s*None", _nrt) is not None)
check("  agent 兜底**刻意不同**：读不出写「状态未知」而不是「待审」——「这一格没读到」"
      "不许说成一条审核结论",
      E._BOARD_STATE_WORD.get(9, "状态未知") == "状态未知"
      and E._BOARD_STATE_WORD.get(None, "状态未知") == "状态未知")
check("  后端的表只有**一处**：`review_notice_text` 是薄包装（自己不带 match 臂，"
      "否则改了一处另一处还在旧口径）",
      not re.search(r"^\s*\d+\s*=>", _rust_src.fn_body(_RUST_TALKS, "review_notice_text"), re.M)
      and "review_notice_text_for" in _rust_src.fn_body(_RUST_TALKS, "review_notice_text"))
# 同仓还有一张**键空间相同**的表（配额申请的 0/1/2），两张抄串 = 把"已驳回"印成"未通过"。
check("  `adminops._QUOTA_STATUS_CN`（0待处理/1已批准/2已驳回）与它**不共用词**"
      "（同键不同义，抄串了就是把一条申请说成一条留言）",
      not (set(_quota._QUOTA_STATUS_CN.values()) & set(_WORD.values())),
      f"{sorted(set(_quota._QUOTA_STATUS_CN.values()))}")


# ══ ③ 位置形态：`talkId` 恒是整数（`/guestbook?lid=<talkId>` 的取值来源）══════
print("\n③ 位置形态：`talkKey` 是 `i32`（非 Option）⇒ agent 的 `isinstance(…, int)` 恒真")

check("⭐ Rust 那个字段是**非 Option** 的整数类型（改叫 `Option<i32>` 的那天，本侧的 "
      "`tid` 就会偶尔是 None ⇒ 位置形态取不到值）",
      _RUST_KEYS.get("talkKey") in ("i32", "i64"), f"{_RUST_KEYS.get('talkKey')!r}")
_mbd = ast.get_source_segment(_ENTS, next(
    n for n in ast.parse(_ENTS).body
    if isinstance(n, ast.FunctionDef) and n.name == "_my_board_digest")) or ""
check("  agent 侧只在 `isinstance(tid, int)` 时印 `talkId:`（读不出就不印，不编一个）",
      "isinstance(tid, int)" in _mbd)

# 真调一遍：帧形态的一行进摘要器，`talkId` 与状态都露出来（正控——不然上面全是空判据）
_ROW = {"talkId": 101, "cat": "寄", "content": "今天河边风很大", "approved": 0}
_bd = E._my_board_digest([_ROW])
_ed = E._entry_digest([_ROW])
check("  正控：帧形态的一行喂进 `_my_board_digest`，`talkId` 与状态都在",
      "talkId:101" in _bd and "待审" in _bd and "〔寄〕" in _bd and "河边风很大" in _bd, _bd)
check("  正控：同一行进 `_entry_digest`（公开池那条通道读同一组键）",
      "〔寄〕" in _ed and "河边风很大" in _ed, _ed)
check("  负控形状：键名换成上游名（`talkKey`）⇒ 那个位置形态**读不出来**（这正是"
      "上锁要防的那件事）",
      "talkId:" not in E._my_board_digest([{"talkKey": 101, "content": "x", "approved": 1}]))

# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
