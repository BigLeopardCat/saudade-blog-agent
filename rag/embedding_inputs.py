"""Pinned local tokenizers and complete-input budgets for optional embeddings."""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, ConfigDict, Field

from config import settings
from rag.chunks import INDEX_VERSION, OVERLAP

logger = logging.getLogger(__name__)


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
    chunking_strategy: Literal["token-window-v2", "sentence-paragraph-v1"] = "token-window-v2"


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


def signature_payload(spec: ProfileSpec) -> bytes:
    """Canonical identity shared with independently deployed embedding consumers."""
    identity = spec.model_dump(exclude={"tokenizer_file"})
    if spec.unicode_normalization == "none":
        identity.pop("unicode_normalization")
    if spec.chunking_strategy == "token-window-v2":
        identity.pop("chunking_strategy")
    identity.update(
        tokenizers_version=version("tokenizers"),
        splitter=("tokenizers-offset-window-v2" if spec.chunking_strategy == "token-window-v2"
                  else "langchain-source-sentence-paragraph-v1"),
        chunking=INDEX_VERSION,
    )
    return json.dumps(identity, sort_keys=True).encode()


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
        signature = hashlib.sha256(signature_payload(spec)).hexdigest()
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
    complete = document_text(title, heading + body, profile)
    if profile.count(complete) <= profile.spec.max_tokens:
        return ((0, len(body), heading + body),)
    # Reserve full metadata and special tokens. HF Tokenizers owns token windows and
    # overlap; offsets project those windows onto the original, un-decoded source.
    budget = profile.spec.max_tokens - profile.count(document_text(title, heading, profile))
    if budget <= 0:
        raise InputProfileError("embedding metadata leaves no room for source text")
    if profile.spec.chunking_strategy == "sentence-paragraph-v1":
        return _sentence_windows(profile, title, heading, body, budget)
    return _source_windows(profile, title, heading, body, budget)


def _sentence_windows(profile, title, heading, body, budget):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=budget, chunk_overlap=min(OVERLAP, budget // 4),
        separators=["\r\n\r\n", "\n\n", "\r\n", "\n", "。", "！", "？",
                    ". ", "! ", "? ", "；", " ", ""],
        length_function=lambda text: len(profile.tokenizer.encode(
            text, add_special_tokens=False).ids),
        keep_separator="end", strip_whitespace=False, add_start_index=True)
    spans, previous_end = [], 0
    for document in splitter.create_documents([body]):
        start = document.metadata["start_index"]
        end = start + len(document.page_content)
        if start < 0 or start > previous_end or body[start:end] != document.page_content:
            # LangChain start-index recovery uses character overlap even with a
            # token length function. Never accept a gap from an ambiguous repeat.
            logger.debug("Sentence source-offset fallback, chars=%d", len(body))
            return _source_windows(profile, title, heading, body, budget)
        text = heading + document.page_content
        if profile.count(document_text(title, text, profile)) <= profile.spec.max_tokens:
            spans.append((start, end, text))
        else:
            spans.extend((start + low, start + high, value) for low, high, value in
                         _source_windows(profile, title, heading, document.page_content, budget))
        previous_end = end
    if previous_end != len(body):
        logger.debug("Sentence complete-coverage fallback, chars=%d", len(body))
        return _source_windows(profile, title, heading, body, budget)
    return tuple(spans)


def _source_windows(profile, title, heading, body, budget):
    # WordPiece continuations can expand at a source-window boundary. Reduce this
    # chunk's subdivision budget only after full-input validation fails, retaining text.
    while budget > 0:
        encoded = profile.tokenizer.encode(body, add_special_tokens=False)
        encoded.truncate(budget, stride=min(OVERLAP, budget // 4))
        windows = [encoded, *encoded.overflowing]
        spans, previous_end, excess = [], 0, 0
        for i, window in enumerate(windows):
            offsets = [(low, high) for low, high in window.offsets if high > low]
            if not offsets:
                raise InputProfileError("tokenizer returned no source offsets")
            start = min(offsets[0][0], previous_end)
            end = len(body) if i == len(windows) - 1 else offsets[-1][1]
            if start < 0 or end <= start or end > len(body):
                raise InputProfileError("tokenizer returned an invalid source range")
            text = heading + body[start:end]
            excess = max(excess, profile.count(document_text(title, text, profile))
                         - profile.spec.max_tokens)
            spans.append((start, end, text))
            previous_end = end
        if not excess:
            return tuple(spans)
        budget -= excess
    raise InputProfileError("embedding metadata leaves no room for source text")


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
