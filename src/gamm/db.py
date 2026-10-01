"""SQLite storage: keys, passkeys, changes and the audit log.

One file, WAL mode. Each unit of work opens its own connection, so this is safe
to use from worker threads.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = [
    # version 1
    """
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        key_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE passkeys (
        id INTEGER PRIMARY KEY,
        credential_id TEXT NOT NULL UNIQUE,
        public_key BLOB NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        removed_at TEXT
    );
    CREATE TABLE passkey_invites (
        id INTEGER PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE TABLE webauthn_challenges (
        challenge TEXT PRIMARY KEY,
        purpose TEXT NOT NULL,
        change_id INTEGER,
        action TEXT,
        content_hash TEXT,
        invite_id INTEGER,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE TABLE changes (
        id INTEGER PRIMARY KEY,
        customer_id TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        why TEXT NOT NULL,
        expected_effect TEXT NOT NULL DEFAULT '',
        how_to_check TEXT NOT NULL DEFAULT '',
        how_to_undo TEXT NOT NULL DEFAULT '',
        judge_on TEXT NOT NULL DEFAULT '',
        changes_json TEXT NOT NULL,
        plans_json TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        proposed_at TEXT NOT NULL,
        proposed_by_key TEXT NOT NULL,
        proposed_by_agent TEXT,
        decided_at TEXT,
        decided_by TEXT,
        decision_note TEXT,
        applied_at TEXT,
        applied_by_key TEXT,
        applied_by_agent TEXT,
        result_json TEXT,
        error TEXT,
        judged_at TEXT,
        judged_by TEXT,
        judgement TEXT
    );
    CREATE INDEX changes_status ON changes(status);
    CREATE TABLE change_events (
        id INTEGER PRIMARY KEY,
        change_id INTEGER NOT NULL REFERENCES changes(id),
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail_json TEXT
    );
    CREATE INDEX change_events_change ON change_events(change_id);
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        key_name TEXT NOT NULL,
        agent TEXT,
        client TEXT,
        tool TEXT NOT NULL,
        arguments_json TEXT,
        ok INTEGER,
        error TEXT,
        duration_ms INTEGER,
        summary TEXT
    );
    CREATE INDEX audit_log_started ON audit_log(started_at);
    """,
]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(SCHEMA[version:], start=version + 1):
                conn.executescript(f"BEGIN; {script}; PRAGMA user_version={number}; COMMIT;")

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock up front (BEGIN IMMEDIATE)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
