# -*- coding: utf-8 -*-
"""词表闸的**判据与反控**（20261014）。

**要治的病**：判据层几十张中文词表"默认可扩"——没有机器判据拦着新表长出来
（`docs/toolcall-stability-roadmap.md` §6 那条 KPI 此前只是一句散文）。
`eval/wordlist_gate.py` 把那句散文变成闸门，**本套件判的是"闸门自己还承重吗"**——
闸门的两层判据各自都要有一条**会红**的反控，否则"它能拦"与"它恒绿"长得一模一样
（本仓三犯过的老坑，见 `tests/test_complexity_budget.py` 与 `test_confirm.py` 的变异锁）。

十一节：

  ① **真值**：闸门在**真仓**上退 0（登记表与代码同步、没有未分类、没有 open 缺出路）
  ② **未登记即红**：临时树里放一个"被当匹配器用"的中文集合，登记表里没有它 ⇒ 退 1 且点名
  ③ **登记了但变大即红**（棘轮本体）
  ④ **`open` 缺出路即红**（`replacement` / `sunset` 少一个就红）／**`category` 没填即红**
  ⑤ **分层**：窄口径（只看"被当匹配器用"的表）**看不见**的中文表——即被当参数传给助手、
     或只做渲染的那一类——由**穷举量**那一层接住。**先证明它在原状下是绿的**，再加一个词看它红，
     且红的理由必须是"穷举量"而不是"未登记"（否则这一条测的还是第一层）
  ⑥ **变异锁**：把闸门源码里穷举量那一段整段挖掉 ⇒ ⑤ 那个反控**不再红**
     （没有它，"第二层在承重"与"第二层是装饰"长得一模一样）
  ⑦ **真数据反控**：从**真登记表**里撤掉一条 `open`、或把某条的 `size` 减一 ⇒ 当场红
  ⑧ **接线**：两层的判据都在闸门源码里（防"只留一层"被静默接受）＋
     本套件必须由 `tests/run_all.py` **按磁盘枚举**收进 CI（写成手抄名单 ⇒ 静默掉队）
  ⑨ **换了量 ⇒ 退 3**：登记表的 `targets` 与扫描面不一致时不许当成"变好/变差"读
  ⑩ **样板新模块里零条目**：`provenance.py` / `textnorm.py` 是这一族的规范形状，自己不该长词表
  ⑪ **eval 侧那一份（判据的判据）也在闸门里**（20261015 批 4）：golden 的负正则/正反词表、
     六个豁免开关、`run_golden.py` 的四张共享族各有一条**会红**的反控（加一个模式 / 多一条
     用例打开豁免 / 族多一个元素 / 同数量改写），一条变异锁证明「新模式」那一段在承重，
     一条真数据反控（往真登记表里塞一个模式）+ 一条 `--update` 锁（唯一放宽通道，只刷读数、
     category/q/why 逐条保留，且不就地写回真表）。**这一段与①②的量分开计数**——
     「运行时判据面少一张表」与「评测判据面多一条正则」不是同一个量。

⚠️ **登记表的 category 是复核物、不是防篡改锁**：把一条 `open` 手改成 `closed` 能让闸门变绿，
本套件拦不住，也不打算拦（同 `eval/complexity_baseline.json` —— 基线类产物一律靠 reviewer
读 diff）。闸门能拦的是**代码侧**：新表长出来、老表变大、`open` 不写出路。

秒级、纯文本 + AST，无网络无 LLM；由 `tests/run_all.py` 按磁盘枚举 ⇒ CI 自动到手。

用法：.venv/bin/python tests/test_wordlist_gate.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE_SRC = ROOT / "eval" / "wordlist_gate.py"
REGISTRY = ROOT / "eval" / "judge_registry.json"

FAILED: list[str] = []

# 闸门模块里 TARGETS 是**全局状态**，反控会把它换成 ["agent"]；每次跑都显式复位，
# 否则后面的真数据反控会拿着被换过的 TARGETS 去扫真仓（静默扫一半）。
_DEFAULT_TARGETS = ["agent", "tools", "server.py"]


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def load_gate(tag: str = "wordlist_gate"):
    """按文件路径加载闸门模块（`eval/` 不是包）。tag 可换 ⇒ 能同时挂多份互不干扰的副本。"""
    spec = importlib.util.spec_from_file_location(tag, GATE_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[tag] = mod
    spec.loader.exec_module(mod)
    return mod


def run(gate, root: pathlib.Path, baseline: pathlib.Path,
        targets: list[str] | None = None) -> tuple[int, str]:
    """在给定 ROOT/登记表上跑一次闸门，返回（退出码，全部输出）。"""
    gate.ROOT, gate.BASELINE = root, baseline
    gate.TARGETS = list(targets) if targets is not None else list(_DEFAULT_TARGETS)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gate.check()
    return rc, buf.getvalue()


def tree(files: dict[str, str]) -> pathlib.Path:
    """把 {相对路径: 源码} 落成一个临时树。"""
    d = pathlib.Path(tempfile.mkdtemp(prefix="wlgate_"))
    for rel, src in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return d


def snapshot_of(gate, root: pathlib.Path, targets: list[str] | None = None) -> dict:
    """按**当前**源码造一份"已如实登记"的登记表（全部登记为 closed ⇒ 不该有别的抱怨）。"""
    gate.ROOT = root
    gate.TARGETS = list(targets) if targets is not None else list(_DEFAULT_TARGETS)
    cands, metric = gate.scan()
    return {"_note": "临时", "targets": gate.TARGETS, "metric": metric,
            "entries": [{**c.as_json(), "category": "closed", "q": "x", "why": "反控用"}
                        for c in cands],
            # eval 侧那一段也如实登记（树里没有 eval 文件时它是三个空字典 ⇒ 闸门这一层沉默）
            "eval_vocab": eval_snapshot_of(gate)}


def eval_snapshot_of(gate) -> dict:
    """eval 侧（判据的**判据**）那一份"已如实登记"的登记表。category/why 一律填上。"""
    ev = gate._eval_vocab(None)
    for grp in ("keys", "exempt", "families"):
        for d in (ev.get(grp) or {}).values():
            d.update(category="closed", q="x", why="反控用")
    return ev


def write_reg(d: pathlib.Path, payload: dict, name: str = "_reg.json") -> pathlib.Path:
    p = d / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── 用到的两段源码 ─────────────────────────────────────────────────────────
# 「被当匹配器用」的中文集合（窄口径看得见）：`x in _WORDS`
_MATCHER_SRC = '''# -*- coding: utf-8 -*-
_WORDS = ("甲", "乙")


def hit(text: str) -> bool:
    return text in _WORDS
'''
# 「不被当匹配器用」的中文集合（窄口径看不见）：只做渲染/被当参数传出
_OPAQUE_SRC = '''# -*- coding: utf-8 -*-
_LABELS = ("甲", "乙")


def render(i: int) -> str:
    return _LABELS[i]
'''
# ── ⑪ 用的三份 eval 侧素材（都是**最小**的：只放这一节要动的形状）─────────────
# 一个负正则站、一个反词表站（多轮，第 1 轮带豁免开关）、一个"顶层 gold 与 rounds 并存"的
# 多轮用例（钉镜像的形状：只读 rounds）。
_EVAL_GOLDEN_SRC = "\n".join([
    json.dumps({"id": "ev_c1", "gold": {"text_not_match_regex": ["(?:已经|已)办好了"]}},
               ensure_ascii=False),
    json.dumps({"id": "ev_c2",
                "rounds": [{"round": 1, "gold": {"text_not_contains": ["已经办好了"],
                                                 "not_match_exempt_conditional": True}},
                           {"round": 2, "gold": {"text_contains": ["办好了"]}}]},
               ensure_ascii=False),
    json.dumps({"id": "ev_c3", "gold": {"text_not_contains": ["顶层这份不该被读"]},
                "rounds": [{"round": 1, "gold": {"text_not_contains": ["轮里的才该被读"]}}]},
               ensure_ascii=False),
]) + "\n"
# 四张共享族各一条（正则那张故意写成拼接，钉 `partial` 那条读数下界的路）
_EVAL_JUDGE_SRC = '''# -*- coding: utf-8 -*-
import re

DENIAL_FAMILY = ("甲", "乙")
ABSENCE_COMPOUND = ("没有找到",)
ABSENCE_LEADS = ("找到",)
_NOUN_TAIL = r"(?!用户|者)"
LOGIN_DEMAND_RE = re.compile(r"先登录" + _NOUN_TAIL)
'''

print("① 真值：闸门在真仓上退 0（登记表与代码同步）")
print("① 真值：闸门在真仓上退 0（登记表与代码同步）")
_g = load_gate()
_rc0, _out0 = run(_g, ROOT, REGISTRY)
check("①a 退出码 0", _rc0 == 0, f"rc={_rc0}")
check("①b 真登记表条目数与扫描一致（不多不少）",
      f"候选 {len(_g.scan()[0])} 个 / 已登记" in _out0.replace("登记表候选", "候选")
      and "未登记：" not in _out0 and "已销账" not in _out0)

print("\n② 未登记即红")
_d = tree({"agent/x.py": _MATCHER_SRC})
_snap = snapshot_of(_g, _d, targets=["agent"])
_reg = write_reg(_d, {**_snap, "entries": []})          # metric 对齐 ⇒ 只有②该红
_rc, _out = run(_g, _d, _reg, targets=["agent"])
check("②a 退 1", _rc == 1, f"rc={_rc}")
check("②b 红的理由是「未登记」且**只有**它（读汇总行的计数，不读恒在的词）",
      "❌ 未登记：agent/x.py::_WORDS" in _out and "1 未登记、0 条变大" in _out
      and "0 个文件穷举量变大" in _out)

print("\n③ 登记了但变大即红（棘轮本体）")
_reg_ok = write_reg(_d, _snap, "_ok.json")
check("③a 反控前提：原状是绿的", run(_g, _d, _reg_ok, targets=["agent"])[0] == 0)
(_d / "agent/x.py").write_text(_MATCHER_SRC.replace('"乙"', '"乙", "丙"'), encoding="utf-8")
_rc, _out = run(_g, _d, _reg_ok, targets=["agent"])
check("③b 加一个词 ⇒ 退 1 且点「只许变小」", _rc == 1 and "只许变小" in _out, f"rc={_rc}")

print("\n④ open 缺出路 / category 没填 ⇒ 红")
(_d / "agent/x.py").write_text(_MATCHER_SRC, encoding="utf-8")   # ③ 把树改脏了，还原
_snap = snapshot_of(_g, _d, targets=["agent"])
_e = {**_snap, "entries": [{**e, "category": "open"} for e in _snap["entries"]]}
_reg = write_reg(_d, _e, "_open.json")
_rc, _out = run(_g, _d, _reg, targets=["agent"])
check("④a open 没写 replacement/sunset ⇒ 退 1", _rc == 1 and "open 类没出路" in _out, f"rc={_rc}")
_e["entries"][0]["replacement"], _e["entries"][0]["sunset"] = "X", "批 9"
_reg = write_reg(_d, _e, "_open2.json")
check("④b 补上出路 ⇒ 绿", run(_g, _d, _reg, targets=["agent"])[0] == 0)
_e["entries"][0]["category"] = "?"
_reg = write_reg(_d, _e, "_q.json")
_rc, _out = run(_g, _d, _reg, targets=["agent"])
check("④c category 留 `?` ⇒ 退 1", _rc == 1 and "category 还没填" in _out, f"rc={_rc}")

print("\n⑤ 分层：窄口径看不见的中文表，由穷举量那一层接住")
_d2 = tree({"agent/y.py": _OPAQUE_SRC})
_snap2 = snapshot_of(_g, _d2, targets=["agent"])
check("⑤a 反控前提：这张表**没进**登记表（窄口径对它没意见）",
      _snap2["entries"] == [] and _snap2["metric"]["by_file"]["agent/y.py"][0] == 2,
      str(_snap2["metric"]["by_file"]))
_reg2 = write_reg(_d2, _snap2, "_ok2.json")
check("⑤b 反控前提：原状是绿的", run(_g, _d2, _reg2, targets=["agent"])[0] == 0)
(_d2 / "agent/y.py").write_text(_OPAQUE_SRC.replace('"乙"', '"乙", "丙"'), encoding="utf-8")
_rc, _out = run(_g, _d2, _reg2, targets=["agent"])
check("⑤c 加一个词 ⇒ 退 1，且**红的只有穷举量那一层**（精确读计数）",
      _rc == 1 and "0 未登记、0 条变大、0 未分类、0 条 open 缺出路、1 个文件穷举量变大"
      in _out, f"rc={_rc}")

print("\n⑥ 变异锁：挖掉穷举量那一段 ⇒ ⑤ 不再红（证明第二层在承重）")
_cut = ("if any(v[i] > int(_oldby.get(f, [0, 0])[i]) for i in (0, 1))]", "if False]")
_src = GATE_SRC.read_text(encoding="utf-8")
check("⑥a 锚点恰好命中 1 处（源码一漂就停，别静默不生效）", _src.count(_cut[0]) == 1,
      f"命中 {_src.count(_cut[0])} 次")
if _src.count(_cut[0]) == 1:
    mut = pathlib.Path(tempfile.mkdtemp(prefix="wlmut_")) / "gate_mut.py"
    mut.write_text(_src.replace(*_cut), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gate_mut", mut)
    gm = importlib.util.module_from_spec(spec)
    sys.modules["gate_mut"] = gm
    spec.loader.exec_module(gm)
    _rcm, _outm = run(gm, _d2, _reg2, targets=["agent"])
    check("⑥b 挖掉之后 ⑤ 那种改动退 0（⇒ ⑤c 的红确实来自穷举量那一层）",
          _rcm == 0, f"rc={_rcm}")
    check("⑥c 变异体在真仓上也不再报穷举量（同一段只管这一件事）",
          "穷举量变大" not in run(gm, ROOT, REGISTRY)[1])
    shutil.rmtree(mut.parent, ignore_errors=True)

print("\n⑦ 真数据反控：动真登记表 ⇒ 当场红")
_d3 = pathlib.Path(tempfile.mkdtemp(prefix="wlreal_"))
_real = json.loads(REGISTRY.read_text(encoding="utf-8"))
_open_ids = [e["id"] for e in _real["entries"] if e["category"] == "open"]
check("⑦a 真登记表里有 open 条目可撤", bool(_open_ids), f"{len(_open_ids)} 条")
_dropped = {**_real, "entries": [e for e in _real["entries"] if e["id"] != _open_ids[0]]}
_rc, _out = run(_g, ROOT, write_reg(_d3, _dropped, "_drop.json"))
check("⑦b 撤掉一条 open ⇒ 退 1 且点名同一符号",
      _rc == 1 and f"未登记：{_open_ids[0]}" in _out, f"rc={_rc}")
_shrunk = {**_real, "entries": [
    ({**e, "size": e["size"] - 1} if e["id"] == _open_ids[1] else e)
    for e in _real["entries"]]}
_rc, _out = run(_g, ROOT, write_reg(_d3, _shrunk, "_shrink.json"))
check("⑦c 把某条 size 减一（＝表长大了）⇒ 退 1 且点「只许变小」",
      _rc == 1 and "只许变小" in _out, f"rc={_rc}")
_rc, _out = run(_g, ROOT, _d3 / "_missing.json")
check("⑦d 没有登记表 ⇒ 退 3（跑不起来 ≠ 通过）", _rc == 3, f"rc={_rc}")
print("\n⑧ 闸门自己的接线：两级判据都在源码里（防「只留一层」被静默接受）")
_src = GATE_SRC.read_text(encoding="utf-8")
check("⑧a 未登记/变大/未分类/open 出路 四条都在", all(
    k in _src for k in ("未登记：", "只许变小", "category 还没填", "open 类没出路")))
check("⑧b 穷举量与窄口径并存（`_file_metric` 与 `_matcher_names` 都被调用）",
      _src.count("_file_metric(") >= 2 and _src.count("_matcher_names(") >= 2)
check("⑧c 进套件靠**按磁盘枚举**不靠点名：run_all.py 有 glob、且一次都没提过本文件名"
      "（改成手抄名单 ⇒ 本套件静默掉队，CI 再也看不见闸门）",
      'TESTS.glob("*.py")' in (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
      and "test_wordlist_gate" not in (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8"))

print("\n⑨ 换了个量 ⇒ 退 3（不是「变好/变差」，是换了扫描面）")
_shifted = {**_real, "targets": ["agent"]}
_d4 = pathlib.Path(tempfile.mkdtemp(prefix="wlshift_"))
_rc, _out = run(_g, ROOT, write_reg(_d4, _shifted, "_shift.json"))
check("⑨a 登记表的 targets 与扫描面不一致 ⇒ 退 3", _rc == 3 and "换了个量" in _out, f"rc={_rc}")
_guard = ('if list(whole.get("targets", [])) != list(TARGETS):', "if False:")
_src_g = GATE_SRC.read_text(encoding="utf-8")
check("⑨b₁ 变异锚点恰好命中 1 处（源码一漂就停，别静默不生效）",
      _src_g.count(_guard[0]) == 1, f"命中 {_src_g.count(_guard[0])} 次")
if _src_g.count(_guard[0]) == 1:
    _mg = pathlib.Path(tempfile.mkdtemp(prefix="wlguard_")) / "gate_noguard.py"
    _mg.write_text(_src_g.replace(*_guard), encoding="utf-8")
    _spec = importlib.util.spec_from_file_location("gate_noguard", _mg)
    _gn = importlib.util.module_from_spec(_spec)
    sys.modules["gate_noguard"] = _gn
    _spec.loader.exec_module(_gn)
    _rcg, _outg = run(_gn, ROOT, _d4 / "_shift.json")   # 与 ⑨a 同一份"换面"的登记表
    check("⑨b₂ 挖掉守卫 ⇒ 同一次「换面」被读成 `✅ 没有变差`（rc=0）——"
          "与「真的没变差」**逐字同形**，这正是这条守卫存在的理由",
          _rcg == 0 and "✅ 没有变差" in _outg, f"rc={_rcg}")
    shutil.rmtree(_mg.parent, ignore_errors=True)

print("\n⑩ 规范形状自己也被看着：新模块里不许有词表形状")
_new = ("agent/provenance.py", "agent/textnorm.py")
check("⑩ provenance.py / textnorm.py 在登记表里**零条目**（引文核对与归一化是样板，不该有词表）",
      not [e for e in _real["entries"] if e["file"] in _new],
      str([e["id"] for e in _real["entries"] if e["file"] in _new]))
shutil.rmtree(_d3, ignore_errors=True)
shutil.rmtree(_d4, ignore_errors=True)

print("\n⑪ eval 侧（判据的**判据**）的棘轮：加一条负正则/一个豁免/一个族元素 ⇒ 都要红")
# 这一节治的是**第二张脸**：agent/ 那份词表看着，eval/ 侧（golden 的负正则、正反词表、
# 六个豁免开关、run_golden 的四张共享族）此前谁都没看着。它必须与①②**分开计数**：
# "运行时判据面少一张表"与"评测判据面多一条正则"不是同一个量。
_d5 = tree({"agent/x.py": _MATCHER_SRC, "eval/golden/basic.jsonl": _EVAL_GOLDEN_SRC,
            "eval/run_golden.py": _EVAL_JUDGE_SRC})
_snap5 = snapshot_of(_g, _d5, targets=["agent"])
_reg5 = write_reg(_d5, _snap5, "_ev_ok.json")
check("⑪a 反控前提：原状是绿的（eval 侧如实行过登记）",
      run(_g, _d5, _reg5, targets=["agent"])[0] == 0)
check("⑪b 镜像锁：多轮用例只读 `rounds` 里的 gold，顶层那份不读（同 `iter_rounds`）",
      _snap5["eval_vocab"]["keys"]["text_not_contains"]["patterns"] == ["已经办好了", "轮里的才该被读"],
      str(_snap5["eval_vocab"]["keys"]["text_not_contains"]["patterns"]))

_p = _d5 / "eval/golden/basic.jsonl"


def _golden_write(lines: list[str]) -> None:
    _p.write_text("\n".join(lines) + "\n", encoding="utf-8")


_golden_write([_EVAL_GOLDEN_SRC.replace("|已)办好了", "|已)办妥了")])   # 换掉一个模式（数量不变）
_rc, _out = run(_g, _d5, _reg5, targets=["agent"])
_n_red_lines = sum(1 for l in _out.splitlines() if l.startswith("  ❌"))
check("⑪c **换掉**一个模式（格/项/数量都不变）⇒ 只可能由「新模式」那一条判红",
      _rc == 1 and _n_red_lines == 1 and "多了 1 个模式" in _out and "办妥了" in _out,
      f"rc={_rc} / 红 {_n_red_lines} 条")
_golden_write([_EVAL_GOLDEN_SRC])                                       # 还原
_c1 = json.loads(_EVAL_GOLDEN_SRC.splitlines()[0])
_c1["gold"]["text_not_match_regex"].append("(?:帮你|给你)办好了")
_lines = _EVAL_GOLDEN_SRC.splitlines()
_lines[0] = json.dumps(_c1, ensure_ascii=False)
_golden_write(_lines)
_rc, _out = run(_g, _d5, _reg5, targets=["agent"])
check("⑪c₂ **追加**一个模式 ⇒ 两条红（新模式 + 项数变大）——一次改动可能同时踩两条线",
      _rc == 1 and "多了 1 个模式" in _out and "变大" in _out, f"rc={_rc}")
check("⑪d 红**只**来自 eval 侧那一层（agent 那套计数一个字都不动 ⇒ 两段没有混成一个量）",
      "eval 侧 2 项变差" in _out and "未登记、0 条变大、0 未分类、0 条 open 缺出路、"
      "0 个文件穷举量变大" not in _out, _out.strip().splitlines()[-1])

_lines = list(_EVAL_GOLDEN_SRC.splitlines())
_lines.append(json.dumps({"id": "ev_c4", "gold": {"text_contains": ["办好了"],
                                                  "not_match_exempt_conditional": True}},
                         ensure_ascii=False))
_golden_write(_lines)
_rc, _out = run(_g, _d5, _reg5, targets=["agent"])
check("⑪e 多一条用例打开豁免开关 ⇒ 红并**点名那条用例**（豁免是判据替自己打的补丁）",
      _rc == 1 and "多被 1 条用例打开了" in _out and "ev_c4" in _out, f"rc={_rc}")
_golden_write(list(_EVAL_GOLDEN_SRC.splitlines()))                     # 还原文案

_j = _d5 / "eval/run_golden.py"
_j.write_text(_EVAL_JUDGE_SRC.replace('("甲", "乙")', '("甲", "乙", "丙")'), encoding="utf-8")
_rc, _out = run(_g, _d5, _reg5, targets=["agent"])
check("⑪f₁ 共享族多一个元素 ⇒ 红（`DENIAL_FAMILY` 那一族是逐次加词长出来的）",
      _rc == 1 and "共享族 `DENIAL_FAMILY` 变大" in _out, f"rc={_rc}")
_j.write_text(_EVAL_JUDGE_SRC.replace('("甲", "乙")', '("甲", "丙")'), encoding="utf-8")
_rc, _out = run(_g, _d5, _reg5, targets=["agent"])
check("⑪f₂ **同数量改写**也红（指纹变了）——「数量不变、条目被换掉」正是这一族的病",
      _rc == 1 and "同数量改写" in _out, f"rc={_rc}")
_j.write_text(_EVAL_JUDGE_SRC, encoding="utf-8")

print("\n⑪g 变异锁：两条判据各挖一次 ⇒ 对应的那次改动不再红（证明是它们在承重）")


def _mutant(anchor: str, repl: str, tag: str):
    """把闸门源码里的 `anchor` 换成 `repl`，返回变异体模块（锚点不唯一则返回 None）。"""
    src = GATE_SRC.read_text(encoding="utf-8")
    if src.count(anchor) != 1:
        return None
    d = pathlib.Path(tempfile.mkdtemp(prefix=f"evmut_{tag}_"))
    f = d / f"gate_{tag}.py"
    f.write_text(src.replace(anchor, repl), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"gate_{tag}", f)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"gate_{tag}"] = mod
    spec.loader.exec_module(mod)
    return mod


_cut_pat = ('newpat = [p for p in got.get("patterns", []) if p not in set(want.get("patterns") or [])]',
            "newpat = []")
_gm_pat = _mutant(*_cut_pat, "pat")
check("⑪g₁ 锚点「新模式」恰好命中 1 处（源码一漂就停，别静默不生效）", _gm_pat is not None)
if _gm_pat:
    _golden_write([_EVAL_GOLDEN_SRC.replace("|已)办好了", "|已)办妥了")])   # 与 ⑪c 同一处改动
    _rcm, _outm = run(_gm_pat, _d5, _reg5, targets=["agent"])
    check("⑪g₂ 挖掉它 ⇒ ⑪c 那处改动退 0（⇒ ⑪c 的红确实来自这一段）", _rcm == 0, f"rc={_rcm}")
    _golden_write(list(_EVAL_GOLDEN_SRC.splitlines()))

_cut_sz = ('if got["sites"] > want["sites"] or got["items"] > want["items"]:', "if False:")
_gm_sz = _mutant(*_cut_sz, "size")
check("⑪g₃ 锚点「格/项变大」恰好命中 1 处", _gm_sz is not None)
if _gm_sz:
    _evs = json.loads(json.dumps(_real["eval_vocab"]))
    _evs["keys"]["text_not_contains"]["sites"] -= 1
    _regs = write_reg(_d5, {**_real, "eval_vocab": _evs}, "_evs2.json")
    check("⑪g₄ 反控前提：未挖掉时那一处改动是红的",
          run(_g, ROOT, _regs)[0] == 1)
    check("⑪g₅ 挖掉它 ⇒ 同一处改动退 0（⇒ ⑪h₂ 的红确实来自这一段）",
          run(_gm_sz, ROOT, _regs)[0] == 0)

print("\n⑪h 真数据反控 + 唯一放宽通道：动真登记表的 eval 段 ⇒ 当场红；`--update` 保留理由")
_d6 = pathlib.Path(tempfile.mkdtemp(prefix="evreal_"))
_ev = json.loads(json.dumps(_real["eval_vocab"]))
_ev["keys"]["text_not_match_regex"]["patterns"].pop()      # 登记表少一个模式（源码里有）
_rc, _out = run(_g, ROOT, write_reg(_d6, {**_real, "eval_vocab": _ev}, "_evp.json"))
check("⑪h₁ 真登记表比现场少一个模式 ⇒ 退 1 且把那个模式点名（「新增必须登记理由」的落点）",
      _rc == 1 and "多了 1 个模式" in _out, f"rc={_rc}")
_ev2 = json.loads(json.dumps(_real["eval_vocab"]))
_ev2["keys"]["text_not_contains"]["sites"] -= 1
_rc, _out = run(_g, ROOT, write_reg(_d6, {**_real, "eval_vocab": _ev2}, "_evs.json"))
check("⑪h₂ 把某条的格数减一（＝评测判据面长大了）⇒ 退 1 且点「只许变小」",
      _rc == 1 and "只许变小" in _out, f"rc={_rc}")
_rc, _out = run(_g, ROOT, write_reg(_d6, {**_real, "eval_vocab": {}}, "_evf.json"))
check("⑪h₃ 整块 `eval_vocab` 撤掉 ⇒ 退 1（否则「eval 侧没人看着」与「eval 侧没问题」同形）",
      _rc == 1 and "没有 `eval_vocab` 这一段" in _out, f"rc={_rc}")
_before = REGISTRY.read_bytes()
_g.ROOT, _g.TARGETS = ROOT, list(_DEFAULT_TARGETS)
_g.BASELINE = _d6 / "_upd.json"
shutil.copyfile(REGISTRY, _g.BASELINE)
_g.update()
_after = json.loads(_g.BASELINE.read_text(encoding="utf-8"))
_missing = [k for grp in ("keys", "exempt", "families")
            for k, v in _real["eval_vocab"][grp].items()
            if _after["eval_vocab"][grp].get(k, {}).get("why") != v.get("why")]
check("⑪h₄ `--update` 是唯一放宽通道，且**只刷新读数**：category/q/why 逐条保留",
      not _missing, str(_missing))
_g.BASELINE = REGISTRY
check("⑪h₅ `--update` 没碰真登记表（就地取材 ≠ 就地写回）", REGISTRY.read_bytes() == _before)
_rc, _out = run(_g, ROOT, REGISTRY)          # 复位后再跑一次真值
check("⑪h₆ 复位后真仓仍是绿的", _rc == 0 and "✅ 没有变差" in _out, f"rc={_rc}")
shutil.rmtree(_d5, ignore_errors=True)
shutil.rmtree(_d6, ignore_errors=True)

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
