"""Typed reading scope, separate from the host's source representation."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, StringConstraints

from rag.source import MAX_DOCUMENT_CHARS


class ArticleQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["question"]
    query: Annotated[StrictStr, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=500)]


class ArticleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["summary"]
    instructions: Annotated[StrictStr, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=1000)] = "按原文结构总结全文。"


class SourceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Annotated[StrictStr, Field(max_length=64)] = ""
    section_schema: Annotated[StrictStr, Field(max_length=128)] = ""


class FullTextRead(SourceVersion):
    kind: Literal["full_text"]
    max_chars: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENT_CHARS)] = MAX_DOCUMENT_CHARS


class SourceRangeRead(SourceVersion):
    kind: Literal["source_range"]
    section: Annotated[StrictStr, Field(max_length=1024)] = ""
    offset: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 0
    max_chars: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENT_CHARS)] = MAX_DOCUMENT_CHARS


class OutlineRead(SourceVersion):
    kind: Literal["outline"]
    offset: Annotated[StrictInt, Field(
        ge=0, le=2_147_483_647, description="Use the returned nextSectionsOffset for paging")] = 0


class MetadataRead(SourceVersion):
    kind: Literal["metadata"]


ReadSelection = Annotated[
    ArticleQuestion | ArticleSummary | FullTextRead | SourceRangeRead | OutlineRead | MetadataRead,
    Field(discriminator="kind")]


def is_question(args):
    return isinstance(args.get("selection"), dict) and args["selection"].get("kind") == "question"


def is_summary(args):
    return isinstance(args.get("selection"), dict) and args["selection"].get("kind") == "summary"


def document_args(args):
    """Project tool arguments for source-coordinate binding, not execution routing."""
    selected = args.get("selection")
    if not isinstance(selected, dict) or selected.get("kind") not in {
        "full_text", "source_range", "outline", "metadata"
    }:
        return None
    return {"article_id": args.get("article_id"), "doc_type": args.get("doc_type", "note"),
            "view": selected["kind"], **{k: v for k, v in selected.items() if k != "kind"}}


def tool_args(args):
    """Publish a source read as the tool's typed request."""
    selected = {k: v for k, v in args.items() if k not in {"article_id", "doc_type", "view"}}
    defaults = {"section": "", "offset": 0, "max_chars": MAX_DOCUMENT_CHARS}
    unused = {"full_text": ("section", "offset"), "outline": ("section", "max_chars"),
              "metadata": ("section", "offset", "max_chars")}
    for key in unused.get(args["view"], ()):
        if type(selected.get(key)) is type(defaults[key]) and selected[key] == defaults[key]:
            selected.pop(key)
    return {"article_id": args["article_id"], "doc_type": args.get("doc_type", "note"),
            "selection": {"kind": args["view"], **selected}}
