"""Shared bilingual lexical analysis with source offsets for evidence excerpts."""

import threading
import unicodedata
from collections.abc import Iterator
from functools import cache
from importlib.metadata import version

import regex
from bm25s.stopwords import STOPWORDS_CHINESE, STOPWORDS_EN_PLUS

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
_STOP_EN = frozenset(STOPWORDS_EN_PLUS)
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
    if word.isalpha():
        yield _english(word)
        return
    yield word
    for part in dict.fromkeys(_PARTS.split(word)):
        if len(part) > 1 and part != word and part not in _STOP_EN:
            yield _english(part)


def _chinese_features(word):
    seen = set()
    for term, start, end in _chinese().tokenize(word, mode="search"):
        feature = (unicodedata.normalize("NFKC", term), start, end)
        if feature not in seen:
            seen.add(feature)
            yield feature
    for start in range(len(word) - 1):
        term = unicodedata.normalize("NFKC", word[start:start + 2])
        feature = (term, start, start + 2)
        if term not in _STOP_ZH and feature not in seen:
            yield feature


def analyzed(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield features and their original Python Unicode source spans.

    Technical identifiers retain punctuation. Chinese search words have a bounded
    bigram backoff, deduplicated by occurrence to preserve document frequencies.
    Normalization changes features, never the source coordinates.
    """
    for match in _UNITS.finditer(text):
        word = match.group()
        if regex.fullmatch(r"\p{Script=Han}+", word):
            for feature, start, end in _chinese_features(word):
                yield feature, match.start() + start, match.start() + end
        else:
            for feature in _word_features(word):
                yield feature, match.start(), match.end()


def tokenize(text: str) -> list[str]:
    """Keep document term frequencies; query consumers may deduplicate features."""
    return [term for term, _, _ in analyzed(text)]


def query_features(text: str) -> list[str]:
    """Prefer content terms without making an all-stopword title unsearchable."""
    features = tokenize(text)
    if not hasattr(_LOCAL, "stop_features"):
        _LOCAL.stop_features = frozenset(_english(word) for word in _STOP_EN)
    content = [term for term in features
               if term not in _STOP_ZH and term not in _LOCAL.stop_features]
    return content or features


@cache
def policy_info() -> dict:
    return {"name": POLICY_VERSION, "jieba": version("jieba"),
            "snowballstemmer": version("snowballstemmer"), "regex": version("regex"),
            "unicode": unicodedata.unidata_version}
