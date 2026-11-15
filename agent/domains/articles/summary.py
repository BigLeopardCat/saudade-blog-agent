"""Read every page of one public revision before publishing a derived summary."""

import json
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent.domains.articles.summary_model import synthesize
from agent.runtime.budget import BudgetExceeded, TaskBudget, current_budget
from agent.runtime.message_content import estimate_tokens
from agent.runtime.progress import emit
from agent.runtime.turns import check_cancelled
from rag.sections import PARSER_VERSION
from rag.source import MAX_DOCUMENT_CHARS, Digest
from tools.result import ToolResult, ok, unavailable
from utils.trace import record


class SummaryCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    complete: Literal[True]
    start: Literal[0]
    end: Annotated[StrictInt, Field(gt=0)]
    total_chars: Annotated[StrictInt, Field(gt=0)]
    revision: Digest
    section_schema: StrictStr
    pages: Annotated[StrictInt, Field(gt=0)]


class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["summary"]
    noteId: Annotated[StrictInt, Field(gt=0)]
    noteTitle: StrictStr
    summary: Annotated[StrictStr, Field(min_length=1)]
    strategy: Literal["stuff", "refine"]
    segments: Annotated[StrictInt, Field(gt=0)]
    coverage: SummaryCoverage


def visible_summary(data, identifier):
    result = SummaryResult.model_validate(data)
    coverage = result.coverage
    if (result.noteId != identifier or coverage.end != coverage.total_chars
            or coverage.section_schema != PARSER_VERSION):
        raise ValueError("incomplete_summary_coverage")
    return result.model_dump()


def _source(identifier, config, budget, read):
    pages, offset, revision, schema, total, title = [], 0, "", "", None, ""
    source_tokens = 0
    while True:
        check_cancelled(config)
        budget.check_time()
        # The outer call accounts for page one; subsequent HTTP reads also spend tool allowance.
        if pages:
            budget.attempt_tools()
        result = read(identifier, view="source_range" if pages else "full_text",
                      doc_type="note", section="", offset=offset, max_chars=MAX_DOCUMENT_CHARS,
                      revision=revision, section_schema=schema)
        if not isinstance(result, ToolResult) or result.kind != "ok":
            raise ValueError("source_read_failed")
        data = result.data
        if not isinstance(data, dict):
            raise ValueError("source_read_failed")
        source = data.get("source") or {}
        body = data.get("noteContent")
        if not pages:
            revision, schema = data["contentRevision"], data["sectionSchema"]
            total = data["totalChars"]
            title = data.get("noteTitle", "文章")
        end = source.get("end")
        if (data.get("selectedScope") != "document" or source.get("section_id")
                or data.get("contentRevision") != revision or data.get("sectionSchema") != schema
                or data.get("totalChars") != total or source.get("offset") != offset
                or not isinstance(body, str) or not body or end != offset + len(body)
                or type(total) is not int or not offset < end <= total
                or data.get("readComplete") != (end == total)
                or data.get("nextOffset") != (None if end == total else end)):
            raise ValueError("incomplete_source_coverage")
        pages.append(body)
        record("article_summary", "source_page", article_id=identifier, offset=offset, end=end,
               total_chars=total, revision=revision)
        emit(config, f"正在读取全文：{end}/{total} 字符", key=f"summary:{identifier}:{end}")
        offset = end
        # Bound source collection before constructing model prompts, even for hostile page counts.
        source_tokens += estimate_tokens([HumanMessage(content=body)])
        if source_tokens > budget.limits.task_tokens - budget.tokens:
            raise BudgetExceeded("task_tokens")
        if end == total:
            return "".join(pages), title, {
                "complete": True, "start": 0, "end": end, "total_chars": total,
                "revision": revision, "section_schema": schema, "pages": len(pages)}


def summarize_article(identifier, instructions, config, *, read):
    budget = current_budget() or TaskBudget()
    try:
        body, title, coverage = _source(identifier, config, budget, read)
        check_cancelled(config)
        emit(config, "正在汇总全文…", key=f"summary:{identifier}")
        text, strategy, segments = synthesize(body, instructions, budget, config)
        data = visible_summary({"view": "summary", "noteId": identifier, "noteTitle": title,
            "summary": text, "strategy": strategy, "segments": segments,
            "coverage": coverage}, identifier)
    except (ValueError, TypeError, KeyError, OpenAIError, TimeoutError, BudgetExceeded) as error:
        reason = error.reason if isinstance(error, BudgetExceeded) else type(error).__name__
        record("article_summary", "incomplete", article_id=identifier, reason=reason)
        return unavailable("全文总结尚未完成：原文读取、版本校验或汇总预算未满足；"
                           "不能用已读片段或中间摘要代替完整总结。", meta={"reason": reason})
    check_cancelled(config)
    record("article_summary", "completed", article_id=identifier, strategy=strategy,
           segments=segments, **coverage)
    return ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)
