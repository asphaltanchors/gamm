"""Assembles the gamm server: auth, audit, gamm's tools, Google's tools and the approval pages."""

from __future__ import annotations

import logging
import os

import uvicorn
from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy

from gamm.ads import AdsBackend, GoogleAdsBackend
from gamm.config import Config
from gamm.db import Database
from gamm.keys import KeyVerifier
from gamm.passkeys import Passkeys
from gamm.service import ChangeService
from gamm.tools import INSTRUCTIONS, AuditMiddleware, register_tools
from gamm.web import register_web_routes

log = logging.getLogger(__name__)

# Google's server names its tools "<namespace>_<tool>"; gamm exposes them under
# the plain names. If an upstream release renames a tool, the contract test fails.
UPSTREAM_TOOL_NAMES = {
    "search_search": "search",
    "metadata_get_resource_metadata": "get_resource_metadata",
    "customers_list_accessible_customers": "list_accessible_customers",
}


def upstream_transport(config: Config) -> StdioTransport:
    """Google's google-ads-mcp, run unmodified as a child process with gamm's credential."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GOOGLE_APPLICATION_CREDENTIALS": str(config.credentials_file),
        "FASTMCP_SHOW_SERVER_BANNER": "false",
    }
    if config.login_customer_id:
        env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = config.login_customer_id
    if config.developer_token:
        env["GOOGLE_ADS_DEVELOPER_TOKEN"] = config.developer_token
    return StdioTransport(command=config.upstream_command, args=list(config.upstream_args), env=env, keep_alive=True)


def build_server(
    config: Config,
    db: Database,
    backend: AdsBackend,
    *,
    upstream: bool = True,
    require_keys: bool = True,
) -> FastMCP:
    service = ChangeService(config, db, backend)
    passkeys = Passkeys(config, db)
    mcp = FastMCP(
        "gamm",
        instructions=INSTRUCTIONS,
        auth=KeyVerifier(db) if require_keys else None,
        middleware=[AuditMiddleware(db)],
    )
    register_tools(mcp, service, config)
    if upstream:
        mcp.mount(create_proxy(upstream_transport(config), name="google-ads-mcp"), tool_names=UPSTREAM_TOOL_NAMES)
    register_web_routes(mcp, config, service, passkeys)
    return mcp


def create_app(config: Config, mcp: FastMCP):
    return mcp.http_app(
        path="/mcp",
        host_origin_protection=True,
        allowed_hosts=sorted({config.public_host, "localhost", "127.0.0.1"}),
        allowed_origins=[config.public_origin],
    )


def serve(config: Config) -> None:
    db = Database(config.database)
    db.migrate()
    mcp = build_server(config, db, GoogleAdsBackend(config))
    log.info("gamm listening on %s:%s (public URL %s)", config.host, config.port, config.public_url)
    # No access log: invite links carry a one-time token in the query string.
    uvicorn.run(create_app(config, mcp), host=config.host, port=config.port, access_log=False, log_level="info")
