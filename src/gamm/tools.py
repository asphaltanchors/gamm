"""gamm's own MCP tools, and the middleware that logs every tool call."""

from __future__ import annotations

import time
from typing import Annotated, Any

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import ToolAnnotations
from pydantic import Field

from gamm.changes import Change
from gamm.config import Config
from gamm.db import Database, dumps, iso, utcnow
from gamm.service import ChangeError, ChangeService

INSTRUCTIONS = """\
gamm gives you read access to Google Ads and a controlled way to change it.

Reading: `search` runs a Google Ads Query Language query. Call
`get_resource_metadata` first to find valid fields; don't guess them.
`list_accessible_customers` lists the accounts you can read.

Changing: every change needs a human's approval, which you cannot give.
1. Check `list_changes` (status "open") and `get_rules` first. Only one change
   per campaign can be open at a time, and freeze windows block changes.
2. Call `propose_change` with exactly what to change and why. gamm reads the
   current values, checks the rules, runs Google's dry run and returns an
   approval link. Send the link to the approver and stop.
3. Once `get_change` shows "approved", call `apply_change`. gamm refuses if the
   account changed since the proposal, and reads the result back.
4. Each change includes a `change_log_row`; record it wherever your team keeps
   its change log, and update it when the status changes.
5. On the judge date, call `record_judgement` with what happened.

Pass your own name as `agent` (e.g. "ads-assistant") so the log shows who acted.
"""

AgentArg = Annotated[str | None, Field(description="Your name, e.g. 'ads-assistant'. Self-reported; logged next to your key.")]
ChangeIdArg = Annotated[int, Field(description="The change number, e.g. 12.")]


def caller_key() -> str:
    token = get_access_token()
    return token.client_id if token else "local"


def _header(name: str) -> str | None:
    try:
        return get_http_request().headers.get(name)
    except Exception:  # noqa: BLE001 - no HTTP request in stdio or in-process calls
        return None


def _client_name(context: MiddlewareContext) -> str | None:
    try:
        info = context.fastmcp_context.session.client_params.client_info
        return f"{info.name} {info.version}".strip()
    except Exception:  # noqa: BLE001 - best effort
        return _header("user-agent")


def _summary(result: Any) -> str | None:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if "change_id" in structured:
            return f"change #{structured['change_id']} {structured.get('status', '')}".strip()
        if isinstance(structured.get("result"), list):
            return f"{len(structured['result'])} rows"
    return None


class AuditMiddleware(Middleware):
    """Writes every tool call to the audit log. A call that can't be logged isn't run."""

    def __init__(self, db: Database):
        self.db = db

    def _start(self, key: str, agent: str | None, client: str | None, tool: str, arguments: dict) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (started_at, key_name, agent, client, tool, arguments_json) VALUES (?, ?, ?, ?, ?, ?)",
                (iso(utcnow()), key, agent, client, tool, dumps(arguments)[:20000]),
            )
            return cur.lastrowid

    def _finish(self, row_id: int, ok: bool, error: str | None, duration_ms: int, summary: str | None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE audit_log SET finished_at = ?, ok = ?, error = ?, duration_ms = ?, summary = ? WHERE id = ?",
                (iso(utcnow()), int(ok), error, duration_ms, summary, row_id),
            )

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        arguments = dict(context.message.arguments or {})
        agent = arguments.get("agent") or _header("x-gamm-agent")
        row_id = await anyio.to_thread.run_sync(
            self._start, caller_key(), agent, _client_name(context), context.message.name, arguments
        )
        started = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as exc:
            await anyio.to_thread.run_sync(self._finish, row_id, False, str(exc)[:4000], int((time.monotonic() - started) * 1000), None)
            raise
        await anyio.to_thread.run_sync(self._finish, row_id, True, None, int((time.monotonic() - started) * 1000), _summary(result))
        return result


async def _run(fn, *args, **kwargs):
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except ChangeError as exc:
        raise ToolError(str(exc)) from exc


def register_tools(mcp: FastMCP, service: ChangeService, config: Config) -> None:
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def propose_change(
        title: Annotated[str, Field(description="Short name for the change, e.g. 'Pause the clearance campaign'.")],
        why: Annotated[str, Field(description="Why this change, with the evidence.")],
        changes: Annotated[
            list[Change],
            Field(description="What to change. Several items are applied together, all or nothing.", min_length=1),
        ],
        expected_effect: Annotated[str, Field(description="What you expect to happen.")] = "",
        how_to_check: Annotated[str, Field(description="How to tell whether it worked.")] = "",
        how_to_undo: Annotated[str, Field(description="How to reverse it.")] = "",
        judge_on: Annotated[str, Field(description="When to judge the result, e.g. '2026-10-20' or '3 weeks after'.")] = "",
        customer_id: Annotated[str | None, Field(description="Account ID; defaults to the main account.")] = None,
        agent: AgentArg = None,
    ) -> dict:
        """Propose a Google Ads change for human approval. Nothing changes until it is approved and applied.

        gamm reads the current values, checks the rules (see get_rules), runs Google's
        dry run, and returns the change with an approval_url for the approver.
        """
        return await _run(
            service.propose,
            changes=changes,
            title=title,
            why=why,
            expected_effect=expected_effect,
            how_to_check=how_to_check,
            how_to_undo=how_to_undo,
            judge_on=judge_on,
            customer_id=customer_id,
            key_name=caller_key(),
            agent=agent,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    async def apply_change(change_id: ChangeIdArg, agent: AgentArg = None) -> dict:
        """Apply an approved change to Google Ads, then read it back.

        Refuses unless the change is approved and unexpired, no freeze window applies,
        and the account still has the values it had when the change was proposed.
        """
        return await _run(service.apply, change_id, key_name=caller_key(), agent=agent)

    @mcp.tool(annotations=read_only)
    async def get_change(change_id: ChangeIdArg) -> dict:
        """Get one change: status, what it changes, history, result and its change_log_row."""
        return await _run(service.get, change_id)

    @mcp.tool(annotations=read_only)
    async def list_changes(
        status: Annotated[
            str | None,
            Field(description="'open' for proposed/approved/applying, or a status such as 'applied'. Omit for all."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict]:
        """List recent changes, newest first."""
        return await _run(service.list, status=status, limit=limit)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def cancel_change(
        change_id: ChangeIdArg,
        reason: Annotated[str, Field(description="Why it is being withdrawn.")],
        agent: AgentArg = None,
    ) -> dict:
        """Withdraw a proposed or approved change that hasn't been applied."""
        return await _run(service.cancel, change_id, reason, key_name=caller_key(), agent=agent)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def record_judgement(
        change_id: ChangeIdArg,
        result: Annotated[str, Field(description="What happened, with numbers and dates.")],
        agent: AgentArg = None,
    ) -> dict:
        """Record how an applied change turned out."""
        return await _run(service.judge, change_id, result, key_name=caller_key(), agent=agent)

    @mcp.tool(annotations=read_only)
    async def get_rules() -> dict:
        """The rules every proposal is checked against, and the accounts gamm may change."""
        rules = config.rules
        return {
            "customer_ids": list(config.customer_ids),
            "min_target_roas": rules.min_target_roas,
            "max_budget_change_pct": rules.max_budget_change_pct,
            "max_daily_budget": rules.max_daily_budget,
            "one_open_change_per": rules.one_open_change_per,
            "max_changes_per_proposal": rules.max_changes_per_proposal,
            "max_keywords_per_change": rules.max_keywords_per_change,
            "allowed_url_hosts": list(rules.allowed_url_hosts),
            "freeze_windows": [
                {"start": str(w.start), "end": str(w.end), "campaign_ids": list(w.campaign_ids) or "all", "reason": w.reason}
                for w in rules.freeze
            ],
            "approval_expires_after_hours": config.approval_ttl_hours,
            "proposal_expires_after_hours": config.proposal_ttl_hours,
        }
