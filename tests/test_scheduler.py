"""Scheduled collection. Run: python -m pytest tests/test_scheduler.py"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import jobs, scheduler  # noqa: E402
from panel.db import connect, save_snapshot  # noqa: E402
from test_panel import _snap, app, as_user, post  # noqa: E402,F401


def wait_idle():
    for _ in range(80):
        if jobs._lock.acquire(blocking=False):
            jobs._lock.release()
            return
        time.sleep(0.25)


def db_of(app):
    return connect(app.config["DB_PATH"])


def test_interval_defaults_to_off_and_validates(app):
    db = db_of(app)
    assert scheduler.get_interval(db) == 0 and scheduler.next_run_at(db) is None and not scheduler.due(db)
    scheduler.set_interval(db, 120)
    assert scheduler.get_interval(db) == 120
    for bad in (-5, 7, 99999):
        with pytest.raises(ValueError):
            scheduler.set_interval(db, bad)
    assert scheduler.get_interval(db) == 120


def test_first_run_is_due_immediately_then_after_the_interval(app):
    db = db_of(app)
    scheduler.set_interval(db, 60)
    assert scheduler.due(db)  # never collected
    db.execute("INSERT INTO jobs (kind, target, user, status, created) VALUES ('collect','all','x','success',?)",
               (time.time() - 30 * 60,))
    db.commit()
    assert not scheduler.due(db)
    assert scheduler.due(db, now=time.time() + 31 * 60)


def test_a_manual_collect_all_resets_the_timer_but_a_single_host_does_not(app):
    db = db_of(app)
    scheduler.set_interval(db, 60)
    db.execute("INSERT INTO jobs (kind, target, user, status, created) VALUES ('collect','pk-worker','x','success',?)",
               (time.time(),))
    db.commit()
    assert scheduler.due(db)
    db.execute("INSERT INTO jobs (kind, target, user, status, created) VALUES ('collect','all','x','success',?)",
               (time.time(),))
    db.commit()
    assert not scheduler.due(db)


def test_tick_starts_one_scheduled_collection(app):
    scheduler.set_interval(db_of(app), 60)
    job_id = scheduler.tick(app)
    assert job_id
    wait_idle()
    row = db_of(app).execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert (row["kind"], row["target"], row["user"], row["status"]) == ("collect", "all", "scheduler", "success")
    assert scheduler.tick(app) is None  # not due again until the interval passes


def test_tick_does_nothing_when_off(app):
    assert scheduler.tick(app) is None
    assert db_of(app).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_tick_waits_while_another_job_runs(app):
    scheduler.set_interval(db_of(app), 60)
    assert jobs._lock.acquire(blocking=False)
    try:
        assert scheduler.tick(app) is None
    finally:
        jobs._lock.release()
    assert db_of(app).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert scheduler.tick(app)  # retried on the next tick, once the lock is free
    wait_idle()


def test_schedule_form_is_admin_only_and_validated(app):
    admin = as_user(app, "alice")
    assert post(admin, "/settings/schedule", minutes="120").status_code == 302
    assert scheduler.get_interval(db_of(app)) == 120
    r = post(admin, "/settings/schedule", minutes="13", follow_redirects=True)
    assert b"Choose one of the listed intervals" in r.data and scheduler.get_interval(db_of(app)) == 120
    assert post(admin, "/settings/schedule", minutes="abc", follow_redirects=True).status_code == 200
    assert post(as_user(app, "olga"), "/settings/schedule", minutes="0").status_code == 403
    assert post(as_user(app, "bob"), "/settings/schedule", minutes="0").status_code == 403
    assert admin.post("/settings/schedule", data={"minutes": "0"}).status_code == 400  # no CSRF token
    assert scheduler.get_interval(db_of(app)) == 120
    assert b"schedule.update" in admin.get("/audit").data


def test_settings_page_shows_the_schedule(app):
    scheduler.set_interval(db_of(app), 240)
    html = as_user(app, "alice").get("/settings").get_data(as_text=True)
    assert "Scheduled collection" in html and 'value="240" selected' in html and "Next run" in html


def test_staleness_scales_with_the_interval(app):
    db = db_of(app)
    save_snapshot(db, "pk-server", _snap("pk-server"))
    db.execute("UPDATE snapshots SET collected=?", (time.time() - 2 * 86400,))
    db.commit()
    c = as_user(app, "alice")

    def status():
        return {d["name"]: d["status"] for d in c.get("/api/v1/devices").get_json()}["pk-server"]
    assert status() == "Stale"  # default: stale after 6 hours
    scheduler.set_interval(db, 1440)
    assert status() == "Online"  # daily collection tolerates up to three missed runs
