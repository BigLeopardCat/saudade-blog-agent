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
# 扫描的是**代码**，不是注释/文档：这两个模块的头注里正写着"不读凭据、不做写""只允许
# GET"这些字，把整份文本拿去扫会**自伤**（判红在自己写的说明文字上，下一个人只会把
# 说明删掉——纪律就从"被扫出来的"变成"不许提的"）。所以剥两样：行尾 `#` 注释 + docstring。
# 够用的扫描器不是解析器（它拦手滑、不拦恶意），但"提都不能提"是纯粹的自伤，值得剥干净。
# 写形状只写一处：账号族（⑦c）**非带身份不可**，它要被扫的清单是"写动词 + 本端点特有的
# 形状"，而"动词"这半必须与分类族逐字同源——抄一份过去，下一个动词出现时就漏掉一边。
_WRITE_VERBS = (".post(", ".put(", ".delete(", ".patch(", ".request(")
_WRITE_PATTERNS = (
    "os.environ", "getenv",              # 凭据/环境（真写通道会从这里开始）
    "/api/protected",                    # 后台写接口前缀
    "Authorization", "_sign_local_jwt",  # 带身份去调（写工具的形状）
) + _WRITE_VERBS


def _docstring_lines(text: str) -> set[int]:
    """模块/类/函数 docstring 覆盖的行号（1 起）。解析不了 ⇒ 当没有。

    片段样本（`'x.post(1)'` 那种）本来就不是合法模块，落进这个分支正好——它们要的是
    "照原样扫一遍"。
    """
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        head = body[0] if body else None
        if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)):
            out.update(range(head.lineno, (head.end_lineno or head.lineno) + 1))
    return out


def scan(text: str, patterns: tuple = _WRITE_PATTERNS) -> list[str]:
    skip = _docstring_lines(text)
    hits = []
    for i, ln in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        code = ln.split("#", 1)[0]
        hits += [p for p in patterns if p in code]
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
# 剥 docstring 这一层要有牙齿：说明文字里提到写形状**不算**（否则纪律变成"不许提"），
# 而同样这段字出现在**代码**里必须命中（否则这一层就成了新的藏身处）。
_DOC_FRAG = '"""本模块只用 _get，不做 .post( /api/protected。"""\nx = 1\n'
check("docstring 里提到的写形状不判红（剥掉的是说明，不是判据）", not scan(_DOC_FRAG))
check("同一段字当字符串常量赋值 ⇒ 照样命中（剥 docstring 没有顺手把代码也剥掉）",
      bool(scan('_DOC = "只用 _get，不做 .post("\n')))
check("函数 docstring 同样剥（不只模块头那一段）",
      not scan('def f():\n    """不做 .put(。"""\n    return 1\n')
      and bool(scan('def f():\n    """不做 .put(。"""\n    return _c.put(1)\n')))

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

print("\n⑦ 账号族夹具（eval/golden_fixture_account.py，20260926）")
import golden_fixture_account as gfa  # noqa: E402

_FIXROW = {"username": "agent_fixture_freeze_a", "status": 1, "role": "user", "id": 7}
_DIR_OK = {"agent_fixture_freeze_a": _FIXROW, "sora": {"username": "sora", "status": 0}}
check("行在且状态是期望态（冻结）→ present",
      gfa.fixture_state("agent_fixture_freeze_a", _DIR_OK) == "present")
check("名录里没有这个名字 → absent",
      gfa.fixture_state("agent_fixture_other", _DIR_OK) == "absent")
check("读不到名录（None）→ unreadable",
      gfa.fixture_state("agent_fixture_freeze_a", None) == "unreadable")
check("行在但状态是「正常」→ wrong_state（**这条就是那条假绿的守卫**：放它跑，"
      "后端走真 no-op 分支，回执照样生成、断言照样过）",
      gfa.fixture_state("agent_fixture_freeze_a",
                        {"agent_fixture_freeze_a": {"username": "x", "status": 0}}) == "wrong_state")
check("状态字段读不出来（缺字段）→ wrong_state（不是 present）",
      gfa.fixture_state("agent_fixture_freeze_a",
                        {"agent_fixture_freeze_a": {"username": "x"}}) == "wrong_state")
check("状态是脏值（非数字）→ wrong_state",
      gfa.fixture_state("agent_fixture_freeze_a",
                        {"agent_fixture_freeze_a": {"username": "x", "status": "?"}}) == "wrong_state")
check("expect_status 可显式传（默认值不是写死的判据：要正常态夹具的用例传 0 就 present）",
      gfa.fixture_state("agent_fixture_freeze_a", _DIR_OK, expect_status=0) == "wrong_state"
      and gfa.fixture_state("agent_fixture_freeze_a",
                            {"agent_fixture_freeze_a": {"username": "x", "status": 0}},
                            expect_status=0) == "present")
for _st in ("absent", "wrong_state", "unreadable"):
    _lb = gfa.state_label(_st, "agent_fixture_freeze_a")
    check(f"{_st} 的跳过原因是**可行动**的一句人话（点名要跑哪个 SQL / 先修哪条前置）",
          bool(_lb.strip()) and "agent_fixture_freeze_a" in _lb, _lb[:60])

print("\n⑦b 残留哨兵（与分类族有一处刻意的不对称：声明的夹具要放行）")
_DECL = ["agent_fixture_freeze_a"]
check("声明的那个夹具**不算**残留（它是常驻的：用例只改状态、不复位就得重跑 SQL）",
      gfa.leftovers(_DIR_OK, _DECL) == [], str(gfa.leftovers(_DIR_OK, _DECL)))
check("前缀族里**没声明**的才算残留（中断的 SQL / 手工插入 / 探针没清干净）",
      gfa.leftovers({**_DIR_OK, "agent_fixture_freeze_b": {"username": "agent_fixture_freeze_b",
                                                           "status": 1}}, _DECL)
      == ["agent_fixture_freeze_b"])
check("真账号（不带前缀）不报", gfa.leftovers({"sora": {}, "guest5": {}}, []) == [])
check("名字中间出现前缀的账号不报（清场 SQL 删不掉它 ⇒ 哨兵也不该报它）",
      gfa.leftovers({"我 agent_fixture_x": {}}, []) == [])
check("大小写不同的不报（与 SQL 的 LIKE 同一判据）",
      gfa.leftovers({"AGENT_FIXTURE_x": {}}, []) == [])
check("干净 → 退出码 0", gfa.verify(_DIR_OK, _DECL)[0] == 0)
_c, _l = gfa.verify({**_DIR_OK, "agent_fixture_z": {}}, _DECL)
check("有残留 → 1，且行带 [fixture-leftover] 点了名",
      _c == 1 and gfa.LEFTOVER_TAG in _l[0] and "agent_fixture_z" in _l[0], str(_l)[:120])
check("读不到 → 2，且行带 [fixture-check-failed] 写明不等于没有",
      gfa.verify(None, _DECL)[0] == 2 and gfa.UNREADABLE_TAG in gfa.verify(None, _DECL)[1][0]
      and "不等于" in gfa.verify(None, _DECL)[1][0])
check("不给 --verify → 用法（退出码 2，不是 0）", gfa.main([]) == 2)
_decl_real = gfa.declared_fixtures()
check("声明的账号夹具是**从用例文件派生**的（不是手抄名单）", _decl_real == ["agent_fixture_freeze_a"],
      str(_decl_real))

print("\n⑦c 账号族只有一条读通道（源码扫描，与分类族的纪律互为镜像）")
# 两半的清单**必须**不同，这不是偷懒：分类族（④）禁止的是"带身份"（那条路必须零凭据），
# 账号族**非带身份不可**（名录是管理员域接口，不带就读不到 ⇒ 真写用例永远静默跳过），
# 所以它的纪律只剩一条：**只有 GET**。把两族的清单写成同一份，等于逼其中一个让步——
# 共享的只该是"写动词"那半（`_WRITE_VERBS`），再加本端点特有的形状。
import os  # noqa: E402

_SRC_ACC = (ROOT / "eval/golden_fixture_account.py").read_text(encoding="utf-8")
_ACC_PATTERNS = _WRITE_VERBS + ('"POST"', '"PUT"', '"DELETE"', "method=",
                                "/status", "frozen", "subprocess", "os.system")
_hits = scan(_SRC_ACC, _ACC_PATTERNS)
check("账号族模块里没有任何写形状 / 端点特有的写形状（只有 GET）",
      not _hits, f"命中：{sorted(set(_hits))}")
for _s, _want in [
    ('_req = Request(url, data, headers={"Authorization": tok}, method="POST")', True),
    ('urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})', False),
    ('    # 顺手 .delete( 一下会怎样', False),          # 注释：不算（与 ④ 同一个剥注释规则）
    ('    return f"{BASE}/api/temp-users/{uid}/status"', True),
    ('if row.get("frozen"):', True),
]:
    check(f"账号族扫描样本：{'命中' if _want else '不命中'} ← {_s.strip()[:46]}",
          bool(scan(_s, _ACC_PATTERNS)) == _want)
check("身份那一半**必须在**（不在的话 directory() 恒 None ⇒ 真写用例永远静默跳过，"
      "而「跳过」看起来只是「前置没配好」、不像是这一族坏了）",
      "_sign_local_jwt" in _SRC_ACC and "Authorization" in _SRC_ACC)
check("'读不到'与'没有'在源码里就分得开（`if directory is None` 早退，不是 try 里吞异常）",
      "if directory is None" in _SRC_ACC)
# 行为上再验一次（比文本扫描硬）：没有 GOLDEN_ADMIN_UID 时**必须**在发请求之前就返回 None
_old_uid = os.environ.pop("GOLDEN_ADMIN_UID", None)
try:
    check("没设 GOLDEN_ADMIN_UID ⇒ directory() 返回 None（不发那次注定被拒的请求）",
          gfa.directory() is None)
finally:
    if _old_uid is not None:
        os.environ["GOLDEN_ADMIN_UID"] = _old_uid

print("\n⑧ 夹具闸（两族共用一个实现，两个跑法调用同一个函数）")
_GATE_CASES = [
    {"id": "c1", "requires_fixture": "agent_fixture_category_a"},
    {"id": "c2", "requires_fixture": "agent_fixture_freeze_a", "requires_fixture_kind": "account"},
    {"id": "c3", "requires_fixture": "agent_fixture_category_b"},
    {"id": "c4"},
]
_SNAPS = {"category": ["编程"],
          "account": {"agent_fixture_freeze_a": _FIXROW}}
_old_snap = gf.snapshot
gf.snapshot = lambda kind: _SNAPS[kind]
try:
    _kept, _drop, _lines = gf.gate(list(_GATE_CASES))
    check("在位的那条留着、不在位的摘掉、没声明的原样通过",
          [c["id"] for c in _kept] == ["c2", "c4"], str([c["id"] for c in _kept]))
    check("被摘掉的 id 按声明逐个报出来（计入分母变化）", _drop == ["c1", "c3"], str(_drop))
    check("不写 kind = 分类族（存量用例的语义一个字都没变）",
          "c1" in _drop and "c2" not in _drop)
    # 第二遍：两类夹具**分别在位/不在位**——每一族的理由各说各的（分类族指公开列表，
    # 账号族指复位 SQL）。只跑一遍"账号夹具在位"是看不到账号族那句话的。
    gf.snapshot = lambda kind: {"category": ["agent_fixture_category_a"],
                                "account": {"agent_fixture_freeze_a": {"status": 0}}}[kind]
    _kept2, _drop2, _lines2 = gf.gate(list(_GATE_CASES))
    _cat_ln = [ln for ln in _lines2 if ln.startswith("[skip] c3")]
    _acc_ln = [ln for ln in _lines2 if ln.startswith("[skip] c2")]
    check("夹具行在但状态不对 ⇒ 不可用（不是「在位」）—— 这就是那条假绿的守卫",
          _drop2 == ["c2", "c3"] and [c["id"] for c in _kept2] == ["c1", "c4"],
          str((_drop2, [c["id"] for c in _kept2])))
    check("两族的跳过理由各说各的（分类族指公开列表，账号族指复位 SQL）",
          bool(_cat_ln) and "公开分类" in _cat_ln[0]
          and bool(_acc_ln) and "复位" in _acc_ln[0], str(_lines2)[:200])
    # 快照按 kind 只取一次：两条分类用例共用一份（省一次往返，也保证看到同一份清单）
    _calls: list[str] = []
    gf.snapshot = lambda kind: (_calls.append(kind), _SNAPS[kind])[1]
    gf.gate(list(_GATE_CASES))
    check("同族夹具只取一次快照（同一批用例看到同一份）", _calls.count("category") == 1,
          str(_calls))
    # 未知 kind 必须**响亮报错**：拼错一个字母会让闸去查另一族 ⇒ 用例静默跳过，
    # 而打印出来的理由是"那一族里没有它"（读的人只会去查那一族）。
    gf.snapshot = _old_snap
    try:
        gf.gate([{"id": "c9", "requires_fixture": "agent_fixture_x",
                  "requires_fixture_kind": "acount"}])
        check("未知 kind → 响亮报错（不是退回分类族）", False, "没报错")
    except SystemExit as e:
        check("未知 kind → 响亮报错（不是退回分类族）", "acount" in str(e), str(e)[:80])
except Exception as e:  # noqa: BLE001
    check(f"夹具闸的用例集跑通（异常：{type(e).__name__}: {e}）", False)
finally:
    gf.snapshot = _old_snap

# 用例文件侧：kind 的取值域受控（新写一个 kind 而没登记 ⇒ 红在这里，而不是运行期）
_kinds = {(c["id"], str(c.get("requires_fixture_kind") or "category"))
          for c in _CASES if c.get("requires_fixture")}
check("用例里出现的每个 requires_fixture_kind 都在 FIXTURE_KINDS 里",
      all(k in gf.FIXTURE_KINDS for _, k in _kinds), str(sorted(_kinds)))
_acc_cases = [cid for cid, k in _kinds if k == "account"]
check("账号族夹具用例真的在文件里（这条闸不是空转的）", bool(_acc_cases), str(_acc_cases))
check("账号族用例声明的名字与 declared_fixtures() 同源（哨兵要放行它，名字只能有一处来源）",
      set(gfa.declared_fixtures()) == {c["requires_fixture"] for c in _CASES
                                       if c.get("requires_fixture")
                                       and str(c.get("requires_fixture_kind") or "") == "account"},
      str(gfa.declared_fixtures()))

print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败 ===")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("=== 全部通过 ===")
