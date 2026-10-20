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

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


EVAL = ROOT / "eval"


sys.path.insert(0, str(EVAL))


sys.path.insert(0, str(ROOT))


import run_golden as rg  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


_LITERAL = (
    re.compile(r"""\b(?:gold|g)\.get\(\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1"""),
    re.compile(r"""\b(?:gold|g)\[\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]"""),
)


def scan_keys(text: str) -> set[str]:
    out: set[str] = set()
    for pat in _LITERAL:
        out.update(m.group(2) for m in pat.finditer(text))
    return out


CASES_FILE = ROOT / "eval/golden/basic.jsonl"


_lines = [ln for ln in CASES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]


_cases = [json.loads(ln) for ln in _lines]


from agent.tasks import step_tool_enum  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402

_by_id = {c.get("id"): c for c in _cases}


def _mut(cid: str) -> dict:
    return json.loads(json.dumps(_by_id[cid]))


from agent import graph as _G  # noqa: E402

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


def _unused_keys(cases: list[dict]) -> list[str]:
    """给定语料里没有任何一轮用到的断言键（本节判据的实现，单独提出来好做反向对照）。"""
    seen: set[str] = set()
    for c in cases:
        for r in rg.iter_rounds(c):
            seen |= set(r["gold"])
    return sorted(rg.GOLD_ASSERT_KEYS - seen)


def test_golden_keys():
    CONSUMERS = ("eval/run_golden.py", "eval/golden_case_runner.py")
    _DYNAMIC = re.compile(r"""\b(?:gold|g)\.get\(\s*[^"'\s)]|\b(?:gold|g)\[\s*[^"'\]]""")
    KNOWN = rg.GOLD_ASSERT_KEYS | rg.GOLD_REQUEST_KEYS | rg.GOLD_ROUND_KEYS
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
    _CPSUB_GET = re.compile(r"""\b_cp\.get\(\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1""")
    _CPSUB_IN = re.compile(r"""(["'])([A-Za-z_][A-Za-z0-9_]*)\1\s+in\s+_cp\b""")
    _CPSUB_DYN = re.compile(r"""\b_cp\.get\(\s*[^"'\s)]""")
    _cp_src = _src["eval/run_golden.py"]
    _impl = ({m.group(2) for m in _CPSUB_GET.finditer(_cp_src)}
             | {m.group(2) for m in _CPSUB_IN.finditer(_cp_src)})
    check("`require_confirm_payload` 的子键都是字面量（动态取键 ⇒ 下面这条扫不到）",
          not _CPSUB_DYN.search(_cp_src))
    check(f"源码实现的子键（{sorted(_impl)}）六个都在（少一个 = 那条断言被删了）",
          _impl == {"workflow", "specs", "workflow_any", "args_from_input", "specs_include",
                    "units"},
          str(sorted(_impl)))
    _cp_bad = [f"{c.get('id')}: {k}" for c in _cases for k in
               ((c.get("gold") or {}).get("require_confirm_payload") or {})
               if k not in _impl]
    check("用例里的每个载荷子键都有实现（拼错 = 那段断言静默不执行）", not _cp_bad,
          "；".join(_cp_bad))
    check("没有裸 `note`（注释键是 `_note`；写成 `note` 等于这条注释不存在）",
          not any(u.endswith("gold.note") for u in _unknown))
    _ids = [c.get("id", "?") for c in _cases]
    check("用例数（192 条）", len(_ids) == 192, f"实际 {len(_ids)}")
    check("用例 id 无重复", len(_ids) == len(set(_ids)),
          f"重复：{sorted({i for i in _ids if _ids.count(i) > 1})}")
    _no_assert = [c.get("id") for c in _cases
                  if not (set().union(*(set(r["gold"]) for r in rg.iter_rounds(c)))
                          & rg.GOLD_ASSERT_KEYS)]
    check("每条用例都至少带一个断言键（只有注释的用例等于没判）", not _no_assert,
          "；".join(_no_assert))
    _ghost_gold = [c.get("id") for c in _cases if c.get("rounds") and c.get("gold")]
    check("多轮用例不写顶层 gold（有 rounds 时它一个读者都没有）", not _ghost_gold,
          "；".join(_ghost_gold))
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
          "get_user_stats" in step_tool_enum("admin")
              and "get_user_stats" not in step_tool_enum(None))
    print("\n⑦ 『不该弹卡』的理由要落笔，理由变了要响（20261002）")
    _no_card = [c["id"] for c in _cases
                if any("__CONFIRM__:" in (g.get("forbid_frame_prefix") or [])
                       for g in (r["gold"] for r in rg.iter_rounds(c)))]
    _declared_np = [c["id"] for c in _cases if c.get("premise_no_popup")]
    _missing_np = [i for i in _no_card if i not in _declared_np]
    _extra_np = [i for i in _declared_np if i not in _no_card]
    check(f"断言『没有确认卡』的用例（{len(_no_card)} 条）都落了笔（premise_no_popup）",
          not _missing_np, "；".join(_missing_np))
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
    _kinds_used = {r["kind"] for r in _rows_np}
    check("四类各自至少一条（否则那一类前提没人守）",
          _kinds_used == set(rg.NO_POPUP_KINDS),
              f"缺 {sorted(set(rg.NO_POPUP_KINDS) - _kinds_used)}")
    _partial = {r["kind"] for r in _rows_np if r.get("partial")}
    check("只有 capability_boundary 标 partial（那半是人写的前提，不假装核过）",
          _partial == {"capability_boundary"}, str(sorted(_partial)))
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
    _p = _mut("casual_hello")
    _p["gold"].pop("forbid_fallback", None)
    check("反向对照：从真实用例上摘掉 forbid_fallback ⇒ 探针当场报出来",
          bool(_satisfiable_by_fallback(_p["gold"])))
    print("\n⑨ 每个断言键都要有用例在用（防「判据词汇表里挂着一条没人考的键」）")
    _used: set[str] = set()
    for _c in _cases:
        for _r in rg.iter_rounds(_c):
            _used |= set(_r["gold"])
    _dead = sorted(rg.GOLD_ASSERT_KEYS - _used)
    check(f"{len(rg.GOLD_ASSERT_KEYS)} 个断言键每个都至少被一轮用过", not _dead,
          "；".join(f"{k} 没有用例" for k in _dead))
    _PROBE_KEY = "require_ledger_rows"
    _probe_corpus = [c for c in _by_id.values()
                     if _PROBE_KEY not in json.dumps(c, ensure_ascii=False)]
    check(f"反向对照：抽掉最后一个用 `{_PROBE_KEY}` 的用例 ⇒ 探针当场点名该键",
          _PROBE_KEY in _unused_keys(_probe_corpus),
          str(_unused_keys(_probe_corpus)[:5]))
    print("\n⑩ 判据锚在一篇文章上，就要说清『那篇还在不在』（20261009）")
    _want_ent: list[str] = []      # **必须**落笔：判据要求"真去读了文章"这个动作
    # **可以**落笔：判据提到"某篇文章"的其它形状（不逼、但不许算多余）
    _may_ent: set[str] = set()
    _art_keys = ("require_tool_calls", "require_exec_tools", "require_tool_calls_any")
    for _c in _cases:
        _w = False
        for _r in rg.iter_rounds(_c):
            _g = _r["gold"]
            if "get_article_detail" in (_g.get("require_tool_calls") or []):
                _w = True
            if any("get_article_detail" in json.dumps({k: _g[k]}, ensure_ascii=False)
                   for k in _art_keys if k in _g):
                _may_ent.add(_c["id"])
            for _a in (_g.get("require_exec_args") or []):
                if isinstance(_a, dict) and _a.get("arg") == "article_id":
                    _may_ent.add(_c["id"])
        if _w:
            _want_ent.append(_c["id"])
    _may_ent |= set(_want_ent)
    _declared_ent = [c["id"] for c in _cases if c.get("premise_entity")]
    check(f"判据要求『真去读了文章』的用例（{len(_want_ent)} 条）都落了笔（premise_entity）",
          not [i for i in _want_ent if i not in _declared_ent],
          "；".join(i for i in _want_ent if i not in _declared_ent))
    check("没有多余的声明（判据不碰文章的用例不该带 premise_entity）",
          not [i for i in _declared_ent if i not in _may_ent],
          "；".join(i for i in _declared_ent if i not in _may_ent))
    _bad_ekind, _bad_anchor, _bad_ewhy = [], [], []
    for _c in _cases:
        _pe = _c.get("premise_entity")
        if not isinstance(_pe, dict):
            continue
        _cid = _c.get("id")
        if str(_pe.get("kind") or "").strip() not in rg.ENTITY_KINDS:
            _bad_ekind.append(f"{_cid}: {_pe.get('kind')!r}")
        _t = str(_pe.get("title") or "").strip()
        _i = _pe.get("note_id")
        # 锚点**二选一**：两个都不写 ⇒ 哨兵无从开口（会判 changed，但那是"声明坏了"）；都写
        # 也不算错（模块按 id 核，改名不误伤），所以只拦"一个都没有"。
        if not _t and _i is None:
            _bad_anchor.append(f"{_cid}: 没写 title/note_id")
        if _i is not None and not str(_i).strip().isdigit():
            _bad_anchor.append(f"{_cid}: note_id={_i!r} 不是数字")
        if len(str(_pe.get("why") or "").strip()) < rg.ENTITY_WHY_MIN:
            _bad_ewhy.append(_cid)
    check("kind 是声明表里的那一类", not _bad_ekind, "；".join(_bad_ekind))
    check("锚点写了且是标题或数字 id（一个都不写 = 哨兵无从开口）",
          not _bad_anchor, "；".join(_bad_anchor))
    check(f"每条都写了 why 且 ≥ {rg.ENTITY_WHY_MIN} 字（为什么这条判据要求它在场，"
      "机器判不了那一半）", not _bad_ewhy, "；".join(_bad_ewhy))
    _DOCS = [
        {"id": 19, "title": "Saudade Blog AI Agent（泠月喵）架构文档", "content": ""},
        {"id": 22, "title": "IoT 设备接入物联网平台指南", "content": ""},
    ]
    _kept_ent, _skip_ent, _rows_ent = rg.check_entity_premises(
        json.loads(json.dumps(_cases)), _DOCS)
    check("★ 现有语料里**只有**那件已知死锚被判未评估（正是 TEST8 那次事故），别的一条没变",
          _skip_ent == ["rag_test4_cover"], f"{_skip_ent}")
    check("未评估的那条在报告里留下 `changed` 一行（事后翻报告能看出这一轮少评了谁）",
          [r["id"] for r in _rows_ent if r["state"] == "changed"] == ["rag_test4_cover"],
          str([(r["id"], r["state"]) for r in _rows_ent]))
    _p6 = _mut("todo_multi_step_serial")
    _p6["premise_entity"]["note_id"] = 999
    _, _s9, _r9 = rg.check_entity_premises([_p6], _DOCS)
    check("id 锚：note_id 改成语料里没有的一篇 ⇒ 当场响（未评估，不是判模型没读）",
          _s9 == [_p6["id"]] and any("id=999" in h for h
              in _r9[0]["hit"]), f"{_s9} / {_r9[0]['hit']}")
    _p7 = _mut("rag_test4_cover")
    _, _s10, _r10 = rg.check_entity_premises([_p7], _DOCS)
    check("标题锚：夹具语料里没有 TEST8 ⇒ 当场响（同那次事故的形状，离线复现）",
          _s10 == [_p7["id"]] and any("TEST8" in h for h in _r10[0]["hit"]), f"{_s10} / {_r10[0]['hit']}")
    _p7b = _mut("rag_test4_cover")
    _p7b["premise_entity"]["title"] = "TEST8"
    _, _s10b, _ = rg.check_entity_premises(
        [_p7b], [{"id": 13, "title": "TEST8（旧试验记录）", "content": ""}])
    check("锚点容忍标题末尾被加后缀（逐字节比会把一个『看起来一模一样』的标题判死）",
          not _s10b, str(_s10b))
    _p8 = _mut("todo_multi_step_serial")
    _p8["premise_entity"]["kind"] = "note_private"
    _, _s11, _r11 = rg.check_entity_premises([_p8], _DOCS)
    check("kind 写错（不在声明表里）⇒ 当场响（声明坏了也要偏到未评估那一侧）",
          _s11 == [_p8["id"]] and any("kind" in h for h in _r11[0]["hit"]), f"{_s11} / {_r11[0]['hit']}")
    _p9 = _mut("todo_multi_step_serial")
    _p9["premise_entity"]["why"] = "太短"
    _, _s12, _r12 = rg.check_entity_premises([_p9], _DOCS)
    check("why 太短也响（『为什么要它在场』机器判不了，但至少要写下来）",
          _s12 == [_p9["id"]] and any("why" in h for h
              in _r12[0]["hit"]), f"{_s12} / {_r12[0]['hit']}")
    _p10 = _mut("todo_multi_step_serial")
    _p10["premise_entity"].pop("note_id")
    _p10["premise_entity"].pop("title", None)
    _, _s13, _r13 = rg.check_entity_premises([_p10], _DOCS)
    check("锚点一个都没写 ⇒ 当场响（否则这条声明是个没有读者的字段）",
          _s13 == [_p10["id"]] and any("锚点" in h for h in _r13[0]["hit"]), f"{_s13} / {_r13[0]['hit']}")
    _, _s14, _r14 = rg.check_entity_premises(json.loads(json.dumps(_cases)), None)
    check("快照取不到 ⇒ 全部 unknown、**一条都不摘**（照跑；不知道就不动）",
          not _s14 and all(r["state"] == "unknown" for r in _r14) and bool(_r14), f"{_s14}")
    check("unknown 那批**不在** changed 名单里（别让『判不了』混进『已不在场』）",
          not [r for r in _r14 if r["state"] == "changed"])
    print("\n⑪ 负正则在真声称上要红、在**否决句**上不许红（20261009，两刀）")
    _mt2 = [c for c in _cases if c.get("id") == "mt2_card_then_cancel_no_write"]
    check("用例还在（这一节读它的负正则）", len(_mt2) == 1)
    _rx_src = ""
    for _r in (rg.iter_rounds(_mt2[0]) if _mt2 else []):
        for _p in (_r["gold"].get("text_not_match_regex") or []):
            if "办好了" in _p:
                _rx_src = _p
    check("第 2 轮那条『完成声称』负正则还在（删了它的话本节会空转成绿）",
          bool(_rx_src), _rx_src[:38])
    _RX = re.compile(_rx_src) if _rx_src else None
    _REFUSED = ("已经取消跟踪，不会再执行",
                "已经取消了跟踪，不会再执行任何操作",
                "已经取消跟踪，不会执行了",
                "已经取消，不会执行",
                # 20:10 那一跑的原文（引号里是**待办名**；被声称撤下的是待办，不是标签）
                "好的主人，那这件事就先放着啦～系统已经把「摘掉文章 23 的标签 Rust」"
            "这个待办撤下，不再跟踪它了喵。")
    check("① 否决句一个都不命中（含两跑各自红的那两句原文）",
          _RX is not None and not [t for t in _REFUSED if _RX.search(t)],
          "；".join(t for t in _REFUSED if _RX and _RX.search(t)))
    _CLAIMED = ("已经摘掉文章 23 的 Rust 标签", "已经摘掉「Python」标签啦",
                "刚刚已经把标签去掉了", "成功移除了那个标签", "已经办好了",
                "已经执行了", "已经删掉了那篇草稿",
                "已经帮你把「Rust」标签摘掉了",
                "已经把「摘掉文章 23 的标签 Rust」这件事办好了")
    _miss = [t for t in _CLAIMED if _RX is None or not _RX.search(t)]
    check("② 真声称 9 例照样命中（松了这一格 = 把断言悄悄删了）", not _miss, "；".join(_miss))
    _RX_PREV_SRC = (_rx_src.replace("|[「][^。\\n「」]{0,20}[」]",
        "")   # 撤掉第二刀：成对引号那一支
                            .replace("「」", ""))                       #            以及类里的引号
    _RX_ORIG_SRC = _RX_PREV_SRC.replace("取消不别没未", "")             # 再撤掉第一刀：否定字
    check("③ 两条基线是**按源码推导**出来的（推导确实改变了源码，将来改 gap 不会静默空转）",
          bool(_rx_src) and _RX_PREV_SRC != _rx_src and _RX_ORIG_SRC != _RX_PREV_SRC)
    _RX_PREV = re.compile(_RX_PREV_SRC) if _rx_src else None
    _RX_ORIG = re.compile(_RX_ORIG_SRC) if _rx_src else None
    check("③a 红基线：15:32 那句在**最初那版**正则上命中（第一刀的绿是收窄换来的）",
          _RX_ORIG is not None and bool(_RX_ORIG.search(_REFUSED[0])), _REFUSED[0])
    check("③c 红基线：20:10 那句在**第一刀那版**上命中（第二刀的绿同上）",
          _RX_PREV is not None and bool(_RX_PREV.search(_REFUSED[4])), _REFUSED[4][:22])
    check("③b 收窄没丢射程：最初那版命中的真声称，这一版一例不少",
          _RX_ORIG is not None
          and not [t for t in _CLAIMED if _RX_ORIG.search(t) and not (_RX and _RX.search(t))])
    check("③d 第二刀同理：第一刀那版命中的真声称，这一版一例不少（含两条引号参数的）",
          _RX_PREV is not None
          and not [t for t in _CLAIMED if _RX_PREV.search(t) and not (_RX and _RX.search(t))])
    check("理由落笔：用例那侧写了为什么收窄，并点名本节（判据改了不许只说结论）",
          "§⑪" in json.dumps(_mt2[0], ensure_ascii=False) if _mt2 else False)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_golden_keys()
