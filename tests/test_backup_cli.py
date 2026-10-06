"""The backup and restore commands, run for real against a temporary instance. Run: python -m pytest tests/test_backup_cli.py"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel.db import connect, init_db  # noqa: E402

PW = "a long enough passphrase"


@pytest.fixture()
def world(tmp_path):
    inst, inv = tmp_path / "inst", tmp_path / "inv"
    inst.mkdir()
    (inv / "group_vars").mkdir(parents=True)
    (inv / "hosts.yml").write_text("all: {}\n")
    init_db(inst / "panel.db")
    c = connect(inst / "panel.db")
    c.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('alice', 'h', 'admin', 1)")
    c.commit()
    c.close()
    (tmp_path / "pw").write_text(PW + "\n")
    env = dict(os.environ, PANEL_INSTANCE=str(inst), PANEL_INVENTORY=str(inv), PYTHONPATH=str(ROOT))
    return inst, inv, tmp_path, env


def run(env, *args, stdin=""):
    return subprocess.run([sys.executable, "-m", "panel", *args], cwd=ROOT, env=env, text=True, capture_output=True,
                          input=stdin, timeout=120)


def test_backup_then_restore_after_losing_the_data(world):
    inst, inv, tmp, env = world
    out = tmp / "b.pkbackup"
    r = run(env, "backup", "--out", str(out), "--passphrase-file", str(tmp / "pw"))
    assert r.returncode == 0, r.stderr
    assert oct(out.stat().st_mode)[-3:] == "600"
    (inst / "panel.db").unlink()
    (inv / "hosts.yml").write_text("lost")
    r = run(env, "restore", str(out), "--passphrase-file", str(tmp / "pw"), "--yes")
    assert r.returncode == 0, r.stderr
    assert connect(inst / "panel.db").execute("SELECT username FROM users").fetchone()[0] == "alice"
    assert (inv / "hosts.yml").read_text() == "all: {}\n"


def test_backup_will_not_overwrite_an_existing_file(world):
    inst, inv, tmp, env = world
    (tmp / "b.pkbackup").write_text("keep me")
    r = run(env, "backup", "--out", str(tmp / "b.pkbackup"), "--passphrase-file", str(tmp / "pw"))
    assert r.returncode != 0 and "already exists" in r.stderr and (tmp / "b.pkbackup").read_text() == "keep me"


def test_restore_asks_for_confirmation_and_stops_on_anything_else(world):
    inst, inv, tmp, env = world
    out = tmp / "b.pkbackup"
    run(env, "backup", "--out", str(out), "--passphrase-file", str(tmp / "pw"))
    (inv / "hosts.yml").write_text("current")
    r = run(env, "restore", str(out), "--passphrase-file", str(tmp / "pw"), stdin="no\n")
    assert r.returncode != 0 and "Nothing was changed" in r.stderr and (inv / "hosts.yml").read_text() == "current"


def test_restore_with_the_wrong_passphrase_fails_cleanly(world):
    inst, inv, tmp, env = world
    out = tmp / "b.pkbackup"
    run(env, "backup", "--out", str(out), "--passphrase-file", str(tmp / "pw"))
    (tmp / "pw2").write_text("not the right passphrase\n")
    r = run(env, "restore", str(out), "--passphrase-file", str(tmp / "pw2"), "--yes")
    assert r.returncode != 0 and "Wrong passphrase" in r.stderr and "Traceback" not in r.stderr


def test_short_passphrase_is_refused(world):
    inst, inv, tmp, env = world
    (tmp / "short").write_text("abc\n")
    r = run(env, "backup", "--out", str(tmp / "x.pkbackup"), "--passphrase-file", str(tmp / "short"))
    assert r.returncode != 0 and "at least 16" in r.stderr and not (tmp / "x.pkbackup").exists()


# ---- the provisionkit command around it ----------------------------------------------------------------------------
from test_cli import cli, world as kit_world  # noqa: E402,F401


def test_kit_backup_passes_an_absolute_path(kit_world, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(kit_world.kit, "run_live", lambda cmd: seen.append([str(c) for c in cmd]) or 0)
    kit_world.kit.backup(str(tmp_path / "x.pkbackup"))
    assert seen[0][1:4] == ["-m", "panel", "backup"] and seen[0][5] == str(tmp_path / "x.pkbackup")


def test_kit_restore_stops_the_panel_and_always_starts_it_again(kit_world, monkeypatch, tmp_path):
    f = tmp_path / "b.pkbackup"
    f.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(kit_world.kit, "service_installed", lambda: True)

    def sh(cmd, check=True, root=False, timeout=600):
        calls.append(list(cmd))
        return type("R", (), {"stdout": "active\n", "returncode": 0})()
    monkeypatch.setattr(kit_world.kit, "sh", sh)
    monkeypatch.setattr(kit_world.kit, "run_live", lambda cmd: 1)  # the restore fails
    with pytest.raises(cli.Failure, match="did not complete"):
        kit_world.kit.restore(str(f))
    assert ["systemctl", "stop", "provisionkit-panel.service"] in calls
    assert calls[-1] == ["systemctl", "start", "provisionkit-panel.service"]


def test_kit_restore_needs_an_existing_file(kit_world, tmp_path):
    with pytest.raises(cli.Failure, match="does not exist"):
        kit_world.kit.restore(str(tmp_path / "nope"))


def test_restore_needs_root_only_when_the_service_exists(kit_world, monkeypatch):
    monkeypatch.setattr(kit_world.kit, "service_installed", lambda: True)
    assert cli.needs_root(kit_world.kit, "restore") and not cli.needs_root(kit_world.kit, "backup")
