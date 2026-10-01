"""Google's MCP server still offers the tools gamm exposes, with the same inputs.

Runs the pinned upstream server (no Google credentials needed to list tools).
After bumping upstream/requirements.in, run with GAMM_UPDATE_CONTRACT=1 to
accept a reviewed change.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from gamm.server import UPSTREAM_TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).with_name("upstream_contract.json")
COMMAND = os.environ.get("GAMM_UPSTREAM_COMMAND", str(ROOT / ".upstream" / "bin" / "google-ads-mcp"))


async def _contract() -> dict:
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "FASTMCP_SHOW_SERVER_BANNER": "false"}
    async with Client(StdioTransport(command=COMMAND, args=[], env=env)) as client:
        tools = {t.name: t.input_schema for t in await client.list_tools()}
        resources = sorted(str(r.uri) for r in await client.list_resources())
    return {"tools": dict(sorted(tools.items())), "resources": resources}


@pytest.mark.skipif(not Path(COMMAND).exists(), reason="upstream server not installed (see docs/development.md)")
def test_upstream_contract():
    current = asyncio.run(_contract())
    if os.environ.get("GAMM_UPDATE_CONTRACT"):
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    assert set(UPSTREAM_TOOL_NAMES) <= set(current["tools"]), "a tool gamm exposes is gone or renamed"
    assert current == expected, "Google's MCP server changed its tools or resources; review, then accept with GAMM_UPDATE_CONTRACT=1"
