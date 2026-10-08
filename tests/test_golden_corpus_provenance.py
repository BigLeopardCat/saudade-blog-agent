# -*- coding: utf-8 -*-
"""语料出处闸（20261006）：**这一轮评的是不是原来那块地**。

判据本体在 `eval/corpus_provenance.py`（纯函数、只读一个 JSON），本套件离线秒级、不取数、
不联网——这也正是那个模块被拆出来的理由（见它的头注：离线套件 import 不起 `run_golden`，
那一串是 `server` → `agent.graph` → langchain）。

**这个闸要防的是什么**：这个仓是公开的，而 golden 的每条期望都锚在维护者站点的文章上。
别人 clone 下来对着自己的博客跑，判据对不上——而**症状与「模型退化」一模一样**：全表飘红，
复审单上一条条写着模型的错话。这条闸的作用就是**在那片红出现之前**说一句「判据脚下这块地
不是原来那块」。

**本套件最贵的一条**（§① 的第三、第四条断言）：**空语料只能判 unknown，不许判 foreign**。
`tools/base._get` 把连不上/5xx 一律吞成 `UPSTREAM_DOWN`（不抛），`_fetch_corpus` 于是
正常返回 0 篇 ⇒「接口不通」「地址指错」「真的没文章」在读数上完全一样。硬判 foreign 等于
把一次网络故障写成「你换了语料」，而这条闸的整段理由就是反对这种归因。实测过这条路径：
它同时是 `--only` 离线调试的安全阀（那时语料本来就取不到）。
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))         # 与两个跑法同一条 import 路径

import corpus_provenance as cp  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def state_of(docs) -> str:
    return cp.check_corpus_premises(docs)[0]


def _docs(*titles: str) -> list:
    return [{"title": t} for t in titles]


def state_with_prov(prov: dict, docs) -> str:
    """把声明换成内存里这一份，跑一次判据（临时文件，跑完即删）。"""
    orig = cp.CORPUS_PROVENANCE_FILE
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prov, f, ensure_ascii=False)
        cp.CORPUS_PROVENANCE_FILE = path
        return cp.check_corpus_premises(docs)[0]
    finally:
        cp.CORPUS_PROVENANCE_FILE = orig
        os.unlink(path)


# 真声明里的锚点（逐字抄自 eval/golden/provenance.json 的 title 字段）。抄一份是**故意的**：
# 下面几条要验「换了别的语料会被拦下」，拿声明里的标题当语料就永远验不出拦截。
REAL_ANCHORS = [
    "ESP32-S3-OBC固件接入参考",
    "IoT 设备接入物联网平台指南",
    "ESP32-S3 OTA 问题与解决记录",
    "Saudade Blog AI Agent（泠月喵）架构文档",
    "Git从入门到入土",
]

# ══════════════════════════════════════════════════════════════════
print("\n① 三态：ok / foreign / unknown（unknown 有两种成因）")

check("锚点全在 ⇒ ok", state_of(_docs(*REAL_ANCHORS)) == "ok")

check("语料有东西但一篇锚点都对不上 ⇒ foreign（这正是「别人的博客」那一格）",
      state_of(_docs("我的第一篇", "随笔：今天天气不错", "读《人月神话》")) == "foreign")

check("语料快照取不到 ⇒ unknown（**不是 foreign**：取不到与空在读数上分不开）",
      state_of(None) == "unknown", state_of(None))
check("语料快照是空的 ⇒ unknown（同上：接口不通 / 地址指错 / 真没文章，读数一样）",
      state_of([]) == "unknown", state_of([]))
check("两者都**不是** foreign —— 被判 foreign 就会被摘光全部用例并退 3",
      state_of(None) != "foreign" and state_of([]) != "foreign")

check("锚点够但不是全部（5 个里中 3 个）⇒ 仍是 ok（容忍改名/删文，见 min_present）",
      state_of(_docs(*REAL_ANCHORS[:3], "无关的一篇")) == "ok")

# ══════════════════════════════════════════════════════════════════
print("\n② 标题比对：去空白 + 大小写 + 子串命中")

check("末尾/内部空白不影响命中（库里那条 `…解决记录 ` 就带尾空格）",
      state_of(_docs("ESP32-S3 OTA 问题与解决记录   ", "IoT 设备接入物联网平台指南",
                     "Git从入门到入土")) == "ok")
check("大小写不影响命中",
      state_of(_docs("esp32-s3-obc固件接入参考", "iot 设备接入物联网平台指南",
                     "git从入门到入土")) == "ok")
check("锚点是语料标题的**子串**即算命中（标题被加了后缀也算同一篇）",
      state_of(_docs("Git从入门到入土（2026 修订版）", "IoT 设备接入物联网平台指南 - 上篇",
                     "ESP32-S3-OBC固件接入参考")) == "ok")
check("**反向不成立**：语料标题只是锚点的一截 ⇒ 不算命中（挡住短标题撞车）",
      state_of(_docs("ESP32-S3", "IoT 设备", "Git")) == "foreign")

# ══════════════════════════════════════════════════════════════════
print("\n③ 阈值：min_present（声明）与它的默认值（锚点数的一半）")

_ANCH = [{"title": t} for t in ("甲文", "乙文", "丙文", "丁文")]
check("min_present=3、命中 3 ⇒ ok",
      state_with_prov({"anchors": _ANCH, "min_present": 3},
                      _docs("甲文", "乙文", "丙文")) == "ok")
check("同上一份声明、只命中 2 ⇒ foreign（阈值真的在起作用）",
      state_with_prov({"anchors": _ANCH, "min_present": 3},
                      _docs("甲文", "乙文", "别的")) == "foreign")
check("不写 min_present ⇒ 默认锚点数的一半（4 个锚点 ⇒ 需 2 个）",
      state_with_prov({"anchors": _ANCH}, _docs("甲文", "乙文")) == "ok")
check("同上、只命中 1 ⇒ foreign",
      state_with_prov({"anchors": _ANCH}, _docs("甲文")) == "foreign")
check("声明里锚点是空表 ⇒ unknown，不是 foreign",
      state_with_prov({"anchors": []}, _docs("甲文")) == "unknown")
check("锚点写了但 title 是空白 ⇒ 视同没有锚点 ⇒ unknown",
      state_with_prov({"anchors": [{"title": "   "}]}, _docs("甲文")) == "unknown")

# ══════════════════════════════════════════════════════════════════
print("\n④ 声明写坏 / 文件不在 ⇒ unknown（「不知道就不动」）")

_orig_file = cp.CORPUS_PROVENANCE_FILE
fd, _bad = tempfile.mkstemp(suffix=".json")
os.close(fd)
with open(_bad, "w", encoding="utf-8") as f:
    f.write("{ 这不是 json")
try:
    cp.CORPUS_PROVENANCE_FILE = _bad
    check("JSON 写坏 ⇒ unknown（判不了 ≠ 不可用）",
          state_of(_docs(*REAL_ANCHORS)) == "unknown")
    cp.CORPUS_PROVENANCE_FILE = str(ROOT / "eval" / "golden" / "不存在的文件.json")
    check("文件不在 ⇒ unknown", state_of(_docs(*REAL_ANCHORS)) == "unknown")
finally:
    cp.CORPUS_PROVENANCE_FILE = _orig_file
    os.unlink(_bad)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 真声明在位，且它声明的是**工具出口那个默认地址**")

_prov = json.loads((ROOT / "eval" / "golden" / "provenance.json").read_text(encoding="utf-8"))
check("provenance.json 解析得了、有 anchors",
      bool(_prov.get("anchors")), f"{len(_prov.get('anchors') or [])} 条")
check("每条锚点都有 title 与 why（why 是给复审的人看的，缺了就等于没说）",
      all((a.get("title") or "").strip() and (a.get("why") or "").strip()
          for a in _prov["anchors"]))
check("min_present ≤ 锚点数（否则这份声明自己永远判 foreign）",
      1 <= int(_prov.get("min_present") or 0) <= len(_prov["anchors"]),
      str(_prov.get("min_present")))
check("声明的那几个锚点**真的都在真语料里认得出**（拿真声明跑真锚点）",
      state_of(_docs(*REAL_ANCHORS)) == "ok")

from config.settings import Settings  # noqa: E402  （只在需要时导入，见文件头）

_default = Settings.model_fields["blog_api_base"].default
check("declared_for.api_base == settings.blog_api_base 的默认值"
      "（改了默认地址却没改声明 ⇒ 红：闸会拿旧站点的锚点去认新语料）",
      (_prov.get("declared_for") or {}).get("api_base") == _default,
      f"{(_prov.get('declared_for') or {}).get('api_base')} vs {_default}")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 接线：两个跑法共用一处实现，且全量跑法把闸放在主循环**之前**")

_RG = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
_FR = (ROOT / "eval" / "golden_full_run.py").read_text(encoding="utf-8")

check("判据本体只有一份（run_golden / golden_full_run 里都没有第二份定义）",
      "def check_corpus_premises" not in _RG and "def check_corpus_premises" not in _FR)
check("两个跑法都调它", "check_corpus_premises(" in _RG and "check_corpus_premises(" in _FR)
_idx_gate = _FR.index("check_corpus_premises(")
_idx_loop = _FR.index("for i, case in enumerate(CASES, 1):")
check("全量跑法里闸在主循环**之前**（一次全量约 18 分钟，摆在后面等于白跑）",
      _idx_gate < _idx_loop, f"{_idx_gate} < {_idx_loop}")

# `provenance_path_for` 曾经是**全仓唯一一个没人调用过的函数**：它少写了一个 `import os`，
# 两个跑法一启动就 NameError，而整套离线判据**全绿**——因为没有任何一条碰过它（20261006
# 造夹具时才发现，转手就把它炸出来了）。教训与"判据看着在、其实不在"同款：**判据得真的
# 调一次那个函数**，源码里 `"provenance_path_for(" in src` 这种字面量断言拦不住这种病。
check("用例路径 → 声明路径：声明与用例同目录（`--golden` 换谁就按谁旁边那份判）",
      cp.provenance_path_for("eval/golden/basic.jsonl") == cp.CORPUS_PROVENANCE_FILE
      and cp.provenance_path_for("eval/fixtures/golden_smoke.jsonl")
      == "eval/fixtures/provenance.json",
      cp.provenance_path_for("eval/fixtures/golden_smoke.jsonl"))
check("裸文件名（没有目录）⇒ 落在当前目录、不炸",
      os.path.normpath(cp.provenance_path_for("basic.jsonl")) == "provenance.json",
      cp.provenance_path_for("basic.jsonl"))
check("两个跑法都用它推声明路径（各自拼一遍路径就会 A 的声明配 B 的用例）",
      "provenance_path_for(" in _RG and "provenance_path_for(" in _FR)

for _name, _src in (("run_golden.py", _RG), ("golden_full_run.py", _FR)):
    _m = re.search(r"^\s*_code = 3 if \(.*$", _src, re.M)
    check(f"{_name}：摘光全部用例时空分母报 **3**（未评估），不是 2（自己摘的）",
          bool(_m) and "corpus" in _m.group(0).lower(),
          (_m.group(0).strip() if _m else "没找到那一行"))
    check(f"{_name}：报告里带上出处对账（事后翻报告能看出这一轮评的是哪块地）",
          '"corpus_provenance":' in _src)

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 文案：foreign 那几行必须说清「这不是模型退化」并给出两条出路")

_lines = "\n".join(cp.report_lines(*cp.check_corpus_premises(_docs("别人的一篇"))))
check("明写**这不是模型退化**（读报告的人最容易读反的一句）", "这不是模型退化" in _lines)
check("给出「自己重做判据」的出路", "重做判据" in _lines)
check("给出自包含夹具的指路", "eval/fixtures/README.md" in _lines)
check("给出「只是改了名」时改锚点的办法",
      "anchors" in _lines and "provenance.json" in _lines)
# 修法要指向**这一轮真正读的那份声明**：`--golden eval/fixtures/golden_smoke.jsonl` 时，
# 顺手写默认那常量就把人指到另一个文件上去了（20261006 实跑 --golden 时抓到的原话）。
_alt_lines = "\n".join(cp.report_lines(
    *cp.check_corpus_premises(_docs("别人的一篇"), "eval/fixtures/provenance.json")))
check("修法点名的是**这一轮**的声明文件（不是默认那份常量）",
      "eval/fixtures/provenance.json" in _alt_lines, _alt_lines.splitlines()[-1])
_unknown_lines = "\n".join(cp.report_lines(*cp.check_corpus_premises(None)))
check("unknown 那行说「照跑」且指向先看这一行（**不是**拦下）",
      "判不了" in _unknown_lines and "照跑" in _unknown_lines)
check("ok 时**不打任何一行**（干净的轮次不该多出噪音）",
      cp.report_lines(*cp.check_corpus_premises(_docs(*REAL_ANCHORS))) == [])

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 实体前提闸（20261009）：同一处实现、两个跑法都接线、措辞不许读反")
# 与 ⑥ 同一条纪律：闸的实现只有一处，两个跑法共用。用例侧"该不该落笔"的锁在
# `tests/test_golden_keys.py` 第 ⑩ 节（那边才有用例文件），这里锁**接线**。
check("判据本体只有一份（两个跑法里都没有第二份定义）",
      "def check_entity_premises" not in _RG and "def check_entity_premises" not in _FR)
check("两个跑法都调它",
      "check_entity_premises(" in _RG and "check_entity_premises(" in _FR)
check("两个跑法都把结论翻成人能读的那几行（各写一份措辞早晚会漏掉那句话）",
      "entity_report_lines(" in _RG and "entity_report_lines(" in _FR)
# 与语料闸**共用同一次快照**：各取一次就是第二个会漂移的地方（写成两个 snapshot_docs()
# 调用 ⇒ 这里当场红）。
check("全量跑法里实体闸复用**语料闸那份快照**（不许再取一次）",
      _FR.count("corpus_provenance.snapshot_docs()") == 1)
_idx_ent = _FR.index("check_entity_premises(")
check("全量跑法里实体闸也在主循环**之前**（它摘掉的那几条否则会白烧一次 LLM）",
      _idx_ent < _idx_loop, f"{_idx_ent} < {_idx_loop}")
check("两个跑法都把它并进 skipped_ids（未评估要单列，不许静默豁免）",
      "_SKIPPED_IDS += _ENTITY_SKIPPED" in _FR and "skip_ids += _entity_skipped" in _RG)
_check_pair = (("run_golden.py", _RG, "skipped_entity_ids", "entity_checks"),
               ("golden_full_run.py", _FR, "skipped_entity_ids", "entity_checks"))
for _name, _src, _k1, _k2 in _check_pair:
    check(f"{_name}：报告里单列未评估的那几条 + 逐条结论",
          f'"{_k1}":' in _src and f'"{_k2}":' in _src)

_DOC19 = [{"id": 19, "title": "Saudade Blog AI Agent（泠月喵）架构文档", "content": ""}]
_ent_cases = [{"id": "self-check", "premise_entity": {
    "kind": "note_visible", "note_id": 19, "why": "探针：这条判据锚在文章 19 上"}}]
_kept, _skipped, _rows = cp.check_entity_premises(_ent_cases, _DOC19)
check("在场 ⇒ 照跑、不打任何一行（干净的轮次不该多出噪音）",
      not _skipped and cp.entity_report_lines(_rows, _skipped) == [])
_, _skipped2, _rows2 = cp.check_entity_premises(_ent_cases, [])
_unknown_lines = "\n".join(cp.entity_report_lines(_rows2, _skipped2))
check("快照取不到 ⇒ 判不了、**照跑**，且要打一行说清（『没报错』≠『核过了』）",
      not _skipped2 and _rows2[0]["state"] == "unknown"
      and "判不了" in _unknown_lines and "照跑" in _unknown_lines)
_, _skipped3, _rows3 = cp.check_entity_premises(_ent_cases, [{"id": 7, "title": "别的"}])
_changed_lines = "\n".join(cp.entity_report_lines(_rows3, _skipped3))
check("不在场 ⇒ 未评估，且那一行**明写「这不是模型退化」**（读报告的人最容易读反的一句）",
      _skipped3 == ["self-check"] and "这不是模型退化" in _changed_lines)
check("不在场那行给出两条出路（换锚 / 退役），并指向报告字段",
      "改锚" in _changed_lines and "entity_checks" in _changed_lines)

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：")
    for f in FAILS:
        print("   - " + f)
    raise SystemExit(1)
print("✅ 语料出处闸：全部通过")
