# Threat model

This document describes what ProvisionKit and its Control Center protect, who they defend against, and what is still
exposed. It describes the code in this repository. Where a control was not tested on real hardware, it says so.

## What is protected

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Deploy key (SSH private key) | Controller, in the `provisionkit` account | Root-equivalent access to every managed node through sudo |
| Inventory and Vault files | `inventories/local/` on the controller | Addresses, account names, any encrypted secrets |
| Panel database and session key | `panel/instance/` | User password hashes, audit log, host snapshots |
| Managed nodes | The lab servers | The things being hardened |
| The repository on GitHub `main` | GitHub | The controller pulls and runs this code |

## Who it defends against

1. An unauthenticated person on the internet who finds the panel's public hostname.
2. A person on the lab network with no account.
3. A logged-in user with fewer rights than they want (viewer or operator reaching admin actions).
4. A compromised managed node trying to attack the controller.
5. A mistake by the owner, such as a bad firewall rule that locks them out.

Out of scope: an attacker with root on the controller, a compromised GitHub account, a malicious owner, and physical
access to the machines.

## Trust boundaries

```
Internet --> Cloudflare Access --> cloudflared (outbound tunnel) --> panel on 127.0.0.1:8080
                                                                       |
                       unprivileged account  ------------------------- +--> ansible --ssh--> managed nodes
                                                                       |
                       one empty request file --> systemd path unit --> root updater (provisionkit update)
```

The panel listens on loopback only. Nothing accepts connections from the internet directly. The tunnel is outbound.

## Threats and controls

### Reaching the panel (actors 1 and 2)

| Threat | Control | Test |
| --- | --- | --- |
| Anyone opens the login page | Cloudflare Access requires an identity first. The panel checks the Access JWT signature, audience and issuer itself, so a request that bypasses Cloudflare is refused | `tests/test_panel_proxy.py` |
| Spoofed client address or Host header | `CF-Connecting-IP` is trusted only from loopback. `PANEL_ALLOWED_HOSTS` rejects other Host values | `tests/test_panel_proxy.py` |
| Password guessing | 12 character minimum, hashed with Werkzeug's default scheme, 5 failures per user and 20 per address in 10 minutes lock sign-in, and a dummy hash is checked for unknown users so timing does not reveal accounts | `tests/test_panel.py` |
| Session theft | Cookies are HttpOnly and SameSite=Lax, Secure when `PANEL_SECURE_COOKIE=1`, and expire after 8 hours | `tests/test_panel.py` |
| Cross-site request forgery | Every state-changing request needs a CSRF token in a header or form field. An empty token never matches | `tests/test_panel.py` |
| Script injection | Strict CSP (`default-src 'self'`, no inline script or style), Jinja autoescaping, text from nodes shown with `textContent`. `X-Frame-Options: DENY`, `nosniff` | `tests/test_panel.py`, hostile text tests in `tests/test_nightly_panel.py` |

### Acting inside the panel (actor 3)

Three roles. Viewers read. Operators also start read-only collection and validation jobs and ping hosts. Admins also
add and remove devices, trust host keys, change settings, manage users and press Update. The server checks the role on
every route. Every state change writes an audit row with user, action, target and address.

Jobs run Ansible with an argument list, never through a shell, with the playbook chosen from a fixed list. Device
names and addresses are validated before they reach the inventory. The inventory editor preserves comments and
masks secret-looking values on display.

### A compromised node (actor 4)

Everything a node reports (collected facts, update status files) is treated as untrusted text. It is parsed with known
keys and bounded lengths and shown with `textContent`. The collection playbook is read-only. Host keys are checked
(`host_key_checking = True`), and a new key is added only after an admin compares the fingerprint the panel shows
with the one on the machine.

### The update button (root)

This is the most privileged path. The panel service has `NoNewPrivileges`, a read-only home and a short list of
writable paths. To update, it creates one empty file in a directory it owns, with `O_EXCL` and `O_NOFOLLOW`. A systemd
path unit sees the file and starts `provisionkit update` as root. Root writes progress only to a root-owned status
directory, which the panel can read but not write, so a panel compromise cannot make root follow a link it planted.
The request is refused while a job or another update runs.

### Owner mistakes (actor 5)

The baseline runs one node at a time, starts with a dry run, asks for confirmation and validates afterward. The SSH
role has a timed rollback if a fresh login fails. The firewall role has one too. `provisionkit update` rolls back the
code if the panel fails to start. Nightly reboots wait for apt and unattended-upgrades and skip the night if they stay
busy.

## Risks that remain

These are known and accepted for now. Each has a possible fix.

1. **Controller compromise is fleet compromise.** The deploy key sits on the controller and opens every node. Fix:
   keep the key in an agent or hardware token, and give nodes narrower sudo rules.
2. **A bad commit on GitHub `main` runs as root on the controller.** `provisionkit update` fast-forwards to `main` and
   the updater runs as root. Fix: require signed commits and verify them before merging, and protect `main` with
   required checks.
3. **A panel compromise can reach the deploy key.** The panel account must read the key to run Ansible. Fix: run
   Ansible in a separate unit or user and let the panel talk to it through a queue.
4. **No second factor in the panel.** Cloudflare Access supplies one if you enable it there, but the panel itself
   accepts a password alone. Fix: add TOTP.
5. **The audit log is not tamper-evident.** It is a SQLite table. An admin or an attacker with the database can edit
   it. Fix: hash-chain the rows and forward them to the node journal or a remote log.
6. **Trust on first use for host keys.** Review of the fingerprint is manual. A careless approval trusts a bad key.
7. **Cloudflare is a dependency.** If the Access policy is misconfigured, the panel's own login is the only barrier.
   That is why the panel verifies the Access token itself.
8. **Not tested on hardware:** the firewall role and the systemd path unit for the updater. The sandbox this was built
   in has no systemd, so both were checked by reading unit files with `systemd-analyze` and simulating the contract.

## Review checklist for each change

- Does a new route check the role on the server?
- Does a new state change write an audit row and require a CSRF token?
- Does new text from a node or a user reach the page only through autoescaping or `textContent`?
- Does any command receive user input? If so, as an argument list, validated first?
- Does a new file written by root live where only root can write?
