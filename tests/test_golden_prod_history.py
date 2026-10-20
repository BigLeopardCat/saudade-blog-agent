"""`GOLDEN_PROD_HISTORY` 注入进去的，必须是**能被读的历史**（20261016 批 3）。

## 这个文件是为什么写的

批 3 要量的那一刀是「图自持上下文 ⇒ `server._build_messages` 停止注入 `req.history[-20:]`」。
可**语料测不出它**：`eval/golden/basic.jsonl` 里 11 条多轮用例**一条都不注入 `history`**
（Rust 每轮真会发，golden 是进程内驱动所以省了）。于是加了 `GOLDEN_PROD_HISTORY=1`：
多轮用例第 i>1 轮的 `history` = 第 1..i−1 轮**真发出去的那两句**，与 Rust `history[-20:]`
逐字同形——四臂跑同一份驱动，对照才同源。

**第一版写错了，而且错法很贵**：`_prod_hist` 攒的是裸 dict
（`{"role": "user", "content": …}`，因为它长得就像 Rust 发过来的那份 JSON），
赋值给 `req.history` 时 **pydantic 不校验**——`ChatRequest` 没开 `validate_assignment`，
而 `list[HistoryItem]` 的强制转换只发生在**构造时**。于是：

    AttributeError: 'dict' object has no attribute 'role'   （server.py:684）

**症状与病因完全不成比例**：异常在**主线程**抛出、traceback 也打出来了，但进程
**不退出**——挂满 15 分钟被 `subprocess` 的超时杀掉，看起来像"服务商把连接挂了"
（当时真的去查了 socket 状态：CLOSE-WAIT 那条是无关的旧连接）。而臂 B 在批 0 跑过
同一条用例 3/3、每次约 9 秒 ⇒ 差异只在"这一格有没有开"。

所以本文件的判据**不是**"源码里有 `HistoryItem(` 这个字面"（那种锁在本仓被点名过：
锁的落点要选判据别选字面），而是**把 `run_case` 真驱动起来**（`run_one` 换桩，
零 LLM、零网络、零报告），逐轮问它：**注入进去的每一条，读得出 `.role` / `.content` 吗**。
最后再用**真的** `_build_messages` 走一遍——那正是当初炸掉的那一行。

## 反向对照（缺了它前一格可能恒绿）

`GOLDEN_PROD_HISTORY` **关**的时候，多轮用例每一轮的 `history` 必须**都是空**——
否则"注入生效"与"语料本来就有 history"分不开，而那正是这一格存在的理由。

秒级、无网络、无 LLM；由 eval.yml 在 push 时跑。

用法：.venv/bin/python tests/test_golden_prod_history.py
"""


import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import run_golden as rg  # noqa: E402

from server import HistoryItem, _build_messages  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


CASE_ID = "mt3_detour_then_return_ledger_value"


def _load_case() -> dict:
    path = ROOT / "eval/golden/basic.jsonl"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            if c["id"] == CASE_ID:
                return c
    raise AssertionError(f"语料里没有 {CASE_ID}（用例文件换过？本文件的前提就没了）")


def _stub_result(text: str) -> dict:
    """`run_one` 的桩：`run_case` 会读的键一个都不能少。

    少一个键报的是 `KeyError`，读起来像"环境坏了"，而不是"那条断言没测着"——
    同 `tests/test_golden_runner_parity.py::_stub_result` 的老教训。
    """
    return {"text": text, "commands": [], "tool_calls": [], "rounds": [],
            "exec_rows": [], "exec_tools": [], "tool_rounds": 0, "trace": None,
            "resets": 0, "resets_reasons": [], "reset_scopes": [],
            "fallback_reasons": [], "confirm_tokens": [], "confirm_payloads": [],
            "error": None}


def drive(case: dict, prod_history: bool) -> list:
    """跑一遍 `run_case`，返回**每一轮真正发给 `run_one` 的那个 `req`**。

    `run_one` 换桩（零 LLM）：本文件问的是"历史摆对了没有"，不是"模型答得对不对"。
    """
    seen: list = []
    saved = rg.run_one

    def _run_one(req, principal, **kw):
        seen.append(req)
        # 回复文本随轮次变，用来验证"攒的是**这一轮真发出去**的那句"
        return _stub_result(f"第 {len(seen)} 轮的回复")

    rg.run_one = _run_one
    key = rg.ENV_PROD_HISTORY
    saved_env = os.environ.get(key)
    try:
        if prod_history:
            os.environ[key] = "1"
        else:
            os.environ.pop(key, None)
        rg.run_case(case)
    finally:
        rg.run_one = saved
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env
    return seen


def main() -> int:
    case = _load_case()
    rounds = rg.iter_rounds(case)

    print("── ① 开档：注入的是**读得动**的历史（就是当初炸掉的那一格）")
    reqs = drive(case, prod_history=True)
    check(f"跑出了 {len(rounds)} 轮（用例的轮数没被丢掉）",
          len(reqs) == len(rounds), f"拿到 {len(reqs)} 轮")
    check("第 1 轮不注入（`_build_messages` 那一轮本来就只有 System 块 + 当前问题）",
          list(reqs[0].history) == [], f"{reqs[0].history!r}")
    r2 = list(reqs[1].history)
    check("第 2 轮注入了上一轮的那一对问答（user + assistant）",
          len(r2) == 2, f"拿到 {len(r2)} 条：{r2!r}")
    check("**注入的每一条都读得出 `.role` / `.content`**（裸 dict 在这里当场 AttributeError）",
          all(isinstance(h, HistoryItem) for h in r2),
          "、".join(type(h).__name__ for h in r2))
    check("类型也对（不能只 duck-type 过 `.role` 就把 str 混进来）",
          [h.role for h in r2] == ["user", "assistant"],
          f"{[getattr(h, 'role', None) for h in r2]}")
    check("第 2 轮注入的 user 文本 = **第 1 轮真发出去的那句**",
          r2[0].content == rounds[0]["user_input"], repr(r2[0].content)[:80])
    check("第 2 轮注入的 assistant 文本 = **第 1 轮最终的回复**",
          r2[1].content == "第 1 轮的回复", repr(r2[1].content)[:80])

    r3 = list(reqs[2].history)
    check("第 3 轮攒到 2 对（逐轮累加，不是只带上一轮）",
          len(r3) == 4 and [h.role for h in r3] == ["user", "assistant"] * 2,
          f"{[h.role for h in r3]}")
    check("第 3 轮的第 2 对来自第 2 轮（顺序是**时间序**，不是倒序）",
          r3[2].content == rounds[1]["user_input"] and r3[3].content == "第 2 轮的回复",
          f"{r3[2].content[:40]!r} / {r3[3].content[:40]!r}")

    print("\n── ② 真 `_build_messages` 走一遍（那正是当初炸掉的那一行）")
    try:
        msgs = _build_messages(reqs[1])
        err = None
    except Exception as e:  # noqa: BLE001 —— 要的就是"什么异常都不许有"
        msgs, err = None, f"{type(e).__name__}: {e}"
    check("带注入历史的请求能造出消息表（`h.role` 那一行不再 AttributeError）",
          err is None, err or f"{len(msgs)} 条消息")
    check("上一轮那对问答真的进了消息表（不是被静默丢掉）",
          err is None and any(rounds[0]["user_input"] in str(getattr(m, "content", ""))
                              for m in msgs),
          "在消息表里找第 1 轮的 user 文本")

    print("\n── ③ 封顶：末尾 20 条（Rust `history[-20:]` 的形状）")
    many = {"id": "prod_history_cap_probe", "tags": ["multi-turn"],
            "context": {"conversation_id": 1},
            "rounds": [{"round": i + 1, "user_input": f"第 {i + 1} 问",
                        "context": {}, "gold": {"nonempty": True}}
                       for i in range(12)]}
    creqs = drive(many, prod_history=True)
    last = list(creqs[-1].history)
    check(f"12 轮（22 条）之后只剩 {rg.PROD_HISTORY_LIMIT} 条",
          len(last) == rg.PROD_HISTORY_LIMIT, f"拿到 {len(last)} 条")
    check("留下的是**最近**的那一段（表头是第 2 轮，不是第 1 轮）",
          last[0].content == "第 2 问", repr(last[0].content)[:40])

    print("\n── ④ 反向对照：关掉开关，多轮用例的每一轮都不许有注入历史")
    nreqs = drive(case, prod_history=False)
    check("关档时每一轮的 `history` 都是空的（否则「注入生效」与「语料自带」分不开）",
          all(list(r.history) == [] for r in nreqs),
          f"{[len(list(r.history)) for r in nreqs]}")

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条不通过：")
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✅ 全部通过：注入进去的历史是**读得动的**，关档时一条都不注入。")
    return 0


def test_golden_prod_history():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_golden_prod_history()
