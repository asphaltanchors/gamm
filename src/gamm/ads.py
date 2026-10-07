"""The Google Ads side: typed account reads and mutate calls.

Change kinds (changes.py) never touch the API directly. They read through the
`AccountReader` methods and describe writes as `OpSpec`s, which this module turns
into one atomic GoogleAdsService.Mutate request. Tests swap in a fake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import google.auth
import httpx
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

from gamm.config import Config

log = logging.getLogger(__name__)

# The Google Ads API version gamm's write path is written against. Google retires
# each version roughly a year after release; see docs/updating.md.
API_VERSION = "v25"
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


class AdsError(RuntimeError):
    """A Google Ads API call failed. The message is safe to show to a bot."""


@dataclass(frozen=True)
class OpSpec:
    """One write, described independently of the client library.

    resource: the MutateOperation field without "_operation", e.g. "campaign".
    action: "update", "create" or "remove".
    resource_name: required for update and remove. On create, an optional
      temporary name with a negative ID (customers/1/assets/-1), so later
      operations in the same request can refer to the new resource.
    fields: dotted field paths to values; enum values are given by name.
    """

    resource: str
    action: str
    resource_name: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "action": self.action,
            "resource_name": self.resource_name,
            "fields": self.fields,
        }


class AccountReader(Protocol):
    def customer(self, customer_id: str) -> dict: ...
    def campaign(self, customer_id: str, campaign_id: str) -> dict | None: ...
    def budget(self, customer_id: str, resource_name: str) -> dict | None: ...
    def asset_group(self, customer_id: str, asset_group_id: str) -> dict | None: ...
    def campaign_negative_keywords(self, customer_id: str, campaign_id: str) -> list[dict]: ...
    def shared_set(self, customer_id: str, shared_set_id: str) -> dict | None: ...
    def shared_set_keywords(self, customer_id: str, shared_set_id: str) -> list[dict]: ...
    def shared_set_campaigns(self, customer_id: str, shared_set_id: str) -> list[str]: ...
    def customer_conversion_goals(self, customer_id: str) -> list[dict]: ...
    def campaign_conversion_goals(self, customer_id: str, campaign_id: str) -> list[dict]: ...
    def asset_group_assets(self, customer_id: str, asset_group_id: str) -> list[dict]: ...
    def linked_assets(self, customer_id: str, campaign_id: str | None, field_types: list[str]) -> list[dict]: ...
    def find_asset(self, customer_id: str, asset_type: str, value: str) -> dict | None: ...
    # Not Google Ads, but read the same way: gamm checks these itself rather than
    # trusting the bot's description of them.
    def http_get(self, url: str) -> dict: ...
    def youtube_title(self, video_id: str) -> str | None: ...


class AdsBackend(AccountReader, Protocol):
    def mutate(self, customer_id: str, ops: list[OpSpec], validate_only: bool) -> list[str]: ...


# Enum-typed fields, keyed by (resource, field path).
_ENUM_FIELDS = {
    ("campaign", "status"): "CampaignStatusEnum",
    ("asset_group", "status"): "AssetGroupStatusEnum",
    ("campaign_criterion", "keyword.match_type"): "KeywordMatchTypeEnum",
    ("shared_criterion", "keyword.match_type"): "KeywordMatchTypeEnum",
    ("asset_group_asset", "field_type"): "AssetFieldTypeEnum",
    ("customer_asset", "field_type"): "AssetFieldTypeEnum",
    ("campaign_asset", "field_type"): "AssetFieldTypeEnum",
}

# Every asset read selects these, so link lists can describe what they point to.
_ASSET_FIELDS = (
    "asset.id, asset.resource_name, asset.type, asset.final_urls, asset.text_asset.text, "
    "asset.callout_asset.callout_text, asset.sitelink_asset.link_text, asset.sitelink_asset.description1, "
    "asset.sitelink_asset.description2, asset.structured_snippet_asset.header, asset.structured_snippet_asset.values, "
    "asset.youtube_video_asset.youtube_video_id, asset.youtube_video_asset.youtube_video_title, "
    "asset.price_asset.type, asset.price_asset.price_offerings, asset.promotion_asset.promotion_target, "
    "asset.promotion_asset.percent_off, asset.promotion_asset.money_amount_off.amount_micros, "
    "asset.promotion_asset.money_amount_off.currency_code, asset.promotion_asset.end_date"
)

# How find_asset looks an asset up by its content.
_FIND_ASSET = {
    "TEXT": "asset.text_asset.text",
    "CALLOUT": "asset.callout_asset.callout_text",
    "YOUTUBE_VIDEO": "asset.youtube_video_asset.youtube_video_id",
}

HTTP_USER_AGENT = "Mozilla/5.0 (compatible; gamm landing-page check; +https://github.com/asphaltanchors/gamm)"


def _set_path(message: Any, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        message = getattr(message, part)
    setattr(message, last, value)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _enum_name(value: Any) -> str:
    return getattr(value, "name", str(value))


def _asset(a: Any) -> dict:
    """An asset as a plain dict: its id, type, URLs and the content for its type."""
    kind = _enum_name(a.type_)
    out: dict[str, Any] = {"id": str(a.id), "resource_name": a.resource_name, "type": kind, "final_urls": list(a.final_urls)}
    if kind == "TEXT":
        out["text"] = a.text_asset.text
    elif kind == "CALLOUT":
        out["text"] = a.callout_asset.callout_text
    elif kind == "SITELINK":
        s = a.sitelink_asset
        out.update(link_text=s.link_text, description1=s.description1, description2=s.description2)
    elif kind == "STRUCTURED_SNIPPET":
        out.update(header=a.structured_snippet_asset.header, values=list(a.structured_snippet_asset.values))
    elif kind == "YOUTUBE_VIDEO":
        v = a.youtube_video_asset
        out.update(youtube_video_id=v.youtube_video_id, youtube_video_title=v.youtube_video_title)
    elif kind == "PRICE":
        out["price_type"] = _enum_name(a.price_asset.type_)
        out["offerings"] = [
            {
                "header": o.header,
                "description": o.description,
                "price_micros": o.price.amount_micros,
                "currency": o.price.currency_code,
                "final_url": o.final_url,
            }
            for o in a.price_asset.price_offerings
        ]
    elif kind == "PROMOTION":
        p = a.promotion_asset
        out.update(
            target=p.promotion_target,
            percent_off_micros=p.percent_off,  # 1,000,000 is 100%
            money_off_micros=p.money_amount_off.amount_micros,
            currency=p.money_amount_off.currency_code,
            end_date=p.end_date,
        )
    return out


class GoogleAdsBackend:
    def __init__(self, config: Config):
        credentials, _ = google.auth.load_credentials_from_file(
            str(config.credentials_file), scopes=[ADS_SCOPE]
        )
        kwargs: dict[str, Any] = {"credentials": credentials, "use_proto_plus": True, "version": API_VERSION}
        if config.developer_token:
            kwargs["developer_token"] = config.developer_token
        if config.login_customer_id:
            kwargs["login_customer_id"] = config.login_customer_id
        self.client = GoogleAdsClient(**kwargs)

    # -- plumbing ---------------------------------------------------------

    def _search(self, customer_id: str, query: str) -> list[Any]:
        service = self.client.get_service("GoogleAdsService")
        try:
            return [row for batch in service.search_stream(customer_id=customer_id, query=query) for row in batch.results]
        except GoogleAdsException as exc:
            raise AdsError(_describe_failure(exc)) from exc

    def build_operations(self, customer_id: str, ops: list[OpSpec]) -> list[Any]:
        built = []
        for spec in ops:
            operation = self.client.get_type("MutateOperation")
            sub = getattr(operation, f"{spec.resource}_operation")
            if spec.action == "remove":
                sub.remove = spec.resource_name
            else:
                target = sub.update if spec.action == "update" else sub.create
                if spec.resource_name:
                    target.resource_name = spec.resource_name
                for path, value in spec.fields.items():
                    enum = _ENUM_FIELDS.get((spec.resource, path))
                    if enum:
                        value = getattr(getattr(self.client.enums, enum), value)
                    _set_path(target, path, value)
                if spec.action == "update":
                    self.client.copy_from(sub.update_mask, field_mask_pb2.FieldMask(paths=list(spec.fields)))
            built.append(operation)
        return built

    def mutate(self, customer_id: str, ops: list[OpSpec], validate_only: bool) -> list[str]:
        service = self.client.get_service("GoogleAdsService")
        request = self.client.get_type("MutateGoogleAdsRequest")
        request.customer_id = customer_id
        request.mutate_operations.extend(self.build_operations(customer_id, ops))
        request.validate_only = validate_only
        try:
            response = service.mutate(request=request)
        except GoogleAdsException as exc:
            raise AdsError(_describe_failure(exc)) from exc
        names = []
        for result in response.mutate_operation_responses:
            kind = result._pb.WhichOneof("response")
            if kind:
                names.append(getattr(result, kind).resource_name)
        return names

    # -- reads ------------------------------------------------------------

    def customer(self, customer_id: str) -> dict:
        rows = self._search(customer_id, "SELECT customer.id, customer.descriptive_name, customer.currency_code FROM customer")
        c = rows[0].customer
        return {"id": str(c.id), "name": c.descriptive_name, "currency_code": c.currency_code}

    def campaign(self, customer_id: str, campaign_id: str) -> dict | None:
        rows = self._search(
            customer_id,
            "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type, campaign.bidding_strategy, "
            "campaign.maximize_conversion_value.target_roas, campaign.target_roas.target_roas, "
            "campaign.campaign_budget, campaign.final_url_suffix "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}",
        )
        if not rows:
            return None
        c = rows[0].campaign
        return {
            "id": str(c.id),
            "name": c.name,
            "status": _enum_name(c.status),
            "channel_type": _enum_name(c.advertising_channel_type),
            "bidding_strategy_type": _enum_name(c.bidding_strategy_type),
            "portfolio_bidding_strategy": c.bidding_strategy or None,
            "maximize_conversion_value_target_roas": c.maximize_conversion_value.target_roas,
            "target_roas_target_roas": c.target_roas.target_roas,
            "budget_resource_name": c.campaign_budget,
            "final_url_suffix": c.final_url_suffix,
        }

    def budget(self, customer_id: str, resource_name: str) -> dict | None:
        rows = self._search(
            customer_id,
            "SELECT campaign_budget.resource_name, campaign_budget.id, campaign_budget.name, "
            "campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
            "campaign_budget.reference_count, campaign_budget.period "
            f"FROM campaign_budget WHERE campaign_budget.resource_name = {_quote(resource_name)}",
        )
        if not rows:
            return None
        b = rows[0].campaign_budget
        return {
            "resource_name": b.resource_name,
            "id": str(b.id),
            "name": b.name,
            "amount_micros": b.amount_micros,
            "explicitly_shared": b.explicitly_shared,
            "reference_count": b.reference_count,
            "period": _enum_name(b.period),
        }

    def asset_group(self, customer_id: str, asset_group_id: str) -> dict | None:
        rows = self._search(
            customer_id,
            "SELECT asset_group.id, asset_group.resource_name, asset_group.name, asset_group.status, "
            "campaign.id, campaign.name "
            f"FROM asset_group WHERE asset_group.id = {int(asset_group_id)}",
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "id": str(row.asset_group.id),
            "resource_name": row.asset_group.resource_name,
            "name": row.asset_group.name,
            "status": _enum_name(row.asset_group.status),
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
        }

    def _keywords(self, rows: list[Any], attr: str) -> list[dict]:
        result = []
        for row in rows:
            crit = getattr(row, attr)
            result.append(
                {
                    "resource_name": crit.resource_name,
                    "criterion_id": str(crit.criterion_id),
                    "text": crit.keyword.text,
                    "match_type": _enum_name(crit.keyword.match_type),
                }
            )
        return result

    def campaign_negative_keywords(self, customer_id: str, campaign_id: str) -> list[dict]:
        rows = self._search(
            customer_id,
            "SELECT campaign_criterion.resource_name, campaign_criterion.criterion_id, "
            "campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
            "FROM campaign_criterion "
            f"WHERE campaign.id = {int(campaign_id)} AND campaign_criterion.negative = TRUE "
            "AND campaign_criterion.type = 'KEYWORD' AND campaign_criterion.status != 'REMOVED'",
        )
        return self._keywords(rows, "campaign_criterion")

    def shared_set(self, customer_id: str, shared_set_id: str) -> dict | None:
        rows = self._search(
            customer_id,
            "SELECT shared_set.id, shared_set.resource_name, shared_set.name, shared_set.type, "
            f"shared_set.status FROM shared_set WHERE shared_set.id = {int(shared_set_id)}",
        )
        if not rows:
            return None
        s = rows[0].shared_set
        return {
            "id": str(s.id),
            "resource_name": s.resource_name,
            "name": s.name,
            "type": _enum_name(s.type_),
            "status": _enum_name(s.status),
        }

    def shared_set_keywords(self, customer_id: str, shared_set_id: str) -> list[dict]:
        rows = self._search(
            customer_id,
            "SELECT shared_criterion.resource_name, shared_criterion.criterion_id, "
            "shared_criterion.keyword.text, shared_criterion.keyword.match_type "
            f"FROM shared_criterion WHERE shared_set.id = {int(shared_set_id)} "
            "AND shared_criterion.type = 'KEYWORD'",
        )
        return self._keywords(rows, "shared_criterion")

    def shared_set_campaigns(self, customer_id: str, shared_set_id: str) -> list[str]:
        rows = self._search(
            customer_id,
            "SELECT campaign.id FROM campaign_shared_set "
            f"WHERE shared_set.id = {int(shared_set_id)} AND campaign_shared_set.status = 'ENABLED'",
        )
        return [str(row.campaign.id) for row in rows]

    def customer_conversion_goals(self, customer_id: str) -> list[dict]:
        rows = self._search(
            customer_id,
            "SELECT customer_conversion_goal.resource_name, customer_conversion_goal.category, "
            "customer_conversion_goal.origin, customer_conversion_goal.biddable "
            "FROM customer_conversion_goal",
        )
        return [
            {
                "resource_name": r.customer_conversion_goal.resource_name,
                "category": _enum_name(r.customer_conversion_goal.category),
                "origin": _enum_name(r.customer_conversion_goal.origin),
                "biddable": r.customer_conversion_goal.biddable,
            }
            for r in rows
        ]

    def campaign_conversion_goals(self, customer_id: str, campaign_id: str) -> list[dict]:
        rows = self._search(
            customer_id,
            "SELECT campaign_conversion_goal.resource_name, campaign_conversion_goal.category, "
            "campaign_conversion_goal.origin, campaign_conversion_goal.biddable "
            f"FROM campaign_conversion_goal WHERE campaign.id = {int(campaign_id)}",
        )
        return [
            {
                "resource_name": r.campaign_conversion_goal.resource_name,
                "category": _enum_name(r.campaign_conversion_goal.category),
                "origin": _enum_name(r.campaign_conversion_goal.origin),
                "biddable": r.campaign_conversion_goal.biddable,
            }
            for r in rows
        ]

    def asset_group_assets(self, customer_id: str, asset_group_id: str) -> list[dict]:
        rows = self._search(
            customer_id,
            f"SELECT asset_group_asset.resource_name, asset_group_asset.field_type, {_ASSET_FIELDS} "
            f"FROM asset_group_asset WHERE asset_group.id = {int(asset_group_id)} "
            "AND asset_group_asset.status != 'REMOVED'",
        )
        return [
            {
                "resource_name": r.asset_group_asset.resource_name,
                "field_type": _enum_name(r.asset_group_asset.field_type),
                "asset": _asset(r.asset),
            }
            for r in rows
        ]

    def linked_assets(self, customer_id: str, campaign_id: str | None, field_types: list[str]) -> list[dict]:
        """Assets linked to the account (campaign_id None) or to one campaign, of the given field types."""
        link = "campaign_asset" if campaign_id else "customer_asset"
        types = ", ".join(_quote(t) for t in field_types)
        where = f"campaign.id = {int(campaign_id)} AND " if campaign_id else ""
        rows = self._search(
            customer_id,
            f"SELECT {link}.resource_name, {link}.field_type, {link}.status, {_ASSET_FIELDS} FROM {link} "
            f"WHERE {where}{link}.field_type IN ({types}) AND {link}.status != 'REMOVED'",
        )
        return [
            {
                "resource_name": getattr(r, link).resource_name,
                "field_type": _enum_name(getattr(r, link).field_type),
                "status": _enum_name(getattr(r, link).status),
                "asset": _asset(r.asset),
            }
            for r in rows
        ]

    def find_asset(self, customer_id: str, asset_type: str, value: str) -> dict | None:
        """The oldest asset of this type with exactly this text (or YouTube ID), to reuse it."""
        rows = self._search(
            customer_id,
            f"SELECT {_ASSET_FIELDS} FROM asset WHERE asset.type = {_quote(asset_type)} "
            f"AND {_FIND_ASSET[asset_type]} = {_quote(value)} ORDER BY asset.id LIMIT 1",
        )
        return _asset(rows[0].asset) if rows else None

    # -- the web ----------------------------------------------------------

    def http_get(self, url: str) -> dict:
        """One GET, without following redirects: {"status", "location"}."""
        try:
            with (
                httpx.Client(follow_redirects=False, timeout=15, headers={"User-Agent": HTTP_USER_AGENT}) as client,
                client.stream("GET", url) as response,
            ):
                return {"status": response.status_code, "location": response.headers.get("location")}
        except httpx.HTTPError as exc:
            raise AdsError(f"couldn't load {url}: {exc}") from exc

    def youtube_title(self, video_id: str) -> str | None:
        """The public title of a public or unlisted YouTube video, or None if YouTube won't say."""
        try:
            response = httpx.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise AdsError(f"couldn't ask YouTube about video {video_id}: {exc}") from exc
        if response.status_code != 200:
            return None
        return response.json().get("title") or None


def _describe_failure(exc: GoogleAdsException) -> str:
    messages = []
    for error in exc.failure.errors:
        location = ".".join(
            element.field_name + (f"[{element.index}]" if "index" in element else "")
            for element in error.location.field_path_elements
        )
        messages.append(f"{error.message}" + (f" (at {location})" if location else ""))
    return f"Google Ads API error (request {exc.request_id}): " + "; ".join(messages)
