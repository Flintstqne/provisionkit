"""Panel tests. Run: python -m pytest tests/test_panel.py"""
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from panel import auth, create_app  # noqa: E402
from panel.db import connect, save_snapshot  # noqa: E402
from panel.jobs import parse_log  # noqa: E402

PW = "correct-horse-battery"


@pytest.fixture()
def app(tmp_path):
    inv = tmp_path / "inv"
    shutil.copytree(ROOT / "inventories" / "example", inv)
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "DEMO": True, "INVENTORY": inv,
                      "DB_PATH": tmp_path / "p.db", "SNAPSHOT_DIR": tmp_path / "snap", "JOB_LOG_DIR": tmp_path / "jobs"})
    db = connect(app.config["DB_PATH"])
    for name, role in (("alice", "admin"), ("bob", "viewer"), ("olga", "operator")):
        db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES (?,?,?,?)",
                   (name, auth.hash_password(PW), role, time.time()))
    db.commit()
    db.close()
    return app


def csrf(client):
    """The CSRF token lives in the session; rendering any page creates it."""
    client.get("/login")
    with client.session_transaction() as sess:
        missing = "csrf" not in sess
    if missing:  # request outside the transaction: leaving it would overwrite the new session
        client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def login(app, user="alice", pw=PW):
    c = app.test_client()
    return c, c.post("/login", data={"username": user, "password": pw, "csrf_token": csrf(c)})


def post(c, url, follow_redirects=False, **data):
    return c.post(url, data=dict(data, csrf_token=csrf(c)), follow_redirects=follow_redirects)


def as_user(app, user):
    c, r = login(app, user)
    assert r.status_code == 302
    return c


def test_requires_login(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/api/v1/devices").status_code == 401
    assert c.get("/healthz").status_code == 200


def test_login_rejects_bad_password_and_missing_csrf(app):
    c, r = login(app, pw="wrong-password-123")
    assert r.status_code == 200 and b"Sign-in failed" in r.data
    assert app.test_client().post("/login", data={"username": "alice", "password": PW}).status_code == 400


def test_lockout_after_repeated_failures(app):
    c = app.test_client()
    for _ in range(auth.MAX_FAILURES):
        post(c, "/login", username="alice", password="nope-nope-nope")
    r = post(c, "/login", username="alice", password=PW)  # correct password, still locked
    assert r.status_code == 200 and b"Sign-in failed" in r.data


def test_open_redirect_blocked(app):
    c = app.test_client()
    r = c.post("/login?next=//evil.example", data={"username": "alice", "password": PW, "csrf_token": csrf(c)})
    assert r.headers["Location"] == "/"


def test_security_headers(app):
    r = as_user(app, "alice").get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"


def test_add_device_writes_inventory_and_keeps_comments(app):
    c = as_user(app, "alice")
    r = post(c, "/devices/new", name="pk-new", address="10.0.0.50", groups=["workload_nodes", "docker_hosts"])
    assert r.status_code == 302
    text = (app.config["INVENTORY"].hosts_file).read_text()
    assert "pk-new: {ansible_host: 10.0.0.50}" in text
    assert "# Documentation addresses" in text  # YAML comments survive the edit
    assert list((app.config["INVENTORY"].dir / ".backups").glob("hosts.*.yml"))
    hosts = app.config["INVENTORY"].hosts()
    assert hosts["pk-new"]["groups"] == ["workload_nodes", "docker_hosts"]
    assert c.get("/devices/pk-new").status_code == 200


@pytest.mark.parametrize("name,addr,groups,msg", [
    ("PK_bad", "10.0.0.51", ["workload_nodes"], "Hostname"),
    ("pk-new", "not an address!", ["workload_nodes"], "Address"),
    ("pk-new", "127.0.0.1", ["workload_nodes"], "routable"),
    ("pk-server", "10.0.0.52", ["workload_nodes"], "already exists"),
    ("pk-new", "192.0.2.20", ["workload_nodes"], "multiple hosts"),
    ("pk-new", "10.0.0.53", ["controllers"], "cannot be added"),
    ("pk-new", "10.0.0.54", ["nonexistent"], "Unknown group"),
    ("pk-new", "10.0.0.55", [], "at least one"),
])
def test_add_device_validation(app, name, addr, groups, msg):
    c = as_user(app, "alice")
    before = app.config["INVENTORY"].hosts_file.read_text()
    r = post(c, "/devices/new", name=name, address=addr, groups=groups)
    assert r.status_code == 200 and msg.encode() in r.data
    assert app.config["INVENTORY"].hosts_file.read_text() == before


def test_example_inventory_is_read_only():
    from panel.inventory import Inventory, InventoryError
    inv = Inventory(ROOT / "inventories" / "example")
    with pytest.raises(InventoryError, match="read-only"):
        inv.add_host("x", "10.0.0.9", ["workload_nodes"])


def test_remove_device(app):
    c = as_user(app, "alice")
    assert post(c, "/devices/pk-worker/remove").status_code == 302
    hosts = app.config["INVENTORY"].hosts()
    assert "pk-worker" not in hosts and "pk-server" in hosts
    assert "pk-worker" not in app.config["INVENTORY"].groups()["k3s_agents"]


def test_roles(app):
    viewer, operator = as_user(app, "bob"), as_user(app, "olga")
    assert viewer.get("/devices").status_code == 200
    assert viewer.get("/devices/new").status_code == 403
    assert post(viewer, "/jobs", kind="collect", target="all").status_code == 403
    assert viewer.get("/audit").status_code == 403
    assert operator.get("/settings").status_code == 403
    assert operator.get("/devices/new").status_code == 403
    assert post(operator, "/devices/pk-worker/remove").status_code == 403


def test_post_without_csrf_rejected(app):
    c = as_user(app, "alice")
    assert c.post("/devices/pk-worker/remove").status_code == 400


def _snap(host, ok=True):
    return {"host": host, "reboot_required": "True", "facts": {"os": "Ubuntu", "os_version": "24.04", "cpu_count": 2,
            "mem_total_mb": 4000, "mem_free_mb": 1000, "uptime_s": 7200, "kernel": "6.8",
            "mounts": [{"mount": "/", "device": "/dev/sda1", "fstype": "ext4", "size_total": 10 * 1024**3,
                        "size_available": 4 * 1024**3}]},
            "checks": [{"id": "a", "title": "A", "ok": True}, {"id": "b", "title": "B", "ok": ok},
                       {"id": "c", "title": "C", "ok": False, "applicable": False}]}


def test_fleet_states_and_summary(app):
    db = connect(app.config["DB_PATH"])
    save_snapshot(db, "pk-server", _snap("pk-server", ok=False))
    save_snapshot(db, "pk-worker", _snap("pk-worker"))
    db.execute("UPDATE snapshots SET collected=? WHERE host='pk-worker'", (time.time() - 10 * 86400,))
    db.commit()
    db.close()
    c = as_user(app, "alice")
    api = {d["name"]: d for d in c.get("/api/v1/devices").get_json()}
    assert api["pk-server"]["status"] == "Online" and api["pk-server"]["compliance"] == "Non-compliant"
    assert api["pk-server"]["checks_total"] == 2 and api["pk-server"]["reboot"] is True  # N/A check not scored
    assert api["pk-worker"]["status"] == "Stale" and api["pk-worker"]["compliance"] == "Compliant"
    assert api["pk-control"]["status"] == "Controller"
    s = c.get("/api/v1/summary").get_json()
    assert s["total"] == 2 and s["scanned"] == 2 and s["compliant"] == 1 and s["findings"] == 1


def test_csv_export_neutralises_formulas(app):
    c = as_user(app, "alice")
    assert c.get("/devices/export.csv").data.startswith(b"name,address")


def test_parse_ansible_output():
    log = """
TASK [Assert baseline state] ***
fatal: [pk-server]: FAILED! => {"assertion": "x", "changed": false, "msg": "Assertion failed"}
fatal: [pk-edge]: UNREACHABLE! => {"changed": false, "msg": "Failed to connect to the host via ssh: timed out", "unreachable": true}

PLAY RECAP ***
pk-server : ok=3 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0
pk-edge   : ok=0 changed=0 unreachable=1 failed=0 skipped=0 rescued=0 ignored=0
pk-worker : ok=9 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0
"""
    r = parse_log(log)
    assert r["pk-server"]["failed"] == 1 and "Assertion failed" in r["pk-server"]["message"]
    assert "Assert baseline state" in r["pk-server"]["message"]
    assert r["pk-edge"]["unreachable"] == 1 and "timed out" in r["pk-edge"]["message"]
    assert r["pk-worker"] == {"ok": 9, "failed": 0, "unreachable": 0, "message": ""}


def test_job_runs_and_rejects_bad_input(app):
    c = as_user(app, "olga")
    assert b"Unknown target" in post(c, "/jobs", kind="collect", target="x; rm -rf /", follow_redirects=True).data
    from panel import jobs
    with pytest.raises(jobs.JobError):
        jobs.start(app, "collect", "x; rm -rf /", "olga", "-", app.config["INVENTORY"])
    with pytest.raises(jobs.JobError):
        jobs.start(app, "baseline", "all", "olga", "-", app.config["INVENTORY"])
    r = post(c, "/jobs", kind="validate", target="pk-worker")
    job_id = int(r.headers["Location"].rsplit("/", 1)[1])
    for _ in range(60):
        j = c.get(f"/jobs/{job_id}/log.json").get_json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.25)
    assert j["status"] == "success" and "PLAY RECAP" in j["log"]
    assert b"pk-worker" in c.get(f"/jobs/{job_id}").data


def test_only_one_job_at_a_time(app):
    from panel import jobs
    c = as_user(app, "olga")
    post(c, "/jobs", kind="validate", target="pk-worker")
    with pytest.raises(jobs.JobError, match="running"):
        jobs.start(app, "validate", "pk-worker", "olga", "-", app.config["INVENTORY"])
    for _ in range(60):  # let the first job release the lock before the fixture is torn down
        if jobs._lock.acquire(blocking=False):
            jobs._lock.release()
            break
        time.sleep(0.25)


def test_user_management_and_password_policy(app):
    c = as_user(app, "alice")
    r = post(c, "/settings/users", username="carol", password="short", role="viewer", follow_redirects=True)
    assert b"at least 12" in r.data
    post(c, "/settings/users", username="carol", password=PW, role="viewer")
    assert login(app, "carol")[1].status_code == 302
    uid = connect(app.config["DB_PATH"]).execute("SELECT id FROM users WHERE username='carol'").fetchone()[0]
    post(c, f"/settings/users/{uid}/toggle")
    assert login(app, "carol")[1].status_code == 200  # disabled account cannot sign in
