"""The `gamm` command, run on the server."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from gamm.config import ConfigError, load_config
from gamm.db import Database


def _setup(args):
    config = load_config(args.config)
    db = Database(config.database)
    db.migrate()
    return config, db


def cmd_serve(args) -> None:
    from gamm.server import serve

    config, _ = _setup(args)
    serve(config)


def cmd_keys(args) -> None:
    from gamm import keys

    _, db = _setup(args)
    if args.action == "create":
        key = keys.create_key(db, args.name)
        print(f"Key for {args.name!r} (shown once; store it in the bot's MCP settings):\n\n  {key}\n")
        print("Send it as the header:  Authorization: Bearer <key>")
    elif args.action == "revoke":
        print("revoked" if keys.revoke_key(db, args.name) else f"no active key named {args.name!r}")
    else:
        for row in keys.list_keys(db):
            state = f"revoked {row['revoked_at']}" if row["revoked_at"] else "active"
            print(f"{row['name']:<20} {state:<32} created {row['created_at']}  last used {row['last_used_at'] or 'never'}")


def cmd_passkeys(args) -> None:
    from gamm.passkeys import Passkeys

    config, db = _setup(args)
    passkeys = Passkeys(config, db)
    if args.action == "invite":
        url = passkeys.create_invite(args.minutes)
        print(f"Open this link on the device that should hold the passkey (valid {args.minutes} minutes, once):\n\n  {url}\n")
    elif args.action == "remove":
        print("removed" if passkeys.remove(args.id) else f"no active passkey with id {args.id}")
    else:
        rows = passkeys.list()
        if not rows:
            print("no passkeys registered; run `gamm passkeys invite`")
        for row in rows:
            print(f"{row['id']:<4} {row['label']:<30} created {row['created_at']}  last used {row['last_used_at'] or 'never'}")


def cmd_changes(args) -> None:
    from gamm.service import ChangeService

    config, db = _setup(args)

    class NoBackend:  # listing needs no Google access
        def __getattr__(self, name):
            raise RuntimeError("not available from the command line")

    service = ChangeService(config, db, NoBackend())
    for change in service.list(status=args.status, limit=args.limit):
        print(f"#{change['change_id']:<5} {change['status']:<18} {change['title']}")
        for line in change["summary"]:
            print(f"        {line}")


def cmd_google_login(args) -> None:
    """Create the server's Google credential: a refresh token limited to the Google Ads scope.

    Run it on a computer with a browser, then copy the file to the server.
    """
    import json
    import os

    from google_auth_oauthlib.flow import InstalledAppFlow

    from gamm.ads import ADS_SCOPE

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, scopes=[ADS_SCOPE])
    credentials = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    data = {
        "type": "authorized_user",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
    }
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    print(f"Saved {args.out} (keep it secret). Copy it to the server as the credentials_file in config.toml.")


def cmd_check(args) -> None:
    """Check settings, the Google credential, Google's MCP server and the database."""
    from fastmcp import Client

    from gamm.ads import GoogleAdsBackend
    from gamm.keys import list_keys
    from gamm.passkeys import Passkeys
    from gamm.server import UPSTREAM_TOOL_NAMES, upstream_transport

    config, db = _setup(args)
    problems = 0

    def report(ok: bool, message: str) -> None:
        nonlocal problems
        problems += not ok
        print(("ok    " if ok else "FAIL  ") + message)

    report(True, f"settings loaded; public URL {config.public_url}; database {config.database}")
    try:
        backend = GoogleAdsBackend(config)
        for cid in config.customer_ids:
            customer = backend.customer(cid)
            report(True, f"Google Ads account {cid}: {customer['name']} ({customer['currency_code']})")
    except Exception as exc:  # noqa: BLE001
        report(False, f"Google Ads access: {exc}")

    async def upstream_tools() -> list[str]:
        async with Client(upstream_transport(config)) as client:
            return [tool.name for tool in await client.list_tools()]

    try:
        names = asyncio.run(upstream_tools())
        missing = sorted(set(UPSTREAM_TOOL_NAMES) - set(names))
        report(not missing, f"Google's MCP server started; tools {', '.join(names)}" + (f"; MISSING {missing}" if missing else ""))
    except Exception as exc:  # noqa: BLE001
        report(False, f"Google's MCP server ({config.upstream_command}): {exc}")

    active_keys = [k["name"] for k in list_keys(db) if not k["revoked_at"]]
    report(bool(active_keys), f"bot keys: {', '.join(active_keys) or 'none (gamm keys create <name>)'}")
    passkey_count = len(Passkeys(config, db).list())
    report(passkey_count > 0, f"passkeys: {passkey_count}" + ("" if passkey_count else " (gamm passkeys invite)"))
    rules = config.rules
    print(
        f"rules: min target ROAS {rules.min_target_roas or 'none'}, budget cap {rules.max_budget_change_pct or 'none'}%, "
        f"one open change per {rules.one_open_change_per}, {len(rules.freeze)} freeze window(s)"
    )
    sys.exit(1 if problems else 0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gamm", description="Google Ads MCP Manager")
    parser.add_argument("--config", help="settings file (default: $GAMM_CONFIG or /etc/gamm/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the server").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="check settings, credentials and Google's MCP server").set_defaults(func=cmd_check)

    keys = sub.add_parser("keys", help="manage bot API keys")
    keys_sub = keys.add_subparsers(dest="action", required=True)
    keys_sub.add_parser("list")
    keys_sub.add_parser("create").add_argument("name")
    keys_sub.add_parser("revoke").add_argument("name")
    keys.set_defaults(func=cmd_keys)

    passkeys = sub.add_parser("passkeys", help="manage the approver's passkeys")
    passkeys_sub = passkeys.add_subparsers(dest="action", required=True)
    passkeys_sub.add_parser("list")
    passkeys_sub.add_parser("invite").add_argument("--minutes", type=int, default=15)
    passkeys_sub.add_parser("remove").add_argument("id", type=int)
    passkeys.set_defaults(func=cmd_passkeys)

    login = sub.add_parser("google-login", help="create the Google credential file (run where you have a browser)")
    login.add_argument("--client-secrets", required=True, help="OAuth client JSON (Desktop app) from Google Cloud")
    login.add_argument("--out", default="google-credentials.json")
    login.set_defaults(func=cmd_google_login)

    changes = sub.add_parser("changes", help="list changes")
    changes.add_argument("--status", help="'open' or a status such as 'applied'")
    changes.add_argument("--limit", type=int, default=20)
    changes.set_defaults(func=cmd_changes)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        args.func(args)
    except ConfigError as exc:
        print(f"gamm: {exc}", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print(f"gamm: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
