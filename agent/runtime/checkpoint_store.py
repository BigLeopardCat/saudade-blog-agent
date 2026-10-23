"""Delete fences and native checkpoint history pruning for the runtime store."""

import logging

logger = logging.getLogger(__name__)


class ConversationDeleted(Exception):
    """A deleted conversation cannot accept late or queued turns."""


def postgres(saver):
    return type(saver).__module__.startswith("langgraph.checkpoint.postgres")


def setup(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_deleted_threads (
        thread_id TEXT PRIMARY KEY,
        deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")


def mark_deleted(saver, thread_id):
    if postgres(saver):
        with saver.conn.connection() as conn:
            conn.execute("INSERT INTO agent_deleted_threads(thread_id) VALUES (%s) "
                         "ON CONFLICT (thread_id) DO NOTHING", (thread_id,))
    else:
        if not hasattr(saver, "_deleted_threads"):
            saver._deleted_threads = set()
        saver._deleted_threads.add(thread_id)


def require_active(saver, thread_id):
    if postgres(saver):
        with saver.conn.connection() as conn:
            found = conn.execute("SELECT 1 FROM agent_deleted_threads WHERE thread_id=%s",
                                 (thread_id,)).fetchone()
    else:
        found = thread_id in getattr(saver, "_deleted_threads", ())
    if found:
        raise ConversationDeleted("会话已删除，请新建会话后再试。")


def prune(saver, thread_id):
    if postgres(saver):
        import psycopg
        try:
            saver.prune([thread_id], strategy="keep_latest")
        except (psycopg.Error, NotImplementedError):
            logger.exception("checkpoint 历史裁剪失败，保留恢复状态 thread=%s", thread_id)
