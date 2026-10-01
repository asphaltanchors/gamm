# Updating

## gamm itself

On the server:

```bash
cd /opt/gamm/app
git pull
./deploy/install.sh
```

The install script is idempotent: it syncs dependencies from `uv.lock`,
reinstalls Google's server from `upstream/requirements.txt` and restarts the
service.

## Google's google-ads-mcp server

gamm runs Google's server unmodified, pinned to one release with hashes in
`upstream/requirements.txt`. To move to a new release:

1. Read the release notes at https://github.com/googleads/google-ads-mcp.
2. Change the pin in `upstream/requirements.in`, then regenerate the lock:

   ```bash
   uv pip compile upstream/requirements.in --universal --python-version 3.13 \
     --generate-hashes -o upstream/requirements.txt
   uv venv --allow-existing --python 3.13 .upstream
   VIRTUAL_ENV=.upstream uv pip sync --require-hashes upstream/requirements.txt
   ```

3. Run the contract test, which compares the tools and resources the new
   release offers with `tests/upstream_contract.json`:

   ```bash
   uv run pytest tests/test_upstream_contract.py
   ```

   If it fails, review the difference. If a tool gamm exposes was renamed,
   update `UPSTREAM_TOOL_NAMES` in `src/gamm/server.py`. When the change is
   acceptable, record it with `GAMM_UPDATE_CONTRACT=1 uv run pytest
   tests/test_upstream_contract.py` and commit the updated snapshot.
4. Deploy as above, then run `gamm check` on the server.

## The Google Ads API version

gamm's write tools are written against one API version (`API_VERSION` in
`src/gamm/ads.py`). Google retires each version about a year after release and
announces sunset dates on the Google Ads developer blog. Roughly once a year:

1. Raise the `google-ads` bound in `pyproject.toml` if the version you want
   needs a newer library, and run `uv lock --upgrade-package google-ads`.
2. Change `API_VERSION`.
3. Run the tests, then on the server run `gamm check` and propose a harmless
   change: the proposal runs Google's validate-only dry run, which changes
   nothing. Cancel it afterwards with `cancel_change`.
