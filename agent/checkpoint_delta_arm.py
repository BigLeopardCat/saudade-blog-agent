# -*- coding: utf-8 -*-
"""golden 的第四条臂：**图自持上下文**（`GOLDEN_ARM=checkpoint_delta`，臂 C）。

**它要回答什么**：臂 B（`checkpoint`）证明了一件事——线程一复用，`_build_messages`
每轮**注入**的那段历史就成了**重复**：图自己已经持有整条会话，注入进来的 `history[-20:]`
又被追加一遍（批 0 实测：历史被重复注入 r 次，提示词 O(轮数²) 膨胀）。那么**停止注入**
是不是就对了？这正是臂 C 量的事。

**它是什么（代码上）**：臂 B 的那层壳 + **一步输入变换**。saver、线程改写、`build()`
约定名全部复用 `CheckpointGoldenArm`（一个字不改），只在 `stream()` 进图之前把
`state["messages"]` 换成"本轮该注入的那两条"：

    [System 块] + [当前问题]

也就是 `server._build_messages` **停止注入 `req.history[-20:]`** 之后逐字剩下的东西
——臂 C 是那一刀（属于"采纳"）的**逐字等价物**，所以采纳时删掉那段循环即可，不返工。

**恒等格**：`turn_msgs <= 2` ⇒ 本轮本来就没注入历史（`_build_messages` 只造了
`[System, 当前问题]`）⇒ **原样返回同一个 state**。这一格让"臂 C 与臂 B 的差集"
**恰好等于**注入过 history 的那几条用例——差集本身就是"这一刀切了什么"的读数。

**两条守卫**（缺一条就会静默丢错东西）：`messages[0]` 必须是以 `[System: ` 开头的
`HumanMessage`、`messages[-1]` 必须是当前问题。不成立就**响亮失败**，不"尽力而为"——
"首尾猜错了但照切"产出的是一份看着像臂 C 的假读数（本仓吃过不止一次）。

**为什么不动 `server.py`**：生产今天**没有** saver，去掉注入＝去掉全部跨轮记忆。
那一刀属于**采纳**（要先过批 2 的写幂等账），不属于本批。

⚠️ **本臂是评测臂，不是生产路径**：golden 的真写默认关（`needs_real_write` 的用例被
跳过）⇒ 崩溃重放那一格在本批的读数里**不存在**。别把"臂 C 通过了"读成"采纳安全了"。
"""
import logging

from agent.checkpoint_arm import CheckpointGoldenArm

logger = logging.getLogger(__name__)

ARM_NAME = "checkpoint_delta"
# 臂标（见 `checkpoint_arm.py` 头注第 1 条纪律）：两臂读数一模一样时先查这个。
DELTA_ARM_MARK = "native-postgres-saver+delta-input"

#: 恒等格的门槛：`_build_messages` 在"没注入历史"时正好造两条（System 块 + 当前问题）。
IDENTITY_TURN_MSGS = 2

_SYS_PREFIX = "[System:"


def _msg_text(m) -> str:
    """多模态 content 兼容（与 `context._msg_text` 同口径：数组只取 text 块）。"""
    content = getattr(m, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


class CheckpointDeltaArm(CheckpointGoldenArm):
    """臂 B + 输入变换：只保留「System 块 + 当前问题」，丢掉注入进来的历史。"""

    name = ARM_NAME

    def __init__(self, mode: str | None = None, saver=None):
        super().__init__(mode=mode, saver=saver)
        self.mark = DELTA_ARM_MARK

    @staticmethod
    def delta(state: dict) -> dict:
        """把注入的历史摘掉。**恒等格返回同一个对象**（判据要的是 `is`）。"""
        messages = state.get("messages") or []
        turn_msgs = state.get("turn_msgs") or 0
        if turn_msgs <= IDENTITY_TURN_MSGS:
            # 本轮没注入历史（或老调用点没报条数）⇒ 这一刀无事可做。
            return state
        # ── 两条守卫：猜错了就响亮失败，不"尽力而为" ──────────────────
        head, tail = messages[0], messages[-1]
        head_text = _msg_text(head).lstrip()
        if not (type(head).__name__ == "HumanMessage" and head_text.startswith(_SYS_PREFIX)):
            raise RuntimeError(
                f"[{ARM_NAME}] messages[0] 不是 `[System: ` 开头的 HumanMessage"
                f"（拿到 {type(head).__name__}: {head_text[:80]!r}）——首尾是这一刀**唯一**"
                "认得出的坐标，猜错了就该当轮炸掉，不许照切")
        if type(tail).__name__ != "HumanMessage":
            raise RuntimeError(
                f"[{ARM_NAME}] messages[-1] 不是当前问题（拿到 {type(tail).__name__}: "
                f"{_msg_text(tail)[:80]!r}）")
        keep = [head, tail]
        out = dict(state)
        out["messages"] = keep
        # `turn_msgs` 必须跟着改：它报的是"本轮注入了几条"，而 `planner_node` 拿它
        # 冻结 `turn_start`（`len(messages) - turn_msgs`）。不改 ⇒ 起点算到负数/错位。
        out["turn_msgs"] = len(keep)
        out["turn_start"] = -1          # 让 `freeze_turn_start` 重算（它认 `-1` 为"没冻过"）
        return out

    def stream(self, state: dict, config: dict | None = None,
               stream_mode: object = None, durability: object = None):
        return super().stream(self.delta(state), config, stream_mode=stream_mode,
                              durability=durability)


# ── 建臂 ─────────────────────────────────────────────────────────────
def build() -> CheckpointDeltaArm:
    """`golden_arm.build_agent()` 的入口（约定名，改名要连着改那边）。"""
    return CheckpointDeltaArm()
