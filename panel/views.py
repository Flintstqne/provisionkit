import csv
import io
import json
import socket
import time
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)

from . import auth, backup, drift, fleet, jobs, ops, report, scheduler, setup, updater, windows
from .db import audit, get_db
from .inventory import NO_ADD, InventoryError

bp = Blueprint("main", __name__)
TABS = ("overview", "hardware", "network", "storage", "compliance", "configuration", "activity")


def _inv():
    return current_app.config["INVENTORY"]


def _stale_after():
    """Data counts as stale after the configured time, or after three missed scheduled collections."""
    minutes = scheduler.get_interval(get_db())
    return max(current_app.config["STALE_AFTER_S"], 3 * minutes * 60)


def _devices():
    root = current_app.config["ROOT"]
    return fleet.devices(_inv(), get_db(), _stale_after(), lambda manifest: drift.classify(manifest, root))


def _ssh_port(d):
    """The host's own ansible_port, else the fleet-wide provisionkit_ssh_port, else 22."""
    return int(d["vars"].get("ansible_port") or _inv().raw_vars().get("provisionkit_ssh_port", 22))


def _log(action, target="", detail=""):
    if getattr(g, "access_email", ""):
        detail = f"{detail} [access: {g.access_email}]".strip()
    audit(get_db(), g.user["username"] if g.user else "-", action, target, detail, auth.client_ip())


# ---- session ---------------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        user = auth.attempt_login(request.form.get("username", "").strip(), request.form.get("password", ""))
        if user:
            session.clear()  # new session id on privilege change
            session.permanent = True
            session["uid"] = user["id"]
            return redirect(auth.safe_next(request.args.get("next")) or url_for("main.index"))
        flash("Sign-in failed. Check your credentials or try again later.", "error")
    return render_template("login.html"), 200


@bp.post("/logout")
def logout():
    if g.user:
        _log("logout")
    session.clear()
    return redirect(url_for("main.login"))


# ---- dashboard and devices -------------------------------------------------------------------

@bp.route("/")
@auth.require()
def index():
    devs = _devices()
    db = get_db()
    recent = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 8").fetchall()
    running = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id DESC").fetchall()
    attention = [d for d in devs.values() if d["status"] in ("Offline", "Stale") or d["compliance"] == "Non-compliant"
                 or d["reboot"] or (d["drift"] and d["drift"]["state"] in drift.ATTENTION)]
    return render_template("dashboard.html", s=fleet.summary(devs), recent=recent, running=running,
                           attention=attention, page="dashboard")


@bp.route("/devices")
@auth.require()
def devices():
    devs = list(_devices().values())
    q, group = request.args.get("q", "").strip().lower(), request.args.get("group", "")
    status = request.args.get("status", "")
    shown = [d for d in devs if (not q or q in d["name"] or q in (d["address"] or "") or q in d["os"].lower())
             and (not group or group in d["groups"]) and (not status or d["status"] == status)]
    return render_template("devices.html", devices=shown, total=len(devs), groups=sorted(_inv().groups()),
                           q=q, group=group, status=status, page="devices")


@bp.route("/devices/export.csv")
@auth.require()
def devices_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "address", "groups", "status", "compliance", "os", "kernel", "cpus", "mem_mb", "reboot_required"])
    for d in _devices().values():
        row = [d["name"], d["address"], " ".join(d["groups"]), d["status"], d["compliance"], d["os"], d["kernel"],
               d["cpus"], d["mem_mb"], d["reboot"]]
        w.writerow([f"'{c}" if isinstance(c, str) and c[:1] in "=+-@\t\r" else c for c in row])  # no CSV formula injection
    _log("devices.export")
    return out.getvalue(), 200, {"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=devices.csv"}


@bp.route("/devices/new", methods=["GET", "POST"])
@auth.require("admin")
def device_new():
    groups = [g_ for g_ in _inv().groups() if g_ not in NO_ADD]
    form = {"name": "", "address": "", "groups": ["workload_nodes"]}
    if request.method == "POST":
        form = {"name": request.form.get("name", ""), "address": request.form.get("address", ""),
                "groups": request.form.getlist("groups")}
        try:
            _inv().add_host(form["name"], form["address"], form["groups"])
        except InventoryError as e:
            flash(str(e), "error")
        else:
            name = form["name"].strip().lower()
            _log("device.add", name, f"{form['address']} groups={','.join(form['groups'])}")
            flash(f"Device {name} added to the inventory. Follow the setup steps below.", "ok")
            return redirect(url_for("main.device", name=name, tab="overview"))
    return render_template("device_new.html", groups=groups, form=form, page="devices")


@bp.route("/devices/<name>")
@auth.require()
def device(name):
    d = _devices().get(name) or abort(404)
    tab = request.args.get("tab", "overview")
    if tab not in TABS:
        abort(404)
    db = get_db()
    activity = jobs_for = []
    if tab == "activity":
        jobs_for = db.execute("SELECT j.*, h.failed, h.unreachable, h.message FROM jobs j "
                              "LEFT JOIN job_hosts h ON h.job_id=j.id AND h.host=? "
                              "WHERE j.target IN (?, 'all') OR h.host=? ORDER BY j.id DESC LIMIT 15", (name, name, name)).fetchall()
        activity = db.execute("SELECT * FROM audit WHERE target=? ORDER BY id DESC LIMIT 15", (name,)).fetchall()
    last = db.execute("SELECT h.*, j.finished FROM job_hosts h JOIN jobs j ON j.id=h.job_id "
                      "WHERE h.host=? AND j.kind='validate' ORDER BY h.job_id DESC LIMIT 1", (name,)).fetchone()
    status = db.execute("SELECT * FROM host_status WHERE host=?", (name,)).fetchone()
    checklist = None
    if not d["snapshot"] and d["status"] != fleet.CONTROLLER:
        try:
            trusted = setup.is_trusted(d["address"] or name, _ssh_port(d))
        except setup.SetupError:
            trusted = False
        last_collect = db.execute("SELECT h.* FROM job_hosts h JOIN jobs j ON j.id=h.job_id "
                                  "WHERE h.host=? AND j.kind='collect' ORDER BY h.job_id DESC LIMIT 1", (name,)).fetchone()
        checklist = setup.steps(d, trusted, last_collect, setup.bootstrap_command(
            str(current_app.config["ROOT"]), _inv().dir, name))
    return render_template("device.html", d=d, tab=tab, tabs=TABS, jobs=jobs_for, activity=activity, last_validate=last,
                           host_status=status, checklist=checklist, page="devices")


@bp.post("/devices/<name>/remove")
@auth.require("admin")
def device_remove(name):
    try:
        _inv().remove_host(name)
    except InventoryError as e:
        flash(str(e), "error")
        return redirect(url_for("main.device", name=name))
    db = get_db()
    for t in ("snapshots", "host_status"):
        db.execute(f"DELETE FROM {t} WHERE host=?", (name,))
    db.commit()
    _log("device.remove", name)
    flash(f"Device {name} removed from the inventory. The server itself was not changed.", "ok")
    return redirect(url_for("main.devices"))


@bp.post("/devices/<name>/hostkey")
@auth.require("admin")
def hostkey_review(name):
    """Fetch the server's host key and show its fingerprint. Nothing is trusted until the admin confirms it."""
    d = _devices().get(name) or abort(404)
    port = _ssh_port(d)
    try:
        _line, kind, fp = setup.scan_host_key(d["address"] or name, port)
    except setup.SetupError as e:
        flash(str(e), "error")
        return redirect(url_for("main.device", name=name))
    _log("hostkey.review", name, fp)
    return render_template("hostkey.html", d=d, port=port, kind=kind, fingerprint=fp, page="devices")


@bp.post("/devices/<name>/hostkey/trust")
@auth.require("admin")
def hostkey_trust(name):
    d = _devices().get(name) or abort(404)
    if request.form.get("verified") != "yes":
        flash("Confirm that you checked the fingerprint on the server.", "error")
        return redirect(url_for("main.device", name=name))
    try:
        added = setup.trust_host_key(d["address"] or name, _ssh_port(d), request.form.get("fingerprint", ""))
    except setup.SetupError as e:
        flash(str(e), "error")
        return redirect(url_for("main.device", name=name))
    _log("hostkey.trust", name, request.form.get("fingerprint", "") + ("" if added else " (already trusted)"))
    if request.form.get("collect") == "yes":
        try:
            job_id = jobs.start(current_app._get_current_object(), "collect", name, g.user["username"],
                                auth.client_ip(), _inv())
        except jobs.JobError as e:
            flash(f"Host key trusted. Could not start collection: {e}", "error")
            return redirect(url_for("main.device", name=name))
        return redirect(url_for("main.job", job_id=job_id))
    flash("Host key trusted.", "ok")
    return redirect(url_for("main.device", name=name))


@bp.post("/devices/<name>/ping")
@auth.require("operator")
def device_ping(name):
    """TCP connect to the SSH port from the controller. No credentials involved."""
    d = _devices().get(name) or abort(404)
    port = _ssh_port(d)
    t0 = time.time()
    try:
        with socket.create_connection((d["address"] or name, port), timeout=3):
            ok, msg = True, f"{d['address']}:{port} accepted a TCP connection in {int((time.time() - t0) * 1000)} ms."
    except OSError as e:
        ok, msg = False, f"{d['address']}:{port} is not reachable ({e.__class__.__name__})."
    _log("device.ping", name, "reachable" if ok else "unreachable")
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("main.device", name=name))


# ---- groups, compliance ----------------------------------------------------------------------

@bp.route("/groups")
@auth.require()
def groups():
    devs = _devices()
    return render_template("groups.html", groups=_inv().groups(), devs=devs, page="groups")


@bp.route("/compliance")
@auth.require()
def compliance():
    devs = [d for d in _devices().values() if d["snapshot"]]
    titles = []
    for d in devs:
        for c in d["checks"]:
            if c["title"] not in [t for _, t in titles]:
                titles.append((c["id"], c["title"]))
    pending = [d for d in _devices().values() if not d["snapshot"] and d["status"] != fleet.CONTROLLER]
    return render_template("compliance.html", devs=devs, titles=titles, pending=pending,
                           s=fleet.summary({d["name"]: d for d in devs}), page="compliance")


# ---- jobs ------------------------------------------------------------------------------------

@bp.route("/jobs")
@auth.require()
def jobs_list():
    rows = get_db().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    targets = ["all"] + sorted(_inv().groups()) + sorted(_inv().hosts())
    readonly = {k: v for k, v in jobs.KINDS.items() if not v.get("mutating")}
    return render_template("jobs.html", jobs=rows, kinds=jobs.KINDS, runnable=readonly, targets=targets, page="jobs")


@bp.post("/jobs")
@auth.require("operator")
def job_start():
    try:
        job_id = jobs.start(current_app._get_current_object(), request.form.get("kind", ""), request.form.get("target", ""),
                            g.user["username"], auth.client_ip(), _inv())
    except jobs.JobError as e:
        flash(str(e), "error")
        return redirect(url_for("main.jobs_list"))
    return redirect(url_for("main.job", job_id=job_id))


@bp.route("/jobs/<int:job_id>")
@auth.require()
def job(job_id):
    row = get_db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone() or abort(404)
    hosts = get_db().execute("SELECT * FROM job_hosts WHERE job_id=? ORDER BY host", (job_id,)).fetchall()
    meta = json.loads(row["meta"] or "{}")
    approve = None
    if row["kind"] == "baseline_check":
        ok, why = ops.preview_state(get_db(), row)
        approve = {"ok": ok, "why": why}
    return render_template("job.html", job=row, hosts=hosts, kinds=jobs.KINDS, meta=meta, approve=approve, page="jobs")


@bp.route("/jobs/<int:job_id>/log.json")
@auth.require()
def job_log(job_id):
    row = get_db().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone() or abort(404)
    path = jobs.log_path(current_app.config, job_id)
    text = path.read_text(errors="replace")[-200_000:] if path.exists() else ""
    return jsonify(status=row["status"], log=text)


# ---- maintenance: windows, baseline approval, rolling reboot ---------------------------------------------------

def _fmt_local(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%a %d %b %H:%M") if ts else ""


@bp.route("/maintenance")
@auth.require()
def maintenance():
    db, inv = get_db(), _inv()
    tz, now = windows.get_zone(db), time.time()
    wins = windows.all_windows(db)
    for w in wins:
        st = windows.state(w, now, tz)
        w.update(open=st["open"], until=_fmt_local(st["until"], tz), next=_fmt_local(st["next"], tz),
                 days_text=" ".join(d.capitalize() for d in w["days"].split(",")))
    managed = ops.managed_hosts(inv)
    hosts = []
    for name, groups in managed.items():
        st = windows.host_status(windows.all_windows(db), groups, now, tz)
        hosts.append({"name": name, "restricted": st["restricted"], "open": st["open"],
                      "until": _fmt_local(st["until"], tz), "next": _fmt_local(st["next"], tz)})
    groups = sorted(inv.groups())
    targets = ["all"] + [g_ for g_ in groups if ops.reboot_targets(inv, g_)] + sorted(managed)
    previews = [r for r in db.execute("SELECT * FROM jobs WHERE kind='baseline_check' AND status='success' "
                                      "ORDER BY id DESC LIMIT 20") if ops.preview_state(db, r)[0]]
    recent = db.execute("SELECT * FROM jobs WHERE kind IN ('baseline_check','baseline_apply','rolling_reboot') "
                        "ORDER BY id DESC LIMIT 10").fetchall()
    return render_template("maintenance.html", windows=wins, hosts=hosts, groups=groups, targets=targets, previews=previews,
                           recent=recent, kinds=jobs.KINDS, days=windows.DAYS, zone=windows.zone_name(db),
                           now_local=_fmt_local(now, tz), page="maintenance")


@bp.post("/maintenance/windows")
@auth.require("admin")
def window_add():
    f = request.form
    try:
        wid = windows.add(get_db(), f.get("scope", ""), f.getlist("days"), f.get("start", ""), f.get("minutes", ""),
                          f.get("note", ""), g.user["username"], set(_inv().groups()))
    except windows.WindowError as e:
        flash(str(e), "error")
    else:
        _log("window.add", f.get("scope", ""), f"#{wid} {','.join(f.getlist('days'))} {f.get('start')} for {f.get('minutes')} min")
        flash("Maintenance window saved.", "ok")
    return redirect(url_for("main.maintenance"))


@bp.post("/maintenance/windows/<int:wid>/delete")
@auth.require("admin")
def window_delete(wid):
    if windows.remove(get_db(), wid):
        _log("window.delete", f"#{wid}")
        flash("Maintenance window removed.", "ok")
    return redirect(url_for("main.maintenance"))


@bp.post("/maintenance/timezone")
@auth.require("admin")
def window_zone():
    try:
        windows.set_zone(get_db(), request.form.get("zone", "").strip())
    except windows.WindowError as e:
        flash(str(e), "error")
    else:
        _log("window.timezone", request.form.get("zone", "").strip())
        flash("Time zone saved.", "ok")
    return redirect(url_for("main.maintenance"))


@bp.post("/devices/<name>/baseline/preview")
@auth.require("operator")
def baseline_preview(name):
    _devices().get(name) or abort(404)
    try:
        job_id = ops.start_preview(current_app._get_current_object(), name, g.user["username"], auth.client_ip(), _inv())
    except (ops.OpsError, jobs.JobError) as e:
        flash(str(e), "error")
        return redirect(url_for("main.device", name=name))
    return redirect(url_for("main.job", job_id=job_id))


@bp.post("/jobs/<int:job_id>/approve")
@auth.require("admin")
def baseline_approve(job_id):
    try:
        new_id = ops.approve(current_app._get_current_object(), job_id, request.form.get("confirm", ""),
                             request.form.get("override", ""), g.user["username"], auth.client_ip(), _inv())
    except (ops.OpsError, jobs.JobError) as e:
        flash(str(e), "error")
        return redirect(url_for("main.job", job_id=job_id))
    return redirect(url_for("main.job", job_id=new_id))


@bp.post("/maintenance/reboot")
@auth.require("admin")
def rolling_reboot():
    try:
        job_id = ops.start_reboot(current_app._get_current_object(), request.form.get("target", ""),
                                  request.form.get("confirm", ""), request.form.get("override", ""),
                                  g.user["username"], auth.client_ip(), _inv())
    except (ops.OpsError, jobs.JobError) as e:
        flash(str(e), "error")
        return redirect(url_for("main.maintenance"))
    return redirect(url_for("main.job", job_id=job_id))


# ---- compliance report, backup ---------------------------------------------------------------------------------

@bp.route("/compliance/report")
@auth.require()
def compliance_report():
    rep = report.build(_devices(), current_app.config["ROOT"])
    _log("report.view")
    return render_template("report.html", rep=rep, page="compliance")


@bp.route("/compliance/report.csv")
@auth.require()
def compliance_report_csv():
    rep = report.build(_devices(), current_app.config["ROOT"])
    _log("report.export", detail=f"{rep['checks']} checks")
    return report.to_csv(rep), 200, {"Content-Type": "text/csv; charset=utf-8",
                                     "Content-Disposition": "attachment; filename=compliance-report.csv"}


@bp.post("/settings/backup")
@auth.require("admin")
def backup_download():
    """Build the encrypted archive in memory and send it. Nothing is written to disk and the passphrase is never stored."""
    cfg = current_app.config
    if request.form.get("passphrase", "") != request.form.get("passphrase2", ""):
        flash("The two passphrases differ.", "error")
        return redirect(url_for("main.settings"))
    try:
        blob = backup.create(cfg["DB_PATH"], _inv().dir if not _inv().is_example else "/nonexistent",
                             request.form.get("passphrase", ""), drift.controller_head(cfg["ROOT"]) or "")
    except backup.BackupError as e:
        flash(str(e), "error")
        return redirect(url_for("main.settings"))
    _log("backup.download", detail=f"{len(blob)} bytes")
    name = time.strftime("provisionkit-backup-%Y%m%d-%H%M%S.pkbackup")
    return blob, 200, {"Content-Type": "application/octet-stream", "Content-Disposition": f"attachment; filename={name}"}


# ---- audit, settings -------------------------------------------------------------------------

@bp.route("/audit")
@auth.require("operator")
def audit_log():
    rows = get_db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("audit.html", rows=rows, page="audit")


@bp.route("/settings")
@auth.require("admin")
def settings():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("settings.html", users=users, roles=auth.ROLES, problems=_inv().problems(),
                           vars=_inv().group_vars(), cfg=current_app.config, page="settings",
                           intervals=scheduler.INTERVALS, interval=scheduler.get_interval(db), stale_after=_stale_after(),
                           next_run=scheduler.next_run_at(db), last_run=scheduler.last_run(db))


@bp.post("/settings/schedule")
@auth.require("admin")
def schedule_update():
    try:
        minutes = int(request.form.get("minutes", ""))
        scheduler.set_interval(get_db(), minutes)
    except ValueError:
        flash("Choose one of the listed intervals.", "error")
    else:
        _log("schedule.update", "collect", f"{minutes} minutes" if minutes else "off")
        flash("Scheduled collection turned off." if not minutes else "Scheduled collection saved.", "ok")
    return redirect(url_for("main.settings"))


@bp.post("/settings/users")
@auth.require("admin")
def user_create():
    name, role = request.form.get("username", "").strip(), request.form.get("role", "")
    pw = request.form.get("password", "")
    try:
        if not name.replace("-", "").replace("_", "").replace(".", "").isalnum() or len(name) > 40:
            raise ValueError("Username may contain letters, digits, dot, dash and underscore (max 40).")
        if role not in auth.ROLES:
            raise ValueError("Unknown role.")
        get_db().execute("INSERT INTO users (username, pw_hash, role, created) VALUES (?,?,?,?)",
                         (name, auth.hash_password(pw), role, time.time()))
        get_db().commit()
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:  # sqlite3.IntegrityError on duplicate username
        flash("That username already exists." if "UNIQUE" in str(e) else "Could not create user.", "error")
    else:
        _log("user.create", name, role)
        flash(f"User {name} created.", "ok")
    return redirect(url_for("main.settings"))


@bp.post("/settings/users/<int:uid>/toggle")
@auth.require("admin")
def user_toggle(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(404)
    if u["id"] == g.user["id"]:
        flash("You cannot disable your own account.", "error")
    else:
        db.execute("UPDATE users SET active=? WHERE id=?", (0 if u["active"] else 1, uid))
        db.commit()
        _log("user.disable" if u["active"] else "user.enable", u["username"])
    return redirect(url_for("main.settings"))


# ---- the Update button -----------------------------------------------------------------------------------------

@bp.get("/update")
@auth.require("admin")
def update_info():
    cfg = current_app.config
    st = updater.status(cfg)
    ok = updater.configured(cfg)
    busy = jobs._lock.locked()
    reason = ("" if ok else "Updating from the panel is not set up. Run: sudo scripts/install_panel.sh") or \
        ("A job is running. Wait for it to finish." if busy else "") or \
        ("An update is in progress." if st["state"] in ("running", "requested") else "")
    return jsonify(version=updater.version(cfg["ROOT"]), available=updater.available(cfg), status=st,
                   log=updater.log_tail(cfg), can_start=not reason, reason=reason, now=time.time())


@bp.post("/update/start")
@auth.require("admin")
def update_start():
    """Ask the root-run updater to run `provisionkit update`. The panel itself changes nothing."""
    cfg = current_app.config
    if jobs._lock.locked():
        return jsonify(ok=False, error="A job is running. Wait for it to finish, because the update restarts the panel."), 409
    try:
        updater.request(cfg)
    except updater.UpdateError as e:
        return jsonify(ok=False, error=str(e)), 409
    _log("update.request", "provisionkit", "from " + (updater.version(cfg["ROOT"]).get("short") or "unknown"))
    return jsonify(ok=True, requested_at=time.time()), 202


# ---- JSON API and health ---------------------------------------------------------------------

@bp.get("/api/v1/devices")
@auth.require()
def api_devices():
    keep = ("name", "address", "groups", "status", "compliance", "os", "kernel", "arch", "cpus", "mem_mb", "uptime_s",
            "reboot", "checks_passed", "checks_total")
    return jsonify([dict({k: d[k] for k in keep}, config=d["drift"]["state"] if d["drift"] else None)
                    for d in _devices().values()])


@bp.get("/api/v1/summary")
@auth.require()
def api_summary():
    s = fleet.summary(_devices())
    return jsonify({k: s[k] for k in ("total", "status", "compliant", "scanned", "reboot", "findings", "compliance_pct",
                                      "drift")})


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return jsonify(status="ok")
