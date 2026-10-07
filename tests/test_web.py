from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from gamm.changes import CHANGE_LIST
from gamm.passkeys import Passkeys
from gamm.server import build_server, create_app
from gamm.service import ChangeService

from fakes import SoftAuthenticator

ORIGIN = "http://localhost:8080"


@pytest.fixture
def client(config, db, account):
    app = create_app(config, build_server(config, db, account, upstream=False))
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def proposals(config, db, account, clock):
    return ChangeService(config, db, account)


def propose(proposals, why="Return is above target", **change):
    change = change or {"kind": "target_roas", "campaign_id": "100", "target_roas": 2.2}
    return proposals.propose(
        changes=CHANGE_LIST.validate_python([change]), title="Raise ROAS", why=why, key_name="bot-a", agent="Alex"
    )


def register(client, config, db, authenticator=None, label="MacBook"):
    authenticator = authenticator or SoftAuthenticator("localhost", ORIGIN)
    token = Passkeys(config, db).create_invite().split("invite=")[1]
    assert client.get(f"/passkeys/register?invite={token}").status_code == 200
    options = client.post("/passkeys/register/options", json={"invite": token}).json()
    response = client.post(
        "/passkeys/register/verify", json={"invite": token, "label": label, "credential": authenticator.create(options)}
    )
    assert response.status_code == 200, response.text
    return authenticator, token


def decide(client, change_id, action, authenticator, post_to=None):
    options = client.post(f"/changes/{change_id}/options", json={"action": action})
    assert options.status_code == 200, options.text
    credential = authenticator.get(options.json())
    return client.post(f"/changes/{post_to or change_id}/decide", json={"action": action, "credential": credential})


def test_pages_have_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert "No passkey is registered yet" in response.text


def test_wrong_host_is_refused(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421


def test_register_and_approve(client, config, db, proposals):
    authenticator, token = register(client, config, db)
    # The invite works once.
    assert client.post("/passkeys/register/options", json={"invite": token}).status_code == 403

    propose(proposals)
    page = client.get("/changes/1")
    assert "Set campaign “Main PMax” (100) target ROAS to 220% (now 200%)" in page.text
    assert "Approve with passkey" in page.text

    response = decide(client, 1, "approve", authenticator)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "approved"}
    change = proposals.get(1)
    assert change["status"] == "approved" and change["decided_by"] == "MacBook"


def test_bot_text_is_escaped(client, config, db, proposals):
    propose(proposals, why="<script>alert(1)</script>")
    page = client.get("/changes/1").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_assertion_cannot_be_replayed(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    propose(proposals)
    options = client.post("/changes/1/options", json={"action": "reject"}).json()
    credential = authenticator.get(options)
    assert client.post("/changes/1/decide", json={"action": "reject", "credential": credential}).status_code == 200
    replay = client.post("/changes/1/decide", json={"action": "reject", "credential": credential})
    assert replay.status_code == 403


def test_assertion_is_bound_to_action(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    propose(proposals)
    options = client.post("/changes/1/options", json={"action": "reject"}).json()
    response = client.post("/changes/1/decide", json={"action": "approve", "credential": authenticator.get(options)})
    assert response.status_code == 403
    assert proposals.get(1)["status"] == "proposed"


def test_assertion_is_bound_to_change(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    propose(proposals)
    propose(proposals, kind="campaign_status", campaign_id="200", status="PAUSED")
    response = decide(client, 1, "approve", authenticator, post_to=2)
    assert response.status_code == 403
    assert proposals.get(2)["status"] == "proposed"


def test_user_verification_is_required(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    authenticator.user_verified = False  # e.g. a tap without Face ID / Touch ID
    propose(proposals)
    assert decide(client, 1, "approve", authenticator).status_code == 403
    assert proposals.get(1)["status"] == "proposed"


def test_unregistered_passkey_is_refused(client, config, db, proposals):
    register(client, config, db)
    propose(proposals)
    stranger = SoftAuthenticator("localhost", ORIGIN)
    assert decide(client, 1, "approve", stranger).status_code == 403


def test_wrong_origin_is_refused(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    propose(proposals)
    authenticator.origin = "https://evil.example"
    assert decide(client, 1, "approve", authenticator).status_code == 403


def test_registration_needs_user_verification(client, config, db):
    token = Passkeys(config, db).create_invite().split("invite=")[1]
    options = client.post("/passkeys/register/options", json={"invite": token}).json()
    weak = SoftAuthenticator("localhost", ORIGIN, user_verified=False)
    response = client.post("/passkeys/register/verify", json={"invite": token, "label": "x", "credential": weak.create(options)})
    assert response.status_code == 403
    assert Passkeys(config, db).list() == []


def test_removed_passkey_cannot_approve(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    Passkeys(config, db).remove(1)
    propose(proposals)
    assert client.post("/changes/1/options", json={"action": "approve"}).status_code == 400


def test_decided_change_has_no_options(client, config, db, proposals):
    authenticator, _ = register(client, config, db)
    propose(proposals)
    assert decide(client, 1, "approve", authenticator).status_code == 200
    assert client.post("/changes/1/options", json={"action": "approve"}).status_code == 409


def test_ad_text_is_shown_escaped(client, proposals):
    change = propose(
        proposals, kind="asset_group_text", asset_group_id="500", field_type="HEADLINE", add=["<b>Big</b> & bold"]
    )
    page = client.get(f"/changes/{change['change_id']}").text
    assert "Add headline: “&lt;b&gt;Big&lt;/b&gt; &amp; bold”" in page
    assert "<b>Big</b>" not in page
