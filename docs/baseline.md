# Applying the baseline to a node

`provisionkit baseline NODE` runs `playbooks/baseline.yml` on one node, with checks before and after. It exists because
this is the one operation that changes a server, and the firewall part can lock you out if it is wrong.

```
provisionkit baseline pk-worker --check-only      preview only, changes nothing
provisionkit baseline pk-worker                   dry run, then you type the node's name, then apply, then validate
provisionkit baseline pk-worker --tags firewall   only some roles (tags: base_system, users, ssh_hardening, firewall,
                                                  automatic_updates, logging, audit, baseline_manifest)
```

Options: `--yes` skips the typed confirmation, `--ask-vault-pass` prompts for the Vault password, `--allow-dirty` overrides
the clean checkout rule below.

## What the command checks before it touches anything

| Check | Why |
|---|---|
| `inventories/local` exists and passes the inventory validator | no placeholder addresses |
| the node is in `workload_nodes` | the controller can never be a target |
| the checkout has no uncommitted changes to tracked files | the manifest records the commit, and `validate.yml` rejects a node baselined from a dirty one |
| the controller's address is covered by `provisionkit_management_sources` (only when the firewall is part of the run) | otherwise the firewall would block this controller's SSH |
| tags are lowercase names separated by commas | no shell surprises |

Then it prints the plan, including the exact list of addresses the firewall will allow, runs the dry run
(`--check --diff`), and waits for you to type the node's name. Only that one node is ever targeted.

## Recommended first run on pk-worker

The firewall role has never run on hardware, and the manifest role has never run on a real node. Do them separately,
smallest change first.

Before you start:

1. `provisionkit update`, then `provisionkit check`. Fix anything marked FAIL. A deploy key with a passphrase is a warning
   here and a failure for the panel.
2. Check `provisionkit_management_sources` in `inventories/local/group_vars/all.yml`. It must contain the controller and
   **the address of the machine you will use to confirm the login** (your workstation).
3. Have console or keyboard access to the node in reach. This is the way back in if everything goes wrong.
4. Open a second terminal on your workstation, ready to run `ssh <admin user>@<node address> true`.

Then:

```
provisionkit baseline pk-worker --tags baseline_manifest --check-only
provisionkit baseline pk-worker --tags baseline_manifest
```

This writes one file, `/etc/provisionkit/baseline.json`. After the next collection, the panel's Config column shows
Current for pk-worker. If the dry run pauses and asks you to confirm a login from another device, that is the firewall
role's prompt. It runs even in dry-run mode and nothing has been applied, so press Enter.

Then the firewall:

```
provisionkit baseline pk-worker --tags firewall --check-only
provisionkit baseline pk-worker --tags firewall
```

What happens on the node, from `roles/firewall`:

1. A rollback timer is armed. In 10 minutes it runs `ufw disable`, whatever else happens.
2. SSH is allowed from every management source, then the default policy becomes deny incoming, and ufw is enabled.
3. The controller logs in again with the deploy key as a fresh session. A closed port must time out, not be refused.
4. The play pauses and asks you to log in from your workstation. Do it in the second terminal, then press Enter.
5. The applied sources are recorded and the rollback timer is stopped.

If any step fails, the role rolls the firewall back itself. If you lose the connection, or you never press Enter, the timer
fires within 10 minutes. If neither happens, run `sudo ufw disable` on the node's console.

After a successful firewall run the command prints one line to add to `inventories/local/group_vars/all.yml`:
`provisionkit_firewall_applied: true`. That makes `validate.yml` and the panel's compliance page check the firewall.
Then click Collect data on the device, and the Firewall check should turn Pass.

## Status

The command's orchestration is covered by 25 tests that replace `ansible-playbook` with a stub that records its
arguments: order of the runs, the typed confirmation, tags and Vault passthrough, refusal on a dirty checkout, a
controller missing from the management sources, unknown hosts, failed dry run, failed apply, failed validation.

**Not tested:** a real baseline run. The sandbox cannot apply SSH hardening or a firewall to itself, and the roles were
not changed. The firewall role has not run on any machine yet, so treat the first run as the real test, with console
access in reach.
