"""A loopback-only HTTP API for durable, named inboxes."""

import secrets
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Condition
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, StringConstraints

from . import MAX_RECEIVE_WAIT_SECONDS

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)]
bearer = HTTPBearer(auto_error=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    sender TEXT NOT NULL REFERENCES sessions(name),
    recipient TEXT NOT NULL REFERENCES sessions(name),
    text TEXT NOT NULL,
    request_id TEXT,
    sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    acknowledged_at TEXT,
    UNIQUE (sender, request_id)
);

CREATE INDEX IF NOT EXISTS pending_messages
ON messages (recipient, id) WHERE acknowledged_at IS NULL;
"""


class Registration(BaseModel):
    name: Name


class OutgoingMessage(BaseModel):
    to: Name
    text: MessageText
    request_id: UUID | None = None


def token_hash(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if not credentials or not 32 <= len(credentials.credentials) <= 128:
        raise HTTPException(401, "A session bearer token is required")
    return sha256(credentials.credentials.encode()).hexdigest()


def create_app(db_path: Path) -> FastAPI:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect():
        with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)

    app = FastAPI(title="Agent Mailbox")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    delivery = Condition()

    def session_name(digest: Annotated[str, Depends(token_hash)]) -> str:
        with connect() as db:
            row = db.execute("SELECT name FROM sessions WHERE token_hash = ?", (digest,)).fetchone()
        if row is None:
            raise HTTPException(401, "Unknown session token")
        return row["name"]

    Session = Annotated[str, Depends(session_name)]

    @app.post("/sessions")
    def register(data: Registration, digest: Annotated[str, Depends(token_hash)]):
        with connect() as db:
            db.execute(
                "INSERT INTO sessions (name, token_hash) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (data.name, digest),
            )
            row = db.execute(
                "SELECT token_hash FROM sessions WHERE name = ?", (data.name,)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["token_hash"], digest):
                raise HTTPException(409, "That name or identity is already registered")
        return {"name": data.name}

    @app.post("/messages")
    def send(data: OutgoingMessage, sender: Session):
        key = str(data.request_id) if data.request_id else None
        with connect() as db:
            if db.execute("SELECT 1 FROM sessions WHERE name = ?", (data.to,)).fetchone() is None:
                raise HTTPException(404, f"Unknown recipient: {data.to}")
            inserted = db.execute(
                """
                INSERT INTO messages (sender, recipient, text, request_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (sender, request_id) DO NOTHING
                """,
                (sender, data.to, data.text, key),
            )
            if inserted.rowcount:
                message_id = inserted.lastrowid
            else:
                previous = db.execute(
                    "SELECT id, recipient, text FROM messages WHERE sender = ? AND request_id = ?",
                    (sender, key),
                ).fetchone()
                if (previous["recipient"], previous["text"]) != (data.to, data.text):
                    raise HTTPException(409, "That request ID is already in use")
                message_id = previous["id"]

        with delivery:
            delivery.notify_all()
        return {"id": message_id, "to": data.to}

    @app.get("/inbox")
    def receive(
        recipient: Session,
        wait: Annotated[float, Query(ge=0, le=MAX_RECEIVE_WAIT_SECONDS)] = 0,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ):
        def pending():
            with connect() as db:
                rows = db.execute(
                    """
                    SELECT id, sender, recipient, text, sent_at FROM messages
                    WHERE recipient = ? AND acknowledged_at IS NULL AND id > ?
                    ORDER BY id LIMIT 50
                    """,
                    (recipient, after),
                ).fetchall()
            return [dict(row) for row in rows]

        with delivery:
            return {"messages": delivery.wait_for(pending, timeout=wait)}

    @app.post("/inbox/{message_id}/ack")
    def acknowledge(message_id: int, recipient: Session):
        with connect() as db:
            updated = db.execute(
                """
                UPDATE messages
                SET acknowledged_at = COALESCE(
                    acknowledged_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                WHERE id = ? AND recipient = ?
                """,
                (message_id, recipient),
            )
            if not updated.rowcount:
                raise HTTPException(404, "Message not found in this inbox")
        return {"id": message_id, "acknowledged": True}

    return app
