# The Update button

An admin sees an **Update** button in the bottom-right corner of every page. A dot on it means GitHub has a newer `main`.
The button does what `provisionkit update` does on the controller (see [cli.md](cli.md)): fast-forward to
`origin/main`, fix the Python environment, restart the panel if needed, roll back if the panel does not come back
healthy, then run the health checks. The dialog shows live progress and reloads the page when it finishes.

## How it works

The panel runs as an unprivileged service in a read-only sandbox. It cannot pull code, install packages or restart
services, and it should not be able to. So it only asks:

```
browser ──click──▶ panel (unprivileged)          creates /var/lib/provisionkit-requests/update   (one empty file)
                                                      │
          systemd path unit ◀─────────────────────────┘  notices the file
                │
                ▼
          provisionkit-update.service (root, oneshot)   removes the request, then runs:
                │                                        provisionkit update --status-dir /var/lib/provisionkit-update
                ▼
          /var/lib/provisionkit-update/status.json + update.log   (root writes, everyone can read)
                ▲
browser ◀─poll──┴── panel reads the status and shows it
```

A second timer (`provisionkit-update-check.timer`, every 3 hours) runs `provisionkit check-updates`, which records how
many commits GitHub is ahead. That is what lights the dot.

## What a click can and cannot do

- It can start **one** `provisionkit update`. That is a fast-forward of the controller's checkout to GitHub's `main`.
  A click cannot choose a branch, a commit, a URL or an option. The request file is empty, and nothing in it is read.
- It does nothing when local files have uncommitted changes, when the checkout is not on `main`, or when history has
  diverged. The dialog shows the refusal.
- It is refused while a panel job (collect, validate) is running, because the update restarts the panel.
- Only admins see the button. The endpoints need an admin session and a CSRF token. Each click is written to the
  audit log, with the Access email when you use the Cloudflare tunnel.

## Trust boundaries, stated plainly

1. **Whoever controls GitHub `main` controls this controller as root.** The updater runs the repository's scripts as
   root, exactly as `sudo provisionkit update` does. Turn on two-factor authentication for your GitHub account, and
   consider branch protection on `main`. The panel cannot make this worse: it can only start the same update.
2. **The `provisionkit` account is root-equivalent on the controller.** The checkout is owned by it, and root runs
   scripts from there. The account already holds the deploy key and passwordless sudo on your servers, so treat access to
   it that way.
3. **Root never writes into a directory the panel controls.** Progress and logs go to `/var/lib/provisionkit-update`,
   owned by root and writable only by root. The panel can read it but cannot plant a symlink for root to follow. The
   only thing root does in the panel's request directory is remove one fixed file name.
4. **The panel treats the status files as untrusted text.** It keeps known keys only, limits lengths, and the page shows
   them with `textContent`, never as HTML.

## Setting it up

New installs get everything from `sudo scripts/install_panel.sh`. An existing install picks it up with
`provisionkit update`: the update notices that the new units are not installed, runs the installer, and restarts the panel.

The installer creates `/var/lib/provisionkit-requests` (the service account, mode 0700) and
`/var/lib/provisionkit-update` (root, mode 0755), and enables `provisionkit-update.path` and
`provisionkit-update-check.timer`. Without them, the dialog says to run the installer.

## When something goes wrong

- **The dialog says "Waiting for the updater to start" for a long time.** Check
  `systemctl status provisionkit-update.path provisionkit-update.service`.
- **"The last update stopped without finishing."** A status that says running but has not changed for 50 minutes is shown
  as stopped. Read `/var/lib/provisionkit-update/update.log`.
- **"Failed: ... rolled back".** The new code did not start cleanly and the previous commit is running again. The log shows
  why.
- The panel is unreachable for a few seconds while it restarts. The dialog keeps retrying and reloads the page when the
  update has finished.

## Status

Tested: the panel module (parsing of every field including garbage and hostile text, request creation with `O_EXCL` and
`O_NOFOLLOW`, a planted symlink, staleness, the log tail), the endpoints (roles, CSRF, audit, refusals while a job runs or
an update is waiting, not installed), the CLI's progress reporting against real git repositories, and the contract
between the two (the real CLI writes, the panel reads). The dialog was also driven in a headless browser against the demo:
button position, the confirmation, the progress steps, the reload, and no console errors.

**Not tested:** the systemd path unit, the root-run update unit, or a real restart of the panel in the middle of an
update. The sandbox has no systemd. The first real click is the real test. Watch it with
`journalctl -u provisionkit-update -f`.
