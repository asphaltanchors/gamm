# Security model

## What gamm protects

- **The Google credential.** Only the gamm server holds it. Agents authenticate
  to gamm with their own keys (`gamm keys create <name>`), stored only as
  hashes. Revoke a key without touching Google.
- **Changes.** Agents can propose. Only a passkey assertion with user
  verification approves, and it is bound to one change, one action
  (approve or reject) and the exact content shown. Challenges are single-use
  and expire after five minutes.
- **What the approver sees.** The "What will change" list is written by gamm
  from the live account, not by the agent. The agent's explanation is
  displayed separately, escaped, and labeled as the agent's. The pages send a
  strict Content Security Policy and allow no inline scripts.
- **Drift.** A change is applied only if the account still has the values it
  had when the change was proposed, and only while the approval is fresh. The
  rules are checked again at that point.
- **Audit.** Every tool call is logged with key, self-reported agent name,
  client, arguments, outcome and duration. A call that can't be logged isn't
  run. Each change keeps a full event history and its before, after and
  read-back values.

## What it doesn't protect

- **Root on the server.** Whoever has a shell on the gamm host can read the
  credential, edit the database or add a passkey invite. Give no agent shell
  access to it.
- **Other copies of the credential.** If an agent can reach another Google
  credential with the Google Ads scope (for example `gcloud` application
  default credentials on a laptop where agents run), it can bypass gamm. Once
  gamm is in place, remove the Google Ads scope from other credentials so gamm
  is the only path that can write.
- **Changes made in the Google Ads web interface.** gamm doesn't block them.
  Google's change history (`change_event`) shows them; check it regularly.
- **Reading.** Any valid key can read everything the credential can see.
- **Self-reported agent names.** The key is authenticated; the `agent` name
  is whatever the agent says. Give agents separate keys when you need to tell
  them apart reliably.

## Network

Serve gamm on a private network. The recommended setup listens on
`127.0.0.1` and uses Tailscale Serve for HTTPS, with Tailscale access rules
limiting who can reach port 443. The server rejects requests whose Host or
Origin header doesn't match `public_url`.

If you later expose the MCP endpoint publicly (for example through Cloudflare
Access, so a hosted chat client can reach it), expose only `/mcp` and keep the
approval pages (`/`, `/changes/…`, `/passkeys/…`) on the private network.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository rather than a
public issue.
