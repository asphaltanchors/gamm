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

## Adding a change kind

1. Add a model to the `Change` union in `changes.py`.
2. Write its `current()` and `plan()` functions and register them in `KINDS`.
   `plan()` must refuse no-op changes and check the relevant rules; the keys
   of `before` and `after` must match what `current()` returns.
3. If it writes a new resource type, extend `_ENUM_FIELDS` in `ads.py` for any
   enum fields and the fake in `tests/fakes.py`.
4. Add tests, then check it against a real account with a proposal (dry run).
