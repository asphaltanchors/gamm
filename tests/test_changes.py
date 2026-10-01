from __future__ import annotations

import pytest

from gamm.changes import CHANGE_LIST, InvalidChange, RuleViolation, current_values, plan_change, values_match
from gamm.config import Rules

from fakes import CID


def change(**data):
    return CHANGE_LIST.validate_python([data])[0]


RULES = Rules(min_target_roas=2.0, max_budget_change_pct=20)


def plan(account, rules=RULES, **data):
    return plan_change(change(**data), account, CID, rules, "USD")


def test_campaign_status_plan(account):
    p = plan(account, kind="campaign_status", campaign_id="200", status="PAUSED")
    assert p.before == {"status": "ENABLED"} and p.after == {"status": "PAUSED"}
    assert p.ops[0].fields == {"status": "PAUSED"}
    assert p.campaign_ids == {"200"}
    assert "“Accessories” (200)" in p.summary[0]


def test_no_op_is_refused(account):
    with pytest.raises(InvalidChange, match="already ENABLED"):
        plan(account, kind="campaign_status", campaign_id="100", status="ENABLED")


def test_unknown_campaign(account):
    with pytest.raises(InvalidChange, match="not found"):
        plan(account, kind="campaign_status", campaign_id="999", status="PAUSED")


def test_asset_group_scope_is_its_campaign(account):
    p = plan(account, kind="asset_group_status", asset_group_id="500", status="ENABLED")
    assert p.campaign_ids == {"100"}
    assert p.ops[0].resource == "asset_group"


def test_target_roas_floor(account):
    with pytest.raises(RuleViolation, match="below the 200% floor"):
        plan(account, kind="target_roas", campaign_id="100", target_roas=1.8)
    p = plan(account, kind="target_roas", campaign_id="100", target_roas=2.2)
    assert p.ops[0].fields == {"maximize_conversion_value.target_roas": 2.2}
    assert "220%" in p.summary[0] and "now 200%" in p.summary[0]


def test_target_roas_needs_value_bidding(account):
    with pytest.raises(InvalidChange, match="MANUAL_CPC"):
        plan(account, kind="target_roas", campaign_id="300", target_roas=3)


def test_target_roas_without_floor(account):
    p = plan(account, rules=Rules(), kind="target_roas", campaign_id="100", target_roas=1.5)
    assert p.after == {"target_roas": 1.5}


def test_budget_cap(account):
    with pytest.raises(RuleViolation, match=r"25\.0% change; the cap is 20%"):
        plan(account, kind="campaign_budget", campaign_id="100", daily_amount=125)
    p = plan(account, kind="campaign_budget", campaign_id="100", daily_amount=120)
    assert p.before == {"amount_micros": 100_000_000} and p.after == {"amount_micros": 120_000_000}
    assert "+20.0%" in p.summary[0]


def test_budget_cap_is_configurable(account):
    p = plan(account, rules=Rules(max_budget_change_pct=50), kind="campaign_budget", campaign_id="100", daily_amount=140)
    assert p.after == {"amount_micros": 140_000_000}


def test_budget_ceiling(account):
    with pytest.raises(RuleViolation, match="ceiling"):
        plan(account, rules=Rules(max_daily_budget=110), kind="campaign_budget", campaign_id="100", daily_amount=115)


def test_shared_budget_refused(account):
    with pytest.raises(InvalidChange, match="shared budget"):
        plan(account, kind="campaign_budget", campaign_id="300", daily_amount=11)


def test_budget_rounds_to_cents(account):
    p = plan(account, kind="campaign_budget", campaign_id="100", daily_amount=110.123)
    assert p.after == {"amount_micros": 110_120_000}


def test_negative_keywords_add_and_remove(account):
    p = plan(
        account,
        kind="campaign_negative_keywords",
        campaign_id="100",
        add=[{"text": "Home  Depot", "match_type": "PHRASE"}],
        remove=[{"text": "COMPETITOR", "match_type": "EXACT"}],
    )
    assert p.before == {"PHRASE:home depot": False, "EXACT:competitor": True}
    assert p.after == {"PHRASE:home depot": True, "EXACT:competitor": False}
    assert [op.action for op in p.ops] == ["create", "remove"]
    assert p.ops[0].fields["keyword.text"] == "Home Depot"
    assert p.ops[1].resource_name.endswith("100~1")


def test_negative_keyword_already_present(account):
    with pytest.raises(InvalidChange, match="already excluded"):
        plan(account, kind="campaign_negative_keywords", campaign_id="100", add=[{"text": "competitor", "match_type": "EXACT"}])


def test_negative_keyword_missing_for_remove(account):
    with pytest.raises(InvalidChange, match="not currently excluded"):
        plan(account, kind="campaign_negative_keywords", campaign_id="100", remove=[{"text": "competitor", "match_type": "PHRASE"}])


def test_negative_keyword_limit(account):
    many = [{"text": f"kw {i}", "match_type": "EXACT"} for i in range(5)]
    with pytest.raises(RuleViolation, match="limit is 3"):
        plan(account, rules=Rules(max_keywords_per_change=3), kind="campaign_negative_keywords", campaign_id="100", add=many)


def test_shared_list_scope(account):
    p = plan(account, kind="shared_negative_keywords", shared_set_id="700", add=[{"text": "rival brand", "match_type": "BROAD"}])
    assert p.campaign_ids == {"100", "200"}
    assert p.ops[0].fields["shared_set"].endswith("sharedSets/700")
    assert "used by 2 active campaign(s)" in p.summary[0]


def test_shared_list_must_be_keywords(account):
    with pytest.raises(InvalidChange, match="NEGATIVE_PLACEMENTS"):
        plan(account, kind="shared_negative_keywords", shared_set_id="701", add=[{"text": "x", "match_type": "BROAD"}])


def test_url_suffix(account):
    p = plan(account, kind="campaign_url_suffix", campaign_id="200", final_url_suffix="?utm_campaign=accessories")
    assert p.after == {"final_url_suffix": "utm_campaign=accessories"}
    with pytest.raises(InvalidChange, match="spaces"):
        plan(account, kind="campaign_url_suffix", campaign_id="200", final_url_suffix="a b")


def test_account_conversion_goal_is_account_wide(account):
    p = plan(account, kind="conversion_goal", category="page_view", origin="website", biddable=False)
    assert p.account_wide
    assert p.ops[0].resource == "customer_conversion_goal"


def test_campaign_conversion_goal(account):
    p = plan(account, kind="conversion_goal", category="PAGE_VIEW", origin="WEBSITE", biddable=True, campaign_id="100")
    assert not p.account_wide and p.campaign_ids == {"100"}
    assert p.ops[0].resource == "campaign_conversion_goal"


def test_unknown_conversion_goal_lists_options(account):
    with pytest.raises(InvalidChange, match="available: ENGAGEMENT/YOUTUBE_HOSTED"):
        plan(account, kind="conversion_goal", category="LEAD", origin="WEBSITE", biddable=False)


def test_current_values_match_plan_keys(account):
    data = dict(kind="target_roas", campaign_id="100", target_roas=2.4)
    p = plan(account, **data)
    assert values_match(current_values(change(**data), account, CID), p.before)


def test_values_match_float_tolerance():
    assert values_match({"x": 2.2}, {"x": 2.2000000000001})
    assert not values_match({"x": 2.2}, {"x": 2.3})
    assert not values_match({"x": 1}, {"y": 1})
