"""The Update button, panel side. Run: python -m pytest tests/test_update_panel.py"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import jobs, updater  # noqa: E402
from panel.db import connect  # noqa: E402
from test_panel import app, as_user, post  # noqa: E402,F401


@pytest.fixture()
def cfg(tmp_path):
    req, st = tmp_path / "requests", tmp_path / "status"
    req.mkdir(mode=0o700)
    st.mkdir()
    return {"UPDATE_REQUEST_DIR": req, "UPDATE_STATUS_DIR": st, "ROOT": ROOT}


@pytest.fixture()
def wired(app, cfg):
    app.config.update(cfg, DEMO=False)  # the shared fixture is a demo app, whose simulated updater would eat the request
    return app


def write_status(cfg, **fields):
    (cfg["UPDATE_STATUS_DIR"] / "status.json").write_text(json.dumps(fields))


# ---- reading what the updater wrote --------------------------------------------------------------------------------

def test_no_files_means_idle(cfg):
    st = updater.status(cfg)
    assert st["state"] == "idle" and st["message"] == "" and st["started"] is None
    assert updater.available(cfg) == {"behind": None, "checked": None, "error": ""}
    assert updater.log_tail(cfg) == ""


@pytest.mark.parametrize("junk", ["", "not json", "[1,2]", "null", '"text"', "{", "\x00\x01"])
def test_garbage_in_the_status_file_is_idle_not_a_crash(cfg, junk):
    (cfg["UPDATE_STATUS_DIR"] / "status.json").write_text(junk)
    (cfg["UPDATE_STATUS_DIR"] / "available.json").write_text(junk)
    assert updater.status(cfg)["state"] == "idle"
    assert updater.available(cfg)["behind"] is None


def test_known_states_and_fields_are_passed_through(cfg):
    write_status(cfg, state="success", step="Finished", message="Updated to abc12345.", started=100.0, updated=160.0,
                 finished=160.0, to="a" * 40, **{"from": "b" * 40}, rolled_back=False, commits=3)
    st = updater.status(cfg, now=200)
    assert st == {"state": "success", "step": "Finished", "message": "Updated to abc12345.", "started": 100.0,
                  "updated": 160.0, "finished": 160.0, "from": "b" * 40, "to": "a" * 40, "rolled_back": False,
                  "commits": 3}


def test_unknown_states_and_extra_keys_are_dropped(cfg):
    write_status(cfg, state="<script>", step="x", evil={"a": 1}, rolled_back="yes", commits="lots")
    st = updater.status(cfg)
    assert st["state"] == "idle" and "evil" not in st and st["rolled_back"] is False and st["commits"] == 0


def test_long_text_is_truncated_and_wrong_types_are_ignored(cfg):
    write_status(cfg, state="failed", message="m" * 5000, step=["a"], started="soon", **{"from": "f" * 500})
    st = updater.status(cfg)
    assert len(st["message"]) == 300 and st["step"] == "" and st["started"] is None and len(st["from"]) == 40


def test_a_running_status_nobody_touches_becomes_stale(cfg):
    write_status(cfg, state="running", step="Pulling", updated=1000.0)
    assert updater.status(cfg, now=1000 + 60)["state"] == "running"
    assert updater.status(cfg, now=1000 + updater.STALE_S + 1)["state"] == "stale"


def test_a_waiting_request_file_shows_as_requested(cfg):
    write_status(cfg, state="success", updated=1.0)
    (cfg["UPDATE_REQUEST_DIR"] / "update").write_text("")
    assert updater.status(cfg)["state"] == "requested"
    write_status(cfg, state="running", updated=time.time())
    assert updater.status(cfg)["state"] == "running"  # the updater has started: the request no longer matters


def test_available_parsing(cfg):
    (cfg["UPDATE_STATUS_DIR"] / "available.json").write_text(json.dumps({"behind": 2, "checked": 5.5, "error": ""}))
    assert updater.available(cfg) == {"behind": 2, "checked": 5.5, "error": ""}
    (cfg["UPDATE_STATUS_DIR"] / "available.json").write_text(json.dumps({"behind": -4, "error": "e" * 999}))
    a = updater.available(cfg)
    assert a["behind"] is None and len(a["error"]) == 300


def test_the_log_tail_is_bounded(cfg):
    (cfg["UPDATE_STATUS_DIR"] / "update.log").write_text("\n".join(f"line {i}" for i in range(500)))
    tail = updater.log_tail(cfg, lines=10).splitlines()
    assert tail == [f"line {i}" for i in range(490, 500)]
    (cfg["UPDATE_STATUS_DIR"] / "update.log").write_bytes(b"x" * 500_000)
    assert len(updater.log_tail(cfg, limit=1000)) <= 1000


# ---- asking for an update ------------------------------------------------------------------------------------------

def test_a_request_is_one_empty_private_file(cfg):
    updater.request(cfg)
    f = cfg["UPDATE_REQUEST_DIR"] / "update"
    assert f.read_text() == "" and oct(f.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in cfg["UPDATE_REQUEST_DIR"].iterdir()] == ["update"]


def test_a_second_request_cannot_queue_another_run(cfg):
    updater.request(cfg)
    with pytest.raises(updater.UpdateError, match="already"):
        updater.request(cfg)


def test_a_running_update_blocks_a_new_request(cfg):
    write_status(cfg, state="running", updated=time.time())
    with pytest.raises(updater.UpdateError, match="already running"):
        updater.request(cfg)
    assert not (cfg["UPDATE_REQUEST_DIR"] / "update").exists()


def test_a_planted_symlink_is_never_followed(cfg, tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep me")
    os.symlink(target, cfg["UPDATE_REQUEST_DIR"] / "update")
    with pytest.raises(updater.UpdateError):
        updater.request(cfg)
    assert target.read_text() == "keep me"


def test_without_the_installer_nothing_happens(tmp_path):
    missing = {"UPDATE_REQUEST_DIR": tmp_path / "nope", "UPDATE_STATUS_DIR": tmp_path / "nope2"}
    assert not updater.configured(missing)
    with pytest.raises(updater.UpdateError, match="not set up"):
        updater.request(missing)
    assert not (tmp_path / "nope").exists()  # it does not create the directories on its own


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_read_only_request_directory_counts_as_not_configured(cfg):
    cfg["UPDATE_REQUEST_DIR"].chmod(0o500)
    assert not updater.configured(cfg)


# ---- the endpoints -------------------------------------------------------------------------------------------------

def test_the_info_endpoint_has_the_documented_shape_and_needs_admin(wired):
    admin = as_user(wired, "alice")
    d = admin.get("/update").get_json()
    assert set(d) == {"version", "available", "status", "log", "can_start", "reason", "now"}
    assert d["can_start"] is True and d["status"]["state"] == "idle" and d["version"]["short"]
    assert as_user(wired, "olga").get("/update").status_code == 403
    assert as_user(wired, "bob").get("/update").status_code == 403
    assert wired.test_client().get("/update").status_code == 302


def test_start_needs_admin_and_a_csrf_token(wired, cfg):
    assert post(as_user(wired, "olga"), "/update/start").status_code == 403
    assert post(as_user(wired, "bob"), "/update/start").status_code == 403
    assert as_user(wired, "alice").post("/update/start").status_code == 400
    assert not (cfg["UPDATE_REQUEST_DIR"] / "update").exists()


def test_start_creates_the_request_and_audits_it(wired, cfg):
    c = as_user(wired, "alice")
    r = post(c, "/update/start")
    assert r.status_code == 202 and r.get_json()["ok"] is True and r.get_json()["requested_at"] > 0
    assert (cfg["UPDATE_REQUEST_DIR"] / "update").exists()
    row = connect(wired.config["DB_PATH"]).execute("SELECT * FROM audit WHERE action='update.request'").fetchone()
    assert row["user"] == "alice" and row["detail"].startswith("from ")
    d = c.get("/update").get_json()
    assert d["status"]["state"] == "requested" and d["can_start"] is False and "in progress" in d["reason"]


def test_a_second_click_is_refused_with_a_reason(wired):
    c = as_user(wired, "alice")
    assert post(c, "/update/start").status_code == 202
    r = post(c, "/update/start")
    assert r.status_code == 409 and r.get_json()["ok"] is False and "already" in r.get_json()["error"]


def test_start_is_refused_while_a_panel_job_runs(wired, cfg):
    assert jobs._lock.acquire(blocking=False)
    try:
        r = post(as_user(wired, "alice"), "/update/start")
        assert r.status_code == 409 and "job is running" in r.get_json()["error"]
        assert as_user(wired, "alice").get("/update").get_json()["can_start"] is False
    finally:
        jobs._lock.release()
    assert not (cfg["UPDATE_REQUEST_DIR"] / "update").exists()


def test_start_when_not_installed_explains_what_to_run(wired, tmp_path):
    wired.config.update(UPDATE_REQUEST_DIR=tmp_path / "x", UPDATE_STATUS_DIR=tmp_path / "y")
    r = post(as_user(wired, "alice"), "/update/start")
    assert r.status_code == 409 and "install_panel.sh" in r.get_json()["error"]


def test_hostile_status_text_is_data_and_the_script_never_uses_innerhtml(wired, cfg):
    write_status(cfg, state="failed", message="<img src=x onerror=alert(1)>", step="<script>alert(1)</script>",
                 updated=time.time())
    d = as_user(wired, "alice").get("/update").get_json()
    assert d["status"]["message"] == "<img src=x onerror=alert(1)>"  # untouched data, shown with textContent
    js = (ROOT / "panel/static/app.js").read_text()
    update_js = js[js.index("// Update button."):]
    assert "innerHTML" not in update_js and "insertAdjacentHTML" not in update_js and "eval(" not in update_js


# ---- the button ----------------------------------------------------------------------------------------------------

def test_only_admins_see_the_button(wired):
    for who, expected in (("alice", True), ("olga", False), ("bob", False)):
        html = as_user(wired, who).get("/").get_data(as_text=True)
        assert ("update-open" in html) is expected and ("update-dialog" in html) is expected, who


def test_the_page_stays_csp_clean(wired):
    html = as_user(wired, "alice").get("/").get_data(as_text=True)
    assert " onclick=" not in html and "<script>" not in html and 'style="' not in html
    assert 'name="csrf-token"' in html


# ---- demo mode plays the updater's part ----------------------------------------------------------------------------

def test_demo_mode_runs_a_simulated_update_to_success(wired, cfg, monkeypatch):
    monkeypatch.setattr(updater, "STEP_DELAY", 0.01)
    wired.config["DEMO"] = True
    (cfg["UPDATE_STATUS_DIR"] / "available.json").write_text(json.dumps({"behind": 2, "checked": 1.0, "error": ""}))
    c = as_user(wired, "alice")
    assert post(c, "/update/start").status_code == 202
    for _ in range(100):
        if updater.status(cfg)["state"] == "success":
            break
        time.sleep(0.05)
    st = updater.status(cfg)
    assert st["state"] == "success" and st["commits"] == 2 and not (cfg["UPDATE_REQUEST_DIR"] / "update").exists()
    assert updater.available(cfg)["behind"] == 0 and "Installing dependencies" in updater.log_tail(cfg)


# ---- the two halves agree: the real CLI writes, the panel reads ---------------------------------------------------

def test_what_the_cli_writes_is_what_the_panel_understands(tmp_path, monkeypatch):
    from test_cli import cli, git, push  # noqa: F401  (real git repositories, the loaded script)
    import test_cli
    for k, v in test_cli.GIT_ENV.items():
        monkeypatch.setenv(k, v)
    bare, pusher, mine = tmp_path / "gh.git", tmp_path / "pusher", tmp_path / "ctl"
    test_cli.git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    test_cli.git(tmp_path, "clone", "-q", str(bare), str(pusher))
    test_cli.commit(pusher, "README.md", "one", "first")
    test_cli.git(pusher, "push", "-q", "origin", "main")
    test_cli.git(tmp_path, "clone", "-q", str(bare), str(mine))
    kit = cli.Kit(mine, out=lambda *_: None)
    kit.check = lambda: []
    test_cli.commit(pusher, "roles/new.yml", "x", "a new role")
    test_cli.git(pusher, "push", "-q", "origin", "main")
    status_dir, req = tmp_path / "status", tmp_path / "req"
    req.mkdir()
    cfg = {"UPDATE_STATUS_DIR": status_dir, "UPDATE_REQUEST_DIR": req}

    assert cli.main(["update", "--skip-env", "--skip-service", "--status-dir", str(status_dir)], kit=kit) == 0
    st = updater.status(cfg)
    assert st["state"] == "success" and st["step"] == "Finished" and st["commits"] == 1
    assert st["message"] == f"Updated to {kit.head()[:8]}." and st["to"] == kit.head() and len(st["from"]) == 40
    assert st["started"] and st["finished"] and st["started"] <= st["finished"] and st["rolled_back"] is False

    cli.main(["check-updates", "--status-dir", str(status_dir)], kit=kit)
    assert updater.available(cfg)["behind"] == 0 and updater.available(cfg)["error"] == ""

    (mine / "README.md").write_text("dirty")  # a failure the panel must show verbatim
    assert cli.main(["update", "--skip-env", "--skip-service", "--status-dir", str(status_dir)], kit=kit) == 1
    st = updater.status(cfg)
    assert st["state"] == "failed" and "local changes" in st["message"]
