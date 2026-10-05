"""CLI: python -m panel {run,create-user,init-inventory,demo}"""
import argparse
import getpass
import sys
import time

from . import auth, create_app
from .config import INSTANCE
from .db import connect
from .inventory import InventoryError, init_local


def main(argv=None):
    p = argparse.ArgumentParser(prog="panel", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="serve the console")
    run.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    run.add_argument("--port", type=int, default=8080)
    u = sub.add_parser("create-user", help="create a user (prompts for the password)")
    u.add_argument("username")
    u.add_argument("--role", choices=auth.ROLES, default="admin")
    sub.add_parser("init-inventory", help="copy inventories/example to inventories/local so devices can be added")
    d = sub.add_parser("demo", help="serve synthetic demo data on loopback (no lab, no Ansible needed)")
    d.add_argument("--port", type=int, default=8080)
    a = p.parse_args(argv)

    if a.cmd == "init-inventory":
        try:
            print(f"Created {init_local()}. Replace the documentation addresses and keys before use.")
        except InventoryError as e:
            sys.exit(str(e))
    elif a.cmd == "create-user":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords differ.")
        app = create_app()
        try:
            db = connect(app.config["DB_PATH"])
            db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES (?,?,?,?)",
                       (a.username, auth.hash_password(pw), a.role, time.time()))
            db.commit()
        except ValueError as e:
            sys.exit(str(e))
        except Exception:
            sys.exit("Could not create user (does it already exist?).")
        print(f"Created {a.role} {a.username}.")
    elif a.cmd == "demo":
        from . import demo
        demo_dir = INSTANCE / "demo"
        cfg = {"DEMO": True, "DB_PATH": demo_dir / "panel.db", "SNAPSHOT_DIR": demo_dir / "snapshots",
               "JOB_LOG_DIR": demo_dir / "jobs", "INVENTORY": demo_dir / "inventory"}
        demo_dir.mkdir(parents=True, exist_ok=True)
        app = create_app(cfg)
        demo.seed(app.config, demo_dir / "inventory")
        db = connect(app.config["DB_PATH"])
        if not db.execute("SELECT 1 FROM users WHERE username='demo'").fetchone():
            db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('demo', ?, 'admin', ?)",
                       (auth.hash_password("demo-password-123"), time.time()))
            db.commit()
        print(f"Demo mode (synthetic data, jobs are simulated). Sign in at http://127.0.0.1:{a.port} as demo / demo-password-123")
        serve(app, "127.0.0.1", a.port)
    else:
        serve(create_app(), a.host, a.port)


def serve(app, host, port):
    try:
        from waitress import serve as wserve
    except ImportError:
        app.run(host=host, port=port)
    else:
        wserve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
