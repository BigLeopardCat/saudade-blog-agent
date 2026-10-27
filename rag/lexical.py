"""BM25S adapter; corpus visibility, tokenization and result metadata stay with the app."""

from importlib.metadata import version

METHOD = "lucene"
DTYPE = "float64"


def backend_info() -> dict:
    return {"name": "bm25s", "version": version("bm25s"), "method": METHOD, "dtype": DTYPE}


class LexicalIndex:
    """Build once and share a read-only sparse score index between queries."""

    def __init__(self, documents: list[list[str]], *, k1: float, b: float):
        self.model = None
        if any(documents):
            import bm25s

            self.model = bm25s.BM25(k1=k1, b=b, method=METHOD, dtype=DTYPE,
                                    backend="numpy", csc_backend="numpy")
            self.model.index(documents, show_progress=False)
        self.vocabulary = frozenset(self.model.vocab_dict if self.model is not None else ())

    def scores(self, query: list[str]) -> dict[int, float]:
        if self.model is None or not query:
            return {}
        return {i: float(score) for i, score in enumerate(self.model.get_scores(query))
                if score > 0}
