"""写操作的**重放闸**（20261016，批 2）——把「同一个 superstep 重放会再写一遍」堵在节点外。

## 它治的是什么（射程只有一格，名字就是这么起的）

LangGraph 的恢复单位是 **superstep**：节点内的 state 更新要等节点**返回**才落盘。
所以进程崩在 `execute_node` 中间时，整个节点**从头重跑**，而它里面那次 `tool.invoke`
是**真的写库**。今天生产没开 saver，这一格不存在；**采纳（线程改会话坐标 + 接上产线
saver）之后它立刻存在**——下一轮请求会从上一轮那个未完成的 checkpoint 恢复。

⚠️ **它不治什么**：planner 在**下一个 superstep** 里重排同一件写（新任务 ⇒ 新键）不归它管。
那一格仍归 `graph._grant_covers`（令牌只授权签名体里那一件）与 `graph._already_done_writes`。
两件事正交，谁也别想替谁——**别把本模块读成"写操作幂等"**。

## 键 = (thread_id, task_id, spec_index)

`task_id` 取自节点 config 的 `configurable["__pregel_task_id"]`（langgraph 的
`CONFIG_KEY_TASK_ID`）。它由 `_algo.py:616` 的
`task_id_func(checkpoint_id_bytes, checkpoint_ns, step, name, PULL, *triggers)` 算出
——**完全由输入 checkpoint + 节点名 + step + triggers 决定**：

  · 重放同一个 superstep ⇒ 输入 checkpoint 没变 ⇒ 同一个 task_id（这正是要的性质）；
  · 换一轮 / 换节点 ⇒ 新 task_id ⇒ 新键（**不会误压一次合法的重复写**）。

⚠️ **不要拿 `configurable["checkpoint_id"]` 当键**（我第一版就是这么写的，错的）：
`_algo.py:745/921/1091/1232` 把它**无条件写死成 `None`**——`_loop.py:473/522` 那两处
拼的是**给 checkpointer** 的 config，不是节点 config。用它会得到
`(thread, None, idx)`：同一个会话里**每个** superstep 的第 idx 件撞同一个键，
静默压掉整条会话的写。`tests/test_replay_guard.py` 的"正控"就是钉这一条的。

## ⚠️ 前提：图必须用 `durability="sync"` 跑（否则本闸**只在崩溃那一刻**失效）

task_id 是从**上一步那个 checkpoint** 算出来的，所以"重放拿到同一个 task_id"的前提是
**那个 checkpoint 真的落了库**。langgraph 缺省是 `durability="async"`：checkpoint 写交给
后台线程、**主循环不等待**（`_loop.py:1202` 原注 "save it, without blocking"）⇒ 进程被
**硬杀**（SIGKILL / OOM / `os._exit`）时，那一步的 checkpoint 可能根本没落盘。恢复时
`__start__` 会**重跑**、写出一个**新 checkpoint**，`execute` 的 task_id 因此**变了**，
键也变了 —— **账被绕过，而表面一切正常**。

实测对照（同一张图、同一个 thread、真 PG saver，子进程 `os._exit`）：

  | 崩法 | 子进程 task_id vs 恢复后 | 原因 |
  |---|---|---|
  | 节点**抛异常** | **逐字相同** | 循环正常退出 ⇒ 后台写被 flush |
  | 硬杀（`os._exit`） | **不同** | 那一步的 checkpoint 丢了 ⇒ 恢复时重跑 `__start__` |
  | 硬杀 + `durability="sync"` | **逐字相同** | 每步落盘都等（`main.py:3002` 的 `_put_checkpoint_fut.result()`）|

⇒ `durability="sync"` 不是"最好配上"，**是前提**：档不对就响亮抛
（`require_sync_durability`），与 `replay_guard_pg.ensure_autocommit` 同一条纪律——
"只在崩溃那一刻失效"的失效形态，静默起来没人看得见。代价是每个 superstep 多一次
**同步提交**（延迟换正确性，明写在 ADR-0012 的《取舍》里）。

## 覆盖范围：从 `authz` **派生**，不手抄

`WRITE_TOOLS` = scope ∈ `authz.CONSENT_SCOPES`（`write.console` + `write.own`）的那批。
页面写（导航/特效/暗色）与设备写（屏显）**不进账**，但理由要写对：不是"用户眼前能改回"
（那条判据对 flip 型工具是错的），而是 `toggle_effect`/`toggle_dark_mode` 带**显式
`action`/`mode`**（set 不是 flip，`tools/base.py` 的 docstring 明写"不因重复命令翻转状态"）
⇒ **重放幂等**，记账是多余的。屏显同办，**代价明写**：重放会再刷一次屏、文案小 LLM
（`_create_display_text`，temperature 0.7）非确定 ⇒ 重放后的屏文与首次不同。

## at-most-once：先占位、后调用、再记结果

**显式选"宁可漏写、绝不双写"**，理由是后果不对称：这本账要护的是
`authz._ALWAYS_CONFIRM_TOOLS` 那一族——公告 / 给第三方的站内通知 / 冻结账号 / 禁言。
双写落在**不可撤回**、且落在**别人**身上（"收不回"正是那张表的 why）；漏写主人看得见、
再说一句就补上。

三种重放结局，一种都不许糊：

  | 占位行 | 重放时 | 为什么 |
  |---|---|---|
  | 有 `outcome` | **逐字重建 `ToolResult` 回放**，不调工具 | 那一轮的行为必须与没崩过一模一样 |
  | 只有占位（`outcome` 为 NULL） | **不调工具**，返回 `kind="in_doubt"` ⇒ `_check_spec` 判 BLOCK | 写**可能发生、也可能没发生**（占位在 invoke **之前**，窗口 = 整个调用时长）。PASS 在本仓的定义是"系统确认的事实"，把"未知"记成确认事实就是编造 |
  | `signature` 与当前 spec 不符 | **响亮抛**，不回放 | 静默回放"另一件的结果"是本设计最坏的失效形态 |

`outcome` **成功与失败都记**：`tool.invoke` 抛异常时 `out` 是 `__ERROR__` 帧，那一帧就是
这次的结局；不记它，下次重放会**假报成功**。`kind`/`meta` 必须一起存、一起还原——
`execute_node` 从 meta 读审计的 `before/after/change` 与 `is_noop(out_meta)`，
只回放字符串会让"一次真 no-op"重放成"真改动"。

## 故障方向（两处刻意不对称）

  · `claim` 故障 ⇒ **响亮抛**（在 `try` 之外）：占位**没提交**意味着"我不知道这一件是不是
    已经做过" ⇒ **不能做**。被 `execute_node` 的 `except Exception` 吞掉的话，"账连不上"
    会被误标成"工具报错"（`_check_spec` 判 `error_frame` ⇒ planner 按规则 5 改参重试），
    而真正的风险（占位没提交）被藏起来。
  · `record` 故障 ⇒ **响亮记日志 + 继续**：写**已经发生**，结果也是真的，不许把成功的一轮
    丢掉；代价只是下一次重放会报 in-doubt（那是诚实的）。

## 默认关

`configurable` 里没有 `replay_guard` ⇒ `guard_of` 返回 `None` ⇒ `invoke_guarded` 退化成
`try: tool.invoke(args) except …`，与不带本模块**逐字节相同**。生产、golden、四条臂
全都不带这个键 ⇒ 本模块对既有读数零影响。
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent import authz

logger = logging.getLogger(__name__)

#: langgraph 的 `CONFIG_KEY_TASK_ID`（`langgraph/_internal/_constants.py:50` 的值）。
_TASK_ID_KEY = "__pregel_task_id"

#: langgraph 的 `CONFIG_KEY_DURABILITY`——节点 config 里也有（实测 async/sync/exit 三档
#: 都如实出现在 `configurable` 里），所以本闸能自己**断言**它，不必靠调用方自觉。
_DURABILITY_KEY = "__pregel_durability"

#: 本闸要求的那一档（理由见头注《前提》）。**缺省 async 是绕过本闸的那一档**。
REQUIRED_DURABILITY = "sync"

#: 账覆盖的工具集——**派生**自权限表，不手抄（手抄的那份会在新增写工具时静默漏掉一格）。
WRITE_TOOLS = frozenset(
    name for name, scope in authz.TOOL_SCOPE.items() if scope in authz.CONSENT_SCOPES
)

#: 重放时"已发出、结果未知"的 `ToolResult.kind`。`_check_spec` 必须把它判 BLOCK——
#: **新 kind 不接进 `_check_spec` 的后果是静默产回执**（见那里与 `tools/base.py` 的注）。
IN_DOUBT = "in_doubt"

#: 只有占位、没有结局时给 narrator/planner 看的如实帧。**不许写成"已经办好了"**。
IN_DOUBT_TEXT = ("〔重放〕这一件在恢复之前已经发出去了，但返回结果没能记下来——"
                 "它**可能已经生效**，也可能没有。请先读一次现状再下结论，不要重发。")

#: 占位行的结局（`text` + `kind` + `meta`）。存 dict 而不是 `ToolResult`：
#: 它要过 JSON/PG，且 `meta` 是机器可读的那一半（见 `tools/base.py` 的 `ToolResult`）。
Outcome = dict


class ReplayUnavailable(RuntimeError):
    """账在场、但拿不到键源（缺 `thread_id` 或 `__pregel_task_id`）。

    **刻意不降级**：拿不到键就等于"分不清这是第几次"，那就必须**不做**而不是"当作没账
    继续做"——后者会静默双写。同 `checkpoint_arm._dsn()` 的"缺了就炸"。
    """


class ReplayMisconfigured(RuntimeError):
    """账在场、但它要的前提不成立（图不是用 `durability="sync"` 跑的）。

    **刻意不降级成"照跑但效果打折"**：档不对时本闸**只在那一种情形下失效**——进程被
    硬杀、恢复时 task_id 变了。那正是它存在的唯一理由，也是唯一没有别的东西会发现的
    情形（异常路径反而正常）。⇒ 当场炸（见模块头注《前提》）。
    """


class ReplayConflict(RuntimeError):
    """占位行上记的 spec 与当前这一件不符 ⇒ **绝不回放**（回放另一件的结果是本设计最坏
    的失效形态：它会静默地把 A 件的结果当成 B 件的）。

    走到这里说明"同一个 (thread, task, idx) 上的 spec 变了"——重放时 specs 应当冻结在
    state 里，所以这是**前提被打破**的信号，要人来看，不是能兜住的异常。
    """


@dataclass(frozen=True)
class Claim:
    """`claim()` 的返回：一次调用就把"该不该做、回放什么"问全（省一次往返）。"""

    proceed: bool                    # True = 你拿到了占位，去调用工具
    outcome: Outcome | None = None   # 非 None = 逐字回放它（`proceed` 此时恒 False）
    claimed_at: str | None = field(default=None)   # 诊断用（in-doubt 时印在日志里）


class ReplayGuard(Protocol):
    """账的接口（实现见 `agent/replay_guard_pg.py`；判据里另有一份进程内的）。

    三条契约，实现方一个都不许省：
      ① `claim` 必须**原子**（并发下只有一个能拿到 `proceed=True`）；
      ② 占位必须在 `claim` 返回**之前**就**提交**（否则崩溃回滚会把占位丢掉 ⇒ 账白做）；
      ③ 已存在的行若 `signature` 不符 ⇒ 抛 `ReplayConflict`（不许静默回放）。
    """

    def claim(self, key: tuple, signature: str) -> Claim: ...

    def record(self, key: tuple, outcome: Outcome) -> None: ...


def guard_of(config: Any) -> ReplayGuard | None:
    """本轮的账（server/臂经 `config["configurable"]` 注入）。没有 ⇒ `None`。

    形状判据（`hasattr(claim)`）抄 `graph._principal_of` 的姿势：注入面在别人手里，
    拿到一个不认识的物件时**当没有账**比"当有账然后炸"安全——`None` 是"逐字节等于今天"，
    而炸会把一整轮对话打死。
    """
    g = (config or {}).get("configurable", {}).get("replay_guard")
    return g if g is not None and hasattr(g, "claim") else None


def replay_key(config: Any, index: int) -> tuple:
    """`(thread_id, task_id, spec_index)`。拿不到键源 ⇒ 抛（见 `ReplayUnavailable`）。"""
    conf = (config or {}).get("configurable", {}) or {}
    thread, task = conf.get("thread_id"), conf.get(_TASK_ID_KEY)
    if not thread or not task:
        raise ReplayUnavailable(
            f"重放闸在场却拿不到键源：thread_id={thread!r} {_TASK_ID_KEY}={task!r}"
            "（没有 checkpointer 时 langgraph 不注入 task_id ⇒ 账只能在真 saver 下开）")
    return (str(thread), str(task), int(index))


def require_sync_durability(config: Any) -> None:
    """图必须是 `durability="sync"` 跑的（理由见头注《前提》）。档不对 ⇒ 抛。

    只在**账在场**时调用：不带账的那条路要逐字节等于今天，一个字节都不许多查。
    """
    dur = (config or {}).get("configurable", {}).get(_DURABILITY_KEY)
    if dur != REQUIRED_DURABILITY:
        raise ReplayMisconfigured(
            f"重放闸在场，但这一轮是 durability={dur!r} 跑的（要 {REQUIRED_DURABILITY!r}）："
            "缺省 async 下 checkpoint 由后台线程写、主循环不等待，进程被硬杀时那一步可能"
            "没落盘 ⇒ 恢复时 task_id 会变 ⇒ 账被静默绕过（**只在崩溃那一刻失效**）。"
            "调用方加 `durability=\"sync\"`（见 agent/replay_guard.py 头注《前提》）")


def _signature(name: str, args: dict) -> str:
    """spec 指纹——**复用** `graph._spec_signature`（与回执/裁剪同源），不另写一份。

    函数内 import：`graph.py` 顶层 import 本模块，反向只能懒加载（同 `graph.py` 里
    `from tools.base import is_noop` 的姿势，那边是为了破 tools.base ↔ adminops 的环）。
    """
    from agent.graph import _spec_signature
    return json.dumps(list(_spec_signature(name, args)), ensure_ascii=False)


def dump_outcome(out: Any) -> Outcome:
    """工具返回值 → 可落库的结局。`kind`/`meta` 一起存（见模块头注）。"""
    return {"text": str(out),
            "kind": str(getattr(out, "kind", "ok") or "ok"),
            "meta": dict(getattr(out, "meta", None) or {})}


def load_outcome(d: Outcome) -> Any:
    """结局 → `ToolResult`（`execute_node` 从它读 `kind`/`meta`，所以**必须**是那个类型）。"""
    from tools.base import ToolResult
    meta = d.get("meta")
    return ToolResult(str(d.get("text") or ""), str(d.get("kind") or "ok"),
                      meta if isinstance(meta, dict) else {})


def _invoke_raw(tool: Any, args: dict) -> Any:
    """不带账的那一次调用——**逐字等于** `execute_node` 20261016 之前那 4 行。"""
    try:
        return tool.invoke(args)
    except Exception as e:                      # noqa: BLE001 —— 与原来那处同宽
        return f"__ERROR__: {type(e).__name__}: {e}"


def invoke_guarded(tool: Any, args: dict, name: str, index: int, config: Any) -> Any:
    """`execute_node` 里**唯一**那次 `tool.invoke` 的入口。

    ⚠️ 这个"唯一"是有依据的：`execute_node` 里其余的东西只有 `record(...)`（进程内
    recorder，崩溃那一轮根本不会 `dump()`）与内存里的 list 追加。**将来若在
    `execute_node` 里新加一处持久副作用（HTTP 写 / 文件写 / 发帧），它必须走这里**，
    否则它会重放两遍而账看不见它。
    """
    guard = guard_of(config)
    if guard is None:
        return _invoke_raw(tool, args)
    # 账在场就查前提（在**任何**工具之前，不只写工具）：档不对是**这一轮**的性质，
    # 不是某一件的性质 ⇒ 第一次调用就炸，别等第一件写。
    require_sync_durability(config)
    if name not in WRITE_TOOLS:
        return _invoke_raw(tool, args)
    key = replay_key(config, index)
    signature = _signature(name, args)
    # 占位**在 try 之外**：账自己的故障必须响亮抛出，不许被下面那个 except 吞成"工具报错"
    # （被吞的后果见模块头注《故障方向》）。
    claim = guard.claim(key, signature)
    if not claim.proceed:
        from utils.trace import record
        if claim.outcome is not None:
            record("execute", "replayed", tool=name, index=index)
            logger.info("[replay] %s 第 %d 件回放（占位于 %s）", name, index, claim.claimed_at)
            return load_outcome(claim.outcome)
        record("execute", "replay_in_doubt", tool=name, index=index)
        logger.warning("[replay] %s 第 %d 件在恢复前已发出、结果未知（占位于 %s）",
                       name, index, claim.claimed_at)
        return load_outcome({"text": IN_DOUBT_TEXT, "kind": IN_DOUBT, "meta": {}})
    out = _invoke_raw(tool, args)
    try:
        guard.record(key, dump_outcome(out))
    except Exception:                           # noqa: BLE001 —— 见模块头注《故障方向》
        # 写**已经发生**、结果也是真的 ⇒ 不许把这一轮丢掉。代价：下次重放会报 in-doubt
        # （那是诚实的）。响亮记日志，绝不静默。
        logger.exception("[replay] %s 第 %d 件结局落账失败（写已发生，本轮照常返回）",
                         name, index)
    return out
