#!/usr/bin/env bash
# Install or update gamm on a Debian or Ubuntu host (for example a Proxmox LXC).
# Run as root from a checkout at /opt/gamm/app:
#
#   git clone https://github.com/asphaltanchors/gamm /opt/gamm/app
#   /opt/gamm/app/deploy/install.sh
#
# Re-run after `git pull` to update. Safe to run repeatedly.
set -euo pipefail

APP=/opt/gamm/app
UPSTREAM=/opt/gamm/upstream
PYTHON=3.13

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
cd "$(dirname "$0")/.."
if [ "$(pwd -P)" != "$APP" ]; then echo "check the repository out at $APP first" >&2; exit 1; fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (https://docs.astral.sh/uv/) to /usr/local/bin"
  apt-get install -y --no-install-recommends curl ca-certificates >/dev/null
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

id gamm >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gamm --shell /usr/sbin/nologin gamm
install -d -o gamm -g gamm -m 700 /var/lib/gamm
install -d -o root -g gamm -m 750 /etc/gamm
if [ ! -f /etc/gamm/config.toml ]; then
  install -o root -g gamm -m 640 config.example.toml /etc/gamm/config.toml
  NEW_CONFIG=1
fi

# Python and caches live under /opt/gamm, owned by root and readable by the service.
export UV_PYTHON_INSTALL_DIR=/opt/gamm/python UV_CACHE_DIR=/opt/gamm/cache
umask 022

echo "Installing gamm"
uv sync --frozen --no-dev --python "$PYTHON"

echo "Installing Google's MCP server (pinned in upstream/requirements.txt)"
uv venv --allow-existing --python "$PYTHON" "$UPSTREAM" >/dev/null
VIRTUAL_ENV="$UPSTREAM" uv pip sync --require-hashes upstream/requirements.txt

install -m 644 deploy/gamm.service /etc/systemd/system/gamm.service
install -m 755 deploy/gamm /usr/local/bin/gamm
systemctl daemon-reload
systemctl enable gamm >/dev/null

if [ "${NEW_CONFIG:-0}" = 1 ] || [ ! -f "$(sed -n 's/^credentials_file *= *"\(.*\)"/\1/p' /etc/gamm/config.toml)" ]; then
  cat <<EOF

Next:
  1. Edit /etc/gamm/config.toml
  2. Put the Google credential where credentials_file says (owner root, group gamm, mode 640)
  3. gamm check
  4. systemctl restart gamm
EOF
else
  systemctl restart gamm
  sleep 2
  systemctl --no-pager --lines=5 status gamm || true
fi
