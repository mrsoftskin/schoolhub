"""SQLite storage. One file holds chunks, conversations, messages, events,
plus two bookkeeping tables (files, index_status) that make incremental
indexing and the Library tab possible.

Connections are cheap; callers open one per logical operation via connect().
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    collection   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    locator      TEXT NOT NULL,
    text         TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_collection ON chunks(collection);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_path);

CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    collection  TEXT NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    indexed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_collection ON files(collection);

CREATE TABLE IF NOT EXISTS index_status (
    collection    TEXT PRIMARY KEY,
    last_indexed  TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    failures_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY,
    collection TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    citations_json  TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS events (
    id        TEXT PRIMARY KEY,
    course    TEXT NOT NULL,
    title     TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at   TEXT,
    all_day   INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL CHECK (kind IN ('exam', 'project', 'quiz', 'recurring', 'admin')),
    source    TEXT NOT NULL CHECK (source IN ('ics', 'csv', 'recurring'))
);
CREATE INDEX IF NOT EXISTS idx_events_starts ON events(starts_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs sync endpoints and streaming
    # generators on a worker threadpool, and successive iterations of one
    # generator can land on different threads - which the default setting
    # rejects outright, aborting the answer mid-stream.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait for a writer instead of failing instantly: an indexing run holds
    # the write lock, and a chat answer must not be lost because of it.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    return conn
