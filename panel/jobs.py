"""Run allow-listed read-only playbooks, keep their logs, and ingest the results."""
import json
import os
import re
import subprocess
import sys
import threading
import time

from .config import ROOT
from .db import audit, connect, save_snapshot

# Only these playbooks can be started from the panel. All of them are read-only on the targets.
KINDS = {
    "collect": {"playbook": "collect.yml", "label": "Collect facts and posture", "scope": "all"},
    "validate": {"playbook": "validate.yml", "label": "Validate baseline (acceptance checks)", "scope": "workload_nodes"},
    "preflight": {"playbook": "preflight.yml", "label": "Preflight platform checks", "scope": "all"},
}
RECAP = re.compile(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)
FATAL = re.compile(r"^fatal: \[(\S+?)\]: (FAILED|UNREACHABLE)! => ", re.M)
MSG = re.compile(r'"msg":\s*"((?:[^"\\]|\\.)*)"')
TASK = re.compile(r"^TASK \[(.*?)\]", re.M)
TIMEOUT_S = 30 * 60

_lock = threading.Lock()


class JobError(ValueError):
    pass


def start(app, kind, target, user, ip, inv):
    if kind not in KINDS:
        raise JobError("Unknown job type.")
    names = set(inv.hosts()) | set(inv.groups())
    if target != "all" and target not in names:
        raise JobError("Unknown target.")
    if not _lock.acquire(blocking=False):
        raise JobError("Another job is running. Wait for it to finish.")
    db = connect(app.config["DB_PATH"])
    try:
        cur = db.execute("INSERT INTO jobs (kind, target, user, status, created) VALUES (?,?,?,?,?)",
                         (kind, target, user, "queued", time.time()))
        job_id = cur.lastrowid
        db.commit()
        audit(db, user, "job.start", f"#{job_id}", f"{kind} on {target}", ip)
    except Exception:
        _lock.release()
        raise
    finally:
        db.close()
    threading.Thread(target=_run, args=(app.config, job_id, kind, target, str(inv.dir)), daemon=True).start()
    return job_id


def log_path(config, job_id):
    return config["JOB_LOG_DIR"] / f"{int(job_id)}.log"


def _command(config, kind, target, inv_dir):
    if config["DEMO"]:
        return [sys.executable, "-c", _DEMO_SCRIPT, kind, target]
    cmd = ["ansible-playbook", str(ROOT / "playbooks" / KINDS[kind]["playbook"]), "-i", f"{inv_dir}/hosts.yml"]
    if target != "all":
        cmd += ["--limit", target]
    if kind == "collect":
        cmd += ["-e", f"panel_out_dir={config['SNAPSHOT_DIR']}"]
    return cmd


def _run(config, job_id, kind, target, inv_dir):
    db = connect(config["DB_PATH"])
    path = log_path(config, job_id)
    started = time.time()
    try:
        db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (started, job_id))
        db.commit()
        env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0", PYTHONUNBUFFERED="1")
        try:
            with open(path, "w") as log:
                proc = subprocess.Popen(_command(config, kind, target, inv_dir), cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
                try:
                    code = proc.wait(timeout=TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    log.write("\n[panel] killed after timeout\n")
                    code = -9
        except OSError as e:
            path.write_text(f"[panel] could not start ansible-playbook: {e}\n")
            code = 127
        text = path.read_text(errors="replace")
        _ingest(db, config, job_id, kind, text, started)
        status = "success" if code == 0 else "failed"
        db.execute("UPDATE jobs SET status=?, finished=?, exit_code=? WHERE id=?", (status, time.time(), code, job_id))
        db.commit()
    except Exception as e:  # keep the lock from leaking on any bug
        db.execute("UPDATE jobs SET status='failed', finished=? WHERE id=?", (time.time(), job_id))
        db.commit()
        audit(db, "panel", "job.error", f"#{job_id}", repr(e)[:300])
    finally:
        db.close()
        _lock.release()


def parse_log(text):
    """Return {host: {ok, failed, unreachable, message}} from ansible-playbook output."""
    hosts = {}
    for m in RECAP.finditer(text):
        hosts[m.group(1)] = {"ok": int(m.group(2)), "unreachable": int(m.group(4)), "failed": int(m.group(5)),
                             "message": ""}
    for m in FATAL.finditer(text):
        host = m.group(1)
        if host not in hosts:
            continue
        tail = text[m.end(): m.end() + 1500]
        msg = MSG.search(tail)
        task = TASK.findall(text[: m.start()])
        label = f"{task[-1]}: " if task else ""
        hosts[host]["message"] = (label + (json.loads(f'"{msg.group(1)}"') if msg else m.group(2).lower()))[:400]
    return hosts


def _ingest(db, config, job_id, kind, text, started):
    results = parse_log(text)
    now = time.time()
    for host, r in results.items():
        db.execute("INSERT OR REPLACE INTO job_hosts VALUES (?,?,?,?,?,?)",
                   (job_id, host, r["ok"], r["failed"], r["unreachable"], r["message"]))
        # A failed collection means the panel has no fresh data, so treat the host as down until the next success.
        down = r["unreachable"] or (kind == "collect" and r["failed"])
        db.execute("INSERT OR REPLACE INTO host_status VALUES (?,?,?,?)",
                   (host, 0 if down else 1, now, r["message"] if down else ""))
    if config["DEMO"] and kind == "collect":
        db.execute("UPDATE snapshots SET collected=?", (now,))
        db.execute("UPDATE host_status SET reachable=1, detail=''")
    elif kind == "collect":
        for f in config["SNAPSHOT_DIR"].glob("*.json"):
            if f.stat().st_mtime < started - 1:
                continue
            try:
                snap = json.loads(f.read_text())
                save_snapshot(db, snap["host"], snap)
            except (ValueError, KeyError):
                continue  # half-written or foreign file; the next collect will replace it
    db.commit()


# Simulated run for PANEL_DEMO=1 so the UI can be shown without a lab.
_DEMO_SCRIPT = r"""
import sys, time
kind, target = sys.argv[1:3]
print(f"PLAY [demo {kind}] " + "*" * 40)
for t in ("Gathering Facts", "Read effective sshd settings", "Assert baseline state"):
    time.sleep(0.6); print(f"\nTASK [{t}] " + "*" * 30); print(f"ok: [{target}]")
print("\nPLAY RECAP " + "*" * 40)
print(f"{target:<20}: ok=3    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0")
"""
