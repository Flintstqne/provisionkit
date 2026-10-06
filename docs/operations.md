# Operations from the panel

Five features cover changing machines safely and keeping records. Everything here is also in the audit log.

## Baseline from the panel (preview, then approve)

1. On a device in `workload_nodes`, an operator or admin chooses **Preview baseline**. This runs `baseline.yml --check --diff`,
   which changes nothing, and shows the output.
2. An admin reads the output and approves it on the job page by typing the node's name.
3. The panel then runs the baseline, the validation and a collection in one job and stops at the first failure.

Approval is refused when any of these is true:

- the preview did not succeed, is older than 30 minutes, or was already applied
- the controller's commit changed after the preview (an update or a new commit), so the preview no longer describes what would run
- the controller checkout has uncommitted changes (the node would record a dirty commit and validation would reject it)
- the node's maintenance window is closed and no override reason was given

**The firewall role is not applied from the panel.** It asks you to log in from a second device before it disarms its
rollback timer, and the panel has no terminal to answer. Use `provisionkit baseline NODE --tags firewall` for that role.

## Maintenance windows

A window is a group (or all nodes), a set of weekdays, a start time and a length, in the panel's time zone (default
America/New_York, changed on the Maintenance page). Windows can cross midnight. A node with no window that applies to it
is unrestricted.

Windows gate baseline apply and rolling reboot. An admin can override a closed window by typing a reason of 8 to 200
characters, which is stored on the job and in the audit log. Windows do not stop collection or validation, and they do not
move the nightly reboot timers, which run on each node.

## Rolling reboot

On the Maintenance page an admin picks a node, a group or all, then types `REBOOT` and the target. The playbook
`playbooks/reboot_rolling.yml` first checks that every node answers, then reboots one node at a time. Each node must come
back and report `running` from systemd before the next one goes down. The first failure stops the run, so at most one node is
down at any moment. A collection runs at the end to refresh the panel. The controller is never in `workload_nodes`, so it is
never rebooted from here.

## Compliance report

**Compliance, Report** shows every check on every collected device with the collection time and the baseline commit the node
reports. **Print or save as PDF** uses the browser's print dialog, and **Download CSV** gives the same rows. Text that starts with
`=`, `+`, `-` or `@` is prefixed with a quote so spreadsheets do not run it.

## Backup and restore

Settings, Backup downloads an encrypted archive of the panel database (users, snapshots, jobs, audit log, settings,
maintenance windows) and the files of `inventories/local`. It leaves out SSH keys, the session secret, job logs and code.

The archive uses scrypt to derive a key from your passphrase and Fernet (AES with HMAC) to encrypt and authenticate. The
passphrase must be 16 characters or longer and is never stored. Lose it and the backup is unreadable.

From a terminal on the controller:

```
provisionkit backup ~/provisionkit.pkbackup
sudo provisionkit restore ~/provisionkit.pkbackup
```

Restore decrypts and checks the whole archive first (checksums, file names, database integrity) and changes nothing if
anything is wrong. It then stops the panel, keeps the current database and inventory next to the originals with a
`.before-restore-DATE` suffix, installs the backup, deletes the session secret so everyone signs in again, and starts the
panel. Restoring is a terminal command on purpose: the panel never replaces its own database while running.
