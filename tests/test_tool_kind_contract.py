"""工具返回值契约的机器锁：**「没声明 kind」与「声明了 ok」在消费侧长得一样**（20261019）。

**病**（`tools/result.py` 的 `ToolResult` 契约只写在注释里，没有机器看着它）：

  工具的返回值应当是**信封**——`ok` / `empty` / `unavailable` / `not_found` 四取一。
  但消费侧那一行是

      getattr(out, "kind", "ok")            # agent/graph.py::execute_node

  ⇒ 一个**裸字符串**（压根没声明 kind）读出来是 `"ok"`，与真的声明了 `ok` **逐字相同**。
  于是"没做成"被 `_check_spec` 判 PASS ⇒ 产回执 ⇒ 落 `execution_log` ⇒ 下一轮
  `recent_executions` 把它注回提示词 ⇒ planner/narrator 读成**系统确认的事实**。

  实测（20261019，`_check_spec` 真跑）：

      device_oled_display 设备离线（409）⇒ 裸串 ⇒ kind='ok' ⇒ PASS ⇒ 回执
      ⇒ Rust `legacy_exec_row` 渲染成「屏幕显示「你好」」   ← 设备离线，什么都没显示

  这不是"某人可能写错"，是**已经写错了**：`get_weather` 把三条失败路径里的**两条**
  包了 `unavailable`、第三条（`except` 那条）漏了；`device_oled_display` 包了一条
  `empty`、漏了八条。同一函数里两种写法并存 ⇒ 是疏忽，不是设计。

**本套件锁什么**：`tools/*.py` 里每个 `@tool` 函数的**裸文本返回**必须**登记在案**，
登记要写明它是 `success_only`（只出现在成功路径上——默认 `ok` 恰好正确）还是
`failure`（出现在"没做成"的路径上——**这是病**，必须带 `sunset`）。集合**双向**：
漏登记一条红，台账里留一条已经不裸的也红。

**射程（写下来免得起假安心）**：
  · **只认"纯文本构造"**——字符串字面量 / f-string / `+` 拼接 / `.join` `.strip` 这类
    文本方法。`return data`、`return _helper(...)` 这种**委托返回**不在射程内：它们的
    运行时类型静态判不出来，而它们绝大多数（`_admin_post` / `_pre_read_fail` / `guard` /
    `fail`）确实交回信封。⇒ 本套件**保证不了**"没有别的裸返回"，它保证的是
    "**明摆着的**那些一条都躲不掉"。
  · **不判文案**：台账里那句 `why` 是给人读的，不是判据。
  · **不判 `_check_spec` 的缺省**：`getattr(…, "kind", "ok")` 是病根，但它是**一个**位置，
    改它（或改成 `isinstance` 判定）是另一件事，不在本套件里。

**为什么不是"直接禁止裸返回"**：9 件工具里 6 件是 `success_only`（站点地图、固定告知语、
时间），把它们的成功返回包一层 `ok()` 是纯噪音——默认 `ok` 在**成功路径**上是对的。
判据要的是"**失败路径**不许裸返回"，而"这一条在不在失败路径上"静态判不出来 ⇒ 由人登记、
由机器守登记（与 `eval/judge_registry.json` 同一形状：不确定就填病那一侧）。

用法：.venv/bin/python tests/test_tool_kind_contract.py
"""


from __future__ import annotations

import ast
import pathlib
import sys

from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


FAILED: list[str] = []
check = collector(FAILED)


_TEXT_METHODS = {"join", "strip", "format", "replace", "lower", "upper",
                 "lstrip", "rstrip", "split", "strip_ws"}


def _is_bare_text(v: ast.expr) -> bool:
    """这一句 `return` 交出去的是不是**裸文本**。"""
    if isinstance(v, ast.Constant) and isinstance(v.value, str):
        return True
    if isinstance(v, ast.JoinedStr):                 # f-string
        return True
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
        return True
    if isinstance(v, ast.Call) and getattr(v.func, "attr", None) in _TEXT_METHODS:
        return True
    return False


def _tool_functions(tree: ast.Module):
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {getattr(d, "id", None) or getattr(getattr(d, "func", None), "id", None)
                 for d in fn.decorator_list}
        if "tool" in names:
            yield fn


def offenders(src: str, path: str) -> dict[str, list[int]]:
    """`{工具名: [裸文本 return 的行号…]}`。**判据只吃源码文本** ⇒ 正/反控能喂合成片段。"""
    out: dict[str, list[int]] = {}
    for fn in _tool_functions(ast.parse(src)):
        hits = [r.lineno for r in ast.walk(fn)
                if isinstance(r, ast.Return) and r.value is not None
                and _is_bare_text(r.value)]
        if hits:
            out[f"{path}::{fn.name}"] = sorted(hits)
    return out


def _emitted(src: str) -> set[str]:
    """`tools/result.py` 里能造出来的 kind（`ToolResult(<文本>, "<kind>"` 的第二参字面量）。"""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ToolResult" \
           and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
           and isinstance(n.args[1].value, str):
            out.add(n.args[1].value)
    return out


def _handled(src: str) -> set[str]:
    """`_check_spec` 里与 `kind` 比过的字面量（`kind == "x"` 与 `kind in ("x", …)`）。"""
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "_check_spec"), None)
    out: set[str] = set()
    for n in ast.walk(fn) if fn else []:
        if not (isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                and n.left.id == "kind"):
            continue
        for c in n.comparators:
            for x in (c.elts if isinstance(c, ast.Tuple) else [c]):
                if isinstance(x, ast.Constant) and isinstance(x.value, str):
                    out.add(x.value)
    return out


def test_tool_kind_contract():
    _ENVELOPES = {"ok", "empty", "unavailable", "not_found", "ToolResult", "fact", "tgt", "_shape"}
    _FOUND: dict[str, list[int]] = {}
    _N_TOOLS = 0
    for _p in sorted((ROOT / "tools").glob("*.py")):
        _src = _p.read_text(encoding="utf-8")
        _N_TOOLS += sum(1 for _ in _tool_functions(ast.parse(_src)))
        _FOUND |= offenders(_src, f"tools/{_p.name}")
    LEDGER: dict[str, dict[str, str]] = {
        # ★ 批 ⑤-1（20261019）收掉的两条**病**，留在注释里当史：`device_oled_display` 的
        # 8 条硬失败（身份/参数/404/409/500）与 `get_weather` 的 `except` 那条此前都是裸串
        # ⇒ kind 缺省成 `ok` ⇒ PASS ⇒ 假回执（「屏幕显示「…」」/「查看天气「…」」）。
        # 台账是**双向**的：它们包好之后必须从下表删掉，否则本套件自己红——这正是要的行为。
        "tools/site.py::navigate_to": {
            "kind": "covered",
            "sunset": "",
            "why": "参数非法那条是裸串，但它**被别的闸兜住了**：`navigate_to` 在 `_CMD_KINDS` 里，"
               "没带 `meta['cmd']` ⇒ `_check_spec` 判 BLOCK `cmd_shape` ⇒ 不进回执。"
               "行为对，只是原因码说的是「返回格式异常」而不是「参数不合法」"},
        "tools/site.py::toggle_effect": {"kind": "covered", "sunset": "",
                                         "why": "同上（两条参数校验路径）"},
        "tools/site.py::toggle_dark_mode": {"kind": "covered", "sunset": "",
                                            "why": "同上（一条参数校验路径）"},
        "tools/device.py::list_devices": {
            "kind": "success_only", "sunset": "",
            "why": "设备清单的行渲染，只有成功一条路（拉不到列表走 `_get` 的 `UPSTREAM_DOWN`）"},
        "tools/site.py::get_site_map": {"kind": "success_only", "sunset": "",
                                        "why": "站内结构常量，没有失败路径"},
        "tools/site.py::get_weather": {
            "kind": "success_only", "sunset": "",
            "why": "批 ⑤-1 收掉了 `except` 那条（改判 `unavailable`）之后，只剩成功那一句"
               "`f\"{loc}天气: …\"`；三条失败路径现在全走信封"},
        "tools/site.py::search_knowledge_base": {
            "kind": "success_only", "sunset": "",
            "why": "命中拼接那一支；空结果与故障各走 `empty` / `unavailable`（同一函数里已包）"},
    }
    print("① 非真空：真的扫到了工具（扫不到 ⇒ 下面每条都是空判据）")
    check("从 `tools/*.py` 取到的 `@tool` 函数数 ≥ 60", _N_TOOLS >= 60, f"{_N_TOOLS} 件")
    check("取到了裸文本返回（今天非空；哪天清零了这条也要跟着改）", bool(_FOUND),
          f"{len(_FOUND)} 件")
    print("\n② 台账双向：裸文本返回的工具集 == 台账（漏登记红、过期也红）")
    _missing = sorted(set(_FOUND) - set(LEDGER))
    _stale = sorted(set(LEDGER) - set(_FOUND))
    check("⭐ 每一件裸文本返回的 `@tool` 都登记在案（漏一件 = 一件**没人看过**的失败路径）",
          not _missing, "、".join(_missing))
    check("⭐ 台账不许过期：已经不裸的那几条要删掉（否则台账会慢慢变成一张没人读的清单）",
          not _stale, "、".join(_stale))
    print("\n③ 病那一侧必须带出路：`kind=failure` 的每一条都要写 `sunset`")
    _bad_sunset = lambda led: sorted(k for k, v in led.items()          # noqa: E731
                                     if v["kind"] == "failure" and not v.get("sunset"))
    _unknown = sorted(k for k, v in LEDGER.items()
                      if v["kind"] not in ("failure", "covered", "success_only"))
    check("⭐ 每条 `failure` 都写了收口批次",
        not _bad_sunset(LEDGER), "、".join(_bad_sunset(LEDGER)))
    check("类别只在闭集里（`failure` / `covered` / `success_only`）", not _unknown,
          "、".join(_unknown))
    check("反控：合成一份「failure 但没写 sunset」的台账 ⇒ 判据必须报出来",
          _bad_sunset({"x::y": {"kind": "failure", "sunset": "", "why": ""}}) == ["x::y"]
          and _bad_sunset({"x::y": {"kind": "failure", "sunset": "批 9", "why": ""}}) == [])
    _fail_n = sum(1 for v in LEDGER.values() if v["kind"] == "failure")
    print(f"     （今天：failure {_fail_n} 件 / covered 3 件 / success_only "
          f"{sum(1 for v in LEDGER.values() if v['kind'] == 'success_only')} 件）")
    print("\n④ 正控与反控：分类函数对合成片段的行为（不然上面全是空判据）")
    _ENV_SRC = '''
@tool
def demo(a: str) -> str:
    """演示。"""
    if not a:
        return unavailable("没有参数，本次未执行")
    if a == "x":
        return not_found("目标不存在")
    return ok("做成了", meta=fact("demo", changed=True, target=tgt("note", 1)))
'''
    _BARE_SRC = '''
@tool
def demo(a: str) -> str:
    """演示。"""
    try:
        return _do(a)
    except Exception as e:
        return f"做砸了: {e}"
'''
    check("正控：全走信封的片段 ⇒ 一条都不报", offenders(_ENV_SRC, "t.py") == {})
    check("反控：失败路径裸返回的片段 ⇒ 报出来（且行号指到那一句）",
          offenders(_BARE_SRC, "t.py") == {"t.py::demo": [8]},
          str(offenders(_BARE_SRC, "t.py")))
    check("反控：成功路径上的裸返回**也**会被报（本套件不静态区分成败 ⇒ 由人登记，"
      "这正是它要与台账配对的原因）",
          offenders('@tool\ndef d() -> str:\n    """x"""\n    return "常量"\n', "t.py")
          == {"t.py::d": [4]})
    print("\n⑤ 工具能声明的 kind ⊆ `_check_spec` 认得的 kind（防**新增 kind 静默落进 PASS**）")
    _PASS_BY_DESIGN = {
        "ok": "正常内容，本来就是 PASS",
        "empty": '"查到了、就是空的"本身是事实 ⇒ PASS 进回执（`_check_spec` 的 docstring 明写）',
    }
    _EMITTED = _emitted((ROOT / "tools" / "result.py").read_text(encoding="utf-8"))
    _BRANCHED = _handled((ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))
    _HANDLED = _BRANCHED | set(_PASS_BY_DESIGN)
    _unhandled = lambda e, h: sorted(e - h)                        # noqa: E731
    check("两边都取到了（取不到 ⇒ 下面那条是空判据）",
          len(_EMITTED) >= 4 and len(_HANDLED) >= 4,
          f"发出 {sorted(_EMITTED)} / 认得 {sorted(_HANDLED)}")
    check("⭐ 工具能声明的每个 kind，要么 `_check_spec` 有分支、要么在 `_PASS_BY_DESIGN` 里"
      "写了理由（漏一个 = 那个 kind **静默**当 ok ⇒ 假回执）",
          not _unhandled(_EMITTED, _HANDLED), "、".join(_unhandled(_EMITTED, _HANDLED)))
    check("`_PASS_BY_DESIGN` 与真分支**不许重叠**（同一个 kind 两处声明 ⇒ 改一处不改另一处）",
          not (set(_PASS_BY_DESIGN) & _BRANCHED), "、"
              .join(sorted(set(_PASS_BY_DESIGN) & _BRANCHED)))
    check("反控：合成的「没登记 kind」必须被报出来",
          _unhandled({"ok", "幽灵_kind"}, _HANDLED) == ["幽灵_kind"])
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_tool_kind_contract()
