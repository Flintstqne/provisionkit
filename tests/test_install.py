"""One-command install: the auto inventory, the validator's placeholder check and install.sh. Run: python -m pytest tests/test_install.py"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from panel.inventory import Inventory, InventoryError, first_authorized_key, init_local  # noqa: E402
from validate_inventory import validate  # noqa: E402

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdeploykey provisionkit-deploy"
ADMIN = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIadminkey you@laptop"


@pytest.fixture()
def tree(tmp_path):
    shutil.copytree(ROOT / "inventories" / "example", tmp_path / "inventories" / "example")
    return tmp_path


def auto(address="10.99.0.5"):
    return {"name": "pk-control", "address": address, "deploy_key_file": "/home/me/.ssh/provisionkit_ed25519",
            "deploy_public_key": PUB, "admin_key": ADMIN}


def test_auto_inventory_is_valid_and_has_this_controller_and_no_nodes(tree):
    dest = init_local(tree, auto=auto())
    assert validate(dest) == []
    inv = Inventory(dest)
    assert inv.groups()["controllers"] == ["pk-control"] and inv.groups()["workload_nodes"] == []
    text = (dest / "group_vars" / "all.yml").read_text()
    assert "10.99.0.5/32" in text and PUB in text and ADMIN in text and "192.0.2." not in text
    assert "/home/me/.ssh/provisionkit_ed25519" in text and "replace-in-local-inventory" not in text


def test_nodes_can_be_added_to_the_empty_auto_inventory(tree):
    inv = Inventory(init_local(tree, auto=auto()))
    inv.add_host("pk-new01", "10.99.0.20", ["workload_nodes"])
    assert inv.groups()["workload_nodes"] == ["pk-new01"] and inv.hosts()["pk-new01"]["address"] == "10.99.0.20"
    assert validate(inv.dir) == []


def test_the_plain_copy_is_unchanged_and_an_existing_inventory_is_never_replaced(tree):
    dest = init_local(tree)
    assert (dest / "hosts.yml").read_text() == (tree / "inventories" / "example" / "hosts.yml").read_text()
    with pytest.raises(InventoryError, match="already exists"):
        init_local(tree, auto=auto())


def test_the_validator_rejects_example_keys_in_a_real_inventory_only(tree):
    dest = init_local(tree)
    (dest / "hosts.yml").write_text((dest / "hosts.yml").read_text().replace("192.0.2", "10.9.9"))
    errs = validate(dest)
    assert any("placeholder" in e for e in errs)
    assert not any("placeholder" in e for e in validate(tree / "inventories" / "example", example=True))


def test_first_authorized_key_skips_comments_and_garbage(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "authorized_keys").write_text(f"# comment\n\nnot a key\n{ADMIN}\nssh-rsa AAAA other\n")
    assert first_authorized_key(tmp_path) == ADMIN
    assert first_authorized_key(tmp_path / "nowhere") is None


def run_panel(tmp_path, *args, home=None):
    env = dict(os.environ, HOME=str(home or tmp_path), PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "panel", *args], cwd=ROOT, env=env, text=True, capture_output=True)


def test_init_inventory_auto_says_what_is_missing(tmp_path):
    r = run_panel(tmp_path, "init-inventory", "--auto", "--deploy-key", str(tmp_path / "nokey"))
    assert r.returncode != 0 and "Create the deploy key first" in r.stderr
    key = tmp_path / "k"
    key.write_text("x")
    Path(str(key) + ".pub").write_text(PUB + "\n")
    r = run_panel(tmp_path, "init-inventory", "--auto", "--deploy-key", str(key), "--address", "10.99.0.5")
    assert r.returncode != 0 and "No admin SSH key" in r.stderr


def test_create_user_reads_a_password_file(tmp_path):
    env = dict(os.environ, PANEL_INSTANCE=str(tmp_path / "inst"), PYTHONPATH=str(ROOT))
    (tmp_path / "pw").write_text("a long enough password\n")
    r = subprocess.run([sys.executable, "-m", "panel", "create-user", "carol", "--role", "admin", "--password-file",
                        str(tmp_path / "pw")], cwd=ROOT, env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_scripts_parse_and_help_works():
    for name in ("install.sh", "install_panel.sh", "demo.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0
    r = subprocess.run(["bash", str(ROOT / "scripts" / "install.sh"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--no-service" in r.stdout and "--check" in r.stdout


def test_install_refuses_bad_options():
    bad = subprocess.run(["bash", str(ROOT / "scripts" / "install.sh"), "--port", "80x"], capture_output=True, text=True)
    assert bad.returncode != 0 and "port must be a number" in bad.stderr
    bad = subprocess.run(["bash", str(ROOT / "scripts" / "install.sh"), "--admin", "a b"], capture_output=True, text=True)
    assert bad.returncode != 0 and "admin name" in bad.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="install.sh refuses to run as root, which is the point of the test")
def test_install_end_to_end_without_the_service(tmp_path):
    """A fresh copy of the repository, a fresh home directory, the real script, real pip. Then run it again."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    repo.mkdir()
    (home / ".ssh" / "authorized_keys").write_text(ADMIN + "\n")
    files = subprocess.run(["git", "-c", "safe.directory=*", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout
    for f in files.splitlines():
        if (ROOT / f).is_file():
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, repo / f)
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    env = dict(os.environ, HOME=str(home))
    first = subprocess.run(["bash", str(repo / "scripts" / "install.sh"), "--no-service", "--yes", "--address", "10.99.0.5"],
                           cwd=tmp_path, env=env, text=True, capture_output=True, timeout=600)
    assert first.returncode == 0, first.stdout + first.stderr
    out = first.stdout
    assert "The inventory is valid." in out and "Password:" in out and "Installed." in out
    assert (home / ".ssh" / "provisionkit_ed25519").stat().st_mode & 0o777 == 0o600
    assert (repo / "inventories" / "local" / "hosts.yml").exists() and (repo / "panel" / "instance" / "panel.db").exists()
    second = subprocess.run(["bash", str(repo / "scripts" / "install.sh"), "--no-service", "--yes"], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=600)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "exists, leaving it alone" in second.stdout and "Password:" not in second.stdout
