# -*- coding: utf-8 -*-
"""**反控 D**：同一个持久化 saver，但线程照旧每请求随机（`GOLDEN_ARM=checkpoint_random`）。

它的全部意义是**一格对照**：把"挂了 saver"与"复用了线程"这两件事拆开。

  · 臂 B（`checkpoint`）＝ saver + 线程复用；
  · 反控 D（本臂）＝ saver + 随机线程 ⇒ 每个 checkpoint 写进去之后再没人读过，
    `state["messages"]` 永远是本轮那几条 ⇒ **行为应当与臂 A 逐用例相同**，
    而磁盘上的行数**线性增长**（只写不读）。

所以这一格要证伪的命题是「接上持久化就够了吗」：如果 D 与 A 的差集为空、而 B
与 A 的差集非空，那么差异**全部**来自"复用线程"这一件事，与"存内存还是存磁盘"
无关——那正是用户要的那句判断的读数版。

实现是**同一个类**，只把 `CHECKPOINT_THREAD` 定成 `random`（不另写一份逻辑：
两份实现必然漂移，而这里唯一要变的就是那一个取值）。
"""
import os

from agent.checkpoint_arm import CheckpointGoldenArm

ARM_NAME = "checkpoint_random"
# 臂标与 B 同源同义（见 `checkpoint_arm` 头注第 1 条纪律）。
CHECKPOINT_ARM_MARK = "native-postgres-saver/random-thread"


def build() -> CheckpointGoldenArm:
    """`golden_arm.build_agent()` 的入口。"""
    os.environ["CHECKPOINT_THREAD"] = "random"
    return CheckpointGoldenArm(mode="random")
