# Panel feature ideas

The Operations ideas (11 to 15) are built. See [operations.md](operations.md). The rest are ideas for the Control Center, grouped by theme. None of these exist yet. Each entry says what it does, why it matters,
what it costs and what could go wrong. The order inside a group is the order I would build them.

## Security

1. **Two-factor sign-in (TOTP).** Authenticator app codes after the password, with recovery codes. Closes the largest
   gap in the threat model. Small: one table column, one setup page. Risk: lockout, so admins need a recovery path
   from the command line.
2. **Tamper-evident audit log.** Each row stores a hash of the previous row. A page shows "chain intact" or the first
   broken row. Cheap and easy to demo. Does not stop deletion of the newest rows, so also copy rows to the journal.
3. **Session list and sign-out everywhere.** Show active sessions with address and age, and let users end them.
4. **Signed-update check.** Before the updater merges, require the new `main` commit to carry a valid signature.
   Closes risk 2 in the threat model.
5. **Secret scan on the inventory.** Warn when a plaintext password or private key appears in `inventories/local`.

## Fleet visibility

6. **History and trends.** Keep each collection run as a time series (CPU load, memory, disk, pending updates) and
   draw small charts per device. Needs a retention setting so SQLite stays small.
7. **Alerts.** Rules such as "node offline for 10 minutes", "disk above 85 percent", "compliance dropped" or "drift
   appeared". Send to email or a webhook. Needs a quiet period and a sent-alerts table so one outage is one message.
8. **Pending security updates per node.** Collection reads the package manager's upgrade list. Show counts and the
   packages with security origins. Read-only, so low risk.
9. **Certificate and service expiry.** List certificates found on nodes with days left.
10. **Inventory of listening ports.** Compare open ports with the firewall rules and flag the difference. Good
    security story, and it works from data collection already does for the network tab.

## Operations

11. **Run baseline from the panel with approval. Built.** A dry run, then an admin approves it by typing the node's name.
    The firewall role stays on the command line because it needs a second login.
12. **Maintenance windows. Built.** Per group, weekdays and a start time. They gate baseline apply and rolling reboot, with
    an audited override. They do not move the nodes' own nightly reboot timers, and alerts do not exist yet.
13. **Rolling reboot. Built.** One node at a time, each must report healthy before the next, the first failure stops the run.
14. **Compliance report export. Built.** An on-screen report that prints to PDF from the browser, plus a CSV, with the
    collection time and the baseline commit per node.
15. **Backup and restore of the panel data. Built.** An encrypted archive from the Settings page or the command line, and a
    checked restore from the command line.

## Usability

16. **Global search** across devices, jobs and audit entries.
17. **Saved views** such as "only non-compliant" on the device table.
18. **Dark mode** and a compact table density option.
19. **Keyboard shortcuts** for the job log and the device list.
20. **Health page for the panel itself.** Database size, last collection time, updater state and disk space on the
    controller.

## Integrations

21. **Webhook out and API tokens in.** Read-only tokens with scopes for scripts. Pairs with alerts.
22. **Prometheus endpoint** that exposes compliance and freshness as metrics for Grafana. Standard in real shops.
23. **Single sign-on** by reading the identity Cloudflare Access already provides, and mapping groups to roles.

## Picking the next one

For the internship goal (systems engineering, security, AI), build 1, 2 and 7 first. They are small, they each map to
a named risk or a normal operations need, and they make a good story: "I wrote a threat model, then closed its top
findings." After that, 6 and 10 give the panel a data and security depth that screenshots show well. The AI triage
idea fits on top of 6 and 7 later, because it needs history and alerts to have anything to reason about.
