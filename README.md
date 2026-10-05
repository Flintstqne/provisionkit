# ProvisionKit

Ansible provisioning, hardening and service deployment for a small Linux lab (Pi controller + two Ubuntu 24.04 servers).

**Status:** scaffold only (plan steps 4–6) plus a read-only preflight (step 8, untested). No roles, no deployments, nothing tested on hardware. See PLAN.md.

```
make check   # yamllint + inventory validator self-test
```

Real addresses/keys/Vault go in `inventories/local/` (git-ignored).
