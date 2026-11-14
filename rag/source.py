"""Versioned source ranges shared by retrieval and public document reads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

Kind = Literal["note", "talk", "board", "announcement"]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class DocumentReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Kind
    id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
    revision: Digest
    section_schema: StrictStr


class SourceRange(DocumentReference):
    section_id: Annotated[StrictStr, Field(pattern=r"^(?:s[1-9][0-9]*)?$")]
    offset: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end < self.offset:
            raise ValueError("source range is reversed")
        return self


class BodyPassage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: SourceRange
    text: Annotated[StrictStr, Field(min_length=1, max_length=24000)]

    @model_validator(mode="after")
    def exact_source_length(self):
        from rag.sections import PARSER_VERSION

        if (
            self.source.section_schema != PARSER_VERSION
            or self.source.end - self.source.offset != len(self.text)
        ):
            raise ValueError("passage does not match its source range")
        return self
