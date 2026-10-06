"""Maintenance windows, baseline approval, rolling reboot and the compliance report. Run: python -m pytest tests/test_operations.py"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import jobs, ops, report, windows  # noqa: E402
from panel.db import connect, init_db  # noqa: E402
from test_panel import _snap, app, as_user, csrf, post  # noqa: E402,F401

NY = ZoneInfo("America/New_York")


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


@pytest.fixture()
def db(tmp_path):
    init_db(tmp_path / "w.db")
    c = connect(tmp_path / "w.db")
    yield c
    c.close()


def win(db, scope="all", days=("sat",), start="02:00", minutes=120):
    return windows.add(db, scope, list(days), start, minutes, "", "t", {"workload_nodes", "controllers"})


# ---- windows ---------------------------------------------------------------------------------------------------

def test_a_window_is_open_inside_and_closed_outside(db):
    win(db)  # Saturday 02:00 for two hours. 2026-10-03 is a Saturday.
    w = windows.all_windows(db)[0]
    assert windows.state(w, ts(2026, 10, 3, 2, 30), NY)["open"]
    assert not windows.state(w, ts(2026, 10, 3, 4, 0), NY)["open"]  # the end is exclusive
    assert not windows.state(w, ts(2026, 10, 3, 1, 59), NY)["open"]
    assert not windows.state(w, ts(2026, 10, 4, 2, 30), NY)["open"]  # Sunday


def test_a_window_that_crosses_midnight_is_open_after_midnight(db):
    win(db, days=("fri",), start="23:00", minutes=180)
    w = windows.all_windows(db)[0]
    st = windows.state(w, ts(2026, 10, 3, 0, 30), NY)  # Saturday 00:30 belongs to Friday's window
    assert st["open"] and st["until"] == ts(2026, 10, 3, 2, 0)


def test_next_opening_is_reported(db):
    win(db)
    w = windows.all_windows(db)[0]
    assert windows.state(w, ts(2026, 10, 5, 12), NY)["next"] == ts(2026, 10, 10, 2)


def test_the_window_follows_daylight_saving_time(db):
    win(db, days=("sun",), start="02:30", minutes=60)  # 2026-11-01 02:30 exists once; clocks go back at 02:00
    w = windows.all_windows(db)[0]
    assert windows.state(w, ts(2026, 11, 1, 3, 0), NY)["open"] or windows.state(w, ts(2026, 11, 1, 2, 45), NY)["open"]


def test_no_window_means_unrestricted(db):
    ok, blocked = windows.gate(db, {"pk-worker": ["workload_nodes"]})
    assert ok and not blocked


def test_a_group_window_restricts_only_that_group(db):
    win(db, scope="workload_nodes")
    ok, blocked = windows.gate(db, {"pk-worker": ["workload_nodes"], "other": ["controllers"]}, ts(2026, 10, 5, 12))
    assert not ok and [b[0] for b in blocked] == ["pk-worker"]
    ok, _ = windows.gate(db, {"pk-worker": ["workload_nodes"]}, ts(2026, 10, 3, 3))
    assert ok


@pytest.mark.parametrize("kw,msg", [
    ({"scope": "nope"}, "Choose a group"), ({"days": []}, "at least one day"), ({"days": ["xyz"]}, "at least one day"),
    ({"start": "25:00"}, "must look like"), ({"start": "2:00"}, "must look like"), ({"minutes": "5"}, "15 to 1440"),
    ({"minutes": "abc"}, "number of minutes"), ({"note": "x" * 81}, "80 characters")])
def test_window_validation(db, kw, msg):
    args = dict(scope="all", days=["sat"], start="02:00", minutes=60, note="")
    args.update(kw)
    with pytest.raises(windows.WindowError, match=msg):
        windows.add(db, args["scope"], args["days"], args["start"], args["minutes"], args["note"], "t", {"workload_nodes"})


def test_time_zone_must_exist(db):
    windows.set_zone(db, "Europe/London")
    assert windows.zone_name(db) == "Europe/London"
    for bad in ("Mars/Olympus", "../etc/passwd", "", "x" * 80):
        with pytest.raises(windows.WindowError):
            windows.set_zone(db, bad)
    assert windows.zone_name(db) == "Europe/London"


# ---- commands the jobs run ---------------------------------------------------------------------------------------

CFG = {"DEMO": False, "SNAPSHOT_DIR": Path("/snap")}


def cmds(kind, target="pk-worker"):
    return [[Path(c).name if i == 1 else c for i, c in enumerate(cmd)] for cmd in jobs._commands(CFG, kind, target, "/inv")]


def test_baseline_preview_is_a_dry_run_without_the_firewall():
    [c] = cmds("baseline_check")
    assert "baseline.yml" in c and "--check" in c and "--diff" in c
    assert c[c.index("--skip-tags") + 1] == "firewall" and c[c.index("--limit") + 1] == "pk-worker"


def test_baseline_apply_runs_baseline_then_validate_then_collect():
    steps = cmds("baseline_apply")
    assert [s[1] for s in steps] == ["baseline.yml", "validate.yml", "collect.yml"]
    assert "--check" not in steps[0] and "firewall" in steps[0]
    assert any(a.startswith("panel_out_dir=") for a in steps[2])


def test_rolling_reboot_uses_the_reboot_playbook_then_collects():
    assert [s[1] for s in cmds("rolling_reboot", "workload_nodes")] == ["reboot_rolling.yml", "collect.yml"]


def test_changing_jobs_cannot_be_started_through_the_generic_form(app):
    c = as_user(app, "olga")  # operator
    for kind in ("baseline_check", "baseline_apply", "rolling_reboot"):
        r = post(c, "/jobs", kind=kind, target="all", follow_redirects=True)
        assert "changes machines" in r.get_data(as_text=True)
    assert connect(app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# ---- the flow in the panel -----------------------------------------------------------------------------------------

def wait_idle():
    for _ in range(200):
        if not jobs._lock.locked():
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.fixture()
def wired(app, monkeypatch):
    """Demo jobs (simulated), a clean fixed checkout, and a managed node."""
    monkeypatch.setattr(ops, "_head", lambda root=ROOT: "a" * 40)
    monkeypatch.setattr(ops, "_dirty", lambda root=ROOT: [])
    inv = app.config["INVENTORY"]
    assert "workload_nodes" in inv.groups()
    return app


def managed(app):
    return sorted(ops.managed_hosts(app.config["INVENTORY"]))


def preview(app, user="olga"):
    host = managed(app)[0]
    c = as_user(app, user)
    r = post(c, f"/devices/{host}/baseline/preview")
    assert r.status_code == 302
    wait_idle()
    return host, int(r.headers["Location"].rsplit("/", 1)[1]), c


def test_a_preview_is_a_dry_run_any_operator_can_start(wired):
    host, job_id, _ = preview(wired)
    row = connect(wired.config["DB_PATH"]).execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["kind"] == "baseline_check" and row["target"] == host and json.loads(row["meta"])["commit"] == "a" * 40


def test_a_viewer_cannot_preview(wired):
    host = managed(wired)[0]
    assert post(as_user(wired, "bob"), f"/devices/{host}/baseline/preview").status_code == 403


def test_only_an_admin_can_approve_and_the_name_must_match(wired):
    host, job_id, op = preview(wired)
    assert post(op, f"/jobs/{job_id}/approve", confirm=host).status_code == 403
    admin = as_user(wired, "alice")
    r = post(admin, f"/jobs/{job_id}/approve", confirm="wrong", follow_redirects=True)
    assert "Type the node" in r.get_data(as_text=True)
    assert connect(wired.config["DB_PATH"]).execute("SELECT COUNT(*) FROM jobs WHERE kind='baseline_apply'").fetchone()[0] == 0


def test_approving_starts_the_apply_once_and_audits_it(wired):
    host, job_id, _ = preview(wired)
    admin = as_user(wired, "alice")
    r = post(admin, f"/jobs/{job_id}/approve", confirm=host)
    assert r.status_code == 302
    wait_idle()
    db = connect(wired.config["DB_PATH"])
    apply = db.execute("SELECT * FROM jobs WHERE kind='baseline_apply'").fetchone()
    assert apply["status"] == "success" and json.loads(apply["meta"])["source_job"] == job_id
    assert db.execute("SELECT 1 FROM audit WHERE action='baseline.approve'").fetchone()
    again = post(admin, f"/jobs/{job_id}/approve", confirm=host, follow_redirects=True)
    assert "Already applied" in again.get_data(as_text=True)


def test_a_stale_preview_cannot_be_approved(wired):
    host, job_id, _ = preview(wired)
    db = connect(wired.config["DB_PATH"])
    db.execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 3600, job_id))
    db.commit()
    r = post(as_user(wired, "alice"), f"/jobs/{job_id}/approve", confirm=host, follow_redirects=True)
    assert "older than 30 minutes" in r.get_data(as_text=True)


def test_a_new_commit_invalidates_the_preview(wired, monkeypatch):
    host, job_id, _ = preview(wired)
    monkeypatch.setattr(ops, "_head", lambda root=ROOT: "b" * 40)
    r = post(as_user(wired, "alice"), f"/jobs/{job_id}/approve", confirm=host, follow_redirects=True)
    assert "configuration changed" in r.get_data(as_text=True)


def test_uncommitted_changes_block_the_apply(wired, monkeypatch):
    host, job_id, _ = preview(wired)
    monkeypatch.setattr(ops, "_dirty", lambda root=ROOT: ["roles/x.yml"])
    r = post(as_user(wired, "alice"), f"/jobs/{job_id}/approve", confirm=host, follow_redirects=True)
    assert "uncommitted changes" in r.get_data(as_text=True)


def close_every_window(app):
    db = connect(app.config["DB_PATH"])
    day = windows.DAYS[(datetime.now(windows.get_zone(db)).weekday() + 3) % 7]  # a day that is not today
    windows.add(db, "all", [day], "03:00", 60, "", "t", set(app.config["INVENTORY"].groups()))
    db.close()


def test_a_closed_window_blocks_until_an_admin_gives_a_reason(wired):
    host, job_id, _ = preview(wired)
    close_every_window(wired)
    admin = as_user(wired, "alice")
    r = post(admin, f"/jobs/{job_id}/approve", confirm=host, follow_redirects=True)
    assert "Outside the maintenance window" in r.get_data(as_text=True)
    r = post(admin, f"/jobs/{job_id}/approve", confirm=host, override="short", follow_redirects=True)
    assert "reason of 8" in r.get_data(as_text=True)
    r = post(admin, f"/jobs/{job_id}/approve", confirm=host, override="security patch for CVE today")
    assert r.status_code == 302
    wait_idle()
    db = connect(wired.config["DB_PATH"])
    assert "security patch" in db.execute("SELECT detail FROM audit WHERE action='baseline.approve'").fetchone()[0]


def test_rolling_reboot_needs_the_typed_confirmation(wired):
    admin = as_user(wired, "alice")
    r = post(admin, "/maintenance/reboot", target="all", confirm="reboot all", follow_redirects=True)
    assert "Type REBOOT all" in r.get_data(as_text=True)
    r = post(admin, "/maintenance/reboot", target="all", confirm="REBOOT all")
    assert r.status_code == 302
    wait_idle()
    row = connect(wired.config["DB_PATH"]).execute("SELECT * FROM jobs WHERE kind='rolling_reboot'").fetchone()
    assert row["status"] == "success" and json.loads(row["meta"])["hosts"] == managed(wired)


def test_rolling_reboot_never_includes_the_controller(wired):
    inv = wired.config["INVENTORY"]
    controllers = set(inv.groups().get("controllers", []))
    assert controllers and not controllers & set(ops.reboot_targets(inv, "all"))
    assert ops.reboot_targets(inv, next(iter(controllers))) == {}


def test_rolling_reboot_is_admin_only_and_respects_windows(wired):
    assert post(as_user(wired, "olga"), "/maintenance/reboot", target="all", confirm="REBOOT all").status_code == 403
    close_every_window(wired)
    r = post(as_user(wired, "alice"), "/maintenance/reboot", target="all", confirm="REBOOT all", follow_redirects=True)
    assert "Outside the maintenance window" in r.get_data(as_text=True)


def test_window_pages_and_forms(wired):
    admin = as_user(wired, "alice")
    r = post(admin, "/maintenance/windows", scope="all", days=["sat", "sun"], start="02:00", minutes="120", note="weekend")
    assert r.status_code == 302
    page = admin.get("/maintenance").get_data(as_text=True)
    assert "Sat Sun 02:00 for 120 min" in page and "weekend" in page
    assert post(as_user(wired, "olga"), "/maintenance/windows", scope="all", days=["sat"], start="02:00",
                minutes="60").status_code == 403
    assert as_user(wired, "bob").get("/maintenance").status_code == 200


def test_hostile_note_is_escaped(wired):
    admin = as_user(wired, "alice")
    post(admin, "/maintenance/windows", scope="all", days=["sat"], start="02:00", minutes="60", note="<script>x</script>")
    assert "<script>x" not in admin.get("/maintenance").get_data(as_text=True)


# ---- report ------------------------------------------------------------------------------------------------------------

def test_report_csv_has_one_row_per_check_and_neutralises_formulas(wired):
    from panel.db import save_snapshot
    db = connect(wired.config["DB_PATH"])
    host = managed(wired)[0]
    snap = _snap(host)
    snap["checks"] = [{"id": "x", "title": "=cmd|' /C calc'!A0", "ok": True}, {"id": "y", "title": "Second", "ok": False}]
    save_snapshot(db, host, snap)
    r = as_user(wired, "bob").get("/compliance/report.csv")
    text = r.get_data(as_text=True)
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert "'=cmd" in text and "\n=" not in text
    assert text.count("Pass") == 1 and text.count("Fail") == 1
    html = as_user(wired, "bob").get("/compliance/report").get_data(as_text=True)
    assert "Compliance report" in html and "Second" in html


def test_report_requires_login(wired):
    assert wired.test_client().get("/compliance/report").status_code == 302
