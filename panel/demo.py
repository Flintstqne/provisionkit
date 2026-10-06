"""Synthetic data for PANEL_DEMO=1 so the console can be shown without a lab. Never touches real inventory."""
import json
import random
import subprocess
import time

from .db import connect, save_snapshot

HOSTS = [  # name, ip, groups, os, cpus, mem_mb, failing checks, reboot, state
    ("pk-server", "10.20.0.20", ["workload_nodes", "docker_hosts", "monitoring_servers", "k3s_servers"], "24.04", 8, 16000, [], False, "ok"),
    ("pk-worker", "10.20.0.30", ["workload_nodes", "k3s_agents"], "24.04", 4, 8000, [], True, "ok"),
    ("pk-db01", "10.20.1.11", ["workload_nodes", "docker_hosts"], "24.04", 8, 32000, ["firewall"], False, "ok"),
    ("pk-web01", "10.20.1.21", ["workload_nodes", "docker_hosts"], "24.04", 4, 8000, [], False, "ok"),
    ("pk-web02", "10.20.1.22", ["workload_nodes", "docker_hosts"], "22.04", 4, 8000, ["ssh_password_auth", "auditd"], False, "ok"),
    ("pk-mon01", "10.20.1.31", ["workload_nodes", "monitoring_servers"], "24.04", 2, 4000, ["ntp_sync"], True, "ok"),
    ("pk-edge01", "10.20.2.10", ["workload_nodes"], "24.04", 2, 4000, [], False, "offline"),
    ("pk-new01", "10.20.2.20", ["workload_nodes"], "24.04", 2, 4000, [], False, "pending"),
]
CHECKS = [("ssh_password_auth", "SSH password authentication disabled"), ("ssh_root_login", "SSH root login disabled"),
          ("ntp_sync", "Time synchronised"), ("auditd", "auditd active with ProvisionKit rules"),
          ("security_updates", "Security-only unattended upgrades"), ("firewall", "Firewall default-deny incoming")]


def _inventory_yaml():
    groups = {}
    for name, ip, gs, *_ in HOSTS:
        for i, g in enumerate(gs):
            groups.setdefault(g, []).append((name, ip if i == 0 else None))
    lines = ["---", "all:", "  children:", "    controllers:", "      hosts:", "        pk-control: {ansible_host: 10.20.0.10}"]
    for g, members in groups.items():
        lines += [f"    {g}:", "      hosts:"]
        lines += [f"        {n}: {{ansible_host: {ip}}}" if ip else f"        {n}: {{}}" for n, ip in members]
    return "\n".join(lines) + "\n"


def _commits():
    """Real commits from this checkout, so the demo shows genuine drift: the current one, and the newest one that is
    behind in a way that matters (a role or the baseline playbook changed since)."""
    from .config import ROOT
    from .drift import RELEVANT

    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True).stdout.split()
    head = (git("rev-parse", "HEAD") or [""])[0]
    for c in git("rev-list", "--max-count=300", "HEAD")[1:]:
        if git("diff", "--name-only", c, "HEAD", "--", *RELEVANT):
            return head, c
    return head, "1" * 40


# which manifest each demo node carries: current, behind, dirty or none
MANIFESTS = {"pk-server": "current", "pk-worker": "current", "pk-db01": "behind", "pk-web01": "current",
             "pk-web02": "dirty", "pk-mon01": "none", "pk-edge01": "current"}


def _manifest(name, kind, head, older):
    if kind == "none":
        return ""
    commit = {"current": head, "behind": older, "dirty": head + "-dirty"}[kind]
    return json.dumps({"schema_version": 1, "node_id": name, "baseline_release": "unreleased",
                       "configuration_commit": commit, "configuration_profile": "standalone",
                       "expected_services": ["ssh", "auditd", "systemd-timesyncd"],
                       "managed_paths": ["/etc/ssh/sshd_config.d/10-provisionkit.conf", "/etc/sudoers.d/90-provisionkit"],
                       "policy_ids": ["ssh-key-only", "ssh-no-root-login", "security-updates-only",
                                      "persistent-journal", "audit-core-rules"]}, indent=2)


def _snapshot(name, ip, os_ver, cpus, mem, failing, reboot, rng, now, manifest_raw=""):
    mem_free = int(mem * rng.uniform(0.2, 0.7))
    root = int(rng.uniform(40, 220) * 1024**3)
    data_disk = int(rng.uniform(200, 900) * 1024**3)
    checks = [{"id": i, "title": t, "ok": i not in failing} for i, t in CHECKS]
    checks[-1]["applicable"] = name in ("pk-server", "pk-worker", "pk-db01")
    return {
        "host": name, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "reboot_required": reboot,
        "manifest_raw": manifest_raw,
        "timezone": "America/New_York",
        "nightly_reboot": {"installed": name != "pk-web02", "enabled": name != "pk-web02",
                           "next": "" if name == "pk-web02" else "Tue 2026-10-06 00:00:00 EDT"},
        "facts": {"os": "Ubuntu", "os_version": os_ver, "kernel": "6.8.0-45-generic" if os_ver == "24.04" else "5.15.0-122-generic",
                  "arch": "x86_64", "fqdn": f"{name}.lab.example", "hostname": name, "python": "3.12.3",
                  "cpu_model": "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz", "cpu_count": cpus, "mem_total_mb": mem,
                  "mem_free_mb": mem_free, "swap_total_mb": 2048, "uptime_s": int(rng.uniform(3600, 86400 * 60)),
                  "default_ipv4": ip,
                  "interfaces": [{"device": "enp3s0", "macaddress": "52:54:00:%02x:%02x:%02x" % (rng.randrange(256), rng.randrange(256), rng.randrange(256)),
                                  "active": True, "mtu": 1500, "type": "ether", "ipv4": {"address": ip, "netmask": "255.255.255.0"}}],
                  "mounts": [{"mount": "/", "device": "/dev/nvme0n1p2", "fstype": "ext4", "size_total": root + 40 * 1024**3,
                              "size_available": 40 * 1024**3},
                             {"mount": "/var/lib/docker", "device": "/dev/sdb1", "fstype": "xfs", "size_total": data_disk + 100 * 1024**3,
                              "size_available": 100 * 1024**3}]},
        "checks": checks,
    }


def seed(config, inv_dir):
    """Write the demo inventory and database content. Idempotent."""
    (inv_dir / "group_vars").mkdir(parents=True, exist_ok=True)
    (inv_dir / "hosts.yml").write_text(_inventory_yaml())
    (inv_dir / "group_vars" / "all.yml").write_text(
        "---\nprovisionkit_environment: lab\nprovisionkit_deploy_user: provisionkit\nprovisionkit_ssh_port: 22\n"
        "provisionkit_network_profile: standalone\nprovisionkit_automatic_reboot: false\nprovisionkit_k3s_enabled: false\n"
        "provisionkit_management_sources:\n  - 10.20.0.10/32\n  - 10.20.0.40/32\n"
        "provisionkit_deploy_public_key: \"ssh-ed25519 AAAAdemo provisionkit-deploy\"\n"
        "provisionkit_admin_users:\n  - name: admin\n    ssh_keys: [\"ssh-ed25519 AAAAdemo admin\"]\n")
    rng, now = random.Random(7), time.time()
    head, older = _commits()
    db = connect(config["DB_PATH"])
    for name, ip, _g, os_ver, cpus, mem, failing, reboot, state in HOSTS:
        if state == "pending":
            continue
        save_snapshot(db, name, _snapshot(name, ip, os_ver, cpus, mem, failing, reboot, rng, now,
                                          _manifest(name, MANIFESTS.get(name, "none"), head, older)))
        age = 2 * 86400 if state == "offline" else rng.uniform(60, 3000)
        db.execute("UPDATE snapshots SET collected=? WHERE host=?", (now - age, name))
        db.execute("INSERT OR REPLACE INTO host_status VALUES (?,?,?,?)",
                   (name, 0 if state == "offline" else 1, now - age,
                    "Failed to connect to the host via ssh: Connection timed out" if state == "offline" else ""))
    if not db.execute("SELECT 1 FROM maintenance_windows").fetchone():
        db.execute("INSERT INTO maintenance_windows (scope, days, start, minutes, note, created_by, created) VALUES "
                   "('workload_nodes', 'sat,sun', '02:00', 180, 'Weekend patch window', 'demo', ?)", (now,))
    db.commit()
    db.close()
