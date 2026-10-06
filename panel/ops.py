"""Changes made from the panel: baseline apply (preview, then approve) and rolling reboot.

Both start through jobs.start(..., mutating_ok=True) and nowhere else. Each one checks, in this order: the node is one the
baseline manages, the confirmation was typed, the maintenance window is open (or an admin gave a reason to override it),
and for the baseline, that the dry run being approved is fresh and still describes the checkout.
"""
import json
import subprocess
import time

from . import jobs, windows
from .config import ROOT
from .db import audit, connect

PREVIEW_MAX_AGE_S = 30 * 60
MIN_REASON = 8
TARGET_GROUP = "workload_nodes"


class OpsError(ValueError):
    pass


def managed_hosts(inv):
    """{name: groups} for the nodes the baseline manages. The controller is never one of them."""
    members = set(inv.groups().get(TARGET_GROUP, []))
    return {n: h["groups"] for n, h in inv.hosts().items() if n in members}


def _head(root=ROOT):
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def _dirty(root=ROOT):
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                       capture_output=True, text=True, timeout=10)
    return [ln[3:] for ln in r.stdout.splitlines()] if r.returncode == 0 else ["(git status failed)"]


def _gate(db, hosts, override):
    """Check the maintenance window. Returns the override reason actually used, or "" when the window was open."""
    ok, blocked = windows.gate(db, hosts)
    if ok:
        return ""
    reason = (override or "").strip()
    if not reason:
        raise OpsError(windows.describe(blocked, windows.get_zone(db)) +
                       " Wait for the window, or an admin can give a reason to override it.")
    if len(reason) < MIN_REASON or len(reason) > 200:
        raise OpsError(f"Give a reason of {MIN_REASON} to 200 characters to override the window.")
    return reason


def start_preview(app, host, user, ip, inv):
    """A dry run of the baseline on one node. Changes nothing, so any operator may start it."""
    if host not in managed_hosts(inv):
        raise OpsError(f"'{host}' is not in {TARGET_GROUP}, so the baseline does not manage it.")
    return jobs.start(app, "baseline_check", host, user, ip, inv, meta={"commit": _head(app.config["ROOT"])},
                      mutating_ok=True)


def preview_state(db, row, now=None):
    """Why a dry run can or cannot be approved: (ok, message)."""
    now = now or time.time()
    meta = json.loads(row["meta"] or "{}")
    if row["kind"] != "baseline_check":
        return False, "Only a baseline preview can be approved."
    if row["status"] != "success":
        return False, "The preview did not finish successfully."
    if meta.get("applied_by"):
        return False, f"Already applied by job #{meta['applied_by']}."
    if now - (row["finished"] or row["created"]) > PREVIEW_MAX_AGE_S:
        return False, "This preview is older than 30 minutes. Run a new one."
    return True, ""


def approve(app, source_id, confirm, override, user, ip, inv):
    """Apply the baseline to the node a successful, fresh dry run was for. Admin only (the view enforces it)."""
    db = connect(app.config["DB_PATH"])
    try:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise OpsError("No such job.")
        ok, why = preview_state(db, row)
        if not ok:
            raise OpsError(why)
        host = row["target"]
        hosts = managed_hosts(inv)
        if host not in hosts:
            raise OpsError(f"'{host}' is no longer managed by the baseline.")
        if (confirm or "").strip() != host:
            raise OpsError(f"Type the node's name ({host}) to approve.")
        root = app.config["ROOT"]
        meta = json.loads(row["meta"] or "{}")
        head = _head(root)
        if not head or meta.get("commit") != head:
            raise OpsError("The configuration changed after this preview ran (update or new commit). Run a new preview.")
        dirty = _dirty(root)
        if dirty:
            raise OpsError("The controller checkout has uncommitted changes (" + ", ".join(dirty[:3]) +
                           "). The baseline records the commit on the node, and validation rejects a dirty one.")
        reason = _gate(db, {host: hosts[host]}, override)
        job_id = jobs.start(app, "baseline_apply", host, user, ip, inv, mutating_ok=True,
                            meta={"source_job": source_id, "commit": head, "override": reason})
        meta["applied_by"] = job_id
        db.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(meta), source_id))
        db.commit()
        audit(db, user, "baseline.approve", host, f"preview #{source_id} -> job #{job_id}" +
              (f"; window override: {reason}" if reason else ""), ip)
        return job_id
    finally:
        db.close()


def reboot_targets(inv, target):
    """Managed nodes a rolling reboot of `target` (a node, a group or all) would touch."""
    managed = managed_hosts(inv)
    if target == "all":
        return dict(managed)
    if target in managed:
        return {target: managed[target]}
    members = set(inv.groups().get(target, []))
    return {n: g for n, g in managed.items() if n in members}


def start_reboot(app, target, confirm, override, user, ip, inv):
    hosts = reboot_targets(inv, target)
    if not hosts:
        raise OpsError("Nothing to reboot there. The controller is never rebooted from the panel, and only nodes in "
                       f"{TARGET_GROUP} are.")
    if (confirm or "").strip() != f"REBOOT {target}":
        raise OpsError(f"Type REBOOT {target} to confirm.")
    db = connect(app.config["DB_PATH"])
    try:
        reason = _gate(db, hosts, override)
        job_id = jobs.start(app, "rolling_reboot", target, user, ip, inv, mutating_ok=True,
                            meta={"hosts": sorted(hosts), "override": reason})
        audit(db, user, "reboot.start", target, f"{len(hosts)} node(s), one at a time" +
              (f"; window override: {reason}" if reason else ""), ip)
        return job_id
    finally:
        db.close()
