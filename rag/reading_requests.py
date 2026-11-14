"""Typed reading scope, separate from the host's source representation."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, StringConstraints

from rag.source import MAX_DOCUMENT_CHARS


class ArticleQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["question"]
    query: Annotated[StrictStr, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=500)]


class DocumentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["full_text", "source_range", "outline", "metadata"]
    section: Annotated[StrictStr, Field(max_length=1024)] = ""
    offset: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 0
    max_chars: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENT_CHARS)] = MAX_DOCUMENT_CHARS
    revision: Annotated[StrictStr, Field(max_length=64)] = ""
    section_schema: Annotated[StrictStr, Field(max_length=128)] = ""


ReadSelection = Annotated[ArticleQuestion | DocumentRead, Field(discriminator="kind")]


def is_question(args):
    return isinstance(args.get("selection"), dict) and args["selection"].get("kind") == "question"


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
    return {"article_id": args["article_id"], "doc_type": args.get("doc_type", "note"),
            "selection": {"kind": args["view"], **{k: v for k, v in args.items()
                if k not in {"article_id", "doc_type", "view"}}}}
