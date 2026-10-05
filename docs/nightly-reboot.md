# Nightly reboots

Rebooting every night clears pending kernel updates, restarts long-running services and shows a broken boot while you
are asleep. It is **off until you opt in**, because it takes machines down.

## What "12am local time" means

Every machine is set to America/New_York (Eastern time) by the baseline, and `provisionkit update` does the same on the
controller. To fix machines that are already provisioned, run `provisionkit baseline --tags timezone`.

Each machine reboots at 00:00 **in its own time zone**. Ubuntu servers often default to UTC, which would mean
the evening in the United States. Check with `timedatectl` and make it explicit with `provisionkit_timezone`
(below). The panel shows every node's time zone and its next reboot on the device page.

## Managed nodes (workload_nodes)

In `inventories/local/group_vars/all.yml`, or per group or host:

```yaml
provisionkit_nightly_reboot: true
provisionkit_timezone: America/New_York        # already the default; set it only to use another zone
# provisionkit_nightly_reboot_time: "00:00"    # default
# provisionkit_nightly_reboot_max_wait_minutes: 30
# provisionkit_nightly_reboot_only_if_required: false
```

Apply it to one node first:

```
provisionkit baseline pk-worker --tags nightly_reboot --check-only
provisionkit baseline pk-worker --tags nightly_reboot
```

The `nightly_reboot` role installs `pk-nightly-reboot.timer` and a small script. Set the option to `false` and run the
same command to remove it. `validate.yml` checks that the timer is enabled and that the time zone matches the declared
one.

## The controller (pk-control)

The controller is not part of the baseline, so it has its own command:

```
sudo provisionkit nightly-reboot enable --timezone America/New_York
provisionkit nightly-reboot status
sudo provisionkit nightly-reboot disable
```

The controller's reboot also waits for a running `provisionkit update` and for running Ansible jobs.

## How the reboot behaves

- **Local time, not persistent.** The timer uses the machine's local time. A machine that was off at midnight does not
  reboot again when it comes back, because that would reboot it right after boot.
- **Never mid-upgrade.** The script waits up to 30 minutes (configurable) for `apt`, `dpkg` and unattended-upgrades to
  finish. If they are still running, it skips that night and the unit fails, so `systemctl --failed` shows it.
- **Everything at once.** All nodes reboot at the same minute. If one depends on another, give them different times.
- **The panel.** A node that is rebooting looks offline if a scheduled collection runs at that moment. It recovers on the
  next successful collection.
- **`provisionkit_automatic_reboot`** (unattended upgrades) reboots only after updates that need it. With a nightly
  reboot you can leave it `false`.

## Status

Tested: the reboot script against stubbed `systemctl`, `pgrep`, `logger` and `sleep` (idle machine, busy package manager,
extra units and processes, `--only-if-required`, bad arguments), the role's templates rendered by real Ansible,
systemd's own calendar parser (00:00 is local midnight in New York, London and UTC), the role run for real with the
systemd steps removed (install, no change on a second run, change of time, removal), the controller command with
temporary directories, and the panel display. 65 tests.

**Not tested:** enabling the timer on a real machine, or an actual reboot. The sandbox has no systemd. Try it on one
node first, and check `systemctl list-timers pk-nightly-reboot.timer` the next day.
