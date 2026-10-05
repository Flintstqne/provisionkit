"""provisionkit CLI tests, against real git repositories. Run: python -m pytest tests/test_cli.py"""
import http.server
import importlib.machinery
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("pkcli", str(ROOT / "scripts" / "provisionkit"))
spec = importlib.util.spec_from_loader("pkcli", loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def commit(repo, name, text="x", msg=None):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg or f"change {name}")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A bare 'GitHub', a pusher clone, and the controller's checkout, all on main."""
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    bare, pusher, mine = tmp_path / "github.git", tmp_path / "pusher", tmp_path / "controller"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(pusher))
    commit(pusher, "README.md", "one", "first")
    commit(pusher, "panel/requirements.txt", "flask\n", "requirements")
    (pusher / ".gitignore").write_text("inventories/local/\n.venv/\n")
    git(pusher, "add", ".gitignore")
    git(pusher, "commit", "-q", "-m", "ignore")
    git(pusher, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(bare), str(mine))
    lines = []
    kit = cli.Kit(mine, out=lines.append)
    kit.check = lambda: []  # the health checks have their own tests below
    return type("W", (), {"bare": bare, "pusher": pusher, "mine": mine, "kit": kit, "out": lines})


def push(w, name, text="new", msg=None):
    commit(w.pusher, name, text, msg)
    git(w.pusher, "push", "-q", "origin", "main")


def test_already_up_to_date(world):
    world.kit.update(skip_env=True, skip_service=True)
    assert any("Already up to date" in ln for ln in world.out)


def test_pulls_new_commits_and_lists_them(world):
    push(world, "roles/new.yml", msg="add a role")
    before = world.kit.head()
    world.kit.update(skip_env=True, skip_service=True)
    assert world.kit.head() != before and (world.mine / "roles/new.yml").exists()
    assert any("add a role" in ln for ln in world.out)


def test_untracked_and_ignored_files_do_not_block(world):
    (world.mine / "inventories/local").mkdir(parents=True)
    (world.mine / "inventories/local/hosts.yml").write_text("real")
    (world.mine / "notes.txt").write_text("mine")
    push(world, "a.txt")
    world.kit.update(skip_env=True, skip_service=True)
    assert (world.mine / "a.txt").exists() and (world.mine / "inventories/local/hosts.yml").read_text() == "real"


def test_local_changes_to_tracked_files_block_and_nothing_moves(world):
    (world.mine / "README.md").write_text("edited")
    push(world, "a.txt")
    before = world.kit.head()
    with pytest.raises(cli.Failure, match="local changes"):
        world.kit.update(skip_env=True, skip_service=True)
    assert world.kit.head() == before and (world.mine / "README.md").read_text() == "edited"


def test_wrong_branch_is_refused(world):
    git(world.mine, "checkout", "-q", "-b", "other")
    with pytest.raises(cli.Failure, match="not 'main'"):
        world.kit.update(skip_env=True, skip_service=True)


def test_diverged_history_is_refused_and_left_alone(world):
    commit(world.mine, "local.txt", "mine", "local work")
    push(world, "remote.txt")
    before = world.kit.head()
    with pytest.raises(cli.Failure, match="diverged"):
        world.kit.update(skip_env=True, skip_service=True)
    assert world.kit.head() == before


def test_local_commits_only_means_nothing_to_pull(world):
    commit(world.mine, "local.txt", "mine", "local work")
    world.kit.update(skip_env=True, skip_service=True)
    assert any("Nothing to pull" in ln for ln in world.out)


def test_unreachable_github_is_a_clear_error(world):
    git(world.mine, "remote", "set-url", "origin", str(world.bare) + ".gone")
    with pytest.raises(cli.Failure, match="Could not reach GitHub"):
        world.kit.update(skip_env=True, skip_service=True)


def test_requirements_change_triggers_a_dependency_install(world, monkeypatch):
    seen = []
    monkeypatch.setattr(world.kit, "ensure_env", lambda changed=False: seen.append(changed))
    monkeypatch.setattr(world.kit, "ensure_service", lambda changed, force=False: None)
    push(world, "panel/requirements.txt", "flask\nnew-dep\n")
    world.kit.update()
    push(world, "docs/x.md")
    world.kit.update()
    assert seen == [True, False]


def service_kit(world, monkeypatch, *, active=True, current=True, installed=True):
    kit = world.kit
    calls = []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kit, "service_installed", lambda: installed)
    monkeypatch.setattr(kit, "service_active", lambda: active)
    monkeypatch.setattr(kit, "unit_is_current", lambda port: current)
    monkeypatch.setattr(kit, "panel_port", lambda: 8080)
    monkeypatch.setattr(kit, "sh", lambda cmd, **kw: calls.append(list(map(str, cmd))) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    return kit, calls


def test_service_restart_decisions(world, monkeypatch):
    kit, calls = service_kit(world, monkeypatch, installed=False)
    kit.ensure_service(set())
    assert calls == []
    kit, calls = service_kit(world, monkeypatch)
    kit.ensure_service({"docs/readme.md"})
    assert calls == [] and any("no restart needed" in ln for ln in world.out)
    for changed in ({"panel/views.py"}, {"scripts/install_panel.sh"}):
        kit, calls = service_kit(world, monkeypatch)
        kit.ensure_service(changed)
        assert len(calls) == 1 and "install_panel.sh" in calls[0][1]
    kit, calls = service_kit(world, monkeypatch, current=False)
    kit.ensure_service(set())
    assert len(calls) == 1
    kit, calls = service_kit(world, monkeypatch, active=False)
    kit.ensure_service(set())
    assert len(calls) == 1 and "--skip-admin" in calls[0]


def test_service_failure_includes_the_log_tail(world, monkeypatch):
    kit, _ = service_kit(world, monkeypatch, active=False)
    monkeypatch.setattr(kit, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom: bad config"))
    with pytest.raises(cli.Failure, match="(?s)did not come back healthy.*boom"):
        kit.ensure_service(set())


def test_service_needs_root(world, monkeypatch):
    kit, _ = service_kit(world, monkeypatch, active=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    with pytest.raises(cli.Failure, match="needs root"):
        kit.ensure_service(set())


def test_failed_service_rolls_the_code_back(world, monkeypatch):
    kit = world.kit
    monkeypatch.setattr(kit, "ensure_env", lambda changed=False: None)
    attempts = []

    def service(changed, force=False):
        attempts.append(force)
        if not force:
            raise cli.Failure("the panel crashed on start")
    monkeypatch.setattr(kit, "ensure_service", service)
    old = kit.head()
    push(world, "panel/broken.py")
    with pytest.raises(cli.Failure, match="rolled back"):
        kit.update()
    assert kit.head() == old and not (world.mine / "panel/broken.py").exists() and attempts == [False, True]


def test_no_rollback_keeps_the_new_code(world, monkeypatch):
    kit = world.kit
    monkeypatch.setattr(kit, "ensure_env", lambda changed=False: None)
    monkeypatch.setattr(kit, "ensure_service", lambda changed, force=False: (_ for _ in ()).throw(cli.Failure("x")))
    push(world, "panel/broken.py")
    with pytest.raises(cli.Failure):
        kit.update(rollback=False)
    assert (world.mine / "panel/broken.py").exists()


def test_rollback_that_also_fails_says_so(world, monkeypatch):
    kit = world.kit
    monkeypatch.setattr(kit, "ensure_env", lambda changed=False: None)
    monkeypatch.setattr(kit, "ensure_service", lambda changed, force=False: (_ for _ in ()).throw(cli.Failure("still bad")))
    push(world, "panel/broken.py")
    with pytest.raises(cli.Failure, match="still has a problem"):
        kit.update()


# ---- health checks ------------------------------------------------------------------------------------------------

@pytest.fixture()
def keyed(world, monkeypatch):
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    key = world.mine.parent / "deploy_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    vars_file = world.mine / "inventories/local/group_vars/all.yml"
    vars_file.parent.mkdir(parents=True)
    vars_file.write_text(f'provisionkit_deploy_key_file: "{key}"\n')
    return world.kit, key


def test_deploy_key_ok(keyed):
    kit, key = keyed
    assert kit.deploy_key_path() == str(key) and kit.check_key().level == "ok"


def test_deploy_key_with_passphrase_is_flagged(keyed):
    kit, key = keyed
    key.unlink()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(key)], check=True)
    r = kit.check_key()
    assert r.level == "warn" and "passphrase" in r.detail


def test_deploy_key_open_to_others_is_flagged(keyed):
    kit, key = keyed
    key.chmod(0o644)
    assert kit.check_key().level == "warn"


def test_missing_deploy_key_fails(keyed):
    kit, key = keyed
    key.unlink()
    assert kit.check_key().level == "fail"


def test_inventory_check(world):
    kit = world.kit
    kit.py = Path(sys.executable)
    assert kit.check_inventory().level == "warn"  # no inventories/local yet
    shutil.copytree(ROOT / "scripts", world.mine / "scripts", dirs_exist_ok=True)
    shutil.copytree(ROOT / "inventories" / "example", world.mine / "inventories/local")
    r = kit.check_inventory()
    assert r.level == "fail" and "documentation address" in r.detail  # the example addresses are placeholders


def test_environment_check_reports_a_missing_venv(world):
    assert world.kit.check_env().level == "fail"


def test_playbook_syntax_check_without_ansible_fails_clearly(world):
    assert world.kit.check_playbooks()[0].level == "fail"


@pytest.mark.skipif(not (ROOT / ".venv/bin/ansible-playbook").exists(), reason="needs the project virtualenv")
def test_playbook_syntax_check_with_ansible(world):
    kit = world.kit
    (world.mine / ".venv").symlink_to(ROOT / ".venv")
    (world.mine / "playbooks").mkdir()
    (world.mine / "playbooks/ok.yml").write_text("---\n- hosts: localhost\n  tasks:\n    - ansible.builtin.ping:\n")
    assert kit.check_playbooks()[0].level == "ok"
    (world.mine / "playbooks/bad.yml").write_text("---\n- hosts: localhost\n  tasks:\n    - not_a_real_module_xyz:\n")
    r = kit.check_playbooks()[0]
    assert r.level == "fail" and "bad.yml" in r.detail and "ok.yml" not in r.detail


def serve(status):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("status", [200, 400])
def test_panel_responds_to_any_http_answer(world, status):
    srv = serve(status)
    try:
        assert world.kit.panel_responds(srv.server_address[1])
    finally:
        srv.shutdown()


def test_panel_responds_false_when_nothing_listens(world):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not world.kit.panel_responds(port)


def test_report_and_exit_summary(world):
    world.kit.report([cli.Result("ok", "a"), cli.Result("warn", "b", "careful"), cli.Result("fail", "c", "broken")])
    text = "\n".join(world.out)
    assert "WARN  b: careful" in text and "FAIL  c: broken" in text and "1 problem(s)" in text
    world.out.clear()
    world.kit.report([cli.Result("ok", "a")])
    assert "Everything is good to go." in "\n".join(world.out)


def test_needs_root_rules(world, monkeypatch):
    monkeypatch.setattr(world.kit, "service_installed", lambda: False)
    assert cli.needs_root(world.kit, "install") and not cli.needs_root(world.kit, "update")
    monkeypatch.setattr(world.kit, "service_installed", lambda: True)
    assert cli.needs_root(world.kit, "update") and not cli.needs_root(world.kit, "check")


def test_main_prints_version_and_status(world, capsys):
    assert cli.main(["version"], kit=world.kit) == 0
    assert capsys.readouterr().out.strip().endswith("ignore")  # newest commit message in the fixture
    world.kit.git("checkout", "-q", "-b", "side")
    assert cli.main(["status", "--no-fetch"], kit=world.kit) == 0
    assert any("side" in ln for ln in world.out)


def test_main_turns_failures_into_exit_code_1(world, capsys):
    (world.mine / "README.md").write_text("edited")
    assert cli.main(["update", "--skip-env", "--skip-service"], kit=world.kit) == 1
    assert "local changes" in capsys.readouterr().err
