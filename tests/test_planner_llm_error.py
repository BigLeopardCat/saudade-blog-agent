# -*- coding: utf-8 -*-
"""决策 LLM 抛异常那条出口（`_llm_error_step`，20261015）的回归锁。

现场（`20261015T092916`，会话 347）：planner 撞了一次 503 ⇒ 确认兑现轮那条「台账里
还剩一件（发公告）」的纠偏**被静默吞掉** ⇒ narrator 只好顺着写成一句征询 ⇒ gate 打回
⇒ 重规划那一版把**两轮前就办完**的 `set_article_status(54)` 当成本轮任务 ⇒ 掏空出口 +
一句「这一轮没有做任何改动」。事后要回答「这一轮为什么零调用」只能去翻 `agent.log`：
这条出口在 trace 里**一个字都不写**（没有 `llm_done`、没有 `decision`）。

所以这个文件守三件事，一件都不是"文案对不对"：

① **判据是系统数据**（HTTP 状态码 + SDK 异常类名，全 ASCII），不是"主人这句话里有没有
   我认识的那个词"。这一条要**结构上**钉住：那两个集合与那个函数体里不许出现中文——
   一旦有人在里面补一句中文词表，"该不该重试"就从可核对的系统事实退化成词表判断，
   而词表判断在异常这一格上连输入都没有（异常对象身上没有主人原话）。
② **重试只限第一次尝试**。决策循环本来就有第二次尝试（`for _attempt in (0, 1)`），
   此前它只被剔空纠偏用。限第一次是**必要的**：第二次也失败时必须落到兜底，兜底才会
   给 `c.plan_obj` 赋值——否则循环耗尽尝试数退出来时 `plan_obj` 还是 `None`。
③ **无论走哪条路都先落一条 `planner.llm_error`**。这才是本次修的那个洞：一个不落事件的
   出口等于把"这一轮为什么零调用"从 trace 里抹掉。键集也要钉：`status` **只在异常身上
   真有状态码时出现**（缺键 ≠ 0——本仓的既有口径）。
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（套件统一住在 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as G  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_SRC = open(ROOT / "agent" / "graph.py", encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _func(name: str) -> ast.FunctionDef:
    for n in ast.walk(_TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise SystemExit(f"❌ 找不到 `{name}`——这个文件的判据都建立在它上面，别静默跳过")


_CJK = re.compile(r"[一-鿿]")


# ══════════════════════════════════════════════════════════════════
print("\n① 「该不该重试」读的是系统数据，不是词表")
# ══════════════════════════════════════════════════════════════════


class _Status(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.status_code = code


_cases = [
    (408, True, "请求超时（4xx 里少数几个「再问一遍就好」的）"),
    (409, True, "冲突"),
    (425, True, "太早"),
    (429, True, "限流"),
    (500, True, "服务端错误"),
    (502, True, "网关错误"),
    (503, True, "**现场那一次**（会话 347 撞的就是它）"),
    (400, False, "请求坏——重问一次还是坏"),
    (401, False, "鉴权不对，重问不会变对"),
    (403, False, "同上"),
    (404, False, "端点不对"),
    (422, False, "参数不对"),
]
_bad = [f"{c}: {G._llm_retriable(_Status(c))}（想要 {w}）"
        for c, w, _d in _cases if G._llm_retriable(_Status(c)) is not w]
check("逐码判：4xx 只有 408/409/425/429 可重试、5xx 全可重试",
      not _bad, str(_bad))

check("连接错误可重试（`APIConnectionError`）——它是**立刻返回**的那一族",
      G._llm_retriable(type("APIConnectionError", (Exception,), {})("x")) is True)
# ★ 超时**刻意不重试**：那一次调用自己的预算是 `planner_native_timeout`（60s），一次
#   超时已经把它花光了；再试一次最坏翻倍，而 SSE 的空闲窗口是 120s ⇒ 会把整轮回复丢掉。
check("★ 超时**刻意**不重试（一次超时已花光这份预算，再试一次会拖死整轮）",
      G._llm_retriable(type("APITimeoutError", (Exception,), {})("x")) is False)
check("  连「超时」这个词都不在表里——表里只有 `APIConnectionError`",
      set(G._RETRY_EXC) == {"APIConnectionError"}, str(set(G._RETRY_EXC)))
check("身上没有 `status_code`、类名也不在表里 ⇒ 不重试（默认不重试，fail-closed）",
      G._llm_retriable(ValueError("谁也不知道这是什么")) is False)
# 反控：状态码不是 int（有的 SDK 会给字符串）⇒ 当"没有状态码"处理，不许抛异常
check("  反控：`status_code` 是字符串 ⇒ 当没有处理（**不许**在这里炸）",
      G._llm_retriable(type("E", (Exception,), {"status_code": "503"})("x")) is False)

# 判据本身不许长成中文词表：**判据代码里的字符串常量**一个中文都不许有（文档串不算——
# 它是话术不是判据，同 `prompts.py` 那条边界）。这条要看 AST 而不是那段源码文本：
# 文本里当然有中文，全在解释"为什么超时不重试"的那段注里。
_rb = _func("_llm_retriable")
_doc = (_rb.body[0].value if _rb.body and isinstance(_rb.body[0], ast.Expr)
        and isinstance(_rb.body[0].value, ast.Constant) else None)
_cn_lits = [n.value for n in ast.walk(_rb)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n is not _doc and _CJK.search(n.value)]
check("★ 这个函数的**判据代码里没有中文常量**"
      "（补一句中文词表＝把可核对的事实换成猜测，而那一步连输入都没有）",
      not _cn_lits, str(_cn_lits)[:120])
_strs = {n.value for n in ast.walk(_rb)
         if isinstance(n, ast.Constant) and isinstance(n.value, str)}
check("  它读的两样东西就是「系统数据」本身：异常身上的 `status_code`（`getattr`）"
      "与类名 `__name__`",
      "status_code" in _strs or any(isinstance(n, ast.Attribute) and n.attr == "status_code"
                                   for n in ast.walk(_rb)),
      str(sorted(_strs)) + " attrs="
      + str(sorted({n.attr for n in ast.walk(_rb) if isinstance(n, ast.Attribute)})))
check("  两个集合的内容也是纯 ASCII 常量",
      all(re.fullmatch(r"[A-Za-z0-9_]+", x) for x in
          list(G._RETRY_EXC) + [str(x) for x in G._RETRY_HTTP]),
      str(sorted(G._RETRY_HTTP)) + str(sorted(G._RETRY_EXC)))


# ══════════════════════════════════════════════════════════════════
print("\n② 重试只限**第一次**尝试（第二次也炸时必须落到兜底）")
# ══════════════════════════════════════════════════════════════════

_BASE = dict(state={"messages": []}, config=None, task_sink=None, rounds=1, user_msg="",
             role="owner", page_ctx="", has_frames=True, doc_anchors=(), ledger_frame="",
             principal=None, round_info="", frames_txt="", gate_note="", deny=set(),
             _base_llm=None, _evidence=False, llm=None, deny_pseudo=set(),
             correction="", correction_kind="", planner_note="", plan_obj=None,
             raw="", decided=None, workflow_name="", params={}, _tools_off=False,
             _img_turn=False, llm_attempt=0)
_EVENTS: list = []
_ORIG_RECORD = G.record
G.record = lambda *a, **k: _EVENTS.append((a, k))  # noqa: ARG005


def _run(exc, *, attempt=0, has_frames=True):
    """真跑 `_decide_llm`（走的是生产那条 except 支），只把 LLM 换成一个必炸的桩。"""
    _EVENTS.clear()

    class _Boom:
        def invoke(self, prompt):
            raise exc

    c = G._DecideCtx(**{**_BASE, "llm": _Boom(), "llm_attempt": attempt,
                        "has_frames": has_frames})
    step = G._decide_llm(c)
    return c, step, list(_EVENTS)


_c0, _s0, _e0 = _run(_Status(503))
check("★ 第一次尝试撞 503 ⇒ 交回循环再问一遍（`continue`），不在这里收尾",
      _s0.action == "continue" and _s0.ret is None, _s0.action)
check("  这一格的计划**仍是 None**（「还没做出决定」这个事实不许被提前写成一张计划）",
      _c0.plan_obj is None, repr(_c0.plan_obj))

_c1, _s1, _e1 = _run(_Status(503), attempt=1)
check("★ 第二次尝试也撞 ⇒ 落到兜底，且 `plan_obj` **被赋值**"
      "（不赋值的话循环退出时它还是 None，终局那几段全落空）",
      _s1.action == "return" and _s1.ret is not None and _c1.plan_obj is not None,
      _s1.action)
# 反控：同一条异常，第一次继续、第二次收尾——**区别只在尝试序号**，不是"重试一次"这件事
# 本身判错。
check("  反控：同一条异常，第 0 次 continue / 第 1 次 return（区别只在序号）",
      _s0.action == "continue" and _s1.action == "return")

_c2, _s2, _e2 = _run(_Status(400))
check("坏请求（400）**当场**兜底——重问一次还是坏，用掉第二次尝试是白花",
      _s2.action == "return" and _c2.plan_obj is not None, _s2.action)
_c3, _s3, _e3 = _run(type("APITimeoutError", (Exception,), {})("timeout"))
check("超时当场兜底（理由见 ① 那条★）",
      _s3.action == "return" and _c3.plan_obj is not None, _s3.action)


# ══════════════════════════════════════════════════════════════════
print("\n③ 兜底不炸对话，也不把原因说错")
# ══════════════════════════════════════════════════════════════════

_plan_txt = str((_s1.ret or {}).get("plan") or "")
check("★ 计划文本里**没有**「已达规划轮次上限」——这是「规划这一步没跑成」，"
      "不是轮次用满；那句话会误导 narrator 按「聊得太多」组织回复",
      "已达规划轮次上限" not in _plan_txt)
check("  如实写明是**服务抖动**这一类原因（事后复盘要读得出是哪一种）",
      "没有跑完" in _plan_txt and "服务抖动" in _plan_txt)
check("  照旧走 `_wrap_up_plan` 的 wrapped 语义（**不新造 status**）",
      (_c1.plan_obj or {}).get("status") == "wrapped"
      and (_c1.plan_obj or {}).get("tools") == [],
      str((_c1.plan_obj or {}).get("status")))
check("  零执行：兜底那张计划里一条工具都没有",
      (_c1.plan_obj or {}).get("tools") == [])

_cnf, _snf, _enf = _run(_Status(500), attempt=1, has_frames=False)
_note = str((_cnf.plan_obj or {}).get("note") or "")
_note_f = str((_c1.plan_obj or {}).get("note") or "")
check("★ 无帧那一版明写「零执行」并要求如实作答、**不得编造**"
      "（借用确定性收尾轮那条既有契约，不另写一份）",
      "无任何工具执行记录" in _note and "不得编造" in _note, _note[:120])
# 反控：有帧 / 无帧**必须是两句不同的话**。写成同一句时，无帧那一版会让 narrator 照着
# 有帧那句去叙述"上面列出的工具调用"——而上面什么都没有，这正是谎话。
check("  反控：有帧与无帧的注记**不是同一句话**（同一句＝无帧那版在指着不存在的工具帧）",
      _note != _note_f and "上面列出的工具调用" in _note_f
      and "上面列出的工具调用" not in _note,
      _note_f[:60])


# ══════════════════════════════════════════════════════════════════
print("\n④ 无论走哪条路都**先落一条** `planner.llm_error`（本次修的正是「一个字都不写」）")
# ══════════════════════════════════════════════════════════════════

for _tag, _ev, _exp_retry in (("第一次/可重试 ⇒ continue", _e0, True),
                              ("第二次/可重试 ⇒ 兜底", _e1, False),
                              ("400 ⇒ 兜底", _e2, False),
                              ("超时 ⇒ 兜底", _e3, False)):
    _hits = [k for a, k in _ev if a[:2] == ("planner", "llm_error")]
    check(f"  {_tag}：恰好一条 `planner.llm_error`（为零就是那个洞又回来了）",
          len(_hits) == 1, f"{len(_hits)} 条")
    check(f"    `retry` 记的是**实际决定**（{_exp_retry}）",
          _hits and _hits[0].get("retry") is _exp_retry,
          str(_hits[0].get("retry") if _hits else None))
    check("    `attempt` 记的是这一格用掉第几次尝试（0/1 都是事实，不是布尔）",
          _hits and _hits[0].get("attempt") in (0, 1),
          str(_hits[0].get("attempt") if _hits else None))

_h0 = [k for a, k in _e0 if a[:2] == ("planner", "llm_error")][0]
check("  键集：`kind`/`round`/`attempt`/`retry`/`duration_s`/`detail` 恒在，"
      "status 那一格另外算",
      {"kind", "round", "attempt", "retry", "duration_s", "detail"} <= set(_h0),
      str(sorted(_h0)))
check("   `kind` 是**异常类名**（不是 `str(e)`——那串话里可能什么都没有）",
      _h0.get("kind") == "_Status", str(_h0.get("kind")))
check("  `status` 在有状态码时**原样**带上",
      _h0.get("status") == 503, str(_h0.get("status")))

_hnone = [k for a, k in _e3 if a[:2] == ("planner", "llm_error")][0]
check("★ `status` 缺席（**不是 0**）：这个异常身上没有状态码，"
      "编一个 0 出来就是在 trace 里造一个不存在的读数",
      "status" not in _hnone, str(sorted(_hnone)))
check("  反控：`kind` 仍在（缺席的是那一格，不是整条事件）",
      _hnone.get("kind") == "APITimeoutError", str(_hnone.get("kind")))

_ev.clear()
_extra = _run(_Status(500, "x" * 400))[2]
_h = [k for a, k in _extra if a[:2] == ("planner", "llm_error")][0]
check("  `detail` 截断 160（异常串可能很长，trace 不是日志）",
      len(str(_h.get("detail"))) == 160, str(len(str(_h.get("detail")))))

G.record = _ORIG_RECORD


# ══════════════════════════════════════════════════════════════════
print("\n⑤ 接线锁（AST）：这条出口**只有一处**，且循环真的会用到它")
# ══════════════════════════════════════════════════════════════════

_dl = _func("_decide_llm")
_tries = [n for n in ast.walk(_dl) if isinstance(n, ast.Try)]
check("`_decide_llm` 里只剩一个 `try`（第二个 `try` 就是第二条件用通路）",
      len(_tries) == 1, f"{len(_tries)} 个")
_handled = [n for n in ast.walk(_dl)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_llm_error_step"]
check("  那个 `try` 的 except 支转发给它（记账 + 处置在同一处）",
      len(_handled) == 1, f"{len(_handled)} 处")

_all_calls = [n for n in ast.walk(_TREE)
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_llm_error_step"]
check("★ 全文件里 `_llm_error_step` **只被调用一次**（唯一出口：分两处＝两处处置会漂）",
      len(_all_calls) == 1, f"{len(_all_calls)} 处")

_retr_calls = [n for n in ast.walk(_TREE)
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_llm_retriable"]
check("  `_llm_retriable` 的消费方也只有那一处（分类表不给别人判）",
      len(_retr_calls) == 1, f"{len(_retr_calls)} 处")

_pd = _func("_planner_decide")
_loops = [n for n in ast.walk(_pd) if isinstance(n, ast.For)
          and isinstance(n.target, ast.Name) and n.target.id == "_attempt"]
check("★ `_planner_decide` 的决策循环是 `for _attempt in (0, 1)`"
      "（把它改成 while/递归，下面那条赋值锁就失去意义）",
      len(_loops) == 1 and [_c.value for _c in _loops[0].iter.elts] == [0, 1],
      ast.dump(_loops[0].iter) if _loops else "无")
_sets = [n for n in ast.walk(_pd) if isinstance(n, ast.Assign)
         for t in n.targets
         if isinstance(t, ast.Attribute) and t.attr == "llm_attempt"]
check("  循环口把序号写进 ctx（`c.llm_attempt = _attempt`）——出口靠它判第几次",
      len(_sets) == 1, f"{len(_sets)} 处")

_all_sets = [n for n in ast.walk(_TREE) if isinstance(n, ast.Assign)
             for t in n.targets
             if isinstance(t, ast.Attribute) and t.attr == "llm_attempt"]
check("  `llm_attempt` 全文件也**只有那一处**被赋值（否则出口读到的不是循环那个序号）",
      len(_all_sets) == 1, f"{len(_all_sets)} 处")

_ann = getattr(G._DecideCtx, "__annotations__", {})
check("`_DecideCtx` 声明了 `llm_attempt`（没这一格，出口拿不到「第几次」）",
      "llm_attempt" in _ann, str(sorted(_ann))[:120])
_setup = ast.get_source_segment(_SRC, _func("_planner_setup")) or ""
check("  装配段给了初值 0（首轮决策就是第 0 次尝试）",
      "llm_attempt=0" in _setup and "llm_attempt" in _setup)

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"{len(FAILS)} 条未通过：")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
