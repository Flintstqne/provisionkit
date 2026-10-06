"""The compliance report: one row per device and check, with the time it was taken and the commit it was checked against."""
import csv
import io
import time

from . import drift


def build(devs, root, now=None):
    now = now or time.time()
    head = drift.controller_head(root) or ""
    rows = []
    for d in devs.values():
        if not d["snapshot"]:
            continue
        manifest = d.get("manifest") or {}
        for c in d["checks"]:
            rows.append({"device": d["name"], "address": d["address"] or "", "check": c["title"], "id": c["id"],
                         "result": {"pass": "Pass", "fail": "Fail", "na": "N/A"}.get(c["state"], str(c["state"])),
                         "collected": d["collected"], "baseline_commit": manifest.get("configuration_commit", "")})
    pending = sorted(d["name"] for d in devs.values() if not d["snapshot"] and d["status"] != "Controller")
    failed = sum(1 for r in rows if r["result"] == "Fail")
    return {"generated": now, "controller_commit": head, "rows": rows, "pending": pending,
            "devices": len({r["device"] for r in rows}), "checks": len(rows), "failed": failed,
            "passed": sum(1 for r in rows if r["result"] == "Pass")}


def _safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in "=+-@\t\r" else v  # no CSV formula injection


def to_csv(report):
    out = io.StringIO()
    w = csv.writer(out)
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else ""  # noqa: E731
    w.writerow(["# ProvisionKit compliance report", "generated " + stamp(report["generated"]),
                "controller commit " + (report["controller_commit"] or "unknown")])
    w.writerow(["device", "address", "check_id", "check", "result", "collected_utc", "baseline_commit_on_node"])
    for r in report["rows"]:
        w.writerow([_safe(r["device"]), _safe(r["address"]), _safe(r["id"]), _safe(r["check"]), r["result"],
                    stamp(r["collected"]), _safe(r["baseline_commit"])])
    return out.getvalue()
