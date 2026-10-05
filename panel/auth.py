"""Sessions, roles, CSRF protection and login throttling."""
import functools
import hmac
import secrets
import time

from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import audit, get_db

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
MAX_FAILURES, WINDOW_S = 5, 600
MIN_PASSWORD = 12
# Hash checked when the user does not exist, so response time does not reveal valid usernames.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def hash_password(pw):
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
    return generate_password_hash(pw)


def client_ip():
    return request.remote_addr or "-"


def locked_out(db, username, ip):
    since = time.time() - WINDOW_S
    by_user = db.execute("SELECT COUNT(*) FROM login_attempts WHERE username=? AND ts>?", (username, since)).fetchone()[0]
    by_ip = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND ts>?", (ip, since)).fetchone()[0]
    return by_user >= MAX_FAILURES or by_ip >= MAX_FAILURES * 4


def attempt_login(username, password):
    """Return the user row on success, else None. Records failures and audits both outcomes."""
    db, ip = get_db(), client_ip()
    if locked_out(db, username, ip):
        audit(db, username, "login.locked", ip=ip)
        return None
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = check_password_hash(user["pw_hash"] if user else _DUMMY_HASH, password) and user and user["active"]
    if not ok:
        db.execute("INSERT INTO login_attempts (username, ip, ts) VALUES (?,?,?)", (username, ip, time.time()))
        db.commit()
        audit(db, username, "login.failed", ip=ip)
        return None
    db.execute("DELETE FROM login_attempts WHERE username=?", (username,))
    db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user["id"]))
    db.commit()
    audit(db, username, "login", ip=ip)
    return user


def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        row = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if row:
            g.user = row
        else:
            session.clear()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Invalid or missing CSRF token.")


def require(role="viewer"):
    def deco(view):
        @functools.wraps(view)
        def wrapped(*a, **kw):
            if g.user is None:
                if request.path.startswith("/api/"):
                    abort(401)
                return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
            if RANK[g.user["role"]] < RANK[role]:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target else None
