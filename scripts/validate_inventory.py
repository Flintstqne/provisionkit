#!/usr/bin/env python3
"""Cross-host inventory checks. Usage: validate_inventory.py INVENTORY_DIR [--example]"""
import ipaddress, sys
from pathlib import Path
import yaml

DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def validate(inv_dir, example=False):
    inv = Path(inv_dir)
    groups = yaml.safe_load((inv / "hosts.yml").read_text())["all"]["children"]
    gv = yaml.safe_load((inv / "group_vars/all.yml").read_text())
    hosts = lambda g: set((groups.get(g) or {}).get("hosts") or {})
    errs, addrs = [], {}
    for g in groups.values():
        for h, v in (g.get("hosts") or {}).items():
            if v and "ansible_host" in v:
                addrs.setdefault(v["ansible_host"], set()).add(h)
    for a, hs in addrs.items():
        if len(hs) > 1:
            errs.append(f"ansible_host {a} used by multiple hosts: {sorted(hs)}")
        try:
            doc = any(ipaddress.ip_address(a) in n for n in DOC_NETS)
        except ValueError:  # hostname, not an IP
            continue
        if doc and not example:
            errs.append(f"ansible_host {a} is a documentation address")
    k3s = hosts("k3s_servers") | hosts("k3s_agents")
    if hosts("controllers") & k3s:
        errs.append("controllers must not be in k3s groups")
    if hosts("controllers") & hosts("llm_servers"):
        errs.append("controllers must not be in llm_servers")
    if hosts("controllers") & hosts("workload_nodes"):
        errs.append("controllers must not be in workload_nodes")
    src = gv.get("provisionkit_management_sources") or []
    if not src:
        errs.append("provisionkit_management_sources is empty")
    for s in src:
        if ipaddress.ip_network(s, strict=False).prefixlen == 0:
            errs.append(f"provisionkit_management_sources contains {s}")
    if not any(u.get("ssh_keys") for u in gv.get("provisionkit_admin_users") or []):
        errs.append("provisionkit_admin_users has no SSH key")
    if not example:  # keys copied from the example inventory would be installed on real servers
        keys = [gv.get("provisionkit_deploy_public_key", "")] + [k for u in gv.get("provisionkit_admin_users") or []
                                                              for k in u.get("ssh_keys") or []]
        if any("replace-in-local-inventory" in str(k) for k in keys):
            errs.append("an SSH public key is still the example placeholder (replace-in-local-inventory)")
    return errs


if __name__ == "__main__":
    e = validate(sys.argv[1], "--example" in sys.argv)
    print("\n".join(e) or "ok")
    sys.exit(bool(e))
