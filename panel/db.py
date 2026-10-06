"""SQLite storage: users, host snapshots, jobs, audit log."""
import json
import sqlite3
import time

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('viewer','operator','admin')),
  active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL, ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
  host TEXT PRIMARY KEY, collected REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_status (
  host TEXT PRIMARY KEY, reachable INTEGER NOT NULL, checked REAL NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, user TEXT NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL, exit_code INTEGER);
CREATE TABLE IF NOT EXISTS job_hosts (
  job_id INTEGER NOT NULL, host TEXT NOT NULL, ok INTEGER, failed INTEGER, unreachable INTEGER,
  message TEXT, PRIMARY KEY (job_id, host));
CREATE TABLE IF NOT EXISTS maintenance_windows (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, days TEXT NOT NULL, start TEXT NOT NULL, minutes INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user TEXT, action TEXT NOT NULL, target TEXT, detail TEXT, ip TEXT);
"""


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    db = connect(path)
    db.executescript(SCHEMA)
    if "meta" not in {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}:  # added after the first release
        db.execute("ALTER TABLE jobs ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
    db.commit()
    db.close()


def audit(db, user, action, target="", detail="", ip=""):
    db.execute("INSERT INTO audit (ts, user, action, target, detail, ip) VALUES (?,?,?,?,?,?)",
               (time.time(), user, action, target, detail, ip))
    db.commit()


def save_snapshot(db, host, data):
    db.execute("INSERT INTO snapshots (host, collected, data) VALUES (?,?,?) "
               "ON CONFLICT(host) DO UPDATE SET collected=excluded.collected, data=excluded.data",
               (host, time.time(), json.dumps(data)))
    db.commit()


def snapshots(db):
    return {r["host"]: dict(json.loads(r["data"]), _collected=r["collected"])
            for r in db.execute("SELECT * FROM snapshots")}
