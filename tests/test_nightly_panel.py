"""Panel display of time zone and nightly reboot state. Run: python -m pytest tests/test_nightly_panel.py"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from panel import fleet  # noqa: E402
from panel.db import connect, save_snapshot  # noqa: E402
from test_panel import _snap, app, as_user  # noqa: E402,F401


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("text", None), ([1], None), (5, None),
    ({}, {"scheduled": False, "installed": False, "next": ""}),
    ({"enabled": True, "installed": True, "next": "Tue 2026-10-06 00:00:00 EDT"},
     {"scheduled": True, "installed": True, "next": "Tue 2026-10-06 00:00:00 EDT"}),
    ({"enabled": "True", "installed": "False"}, {"scheduled": True, "installed": False, "next": ""}),
    ({"enabled": False, "installed": True}, {"scheduled": False, "installed": True, "next": ""}),
    ({"next": "x" * 500}, {"scheduled": False, "installed": False, "next": "x" * 60}),
])
def test_parse_nightly(raw, expected):
    assert fleet.parse_nightly(raw) == expected


def put(app, host, **extra):
    db = connect(app.config["DB_PATH"])
    snap = _snap(host)
    snap.update(extra)
    save_snapshot(db, host, snap)
    db.close()


def page(app, host):
    return as_user(app, "alice").get(f"/devices/{host}").get_data(as_text=True)


def test_a_scheduled_node_shows_zone_and_next_run(app):
    put(app, "pk-worker", timezone="America/New_York",
        nightly_reboot={"installed": True, "enabled": True, "next": "Tue 2026-10-06 00:00:00 EDT"})
    html = page(app, "pk-worker")
    assert "America/New_York" in html and "Scheduled, next Tue 2026-10-06 00:00:00 EDT" in html


def test_a_node_without_the_timer_says_not_scheduled(app):
    put(app, "pk-worker", timezone="UTC", nightly_reboot={"installed": False, "enabled": False, "next": ""})
    html = page(app, "pk-worker")
    assert "Nightly reboot" in html and "Not scheduled" in html and "UTC" in html


def test_an_installed_but_inactive_timer_is_called_out(app):
    put(app, "pk-worker", nightly_reboot={"installed": True, "enabled": False, "next": ""})
    assert "Installed but not active" in page(app, "pk-worker")


def test_old_snapshots_without_the_fields_still_render(app):
    put(app, "pk-worker")
    html = page(app, "pk-worker")
    assert "Not scheduled" in html and "Time zone" in html


def test_hostile_text_from_a_node_is_escaped(app):
    put(app, "pk-worker", timezone="<script>alert(1)</script>",
        nightly_reboot={"installed": True, "enabled": True, "next": "<img src=x onerror=alert(1)>"})
    html = page(app, "pk-worker")
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html and "&lt;" in html
