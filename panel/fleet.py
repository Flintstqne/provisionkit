"""Join inventory, collected snapshots and reachability into device records for the views."""
import json
import time

from .db import snapshots

OS_ORDER = ("Online", "Stale", "Offline", "Pending")
CONTROLLER = "Controller"


def _truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_gb(b):
    return round(_num(b) / 1024**3, 1)


def parse_manifest(raw):
    """None when the node has no manifest. An empty dict, which classifies as unreadable, when it is damaged."""
    if not raw or not str(raw).strip():
        return None
    try:
        manifest = json.loads(raw)
    except ValueError:
        return {}
    return manifest if isinstance(manifest, dict) else {}


def parse_checks(snap):
    checks = []
    for c in snap.get("checks") or []:
        if "applicable" in c and not _truthy(c["applicable"]):
            checks.append({"id": c["id"], "title": c["title"], "state": "na"})
        else:
            checks.append({"id": c["id"], "title": c["title"], "state": "pass" if _truthy(c.get("ok")) else "fail"})
    return checks


def parse_storage(facts):
    out = []
    for m in facts.get("mounts") or []:
        total, free = _num(m.get("size_total")), _num(m.get("size_available"))
        if total <= 0:
            continue
        out.append({"mount": m.get("mount"), "device": m.get("device"), "fstype": m.get("fstype"),
                    "total_gb": _fmt_gb(total), "used_gb": _fmt_gb(total - free),
                    "used_pct": round((total - free) / total * 100)})
    return out


def parse_interfaces(facts):
    out = []
    for i in facts.get("interfaces") or []:
        if not isinstance(i, dict):
            continue
        v4 = i.get("ipv4") or {}
        out.append({"name": i.get("device"), "mac": i.get("macaddress", ""), "ip": v4.get("address", ""),
                    "netmask": v4.get("netmask", ""), "mtu": i.get("mtu"), "up": _truthy(i.get("active")),
                    "type": i.get("type", "")})
    return out


def build_device(host, snap, status, now, stale_after, drift_for=None):
    d = dict(host)
    d.update(snapshot=bool(snap), collected=None, os="", kernel="", arch="", cpu_model="", cpus=0, mem_mb=0,
             mem_used_pct=None, uptime_s=0, checks=[], storage=[], interfaces=[], reboot=False, facts={},
             manifest=None, drift=None)
    if snap:
        f = snap.get("facts") or {}
        total, free = _num(f.get("mem_total_mb")), _num(f.get("mem_free_mb"))
        d.update(collected=snap["_collected"], facts=f, reboot=_truthy(snap.get("reboot_required")),
                 os=f"{f.get('os', '')} {f.get('os_version', '')}".strip(), kernel=f.get("kernel", ""),
                 arch=f.get("arch", ""), cpu_model=f.get("cpu_model", ""), cpus=int(_num(f.get("cpu_count"))),
                 mem_mb=int(total), uptime_s=int(_num(f.get("uptime_s"))),
                 mem_used_pct=round((total - free) / total * 100) if total else None,
                 checks=parse_checks(snap), storage=parse_storage(f), interfaces=parse_interfaces(f),
                 manifest=parse_manifest(snap.get("manifest_raw")))
        if drift_for:
            d["drift"] = drift_for(d["manifest"])
    if "controllers" in d["groups"]:
        d["status"] = CONTROLLER  # the machine running the panel; not scanned
    elif status is not None and not status["reachable"]:
        d["status"] = "Offline"
    elif not snap:
        d["status"] = "Pending"
    elif now - d["collected"] > stale_after:
        d["status"] = "Stale"
    else:
        d["status"] = "Online"
    scored = [c for c in d["checks"] if c["state"] != "na"]
    d["checks_total"] = len(scored)
    d["checks_passed"] = sum(c["state"] == "pass" for c in scored)
    if d["status"] == CONTROLLER:
        d["compliance"] = "N/A"
    elif not snap:
        d["compliance"] = "Unknown"
    else:
        d["compliance"] = "Compliant" if d["checks_passed"] == d["checks_total"] else "Non-compliant"
    return d


def devices(inv, db, stale_after, drift_for=None):
    snaps = snapshots(db)
    status = {r["host"]: r for r in db.execute("SELECT * FROM host_status")}
    now = time.time()
    return {n: build_device(h, snaps.get(n), status.get(n), now, stale_after, drift_for)
            for n, h in inv.hosts().items()}


def summary(devs):
    vals = [d for d in devs.values() if d["status"] != CONTROLLER]
    by_status = {s: sum(d["status"] == s for d in vals) for s in OS_ORDER}
    by_os = {}
    for d in vals:
        k = d["os"] or "Not collected"
        by_os[k] = by_os.get(k, 0) + 1
    scanned = [d for d in vals if d["snapshot"]]
    failing = {}
    for d in scanned:
        for c in d["checks"]:
            if c["state"] == "fail":
                failing.setdefault(c["title"], []).append(d["name"])
    drift = {}
    for d in vals:
        if d["drift"]:
            drift[d["drift"]["state"]] = drift.get(d["drift"]["state"], 0) + 1
    return {
        "drift": drift, "drift_attention": sum(drift.get(k, 0) for k in ("behind", "unknown", "dirty", "invalid")),
        "total": len(vals), "status": by_status, "os": sorted(by_os.items(), key=lambda kv: -kv[1]),
        "compliant": sum(d["compliance"] == "Compliant" for d in scanned), "scanned": len(scanned),
        "reboot": sum(d["reboot"] for d in vals), "findings": sum(len(v) for v in failing.values()),
        "failing": sorted(failing.items(), key=lambda kv: -len(kv[1])),
        "compliance_pct": round(100 * sum(d["compliance"] == "Compliant" for d in scanned) / len(scanned))
        if scanned else None,
    }


def humanize_uptime(s):
    s = int(s)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60}m"


def ago(ts, now=None):
    if not ts:
        return "never"
    s = int((now or time.time()) - ts)
    if s < 60:
        return "just now"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
