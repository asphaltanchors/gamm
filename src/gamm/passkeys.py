"""Passkeys for the approver.

A change is approved only by a passkey assertion with user verification (Face ID,
Touch ID or a security-key PIN). A bot that can drive the approver's browser or
share their network identity still can't produce one.

Registering a passkey needs a one-time invite created on the server
(`gamm passkeys invite`), so only someone with a shell there can add one.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from gamm.config import Config
from gamm.db import Database, iso, parse_iso, utcnow
from gamm.keys import hash_secret

CHALLENGE_TTL = dt.timedelta(minutes=5)
USER_ID = b"gamm-approver"


class PasskeyError(Exception):
    pass


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _challenge_from(credential: dict) -> str:
    try:
        client_data = json.loads(_b64url_decode(credential["response"]["clientDataJSON"]))
        return client_data["challenge"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("malformed passkey response") from exc


class Passkeys:
    def __init__(self, config: Config, db: Database, clock=utcnow):
        self.config = config
        self.db = db
        self.clock = clock

    @property
    def rp_id(self) -> str:
        return self.config.public_host

    # -- invites -------------------------------------------------------------

    def create_invite(self, minutes: int = 15) -> str:
        token = secrets.token_urlsafe(24)
        now = self.clock()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO passkey_invites (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (hash_secret(token), iso(now), iso(now + dt.timedelta(minutes=minutes))),
            )
        return f"{self.config.public_url}/passkeys/register?invite={token}"

    def _invite(self, conn, token: str):
        row = conn.execute("SELECT * FROM passkey_invites WHERE token_hash = ?", (hash_secret(token or ""),)).fetchone()
        if row is None or row["used_at"] or parse_iso(row["expires_at"]) <= self.clock():
            raise PasskeyError("this invite link is invalid, used or expired; create a new one with `gamm passkeys invite`")
        return row

    def invite_is_valid(self, token: str) -> bool:
        with self.db.connect() as conn:
            try:
                self._invite(conn, token)
                return True
            except PasskeyError:
                return False

    # -- registration -----------------------------------------------------------

    def registration_options(self, token: str) -> str:
        with self.db.transaction() as conn:
            invite = self._invite(conn, token)
            existing = conn.execute("SELECT credential_id FROM passkeys WHERE removed_at IS NULL").fetchall()
            options = generate_registration_options(
                rp_id=self.rp_id,
                rp_name="gamm",
                user_id=USER_ID,
                user_name=self.config.approver_name,
                user_display_name=self.config.approver_name,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.PREFERRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=_b64url_decode(r["credential_id"])) for r in existing],
            )
            conn.execute(
                "INSERT INTO webauthn_challenges (challenge, purpose, invite_id, expires_at) VALUES (?, 'register', ?, ?)",
                (bytes_to_base64url(options.challenge), invite["id"], iso(self.clock() + CHALLENGE_TTL)),
            )
        return options_to_json(options)

    def register(self, token: str, label: str, credential: dict) -> str:
        label = " ".join((label or "").split())[:60] or "passkey"
        challenge = _challenge_from(credential)
        with self.db.transaction() as conn:
            invite = self._invite(conn, token)
            row = self._take_challenge(conn, challenge, "register")
            if row["invite_id"] != invite["id"]:
                raise PasskeyError("this passkey response belongs to a different invite")
            try:
                verified = verify_registration_response(
                    credential=credential,
                    expected_challenge=_b64url_decode(challenge),
                    expected_rp_id=self.rp_id,
                    expected_origin=self.config.public_origin,
                    require_user_verification=True,
                )
            except Exception as exc:
                raise PasskeyError(f"passkey registration failed: {exc}") from exc
            conn.execute(
                "INSERT INTO passkeys (credential_id, public_key, sign_count, label, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    bytes_to_base64url(verified.credential_id),
                    verified.credential_public_key,
                    verified.sign_count,
                    label,
                    iso(self.clock()),
                ),
            )
            conn.execute("UPDATE passkey_invites SET used_at = ? WHERE id = ?", (iso(self.clock()), invite["id"]))
        return label

    # -- approval -----------------------------------------------------------------

    def has_passkeys(self) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM passkeys WHERE removed_at IS NULL LIMIT 1").fetchone() is not None

    def decision_options(self, change_id: int, action: str, content_hash: str) -> str:
        with self.db.transaction() as conn:
            creds = conn.execute("SELECT credential_id FROM passkeys WHERE removed_at IS NULL").fetchall()
            if not creds:
                raise PasskeyError("no passkey is registered yet; run `gamm passkeys invite` on the server")
            options = generate_authentication_options(
                rp_id=self.rp_id,
                allow_credentials=[PublicKeyCredentialDescriptor(id=_b64url_decode(r["credential_id"])) for r in creds],
                user_verification=UserVerificationRequirement.REQUIRED,
            )
            conn.execute(
                """INSERT INTO webauthn_challenges (challenge, purpose, change_id, action, content_hash, expires_at)
                   VALUES (?, 'decide', ?, ?, ?, ?)""",
                (bytes_to_base64url(options.challenge), change_id, action, content_hash, iso(self.clock() + CHALLENGE_TTL)),
            )
        return options_to_json(options)

    def verify_decision(self, change_id: int, action: str, credential: dict) -> tuple[str, str]:
        """Check a passkey assertion for this change and action. Returns (passkey label, content hash)."""
        challenge = _challenge_from(credential)
        with self.db.transaction() as conn:
            row = self._take_challenge(conn, challenge, "decide")
            if row["change_id"] != change_id or row["action"] != action:
                raise PasskeyError("this passkey response was for a different change or action")
            raw_id = credential.get("rawId") or credential.get("id") or ""
            key = conn.execute(
                "SELECT * FROM passkeys WHERE credential_id = ? AND removed_at IS NULL", (raw_id,)
            ).fetchone()
            if key is None:
                raise PasskeyError("unknown or removed passkey")
            try:
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=_b64url_decode(challenge),
                    expected_rp_id=self.rp_id,
                    expected_origin=self.config.public_origin,
                    credential_public_key=key["public_key"],
                    credential_current_sign_count=key["sign_count"],
                    require_user_verification=True,
                )
            except Exception as exc:
                raise PasskeyError(f"passkey check failed: {exc}") from exc
            conn.execute(
                "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
                (verified.new_sign_count, iso(self.clock()), key["id"]),
            )
        return key["label"], row["content_hash"]

    def _take_challenge(self, conn, challenge: str, purpose: str):
        row = conn.execute(
            "SELECT * FROM webauthn_challenges WHERE challenge = ? AND purpose = ?", (challenge, purpose)
        ).fetchone()
        if row is None or row["used_at"] or parse_iso(row["expires_at"]) <= self.clock():
            raise PasskeyError("this passkey request is unknown, used or expired; try again")
        conn.execute("UPDATE webauthn_challenges SET used_at = ? WHERE challenge = ?", (iso(self.clock()), challenge))
        return row

    # -- management ---------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, label, created_at, last_used_at FROM passkeys WHERE removed_at IS NULL ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove(self, passkey_id: int) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE passkeys SET removed_at = ? WHERE id = ? AND removed_at IS NULL", (iso(self.clock()), passkey_id)
            )
            return cur.rowcount > 0
