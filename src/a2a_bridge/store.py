"""Persistence for the conversation-id -> A2A contextId mapping.

Why this exists at all: A2A servers commonly bind session state to `contextId`,
including anything the agent has decided about the caller — history, entitlement,
subscription. Lose the mapping and the user silently starts over. In a demo that
looks like a gate firing twice; in production it looks like data loss.

Three backends because deployments differ, and the choice is not the bridge's
business: `memory://` for tests, `sqlite:///path` for a single container with a
volume, `mongodb://...` when an operator already runs Mongo and wants the map
joinable against their own records.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from urllib.parse import urlparse


class ContextStore(ABC):
    @abstractmethod
    def get(self, agent_id: str, conversation_id: str) -> str | None: ...

    @abstractmethod
    def put(self, agent_id: str, conversation_id: str, context_id: str) -> None: ...

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class MemoryStore(ContextStore):
    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str, conversation_id: str) -> str | None:
        with self._lock:
            return self._d.get((agent_id, conversation_id))

    def put(self, agent_id: str, conversation_id: str, context_id: str) -> None:
        with self._lock:
            self._d[(agent_id, conversation_id)] = context_id


class SqliteStore(ContextStore):
    def __init__(self, path: str) -> None:
        # check_same_thread=False because the ASGI server touches this from a
        # worker thread; every write is guarded by the lock below.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS context_map (
                       agent_id        TEXT NOT NULL,
                       conversation_id TEXT NOT NULL,
                       context_id      TEXT NOT NULL,
                       created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                       PRIMARY KEY (agent_id, conversation_id)
                   )"""
            )
            self._conn.commit()

    def get(self, agent_id: str, conversation_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT context_id FROM context_map WHERE agent_id=? AND conversation_id=?",
                (agent_id, conversation_id),
            ).fetchone()
        return row[0] if row else None

    def put(self, agent_id: str, conversation_id: str, context_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO context_map (agent_id, conversation_id, context_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(agent_id, conversation_id) DO UPDATE SET context_id=excluded.context_id""",
                (agent_id, conversation_id, context_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class MongoStore(ContextStore):
    """Keeps the map in its own collection.

    Never writes into collections owned by the chat client. Foreign fields in
    someone else's schema are one migration away from being dropped, and the
    join works fine from a sidecar collection.
    """

    def __init__(self, url: str, *, database: str | None = None, collection: str = "a2a_context_map") -> None:
        from pymongo import MongoClient

        self._client = MongoClient(url)
        db = self._client.get_default_database() if database is None else self._client[database]
        self._col = db[collection]
        self._col.create_index([("agent_id", 1), ("conversation_id", 1)], unique=True)

    def get(self, agent_id: str, conversation_id: str) -> str | None:
        doc = self._col.find_one(
            {"agent_id": agent_id, "conversation_id": conversation_id}, {"context_id": 1}
        )
        return doc.get("context_id") if doc else None

    def put(self, agent_id: str, conversation_id: str, context_id: str) -> None:
        self._col.update_one(
            {"agent_id": agent_id, "conversation_id": conversation_id},
            {"$set": {"context_id": context_id}, "$currentDate": {"updated_at": True}},
            upsert=True,
        )

    def close(self) -> None:
        self._client.close()


def build_store(url: str) -> ContextStore:
    scheme = urlparse(url).scheme
    if scheme in ("", "memory"):
        return MemoryStore()
    if scheme == "sqlite":
        # sqlite:///abs/path.db  ->  /abs/path.db
        return SqliteStore(url.replace("sqlite://", "", 1) or ":memory:")
    if scheme in ("mongodb", "mongodb+srv"):
        return MongoStore(url)
    raise ValueError(f"unsupported store url: {url!r}")
