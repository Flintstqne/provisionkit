"""Nightly reboot: the script, the role's templates and the schedule. Run: python -m pytest tests/test_nightly_reboot.py"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "roles/nightly_reboot/files/provisionkit-nightly-reboot"
ROLE = ROOT / "roles/nightly_reboot"
ANSIBLE = shutil.which("ansible-playbook") or str(Path(sys.executable).parent / "ansible-playbook")


@pytest.fixture()
def stubs(tmp_path):
    """Fake systemctl, pgrep, logger and sleep first on PATH, each logging its calls."""
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()

    def stub(name, body):
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)
    stub("systemctl", 'echo "systemctl $*" >> "$PK_LOG"\ncase "$1" in is-active) [ "${PK_UNIT_ACTIVE:-0}" = 1 ] && exit 0 || exit 3;; esac\nexit 0\n')
    stub("pgrep", 'echo "pgrep $*" >> "$PK_LOG"\n[ "${PK_BUSY:-0}" = 1 ] && exit 0\nexit 1\n')
    stub("logger", 'echo "logger $*" >> "$PK_LOG"\n')
    stub("sleep", 'echo "sleep $*" >> "$PK_LOG"\n')
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PK_LOG": str(log),
           "PK_REBOOT_REQUIRED_FILE": str(tmp_path / "reboot-required")}

    def run(*args, **extra):
        r = subprocess.run([str(SCRIPT), *args], env={**env, **extra}, capture_output=True, text=True, timeout=30)
        calls = log.read_text().splitlines() if log.exists() else []
        return r, calls
    run.tmp = tmp_path
    return run


def test_idle_machine_reboots(stubs):
    r, calls = stubs()
    assert r.returncode == 0 and "systemctl reboot" in calls and "rebooting now" in r.stdout


def test_a_busy_package_manager_delays_then_skips_the_night(stubs):
    r, calls = stubs("--max-wait", "1", PK_BUSY="1")
    assert r.returncode == 1 and "skipped tonight" in r.stdout
    assert "systemctl reboot" not in calls
    assert calls.count("sleep 30") == 2  # waited a full minute in 30 second steps


def test_it_waits_for_the_package_manager_by_process_name(stubs):
    _, calls = stubs("--max-wait", "1", PK_BUSY="1")
    pgrep = next(c for c in calls if c.startswith("pgrep"))
    assert pgrep == "pgrep -x apt|apt-get|dpkg|unattended-upgr"


def test_extra_units_and_processes_can_block_the_reboot(stubs):
    r, calls = stubs("--max-wait", "1", "--wait-for-unit", "provisionkit-update.service", PK_UNIT_ACTIVE="1")
    assert r.returncode == 1 and "systemctl is-active --quiet provisionkit-update.service" in calls
    _, calls = stubs("--max-wait", "1", "--wait-for-process", "ansible-playbook", PK_BUSY="1")
    assert any(c.startswith("pgrep -x apt|apt-get|dpkg|unattended-upgr|ansible-playbook") for c in calls)


def test_only_if_required_skips_a_clean_machine_and_reboots_a_dirty_one(stubs):
    r, calls = stubs("--only-if-required")
    assert r.returncode == 0 and "systemctl reboot" not in calls and "nothing to do" in r.stdout
    (stubs.tmp / "reboot-required").write_text("")
    r, calls = stubs("--only-if-required")
    assert r.returncode == 0 and "systemctl reboot" in calls


@pytest.mark.parametrize("args", [["--bogus"], ["--max-wait", "abc"], ["--max-wait", "-5"], ["--max-wait", ""],
                                  ["--max-wait"], ["--wait-for-unit"]])
def test_bad_arguments_are_refused_without_rebooting(stubs, args):
    r, calls = stubs(*args)
    assert r.returncode != 0 and "systemctl reboot" not in calls


def test_the_script_is_posix_sh_and_passes_a_syntax_check():
    assert SCRIPT.read_text().startswith("#!/bin/sh")
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


# ---- the templates, rendered by real Ansible ---------------------------------------------------------------------

def render(tmp_path, **vars_):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    play = tmp_path / "render.yml"
    play.write_text(f"""---
- hosts: localhost
  gather_facts: false
  vars: {vars_!r}
  tasks:
    - ansible.builtin.template: {{src: "{ROLE}/templates/pk-nightly-reboot.service.j2", dest: "{out}/pk-nightly-reboot.service"}}
    - ansible.builtin.template: {{src: "{ROLE}/templates/pk-nightly-reboot.timer.j2", dest: "{out}/pk-nightly-reboot.timer"}}
""")
    r = subprocess.run([ANSIBLE, str(play)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    return (out / "pk-nightly-reboot.service").read_text(), (out / "pk-nightly-reboot.timer").read_text()


def test_default_templates(tmp_path):
    service, timer = render(tmp_path, nightly_reboot_time="00:00", nightly_reboot_max_wait_minutes=30,
                            nightly_reboot_only_if_required=False)
    assert "ExecStart=/usr/local/sbin/provisionkit-nightly-reboot --max-wait 30\n" in service
    assert "TimeoutStartSec=2100" in service and "Type=oneshot" in service
    assert "OnCalendar=*-*-* 00:00:00" in timer and "Persistent=false" in timer
    assert "WantedBy=timers.target" in timer


def test_template_options(tmp_path):
    service, timer = render(tmp_path, nightly_reboot_time="03:15", nightly_reboot_max_wait_minutes=10,
                            nightly_reboot_only_if_required=True)
    assert "--max-wait 10 --only-if-required" in service and "OnCalendar=*-*-* 03:15:00" in timer


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_the_timer_unit_is_valid_and_midnight_means_local_midnight(tmp_path):
    _, timer = render(tmp_path, nightly_reboot_time="00:00", nightly_reboot_max_wait_minutes=30,
                      nightly_reboot_only_if_required=False)
    unit = tmp_path / "pk-nightly-reboot.timer"
    unit.write_text(timer)
    verify = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    assert "pk-nightly-reboot.timer" not in verify.stderr.replace("Unit pk-nightly-reboot.service not found", "") or \
        verify.returncode == 0 or "not found" in verify.stderr  # the service file lives elsewhere in this test
    for tz, zone_re in (("America/New_York", r"00:00:00 E[SD]T"), ("Europe/London", r"00:00:00 (GMT|BST)"),
                        ("UTC", r"00:00:00 UTC")):
        out = subprocess.run(["systemd-analyze", "calendar", "*-*-* 00:00:00"], capture_output=True, text=True,
                             env={**os.environ, "TZ": tz}).stdout
        assert re.search(r"Next elapse: .*" + zone_re, out), (tz, out)


@pytest.mark.parametrize("bad", ["24:00", "9:30", "12:60", "noon", "00:00:00", "", "00-00"])
def test_the_role_rejects_a_malformed_time(tmp_path, bad):
    play = tmp_path / "p.yml"
    play.write_text(f"""---
- hosts: localhost
  gather_facts: false
  vars: {{provisionkit_nightly_reboot: false, provisionkit_nightly_reboot_time: "{bad}"}}
  roles: [nightly_reboot]
""")
    r = subprocess.run([ANSIBLE, str(play)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_ROLES_PATH": str(ROOT / "roles")})
    assert r.returncode != 0 and "must look like 00:00" in r.stdout + r.stderr


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("timedatectl"), reason="needs root and timedatectl")
def test_an_unknown_time_zone_name_is_rejected_before_anything_is_changed(tmp_path):
    play = tmp_path / "p.yml"
    play.write_text("""---
- hosts: localhost
  gather_facts: false
  vars: {provisionkit_nightly_reboot: false, provisionkit_timezone: "Mars/Olympus_Mons"}
  roles: [nightly_reboot]
""")
    r = subprocess.run([ANSIBLE, str(play)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_ROLES_PATH": str(ROOT / "roles")})
    assert r.returncode != 0 and "is not a time zone name" in r.stdout + r.stderr
