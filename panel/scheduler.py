"""Collect data from every host on a timer. The interval is a setting, stored in SQLite, changed on the Settings page."""
import threading
import time

from . import jobs
from .db import connect

INTERVALS = [(0, "Off"), (30, "Every 30 minutes"), (60, "Every hour"), (120, "Every 2 hours"),
             (240, "Every 4 hours"), (360, "Every 6 hours"), (720, "Every 12 hours"), (1440, "Every day")]
VALID = {m for m, _ in INTERVALS}
TICK_S = 30


def get_interval(db):
    row = db.execute("SELECT value FROM settings WHERE key='collect_interval_minutes'").fetchone()
    try:
        minutes = int(row["value"]) if row else 0
    except ValueError:
        minutes = 0
    return minutes if minutes in VALID else 0


def set_interval(db, minutes):
    if minutes not in VALID:
        raise ValueError("Choose one of the listed intervals.")
    db.execute("INSERT INTO settings (key, value) VALUES ('collect_interval_minutes', ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(minutes),))
    db.commit()


def last_run(db):
    """The most recent collect-all job, whoever started it, so a manual refresh also resets the timer."""
    return db.execute("SELECT * FROM jobs WHERE kind='collect' AND target='all' ORDER BY id DESC LIMIT 1").fetchone()


def next_run_at(db):
    minutes = get_interval(db)
    if not minutes:
        return None
    last = last_run(db)
    return (last["created"] + minutes * 60) if last else time.time()


def due(db, now=None):
    nxt = next_run_at(db)
    return nxt is not None and (now or time.time()) >= nxt


def tick(app):
    """Start a scheduled collection if one is due and no other job is running. Returns the job id or None."""
    db = connect(app.config["DB_PATH"])
    try:
        if not due(db):
            return None
    finally:
        db.close()
    try:
        return jobs.start(app, "collect", "all", "scheduler", "-", app.config["INVENTORY"])
    except jobs.JobError:
        return None  # another job holds the lock; try again on the next tick


def start(app):
    def loop():
        while True:
            time.sleep(TICK_S)
            try:
                tick(app)
            except Exception as e:  # a bad tick must never stop the schedule
                app.logger.warning("scheduler tick failed: %r", e)

    threading.Thread(target=loop, name="collect-scheduler", daemon=True).start()
