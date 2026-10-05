# ProvisionKit

Ansible provisioning, hardening and service deployment for a small Linux lab (Pi controller + two Ubuntu 24.04 servers).

**Status:** early. Verified on one physical node (`pk-worker`, Ubuntu 24.04.5 x86_64):

- `playbooks/preflight.yml`: read-only platform and disk checks. Passed.
- `playbooks/bootstrap.yml`: creates the `provisionkit` deployment account and key, validated sudoers, and a fresh-session privilege check. Passed, and a second run reported no changes.
- `base_system` and `users` roles via `playbooks/baseline.yml`: packages, hostname, time sync, declared admin accounts. Passed, and repeat runs report no changes.
- `ssh_hardening` role: key-only SSH, no root login, validated config, fresh-login checks, and a timed rollback. Both the rescue path and the timer path (controller killed mid-run) were tested on the worker.
- `automatic_updates`, `logging`, `audit` roles and a pending-reboot report: security-only unattended upgrades (dry run verified; timers not yet observed firing), a persistent size-capped journal, auditd rules for identity, sudo, SSH and managed paths, and a controlled audit event test (`tests/verify_audit.yml`). Repeat runs report no changes. After a reboot the journal and services came back.

- `playbooks/validate.yml`: read-only acceptance checks (sshd policy, accounts, file modes, journal, update origins, audit rules, units). Passed on the worker. It also checks the firewall once `provisionkit_firewall_applied` is set.

Written but **not yet applied or tested on hardware**: the `firewall` role (UFW, timed rollback). It is waiting for the worker's wired connection. Its source-restriction cannot be tested with only two allowed sources, so only default-deny is checked.

Not done: Fail2Ban, sysctl profiles, Docker, monitoring, K3s, a second node. The controller is a Raspberry Pi (Debian 12, Python 3.11, so `ansible-core<2.20`). Nothing has been tested on a second server, and the firewall has not run anywhere.

```
make check   # syntax checks, yamllint, inventory validator self-test
```

`panel/` is a small web console for the controller (inventory, device data, compliance, read-only jobs). See [panel/README.md](panel/README.md).

`roles/llm_server` runs a local llama.cpp model server on a node (loopback only, pinned and checksum-verified, sandboxed). Written and partly tested, **not yet run on hardware**. See [docs/llm-server.md](docs/llm-server.md).

Real addresses/keys/Vault go in `inventories/local/` (git-ignored).
