"""Validate the host's bounded document view before recording read evidence."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from agent.domains.article_metadata import tag_references
from rag import sections
from rag.source import MAX_DOCUMENT_CHARS, Digest, DocumentReference, Kind, SourceRange

Count = Annotated[StrictInt, Field(ge=0)]


class Section(BaseModel):
    id: Annotated[StrictStr, Field(pattern=r"^s[1-9][0-9]*$")]
    title: Annotated[StrictStr, Field(max_length=200)]
    path: Annotated[list[Annotated[StrictStr, Field(max_length=200)]], Field(max_length=6)]


class DisplayMetadata(BaseModel):
    authorName: Annotated[StrictStr, Field(max_length=80)] | None = None
    authorNameTruncated: StrictBool | None = None
    authorAvatar: Annotated[StrictStr, Field(max_length=2048)] | None = None
    authorAvatarTruncated: StrictBool | None = None
    description: Annotated[StrictStr, Field(max_length=2000)] | None = None
    descriptionTruncated: StrictBool | None = None
    cover: Annotated[StrictStr, Field(max_length=2048)] | None = None
    coverTruncated: StrictBool | None = None
    coverFit: Literal["cover", "contain"] | None = None
    carouselFit: Literal["cover", "contain"] | None = None
    coverFocusX: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    coverFocusY: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    coverZoom: Annotated[StrictFloat, Field(ge=1, le=4, allow_inf_nan=False)] | None = None
    carouselFocusX: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    carouselFocusY: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    carouselZoom: Annotated[StrictFloat, Field(ge=1, le=4, allow_inf_nan=False)] | None = None


class MetadataPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["metadata"]
    type: Kind
    id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
    title: Annotated[StrictStr, Field(max_length=255)]
    contentRevision: Digest
    sectionSchema: StrictStr
    readComplete: StrictBool
    source: None
    metadata: dict

    @model_validator(mode="after")
    def no_body_evidence(self):
        if self.readComplete or self.sectionSchema != sections.PARSER_VERSION:
            raise ValueError("metadata cannot establish a body read")
        return self


class Page(BaseModel):
    view: Literal["body", "outline"]
    type: Kind
    id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
    title: Annotated[StrictStr, Field(max_length=255)]
    content: Annotated[StrictStr, Field(max_length=MAX_DOCUMENT_CHARS)]
    contentRevision: Digest
    sectionSchema: StrictStr
    readOffset: Count
    nextOffset: Count | None
    totalChars: Count
    readComplete: StrictBool
    sections: Annotated[list[Section], Field(max_length=12)]
    sectionCount: Count
    sectionsOffset: Count
    nextSectionsOffset: Count | None
    source: SourceRange | None
    metadata: dict = Field(default_factory=dict)
    readSection: StrictStr | None = None
    note: StrictStr | None = None

    @model_validator(mode="after")
    def consistent_range(self):
        if (
            self.sectionSchema != sections.PARSER_VERSION
            or self.sectionCount < len(self.sections)
            or len({row.id for row in self.sections}) != len(self.sections)
        ):
            raise ValueError("unsupported section view")
        section_end = self.sectionsOffset + len(self.sections)
        if (
            section_end > self.sectionCount
            or self.nextSectionsOffset != (section_end if section_end < self.sectionCount else None)
            or not self.sections and self.nextSectionsOffset is not None
            or self.view == "body" and self.sectionsOffset != 0
            or self.view == "outline" and (self.readOffset != 0 or self.source is not None)
        ):
            raise ValueError("inconsistent section directory range")
        if self.source is None:
            if self.content or self.readComplete or self.nextOffset is not None:
                raise ValueError("missing read provenance")
            return self
        source = self.source
        end = self.readOffset + len(self.content)
        if (
            (
                source.type,
                source.id,
                source.revision,
                source.section_schema,
                source.offset,
                source.end,
            )
            != (self.type, self.id, self.contentRevision, self.sectionSchema, self.readOffset, end)
            or end > self.totalChars
            or self.readComplete != (end == self.totalChars)
            or self.nextOffset != (None if self.readComplete else end)
            or not self.content
            and not self.readComplete
        ):
            raise ValueError("inconsistent document range")
        return self


def _article_metadata(metadata):
    data = {}
    data.update(DisplayMetadata.model_validate(metadata).model_dump(exclude_unset=True))
    try:
        references = tag_references(metadata.get("tags"))
    except ValueError:
        references = None
    # Metadata is descriptive; the body and source coordinates remain independently verified.
    data.update(
        {
            key: metadata[key]
            for key in (
                "status",
                "isPublic",
                "isTop",
                "categoryId",
                "categoryName",
                "createdAt",
                "updatedAt",
                "tags",
                "tagsTruncated",
            )
            if key in metadata
        }
    )
    data["tags"] = references
    if references is None:
        data.pop("tagsTruncated", None)
    return data


def body_continuation(data, identifier):
    """Extract a cursor only from the validated body range actually shown to the model."""
    try:
        source = SourceRange.model_validate(data.get("source"))
    except ValueError:
        return None
    body = data.get("noteContent")
    if (data.get("view") != "body" or source.type != "note" or source.id != identifier
            or source.section_id or source.section_schema != sections.PARSER_VERSION
            or data.get("contentRevision") != source.revision
            or data.get("sectionSchema") != source.section_schema
            or type(data.get("readOffset")) is not int or data["readOffset"] != source.offset
            or data.get("readComplete") is not False
            or not isinstance(body, str) or not body or source.end - source.offset != len(body)
            or type(data.get("nextOffset")) is not int or data["nextOffset"] != source.end
            or type(data.get("totalChars")) is not int or not 0 < source.end < data["totalChars"]):
        return None
    return {"offset": source.end, "revision": source.revision,
            "section_schema": source.section_schema}


def directory_targets(data, identifier):
    """Bind only sections and directory cursors actually visible in a validated page."""
    if data.get("noteId") != identifier or data.get("view") not in {"body", "outline"}:
        return []
    try:
        reference = DocumentReference(type="note", id=identifier,
                                      revision=data["contentRevision"],
                                      section_schema=data["sectionSchema"])
        rows = [Section.model_validate(row) for row in data["sections"]]
        start, count = data["sectionsOffset"], data["sectionCount"]
        if (reference.section_schema != sections.PARSER_VERSION
                or type(start) is not int or type(count) is not int
                or not 0 <= start <= start + len(rows) <= count):
            return []
    except (KeyError, TypeError, ValueError):
        return []
    base = {"article_id": identifier, "doc_type": "note",
            "revision": reference.revision, "section_schema": reference.section_schema}
    targets = [{**base, "view": "body", "section": row.id, "offset": 0} for row in rows]
    next_offset = data.get("nextSectionsOffset")
    if (type(next_offset) is int and rows and next_offset == start + len(rows)
            and next_offset < count):
        targets.append({**base, "view": "outline", "section": "", "offset": next_offset})
    return targets


def project(value, kind, identifier, section, offset, limit, revision, section_schema, view="body"):
    page = (MetadataPage if view == "metadata" else Page).model_validate(value)
    if (
        (page.type, page.id, page.view) != (kind, identifier, view)
        or view == "metadata" and bool(section or offset)
        or isinstance(page, Page) and (
            (page.readOffset if view == "body" else page.sectionsOffset) != offset
            or len(page.content) > limit)
        or revision
        and page.contentRevision != revision
        or section_schema
        and page.sectionSchema != section_schema
        or not section
        and page.source
        and page.source.section_id
        or section.startswith("s")
        and section[1:].isdigit()
        and page.source
        and page.source.section_id != section
    ):
        raise ValueError("document response differs from the request")
    data = page.model_dump(
        exclude={"metadata", "content", "type", "id", "title"}, exclude_none=False
    )
    if kind == "note" and view == "outline":
        data.update(noteId=page.id, noteTitle=page.title)
    elif kind == "note":
        scoped_body = view == "body" and page.source is not None and (offset > 0 or bool(section))
        if scoped_body:
            # Selected body ranges retain their exact content and provenance.
            # Display metadata and the full directory have dedicated views.
            for key in ("sections", "sectionCount", "sectionsOffset", "nextSectionsOffset"):
                data.pop(key, None)
            data["omittedViews"] = ["metadata", "outline"]
        else:
            data.update(_article_metadata(page.metadata))
        if view == "body" and page.source is not None:
            data["selectedScope"] = "section_subtree" if page.source.section_id else "document"
        data.update(
            noteId=page.id,
            noteTitle=page.title,
            **({"sectionText" if section else "noteContent": page.content}
               if view == "body" else {}),
        )
    else:
        data.update(id=page.id, title=page.title,
                    **({"content": page.content} if isinstance(page, Page) else {}))
    return data
