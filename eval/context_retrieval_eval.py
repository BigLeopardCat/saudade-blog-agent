"""Replay source reads through existing host/page budgets; no HTTP, answer model or Judge."""

from __future__ import annotations

import copy
from functools import lru_cache

from langchain_core.messages import ToolMessage

from agent.domains.articles.reading import project
from agent.runtime.message_content import estimate_tokens
from eval.fixtures.document_reads import document_page
from eval.offline_rag_eval import evaluate
from eval.section_range_ablation import volume
from rag.context_windows import expand_context, merge_read_windows
from rag.corpus import content_revision
from rag.reading import article_frame_text, render_article_result
from rag.sections import PARSER_VERSION, outline

FRAME_BUDGET = 4096


def read_frame(row, identifier, section, offset, end, remaining):
    return copy.deepcopy(_read_frame(row.get("title", ""), row["text"], identifier,
                                    section, offset, end, remaining))


@lru_cache(maxsize=4096)
def _read_frame(title, content, identifier, section, offset, end, remaining):
    if remaining <= 32:
        return None
    revision = content_revision(title, content)
    limit = min(24000, end - offset)
    params = {"section": section, "offset": offset, "limit": limit, "revision": revision,
              "section_schema": PARSER_VERSION, "view": "body"}
    response = document_page("note", identifier, title, content, params)
    frame = project(response, "note", identifier, section, offset, limit, revision, PARSER_VERSION)
    text = article_frame_text(frame)
    def message(value):
        return ToolMessage(content=value, tool_call_id="context-read")

    if estimate_tokens([message(text)]) > remaining:
        cap = max(0, int((remaining - 32) * 1.5))
        if not cap:
            return None
        text, frame = render_article_result(frame, text, cap)
    tokens = estimate_tokens([message(text)])
    if frame is None or tokens > remaining:
        return None
    return frame, text, tokens


def read_contexts(data, arm, profile, *, mode="window", context_budget=FRAME_BUDGET,
                  window_tokens=1024):
    if mode not in {"window", "parent", "neighbors"} or context_budget < 1:
        raise ValueError("known context policy and positive query budget required")
    keys = sorted(data["corpus"])
    identifiers = {did: index + 1 for index, did in enumerate(keys)}

    @lru_cache(maxsize=2048)
    def sections(did):
        row = data["corpus"][did]
        return {section["section_id"]: section["text"]
                for section in outline(row["text"], row.get("title", ""))}

    visible, costs, runs, frame_lengths = {}, {}, {}, {}
    for qid, selected in arm["locations"].items():
        selected = selected[:8]
        runs[qid] = {did: arm["run"][qid][did] for did in sorted(
            arm["run"][qid], key=lambda did: (-arm["run"][qid][did], did))[:8]}
        requested = (selected if mode == "window" else expand_context(selected,
            lambda hit, sid: sections(hit["document_id"])[sid], profile,
            mode=mode, max_tokens=window_tokens))
        shown, tokens, chars, body_tokens, reads, planned = [], 0, 0, 0, 0, 0
        sizes = []
        for hit in requested:
            did = hit["document_id"]
            matches = []
            ranges = merge_read_windows(hit["matches"])
            planned += len(ranges)
            for window in ranges:
                offset, end, sid = window["offset"], window["end"], window["section_id"]
                while offset < end:
                    frame = read_frame(data["corpus"][did], identifiers[did], sid,
                                       offset, end, context_budget - tokens)
                    if frame is None:
                        break
                    value, frame_text, used = frame
                    source = value["source"]
                    if source["offset"] != offset or not offset < source["end"] <= end:
                        raise ValueError("visible source differs from requested read range")
                    body = value["sectionText"]
                    tokens += used
                    reads += 1
                    sizes.append(len(frame_text))
                    chars += len(body)
                    body_tokens += profile.count(body)
                    matches.append({"section_id": sid, "offset": offset, "end": source["end"]})
                    offset = source["end"]
            shown.append({"document_id": did, "matches": matches})
        visible[qid] = shown
        frame_lengths[qid] = sizes
        costs[qid] = {"frame_estimated_tokens": tokens, "body_bge_tokens": body_tokens,
                      "body_chars": chars, "unique_source_chars": volume(shown),
                      "simulated_reads": reads, "planned_ranges": planned}
    scored = evaluate(data, runs, [1, 3, 8], locations=visible)
    return {**scored, "locations": visible, "per_query_cost": costs,
        "per_query_frame_lengths": frame_lengths,
        "mean_cost": {metric: sum(row[metric] for row in costs.values()) / len(costs)
                      for metric in next(iter(costs.values()))},
        "policy": {"mode": mode, "query_frame_estimated_token_budget": context_budget,
                   "per_expansion_bge_token_budget": window_tokens,
                   "preserve_larger_retrieval_anchors": True,
                   "cost_counter": "existing framework estimate, chars_per_token=1.5; "
                                   "BGE body count separately; not provider billing"},
        "scope": "Frozen-source host/page contract replay, including production result clipping. "
                 "No HTTP, LLM, answers or Judge; does not measure Agent task success.",
        "http_calls": 0, "model_calls": 0, "semantic_judge_calls": 0}
