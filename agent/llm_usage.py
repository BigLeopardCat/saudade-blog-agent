# -*- coding: utf-8 -*-
"""一次 LLM 调用的 token 用量提取——trace 里用量字段的唯一来源。

**为什么存在**：本 agent 的输入侧占成本 ~99%（回复 p50 只有 142 字），而"还能不能
再省"取决于一件事——**前缀缓存到底有没有在生产命中**。前缀缓存按**渲染后字符串的
连续前缀**命中（相同前缀越长的部分按缓存价计费），所以"模板里哪一段算稳定前缀"
是个能被测量的量；但在这一步之前，trace 的 `llm_done` 只记 `duration_s`，用量分文
未记 ⇒ "该不该重排模板"这个决定没有任何生产数据可依（只有一次探针结论：同一 prompt
逐字节重发，第二次 `cache_read=4096`，隐式缓存在这个端点上确实生效）。

这里把 `resp.usage_metadata` 抠成几个数，交给既有 `llm_done` 事件一起落盘：
零迁移、零 schema 变更，`eval/token_cost_report.py` 立刻能按天/按引擎聚合。

**不写价格常量**：网关只回 token 数、不回钱数（无计价字段，已核）。金额一律离线
按控制台单价折算——把单价抄进代码，等于把"随时会变的商务数字"焊进判据里。

字段契约（读者：`eval/token_cost_report.py` 与任何事后对账的人）：

  input        本次调用送进去的全部输入 token（**含**命中缓存的那部分）
  output       模型生成的 token
  cache_read   其中命中前缀缓存的 token

`cache_read/input` 才是判决量（命中率），绝对数不是。

**`cache_read` 缺席 ≠ 没命中**：只有端点真的回了这个字段才会写进去。缺这一格表示
"这个形状里量不到缓存"，而 `cache_read=0` 表示"端点说了没命中"——两者在聚合里必须
分开看，所以这里**不**把量不到的补成 0（同 `eval/dial_matrix.py` 里 fallback 记 None
不记 0 的纪律）。同理，整个函数取不到用量时返回 `{}`。

调用方直接展开即可：`record(..., **usage_fields(resp))`——少这几个键不是错误，
不需要兜底分支（trace 里没有这一格 = 这次没量到）。

**两条侧任务（20261018 批 4 起都记了，两条走**不同**的路）**：

  · `agent/summarizer.py` ⇒ **落本轮 trace**。它跑在**同一轮对话**里
    （`server._summarize_dialogue` 经 `_submit_with_context` 提交，那里的 `copy_context`
    把 recorder 带进 worker 线程）⇒ 直接 `record("summarizer", "llm_done", **usage_fields(resp))`。
  · `agent/moderator.py` ⇒ **交给调用方**（`review(..., on_usage=…)` 回调），由
    `server.py` 的审核端点打进既有那条 `[review]` 日志行。它**没有** trace 上下文
    （审核是留言板的独立请求、不挂任何一轮对话），而 `record()` 在没有 recorder 时是
    **静默空转** —— 往那里写等于生产上什么都没发生。

  这份头注此前写的是"两条都运行在没有 trace 上下文的位置"⇒ 对摘要那条是**错的**
  （照它做会得出"摘要没法记"的结论）。别照 moderator 的样子给摘要加回调，也别照摘要的
  样子给 moderator 加 `record()`：两边的上下文不一样，判据在 `tests/test_side_tasks.py` ⑧。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _int(v):
    """能转 int 就转，转不了回 None（'12'／12 都认；None／''／对象都不认）。"""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def usage_fields(resp) -> dict:
    """从一次 LLM 响应里取出用量（纯函数，鸭子类型，永不抛）。

    认两种形状：langchain 归一后的 `usage_metadata`（`input_tokens`/`output_tokens`/
    `input_token_details.cache_read`），以及网关原始 `response_metadata["token_usage"]`
    （`prompt_tokens`/`completion_tokens`/`prompt_tokens_details.cached_tokens`）。
    老版本 langchain 只填后者的情形实测存在，两条都读。
    """
    try:
        um = getattr(resp, "usage_metadata", None)
        if not isinstance(um, dict) or not um:
            rm = getattr(resp, "response_metadata", None)
            um = rm.get("token_usage") if isinstance(rm, dict) else None
        if not isinstance(um, dict) or not um:
            return {}
        n_in = _int(um.get("input_tokens", um.get("prompt_tokens")))
        n_out = _int(um.get("output_tokens", um.get("completion_tokens")))
        # 解析不出来的**不写**（写成 0 是把"读不懂这个值"记成"token 数是 0"，
        # 聚合时会静默拉低均值）；一个都没解析出来就整格不记。
        out = {}
        if n_in is not None:
            out["input"] = n_in
        if n_out is not None:
            out["output"] = n_out
        if not out:
            return {}
        det = um.get("input_token_details") or um.get("prompt_tokens_details") or {}
        if isinstance(det, dict):
            cache = _int(det.get("cache_read", det.get("cached_tokens")))
            if cache is not None:
                out["cache_read"] = cache
        return out
    except Exception as e:  # noqa: BLE001 —— 记账绝不许反噬调用方
        logger.warning("[usage] 用量提取失败（本轮不记用量）: %s", e)
        return {}
