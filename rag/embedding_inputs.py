"""Pinned local tokenizers and complete-input budgets for optional embeddings."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from config import settings
from rag.chunks import OVERLAP


class InputProfileError(ValueError):
    """Invalid profile or input; callers retain lexical retrieval."""


class ProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    model_revision: str = Field(min_length=1)
    tokenizer_file: str = Field(min_length=1)
    tokenizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_tokens: int = Field(gt=0)
    add_special_tokens: bool = True
    query_prefix: str = ""
    document_prefix: str = ""
    pooling: str = Field(min_length=1)
    normalization: str = Field(min_length=1)
    unicode_normalization: Literal["none", "NFC"] = "none"


@dataclass(frozen=True)
class InputProfile:
    spec: ProfileSpec
    signature: str
    tokenizer: Any = field(compare=False, hash=False, repr=False)

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=self.spec.add_special_tokens).ids)

    def normalize(self, text: str) -> str:
        return (
            unicodedata.normalize("NFC", text) if self.spec.unicode_normalization == "NFC" else text
        )

    def validate(self, text: str) -> str:
        if self.count(text) > self.spec.max_tokens:
            raise InputProfileError("embedding input exceeds configured token budget")
        return text


@lru_cache(maxsize=4)
def load_profile(filename: str) -> InputProfile:
    # Immutable files per deployment. Changing files at the same path requires restart.
    try:
        spec = ProfileSpec.model_validate_json(Path(filename).read_bytes())
        artifact = (Path(filename).resolve().parent / spec.tokenizer_file).read_bytes()
        if hashlib.sha256(artifact).hexdigest() != spec.tokenizer_sha256:
            raise InputProfileError("tokenizer SHA-256 mismatch")
        from tokenizers import Tokenizer

        try:
            tokenizer = Tokenizer.from_str(artifact.decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            # The Rust binding raises plain Exception for invalid tokenizer schemas.
            raise InputProfileError("invalid tokenizer artifact") from error
        tokenizer.no_truncation()
        tokenizer.no_padding()
        identity = spec.model_dump(exclude={"tokenizer_file"})
        if spec.unicode_normalization == "none":
            identity.pop("unicode_normalization")  # Preserve existing profile namespaces.
        identity.update(tokenizers_version=version("tokenizers"), splitter="source-char-window-v1")
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return InputProfile(spec, signature, tokenizer)
    except (OSError, ValueError, ImportError) as error:
        raise InputProfileError("cannot load pinned embedding profile/tokenizer") from error


def active_profile() -> InputProfile | None:
    filename = settings.embedding_profile.strip()
    return load_profile(filename) if filename else None


def document_text(title: str, text: str, profile: InputProfile | None) -> str:
    value = f"{title[:400]}\n{text}"
    return profile.normalize(profile.spec.document_prefix + value) if profile else value


def query_text(query: str, profile: InputProfile | None) -> str:
    value = profile.spec.query_prefix + query if profile else query
    return profile.validate(profile.normalize(value)) if profile else value


def validate_inputs(texts: list[str], signature: str) -> None:
    profile = active_profile()
    if signature != (profile.signature if profile else ""):
        raise InputProfileError("embedding profile changed during request")
    if profile:
        for text in texts:
            profile.validate(text)


@lru_cache(maxsize=2048)
def _ranges(profile: InputProfile, title: str, heading: str, body: str) -> tuple:
    def fits(value: str) -> bool:
        return (
            profile.count(document_text(title, heading + value, profile)) <= profile.spec.max_tokens
        )

    if fits(body):
        return ((0, len(body), heading + body),)
    spans, start = [], 0
    while start < len(body):
        low, high = 0, len(body) - start
        while low < high:
            middle = (low + high + 1) // 2
            if fits(body[start : start + middle]):
                low = middle
            else:
                high = middle - 1
        if not low:
            raise InputProfileError("embedding metadata leaves no room for source text")
        end = start + low
        # Character offsets slice the original string; no token decoding or Unicode loss.
        profile.validate(document_text(title, heading + body[start:end], profile))
        spans.append((start, end, heading + body[start:end]))
        if end == len(body):
            break
        start = end - min(OVERLAP, low // 4)
    return tuple(spans)


def vector_chunks(chunks: list[dict]) -> list[dict]:
    """Refine lexical chunks for embeddings, retaining document/section source ranges."""
    profile = active_profile()
    if profile is None:
        return chunks
    result = []
    for chunk in chunks:
        text, title = chunk["text"], chunk.get("title", "")
        boundary = chunk.get("prefix_chars", 0)
        heading, body = text[:boundary], text[boundary:]
        spans = _ranges(profile, title, heading, body)
        if len(spans) == 1 and spans[0][2] == text:
            result.append(chunk)
            continue
        if not spans:
            raise InputProfileError("embedding metadata exceeds token budget")
        for low, high, value in spans:
            start = chunk.get("start", 0) + low
            end = chunk.get("start", 0) + high
            identity = f"{chunk.get('section_id', chunk['section'])}:{start}:{end}:{value}"
            result.append(
                {
                    **chunk,
                    "text": value,
                    "start": start,
                    "end": end,
                    "chunk_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                }
            )
    return result
