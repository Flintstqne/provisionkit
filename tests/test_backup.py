"""Encrypted backup and restore. Run: python -m pytest tests/test_backup.py"""
import io
import json
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from cryptography.fernet import Fernet  # noqa: E402
from panel import backup  # noqa: E402
from panel.db import connect, init_db  # noqa: E402
from test_panel import app, as_user, csrf, post  # noqa: E402,F401

PW = "correct horse battery staple"


@pytest.fixture()
def data(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    db = inst / "panel.db"
    init_db(db)
    c = connect(db)
    c.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('alice', 'h', 'admin', 1)")
    c.execute("INSERT INTO settings VALUES ('timezone', 'America/New_York')")
    c.commit()
    c.close()
    (inst / "secret_key").write_text("session-secret")
    inv = tmp_path / "inventories" / "local"
    (inv / "group_vars").mkdir(parents=True)
    (inv / "hosts.yml").write_text("all: {}\n")
    (inv / "group_vars" / "all.yml").write_text("provisionkit_ssh_port: 22\n")
    (inv / ".git").mkdir()
    (inv / ".git" / "config").write_text("secret")
    (tmp_path / "id_ed25519").write_text("PRIVATE KEY")
    (inv / "link").symlink_to(tmp_path / "id_ed25519")
    return db, inv, tmp_path


def test_round_trip_restores_database_and_inventory(data, tmp_path):
    db, inv, _ = data
    blob = backup.create(db, inv, PW, commit="a" * 40)
    c = connect(db)
    c.execute("DELETE FROM users")
    c.commit()
    c.close()
    (inv / "hosts.yml").write_text("changed")
    info = backup.restore(blob, PW, db, inv)
    assert info["files"] == 3 and info["commit"] == "a" * 40
    assert connect(db).execute("SELECT username FROM users").fetchone()[0] == "alice"
    assert (inv / "hosts.yml").read_text() == "all: {}\n"
    assert len(info["kept"]) == 2 and all(Path(k).exists() for k in info["kept"])
    assert oct((inv / "hosts.yml").stat().st_mode)[-3:] == "600"


def test_keys_secret_git_data_and_links_stay_out(data):
    db, inv, _ = data
    manifest, files = backup.read(backup.create(db, inv, PW), PW)
    assert sorted(files) == ["inventories/local/group_vars/all.yml", "inventories/local/hosts.yml", "panel.db"]
    assert b"PRIVATE KEY" not in b"".join(files.values()) and b"session-secret" not in b"".join(files.values())


def test_restore_rotates_the_session_secret(data):
    db, inv, tmp = data
    blob = backup.create(db, inv, PW)
    backup.restore(blob, PW, db, inv)
    assert not (db.parent / "secret_key").exists()


def test_the_archive_is_encrypted(data):
    db, inv, _ = data
    blob = backup.create(db, inv, PW)
    assert blob.startswith(backup.MAGIC) and b"alice" not in blob and b"provisionkit_ssh_port" not in blob


def test_wrong_passphrase_and_short_passphrase(data):
    db, inv, _ = data
    blob = backup.create(db, inv, PW)
    with pytest.raises(backup.BackupError, match="Wrong passphrase"):
        backup.read(blob, "another long passphrase here")
    with pytest.raises(backup.BackupError, match="at least 16"):
        backup.create(db, inv, "short")


def test_a_wrong_passphrase_changes_nothing(data):
    db, inv, tmp = data
    blob = backup.create(db, inv, PW)
    before = db.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.restore(blob, "not the passphrase at all", db, inv)
    assert db.read_bytes() == before and not list(db.parent.glob("*.before-restore-*"))


def test_tampering_is_detected(data):
    db, inv, _ = data
    blob = bytearray(backup.create(db, inv, PW))
    blob[-5] ^= 0xFF
    with pytest.raises(backup.BackupError):
        backup.read(bytes(blob), PW)
    with pytest.raises(backup.BackupError, match="not a ProvisionKit backup"):
        backup.read(b"hello", PW)


def forge(entries, passphrase=PW, manifest=True):
    """An archive made with the right passphrase but hostile contents."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    salt = b"s" * 16
    return backup.MAGIC + salt + Fernet(backup._key(passphrase, salt)).encrypt(buf.getvalue())


@pytest.mark.parametrize("name", ["../evil", "/etc/passwd", "inventories/local/../../x", "inventories/other/x",
                                  "panel.db.bak", "inventories/local/", "a\\b"])
def test_hostile_member_names_are_refused(name):
    with pytest.raises(backup.BackupError, match="unexpected entry"):
        backup.read(forge({name: b"x"}), PW)


def test_links_in_the_archive_are_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("inventories/local/hosts.yml")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
        tar.addfile(info)
    salt = b"s" * 16
    blob = backup.MAGIC + salt + Fernet(backup._key(PW, salt)).encrypt(buf.getvalue())
    with pytest.raises(backup.BackupError, match="unexpected entry"):
        backup.read(blob, PW)


def test_checksum_mismatch_and_a_database_that_is_not_the_panels(data, tmp_path):
    db, inv, _ = data
    manifest, files = backup.read(backup.create(db, inv, PW), PW)
    files["inventories/local/hosts.yml"] = b"tampered"
    forged = forge({**files, "manifest.json": json.dumps(manifest).encode()})
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.read(forged, PW)
    other = tmp_path / "other.db"
    sqlite3.connect(other).executescript("CREATE TABLE t (x);")
    import hashlib
    d = other.read_bytes()
    m = {"version": 1, "created": 1, "commit": "", "files": {"panel.db": hashlib.sha256(d).hexdigest()}}
    with pytest.raises(backup.BackupError, match="not a panel database"):
        backup.read(forge({"panel.db": d, "manifest.json": json.dumps(m).encode()}), PW)


def test_a_restore_into_a_fresh_install_needs_no_existing_files(data, tmp_path):
    db, inv, _ = data
    blob = backup.create(db, inv, PW)
    new_db, new_inv = tmp_path / "fresh" / "panel.db", tmp_path / "fresh" / "inv"
    new_db.parent.mkdir()
    info = backup.restore(blob, PW, new_db, new_inv)
    assert info["kept"] == [] and (new_inv / "hosts.yml").exists()
    assert connect(new_db).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_an_old_database_gains_the_jobs_meta_column(tmp_path):
    p = tmp_path / "old.db"
    sqlite3.connect(p).executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, user TEXT NOT NULL, "
        "status TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL, exit_code INTEGER);")
    init_db(p)
    assert "meta" in [r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(jobs)")]
    init_db(p)  # and a second start does nothing


# ---- the panel's download button ---------------------------------------------------------------------------------

def test_download_is_admin_only_and_needs_matching_long_passphrases(app):
    assert post(as_user(app, "olga"), "/settings/backup", passphrase=PW, passphrase2=PW).status_code == 403
    admin = as_user(app, "alice")
    r = post(admin, "/settings/backup", passphrase=PW, passphrase2="different passphrase!!", follow_redirects=True)
    assert "differ" in r.get_data(as_text=True)
    r = post(admin, "/settings/backup", passphrase="short", passphrase2="short", follow_redirects=True)
    assert "at least 16" in r.get_data(as_text=True)


def test_download_returns_a_restorable_archive_and_audits_without_the_passphrase(app, tmp_path):
    admin = as_user(app, "alice")
    r = post(admin, "/settings/backup", passphrase=PW, passphrase2=PW)
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    manifest, files = backup.read(r.data, PW)
    assert "panel.db" in files
    rows = connect(app.config["DB_PATH"]).execute("SELECT * FROM audit WHERE action='backup.download'").fetchall()
    assert len(rows) == 1 and PW not in " ".join(str(v) for v in dict(rows[0]).values())
