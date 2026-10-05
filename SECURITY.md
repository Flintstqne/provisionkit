# Security

ProvisionKit is a personal lab project. It manages real machines, so security reports are welcome.

## Report a problem

Use GitHub private vulnerability reporting: the Security tab of this repository, then "Report a vulnerability".
Do not open a public issue for a vulnerability. You will get an answer within a week. This is a one-person project,
so there is no bounty and no guaranteed fix time.

## Supported version

Only the latest commit on `main`. `provisionkit update` moves a controller to it.

## What to read first

- [docs/threat-model.md](docs/threat-model.md) lists what the system protects, who it defends against, what each
  control does and which risks remain.
- [docs/cloudflare-tunnel.md](docs/cloudflare-tunnel.md) covers exposing the panel.
- [docs/panel-update.md](docs/panel-update.md) covers the one root-run component the panel can trigger.

## Secrets

Nothing secret is committed. Real addresses, keys and Vault files live in `inventories/local/`, which is git-ignored.
The panel's session key and database live in `panel/instance/` (mode 0700). CI installs only pinned dependencies and
runs no deployment step.
