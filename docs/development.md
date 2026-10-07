# Development

```bash
uv sync                      # gamm and its test dependencies
uv run pytest                # all tests; no Google account needed
```

The tests use an in-memory fake account (`tests/fakes.py`) and a software
passkey that produces real WebAuthn signatures, so the approval flow is tested
end to end without a browser.

The upstream contract test needs Google's server installed locally:

```bash
uv venv --python 3.13 .upstream
VIRTUAL_ENV=.upstream uv pip sync --require-hashes upstream/requirements.txt
uv run pytest tests/test_upstream_contract.py
```

## Running locally against a real account

Passkeys work on `http://localhost`, so you can run the whole thing on your
own computer. Create a settings file outside the repository, for example:

```toml
[server]
public_url = "http://localhost:8080"
database = "./gamm-dev.db"

[google_ads]
credentials_file = "./google-credentials.json"
customer_ids = ["123-456-7890"]

[upstream]
command = "/path/to/gamm/.upstream/bin/google-ads-mcp"
```

Then:

```bash
uv run gamm --config dev.toml keys create dev
uv run gamm --config dev.toml passkeys invite
uv run gamm --config dev.toml serve
```

Proposals only run Google's dry run, so they are safe to try. **`apply_change`
writes to the real account.**

## Layout

| Path | Contents |
| --- | --- |
| `src/gamm/changes.py` | The change kinds: validation, rules, before/after values, writes |
| `src/gamm/service.py` | Propose, approve, apply, cancel, judge |
| `src/gamm/ads.py` | Google Ads reads and the atomic mutate call |
| `src/gamm/passkeys.py` | Passkey registration and approval checks |
| `src/gamm/web.py`, `templates/`, `static/` | The approval pages |
| `src/gamm/tools.py` | gamm's MCP tools and the audit middleware |
| `src/gamm/server.py` | Assembly: auth, tools, Google's server, pages |
| `upstream/` | The pinned Google server |
| `deploy/` | Install script, systemd unit, admin wrapper |

## Change kinds and their limits

v1 kinds change settings in place. The v2 kinds change ads' text and assets;
they link and unlink assets and never delete one.

| Kind | Scope for the one-open-change rule | Limits gamm checks before Google's dry run |
| --- | --- | --- |
| `campaign_status`, `campaign_budget`, `target_roas`, `campaign_negative_keywords`, `campaign_url_suffix` | the campaign | budget cap and ceiling, ROAS floor, keywords per change |
| `asset_group_status` | the asset group's campaign | |
| `shared_negative_keywords` | every campaign using the list (account-level lists: the account) | keywords per change |
| `conversion_goal` | the campaign, or the account for default goals | |
| `asset_group_text` | the asset group's campaign | Lengths: headline 30, long headline 90, description 90, business name 25. Counts after the change: headlines 3–15, long headlines 1–5, descriptions 2–5 with at least one of 60 characters or fewer, exactly 1 business name. No duplicate adds; removals must be linked. Reuses an identical existing TEXT asset. |
| `asset_group_video` | the asset group's campaign | YouTube IDs only (11 characters); at most 5 videos. The title shown comes from the account, or from YouTube's oEmbed for a video new to the account; a video YouTube won't describe is refused. |
| `sitelinks` | the campaign, or the account for `scope: account` | Link text 25, descriptions 35 (both or neither). gamm loads each new `final_url` itself, following redirects only on `allowed_url_hosts`, and refuses anything but HTTP 200. Removes by link text. |
| `callouts` | as `sitelinks` | Text 25. Reuses an identical existing CALLOUT asset. |
| `structured_snippet` | as `sitelinks` | Header from Google's list; 3–10 values of 25 characters. Creates a new snippet asset and unlinks any snippet with that header in the scope. |
| `unlink_assets` | as `sitelinks` | Only PRICE, PROMOTION, SITELINK, CALLOUT and STRUCTURED_SNIPPET links in the scope. The summary describes each asset as read from the account. |

New assets are created in the same request as their links, under temporary
names (`customers/1/assets/-1`); `combined_ops()` renumbers them so several
changes can share one request.

## Adding a change kind

1. Add a model to the `Change` union in `changes.py`.
2. Write its `current()` and `plan()` functions and register them in `KINDS`.
   `plan()` must refuse no-op changes and check the relevant rules; the keys
   of `before` and `after` must match what `current()` returns.
3. If it writes a new resource type, extend `_ENUM_FIELDS` in `ads.py` for any
   enum fields and the fake in `tests/fakes.py`.
4. Add tests, then check it against a real account with a proposal (dry run).
