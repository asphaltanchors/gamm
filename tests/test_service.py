from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from gamm.changes import CHANGE_LIST
from gamm.config import FreezeWindow
from gamm.service import ChangeError, ChangeService


def changes(*items):
    return CHANGE_LIST.validate_python(list(items))


ROAS = {"kind": "target_roas", "campaign_id": "100", "target_roas": 2.2}


def propose(service, *items, **kwargs):
    return service.propose(
        changes=changes(*(items or (ROAS,))),
        title=kwargs.pop("title", "Raise ROAS"),
        why=kwargs.pop("why", "Return is above target"),
        key_name=kwargs.pop("key_name", "bot-a"),
        agent=kwargs.pop("agent", "Alex"),
        **kwargs,
    )


def approve(service, change):
    return service.decide(change["change_id"], "approve", "MacBook", change["content_hash"])


def test_full_workflow(service, account):
    proposed = propose(service, judge_on="2026-10-26")
    assert proposed["status"] == "proposed"
    assert proposed["approval_url"] == "http://localhost:8080/changes/1"
    assert proposed["proposed_by"] == "Alex (key: bot-a)"
    assert account.mutations[-1][0] is True  # dry run only
    assert account.campaigns["100"]["maximize_conversion_value_target_roas"] == 2.0

    with pytest.raises(ChangeError, match="waiting for approval at http://localhost:8080/changes/1"):
        service.apply(1, key_name="bot-a")

    approved = approve(service, proposed)
    assert approved["status"] == "approved"

    applied = service.apply(1, key_name="claude-code", agent="Claude")
    assert applied["status"] == "applied"
    assert account.campaigns["100"]["maximize_conversion_value_target_roas"] == 2.2
    assert applied["result"]["mismatches"] == []
    row = applied["change_log_row"]
    assert row["status"] == "Applied"
    assert row["approved"].startswith("Approved 2026-10-05 10:00 PDT by Sam (passkey: MacBook)")
    assert "read back OK" in row["applied_by_and_read_back"]
    assert row["judge_on"] == "2026-10-26"
    assert [e["event"] for e in applied["events"]] == ["proposed", "approved", "applying", "applied"]

    with pytest.raises(ChangeError, match="applied, not approved"):
        service.apply(1, key_name="bot-a")

    judged = service.judge(1, "ROAS 2.6x over 3 weeks", key_name="bot-a", agent="Alex")
    assert judged["judgement"] == "ROAS 2.6x over 3 weeks"


def test_rule_violation_is_not_stored(service):
    with pytest.raises(ChangeError, match="breaks a rule: target ROAS 150% is below the 200% floor"):
        propose(service, {"kind": "target_roas", "campaign_id": "100", "target_roas": 1.5})
    assert service.list() == []


def test_dry_run_failure_is_not_stored(service, account):
    account.fail_dry_run = "Budget too small"
    with pytest.raises(ChangeError, match="Google rejected the dry run.*Budget too small"):
        propose(service)
    assert service.list() == []


def test_one_open_change_per_campaign(service):
    propose(service)
    with pytest.raises(ChangeError, match=r"change #1 “Raise ROAS” is still proposed"):
        propose(service, {"kind": "campaign_budget", "campaign_id": "100", "daily_amount": 110})
    # A different campaign is fine.
    other = propose(service, {"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"})
    assert other["status"] == "proposed"


def test_account_wide_change_conflicts_with_everything(service):
    propose(service)
    with pytest.raises(ChangeError, match="still proposed"):
        propose(service, {"kind": "conversion_goal", "category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": False})


def test_one_open_change_per_account(config, db, account, clock):
    rules = dataclasses.replace(config.rules, one_open_change_per="account")
    service = ChangeService(dataclasses.replace(config, rules=rules), db, account, clock=clock)
    propose(service)
    with pytest.raises(ChangeError, match="still proposed"):
        propose(service, {"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"})


def test_bundle_is_atomic_and_scoped(service, account):
    proposed = propose(
        service,
        {"kind": "asset_group_status", "asset_group_id": "500", "status": "ENABLED"},
        {"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"},
        title="Swap campaigns",
    )
    assert len(proposed["summary"]) == 2
    approve(service, proposed)
    applied = service.apply(proposed["change_id"], key_name="bot-a")
    assert applied["status"] == "applied"
    real_writes = [ops for validate_only, ops in account.mutations if not validate_only]
    assert len(real_writes) == 1 and len(real_writes[0]) == 2  # one request, both operations


def test_stale_change_is_not_applied(service, account):
    proposed = propose(service)
    approve(service, proposed)
    account.campaigns["100"]["maximize_conversion_value_target_roas"] = 2.4  # someone changed it in the UI
    with pytest.raises(ChangeError, match="account changed since it was proposed"):
        service.apply(1, key_name="bot-a")
    assert service.get(1)["status"] == "stale"
    assert account.campaigns["100"]["maximize_conversion_value_target_roas"] == 2.4
    assert not [m for m in account.mutations if not m[0]]


def test_rules_are_rechecked_at_apply(config, db, account, clock):
    service = ChangeService(config, db, account, clock=clock)
    proposed = propose(service)
    approve(service, proposed)
    stricter = dataclasses.replace(config, rules=dataclasses.replace(config.rules, min_target_roas=2.5))
    with pytest.raises(ChangeError, match="below the 250% floor"):
        ChangeService(stricter, db, account, clock=clock).apply(1, key_name="bot-a")
    assert service.get(1)["status"] == "stale"


def test_google_refusal_marks_failed(service, account):
    approve(service, propose(service))
    account.fail_mutate = "Policy violation"
    with pytest.raises(ChangeError, match="Google refused change #1; nothing was changed"):
        service.apply(1, key_name="bot-a")
    assert service.get(1)["status"] == "failed"


def test_readback_mismatch(service, account):
    approve(service, propose(service))
    account.ignore_writes = True
    applied = service.apply(1, key_name="bot-a")
    assert applied["status"] == "applied_unverified"
    assert applied["result"]["mismatches"][0]["found"] == {"target_roas": 2.0}
    assert "read-back mismatch" in applied["change_log_row"]["applied_by_and_read_back"]


def test_approval_expires(service, clock):
    approve(service, propose(service))
    clock.advance(hours=49)
    with pytest.raises(ChangeError, match="expired, not approved.*Propose it again"):
        service.apply(1, key_name="bot-a")


def test_unapproved_proposal_expires_and_frees_campaign(service, clock):
    propose(service)
    clock.advance(hours=169)
    assert service.get(1)["status"] == "expired"
    assert propose(service)["change_id"] == 2


def test_decision_needs_matching_content(service):
    proposed = propose(service)
    with pytest.raises(ChangeError, match="not the version that was shown"):
        service.decide(1, "approve", "MacBook", "0" * 64)
    service.decide(1, "reject", "MacBook", proposed["content_hash"])
    with pytest.raises(ChangeError, match="rejected, so it can't be approved"):
        service.decide(1, "approve", "MacBook", proposed["content_hash"])


def test_freeze_window(config, db, account, clock):
    today = dt.date(2026, 10, 5)
    window = FreezeWindow(start=today, end=today + dt.timedelta(days=21), campaign_ids=("100",), reason="settling")
    frozen = dataclasses.replace(config, rules=dataclasses.replace(config.rules, freeze=(window,)))
    service = ChangeService(frozen, db, account, clock=clock)
    with pytest.raises(ChangeError, match="frozen for campaigns 100 from 2026-10-05 to 2026-10-26 \\(settling\\)"):
        propose(service)
    assert propose(service, {"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"})["status"] == "proposed"


def test_cancel(service):
    propose(service)
    cancelled = service.cancel(1, "superseded", key_name="bot-a")
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ChangeError, match="cancelled and can't be cancelled"):
        service.cancel(1, "again", key_name="bot-a")


def test_only_configured_accounts(service):
    with pytest.raises(ChangeError, match="not one gamm is allowed to change"):
        propose(service, customer_id="999-999-9999")


def test_interrupted_apply_is_flagged_on_restart(config, db, account, clock, service):
    approve(service, propose(service))
    with db.transaction() as conn:
        conn.execute("UPDATE changes SET status = 'applying' WHERE id = 1")
    restarted = ChangeService(config, db, account, clock=clock)
    assert restarted.get(1)["status"] == "interrupted"
