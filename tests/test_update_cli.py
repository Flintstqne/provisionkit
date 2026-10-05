"""Progress reporting for the panel's Update button. Run: python -m pytest tests/test_update_cli.py"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_cli import cli, commit, git, push, world  # noqa: E402,F401  (real git repositories and the loaded script)


@pytest.fixture()
def rec(monkeypatch, tmp_path):
    """Record every status write so the whole sequence of steps can be checked."""
    history = []

    class Recording(cli.StatusFile):
        def write(self, **fields):
            super().write(**fields)
            history.append(dict(self.data))
    monkeypatch.setattr(cli, "StatusFile", Recording)
    return type("R", (), {"history": history, "dir": tmp_path / "status", "path": tmp_path / "status" / "status.json"})


def read(rec):
    return json.loads(rec.path.read_text())


def run(world, rec, *extra):
    return cli.main(["update", "--skip-env", "--skip-service", "--status-dir", str(rec.dir), *extra], kit=world.kit)


def test_status_file_is_atomic_world_readable_and_leaves_no_temp_files(tmp_path):
    f = cli.StatusFile(tmp_path / "s")
    f.write(state="running", step="a")
    f.write(step="b")
    data = json.loads((tmp_path / "s" / "status.json").read_text())
    assert data["state"] == "running" and data["step"] == "b" and "updated" in data
    assert oct((tmp_path / "s" / "status.json").stat().st_mode & 0o777) == "0o644"
    assert [p.name for p in (tmp_path / "s").iterdir()] == ["status.json"]


def test_a_successful_update_reports_every_step(world, rec):
    push(world, "roles/x.yml")
    old = world.kit.head()
    assert run(world, rec) == 0
    steps = [h["step"] for h in rec.history]
    assert steps[0] == "Starting" and "Fetching from GitHub" in steps and "Pulling 1 new commit(s)" in steps
    assert "Running health checks" in steps and steps[-1] == "Finished"
    final = read(rec)
    assert final["state"] == "success" and final["from"] == old and final["to"] == world.kit.head() != old
    assert final["message"] == f"Updated to {world.kit.head()[:8]}." and final["commits"] == 1
    assert final["started"] <= final["finished"] and final["rolled_back"] is False


def test_nothing_to_pull_says_so(world, rec):
    assert run(world, rec) == 0
    assert read(rec)["state"] == "success" and read(rec)["message"] == "Already up to date."


def test_the_first_status_is_written_before_any_work(world, rec):
    assert run(world, rec) == 0
    assert rec.history[0]["state"] == "running" and rec.history[0]["finished"] is None


def test_a_refusal_is_reported_as_failed_with_the_reason(world, rec):
    (world.mine / "README.md").write_text("edited")
    assert run(world, rec) == 1
    final = read(rec)
    assert final["state"] == "failed" and "local changes" in final["message"] and final["finished"]
    assert "\n" not in final["message"]  # one line, safe to show as a sentence


def test_a_failed_service_step_is_reported_with_the_rollback(world, rec, monkeypatch):
    monkeypatch.setattr(world.kit, "ensure_env", lambda changed=False: None)
    calls = []

    def service(changed, force=False):
        calls.append(force)
        if not force:
            raise cli.Failure("the panel crashed on start")
    monkeypatch.setattr(world.kit, "ensure_service", service)
    old = world.kit.head()
    push(world, "panel/x.py")
    assert cli.main(["update", "--status-dir", str(rec.dir)], kit=world.kit) == 1
    final = read(rec)
    assert final["state"] == "failed" and final["rolled_back"] is True and "rolled back" in final["message"]
    assert world.kit.head() == old and calls == [False, True]


def test_failed_health_checks_after_an_update_are_reported(world, rec, monkeypatch):
    monkeypatch.setattr(world.kit, "check", lambda: [cli.Result("ok", "git"), cli.Result("fail", "panel service"),
                                                    cli.Result("fail", "deploy key")])
    assert run(world, rec) == 1
    final = read(rec)
    assert final["state"] == "failed" and "panel service, deploy key" in final["message"]


def test_an_unexpected_error_never_leaves_running_behind(world, rec, monkeypatch):
    monkeypatch.setattr(world.kit, "fetch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run(world, rec)
    final = read(rec)
    assert final["state"] == "failed" and "Unexpected error: boom" in final["message"]


def test_no_status_dir_means_no_files_and_the_same_behaviour(world, tmp_path):
    assert cli.main(["update", "--skip-env", "--skip-service"], kit=world.kit) == 0
    assert not (tmp_path / "status").exists()


def test_check_updates_records_how_far_behind_the_checkout_is(world, tmp_path):
    out = tmp_path / "avail"
    assert cli.main(["check-updates", "--status-dir", str(out)], kit=world.kit) == 0
    a = json.loads((out / "available.json").read_text())
    assert a["behind"] == 0 and a["ahead"] == 0 and a["error"] == "" and a["head"] == world.kit.head()
    push(world, "a.txt")
    push(world, "b.txt")
    commit(world.mine, "local.txt", "x", "local work")
    assert cli.main(["check-updates", "--status-dir", str(out)], kit=world.kit) == 0
    a = json.loads((out / "available.json").read_text())
    assert a["behind"] == 2 and a["ahead"] == 1


def test_check_updates_records_a_network_problem_and_exits_nonzero(world, tmp_path):
    git(world.mine, "remote", "set-url", "origin", str(world.bare) + ".gone")
    out = tmp_path / "avail"
    assert cli.main(["check-updates", "--status-dir", str(out)], kit=world.kit) == 1
    a = json.loads((out / "available.json").read_text())
    assert "Could not reach GitHub" in a["error"] and "checked" in a


def test_check_updates_needs_root_only_when_the_service_is_installed(world, monkeypatch):
    monkeypatch.setattr(world.kit, "service_installed", lambda: False)
    assert not cli.needs_root(world.kit, "check-updates")
    monkeypatch.setattr(world.kit, "service_installed", lambda: True)
    assert cli.needs_root(world.kit, "check-updates") and os.path.exists(world.mine)


# ---- the installer's units ---------------------------------------------------------------------------------------

def test_every_managed_unit_has_a_template_and_the_installer_renders_it(world):
    import subprocess
    for name, filename in cli.MANAGED_UNITS.items():
        assert (ROOT / "panel/deploy" / filename).exists(), filename
        r = subprocess.run(["bash", str(ROOT / "scripts/install_panel.sh"), "--print-unit", name, "--user", "u1"],
                           capture_output=True, text=True)
        import re
        assert r.returncode == 0 and not re.search(r"@[A-Z_]+@", r.stdout), (name, r.stdout, r.stderr)  # none left unfilled


def test_unit_is_current_compares_all_five(world, monkeypatch, tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr(cli, "UNIT_DIR", units)
    rendered = {n: f"unit text for {n}\n" for n in cli.MANAGED_UNITS}

    def fake_sh(cmd, check=True, root=False, timeout=600):
        name = cmd[cmd.index("--print-unit") + 1]
        return cli.subprocess.CompletedProcess(cmd, 0, rendered[name], "")
    monkeypatch.setattr(world.kit, "sh", fake_sh)
    assert not world.kit.unit_is_current(8080)  # nothing installed yet
    for n, f in cli.MANAGED_UNITS.items():
        (units / f).write_text(rendered[n])
    assert world.kit.unit_is_current(8080)
    (units / "provisionkit-update.path").write_text("an old version\n")
    assert not world.kit.unit_is_current(8080)  # one stale unit is enough to reinstall
    (units / "provisionkit-update.path").unlink()
    assert not world.kit.unit_is_current(8080)  # a missing unit too


def test_the_updater_cannot_be_hijacked_through_the_request_directory():
    """The root service only unlinks one fixed name there and writes nothing into any directory the panel controls."""
    service = (ROOT / "panel/deploy/provisionkit-update.service").read_text()
    assert "ExecStartPre=/bin/rm -f /var/lib/provisionkit-requests/update" in service
    assert "StandardOutput=file:/var/lib/provisionkit-update/update.log" in service
    assert "--status-dir /var/lib/provisionkit-update" in service
    assert "provisionkit-requests" not in service.replace("ExecStartPre=/bin/rm -f /var/lib/provisionkit-requests/update", "")
    installer = (ROOT / "scripts/install_panel.sh").read_text()
    assert "install -d -m 0755 -o root -g root /var/lib/provisionkit-update" in installer
    assert "install -d -m 0700 -o \"$RUN_USER\" -g \"$RUN_USER\" /var/lib/provisionkit-requests" in installer
    panel_unit = (ROOT / "panel/deploy/provisionkit-panel.service").read_text()
    assert "/var/lib/provisionkit-requests" in panel_unit and "/var/lib/provisionkit-update" not in panel_unit


class Done:
    def __init__(self, out="", code=0):
        self.stdout, self.returncode = out, code


def stub_clock(world, monkeypatch, current, tmp_path, set_code=0):
    calls = []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli, "ZONEINFO", tmp_path / "zi")
    (tmp_path / "zi" / "America").mkdir(parents=True, exist_ok=True)
    (tmp_path / "zi" / "America" / "New_York").write_text("x")

    def sh(cmd, check=True, root=False, timeout=600):
        calls.append(list(cmd))
        return Done(current if cmd[1] == "show" else "", set_code)
    monkeypatch.setattr(world.kit, "sh", sh)
    return calls


def test_the_controller_is_moved_to_new_york_time(world, monkeypatch, tmp_path):
    calls = stub_clock(world, monkeypatch, "UTC", tmp_path)
    world.kit.ensure_timezone()
    assert ["timedatectl", "set-timezone", "America/New_York"] in calls


def test_a_controller_already_on_new_york_time_is_left_alone(world, monkeypatch, tmp_path):
    calls = stub_clock(world, monkeypatch, "America/New_York", tmp_path)
    world.kit.ensure_timezone()
    assert not any(c[1] == "set-timezone" for c in calls)


def test_a_failed_clock_change_does_not_fail_the_update(world, monkeypatch, tmp_path):
    stub_clock(world, monkeypatch, "UTC", tmp_path, set_code=1)
    world.kit.ensure_timezone()
