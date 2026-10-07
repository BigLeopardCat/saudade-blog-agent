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

# ⚠️ **顶层键拼对、子键拼错**是同一种失效的下一层：`require_confirm_payload` 的值是个
# dict，`{"skil": ...}` / `{"arg_from_input": [...]}` 一样是"看着有判据、其实一个断言都
# 没跑"。20261006 加第四键 `args_from_input` 时补上这道——它下一次被骗的概率不比顶层低
# （那张卡是本仓断言键最集中的一格）。
_CPSUB_GET = re.compile(r"""\b_cp\.get\(\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1""")
_CPSUB_IN = re.compile(r"""(["'])([A-Za-z_][A-Za-z0-9_]*)\1\s+in\s+_cp\b""")
# 动态取子键（`_cp.get(k)`）与顶层那条同一个理由：反射扫不到。
_CPSUB_DYN = re.compile(r"""\b_cp\.get\(\s*[^"'\s)]""")
_cp_src = _src["eval/run_golden.py"]
_impl = ({m.group(2) for m in _CPSUB_GET.finditer(_cp_src)}
         | {m.group(2) for m in _CPSUB_IN.finditer(_cp_src)})
check("`require_confirm_payload` 的子键都是字面量（动态取键 ⇒ 下面这条扫不到）",
      not _CPSUB_DYN.search(_cp_src))
check(f"源码实现的子键（{sorted(_impl)}）四个都在（少一个 = 那条断言被删了）",
      _impl == {"skill", "specs", "skill_any", "args_from_input"}, str(sorted(_impl)))
_cp_bad = [f"{c.get('id')}: {k}" for c in _cases for k in
           ((c.get("gold") or {}).get("require_confirm_payload") or {})
           if k not in _impl]
check("用例里的每个载荷子键都有实现（拼错 = 那段断言静默不执行）", not _cp_bad,
      "；".join(_cp_bad))
# 单独点名 `note`：它是抓到的第一例（`_note` 少一个下划线）。后来人若复制粘贴了那一行，
# 报错里直接给出正解。
check("没有裸 `note`（注释键是 `_note`；写成 `note` 等于这条注释不存在）",
      not any(u.endswith("gold.note") for u in _unknown))
_ids = [c.get("id", "?") for c in _cases]
# 数字是**刻意钉住的**：它逼着每加/删一条用例的人在这里露一次脸（顺带重新看一眼下面
# 几条覆盖面断言）。改它的同时要一起看 `_cases` 上游有没有别的计数（README 与
# eval 报告里的条数是另算的，别把它们与这里对齐成"同一个数"）。
check("用例数（168 条）", len(_ids) == 168, f"实际 {len(_ids)}")
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

# 夹具那套用例（`eval/fixtures/golden_smoke.jsonl`，20261006）走**同一套**拼写校验：
# 判据表只有一份，两边一个字都不差（这里若各写一份规则，夹具那套的拼写错误就没人发现）。
# 它**不进**上面那几条计数/前提/哨兵断言——那些钉的是维护者那套用例的形状（167 条、
# premise_absent 的落笔、手抄写工具清单），与夹具无关。
_FIX_FILE = ROOT / "eval/fixtures/golden_smoke.jsonl"
check("夹具用例文件在（README/CONTRIBUTING 都指着它，删了就是文档在说谎）",
      _FIX_FILE.is_file(), str(_FIX_FILE))
_fix_cases = [json.loads(ln) for ln in
              _FIX_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()] \
    if _FIX_FILE.is_file() else []
_fix_unknown = [f"{c.get('id')}: gold.{k}"
                for c in _fix_cases for k in (c.get("gold") or {})
                if k not in KNOWN | rg.GOLD_COMMENT_KEYS]
_fix_no_assert = [c.get("id") for c in _fix_cases
                  if not (set(c.get("gold") or {}) & rg.GOLD_ASSERT_KEYS)]
_fix_ids = [c.get("id") for c in _fix_cases]
check("夹具用例没有拼错的 gold 键", not _fix_unknown, "；".join(_fix_unknown))
check("夹具用例每条都至少带一个断言键（只有注释的用例等于没判）",
      not _fix_no_assert, "；".join(_fix_no_assert))
check("夹具用例 id 不重复", bool(_fix_ids) and len(_fix_ids) == len(set(_fix_ids)),
      str(_fix_ids))

print("\n⑤ 写族清单一律用哨兵，不许手抄（20261001）")
# 20261001 实测的现状：44 条含 `forbid_tool_calls` 的用例各自手抄一份后台写工具清单，
# **没有一条是完整的**——缺口完全跟着工具的上线时间走（额度三件与新待办工具 44/44 条
# 没禁、`send_user_notice` 43/44 没禁、`freeze_account` 36/44 没禁）。判据因此**每上
# 一个写工具就集体松一寸**，而没有任何东西会说话。哨兵 `@write_console` 把清单的唯一
# 事实源交回 `authz.TOOL_SCOPE`。这两条锁防的是"手抄清单长回来"：谁再往清单里写一个
# 具体写工具名，这里当场红，不必等到某个新工具上线后判据静默变松。
_wc = rg.write_console_tools()
check("authz.TOOL_SCOPE 里读得到后台写工具（哨兵的单一事实源）", len(_wc) >= 10,
      f"{len(_wc)} 个")
_probe = [{"id": "self-check", "gold": {"forbid_tool_calls": ["@write_console"]}}]
rg.expand_forbid_tokens(_probe)
check("哨兵展开成完整全集（不是把 token 当字面量比）",
      len(_probe[0]["gold"]["forbid_tool_calls"]) == len(_wc),
      f"展开 {len(_probe[0]['gold']['forbid_tool_calls'])} 个 vs 全集 {len(_wc)} 个")
_hand = []
for _c in _cases:
    for _r in rg.iter_rounds(_c):
        _bad = [t for t in (_r["gold"].get("forbid_tool_calls") or []) if t in _wc]
        if _bad:
            _hand.append(f"{_c.get('id')}: {_bad[:3]}")
check("含写工具的清单不许手抄（要写 `@write_console`）", not _hand,
      "；".join(_hand[:5]))

print("\n⑥ 事实前提要落笔，前提变了要响（20261001）")
# 病根不是"哨兵写错了"，是**没有人问过那个问题**：`note_traffic_denied_visitor` 断言
# "访客拿不到阅读量"，而八分钟后另一个提交把阅读量挂上了公开列表帧 ⇒ 模型如实按公开
# 数据排了个榜，判据把它判成幻觉。本节的锁只做两件事：**逼着写**（①）与**写完就能自动
# 验**（④⑤）——第二件是关键：前提是否还成立，从此在秒级套件里就能回答，不必等夜间跑完
# 一条一条读回复。
_rg_root = ROOT
_want = []          # 必须有 premise_absent 的用例：判据是"在断言做不到"
for _c in _cases:
    _golds = [r["gold"] for r in rg.iter_rounds(_c)]
    if any(g.get("require_denial") for g in _golds):
        _want.append(_c["id"])
_missing = [i for i in _want if not any(c["id"] == i and c.get("premise_absent")
                                        for c in _cases)]
check(f"断言『做不到』的用例（{len(_want)} 条）都落了笔（premise_absent）",
      not _missing, "；".join(_missing))
_bad_role, _bad_fact, _bad_name, _bad_why = [], [], [], []
from agent.tasks import step_tool_enum  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402
_registry = {t.name for t in _TOOL_REGISTRY}
for _c in _cases:
    _pa = _c.get("premise_absent")
    if not isinstance(_pa, dict):
        continue
    _cid = _c.get("id")
    if _pa.get("role") not in rg.PREMISE_ROLES:
        _bad_role.append(f"{_cid}: {_pa.get('role')!r}")
    if len(str(_pa.get("fact") or "").strip()) < 6:
        _bad_fact.append(_cid)
    _sup = _pa.get("suppliers")
    if not isinstance(_sup, list):
        _bad_name.append(f"{_cid}: suppliers 不是 list")
        continue
    for _t in _sup:
        if _t in rg.FORBID_TOKENS or _t in _registry:
            continue
        _bad_name.append(f"{_cid}: {_t!r}")
    if not _sup and len(str(_pa.get("why") or "").strip()) < 12:
        _bad_why.append(_cid)
    # 同一个角色在用例里有两处（context.role 与 premise_absent.role）——两处都要写时
    # 必须同值，否则哨兵问的是另一个身份的可达面（判据在别人手里的老毛病）。
    _ctx_role = (_c.get("context") or {}).get("role")
    _decl = rg.premise_role(_pa)
    if _ctx_role and _decl and _ctx_role != _decl:
        _bad_role.append(f"{_cid}: context.role={_ctx_role} vs premise={_decl}")
check("role 合法（visitor/user/admin/superadmin）且与 context.role 一致",
      not _bad_role, "；".join(_bad_role))
check("fact 是一句真话（≥6 字）", not _bad_fact, "；".join(_bad_fact))
check("suppliers 里每个名字都是真工具名或哨兵（拼错 = 哨兵永不响）",
      not _bad_name, "；".join(_bad_name))
check("suppliers 为空的必须写 why（哨兵判不了，要说明为什么没有清单）",
      not _bad_why, "；".join(_bad_why))

_kept, _skipped, _rows = rg.check_premises(json.loads(json.dumps(_cases)))
check("★ 现有语料的事实前提一条都没变（变了会在这里红，不必等夜间）",
      not _skipped, "；".join(_skipped))
# 判据的判据：这两条探针证明哨兵**真的会响**。没有它们，"全部 ok" 与"哨兵坏了恒 ok"
# 长得一模一样——同族教训见 tests/test_confirm.py 的变异锁。
_probe_hist = [{"id": "note_traffic_denied_visitor",
                "premise_absent": {"fact": "文章阅读/点赞/收藏的计数与排行", "role": "visitor",
                                   "suppliers": ["get_note_stats", "list_notes",
                                                 "get_article_detail", "search_notes",
                                                 "get_top_notes"]}}]
_, _s2, _r2 = rg.check_premises(_probe_hist)
check("历史回归：`note_traffic` 那次的声明喂进来，哨兵当场响（list_notes 是公开工具）",
      _s2 == ["note_traffic_denied_visitor"] and "list_notes" in _r2[0]["hit"],
      f"{_s2} / {_r2[0]['hit'][:4]}")
_probe_reach = [{"id": "self-check",
                 "premise_absent": {"fact": "探针", "role": "admin",
                                    "suppliers": ["get_server_status"]}}]
_, _s3, _r3 = rg.check_premises(_probe_reach)
check("管理员能取的服务器状态工具，被声明成『管理员拿不到』时也当场响",
      _s3 == ["self-check"], f"{_s3} / {_r3[0]['hit']}")
check("哨兵对管理员-角色判可达、对访客判不可达（同一件工具，两个答案）",
      "get_user_stats" in step_tool_enum("admin") and "get_user_stats" not in step_tool_enum(None))

print("\n⑦ 『不该弹卡』的理由要落笔，理由变了要响（20261002）")
# 上一节治的是"事实前提住在别人手里"；这一节是同一副药治**另一件前提**：15 条用例的
# `forbid_frame_prefix` 里写着 `__CONFIRM__:`——它们在断言"这一轮**没有**确认卡"，而
# "为什么不该有卡"同样住在别人手里（提问判据 / 角色权限表 / 目标是否存在的现场 / 设计）。
# 20261001 那次红色 2.5 小时的实证：新设计把审核族整体改成恒弹卡，老判据还写着"不许弹"，
# 红色的那段时间里判的是**判据自己**。锁法照 ⑥：逼着写（①）＋ 写完能自动验（②③④）。
_no_card = [c["id"] for c in _cases
            if any("__CONFIRM__:" in (g.get("forbid_frame_prefix") or [])
                   for g in (r["gold"] for r in rg.iter_rounds(c)))]
_declared_np = [c["id"] for c in _cases if c.get("premise_no_popup")]
_missing_np = [i for i in _no_card if i not in _declared_np]
_extra_np = [i for i in _declared_np if i not in _no_card]
check(f"断言『没有确认卡』的用例（{len(_no_card)} 条）都落了笔（premise_no_popup）",
      not _missing_np, "；".join(_missing_np))
# 反面：没有禁卡断言的用例带上这个键 = 一个没有读者的声明（"看着有、其实没有"的老毛病；
# 真出现这种情况要么是判据被删了、要么是键写错了地方——两种都该露脸）。
check("没有多余的声明（没有禁卡断言的用例不该带 premise_no_popup）",
      not _extra_np, "；".join(_extra_np))

_bad_kind, _bad_ntools, _bad_nwhy = [], [], []
for _c in _cases:
    _pn = _c.get("premise_no_popup")
    if not isinstance(_pn, dict):
        continue
    _cid = _c.get("id")
    _kind = str(_pn.get("kind") or "").strip()
    if _kind not in rg.NO_POPUP_KINDS:
        _bad_kind.append(f"{_cid}: {_kind!r}")
    _tl = _pn.get("tools")
    _tl = [] if _tl is None else _tl
    if not isinstance(_tl, list):
        _bad_ntools.append(f"{_cid}: tools 不是 list")
        _tl = []
    else:
        _bad_ntools += [f"{_cid}: {t!r}" for t in _tl
                        if t not in rg.FORBID_TOKENS and t not in _registry]
    # 两类"点名工具"的声明**必须**点名（哨兵核的就是这几件到不到得了）；另外两类**不许**
    # 点名——它们的判据（提问 / `require_absence`）与工具无关，写上去是个没人读的字段。
    if _kind in ("role_denied", "capability_boundary"):
        if not _tl:
            _bad_ntools.append(f"{_cid}: {_kind} 没点名工具")
    elif _kind in ("question", "target_absent") and _tl:
        _bad_ntools.append(f"{_cid}: {_kind} 不该点名工具（判据与工具无关）")
    # why：capability_boundary 的"设计那一半"机器判不了 ⇒ 强制写、且要够长（哨兵同判据）；
    # 另外三类也要求写一句（本节的 house rule，比哨兵严一档——哨兵只读 cap 的 why）。
    _why = str(_pn.get("why") or "").strip()
    if not _why:
        _bad_nwhy.append(_cid)
    elif _kind == "capability_boundary" and len(_why) < rg._NO_POPUP_WHY_MIN:
        _bad_nwhy.append(f"{_cid}: why 只 {len(_why)} 字")
check("kind 是四类之一", not _bad_kind, "；".join(_bad_kind))
check("tools 是真工具名或哨兵，且『该点名的点名、不该点名的不点名』",
      not _bad_ntools, "；".join(_bad_ntools))
check("每条都写了 why；capability_boundary 的 why ≥ 12 字（那半机器判不了）",
      not _bad_nwhy, "；".join(_bad_nwhy))

_kept_np, _skipped_np, _rows_np = rg.check_no_popup_premises(json.loads(json.dumps(_cases)))
check("★ 现有语料的『不该弹卡』理由一条都没变（变了会在这里红，不必等夜间）",
      not _skipped_np, "；".join(_skipped_np))
# 四类各自至少被用到一条：否则"四类设计"里有一格是死代码，而它对应的那类前提**没人守**。
_kinds_used = {r["kind"] for r in _rows_np}
check("四类各自至少一条（否则那一类前提没人守）",
      _kinds_used == set(rg.NO_POPUP_KINDS), f"缺 {sorted(set(rg.NO_POPUP_KINDS) - _kinds_used)}")
# `capability_boundary` 只核了"卡不是权限压的"那一半，另一半是设计——报告里标 partial，
# 这里锁"标了 partial 的恰好是这一类"，防它被当成"全核过了"。
_partial = {r["kind"] for r in _rows_np if r.get("partial")}
check("只有 capability_boundary 标 partial（那半是人写的前提，不假装核过）",
      _partial == {"capability_boundary"}, str(sorted(_partial)))

# 判据的判据：下面五条探针证明哨兵**真的会响**。没有它们，"全部 ok"与"哨兵坏了恒 ok"
# 长得一模一样——同族教训见 tests/test_confirm.py 的变异锁。探针都**从真实用例变异**而来
# （自己造一条空壳用例只能证明哨兵会算数，证明不了这条用例的理由是承重的）。
_by_id = {c.get("id"): c for c in _cases}


def _mut(cid: str) -> dict:
    return json.loads(json.dumps(_by_id[cid]))


_p1 = _mut("admin_write_question_no_exec")
_p1["user_input"] = "把文章 12 设为私密"
_, _s4, _r4 = rg.check_no_popup_premises([_p1])
check("提问那半：主人这句从疑问改成祈使 ⇒ 当场响（提问轮才不弹卡）",
      _s4 == [_p1["id"]] and any("is_question_like" in h for h in _r4[0]["hit"]),
      f"{_s4} / {_r4[0]['hit']}")

_p2 = _mut("admin_write_denied_user")
_p2["context"]["role"] = "admin"
_, _s5, _r5 = rg.check_no_popup_premises([_p2])
check("角色那半：同一件写工具，发起人从普通用户换成管理员（够得着了）⇒ 当场响",
      _s5 == [_p2["id"]] and "set_article_status" in _r5[0]["hit"],
      f"{_s5} / {_r5[0]['hit']}")

_p3 = _mut("admin_tag_move_unresolved_target_honest")
_p3["gold"].pop("require_absence")
_, _s6, _r6 = rg.check_no_popup_premises([_p3])
check("查无此物那半：抽掉判据里的 require_absence ⇒ 当场响",
      _s6 == [_p3["id"]] and any("require_absence" in h for h in _r6[0]["hit"]),
      f"{_s6} / {_r6[0]['hit']}")

_p4 = _mut("nav_direct_no_confirm_promise")
_p4["premise_no_popup"]["tools"] = ["navigate_to", "list_admin_notes"]
_, _s7, _r7 = rg.check_no_popup_premises([_p4])
check("能力边界那半：混进一件这个角色够不着的工具 ⇒ 当场响"
      "（那就不是『设计使然』，是权限不给）",
      _s7 == [_p4["id"]] and _r7[0]["hit"] == ["list_admin_notes"], f"{_s7} / {_r7[0]['hit']}")

_p5 = _mut("nav_direct_no_confirm_promise")
_p5["premise_no_popup"]["tools"] = ["navigate_to"]
_p5["premise_no_popup"]["why"] = "太短"
_, _s8, _r8 = rg.check_no_popup_premises([_p5])
check("why 太短也响（设计那一半没人能自动判，但至少要写下来）",
      _s8 == [_p5["id"]] and any("why" in h for h in _r8[0]["hit"]), f"{_s8} / {_r8[0]['hit']}")

print("\n⑧ 判据不得被『兜底文本』满足（20261002）")
# 病根与 §⑥⑦ 同族（"看着有、其实没有"），但住在**正文**上：gate 的兜底/纠正文本是完整
# 的中文句子（「喵呜……主人，我得收回一句：这一轮系统**没有执行任何跳转**……」），它照样
# 能命中 `text_contains` / `require_denial` / `text_not_contains`——一条用例若整套判据都能被
# 某族兜底文本满足，那它测的就不是"模型答对了"，而是"这一轮出过事"。判据 `forbid_fallback`
# 早就有（§⑧ 的键表里有它），问题是**只在 76 条里 80 处手写**过、没有东西逼着新用例写：
# 实测旧语料 25 条中招（5 条在回归组），全是被这一步扫出来的。
#
# 探针 = 离线重放（不跑模型、不联网）：把 `agent.graph` 的每一条 `_FALLBACK_*` 当正文喂进
# `check_gold`，看这个 `gold` 会不会**整套**放行。锁法照 §⑥⑦：逼着写（①）+ 探针真会响（③）。
from agent import graph as _G  # noqa: E402

# ⚠️ 只收 `agent.graph` 的常量，**不含** `server._RECOVERY_SENTENCE` / `PRODUCER_ERROR_TEXT`：
# 那两条只在 `event_stream`（SSE 端点）与 `_run_agent_sync` 里补发，而 golden 直连
# `server._run_agent_stream_to_queue`（producer）拿帧流 ⇒ 结构上到不了（见 `run_one`）。
# 把它们列进来会逼着 25 条用例为一个到不了的输入挂键。
_fb_texts = sorted({getattr(_G, n) for n in dir(_G)
                    if n.startswith("_FALLBACK") and isinstance(getattr(_G, n), str)})


def _satisfiable_by_fallback(gold: dict) -> str:
    """这个 gold 能否被某条兜底文本**整套**满足？返回命中的那条文本（空串 = 不会）。"""
    for t in _fb_texts:
        res = {"text": t, "commands": [], "tool_calls": [], "exec_rows": [],
               "exec_tools": [], "frames": [], "confirm_tokens": [], "confirm_payloads": [],
               "task_frames": [], "ledger_frames": [], "resets": 0, "resets_reasons": [],
               "reset_scopes": [], "fallback_reasons": [], "error": None}
        if not rg.check_gold(gold, res, docs=None):
            return t
    return ""


check(f"探针的输入非空（实得 {len(_fb_texts)} 条 _FALLBACK_*）", len(_fb_texts) >= 20,
      str(len(_fb_texts)))
_loose = []
for _c in _cases:
    for _i, _r in enumerate(rg.iter_rounds(_c), 1):
        _g = _r["gold"]
        if _g.get("forbid_fallback"):
            continue
        if _satisfiable_by_fallback(_g):
            _loose.append(f"{_c['id']}" + (f"[r{_i}]" if _c.get("rounds") else ""))
check("没有『能被兜底文本整套满足』却又不挂 forbid_fallback 的轮次"
      "（挂了它，真走兜底那轮会有 __RESET__:text 让判据当场红）",
      not _loose, "；".join(_loose))

# 判据的判据：把一条**真实用例**的 `forbid_fallback` 摘掉，探针必须当场报出来。
# 没有这一条，"全部 ok" 与 "探针坏了恒不报" 长得一模一样（同 §⑥⑦ 的变异锁纪律）。
_p = _mut("casual_hello")
_p["gold"].pop("forbid_fallback", None)
check("反向对照：从真实用例上摘掉 forbid_fallback ⇒ 探针当场报出来",
      bool(_satisfiable_by_fallback(_p["gold"])))

print("\n⑨ 每个断言键都要有用例在用（防「判据词汇表里挂着一条没人考的键」）")
# 与 §② 是**两个方向**：§② 管"表里的键必须真被源码读到"（读到 = 判据真的在跑），
# 本节管"读到的键必须真有用例拿它判过"（有用例 = 判据真的会红）。中间那一格——
# 源码里写着 `gold.get("X")`、表里也有 X、而**167 条用例一条都没写过 X**——两边都不响，
# 而那一格的含义是"这个判据永远不会被判红"：它的**红**从没被验证过，绿的用例也不构成
# 证据（绿灯只能说"没触发"，不能说"判得对"）。本节的锁法照 §⑥⑦：逼着用（①）＋
# 探针会响（②）。守的是**新增**键：以后谁往判据里加一个键却没配用例，这里当场红。
_used: set[str] = set()
for _c in _cases:
    for _r in rg.iter_rounds(_c):
        _used |= set(_r["gold"])
_dead = sorted(rg.GOLD_ASSERT_KEYS - _used)
check(f"{len(rg.GOLD_ASSERT_KEYS)} 个断言键每个都至少被一轮用过", not _dead,
      "；".join(f"{k} 没有用例" for k in _dead))


def _unused_keys(cases: list[dict]) -> list[str]:
    """给定语料里没有任何一轮用到的断言键（本节判据的实现，单独提出来好做反向对照）。"""
    seen: set[str] = set()
    for c in cases:
        for r in rg.iter_rounds(c):
            seen |= set(r["gold"])
    return sorted(rg.GOLD_ASSERT_KEYS - seen)


# 反向对照：从真实语料里**抽掉所有用某个键的用例**（= 那个键从此没有任何用例在考），
# 探针必须当场点名它。不这么做的话，"全部都用过"与"函数恒返回空"长得一模一样
# （同 §⑥⑦ 的变异锁纪律）。断言只钉"这个键被报出来"，不钉整张孤儿名单——名单会随
# 加用例而变（`require_ledger_*` 三个键现在同住一条用例，抽掉它三个一起变孤儿），
# 那属于用例侧的正常变化，不该让这条锁红。
_PROBE_KEY = "require_ledger_rows"
_probe_corpus = [c for c in _by_id.values()
                 if _PROBE_KEY not in json.dumps(c, ensure_ascii=False)]
check(f"反向对照：抽掉最后一个用 `{_PROBE_KEY}` 的用例 ⇒ 探针当场点名该键",
      _PROBE_KEY in _unused_keys(_probe_corpus),
      str(_unused_keys(_probe_corpus)[:5]))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
