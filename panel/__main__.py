"""CLI: python -m panel {run,create-user,init-inventory,demo,backup,restore}"""
import argparse
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path

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
    b = sub.add_parser("backup", help="write an encrypted backup of the panel data (prompts for a passphrase)")
    b.add_argument("--out", required=True, help="file to write, for example /home/you/pk.pkbackup")
    b.add_argument("--passphrase-file", help="read the passphrase from this file instead of asking")
    r = sub.add_parser("restore", help="replace the panel data from a backup. Stop the panel first.")
    r.add_argument("file")
    r.add_argument("--passphrase-file", help="read the passphrase from this file instead of asking")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
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
    elif a.cmd in ("backup", "restore"):
        sys.exit(_backup_command(a))
    elif a.cmd == "demo":
        from . import demo
        demo_dir = INSTANCE / "demo"
        cfg = {"DEMO": True, "DB_PATH": demo_dir / "panel.db", "SNAPSHOT_DIR": demo_dir / "snapshots",
               "JOB_LOG_DIR": demo_dir / "jobs", "INVENTORY": demo_dir / "inventory",
               "UPDATE_REQUEST_DIR": demo_dir / "update-requests", "UPDATE_STATUS_DIR": demo_dir / "update-status"}
        for d in (cfg["UPDATE_REQUEST_DIR"], cfg["UPDATE_STATUS_DIR"]):
            d.mkdir(parents=True, exist_ok=True)
        (cfg["UPDATE_STATUS_DIR"] / "available.json").write_text(
            '{"behind": 2, "ahead": 0, "error": "", "checked": %d}' % (time.time() - 3 * 3600))
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


def _passphrase(a, confirm):
    if a.passphrase_file:
        return Path(a.passphrase_file).read_text().rstrip("\n")
    pw = getpass.getpass("Backup passphrase: ")
    if confirm and pw != getpass.getpass("Repeat: "):
        sys.exit("Passphrases differ.")
    return pw


def _backup_command(a):
    from . import backup
    from .config import ROOT, Config, inventory_dir
    inv = inventory_dir()
    try:
        if a.cmd == "backup":
            out = Path(a.out).resolve()
            if out.exists():
                return f"{out} already exists. Choose another file name."
            head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            example = (ROOT / "inventories" / "example").resolve()
            blob = backup.create(Config.DB_PATH, inv if inv.resolve() != example else "/nonexistent", _passphrase(a, True), head)
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            print(f"Wrote {out} ({len(blob)} bytes). Keep the passphrase: it cannot be recovered.")
            return 0
        blob = Path(a.file).read_bytes()
        pw = _passphrase(a, False)
        manifest, files = backup.read(blob, pw)  # checks everything before anything is replaced
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.get("created", 0)))
        print(f"Backup from {when}, {len(files)} file(s), controller commit {manifest.get('commit', '')[:12] or 'unknown'}.")
        if not a.yes and input("Type restore to replace this panel's data with it: ").strip() != "restore":
            return "Stopped. Nothing was changed."
        info = backup.restore(blob, pw, Config.DB_PATH, Path(os.environ.get("PANEL_INVENTORY") or ROOT / "inventories" / "local"))
        print(f"Restored {info['files']} file(s). The previous data was kept as: " + (", ".join(info["kept"]) or "(none)"))
        print("Everyone is signed out. Restart the panel to load the data: sudo systemctl restart provisionkit-panel")
        return 0
    except (backup.BackupError, OSError) as e:
        return f"Error: {e}"


def serve(app, host, port):
    try:
        from waitress import serve as wserve
    except ImportError:
        app.run(host=host, port=port)
    else:
        wserve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
