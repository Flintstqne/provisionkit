"""Encrypted backup and restore of the panel's data.

What goes in: the panel database (users, snapshots, jobs, audit log, settings, maintenance windows) and the files of
inventories/local. What stays out on purpose: the deploy key and other SSH keys, the session secret (so a restored panel
signs everyone out), job logs and the code, which GitHub holds.

The archive is a tar of those files, encrypted with a key derived from a passphrase (scrypt, then Fernet, which is
AES-128-CBC with HMAC-SHA256). Anyone with the file and a weak passphrase can guess offline, so the passphrase must be
long (16 characters or more).
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"PKBACKUP1\n"
MIN_PASSPHRASE = 16
MAX_FILE = 5 * 1024 * 1024
MAX_FILES = 500
MAX_TOTAL = 64 * 1024 * 1024
INV_PREFIX = "inventories/local/"
EXPECTED_TABLES = {"users", "snapshots", "jobs", "audit", "settings"}


class BackupError(ValueError):
    pass


def _key(passphrase, salt):
    raw = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())
    return base64.urlsafe_b64encode(raw)


def _check_passphrase(passphrase):
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")


def _inventory_files(inv_dir):
    out = []
    base = Path(inv_dir)
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if ".git" in rel.parts or p.is_symlink() or not p.is_file():
            continue
        out.append((INV_PREFIX + rel.as_posix(), p))
    return out


def _db_bytes(db_path):
    """A consistent copy of the live database, taken with SQLite's backup API."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "copy.db"
        src = sqlite3.connect(db_path, timeout=10)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return dest.read_bytes()


def create(db_path, inv_dir, passphrase, commit=""):
    """Return the encrypted archive as bytes."""
    _check_passphrase(passphrase)
    files = {"panel.db": _db_bytes(db_path)}
    for name, path in _inventory_files(inv_dir):
        if path.stat().st_size > MAX_FILE:
            raise BackupError(f"{name} is larger than {MAX_FILE // 1024 // 1024} MB, which is not an inventory file.")
        files[name] = path.read_bytes()
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
        raise BackupError("The data is larger than a backup allows.")
    manifest = {"version": 1, "created": int(time.time()), "commit": commit,
                "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    files["manifest.json"] = json.dumps(manifest, indent=1).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
    salt = os.urandom(16)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(buf.getvalue())


def _safe_name(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or name != p.as_posix() or "\\" in name or "\0" in name:
        return False
    return name in ("panel.db", "manifest.json") or (name.startswith(INV_PREFIX) and len(p.parts) > 2)


def read(blob, passphrase):
    """Decrypt and fully check an archive. Returns (manifest, {name: bytes}). Writes nothing."""
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 16 + 1:
        raise BackupError("This is not a ProvisionKit backup.")
    salt = blob[len(MAGIC): len(MAGIC) + 16]
    try:
        plain = Fernet(_key(passphrase, salt)).decrypt(blob[len(MAGIC) + 16:])
    except InvalidToken:
        raise BackupError("Wrong passphrase, or the file is damaged.") from None
    files, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            for m in tar:
                if not m.isreg() or not _safe_name(m.name):
                    raise BackupError(f"The archive holds an unexpected entry ({m.name[:60]!r}). Nothing was restored.")
                total += m.size
                if m.size > MAX_TOTAL or total > MAX_TOTAL or len(files) >= MAX_FILES:
                    raise BackupError("The archive is larger than a backup allows.")
                files[m.name] = tar.extractfile(m).read()
    except (tarfile.TarError, EOFError, OSError) as e:
        raise BackupError(f"The archive could not be read ({e.__class__.__name__}).") from None
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("The archive has no valid manifest.") from None
    if manifest.get("version") != 1 or set(manifest.get("files", {})) != set(files) or "panel.db" not in files:
        raise BackupError("The manifest does not match the files in the archive.")
    for name, digest in manifest["files"].items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise BackupError(f"{name} does not match its checksum. Nothing was restored.")
    _check_db(files["panel.db"])
    return manifest, files


def _check_db(data):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "check.db"
        p.write_bytes(data)
        db = sqlite3.connect(p)
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The database inside the backup is damaged.")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError:
            raise BackupError("The database inside the backup is not readable.") from None
        finally:
            db.close()
    if not EXPECTED_TABLES <= tables:
        raise BackupError("The database inside the backup is not a panel database.")


def restore(blob, passphrase, db_path, inv_dir):
    """Replace the panel database and inventory with the archive's. Stop the panel first.

    The current database and inventory are kept next to the originals with a .before-restore suffix. Returns a summary."""
    manifest, files = read(blob, passphrase)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    db_path, inv_dir = Path(db_path), Path(inv_dir)
    kept = []
    if db_path.exists():
        keep = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
        shutil.copy2(db_path, keep)
        kept.append(str(keep))
    for suffix in ("-wal", "-shm"):  # stale journal files would be replayed onto the restored database
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp_db = db_path.with_name(db_path.name + ".restoring")
    tmp_db.write_bytes(files["panel.db"])
    os.replace(tmp_db, db_path)
    # Sessions hold a user number, and numbers differ between databases. A new session secret signs everyone out.
    (db_path.parent / "secret_key").unlink(missing_ok=True)
    inv_files = {n[len(INV_PREFIX):]: b for n, b in files.items() if n.startswith(INV_PREFIX)}
    if inv_files:
        if inv_dir.exists():
            keep = inv_dir.with_name(f"{inv_dir.name}.before-restore-{stamp}")
            shutil.copytree(inv_dir, keep, symlinks=True)
            kept.append(str(keep))
        for rel, data in inv_files.items():
            dest = inv_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o600)
    return {"created": manifest.get("created"), "commit": manifest.get("commit", ""), "files": len(files),
            "inventory_files": len(inv_files), "kept": kept}
