"""Evaluation coordinates isolate cases, repetitions and reruns.

Check the default and injected turn runners by behavior. Remaining transport
checks ensure the chosen coordinate reaches the producer and trace recorder.
"""


from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "eval"))


import golden_thread as gt  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _no_rep(monkey: str = "") -> None:
    """把 `GOLDEN_REP` 拨到 `monkey`（空串 = 没设）。"""
    import os
    if monkey:
        os.environ[gt.ENV_REP] = monkey
    else:
        os.environ.pop(gt.ENV_REP, None)


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _check_turn_runner_coordinates():
    import run_golden as rg
    case = {"id": "coordinate_probe", "user_input": "hello", "gold": {}, "context": {}}
    received = []

    def turn(req, principal, **kwargs):
        received.append(kwargs["thread"])
        return {"text": "fixture", "confirm_tokens": []}

    expected = gt.for_case(case["id"], suffix="__probe")
    with patch.object(rg, "run_one", turn):
        rg.run_case(case, suffix="__probe")
        rg.run_case(case, suffix="__probe", turn_runner=turn)
    check("缺省与注入运行器都收到正确的会话坐标", received == [expected, expected],
          repr(received))


def test_golden_thread():
    print("① 三条不变式")
    _no_rep()
    _A = gt.for_case("case_a")
    check("同一个坐标 ⇒ 恒同一个名字（复位与写入对得上）",
          _A == gt.for_case("case_a"), _A)
    check("**不同用例 ⇒ 不同名字**（本件的本体：跨用例不串）",
          _A != gt.for_case("case_b"),
          f"{_A} / {gt.for_case('case_b')}")
    check("不同 suffix（重跑那一轮）⇒ 不同名字",
          _A != gt.for_case("case_a", suffix="__rerun"),
          f"{_A} / {gt.for_case('case_a', suffix='__rerun')}")
    _no_rep("3")
    check("不同遍（`GOLDEN_REP`）⇒ 不同名字（逐遍跑不许在上一遍的 state 上接着跑）",
          _A != gt.for_case("case_a"),
          f"{_A} / {gt.for_case('case_a')}")
    check("（对照）同一遍 ⇒ 还是同一个名字（上一条不是「凡设了 rep 就变」）",
          gt.for_case("case_a") == gt.for_case("case_a"))
    _no_rep()
    check("（对照）`GOLDEN_REP` 不设 ⇒ 名字里没有 `r` 段（手跑不分遍）",
          "_r" not in _A and not _A.endswith("_r"), _A)
    print()
    print("② 归一化：名字会原样进 PG 的 `thread_id` 列，两处必须是同一个字符串")
    check("`.` / `-` / 空格一律折成 `_`",
          gt.for_case("a.b-c d") == "golden_native_a_b_c_d",
          gt.for_case("a.b-c d"))
    check("空用例 id ⇒ `x`（不编一个像用例名的东西顶上）",
          gt.for_case("") == "golden_native_x",
          gt.for_case(""))
    check("名字里只有 `[0-9A-Za-z_]`（`DELETE ... WHERE thread_id = %s` 的两侧同一个串）",
          all(ch.isalnum() or ch == "_" for ch in gt.for_case("a.b-c d")),
          gt.for_case("a.b-c d"))
    print()
    print("③ 真语料上没有撞名（归一化会把不同的 id 折成同一个 slug，那是真实的失效面）")
    _cases = [json.loads(ln) for ln in
              (ROOT / "eval" / "golden" / "basic.jsonl").read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    _ids = [c["id"] for c in _cases]
    _names: dict[str, list[str]] = {}
    for _i in _ids:
        _names.setdefault(gt.for_case(_i), []).append(_i)
    _collide = {k: v for k, v in _names.items() if len(v) > 1}
    check(f"{len(_ids)} 条用例的线程名两两不同", not _collide,
        json.dumps(_collide, ensure_ascii=False))
    check("（反控）判据真的在比字符串：把两条 id 折成同一个 slug ⇒ 上一条必须能红",
          len({gt._slug("a.b"), gt._slug("a-b"), gt._slug("a b")}) == 1,
          f"{gt._slug('a.b')} / {gt._slug('a-b')} / {gt._slug('a b')}")
    print()
    print("④ 运行器收到正确坐标，生产者继续传递该坐标")
    _rg = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    _tree = ast.parse(_rg)
    _check_turn_runner_coordinates()
    _ro = _fn(_tree, "run_one")
    _submits = [n for n in ast.walk(_ro or ast.Module(body=[], type_ignores=[]))
                if isinstance(n, ast.Call) and _call_name(n.func) == "submit"]
    _sent = [ast.unparse(a) for n in _submits for a in n.args
             if "_run_agent_stream_to_queue" in ast.unparse(a)]
    check("找得到把活儿交给 `_run_agent_stream_to_queue` 的那次 `submit`（前提自己摆）",
          len(_submits) == 1 and len(_sent) == 1, "；".join(_sent) or "（没找到）")
    _stream_pos = [ast.unparse(a) for n in _submits for a in n.args]
    check("传给图的线程是**形参** `thread`（位置：messages 之后、queue 之前），不是字面",
          len(_stream_pos) >= 6 and _stream_pos[4] == "thread", "；".join(_stream_pos))
    check("文件里**不再**出现 `\"golden_thread\"` 这个字面常量（常量退场 = 本件的落点）",
          not [n for n in ast.walk(_tree)
               if isinstance(n, ast.Constant) and n.value == gt.LEGACY])
    check("`run_one` 的 `thread` 缺省仍是 `LEGACY`（探针调用方不走用例主循环，行为不许变）",
          "thread: str = golden_thread.LEGACY" in _rg)
    print()
    print("⑤ 接线锁：落进 trace 的 `thread_id` 是形参，不是写死的字面")
    _gtree = ast.parse((ROOT / "eval" / "golden_trace.py").read_text(encoding="utf-8"))
    _sc = _fn(_gtree, "start_case")
    _start = [n for n in ast.walk(_sc or ast.Module(body=[], type_ignores=[]))
              if isinstance(n, ast.Call) and _call_name(n.func) == "start_trace"]
    check("`start_case` 调 `start_trace` 时第三个实参是形参 `thread`",
          bool(_start) and len(_start[0].args) >= 3 and ast.unparse(_start[0].args[2]) == "thread",
          ast.unparse(_start[0].args[2]) if _start and len(_start[0].args) >= 3 else "（没找到）")
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_golden_thread()
