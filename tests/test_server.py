"""The MCP endpoint over real HTTP: keys, tools and the audit log."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastmcp import Client

from gamm import keys
from gamm.server import build_server, create_app

from conftest import make_config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running(tmp_path, account):
    from gamm.db import Database

    port = free_port()
    config = make_config(tmp_path)
    config = type(config)(**{**config.__dict__, "public_url": f"http://localhost:{port}", "port": port})
    db = Database(config.database)
    db.migrate()
    app = create_app(config, build_server(config, db, account, upstream=False))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://localhost:{port}", db, account
    server.should_exit = True
    thread.join(timeout=5)


def test_mcp_needs_a_key(running):
    url, db, _ = running
    response = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          headers={"accept": "application/json, text/event-stream"})
    assert response.status_code == 401
    bad = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"accept": "application/json, text/event-stream", "authorization": "Bearer gamm_nope"})
    assert bad.status_code == 401


def test_revoked_key_stops_working(running):
    url, db, _ = running
    key = keys.create_key(db, "bot-a")
    keys.revoke_key(db, "bot-a")

    async def go():
        async with Client(f"{url}/mcp", auth=key) as client:
            await client.list_tools()

    with pytest.raises(Exception):
        asyncio.run(go())


def test_propose_over_mcp_is_audited(running):
    url, db, account = running
    key = keys.create_key(db, "bot-a")

    async def go():
        async with Client(f"{url}/mcp", auth=key) as client:
            names = {tool.name for tool in await client.list_tools()}
            rules = (await client.call_tool("get_rules", {})).structured_content
            result = await client.call_tool(
                "propose_change",
                {
                    "title": "Raise ROAS",
                    "why": "Above target for 3 weeks",
                    "changes": [{"kind": "target_roas", "campaign_id": "100", "target_roas": 2.2}],
                    "agent": "Alex",
                },
            )
            refused = await client.call_tool(
                "apply_change", {"change_id": 1, "agent": "Alex"}, raise_on_error=False
            )
            return names, rules, result.structured_content, refused

    names, rules, proposed, refused = asyncio.run(go())
    assert {"propose_change", "apply_change", "get_change", "list_changes", "cancel_change", "record_judgement", "get_rules"} <= names
    assert rules["min_target_roas"] == 2.0 and rules["max_budget_change_pct"] == 20
    assert proposed["status"] == "proposed" and proposed["proposed_by"] == "Alex (key: bot-a)"
    assert refused.is_error and "waiting for approval" in refused.content[0].text

    with db.connect() as conn:
        rows = conn.execute("SELECT key_name, agent, tool, ok, summary FROM audit_log ORDER BY id").fetchall()
    calls = [(r["key_name"], r["agent"], r["tool"], r["ok"]) for r in rows]
    assert ("bot-a", None, "get_rules", 1) in calls
    assert ("bot-a", "Alex", "propose_change", 1) in calls
    assert ("bot-a", "Alex", "apply_change", 0) in calls
    assert any(r["summary"] == "change #1 proposed" for r in rows)
