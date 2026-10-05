"""provisionkit baseline, with a stub ansible-playbook that records how it was called. Run: pytest tests/test_baseline.py"""
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("pkcli2", str(ROOT / "scripts" / "provisionkit"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("pkcli2", loader))
loader.exec_module(cli)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.org", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.org",
       "GIT_CONFIG_GLOBAL": "/dev/null"}
HOSTS = """---
all:
  children:
    controllers:
      hosts:
        pk-control: {ansible_host: 10.0.0.246}
    workload_nodes:
      hosts:
        pk-worker: {ansible_host: 10.0.0.51}
        pk-other: {ansible_host: 10.0.0.52}
"""
VARS = """---
provisionkit_management_sources:
  - 10.0.0.246/32
  - 10.0.0.40/32
provisionkit_admin_users:
  - {name: admin, ssh_keys: ["ssh-ed25519 AAAA admin"]}
"""
STUB = """#!/bin/sh
echo "$@" >> "$PK_LOG"
case "$*" in *"$PK_FAIL_ON"*) [ -n "$PK_FAIL_ON" ] && exit 2;; esac
exit 0
"""


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], env={**os.environ, **ENV}, check=True, capture_output=True, text=True)


@pytest.fixture()
def box(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "repo"
    (repo / "inventories/local/group_vars").mkdir(parents=True)
    (repo / "inventories/local/hosts.yml").write_text(HOSTS)
    (repo / "inventories/local/group_vars/all.yml").write_text(VARS)
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/validate_inventory.py", repo / "scripts")
    (repo / ".venv/bin").mkdir(parents=True)
    stub = repo / ".venv/bin/ansible-playbook"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (repo / ".gitignore").write_text("inventories/local/\n.venv/\n")
    (repo / "README.md").write_text("x")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PK_LOG", str(log))
    monkeypatch.setenv("PK_FAIL_ON", "")
    lines = []
    kit = cli.Kit(repo, out=lines.append)
    kit.py = Path(sys.executable)

    def calls():
        return log.read_text().splitlines() if log.exists() else []
    return type("B", (), {"repo": repo, "kit": kit, "out": lines, "calls": staticmethod(calls)})


def typed(answer):
    return lambda prompt: answer


def test_dry_run_then_apply_then_validate_in_that_order(box):
    box.kit.baseline("pk-worker", ask=typed("pk-worker"))
    c = box.calls()
    assert len(c) == 3
    assert "baseline.yml" in c[0] and "--check --diff" in c[0] and "--limit pk-worker" in c[0]
    assert "baseline.yml" in c[1] and "--check" not in c[1] and "--limit pk-worker" in c[1]
    assert "validate.yml" in c[2] and "--limit pk-worker" in c[2]
    assert all("-i inventories/local/hosts.yml" in x for x in c)


def test_only_the_named_node_is_ever_targeted(box):
    box.kit.baseline("pk-worker", ask=typed("pk-worker"))
    assert not any("pk-other" in x or "all" in x.split() for x in box.calls())


def test_wrong_confirmation_stops_after_the_dry_run(box):
    with pytest.raises(cli.Failure, match="Nothing was changed"):
        box.kit.baseline("pk-worker", ask=typed("yes"))
    assert len(box.calls()) == 1 and "--check" in box.calls()[0]


def test_yes_skips_the_confirmation(box):
    box.kit.baseline("pk-worker", yes=True, ask=lambda p: pytest.fail("must not ask"))
    assert len(box.calls()) == 3


def test_check_only_never_applies(box):
    box.kit.baseline("pk-worker", check_only=True, ask=lambda p: pytest.fail("must not ask"))
    assert len(box.calls()) == 1 and "--check" in box.calls()[0]
    assert any("Nothing was changed" in ln for ln in box.out)


def test_a_failed_dry_run_aborts_before_anything_is_applied(box, monkeypatch):
    monkeypatch.setenv("PK_FAIL_ON", "--check")
    with pytest.raises(cli.Failure, match="dry run failed"):
        box.kit.baseline("pk-worker", yes=True)
    assert len(box.calls()) == 1


def test_a_failed_apply_skips_validation_and_explains_the_firewall_rollback(box, monkeypatch):
    calls = {"n": 0}
    real = box.kit.run_live

    def flaky(cmd):
        calls["n"] += 1
        rc = real(cmd)
        return 2 if calls["n"] == 2 else rc  # the second call is the real apply
    monkeypatch.setattr(box.kit, "run_live", flaky)
    with pytest.raises(cli.Failure, match="firewall role rolls itself back"):
        box.kit.baseline("pk-worker", yes=True)
    assert not any("validate.yml" in x for x in box.calls())


def test_failed_validation_is_reported_and_exits_nonzero(box, monkeypatch):
    monkeypatch.setenv("PK_FAIL_ON", "validate.yml")
    with pytest.raises(cli.Failure, match="Validation failed"):
        box.kit.baseline("pk-worker", yes=True)


def test_tags_and_vault_flag_are_passed_to_every_run(box):
    box.kit.baseline("pk-worker", tags="audit,logging", yes=True, ask_vault=True)
    c = box.calls()
    assert "--tags audit,logging" in c[0] and "--tags audit,logging" in c[1] and "--tags" not in c[2]
    assert all("--ask-vault-pass" in x for x in c)


@pytest.mark.parametrize("tags", ["firewall; rm -rf /", "Firewall", "a b", "x,,y", "$(id)"])
def test_tags_are_validated(box, tags):
    with pytest.raises(cli.Failure, match="Tags must be"):
        box.kit.baseline("pk-worker", tags=tags, yes=True)
    assert box.calls() == []


def test_unknown_host_and_the_controller_are_refused(box):
    with pytest.raises(cli.Failure, match="not in workload_nodes"):
        box.kit.baseline("pk-nope", yes=True)
    with pytest.raises(cli.Failure, match="not in workload_nodes"):
        box.kit.baseline("pk-control", yes=True)
    assert box.calls() == []


def test_a_dirty_checkout_is_refused_because_validation_would_reject_the_node(box):
    (box.repo / "README.md").write_text("edited")
    with pytest.raises(cli.Failure, match="uncommitted changes"):
        box.kit.baseline("pk-worker", yes=True)
    assert box.calls() == []
    box.kit.baseline("pk-worker", yes=True, allow_dirty=True)  # an explicit override exists
    assert len(box.calls()) == 3


def test_untracked_and_ignored_files_do_not_count_as_dirty(box):
    (box.repo / "notes.txt").write_text("mine")
    box.kit.baseline("pk-worker", yes=True)
    assert len(box.calls()) == 3


def test_firewall_is_refused_when_the_controller_would_be_locked_out(box):
    (box.repo / "inventories/local/group_vars/all.yml").write_text(VARS.replace("  - 10.0.0.246/32\n", ""))
    with pytest.raises(cli.Failure, match="10.0.0.246 is not covered"):
        box.kit.baseline("pk-worker", yes=True)
    assert box.calls() == []


def test_a_range_that_covers_the_controller_is_accepted(box):
    (box.repo / "inventories/local/group_vars/all.yml").write_text(VARS.replace("10.0.0.246/32", "10.0.0.0/24"))
    box.kit.baseline("pk-worker", yes=True)
    assert len(box.calls()) == 3


def test_the_source_check_is_skipped_when_the_firewall_is_not_part_of_the_run(box):
    (box.repo / "inventories/local/group_vars/all.yml").write_text(VARS.replace("  - 10.0.0.246/32\n", ""))
    box.kit.baseline("pk-worker", tags="audit", yes=True)
    assert len(box.calls()) == 3


def test_the_plan_names_the_sources_and_the_rollback(box):
    box.kit.baseline("pk-worker", yes=True)
    text = "\n".join(box.out)
    assert "10.0.0.246/32" in text and "10.0.0.40/32" in text and "10 minute" in text and "pk-worker (10.0.0.51)" in text


def test_the_firewall_flag_hint_appears_only_after_a_passing_run_that_included_it(box):
    box.kit.baseline("pk-worker", yes=True)
    assert "provisionkit_firewall_applied: true" in "\n".join(box.out)
    box.out.clear()
    box.kit.baseline("pk-worker", tags="audit", yes=True)
    assert "provisionkit_firewall_applied" not in "\n".join(box.out)
    box.out.clear()
    (box.repo / "inventories/local/group_vars/all.yml").write_text(VARS + "provisionkit_firewall_applied: true\n")
    box.kit.baseline("pk-worker", yes=True)
    assert "provisionkit_firewall_applied" not in "\n".join(box.out)


def test_a_missing_or_invalid_inventory_is_refused(box):
    (box.repo / "inventories/local/hosts.yml").write_text(HOSTS.replace("10.0.0.51", "192.0.2.51"))  # documentation address
    with pytest.raises(cli.Failure, match="inventory has problems"):
        box.kit.baseline("pk-worker", yes=True)
    shutil.rmtree(box.repo / "inventories/local")
    with pytest.raises(cli.Failure, match="does not exist"):
        box.kit.baseline("pk-worker", yes=True)


def test_controllers_not_in_handles_hostnames_and_bad_entries(box):
    f = box.kit.controllers_not_in
    assert f(["10.0.0.0/24"], ["10.0.0.5"]) == []
    assert f(["10.0.0.0/24"], ["10.0.1.5"]) == ["10.0.1.5"]
    assert f(["not-an-ip", "10.0.0.5"], ["10.0.0.5"]) == []
    assert f([], ["pk-control.lan"]) == []  # a hostname cannot be checked, so it is not guessed at


def test_main_wires_the_command_and_returns_one_on_failure(box, capsys):
    assert cli.main(["baseline", "pk-nope", "--yes"], kit=box.kit) == 1
    assert "not in workload_nodes" in capsys.readouterr().err
    assert cli.main(["baseline", "pk-worker", "--yes", "--check-only"], kit=box.kit) == 0
