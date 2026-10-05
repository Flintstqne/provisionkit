# ProvisionKit

Ansible provisioning, hardening and service deployment for a small Linux lab (Pi controller + two Ubuntu 24.04 servers).

**Status:** early. Verified on one physical node (`pk-worker`, Ubuntu 24.04.5 x86_64):

- `playbooks/preflight.yml`: read-only platform and disk checks. Passed.
- `playbooks/bootstrap.yml`: creates the `provisionkit` deployment account and key, validated sudoers, and a fresh-session privilege check. Passed, and a second run reported no changes.

Not done: hardening roles, firewall, Docker, monitoring, K3s. The controller is a Raspberry Pi (Debian 12, Python 3.11, so `ansible-core<2.20`). Nothing has been tested on the second server or after a reboot.

```
make check   # yamllint + inventory validator self-test
```

Real addresses/keys/Vault go in `inventories/local/` (git-ignored).
