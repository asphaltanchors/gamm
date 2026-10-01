"""Bot API keys. Only a hash of each key is stored."""

from __future__ import annotations

import hashlib
import re
import secrets

from fastmcp.server.auth import AccessToken, TokenVerifier

from gamm.db import Database, iso, utcnow

KEY_PREFIX = "gamm_"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_key(db: Database, name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ValueError("key names are lowercase letters, digits, - and _, up to 40 characters")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    with db.transaction() as conn:
        existing = conn.execute("SELECT revoked_at FROM api_keys WHERE name = ?", (name,)).fetchone()
        if existing and existing["revoked_at"] is None:
            raise ValueError(f"an active key named {name!r} already exists; revoke it first")
        if existing:
            conn.execute("DELETE FROM api_keys WHERE name = ?", (name,))
        conn.execute(
            "INSERT INTO api_keys (name, key_hash, created_at) VALUES (?, ?, ?)",
            (name, hash_secret(key), iso(utcnow())),
        )
    return key


def revoke_key(db: Database, name: str) -> bool:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
            (iso(utcnow()), name),
        )
        return cur.rowcount > 0


def list_keys(db: Database) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name, created_at, last_used_at, revoked_at FROM api_keys ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def key_name_for(db: Database, key: str) -> str | None:
    if not key.startswith(KEY_PREFIX):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_secret(key),),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE name = ?", (iso(utcnow()), row["name"]))
    return row["name"]


class KeyVerifier(TokenVerifier):
    """Accepts `Authorization: Bearer gamm_...` keys created with `gamm keys create`."""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        name = key_name_for(self.db, token)
        if name is None:
            return None
        return AccessToken(token=token, client_id=name, scopes=[])
