# -*- coding: utf-8 -*-
"""写操作幂等账的判据（20261016 批 2）：真图崩溃+恢复、零 LLM、零网络。

**这一件为什么存在**：LangGraph 的 checkpoint 提交粒度是 **superstep 不是语句** ⇒
一个节点里连做 N 次写，进程死在中间、从上一个 checkpoint 恢复，那 N 次写会**各执行
两遍**（实测见 `native-checkpoint-verdict-20261015.md` §2.6）。生产今天没有 saver
（`build_graph` 的 `checkpointer` 缺省 `None`，生产调用点不传）⇒ 这件事炸不了，但**接上
saver 那天重放路径第一次通电，而它一行判据都没有**。本文件就是那道保险丝上的判据。

⚠️ **口径更正（20261019）：本件量的是「续跑」那一格，不是生产那一格。**
本件第二遍传的是 `invoke(None)`（= "接着跑没跑完的任务"），而**生产每轮传 dict 输入**
⇒ langgraph 丢弃未完成任务、**从 START 重启**（`langgraph/pregel/_loop.py:977-979`）
⇒ `checkpoint_ns`（幂等键的身份成分）**逐字变了** ⇒ 账认不出同一件写。
真机读数（同一份夹具、只差"恢复那一次传什么"）：

| 恢复那一次传什么 | 两遍的 `checkpoint_ns` | 同一件写执行了几次 |
|---|---|---|
| `None`（= 本件） | 逐字相同 | 1（账压住了） |
| **dict**（= 生产每轮的形状） | 逐字变了 | **2（账没压住）** |

⇒ **本件的绿不等于生产受保护**。生产形状那一格的读数在
`eval/experiments/checkpoint_probe.py --crash-resume --guard --prod-shape`（真机、不在 CI）。
本件的断言**一个字都不改**——续跑那一格仍然有效且必要，只是它盖的不是生产。
完整口径见 `docs/adr/adr-0012-write-idempotency-ledger.md` 的《口径更正（20261019）》。

被测十一块（每条都配了会红的那一半）：
  · ① **§2.6 镜像（续跑那一格）**：真 `StateGraph` + `MemorySaver`，节点做到一半抛异常、
    再 `invoke(None)` **续跑** ⇒ 写**只发生一次**（⚠️ 生产不走续跑，见头注的口径更正）；
  · ② **反控**：同一场景换成 `NullLedger`（今天的形状）⇒ 写**发生两次**。没有这一条，
    ①可能只是恒真（本仓纪律：正控不红不算数）；
  · ③ **intent 窗口**：留着一行 `intent` ⇒ 判 BLOCK `write_outcome_unknown`，**且不重跑**；
  · ④ **惰性**：没挂 saver ⇒ `active()` 为假，且**连库文件都不该被创建**；
  · ⑤ **不误合并**：参数不同 / 工具不同的两次调用**都要执行**——"至多一次"最容易
    退化成"从不执行"的地方，而且退化了不报错；
  · ⑥ **重放逐字还回**：文本 + `kind` + `meta` 三样都要（只还文本会让命令帧判 BLOCK）；
  · ⑦ **fail-closed**：账本不可用 ⇒ 拒绝执行（响亮，不降级成"不记账"）；
  · ⑧ **接线锁**：`agent/graph.py` 里不许再出现 `tool.invoke(`（收口唯一）；
  · ⑨ **durability 自断言**：账本在场 ⇒ 必须 `sync` 档（缺省 `async` 下账**静默失效**）；
  · ⑩ **`_rehydrate` 的行形状**：喂 dict 会**不报错地**回放字面串（PG 那条路的正控）；
  · ⑪ **依赖面锁**：`psycopg` 只住在 `agent/idem_pg.py`，且只在 `_shared` 体内懒 import。

⑨–⑪ 是 20261016 迁 PG 那一批补的：⑨ 钉"账的前提"，⑩ 钉"两个后端交出的行必须是
**同一种形状**"，⑪ 钉"PG 是**可选档**——没挂 PG saver 的进程（含 CI）一次都不碰 psycopg"。
三件都是**不报错**的失效，靠读代码看不出来。

为什么主断言落在**调用次数**上而不是"帧文本长什么样"：这一件的失败面是"写真的做了
两遍"，那是外部可见且不可撤销的。断言一句措辞是假绿——换一句话就过。

用法：.venv/bin/python tests/test_write_idempotency.py
"""
import ast
import os
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

import agent.graph as g  # noqa: E402
import agent.idem as idem  # noqa: E402
from agent import block_reasons as br  # noqa: E402
from tools.base import ToolResult  # noqa: E402

FAILS: list[str] = []
_TMP = tempfile.mkdtemp(prefix="idem-judge-")


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


class _Boom(Exception):
    """模拟"进程死在节点中间"（节点没返回 ⇒ 这一轮没有 checkpoint）。"""


def _mk_tool(idx: int, hits: list, meta: dict | None = None):
    """假工具：只做一件事——把"我真的被调到了"记进 `hits`。"""
    class _T:
        name = f"t{idx}"

        def invoke(self, args):
            hits.append(idx)
            return ToolResult(f"done {idx}", "ok", meta or {"n": idx})
    return _T()


# 两个**真**写工具名（`authz.TOOL_SCOPE` 里落在 `WRITE_SCOPES` 的那一族）：
# 用假名字的话 `is_write` 为假 ⇒ `guarded_invoke` 直通 ⇒ 整套探针恒绿。
_W0, _W1 = "add_favorite", "toggle_effect"


def _run(led, *, crash_after, thread, fp0="fp0", fp1="fp1",
         names=(_W0, _W1), meta=None):
    """跑一次真图：第一遍在 `crash_after` 那一步之后抛，第二遍从 checkpoint 恢复。

    返回 `(真实执行次数, 每一遍节点里 config 的键)`。
    """
    hits: list = []
    seen: list = []
    box = {"crashed": False}

    class _S(TypedDict, total=False):
        done: int

    def node(state, config):
        seen.append(sorted((config.get("configurable") or {}).keys()))
        for i in range(2):
            idem.guarded_invoke(_mk_tool(i, hits, meta), names[i], {}, config,
                                fingerprint=(fp0 if i == 0 else fp1), ledger=led)
            if not box["crashed"] and i == crash_after:
                box["crashed"] = True      # 只崩第一遍——否则第二遍又崩，永远跑不完
                raise _Boom()
        return {"done": 1}

    graph = StateGraph(_S)
    graph.add_node("n", node)
    graph.add_edge(START, "n")
    graph.add_edge("n", END)
    app = graph.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": thread}}
    for first in (True, False):
        try:
            # `durability="sync"` 是账的前提（见 `idem.require_sync_durability`）：
            # 不传的话这里会当场抛 `ReplayMisconfigured` —— 那正说明断言咬得住。
            app.invoke({} if first else None, cfg, durability=idem.REQUIRED_DURABILITY)
        except _Boom:
            pass
    return hits, seen


def _cfg(ns: str, saver=True) -> dict:
    """一条"节点内"的 config：`checkpoint_ns` 是幂等键的身份成分（实测纯函数）。

    `__pregel_durability` 与 `__pregel_checkpointer` 一样是**真图会写进来的**内部键
    （`langgraph/_internal/_constants.py::CONFIG_KEY_DURABILITY` = `"__pregel_durability"`，
    实测：传了 `durability="sync"` 才有这个键，不传则**整个键缺席**）。没挂 saver 时
    两个键一起缺席——那种 config 下闸本来就是关的，形状照实写。
    """
    conf = {"checkpoint_ns": ns,
            "__pregel_checkpointer": MemorySaver() if saver else None}
    if saver:
        conf["__pregel_durability"] = idem.REQUIRED_DURABILITY
    return {"configurable": conf}


print("① §2.6 镜像（**续跑**那一格，不是生产那一格）：崩溃 + `invoke(None)` 续跑后，写只发生一次")
_led = idem.Ledger(os.path.join(_TMP, "a.sqlite3"))
_hits, _seen = _run(_led, crash_after=0, thread="t1")
check("挂了 saver 的 config 里真有 `__pregel_checkpointer`（否则整套探针恒绿）",
      "__pregel_checkpointer" in _seen[0], str(_seen[0][:4]))
check("节点做到一半崩、再从 checkpoint **续跑**（`invoke(None)`）⇒ 第一件写**只执行一次**"
      "（生产形状下不是这样：账不命中，读数 2×，见头注）",
      _hits == [0, 1], f"实际执行序列={_hits}")

print("\n② 反控：没有账本（今天的形状）时，同一场景写发生**两次**")
_rev, _ = _run(idem.NullLedger(), crash_after=0, thread="t2")
check("`NullLedger` ⇒ 第一件写执行两次（证明①不是恒真）",
      _rev == [0, 0, 1], f"实际执行序列={_rev}")

print("\n③ intent 窗口：留在 `intent` 的那一行 = 结果未知，且**不许重跑**")
_led3 = idem.Ledger(os.path.join(_TMP, "b.sqlite3"))
_key = idem.key_for(_cfg("n:deadbeef"), _W0, "fpZ")
_led3.claim(_key, _W0, "fpZ")            # 模拟"记了账、还没拿到返回就死了"
_hits3: list = []
_out3 = idem.guarded_invoke(_mk_tool(0, _hits3), _W0, {}, _cfg("n:deadbeef"),
                            fingerprint="fpZ", ledger=_led3)
check("留 `intent` ⇒ 第二次**不执行**", _hits3 == [], f"执行序列={_hits3}")
check("返回的是本族的错误帧，取回函数认得出",
      idem.replay_reason(str(_out3)) == idem.REASON_OUTCOME_UNKNOWN, str(_out3)[:80])
check("`_check_spec` 判 BLOCK 且原因是 `write_outcome_unknown`（接上受阻链路）",
      g._check_spec(_W0, {}, True, str(_out3), "chat") ==
      (g._VERDICT_BLOCK, "write_outcome_unknown"),
      str(g._check_spec(_W0, {}, True, str(_out3), "chat")))
check("原因码在两张表里都有，且**不可重试**（重试正是要防的那件事）",
      br.REASONS.get(idem.REASON_OUTCOME_UNKNOWN, (None, None))[1] is False
      and br.block_reason_type(idem.REASON_OUTCOME_UNKNOWN)[0] == "写操作结果未知",
      str(br.block_reason_type(idem.REASON_OUTCOME_UNKNOWN)))

print("\n④ 惰性：没挂 saver ⇒ 闸为假，且**连库文件都不该被创建**")
check("`active(None)` / `active({})` / 没 checkpointer 的 config 全为假",
      idem.active(None) is False and idem.active({}) is False
      and idem.active({"configurable": {"__pregel_checkpointer": None}}) is False)
check("挂了 saver 的 config 为真（否则闸恒关，等于没有这本账）",
      idem.active(_cfg("n:x")) is True)
_never = os.path.join(_TMP, "never", "never.sqlite3")
_hits4: list = []
idem.guarded_invoke(_mk_tool(0, _hits4), _W0, {}, {"configurable": {"checkpoint_ns": "n:x"}},
                    fingerprint="f", ledger=idem.Ledger(_never))
check("没挂 saver 时写照常执行、账本一次都没被碰过",
      _hits4 == [0] and not os.path.exists(_never))

print("\n⑤ 不误合并：参数不同 / 工具不同的两次调用**都要执行**")
_hits5, _ = _run(idem.Ledger(os.path.join(_TMP, "c.sqlite3")), crash_after=None,
                 thread="t5", fp0="A", fp1="B")
check("同一轮、同一工具、**参数不同** ⇒ 两件都执行（少这一格就是「从不执行」）",
      _hits5 == [0, 1], f"执行序列={_hits5}")
_led5b = idem.Ledger(os.path.join(_TMP, "d.sqlite3"))
_hits5b: list = []
for i, nm in enumerate((_W0, _W1)):
    idem.guarded_invoke(_mk_tool(i, _hits5b), nm, {}, _cfg("n:same"),
                        fingerprint="same-fp", ledger=_led5b)
check("同一轮、**同一指纹但工具不同** ⇒ 两件都执行（键里含工具名）",
      _hits5b == [0, 1], f"执行序列={_hits5b}")

print("\n⑥ 重放要**逐字**还回返回值（含 `meta`），不能只还文本")
_led6 = idem.Ledger(os.path.join(_TMP, "e.sqlite3"))
_CMD_TOOL = "navigate_to"          # 真命令工具（`_check_spec` 的 `cmd_shape` 只认它那一族）
_cmd = {"kind": "navigate", "url": "/about"}
_hits6: list = []
_first = idem.guarded_invoke(_mk_tool(0, _hits6, {"cmd": _cmd}), _CMD_TOOL, {}, _cfg("n:m"),
                             fingerprint="f", ledger=_led6)
_second = idem.guarded_invoke(_mk_tool(0, _hits6, {"cmd": _cmd}), _CMD_TOOL, {}, _cfg("n:m"),
                              fingerprint="f", ledger=_led6)
check("第二次是重放命中，工具没再执行", _hits6 == [0] and str(_second) == str(_first))
check("重放还回了 `meta['cmd']`（只还文本会让命令帧判 BLOCK，那是一次**凭空**的失败）",
      isinstance(_second, ToolResult) and _second.meta.get("cmd") == _cmd
      and _second.kind == "ok", f"meta={getattr(_second, 'meta', None)}")
check("重放出来的命令帧仍能通过 `_check_spec`（照 `cmd_shape` 校验）",
      g._check_spec(_CMD_TOOL, {}, True, str(_second), "chat", _second.kind, _second.meta)
      == (g._VERDICT_PASS, "ok"),
      str(g._check_spec(_CMD_TOOL, {}, True, str(_second), "chat",
                        _second.kind, _second.meta)))

print("\n⑦ fail-closed：账本不可用 ⇒ 拒绝执行（不降级成「不记账」）")
_bad = idem.Ledger(_TMP)          # 指向一个**目录** ⇒ sqlite 打不开
_hits7: list = []
_out7 = idem.guarded_invoke(_mk_tool(0, _hits7), _W0, {}, _cfg("n:boom"),
                            fingerprint="f", ledger=_bad)
check("账本打不开 ⇒ 写**一个都没执行**", _hits7 == [], f"执行序列={_hits7}")
check("返回 kind=unavailable ⇒ `_check_spec` 判 BLOCK `unavailable`（响亮，不静默）",
      getattr(_out7, "kind", None) == "unavailable"
      and g._check_spec(_W0, {}, True, str(_out7), "chat",
                        getattr(_out7, "kind", "ok")) == (g._VERDICT_BLOCK, "unavailable"),
      f"kind={getattr(_out7, 'kind', None)}")

print("\n⑧ 接线锁：写操作的出口只剩一个")


def _invoke_calls(src: str) -> int:
    """源码里 `tool.invoke(...)` **真的被调用**的次数。

    读 AST 不读字面：本仓纪律「锁的落点要选判据别选字面」——`agent/idem.py` 的注里
    逐字写着 `tool.invoke(` 这四个字（正是在解释为什么它搬到了这里），按文本数会
    数出两处、把一条正确的注释判成违规。
    """
    return sum(1 for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "invoke"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "tool")


_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_idem_src = (ROOT / "agent" / "idem.py").read_text(encoding="utf-8")
check("`agent/graph.py` 里不再有 `tool.invoke(...)`（绕过收口就是绕过幂等账）",
      _invoke_calls(_src) == 0, f"出现 {_invoke_calls(_src)} 次")
check("`tool.invoke(...)` 在 `agent/idem.py` 里**恰好一处**（收口真的搬过去了，不是被删了）",
      _invoke_calls(_idem_src) == 1, f"出现 {_invoke_calls(_idem_src)} 次")
check("`guarded_invoke` 在 graph.py 里**恰好被调一次**（两处调用 = 有一条路绕过账本）",
      _src.count("idem.guarded_invoke(") == 1, f"出现 {_src.count('idem.guarded_invoke(')} 次")
# 指纹必须取自**改动之前**的 args：`device_oled_display` 的 text 是 temperature 0.7
# 现创的，对改后的 args 取指纹 ⇒ 那个工具恒不命中，而且不报错。判据钉**先后顺序**。
_fn = next(n for n in ast.walk(ast.parse(_src))
           if isinstance(n, ast.FunctionDef) and n.name == "execute_node")
_fp_line = min(n.lineno for n in ast.walk(_fn)
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_spec_signature")
_mut_line = min(n.lineno for n in ast.walk(_fn)
                if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == "args" for t in n.targets))
_call_line = min(n.lineno for n in ast.walk(_fn)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "guarded_invoke")
check("指纹在**改动 args 之前**取、且在调用之前（顺序反了 ⇒ 屏显恒不命中且不报错）",
      _fp_line < _mut_line < _call_line, f"指纹@{_fp_line} < 改参@{_mut_line} < 调用@{_call_line}")

print("\n⑨ durability 自断言：账本在场就必须 `sync` 档（缺省 `async` ⇒ 账**静默失效**）")
check("`REQUIRED_DURABILITY` 就是 `sync`（改这个字面等于改账的前提）",
      idem.REQUIRED_DURABILITY == "sync", repr(idem.REQUIRED_DURABILITY))
_hits9: list = []
idem.guarded_invoke(_mk_tool(0, _hits9), _W0, {}, _cfg("n:dur"),
                    fingerprint="f", ledger=idem.Ledger(os.path.join(_TMP, "f.sqlite3")))
check("`sync` 档：照常执行（断言没把功能一起挡掉）", _hits9 == [0], f"执行序列={_hits9}")
for _bad in ("async", "exit", None):
    _conf = {"checkpoint_ns": "n:dur", "__pregel_checkpointer": MemorySaver()}
    if _bad is not None:
        _conf["__pregel_durability"] = _bad      # `None` = 那个键**整个缺席**（缺省档）
    _h9: list = []
    _o9, _raised = None, None
    try:
        _o9 = idem.guarded_invoke(_mk_tool(0, _h9), _W0, {}, {"configurable": _conf},
                                  fingerprint="f",
                                  ledger=idem.Ledger(os.path.join(_TMP, "g.sqlite3")))
    except idem.ReplayMisconfigured as e:
        _o9, _raised = None, e
    # 两件事一起判：①抛的是**异常**（若 `require_sync_durability` 落在那个 `try` 里，
    # 这里拿到的会是一条 `unavailable` 回执 —— 整轮静悄悄地全不执行，更难发现）；
    # ②写**一件都没执行**（抛在 `is_write` 之前，任何工具都当场炸）。
    check(f"档位 {_bad!r} ⇒ 抛 `ReplayMisconfigured`（不是回执）且写一件都没执行",
          _raised is not None and _o9 is None and _h9 == [],
          f"{type(_raised).__name__}: {_raised}")
_never9 = os.path.join(_TMP, "never9", "n.sqlite3")
_h9c: list = []
idem.guarded_invoke(_mk_tool(0, _h9c), _W0, {},
                    {"configurable": {"checkpoint_ns": "n:x", "__pregel_durability": "async"}},
                    fingerprint="f", ledger=idem.Ledger(_never9))
check("没挂 saver ⇒ 连档位都不查（今天就是这个形状：那一格不存在，一行都不该动）",
      _h9c == [0] and not os.path.exists(_never9), f"执行序列={_h9c}")

print("\n⑩ 重放的行**必须是元组**：喂 dict 会**不报错地**回放字面串（PG 那条路的正控）")
_row10 = ("done", "正文", "ok", '{"cmd": {"kind": "navigate"}}')
_r10 = idem._rehydrate(_row10)
check("4 元组 ⇒ 逐字还原（`status` 被丢掉，text / kind / meta 三样都在）",
      isinstance(_r10, ToolResult) and str(_r10) == "正文" and _r10.kind == "ok"
      and _r10.meta.get("cmd") == {"kind": "navigate"}, f"{str(_r10)!r} / {_r10.kind!r}")
_dict10 = {"status": "done", "text": "正文", "kind": "ok", "meta": "{}"}
_d10 = idem._rehydrate(_dict10)
check("喂 dict ⇒ 解包到的是**键名**（正文变 `'text'`、kind 变 `'kind'`），且**不抛**"
      " ⇒ PG 那条池若交出 `dict_row`，重放会静默腐化",
      str(_d10) == "text" and _d10.kind == "kind", f"{str(_d10)!r} / {_d10.kind!r}")
_led10 = idem.Ledger(os.path.join(_TMP, "h.sqlite3"))
_led10.claim("k10", _W0, "f")
_kind10, _row10 = _led10.claim("k10", _W0, "f")
check("SQLite 这条路交出的行**就是元组**（两个后端同一份契约，这条是它的正控）",
      isinstance(_row10, tuple) and _row10[0] == "intent" and _kind10 == "intent",
      f"{_kind10!r} / {_row10!r}")

print("\n⑪ 依赖面锁：psycopg 只住在 `agent/idem_pg.py`，且只在真要用时才被 import")


def _import_targets(n) -> list[str]:
    """这个 AST 节点 import 的**完整路径**（`import a.b` 与 `from a import b` 同形）。

    读 AST 不读字面（同 ⑧）：`agent/idem.py` 的注释与 docstring 里逐字写着 `psycopg`
    ——那正是在解释"它为什么住隔壁"。按文本数会把一条正确的注释判成违规。
    """
    if isinstance(n, ast.ImportFrom):
        base = n.module or ""
        return [f"{base}.{a.name}" if base else a.name for a in n.names]
    if isinstance(n, ast.Import):
        return [a.name for a in n.names]
    return []


def _imports_pg(n) -> bool:
    """这个节点 import 的**是 psycopg 本身**（不是某个恰好带这串字符的名字）。"""
    return any(t.split(".")[0] == "psycopg" for t in _import_targets(n))


_idem_tree = ast.parse(_idem_src)
_pg_path = ROOT / "agent" / "idem_pg.py"
_pg_src = _pg_path.read_text(encoding="utf-8") if _pg_path.exists() else ""
_pg_imports = [n for n in ast.walk(ast.parse(_pg_src)) if _imports_pg(n)] if _pg_src else []
check("`agent/idem_pg.py` 存在且它**确实** import 了 psycopg（否则下面两条是恒真）",
      len(_pg_imports) > 0, f"{_pg_path.name} 存在={_pg_path.exists()}，import 处={len(_pg_imports)}")
check("`agent/idem.py` 不 import psycopg（PG 是**可选档**：没挂 PG saver 的进程与 CI 都不装它）",
      [n for n in ast.walk(_idem_tree) if _imports_pg(n)] == [])
check("`agent.idem_pg` 不在 idem.py 的**模块级** import 里（那样 import 期就把 psycopg "
      "拖进来，没装 psycopg 的进程会**整个** import 失败）",
      [n for n in _idem_tree.body if "agent.idem_pg" in _import_targets(n)] == [])
_shared_fn = next(n for n in ast.walk(_idem_tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_shared")
check("`agent.idem_pg` 只在 `_shared` 函数体内懒 import（恰好一处）",
      sum(1 for n in ast.walk(_shared_fn)
          if "agent.idem_pg" in _import_targets(n)) == 1)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
