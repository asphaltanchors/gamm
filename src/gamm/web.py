"""The approval pages, served next to the MCP endpoint.

Viewing needs network access only (keep the server on a private network such
as a tailnet). Approving or rejecting needs a passkey assertion for that exact
change, so a bot that can reach the page still can't approve anything.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from importlib import resources

from fastmcp import FastMCP
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from gamm.config import Config
from gamm.db import parse_iso
from gamm.passkeys import PasskeyError, Passkeys
from gamm.service import ChangeError, ChangeService

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
STATIC_FILES = {"gamm.js": "text/javascript", "gamm.css": "text/css"}
MAX_BODY = 64 * 1024


def register_web_routes(mcp: FastMCP, config: Config, service: ChangeService, passkeys: Passkeys) -> None:
    env = Environment(loader=PackageLoader("gamm", "templates"), autoescape=select_autoescape(default=True))

    def local_time(value: str | None) -> str:
        if not value:
            return ""
        return parse_iso(value).astimezone(config.tz).strftime("%a %b %-d, %Y %-I:%M %p %Z")

    env.filters["local"] = local_time

    def page(template: str, status_code: int = 200, **context) -> HTMLResponse:
        html = env.get_template(template).render(config=config, **context)
        return HTMLResponse(html, status_code=status_code, headers=SECURITY_HEADERS)

    def error(message: str, status_code: int = 400) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status_code, headers=SECURITY_HEADERS)

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ValueError("request too large")
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def index(request: Request) -> Response:
        return page(
            "index.html",
            open_changes=service.list(status="open", limit=50),
            recent=service.list(limit=30),
            has_passkeys=passkeys.has_passkeys(),
        )

    @mcp.custom_route("/changes/{change_id:int}", methods=["GET"], include_in_schema=False)
    async def change_page(request: Request) -> Response:
        try:
            change = service.get(request.path_params["change_id"])
        except ChangeError as exc:
            return page("message.html", 404, title="Not found", message=str(exc))
        return page("change.html", change=change, has_passkeys=passkeys.has_passkeys(), now=dt.datetime.now(dt.UTC))

    @mcp.custom_route("/changes/{change_id:int}/options", methods=["POST"], include_in_schema=False)
    async def decision_options(request: Request) -> Response:
        change_id = request.path_params["change_id"]
        try:
            action = (await body(request)).get("action")
            if action not in ("approve", "reject"):
                return error("action must be approve or reject")
            change = service.get(change_id, include_events=False)
            if change["status"] != "proposed":
                return error(f"change #{change_id} is {change['status']}", 409)
            options = passkeys.decision_options(change_id, action, change["content_hash"])
        except (ChangeError, PasskeyError, ValueError) as exc:
            return error(str(exc))
        return Response(options, media_type="application/json", headers=SECURITY_HEADERS)

    @mcp.custom_route("/changes/{change_id:int}/decide", methods=["POST"], include_in_schema=False)
    async def decide(request: Request) -> Response:
        change_id = request.path_params["change_id"]
        try:
            data = await body(request)
            action = data.get("action")
            credential = data.get("credential")
            if action not in ("approve", "reject") or not isinstance(credential, dict):
                return error("expected an action and a passkey response")
            label, content_hash = passkeys.verify_decision(change_id, action, credential)
            change = service.decide(change_id, action, approver=label, content_hash=content_hash)
        except PasskeyError as exc:
            log.warning("passkey check failed for change #%s: %s", change_id, exc)
            return error(str(exc), 403)
        except (ChangeError, ValueError) as exc:
            return error(str(exc), 409)
        return JSONResponse({"status": change["status"]}, headers=SECURITY_HEADERS)

    @mcp.custom_route("/passkeys/register", methods=["GET"], include_in_schema=False)
    async def register_page(request: Request) -> Response:
        token = request.query_params.get("invite", "")
        if not passkeys.invite_is_valid(token):
            return page(
                "message.html",
                400,
                title="Invite not valid",
                message="This invite link is invalid, used or expired. Create a new one on the server with `gamm passkeys invite`.",
            )
        return page("register.html", invite=token)

    @mcp.custom_route("/passkeys/register/options", methods=["POST"], include_in_schema=False)
    async def register_options(request: Request) -> Response:
        try:
            options = passkeys.registration_options((await body(request)).get("invite", ""))
        except (PasskeyError, ValueError) as exc:
            return error(str(exc), 403)
        return Response(options, media_type="application/json", headers=SECURITY_HEADERS)

    @mcp.custom_route("/passkeys/register/verify", methods=["POST"], include_in_schema=False)
    async def register_verify(request: Request) -> Response:
        try:
            data = await body(request)
            if not isinstance(data.get("credential"), dict):
                return error("expected a passkey response")
            label = passkeys.register(data.get("invite", ""), data.get("label", ""), data["credential"])
        except (PasskeyError, ValueError) as exc:
            return error(str(exc), 403)
        log.info("passkey %r registered", label)
        return JSONResponse({"label": label}, headers=SECURITY_HEADERS)

    @mcp.custom_route("/static/{name}", methods=["GET"], include_in_schema=False)
    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in STATIC_FILES:
            return PlainTextResponse("not found", status_code=404)
        content = resources.files("gamm").joinpath("static", name).read_bytes()
        return Response(content, media_type=STATIC_FILES[name], headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")
