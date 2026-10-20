"""结构化载荷（20261019 批 G1）：**`ToolResult.data` 是本体，文本是它的渲染**。

**病**（本批要收的那一族）：`tools/result.py::_shape` 的出口此前是 `str(data)` ⇒
结构在**出口处**被丢掉，于是每一个想读结构的消费方都得把那个 repr **再解析回来**：

  · `agent/arg_refs.py::parse_data` 的 `literal_eval`（参数引用 `$x[0].y` 的取值源）
  · `agent/entity_digest.py::_parse`（跨轮实体摘要）
  · `agent/deterministic_plan.py::_doc_title` 的**正则**（跨轮指代锚点，实测会把
    `He said "hi"` 抠成 `He said `）

三处是同一个东西的三份实现，而结构**本来就在手上**——丢掉的只有"它在哪一格"。

**本套件锁什么**：出口的文本**逐字节不变**（所有 `str()`/切片/拼接的调用点一个字
不用改），同时本体多留一格 `data`；`structured_of` 读的是那一格，**不是**把文本再解
一遍。

**射程（写下来免得起假安心）**：
  · 本套件判的是**载体本身**（`_shape` / `ToolResult` / `structured_of` 的契约），
    不判"哪个消费方已经改用它了"——G3/G5 各自有自己那一套判据；
  · `parse_data` 的回落支**不是病**：真正只有文本的输入（trace 回放、测试直接喂
    repr 串）本来就没有本体可取，判别的是"有没有本体"而不是"本体坏了没有"。

用法：.venv/bin/python tests/test_structured_carrier.py
"""


import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from langchain_core.tools import tool  # noqa: E402

from agent.arg_refs import parse_data, structured_of  # noqa: E402
from tools.result import UPSTREAM_DOWN, ToolResult, _label_id_keys, _shape, ok  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


@tool
def _demo_carrier() -> str:
    """演示：一条走 `_shape` 出口的工具。"""
    return _shape([{"talkKey": 5, "content": "x"}])


def test_structured_carrier():
    FRAMES = [
        [{"talkKey": 101, "content": "第一条", "留名": "小舟"},
         {"talkKey": 102, "content": "第二条", "留名": ""}],
        {"data": {"records": [{"noteKey": 7, "noteTitle": "架构笔记"}]}},
        {"unread": 1, "items": [{"id": 3, "title": "公告"}]},
        [{"noteId": 1, "noteTitle": "It's a test"}],
        [],
        {},
        [{"talkId": 5, "content": "已经叫命名空间名的行"}],
    ]
    print("① 非真空：两条出口都取到了（取不到 ⇒ 下面每条都是空判据）")
    check("`_shape` / `structured_of` 都在", callable(_shape) and callable(structured_of))
    check("`ToolResult.data` 的缺省是 **None**（不是 `{}`——`{}` 会伪装成「有载荷，只是空的」）",
          ok("任意文本").data is None, repr(ok("任意文本").data))
    check("失败信封不带本体（`UPSTREAM_DOWN.data is None`）", UPSTREAM_DOWN.data is None)
    print("\n② ⭐ 出口的**文本逐字节不变**（这条红了 = 全仓 `str(out)` 的调用点都可能受影响）")
    for _d in FRAMES:
        _old = str(_label_id_keys(_d))          # 改前的表达式，逐字照抄
        check(f"`str(_shape({type(_d).__name__}))` == 改前的 `str(_label_id_keys(...))`",
              str(_shape(_d)) == _old, f"{str(_shape(_d))!r} vs {_old!r}")
    print("\n③ 本体留在 `.data` 里，且与文本**同源**（都是改过名的那份）")
    for _d in FRAMES:
        _s = _shape(_d)
        check(f"`_shape(...).data == _label_id_keys(...)`（{type(_d).__name__}）",
              _s.data == _label_id_keys(_d), repr(_s.data))
    check("kind 仍是 `ok`（`_check_spec` 的读法一个字不用改）",
          all(_shape(_d).kind == "ok" for _d in FRAMES))
    print("\n④ ⭐ 与**老路对拍**：今天 `structured_of` 与 `parse_data(str(out))` 必须给同一个东西")
    for _d in FRAMES:
        _s = _shape(_d)
        check(f"`structured_of(out) == parse_data(str(out))`（{type(_d).__name__}）",
              structured_of(_s) == parse_data(str(_s)),
              f"{structured_of(_s)!r} vs {parse_data(str(_s))!r}")
    print("\n⑤ 反控：**文本与本体故意不一致** ⇒ `structured_of` 跟本体走")
    _FAKE = ToolResult('[{"noteId": 1}]', "ok", None, [{"noteId": 999}])
    check("`structured_of` 给出的是**本体**那个值", structured_of(_FAKE) == [{"noteId": 999}],
          repr(structured_of(_FAKE)))
    check("反控的反面：同一份输入，**文本**那条路给的是另一个答案（两条路真的分得开）",
          parse_data(str(_FAKE)) == [{"noteId": 1}], repr(parse_data(str(_FAKE))))
    print("\n⑥ 反控：**没有本体**的裸文本 ⇒ 回落 `parse_data`（这是另一类输入，不是兜底容错）")
    check("裸 repr 串仍解析得出", structured_of('[{"noteId": 4}]') == [{"noteId": 4}])
    check("裸人话串仍解析不出（返回 None，调用方据此报 ref_unparsed，不猜）",
          structured_of("当前用户还没有绑定任何 IoT 设备") is None)
    print("\n⑦ 身份透传：`_shape` 已经是 `ToolResult` 时**按身份原样返回**")
    check("`_shape(UPSTREAM_DOWN) is UPSTREAM_DOWN`", _shape(UPSTREAM_DOWN) is UPSTREAM_DOWN)
    print("\n⑧ ⭐ `.invoke()` 之后本体仍在（与 `kind` 同一条生命线）")
    _out = _demo_carrier.invoke({})
    check("`invoke()` 的返回值是 `ToolResult`", isinstance(_out, ToolResult), type(_out).__name__)
    check("`.kind` 透传（既有契约）", getattr(_out, "kind", None) == "ok")
    _d_out = getattr(_out, "data", None)
    check("`.data` 透传（本批新加的那一格）",
          _d_out == [{"talkId": 5, "content": "x"}], repr(_d_out))
    check("`str()` 仍是那个渲染串（Python repr：`talkKey` 已在出口改成 `talkId`）",
          str(_out) == "[{'talkId': 5, 'content': 'x'}]", str(_out))
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_structured_carrier()
