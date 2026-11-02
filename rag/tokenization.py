"""Library analyzers with the existing CJK n-gram and ASCII word policy."""

import re

from sklearn.feature_extraction.text import CountVectorizer

# Language boundaries are application configuration; token/gram generation is sklearn's.
_CJK_RUN = re.compile(r"[一-鿿]+")
_CJK_ANALYZE = CountVectorizer(analyzer="char", ngram_range=(2, 3)).build_analyzer()
_ASCII_PATTERN = r"[a-zA-Z0-9_\.]+"
_ASCII_ANALYZE = CountVectorizer(token_pattern=_ASCII_PATTERN).build_analyzer()


def tokenize(text: str) -> list[str]:
    """Keep corpus frequencies; callers decide whether to deduplicate query features."""
    return [token for run in _CJK_RUN.findall(text) for token in _CJK_ANALYZE(run)] + list(
        _ASCII_ANALYZE(text)
    )
