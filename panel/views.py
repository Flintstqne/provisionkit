import csv
import io
import socket
import time

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)

from . import auth, fleet, jobs
from .db import audit, get_db
from .inventory import NO_ADD, InventoryError

bp = Blueprint("main", __name__)
TABS = ("overview", "hardware", "network", "storage", "compliance", "configuration", "activity")


def _inv():
    return current_app.config["INVENTORY"]


def _devices():
    return fleet.devices(_inv(), get_db(), current_app.config["STALE_AFTER_S"])


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
                 or d["reboot"]]
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
            flash(f"Device {name} added to the inventory. Bootstrap it from the controller, then collect its data.", "ok")
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
    return render_template("device.html", d=d, tab=tab, tabs=TABS, jobs=jobs_for, activity=activity, last_validate=last,
                           host_status=status, page="devices")


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


@bp.post("/devices/<name>/ping")
@auth.require("operator")
def device_ping(name):
    """TCP connect to the SSH port from the controller. No credentials involved."""
    d = _devices().get(name) or abort(404)
    port = int(_inv().raw_vars().get("provisionkit_ssh_port", 22))
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
    return render_template("jobs.html", jobs=rows, kinds=jobs.KINDS, targets=targets, page="jobs")


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
    return render_template("job.html", job=row, hosts=hosts, kinds=jobs.KINDS, page="jobs")


@bp.route("/jobs/<int:job_id>/log.json")
@auth.require()
def job_log(job_id):
    row = get_db().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone() or abort(404)
    path = jobs.log_path(current_app.config, job_id)
    text = path.read_text(errors="replace")[-200_000:] if path.exists() else ""
    return jsonify(status=row["status"], log=text)


# ---- audit, settings -------------------------------------------------------------------------

@bp.route("/audit")
@auth.require("operator")
def audit_log():
    rows = get_db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("audit.html", rows=rows, page="audit")


@bp.route("/settings")
@auth.require("admin")
def settings():
    users = get_db().execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("settings.html", users=users, roles=auth.ROLES, problems=_inv().problems(),
                           vars=_inv().group_vars(), cfg=current_app.config, page="settings")


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


# ---- JSON API and health ---------------------------------------------------------------------

@bp.get("/api/v1/devices")
@auth.require()
def api_devices():
    keep = ("name", "address", "groups", "status", "compliance", "os", "kernel", "arch", "cpus", "mem_mb", "uptime_s",
            "reboot", "checks_passed", "checks_total")
    return jsonify([{k: d[k] for k in keep} for d in _devices().values()])


@bp.get("/api/v1/summary")
@auth.require()
def api_summary():
    s = fleet.summary(_devices())
    return jsonify({k: s[k] for k in ("total", "status", "compliant", "scanned", "reboot", "findings", "compliance_pct")})


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return jsonify(status="ok")
