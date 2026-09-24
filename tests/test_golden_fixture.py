# -*- coding: utf-8 -*-
"""真写夹具的只读在位检查（`eval/golden_fixture.py`，20260925）。

**为什么这条测试存在**：`golden_write_category_delete_exec` 是本仓第一条**真删生产库**的用例，
它的两条安全边界（"前置条件不在就不跑"、"残留得看得见"）都落在 `eval/golden_fixture.py` 上。
这个模块小到看起来不值得测——但它判错的两个方向都很贵：

  · 把 `unreadable` 判成 `absent`（网络抖一下）⇒ 用例静默 SKIP，**看着"不需要跑"**，
    而真相是"我们不知道夹具在不在"；
  · 把前缀族判宽（认名字中间出现该串）⇒ 哨兵对着一条**它自己清不掉**的记录天天响，
    下一个人学会忽略它（"哨兵一响就没人看了"）。

锁住的四类：

  ① 三态分列（present / absent / unreadable）——`读不到` ⛔ 不是 `没有`
  ② 残留按**前缀**认（与清场 SQL 的 `LIKE 'agent_fixture_%'` 同一判据）
  ③ `--verify` 的退出码 0/1/2 与行标（`[fixture-leftover]` / `[fixture-check-failed]`）
  ④ **本模块里没有第二条写通道**（源码扫描：凭据读取 / 写方法 / protected 路径）——
     扫描器本身带牙齿（5 条样本，含注释行与 noqa 行）
  ⑤ 用例文件侧的机械不变量：每条 `requires_fixture` 的名字必须带保留前缀（否则清场
     覆盖不到它、"结构上不可能误删真数据"这条性质就没了）；每条 `needs_real_write`
     用例必须声明 `requires_fixture`（真写却没说目标是谁 = 无界）。

无网络（HTTP 那条路径一次都不走：全部走注入的 `titles`）、无 LLM、秒级。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/ 下）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import golden_fixture as gf  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(name)


print("① 三态分列：读不到不是没有")
check("清单里有 → present", gf.fixture_state("agent_fixture_category_a",
                                             ["编程", "agent_fixture_category_a"]) == "present")
check("清单里没有 → absent", gf.fixture_state("agent_fixture_category_a", ["编程"]) == "absent")
check("读不到（titles=None）→ unreadable",
      gf.fixture_state("agent_fixture_category_a", None) == "unreadable")
check("absent 与 unreadable 是两个答案（前者是事实，后者是不知道）",
      gf.fixture_state("x", ["y"]) != gf.fixture_state("x", None))
# 名字**不是**夹具时也不该因为"名字像"而误判——判据是相等，不是包含
check("只认整名相等（前缀相同但不是它 ⇒ absent）",
      gf.fixture_state("agent_fixture_category_a",
                       ["agent_fixture_category_ab"]) == "absent")

print("\n② 残留按前缀认（与清场 SQL 同一判据）")
_t = ["编程", "agent_fixture_x", "agent_fixture_y", "我 agent_fixture_z", "AGENT_FIXTURE_w"]
check("前缀族全部挑出来", gf.leftovers(_t) == ["agent_fixture_x", "agent_fixture_y"],
      str(gf.leftovers(_t)))
check("名字中间出现该串的不算残留（清场删不掉它 ⇒ 哨兵不该报它）",
      "我 agent_fixture_z" not in gf.leftovers(_t))
check("大小写不同的不算（SQL 的 LIKE 在默认排序规则外不做大小写折叠，两边判据要一致）",
      "AGENT_FIXTURE_w" not in gf.leftovers(_t))
check("没有残留 → 空列表（不是 None：清单读到了，就是没有）", gf.leftovers(["编程"]) == [])

print("\n③ --verify 的退出码与行标")
check("干净 → 0", gf.verify(["编程"])[0] == 0)
_c, _lines = gf.verify(["编程", "agent_fixture_a"])
check("有残留 → 1", _c == 1)
check("残留行带 [fixture-leftover] 且点了名",
      all(gf.LEFTOVER_TAG in ln for ln in _lines) and any("agent_fixture_a" in ln for ln in _lines),
      str(_lines))
_c, _lines = gf.verify(None)
check("读不到 → 2", _c == 2)
check("读不到的行带 [fixture-check-failed] 且写明不等于没有",
      gf.UNREADABLE_TAG in _lines[0] and "不等于" in _lines[0], _lines[0])
check("残留优先于干净（两件事不会互相掩盖）", gf.verify(["agent_fixture_a"])[0] == 1)

print("\n④ 本模块里没有第二条写通道（源码扫描）")
_SRC = (ROOT / "eval/golden_fixture.py").read_text(encoding="utf-8")
# 扫描的是**代码**，不是注释/文档：本模块的头注里写着"不读凭据、不做写"这些字，
# 把整份文本拿去扫会自伤（而且是在注释里判红，下一个人只会把注释删掉）。
# 朴素剥注释：本文件不含带 `#` 的字符串字面量（noqa 之类都是行尾注释，扫到的样本
# 见本节末尾）——够用的扫描器不是解析器，它拦的是手滑，不是恶意。
_WRITE_PATTERNS = (
    "os.environ", "getenv",              # 凭据/环境（真写通道会从这里开始）
    "/api/protected",                    # 后台写接口前缀
    "Authorization", "_sign_local_jwt",  # 带身份去调（写工具的形状）
    ".post(", ".put(", ".delete(", ".request(", ".patch(",
)


def scan(text: str) -> list[str]:
    hits = []
    for ln in text.splitlines():
        code = ln.split("#", 1)[0]
        hits += [p for p in _WRITE_PATTERNS if p in code]
    return hits


_tool = scan(_SRC)
check("模块里没有凭据读取 / 写方法 / protected 路径", not _tool, f"命中：{sorted(set(_tool))}")
# 扫描器有牙齿：拿 5 条样本喂它，命中与不命中都要符合预期（不然"没命中"可能只是它瞎了）
_SAMPLES = [
    ('resp = _client.post(f"{url}/x", json=payload)', True),
    ('uid = os.environ.get("GOLDEN_ADMIN_UID")', True),
    ('headers = {"Authorization": "Bearer " + tok}', True),
    ('    # 顺手 .delete( 一下会怎样', False),        # 注释：不算
    ('from tools.base import _get  # noqa: E402', False),
]
for _s, _want in _SAMPLES:
    _got = bool(scan(_s))
    check(f"扫描器样本：{('命中' if _want else '不命中')} ← {_s.strip()[:44]}", _got == _want)
check("扫描器对本模块只说没命中（不是因为它什么都扫不到）",
      bool(scan("import subprocess")) is False and bool(scan("x.post(1)")) is True)

print("\n⑤ 用例侧的不变量（机械守着前缀这条性质）")
import json  # noqa: E402

_CASES = [json.loads(ln) for ln in
          (ROOT / "eval/golden/basic.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
_fix = [(c["id"], c["requires_fixture"]) for c in _CASES if c.get("requires_fixture")]
check("有声明 requires_fixture 的用例（本键不是摆设）", bool(_fix), str(_fix))
check("每条 requires_fixture 的名字都带保留前缀（否则清场 SQL 覆盖不到它）",
      all(n.startswith(gf.FIXTURE_PREFIX) for _, n in _fix),
      str([n for _, n in _fix if not n.startswith(gf.FIXTURE_PREFIX)]))
_write = [c["id"] for c in _CASES if c.get("needs_real_write")]
check("每条 needs_real_write 用例都声明了 requires_fixture（真写不许无界）",
      all(c.get("requires_fixture") for c in _CASES if c.get("needs_real_write")),
      str([c["id"] for c in _CASES
           if c.get("needs_real_write") and not c.get("requires_fixture")]))
# 真写用例必须同时要真身份：uid=0 是哨兵，写不动 ⇒ 用例会红成一个误导性的"没反应"
check("每条 needs_real_write 用例都要真身份（needs_admin_uid 或 needs_user_uid）",
      all(c.get("needs_admin_uid") or c.get("needs_user_uid") for c in _CASES
          if c.get("needs_real_write")),
      str(_write))

print("\n⑥ CLI 入口")
check("不给 --verify → 用不出来的用法（2，不是 0）", gf.main([]) == 2)
_old = gf.category_titles
gf.category_titles = lambda: ["编程"]      # 注入：这一节一次网络都不走
try:
    check("--verify 干净 → 0", gf.main(["--verify"]) == 0)
    gf.category_titles = lambda: None
    check("--verify 读不到 → 2", gf.main(["--verify"]) == 2)
    gf.category_titles = lambda: ["agent_fixture_a"]
    check("--verify 有残留 → 1", gf.main(["--verify"]) == 1)
finally:
    gf.category_titles = _old

print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败 ===")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("=== 全部通过 ===")
