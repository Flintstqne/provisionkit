# Reaching the panel through a Cloudflare tunnel

The panel keeps listening on `127.0.0.1:8080` on pk-control. `cloudflared` on the same machine makes an outbound
connection to Cloudflare and forwards one hostname to that loopback port. You open no inbound ports and the Pi has no
public address.

This machine holds the deploy key and can run Ansible against your servers, so treat internet access to the panel
like internet access to SSH on the controller. Three layers protect it:

1. **Cloudflare Access** decides who may reach the hostname at all (single sign-on, ideally with MFA at the identity
   provider).
2. **The panel verifies the Access token itself** (`PANEL_CF_ACCESS_*`). A request without a valid token for your
   application is refused with 403, even if the tunnel were misconfigured or something on the LAN reached port 8080.
3. **The panel's own login** with roles, lockout and an audit log.

## Setup

Do the first two steps before any tunnel work: confirm the panel runs on pk-control and you can sign in over an SSH
tunnel (`ssh -L 8080:127.0.0.1:8080 pk-control`, then open `http://127.0.0.1:8080`).

1. **Run the panel as a service** with `sudo scripts/install_panel.sh`. It loads `/etc/provisionkit-panel.env`
   (values to set: `panel/deploy/provisionkit-panel.env.example`).
2. **Install cloudflared** on pk-control (Debian 12, arm64). Follow Cloudflare's current install instructions for
   Debian packages.
3. **Create the tunnel** (interactive, needs your Cloudflare account and a domain in it):
   ```
   cloudflared tunnel login
   cloudflared tunnel create provisionkit-panel
   cloudflared tunnel route dns provisionkit-panel panel.example.org
   ```
   Copy the credentials JSON the create command prints into `/etc/cloudflared/` (mode 0600) and write
   `/etc/cloudflared/config.yml` from `panel/deploy/cloudflared-config.yml.example`.
4. **Create the Access application** in the Cloudflare Zero Trust dashboard: a self-hosted application for
   `panel.example.org`, with an Allow policy for your own identity only. Use an identity provider that enforces MFA.
   Email one-time PINs prove control of a mailbox, which is a single factor. Keep the session duration short. Note the
   team name (the part before `.cloudflareaccess.com`) and the application's Audience (AUD) tag.
5. **Fill in `/etc/provisionkit-panel.env`** with the hostname, team name and AUD tag, then restart the panel.
6. **Start the tunnel:** `sudo cloudflared service install`, then `sudo systemctl enable --now cloudflared`.

## What each setting does

| Variable | Purpose |
|---|---|
| `PANEL_SECURE_COOKIE=1` | Session cookie only over HTTPS, and sends an HSTS header. |
| `PANEL_ALLOWED_HOSTS` | Rejects requests whose Host header is not your hostname. |
| `PANEL_TRUST_CF_IP=1` | Uses `CF-Connecting-IP` as the client address, only when the TCP peer is loopback. Without it every visitor looks like 127.0.0.1, so the lockout counts everyone together and the audit log shows nothing useful. |
| `PANEL_CF_ACCESS_TEAM` and `PANEL_CF_ACCESS_AUD` | Turns on token verification (RS256 only, issuer, audience and expiry checked, fails closed). Set both or neither. The panel refuses to start with one. `/healthz` stays open for local checks. |

The Access email is added to the audit log entries for sign-ins and changes.

## Advice

- Use a viewer or operator account for daily remote use. Keep the admin account for work on the LAN.
- Add a Cloudflare rate limit rule on `/login` as well. The panel's lockout is per user and per address.
- The panel's token check needs outbound HTTPS from pk-control to `<team>.cloudflareaccess.com` to fetch the signing
  keys. If that fails the panel returns 403 to everyone, which is the safe direction.

## Status

Tested: 18 pytest cases using locally generated RSA keys and no network. They cover valid tokens, wrong signer, wrong
audience, wrong issuer, expiry, a missing expiry, algorithm confusion, key fetch failure, forged `CF-Connecting-IP`
from a LAN peer, per-client lockout, HSTS and the Host check. A bug found on the way is fixed: a rejected Host header
used to crash the error page.

**Not tested:** a real Cloudflare account, the real signing-key endpoint, cloudflared itself, or the dashboard steps.
The command names and config keys come from Cloudflare's documented workflow, but check them against the current
documentation. Cloudflare's dashboard wording changes.
