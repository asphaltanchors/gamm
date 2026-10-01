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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

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


Change = Annotated[
    CampaignStatus
    | AssetGroupStatus
    | CampaignBudget
    | TargetRoas
    | CampaignNegativeKeywords
    | SharedNegativeKeywords
    | CampaignUrlSuffix
    | ConversionGoal,
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


def _asset_group_status_plan(ch: AssetGroupStatus, reader: AccountReader, cid: str, rules: Rules, currency: str) -> Plan:
    group = _asset_group(reader, cid, ch.asset_group_id)
    label = f"asset group “{group['name']}” ({group['id']}) in campaign “{group['campaign_name']}”"
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
}


def current_values(change: BaseModel, reader: AccountReader, customer_id: str) -> dict:
    return KINDS[change.kind][0](change, reader, customer_id)


def plan_change(change: BaseModel, reader: AccountReader, customer_id: str, rules: Rules, currency: str) -> Plan:
    return KINDS[change.kind][1](change, reader, customer_id, rules, currency)
