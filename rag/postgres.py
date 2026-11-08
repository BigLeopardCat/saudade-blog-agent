"""PostgreSQL corpus membership and pgvector distance queries.

Embeddings are computed before entering this repository. A short transaction
publishes complete membership; failed builds cannot replace the active corpus.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url

SCHEMA = "rag"
metadata = MetaData(schema=SCHEMA)
embeddings = Table(
    "embeddings", metadata,
    Column("collection", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("dimension", Integer, primary_key=True),
    Column("space", String, nullable=False),
    Column("embedding", Vector(), nullable=False),
    CheckConstraint("dimension > 0 AND vector_dims(embedding) = dimension"),
)
members = Table(
    "members", metadata,
    Column("collection", String, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("key", String, nullable=False),
    Column("dimension", Integer, nullable=False),
    Column("document_type", String, nullable=False),
    Column("document_id", String, nullable=False),
    ForeignKeyConstraint(
        ["collection", "key", "dimension"],
        ["rag.embeddings.collection", "rag.embeddings.key", "rag.embeddings.dimension"],
    ),
)
snapshots = Table(
    "snapshots", metadata,
    Column("collection", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
    Column("space", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    Column("dimension", Integer, nullable=False),
    Column("count", Integer, nullable=False),
    Column("stats", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
)


class StaleSnapshot(ValueError):
    """The reader's corpus no longer matches the published database membership."""


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    space: dict
    dimension: int
    count: int
    stats: dict


class PostgresRepository:
    """One bounded pool; runtime access never creates tables or extensions."""

    def __init__(self, dsn: str, collection: str, *, pool_size: int = 4):
        if type(pool_size) is not int or pool_size < 2:
            raise ValueError("RAG vector pool requires at least two connections")
        if not dsn.strip() or not collection or len(collection) > 128:
            raise ValueError("RAG vector DSN and bounded collection name are required")
        url = make_url(dsn)
        if url.get_backend_name() != "postgresql":
            raise ValueError("RAG vector storage requires PostgreSQL")
        self.engine = create_engine(
            url.set(drivername="postgresql+psycopg"),
            pool_size=pool_size, max_overflow=0, pool_timeout=3, pool_pre_ping=True,
            connect_args={"connect_timeout": 3, "options": "-c statement_timeout=10000"},
        )
        self.collection = collection

    def close(self):
        self.engine.dispose()

    def setup(self):
        """Explicit operator migration; requires extension/schema ownership."""
        with self.engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS rag"))
            metadata.create_all(connection)

    @contextmanager
    def writer(self):
        """A session lock serializes builders without holding a transaction over I/O."""
        lock = int.from_bytes(hashlib.sha256(self.collection.encode()).digest()[:8],
                              "big", signed=True)
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            acquired = connection.scalar(select(func.pg_try_advisory_lock(lock)))
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    connection.execute(select(func.pg_advisory_unlock(lock)))

    def snapshot(self) -> Snapshot | None:
        with self.engine.connect() as connection:
            count = select(func.count()).where(
                members.c.collection == snapshots.c.collection).scalar_subquery()
            row = connection.execute(select(snapshots, count.label("member_count")).where(
                snapshots.c.collection == self.collection)).mappings().first()
            if row is not None and row["count"] != row["member_count"]:
                raise ValueError("Vector membership is incomplete")
        return None if row is None else Snapshot(**{
            name: row[name] for name in ("fingerprint", "space", "dimension", "count", "stats")})

    def available(self, space: str, dimension: int = 0) -> dict[str, int]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(embeddings.c.key, embeddings.c.dimension).where(
                embeddings.c.collection == self.collection, embeddings.c.space == space,
                embeddings.c.dimension == dimension if dimension else text("TRUE")))
            return {key: size for key, size in rows}

    def commit(self, *, rows: list[dict], chunks: list[dict], keys: list[str], space: dict,
               space_key: str, fingerprint: str, dimension: int, stats: dict,
               complete: bool):
        """Cache successful inputs and atomically replace only complete corpus snapshots."""
        with self.engine.begin() as connection:
            if rows:
                connection.execute(insert(embeddings).on_conflict_do_nothing(), [
                    {**row, "collection": self.collection, "space": space_key}
                    for row in rows])
            if not complete:
                self._prune_pending(connection, space_key, keys, dimension)
                return
            connection.execute(delete(members).where(members.c.collection == self.collection))
            if chunks:
                connection.execute(insert(members), [
                    {"collection": self.collection, "ordinal": index, "key": key,
                     "dimension": dimension, "document_type": chunk["type"],
                     "document_id": str(chunk["id"])}
                    for index, (chunk, key) in enumerate(zip(chunks, keys, strict=True))])
            row = {"collection": self.collection, "fingerprint": fingerprint, "space": space,
                   "dimension": dimension, "count": len(chunks), "stats": stats}
            connection.execute(insert(snapshots).values(**row).on_conflict_do_update(
                index_elements=[snapshots.c.collection], set_=row))
            # The collection contains only the current public corpus and active embedding space.
            active = select(members.c.key).where(
                members.c.collection == self.collection,
                members.c.dimension == embeddings.c.dimension)
            connection.execute(delete(embeddings).where(
                embeddings.c.collection == self.collection,
                ~embeddings.c.key.in_(active)))

    def _prune_pending(self, connection, space_key, keys, dimension):
        active = select(members.c.key).where(
            members.c.collection == embeddings.c.collection,
            members.c.key == embeddings.c.key,
            members.c.dimension == embeddings.c.dimension,
        ).exists()
        target = and_(embeddings.c.space == space_key, embeddings.c.key.in_(keys),
                      embeddings.c.dimension == dimension)
        connection.execute(delete(embeddings).where(
            embeddings.c.collection == self.collection, ~active, ~target))

    def scores(self, snapshot: Snapshot, query: list[float], top_k: int) -> dict[int, float]:
        """Exact cosine in PostgreSQL, limiting documents rather than chunks."""
        if len(query) != snapshot.dimension or top_k < 1:
            raise ValueError("Invalid vector dimension or document window")
        # Repeatable read keeps membership and its fingerprint in one MVCC snapshot.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                current = conn.execute(select(snapshots.c.fingerprint, snapshots.c.space).where(
                    snapshots.c.collection == self.collection)).first()
                if current is None or current != (snapshot.fingerprint, snapshot.space):
                    raise StaleSnapshot("Published vector corpus changed")
                if snapshot.count == 0:
                    return {}
                distance = embeddings.c.embedding.cosine_distance(query)
                candidates = select(
                    members.c.ordinal, members.c.document_type, members.c.document_id,
                    distance.label("distance"),
                ).select_from(members.join(embeddings, and_(
                    members.c.collection == embeddings.c.collection,
                    members.c.key == embeddings.c.key,
                    members.c.dimension == embeddings.c.dimension,
                ))).where(members.c.collection == self.collection,
                          members.c.dimension == snapshot.dimension).cte("distances")
                documents = select(
                    candidates.c.document_type, candidates.c.document_id,
                ).where(candidates.c.distance < 1).group_by(
                    candidates.c.document_type, candidates.c.document_id,
                ).order_by(func.min(candidates.c.distance), func.min(candidates.c.ordinal)).limit(
                    top_k).cte("documents")
                statement = select(candidates.c.ordinal, (1 - candidates.c.distance)).join(
                    documents, and_(candidates.c.document_type == documents.c.document_type,
                                    candidates.c.document_id == documents.c.document_id),
                ).where(candidates.c.distance < 1).order_by(candidates.c.ordinal)
                return {ordinal: float(score) for ordinal, score in conn.execute(statement)}
