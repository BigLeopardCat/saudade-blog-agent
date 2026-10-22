# -*- coding: utf-8 -*-
"""Extract total input, output, cache and reasoning token counts from responses.

Missing fields remain absent. In particular, unknown reasoning or cache usage
must not be reported as zero. No provider prices are embedded here.
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
        details = um.get("output_token_details") or um.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            reasoning = _int(details.get("reasoning", details.get("reasoning_tokens")))
            if reasoning is not None:
                out["reasoning"] = reasoning
        return out
    except Exception as e:  # noqa: BLE001 —— 记账绝不许反噬调用方
        logger.warning("[usage] 用量提取失败（本轮不记用量）: %s", e)
        return {}
