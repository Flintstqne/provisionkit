"""Guided setup for a new device: review and trust its SSH host key, and work out which setup steps are done.

The panel never handles the initial admin login of a server, so bootstrapping stays a command run on the controller.
"""
import os
import re
import subprocess
from pathlib import Path

KEY_TYPES = ("ed25519", "ecdsa", "rsa")  # strongest first
FP_RE = re.compile(r"\b(SHA256:[A-Za-z0-9+/]{43})\b")


class SetupError(ValueError):
    pass


def known_hosts_path():
    return Path(os.environ.get("PANEL_KNOWN_HOSTS") or Path.home() / ".ssh" / "known_hosts")


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False, **kw)
    except FileNotFoundError:
        raise SetupError(f"{cmd[0]} is not installed on the controller (apt install openssh-client).") from None
    except subprocess.TimeoutExpired:
        raise SetupError("The server did not answer in time.") from None


def _target(address, port):
    if not address or address.startswith("-"):
        raise SetupError("Invalid address.")
    return address if int(port) == 22 else f"[{address}]:{int(port)}"


def scan_host_key(address, port=22):
    """Fetch the server's host key without trusting it. Returns (known_hosts line, key type, fingerprint)."""
    _target(address, port)
    for kind in KEY_TYPES:
        r = _run(["ssh-keyscan", "-T", "5", "-t", kind, "-p", str(int(port)), "--", address])
        lines = [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]
        if not lines:
            continue
        fp = _run(["ssh-keygen", "-lf", "-"], input=lines[0] + "\n")
        found = FP_RE.search(fp.stdout)
        if found:
            return lines[0], kind, found.group(1)
    raise SetupError(f"Could not read a host key from {address}:{int(port)}. Is SSH running and reachable?")


def is_trusted(address, port=22):
    path = known_hosts_path()
    if not path.exists():
        return False
    return _run(["ssh-keygen", "-F", _target(address, port), "-f", str(path)]).returncode == 0


def trust_host_key(address, port, expected_fp):
    """Add the host key to known_hosts, but only if it still has the fingerprint the admin reviewed."""
    line, _kind, fp = scan_host_key(address, port)
    if fp != expected_fp:
        raise SetupError(f"The host key changed since you reviewed it ({fp}). Nothing was trusted. Start again.")
    if is_trusted(address, port):
        return False
    path = known_hosts_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")
    return True


def bootstrap_command(root, inventory_dir, name):
    """The one step done by hand, because it needs the server's initial admin login."""
    inner = (f"cd {root} && .venv/bin/ansible-playbook playbooks/bootstrap.yml -i {inventory_dir}/hosts.yml "
             f"--limit {name} -u INSTALLER_USER --ask-pass --ask-become-pass")
    return f"sudo -u {os.environ.get('USER') or 'provisionkit'} -H bash -c '{inner}'"


def steps(device, trusted, last_collect, bootstrap_cmd):
    """Setup checklist for a device with no data yet. Each step: key, title, state, detail."""
    permission_denied = bool(last_collect and "permission denied" in (last_collect["message"] or "").lower())
    out = [{"key": "registered", "title": "Added to the inventory", "state": "done", "detail": ""}]
    out.append({"key": "hostkey", "title": "Server's SSH host key trusted",
                "state": "done" if trusted else "todo",
                "detail": "" if trusted else "Review the key's fingerprint and trust it. Host key checking stays on."})
    if device["snapshot"]:
        access = ("done", "")
    elif not trusted:
        access = ("blocked", "Trust the host key first.")
    elif permission_denied:
        access = ("todo", "The deploy account is not set up on this server. Run the bootstrap command below, "
                          "then collect data.")
    elif last_collect and (last_collect["unreachable"] or last_collect["failed"]):
        access = ("failed", last_collect["message"] or "The last collection failed.")
    else:
        access = ("todo", "Collect data to confirm the controller can log in.")
    out.append({"key": "access", "title": "Deploy account works (bootstrap)", "state": access[0], "detail": access[1],
                "command": bootstrap_cmd if permission_denied else ""})
    out.append({"key": "data", "title": "Data collected", "state": "done" if device["snapshot"] else
                ("blocked" if access[0] != "done" else "todo"), "detail": ""})
    return out
