"""Panel settings. Everything is overridable with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root (the controller checkout)
INSTANCE = Path(os.environ.get("PANEL_INSTANCE", ROOT / "panel" / "instance"))


def inventory_dir():
    """inventories/local when it exists, else the read-only example inventory."""
    env = os.environ.get("PANEL_INVENTORY")
    if env:
        return Path(env)
    local = ROOT / "inventories" / "local"
    return local if local.exists() else ROOT / "inventories" / "example"


class Config:
    DEMO = os.environ.get("PANEL_DEMO") == "1"
    DB_PATH = INSTANCE / "panel.db"
    SNAPSHOT_DIR = INSTANCE / "snapshots"
    JOB_LOG_DIR = INSTANCE / "jobs"
    STALE_AFTER_S = int(os.environ.get("PANEL_STALE_AFTER", 6 * 3600))  # snapshot older than this is "stale"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("PANEL_SECURE_COOKIE") == "1"  # set when served over HTTPS
    PERMANENT_SESSION_LIFETIME = 8 * 3600
    MAX_CONTENT_LENGTH = 64 * 1024
