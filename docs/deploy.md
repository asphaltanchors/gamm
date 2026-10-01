# Deploying on a Proxmox LXC with Tailscale

gamm should run alone on a small host that only you can log into. This guide
uses an unprivileged Debian 13 container on Proxmox, reached over Tailscale.
Any Debian or Ubuntu machine works the same way from step 2.

## 1. Create the container (on the Proxmox host)

```bash
pveam update
pveam available --section system | grep debian-13
pveam download local debian-13-standard_13.1-2_amd64.tar.zst   # use the current name

pct create 120 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname gamm --cores 1 --memory 1024 --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1 --onboot 1

# Tailscale needs the TUN device inside the container.
cat >> /etc/pve/lxc/120.conf <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

pct start 120
pct enter 120
```

`nesting=1` lets systemd's sandboxing options in `gamm.service` work inside the
container.

## 2. Tailscale

Inside the container:

```bash
apt update && apt full-upgrade -y && apt install -y git curl ca-certificates
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname gamm
```

In the Tailscale admin console, turn on MagicDNS and HTTPS certificates. The
server's address will be `https://gamm.<your-tailnet>.ts.net`.

Limit who can reach it with your access rules. For example, with a tag on the
server and one on your agent hosts:

```json
{
  "tagOwners": { "tag:gamm": ["autogroup:admin"], "tag:agents": ["autogroup:admin"] },
  "grants": [
    { "src": ["autogroup:admin"], "dst": ["tag:gamm"], "ip": ["443"] },
    { "src": ["tag:agents"], "dst": ["tag:gamm"], "ip": ["443"] }
  ]
}
```

Don't give agents SSH access to this host: whoever has a shell on it holds the
Google credential.

## 3. Install gamm

```bash
git clone https://github.com/asphaltanchors/gamm /opt/gamm/app
/opt/gamm/app/deploy/install.sh
```

This creates a `gamm` system user, installs gamm and Google's pinned server
under `/opt/gamm`, creates `/etc/gamm/config.toml` from the example, and
installs the `gamm` systemd service and the `gamm` admin command.

## 4. The Google credential

gamm needs one credential with the Google Ads scope
(`https://www.googleapis.com/auth/adwords`). An OAuth user credential is
simplest: changes then show in Google's change history as that user, made
through the API.

1. In your Google Cloud project, enable the Google Ads API. Under
   **APIs & Services → Credentials**, create an OAuth client ID of type
   **Desktop app** and download its JSON.
2. On the **OAuth consent screen**, use **Internal** (Google Workspace) or
   publish the app to **In production**. In **Testing**, refresh tokens expire
   after 7 days and gamm will stop working.
3. On a computer with a browser, sign in as the Google Ads user:

   ```bash
   uvx --from git+https://github.com/asphaltanchors/gamm gamm google-login \
     --client-secrets client_secret.json --out google-credentials.json
   ```

   The token it saves is limited to the Google Ads scope. Older Google Cloud
   projects may also need a developer token; set `developer_token` in the
   settings if Google asks for one.
4. Copy it to the server and lock it down, then delete your local copy:

   ```bash
   scp google-credentials.json root@gamm:/etc/gamm/google-credentials.json
   ssh root@gamm 'chown root:gamm /etc/gamm/google-credentials.json && chmod 640 /etc/gamm/google-credentials.json'
   rm google-credentials.json
   ```

## 5. Settings

Edit `/etc/gamm/config.toml`: set `public_url` to the Tailscale address, your
account IDs, the approver's name and your rules. Then:

```bash
gamm check                 # settings, Google access, Google's server, keys, passkeys
systemctl restart gamm
tailscale serve --bg 8080  # HTTPS on the tailnet → 127.0.0.1:8080
journalctl -u gamm -f
```

## 6. Your passkey

```bash
gamm passkeys invite
```

Open the printed link on the device you'll approve from and follow the Face ID
or Touch ID prompt. Passkeys saved to iCloud Keychain or Google Password
Manager sync to your other devices. Otherwise create another invite for each
device. Passkeys are tied to the `public_url` hostname, so set that first.

## 7. Agent keys

```bash
gamm keys create claude-code
gamm keys create assistant-bot
```

Each key is shown once. Give each agent the MCP URL
`https://gamm.<your-tailnet>.ts.net/mcp` and the header
`Authorization: Bearer <key>`. Agents that share a key can send their own name
in the `agent` argument of gamm's tools or an `X-Gamm-Agent` header; it is
logged, but not authenticated.

For Claude Code:

```bash
claude mcp add --transport http --scope user gamm https://gamm.<your-tailnet>.ts.net/mcp \
  --header "Authorization: Bearer <key>"
```

## Operating

- `gamm changes --status open` lists open changes; the web page at
  `public_url` shows the same.
- `gamm keys revoke <name>` cuts off one agent immediately.
- `gamm passkeys list` / `gamm passkeys remove <id>` manage approver passkeys.
- Back up `/var/lib/gamm/gamm.db` with your container backups, or with
  `sqlite3 /var/lib/gamm/gamm.db ".backup /root/gamm-backup.db"`.
- If the service fails with `226/NAMESPACE`, the container lacks nesting:
  `pct set 120 --features nesting=1` on the Proxmox host and restart it.
