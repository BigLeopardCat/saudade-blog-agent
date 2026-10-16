# -*- coding: utf-8 -*-
"""golden 的第三条臂：**原生 LangGraph checkpoint 落 PostgreSQL**（`GOLDEN_ARM=checkpoint`）。

**它要回答什么**：当初弃用原生 checkpoint 的理由是"线程累积 → worker 内存膨胀"。
那条理由**只对内存 saver 成立**——持久化 saver 里，累积的是**磁盘上的行**，不是
worker 的堆。所以真正的问题是另一件事，而且是可量的：

    接上持久化 saver、线程一复用，`state["messages"]` 就从"本轮那几条"变成
    "这个会话开天辟地以来所有条"——而**没有任何一处代码会因此报错**。

`add_messages` 是**追加** reducer（只在 id 相同时替换，而本仓的消息是每轮新建、
无 id ⇒ 永不去重）。于是三处读端会把**上一轮的事实**当成**本轮的事实**：

  ① `context._page_ctx` 取**第一条** `[System:` ⇒ 读到上一轮的页面/时间/特效；
  ② `context._has_frames` / `graph.frame_chars` / gate 的 `frames` ⇒ 上一轮的
     ToolMessage 被算成本轮证据（首轮五条确定性快道因此**永不命中**）；
  ③ `graph.model_node` 送出去的 `_msgs` 里，narrator 那一段取自 `state["messages"]`
     ⇒ **无截断**，随会话线性膨胀（埋点 `prompt_oversize` 就在那儿）。
     ⚠️ 本支已给这一段加了**轮次窗口**（`_narrator_msgs(state)` ⇒ `_turn(state)`，
     `tests/test_turn_window.py` 规则 F 盯着这条链路）⇒ 本条读数的"膨胀"只该出现在
     **窗口之外**的部分；窗口本身封顶与否，看 `--narrator-window` 那一格。

**它是什么（代码上）**：一层极薄的适配器。本臂**用的就是生产那张图**
（`agent.create_agent(checkpointer=...)`）——拓扑、节点、判据一个字都不动。
唯一的两处差别是：

  · 编译时挂上 `PostgresSaver`（`graph.build_graph(checkpointer=...)`）；
  · `stream()` 里把 `thread_id` 改写成**会话 id**（`conv_<conversation_id>`）。

`thread_id` 的改写发生在**臂这一层**（读 `config["configurable"]`），所以
`server.py` **一个字节都不用动**——与 `react_arm` 同一条纪律：生产那半只认四类
update 与那几族控制帧，不认这循环内部长什么样。

**两种线程模式**（`CHECKPOINT_THREAD`，缺省 `conversation`）：

  · `conversation`（臂 B，`GOLDEN_ARM=checkpoint`）：线程 = 会话 ⇒ 恢复**真的发生**；
  · `random`（反控 D，`GOLDEN_ARM=checkpoint_random`）：线程照旧每请求随机 ⇒
    saver **只写不读**。这一格是专门用来钉死"内存 vs 磁盘"那个伪理由的：
    它证明**光有 saver 零收益**，收益/代价全在"复用线程"这一件事上。

**三条纪律**（每条都对应一次假读数）：

  · **`CHECKPOINT_ARM_MARK` 必须在**：两臂读数一模一样时先查臂标——本仓吃过
    "两臂其实跑的是同一份代码"（`eval/golden_arm.py` 头注）。
  · **DSN 不在场 ⇒ 当场炸，绝不静默降级成无 saver**。否则"saver 根本没接上"
    会伪装成"checkpoint 没区别"——那是这次实验最坏的一种失效（负控 2）。
  · **不碰 `state`**：输入原样交给图。臂这一层若顺手去重/裁剪 `messages`，量的
    就不是"原生 checkpoint 的后果"而是"我写的一层补丁的效果"了。
"""
import logging
import os

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent import create_agent, idem

logger = logging.getLogger(__name__)

ARM_NAME = "checkpoint"
# 臂标（见头注第 1 条纪律）：`eval/experiments/checkpoint_probe.py` 逐臂印它。
CHECKPOINT_ARM_MARK = "native-postgres-saver"

ENV_DSN = "CHECKPOINT_PG_DSN"
ENV_THREAD = "CHECKPOINT_THREAD"

THREAD_CONVERSATION = "conversation"
THREAD_RANDOM = "random"


def _dsn() -> str:
    """实验库的 DSN。**缺了就炸**（见头注第 2 条纪律）。

    **刻意不写进 `.env`**：golden 的 cwd 是主仓、`config/settings.py` 的
    `env_file=".env"` 读的就是**产线那份** ⇒ 把实验库的 DSN 写进去等于改产线配置。
    走进程环境变量（`/home/ubuntu/exp/ckpt.env`，600，在仓外）。
    """
    dsn = (os.environ.get(ENV_DSN) or "").strip()
    if not dsn:
        raise RuntimeError(
            f"{ENV_DSN} 没设 —— 这条臂没有 saver 就没有存在的理由。"
            "**不许静默退回无 saver 的图**：那样跑出来的是一份"
            "「看着像持久化实验、其实是现状」的假读数（负控 2）。"
            "取 DSN：`source /home/ubuntu/exp/ckpt.env`")
    return dsn


def thread_mode() -> str:
    """`conversation`（臂 B）还是 `random`（反控 D）。不认识的取值响亮失败。"""
    mode = (os.environ.get(ENV_THREAD) or "").strip().lower() or THREAD_CONVERSATION
    if mode not in (THREAD_CONVERSATION, THREAD_RANDOM):
        raise RuntimeError(f"{ENV_THREAD}={mode!r} 不认识（已知："
                           f"{THREAD_CONVERSATION}/{THREAD_RANDOM}）")
    return mode


def _make_pool(pool_max: int) -> ConnectionPool:
    """建一条长期存活的连接池，并在建臂那一刻就**连一次**（负控 2）。

    `autocommit=True` + `prepare_threshold=0` + `dict_row` 三项都是 langgraph 的
    要求（`from_conn_string` 内部就是这么连的）；池的 `max_size` 只给本进程用——
    4 个 uvicorn worker 各一条池，`max_connections=30` 那个参数是照此定的。
    `autocommit` 还是**幂等账**的硬要求：占位必须**按语句**即时落库
    （见 `agent/idem_pg.py` 的 `ensure_autocommit`）。

    不预连的话，DSN 指错只会表现为"每个用例跑到第一次写 checkpoint 时卡 30 秒再抛
    PoolTimeout"——慢、且混在一堆 golden 输出里，很容易被读成"这一臂就是慢"。
    这里把它变成一句立刻可见的启动期报错。
    """
    pool = ConnectionPool(
        conninfo=_dsn(), min_size=1, max_size=pool_max, open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0,
                "row_factory": dict_row})
    try:
        pool.wait(timeout=10)
    except Exception as e:  # noqa: BLE001
        pool.close()
        raise RuntimeError(
            f"{ENV_DSN} 连不上实验库（{type(e).__name__}: {e}）——臂**不降级**："
            "连不上就该当轮炸掉，否则「saver 没接上」会伪装成「checkpoint 没区别」") from e
    return pool


def make_saver(pool_max: int = 4) -> PostgresSaver:
    """建一个长期存活的 saver（连接池，不是 `from_conn_string` 那个上下文管理器）。"""
    saver = PostgresSaver(_make_pool(pool_max))
    saver.setup()  # 幂等：建表 + 跑 migrations
    return saver


class CheckpointGoldenArm:
    """`server._agent` 上的一层薄外壳：生产那张图 + 持久化 saver + 线程改写。"""

    name = ARM_NAME

    def __init__(self, mode: str | None = None, saver: PostgresSaver | None = None):
        self.mode = mode or thread_mode()
        self.saver = saver or make_saver()
        # 幂等账（20261016 批 2）：本臂的线程**真的会复用**，所以崩溃重放这一格在本臂上
        # 是**活的**——`execute_node` 里的写必须走账，否则同一件会落两次库。
        # ⚠️ 账本**不进 config**：`idem` 的闸是"挂没挂 checkpointer"（`idem.active`），
        # 账本本体由 `idem._shared(config)` 按 saver 自己选（挂了 saver 那条池 ⇒ 同库）。
        self.graph = create_agent(checkpointer=self.saver)
        # 臂标也要能被"实例上"读到（进程里 import 到的是模块，两者都印更省事）。
        self.mark = CHECKPOINT_ARM_MARK
        logger.info("[checkpoint] 臂就绪：saver=postgres thread_mode=%s idem=on", self.mode)

    # ── 线程改写 ─────────────────────────────────────────────────────
    def _thread_for(self, config: dict | None) -> str:
        """这一轮用哪个 `thread_id`（**不改 config 以外的东西**）。"""
        conf = dict((config or {}).get("configurable") or {})
        original = str(conf.get("thread_id") or "")
        if self.mode == THREAD_RANDOM:
            return original
        conv = conf.get("conversation_id")
        # 会话 id 是**生产上真正稳定的那个坐标**（`server.py` 由请求体带进来，
        # 与确认令牌的签发维度同一个）。它缺席时退回原 thread_id = 反控 D 的行为
        # ——**刻意不编一个**（"每进程一个稳定线程"在生产上不存在，编出来量到的
        # 是我们自己发明的语义，不是要评的那条路）。
        if conv is None or str(conv).strip() == "" or str(conv) == "0":
            return original
        return f"conv_{conv}"

    # ── 生产接口（与 `CompiledStateGraph.stream` / `react_arm` 同一签名）─────
    def stream(self, state: dict, config: dict | None = None,
               stream_mode: object = None):
        """逐字转发内层图的 `(mode, data)` 二元组（帧契约不动）。

        `stream_mode` 原样往下传（生产者要 `["messages", "updates"]` 两路）。

        ⚠️ **`durability="sync"` 是幂等账的前提，不是性能选项**：缺省 `async` 下
        checkpoint 由后台线程写、主循环不等待，进程被硬杀时那一步可能没落盘 ⇒ 恢复时
        `__start__` 重跑、task_id 变了 ⇒ **账只在崩溃那一刻失效**（实测对照与推导见
        `agent/idem.py::require_sync_durability`）。代价：每个 superstep 多一次同步提交。
        """
        cfg = dict(config or {})
        conf = dict(cfg.get("configurable") or {})
        conf["thread_id"] = self._thread_for(config)
        cfg["configurable"] = conf
        return self.graph.stream(state, cfg, stream_mode=stream_mode,
                                 durability=idem.REQUIRED_DURABILITY)


# ── 建臂 ─────────────────────────────────────────────────────────────
def build() -> CheckpointGoldenArm:
    """`golden_arm.build_agent()` 的入口（约定名，改名要连着改那边）。"""
    return CheckpointGoldenArm()
