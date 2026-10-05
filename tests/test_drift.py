"""Config drift: manifest commit versus the controller checkout. Run: python -m pytest tests/test_drift.py"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import drift, fleet  # noqa: E402
from panel.db import connect, save_snapshot  # noqa: E402
from test_panel import _snap, app, as_user  # noqa: E402,F401

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.org", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@e.org", "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, path, text="x"):
    f = Path(repo) / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text + str(time.time_ns()))
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"change {path}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    drift._cache.clear()
    drift._head.update(at=0.0, root=None, value=None)
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "README.md")
    return tmp_path


def manifest(commit_text):
    return {"schema_version": 1, "configuration_commit": commit_text}


def test_no_manifest(repo):
    r = drift.classify(None, repo)
    assert r["state"] == "none" and r["label"] == "No manifest"


@pytest.mark.parametrize("bad", ["", "abc123", "--output=/tmp/x", "../../etc/passwd", "g" * 40, "A" * 40, None, 123,
                                 "a" * 39, "a" * 41 + "x"])
def test_malformed_commits_are_unreadable_and_never_reach_git(repo, bad, monkeypatch):
    monkeypatch.setattr(drift, "_git", lambda *a: pytest.fail("git must not run for a malformed commit"))
    assert drift.classify({"configuration_commit": bad}, repo)["state"] == "invalid"
    assert drift.classify({}, repo)["state"] == "invalid"


def test_same_commit_is_current(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert drift.classify(manifest(head), repo)["state"] == "current"


def test_uncommitted_baseline_is_flagged(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert drift.classify(manifest(head + "-dirty"), repo)["state"] == "dirty"


def test_newer_commits_that_do_not_touch_roles_still_count_as_current(repo):
    old = commit(repo, "roles/base/tasks/main.yml")
    commit(repo, "panel/views.py")
    commit(repo, "docs/x.md")
    r = drift.classify(manifest(old), repo)
    assert r["state"] == "current" and r["behind"] == 2 and "none of which change roles" in r["detail"]


def test_a_role_change_since_the_baseline_means_behind(repo):
    old = commit(repo, "roles/base/tasks/main.yml")
    commit(repo, "roles/firewall/tasks/main.yml")
    commit(repo, "panel/views.py")
    r = drift.classify(manifest(old), repo)
    assert r["state"] == "behind" and r["behind"] == 2 and r["files"] == ["roles/firewall/tasks/main.yml"]


def test_the_baseline_playbook_counts_but_other_playbooks_do_not(repo):
    old = commit(repo, "playbooks/baseline.yml")
    commit(repo, "playbooks/validate.yml")
    assert drift.classify(manifest(old), repo)["state"] == "current"
    drift._cache.clear()
    drift._head.update(at=0.0)  # the controller's commit is cached for a few seconds
    commit(repo, "playbooks/baseline.yml")
    assert drift.classify(manifest(old), repo)["state"] == "behind"


def test_a_commit_the_checkout_does_not_have_is_unknown(repo):
    assert drift.classify(manifest("1" * 40), repo)["state"] == "unknown"


def test_a_commit_from_another_branch_is_unknown(repo):
    git(repo, "checkout", "-q", "-b", "side")
    other = commit(repo, "roles/side.yml")
    git(repo, "checkout", "-q", "main")
    commit(repo, "roles/main.yml")
    assert drift.classify(manifest(other), repo)["state"] == "unknown"


def test_no_git_repository_means_unknown(tmp_path):
    drift._head.update(at=0.0, root=None, value=None)
    assert drift.classify(manifest("1" * 40), tmp_path)["state"] == "unknown"


def test_describe_output_with_a_tag_prefix_is_understood(repo):
    old = commit(repo, "roles/a.yml")
    commit(repo, "roles/b.yml")
    assert drift.classify(manifest(f"v1.0-3-g{old}"), repo)["state"] == "behind"


def test_results_are_cached_per_commit_pair(repo, monkeypatch):
    old = commit(repo, "roles/a.yml")
    commit(repo, "roles/b.yml")
    drift.classify(manifest(old), repo)
    monkeypatch.setattr(drift, "_git", lambda *a: pytest.fail("a second lookup should come from the cache"))
    assert drift.classify(manifest(old), repo, head=git(repo, "rev-parse", "HEAD"))["state"] == "behind"


def test_head_is_cached_briefly(repo, monkeypatch):
    first = drift.controller_head(repo)
    commit(repo, "roles/c.yml")
    assert drift.controller_head(repo) == first  # inside the cache window
    assert drift.controller_head(repo, ttl=0) != first


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("   ", None), ("{not json", {}), ("[1, 2]", {}),
                                          ('{"a": 1}', {"a": 1})])
def test_manifest_parsing_never_raises(raw, expected):
    assert fleet.parse_manifest(raw) == expected


# ---- the pages ---------------------------------------------------------------------------------------------------

def put(app, host, raw):
    db = connect(app.config["DB_PATH"])
    snap = _snap(host)
    snap["manifest_raw"] = raw
    save_snapshot(db, host, snap)
    db.close()


def test_pages_show_drift_for_each_state(app, repo):
    old = commit(repo, "roles/a.yml")
    commit(repo, "roles/b.yml")
    head = git(repo, "rev-parse", "HEAD")
    app.config["ROOT"] = repo
    put(app, "pk-server", json.dumps(manifest(head)))
    put(app, "pk-worker", json.dumps(manifest(old)))
    c = as_user(app, "alice")
    api = {d["name"]: d["config"] for d in c.get("/api/v1/devices").get_json()}
    assert api["pk-server"] == "current" and api["pk-worker"] == "behind" and api["pk-control"] is None
    html = c.get("/devices").get_data(as_text=True)
    assert "Behind" in html and "Current" in html
    dash = c.get("/").get_data(as_text=True)
    assert "Config drift" in dash and "config behind" in dash
    card = c.get("/devices/pk-worker").get_data(as_text=True)
    assert "Baseline manifest" in card and "roles/b.yml" in card and "provisionkit baseline pk-worker" in card
    assert c.get("/api/v1/summary").get_json()["drift"] == {"current": 1, "behind": 1}


def test_a_node_without_a_manifest_and_a_damaged_manifest(app, repo):
    app.config["ROOT"] = repo
    put(app, "pk-server", "")
    put(app, "pk-worker", "{this is not json")
    api = {d["name"]: d["config"] for d in as_user(app, "alice").get("/api/v1/devices").get_json()}
    assert api["pk-server"] == "none" and api["pk-worker"] == "invalid"
    html = as_user(app, "alice").get("/devices/pk-worker").get_data(as_text=True)
    assert "Unreadable" in html


def test_hostile_manifest_text_is_escaped(app, repo):
    app.config["ROOT"] = repo
    head = git(repo, "rev-parse", "HEAD")
    m = {"configuration_commit": head, "baseline_release": "<script>alert(1)</script>",
         "expected_services": ["<img src=x onerror=alert(1)>"]}
    put(app, "pk-server", json.dumps(m))
    html = as_user(app, "alice").get("/devices/pk-server").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
