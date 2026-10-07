"""The changes a bot can propose, and how each is checked and carried out.

Each kind has two functions:

- current(): reads the values the change would touch, as a flat dict.
- plan(): validates the change against the account and the rules, and returns a
  Plan: server-written summary lines, before/after values (same keys as
  current()), and the writes to make.

The approval page shows the Plan's summary, never text written by the bot.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field, TypeAdapter

from gamm.ads import AccountReader, OpSpec
from gamm.config import Rules


class InvalidChange(ValueError):
    """The change doesn't make sense for the account as it is now."""


class RuleViolation(ValueError):
    """The change breaks one of the configured rules."""


# -- what bots can ask for ------------------------------------------------

Status = Literal["ENABLED", "PAUSED"]
MatchType = Literal["EXACT", "PHRASE", "BROAD"]


class Keyword(BaseModel):
    text: str = Field(min_length=1, max_length=80, description="Keyword text, e.g. 'free shipping'.")
    match_type: MatchType


class CampaignStatus(BaseModel):
    """Pause or enable a campaign."""

    kind: Literal["campaign_status"]
    campaign_id: str
    status: Status


class AssetGroupStatus(BaseModel):
    """Pause or enable a Performance Max asset group."""

    kind: Literal["asset_group_status"]
    asset_group_id: str
    status: Status


class CampaignBudget(BaseModel):
    """Set a campaign's daily budget (its own, unshared budget)."""

    kind: Literal["campaign_budget"]
    campaign_id: str
    daily_amount: float = Field(gt=0, description="New daily budget in the account's currency, e.g. 150.00.")


class TargetRoas(BaseModel):
    """Set a campaign's target ROAS (Maximize conversion value or Target ROAS bidding)."""

    kind: Literal["target_roas"]
    campaign_id: str
    target_roas: float = Field(gt=0, description="As a ratio: 2.2 means 220%.")


class CampaignNegativeKeywords(BaseModel):
    """Add or remove a campaign's own negative keywords."""

    kind: Literal["campaign_negative_keywords"]
    campaign_id: str
    add: list[Keyword] = Field(default_factory=list)
    remove: list[Keyword] = Field(default_factory=list)


class SharedNegativeKeywords(BaseModel):
    """Add or remove keywords in a shared negative keyword list (affects every campaign using it)."""

    kind: Literal["shared_negative_keywords"]
    shared_set_id: str
    add: list[Keyword] = Field(default_factory=list)
    remove: list[Keyword] = Field(default_factory=list)


class CampaignUrlSuffix(BaseModel):
    """Set a campaign's final URL suffix (e.g. UTM tags). An empty string clears it."""

    kind: Literal["campaign_url_suffix"]
    campaign_id: str
    final_url_suffix: str = Field(max_length=2048, description="e.g. 'utm_source=google&utm_medium=cpc&utm_campaign=x'")


class ConversionGoal(BaseModel):
    """Choose whether a conversion goal is used for bidding, for the account default or one campaign."""

    kind: Literal["conversion_goal"]
    category: str = Field(description="Goal category, e.g. PURCHASE, PAGE_VIEW, ADD_TO_CART.")
    origin: str = Field(description="Goal origin, e.g. WEBSITE, YOUTUBE_HOSTED, CALL_FROM_ADS.")
    biddable: bool = Field(description="true to use the goal for bidding, false to stop.")
    campaign_id: str | None = Field(default=None, description="Omit to change the account default goals.")


ScopeArg = Field(description="'account' for the whole account, or a campaign ID.")
TextField = Literal["HEADLINE", "LONG_HEADLINE", "DESCRIPTION", "BUSINESS_NAME"]
SnippetHeader = Literal[
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations", "Featured hotels",
    "Insurance coverage", "Models", "Neighborhoods", "Service catalog", "Shows", "Styles", "Types",
]


class AssetGroupText(BaseModel):
    """Add or remove headlines, long headlines, descriptions or the business name of a Performance Max asset group."""

    kind: Literal["asset_group_text"]
    asset_group_id: str
    field_type: TextField
    add: list[str] = Field(default_factory=list, description="Texts to add, exactly as they should appear.")
    remove: list[str] = Field(default_factory=list, description="Linked texts to remove (the asset itself is kept).")


class AssetGroupVideo(BaseModel):
    """Add or remove YouTube videos in a Performance Max asset group."""

    kind: Literal["asset_group_video"]
    asset_group_id: str
    add: list[str] = Field(default_factory=list, description="YouTube video IDs, e.g. 'dQw4w9WgXcQ'.")
    remove: list[str] = Field(default_factory=list, description="Asset IDs or YouTube video IDs of linked videos.")


class Sitelink(BaseModel):
    link_text: str = Field(description="Up to 25 characters.")
    final_url: str = Field(description="Must load (HTTP 200) on a host in the allowed_url_hosts rule.")
    description1: str = Field(default="", description="Up to 35 characters; give both descriptions or neither.")
    description2: str = Field(default="", description="Up to 35 characters.")


class Sitelinks(BaseModel):
    """Add or remove sitelinks for the account or one campaign. Removing unlinks; the asset is kept."""

    kind: Literal["sitelinks"]
    scope: str = ScopeArg
    add: list[Sitelink] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list, description="Link texts of linked sitelinks to remove.")


class Callouts(BaseModel):
    """Add or remove callouts for the account or one campaign. Removing unlinks; the asset is kept."""

    kind: Literal["callouts"]
    scope: str = ScopeArg
    add: list[str] = Field(default_factory=list, description="Callout texts, up to 25 characters each.")
    remove: list[str] = Field(default_factory=list, description="Texts of linked callouts to remove.")


class StructuredSnippet(BaseModel):
    """Set the values of the structured snippet with this header, for the account or one campaign."""

    kind: Literal["structured_snippet"]
    scope: str = ScopeArg
    header: SnippetHeader
    values: list[str] = Field(description="3 to 10 values, up to 25 characters each.")


class UnlinkAssets(BaseModel):
    """Unlink price, promotion, sitelink, callout or structured snippet assets from the account or one campaign."""

    kind: Literal["unlink_assets"]
    scope: str = ScopeArg
    asset_ids: list[str] = Field(min_length=1)


Change = Annotated[
    CampaignStatus
    | AssetGroupStatus
    | CampaignBudget
    | TargetRoas
    | CampaignNegativeKeywords
    | SharedNegativeKeywords
    | CampaignUrlSuffix
    | ConversionGoal
    | AssetGroupText
    | AssetGroupVideo
    | Sitelinks
    | Callouts
    | StructuredSnippet
    | UnlinkAssets,
    Field(discriminator="kind"),
]
CHANGE_LIST = TypeAdapter(list[Change])


# -- plans ------------------------------------------------------------------


@dataclass
class Plan:
    summary: list[str]
    before: dict[str, Any]
    after: dict[str, Any]
    ops: list[OpSpec]
    campaign_ids: set[str] = field(default_factory=set)
    account_wide: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "before": self.before,
            "after": self.after,
            "ops": [op.as_dict() for op in self.ops],
            "campaign_ids": sorted(self.campaign_ids),
            "account_wide": self.account_wide,
        }


# New assets get temporary names with negative IDs so that the link created in
# the same request can point at them. Each plan numbers its own from -1;
# combined_ops renumbers them so they're unique across the whole request.
_TEMP_ASSET = re.compile(r"^(customers/\d+/assets/)-(\d+)$")


def _shift_temporary(value: Any, offset: int) -> Any:
    match = _TEMP_ASSET.match(value) if isinstance(value, str) else None
    return f"{match.group(1)}-{int(match.group(2)) + offset}" if match else value


def combined_ops(plans: list[Plan]) -> list[OpSpec]:
    """Every plan's writes, in order, as one request."""
    ops: list[OpSpec] = []
    offset = 0
    for plan in plans:
        for op in plan.ops:
            fields = {k: _shift_temporary(v, offset) for k, v in op.fields.items()}
            ops.append(OpSpec(op.resource, op.action, _shift_temporary(op.resource_name, offset), fields))
        offset += sum(1 for op in plan.ops if op.resource == "asset" and op.action == "create")
    return ops


def values_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.keys() != b.keys():
        return False
    for key, x in a.items():
        y = b[key]
        if isinstance(x, float) or isinstance(y, float):
            if not isinstance(x, int | float) or not isinstance(y, int | float):
                return False
            if not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif x != y:
            return False
    return True


def money(micros: int, currency: str) -> str:
    return f"{micros / 1_000_000:,.2f} {currency}"


def pct(ratio: float) -> str:
    return "none" if not ratio else f"{ratio * 100:g}%"


def quoted(value: str) -> str:
    return f'"{value}"' if value else "(none)"


def _campaign(reader: AccountReader, cid: str, campaign_id: str) -> dict:
    campaign = reader.campaign(cid, campaign_id)
    if campaign is None:
        raise InvalidChange(f"campaign {campaign_id} not found in account {cid}")
    if campaign["status"] == "REMOVED":
        raise InvalidChange(f"campaign {campaign_id} has been removed and can't be changed")
    return campaign


def _label(campaign: dict) -> str:
    return f"campaign “{campaign['name']}” ({campaign['id']})"


# -- campaign_status ------------------------------------------------------------


def _campaign_status_current(ch: CampaignStatus, reader: AccountReader, cid: str) -> dict:
    return {"status": _campaign(reader, cid, ch.campaign_id)["status"]}


def _campaign_status_plan(ch: CampaignStatus, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign = _campaign(reader, cid, ch.campaign_id)
    if campaign["status"] == ch.status:
        raise InvalidChange(f"{_label(campaign)} is already {ch.status}")
    return Plan(
        summary=[f"Set {_label(campaign)} to {ch.status} (now {campaign['status']})"],
        before={"status": campaign["status"]},
        after={"status": ch.status},
        ops=[OpSpec("campaign", "update", f"customers/{cid}/campaigns/{campaign['id']}", {"status": ch.status})],
        campaign_ids={campaign["id"]},
    )


# -- asset_group_status --------------------------------------------------------------


def _asset_group(reader: AccountReader, cid: str, asset_group_id: str) -> dict:
    group = reader.asset_group(cid, asset_group_id)
    if group is None:
        raise InvalidChange(f"asset group {asset_group_id} not found in account {cid}")
    if group["status"] == "REMOVED":
        raise InvalidChange(f"asset group {asset_group_id} has been removed and can't be changed")
    return group


def _asset_group_status_current(ch: AssetGroupStatus, reader: AccountReader, cid: str) -> dict:
    return {"status": _asset_group(reader, cid, ch.asset_group_id)["status"]}


def _group_label(group: dict) -> str:
    return f"asset group “{group['name']}” ({group['id']}) in campaign “{group['campaign_name']}”"


def _asset_group_status_plan(ch: AssetGroupStatus, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    group = _asset_group(reader, cid, ch.asset_group_id)
    label = _group_label(group)
    if group["status"] == ch.status:
        raise InvalidChange(f"{label} is already {ch.status}")
    return Plan(
        summary=[f"Set {label} to {ch.status} (now {group['status']})"],
        before={"status": group["status"]},
        after={"status": ch.status},
        ops=[OpSpec("asset_group", "update", group["resource_name"], {"status": ch.status})],
        campaign_ids={group["campaign_id"]},
    )


# -- campaign_budget ------------------------------------------------------------------


def _budget_for(reader: AccountReader, cid: str, campaign: dict) -> dict:
    budget = reader.budget(cid, campaign["budget_resource_name"])
    if budget is None:
        raise InvalidChange(f"{_label(campaign)} has no budget gamm can read")
    return budget


def _campaign_budget_current(ch: CampaignBudget, reader: AccountReader, cid: str) -> dict:
    return {"amount_micros": _budget_for(reader, cid, _campaign(reader, cid, ch.campaign_id))["amount_micros"]}


def _campaign_budget_plan(ch: CampaignBudget, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign = _campaign(reader, cid, ch.campaign_id)
    budget = _budget_for(reader, cid, campaign)
    if budget["explicitly_shared"] or budget["reference_count"] > 1:
        raise InvalidChange(
            f"{_label(campaign)} uses a shared budget (“{budget['name']}”, {budget['reference_count']} campaigns); "
            "gamm only changes unshared budgets"
        )
    if budget["period"] != "DAILY":
        raise InvalidChange(f"{_label(campaign)} has a {budget['period']} budget; gamm only changes daily budgets")
    old = budget["amount_micros"]
    new = round(ch.daily_amount * 100) * 10_000  # whole cents, in micros
    if new == old:
        raise InvalidChange(f"{_label(campaign)} daily budget is already {money(old, currency)}")
    if rules.max_budget_change_pct is not None:
        if old <= 0:
            raise RuleViolation(f"{_label(campaign)} has no current budget to measure a capped move against")
        move = abs(new - old) / old * 100
        if move > rules.max_budget_change_pct + 1e-9:
            low = old * (1 - rules.max_budget_change_pct / 100)
            high = old * (1 + rules.max_budget_change_pct / 100)
            raise RuleViolation(
                f"moving {_label(campaign)} from {money(old, currency)} to {money(new, currency)} is a {move:.1f}% change; "
                f"the cap is {rules.max_budget_change_pct:g}% per change "
                f"(allowed now: {money(round(low), currency)} to {money(round(high), currency)})"
            )
    if rules.max_daily_budget is not None and new > rules.max_daily_budget * 1_000_000:
        raise RuleViolation(
            f"{money(new, currency)} is above the {money(round(rules.max_daily_budget * 1_000_000), currency)} daily budget ceiling"
        )
    change_pct = (new - old) / old * 100 if old else 0
    return Plan(
        summary=[
            f"Set {_label(campaign)} daily budget to {money(new, currency)} "
            f"(now {money(old, currency)}, {change_pct:+.1f}%)"
        ],
        before={"amount_micros": old},
        after={"amount_micros": new},
        ops=[OpSpec("campaign_budget", "update", budget["resource_name"], {"amount_micros": new})],
        campaign_ids={campaign["id"]},
    )


# -- target_roas --------------------------------------------------------------------------

_ROAS_FIELDS = {
    "MAXIMIZE_CONVERSION_VALUE": ("maximize_conversion_value.target_roas", "maximize_conversion_value_target_roas"),
    "TARGET_ROAS": ("target_roas.target_roas", "target_roas_target_roas"),
}


def _roas_field(campaign: dict) -> tuple[str, str]:
    if campaign["portfolio_bidding_strategy"]:
        raise InvalidChange(f"{_label(campaign)} uses a portfolio bid strategy; gamm doesn't change those")
    fields = _ROAS_FIELDS.get(campaign["bidding_strategy_type"])
    if fields is None:
        raise InvalidChange(
            f"{_label(campaign)} bids with {campaign['bidding_strategy_type']}; target ROAS applies only to "
            "Maximize conversion value or Target ROAS bidding"
        )
    return fields


def _target_roas_current(ch: TargetRoas, reader: AccountReader, cid: str) -> dict:
    campaign = _campaign(reader, cid, ch.campaign_id)
    return {"target_roas": campaign[_roas_field(campaign)[1]]}


def _target_roas_plan(ch: TargetRoas, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign = _campaign(reader, cid, ch.campaign_id)
    api_path, key = _roas_field(campaign)
    old = campaign[key]
    new = round(ch.target_roas, 4)
    if rules.min_target_roas is not None and new < rules.min_target_roas - 1e-9:
        raise RuleViolation(f"target ROAS {pct(new)} is below the {pct(rules.min_target_roas)} floor")
    if math.isclose(old, new, abs_tol=1e-9):
        raise InvalidChange(f"{_label(campaign)} target ROAS is already {pct(old)}")
    return Plan(
        summary=[f"Set {_label(campaign)} target ROAS to {pct(new)} (now {pct(old)})"],
        before={"target_roas": old},
        after={"target_roas": new},
        ops=[OpSpec("campaign", "update", f"customers/{cid}/campaigns/{campaign['id']}", {api_path: new})],
        campaign_ids={campaign["id"]},
    )


# -- negative keywords (campaign and shared list) ------------------------------------------


def _kw_key(match_type: str, text: str) -> str:
    return f"{match_type}:{' '.join(text.lower().split())}"


def _kw_label(kw: Keyword) -> str:
    return f"[{kw.match_type.lower()}] {' '.join(kw.text.split())}"


def _check_keyword_lists(add: list[Keyword], remove: list[Keyword], rules: Rules) -> None:
    total = len(add) + len(remove)
    if total == 0:
        raise InvalidChange("give at least one keyword to add or remove")
    if total > rules.max_keywords_per_change:
        raise RuleViolation(f"{total} keywords in one change; the limit is {rules.max_keywords_per_change}")
    keys = [_kw_key(k.match_type, k.text) for k in add + remove]
    if len(set(keys)) != len(keys):
        raise InvalidChange("the same keyword appears more than once")


def _keyword_plan(
    add: list[Keyword],
    remove: list[Keyword],
    existing: list[dict],
    where: str,
    rules: Rules,
    create_fields: Callable[[Keyword], dict],
    resource: str,
) -> tuple[list[str], dict, dict, list[OpSpec]]:
    _check_keyword_lists(add, remove, rules)
    index = {_kw_key(k["match_type"], k["text"]): k for k in existing}
    already = [_kw_label(k) for k in add if _kw_key(k.match_type, k.text) in index]
    if already:
        raise InvalidChange(f"already excluded in {where}: {', '.join(already)}")
    missing = [_kw_label(k) for k in remove if _kw_key(k.match_type, k.text) not in index]
    if missing:
        raise InvalidChange(f"not currently excluded in {where}, so can't be removed: {', '.join(missing)}")
    before = {_kw_key(k.match_type, k.text): _kw_key(k.match_type, k.text) in index for k in add + remove}
    after = {**{_kw_key(k.match_type, k.text): True for k in add}, **{_kw_key(k.match_type, k.text): False for k in remove}}
    ops = [OpSpec(resource, "create", None, create_fields(k)) for k in add]
    ops += [OpSpec(resource, "remove", index[_kw_key(k.match_type, k.text)]["resource_name"]) for k in remove]
    summary = []
    if add:
        summary.append(f"Add negative keywords to {where}: {', '.join(_kw_label(k) for k in add)}")
    if remove:
        summary.append(f"Remove negative keywords from {where}: {', '.join(_kw_label(k) for k in remove)}")
    return summary, before, after, ops


def _presence(keywords: list[Keyword], existing: list[dict]) -> dict:
    present = {_kw_key(k["match_type"], k["text"]) for k in existing}
    return {_kw_key(k.match_type, k.text): _kw_key(k.match_type, k.text) in present for k in keywords}


def _campaign_negatives_current(ch: CampaignNegativeKeywords, reader: AccountReader, cid: str) -> dict:
    _campaign(reader, cid, ch.campaign_id)
    return _presence(ch.add + ch.remove, reader.campaign_negative_keywords(cid, ch.campaign_id))


def _campaign_negatives_plan(
    ch: CampaignNegativeKeywords, reader: AccountReader, cid: str, rules: Rules, currency: str
) -> Plan:
    campaign = _campaign(reader, cid, ch.campaign_id)
    resource = f"customers/{cid}/campaigns/{campaign['id']}"
    summary, before, after, ops = _keyword_plan(
        ch.add,
        ch.remove,
        reader.campaign_negative_keywords(cid, ch.campaign_id),
        _label(campaign),
        rules,
        lambda k: {
            "campaign": resource,
            "negative": True,
            "keyword.text": " ".join(k.text.split()),
            "keyword.match_type": k.match_type,
        },
        "campaign_criterion",
    )
    return Plan(summary, before, after, ops, campaign_ids={campaign["id"]})


def _shared_set(reader: AccountReader, cid: str, shared_set_id: str) -> dict:
    shared = reader.shared_set(cid, shared_set_id)
    if shared is None:
        raise InvalidChange(f"shared list {shared_set_id} not found in account {cid}")
    if shared["type"] not in ("NEGATIVE_KEYWORDS", "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS"):
        raise InvalidChange(f"shared list “{shared['name']}” is a {shared['type']} list, not a negative keyword list")
    if shared["status"] != "ENABLED":
        raise InvalidChange(f"shared list “{shared['name']}” is {shared['status']}")
    return shared


def _shared_negatives_current(ch: SharedNegativeKeywords, reader: AccountReader, cid: str) -> dict:
    _shared_set(reader, cid, ch.shared_set_id)
    return _presence(ch.add + ch.remove, reader.shared_set_keywords(cid, ch.shared_set_id))


def _shared_negatives_plan(
    ch: SharedNegativeKeywords, reader: AccountReader, cid: str, rules: Rules, currency: str
) -> Plan:
    shared = _shared_set(reader, cid, ch.shared_set_id)
    account_wide = shared["type"] == "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS"
    campaigns = set(reader.shared_set_campaigns(cid, ch.shared_set_id))
    users = "the whole account" if account_wide else f"used by {len(campaigns)} active campaign(s)"
    summary, before, after, ops = _keyword_plan(
        ch.add,
        ch.remove,
        reader.shared_set_keywords(cid, ch.shared_set_id),
        f"shared list “{shared['name']}” ({shared['id']}, {users})",
        rules,
        lambda k: {
            "shared_set": shared["resource_name"],
            "keyword.text": " ".join(k.text.split()),
            "keyword.match_type": k.match_type,
        },
        "shared_criterion",
    )
    return Plan(summary, before, after, ops, campaign_ids=campaigns, account_wide=account_wide)


# -- campaign_url_suffix ---------------------------------------------------------------------


def _url_suffix_current(ch: CampaignUrlSuffix, reader: AccountReader, cid: str) -> dict:
    return {"final_url_suffix": _campaign(reader, cid, ch.campaign_id)["final_url_suffix"]}


def _url_suffix_plan(ch: CampaignUrlSuffix, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign = _campaign(reader, cid, ch.campaign_id)
    old = campaign["final_url_suffix"]
    new = ch.final_url_suffix.strip().lstrip("?")
    if any(c.isspace() for c in new):
        raise InvalidChange("a URL suffix can't contain spaces")
    if new == old:
        raise InvalidChange(f"{_label(campaign)} final URL suffix is already {quoted(old)}")
    return Plan(
        summary=[f"Set {_label(campaign)} final URL suffix to {quoted(new)} (now {quoted(old)})"],
        before={"final_url_suffix": old},
        after={"final_url_suffix": new},
        ops=[OpSpec("campaign", "update", f"customers/{cid}/campaigns/{campaign['id']}", {"final_url_suffix": new})],
        campaign_ids={campaign["id"]},
    )


# -- conversion_goal -------------------------------------------------------------------------


def _goal(ch: ConversionGoal, reader: AccountReader, cid: str) -> tuple[dict, str, dict | None]:
    category, origin = ch.category.strip().upper(), ch.origin.strip().upper()
    if ch.campaign_id:
        campaign = _campaign(reader, cid, ch.campaign_id)
        goals = reader.campaign_conversion_goals(cid, ch.campaign_id)
        where = _label(campaign)
    else:
        campaign = None
        goals = reader.customer_conversion_goals(cid)
        where = "the account default goals"
    for goal in goals:
        if goal["category"] == category and goal["origin"] == origin:
            return goal, where, campaign
    available = ", ".join(sorted(f"{g['category']}/{g['origin']}" for g in goals)) or "none"
    raise InvalidChange(f"no {category}/{origin} goal in {where}; available: {available}")


def _goal_current(ch: ConversionGoal, reader: AccountReader, cid: str) -> dict:
    return {"biddable": _goal(ch, reader, cid)[0]["biddable"]}


def _goal_plan(ch: ConversionGoal, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    goal, where, campaign = _goal(ch, reader, cid)
    name = f"{goal['category']}/{goal['origin']}"

    def state(biddable: bool) -> str:
        return "used for bidding" if biddable else "not used for bidding"

    if goal["biddable"] == ch.biddable:
        raise InvalidChange(f"conversion goal {name} in {where} is already {state(ch.biddable)}")
    resource = "campaign_conversion_goal" if campaign else "customer_conversion_goal"
    return Plan(
        summary=[f"Make conversion goal {name} in {where} {state(ch.biddable)} (now {state(goal['biddable'])})"],
        before={"biddable": goal["biddable"]},
        after={"biddable": ch.biddable},
        ops=[OpSpec(resource, "update", goal["resource_name"], {"biddable": ch.biddable})],
        campaign_ids={campaign["id"]} if campaign else set(),
        account_wide=campaign is None,
    )


# -- assets: shared helpers -------------------------------------------------------------------


def _clean(text: str) -> str:
    return " ".join(text.split())


def _fold(text: str) -> str:
    return _clean(text).casefold()


def _texts(texts: list[str]) -> str:
    return ", ".join(f"“{t}”" for t in texts)


def _check_unique(items: list[str], what: str, key: Callable[[str], str] = _clean) -> None:
    seen: set[str] = set()
    repeated = []
    for item in items:
        if key(item) in seen:
            repeated.append(item)
        seen.add(key(item))
    if repeated:
        raise InvalidChange(f"{what} listed more than once: {_texts(repeated)}")


def _check_lengths(texts: list[str], limit: int, what: str) -> None:
    if any(not t for t in texts):
        raise InvalidChange(f"{what} can't be empty")
    too_long = [f"“{t}” ({len(t)})" for t in texts if len(t) > limit]
    if too_long:
        raise InvalidChange(f"{what} can be at most {limit} characters: {', '.join(too_long)}")


class _NewAssets:
    """The assets one plan creates, under temporary names (see combined_ops)."""

    def __init__(self, cid: str):
        self.cid = cid
        self.ops: list[OpSpec] = []

    def create(self, fields: dict) -> str:
        name = f"customers/{self.cid}/assets/-{len(self.ops) + 1}"
        self.ops.append(OpSpec("asset", "create", name, fields))
        return name

    def reuse_or_create(self, reader: AccountReader, asset_type: str, value: str, fields: dict) -> str:
        existing = reader.find_asset(self.cid, asset_type, value)
        return existing["resource_name"] if existing else self.create(fields)


def _scope(reader: AccountReader, cid: str, scope: str) -> tuple[dict | None, str]:
    """The campaign a scope names (None for the account), and how to describe it."""
    if scope.strip().lower() == "account":
        return None, "the account"
    campaign = _campaign(reader, cid, scope.strip())
    return campaign, _label(campaign)


def _scope_rules(campaign: dict | None) -> dict:
    return {"campaign_ids": {campaign["id"]}} if campaign else {"account_wide": True}


def _scope_links(reader: AccountReader, cid: str, campaign: dict | None, field_types: list[str]) -> list[dict]:
    return reader.linked_assets(cid, campaign["id"] if campaign else None, field_types)


def _link(cid: str, campaign: dict | None, asset: str, field_type: str) -> OpSpec:
    if campaign:
        fields = {"campaign": f"customers/{cid}/campaigns/{campaign['id']}", "asset": asset, "field_type": field_type}
        return OpSpec("campaign_asset", "create", None, fields)
    return OpSpec("customer_asset", "create", None, {"asset": asset, "field_type": field_type})


def _unlink(campaign: dict | None, link: dict) -> OpSpec:
    return OpSpec("campaign_asset" if campaign else "customer_asset", "remove", link["resource_name"])


def _missing_or_present(
    add: list[str], remove: list[str], linked: dict[str, list[dict]], where: str, key: Callable[[str], str]
) -> None:
    already = [t for t in add if key(t) in linked]
    if already:
        raise InvalidChange(f"already in {where}: {_texts(already)}")
    missing = [t for t in remove if key(t) not in linked]
    if missing:
        raise InvalidChange(f"not in {where}, so can't be removed: {_texts(missing)}")


# -- asset_group_text ---------------------------------------------------------------------------

# Performance Max limits: longest text, fewest and most per asset group.
_TEXT_RULES = {
    "HEADLINE": (30, 3, 15),
    "LONG_HEADLINE": (90, 1, 5),
    "DESCRIPTION": (90, 2, 5),
    "BUSINESS_NAME": (25, 1, 1),
}
_SHORT_DESCRIPTION = 60


def _group_links(reader: AccountReader, cid: str, asset_group_id: str, field_type: str) -> list[dict]:
    return [link for link in reader.asset_group_assets(cid, asset_group_id) if link["field_type"] == field_type]


def _ordered(adds: list[OpSpec], removes: list[OpSpec], count_now: int, minimum: int) -> list[OpSpec]:
    """Removals first, so a full group has room, unless that would dip below the minimum part-way."""
    return removes + adds if count_now - len(removes) >= minimum else adds + removes


def _asset_group_text_current(ch: AssetGroupText, reader: AccountReader, cid: str) -> dict:
    _asset_group(reader, cid, ch.asset_group_id)
    return {"texts": sorted(link["asset"]["text"] for link in _group_links(reader, cid, ch.asset_group_id, ch.field_type))}


def _asset_group_text_plan(ch: AssetGroupText, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    group = _asset_group(reader, cid, ch.asset_group_id)
    where = _group_label(group)
    label = ch.field_type.lower().replace("_", " ")
    longest, fewest, most = _TEXT_RULES[ch.field_type]
    add = [_clean(t) for t in ch.add]
    remove = [_clean(t) for t in ch.remove]
    if not add and not remove:
        raise InvalidChange(f"give at least one {label} to add or remove")
    _check_unique(add + remove, f"{label}s")
    _check_lengths(add, longest, f"a {label}")

    links = _group_links(reader, cid, group["id"], ch.field_type)
    linked: dict[str, list[dict]] = {}
    for link in links:
        linked.setdefault(_clean(link["asset"]["text"]), []).append(link)
    _missing_or_present(add, remove, linked, where, _clean)

    before = sorted(link["asset"]["text"] for link in links)
    after = sorted([t for t in before if _clean(t) not in remove] + add)
    if not fewest <= len(after) <= most:
        needs = f"exactly {fewest}" if fewest == most else f"{fewest} to {most}"
        raise InvalidChange(f"{where} would have {len(after)} {label}s; Performance Max needs {needs}")
    if ch.field_type == "DESCRIPTION" and not any(len(t) <= _SHORT_DESCRIPTION for t in after):
        raise InvalidChange(f"Performance Max needs at least one description of {_SHORT_DESCRIPTION} characters or fewer")

    new = _NewAssets(cid)
    adds = [
        OpSpec(
            "asset_group_asset",
            "create",
            None,
            {
                "asset_group": group["resource_name"],
                "asset": new.reuse_or_create(reader, "TEXT", t, {"text_asset.text": t}),
                "field_type": ch.field_type,
            },
        )
        for t in add
    ]
    removes = [OpSpec("asset_group_asset", "remove", link["resource_name"]) for t in remove for link in linked[t]]
    summary = [f"Change the {label}s of {where} ({len(before)} now, {len(after)} after)"]
    summary += [f"Add {label}: “{t}”" for t in add]
    summary += [f"Remove {label}: “{t}”" for t in remove]
    return Plan(
        summary,
        {"texts": before},
        {"texts": after},
        new.ops + _ordered(adds, removes, len(before), fewest),
        campaign_ids={group["campaign_id"]},
    )


# -- asset_group_video ----------------------------------------------------------------------------

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MAX_VIDEOS = 5


def _video_label(video_id: str, title: str) -> str:
    return f"“{title}” (youtu.be/{video_id})"


def _asset_group_video_current(ch: AssetGroupVideo, reader: AccountReader, cid: str) -> dict:
    _asset_group(reader, cid, ch.asset_group_id)
    links = _group_links(reader, cid, ch.asset_group_id, "YOUTUBE_VIDEO")
    return {"videos": sorted(link["asset"]["youtube_video_id"] for link in links)}


def _asset_group_video_plan(ch: AssetGroupVideo, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    group = _asset_group(reader, cid, ch.asset_group_id)
    where = _group_label(group)
    add = [v.strip() for v in ch.add]
    remove = [v.strip() for v in ch.remove]
    if not add and not remove:
        raise InvalidChange("give at least one video to add or remove")
    bad = [v for v in add if not _YOUTUBE_ID.match(v)]
    if bad:
        raise InvalidChange(f"not YouTube video IDs (11 characters, e.g. dQw4w9WgXcQ): {', '.join(bad)}")
    _check_unique(add + remove, "videos")

    links = _group_links(reader, cid, group["id"], "YOUTUBE_VIDEO")
    already = [v for v in add if any(link["asset"]["youtube_video_id"] == v for link in links)]
    if already:
        raise InvalidChange(f"already in {where}: {', '.join(already)}")
    removing = []
    for item in remove:
        match = [link for link in links if item in (link["asset"]["id"], link["asset"]["youtube_video_id"])]
        if not match:
            raise InvalidChange(f"no video {item} in {where}, so it can't be removed")
        removing += match

    before = sorted(link["asset"]["youtube_video_id"] for link in links)
    gone = {link["asset"]["youtube_video_id"] for link in removing}
    after = sorted([v for v in before if v not in gone] + add)
    if len(after) > _MAX_VIDEOS:
        raise InvalidChange(f"{where} would have {len(after)} videos; Performance Max allows {_MAX_VIDEOS}")

    new = _NewAssets(cid)
    adds, summary = [], []
    for video_id in add:
        existing = reader.find_asset(cid, "YOUTUBE_VIDEO", video_id)
        title = (existing or {}).get("youtube_video_title") or reader.youtube_title(video_id)
        if not title:
            raise InvalidChange(f"YouTube has no public or unlisted video {video_id}")
        asset = existing["resource_name"] if existing else new.create({"youtube_video_asset.youtube_video_id": video_id})
        adds.append(OpSpec("asset_group_asset", "create", None, {"asset_group": group["resource_name"], "asset": asset, "field_type": "YOUTUBE_VIDEO"}))
        summary.append(f"Add video {_video_label(video_id, title)} to {where}")
    for link in removing:
        asset = link["asset"]
        summary.append(
            f"Remove video {_video_label(asset['youtube_video_id'], asset['youtube_video_title'] or '(untitled)')} "
            f"(asset {asset['id']}) from {where}"
        )
    removes = [OpSpec("asset_group_asset", "remove", link["resource_name"]) for link in removing]
    return Plan(summary, {"videos": before}, {"videos": after}, new.ops + removes + adds, campaign_ids={group["campaign_id"]})


# -- sitelinks ----------------------------------------------------------------------------------------

_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5


def _landing_page(reader: AccountReader, url: str, rules: Rules) -> str:
    """Loads url, following redirects, and returns where it ends. Refuses anything but HTTP 200 on an allowed host."""
    if not rules.allowed_url_hosts:
        raise RuleViolation("no allowed_url_hosts are configured, so gamm can't add links")
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise InvalidChange(f"{current} isn't a web address")
        host = parsed.hostname.lower()
        if host not in rules.allowed_url_hosts:
            raise RuleViolation(
                f"{current} is on {host}, which isn't an allowed host ({', '.join(rules.allowed_url_hosts)})"
            )
        response = reader.http_get(current)
        if response["status"] in _REDIRECTS and response.get("location"):
            current = urljoin(current, response["location"])
            continue
        if response["status"] != 200:
            at = f" (at {current})" if current != url else ""
            raise InvalidChange(f"{url} returned HTTP {response['status']}{at}; links must load")
        return current
    raise InvalidChange(f"{url} redirects more than {_MAX_REDIRECTS} times")


def _sitelink_view(asset: dict) -> dict:
    return {
        "link_text": asset["link_text"],
        "final_url": (asset["final_urls"] or [""])[0],
        "description1": asset["description1"],
        "description2": asset["description2"],
    }


def _sorted_sitelinks(views: list[dict]) -> list[dict]:
    return sorted(views, key=lambda v: (v["link_text"], v["final_url"], v["description1"], v["description2"]))


def _sitelink_label(view: dict) -> str:
    text = f"“{view['link_text']}” → {view['final_url']}"
    if view["description1"] or view["description2"]:
        text += f" (“{view['description1']}” / “{view['description2']}”)"
    return text


def _sitelinks_current(ch: Sitelinks, reader: AccountReader, cid: str) -> dict:
    campaign, _ = _scope(reader, cid, ch.scope)
    links = _scope_links(reader, cid, campaign, ["SITELINK"])
    return {"sitelinks": _sorted_sitelinks([_sitelink_view(link["asset"]) for link in links])}


def _sitelinks_plan(ch: Sitelinks, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign, where = _scope(reader, cid, ch.scope)
    if not ch.add and not ch.remove:
        raise InvalidChange("give at least one sitelink to add or remove")
    views = []
    for item in ch.add:
        view = {
            "link_text": _clean(item.link_text),
            "final_url": item.final_url.strip(),
            "description1": _clean(item.description1),
            "description2": _clean(item.description2),
        }
        _check_lengths([view["link_text"]], 25, "sitelink text")
        if bool(view["description1"]) != bool(view["description2"]):
            raise InvalidChange(f"sitelink “{view['link_text']}”: give both descriptions or neither")
        if view["description1"]:
            _check_lengths([view["description1"], view["description2"]], 35, "a sitelink description")
        views.append(view)
    remove = [_clean(t) for t in ch.remove]
    _check_unique([v["link_text"] for v in views] + remove, "sitelinks", key=_fold)

    links = _scope_links(reader, cid, campaign, ["SITELINK"])
    linked: dict[str, list[dict]] = {}
    for link in links:
        linked.setdefault(_fold(link["asset"]["link_text"]), []).append(link)
    _missing_or_present([v["link_text"] for v in views], remove, linked, where, _fold)

    new = _NewAssets(cid)
    adds, summary = [], []
    for view in views:
        landed = _landing_page(reader, view["final_url"], rules)
        fields = {"sitelink_asset.link_text": view["link_text"], "final_urls": [view["final_url"]]}
        if view["description1"]:
            fields["sitelink_asset.description1"] = view["description1"]
            fields["sitelink_asset.description2"] = view["description2"]
        adds.append(_link(cid, campaign, new.create(fields), "SITELINK"))
        redirect = f" (redirects to {landed})" if landed != view["final_url"] else ""
        summary.append(f"Add sitelink to {where}: {_sitelink_label(view)}{redirect}")
    removing = [link for t in remove for link in linked[_fold(t)]]
    for link in removing:
        summary.append(f"Remove sitelink from {where}: {_sitelink_label(_sitelink_view(link['asset']))}")

    before = _sorted_sitelinks([_sitelink_view(link["asset"]) for link in links])
    kept = [_sitelink_view(link["asset"]) for link in links if link not in removing]
    return Plan(
        summary,
        {"sitelinks": before},
        {"sitelinks": _sorted_sitelinks(kept + views)},
        new.ops + [_unlink(campaign, link) for link in removing] + adds,
        **_scope_rules(campaign),
    )


# -- callouts ---------------------------------------------------------------------------------------------


def _callouts_current(ch: Callouts, reader: AccountReader, cid: str) -> dict:
    campaign, _ = _scope(reader, cid, ch.scope)
    return {"callouts": sorted(link["asset"]["text"] for link in _scope_links(reader, cid, campaign, ["CALLOUT"]))}


def _callouts_plan(ch: Callouts, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign, where = _scope(reader, cid, ch.scope)
    add = [_clean(t) for t in ch.add]
    remove = [_clean(t) for t in ch.remove]
    if not add and not remove:
        raise InvalidChange("give at least one callout to add or remove")
    _check_lengths(add, 25, "a callout")
    _check_unique(add + remove, "callouts", key=_fold)

    links = _scope_links(reader, cid, campaign, ["CALLOUT"])
    linked: dict[str, list[dict]] = {}
    for link in links:
        linked.setdefault(_fold(link["asset"]["text"]), []).append(link)
    _missing_or_present(add, remove, linked, where, _fold)

    new = _NewAssets(cid)
    adds = [
        _link(cid, campaign, new.reuse_or_create(reader, "CALLOUT", t, {"callout_asset.callout_text": t}), "CALLOUT")
        for t in add
    ]
    removing = [link for t in remove for link in linked[_fold(t)]]
    before = sorted(link["asset"]["text"] for link in links)
    after = sorted([link["asset"]["text"] for link in links if link not in removing] + add)
    summary = [f"Add callout to {where}: “{t}”" for t in add]
    summary += [f"Remove callout from {where}: “{link['asset']['text']}”" for link in removing]
    return Plan(
        summary,
        {"callouts": before},
        {"callouts": after},
        new.ops + [_unlink(campaign, link) for link in removing] + adds,
        **_scope_rules(campaign),
    )


# -- structured_snippet ------------------------------------------------------------------------------------


def _snippet_links(reader: AccountReader, cid: str, campaign: dict | None, header: str) -> list[dict]:
    return [link for link in _scope_links(reader, cid, campaign, ["STRUCTURED_SNIPPET"]) if link["asset"]["header"] == header]


def _snippet_current(ch: StructuredSnippet, reader: AccountReader, cid: str) -> dict:
    campaign, _ = _scope(reader, cid, ch.scope)
    return {"values": sorted(link["asset"]["values"] for link in _snippet_links(reader, cid, campaign, ch.header))}


def _snippet_plan(ch: StructuredSnippet, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign, where = _scope(reader, cid, ch.scope)
    values = [_clean(v) for v in ch.values]
    if not 3 <= len(values) <= 10:
        raise InvalidChange(f"a structured snippet needs 3 to 10 values, not {len(values)}")
    _check_lengths(values, 25, "a snippet value")
    _check_unique(values, "snippet values", key=_fold)

    links = _snippet_links(reader, cid, campaign, ch.header)
    before = sorted(link["asset"]["values"] for link in links)
    if before == [values]:
        raise InvalidChange(f"the “{ch.header}” snippet of {where} already has these values")
    new = _NewAssets(cid)
    asset = new.create({"structured_snippet_asset.header": ch.header, "structured_snippet_asset.values": values})
    now = "; ".join(", ".join(v) for v in before) or "no snippet with this header"
    return Plan(
        [f"Set the “{ch.header}” structured snippet of {where} to: {', '.join(values)} (now: {now})"],
        {"values": before},
        {"values": [values]},
        new.ops + [_unlink(campaign, link) for link in links] + [_link(cid, campaign, asset, "STRUCTURED_SNIPPET")],
        **_scope_rules(campaign),
    )


# -- unlink_assets -------------------------------------------------------------------------------------------

_UNLINKABLE = ["PRICE", "PROMOTION", "SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"]


def _describe_asset(asset: dict, currency: str) -> str:
    kind = asset["type"]
    if kind == "SITELINK":
        return f"sitelink {_sitelink_label(_sitelink_view(asset))}"
    if kind == "CALLOUT":
        return f"callout “{asset['text']}”"
    if kind == "STRUCTURED_SNIPPET":
        return f"structured snippet “{asset['header']}”: {', '.join(asset['values'])}"
    if kind == "PRICE":
        offers = "; ".join(
            f"“{o['header']}” {money(o['price_micros'], o['currency'] or currency)} → {o['final_url']}" for o in asset["offerings"]
        )
        return f"price asset ({asset['price_type'].lower().replace('_', ' ')}): {offers or 'no offerings'}"
    if kind == "PROMOTION":
        parts = [f"promotion “{asset['target']}”"]
        if asset["percent_off_micros"]:
            parts.append(f"{asset['percent_off_micros'] / 10_000:g}% off")
        if asset["money_off_micros"]:
            parts.append(f"{money(asset['money_off_micros'], asset['currency'] or currency)} off")
        if asset["end_date"]:
            parts.append(f"ends {asset['end_date']}")
        return ", ".join(parts) + (f" → {', '.join(asset['final_urls'])}" if asset["final_urls"] else "")
    return kind.lower()


def _unlink_ids(ch: UnlinkAssets) -> list[str]:
    ids = [i.strip() for i in ch.asset_ids]
    bad = [i for i in ids if not i.isdigit()]
    if bad:
        raise InvalidChange(f"asset IDs are numbers: {', '.join(bad)}")
    _check_unique(ids, "asset IDs")
    return ids


def _unlink_current(ch: UnlinkAssets, reader: AccountReader, cid: str) -> dict:
    campaign, _ = _scope(reader, cid, ch.scope)
    ids = set(_unlink_ids(ch))
    return {"linked": sorted({link["asset"]["id"] for link in _scope_links(reader, cid, campaign, _UNLINKABLE)} & ids)}


def _unlink_plan(ch: UnlinkAssets, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    campaign, where = _scope(reader, cid, ch.scope)
    ids = _unlink_ids(ch)
    links = _scope_links(reader, cid, campaign, _UNLINKABLE)
    missing = [i for i in ids if not any(link["asset"]["id"] == i for link in links)]
    if missing:
        raise InvalidChange(
            f"not linked to {where} as a price, promotion, sitelink, callout or structured snippet: {', '.join(missing)}"
        )
    removing = [link for link in links if link["asset"]["id"] in ids]
    summary = [
        f"Unlink asset {link['asset']['id']} from {where}"
        + (" (link paused)" if link.get("status") == "PAUSED" else "")
        + f": {_describe_asset(link['asset'], currency)}"
        for link in removing
    ]
    return Plan(
        summary,
        {"linked": sorted(ids)},
        {"linked": []},
        [_unlink(campaign, link) for link in removing],
        **_scope_rules(campaign),
    )


# -- registry -------------------------------------------------------------------------------------

KINDS: dict[str, tuple[Callable[..., dict], Callable[..., Plan]]] = {
    "campaign_status": (_campaign_status_current, _campaign_status_plan),
    "asset_group_status": (_asset_group_status_current, _asset_group_status_plan),
    "campaign_budget": (_campaign_budget_current, _campaign_budget_plan),
    "target_roas": (_target_roas_current, _target_roas_plan),
    "campaign_negative_keywords": (_campaign_negatives_current, _campaign_negatives_plan),
    "shared_negative_keywords": (_shared_negatives_current, _shared_negatives_plan),
    "campaign_url_suffix": (_url_suffix_current, _url_suffix_plan),
    "conversion_goal": (_goal_current, _goal_plan),
    "asset_group_text": (_asset_group_text_current, _asset_group_text_plan),
    "asset_group_video": (_asset_group_video_current, _asset_group_video_plan),
    "sitelinks": (_sitelinks_current, _sitelinks_plan),
    "callouts": (_callouts_current, _callouts_plan),
    "structured_snippet": (_snippet_current, _snippet_plan),
    "unlink_assets": (_unlink_current, _unlink_plan),
}


def current_values(change: BaseModel, reader: AccountReader, customer_id: str) -> dict:
    return KINDS[change.kind][0](change, reader, customer_id)


def plan_change(change: BaseModel, reader: AccountReader, customer_id: str, rules: Rules, currency: str) -> Plan:
    return KINDS[change.kind][1](change, reader, customer_id, rules, currency)
