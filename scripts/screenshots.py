#!/usr/bin/env python3
"""Regenerate docs/panel/*.png from the demo. Not part of CI. Needs: pip install playwright, and a Chromium it can launch.

  python -m panel demo --port 8099 &
  python scripts/screenshots.py [OUT_DIR] [CHROMIUM_PATH]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "panel")
CHROMIUM = sys.argv[2] if len(sys.argv) > 2 else None

PAGES = [  # path, file name, full page
    ("/", "dashboard", False), ("/devices", "devices", False), ("/devices/pk-web02", "device", False),
    ("/devices/pk-web02?tab=configuration", "configuration", False), ("/devices/pk-db01", "drift", True),
    ("/devices/pk-new01", "setup", True), ("/compliance", "compliance", False), ("/compliance/report", "report", True),
    ("/maintenance", "maintenance", True), ("/settings", "settings", True)]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM) if CHROMIUM else p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(BASE + "/login")
        pg.fill("input[name=username]", "demo")
        pg.fill("input[name=password]", "demo-password-123")
        pg.click("button.primary")
        for path, name, full in PAGES:
            pg.goto(BASE + path)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
        for button, name in (("Collect data", "job"), ("Preview baseline", "approve")):  # a finished simulated job each
            pg.goto(BASE + "/devices/pk-web02")
            pg.click(f"text={button}")
            pg.wait_for_url("**/jobs/*")
            pg.wait_for_timeout(4500)
            pg.reload()
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(OUT / f"{name}.png"))
        pg.goto(BASE + "/")
        pg.click("#update-open")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / "update.png"))
        browser.close()


if __name__ == "__main__":
    main()
