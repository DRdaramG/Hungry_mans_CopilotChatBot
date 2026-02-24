"""
Persistent storage for chat conversations using SQLite.

Stores conversations and messages in a local SQLite database
(``Asset/chatbot.db`` inside the project root) for efficient storage and
retrieval of long chat histories.

Messages are retrieved in pages — only the most recent N messages are
loaded initially; older messages are fetched on demand when the user
scrolls up.
"""

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .paths import asset_path

log = logging.getLogger("copilot_chatbot")

DB_PATH = asset_path("chatbot.db")

# How many messages to load at a time when scrolling up.
PAGE_SIZE = 100


@dataclass
class Conversation:
    """A single chat conversation (metadata only — messages live in DB)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "New Chat"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class ChatStore:
    """Manages persistent storage of multiple conversations in SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or DB_PATH
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

        self._active_id: str | None = None
        self._load_active_id()

        # Ensure at least one conversation exists
        if not self.list_conversations():
            conv = self.new_conversation()
            self._active_id = conv.id
            self._persist_active_id()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id    TEXT NOT NULL REFERENCES conversations(id)
                                ON DELETE CASCADE,
                seq        INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conv_seq
                ON messages(conv_id, seq);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Active conversation persistence
    # ------------------------------------------------------------------

    def _load_active_id(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='active_id'",
        ).fetchone()
        if row:
            self._active_id = row[0]
        # Validate
        if self._active_id:
            exists = self._conn.execute(
                "SELECT 1 FROM conversations WHERE id=?",
                (self._active_id,),
            ).fetchone()
            if not exists:
                self._active_id = None

    def _persist_active_id(self) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value)"
            " VALUES ('active_id', ?)",
            (self._active_id,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Active conversation
    # ------------------------------------------------------------------

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @active_id.setter
    def active_id(self, cid: str) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (cid,),
        ).fetchone()
        if exists:
            self._active_id = cid
            self._persist_active_id()

    def active(self) -> Conversation | None:
        """Return the currently active conversation."""
        if self._active_id:
            return self.get(self._active_id)
        return None

    # ------------------------------------------------------------------
    # Conversation CRUD
    # ------------------------------------------------------------------

    def get(self, cid: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at"
            " FROM conversations WHERE id=?",
            (cid,),
        ).fetchone()
        if not row:
            return None
        return Conversation(
            id=row[0], title=row[1],
            created_at=row[2], updated_at=row[3],
        )

    def list_conversations(self) -> list[Conversation]:
        """Return conversations ordered by last update (newest first)."""
        rows = self._conn.execute(
            "SELECT id, title, created_at, updated_at"
            " FROM conversations ORDER BY updated_at DESC",
        ).fetchall()
        return [
            Conversation(id=r[0], title=r[1], created_at=r[2], updated_at=r[3])
            for r in rows
        ]

    def new_conversation(self, title: str = "New Chat") -> Conversation:
        """Create a new conversation and make it active."""
        conv = Conversation(title=title)
        self._conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (conv.id, conv.title, conv.created_at, conv.updated_at),
        )
        self._conn.commit()
        self._active_id = conv.id
        self._persist_active_id()
        return conv

    def delete_conversation(self, cid: str) -> None:
        self._conn.execute(
            "DELETE FROM conversations WHERE id=?", (cid,),
        )
        self._conn.commit()
        if self._active_id == cid:
            self._active_id = None
        # Always keep at least one conversation
        if not self.list_conversations():
            conv = self.new_conversation()
            self._active_id = conv.id
        elif self._active_id is None:
            self._active_id = self.list_conversations()[0].id
        self._persist_active_id()

    def rename_conversation(self, cid: str, title: str) -> None:
        self._conn.execute(
            "UPDATE conversations SET title=? WHERE id=?",
            (title, cid),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Message CRUD
    # ------------------------------------------------------------------

    def add_message(self, conv_id: str, role: str, content) -> int:
        """Append a message to a conversation.

        *content* can be a string or a list (multipart).
        Returns the sequence number of the new message.
        """
        if isinstance(content, list):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = content

        # Get next seq number
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE conv_id=?",
            (conv_id,),
        ).fetchone()
        seq = row[0] + 1

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO messages (conv_id, seq, role, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conv_id, seq, role, content_str, now),
        )
        # Update conversation timestamp
        self._conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now, conv_id),
        )
        self._conn.commit()
        return seq

    def get_messages(
        self,
        conv_id: str,
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """Return messages for *conv_id* in chronological order.

        Parameters
        ----------
        limit  : Maximum number of messages to return.
        offset : Number of *newest* messages to skip (for pagination
                 from the bottom).  ``offset=0, limit=100`` → last 100.

        Returns
        -------
        List of ``{"role": ..., "content": ...}`` dicts, oldest first.
        """
        total = self.message_count(conv_id)
        # We want the most recent `limit` messages BEFORE the `offset` tail
        # i.e. skip `offset` from the end, then take `limit` from the end
        # of the remaining.
        remaining = total - offset
        if remaining <= 0:
            return []
        start = max(0, remaining - limit)
        actual_limit = remaining - start

        rows = self._conn.execute(
            "SELECT role, content, seq FROM messages"
            " WHERE conv_id=? ORDER BY seq ASC"
            " LIMIT ? OFFSET ?",
            (conv_id, actual_limit, start),
        ).fetchall()
        return [
            {"role": r[0], "content": self._parse_content(r[1]), "seq": r[2]}
            for r in rows
        ]

    def get_all_messages(self, conv_id: str) -> list[dict]:
        """Return ALL messages for *conv_id* (for API calls)."""
        rows = self._conn.execute(
            "SELECT role, content FROM messages"
            " WHERE conv_id=? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()
        return [
            {"role": r[0], "content": self._parse_content(r[1])}
            for r in rows
        ]

    def message_count(self, conv_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv_id=?",
            (conv_id,),
        ).fetchone()
        return row[0] if row else 0

    def clear_messages(self, conv_id: str) -> None:
        """Delete all messages in a conversation."""
        self._conn.execute(
            "DELETE FROM messages WHERE conv_id=?", (conv_id,),
        )
        self._conn.commit()

    def get_message_by_seq(self, conv_id: str, seq: int) -> dict | None:
        """Return a single message by conversation ID and sequence number."""
        row = self._conn.execute(
            "SELECT role, content FROM messages"
            " WHERE conv_id=? AND seq=?",
            (conv_id, seq),
        ).fetchone()
        if not row:
            return None
        return {"role": row[0], "content": self._parse_content(row[1])}

    def update_message(self, conv_id: str, seq: int, content) -> None:
        """Update the content of a message identified by conv_id and seq."""
        if isinstance(content, list):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = content
        self._conn.execute(
            "UPDATE messages SET content=? WHERE conv_id=? AND seq=?",
            (content_str, conv_id, seq),
        )
        self._conn.commit()

    def get_system_prompt(self, conv_id: str) -> str:
        """Return the system prompt for a conversation (empty if none)."""
        row = self._conn.execute(
            "SELECT content FROM messages"
            " WHERE conv_id=? AND role='system'"
            " ORDER BY seq ASC LIMIT 1",
            (conv_id,),
        ).fetchone()
        if row:
            return self._parse_content(row[0])
        return ""

    def set_system_prompt(self, conv_id: str, text: str) -> None:
        """Set or update the system prompt (seq=0 message).

        If *text* is empty, any existing system prompt is removed.
        """
        existing = self._conn.execute(
            "SELECT rowid FROM messages"
            " WHERE conv_id=? AND role='system' AND seq=0",
            (conv_id,),
        ).fetchone()

        if text:
            if existing:
                self._conn.execute(
                    "UPDATE messages SET content=? WHERE rowid=?",
                    (text, existing[0]),
                )
            else:
                # Shift all existing messages up by 1 to make room at seq=0
                self._conn.execute(
                    "UPDATE messages SET seq = seq + 1 WHERE conv_id=?",
                    (conv_id,),
                )
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "INSERT INTO messages"
                    " (conv_id, seq, role, content, created_at)"
                    " VALUES (?, 0, 'system', ?, ?)",
                    (conv_id, text, now),
                )
        else:
            if existing:
                self._conn.execute(
                    "DELETE FROM messages WHERE rowid=?",
                    (existing[0],),
                )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Auto-title
    # ------------------------------------------------------------------

    def auto_title(self, cid: str) -> None:
        """Set title from first user message if still ``'New Chat'``."""
        row = self._conn.execute(
            "SELECT title FROM conversations WHERE id=?", (cid,),
        ).fetchone()
        if not row or row[0] != "New Chat":
            return

        msg_row = self._conn.execute(
            "SELECT content FROM messages"
            " WHERE conv_id=? AND role='user'"
            " ORDER BY seq ASC LIMIT 1",
            (cid,),
        ).fetchone()
        if not msg_row:
            return

        content = self._parse_content(msg_row[0])
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    content = p.get("text", "")
                    break
            else:
                content = ""
        if isinstance(content, str) and content.strip():
            title = content.strip()[:40]
            if len(content.strip()) > 40:
                title += "…"
            self.rename_conversation(cid, title)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_content(raw: str):
        """Parse stored content — may be plain text or JSON list."""
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return raw

    def save(self) -> None:
        """Explicit save (for compatibility). SQLite auto-commits."""
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def touch(self, cid: str) -> None:
        """Update the conversation's ``updated_at`` timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now, cid),
        )
        self._conn.commit()
