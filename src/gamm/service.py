"""The change workflow: propose → approve (passkey, on the web page) → apply → judge.

Status values:
  proposed            waiting for the approver
  approved            approved; a bot may apply it until the approval expires
  rejected, cancelled, expired
  applying            the write is in flight
  applied             written and read back as expected
  applied_unverified  written, but the read-back didn't match; check the account
  stale               the account changed after the proposal, so it wasn't applied
  failed              Google refused the write; nothing changed
  interrupted         gamm stopped mid-write; check the account
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel

from gamm.ads import AdsBackend, AdsError
from gamm.changes import (
    CHANGE_LIST,
    InvalidChange,
    Plan,
    RuleViolation,
    current_values,
    plan_change,
    values_match,
)
from gamm.config import Config
from gamm.db import Database, dumps, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

OPEN_STATUSES = ("proposed", "approved", "applying")
APPLIED_STATUSES = ("applied", "applied_unverified")


class ChangeError(Exception):
    """A request that can't go ahead. The message is written for the bot or person asking."""


def _who(key_name: str, agent: str | None) -> str:
    return f"{agent.strip()} (key: {key_name})" if agent and agent.strip() else key_name


class ChangeService:
    def __init__(self, config: Config, db: Database, backend: AdsBackend, clock=utcnow):
        self.config = config
        self.db = db
        self.backend = backend
        self.clock = clock
        self._mark_interrupted()

    # -- helpers -----------------------------------------------------------------

    def _customer(self, customer_id: str | None) -> str:
        if not customer_id:
            return self.config.default_customer_id
        cid = "".join(ch for ch in str(customer_id) if ch.isdigit())
        if cid not in self.config.customer_ids:
            raise ChangeError(f"account {customer_id} is not one gamm is allowed to change")
        return cid

    def _today(self) -> dt.date:
        return self.clock().astimezone(self.config.tz).date()

    def _check_freeze(self, campaign_ids: set[str], account_wide: bool) -> None:
        today = self._today()
        for window in self.config.rules.freeze:
            if window.covers(today, campaign_ids, account_wide):
                scope = "the whole account" if not window.campaign_ids else "campaigns " + ", ".join(window.campaign_ids)
                reason = f" ({window.reason})" if window.reason else ""
                raise ChangeError(f"changes are frozen for {scope} from {window.start} to {window.end}{reason}")

    def _event(self, conn, change_id: int, event: str, actor: str, detail: Any = None) -> None:
        conn.execute(
            "INSERT INTO change_events (change_id, at, event, actor, detail_json) VALUES (?, ?, ?, ?, ?)",
            (change_id, iso(self.clock()), event, actor, dumps(detail) if detail is not None else None),
        )

    def _set_status(self, conn, change_id: int, status: str, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in ["status", *fields])
        conn.execute(f"UPDATE changes SET {assignments} WHERE id = ?", (status, *fields.values(), change_id))

    def _expire(self, conn) -> None:
        now = self.clock()
        for row in conn.execute("SELECT id, status, proposed_at, decided_at FROM changes WHERE status IN ('proposed', 'approved')"):
            if row["status"] == "proposed":
                deadline = parse_iso(row["proposed_at"]) + dt.timedelta(hours=self.config.proposal_ttl_hours)
            else:
                deadline = parse_iso(row["decided_at"]) + dt.timedelta(hours=self.config.approval_ttl_hours)
            if now >= deadline:
                self._set_status(conn, row["id"], "expired")
                self._event(conn, row["id"], "expired", "gamm", {"was": row["status"]})

    def _check_conflicts(self, conn, customer_id: str, campaign_ids: set[str], account_wide: bool) -> None:
        rows = conn.execute(
            f"SELECT id, title, status, scope_json FROM changes WHERE customer_id = ? AND status IN {OPEN_STATUSES}",
            (customer_id,),
        ).fetchall()
        for row in rows:
            scope = json.loads(row["scope_json"])
            other_ids = set(scope["campaign_ids"])
            if self.config.rules.one_open_change_per == "account":
                clash = True
            else:
                clash = account_wide or scope["account_wide"] or bool(campaign_ids & other_ids)
            if clash:
                raise ChangeError(
                    f"change #{row['id']} “{row['title']}” is still {row['status']} and touches the same "
                    f"{'account' if self.config.rules.one_open_change_per == 'account' or account_wide or scope['account_wide'] else 'campaign'}; "
                    "apply, cancel or wait for it to expire first (one open change at a time)"
                )

    def _mark_interrupted(self) -> None:
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT id FROM changes WHERE status = 'applying'").fetchall():
                self._set_status(conn, row["id"], "interrupted", error="gamm restarted while applying; check the account")
                self._event(conn, row["id"], "interrupted", "gamm")

    def _plans(self, changes: list[BaseModel], customer_id: str) -> list[Plan]:
        currency = self.backend.customer(customer_id)["currency_code"]
        plans = []
        for number, change in enumerate(changes, start=1):
            try:
                plans.append(plan_change(change, self.backend, customer_id, self.config.rules, currency))
            except RuleViolation as exc:
                raise ChangeError(f"change {number} ({change.kind}) breaks a rule: {exc}") from exc
            except InvalidChange as exc:
                raise ChangeError(f"change {number} ({change.kind}): {exc}") from exc
            except AdsError as exc:
                raise ChangeError(f"change {number} ({change.kind}): couldn't read the account: {exc}") from exc
        return plans

    # -- the workflow --------------------------------------------------------------

    def propose(
        self,
        *,
        changes: list[BaseModel],
        title: str,
        why: str,
        key_name: str,
        agent: str | None = None,
        expected_effect: str = "",
        how_to_check: str = "",
        how_to_undo: str = "",
        judge_on: str = "",
        customer_id: str | None = None,
    ) -> dict:
        cid = self._customer(customer_id)
        if not title.strip() or not why.strip():
            raise ChangeError("a proposal needs a short title and a reason (why)")
        if not changes:
            raise ChangeError("a proposal needs at least one change")
        if len(changes) > self.config.rules.max_changes_per_proposal:
            raise ChangeError(f"at most {self.config.rules.max_changes_per_proposal} changes per proposal")

        plans = self._plans(changes, cid)
        campaign_ids = set().union(*(p.campaign_ids for p in plans))
        account_wide = any(p.account_wide for p in plans)
        self._check_freeze(campaign_ids, account_wide)

        ops = [op for plan in plans for op in plan.ops]
        try:
            self.backend.mutate(cid, ops, validate_only=True)
        except AdsError as exc:
            raise ChangeError(f"Google rejected the dry run, so nothing was proposed: {exc}") from exc

        changes_json = dumps([c.model_dump() for c in changes])
        plans_json = dumps([p.as_dict() for p in plans])
        texts = {
            "title": title.strip(),
            "why": why.strip(),
            "expected_effect": expected_effect.strip(),
            "how_to_check": how_to_check.strip(),
            "how_to_undo": how_to_undo.strip(),
            "judge_on": judge_on.strip(),
        }
        content_hash = hashlib.sha256(dumps([cid, changes_json, plans_json, texts]).encode()).hexdigest()
        who = _who(key_name, agent)
        with self.db.transaction() as conn:
            self._expire(conn)
            self._check_conflicts(conn, cid, campaign_ids, account_wide)
            cur = conn.execute(
                """INSERT INTO changes (customer_id, status, title, why, expected_effect, how_to_check,
                   how_to_undo, judge_on, changes_json, plans_json, scope_json, content_hash,
                   proposed_at, proposed_by_key, proposed_by_agent)
                   VALUES (?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    texts["title"],
                    texts["why"],
                    texts["expected_effect"],
                    texts["how_to_check"],
                    texts["how_to_undo"],
                    texts["judge_on"],
                    changes_json,
                    plans_json,
                    dumps({"campaign_ids": sorted(campaign_ids), "account_wide": account_wide}),
                    content_hash,
                    iso(self.clock()),
                    key_name,
                    agent.strip() if agent else None,
                ),
            )
            change_id = cur.lastrowid
            self._event(conn, change_id, "proposed", who, {"dry_run": "passed"})
        log.info("change #%s proposed by %s", change_id, who)
        return self.get(change_id)

    def decide(self, change_id: int, action: str, approver: str, content_hash: str) -> dict:
        """Record the approver's decision. Only the web layer calls this, after verifying a passkey."""
        if action not in ("approve", "reject"):
            raise ChangeError("action must be approve or reject")
        with self.db.transaction() as conn:
            self._expire(conn)
            row = self._row(conn, change_id)
            if row["status"] != "proposed":
                raise ChangeError(f"change #{change_id} is {row['status']}, so it can't be {action}d")
            if row["content_hash"] != content_hash:
                raise ChangeError(f"change #{change_id} is not the version that was shown; reload the page")
            status = "approved" if action == "approve" else "rejected"
            self._set_status(conn, change_id, status, decided_at=iso(self.clock()), decided_by=approver)
            self._event(conn, change_id, status, approver)
        log.info("change #%s %s by %s", change_id, status, approver)
        return self.get(change_id)

    def apply(self, change_id: int, key_name: str, agent: str | None = None) -> dict:
        who = _who(key_name, agent)
        with self.db.transaction() as conn:
            self._expire(conn)
            row = self._row(conn, change_id)
            if row["status"] != "approved":
                hint = {
                    "proposed": f" It is waiting for approval at {self.approval_url(change_id)}",
                    "expired": " Propose it again if it's still wanted.",
                }.get(row["status"], "")
                raise ChangeError(f"change #{change_id} is {row['status']}, not approved, so it can't be applied.{hint}")
            scope = json.loads(row["scope_json"])
            self._check_freeze(set(scope["campaign_ids"]), scope["account_wide"])
            self._set_status(conn, change_id, "applying")
            self._event(conn, change_id, "applying", who)

        cid = row["customer_id"]
        changes = CHANGE_LIST.validate_json(row["changes_json"])
        stored = json.loads(row["plans_json"])

        def finish(status: str, event: str, detail: Any, **fields: Any) -> dict:
            with self.db.transaction() as conn:
                self._set_status(conn, change_id, status, **fields)
                self._event(conn, change_id, event, who, detail)
            return self.get(change_id)

        # 1. The account must still look the way it did when the approver saw the proposal.
        try:
            drift = []
            for number, (change, plan) in enumerate(zip(changes, stored, strict=True), start=1):
                now = current_values(change, self.backend, cid)
                if not values_match(now, plan["before"]):
                    drift.append({"change": number, "expected": plan["before"], "found": now})
            if drift:
                raise ChangeError(
                    f"the account changed since it was proposed ({dumps(drift)}); propose it again from the current state"
                )
            # 2. Re-check against today's rules and rebuild the writes from fresh reads.
            plans = self._plans(changes, cid)
        except (ChangeError, InvalidChange, AdsError) as exc:
            finish("stale", "stale", str(exc), error=str(exc))
            raise ChangeError(f"change #{change_id} was not applied: {exc}") from exc

        # 3. Write, in one atomic request.
        ops = [op for plan in plans for op in plan.ops]
        try:
            resource_names = self.backend.mutate(cid, ops, validate_only=False)
        except AdsError as exc:
            finish("failed", "failed", str(exc), error=str(exc))
            raise ChangeError(f"Google refused change #{change_id}; nothing was changed: {exc}") from exc
        except Exception as exc:  # network trouble: the write may or may not have landed
            finish("interrupted", "interrupted", repr(exc), error=f"unknown outcome: {exc!r}; check the account")
            raise ChangeError(f"change #{change_id}: the write's outcome is unknown ({exc!r}); check the account") from exc

        # 4. Read back.
        readback, mismatches = [], []
        for number, (change, plan) in enumerate(zip(changes, plans, strict=True), start=1):
            try:
                now = current_values(change, self.backend, cid)
            except Exception as exc:  # noqa: BLE001 - report, don't hide, a failed read-back
                now = {"error": str(exc)}
            readback.append(now)
            if not values_match(now, plan.after):
                mismatches.append({"change": number, "expected": plan.after, "found": now})
        result = {"resource_names": resource_names, "readback": readback, "mismatches": mismatches}
        status = "applied" if not mismatches else "applied_unverified"
        return finish(
            status,
            status,
            result,
            applied_at=iso(self.clock()),
            applied_by_key=key_name,
            applied_by_agent=agent.strip() if agent else None,
            result_json=dumps(result),
            error=None if not mismatches else "read-back didn't match; check the account",
        )

    def cancel(self, change_id: int, reason: str, key_name: str, agent: str | None = None) -> dict:
        who = _who(key_name, agent)
        with self.db.transaction() as conn:
            self._expire(conn)
            row = self._row(conn, change_id)
            if row["status"] not in ("proposed", "approved"):
                raise ChangeError(f"change #{change_id} is {row['status']} and can't be cancelled")
            self._set_status(conn, change_id, "cancelled", decision_note=reason.strip() or None)
            self._event(conn, change_id, "cancelled", who, {"reason": reason})
        return self.get(change_id)

    def judge(self, change_id: int, result: str, key_name: str, agent: str | None = None) -> dict:
        who = _who(key_name, agent)
        if not result.strip():
            raise ChangeError("say what the result was")
        with self.db.transaction() as conn:
            row = self._row(conn, change_id)
            if row["status"] not in APPLIED_STATUSES:
                raise ChangeError(f"change #{change_id} is {row['status']}; only applied changes can be judged")
            conn.execute(
                "UPDATE changes SET judged_at = ?, judged_by = ?, judgement = ? WHERE id = ?",
                (iso(self.clock()), who, result.strip(), change_id),
            )
            self._event(conn, change_id, "judged", who, {"result": result.strip()})
        return self.get(change_id)

    # -- reading -------------------------------------------------------------------------

    def _row(self, conn, change_id: int):
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (int(change_id),)).fetchone()
        if row is None:
            raise ChangeError(f"there is no change #{change_id}")
        return row

    def approval_url(self, change_id: int) -> str:
        return f"{self.config.public_url}/changes/{change_id}"

    def get(self, change_id: int, include_events: bool = True) -> dict:
        with self.db.transaction() as conn:
            self._expire(conn)
            row = dict(self._row(conn, change_id))
            events = [
                {"at": e["at"], "event": e["event"], "actor": e["actor"], "detail": json.loads(e["detail_json"]) if e["detail_json"] else None}
                for e in conn.execute("SELECT * FROM change_events WHERE change_id = ? ORDER BY id", (change_id,))
            ] if include_events else []
        plans = json.loads(row["plans_json"])
        expires_at = None
        if row["status"] == "proposed":
            expires_at = parse_iso(row["proposed_at"]) + dt.timedelta(hours=self.config.proposal_ttl_hours)
        elif row["status"] == "approved":
            expires_at = parse_iso(row["decided_at"]) + dt.timedelta(hours=self.config.approval_ttl_hours)
        out = {
            "change_id": row["id"],
            "status": row["status"],
            "customer_id": row["customer_id"],
            "title": row["title"],
            "summary": [line for plan in plans for line in plan["summary"]],
            "why": row["why"],
            "expected_effect": row["expected_effect"],
            "how_to_check": row["how_to_check"],
            "how_to_undo": row["how_to_undo"],
            "judge_on": row["judge_on"],
            "proposed_at": row["proposed_at"],
            "proposed_by": _who(row["proposed_by_key"], row["proposed_by_agent"]),
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "applied_at": row["applied_at"],
            "applied_by": _who(row["applied_by_key"], row["applied_by_agent"]) if row["applied_by_key"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "judgement": row["judgement"],
            "judged_at": row["judged_at"],
            "expires_at": iso(expires_at) if expires_at else None,
            "approval_url": self.approval_url(row["id"]),
            "content_hash": row["content_hash"],
            "changes": json.loads(row["changes_json"]),
            "plans": plans,
            "change_log_row": self.change_log_row(row, plans),
        }
        if include_events:
            out["events"] = events
        return out

    def list(self, status: str | None = None, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.db.transaction() as conn:
            self._expire(conn)
            if status == "open":
                rows = conn.execute(f"SELECT id FROM changes WHERE status IN {OPEN_STATUSES} ORDER BY id DESC LIMIT ?", (limit,))
            elif status:
                rows = conn.execute("SELECT id FROM changes WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
            else:
                rows = conn.execute("SELECT id FROM changes ORDER BY id DESC LIMIT ?", (limit,))
            ids = [r["id"] for r in rows.fetchall()]
        result = []
        for change_id in ids:
            full = self.get(change_id, include_events=False)
            result.append(
                {k: full[k] for k in ("change_id", "status", "title", "summary", "proposed_at", "proposed_by", "expires_at", "approval_url", "judge_on")}
            )
        return result

    def change_log_row(self, row, plans: list[dict]) -> dict:
        """The row to record in a human-readable change log (e.g. a Notion table)."""
        tz = self.config.tz

        def local(value: str | None) -> str:
            return parse_iso(value).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if value else ""

        status = row["status"]
        approved = ""
        if row["decided_at"]:
            verb = "Approved" if status not in ("rejected",) else "Rejected"
            approved = f"{verb} {local(row['decided_at'])} by {self.config.approver_name} (passkey: {row['decided_by']})"
        applied = ""
        if row["applied_at"]:
            check = "read back OK" if status == "applied" else "read-back mismatch, check the account"
            applied = f"{_who(row['applied_by_key'], row['applied_by_agent'])} via gamm at {local(row['applied_at'])}; {check}"
        return {
            "date": local(row["proposed_at"])[:10],
            "status": status.replace("_", " ").capitalize(),
            "change": "; ".join(line for plan in plans for line in plan["summary"]),
            "why": row["why"],
            "proposed_by": _who(row["proposed_by_key"], row["proposed_by_agent"]),
            "approved": approved,
            "applied_by_and_read_back": applied,
            "judge_on": row["judge_on"],
            "gamm_change": f"#{row['id']} {self.approval_url(row['id'])}",
        }
