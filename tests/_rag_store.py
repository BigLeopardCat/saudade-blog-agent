"""Test-only storage double; PostgreSQL semantics are verified in integration tests."""

import copy
from contextlib import nullcontext

import numpy as np
import pytest

from config import settings
from rag import vector_index
from rag.postgres import Snapshot, StaleSnapshot

_repositories = {}


@pytest.fixture(autouse=True)
def memory_vectors(monkeypatch):
    monkeypatch.setattr(settings, "rag_vector_dsn", "postgresql://offline.invalid/rag")
    monkeypatch.setattr(vector_index, "_store", None)
    monkeypatch.setattr(vector_index, "_query_cache", {})
    monkeypatch.setattr(vector_index, "_query_order", [])
    _repositories.clear()
    yield
    _repositories.clear()


class MemoryRepository:
    def __init__(self):
        self.rows = {}
        self.active = None
        self.chunks = []
        self.keys = []

    def close(self):
        pass

    def writer(self):
        return nullcontext(True)

    def snapshot(self):
        return self.active

    def available(self, space, dimension=0):
        return {key: dim for (key, dim), row in self.rows.items() if row["space"] == space
                and (not dimension or dim == dimension)}

    def commit(self, *, rows, chunks, keys, space, space_key, fingerprint,
               dimension, stats, complete):
        for row in rows:
            self.rows[(row["key"], row["dimension"])] = {**row, "space": space_key}
        if complete:
            self.keys, self.chunks = list(keys), copy.deepcopy(chunks)
            self.active = Snapshot(fingerprint, space, dimension, len(chunks), stats)
            self.rows = {key: value for key, value in self.rows.items()
                         if key[0] in keys and key[1] == dimension}

    def scores(self, snapshot, query, top_k):
        if snapshot != self.active:
            raise StaleSnapshot("test snapshot changed")
        scores = {}
        for ordinal, key in enumerate(self.keys):
            vector = self.rows[(key, snapshot.dimension)]["embedding"]
            score = float(np.dot(query, vector) / (np.linalg.norm(query) * np.linalg.norm(vector)))
            if score > 0:
                scores[ordinal] = score
        documents = {}
        for ordinal in sorted(scores, key=lambda i: -scores[i]):
            chunk = self.chunks[ordinal]
            documents.setdefault((chunk["type"], chunk["id"]), None)
        identities = set(list(documents)[:top_k])
        return {i: score for i, score in scores.items()
                if (self.chunks[i]["type"], self.chunks[i]["id"]) in identities}


def memory_store(path):
    repository = _repositories.setdefault(str(path), MemoryRepository())
    return vector_index.VectorStore(repository)
