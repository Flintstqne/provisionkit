"""Read and edit the Ansible inventory. The inventory files stay the single source of truth."""
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
from validate_inventory import validate  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SECRET_RE = re.compile(r"pass|secret|token|vault|private", re.I)
NO_ADD = {"controllers"}  # the controller is the machine running the panel

GROUP_INFO = {
    "controllers": "Ansible control node",
    "workload_nodes": "Hardened baseline targets",
    "docker_hosts": "Container hosts",
    "monitoring_servers": "Monitoring stack",
    "llm_servers": "Local LLM inference",
    "k3s_servers": "Kubernetes control plane",
    "k3s_agents": "Kubernetes agents",
}


class InventoryError(ValueError):
    pass


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class Inventory:
    def __init__(self, path, example_dir=None):
        self.dir = Path(path)
        self.hosts_file = self.dir / "hosts.yml"
        self.vars_file = self.dir / "group_vars" / "all.yml"
        self.is_example = self.dir.resolve() == (example_dir or ROOT / "inventories" / "example").resolve()

    @property
    def writable(self):
        return not self.is_example and os.access(self.hosts_file, os.W_OK)

    def _load(self):
        return _yaml().load(self.hosts_file.read_text())

    def group_vars(self):
        data = _yaml().load(self.vars_file.read_text()) if self.vars_file.exists() else {}
        return {k: _mask(k, v) for k, v in (data or {}).items()}

    def raw_vars(self):
        return _yaml().load(self.vars_file.read_text()) or {}

    def groups(self):
        children = self._load()["all"].get("children") or {}
        return {g: sorted((v or {}).get("hosts") or {}) for g, v in children.items()}

    def hosts(self):
        """name -> {address, groups, vars}"""
        out = {}
        for group, body in (self._load()["all"].get("children") or {}).items():
            for name, hv in ((body or {}).get("hosts") or {}).items():
                h = out.setdefault(name, {"name": name, "address": None, "groups": [], "vars": {}})
                h["groups"].append(group)
                for k, v in (hv or {}).items():
                    if k == "ansible_host":
                        h["address"] = str(v)
                    else:
                        h["vars"][k] = _mask(k, v)
        return dict(sorted(out.items()))

    def problems(self):
        return validate(self.dir, example=self.is_example)

    def add_host(self, name, address, groups):
        self._require_writable()
        name, address = name.strip().lower(), address.strip()
        if not NAME_RE.match(name):
            raise InventoryError("Hostname must be lowercase letters, digits and hyphens (max 63).")
        _check_address(address)
        data = self._load()
        children = data["all"]["children"]
        groups = list(dict.fromkeys(groups))
        if not groups:
            raise InventoryError("Select at least one group.")
        for g in groups:
            if g not in children:
                raise InventoryError(f"Unknown group: {g}")
            if g in NO_ADD:
                raise InventoryError(f"Devices cannot be added to {g} from the panel.")
        if name in self.hosts():
            raise InventoryError(f"A device named {name} already exists.")
        before = set(self.problems())
        for i, g in enumerate(groups):
            if children[g].get("hosts") is None:
                children[g]["hosts"] = CommentedMap()
            entry = CommentedMap({"ansible_host": address}) if i == 0 else CommentedMap()
            entry.fa.set_flow_style()
            children[g]["hosts"][name] = entry
        self._commit(data, before)

    def remove_host(self, name):
        self._require_writable()
        if name not in self.hosts():
            raise InventoryError("Unknown device.")
        data = self._load()
        for body in data["all"]["children"].values():
            hosts = (body or {}).get("hosts") or {}
            hosts.pop(name, None)
        self._commit(data, set(self.problems()))

    def _require_writable(self):
        if not self.writable:
            raise InventoryError("Inventory is read-only. Run `python -m panel init-inventory` to create inventories/local.")

    def _commit(self, data, errors_before):
        """Validate in a scratch copy, back up the current file, then replace atomically."""
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "inv"
            shutil.copytree(self.dir, scratch)
            _yaml().dump(data, scratch / "hosts.yml")
            new_errors = [e for e in validate(scratch, example=self.is_example) if e not in errors_before]
            if new_errors:
                raise InventoryError("; ".join(new_errors))
            backups = self.dir / ".backups"
            backups.mkdir(exist_ok=True)
            shutil.copy2(self.hosts_file, backups / f"hosts.{time.strftime('%Y%m%d-%H%M%S')}.yml")
            for old in sorted(backups.glob("hosts.*.yml"))[:-20]:
                old.unlink()
            tmp_file = self.hosts_file.with_suffix(".yml.tmp")
            shutil.copy2(scratch / "hosts.yml", tmp_file)
            os.replace(tmp_file, self.hosts_file)


def _check_address(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if len(address) > 253 or not all(LABEL_RE.match(p) for p in address.split(".")):
            raise InventoryError("Address must be an IP address or a valid DNS name.") from None
        return
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        raise InventoryError("Address must be a routable unicast address.")


def _mask(key, value):
    if SECRET_RE.search(key) and "public" not in key:
        return "********"
    if isinstance(value, list):
        return [_mask(key, v) for v in value]
    if isinstance(value, dict):
        return {k: _mask(k, v) for k, v in value.items()}
    return value if isinstance(value, (int, float, bool, type(None))) else str(value)


def detect_address():
    """The address this machine uses to reach the network. A UDP connect sends no packet, it only picks a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            addr = sock.getsockname()[0]
        return None if addr.startswith("127.") or addr == "0.0.0.0" else addr
    except OSError:
        return None


def first_authorized_key(home=None):
    """The first public key in the installing user's authorized_keys: usually the key of their own workstation."""
    f = Path(home or Path.home()) / ".ssh" / "authorized_keys"
    try:
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.split()[0].startswith(("ssh-", "ecdsa-", "sk-")):
                return line
    except OSError:
        pass
    return None


def init_local(root=ROOT, auto=None):
    """Copy the example inventory to inventories/local for editing.

    With `auto` (a dict: name, address, deploy_key_file, deploy_public_key, admin_key) the copy is made usable at once: the
    controller is this machine, no managed nodes yet (add them in the panel), the controller is the only management source,
    and the deploy and admin keys are filled in. Without it the example is copied unchanged."""
    dest = root / "inventories" / "local"
    if dest.exists():
        raise InventoryError(f"{dest} already exists.")
    shutil.copytree(root / "inventories" / "example", dest)
    if not auto:
        return dest
    hosts = {"all": {"children": {"controllers": {"hosts": {auto["name"]: {"ansible_host": auto["address"]}}}}}}
    for group in ("workload_nodes", "docker_hosts", "monitoring_servers", "k3s_servers", "k3s_agents", "llm_servers"):
        hosts["all"]["children"][group] = {"hosts": {}}
    y = _yaml()
    with open(dest / "hosts.yml", "w") as f:
        f.write("---\n# Written by the installer. Add managed nodes in the panel (Devices, Add device).\n")
        y.dump(hosts, f)
    gv = dest / "group_vars" / "all.yml"
    text = gv.read_text()
    text = re.sub(r"^provisionkit_management_sources:\n(?:  - .*\n)+",
                  f"provisionkit_management_sources:\n  - {auto['address']}/32  # this controller\n", text, flags=re.M)
    text = re.sub(r"^provisionkit_deploy_public_key:.*$", "provisionkit_deploy_public_key: " + json.dumps(auto["deploy_public_key"]),
                  text, flags=re.M)
    text = re.sub(r"^provisionkit_deploy_key_file:.*$", "provisionkit_deploy_key_file: " + auto["deploy_key_file"], text, flags=re.M)
    text = re.sub(r"^(    ssh_keys: )\[.*\]$", lambda m: m.group(1) + "[" + json.dumps(auto["admin_key"]) + "]", text, flags=re.M)
    gv.write_text(text)
    return dest
