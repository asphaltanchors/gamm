from __future__ import annotations

import datetime as dt

import pytest

from gamm.config import parse_config
from gamm.db import Database
from gamm.service import ChangeService

from fakes import CID, FakeAccount


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 5, 17, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += dt.timedelta(**kwargs)


def make_config(tmp_path, **rules):
    data = {
        "server": {"public_url": "http://localhost:8080", "database": str(tmp_path / "gamm.db"), "timezone": "America/Los_Angeles"},
        "google_ads": {"credentials_file": str(tmp_path / "creds.json"), "customer_ids": [CID]},
        "approval": {"approver_name": "Sam", "ttl_hours": 48, "proposal_ttl_hours": 168},
        "rules": {"min_target_roas": 2.0, "max_budget_change_pct": 20, **rules},
    }
    return parse_config(data)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def account():
    return FakeAccount()


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def db(config):
    database = Database(config.database)
    database.migrate()
    return database


@pytest.fixture
def service(config, db, account, clock):
    return ChangeService(config, db, account, clock=clock)
