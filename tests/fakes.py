"""An in-memory Google Ads account and a software passkey, for tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from gamm.ads import AdsError, OpSpec

CID = "1112223333"

_CAMPAIGN_FIELDS = {
    "status": "status",
    "maximize_conversion_value.target_roas": "maximize_conversion_value_target_roas",
    "target_roas.target_roas": "target_roas_target_roas",
    "final_url_suffix": "final_url_suffix",
}


def campaign_rn(campaign_id: str) -> str:
    return f"customers/{CID}/campaigns/{campaign_id}"


class FakeAccount:
    def __init__(self):
        self.currency = "USD"
        self.campaigns = {
            "100": {
                "id": "100",
                "name": "Main PMax",
                "status": "ENABLED",
                "channel_type": "PERFORMANCE_MAX",
                "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE",
                "portfolio_bidding_strategy": None,
                "maximize_conversion_value_target_roas": 2.0,
                "target_roas_target_roas": 0.0,
                "budget_resource_name": f"customers/{CID}/campaignBudgets/900",
                "final_url_suffix": "utm_source=google",
            },
            "200": {
                "id": "200",
                "name": "Accessories",
                "status": "ENABLED",
                "channel_type": "PERFORMANCE_MAX",
                "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE",
                "portfolio_bidding_strategy": None,
                "maximize_conversion_value_target_roas": 2.5,
                "target_roas_target_roas": 0.0,
                "budget_resource_name": f"customers/{CID}/campaignBudgets/901",
                "final_url_suffix": "",
            },
            "300": {
                "id": "300",
                "name": "Old search",
                "status": "PAUSED",
                "channel_type": "SEARCH",
                "bidding_strategy_type": "MANUAL_CPC",
                "portfolio_bidding_strategy": None,
                "maximize_conversion_value_target_roas": 0.0,
                "target_roas_target_roas": 0.0,
                "budget_resource_name": f"customers/{CID}/campaignBudgets/902",
                "final_url_suffix": "",
            },
        }
        self.budgets = {
            f"customers/{CID}/campaignBudgets/900": self._budget("900", 100_000_000),
            f"customers/{CID}/campaignBudgets/901": self._budget("901", 30_000_000),
            f"customers/{CID}/campaignBudgets/902": self._budget("902", 10_000_000, shared=True),
        }
        self.asset_groups = {
            "500": {
                "id": "500",
                "resource_name": f"customers/{CID}/assetGroups/500",
                "name": "Accessories",
                "status": "PAUSED",
                "campaign_id": "100",
                "campaign_name": "Main PMax",
            }
        }
        self.campaign_negatives = {
            "100": [self._kw("campaignCriteria", "100", "1", "competitor", "EXACT")],
            "200": [],
            "300": [],
        }
        self.shared_sets = {
            "700": {"id": "700", "resource_name": f"customers/{CID}/sharedSets/700", "name": "Brand Terms", "type": "NEGATIVE_KEYWORDS", "status": "ENABLED"},
            "701": {"id": "701", "resource_name": f"customers/{CID}/sharedSets/701", "name": "Excluded channels", "type": "NEGATIVE_PLACEMENTS", "status": "ENABLED"},
        }
        self.shared_keywords = {"700": [self._kw("sharedCriteria", "700", "5", "brand name", "PHRASE")], "701": []}
        self.shared_campaigns = {"700": ["100", "200"], "701": []}
        self.customer_goals = [
            self._goal(f"customers/{CID}/customerConversionGoals/{c}~{o}", c, o, b)
            for c, o, b in [("PURCHASE", "WEBSITE", True), ("PAGE_VIEW", "WEBSITE", True), ("ENGAGEMENT", "YOUTUBE_HOSTED", True)]
        ]
        self.campaign_goals = {
            "100": [
                self._goal(f"customers/{CID}/campaignConversionGoals/100~{c}~{o}", c, o, b)
                for c, o, b in [("PURCHASE", "WEBSITE", True), ("PAGE_VIEW", "WEBSITE", False)]
            ]
        }
        self.mutations: list[tuple[bool, list[OpSpec]]] = []
        self.fail_mutate: str | None = None
        self.fail_dry_run: str | None = None
        self.ignore_writes = False
        self._next_id = 1000

    @staticmethod
    def _budget(budget_id: str, micros: int, shared: bool = False) -> dict:
        return {
            "resource_name": f"customers/{CID}/campaignBudgets/{budget_id}",
            "id": budget_id,
            "name": f"Budget {budget_id}",
            "amount_micros": micros,
            "explicitly_shared": shared,
            "reference_count": 2 if shared else 1,
            "period": "DAILY",
        }

    @staticmethod
    def _kw(collection: str, parent: str, crit: str, text: str, match: str) -> dict:
        return {"resource_name": f"customers/{CID}/{collection}/{parent}~{crit}", "criterion_id": crit, "text": text, "match_type": match}

    @staticmethod
    def _goal(rn: str, category: str, origin: str, biddable: bool) -> dict:
        return {"resource_name": rn, "category": category, "origin": origin, "biddable": biddable}

    # -- reader ---------------------------------------------------------------

    def customer(self, customer_id):
        return {"id": customer_id, "name": "Test account", "currency_code": self.currency}

    def campaign(self, customer_id, campaign_id):
        return copy.deepcopy(self.campaigns.get(str(campaign_id)))

    def budget(self, customer_id, resource_name):
        return copy.deepcopy(self.budgets.get(resource_name))

    def asset_group(self, customer_id, asset_group_id):
        return copy.deepcopy(self.asset_groups.get(str(asset_group_id)))

    def campaign_negative_keywords(self, customer_id, campaign_id):
        return copy.deepcopy(self.campaign_negatives.get(str(campaign_id), []))

    def shared_set(self, customer_id, shared_set_id):
        return copy.deepcopy(self.shared_sets.get(str(shared_set_id)))

    def shared_set_keywords(self, customer_id, shared_set_id):
        return copy.deepcopy(self.shared_keywords.get(str(shared_set_id), []))

    def shared_set_campaigns(self, customer_id, shared_set_id):
        return list(self.shared_campaigns.get(str(shared_set_id), []))

    def customer_conversion_goals(self, customer_id):
        return copy.deepcopy(self.customer_goals)

    def campaign_conversion_goals(self, customer_id, campaign_id):
        return copy.deepcopy(self.campaign_goals.get(str(campaign_id), []))

    # -- writer ---------------------------------------------------------------

    def mutate(self, customer_id, ops, validate_only):
        self.mutations.append((validate_only, list(ops)))
        if validate_only:
            if self.fail_dry_run:
                raise AdsError(self.fail_dry_run)
            return []
        if self.fail_mutate:
            raise AdsError(self.fail_mutate)
        if self.ignore_writes:
            return []
        names = []
        for op in ops:
            names.append(self._apply(op))
        return names

    def _apply(self, op: OpSpec) -> str:
        if op.resource == "campaign":
            campaign = self.campaigns[op.resource_name.rsplit("/", 1)[1]]
            for path, value in op.fields.items():
                campaign[_CAMPAIGN_FIELDS[path]] = value
            return op.resource_name
        if op.resource == "campaign_budget":
            self.budgets[op.resource_name]["amount_micros"] = op.fields["amount_micros"]
            return op.resource_name
        if op.resource == "asset_group":
            self.asset_groups[op.resource_name.rsplit("/", 1)[1]]["status"] = op.fields["status"]
            return op.resource_name
        if op.resource in ("campaign_criterion", "shared_criterion"):
            store = self.campaign_negatives if op.resource == "campaign_criterion" else self.shared_keywords
            collection = "campaignCriteria" if op.resource == "campaign_criterion" else "sharedCriteria"
            if op.action == "remove":
                parent = op.resource_name.rsplit("/", 1)[1].split("~")[0]
                store[parent] = [k for k in store[parent] if k["resource_name"] != op.resource_name]
                return op.resource_name
            parent = (op.fields.get("campaign") or op.fields.get("shared_set")).rsplit("/", 1)[1]
            self._next_id += 1
            kw = self._kw(collection, parent, str(self._next_id), op.fields["keyword.text"], op.fields["keyword.match_type"])
            store.setdefault(parent, []).append(kw)
            return kw["resource_name"]
        if op.resource in ("customer_conversion_goal", "campaign_conversion_goal"):
            goals = self.customer_goals + [g for gs in self.campaign_goals.values() for g in gs]
            for goal in goals:
                if goal["resource_name"] == op.resource_name:
                    goal["biddable"] = op.fields["biddable"]
            return op.resource_name
        raise AssertionError(f"fake can't apply {op}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    """A minimal platform authenticator: P-256 key, "none" attestation, user verified."""

    def __init__(self, rp_id: str, origin: str, user_verified: bool = True):
        self.rp_id = rp_id
        self.origin = origin
        self.user_verified = user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.user_verified else 0) | (0x40 if attested else 0)

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        numbers = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags(True)])
            + self.sign_count.to_bytes(4, "big")
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose_key
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": b64url(attestation),
            },
        }

    def get(self, options: dict) -> dict:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([self._flags(False)]) + self.sign_count.to_bytes(4, "big")
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(b"gamm-approver"),
            },
        }
