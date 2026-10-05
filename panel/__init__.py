"""ProvisionKit Control Center: a small web console that runs on the Ansible controller."""
import secrets
import time

from flask import Flask, g, render_template, request, session

from . import auth, fleet
from .config import Config, inventory_dir
from .db import close_db, init_db
from .inventory import GROUP_INFO, Inventory


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(overrides or {})
    for d in (app.config["SNAPSHOT_DIR"], app.config["JOB_LOG_DIR"]):
        d.mkdir(parents=True, exist_ok=True)
    app.config.setdefault("INVENTORY", None)
    app.config["INVENTORY"] = Inventory(app.config["INVENTORY"] or inventory_dir())
    init_db(app.config["DB_PATH"])
    _secret_key(app)
    _recover_jobs(app)

    app.teardown_appcontext(close_db)
    app.before_request(auth.load_user)
    app.before_request(auth.check_csrf)

    from .views import bp
    app.register_blueprint(bp)

    @app.context_processor
    def inject():
        return {"csrf_token": auth.csrf_token, "inv": app.config["INVENTORY"], "demo": app.config["DEMO"],
                "ago": fleet.ago, "uptime": fleet.humanize_uptime, "group_info": GROUP_INFO,
                "env_name": _env_name(app), "now": time.time()}

    app.add_template_filter(lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)), "timestamp")

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    for code, title in ((400, "Bad request"), (403, "Access denied"), (404, "Not found"), (413, "Request too large")):
        app.register_error_handler(code, lambda e, c=code, t=title: (render_template("error.html", code=c, title=t,
                                   message=getattr(e, "description", "")), c))
    return app


def _env_name(app):
    try:
        return app.config["INVENTORY"].raw_vars().get("provisionkit_environment", "")
    except OSError:
        return ""


def _secret_key(app):
    """A random key persisted in the instance dir, created with owner-only permissions."""
    path = app.config["DB_PATH"].parent / "secret_key" if not app.config.get("SECRET_KEY") else None
    if path is None:
        return
    if not path.exists():
        path.touch(mode=0o600)
        path.write_text(secrets.token_hex(32))
    app.config["SECRET_KEY"] = path.read_text().strip()


def _recover_jobs(app):
    """Jobs left running by a previous process can never finish; mark them failed."""
    from .db import connect
    db = connect(app.config["DB_PATH"])
    db.execute("UPDATE jobs SET status='failed', finished=? WHERE status IN ('queued','running')", (time.time(),))
    db.commit()
    db.close()
