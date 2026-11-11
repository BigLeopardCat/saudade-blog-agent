"""BM25S adapter; corpus visibility, tokenization and result metadata stay with the app."""

from functools import lru_cache
from importlib.metadata import version

from rag.tokenization import policy_info

METHOD = "lucene"
DTYPE = "float64"


def backend_info() -> dict:
    return {"name": "bm25s", "version": version("bm25s"), "method": METHOD, "dtype": DTYPE,
            "tokenizer": policy_info()}


class LexicalIndex:
    """Build once and share a read-only sparse score index between queries."""

    def __init__(self, documents: list[list[str]], *, k1: float, b: float):
        self.count = len(documents)
        snapshot = tuple(tuple(row) for row in documents)
        # A known article is its own passage corpus; unrelated articles must not
        # change its section ranking. Cache immutable subsets within this snapshot.
        self._scoped = lru_cache(maxsize=32)(lambda indices: LexicalIndex(
            [list(snapshot[i]) for i in indices], k1=k1, b=b))
        self.model = None
        if any(documents):
            import bm25s

            self.model = bm25s.BM25(k1=k1, b=b, method=METHOD, dtype=DTYPE,
                                    backend="numpy", csc_backend="numpy")
            self.model.index(documents, show_progress=False)
        self.vocabulary = frozenset(self.model.vocab_dict if self.model is not None else ())

    def scores(self, query: list[str], *,
               allowed_indices: list[int] | None = None) -> dict[int, float]:
        query = [term for term in query if term in self.vocabulary]
        if self.model is None or not query:
            return {}
        if allowed_indices is not None:
            if not allowed_indices:
                return {}
            indices = tuple(allowed_indices)
            return {indices[i]: score for i, score in self._scoped(indices).scores(query).items()}
        return {i: float(score)
                for i, score in enumerate(self.model.get_scores(query))
                if score > 0}
