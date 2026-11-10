"""Shared bilingual lexical analysis with source offsets for evidence excerpts."""

import threading
import unicodedata
from collections.abc import Iterator
from functools import cache
from importlib.metadata import version

import regex
from bm25s.stopwords import STOPWORDS_CHINESE, STOPWORDS_EN

POLICY_VERSION = "bilingual-search-v1"
_START = r"[\p{L}\p{N}--\p{Script=Han}]"
_CONTINUE = r"[\p{L}\p{M}\p{N}\p{Pc}--\p{Script=Han}]"
_UNITS = regex.compile(
    fr"\p{{Script=Han}}+|[.．_＿]?{_START}{_CONTINUE}*"
    fr"(?:[.．/／:：\-－]{_START}{_CONTINUE}*)*(?:[+＋]{{1,2}}|[#＃])?",
    regex.V1,
)
_PARTS = regex.compile(r"[._/:-]+")
_STOP_ZH = frozenset(STOPWORDS_CHINESE)
_STOP_EN = frozenset(STOPWORDS_EN)
_LOCAL = threading.local()


@cache
def _chinese():
    from jieba import Tokenizer

    tokenizer = Tokenizer()
    tokenizer.cache_file = f"saudade-jieba-{version('jieba')}.cache"
    tokenizer.initialize()
    return tokenizer


def _english(word):
    if not hasattr(_LOCAL, "stemmer"):
        from snowballstemmer.english_stemmer import EnglishStemmer

        _LOCAL.stemmer = EnglishStemmer()
    return _LOCAL.stemmer.stemWord(word) if word.isascii() and word.isalpha() else word


def _normalize(word):
    value = unicodedata.normalize("NFKC", word).casefold()
    return "".join(char for char in unicodedata.normalize("NFKD", value)
                   if unicodedata.category(char) != "Mn")


def _word_features(word):
    word = _normalize(word)
    if word in _STOP_EN:
        return
    if word.isalpha():
        yield _english(word)
        return
    yield word
    for part in dict.fromkeys(_PARTS.split(word)):
        if len(part) > 1 and part != word and part not in _STOP_EN:
            yield _english(part)


def analyzed(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield features and their original Python Unicode source spans.

    Whole technical identifiers retain punctuation. Chinese search segmentation
    adds bigrams for unknown words and adjacent single-character segments.
    Normalization changes features, never the source coordinates.
    """
    for match in _UNITS.finditer(text):
        word = match.group()
        if regex.fullmatch(r"\p{Script=Han}+", word):
            tokenizer = _chinese()
            previous_single = None
            for term, start, end in tokenizer.tokenize(word, mode="search"):
                feature = unicodedata.normalize("NFKC", term)
                if feature in _STOP_ZH:
                    previous_single = None
                    continue
                if end - start == 1:
                    if previous_single is not None and previous_single[1] == start:
                        low = previous_single[0]
                        yield (unicodedata.normalize("NFKC", word[low:end]),
                               match.start() + low, match.start() + end)
                    previous_single = (start, end)
                else:
                    previous_single = None
                yield feature, match.start() + start, match.start() + end
                if len(term) > 2 and not tokenizer.FREQ.get(term):
                    for offset in range(len(term) - 1):
                        gram = unicodedata.normalize("NFKC", term[offset:offset + 2])
                        if gram not in _STOP_ZH:
                            absolute = match.start() + start + offset
                            yield gram, absolute, absolute + 2
        else:
            for feature in _word_features(word):
                yield feature, match.start(), match.end()


def tokenize(text: str) -> list[str]:
    """Keep document term frequencies; query consumers may deduplicate features."""
    return [term for term, _, _ in analyzed(text)]


@cache
def policy_info() -> dict:
    return {"name": POLICY_VERSION, "jieba": version("jieba"),
            "snowballstemmer": version("snowballstemmer"), "regex": version("regex"),
            "unicode": unicodedata.unidata_version}
