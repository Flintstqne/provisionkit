# ProvisionKit

Ansible provisioning, hardening and service deployment for a small Linux lab (Pi controller + two Ubuntu 24.04 servers).

**Status:** early. Verified on one physical node (`pk-worker`, Ubuntu 24.04.5 x86_64):

- `playbooks/preflight.yml`: read-only platform and disk checks. Passed.
- `playbooks/bootstrap.yml`: creates the `provisionkit` deployment account and key, validated sudoers, and a fresh-session privilege check. Passed, and a second run reported no changes.

- `base_system` and `users` roles via `playbooks/baseline.yml`: packages, hostname, time sync, declared admin accounts. Passed, and repeat runs report no changes.
- `ssh_hardening` role: key-only SSH, no root login, validated config, fresh-login checks, and a timed rollback. Both the rescue path and the timer path (controller killed mid-run) were tested on the worker.

Not done: firewall, Fail2Ban, updates, logging, Docker, monitoring, K3s. The controller is a Raspberry Pi (Debian 12, Python 3.11, so `ansible-core<2.20`). Nothing has been tested on a second server or after a reboot.

```
make check   # yamllint + inventory validator self-test
```

Real addresses/keys/Vault go in `inventories/local/` (git-ignored).
