"""Settings, loaded from a TOML file that lives outside the repository.

Everything specific to one advertiser (account IDs, rules, hostnames) belongs in
that file, never in code. See config.example.toml.
"""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = "/etc/gamm/config.toml"
CONFIG_ENV_VAR = "GAMM_CONFIG"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FreezeWindow:
    start: dt.date
    end: dt.date  # inclusive
    campaign_ids: tuple[str, ...] = ()  # empty means the whole account
    reason: str = ""

    def covers(self, day: dt.date, campaign_ids: set[str], account_wide: bool) -> bool:
        if not (self.start <= day <= self.end):
            return False
        if not self.campaign_ids or account_wide:
            return True
        return bool(set(self.campaign_ids) & campaign_ids)


@dataclass(frozen=True)
class Rules:
    min_target_roas: float | None = None
    max_budget_change_pct: float | None = None
    max_daily_budget: float | None = None
    one_open_change_per: str = "campaign"  # "campaign" or "account"
    max_changes_per_proposal: int = 20
    max_keywords_per_change: int = 100
    allowed_url_hosts: tuple[str, ...] = ()  # hostnames new sitelinks may point to
    freeze: tuple[FreezeWindow, ...] = ()


@dataclass(frozen=True)
class Config:
    public_url: str
    database: Path
    credentials_file: Path
    customer_ids: tuple[str, ...]
    host: str = "127.0.0.1"
    port: int = 8080
    timezone: str = "UTC"
    login_customer_id: str | None = None
    developer_token: str | None = None
    upstream_command: str = "google-ads-mcp"
    upstream_args: tuple[str, ...] = ()
    approver_name: str = "the approver"
    approval_ttl_hours: float = 48
    proposal_ttl_hours: float = 168
    rules: Rules = field(default_factory=Rules)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).hostname or "localhost"

    @property
    def public_origin(self) -> str:
        parsed = urlparse(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def default_customer_id(self) -> str:
        return self.customer_ids[0]


def clean_customer_id(value: str | int) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _optional_number(table: dict, key: str) -> float | None:
    value = table.get(key)
    if value in (None, 0, ""):
        return None
    if not isinstance(value, int | float):
        raise ConfigError(f"rules.{key} must be a number")
    return float(value)


def _parse_rules(table: dict) -> Rules:
    freeze = []
    for item in table.get("freeze", []):
        try:
            start = dt.date.fromisoformat(str(item["start"]))
            end = dt.date.fromisoformat(str(item["end"]))
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"rules.freeze entries need start and end dates: {exc}") from exc
        if end < start:
            raise ConfigError(f"rules.freeze window ends before it starts: {start} to {end}")
        freeze.append(
            FreezeWindow(
                start=start,
                end=end,
                campaign_ids=tuple(clean_customer_id(c) for c in item.get("campaign_ids", [])),
                reason=str(item.get("reason", "")),
            )
        )
    hosts = table.get("allowed_url_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts):
        raise ConfigError('rules.allowed_url_hosts must be a list of hostnames, e.g. ["www.example.com"]')
    per = table.get("one_open_change_per", "campaign")
    if per not in ("campaign", "account"):
        raise ConfigError('rules.one_open_change_per must be "campaign" or "account"')
    return Rules(
        min_target_roas=_optional_number(table, "min_target_roas"),
        max_budget_change_pct=_optional_number(table, "max_budget_change_pct"),
        max_daily_budget=_optional_number(table, "max_daily_budget"),
        one_open_change_per=per,
        max_changes_per_proposal=int(table.get("max_changes_per_proposal", 20)),
        max_keywords_per_change=int(table.get("max_keywords_per_change", 100)),
        allowed_url_hosts=tuple(h.strip().lower() for h in hosts),
        freeze=tuple(freeze),
    )


def parse_config(data: dict, base_dir: Path | None = None) -> Config:
    server = data.get("server", {})
    ads = data.get("google_ads", {})
    upstream = data.get("upstream", {})
    approval = data.get("approval", {})
    base_dir = base_dir or Path.cwd()

    def path(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (base_dir / p)

    try:
        public_url = str(server["public_url"]).rstrip("/")
        database = path(server["database"])
        credentials_file = path(ads["credentials_file"])
        customer_ids = tuple(clean_customer_id(c) for c in ads["customer_ids"])
    except KeyError as exc:
        raise ConfigError(f"missing required setting: {exc.args[0]}") from exc
    if not customer_ids or not all(customer_ids):
        raise ConfigError("google_ads.customer_ids must list at least one account ID")
    parsed = urlparse(public_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("server.public_url must be a full URL, e.g. https://gamm.example.ts.net")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ConfigError("server.public_url must use https (passkeys require it) except on localhost")

    timezone = str(server.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"server.timezone is not a valid time zone: {timezone}") from exc

    login = ads.get("login_customer_id")
    return Config(
        public_url=public_url,
        database=database,
        credentials_file=credentials_file,
        customer_ids=customer_ids,
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8080)),
        timezone=timezone,
        login_customer_id=clean_customer_id(login) if login else None,
        developer_token=ads.get("developer_token") or None,
        upstream_command=str(upstream.get("command", "google-ads-mcp")),
        upstream_args=tuple(str(a) for a in upstream.get("args", [])),
        approver_name=str(approval.get("approver_name", "the approver")),
        approval_ttl_hours=float(approval.get("ttl_hours", 48)),
        proposal_ttl_hours=float(approval.get("proposal_ttl_hours", 168)),
        rules=_parse_rules(data.get("rules", {})),
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from exc
    return parse_config(data, base_dir=path.parent)
