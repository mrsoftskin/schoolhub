"""Conversation and message persistence for the Chat tab.

A conversation belongs to exactly one collection (or 'all') for its whole
life - there is no move operation, by design.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Connection

from .errors import BrainError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_conversation(conn: Connection, collection: str, title: str = "New conversation") -> dict:
    now = _now()
    cur = conn.execute(
        "INSERT INTO conversations (collection, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (collection, title, now, now),
    )
    conn.commit()
    return get_conversation(conn, cur.lastrowid)


def get_conversation(conn: Connection, conversation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise BrainError(f"Conversation {conversation_id} does not exist")
    return dict(row)


def list_conversations(conn: Connection, collection: str | None = None) -> list[dict]:
    if collection:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE collection = ? ORDER BY updated_at DESC",
            (collection,),
        )
    else:
        rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


def delete_conversation(conn: Connection, conversation_id: int) -> None:
    get_conversation(conn, conversation_id)  # loud 404
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.commit()


def rename_conversation(conn: Connection, conversation_id: int, title: str) -> dict:
    get_conversation(conn, conversation_id)
    conn.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), conversation_id),
    )
    conn.commit()
    return get_conversation(conn, conversation_id)


def list_messages(conn: Connection, conversation_id: int) -> list[dict]:
    get_conversation(conn, conversation_id)
    rows = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
    ).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["citations"] = json.loads(m.pop("citations_json") or "[]")
        out.append(m)
    return out


def add_message(
    conn: Connection,
    conversation_id: int,
    role: str,
    content: str,
    *,
    citations: list[dict] | None = None,
    model: str | None = None,
) -> int:
    convo = get_conversation(conn, conversation_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, citations_json, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (conversation_id, role, content,
         json.dumps(citations) if citations is not None else None, model, now),
    )
    # First user message titles the conversation.
    title = convo["title"]
    if role == "user" and title == "New conversation":
        title = content.strip().replace("\n", " ")[:60] or title
    conn.execute(
        "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
        (now, title, conversation_id),
    )
    conn.commit()
    return cur.lastrowid


def history_for_api(conn: Connection, conversation_id: int) -> list[dict]:
    """Prior turns as {role, content} for the Anthropic messages array."""
    return [
        {"role": m["role"], "content": m["content"]}
        for m in list_messages(conn, conversation_id)
    ]
