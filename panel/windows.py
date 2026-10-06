"""Maintenance windows: the hours in which changes (baseline apply, rolling reboot) are allowed.

A window belongs to a group (or to every node, scope "all"), repeats on chosen weekdays at a start time in the panel's
time zone, and lasts a number of minutes. A node with no window that applies to it is unrestricted. Read-only jobs such
as collection and validation ignore windows. Nightly reboots run from timers on the nodes themselves, so windows do not
move them.
"""
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_ZONE = "America/New_York"
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
ZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
MAX_WINDOWS = 50
MIN_MINUTES, MAX_MINUTES = 15, 24 * 60


class WindowError(ValueError):
    pass


def get_zone(db):
    row = db.execute("SELECT value FROM settings WHERE key='timezone'").fetchone()
    return _zone(row["value"] if row else DEFAULT_ZONE) or ZoneInfo(DEFAULT_ZONE)


def zone_name(db):
    row = db.execute("SELECT value FROM settings WHERE key='timezone'").fetchone()
    return row["value"] if row and _zone(row["value"]) else DEFAULT_ZONE


def _zone(name):
    if not ZONE_RE.match(name or ""):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def set_zone(db, name):
    if not _zone(name):
        raise WindowError("That is not a time zone name. Try America/New_York or Europe/London.")
    db.execute("INSERT INTO settings (key, value) VALUES ('timezone', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (name,))
    db.commit()


def add(db, scope, days, start, minutes, note, user, groups):
    """Validate and store one window. `groups` is the set of group names that exist."""
    if scope != "all" and scope not in groups:
        raise WindowError("Choose a group from the list, or all nodes.")
    days = [d for d in DAYS if d in set(days)]
    if not days:
        raise WindowError("Choose at least one day.")
    if not TIME_RE.match(start or ""):
        raise WindowError("The start time must look like 02:00 (24 hour clock).")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise WindowError("The length must be a number of minutes.") from None
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise WindowError(f"The length must be {MIN_MINUTES} to {MAX_MINUTES} minutes.")
    note = (note or "").strip()
    if len(note) > 80:
        raise WindowError("The note can be 80 characters at most.")
    if db.execute("SELECT COUNT(*) FROM maintenance_windows").fetchone()[0] >= MAX_WINDOWS:
        raise WindowError(f"At most {MAX_WINDOWS} windows.")
    cur = db.execute("INSERT INTO maintenance_windows (scope, days, start, minutes, note, created_by, created) "
                     "VALUES (?,?,?,?,?,?,?)", (scope, ",".join(days), start, minutes, note, user, time.time()))
    db.commit()
    return cur.lastrowid


def remove(db, window_id):
    cur = db.execute("DELETE FROM maintenance_windows WHERE id=?", (window_id,))
    db.commit()
    return cur.rowcount > 0


def all_windows(db):
    return [dict(r) for r in db.execute("SELECT * FROM maintenance_windows ORDER BY scope, start, id")]


def _occurrence(w, day, tz):
    """(start_ts, end_ts) of the window that begins on `day` (a date in the panel's zone), or None if it skips that day."""
    if DAYS[day.weekday()] not in w["days"].split(","):
        return None
    h, m = int(w["start"][:2]), int(w["start"][3:])
    start = datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()
    return start, start + w["minutes"] * 60


def state(w, now, tz):
    """For one window: whether it is open at `now`, when that opening ends, and when it next opens."""
    today = datetime.fromtimestamp(now, tz).date()
    open_until, next_start = None, None
    for offset in range(-1, 9):  # yesterday's window can run past midnight; look a week ahead for the next one
        occ = _occurrence(w, today + timedelta(days=offset), tz)
        if not occ:
            continue
        if occ[0] <= now < occ[1]:
            open_until = occ[1]
        elif occ[0] > now and (next_start is None or occ[0] < next_start):
            next_start = occ[0]
    return {"open": open_until is not None, "until": open_until, "next": next_start}


def applies(w, groups):
    return w["scope"] == "all" or w["scope"] in groups


def host_status(windows, groups, now, tz):
    """{restricted, open, until, next} for one node. Not restricted means no window applies, so changes are allowed."""
    mine = [(w, state(w, now, tz)) for w in windows if applies(w, groups)]
    if not mine:
        return {"restricted": False, "open": True, "until": None, "next": None}
    opened = [s for _, s in mine if s["open"]]
    nexts = [s["next"] for _, s in mine if s["next"]]
    return {"restricted": True, "open": bool(opened), "until": max((s["until"] for s in opened), default=None),
            "next": min(nexts) if nexts else None}


def gate(db, hosts, now=None):
    """hosts: {name: [groups]}. Returns (allowed, blocked) where blocked lists (host, next opening or None)."""
    now = now or time.time()
    tz, windows = get_zone(db), all_windows(db)
    blocked = []
    for name, groups in sorted(hosts.items()):
        st = host_status(windows, groups, now, tz)
        if st["restricted"] and not st["open"]:
            blocked.append((name, st["next"]))
    return not blocked, blocked


def describe(blocked, tz):
    parts = []
    for name, nxt in blocked:
        when = datetime.fromtimestamp(nxt, tz).strftime("%a %H:%M") if nxt else "never"
        parts.append(f"{name} (next window {when})")
    return "Outside the maintenance window for " + ", ".join(parts) + "."
