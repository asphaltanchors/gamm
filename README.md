# gamm: Google Ads MCP Manager

gamm lets AI agents read Google Ads freely and change it only with a person's
approval. It is an [MCP](https://modelcontextprotocol.io) server you run
yourself. It holds the one Google credential, gives each agent its own key,
logs every call, and applies a change only after the approver confirms it with
a passkey (Face ID, Touch ID or a security key).

```
agents ──(HTTPS, private network, per-agent key)──► gamm
                                                     ├─ every call written to the audit log
                                                     ├─ read tools ──► Google's google-ads-mcp (unmodified, pinned)
                                                     ├─ write tools ─► Google Ads API
                                                     └─ approval page (passkey)
```

## Why

Google's official [google-ads-mcp](https://github.com/googleads/google-ads-mcp)
server is read-only. To manage an account with agents you also need writes,
and you need them to be safe:

- **One credential.** Agents never hold a Google login. Each gets a gamm key
  that can be revoked on its own.
- **Approval that agents can't fake.** An agent that controls your browser or
  shares your network identity still can't produce a passkey assertion with
  user verification. Approval in chat or in a shared document can be faked.
- **Specific write tools, no "change anything" tool.** Each change is checked
  against your rules before you see it.
- **One audit trail.** Every call is logged with which key, which agent, when
  and the arguments, and each change keeps its before and after values.

## How a change works

1. **Propose.** An agent calls `propose_change`. gamm reads the current values,
   checks the rules, runs Google's validate-only dry run, and returns an
   approval link. The approval page shows what will change, worked out by gamm
   from the live account. The agent's own explanation is shown separately and
   labeled as the agent's.
2. **Approve.** You open the link and approve or reject with your passkey. The
   approval covers that exact version of the change.
3. **Apply.** The agent calls `apply_change`. gamm refuses if the approval has
   expired, a freeze window applies, or the account no longer has the values it
   had when the change was proposed. Then it writes everything in one atomic
   request and reads it back.
4. **Log and judge.** Each change comes with a `change_log_row` for your own
   change log, and `record_judgement` stores how it turned out.

## Tools

| Tool | What it does |
| --- | --- |
| `search` | Run a Google Ads Query Language query (Google's tool) |
| `get_resource_metadata` | List the fields a resource supports (Google's tool) |
| `list_accessible_customers` | List the accounts the credential can read (Google's tool) |
| `propose_change` | Propose one or more changes, applied together |
| `apply_change` | Apply an approved change and read it back |
| `get_change`, `list_changes` | Status, history and the change-log row |
| `cancel_change` | Withdraw a change that hasn't been applied |
| `record_judgement` | Record how an applied change turned out |
| `get_rules` | The rules every proposal is checked against |

Google's reference resources (metrics, segments, the API discovery document and
release notes) are passed through too.

Changes a proposal can contain:

| Kind | Change |
| --- | --- |
| `campaign_status` | Pause or enable a campaign |
| `asset_group_status` | Pause or enable a Performance Max asset group |
| `campaign_budget` | Set a campaign's daily budget (unshared budgets only) |
| `target_roas` | Set a campaign's target ROAS |
| `campaign_negative_keywords` | Add or remove a campaign's negative keywords |
| `shared_negative_keywords` | Add or remove keywords in a shared negative keyword list |
| `campaign_url_suffix` | Set a campaign's final URL suffix (UTM tags) |
| `conversion_goal` | Use or stop using a conversion goal for bidding (account default or one campaign) |

## Rules

Set in the settings file. Each is optional.

- `min_target_roas`: target ROAS is never set below this.
- `max_budget_change_pct`: the largest budget move in one change.
- `max_daily_budget`: a ceiling for any campaign's daily budget.
- `one_open_change_per`: `campaign` (default) or `account`. A change touching
  the whole account, such as account-default conversion goals, blocks every
  other open change.
- Freeze windows: date ranges, for the whole account or listed campaigns, in
  which nothing can be proposed or applied.
- Approvals expire (48 hours by default) and so do unapproved proposals (7 days).

Rules are checked when a change is proposed and again when it is applied.

## Running it

gamm is meant to run alone on a small private host reached over a private
network such as [Tailscale](https://tailscale.com). See
[docs/deploy.md](docs/deploy.md) for a Proxmox LXC setup. You need:

- A Google Cloud project with the Google Ads API enabled and an OAuth client.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- An HTTPS address for the approval page (passkeys require it).

Read [docs/security.md](docs/security.md) for what gamm does and doesn't
protect against. [docs/updating.md](docs/updating.md) covers upgrading Google's
server and the Google Ads API version.

## Development

```bash
uv sync
uv run pytest
```

See [docs/development.md](docs/development.md).

## License

Apache 2.0. gamm runs Google's google-ads-mcp (also Apache 2.0) as a separate,
unmodified program.
