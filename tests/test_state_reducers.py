"""`AgentState` 的**累加器**要么带 reducer、要么在豁免台账里写明理由——机械闸（20261016）。

## 为什么要有这一条

LangGraph 合并两份 state 更新时，**只有带 reducer 的键**才做"合并"，其余键一律
**last-write-wins**。`AgentState` 28 个键里有 **9 个是 list**（天然会被读成"累加器"），
其中**只有 `messages` 带 `Annotated[list, add_messages]`**，其余 8 个是裸 list。

裸 list 本身不是错——有些键**本来就是覆盖语义**（`blocked` 只含本轮受阻项，
`task_satisfied` 每轮重算）。错的是**没人分得清哪几个是**：另一些是**请求内累加器**
（`receipts` / `tool_data` / `executed` / `noop_specs` / `blocked_seen`），它们的"累加"
**完全靠每个写入者记得把整个累加器原样再返回一遍**
（`execute_node` 里那句 `"executed": executed + [s for s in specs if s not in executed]`）。
漏掉那一步 = **静默丢一半**，没有任何东西会红。

这与 `test_state_keys.py` 是**同一个机制的两半**：

  · 那个闸管的是**键名**在不在 schema 里（未声明的键被 LangGraph 静默丢，付过 13 次代价）；
  · 这个闸管的是**合并语义**有没有被声明出来。

`graph_input()` 给 28 个键**全部**配了初值，键集的"恰好性"由 `test_state_keys.py` ⑤ 锁住
⇒ **「初值」那一半已经锁死了；「reducer」这一半此前没有任何判据**（本文件即那一半）。

## 判法（静态：读注解对象 + 一份台账，不跑图、不调 LLM、无网络）

  · 取 `AgentState.__annotations__`——**真实注解对象**，不是源码文本。`graph.py` 顶部
    禁 `from __future__ import annotations`（注解若变字符串，langgraph 认不出 config 参数，
    见 `test_authz.py` ⑧ 那把回归锁），所以这里拿到的是 `list[str]` / `Annotated[...]`
    这类真对象，可以直接判；
  · 挑出**裸 list** 的键：没有 `Annotated` 元数据（`__metadata__` 为空），且
    `get_origin(ann) is list` 或就是 `list`；
  · 每一个裸 list 键都必须在 `EXEMPT` 台账里，且理由**实质性**（见下）；
  · 反向：台账里的每一格**必须仍是**裸 list 键（键没了、或已经带上 reducer 了
    ⇒ **过期条目要销账**——照 `test_state_keys.py` ③ 的双向台账体例）。

## 台账的理由写什么

不是"忘了加"，而是**这个键为什么是这个合并语义**：覆盖是对的？还是累加由写入者手工保证？
每一格都点了**写点在哪**（函数名，不是行号——行号会漂）。改语义时先读那一格。
台账落在**判据这一侧**（而不是 `graph.py` 的 `AgentState` 旁边）：它是判据的**数据**，
和判据同生共死；`AgentState` 的 docstring 已经把每个键的语义写在那儿了，不必抄第二份。

## 射程（本闸**抓不到**什么，如实划界）

  · **不带 reducer 的 dict 键**（`plan_obj` / `pending_confirm` / `confirm_grant` /
    `pending_action` / `ledger`）：dict 也可能被当累加器使，但本仓这五个都是**整体覆盖**，
    且 dict 上没有 list 那种天然的 `+`。**这是有意不覆盖的一格，不是漏了**——真出现
    dict 累加器时另立一条（那时"合并"的语义要单独定，不是套 `operator.add` 就完事）。
  · **带 reducer 的键，reducer 对不对**：本闸只判"声明了"，不判"声明得对"。
  · **写入者有没有真的按累加器语义写**：那是 `test_state_keys.py` 的 producer 面。
  · `agent/react_line.py` 用的是 langchain 自己的 `AgentState`，另一套 schema，不在射程。
  · 别的模块造的 state 更新：本仓 state 只在 `agent/graph.py` 里造。

## 反控（④⑤⑥⑦，本仓「正控不红不算数」）

  · ④ 合成的注解映射里**摘掉 `messages` 的 reducer** ⇒ 必须被报成"未登记"；
  · ⑤ 合成的台账里**理由为空/过短** ⇒ 必须被报成"理由不实质"；
  · ⑥ 合成的台账里**条目已过期**（那个键已经带上 reducer）⇒ 必须被销账报红；
  · ⑦ **负控**：带 reducer 的键**不许**被报成未登记（防"把所有 list 都报出来"这种恒红）。

用法：.venv/bin/python tests/test_state_reducers.py
"""


import sys
from pathlib import Path
from typing import Annotated, get_origin

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.graph as G  # noqa: E402

_MIN_REASON = 20


EXEMPT = {
    "executed": (
        "请求内累计 + 去重：`execute_node` 返回 "
        "`executed + [s for s in specs if s not in executed]`。累加由写入者手工保证；"
        "它是「该配 reducer」的头号候选，登记在此而非当场加 reducer，理由见 ADR-0010。"
    ),
    "receipts": (
        "请求内累计：`execute_node` 取 `list(state.get(\"receipts\") or [])`、逐 spec append、"
        "整体再返回。是跨轮执行记忆（`__EXEC__` 帧）与 reflector 的原料，**只增不减**。"
    ),
    "noop_specs": (
        "请求内累计的零改动签名（去重）：`execute_node` 取 `list(state.get(\"noop_specs\") or [])`"
        "后 append。与 `receipts` 同模式，靠 `_trim_noop_specs` 消费。"
    ),
    "blocked": (
        "**覆盖语义是对的**：`execute_node` 每轮现造 `blocked: list = []`，只装**本轮**受阻项"
        "——路由判 `blocked_repeat` 与 reflector 输入都要的是「本轮」，累加反而会喂错。"
    ),
    "blocked_seen": (
        "请求内累计（集合语义、已去重）：`execute_node` 返回 "
        "`sorted(prev_seen | {_blocked_key(b) for b in blocked})`。与 `blocked` **同源但不同语义**"
        "（一个是本轮、一个是请求内），改其中一个必须同时看另一个。"
    ),
    "tool_data": (
        "请求内累计：`execute_node` 取 `list(state.get(\"tool_data\") or [])` 后 append。"
        "是参数引用（`$tool[N].field`，见 `agent/arg_refs.py`）的取值源，**顺序即下标**"
        "——加 reducer 时必须保住顺序语义。"
    ),
    "task_satisfied": (
        "**覆盖语义是对的**：`_confirm_popup` 两支出口都返回 `_satisfied_specs(already)`，"
        "按本轮现状**重算**；累加会让上一轮的「已达成」赖在账上，而它恰恰是"
        "「靠回执结账的账上永远结不掉」那条纪律的反面（掏空出口正是为了结掉它）。"
    ),
    "task_frames": (
        "**两种语义并存，这一格未裁决**：`planner_node` 的壳累加"
        "（`list(upd.get(\"task_frames\") or ()) + sink`），而 `_plan_update` 是整体赋值"
        "（`upd[\"task_frames\"] = task_frames`）。今天靠「同一轮只走其中一条出口」维持不冲突；"
        "**动它之前先读这两处**——它被标成待裁决，不是已定案。"
    ),
}


FAILED: list = []
check = collector(FAILED)


def bare_list_keys(annotations: dict) -> set:
    """注解映射里**裸 list** 的键（带 `Annotated` 元数据 = 有 reducer，不算）。"""
    out = set()
    for name, ann in annotations.items():
        if getattr(ann, "__metadata__", None):
            continue                      # Annotated[list, reducer] ⇒ 已声明合并语义
        if ann is list or get_origin(ann) is list:
            out.add(name)
    return out


def reduced_keys(annotations: dict) -> set:
    """带 `Annotated` reducer 的键（扫面自检用）。"""
    return {n for n, a in annotations.items() if getattr(a, "__metadata__", None)}


def _reason_ok(reason) -> bool:
    return isinstance(reason, str) and len(reason.strip()) >= _MIN_REASON


def audit(annotations: dict, exempt: dict) -> tuple:
    """→ (unregistered, stale, weak)。三份都排序，便于比对。

    `exempt` 的值就是**理由字符串本身**——不套 1 元组：`("只有一句",)` 与 `"只有一句"`
    在 Python 里只差一个逗号，而这个差别恰好会让 `exempt[k][0]` 悄悄读到**第一个字符**
    （本文件首版就是这么错的：③a 把 8 格全报成"理由过短"）。
    """
    bare = bare_list_keys(annotations)
    unregistered = sorted(bare - set(exempt))
    stale = sorted(k for k in exempt if k not in bare)
    weak = sorted(k for k in exempt if not _reason_ok(exempt[k]))
    return unregistered, stale, weak


def _noop_reducer(left, right):      # 反控只关心"有没有元数据"，不关心它做什么
    return right


_NEG_STRIPPED = {"messages": list, "receipts": list[dict]}          # ④ 摘掉 reducer


_NEG_WEAK = {"receipts": ""}                                        # ⑤ 理由为空


_NEG_STALE = {"messages": "这条已经带上 reducer 了，还挂在台账里就是过期条目"}  # ⑥


def main() -> int:
    ann = G.AgentState.__annotations__
    bare = bare_list_keys(ann)

    print(f"[扫面] 声明 {len(ann)} 个键；裸 list {len(bare)} 个：{sorted(bare)}")

    check("① 扫面能看到 reducer（判据瞎了 ⇒ 后面全是恒真的假绿）",
          "messages" in reduced_keys(ann),
          f"带 reducer 的键={sorted(reduced_keys(ann))}")

    unregistered, stale, weak = audit(ann, EXEMPT)
    check("② 每个裸 list 键都已登记（要么带 reducer，要么在台账里）",
          not unregistered, f"未登记={unregistered}")
    check("③a 台账每格的理由都实质性（长度是代理判据，不是真判据）",
          not weak, f"理由过短={weak}")
    check("③b 台账没有过期条目（键没了 / 已带 reducer ⇒ 销账）",
          not stale, f"过期={stale}")

    n, _, _ = audit(_NEG_STRIPPED, EXEMPT)
    check("④ 反控：摘掉 `messages` 的 reducer ⇒ 报成未登记",
          n == ["messages"], str(n))

    _, _, w = audit({"receipts": list[dict]}, _NEG_WEAK)
    check("⑤ 反控：理由为空 ⇒ 报成理由不实质", w == ["receipts"], str(w))

    _, s, _ = audit({"messages": Annotated[list, _noop_reducer]}, _NEG_STALE)
    check("⑥ 反控：条目已过期 ⇒ 报成过期条目", s == ["messages"], str(s))

    n2, _, _ = audit({"messages": Annotated[list, _noop_reducer]}, EXEMPT)
    check("⑦ 负控：带 reducer 的键**不许**被报成未登记（防恒红）",
          n2 == [], str(n2))

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("✅ 全绿")
    return 0


def test_state_reducers():
    main()

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_state_reducers()
