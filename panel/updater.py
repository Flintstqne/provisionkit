"""The panel side of the Update button.

The panel never updates anything itself. It runs as an unprivileged service in a read-only sandbox, so it cannot pull
code, install packages or restart services. It does two things:

* leave an empty request file in a directory it owns, which a systemd path unit notices and answers by running
  `provisionkit update` as root, and
* read the progress files that update writes into a directory only root can write.

Everything read from those files is treated as untrusted text: known keys only, bounded lengths, never evaluated.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

REQUEST_NAME = "update"
STALE_S = 50 * 60  # a "running" status that has not been touched for this long is not running any more
STATES = ("running", "success", "failed")
STEP_DELAY = 1.0  # demo mode only
_version = {"at": 0.0, "root": None, "value": {}}


class UpdateError(Exception):
    pass


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(errors="replace")[:100_000])
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(v, n=300):
    return str(v)[:n] if isinstance(v, (str, int, float)) else ""


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def request_path(cfg):
    return Path(cfg["UPDATE_REQUEST_DIR"]) / REQUEST_NAME


def configured(cfg):
    """True when the installer has created both directories and the panel may write to the request directory."""
    req, status = Path(cfg["UPDATE_REQUEST_DIR"]), Path(cfg["UPDATE_STATUS_DIR"])
    return req.is_dir() and os.access(req, os.W_OK) and status.is_dir()


def status(cfg, now=None):
    """What the updater last reported, reduced to known keys. State is one of idle, requested, running, success,
    failed or stale."""
    now = now or time.time()
    raw = _read_json(Path(cfg["UPDATE_STATUS_DIR"]) / "status.json")
    state = raw.get("state") if raw.get("state") in STATES else "idle"
    updated = _num(raw.get("updated")) or 0
    if state == "running" and now - updated > STALE_S:
        state = "stale"
    if os.path.lexists(request_path(cfg)) and state != "running":
        state = "requested"
    return {"state": state, "step": _text(raw.get("step")), "message": _text(raw.get("message")),
            "started": _num(raw.get("started")), "updated": updated or None, "finished": _num(raw.get("finished")),
            "from": _text(raw.get("from"), 40), "to": _text(raw.get("to"), 40),
            "rolled_back": raw.get("rolled_back") is True, "commits": int(_num(raw.get("commits")) or 0)}


def available(cfg):
    raw = _read_json(Path(cfg["UPDATE_STATUS_DIR"]) / "available.json")
    behind = _num(raw.get("behind"))
    return {"behind": int(behind) if behind is not None and behind >= 0 else None, "checked": _num(raw.get("checked")),
            "error": _text(raw.get("error"))}


def log_tail(cfg, lines=80, limit=60_000):
    try:
        with open(Path(cfg["UPDATE_STATUS_DIR"]) / "update.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def version(root, ttl=15):
    """The commit this checkout is on: {head, short, date}. Cached briefly because every dialog open asks."""
    if _version["root"] == str(root) and time.time() - _version["at"] < ttl:
        return _version["value"]
    value = {}
    try:
        r = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H %cs"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
        head, _, date = r.stdout.strip().partition(" ")
        if r.returncode == 0 and len(head) == 40:
            value = {"head": head, "short": head[:8], "date": date}
    except (OSError, subprocess.TimeoutExpired):
        pass
    _version.update(at=time.time(), root=str(root), value=value)
    return value


def request(cfg):
    """Ask the updater to run. Creates one empty file with O_EXCL, so a second click cannot queue a second run and a
    symlink planted at that name cannot be followed."""
    if not configured(cfg):
        raise UpdateError("Updating from the panel is not set up on this machine. Run: sudo scripts/install_panel.sh")
    if status(cfg)["state"] in ("running", "requested"):
        raise UpdateError("An update is already running or waiting to start.")
    try:
        fd = os.open(request_path(cfg), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise UpdateError("An update is already waiting to start.") from None
    except OSError as e:
        raise UpdateError(f"Could not create the update request ({e.strerror}).") from None
    os.close(fd)
    if cfg.get("DEMO"):
        threading.Thread(target=_simulate, args=(dict(cfg),), daemon=True).start()


def _simulate(cfg):
    """Demo mode: play the updater's part, so the dialog can be shown without systemd or GitHub."""
    status_dir = Path(cfg["UPDATE_STATUS_DIR"])
    log = status_dir / "update.log"
    steps = [("Starting", ""), ("Fetching from GitHub", "Fetching origin/main ..."),
             ("Pulling 2 new commit(s)", "2 new commit(s):\n3f9c1ab Panel: show configuration drift\n0b7d2e4 Add provisionkit baseline"),
             ("Checking the Python environment", "Installing dependencies ..."),
             ("Restarting the panel if needed", "Restarting the panel service ...\nPanel service: restarted and healthy."),
             ("Running health checks", "  OK    git\n  OK    python environment\n  OK    panel service")]
    request_path(cfg).unlink(missing_ok=True)
    data, started = {}, time.time()

    def write(name, **fields):
        data.update(fields, updated=time.time())
        (status_dir / name).write_text(json.dumps(data))
    log.write_text("")
    write("status.json", state="running", step="Starting", started=started, finished=None, message="",
          rolled_back=False, commits=0, **{"from": "a" * 40, "to": None})
    for step, line in steps[1:]:
        time.sleep(STEP_DELAY)
        write("status.json", step=step)
        with open(log, "a") as f:
            f.write(line + "\n")
    time.sleep(STEP_DELAY)
    write("status.json", state="success", step="Finished", finished=time.time(), to="b" * 40, commits=2,
          message="Updated to bbbbbbbb.")
    (status_dir / "available.json").write_text(json.dumps({"checked": time.time(), "behind": 0, "ahead": 0, "error": ""}))
