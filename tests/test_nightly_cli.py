"""provisionkit nightly-reboot for the controller. Run: python -m pytest tests/test_nightly_cli.py"""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("pkcli3", str(ROOT / "scripts" / "provisionkit"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("pkcli3", loader))
loader.exec_module(cli)
SRC = ROOT / "roles/nightly_reboot/files/provisionkit-nightly-reboot"


@pytest.fixture()
def box(tmp_path, monkeypatch):
    sbin, units, zones = tmp_path / "sbin", tmp_path / "units", tmp_path / "zoneinfo"
    for d in (units, zones / "America"):
        d.mkdir(parents=True)
    (zones / "America" / "New_York").write_text("tz")
    (zones / "UTC").write_text("tz")
    monkeypatch.setattr(cli, "SBIN", sbin)
    monkeypatch.setattr(cli, "SYSTEMD_DIR", units)
    monkeypatch.setattr(cli, "ZONEINFO", zones)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    calls, lines = [], []
    answers = {("timedatectl", "show"): "America/New_York\n", ("systemctl", "is-enabled"): "enabled\n",
               ("systemctl", "show"): "Tue 2026-10-06 00:00:00 EDT\n"}

    def fake_sh(cmd, check=True, root=False, timeout=600):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, answers.get(tuple(cmd[:2]), ""), "")
    kit = cli.Kit(ROOT, out=lines.append)
    monkeypatch.setattr(kit, "sh", fake_sh)
    return type("B", (), {"kit": kit, "calls": calls, "out": lines, "sbin": sbin, "units": units})


def test_enable_installs_script_service_and_timer(box):
    box.kit.nightly_reboot("enable")
    script = box.sbin / "provisionkit-nightly-reboot"
    assert script.read_text() == SRC.read_text() and oct(script.stat().st_mode & 0o777) == "0o755"
    service = (box.units / "pk-nightly-reboot.service").read_text()
    assert "--max-wait 30 --wait-for-unit provisionkit-update.service --wait-for-process ansible-playbook" in service
    timer = (box.units / "pk-nightly-reboot.timer").read_text()
    assert "OnCalendar=*-*-* 00:00:00" in timer and "Persistent=false" in timer
    assert [c[:2] for c in box.calls if c[0] == "systemctl"] == [["systemctl", "daemon-reload"],
                                                                 ["systemctl", "enable"], ["systemctl", "restart"]]
    assert any("every night at 00:00" in ln and "America/New_York" in ln for ln in box.out)


def test_options_end_up_in_the_units(box):
    box.kit.nightly_reboot("enable", time="03:15", max_wait=10, only_if_required=True)
    assert "OnCalendar=*-*-* 03:15:00" in (box.units / "pk-nightly-reboot.timer").read_text()
    service = (box.units / "pk-nightly-reboot.service").read_text()
    assert "--max-wait 10 --only-if-required" in service and "TimeoutStartSec=900" in service


@pytest.mark.parametrize("bad", ["24:00", "9:30", "12:60", "noon", "00:00:00", "", "00-00", "00:00; reboot"])
def test_a_bad_time_is_refused_and_nothing_is_written(box, bad):
    with pytest.raises(cli.Failure, match="must look like 00:00"):
        box.kit.nightly_reboot("enable", time=bad)
    assert not list(box.units.iterdir()) and box.calls == []


@pytest.mark.parametrize("wait", [0, -1, 241])
def test_a_bad_wait_is_refused(box, wait):
    with pytest.raises(cli.Failure, match="1 to 240"):
        box.kit.nightly_reboot("enable", max_wait=wait)


def test_a_time_zone_is_validated_then_set(box):
    box.kit.nightly_reboot("enable", timezone="America/New_York")
    assert ["timedatectl", "set-timezone", "America/New_York"] in box.calls
    assert box.calls.index(["timedatectl", "set-timezone", "America/New_York"]) < \
        box.calls.index(["systemctl", "daemon-reload"])


@pytest.mark.parametrize("bad", ["Mars/Olympus", "America/New_York; reboot", "../../etc/passwd", "a b", "America/", "",
                                 "$(id)", "America/New_York/../../x"])
def test_a_bad_time_zone_is_refused_before_anything_changes(box, bad):
    with pytest.raises(cli.Failure, match="not a time zone name"):
        box.kit.nightly_reboot("enable", timezone=bad)
    assert box.calls == [] and not list(box.units.iterdir())


def test_enable_and_disable_need_root(box, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    for action in ("enable", "disable"):
        with pytest.raises(cli.Failure, match="sudo"):
            box.kit.nightly_reboot(action)
    assert not list(box.units.iterdir())


def test_disable_removes_everything_and_is_safe_to_repeat(box):
    box.kit.nightly_reboot("enable")
    box.kit.nightly_reboot("disable")
    assert not list(box.units.iterdir()) and not (box.sbin / "provisionkit-nightly-reboot").exists()
    assert ["systemctl", "disable", "--now", "pk-nightly-reboot.timer"] in box.calls
    box.kit.nightly_reboot("disable")  # nothing left to remove


def test_status_when_not_installed_and_when_installed(box):
    box.kit.nightly_reboot("status")
    assert "not installed" in box.out[-1] and "America/New_York" in box.out[-1]
    box.kit.nightly_reboot("enable", time="00:00")
    box.kit.nightly_reboot("status")
    assert "enabled, at 00:00 (America/New_York)" in box.out[-1] and "Tue 2026-10-06 00:00:00 EDT" in box.out[-1]


def test_the_controller_units_use_the_same_schedule_lines_as_the_role_templates():
    timer = cli.reboot_timer_text("00:00")
    role_timer = (ROOT / "roles/nightly_reboot/templates/pk-nightly-reboot.timer.j2").read_text()
    for line in ("Persistent=false", "AccuracySec=1s", "WantedBy=timers.target"):
        assert line in timer and line in role_timer
    assert "OnCalendar=*-*-* 00:00:00" in timer and "OnCalendar=*-*-* {{ nightly_reboot_time }}:00" in role_timer


def test_root_is_needed_only_to_change_things(box):
    assert cli.needs_root(box.kit, "nightly-reboot", "enable") and cli.needs_root(box.kit, "nightly-reboot", "disable")
    assert not cli.needs_root(box.kit, "nightly-reboot", "status")


def test_main_wires_the_command(box, capsys):
    assert cli.main(["nightly-reboot", "status"], kit=box.kit) == 0
    assert cli.main(["nightly-reboot", "enable", "--time", "25:00"], kit=box.kit) == 1
    assert "must look like 00:00" in capsys.readouterr().err
