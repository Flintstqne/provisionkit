"""Guided setup and host key trust. Run: python -m pytest tests/test_setup.py"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import setup  # noqa: E402
from test_panel import app, as_user, post  # noqa: E402,F401  (the app fixture and helpers)

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
HAVE_SSH = all(shutil.which(t) for t in ("ssh-keyscan", "ssh-keygen")) and os.path.exists(SSHD) and \
    os.path.isdir("/run/sshd")
needs_ssh = pytest.mark.skipif(not HAVE_SSH, reason="needs openssh-client and openssh-server (sshd, /run/sshd)")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def sshd(tmp_path, monkeypatch):
    key = tmp_path / "host_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    port = free_port()
    cfg = tmp_path / "sshd_config"
    cfg.write_text(f"HostKey {key}\nListenAddress 127.0.0.1\nPidFile none\nUsePAM no\nPort {port}\n")
    proc = subprocess.Popen([SSHD, "-D", "-f", str(cfg)], stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    kh = tmp_path / "known_hosts"
    monkeypatch.setenv("PANEL_KNOWN_HOSTS", str(kh))
    fp = subprocess.run(["ssh-keygen", "-lf", f"{key}.pub"], capture_output=True, text=True).stdout.split()[1]
    yield {"port": port, "fp": fp, "kh": kh}
    proc.terminate()
    proc.wait(timeout=5)


@needs_ssh
def test_scan_returns_the_servers_real_fingerprint(sshd):
    line, kind, fp = setup.scan_host_key("127.0.0.1", sshd["port"])
    assert kind == "ed25519" and fp == sshd["fp"] and line.startswith(f"[127.0.0.1]:{sshd['port']} ssh-ed25519 ")


@needs_ssh
def test_trust_writes_known_hosts_once_with_private_mode(sshd):
    assert not setup.is_trusted("127.0.0.1", sshd["port"])
    assert setup.trust_host_key("127.0.0.1", sshd["port"], sshd["fp"]) is True
    assert setup.is_trusted("127.0.0.1", sshd["port"])
    assert oct(sshd["kh"].stat().st_mode & 0o777) == "0o600"
    assert setup.trust_host_key("127.0.0.1", sshd["port"], sshd["fp"]) is False
    assert sshd["kh"].read_text().count("ssh-ed25519") == 1


@needs_ssh
def test_trust_refuses_a_fingerprint_that_was_not_reviewed(sshd):
    with pytest.raises(setup.SetupError, match="changed since you reviewed"):
        setup.trust_host_key("127.0.0.1", sshd["port"], "SHA256:" + "A" * 43)
    assert not sshd["kh"].exists() or sshd["kh"].read_text() == ""


@needs_ssh
def test_unreachable_server_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_KNOWN_HOSTS", str(tmp_path / "kh"))
    with pytest.raises(setup.SetupError, match="Could not read a host key"):
        setup.scan_host_key("127.0.0.1", free_port())


def test_address_cannot_inject_options():
    with pytest.raises(setup.SetupError, match="Invalid address"):
        setup.scan_host_key("-oProxyCommand=evil", 22)


def dev(snapshot=False):
    return {"snapshot": snapshot}


def row(message="", unreachable=0, failed=0):
    return {"message": message, "unreachable": unreachable, "failed": failed}


def states(**kw):
    return {s["key"]: s["state"] for s in setup.steps(**kw)}


def test_steps_for_a_brand_new_device():
    assert states(device=dev(), trusted=False, last_collect=None, bootstrap_cmd="x") == \
        {"registered": "done", "hostkey": "todo", "access": "blocked", "data": "blocked"}


def test_steps_after_trust_wait_for_a_collection():
    assert states(device=dev(), trusted=True, last_collect=None, bootstrap_cmd="x") == \
        {"registered": "done", "hostkey": "done", "access": "todo", "data": "blocked"}


def test_permission_denied_points_at_bootstrap_with_the_command():
    out = setup.steps(dev(), True, row("Gathering Facts: provisionkit@10.0.0.9: Permission denied (publickey)."),
                      "THE COMMAND")
    access = next(s for s in out if s["key"] == "access")
    assert access["state"] == "todo" and access["command"] == "THE COMMAND" and "bootstrap" in access["detail"]


def test_other_failures_show_their_message():
    out = setup.steps(dev(), True, row("Connection timed out", unreachable=1), "x")
    access = next(s for s in out if s["key"] == "access")
    assert access["state"] == "failed" and "timed out" in access["detail"] and access["command"] == ""


def test_steps_all_done_once_data_exists():
    assert set(states(device=dev(True), trusted=True, last_collect=None, bootstrap_cmd="x").values()) == {"done"}


def test_bootstrap_command_is_shell_safe(tmp_path):
    cmd = setup.bootstrap_command("/home/p/provisionkit", tmp_path, "pk-new")
    assert "--limit pk-new" in cmd and "--ask-pass --ask-become-pass" in cmd and cmd.startswith("sudo -u ")


@pytest.fixture()
def fake_ssh(monkeypatch):
    calls = {}
    monkeypatch.setattr(setup, "scan_host_key", lambda a, p=22: ("line", "ed25519", "SHA256:" + "B" * 43))
    monkeypatch.setattr(setup, "is_trusted", lambda a, p=22: False)

    def trust(a, p, fp):
        calls["trusted"] = (a, p, fp)
        if fp != "SHA256:" + "B" * 43:
            raise setup.SetupError("The host key changed since you reviewed it.")
        return True
    monkeypatch.setattr(setup, "trust_host_key", trust)
    return calls


def test_device_page_shows_the_checklist(app, fake_ssh):
    html = as_user(app, "alice").get("/devices/pk-server").get_data(as_text=True)
    assert "Set up this device" in html and "Review host key" in html


def test_review_page_shows_fingerprint_and_needs_admin(app, fake_ssh):
    r = post(as_user(app, "alice"), "/devices/pk-server/hostkey")
    assert r.status_code == 200 and b"SHA256:" + b"B" * 43 in r.data and b"ssh-keygen -lf" in r.data
    assert post(as_user(app, "olga"), "/devices/pk-server/hostkey").status_code == 403
    assert post(as_user(app, "bob"), "/devices/pk-server/hostkey").status_code == 403


def test_trust_needs_the_confirmation_checkbox(app, fake_ssh):
    c = as_user(app, "alice")
    r = post(c, "/devices/pk-server/hostkey/trust", fingerprint="SHA256:" + "B" * 43, follow_redirects=True)
    assert b"Confirm that you checked" in r.data and "trusted" not in fake_ssh


def test_trust_with_collect_starts_a_job_and_audits(app, fake_ssh):
    from panel import jobs
    c = as_user(app, "alice")
    r = post(c, "/devices/pk-server/hostkey/trust", fingerprint="SHA256:" + "B" * 43, verified="yes", collect="yes")
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    assert fake_ssh["trusted"][0] == "192.0.2.20"
    for _ in range(60):
        if jobs._lock.acquire(blocking=False):
            jobs._lock.release()
            break
        time.sleep(0.25)
    assert b"hostkey.trust" in as_user(app, "alice").get("/audit").data


def test_trust_with_a_stale_fingerprint_is_rejected(app, fake_ssh):
    r = post(as_user(app, "alice"), "/devices/pk-server/hostkey/trust", fingerprint="SHA256:" + "C" * 43, verified="yes",
             follow_redirects=True)
    assert b"changed since you reviewed" in r.data


def test_untrusted_key_blocks_later_steps_even_after_a_failed_collect():
    assert states(device=dev(), trusted=False, last_collect=row("Host key verification failed.", unreachable=1),
                  bootstrap_cmd="x")["access"] == "blocked"
