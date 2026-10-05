"""Compare each node's baseline manifest with the controller's checkout.

The baseline_manifest role records the git commit a node was configured from. A node is "behind" only when files
that change what the baseline does (roles and the baseline playbook) changed since that commit. Panel, CLI and
docs commits do not make every node look out of date.
"""
import re
import subprocess
import time

RELEVANT = ("roles", "playbooks/baseline.yml")
COMMIT = re.compile(r"(?:^|-g)([0-9a-f]{40})(-dirty)?$")  # plain `git describe --abbrev=40` output, with or without tags
LABELS = {"current": "Current", "behind": "Behind", "none": "No manifest", "unknown": "Unknown commit",
          "dirty": "Uncommitted", "invalid": "Unreadable"}
ATTENTION = ("behind", "unknown", "dirty", "invalid")
_head = {"at": 0.0, "root": None, "value": None}
_cache = {}


def _git(root, *args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def controller_head(root, ttl=15):
    """The commit the controller's checkout is on now (cached for a few seconds, since every page asks)."""
    if _head["root"] == str(root) and time.time() - _head["at"] < ttl:
        return _head["value"]
    r = _git(root, "rev-parse", "HEAD")
    value = r.stdout.strip() if r and r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", r.stdout.strip()) else None
    _head.update(at=time.time(), root=str(root), value=value)
    return value


def _result(state, detail="", behind=0, files=()):
    return {"state": state, "label": LABELS[state], "detail": detail, "behind": behind, "files": list(files)}


def classify(manifest, root, head=None):
    """Return {state, label, detail, behind, files} for one node's manifest. Never raises."""
    if manifest is None:
        return _result("none", "This node has no baseline manifest yet. Run the baseline on it to record one.")
    m = COMMIT.search(str(manifest.get("configuration_commit", "")) if isinstance(manifest, dict) else "")
    if not m:
        return _result("invalid", "The manifest's configuration commit is missing or not a full commit hash.")
    commit, dirty = m.group(1), bool(m.group(2))
    head = head or controller_head(root)
    if dirty:
        return _result("dirty", "The node was configured from a checkout with uncommitted changes, so the exact "
                                "configuration cannot be reconstructed.")
    if not head:
        return _result("unknown", "The controller's own commit could not be read, so nothing to compare against.")
    if commit == head:
        return _result("current", "Configured from the controller's current commit.")
    key = (str(root), commit, head)
    if key in _cache:
        return _cache[key]
    _cache[key] = out = _compare(root, commit, head)
    return out


def _compare(root, commit, head):
    if (_git(root, "cat-file", "-e", f"{commit}^{{commit}}") or _Fail).returncode != 0:
        return _result("unknown", "The node was configured from a commit this checkout does not have.")
    if (_git(root, "merge-base", "--is-ancestor", commit, head) or _Fail).returncode != 0:
        return _result("unknown", "The node was configured from a commit that is not in this branch's history.")
    count = int((_git(root, "rev-list", "--count", f"{commit}..{head}") or _Zero).stdout.strip() or 0)
    diff = _git(root, "diff", "--name-only", commit, head, "--", *RELEVANT)
    files = [ln for ln in (diff.stdout.splitlines() if diff else []) if ln]
    if not files:
        return _result("current", f"{count} newer commit(s), none of which change roles or the baseline playbook.",
                       count)
    return _result("behind", f"{count} commit(s) behind. {len(files)} role or playbook file(s) changed since.",
                   count, files[:12])


class _Fail:
    returncode = 1
    stdout = ""


class _Zero:
    stdout = "0"
