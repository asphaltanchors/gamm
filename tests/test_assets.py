from __future__ import annotations

import pytest
from fakes import CID

from gamm.changes import CHANGE_LIST, InvalidChange, RuleViolation, combined_ops, current_values, plan_change, values_match
from gamm.config import Rules
from gamm.service import ChangeError

RULES = Rules(allowed_url_hosts=("www.example.com", "example.com"))


def change(**data):
    return CHANGE_LIST.validate_python([data])[0]


def plan(account, rules=RULES, **data):
    return plan_change(change(**data), account, CID, rules, "USD")


def texts(account, field_type):
    return sorted(
        account.assets[link["asset_id"]]["text"] for link in account.group_links if link["field_type"] == field_type
    )


# -- asset_group_text -----------------------------------------------------------


def test_text_add_and_remove(account):
    p = plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Rated  5,000 lbs"], remove=["Fast install"])
    assert p.before == {"texts": ["Fast install", "Made in USA", "Strong asphalt anchors"]}
    assert p.after == {"texts": ["Made in USA", "Rated 5,000 lbs", "Strong asphalt anchors"]}
    assert p.campaign_ids == {"100"}
    assert p.summary[1:] == ["Add headline: “Rated 5,000 lbs”", "Remove headline: “Fast install”"]
    create, *links = p.ops
    assert (create.resource, create.resource_name, create.fields) == ("asset", f"customers/{CID}/assets/-1", {"text_asset.text": "Rated 5,000 lbs"})
    # At the 3-headline minimum, so the new one is linked before the old one goes.
    assert [(op.resource, op.action) for op in links] == [("asset_group_asset", "create"), ("asset_group_asset", "remove")]
    assert links[0].fields == {"asset_group": f"customers/{CID}/assetGroups/500", "asset": create.resource_name, "field_type": "HEADLINE"}
    assert links[1].resource_name.endswith("500~201~HEADLINE")


def test_text_reuses_identical_asset(account):
    p = plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Unused but reusable"])
    assert [op.resource for op in p.ops] == ["asset_group_asset"]
    assert p.ops[0].fields["asset"] == f"customers/{CID}/assets/250"


def test_text_length_limits(account):
    with pytest.raises(InvalidChange, match=r"at most 30 characters: “This headline is far too long!!” \(31\)"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["This headline is far too long!!"])
    with pytest.raises(InvalidChange, match="at most 25"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="BUSINESS_NAME", add=["A business name that is too long"], remove=["Acme Anchors"])


def test_text_counts(account):
    with pytest.raises(InvalidChange, match="would have 2 headlines of its own; Performance Max needs 3 to 15"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", remove=["Fast install"])
    with pytest.raises(InvalidChange, match="would have 2 business names of its own; Performance Max needs exactly 1"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="BUSINESS_NAME", add=["Other Co"])
    p = plan(account, kind="asset_group_text", asset_group_id="500", field_type="BUSINESS_NAME", add=["Other Co"], remove=["Acme Anchors"])
    assert p.after == {"texts": ["Other Co"]}


def test_text_needs_a_short_description(account):
    with pytest.raises(InvalidChange, match="at least one description of 60 characters or fewer"):
        plan(
            account,
            kind="asset_group_text",
            asset_group_id="500",
            field_type="DESCRIPTION",
            add=["Our anchors grip asphalt, pavers and soft ground better than anything else."],
            remove=["Holds up to 5,000 lbs in asphalt."],
        )


def test_text_duplicates_and_missing(account):
    with pytest.raises(InvalidChange, match="already in asset group"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Fast install"])
    with pytest.raises(InvalidChange, match="not in asset group .* can't be removed: “Nope”"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["New one"], remove=["Nope"])
    with pytest.raises(InvalidChange, match="listed more than once"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["New one", "New  one"])
    with pytest.raises(InvalidChange, match="at least one headline"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE")


def test_text_limits_ignore_google_generated_assets(account):
    for i in range(12):
        account.link_to_group("500", account.add_asset("TEXT", f"60{i}", text=f"Headline {i}"), "HEADLINE")
    for i in range(5):
        account.link_to_group("500", account.add_asset("TEXT", f"65{i}", text=f"Generated {i}"), "HEADLINE", source="AUTOMATICALLY_CREATED")
    # 15 of the advertiser's own plus 5 generated: removing a generated one is fine,
    p = plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", remove=["Generated 0"])
    assert p.summary == [
        "Change the headlines of asset group “Accessories” (500) in campaign “Main PMax” (15 of its own now, 15 after)",
        "Remove headline: “Generated 0” (generated by Google)",
    ]
    assert len(p.after["texts"]) == 19
    # and swapping one of the advertiser's own is too, but adding a 16th isn't.
    plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Brand new"], remove=["Headline 0"])
    with pytest.raises(InvalidChange, match="16 headlines of its own"):
        plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Brand new"])


def test_text_removes_first_when_full(account):
    for i in range(12):
        account.link_to_group("500", account.add_asset("TEXT", f"60{i}", text=f"Headline {i}"), "HEADLINE")
    p = plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Brand new"], remove=["Headline 0"])
    assert [op.action for op in p.ops] == ["create", "remove", "create"]


# -- asset_group_video ----------------------------------------------------------------


def test_video_add_and_remove(account):
    p = plan(account, kind="asset_group_video", asset_group_id="500", add=["bbbbbbbbbbb"], remove=["240"])
    assert p.before == {"videos": ["aaaaaaaaaaa"]} and p.after == {"videos": ["bbbbbbbbbbb"]}
    # Titles come from YouTube and the account, never from the bot.
    assert p.summary == [
        "Add video “Anchor demo” (youtu.be/bbbbbbbbbbb) to asset group “Accessories” (500) in campaign “Main PMax”",
        "Remove video “How to install” (youtu.be/aaaaaaaaaaa) (asset 240) from asset group “Accessories” (500) in campaign “Main PMax”",
    ]
    assert [(op.resource, op.action) for op in p.ops] == [("asset", "create"), ("asset_group_asset", "remove"), ("asset_group_asset", "create")]
    assert p.ops[0].fields == {"youtube_video_asset.youtube_video_id": "bbbbbbbbbbb"}


def test_video_checks(account):
    with pytest.raises(InvalidChange, match="not YouTube video IDs"):
        plan(account, kind="asset_group_video", asset_group_id="500", add=["https://youtu.be/x"])
    with pytest.raises(InvalidChange, match="no public or unlisted video zzzzzzzzzzz"):
        plan(account, kind="asset_group_video", asset_group_id="500", add=["zzzzzzzzzzz"])
    with pytest.raises(InvalidChange, match="already in"):
        plan(account, kind="asset_group_video", asset_group_id="500", add=["aaaaaaaaaaa"])
    with pytest.raises(InvalidChange, match="no video 999"):
        plan(account, kind="asset_group_video", asset_group_id="500", remove=["999"])


def test_video_limit(account):
    for i in range(4):
        account.link_to_group("500", account.add_asset("YOUTUBE_VIDEO", f"70{i}", youtube_video_id=f"video{i:06d}", youtube_video_title="x"), "YOUTUBE_VIDEO")
    with pytest.raises(InvalidChange, match="would have 6 videos; Performance Max allows 5"):
        plan(account, kind="asset_group_video", asset_group_id="500", add=["bbbbbbbbbbb"])


# -- sitelinks -------------------------------------------------------------------------


def test_sitelink_add_and_remove_for_account(account):
    p = plan(
        account,
        kind="sitelinks",
        scope="account",
        add=[{"link_text": "New anchors", "final_url": "https://example.com/moved", "description1": "See them", "description2": "All sizes"}],
        remove=["faqs"],
    )
    assert p.account_wide and not p.campaign_ids
    assert p.summary == [
        "Add sitelink to the account: “New anchors” → https://example.com/moved (“See them” / “All sizes”) (redirects to https://www.example.com/new)",
        "Remove sitelink from the account: “FAQs” → https://www.example.com/faq",
    ]
    assert p.after == {
        "sitelinks": [{"link_text": "New anchors", "final_url": "https://example.com/moved", "description1": "See them", "description2": "All sizes"}]
    }
    assert [(op.resource, op.action) for op in p.ops] == [("asset", "create"), ("customer_asset", "remove"), ("customer_asset", "create")]
    assert p.ops[0].fields["final_urls"] == ["https://example.com/moved"]


def test_sitelink_for_campaign(account):
    p = plan(account, kind="sitelinks", scope="100", add=[{"link_text": "New", "final_url": "https://www.example.com/new"}])
    assert p.campaign_ids == {"100"} and not p.account_wide
    assert p.ops[1].resource == "campaign_asset"
    assert p.ops[1].fields["campaign"] == f"customers/{CID}/campaigns/100"
    assert "sitelink_asset.description1" not in p.ops[0].fields


@pytest.mark.parametrize(
    ("url", "error", "message"),
    [
        ("https://www.example.com/gone", InvalidChange, "returned HTTP 404"),
        ("https://www.example.com/away", RuleViolation, "elsewhere.test, which isn't an allowed host"),
        ("https://evil.test/", RuleViolation, "isn't an allowed host"),
        ("ftp://www.example.com/x", InvalidChange, "isn't a web address"),
    ],
)
def test_sitelink_url_checks(account, url, error, message):
    with pytest.raises(error, match=message):
        plan(account, kind="sitelinks", scope="account", add=[{"link_text": "X", "final_url": url}])


def test_sitelink_needs_allowed_hosts(account):
    with pytest.raises(RuleViolation, match="no allowed_url_hosts"):
        plan(account, rules=Rules(), kind="sitelinks", scope="account", add=[{"link_text": "X", "final_url": "https://www.example.com/new"}])


def test_sitelink_text_checks(account):
    with pytest.raises(InvalidChange, match="both descriptions or neither"):
        plan(account, kind="sitelinks", scope="account", add=[{"link_text": "X", "final_url": "https://www.example.com/new", "description1": "Only one"}])
    with pytest.raises(InvalidChange, match="at most 25"):
        plan(account, kind="sitelinks", scope="account", add=[{"link_text": "A sitelink text that is too long", "final_url": "https://www.example.com/new"}])
    with pytest.raises(InvalidChange, match="already in the account"):
        plan(account, kind="sitelinks", scope="account", add=[{"link_text": "FAQS", "final_url": "https://www.example.com/new"}])
    with pytest.raises(InvalidChange, match="campaign 999 not found"):
        plan(account, kind="sitelinks", scope="999", remove=["FAQs"])


# -- callouts ------------------------------------------------------------------------------


def test_callouts(account):
    p = plan(account, kind="callouts", scope="account", add=["Made in USA", "Lifetime support"], remove=["free shipping"])
    assert p.before == {"callouts": ["Free shipping"]}
    assert p.after == {"callouts": ["Lifetime support", "Made in USA"]}
    # "Made in USA" reuses asset 311; "Lifetime support" is new.
    assert [(op.resource, op.action) for op in p.ops] == [
        ("asset", "create"),
        ("customer_asset", "remove"),
        ("customer_asset", "create"),
        ("customer_asset", "create"),
    ]
    assert p.ops[2].fields["asset"] == f"customers/{CID}/assets/311"
    with pytest.raises(InvalidChange, match="at most 25"):
        plan(account, kind="callouts", scope="account", add=["A callout that is much too long"])


# -- structured_snippet ---------------------------------------------------------------------


def test_snippet_replaces_values(account):
    p = plan(account, kind="structured_snippet", scope="account", header="Types", values=["Bolts", "Kits", "Epoxy", "Drill bits"])
    assert p.before == {"values": [["Bolts", "Kits", "Epoxy"]]}
    assert p.after == {"values": [["Bolts", "Kits", "Epoxy", "Drill bits"]]}
    assert p.summary == ["Set the “Types” structured snippet of the account to: Bolts, Kits, Epoxy, Drill bits (now: Bolts, Kits, Epoxy)"]
    assert [(op.resource, op.action) for op in p.ops] == [("asset", "create"), ("customer_asset", "remove"), ("customer_asset", "create")]
    assert p.ops[1].resource_name.endswith("320~STRUCTURED_SNIPPET")


def test_snippet_created_when_missing(account):
    p = plan(account, kind="structured_snippet", scope="100", header="Models", values=["SP10", "SP12", "AM625"])
    assert p.before == {"values": []}
    assert "(now: no snippet with this header)" in p.summary[0]


def test_snippet_checks(account):
    with pytest.raises(InvalidChange, match="already has these values"):
        plan(account, kind="structured_snippet", scope="account", header="Types", values=["Bolts", "Kits", "Epoxy"])
    with pytest.raises(InvalidChange, match="3 to 10 values"):
        plan(account, kind="structured_snippet", scope="account", header="Types", values=["Bolts", "Kits"])
    with pytest.raises(Exception, match="header"):
        change(kind="structured_snippet", scope="account", header="Flavors", values=["a", "b", "c"])


# -- unlink_assets -------------------------------------------------------------------------------


def test_unlink_promotion_and_price(account):
    p = plan(account, kind="unlink_assets", scope="account", asset_ids=["340", "330"])
    assert p.before == {"linked": ["330", "340"]} and p.after == {"linked": []}
    assert p.summary == [
        "Unlink asset 330 from the account: price asset (product categories): “6-pack” 65.00 USD → https://www.example.com/p/1",
        "Unlink asset 340 from the account (link paused): promotion “Anchor kits”, 10% off, ends 2023-12-31 → https://www.example.com/kits",
    ]
    assert all(op.action == "remove" and op.resource == "customer_asset" for op in p.ops)


def test_unlink_refuses_other_types_and_scopes(account):
    with pytest.raises(InvalidChange, match="not linked to the account .*: 301"):
        plan(account, kind="unlink_assets", scope="account", asset_ids=["301"])  # linked to campaign 100, not the account
    with pytest.raises(InvalidChange, match="not linked"):
        plan(account, kind="unlink_assets", scope="account", asset_ids=["200"])  # a headline
    p = plan(account, kind="unlink_assets", scope="100", asset_ids=["301"])
    assert p.campaign_ids == {"100"}
    assert p.summary == ["Unlink asset 301 from campaign “Main PMax” (100): sitelink “Kits” → https://www.example.com/kits (“Every size” / “Ships fast”)"]


# -- one request ------------------------------------------------------------------------------------


def test_combined_ops_renumbers_new_assets(account):
    a = plan(account, kind="callouts", scope="account", add=["One", "Two"])
    b = plan(account, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["Three"])
    ops = combined_ops([a, b])
    created = [op.resource_name for op in ops if op.resource == "asset"]
    assert created == [f"customers/{CID}/assets/-1", f"customers/{CID}/assets/-2", f"customers/{CID}/assets/-3"]
    assert ops[-1].fields["asset"] == f"customers/{CID}/assets/-3"
    account._check_temporary_names(ops)


# -- the whole workflow, for each kind ------------------------------------------------------------------

EXAMPLES = [
    {"kind": "asset_group_text", "asset_group_id": "500", "field_type": "HEADLINE", "add": ["Rated 5,000 lbs", "Unused but reusable"], "remove": ["Fast install"]},
    {"kind": "asset_group_video", "asset_group_id": "500", "add": ["bbbbbbbbbbb", "ccccccccccc"], "remove": ["aaaaaaaaaaa"]},
    {"kind": "sitelinks", "scope": "account", "add": [{"link_text": "New", "final_url": "https://example.com/moved"}], "remove": ["FAQs"]},
    {"kind": "callouts", "scope": "account", "add": ["Made in USA", "Lifetime support"], "remove": ["Free shipping"]},
    {"kind": "structured_snippet", "scope": "account", "header": "Types", "values": ["Bolts", "Kits", "Epoxy", "Drill bits"]},
    {"kind": "unlink_assets", "scope": "account", "asset_ids": ["330", "340"]},
]


@pytest.fixture
def asset_service(tmp_path, db, account, clock):
    from conftest import make_config

    from gamm.service import ChangeService

    config = make_config(tmp_path, allowed_url_hosts=["www.example.com", "example.com"])
    return ChangeService(config, db, account, clock=clock)


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["kind"] for e in EXAMPLES])
def test_kind_end_to_end(asset_service, account, example):
    proposed = asset_service.propose(changes=CHANGE_LIST.validate_python([example]), title="t", why="w", key_name="bot")
    dry_run, ops = account.mutations[-1]
    assert dry_run is True and ops
    asset_service.decide(proposed["change_id"], "approve", "MacBook", proposed["content_hash"])
    applied = asset_service.apply(proposed["change_id"], key_name="bot")
    assert applied["status"] == "applied", applied["result"]
    assert applied["result"]["mismatches"] == []
    assert applied["result"]["readback"] == [proposed["plans"][0]["after"]]


def test_several_asset_changes_in_one_request(asset_service, account):
    items = [EXAMPLES[0], EXAMPLES[3], EXAMPLES[4]]
    proposed = asset_service.propose(changes=CHANGE_LIST.validate_python(items), title="t", why="w", key_name="bot")
    assert proposed["plans"][2]["account_wide"]
    asset_service.decide(proposed["change_id"], "approve", "MacBook", proposed["content_hash"])
    applied = asset_service.apply(proposed["change_id"], key_name="bot")
    assert applied["status"] == "applied"
    assert texts(account, "HEADLINE") == ["Made in USA", "Rated 5,000 lbs", "Strong asphalt anchors", "Unused but reusable"]


def test_asset_group_change_holds_its_campaign(asset_service):
    asset_service.propose(changes=CHANGE_LIST.validate_python([EXAMPLES[0]]), title="t", why="w", key_name="bot")
    with pytest.raises(ChangeError, match="one open change at a time"):
        asset_service.propose(
            changes=CHANGE_LIST.validate_python([{"kind": "campaign_status", "campaign_id": "100", "status": "PAUSED"}]),
            title="t", why="w", key_name="bot",
        )
    # Another campaign is free...
    asset_service.propose(
        changes=CHANGE_LIST.validate_python([{"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"}]),
        title="t", why="w", key_name="bot",
    )


def test_account_scope_waits_for_everything(asset_service):
    asset_service.propose(
        changes=CHANGE_LIST.validate_python([{"kind": "campaign_status", "campaign_id": "200", "status": "PAUSED"}]),
        title="t", why="w", key_name="bot",
    )
    with pytest.raises(ChangeError, match="touches the same account"):
        asset_service.propose(changes=CHANGE_LIST.validate_python([EXAMPLES[3]]), title="t", why="w", key_name="bot")


def test_link_that_breaks_after_approval_is_not_applied(asset_service, account):
    proposed = asset_service.propose(changes=CHANGE_LIST.validate_python([EXAMPLES[2]]), title="t", why="w", key_name="bot")
    asset_service.decide(proposed["change_id"], "approve", "MacBook", proposed["content_hash"])
    account.web["https://www.example.com/new"] = (500, None)
    with pytest.raises(ChangeError, match="returned HTTP 500"):
        asset_service.apply(proposed["change_id"], key_name="bot")
    assert asset_service.get(proposed["change_id"])["status"] == "stale"


def test_readback_matches_current(account):
    data = EXAMPLES[1]
    p = plan(account, **data)
    assert values_match(current_values(change(**data), account, CID), p.before)


@pytest.mark.parametrize(("campaign_id", "selected"), [("100", True), (None, False)])
def test_linked_assets_query_selects_what_it_filters(campaign_id, selected):
    from gamm.ads import GoogleAdsBackend

    queries = []
    backend = object.__new__(GoogleAdsBackend)
    backend._search = lambda customer_id, query: queries.append(query) or []
    backend.linked_assets(CID, campaign_id, ["SITELINK"])
    select, where = queries[0].split(" WHERE ")
    assert ("campaign.id" in select) is selected
    assert ("campaign.id = 100" in where) is selected
